"""DESIGN section 12 cases that only need rules (Phase 1). Routing/pipeline cases follow later."""

from datetime import date

from app.extract import rules
from app.extract.clean import clean_text, strip_noise, unwrap_forward
from tests.conftest import TODAY

MANAGER = ("GRC Manager", "grc-manager@example.com")
DOMAINS = {"example.co.id": "BRI"}


def _extract(text: str):
    unwrapped = unwrap_forward(text, *MANAGER)
    light = strip_noise(unwrapped.body)
    return unwrapped, light, clean_text(unwrapped.body)


def test_manpower_planning(fixture_text):
    original = fixture_text("manpower_planning.txt")
    unwrapped, light, cleaned = _extract(original)

    # requester org is BRI, from the forwarded sender; manager note goes to Notes
    assert rules.requester_org(unwrapped.sender_email, light, DOMAINS, []) == "BRI"
    assert rules.requester_org("", light, {}, ["BRI"]) == "BRI"
    assert unwrapped.manager_note == "Tolong dicek ya"

    # title is not the greeting
    title = rules.title_rule(cleaned)
    assert not title.lower().startswith("selamat")
    assert title.startswith("Dalam rangka")

    # due date 2026-11-06 with a weekday mismatch flag
    due = rules.due_date(light, TODAY)
    assert due.value == date(2026, 11, 6)
    assert any("Weekday mismatch" in flag for flag in due.flags)

    # the known contact used by routing rule 1 is extracted
    names = [contact.name for contact in rules.contacts(light)]
    assert "Hana Setyaningsih" in names

    # the original is untouched by extraction
    assert "**" in original and "0812-0000-0000" in original


def test_formal_letter_code(fixture_text):
    _, light, cleaned = _extract(fixture_text("formal_letter_emr_irm.txt"))
    ref = rules.letter_ref(light)
    assert ref is not None and ref.letter_code == "EMR/IRM"
    assert rules.due_date(light, TODAY).value == date(2026, 11, 13)
    assert rules.title_rule(cleaned) == (
        "Permintaan Data Eksposur Risiko Kantor Cabang Luar Negeri"
    )


def test_english_due(fixture_text):
    _, light, cleaned = _extract(fixture_text("english_due.txt"))
    due = rules.due_date(light, TODAY)
    assert due.value == date(2026, 11, 15)
    assert due.flags == []
    assert rules.title_rule(cleaned).startswith("Please send us the updated outsourcing register")


def test_no_date(fixture_text):
    _, light, cleaned = _extract(fixture_text("no_date.txt"))
    assert rules.due_date(light, TODAY).value is None
    assert rules.title_rule(cleaned).startswith("Mohon bantuannya")


def test_nested_forward(fixture_text):
    unwrapped, light, _ = _extract(fixture_text("nested_forward.txt"))
    assert unwrapped.sender_email == "rina.kusuma@example.go.id"
    assert rules.due_date(light, TODAY).value == date(2026, 11, 20)


def test_printer(fixture_text):
    _, light, cleaned = _extract(fixture_text("printer.txt"))
    assert rules.due_date(light, TODAY).value is None
    assert rules.letter_ref(light) is None
    assert rules.contacts(light) == []
    assert (
        rules.title_rule(cleaned) == "Tolong cek printer di lantai 3, tidak bisa print dari kemarin"
    )


def test_correct_weekday(fixture_text):
    _, light, _ = _extract(fixture_text("correct_weekday.txt"))
    due = rules.due_date(light, TODAY)
    assert due.value == date(2026, 11, 6)
    assert due.flags == []
