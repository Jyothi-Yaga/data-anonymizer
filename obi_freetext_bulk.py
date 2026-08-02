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


def bulk_scrub_freetext(items, gl, engine, domain='general', company_map=None,
                         known=None, pattern_cache=None):
    """items: list of (col_name, text_or_None) tuples -- typically every free-text
    cell across one row-batch (any number of distinct columns; this function doesn't
    care which row/column a cell belongs to beyond needing the column name for
    per-cell log context, matching scrub_pre's existing per-span logging).

    Returns a list of scrubbed values, same length/order as `items`; a None input
    stays None.

    `pattern_cache`: pass a dict the CALLER keeps alive across calls (e.g. one per
    run_inplace() invocation) so the company_map regex compiles once, not per batch --
    see apply_literal_map's docstring for the performance incident this avoids."""
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
        for s, e, t in ents_by_idx.get(i, []):
            span = stripped[s:e]
            if not _keep_entity(span, t, protect_list[i], domain):
                continue
            lit_map[span.casefold()] = engine.fake(span, t)
        v = apply_literal_map(pre_list[i], lit_map, protect_list[i])
        # scrub_post() (not called here -- see module docstring) normally applies these two
        # right after its own GLiNER-substitution loop; do the same here so a Han-script
        # company/person name GLiNER's Latin-centric model misses, and any literal
        # 'centific'/'pactera' occurrence, still get caught regardless of this path skipping
        # scrub_post() itself.
        v = _han_token_backstop(v, engine)
        v = engine.apply_forced(v)
        if company_map:
            v = apply_literal_map(v, company_map, protect_list[i], pattern_cache=pattern_cache)
        v = _json_safe_fallback(str(text), v, pre_list[i])
        out[i] = v
    return out
