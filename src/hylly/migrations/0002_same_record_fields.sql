-- 0002: every shop's download records name the title and the file by the same fields, and the table takes any shop's.
--
-- A download record names, besides what 0001 says it holds, the shop's title and the file within it by the same fields
-- for every shop, and what the shop listed for the file:
--   item         the shop's own id for the title: Humble's machine name (as shop_id has it), StoryBundle's book,
--                DriveThruRPG's product, itch.io's game.
--   file         the file within it: for Humble its name in the title's folder (the last part of path), as Humble's
--                download tells a title's files apart by their place; for StoryBundle 0, a book being one file; for
--                DriveThruRPG the file's index; for itch.io the upload.
--   listed_md5, listed_size  the MD5 and the size the shop listed for the file when it was downloaded, what tells a
--                file the shop has changed since: DriveThruRPG's MD5 always (null for none) and its size when it lists
--                no MD5; itch.io's both. Humble's only when the file that came was another (a listing gone stale), as
--                its listing is taken as exact: else its md5 and size are what Humble listed. StoryBundle lists neither.
-- The names each shop had of its own for these go: storybundle_id; drivethrurpg_id, drivethrurpg_index,
-- drivethrurpg_md5 and drivethrurpg_size; itchio_id, itchio_upload, itchio_md5 and itchio_size. Every other field of
-- a record stays as it was written, shop_id among them.
--
-- The table no longer lists the shops it takes, so that a new shop's downloads need no migration (shops.SHOPS is
-- where the shops are named). SQLite can't change a table's checks in place: the table is made anew, every record in
-- its place (seq), in one pass, with its index and the view downloaded, and two columns more for SQL, item and file. A
-- record is rewritten only while it has a field of before, so the rewriting does nothing a second time. The pages the
-- old table leaves free are given back as the database opens (db._give_back).

CREATE TABLE download_0002 (  -- each file downloaded into the library, as recorded when it finished
    seq INTEGER PRIMARY KEY,
    shop TEXT NOT NULL CHECK (shop <> ''),  -- whose download: a shop's id (shops.SHOPS)
    body TEXT NOT NULL CHECK (json_valid(body)),  -- the record, every field as it was written
    path ANY GENERATED ALWAYS AS (json_extract(body, '$.path')) VIRTUAL,  -- in the library
    title ANY GENERATED ALWAYS AS (json_extract(body, '$.title')) VIRTUAL,
    bundle ANY GENERATED ALWAYS AS (json_extract(body, '$.bundle')) VIRTUAL,
    kind ANY GENERATED ALWAYS AS (json_extract(body, '$.kind')) VIRTUAL,
    ext ANY GENERATED ALWAYS AS (json_extract(body, '$.ext')) VIRTUAL,
    size ANY GENERATED ALWAYS AS (json_extract(body, '$.size')) VIRTUAL,
    md5 ANY GENERATED ALWAYS AS (json_extract(body, '$.md5')) VIRTUAL,
    downloaded_at ANY GENERATED ALWAYS AS (json_extract(body, '$.downloaded_at')) VIRTUAL,
    item ANY GENERATED ALWAYS AS (json_extract(body, '$.item')) VIRTUAL,  -- the shop's id for the title
    file ANY GENERATED ALWAYS AS (json_extract(body, '$.file')) VIRTUAL  -- the file within it
) STRICT;

-- Every record in one pass, in its place (seq), as these say:
--   renamed  a record's fields made again, in their order, each field of before under its new name, and each value as
--            it was: json_each gives a JSON true or false as a number, and an object or a list as a text, which json()
--            gives back as JSON.
--   filed    a Humble record with its file put beside its path: what follows the path's last / or \ (the inner rtrim
--            takes every letter but those off its end), or for a path that ends in one, what comes before it; never
--            nothing. Its item is put beside its shop_id below.
--   old      whether the record has a field of before, of its shop's: if not, it goes as it is.
WITH r AS (
    SELECT d.seq, d.shop, d.body,
        (SELECT json_group_object(
                CASE d.shop || ':' || j.key
                    WHEN 'storybundle:storybundle_id' THEN 'item'
                    WHEN 'drivethrurpg:drivethrurpg_id' THEN 'item'
                    WHEN 'drivethrurpg:drivethrurpg_index' THEN 'file'
                    WHEN 'drivethrurpg:drivethrurpg_md5' THEN 'listed_md5'
                    WHEN 'drivethrurpg:drivethrurpg_size' THEN 'listed_size'
                    WHEN 'itchio:itchio_id' THEN 'item'
                    WHEN 'itchio:itchio_upload' THEN 'file'
                    WHEN 'itchio:itchio_md5' THEN 'listed_md5'
                    WHEN 'itchio:itchio_size' THEN 'listed_size'
                    ELSE j.key END,
                CASE j.type WHEN 'true' THEN json('true') WHEN 'false' THEN json('false')
                    WHEN 'object' THEN json(j.value) WHEN 'array' THEN json(j.value) ELSE j.value END)
            FROM json_each(d.body) AS j) AS renamed,
        CASE WHEN json_type(d.body, '$.path') = 'text' AND json_extract(d.body, '$.path') <> ''
                AND json_type(d.body, '$.file') IS NULL
            THEN json_set(d.body, '$.file', CASE
                WHEN rtrim(json_extract(d.body, '$.path'), '/\') = '' THEN json_extract(d.body, '$.path')
                ELSE substr(rtrim(json_extract(d.body, '$.path'), '/\'), 1 + length(rtrim(rtrim(json_extract(d.body, '$.path'), '/\'),
                    replace(replace(rtrim(json_extract(d.body, '$.path'), '/\'), '/', ''), '\', '')))) END)
            ELSE d.body END AS filed,
        EXISTS (SELECT 1 FROM json_each(d.body) AS j WHERE d.shop || ':' || j.key IN (
            'storybundle:storybundle_id', 'drivethrurpg:drivethrurpg_id', 'drivethrurpg:drivethrurpg_index',
            'drivethrurpg:drivethrurpg_md5', 'drivethrurpg:drivethrurpg_size', 'itchio:itchio_id', 'itchio:itchio_upload',
            'itchio:itchio_md5', 'itchio:itchio_size')) AS old
    FROM download AS d)
INSERT INTO download_0002 (seq, shop, body)
SELECT seq, shop, CASE
    WHEN shop = 'humble' AND json_type(body, '$.shop_id') = 'text' AND json_type(body, '$.item') IS NULL
        THEN json_set(filed, '$.item', json_extract(body, '$.shop_id'))
    WHEN shop = 'humble' THEN filed
    WHEN NOT old THEN body
    WHEN shop = 'storybundle' AND json_type(body, '$.storybundle_id') IS NOT NULL THEN json_set(renamed, '$.file', 0)
    ELSE renamed
END
FROM r ORDER BY seq;

DROP VIEW downloaded;
DROP TABLE download;
ALTER TABLE download_0002 RENAME TO download;
CREATE INDEX download_by_path ON download (shop, path);

CREATE VIEW downloaded AS  -- the files as they stand: the latest record of each path
SELECT * FROM download AS d
WHERE seq = (SELECT max(seq) FROM download WHERE shop = d.shop AND path = d.path);
