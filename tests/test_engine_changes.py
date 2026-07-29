#!/usr/bin/env python3
"""Tests for engine changes #2/#3/#5/#6/#7 (no gliner/torch/DB needed).
Run:  python tests/test_engine_changes.py    # -> "N passed"
Placeholder values only."""
import os, sys
for _p in ('src','.'):
    _d=os.path.join(os.path.dirname(__file__),'..',_p)
    if os.path.exists(os.path.join(_d,'obi_anonymizer.py')): sys.path.insert(0,_d); break
import obi_anonymizer as m   # noqa: E402


def _fe():
    fe = m.FakeEngine.__new__(m.FakeEngine)
    fe._used = set(); fe._cache = {}
    return fe


# ── #3 strict class-preserving id ────────────────────────────────────────────────
def test_id_class_preserving():
    fe = _fe()
    for v in ['P3003490', 'UWHSASLLC21P001', '202112-E00001853', 'a1b2-C3D4']:
        out = fe._gen_guid(v)
        assert len(out) == len(v), f"length must be preserved: {v}->{out}"
        for oc, nc in zip(v, out):
            if oc.isdigit():
                assert nc.isdigit(), f"digit->digit violated: {v}->{out}"
            elif oc.isalpha():
                assert nc.isalpha() and nc.isupper() == oc.isupper(), f"letter/case violated: {v}->{out}"
            else:
                assert nc == oc, f"separator must be kept: {v}->{out}"

def test_id_deterministic():
    assert _fe()._gen_guid('P3003490') == _fe()._gen_guid('P3003490'), "id must be deterministic"

def test_id_letter_never_self_maps():
    # a letter position must never fake back to the SAME letter (case-insensitive) -- otherwise a
    # personnel code like 'P0000004' has a coincidental chance of faking to another 'P#######'
    # value, indistinguishable in shape from some other real id it isn't actually related to.
    import string
    for ch in string.ascii_letters:
        for salt in range(50):
            v = f"{ch}{salt:05d}"
            out = _fe()._gen_guid(v)
            assert out[0].lower() != ch.lower(), f"leading letter self-mapped: {v}->{out}"


# ── #6 AMOUNT_HINT / AMOUNT_SKIP ─────────────────────────────────────────────────
def test_amount_hint():
    money = ['revenue_base', 'TCV', 'TOTALEFFORTINHOURS', 'new_forecastedrevenue',
             'marginamount', 'annual_budget', 'total_spend']
    notmoney = ['msdyn_forecastcategory', 'new_invoicesubmissionpreference',
                'accountid', 'statuscode', 'ratetype', 'version']
    for c in money:
        assert m.AMOUNT_HINT.search(c) and not m.AMOUNT_SKIP.search(c), f"{c} should be money"
    for c in notmoney:
        assert not (m.AMOUNT_HINT.search(c) and not m.AMOUNT_SKIP.search(c)), f"{c} should NOT be money"


# ── #5 country / location generators ─────────────────────────────────────────────
def test_country_form_preserving():
    fe = _fe()
    assert len(fe._gen_country('US')) == 2 and fe._gen_country('US').isupper()
    assert len(fe._gen_country('USA')) == 3
    out = fe._gen_country('United States')
    assert ' ' not in out or out[0].isupper()           # a real title-cased country name
    # never identity
    assert fe._gen_country('CN') != 'CN'
    # deterministic
    assert fe._gen_country('India') == fe._gen_country('India')

def test_location_generator():
    fe = _fe()
    out = fe._gen_location('Bangalore')
    assert out in m.CITIES and out != 'Bangalore'
    assert fe._gen_location('Bangalore') == fe._gen_location('Bangalore')   # consistent

def test_ref_deny_and_hints():
    assert m.REF_DENY.search('stepname') and m.REF_DENY.search('Billing_type_name')
    assert m.REF_DENY.search('ProductLine') and m.REF_DENY.search('ProjectGroup')
    assert m.COUNTRY_HINT.search('address1_country')
    assert m.LOCATION_HINT.search('address1_city') and m.LOCATION_HINT.search('VNSWORKLOCATION'.lower())


# ── #7 harvest stopword filter ───────────────────────────────────────────────────
def test_harvest_stopword_left_unchanged():
    fe = _fe()
    for w in ['email', 'Mobile', 'target', 'GDC', 'contact']:
        assert fe.fake(w, 'org') == w, f"generic token {w!r} must be left unchanged (not persisted)"
    # a real multi-word org is still anonymized (generator path; bypasses xref/DB state)
    assert fe._gen_org('Bounce Marketing LLC') != 'Bounce Marketing LLC'


# ── #2 fit_width word-boundary clamp ─────────────────────────────────────────────
def test_fit_width():
    assert m.fit_width('Bridgeport Systems', 12) == 'Bridgeport'   # cut at space, whole token
    assert m.fit_width('Bridgeport', 4) == 'Brid'                  # first token too long -> hard cut
    assert m.fit_width('short', 50) == 'short'                     # under limit -> unchanged
    assert m.fit_width(None, 10) is None
    assert m.fit_width('x', 0) == 'x'                              # unbounded (0) -> unchanged


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    passed = 0
    for t in tests:
        t(); print(f"  ok  {t.__name__}"); passed += 1
    print(f"{passed} passed")
