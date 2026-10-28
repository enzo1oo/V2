# CLAUDE.md (placeholder)

Replace this file with the project's CLAUDE.md. Until then:

- `docs/DESIGN.md` is the source of truth. A design change updates DESIGN.md in the same commit.
- Pure logic lives in `app/extract/*` and `app/route.py` and is fully unit-tested.
- Never log or commit message bodies, names, emails or phone numbers. Fixtures use fake data only.
- Before reporting a phase: `pytest` and `ruff check` / `ruff format --check` must pass.
