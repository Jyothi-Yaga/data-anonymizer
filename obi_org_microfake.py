"""
Engine-generic, per-alpha-run consistency fix for structured 'org' fakes on composite/coded
values -- e.g. PurchaseOrderLine.DEFAULTLEDGERDIMENSIONDISPLAYVALUE's ledger dimension codes
like '-UWHSASLLC26P010-5060000872-LLM_Collection_New-LLM_Collection-LLM-COST--SBU1---'.

Two problems confirmed via dry-run review of PurchaseOrderLine (neither specific to that table
-- both live inside obi_anonymizer.py's FakeEngine._gen_org, used by every 'org'-typed
structured column project-wide):

  1. A short code token that ALREADY has an established mapping_xref entry elsewhere (e.g.
     'UWHS' -> 'WPST') is never reused when it's a PREFIX of a longer, never-before-seen alpha
     run ('UWHSASLLC') -- _gen_org() only ever checks reuse for the WHOLE run as one opaque
     unit, so the whole 9-letter run gets a brand-new, unrelated fake instead of reusing the
     established mapping for its known first 4 letters.
  2. The same literal word repeated multiple times within ONE value (e.g. 'LLM' 3x in one
     DEFAULTLEDGERDIMENSIONDISPLAYVALUE row: 'LLM_Collection_New-LLM_Collection-LLM') gets a
     DIFFERENT fake each occurrence, because _gen_org()'s per-segment seed is salted by the
     segment's POSITION in the string (f"{sd}:{idx}:{norm}"), not by the segment's own text.

Deliberately a SEPARATE module rather than a change to _gen_org() itself: _gen_org() is used by
dozens of already-verified-clean 'org' columns across the whole project (DELIVERYADDRESSNAME,
DEFINITIONGROUP, LINEDESCRIPTION, dyncrm_* company columns, ...) and changing its core algorithm
risks regressing all of them with no way to scope the change to just the columns that need it.
This module is opt-in per column: a plan.json column entry sets "type": "org_code" (instead of
plain "org") wherever the composite/coded-value pattern is confirmed present; obi_anonymizer.py's
run()/run_inplace() route that type here instead of through the plain _gen_org() path.

Determinism, not persistence, is what keeps sub-word fakes consistent across calls: the same
literal word always hashes to the same fake via engine._seed(), so 'Collection' gets the same
fake wherever/whenever it's re-encountered, with no new mapping_xref rows required for it.
Only the "already-known short code" reuse (problem #1) reads real historical mapping_xref data
-- and only a narrow, pre-vetted slice of it (see _load_known_codes' filter), never mutating it.
"""
import re

_ALPHA_RUN_RE = re.compile(r'([^\W\d_]+)', re.UNICODE)

_known_codes = None   # {UPPERCASE_TOKEN: fake_string}, lazy-loaded once per process; None until loaded


def _load_known_codes(engine):
    """One-time read of a narrow, safe slice of mapping_xref: 4-8 char, PURELY alphabetic,
    ALL-UPPERCASE originals with an UNAMBIGUOUS single fake on file, excluding the 'Country'/
    'location'/'Region' geographic types (a country/region fake is a different semantic
    category -- confirmed 'US' -> 'IN' on file under 'Country', which is correct for an actual
    country column but wrong to splice into an unrelated embedded code match).

    Even after that, confirmed by direct testing that 3-letter tokens are NOT safe: real,
    legitimately-anonymized short company abbreviations on file ('SIT' -> 'ZPH', 'MGM' ->
    'FakeCompany_01056') coincidentally prefix ordinary English words ('Site', 'Mgmt'), which
    would otherwise get their first 3 letters silently spliced with an unrelated company's fake.
    Length >=4 (matching the shortest real motivating example, 'UWHS') cuts this risk sharply
    without eliminating the feature -- 4+ character coincidental prefix collisions are far rarer.
    micro_org_fake() additionally only consults this dict for alpha runs that are THEMSELVES
    all-uppercase (seg.isupper()) -- 'Site'/'Mgmt' are Title-case and so are never even
    candidates, regardless of what's in this dict; this is what actually closes the false-match
    class demonstrated above, the length bump is a second, independent layer.

    ACROSS ALL remaining types on purpose, not just 'CompanyName' -- confirmed the real
    'UWHS'->'WPST' entry is stored under type 'Other' (came from an id/code column, not a
    company-name column), and the whole point is that a short internal code token should mean
    the same fake wherever it recurs, regardless of which column it first appeared in."""
    global _known_codes
    if _known_codes is not None:
        return _known_codes
    # NOTE: deliberately reuses the CALLER's own cursor (engine.cur) rather than importing
    # obi_anonymizer and calling its connect_map() -- when this module is imported from a
    # script invoked as `python obi_anonymizer.py ...`, that script runs as __main__, so
    # `import obi_anonymizer` here loads a SEPARATE, freshly-initialized module object (its
    # own copy of CS/MAP_CS at import-time defaults, never touched by the CLI's own
    # _apply_version() call on __main__'s globals) -- confirmed the hard way: it tried to
    # connect with the hardcoded default 'ODBC Driver 18' string instead of the real
    # V2_DATA_CS from .env. engine.cur already points at the right database (mapping_xref and
    # the data tables are the SAME Azure DB under version 2 -- the only version this 'org_code'
    # type is meant for, since mapping_xref itself is a v2-only table).
    cur = engine.cur
    rows = cur.execute(
        "SELECT originalvalue, anonymizedvalue FROM o2c.mapping_xref "
        "WHERE originalvalue COLLATE Latin1_General_CS_AS = UPPER(originalvalue) COLLATE Latin1_General_CS_AS "
        "AND originalvalue NOT LIKE '%[^A-Za-z]%' AND LEN(originalvalue) BETWEEN 4 AND 8 "
        "AND description NOT IN ('Country', 'location', 'Region')").fetchall()
    m, conflicted = {}, set()
    for orig, fake in rows:
        key = orig.upper()
        if key in m and m[key] != fake:
            conflicted.add(key)
        m[key] = fake
    for key in conflicted:            # ambiguous historical mapping -- skip rather than guess
        del m[key]
    _known_codes = m
    return _known_codes


def _find_known_prefix(run_upper, known_codes):
    """Longest known-code match that is a PREFIX of run_upper (min length 4, matching
    _load_known_codes' own minimum -- see its docstring for why shorter is unsafe). None if no
    known code prefixes this run."""
    top = min(len(run_upper), 8)
    for L in range(top, 3, -1):
        cand = run_upper[:L]
        if cand in known_codes:
            return cand
    return None


def _seg_fake(oa, engine, seg):
    """One alpha-run segment's fake -- same rules _gen_org() uses per-segment inside a
    multi-segment value (head-word only, no descriptor tail; short all-caps -> same-length
    acronym), but keyed by the segment's OWN normalized text rather than its position, so the
    same real word always gets the same fake wherever it recurs (fixes problem #2)."""
    wk = seg.lower()
    if wk in oa.FORCED_MAP:
        return oa.case_like(seg, oa.FORCED_MAP[wk])
    if wk in oa.LEGAL_SUFFIX or wk in oa.FILLER:
        return seg
    wseed = engine._seed('microorg:' + engine.normalize(seg))
    if seg.isupper() and len(seg) <= 4:
        return ''.join(chr(65 + (engine._seed(f"{wseed}:{k}") % 26)) for k in range(len(seg)))
    return oa.case_like(seg, engine._org_head(wseed))


def micro_org_fake(original, engine):
    """Drop-in replacement for engine.fake(original, 'org') on composite/coded structured
    values. Checks for an existing WHOLE-VALUE mapping_xref entry FIRST, same as fake() itself
    -- an already-correct historical mapping for this exact composite string is reused as-is,
    never regenerated (idempotent across repeated runs; never touches/second-guesses a mapping
    that's already on file). Only a genuinely NEW composite value reaches the per-segment
    decomposition below. Falls back to plain engine.fake() for a single-word value (unaffected
    -- that already gets the nicer head+tail treatment via the existing path)."""
    import obi_anonymizer as oa
    xref_id, existing = engine.xref_lookup_with_id(original, 'org')
    if existing is not None:
        engine._used.add(existing.strip().casefold())
        engine._log_outcome(original, existing, f'REUSE:{xref_id}' if xref_id else 'GENERATED-NOT-STORED')
        return existing

    s = original.strip()
    if oa.chinese_anon.is_han(s):
        return engine.fake(original, 'org')            # Han path unaffected, unrelated logic
    parts = list(_ALPHA_RUN_RE.finditer(s))
    if len(parts) <= 1:
        return engine.fake(original, 'org')             # single-word value -- unchanged behavior

    known_codes = _load_known_codes(engine)
    out, prev = [], 0
    for m in parts:
        out.append(s[prev:m.start()])
        seg = m.group(0)
        # only an ALREADY all-caps run is even a candidate for known-code reuse -- e.g.
        # 'UWHSASLLC' qualifies, but an ordinary Title-case word like 'Site' or 'Mgmt' never
        # does, regardless of what's in known_codes (see _load_known_codes' docstring for the
        # real false-match this closes: 'Site'/'Mgmt' coincidentally prefix real short company
        # abbreviations 'SIT'/'MGM' on file).
        known = known_codes.get(seg) if seg.isupper() else None
        if known is not None:
            out.append(oa.case_like(seg, known))
        else:
            prefix = _find_known_prefix(seg, known_codes) if seg.isupper() else None
            if prefix:
                out.append(oa.case_like(seg[:len(prefix)], known_codes[prefix]))
                remainder = seg[len(prefix):]
                if remainder:
                    out.append(_seg_fake(oa, engine, remainder))
            else:
                out.append(_seg_fake(oa, engine, seg))
        prev = m.end()
    out.append(s[prev:])
    joined = ''.join(out)
    norm = engine.normalize(s)
    result = engine._uniq(joined, 'microorg:' + norm, lambda sd: joined)
    engine._persist(original, result, 'org')
    return result
