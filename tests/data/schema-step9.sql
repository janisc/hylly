-- The tables as the nine steps of Hylly's test versions, before its first release, left them: what the databases
-- made by those versions have, step 9 being the last. Made once from those steps' files, as SQLite keeps each
-- table, index and view (sqlite_schema, in the order made); its SQL is never edited.
-- tests/test_curation.py holds src/hylly/migrations/0001_start.sql to it, so that the first step can't drift from
-- what those databases have, which this version takes on as they are (db.standing).

CREATE TABLE person (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    since TEXT NOT NULL DEFAULT '',
    new_seen TEXT CHECK (new_seen IS NULL OR json_valid(new_seen)),  -- how far they've been shown what's new
    look TEXT NOT NULL DEFAULT '',     -- the theme they picked: '' for the library's own
    opens TEXT NOT NULL DEFAULT '',    -- the shelf they open with
    removed TEXT,                      -- when their account was removed: kept aside, and no longer theirs
    rev INTEGER NOT NULL DEFAULT 0     -- one more at each change of theirs, so the server sees one a command made
, own_look TEXT CHECK (own_look IS NULL OR json_valid(own_look))) STRICT;

CREATE UNIQUE INDEX person_by_name ON person (name) WHERE removed IS NULL;

CREATE TABLE place (  -- every place reached, the latest by time is where they are
    seq INTEGER PRIMARY KEY,
    person INTEGER NOT NULL REFERENCES person (id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    title_t TEXT,
    title_f TEXT,
    at TEXT CHECK (at IS NULL OR json_valid(at)),  -- a page, or a spot in an e-book
    pct REAL,
    file TEXT,
    device TEXT,
    local_time TEXT,
    time REAL,
    pages INTEGER,  -- a comic's or a PDF's
    note TEXT,      -- a book's chapter
    more TEXT CHECK (more IS NULL OR json_valid(more))
) STRICT;

CREATE INDEX place_by_title ON place (person, title, time);

CREATE TABLE finished (  -- every change of the mark: done, or no longer
    seq INTEGER PRIMARY KEY,
    person INTEGER NOT NULL REFERENCES person (id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    title_t TEXT,
    title_f TEXT,
    done INTEGER CHECK (done IN (0, 1)),
    local_time TEXT,
    time REAL,
    more TEXT CHECK (more IS NULL OR json_valid(more))
) STRICT;

CREATE INDEX finished_by_title ON finished (person, title);

CREATE TABLE guide_seen (  -- the reader's first-time guides they've had
    person INTEGER NOT NULL REFERENCES person (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('book', 'pages')),
    local_time TEXT,
    time REAL,
    more TEXT CHECK (more IS NULL OR json_valid(more)),
    PRIMARY KEY (person, kind)
) STRICT;

CREATE TABLE list (  -- the reading list, and their own
    person INTEGER NOT NULL REFERENCES person (id) ON DELETE CASCADE,
    id TEXT NOT NULL,       -- 'reading', or the id their device gave it
    name TEXT,              -- none for the reading list
    made INTEGER NOT NULL,  -- their order: when made, or brought back
    deleted TEXT, apps INTEGER NOT NULL DEFAULT 0 CHECK (apps IN (0, 1)), featured INTEGER NOT NULL DEFAULT 0 CHECK (featured IN (0, 1) AND (featured = 0 OR apps = 1)),           -- when deleted: it can be brought back
    PRIMARY KEY (person, id),
    CHECK ((id = 'reading') = (name IS NULL))
) STRICT;

CREATE TABLE list_entry (
    person INTEGER NOT NULL,
    list TEXT NOT NULL,
    title TEXT NOT NULL,
    title_t TEXT,
    title_f TEXT,
    added TEXT,
    position INTEGER NOT NULL,
    more TEXT CHECK (more IS NULL OR json_valid(more)),
    PRIMARY KEY (person, list, title),  -- a list holds a title once
    FOREIGN KEY (person, list) REFERENCES list (person, id) ON DELETE CASCADE
) STRICT;

CREATE TABLE bookmark (
    seq INTEGER PRIMARY KEY,  -- their order: the oldest first
    person INTEGER NOT NULL REFERENCES person (id) ON DELETE CASCADE,
    key TEXT NOT NULL,        -- made where it was marked, so two devices can't overwrite each other's
    title TEXT NOT NULL,
    title_t TEXT,
    title_f TEXT,
    why TEXT,                 -- inspiring, clever, useful, funny or infuriating
    at TEXT CHECK (at IS NULL OR json_valid(at)),
    pct REAL,
    file TEXT,
    pages INTEGER,
    page INTEGER,
    part INTEGER,
    inside REAL,
    note TEXT,
    text TEXT,
    word TEXT,
    local_time TEXT,
    time REAL,
    more TEXT CHECK (more IS NULL OR json_valid(more)),
    UNIQUE (person, key)
) STRICT;

CREATE TABLE shelf (  -- their own shelves: the filters, as the page sends them
    seq INTEGER PRIMARY KEY,
    person INTEGER NOT NULL REFERENCES person (id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    body TEXT NOT NULL CHECK (json_valid(body)),
    UNIQUE (person, id)
) STRICT;

CREATE TABLE review (  -- what they've said of a title, as it now stands (reviews.step)
    seq INTEGER PRIMARY KEY,
    person INTEGER NOT NULL REFERENCES person (id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL CHECK (json_valid(body)),
    stars INTEGER GENERATED ALWAYS AS (json_extract(body, '$.stars')) VIRTUAL,
    state TEXT GENERATED ALWAYS AS (json_extract(body, '$.state')) VIRTUAL,
    UNIQUE (person, title),
    CHECK (json_extract(body, '$.stars') IS NULL OR json_extract(body, '$.stars') BETWEEN 1 AND 5)
) STRICT;

CREATE TABLE report (  -- what's wrong with a title's file, in their words: the latest of theirs on each title
    seq INTEGER PRIMARY KEY,
    person INTEGER NOT NULL REFERENCES person (id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    title_t TEXT,
    title_f TEXT,
    reasons TEXT CHECK (reasons IS NULL OR json_valid(reasons)),
    comment TEXT,
    local_time TEXT,
    time REAL,
    more TEXT CHECK (more IS NULL OR json_valid(more)),
    UNIQUE (person, title)
) STRICT;

CREATE TABLE report_decision (  -- the library's word on a reported title: the latest counts
    seq INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    title_t TEXT,
    title_f TEXT,
    decision TEXT NOT NULL CHECK (decision IN ('better', 'fine')),
    decided_by TEXT,
    upto REAL,  -- the newest report it covers
    local_time TEXT,
    time REAL,
    more TEXT CHECK (more IS NULL OR json_valid(more))
) STRICT;

CREATE INDEX report_decision_by_title ON report_decision (title, seq);

CREATE TABLE library (  -- the library whose records this database keeps: state/database.json in it names the same id
    id TEXT PRIMARY KEY,
    root TEXT NOT NULL,      -- where it was, on this computer
    since TEXT NOT NULL,
    opens_with TEXT NOT NULL DEFAULT '',  -- the shelf for everyone the library opens with
    suggestions_made TEXT NOT NULL DEFAULT ''  -- when the suggestions waiting were found
) STRICT;

CREATE TABLE edit (  -- every change of yours, in order: replayed, they give each title's tags and corrections (curation.Edits)
    seq INTEGER PRIMARY KEY,
    at TEXT,
    who TEXT,
    field TEXT,   -- tag; kind, ereader, format; title, authors, series, number, year, publisher. None for a reattach
    op TEXT,      -- add, remove, decline, undecline; set, clear; reattach
    value TEXT CHECK (value IS NULL OR json_valid(value)),  -- the tag, or what was set (authors: a list)
    was TEXT CHECK (was IS NULL OR json_valid(was)),        -- each title's value before, for an exact undo
    ids TEXT CHECK (ids IS NULL OR json_valid(ids)),        -- a reattach: each old id, and the new one
    titles INTEGER,  -- how many titles it was on (edit_title), or none for a change without titles
    more TEXT CHECK (more IS NULL OR json_valid(more))
) STRICT;

CREATE TABLE edit_title (  -- the titles each change was on
    edit INTEGER NOT NULL REFERENCES edit (seq) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    title_t TEXT,
    title_f TEXT,
    more TEXT CHECK (more IS NULL OR json_valid(more)),
    PRIMARY KEY (edit, position)
) STRICT;

CREATE INDEX edit_title_by_title ON edit_title (title);

CREATE TABLE shared_shelf (  -- the shelves for everyone (shelves.Shared)
    seq INTEGER PRIMARY KEY,  -- their order: the order made
    id TEXT NOT NULL UNIQUE,
    body TEXT NOT NULL CHECK (json_valid(body)),
    made_by TEXT NOT NULL DEFAULT ''  -- who made it
) STRICT;

CREATE TABLE suggestion (  -- the suggested tags waiting for a yes or a no, as last found (curation.find_suggestions)
    seq INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    title_t TEXT,
    tags TEXT NOT NULL CHECK (json_valid(tags)),  -- each tag suggested, with why
    more TEXT CHECK (more IS NULL OR json_valid(more))
) STRICT;

CREATE TABLE title (  -- every title, as the page gets it
    id TEXT PRIMARY KEY,
    position INTEGER NOT NULL UNIQUE,  -- the page's order: by series, then number, then title
    body TEXT NOT NULL CHECK (json_valid(body)),  -- every field, as data.js has it
    name ANY GENERATED ALWAYS AS (json_extract(body, '$.t')) VIRTUAL,
    kind ANY GENERATED ALWAYS AS (json_extract(body, '$.k')) VIRTUAL,  -- book, tech, comic, manual, rpg
    source ANY GENERATED ALWAYS AS (json_extract(body, '$.src')) VIRTUAL,  -- humble, storybundle, drivethrurpg, itchio, or a folder of yours
    bundle ANY GENERATED ALWAYS AS (json_extract(body, '$.b')) VIRTUAL,
    bought ANY GENERATED ALWAYS AS (json_extract(body, '$.dt')) VIRTUAL,
    authors ANY GENERATED ALWAYS AS (json_extract(body, '$.a')) VIRTUAL,  -- a JSON list
    series ANY GENERATED ALWAYS AS (json_extract(body, '$.s')) VIRTUAL,
    number ANY GENERATED ALWAYS AS (json_extract(body, '$.n')) VIRTUAL,
    year ANY GENERATED ALWAYS AS (json_extract(body, '$.y')) VIRTUAL,
    publisher ANY GENERATED ALWAYS AS (json_extract(body, '$.p')) VIRTUAL,
    genres ANY GENERATED ALWAYS AS (json_extract(body, '$.g')) VIRTUAL  -- a JSON list
) STRICT;

CREATE TABLE title_file (  -- each title's files, as the page lists them
    title TEXT NOT NULL REFERENCES title (id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    path TEXT NOT NULL,  -- in the library, as on the disk
    format TEXT,
    size INTEGER,
    PRIMARY KEY (title, position)
) STRICT;

CREATE INDEX title_file_by_path ON title_file (path);

CREATE TABLE title_alias (  -- the ids of a merged title's other copies
    alias TEXT NOT NULL,
    title TEXT NOT NULL REFERENCES title (id) ON DELETE CASCADE,
    PRIMARY KEY (alias, title)
) STRICT;

CREATE TABLE catalog_build (  -- each build: when, and what the page gets besides the titles
    seq INTEGER PRIMARY KEY,
    generated TEXT NOT NULL,
    titles INTEGER NOT NULL,
    sources TEXT NOT NULL CHECK (json_valid(sources)),  -- the sources' names, as the page shows them
    rules TEXT NOT NULL CHECK (json_valid(rules))       -- what the page needs of the rules
) STRICT;

CREATE TABLE seen_title (  -- when each title first came into the library: '' for one there at its first catalog
    id TEXT PRIMARY KEY,
    came TEXT NOT NULL
) STRICT;

CREATE TABLE seen_file (  -- the file names of your own folders' titles seen so far, so one moved or renamed isn't news
    name TEXT PRIMARY KEY
) STRICT;

CREATE TABLE account (  -- who may log in, and what they may do (access.ROLES)
    name TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('borrower', 'reader', 'librarian', 'owner')),
    hash TEXT NOT NULL,  -- the password's salted scrypt hash: never the password itself
    since TEXT NOT NULL DEFAULT ''
) STRICT;

CREATE TABLE browser_login (  -- a browser kept logged in: only its token's SHA-256, of no use to anyone who reads it
    digest TEXT PRIMARY KEY,
    name TEXT NOT NULL,     -- whose: a renamed or removed user's end, as these go by name
    stamp TEXT NOT NULL,    -- made from the password's hash: a new password ends it
    since INTEGER NOT NULL, -- seconds since 1970
    until INTEGER NOT NULL
) STRICT;

CREATE INDEX browser_login_by_since ON browser_login (since);

CREATE TABLE download (  -- each file downloaded into the library, as recorded when it finished
    seq INTEGER PRIMARY KEY,
    shop TEXT NOT NULL CHECK (shop IN ('humble', 'storybundle', 'drivethrurpg', 'itchio')),  -- whose download
    body TEXT NOT NULL CHECK (json_valid(body)),  -- the record, every field as it was written
    path ANY GENERATED ALWAYS AS (json_extract(body, '$.path')) VIRTUAL,  -- in the library
    title ANY GENERATED ALWAYS AS (json_extract(body, '$.title')) VIRTUAL,
    bundle ANY GENERATED ALWAYS AS (json_extract(body, '$.bundle')) VIRTUAL,
    kind ANY GENERATED ALWAYS AS (json_extract(body, '$.kind')) VIRTUAL,
    ext ANY GENERATED ALWAYS AS (json_extract(body, '$.ext')) VIRTUAL,
    size ANY GENERATED ALWAYS AS (json_extract(body, '$.size')) VIRTUAL,
    md5 ANY GENERATED ALWAYS AS (json_extract(body, '$.md5')) VIRTUAL,
    downloaded_at ANY GENERATED ALWAYS AS (json_extract(body, '$.downloaded_at')) VIRTUAL
) STRICT;

CREATE INDEX download_by_path ON download (shop, path);

CREATE TABLE copied (  -- each book hylly add copied into a folder of yours, as recorded once it was on the disk
    seq INTEGER PRIMARY KEY,
    body TEXT NOT NULL CHECK (json_valid(body)),  -- the record, every field as it was written
    dest ANY GENERATED ALWAYS AS (json_extract(body, '$.dest')) VIRTUAL,  -- in the library
    src ANY GENERATED ALWAYS AS (json_extract(body, '$.src')) VIRTUAL,  -- where it was copied from, only read
    size ANY GENERATED ALWAYS AS (json_extract(body, '$.size')) VIRTUAL,
    md5 ANY GENERATED ALWAYS AS (json_extract(body, '$.md5')) VIRTUAL,
    copied_at ANY GENERATED ALWAYS AS (json_extract(body, '$.copied_at')) VIRTUAL
) STRICT;

CREATE VIEW downloaded AS  -- the files as they stand: the latest record of each path
SELECT * FROM download AS d
WHERE seq = (SELECT max(seq) FROM download WHERE shop = d.shop AND path = d.path);

CREATE UNIQUE INDEX list_featured ON list (featured) WHERE featured = 1;
