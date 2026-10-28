"""Timed jobs: change detection, recurring reports, digests (DESIGN sections 7 and 8)."""

from __future__ import annotations

import calendar
import logging
import re
from datetime import date, datetime, timedelta

from pydantic import BaseModel

from app import clock
from app.gmail import GmailAuthError, GmailClient
from app.models import OPEN_STATUSES, AuditEntry, CatalogEntry, RawRequest, StateRow, Task
from app.notify import (
    MissingEmail,
    assignment_email,
    manager_digest,
    personal_digest,
    task_line,
)
from app.pipeline import SERVICE_USER, Services, process_request
from app.route import find_person, load_ratio
from app.sheets import SchemaError, SheetStore

logger = logging.getLogger("grc.jobs")

NO_EMAIL_FLAG = "Assignment email not sent: doer has no email in Staff"


# --- /jobs/changes -----------------------------------------------------------------------------


class ChangesRun(BaseModel):  # counts only (no names or emails in logs)
    tasks: int = 0
    baseline: bool = False
    emailed: int = 0
    not_sent: int = 0


def _state_row(task: Task, notified_doer: str, notified_at: datetime | None) -> StateRow:
    return StateRow(
        id=task.id,
        status=task.status,
        owner=task.owner,
        doer=task.doer,
        notified_doer=notified_doer,
        notified_at=notified_at,
    )


def run_changes(store: SheetStore, gmail: GmailClient, sheet_url: str) -> ChangesRun:
    """Email the doer of every open task whose current doer has not been emailed yet.

    That covers both triggers in section 7: approval (Proposed → open, nobody notified) and
    handoff (Doer changed on an open task). A send that fails stays pending and is retried on the
    next run. The first run (empty `_state`) only records the current state (v1.1).
    """
    tasks = [task for task in store.tasks() if task.id]
    state = store.state()
    run = ChangesRun(tasks=len(tasks), baseline=not state)
    if not state:
        rows = [
            _state_row(task, task.doer if task.status in OPEN_STATUSES else "", None)
            for task in tasks
        ]
        store.write_state(rows)
        logger.info("changes_run", extra=run.model_dump())
        return run

    staff = store.staff()
    sender_name = store.config().sender_name
    rows: dict[str, StateRow] = {}
    for task in tasks:
        previous = state.get(task.id)
        notified_doer = previous.notified_doer if previous else ""
        notified_at = previous.notified_at if previous else None
        rows[task.id] = _state_row(task, notified_doer, notified_at)
        doer = task.doer.strip()
        if task.status not in OPEN_STATUSES or not doer or doer == notified_doer:
            continue
        try:
            email = assignment_email(task, staff, sheet_url)
        except MissingEmail:
            run.not_sent += 1
            if NO_EMAIL_FLAG not in task.flags:
                store.update_task(task.id, {"Flag": "; ".join([*task.flags, NO_EMAIL_FLAG])})
            logger.warning("assignment_not_sent", extra={"task_id": task.id})
            continue
        try:
            gmail.send(email, sender_name)
        except GmailAuthError:
            raise
        except Exception as error:
            run.not_sent += 1  # stays pending: retried on the next run
            logger.error(
                "assignment_send_failed",
                extra={"task_id": task.id, "error_type": type(error).__name__},
            )
            continue
        sent_at = clock.now()
        rows[task.id] = _state_row(task, doer, sent_at)
        run.emailed += 1
        # Record each send at once, so a crash later in the run cannot cause a second email.
        store.write_state(list(rows.values()) + [s for i, s in state.items() if i not in rows])
        _audit(store, task.id, "Assignment emailed", "Doer", doer)
    store.write_state(list(rows.values()))
    logger.info("changes_run", extra=run.model_dump())
    return run


def _audit(store: SheetStore, task_id: str, action: str, field: str, value: str) -> None:
    try:
        store.append_audit(
            [
                AuditEntry(
                    timestamp=clock.now(),
                    user=SERVICE_USER,
                    task_id=task_id,
                    action=action,
                    field=field,
                    new_value=value,
                )
            ]
        )
    except Exception as error:
        logger.error(
            "audit_write_failed", extra={"task_id": task_id, "error_type": type(error).__name__}
        )


# --- /jobs/recurring ---------------------------------------------------------------------------

_MONTHS = {name: i for i, name in enumerate(calendar.month_abbr) if name}
_DEFAULT_MONTHS = {"Quarterly": [1, 4, 7, 10], "Semester": [1, 7]}


def due_months(entry: CatalogEntry) -> list[int]:
    """Months a recurring report falls due (section 7); empty when it cannot be worked out."""
    listed = [
        _MONTHS[word[:3].title()]
        for word in re.findall(r"[A-Za-z]+", entry.due_months_seen)
        if word[:3].title() in _MONTHS
    ]
    recurrence = entry.recurrence.strip()
    if recurrence == "Monthly":
        return list(range(1, 13))
    if recurrence in _DEFAULT_MONTHS:
        return sorted(set(listed)) or _DEFAULT_MONTHS[recurrence]
    if recurrence == "Annual":
        return listed[:1]
    return []


def due_dates_within(entry: CatalogEntry, today: date, lead_days: int) -> list[date]:
    """Due dates in [today, today + lead] for a recurring report; the day is capped at month end."""
    try:
        day = int(float(entry.typical_due_day))
    except ValueError:
        return []
    months = due_months(entry)
    if day < 1 or not months:
        return []
    end = today + timedelta(days=lead_days)
    found = []
    year, month = today.year, today.month
    while date(year, month, 1) <= end:
        if month in months:
            due = date(year, month, min(day, calendar.monthrange(year, month)[1]))
            if today <= due <= end:
                found.append(due)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return found


class RecurringRun(BaseModel):
    candidates: int = 0
    created: int = 0
    existing: int = 0
    failed: int = 0


def recurring_request(entry: CatalogEntry, due: date) -> RawRequest:
    return RawRequest(
        source="Recurring",
        source_id=f"Recurring {entry.catalog_id} {due:%Y-%m}",
        received_at=clock.now(),
        subject=entry.report_type,
        body_text=(
            f"{entry.report_type} – {due:%b %Y}\n\n"
            f"Recurring report ({entry.recurrence}) for {entry.regulator or 'n/a'}, "
            f"due {due:%d %B %Y}."
        ),
        catalog_id=entry.catalog_id,
    )


def run_recurring(services: Services, today: date | None = None) -> RecurringRun:
    """Create confirmed Catalog reports due within the lead time, once per period."""
    today = today or clock.today()
    config = services.store.config()
    run = RecurringRun()
    for entry in services.store.catalog():
        if entry.is_adhoc or not entry.confirmed:
            continue
        for due in due_dates_within(entry, today, config.recurring_lead_days):
            run.candidates += 1
            try:
                result = process_request(recurring_request(entry, due), services, today=today)
            except SchemaError:
                raise
            except Exception as error:
                run.failed += 1
                logger.error("recurring_failed", extra={"error_type": type(error).__name__})
                continue
            if result.created:
                run.created += 1
            else:
                run.existing += 1
    logger.info("recurring_run", extra=run.model_dump())
    return run


# --- /jobs/digest ------------------------------------------------------------------------------


class DigestRun(BaseModel):
    personal_sent: int = 0
    manager_sent: bool = False
    failed: int = 0


def _open(tasks: list[Task]) -> list[Task]:
    return [task for task in tasks if task.status in OPEN_STATUSES]


def run_digest(
    store: SheetStore, gmail: GmailClient, sheet_url: str, now: datetime | None = None
) -> DigestRun:
    now = now or clock.now()
    today = now.date()
    config = store.config()
    tasks = [task for task in store.tasks() if task.id]
    staff = store.staff()
    open_tasks = _open(tasks)  # Proposed is not open, so it never appears in personal digests
    label = f"{today:%a %d %b %Y}"
    stale_before = now - timedelta(days=config.stale_after_days)
    run = DigestRun()

    def send(email, kind: str) -> bool:
        try:
            gmail.send(email, config.sender_name)
            return True
        except GmailAuthError:
            raise
        except Exception as error:
            run.failed += 1
            logger.error("digest_failed", extra={"kind": kind, "error_type": type(error).__name__})
            return False

    for person in staff:
        if not person.email.strip():
            continue
        mine = [t for t in open_tasks if t.doer.strip().casefold() == person.name.casefold()]
        overdue = [t for t in mine if t.due_date and t.due_date < today]
        due_soon = [t for t in mine if t.due_date and today <= t.due_date <= today + timedelta(7)]
        stale = [
            t
            for t in mine
            if t.status == "In Progress" and t.last_updated and t.last_updated < stale_before
        ]
        email = personal_digest(person, overdue, due_soon, stale, sheet_url, label)
        if email is not None and send(email, "personal"):
            run.personal_sent += 1

    if not config.manager_email.strip():
        logger.warning("manager_digest_skipped_no_email")
        return run
    leaving_by = today + timedelta(days=config.leaving_warning_days)
    catalog = store.catalog()
    leaving = []
    for person in staff:
        end = person.internship_end
        if person.role != "Intern" or end is None or not today <= end <= leaving_by:
            continue
        owned = [e.catalog_id for e in catalog if find_person(e.default_owner, [person])]
        if owned:
            leaving.append(
                f"{person.name} leaves {end:%d %b %Y}; default owner of {', '.join(owned)}"
            )
    since = today - timedelta(days=1)
    ai_down = sum(
        1 for t in tasks if "AI unavailable" in t.flags and t.start is not None and t.start >= since
    )
    sections = [
        ("Awaiting approval", [task_line(t) for t in tasks if t.status == "Proposed"]),
        ("Overdue", [task_line(t) for t in open_tasks if t.due_date and t.due_date < today]),
        (
            "Over the load threshold",
            [
                f"{p.name}: load {load_ratio(p):g} (threshold {config.overload_threshold:g})"
                for p in staff
                if p.role != "Manager" and load_ratio(p) > config.overload_threshold
            ],
        ),
        ("Interns leaving who still own Catalog reports", leaving),
        ("AI unavailable (last 24 h)", [f"{ai_down} request(s)"] if ai_down else []),
    ]
    email = manager_digest(config.manager_email.strip(), sections, sheet_url, label)
    if email is not None:
        run.manager_sent = send(email, "manager")
    logger.info("digest_run", extra=run.model_dump())
    return run
