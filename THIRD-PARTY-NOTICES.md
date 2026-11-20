# Third-party notices

Hylly is licensed under the GNU Affero General Public License, version 3 or later (see `LICENSE`). It includes, or
runs with, the following work by others under their own licences, and shows data from online sources under their
terms.

## Included in this repository

| Component | Where | Licence | Copyright |
|---|---|---|---|
| [foliate-js](https://github.com/johnfactotum/foliate-js): the in-page reader's engine for EPUB, MOBI, AZW3 and FB2 | `src/hylly/foliate/` | MIT (`src/hylly/foliate/LICENSE`) | John Factotum |
| [zip.js](https://github.com/gildas-lormeau/zip.js), bundled with foliate-js | `src/hylly/foliate/vendor/zip.js` | BSD-3-Clause (`src/hylly/foliate/vendor/LICENSE-zip.js.txt`) | Gildas Lormeau |
| [fflate](https://github.com/101arrowz/fflate), bundled with foliate-js | `src/hylly/foliate/vendor/fflate.js` | MIT (`src/hylly/foliate/vendor/LICENSE-fflate.txt`) | Arjun Barrett |
| [Literata](https://github.com/googlefonts/literata) typeface | `src/hylly/fonts/` | SIL Open Font License 1.1 (`src/hylly/fonts/OFL-Literata.txt`) | The Literata Project Authors |
| [Atkinson Hyperlegible](https://www.brailleinstitute.org/freefont/) typeface | `src/hylly/fonts/` | SIL Open Font License 1.1 (`src/hylly/fonts/OFL-AtkinsonHyperlegible.txt`) | Braille Institute of America |
| [Lucide](https://github.com/lucide-icons/lucide) icons, a few changed: the library page's tag, history, bookmark, book, pencil, log-out and hand, and other icons it shares with Feather | `src/hylly/catalog_page.html` | ISC, with MIT for the icons Lucide takes from Feather (`src/hylly/LICENSE-lucide.txt`) | Lucide Icons and Contributors; Cole Bemis |
| [Feather](https://github.com/feathericons/feather) icons: the library page's wrench, flag, speech bubble, star and list | `src/hylly/catalog_page.html` | MIT (`src/hylly/LICENSE-feather.txt`) | Cole Bemis |

The vendored files in `src/hylly/foliate/` and `src/hylly/fonts/` are kept as they came, but for three lines of
foliate-js's, one each in `mobi.js`, `paginator.js` and `fixed-layout.js`; `src/hylly/foliate/README.md` says which commit
they come from, and which lines. The icons are written into Hylly's own files. A running Hylly
lists these in its help's *Licences and source*, and serves each licence's text at `/licences/` and its path under
`src/hylly/`, such as `/licences/foliate/LICENSE`.

## Python packages Hylly depends on

Installed from PyPI with Hylly, not part of this repository:

| Package | Licence |
|---|---|
| PyMuPDF (with MuPDF, Artifex) | GNU AGPL-3.0, or Artifex's commercial licence |
| Pillow | MIT-CMU (HPND) |
| httpx, httpcore, idna, psutil | BSD-3-Clause |
| anyio, h11 | MIT |
| certifi | MPL-2.0 |
| typing-extensions | PSF-2.0 |
| rarfile | ISC |

PyMuPDF's licence is why Hylly is AGPL: a program that uses it and is offered over a network must offer its source.

## Programs Hylly can call

- **7-Zip** (Igor Pavlov), installed separately, opens CBR comics (RAR archives). It is GNU LGPL-2.1 or later,
  with the unRAR licence's restriction for its RAR code (RAR files may be unpacked, but the code may not be used to
  make RAR files). Hylly doesn't ship it. Because of that restriction some systems' packages leave the RAR code out,
  or put it in a package of its own (Debian's and Ubuntu's `7zip-rar`); the README says which 7-Zip has it.

## Data from online sources

Hylly ships no data from these sources. Each library asks them for its own, with its owner's own keys where a source
needs one, and keeps the answers in the library (`state\lookups\`). The library page credits them: *Details from*
names where a title's looked-up details came from, with a link to that source's page for the title. The feeds for
reading apps do the same: such a title's summary ends with *Details from* and the sources' names, and in the feed for
tablets the title links to each source's page.

| Source | Used for | What its terms ask |
|---|---|---|
| [Metron](https://metron.cloud) | comics' details | CC BY-SA 4.0: credit Metron, with a link |
| [Comic Vine](https://comicvine.gamespot.com) | comics' details | non-commercial use only, with a link to Comic Vine |
| [Open Library](https://openlibrary.org) | books' details | open data, no login |
| [Google Books](https://books.google.com) | books' details, and comics that may be collected editions | Google's API terms, with a link to Google Books |
| [Wiktionary](https://www.wiktionary.org) and [Wikipedia](https://www.wikipedia.org) | word meanings in the reader | text under CC BY-SA |

The word meanings aren't kept: the reader's browser asks for them as you read, and the box names Wiktionary or
Wikipedia and their licence, CC BY-SA, with a link to the licence and one to the whole entry.

## Names

Hylly is an independent, unofficial project. It is not affiliated with, endorsed by or sponsored by any of the shops,
services, apps or devices it mentions. Their names and trademarks belong to their owners and are used here only to
say what Hylly works with.
