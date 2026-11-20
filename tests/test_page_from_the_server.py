"""The library page comes only from the server. A catalog build leaves no copy of the page, its help or tags.js in the
library's catalog folder: one there, opened from the folder, would show old tags and run old code, with no server
behind it. And the README and the help never tell anyone to open such a file from the folder, nor link to one."""

from __future__ import annotations

import re
from pathlib import Path

from hylly import catalog

from helpers import build_library


ROOT = Path(__file__).resolve().parents[1]
SENTENCES = re.compile(r"(?<=[.!?])\s+|\n\s*\n|</?(?:li|p|tr|td)\b[^>]*>")  # a sentence, a list item, a paragraph
OPEN = re.compile(r"\b(?:open|double-click|browse to|go to)\b", re.I)  # an instruction to open something
NOT = re.compile(r"\b(?:don't|do not|never|not)\s+$", re.I)  # unless it says not to
PAGE_FILE = re.compile(r"[^\s`'\"()<>\[\]]*(?:index|help)\.html?\b|[^\s`'\"()<>\[\]]*tags\.js\b")  # one, with its path
ADDRESS = re.compile(r"^/catalog/|^(?!file:)[a-z][a-z0-9+.-]*://|:\d+/", re.I)  # on a server: by its path, host or port
FILE_LINK = re.compile(r"""href\s*=\s*["']?\s*file:|\]\(\s*<?\s*file:""", re.I)  # a link, in HTML or Markdown


def from_the_folder(text: str) -> list[str]:
    """What tells a reader to open a page's file from the folder: a file: link, or a sentence that says to open a page
    file (index.html, help.html, tags.js) by a path in the folder, or by its name alone where the sentence says it's in
    the folder. Not an address on a server (/catalog/index.html, with a host or a port or without), not a mention (an
    old catalog/index.html is removed), and not an open said not to be done, or said of something else first."""
    found = [m.group(0) for m in FILE_LINK.finditer(text)]
    for sentence in SENTENCES.split(text):
        words = re.sub(r"<[^>]*>", "", sentence)  # the words: not the help's own links to its pages on the server
        for path in PAGE_FILE.finditer(words):
            if ADDRESS.search(path.group(0)) or path.group(0).startswith("src/"):
                continue
            if not re.search(r"[\\/]", path.group(0)) and not re.search(r"\bfolder\b", words, re.I):
                continue  # a page's name alone, not said to be the folder's
            before = words[:path.start()]
            verb = ([None] + list(OPEN.finditer(before)))[-1]
            if verb and not re.search(r"[,;]", before[verb.end():]) and not NOT.search(before[:verb.start()]):
                found.append(" ".join(words.split())[:160])
    return found


def test_a_catalog_build_leaves_no_page_help_or_tags_anywhere_in_the_library_s_catalog_folder(tmp_path):
    """The whole build, as hylly catalog runs it, where test_catalog.py has write_page alone: what an earlier version
    left is gone, and nothing the build writes, at any depth, is a page or a script but the titles' data.js."""
    build_library(tmp_path)
    folder = tmp_path / "catalog"
    folder.mkdir()
    for name in ("index.html", "help.html", "tags.js"):  # as an earlier version left them there
        (folder / name).write_text("left by an earlier version", encoding="utf-8")
    catalog.build(tmp_path, log=lambda s: None)
    pages = [p.relative_to(folder).as_posix() for p in folder.rglob("*")
             if p.suffix.lower() in (".html", ".htm", ".js") and p.relative_to(folder).as_posix() != "data.js"]
    assert (folder / "data.js").is_file() and pages == []


def test_the_readme_and_the_help_never_tell_anyone_to_open_the_page_from_the_folder():
    for name in ("README.md", "src/hylly/help.html"):
        assert from_the_folder((ROOT / name).read_text(encoding="utf-8")) == [], name


def test_an_address_or_a_mention_is_no_instruction_to_open_the_page_from_the_folder():
    for said in ("Open http://192.168.1.5:8765/catalog/index.html on the tablet.",
                 "open localhost:8765/catalog/index.html",
                 "Open http://<i>server</i>:8765/catalog/index.html on the tablet.",
                 "Open <code>http://<this computer>:8765/catalog/index.html</code>.",
                 "Open <code>http://&lt;this computer&gt;:8765/catalog/index.html</code>.",
                 "Open `/catalog/help.html` from the library page's Help link.",
                 "Open the help (help.html).",
                 "Don't open catalog\\index.html, open http://localhost:8765/ instead.",
                 "an old catalog/index.html is removed",
                 "See [the guide](https://docs.example.org/guide/index.html).",
                 "Opened from file:///C:/Books/catalog/index.html, the page only says to run the server.",
                 '<a class="back" href="index.html">Back to the library</a>',
                 "[src/hylly/help.html](src/hylly/help.html)"):
        assert from_the_folder(said) == [], said
    for said in ("`uv run hylly catalog`, then open `catalog\\index.html` in the library folder.",  # the README, once
                 "You can also open {{library}}\\catalog\\index.html directly.",  # and the help
                 "<li>Double-click <code>catalog/help.html</code> in the library folder.</li>",
                 "Open index.html in the library's catalog folder.",
                 "Open /srv/books/catalog/index.html in a browser.",
                 "Open file:///C:/Books/catalog/index.html in a browser.",
                 '<a href="file:///C:/Books/catalog/index.html">the page</a>',
                 "[the page](file:///srv/books/catalog/index.html)"):
        assert from_the_folder(said), said
