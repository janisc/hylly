-- 0005: the files removed from the library from the Server page, each into _Removed, and what became of it since
-- (removed.py).
--
-- To decide's Keep left only, Keep right only and Remove both, for two copies that may be one book, move a copy's files
-- out of the library into the folder _Removed at its top, each at its own place below the library, where no build reads
-- it; nothing is deleted until _Removed is emptied, on the Server page too. One row a file, kept for good: where it was,
-- where it went, and what became of it since, so that hylly verify takes no file you removed for one missing, also once
-- _Removed is emptied, and Put back takes back exactly what the removal did.
--   path         its place in the library, below its top, with / between the folders: what the records name it by
--   held         its place below _Removed: path itself, or with " (2)" and so on where that was taken
--   size         its size as it was removed
--   title, title_t  the catalog id of the title it was a file of, and its name, for people
--   shop         the shop whose download recorded it, "" for a file of your own folders
--   moved        for a shop's file, the answer the removal gave its title, so no download fetches it again (curation's
--                moved: {"id": the title's catalog id at the shop, "key": moved:<its file>, "was": the answer before})
--   removed_at   when it was removed
--   state        removed (in _Removed now), back (put back in its place), emptied (deleted with _Removed's files)
--   ended_at     when it was put back or emptied
-- Data only of its own: no other table changes. The table is made only if it isn't there, so the step done again
-- changes nothing.

CREATE TABLE IF NOT EXISTS removed (  -- each file removed into _Removed, and what became of it (removed.py)
    seq INTEGER PRIMARY KEY,
    path TEXT NOT NULL CHECK (path <> ''),
    held TEXT NOT NULL CHECK (held <> ''),
    size INTEGER NOT NULL CHECK (size >= 0),
    title TEXT NOT NULL,
    title_t TEXT NOT NULL,
    shop TEXT NOT NULL,
    moved TEXT CHECK (moved IS NULL OR json_valid(moved)),
    removed_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('removed', 'back', 'emptied')),
    ended_at TEXT,
    more TEXT CHECK (more IS NULL OR json_valid(more))
) STRICT;
