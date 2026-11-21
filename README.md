# Hylly

**Hylly** (Finnish for *shelf*) is a home library for the e-books and comics you own. It downloads them from the shops you bought them from, keeps them safe in one folder, finds their covers and details, and lets you read them in the browser or in your reading apps, at home or away. It is built by AI: its code, tests and documentation were written by Claude.

**[See it in pictures](https://janisc.github.io/hylly/):** the library, the reader, and what it's for.

It is a toolbox: a small Python program, `hylly`, with one command per job, plus a few free parts and services it builds on. You can use only the parts you need. It calls each thing by one name, always the same: [the glossary](docs/glossary.md) says what each means, from *shop* and *kind* to the *library feed*.

An AI assistant can help you with it too: run its commands, set up your library and its server and check them with `hylly doctor`, or add a shop or a lookup source that's missing. Hylly comes with notes and skills for one ([AGENTS.md](AGENTS.md), [the playbooks](docs/playbooks.md) and the skills in `.claude/skills`), and its commands say what happened in a way an assistant can read. It can help, and it can be wrong, as anyone can: read what it's about to do, above all before anything is removed or a shop's account is changed, and keep a backup.

## What it solves

**"I've bought hundreds of books in bundles, and I don't know what I have or where it is."**
Hylly lists everything you own on Humble Bundle, StoryBundle, DriveThruRPG and itch.io, and downloads it all (from itch.io, the books, comics and tabletop games). Downloads resume after an interruption. Each file is checked before it's kept: its length against the download server's, and its MD5 against the shop's checksum or else the server's. When the shop's checksum doesn't confirm it and the server's can't either, it's kept on its length and its first bytes, which must be those of its kind, so a sign-in page or an empty answer is never kept as a book. One format is kept per Humble Bundle title, by rules you can change, and nothing is downloaded twice.

**"My files are all over the place: old downloads, comic folders, game manuals, duplicates."**
Everything lives in one library folder, on your computer, a NAS or a USB disk. The files stay exactly as they were delivered. The friendly part (titles, authors, series, genres, tags) is a separate catalog on top, so nothing is ever renamed or moved behind your back. Folders of books, comics and manuals you got elsewhere join in as they are.

**"File names tell me nothing. Where are the covers? Which volume comes next?"**
The library page shows covers, series in volume order, genres, your own tags, and search and filters over everything. Details the files lack are looked up automatically in Metron, Comic Vine, Open Library and Google Books, politely and within each source's limits. You can correct titles, authors, series and more yourself, in the page.

**"Getting books onto my tablet and e-reader is a chore."**
Cables or sync apps, deciding what fits, keeping every device in order. With Hylly your reading apps reach the library themselves, over OPDS: they browse and search it like a bookshop, with covers and series, and download only the books you pick, whole, so they stay readable without a connection. An e-ink reader gets a feed of its own, with only the books that suit its screen. Most people have never heard of OPDS, and it changes everything: see [OPDS: the library in your reading apps](#opds-the-library-in-your-reading-apps).

**"I just want to read, on whatever is at hand."**
The library page has a reader built in. Comics and PDFs come a page at a time, so even a huge one opens at once. E-books flow to your screen, with your choice of font and size, contents, search, and the meaning of a word when you hold your finger on it. The place you stopped at is kept. On a tablet or a phone it can be added to the home screen and opens like an app.

**"When I'm away from home, my library is out of reach."**
Through Tailscale your own phones, tablets and laptops reach the library from anywhere, privately and over HTTPS, with your own users and passwords. No port is opened on your router, and nobody outside your Tailscale network can get to it. For an e-reader or a friend without Tailscale, Tailscale Funnel can give it a public address instead, which puts its login page on the internet: see [Ways to run it](#ways-to-run-it).

**"Is it still running? Is the disk all right?"**
A server page shows what's running, who's connected and the latest messages. HyllyBell sends alarms to your phone through ntfy, or to a program of yours for any other way of telling you, for things like wrong passwords, a new device or a burst of downloads, with a daily summary. `hylly verify` checks that the files downloaded from the shops or copied in with `hylly add` are still there and intact; the files that were in your own folders already have no record to check them against.

**"What if a shop closes, or loses my books?"**
Then you still have them, all checked, in a folder you own.

## How the parts fit together

```
 Shops                       Folders of your own
 Humble Bundle · StoryBundle  books · comics · manuals
 DriveThruRPG · itch.io       you got elsewhere
      │ download                    │ copied in or read where they are
      ▼                             ▼
 ┌──────── Library folder (computer, NAS or USB disk) ────────┐
 │  files exactly as delivered, each one checked by MD5       │
 └─────────────────────────────┬──────────────────────────────┘
                               │ catalog  ◄── lookup: Metron, Comic Vine,
                               ▼               Open Library, Google Books
              Catalog: covers, series, genres, your tags
                               │ serve
                               ▼
     Library server ──► library page and reader (tablet, phone, desktop)
                    ──► OPDS feeds: reading apps on tablets and e-readers
                    ──► HyllyBell: alarms to your phone (ntfy, or a program of yours)
                    ◄── Tailscale: from anywhere, with a login
```

- **The library folder** is the heart: plain files and folders, readable by anything.
- **Hylly's own records** (your tags and corrections, the catalog, the accounts, what was downloaded) are in one database, `hylly.db` in the data folder of the computer that keeps them. The server copies it into the library's `state\db-backup` every night, and `hylly backup` takes a fresh copy, so the library can move to another disk or computer and still know itself.
- **The library page** comes from the server, which lays your tags and corrections over the catalog as it sends it. The library's `catalog` folder keeps what the server reads: the titles as the last build wrote them, and their covers.
- **The server** gives you the page with its reader, the feeds, logins and editing. It runs quietly in the background and can start when you log on.

## OPDS: the library in your reading apps

OPDS is an open standard that lets a reading app browse a library, much as a shop's own app browses its shop. Many reading apps speak it: Panels and Yomu on the iPad, and KOReader on e-readers and Android, among others. Hylly serves your whole library over OPDS, and that solves a lot:

- **No more copying files to devices.** Add the library to an app once, as an OPDS catalog (one address), and the app sees everything: what's new, by source, kind, genre and tag, series in volume order, and search, all with covers.
- **Each device holds what you choose.** Pick a book in the app and it downloads, whole, onto that device's shelf. Nothing to sync and nothing to tidy up on the library's computer.
- **Ready for a journey.** Downloaded books are on the device, so they read without a connection: fill the tablet before the train or the plane. The reader in the library page works the other way round: it fetches from the server as you go, so it's for reading at home or online.
- **Two feeds for two kinds of screen:**
  - `…/opds`, the whole library, for a tablet: comics, PDFs, art books, everything.
  - `…/ereader/opds`, only what suits an e-ink screen: books and tech books, each offered as its one best file (EPUB first) and small enough for the device, without the comics and picture books. It's for e-readers that can add an OPDS catalog. Its rules are under `[ereader_feed]` in the settings, and you can put single books in or leave them out yourself.
- **Your own, in each.** Both feeds are the logged-in person's own: each starts with what you're in the middle of, your reading list and your lists ticked for the reading apps, then the whole library.
- **At home and away.** Every app logs in with your name and password, at home too. With Tailscale it uses the same HTTPS address at home and away; an e-reader that can't run Tailscale uses the address on your home network (see [Ways to run it](#ways-to-run-it)).

## Quick start

Hylly runs on Windows, macOS and Linux. You need [uv](https://docs.astral.sh/uv/) (it brings Python with it), [git](https://git-scm.com/), which uv fetches Hylly with, and, for CBR comics, [7-Zip](https://www.7-zip.org/) with its RAR part, which most of them need (`hylly doctor` says whether the 7-Zip it finds has it):

- **Windows:** 7-Zip from 7-zip.org.
- **Debian and Ubuntu:** the packages `7zip` and `7zip-rar` (on Debian from non-free). On other Linux systems, 7-zip.org's `7zz` has the RAR part, where a system's own package may not.
- **macOS:** 7-zip.org's download for macOS, as Homebrew's `sevenzip` is built without the RAR part. Unpack it (double-click it in Finder), and put its `7zz` in a folder on the PATH, or give its full path as `sevenzip` under `[tools]` in the settings, outside Hylly's own folders and the library. macOS may hold it back the first time it runs, as a program from the internet: allow it in System Settings, *Privacy & Security* (*Open Anyway*), or on an older macOS open it once from Finder with right-click, *Open*.

1. **Install,** like any command-line tool, from any folder:

   ```
   uv tool install --force --python 3.14 git+https://github.com/janisc/hylly@main --overrides https://raw.githubusercontent.com/janisc/hylly/main/tool-overrides.txt --constraints https://raw.githubusercontent.com/janisc/hylly/main/tool-constraints.txt
   ```

   It puts `hylly` and `hyllyd` on the PATH (`uv tool update-shell` adds uv's folder for them if they aren't found), with a Python and libraries of their own. The two files from the same address give it the versions of its libraries Hylly was tested with, which the weekly check for published flaws goes by (`tool-constraints.txt`), and leave out what Hylly never uses (`tool-overrides.txt`): uv takes no lock file for a tool. The same command installs it again over itself for an update: *Keep Hylly up to date* in [Growing it](#growing-it).

   **Just looking?** `hylly demo` makes a small library of made-up books, apart from everything else, and opens it in your browser. It needs no shop's login and no books of your own: it makes its own owner, `demo`, and at a terminal logs your browser in as it with a one-time link. The steps below can wait until you want a library of your own.
2. **Settings:** `hylly settings edit` opens `settings.toml` in your editor (empty at first; [settings.example.toml](settings.example.toml) says what each setting does). `library` is the one to set: the folder for your library, on this computer, or a share such as `\\NAS\Books`; step 3 can write it for you. `hylly settings places` says where Hylly keeps its own files on this computer: the settings, the database, the logs and the reader's cache, each in a folder of its own where your system keeps such things, or all in one folder for an install that has them so (*Where things are* in the help).
3. **Start the library:** `hylly init FOLDER` marks the folder as your library, so that a disk that isn't there can't be taken for an empty library, and writes the `library` line into `settings.toml` when there is none yet.
4. **Log in to a shop** (steps 4 to 6 are for each shop you use, Humble Bundle here and the others alike; skip them for folders of your own, which step 7 reads as they are: [Growing it](#growing-it) has both): `hylly login humble` asks for your Humble session cookie: log in on humblebundle.com, and copy the value of `ory_session_stoiccohens510ht9xek` (its sign-in's cookie) from the browser's developer tools (`--clipboard` takes it from the clipboard), or export the browser's cookies to a cookies.txt and give it with `--cookies cookies.txt`. Check: it says Humble takes it. The cookie stays on this computer and is never printed.
5. **See what it would download:** `hylly humble download --dry-run` reads your orders and lists what it would download, the best format of each title, with what your library has and still needs, per kind; it downloads nothing and saves nothing.
6. **Download a few, then everything:** `hylly humble download --limit 5`, then `hylly download`, which runs every shop you have a login for. Ctrl-C stops safely; running it again goes on where it stopped.
7. **Build the catalog:** `hylly catalog`. The library page shows it once the server runs (step 9).
8. **Add yourself:** `hylly user add NAME` asks for a password (a script gives it with `--password-stdin`): the first user is the library's owner. Every request to the server logs in, from this computer too, and nothing is open without a login.
9. **Serve it:** `hylly serve` starts the server and, at a terminal, shows a one-time link that logs a browser in as the owner, once, within 10 minutes; on Windows and macOS, or where there is a desktop (`DISPLAY` or `WAYLAND_DISPLAY` set), it opens it in this computer's browser too. `--background` keeps it running hidden, with the settings as its only options, and `hylly server autostart on` starts it at every logon: on Windows it sets that up, and on Linux and macOS it prints the systemd user unit or the launchd agent for this computer, with the commands that install it, as the system then runs the server (`hylly server status`, `stop` and `restart` look after it). For your tablet or e-reader at home, set `listen = "0.0.0.0"` under `[server]` in the settings (`hylly settings edit`) and run `hylly server restart` (or `hylly serve --lan` for one run, in that terminal): it then shows the address to use, and each device logs in once. When Windows asks whether Python may accept connections, allow private networks only. [Ways to run it](#ways-to-run-it) has the rest.

Every command, and each of its actions, explains itself with `--help` (`hylly humble download --help`); `hylly` alone shows where to start, `hylly --help` lists every command by what it's for, and `hylly --version` says which version you have. Every command that changes something does it at once, and with `--dry-run` (`-n`) only says what it would do. Then add what you need, from the list below.

**The commands you'll use most:**

```
hylly download           # what's new at every shop you have a login for
hylly catalog --lookup   # the new books into the library page, with their details
hylly serve              # the library page and the reader, in your browser
hylly server status      # is the server running, and at which addresses
hylly jobs               # what's running, and how each job's last run ended
hylly doctor             # what shape it's all in, with the next step
hylly backup TARGET      # a backup on another disk (--dry-run: the plan first)
```

**One program, and the server:** `hylly` takes every command. Beside it, `hyllyd` is the library server itself, and only that: `hylly serve` starts it for you, and a service runs it as `hyllyd --in-background`, with the settings as its options. The server needs none of the shops' logins, which are kept in a `logins` folder of their own, and its code can't reach them. [docs/architecture.md](docs/architecture.md) tells how the parts fit together.

## Growing it

| To | Use |
|---|---|
| Add more shops | `hylly login storybundle` (a cookie, and your account's number in the settings: `storybundle_user`) or `hylly login drivethrurpg` (an application key), then `hylly storybundle download` and `hylly drivethrurpg download`; every shop works the same way, and `hylly download` runs them all. No shop's programs, scripts or shortcuts are downloaded: every run names what it left out |
| Add itch.io | `hylly login itchio` (an API key). From then on `hylly download` includes itch.io (`hylly itchio download`): it fetches the books, comics and tabletop games in your itch.io library, and the items filed as other or as games whose files are all books or comics (`--types` of `hylly itchio download` chooses others), into `itchio\`, and takes of their files only the kinds on the rules' list (`[itchio]` in `formats.toml`), never a program. Two steps are its own, and `hylly download` never takes them: `hylly itchio survey` counts what your library holds (and your bundles, with `hylly login itchio-bundles`, a cookie from the browser), by kind, with the space it would take, and downloads nothing; `hylly itchio claim`, with that cookie and once a survey has finished, adds the bundles' items you want to your library, asking first |
| Choose what to download | `hylly list ~/hylly-list.csv` writes everything you own at every shop with a login, a row per file, what a download would fetch ticked (`hylly humble list` and so on for one shop, with its download's options that choose titles); edit its `download` column in a spreadsheet or an editor, then `hylly download ~/hylly-list.csv` goes by your changes: a row you tick comes, and one you untick stays left out of every later download until you tick it again (or `hylly set left-out auto --id ID` with its id). Moving the mark between a title's files answers the question of which to keep. `--dry-run` says first what it would bring; over ssh to the server computer, `-` gives the list through standard output and input |
| Bring in folders of your own | put them in a folder of their own in the library, any name: the catalog reads it as it is, and `[folders.NAME]` in the settings can say more (a name to show, a kind, subfolders as genres). `hylly add NAME` copies books in from elsewhere, each copy checked. The Inbox, `[inbox]` in the settings, is a folder you drop books of every kind in, which the server empties into the library once a minute: each book copied into the folder of yours its kind belongs in, its folders there read as its collection, its kind and its `#` tags, checked, looked up and catalogued, and only then removed from there, and what can't be taken set aside in its `Not added` folder with a note why. `hylly humble seed <folders>` adopts copies of Humble files you already have, so they aren't downloaded again |
| Look up missing details | `hylly lookup` with free keys from Metron, Comic Vine and Google Books (`hylly login metron`, and so on, which says where to get each one; Google's key goes in a project without billing, restricted to the Books API); Open Library needs none. It keeps well inside each source's limits, and only one runs at a time |
| Tag and correct titles | `hylly tag` and `hylly set`, or straight in the library page |
| Read on your devices | `listen = "0.0.0.0"` under `[server]` in the settings (or `hylly serve --lan`), then the addresses `hylly server status` shows: the page's in a browser, `/opds` in a tablet's reading app, `/ereader/opds` on an e-reader, each with a name and password (see [OPDS](#opds-the-library-in-your-reading-apps)) |
| Let others in, or reach it from outside | `hylly user add NAME` for each person (the first is the owner, the rest readers unless `--role` says borrower, librarian or owner: a borrower only reads in the page, with no files or feeds; a reader also downloads and uses the reading apps; a librarian also corrects one title at a time; an owner does everything, many titles at once and the Server page too), and `hylly user link NAME` for a one-time link that logs their browser in; from outside, Tailscale: see [Ways to run it](#ways-to-run-it) |
| Work from another computer | the first computer that works on a library keeps its records; any other one runs its commands there, over ssh: `[remote]` in its settings, or `hylly -H box doctor` for one command, as long as the one that keeps them runs Linux or macOS (Windows' own ssh server reads a passed-on command another way). The help's *From another computer* has the rest |
| Get alarms on your phone | the free ntfy app, with its address in `secrets/ntfy-url` in the settings folder, and `[bell]` in the settings; `hylly bell test` sends one |
| Check the downloaded and copied files | `hylly verify`: every file a shop's download or `hylly add` recorded is still there, at its size, and `--hash` re-reads each one against its recorded MD5. The files that were in your own folders already have no record to check them against |
| A backup on another disk | `hylly backup TARGET` keeps the books as plain files, each one checked, deleting no book, with your records, edits and settings in a zip beside them, the records copied as they are at that moment. The first backup to a disk asks first, and `--dry-run` shows the plan. `hylly backup check TARGET` reads every copy and the newest zips again, and `hylly restore BACKUP NEW_FOLDER` brings it all back, the records too. With `target` under `[backup]` in the settings a plain `hylly backup` runs on a timer (`examples/hylly-backup.timer`), and `keep` there thins the older zips |
| Copy a lot, and leave it running | `hylly copy SOURCE TARGET` (HyllyCopy) moves terabytes or tens of thousands of small files between any two folders or shares: only reads the source, overwrites nothing, checks every copy, and waits out a share that stalls or restarts. The first copy to a target asks first, `--dry-run` shows the plan, and `hylly copy check TARGET` reads the copies again |
| See what shape it's all in | `hylly doctor` lists the settings, the library, the logins saved and more, with the next step for whatever is wrong; `hylly jobs` says what's running, and `hylly logs` where the logs are |
| Run it from a script or an AI assistant | exit codes that say what happened (70: a bug, to report), the result alone on standard output and the rest on standard error, `--json` on the commands that report, and `hylly commands` for what each one may do: [AGENTS.md](AGENTS.md) for an assistant, then the playbooks in [docs/playbooks.md](docs/playbooks.md), from setting it up to a backup and its restore |
| Keep up with the shops | `hylly download` again: what's new at every shop. `--update` also fetches the files Humble and DriveThruRPG have replaced (the old ones are kept in `superseded\`); StoryBundle's books come once, as they are; itch.io's download asks about an item's files again once its answer is two weeks old (or a few days more, by item), and at once after a file of it has gone, so a creator's new and replaced files come, each beside the old one, which stays (`hylly itchio download --again` asks about every item now) |
| Keep Hylly up to date | the install again, over itself, `uv tool install --force --python 3.14 git+https://github.com/janisc/hylly@main --overrides https://raw.githubusercontent.com/janisc/hylly/main/tool-overrides.txt --constraints https://raw.githubusercontent.com/janisc/hylly/main/tool-constraints.txt`, for the newest Hylly with the versions of its libraries it was tested with (`uv tool upgrade` keeps those it has); then `hylly server restart` and `hylly doctor`. On Windows, `hylly server stop` first, as a running server holds its libraries' files open, and `hylly serve --background` after. The help's *Keeping it up to date* has these in one block to copy, and what else to keep new: Python, 7-Zip, the reading apps |

**For the home network:** set `listen = "0.0.0.0"` under `[server]` in the settings (`hylly settings edit`), then `hylly server restart`.

**To stop using Hylly:** `hylly server autostart off` and `hylly server stop` first, while the command is there, and then `uv tool uninstall hylly`: a start at logon outlives the program. The help's *Stopping using Hylly* has the rest, by hand too, with what to keep and what stays behind: Hylly's own folders with the logins, keys and everyone's reading, the reader's cache, Tailscale Serve or Funnel, and what the browsers keep.

**The help page:** the library page's **Help** link opens the user guide, which the server makes from [src/hylly/help.html](src/hylly/help.html) (at `/catalog/help.html`). You as the owner get it with your own folders filled in; everyone else gets them in words. It has a section for every part, and a reference of every command and option, made from the program itself so it can't fall behind. What changed in each version is in [CHANGELOG.md](CHANGELOG.md).

## Ways to run it

Every request logs in, wherever it comes from, this computer too, and each user has the same role everywhere: nothing is let in by where it comes from. What you choose is where the server listens and how other devices reach it. [docs/ways-to-run-it.md](docs/ways-to-run-it.md) has a block of settings for each way, with what to type and why:

- **One computer:** nothing to set. The server listens on this computer only (`listen = "127.0.0.1"` under `[server]`, as it comes).
- **A home server:** `listen = "0.0.0.0"` for the devices at home, and HTTPS at home too through [Tailscale](https://tailscale.com/) Serve (`tailscale serve --bg 8770`), a VPN with a free plan, with `address` set to the `https://….ts.net` address it gives. A device that can't run Tailscale, such as an e-reader with KOReader, uses the plain home address with a reader account of its own: `hylly server status` shows it as *Home network*. When Windows asks whether Python may accept connections, allow private networks only. An Inbox on a share of its own takes the books in, and the library's share can stay read-only: on Linux with the server walled in, `examples/hylly-jobs.service` and its timer run it.
- **Open to the internet:** Tailscale Funnel (`tailscale funnel --bg 8770`) or an HTTPS proxy of your own, pointed at the server's one port, handing on the name the browser used (nginx: `proxy_set_header Host $host`, or logins are refused), with the proxy's address in `trusted_proxies` when it isn't on this computer. The login page is then on the public internet, where scanners find it within minutes: give every user a long password, run `hylly doctor`, and on Linux run the server walled in, with [examples/hylly.service](examples/hylly.service), which `hylly serve --service-file` prints for your computer.
- **In a container:** planned, later.

## The library folder

```
<library>\
  humble\<bundle>\<title>\<file>              Humble Bundle files, as delivered
  storybundle\<bundle>\<title>\<file>         StoryBundle files (ZIPs kept as delivered)
  drivethrurpg\<publisher>\<product>\<file>   every file of each DriveThruRPG product, but programs
  itchio\<creator>\<title>\<file>             the files of each itch.io item, of the kinds on the rules' list
  collections\, comics\, manuals\             folders of your own, one per collection
  catalog\        the titles as the last build wrote them for the server, their covers and caches: always safe
                  to delete, rebuilt by hylly catalog
  curation\       the words that suggest tags (tags.toml)
  superseded\     older versions of files a shop replaced (never deleted)
  state\          private: order data, the lookups' answers, logs, which computer keeps the records
                  (database.json), and the database's nightly copies (db-backup)
```

## One file per title

What Hylly knows (the words that make a bundle a comic one, the formats, the genres) is in rules files you can change, and what you prefer is in `[download]` of the settings: see [docs/rules.md](docs/rules.md), `hylly rules` and `hylly settings show`. By default:

1. **Kind:** a *comic* when CBZ or CBR is offered, or the bundle or publisher is a comic one; an *rpg* or *tech* book by its bundle or publisher; otherwise a *book*. A small EPUB in a comic or RPG bundle is a novel, so a *book*.
2. **Comics:** CBZ, then CBR or CB7 when offered, otherwise the larger of the PDF and the EPUB (`tablet`; put `"largest"` first on it to prefer the biggest file, as HD PDFs often are).
3. **RPG books:** the PDF, which keeps the print layout (`rpg`).
4. **Books:** EPUB, then AZW3, MOBI, PDF (`ereader`).
5. **Your choice** (`hylly set format pdf`, or one file of several in a format: `hylly set file`) beats these rules when the shop offers it. A title already downloaded keeps its format while it is offered, so a change of these lists downloads nothing again.
6. **Fallback:** when Humble no longer has the chosen file, or it comes wrong twice, the next format is tried, and the record says which one stood in. Any other failure leaves the title to be tried again as chosen, and a title with another format standing in gets its chosen file tried once on every later download.
7. **Parts and extras** (issues, supplements, maps, code) are all kept. Translations are skipped when an untranslated file exists. Audio and video aren't downloaded.
8. **Duplicates:** a title bought more than once is downloaded once, into the folder it has already, and a file a shop renamed isn't downloaded again under its new name.
9. **Where the rules are unsure** (several PDFs of a title, an item of extras only, a file gone from its folder; in the catalog, two copies that may be one book, tags whose title is gone, files of an unknown kind in a folder), they guess as above and keep the question for you: every download and `hylly catalog` says how many, and `hylly decide` lists them and takes your answers, and `hylly decide NUMBER auto` takes one back; a lookup's near match is shown in its title's details instead (`hylly decide --kind match` lists them) (with `unsure = "ask"` under `[download]`, a download's file waits for your answer instead).

## Privacy

- **Your logins and keys** live only in Hylly's own folders on your computer (`hylly settings places` says where: a folder for each kind where your system keeps such things, or all in one, `%LOCALAPPDATA%\Hylly` on Windows and `~/.local/share/Hylly` on Linux and macOS, for an install that has them there, or where `HYLLY_HOME` puts them), outside the library and the program, yours alone, and are never printed.
- **Your settings** live there too, so personal values never end up in this repository.
- **`state\`** holds the shops' order data, which can open their download pages: keep it private. The server never serves it.

### What leaves your computer

Hylly has no server of its own, so nothing goes to its makers. Only these leave your computer, and only when they are used:

- **To the shops you log in to** (Humble Bundle, StoryBundle, DriveThruRPG, itch.io): your login for that shop, which goes to no one else, and the questions about your library and orders. The files come from the download links the shop gives. itch.io also gets the claims you ask for (`hylly itchio claim`, which asks first).
- **To the lookup sources,** only when you run `hylly lookup` (or `hylly catalog --lookup`), or the Inbox takes in books (unless `[inbox]` says `lookup = false`), and only about titles that lack details. Metron and Comic Vine get a comic's series and number. Open Library and Google Books get a book's ISBN, or else its title and first author (Google Books only the surname). Google Books also gets a comic's series and number when the comic may be a collected edition and the sources before it haven't found it. Each key goes only to its own source. Saving a source's key with `hylly login` also asks that source one fixed question, to check the key, with nothing about your library. A source left out of `order` under `[lookups]` in the settings is never asked during lookups. As it comes, `order` lists none, which asks every one: Open Library needs no key, so it is asked unless a list leaves it out. `enabled = false` under `[lookups]` asks none at all.
- **Shop covers:** a title whose file has no cover of its own shows the shop's cover picture. The browser of whoever views the library page, or their reading app, loads it straight from the shop's own image servers, or from the image service the shop uses.
- **Word meanings in the reader:** when you hold a word in a book, or pick a few, the reader's browser asks Wiktionary or Wikipedia about them. They get the words; for a single word, also the few words right next to it (up to two on each side), to find a term such as *sotto voce* or a name; which language the book is in; and the library's address, which the browser sends along. `word_meanings = false` under `[reader]` in the settings turns it off.
- **Alarms to your phone,** only if you set an ntfy address (in `secrets/ntfy-url` in the settings folder): each alarm and the daily summary go to that ntfy server as text, in counts and kinds: no names, addresses or file names, unless you set `remote_detail = "full"`, when they name users and their roles, internet addresses and kinds of device. An alarm about many downloads names the latest file. The daily summary names the addresses most often asked for without a login, by their first two parts, which for a file of the library can be a shop's folder and a bundle's. A lockout names the user name that was typed. A long job's note about how it ended goes the same way: in full detail, it can name a file or folder of yours, or what a shop answered. A program of your own named as `command` under `[bell]`, for e-mail or a webhook, gets the same text, and sends it wherever it sends it.
- **A tunnel or a proxy,** only if you set one up, such as Tailscale Funnel: what visitors from outside get then goes through it. Hylly doesn't start one; you point it at the server's one port (see [Ways to run it](#ways-to-run-it)).

Like any request on the internet, each of these shows the other end where it came from: your computer's internet address or, for covers and word meanings, the viewer's. Requests to the shops and lookup sources also name Hylly, its version and its project page. A way to reach you goes to the lookup sources only if you set `contact` under `[lookups]`.

What doesn't happen: no usage reports, no update checks and no crash reports. Apart from the shop covers and word meanings, the library page, the reader and the help load nothing of their own from other sites. Nor can a book in the reader, in any format and however its pages are written: the browser refuses its scripts, and any style, font, picture or sound from another site. The one exception is a page that is nothing but an SVG picture (or other XML that isn't a web page), which could at most ask the sites the shop covers come from.

## Built on

- [foliate-js](https://github.com/johnfactotum/foliate-js) (MIT): the e-book engine of the reader.
- [PyMuPDF](https://pymupdf.readthedocs.io/) (AGPL-3.0): PDF pages and covers.
- [Pillow](https://python-pillow.org/), [httpx](https://www.python-httpx.org/), [psutil](https://github.com/giampaolo/psutil), [rarfile](https://github.com/markokr/rarfile); [7-Zip](https://www.7-zip.org/) for CBR comics.
- [Literata](https://github.com/googlefonts/literata) and [Atkinson Hyperlegible](https://www.brailleinstitute.org/freefont/) (SIL OFL): the reader's fonts.
- [Lucide](https://github.com/lucide-icons/lucide) (ISC) and [Feather](https://github.com/feathericons/feather) (MIT): some of the library page's icons.
- [Tailscale](https://tailscale.com/) for access from outside, [ntfy](https://ntfy.sh/) for alarms.
- Details from [Metron](https://metron.cloud/), [Comic Vine](https://comicvine.gamespot.com/), [Open Library](https://openlibrary.org/) and [Google Books](https://books.google.com/); word meanings from [Wiktionary](https://www.wiktionary.org/) and [Wikipedia](https://www.wikipedia.org/).

**Browsers:** Hylly is made for Safari, Chrome and Edge, and tried in them. The library page needs Safari on iOS or iPadOS 16.2 or later, or a current Chrome or Edge, and reading e-books in it Safari 16.4. Others, such as a current Firefox, should work too, and what doesn't work in them is put right once it's reported. An older browser says what it needs, and a reading app can still use the library through its feed, at `/opds`.

**Your account, your shop's terms.** Hylly uses your own logins and only what your accounts offer you, but automating a shop may go against its terms. Whether to do that is your decision, at your own risk.

Hylly is an independent, unofficial project, not affiliated with, endorsed by or sponsored by Humble Bundle, StoryBundle, DriveThruRPG, itch.io or any of the services, apps and devices named here; their names and trademarks belong to their owners. It only downloads what your own account offers you. The licences of the included work by others, and the terms of the online sources, are in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

## For developers

- **Tests:** `uv run pytest`, in a checkout of the source after `uv sync --python 3.14`: all of them, as CI runs them, in several minutes. For a quicker answer while you work, `uv run pytest -m "not slow"` leaves out the few that take seconds each, and one file runs the tests of one module: most modules have a file of their own name, such as `uv run pytest tests/test_catalog.py` for `catalog.py`. Everything runs against small made-up libraries and fake shop servers, never the network or your settings.
- **Contributing:** [CONTRIBUTING.md](CONTRIBUTING.md): how to try it, where to start reading, and the tests that fail on purpose.
- **Design notes:** [the parts, and why they're so](docs/architecture.md), [the database](docs/database.md), [downloads](docs/downloads.md) (and how to add a shop), [catalog and server](docs/catalog.md), [lookup sources](docs/lookups.md) (and how to add one), [stars and reviews](docs/reviews.md) (a small state machine).
- **Words:** [the glossary](docs/glossary.md), one name for each thing, in the code and the docs alike.
- **Changing it:** [what stays put](docs/architecture.md#what-stays-put) for those who depend on Hylly, and [the code's style](docs/architecture.md#adding-to-it): no formatter, so a change never reformats a whole file.
- **What may come next:** [the roadmap](docs/roadmap.md), ideas in no particular order, none of them a promise.

## License

Copyright (C) 2026 janisc and the Hylly contributors

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It comes with no warranty: the licence, in [LICENSE](LICENSE), says so in full.

In short: you may use, change and share Hylly; if you run a changed version as a service for others, offer them its source. A running Hylly offers its own: *Source* in the library page's account menu and on the login page, and *Licences and source* at the end of its help, with the licences of the work by others it includes. A changed version run for others sets `source_url` under `[server]` to where its own source is, and those links lead there. PyMuPDF, which Hylly uses for PDFs, is under the same licence.
