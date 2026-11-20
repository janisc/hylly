-- 0004: the list of questions the rules can't settle by themselves, put to you (curation.py, hylly decide).
--
-- A run of a shop's download finds what its rules are unsure of (curation.QUESTIONS: which file of a title to keep,
-- whether an item of only extras is a title, a file gone from where it was downloaded) and keeps each here, one row a
-- question, found again by its kind and its subject: the shop, the title's id there (its catalog id is the shop's letter,
-- a colon and this), and the file's key where it is one file's. A question a run no longer finds goes. What answers one
-- is an ordinary edit (the table edit): a row only says what was asked, what the run took, and the answer given.
--   options      each option: a file the shop lists (its name, format and size), or the words of a choice
--   guess        the option the rules take, by its place in options
--   held         the run took it not, but held that file back until you answer ([download] unsure = "ask")
--   first_found, last_found   when a run first and last found it
--   answer       your answer, as given (which option, who, when), until a run finds the question settled
-- A question's number (seq) is never given again once its question has gone (AUTOINCREMENT, which keeps the highest
-- in sqlite_sequence), so an answer typed from an older list never reaches another question. Data only of its own:
-- no other table changes. The table is made only if it isn't there, so the step done again
-- changes nothing.

CREATE TABLE IF NOT EXISTS question (  -- what the rules couldn't settle, as runs last found it (curation.keep_questions)
    seq INTEGER PRIMARY KEY AUTOINCREMENT,  -- its number, which hylly decide answers it by: never given twice
    kind TEXT NOT NULL CHECK (kind <> ''),  -- which-file, title, moved (curation.QUESTIONS)
    shop TEXT NOT NULL CHECK (shop <> ''),  -- a shop's id (shops.SHOPS)
    item TEXT NOT NULL,  -- the title's id at the shop
    file TEXT NOT NULL DEFAULT '',  -- the file's key among the title's, for a question of one file
    title_t TEXT NOT NULL DEFAULT '',  -- the title's name, for people
    options TEXT NOT NULL CHECK (json_valid(options)),
    guess INTEGER,
    held INTEGER NOT NULL DEFAULT 0,
    first_found TEXT NOT NULL,
    last_found TEXT NOT NULL,
    answer TEXT CHECK (answer IS NULL OR json_valid(answer)),
    more TEXT CHECK (more IS NULL OR json_valid(more)),
    UNIQUE (kind, shop, item, file)
) STRICT;
