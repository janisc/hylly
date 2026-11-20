# Glossary

Two names, and plain words for everything else: a part that works on its own gets a name, and the rest uses ordinary words,
always the same ones.

| Word | What it means |
|---|---|
| **Hylly** | The program. Finnish for "shelf". |
| **Shelf** | A saved part of the library, which the filters and the search then work inside. Tidiness, not a lock. The file that tells `hylly add` which subfolder each book goes into is its *list* (`list =`, `--list`, `examples/add-list.csv`), never a shelf. |
| **HyllyReader** | The reader in the library page. `reader.py`, the *Read* button. |
| **HyllyBell** | What tells you things: alarms, and the daily summary. `hylly bell`, `[bell]`, `bell.py`. |
| **Library** | Your collection, in one folder, the *library folder* ("library root" in the code). A shop's own list is always qualified: "your itch.io library". |
| **Library page** | The page listing the books, at `/catalog/index.html` on the library server. |
| **Person** | What the server keeps about one logged-in user: `people.py`, in the database (`hylly.db` in the data folder). |
| **Server page** | The owner's page for the server, `/server`. |
| **Library feed** | The feed for reading apps (OPDS), `/opds`; the *e-reader feed*, `/ereader/opds`, has only what suits an e-ink screen. Each is the logged-in person's own. |
| **Shop** | A place you bought from: Humble Bundle, StoryBundle, DriveThruRPG, itch.io. Its downloads go in its *shop folder*. |
| **Your folder** | A folder of your own at the top of the library ("own folder" in the code). Its first subfolder is the title's *collection*. |
| **Bundle** | A set bought together at a shop. |
| **Source** | Where a title came from: a shop or your folder. Metadata sites are always *lookup sources*. |
| **Title** | One work, in any number of files and formats. Never "item" or "product" to users. |
| **Main file, extra** | The file you read, and its companions: maps, sheets, bonus PDFs. |
| **Kind** | Book, tech, RPG, comic or manual as they come: the `[kinds.X]` tables of the rules (`kinds.toml`), and a kind your rules add. Nothing else is a kind. itch.io's categories are *types* (`--types`). |
| **Format** | EPUB, PDF, CBZ and so on. |
| **Card** | One tile in the library page: a title, or a series with *Series together*. |
| **Copies** | The same book in several places, shown as one card. |
| **Catalog** | The generated data and the `catalog/` folder only. The page is the library page. |
| **Reading app, e-reader** | An app that reads the library feed; an e-ink device. |
| **Roles** | Owner, librarian, reader, borrower, from the most a user may do to the least: the help's *Logins and the access log* says what each may. |
| **Survey, claim** | itch.io: count what there is; put a bundle's items into your itch.io library. |
| **Lookup** | Asking a lookup source for a title's details, within its budget. |
| **Alarm, daily summary** | What HyllyBell sends: something worth a look; the day in brief. |
| **Superseded** | The folder where a replaced file goes, rather than being deleted. |
| **Check** | One line of `hylly doctor`, about one thing. In `--json`: its `id`, which stays the same between versions (`catalog`, `settings-rights`; `doctor.CHECKS` has them all), `what` it is about, which several share, its `state` (`ok`, `info`, `update` or `problem`), what it `says`, and the `next` step in words; then, for a script or an AI assistant, `run`, the one command to run, only where the step is one command that needs no sudo, password, login or yes, and `owner: true` where the step is the owner's alone. |
