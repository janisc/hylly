# Contributing to Hylly

Thank you for looking. This page is for anyone reading or changing Hylly's code: how to try it, where to start
reading, and the tests that fail on purpose, with what to do then. An AI assistant starts at [AGENTS.md](AGENTS.md).

## What is taken now

Hylly 0.1 takes issues: a defect, with what you ran, `hylly --version`, and what it wrote on standard error, or an idea.
It doesn't take pull requests yet, nor translations: every word Hylly shows is English, written where it's used.
Before you paste `hylly doctor` or a log into an issue, take out your folders, names and titles.

## Trying it

- A checkout of the source: `git clone https://github.com/janisc/hylly.git`, then `uv sync --python 3.14` in the
  folder it makes, with the libraries `uv.lock` names; `uv run hylly ...` there runs the checkout's own, as every
  command below does. uv brings Python with it; Hylly runs on Python 3.11 and newer, and `--python 3.14` asks for a
  current one, as uv would otherwise take an older one it finds, which `hylly doctor` then calls old. To have the
  checkout's `hylly` on the PATH, in any folder, as the README's install has the published one:
  `uv tool install --force --python 3.14 --editable . --overrides tool-overrides.txt --constraints tool-constraints.txt`
  there, and again after an update of the checkout, for the library versions it was tried with and any program it adds
  or takes away. The Windows launchers in `scripts/` start and stop the checkout's server with a double-click.
- `uv run hylly demo` makes a small library of made-up books, apart from your own, and serves it until Ctrl-C.
  `uv run hylly demo --background` serves it on its own and ends, and `uv run hylly demo --stop` stops that server and
  no other. Every run says what to set for other commands on it, and what to leave unset.
- With `HYLLY_HOME` set to a folder of its own, any Hylly is apart from yours, with every kind of its own files in
  that folder (`paths.PLACES`: settings, database, logs and locks, cache, logins), as long as `HYLLY_SETTINGS`,
  `HYLLY_RULES` and `HYLLY_REMOTE` are unset: they would lead it to your own settings, rules or server computer.

## Where to start reading

1. [docs/architecture.md](docs/architecture.md) and `src/hylly/parts.py`: the parts, and which may use which.
2. What everything stands on, in `src/hylly`: `paths.py` (where things are, Hylly's own folders by kind among them), `storage.py` (Hylly's own files, written so
   that a write cut short costs as little as possible), `exits.py` (how a command ends, and its exit codes), `db.py`
   (the database, and [docs/database.md](docs/database.md) on why it's so) and `rules.py` (what Hylly knows about
   names, from `src/hylly/data`).
3. `commands.py`, every command and option, with `effects.py`, what each may do; and `main` and `_main` in `cli.py`:
   how a command runs, and how it ends.
4. `serve.py`, the library server: `make_server`, and `Handler.GET` and `Handler.POST`, every address with who may
   ask and what answers, run by one dispatcher (`Handler._route`); the catalog it serves, your changes laid
   over it, in `library.py` (`Catalog`), and its feeds for reading apps in `opds.py` (`answer`, and `PAGES`, one
   function for each kind of page).
5. `catalog.py`: `build`, which makes the catalog.

Then the part you're after: [downloads](docs/downloads.md) (and writing a shop), [lookups](docs/lookups.md) (and
writing a lookup source), [catalog and server](docs/catalog.md), [rules](docs/rules.md) (and
[adding one](docs/rules.md#adding-a-rule)), [stars and reviews](docs/reviews.md). [The glossary](docs/glossary.md) has
one name for each thing, in the code and the docs alike.

## Tests

- Run one file at a time while you work, such as `uv run python -m pytest tests/test_rules.py -q`: most modules have a
  file of their own name (`tests/test_catalog.py` for `catalog.py`). Run `tests/test_parts.py` with every change: it
  reads every import, and the library server may never reach the shops' logins or the code that talks to the shops.
  The whole suite takes several minutes: run it once at the end. `uv run pytest -m "not slow"` is a quicker whole run,
  without the tests marked `@pytest.mark.slow` (about three seconds or more each: mark a new one so); CI runs them
  all, on Linux, Windows and macOS.
- The lint rules are `[tool.ruff]`'s in `pyproject.toml`: `uv run ruff check .` checks them, as CI does before the
  tests. No formatter is run over the code.
- No test touches your settings, your server or the network: `tests/conftest.py` gives each one a settings folder and a
  home of its own, and refuses every host but this computer. Keep it so: for a change of your own in a test, use
  `monkeypatch.context()`, never `monkeypatch.undo()`, which takes away conftest's too.
- No test runs a browser: after a change to the library page or the reader, run the browser check too, `uv run --with
  playwright python tests/browser/check.py`, as [tests/browser/README.md](tests/browser/README.md) says.
- A test's name is a sentence that says what holds, such as `test_a_dry_run_changes_nothing`. Made-up books, the
  made-up library (`build_library`, `make_epub`, `png`) and the made-up shops are in `tests/helpers.py`; the fixtures
  any file may use are in `tests/conftest.py`: `library`, the made-up library with its catalog, a copy of the one built
  once for the run, and `serving` and `server` for the library server. A test file never imports another: what two
  files share goes there.
- Many tests read a message's words, as the words are part of what a command does: change the test with the words.

### Tests that fail on purpose

| The test | fails when | Then |
|---|---|---|
| `tests/test_rules.py`: the shipped rules are as reviewed | a value in `src/hylly/data/*.toml` changed | review the change, write `tests/shipped_rules.json` and `tests/shipped_word_answers.json` again, and commit them with it: `uv run python -m pytest tests/test_rules.py -k as_reviewed --new-snapshot` writes both |
| `tests/test_rules.py`: the shipped series and junk words answer as reviewed | a `[series]` or `[junk]` word in `src/hylly/data/*.toml` was added, or finds otherwise than before, in made-up titles | review the titles its message names, write the two files again with the command above, and commit them with the change |
| `tests/test_wording.py` | the words a feature of the page hangs on (a line of its script, a word of the help) changed | see the feature at work in a browser (`tests/browser/check.py` runs some of it), then change the pin |
| `tests/test_rules.py`: the default settings are as reviewed | a default setting changed in one place | change it in all four: `settings.DEFAULTS`, the e-reader feed's in `library.py` (`Device`), `settings.example.toml` and the Settings of [rules.md](docs/rules.md), and in the test's own list |
| `tests/test_shelf_rule.py` | the library page's shelf rule changed | change `shelves.rule` and `tests/shelf_cases.json` to match, run `tests/browser/check.py` for the cases in the browsers, then the fingerprint to the one the message gives |
| `tests/test_routes.py`: every address answers each caller as pinned | an answer of the server's changed: its status, a header that matters or its body, for any caller (`tests/routes.json`) | if the change is meant, review what the message lists, write `tests/routes.json` again and commit it with the change: `uv run python -m pytest tests/test_routes.py --new-snapshot`; a new address is a row of `Handler.GET` or `Handler.POST`, which the walk asks as it is (give it its queries in the walk's `QUERIES` if it needs them); one the code compares a path with otherwise goes into the walk's `asks` (its other tests say which aren't asked) |
| `tests/test_parts.py` | a module is in no part, an import crosses a wall, or the parts' table is out of date | put the module in a part in `parts.py`, move a shared piece down into a part both may use, and the table in [architecture.md](docs/architecture.md) |
| `tests/test_vendored.py` | a file of the reader's engine in `src/hylly/foliate/` changed | put it back as it came; for a change that is meant, tell of it in `src/hylly/foliate/README.md`, take one more of the commit's letters into the address there and in `FOLIATE` (`src/hylly/catalog_page.html`), and renew the test's table with the lines its message gives |
| `tests/test_docs_commands.py`, `tests/test_docs_paths.py` | a doc names a command, an option or a file that has gone | put the doc right |
| `tests/test_reviews.py`: the docs draw the table | the reviews' states changed | draw it again: `reviews.diagram()` into [reviews.md](docs/reviews.md) |
| `tests/test_updates.py`: the tool's overrides and constraints | `pyproject.toml`'s overrides, or `uv.lock`, changed | say the same in `tool-overrides.txt`, and write `tool-constraints.txt` again as its first lines say |

## The code's style

- The file's own: `from __future__ import annotations`, type hints, docstrings and comments in plain English that say
  why, and lines to 120 characters. No formatter is run over the code: change the lines your change needs. The lint
  rules are `[tool.ruff]`'s in `pyproject.toml`; [Adding to it](docs/architecture.md#adding-to-it) has more, and
  [What stays put](docs/architecture.md#what-stays-put) what others depend on.
- A command that can't go on ends with `exits.Exit(code, message)`, the message saying what is wrong and what to do.
- The words: a shop, never a store; this computer, in what people read; made-up names only (Ann Writer, Lantern Wolf,
  Moonwake Press), never a real title, person or host; never a hint at removing DRM.
- A change people see goes into the help page (`src/hylly/help.html`), README.md, `settings.example.toml` and
  `docs/*.md` wherever they say it, and gets a line in [CHANGELOG.md](CHANGELOG.md), under the heading at its top for
  the version not yet released; when the top one is a release already, a new heading goes above it first:
  `## Unreleased`.
