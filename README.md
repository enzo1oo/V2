# grc-tracker

GRC request intake and task assignment for BRI HK. The design (source of truth) is
[`docs/DESIGN.md`](docs/DESIGN.md).

Build status: **Phase 6** (all endpoints built; every `/jobs/*` and `/chat` call is token-checked).
Phase 7 adds the Dockerfile, Cloud Build, Apps Script audit and the IT setup checklist.

## Local development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Tests run with the clock frozen at Monday 26 Oct 2026 09:00 Asia/Hong_Kong (`tests/conftest.py`).

Local run against the **dev** sheet (env vars as in DESIGN section 10, in a `.env` that is never
committed):

```bash
.venv/bin/uvicorn app.main:app --reload --env-file .env
```

The Gmail refresh token is minted once with `scripts/mint_gmail_token.py` (see its docstring) and
stored in Secret Manager. The full IT setup checklist and deploy steps are added in Phase 7.
