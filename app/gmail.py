"""Gmail intake: label poll, message parsing, label changes and sending (DESIGN D3, D4, D15).

One intake mailbox, reached with an OAuth refresh token from Secret Manager. Labels change only
after the row is written, so a crash at any point leaves the email labelled for the next run.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app import clock
from app.models import Attachment, FailureRow, RawRequest
from app.pipeline import Services, process_request
from app.sheets import SchemaError

logger = logging.getLogger("grc.gmail")

SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
)
FAILED_LABEL = "GRC/Failed"
MAX_FAILURES = 3
THREADS_PER_RUN = 20


class GmailAuthError(Exception):
    """The refresh token was rejected; the Gmail job stops and labels stay for the retry."""


class GmailMessage(BaseModel):
    id: str
    thread_id: str
    subject: str = ""
    sender_name: str = ""
    sender_email: str = ""
    received_at: datetime
    body_text: str = ""
    body_html: str = ""
    attachments: list[Attachment] = Field(default_factory=list)


class OutgoingEmail(BaseModel):
    to: list[str]
    cc: list[str] = Field(default_factory=list)
    subject: str
    text: str
    html: str


class GmailClient(Protocol):
    def labelled_threads(self, label: str, limit: int) -> list[str]: ...

    def newest_message(self, thread_id: str) -> GmailMessage: ...

    def move_label(self, thread_id: str, remove: str, add: str) -> None:
        """Remove label `remove` from the thread and add `add` (created if missing)."""
        ...

    def send(self, email: OutgoingEmail, sender_name: str) -> None: ...


# --- Parsing (pure) ----------------------------------------------------------------------------


def _decode_header_value(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _charset(part: dict[str, Any]) -> str:
    for header in part.get("headers", []):
        if header.get("name", "").lower() == "content-type":
            for param in header.get("value", "").split(";")[1:]:
                key, _, value = param.strip().partition("=")
                if key.lower() == "charset" and value:
                    return value.strip('"')
    return "utf-8"


def _text(part: dict[str, Any]) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    raw = _b64(data)
    try:
        return raw.decode(_charset(part), errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def parse_message(
    message: dict[str, Any], fetch_attachment: Callable[[str, str], bytes]
) -> GmailMessage:
    """Gmail API `messages.get(format=full)` JSON -> GmailMessage.

    The first text/plain and text/html parts are the body; every part with a filename is an
    attachment (inline images included). `fetch_attachment(message_id, attachment_id)` loads
    attachment bytes that are not inline in the JSON.
    """
    payload = message.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    name, address = parseaddr(_decode_header_value(headers.get("from", "")))
    text = html = ""
    attachments: list[Attachment] = []

    def walk(part: dict[str, Any]) -> None:
        nonlocal text, html
        mime = part.get("mimeType", "")
        filename = part.get("filename") or ""
        body = part.get("body", {})
        if filename:
            if body.get("attachmentId"):
                data = fetch_attachment(message["id"], body["attachmentId"])
            else:
                data = _b64(body.get("data", ""))
            attachments.append(Attachment(filename=filename, mime=mime, data=data))
        elif mime == "text/plain" and not text:
            text = _text(part)
        elif mime == "text/html" and not html:
            html = _text(part)
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    received = datetime.fromtimestamp(int(message.get("internalDate", "0")) / 1000, tz=clock.TZ)
    return GmailMessage(
        id=message["id"],
        thread_id=message.get("threadId", ""),
        subject=_decode_header_value(headers.get("subject", "")),
        sender_name=name,
        sender_email=address.lower(),
        received_at=received,
        body_text=text,
        body_html=html,
        attachments=attachments,
    )


def build_mime(email: OutgoingEmail, sender: str, sender_name: str) -> str:
    """Base64url `raw` for messages.send: plain text plus an HTML alternative."""
    message = EmailMessage()
    message["From"] = formataddr((sender_name, sender))
    message["To"] = ", ".join(email.to)
    if email.cc:
        message["Cc"] = ", ".join(email.cc)
    message["Subject"] = email.subject
    message.set_content(email.text)
    message.add_alternative(email.html, subtype="html")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


# --- Real client -------------------------------------------------------------------------------


class GoogleGmailClient:
    """Gmail API v1 as the intake mailbox ("me"), one client per thread."""

    def __init__(self, credentials: Any):
        self._credentials = credentials
        self._local = threading.local()
        self._label_ids: dict[str, str] = {}
        self._address: str | None = None

    @classmethod
    def from_secrets(cls, token_secret: str, client_secret: str) -> GoogleGmailClient:
        from google.oauth2.credentials import Credentials

        from app.settings import read_secret

        client = json.loads(read_secret(client_secret))
        client = client.get("installed") or client.get("web") or client
        credentials = Credentials(
            token=None,
            refresh_token=read_secret(token_secret).strip(),
            client_id=client["client_id"],
            client_secret=client["client_secret"],
            token_uri=client.get("token_uri", "https://oauth2.googleapis.com/token"),
            scopes=list(SCOPES),
        )
        return cls(credentials)

    def _users(self) -> Any:
        if not hasattr(self._local, "service"):
            from googleapiclient.discovery import build

            self._local.service = build(
                "gmail", "v1", credentials=self._credentials, cache_discovery=False
            )
        return self._local.service.users()

    def _execute(self, request: Any) -> Any:
        from google.auth.exceptions import RefreshError

        try:
            return request.execute()
        except RefreshError as error:
            raise GmailAuthError(type(error).__name__) from None

    def _label_id(self, name: str, create: bool) -> str | None:
        if name not in self._label_ids:
            labels = self._execute(self._users().labels().list(userId="me")).get("labels", [])
            self._label_ids.update({label["name"]: label["id"] for label in labels})
        if name not in self._label_ids and create:
            created = self._execute(self._users().labels().create(userId="me", body={"name": name}))
            self._label_ids[name] = created["id"]
        return self._label_ids.get(name)

    def labelled_threads(self, label: str, limit: int) -> list[str]:
        label_id = self._label_id(label, create=False)
        if label_id is None:
            return []
        response = self._execute(
            self._users().threads().list(userId="me", labelIds=[label_id], maxResults=limit)
        )
        return [thread["id"] for thread in response.get("threads", [])]

    def newest_message(self, thread_id: str) -> GmailMessage:
        thread = self._execute(
            self._users().threads().get(userId="me", id=thread_id, format="minimal")
        )
        newest = max(thread["messages"], key=lambda m: int(m.get("internalDate", "0")))
        full = self._execute(
            self._users().messages().get(userId="me", id=newest["id"], format="full")
        )
        return parse_message(full, self._attachment)

    def _attachment(self, message_id: str, attachment_id: str) -> bytes:
        response = self._execute(
            self._users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
        )
        return _b64(response["data"])

    def move_label(self, thread_id: str, remove: str, add: str) -> None:
        body = {
            "removeLabelIds": [i for i in [self._label_id(remove, create=False)] if i],
            "addLabelIds": [self._label_id(add, create=True)],
        }
        self._execute(self._users().threads().modify(userId="me", id=thread_id, body=body))

    def send(self, email: OutgoingEmail, sender_name: str) -> None:
        if self._address is None:
            self._address = self._execute(self._users().getProfile(userId="me"))["emailAddress"]
        raw = build_mime(email, self._address, sender_name)
        self._execute(self._users().messages().send(userId="me", body={"raw": raw}))


# --- The /jobs/gmail run -----------------------------------------------------------------------


class InboxRun(BaseModel):
    threads: int = 0
    created: int = 0
    duplicates: int = 0
    failed: int = 0
    gave_up: int = 0


def _record_failure(services: Services, key: str, error: Exception) -> int:
    """Count a failure in `_failures`; returns the new count (best effort)."""
    previous = services.store.failures().get(key)
    count = (previous.count if previous else 0) + 1
    try:
        services.store.upsert_failure(
            FailureRow(key=key, count=count, last_error=type(error).__name__, last_at=clock.now())
        )
    except Exception as write_error:
        logger.error("failure_count_not_saved", extra={"error_type": type(write_error).__name__})
    return count


def _move(gmail: GmailClient, thread_id: str, remove: str, add: str) -> None:
    try:
        gmail.move_label(thread_id, remove=remove, add=add)
    except GmailAuthError:
        raise
    except Exception as error:
        logger.error("label_change_failed", extra={"error_type": type(error).__name__})


def process_inbox(gmail: GmailClient, services: Services, limit: int = THREADS_PER_RUN) -> InboxRun:
    """Log up to `limit` labelled threads. Stops on schema or Gmail auth errors (labels stay)."""
    config = services.store.config()
    run = InboxRun()
    for thread_id in gmail.labelled_threads(config.gmail_intake_label, limit):
        run.threads += 1
        key = thread_id
        try:
            message = gmail.newest_message(thread_id)
            key = message.id
            raw = RawRequest(
                source="Email",
                source_id=message.id,
                received_at=message.received_at,
                subject=message.subject,
                body_text=message.body_text,
                body_html=message.body_html,
                sender_name=message.sender_name,
                sender_email=message.sender_email,
                attachments=message.attachments,
                submitted_by=message.sender_email,
            )
            result = process_request(raw, services)
        except (GmailAuthError, SchemaError):
            raise
        except Exception as error:
            run.failed += 1
            count = _record_failure(services, key, error)
            logger.warning(
                "request_failed",
                extra={"error_type": type(error).__name__, "failures": count},
            )
            if count >= MAX_FAILURES:
                # Take it out of the intake label so it stops retrying; the alert goes out (s.11).
                _move(gmail, thread_id, config.gmail_intake_label, FAILED_LABEL)
                run.gave_up += 1
                logger.error("request_gave_up", extra={"failures": count})
            continue
        # Only now, with the row written, is the email marked as logged (D15). If this fails the
        # next run finds the row by Source ID and tries the label again.
        _move(gmail, thread_id, config.gmail_intake_label, config.gmail_done_label)
        if result.created:
            run.created += 1
        else:
            run.duplicates += 1
    logger.info("gmail_run", extra=run.model_dump())
    return run
