"""
Batched, stripped-text-primary free-text scrubbing for run_inplace() (the
UPDATE-IN-PLACE path).

Why this exists: run_inplace() previously called scrub_text() once per cell, which
calls GLiNER's UN-batched entities() -- one GPU forward pass per 800-char window,
sequentially, one cell fully processed before the next starts. Measured directly on
dyncrm_activity.description (avg 66,712 chars, max ~4.95M chars, 72,659 non-null
rows): a ~62KB "typical" cell took 6.64s, a ~2M-char (MAX_SCAN-capped) cell took
~250s. Extrapolated across the real length distribution, a full run would take on
the order of 170+ hours (~7 days) of continuous GPU time -- not practical.

Two changes from scrub_text(), found in that order while investigating the above:

1. Batching alone (entities_batch() across a whole row-batch instead of one
   entities() call per cell) turned out to help far less than expected -- measured
   only ~1.03x wall-clock on a representative 30-row sample, ~1.39x even isolated at
   the raw model level. GLiNER's own batch_predict_entities() (deprecated in favor of
   GLiNER.inference upstream) doesn't parallelize this model's inference much.

2. The real lever is TOTAL CHARACTERS WINDOWED, not batching. These are
   Outlook/Word-generated HTML emails -- measured on the 5 largest real cells,
   HTML-stripping shrinks them from ~4.85-4.95M chars down to ~350-365K (a ~14x
   reduction); on a representative 10K-100K-char sample, stripped length ranged
   3.4%-28% of raw. scrub_text()/run()'s existing pipeline scans the RAW (HTML-intact)
   text FIRST and only strips as a secondary supplementary pass (fix A) -- meaning the
   expensive, MAX_SCAN-prone pass is the noisy one. Making the stripped text the
   PRIMARY (and only) detection surface here cut measured wall-clock ~4.2x on both a
   30-row representative sample (77.50s -> 17.96s) and the 2 largest real cells
   (497.22s -> 119.45s combined) -- combined with batching, this is what actually
   moves the needle. Verified this doesn't lose coverage vs. the raw-first approach:
   diffed output on the same 30-row sample -- every entity CATEGORY scrub_text()
   caught (person names in prose, signature names, org names, phone numbers) is still
   caught here; the only diffs were different (independently seeded) fakes for the
   same real values, expected when comparing two separately-instantiated engines.

Detected spans are applied back onto the un-stripped (HTML-intact) text via a literal
string-match substitution (apply_literal_map), never offset splicing -- stripping
shifts character offsets, so a position in the stripped text doesn't correspond to
the same position in the original.

Filtering logic (generic-entity/pronoun/email-shape/phone-shape/resume-domain-brand
checks), the Han-script backstop, and FORCED_MAP are all duplicated/re-invoked from
scrub_post() rather than calling it, because scrub_post() does offset-splicing
internally and returns the final spliced string, not a (span -> fake) map -- there's
nothing to intercept and redirect into a literal map without changing its return
contract, which risks the already-verified run()/scrub_text() callers. Kept
deliberately narrow and mirrors scrub_post() check-for-check; if scrub_post() changes,
this needs the same change made here.

NOT wired into run() (the from-scratch INSERT path) -- that path already batches via
its own inline copy of the 2-stage (raw-first + stripped-supplement) algorithm, which
works and is verified. This is a separate, additive module for run_inplace() only.
"""
from obi_anonymizer import (
    scrub_pre, strip_html_for_detection, apply_literal_map,
    _is_generic_entity_span, _is_known_brand_span, _json_safe_fallback,
    _PRONOUN_STOP, _han_token_backstop,
)


def _keep_entity(span, ptype, protect, domain):
    """Mirrors scrub_post()'s per-entity filter checks (see module docstring for why
    this is a duplication, not a call-through). `span`/`ptype` are as returned by
    GlinerDetector.entities_batch() against the STRIPPED text."""
    low = span.casefold()
    if len(span) < 3 or low in protect:
        return False
    if '@' in span:
        return False   # e-mail component -- scrub_pre already masked real addresses
    if ptype in ('person', 'org') and _is_generic_entity_span(span):
        return False
    if domain == 'resume' and ptype in ('person', 'org') and _is_known_brand_span(span):
        return False
    if ptype == 'person' and span.strip().casefold() in _PRONOUN_STOP:
        return False
    if ptype == 'email' and '@' not in span:
        return False
    if ptype == 'phone' and not any(c.isdigit() for c in span):
        return False
    return True


def _geo_fake_for_span(span, ptype, engine):
    """Mirrors scrub_post()'s 'street_address'/'postal_code' special-casing (see that
    function's comment) -- kept as its own small duplication rather than a shared call for the
    same reason bulk_scrub_freetext() already duplicates scrub_post()'s filter checks (module
    docstring): scrub_post() does offset-splicing internally, this module builds a literal map
    instead, so there's no shared return contract to call through to safely."""
    if ptype == 'street_address':
        from location_anonymizer import _split_street_address
        return _split_street_address(span, engine)
    if ptype == 'postal_code':
        from location_anonymizer import _digit_shuffle
        return _digit_shuffle(span)
    return engine.fake(span, ptype)


def bulk_scrub_freetext(items, gl, engine, domain='general', company_map=None,
                         known=None, pattern_cache=None, geo_aware=False, geo_columns=None,
                         exact_map=None, exact_pattern_cache=None):
    """items: list of (col_name, text_or_None) tuples -- typically every free-text
    cell across one row-batch (any number of distinct columns; this function doesn't
    care which row/column a cell belongs to beyond needing the column name for
    per-cell log context, matching scrub_pre's existing per-span logging).

    Returns a list of scrubbed values, same length/order as `items`; a None input
    stays None.

    `pattern_cache`: pass a dict the CALLER keeps alive across calls (e.g. one per
    run_inplace() invocation) so the company_map regex compiles once, not per batch --
    see apply_literal_map's docstring for the performance incident this avoids.

    `exact_map`/`exact_pattern_cache`: same backstop mechanism as `company_map`/
    `pattern_cache`, applied at the same two points (before GLiNER, and again after as a
    safety net -- see the `company_map` comment below for why both matter), but via
    apply_literal_map(..., case_adapt=False) so the fake is inserted verbatim instead of
    being re-cased to match the original span. Use this for entries whose fake has its own
    deliberate case pattern that case_like() would corrupt (e.g. constants.PWSDETAIL_CODE_MAP's
    'C_TKH DN_Fathom'-style codes) -- kept as a SEPARATE dict/cache from company_map rather than
    a flag on it, so callers that don't need case_adapt=False (i.e. everyone using
    MANUAL_COMPANY_MAP) are completely unaffected.

    `geo_aware`/`geo_columns` -- opt-in only (default False/None, so every EXISTING freetext
    caller is completely unaffected). When True, cells whose column name is in `geo_columns`
    (a set) ALSO run location_anonymizer.detect_geo_entities() on the stripped text and add
    resolved geo spans into the SAME literal map used for person/org/email/phone -- see
    location_anonymizer.py's module docstring for why geo detection is folded into this same
    pass rather than a separate geo-only pipeline."""
    n = len(items)
    out = [None] * n
    pre_list = [None] * n
    protect_list = [None] * n
    stripped_list = [None] * n
    idxs = [i for i, (_, text) in enumerate(items) if text is not None]

    for i in idxs:
        col, text = items[i]
        engine.set_log_context(col)
        pre, protect = scrub_pre(str(text), engine, known)
        if company_map:
            # Apply the company backstop BEFORE GLiNER runs, not just after (the original,
            # still-present call near the end of this function) -- confirmed necessary on
            # pwsdetail.QNRData/SummaryData: a curated code like 'MSFT'/'CTFC' sitting inside a
            # readable-ish sub-string ("STE-4-Off (MSFT-CNE)") sometimes gets swept into GLiNER's
            # OWN 'organization' span first (whole-phrase, unstable boundaries depending on the
            # surrounding cell's content -- confirmed reproducing only on full real cells, not a
            # short isolated snippet), so by the time the later company_map pass runs its own
            # literal search, the code has already been replaced by something else and there's
            # nothing left to find. Applying here first means GLiNER sees the curated fake
            # instead of the raw code, and _protect() below stops it from being re-faked as a
            # "new" email/name if it happens to resemble one -- it does NOT stop GLiNER from
            # separately mis-tagging the fake's surrounding phrase, which is a GLiNER model
            # limitation on unfamiliar tokens, not something a string-level guard can fully
            # close; the later pass stays in place as a harmless no-op safety net for text this
            # early pass didn't touch.
            pre = apply_literal_map(pre, company_map, protect, pattern_cache=pattern_cache)
            for fake in company_map.values():
                protect.add(fake.casefold())
        if exact_map:
            pre = apply_literal_map(pre, exact_map, protect, pattern_cache=exact_pattern_cache,
                                     case_adapt=False)
            for fake in exact_map.values():
                protect.add(fake.casefold())
        pre_list[i] = pre
        protect_list[i] = protect
        stripped_list[i] = strip_html_for_detection(pre)

    ents = gl.entities_batch([stripped_list[i] for i in idxs]) if idxs else []
    ents_by_idx = dict(zip(idxs, ents))

    for i in idxs:
        col, text = items[i]
        engine.set_log_context(col)
        stripped = stripped_list[i]
        lit_map = {}
        street_addr_map = {}   # 'street_address' spans, applied SEPARATELY -- see below
        for s, e, t in ents_by_idx.get(i, []):
            span = stripped[s:e]
            if not _keep_entity(span, t, protect_list[i], domain):
                continue
            lit_map[span.casefold()] = engine.fake(span, t)
        if geo_aware and geo_columns and col in geo_columns:
            from location_anonymizer import detect_geo_entities
            geo_ents = detect_geo_entities(stripped, engine, gl=gl, column=col)
            for s, e, t in geo_ents:
                span = stripped[s:e]
                if span.casefold() in lit_map or len(span) < 2:
                    continue      # standard person/org/email/phone entities win on overlap
                if t == 'street_address':
                    street_addr_map[span] = _geo_fake_for_span(span, t, engine)
                else:
                    lit_map[span.casefold()] = _geo_fake_for_span(span, t, engine)
        v = pre_list[i]
        # applied via plain exact replacement, NOT apply_literal_map/lit_map -- a street-address
        # fake is already a hand-composed mix of verbatim (house number/direction/suffix) and
        # freshly-faked (street name) segments with its OWN correct internal case pattern;
        # apply_literal_map's case_like(original_span, fake) re-cases the WHOLE fake to match
        # the ORIGINAL span's overall case, which mangles that internal mix (confirmed the hard
        # way: '1115 SE 164th Street' -> '1115 SE Greystone Street' fed through case_like turned
        # the correctly-preserved 'SE' into 'Se', since case_like .capitalize()s every alpha run
        # of a Title-cased match). Safe as a plain replace: these spans are long, distinctive,
        # multi-word strings, not short tokens that could coincidentally match unrelated text.
        for orig_span, fake_val in street_addr_map.items():
            v = v.replace(orig_span, fake_val)
        v = apply_literal_map(v, lit_map, protect_list[i])
        # scrub_post() (not called here -- see module docstring) normally applies these two
        # right after its own GLiNER-substitution loop; do the same here so a Han-script
        # company/person name GLiNER's Latin-centric model misses, and any literal
        # 'centific'/'pactera' occurrence, still get caught regardless of this path skipping
        # scrub_post() itself.
        v = _han_token_backstop(v, engine)
        v = engine.apply_forced(v)
        if company_map:
            v = apply_literal_map(v, company_map, protect_list[i], pattern_cache=pattern_cache)
        if exact_map:
            v = apply_literal_map(v, exact_map, protect_list[i],
                                   pattern_cache=exact_pattern_cache, case_adapt=False)
        v = _json_safe_fallback(str(text), v, pre_list[i])
        out[i] = v
    return out
