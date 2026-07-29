# Environment Setup — Python, ODBC Driver, and Dependency Tiers

How to get `obi_anonymizer.py` running from nothing: system prerequisites, the virtual
environment(s), and the three dependency tiers (matching the tool's own lazy-import design —
see `requirements.txt`). Verified on Ubuntu 24.04 LTS, Python 3.12.

---

## 1. Why there are (potentially) *two* venvs

`obi_anonymizer.py` imports everything below with a `try/except` and degrades gracefully if a
package is missing — so the tool never crashes on a bare install. But "degrades gracefully" for
GLiNER specifically means: **every `freetext`-typed column silently skips PII detection and
leaves the text essentially raw.** So in practice:

- **Tier 1 (core)** — always required, tiny, no GPU. `pyodbc`, `pyahocorasick`.
- **Tier 2 (GLiNER / free-text detection)** — required for real, working `freetext` coverage.
  Pulls in PyTorch, which is large (~3-4GB with CUDA libs) and platform/GPU-specific.
- **Tier 3 (higher-fidelity fakes)** — optional, all graceful-degrade, but you want these for the
  ethnicity/gender-aware, large-pool fake generation the tool is actually designed around.

If your machine has plenty of disk, install all three tiers into **one** venv and skip to
§4. If disk is tight (see §6), Tier 2 is the one worth splitting into its own venv, since it's
the only large/finicky one.

---

## 2. System prerequisites

- **Python 3.12+** (`python3 --version`). No 3.14-specific behavior is relied on despite what an
  older doc in this folder says — 3.12 is what's actually verified working.
- **ODBC Driver 17 for SQL Server** (system package, not pip). On Ubuntu/Debian:
  ```bash
  curl https://packages.microsoft.com/keys/microsoft.asc | sudo tee /etc/apt/trusted.gpg.d/microsoft.asc
  curl https://packages.microsoft.com/config/ubuntu/24.04/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list
  sudo apt-get update
  sudo ACCEPT_EULA=Y apt-get install -y msodbcsql17 unixodbc-dev
  ```
  Verify: `odbcinst -q -d` should list `[ODBC Driver 17 for SQL Server]`.
- **A GPU is optional.** GLiNER runs on CPU fine, just slower (fine for `analyze`/small samples;
  budget more time for a full `run`/`verify` on a large freetext-heavy table).

---

## 3. Create the venv(s)

```bash
cd data-slice-anonymization
python3 -m venv .venv
source .venv/bin/activate
```

If you're splitting Tier 2 into its own venv instead (see §1/§6):

```bash
python3 -m venv .venv-gliner
```

---

## 4. Install the dependencies

**Tier 1 — core (always):**
```bash
pip install pyodbc==5.3.0 pyahocorasick==2.3.1
```

**Tier 2 — GLiNER / free-text detection:**
```bash
# torch is GPU/CUDA-build-specific and NOT resolvable from the plain PyPI index -- pick the
# index matching your NVIDIA driver. Check with `nvidia-smi` first.
pip install torch --index-url https://download.pytorch.org/whl/cu121   # CUDA >= 12.1
# No GPU / unsure -> plain CPU build instead:
#   pip install torch
pip install gliner==0.2.27
```
First run of any `freetext` column downloads the model (`urchade/gliner_multi_pii-v1`, a few
hundred MB) to the HuggingFace cache. Point it somewhere with room via `.env`:
```
HF_HOME=/path/with/space/hf
```

**Tier 3 — higher-fidelity fakes (recommended, all optional):**
```bash
pip install Faker==40.31.0 names-dataset==3.3.1 gender-guesser==0.4.0 \
            pypinyin==0.55.0 ethnicseer==0.1.2
```

Or, all at once from the pinned list (skip/comment the `torch`/`gliner` lines first if you're
deliberately doing a Tier-1-only or split-venv install — see `requirements.txt` for the full
annotated version):
```bash
pip install -r requirements.txt
```

---

## 5. Configure `.env`

```bash
cp .env.example .env
chmod 600 .env
```
Fill in real values for whichever of `V1_DATA_CS` / `V1_MAP_CS` / `V2_DATA_CS` you need (see the
comments in `.env.example` for what each connects to, and `docs/ANONYMIZER_GUIDE.md` §0/§4 for the
v1/v2 distinction). `V1_XREF` / `V2_XREF` control which shared mapping table each version reuses
against — leave at their defaults unless you have a specific reason to point v1 at `mapping_xref`.

**Never commit the real `.env`** — it holds credentials. `.gitignore` already excludes it.

---

## 6. Disk-space troubleshooting

Tier 2 (`torch` + CUDA libraries) needs ~3-4GB free. On a disk-constrained box this can fail
outright with `No space left on device` even after clearing pip/apt/journal caches. If that
happens:
```bash
df -h /                     # confirm what's actually free
pip cache purge
sudo apt-get clean
sudo journalctl --vacuum-size=200M
```
If it's still too tight, keep Tier 1 + Tier 3 in your main `.venv` and either (a) build Tier 2 in
a separate venv on a machine/volume with more room, or (b) check whether a teammate already has a
working GLiNER install you can point the interpreter at — reuse it read-only, never write into
someone else's venv or repo.

---

## 7. Verify the install

Import checks (no DB, no credentials needed):
```bash
python -c "import pyodbc, ahocorasick; print('tier 1 ok')"
python -c "import torch, gliner; print('tier 2 ok — cuda:', torch.cuda.is_available())"
python -c "from faker import Faker; import names_dataset, gender_guesser, pypinyin, ethnicseer; print('tier 3 ok')"
```

A real, zero-risk CLI smoke test — `status` and `plan` return before any DB connection is opened
(pure local state-file reads), so this works even with `.env` unfilled:
```bash
python obi_anonymizer.py status any_table_name
```
Expect a `[version 1] data=... mapping=...` line (confirms `.env`/version resolution) followed by
`plan: no` / `checkpoint: none` (expected for a table you haven't analyzed yet).

---

## 8. Which interpreter for which command

| Command | Needs Tier 2 (GLiNER)? |
|---|---|
| `analyze`, `plan`, `status`, `reset` | No |
| `run` / `verify` — no `freetext`-typed column enabled | No |
| `run` / `verify` — any `freetext`-typed column enabled | **Yes** |

If you went the split-venv route (§1/§6), use the Tier-1-only venv for the "No" rows and the
Tier-2 venv only when a plan has an enabled `freetext` column.
