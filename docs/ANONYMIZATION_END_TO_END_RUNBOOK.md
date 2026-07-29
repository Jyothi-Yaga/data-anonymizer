# OBI — End-to-End Anonymization Runbook

**Goal:** take a new user's raw PostgreSQL `.dump` (same OBI schema) → produce an anonymized PostgreSQL `.dump` with identical schema/table-names/types, ready to hand off (e.g. SharePoint).

**Proven on:** the `kiran-revops` slice (2026-07-04). This document is the reusable playbook — when a new same-type dump arrives, follow it start to finish, in any chat.

> **Reference docs** (details live here, this runbook ties them together):
> - Connection + credentials: [VM_CONNECTION.md](VM_CONNECTION.md)
> - Load/migrate details: [slice_load/LOAD_SLICE_RUNBOOK.md](slice_load/LOAD_SLICE_RUNBOOK.md)
> - Anonymizer engine: [anonymizer/ANONYMIZER_GUIDE.md](anonymizer/ANONYMIZER_GUIDE.md), [anonymizer/ANONYMIZATION_APPROACH.md](anonymizer/ANONYMIZATION_APPROACH.md)
> - Column decisions template: [anonymizer/COLUMN_REVIEW.md](anonymizer/COLUMN_REVIEW.md)

> **Which version is this runbook?** This is the **v1** flow: raw slice `.dump` → anonymized `.dump`,
> mapping via `obi.mapping_slice`. The engine also has a **v2** mode (`--version 2`) that anonymizes
> **`obi-sql-db` tables in place** using `obi.mapping_xref` — that is a different workflow, summarized
> in **Appendix — Version 2** at the bottom (no dump is produced there). Version selection + `.env`
> credentials are documented in [anonymizer/ANONYMIZER_GUIDE.md](anonymizer/ANONYMIZER_GUIDE.md) §0.

---

## Why this is more than "just re-dump"

`pg_restore` (loading) is one command because it replays a pre-built file. Producing an anonymized dump is harder because the anonymization happens in **SQL Server** (the VM's GPU pipeline / anonymizer engine target), but the deliverable must be a **PostgreSQL** dump. So the data has to cross the DB boundary twice:

```
raw PG .dump ──restore──▶ Postgres (obi_slice_pg) ──pg_to_mssql──▶ SQL Server (obi_slice)
                                                                        │
                                                                   anonymize (engine + GLiNER)
                                                                        │
anon PG .dump ◀──pg_dump── Postgres (obi_slice_pg) ◀──reverse_load── SQL Server (<T>_anonymized)
```

The reverse leg is where type-casts and constraint checks matter (see Steps 8–10).

---

## Environment (Azure VM)

| Item | Value |
|---|---|
| VM | `obi-data-anonymize` (Ubuntu, 4× T4 GPU) — see VM_CONNECTION.md |
| Postgres container | `obi-postgres` (PG16), DB `obi_slice_pg`, schema `obi`, volume `obi_pg_data` (persistent) |
| SQL Server container | `obi-sqlserver` (2022), DB `obi_slice`, schema `obi`, volume `obi_mssql_data` (persistent) |
| Python venv | `/mnt/obi/venv/bin/python` (has `pyodbc`, `pg8000`, faker, GLiNER deps) |
| Working dir | `/mnt/obi/slice/` ; scripts in `/mnt/obi/slice/anonymizer/` ; migrator in `/mnt/obi/slice/slice_load/` |
| Env file | `/mnt/obi/slice/anonymizer/env.sh` (mode 600) → sets `OBI_ANON_CS` (local SQL Server) + `HF_HOME` |
| SA password | in `env.sh` (`OBI_ANON_CS`) — don't echo to stdout |
| PG password | inside container: `sudo docker exec obi-postgres printenv POSTGRES_PASSWORD` (capture into a var, don't print) |

**Connect** (two terminals — tunnel + ssh): see VM_CONNECTION.md §2. All long jobs are launched detached (`setsid … &`) so they survive a dropped Bastion tunnel.

> ⚠️ **Heredoc pitfall (learned the hard way):** when you do `ssh host 'cat > file' <<'EOF' … EOF`, any commands you put *after* the `EOF` terminator run **locally**, not on the VM. Always run/verify the written script in a **separate** `ssh` call.

---

## Step 0 — Reset or namespace per user

Each dataset should be isolated so it can't mix with a previous user's data. Two options:

- **Reuse (simplest):** drop and recreate the two DBs before loading:
  ```bash
  sudo docker exec obi-postgres psql -U postgres -c "DROP DATABASE IF EXISTS obi_slice_pg;"
  sudo docker exec obi-postgres psql -U postgres -c "CREATE DATABASE obi_slice_pg;"
  # SQL Server: pg_to_mssql recreates obi.* tables, but drop the DB first to be clean:
  # (from a pyodbc session on master) DROP DATABASE obi_slice;  then it's recreated in Step 3.
  ```
- **Namespace:** use per-user DB names (`obi_slice_pg_<user>`, `obi_slice_<user>`) and pass them through the commands below. Keeps multiple users side by side.

---

## Step 1 — Stage the new source dump on the VM

Copy the new `.dump` to the VM (from your laptop, through the tunnel):
```bash
scp -P <tunnel-port> -i obi-data-anonymize_key.pem \
  "C:/path/to/<newuser>.dump" azureuser@127.0.0.1:/mnt/obi/slice/<newuser>.dump
```

---

## Step 2 — Restore into Postgres

```bash
sudo docker cp /mnt/obi/slice/<newuser>.dump obi-postgres:/tmp/src.dump
sudo docker exec obi-postgres pg_restore -U postgres -d obi_slice_pg --no-owner --no-privileges /tmp/src.dump
```
Warnings about roles/extensions are harmless. **Sanity — which tables actually have data** (only these matter):
```bash
sudo docker exec -i obi-postgres psql -U postgres -d obi_slice_pg <<'SQL'
DO $$ DECLARE r record; n bigint; BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='obi' ORDER BY 1 LOOP
    EXECUTE format('SELECT count(*) FROM obi.%I', r.tablename) INTO n;
    IF n>0 THEN RAISE NOTICE '% = %', r.tablename, n; END IF;
  END LOOP; END $$;
SQL
```
> The OBI dumps carry ~149 tables but typically only ~24 hold data. Note the populated PII tables — that's your work-list for everything below.

---

## Step 3 — Migrate populated tables Postgres → SQL Server

```bash
cd /mnt/obi/slice && source anonymizer/env.sh
PGPW=$(sudo docker exec obi-postgres printenv POSTGRES_PASSWORD)
PGPORT=$(sudo docker port obi-postgres 5432 | head -1 | sed 's/.*://')
/mnt/obi/venv/bin/python slice_load/pg_to_mssql.py \
  --pg-db obi_slice_pg --pg-port "$PGPORT" --pg-pass "$PGPW" \
  --ms-db obi_slice --ms-pass "$(…SA from env…)" --dry     # preview
# drop --dry to run for real
```
Migrates non-empty `obi` tables (type-mapped), bulk-loads rows, creates empty `obi.mapping_xref`. Details: LOAD_SLICE_RUNBOOK.md. **Type flattening to remember for the reverse leg:** `uuid→uniqueidentifier`, `boolean→bit`, `timestamptz→datetimeoffset`, `json/jsonb→nvarchar(max)`, `bytea→varbinary(max)`.

---

## Step 4 — Column review + anonymize (table by table)

This is the human-in-the-loop part. **Always get column-type approval before running a table.**
```bash
cd /mnt/obi/slice/anonymizer && source env.sh          # or rely on anonymizer/.env (auto-loaded)
python obi_anonymizer.py analyze <table> --sample-rows 300     # proposes column types
# review/adjust _state/<table>.plan.json  (or use set_plan.py)
python obi_anonymizer.py run <table> --limit 100 --dry-run     # safe sample → <table>_script
python obi_anonymizer.py run <table> --limit all               # real → <table>_anonymized
python obi_anonymizer.py verify <table>
```
> This runbook uses **v1** (the default; equivalent to `--version 1`). Credentials can now come from
> `anonymizer/.env` instead of `source env.sh` — a value exported in the terminal still wins.
> Per-user slice DBs (e.g. `obi_nick_slice`) → point `OBI_ANON_CS`/`V1_DATA_CS` at that DB; each slice
> also gets its own `env_<user>.sh` on the VM.
- Column types: `person | org | email | phone | amount | url | freetext | id | skip`. Free-text bodies use GLiNER (GPU) — set mode `freetext`.
- Forced brand words are applied case-preserving (`centific→aventraa`, `pactera→eventraa`).
- **Snapshot `MAX(id)` of the map before each table**, then check new pairs + consistency after.
- Full engine behaviour + column guidance: ANONYMIZER_GUIDE.md, COLUMN_REVIEW.md.

---

## Step 5 — Verify anonymization quality (in SQL Server)

Before exporting, confirm the anonymized tables are clean:
- **Forced words:** scan every `_anonymized` text column for `centific`/`pactera` → expect 0. (`anonymizer/scan_forced.py`)
- **Map consistency:** 0 originals with >1 fake; 0 fakes shared by >1 original (ignore benign name-order variants).
- **Row counts:** each `<T>_anonymized` count == original `<T>` count.

---

## Step 6 — Decide the deliverable scope

Inspect the full table list; OBI source dumps carry **anonymization-scratch/backup tables** from prior projects (all usually empty): `anonymized_map*`, `mapping_slice`, `mapping_xref*`, `_cr_*`, `_canonical_map`, `_reused_fakes`, `anon_canonical`, `*_script`, `*_script_map`, `*_anonymized` (prior run), `*_anonymized_anonymized`, dated `*_bkp/backup/original`.

**Confirm they're empty** (no PII leak) then choose scope with the user:
- **Drop artifacts only (recommended):** remove the scratch/backup tables, keep all legitimate schema tables (populated + empty real ones like `channel_messages`, `users`, `jira_*`).
- **Exact mirror:** keep all ~149 tables.
- **Minimal:** only the populated tables.

**Drop artifacts safely (Postgres, atomic, aborts if any match has rows):**
```bash
sudo docker exec -i obi-postgres psql -U postgres -d obi_slice_pg -v ON_ERROR_STOP=1 <<'SQL'
DO $$ DECLARE r record; n bigint; d int:=0; k int:=0; BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='obi' ORDER BY 1 LOOP
    IF r.tablename ~* '(_anonymized|_anonymised|_script|_script_map|_original)$'
       OR r.tablename ~* '^(anon|mapping_|_cr_|_align_remap|_cleanup_remap|_consistency_remap|_canonical_map|_reused_fakes)'
       OR r.tablename ~* '(bkp|backup)' THEN
       EXECUTE format('SELECT count(*) FROM obi.%I', r.tablename) INTO n;
       IF n>0 THEN RAISE EXCEPTION 'ABORT: % has % rows', r.tablename, n; END IF;
       EXECUTE format('DROP TABLE obi.%I CASCADE', r.tablename); d:=d+1;
    ELSE k:=k+1; END IF;
  END LOOP; RAISE NOTICE 'DROPPED %  KEPT %', d, k; END $$;
SQL
```

---

## Step 7 — Constraint pre-check (avoid mid-reload failures)

The SQL Server `_anonymized` tables have **no constraints**, but the Postgres schema has **PKs and unique indexes**. If anonymization altered a key column non-bijectively, two rows collide and the reload fails. **Check before reloading:**

1. Get PK columns per table (Postgres `information_schema.table_constraints` + `key_column_usage`).
2. Get non-PK unique indexes (`pg_index` where `indisunique AND NOT indisprimary`).
3. For each, count duplicate key-groups in the SQL Server `<T>_anonymized`:
   ```sql
   SELECT count(*) FROM (SELECT <pkcols> FROM obi.<T>_anonymized GROUP BY <pkcols> HAVING count(*)>1) q;
   ```
   (See `anonymizer/diag_pk.py` for the automated version.)

**If a key column collided** (e.g. kiran's `OMLegalStaging.LEGALENTITYID`: two codes → same fake): fix per the user's choice —
- **Bijective fix (recommended):** reassign one colliding row a fresh unique fake:
  `UPDATE TOP(1) obi.<T>_anonymized SET <col>='<fresh>' WHERE <col>='<dup>';`
- or un-anonymize the key column, or drop that PK in the dump.
Re-check until 0 dup-groups.

---

## Step 8 — Reverse-load SQL Server → Postgres

Script: **`/mnt/obi/slice/anonymizer/reverse_load.py`**. It truncates each target Postgres table and reloads the anonymized rows from SQL Server, **preserving the original PG types**:
- casts `uuid` → `::uuid`, `json/jsonb` → `::json/::jsonb`; converts `bit`→`bool`; passes `bytea` bytes;
- registers a **pyodbc output converter for `datetimeoffset` (SQL type −155)** → tz-aware datetime for `timestamptz`;
- sets `session_replication_role='replica'` to disable FK triggers, `TRUNCATE … CASCADE`, then batched multi-row INSERT (batch sized to stay under Postgres' 65535-param limit);
- verifies each table's PG vs SQL Server row count.

> **Reusability:** the script's table list is now **env-overridable** — set `OBI_RL_TABLES=<csv>` to the new dataset's work-list from Step 2 (no code edit). It falls back to a hardcoded `_DEFAULT_TABLES` (kiran's list) if the var is unset. Also honors `PGDB` (target Postgres DB, e.g. `obi_nick_slice`). Exclude tables with no PII / no `_anonymized` copy — those keep their raw rows, e.g. kiran's `dyncrm_applicationuser`.
>
> ```bash
> export OBI_RL_TABLES="dyncrm_account,dyncrm_activity,dyncrm_contact,dyncrm_leads,dyncrm_opportunity,dyncrm_systemuser,outlook_email,sharepoint_files,sharepoint_sites,teams_chat"
> export PGDB=obi_nick_slice
> ```

Run it:
```bash
cd /mnt/obi/slice/anonymizer && source env.sh
export PGHOST=localhost
export PGPORT=$(sudo docker port obi-postgres 5432 | head -1 | sed 's/.*://')
export PGPW=$(sudo docker exec obi-postgres printenv POSTGRES_PASSWORD)
setsid /mnt/obi/venv/bin/python reverse_load.py > reverse_load.log 2>&1 < /dev/null &
# poll:  tail -f reverse_load.log   → expect "ALL DONE CLEAN"
```

---

## Step 9 — Produce the PostgreSQL dump

```bash
sudo docker exec obi-postgres pg_dump -U postgres -Fc obi_slice_pg -f /tmp/<newuser>_anonymized.dump
sudo docker cp obi-postgres:/tmp/<newuser>_anonymized.dump /mnt/obi/slice/<newuser>_anonymized.dump
# verify TABLE DATA entry count:
sudo docker exec obi-postgres pg_restore -l /tmp/<newuser>_anonymized.dump | grep -c "TABLE DATA"
```
`-Fc` = custom format, identical to the source dump; restore with `pg_restore -d <db> <file>`.

**Quick PII sanity in Postgres before shipping:** sample a known email column (should be fake `@…` domain) and `count(*) WHERE name ILIKE '%centific%' OR '%pactera%'` → 0.

---

## Step 10 — Download + verify

```bash
scp -P <tunnel-port> -i obi-data-anonymize_key.pem \
  azureuser@127.0.0.1:/mnt/obi/slice/<newuser>_anonymized.dump "C:/Users/.../Desktop/OBI/"
certutil -hashfile <newuser>_anonymized.dump SHA256      # compare against VM: sha256sum on VM
```
> **Size display note:** a dump of `N` bytes shows as `N/1e6` MB (decimal) locally but `N/2^20` MB (binary/MiB) on SharePoint/Explorer — e.g. 34,759,073 bytes = 34.8 MB decimal = 33.1 MB binary. **Same file.** Confirm integrity by SHA-256, not by the displayed size.

---

## Step 11 — Teardown / cost

- Deliverable is downloaded → you may stop work.
- `docker stop obi-postgres obi-sqlserver` — data persists (named volumes). **Does not save VM cost.**
- To save cost: **deallocate the VM** (Azure Portal → Stop). Disk (dumps, volumes, scripts) persists; restart when the next dataset arrives.
- Do **not** `docker rm -v` or `docker volume rm` unless you intend to wipe the DBs.

---

## Appendix — gotchas learned

| Gotcha | Fix |
|---|---|
| Commands after an `ssh 'cat>file' <<EOF … EOF` run **locally** | Write the file in one `ssh`; run/verify in a **separate** `ssh` |
| `pyodbc` can't decode `datetimeoffset` | register output converter for SQL type **−155** (struct `<6hI2h`) |
| Reload FK-order / TRUNCATE on referenced table fails | `SET session_replication_role='replica'` + `TRUNCATE … CASCADE` |
| Postgres INSERT param limit (65535) | size the multi-row batch = `50000 // ncols` |
| SQL Server has no PKs → collisions invisible until reload | Step 7 constraint pre-check before the long reload |
| PK column anonymized non-bijectively (fake collision) | bijective re-map of one colliding row (Step 7) |
| Source dump carries empty scratch/mapping tables | drop-artifacts pass (Step 6), with empty-guard |
| Credentials in `LIKE '%…%'` / heredocs get mangled or leaked | parameterize; read secrets into vars, never `echo` them |
| MB vs MiB size mismatch | expected; verify by SHA-256 |
| `reverse_load.py` TABLES hardcoded per dataset | now `OBI_RL_TABLES=<csv>` env override (+ `PGDB`); default falls back to kiran's list |

---

## Appendix — Version 2 (anonymize `obi-sql-db` tables in place)

**Different goal from the main runbook:** v2 anonymizes tables **directly in Azure `obi-sql-db`**
(no slice, no `.dump`), using **`obi.mapping_xref`** as the mapping source of truth. Use it when you
just need `<table>_anonymized` copies inside `obi-sql-db`, or to add columns to copies that already
exist there from earlier work. Full engine detail: [anonymizer/ANONYMIZER_GUIDE.md](anonymizer/ANONYMIZER_GUIDE.md) §0.

**Setup (once):** copy [anonymizer/.env.example](anonymizer/.env.example) → `anonymizer/.env`, fill
`V2_DATA_CS` (and optionally `V2_MAP_CS`; defaults to `V2_DATA_CS`), `chmod 600 .env`.

**Per table** (you provide the table + columns; record them in
[anonymizer/COLUMN_REVIEW_V2.md](anonymizer/COLUMN_REVIEW_V2.md)):
```bash
cd /mnt/obi/slice/anonymizer
python obi_anonymizer.py analyze <table> --version 2 --sample-rows 300
#   edit _state_v2/<table>.plan.json — enable ONLY the columns for this run, fix each type
python obi_anonymizer.py run <table> --version 2 --limit 100 --dry-run   # preview -> <table>_script
python obi_anonymizer.py run <table> --version 2 --limit all             # see below
python obi_anonymizer.py verify <table> --version 2 --order-col <unique id>
```

**Behaviour of the real run (v2):**
- If `<table>_anonymized` **already exists with rows** → **UPDATE-IN-PLACE**: only the enabled columns
  are anonymized on that existing copy (keyed on PK/unique id); other columns and rows are untouched.
  Separate checkpoint `_state_v2/<table>.inplace.ckpt.json`. `--restart` forces a fresh rebuild instead.
- If it **doesn't exist** → built fresh from the source table (like v1's INSERT flow).
- New original→fake pairs are written to `obi.mapping_xref` (reuse-first, consistent across runs).

**Steps 5–10 (verify quality, scope, reverse-load, dump, download) do NOT apply to v2** — there is no
PG round-trip and no `.dump`; the deliverable is the `_anonymized` table(s) inside `obi-sql-db`.
