# OBI Slice Anonymizer

The anonymization engine used to anonymize the OBI data slices (kiran, nick, …).
This bundle contains the **core logic + documentation** only (no one-off remediation scripts).

## New here? Start in this order

1. **[docs/ENVIRONMENT_SETUP.md](docs/ENVIRONMENT_SETUP.md)** — get Python, the ODBC driver, and
   the dependencies installed. Do this first; nothing else runs without it.
2. **[docs/ANONYMIZATION_APPROACH.md](docs/ANONYMIZATION_APPROACH.md)** — the design in one read:
   what problem this solves, the "three C's" (Client/Colleague/Cash), the 4-step workflow.
3. **[docs/ANONYMIZER_GUIDE.md](docs/ANONYMIZER_GUIDE.md)** — the day-to-day reference: v1/v2
   logic versions, `.env` credentials, the mapping table, column types, safety guards, and the
   actual `analyze → plan → run → verify` sequence per table.

That's enough to run the tool. The rest is read **as needed**, not up front:

| Doc | Read it when… |
|---|---|
| [docs/CHINESE_DATA_ANONYMIZATION.md](docs/CHINESE_DATA_ANONYMIZATION.md) | a table has Chinese-language names/orgs/cities — explains `obi_chinese_anonymizer.py`. |
| [docs/COLUMN_REVIEW.md](docs/COLUMN_REVIEW.md) | **before running any shared-slice table** — see "Before you run anything on a shared table" below. Not a cover-to-cover read, a per-table lookup. |
| [docs/COLUMN_REVIEW_TEMPLATE.md](docs/COLUMN_REVIEW_TEMPLATE.md) | a table isn't in `COLUMN_REVIEW.md` yet — use this as the structure for getting the team's decisions written down. |
| [docs/ENGINE_CHANGES.md](docs/ENGINE_CHANGES.md) | you want the "why" behind a specific piece of engine logic (windowing, forced substitutions, deny-lists, …). |
| [docs/ANONYMIZATION_END_TO_END_RUNBOOK.md](docs/ANONYMIZATION_END_TO_END_RUNBOOK.md) | you need the *broader* raw-Postgres-dump → anonymized-Postgres-dump pipeline this engine plugs into. **Heads up:** it links to `VM_CONNECTION.md` / `slice_load/LOAD_SLICE_RUNBOOK.md`, which aren't part of this trimmed bundle — background context only, not something you can follow start-to-finish from here. |

## Before you run anything on a shared table

Multiple people work these slices at once. **Before you `run` any table, check
[docs/COLUMN_REVIEW.md](docs/COLUMN_REVIEW.md) for that table first** and follow its column/type
decisions exactly — don't re-analyze and decide independently, or you risk anonymizing a column
differently than a teammate already did, or skipping one everyone assumed someone else had
covered.

If the table isn't in `COLUMN_REVIEW.md` yet: run `analyze`, review the resulting plan **as a
team** using [docs/COLUMN_REVIEW_TEMPLATE.md](docs/COLUMN_REVIEW_TEMPLATE.md) as the structure,
get agreement, then add it to `COLUMN_REVIEW.md` (with an owner + status, per the template)
*before* anyone runs it for real.

## Contents

| File | What it is |
|---|---|
| `obi_anonymizer.py` | The anonymization engine — CLI: `analyze` / `plan` / `run` / `verify` per table. |
| `obi_chinese_anonymizer.py` | Chinese-language name/org/city handling, called from `obi_anonymizer.py`. |
| `constants.py` | Shared stopword/brand/domain constants used by the engine. |
| `set_plan.py` | Helper to edit a table's column plan (enable/disable columns, set PII type). |
| `helpers/phase_freetext_scrub.py` | Post-step: map-based whole-cell scrub for free-text columns (see README §"How it works"). |
| `tests/` | `test_gliner_windows.py`, `test_engine_changes.py` — run these after touching the engine. |
| `requirements.txt` | Pinned dependency versions, tiered (see `docs/ENVIRONMENT_SETUP.md`). |
| `.env.example` | Config template — copy to `.env` and fill credentials (no secrets committed). |
| `docs/COLUMN_REVIEW.md` | The team's agreed per-table column/type decisions — check before running a shared table. |
| `docs/COLUMN_REVIEW_TEMPLATE.md` | Generic blank structure for proposing/agreeing a new table's column plan. |
| `docs/` | See "New here?" above for the rest of what to read and when. |

## How it works (in brief)

- **Source of truth:** the shared entity map `obi.mapping_slice` (`originalvalue → anonymizedvalue`,
  with a `description` type ∈ Names / Email / CompanyName / Phone / country / id / …). A value already
  mapped there reuses its fake (consistency); new values get a deterministic fake.
- **Detection:** GLiNER PII detection for free-text columns + column-plan types for structured columns.
- **Fake generation:** deterministic, ethnicity/gender-aware, injective fakes — the same original
  yields the same fake across rows/tables. Money/quantity columns get a ±15% jitter.
- **Forced substitutions:** e.g. `centific → aventraa`, `pactera → eventraa` (case-preserving, anywhere).

## Quick start

Set up Python/the ODBC driver/dependencies first — see
**[docs/ENVIRONMENT_SETUP.md](docs/ENVIRONMENT_SETUP.md)** if you haven't. Then:

```bash
cp .env.example .env            # fill in DB credentials (Azure map + local slice DB)
source .venv/bin/activate       # the venv you built per ENVIRONMENT_SETUP.md
python obi_anonymizer.py analyze <table>            # detect PII columns -> <table>.plan.json
python obi_anonymizer.py run     <table> --limit 100   # sample run
python obi_anonymizer.py run     <table> --limit all   # full run (resumes from checkpoint)
python obi_anonymizer.py verify  <table>            # residual-PII + sample check
```

See `docs/ANONYMIZER_GUIDE.md` for the full per-table workflow (v1/v2, column plans, safety guards).

> **Credentials:** never commit a real `.env` or any key. Set `PWD=` via `.env` / environment;
> the engine reads connection strings from env vars (`OBI_ANON_CS` / `V1_MAP_CS` / …).
