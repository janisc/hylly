"""The shops Hylly downloads from, each named once. Every list and label of them comes from here: the folders that are a
shop's, the download records, the commands and their words, the jobs, the settings' defaults, the library server and,
through the catalog's data.js, the library page. Names and plain facts only, nothing that talks to a shop, so any part
may read it, the library server too (parts.py), as sites.py's logins are read. The kinds a title may be are the rules'
(kinds.toml's [kinds.X]: rules.Rules.kinds), as yours may add one.

tests/test_shops.py walks the table: each shop has a module of its own named by its id, its command and actions, its
job, what each action may do (effects.py), a reader in the catalog and its logins, and no list of shops is kept by hand
anywhere else. docs/downloads.md's "Writing a shop" says what a new one has to give."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Shop:
    id: str  # its name everywhere: hylly <id> <action>, its module (<id>.py), its folder in the library, its records
    label: str  # its short name, as messages give it ("Humble: done.")
    name: str  # its full name, as the library page and the feeds show the source
    site: str  # the site it talks to, as hylly commands says it
    letter: str  # the catalog's ids of its titles start with it and a colon ("h:..."); "f" is your own folders'
    logins: tuple[str, ...]  # its logins in sites.SITES; the first is the one its download needs
    # hylly <id> <action>, download among them. None is called list: hylly SHOP list [FILE] and the LIST after hylly SHOP
    # download are every shop's, from the table and its download's own options (commands.shop_lists, shoplist.py)
    actions: tuple[str, ...]
    job: str  # its job (runs.py), as hylly jobs and a second run's refusal name it
    kind: str = "book"  # what its titles are unless their words or its own say otherwise (rules.Rules.kind_of)
    trusted_pdfs: bool = True  # its publishers are known, so the e-reader feed offers its PDFs ([ereader_feed] pdf_from)
    update: bool = False  # --update also fetches the files it has replaced
    bundles: bool = True  # its titles come in bundles, which the library page names after it; else their publisher
    # How its downloads go where the shops differ, for the shops' core (shopping.py) and the downloads (download.py):
    exact: bool = False  # it lists each file's very size and MD5 (Humble), which a download then holds the file to
    link_refreshes: int = 2  # fresh links asked for a file whose link the shop refuses, before the file fails
    beside: bool = False  # a file it has changed comes beside the old one, under a name of its own, which stays (itch.io's
    # way), rather than waiting for --update to take the old one's place; and one it lists anew that is a file of the
    # title's here already (its MD5) isn't fetched again
    maker: str = ""  # who made a title, when it doesn't say (DriveThruRPG's "Others"): its folder goes in that one's

    @property
    def folder(self) -> str:
        """Its folder in the library, where its downloads go: never read as one of your own."""
        return self.id

    @property
    def state(self) -> str:
        """Its folder in the library's state folder, with its snapshots, its failures file and its working files: every
        file of its own there, named in it without the shop's id (failures.csv, itch.io's uploads.jsonl). What is every
        shop's stays in the state folder itself (its log, download-log.txt, and the library's own files)."""
        return self.id

    @property
    def failures(self) -> str:
        """The file that lists the files that failed last time, as a path in the library's state folder."""
        return f"{self.state}/failures.csv"

    def snapshot(self, when: datetime) -> str:
        """A new snapshot's place in the library's state folder, named by when it was made (snapshot.NAME): the newest is
        the last by name."""
        return f"{self.state}/{when:%Y-%m-%d_%H%M}.json"

    @property
    def tag(self) -> str:
        """What its lines in state/download-log.txt start with: the log is every shop's."""
        return f"[{self.id}] "


SHOPS: dict[str, Shop] = {shop.id: shop for shop in (
    Shop("humble", "Humble", "Humble Bundle", "humblebundle.com", "h", ("humble",),
         ("download", "seed"), "A Humble download or seed", update=True, exact=True),
    Shop("storybundle", "StoryBundle", "StoryBundle", "storybundle.com", "s", ("storybundle",), ("download",),
         "A StoryBundle download", link_refreshes=5),
    Shop("drivethrurpg", "DriveThruRPG", "DriveThruRPG", "drivethrurpg.com", "d", ("drivethrurpg",), ("download",),
         "A DriveThruRPG download", kind="rpg", update=True, bundles=False, link_refreshes=3, maker="Others"),
    Shop("itchio", "itch.io", "itch.io", "itch.io", "i", ("itchio", "itchio-bundles"), ("survey", "claim", "download"),
         "An itch.io survey, claim or download", trusted_pdfs=False, link_refreshes=3, beside=True),
)}


def labels() -> dict[str, str]:
    """Each shop's full name by its id: the sources' names before your folders' (folders.labels)."""
    return {shop.id: shop.name for shop in SHOPS.values()}
