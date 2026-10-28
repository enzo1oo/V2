import json
import logging
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.extract import ai as ai_module
from app.extract.ai import (
    AI_UNAVAILABLE_FLAG,
    PROMPT_VERSION,
    BriAgentProvider,
    NotConfigured,
    NullProvider,
    VertexGeminiProvider,
    apply_guardrails,
    build_prompt,
    provider_from_config,
    run_ai,
)
from app.models import AIResult, CatalogEntry
from tests.fakes import FakeAIProvider

CATALOG = [
    CatalogEntry.model_validate({"Catalog ID": "HR-DATA", "Report type": "Employee data"}),
    CatalogEntry.model_validate({"Catalog ID": "ADHOC-HR", "Report type": "Ad hoc HR"}),
]
ORIGINAL = "deadline pengumpulan pada** Rabu 06\nNovember 2026.**"


def result(**overrides) -> AIResult:
    payload = {
        "title_en": "Title",
        "summary_en": "Summary.",
        "deliverables": [],
        "deadline": "2026-11-06",
        "deadline_quote": "Rabu 06 November 2026",
        "requester_org": None,
        "contacts": [],
        "catalog_id": "HR-DATA",
        "language": "id",
        **overrides,
    }
    return AIResult.model_validate_json(json.dumps(payload))


# --- guardrails --------------------------------------------------------------------------------


def test_quote_matches_across_line_break_and_emphasis():
    kept = apply_guardrails(result(), ORIGINAL, CATALOG)
    assert kept.deadline == date(2026, 11, 6)


@pytest.mark.parametrize("quote", ["Rabu, 6 Nov 2026", None, "", "  "])
def test_deadline_dropped_when_quote_not_verbatim(quote):
    assert apply_guardrails(result(deadline_quote=quote), ORIGINAL, CATALOG).deadline is None


@pytest.mark.parametrize("catalog_id", ["FAKE-1", "ADHOC-HR", "none"])
def test_unknown_or_adhoc_catalog_becomes_none(catalog_id):
    assert apply_guardrails(result(catalog_id=catalog_id), ORIGINAL, CATALOG).catalog_id == "NONE"


# --- run_ai ------------------------------------------------------------------------------------


def test_null_provider_is_silent():
    outcome = run_ai(NullProvider(), "x", "x", CATALOG, [])
    assert outcome.result is None and outcome.flags == []


def test_success_applies_guardrails():
    outcome = run_ai(FakeAIProvider(result(catalog_id="FAKE")), "x", ORIGINAL, CATALOG, [])
    assert outcome.flags == []
    assert outcome.result.catalog_id == "NONE"


def test_error_retries_once_then_flags():
    provider = FakeAIProvider(error=RuntimeError("boom"))
    outcome = run_ai(provider, "x", "x", CATALOG, [])
    assert outcome.result is None and outcome.flags == [AI_UNAVAILABLE_FLAG]
    assert len(provider.calls) == 2


def test_not_configured_is_not_retried():
    provider = FakeAIProvider(error=NotConfigured("no api"))
    assert run_ai(provider, "x", "x", CATALOG, []).flags == [AI_UNAVAILABLE_FLAG]
    assert len(provider.calls) == 1


def test_timeout():
    provider = FakeAIProvider(result(), delay=0.3)
    outcome = run_ai(provider, "x", "x", CATALOG, [], timeout=0.05)
    assert outcome.flags == [AI_UNAVAILABLE_FLAG]


def test_input_capped_at_8000_chars():
    provider = FakeAIProvider(result())
    run_ai(provider, "y" * 9000, "y", CATALOG, [])
    assert len(provider.calls[0]["text"]) == 8000


def test_logs_metadata_but_never_text(caplog):
    secret = "Rahasia 0812-0000-0000 fajar@example.co.id"
    with caplog.at_level(logging.INFO, logger="grc.ai"):
        run_ai(FakeAIProvider(result()), secret, secret, CATALOG, ["Form.xlsx"])
    [record] = caplog.records
    assert record.getMessage() == "ai_call"
    assert record.provider == "Fake" and record.model == "fake-model"
    assert record.prompt_version == PROMPT_VERSION
    assert record.success is True and isinstance(record.latency_ms, int)
    dumped = json.dumps(record.__dict__, default=str)
    assert "Rahasia" not in dumped and "fajar" not in dumped and "Form.xlsx" not in dumped


# --- providers ---------------------------------------------------------------------------------


def test_bri_agent_stub_raises_not_configured():
    with pytest.raises(NotConfigured):
        BriAgentProvider().extract("x", CATALOG, [])


def test_provider_from_config():
    assert isinstance(provider_from_config("Off", "", "", "p"), NullProvider)
    assert isinstance(provider_from_config("BRI agent", "", "", "p"), BriAgentProvider)
    vertex = provider_from_config("Vertex", "gemini-x", "asia-southeast1", "proj")
    assert isinstance(vertex, VertexGeminiProvider)
    assert (vertex.model, vertex.region, vertex.project) == ("gemini-x", "asia-southeast1", "proj")


def test_prompt_lists_only_non_adhoc_catalog_and_file_names():
    prompt = build_prompt("Isi pesan", CATALOG, ["Form.xlsx"])
    assert "HR-DATA — Employee data" in prompt
    assert "ADHOC-HR" not in prompt
    assert "- Form.xlsx" in prompt
    assert "Isi pesan" in prompt
    assert "{{" not in prompt


def test_vertex_provider_request_and_parsing():
    client = MagicMock()
    payload = result().model_dump(mode="json")
    client.models.generate_content.return_value = SimpleNamespace(text=json.dumps(payload))
    provider = VertexGeminiProvider("proj", "asia-southeast1", "gemini-x", lambda: client)
    parsed = provider.extract("Isi pesan", CATALOG, [])
    assert parsed is not None and parsed.deadline == date(2026, 11, 6)
    kwargs = client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "gemini-x"
    assert kwargs["config"].temperature == 0
    assert kwargs["config"].response_mime_type == "application/json"
    assert kwargs["config"].response_json_schema == ai_module.RESPONSE_SCHEMA
    assert "Isi pesan" in kwargs["contents"]


def test_vertex_invalid_json_counts_as_unavailable():
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(text='{"title_en": 1}')
    provider = VertexGeminiProvider("proj", "r", "m", lambda: client)
    assert run_ai(provider, "x", "x", CATALOG, []).flags == [AI_UNAVAILABLE_FLAG]


def test_response_schema_matches_ai_result_fields():
    assert set(ai_module.RESPONSE_SCHEMA["required"]) == set(AIResult.model_fields)
