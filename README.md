# grc-tracker

GRC request intake and task assignment for BRI HK. The design (source of truth) is
[`docs/DESIGN.md`](docs/DESIGN.md).

Build status: **Phase 2** (rules, sheet store and routing; tested against an in-memory sheet).

## Local development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Tests run with the clock frozen at Monday 26 Oct 2026 09:00 Asia/Hong_Kong (`tests/conftest.py`).

Environment variables, the IT setup checklist and deploy steps are added in Phase 7.
