"""
Context-aware, structure-preserving location/address anonymization.

The plain 'location'/'country' ptypes (obi_anonymizer.py's _gen_location/_gen_country) treat a
WHOLE cell as one opaque value -- "San Diego, California" becomes one unrelated fake city name,
losing the state entirely, and a real mapping like San Francisco -> Morbi is never reused when
"San Francisco" appears embedded inside a bigger string like "San Francisco Bay Area". This
module detects individual geo entities (city/state-province/country/street-name/postal-code)
inside a string and replaces ONLY those spans, leaving every separator/suffix/non-entity word
untouched -- see /home/azureuser/.claude/plans/merry-percolating-kahan.md for the full design.

Wired in as a NEW opt-in plan.json type ("location_composite"), routed through the SAME
freetext-batched path bulk_scrub_freetext()/scrub_text() already use for person/org/email/phone
detection -- confirmed necessary (not a geo-only pipeline) because real dyncrm_contact.
address1_composite data includes full signature blocks (names/emails/phones mixed with the
address, e.g. "Heidi Cruz\\nExecutive Assistant to Tuan Tran...heidi.cruz@hp.com\\nT +1 360 975
5712...1115 SE 164th Street, Suite 210\\r\\nVancouver, WA 98683\\r\\nUSA"); a geo-only parser
that preserves "everything else" as-is would leave those sitting in the clear.

detect_geo_entities() is the single entry point new callers need, returning spans in the exact
(start, end, ptype) shape obi_anonymizer.scrub_post() already consumes, so geo spans merge into
the SAME entity list as person/org/email/phone spans and go through the same, already-tested
splice loop -- see merge_entity_spans() for how the two lists are combined before that happens.

Resolution of a recognized span ALWAYS goes through engine.fake() live, never a frozen cache --
_persist() updates engine._xref/_xref_any immediately in-memory, so engine.fake() is always
current within a run; a frozen gazetteer snapshot would miss mappings this same run just
created. The gazetteer built here is for RECOGNITION only (does a span exist, what type is it),
never for resolution (what fake replaces it).

KNOWN LIMITATION, not a bug: mapping_xref enforces ONE row per originalvalue globally
(UX_mapping_xref_originalvalue_upperhash is unique on the text alone, not (text, type) --
confirmed the hard way earlier this session chasing a 23000 constraint-violation loop). So once
ANY row exists for a given real-world text (under ANY type, from ANY earlier run), that row's
existing fake is what every future call for that exact text gets, forever, regardless of which
ptype this module thinks it should be. This is exactly what the user's own requirement #1 asks
for ("if a mapping already exists, reuse it -- never generate new"), so it's treated as correct
here, not worked around. The type-aware generation this module adds (_gen_state_province,
_gen_street_name, the ambiguity-resolution tiers below) only actually shapes the FIRST time a
given real text is ever anonymized -- which given ~1M existing mapping_xref rows accumulated
over this whole project, will often already have happened for common place names.
"""
import re

_ZIP_US_RE = re.compile(r'(?<![\d-])\d{5}-\d{4}(?![\d-])|(?<![\d-])\d{5}(?![\d-])')
_ZIP_UK_RE = re.compile(r'\b[A-Za-z]{1,2}\d[A-Za-z\d]?\s?\d[A-Za-z]{2}\b')
_ZIP_GENERIC6_RE = re.compile(r'(?<![\d-])\d{6}(-\d{3})?(?![\d-])')   # India/China/Brazil-style

GEO_LABELS = ['city', 'country', 'state or province', 'street address']
GEO_LABEL2TYPE = {'city': 'city', 'country': 'country', 'state or province': 'state_province',
                   'street address': 'street_address'}
# Calibrated against live samples during design (see plan doc): city/country score well above
# 0.3-0.5; state-or-province recall is measurably weaker (missed a real Indian state at 0.3 in
# testing) so its bar is lower, matching the SAME per-label-threshold philosophy
# GlinerDetector.LABEL_THRESHOLD already uses for organization/person/email/phone.
GEO_CALL_THRESHOLD = 0.2
GEO_LABEL_THRESHOLD = {'city': 0.3, 'country': 0.3, 'state or province': 0.2, 'street address': 0.35}

STREET_DIRECTION_RE = re.compile(r'^(N|S|E|W|NE|NW|SE|SW)$', re.I)
_HOUSE_NUM_RE = re.compile(r'^\d+(st|nd|rd|th)?$', re.I)

_gaz_cache = None      # {normalized_text: ptype} -- recognition only, see module docstring
_ambiguous_cache = None   # {normalized_text} -- keys seen as BOTH a country and a subdivision
                           # candidate (e.g. 'georgia', 'ar') -- see _resolve_ambiguous_types()


def _load_gazetteer(engine):
    """Build once per process: every existing mapping_xref location/country/region/address
    original (recognition only, ~4,124 rows as of this writing) + pycountry's full country and
    subdivision lists (ALL 249 countries -- broader than the 20-country curated fake-output pool
    in obi_anonymizer.py, since RECOGNITION should catch as much real-world text as possible even
    though the fake POOL stays curated/reviewable). mapping_xref entries win on conflict (they
    reflect this project's own established typing for that exact text); pycountry only fills gaps.

    is_pii='N' rows are EXCLUDED here (confirmed the hard way: 'New York' is only in
    mapping_xref as description='Country' because of a stale, admittedly-unreliable
    dyncrm_leads.address1_country review -- is_pii='N' means that exact classification was
    reviewed as NOT trustworthy, so feeding it into a NEW recognizer as ground truth just
    re-imports the same unreliability into a context it was never reviewed for).

    ALSO builds _ambiguous_cache: text that is a plausible candidate for BOTH 'country' and
    'state_province' from pycountry alone (e.g. 'Georgia' the US state / 'Georgia' the country,
    'AR' Arkansas's code / Argentina's alpha_2) -- _resolve_ambiguous_types() only lets
    column-context tiers override a confident initial type for spans in this set, so an
    unambiguous country name like 'France' is never second-guessed just because it showed up in
    a state-hinted column."""
    global _gaz_cache, _ambiguous_cache
    if _gaz_cache is not None:
        return _gaz_cache
    import obi_anonymizer as oa
    gaz = {}
    try:
        rows = engine.cur.execute(
            "SELECT description, originalvalue FROM o2c.mapping_xref "
            "WHERE description IN ('location','Country','country','Region','Address') "
            "AND (is_pii IS NULL OR is_pii <> 'N')").fetchall()
    except Exception:
        rows = []
    _DESC2TYPE = {'country': 'country', 'Country': 'country', 'location': 'city',
                  'Region': 'state_province', 'Address': 'city'}
    for desc, orig in rows:
        if not orig:
            continue
        gaz[engine.normalize(orig)] = _DESC2TYPE.get(desc, 'city')
    country_keys, state_keys = set(), set()
    if oa.pycountry:
        for c in oa.pycountry.countries:
            for name in filter(None, (getattr(c, 'name', None), getattr(c, 'official_name', None),
                                       getattr(c, 'alpha_2', None), getattr(c, 'alpha_3', None))):
                nk = engine.normalize(oa._strip_diacritics(name))
                gaz.setdefault(nk, 'country')
                country_keys.add(nk)
            try:
                subs = list(oa.pycountry.subdivisions.get(country_code=c.alpha_2))
            except Exception:
                subs = []
            for sub in subs:
                code_part = sub.code.split('-', 1)[-1]
                for name in filter(None, (sub.name, code_part if len(code_part) >= 2 else None)):
                    nk = engine.normalize(oa._strip_diacritics(name))
                    gaz.setdefault(nk, 'state_province')
                    state_keys.add(nk)
    _gaz_cache = gaz
    _ambiguous_cache = country_keys & state_keys
    return gaz


def _gaz_lookup(gaz, span):
    return gaz.get(_norm_plain(span))


def _norm_plain(s):
    """Cheap normalize for gazetteer keys when we don't have an engine handy mid-scan (matches
    FakeEngine.normalize()'s casefold+whitespace-collapse; diacritic-stripping doesn't matter
    here since gazetteer keys were already built through the full normalize() at load time --
    only plain ASCII input text needs to match, which strip-then-casefold handles either way)."""
    return re.sub(r'\s+', ' ', s or '').strip().casefold()


def _find_zip_spans(text):
    out = []
    for m in _ZIP_US_RE.finditer(text):
        out.append((m.start(), m.end(), 'postal_code'))
    for m in _ZIP_UK_RE.finditer(text):
        out.append((m.start(), m.end(), 'postal_code'))
    for m in _ZIP_GENERIC6_RE.finditer(text):
        out.append((m.start(), m.end(), 'postal_code'))
    return out


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*|\d+[A-Za-z]?")


_ZIP_AFTER_RE = re.compile(r'^\s?\d{4,6}\b')
_COMMA_BEFORE_RE = re.compile(r',\s?$')


def _short_code_context_ok(text, span_start, span_end, n_tokens):
    """A short (<=3 char) gazetteer candidate is coincidentally very likely to match SOME
    obscure pycountry subdivision code purely by chance -- confirmed the hard way scanning a
    real signature block: ordinary English words/fragments ('to', 'and', 'GM', 'SE', the 'th'
    left over from '164th') all matched real subdivision codes somewhere in the world and got
    faked as if they were place names, while the genuine street address sitting right there
    went undetected. A short match is only trusted in one of FOUR address-shaped contexts:
    (1) it's the ENTIRE alpha content of the cell (n_tokens==1, e.g. a bare 'FL' in
    address1_stateorprovince), (2) it's ALONE on its own line, nothing else besides whitespace
    (e.g. a standalone 'USA' line in a multi-line signature block -- confirmed the hard way this
    is NOT covered by n_tokens==1 alone, since the surrounding lines have plenty of other alpha
    tokens even though THIS line doesn't), (3) it's immediately preceded by ', '
    ('Vancouver, WA'), or (4) it's immediately followed by a 4-6 digit run ('WA 98683') --
    anywhere else in ordinary prose, it's rejected. This is deliberately conservative: a
    rejected short code just falls through to the (much more context-aware) GLiNER geo-pass
    instead of leaking through unflagged -- it's a precision guard on ONE detection layer, not
    the last line of defense."""
    if n_tokens == 1:
        return True
    line_start = text.rfind('\n', 0, span_start) + 1
    line_end = text.find('\n', span_end)
    if line_end == -1:
        line_end = len(text)
    if text[line_start:line_end].strip(' \t\r') == text[span_start:span_end]:
        return True
    if _COMMA_BEFORE_RE.search(text[max(0, span_start - 3):span_start]):
        return True
    if _ZIP_AFTER_RE.match(text[span_end:span_end + 8]):
        return True
    return False


def _find_gazetteer_spans(text, gaz):
    """Longest-match-first scan of `text` against the gazetteer, word-run by word-run (a run of
    consecutive alpha tokens, e.g. 'New York' or 'United States', tried as one span before
    falling back to single words) -- deliberately simple/cheap, not a full trie, since gazetteer
    hits are exact-string lookups on a handful of candidate substrings per cell, not a scan over
    the whole ~50K-entry gazetteer per character. Short (<=3 char) single-token candidates are
    additionally gated by _short_code_context_ok() -- see its docstring."""
    tokens = list(re.finditer(r"[A-Za-z][A-Za-z'.\-]*", text))
    out = []
    i = 0
    n = len(tokens)
    while i < n:
        matched = False
        for j in range(min(i + 4, n), i, -1):     # try up to 4-word runs, longest first
            span_start, span_end = tokens[i].start(), tokens[j - 1].end()
            candidate = text[span_start:span_end]
            if j - i == 1 and len(candidate) <= 3 and not _short_code_context_ok(text, span_start, span_end, n):
                continue
            ptype = gaz.get(_norm_plain(candidate))
            if ptype:
                out.append((span_start, span_end, ptype))
                i = j
                matched = True
                break
        if not matched:
            i += 1
    return out


def _geo_pass(text, gl):
    """GLiNER geo-detection fallback -- a SEPARATE, standalone call using GEO_LABELS/
    GEO_LABEL_THRESHOLD, never touching GlinerDetector.LABELS/LABEL_THRESHOLD (shared by every
    existing freetext column project-wide). Reuses GlinerDetector._windows() for safety on a
    rare long cell, but calls gl.model directly since the wrapper's entities()/entities_batch()
    are hardwired to the class-level LABELS."""
    if gl is None or getattr(gl, 'model', None) is None or not text:
        return []
    import obi_anonymizer as oa
    out, seen = [], set()
    try:
        for off, sub in gl._windows(text):
            preds = gl.model.predict_entities(sub, GEO_LABELS, threshold=GEO_CALL_THRESHOLD)
            for e in preds:
                if e.get('score', 1.0) < GEO_LABEL_THRESHOLD.get(e['label'], 0.3):
                    continue
                a = (off + e['start'], off + e['end'], GEO_LABEL2TYPE.get(e['label'], 'city'))
                if a not in seen:
                    seen.add(a); out.append(a)
    except Exception:
        return []
    return out


_SOURCE_PRIORITY = {'zip': 0, 'gaz': 1, 'gliner': 2}


def merge_entity_spans(*span_groups, text=None):
    """Combine multiple (start,end,ptype) lists (each already tagged with a source-priority via
    the caller's ordering: pass HIGHEST-priority group first) into one clean, non-overlapping
    list. Overlap is resolved by group order first, then longer-span-wins -- NOT scrub_post's own
    'whichever sorts first by start position' skip, which can't arbitrate between multiple
    detectors that disagree about the same characters (confirmed a real case during design: a
    gazetteer city match and a GLiNER state guess can both fire on the identical substring).

    When `text` is given, a LOWER-priority candidate that only PARTIALLY overlaps an
    already-kept higher-priority span is TRIMMED to its non-overlapping remainder (edge
    whitespace/comma stripped) and kept under its own original type, rather than being dropped
    entirely -- confirmed the hard way this matters: for 'Punta Gorda, FL', the gazetteer's
    higher-priority 'FL' (13-15) used to cause GLiNER's whole-string 'Punta Gorda, FL' (0-15)
    'city' guess to be discarded outright, silently losing 'Punta Gorda' (a real, sensitive city
    name) with nothing left to replace it. Without `text`, falls back to the old drop-entirely
    behavior (used by callers that don't have a single flat string, if any)."""
    tagged = []
    for priority, group in enumerate(span_groups):
        for s, e, t in group:
            tagged.append((priority, -(e - s), s, e, t))   # priority asc, span-length desc
    tagged.sort()
    kept = []
    occupied = []   # list of (start, end) already claimed, kept sorted by start for the overlap check
    for _, _, s, e, t in tagged:
        overlaps = [(os, oe) for (os, oe) in occupied if s < oe and os < e]
        if not overlaps:
            kept.append((s, e, t))
            occupied.append((s, e))
            continue
        if text is None:
            continue   # old behavior: drop entirely when we can't safely trim
        for piece_s, piece_e in _subtract_intervals(s, e, overlaps):
            piece = text[piece_s:piece_e]
            trimmed = piece.strip(' ,.\t')
            if len(trimmed) < 2:
                continue
            off = piece.index(trimmed)
            ns, ne = piece_s + off, piece_s + off + len(trimmed)
            kept.append((ns, ne, t))
            occupied.append((ns, ne))
    kept.sort()
    return kept


def _subtract_intervals(s, e, overlaps):
    """[s,e) minus every (os,oe) in `overlaps` -> list of remaining (start,end) sub-intervals,
    in order. Small helper for merge_entity_spans' trimming path."""
    pieces = [(s, e)]
    for os, oe in overlaps:
        new_pieces = []
        for ps, pe in pieces:
            if os >= pe or oe <= ps:
                new_pieces.append((ps, pe))
                continue
            if ps < os:
                new_pieces.append((ps, os))
            if oe < pe:
                new_pieces.append((oe, pe))
        pieces = new_pieces
    return pieces


def _resolve_ambiguous_types(text, spans, engine, column=None, country_hint_col_value=None):
    """Ambiguity tiers for a span whose text is GENUINELY ambiguous between 'country' and
    'state_province' per pycountry (e.g. 'Georgia'/'AR' -- see _load_gazetteer's
    _ambiguous_cache). An UNAMBIGUOUS country name ('France') is never touched here regardless
    of column context -- only spans in _ambiguous_cache are eligible for any tier below.
    Reassigns `ptype` in place (returns a new list); does NOT decide the fake VALUE, only which
    ptype engine.fake() gets called with for that span.

    Tier 1 -- in-text sibling context: every worked example in the spec follows a "smaller unit,
    then bigger unit" ordering ('Georgia United States', 'Bangalore Karnataka India'). A span
    immediately before (adjacent, or separated only by whitespace/comma) a confidently-typed
    'country' span in the SAME text is treated as 'state_province'.
    Tier 2 -- row-level sibling column: if the caller passed a resolved country value for this
    row (column's own plan.json "country_column" companion, mirroring the existing mechanism
    'region' ptype already uses), bias a standalone ambiguous span toward 'state_province'.
    Tier 3 -- column-name hint: obi_anonymizer.STATE_HINT matching the column name.
    Tier 4 -- default: normalize to 'country' (confirmed policy: a bare ambiguous name with zero
    context defaults to country) -- applies even if the detector's own first guess happened to
    be 'state_province', so the OUTPUT is consistent regardless of which detector fired first."""
    import obi_anonymizer as oa
    gaz = _load_gazetteer(engine)
    out = list(spans)
    for idx, (s, e, t) in enumerate(out):
        if t not in ('state_province', 'country'):
            continue
        if _norm_plain(text[s:e]) not in (_ambiguous_cache or ()):
            continue   # unambiguous (e.g. 'France') -- never overridden by column context
        # tier 1: does a country-typed span sit immediately after this one?
        has_adjacent_country = any(
            t2 == 'country' and s2 >= e and s2 - e <= 3
            and not text[e:s2].strip(' ,\t').strip().isalnum()
            for (s2, e2, t2) in out if (s2, e2) != (s, e)
        )
        if has_adjacent_country:
            out[idx] = (s, e, 'state_province')
            continue
        # tier 2: sibling country_column value known for this row
        if country_hint_col_value:
            out[idx] = (s, e, 'state_province')
            continue
        # tier 3: column-name hint
        if column and oa.STATE_HINT.search(column):
            out[idx] = (s, e, 'state_province')
            continue
        # tier 4: default -- normalize to country regardless of the detector's initial guess
        out[idx] = (s, e, 'country')
    return out


def _split_street_address(span_text, engine):
    """Tokenize a GLiNER 'street address' span into house number (verbatim) + directional
    prefix (verbatim) + street-name word(s) (faked via engine.fake(..., 'street_name')) + a
    STREET_SUFFIX allow-list word (verbatim). Falls back to faking the WHOLE span as a 'city'
    (today's safe whole-value behavior) if it can't confidently segment -- never a confident but
    wrong parse."""
    import obi_anonymizer as oa
    toks = re.findall(r"\S+|\s+", span_text)
    if not toks or not _HOUSE_NUM_RE.match(toks[0].strip()):
        return engine.fake(span_text, 'city')
    out = [toks[0]]
    i = 1
    if i < len(toks) and toks[i].isspace():
        out.append(toks[i]); i += 1
    if i < len(toks) and STREET_DIRECTION_RE.match(toks[i].strip()):
        out.append(toks[i]); i += 1
        if i < len(toks) and toks[i].isspace():
            out.append(toks[i]); i += 1
    name_start = i
    suffix_idx = None
    for k in range(len(toks) - 1, i - 1, -1):
        # strip trailing punctuation (e.g. the comma in 'Street,') for the MATCH only -- the
        # token itself, comma included, still goes out verbatim via toks[name_end:] below.
        w = toks[k].strip().strip(',.;:').casefold()
        if w and w in oa.STREET_SUFFIX:
            suffix_idx = k
            break
    if suffix_idx is None or suffix_idx <= name_start:
        return engine.fake(span_text, 'city')
    # trailing whitespace token(s) between the name and the suffix (e.g. the ' ' in
    # 'Deer Creek Rd') belong to the SEPARATOR, not the name -- must be excluded from the
    # name slice and preserved verbatim afterward, or the space is silently lost (confirmed the
    # hard way: 'Deer Creek Rd' -> 'AshgroveRd', missing space, when the name slice's join
    # swallowed the separator instead of keeping it).
    name_end = suffix_idx
    while name_end > name_start and toks[name_end - 1].isspace():
        name_end -= 1
    name_part = ''.join(toks[name_start:name_end]).strip()
    if not name_part:
        return engine.fake(span_text, 'city')
    faked_name = engine.fake(name_part, 'street_name')
    out.append(faked_name)
    out.extend(toks[name_end:])   # preserved separator(s) + suffix + anything after, verbatim
    return ''.join(out)


def _digit_shuffle(text):
    """Deterministic, format-preserving digit substitution for postal codes -- same idiom as
    _gen_phone (digit->digit, everything else kept verbatim). NOT persisted to mapping_xref
    (same reasoning obi_anonymizer._persist() already documents for id/url: high-cardinality,
    purely mechanical, deterministic -- storing every zip code would bloat the shared map for
    no reuse benefit)."""
    import obi_anonymizer as oa
    seed = oa.FakeEngine._seed('zip:' + text)
    out = []
    for i, ch in enumerate(text):
        if ch.isdigit():
            out.append(str((int(ch) + 1 + (seed >> (i % 32)) % 8) % 10))
        else:
            out.append(ch)
    return ''.join(out)


def detect_geo_entities(text, engine, gl=None, column=None, country_hint_col_value=None):
    """Single entry point: detect geo entities in `text`, resolve ambiguity, and return a
    resolved (start, end, ptype) list -- 'city'/'country'/'state_province' spans are ready to
    feed straight into engine.fake(); 'street_address' spans still need _split_street_address()
    (called by the integration layer, not here, since that one mutates the reconstructed text
    rather than just tagging a span); 'postal_code' spans need _digit_shuffle(), not
    engine.fake(). See obi_anonymizer.scrub_post()'s per-ptype dispatch for how these meet up."""
    import obi_anonymizer as oa
    if not text:
        return []
    gaz = _load_gazetteer(engine)
    zip_spans = _find_zip_spans(text)
    gaz_spans = _find_gazetteer_spans(text, gaz)
    gliner_spans = _geo_pass(text, gl)
    merged = merge_entity_spans(zip_spans, gaz_spans, gliner_spans, text=text)
    # defense-in-depth, applied regardless of WHICH layer produced the span (gazetteer OR
    # GLiNER -- confirmed both can false-positive on a short fragment: GLiNER itself tagged the
    # bare 'SE' in '1115 SE 164th Street' as Sweden's country code with enough confidence to
    # survive its own threshold, a real-data case the gazetteer-only guard below doesn't cover
    # on its own). Two independent checks:
    #  (a) a STREET_SUFFIX word (Rd/St/Dr/Fl/...) embedded in a larger string is never a real
    #      place -- see _find_gazetteer_spans' docstring for the confirmed Luxembourg/Syria 'RD'
    #      and US/'Floor' 'FL' collisions in both directions.
    #  (b) ANY short (<=3 char) country/state_province span needs the SAME address-shaped
    #      context _short_code_context_ok() already requires for gazetteer matches -- a
    #      standalone short code anywhere in ordinary prose is coincidence far more often than
    #      it's a real, sensitive place reference.
    n_tokens = len(re.findall(r"[A-Za-z][A-Za-z'.\-]*", text))
    kept = []
    for (s, e, t) in merged:
        span = text[s:e].strip()
        # both checks below need the SAME address-shaped-context test (comma-before/zip-after/
        # own-line/bare-whole-cell) -- confirmed the hard way using two DIFFERENT, narrower
        # tests here caused 'FL' in 'Punta Gorda, FL' to be wrongly rejected: this block's
        # earlier version only allowed a STREET_SUFFIX word through when it was the ENTIRE text,
        # missing the comma-before case _short_code_context_ok already handles correctly.
        if t in ('city', 'country', 'state_province') and (
                span.casefold() in oa.STREET_SUFFIX or len(span) <= 3):
            if not _short_code_context_ok(text, s, e, n_tokens):
                continue
        kept.append((s, e, t))
    return _resolve_ambiguous_types(text, kept, engine, column=column,
                                     country_hint_col_value=country_hint_col_value)
