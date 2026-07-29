# Anonymizing Chinese-Language Data — Approach, Rationale, Coverage

Status date: 2026-07-17. Scope: the `obip1.*` table batch (contract-lifecycle-management
`clm_*`, Chinese recruiting/ATS `sift_bus_*`, resume-parsing `ResumeMapping_*`/`ResumeRating_*`,
and the Dynamics `crm_itticket*` export) — the first batch in this project with real
Simplified-Chinese PII in production data.

---

## 1. Why this needed its own approach

The core engine (`obi_anonymizer.py`) was built and proven on English-language `obi.*` tables:
Western names, Latin-script company names, US/EU-style addresses. Direct sampling of the newer
`obip1.*` tables surfaced **real Simplified Chinese PII already in production**:

- Candidate/staff names in Han script, plus a recurring bilingual compound: a pinyin/Latin name
  followed by the real Chinese name in parentheses — `Xiaoting Li （李晓婷）`.
- Chinese company/customer names, Chinese city/region strings.
- QQ-based personal emails (`<digits>@qq.com`).
- Full bilingual resume text and JSON-embedded resume fields.

None of the engine's existing logic was written to handle this correctly:
- The whitespace tokenizer used for Western "First Last" names would split
  `Xiaoting Li （李晓婷）` into three unrelated tokens and fake each independently — the Latin
  half and the Chinese half would end up describing **two different fake people**, and the
  parentheses would be lost.
- A standalone Chinese full name like `李晓婷` (surname + given name run together, no space, per
  Chinese convention) would be seen as a single token and faked as a first-name-only value,
  **silently dropping the surname**.
- Faking a Chinese city or company name against the existing English `CITIES` / org-name pools
  would replace it with an English placeholder — breaking script consistency in an otherwise
  all-Chinese field.

So a dedicated Chinese-aware code path was added rather than stretching the English logic to
cover it implicitly.

---

## 2. What we're using

### 2.1 Architecture (unchanged core)

Chinese handling is an **extension of the existing engine**, not a separate pipeline:

- **`obi_anonymizer.py`** — the single engine for every table. Same plan → run → verify
  workflow, same `mapping_xref` reuse-first dictionary, same deterministic seeded
  `FakeEngine`.
- **`obi_chinese_anonymizer.py`** (new, `data-slice-anonymization/obi_chinese_anonymizer.py`)
  — a small, stateless helper module imported by the engine (`import obi_chinese_anonymizer as
  chinese_anon`). It holds no cache and writes nothing to `mapping_xref` itself; it's called
  *from* `FakeEngine._gen_person` / `_gen_org` / `_gen_location` with the engine's own seed
  function, so injectivity (`self._used`) and reuse-first persistence keep working exactly as
  they do for every other type.

### 2.2 Detection: routing by script, not language-ID

There's no separate "Chinese mode" you switch on. Every generator checks the value's script
directly:

```python
def is_han(s):
    return any('一' <= c <= '鿿' for c in (s or ''))
```

If a value (or a parsed sub-span of it) is Han script, it's routed to the Chinese-aware
generator instead of the Western one. This is cheap, has no false-negative risk from a
language-detector guessing wrong, and works per-value rather than per-column (a column can mix
scripts row to row).

### 2.3 Generation: matched Chinese/pinyin pairs from Faker's `zh_CN` locale

- `self.fk_zh = Faker('zh_CN')` (already loaded at `FakeEngine.__init__`, alongside `fk_en`/
  `fk_in`) supplies real Chinese surname/given-name draws.
- `pypinyin.lazy_pinyin(...)` transliterates a Han name to pinyin.
- For the bilingual pattern, **both halves are generated from ONE seed pair** (surname seed +
  given-name seed derived from the parsed original), then one is kept in Han script and the
  other transliterated — so the Latin and Chinese halves always describe the *same* fake
  identity instead of two independently-rolled fakes that could disagree.

### 2.4 Detection *inside* free text: GLiNER — with a known, unresolved gap

Free-text columns (chat bodies, descriptions, resume blobs) go through **GLiNER**
(`urchade/gliner_multi_pii-v1`), the same NER model used for English free text, detecting
`person` / `organization` / `email` / `phone number` spans at a 0.5 threshold.

**This is the one open gap in current Chinese coverage.** Research into that specific
checkpoint's model card describes 6 training languages, apparently all European — Chinese is
not confirmed to be among them. A broader "100+ languages" claim exists for the general GLiNER
architecture, but that's a different, non-PII-tuned model; it is not evidence this checkpoint
detects Chinese PII labels correctly. **No in-house empirical test on real Chinese text has been
run yet.** Until one is, every Chinese-language free-text column is treated as *unverified* for
GLiNER, and none have been anonymized (see §5).

### 2.5 Supporting libraries

| Library | Used for |
|---|---|
| `faker` (`zh_CN` locale) | drawing real Chinese surnames / given names for fake identities |
| `pypinyin` | transliterating a Han name to pinyin for the Latin half of a bilingual pair |
| `gliner` (`urchade/gliner_multi_pii-v1`) | detecting embedded PII inside free text (English-proven; Chinese coverage unconfirmed) |
| `ethnicseer` | ethnicity signal for routing plain-Latin tokens to the right name pool (Chinese vs. Indian vs. Western) — separate from the `is_han` script check |

---

## 3. Why this approach (design rationale)

1. **One engine, one dictionary — not a Chinese-specific side pipeline.** Every fake still goes
   through the same `mapping_xref` original→fake table used across the whole database. A person
   who appears in both an English-language table and a Chinese `clm_*` table gets a single,
   consistent fake identity everywhere their real value matches — the Chinese module just
   supplies the generator when Han script is detected; it doesn't fork the persistence or reuse
   logic.
2. **Deterministic, seeded generation over an LLM.** The whole engine — Chinese path included —
   is deterministic (seeded hash → generator), not an LLM asked to "rewrite this text safely."
   Research done for this batch (see §4) converges on the same reason: an LLM asked to redact
   text can silently skip an entity, flag something that isn't PII, or hallucinate PII-shaped
   text back in. A fixed-label NER model (GLiNER) plus a deterministic generator gives an
   exhaustive, reproducible, auditable scan — the LLM's proper role here is at most a
   *second-pass* supplementary scrubber, never the sole detector.
3. **Script consistency is a correctness requirement, not polish.** A Chinese company name faked
   to an English word, or a Chinese city faked to `Lakewood`, would be an obvious tell that the
   field was tampered with and would break any downstream process that expects a Han-script
   value in that field. Every Chinese generator (`gen_chinese_org`, `gen_chinese_location`,
   `gen_chinese_fullname`) picks from a Han-script pool and never crosses scripts.
4. **The bilingual pattern needs joint generation, not independent per-half faking**, because
   both halves name the *same* person — see §1 for what breaks otherwise. Generating from one
   seed pair and rendering two views of it is the only way to keep them describing one identity.
5. **GLiNER's Chinese gap is being tracked, not assumed away.** Rather than quietly trusting an
   unverified capability (which is exactly how the earlier GLiNER-unavailable-in-environment leak
   happened on this project — freetext columns silently fell back to raw pass-through), Chinese
   free-text columns are explicitly deferred (status `PENDING`, "Chinese deferred" in the tracking
   sheet) until the coverage question is settled empirically.
6. **Invented-name pools, checked against real companies.** The Chinese org fake-name pool
   (`CN_ORG_HEAD`/`CN_ORG_TAIL`) deliberately uses poetic/nature-imagery character combinations
   that are *not* standard Chinese corporate-naming vocabulary. An earlier draft of the list used
   common auspicious-business words and two entries turned out to be name-roots of real companies
   (正泰 = Chint Group; 尚德 = Suntech Power) — replaced after a user-verified spot-check. The
   same discipline used for the Latin `ORG_HEAD` pool.

---

## 4. Available approaches surveyed (and what we picked)

Before extending the engine, a research pass surveyed what else exists for Chinese-language PII
detection/anonymization, on-prem-vs-cloud, and LLM-vs-classical-NER. Summary:

| Approach | Verdict for this project |
|---|---|
| **GLiNER + deterministic FakeEngine (current architecture)** | **Kept as the core.** Nothing surveyed beats it for structured columns; the only actual gap is unconfirmed Chinese coverage in free text. |
| Microsoft Presidio | Open-source, self-hosted, no data egress. Only as good as the NER model plugged in (default spaCy models are modest). Worth adding as an *additional* regex/context-rule layer for high-precision structured formats (contract numbers, CN ID numbers) — not a replacement. |
| AWS Comprehend PII detection | Cloud API; real-time detection documented for English/Spanish only. Requires sending data to AWS. **Ruled out** — data egress, and no Chinese support. |
| Google Cloud DLP | Broadest detector library (70+ infoTypes) but a managed API call — data egress to Google Cloud required. **Ruled out** for the same reason. |
| Azure AI Language PII (on-prem "disconnected container") | The one cloud-vendor option with a genuine on-prem mode (container only sends billing pings, not text); supports 70+ languages. Worth a closer look **only if** its no-data-leaves-network claim is independently verified — not yet adopted. |
| HanLP (Chinese NER pipeline, e.g. MSRA_NER_BERT_BASE_ZH) | Confirmed real, actively maintained, tags PER/LOC/ORG for Chinese specifically. **Candidate fallback/replacement** for the free-text Chinese gap. |
| `ckiplab/bert-base-chinese-ner` | Confirmed real, but trained toward Traditional Chinese — recall against this project's Simplified-Chinese data needs checking before relying on it. |
| Local SLM/LLM second pass (Qwen2.5-7B/14B, Phi-3.5-mini, Llama-3.1-8B) | Qwen2.5 has heavy Chinese pretraining and is broadly reported strong on Chinese benchmarks generally (no verified Chinese-NER-specific score found). **Candidate second-pass** scrubber for paraphrased/contextual PII a span-based NER model misses — never as the sole detector (see §3.2 on why). |
| spaCy (`en_core_web_lg`, `xx_ent_wiki_sm`) | Neither is PII-specialized; the multilingual model is trained on formal Wikipedia text, a poor match for informal tickets/resumes. **Not adopted.** |
| Jieba | Segmentation/POS only, not a PII/NER tool by itself — would need pairing with a tagger. Not a standalone option. |

**Net decision:** keep GLiNER + the deterministic engine as the architecture; run an empirical
Chinese-text test of `gliner_multi_pii-v1` before trusting it on any real Chinese free-text
column; if it underperforms, HanLP or a locally-hosted Qwen2.5-7B/14B are the queued fallbacks.
Presidio is a candidate *additional* layer for pattern-perfect structured formats (CN ID numbers,
ticket numbers), independent of the Chinese-language question. No cloud PII API is in scope
(data-egress constraint), except Azure's on-prem container as an unverified maybe.

---

## 5. Cases covered today (structured columns — live, working)

These all run through `FakeEngine` today and don't depend on GLiNER at all — they're triggered
purely by Han-script detection on a structured (whole-cell) value.

### 5.1 Bilingual "Latin Name （中文名）" staff names

The single most common Chinese-PII shape in the `clm_*` (contract) tables:
`creator_name` / `updater_name` and similar staff-name columns.

```
BEFORE: Xiaoting Li （李晓婷）
AFTER : Kun Zhang （张坤）
```

Both halves are generated from one seed pair (`gen_bilingual_name`), so the Latin and Chinese
names always describe one consistent fake person — never two unrelated identities.

### 5.2 The same pattern glued directly to an email, no separator

Real source data glues the email straight onto the closing paren with no space
(`...（郭超辉）chaohui.guo@centific.com`) — the parser (`_BILINGUAL_RE`) handles this directly,
and the email's fake local-part is derived from the *same* fake given/surname just generated for
the name, so the name and email never disagree:

```
BEFORE: Chaohui Guo （郭超辉）chaohui.guo@centific.com
AFTER : Lihua Lin （林丽华）lihua.lin@aventraa.com
```

(The domain change to `aventraa.com` is the existing forced-word rule — `centific → aventraa` —
applied the same way it is everywhere else in the engine, not something Chinese-specific.)

### 5.3 Standalone Chinese full name (no Latin annotation)

E.g. a `name` column holding just `李晓婷` with no bracketed Latin form. Chinese convention runs
surname + given name together with no space, so this is split deterministically into its two
components (each faked from its own seed) and rejoined the same way — not treated as one
first-name-only token:

```
BEFORE: 李晓婷
AFTER : 张欣
```

### 5.4 Chinese organization/company name

```
BEFORE: 华鑫科技
AFTER : 云璃控股
```

Faked from a head+tail pool of invented, poetic/nature-imagery character pairs (`CN_ORG_HEAD` +
`CN_ORG_TAIL`) — checked against real Chinese company names (see §3.6) so the fake output can't
collide with an actual business.

### 5.5 Chinese city / location

```
BEFORE: 北京
AFTER : 沈阳
```

Maps to a *different* real Chinese city from a fixed pool (`CN_CITIES`) spanning multiple
provinces/regions, mirroring how the Latin `CITIES` pool already works for Western location
values — never an English placeholder.

---

## 6. Cases NOT yet covered (deferred, with why)

As of 2026-07-17, across the 38-table `obip1` batch: 35 tables have confirmed Chinese content;
2 tables are fully done; 3 are partially done (their non-Chinese columns are anonymized, the
Chinese-language columns on those same tables are pending); the remaining tables — and every
Chinese-language column identified across the whole batch — are still `PENDING`. Roughly 95 of
206 requested columns across the batch are deferred specifically because they carry Chinese
content.

### 6.1 Chinese free text (the main open item)

Resume bodies, job descriptions, IT-ticket text, candidate remarks — e.g.
`sift_bus_resume.standard_resume`, `sift_bus_resume.resume_content`,
`ResumeRating_Job_Requisition.job_description`, `crm_itticket.description`. **Not anonymized
yet.** GLiNER is the detector this engine uses for free text everywhere else, but as noted in
§2.4/§4, its Chinese coverage is unconfirmed and the working assumption is that it's likely
insufficient until proven otherwise by a direct test on real (sanitized) Chinese text. Shipping
these columns without that verification would risk repeating the exact class of bug already
found once on this project (GLiNER silently unavailable → freetext columns leaked raw, see the
project's pending-repairs tracker) — except here the risk is GLiNER running but not actually
tagging Chinese entities, which would look identical to a clean pass while leaking everything.

### 6.2 JSON-embedded resume PII

`ResumeMapping_Resume_ParsingLog.Outputs` / `.Resume_Info`,
`sift_bus_resume.standard_resume` — these store a fully parsed resume as a JSON blob with PII
sitting in named string fields (name, education, skills, etc.). Running the flat freetext
scrub/NER pass over the raw JSON text as-is would mis-scope matches (spans could straddle
punctuation/structure, or the scrubber could corrupt the JSON). Needs a thin
parse → scrub the string leaves → reserialize wrapper; not built yet.

### 6.3 Filenames embedding a real name

Resume/attachment filenames routinely embed a real candidate or staff name (e.g.
`original_file_name`, `blob_file_name` columns across the `clm_*` and `sift_bus_resume` tables).
Same class of issue as an already-documented embedded-name-in-path bug for the other (non-Chinese)
table lineage — worth checking whether that existing fix pattern extends here before building a
new one.

### 6.4 Chinese Resident Identity Card numbers

No ID-number column has actually been observed yet in this batch's sampled data, but given how
much candidate/employee data these tables carry, it's flagged as a pattern worth a dedicated
recognizer if one turns up: 18 digits — 6-digit region code + 8-digit birth date (YYYYMMDD) +
3-digit sequence (odd=male/even=female) + 1 checksum digit (ISO 7064 MOD 11-2). A shape-preserving
fake would need to scramble the embedded birth date too, not just the checksum, since that
sub-field is separately sensitive. Not implemented — no confirmed occurrence yet to build against.

### 6.5 Two signals identified but not yet wired into detection rules

- **Mainland mobile numbers** (`^1[3-9]\d{9}$`) — observed directly in `sift_bus_candidate.phone`;
  currently faked via the generic `phone` type (format-preserving digit substitution), which is
  adequate, but a dedicated recognizer isn't yet cross-checked against carrier-prefix validity.
- **QQ-number emails** (`^\d{6,18}@qq\.com$`) — observed directly and repeatedly in
  `sift_bus_candidate.email` and inside resume free text. Currently handled by the generic
  `email` type when it's a standalone column value; not yet given its own recognizer rule to
  route it to person-linked fake generation when it appears *inside* free text (where GLiNER
  would need to catch it, and per §6.1, Chinese free text isn't verified yet).

---

## 7. Where to look in the code

| File | Role |
|---|---|
| `data-slice-anonymization/obi_anonymizer.py` | the engine — `FakeEngine._gen_person`/`_gen_org`/`_gen_location` call into the Chinese module when `chinese_anon.is_han(...)` is true; `GlinerDetector` class is the free-text NER wrapper |
| `data-slice-anonymization/obi_chinese_anonymizer.py` | all Chinese-specific logic: bilingual-pattern parsing/formatting, Chinese name/org/location generators, Han-script detection |
| `data-slice-anonymization/docs/ANONYMIZATION_APPROACH.md` | the general (language-agnostic) engine approach — read this first if unfamiliar with the plan → run → verify workflow |
| `claude/obip1_new_tables_pii_analysis.xlsx` | full per-column PII classification for the batch, plus the `Research_LLM_SLM_Tools` and `Research_Chinese_PII` tabs this document draws from |
| `claude/obip1_chinese_content_and_status_new.xlsx` | live per-table status: which columns are done, which are deferred for Chinese content |
