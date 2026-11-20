"""What's on a shelf, as the server decides it for a shelf's feed (shelves.rule): the same as the page's shelfRule. The cases
are shared with the check in real browsers, tests/browser/check.py, which runs them through the page's own code, as no
test here runs a browser; and the page's rule is fingerprinted here, so that a change to it can't go by without the
server's following, and that check run again."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hylly import shelves

HERE = Path(__file__).parent
PAGE = HERE.parent / "src" / "hylly" / "catalog_page.html"
CASES = json.loads((HERE / "shelf_cases.json").read_text(encoding="utf-8"))
# The page's code that decides what's on a shelf, line for line: fold and unaccented (rules.words_of), norm, words,
# Missing's names (FACET.miss.labels), SHELF_TEXT, values, missing, shelfRule; and each filter group's values in FACET,
# every line of the table that starts with VALUES, in its order.
PIECES = ["const FOLDED = ", "const onLetter = ", "function foldChar(c)", "const ASCII = ", "const fold = ", "const ACCENT = ",
          "const unaccented = ", "const norm = ", "const words = ", "    labels: { all:", "const SHELF_TEXT = ", "const values = ",
          "function missing(it)", "function shelfRule(shelf)"]
VALUES = "    values: "
FINGERPRINT = "af341e27e8cac239eca50a301d92d1b7cb883c0e92c585f176693553ecde062e"


def page_rule() -> str:
    lines = PAGE.read_text(encoding="utf-8").splitlines()
    out = []
    for start in PIECES:
        i = next(n for n, line in enumerate(lines) if line.startswith(start))
        j = next(n for n in range(i, len(lines)) if lines[n] == "}") if start.startswith("function") else i
        out += lines[i:j + 1]
    table = lines.index("const FACET = {")
    out += [line for line in lines[table:lines.index("};", table)] if line.startswith(VALUES)]
    return "\n".join(out)


def test_the_page_s_shelf_rule_is_the_one_the_server_follows():
    found = hashlib.sha256(page_rule().encode("utf-8")).hexdigest()
    assert found == FINGERPRINT, ("The page's shelf rule changed: change shelves.rule (and shelf_cases.json) to match, "
                                  "run tests/browser/check.py for the cases in the browsers, then the fingerprint "
                                  f"here to {found}")


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: json.dumps(c["shelf"]))
def test_a_shelf_holds_what_the_page_says(case):
    on = shelves.rule(case["shelf"])
    assert [it["id"] for it in CASES["items"] if on(it)] == case["on"]


def test_reading_and_lists_go_by_whoever_it_is_for():
    me = {"reading": {"a": "started", "c": "finished"}, "on_list": {"b"}, "lists_of": {"c": ["l1"]}}
    items = CASES["items"]
    on = lambda shelf, who=me: [it["id"] for it in items if shelves.rule(shelf, who)(it)]  # noqa: E731
    assert on({"show": {"rd": ["started"]}}) == ["a"] and on({"show": {"rd": ["not"]}}) == ["b", "d"]  # no place: not started
    assert on({"show": {"ls": ["on"]}}) == ["b"] and on({"show": {"ls": ["l1"]}}) == ["c"] and on({"hide": {"ls": ["on", "l1"]}}) == ["a", "d"]
    assert on({"show": {"rd": ["started"]}}, None) == []  # nobody's: as the page without a login


def test_the_e_reader_feed_goes_by_the_server_s_own_say():
    items = CASES["items"]
    assert [it["id"] for it in items if shelves.rule({"show": {"kf": ["yes"]}}, None, lambda it: it["id"] == "c")(it)] == ["c"]  # a correction since


def test_a_shelf_ticked_for_the_reading_apps_keeps_the_tick():
    shelf = {"id": "s1", "name": "Comics", "show": {"k": ["comic"]}, "hide": {}}
    assert shelves.clean({**shelf, "apps": True})["apps"] is True
    assert "apps" not in shelves.clean({**shelf, "apps": "yes"}) and "apps" not in shelves.clean(shelf)
