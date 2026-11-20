# Roadmap

Ideas for Hylly after 0.1, in no particular order. None of them is a promise or has a date: they're what may come
next, written down so they aren't forgotten, and each says where in the code it would start. To talk about one,
or to suggest another, open an issue.

## Stream a comic's pages to reading apps (OPDS page streaming)

Comic apps that speak OPDS page streaming (OPDS-PSE) can show a comic a page at a time, straight from the server,
without downloading the whole file first, and can say how far you've read. Hylly's feeds give whole files today.
First: which of the apps people use with Hylly read it, and how each sends its place back. Then the feed's links, the
pages drawn as the reader already draws them, and a place kept as the page's own are.

Where it starts: `serve.py` (the feeds), `reader.py` (pages drawn on the server), the help's *OPDS*.

## Reading progress from KOReader

KOReader can sync where you are in a book with a server of its own kind. If Hylly answered it, a book read on an
e-reader would show in *Continue reading* in the page, and the other way round. Its ids for books and its places in
them differ from the page's, so the first step is to find out whether the two can be matched reliably; until then this
is an idea, not a plan.

Where it starts: `people.py` (places), `serve.py`.

## CB7 and DjVu in the browser's reader

The reader in the page opens e-books, PDFs, and CBZ and CBR comics. CB7 comics and DjVu files are catalogued, and go
to the reading apps, but don't open in the page. Each needs its pages taken out or drawn on the server, as a CBR's
and a PDF's are, with the same limits and checks.

Where it starts: `reader.py`, `pdfworker.py`, the help's *Reading*.

## Text settings that follow you to your other devices

The reader's *Aa* settings (font, size, spacing, margins, page colour) are kept in each browser, while your place,
lists and theme follow your login. A phone and a tablet often want different sizes, so this needs a design first:
which settings follow, and whether per kind of device.

Where it starts: `catalog_page.html` (Aa), `people.py`.

## Correct more of a title's details by hand

A librarian can correct a title's name, authors, series and number, year and publisher. A description, an ISBN, the
source and a genre that's wrong can't be corrected yet. Each needs its field in the corrections (`curation.TEXT` and
the page's editor), its limits, and its undo, as the others have.

Where it starts: `curation.py`, `catalog_page.html` (the title editor), docs/catalog.md (*Your own changes*).

## Look a title's details up from the page

`hylly lookup` asks the sources about many titles at once. In the page's title editor, an owner or librarian could ask
one source about one title: the question shown and editable first, the answers beside each field, ticks for what to
take, and the result kept as an ordinary correction. Readers could also report wrong details, as they report a bad
file now.

Where it starts: `lookups/`, `serve.py`, `catalog_page.html`, docs/lookups.md.

## Remove a title from the library

Two copies that may be one book can already be kept both, one, or neither on the Server page, the files moved into
`_Removed` rather than deleted. Removing any title on purpose, from its details or by a command with a dry run and a
yes, would need an answer to what happens to everything that names it: places, lists, bookmarks, reviews, reports and
tags.

Where it starts: `removed.py`, `cli.py`, `catalog.py`, docs/catalog.md.

## Quiet hours for the alarms

HyllyBell sends alarms and a daily summary to a phone. Quiet hours would hold back all but the alarms that can't wait
(an address locked out, say) until the morning, and send what was held in one note.

Where it starts: `bell.py`, the help's *Alarms*.

## Keep a copy in step with its source

A second run of `hylly copy` copies what's new, and lists a file that has changed since it was copied, which stays as
it was. An option could bring the changed file over too, keeping the old one aside rather than writing over it, and
still never deleting anything.

Where it starts: `hyllycopy.py`.

## One copy engine for hylly add and HyllyCopy

`hylly add` copies a list of books in with an older engine of its own; `hylly copy` and the backup use HyllyCopy's,
which copies to a part in its own folder, checks what it wrote by reading it back, never writes over a file and waits
out a share that goes away. One routine to copy and check one file, taken out of HyllyCopy and used by both, would give
`hylly add` the same care; `hylly add` keeps its list, its MD5 record and its summary.

Where it starts: `hyllycopy.py` (`Copying._copy_file`), `copies.py`.

## A container image

The README mentions a container for later. An image would run `hyllyd` with the walls the systemd service gives it
(`examples/hylly.service`): the library and Hylly's own folders as volumes, the logins and the alarms' secrets as
secret files (`*_FILE`), the health check on `/healthz`, and the periodic jobs inside or on the host's timer.

Where it starts: `examples/hylly.service`, docs/ways-to-run-it.md, docs/architecture.md (*The server's periodic jobs*).

## A guided first setup

Setting Hylly up is a handful of commands today (`hylly init`, `hylly user add`, `hylly catalog`, `hylly serve`),
each proved by a check of `hylly doctor`. A guided setup would ask the few questions once, run the steps in order, and
end with the doctor's word, with checks that need the network (a shop's login, the address from outside) as an option.

Where it starts: docs/playbooks.md (*First setup*), `doctor.py`.

## More shops, in Hylly's own code

Hylly downloads from four shops. Another, a shop that lets you download your books as files, can be added in Hylly's
own code, on the shops' core: its row, a module, its tests, and its words, as docs/downloads.md's *Writing a shop*
says, with a skill for an AI assistant (`.claude/skills/add-shop/SKILL.md`). Before any code: the shop's terms on
automated access, read. Say in an issue which shop you'd like, and whether you can test it with an account of your
own.

Where it starts: docs/downloads.md (*Writing a shop*), `shops.py`, `tests/test_shops.py`.

## The library page in other languages

The page, the reader and the help are in English; the word box already translates a held word into a language of the
reader's choosing. The page's own words in other languages would need its texts gathered in one place, a language
chosen per browser, and the help kept in step. The books' details stay as the shops and sources give them.

Where it starts: `catalog_page.html`, `help.html`.

## What Hylly won't do, for now

Some things have been weighed and left out, to keep Hylly simple and safe for a family's library. An issue for one of
them is closed with a link here, unless it brings something new:
- Password recovery: the owner sets a new password (`hylly user`).
- Settings that take a lot of explaining in the page, or anything extra in the reading screen.
- Page curls, and a free slider for the size of covers.
- A promise of reading offline in the browser: the reading apps take whole files for that.
- A search for words anywhere, saved in a shelf.
- Tag suggestions that change as you type.
- Perfect details for every title: the catalog guesses, and you correct.
- Downloading games from itch.io: Hylly is for books, comics and tabletop.
- Shops loaded from outside Hylly's own code.
