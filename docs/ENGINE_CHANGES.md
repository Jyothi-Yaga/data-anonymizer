# Engine enhancements — `obi_anonymizer.py`

This document describes the enhancements made to the anonymization engine over the initial
version. All connection strings in the code are credential-sanitized (`PWD=__SET_VIA_ENV__`);
real credentials are supplied at runtime via environment variables / `.env` (see `.env.example`).

Run the validation tests:
```
python tests/test_gliner_windows.py     # -> 4 passed
python tests/test_engine_changes.py     # -> 8 passed
```

| # | Change | Where |
|---|--------|-------|
| 1 | **Windowed full-cell GLiNER scan** (fixes the `text[:4000]` truncation leak) | `GlinerDetector._windows/_predict_window/entities/entities_batch` |
| 2 | **Length-aware (word-boundary) truncation** — no more mangled half-tokens | `fit_width()`; called in `run()` & `run_inplace()` |
| 3 | **Strict class-preserving `id` generator** (digit→digit, letter→letter, sep kept) | `FakeEngine._gen_guid` |
| 4 | **Reversibility / consistency** | see note below |
| 5 | **Reference deny-list + `country`/`location` types** | `REF_DENY`, `COUNTRY_HINT`, `LOCATION_HINT`, `_gen_country`, `_gen_location`, `analyze()` |
| 6 | **AMOUNT_HINT extension** (revenue/margin/tcv/acv/arr/mrr/effort/forecast/budget/spend…) | `AMOUNT_HINT` / `AMOUNT_SKIP` |
| 7 | **Harvest-time stopword filter** (stops generic-word map pollution) | `HARVEST_STOP` + guard in `FakeEngine.fake()` |

---

### #1 Windowing
The old detector scanned only `text[:4000]`, so any PII past char 4000 in the very large text
cells (up to ~2,000,000 chars) was never detected — a real leak. Now `_windows()` yields
overlapping windows (`WIN_SIZE=4000`, `WIN_OVERLAP=256`) covering `text[:MAX_SCAN]`
(`MAX_SCAN=2,000,000`; exceeding it is **logged, never silent**); spans are shifted to absolute
coordinates and deduped. Both the single (`entities`) and batch (`entities_batch`) paths were
rewritten. Tests prove entities at char 5000/6000 are detected.

### #2 Length-aware truncation
Old code did a blind `v[:ml]` that could cut a fake mid-token (`'Bridgeport Systems'[:12]` →
`'Bridgeport S'`). `fit_width()` prefers the last whitespace boundary ≤ limit (→ `'Bridgeport'`),
hard-cuts only when the first token itself exceeds the column. ID/URL fakes are same-length as
the original, so they never hit this path.

### #3 Class-preserving id
`P3003490` → `J6441060` (was the hex bug `Pa48bee2`); `202112-E00001853` → `293514-S99399964`.
Digits stay digits, letters keep case, separators kept, length preserved. Deterministic (pure
`sha256` seed) → same input → same output across rows/tables/runs.

### #4 Reversibility / consistency
person / email / org / phone — and now **country / location** — flow through `_persist` into the
mapping table (`original→fake`), so they are reversible by reverse-lookup and consistent
everywhere. `id`/`url` are deterministic & consistent across runs by construction (no per-run
randomness) but are **intentionally not persisted** to avoid bloating the map with millions of
GUIDs. No further code change required.

### #5 Reference deny-list + country/location
- `REF_DENY` forces reference/enum columns (`stepname`, `ProductLine`, `Billing_type_name`,
  `applicationname`, `ProjectGroup`, `*typename`, `*category*name`, …) to type `skip` — the real
  value is **preserved verbatim** (these leaked as garbage in earlier runs because they were
  mistyped as person/org).
- `COUNTRY_HINT` → type `country` → `_gen_country`: maps to a **different real country**,
  form-preserving (`US`→2-letter code, `USA`→3-letter, `United States`→full name).
- `LOCATION_HINT` → type `location` → `_gen_location`: maps to a **different real city**.
- Both are deterministic/consistent and persisted (reversible). Wired into `analyze()` ahead of
  the name/org hints.

### #6 AMOUNT_HINT extension
Added revenue/margin/tcv/acv/arr/mrr/effort/forecast/budget/spend/worth/opex/capex to the
money-column detector; extended `AMOUNT_SKIP` with category/preference/msdyn/percent/_pct/flag/
indicator/skip/calculate so OptionSet codes (`msdyn_forecastcategory`, `…preference`) are NOT
jittered.

### #7 Harvest stopword filter
A bare single-token person/org candidate whose normalized form is in `HARVEST_STOP` (email,
mobile, target, gdc, contact, …) is **left unchanged and never persisted** — the forward-fix for
the map pollution that caused earlier free-text over-matches. Multi-word names ("Target
Corporation", "Bounce Marketing LLC") are unaffected.

---

### Companion post-step: `helpers/phase_freetext_scrub.py`
The engine's GLiNER pass detects entities anew but can miss map-known entities that appear inside
long free-text bodies. `phase_freetext_scrub.py` is a map-based whole-cell Aho-Corasick scrub that
replaces any already-mapped Name (multi-token ≥6 chars), CompanyName (≥5), Email or Phone (≥7)
wherever it appears in the configured free-text columns, iterating to convergence. Running it as a
post-step after the engine is recommended for a leak-free result.
