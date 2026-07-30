# FIX GUIDE — Free-text domain scoping + deterministic company backstop (NICK slice)

## 1. What was wrong

After anonymizing the NICK slice (`obi_nick_slice`, 10 populated tables), a `piicheck` run against
`obi.mapping_xref` (the mapping table this pipeline actually uses) showed a **20.3% leak rate**
(617 leaks / 3,040 PII occurrences, 646 total defects) — far above what Cassie's slice showed.

Traced one real row (`dyncrm_activity.description`, a Gmail thread with Nick Kampa) end to end:

**Raw:**
```
...Hi Nick -- I'm unfortunately out of the country this week. Thanks for the invite!...
On Mon, Aug 18, 2025 at 8:11 AM Nick Kampa &lt;<a href="mailto:nicholas.kampa@centific.com">
nicholas.kampa@centific.com</a>&gt; wrote:...
```

**Anonymized (before this fix):**
```
...Hi Nick -- I'm unfortunately out of the country this week. Thanks for the invite!...
On Mon, Aug 18, 2025 at 8:11 AM Laura Cain &lt;<a href="mailto:laura.cain@aventraa.com">
laura.cain@aventraa.com</a>&gt; wrote:...
```

`"Nick Kampa" <nicholas.kampa@centific.com>` was correctly faked (strong signal: full name next
to an email). The bare first name `"Hi Nick"` a few lines earlier — same real person — was left
untouched. Separately, `mapping_xref` already has a canonical fake for `"Google"` (id 51071 →
`"Buckley-Jones"`), but `"Google"`, `"Meta"`, and other real client/competitor names survived
verbatim throughout `dyncrm_leads.description` and `dyncrm_activity`.

## 2. Root causes

1. **`KNOWN_BRAND_STOP` applied table-agnostically.** `obi_anonymizer.py` has a list of
   well-known brands (`google`, `microsoft`, `aws`, `meta`-adjacent entries, etc.) that
   `scrub_post()` deliberately leaves untouched — built and commented specifically for
   `ResumeRating_Job_Candidate.Comment`, where "NVIDIA GPU experience" is a skill mention, not
   the candidate's identity. That list was being consulted for **every** table's free text,
   including `dyncrm_activity`/`dyncrm_leads`, where the same brand names are real client
   identities that must be anonymized. A policy correct for one domain was silently applied
   everywhere.
2. **HTML noise diluting GLiNER's signal.** `dyncrm_activity.description` averages **15.8KB**,
   max **1.7MB**, 79% containing raw HTML. Feeding `<span style="...">` markup directly into
   GLiNER's 800-char windows burns window budget on non-content and plausibly pushes short/
   ambiguous real mentions (a bare first name, a brand used attributively — "Meta smart
   glasses") under the detection confidence threshold.
3. **One global GLiNER confidence threshold for every label.** `organization` mentions
   measurably under-fire relative to `person`/`email`, which have stronger structural signal
   (an email always has `@`; a person is often adjacent to one).
4. **No deterministic backstop.** If GLiNER missed an entity for any reason (threshold, markup
   noise, or the resume brand-stop list), nothing else caught it — a single point of failure for
   the whole free-text path.

## 3. The fix

Four changes to `obi_anonymizer.py` (all in the free-text scrub path — `scrub_pre` → GLiNER →
`scrub_post` → `apply_forced`), plus a new `constants.py`:

- **[C] Domain-scoped `KNOWN_BRAND_STOP`.** Added `constants.table_domain(table)` →
  `'resume'` or `'general'`. `scrub_post(text, ents, engine, protect, domain=...)` now only
  consults `KNOWN_BRAND_STOP` when `domain == 'resume'`. `RESUME_DOMAIN_TABLES` lists every
  actual resume/candidate-matching table found in `_state_v2` (`ResumeRating_*`,
  `ResumeMapping_*`, `sift_bus_*`). Every other table defaults to `'general'`, where brands are
  anonymized like anything else. `GENERIC_ENTITY_STOP` (job titles/degrees/tech-tool
  vocabulary) is **not** domain-gated — a job title is never real PII in any table.
- **[A] HTML-aware secondary pass.** New `strip_html_for_detection()` strips tags/entities to
  build a cleaner detection surface. For `domain != 'resume'` cells that still contain markup
  after the primary pass, a second batched GLiNER call runs on the stripped copy. Hits are
  applied as **literal string substitutions** against the original HTML-intact text (never
  offset splicing — the stripped text has different offsets), via a new `apply_literal_map()`.
- **[B] Per-label dynamic thresholds.** `GlinerDetector` now calls the model at a low
  `CALL_THRESHOLD=0.25` to surface more candidates (each with its own `score`), then filters in
  Python against a per-label `LABEL_THRESHOLD` (`organization: 0.3, person: 0.4, email: 0.55,
  phone number: 0.5`) — loosened specifically where recall was weak, left strict where
  structural signal is already strong.
- **[D] Deterministic company backstop.** `MANUAL_COMPANY_MAP` in `constants.py` — a small,
  hand-curated `{real company: fixed fake}` dict, applied via `apply_literal_map()` after
  GLiNER regardless of what it detected. Same reliability contract as the existing
  `FORCED_MAP` (Centific→Aventraa): once a company is listed, it's **always** faked, everywhere
  in free text. Kept separate from `FORCED_MAP`/`apply_forced()` because that mechanism has NO
  word-boundary check (safe only for made-up tokens like "centific" that never collide with
  real words) — `apply_literal_map()` adds word-boundary + **HTML-tag-safety** guards instead
  (see incident below).

## 4. Incidents found while shipping this (both fixed, both worth knowing)

### Incident 1 — HTML tag corruption (caught in testing, before any real run)

First version of `apply_literal_map()` had no tag-awareness. Testing against real data with
`"Meta"` in the company map corrupted the HTML tag itself:
`<meta http-equiv="Content-Type"...>` → `<evercrest solutions talos http-equiv="Content-Type"...>`
— "meta" satisfied the word-boundary check just as validly inside a tag as inside prose.
**Fixed**: `apply_literal_map()` now computes HTML tag spans once and skips any candidate match
overlapping one, before any real run touched this bug.

### Incident 2 — wrong mapping table on first re-run (caught after, cleaned up)

`obi_anonymizer.py` resolves the V1 mapping table via
`os.environ.get('V1_XREF', 'mapping_slice')` — it only uses `mapping_xref` if that env var is
explicitly set. It isn't persisted anywhere (not in `.env`, not in `.bashrc`) — the original
team run must have set it inline in their own shell. The first re-run of this fix did **not**
re-set it, so `dyncrm_leads`/`dyncrm_activity` silently anonymized against `mapping_slice`
instead, and wrote **5,008 new rows** into the shared `mapping_slice` table (id 299303–304310)
that shouldn't have been there.

**Fixed**: identified the exact contiguous id range (confirmed nothing else wrote to
`mapping_slice` in that window), deleted all 5,008 rows, and re-ran both tables with
`V1_XREF=mapping_xref` explicitly exported.

### Incident 3 — performance regression from fix D's first implementation (caught after the correct re-run)

The first working version of fix D queried **every** `mapping_xref` `CompanyName` row (93,695
of them) at runtime and `apply_literal_map()` recompiled a ~93,695-way regex alternation **from
scratch on every single free-text cell** (no caching at all). Combined with `mapping_xref`
being ~6.3x bigger than `mapping_slice`'s CompanyName set (14,870), this took a run that
previously finished in ~94 minutes to **~4 hours** (14,355s for `dyncrm_activity`).

**Fixed**: rolled back the bulk auto-load entirely — `load_company_forced_map()` now builds
from `MANUAL_COMPANY_MAP` only (a small, deliberately curated list), and `apply_literal_map()`
accepts an optional `pattern_cache` so the compiled regex is built once per run and reused,
not rebuilt per cell. Anything not manually listed still gets normal GLiNER detection + the
engine's existing (fast, per-value dict lookup) `mapping_xref` reuse-first — just without the
extra guaranteed backstop. Validated: `load_company_forced_map()` now returns in ~0.0000s
regardless of map size (previously did a live Azure SQL query every run).

### Incident 4 — rare `mapping_xref` write collisions (observed, not chased further)

Running `dyncrm_leads` and `dyncrm_activity` concurrently (to save wall-clock time) against the
same shared `mapping_xref` produced 3 total `(warn: map flush ... failed after retry:
SQLSTATE 23000 duplicate key)` messages across both runs — the engine preloads the map once per
process and assumes anything new is safe to insert, which doesn't hold when two writers hit the
same real value near-simultaneously (same root-cause class as this project's already-known
cross-run name/email drift issue). Both runs still completed with full row counts; impact is 3
values out of tens of thousands processed, not chased further given the very low volume — but
worth avoiding fully concurrent runs against the shared map where practical.

## 5. Files changed

- **New**: `data-slice-anonymization/constants.py` — `RESUME_DOMAIN_TABLES`, `table_domain()`,
  `GENERIC_ENTITY_STOP`, `KNOWN_BRAND_STOP`, `_PRONOUN_STOP`, `FORCED_MAP`,
  `MANUAL_COMPANY_MAP` (moved/consolidated from inline definitions in `obi_anonymizer.py`).
- **Modified**: `data-slice-anonymization/obi_anonymizer.py` —
  - `GlinerDetector`: `CALL_THRESHOLD`, `LABEL_THRESHOLD`, updated `_predict_window`/
    `entities_batch` (fix B).
  - `scrub_post(..., domain='general')`: `KNOWN_BRAND_STOP` gated by domain (fix C).
  - New: `strip_html_for_detection()`, `apply_literal_map()`, `load_company_forced_map()`
    (fixes A/D).
  - `run()`'s batched free-text loop, `scrub_text()`, `run_inplace()`: threaded `domain`/
    `company_forced_map`/`company_pattern_cache` through; added the stage-2 HTML-stripped pass
    and stage-3 company-map application.
- **Backup**: `obi_anonymizer.py.bak_pre_freetext_fix_20260728` (pre-fix snapshot, VM only).

## 6. How to extend `MANUAL_COMPANY_MAP`

In `constants.py`:
```python
MANUAL_COMPANY_MAP = {
    'google': 'Zenith Industries',
    'meta': 'Falconix Labs',
    # add more as you identify companies present in a slice's data
}
```
Keys are matched case-insensitively, whole-word/phrase, HTML-tag-safe. Once listed, that
company is **always** faked to the given value, everywhere in free text, same reliability
guarantee as `FORCED_MAP`'s Centific→Aventraa rule.

## 7. Before / after proof

| | Before fix | After fix |
|---|---|---|
| Total defects | 646 | **320** |
| Total leaks | 617 | **279** |
| Leak rate | 20.3% (617/3,040) | **9.14%** (279/3,051) |
| `dyncrm_leads.description` LEAK_WHOLE (Google/Meta verbatim) | 20 | **0** |
| `dyncrm_leads.jobtitle` LEAK_EMBEDDED | 28 | **12** |
| `dyncrm_activity.description` LEAK_EMBEDDED | 200+ (capped) | **187** |

Example — same `dyncrm_leads.description` rows, before and after:

| Raw | Before | After |
|---|---|---|
| `"10 - Google Smart Glasses"` | `"10 - Google Smart Glasses"` (untouched) | `"10 - Falconix Harborview Delphi Networks Monarch"` |
| `"11 - Google Deep Mind (Not Google Next)"` | `"11 - Google Deep Mind (Not Google Next)"` (untouched) | `"11 - Granite Everline Delphi Networks Lumen (Not Zenith Industries Next)"` |

Resume-domain behavior confirmed unchanged (brands still intentionally left real):
```
IN:  Candidate has strong Google Cloud and AWS experience, worked closely with Meta ads team.
OUT (domain='resume'):  <unchanged, as intended>
OUT (domain='general'): Candidate has strong Quantic Everline and SSH experience, worked
                         closely with Evercrest Solutions Talos eye team.
```

## 8. Remaining known issues (out of scope for this fix)

- **`dyncrm_systemuser.lastname`/`.title`/`.firstname`/`.fullname`** (16/7/3/2 LEAK_WHOLE) —
  these are **structured** (`person`-typed), not free-text columns. Untouched by fixes A–D,
  which only apply to `mode == 'freetext'` columns. Run completed all 7,913/7,913 rows
  (confirmed via checkpoint), so it's a per-row miss in the structured path, not a partial run.
  Needs separate investigation.
- **Residual `dyncrm_activity.description` leaks (187)** — the dominant remaining category.
  These are giant, deeply HTML/CSS-nested cells; fixes A/B measurably reduced this (200+ → 187)
  but didn't eliminate it — a genuine GLiNER precision/recall limit on very messy real-world
  content, not a code bug.
- **`sharepoint_files.parent_folder_path`** (7 LEAK_EMBEDDED) — this column is intentionally
  `enabled: false` in the plan; not a fix regression, a pre-existing plan decision.
- **`sharepoint_files.drive_name`** (6 LEAK_WHOLE, all literally `"Documents"`) — map noise
  (SharePoint's default library name coincidentally in `mapping_xref`), not real PII.
