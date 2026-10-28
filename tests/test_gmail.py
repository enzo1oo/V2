import base64
import email
from datetime import datetime

import pytest

from app import clock
from app.extract.ai import NullProvider
from app.gmail import (
    FAILED_LABEL,
    GmailAuthError,
    OutgoingEmail,
    build_mime,
    parse_message,
    process_inbox,
)
from app.pipeline import Services
from app.sheets import SchemaError
from tests.fakes import FakeDriveStore, FakeGmailClient, FakeSheetsBackend, FakeSheetStore

INTAKE, DONE = "GRC/To log", "GRC/Logged"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


# --- parse_message -----------------------------------------------------------------------------


def gmail_json():
    return {
        "id": "m1",
        "threadId": "t1",
        "internalDate": str(int(datetime(2026, 10, 26, 3, 0, tzinfo=clock.TZ).timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "=?UTF-8?B?R1JDIE1hbmFnZXI=?= <Mgr@Example.com>"},
                {"name": "Subject", "value": "Fwd: Data request"},
            ],
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "headers": [
                                {
                                    "name": "Content-Type",
                                    "value": 'text/plain; charset="ISO-8859-1"',
                                }
                            ],
                            "body": {"data": b64("Caf\xe9 data".encode("latin-1"))},
                        },
                        {"mimeType": "text/html", "body": {"data": b64(b"<p>Cafe</p>")}},
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "Form.pdf",
                    "body": {"attachmentId": "att-1", "size": 3},
                },
                {"mimeType": "image/png", "filename": "logo.png", "body": {"data": b64(b"png")}},
            ],
        },
    }


def test_parse_message():
    fetched = []

    def fetch(message_id, attachment_id):
        fetched.append((message_id, attachment_id))
        return b"pdf"

    message = parse_message(gmail_json(), fetch)
    assert (message.id, message.thread_id) == ("m1", "t1")
    assert (message.sender_name, message.sender_email) == ("GRC Manager", "mgr@example.com")
    assert message.subject == "Fwd: Data request"
    assert message.body_text == "Café data"
    assert message.body_html == "<p>Cafe</p>"
    assert [(a.filename, a.data) for a in message.attachments] == [
        ("Form.pdf", b"pdf"),
        ("logo.png", b"png"),
    ]
    assert fetched == [("m1", "att-1")]
    assert message.received_at == datetime(2026, 10, 26, 3, 0, tzinfo=clock.TZ)


def test_build_mime_plain_and_html():
    out = OutgoingEmail(
        to=["a@example.com"],
        cc=["b@example.com"],
        subject="[GRC] G-0001 – Title",
        text="Hi",
        html="<p>Hi</p>",
    )
    raw = build_mime(out, "intake@example.com", "GRC Tracker")
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert parsed["From"] == "GRC Tracker <intake@example.com>"
    assert parsed["Cc"] == "b@example.com"
    assert parsed.get_content_type() == "multipart/alternative"
    assert [part.get_content_type() for part in parsed.get_payload()] == ["text/plain", "text/html"]


# --- process_inbox -----------------------------------------------------------------------------


def services():
    store = FakeSheetStore(FakeSheetsBackend.from_fixtures())
    store.load()
    return Services(store=store, drive=FakeDriveStore(), ai=NullProvider())


def g_tasks(svc):
    return [task for task in svc.store.tasks() if task.id.startswith("G-")]


def test_logs_and_moves_label_after_row(fixture_text):
    gmail = FakeGmailClient()
    gmail.add("t1", INTAKE, body_text=fixture_text("manpower_planning.txt"))
    gmail.add("t2", "INBOX", body_text="not for us")
    svc = services()
    run = process_inbox(gmail, svc)
    assert (run.threads, run.created, run.failed) == (1, 1, 0)
    assert gmail.labels("t1") == {DONE}
    assert gmail.labels("t2") == {"INBOX"}
    [task] = g_tasks(svc)
    assert task.source_id == "msg-t1" and task.source == "Email"


def test_label_failure_is_retried_safely_without_second_row(fixture_text):
    gmail = FakeGmailClient()
    gmail.add("t1", INTAKE, body_text=fixture_text("manpower_planning.txt"))
    svc = services()
    gmail.fail_labelling = True
    run = process_inbox(gmail, svc)
    assert run.created == 1
    assert gmail.labels("t1") == {INTAKE}  # still labelled: next run tries again

    gmail.fail_labelling = False
    run = process_inbox(gmail, svc)
    assert (run.created, run.duplicates) == (0, 1)
    assert gmail.labels("t1") == {DONE}
    assert len(g_tasks(svc)) == 1


def test_failure_leaves_label_then_gives_up_after_three(fixture_text):
    gmail = FakeGmailClient()
    gmail.add("t1", INTAKE, body_text="Mohon kirim laporan.")
    gmail.add("t2", INTAKE, body_text=fixture_text("printer.txt"))
    svc = services()
    svc.drive.fail_upload = "original.txt"

    for attempt in (1, 2):
        run = process_inbox(gmail, svc)
        assert run.failed == 2 and run.gave_up == 0
        assert gmail.labels("t1") == {INTAKE}
        assert svc.store.failures()["msg-t1"].count == attempt
    run = process_inbox(gmail, svc)
    assert run.gave_up == 2
    assert gmail.labels("t1") == {FAILED_LABEL}
    assert g_tasks(svc) == []
    assert svc.store.failures()["msg-t1"].last_error == "OSError"


def test_one_bad_thread_does_not_stop_the_others(fixture_text):
    gmail = FakeGmailClient()
    gmail.add("t1", INTAKE, body_text="x")
    gmail.add("t2", INTAKE, body_text=fixture_text("printer.txt"))
    gmail.fail_reading.add("t1")
    svc = services()
    run = process_inbox(gmail, svc)
    assert (run.failed, run.created) == (1, 1)
    assert svc.store.failures()["t1"].count == 1  # keyed by thread when unreadable


def test_auth_failure_stops_and_keeps_labels(fixture_text):
    gmail = FakeGmailClient()
    gmail.add("t1", INTAKE, body_text="x")
    gmail.auth_broken = True
    with pytest.raises(GmailAuthError):
        process_inbox(gmail, services())
    gmail.auth_broken = False
    assert gmail.labels("t1") == {INTAKE}


def test_schema_error_stops_the_run(monkeypatch):
    gmail = FakeGmailClient()
    gmail.add("t1", INTAKE, body_text="x")
    svc = services()

    def broken(*args, **kwargs):
        raise SchemaError(["Master is missing column(s): Source ID"])

    monkeypatch.setattr("app.gmail.process_request", broken)
    with pytest.raises(SchemaError):
        process_inbox(gmail, svc)
    assert gmail.labels("t1") == {INTAKE}


def test_limit_of_20_threads():
    gmail = FakeGmailClient()
    for i in range(25):
        gmail.add(f"t{i}", INTAKE, body_text="Tolong cek printer")
    run = process_inbox(gmail, services())
    assert run.threads == 20


# --- GoogleGmailClient (API client mocked) -----------------------------------------------------


def google_client():
    from unittest.mock import MagicMock

    from app.gmail import GoogleGmailClient

    client = GoogleGmailClient(credentials=object())
    users = MagicMock()
    client._local.service = MagicMock()
    client._local.service.users.return_value = users
    users.labels.return_value.list.return_value.execute.return_value = {
        "labels": [{"name": INTAKE, "id": "L1"}]
    }
    users.labels.return_value.create.return_value.execute.return_value = {"id": "L2"}
    return client, users


def test_newest_message_is_the_request():
    client, users = google_client()
    users.threads.return_value.get.return_value.execute.return_value = {
        "messages": [{"id": "old", "internalDate": "1"}, {"id": "new", "internalDate": "9"}]
    }
    users.messages.return_value.get.return_value.execute.return_value = gmail_json()
    attachments = users.messages.return_value.attachments.return_value
    attachments.get.return_value.execute.return_value = {"data": b64(b"pdf")}
    message = client.newest_message("t1")
    assert message.attachments[0].data == b"pdf"
    assert users.messages.return_value.get.call_args.kwargs["id"] == "new"


def test_move_label_creates_done_label_once():
    client, users = google_client()
    client.move_label("t1", remove=INTAKE, add=DONE)
    client.move_label("t2", remove=INTAKE, add=DONE)
    users.labels.return_value.create.assert_called_once()
    body = users.threads.return_value.modify.call_args.kwargs["body"]
    assert body == {"removeLabelIds": ["L1"], "addLabelIds": ["L2"]}


def test_missing_intake_label_means_no_threads():
    client, users = google_client()
    assert client.labelled_threads("Other/label", 20) == []
    users.threads.return_value.list.assert_not_called()


def test_refresh_error_becomes_gmail_auth_error():
    from google.auth.exceptions import RefreshError

    client, users = google_client()
    users.labels.return_value.list.return_value.execute.side_effect = RefreshError("invalid_grant")
    with pytest.raises(GmailAuthError):
        client.labelled_threads(INTAKE, 20)
