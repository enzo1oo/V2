"""Routing rules (DESIGN section 5). Pure: no network, no clock; `today` is an argument.

The AI never chooses the assignee: `ai_catalog_id` is only a hint used by rule 4, and only after
rules 1-3 found nobody.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.models import AppConfig, CatalogEntry, Contact, LetterCode, RouteResult, StaffMember

NEEDS_REVIEW_FLAG = "Needs review: no owner"


@dataclass(frozen=True)
class CatalogMatch:
    entry: CatalogEntry
    score: int
    matched: tuple[str, ...]


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_]", " ", text.casefold())).strip()


def _contains_phrase(normalised_text: str, phrase: str) -> bool:
    phrase = _normalise(phrase)
    if not phrase:
        return False
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", normalised_text) is not None


def score_catalog(
    text: str, contacts: list[Contact], catalog: list[CatalogEntry]
) -> CatalogMatch | None:
    """Rule 1 scoring: keyword length per keyword found, +100 per known contact found.

    Each keyword and contact counts once. Ties go to the manager-confirmed row, then Catalog ID.
    """
    normalised = _normalise(text)
    emails = {c.email.casefold() for c in contacts if c.email}
    names = {_normalise(c.name) for c in contacts if c.name}
    best: CatalogMatch | None = None
    for entry in catalog:
        if entry.is_adhoc:
            continue
        score = 0
        matched: list[str] = []
        for keyword in dict.fromkeys(entry.match_keywords):
            if _contains_phrase(normalised, keyword):
                score += len(_normalise(keyword))
                matched.append(keyword)
        for known in dict.fromkeys(entry.known_contacts):
            if "@" in known:
                found = known.casefold() in emails or _contains_phrase(normalised, known)
            else:
                found = _normalise(known) in names or _contains_phrase(normalised, known)
            if found:
                score += 100
                matched.append(known)
        if score <= 0:
            continue
        candidate = CatalogMatch(entry, score, tuple(matched))
        if best is None or _rank(candidate) < _rank(best):
            best = candidate
    return best


def _rank(match: CatalogMatch) -> tuple[int, bool, str]:
    return (-match.score, not match.entry.confirmed, match.entry.catalog_id)


# --- People ------------------------------------------------------------------------------------


def find_person(name: str, staff: list[StaffMember]) -> StaffMember | None:
    wanted = name.strip().casefold()
    if not wanted:
        return None
    return next((person for person in staff if person.name.strip().casefold() == wanted), None)


def unavailable_reason(person: StaffMember, today: date, due: date | None) -> str | None:
    """Why `person` cannot be proposed, or None if available."""
    if person.role == "Manager":
        return "is a manager"
    end = person.internship_end
    if end is not None:
        if end < today:
            return f"internship ended {end:%d %b %Y}"
        if due is not None and end < due:
            return f"internship ends {end:%d %b %Y}, before the due date"
    return None


def load_ratio(person: StaffMember) -> float:
    return person.weighted_open_load / person.capacity


def _owner_problem(
    name: str, staff: list[StaffMember], today: date, due: date | None, threshold: float | None
) -> tuple[StaffMember | None, str | None]:
    """(person, None) if `name` can take the task, else (None, reason)."""
    if not name.strip():
        return None, "not set"
    person = find_person(name, staff)
    if person is None:
        return None, f"{name} is not in Staff"
    reason = unavailable_reason(person, today, due)
    if reason:
        return None, f"{person.name} {reason}"
    if threshold is not None and load_ratio(person) > threshold:
        return None, f"{person.name} is overloaded (load {load_ratio(person):g} > {threshold:g})"
    return person, None


def _catalog_rules(
    match: CatalogMatch,
    staff: list[StaffMember],
    today: date,
    due: date | None,
    config: AppConfig,
    prefix: str,
) -> tuple[RouteResult | None, list[str]]:
    """Rules 1 and 2 for one catalog row. Returns a result or the reasons it failed."""
    entry = match.entry
    label = f'catalog "{entry.report_type}"'
    if match.matched:
        label += f" ({', '.join(match.matched)})"
    owner, owner_problem = _owner_problem(
        entry.default_owner, staff, today, due, config.overload_threshold
    )
    if owner is not None:
        note = f"{prefix.format(rule=1)}: {label} → default owner"
        return RouteResult(
            owner=owner.name, division=entry.division or None,
            catalog_id=entry.catalog_id, rule=1, note=note,
        ), []  # fmt: skip
    reasons = [f"default owner {owner_problem}"]
    backup, backup_problem = _owner_problem(entry.backup, staff, today, due, threshold=None)
    if backup is not None:
        note = f"{prefix.format(rule=2)}: {label} → backup; default owner {owner_problem}"
        return RouteResult(
            owner=backup.name, division=entry.division or None,
            catalog_id=entry.catalog_id, rule=2, note=note,
        ), []  # fmt: skip
    reasons.append(f"backup {backup_problem}")
    return None, reasons


def route(
    *,
    text: str,
    contacts: list[Contact],
    letter_code: str | None,
    due: date | None,
    catalog: list[CatalogEntry],
    staff: list[StaffMember],
    letter_codes: list[LetterCode],
    config: AppConfig,
    today: date,
    ai_catalog_id: str | None = None,
    known_catalog_id: str | None = None,
) -> RouteResult:
    """Propose an owner by rules 1-5 (DESIGN section 5).

    `known_catalog_id` (recurring reports only) skips rule 1 scoring and uses that row.
    """
    flags: list[str] = []
    reasons: list[str] = []
    division: str | None = None
    division_source = ""

    code_division = None
    if letter_code:
        code_division = next(
            (
                code.route_to_division
                for code in letter_codes
                if code.letter_code.strip().upper() == letter_code.strip().upper()
            ),
            None,
        )
        if code_division is None:
            flags.append(f"{config.unknown_letter_code}: unknown letter code {letter_code}")

    # Rules 1 and 2: a known report.
    known = next(
        (e for e in catalog if known_catalog_id and e.catalog_id == known_catalog_id), None
    )
    if known is not None and not known.is_adhoc:
        # A recurring report already knows its Catalog row: rule 1 uses it directly.
        match: CatalogMatch | None = CatalogMatch(known, 0, ("recurring",))
    else:
        match = score_catalog(text, contacts, catalog)
    matched_id = None
    if match is not None:
        result, failed = _catalog_rules(match, staff, today, due, config, "Rule {rule}")
        if result is not None:
            result.flags = flags
            return result
        reasons.append(f'catalog "{match.entry.report_type}": ' + "; ".join(failed))
        matched_id = match.entry.catalog_id
        if match.entry.division:
            division = match.entry.division
            division_source = f'catalog "{match.entry.report_type}"'

    if division is None and code_division:
        division = code_division
        division_source = f"letter code {letter_code}"

    # Rule 3: least-loaded available person in the division.
    if division:
        candidates = [
            person
            for person in staff
            if person.main_division.strip().casefold() == division.strip().casefold()
            and unavailable_reason(person, today, due) is None
        ]
        if candidates:
            person = min(candidates, key=lambda p: (load_ratio(p), p.name.casefold()))
            note = (
                f"Rule 3: division {division} (from {division_source}) → lowest load "
                f"({load_ratio(person):g})"
            )
            if reasons:
                note += "; " + "; ".join(reasons)
            return RouteResult(
                owner=person.name, division=division, catalog_id=matched_id,
                rule=3, note=note, flags=flags,
            )  # fmt: skip
        reasons.append(f"nobody available in division {division}")

    # Rule 4: the AI's catalog suggestion, through rules 1-2 only.
    ai_entry = next(
        (
            entry
            for entry in catalog
            if ai_catalog_id and entry.catalog_id == ai_catalog_id and not entry.is_adhoc
        ),
        None,
    )
    if ai_entry is not None:
        ai_match = CatalogMatch(ai_entry, 0, ())
        result, failed = _catalog_rules(
            ai_match, staff, today, due, config, "Rule 4 (AI suggested)"
        )
        if result is not None:
            result.rule = 4
            result.flags = flags
            return result
        reasons.append(f'AI suggestion "{ai_entry.report_type}": ' + "; ".join(failed))

    # Rule 5: nobody.
    note = "Rule 5: no owner found"
    if reasons:
        note += "; " + "; ".join(reasons)
    return RouteResult(
        owner=None, division=division, catalog_id=matched_id,
        rule=5, note=note, flags=[*flags, NEEDS_REVIEW_FLAG],
    )  # fmt: skip
