# OBI Slice Anonymizer — Consolidated Guide

_Single source of reference for the from-scratch table anonymizer: what it does, how the data was
loaded, the current approach, and step-by-step execution. Supersedes the older `README.md` and
`ANONYMIZATION_APPROACH.md` in this folder._

_Last updated: 2026-07-06._

---

## 0. Logic versions (v1 / v2) & credentials (`.env`)

The engine supports **two logic versions**, selected with `--version {1,2}` (default `1`; or set
`OBI_VERSION`). The flag only swaps four things — **DATA connection, MAPPING connection, mapping-table
name, and state dir** — the anonymization logic itself is identical.

| | **v1** (default, present behaviour) | **v2** (new) |
|---|---|---|
| DATA (what gets anonymized) | a migrated **slice** (local SQL Server, e.g. `obi_nick_slice`) | **`obi-sql-db`** tables directly (Azure) |
| MAPPING source of truth | `obi.mapping_slice` (shared, Azure) | `obi.mapping_xref` (Azure) |
| existing `<table>_anonymized` | foreign-row guard: refuses unless `--restart` (or `--columns` for in-place) | **auto UPDATE-IN-PLACE**: anonymizes only the enabled columns on the existing copy, keyed on PK/unique id; never rebuilds. Builds fresh only if the copy doesn't exist |
| state dir (plans + checkpoints) | `_state/` | `_state_v2/` (so same-named tables never collide with v1) |
| column-review doc | [COLUMN_REVIEW.md](COLUMN_REVIEW.md) | [COLUMN_REVIEW_V2.md](COLUMN_REVIEW_V2.md) |

Every invocation prints its resolved config, e.g.
`[version 2] data=obi-sql-db  mapping=obi-sql-db.mapping_xref  state=_state_v2` (no secrets).

**Credentials via `.env`.** The script auto-loads `anonymizer/.env` at startup into the environment,
**without overriding** anything already exported in the terminal (so existing `OBI_ANON_CS`/`OBI_MAP_CS`
workflows are unchanged). Copy [.env.example](.env.example) → `.env`, fill in values, `chmod 600 .env`.
Keys: `V1_DATA_CS` / `V1_MAP_CS` (v1), `V2_DATA_CS` / `V2_MAP_CS` (v2; `V2_MAP_CS` defaults to
`V2_DATA_CS`), optional `V2_XREF` (default `mapping_xref`). Precedence: terminal env → `.env` → built-in default.

**v2 workflow (per table):**
```bash
python obi_anonymizer.py analyze <table> --version 2                 # plan -> _state_v2/<table>.plan.json
#   edit the plan: enable ONLY the columns you want this run + fix each type
python obi_anonymizer.py run <table> --version 2 --limit 100 --dry-run
python obi_anonymizer.py run <table> --version 2 --limit all         # in-place if copy exists, else fresh
python obi_anonymizer.py verify <table> --version 2 --order-col <unique id>
```

Everything below (types, freetext scrubbing, safety guards, verify) applies to **both** versions.

---

## 1. Purpose & big picture

We anonymize a **slice** of the OBI dataset (Kiran's revops slice) so it can be shared safely.
Each source table `obi.<table>_anonymized` (RAW — despite the name, these hold the real PII slice)
is read, its PII columns are replaced with realistic fakes, and the result is written to a new
`obi.<table>_clean` table. The **raw tables are never modified**; `_clean` is a derived copy.

Every original→fake pair is recorded in a **mapping table** so the same original always maps to the
same fake (consistency) and no two different originals share a fake (bijectivity/reversibility).

**Design principles (all enforced by the engine):**
- **Consistent** — same original → same fake everywhere (via the mapping table + in-memory cache).
- **Bijective / reversible** — two different originals never collapse to one fake.
- **Clean output** — no hex/number suffixes on fakes.
- **Ethnicity preserved** — Indian→Indian, Chinese→romanized pinyin, else Western; gender-matched.
- **Format/shape preserved** — `UNSCO`→`VWXYZ` (acronym), `Centific PVT LTD`→`Aventraa PVT LTD`
  (legal suffix kept), case mirrored.
- **Forced words (hard rule)** — `centific`→`aventraa`, `pactera`→`eventraa` (`pacteraedge`→
  `eventraaedge`) wherever they appear, case-preserving.

---

## 2. How the data was loaded (Kiran's slice)

The slice arrived as a **PostgreSQL dump**: `slice_data/kiran_data_seed.dump` (~51 MB).
Flow (all local, nothing touches production) — see `slice_load/LOAD_SLICE_RUNBOOK.md` for full detail:

1. **SQL Server 2022 in Docker** (the target): container `obi-sqlserver`, `localhost,1433`, `sa`.
2. **Restore the dump into local Postgres** (`obi_slice` DB) with `pg_restore`.
3. **Migrate Postgres → SQL Server** with `slice_load/pg_to_mssql.py` — recreates each non-empty
   `obi` table (type-mapped), bulk-loads rows, and creates an empty `obi.mapping_xref`.
4. Result: local SQL Server DB **`obi_slice`**, schema **`obi`**, ~27 data tables named
   `<table>_anonymized` (RAW slice data) + `mapping_xref`.

> Blobs (HTML/PDF/xlsx attachments) are **not** handled by this DB tool — they need separate file
> anonymization before the slice is fully shareable.

---

## 3. Environment & prerequisites

- **Local SQL Server** (Docker `obi-sqlserver`): `localhost,1433`, DB `obi_slice`, schema `obi`,
  user `sa`, password `__SET_VIA_ENV__`.
- **Python 3.14** (local). All libraries work on 3.14 — **no separate venv needed**:
  `pip install faker pyodbc pypinyin gender-guesser ethnicseer gliner names-dataset pg8000`.
  (GLiNER pulls PyTorch; first run downloads the model `urchade/gliner_multi_pii-v1` to the HF cache.)
- **ODBC Driver 17 for SQL Server** installed.
- **Connection string** via env var (the tool reads `OBI_ANON_CS` automatically):
  ```bash
  export OBI_ANON_CS="DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost,1433;DATABASE=obi_slice;UID=sa;PWD=__SET_VIA_ENV__;Encrypt=no;TrustServerCertificate=yes;"
  ```
  (PowerShell: `$env:OBI_ANON_CS = "..."`)

---

## 4. The mapping table (single source of truth)

- **Columns:** `id` (int identity PK), `description` (type label: Names/Email/CompanyName/…),
  `originalvalue`, `anonymizedvalue`, `comment`.
- **Read-write, reuse-first:** at the start of every run the whole table is preloaded into memory;
  an existing original→fake is **reused**; a new pair is **generated and inserted back** (batched).
- **Never stores:** `id`/`url`/`amount` values (those are format-preserving/deterministic or jitter);
  no-op pairs (fake == original); values > 256 chars or non-ASCII.
- **Location (LIVE as of 2026-07-03):** **`obi.mapping_slice` on Azure `obi-sql-db`** — the shared
  single source of truth all teammates reference. **Data tables stay local**; the anonymizer uses a
  **separate mapping connection** (Azure) to preload/reuse existing pairs and insert new ones, while
  reading/writing the data tables on the local connection.
  - Data connection env var: **`OBI_ANON_CS`** (local slice).
  - Mapping connection env var: **`OBI_MAP_CS`** (defaults to Azure `obi-sql-db`; see `VM_CONNECTION.md`).
  - Requires the running machine's IP whitelisted on the Azure SQL firewall.
  - Migration was done via `push_to_azure.py` (seeded from the local bijective map, 13,945 pairs).
  - **Version-dependent (see §0):** `mapping_slice` is the map for **v1**. **v2** instead uses
    `obi.mapping_xref` on the same Azure `obi-sql-db` (`V2_MAP_CS` / `V2_XREF`) — so the "legacy"
    `mapping_xref` **is** the source of truth when you run with `--version 2`.

---

## 5. Column types (what you assign per column)

| type | use for | behaviour |
|---|---|---|
| `person` | people's names | realistic fake name; ethnicity+gender preserved; token-level so first/last/full stay consistent |
| `org` | company / client / customer / dept names & codes | brandable fake (`Vertex Systems`); keeps digits/separators/legal-suffixes; forced words applied |
| `email` | email addresses | fake local part + masked domain; forced domain (`@centific.com`→`@aventraa.com`) |
| `phone` | phone numbers **and** ID/personnel codes (`P0059234`) | reshuffles digits, keeps format/prefix |
| `amount` | money / quantity / cost | random **±15% jitter** per cell (NOT stored in the map) |
| `url` | website URLs | swaps the registrable domain label, keeps scheme/sub-domain/TLD/path shape |
| `freetext` | notes / bodies / JSON | GLiNER scrub of embedded PII (see §6) |
| `id` | GUIDs / opaque identifiers | format-preserving hex remap; deterministic; NOT stored in the map |
| *(omit / disable)* | non-PII (flags, categories, dates, roles) | left unchanged |

---

## 6. How free-text (`freetext`) columns are scrubbed

`scrub_text` runs **precise passes first, GLiNER last** (order matters — it fixes an earlier bug
where GLiNER mangled email boundaries and produced inconsistent/duplicate fakes):

1. **Emails** (plain **and** URL-encoded `%40` as in Outlook SafeLinks) → canonical `engine.fake('email')`.
2. **Known-participant sweep** — a **global** pre-pass harvests every name/email in the table
   (from structured person/email columns + `"name"`/`"address"` JSON fields), and each free-text cell
   is literal-swept so participants are masked even where GLiNER misses them (large/messy HTML/JSON),
   and even when a name is only *quoted* in another row.
3. **GLiNER** (`urchade/gliner_multi_pii-v1`) for any remaining entities — **skips** anything already
   handled (e.g. `@`-containing spans, already-inserted participant fakes) so it never re-fakes a fake.
4. **Forced words** (`centific`/`pactera`) across the whole string, including glued/encoded forms.

---

## 7. Safety guards built into the engine

- **Bijectivity at scale:** if the base fake space saturates (e.g. thousands of single-word orgs vs
  the finite `ORG_HEAD×ORG_TAIL` grid), `_uniq` falls back to an **unbounded clean extension**
  (appends brandable words / an extra email token) — guaranteeing a unique, digit-free fake no matter
  how large the dataset grows. (Verified: 5,000 new orgs → 0 collisions.)
- **No fed-back fakes:** any value already carrying a reserved fake word (`aventraa`/`eventraa`) is
  returned unchanged — so re-running on already-anonymized data can't re-fake and pollute the map.
- **Amount precision:** `jitter_amount` uses a wide local decimal context so large values
  (e.g. 12-digit revenue) are jittered correctly (not silently skipped).
- **Foreign-row guard:** `run` refuses to write into a `_clean` table that has rows it didn't create,
  unless `--restart` (truncate) is passed.
- **Checkpoint/resume:** progress is saved after each committed batch (`_state/<table>.ckpt.json`);
  Ctrl-C pauses cleanly; re-running the same command resumes.

---

## 8. Files in this folder

| file | purpose |
|---|---|
| `obi_anonymizer.py` | the tool — `analyze` / `run` / `verify` commands |
| `set_plan.py` | set exactly which columns+types are enabled in a table's plan (avoids hand-editing JSON) |
| `repair_collisions.py` | surgical fix for collided fakes in the map + `_clean` (keep canonical, regenerate others) |
| `cleanup_fedback.py` | remove fed-back-fake pollution from the map (fakes that leaked into `originalvalue`) |
| `_state/<table>.plan.json` | per-table column plan (written by `analyze`, edited by `set_plan`) |
| `_state/<table>.ckpt.json` | per-table resume checkpoint |

---

## 9. Step-by-step execution (per table)

Set `OBI_ANON_CS` first (§3). Table arg is the **source** name, i.e. `<table>_anonymized`.

```bash
cd anonymizer

# 1) ANALYZE (read-only) — detects candidate PII columns, writes _state/<t>.plan.json
python obi_anonymizer.py analyze dyncrm_account_anonymized --sample-rows 200

# 2) SET THE PLAN — choose exactly the columns + types to anonymize
#    (enables these, disables the rest). Format: COLUMN:type ...
python set_plan.py dyncrm_account_anonymized \
    name:org new_accountnamebybu:org \
    address1_primarycontactname:person emailaddress1:email revenue:amount
#    (If a column you want isn't in the plan, add it — analyze only lists candidates.)

# 3) DRY-RUN a small sample — writes to throwaway <t>_script + <t>_script_map,
#    NEVER touches the real _clean table or the mapping table.
python obi_anonymizer.py run dyncrm_account_anonymized --limit 50 --dry-run --order-col accountid
#    -> review <t>_script for quality / leaks.

# 4) REAL RUN — writes <table>_clean, inserts new pairs into the mapping table.
python obi_anonymizer.py run dyncrm_account_anonymized --limit all --out-suffix _clean --order-col accountid

# 5) VERIFY — residual leaks per column + collision/no-op check across ALL rows.
python obi_anonymizer.py verify dyncrm_account_anonymized --out-suffix _clean --order-col accountid
```

**Key flags:**
- `--out-suffix _clean` — output table suffix (source is `*_anonymized`, so we use `_clean`). Default is `_anonymized`; always pass `_clean` for the slice.
- `--order-col <col>` — a **unique, un-anonymized** column for stable read/resume **and** exact per-row verify. If none exists, the tool orders by a composite and verify falls back to a conservative column-level check.
- `--limit N` (sample) | `--limit all` (finish) | `--restart` (truncate `_clean` + redo) | `--dry-run`.
- `--columns col1,col2` — anonymize those columns **in place** (UPDATE) instead of rebuilding (used when the target already holds foreign rows).

**Execution rules:**
- **Run tables sequentially** (one real run at a time). The mapping table is shared state; concurrent
  real runs could mint the same fake for two originals → collision. (Analyses are read-only → safe in parallel.)
- **Structured tables** are fast (seconds–minutes). **Free-text tables** (GLiNER) are slow
  (~18 min / 1,000 rows); run them in the background — checkpoint/resume makes that safe.

---

## 10. What "verify" means

- `residual_rows = 0` on every column → **PASS**.
- Some residuals are **benign** and expected:
  - `amount` columns where the value is `0` (can't be jittered).
  - `url`/text placeholders (`*`, `-`, `NA`), pure-number "names" (`123`), a bare ZIP.
  - `freetext` cells with no detectable PII.
- Collision check counts distinct `originalvalue`→same fake. **Name-format variants** of the *same
  person* (`Hu,Jing` vs `Jing Hu`) show up here but are **correct** (same entity → same fake), not violations.
- Real leaks / real collisions must be 0. For free-text tables also grep the output for `centific`,
  `@centific.com`, and known real names.

---

## 11. Status (2026-07-03)

**DONE & verified (→ `<t>_clean`, 0 real leaks, 0 real collisions):**
OMLegalStaging (18), dyncrm_competitor (84), VendorInvoiceSubLine (85), VendorInvoiceLine (153),
dyncrm_account (2,008), outlook_email (194), project_stage (721), pwsheader (1,077), emp_mstr (2,033).

**Skipped (no PII):** dyncrm_applicationuser (only Microsoft platform app names + GUIDs).

**Pending:** pwsdetail (1,077, JSON), dyncrm_orders (4,740), dyncrm_systemuser (7,911),
dyncrm_contact (47,830), VNSTeamMemberStaging (26,849), dyncrm_leads (37,178),
dyncrm_opportunity (5,903), ts_mstr (needs loading).

---

## 12. Known limitations

- **Numbers inside JSON** free-text (e.g. `pwsdetail` financial blobs) are **not** jittered — only
  embedded PII *text* (names/orgs/emails) is scrubbed. Jittering JSON numerics needs custom logic.
- **Locations** (cities/countries) masked as `org` become brandable tokens (no geo-preserving faker).
- **Blobs / attachments** are out of scope for this DB tool.

---

## 13. Maintenance scripts

```bash
# Repair collided fakes (keep canonical, regenerate the rest, update map + _clean):
python repair_collisions.py            # (edit the target table list inside if needed)

# Remove fed-back-fake pollution (fakes that ended up as originalvalue):
python cleanup_fedback.py
```
