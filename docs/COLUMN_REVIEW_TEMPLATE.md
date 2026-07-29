# Column Review — Template

## Purpose

Several people work the same shared slice/mapping table at once. If two people independently
decide what to do with the same table's columns, they can disagree — one anonymizes a column
the other left raw, or the same column gets typed two different ways in two different runs. That
produces inconsistent output and, worse, can leave a column un-anonymized simply because everyone
assumed someone else had already covered it.

This file is the fix: a **written, per-table agreement** of exactly which columns get
anonymized, as what type, before anyone runs `run` for real. Once a table's section is filled in
and the team has agreed on it:

- It's the source of truth for that table — don't re-decide it independently.
- Whoever runs the table uses `set_plan.py` (or hand-edits `<table>.plan.json`) to match this
  section exactly, then runs `run`.
- If you think a decision here is wrong, raise it with the team and edit this file — don't just
  quietly run something different.

Copy this file's structure into [COLUMN_REVIEW.md](COLUMN_REVIEW.md) (or append a new `##`
section directly there) once a table's plan is agreed. Keep this template itself generic — don't
fill in real table/column data here.

---

## The type & mode vocabulary (keep this in sync with the engine)

**`mode`** — which code path a column goes through:
- `structured` — dispatches on the column's `TYPE` (below).
- `freetext` — the whole cell is scanned by GLiNER for embedded PII (person / organization /
  email / phone-number spans), regardless of `TYPE`. Long comment/description/body columns.

**`TYPE`** (meaningful when `mode=structured`):

| Type | What it means | Fake behavior |
|---|---|---|
| `person` | A person's name | Ethnicity/gender-aware fake name, deterministic & reused |
| `org` | Company/organization name | Structure-preserving brandable fake (keeps legal suffixes) |
| `email` | Email address | Local part + domain faked, consistent with any faked contact name |
| `phone` | Phone number | Digit-for-digit format-preserving replacement |
| `id` | Opaque ID / GUID / personnel number | Class-preserving (digit→digit, letter→letter, same length) |
| `url` | URL / website | Domain swapped consistently, path/query kept |
| `birth` | Date of birth | Calendar-aware jitter (shifts the date) |
| `region` | State/province code (paired with a country column) | Different real region code, same country namespace |
| `country` | Country | Different real country, form-preserving (2-letter/3-letter/full name) |
| `location` | City | Different real city |
| `amount` | Money / quantity | Random ±15% jitter — NOT identity-masking, just perturbation |
| `skip` | Reference/enum value — NOT personal data (status codes, stage names, product lines, application names, …) | Preserved verbatim, on purpose |

Do not use `freetext` as a `TYPE` value when deciding — it belongs in `mode`. If `analyze`'s
draft plan shows `freetext` in the `TYPE` column for a freetext-mode row, that's fine (harmless
tooling convention), just don't hand-pick it as a type for a *structured* column.

**Getting the type wrong has real, prior-confirmed consequences** — reference/enum columns
(stage names, product lines, application names, category names, …) mistyped as `person`/`org`
have leaked as garbage fakes in earlier runs; that's specifically why `skip` and the deny-list
behind it exist. When in doubt, prefer `skip` over guessing `person`/`org`, and say so in Notes.

---

## How to fill this out

1. Run `analyze <table>` — this detects candidate PII columns and writes a draft
   `_state/<table>.plan.json`, plus prints a `KEEP?/column/TYPE/mode/conf/sample` table like the
   one below.
2. Review it **as a team**, not solo — especially anything with low `conf` or anything that
   looks like a reference/enum value rather than real PII.
3. Transcribe the **agreed** decision into a section below (or directly into
   `COLUMN_REVIEW.md`), filling in the metadata line and the table.
4. Whoever runs it applies the same decisions via `set_plan.py <table> COL:type COL:type ...`
   before `run`.
5. If the plan changes later (a column added, a type corrected), update this section and bump
   **Last updated** — don't let the doc drift from what's actually being run.

---

## `<TABLE_NAME>` — rows=`<row count>`, `<N>` ON

**Owner:** `<who is running/responsible for this table>`
**Status:** `proposed` / `agreed` / `locked` — *(proposed = draft from analyze, not yet reviewed;
agreed = team has signed off; locked = already run for real — changing it now needs a new pass,
not a silent edit)*
**Last updated:** `<YYYY-MM-DD>`
**Notes:** `<anything a teammate should know before touching this table — e.g. "shared with
another team, only raw_json is freetext-scrubbed, rest is skip">`

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| y/n | `<column_name>` | `<type>` | `structured` / `freetext` | `<0.0–1.0, from analyze>` | `<representative example value — not the most sensitive row you can find>` |

*(Add one row per candidate column. Delete this instructional row once real rows are added.)*

### Worked example *(illustrative only — delete before use)*

**Owner:** jdoe · **Status:** agreed · **Last updated:** 2026-01-01
**Notes:** `status_code` and `record_type` are enum/reference values, not PII — kept verbatim.

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | contact_email | email | structured | 1.0 | jane.doe@example.com |
| **y** | contact_name | person | structured | 0.8 | Jane Doe |
| **y** | employer_name | org | structured | 0.7 | Example Holdings LLC |
| **y** | phone_number | phone | structured | 0.6 | +1-555-0142 |
| **y** | notes | freetext | freetext | 0.5 | "Spoke with Jane Doe at Example Holdings re: renewal" |
| **n** | status_code | skip | structured | 0.0 | ACTIVE |
| **n** | record_type | skip | structured | 0.0 | Lead |
