#!/usr/bin/env python3
"""Tests for the GLiNER windowed full-cell scan (fixes the text[:4000] truncation leak).
Uses a tiny STUB model — no gliner / torch / DB / real data required.
Run:  python tests/test_gliner_windows.py    # -> "4 passed"
Placeholder names only.
"""
import os, sys
for _p in ('src','.'):
    _d=os.path.join(os.path.dirname(__file__),'..',_p)
    if os.path.exists(os.path.join(_d,'obi_anonymizer.py')): sys.path.insert(0,_d); break
from obi_anonymizer import GlinerDetector   # noqa: E402

# ── stub GLiNER model ────────────────────────────────────────────────────────────
# Detects fixed placeholder tokens wherever they occur in the *given* (window) string,
# returning window-relative spans — exactly the shape real GLiNER returns.
PLACEHOLDERS = {'Zzyzx Qwerty': 'person', 'Vexil Corp': 'organization'}

class StubModel:
    def predict_entities(self, text, labels, threshold=0.5):
        out = []
        for tok, label in PLACEHOLDERS.items():
            i = text.find(tok)
            while i != -1:
                out.append({'start': i, 'end': i + len(tok), 'label': label})
                i = text.find(tok, i + 1)
        return out
    def batch_predict_entities(self, texts, labels, threshold=0.5):
        return [self.predict_entities(t, labels, threshold) for t in texts]

def _det():
    d = GlinerDetector(batch_size=8)
    d.model = StubModel(); d.tried = True
    return d

def test_windows_cover_full_text():
    d = _det()
    text = 'x' * 10000
    covered = [False] * len(text)
    for off, sub in d._windows(text):
        for i in range(off, off + len(sub)):
            covered[i] = True
    assert all(covered), "windows must cover every character of the cell"

def test_windows_overlap():
    d = _det()
    text = 'y' * 10000
    wins = list(d._windows(text))
    assert len(wins) >= 2
    for (o1, s1), (o2, s2) in zip(wins, wins[1:]):
        # next window starts before the previous one ends -> overlap == WIN_OVERLAP
        assert (o1 + len(s1)) - o2 == GlinerDetector.WIN_OVERLAP, "consecutive windows must overlap by WIN_OVERLAP"

def test_entity_past_4000_single():
    d = _det()
    pos = 5000                                   # well past the old 4000 cut
    text = ('a' * pos) + 'Zzyzx Qwerty' + ('b' * 3000)
    ents = d.entities(text)
    assert (pos, pos + len('Zzyzx Qwerty'), 'person') in ents, \
        "entity past char 4000 must be detected in absolute coords (single path)"

def test_entity_past_4000_batch():
    d = _det()
    pos = 6000
    text = ('a' * pos) + 'Vexil Corp' + ('b' * 5000)
    out = d.entities_batch([text, 'short and clean'])
    assert (pos, pos + len('Vexil Corp'), 'org') in out[0], \
        "entity past char 4000 must be detected in absolute coords (batch path)"
    assert out[1] == [], "clean text yields no entities"

if __name__ == '__main__':
    tests = [test_windows_cover_full_text, test_windows_overlap,
             test_entity_past_4000_single, test_entity_past_4000_batch]
    passed = 0
    for t in tests:
        t(); print(f"  ok  {t.__name__}"); passed += 1
    print(f"{passed} passed")
