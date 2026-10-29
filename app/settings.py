"""Environment variables (IDs only, DESIGN section 10) and Secret Manager access.

Secrets are never read from the environment: an env var only names the secret.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import BaseModel


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

    @property
    def sheet_url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.sheet_id}"


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
