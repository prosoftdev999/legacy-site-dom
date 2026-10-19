"""Checks /app/output/page_dom.json against the trees Chromium built.

The answer is only parsed as JSON. Each page's tree is flattened into four sequences of node
facts (elements, attributes, text, comments and doctypes), each fact carrying its depth.
On both sides adjacent text is joined first and then text made only of ASCII whitespace is
dropped; content is read only on template elements and frame only on iframes with a srcdoc
attribute, and other keys are ignored. A test fails if any page differs in its sequence; the message counts the differing
facts.
"""
import gzip
import json
import os
from difflib import SequenceMatcher

import pytest

ANSWER = "/app/output/page_dom.json"
EXPECTED = os.path.join(os.path.dirname(__file__), "expected.json.gz")
MAX_BYTES = 400 * 1024 * 1024
WS = " \t\n\f\r"


def _norm(nodes):
    out = []
    for n in nodes if isinstance(nodes, list) else []:
        if isinstance(n, str) and out and isinstance(out[-1], str):
            out[-1] += n
        else:
            out.append(n)
    return [n for n in out if not (isinstance(n, str) and all(c in WS for c in n))]


def _flat(nodes, depth=0, parent="#document", acc=None):
    if acc is None:
        acc = {"elements": [], "attributes": [], "text": [], "markup": []}
    for n in _norm(nodes):
        if isinstance(n, str):
            acc["text"].append((depth, parent, n))
        elif isinstance(n, dict) and isinstance(n.get("tag"), str):
            tag = n["tag"]
            acc["elements"].append((depth, tag))
            attrs = n.get("attrs")
            if not isinstance(attrs, dict):
                attrs = {"#invalid": repr(attrs)}
            acc["attributes"].append((depth, tag, json.dumps(sorted(attrs.items()), ensure_ascii=False)))
            _flat(n.get("children", []), depth + 1, tag, acc)
            shadow = n.get("shadow")
            if shadow is not None:
                mode = shadow.get("mode") if isinstance(shadow, dict) else None
                acc["elements"].append((depth + 1, "#shadow-root", repr(mode)))
                _flat(shadow.get("children", []) if isinstance(shadow, dict) else [], depth + 2, "#shadow-root", acc)
            # content and frame are read only where the instruction defines them; other keys are ignored
            if tag == "template" and "content" in n:
                acc["elements"].append((depth + 1, "#template-content"))
                _flat(n["content"], depth + 2, "#template-content", acc)
            if tag == "iframe" and "srcdoc" in attrs and "frame" in n:
                acc["elements"].append((depth + 1, "#frame-document"))
                _flat(n["frame"], depth + 2, "#frame-document", acc)
        elif isinstance(n, dict) and "comment" in n:
            acc["markup"].append((depth, "comment", repr(n["comment"])))
        elif isinstance(n, dict) and "doctype" in n:
            acc["markup"].append((depth, "doctype", repr(n.get("doctype")), repr(n.get("public")), repr(n.get("system"))))
        else:
            acc["elements"].append((depth, "#unrecognised", repr(n)[:80]))
    return acc


def _wrong(a, b):
    if a == b:
        return 0
    sm = SequenceMatcher(None, a, b, autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal")


@pytest.fixture(scope="module")
def expected():
    with gzip.open(EXPECTED, "rt", encoding="utf-8") as f:
        return json.load(f)["pages"]


@pytest.fixture(scope="module")
def answer():
    assert os.path.isfile(ANSWER) and not os.path.islink(ANSWER), "missing " + ANSWER
    assert os.path.getsize(ANSWER) <= MAX_BYTES, "answer file is too large"
    with open(ANSWER, "rb") as f:
        data = json.loads(f.read().decode("utf-8"))
    assert isinstance(data, dict) and isinstance(data.get("pages"), dict), "top level must be {\"pages\": {...}}"
    return data["pages"]


@pytest.fixture(scope="module")
def flats(expected, answer):
    return {uri: (_flat(exp), _flat(answer.get(uri, []))) for uri, exp in expected.items()}


def _check(flats, kind):
    bad = {}
    for uri, (fe, fg) in flats.items():
        w = _wrong(fe[kind], fg[kind])
        if w:
            bad[uri] = w
    if bad:
        worst = sorted(bad.items(), key=lambda kv: -kv[1])[:5]
        pytest.fail("%d %s facts wrong on %d of %d pages; e.g. %s"
                    % (sum(bad.values()), kind, len(bad), len(flats), worst))


def test_every_page_present(expected, answer):
    missing = [u for u in expected if u not in answer]
    not_lists = [u for u in expected if u in answer and not isinstance(answer[u], list)]
    assert not missing, "%d pages missing, e.g. %s" % (len(missing), missing[:3])
    assert not not_lists, "%d pages are not node lists, e.g. %s" % (len(not_lists), not_lists[:3])


def test_elements(flats):
    _check(flats, "elements")


def test_attributes(flats):
    _check(flats, "attributes")


def test_text(flats):
    _check(flats, "text")


def test_comments_and_doctypes(flats):
    _check(flats, "markup")
