# foliate-js

The reader's engine for EPUB, MOBI, AZW3 and FB2 books: the files it needs from
[foliate-js](https://github.com/johnfactotum/foliate-js) (MIT licence, see LICENSE), as they came, but for the lines
below, at commit 78914aef4466eb960965702401634c2cb348e9b1 (1 May 2026). The server serves them at `/foliate/`.

Left out on purpose: `comic-book.js`, `pdf.js` and `tts.js`, which `view.js` asks for only for a CBZ comic, a
PDF or reading aloud. The reader never hands foliate a comic or a PDF (the server sends those as pages), and
doesn't read aloud.

## Changed here

- `mobi.js`, in `loadResourceBlob`: an AZW3's style sheet or picture becomes a file of the type the reader gave it,
  `new Blob([newData], { type: newType })`. foliate-js writes `{ newType }`, so each of them had no type at all: an
  SVG picture didn't show, and a browser took the rest for whatever their bytes looked like. The reader cleans every
  part a book hands over and names its type (`scriptless` in `catalog_page.html`); the two go together.
- `paginator.js` and `fixed-layout.js`, where each makes a book's frame: `allow-scripts` in its `sandbox` only where
  `navigator.vendor` is Apple's, that is in WebKit (Safari, and every browser on an iPhone or an iPad), which needs it
  for the events in the frame to reach the reader (the WebKit bug the line above it names). foliate-js gives it to
  every browser. A book's frame is the library's own page (`allow-same-origin`, which the reader needs to reach into
  it), so a script that ran there could act as the library: elsewhere (Chrome, Edge, Firefox), nothing in the frame may
  run at all, whatever a book or a slip in the reader's cleaning lets through, and in WebKit the page's policy still
  stops it.
- `paginator.js`, for a book closed in the middle of a jump: its `destroy` lets go of everything its size watcher
  watches (`this.#observer.disconnect()`; foliate-js lets go of the renderer itself, which it never watched), and the
  two callbacks `setStyles` leaves for later (the background a frame on, the layout once the fonts are there) do
  nothing unless it still has a page with a document: none once it's closed, and none in a page a jump still loading
  made after that, whose frame is gone. A jump still loading its chapter as the book closed left the watcher such a
  page, and laying that out threw in `columnize`; a chapter that came in the frame the book closed in threw reading its
  page's background. Each put an error in the console after the book had closed.

The page asks for these files under the commit's first letters (`FOLIATE` in `catalog_page.html`), and a browser keeps
them for a year by that address. So a change here also takes one more of the commit's letters into the address
(`78914ae` was the files as they came, `78914aef` with `mobi.js`'s line, `78914aef4` with the frames' too,
`78914aef44` with `paginator.js`'s for a closed book), or browsers would go on with the old files.

To update: copy the same files from a newer commit, note it here, make the changes above again unless foliate-js has
made them, and go through *What Hylly uses* below. The address, `FOLIATE` in `catalog_page.html`, changes with the
commit: it starts again with the new commit's first seven letters. `tests/test_vendored.py` holds each file's
fingerprint beside the address, and fails until both are renewed together: its message gives the new lines.

## What Hylly uses

foliate-js promises no stable interface, and the reader (the books part of `catalog_page.html`) reaches well into its
parts. A name renamed there fails quietly: Lift falls back to the slide or turns wrongly, a place isn't kept, a book's
parts go uncleaned. So after an update, check that each of these still is there and does what it says.
`tests/test_vendored.py` finds each name below in foliate's files and in the page (the page's own functions are named
without the code marks); the browser check (`tests/browser/check.py`) opens an e-book and a hostile one in WebKit and
Chromium, but not a big EPUB read a part at a time: open one by hand.

- **The view** (`view.js`, the `foliate-view` element): `open`, given a `File` or the book a big EPUB's loader made;
  `init` with `lastLocation` and `showTextStart`; `close`, `goTo`, `goToFraction`, `getSectionFractions`,
  `resolveNavigation` (its `index`), `getCFI`, `addAnnotation` and `deleteAnnotation` (by `value`), `search` (results
  with `progress`, `label` and `subitems`, each with a `cfi` and an `excerpt` of `pre`, `match` and `post`) and
  `clearSearch`; `isFixedLayout`, `book` and `renderer`. Its events: `load` (`doc`, `index`), `relocate` (`cfi`,
  `fraction`, `location`, `section`, `tocItem`, `time`, `range`), `create-overlay` (`index`), `draw-annotation`
  (`draw`, given `overlayer.js`'s `Overlayer` and its `underline`) and `external-link` (`href_`, cancelled: the reader
  opens such a link itself).
- **The renderer** (`paginator.js`, and `fixed-layout.js`'s `foliate-fxl`): `page`, `pages`, `atStart`, `atEnd`,
  `sideProp` and `bookDir`, which Lift goes by; `getContents` (`doc`, `index`); `setStyles`, which only the paginator
  has; the attributes `flow`, `margin`, `gap`, `max-inline-size`, `max-column-count` and `animated`; and its own
  `relocate`. Its `next`, `prev` and `goTo` are wrapped on the instance, each calling its class's own (the page's
  watchMoves), so that a slow move says Loading and a jump dropped while a page turns (the paginator's `#locked`) goes
  again.
- **The book** (`epub.js`, `mobi.js`, `fb2.js`): `sections` (`id`, `linear`, `load`, `unload`), `toc` (`label`,
  `href`, `subitems`), `landmarks` (`type`, `href`), `metadata` and its `language`, `dir`, `mobi` (an AZW3's or a
  MOBI's, whose `headers` have `kf8` for a book in the newer MOBI format) and `destroy`. And `transformTarget`,
  whose `data` event (`data`, `type`, `name`) and `load` event (`isScript`, `allow`) are where an EPUB's and a KF8
  book's parts are cleaned (the page's scriptless). foliate gives one to those two and none to an FB2 or an older MOBI.
  An EPUB, an AZW3 or a KF8 MOBI whose book has no `transformTarget` isn't shown at all: the reader closes and says
  the book couldn't be opened, and the console says why. A section with no `unload` (an AZW3's, a MOBI's, an FB2's)
  has its `load` replaced, so that the page it makes out of foliate's sight is cleaned too.
- **A big EPUB, read a part at a time** (the page's rangeLoader): `epub.js`'s `EPUB`, made with the page's own loader
  and then `init`, asks it for `entries`, `loadText`, `loadBlob` and `getSize`. That loader reads the file with zip.js
  (`vendor/zip.js`): `configure`, `ZipReader`, a `BlobReader` with a `readUint8Array` and a `size` of its own,
  `TextWriter` and `BlobWriter`, and each entry's `filename`, `offset`, `rawFilename`, `rawExtraField`,
  `compressedSize`, `uncompressedSize` and `getData`, which say where a part's bytes lie.
- **The files asked for by name**: those in `FOLIATE_FILES` in the page, fetched ahead, and `epub.js`, `epubcfi.js`
  (its `collapse` and `compare`, which tell whether a bookmark is on the page shown), `overlayer.js` and
  `vendor/zip.js`, imported where they're needed.
