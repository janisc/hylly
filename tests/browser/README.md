# The browser check

The tests run no browser, so `check.py` here opens the library page and its reader in real ones: in WebKit, the engine
of Safari, and in Chromium, the engine of Chrome and Edge, the browsers Hylly is made for. In each it opens one e-book
and one comic, turns a page, and checks that the text, a picture and the book's font are there. It opens a hostile
e-book too, made to do harm, and checks that its page shows the book's words with none of its traps left in it: no
script, no `onload`, no `javascript:` link, no frame, no picture from another site and no refresh to another address:
that is the reader's cleaner at work. That none of them ran and nothing was fetched from anywhere but the server is
checked too, but that is the page's own content policy, which stops what the cleaner misses: with the cleaner off,
only the counts of what is left fail. It also runs the shelf
rule's cases (`tests/shelf_cases.json`) through the page's own `shelfRule`, which `tests/test_shelf_rule.py` runs
through the server's, so that the page and the server put the same titles on a shelf.

Run it after a change to the library page (`src/hylly/catalog_page.html`) or the reader, and before a release.

It isn't one of the tests (pytest doesn't collect it), and Playwright, which drives the browsers, isn't among Hylly's
requirements: `uv run --with playwright` brings it for the one run. From the checkout:

```
uv run --with playwright playwright install webkit chromium
uv run --with playwright python tests/browser/check.py
```

The first line is needed once: it downloads the two browsers into Playwright's own folder. On Linux they may also need
system libraries: `uv run --with playwright playwright install --with-deps webkit chromium` installs those too,
asking for sudo.

Everything the check makes is in a temporary folder that goes when it ends: a demo library (`hylly demo`) with two more
e-books, one that has a picture in its text (and a copy of it in a second folder, so that one title has two sources)
and the hostile one, its own settings and users, and a server on a free port of this computer. Your own settings,
library and server are never used, nor the network: the browsers' requests for anywhere else are stopped. It prints a line for each check and ends with exit code 0 when all passed, 1 when any failed, and 2
when it couldn't run. Ctrl-C, or a SIGTERM or SIGHUP, ends it early the same way: the server stopped and the folder
removed.

- `--browser webkit` checks in that one only; given again, in more. `chrome` and `msedge` are the Chrome and Edge
  installed on this computer, as Playwright finds them.
- `--python` names the Python that runs Hylly, for a Python with Playwright but without Hylly's requirements. Without
  it, the check uses its own Python, or else the checkout's `.venv` that `uv sync` makes. The code is always this
  checkout's.
- `--keep` keeps the temporary folder, with the server's log, and says where it is.

Playwright's WebKit is not Safari itself: before a release, also open one e-book and one comic by hand on an iPad or an
iPhone.
