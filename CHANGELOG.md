# Changelog

What changed in each version of Hylly, the newest first. How each part works is in the help page, the library page's
**Help**.

## Unreleased

- On Windows, a command or the server reading the library's marker just as another program writes it waits a
  moment for it, instead of saying the marker can't be read.
- A title's details, your lists and bookmarks fill the screen on a phone or a tablet, with *‹ Library* at the top, and open
  as a window in the middle of the page on a computer. They no longer scroll sideways: a wide cover had pushed the
  fifth star off an upright phone.
- The Inbox knows a book identical to one in your own folders, by its size and MD5, not only to a copy Hylly recorded, and
  leaves it out as already in the library.
- Every e-book's text follows the size you choose under *Aa*. A book that sets its text in fixed sizes (in pixels, points,
  or words such as "small") had stayed as it was at every step. A step is now the same size in every book, as it goes by
  the book's main text, and there are two larger ones: nine in all.
- A manual named by its folder and what it is ("Moonwake - Reference Card.pdf" in `manuals/Moonwake`) goes by that name,
  whole: the game had been taken for its author, and what a scan's PDF says of itself for its title. A title that is only
  a program's own words ("Adobe Photoshop PDF", "Full page photo") gives way to the file's name everywhere.
- How long a copy or a backup has left goes by what each file costs and by its bytes, both learned as it runs: one that
  began with thousands of small files had its first notes say hundreds of hours.
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
