# Hylly's architecture: its parts, and its two programs

Hylly is one command, `hylly`, that takes every command, as git or cargo do: one familiar name, and separate pieces
behind it. Beside it is one more program, `hyllyd`, the library server itself. This says what the pieces are, which may
use which, and why. The code is in parts to keep related but separate problems apart: getting books, cataloguing
them, serving them to readers, and keeping copies. The parts share one command rather than a program each, as more
names to learn would bring no gain, and `hyllyd` does nothing but serve.

## The programs

The programs are those `pyproject.toml` installs (`[project.scripts]`), each in the part of the module it starts,
and a test keeps this table to them.

<!-- pyproject.toml -->
| Program | What it is | Its part |
|---|---|---|
| `hylly` | Every command, each a subcommand with its own `--help`, and a command's actions as commands of their own: `hylly humble download`, `hylly server status`. `hylly --help` lists them by what they're for (`commands.AREAS`), and `hylly` alone shows where to start (`commands.START`). | front |
| `hyllyd` | The library server itself, and only that: what starting it needs (`commands.daemon_parser`). `hylly serve` starts it for a person, `hylly server status`, `stop` and `restart` look after it, and a service starts it as `hyllyd --in-background`. Its settings are its only options: what's typed is for one run in the foreground, nothing passes options between processes or remembers them, and it never opens a browser. It loads only the server's part, so not the shops' logins, nor the code that talks to the shops. | server |
<!-- end -->

- **The commands are parsers within parsers** (`commands.build_parser`): each action takes only its own options, as
  argparse holds it to, `--library` and `--rules` only where the action reads them, and no option may be shortened.
  What does something is a command, never an option: `hylly server stop`, `hylly jobs stop NAME`,
  `hylly copy check TARGET`, `hylly restore BACKUP NEW_FOLDER`. Calls between Hylly's own processes are under
  `hylly internal`, left out of `--help` and of `hylly commands`, but for `hylly commands --internal`.
- **A dry run wherever something changes:** every command that changes something does it at once, and takes
  `--dry-run` (`-n`), which only says what it would do. A dry run changes nothing: each database is read into a copy
  in memory (`db.start_dry_run`), and no lock, log or note is written. A test runs every one of them on a made-up
  library and compares every file before and after (`tests/test_assistants.py`). `hylly copy`, `hylly backup` and the
  commands that change many titles, or what can't be brought back, ask first, and take `--yes` (`-y`).
- **One install, like any command-line tool:** `uv tool install --force --python 3.14 git+https://github.com/janisc/hylly@main --overrides https://raw.githubusercontent.com/janisc/hylly/main/tool-overrides.txt --constraints https://raw.githubusercontent.com/janisc/hylly/main/tool-constraints.txt` (`hylly.INSTALL`, which the help fills in and the README
  gives as it is) puts `hylly` and `hyllyd` on the PATH: one package, `hylly`, with two entry points (`pyproject.toml`,
  `[project.scripts]`), so there's one thing to update, and no folder of the program's for anything to run in. A tool's
  install takes no lock: `tool-constraints.txt` hands it `uv.lock`'s versions (a test keeps the two alike), so the
  command runs on what the tests and the weekly audit checked. An update is the same command again, which takes the new
  versions, and a program taken away off the PATH (`uv tool upgrade` keeps the versions it was given). The help's
  *Keeping it up to date* has the whole update in one block, with Windows' own order: a running server holds its
  libraries' files open there, so it stops first. From a checkout of the source, the same with
  `uv tool install --force --python 3.14 --editable . --overrides tool-overrides.txt --constraints tool-constraints.txt`
  (CONTRIBUTING.md). Both name the Python, as uv would otherwise take an older one it finds (`hylly.PYTHON`).
- **Where Hylly keeps its own files:** `paths.PLACES`, one table: a folder for each kind (the settings, the data, the
  state, the cache and the logins), each with its own variable (`HYLLY_CONFIG_DIR` and the rest) and a default per
  system: Linux's XDG folders, macOS's `Library`, Windows' roaming AppData for the settings and its local one for the
  rest. Every module asks `paths.place` by kind. `HYLLY_HOME` keeps every kind in one folder, over their own variables:
  the tests, the demo and an assistant's sandbox use it. An install that kept everything in one folder before the kinds
  had theirs (`%LOCALAPPDATA%\Hylly`, `~/.local/share/Hylly`) goes on using it all in one, while it holds Hylly's files:
  nothing of it moves. `hylly settings places` and the doctor's `places` say which it is.
- **How the server is run:** in a terminal (`hylly serve`), hidden in the background (`hylly serve --background`, and
  the Windows task of `hylly server autostart on`), or by the system in the foreground, from a systemd unit or a launchd
  agent (`instance.logon_start` prints the user's; `examples/hylly.service` is the walled one). Its settings are its
  only options. However it runs, it writes `server.log` itself, a line at a time with a size limit (`instance.logged`,
  `instance.write_log`), and says the same on standard error. The walled one restarts by ending with code 75, which its
  unit takes for a restart.
- **Where each runs:** on the server computer, which owns the library's data (*The database*, below). The shops'
  logins are there too, walled off from the server itself. On another computer, `hylly -H HOST` before the command
  names the server, else `HYLLY_REMOTE`, else `[remote] ssh` in its settings, and the command is passed on to it over
  ssh (`remote.py`), saying `Running on HOST.` on standard error; a long job starts there on its own, so closing the
  window doesn't stop it. What's about this computer and its disks stays here (`remote.LOCAL`: `copy`, `backup`,
  `restore`, `demo`, `commands` and `settings`, and `remote.LOCAL_JOBS`: the stop of a copy or a backup, `hylly jobs stop
  copy` or `hylly jobs stop backup`), and so does any command given `hylly --here` before it. `hylly commands --json`
  says where each runs. Which computer that is, only `[remote]` of the settings is read for (`remote.server`), so a
  mistake elsewhere in them stops nothing on its way, and `hylly doctor` runs here when they can't be read at all.
- **How a command ends:** with a code of `exits.TABLE`, each ending an `exits.Exit` with its code and words, which
  `cli.main` says on standard error. What `cli._in_words` turns into one (a shop's or the network's trouble, a disk's,
  the database's) is said in words; anything else is a bug, code 70, with its traceback on standard error (`exits.bug`).
  Standard output has the result only, and with `--json` of the commands in `cli.ONE_OBJECT`, one object however they
  end. A long job's handler returns how its run ended (`cli.Ended`: what it did in counts, how many failed, whether it
  was stopped, sent its own note, or did its work though it ends needing a person), or the `Exit` it ends with carries
  it; its watch (`cli._watched`) makes of that and the exit code the phone's note, if any, and what `hylly jobs` says.

## The parts

Each module belongs to one part, and imports only from the parts its part may use, at the top of a module or inside
a function alike. `src/hylly/parts.py` says so as data, and `tests/test_parts.py` reads every import and holds them to
it. The table below is made from `parts.py`, and a test keeps the two alike.

<!-- parts.py -->
| Part | Modules | May use |
|---|---|---|
| core | `__init__`, `exits`, `paths`, `storage`, `settings`, `rules`, `matching`, `notify`, `instance`, `runs`, `access`, `bell`, `sites`, `shops`, `parts`, `db`, `remote`, `fetching`, `words` | itself only |
| books | `archives`, `meta`, `reader`, `pdfworker` | core |
| logins | `logins` | core |
| records | `snapshot`, `inventory`, `plan`, `curation`, `folders`, `manifest`, `library`, `removed` | core |
| words | `commands`, `effects`, `helppage` | core, records |
| catalog | `catalog`, `lookups` | core, books, logins, records, words |
| shops | `humble`, `storybundle`, `drivethrurpg`, `itchio`, `itchio_survey`, `shopping`, `shoplist`, `shopsession`, `download`, `copies` | core, books, logins, records |
| server | `daemon`, `serve`, `opds`, `dashboard`, `dbpage`, `people`, `reviews`, `reports`, `shelves`, `themes`, `updates`, `periodic` | core, books, records, words, copy |
| copy | `hyllycopy`, `backup`, `dbcopy` | core |
| front | `__main__`, `cli`, `doctor`, `demo` | everything |
<!-- end -->

- **The server can't reach the shops' logins, nor the code that talks to the shops, by any road** (`SERVER_NEVER`):
  not directly, and not through anything it uses. A test follows every import from the server's modules to be sure.
- **The shops ask through one web session** (`shopsession.ShopSession`, in the shops' part, as it needs their logins'
  errors): requests at the shop's pace however many downloads ask, retries, the shop's `Retry-After` waited out by
  every request to it, and errors without addresses. What a shop does differently is said where it makes its session, as the session's options. The lookup
  sources have a session of their own (`lookups.Session`), with budgets kept on disk; both read `Retry-After` with
  `logins.retry_after`. The downloads and the lookup sources' session follow an address a shop or a source gave, or a
  redirect, only over https and only to the internet, never to this computer or the home network (`fetching`).
- **The words** (`commands`, `effects`, `helppage`) are what every command and option is, and the help page made from
  them. They're kept apart from the commands' code (`cli.py`), so that the server can send the help page without the
  code that would download from a shop.
- **Sizes, how long and counts are worded one way** (`words.py`, in the core, so that every part may use it): a size
  in thousands, as disks are sold (1 GB is 1,000,000,000 bytes), how long in minutes up to an hour and a half and then
  in hours, a count with its word in the plural unless it's one (`many`); and a page's `{{name}}` filled in, escaped
  (`fill`), for the help, the login page and the owners' own pages alike.
- **The catalog as it's served** (`library.Catalog`: the last build's `data.js` with your tags and corrections laid
  over it, and what's asked of its titles, the e-reader feed's choice among them too) is in the records' part, so that
  `hylly tag`, `set`, `lookup`, `reviews` and `reports` and the doctor read it as the server shows it without loading
  the server. The feeds for reading apps (`opds`) are the server's: one function for each kind of page, found by its
  address (`opds.PAGES`), each reading of the person's records only what it shows. `serve.py` takes each request: who
  is asking, what they may have, and the library's files.
- **The shops' core** (`shopping`) is what every shop has in common, as every shop is on it: which files to download,
  the jobs and their records, the snapshot. It is in the shops' part, beside the downloads (`download`) whose jobs it
  makes, because it is only ever run with a shop's own code, which talks to the shop. The catalog, which may not reach
  the shops, reads the snapshots it writes by their shape alone (`catalog.shop_items`; Humble's, its orders, by its
  plan: `catalog.humble_items`), and the commands' words take the options every shop has from the table
  (`commands.shop_download`). The free names of a title's folder and files
  (`plan.free_folder`, `plan.free_name`) are in the records' part, as Humble's plan names its folders too. Where the
  shops differ in how a download goes, it's a trait of the shop's in the table (`shops.Shop`: `exact`,
  `link_refreshes`, `beside`, `maker`), or of its titles and files (`Title.under`, the folder its own goes in when that
  isn't its maker's; `File.left`, a reason of the shop's own to leave a file out), or a way of its own that its
  `Library` or its files have and the core asks of a shop that has it (itch.io's: `Library.chosen`, `left_out` and
  `gone`; `File.current`, `File.device` and `File.keep_size`; StoryBundle's: `File.name` empty for a file named only
  in its link, which `shopping.Naming` names by the link its download starts with, `File.rough` for a rounded size,
  `Title.place` for the shop's own order, `Title.record` for what its records keep besides, `Library.refused`, and
  `shopping.select`'s `bundle` and the run's `skip` for its own options; Humble's: `Title.folder`, a title's folder by
  the shop's own plan (`plan.py`), its files then known and recorded by their places,
  `File.fallbacks`, the formats behind a chosen file, `File.url` and `File.ext`, `Title.device` and `Title.why`,
  `Title.found`, the kind its plan found, and on its `Library` its own `snapshot`, `unanswered` and `summary`), never a
  test of which shop it is.
- **The shop list** (`shoplist`, `hylly SHOP list` and `hylly list`, then `hylly SHOP download LIST` and `hylly download
  LIST`: [downloads.md](downloads.md#the-shop-list)) is written once, beside the core, for every shop: its rows are the
  core's (`shopping.select`'s `rows`: every file a download takes or could take, the formats behind a chosen file too,
  with the rules' verdict), and a list's marks go back to the same `select` (`marks`, with what the rules ticked as it
  was written, `picked`, so that only your changes count), so a plain run is a download of the list the rules make,
  never written out, and a list read back unchanged chooses the same. A list that moves a which-file question's mark
  from the guess to one other file (both marks changed from `picked`) answers the question with it, as `hylly decide`
  does (`Selection.answers`, `cli._answer_moved`). Its `list` action, with
  the options of a shop's download that choose titles, and the `LIST` after its download come from the table and the
  download's own parser (`commands.shop_lists`), never by hand. What a list unticks is kept as your edit (`curation`'s
  `left-out`, a field of its own beside `answers` and `catalog`), which a plain run honours (`select`'s `yours`).
- **The kinds are data:** the rules' `[kinds.X]` tables (`kinds.toml`, `rules.Kind`: a kind's names, its device, its
  words, the folders and the itch.io classifications that are it), and nothing else lists them: the settings' lists
  under `[download]` (`settings.section_defaults`), the folders' and the feeds' checks, the corrections
  (`curation.single`), every `--kind` (checked as the command runs, `cli._kinds`), Humble's order of a run
  (`humble.kind_order`) and the library page, which the server sends them to (`library.kinds_table`). A title's kind is
  found one way at every shop (`rules.Rules.kind_of`): the core's `shopping.kind_found` for a download, worked out once
  and with your correction laid over it (`shopping.corrected`, by the catalog id), Humble's plan by every format
  offered, and the catalog by the files downloaded (`catalog.kind_of`). Where a title is best read is one function
  too (`plan.device_for`), which the catalog calls once for every title after it is read (`catalog.device_of`).
- **One list of questions** (`curation.QUESTIONS`, the table `question`): what a download's rules can't settle by
  themselves, found by the shops' core (`shopping.select`: a file gone from its place, and a title's file its shop's
  own rules were unsure of, `File.unsure`) and by a shop's own (`Library.questions`: Humble's items of extras only),
  kept by a run (`curation.keep_questions`, by each question's kind and subject, so never twice), and answered by an
  ordinary correction (`curation.answer`: `file`, or the title's `answers`), which settles it, by `hylly decide` and
  the Server page alike: the records' part has both, so the server answers without the shops' code. The catalog's
  questions (two copies one book, a lookup's near miss, a title gone, a folder's files of an unknown kind) are in the
  same table under the shop `catalog`, found by a complete build (`catalog.questions_of`, plain functions of what the
  build has, so the check tool counts them offline), and answered the same way (the title's `catalog` answers, a field
  apart from the downloads' `answers`), or by a `reattach` or a `forget`; the
  build reads the answers back (`merge_copies`'s pairs, `lookups.Answers`, `folder_items`). A quiet kind (a lookup's
  near match) is kept as any other, but shown with its title rather than on the list. An answered question stays
  on the answered list while its edit stands, and `curation.take_back` takes one answer back alone. A kind of question
  more is a row in `QUESTIONS`, the code that finds it, and where its answer is read; `[download] unsure` says whether
  a run takes the guess or holds the file back.
- **itch.io's survey and claiming** (`itchio_survey`) are a module of their own beside the shop's (`itchio`): what
  the library and the bundles hold, counted before anything is claimed or downloaded, and the bundles' items
  claimed. They use the shop's API session and the answers it keeps about each item's files (`itchio.Lines`,
  `itchio.ask_files`), which its downloads read too, never the other way round; so `itchio` reads like the other
  shops' modules, its API, its items and its `Library`.
- **The records** are what's kept of the library, on disk and in the database: the shops' orders as snapshots
  without their credentials, the download record, your own folders and corrections. The catalog reads them; the shops
  write them. Every shop's download records name the title and the file by the same fields (`item`, `file`, with what
  the shop listed, `listed_md5` and `listed_size`: docs/downloads.md), and each shop's own files are in a folder of
  its own in the library's state folder (`shops.Shop.state`: its snapshots, thinned as each new one is saved, its
  failures file and its working files); what is every shop's stays in the state folder itself (`paths.state_dir`).
- **The copy part** (`hyllycopy`, `backup`, `dbcopy`) needs nothing but the core, and the server uses it only for the
  nightly copies of the database and its settings (`dbcopy`), which go by name and never take a login.

## The logins, and the server's walls

The shops' logins and the lookup sources' keys are in a folder of their own, `logins` in the data folder unless
`HYLLY_LOGINS_DIR` puts it elsewhere (`paths.logins_dir`), and only there. On a Linux
server run by systemd (`examples/hylly.service`), the server sees only the library, Hylly's own folders and the program,
and `InaccessiblePaths=` hides the logins folder from it too, so a flaw in the server couldn't read them even if its code
were changed. The service starts the server itself, as `hyllyd --in-background`, which opens its log itself, inside the
walls, in the state folder they let it write. The periodic jobs' service (`examples/hylly-jobs.service`) has the same
walls, but sees the Inbox, and of the logins folder only the lookup sources' key files, read-only, over an empty tmpfs,
as the Inbox's pass asks the sources about its new books: never the shops' logins.

**What the walled server can write, the owner's own commands mustn't trust.** It writes the data, state and cache
folders, and the library. So:
- `BindReadOnlyPaths=` keeps the whole settings folder read-only for it: `settings.toml`, `rules/`, the themes and
  `secrets/`, which holds the server's own secrets, the alarms' ntfy address and token (`settings.secret_from`), apart
  from the logins it must never see, and `settings.toml` none (one still under `[bell]`, where an older Hylly kept
  it, is never read: `settings.RETIRED` says where it goes now). A service made when everything was in one folder,
  `~/.local/share/Hylly`, keeps `settings.toml`, `rules/` and `secrets/` read-only by `ReadOnlyPaths=` there, and goes
  on working as it is.
- The output of long jobs started from another computer goes to `job-output` in the state folder, which
  `InaccessiblePaths=` hides from it as it hides the logins (all in one, `~/.local/state/hylly/job-output`, outside the
  one folder), in files made new (`O_EXCL|O_NOFOLLOW`): its standard output and its standard error each in one of its
  own, so that the two stay apart on the computer it was typed on too (`remote.as_job`).
- A record's path is followed only if it stays inside the library (`paths.inside_library`, `paths.library_file`; a
  catalog build's `paths.LibraryFiles`, which resolves the library and each folder once, and a file that is a link).
- The server computer's name, from `-H`, `HYLLY_REMOTE` or `[remote] ssh`, must look like a host name, and goes after
  `--`.

Why not a user of its own for the server: Hylly writes people's data, sessions and users readable by their owner
alone (mode 600), and the owner's own commands (`hylly user add`, `hylly people`) write the same files. A second user
would need those shared with a group, which loosens what keeps one person's data from another program. The walls above
already give the server a disk of its own; hiding the logins closed what was left.

## The database

What Hylly keeps that the books can't bring back is in one SQLite file, `hylly.db` in the data folder of the
server computer: what's kept about people, the decisions on reported titles, your tags and corrections, the catalog,
the users and the browsers kept logged in, and the record of what was downloaded and copied in. `db.py` is in the core,
so every part may use it: opening it, the migrations (`src/hylly/migrations/NNNN_*.sql`), `Shape` (a kind of record in
its table), which computer keeps the library's records (`own`), and the nightly copy. Which tables a part writes stays
that part's own. [database.md](database.md) says what is in it and who writes each table, what stays a file and why,
how its steps change the tables, and how it is copied and looked at.

- **One computer owns it:** only Hylly's own code writes it, on the computer it's on, never by hand and never over a
  network share, where SQLite's locks don't hold. So a command typed on another computer runs on the server computer
  (`remote.py`), and `state/database.json` in the library names the database, and the install, that keeps the
  library's records (`db.own`): a command elsewhere that would change one is refused, with where to run it instead.
- **What stays a file:** your settings and rules, the logins and keys, the books and what the shops and lookup sources
  answered, the catalog's own files, the alarms' memory and the lookups' budgets, and the logs:
  [database.md](database.md#what-stays-a-file-and-why) says why each.

## The roles

A library has a few users, mostly family: most only read, and are glad they can't break anything by accident; one
person runs it; someone between may be trusted to fix a title's details. So there are four roles (`access.ROLES`),
each with what the one above it in the table has:

| Role | May |
|---|---|
| borrower | read in the page only: no book file and no reading app; e-books come as pages drawn on the server |
| reader | browse, search, download and read: every new user is one, but the first |
| librarian | change one title at a time, its details and its tags, and undo their own changes; go through reported titles; make shelves for everyone and change their own |
| owner | everything: many titles at once, the suggested tags, everyone's changes |

- **The server decides, not the page:** the page only hides what a role can't use, and every request is checked
  (*Roles* in docs/catalog.md says how). A role is read at every request, not kept with a browser's login, so a lower
  one holds at once, on every device.
- **Every request logs in, this computer's too, and a role is the same wherever it comes from:** where a request
  comes from can't be told for sure (a proxy or a container in front makes everything look local), so it never lets
  anyone in. The first user added is the owner, as the one who sets the library up adds themselves first; without a
  login nothing is answered but the login page and what it needs, and `/healthz`. How the server is reached is a
  choice of setup, not of code: docs/ways-to-run-it.md.
- **Users are made and changed with `hylly user` on the server computer,** never in the page: setting passwords over
  the network is what a small project gets wrong. There's no password recovery: the owner sets a new one.
- **One title** is one entry, a merged title's copies included; a series card counts as several.

## The server's periodic jobs

What the server does now and then, besides answering requests, is in one place, `periodic.py`: the nightly copy of the
database and the settings (`dbcopy.py`), the alarms' look every minute and the daily summary (`bell.Watch.tick`), the
alarms the server held back (`bell.Watch.tell_of_held`), its address at home for the pages (`access.look_at_adapters`),
the update check as it starts (`updates.say`), and the Inbox's pass once a minute (`hylly internal inbox`). Each is one
entry of `periodic.JOBS`: its name, how often it's due, its function, and its kind: the server's own, a long one, or one
that needs what's outside a walled server's walls.

- **The server runs them by default,** once a minute in a thread of its own, and each job in a thread of its own, so
  a slow one holds up no other and one still running isn't started again. A desktop has no cron, and a container
  runs one process, so inside is the right default.
- **`jobs = "external"` under `[server]`** leaves the nightly copy and the alarms' look to `hylly server jobs`, which
  runs what's due once and ends, for a systemd timer (`examples/hylly-jobs.timer`, walled in as the server is), cron,
  Task Scheduler or a container's own scheduler. It runs a quick job (the alarms) itself, and starts a long one (the
  nightly copy) as a process of its own that it doesn't wait for (`hylly internal server-job NAME`), so the alarms go on
  every minute while a copy takes its time, and the copy isn't cut short when the run that started it ends. A long job
  has a time limit (the nightly copy two hours), after which its process ends itself; how it ended is kept beside its
  lock, and a failure, a bug or the limit is said in `bell.log` as a long job's note. A copy late by a day and a half is
  an alarm of its own (`bell.Watch.check_copies`). The server
  reads the setting each round. What only the running server holds in its memory, its own held alarms and its address
  at home, and the update check of what it runs on, stay with it. With the setting as it comes, `hylly server jobs`
  runs nothing and ends well, so a timer left on isn't a failure every minute.
- **One run of each job at a time:** each that may run elsewhere has a lock of its own, `server-jobs/<job>.lock` in the
  state folder, which the server takes too; one still running from before is left to it.
- **What each keeps stays where it was:** tonight's copy is its day's folder in `state/db-backup`; the alarms' memory
  is `bell.json`, and `bell-jobs.json` for `hylly server jobs`, as a running server holds `bell.json` in memory and
  would write over another program's changes. Long jobs' notes keep `bell-jobs.json` too, so one program at a time
  reads, changes and writes it (`bell.one_writer`). The day's summary goes out once, from whichever of the two sends it
  first. When each job last ran: the server's memory, or `server-jobs/<job>.json` for `hylly server jobs`, each run of
  which is a process of its own, read once its lock is taken and written back under it.
- **The Inbox** (`[inbox]` in the settings: one folder, or several, whose books go each to the folder of yours its kind
  belongs in, `folders.placed`) is a long job that needs what a walled server can't see,
  the Inbox's folders and the lookups' keys (`outside_walls`). It never touches the library, by any spelling of it
  (`copies.Guard`: the folders' identities on the disk, so `\\?\`, another share name, a link or a bind mount are told
  for what they are), nor anything outside the Inbox: it follows no link or junction in it, and an original goes only
  once its copy in the library and it, both read again then, are the same. A server that isn't walled starts each pass itself; a
  walled one leaves it to `hylly server jobs` on its timer, outside its walls, which runs it and nothing else when
  `jobs` is as it comes, and the job "inbox watch" says once in the server's log when nothing has run it for ten
  minutes, as `hylly doctor` does. A pass is a process of its own, `hylly internal inbox` (in `cli.py`, with its engine in
  `copies.py`), as copying, the lookups and the catalog are no part of the server's. It starts only when there is
  anything to do (`folders.waiting`, its `idle`), and holds the `add` job's lock, so a `hylly add` typed meanwhile is
  refused with exit code 4, and a pass leaves the Inboxes to one that runs. Its state is in the library's `state/inbox.json`: what each look saw (a book is taken once it has stayed
  the same for two minutes), the lookups that wait for a later pass, what was said once, and what each pass did, for
  the daily summary. The catalog build asks the lookup sources about its new books only (`catalog.build`'s `ask`).
- **A job that fails** is said in one line and tried again when it's next due; it stops neither the others nor the
  server. "The server is down" is no job, as a server can't say it isn't there: that's systemd's `OnFailure=` or a
  container's health check of `/healthz`.

## HyllyCopy, and why not rsync or robocopy

`hylly copy` and `hylly backup` copy with an engine of Hylly's own (`hyllycopy.py`), in Python, with no new
dependency. rsync and robocopy are good at moving bytes, but neither does what a copy left running for days needs:

| | rsync | robocopy | HyllyCopy |
|---|---|---|---|
| Runs on | Linux and macOS; on Windows only through WSL or a port | Windows only | all three |
| A big file cut off part-way | `--partial` keeps it, `--append-verify` goes on from it | its restartable mode, slowly | its part, kept in `.hyllycopy` on the target, compared with the source and gone on from |
| Checks the copy | checksums the transfer; `-c` compares whole files, slowly | by size and time only | a checksum while reading, and a read-back past the system's cache |
| Can delete | `--delete` | `/MIR`, `/PURGE`, `/MOV` | never: the source is only read, and nothing is overwritten |
| A share that goes away | exits; a rerun goes on | retries, then the next file | waits, goes on, and holds the job with a note |
| Keeps the computer awake, notifies, reports | no | no (a log file) | yes |

Behind HyllyCopy, either would have meant reading three tools' output and still adding the checks, the waking and the
notes. It leans on what exists where that's the right tool: each system's own way to stay awake, and the usual
checksum lists (`.sfv`, `.md5`, and a manifest any `sha256sum` can check).

## Adding to it

- A new module goes in a part in `parts.py`; the test says so if it's forgotten.
- A new command goes in `commands.build_parser`, under what it's for in `commands.AREAS`, and each of its actions
  gets a row in `effects.EFFECTS` (a test says so if one is missing). `hylly --help`, `hylly commands`, and the help
  page's reference and its lists of commands (`helppage.command_lists`) are made from those. Its other facts are in a
  table each, kept by the module that uses them, and `tests/test_programs.py` holds them to each other for every
  command line the parser takes (each action, with a dry run, `--json` and a LIST): `runs.JOBS`, the jobs, one run of
  each at a time, with what each is called and how one that has hung is ended (`hylly jobs stop NAME` takes them);
  `cli.job_of`, the job a run holds, the one its row of `effects.EFFECTS` names (none for a dry run, nor for the few
  that take their locks themselves), which makes it a watched run with a phone note and a record in `hylly jobs`, and,
  passed on to the server computer, one started there on its own; `cli.HANDLERS`, the function that runs each
  command; `cli.NEEDS_THE_LIBRARY`, those whose library is checked before they start; `cli.asks_a_shop`, a dry run that
  reads a shop's site, which waits its turn; `cli.STARTED_APART`, those that hold no job and, passed on, start there on
  their own all the same; and `remote.LOCAL` and `remote.LOCAL_JOBS`, what runs on the computer it's typed on, and
  whose stop does. `cli.ONE_OBJECT`, those whose `--json` is one object however they end, is held to the parser by
  `tests/test_docs_commands.py` and `tests/test_assistants.py`.
- A new periodic job of the server's is one entry in `periodic.JOBS`, with its interval and its function; a job that
  keeps what only the running server holds is `server_only`, so that `hylly server jobs` leaves it alone, one that
  takes long is `long`, so that `hylly server jobs` starts it on its own and the alarms needn't wait for it, and one that
  needs what a walled server can't see is `outside_walls`, so that the timer runs it beside such a server.
- If a part needs another it may not use, move the shared piece down into a part both may use, as `sites.py` (the
  sites' names, and the lookup sources' rows), `shops.py` (the shops' names and facts), `manifest.py` (the download
  record) and `helppage.py` (the help page) were moved.
- **The code's style is the file's own:** comments and prose wrap at 120 characters, and a long string or call may
  run past it; the lint rules are `[tool.ruff]`'s in `pyproject.toml` (ruff's long-standing ones, and two more the
  code keeps), which `uv run ruff check .` and CI's lint job check. No formatter is run over the code: change the
  lines your change needs, and never reformat a whole file, which would bury the change in a diff of every line. Line endings are LF, but CRLF for the Windows scripts, whatever a
  computer's git settings say (`.gitattributes`).

## What stays put

From 0.1 on, others depend on these: owners type the feeds' addresses into their devices and write scripts against the
exit codes, and the authors of themes, rules and lookup sources write to the files' and the classes' shapes. So they
stay as they are:

- **The settings' names**, the sections and keys of `settings.toml` (`settings.example.toml` has them all). One that
  Hylly no longer reads, a renamed one among them, is left out and told, with what took its place (`settings.RETIRED`:
  `public_address` under `[server]` is `address`, say), and `hylly settings show` marks it: never dropped without a
  word.
- **A step of the database, once released** (`src/hylly/migrations/NNNN_*.sql`): a database takes each step once, by
  its number (`PRAGMA user_version`), so a change is a step of its own, after the others. A database a newer Hylly has
  taken further is left alone (`db.Newer`).
- **The feeds' addresses:** `/opds` and `/ereader/opds`, and the personal `/me/opds` and `/me/ereader/opds`
  (`serve.Handler.FEED_PATHS`). What is under them, a reading app reaches by the feeds' own links.
- **The exit codes** (`exits.TABLE`, in [the playbooks](playbooks.md#exit-codes) and in `hylly commands --json`): a
  code keeps its meaning, and a new one may come.
- **What `--json` gives:** each result of one object (`cli.ONE_OBJECT`) has `command`, `exit_code`, `meaning` and
  `message`, and the command's own fields beside them. A field keeps its name and what it holds, and new ones may come,
  so a script reads the fields it needs and passes over the rest. `hylly commands --json` says the version.
- **A title's id**, which your tags and corrections go by, made the same way in every version: the shop's letter and
  its id there for a shop's title (`curation.catalog_id`), and for one of your folders' made from its first file's path,
  in one Unicode form and lower case (`catalog.folder_id`). So moving or renaming that file gives a new id, and
  `hylly catalog` then moves your edits to it ([Your own changes](catalog.md#your-own-changes-curationpy)).
- **The theme files' keys** (`themes.KEYS`): a key that comes later is filled in for a file without it
  (`themes.ADDED`), so a theme made for one version keeps working in the next; if what is filled in fails the contrast
  check, the message names the key to set in the file.
- **The rules files' tables and keys** (`rules.SPECS`, [rules.md](rules.md)), and the four ways a file of yours
  changes the shipped ones. A table no longer read is refused, with what took its place (`rules.RETIRED`), and let by
  when it is left empty, as an older copy of a shipped file has it.
- **What a lookup source's author writes** ([lookups.md](lookups.md#writing-a-source)): its row in `sites.LOOKUPS`,
  its module, `src/hylly/lookups/<id>.py`, and its class, a `lookups.Source`: the attributes, and what `find()` is
  given (a `Session` and a `Query`) and gives back.

Not promised: the addresses under `/api/`, which are Hylly's own pages' and change with them; the database's tables,
which a new step changes; and the rest of the code, the names of its other modules among it.
