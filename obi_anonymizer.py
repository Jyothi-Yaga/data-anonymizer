#!/usr/bin/env python3
"""
obi_anonymizer.py — from-scratch, single-table, interactive-plan anonymizer for obi.*
=====================================================================================
Built fresh (does NOT reuse _canonical_map or any of the earlier 18-table helper
tables). It only *references* the anonymization LOGIC proven on obi.outlook_email:
GLiNER PII detection + deterministic, ethnicity/gender-aware, injective fake
generation.

MAPPING SOURCE OF TRUTH (read-only): obi.mapping_xref
    (id, description[type], originalvalue, anonymizedvalue, comment)
    description ∈ {Names, Email, CompanyName, Name}
  * If an original already has a mapping there -> that fake is reused (consistency).
  * We NEVER insert into mapping_xref and NEVER create any helper table.
  * Values with no existing mapping get a DETERMINISTIC fake (seeded hash), so the
    same original always yields the same fake across rows/tables without any state.
    (Existing xref pairs are reversible via the table; generated fallbacks are
    one-way pseudonyms.)

WORKFLOW (plan-file based, safe for detached VM runs)
  1. analyze : inspect the table, detect PII columns, write <table>.plan.json + report
  2. (you)   : edit <table>.plan.json — enable/disable columns, fix the type
  3. run     : create <table>_anonymized, anonymize ONLY enabled columns.
               --limit N   -> process first N rows (sample to eyeball)
               --limit all -> process everything (resumes from checkpoint)
  4. verify  : row-count + residual raw-PII check + sample original->fake pairs

RECORDS PARAMETER & PAUSE/RESUME
  * --limit <N|all|complete>  controls how many rows this invocation processes.
  * Progress is checkpointed after every committed batch in <table>.ckpt.json.
  * Ctrl-C pauses cleanly (finishes the current batch, saves checkpoint, exits).
  * Re-running `run` resumes from the checkpoint automatically (or --restart to redo).

USAGE
  PY=/mnt/obi/venv/bin/python   # on the VM (has gliner, faker, pyodbc, pypinyin,…)
  $PY obi_anonymizer.py analyze  <table> [--sample-rows 500]
  $PY obi_anonymizer.py plan     <table>            # reprint the current plan
  $PY obi_anonymizer.py run      <table> --limit 100 --dry-run   # SAFE trial: writes to
  #     <table>_script only, does NOT touch mapping_xref (won't affect teammates)
  $PY obi_anonymizer.py run      <table> --limit 100    # real sample -> <table>_anonymized
  $PY obi_anonymizer.py run      <table> --limit all
  $PY obi_anonymizer.py status   <table>
  $PY obi_anonymizer.py verify   <table> [--sample 20]
  $PY obi_anonymizer.py reset    <table> [--restart]   # clear checkpoint (+truncate)

State files live next to this script under ./_state/<table>.{plan,ckpt}.json
"""
import argparse, calendar, decimal, hashlib, html, json, os, random, re, signal, struct, sys, time, datetime, unicodedata
import obi_chinese_anonymizer as chinese_anon
from constants import (RESUME_DOMAIN_TABLES, table_domain, GENERIC_ENTITY_STOP,
                        KNOWN_BRAND_STOP, _PRONOUN_STOP, FORCED_MAP, MANUAL_COMPANY_MAP)

# Windows consoles default to cp1252 and crash printing non-ASCII (Chinese names, ₹, …).
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# ── .env loader ──────────────────────────────────────────────────────────────────
# Load KEY=VALUE pairs from a `.env` next to this script into os.environ, WITHOUT
# overriding anything already set in the real environment (a value exported in the
# terminal always wins, so nothing that works today breaks). Keeps per-phase DB
# credentials in one file per machine instead of being re-exported each session.
# No external dependency. Lines starting with '#' and blank lines are ignored;
# surrounding single/double quotes on the value are stripped.
_BASE = os.path.dirname(os.path.abspath(__file__))
def _load_dotenv(path=None):
    path = path or os.path.join(_BASE, '.env')
    try:
        if not os.path.exists(path):
            return
        with open(path, encoding='utf-8') as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, _, v = line.partition('=')
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass
_load_dotenv()

# ── DB config ──────────────────────────────────────────────────────────────────
# Override with the OBI_ANON_CS env var to point at a different server (e.g. a local
# SQL Server / Docker holding a migrated slice) without editing this file.
# DATA connection — the tables being anonymized (e.g. local SQL Server holding the slice).
CS = os.environ.get('OBI_ANON_CS') or (
      'DRIVER={ODBC Driver 18 for SQL Server};'
      'SERVER=obi-poc-server.database.windows.net;DATABASE=obi-sql-db;'
      'UID=obi_admin;PWD=__SET_VIA_ENV__;Encrypt=yes;TrustServerCertificate=no;')
# MAPPING connection — the SHARED single source of truth `obi.mapping_slice` on Azure obi-sql-db.
# Data may be local while the mapping is remote/shared; override with OBI_MAP_CS if needed.
MAP_CS = os.environ.get('OBI_MAP_CS') or (
      'DRIVER={ODBC Driver 18 for SQL Server};'
      'SERVER=obi-poc-server.database.windows.net;DATABASE=obi-sql-db;'
      'UID=obi_admin;PWD=__SET_VIA_ENV__;Encrypt=yes;TrustServerCertificate=no;')
SCHEMA = 'obi'                                # DATA schema -- overridable via --schema (e.g. obip1)
MAP_SCHEMA = 'obi'                            # MAPPING table's schema -- always 'obi', never overridden;
                                               # the shared mapping_xref/mapping_slice table lives here
                                               # regardless of which schema the data being anonymized is in.
XREF   = 'mapping_slice'                      # shared mapping source of truth (Azure obi-sql-db)

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_state')

# Import-time resolutions kept as ultimate fallbacks for _apply_version (so we do not
# repeat the credential literal a third time; MAP_CS default already points at obi-sql-db).
_DATA_CS_DEFAULT = CS
_MAP_CS_DEFAULT  = MAP_CS

# ── logic version ────────────────────────────────────────────────────────────────
# VERSION 1 (default, present behaviour): DATA = OBI_ANON_CS (a migrated slice),
#   MAPPING = obi.mapping_slice (shared Azure obi-sql-db). Untouched.
# VERSION 2 (new): DATA = obi-sql-db tables directly, MAPPING = obi.mapping_xref,
#   its own _state_v2 dir (so same-named tables' plans/checkpoints never collide with v1),
#   and run() auto-updates an already-existing <table>_anonymized IN PLACE (only the
#   enabled columns) instead of rebuilding it.
VERSION = 1

def _apply_version(v):
    """Resolve DATA conn (CS), MAPPING conn (MAP_CS), mapping-table name (XREF) and the
    state dir for the selected logic version. All are module globals read at call-time,
    so reassigning them here — before any connect()/_paths() — switches behaviour without
    touching the engine. Credentials come from the environment, which .env has populated."""
    global CS, MAP_CS, XREF, STATE_DIR, VERSION
    VERSION = int(v)
    if VERSION == 2:
        CS     = os.environ.get('V2_DATA_CS') or os.environ.get('OBI_V2_DATA_CS') or _MAP_CS_DEFAULT
        MAP_CS = os.environ.get('V2_MAP_CS')  or os.environ.get('OBI_V2_MAP_CS')  or CS
        XREF   = os.environ.get('V2_XREF', 'mapping_xref')
        STATE_DIR = os.path.join(_BASE, '_state_v2')
    else:
        CS     = os.environ.get('OBI_ANON_CS') or os.environ.get('V1_DATA_CS') or _DATA_CS_DEFAULT
        MAP_CS = os.environ.get('OBI_MAP_CS')  or os.environ.get('V1_MAP_CS')  or _MAP_CS_DEFAULT
        XREF   = os.environ.get('V1_XREF', 'mapping_slice')
        STATE_DIR = os.path.join(_BASE, '_state')

def _cs_dbname(cs):
    """Extract just DATABASE=... from a connection string for safe logging (no secret)."""
    m = re.search(r'DATABASE=([^;]+)', cs or '', re.I)
    return m.group(1) if m else '?'
BATCH      = 2000                             # rows per committed batch
SAMPLE_ROWS_DEFAULT = 500                     # rows sampled during analyze
MAX_LOOKUP_LEN = 256                          # xref.originalvalue is nvarchar(256) (Unicode-safe)

# xref type label  ->  our internal PII type   (label written into mapping_xref.description)
XREF_TYPE = {'person': 'Names', 'email': 'Email', 'org': 'CompanyName',
             'name': 'Names', 'phone': 'Phone',
             'country': 'country', 'location': 'location', 'region': 'Region'}

# ── #5 country / location fake pools — map to a DIFFERENT real place (localized, real value,
# not structural garbage). Deterministic & consistent: same input always -> same output.
COUNTRIES = [
    ('China', 'CN', 'CHN'),      ('India', 'IN', 'IND'),      ('Japan', 'JP', 'JPN'),
    ('Germany', 'DE', 'DEU'),    ('France', 'FR', 'FRA'),     ('Brazil', 'BR', 'BRA'),
    ('Canada', 'CA', 'CAN'),     ('Australia', 'AU', 'AUS'),  ('Spain', 'ES', 'ESP'),
    ('Italy', 'IT', 'ITA'),      ('Mexico', 'MX', 'MEX'),     ('Egypt', 'EG', 'EGY'),
    ('Kenya', 'KE', 'KEN'),      ('Norway', 'NO', 'NOR'),     ('Poland', 'PL', 'POL'),
    ('Turkey', 'TR', 'TUR'),     ('Vietnam', 'VN', 'VNM'),    ('Chile', 'CL', 'CHL'),
    ('Sweden', 'SE', 'SWE'),     ('Portugal', 'PT', 'PRT'),
]
CITIES = ['Riverton', 'Lakewood', 'Fairview', 'Kingsport', 'Brookfield', 'Ashford',
          'Greenville', 'Westbrook', 'Millbrook', 'Oakdale', 'Cedarville', 'Elmwood',
          'Bridgeport', 'Clearwater', 'Newport', 'Silverton', 'Glenwood', 'Fairmont',
          'Sunnyvale', 'Maplewood']

# ── FORCED substitution rule ─────────────────────────────────────────────────────
# Hard rule: wherever these words appear (as whole words, any casing, in any column
# or inside free text), the fake MUST be the mapped word — case-preserving.
#   centific -> aventraa   ·   pactera -> eventraa
# FORCED_MAP now lives in constants.py (imported above) -- extend it there.
# A value that already carries one of our reserved forced-fake words is ALREADY anonymized.
# The engine refuses to re-fake it (which would feed a fake back in as an "original" and pollute
# mapping_xref) — a safety net for accidentally running on an already-anonymized source.
_RESERVED_FAKE_RE = re.compile('|'.join(re.escape(v) for v in FORCED_MAP.values()), re.I)

# Org tokens kept verbatim (legal suffixes + generic fillers) so only the
# distinctive core is faked:  "Centific PVT LTD" -> "Aventraa PVT LTD".
LEGAL_SUFFIX = {'pvt','ltd','limited','inc','incorporated','llc','llp','lp','gmbh','plc',
                'corp','corporation','co','company','group','holding','holdings','sa','ag',
                'bv','nv','srl','spa','pte','sarl','kg','oy','ab','as','pty','kk','inc.',
                'ltd.','co.','llc.','gmbh.','plc.','pvt.','s.r.l.','s.a.','n.v.'}
FILLER = {'the','and','of','&','global','international','intl','technologies','technology',
          'solutions','services','systems','consulting','labs','ventures'}

# Brandable company words (deliberately NOT surnames, so org fakes never look like a person)
# and org descriptors. Used to build company-style fakes: e.g. "Vertex Systems".
ORG_HEAD = ['Vertex','Nimbus','Quantic','Cobalt','Meridian','Aperture','Solstice','Vantage',
            'Beacon','Cascade','Ironwood','Brightwave','Cedarpoint','Blueridge','Kestrel',
            'Lumen','Zenith','Pinnacle','Northgate','Granite','Harborview','Evercrest',
            'Falconix','Onyx','Sapphire','Titan','Vanguard','Emberline','Aurora','Nexus',
            'Vireo','Talos','Helios','Arcadia','Boreal','Cirrus','Delphi','Equinox','Fathom',
            'Grovewood','Halcyon','Indigo','Juniper','Kinetic','Lattice','Monarch','Novena',
            'Obsidian','Polaris','Quill','Radian','Sable','Tessera','Umbra','Verdant',
            'Wrenfield','Xenon','Yonder','Zephyr','Everline','Cypress','Skyline','Summitry']
ORG_TAIL = ['Systems','Technologies','Analytics','Solutions','Labs','Networks','Dynamics',
            'Digital','Cloud','Data','Ventures','Industries','Global','Group','Holdings',
            'Works','Collective','Partners']

# Public/free e-mail providers — NOT company-identifying, so their domain is KEPT as-is
# (only the local part is masked). Everything else is a company domain and gets masked.
PUBLIC_EMAIL_DOMAINS = {
    'gmail.com','googlemail.com','yahoo.com','yahoo.co.uk','yahoo.co.in','yahoo.fr','yahoo.de',
    'ymail.com','rocketmail.com','hotmail.com','hotmail.co.uk','hotmail.fr','outlook.com',
    'outlook.co.in','live.com','live.co.uk','msn.com','aol.com','icloud.com','me.com','mac.com',
    'protonmail.com','proton.me','gmx.com','gmx.net','gmx.de','yandex.com','yandex.ru','mail.ru',
    'zoho.com','qq.com','163.com','126.com','sina.com','sohu.com','foxmail.com','mail.com',
    'rediffmail.com','comcast.net','att.net','verizon.net','sbcglobal.net','bellsouth.net',
    'cox.net','earthlink.net','fastmail.com','hey.com','pm.me','tutanota.com','naver.com',
    'daum.net','hanmail.net','web.de','t-online.de','orange.fr','free.fr','laposte.net'}

def _wordkey(tok):
    return re.sub(r'[^\w.]', '', tok).lower().strip('.')

def _person_tokens(s):
    """(first_tok, last_tok) for a 2+-token person name, honoring 'Last, First' order; None
    if it doesn't resolve to a distinguishable first/last pair. Shared by the email name_hint
    lookup so a companion email in the same row can be told which token is the first name vs
    surname, instead of guessing from the email's own segment order (some orgs use
    lastname.firstname@...)."""
    s = (s or '').strip()
    m = re.match(r"^\s*([A-Za-z\-']+)\s*,\s*([A-Za-z\-']+)\s*$", s)
    if m: s = f"{m.group(2)} {m.group(1)}"
    toks = [t for t in re.split(r'\s+', s) if t]
    if len(toks) < 2: return None
    return toks[0], toks[-1]

def case_like(orig_tok, fake_tok):
    """Make fake_tok match orig_tok's case pattern (UPPER / lower / Title)."""
    if not fake_tok: return fake_tok
    core = re.sub(r'[^A-Za-z]', '', orig_tok)
    if core and core.isupper():   return fake_tok.upper()
    if core and core.islower():   return fake_tok.lower()
    if core and core[:1].isupper():        # Title: capitalize each alpha run
        return re.sub(r'[A-Za-z]+', lambda m: m.group(0).capitalize(), fake_tok)
    return fake_tok

def fit_width(v, ml):
    """#2 length-aware clamp for narrow columns. The old code did a blind v[:ml], which could
    slice a fake mid-token ('Bridgeport Systems'[:12] -> 'Bridgeport S'). Instead prefer the
    last whitespace boundary at or before the limit so the result is a whole token; fall back to
    a hard cut only when the very first token is itself longer than the column. Note: ID and URL
    fakes are format-preserving (same length as the original), so they never reach this path."""
    if not (isinstance(v, str) and ml and ml > 0 and len(v) > ml):
        return v
    head = v[:ml]
    sp = head.rfind(' ')
    return head[:sp].rstrip() if sp > 0 else head

def _shift_ne(base, span, r, ch):
    """Pick a letter in [base, base+span) deterministically from r, but NEVER the same letter as
    ch (case-insensitively). Used by _gen_guid so a letter-class code never has a real chance of
    reproducing its own original character -- e.g. personnel codes like 'P0000004' always fake to
    a DIFFERENT leading letter, never coincidentally back to 'P' (which would make the fake
    indistinguishable in shape from some other, real 'P#######' id it isn't actually reversible
    to). Deterministic: same (base, span, r, ch) always -> same output."""
    v = r % span
    if chr(base + v).lower() == ch.lower():
        v = (v + 1) % span
    return chr(base + v)

_DIRTY_RE = re.compile(r'(^FakeCompany[_]|^FakeOrg[_]|[_][0-9a-fA-F]{8}$|[_]\d{3,}$)')

# Opaque identifiers (GUIDs/UUIDs, Atlassian-style "712020:ec6a...", bare 24-hex Mongo ids)
# have no name/org shape — running them through the person/org generator yields nonsense.
# looks_like_id() is used by analyze() (to type such columns) and by fake() (a runtime guard
# that overrides the declared type), so it's caught regardless of column-name heuristics.
_GUID_DASHED = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
# personnel/employee code shape (P0091302, C142191, D8350591, ...) -- found sitting directly in
# a 'person'-typed create_by/update_by column (not just the dedicated *_employeeid columns), so
# without this it was fed to _gen_person as if the code itself were someone's name. No genuine
# person/org name is 1-3 letters immediately followed by 4+ digits with nothing else, so this is
# safe to treat as an unconditional shape override, same as the GUID/hex cases below.
_PERSONNEL_CODE_RE = r'[A-Za-z]{1,3}\d{4,}'
_ID_LIKE_RE  = re.compile(r'^(?:[\w.\-]+:)?(?:' + _GUID_DASHED + r'|[0-9a-fA-F]{20,40}|'
                           + _PERSONNEL_CODE_RE + r')$')
def looks_like_id(s):
    return bool(s) and bool(_ID_LIKE_RE.match(s.strip()))

# URLs have scheme/host/path structure that must be preserved — feeding one through the org
# generator destroys it. Only company/site names inside are swapped (via the forced-word rule).
_URL_RE = re.compile(r'^[A-Za-z][A-Za-z0-9+.\-]*://\S+$')
def looks_like_url(s):
    return bool(s) and bool(_URL_RE.match(s.strip()))

# Filenames (SOW docs, attachments, exports) carry a real file EXTENSION that must survive
# untouched — a `.docx` faked into `.quill` breaks the file type. Only the name STEM (before
# the last recognized extension) gets anonymized; the extension is reattached verbatim,
# case as originally written. Deliberately a fixed, common-format allowlist (not "any .xxx")
# so a genuine multi-part org/id value that happens to end in ".co"-style text isn't misread.
_FILE_EXT_RE = re.compile(
    r'^(.+)(\.(?:docx?|xlsx?|pptx?|pdf|txt|csv|rtf|odt|ods|odp|msg|eml|pst|ost|'
    r'zip|rar|7z|tar|gz|png|jpe?g|gif|bmp|tiff?|svg|mp4|mov|avi|wav|mp3|'
    r'json|xml|html?|log|vsdx?|one|mpp|dwg))$', re.I)
def looks_like_filename(s):
    return _FILE_EXT_RE.match(s or '')

# numeric price/quantity columns get a random ±15% perturbation (NOT name/org masking, NOT
# stored in mapping_xref). Detect money/qty columns by name; skip IDs/line-nos/flags/years.
NUMERIC_TYPES = {'int', 'bigint', 'smallint', 'tinyint', 'decimal', 'numeric', 'float',
                 'real', 'money', 'smallmoney'}
AMOUNT_HINT = re.compile(r'(amount|amt|price|cost|qty|quantity|rate|charge|discount|tax|'
                         r'netamount|unitprice|total|\bfee\b|retain|value|freight|salesprice|'
                         r'unitcost|linetotal|subtotal|'
                         r'revenue|margin|\btcv\b|\bacv\b|\barr\b|\bmrr\b|effort|forecast|'
                         r'budget|spend|worth|opex|capex)', re.I)
AMOUNT_SKIP = re.compile(r'(id$|_id|number$|lineno|linenumber|recid|sequencenumber|year|'
                         r'version|code$|status|timezone|utcoffset|typecode|ratetype|'
                         r'category|preference|msdyn|percent|_pct|flag|indicator|skip|calculate)', re.I)

def jitter_amount(value, max_pct=15.0):
    """Randomly adjust a numeric value by up to ±max_pct percent (the team's randomly_adjust
    logic). Preserves numeric type/scale. Per-cell random; never stored in mapping_xref."""
    if value is None or isinstance(value, bool):
        return value
    try:
        pct = random.uniform(-max_pct, max_pct) / 100.0
        if isinstance(value, decimal.Decimal):
            # Use a local context wide enough for big values (e.g. 12-digit revenue with
            # 18-place scale = 30 sig digits) — the default 28-digit context makes quantize
            # raise InvalidOperation, which would silently return the value UNCHANGED.
            with decimal.localcontext() as ctx:
                ctx.prec = max(50, len(value.as_tuple().digits) + 20)
                adj = value + (value * decimal.Decimal(repr(pct)))
                exp = value.as_tuple().exponent
                q = decimal.Decimal(1).scaleb(exp) if isinstance(exp, int) else decimal.Decimal('1')
                return adj.quantize(q, rounding=decimal.ROUND_HALF_UP)
        if isinstance(value, int):   return int(round(value * (1 + pct)))
        if isinstance(value, float): return value * (1 + pct)
        return float(value) * (1 + pct)          # numeric stored as string
    except Exception:
        return value

import pyodbc  # required

# ── logging ─────────────────────────────────────────────────────────────────────
def log(m): print(f"[{datetime.datetime.now():%H:%M:%S}] {m}", flush=True)

# ── per-run value log: logs/<table>/{dryrun,runv<version>}.log ──────────────────
# Purely additive record of what a run/dry-run wrote: the command, every
# column/original/anonymized triple, and the end time. Never read back by the
# engine itself -- does not change any anonymization behaviour.
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def _ist_now():
    return datetime.datetime.now(_IST).strftime('%Y-%m-%d %H:%M:%S IST')

def _mapping_xref_max_id():
    """Read MAX(id) from the shared mapping table via a fresh, short-lived connection (mirrors
    FakeEngine._load_xref's own approach). Purely informational for the value log -- never
    raises; returns None (and logs a warning) if the mapping DB can't be reached."""
    try:
        mc = connect_map()
        v = mc.cursor().execute(f"SELECT MAX(id) FROM [{MAP_SCHEMA}].[{XREF}]").fetchone()[0]
        mc.close()
        return v
    except Exception as e:
        log(f"  (warn: could not read {XREF} max id: {str(e)[:80]})")
        return None

def open_value_log(table, dry_run):
    log_dir = os.path.join(_BASE, 'logs', table)
    try:
        os.makedirs(log_dir, exist_ok=True)
        fname = 'dryrun.log' if dry_run else f'runv{VERSION}.log'
        fh = open(os.path.join(log_dir, fname), 'a', encoding='utf-8')
        fh.write(f"COMMAND RAN - {' '.join(sys.argv)} - {_ist_now()}\n")
        max_id_before = _mapping_xref_max_id()
        fh.write(f"MAX ID before any update: {max_id_before}\n")
        fh.flush()
        log(f"  MAX ID before any update: {max_id_before}")
        return fh
    except Exception as e:
        log(f"  (warn: could not open value log for {table}: {str(e)[:80]})")
        return None

def log_value(fh, col, original, fake, id_state='GENERATED-NOT-STORED'):
    if fh is None or original is None:
        return
    try:
        o = str(original).replace('\r', ' ').replace('\n', ' ')
        f = str(fake).replace('\r', ' ').replace('\n', ' ')
        fh.write(f"{col} - {o} - {f} - {id_state}\n")
    except Exception:
        pass

def close_value_log(fh):
    if fh is None:
        return
    try:
        max_id_after = _mapping_xref_max_id()
        fh.write(f"MAX ID after the successful run: {max_id_after}\n")
        fh.write(f"END TIME - {_ist_now()}\n")
        fh.close()
        log(f"  MAX ID after the successful run: {max_id_after}")
    except Exception:
        pass

# ════════════════════════════════════════════════════════════════════════════════
#  FAKE GENERATION ENGINE  (deterministic · ethnicity/gender-aware · injective)
#  — mirrors the outlook_email logic; self-contained with graceful fallbacks.
# ════════════════════════════════════════════════════════════════════════════════
class FakeEngine:
    def __init__(self, cur, prefer_clean=False, persist_enabled=True, write_table=None, map_cn=None):
        self.cur = cur                        # DATA connection (tables being anonymized)
        self.prefer_clean = prefer_clean      # regenerate clean fakes for legacy dirty ones
        self.persist_enabled = persist_enabled  # False = never persist new pairs anywhere
        # where NEW original->fake pairs are written. Real run: mapping_slice (shared, Azure).
        # Dry run: an isolated <table>_script_map temp table in the LOCAL data DB.
        self.write_table = write_table or XREF
        self._cache = {}                      # (type, norm) -> fake  (per-run memo)
        self._ff_cache = {}                   # name-token -> fake first name (row consistency)
        self._fl_cache = {}                   # name-token -> fake surname
        self._dom_cache = {}                  # real email domain -> fake domain (consistent, compact)
        self._region_pool = {}                # country_code (raw) -> [real region_code,...] seen in THIS table
        self._used  = set()                   # injectivity ledger (fakes taken)
        self._xref = None                     # (label,orig)->fake and orig->fake
        self._xref_any = None
        self._xref_ids = {}                   # nk -> mapping_xref.id (parallel to _xref_any) for logging
        self._non_pii = set()                 # normalized originals flagged is_pii='N' -- never faked
        self._relabeled = 0
        # batched writes (flushed per commit) — avoids per-value round-trips
        self._pending_ins = []                # [(label, original, fake)]
        self._pending_ins_meta = []           # parallel to _pending_ins: [(col, original, fake, ptype)]
        self._pending_upd = []                # [(fake, original)]  (prefer-clean rewrites)
        self._persisted = set()               # originals already queued/written this run
        # per-cell logging support (populated by run/run_inplace via attach_log/set_log_context)
        self._vlog = None                     # log file handle (opened by open_value_log)
        self._current_col = None              # column name of the cell currently being anonymized
        self._last_id_state = None            # last outcome tag (REUSE:<id>/NEW:pending/NEW:DRY-RUN/GENERATED-NOT-STORED)
        # Shared map (obi.mapping_slice on Azure) = the real target. To survive long idle periods
        # during slow GLiNER batches (Azure closes idle connections -> 08S01), we do NOT hold a
        # persistent Azure connection: flush_pending opens a FRESH short-lived one each time.
        # Dry-run writes to a LOCAL temp table -> use the (stable) local data connection directly.
        self._shared = (self.write_table == XREF)
        if self._shared:
            self._pconn = None; self._pcur = None          # opened fresh per flush
        else:
            self._pconn = cur.connection
            try:    self._pcur = self._pconn.cursor(); self._pcur.fast_executemany = True
            except Exception: self._pcur = self._pconn.cursor()
        self._init_faker()
        self._load_xref()

    def _init_faker(self):
        try:
            from faker import Faker
            self._Faker = Faker
            self.fk_en = Faker('en_US'); self.fk_in = Faker('en_IN'); self.fk_zh = Faker('zh_CN')
            for f in (self.fk_en, self.fk_in, self.fk_zh): f.seed_instance(0)
            self.has_faker = True
        except Exception:
            self.has_faker = False
        try:
            import gender_guesser.detector as gg
            self.gender = gg.Detector(case_sensitive=False)
        except Exception:
            self.gender = None
        try:
            import pypinyin; self.pypinyin = pypinyin
        except Exception:
            self.pypinyin = None
        self._ethnic = None; self._ethnic_tried = False; self._ethnic_cache = {}

    def _ethnicity(self, name):
        """Return 'ind' | 'chi' | 'other' using ethnicseer (cached), heuristic fallback.
        Restricted vocabulary keeps outlook's rule: only Indian/Chinese are special-cased."""
        norm = self.normalize(name)
        if norm in self._ethnic_cache: return self._ethnic_cache[norm]
        res = None
        if not self._ethnic_tried:
            self._ethnic_tried = True
            try:
                from ethnicseer import EthnicClassifier
                self._ethnic = EthnicClassifier.load_pretrained_model()
            except Exception:
                self._ethnic = None
        if self._ethnic is not None:
            try:
                code = self._ethnic.classify_names([name])[0]
                res = {'ind': 'ind', 'chi': 'chi'}.get(code, 'other')
            except Exception:
                res = None
        if res is None:                                   # heuristic fallback
            if re.search(r'\b(kumar|singh|reddy|rao|patel|sharma|gupta|nair|iyer|krishnan|'
                         r'balasubramanian|garimella|komiripalepu|subramaniam|venkat|naidu)\b', norm):
                res = 'ind'
            elif re.search(r'\b(wei|zhang|wang|li|liu|chen|yang|huang|zhao|dai|hao|zhou|xu|'
                           r'sun|ma|zhu|lin|guo)\b', norm):
                res = 'chi'
            else:
                res = 'other'
        self._ethnic_cache[norm] = res
        return res

    # -- helpers -----------------------------------------------------------------
    @staticmethod
    def normalize(s):
        s = unicodedata.normalize('NFKD', s or '')
        s = ''.join(c for c in s if not unicodedata.combining(c))
        return re.sub(r'\s+', ' ', s).strip().casefold()

    @staticmethod
    def _seed(s):
        return int(hashlib.sha256(s.encode('utf-8', errors='replace')).hexdigest(), 16)

    def _load_xref(self):
        """Preload the whole shared mapping (obi.mapping_slice) once from the MAPPING connection:
        build the lookup dicts and seed the injectivity ledger. Avoids per-value round-trips.
        Also records the row id per normalized-original so per-cell logs can report REUSE:<id>.

        `is_pii` (added to mapping_xref after a manual review pass) flags rows that entered the
        table via an over-eager blanket harvest but aren't actually PII -- confirmed live:
        4,414 'N' rows are bare junk tokens ('A', 'Admin', '1 1', '?', 'Aj') that got harvested
        as if they were real person names (the same HARVEST_STOP-shaped false-positive class
        this project has hit repeatedly), each carrying a bogus fake ('Admin' -> 'Norma Short').
        These are excluded entirely: not loaded into the reuse-first cache (so a future run
        never reuses the bogus fake), and their original value is tracked in self._non_pii so
        fake() below leaves any matching value alone rather than routing it through
        generation/reuse at all. Their OLD fake string is still added to self._used, though --
        it's already sitting in the shared table under this now-disowned original, so treating
        it as available again would let a genuinely different original collide onto the same
        fake string. New rows this run persists don't set is_pii (NULL) and are treated as
        normal/includable, same as rows from before this column existed."""
        self._xref, self._xref_any = {}, {}
        self._xref_ids = {}
        self._non_pii = set()
        try:
            mc = connect_map()                             # fresh short-lived read connection
            rows = mc.cursor().execute(
                f"SELECT id, description, originalvalue, anonymizedvalue, is_pii "
                f"FROM [{MAP_SCHEMA}].[{XREF}] WHERE originalvalue IS NOT NULL "
                f"AND anonymizedvalue IS NOT NULL").fetchall()
            mc.close()
        except Exception as e:
            log(f"  (warn: could not preload {XREF}: {e})"); return
        skipped_non_pii = 0
        for rid, desc, orig, fake, is_pii in rows:
            if not orig or not fake: continue
            nk = self.normalize(orig)                  # case/space-insensitive reuse key
            if (is_pii or '').strip().upper() == 'N':
                self._non_pii.add(nk)
                self._used.add(fake.strip().casefold())
                skipped_non_pii += 1
                continue
            self._xref_any.setdefault(nk, fake)
            self._xref_ids.setdefault(nk, rid)
            if desc: self._xref[(desc.strip().casefold(), nk)] = fake
            self._used.add(fake.strip().casefold())
        log(f"  loaded {len(rows) - skipped_non_pii:,} {XREF} pairs from shared map (reuse-first)"
            f"  [{skipped_non_pii:,} is_pii='N' rows excluded from consideration]")

    def _uniq(self, cand, seedkey, make):
        """Injectivity (bijective) with CLEAN output — no hex/number suffixes.
        If cand is already taken by a different original, deterministically re-roll
        (salted seed) to a *different clean* fake. If the base generator's space is
        SATURATED (e.g. thousands of single-word orgs vs the finite ORG_HEAD×ORG_TAIL
        grid), fall back to an UNBOUNDED clean extension: append brandable ORG_HEAD
        word(s) for names/orgs, or an extra local-part token for emails — guaranteeing a
        unique, digit-free fake no matter how large the dataset grows."""
        k = cand.strip().casefold()
        if k not in self._used:
            self._used.add(k); return cand
        for i in range(1, 200):
            c = make(self._seed(f"{seedkey}#{i}"))
            if c and c.strip().casefold() not in self._used:
                self._used.add(c.strip().casefold()); return c
        # unbounded, clean, deterministic fallback (base grid saturated)
        typ = seedkey.split(':', 1)[0]
        is_email = typ == 'email' and '@' in cand
        H = len(ORG_HEAD)
        for i in range(20000):
            if is_email:
                loc, dom = cand.split('@', 1)
                w = ORG_HEAD[self._seed(f"{seedkey}~e{i}") % H].lower()
                c = f"{loc}.{w}@{dom}" if i < H else f"{loc}.{w}{ORG_HEAD[self._seed(f'{seedkey}~f{i}')%H].lower()}@{dom}"
            else:
                sep = ' ' if ' ' in cand else ''
                w1 = ORG_HEAD[self._seed(f"{seedkey}~a{i}") % H]
                if i < H:
                    c = f"{cand}{sep}{w1}"
                else:                                   # two extra words -> ~H*H headroom
                    w2 = ORG_HEAD[self._seed(f"{seedkey}~b{i}") % H]
                    c = f"{cand}{sep}{w1}{sep}{w2}"
            if c.strip().casefold() not in self._used:
                self._used.add(c.strip().casefold()); return c
        self._used.add(cand.strip().casefold()); return cand

    # -- xref lookup (read-only, in-memory) --------------------------------------
    def xref_lookup(self, original, ptype):
        if not original or len(original) > MAX_LOOKUP_LEN:
            return None
        nk = self.normalize(original)              # case/space-insensitive reuse key
        label = XREF_TYPE.get(ptype)
        if label:
            v = self._xref.get((label.strip().casefold(), nk))
            if v: return v
        return self._xref_any.get(nk)              # type-agnostic fallback

    def xref_lookup_with_id(self, original, ptype):
        """Same as xref_lookup, but also returns the mapping_xref row id (or None if not persisted).
        Used by fake() to emit REUSE:<id> log lines. Returns (id, fake) or (None, None)."""
        v = self.xref_lookup(original, ptype)
        if v is None:
            return (None, None)
        nk = self.normalize(original)
        return (self._xref_ids.get(nk), v)

    # -- per-cell logging plumbing (set by run/run_inplace before each cell) -----
    def attach_log(self, vlog):
        """Attach the open value-log handle so fake()/_persist() can emit per-cell lines directly."""
        self._vlog = vlog

    def set_log_context(self, col):
        """Set the column name for subsequent log lines emitted from fake()/_persist().
        Also resets _last_id_state so a cell that never reaches _log_outcome (e.g. amount jitter,
        skip column) can default to GENERATED-NOT-STORED at flush time."""
        self._current_col = col
        self._last_id_state = 'GENERATED-NOT-STORED'

    def _log_outcome(self, original, fake, id_state):
        """Record the outcome of an anonymization decision. Writes one line to the value log
        (if attached) and stores id_state on the engine so callers can read it if they want to
        double-log at cell granularity."""
        self._last_id_state = id_state
        if self._vlog is not None and self._current_col is not None and original is not None:
            log_value(self._vlog, self._current_col, original, fake, id_state)

    # -- ethnicity / script ------------------------------------------------------
    def _is_han(self, s):
        return any('一' <= c <= '鿿' for c in s)

    # -- forced-word substitution (case-preserving) ------------------------------
    # Fire when the brand word is bounded by any NON-LETTER (so it also catches
    # URL-encoded/glued forms like '...%40centific.com' where a plain \b fails
    # because the digit from %40 is a word char) but never inside a longer word.
    # Replace the brand word WHEREVER it appears (no word boundary) — it's a distinctive,
    # made-up token, so any occurrence is the brand: catches glued compounds
    # ('centificglobal'->'aventraaglobal', 'CentificProjectRole'->...), and URL-encoded
    # neighbours where a hex letter abuts it ('%2Fcentific', '%40centific.com').
    # (longest-first so 'pacteraedge' resolves before 'pactera' if both are mapped)
    _FORCED_RE = re.compile(r'(' +
                            '|'.join(re.escape(k) for k in sorted(FORCED_MAP, key=len, reverse=True)) +
                            r')', re.I)
    def apply_forced(self, text):
        if not text: return text
        return self._FORCED_RE.sub(lambda m: case_like(m.group(0), FORCED_MAP[m.group(0).lower()]),
                                   text)

    # -- token-level name maps (row-consistent AND bijective, backed by a big corpus) --
    _NAME_OK = re.compile(r"^[A-Za-z][A-Za-z'\-]{1,19}$")
    def _ensure_bigpool(self):
        """Lazy-load a large real-name corpus (names-dataset: ~727k first / ~984k last) so
        every distinct original name can get a UNIQUE, realistic fake — Faker's ~1-3k pool
        is far too small (that's what caused Smith<-323 originals). Falls back to Faker if
        the corpus isn't installed."""
        if hasattr(self, '_big_first'): return
        self._big_first = self._big_last = None
        try:
            from names_dataset import NameDataset
            nd = NameDataset()
            self._big_first = [k for k in nd.first_names.keys() if self._NAME_OK.match(k)]
            self._big_last  = [k for k in nd.last_names.keys()  if self._NAME_OK.match(k)]
            log(f"  name corpus: {len(self._big_first):,} first / {len(self._big_last):,} last (unique fakes)")
        except Exception as e:
            log(f"  names-dataset unavailable ({str(e)[:60]}) — falling back to Faker name pool")

    def _pick_unique(self, pool, seedkey, avoid_norm):
        """Deterministic UNIQUE pick from a pool. Linear (not quadratic) probe over the first
        min(2*L, 256) tries -- guarantees every slot in a small pool (e.g. the 82-entry
        CN_CITIES) actually gets tried at least once; quadratic step (base+s*s)%L was found by
        stress-testing to skip most residues on small L (only ~L/2 distinct values ever hit no
        matter how many tries), which silently exhausted the pool early and fell through to a
        compound fallback that had no retry/uniqueness check of its own -- confirmed to produce
        real collisions once a small pool like CN_CITIES passed ~82 distinct values. The compound
        fallback below now retries the same way (~L^2 space) instead of returning one fixed
        value, so a small pool degrades gracefully instead of colliding."""
        L = len(pool); base = self._seed(seedkey) % L
        for s in range(0, min(2 * L, 256)):
            v = pool[(base + s) % L]
            k = v.strip().casefold()
            if k and k != avoid_norm and k not in self._used:
                self._used.add(k); return v
        base2 = self._seed(seedkey + ':2') % (L * L)
        for s in range(0, min(L * L, 4096)):
            combo = (base2 + s) % (L * L)
            v = f"{pool[combo // L]}-{pool[combo % L]}"          # ~L^2 space, still name-like
            k = v.strip().casefold()
            if k and k != avoid_norm and k not in self._used:
                self._used.add(k); return v
        v = f"{pool[base]}-{pool[(base + 1) % L]}"    # truly exhausted -- last resort, accept risk
        self._used.add(v.strip().casefold()); return v

    def _mk_name(self, token, seed, eth, han, first):
        """One ethnicity-appropriate candidate (Faker) for a given seed; 'other' handled by
        the big pool in _ff/_fl, not here."""
        if han and self.has_faker:
            self.fk_zh.seed_instance(seed); return self.fk_zh.first_name() if first else self.fk_zh.last_name()
        if eth == 'chi' and self.has_faker:
            self.fk_zh.seed_instance(seed); h = self.fk_zh.first_name() if first else self.fk_zh.last_name()
            return ''.join(self.pypinyin.lazy_pinyin(h)).capitalize() if self.pypinyin else ('Wei' if first else 'Chen')
        if eth == 'ind' and self.has_faker:
            self.fk_in.seed_instance(seed)
            if not first: return self.fk_in.last_name()
            g = self.gender.get_gender(self.normalize(token)) if self.gender else None
            return (self.fk_in.first_name_male() if g in ('male', 'mostly_male')
                    else self.fk_in.first_name_female() if g in ('female', 'mostly_female')
                    else self.fk_in.first_name())
        if self.has_faker:                       # 'other' Faker fallback if no corpus
            self.fk_en.seed_instance(seed)
            if not first: return self.fk_en.last_name()
            g = self.gender.get_gender(self.normalize(token)) if self.gender else None
            return (self.fk_en.first_name_male() if g in ('male', 'mostly_male')
                    else self.fk_en.first_name_female() if g in ('female', 'mostly_female')
                    else self.fk_en.first_name())
        return (f"Name{seed % 100000}" if first else f"S{seed % 99999}")

    def _name_token(self, token, first):
        n = self.normalize(token)
        if not n: return token
        cache = self._ff_cache if first else self._fl_cache
        if n in cache: return cache[n]
        self._ensure_bigpool()
        eth = self._ethnicity(token); han = self._is_han(token)
        pool = self._big_first if first else self._big_last
        v = None
        if han or eth in ('chi', 'ind'):         # ethnicity-preserving Faker, unique via _used
            for i in range(0, 40):
                cand = self._mk_name(token, self._seed(f"{'ff' if first else 'fl'}:{n}#{i}"), eth, han, first)
                k = cand.strip().casefold()
                if k and k != n and k not in self._used:
                    self._used.add(k); v = cand; break
        if v is None and pool:                   # 'other' bulk, or ethnicity pool saturated -> big corpus
            v = self._pick_unique(pool, f"{'ff' if first else 'fl'}:{n}", n)
        if v is None:                            # no corpus + Faker saturated
            for i in range(0, 40):
                cand = self._mk_name(token, self._seed(f"x{i}:{n}"), 'other', False, first)
                k = cand.strip().casefold()
                if k and k != n and k not in self._used:
                    self._used.add(k); v = cand; break
            if v is None: v = (self._mk_name(token, self._seed(n), 'other', False, first) + 'ex')
        cache[n] = v; return v

    def _ff(self, token): return self._name_token(token, True)
    def _fl(self, token): return self._name_token(token, False)

    @staticmethod
    def _person_role(col):
        c = (col or '').lower()
        if 'first' in c or 'middle' in c or 'given' in c or 'nick' in c: return 'first'
        if 'last' in c or 'surname' in c or 'family' in c:               return 'last'
        return 'full'          # fullname / name / yomifullname / free-text spans

    _TITLE_RE = re.compile(r"^((?:mr|mrs|ms|miss|mx|dr|prof|sir|madam)\.?)\s+(.+)$", re.I)
    # "Last[ Middle], First[ Middle]" -> canonical "First[ Middle] Last[ Middle]". Each side may
    # be multiple words (e.g. 'Ashrith reddy, Namireddy' -> 'Namireddy Ashrith reddy'), not just
    # a single token -- the OLD version of this regex only matched one bare word on each side of
    # the comma, silently skipping this exact multi-word case.
    _LAST_FIRST_RE = re.compile(r"^\s*([A-Za-z\-'\s]+?)\s*,\s*([A-Za-z\-'\s]+?)\s*$")

    def _gen_person(self, original, col=None):
        """Column-aware, row-consistent: built from per-token first/surname maps so that
        firstname / lastname / fullname / email of the same person all agree."""
        s = original.strip()
        # honorific title prefix ('Mr. Ravikoti Chandra Shekar') -- kept verbatim and excluded
        # from the tokenizer below; without this, "Mr." itself got faked as if it were the
        # person's first name (e.g. -> 'Aladwani Omisha Laksh Sankar', a 4th fake token standing
        # in for "Mr."). Every return path below is prefixed with `title` (empty string if none).
        title = ''
        m_title = self._TITLE_RE.match(s)
        if m_title:
            title, s = m_title.group(1) + ' ', m_title.group(2)
        # bilingual "Latin Name （中文名）" staff-name pattern (obip1 clm_* tables): handle
        # BEFORE the generic whitespace tokenizer below, which would otherwise treat the
        # parenthesized Chinese segment as just another token -- see
        # obi_chinese_anonymizer.py for why that mis-splits and mis-pairs the two halves.
        bi = chinese_anon.split_bilingual(s)
        if bi is not None:
            latin, chinese_part, op, cp, email = bi
            # seed on the PARSED (latin, chinese) pair, not the raw original string: incidental
            # whitespace elsewhere in the source (e.g. one row has an extra space before the
            # glued-on email, another doesn't) must not change the seed -- same real person's
            # name has to fake to the same identity regardless of that kind of noise.
            canon = f"{latin}|{chinese_part}"
            pair = chinese_anon.gen_bilingual_name(
                canon, self._seed, getattr(self, 'fk_zh', None), self.pypinyin, self._used)
            if pair is not None:
                fake_chinese, fake_latin = pair
                fake_email = None
                if email:
                    fake_email = self._gen_bilingual_email(email, fake_latin)
                return title + chinese_anon.format_bilingual(fake_latin, fake_chinese, op, cp, fake_email)
        # "Name (Company annotation)" pattern (obip1 crm_itticket_Dict.Name, Type=Customer/
        # workedby: internal helpdesk requesters annotated with which legal entity they belong
        # to, e.g. 'Xiaoxi Bai (Centific Technologies Inc)', 'Bella Chen (Shanghai Centific
        # Technology)'). Checked AFTER the CJK-bilingual-parens case above returns None for it
        # (that one only matches Han-script parens content) -- without this, the generic
        # whitespace tokenizer below treats the whole parenthetical annotation as more name
        # tokens, producing a garbled multi-word non-name AND silently losing the real company
        # identity (so 'Centific'/'Pactera' never goes through FORCED_MAP -> 'Aventraa'/
        # 'Eventraa' at all, since the literal words are consumed as fake-name-generation input,
        # not passed through). Recurses into _gen_person for the name half (reuses all of the
        # above: titles, single-Han-token, Last-First, etc.) and _gen_org for the company half
        # (which already applies FORCED_MAP internally), then reassembles with the parens intact.
        # Same shape, square-bracket flavor (e.g. 'Buttner, Stephen [C]' -- a status/contractor
        # marker suffix, confirmed present in real data alongside the round-parens company
        # annotation case): try round parens first, then square brackets.
        m_paren = re.match(r'^(.+?)\s*\(([^()]+)\)\s*$', s)
        m_co = m_paren or re.match(r'^(.+?)\s*\[([^\[\]]+)\]\s*$', s)
        bracket_ch = ('(', ')') if m_paren else ('[', ']')
        if m_co and not chinese_anon.is_han(m_co.group(2)):
            name_part, company_part = m_co.group(1).strip(), m_co.group(2).strip()
            if name_part:
                # go through the TOP-LEVEL fake() dispatcher, not _gen_person/_gen_org directly --
                # that's what applies the cache/reuse-first/persist layer. Calling the _gen_*
                # generators directly bypassed it, so the SAME real company (e.g. 'Centific
                # Technologies Inc', which always word-substitutes to the same deterministic
                # 'Aventraa Technologies Inc' with no per-row seeding) looked like a fresh
                # "collision" on every subsequent row and fell into _uniq's disambiguation
                # fallback, appending a random extra word each time (confirmed empirically: 3
                # occurrences of the identical annotation produced 3 different garbled results
                # like 'Aventraa Technologies Inc Kestrel/Aurora/Quantic'). Routing through
                # fake() also means the name half gets the SAME fake as if that person's name
                # appeared standalone elsewhere in the table, instead of an independent one.
                fake_name = self.fake(name_part, 'person', col=col)
                fake_company = self.fake(company_part, 'org')
                ob, cb = bracket_ch
                return f"{title}{fake_name} {ob}{fake_company}{cb}"
        # NOTE: "Last, First" is now canonicalized in fake() itself, BEFORE the reuse-first
        # cache/xref lookup -- by the time _gen_person runs, `s` is already "First Last" (see
        # fake()'s docstring comment for why it has to happen that early).
        # Token boundary = whitespace OR hyphen (not whitespace alone), so a hyphenated real
        # surname ('Smith-Jones') or a hyphen-joined slug forced through as type=person
        # ('nick-sbu1lead-dm') is treated as multiple name-like tokens instead of one glued blob
        # that _ff/_fl fakes as a single word with the hyphen silently dropped. Every separator
        # (each run of spaces, or a hyphen) is spliced back VERBATIM from the original position
        # rather than normalized to a single ' '.join() space, so whatever exact shape the
        # original had survives into the fake unchanged. This only affects what happens WITHIN an
        # existing whitespace-delimited token -- the whitespace-token count/order zip_name_tokens
        # relies on (its own original.split()/fake.split() pairing) is unaffected either way.
        tok_re = re.compile(r'[^\s\-]+')
        toks = tok_re.findall(s)
        if not toks: return original
        role = self._person_role(col)

        def _rebuild(fake_for):
            out, last_end = [], 0
            for i, m in enumerate(tok_re.finditer(s)):
                out.append(s[last_end:m.start()])
                out.append(fake_for(i, m.group(0)))
                last_end = m.end()
            out.append(s[last_end:])
            return title + ''.join(out)

        if role == 'first':
            return _rebuild(lambda i, t: case_like(t, self._ff(t)))
        if role == 'last':
            return _rebuild(lambda i, t: case_like(t, self._fl(t)))
        # full: first token -> first name, last token -> surname, middles -> first-style
        if len(toks) == 1:
            # a single Han-script token in a full-name column is virtually always a complete
            # Chinese name (surname+given run together, no space -- Chinese convention), not a
            # first-name-only value. Faking it via _ff alone (correct for a single LATIN token
            # like "Madonna") would call fk_zh.first_name() and silently drop the surname.
            if chinese_anon.is_han(toks[0]) and len(toks[0]) >= 2:
                cn = chinese_anon.gen_chinese_fullname(original, self._seed, getattr(self, 'fk_zh', None), self._used)
                if cn is not None:
                    return title + cn
            return title + case_like(toks[0], self._ff(toks[0]))
        last_i = len(toks) - 1
        return _rebuild(lambda i, t: case_like(t, self._fl(t)) if i == last_i else case_like(t, self._ff(t)))

    def _gen_bilingual_email(self, email, fake_latin):
        """Fake email for the bilingual-name-plus-email compound (see
        chinese_anon.split_bilingual): local-part rebuilt from the SAME fake given/surname just
        generated for the Latin name sitting next to it in the same string -- so 'Chaohui Guo'
        faked to 'Yong Liang' also fakes 'chaohui.guo@...' to 'yong.liang@...', never an
        independently-generated local-part that could disagree with the adjacent name. Domain
        goes through the engine's own _fake_domain, so FORCED_MAP/public-provider policy is
        identical to every other emailed value (centific.com -> aventraa.com, same as always)."""
        local, domain = email.split('@', 1)
        _, _, digit_suffix = chinese_anon.split_email_local(local)
        given_latin, surname_latin = fake_latin.split(' ', 1)
        fake_local = chinese_anon.gen_bilingual_email_local(given_latin, surname_latin, digit_suffix or '')
        return f"{fake_local}@{self._fake_domain(domain)}"

    def _fake_domain(self, domain):
        """Fake an e-mail domain, HYBRID policy:
          - public providers (gmail/yahoo/outlook/…) -> kept as-is (not company-identifying);
          - company domains -> the WHOLE registrable domain masked to a compact 2-word brandable
            core (or the forced word), keeping only the public suffix (.com / .co.uk / …).
        Cached per real domain -> one consistent, compact fake domain (no ballooning, no leak)."""
        dl = (domain or '').strip().lower()
        if not dl or '.' not in dl:
            return domain or 'example.com'
        if dl in PUBLIC_EMAIL_DOMAINS:
            return domain                          # keep public provider (original case)
        if dl in self._dom_cache:
            return self._dom_cache[dl]
        labels = dl.split('.')
        suffix = '.'.join(labels[-2:]) if len(labels) >= 2 and '.'.join(labels[-2:]) in self._TWO_LEVEL_TLD else labels[-1]
        nsuf = suffix.count('.') + 1
        reg = labels[-(nsuf + 1)] if len(labels) > nsuf else labels[0]   # registrable label
        forced = self.apply_forced(reg)
        if forced.lower() != reg.lower():
            core = re.sub(r'[^a-z0-9]', '', forced.lower()) or 'aventraa'
        else:
            H = len(ORG_HEAD); sd = self._seed('emaildom:' + dl)
            core = (ORG_HEAD[sd % H] + ORG_HEAD[(sd // H) % H]).lower()   # compact 2-word core
        fake = f"{core}.{suffix}"
        self._dom_cache[dl] = fake
        return fake

    def _gen_email(self, original, name_hint=None):
        s = original.strip()
        m = re.search(r'@([^@\s]+)$', s)
        domain = m.group(1) if m else 'example.com'
        local = s.split('@')[0]
        parts = [p for p in re.split(r'[._\-]+', local) if p]
        # rebuild local from the SAME token maps used by the name columns
        if name_hint and (name_hint[0] or name_hint[1]):
            # a companion person column (or columns) in the SAME row names these token(s) --
            # trust them directly via SUBSTRING replacement in the local part, regardless of
            # whether the local part has separators to split on or not. Position-preserving:
            # keeps whatever separator/digit/filler structure the original had (a firstname.
            # lastname@... shape stays dotted; a concatenated personal-email shape like
            # 'paulchriscampbell507@gmail.com' stays concatenated), and naturally handles a
            # multi-word field where MORE than one of its own words is embedded in the email
            # (e.g. Last_Name='Chisom Berenice' both appearing) -- substituting only the first
            # match per role left the other word's real text sitting in the fake output
            # unchanged (a confirmed leak). Anything else in the local part (a nickname/filler we
            # have no fake for, digits, an unmatched extra segment, ...) is left exactly as-is
            # rather than guessed at. Longest token first, so a short match that happens to be a
            # substring of a longer, not-yet-replaced one doesn't get substituted first and
            # corrupt it.
            #
            # Each hit carries an `is_whole` flag telling us how the COMPANION COLUMN ITSELF
            # would resolve this exact token, so the email substitution never disagrees with it:
            #   - is_whole=True: this token IS the entire field value (e.g. a single-word
            #     Last_Name='Verma') -- that field's own fake comes from fake()'s top-level
            #     reuse-first check on the WHOLE value, so we call fake() here too. Confirmed
            #     empirically: Last_Name='Verma' reuse-first resolves to 'Oddrun' (an existing
            #     mapping_xref entry from some earlier table); calling _fl('Verma') directly
            #     instead draws an unrelated fresh 'Panchal', disagreeing with the column.
            #   - is_whole=False: this token is just ONE WORD of a multi-word field (e.g. 'Grace'
            #     inside First_Name='Charisse Grace') -- _gen_person resolves a multi-word
            #     field's OWN tokens via direct _ff/_fl calls per token (bypassing reuse-first
            #     entirely, since reuse-first there only ever checks the FULL multi-word phrase,
            #     not each word). Routing this token through fake() instead would wrongly apply
            #     reuse-first to a bare word that the real column never treats as a whole value
            #     -- confirmed empirically: First_Name='Charisse Grace' fakes to 'Eurofashion
            #     Dictionary' (Grace -> Dictionary via a plain _ff call), but routing the email's
            #     'Grace' hit through fake() picked up an unrelated pre-existing mapping_xref
            #     entry for the bare word 'Grace' from some other table/context.
            # Match AND substitute on the same cleaned (alnum-only) string, not the raw local
            # part with its separators still in -- the caller detected containment against the
            # cleaned string too (e.g. 'r.mary.am.d' -> cleaned 'rmaryamd' contains 'maryam'),
            # but a literal, separator-preserving replace() against the RAW local can never find
            # that same substring (the dots split 'mary'/'am' apart, so 'maryam' isn't
            # contiguous in 'r.mary.am.d') -- confirmed empirically as a silent no-op: the whole
            # email passed through completely unchanged. Trades exact separator-style
            # preservation for guaranteed correctness. A single combined regex (not sequential
            # .replace() calls) finds genuinely non-overlapping leftmost matches in one pass --
            # sequential replacement let an earlier match consume a character a later, OVERLAPPING
            # match also needed (e.g. First_Name='Zedd'/Last_Name='Demir' both sitting in
            # 'izeddemir', sharing the 'd' at the seam), leaving a corrupted half-real leftover
            # fragment ('ized...') instead of either name being fully replaced.
            fn_hits, ln_hits = name_hint
            local_clean = re.sub(r'[^a-z0-9]', '', local.lower())
            hits = [(t, 'last_name', w) for t, w in (ln_hits or [])] + \
                   [(t, 'first_name', w) for t, w in (fn_hits or [])]
            tok_to_fake = {}
            for tok, role_col, is_whole in hits:
                tok_clean = re.sub(r'[^a-z0-9]', '', tok.lower())
                if not tok_clean or tok_clean not in local_clean or tok_clean in tok_to_fake:
                    continue
                if is_whole:
                    fakeraw = self.fake(tok, 'person', col=role_col)
                else:
                    fakeraw = self._ff(tok) if role_col == 'first_name' else self._fl(tok)
                tok_to_fake[tok_clean] = re.sub(r'[^a-z0-9]', '', fakeraw.lower()) or 'x'
            if tok_to_fake:
                pat = re.compile('|'.join(re.escape(t) for t in sorted(tok_to_fake, key=len, reverse=True)))
                newlocal = pat.sub(lambda m: tok_to_fake[m.group(0)], local_clean)
            else:
                newlocal = local_clean
        elif len(parts) >= 2:
            # PROACTIVE reverse lookup: before independently drawing fresh first/last tokens,
            # check whether "First Last" (derived from this email's own local part) already has
            # an established person mapping ANYWHERE in mapping_xref. Without this, a person who
            # is ONLY ever seen as an email address in this run (never independently faked as a
            # 'person' value -- e.g. a requester who never shows up in an agent-name column)
            # never benefits from fake()'s reuse-first backfill at all, since that backfill only
            # fires when the whole name itself gets looked up. Confirmed empirically: 'Lokesh
            # Kenche' already mapped to 'Matthew Martin' in mapping_xref, but
            # 'lokesh.kenche@centific.com' still faked independently to
            # 'manya.jalilzad@aventraa.com' because he was never an agent on any of the rows
            # where his email appears as the requester -- this closes that gap directly.
            candidate = f"{parts[0].capitalize()} {parts[-1].capitalize()}"
            existing = self.xref_lookup(candidate, 'person')
            if existing is not None:
                self._backfill_name_tokens(candidate, existing)
            fn = re.sub(r'[^a-z0-9]', '', self._ff(parts[0]).lower()) or 'user'
            ln = re.sub(r'[^a-z0-9]', '', self._fl(parts[-1]).lower()) or 'x'
            if len(parts[0]) == 1: fn = fn[:1]                     # initial.last shape
            sep = '.' if '.' in local else ('_' if '_' in local else '')
            newlocal = f"{fn}{sep}{ln}"
        elif parts:
            newlocal = re.sub(r'[^a-z0-9]', '', self._ff(parts[0]).lower()) or 'user'
        else:
            newlocal = 'user'
        fdom = self._fake_domain(domain)
        cand = f"{newlocal}@{fdom}"
        norm = self.normalize(s)
        return self._uniq(cand, 'email:' + norm, lambda sd: f"{newlocal}{chr(97 + sd % 26)}@{fdom}")

    def _gen_phone(self, original):
        norm = self.normalize(original); seed = self._seed('phone:' + norm)
        digits = ''.join(ch for ch in original if ch.isdigit())
        n = str(seed); newdigits = (n * ((max(len(digits), 6) // len(n)) + 1))[:max(len(digits), 6)]
        out, di = [], 0
        for ch in original:                                # format-preserving
            if ch.isdigit() and di < len(newdigits): out.append(newdigits[di]); di += 1
            else: out.append(ch)
        cand = ''.join(out) if digits else f"+1-555-{seed % 10000:04d}"
        return self._uniq(cand, 'phone:' + norm, lambda s: f"+1-555-{s % 10000:04d}")

    def _gen_birth(self, original):
        """Standalone 'birth' column (e.g. sift_bus_candidate.birth = '1992.05', not embedded in
        JSON like the ResumeRating tables) -- the whole cell IS the date, so no JSON-key regex is
        needed here; calls the same calendar-aware jitter_birthdate directly. Falls back to the
        opaque digit-preserving substitution if the value doesn't match the expected
        YYYY<sep>MM[<sep>DD] shape (jitter_birthdate returns None for that)."""
        fake = jitter_birthdate(original, self._seed)
        return fake if fake is not None else self._gen_guid(original)

    _ID_SEGMENT_RE = re.compile(r'([_\-])')

    def _gen_guid(self, original, id_hint=None):
        """Format-preserving fake for opaque IDs (GUIDs / account-ids / personnel numbers).
        STRICT CLASS-PRESERVING: digit -> deterministic digit (0-9), uppercase letter -> uppercase
        letter, lowercase letter -> lowercase letter; every other char (colons, dashes, '_', a
        literal prefix) kept in place. This fixes the old hex substitution that turned digits into
        letters a-f (e.g. 'P3003490' -> 'Pa48bee2'): now 'P3003490' -> 'P7251840' (digits stay digits,
        char stays char). A hex-range letter (a-f/A-F) is further restricted to another hex-range
        letter (not the full alphabet) so a genuine GUID's fake stays entirely hex -- required for
        SQL Server to accept it back into a `uniqueidentifier` column (a value with e.g. 'q' or 'Z'
        fails that conversion outright). Non-hex letters (g-z/G-Z, as in personnel codes like the
        'P' in 'P3003490') are unaffected, still drawn from the full alphabet as before.
        Deterministic -- but as of the mapping_xref persistence change, `fake()` now checks/writes
        this value through mapping_xref same as person/org/email, so a value already mapped there
        (under ANY type) is reused instead of regenerated here.

        `id_hint` (raw_segment -> companion_column's own fake) lets a `_`/`-`-delimited segment
        that exactly matches ANOTHER enabled id-type column's raw value in the SAME row reuse that
        column's already-established fake instead of being faked independently -- e.g. a shared
        company/data-area code embedded as a prefix (`DATAAREAID='UWHS'`,
        `PURCHASEORDER='UWHS_PO000189'`) now consistently fakes to the SAME code in both places,
        instead of a different, unrelated-looking prefix each time it's embedded elsewhere."""
        norm = self.normalize(original)
        def _char(ch, r):
            if ch.isdigit():        return str(r % 10)
            if 'a' <= ch <= 'f':    return _shift_ne(97, 6, r, ch)
            if 'A' <= ch <= 'F':    return _shift_ne(65, 6, r, ch)
            if 'a' <= ch <= 'z':    return _shift_ne(97, 26, r, ch)
            if 'A' <= ch <= 'Z':    return _shift_ne(65, 26, r, ch)
            return ch                                            # separators/punct kept in place
        def build(sd):
            if not id_hint:
                return ''.join(_char(ch, self._seed(f"{sd}:{i}:{norm}"))
                               for i, ch in enumerate(original))
            out = []; i = 0
            for seg in self._ID_SEGMENT_RE.split(original):
                if seg in id_hint:
                    out.append(id_hint[seg]); i += len(seg); continue
                for ch in seg:
                    out.append(_char(ch, self._seed(f"{sd}:{i}:{norm}"))); i += 1
            return ''.join(out)
        return self._uniq(build(self._seed('id:' + norm)), 'id:' + norm, build)

    _TWO_LEVEL_TLD = {'co.uk','org.uk','ac.uk','gov.uk','co.jp','co.in','com.cn','net.cn',
                      'com.au','net.au','org.au','com.hk','com.sg','com.br','com.mx',
                      'co.kr','co.za','com.tw','co.il','com.tr'}

    def _gen_url(self, original):
        """Format-preserving fake for URLs. Keeps scheme, sub-domains (www), the TLD/suffix,
        and the path SHAPE; deterministically swaps the registrable domain LABEL (the part that
        identifies the company, e.g. 'baidu' in www.baidu.com) for a brandable word so the real
        company no longer leaks. Consistent: same domain label -> same fake label. Forced words
        (centific->aventraa) still apply."""
        s = (original or '').strip()
        m = re.match(r'^([A-Za-z][A-Za-z0-9+.\-]*://)(.*)$', s)
        if m:
            scheme, rest = m.group(1), m.group(2)
        elif re.match(r'^[A-Za-z0-9][\w\-]*(\.[A-Za-z0-9][\w\-]*)+([/:?#]|$)', s):
            scheme, rest = '', s      # scheme-less bare host: www.adobe.com, mathworks.com/x
        else:
            return self._gen_org(s)   # not URL-shaped (free text in the field) -> org-scrub
        # Malformed value with a SECOND embedded URL (e.g. 'https://Foo ... https://www.foo.com')
        # — structured host parsing can't mask the company in the tail. Org-fake every alpha run
        # of the whole string instead (masks both copies of the brand).
        if '://' in rest:
            return self._gen_org(s)
        # split netloc (host[:port]) from the remainder (path/query/fragment)
        cut = len(rest)
        for ch in '/?#':
            i = rest.find(ch)
            if i != -1: cut = min(cut, i)
        netloc, tail = rest[:cut], rest[cut:]
        hostport = netloc.rsplit('@', 1)  # strip any userinfo
        host = hostport[-1]
        port = ''
        if ':' in host:
            host, port = host.split(':', 1); port = ':' + port
        labels = host.split('.')
        if len(labels) < 2:
            # single-label host (no TLD): 'https://pgatour', 'https://<CJK company>'. The whole
            # host IS the identifier — swap it, unless it's a trivial placeholder (*, -, NA).
            reg_idx = 0
            suffix_len = 0
            if len(re.findall(r'[^\W\d_]', host, re.UNICODE)) < 2:
                return self.apply_forced(s)   # '*', '-', bare/IP-ish -> leave
        else:
            # figure out how many trailing labels form the public suffix
            suffix_len = 2 if '.'.join(labels[-2:]).lower() in self._TWO_LEVEL_TLD else 1
            reg_idx = len(labels) - suffix_len - 1   # index of the registrable label
            if reg_idx < 0:
                return self.apply_forced(s)
        orig_label = labels[reg_idx]
        forced = self.apply_forced(orig_label)
        if forced.lower() != orig_label.lower():
            new_label = forced                                  # centific -> aventraa
        else:
            seed = self._seed('urlhost:' + orig_label.lower())
            word = ORG_HEAD[seed % len(ORG_HEAD)].lower()
            new_label = case_like(orig_label, word)             # mirror caps of original label
        labels[reg_idx] = new_label
        return f"{scheme}{'.'.join(labels)}{port}{tail}"

    def _org_head(self, seed):
        return ORG_HEAD[seed % len(ORG_HEAD)]

    def _org_tail(self, seed):
        return ORG_TAIL[(seed // 7) % len(ORG_TAIL)]

    def _acronym_fake(self, original):
        """UNSCO -> VWXYZ, C3 -> X7 : same length/shape, letter->letter (case kept),
        digit->digit, separators kept. Deterministic."""
        base = self.normalize(original)
        def make(sd):
            out = []
            for k, ch in enumerate(original):
                if ch.isalpha():
                    c = chr(65 + (self._seed(f"acr:{base}:{sd}:{k}") % 26))
                    out.append(c if ch.isupper() else c.lower())
                elif ch.isdigit():
                    out.append(str(self._seed(f"acrd:{base}:{sd}:{k}") % 10))
                else:
                    out.append(ch)
            return ''.join(out)
        return self._uniq(make(0), 'acr:' + base, make)

    def _gen_org(self, original):
        """Org-style fake that PRESERVES STRUCTURE and never looks like a person.
        Works on alphabetic runs, keeping every separator/digit (spaces, underscores,
        hyphens, numbers) exactly in place. Each alpha run becomes:
          - the forced fake if it's a forced word (centific->aventraa),
          - kept as-is if it's a legal suffix / filler (PVT, LTD, of, Global),
          - a same-length UPPERCASE acronym if it's a short all-caps code segment
            (GDC, CN, ENG -> preserves the code shape),
          - otherwise a brandable company word (Vertex, Nimbus, ...), case-matched.
        A plain single-word company gets a descriptor too (e.g. 'Vertex Systems')."""
        s = original.strip()
        if chinese_anon.is_han(s):        # Chinese company name -> a different Han-script
            return chinese_anon.gen_chinese_org(s, self._seed, self._used)   # brandable, not English
        norm = self.normalize(s)
        seed = self._seed('org:' + norm)
        # single all-caps acronym token -> same-shape acronym (UNSCO -> VWXYZ)
        if ' ' not in s and '_' not in s:
            core = re.sub(r'[^A-Za-z0-9]', '', s)
            if core and core.isupper() and 2 <= len(core) <= 8 and _wordkey(s) not in FORCED_MAP:
                return self._acronym_fake(s)
        single_run = len(re.findall(r'[^\W\d_]+', s, re.UNICODE)) <= 1
        def build(sd):
            # split on runs of LETTERS (any script, incl. CJK/accented); digits, spaces,
            # underscores and punctuation are separators kept verbatim (preserves numbers/shape)
            parts = re.split(r'([^\W\d_]+)', s, flags=re.UNICODE)
            out = []
            for idx, seg in enumerate(parts):
                if not seg or not seg[0].isalpha():      # separator / digits / punct
                    out.append(seg); continue
                wk = seg.lower()
                if wk in FORCED_MAP:
                    out.append(case_like(seg, FORCED_MAP[wk])); continue
                if wk in LEGAL_SUFFIX or wk in FILLER:
                    out.append(seg); continue            # keep suffix / filler verbatim
                wseed = self._seed(f"{sd}:{idx}:{norm}")
                if seg.isupper() and len(seg) <= 4:      # short caps code seg -> acronym
                    out.append(''.join(chr(65 + (self._seed(f"{wseed}:{k}") % 26))
                                       for k in range(len(seg))))
                else:
                    w = self._org_head(wseed)
                    if single_run:                       # a real company name -> add descriptor
                        w = w + ' ' + self._org_tail(wseed)
                    out.append(case_like(seg, w))
            return ''.join(out)
        return self._uniq(build(seed), 'org:' + norm, build)

    def _gen_country(self, original):
        """#5 country fake -> a DIFFERENT real country, matching the input's *form*:
        2-letter ISO code -> 2-letter code, 3-letter -> 3-letter, otherwise full name.
        Deterministic & consistent (many originals may share a target — acceptable for
        geographic generalization). Never maps a country to itself."""
        s = original.strip(); norm = self.normalize(s)
        n = len(COUNTRIES); base = self._seed('country:' + norm) % n
        tgt = COUNTRIES[base]
        for j in range(n):                      # avoid identity mapping
            c = COUNTRIES[(base + j) % n]
            if norm not in (c[0].casefold(), c[1].casefold(), c[2].casefold()):
                tgt = c; break
        core = re.sub(r'[^A-Za-z]', '', s)
        if len(core) == 2:                      out = tgt[1]      # US  -> CN
        elif len(core) == 3 and core.isalpha(): out = tgt[2]      # USA -> CHN
        else:                                   out = tgt[0]      # United States -> China
        return case_like(s, out)

    def _gen_location(self, original):
        """#5 location/city fake -> a DIFFERENT real city name, case-matched. Deterministic AND
        injective (each distinct original city gets its own distinct fake city, via the same
        _pick_unique quadratic-probe-against-_used mechanism as names/orgs) -- NOT "many->one
        generalization" as originally designed here. Changed on request: two different real
        cities (观察 e.g. 苏州/成都) landing on the same fake city loses the distinction between
        them in the anonymized data, same concern as any other type would have. Already-persisted
        mapping_xref entries are untouched either way -- reuse-first resolves before this runs."""
        s = original.strip(); norm = self.normalize(s)
        if ',' in s and chinese_anon.is_han(s):
            # comma-separated multi-city cell (e.g. '上海,北京' or '成都,深圳,无锡,上海') -- fake
            # EACH city independently and rejoin, so a 4-city list stays a 4-city list instead of
            # collapsing to one.
            return ','.join(self._gen_location(part) for part in s.split(','))
        if chinese_anon.is_han(s):
            if chinese_anon.is_location_skip(s):   # work-mode descriptor (e.g. '在线'/Online),
                return original                    # not a real place -- leave unchanged
            return self._pick_unique(chinese_anon.CN_CITIES, 'location:' + s, norm)
        return case_like(s, self._pick_unique(CITIES, 'location:' + norm, norm))

    # -- region/state subdivision (e.g. Workday-style 'USA-CA', 'MYS-7') ---------
    def load_region_pool(self, cur, schema, tbl, region_col, country_col):
        """Populate self._region_pool[country_value] = [real region_code,...] from THIS
        table's own distinct (country, region) pairs. Used by _gen_region so a fake region
        is always another REAL, already-observed subdivision of the SAME country -- 'nearby
        and makes sense' rather than a fabricated code or a random unrelated country/region
        combo. Only covers the country values actually present here; a country never seen with
        a region value simply has no pool (region left unchanged for it -- see _gen_region).

        Filters out data-entry outliers: a region code's own leading "<ISO3>-" prefix should
        agree with the majority of that country_code's other region codes (e.g. 'BRA-SC',
        'BRA-SP', ... for BR) -- confirmed present in real data: one row has Country_Code='BR'
        paired with Region_Code='USA-AL' (a plain upstream data error, not anything this tool
        produced), which without this filter got learned into BR's pool and then handed out as
        the 'fake' for an unrelated, entirely legitimate BRA-SC row -- a real country/region
        crossed a border because the SOURCE data crossed it first. Only the majority prefix per
        country is kept; a country with no clear majority (all-distinct prefixes) keeps everything,
        same as before, since there's no basis to call any single one an outlier."""
        try:
            rows = cur.execute(
                f"SELECT DISTINCT [{country_col}], [{region_col}] FROM [{schema}].[{tbl}] "
                f"WHERE [{region_col}] IS NOT NULL AND [{country_col}] IS NOT NULL").fetchall()
        except Exception as e:
            log(f"  (warn: could not load region pool for {tbl}.{region_col}: {e})"); return
        pool = {}
        for country, region in rows:
            pool.setdefault(str(country), []).append(str(region))
        dropped = 0
        for k, regions in pool.items():
            prefixes = [r.split('-', 1)[0] for r in regions]
            counts = {}
            for p in prefixes: counts[p] = counts.get(p, 0) + 1
            majority = max(counts, key=counts.get)
            if counts[majority] > 1 and counts[majority] < len(regions):
                kept = [r for r, p in zip(regions, prefixes) if p == majority]
                dropped += len(regions) - len(kept)
                pool[k] = kept
        for k in pool: pool[k].sort()               # deterministic ordering (stable seeding)
        self._region_pool = pool
        log(f"  loaded region pool: {len(pool)} country code(s), "
            f"{sum(len(v) for v in pool.values())} region value(s) total"
            + (f" ({dropped} outlier pair(s) dropped)" if dropped else ""))

    def _gen_region(self, original, country=None):
        """Fake a region/state code -> a DIFFERENT REAL region code observed for the SAME
        country in this table (see load_region_pool) -- keeps country/region a valid, plausible
        pair instead of a random cross-country combo. Deterministic (same original+country ->
        same fake). Falls back to leaving the value unchanged if no companion country was given
        or no alternative region is known for it (safe no-op, never invents a fake code)."""
        if not country:
            return original
        pool = self._region_pool.get(country) or []
        norm = self.normalize(original)
        choices = [c for c in pool if self.normalize(c) != norm]
        if not choices:
            return original
        seed = self._seed(f"region:{country}:{norm}")
        return choices[seed % len(choices)]

    # -- persist a new mapping into mapping_xref (read-write, BATCHED) ------------
    def _persist(self, original, fake, ptype):
        label = XREF_TYPE.get(ptype, 'Other')
        nk = self.normalize(original)              # case/space-insensitive reuse key
        # in-memory update is immediate so lookups this run stay consistent
        self._xref_any.setdefault(nk, fake)
        self._xref[(label.strip().casefold(), nk)] = fake
        # id/url types are deterministic (sha256-seeded, class-preserving) and are intentionally
        # NOT persisted to mapping_xref -- storing millions of GUIDs would bloat the shared map,
        # and re-runs regenerate identical fakes without any table state (see ENGINE_CHANGES.md #4).
        if ptype in ('id', 'url'):
            self._log_outcome(original, fake, 'GENERATED-NOT-STORED')
            return
        # queue the DB write (skip oversized — nvarchar(256) limit; xref.originalvalue IS
        # nvarchar, confirmed against the live schema, so non-ASCII/Chinese text is NOT
        # skipped here -- it round-trips fine and needs to persist for cross-run reuse).
        # NEVER persist a no-op mapping (fake == original): it's not a real anonymization,
        # it pollutes the map, and the type-agnostic reuse would echo the original back.
        if (self.persist_enabled and fake is not None and original is not None
                and fake.strip().casefold() != original.strip().casefold()
                and nk not in self._persisted
                and len(original) <= MAX_LOOKUP_LEN and len(fake) <= MAX_LOOKUP_LEN):
            self._persisted.add(nk)
            self._pending_ins.append((label, original, fake))
            self._pending_ins_meta.append((self._current_col, original, fake, ptype))
            # dry-run writes to a local temp table; the real id doesn't matter -> log immediately.
            # real (shared-map) run: id is assigned on flush; emit a pending marker for now, the
            # resolved NEW:<id> line is written by flush_pending() after OUTPUT INSERTED.id.
            if not self._shared:
                self._log_outcome(original, fake, 'NEW:DRY-RUN')
            else:
                self._log_outcome(original, fake, 'NEW:pending')
        else:
            # guard-rejected: no-op, oversized, already-queued, or persist_enabled=False.
            self._log_outcome(original, fake, 'GENERATED-NOT-STORED')

    def flush_pending(self):
        """Write queued mapping inserts/updates in bulk and COMMIT. For the shared Azure map we
        open a FRESH connection here (and retry once on a dropped link) so a long-idle connection
        during slow GLiNER batches can't 08S01 us. Dry-run writes to the local temp table via the
        stable data connection. Safe to insert plainly — we preloaded the table, so queued
        originals are known-new (in-memory dedup).

        Real-run INSERT uses ``OUTPUT INSERTED.id`` in sub-batches (≤500 rows/statement so total
        params stay under SQL Server's 2100 limit) so we can log the assigned id per new row and
        also backfill self._xref_ids for same-run second lookups."""
        if not self._pending_ins and not self._pending_upd:
            return
        wt = self.write_table
        ins, upd = self._pending_ins, self._pending_upd
        meta = self._pending_ins_meta
        self._pending_ins, self._pending_upd, self._pending_ins_meta = [], [], []
        # dry-run writes to a throwaway table alongside the DATA (SCHEMA); a real run writes to
        # the shared mapping table, which always lives in MAP_SCHEMA regardless of --schema.
        sch = MAP_SCHEMA if self._shared else SCHEMA
        update_sql = f"UPDATE [{sch}].[{wt}] SET anonymizedvalue=? WHERE originalvalue=?"
        insert_sql_plain = f"INSERT INTO [{sch}].[{wt}](description,originalvalue,anonymizedvalue) VALUES (?,?,?)"

        def _do_dryrun(conn):
            cur = conn.cursor()
            try: cur.fast_executemany = True
            except Exception: pass
            if ins: cur.executemany(insert_sql_plain, ins)
            if upd: cur.executemany(update_sql, upd)
            conn.commit()

        def _do_real(conn):
            """Real-run flush: multi-row INSERT with OUTPUT so we can log the new ids and
            backfill self._xref_ids. Sub-batches to keep param count ≤ 2100."""
            cur = conn.cursor()
            if ins:
                # 500 rows × 3 cols = 1500 params — safely under the 2100-param limit
                per_stmt = 500
                for i in range(0, len(ins), per_stmt):
                    chunk = ins[i:i + per_stmt]
                    meta_chunk = meta[i:i + per_stmt]
                    values_sql = ",".join(["(?,?,?)"] * len(chunk))
                    sql = (f"INSERT INTO [{sch}].[{wt}](description,originalvalue,anonymizedvalue) "
                           f"OUTPUT INSERTED.id VALUES {values_sql}")
                    flat = [x for row in chunk for x in row]
                    cur.execute(sql, flat)
                    returned_ids = [r[0] for r in cur.fetchall()]
                    # SQL Server does not guarantee OUTPUT ordering matches VALUES order without
                    # a sorted OUTPUT INTO ... trick; empirically the ordering aligns for a simple
                    # INSERT ... VALUES, but to be safe we look up ids by (originalvalue, fake) if
                    # counts diverge. Counts should be equal for INSERT.
                    if len(returned_ids) == len(meta_chunk):
                        for (col_name, orig, fk, ptype), new_id in zip(meta_chunk, returned_ids):
                            nk = self.normalize(orig)
                            self._xref_ids[nk] = new_id
                            if self._vlog is not None and col_name is not None:
                                log_value(self._vlog, col_name, orig, fk, f'NEW:{new_id}')
                    else:
                        # fallback: at least backfill via a per-row SELECT so future lookups can log REUSE:<id>
                        for col_name, orig, fk, ptype in meta_chunk:
                            try:
                                row = cur.execute(
                                    f"SELECT TOP 1 id FROM [{sch}].[{wt}] WHERE originalvalue=? AND anonymizedvalue=? ORDER BY id DESC",
                                    orig, fk).fetchone()
                                if row and row[0] is not None:
                                    nk = self.normalize(orig)
                                    self._xref_ids[nk] = row[0]
                                    if self._vlog is not None and col_name is not None:
                                        log_value(self._vlog, col_name, orig, fk, f'NEW:{row[0]}')
                            except Exception:
                                pass
            if upd:
                try: cur.fast_executemany = True
                except Exception: pass
                cur.executemany(update_sql, upd)
            conn.commit()

        if not self._shared:                        # dry-run: local temp table, stable connection
            try: _do_dryrun(self._pconn)
            except Exception as e: log(f"  (warn: flush to {wt} failed, {str(e)[:80]})")
            return
        # shared Azure map: fresh connection, one retry on a dropped link
        for attempt in (1, 2):
            try:
                mc = connect_map(); _do_real(mc); mc.close(); return
            except Exception as e:
                if attempt == 1:
                    log(f"  (map flush retry after: {str(e)[:70]})")
                else:
                    log(f"  (warn: map flush to {wt} failed after retry: {str(e)[:80]})")

    def _backfill_name_tokens(self, canon, val):
        """When a whole 'person' name resolves via reuse-first (already persisted in
        mapping_xref from some earlier run/table), _gen_person's token-level generation never
        actually runs, so _ff_cache/_fl_cache never learn this person's per-token mapping.
        Without this, a LATER value built from the SAME name tokens independently (e.g. an
        email local-part like 'lokesh.kenche@...' faked without a same-row name_hint) draws its
        own unrelated fake tokens via _ff/_fl instead of reusing the already-established pairing
        -- confirmed empirically: reuse-first correctly resolved 'Lokesh Kenche' -> 'Matthew
        Martin' (already in mapping_xref), but the same real person's email
        'lokesh.kenche@centific.com' independently faked to an unrelated
        'manya.jalilzad@aventraa.com' since the token caches were never populated. Best-effort:
        only backfills the plain 'first [middles] last' shape _gen_person's generic tokenizer
        actually produces -- skips Han-script, bilingual, and parenthetical-compound names
        (fundamentally different structure, not a simple positional token pairing) and skips
        silently if the original/fake word counts don't line up 1:1 rather than guessing a
        wrong pairing."""
        if (chinese_anon.is_han(canon) or chinese_anon.is_han(val)
                or '(' in canon or '(' in val or '[' in canon or '[' in val):
            return
        m_title = self._TITLE_RE.match(canon)
        if m_title:
            canon = m_title.group(2)
            vm_title = self._TITLE_RE.match(val)
            val = vm_title.group(2) if vm_title else val
        o_toks = [t for t in re.split(r'\s+', canon.strip()) if t]
        f_toks = [t for t in re.split(r'\s+', val.strip()) if t]
        if not o_toks or len(o_toks) != len(f_toks):
            return
        if len(o_toks) == 1:
            n = self.normalize(o_toks[0])
            if n: self._ff_cache[n] = f_toks[0]
            return
        n0 = self.normalize(o_toks[0])
        if n0: self._ff_cache[n0] = f_toks[0]
        for ot, ft in zip(o_toks[1:-1], f_toks[1:-1]):
            n = self.normalize(ot)
            if n: self._ff_cache[n] = ft
        nl = self.normalize(o_toks[-1])
        if nl: self._fl_cache[nl] = f_toks[-1]

    # -- public: resolve one value ----------------------------------------------
    def fake(self, original, ptype, col=None, name_hint=None, id_hint=None, country_hint=None):
        if original is None: return None
        original = str(original)
        if len(original.strip()) <= 1: return original
        # mapping_xref.is_pii='N': a manual review pass flagged this exact original as NOT
        # actually PII (it only entered the map via an over-eager blanket harvest -- see
        # _load_xref's docstring for confirmed examples: 'Admin', 'A', '1 1', '?'). Leave it
        # completely unchanged rather than reusing/regenerating a fake for it.
        if self.normalize(original) in self._non_pii:
            self._log_outcome(original, original, 'NOT-PII-SKIPPED')
            return original
        # SAFETY: a value already carrying a reserved forced-fake word (e.g. '…@aventraa.com')
        # is already anonymized — return it unchanged instead of re-faking (which would persist a
        # fake as an "original" and pollute mapping_xref, e.g. when re-running on anonymized data).
        if _RESERVED_FAKE_RE.search(original):
            self._log_outcome(original, original, 'GENERATED-NOT-STORED')
            return original
        # Filename guard: fake only the stem, keep the real extension verbatim (case as written).
        # Recurses through this same fake() on the stem alone, so caching/reuse-first/mapping_xref
        # persistence all key on the stem -- the identical stem with a DIFFERENT extension
        # elsewhere still gets the same fake stem, just with its own extension reattached.
        m = looks_like_filename(original)
        if m:
            stem, ext = m.group(1), m.group(2)
            faked_stem = self.fake(stem, ptype, col=col, name_hint=name_hint, id_hint=id_hint,
                                    country_hint=country_hint)
            return (faked_stem if faked_stem is not None else stem) + ext
        # #7 harvest stopword filter: a bare single-token person/org candidate that is really a
        # generic word (email/mobile/target/gdc/…) is left unchanged and NOT persisted — this is
        # what pollutes the map and drives free-text over-match. Multi-word names are unaffected.
        # Trailing digits stripped before the check too -- placeholder/system-account values like
        # 'admin123' or 'team0012' (found sitting in a 'person'-typed create_by/update_by column)
        # are just a stopword with a numeric suffix, not a real name; without the strip only the
        # bare 'admin'/'team' matched and the digited variants leaked through as fake names.
        if ptype in ('person', 'org'):
            norm = self.normalize(original)
            if norm in WORKFLOW_LABEL_STOP:
                self._log_outcome(original, original, 'GENERATED-NOT-STORED')
                return original
            if ' ' not in original.strip():
                if (norm in HARVEST_STOP or norm.rstrip('0123456789') in HARVEST_STOP
                        or chinese_anon.is_harvest_stop(original)):
                    self._log_outcome(original, original, 'GENERATED-NOT-STORED')
                    return original
        if looks_like_id(original):        # runtime guard: shape overrides declared type —
            ptype = 'id'                   # a GUID/account-id never gets person/org treatment
        elif looks_like_url(original):     # a URL never gets its scheme/host/path mangled
            ptype = 'url'
        elif ptype not in ('email', 'freetext', 'url', 'id') and EMAIL_RE.match(original.strip()):
            # a column typed 'person' (e.g. addedBy, where SOME rows are a bilingual name +
            # embedded email but others are just a bare email with no name at all) must still
            # fake a bare-email row to another EMAIL, not a name string with no '@' -- else the
            # column ends up holding non-email-shaped garbage for exactly the rows that had
            # nothing else to key off of. The bilingual+email COMPOUND shape doesn't match here:
            # EMAIL_RE is anchored end-to-end, and the compound's leading "Name (" text/space
            # breaks that anchor, so it still routes through _gen_person as intended.
            ptype = 'email'
        # "Last, First" -> "First Last" canonicalization MUST happen here, BEFORE the reuse-first
        # cache-key/xref lookup below -- not downstream inside _gen_person, where it used to live.
        # Confirmed as a real reuse-first miss: mapping_xref already had 'Jyothi Yaga' -> a
        # specific fake from some other table/row, but THIS row spelled the same person
        # 'Yaga, Jyothi' -- normalize('Yaga, Jyothi') != normalize('Jyothi Yaga'), so the lookup
        # below never found the existing mapping and generated a completely different, unrelated
        # fake for the SAME real person. Canonicalizing first means the lookup key is the same
        # either way, regardless of which comma-order any given row happens to use.
        canon = original
        if ptype == 'person':
            m_lf = self._LAST_FIRST_RE.match(original)
            if m_lf:
                canon = f"{m_lf.group(2).strip()} {m_lf.group(1).strip()}"
        role = self._person_role(col) if ptype == 'person' else ''
        key = (ptype, self.normalize(canon), role)        # role part keeps first/last distinct
        if key in self._cache: return self._cache[key]
        def gen(o):
            if ptype == 'person': r = self._gen_person(o, col)
            elif ptype == 'email': r = self._gen_email(o, name_hint=name_hint)
            elif ptype == 'id': r = self._gen_guid(o, id_hint=id_hint)
            elif ptype == 'url': r = self._gen_url(o)
            elif ptype == 'birth': r = self._gen_birth(o)
            elif ptype == 'region': r = self._gen_region(o, country=country_hint)
            else: r = {'phone': self._gen_phone,
                       'country': self._gen_country,
                       'location': self._gen_location,
                       'org': self._gen_org}.get(ptype, self._gen_org)(o)
            return self.apply_forced(r)    # ensure centific/pactera never survives (e.g. phone-typed text)
        xref_id, val = self.xref_lookup_with_id(canon, ptype)   # 1) reuse existing mapping
        if val is not None and id_hint:
            # A compound id-type value (e.g. '<EmployeeId>_<CreateDate>') must always embed
            # THIS row's companion column's own fake for its delimited segment(s) -- a stale
            # mapping_xref entry for the exact same original string, persisted by some OTHER
            # table/column that had no id_hint (or a different one) at the time, would silently
            # desync the two columns even though both are correct-looking fakes on their own.
            # Confirmed on ResumeMapping_Resume_Record: 'Name' reused a fake that
            # ResumeMapping_Resume_ParsingLog.Name had already established for the identical
            # string, which didn't match THIS row's own EmployeeId fake. Recompute via id_hint
            # and correct the shared record if it disagrees, rather than trusting a reuse that
            # predates (and can't know about) this row's own hint.
            fresh = gen(canon)
            if fresh != val:
                self._rewrite_xref(canon, fresh, ptype)
                val = fresh
                # rewrite -> the previously-recorded xref_id now points to a different fake;
                # log the effective outcome as a REUSE of that map row (id unchanged, value replaced).
                self._log_outcome(original, val, f'REUSE:{xref_id}' if xref_id else 'GENERATED-NOT-STORED')
            else:
                self._used.add(val.strip().casefold())
                self._log_outcome(original, val, f'REUSE:{xref_id}' if xref_id else 'GENERATED-NOT-STORED')
        elif val is not None and self.prefer_clean and _DIRTY_RE.search(val):
            new = gen(canon); self._rewrite_xref(canon, new, ptype)
            val = new; self._relabeled += 1
            self._log_outcome(original, val, f'REUSE:{xref_id}' if xref_id else 'GENERATED-NOT-STORED')
        elif val is not None:
            self._used.add(val.strip().casefold())
            if ptype == 'person':
                self._backfill_name_tokens(canon, val)
            self._log_outcome(original, val, f'REUSE:{xref_id}' if xref_id else 'GENERATED-NOT-STORED')
        else:                                            # 2) generate + persist new
            val = gen(canon)
            self._persist(original, val, ptype)          # _persist emits the NEW/GEN-NOT-STORED log line
        self._cache[key] = val
        return val

    def _rewrite_xref(self, original, new, ptype):
        label = XREF_TYPE.get(ptype, 'Other')
        nk = self.normalize(original)              # case/space-insensitive reuse key
        self._xref_any[nk] = new
        self._xref[(label.strip().casefold(), nk)] = new
        self._used.add(new.strip().casefold())
        if (self.persist_enabled
                and len(original) <= MAX_LOOKUP_LEN and len(new) <= MAX_LOOKUP_LEN):
            if self.write_table == XREF:
                self._pending_upd.append((new, original))       # UPDATE the real row
            elif nk not in self._persisted:                      # dry-run: record in temp table
                self._persisted.add(nk)
                self._pending_ins.append((XREF_TYPE.get(ptype, 'Other'), original, new))


# ════════════════════════════════════════════════════════════════════════════════
#  GLiNER wrapper (lazy, optional) — used to detect embedded PII in free text
# ════════════════════════════════════════════════════════════════════════════════
class GlinerDetector:
    LABELS = ['person', 'organization', 'email', 'phone number']
    LABEL2TYPE = {'person': 'person', 'organization': 'org',
                  'email': 'email', 'phone number': 'phone'}
    # Fix B: per-label confidence bar instead of one global cutoff. GLiNER's own
    # predict_entities/batch_predict_entities take ONE threshold applied to every label in the
    # same call -- but 'organization' recall was measurably weaker than 'person'/'email' on
    # real freetext (confirmed: "Google"/"Meta" mentioned attributively -- "Meta smart
    # glasses", "Google HCLS" -- scored under a flat 0.5 while a full "Name <email>" signature
    # cleared it easily). CALL_THRESHOLD is the single low cutoff passed to the model itself
    # (so we don't lose candidates before we can even see their score); LABEL_THRESHOLD is
    # then applied per-label in Python against each candidate's own returned `score`. Loosen
    # organization first since that's the label demonstrably under-firing; keep email/phone
    # strict since they already have strong structural signal ('@', digits) and don't need help.
    CALL_THRESHOLD  = 0.25
    LABEL_THRESHOLD = {'organization': 0.3, 'person': 0.4, 'email': 0.55, 'phone number': 0.5}
    # WIN_SIZE was 4000 (chars) until this was found: the model's own max_len=384 is a WORD-TOKEN
    # cap applied INSIDE predict_entities() per call (gliner/data_processing/processor.py
    # preprocess_example -- "Sentence of length N has been truncated to 384"), separate from and
    # underneath this class's own cell-level windowing. A 4000-char window of real dense English
    # text word-tokenizes to ~700-1150 words -- every window was silently hitting that cap, so
    # entities in roughly the back 60-70% of EVERY window were dropped before detection ever ran,
    # regardless of content structure. Verified empirically against this table's real longest
    # cells: WIN_SIZE=4000 -> 10/10 windows over the cap (max 1154 words); WIN_SIZE=800/overlap=100
    # -> 0/1148 windows over the cap across the 30 longest real cells (max 327). CJK text is
    # unaffected (GLiNER's word-splitter treats a space-less CJK run as one token).
    WIN_SIZE    = 800         # GLiNER context window (chars) -- sized to the model's real
                              # word-token budget, not just an arbitrary "context window" guess
    WIN_OVERLAP = 100         # consecutive windows overlap so a boundary-straddling entity is
                              # wholly contained in at least one window
    MAX_SCAN    = 2_000_000   # cap worst-case work per cell; exceeding it is logged, never silent

    @classmethod
    def _windows(cls, text):
        """Yield (offset, substring) windows covering text[:MAX_SCAN]. Consecutive windows step by
        WIN_SIZE-WIN_OVERLAP so every WIN_SIZE-char span (hence any entity up to that size) is wholly
        inside some window. Replaces the old text[:4000] truncation that left everything past char
        4000 unscanned (real names past offset 4000 in ~2M-char HTML cells were leaking verbatim)."""
        n = min(len(text), cls.MAX_SCAN)
        step = max(1, cls.WIN_SIZE - cls.WIN_OVERLAP)
        off = 0
        while off < n:
            yield off, text[off:off + cls.WIN_SIZE]
            if off + cls.WIN_SIZE >= n:
                break
            off += step

    def __init__(self, batch_size=64):
        self.model = None; self.tried = False
        self.batch_size = batch_size          # GLiNER sub-batch (GPU: tune with --gliner-batch)
        self.device = 'cpu'

    def load(self):
        if self.tried: return self.model is not None
        self.tried = True
        try:
            from gliner import GLiNER
            log("  loading GLiNER (urchade/gliner_multi_pii-v1) …")
            self.model = GLiNER.from_pretrained('urchade/gliner_multi_pii-v1')
            # move to GPU when available — this is the whole point of running on the VM;
            # falls back to CPU cleanly if torch/CUDA is missing.
            try:
                import torch
                if torch.cuda.is_available():
                    self.model = self.model.to('cuda'); self.device = 'cuda'
                    log(f"  GLiNER on GPU (cuda:0 = {torch.cuda.get_device_name(0)}), "
                        f"sub-batch={self.batch_size}")
                else:
                    log(f"  GLiNER on CPU (no CUDA), sub-batch={self.batch_size}")
            except Exception as e:
                log(f"  GLiNER device select -> CPU ({str(e)[:60]})")
            log("  GLiNER ready")
        except Exception as e:
            log(f"  GLiNER unavailable ({str(e)[:80]}) — free-text columns need it; "
                f"structured columns still work")
            self.model = None
        return self.model is not None

    def _predict_window(self, sub):
        """Run GLiNER on one window substring; return [(start,end,type),…] in WINDOW-relative
        coords. Calls the model at the low CALL_THRESHOLD, then re-filters each candidate by
        its own score against the per-label LABEL_THRESHOLD (fix B)."""
        ents = self.model.predict_entities(sub, self.LABELS, threshold=self.CALL_THRESHOLD)
        out = []
        for e in ents:
            if e.get('score', 1.0) < self.LABEL_THRESHOLD.get(e['label'], 0.5):
                continue
            out.append((e['start'], e['end'], self.LABEL2TYPE.get(e['label'], 'person')))
        return out

    def entities(self, text):
        """Detect PII across the WHOLE cell via overlapping windows (not just text[:4000]).
        Spans are shifted to ABSOLUTE coordinates and de-duped by exact span; scrub_post skips
        overlapping spans so boundary fragments don't double-apply. Contract unchanged:
        returns [(start, end, type), …] in absolute coordinates."""
        if not self.model or not text or len(text) < 3: return []
        if len(text) > self.MAX_SCAN:
            log(f"  NOTE: free-text cell len {len(text):,} > MAX_SCAN {self.MAX_SCAN:,}; "
                f"scanning first {self.MAX_SCAN:,} chars only")
        seen = set(); out = []
        try:
            for off, sub in self._windows(text):
                for s, e, t in self._predict_window(sub):
                    a = (off + s, off + e, t)
                    if a not in seen:
                        seen.add(a); out.append(a)
        except Exception:
            return []
        out.sort()
        return out

    def entities_batch(self, texts):
        """Batch GLiNER inference over the WHOLE of each cell. Every text is split into overlapping
        windows; ALL windows across ALL texts are flattened into the same GPU sub-batches (so the
        one-forward-pass batching / --gliner-batch tuning is preserved), then window spans are shifted
        to absolute coords and mapped back to their source text (de-duped per text). Returns a list
        aligned with `texts`, each [(start,end,type),…] in absolute coordinates."""
        out = [[] for _ in texts]
        if not self.model:
            return out
        jobs = []                                   # (text_idx, offset, substring)
        for i, t in enumerate(texts):
            if not t or len(t) < 3:
                continue
            if len(t) > self.MAX_SCAN:
                log(f"  NOTE: free-text cell len {len(t):,} > MAX_SCAN {self.MAX_SCAN:,}; "
                    f"scanning first {self.MAX_SCAN:,} chars only")
            for off, sub in self._windows(t):
                jobs.append((i, off, sub))
        if not jobs:
            return out
        seen = [set() for _ in texts]
        bs = max(1, self.batch_size)
        for s in range(0, len(jobs), bs):
            chunk = jobs[s:s + bs]
            subs = [j[2] for j in chunk]
            try:
                preds = self.model.batch_predict_entities(subs, self.LABELS, threshold=self.CALL_THRESHOLD)
                for (ti, off, _sub), ents in zip(chunk, preds):
                    for e in ents:
                        if e.get('score', 1.0) < self.LABEL_THRESHOLD.get(e['label'], 0.5):
                            continue
                        a = (off + e['start'], off + e['end'], self.LABEL2TYPE.get(e['label'], 'person'))
                        if a not in seen[ti]:
                            seen[ti].add(a); out[ti].append(a)
            except Exception:                       # fallback: per-window
                for ti, off, sub in chunk:
                    for s2, e2, t2 in self._predict_window(sub):
                        a = (off + s2, off + e2, t2)
                        if a not in seen[ti]:
                            seen[ti].add(a); out[ti].append(a)
        for i in range(len(out)):
            out[i].sort()
        return out


# ════════════════════════════════════════════════════════════════════════════════
#  DB helpers
# ════════════════════════════════════════════════════════════════════════════════
SQL_SS_TIMESTAMPOFFSET = -155   # ODBC native type for SQL Server's datetimeoffset

def _handle_datetimeoffset(raw):
    """pyodbc has no built-in decoder for datetimeoffset (ODBC type -155) — without this,
    SELECTing any datetimeoffset column raises 'ODBC SQL type -155 is not yet supported'.
    Decode the raw ODBC struct into a string SQL Server can implicitly CAST back on
    INSERT/UPDATE, preserving the exact offset."""
    tup = struct.unpack("<6hI2h", raw)   # y,mo,d,h,mi,s, frac(ns), tz_h, tz_m
    return ("{0:04d}-{1:02d}-{2:02d} {3:02d}:{4:02d}:{5:02d}.{6:07d} {7:+03d}:{8:02d}"
            .format(tup[0], tup[1], tup[2], tup[3], tup[4], tup[5], tup[6] // 100,
                    tup[7], abs(tup[8])))

def connect():
    cn = pyodbc.connect(CS, timeout=7200, autocommit=False)
    cn.add_output_converter(SQL_SS_TIMESTAMPOFFSET, _handle_datetimeoffset)
    return cn

def connect_map():
    """Connection to the SHARED mapping store (obi.mapping_slice). Separate from the data
    connection so data can be local while the mapping is the remote shared source of truth.
    SHORT timeout so a network blip fails fast (flush_pending then retries/continues) instead
    of hanging on a dead socket."""
    return pyodbc.connect(MAP_CS, timeout=30, autocommit=False)

def tbl_exists(cur, name):
    return cur.execute("SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
                       "WHERE TABLE_SCHEMA=? AND TABLE_NAME=?", SCHEMA, name).fetchone()[0] > 0

def columns(cur, tbl):
    return [(r[0], r[1]) for r in cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA=? AND TABLE_NAME=? ORDER BY ORDINAL_POSITION", SCHEMA, tbl).fetchall()]

def identity_cols(cur, tbl):
    return set(r[0] for r in cur.execute(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=? AND TABLE_NAME=? "
        "AND COLUMNPROPERTY(OBJECT_ID(TABLE_SCHEMA+'.'+TABLE_NAME),COLUMN_NAME,'IsIdentity')=1",
        SCHEMA, tbl).fetchall())

def pk_cols(cur, tbl):
    return [r[0] for r in cur.execute("""
        SELECT kcu.COLUMN_NAME FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu ON tc.CONSTRAINT_NAME=kcu.CONSTRAINT_NAME
         AND tc.TABLE_SCHEMA=kcu.TABLE_SCHEMA
        WHERE tc.TABLE_SCHEMA=? AND tc.TABLE_NAME=? AND tc.CONSTRAINT_TYPE='PRIMARY KEY'
        ORDER BY kcu.ORDINAL_POSITION""", SCHEMA, tbl).fetchall()]

def pick_order_col(cur, tbl, cols):
    idc = identity_cols(cur, tbl)
    if idc: return next(iter(idc)), True
    pk = pk_cols(cur, tbl)
    if pk: return pk[0], True
    return cols[0][0], False       # fallback: first column, not guaranteed unique

def sortable_cols(cur, tbl):
    """Columns SQL Server can ORDER BY (excludes (max)/text/ntext/image/xml/blob)."""
    rows = cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA=? AND TABLE_NAME=? ORDER BY ORDINAL_POSITION", SCHEMA, tbl).fetchall()
    bad = {'text', 'ntext', 'image', 'xml', 'geography', 'geometry', 'timestamp', 'varbinary', 'binary'}
    out = []
    for name, dt, ml in rows:
        if dt in bad: continue
        if dt in ('nvarchar', 'varchar') and ml == -1: continue     # (max) not sortable
        out.append(name)
    return out

def order_by_clause(cur, tbl, override, cols):
    """Deterministic ORDER BY for OFFSET pagination + safe resume.
    Unique key (override / identity / PK) preferred; else a composite of sortable
    columns so pagination is deterministic even without a declared key."""
    if override:
        return f"[{override}]", override, True
    idc = identity_cols(cur, tbl)
    if idc:
        c = next(iter(idc)); return f"[{c}]", c, True
    pk = pk_cols(cur, tbl)
    if pk:
        return ', '.join(f"[{c}]" for c in pk), pk[0], True
    sc = sortable_cols(cur, tbl)[:12] or [cols[0][0]]
    return ', '.join(f"[{c}]" for c in sc), sc[0], False

def _keyset_is_unique(cur, tbl, key_cols):
    """Verify a candidate key GENUINELY, uniquely identifies every row of tbl right now --
    the only way it's safe to target UPDATE...WHERE by it. A key that merely 'looks like' a
    row identifier (by name, or by being some OTHER table's declared PK) can still collide
    across many rows of THIS table; trusting it without checking silently breaks in-place
    updates (see FIX_GUIDE_nonunique_fallback_key.md)."""
    total = cur.execute(f"SELECT COUNT(*) FROM [{SCHEMA}].[{tbl}]").fetchone()[0]
    if total == 0:
        return True
    if len(key_cols) == 1:
        combined = f"CAST([{key_cols[0]}] AS nvarchar(400))"
    else:
        concat_expr = ",".join(f"CAST([{c}] AS nvarchar(400))+CHAR(30)" for c in key_cols)
        combined = f"CONCAT({concat_expr})"
    distinct = cur.execute(
        f"SELECT COUNT(DISTINCT {combined}) FROM [{SCHEMA}].[{tbl}]").fetchone()[0]
    return distinct == total

def key_columns(cur, tbl, override, cols, source_tbl=None):
    """Column(s) that best-effort identify a row, for targeting in-place UPDATEs.
    Preference: override > identity > PK(tbl) > PK(source_tbl) [only if its columns exist
    here AND are verified genuinely unique on tbl's actual data -- the _anonymized copy is a
    plain schema copy that never carries the source's PK/identity CONSTRAINT over, even
    though the same columns and values are present] > composite of sortable columns, ALSO
    verified unique. Returns None (caller must check) if nothing available is actually
    unique -- silently proceeding with a colliding key means UPDATE...WHERE targets multiple
    rows at once per statement, which either no-ops or corrupts the anonymization depending
    on execution order, with no error raised (see FIX_GUIDE_nonunique_fallback_key.md)."""
    if override: return [override]
    idc = identity_cols(cur, tbl)
    if idc: return [next(iter(idc))]
    pk = pk_cols(cur, tbl)
    if pk: return pk
    col_names = {name for name, _ in cols}
    if source_tbl:
        src_pk = pk_cols(cur, source_tbl)
        if src_pk and all(c in col_names for c in src_pk) and _keyset_is_unique(cur, tbl, src_pk):
            return src_pk
    sc = sortable_cols(cur, tbl)[:12] or [cols[0][0]]
    return sc if _keyset_is_unique(cur, tbl, sc) else None

# ── state files ─────────────────────────────────────────────────────────────────
def _paths(tbl):
    os.makedirs(STATE_DIR, exist_ok=True)
    return (os.path.join(STATE_DIR, f"{tbl}.plan.json"),
            os.path.join(STATE_DIR, f"{tbl}.ckpt.json"))

def load_json(p, default=None):
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f: return json.load(f)
    return default

def _json_default(o):
    # key snapshots (run_inplace's keys.json) can include a datetime/date column when the
    # composite fallback key needs one for uniqueness (e.g. create_time) -- json.dump has no
    # built-in handler for those and raises TypeError, leaving a truncated, half-written file
    # behind (open(p,'w') already truncated it before the error hit). isoformat() round-trips
    # unambiguously and is never itself the anonymization target, so plain str-ing it is fine.
    if isinstance(o, (datetime.datetime, datetime.date)):
        return o.isoformat()
    if isinstance(o, decimal.Decimal):        # same gap -- a decimal/numeric column can end up
        return str(o)                         # in a composite fallback key too
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")

def save_json(p, obj):
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, default=_json_default)


# ════════════════════════════════════════════════════════════════════════════════
#  ANALYZE  — detect PII columns, write plan
# ════════════════════════════════════════════════════════════════════════════════
NAME_HINT  = re.compile(r'(name|firstname|lastname|fullname|employee|manager|contact|author|editor|reporter|assignee|lead|owner|consultant|resource|representative|personincharge)', re.I)
EMAIL_HINT = re.compile(r'(email|mail|upn|windowsliveid)', re.I)
PHONE_HINT = re.compile(r'(phone|mobile|fax|telephone|cell)', re.I)
ORG_HINT   = re.compile(r'(company|organization|org|account|customer|vendor|competitor|project.?name|space.?name|department|legalentity|businessunit)', re.I)
SKIP_HINT  = re.compile(r'(id$|_id|guid|code$|codes$|number$|date|time|status|flag|amount|price|rate|qty|quantity|percent|hours|url$|key$)', re.I)
# ── #5 reference / enum columns — non-PII business vocabulary that must be PRESERVED verbatim.
# These leaked as anonymized garbage in earlier slices (stepname, ProductLine, Billing_type_name,
# applicationname, ProjectGroup …) because they were mistyped person/org. Forcing them to 'skip'
# keeps the real reference values. Checked BEFORE any name/org hint.
REF_DENY   = re.compile(r'(stepname|step_name|productline|product_line|billing.?type|'
                        r'applicationname|application_name|projectgroup|project_group|'
                        r'stage.?name|typename|type_name|category.?name|groupname|group_name|'
                        r'milestone|phasename|phase_name|processname|process_name|reasonname|'
                        r'\bstatusname\b|licensetype|producttype|servicetype|contracttype)', re.I)
# ── #5 country / location columns -> real localized fake (a *different* real place), not org-scrub.
COUNTRY_HINT  = re.compile(r'(country|countryregion|countrycode|nation|address1_country)', re.I)
LOCATION_HINT = re.compile(r'(^city$|_city|address1_city|worklocation|deliveryname|region$|'
                           r'state$|province|locationname|location_name)', re.I)
EMAIL_RE   = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
PHONE_RE   = re.compile(r'^[+()\d][\d\s\-()]{5,}$')

# ── #7 harvest-time stopword filter — single generic tokens that GLiNER sometimes tags as a
# person/org (email, mobile, target, gdc, …). Persisting them as fakes pollutes the map and
# causes free-text over-match (the earlier 84k-replacement / 1,410-generic-row incident). A
# single-token person/org candidate whose normalized form is in this set is LEFT UNCHANGED and
# never persisted. Multi-word names ("Target Corporation") are unaffected — only bare tokens.
HARVEST_STOP = {
    'email', 'emails', 'mail', 'e-mail', 'mobile', 'phone', 'fax', 'cell', 'contact',
    'target', 'gdc', 'name', 'names', 'address', 'number', 'info', 'admin', 'user',
    'team', 'teams', 'company', 'org', 'account', 'customer', 'vendor', 'support',
    'sales', 'service', 'services', 'manager', 'owner', 'lead', 'leads', 'project',
    'group', 'region', 'country', 'city', 'state', 'date', 'time', 'status', 'type',
    'code', 'id', 'test', 'null', 'none', 'unknown', 'other', 'department', 'title',
    'string', 'sample', 'placeholder', 'default', 'system', 'guest',
    # department/role-queue approvers (a workflow step approved by a team, not a named
    # individual) -- found in clm_contract_flow_instance_node.approver_name alongside real
    # people ('Legal', 'InfoSec' sitting next to 'Heather Lin', 'Emma Chen'); not a person's
    # name at all, so faking it as one is wrong, not just imprecise.
    'legal', 'infosec', 'pricing', 'finance', 'hr', 'procurement', 'compliance',
    'audit', 'engineering', 'marketing', 'operations', 'security',
    # service/automation account username (obip1.sift_bus_resume.create_by: 'airdc2' sitting
    # alongside 'team0012'/'admin'/'管理员' in the same column's distinct-value list -- a
    # system account, not a real person's name).
    'airdc',
}

# EXACT multi-word system/bot labels sitting in an otherwise-real-names 'person' column (e.g.
# crm_itticket.aip_workbyname: 'PA Workflow' on 9,762/43,671 rows, '# ASK IT Workflow' on 3 --
# a Power Automate workflow's own system identity, not a support agent -- mixed in with 104
# real employee full names). HARVEST_STOP above only ever applied to single-token values (a
# deliberate scope limit so real multi-word names are never at risk); this is a SEPARATE, exact
# full-string match against a short, explicitly-vetted list, so it can safely cover multi-word
# labels without widening HARVEST_STOP's own single-token guarantee.
WORKFLOW_LABEL_STOP = {
    'pa workflow', '# ask it workflow',
}

def zip_name_tokens(original, fake):
    """Positional original-token -> fake-token pairs for a person value already faked as a
    WHOLE string (e.g. via engine.fake(val, 'person', ...)). _gen_person maps word order/count
    1:1 (bilingual "Latin （Chinese）" values excepted -- handled via their own latin/chinese
    split), so zipping the two token lists gives an exact fragment-level map that can never
    disagree with what's already sitting in the faked column -- no separate/re-derived fake.
    Used for ROW-SCOPED fragment scrubbing (see _row_person_tokens in run()): a bare fragment
    like 'Jacky' glued into a filename in the SAME row as a faked 'Jacky Kang' resolves to that
    row's own already-established fake, without assuming some OTHER row's same-named person
    is the same individual."""
    bi_o = chinese_anon.split_bilingual(original)
    bi_f = chinese_anon.split_bilingual(fake)
    if bi_o and bi_f:
        lat_o, chi_o, _, _, _ = bi_o
        lat_f, chi_f, _, _, _ = bi_f
        pairs = list(zip(lat_o.split(), lat_f.split()))
        pairs.append((chi_o, chi_f))
        return pairs
    return list(zip(original.split(), fake.split()))

def analyze(cur, tbl, sample_rows, gl):
    if not tbl_exists(cur, tbl):
        log(f"source table [{SCHEMA}].[{tbl}] not found"); return
    cols = columns(cur, tbl)
    total = cur.execute(f"SELECT COUNT(*) FROM [{SCHEMA}].[{tbl}] WITH (NOLOCK)").fetchone()[0]
    ordc, uniq = pick_order_col(cur, tbl, cols)
    log("=" * 78)
    log(f"ANALYZE {tbl}  rows={total:,}  cols={len(cols)}  order_key=[{ordc}] "
        f"({'unique' if uniq else 'NON-UNIQUE — set --order-col for safe resume'})")

    plan_cols = []
    # nchar/char (fixed-width) included alongside the variable-width types -- without them, a
    # fixed-width text column (e.g. Candidate_WID nchar(32), a real Workday WID code) is silently
    # skipped by analyze() entirely (no log line, no plan entry at all), even though run()/
    # run_inplace() handle nchar/char columns fine once a plan entry exists for one.
    text_types = ('nvarchar', 'varchar', 'text', 'ntext', 'nchar', 'char')
    for name, dtype in cols:
        if dtype in NUMERIC_TYPES:
            # numeric price/quantity -> ±15% jitter candidate (disabled by default)
            if not (AMOUNT_HINT.search(name) and not AMOUNT_SKIP.search(name)):
                continue
            try:
                nvals = [r[0] for r in cur.execute(
                    f"SELECT TOP 3 [{name}] FROM [{SCHEMA}].[{tbl}] WITH (NOLOCK) "
                    f"WHERE [{name}] IS NOT NULL").fetchall()]
            except Exception:
                nvals = []
            plan_cols.append({'column': name, 'type': 'amount', 'mode': 'structured',
                              'enabled': False, 'confidence': 0.5,
                              'reason': 'numeric amount/quantity -> ±15% jitter (review)',
                              'samples': [str(v)[:40] for v in nvals]})
            continue
        if dtype not in text_types:
            continue
        try:
            vals = [r[0] for r in cur.execute(
                f"SELECT TOP {sample_rows} [{name}] FROM [{SCHEMA}].[{tbl}] WITH (NOLOCK) "
                f"WHERE [{name}] IS NOT NULL AND LEN([{name}])>1").fetchall()]
        except Exception:
            vals = []
        if not vals:
            continue
        avg_len = sum(len(str(v)) for v in vals) / len(vals)
        emails = sum(1 for v in vals if EMAIL_RE.match(str(v).strip()))
        phones = sum(1 for v in vals if PHONE_RE.match(str(v).strip()))
        idlike = sum(1 for v in vals if looks_like_id(str(v).strip()))
        urllike = sum(1 for v in vals if looks_like_url(str(v).strip()))
        ptype, mode, conf, reason = None, 'structured', 0.0, ''

        if REF_DENY.search(name):
            # #5 reference/enum column (stepname, ProductLine, Billing_type_name, …) — NON-PII
            # business vocabulary. Force 'skip' so the real value is preserved, never anonymized.
            ptype, conf, reason = 'skip', 0.0, 'reference/enum column — preserve verbatim (deny-list)'
        elif COUNTRY_HINT.search(name):
            ptype, conf, reason = 'country', 0.7, 'country column -> different real country (localized)'
        elif LOCATION_HINT.search(name):
            ptype, conf, reason = 'location', 0.6, 'location/city column -> different real place'
        elif EMAIL_HINT.search(name) or emails / len(vals) > 0.3:
            ptype, conf, reason = 'email', max(0.6, emails/len(vals)), f"{emails}/{len(vals)} look like emails"
        elif PHONE_HINT.search(name) or phones / len(vals) > 0.3:
            ptype, conf, reason = 'phone', max(0.6, phones/len(vals)), f"{phones}/{len(vals)} look like phones"
        elif idlike / len(vals) > 0.5:
            # BEFORE name/org hints so GUID-shaped "author_id"/"lead_id" aren't mistyped person/org.
            # Off by default (a policy call) but correctly typed 'id' if the user enables it.
            ptype, conf, reason = 'id', 0.3, f"{idlike}/{len(vals)} look like GUID/account-id — opaque identifier"
        elif urllike / len(vals) > 0.5:
            # BEFORE the 'url$' SKIP_HINT; type='url' keeps the URL intact vs org-faking it.
            ptype, conf, reason = 'url', 0.3, f"{urllike}/{len(vals)} look like URLs — format-preserving"
        elif avg_len > 80:
            # free-text candidate — confirm with GLiNER on a few samples
            ptype, mode, conf, reason = 'freetext', 'freetext', 0.5, f"avg_len={avg_len:.0f}"
            if gl.load():
                hits = sum(1 for v in vals[:20] if gl.entities(str(v)))
                conf = min(0.95, 0.4 + hits / 20)
                reason += f"; GLiNER found entities in {hits}/20 samples"
        elif NAME_HINT.search(name):
            ptype, conf, reason = 'person', 0.7, 'name-like column'
        elif ORG_HINT.search(name):
            ptype, conf, reason = 'org', 0.6, 'org-like column'
        elif not SKIP_HINT.search(name):
            # ambiguous text col — probe GLiNER on samples
            if gl.load():
                labels = {}
                for v in vals[:20]:
                    for _, _, t in gl.entities(str(v)): labels[t] = labels.get(t, 0) + 1
                if labels:
                    top = max(labels, key=labels.get)
                    ptype, conf = top, 0.5
                    reason = f"GLiNER majority={top} ({labels})"

        if not ptype:                       # keep as a DISABLED menu entry so the
            ptype = 'org' if not SKIP_HINT.search(name) else 'skip'   # user can flip it on
            reason = reason or ('looks like id/code/number/date — off by default'
                                if SKIP_HINT.search(name) else 'no PII signal — review')
        plan_cols.append({
            'column': name, 'type': ptype, 'mode': mode,
            'enabled': bool(conf >= 0.55), 'confidence': round(conf, 2),
            'reason': reason,
            'samples': [str(v)[:60] for v in vals[:3]],
        })

    plan = {'table': tbl, 'rows': total, 'order_col': ordc, 'order_unique': uniq,
            'generated_at': str(datetime.datetime.now()),
            'columns': sorted(plan_cols, key=lambda c: (-c['confidence'], c['column']))}
    pp, _ = _paths(tbl); save_json(pp, plan)

    log("-" * 78)
    log(f"{'column':32}{'type':10}{'mode':11}{'en':4}{'conf':6} reason / samples")
    log("-" * 78)
    for c in plan['columns']:
        log(f"{c['column'][:31]:32}{c['type']:10}{c['mode']:11}"
            f"{'Y' if c['enabled'] else 'n':4}{c['confidence']:<6} {c['reason']}")
        log(f"{'':32}samples={c['samples']}")
    log("-" * 78)
    log(f"Plan written -> {pp}")
    log("EDIT that file: set \"enabled\": true/false and fix \"type\" per column, then:")
    log(f"   run {tbl} --limit 100      (sample)   ->   run {tbl} --limit all")


# ════════════════════════════════════════════════════════════════════════════════
#  RUN  — build target, anonymize enabled columns, checkpointed + resumable
# ════════════════════════════════════════════════════════════════════════════════
_PAUSE = {'stop': False}
def _sigint(sig, frm):
    log("… pause requested — finishing current batch then saving checkpoint")
    _PAUSE['stop'] = True

def ensure_target(cur, source, anon):
    """Create the output table `anon` as an empty schema copy of `source` if missing."""
    if not tbl_exists(cur, anon):
        log(f"  creating [{SCHEMA}].[{anon}] (empty schema copy of {source})")
        cur.execute(f"SELECT * INTO [{SCHEMA}].[{anon}] FROM [{SCHEMA}].[{source}] WHERE 1=0")
        cur.connection.commit()
    return anon

def run(cur, tbl, limit, restart, order_override, gl, prefer_clean=False, dry_run=False,
        out_suffix='_anonymized', only_columns=None, batch_size=None):
    batch_size = batch_size or BATCH
    pp, cp = _paths(tbl)
    plan = load_json(pp)
    if not plan:
        log(f"no plan for {tbl} — run `analyze {tbl}` first"); return
    if only_columns:
        wanted = [c.strip() for c in only_columns.split(',') if c.strip()]
        by_name = {c['column'].lower(): c for c in plan['columns']}
        missing = [c for c in wanted if c.lower() not in by_name]
        if missing:
            log(f"  no plan entry for column(s) {missing} — check spelling or re-run `analyze {tbl}`"); return
        enabled = [by_name[c.lower()] for c in wanted]
        log(f"  --columns override: anonymizing exactly {wanted} (ignoring plan 'enabled' flags)")
    else:
        enabled = [c for c in plan['columns'] if c.get('enabled')]
    if not enabled:
        log("no columns enabled in the plan — edit the plan first, or pass --columns col1,col2"); return

    # entity base = source name minus a trailing _anonymized (so X_anonymized -> X)
    base = re.sub(r'_anonymized$', '', tbl)
    if dry_run:
        anon = base + '_script'
    else:
        anon = base + out_suffix
        if anon.lower() == tbl.lower():
            log(f"  STOP: output table [{anon}] == source. Pass --out-suffix (e.g. _clean) "
                f"so we don't overwrite the source while reading it."); return
    anon = ensure_target(cur, tbl, anon)
    cols = columns(cur, tbl)
    order_sql, ordc, ord_uniq = order_by_clause(cur, tbl, order_override, cols)
    if not ord_uniq:
        log(f"  NOTE: no unique key — ordering by a composite of sortable columns for "
            f"deterministic resume. For big tables pass --order-col <unique col>.")
    insert_cols = [c for c, d in cols if d != 'timestamp']
    idc = identity_cols(cur, anon)
    # column char widths — a generated fake must fit the target column or the INSERT fails
    maxlen = {r[0]: r[2] for r in cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA=? AND TABLE_NAME=?", SCHEMA, tbl).fetchall()}

    enabled_map = {c['column'].lower(): c for c in enabled}
    freetext = [c for c in enabled if c.get('mode') == 'freetext']
    if freetext: gl.load()

    total = cur.execute(f"SELECT COUNT(*) FROM [{SCHEMA}].[{tbl}] WITH (NOLOCK)").fetchone()[0]
    present = cur.execute(f"SELECT COUNT(*) FROM [{SCHEMA}].[{anon}] WITH (NOLOCK)").fetchone()[0]

    dry_map = base + '_script_map'                  # isolated temp mapping table for dry runs
    if dry_run:
        # DRY RUN: throwaway <table>_script sample, always rebuilt fresh. The shared
        # mapping_xref is only READ (for reuse). NEW original->fake pairs are written to
        # the isolated <table>_script_map table instead — teammates are never affected.
        n = total if str(limit).lower() in ('all', 'complete', 'full') else int(limit)
        target_total = min(total, n)
        if present > 0:
            log(f"  dry-run: TRUNCATE {anon} (rebuilding fresh sample)")
            cur.execute(f"TRUNCATE TABLE [{SCHEMA}].[{anon}]"); cur.connection.commit()
        # (re)create the temp mapping table, same shape as mapping_xref's core columns
        if tbl_exists(cur, dry_map):
            cur.execute(f"TRUNCATE TABLE [{SCHEMA}].[{dry_map}]")
        else:
            log(f"  dry-run: creating temp map table [{SCHEMA}].[{dry_map}]")
            cur.execute(f"CREATE TABLE [{SCHEMA}].[{dry_map}] "
                        f"(description nvarchar(50), originalvalue nvarchar(256), anonymizedvalue nvarchar(256))")
        cur.connection.commit()
        ckpt = {'done': 0, 'table': tbl}
        cp = None                                   # never persist a checkpoint for dry runs
    else:
        ckpt_file = load_json(cp)                   # None if this tool never ran here
        # v2: if the target already exists WITH ROWS, anonymize ONLY the enabled columns
        # IN PLACE on it (UPDATE, keyed on PK/unique id) — never rebuild/drop. This is the
        # incremental column-level anonymization requested for obi-sql-db tables that already
        # carry an anonymized copy from earlier work. (--restart forces a fresh rebuild instead.)
        if VERSION == 2 and not restart and present > 0:
            log(f"  [v2] {anon} already exists with {present:,} row(s) -> UPDATE-IN-PLACE on "
                f"enabled cols {[c['column'] for c in enabled]} (existing copy not rebuilt)")
            run_inplace(cur, tbl, anon, enabled, limit, order_override, gl, prefer_clean=prefer_clean)
            return
        if restart:
            log(f"  --restart: TRUNCATE {anon} + reset checkpoint")
            cur.execute(f"TRUNCATE TABLE [{SCHEMA}].[{anon}]"); cur.connection.commit()
            ckpt = {'done': 0, 'table': tbl}
        elif ckpt_file is None and present > 0:
            # target has rows we did NOT create (e.g. the old flawed pipeline). Do not
            # silently trust them — this is a from-scratch tool.
            if only_columns:
                log(f"  [{anon}] already has {present:,} row(s) (no checkpoint) — --columns given, "
                    f"so anonymizing {[c['column'] for c in enabled]} IN PLACE (no drop/truncate).")
                run_inplace(cur, tbl, anon, enabled, limit, order_override, gl, prefer_clean=prefer_clean)
                return
            log(f"  STOP: [{anon}] already has {present:,} rows but no checkpoint from this tool.")
            log(f"        These are foreign/old rows. To redo fresh:  run {tbl} --limit <N> --restart")
            log(f"        (that truncates {anon} and starts clean). Refusing to continue.")
            log(f"        Or pass --columns col1,col2 to anonymize those columns IN PLACE (no rebuild).")
            return
        else:
            ckpt = ckpt_file or {'done': 0, 'table': tbl}
            if present != ckpt['done']:             # our own resume — trust actual rows
                log(f"  resume reconcile: checkpoint {ckpt['done']} -> {present} rows in target")
                ckpt['done'] = present

        if str(limit).lower() in ('all', 'complete', 'full'):
            target_total = total
        else:
            target_total = min(total, ckpt['done'] + int(limit))

    if ckpt['done'] >= target_total:
        log(f"  nothing to do — {ckpt['done']:,} rows already processed "
            f"(target {target_total:,}). Use --limit all to continue or --restart to redo.")
        return

    log("=" * 78)
    mode = "DRY-RUN (no mapping_xref writes)" if dry_run else "RUN"
    log(f"{mode} {tbl} -> {anon}  | enabled cols: {[c['column'] for c in enabled]}")
    log(f"  order_by={order_sql}  already_done={ckpt['done']:,}  this_run_until={target_total:,}  total={total:,}")

    vlog = open_value_log(tbl, dry_run)   # logs/<tbl>/{dryrun,runv<version>}.log
    engine = FakeEngine(cur, prefer_clean=prefer_clean, persist_enabled=True,
                        write_table=(dry_map if dry_run else XREF))
    engine.attach_log(vlog)   # fake()/_persist()/flush_pending() emit per-cell log lines with REUSE:<id>/NEW:<id>
    if dry_run:
        log(f"  dry-run: new original->fake pairs go to [{SCHEMA}].[{dry_map}] "
            f"(mapping_xref is read-only)")
    if prefer_clean:
        log("  --prefer-clean: legacy hex/number-suffixed xref fakes will be regenerated clean")
    have_freetext = any(c.get('mode') == 'freetext' for c in enabled)
    have_email_person = (any(c.get('type') == 'email' and c.get('mode') != 'freetext' for c in enabled)
                         and any(c.get('type') == 'person' and c.get('mode') != 'freetext' for c in enabled))

    # domain-scoped freetext hardening (fixes C/A/D) -- see FIX_GUIDE_freetext_domain_scope_
    # and_forced_company_map.md. `domain` gates KNOWN_BRAND_STOP in scrub_post; the company
    # forced-map is the deterministic backstop applied on top of GLiNER regardless of domain.
    domain = table_domain(base)
    company_forced_map = load_company_forced_map() if have_freetext else {}
    company_pattern_cache = {}   # compiled once, reused across every cell (see apply_literal_map)
    for c in enabled:                          # 'region' columns: build the real region pool
        if c.get('type') == 'region' and c.get('country_column'):
            engine.load_region_pool(cur, SCHEMA, tbl, c['column'], c['country_column'])

    # email_value -> (first_hits, last_hits) UNION across every row that carries this exact
    # email, populated below by _collect_email_hints before any row is actually faked. Without
    # this, the SAME email recurring across multiple rows (e.g. a candidate applying to several
    # job requisitions) gets its mask decided by whichever row happens to be processed FIRST --
    # confirmed empirically: candidate C143423 has First_Name='Chinnolla Sathvik' on one row but
    # just 'Chinnolla' on another (real source-data inconsistency, same as the cand_consistency
    # cases elsewhere), both sharing email 'chinnollasathvik10@gmail.com'. Whichever row's
    # (incomplete) name data got hit first left 'sathvik' sitting in the fake email as real,
    # unmasked text -- reuse-first then locked that same incomplete fake in for BOTH rows. Since
    # fake() resolves a given email value exactly once and reuses it everywhere after, the fix
    # has to happen before the FIRST resolution: merge every row's hint for that email into one
    # union first, so whichever row triggers the actual fake() call already has full coverage.
    email_hint_union = {}
    def _merge_hint_list(dst, src):
        seen = {t for t, _ in dst}
        for t, w in (src or []):
            if t not in seen:
                dst.append((t, w)); seen.add(t)

    def _row_name_hint_core(email_value, row):
        """Return (first_hits, last_hits) -- lists of (token, is_whole) pairs from the companion
        person column(s) in this SAME row that are actually present in the email's local part,
        for _gen_email to substitute via substring replacement. `is_whole` says whether that
        token IS the entire field's value (a single-word column, e.g. Last_Name='Verma') or just
        one word of a multi-word field (e.g. 'Grace' inside First_Name='Charisse Grace') -- see
        the long note in _gen_email's hint branch for why that distinction matters (it decides
        whether the substitution goes through fake()'s reuse-first or the field's own direct
        per-token _ff/_fl resolution, so it never disagrees with what the column itself fakes to).
        Two routes, tried in order:
        1) ONE enabled person column holds a full "First Last" name whose two tokens exactly
           match the email's own separator-split segments (order-independent) -- lets _gen_email
           trust the name's first/last roles instead of guessing from the email's own segment
           order (fixes orgs that use lastname.firstname@... locally). Always multi-word (that's
           what qualifies it for this route), so is_whole=False for both.
        2) Otherwise (route 1 found nothing -- e.g. first/last are SEPARATE columns, so no
           single column ever holds both tokens together, or the email has no separators to
           split on at all, e.g. concatenated personal emails like
           'paulchriscampbell507@gmail.com'): a CONTAINMENT check against separate first-role/
           last-role person columns -- does the email's local part actually contain that
           column's name token(s) as a substring? Checked per-token (not the whole field), and
           EVERY matching token is collected (not just the first) -- a multi-word field like
           Last_Name='Chisom Berenice' may have MORE than one of its own words embedded in the
           email; capping at one match per role left the other word's real text sitting in the
           fake output unchanged (a confirmed leak). Either list may come back empty if that
           side isn't actually present -- _gen_email handles a partial hint by substituting just
           what was found."""
        local = email_value.split('@')[0]
        eparts = {p.strip('.').lower() for p in re.split(r'[._\-]+', local) if p}
        if len(eparts) >= 2:
            for i, cn in enumerate(insert_cols):
                c = enabled_map.get(cn.lower())
                if c is None or c.get('mode') == 'freetext' or c.get('type') != 'person':
                    continue
                if row[i] is None:
                    continue
                pt = _person_tokens(str(row[i]))
                if pt and {pt[0].lower(), pt[1].lower()} == eparts:
                    return ([(pt[0], False)], [(pt[1], False)])
        local_clean = re.sub(r'[^a-z0-9]', '', local.lower())
        fn_hits, ln_hits = [], []
        for i, cn in enumerate(insert_cols):
            c = enabled_map.get(cn.lower())
            if c is None or c.get('mode') == 'freetext' or c.get('type') != 'person' or row[i] is None:
                continue
            role = FakeEngine._person_role(cn)
            if role not in ('first', 'last'):
                continue
            toks = re.split(r'\s+', str(row[i]).strip())
            is_whole = len(toks) == 1
            for tok in toks:
                tk = re.sub(r'[^a-z0-9]', '', tok.lower())
                if len(tk) >= 3 and tk in local_clean:
                    (fn_hits if role == 'first' else ln_hits).append((tok, is_whole))
        return (fn_hits, ln_hits) if (fn_hits or ln_hits) else None

    def _collect_email_hints(row):
        """Pre-pass, once per row: fold this row's own _row_name_hint_core result for every
        enabled email column into email_hint_union, keyed by the email's raw value. Must run to
        completion over EVERY row before any email is actually faked -- see email_hint_union's
        definition above for why."""
        for i, cn in enumerate(insert_cols):
            c = enabled_map.get(cn.lower())
            if c is None or c.get('mode') == 'freetext' or c.get('type') != 'email' or row[i] is None:
                continue
            s = str(row[i]).strip()
            if '@' not in s:
                continue
            hint = _row_name_hint_core(s, row)
            if not hint:
                continue
            fn_h, ln_h = hint
            u = email_hint_union.setdefault(s, ([], []))
            _merge_hint_list(u[0], fn_h)
            _merge_hint_list(u[1], ln_h)

    def _row_name_hint(email_value, row):
        """Public entry point: prefer the UNION of hints gathered across every row sharing this
        email (see email_hint_union) over this one row's own (possibly incomplete) view."""
        u = email_hint_union.get(email_value)
        if u and (u[0] or u[1]):
            return u
        return _row_name_hint_core(email_value, row)

    def _row_id_hint_pool(row):
        """Pre-pass, once per row: establish fakes for every enabled id-type column whose OWN
        raw value has NO `_`/`-` delimiter (an 'atomic' reference/company code, e.g.
        DATAAREAID='UWHS'). Must run BEFORE any compound (delimited) id-type value in the row is
        faked -- calling engine.fake() on a compound value here (to build some OTHER column's
        hint) would cache it prematurely, without a hint, before its own turn ever sees one.
        Restricting this pre-pass to atomic values only avoids that cache-poisoning trap: a
        compound value (e.g. PURCHASEORDER='UWHS_PO000189') is computed exactly once, in its own
        turn below, with the atomic pool already available as its id_hint."""
        pool = {}
        for i, cn in enumerate(insert_cols):
            c = enabled_map.get(cn.lower())
            if c is None or c.get('mode') == 'freetext' or c.get('type') != 'id':
                continue
            if row[i] is None:
                continue
            raw_val = str(row[i])
            if len(raw_val) < 2 or re.search(r'[_\-]', raw_val):
                continue                      # compound values: computed in their own turn below
            pool[raw_val] = engine.fake(raw_val, 'id', col=cn)
        return pool or None

    def _row_person_tokens(row, known):
        """ROW-SCOPED fragment map: token -> fake token, built ONLY from THIS row's own enabled
        person columns (never the global `known` harvest). A bare name fragment quoted elsewhere
        in the SAME row (e.g. a customer's first name glued into that row's own contract/
        attachment-title text) resolves to THIS row's already-established fake -- it never
        assumes some OTHER row's same-named person is the same individual, which a
        global/table-wide token index would silently do. See zip_name_tokens for why the
        fragment fake is guaranteed consistent with the already-faked full value."""
        toks = {}
        for i, cn in enumerate(insert_cols):
            c = enabled_map.get(cn.lower())
            if c is None or c['mode'] == 'freetext' or c['type'] != 'person' or row[i] is None:
                continue
            s = str(row[i]).strip()
            fake = known.get(s)
            if not fake:
                continue
            for o, f in zip_name_tokens(s, fake):
                on = o.strip()
                if len(on) >= 2 and on.lower() not in HARVEST_STOP and f and f != o:
                    toks[on] = f
        return toks

    def _accumulate_known(row, known):
        """Collect participant PII (name/email) originals -> consistent fakes from a row, into
        the shared `known` dict: structured person/email columns + name/address values regex-
        extracted from recipient/raw JSON free-text columns. Built GLOBALLY across all rows
        (see pre-pass below) so a name that is a participant in one row is also masked where it
        is merely quoted in another row's body — closing GLiNER recall gaps deterministically."""
        for i, cn in enumerate(insert_cols):
            c = enabled_map.get(cn.lower())
            if c is None or row[i] is None:
                continue
            if c['mode'] != 'freetext' and c['type'] == 'person':
                s = str(row[i]).strip()
                # same 2-vs-3 length floor as the freetext _JSON_NAME harvest below -- a real
                # 2-character Chinese name (e.g. create_by='王晶') must still be eligible for
                # cross-column reuse.
                min_len = 2 if chinese_anon.is_han(s) else 3
                if len(s) >= min_len and not _FILE_RE.search(s) and s not in known:
                    known[s] = engine.fake(s, 'person', col=cn)
            elif c['mode'] != 'freetext' and c['type'] == 'email':
                s = str(row[i]).strip()
                if '@' in s and s not in known:
                    known[s] = engine.fake(s, 'email', name_hint=_row_name_hint(s, row))
            elif c['mode'] == 'freetext':
                s = str(row[i])
                for nm in _JSON_NAME.findall(s):
                    nm = nm.strip()
                    is_han_name = chinese_anon.is_han(nm)
                    # a pure-Chinese name (e.g. '黄泽维') has ZERO Latin letters -- the old
                    # Latin-only check silently dropped every such name from the harvest, so a
                    # majority-Chinese-named table (e.g. sift_bus_resume) never got cross-column
                    # reuse for its candidates' names at all. Accept either a Latin letter OR Han
                    # script.
                    # SEPARATELY, the length floor: a real Chinese name is very commonly just 2
                    # characters (1-char surname + 1-char given name, e.g. '张洁', '陈阳') -- the
                    # blanket `3 <= len(nm)` floor (a reasonable guard against short, generic
                    # LATIN tokens) silently excluded EVERY 2-character Chinese name from the
                    # harvest too. Confirmed as a real, high-volume leak: in a 1000-row sample,
                    # 21 candidates' real 2-character names survived unmasked in
                    # jd_match_summary and 25 in resume_content, purely because they never made
                    # it into `known` for cross-column reuse (and GLiNER's own Chinese coverage
                    # didn't independently catch them either). Han-script names get a floor of 2;
                    # non-Han (Latin) keeps the original floor of 3.
                    min_len = 2 if is_han_name else 3
                    if (min_len <= len(nm) <= 80 and (re.search(r'[A-Za-z]', nm) or is_han_name)
                            and not _FILE_RE.search(nm) and nm not in known):
                        known[nm] = engine.fake(nm, 'person')
                for em in _JSON_ADDR.findall(s):
                    em = em.strip()
                    if '@' in em and em not in known:
                        known[em] = engine.fake(em, 'email')

    col_idx = {cn.lower(): i for i, cn in enumerate(insert_cols)}

    def _row_filter_passes(c, row):
        """A plan column may carry "row_filter": {"column": "<other col>", "in": [...]} when
        the SAME column holds different KINDS of data depending on another column's value in
        that row -- e.g. crm_itticket_Dict.Name is a real person's name when Type is
        'Customer'/'workedby', but a category label ('Critical', 'IAM', 'Tier 3') for every
        other Type. There's no per-row plan mechanism otherwise, so without this a column-wide
        'person' type would fake the category labels too, corrupting real lookup data. Returns
        True (anonymize this row) when there's no filter, the filter column isn't found, or its
        value is in the allowed set; False means leave this row's value UNTOUCHED for this
        column."""
        rf = c.get('row_filter')
        if not rf or row is None:
            return True
        idx = col_idx.get(rf['column'].lower())
        if idx is None:
            return True
        rowval = row[idx]
        return rowval is not None and str(rowval) in rf.get('in', [])

    def anon_val(colname, value, known, row=None, id_hint_pool=None):
        c = enabled_map.get(colname.lower())
        if c is None or value is None: return value
        if not _row_filter_passes(c, row):
            return value
        engine.set_log_context(colname)   # every log line emitted inside fake() carries this col
        if c['type'] == 'amount':                     # numeric price/qty -> ±15% jitter
            v = jitter_amount(value)
            engine._log_outcome(str(value), v, 'GENERATED-NOT-STORED')  # amount bypasses fake()
            return v
        if c['mode'] == 'freetext':
            return scrub_text(str(value), gl, engine, known, domain=domain, company_map=company_forced_map)
        hint = _row_name_hint(str(value), row) if (c['type'] == 'email' and row is not None
                                                   and '@' in str(value)) else None
        id_hint = id_hint_pool if c['type'] == 'id' else None
        country_hint = None
        if c['type'] == 'region' and row is not None and c.get('country_column'):
            cidx = col_idx.get(c['country_column'].lower())
            if cidx is not None and row[cidx] is not None:
                country_hint = str(row[cidx])
        return engine.fake(value, c['type'], col=colname, name_hint=hint, id_hint=id_hint,
                            country_hint=country_hint)   # colname -> role

    read_cur = connect().cursor(); read_cur.setoutputsize(10_000_000)
    col_list = ', '.join(f"[{c}]" for c in insert_cols)
    place    = ', '.join('?' for _ in insert_cols)

    # GLOBAL known-participant harvest (free-text tables only): one pass over ALL target rows to
    # build the full name/email dictionary, so the per-cell sweep masks a participant wherever it
    # appears — including rows where GLiNER didn't tag it. Cheap for the small free-text tables.
    global_known = {}
    if have_freetext:
        hcur = connect().cursor(); hcur.setoutputsize(10_000_000)
        hrows = hcur.execute(
                f"SELECT {col_list} FROM [{SCHEMA}].[{tbl}] ORDER BY {order_sql} "
                f"OFFSET 0 ROWS FETCH NEXT {target_total} ROWS ONLY").fetchall()
        hcur.connection.close()
        if have_email_person:                  # must fully populate BEFORE any row is faked below
            for hrow in hrows: _collect_email_hints(hrow)
        for hrow in hrows:
            _accumulate_known(hrow, global_known)
        engine.flush_pending(); cur.connection.commit()   # persist harvested pairs up front
        log(f"  harvested {len(global_known):,} known participant name/email originals "
            f"(global sweep over {target_total:,} rows)")
    elif have_email_person:
        # no freetext harvest to piggyback on -- a dedicated (cheap: no GLiNER) pass just to
        # populate email_hint_union before the main per-row loop below faces its first email.
        hcur = connect().cursor(); hcur.setoutputsize(10_000_000)
        for hrow in hcur.execute(
                f"SELECT {col_list} FROM [{SCHEMA}].[{tbl}] ORDER BY {order_sql} "
                f"OFFSET 0 ROWS FETCH NEXT {target_total} ROWS ONLY").fetchall():
            _collect_email_hints(hrow)
        hcur.connection.close()
        log(f"  pre-scanned {target_total:,} rows for email/name consistency ({len(email_hint_union):,} distinct emails)")

    if idc: cur.execute(f"SET IDENTITY_INSERT [{SCHEMA}].[{anon}] ON")

    ft_idx = [i for i, cn in enumerate(insert_cols)
              if (enabled_map.get(cn.lower()) or {}).get('mode') == 'freetext']

    def _is_conn_drop(e):
        s = str(e)
        return any(x in s for x in ('08S01', 'link failure', 'aborted', '10053', '10054',
                                    'Communication link', 'Connection is busy', 'not connected'))

    signal.signal(signal.SIGINT, _sigint)
    t0 = time.time(); done = ckpt['done']; fails = 0
    while done < target_total:
        try:
            take = min(batch_size, target_total - done)
            rows = read_cur.execute(
                f"SELECT {col_list} FROM [{SCHEMA}].[{tbl}] "
                f"ORDER BY {order_sql} OFFSET {done} ROWS FETCH NEXT {take} ROWS ONLY").fetchall()
            if not rows: break
            known = global_known if have_freetext else None
            # BATCHED free-text: pre-scrub every cell (fast), ONE GLiNER pass over the batch, apply.
            pre_cache = {}
            if ft_idx:
                keys, texts = [], []
                for ri, row in enumerate(rows):
                    row_tokens = _row_person_tokens(row, known) if known else None
                    for ci in ft_idx:
                        if row[ci] is None: continue
                        engine.set_log_context(insert_cols[ci])   # freetext span logs need the col name
                        pre, protect = scrub_pre(str(row[ci]), engine, known, row_tokens)
                        pre_cache[(ri, ci)] = [pre, protect, None]
                        keys.append((ri, ci)); texts.append(pre)
                for k, ents in zip(keys, gl.entities_batch(texts)):
                    pre_cache[k][2] = ents

                # STAGE 1: domain-aware scrub_post (fix C -- KNOWN_BRAND_STOP only applies when
                # domain=='resume') + the existing JSON-safety fallback, unchanged otherwise.
                stage1 = {}
                for k in keys:
                    ri, ci = k
                    engine.set_log_context(insert_cols[ci])   # per-cell col context for scrub_post spans
                    pre, protect, ents = pre_cache[k]
                    v1 = scrub_post(pre, ents or [], engine, protect, domain=domain)
                    v1 = _json_safe_fallback(rows[ri][ci], v1, pre)
                    stage1[k] = v1

                # STAGE 2 (fix A): non-resume domain only -- retry on an HTML-stripped copy of
                # whatever stage 1 left untouched, batched the same way as the primary pass, so
                # entities diluted by markup noise in the raw-HTML windowed pass get a second,
                # cleaner-context chance.
                sec_keys, sec_texts = [], []
                if domain != 'resume':
                    for k in keys:
                        v1 = stage1[k]
                        if '<' in v1:
                            sec_keys.append(k); sec_texts.append(strip_html_for_detection(v1))
                sec_ents = dict(zip(sec_keys, gl.entities_batch(sec_texts))) if sec_texts else {}

                # STAGE 3: apply stage-2 entities as LITERAL substitutions (never offset splicing
                # -- stripped text has different offsets than `v`, see apply_literal_map), then
                # the MANUAL_COMPANY_MAP backstop (fix D) as a final deterministic safety net
                # for specific companies you've curated, regardless of what GLiNER did or missed.
                for k in keys:
                    ri, ci = k
                    engine.set_log_context(insert_cols[ci])   # per-cell col context for stage-2 spans
                    pre, protect, ents = pre_cache[k]
                    v = stage1[k]
                    if sec_ents.get(k):
                        stripped = strip_html_for_detection(v)
                        lit_map = {}
                        for s, e, t in sec_ents[k]:
                            span = stripped[s:e]
                            low = span.casefold()
                            if len(span) < 3 or low in protect or _is_generic_entity_span(span):
                                continue
                            lit_map[low] = engine.fake(span, t)
                        v = apply_literal_map(v, lit_map, protect)
                    if company_forced_map:
                        v = apply_literal_map(v, company_forced_map, protect, pattern_cache=company_pattern_cache)
                    v = _json_safe_fallback(rows[ri][ci], v, pre)
                    pre_cache[k].append(v)   # index 3 = final scrubbed value
            out = []
            for ri, row in enumerate(rows):
                newrow = []
                id_hint_pool = _row_id_hint_pool(row)
                for i, cn in enumerate(insert_cols):
                    if (ri, i) in pre_cache:
                        v = pre_cache[(ri, i)][3]
                    else:
                        v = anon_val(cn, row[i], known, row, id_hint_pool)
                    v = fit_width(v, maxlen.get(cn))   # #2 word-boundary clamp to column width
                    # NOTE: per-cell/per-span logging is emitted from inside engine.fake()/_persist()/
                    # flush_pending() via engine._log_outcome, so we do NOT log here (avoids double-log).
                    # amount cells are logged inside anon_val (they bypass fake()); freetext cells are
                    # logged one line per detected span from within scrub_text -> engine.fake(span).
                    newrow.append(v)
                out.append(newrow)
            cur.executemany(
                f"INSERT INTO [{SCHEMA}].[{anon}] ({col_list}) VALUES ({place})", out)
            engine.flush_pending()             # write queued xref pairs (fresh Azure conn, resilient)
            cur.connection.commit()
            done += len(rows); fails = 0
            if cp:                             # dry-run has cp=None -> no checkpoint written
                save_json(cp, {'done': done, 'table': tbl, 'total': total,
                               'updated_at': str(datetime.datetime.now())})
            log(f"  +{len(rows):,}  ({done:,}/{target_total:,})  {time.time()-t0:.0f}s")
            if _PAUSE['stop'] and not dry_run:
                log(f"  PAUSED at {done:,}. Re-run the same command to resume."); break
        except pyodbc.Error as e:
            if dry_run or not _is_conn_drop(e):
                raise
            # A local/Azure connection dropped (idle during a long GLiNER batch). Reconnect BOTH
            # data connections and retry this batch. Reconcile `done` from the actually-committed
            # row count so a half-done commit neither double-inserts nor is skipped.
            fails += 1
            if fails > 25:
                log(f"  too many consecutive connection drops — PAUSING at {done:,} "
                    f"(re-run the same command to resume)."); break
            log(f"  connection dropped ({str(e)[:55]}); reconnecting + retrying (attempt {fails})")
            for c in (read_cur, cur):
                try: c.connection.close()
                except Exception: pass
            read_cur = connect().cursor(); read_cur.setoutputsize(10_000_000)
            cur = connect().cursor()
            if idc:
                try: cur.execute(f"SET IDENTITY_INSERT [{SCHEMA}].[{anon}] ON")
                except Exception: pass
            try: done = cur.execute(f"SELECT COUNT(*) FROM [{SCHEMA}].[{anon}]").fetchone()[0]
            except Exception: pass
            engine._pending_ins = []; engine._pending_upd = []   # drop partial-batch pairs (rare)
    engine.flush_pending()                     # safety flush for any trailing queued pairs
    if idc: cur.execute(f"SET IDENTITY_INSERT [{SCHEMA}].[{anon}] OFF")
    cur.connection.commit()
    read_cur.connection.close()
    close_value_log(vlog)

    if engine._relabeled:
        log(f"  --prefer-clean rewrote {engine._relabeled} legacy dirty xref fake(s)")
    if dry_run:
        newpairs = cur.execute(f"SELECT COUNT(*) FROM [{SCHEMA}].[{dry_map}]").fetchone()[0]
        log(f"  DRY-RUN DONE — {done:,} sample rows in [{SCHEMA}].[{anon}]; "
            f"{newpairs:,} new original->fake pairs in [{SCHEMA}].[{dry_map}].")
        log(f"  mapping_xref was NOT modified. Review both, then do the real run without --dry-run.")
    elif done >= total:
        log(f"  COMPLETE — {done:,} rows anonymized into {anon}")
    elif not _PAUSE['stop']:
        log(f"  sample/limit reached at {done:,}. Review {anon}, then: run {tbl} --limit all")


# (?<!\\) -- an email's local part must not start immediately after a literal backslash.
# Without this, a JSON-escaped '\n' (backslash + literal 'n') sitting directly against an email
# with no separator (confirmed present: '...报表\\nSIQI2010@LIVE.CN 设计...', a JSON string using
# raw '\n' as an inline line break) gets its trailing 'n' GREEDILY absorbed into the email match
# ('nSIQI2010@LIVE.CN') since 'n' is a valid local-part character with no boundary marker of its
# own -- the escape's 'n' is then replaced along with the email, leaving a bare '\' immediately
# followed by the fake email's first letter (e.g. '\e'), which is not a valid JSON escape and
# breaks the whole cell's JSON structure. The lookbehind makes the regex retry from the next
# character (here, the real email start) instead, leaving the backslash-n escape intact.
# Local part must START and END on an alnum char (the `._%+-` punctuation is only allowed in the
# MIDDLE, via the optional non-capturing group). Two emails glued back-to-back with no whitespace
# between them (e.g. Teams 1:1 chat_id 'nicholas.kampa@centific.com_peer.bdr@centific.com@unq...')
# used to break this: re.sub's next match starts exactly where the previous one ended, and a bare
# '[A-Za-z0-9._%+\-]+' happily starts a match ON that leading '_' (or '.'/'-'), since those are
# valid ANYWHERE in the old class including position 0. That silently absorbed the separator into
# the second email's matched span -- it never reappeared after substitution -- AND polluted the
# fake-lookup key ('_peer.bdr@...' instead of 'peer.bdr@...'), so the same real email got a
# DIFFERENT fake here than wherever else it was faked from a clean key (e.g. a structured email
# column in the same row). Requiring alnum at both ends closes this without affecting any
# legitimate email (real addresses don't start/end their local part on '.'/'_'/'%'/'+'/'-').
_EMAIL_PLAIN = re.compile(r'(?<!\\)[A-Za-z0-9](?:[A-Za-z0-9._%+\-]*[A-Za-z0-9])?'
                           r'@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
# URL-encoded email (@ -> %40) as found in Outlook SafeLinks / tracking URLs, e.g.
# 'kiran.mallakunta%40centific.com'. GLiNER never tags these, so they'd leak the real
# name (local part) and domain unless masked explicitly. Same alnum-boundary fix as _EMAIL_PLAIN
# above, for the same back-to-back-emails reason.
_EMAIL_ENC = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9._+\-]*[A-Za-z0-9])?'
                         r'%40[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', re.I)

# HTML-entity-encoded email (@ -> '&#64;' decimal, '&#x40;' hex, or the doubly-escaped
# '&amp;#64;'/'&amp;#x40;' forms seen inside href/query-string content) -- e.g.
# 'diana.moeck&#64;centific.com', confirmed present in real data
# (ResumeRating_Job_Requisition.job_description "How to Apply" sections: a real employee's
# contact email pasted from rich-text HTML where the '@' got entity-escaped). Neither
# _EMAIL_PLAIN (no literal '@') nor _EMAIL_ENC (only the URL '%40' form) catches this shape, so
# it leaked through unscrubbed. Generic over WHICH entity spelling is used (decimal vs hex,
# singly vs doubly escaped) rather than hardcoding '&#64;' alone, so any future variant of this
# same class of encoding is caught by the same pass instead of needing another one-off fix.
_HTML_AT_ENTITY = re.compile(r'&(?:amp;)?#(?:64|x40);', re.I)
# Same alnum-boundary fix as _EMAIL_PLAIN/_EMAIL_ENC above (back-to-back emails with no
# whitespace between them would otherwise absorb the separator into the second match).
_EMAIL_HTMLENT = re.compile(
    r'[A-Za-z0-9](?:[A-Za-z0-9._+\-]*[A-Za-z0-9])?'
    r'&(?:amp;)?#(?:64|x40);[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', re.I)

_JSON_NAME = re.compile(r'"name"\s*:\s*"([^"]{2,80})"')
_JSON_ADDR = re.compile(r'"(?:address|emailAddress|userUpn|upn)"\s*:\s*"([^"\s]+@[^"\s]+)"', re.I)
# 3-group (pre, val, post) sibling of _JSON_NAME (which stays 1-group -- it's also used by
# _accumulate_known's .findall() harvest and changing its shape would break that). Used for a
# DIRECT, guaranteed substitution in scrub_pre rather than relying solely on the generic
# known-dict harvest-and-substitute pass -- confirmed necessary: a candidate's own
# personalInfo.name can be a short value that coincidentally IS a common English word/pronoun
# (e.g. 'HE', an abbreviated real name in the source data), and _english_words() correctly
# refuses to use such a value as a table-wide freetext substitution TRIGGER (see that filter's
# own docstring) -- but that same refusal was also silently leaving the CANDIDATE'S OWN name
# field unfaked, which is the one place this value is unconditionally real PII, not a possible
# coincidence. A direct key-anchored regex sidesteps the dictionary-word ambiguity entirely: this
# occurrence is always the candidate's actual name, so it's always faked, no collision check
# needed.
_JSON_NAME_RE = re.compile(r'("name"\s*:\s*")([^"]{2,80})(")')

# LinkedIn profile slugs (seen embedded in parsed-resume JSON, e.g. Outputs/Resume_Info:
# 'linkedin.com/in/charisse-grace-agustin/') carry the candidate's real name but GLiNER never
# tags a URL-path slug as a person entity -- confirmed empirically (see ResumeRating_Job_
# Resume_ParsingLog investigation), so without this pass it leaks through untouched. Real slugs
# are inconsistent in shape (hyphenated 'charisse-grace-agustin', concatenated 'stuartlech',
# with/without a trailing LinkedIn-generated id suffix like '-2a1b8231a') -- no reliable
# name-vs-suffix tokenization exists across all of them. Faked as an OPAQUE token instead (same
# letter/digit-class-preserving substitution as the 'id' type): privacy only needs the real name
# gone, not a naturalistic replacement, and this closes the leak uniformly regardless of slug shape.
# Also matches the bare 'linkedin.com/<slug>' shape (no '/in/' segment -- an older/alternate
# personal-profile URL form, confirmed present in real data, e.g. 'linkedin.com/ajinkya-ghodekar').
# NON_PROFILE_LINKEDIN_PATHS excludes the first segment when it's a known non-profile LinkedIn
# section (learning courses, company pages, ...) -- those aren't a person's slug and shouldn't be
# faked as one (e.g. 'linkedin.com/learning/hr-as-a-strategic-business-partner' is a course title).
# 'in' and 'pub' are deliberately NOT in this denylist -- they're the current and legacy personal-
# profile path markers ('linkedin.com/in/<slug>', old-style 'in.linkedin.com/pub/<slug>/...'), not
# non-profile sections; both are optionally skipped by the pattern below instead of excluded.
NON_PROFILE_LINKEDIN_PATHS = {'learning', 'company', 'school', 'jobs', 'pulse', 'feed',
                               'groups', 'showcase', 'posts', 'sales', 'help', 'legal'}
_LINKEDIN_RE = re.compile(
    r'linkedin\.com/(?!(?:' + '|'.join(NON_PROFILE_LINKEDIN_PATHS) + r')(?:/|$))'
    r'(?:in/|pub/)?([A-Za-z0-9][A-Za-z0-9\-]{0,100})', re.I)
# parsed-resume JSON "birth" field (e.g. '"birth":"1976.07"') -- a direct-identifier-adjacent
# field GLiNER has no label for at all (its LABELS are person/organization/email/phone number
# only), so a populated birth date leaks through 100% unscrubbed otherwise.
_JSON_BIRTH_RE = re.compile(r'("birth"\s*:\s*")([^"]{4,20})(")')
_DATE_YMD_RE = re.compile(r'^(\d{4})([./\-])(\d{1,2})(?:([./\-])(\d{1,2}))?$')
# same rationale as _JSON_BIRTH_RE: GLiNER has no "location" label at all, and a parsed-resume
# JSON "phone" value sits right next to (not always caught by) GLiNER's own 'phone number' label
# whose Chinese-text coverage is unconfirmed -- both are direct/indirect PII with a fixed,
# reliable JSON key name, so a key-anchored regex is more reliable than depending on model
# detection. Values routed through engine.fake(val, 'location'/'phone'), reusing the existing
# generators exactly as _JSON_BIRTH_RE reuses jitter_birthdate.
_JSON_LOCATION_RE = re.compile(r'("location"\s*:\s*")([^"]{0,80})(")')
_JSON_PHONE_RE = re.compile(r'("phone"\s*:\s*")([^"]{0,40})(")')

def jitter_birthdate(original, seed_fn):
    """Format-preserving, semantically-bounded fake for a 'birth' value: YYYY<sep>MM or
    YYYY<sep>MM<sep>DD (real data here is ~99.7% the former, e.g. '1976.07'; one real value had
    a day component, '1999.10.16'). Shifts year by a random +/-2..3, month by +/-0..2 (carrying
    into the year via real calendar arithmetic), and -- ONLY if the original had a day component
    -- day by +/-0..5 (via datetime.timedelta, so month/year rollover and leap years are handled
    correctly, never landing on an invalid date). Reformats with the SAME separators and
    zero-padding width as the original. A birth date isn't a shared identity the way a name/email
    is (two candidates sharing a birth month is coincidence, not the same person), so this is a
    self-contained deterministic function of the input string, not persisted through
    mapping_xref like person/org/email fakes are.
    Returns None if `original` doesn't match the expected YYYY<sep>MM[<sep>DD] shape (e.g. the
    one malformed '16.7' value seen in real data) -- caller falls back to the old opaque
    substitution rather than guessing at a different date shape."""
    m = _DATE_YMD_RE.match(original.strip())
    if not m:
        return None
    y_s, sep1, mo_s, sep2, d_s = m.groups()
    year, month = int(y_s), int(mo_s)
    has_day = d_s is not None
    day = int(d_s) if has_day else 1
    if not (1 <= month <= 12):
        return None

    def pick(tag, choices):
        return choices[seed_fn(f"birth:{tag}:{original}") % len(choices)]

    dy = pick('y', [-3, -2, 2, 3])
    dm = pick('m', [-2, -1, 0, 1, 2])
    dd = pick('d', [-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5]) if has_day else 0

    total_months = (month - 1) + dm
    year2 = year + dy + total_months // 12
    month2 = total_months % 12 + 1
    day2 = min(day, calendar.monthrange(year2, month2)[1])   # clamp into the shifted month
    dt = datetime.date(year2, month2, day2) + datetime.timedelta(days=dd)

    y_out, mo_out = str(dt.year).zfill(len(y_s)), str(dt.month).zfill(len(mo_s))
    if has_day:
        return f"{y_out}{sep1}{mo_out}{sep2}{str(dt.day).zfill(len(d_s))}"
    return f"{y_out}{sep1}{mo_out}"
# attachment/file names wrongly caught by "name" JSON fields — not person PII, don't fake them
_FILE_RE = re.compile(r'\.(png|jpe?g|gif|bmp|svg|pdf|docx?|xlsx?|pptx?|zip|csv|txt|html?|eml|msg)$', re.I)

_ENGLISH_WORDS = None
def _english_words():
    """Lazy-loaded lowercase English dictionary (nltk 'words' corpus, ~235k entries), used by
    scrub_pre to stop a real participant's name from being used as a table-wide freetext
    find/replace trigger when that name ALSO happens to be an ordinary English word (e.g. a real
    candidate surnamed 'Good' -- confirmed present in ResumeRating_Job_Candidate -- otherwise
    causes every OTHER row's ordinary use of the word 'Good' to be replaced with his fake
    surname, table-wide). A word-boundary check alone can't catch this class of false positive
    (the coincidence is a WHOLE-word match, not a mid-word one) -- this needs an actual
    dictionary. Falls back to an empty set (no filtering) if the corpus isn't available/
    downloadable, same as every other optional NLP dependency in this file."""
    global _ENGLISH_WORDS
    if _ENGLISH_WORDS is not None:
        return _ENGLISH_WORDS
    _ENGLISH_WORDS = set()
    try:
        from nltk.corpus import words
        _ENGLISH_WORDS = {w.lower() for w in words.words()}
    except LookupError:
        try:
            import nltk
            nltk.download('words', quiet=True)
            from nltk.corpus import words
            _ENGLISH_WORDS = {w.lower() for w in words.words()}
        except Exception as e:
            log(f"  (warn: English dictionary unavailable, {str(e)[:60]} -- "
                f"known-name freetext substitution won't filter common-word collisions)")
    except Exception as e:
        log(f"  (warn: English dictionary unavailable, {str(e)[:60]} -- "
            f"known-name freetext substitution won't filter common-word collisions)")
    return _ENGLISH_WORDS

def _json_str_escape(s):
    """Escape `s` for splicing directly into a JSON string context the caller already owns the
    surrounding quotes for (backslashes, quotes, newlines/tabs/control chars). Needed because a
    fake VALUE can come from mapping_xref reuse-first and be legacy/dirty data predating a
    generator change -- confirmed present: a stale type-agnostic reuse-first hit for a
    'location' lookup resolved to an old Faker-generated multi-line US street address with a
    literal embedded '\\r\\n' (from when 'location' PII was handled as a generic Faker address,
    before _gen_location's current city-name approach), which broke JSON syntax when spliced in
    raw. json.dumps()[1:-1] gives exactly the escaped inner content without its own quotes."""
    return json.dumps(s)[1:-1]

def scrub_pre(text, engine, known=None, row_tokens=None):
    """PRECISE canonical passes (no GLiNER): mask e-mails (plain + %40), known participants
    (whole-string, table-wide), and row_tokens (bare name FRAGMENTS, but only ones established
    by THIS row's own person columns -- see _row_person_tokens in run()). Returns
    (intermediate_text, protect_set). Fast — safe to run per cell before a BATCHED GLiNER pass
    over all the intermediate texts. Split out so GLiNER can be called once per batch."""
    protect = set()
    def _protect(fake):
        # protect the inserted fake AND its email local-part so a later GLiNER pass can't
        # re-fake e.g. 'loml.bongwe' out of the fake 'loml.bongwe@aventraa.com'.
        protect.add(fake.casefold())
        if '@' in fake:
            protect.add(fake.split('@', 1)[0].casefold())

    def _plain(m):
        fake = engine.fake(m.group(0), 'email'); _protect(fake); return fake
    text = _EMAIL_PLAIN.sub(_plain, text)

    def _enc(m):
        s = m.group(0); prefix = ''
        if (m.start() >= 1 and m.string[m.start() - 1] == '%'
                and len(s) >= 2 and all(c in '0123456789abcdefABCDEF' for c in s[:2])):
            prefix, s = s[:2], s[2:]
        fake = engine.fake(s.replace('%40', '@'), 'email'); _protect(fake)
        return prefix + fake.replace('@', '%40')
    text = _EMAIL_ENC.sub(_enc, text)

    def _htmlent(m):
        s = m.group(0)
        at = _HTML_AT_ENTITY.search(s).group(0)          # exact entity spelling found, e.g. '&#64;'
        local, domain = s.split(at, 1)
        fake = engine.fake(f"{local}@{domain}", 'email'); _protect(fake)
        f_local, f_domain = fake.split('@', 1)
        return f_local + at + f_domain                    # same entity spelling round-tripped back
    text = _EMAIL_HTMLENT.sub(_htmlent, text)

    def _li(m):
        fake = engine.fake(m.group(1), 'id'); _protect(fake)
        # splice in just the slug capture group -- everything else in the match (the
        # 'linkedin.com/' prefix, optional 'in/') is kept exactly as matched
        return m.group(0)[:m.start(1) - m.start()] + fake + m.group(0)[m.end(1) - m.start():]
    text = _LINKEDIN_RE.sub(_li, text)

    def _birth(m):
        pre, val, post = m.group(1), m.group(2), m.group(3)
        if not val.strip():
            return m.group(0)
        fake = jitter_birthdate(val, engine._seed)
        if fake is None:                            # doesn't match YYYY<sep>MM[<sep>DD] shape
            fake = engine.fake(val, 'id')            # fall back to opaque substitution
        _protect(fake)
        return pre + _json_str_escape(fake) + post
    text = _JSON_BIRTH_RE.sub(_birth, text)

    def _location(m):
        pre, val, post = m.group(1), m.group(2), m.group(3)
        if not val.strip():
            return m.group(0)
        fake = engine.fake(val, 'location'); _protect(fake)
        return pre + _json_str_escape(fake) + post
    text = _JSON_LOCATION_RE.sub(_location, text)

    def _phone(m):
        pre, val, post = m.group(1), m.group(2), m.group(3)
        if not val.strip():
            return m.group(0)
        fake = engine.fake(val, 'phone'); _protect(fake)
        return pre + _json_str_escape(fake) + post
    text = _JSON_PHONE_RE.sub(_phone, text)

    def _pname(m):
        pre, val, post = m.group(1), m.group(2), m.group(3)
        if not val.strip():
            return m.group(0)
        fake = engine.fake(val, 'person'); _protect(fake)
        return pre + _json_str_escape(fake) + post
    text = _JSON_NAME_RE.sub(_pname, text)

    if known:
        ewords = _english_words()
        for orig in sorted(known, key=len, reverse=True):
            fake = known[orig]
            if not orig or orig == fake:
                continue
            # Two distinct false-positive classes, both confirmed on ResumeRating_Job_Candidate
            # (19k+ distinct real candidate names -- far more collision surface than the smaller
            # employee-name tables this pass was originally built against):
            #  1) a real participant's name is a SUBSTRING of an unrelated longer word (e.g.
            #     'Ali' -- a real first/last name for 44 candidates -- sitting inside
            #     'qualified': 'qu-ALI-fied'). A plain `orig in text` check has no word-boundary
            #     concept at all, so it corrupted the middle of ordinary words. Fixed the same
            #     way row_tokens already does it below: bound the match with
            #     (?<![A-Za-z])...(?![A-Za-z]).
            #  2) a real participant's name IS, in its entirety, an ordinary English dictionary
            #     word (e.g. a candidate surnamed 'Good') -- a word-boundary fix alone can't
            #     catch this, since the coincidence is a WHOLE-word match, not a mid-word one.
            #     Without this, every OTHER row's ordinary use of the word 'Good' in a Comment
            #     got replaced with his fake surname, table-wide -- confirmed empirically ('Good
            #     customer-facing and sales background...' -> 'Tchilumbu customer-facing...').
            #     Skipped via _english_words() (see its docstring); only gates this PLAIN-text
            #     trigger, never the email-encoded branch below (a genuine email address never
            #     collides with a bare dictionary word -- it always contains '@').
            #  3) a real participant's name field is actually GARBAGE from an upstream
            #     resume-parsing error, not a real name at all -- confirmed present: row
            #     Candidate_ID='C154186' has First_Name='Pipelines', Last_Name='LLM' (skill
            #     keywords misassigned into name fields, not a person). 'LLM' then got used as a
            #     freetext substitution trigger, corrupting 2,366 OTHER rows' legitimate mentions
            #     of the term 'LLM' (95% of every 'LLM' occurrence table-wide) -- found via a full
            #     sweep for name fields matching GENERIC_ENTITY_STOP (43 rows: 'MD' -- actually a
            #     common South/Southeast Asian given-name abbreviation for 'Mohammad', not the
            #     medical degree, but still wrong as a blanket freetext trigger -- 'QA', 'DevOps',
            #     'PM', 'VP', 'Cloud', 'Product', 'Good', 'Ta', etc. -- plus THREE more rows with a
            #     real company/platform name misassigned into a name field ('AWS', 'ServiceNow',
            #     'Workday'), caught the same way via KNOWN_BRAND_STOP.
            #     _english_words() alone can't catch domain jargon like 'LLM'/'QA'/'DevOps' since
            #     those aren't standard English words -- reuses the SAME GENERIC_ENTITY_STOP/
            #     KNOWN_BRAND_STOP vocabulary scrub_post already trusts for exactly this purpose.
            #     This only gates the freetext substitution TRIGGER; the structured First_Name/
            #     Last_Name column itself is still faked normally via the regular per-column
            #     fake() path.
            # cheap substring pre-check FIRST, same as the original code -- `known` can hold tens
            # of thousands of entries (one per distinct participant table-wide), so compiling a
            # regex for every single one on every cell (instead of only the rare entries that are
            # even plausibly present) turned a ~25min run into a many-hour one the first time
            # this was tried. The dictionary/boundary checks only refine an ALREADY-cheap hit.
            if (orig in text and orig.strip().lower() not in ewords
                    and orig.strip().lower() not in GENERIC_ENTITY_STOP
                    and orig.strip().lower() not in KNOWN_BRAND_STOP):
                if chinese_anon.is_han(orig):
                    text = text.replace(orig, fake); _protect(fake)
                else:
                    patt = re.compile(r'(?<![A-Za-z])' + re.escape(orig) + r'(?![A-Za-z])')
                    if patt.search(text):
                        text = patt.sub(lambda m: fake, text); _protect(fake)
            if '@' in orig:
                eo = orig.replace('@', '%40')
                if eo in text:
                    text = text.replace(eo, fake.replace('@', '%40')); _protect(fake)

    if row_tokens:
        # bare fragment pass -- only tokens THIS row's own person columns established, so a
        # different person elsewhere in the table who happens to share the fragment (e.g. a
        # different "Jacky") is never touched. Han-script tokens: plain substring (no Latin-style
        # word boundary concept). Latin tokens: bounded by non-letter chars, same technique as
        # apply_forced/_FORCED_RE, so e.g. "Li" can't corrupt the middle of "Liability".
        for tok in sorted(row_tokens, key=len, reverse=True):
            fake = row_tokens[tok]
            if chinese_anon.is_han(tok):
                if tok and tok in text:
                    text = text.replace(tok, fake); _protect(fake)
            else:
                patt = re.compile(r'(?<![A-Za-z])' + re.escape(tok) + r'(?![A-Za-z])', re.I)
                if patt.search(text):
                    text = patt.sub(lambda m: case_like(m.group(0), fake), text)
                    _protect(fake)
    return text, protect

# ── GLiNER over-triggers 'person'/'organization' on job-title, degree, seniority and generic
# collective-noun vocabulary in HR/job-posting free text (confirmed on
# ResumeRating_Job_Requisition.Job_Description/Justification: of 819 new mapping_xref pairs
# from one run, the large majority were things like 'Ph.D. Research Intern', 'AI Recruiter',
# 'CHRO', 'Operational Coordination', 'PyTorch/JAX/TensorFlow', 'LLM systems' -- role titles,
# degrees, department/function words and ML/dev-tool names, none of which are a real person or
# company identity). A span containing ANY of these words is left UNCHANGED rather than faked --
# real personal names and real company/brand names essentially never share this vocabulary, so
# this trades a little recall (a real name/company glued to a generic word in one GLiNER span is
# skipped whole) for much higher precision, matching what was explicitly asked for. Note
# 'Centific'/'Pactera' are NOT skipped by anything here -- apply_forced() (FORCED_MAP) still runs
# unconditionally afterwards and catches them regardless of whether this filter skipped the span,
# so e.g. "Centific's AI Platform" still becomes "Aventraa's AI Platform" even though 'Platform'
# below stops the whole span from being GLiNER-faked.
# 'university'/'universities'/'college'/'colleges'/'school'/'schools'/'institution'/'institutions'
# were REMOVED from this set (2026-07-24) -- confirmed on ResumeMapping_Resume_Record.Education
# that this blanket stop caused 145/198 (73%) of populated cells to leak the real institution
# name verbatim in cleartext (e.g. 'Anna University', 'Bharathiar University', 'University of
# Oklahoma Norman Campus' all left untouched), since the word is part of the specific institution's
# own proper name here, not incidental generic vocabulary. Unlike a job-posting mentioning
# "local universities" in passing (no specific name attached, so GLiNER has nothing to tag as an
# entity in the first place), an Education-column value IS the institution's name -- the whole
# point is to mask it. If a future table reintroduces the original false-positive this list was
# built to prevent (a bare generic reference getting flagged), address it narrowly rather than
# re-adding these words wholesale, since that regresses this fix silently.
# GENERIC_ENTITY_STOP now lives in constants.py (imported above) -- extend it there.

# KNOWN_BRAND_STOP now lives in constants.py (imported above) -- extend it there,
# and remember it only applies when table_domain(table) == 'resume'.
def _is_known_brand_span(span):
    low = span.casefold()
    return any(b in low for b in KNOWN_BRAND_STOP)

# _PRONOUN_STOP now lives in constants.py (imported above).
_GENERIC_STOP_RE = re.compile(r"[A-Za-z]+")   # pure letter runs -- '.net'/'Moderator-1'/"bachelor's"
                                               # split on the digit/hyphen/apostrophe/dot so the
                                               # letter part alone ('net', 'moderator', 'bachelor')
                                               # still matches GENERIC_ENTITY_STOP

def _is_generic_entity_span(span):
    """True if ANY word in `span` is generic job-title/degree/department/tech-tool vocabulary
    (see GENERIC_ENTITY_STOP) -- such a span should be left unfaked. Word-level check (not
    substring), so e.g. 'grAId' or a surname that happens to contain 'lead' as a substring isn't
    falsely caught."""
    return any(w.casefold() in GENERIC_ENTITY_STOP for w in _GENERIC_STOP_RE.findall(span))

def scrub_post(text, ents, engine, protect, domain='general'):
    """Apply GLiNER entities (already computed) + the forced-word rule. `ents` is the entity list
    for THIS text (from a batched predict). Skips e-mails (already masked) and already-inserted
    participant fakes so it never re-fakes a fake.

    `domain` -- 'resume' or 'general' (default), from constants.table_domain(table). Gates
    ONLY the KNOWN_BRAND_STOP skip below: that list encodes a policy specific to resume/
    candidate-evaluation text (a brand mentioned as a past employer/skill isn't the
    candidate's own identity). In every other ('general'/CRM) table a brand name IS a real
    client/competitor identity and must be faked like anything else -- see
    FIX_GUIDE_freetext_domain_scope_and_forced_company_map.md for the leak this fixes
    (KNOWN_BRAND_STOP was previously applied table-agnostically, so dyncrm_activity/
    dyncrm_leads left real client names like "Google" untouched). GENERIC_ENTITY_STOP
    (job titles/degrees/tech-tool vocabulary) is NOT domain-gated -- a job title is never
    real PII in any table."""
    if ents:
        ents = sorted(ents, key=lambda e: e[0])
        out, prev, last = [], 0, -1
        for start, end, ptype in ents:
            if start < last: continue
            span = text[start:end]
            after = text[end:end + 1]; before = text[start - 1:start] if start else ''
            # skip e-mail components: contains '@', already an inserted fake, or sits
            # directly against an '@' (a local-part 'x' in 'x@dom' or a domain 'dom' in 'x@dom').
            if ('@' in span or span.casefold() in protect
                    or after == '@' or before == '@'):
                continue
            # skip GLiNER window/overlap-boundary fragments (e.g. 'chers' left over from
            # 'teachers' cut mid-word by a window edge) -- if the span starts mid-word (the
            # character right before it is a letter of the same script), it isn't a real
            # standalone token at all, so faking it just replaces a text fragment with noise.
            if before and before.isalpha() and span[:1].isalpha() and before.isascii() == span[:1].isascii():
                continue
            # skip generic job-title/degree/department/tech-tool vocabulary for person/org --
            # see GENERIC_ENTITY_STOP docstring above.
            if ptype in ('person', 'org') and _is_generic_entity_span(span):
                continue
            # skip a well-known company/platform mentioned as a skill/employer reference, not
            # the candidate's own identity -- see KNOWN_BRAND_STOP docstring above. RESUME
            # DOMAIN ONLY -- see this function's own docstring.
            if domain == 'resume' and ptype in ('person', 'org') and _is_known_brand_span(span):
                continue
            # skip a bare pronoun mislabeled 'person' (e.g. 'I', 'You', 'me') -- never a name.
            if ptype == 'person' and span.strip().casefold() in _PRONOUN_STOP:
                continue
            # skip the literal WORD 'email'/'emails'/'e-mail' mislabeled ptype='email' -- a
            # genuine email address always contains '@'; without '@' this is just someone
            # writing about email in general (e.g. 'no email listed'), not an actual address.
            if ptype == 'email' and '@' not in span:
                continue
            # skip a bare punctuation/quote span mislabeled 'phone number' -- confirmed present
            # on sift_bus_resume.standard_resume: the literal '""' immediately following the
            # JSON key '"phone":' gets detected by GLiNER as a 'phone number' entity (same class
            # of false positive as the empty-'email' case above, just for the other label). A
            # genuine phone number always has at least one digit; faking a digit-less span here
            # was replacing the JSON's empty '""' with an UNQUOTED fake number (e.g.
            # '"phone":+1-555-2540,'), corrupting the surrounding JSON syntax itself, not just
            # the value.
            if ptype == 'phone' and not any(c.isdigit() for c in span):
                continue
            out.append(text[prev:start]); out.append(engine.fake(span, ptype))
            prev = end; last = end
        out.append(text[prev:])
        text = ''.join(out)
    text = _han_token_backstop(text, engine)
    return engine.apply_forced(text)


# GLiNER (English/Latin-centric NER) has near-zero recall on Han-script entities -- confirmed 0
# hits at threshold 0.1 on a real vendor name sitting inside an otherwise-Latin filename
# ('..._CQC_昇迪凡科_202510...pdf'), so a real company/person name in Chinese can sail through
# scrub_post's GLiNER-driven loop above completely untouched. This deterministic backstop runs
# after that loop (so anything GLiNER DID already fake is now Latin text and won't re-match):
# any isolated 2-6 character contiguous Han-script run -- bounded by non-Han characters or the
# string edges, i.e. the shape a name/company token takes in a delimited filename, NOT part of a
# longer narrative Chinese phrase (which is left alone) -- is faked via _han_org_fake below.
# Same tradeoff GENERIC_ENTITY_STOP/HARVEST_STOP already accept for Latin script: a flat curated
# stopword list (CN_HARVEST_STOP/CN_GENERIC_STOP/CN_COUNTRY_STOP in obi_chinese_anonymizer.py),
# not a perfect classifier -- an unlisted generic Chinese term can still get swept up. Given the
# alternative is a confirmed real leak, that's an acceptable, correctable-over-time tradeoff.
_HAN_TOKEN_RE = re.compile('[一-鿿]{1,20}')

def _han_token_backstop(text, engine):
    if not chinese_anon.is_han(text):
        return text
    out, prev = [], 0
    for m in _HAN_TOKEN_RE.finditer(text):
        run = m.group(0)
        if not (2 <= len(run) <= 6):
            continue
        if (chinese_anon.is_harvest_stop(run) or chinese_anon.is_generic_stop(run)
                or chinese_anon.is_country_stop(run)):
            continue
        out.append(text[prev:m.start()]); out.append(_han_org_fake(run, engine))
        prev = m.end()
    out.append(text[prev:])
    return ''.join(out)


def _han_org_fake(run, engine):
    """Like engine.fake(run, 'org'), but bypasses xref_lookup's type-AGNOSTIC fallback.
    Confirmed live: '昇迪凡科' (a real company name) already had a person-labeled ('Names')
    mapping_xref entry from some other, likely-misclassified column elsewhere -- a fresh
    org-typed request for the identical string got that person-shaped fake back instead of a
    company-shaped one, since xref_lookup's fallback tier matches purely on the string,
    ignoring type. Reuse only a genuinely org-labeled ('CompanyName') prior entry; otherwise
    generate fresh via the Chinese org generator and persist it under the correct label --
    _persist() writes straight into engine._xref keyed by that label (not just the
    type-agnostic _xref_any), so this also self-heals any later same-run 'org' lookup for the
    same string, not just this backstop's own calls."""
    nk = engine.normalize(run)
    cached = engine._xref.get((XREF_TYPE['org'].casefold(), nk))
    if cached:
        return cached
    fresh = engine.apply_forced(chinese_anon.gen_chinese_org(run, engine._seed, engine._used))
    engine._persist(run, fresh, 'org')
    return fresh


# ════════════════════════════════════════════════════════════════════════════════
#  Free-text hardening: HTML preprocessing (fix A), deterministic company backstop (fix D)
#  See FIX_GUIDE_freetext_domain_scope_and_forced_company_map.md.
# ════════════════════════════════════════════════════════════════════════════════
_HTML_TAG_RE = re.compile(r'<[^>]+>')
_HTML_WS_ENTITY_RE = re.compile(r'&nbsp;|&#160;', re.I)
_HTML_WS_RE = re.compile(r'\s+')

def strip_html_for_detection(text):
    """Best-effort HTML/CSS stripper used ONLY to build a cleaner surface for a SECOND GLiNER
    pass -- never written back to the DB, and never used for offset-based splicing (see
    apply_literal_map). Real activity/email-sync freetext cells are frequently HTML (confirmed:
    dyncrm_activity.description averages ~15.8KB, max ~1.7MB, 79% containing markup) -- feeding
    raw '<span style="...">' noise into an 800-char GLiNER window dilutes the local context
    around a real entity and burns windows on pure markup instead of content, which plausibly
    explains why short/ambiguous mentions (a bare first name in a casual greeting, a brand name
    used attributively) fall under the confidence threshold in the raw-HTML pass but are caught
    once the surrounding noise is removed. Doesn't need to be perfect HTML parsing (it's
    detection-only input, not output) -- a cheap regex/entity-unescape is enough."""
    if '<' not in text:
        return text
    t = _HTML_WS_ENTITY_RE.sub(' ', text)
    t = _HTML_TAG_RE.sub(' ', t)
    t = html.unescape(t)
    return _HTML_WS_RE.sub(' ', t).strip()


_LITERAL_WORD_BOUND = r'(?<![A-Za-z0-9])({})(?![A-Za-z0-9])'

def apply_literal_map(text, literal_map, protect, min_len=3, pattern_cache=None):
    """Deterministic, word/phrase-boundary-aware, longest-match-first literal substitution --
    the mechanism behind BOTH the MANUAL_COMPANY_MAP backstop (fix D) and the secondary
    HTML-stripped-pass entities (fix A). Unlike scrub_post's GLiNER-offset splicing, this
    operates on literal STRING content, so it's safe to apply against a DIFFERENT (but
    string-identical-where-unmodified) text than whatever produced `literal_map`'s keys --
    exactly what's needed when detection ran on a stripped copy but the substitution must land
    in the original HTML-intact text.

    `literal_map`: {lowercased original -> fake}. `protect`: casefolded fakes already inserted
    this cell (never re-fake a fake). `min_len`: skip too-short keys (avoids single-letter/very
    common short-token noise from ever reaching this deterministic path).

    `pattern_cache`: optional mutable dict the CALLER keeps alive across cells (e.g. one per
    run()/scrub_text call site), used to avoid recompiling the same regex per cell. PERFORMANCE
    INCIDENT: the original company backstop queried mapping_xref directly (93,695 CompanyName
    rows) and this function recompiled a ~93K-way regex from scratch on EVERY cell (no cache at
    all) -- that alone turned a ~94min run into 3+ hours. Rolled back to a small, manually
    curated MANUAL_COMPANY_MAP (see load_company_forced_map), but ALSO fixed the underlying
    bug here: pass a stable dict and this now compiles once and reuses it, keyed by the
    literal_map object's identity (safe because the caller-owned cache and the map it's built
    from share the same lifetime -- a stage-2 per-cell entity map is a fresh dict each call, so
    it simply never hits the cache and compiles fresh each time, which is fine since those maps
    are always tiny).

    CRITICAL guard: never substitute inside an HTML tag (`<meta ...>`, `<div class="...">`,
    etc.) -- confirmed by direct testing that a real company literally named "Meta" corrupted
    the HTML tag `<meta http-equiv=...>` itself into `<evercrest solutions talos http-equiv=...>`
    the first time this ran, since 'meta' satisfies the word-boundary check just as validly
    inside a tag as inside prose. Tag spans are computed once and any candidate match
    overlapping one is skipped entirely, not just the exact colliding word, since a tag's
    attribute VALUES (e.g. a real name in a mailto: href) are handled by scrub_post's own
    email/link passes already, not this literal sweep -- this sweep is prose-only."""
    if not text or not literal_map:
        return text
    if pattern_cache is not None and pattern_cache.get('map_id') == id(literal_map):
        pattern = pattern_cache['pattern']
        if pattern is None:                 # cached "no usable keys" result
            return text
    else:
        keys = sorted((k for k in literal_map if len(k) >= min_len), key=len, reverse=True)
        pattern = (re.compile(_LITERAL_WORD_BOUND.format('|'.join(re.escape(k) for k in keys)), re.I)
                   if keys else None)
        if pattern_cache is not None:
            pattern_cache['map_id'] = id(literal_map)
            pattern_cache['pattern'] = pattern
        if pattern is None:
            return text
    tag_spans = [(m.start(), m.end()) for m in _HTML_TAG_RE.finditer(text)] if '<' in text else []

    def _in_tag(s, e):
        return any(ts < e and s < te for ts, te in tag_spans)

    def _sub(m):
        span = m.group(0)
        low = span.lower()
        if low in protect:
            return span
        s, e = m.start(), m.end()
        if _in_tag(s, e):
            return span   # never touch HTML tag names/attributes -- prose-only sweep
        if (s > 0 and text[s - 1] == '@') or (e < len(text) and text[e] == '@'):
            return span   # don't touch anything glued to '@' (email-adjacent), same as scrub_post
        return case_like(span, literal_map[low])

    return pattern.sub(_sub, text)


_company_forced_map_cache = None
_scrub_text_company_pattern_cache = {}   # compiled once, reused across scrub_text() calls

def load_company_forced_map():
    """Build the deterministic company backstop (fix D) from constants.MANUAL_COMPANY_MAP only.

    EARLIER VERSION queried every mapping_xref CompanyName row (93,695 of them) and rebuilt a
    regex alternation from all of them on every single freetext cell -- with apply_literal_map
    recompiling that ~93K-way regex from scratch per call (not cached), this turned a ~94min
    run into a 3+ hour one. Rolled back per explicit decision: rely on the ALREADY-fast normal
    path instead (GLiNER detection + engine.fake()'s existing per-value reuse-first lookup
    against mapping_xref, which is a plain dict lookup, not a bulk regex scan) for anything not
    manually listed here. MANUAL_COMPANY_MAP is for a small, deliberately-curated set of
    companies you already know appear in this data and want a guaranteed, deterministic
    backstop for regardless of what GLiNER detects -- same reliability contract as FORCED_MAP
    (Centific/Pactera), just kept as its own dict (not merged into FORCED_MAP/apply_forced)
    because that mechanism has NO word-boundary check by design (safe only for distinctive
    made-up tokens like 'centific' that never collide with real words) -- 'meta'/'apple'/'dell'
    style entries need the word-boundary + HTML-tag-safety guard apply_literal_map provides.
    Cached for the life of the process."""
    global _company_forced_map_cache
    if _company_forced_map_cache is not None:
        return _company_forced_map_cache
    m = {k.strip().lower(): v for k, v in MANUAL_COMPANY_MAP.items() if k.strip()}
    log(f"  company forced-map: {len(m):,} manually-curated entries (MANUAL_COMPANY_MAP)")
    _company_forced_map_cache = m
    return m


def _json_keyset(obj):
    """All dict keys anywhere in a parsed JSON value, walked recursively through nested
    dicts/lists. Used to detect STRUCTURAL corruption (a key itself got overwritten) that stays
    perfectly valid JSON syntax -- json.loads() alone can't catch this."""
    if isinstance(obj, dict):
        ks = set(obj.keys())
        for v in obj.values(): ks |= _json_keyset(v)
        return ks
    if isinstance(obj, list):
        ks = set()
        for v in obj: ks |= _json_keyset(v)
        return ks
    return set()

def _json_safe_fallback(original, scrubbed, pre):
    """If `original` was valid JSON, the scrubbed output must (a) stay valid JSON and (b) keep
    the exact same set of KEYS -- confirmed empirically on sift_bus_resume.standard_resume:
    GLiNER's BATCHED inference (entities_batch, production path) occasionally detects a spurious
    span that doesn't reproduce with the single-cell entities() call on the same text (same root
    cause as the earlier ResumeRating_Job_Candidate batch-composition findings), and that span's
    replacement can land across JSON punctuation OR structure -- e.g. replacing the bare '""'
    right after a `"phone":` key with an unquoted fake number (breaks syntax), or overwriting the
    KEY NAME itself (e.g. '"projectResponsibility":"..."' -> '"Kyoka":"..."') which stays
    perfectly valid, parseable JSON while silently renaming/losing a field -- json.loads()
    succeeding is NOT enough to prove the structure survived intact, hence the separate key-set
    comparison. Since this is a rare, non-reproducible batch artifact rather than a specific
    word/pattern that can be stop-listed, the robust fix is a safety net: fall back to the
    scrub_pre-only result (`pre`) for either failure -- the JSON-key regex substitutions there
    are anchored to exact key patterns, never touch a key name, and only ever insert
    engine-generated location/phone/id values (JSON-escaped), so `pre` cannot itself break syntax
    or structure that was valid going in. Costs, at most, a missed GLiNER-only scrub (e.g. a
    3rd-party name embedded in prose) for the rare row this fires on -- never a corrupted or
    silently-restructured cell."""
    try:
        orig_obj = json.loads(original)
    except Exception:
        return scrubbed                      # not JSON to begin with -- no guarantee to keep
    try:
        scrubbed_obj = json.loads(scrubbed)
    except Exception:
        return pre
    if _json_keyset(scrubbed_obj) != _json_keyset(orig_obj):
        return pre
    return scrubbed

def scrub_text(text, gl, engine, known=None, domain='general', company_map=None):
    """Single-cell scrub (pre -> per-text GLiNER -> post). Used for non-batched paths.

    `domain`/`company_map` -- see scrub_post()/apply_literal_map() docstrings (fixes C/A/D).
    For domain != 'resume', a SECOND GLiNER pass runs on an HTML-stripped copy of the
    post-scrub text to catch entities whose signal was diluted by markup in the first pass
    (fix A); its hits and the MANUAL_COMPANY_MAP backstop (fix D) are applied as literal
    substitutions, never offset splicing, so they're safe against the stripped/original text
    not being the same string."""
    pre, protect = scrub_pre(text, engine, known)
    scrubbed = scrub_post(pre, gl.entities(pre), engine, protect, domain=domain)
    scrubbed = _json_safe_fallback(text, scrubbed, pre)
    if domain != 'resume' and '<' in scrubbed:
        stripped = strip_html_for_detection(scrubbed)
        ents2 = gl.entities(stripped)
        if ents2:
            lit_map = {}
            for s, e, t in ents2:
                span = stripped[s:e]
                low = span.casefold()
                if len(span) < 3 or low in protect or _is_generic_entity_span(span):
                    continue
                lit_map[low] = engine.fake(span, t)
            scrubbed = apply_literal_map(scrubbed, lit_map, protect)
    if company_map:
        scrubbed = apply_literal_map(scrubbed, company_map, protect,
                                      pattern_cache=_scrub_text_company_pattern_cache)
    return scrubbed


def run_inplace(cur, tbl, anon, enabled, limit, order_override, gl, prefer_clean=False):
    """Anonymize the given columns directly INSIDE an already-populated target table via
    UPDATE — never truncates/rebuilds/drops it. Used when the target already holds
    foreign/old rows (no checkpoint) and the caller passed --columns. Reads only the key
    column(s) + requested columns, so unrelated columns/types elsewhere don't matter."""
    acols = columns(cur, anon)
    keys = key_columns(cur, anon, order_override, acols, source_tbl=tbl)
    if keys is None:
        log(f"  STOP: no genuinely unique key found for {anon} -- neither an identity/PK on "
            f"{anon} itself, nor {tbl}'s own PK (if any), nor the first-12-sortable-columns "
            f"fallback actually uniquely identify this table's rows. Proceeding would target "
            f"UPDATE...WHERE at MULTIPLE rows per statement, which corrupts or no-ops the "
            f"anonymization with no error (see claude/jyothi_fixes/"
            f"FIX_GUIDE_nonunique_fallback_key.md). Pass --order-col <a genuinely unique column>.")
        return
    order_sql = ', '.join(f"[{k}]" for k in keys)
    if not order_override and not identity_cols(cur, anon) and not pk_cols(cur, anon):
        log(f"  NOTE: no unique key on {anon} — using composite key {keys} to target UPDATEs "
            f"(verified genuinely unique on this table's current data). "
            f"For big tables pass --order-col <unique col>.")
    col_names = [c['column'] for c in enabled]
    maxlen = {r[0]: r[2] for r in cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA=? AND TABLE_NAME=?", SCHEMA, anon).fetchall()}
    _, cp = _paths(tbl)
    cp_inplace = cp.replace('.ckpt.json', '.inplace.ckpt.json')
    keys_snap_path = cp.replace('.ckpt.json', '.inplace.keys.json')
    ckpt = load_json(cp_inplace) or {'done': 0, 'table': tbl}
    total = cur.execute(f"SELECT COUNT(*) FROM [{SCHEMA}].[{anon}] WITH (NOLOCK)").fetchone()[0]
    target_total = total if str(limit).lower() in ('all', 'complete', 'full') \
        else min(total, ckpt['done'] + int(limit))
    if ckpt['done'] >= target_total:
        log(f"  nothing to do — {ckpt['done']:,} rows already updated in place (target {target_total:,})."); return

    # frozen key snapshot -- captured ONCE, before this run touches a single row, and reused
    # for every batch (and replayed unchanged across a paused/resumed run). `keys` may itself
    # be one of the columns being anonymized (e.g. --order-col opportunityid with
    # opportunityid also in --columns): re-deriving "the next N rows" via `ORDER BY <key>
    # OFFSET done` against the LIVE table -- as this used to do -- means that as soon as a
    # batch rewrites that key's values, the table's sort order under that same ORDER BY
    # reshuffles, so a later OFFSET window no longer lines up with the intended rows. Some
    # untouched rows then fall permanently outside every window (silently skipped, stay raw
    # forever) while some already-processed rows resort back INTO a later window and get
    # their already-fake value faked again -- a fake-of-a-fake, persisted into mapping_xref
    # as bogus pollution. See claude/jyothi_fixes/FIX_GUIDE_order_col_self_mutation.md.
    key_rows = load_json(keys_snap_path)
    if key_rows is None:
        key_rows = [list(r) for r in cur.execute(
            f"SELECT {order_sql} FROM [{SCHEMA}].[{anon}] ORDER BY {order_sql}").fetchall()]
        save_json(keys_snap_path, key_rows)

    freetext = [c for c in enabled if c.get('mode') == 'freetext']
    if freetext: gl.load()
    log("=" * 78)
    log(f"UPDATE-IN-PLACE {anon}  (existing table, NOT dropped)  | columns: {col_names}")
    log(f"  key={keys}  already_done={ckpt['done']:,}  this_run_until={target_total:,}  total={total:,}")

    vlog = open_value_log(tbl, dry_run=False)   # logs/<tbl>/runv<version>.log (never a dry-run here)
    engine = FakeEngine(cur, prefer_clean=prefer_clean, persist_enabled=True, write_table=XREF)
    engine.attach_log(vlog)   # per-cell log emission (REUSE:<id>/NEW:<id>/GENERATED-NOT-STORED)
    # domain-scoped freetext hardening (fixes C/A/D) -- see run()'s equivalent setup and
    # FIX_GUIDE_freetext_domain_scope_and_forced_company_map.md.
    domain = table_domain(tbl)
    company_forced_map = (load_company_forced_map()
                          if any(c.get('mode') == 'freetext' for c in enabled) else {})
    for c in enabled:                          # 'region' columns: build the real region pool
        if c.get('type') == 'region' and c.get('country_column'):
            # from the SOURCE table (tbl), never anon -- anon may already hold partially-faked
            # region values from an earlier incremental run, which must not pollute the pool.
            engine.load_region_pool(cur, SCHEMA, tbl, c['column'], c['country_column'])
    read_cur = connect().cursor(); read_cur.setoutputsize(10_000_000)
    # one dedup'd select list (composite key may overlap --columns -> avoid ambiguous ORDER BY)
    sel_cols = list(keys)
    for c in col_names:
        if c not in sel_cols: sel_cols.append(c)
    # a column's plan entry may carry "row_filter": {"column": "<other col>", ...} (see
    # run()'s _row_filter_passes) -- that other column must be SELECTed too even though it's
    # not itself being anonymized, or the filter has nothing to read. Same idea for a 'region'
    # column's "country_column" companion (see run()'s equivalent note above load_region_pool).
    for c in enabled:
        rf = c.get('row_filter')
        if rf and rf['column'] not in sel_cols:
            sel_cols.append(rf['column'])
        cc = c.get('country_column')
        if cc and cc not in sel_cols:
            sel_cols.append(cc)
    sel_list = ', '.join(f"[{c}]" for c in sel_cols)
    pos = {c: i for i, c in enumerate(sel_cols)}
    set_clause = ', '.join(f"[{c}]=?" for c in col_names)
    # NULL-safe equality: plain "[col]=?" NEVER matches when the row's actual value is NULL --
    # SQL's "NULL = NULL" is UNKNOWN, not TRUE, so any composite fallback key (used whenever the
    # table has no real unique key) that happens to include a NULL-valued column would silently
    # match ZERO rows for every batch, every time -- confirmed empirically on clm_contract, where
    # ALL 99 rows have external_serial_number/department = NULL: the real run reported success
    # ("MAX ID after the successful run") but updated 0 rows, an entirely silent no-op (same bug
    # class as the earlier clm_contract_context.creator incident). The value is passed twice --
    # once for the "=" branch, once for the "IS NULL" sentinel check -- so ONE static SQL
    # template stays correct for both NULL and non-NULL values without needing per-row SQL text.
    def _null_safe_eq(k):
        return f"([{k}] = ? OR (? IS NULL AND [{k}] IS NULL))"
    where_clause = ' AND '.join(_null_safe_eq(k) for k in keys)

    # fetch this batch's CURRENT column values by explicit key match against the frozen
    # snapshot (never by re-sorting the live table -- see key_rows comment above). Chunked
    # defensively so a composite (multi-column) key never exceeds SQL Server's ~2100
    # parameter ceiling; for the common single-column key this is just one query per batch.
    MAX_PARAMS = 2000
    def _fetch_batch_rows(batch_keys):
        rows = []
        step = max(1, MAX_PARAMS // max(1, len(keys) * 2))
        for i in range(0, len(batch_keys), step):
            chunk = batch_keys[i:i + step]
            if len(keys) == 1:
                # IN(...) has no NULL-equality problem (IS NULL isn't expressible via IN anyway,
                # and a true single-column key -- identity/PK -- is never itself NULL).
                where = f"[{keys[0]}] IN ({', '.join('?' for _ in chunk)})"
                params = [kv[0] for kv in chunk]
            else:
                where = ' OR '.join(
                    '(' + ' AND '.join(_null_safe_eq(k) for k in keys) + ')' for _ in chunk)
                params = [v for kv in chunk for val in kv for v in (val, val)]
            rows.extend(read_cur.execute(
                f"SELECT {sel_list} FROM [{SCHEMA}].[{anon}] WHERE {where}", params).fetchall())
        return rows

    def _inplace_name_hint(email_value, row):
        """Same idea as run()'s _row_name_hint (see its docstring for the full two-route
        rationale): if a companion person column was ALSO included in this --columns batch,
        trust its first/last roles for a matching email instead of guessing from the email's own
        segment order. Falls through to a substring-containment check across separate first-
        role/last-role columns when no single column holds both tokens together."""
        local = email_value.split('@')[0]
        eparts = {p.strip('.').lower() for p in re.split(r'[._\-]+', local) if p}
        if len(eparts) >= 2:
            for c in enabled:
                if c.get('mode') == 'freetext' or c.get('type') != 'person':
                    continue
                idx = pos.get(c['column'])
                if idx is None or row[idx] is None:
                    continue
                pt = _person_tokens(str(row[idx]))
                if pt and {pt[0].lower(), pt[1].lower()} == eparts:
                    return ([(pt[0], False)], [(pt[1], False)])
        local_clean = re.sub(r'[^a-z0-9]', '', local.lower())
        fn_hits, ln_hits = [], []
        for c in enabled:
            if c.get('mode') == 'freetext' or c.get('type') != 'person':
                continue
            idx = pos.get(c['column'])
            if idx is None or row[idx] is None:
                continue
            role = FakeEngine._person_role(c['column'])
            if role not in ('first', 'last'):
                continue
            toks = re.split(r'\s+', str(row[idx]).strip())
            is_whole = len(toks) == 1
            for tok in toks:
                tk = re.sub(r'[^a-z0-9]', '', tok.lower())
                if len(tk) >= 3 and tk in local_clean:
                    (fn_hits if role == 'first' else ln_hits).append((tok, is_whole))
        return (fn_hits, ln_hits) if (fn_hits or ln_hits) else None

    def _inplace_id_hint_pool(row):
        """Same idea as run()'s _row_id_hint_pool: pre-pass, once per row, establishing fakes
        for every enabled id-type column whose OWN raw value has NO `_`/`-` delimiter (an
        'atomic' reference/company code). Must run BEFORE any compound (delimited) id-type
        value in the row is faked -- see the long comment on _row_id_hint_pool for why."""
        pool = {}
        for c in enabled:
            if c.get('mode') == 'freetext' or c.get('type') != 'id':
                continue
            idx = pos.get(c['column'])
            if idx is None or row[idx] is None:
                continue
            raw_val = str(row[idx])
            if len(raw_val) < 2 or re.search(r'[_\-]', raw_val):
                continue
            pool[raw_val] = engine.fake(raw_val, 'id', col=c['column'])
        return pool or None

    signal.signal(signal.SIGINT, _sigint)
    t0 = time.time(); done = ckpt['done']
    while done < target_total:
        take = min(BATCH, target_total - done)
        batch_keys = key_rows[done:done + take]
        if not batch_keys: break
        rows = _fetch_batch_rows(batch_keys)
        if not rows: break
        if len(rows) != len(batch_keys):
            log(f"  WARNING: batch expected {len(batch_keys)} row(s) by key but found {len(rows)} "
                f"-- some snapshotted keys no longer resolve to a row (deleted since snapshot?)")
        upd = []
        for row in rows:
            keyvals = [v for k in keys for v in (row[pos[k]], row[pos[k]])]  # doubled -- see _null_safe_eq
            id_hint_pool = _inplace_id_hint_pool(row)
            newvals = []
            for c in enabled:
                v = row[pos[c['column']]]
                rf = c.get('row_filter')
                if rf and v is not None:
                    rfval = row[pos.get(rf['column'])] if rf['column'] in pos else None
                    if rfval is None or str(rfval) not in rf.get('in', []):
                        newvals.append(v); continue   # filter column doesn't match -- leave untouched
                engine.set_log_context(c['column'])   # every engine-emitted log line for this cell tags this col
                orig_v = v
                if v is not None and c['type'] == 'amount':
                    v = jitter_amount(v)
                    engine._log_outcome(str(orig_v), v, 'GENERATED-NOT-STORED')  # amount bypasses fake()
                elif v is not None and c['mode'] == 'freetext':
                    v = scrub_text(str(v), gl, engine, domain=domain, company_map=company_forced_map)
                elif v is not None:
                    hint = _inplace_name_hint(str(v), row) if (c['type'] == 'email'
                                                               and '@' in str(v)) else None
                    id_hint = id_hint_pool if c['type'] == 'id' else None
                    country_hint = None
                    if c['type'] == 'region' and c.get('country_column'):
                        cidx = pos.get(c['country_column'])
                        if cidx is not None and row[cidx] is not None:
                            country_hint = str(row[cidx])
                    v = engine.fake(v, c['type'], col=c['column'], name_hint=hint, id_hint=id_hint,
                                    country_hint=country_hint)   # col -> role  (logs from inside fake())
                v = fit_width(v, maxlen.get(c['column']))   # #2 word-boundary clamp
                # NOTE: per-cell/per-span log lines are emitted inside engine.fake()/_persist()/
                # flush_pending() -- no double-log here (see corresponding note in run()).
                newvals.append(v)
            upd.append(tuple(newvals) + tuple(keyvals))
        cur.executemany(f"UPDATE [{SCHEMA}].[{anon}] SET {set_clause} WHERE {where_clause}", upd)
        engine.flush_pending(); cur.connection.commit()
        done += len(batch_keys)   # advance by snapshot position, not by rows actually found
        save_json(cp_inplace, {'done': done, 'table': tbl, 'total': total,
                               'updated_at': str(datetime.datetime.now())})
        log(f"  +{len(rows):,}  ({done:,}/{target_total:,})  {time.time()-t0:.0f}s")
        if _PAUSE['stop']:
            log(f"  PAUSED at {done:,}. Re-run the same command to resume."); break
    engine.flush_pending(); cur.connection.commit(); read_cur.connection.close()
    close_value_log(vlog)
    if engine._relabeled:
        log(f"  --prefer-clean rewrote {engine._relabeled} legacy dirty xref fake(s)")
    if done >= total:
        log(f"  COMPLETE — {done:,} rows updated in place in {anon} (never dropped)")
    elif not _PAUSE['stop']:
        log(f"  sample/limit reached at {done:,}. Continue: run {tbl} --limit all --columns {','.join(col_names)}")


# ════════════════════════════════════════════════════════════════════════════════
#  VERIFY / STATUS / RESET
# ════════════════════════════════════════════════════════════════════════════════
def verify(cur, tbl, sample, out_suffix='_anonymized', key_override=None):
    pp, cp = _paths(tbl); plan = load_json(pp)
    anon = re.sub(r'_anonymized$', '', tbl) + out_suffix
    if not tbl_exists(cur, anon): log(f"[{tbl}] no anon table"); return
    src = cur.execute(f"SELECT COUNT(*) FROM [{SCHEMA}].[{tbl}] WITH (NOLOCK)").fetchone()[0]
    dst = cur.execute(f"SELECT COUNT(*) FROM [{SCHEMA}].[{anon}] WITH (NOLOCK)").fetchone()[0]
    enabled = [c for c in (plan['columns'] if plan else []) if c.get('enabled')]
    # per-row join needs a TRUE unique key (identity / declared PK / --order-col).
    # The composite-sortable fallback is NOT unique -> would explode the join, so we
    # only join on a guaranteed-unique key; otherwise use the coincidental upper-bound.
    if key_override:            keys = [key_override]
    else:
        idc = identity_cols(cur, tbl); keys = [next(iter(idc))] if idc else (pk_cols(cur, tbl) or None)
    joinable = bool(keys)
    log("=" * 78); log(f"VERIFY {tbl}: src={src:,} dst={dst:,} "
                       f"({'MATCH' if src==dst else 'PARTIAL/complete pending'})")
    if joinable:
        on = ' AND '.join(f"s.[{k}]=a.[{k}]" for k in keys)
        log(f"  per-row residual keyed on {keys} (accurate: fake == that row's own original)")
    else:
        log(f"  NO unique key — showing COINCIDENTAL upper-bound (fake matches any source value; "
            f"realistic name fakes inflate this). For exact per-row, pass --order-col <unique id>.")
    leaks = 0
    for c in enabled:
        col = c['column']
        # a "row_filter" column is INTENTIONALLY left unchanged for rows where the filter
        # column's value isn't in the allowed set (see run()'s _row_filter_passes) -- e.g.
        # crm_itticket_Dict.Name is only a real person's name when Type is 'Customer'/
        # 'workedby'; the other ~483 rows are category labels that must stay untouched. Without
        # this, verify() would wrongly count every one of those as a "leak".
        rf = c.get('row_filter')
        rf_clause = ''
        if rf:
            allowed = "', '".join(v.replace("'", "''") for v in rf.get('in', []))
            rf_clause = f" AND s.[{rf['column']}] IN ('{allowed}')" if allowed else " AND 1=0"
        try:
            if joinable:                          # TRUE residual: same row's value unchanged
                same = cur.execute(f"""
                  SELECT COUNT(*) FROM [{SCHEMA}].[{tbl}] s WITH(NOLOCK)
                  JOIN [{SCHEMA}].[{anon}] a WITH(NOLOCK) ON {on}
                  WHERE s.[{col}] IS NOT NULL AND LEN(s.[{col}])>1 AND s.[{col}]=a.[{col}]{rf_clause}""").fetchone()[0]
            else:                                 # fallback: coincidental "exists anywhere" (noisier)
                same = cur.execute(f"""
                  SELECT COUNT(*) FROM
                    (SELECT DISTINCT [{col}] v FROM [{SCHEMA}].[{anon}] WITH(NOLOCK)
                     WHERE [{col}] IS NOT NULL AND LEN([{col}])>1) a
                  WHERE EXISTS (SELECT 1 FROM [{SCHEMA}].[{tbl}] s WITH(NOLOCK) WHERE s.[{col}]=a.v{rf_clause})""").fetchone()[0]
        except Exception as e:
            log(f"   {col}: check err {str(e)[:50]}"); continue
        flag = 'RESIDUAL (row unchanged)' if same and c['mode'] == 'structured' else 'ok'
        if same and c['mode'] == 'structured': leaks += 1
        log(f"   {col:30} type={c['type']:8} residual_rows={same:<6} {flag}")
        try:
            ex = [r[0] for r in cur.execute(
                f"SELECT TOP {sample} [{col}] FROM [{SCHEMA}].[{anon}] WITH(NOLOCK) "
                f"WHERE [{col}] IS NOT NULL AND LEN([{col}])>1").fetchall()]
            log(f"      anon samples: {[str(x)[:40] for x in ex[:5]]}")
        except Exception:
            pass
    log(f"  columns with residual rows: {leaks}  -> {'PASS' if leaks==0 and src==dst else 'REVIEW'}")

def status(tbl):
    pp, cp = _paths(tbl)
    plan = load_json(pp); ck = load_json(cp)
    log(f"[{tbl}] plan: {'yes' if plan else 'no'}"
        + (f" ({sum(1 for c in plan['columns'] if c.get('enabled'))} enabled cols)" if plan else ''))
    log(f"[{tbl}] checkpoint: {ck if ck else 'none'}")

def reset(cur, tbl, restart):
    pp, cp = _paths(tbl)
    if os.path.exists(cp): os.remove(cp); log(f"  removed {cp}")
    if restart and tbl_exists(cur, tbl + '_anonymized'):
        cur.execute(f"TRUNCATE TABLE [{SCHEMA}].[{tbl}_anonymized]"); cur.connection.commit()
        log(f"  truncated {tbl}_anonymized")


# ════════════════════════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="from-scratch obi single-table anonymizer",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument('command', choices=['analyze', 'plan', 'run', 'verify', 'status', 'reset'])
    ap.add_argument('table')
    ap.add_argument('--version', type=int, choices=[1, 2],
                    default=int(os.environ.get('OBI_VERSION', '1')),
                    help="logic version: 1=slice + mapping_slice (default, present behaviour); "
                         "2=obi-sql-db + mapping_xref, UPDATE-IN-PLACE if <table>_anonymized already "
                         "exists. Credentials per phase come from anonymizer/.env "
                         "(V1_DATA_CS/V1_MAP_CS, V2_DATA_CS/V2_MAP_CS).")
    ap.add_argument('--schema', default=None,
                    help="override the DATA schema (default 'obi'), e.g. --schema obip1 for the "
                         "obip1.* table batch. The shared mapping table is NEVER affected by this -- "
                         "it always stays in MAP_SCHEMA='obi' regardless.")
    ap.add_argument('--limit', default='100', help="rows this run: N | all | complete")
    ap.add_argument('--sample-rows', type=int, default=SAMPLE_ROWS_DEFAULT, help="analyze sample size")
    ap.add_argument('--order-col', default=None, help="override the resume/order key column")
    ap.add_argument('--restart', action='store_true', help="run: truncate target + reset checkpoint")
    ap.add_argument('--prefer-clean', action='store_true',
                    help="run: regenerate clean fakes for legacy hex/number-suffixed xref values "
                         "(and rewrite those xref rows)")
    ap.add_argument('--dry-run', action='store_true',
                    help="run: write the sample to <table>_script (a throwaway copy) and DO NOT "
                         "write anything to mapping_xref — safe trial that can't affect teammates")
    ap.add_argument('--sample', type=int, default=10, help="verify: #pairs to display")
    ap.add_argument('--out-suffix', default='_anonymized',
                    help="output table suffix on the entity base (e.g. _clean when the source "
                         "table itself is named <entity>_anonymized). Default _anonymized.")
    ap.add_argument('--columns', default=None,
                    help="run: comma-separated columns to anonymize, overriding the plan's "
                         "'enabled' flags. If the target already has foreign rows (no checkpoint), "
                         "those columns are updated IN PLACE (no drop/truncate).")
    ap.add_argument('--batch', type=int, default=BATCH,
                    help=f"run: rows per committed batch (default {BATCH}; raise for big structured "
                         f"tables like ts_mstr to cut round-trips, e.g. --batch 10000)")
    ap.add_argument('--gliner-batch', type=int, default=64,
                    help="freetext: GLiNER sub-batch size per forward pass (default 64; raise on GPU "
                         "e.g. 128/256 for throughput, lower if you hit CUDA OOM)")
    a = ap.parse_args()

    _apply_version(a.version)     # resolve CS / MAP_CS / XREF / STATE_DIR for the chosen version
    if a.schema:
        global SCHEMA
        SCHEMA = a.schema
    log(f"[version {VERSION}] data={_cs_dbname(CS)}.{SCHEMA}  mapping={_cs_dbname(MAP_CS)}.{MAP_SCHEMA}.{XREF}  "
        f"state={os.path.basename(STATE_DIR)}")

    if a.command == 'status':
        status(a.table); return
    if a.command == 'plan':
        pp, _ = _paths(a.table); p = load_json(pp)
        print(json.dumps(p, indent=2, ensure_ascii=False) if p else f"no plan for {a.table}"); return

    cn = connect(); cur = cn.cursor()
    gl = GlinerDetector(batch_size=a.gliner_batch)
    try:
        if a.command == 'analyze': analyze(cur, a.table, a.sample_rows, gl)
        elif a.command == 'run':   run(cur, a.table, a.limit, a.restart, a.order_col, gl,
                                        prefer_clean=a.prefer_clean, dry_run=a.dry_run,
                                        out_suffix=a.out_suffix, only_columns=a.columns,
                                        batch_size=a.batch)
        elif a.command == 'verify':verify(cur, a.table, a.sample, out_suffix=a.out_suffix,
                                           key_override=a.order_col)
        elif a.command == 'reset': reset(cur, a.table, a.restart)
    finally:
        try: cn.commit()
        except Exception: pass
        cn.close()

if __name__ == '__main__':
    main()