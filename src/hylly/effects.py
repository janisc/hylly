"""What each command may do, for whoever runs it on someone's behalf: hylly commands, and the help page's reference. A
row for every command as it's run, each action of its own (hylly people list reads, hylly people forget can't be undone):
tests/test_assistants.py holds the rows to the parser.

    online       the sites it talks to; none means this computer and the library only
    writes       what it may change: library (the files), records (the library's, in the database, and state/ and
                 catalog/ beside the files), settings (the folder with your logins, and in its database the users and
                 what's kept about people), system (what starts at logon, and the running server or job), nothing
    job          the job it holds while it runs: a second run of it is refused, with exit code 4 (hylly jobs lists them).
                 Its lock and its note of how the last run ended are in the state folder (<job>.lock, <job>.json,
                 <job>-last.json), and a long job's notes to the phone in bell-jobs.json beside them. Its dry run holds
                 none. A job of runs.JOBS by its name, as cli.job_of gives it (a test holds the two alike), or in words
                 for a command that takes its locks itself
    account      it changes something in your account at a shop: decide with its owner first
    person       what only a person can do for it: "always", or the option that stands in for them once they have decided
    dry_run      what --dry-run (-n) reads to say what it would do: "offline" (this computer only), or "reads the site".
                 Every command that changes something has one

Any of these may also send a note to the ntfy server in the settings folder's secrets, and run the program named as
command under [bell] with it: long jobs when they end, and the server, or hylly server jobs, its alarms (ALSO). What hylly bell shows is what such a note can hold.
"""

from __future__ import annotations

from .paths import PLACES
from .settings import SECRETS
from .shops import SHOPS
from .sites import KEYS, LOOKUPS, SITES


def _e(online=(), writes=("nothing",), job=None, account=False, person=None, dry_run=None) -> dict:
    return {"online": list(online), "writes": list(writes), "job": job, "account": account, "person": person, "dry_run": dry_run}


def _at(shop: str) -> list[str]:
    """The site a shop's action talks to (shops.py)."""
    return [SHOPS[shop].site]


SOURCES = tuple(s.host for s in LOOKUPS.values())  # the lookup sources' sites, from their rows
YES = "--yes, once its owner has decided"
FIRST = ("--yes for the first {what} to a target, the first there from another {other}, or one that would write more than all "
         "{done} there before, once its owner has checked {which}")
SERVER = "with -H, HYLLY_REMOTE or [remote] ssh: the server computer, over ssh"
INBOX = "the Inbox ([inbox] in the settings)"
OFF, SITE = "offline", "reads the site"
LIST = ("the list, in the file you name (FILE: as it comes <shop>-list.csv, or hylly-list.csv, in the current folder), never "
        "over one there without --force; - writes it to standard output")
TARGETS = ("settings: hyllycopy-targets.json, the targets this computer has copied to with each one's mark, so that a disk "
           "not mounted isn't taken for an empty target")

EFFECTS = {
    "login": _e(online=["the site you name; with --forget or --forget-all, none"],
                writes=["settings: the login's file in the logins folder, saved, or deleted with --forget or --forget-all"],
                person="always, unless its owner has copied it and uses --clipboard, or gives it with --stdin or --cookies; "
                       "--forget and --forget-all need nobody",
                dry_run="checks it with the site, and saves nothing; with --forget or --forget-all, offline, and deletes "
                        "nothing"),
    "humble download": _e(online=_at("humble"), writes=["library", "records"], job="humble", dry_run=SITE),
    "humble seed": _e(writes=["library", "records"], job="humble", dry_run=OFF),
    "storybundle download": _e(online=_at("storybundle"), writes=["library", "records"], job="storybundle", dry_run=SITE),
    "drivethrurpg download": _e(online=_at("drivethrurpg"), writes=["library", "records"], job="drivethrurpg", dry_run=SITE),
    "itchio survey": _e(online=_at("itchio"), writes=["records"], job="itchio", dry_run=OFF),
    "itchio claim": _e(online=_at("itchio"), writes=["records"], job="itchio", account=True, person=YES, dry_run=SITE),
    "itchio download": _e(online=_at("itchio"), writes=["library", "records"], job="itchio", dry_run=SITE),
    "download": _e(online=[shop.site for shop in SHOPS.values()], writes=["library", "records"], job="each shop's", dry_run=SITE),
    # the shop list (shoplist.py), every shop's from the table: read at the shop, written as a file of your own
    **{f"{shop.id} list": _e(online=_at(shop.id), writes=[LIST], dry_run=SITE) for shop in SHOPS.values()},
    "list": _e(online=[shop.site for shop in SHOPS.values()], writes=[LIST], dry_run=SITE),
    "add": _e(online=["with --inbox: " + s for s in SOURCES] + ["with --inbox, with nobody at the keyboard: the ntfy "
                                                                    "server in the settings, for the pass's note"],
              writes=["library", "records",
                      "with --inbox: " + INBOX + ", its books removed from there once the library has them, and what "
                      "can't be taken moved into its Not added folder with a note; the tags and kinds its folders name, "
                      "as edits of the Inbox's; the passes' state and log in the library's state folder",
                      "settings: with --inbox, with nobody at the keyboard, the pass's note in bell.log and "
                      "bell-jobs.json"],
              job="add", person="--yes for the first run of hylly add --inbox by hand, which removes the Inbox's books "
                                "from there once the library has them, once its owner has decided", dry_run=OFF),
    "catalog": _e(online=["with --lookup: " + s for s in SOURCES], writes=["records"], job="catalog",
                  person="--yes for a build with far fewer titles than the last one, once its owner has decided", dry_run=OFF),
    "lookup": _e(online=SOURCES, writes=["records"], job="lookup", dry_run=OFF),
    "tag add": _e(writes=["records"], person=YES, dry_run=OFF),
    "tag remove": _e(writes=["records"], person=YES, dry_run=OFF),
    "tag list": _e(),
    "tag suggest": _e(writes=["records: the suggestions waiting for review, and curation/tags.toml, started with examples if "
                              "it isn't there"], dry_run=OFF),
    "set": _e(writes=["records"], person=YES, dry_run=OFF),
    "decide": _e(writes=["records"], person="always: the answers are its owner's", dry_run=OFF),
    "rules": _e(),
    "rules show": _e(),
    "rules copy": _e(writes=["settings: a file in your rules folder, never over one that's there"], dry_run=OFF),
    "rules try": _e(),
    "verify": _e(),
    "serve": _e(online=["none of its own; browsers viewing the page load shop covers and word meanings"],
                writes=["records", "settings", "system: a server started, unless one runs already; with --service-file "
                        "none of its own: the walled service for this computer is printed"],
                dry_run=OFF),
    "server status": _e(),
    "server stop": _e(writes=["system: the running server ends"], dry_run=OFF),
    "server restart": _e(writes=["records", "settings", "system: the running server ends, and one starts in its place"], dry_run=OFF),
    "server autostart on": _e(writes=["system, on Windows: the start at every logon (a task), and a server started now; on Linux "
                                      "and macOS nothing: the systemd unit or launchd agent is printed"], dry_run=OFF),
    "server autostart off": _e(writes=["system, on Windows: the start at logon goes, and a running server stays; on Linux and "
                                       "macOS nothing: how to remove it is printed"], dry_run=OFF),
    "server take-over": _e(writes=["records: state/database.json in the library, which names this computer's database from "
                                   "now on"], person="--yes, once its owner has decided, and stopped the server that kept them",
                           dry_run=OFF),
    "server jobs": _e(online=["the ntfy server in the settings, for the alarms it raises", *SOURCES],
                      writes=["records: through the nightly copy it starts, the copies of the database and the settings in "
                              "state/db-backup and state/settings-backup",
                              "library, records and the Inboxes: through the Inbox's pass it starts (internal inbox), with "
                              "jobs = \"external\", or on a computer whose server is walled in",
                              "settings: server-jobs/, each job's lock and when it last ran; the alarms' bell-jobs.json, "
                              "and bell.log"],
                      job="each of its jobs' own, in server-jobs/: one still running from before is left to run, and "
                          "the nightly copy and the Inbox run as processes of their own (internal server-job)", dry_run=OFF),
    "user add": _e(writes=["settings"], person="always, unless its owner gives the password with --password-stdin", dry_run=OFF),
    # what changes who may do what, or lets more people in, is the library's owner's to decide: add and restore take a
    # password, role and remove a yes, and a link that goes anywhere but a terminal a yes too
    "user role": _e(writes=["settings"], person=YES + ": it changes what the user may do, and an owner may do anything", dry_run=OFF),
    "user rename": _e(writes=["settings"], dry_run=OFF),
    "user remove": _e(writes=["settings"], person=YES, dry_run=OFF),
    "user restore": _e(writes=["settings"], person="always, unless its owner gives the new password with --password-stdin",
                       dry_run=OFF),
    "user list": _e(),
    "user link": _e(writes=["settings: the link's digest, until it's used or 10 minutes are up"],
                    person="--yes where standard output is a pipe or a file, once its owner has decided where the link goes: it "
                           "logs a browser in as that user, the owner too", dry_run=OFF),
    "people list": _e(),
    "people export": _e(),
    "people forget": _e(writes=["settings"], person="--yes, once the person it's about has decided: it can't be undone from here",
                        dry_run=OFF),
    "people erase": _e(writes=["settings"], person="--yes, once the person it's about has decided: it can't be undone from here",
                       dry_run=OFF),
    "reviews list": _e(),
    "reviews export": _e(writes=["with --out: the file you name, written over if it's there"], dry_run=OFF),
    "reports": _e(),  # lists them, with what the saved orders say: asks no shop
    "theme list": _e(),
    "theme check": _e(),  # checks a file: changes nothing
    "bell show": _e(),
    "bell test": _e(online=["the ntfy server in the settings",
                            "the program named as command under [bell], if one is set, and whatever it reaches"],
                    writes=["settings: the test in bell.log"], dry_run=OFF),
    "copy": _e(writes=["the target folder you name, never over what is there; the source is only read", TARGETS], job="copy",
               person=FIRST.format(what="copy", other="source", done="copied", which="the source and the target"), dry_run=OFF),
    "copy check": _e(writes=["the check's report, in the target's .hyllycopy folder: the copies are only read"], job="copy", dry_run=OFF),
    "backup": _e(online=[SERVER],
                 writes=["the target folder you name, or target under [backup] in the settings: the books as plain files, a "
                         "zip of the library's records, your edits and the settings, and HYLLY-BACKUP.txt; what changed is set "
                         "aside there, and no book is deleted; with keep under [backup], the older zips it thins go, each "
                         "written down in saved/thinned.txt",
                         "records: today's copy of the database made again in the library's state/db-backup, or, when another "
                         "computer keeps them, by that server over ssh, which then also copies its settings into "
                         "state/settings-backup",
                         "records: a note of the backup in the library's state/backup.json, through the server when this "
                         "computer may only read the library", TARGETS],
                 job="backup", person=FIRST.format(what="backup", other="library", done="backed up", which="the library and the target"), dry_run=OFF),
    "backup check": _e(writes=["the check's report, in the backup's library/.hyllycopy folder: the books, their earlier "
                                   "copies and the zips are only read"],
                       job="backup", dry_run=OFF),
    "restore": _e(writes=["the new folder you name, never over what is there: the books, the newest zip (or --saved) unpacked "
                          "in it, the backed-up settings in its _settings-from-backup, and until it's whole .hylly-restoring.json, which "
                          "lets the same command go on there; it goes into a new or empty folder only",
                          "settings: this computer's database, made from the backup's copy of the library's records, if it "
                          "has none", TARGETS], job="backup", dry_run=OFF),
    "init": _e(writes=["records: the folder's mark as a Hylly library, and the folder if it isn't there",
                       "settings: with FOLDER and no settings.toml yet, one with the library's line, in this user's usual "
                       "settings folder or the one HYLLY_HOME or HYLLY_CONFIG_DIR names, while HYLLY_SETTINGS isn't set; one "
                       "there already is never changed"],
               person="--yes for an empty or new folder, once its owner has checked it's for a new library and not where an "
                      "unmounted disk should be, and --new as well for one this computer's database knows as the library, "
                      "with titles", dry_run=OFF),
    "settings show": _e(),  # the values, and where the alarms' secrets are kept, never their values
    "settings path": _e(),
    "settings places": _e(),
    "settings check": _e(),
    "settings edit": _e(writes=["settings: settings.toml, as you change it in your editor, and made empty first if it isn't "
                                "there"], person="always: its owner edits the file", dry_run=OFF),
    "jobs list": _e(),
    "jobs stop": _e(writes=["system: the running job ends; what it has done is kept"], dry_run=OFF),
    "logs": _e(),
    "doctor": _e(),
    "commands": _e(),
    "demo": _e(writes=["its own folder, settings and server, apart from yours",
                       "system: with --background, its server started and left running; with --stop, that server ends, and "
                       "no other"], dry_run=OFF),
    # between Hylly's own processes: hylly commands --internal
    "internal backup-noted": _e(writes=["records: the note, in the library's state/backup.json"], dry_run=OFF),
    "internal fresh-copy": _e(writes=["records: today's copy of the database, and of the settings, in the library's state"], dry_run=OFF),
    "internal job": _e(writes=["what its command writes"], dry_run=OFF),
    "internal inbox": _e(online=[*SOURCES, "the ntfy server in the settings, for its note"],
                         writes=["library", "records", INBOX + ": its books removed from there once the library has "
                                 "them, what can't be taken moved into its Not added folder with a note; the tags and "
                                 "kinds its folders name, as edits of the Inbox's; the passes' state and log in the "
                                 "library's state folder",
                                 "settings: its note in bell-jobs.json and bell.log"],
                         job="add, while it runs: a hylly add typed meanwhile is refused with code 4, and one running already "
                             "has the Inbox left to it", dry_run=OFF),
    "internal server-job": _e(online=["for the Inbox: " + s for s in SOURCES],
                              writes=["records: the nightly copy's, in state/db-backup and state/settings-backup",
                                      "library, records and the Inboxes: the Inbox's, through its pass (internal inbox)",
                                      "settings: server-jobs/, what the job said"],
                              job="the job's own, in server-jobs/: a second run of it at once ends with code 4", dry_run=OFF),
}

ALSO = ("Long jobs, when they end, the server, for its alarms, and hylly server jobs, for those it raises with jobs = "
        "\"external\" under [server], send a note to the ntfy server in secrets/ntfy-url in the settings folder, if one "
        "is set, and run the program named as command under [bell] with the same note, if one is set. "
        "It is in counts and kinds unless remote_detail says otherwise.")

REMOTE = ("With -H HOST, HYLLY_REMOTE or [remote] ssh in the settings naming the server computer (in that order), a command "
          "runs there over ssh, as the server computer owns the library's data, and says on standard error that it does "
          "(Running on HOST.). A long job (one that holds a job hylly jobs lists; hylly download, each of whose shops' "
          "runs is one; and hylly server jobs, whose own long jobs write where it does) starts there on its own, so "
          "closing the window here doesn't stop it; hylly jobs stop NAME stops it. Its dry run, which holds no job and "
          "changes nothing, runs while ssh does. Commands about this computer and its disks run here: those marked runs "
          "here. --here, before the command, runs any command here.")

WHOSE = {**{login: f"{shop.label}'s" for shop in SHOPS.values() for login in shop.logins},  # each shop's logins
         **{sid: s.name + ("'" if s.name.endswith("s") else "'s")  # Metron's, Google Books': those with a key
            for sid, s in LOOKUPS.items() if s.key}}


def _secret(site: str, what: str) -> str:
    """A login's variable, in words: a cookie is called one, with its name; a key by what the site calls it."""
    kind = what if site in KEYS else f"login cookie ({what})"
    return (f"SECRET, set by its owner only, never asked for: {WHOSE[site]} {kind}"
            + (", for the bundles" if site == "itchio-bundles" else "") + ", instead of the saved one")


NTFY = {"ntfy-url": "the alarms' ntfy address, whose topic is as good as a password for reading them",  # settings.SECRETS
        "ntfy-token": "the alarms' ntfy token, for an ntfy server that wants one, read only from where the address is"}
# the logins, and the alarms' secrets: an assistant never asks for one, nor sets one
SECRET = [*(variable for _, _, variable in SITES.values()), *(variable for _, variable in SECRETS.values())]

ENVIRONMENT = {
    "HYLLY_LIBRARY": "the library's folder, instead of library in settings.toml",
    "HYLLY_HOME": "one folder for all of Hylly's own files, on every system: settings.toml, the database with the users, "
                  "the logins, the logs and locks and the reader's cache, over each kind's own variable below. Set to "
                  "another folder, it makes a Hylly apart from your own, to try things on, as long as HYLLY_SETTINGS, "
                  "HYLLY_RULES and HYLLY_REMOTE are unset: set, they still lead to your own settings, rules or server "
                  "computer (hylly --here keeps a command here). Such a one can't change the start at logon, which is one "
                  "for the whole user",
    **{variable: f"a folder of its own for {holds}, by its full path, instead of this system's default: hylly settings "
                 "places says where each kind is" + ("; another place makes another Hylly, as HYLLY_HOME does, so the start "
                 "at logon refuses it, and a system-wide service's walls no longer keep its server from starting"
                 if variable == "HYLLY_CONFIG_DIR" else "") for holds, variable, _, _ in PLACES.values()},
    "HYLLY_SETTINGS": "another settings.toml: only that file, not the settings folder",
    "HYLLY_RULES": "your rules folder or file, instead of rules in settings.toml",
    "HYLLY_REMOTE": "the server computer that commands run on, as -H does: its name as ssh knows it, instead of [remote] ssh in "
                    "the settings",
    "HYLLY_NO_NOTIFY": "any value: no notifications on this computer",
    "HYLLY_STORYBUNDLE_USER": "your StoryBundle account's number, instead of storybundle_user in settings.toml: not a "
                              "secret",
    "HYLLY_ALLOW_ROOT": "any value: lets Hylly run as root, where there truly is no other way (some containers). Not on a "
                        "computer of your own: what it writes would be root's, and a flaw in it or in a book could do "
                        "anything there",
    "HYLLY_SYSTEM_SERVICE": "set by the system-wide service with walls (examples/hylly.service) for the server inside it: never "
                            "set by hand",
    **{variable: _secret(site, what) for site, (what, _, variable) in SITES.items()},
    **{variable: f"SECRET, set by its owner only, never asked for: {NTFY[name]}, instead of secrets/{name} in the settings "
                 "folder" for name, (_, variable) in SECRETS.items()},
    **{f"{variable}_FILE": f"a file that holds {NTFY[name].partition(',')[0]}, as Docker gives its secrets: read when "
                           f"{variable} isn't set, before systemd's credential and secrets/{name}" for name, (_, variable) in SECRETS.items()},
    "CREDENTIALS_DIRECTORY": "systemd's, for a unit with LoadCredential=ntfy-url:FILE (or ntfy-token): the alarms' secrets "
                             "from there, before secrets/ in the settings folder. Never set by hand",
}
