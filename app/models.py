"""Data models. Sheet-backed models use the exact sheet header text as field aliases."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Status = Literal["Proposed", "Not Started", "In Progress", "On Hold", "Done", "Cancelled"]
OPEN_STATUSES: frozenset[str] = frozenset({"Not Started", "In Progress", "On Hold"})
RequestSource = Literal["Email", "Google Chat", "Recurring", "Manual"]


def _split_list(value: object) -> object:
    """Comma-separated sheet cell -> list of trimmed, non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


def _blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class _SheetRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


# --- Intake -----------------------------------------------------------------------------------


class Attachment(BaseModel):
    filename: str
    mime: str
    data: bytes = Field(repr=False)

    @property
    def size(self) -> int:
        return len(self.data)


class RawRequest(BaseModel):
    source: RequestSource
    source_id: str
    received_at: datetime
    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    sender_name: str = ""
    sender_email: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    submitted_by: str = ""


class Contact(BaseModel):
    name: str | None = None
    email: str | None = None

    def label(self) -> str:
        if self.name and self.email:
            return f"{self.name} <{self.email}>"
        return self.name or self.email or ""


# --- Reference tabs ----------------------------------------------------------------------------


class StaffMember(_SheetRow):
    name: str = Field(alias="Name")
    email: str = Field(default="", alias="Email")
    main_division: str = Field(default="", alias="Main division")
    role: Literal["Staff", "Intern", "Manager"] = Field(default="Staff", alias="Role")
    internship_end: date | None = Field(default=None, alias="Internship end date")
    capacity: float = Field(default=1.0, alias="Capacity (1 = full time)")
    weighted_open_load: float = Field(default=0.0, alias="Weighted open load")

    _blanks = field_validator("internship_end", mode="before")(_blank_to_none)

    @field_validator("capacity", mode="before")
    @classmethod
    def _capacity_default(cls, value: object) -> object:
        # Blank or zero capacity would make the load ratio meaningless; treat it as full time.
        return 1.0 if _blank_to_none(value) in (None, 0, "0") else value

    @field_validator("weighted_open_load", mode="before")
    @classmethod
    def _load_default(cls, value: object) -> object:
        return 0.0 if _blank_to_none(value) is None else value


class CatalogEntry(_SheetRow):
    catalog_id: str = Field(alias="Catalog ID")
    report_type: str = Field(default="", alias="Report type")
    division: str = Field(default="", alias="Division")
    regulator: str = Field(default="", alias="Regulator / Institution")
    recurrence: str = Field(default="", alias="Recurrence")
    default_owner: str = Field(default="", alias="Default owner")
    backup: str = Field(default="", alias="Backup")
    typical_due_day: str = Field(default="", alias="Typical due day")
    due_months_seen: str = Field(default="", alias="Due months seen (2026)")
    match_keywords: list[str] = Field(default_factory=list, alias="Match keywords")
    known_contacts: list[str] = Field(default_factory=list, alias="Known contacts")
    confirmed: bool = Field(default=False, alias="Confirmed by manager?")

    _lists = field_validator("match_keywords", "known_contacts", mode="before")(_split_list)

    @field_validator("typical_due_day", mode="before")
    @classmethod
    def _as_text(cls, value: object) -> object:
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    @field_validator("confirmed", mode="before")
    @classmethod
    def _yes_no(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower() in {"yes", "y", "true"}
        return bool(value)

    @property
    def is_adhoc(self) -> bool:
        return self.catalog_id.upper().startswith("ADHOC-")


class LetterCode(_SheetRow):
    letter_code: str = Field(alias="Letter code")
    route_to_division: str = Field(alias="Route to division")


# --- Master ------------------------------------------------------------------------------------


class Task(_SheetRow):
    """One Master row. `weight` is a formula column and is never written from this model."""

    id: str = Field(default="", alias="ID")
    type: str = Field(default="", alias="Type")
    title: str = Field(default="", alias="Title")
    catalog_id: str = Field(default="", alias="Catalog ID")
    category: str = Field(default="", alias="Category")
    division: str = Field(default="", alias="Division")
    requester: str = Field(default="", alias="Requester / Regulator")
    owner: str = Field(default="", alias="Owner")
    doer: str = Field(default="", alias="Doer")
    support: str = Field(default="", alias="Support")
    whole_team: str = Field(default="", alias="Whole team")
    letter_no: str = Field(default="", alias="Letter no.")
    letter_code: str = Field(default="", alias="Letter code")
    request_reference: str = Field(default="", alias="Request reference")
    start: date | None = Field(default=None, alias="Start / received")
    due_date: date | None = Field(default=None, alias="Due date")
    submitted_date: date | None = Field(default=None, alias="Submitted date")
    priority: str = Field(default="", alias="Priority")
    effort: str = Field(default="", alias="Effort")
    progress: str = Field(default="", alias="Progress")
    status: Status = Field(default="Proposed", alias="Status")
    last_updated: datetime | None = Field(default=None, alias="Last updated")
    link: str = Field(default="", alias="Link")
    send_to: str = Field(default="", alias="Send to")
    notes: str = Field(default="", alias="Notes")
    flag: str = Field(default="", alias="Flag")
    source: str = Field(default="", alias="Source")
    routing_note: str = Field(default="", alias="Routing note")
    original_message: str = Field(default="", alias="Original message")
    files: str = Field(default="", alias="Files")
    ai_summary: str = Field(default="", alias="AI summary")
    deliverables: str = Field(default="", alias="Deliverables")
    contacts: str = Field(default="", alias="Contacts")
    source_id: str = Field(default="", alias="Source ID")

    _dates = field_validator("start", "due_date", "submitted_date", "last_updated", mode="before")(
        _blank_to_none
    )

    @property
    def flags(self) -> list[str]:
        return [part.strip() for part in self.flag.split(";") if part.strip()]


MASTER_REQUIRED_COLUMNS: tuple[str, ...] = (
    *(field.alias for field in Task.model_fields.values() if field.alias),
    "Weight",
)


# --- Results -----------------------------------------------------------------------------------


class RouteResult(BaseModel):
    owner: str | None = None
    division: str | None = None
    catalog_id: str | None = None
    rule: int
    note: str
    flags: list[str] = Field(default_factory=list)


class TaskResult(BaseModel):
    task_id: str
    created: bool
    title: str
    owner: str | None = None
    due_date: date | None = None
    flags: list[str] = Field(default_factory=list)
    files_url: str = ""


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _trim_words(text: str, limit: int) -> str:
    """Cut `text` to at most `limit` characters at a word boundary, ending with an ellipsis."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "…"


class AIResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    title_en: str
    summary_en: str
    deliverables: list[str] = Field(default_factory=list)
    deadline: date | None = None
    deadline_quote: str | None = None
    requester_org: str | None = None
    contacts: list[Contact] = Field(default_factory=list)
    catalog_id: str = "NONE"
    language: Literal["id", "en", "mixed"]

    @field_validator("title_en", mode="after")
    @classmethod
    def _title_limit(cls, value: str) -> str:
        return _trim_words(value, 90)

    @field_validator("summary_en", mode="after")
    @classmethod
    def _two_sentences(cls, value: str) -> str:
        sentences = _SENTENCE_END.split(value.strip())
        return " ".join(sentences[:2])
