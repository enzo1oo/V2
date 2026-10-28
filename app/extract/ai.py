"""AI extraction behind one interface (DESIGN section 6).

The AI only suggests a title, summary, deliverables, deadline and catalog ID. It never picks the
assignee. `run_ai` adds the guardrails (timeout, one retry, quote check, catalog check) so every
provider gets them, and it never raises: on any failure the pipeline continues on rules.

Logs carry provider, model, prompt version, latency and success only — never message text.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from app.models import DEFAULT_AI_MODEL, AIResult, CatalogEntry

logger = logging.getLogger("grc.ai")

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "extract_v1.md"
PROMPT_VERSION = "extract_v1"
MAX_INPUT_CHARS = 8_000
TIMEOUT_SECONDS = 20.0
AI_UNAVAILABLE_FLAG = "AI unavailable"

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai")


class NotConfigured(Exception):
    """The provider cannot be used: no API key, or Vertex mode without project and region."""


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


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace `$ref`s with their `$defs`, so the API sees one self-contained schema."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].rsplit("/", 1)[-1]])
            return {key: walk(value) for key, value in node.items() if key != "$defs"}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(schema)


# The answer's JSON Schema, derived from AIResult (DESIGN section 6). The API does not enforce
# maxLength, so AIResult's validators trim; model_validate_json checks everything else.
RESPONSE_SCHEMA: dict[str, Any] = _inline_refs(AIResult.model_json_schema())


# --- Providers ---------------------------------------------------------------------------------


class NullProvider:
    """`AI provider = Off`."""

    name = "Off"
    model = ""

    def extract(
        self, masked_text: str, catalog: list[CatalogEntry], attachment_names: list[str]
    ) -> AIResult | None:
        return None


class GeminiProvider:
    """Gemini through the google-genai SDK (2.33): API-key mode or Vertex mode (DESIGN D6).

    Uses the SDK's documented structured-output call, `client.interactions.create(...,
    response_format={"type": "text", "mime_type": "application/json", "schema": ...})`, and
    reads `interaction.output_text`. The SDK's own retries are switched off: `run_ai` owns the
    timeout-and-one-retry policy. Requests are not stored on Google's side (`store=False`).
    """

    def __init__(
        self,
        mode: Literal["api_key", "vertex"],
        model: str,
        *,
        api_key: Callable[[], str] | None = None,
        project: str = "",
        region: str = "",
        client_factory: Callable[[], Any] | None = None,
    ):
        self.mode = mode
        self.name = "Gemini API key" if mode == "api_key" else "Vertex"
        self.model = model or DEFAULT_AI_MODEL
        self._api_key = api_key
        self._project = project
        self._region = region
        self._client_factory = client_factory
        self._client: Any = None
        self._lock = threading.Lock()

    def _get_client(self) -> Any:
        with self._lock:
            if self._client is None:
                self._client = (
                    self._client_factory() if self._client_factory else self._build_client()
                )
            return self._client

    def _build_client(self) -> Any:
        from google import genai
        from google.genai import types

        http_options = types.HttpOptions(
            timeout=int(TIMEOUT_SECONDS * 1000),
            retry_options=types.HttpRetryOptions(attempts=1),
        )
        if self.mode == "vertex":
            if not self._project or not self._region:
                raise NotConfigured("Vertex mode needs GCP_PROJECT and Config 'AI region'")
            return genai.Client(
                vertexai=True,
                project=self._project,
                location=self._region,
                http_options=http_options,
            )
        key = self._api_key() if self._api_key else ""
        if not key:
            raise NotConfigured("no Gemini API key")
        return genai.Client(api_key=key, http_options=http_options)

    def extract(
        self, masked_text: str, catalog: list[CatalogEntry], attachment_names: list[str]
    ) -> AIResult | None:
        interaction = self._get_client().interactions.create(
            model=self.model,
            input=build_prompt(masked_text, catalog, attachment_names),
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": RESPONSE_SCHEMA,
            },
            generation_config={"temperature": 0},
            store=False,
        )
        text = getattr(interaction, "output_text", None)
        if not text:
            return None
        return AIResult.model_validate_json(text)


def gemini_api_key(secret_name: str, project: str) -> Callable[[], str]:
    """The key loader: GEMINI_API_KEY (local .env) first, else Secret Manager. Never logged."""

    def load() -> str:
        local = os.environ.get("GEMINI_API_KEY", "").strip()
        if local:
            return local
        if not secret_name:
            raise NotConfigured("GEMINI_KEY_SECRET is not set")
        from app.settings import read_secret

        return read_secret(secret_name, project).strip()

    return load


def provider_from_config(
    provider: str, model: str, region: str, project: str, key_secret: str = ""
) -> AIProvider:
    """Config `AI provider` → provider. Switching mode is a Config change, not a code change."""
    if provider == "Gemini API key":
        return GeminiProvider("api_key", model, api_key=gemini_api_key(key_secret, project))
    if provider == "Vertex":
        return GeminiProvider("vertex", model, project=project, region=region)
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


def _status_code(error: Exception) -> int:
    for attribute in ("status_code", "code"):
        value = getattr(error, attribute, None)
        if isinstance(value, int):
            return value
    return 0


def _permanent(status: int) -> bool:
    """4xx (bad key, location not supported, bad request) will not succeed on a retry."""
    return 400 <= status < 500 and status not in (408, 429)


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
        status = 0
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
            status = _status_code(error)
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
                "status": status,
            },
        )
        if success and result is not None:
            return AIOutcome(apply_guardrails(result, original_text, catalog))
        if error_type == "NotConfigured" or _permanent(status):
            break
    return AIOutcome(None, [AI_UNAVAILABLE_FLAG])
