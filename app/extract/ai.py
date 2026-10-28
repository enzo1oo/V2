"""AI extraction behind one interface (DESIGN section 6).

The AI only suggests a title, summary, deliverables, deadline and catalog ID. It never picks the
assignee. `run_ai` adds the guardrails (timeout, one retry, quote check, catalog check) so every
provider gets them, and it never raises: on any failure the pipeline continues on rules.

Logs carry provider, model, prompt version, latency and success only — never message text.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from app.models import AIResult, CatalogEntry

logger = logging.getLogger("grc.ai")

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "extract_v1.md"
PROMPT_VERSION = "extract_v1"
MAX_INPUT_CHARS = 8_000
TIMEOUT_SECONDS = 20.0
AI_UNAVAILABLE_FLAG = "AI unavailable"

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai")


class NotConfigured(Exception):
    """The provider cannot be used yet (e.g. the BRI agent API is not known)."""


class AIProvider(Protocol):
    name: str
    model: str

    def extract(
        self, masked_text: str, catalog: list[CatalogEntry], attachment_names: list[str]
    ) -> AIResult | None: ...


# --- Prompt ------------------------------------------------------------------------------------


def catalog_lines(catalog: list[CatalogEntry]) -> str:
    """`ID — report type` per non-ADHOC catalog row (the only IDs the AI may return)."""
    return "\n".join(
        f"{entry.catalog_id} — {entry.report_type}" for entry in catalog if not entry.is_adhoc
    )


def build_prompt(masked_text: str, catalog: list[CatalogEntry], attachment_names: list[str]) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    attachments = "\n".join(f"- {name}" for name in attachment_names) or "(none)"
    return (
        template.replace("{{catalog}}", catalog_lines(catalog))
        .replace("{{attachments}}", attachments)
        .replace("{{text}}", masked_text[:MAX_INPUT_CHARS])
    )


# JSON Schema for the model's answer. `deadline` is an ISO date string; AIResult parses it.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title_en": {"type": "string", "maxLength": 90},
        "summary_en": {"type": "string"},
        "deliverables": {"type": "array", "items": {"type": "string"}},
        "deadline": {"type": ["string", "null"], "format": "date"},
        "deadline_quote": {"type": ["string", "null"]},
        "requester_org": {"type": ["string", "null"]},
        "contacts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "email": {"type": ["string", "null"]},
                },
                "required": ["name", "email"],
            },
        },
        "catalog_id": {"type": "string"},
        "language": {"type": "string", "enum": ["id", "en", "mixed"]},
    },
    "required": [
        "title_en",
        "summary_en",
        "deliverables",
        "deadline",
        "deadline_quote",
        "requester_org",
        "contacts",
        "catalog_id",
        "language",
    ],
}


# --- Providers ---------------------------------------------------------------------------------


class NullProvider:
    """`AI provider = Off`."""

    name = "Off"
    model = ""

    def extract(
        self, masked_text: str, catalog: list[CatalogEntry], attachment_names: list[str]
    ) -> AIResult | None:
        return None


class BriAgentProvider:
    """BRI's AI agent. A stub until the agent owner shares the endpoint, auth and payload."""

    name = "BRI agent"

    def __init__(self, model: str = ""):
        self.model = model

    def extract(
        self, masked_text: str, catalog: list[CatalogEntry], attachment_names: list[str]
    ) -> AIResult | None:
        raise NotConfigured("BRI agent API details are not known yet")


class VertexGeminiProvider:
    """Gemini on Vertex AI through the google-genai SDK: temperature 0, schema-enforced JSON."""

    name = "Vertex"

    def __init__(
        self, project: str, region: str, model: str, client_factory: Callable[[], Any] | None = None
    ):
        self.project = project
        self.region = region
        self.model = model
        self._client_factory = client_factory
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                from google import genai
                from google.genai import types

                self._client = genai.Client(
                    vertexai=True,
                    project=self.project,
                    location=self.region,
                    http_options=types.HttpOptions(timeout=int(TIMEOUT_SECONDS * 1000)),
                )
        return self._client

    def extract(
        self, masked_text: str, catalog: list[CatalogEntry], attachment_names: list[str]
    ) -> AIResult | None:
        from google.genai import types

        response = self._get_client().models.generate_content(
            model=self.model,
            contents=build_prompt(masked_text, catalog, attachment_names),
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
                response_json_schema=RESPONSE_SCHEMA,
            ),
        )
        if not response.text:
            return None
        return AIResult.model_validate_json(response.text)


def provider_from_config(provider: str, model: str, region: str, project: str) -> AIProvider:
    if provider == "Vertex":
        return VertexGeminiProvider(project=project, region=region, model=model)
    if provider == "BRI agent":
        return BriAgentProvider(model=model)
    return NullProvider()


# --- Guardrails --------------------------------------------------------------------------------


@dataclass
class AIOutcome:
    result: AIResult | None
    flags: list[str] = field(default_factory=list)


def _normalise_quote(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("*", "")).strip().casefold()


def apply_guardrails(result: AIResult, original_text: str, catalog: list[CatalogEntry]) -> AIResult:
    """Drop a deadline whose quote is not in the original text; unknown catalog IDs → NONE.

    The quote check ignores whitespace, line breaks, `*` emphasis and case, so a deadline that
    wraps over two lines in the email still counts as word-for-word.
    """
    updates: dict[str, Any] = {}
    quote = result.deadline_quote or ""
    if result.deadline is not None and (
        not quote.strip() or _normalise_quote(quote) not in _normalise_quote(original_text)
    ):
        updates["deadline"] = None
    known = {entry.catalog_id for entry in catalog if not entry.is_adhoc}
    if result.catalog_id not in known:
        updates["catalog_id"] = "NONE"
    return result.model_copy(update=updates) if updates else result


def run_ai(
    provider: AIProvider,
    masked_text: str,
    original_text: str,
    catalog: list[CatalogEntry],
    attachment_names: list[str],
    timeout: float | None = None,
    tries: int = 2,
) -> AIOutcome:
    """Call the provider with a timeout and one retry; never raises."""
    if isinstance(provider, NullProvider):
        return AIOutcome(None)
    timeout = TIMEOUT_SECONDS if timeout is None else timeout
    for attempt in range(1, tries + 1):
        started = time.monotonic()
        error_type = ""
        result: AIResult | None = None
        try:
            future = _executor.submit(
                provider.extract, masked_text[:MAX_INPUT_CHARS], catalog, attachment_names
            )
            result = future.result(timeout=timeout)
        except NotConfigured:
            error_type = "NotConfigured"
        except FutureTimeout:
            error_type = "Timeout"
        except ValidationError:
            error_type = "InvalidResponse"
        except Exception as error:  # any SDK/network error: the pipeline carries on with rules
            error_type = type(error).__name__
        latency_ms = round((time.monotonic() - started) * 1000)
        success = result is not None
        logger.info(
            "ai_call",
            extra={
                "provider": provider.name,
                "model": provider.model,
                "prompt_version": PROMPT_VERSION,
                "latency_ms": latency_ms,
                "success": success,
                "attempt": attempt,
                "error_type": error_type,
            },
        )
        if success and result is not None:
            return AIOutcome(apply_guardrails(result, original_text, catalog))
        if error_type == "NotConfigured":
            break
    return AIOutcome(None, [AI_UNAVAILABLE_FLAG])
