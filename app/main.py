"""FastAPI routes. Each job builds a fresh Runtime, so every run starts with one sheet read.

Token checks on /jobs/* and /chat are added in Phase 6 (app/auth.py); do not deploy before that.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from app.drive import DriveStore, GoogleDriveStore
from app.extract.ai import provider_from_config
from app.gmail import GmailAuthError, GmailClient, GoogleGmailClient, process_inbox
from app.jobs import run_changes, run_digest, run_recurring
from app.pipeline import Services
from app.settings import Settings, get_settings
from app.sheets import GoogleSheetsBackend, SchemaError, SheetStore

logger = logging.getLogger("grc.main")

app = FastAPI(title="grc-tracker", docs_url=None, redoc_url=None, openapi_url=None)


@dataclass
class Runtime:
    settings: Settings
    store: SheetStore
    drive: DriveStore
    gmail: GmailClient

    def services(self) -> Services:
        config = self.store.config()
        ai = provider_from_config(
            config.ai_provider, config.ai_model, config.ai_region, self.settings.gcp_project
        )
        return Services(store=self.store, drive=self.drive, ai=ai)


_gmail_client: GmailClient | None = None


def get_runtime() -> Runtime:
    """Real Google clients. Tests replace this with `app.dependency_overrides`."""
    global _gmail_client
    settings = get_settings()
    if _gmail_client is None:
        _gmail_client = GoogleGmailClient.from_secrets(
            settings.gmail_token_secret, settings.oauth_client_secret
        )
    return Runtime(
        settings=settings,
        store=SheetStore(GoogleSheetsBackend(settings.sheet_id)),
        drive=GoogleDriveStore(settings.shared_drive_folder_id),
        gmail=_gmail_client,
    )


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


def _job_error(job: str, error: Exception) -> JSONResponse:
    kind = "schema_error" if isinstance(error, SchemaError) else "gmail_auth_error"
    logger.error(kind, extra={"job": job})
    return JSONResponse(status_code=500, content={"job": job, "error": kind})


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/jobs/gmail", response_model=None)
def gmail_job(runtime: RuntimeDep) -> dict[str, object] | JSONResponse:
    try:
        runtime.store.load()
        run = process_inbox(runtime.gmail, runtime.services())
    except (SchemaError, GmailAuthError) as error:
        return _job_error("gmail", error)
    return {"job": "gmail", **run.model_dump()}


@app.post("/jobs/changes", response_model=None)
def changes_job(runtime: RuntimeDep) -> dict[str, object] | JSONResponse:
    try:
        runtime.store.load()
        run = run_changes(runtime.store, runtime.gmail, runtime.settings.sheet_url)
    except (SchemaError, GmailAuthError) as error:
        return _job_error("changes", error)
    return {"job": "changes", **run.model_dump()}


@app.post("/jobs/recurring", response_model=None)
def recurring_job(runtime: RuntimeDep) -> dict[str, object] | JSONResponse:
    try:
        runtime.store.load()
        run = run_recurring(runtime.services())
    except SchemaError as error:
        return _job_error("recurring", error)
    return {"job": "recurring", **run.model_dump()}


@app.post("/jobs/digest", response_model=None)
def digest_job(runtime: RuntimeDep) -> dict[str, object] | JSONResponse:
    try:
        runtime.store.load()
        run = run_digest(runtime.store, runtime.gmail, runtime.settings.sheet_url)
    except (SchemaError, GmailAuthError) as error:
        return _job_error("digest", error)
    return {"job": "digest", **run.model_dump()}
