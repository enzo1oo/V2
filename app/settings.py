"""Environment variables (IDs only, DESIGN section 10) and Secret Manager access.

Gmail and Gemini secrets are not read from the environment: an env var only names the secret.
The WhatsApp secrets (v1.3) are env vars that Cloud Run fills from Secret Manager; they are
`SecretStr`, so they never show in a repr or a log.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import BaseModel, SecretStr


class Settings(BaseModel):
    gcp_project: str = ""
    gcp_region: str = ""
    sheet_id: str = ""
    shared_drive_folder_id: str = ""
    gmail_token_secret: str = ""
    oauth_client_secret: str = ""
    gemini_key_secret: str = ""
    scheduler_sa_email: str = ""
    service_url: str = ""
    chat_project_number: str = ""
    # WhatsApp (v1.3). The two secrets are injected by Cloud Run from Secret Manager.
    runtime_sa_email: str = ""  # Cloud Tasks calls /tasks/* with this account's token
    wa_phone_number_id: str = ""
    wa_waba_id: str = ""
    wa_graph_version: str = ""
    wa_verify_token: str = ""
    wa_access_token: SecretStr = SecretStr("")
    wa_app_secret: SecretStr = SecretStr("")
    wa_tasks_queue: str = ""

    @property
    def sheet_url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.sheet_id}"

    @property
    def wa_enabled(self) -> bool:
        return all(
            (
                self.wa_phone_number_id,
                self.wa_graph_version,
                self.wa_verify_token,
                self.wa_access_token.get_secret_value(),
                self.wa_app_secret.get_secret_value(),
            )
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(**{name: os.environ.get(name.upper(), "") for name in Settings.model_fields})


def secret_path(project: str, name: str) -> str:
    """A bare secret name becomes `projects/<project>/secrets/<name>/versions/latest`."""
    if name.startswith("projects/"):
        return name if "/versions/" in name else f"{name}/versions/latest"
    return f"projects/{project}/secrets/{name}/versions/latest"


def read_secret(name: str, project: str | None = None) -> str:
    """Read a secret's value from Secret Manager with the runtime service account."""
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    path = secret_path(project or get_settings().gcp_project, name)
    response = client.access_secret_version(request={"name": path})
    return response.payload.data.decode("utf-8")
