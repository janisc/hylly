-- 0003: StoryBundle's download records name a book's kind book, as every shop's records name a kind of the rules'.
--
-- Before the shops' core, StoryBundle's own downloads wrote kind = 'storybundle' in their records, which is
-- no kind of the rules' (the kinds are kinds.toml's [kinds.X] tables: book, tech, rpg, comic, manual as they come).
-- Every StoryBundle record since names its kind as the core finds it, book unless its bundle's words say otherwise.
-- So each record of shop storybundle whose kind is 'storybundle' is rewritten in place (its seq, every other field as
-- it was) to say 'book', what StoryBundle's titles were then and are unless their words say more. Data only: the tables
-- stay as they are. A record is rewritten only while it says 'storybundle', so the step done again changes nothing.
-- The copy of the database beside it, taken before the step (hylly.before-0003.db), has the records as they were.

UPDATE download SET body = json_set(body, '$.kind', 'book')
WHERE shop = 'storybundle' AND json_type(body, '$.kind') = 'text' AND json_extract(body, '$.kind') = 'storybundle';
