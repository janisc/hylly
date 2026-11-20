"""The sites whose logins or keys Hylly keeps, by name: what each one's saved value is called, the file it's kept in,
the environment variable that can stand in for it, and how a shop's login is ended at the shop (ENDED_THERE). Names
only, nothing secret: the logins themselves are logins.py's, which only the parts that talk to the shops and the lookup
sources may import (parts.py). The commands' list and the help page read the names from here, so they needn't.

The lookup sources are named here too, each in a row of its own (LOOKUPS), which is all that's said of a source outside
its code: hylly login and its words, the commands' help, hylly commands and the help's list of keys are made from it, as
the parts that say them mustn't load the code that talks to the network. The code is lookups/<id>.py, whose Source takes
its name, its key and its budgets from its row (docs/lookups.md)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Lookup:
    """A lookup source, by name: what people are told of it, and the budgets it keeps to."""

    name: str  # what people see: "Metron"
    host: str  # the site it asks, as hylly commands lists it
    key: str = ""  # what its key is called, as the site calls it ("API token"); "": it needs none
    get_key: str = ""  # where to get one, in a sentence or two: hylly login says it, and the help's list of keys
    per_hour: int = 0  # requests in a rolling hour, at most (0: no budget of its own)
    per_day: int = 0  # and in a rolling day
    contact: bool = False  # it asks clients for a way to reach their people: contact under [lookups] in the settings
    key_at: str = ""  # where its key is made, and deleted: hylly login --forget names it ("": the site it asks, host)

    @property
    def word(self) -> str:
        """Its key's last word, small: "token", "key". The key's file and variable are named after it."""
        return self.key.split()[-1].lower()


# The lookup sources, by id, in the order they are asked when [lookups] order in the settings lists none: the first found
# answer for a field wins. The id names the code (lookups/<id>.py), the cache and the --source.
LOOKUPS = {
    "metron": Lookup("Metron", "metron.cloud", key="API token",
                     get_key="Sign up at metron.cloud, open your account page (top right) > API Token, make one and copy it.",
                     key_at="metron.cloud, on your account page", per_day=4500),  # the headers tell the real limit; this keeps a margin under the usual 5,000
    "comicvine": Lookup("Comic Vine", "comicvine.gamespot.com", key="API key",
                        get_key="Sign in at comicvine.gamespot.com/api, where the page shows your API key, and copy it.",
                        key_at="comicvine.gamespot.com/api", per_hour=190),  # about 200 an hour per resource
    "openlibrary": Lookup("Open Library", "openlibrary.org", per_hour=3000, per_day=10000, contact=True),
    "googlebooks": Lookup("Google Books", "googleapis.com", key="API key",
                          get_key="At console.cloud.google.com, make a project with no billing account, so that a key that "
                                  "leaks can't cost money, and enable the Books API in it (APIs & Services > Library). Make "
                                  "a key (Credentials > Create credentials > API key), restrict it to the Books API (API "
                                  "restrictions), so it opens nothing else, and copy it.",
                          key_at="console.cloud.google.com, under Credentials", per_day=900),  # the daily quota isn't clearly published: 1,000 was the old default
}
SITES = {  # site: (what its saved value is, file name in the logins folder, environment variable, HYLLY_ as every one)
    "humble": ("ory_session_stoiccohens510ht9xek", "humble-session.txt", "HYLLY_HUMBLE_SESSION"),  # its sign-in (Ory)
    "storybundle": ("_storybundle31_session", "storybundle-session.txt", "HYLLY_STORYBUNDLE_SESSION"),
    "drivethrurpg": ("application key", "drivethrurpg-key.txt", "HYLLY_DRIVETHRURPG_KEY"),
    **{sid: (s.key, f"{sid}-{s.word}.txt", f"HYLLY_{sid.upper()}_{s.word.upper()}") for sid, s in LOOKUPS.items() if s.key},
    "itchio": ("API key", "itchio-key.txt", "HYLLY_ITCHIO_KEY"),
    "itchio-bundles": ("itchio", "itchio-session.txt", "HYLLY_ITCHIO_SESSION"),
}
KEYS = {"drivethrurpg", "itchio", *(sid for sid, s in LOOKUPS.items() if s.key)}  # a key or token from the site, not a browser cookie
ENDED_THERE = {  # a shop's login: (what it is, how it ends at the site), for hylly login --forget: only the file goes
    "humble": ("your Humble login", "log out of humblebundle.com in the browser it came from, or change your "
                                    "password there"),
    "storybundle": ("your StoryBundle login", "log out of storybundle.com in the browser it came from, or change your "
                                              "password there"),
    "drivethrurpg": ("your DriveThruRPG key", "delete it at drivethrurpg.com > Account > Settings > Library App Keys"),
    "itchio": ("your itch.io API key", "revoke it at itch.io > Settings > API keys"),
    "itchio-bundles": ("your itch.io login, for the bundles", "log out of itch.io in the browser it came from, or change "
                                                              "your password there"),
}  # a lookup source's key: from its row (cli._ended_there)
# Where each browser shows a site's cookies, as the help's "Not logged in" says it: for hylly login's prompt and
# itch.io's refusal
COOKIE_PLACES = ("in Chrome and Edge, F12 > Application > Cookies; in Firefox, F12 > Storage > Cookies; in Safari on a Mac, "
                 "Develop > Show Web Inspector > Storage > Cookies (the Develop menu first needs Show features for web "
                 "developers, in Safari's settings under Advanced)")
