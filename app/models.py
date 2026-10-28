"""Data models. Sheet-backed models use the exact sheet header text as field aliases."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

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
    """A row read from a tab. Blank cells arrive as "" and numbers in text columns become text."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", coerce_numbers_to_str=True)


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

    @field_validator("role", mode="before")
    @classmethod
    def _role_default(cls, value: object) -> object:
        # The Role column is optional in the sheet; a blank role is ordinary staff.
        return "Staff" if _blank_to_none(value) is None else value

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


# --- Service tabs and Config -------------------------------------------------------------------


class StateRow(_SheetRow):
    """One `_state` row: what the service last saw and notified for a task."""

    id: str = Field(alias="ID")
    status: str = Field(default="", alias="Status")
    owner: str = Field(default="", alias="Owner")
    doer: str = Field(default="", alias="Doer")
    notified_doer: str = Field(default="", alias="Notified doer")
    notified_at: datetime | None = Field(default=None, alias="Notified at")

    _dates = field_validator("notified_at", mode="before")(_blank_to_none)


class FailureRow(_SheetRow):
    """One `_failures` row: a failure counter (key = Source ID) or alert throttle (`alert:…`)."""

    key: str = Field(alias="Key")
    count: int = Field(default=0, alias="Count")
    last_error: str = Field(default="", alias="Last error")
    last_at: datetime | None = Field(default=None, alias="Last at")

    _dates = field_validator("last_at", mode="before")(_blank_to_none)

    @field_validator("count", mode="before")
    @classmethod
    def _count_default(cls, value: object) -> object:
        return 0 if _blank_to_none(value) is None else value


class AuditEntry(_SheetRow):
    timestamp: datetime = Field(alias="Timestamp")
    user: str = Field(alias="User")
    task_id: str = Field(default="", alias="Task ID")
    action: str = Field(alias="Action")
    field: str = Field(default="", alias="Field")
    old_value: str = Field(default="", alias="Old value")
    new_value: str = Field(default="", alias="New value")
    reason: str = Field(default="", alias="Reason")


def _yes(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in {"yes", "y", "true"}


class AppConfig(_SheetRow):
    """The Config tab (Setting → Value). New v1 settings have defaults; the rest are required."""

    effort_small: float = Field(alias="Effort: Small")
    effort_medium: float = Field(alias="Effort: Medium")
    effort_large: float = Field(alias="Effort: Large")
    support_share: float = Field(alias="Support share")
    overload_threshold: float = Field(alias="Overload threshold")
    stale_after_days: int = Field(alias="Stale after (days)")
    reminder_time: str = Field(alias="Reminder time")
    leaving_warning_days: int = Field(alias="Leaving warning (days)")
    unknown_letter_code: str = Field(alias="Unknown letter code")
    manager_email: str = Field(alias="Manager email")
    allowed_email_domain: str = Field(alias="Allowed email domain")
    gmail_intake_label: str = Field(alias="Gmail intake label")
    gmail_done_label: str = Field(alias="Gmail done label")
    recurring_lead_days: int = Field(alias="Recurring lead (days)")
    default_due_days: int = Field(alias="Default due (days)")
    ai_provider: Literal["Vertex", "BRI agent", "Off"] = Field(default="Off", alias="AI provider")
    ai_model: str = Field(default="", alias="AI model")
    ai_region: str = Field(default="", alias="AI region")
    sender_name: str = Field(default="GRC Tracker", alias="Sender name")
    mask_emails_for_ai: bool = Field(default=False, alias="Mask emails for AI")
    requester_domains: str = Field(default="", alias="Requester domains")
    known_requesters: list[str] = Field(default_factory=list, alias="Known requesters")

    _lists = field_validator("known_requesters", mode="before")(_split_list)

    @field_validator("ai_provider", "sender_name", mode="before")
    @classmethod
    def _blank_uses_default(cls, value: object, info: ValidationInfo) -> object:
        if _blank_to_none(value) is None and info.field_name:
            return cls.model_fields[info.field_name].default
        return value

    @field_validator("reminder_time", mode="before")
    @classmethod
    def _time_text(cls, value: object) -> object:
        # Sheets returns a time cell as a fraction of a day (09:00 -> 0.375).
        if isinstance(value, int | float) and 0 <= value < 1:
            minutes = round(value * 24 * 60)
            return f"{minutes // 60:02d}:{minutes % 60:02d}"
        return value

    @field_validator("mask_emails_for_ai", mode="before")
    @classmethod
    def _yes_no(cls, value: object) -> object:
        return value if isinstance(value, bool) else _yes(value)

    def effort_weight(self, effort: str) -> float:
        """Weight of an Effort value; blank or unknown counts as Medium (as the sheet formula)."""
        return {"Small": self.effort_small, "Large": self.effort_large}.get(
            effort.strip(), self.effort_medium
        )


REQUIRED_CONFIG_SETTINGS: tuple[str, ...] = tuple(
    field.alias for field in AppConfig.model_fields.values() if field.alias and field.is_required()
)

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "Master": MASTER_REQUIRED_COLUMNS,
    "Staff": tuple(f.alias for f in StaffMember.model_fields.values() if f.alias),
    "Catalog": tuple(f.alias for f in CatalogEntry.model_fields.values() if f.alias),
    "LetterCodes": ("Letter code", "Route to division"),
    "AuditLog": tuple(f.alias for f in AuditEntry.model_fields.values() if f.alias),
    "Config": ("Setting", "Value"),
    "_state": tuple(f.alias for f in StateRow.model_fields.values() if f.alias),
    "_failures": tuple(f.alias for f in FailureRow.model_fields.values() if f.alias),
}
