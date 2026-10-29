"""FastAPI routes. Each call builds a fresh Runtime, so every run starts with one sheet read.

/jobs/* needs a Cloud Scheduler OIDC token and /chat a Google Chat token (app/auth.py);
/health and /healthz are open and return no data (Cloud Run reserves some paths ending in
"z", so /health is the one to use there; /healthz stays for DESIGN section 7).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.alerts import send_alert
from app.auth import require_chat, require_scheduler
from app.chat import ChatClient, GoogleChatClient, handle_event
from app.drive import DriveStore, GoogleDriveStore, NoFileStorage
from app.extract.ai import provider_from_config
from app.gmail import GmailAuthError, GmailClient, GoogleGmailClient, process_inbox
from app.jobs import run_changes, run_digest, run_recurring
from app.logs import setup_logging
from app.pipeline import Services
from app.settings import Settings, get_settings
from app.sheets import GoogleSheetsBackend, SchemaError, SheetStore

logger = logging.getLogger("grc.main")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    yield


app = FastAPI(
    title="grc-tracker", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
)


@dataclass
class Runtime:
    settings: Settings
    store: SheetStore
    drive: DriveStore
    gmail: GmailClient
    chat: ChatClient

    def services(self) -> Services:
        config = self.store.config()
        ai = provider_from_config(
            config.ai_provider,
            config.ai_model,
            config.ai_region,
            self.settings.gcp_project,
            self.settings.gemini_key_secret,
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
        drive=(
            GoogleDriveStore(settings.shared_drive_folder_id)
            if settings.shared_drive_folder_id
            else NoFileStorage()
        ),
        gmail=_gmail_client,
        chat=GoogleChatClient(),
    )


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


def _log_schema_error(job: str, error: SchemaError) -> None:
    # Problems name only tabs, columns and settings (never cell values), so they are safe to log.
    # The log is the only place they show when Config itself is unreadable and no alert can go.
    logger.error("schema_error", extra={"job": job, "n_problems": len(error.problems)})
    for problem in error.problems:
        logger.error("schema_problem", extra={"job": job, "reason": problem})


def _job_error(job: str, error: Exception, runtime: Runtime) -> JSONResponse:
    if isinstance(error, SchemaError):
        _log_schema_error(job, error)
        send_alert("schema_error", error.problems, runtime.gmail, runtime.store)
        kind = "schema_error"
    else:
        # The mailbox itself is broken, so no email can go out: the log-based alert covers it.
        # str(error) is "RefreshError: <Google reason code>", never a token (gmail.py).
        logger.error("gmail_auth_error", extra={"job": job, "reason": str(error)})
        kind = "gmail_auth_error"
    return JSONResponse(status_code=500, content={"job": job, "error": kind})


jobs = APIRouter(prefix="/jobs", dependencies=[Depends(require_scheduler)])


@app.get("/health")
@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@jobs.post("/gmail", response_model=None)
def gmail_job(runtime: RuntimeDep) -> dict[str, object] | JSONResponse:
    try:
        runtime.store.load()
        run = process_inbox(runtime.gmail, runtime.services())
    except (SchemaError, GmailAuthError) as error:
        return _job_error("gmail", error, runtime)
    if run.gave_up:
        details = [f"{run.gave_up} request(s) moved to GRC/Failed in this run"]
        send_alert("request_gave_up", details, runtime.gmail, runtime.store)
    return {"job": "gmail", **run.model_dump()}


@jobs.post("/changes", response_model=None)
def changes_job(runtime: RuntimeDep) -> dict[str, object] | JSONResponse:
    try:
        runtime.store.load()
        run = run_changes(runtime.store, runtime.gmail, runtime.settings.sheet_url)
    except (SchemaError, GmailAuthError) as error:
        return _job_error("changes", error, runtime)
    return {"job": "changes", **run.model_dump()}


@jobs.post("/recurring", response_model=None)
def recurring_job(runtime: RuntimeDep) -> dict[str, object] | JSONResponse:
    try:
        runtime.store.load()
        run = run_recurring(runtime.services())
    except SchemaError as error:
        return _job_error("recurring", error, runtime)
    return {"job": "recurring", **run.model_dump()}


@jobs.post("/digest", response_model=None)
def digest_job(runtime: RuntimeDep) -> dict[str, object] | JSONResponse:
    try:
        runtime.store.load()
        run = run_digest(runtime.store, runtime.gmail, runtime.settings.sheet_url)
    except (SchemaError, GmailAuthError) as error:
        return _job_error("digest", error, runtime)
    return {"job": "digest", **run.model_dump()}


app.include_router(jobs)


@app.post("/chat", dependencies=[Depends(require_chat)])
async def chat_event(request: Request, runtime: RuntimeDep) -> dict[str, object]:
    event = await request.json()
    return await run_in_threadpool(_handle_chat, event, runtime)


def _handle_chat(event: dict[str, object], runtime: Runtime) -> dict[str, object]:
    try:
        runtime.store.load()
        return handle_event(event, runtime.services(), runtime.chat, runtime.gmail)
    except SchemaError as error:
        _log_schema_error("chat", error)
        return {"text": "The GRC tracker is not available right now. Please email the request."}
    except GmailAuthError as error:
        logger.error("gmail_auth_error", extra={"job": "chat", "reason": str(error)})
        return {"text": "The GRC tracker is not available right now. Please email the request."}
