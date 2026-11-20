"""The library page and its reader in real browsers, which no test runs: in each browser engine asked for (WebKit, as
Safari has it, and Chromium, as Chrome and Edge have it), one e-book and one comic opened, a page turned, the text, a
picture and the book's font there; a hostile e-book opened, whose page shows its words with none of its traps left in
it (a script, an onload, a javascript: link, a frame, a picture from another site, a refresh to another address: the
cleaner's work), and none run and nothing fetched from outside (the page's policy's, which stops what the cleaner
misses); and the shelf rule's cases (tests/shelf_cases.json) run through the page's own
shelfRule, which tests/test_shelf_rule.py runs through the server's (shelves.rule), so that the two can't drift apart.
Run it after a change to the library page or the reader, and before a release; tests/browser/README.md says how.

Everything is made in a temporary folder of its own: a demo library (hylly demo), with two more e-books, one that has a
picture in its text (and a copy of it in a second folder of the library's, so that one title has two sources) and the
hostile one, its settings and users, and a server on a free port of this computer, logged in to as a person does it,
with the login page's form. Your own settings, library and server are never used, nor the network: a request for
anywhere but the server is stopped in the browser, and counted. It prints a line for each check and ends with exit
code 0 when all passed, 1 when any failed, and 2 when it couldn't run. tests/test_security.py checks the cleaner by its
words as well, which is all a test without a browser can do.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
CASES = REPO / "tests" / "shelf_cases.json"
BROWSERS = {  # what --browser takes: the name shown, Playwright's engine, and its channel, an installed browser
    "webkit": ("WebKit", "webkit", None),
    "chromium": ("Chromium", "chromium", None),
    "chrome": ("Chrome", "chromium", "chrome"),
    "msedge": ("Edge", "chromium", "msedge"),
}
BOOK = "Pictures and Words.epub"  # the check's own e-book, beside the demo's
HOSTILE = "A Book of Traps.epub"  # and a book made to do harm: what the reader's cleaner (withoutScripts) takes out
OUTSIDE = "http://127.0.0.1:9"  # another site, as far as the page goes: a port of this computer that nothing answers on
TRAP_WORDS = "Plain made-up words stay where the traps were taken out."
COPY = "Extras"  # a folder of the library's own with a copy of it: one title, of two sources, so that srcs is tried
COMIC = "The Clockwork Cat.cbz"  # one of the demo's comics
WORDS = "Plain made-up words fill this page so that it has something to lay out."
# Before the page's own scripts: the reader's first-time guide counts as seen, so that it doesn't stand over the book,
# and the books are read in Literata, one of the fonts the program brings
BEFORE = """try {
  localStorage.setItem("hylly.guide", '["book","pages"]');
  localStorage.setItem("hylly.bookstyle", '{"font":"literata"}');
} catch {}"""

STUCK = 'document.getElementById("rwhat").textContent + " " + document.getElementById("rpct").textContent'
READ_BOOK = """async (file) => {
  const it = CATALOG.items.find(i => (i.f || []).some(([p]) => decodeURIComponent(p).endsWith("/" + file)));
  if (!it) return { listed: false };
  openReader(it);
  let at = null;
  for (let i = 0; i < 60 && at === null; i++) {
    await new Promise(r => setTimeout(r, 500));
    if (document.getElementById("rload").hidden && RD.view && RD.view.lastLocation) at = RD.view.lastLocation.fraction;
  }
  if (at === null) return { opened: false, stuckOn: %s };
  await new Promise(r => setTimeout(r, 1500));
  const doc = RD.view.renderer.getContents()[0].doc, picture = doc.images[0];
  const r = { opened: true, text: doc.body.innerText.includes(%s), picture: !!picture && picture.naturalWidth === 60,
              font: doc.fonts.check("16px Literata") && [...doc.fonts].some(f => f.status === "loaded") };
  await RD.view.next();
  await new Promise(r => setTimeout(r, 800));
  r.turned = RD.view.lastLocation.fraction > at;
  return r;
}""" % (STUCK, json.dumps(WORDS))
READ_COMIC = """async (file) => {
  const it = CATALOG.items.find(i => (i.f || []).some(([p]) => decodeURIComponent(p).endsWith("/" + file)));
  if (!it) return { listed: false };
  openReader(it);
  let opened = false;
  for (let i = 0; i < 60 && !opened; i++) {
    await new Promise(r => setTimeout(r, 500));
    opened = document.getElementById("rload").hidden && !!cur() && cur().naturalWidth > 0;
  }
  if (!opened) return { opened, stuckOn: %s };
  const r = { opened, picture: cur().naturalWidth > 0 }, from = RD.n;
  turn(1);
  for (let i = 0; i < 20 && RD.n === from; i++) await new Promise(r => setTimeout(r, 200));
  await new Promise(r => setTimeout(r, 800));
  r.turned = RD.n === from + 1 && cur().naturalWidth > 0;
  return r;
}""" % STUCK
# The hostile book opened, and its page as the reader shows it looked through: what of its traps is still there (the
# cleaner's work: left), and whether any of them ran (each would mark the page's window, or the book's frame's, which
# the page's own policy stops even where the cleaner missed one). Opened, its words shown and nothing left or run, each
# is true; left has the counts, to say more when one isn't.
READ_HOSTILE = """async (file) => {
  const it = CATALOG.items.find(i => (i.f || []).some(([p]) => decodeURIComponent(p).endsWith("/" + file)));
  if (!it) return { listed: false };
  openReader(it);
  let opened = false;
  for (let i = 0; i < 60 && !opened; i++) {
    await new Promise(r => setTimeout(r, 500));
    opened = document.getElementById("rload").hidden && !!RD.view && !!RD.view.lastLocation;
  }
  if (!opened) return { opened, stuckOn: %s };
  await new Promise(r => setTimeout(r, 2000));  // time for anything left in it to load, run or reach out
  const doc = RD.view.renderer.getContents()[0].doc, all = [...doc.querySelectorAll("*")];
  const left = {
    scripts: doc.querySelectorAll("script, noscript").length,
    handlers: all.filter(el => [...el.attributes].some(a => a.name.toLowerCase().startsWith("on"))).length,
    javascriptLinks: all.filter(el => [...el.attributes].some(a => /^\\s*javascript:/i.test(a.value))).length,
    frames: doc.querySelectorAll("iframe, frame, frameset, object, embed").length,
    outsidePictures: all.filter(el => ["src", "srcset", "poster"].some(a => /^\\s*(https?:)?\\/\\//i.test(el.getAttribute(a) || ""))).length,
    refreshes: [...doc.querySelectorAll("meta")].filter(m => /refresh/i.test(m.getAttribute("http-equiv") || "")).length,
  };
  const ran = [window.HOSTILE, doc.defaultView && doc.defaultView.HOSTILE].filter(Boolean);
  return { opened, text: doc.body.innerText.includes(%s), ...Object.fromEntries(Object.entries(left).map(([k, n]) => [`no ${k}`, !n])),
           nothingRan: !ran.length, left, ran };
}""" % (STUCK, json.dumps(TRAP_WORDS))
# The cases' titles made ready as the page makes the server's, and each case's shelf through the page's own shelfRule.
# lay is the page's own; the sources of a merged title (srcs) the page makes in its loop over the titles, with no
# function of its own (catalog_page.html: "for (const it of ITEMS) { lay(it); it.srcs = [it.src, ...]"), so srcsOf
# here is that line. It is held to what the page made of every title it has, the check's own e-book among them, one
# title of two sources (COPY): asThePage fails without such a title, as then a merged one's sources weren't tried. The
# cases are nobody's, as shelves.rule has them without a person: no reading, no lists. So for this run the page knows
# no one (ME), as it was before it learnt who's logged in.
SHELF_CASES = """(cases) => {
  const srcsOf = it => [it.src, ...(it.cp || []).map(c => c[0])];
  const asThePage = ITEMS.some(it => (it.cp || []).length > 0)
    && ITEMS.every(it => JSON.stringify(it.srcs) === JSON.stringify(srcsOf(it)));
  const me = ME;
  ME = null;
  try {
    const items = cases.items.map(it => {
      const x = JSON.parse(JSON.stringify(it));
      lay(x);
      x.srcs = srcsOf(x);
      return x;
    });
    return { asThePage, on: cases.cases.map(c => items.filter(shelfRule(c.shelf)).map(it => it.id)) };
  } finally {
    ME = me;
  }
}"""


def png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\0" + bytes(rgb) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def make_book(path: Path) -> None:
    """An e-book of one long chapter: a picture of 60 by 40 at its start, and words enough for several pages."""
    xhtml = '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/book.opf" media-type="application/oebps-package+xml"/>'
                   "</rootfiles></container>")
        z.writestr("OEBPS/book.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
                   'unique-identifier="i">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="i">urn:hylly-check:1'
                   "</dc:identifier><dc:title>Pictures and Words</dc:title><dc:creator>Ann Writer</dc:creator>"
                   "<dc:language>en</dc:language></metadata><manifest>"
                   '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                   '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="pic" href="pic.png" media-type="image/png"/></manifest>'
                   '<spine><itemref idref="c1"/></spine></package>')
        z.writestr("OEBPS/nav.xhtml", xhtml + '<head><title>Contents</title></head><body><nav epub:type="toc"><ol>'
                   '<li><a href="c1.xhtml">One</a></li></ol></nav></body></html>')
        z.writestr("OEBPS/c1.xhtml", xhtml + '<head><title>One</title></head><body><h1>One</h1>'
                   f'<p><img src="pic.png" alt="a picture"/></p><p>{" ".join([WORDS] * 200)}</p></body></html>')
        z.writestr("OEBPS/pic.png", png(60, 40, (200, 80, 40)))


def make_hostile_book(path: Path) -> None:
    """An e-book whose one chapter has every kind of trap the reader takes out of a book's pages, each marking the
    page's window (HOSTILE) should it run: a script, an onload on the body and on a picture, a javascript: link, a
    frame and an object, a picture from another site, a refresh to another address. Its words are made up."""
    mark = "top.HOSTILE = (top.HOSTILE || '') + ' {}'"
    xhtml = '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/book.opf" media-type="application/oebps-package+xml"/>'
                   "</rootfiles></container>")
        z.writestr("OEBPS/book.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
                   'unique-identifier="i">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="i">urn:hylly-check:traps'
                   "</dc:identifier><dc:title>A Book of Traps</dc:title><dc:creator>Bea Artist</dc:creator>"
                   "<dc:language>en</dc:language></metadata><manifest>"
                   '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                   '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml" properties="scripted"/>'
                   '<item id="pic" href="pic.png" media-type="image/png"/></manifest>'
                   '<spine><itemref idref="c1"/></spine></package>')
        z.writestr("OEBPS/nav.xhtml", xhtml + '<head><title>Contents</title></head><body><nav epub:type="toc"><ol>'
                   '<li><a href="c1.xhtml">Traps</a></li></ol></nav></body></html>')
        z.writestr("OEBPS/c1.xhtml", xhtml + '<head><title>Traps</title>'
                   f'<meta http-equiv="refresh" content="1; url={OUTSIDE}/refreshed"/>'
                   f'<script>{mark.format("script")}</script>'
                   f'</head><body onload="{mark.format("body onload")}"><h1>Traps</h1><p>{TRAP_WORDS}</p>'
                   f'<p><img src="pic.png" alt="a picture" onload="{mark.format("picture onload")}"/></p>'
                   f'<p><a href="javascript:{mark.format("link")}">a link that would run</a></p>'
                   f'<iframe src="{OUTSIDE}/framed"></iframe><object data="{OUTSIDE}/object"></object>'
                   f'<p><img src="{OUTSIDE}/pixel.png" alt="a picture from another site"/></p>'
                   f'<p>{" ".join([TRAP_WORDS] * 40)}</p></body></html>')
        z.writestr("OEBPS/pic.png", png(60, 40, (40, 80, 200)))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def listening(port: int, server: subprocess.Popen, seconds: float = 60) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and server.poll() is None:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def until(page, expression: str, seconds: float = 30) -> None:
    """Asked from here, again and again: the page's policy forbids the eval that Playwright's own waiting uses."""
    deadline = time.monotonic() + seconds
    while not page.evaluate(expression):
        if time.monotonic() > deadline:
            raise TimeoutError(f"waited {seconds:g} seconds for {expression}")
        page.wait_for_timeout(200)


class Demo:
    """The demo library, its users and its server, in a temporary folder: Hylly run by python, from this checkout."""

    def __init__(self, work: Path):
        self.work, self.python = work, ""
        self.folder, self.password = work / "demo", secrets.token_urlsafe(18)
        home = work / "home"
        home.mkdir()
        # Apart from yours, as hylly demo is: none of your HYLLY_ variables (your settings, rules and server computer,
        # the shops' logins, the alarms' secrets), and a home folder of its own
        self.env = {k: v for k, v in os.environ.items()
                    if not k.upper().startswith("HYLLY_") and k.upper() != "CREDENTIALS_DIRECTORY"}
        self.env.update(HOME=str(home), HYLLY_HOME=str(self.folder / "settings"),
                        HYLLY_LIBRARY=str(self.folder / "library"), HYLLY_NO_NOTIFY="1", PYTHONUTF8="1",
                        PYTHONPATH=os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH")])))
        if os.name == "nt":
            self.env["USERPROFILE"] = str(home)
        self.server: subprocess.Popen | None = None

    def runs_hylly(self, python: str) -> str:
        """Why python can't run Hylly, or "" when it can."""
        try:
            asked = subprocess.run([python, "-c", "import hylly.serve"], cwd=self.work, env=self.env,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace")
        except OSError as e:  # no such program, say
            return e.strerror or str(e)
        if not asked.returncode:
            return ""
        return (asked.stderr.strip().splitlines() or [f"exit code {asked.returncode}"])[-1]

    def hylly(self, *args: str, given: str | None = None) -> None:
        done = subprocess.run([self.python, "-m", "hylly", *args], cwd=self.work, env=self.env, input=given,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if done.returncode:
            said = (done.stdout + done.stderr).strip()[-800:]
            raise RuntimeError(f"hylly {args[0]} ended with exit code {done.returncode}: {said}")

    def make(self, users: list[str]) -> None:
        self.hylly("demo", str(self.folder), "--no-serve")
        for folder in ("ebooks", COPY):  # the same book twice: one title, with a copy in a source of its own
            (self.folder / "library" / folder).mkdir(exist_ok=True)
            make_book(self.folder / "library" / folder / BOOK)
        make_hostile_book(self.folder / "library" / "ebooks" / HOSTILE)
        self.hylly("catalog")
        for name in users:
            self.hylly("user", "add", name, "--role", "reader", "--password-stdin", given=self.password + "\n")

    def serve(self) -> str:
        port = free_port()
        with (self.work / "server.log").open("w", encoding="utf-8") as log:
            self.server = subprocess.Popen([self.python, "-m", "hylly", "serve", "--port", str(port), "--no-browser"],
                                           cwd=self.work, env=self.env, stdout=log, stderr=subprocess.STDOUT)
        if not listening(port, self.server):
            said = (self.work / "server.log").read_text(encoding="utf-8", errors="replace").strip()[-800:]
            raise RuntimeError(f"the demo's server didn't start: {said or 'it said nothing'}")
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        if self.server is not None and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(timeout=20)


def hylly_python(given: str | None, demo: Demo) -> str:
    """The Python that runs Hylly: the one given, else this one, else the checkout's own .venv (uv sync's)."""
    venv = REPO / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    tried = []
    for python in [given] if given else [sys.executable, *([str(venv)] if venv.is_file() else [])]:
        why = demo.runs_hylly(python)
        if not why:
            return python
        tried.append(f"{python} ({why})")
    raise RuntimeError(f"no Python here runs Hylly: {', '.join(tried)}. Run the check with uv run, as "
                       "tests/browser/README.md says, or give a Python that has Hylly's requirements with --python")


def log_in(page, base: str, name: str, password: str) -> None:
    """The login page's own form, as a person fills it in, and on to the library page, once it knows who's there."""
    page.goto(base + "/login?next=/catalog/index.html", wait_until="load", timeout=60000)
    page.fill('input[name="username"]', name)
    page.fill('input[name="password"]', password)
    page.press('input[name="password"]', "Enter")
    page.wait_for_url(lambda url: "/login" not in url, timeout=20000)
    until(page, "!!window.CATALOG && typeof openReader === 'function' && typeof shelfRule === 'function' && !!ME")


def shelf_rule(page) -> dict:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    got = page.evaluate(SHELF_CASES, cases)
    wrong = [{"shelf": c["shelf"], "server": c["on"], "page": g}
             for c, g in zip(cases["cases"], got["on"]) if c["on"] != g]
    return {"cases": len(cases["cases"]), "same": not wrong, "srcsAsThePage": got["asThePage"],
            **({"differ": wrong[:3]} if wrong else {})}


def hostile_book(page) -> dict:
    """The hostile book read, every request for anywhere but the server stopped in the browser and counted. Two layers
    stand between a book and the reader: the cleaner (withoutScripts), which the counts of what is left in the page
    test, and the page's content policy, which stops a script or a fetch the cleaner missed before it runs or leaves
    the browser. The requests counted here and the marks of what ran (ran) test that second layer only: with the
    cleaner switched off they stay at nothing, and it's the counts that fail. What the reader makes of the book in the
    browser itself (blob: and data: addresses, which WebKit hands to a route too) is no request."""
    here, asked = page.url.split("/catalog/")[0], []

    def elsewhere(route) -> None:
        asked.append(route.request.url[:80])
        route.abort()

    page.context.route(lambda url: url.split(":", 1)[0].lower() in ("http", "https", "ws", "wss", "ftp")
                       and not url.startswith(here + "/"), elsewhere)
    result = page.evaluate(READ_HOSTILE, HOSTILE)
    return {**result, "nothingFetchedOutside": not asked, **({"fetched": asked[:3]} if asked else {})}


PROBES = [  # what's checked, and how: each in a browser context of its own, logged in
    ("e-book (text, a picture, Literata, a page turned)", lambda page: page.evaluate(READ_BOOK, BOOK)),
    ("comic (a picture, a page turned)", lambda page: page.evaluate(READ_COMIC, COMIC)),
    ("hostile e-book (its words, and no script, handler, javascript: link, frame, outside picture or refresh left or run)",
     hostile_book),
    ("shelf rule: the page's over the cases the server's is tested with", shelf_rule),
]
SAID = ("stuckOn", "cases", "differ", "left", "ran", "fetched")  # in a result, what says more rather than passes or fails
ENDING = ("SIGINT", "SIGTERM", "SIGHUP")  # Ctrl-C, a kill, a terminal closed: where the system has them


def run(browsers: list[str], demo: Demo) -> int:
    """The checks in each browser, a line each; the number that failed."""
    from playwright.sync_api import sync_playwright

    users = {name: f"check-{name}" for name in browsers}  # one each, so that one's reading places aren't another's
    demo.make(list(users.values()))
    base = demo.serve()
    print(f"A demo library served at {base}/, Hylly from {SRC}", flush=True)
    failed = 0
    with sync_playwright() as p:
        for name in browsers:
            shown, engine, channel = BROWSERS[name]
            try:
                browser = getattr(p, engine).launch(**({"channel": channel} if channel else {}))
            except Exception as e:  # noqa: BLE001 - said, and counted as failed: the next browser may start
                first = (str(e).strip().splitlines() or [type(e).__name__])[0]
                how = f"is {shown} installed?" if channel else f"playwright install {engine} installs it"
                print(f"FAIL {shown}: couldn't start it ({first[:200]}): {how}", flush=True)
                failed += 1
                continue
            print(f"{shown} {browser.version}", flush=True)
            for what, probe in PROBES:
                context = browser.new_context(viewport={"width": 1180, "height": 820}, device_scale_factor=2)
                context.add_init_script(BEFORE)
                page = context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e, errors=errors: errors.append(str(e)[:200]))
                try:
                    log_in(page, base, users[name], demo.password)
                    result = probe(page)
                except Exception as e:  # noqa: BLE001 - a check that couldn't finish is a failed one, said as such
                    result = {"ranThrough": False, "stuckOn": str(e).strip()[:300]}
                good = all(v is True for k, v in result.items() if k not in SAID) and not errors
                failed += not good
                print(f"  {'ok  ' if good else 'FAIL'} {what}: {json.dumps(result, ensure_ascii=False)}", flush=True)
                for line in dict.fromkeys(errors):
                    print(f"       an error in the page: {line}", flush=True)
                context.close()
            browser.close()
    return failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--browser", action="append", choices=list(BROWSERS),
                        help="a browser to check in, again for more: webkit and chromium, Playwright's own (the "
                             "default: both), or chrome and msedge, the ones installed on this computer")
    parser.add_argument("--python", help="the Python that runs Hylly, with its requirements (default: this one, or "
                                         "else the checkout's .venv, as uv sync makes it); the code is this checkout's")
    parser.add_argument("--keep", action="store_true",
                        help="keep the temporary folder, with the server's log, and say where it is")
    args = parser.parse_args()
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        print("This Python has no Playwright: run the check as tests/browser/README.md says, "
              "uv run --with playwright python tests/browser/check.py", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="hylly-browser-check-"))
    demo = Demo(work)
    stopped: list[int] = []
    ending = False  # tidying up at the end, as it does however it ends

    def tidy() -> None:
        demo.stop()
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)

    def stop(signum, frame) -> None:
        """Ctrl-C, SIGTERM or SIGHUP: the demo's server ended and its folder removed at once, whatever the browsers'
        driver is in the middle of (a signal's default would end this process with neither), then the check ends.
        While it tidies up at the end, the signal is only noted, for the exit code: that tidying up goes on."""
        if not stopped:
            stopped.append(signum)
            if not ending:
                tidy()
        if not ending:
            raise KeyboardInterrupt

    for name in ENDING:
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)
    failed = 0
    try:
        demo.python = hylly_python(args.python, demo)
        failed = run(list(dict.fromkeys(args.browser or ["webkit", "chromium"])), demo)
    except RuntimeError as e:
        if not stopped:
            print(f"Couldn't run the check: {e}", file=sys.stderr)
            return 2
    except BaseException:  # stop's, or what Playwright raises when its driver goes with a Ctrl-C too
        if not stopped:
            raise
    finally:
        ending = True
        tidy()  # again after a signal: what was under way when it came may have written since
        if args.keep:
            print(f"Kept: {work}")
    if stopped:
        print(f"Stopped ({signal.Signals(stopped[0]).name}): the demo's server is ended"
              f"{'' if args.keep else ' and its folder removed'}.", file=sys.stderr)
        return 128 + stopped[0]  # as a shell has it: 130 after Ctrl-C, 143 after SIGTERM
    print("All good." if not failed else f"{failed} FAILED.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
