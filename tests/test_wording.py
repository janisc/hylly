"""Pins on words, not tests of what they do: a line of the library page's or the server page's script, or a word of the
help, that only a browser runs or reads, so no test here can drive it. Each says that the words a feature hangs on are
still there; none shows the feature works. A change of the words that keeps the feature fails one of these: change the
pin with it, once you have seen the feature at work in a browser (tests/browser/check.py runs some of the page). A
break that keeps the words passes them, which is why they're gathered here, apart from the tests of behaviour."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "src" / "hylly"


def page(name: str) -> str:
    return (SOURCES / name).read_text(encoding="utf-8")


def test_the_server_page_doesn_t_ask_for_what_nobody_is_looking_at():
    """Its status is asked for only while the page is in view, and it no longer names the Windows script it once did."""
    text = page("server_page.html")
    assert "document.hidden" in text and "scripts\\\\Library.cmd" not in text


def test_the_server_page_says_what_starts_a_server_in_the_system_s_walls_again():
    """It goes by its status's service, which test_other_systems asks a running server for."""
    text = page("server_page.html")
    assert "s.service" in text and "sudo systemctl start hylly" in text


def test_the_server_page_leads_to_the_database_page():
    assert 'href="/database"' in page("server_page.html")


def test_the_page_says_why_and_offers_no_read_for_a_title_that_can_t_be_opened():
    text = page("catalog_page.html")
    assert "canRead(it) ? readRow(it) : cantOpen(it) + listRow(it)" in text
    assert "This book can't be opened." in text and '<li id="cantopen">' in page("help.html")


def test_a_place_counts_the_page_youre_on_and_is_found_under_any_copy():
    text = page("catalog_page.html")
    assert "(n + 1) / of * 100" in text
    keeps = re.findall(r"const (readAt|pagesAt|bookAt) = [^\n]*placeKept\(it, ", text)
    assert keeps == ["readAt", "pagesAt", "bookAt"]
    assert "for (const id of idsOf(it)) { const v = localStorage" in text
    assert "const idsOf = it => [it.id, ...(it.ids || [])];" in text  # its own id, then every copy's
