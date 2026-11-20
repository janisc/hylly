# Online lookups (`hylly lookup`): sources as plugins

Files often lack their details. Most comics have no authors, description or genre inside them at all (few comic files
carry a `ComicInfo.xml`). Lookups ask metadata sources for what is
missing. The catalog shows what they found, with a link to where it came from.

| Source | For | Login | Pace | Notes |
|---|---|---|---|---|
| **Metron** (`metron.py`) | comics | API token (free account) | one request every 3.2 s, ≤ 4,500 a day (the site's headers give the real limit) | open data, CC BY-SA 4.0 |
| **Comic Vine** (`comicvine.py`) | comics Metron didn't find | API key (free account) | one every 3 s, ≤ 190 an hour | non-commercial use only |
| **Open Library** (`openlibrary.py`) | books, tech, RPG | none | one a second, ≤ 3,000 an hour, ≤ 10,000 a day | open data; no published limit |
| **Google Books** (`googlebooks.py`) | what the sources before it left out: book descriptions, comics' collected editions | API key (Google Cloud, free; in a project without billing, restricted to the Books API) | one a second, ≤ 900 a day | says a spent quota with 403, so it reads its own 400s and 403s (`Session.json(own=…)`, which hands it the status and the answer's JSON): `Quota` or `Refused` by the reasons Google gives (`QUOTA`, `REFUSED`), and any other one `Changed`, with its reasons |

## What the core does for every source (`lookups/__init__.py`)

- **Pace and budgets.**
  - Never faster than the source's `interval`.
  - `per_hour` and `per_day` budgets are kept on disk (`state\lookups\lookup-budget-<id>.txt`, one line per request, written before it is sent), so neither stopping and starting again nor a killed run can overspend them, and a request that fails still counts.
  - Each request names Hylly (`USER_AGENT`: its version and project page), with the `contact` under `[lookups]` if one is set.
  - One run at a time on a computer (`runs.py`, as for the shops' downloads): each run counts its requests in memory from the file it read at its start, so two side by side would each spend the whole budget. A second `hylly lookup` is refused and told where the first runs; `hylly catalog --lookup` builds without new lookups; `hylly jobs stop lookup` ends one that has hung.
  - `Retry-After` is honoured, and so are Metron-style `X-RateLimit-{Burst,Sustained}-{Remaining,Reset}` headers.
  - A wait of a minute or more (`LONG_WAIT`: the hour's budget used, or the source asked to slow down) is logged with its end, so a quiet run doesn't look hung. The progress line's time to go is the longer of the speed so far and `Budget.done_by`, which plays the remaining requests through the pace and the hourly budget.
- **Retries:** network errors and 5xx are retried with backoff (up to 6 times).
- **Typed problems:**

  | Problem | Raised for | What happens |
  |---|---|---|
  | `Refused` | 401 or 403, or the source says the key is wrong (Google Books: only for a reason that says so) | Stops this source |
  | `Quota` | The budget is spent, a 429 or a rate-limit header asks for a wait longer than 3 minutes (`MAX_WAIT`), or three 429s come in a row | Stops this source; a later run continues |
  | `Changed` | An answer the plugin can't read, or a redirect that isn't https or leads to this computer, the home network or the tailnet (`fetching`), which isn't followed. A resolver that answers names on the internet with addresses that aren't public (a proxy in fake-IP mode, a cache at home that rewrites names, a cloud's private-endpoint names) makes every source's answer one of these, as the address a name leads to is all Hylly can go by | Recorded as an error; three in a row stop the source ("may need an update") |
  | Anything else from `find()` | A slip in the plugin, or an answer it didn't foresee (a `KeyError`, a `TypeError`) | Counted as `Changed`: recorded as an error that names the exception and the plugin's line (`KeyError at comicvine.py:85`), never its words, which could quote the answer; three in a row stop the source, and the others go on |
  | `Transient` | Network trouble or server errors after all retries | Recorded as an error and asked again next run; three titles in a row stop the source for this run (the site seems down) |
  | A local `OSError` | The cache can't be written, for example a full disk | Stops the whole run, with nothing lost |

  Anything that stops a source or the run is logged to `state\lookup-log.txt` and shown as a notification on this computer (`notify.py`), but for a spent budget, which is expected pacing.
- **The cache:** `state\lookups\lookup-<id>.jsonl` has one line per answer, appended at once. Each line has the key, the catalog ids, `found`, `none` or `error`, the time, and the found fields. A description is cut to 20,000 characters (`MAX_DESCRIPTION`), far more than the page shows.
  - The last line for a key wins, and a line cut short by a crash is ignored.
  - `none` is asked again after 180 days; `error` at the next run.
  - `refused` is an answer a person found to be about another book: a line added to the cache by hand or by a script, with the same key and ids. It stays out and isn't asked again, even with `--again`. A title corrected in the page has a new question, and that is asked. Such lines are still read, but the way to say it now is an edit: `hylly set refused openlibrary --id ID` (or *(not this book)* beside the source in the title's details), or *none of these* to the catalog's question about a near miss (below).
  - **Your word on a source's answer** (`curation.lookup_answers`, read by `Answers` and `run` through `lookups.yours`): a source refused for a title gives it nothing and isn't asked about it again, `--again` or not, and a later source is asked about the title as if the refused one had found nothing, as with a refused line; a near miss you said to use fills the title as a found answer would, while the source's answer for it is that very match (`match_mark`: its page, else its words); another match later is a near miss again. Taken back (`hylly decide NUMBER auto`, `hylly set refused auto`), the source answers and is asked as before.
  - A title edited since (`hylly set title …`) has a new key, so it is asked again.
- **Incremental runs:**
  - Only titles without a standing answer are asked; `--again` asks them all.
  - **A question asks with the title's own details,** never with what a lookup found: each catalog record's `lf` names the fields lookups filled in, and `Query.from_record` leaves them out. Otherwise a found author or ISBN would change the question's key, the title would be asked again, and the new answer, often none, would replace the good one. Your own corrections do count: a title you fixed is asked about as you wrote it.
  - `enabled = false` in `[lookups]` turns lookups off altogether: `lookups.available()` gives no source and says why (`OFF`), and `hylly lookup`, `hylly catalog --lookup` and the Inbox's pass each say it once and ask nothing. What was found before stays in the catalog.
  - The order of asking is `order` in `[lookups]` of the settings (`lookups.in_order()`), and a source left out of it is not asked. As it comes, `order` lists none, and that is every source in the order of `sites.LOOKUPS`: Metron, Comic Vine, Open Library, Google Books. A later source is asked about a title only if the earlier ones' answers, plus what the title already has, still lack one of its `wants`. A run reads an earlier answer as the catalog does (`lookups.usable`, below), so what the catalog leaves out of it counts as lacking: an advert for a description, a comic's answer from long after it was bought, what a match for a book asked by its title alone may borrow from another book. Comic Vine isn't asked about a comic Metron found; Google Books is asked about a book Open Library found without a description, or with one that is an advert. A title an earlier source (one with a key) hasn't answered yet waits, so a fallback's budget isn't spent on titles the first source will find tomorrow.
  - `hylly catalog` only reads the caches (`Answers`), so it stays fast and works offline.
- **Merging into the catalog:** found fields fill only what the files and the shop left empty (authors, description, subjects, and series and number, year, publisher, ISBN). Your own edits win over everything, at view time. Matches scoring under `accept` in `[lookups]` (0.8) are kept as `none` with the near miss, never used by the rules: a near miss at or above `NEAR` (0.6) is kept as one of the catalog's questions, a quiet one shown in the title's details (`Answers.near`, [catalog.md](catalog.md#the-catalogs-questions)), and used if you say so.
  - `Answers.for_query` also leaves out what can't be right, at every build, so a cached answer drops out without a new request. It reads each source's answer with `usable`, as `run` does, so a later source is asked for what is left out and fills it in:
    - a comic's answer dated more than a year after it was bought (`too_late`, from the record's `dt`; a cover date runs ahead of the sale);
    - a description that is an advert or only a link (`plain_description`). An advert (`advert`) has selling words (`SELLING`) with a phone number, or with a link and more: two selling words, or one in the link's own sentence; or it has three phone numbers. A real description can have one selling word and a link a sentence on: a bonus story, and where the code is;
    - for a book asked by its title alone, with neither author nor ISBN (`borrowed`), all of a match but its description and subjects (`TITLE_ALONE`): a source gives the first book of that name it knows, and its authors, publisher, year and ISBN could be another book's. A comic is asked by series and number, and keeps all of its answer; so does a match you said to use, as you found it to be the book. All of a match is used, too, when it is vouched for:
      - the title asked has three words or more, articles (`ARTICLES`: the, a, an) left out, and the match has that very title by `norm`'s words, not only a similar one (`same_long_title`). A subtitle after a colon may differ on either side: the title asked is the one before it (`bare_title`, as the sources ask it), and so is the match's (its `match`); or the two are the same with their subtitles, as Google Books' answers saved with title and subtitle as one text have it. A long title's exact match is seldom another book;
      - or the match's publisher or one of its years is the title's own (`agrees`: one publisher's name the other's first words, or the same year), which a short or common title needs. A self-publishing platform's name (`SELF_PUBLISHED`: CreateSpace, Independently published, Lulu and the like) never vouches, as thousands of books share it. Open Library vouches by the year a work first came out, not by every edition's: a work printed for long has one in almost every year, and a PDF's own year is often the year its file was made.
  - Markdown links and emphasis in descriptions become plain words. `meta.mend` puts right text read in the wrong encoding, in every description and author.
  - Every build cleans every saved description, and anyone can edit the sources' pages, so no pattern that cleans them may backtrack over a long run of one character: 3,200 spaces once took minutes. Whitespace is made single spaces before the patterns with `\s*`, and a repeat stops where the next try would start (`<[^<>]+>`, not `<[^>]+>`). `test_a_long_run_in_a_description_is_cleaned_at_once` times them.
- **Secrets:** keys are never logged, cached or put in an error message; requests are logged by path only. Tests check this.

## Writing a source

A source is two things: its row in `sites.LOOKUPS` (`src/hylly/sites.py`), and a subclass of `lookups.Source` with the same id in its own module, `src/hylly/lookups/<id>.py`. Nothing else lists it: `lookups.sources()` finds each row's module by its id.

The row is what's said of the source by the parts that mustn't load its code (`parts.py`), and the class takes its name, its key and its budgets from it:

```python
"example": Lookup("Example DB", "example.org",           # what people see, and the site it asks
                  key="API key",                          # what the site calls its key; leave it out for none
                  get_key="Sign in at example.org/account, where the page shows your API key, and copy it.",
                  key_at="example.org/account",           # where the key is made and deleted; leave it out for the site
                  per_hour=0, per_day=0,                  # budgets; 0 = none of its own
                  contact=False),                         # True if it asks clients for a way to reach them
```

From the row alone:

- `hylly login <id>` saves its key, as `<id>-key.txt` in the logins folder, or takes it from `HYLLY_<ID>_KEY` (the key's last word names both: an "API token" is `<id>-token.txt`). It asks for the key in the row's words and says `get_key` to the owner (`cli._login_lookup`); `hylly login <id> --forget` deletes it and says to delete it at `key_at` too. `hylly doctor` and `hylly commands` list it with the other logins (`sites.SITES`, `sites.KEYS`).
- The help page's list of where to get each key (`<!--lookups:keys-->`, filled by `helppage.lookup_keys`), and the `hylly login` and `hylly lookup` help texts, name it (`commands.py`).
- `hylly commands` lists its `host` among the sites `hylly lookup`, `hylly catalog --lookup` and the Inbox may talk to (`effects.SOURCES`).
- It is asked in the order of the table when `order` under `[lookups]` lists none, as it comes: put it where its answers should win.

The class:

```python
class Example(Source):
    id = "example"                       # its row's: the cache file and --source name
    kinds = frozenset({"comic"})         # which titles it's asked about
    wants = frozenset({"authors", "description"})  # asked when a title lacks any of these (the default)
    interval = 2.0                       # seconds between requests, at least
    home, terms = "https://example.org", "Data CC BY 4.0: link back."  # for the owner: hylly lookup --dry-run
    verified = ""                        # the date it last worked against the real site (shown with them)

    def check(self, s: Session) -> str:  # cheap, read-only; proves the key works
        ...

    def find(self, s: Session, q: Query) -> dict | None:
        ...  # s.json(url, params): throttled, retried, typed problems; None for 404
        ...  # s.json(url, params, own=frozenset({403})): the statuses it reads itself, as (status, JSON)
        return {"authors": [...], "description": "...", "url": "...", "score": 0.93, "match": "Moonwake #3"}
```

`name`, `per_hour`, `per_day` and `credential` come from the row (`credential` is the id when the row has a key); a test holds every real source to its row, so they are written once.

- **What you get:** `find()` receives a `Query`: id, kind, title, series, number, authors, ISBN, publisher, year, the year it was bought (`bought`), and the fields the title already has. An itch.io title with no author comes with its creator as the author, and still counts as lacking one: asked by title alone, such a title is easily given the author of another book with the same name. If no source answers, the catalog shows the creator as the author; an answer comes before that, and your correction before all.
  - For comics, `comic_name(q.title, q.series, q.number)` gives the series, the number (without the zeros before its whole part: `007` is 7, `0.5` stays 0.5), collected edition or single issue, the subtitle and a year hint.
  - `similar(a, b)` scores names from 0 to 1, by their words in any script: `norm(name)` gives them as `rules.words_of` reads words (small letters without accents, letters and digits of Cyrillic, Greek or Japanese as of Latin), one space apart. The questions' keys (`Source.key`) are made of the same words, so a title in another script is asked about on its own.
- **What you return:** `None` when nothing matches well. Otherwise the fields you're sure of, plus `url` (the source's own page, shown as the link) and a `score`.
  - Be strict: a wrong author is worse than none. A book asked by its title alone takes your match's authors, publisher, year and ISBN only when the match is vouched for (`borrowed`): its title, in `match`, is the title asked, of three words or more (a subtitle after a colon, `title: subtitle`), or its publisher or year agrees with the title's (a self-publishing platform's name never does, `SELF_PUBLISHED`). So give the match's title, `publisher` and `year` where you know them. Titles asked the same way share one answer, so where you know more than one, give them all as `publishers` and `years`, as Open Library does (with the year a work first came out, alone): they only vouch, and fill nothing.
  - Raise `Changed`, with a message that says what was odd, when an answer doesn't have the shape you expect: `texts(d, *keys)` says whether fields are text or nothing, `names(value)` whether a list is one of names, and `whole(value)` gives an id as a whole number for your next question's path, or None. Anything else that escapes `find()` (a `KeyError`, a `ValueError` from `int()`) is counted as `Changed` too, so one odd answer never ends the run; but its message names only the exception and your line. Half a character alone in an answer (JSON's `\ud83d`, an emoji cut through) is mended to U+FFFD before the answer is cached, and an answer that still can't be written as JSON counts as `Changed`. A site's own words go into a message only through `their_words()`: on one line, at most 200 characters, as the message reaches the cache, the log and a notification.
  - A description the site gives as HTML goes through `plain_html()`, as Comic Vine's and Google Books' do: each tag a space, its entities the characters they stand for, a run of spaces one space.
  - A regular expression you run on a site's text, or on a title, follows the rule above for descriptions: no backtracking over a long run.
- **Credentials:**
  - The row's `key` and `get_key`; `hylly login <id>` then saves it and runs `check()`.
  - Put a key that travels in the query string into `params` only, never into a path the core logs. A header the site wants with every request (Metron's `Authorization`) goes in `self.headers`, which `Session` sends.
- **Tests:** `tests/test_lookups.py` shows how, with a fake site behind `httpx.MockTransport` and a fake clock (`Clock`), so a run that would wait hours takes milliseconds. Every source needs at least:
  - a good match, a near miss that is refused, and nothing found
  - the login refused (no secret in the message)
  - the site's own rate limit (429, `Retry-After`, or its headers)
  - an unreadable answer
  - a run done twice: the second asks nothing
- **Documentation:** the module docstring says how the site works and when that was last checked, as for the shop plugins ("How it works (2026-10-31)"). `hylly lookup --dry-run` shows each source's `home`, `terms` and `verified` to the owner, who is the one to keep to the terms, so keep them true.

### Every place a new source goes

The row and the module are the source. The rest is written by hand; after each place, the test that fails until it names the source. The tests themselves no test can check.

1. `src/hylly/sites.py`: its row in `LOOKUPS`, where its answers should win (`test_every_source_is_its_row_and_its_module_and_takes_its_facts_from_the_row`).
2. `src/hylly/lookups/<id>.py`: the class and its docstring (the same test: a row without its module, or a module without its row, fails it).
3. `tests/test_lookups.py`: a fake site and the tests below.
4. `src/hylly/help.html`, section "Looking up details online": a row in the table of sources (in the order of `sites.LOOKUPS`), and its pace in "Slow on purpose"; if `contact` is set, the sentence about `contact`. The list of keys is filled in from the row. A line in `CHANGELOG.md` (places 4 to 7: `test_the_docs_written_by_hand_name_every_source`, but for `CHANGELOG.md`).
5. `README.md`: the sources named in "What it solves", in the picture of "How the parts fit together" and in "Growing it" (the keys), what the source is sent in "What leaves your computer", and its link in "Built on".
6. `THIRD-PARTY-NOTICES.md`: its row among the sources of details, with what its terms ask.
7. This file: its row in the table at the top.
8. A source with a key: its environment variable (`HYLLY_<ID>_KEY`, or `_TOKEN`) in the list near the top of `settings.example.toml` (the same test), and its key file in `examples/hylly-jobs.service` (`BindReadOnlyPaths=`), so the Inbox's lookups behind a walled server can read it (`tests/test_periodic.py`).

## Adding a source with an AI agent

The skill in `.claude/skills/add-lookup-source/SKILL.md` gives an agent the method, the rules and the checklist. In short: desk research first; the user saves the key; one read-only probe, then a handful; the plugin and its tests from what the probes showed; a dry run, a small real run, a second run. It also says when to stop and ask.
