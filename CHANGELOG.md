# Changelog

What changed in each version of Hylly, the newest first. How each part works is in the help page, the library page's
**Help**.

## Unreleased

- On Windows, a command or the server reading the library's marker just as another program writes it waits a
  moment for it, instead of saying the marker can't be read.
- The PDFs of a library given by a relative path (`hylly catalog --library books`) are read: each run took them for
  files that couldn't be read just then.

## 0.1.0 (2026-11-20)

The first release. Hylly downloads the e-books, comics and RPG books you own at Humble Bundle, StoryBundle, DriveThruRPG
and itch.io, checks each file, and keeps them all in one library folder, exactly as the shops delivered them, beside
folders of books you got elsewhere. An Inbox takes in whatever you drop into one folder, each book into the folder its kind
belongs in, its subfolders read as collections and #tags. A catalog on top gives them titles, authors, series, genres and your own tags,
renaming nothing, and looks up what the files don't say in Metron, Comic Vine, Open Library and Google Books. What the rules can't settle on their own, such as two copies that may be one book, waits on the Server page, where the files can be compared side by side and one set aside. The
library server shows it all in a library page with a reader built in, and as feeds for reading apps and e-readers
(OPDS), at home or away, with users, roles and an access log. Alarms come to your phone, a backup keeps the library on
a second disk, and HyllyCopy copies hundreds of gigabytes between any two folders, a share's too, and goes on from
where it was after a stop.
