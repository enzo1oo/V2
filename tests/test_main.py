import logging

from fastapi.testclient import TestClient

from app.auth import require_chat, require_scheduler
from app.main import Runtime, app, get_runtime
from app.settings import Settings
from tests.fakes import (
    FakeChatClient,
    FakeDriveStore,
    FakeGmailClient,
    FakeSheetsBackend,
    FakeSheetStore,
)


def runtime(gmail=None, backend=None):
    return Runtime(
        settings=Settings(sheet_id="sheet-1", gcp_project="proj"),
        store=FakeSheetStore(backend or FakeSheetsBackend.from_fixtures()),
        drive=FakeDriveStore(),
        gmail=gmail or FakeGmailClient(),
        chat=FakeChatClient(),
    )


def client_with(rt):
    app.dependency_overrides[get_runtime] = lambda: rt
    app.dependency_overrides[require_scheduler] = lambda: None  # token checks: tests/test_auth.py
    app.dependency_overrides[require_chat] = lambda: None
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_health_endpoints_have_no_data():
    client = TestClient(app)
    for path in ("/health", "/healthz"):
        response = client.get(path)
        assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_gmail_job(fixture_text):
    gmail = FakeGmailClient()
    gmail.add("m1", body_text=fixture_text("manpower_planning.txt"))
    gmail.add("m2", body_text="spam", sender_email="someone@elsewhere.example.net")
    response = client_with(runtime(gmail)).post("/jobs/gmail")
    assert response.status_code == 200
    assert response.json() == {
        "job": "gmail", "messages": 2, "created": 1, "duplicates": 0, "ignored": 1, "replies": 0,
        "failed": 0, "gave_up": 0, "ack_failed": 0,
    }  # fmt: skip
    assert len(gmail.replies) == 1


def test_gmail_job_schema_error_is_500():
    backend = FakeSheetsBackend.from_fixtures()
    del backend.tabs["_failures"]
    response = client_with(runtime(backend=backend)).post("/jobs/gmail")
    assert response.status_code == 500
    assert response.json() == {"job": "gmail", "error": "schema_error"}


def test_schema_problems_are_logged(caplog):
    backend = FakeSheetsBackend.from_fixtures()
    del backend.tabs["_failures"]
    with caplog.at_level(logging.ERROR, logger="grc"):
        client_with(runtime(backend=backend)).post("/jobs/gmail")
    reasons = [r.reason for r in caplog.records if r.getMessage() == "schema_problem"]
    assert reasons == ["missing tab _failures"]


def test_gmail_job_auth_error_is_500():
    gmail = FakeGmailClient()
    gmail.add("m1", body_text="x")
    gmail.auth_broken = True
    response = client_with(runtime(gmail)).post("/jobs/gmail")
    assert response.status_code == 500
    assert response.json()["error"] == "gmail_auth_error"


def test_changes_recurring_and_digest_endpoints():
    rt = runtime()
    client = client_with(rt)
    changes = client.post("/jobs/changes").json()
    assert changes["job"] == "changes" and changes["baseline"] is True
    assert client.post("/jobs/recurring").json()["job"] == "recurring"
    digest = client.post("/jobs/digest").json()
    assert digest == {"job": "digest", "personal_sent": 0, "manager_sent": False, "failed": 0}


def test_chat_endpoint(fixture_text):
    import json

    event = json.loads(fixture_text("chat_message.json"))
    response = client_with(runtime()).post("/chat", json=event)
    assert response.status_code == 200
    assert response.json()["text"].startswith("Logged as G-0001")


def test_chat_endpoint_when_sheet_is_broken(fixture_text):
    import json

    backend = FakeSheetsBackend.from_fixtures()
    del backend.tabs["_state"]
    event = json.loads(fixture_text("chat_message.json"))
    response = client_with(runtime(backend=backend)).post("/chat", json=event)
    assert response.status_code == 200
    assert "not available" in response.json()["text"]
