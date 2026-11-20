# The database: what Hylly keeps, and why it's built so

What Hylly keeps that the books can't bring back is in one SQLite file, `hylly.db` in the data folder of the computer
that keeps the library's records, the server computer (`db.file()`; `hylly settings places` says where that folder
is). This page is for those who work on the code: what is in the database and who writes it, what stays a file and
why, how the tables change from one version to the next, and how the database is copied and looked at. For using it,
the help's *The database* is the place.

## Why a database, and why SQLite

- **SQLite as Python ships it (`sqlite3`), and the SQL written out.** No package to add, and no other program to
  update on each of the three systems. A database server such as PostgreSQL would want running, securing and backups
  of its own; an ORM would put a layer between the code and its SQL, which is what a reader of the code should see,
  and would be a heavy dependency.
- **What it brings:** rules no bug gets past, as the tables keep them (`STRICT` tables, a role one of four, a review's
  stars 1 to 5, a list holding a title once, what's kept as JSON valid JSON); a change that lands completely or not at
  all, in one transaction; and questions across everything in SQL (the Database page, below).
- **What it costs:** nothing else holds your tags and corrections, the users, or what everyone has read. So its
  nightly copy and `hylly backup` matter most (below).

## One computer keeps the records

- **Only Hylly's own code writes it, on the computer it's on:** the server and the commands run there, each a program
  of its own, opening the one file. Never by hand, and never over a network share, where SQLite's locks don't hold. A
  command typed on another computer is passed on to the server computer over ssh (`remote.py`, `[remote]` in that
  computer's settings), so every job opens the database on that computer's own disk.
- **Why commands don't go through the web server:** each would want an endpoint, and downloads would bring the shops'
  code into the server, which its walls are there to keep out
  ([architecture.md](architecture.md#the-logins-and-the-servers-walls)).
- **In the data folder, on the computer's own disk, never on the library's:** that disk's file system may be anything
  the server can read (a USB disk formatted for another system, say), where SQLite can't count on its locks and its
  flushes. Only its owner may read the file.
- **The library's marker:** `state/database.json` in the library (`db.owner_file`) says which database holds its
  records, by the id of that database's `library` row, and the install that keeps it, by a random token kept beside
  the database (`records-token` in the data folder, `db.token_file`), never in it. `db.own` checks it before anything
  changes a record, and once checked, a check is one look at the marker's file, read again only when it has changed.
- **What the marker keeps apart:** a command on another computer that would change a record is refused
  (`db.NotHere`), with where to run it instead; so is a copy of `hylly.db` in another settings folder or on another
  computer, which has the id but not the token, as two databases must never keep the records apart, each missing the
  other's changes. A program still running when another database takes the records on notices at its next look, and
  from then on keeps no change (`db.Moved`), saying so once in its log and as an alarm.
- **Who starts:** a library with no marker is taken on by the first database to work on it. The marker is made by an
  exclusive create and read back, so of two computers starting on a new library at once only the one it names goes on.
  A folder without a marker is taken as this database's library, moved or restored, only while the place it was can be
  read and has no marker there now: a disk unplugged might leave another library in its place.
- **Moving the records on purpose** is one command, `hylly server take-over`, which asks first and rewrites the marker;
  a restore does it itself. `hylly doctor`'s `records` check says when this computer's database has the library's
  records but the marker names another install. Which jobs the library's own locks still decide for, beside this, is
  in `runs.py`.

## How it's opened

- **One `db.Database` per file for the whole program** (`db.database()`), with one connection, used by one thread at a
  time. `Database.write()` is one transaction that holds the database's write lock (another program's write waits for
  it), whole or not at all; inside another, it's part of that one. `Database.read()` is the database as it is at one
  moment, however many questions are asked of it.
- **Each connection** (`db.connect`): a writer waits up to `db.BUSY_SECONDS` for another; `synchronous = FULL`, so
  that every change is on the disk before it's done; `foreign_keys = ON`; and `secure_delete = ON`, so that what's
  deleted is overwritten rather than left in the file's free pages: a review's words, once removed, are gone from the
  file itself, and `Database.checkpoint` empties the WAL after them. It needs SQLite 3.37 or newer
  (`db.OLDEST_SQLITE`), for its `STRICT` tables.
- **WAL,** so that one program's readers never wait for another's writer (the server's reads go on through a
  command's write): set as the program first opens the file, once the steps are taken (`Database._connection`), so
  that a database left alone keeps its header as it was. Within one program they do, as its one connection takes one
  thread's question at a time: so the server writes only when something changes (a browser's login is read first, and
  written only on the day its end moves: `access.Sessions.renew`).
- **Long jobs keep their transactions short,** so a request that writes waits a moment at most: a download keeps each
  file's record as the file is done (`manifest.Manifest.add`), and a catalog build works out everything first and keeps
  its titles in one transaction at its end, read back and checked (`catalog.keep`).
- **A dry run changes nothing:** every database a command with `--dry-run` opens is read into a copy in memory
  (`db.start_dry_run`, `db.scratch`), brought up to date there, and what the command would write goes nowhere.

## The tables, and who writes each

Every table is made by the steps in `src/hylly/migrations/` (below). Which tables a part writes stays that part's own:

| Tables | What they hold | Written by |
|---|---|---|
| `person`, `place`, `finished`, `guide_seen`, `list`, `list_entry`, `bookmark`, `shelf`, `review`, `report` | what's kept about each person: where they are in a title (the latest places and finished marks of each, `people.HISTORY_KEPT`), their lists, bookmarks and shelves, their reviews as they now stand (`reviews.step`), the titles they reported | `people.py`; a user removed or renamed, `access.py` |
| `report_decision` | the library's word on a reported title | `reports.py`; a user renamed, `access.py`, or erased, `people.py` |
| `library` | the library whose records these are, by the id its marker names, and what it opens with | `db.py`, `curation.py`, `shelves.py` |
| `edit`, `edit_title` | every change of yours, in order, and the titles each was on: replayed, they give each title's tags and corrections (`curation.Edits`) | `curation.py`; a user renamed, `access.py`, or erased, `people.py` |
| `shared_shelf` | the shelves for everyone (`shelves.Shared`) | `shelves.py`; a user renamed, `access.py`, or erased, `people.py` |
| `suggestion` | the suggested tags waiting for a yes or a no | `curation.py` |
| `question` | what the rules couldn't settle (`curation.QUESTIONS`), for `hylly decide` | `curation.py`; a user renamed, `access.py`, or erased, `people.py` |
| `title`, `title_file`, `title_alias`, `catalog_build`, `seen_title`, `seen_file` | the catalog: each title as the server gets it, its files, a merged title's other ids, each build, and when each title first came | `catalog.py` |
| `account`, `browser_login` | who may log in, with their role (`access.ROLES`) and only a salted hash of their password; the browsers kept logged in, by a digest of their token alone | `access.py` |
| `download`, `copied`, and the view `downloaded` | each file a shop's download brought in, and each book `hylly add` copied in, every record kept in order; the view gives each path's newest record | `manifest.py` |
| `removed` | each file the Server page removed into `_Removed` (To decide's *Keep only this one* and *Remove both*, in *Compare the files*): where it was and where it went, the answer it gave a shop's title so that no download fetches it again, and whether it was put back or emptied since; `hylly verify` and `hylly add` read it, so that a file you removed isn't one missing, nor a copy of your own folders' copied in again | `removed.py` |

- **A user renamed** (`access.rename_user`) is renamed everywhere in one transaction: their account, what's kept about
  them, the records that name them (`access.NAMED_BY`: `edit`, `shared_shelf`, `report_decision`) and their answers in
  `question`. So a later user given the old name gets none of it.
- **A user removed** keeps their name while what's kept about them is kept aside (a row of `person` with `removed`, one
  of its own for someone the records name with nothing else kept): no new or renamed user may take it, as those records
  would become theirs. **Erased for good** (`people.erase`), and with no user of that name, the records that name them
  say `access.FORMER` instead, in the same transaction, and the name is free.
- **A record of fields** is kept by its `db.Shape`: each field in a column of its own, of the type it should be, and
  anything else (a field of another type among them) in the column `more`, as JSON, so a record comes back exactly as
  it went in, and its columns can still be asked about in SQL.
- **What comes whole** (a title as the library page gets it, a download's record, a review) is kept as JSON in a column
  `body`, with columns made from it for SQL (a title's `name` and `kind`, a download's `path` and `size`): what's read
  from it and what's written stay the same record.
- **A title is named by its catalog id** (`title`), with its name and first file beside it where what people did needs
  them (`title_t`, `title_f`), so that a title whose id has changed is still found (`matching.Titles`). What people did
  with a title isn't a foreign key to the catalog: a title may leave the catalog and return, and what was kept of it
  stays in between.
- **A new shop needs no table:** `download` takes any shop's records, which name the title and the file by the same
  fields for every shop ([downloads.md](downloads.md#the-record)).

Not promised to anyone outside: the tables change with a new step ([What stays put](architecture.md#what-stays-put)).

## What stays a file, and why

- **Your settings and rules files:** you write them by hand.
- **Logins at the shops and keys for the lookup sources,** in a folder of their own, which a walled server can't
  see. **The server's own secrets** too, the alarms' ntfy address and token, each in a file of its own in `secrets/`
  in the settings folder, which it may read: systemd's `LoadCredential=` and Docker's `*_FILE` give them the same way.
- **The books, and the shops' and lookups' answers as received** (the snapshots in each shop's folder in `state/`, the
  lookups' answers in `state/lookups`): kept exactly as they arrived, so that a later version can read more out of
  them; the database has only what Hylly has taken from them so far.
- **The catalog's files** (`catalog/data.js`, the covers): made by the catalog build, `data.js` from the database and
  the covers from the books, and safe to delete and make again (`hylly catalog --page` makes the page's data again
  from the database alone). The server reads `data.js` and lays your tags and corrections over it, from the database,
  whenever either changes; the library page comes only from the server, and its data.js is the server's own, written
  out as one JSON value (`library.Catalog.page_script`), never the file as it is. The page loads it in one go, which
  keeps it fast, and nothing else holds a copy of the tags to fall behind.
- **The alarms' memory** (`bell.json`) **and the lookups' budgets:** counters written as things happen (a budget gets a
  line before each request is sent), which must keep working if the database itself is the trouble.
- **The logs:** other programs read the access log (GoAccess, fail2ban).

## Steps: how the tables change

- **Each step is a numbered SQL file,** `src/hylly/migrations/NNNN_*.sql`, numbered from 1 with none missing
  (`db.migrations`). A program that opens the database takes the steps it hasn't had yet, in order (`db.migrate`), each
  in one transaction holding the write lock, and only if no other program has taken it meanwhile: two programs opening
  it at once take each step once. `PRAGMA user_version` holds the number of the last step taken, and
  `PRAGMA application_id` (`db.APPLICATION_ID`) marks the file as Hylly's.
- **A copy first:** before a step is taken on a database in use, a copy of it goes beside it, made by SQLite's backup
  API: `hylly.before-NNNN.db`, never written over (`db.copy_before`). An older Hylly can't open the database after the
  step, so that copy is the way back. A step that leaves much of the file free gives the room back (`VACUUM`).
- **A newer version's database is left alone** (`db.Newer`): nothing is read from it or written to it, nobody can log
  in, and an alarm says so until Hylly is updated.
- **The steps so far:** the first makes the tables; the second names every shop's download records by the same fields
  and lets the table take any shop's; the third gives StoryBundle's old records the kind `book`; the fourth makes the
  table of questions; the fifth the table of the files removed into `_Removed`.

**Adding a step:**
1. A new file in `src/hylly/migrations/`, with the next number. A step once released never changes: a database takes
   each step once, by its number, so a change is a step of its own, after the others.
2. Write it so that done again it changes nothing, as the third to fifth are: `CREATE TABLE IF NOT EXISTS`, and an
   `UPDATE` whose `WHERE` matches only what's still to change. Data that moves within the database moves in the step's
   own SQL.
3. A column that holds a secret (a hash, a digest) goes in `db.SECRET`, so that the Database page and the plain export
   beside each nightly copy leave it out.
4. Tests beside the other steps' in `tests/test_curation.py`: a new database made through every step, and one of the
   step before brought up to it after its copy, with its rows as they were
   (`test_the_fourth_step_makes_the_questions_table_after_a_copy_and_done_again_changes_nothing` is one).
5. The code that writes the new table is its part's own, as in the table above, and the docs that name the tables
   (this page, and [catalog.md](catalog.md) or [downloads.md](downloads.md) where the step is theirs) say what changed.

## The nightly copy

- **When:** one of the server's periodic jobs ([architecture.md](architecture.md#the-servers-periodic-jobs):
  `periodic.JOBS`, `dbcopy.py`), at 3 at night or as soon after it as the library is there (`db.BACKUP_FROM_HOUR`), or
  `hylly server jobs` on a timer with `jobs = "external"` under `[server]`.
- **How** (`db.nightly`): SQLite's backup API takes the copy, whole and consistent even while the server writes,
  never by copying the file itself. It's made beside the database, switched from WAL to SQLite's ordinary journal so
  that any program can open it, even from a place it may only read, and only then moved onto the library's disk, into
  `state/db-backup/<day>/`. With its `hylly.db` go a `<table>.jsonl` for every table, a row on each line, which any
  text editor opens, with the secret columns (`db.SECRET`) left out; `check.json`, the result of SQLite's
  `integrity_check` on the copy, a count of rows per table, and the library it belongs to; and a `README.txt` that
  tells how to return to that night.
- **How long each stays:** until seven newer ones are there (`db.BACKUPS_KEPT`), or until it's more than 14 days older
  than the copy just made (`db.NIGHTS_DAYS`), and only once a copy is made whose check is good: after one whose check
  isn't, none is removed, and the latest copy is never removed, however old.
- **Whose:** copies are made only of the database the marker names, and only those copies are counted and thinned;
  a copy of any other database found in the folder is left alone, and mentioned once.
- **Who can read them:** on Linux and macOS, where the disk keeps rights, each copy's folder and files are their
  owner's only, as a copy's `hylly.db` has the password hashes. `hylly doctor`'s `database` check says when the last
  copy was made and whether its check was good, and `state-copies` when others can read them.
- **When it fails:** a copy that fails is tried again every hour, and one whose check isn't good is made again the next
  night; either raises one alarm, and the first good copy after says so (`dbcopy.py`). No copy made for a day and a
  half, whatever the reason (nothing running the server's jobs, say), is an alarm of its own
  (`bell.Watch.check_copies`).

## Backups and a restore

- **`hylly backup`** makes today's copy again just before its zip, so that the zip has the records as they are, and
  takes it with the rest of the library's `state` folder (`backup.fresh_records`). On another computer, with the
  server named, it asks the server for that fresh copy; without one, the zip has the last nightly copy, and the backup
  says so.
- **`hylly restore`** brings a library back into a new folder, and makes its records this computer's database when it
  has none yet, tied to this install as `hylly server take-over` would.
- **One night by hand:** each copy's README says how: stop the server, put the copy's `hylly.db` in the data folder in
  place of the one there (keeping that one, and taking away the `hylly.db-wal` and `hylly.db-shm` beside it), and start
  the server.
- **`hylly people export`** gives what's kept about one person as JSON, for that person, apart from any backup.

## Looking inside

- **The Database page,** `/database` (`dbpage.py`), linked from the Server page and, like it, for an owner only, on any
  device the owner logs in on: every table a page at a time, how each is made, and questions of your own in SQL, with
  examples to learn on (`dbpage.EXAMPLES`). It can't write, by three guards: the connection is opened read-only,
  `PRAGMA query_only` is on, and SQLite's own authorizer refuses all but reading. The secret columns read as nothing,
  and a question stops after 3 seconds and shows at most 500 rows (`dbpage.SECONDS`, `dbpage.ROWS`).
- **A nightly copy** opens in any program that reads SQLite, DB Browser for SQLite say. Nothing done to it reaches the
  server. Never open the live file with another program.
