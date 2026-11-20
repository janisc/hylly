# Rules: what Hylly knows about books, and how to change it

Hylly guesses a lot from names: a title and its author from a file name, a series and its number, whether a
bundle holds comics or RPG books, a genre from a title's words, a game from an RPG book's name, which files of a
title are books. What it goes by isn't written into the program. It comes from **rules files** in TOML, the same
language as `settings.toml`. Hylly ships one set, and you can change any part of it with files of your own.

Your own choices (which format to keep for comics, what makes a title a tablet's, how big the reader's page cache is)
are **settings** in `settings.toml`, next to the others. See [Settings](#settings) below and `settings.example.toml`.
The kinds a title may be, and where each is best read, are rules: [The kinds](#the-kinds) below.

## The shipped files

They are in the program, in `src/hylly/data/`:

| File | What it holds |
|---|---|
| `names.toml` | `[filename]`: tokens and patterns taken out of file names, a list's number before a title (none as it comes: see the examples below), dots as spaces. `[junk]`: titles and authors in a file's own details that mean nothing (`Untitled`, `Microsoft Word - …`, `Administrator`). `[names]`: the small words a person's name in a file's name may have (`name_particles`: de, van, von; its other words start with a capital letter, of any script that has them: Sävel, Éloïse and Zdzisław are names; where both sides of "Title - Author" could be names, those in the letters A to Z alone win, as they did before), those a surname starts with when the library page sorts by author (`particles`), words that aren't names, suffixes and company words that keep "Brennan, Jo Jr." as it is, what separates authors. |
| `series.toml` | `[series]`: the words before a number (vol, book, issue, part), the same in brackets, what marks one part of several, the words a series name leaves out when it is compared ("the complete"), and for comics' lookups what marks a collected edition. |
| `kinds.toml` | `[kinds.book]`, `[kinds.tech]`, `[kinds.rpg]`, `[kinds.comic]`, `[kinds.manual]`: the kinds a title may be, one table each, and these are all of them ([The kinds](#the-kinds)): its name on the library page, where it is best read, the words in a bundle's name and the publishers that make a shop's title that kind, at every shop, and `[kinds]` `prose_max_mb`. `[labels]`: what marks a shop's file as an extra, audio or video. `[languages]`: the words that mark a translation. |
| `genres.toml` | `[genres."Science Fiction"]` and so on: the words in a title's subjects, bundle, folder or title that give it a genre. A comment says why for any word that is tricky. `[genres]` `titles_unread`: the sources whose titles aren't read for those words, by name (a shop's folder, or one of yours): none as shipped, as genres go by whole words ("witch" isn't found in switch, nor "craft" in aircraft); `titles_unread_add = ["humble"]` leaves Humble's out, say. `[kind_genres]`: the genre every title of a kind gets, `[kind_genres.comic]` `genre` and so on, before its others; not one that has a genre `unless` names (case not counting: a comic that is Manga isn't Comics & Graphic Novels too), and with `only = true` that genre alone (manuals are Game Manuals). |
| `games.toml` | `[games."Call of Cthulhu"]` and so on: the names of widely known RPG games, for the Game filter; add any other in a file of your own. `ordinary = true` for a name that is an ordinary word too. The first game in the list that a name finds is the one shown; a game you add comes after the shipped ones. |
| `formats.toml` | `[formats.epub]` and so on: one table per file format, by its extension, saying what it is (`layout`: reflowable, fixed or comic), whether a shop's file of it is a book (`shop`), whether it is a book in your own folders (`book`), and whether `hylly set format` can choose it (`keep`). `[packages]`: the books looked for in a ZIP package, and the files a ZIP of pictures may hold besides its pages. `[media]`: audio, video and programs (scripts and shortcuts among them). `[itchio]` `fetch`: the kinds of file `hylly itchio download` takes from an item; any other is left out, and each run lists it. A program never comes, whatever the list says, and Windows' programs, scripts and shortcuts don't whatever any rules file says. |

## Your own files

Put `.toml` files in the `rules` folder next to `settings.toml`, in the settings folder (`hylly settings places`
says where it is: `%APPDATA%\Hylly\rules\` on Windows, `~/.config/hylly/rules/` on Linux,
`~/Library/Application Support/Hylly/rules/` on macOS, or in the one folder of an install that keeps everything there,
`%LOCALAPPDATA%\Hylly\rules\` or `~/.local/share/Hylly/rules/`). Any name ending in `.toml` works. They are read in
name order, after the shipped files, and any of them can hold any of the tables above. Save them as UTF-8 (a BOM at
the start, as Notepad may write, is fine). `hylly rules` names what isn't read: a file whose name ends otherwise
(Notepad's `mine.toml.txt`), a folder inside the rules folder (it isn't looked in), a `.toml` file beside
`settings.toml` rather than in the rules folder, and a table of rules written into `settings.toml`, which reads none
(the settings say so too, as they are read). Somewhere else:

- `rules = 'D:\somewhere'` at the top of `settings.toml`, above the first `[section]`: a folder, or one file, read
  instead of the rules folder next to `settings.toml`. Under a section, as a line added at the end of the file falls,
  it is left out, and said. A folder every computer reads, such as `curation\rules\` in the library, makes them all
  build the same catalog. (Not the `curation` folder itself: its `tags.toml` holds your tags' words, which aren't
  rules.)
- `HYLLY_RULES` in the environment does the same, and wins over `rules =`.
- `--rules FILE_OR_FOLDER` adds one more, read last, so what it says wins; it can be given more than once. The
  commands that read the rules take it, and no others: `hylly catalog --rules FILE_OR_FOLDER`, say, or
  `hylly rules try NAME --rules FILE_OR_FOLDER`. Each one's `--help` shows it, and the help page's *Every command and
  option* lists them all. `hylly serve --rules FILE_OR_FOLDER` takes it for that one run, here in the foreground: a
  server in the background, restarted (`hylly server restart`, the server page's Restart) or started at logon goes by
  the settings, so a rules file it should always read goes in `rules =` or `HYLLY_RULES`.

`hylly rules copy genres` puts a copy of the shipped `genres.toml` in your rules folder, with every rule in it
commented out and the tables' names and notes as they are: it changes nothing until you take the `#` off a line.
Each rule is one line there, a long list too, so a line taken in is always whole. Take it off only the lines you
change: what stays commented out stays as shipped, and gets the program's own updates. When `rules =` names one
file, write your changes there instead.

## How your files change the shipped ones

Four rules, the same for every table:

1. **Tables merge by name.** A genre, game, kind or format you name changes that one; the others stay as shipped.
   A genre's or a game's name goes with case not counting: `[genres."travel"]` changes the shipped Travel, which
   keeps its spelling. A kind's and a format's are in small letters.
2. **A value you write replaces the shipped one,** lists included.
3. **`<key>_add` adds to a list and `<key>_drop` takes out of it,** and leaves the rest as shipped.
4. **`off = true` takes a shipped entry away; `inherit = false` at the top of a table starts it empty,** so only
   your entries count.

Some examples:

```toml
# A genre of your own: it comes after the shipped ones
[genres."Railway Stories"]
words = ["railway", "steam locomotive", "branch line"]

# More words for a shipped genre, and one fewer
[genres."Travel"]
words_add = ["guidebook"]
words_drop = ["travel*"]

# A shipped genre you don't want
[genres."Romance"]
off = true

# Only your own games
[games]
inherit = false
[games."Pocket Picnic"]
words = ["pocket picnic"]

# Names in your files that are tags to take out, and a list's number before each title, as in "53.01 The Silent
# Orbit", taken off file names and the titles files give alike (none is, as the rules come: a title may start with a
# number)
[filename]
strip_add = ["(shelf copy)", "[scan]"]
strip_patterns_add = ['~[a-z]+~$']
number_pattern = '^\d{1,3}\.\d{1,2}[A-Z]?\s+'

# Plain text files in your own folders are books too
[formats.txt]
book = true

# Music from itch.io too: hylly itchio download takes only the kinds on this list
[itchio]
fetch_add = ["mp3"]
```

The finer points:

- In one table, a value comes first, then its `_drop`, then its `_add`, whatever their order in the file.
- `_add` and `_drop` compare words and the other lists with case not counting, and patterns exactly.
- A table with nothing in it, as in a copied file whose rules are still commented out, changes nothing.
- `off = true` is for one entry. To start a whole table empty, `inherit = false`.
- `off = true` on an entry that isn't there, or a `_drop` that finds nothing, is no mistake: a name may be written
  wrong, or a later version may no longer ship it. `hylly rules` lists it under its file as *nothing to take away*,
  and `hylly rules show` as a note.
- An `_add` or a `_drop` in an entry that isn't there makes a new one, as a value would: `[genres."Travle"]
  words_add = ["guidebook"]` is a genre of its own. `hylly rules` lists it under its file as *a new entry, as there
  was none of that name*, and `hylly rules show` as a note.

How a rule is written:

- **words:** whole words or phrases, case and accents not counting, in every table. Spaces, punctuation and `_`
  end a word ("PDF_Bonus" has the word "bonus"). Between the words of a phrase any of them will do ("sci fi" finds
  Sci-Fi), and next to a mark such as the `&` of "d & d" none is needed (D&D). A trailing `*` means "starts with":
  `dystopi*` finds dystopia and dystopian; a `*` alone is no word. A plural that changes the word's end is a word of
  its own: `mystery*` doesn't find mysteries, so the shipped genres have `mysteries` too. In `[junk]` the words are at
  the start of a title or a name. In `[series]` a starred word takes in the rest of its word:
  `fold_words_add = ["collect*"]` folds "Moonwake Collected" into "Moonwake".
- **patterns** (`strip_patterns`, `title_patterns`, `author_patterns`, `number_pattern`, and `patterns` in a genre
  or a game): regular expressions, written as TOML literal strings (`'...'`), only where words can't say it. Case
  doesn't count in them, so a `(?i)` in front changes nothing. A list of them is used joined into one, so a group's
  name, `(?P<name>...)`, can be used once in it. `number_pattern` is used alone, and there case counts unless it
  starts with `(?i)`.
- **publishers** are found anywhere in a publisher's name; **formats** are extensions in small letters (in
  `[itchio]` `fetch` a dot in front or capitals don't matter); the other lists (`strip`, `title_extensions`,
  `[names]`, `[kind_genres]` `unless`) hold items as they are written, case not counting. A `strip` item with a `_` in
  it is looked for in a file's name as it is, `_` and all: `strip_add = ["_retail"]` takes "_retail" out of "The Moon
  Garden_retail", and leaves "The Retail Giant" as it is. The other items, and `strip_patterns`, look in the name with
  its `_` as spaces.
- **numbers** are plain numbers, such as `8`.

A mistake stops the command with the file, the table and the entry it is in, for example
`rules\mine.toml, [genres."Railway Stories"] patterns: 'steam(' isn't a pattern Hylly can read`. Two of your files
with the same name are told apart by their full paths. A server reads the rules and the settings when it starts,
and a mistake stops it there. Started hidden (`hylly serve --background`, `hylly server restart`, at logon), it
writes the message in `server.log`, and `hylly serve --background` and `hylly server restart` show it too. Once a
server runs, a mistake made in a file keeps the rules and settings it had, and is told once in its log; once more if
it is made again after being put right.

A rules folder or file that isn't there when the server starts, such as one on a share that comes up after the
start at logon, doesn't stop it: the server says so, and reads the rules once they are there. Until then, a page
that needs them says that something went wrong. `hylly serve --rules FILE_OR_FOLDER` with other files than a running server's
starts it again with them.

## When a change shows

- A command reads the rules and the settings once, as it starts: a change made while it runs counts from the next
  command. A running server looks at them again every few seconds.
- Kinds, genres, games, series and formats are applied when the catalog is built: `hylly catalog` shows a change.
  The OPDS feeds group series by the words the last `hylly catalog` saved, as the library page does.
- Titles and authors that came from a file's name are read again from the name by the rules in effect, without
  reading the file; so are the authors of a file whose own details have none.
- What a file's own details say was read when the file was, by the rules then: how `[names]` `author_separators`
  split its authors, what `[junk]` set aside, and what `[packages]` found in a ZIP. `hylly catalog --rebuild` reads
  every file again.
- `keep` says which formats `hylly set format` can choose now. A format chosen before stays chosen while the rules
  know the format; one taken away with `off = true` loses its choices.
- The kinds' words decide a shop's title's kind at its next download, and in the catalog at the next `hylly catalog`
  (where each title is read with it); a title downloaded already keeps its files whatever its kind becomes.
- Humble's kinds, labels and languages, and the `[download]` settings, decide which of a Humble title's files is
  downloaded (the other shops' come as they are): `hylly humble download --dry-run` shows what would change. Files
  already downloaded stay, and so does a title's format while Humble offers it, even when a format earlier on its list
  comes later (an EPUB beside a MOBI downloaded before): nothing downloads that by itself, and every Humble download
  counts such titles. `hylly set format` changes one title's. The e-reader feed takes
  `ereader` as the server starts: `hylly server restart` after a change.
- The same order picks a title's main file in the catalog, read for its details and its cover, of the files Hylly can
  read, never an extra (the format its download stands on first), and what it lacks of a description, series, publisher
  and ISBN a Humble title takes from the same book in its other formats (never a PDF); and the book read inside a ZIP
  package (a ZIP already read, with `--rebuild`).
- `[itchio]` `fetch` counts from the next `hylly itchio download`, and `[media]` `programs` from the next download from
  any shop, as no shop's programs are downloaded; each lists what they leave out (`--dry-run` too). A file an earlier
  run downloaded stays, and is listed as still there.

## The kinds

Every kind a title may be is a table in `kinds.toml`, `[kinds.NAME]`, and nothing else names them: the library page
(its Kind filter and a title's details, from the server), the feeds, the settings' lists under `[download]`, your
folders' `kind`, your corrections (`hylly set kind`) and every `--kind` take these and no others. A kind added in a
rules file of yours is a kind everywhere, with a list of its own under `[download]` if you give it one. A kind
can be added, but none can be taken away: in `[kinds]` and `[kinds.X]`, `inherit = false` and `off = true` act on
the words alone (`bundle_words`, `publishers`, and `[kinds]`' own `prose_max_mb`), as they did when those tables held
only words, so a rules file written then means what it meant (`[kinds.comic] off = true`: comics found by their words
no more, by their CBZ still). A kind may not be named as one of `[download]`'s own settings (`ereader`, `tablet`,
`size_tie`, `language`). Their order is the order they are listed in, and the order a Humble download fetches them
(the small and useful first); it goes from the most general to the most particular. Each may have:

| Key | What it says |
|---|---|
| `label`, `plural` | its name for one and for many: "Comic", "Comics" (the page's Kind filter, the feeds' By kind); by default the kind's name, and that with an s |
| `device` | where it is best read: `ereader`, `tablet` or `either` (fine on an e-reader, better on a tablet); `ereader` by default. Comics and RPG books are `tablet`, tech books `either`, as they come. A title's file can still make it a tablet's: `[devices]` in the settings |
| `why_device` | why it is read there, in a few words, as `hylly rules try` and a dry run's `--json` say it (tech books': "better on a tablet for code and diagrams"); without it, the device's own words |
| `folders` | your own folders of these names are of this kind unless their settings say otherwise: `comics`, `manuals` |
| `bundle_words`, `publishers` | words in the name of a bundle a shop's title came in, and names found as whole words in who made it (its publisher, an itch.io creator: "udon" is Udon Lantern Press, never Pseudonym Press), that make it this kind |
| `prose` | a title of this kind by its words whose text file (EPUB, MOBI) is smaller than `[kinds]` `prose_max_mb` is a novel: a book (comics and RPG books) |
| `layout` | a title offered in a format of this layout (`formats.toml`) is this kind, whatever its words: comics, by a CBZ or a CBR |
| `itchio` | itch.io's classifications of this kind, as its survey groups them: `book`, `comic`, `tabletop`, `other`, `game` |
| `joins_books` | a copy of it in another place is one title with a book's there, on one card in the library page (tech books, RPG books); a copy of any other kind joins only its own kind's |
| `text_first` | its e-book opens before its pages (a PDF, a comic's) in the library page's reader (books) |

What kind a shop's title is goes by the same rule at every shop (`Rules.kind_of`): a format of a kind's `layout`
offered makes it that kind; else the words of the most particular kind found (comics before RPG books before tech
books) in a bundle's name or in who made it; else its shop's own: a book at Humble and StoryBundle, an RPG book at
DriveThruRPG, and at itch.io the kind of its classification. The words of the shop's own kind change nothing: an RPG
publisher's name at DriveThruRPG leaves a title an RPG book, as without it, and the prose rule is only for words that
would make a title another kind. A shop's title goes by what its shop lists: Humble's by
every format offered, StoryBundle's by its bundle alone until the book is downloaded (its link alone names its format),
the catalog's by the files downloaded. Your own folders' titles are of their folder's kind. Your corrections win over
all of it, and a title not downloaded yet is downloaded as the kind you chose (see `docs/downloads.md`).

## Seeing what is in effect

```
hylly rules                       # where the rules come from, and what each of your files changes
hylly rules show genres           # the genres in effect, each marked shipped, from your file, or changed in it
hylly rules show filename         # one table
hylly rules copy genres           # the shipped genres.toml into your rules folder, commented out (never overwrites)
hylly rules try "Neon Rangers 002 (2009).cbz" --bundle "Comics Bundle"   # what the rules make of a name
```

`hylly rules try` also takes `--publisher`, `--kind` and `--mb` (the file's size). It makes a title of that one file
as `hylly catalog` makes every title, by the catalog's own code, so what it says is what a build does: a file in a
folder of yours with no details of its own, named by its book's title (as `named = true` reads a folder's files, so
"Title (Series Book 2)" gives the series), its authors tidied as the catalog has them, and a title that is only "Vol. 3"
shown with its series in front. Its kind is found as a shop's title's is, by its format, `--bundle` and `--publisher`,
unless `--kind` says, and its genres from the bundle's words and the title's. It reads the name as a shop's file
labelled by its format (CBZ) too, and says on a line of its own whether the name's words would make a shop's file an
extra, audio or video, and which language they name.

## Settings

What you prefer, rather than what Hylly knows, is in `settings.toml`. Every key is optional; these are the
defaults. A size of files or a disk (`max_mb`, `image_heavy_mb`, `cache_gb`, and `[kinds]`' `prose_max_mb` in the
rules) counts as disks are sold, 1 MB being 1,000,000 bytes; the defaults of `max_mb`, `image_heavy_mb` and
`prose_max_mb` are where they keep the limits they had in 1,024s, and `cache_gb`'s 10 is now 10,000,000,000 bytes. `memory_mb` counts in 1,024s, as memory does.

```toml
[download]                   # the format a title is kept in: the first on its kind's list offered, its largest file
ereader = ["epub", "azw3", "mobi", "azw", "prc", "fb2", "pdf", "djvu", "cbz", "cbr", "cb7", "txt"]  # every other kind
tablet = ["cbz", "cbr", "cb7", "largest", "pdf", "epub"]  # a kind read on a tablet; "largest": of the formats after it
book = []                    # a list for each kind in the rules; []: its device's, tablet or ereader
tech = []
rpg = ["pdf", "cbz", "cbr", "cb7", "largest", "epub"]  # the PDF first, which keeps the print layout; [] the tablet's
                             # list while [kinds.rpg] device = "tablet"
comic = []
manual = []
size_tie = 0.95              # "largest": files this near the largest count as as large, the first listed wins
language = ""                # "": the file whose label names no language; or one of [languages], such as "german"
unsure = "guess"             # where these are unsure (several PDFs, say): "guess" takes it and asks you after (hylly
                             # decide); "ask" holds that file back until you answer

[devices]                    # where a title is best read, by its kind's device ([kinds.X] in the rules) and its main
                             # file, the same for every title of every shop and every folder of yours; a ZIP package by
                             # the book inside it, never by its own size. These make a title a tablet's, whatever its kind
fixed_layout = "tablet"      # a PDF, a DjVu or a comic file; "either" or "ereader"
image_heavy_mb = 21          # a bigger text file is probably full of pictures: a tablet

[reader]
cache_gb = 10                # the server's disk: the pages it has drawn and comics unpacked whole
cache_folder = ""            # where, by its full path, a folder of its own on another disk say; "": Hylly's cache folder
                             # (Hylly uses pages and drawn there, and removes only the folders it made in them)
book_fonts = true            # false: the browser refuses the font files books bring
word_meanings = true         # false: the word box asks Wiktionary and Wikipedia nothing; it has only Bookmark
memory_mb = 128              # pages the server keeps in its memory, 16 to 16384; it also keeps to half of what's free
pages_ahead = 5              # pages the browser fetches before you get to them, 0 to 30; 0: it reads nothing ahead
pages_ahead_fast = 9         # and when you're turning fast, 0 to 60
sharp_ahead = 2              # sharp pictures of the next pages, when reading at the screen's width, 0 to 5
tiles_ahead = 6              # the page strip's pictures near you, 0 to 30
draw_ahead = 32              # pages the server draws before they're asked for, at most, 0 to 200; 0: none

[lookups]
enabled = true               # false: no source is asked at all; what was found before stays
order = []                   # the sources asked, in this order; one left out isn't asked; []: every one, in Hylly's order
accept = 0.8                 # a match scoring less isn't used
none_days = 180              # a title nothing was found for is asked again after this long
contact = ""                 # a way to reach you, sent with each question to the sources; none by default

[look]
theme = "hylly"              # the library's own theme, until someone picks another (hylly theme list)
themes = []                  # those that may be picked; []: all of them

[reviews]
who = "off"                  # who may give titles stars and words, which everyone sees: "borrower" (everyone logged in),
                             # "reader" (everyone but borrowers), "librarian" or "owner"; "off": nobody, and none show
moderation = "off"           # whose words wait for an owner: "on" (all but an owner's), "borrowers", or "off" (nobody's)

[tools]
# sevenzip = 'C:\Program Files\7-Zip\7z.exe'   # 7-Zip for CBR comics: the default on Windows, by its full path;
                                               # elsewhere "": 7z, else 7zz, on the PATH

[remote]
ssh = ""                     # on a computer that isn't the server: the server's name for ssh, and commands run there
                             # (remote.py), unless hylly -H or HYLLY_REMOTE names another; "": here
programs = ""                # the folder hylly is in there, such as "~/.local/bin", if ssh doesn't find it

[ereader_feed]               # the e-reader feed: which titles it lists, and the one file each offers
kinds = ["book", "tech"]     # the kinds it lists (of those in the rules: book, tech, rpg, comic, manual)
formats = []                 # the first a title has is offered; []: those of ereader under [download] that are books
                             # and flow, so no PDF or TXT. A title you put in the feed yourself offers any of formats,
                             # then of ereader, of any size: the e-reader's list decides
max_mb = 63                  # a bigger file isn't offered: mostly image-heavy
skip_tablet = true           # titles suggested for a tablet (their kind, or [devices]) are left out
pdf_from = ["humble", "storybundle", "drivethrurpg"]  # PDFs only from these sources, by folder name; "any": from everywhere

[backup]                     # hylly backup
target = ""                  # the backup's folder, by its full path, for a plain hylly backup (a timer's); "": none, so
                             # hylly backup TARGET names it. The one typed wins
keep = {}                    # how many of its zips stay, as restic's forget counts them, such as { daily = 7,
                             # weekly = 4, monthly = 12 }; {}: every one
```

`hylly settings show` lists each with its value, marked default, settings.toml or the environment, and the same for `[server]`, `[bell]`,
`[ereader_feed]`, `[inbox]` and each `[folders.NAME]` (`settings.example.toml` has them all). A value of the wrong kind
stops the command, or the server as it starts, with the section and the key, as a mistake in a rules file does, in
every section: `access_log = "false"` in quotes is a text, not false. Numbers are finite, may be in quotes (`"30"` can
only mean 30), and `none_days` is at most 999999999. A key a section doesn't have, a section there is none of, and a name at the top other than `library`,
`rules` and `storybundle_user` are told once and left out, and `hylly settings show` lists them as such; so is a key Hylly no
longer reads, such as `lan` or `login`, with what took its place: `[devices]` `tablet_kinds` and `either_kinds` went
for each kind's own `device` in the rules, and a folder's `devices` for every title's device. On Windows,
`sevenzip` is a full path; elsewhere it may also be a name alone, looked for on the PATH. `settings.toml` is UTF-8
too, and a mistake in its TOML names the line. A command stops at the first mistake; `hylly settings check` lists all of them.

## Adding a rule

For a contributor: a new key of the shipped rules, or a new table. It takes five places, and the rest follows from
them:

1. **The key, and the kind of value it holds,** in `SPECS` in `src/hylly/rules.py`: a table's own keys in its `Spec`'s
   `keys`, or the keys each of its entries may have in `entry` (as each `[genres."Horror"]` has). The kinds are words,
   patterns, a pattern, a list, true or false, a number and text. A new table also goes in `FILE_OF`, by the shipped
   file it is in, and a new file in `SHIPPED`.
2. **Its value as shipped,** in that file in `src/hylly/data/`, with a comment that says why wherever it's tricky.
3. **What it does,** in `Rules.__init__`: the value made into a pattern, a list or a set once, as the rules are read,
   and used through `rules.current()` where it counts, with a test of it in `tests/test_rules.py`.
4. **The snapshots,** `tests/shipped_rules.json`, which holds every shipped value in its order, so that none changes
   unseen, and `tests/shipped_word_answers.json`, what the `[series]` and `[junk]` words find in made-up titles, title
   by title. Once the change is reviewed, write them again, both with the one command, and commit them with it:
   `uv run python -m pytest tests/test_rules.py -k as_reviewed --new-snapshot`
5. **This page:** the file's row in the table at the top, and what the key does wherever it fits below.

Checking a file, the four ways yours change the shipped ones, `hylly rules show` and `hylly rules copy` go by `SPECS`
and `FILE_OF`, and need nothing more. Once released, a table stays ([What stays put](architecture.md#what-stays-put)):
one that is no longer read goes into `RETIRED`, with what took its place.
