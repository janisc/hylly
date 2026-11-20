"""hylly backup: the books as plain files, nothing deleted, the rest in a zip, logins never, and getting it all back."""

from __future__ import annotations

import errno
import functools
import hashlib
import json
import os
import re
import shutil
import sys
import tomllib
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from hylly import backup, bell, catalog, cli, dbcopy, doctor, exits, hyllycopy as hc, paths, people, serve, words
from hylly.library import Catalog
from hylly.exits import PARTLY, Exit

from helpers import DT_OLD, FIRST_RELEASE_RECORDS, at_the_first_step, build_library, made_before_the_library_was_kept
from helpers import named_now


@pytest.fixture
def library(tmp_path, library_at):
    root = library_at(tmp_path / "library")
    (root / "curation").mkdir(exist_ok=True)
    (root / "curation" / "edits.jsonl").write_bytes(b'{"made": "up"}\n')
    (root / "comics").mkdir(exist_ok=True)
    (root / "comics" / "A Comic 1.cbz").write_bytes(b"pages of a comic")
    (root / "catalog" / "covers").mkdir(exist_ok=True)
    (root / "catalog" / "covers" / "made-up.webp").write_bytes(b"a tiny cover")
    (root / "_ToBeRemoved").mkdir()
    (root / "_ToBeRemoved" / "old.pdf").write_bytes(b"on its way out")
    return root


@pytest.fixture
def top(tmp_path):
    return tmp_path / "disk" / "backup"


@pytest.fixture
def settings(monkeypatch, tmp_path):
    """A settings folder with something in it, on the same computer: the database the library's records are in comes
    along. Its settings and rules are read afresh, as the next command would read them."""
    import shutil

    from hylly import db, rules
    from hylly import settings as the_settings

    before = paths.config_dir() / db.FILE
    db.close_all()
    folder = tmp_path / "settings"
    folder.mkdir()
    monkeypatch.setenv("HYLLY_HOME", str(folder))
    monkeypatch.delenv("HYLLY_SETTINGS", raising=False)
    home = paths.config_dir()
    home.mkdir(parents=True, exist_ok=True)
    if before.is_file():
        shutil.copyfile(before, home / db.FILE)
    if db.token_file(before).is_file():  # this install's own, beside it: the folder moved, not a copy of the database
        shutil.copyfile(db.token_file(before), db.token_file(home / db.FILE))
    (home / "settings.toml").write_text('[server]\nname = "Made-up Library"\n', encoding="utf-8")
    (home / "users.txt").write_text("rory:owner:a-made-up-hash\n", encoding="utf-8")
    (home / "rules").mkdir()
    (home / "rules" / "mine.toml").write_bytes(b"# my rules\n")
    for secret in ("session.txt", "sessions.json", "humble-cookies.json", "keys.toml", "bell.json"):
        (home / secret).write_text("A-MADE-UP-SECRET", encoding="utf-8")
    the_settings.reload()
    rules.reset()
    return home


PHONE = '[server]\nname = "Made-up Library"\n\n[bell]\nwrong_passwords = 3\n'  # the phone's own address is in secrets/
OLD_PHONE = ('[server]\nname = "Made-up Library"\n\n[bell]\nntfy = "https://ntfy.sh/A-MADE-UP-SECRET-TOPIC"  # the phone\n'
             'ntfy_token = "A-MADE-UP-SECRET-TOKEN"\nwrong_passwords = 3\n')  # as an older Hylly kept them
OLD_NOTE = "left out of this copy, as it's as good as a password: put yours back by hand"  # the old rewriter's
OLD_COPY = ('[server]\nname = "Made-up Library"\n\n[bell]\nntfy = ""  # ' + OLD_NOTE + '\nntfy_token = ""  # ' + OLD_NOTE + '\n'
            '# ntfy = ""  # ' + OLD_NOTE + '\nwrong_passwords = 3  # the phone: (left out of this copy), with (left out of this '
            'copy)\n')  # a nightly copy of the settings an older Hylly made, as its rewriter left them
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the rights of Linux and macOS")
# A disk gone from the folder it was mounted at, played by renaming that folder while the backup has files open in it:
# Windows renames no folder with a file open in it, and a disk gone there takes its drive letter along
mount_folder = pytest.mark.skipif(os.name == "nt", reason="a disk gone from its mount folder, as Linux and macOS have it")


def computer(monkeypatch, folder: Path) -> None:
    """Another computer's settings folder (HYLLY_HOME): its settings and rules read afresh, as a command run there reads
    them, never the ones read on the computer before."""
    from hylly import rules
    from hylly import settings as the_settings

    monkeypatch.setenv("HYLLY_HOME", str(folder))
    the_settings.reload()
    rules.reset()


def phone_set(settings: Path, monkeypatch, written: str = PHONE) -> None:
    """The phone's ntfy topic and token in the settings folder's secrets, which are as good as a password, and the
    settings as written (OLD_PHONE: with them under [bell], as before they had files of their own). Nothing is sent there."""
    import httpx

    from hylly import settings as the_settings

    def nowhere(*a, **k):
        raise httpx.ConnectError("no alarms from a test")

    monkeypatch.setattr(httpx, "post", nowhere)
    (settings / "settings.toml").write_text(written, encoding="utf-8")
    the_settings.secrets_dir().mkdir(parents=True, exist_ok=True)
    (the_settings.secrets_dir() / "ntfy-url").write_text("https://ntfy.sh/A-MADE-UP-SECRET-TOPIC\n", encoding="utf-8")
    (the_settings.secrets_dir() / "ntfy-token").write_text("A-MADE-UP-SECRET-TOKEN\n", encoding="utf-8")


def notes_kept(monkeypatch) -> list[tuple[str, str, str]]:
    """The notes a backup sends to the phone, kept here instead: their title, their text, and the words for the phone."""
    said = []
    monkeypatch.setattr(bell, "job_note", lambda title, text, public="", wait=None: said.append((title, text, public)) or True)
    return said


def no_secret_in(copy: bytes) -> dict:
    """What a copy of the settings holds, which is never the phone's topic or token."""
    assert b"A-MADE-UP-SECRET" not in copy and b"AMADEUPSECRET" not in copy
    found = tomllib.loads(copy.decode("utf-8"))
    assert not {"ntfy", "ntfy_token"} & set(found.get("bell", {}))
    return found


@pytest.fixture
def today(monkeypatch) -> datetime:
    """The moment a test's backups go by, read before they run and given to them as now=, where the code takes it: the
    day's folder in changed and the database's fresh copy are named by it, so that a test run across midnight finds
    them where it looks."""
    moment = datetime.now()
    real = backup.BackingUp.__init__
    monkeypatch.setattr(backup.BackingUp, "__init__", lambda self, *a, now=None, **k: real(self, *a, now=now or moment, **k))
    fresh = backup.fresh_records
    monkeypatch.setattr(backup, "fresh_records", lambda root, now=None: fresh(root, now=now or moment))
    return moment


def run(library, top, *more):
    """A backup, with --yes: the first one to a target asks first (tests of that say so)."""
    cli.main(["backup", str(top), "--library", str(library), "--yes", *map(str, more)])


def check(top):
    cli.main(["backup", "check", str(top)])


def books(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file() and ".hyllycopy" not in p.parts}


def in_the_zip(top) -> dict[str, bytes]:
    with zipfile.ZipFile(backup.saved_zips(top)[-1]) as z:
        return {n: z.read(n) for n in z.namelist()}


def code_of(*argv) -> int:
    return exit_of("backup", *argv)


def exit_of(*argv) -> int:
    with pytest.raises(SystemExit) as e:
        cli.main(list(map(str, argv)))
    return e.value.code


# ---- what a backup is ---------------------------------------------------------------------------------------------------------

def test_a_plan_writes_nothing(library, top, settings, capsys):
    run(library, top, "--dry-run")
    out = capsys.readouterr().out
    assert "To copy:" in out and "only a dry run" in out and "a zip of state, curation, catalog" in out
    assert "--everything" not in out and not top.exists()


def test_a_backup_acts_and_its_first_to_a_target_asks_first(library, top, settings, capsys, monkeypatch):
    """hylly backup TARGET backs up: no --run, and never a success that did nothing. A target with no backup of this
    library asks first, as a mistyped one would get all of it: with nobody at the keyboard, exit code 8 and nothing
    there; with --yes, or a yes, it goes ahead. The next backups don't ask."""
    assert code_of(top, "--library", library) == exits.NEEDS_YOU
    assert "has no backup of" in capsys.readouterr().err and not top.exists()
    monkeypatch.setattr(cli, "at_keyboard", lambda: True)
    monkeypatch.setattr("builtins.input", lambda question: "y")
    cli.main(["backup", str(top), "--library", str(library)])
    assert books(top / "library")["comics/A Comic 1.cbz"] == b"pages of a comic" and backup.saved_zips(top)
    monkeypatch.setattr("builtins.input", lambda question: pytest.fail("not asked again"))
    cli.main(["backup", str(top), "--library", str(library)])
    assert len(backup.saved_zips(top)) == 2


def test_a_backup_that_would_write_more_than_all_before_asks(library, top, settings, capsys, monkeypatch):
    run(library, top)
    monkeypatch.setattr(hc, "FAR_MORE", 0)
    before = sum(len(data) for data in books(top / "library").values())
    (library / "comics" / "A Big One.cbz").write_bytes(b"x" * (2 * before))
    zips = len(backup.saved_zips(top))
    assert code_of(top, "--library", library) == exits.NEEDS_YOU
    assert "more than all" in capsys.readouterr().err and not (top / "library" / "comics" / "A Big One.cbz").exists()
    assert len(backup.saved_zips(top)) == zips  # nothing was zipped either
    run(library, top)
    assert (top / "library" / "comics" / "A Big One.cbz").is_file()


def test_a_dry_run_of_each_changes_nothing(library, top, settings, tmp_path, capsys):
    run(library, top)
    made = {p: p.stat().st_mtime_ns for p in top.rglob("*") if p.is_file()}
    run(library, top, "-n")
    cli.main(["backup", "check", str(top), "-n"])
    cli.main(["restore", str(top), str(tmp_path / "new"), "-n"])
    out = capsys.readouterr().out
    assert "only a dry run" in out and "would read back 1 zip" in out and "It would read" in out
    assert {p: p.stat().st_mtime_ns for p in top.rglob("*") if p.is_file()} == made and not (tmp_path / "new").exists()


def test_the_books_are_plain_files_and_the_rest_is_in_a_zip(library, top, settings, capsys):
    run(library, top)
    out = capsys.readouterr().out
    assert "are copied and checked" in out and "Saved:" in out and "delete" not in out
    copied = books(top / "library")
    assert copied["comics/A Comic 1.cbz"] == b"pages of a comic"
    assert not {p for p in copied if p.split("/")[0] in ("state", "curation", "_ToBeRemoved")}
    assert not {p for p in copied if p.startswith("catalog/") and not p.startswith("catalog/covers/")}
    assert copied["catalog/covers/made-up.webp"] == b"a tiny cover"
    saved = in_the_zip(top)
    assert not [n for n in saved if n.startswith("catalog/covers")] and "catalog/data.js" in saved
    assert saved["curation/edits.jsonl"] == b'{"made": "up"}\n'
    assert any(n.startswith("state/") for n in saved) and any(n.startswith("catalog/") for n in saved)
    assert not [n for n in saved if n.startswith("_ToBeRemoved")]
    assert (top / backup.NOTE).read_text(encoding="utf-8").count("hylly restore") == 2


def test_a_file_still_being_downloaded_or_copied_stays_out_of_the_books_and_is_listed(library, top, settings, capsys):
    """A download's .part and a copy's .copying (and Hylly's other temporary endings, paths.TEMPORARY) are half made:
    copied, they would stay in the backup for good, where nothing is deleted, and come back with a restore, and one
    still growing failed the backup. They are left out, said as such, and the backup is whole without them."""
    for name in ("Big Book.pdf.part", "New.epub.copying", "Old.epub.seed", "notes.TMP"):
        (library / "comics" / name).write_bytes(b"half made")
    (library / "comics" / "Annual.part").mkdir()  # a folder of such a name is no such file: it comes, with its books
    (library / "comics" / "Annual.part" / "Annual 1.cbz").write_bytes(b"a whole annual")
    run(library, top, "--dry-run")
    assert "4 \u00d7 still being downloaded or copied" in capsys.readouterr().out
    run(library, top)  # no failure: they don't count against it
    copied = books(top / "library")
    assert copied["comics/A Comic 1.cbz"] == b"pages of a comic" and not [p for p in copied if p.endswith("half made")]
    assert not [p for p in copied if p.lower().endswith((".part", ".copying", ".seed", ".tmp"))]
    assert copied["comics/Annual.part/Annual 1.cbz"] == b"a whole annual"
    assert "Left out" in capsys.readouterr().out


def test_the_plan_says_what_goes_into_the_zip_instead_of_among_the_books(library, top, settings, capsys):
    """The dry run's Left out line once said "6 × in the zip", which made sense only after its last line: what goes
    into the zip is no loss, and says so; the other kinds keep their count."""
    (library / "comics" / "Big Book.pdf.part").write_bytes(b"half made")
    run(library, top, "--dry-run")
    left = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("Left out: "))
    assert re.search(r"\b\d+ files and folders go into the zip instead\b", left) and "× in the zip" not in left
    assert "1 × still being downloaded or copied" in left


def test_logins_keys_sessions_and_the_phones_topic_never_go_in(library, top, settings, monkeypatch):
    """The phone's ntfy address and token are in the settings folder's secrets/, which no copy takes. The server's
    nightly copies of the settings made before, with them left out as the old rewriter did, go in as they are: no
    secret is in them, and the backup is whole."""
    phone_set(settings, monkeypatch)
    earlier = backup.settings_backups(library) / "2026-11-05"
    earlier.mkdir(parents=True)
    (earlier / "settings.toml").write_text(OLD_COPY, encoding="utf-8")
    run(library, top)  # whole: one that isn't ends with exit code 1
    saved = in_the_zip(top)
    assert saved["state/settings-backup/2026-11-05/settings.toml"] == (earlier / "settings.toml").read_bytes()
    assert saved["settings/settings.toml"].startswith(b"[server]")
    assert "settings/users.txt" not in saved  # the users are in the database's nightly copy: users.txt from before would bring back users removed since
    assert saved["settings/rules/mine.toml"] == b"# my rules\n"
    assert not [n for n in saved if b"A-MADE-UP-SECRET" in saved[n] or b"AMADEUPSECRET" in saved[n]]
    assert {n for n in saved if n.startswith("settings/")} == {"settings/settings.toml", "settings/rules/mine.toml"}
    kept = no_secret_in(saved["settings/settings.toml"])
    assert kept["bell"] == {"wrong_passwords": 3} and kept["server"]["name"] == "Made-up Library"


def test_the_rules_go_from_where_they_are_kept_and_the_owners_themes_too(library, top, settings, monkeypatch, tmp_path, capsys):
    """rules = or HYLLY_RULES may keep the rules in any folder, and the owner's own themes are beside the settings: the
    zip, the server's nightly copy and a restore have them, by name, and the plan says so."""
    phone_set(settings, monkeypatch)
    notes_kept(monkeypatch)
    (settings / "themes").mkdir()
    (settings / "themes" / "mine.toml").write_bytes(b"# my own colours\n")
    (settings / "themes" / "notes.txt").write_bytes(b"not a theme")
    shared = tmp_path / "shared-rules"  # every computer's rules, in a folder of their own
    shared.mkdir()
    (shared / "ours.toml").write_bytes(b"# our rules\n")
    (shared / "humble-cookies.json").write_text("A-MADE-UP-SECRET", encoding="utf-8")  # not rules: never taken
    monkeypatch.setenv("HYLLY_RULES", str(shared))
    run(library, top, "--dry-run")
    plan = " ".join(capsys.readouterr().out.split())
    assert "Of this computer's settings, it takes settings.toml;" in plan
    assert f"1 rules file from {shared};" in plan and f"1 theme of your own from {settings / 'themes'}." in plan
    run(library, top)
    saved = in_the_zip(top)
    assert {n for n in saved if n.startswith("settings/")} == {"settings/settings.toml", "settings/rules/ours.toml", "settings/themes/mine.toml"}
    assert saved["settings/rules/ours.toml"] == b"# our rules\n" and saved["settings/themes/mine.toml"] == b"# my own colours\n"
    assert not [n for n in saved if b"A-MADE-UP-SECRET" in saved[n]]
    made = backup.settings_tonight(library, datetime(2026, 11, 6, 4))
    assert sorted(p.relative_to(made).as_posix() for p in made.rglob("*") if p.is_file()) == ["rules/ours.toml", "settings.toml", "themes/mine.toml"]
    no_secret_in((made / "settings.toml").read_bytes())
    to = tmp_path / "restored"
    cli.main(["restore", str(top), str(to)])
    assert (to / "_settings-from-backup" / "themes" / "mine.toml").read_bytes() == b"# my own colours\n"
    assert "settings.toml, rules and themes" in " ".join(capsys.readouterr().out.split())


def test_with_a_folder_for_each_kind_a_backup_takes_the_settings_and_never_the_state_the_cache_or_the_logins(library, top, monkeypatch, tmp_path):
    """Hylly's own files kept apart by kind (paths.PLACES): of the settings folder, settings.toml, the rules and the
    owner's themes; the database by its fresh copy in the library; of the state folder (logs, locks and notes, the
    alarms' memory, what long jobs said), the cache and the logins, nothing, however rules = is pointed at them."""
    import shutil

    from hylly import db, rules
    from hylly import settings as the_settings

    before = db.file()  # the library's records, which go with the database
    db.close_all()
    monkeypatch.delenv("HYLLY_HOME")
    monkeypatch.delenv("HYLLY_SETTINGS")
    for kind in ("config", "data", "state", "cache"):
        monkeypatch.setenv(f"HYLLY_{kind.upper()}_DIR", str(tmp_path / kind))
        (tmp_path / kind).mkdir()
    if before.is_file():
        shutil.copyfile(before, db.file())
    if db.token_file(before).is_file():  # this install's own, beside it: the folder moved, not a copy of the database
        shutil.copyfile(db.token_file(before), db.token_file())
    config, state, cache = tmp_path / "config", tmp_path / "state", tmp_path / "cache"
    (config / "settings.toml").write_text('[server]\nname = "Made-up Library"\n', encoding="utf-8")
    for folder, name in ((config / "rules", "mine.toml"), (config / "themes", "mine.toml"), (state, "kept.toml"),
                         (cache / "pages", "kept.toml"), (paths.logins_dir(), "keys.toml"),
                         (state / "job-output", "said.toml")):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text('comicvine = "A-MADE-UP-SECRET"\n' if "logins" in str(folder) else "# made up\n", encoding="utf-8")
    (state / "server.log").write_text("A-MADE-UP-SECRET\n", encoding="utf-8")
    the_settings.reload()
    rules.reset()
    assert paths.settings_file() == config / "settings.toml" and db.file() == tmp_path / "data" / db.FILE
    found, missed = backup._settings_to_save()
    assert sorted(inside for _, inside in found) == ["rules/mine.toml", "settings.toml", "themes/mine.toml"] and missed == []
    for wrong in (state, cache / "pages", paths.logins_dir(), state / "job-output"):  # the logins: with the data, or on
        # Windows in the install's one folder of before, which LOCALAPPDATA holds there
        (config / "settings.toml").write_text(f"rules = '{wrong}'\n", encoding="utf-8")
        the_settings.reload()
        found, missed = backup._settings_to_save()
        assert [inside for _, inside in found if inside.startswith("rules/")] == [], wrong
        assert "keep the rules in a folder of their own" in missed[0], wrong
    (config / "settings.toml").write_text('[server]\nname = "Made-up Library"\n', encoding="utf-8")
    the_settings.reload()
    run(library, top)
    saved = in_the_zip(top)
    assert {n for n in saved if n.startswith("settings/")} == {"settings/settings.toml", "settings/rules/mine.toml", "settings/themes/mine.toml"}
    assert not [n for n in saved if b"A-MADE-UP-SECRET" in saved[n]] and any(n.endswith("/" + db.FILE) for n in saved)


def test_rules_in_one_file_or_beside_another_settings_file_go_too_and_the_settings_folders_own_files_never(library, settings, monkeypatch, tmp_path):
    def taken() -> tuple[list[str], list[str]]:
        found, missed = backup._settings_to_save()
        return [inside for _, inside in found], missed

    one = tmp_path / "my-rules.toml"
    one.write_bytes(b"# all my rules\n")
    (settings / "settings.toml").write_text(f"rules = '{one}'\n", encoding="utf-8")
    assert taken() == (["settings.toml", "rules/my-rules.toml"], [])
    (settings / "logins").mkdir()
    (settings / "logins" / "keys.toml").write_text('comicvine = "A-MADE-UP-SECRET"\n', encoding="utf-8")
    (settings / "keys.toml").write_text('comicvine = "A-MADE-UP-SECRET"\n', encoding="utf-8")
    for wrong in (settings, settings / "logins", settings / "settings.toml"):  # never the logins, nor settings.toml as it is
        (settings / "settings.toml").write_text(f"rules = '{wrong}'\n", encoding="utf-8")
        names, missed = taken()
        assert names == ["settings.toml"] and "keep the rules in a folder of their own" in missed[0], wrong
    (settings / "settings.toml").write_text(f"rules = '{tmp_path / 'gone'}'\n", encoding="utf-8")
    names, missed = taken()
    assert names == ["settings.toml"] and "there's no such file or folder" in missed[0]
    with pytest.raises(OSError, match="not all of them: rules, from .*no such file or folder"):  # said, as the server says it
        backup.settings_tonight(library, datetime(2026, 11, 6, 4))
    assert (backup.settings_backups(library) / "2026-11-06" / "settings.toml").is_file()  # and the rest copied all the same
    (settings / "settings.toml").write_text("rules = [\n", encoding="utf-8")  # unreadable: nothing says where the rules are
    names, missed = taken()
    assert names == ["settings.toml"] and missed[0].startswith("rules: ")
    elsewhere = tmp_path / "elsewhere"  # HYLLY_SETTINGS: the rules are beside that file
    (elsewhere / "rules").mkdir(parents=True)
    (elsewhere / "rules" / "theirs.toml").write_bytes(b"# rules beside it\n")
    (elsewhere / "settings.toml").write_text("[server]\n", encoding="utf-8")
    monkeypatch.setenv("HYLLY_SETTINGS", str(elsewhere / "settings.toml"))
    assert taken() == (["settings.toml", "rules/theirs.toml"], [])


def test_a_copy_of_the_settings_is_as_written_and_one_with_the_old_secrets_in_it_is_refused():
    """settings.toml holds no secrets, so a copy is the file as it is (Notepad's start and Windows' line ends too). One
    that still sets the ntfy address or token under [bell], however it's written, as before they had files of their
    own, isn't copied at all; nor is one that can't be read as TOML, as nothing then says what it holds. An empty one is
    no secret: the server's nightly copies made before have them so, with the old rewriter's note (OLD_COPY)."""
    usual = b"\xef\xbb\xbf" + PHONE.replace("\n", "\r\n").encode("utf-8")
    for clean in (usual, b'[bell]\n# ntfy = ""\n', b"[server]\nport = 8770\n", b"", b'[server]\nntfy = "mine"\n',
                  OLD_COPY.encode("utf-8"), OLD_COPY.replace("\n", "\r\n").encode("utf-8"), b"bell.ntfy = ''  # off\n"):
        assert backup.no_secrets(clean) is clean
    dotted = b'bell.ntfy = "https://ntfy.sh/A-MADE-UP-SECRET"\nbell.ntfy_token = "A-MADE-UP-SECRET"\n'
    quoted = b'[ "bell" ]\n  \'ntfy\' = "https://ntfy.sh/A-MADE-UP-SECRET"\n"ntfy_token"="A-MADE-UP-SECRET"#\n'
    inline = b'bell = { ntfy = "https://ntfy.sh/A-MADE-UP-SECRET" }\n'
    for old in (OLD_PHONE.encode("utf-8"), dotted, quoted, inline):
        with pytest.raises(OSError, match="still has the alarms' ntfy address or token under \\[bell\\], so it wasn't "
                                          "copied: put them in secrets/ntfy-url and secrets/ntfy-token") as refused:
            backup.no_secrets(old, "settings.toml")
        assert "MADE-UP" not in str(refused.value)
    with pytest.raises(OSError, match="can't be read as TOML"):
        backup.no_secrets(b'[bell\nntfy = "https://ntfy.sh/A-MADE-UP-SECRET"\n')


def test_settings_with_the_old_secrets_in_them_are_not_copied_and_the_backup_says_so(library, top, settings, monkeypatch, capsys):
    """Set by hand under [bell], where they are no longer read, which the settings say, and a nightly copy of such
    settings from before: neither goes in the zip, and the backup isn't whole."""
    phone_set(settings, monkeypatch, 'bell = { ntfy = "https://ntfy.sh/A-MADE-UP-SECRET-TOPIC" }\n')
    said = notes_kept(monkeypatch)  # not sent: one the phone didn't get would be tried for an hour
    earlier = backup.settings_backups(library) / "2026-11-05"
    earlier.mkdir(parents=True)
    (earlier / "settings.toml").write_text(OLD_PHONE, encoding="utf-8")  # as an older Hylly kept them
    assert code_of(top, "--yes", "--library", library) == PARTLY
    out, err = capsys.readouterr()
    assert "[bell] ntfy is no longer used, so it is left out. It is a secret, kept in a file of its own now" in err
    assert "Not in the zip: settings/settings.toml" in out and "Not in the zip: state/settings-backup/2026-11-05/settings.toml" in out
    assert f"{earlier / 'settings.toml'} still has the alarms' ntfy address" in out and "\\\\?\\" not in out  # as it's written
    assert "Hylly backup: not all in the zip" in [title for title, _, _ in said]
    shutil.rmtree(earlier)
    saved = in_the_zip(top)
    assert "settings/settings.toml" not in saved and not [n for n in saved if b"A-MADE-UP-SECRET" in saved[n]]
    with pytest.raises(OSError, match="still has the alarms' ntfy address"):
        backup.settings_tonight(library, datetime(2026, 11, 6, 4))
    assert not list(backup.settings_backups(library).iterdir())  # no copy, rather than one with the topic in it


def test_the_second_backup_copies_only_what_is_new_and_makes_a_new_zip(library, top, settings, capsys):
    run(library, top)
    capsys.readouterr()
    first = backup.saved_zips(top)
    (library / "comics" / "A Comic 2.cbz").write_bytes(b"a new one")
    (library / "curation" / "edits.jsonl").write_bytes(b'{"made": "up"}\n{"another": "edit"}\n')
    written = []
    copy_file = hc.Copying._copy_file
    hc.Copying._copy_file = lambda self, rel, listed, result: (written.append(rel), copy_file(self, rel, listed, result))[1]
    try:
        run(library, top)
    finally:
        hc.Copying._copy_file = copy_file
    assert written == ["comics/A Comic 2.cbz"]
    zips = backup.saved_zips(top)
    assert len(zips) == len(first) + 1 and first[0] in zips
    assert b"another" in in_the_zip(top)["curation/edits.jsonl"]


def test_a_new_catalog_changes_nothing_among_the_books(library, top, settings):
    run(library, top)
    (library / "catalog" / "data.js").write_text("window.CATALOG = {};", encoding="utf-8")
    (library / "catalog" / "covers" / "another.webp").write_bytes(b"one more")
    run(library, top)
    assert not (top / "changed").exists() and (top / "library" / "catalog" / "covers" / "another.webp").is_file()
    assert in_the_zip(top)["catalog/data.js"] == b"window.CATALOG = {};"


def test_a_book_gone_from_the_library_stays_in_the_backup(library, top, settings):
    run(library, top)
    (library / "comics" / "A Comic 1.cbz").unlink()
    run(library, top)
    assert (top / "library" / "comics" / "A Comic 1.cbz").read_bytes() == b"pages of a comic"


def test_a_changed_book_gets_its_new_copy_and_the_old_one_is_set_aside(library, top, settings, capsys, today):
    run(library, top)
    book = library / "comics" / "A Comic 1.cbz"
    book.write_bytes(b"pages of a comic, changed")
    run(library, top)
    assert (top / "library" / "comics" / "A Comic 1.cbz").read_bytes() == b"pages of a comic, changed"
    day = today.strftime("%Y-%m-%d")
    assert (top / "changed" / day / "comics" / "A Comic 1.cbz").read_bytes() == b"pages of a comic"
    assert "earlier copies are in changed" in capsys.readouterr().out
    book.write_bytes(b"and changed again today")
    run(library, top)
    assert (top / "library" / "comics" / "A Comic 1.cbz").read_bytes() == b"and changed again today"
    assert (top / "changed" / day / "comics" / "A Comic 1 (2).cbz").read_bytes() == b"pages of a comic, changed"
    check(top)
    assert "read back the same" in capsys.readouterr().out


def test_an_earlier_copy_set_aside_keeps_its_checksum_and_the_check_reads_it_again(library, top, settings, capsys, today):
    """The earlier copy is the one that matters when the library's file was damaged: its checksum as it was copied goes
    into changed/manifest.sha256, as sha256sum writes it, and hylly backup check reads it again. One set aside by an
    older Hylly, with no checksum, is counted, not taken for damaged."""
    run(library, top)
    (library / "comics" / "A Comic 1.cbz").write_bytes(b"pages of a comic, changed")
    run(library, top)
    day = today.strftime("%Y-%m-%d")
    earlier = top / "changed" / day / "comics" / "A Comic 1.cbz"
    sums = (top / "changed" / backup.MANIFEST).read_text(encoding="utf-8")
    assert sums == f"{hashlib.sha256(b'pages of a comic').hexdigest()}  {day}/comics/A Comic 1.cbz\n"
    older = top / "changed" / "2026-10-01" / "comics" / "Another Comic.cbz"  # set aside before checksums were noted
    older.parent.mkdir(parents=True)
    older.write_bytes(b"an earlier copy")
    capsys.readouterr()
    check(top)
    out = " ".join(capsys.readouterr().out.split())
    assert "Earlier copies in changed read back: 1." in out and "1 earlier copy there has no checksum" in out
    cli.main(["backup", "check", str(top), "--dry-run"])
    assert "and 1 earlier copy in" in capsys.readouterr().out
    earlier.write_bytes(b"pages of a c0mic")  # damaged on the backup's disk
    assert code_of("check", top) == PARTLY
    damaged = f"changed/{day}/comics/A Comic 1.cbz: its contents have changed"
    assert damaged in capsys.readouterr().out and damaged in (top / "library" / hc.FOLDER / "checked.txt").read_text(encoding="utf-8")
    earlier.unlink()
    assert code_of("check", top) == PARTLY and "A Comic 1.cbz: it's missing" in capsys.readouterr().out
    (top / "library" / hc.FOLDER / "record.jsonl").unlink()  # a record lost: the earlier copy's checksum is read from it
    (library / "comics" / "A Comic 1.cbz").write_bytes(b"and changed again")
    run(library, top)
    last = (top / "changed" / backup.MANIFEST).read_text(encoding="utf-8").splitlines()[-1]
    assert last == f"{hashlib.sha256(b'pages of a comic, changed').hexdigest()}  {day}/comics/A Comic 1.cbz"
    check(top)  # the last line for a place counts


@mount_folder
def test_the_check_of_the_earlier_copies_holds_while_the_disk_is_away_as_the_books_check_does(library, top, settings,
                                                                                               monkeypatch, tmp_path):
    """A disk that drops as the earlier copies in changed/ are read again isn't taken for copies gone: the check holds
    till it's back, as it does for the books, and finds them as they were."""
    run(library, top)
    (library / "comics" / "A Comic 1.cbz").write_bytes(b"pages of a comic, changed")
    run(library, top)
    real_stat, away, held = os.stat, tmp_path / "away", []

    def stat(path, *a, **k):  # the disk goes as the first earlier copy is looked at
        if not held and str(path).startswith(str(top / "changed") + os.sep) and not os.path.lexists(away):
            top.rename(away)
            top.mkdir()
        return real_stat(path, *a, **k)

    def hold(self, side, e):
        held.append(1)
        top.rmdir()
        away.rename(top)  # and it's back

    with monkeypatch.context() as m:
        m.setattr(os, "stat", stat)
        m.setattr(hc._Job, "_hold", hold)
        check(top)
    assert held == [1] and "Problems: 0." in (top / "library" / hc.FOLDER / "checked.txt").read_text(encoding="utf-8")


def test_the_same_change_of_contents_with_the_same_size_is_seen(library, top, settings):
    run(library, top)
    book = library / "comics" / "A Comic 1.cbz"
    book.write_bytes(b"PAGES OF A COMIC")
    os.utime(book, (book.stat().st_atime, book.stat().st_mtime + 10))
    run(library, top)
    assert (top / "library" / "comics" / "A Comic 1.cbz").read_bytes() == b"PAGES OF A COMIC"


def test_a_changed_book_that_cant_be_read_leaves_the_good_copy_where_it_was(library, top, settings, monkeypatch, tmp_path, capsys):
    """A failing library disk while a changed book's new copy is made: the backup's good copy stays in library/, where a
    restore takes it from, as the earlier copy is set aside only once the new one is whole and checked."""
    said = notes_kept(monkeypatch)
    run(library, top)
    (library / "comics" / "A Comic 1.cbz").write_bytes(b"pages of a")  # cut short in the library
    write_part = hc.Copying._write_part

    def unreadable(self, src, part, other):
        if src.endswith("A Comic 1.cbz"):
            raise hc.FileProblem("Input/output error", again=False)  # as a read error on the library's disk ends
        return write_part(self, src, part, other)

    monkeypatch.setattr(hc.Copying, "_write_part", unreadable)
    assert code_of(top, "--yes", "--library", library) == PARTLY
    shown = capsys.readouterr()
    out = shown.out + shown.err  # the end on standard output, the file's trouble as it happened on standard error
    assert "1 aren't" in shown.out and "Input/output error" in out and "earlier copies are in changed" not in out
    assert "Hylly backup: finished, not all of it" in [title for title, _, _ in said]
    assert (top / "library" / "comics" / "A Comic 1.cbz").read_bytes() == b"pages of a comic"
    assert not (top / "changed").exists()
    monkeypatch.setattr(hc.Copying, "_write_part", write_part)
    check(top)
    assert "read back the same" in capsys.readouterr().out  # the backup's record still tells of the good copy
    cli.main(["restore", str(top), str(tmp_path / "restored")])
    assert (tmp_path / "restored" / "comics" / "A Comic 1.cbz").read_bytes() == b"pages of a comic"
    assert "Not restored" not in capsys.readouterr().out


def test_a_new_copy_that_cant_take_its_name_puts_the_earlier_one_back(library, top, settings, monkeypatch):
    run(library, top)
    (library / "comics" / "A Comic 1.cbz").write_bytes(b"pages of a comic, changed")
    name_it = hc.Copying._name_it

    def refused(self, rel, part, dst, sha):
        if rel == "comics/A Comic 1.cbz":
            raise hc.FileProblem("its path is too long for this share", again=False)
        return name_it(self, rel, part, dst, sha)

    monkeypatch.setattr(hc.Copying, "_name_it", refused)
    assert code_of(top, "--yes", "--library", library) == PARTLY
    assert (top / "library" / "comics" / "A Comic 1.cbz").read_bytes() == b"pages of a comic"
    assert not backup.set_aside_days(top) and not backup.earlier_copies(top)
    monkeypatch.setattr(hc.Copying, "_name_it", name_it)
    run(library, top)
    assert (top / "library" / "comics" / "A Comic 1.cbz").read_bytes() == b"pages of a comic, changed"
    assert list(backup.set_aside_days(top).values()) == [1]


def test_a_stop_between_the_two_loses_nothing_and_a_restore_says_where_earlier_copies_are(library, top, settings, monkeypatch, tmp_path, capsys,
                                                                                          today):
    run(library, top)
    (library / "comics" / "A Comic 1.cbz").write_bytes(b"pages of a comic, changed")
    older = top / "changed" / "2026-11-01" / "comics" / "Another Comic.cbz"  # set aside by an earlier backup
    older.parent.mkdir(parents=True)
    older.write_bytes(b"an earlier copy")
    name_it = hc.Copying._name_it

    def power_cut(self, rel, part, dst, sha):
        raise KeyboardInterrupt

    monkeypatch.setattr(hc.Copying, "_name_it", power_cut)
    with pytest.raises((KeyboardInterrupt, SystemExit)):
        run(library, top)
    monkeypatch.setattr(hc.Copying, "_name_it", name_it)
    day = today.strftime("%Y-%m-%d")
    assert (top / "changed" / day / "comics" / "A Comic 1.cbz").read_bytes() == b"pages of a comic"
    capsys.readouterr()
    to = tmp_path / "restored"
    cli.main(["restore", str(top), str(to), "--dry-run"])
    said = f"holds 2 earlier copies of books that changed in the library, set aside on 2026-11-01, {day}."
    assert said in " ".join(capsys.readouterr().out.split()) and not to.exists()
    cli.main(["restore", str(top), str(to)])
    end = " ".join(capsys.readouterr().out.split())
    assert f"Not restored: {top / 'changed'} {said}" in end and "missing or not as it should be" in end
    run(library, top)  # the next backup goes on: the new copy, whole in the parts, takes its name
    assert (top / "library" / "comics" / "A Comic 1.cbz").read_bytes() == b"pages of a comic, changed"
    assert backup.set_aside_days(top) == {"2026-11-01": 1, day: 1}


def test_a_plain_copy_still_refuses_to_write_over_a_different_file(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    (source).mkdir()
    (source / "a.pdf").write_bytes(b"one")
    cli.main(["copy", str(source), str(target), "--yes"])
    (source / "a.pdf").write_bytes(b"another")
    with pytest.raises(SystemExit):
        cli.main(["copy", str(source), str(target), "--yes"])
    assert (target / "a.pdf").read_bytes() == b"one"


def test_a_stopped_backup_goes_on_from_where_it_was(library, top, settings, monkeypatch):
    calls = []
    copy_file = hc.Copying._copy_file

    def cut_short(self, rel, listed, result):
        calls.append(rel)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return copy_file(self, rel, listed, result)

    monkeypatch.setattr(hc.Copying, "_copy_file", cut_short)
    with pytest.raises((KeyboardInterrupt, SystemExit)):
        run(library, top)
    assert not backup.saved_zips(top) and not backup.last(library)
    monkeypatch.setattr(hc.Copying, "_copy_file", copy_file)
    run(library, top)
    assert backup.saved_zips(top) and backup.last(library)
    assert (top / "library" / "comics" / "A Comic 1.cbz").is_file()


def test_a_library_gone_is_not_believed(library, top, settings):
    run(library, top)
    before = books(top / "library")
    for path in [p for p in library.rglob("*") if p.is_file() and "state" not in p.parts and "catalog" not in p.parts]:
        path.unlink()
    code = code_of(top, "--library", library, "--yes")
    assert code != 0 and books(top / "library") == before


def test_the_library_remembers_and_the_doctor_tells(library, top, settings):
    text = json.dumps(doctor._backup(library))
    assert "None made yet" in text and doctor.PROBLEM not in text
    run(library, top)
    done = backup.last(library)
    assert done["target"] == str(top) and done["files"] >= 1 and done["saved"].endswith(".zip")
    text = json.dumps(doctor._backup(library))
    assert "files" in text and doctor.PROBLEM not in text
    old = dict(done, when=(datetime.now() - timedelta(days=40)).isoformat(timespec="seconds"))
    backup.record_file(library).write_text(json.dumps(old), encoding="utf-8")
    assert doctor.PROBLEM in json.dumps(doctor._backup(library))


def test_a_zip_read_back_wrong_is_not_given_its_name(library, top, settings, monkeypatch):
    top.mkdir(parents=True)  # as the copy of the books leaves it: saved/ is never made above it
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda self: "state/one.json")
    with pytest.raises(OSError):
        backup.save_the_rest(library, top, sleep=lambda s: None)
    assert not backup.saved_zips(top) and not list(backup.saved_of(top).glob("*.part"))


def test_a_share_that_goes_away_while_zipping_is_tried_again(library, top, settings):
    top.mkdir(parents=True)
    goes = []
    real = backup._write_zip

    def once(root, part, night=None):
        goes.append(1)
        if len(goes) == 1:
            raise OSError("the share went away")
        return real(root, part, night)

    backup._write_zip = once
    try:
        made, missed = backup.save_the_rest(library, top, sleep=lambda s: None)
    finally:
        backup._write_zip = real
    assert len(goes) == 2 and made.is_file() and not missed


@mount_folder
def test_once_the_books_are_copied_a_backup_holds_while_its_disk_is_away_and_writes_nothing_where_it_was(library, top, settings,
                                                                                                         monkeypatch, tmp_path):
    """A disk dropping between the last book and the zip (the records' fresh copy may take minutes) leaves an empty
    folder where it was mounted, which would take the zip, password hashes and all, on the system's own disk. Its mark
    is gone with it: the backup holds as the copy does, and goes on once it's back."""
    run(library, top)
    (library / "comics" / "A New One.cbz").write_bytes(b"new pages")
    copy_run, away, held = hc.Copying.run, tmp_path / "away", []

    def then_unmounted(self):
        result = copy_run(self)
        top.rename(away)  # the disk gone, and the folder it was mounted at left empty
        top.mkdir()
        return result

    def hold(self, side, e):
        held.append(sorted(p.name for p in top.iterdir()))  # what the empty folder got meanwhile
        top.rmdir()
        away.rename(top)  # and it's back

    monkeypatch.setattr(hc.Copying, "run", then_unmounted)
    monkeypatch.setattr(hc._Job, "_hold", hold)
    run(library, top)
    assert held == [[]] and len(backup.saved_zips(top)) == 2 and "Last finished" in (top / backup.NOTE).read_text(encoding="utf-8")


@mount_folder
def test_a_book_s_earlier_copy_waits_for_the_disk_and_makes_no_folder_where_it_was(library, top, settings, monkeypatch,
                                                                                    tmp_path, today):
    """Setting a changed book's earlier copy aside makes changed/<day>/ and the folders below it: only while the disk
    answers, and only below the target, so a disk gone just then leaves no empty folders on the place it was mounted."""
    run(library, top)
    (library / "comics" / "A Comic 1.cbz").write_bytes(b"pages of a comic, changed")
    set_aside, away, held = backup.BackingUp._set_aside, tmp_path / "away", []

    def as_the_disk_goes(self, rel, dst, name_it):
        top.rename(away)  # the disk gone, and the folder it was mounted at left empty
        top.mkdir()
        return set_aside(self, rel, dst, name_it)

    def hold(self, side, e):
        held.append(sorted(p.name for p in top.iterdir()))
        shutil.rmtree(top)
        away.rename(top)  # and it's back

    monkeypatch.setattr(backup.BackingUp, "_set_aside", as_the_disk_goes)
    monkeypatch.setattr(hc._Job, "_hold", hold)
    run(library, top)
    day = today.strftime("%Y-%m-%d")
    assert held and held[0] == [] and (top / "changed" / day / "comics" / "A Comic 1.cbz").read_bytes() == b"pages of a comic"


def test_a_zip_s_part_a_drop_left_goes_at_the_next_backup_and_only_the_backups_own(library, top, settings):
    """A disk that went away mid-zip leaves the part, password hashes and all: the next backup removes it, once nothing
    has written to it for a while (another computer's backup to this disk may be writing its own), and of the
    backups' own names only."""
    run(library, top)
    saved = backup.saved_of(top)
    for name in ("2026-01-01 030000.zip.part", "2026-01-02 030000 (2).zip.part", "Ann Writer's.zip.part"):
        (saved / name).write_bytes(b"half a zip")
        os.utime(saved / name, (1_700_000_000, 1_700_000_000))
    (saved / "2026-01-03 030000.zip.part").write_bytes(b"being written")  # just now
    run(library, top)
    assert sorted(p.name for p in saved.glob("*.part")) == ["2026-01-03 030000.zip.part", "Ann Writer's.zip.part"]


def test_a_zip_is_never_left_or_named_where_the_backup_s_disk_was(library, top, settings, monkeypatch):
    """Asked before saved/ is made, and again before the zip takes its name: one made meanwhile on the place an
    unmounted disk left is removed, as it holds the password hashes. Nor is the note written there."""
    top.mkdir(parents=True)
    with pytest.raises(OSError, match="isn't answering"):
        backup.save_the_rest(library, top, sleep=lambda s: None, there=lambda: False)
    assert not backup.saved_of(top).exists()
    answers = iter([True, False] * backup.ZIP_TRIES)
    with pytest.raises(OSError, match="isn't answering"):
        backup.save_the_rest(library, top, sleep=lambda s: None, there=lambda: next(answers))
    assert not list(backup.saved_of(top).iterdir())
    from hylly import storage

    monkeypatch.setattr(storage.time, "sleep", lambda s: None)  # the note's tries again, for a moment's trouble
    with pytest.raises(OSError, match="isn't answering"):
        backup.note_on_the_disk(top, "a note", there=lambda: False)
    assert not (top / backup.NOTE).exists()


# ---- checking and getting it back ------------------------------------------------------------------------------------------------

def test_verify_finds_a_book_and_a_zip_gone_bad(library, top, settings, capsys):
    run(library, top)
    check(top)
    out = capsys.readouterr().out
    assert "read back the same" in out and "Zips read back: 1" in out
    (top / "library" / "comics" / "A Comic 1.cbz").write_bytes(b"pages of a c0mic")
    made = backup.saved_zips(top)[-1]
    data = bytearray(made.read_bytes())
    data[100] ^= 0xFF
    made.write_bytes(bytes(data))
    assert code_of("check", top) != 0
    out = capsys.readouterr().out
    assert "aren't as they were" in out and "not as they were" in out


def test_a_check_reads_back_the_newest_zips_unless_all_and_the_plan_counts_them(library, top, settings, capsys):
    """A zip for each backup builds up, 365 a year when nightly: each is whole by itself, so the newest are read back,
    --all reads every one, the plan says how many there are and how big, and the note says older ones may go by hand."""
    run(library, top)
    newest = backup.saved_zips(top)[-1]
    for name in ("2026-01-01 030000.zip", "2026-01-02 030000.zip", "2026-01-03 030000.zip"):
        shutil.copyfile(newest, backup.saved_of(top) / name)
    oldest = backup.saved_zips(top)[0]
    data = bytearray(oldest.read_bytes())
    data[100] ^= 0xFF
    oldest.write_bytes(bytes(data))
    capsys.readouterr()
    check(top)
    assert "Zips read back: 3, the newest of 4 (--all reads every one)." in capsys.readouterr().out
    assert code_of("check", top, "--all") == PARTLY
    out = capsys.readouterr().out
    assert "Zips read back: 4. 1 of them not as they were:" in out and oldest.name in out
    cli.main(["backup", "check", str(top), "--dry-run"])
    assert "the newest 3 of the 4 zips" in capsys.readouterr().out
    run(library, top, "--dry-run")
    size = words.size_words(sum(p.stat().st_size for p in backup.saved_zips(top)))
    assert f"On the disk now: 4 zips in saved, {size} in all, the newest" in capsys.readouterr().out
    assert "be deleted by hand" in " ".join((top / backup.NOTE).read_text(encoding="utf-8").split())


def test_a_zip_too_damaged_for_zlib_itself_is_told_of_not_a_crash(library, top, settings, capsys, monkeypatch):
    """Where a damaged byte lands decides whether zlib stops at once or a check finds it later, and that differs with the
    computer's zlib: on Linux the test above stopped zlib itself, which once ended verify with a traceback."""
    import zlib
    run(library, top)
    capsys.readouterr()

    def broken(self):
        raise zlib.error("Error -3 while decompressing data: invalid bit length repeat")

    monkeypatch.setattr(zipfile.ZipFile, "testzip", broken)
    assert code_of("check", top) != 0
    out = capsys.readouterr().out
    assert "1 of them not as they were" in out and "invalid bit length repeat" in out


def test_a_zip_is_read_back_one_way_when_made_checked_and_restored_each_in_its_own_words(library, top, settings,
                                                                                         tmp_path, monkeypatch):
    """backup._read_back, with what a zip cut short or damaged raises in one place (ZIP_DAMAGE): the zip made again
    (save_the_rest), a check's list (check_saved), a restore refused before the books (_reads_back), each saying it as
    it did."""
    run(library, top)
    made = backup.saved_zips(top)[-1]
    cut = tmp_path / "cut.zip"
    cut.write_bytes(made.read_bytes()[: made.stat().st_size // 2])
    assert backup._read_back(made) == (None, None) and isinstance(backup._read_back(cut)[1], backup.ZIP_DAMAGE)

    monkeypatch.setattr(backup, "_read_back", lambda path: ("state/one.json", None))
    with pytest.raises(OSError, match="state/one.json read back differently"):
        backup.save_the_rest(library, top, sleep=lambda s: None)
    assert backup.check_saved(top) == (1, [f"{made.name}: state/one.json isn't as it was"])
    with pytest.raises(SystemExit, match="state/one.json in it isn't as it was written"):
        backup._reads_back(made, top)

    monkeypatch.setattr(backup, "_read_back", lambda path: (None, zipfile.BadZipFile("File is not a zip file")))
    with pytest.raises(OSError, match=re.escape("it read back damaged (File is not a zip file)")):
        backup.save_the_rest(library, top, sleep=lambda s: None)
    assert backup.check_saved(top) == (1, [f"{made.name}: File is not a zip file"])
    with pytest.raises(SystemExit, match="can't be read back: File is not a zip file"):
        backup._reads_back(made, top)
    assert backup.saved_zips(top) == [made] and not list(backup.saved_of(top).glob("*.part"))


def test_a_library_that_looks_short_goes_by_hyllycopy_s_rule_for_most_of_a_source_lost(library, top, monkeypatch):
    """BackingUp.looks_short asks hyllycopy.lost_most, as HyllyCopy itself does: one threshold (SHRUNK_FROM) for
    both."""
    job = backup.BackingUp(library, top)
    job.record.entries = {f"{i}.epub": {} for i in range(5)}
    result = hc.Result(hc.Listing(files={"0.epub": (1, 1), "1.epub": (1, 1)}))
    assert job.looks_short(result) == ("The library has 2 files now, and this backup holds 5. Is all of it there? "
                                       "Nothing was deleted from the backup.")
    monkeypatch.setattr(hc, "SHRUNK_FROM", 6)
    assert job.looks_short(result) == ""


def test_it_all_comes_back_and_can_be_served(library, top, settings, tmp_path, capsys):
    run(library, top)
    to = tmp_path / "restored"
    cli.main(["restore", str(top), str(to), "--dry-run"])
    assert "only a dry run" in capsys.readouterr().out and not to.exists()
    cli.main(["restore", str(top), str(to)])
    out = capsys.readouterr().out
    assert "Unpacked" in out and "Next:" in out
    from hylly import settings as the_settings

    assert f"put the ntfy address in {the_settings.secrets_dir() / 'ntfy-url'}, and a token" in " ".join(out.split())  # never in a copy
    assert (to / "comics" / "A Comic 1.cbz").read_bytes() == b"pages of a comic"
    assert (to / "curation" / "edits.jsonl").read_bytes() == b'{"made": "up"}\n'
    assert (to / "catalog" / "covers" / "made-up.webp").read_bytes() == b"a tiny cover" and (to / "catalog" / "data.js").is_file()
    assert (to / "_settings-from-backup" / "settings.toml").is_file() and not (to / "_settings-from-backup" / "users.txt").exists()
    assert not paths.library_missing(to)
    assert Catalog(to).get() and len(Catalog(to).get()) == len(Catalog(library).get())


def test_a_restore_writes_over_nothing_and_goes_on(library, top, settings, tmp_path, monkeypatch):
    """Run again into its folder after a stop, it goes on: nothing there is written over, a change made since included.
    Into a folder with other files in it, or one it finished, which may be a library in use by now, it doesn't start at
    all: nothing of the backup is mixed in there, and a book removed since doesn't come back."""
    run(library, top)
    to = tmp_path / "restored"
    real_unpack = backup.unpack

    def stopped(made, folder, there=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(backup, "unpack", stopped)  # a stop after the books, before the zip
    assert exit_of("restore", top, to) == exits.INTERRUPTED and (to / backup.RESTORING).is_file()
    monkeypatch.setattr(backup, "unpack", real_unpack)
    (to / "curation").mkdir()
    (to / "curation" / "edits.jsonl").write_text("mine, newer\n", encoding="utf-8")
    cli.main(["restore", str(top), str(to)])  # the same command goes on
    assert (to / "curation" / "edits.jsonl").read_text(encoding="utf-8") == "mine, newer\n"
    assert not (to / backup.RESTORING).exists()
    written, kept = backup.unpack(backup.saved_zips(top)[-1], to)
    assert written == 0 and kept > 1
    (to / "comics" / "A Comic 1.cbz").unlink()  # the restored library, in use: a book removed from it
    assert exit_of("restore", top, to) == exits.USAGE and not (to / "comics" / "A Comic 1.cbz").exists()
    other = tmp_path / "my own folder"
    (other / "notes").mkdir(parents=True)
    (other / "notes" / "mine.txt").write_text("mine", encoding="utf-8")
    assert exit_of("restore", top, other) == exits.USAGE and exit_of("restore", top, other, "-n") == exits.USAGE
    assert sorted(p.name for p in other.rglob("*")) == ["mine.txt", "notes"]


@mount_folder
def test_a_restore_holds_while_the_new_library_s_disk_is_away_and_unpacks_nothing_where_it_was(library, top, settings,
                                                                                               monkeypatch, tmp_path):
    """As a backup does before its zip: the books copied, and the new folder's disk gone, so the mark the copy left
    there with it, the zip's records and password hashes aren't unpacked onto the place it leaves. Gone while it's
    unpacked, nothing more is written, and the same command goes on once it's back."""
    run(library, top)
    to, away, held = tmp_path / "restored", tmp_path / "away", []
    copy_run = hc.Copying.run

    def then_unmounted(self):
        result = copy_run(self)
        to.rename(away)
        to.mkdir()
        return result

    def hold(self, side, e):
        held.append(sorted(p.name for p in to.iterdir()))
        to.rmdir()
        away.rename(to)

    monkeypatch.setattr(hc.Copying, "run", then_unmounted)
    monkeypatch.setattr(hc._Job, "_hold", hold)
    cli.main(["restore", str(top), str(to)])
    assert held == [[]] and (to / "curation" / "edits.jsonl").read_bytes() == b'{"made": "up"}\n'
    made, again = tmp_path / "made-up.zip", tmp_path / "again"
    with zipfile.ZipFile(made, "w") as z:
        for n in (1, 2, 3):
            z.writestr(f"state/made-up-{n}.json", "{}")
    (again / hc.FOLDER).mkdir(parents=True)
    (again / hc.FOLDER / "id").write_text("a-made-up-mark\n", encoding="ascii")
    marks, read_mark = iter(["a-made-up-mark", "a-made-up-mark"]), hc._read_mark
    monkeypatch.setattr(hc, "_read_mark", lambda path: next(marks, ""))  # read at first and before the first file, then gone
    with pytest.raises(Exit) as e:
        backup.unpack(made, again)
    assert e.value.code == exits.LOCAL and "isn't answering" in e.value.message and "goes on" in e.value.message
    assert sorted(p.name for p in (again / "state").iterdir()) == ["made-up-1.json"]

    def unreadable(path):
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(hc, "_read_mark", unreadable)  # a mark that can't be read is a disk gone, not one with no mark
    with pytest.raises(Exit):
        backup.unpack(made, again)
    monkeypatch.setattr(hc, "_read_mark", read_mark)
    assert sorted(p.name for p in (again / "state").iterdir()) == ["made-up-1.json"]


def test_a_restore_unpacks_nothing_onto_the_folder_a_disk_gone_just_after_its_wait_leaves(library, top, settings,
                                                                                          monkeypatch, tmp_path, capsys):
    """The new library's disk answers when the books are copied, and is unmounted just after: its mark is asked before
    each file is unpacked, so none lands on the empty folder it leaves, hylly.db's copy and the settings least of all.
    Once it's back, the same command goes on there."""
    run(library, top)
    to, away = tmp_path / "restored", tmp_path / "away"
    wait_for = hc._Job.wait_for

    def then_unmounted(self, side):
        wait_for(self, side)
        to.rename(away)
        to.mkdir()

    monkeypatch.setattr(hc._Job, "wait_for", then_unmounted)
    assert exit_of("restore", top, to) == exits.LOCAL and "isn't answering" in capsys.readouterr().err
    assert not list(to.iterdir()) and not (away / "state").exists() and not (away / "_settings-from-backup").exists()
    monkeypatch.setattr(hc._Job, "wait_for", wait_for)
    to.rmdir()
    away.rename(to)  # it's back
    cli.main(["restore", str(top), str(to)])
    assert (to / "curation" / "edits.jsonl").read_bytes() == b'{"made": "up"}\n' and (to / "_settings-from-backup").is_dir()
    assert not (to / backup.RESTORING).exists()


def test_a_damaged_zip_is_said_before_the_books_are_copied_with_the_older_ones(library, top, settings, tmp_path, capsys):
    """Read back in the plan and before the copy, not found after hours of it: what's wrong, nothing copied, and the zips
    to choose from with --saved. One damaged after that is said as such, not in a traceback."""
    run(library, top)
    older = backup.saved_zips(top)[-1]
    backup.save_the_rest(library, top, now=datetime(2099, 1, 1))
    newest = backup.saved_zips(top)[-1]
    newest.write_bytes(newest.read_bytes()[: newest.stat().st_size // 2])  # cut short, as a disk pulled out mid-write leaves it
    to = tmp_path / "restored"
    capsys.readouterr()
    for dry in ([], ["-n"]):
        assert exit_of("restore", top, to, *dry) == exits.LOCAL
        err = capsys.readouterr().err
        assert newest.name in err and "can't be read back" in err and older.name in err and "--saved" in err and "Traceback" not in err
        assert not to.exists()
    cli.main(["restore", str(top), str(to), "--saved", older.stem])  # the older one, whole
    assert (to / "curation" / "edits.jsonl").is_file()
    again = tmp_path / "again"
    monkey = pytest.MonkeyPatch()
    monkey.setattr(backup, "_reads_back", lambda made, top: None)  # whole when read back, damaged since
    monkey.setattr(backup, "unpack", lambda made, to, there=None: (_ for _ in ()).throw(zipfile.BadZipFile("File is not a zip file")))
    try:
        assert exit_of("restore", top, again) == exits.LOCAL
    finally:
        monkey.undo()
    err = capsys.readouterr().err
    assert "The books are copied" in err and "--saved" in err and "Traceback" not in err and (again / backup.RESTORING).is_file()
    assert err.index("--saved") < err.index("same command")  # an older zip first: the same one is refused when read back


def test_a_restore_left_unfinished_goes_on_only_from_its_own_backup_and_while_untouched(library, top, settings, tmp_path, monkeypatch, capsys):
    """Gone on with only from the backup it began from (by the disk's mark, under any drive letter), and only while what it
    copied is still as copied: one that ended with failed files may be a library in use by now, and a book removed from it
    would come back."""
    run(library, top)
    other = tmp_path / "disk" / "another"
    run(library, other)
    to = tmp_path / "restored"
    real_unpack = backup.unpack
    monkeypatch.setattr(backup, "unpack", lambda made, folder, there=None: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert exit_of("restore", top, to) == exits.INTERRUPTED and (to / backup.RESTORING).is_file()
    monkeypatch.setattr(backup, "unpack", real_unpack)  # only this: undo() would take the test's own settings folder away too
    capsys.readouterr()
    assert exit_of("restore", other, to) == exits.USAGE and "from another backup" in capsys.readouterr().err
    moved = tmp_path / "moved-disk"
    shutil.copytree(top, moved)  # the same disk under another letter: its mark says it's the same backup
    (to / "comics" / "A Comic 1.cbz").unlink()  # in use since: a book removed
    assert exit_of("restore", moved, to) == exits.USAGE
    err = capsys.readouterr().err
    assert "A Comic 1.cbz" in err and "in use" in err and not (to / "comics" / "A Comic 1.cbz").exists()
    shutil.copyfile(library / "comics" / "A Comic 1.cbz", to / "comics" / "A Comic 1.cbz")  # as it was
    cli.main(["restore", str(moved), str(to)])  # the same backup, another way to it, all as copied: it goes on
    assert not (to / backup.RESTORING).exists() and (to / "curation" / "edits.jsonl").is_file()


def test_a_restore_left_unfinished_tells_a_book_changed_since_by_its_time_too(library, top, settings, tmp_path, monkeypatch, capsys):
    """The same size, written later than the restore ended: in use, so not gone on with. A copy's own time, on a disk that
    keeps none, is no change. A note that can't be read says so plainly."""
    run(library, top)
    to = tmp_path / "restored"
    real_unpack = backup.unpack
    monkeypatch.setattr(backup, "unpack", lambda made, folder, there=None: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert exit_of("restore", top, to) == exits.INTERRUPTED
    monkeypatch.setattr(backup, "unpack", real_unpack)  # only this: undo() would take the test's own settings folder away too
    note = json.loads((to / backup.RESTORING).read_text(encoding="utf-8"))
    assert isinstance(note.get("ended"), float)  # told when it ended, stopped or not
    book = to / "comics" / "A Comic 1.cbz"
    copied_at = book.stat().st_mtime
    os.utime(book, (copied_at, note["ended"] - 60))  # a copy's own time, before the restore ended: no change
    assert backup._used_since(to, note["ended"]) == ""
    book.write_bytes(b"PAGES OF A COMIC")  # the same size, written in use, since
    os.utime(book, (copied_at, note["ended"] + 60))
    capsys.readouterr()
    assert exit_of("restore", top, to) == exits.USAGE and "A Comic 1.cbz" in capsys.readouterr().err
    (to / backup.RESTORING).write_text("not a note", encoding="utf-8")
    assert exit_of("restore", top, to) == exits.USAGE
    err = capsys.readouterr().err
    assert "can't be read" in err and "None" not in err


def test_what_systems_leave_in_any_folder_doesnt_make_a_new_disk_a_folder_with_files(library, top, settings, tmp_path):
    run(library, top)
    to = tmp_path / "new disk"
    (to / "System Volume Information").mkdir(parents=True)
    for name in ("desktop.ini", ".DS_Store", "Thumbs.db"):
        (to / name).write_bytes(b"the system's own")
    cli.main(["restore", str(top), str(to)])
    assert (to / "curation" / "edits.jsonl").is_file() and (to / "comics" / "A Comic 1.cbz").is_file()


def test_a_backup_onto_the_library_s_own_disk_is_said_plainly(library, top, settings, capsys, monkeypatch):
    """No refusal: it helps against a book deleted by mistake. But it's no second copy on another disk, and the plan and
    the run say so before anything."""
    for dry in (["-n"], []):
        cli.main(["backup", str(top), "--library", str(library), "--yes", *dry])
        assert "is on the library's own disk" in capsys.readouterr().err
    real = os.stat

    def another_disk(path, *a, **k):  # the target's disk told apart from the library's by its number
        st = real(path, *a, **k)
        if Path(path) != library:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, *tuple(st)[3:]))
        return st

    monkeypatch.setattr(backup.os, "stat", another_disk)
    assert backup.on_the_same_disk(library, top) == ""


def test_an_older_zip_can_be_chosen(library, top, settings, tmp_path):
    run(library, top)
    first = backup.saved_zips(top)[-1]
    (library / "curation" / "edits.jsonl").write_text("later\n", encoding="utf-8")
    backup.save_the_rest(library, top, now=datetime(2099, 1, 1))
    assert backup.choose_zip(top, None).name.startswith("2099")
    assert backup.choose_zip(top, first.stem) == first
    with pytest.raises(Exit):
        backup.choose_zip(top, "1999")
    to = tmp_path / "restored"
    cli.main(["restore", str(top), str(to), "--saved", first.stem])
    assert (to / "curation" / "edits.jsonl").read_bytes() == b'{"made": "up"}\n'


def test_a_zip_cant_write_outside_the_new_library(tmp_path):
    made = tmp_path / "bad.zip"
    with zipfile.ZipFile(made, "w") as z:
        z.writestr("../outside.txt", "no")
        z.writestr("state/../../outside.txt", "no")
        z.writestr("elsewhere/file.txt", "no")
        z.writestr("state/fine.json", "{}")
    to = tmp_path / "deep" / "restored"
    to.mkdir(parents=True)  # as a restore makes it, before the books are copied
    assert backup.unpack(made, to) == (1, 0)
    assert (to / "state" / "fine.json").is_file() and not list(tmp_path.glob("outside*")) and not (to / "elsewhere").exists()


def test_what_isnt_a_backup_is_said_so(tmp_path, settings):
    (tmp_path / "just a folder").mkdir()
    assert code_of("check", tmp_path / "just a folder") != 0
    assert exit_of("restore", tmp_path / "just a folder", tmp_path / "to") != 0


# ---- the server's settings, for a backup made elsewhere -----------------------------------------------------------------------------

def test_the_server_copies_its_settings_every_night_and_never_a_login_or_the_phones_topic(library, settings, monkeypatch):
    phone_set(settings, monkeypatch)
    made = backup.settings_tonight(library, datetime(2026, 11, 6, 4))
    assert not (made / "users.txt").exists() and (made / "rules" / "mine.toml").is_file() and (made / "settings.toml").is_file()
    assert not [p for p in made.rglob("*") if p.is_file() and b"A-MADE-UP-SECRET" in p.read_bytes()]
    assert no_secret_in((made / "settings.toml").read_bytes())["server"]["name"] == "Made-up Library"
    assert backup.settings_tonight(library, datetime(2026, 11, 6, 5)) is None  # once a day
    for day in range(7, 17):
        backup.settings_tonight(library, datetime(2026, 11, day, 4))
    kept = sorted(p.name for p in backup.settings_backups(library).iterdir())
    assert len(kept) == backup.SETTINGS_KEPT and kept[-1] == "2026-11-16"


def test_it_goes_with_the_nightly_copy_and_a_failure_is_only_said(library, settings, monkeypatch):
    said = []
    dbcopy.copy_tonight(library, said.append, datetime(2026, 11, 6, 4))
    assert (backup.settings_backups(library) / "2026-11-06" / "settings.toml").is_file()

    def fails(root, now=None):
        raise OSError("no room")

    monkeypatch.setattr(backup, "settings_tonight", fails)
    dbcopy.copy_tonight(library, said.append, datetime(2026, 11, 7, 4))
    dbcopy.copy_tonight(library, said.append, datetime(2026, 11, 7, 5))  # the next hour: said once, not every hour
    assert len([s for s in said if "settings couldn't be copied" in s]) == 1


def test_the_catalog_doesnt_take_the_settings_for_books(library, settings):
    backup.settings_tonight(library, datetime(2026, 11, 6, 4))
    catalog.build(library, log=lambda s: None)
    assert not [i for i in Catalog(library).get() if "settings-backup" in str(i)]


def test_the_server_doesnt_serve_the_copies(library, settings, log_in):
    import threading

    import httpx

    backup.settings_tonight(library, datetime(2026, 11, 6, 4))
    srv, _ = serve.make_server(library, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")) as owner:  # not even to the owner
            assert owner.get("/state/settings-backup/2026-11-06/settings.toml").status_code in (403, 404)
    finally:
        srv.shutdown()
        srv.server_close()


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@posix_only
def test_the_zips_and_the_copies_of_the_settings_are_their_owners_only(library, top, settings, tmp_path):
    old = backup.settings_backups(library)
    old.mkdir(parents=True, mode=0o755)  # as one made before: left so, as a chmod on the library's disk may be kept as
    made = backup.settings_tonight(library, datetime(2026, 11, 6, 4))  # extended attributes, or refused (the doctor tells)
    assert mode(old) == 0o755 and mode(made) == mode(made / "rules") == 0o700
    assert mode(made / "settings.toml") == mode(made / "rules" / "mine.toml") == 0o600
    run(library, top)
    assert mode(backup.saved_of(top)) == 0o700 and mode(backup.saved_zips(top)[-1]) == 0o600
    to = tmp_path / "restored"
    cli.main(["restore", str(top), str(to)])
    private = [p for p in [*(to / "state").rglob("*"), *(to / "_settings-from-backup").rglob("*")] if p.is_file()]
    assert private and all(mode(p) == 0o600 for p in private)


@posix_only
def test_no_chmod_or_copystat_reaches_the_library_s_disk_or_a_target(library, top, settings, monkeypatch, tmp_path, request):
    """Modes are given as things are made, never by a chmod after, on the library's disk or on a disk copied to: a disk
    that keeps modes as extended attributes (Linux's ntfs3) would store one for each file, and one that refuses chmod
    stops what copies to it (copystat's own chmod among them). The backup and its zip, a book set aside, the nightly
    copies of the records and the settings, a restore, hylly copy, a download's older version set aside on another
    disk, and the library's own files written again: each made with the mode it needs, and none chmod'ed."""
    from hylly import db
    from hylly import download as dl

    before = os.umask(0o022)  # a usual one: a copy's mode is its original's, as far as the umask allows
    request.addfinalizer(lambda: os.umask(before))
    restored, moved = tmp_path / "restored", tmp_path / "moved"
    disks = tuple(os.path.realpath(p) for p in (library, top, restored, moved))
    (library / "comics" / "A Script.sh").write_bytes(b"made up")
    os.chmod(library / "comics" / "A Script.sh", 0o755)
    chmod = os.chmod

    def watched(path, *a, **k):
        assert not os.path.realpath(path).startswith(disks), f"a chmod of {path}"
        return chmod(path, *a, **k)

    def never(*a, **k):
        pytest.fail(f"a chmod by its handle, or a copystat: {a}")

    monkeypatch.setattr(os, "chmod", watched)
    for name in ("fchmod", "lchmod"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, never)
    monkeypatch.setattr(shutil, "copystat", never)
    monkeypatch.setattr(shutil, "copymode", never)
    run(library, top)
    (library / "comics" / "A Comic 1.cbz").write_bytes(b"pages of a comic, changed")
    run(library, top)  # the earlier copy set aside, the library's note of the backup written again
    assert backup.set_aside_days(top) and mode(top / "library" / "comics" / "A Script.sh") == 0o755
    assert mode(backup.saved_of(top)) == 0o700 and mode(backup.saved_zips(top)[-1]) == 0o600
    night = db.nightly(library, datetime(2026, 11, 6, 4))
    assert mode(night) == 0o700 and mode(night / db.FILE) == 0o600
    assert mode(backup.settings_tonight(library, datetime(2026, 11, 6, 4)) / "settings.toml") == 0o600
    cli.main(["restore", str(top), str(restored)])
    cli.main(["copy", str(library), str(moved), "--yes"])
    assert mode(moved / "comics" / "A Script.sh") == 0o755
    (library / "superseded").mkdir()
    dl._copy_aside(library / "comics" / "A Script.sh", library / "superseded" / "A Script.sh")  # as from another disk
    assert (library / "superseded" / "A Script.sh").read_bytes() == b"made up"


@posix_only
def test_the_doctor_says_when_others_can_read_the_nightly_copies(library, settings):
    from hylly import db

    night = db.nightly(library, datetime(2026, 11, 6, 4))
    backup.settings_tonight(library, datetime(2026, 11, 6, 4))
    assert not doctor._state_copies(library)  # as Hylly makes them
    os.chmod(night, 0o755)
    os.chmod(night / db.FILE, 0o644)
    said = doctor._state_copies(library)
    assert said[0]["state"] == doctor.PROBLEM and "db-backup/2026-11-06/hylly.db" in said[0]["says"] and "chmod -R go-rwx" in said[0]["next"]
    os.chmod(night / db.FILE, 0o640)  # the group can't pass through the folder: no one else gets at it
    os.chmod(night, 0o701)
    assert not doctor._state_copies(library)
    os.chmod(paths.state_dir(library), 0o700)  # nor through state
    os.chmod(night / db.FILE, 0o644)
    assert not doctor._state_copies(library)


@posix_only
def test_on_a_disk_without_rights_of_its_own_the_doctor_names_its_mount_options_and_finds_no_problem(library, settings):
    """exFAT, a share, NTFS without permissions: every file shows what the mount says, whatever Hylly set, and chmod
    can't change it. That isn't a problem to put right on every run of the doctor."""
    from hylly import db

    db.nightly(library, datetime(2026, 11, 6, 4))
    backup.settings_tonight(library, datetime(2026, 11, 6, 4))
    for folder in (db.backups(library), backup.settings_backups(library)):  # as such a mount shows them
        for path in [folder, *folder.rglob("*")]:
            os.chmod(path, 0o755 if path.is_dir() else 0o644)
    os.chmod(paths.state_dir(library), 0o755)
    os.chmod(library, 0o755)
    said = doctor._state_copies(library)
    assert said[0]["state"] == doctor.INFO and "mount options" in said[0]["says"] and "umask=077" in said[0]["next"]


def test_the_zip_takes_only_the_newest_nightly_copy_of_the_database(library, top, today):
    from hylly import db

    people.Person("rory").place({"id": "h:novel", "t": "The Novel", "f": "n.epub"}, 1, 0.1)
    for day in (5, 6, 7):
        db.nightly(library, datetime(2026, 11, day, 4))
    run(library, top)
    with zipfile.ZipFile(backup.saved_zips(top)[-1]) as z:
        nights = {n.split("/")[2] for n in z.namelist() if n.startswith("state/db-backup/")}
    assert nights == {today.strftime("%Y-%m-%d")}  # the one the backup made, of the records as they are


# ---- the library's records: as they are in each backup, and in place again after a restore ---------------------------------

def tag(library, name):
    cli.main(["tag", "add", name, "--id", "h:novel", "--yes", "--library", str(library)])


def records_in_the_zip(top) -> str:
    """The tables of the database's copy in the newest zip, as JSON lines, all together."""
    return "".join(v.decode("utf-8") for n, v in in_the_zip(top).items() if n.startswith("state/db-backup/") and n.endswith(".jsonl"))


def test_a_backup_takes_the_records_as_they_are_not_as_they_were_last_night(library, top, settings, capsys, today):
    from hylly import db

    db.nightly(library, today)  # today's copy, made before the tag
    tag(library, "Knitting")
    run(library, top)
    assert "without what changed since" not in capsys.readouterr().out
    assert "Knitting" in records_in_the_zip(top)
    assert sorted(p.name for p in db.backups(library).iterdir()) == [today.strftime("%Y-%m-%d")]  # nothing half-made
    assert not any(p.name.startswith(f"{db.FILE}.") for p in db.file().parent.iterdir())  # left behind


def test_records_another_computer_keeps_come_fresh_from_it_or_the_backup_says_they_dont(library, top, settings, monkeypatch, tmp_path, capsys):
    from hylly import db, remote

    owner = db.file()
    tag(library, "Knitting")
    marker = json.loads(db.owner_file(library).read_text(encoding="utf-8"))
    db.owner_file(library).write_text(json.dumps({**marker, "computer": "server-test"}), encoding="utf-8")
    computer(monkeypatch, tmp_path / "another-computer")  # this one's settings: its records aren't here
    db.close_all()
    assert code_of(top, "--yes", "--library", library) == PARTLY
    out = capsys.readouterr().out
    assert "Not in the zip: the library's records" in out and "kept by server-test" in out and "[remote] ssh" in out
    db.nightly(library, datetime(2026, 11, 10, 4), where=owner)  # the server's last copy
    assert code_of(top, "--yes", "--library", library) == PARTLY
    assert "are their copy of 2026-11-10" in capsys.readouterr().out and "Knitting" in records_in_the_zip(top)
    asked = []
    monkeypatch.setattr(remote, "server", lambda: ("server", ""))
    monkeypatch.setattr(remote, "ask", lambda argv, text="", timeout=0: asked.append(argv) or "Copied.")
    run(library, top)  # whole
    assert asked == [["internal", "fresh-copy"]] and "without what changed since" not in capsys.readouterr().out


def test_the_server_makes_a_fresh_copy_when_asked_but_only_of_its_own(library, settings, monkeypatch, tmp_path, capsys):
    from hylly import db

    db.nightly(library)
    tag(library, "Knitting")
    cli.main(["internal", "fresh-copy", "--library", str(library)])
    assert "Copied" in capsys.readouterr().out
    assert "Knitting" in "".join(p.read_text(encoding="utf-8") for p in db.nights(library)[-1].glob("*.jsonl"))
    computer(monkeypatch, tmp_path / "another-computer")
    db.close_all()
    with pytest.raises(SystemExit):
        cli.main(["internal", "fresh-copy", "--library", str(library)])


@pytest.mark.parametrize("earlier", [False, True], ids=["no-copy-before", "after-tonights-copy"])
def test_after_a_restore_on_a_new_computer_the_records_are_all_there(library, top, settings, monkeypatch, tmp_path, capsys, earlier):
    from hylly import access, curation, db, manifest

    if earlier:
        db.nightly(library)  # tonight's copy came before what's below
    tag(library, "Knitting")
    access.add_user("owen", "a made-up password", role="reader")
    manifest.Manifest(library, "humble").add({"path": "humble/A Made-up Book.pdf", "size": 3, "md5": "made-up"})
    run(library, top)
    computer(monkeypatch, tmp_path / "new-computer")  # a new computer: an empty settings folder
    db.close_all()
    to = tmp_path / "restored"
    cli.main(["restore", str(top), str(to), "--dry-run"])
    assert "this computer's database" in capsys.readouterr().out and not db.file().exists()  # a plan: nothing done
    cli.main(["restore", str(top), str(to)])
    out = capsys.readouterr().out
    assert "are this computer's database now" in out and "as said above" not in out
    assert "_settings-from-backup" in out and f"library = '{to}'" in out
    assert curation.load(to).of(["h:novel"]) == ["Knitting"]
    assert "owen" in access.load_accounts()
    assert manifest.Manifest(to, "humble").get("humble/A Made-up Book.pdf")["size"] == 3
    assert Catalog(to).get()


def test_a_restore_never_writes_over_this_computers_database(library, top, settings, monkeypatch, tmp_path, capsys):
    from hylly import db

    run(library, top)
    cli.main(["restore", str(top), str(tmp_path / "again")])
    assert "keeps this library's records already" in capsys.readouterr().out  # the same library's: newer than the copy
    computer(monkeypatch, tmp_path / "another-computer")
    db.close_all()
    other = tmp_path / "other"
    build_library(other)
    catalog.build(other, log=lambda s: None)  # this computer's database keeps another library's records
    mine = json.loads(db.owner_file(other).read_text(encoding="utf-8"))["id"]
    cli.main(["restore", str(top), str(tmp_path / "restored")])
    out = capsys.readouterr().out
    assert "has another library's records, so it was left as it is" in out and "as said above" in out
    db.close_all()
    assert backup._library_in(db.file()) == mine


def test_a_restore_passes_over_a_copy_of_another_database_and_the_zip_never_takes_one(library, top, settings, monkeypatch, tmp_path, capsys):
    import shutil

    from hylly import db

    tag(library, "Knitting")
    db.nightly(library, datetime(2026, 11, 5, 4))
    mine = json.loads(db.owner_file(library).read_text(encoding="utf-8"))["id"]
    computer(monkeypatch, tmp_path / "another-computer")  # another library's, on another computer
    db.close_all()
    other = tmp_path / "other"
    build_library(other)
    catalog.build(other, log=lambda s: None)
    shutil.copytree(db.nightly(other, datetime(2026, 11, 6, 4)), db.backups(library) / "2026-11-06")  # the newest, and not this library's
    computer(monkeypatch, tmp_path / "new-computer")  # a new computer: an empty settings folder
    db.close_all()
    assert code_of(top, "--yes", "--library", library) == PARTLY  # no fresh copy from here: the newest of the library's records
    assert "are their copy of 2026-11-05" in capsys.readouterr().out
    assert {n.split("/")[2] for n in in_the_zip(top) if n.startswith("state/db-backup/")} == {"2026-11-05"}
    placed, said = backup.records_in_place(library)
    assert placed and "as copied on 2026-11-05" in said
    assert backup._library_in(db.file()) == mine
    os.remove(db.file())
    shutil.rmtree(db.backups(library) / "2026-11-05")  # only the other database's copy: nothing put in place
    placed, said = backup.records_in_place(library)
    assert not placed and "start afresh" in said and "2026-11-06 are of another database" in said and not db.file().exists()


def test_without_the_library_s_marker_the_zip_has_the_newest_copy_and_a_restore_takes_it(library, top, settings, monkeypatch, tmp_path, capsys):
    from hylly import curation, db

    tag(library, "Knitting")
    db.nightly(library, datetime(2026, 11, 5, 4))
    db.owner_file(library).unlink()  # no database has taken the library on: nothing tells its copies apart
    run(library, top)
    assert {n.split("/")[2] for n in in_the_zip(top) if n.startswith("state/db-backup/")} == {"2026-11-05"}
    assert "Knitting" in records_in_the_zip(top)
    computer(monkeypatch, tmp_path / "new-computer")  # a new computer: an empty settings folder
    db.close_all()
    to = tmp_path / "restored"
    cli.main(["restore", str(top), str(to)])
    assert "as copied on 2026-11-05, are this computer's database now" in capsys.readouterr().out
    assert curation.load(to).of(["h:novel"]) == ["Knitting"]


def test_a_restore_never_puts_in_place_a_copy_this_version_leaves_alone_and_says_what_to_do(library, settings, monkeypatch, tmp_path):
    """A copy from the test versions before the first release: one of their first step (no library table), or of their
    fifth, is refused as the database would refuse it, with its words; one that came through all nine is put in place."""
    import contextlib
    import sqlite3

    from hylly import db

    night = db.nightly(library, datetime(2026, 11, 5, 4))
    shutil.copytree(night, tmp_path / "as-made")
    made_before_the_library_was_kept(night)
    assert db.nights(library) == [night]  # the library's own, from before
    computer(monkeypatch, tmp_path / "new-computer")
    db.close_all()
    placed, said = backup.records_in_place(library)
    assert not placed and not db.file().exists() and "came through 1 of them" in said and "hylly people list" in said
    shutil.rmtree(night)
    shutil.copytree(tmp_path / "as-made", night)
    (night / db.FILE).unlink()  # their tables, this version's first step's, as the copy was made then
    at_the_first_step(night / db.FILE, records=[], library=json.loads(db.owner_file(library).read_text(encoding="utf-8"))["id"])
    for step, put in ((5, False), (9, True)):  # as their fifth step, or their ninth, left it: the same tables, their header
        with contextlib.closing(sqlite3.connect(night / db.FILE, isolation_level=None)) as c:
            c.execute("PRAGMA application_id = 0")
            c.execute(f"PRAGMA user_version = {step}")
        computer(monkeypatch, tmp_path / f"computer-{step}")
        db.close_all()
        placed, said = backup.records_in_place(library)
        assert placed == put == db.file().is_file(), said
        assert put or ("came through 5 of them" in said and "wasn't made this computer's database" in said)
    with db.database().read() as c:  # taken on, once opened, and brought through this version's steps
        assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.migrations())


def test_an_older_nightly_copy_a_restore_puts_in_place_is_brought_up_to_this_version_as_it_first_opens(library, settings,
                                                                                                       monkeypatch, tmp_path):
    """A nightly copy of the first release's database, whose download records have each shop's fields of its own,
    is put in place by a restore as it is, and brought through this version's steps as it is first opened, a copy of it
    as it was kept beside it first: its records are never read as they were, but as this version names them."""
    import contextlib
    import sqlite3

    from hylly import db, manifest

    night = db.nightly(library, datetime(2026, 11, 5, 4))
    mine = json.loads(db.owner_file(library).read_text(encoding="utf-8"))["id"]
    (night / db.FILE).unlink()
    at_the_first_step(night / db.FILE, library=mine)  # as the first release made it
    computer(monkeypatch, tmp_path / "new-computer")
    db.close_all()
    placed, said = backup.records_in_place(library)
    assert placed, said
    with contextlib.closing(sqlite3.connect(db.file())) as c:  # put in place as it was
        assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    record = manifest.Manifest(library, "drivethrurpg").get(DT_OLD["path"])  # opened: brought up to date first
    change = next(change for _, old, change in FIRST_RELEASE_RECORDS if old is DT_OLD)
    assert record == named_now(DT_OLD, change) and record["item"] == "1001"
    with contextlib.closing(sqlite3.connect(db.file().with_name("hylly.before-0002.db"))) as kept:
        assert kept.execute("PRAGMA user_version").fetchone()[0] == 1


def test_a_folder_that_cant_be_read_is_said_not_left_out_quietly(library, top, settings, monkeypatch, capsys):
    """Said at the end, to the phone, in the backup's note on its disk and in the library's, so that hylly doctor tells of a backup that
    isn't whole: an exit code alone, of a backup run by a schedule, nobody reads."""
    said = notes_kept(monkeypatch)
    real = os.walk

    def walk(folder, *args, onerror=None, **kwargs):
        if os.path.basename(folder) == "curation":
            onerror(PermissionError(13, "Permission denied", folder))
            return iter(())
        if os.path.basename(folder) == "catalog":
            onerror(FileNotFoundError(2, "gone while it was read", folder))  # nothing to say of that
        return real(folder, *args, onerror=onerror, **kwargs)

    monkeypatch.setattr(os, "walk", walk)
    assert code_of(top, "--yes", "--library", library) == PARTLY
    out = capsys.readouterr().out
    assert "Not in the zip: curation/: Permission denied" in out and "gone while" not in out
    phone = [(title, public) for title, _, public in said if "zip" in title]
    assert phone == [("Hylly backup: not all in the zip", "Something couldn't be read into the zip: the backup's output says what.")]
    assert backup.last(library)["missed"] == ["Not in the zip: curation/: Permission denied"]
    readme = (top / backup.NOTE).read_text(encoding="utf-8")
    assert "NOT WHOLE" in readme and "  - Not in the zip: curation/: Permission denied\n" in readme and "Last finished" not in readme
    told = doctor._backup(library)[0]
    assert told["state"] == doctor.PROBLEM and "It isn't whole: Not in the zip: curation/: Permission denied" in told["says"]
    assert "back up again" in told["next"]
    monkeypatch.setattr(os, "walk", real)
    run(library, top)  # whole again
    assert "missed" not in backup.last(library) and doctor._backup(library)[0]["state"] == doctor.INFO
    assert "Last finished" in (top / backup.NOTE).read_text(encoding="utf-8")


def test_a_backup_with_older_records_isnt_counted_whole_either(library, top, settings, monkeypatch, capsys):
    said = notes_kept(monkeypatch)
    older = "In the zip, the library's records are their copy of 2026-11-10, without what changed since: a made-up reason."
    monkeypatch.setattr(backup, "fresh_records", lambda root, now=None: (None, older))
    assert code_of(top, "--yes", "--library", library) == PARTLY
    assert "Hylly backup: older records in it" in [title for title, _, _ in said]
    assert backup.last(library)["missed"] == [older] and f"  - {older}\n" in (top / backup.NOTE).read_text(encoding="utf-8")
    assert doctor._backup(library)[0]["state"] == doctor.PROBLEM and older in doctor._backup(library)[0]["says"]
    with pytest.raises(ValueError):  # what the server takes from another computer: a list of words, or nothing
        backup.keep_note(library, {**backup.last(library), "missed": "not a list"})
    with pytest.raises(ValueError):
        backup.keep_note(library, {**backup.last(library), "another": 1})


def test_todays_copy_made_again_takes_the_earlier_ones_place_only_when_whole(library, settings, monkeypatch):
    from hylly import db

    real, first = db.export, db.nightly(library)
    assert db.nightly(library) is None  # once a day by itself
    (first / "mine.txt").write_text("from the first", encoding="utf-8")

    def broken(source, target):
        raise OSError("the disk is full")

    monkeypatch.setattr(db, "export", broken)
    with pytest.raises(OSError):
        db.nightly(library, fresh=True)
    assert (first / "mine.txt").is_file() and db.nights(library) == [first]  # the earlier one stays
    monkeypatch.setattr(db, "export", real)
    assert db.nightly(library, fresh=True) == first and not (first / "mine.txt").exists()
    assert sorted(p.name for p in db.backups(library).iterdir()) == [first.name]


def test_a_backup_of_another_library_to_a_disk_with_a_backup_asks_too(library, top, settings, tmp_path, capsys):
    """A known disk with nothing of this library on it: a mistyped library, say. It asks, as at a first backup."""
    run(library, top)
    other = tmp_path / "another"
    (other / "ebooks").mkdir(parents=True)
    (other / "ebooks" / "Ann Writer - Another Book.epub").write_bytes(b"not really a book" * 100)
    assert code_of(top, "--library", other) == exits.NEEDS_YOU
    assert "has no backup of" in capsys.readouterr().err
    assert not (top / "library" / "ebooks" / "Ann Writer - Another Book.epub").exists()


def test_a_server_named_wrong_stops_a_backup_before_anything(library, top, settings, monkeypatch, capsys):
    """HYLLY_REMOTE set and empty, as elsewhere: not taken for no server, and the zip with older records. Only when the
    records come from the server, though: a backup made on the computer that keeps them asks no server, and goes ahead."""
    from hylly import db

    monkeypatch.setenv("HYLLY_REMOTE", "")
    assert db.owner_file(library).is_file()  # this computer's database keeps the records, as the build took them on
    run(library, top)
    assert backup.last(library) is not None and "set and empty" not in capsys.readouterr().err
    db.owner_file(library).write_text(json.dumps({"id": "f" * 32, "computer": "elsewhere"}), encoding="utf-8")
    elsewhere = top.parent / "another"
    assert code_of(elsewhere, "--yes", "--library", library) == exits.NOT_SET_UP
    assert "HYLLY_REMOTE, set and empty," in capsys.readouterr().err and not elsewhere.exists()


# ---- the set-aside, the locks, copies dated ahead, links, the README; a target and keep for a timer -----------------------

def test_a_file_with_no_ending_changed_twice_in_a_day_is_set_aside_inside_the_backup_under_a_dotted_folder(library, tmp_path, settings, capsys,
                                                                                                            today):
    """The second name is made of the file's own name, not of the whole path: a dot earlier in it (a share's
    address, disk.1) must not take the earlier copy out of the backup. And the message names the folder with a slash,
    changed/<day>, where it once wrote changed\\<day>."""
    top = tmp_path / "disk.1" / "backup"
    (library / "ebooks").mkdir(exist_ok=True)
    licence = library / "ebooks" / "LICENSE"
    licence.write_bytes(b"the first words")
    run(library, top)
    licence.write_bytes(b"the second words, longer")
    run(library, top)
    licence.write_bytes(b"the third words, longer still")
    run(library, top)
    day = top / "changed" / today.strftime("%Y-%m-%d") / "ebooks"
    assert (top / "library" / "ebooks" / "LICENSE").read_bytes() == b"the third words, longer still"
    assert (day / "LICENSE").read_bytes() == b"the first words" and (day / "LICENSE (2)").read_bytes() == b"the second words, longer"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["disk.1", "library", "settings"]  # nothing beside the backup
    assert "changed/" in capsys.readouterr().out


def test_setting_a_book_aside_waits_out_a_moment_s_trouble_and_lasting_trouble_is_that_book_s_failure(library, top, settings, monkeypatch, capsys):
    """The move into changed/ goes through HyllyCopy's waiting, as naming a copy does: an error once is tried
    again, and one that stays is that one book's failure, never a traceback that ends the backup."""
    run(library, top)
    book = library / "comics" / "A Comic 1.cbz"
    monkeypatch.setattr(hc, "WAITS", (0,) * len(hc.WAITS))
    real, goes = os.replace, []

    def fails(src, dst, *a, **k):
        if f"{os.sep}changed{os.sep}" in str(dst):
            goes.append(dst)
            if len(goes) == 1 or why is not None:
                raise OSError(why or errno.EIO, os.strerror(why or errno.EIO))
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", fails)
    why = None
    book.write_bytes(b"pages of a comic, changed")
    run(library, top)  # once: waited out
    assert len(goes) == 2 and (top / "library" / "comics" / "A Comic 1.cbz").read_bytes() == b"pages of a comic, changed"
    why = errno.ENAMETOOLONG  # a name this disk can't hold, every time
    book.write_bytes(b"pages of a comic, changed again")
    assert code_of(top, "--yes", "--library", library) == PARTLY
    shown = capsys.readouterr()
    assert "Traceback" not in shown.err and "A Comic 1.cbz" in shown.out + shown.err
    assert (top / "library" / "comics" / "A Comic 1.cbz").read_bytes() == b"pages of a comic, changed"  # the earlier copy stays


def test_the_jobs_locks_stay_out_of_the_zip_and_out_of_a_library_restored_from_an_older_zip(library, top, settings, tmp_path):
    """A running job's lock can't be read on Windows, which once made every backup beside one end 'partly', and a
    restored library would carry a note of a job running on the old computer."""
    locks = library / "state" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / "catalog.lock").write_bytes(b"")
    (locks / "catalog.json").write_text('{"host": "made-up-host", "pid": 4242}', encoding="utf-8")
    before = backup.size_of_the_rest(library)
    (locks / "lookup.json").write_bytes(b"x" * 50_000)
    assert backup.size_of_the_rest(library) == before
    run(library, top)
    assert not [n for n in in_the_zip(top) if n.startswith("state/locks")] and backup.last(library).get("missed") is None
    older = backup.saved_of(top) / "2026-01-02 030000.zip"  # one made before they were left out
    with zipfile.ZipFile(older, "w") as z:
        z.writestr("state/locks/catalog.json", '{"host": "made-up-host", "pid": 4242}')
        z.writestr("state/made-up.json", "{}")
    (tmp_path / "restored").mkdir()
    written, kept = backup.unpack(older, tmp_path / "restored")
    assert (written, kept) == (1, 0) and (tmp_path / "restored" / "state" / "made-up.json").is_file()
    assert not (tmp_path / "restored" / "state" / "locks").exists()


def test_a_backup_holds_its_lock_to_the_end_of_its_zip_and_a_restore_to_the_end_of_its_records(library, top, settings, tmp_path, monkeypatch):
    """While the zip is made, hylly jobs tells of the backup, and a second one is refused, not let in beside it."""
    from hylly import runs

    seen, save, place = [], backup.save_the_rest, backup.records_in_place

    def busy() -> None:
        seen.append(runs.current("backup"))
        with pytest.raises(runs.Busy), runs.only_one("backup"):
            pass

    monkeypatch.setattr(backup, "save_the_rest", lambda *a, **k: busy() or save(*a, **k))
    monkeypatch.setattr(backup, "records_in_place", lambda to: busy() or place(to))
    run(library, top)
    cli.main(["restore", str(top), str(tmp_path / "restored")])
    assert len(seen) == 2 and all(note is not None for note in seen)
    assert runs.current("backup") is None  # and let go at the end


def test_two_fresh_copies_of_the_database_take_turns(library, settings, monkeypatch):
    """Two at once used to share hylly.db.now and <day>.fresh: on Windows one ended with exit code 6, elsewhere one
    could take the other's work away mid-copy. The second waits for the first, and says so if it waits too long."""
    import threading
    import time

    from hylly import db

    first = db.nightly(library)
    made = []
    with db._one_copy_at_a_time(db.file()):  # one being made
        second = threading.Thread(target=lambda: made.append(db.nightly(library, fresh=True)))
        second.start()
        time.sleep(0.8)
        assert second.is_alive() and not made
        assert not db.file().with_name(f"{db.FILE}.now").exists() and not list(db.backups(library).glob("*.fresh"))
    second.join(30)
    assert made == [first] and sorted(p.name for p in db.backups(library).iterdir()) == [first.name]
    assert db.COPY_WAIT <= backup.FRESH_WAIT / 2  # the server's wait ends well before another computer's backup stops asking
    monkeypatch.setattr(db, "COPY_WAIT", 0.3)
    with db._one_copy_at_a_time(db.file()), pytest.raises(TimeoutError, match="another copy of the database"):
        db.nightly(library, fresh=True)


def test_a_copy_of_the_records_dated_ahead_of_this_computer_s_day_is_zipped_and_restored(library, top, settings, monkeypatch, tmp_path, capsys):
    """A server in another time zone dates tonight's copy tomorrow. A backup without a fresh copy takes it, by
    its name, and a restore puts it in place; a fresh copy is zipped by the day it was made under, even beside a copy
    dated far ahead by a clock that was wrong once."""
    from hylly import db, remote

    owner = db.file()
    tag(library, "Knitting")
    tomorrow = datetime.now() + timedelta(days=1)
    marker = json.loads(db.owner_file(library).read_text(encoding="utf-8"))
    db.owner_file(library).write_text(json.dumps({**marker, "computer": "server-test"}), encoding="utf-8")
    db.nightly(library, tomorrow, where=owner)  # the server's, under its date
    computer(monkeypatch, tmp_path / "another-computer")  # no [remote] here: no fresh copy
    db.close_all()
    assert code_of(top, "--yes", "--library", library) == PARTLY
    day = tomorrow.strftime("%Y-%m-%d")
    assert f"are their copy of {day}" in capsys.readouterr().out
    assert {n.split("/")[2] for n in in_the_zip(top) if n.startswith("state/db-backup/")} == {day}
    placed, said = backup.records_in_place(library)  # this computer has no database yet: the copy becomes it
    assert placed and f"as copied on {day}" in said
    db.close_all()
    os.remove(db.file())
    shutil.copytree(db.backups(library) / day, db.backups(library) / "2099-01-01")  # a clock that was far ahead, once
    monkeypatch.setattr(remote, "server", lambda: ("server", ""))
    monkeypatch.setattr(remote, "ask", lambda argv, text="", timeout=0: f"Copied: /srv/books/state/db-backup/{day}.\n")
    run(library, top)  # whole: the fresh copy the server named
    assert {n.split("/")[2] for n in in_the_zip(top) if n.startswith("state/db-backup/")} == {day}


def test_a_link_or_a_junction_is_left_out_of_the_zip_and_the_settings_as_hyllycopy_leaves_it_out(library, top, settings, tmp_path, monkeypatch):
    """One definition of a link, storage's, for the backup's zip as for HyllyCopy: a Windows junction is one too.
    Junctions are Windows' own; here one is made up as Windows would tell it, by its reparse tag."""
    import stat
    import types

    from hylly import storage

    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "inside").mkdir(parents=True)
    (elsewhere / "inside" / "not-the-library.json").write_text("{}", encoding="utf-8")
    (elsewhere / "linked.toml").write_text("# not a rules file of this computer's\n", encoding="utf-8")
    junction = library / "state" / "junction"
    (junction / "inside").mkdir(parents=True)
    (junction / "inside" / "behind-a-junction.json").write_text("{}", encoding="utf-8")
    real_stat = os.stat
    seen_as_junction = types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_reparse_tag=0xA0000003)

    def as_windows_tells_it(path, *a, follow_symlinks=True, **k):
        if not follow_symlinks and os.path.basename(os.fspath(path)) == "junction":
            return seen_as_junction
        return real_stat(path, *a, follow_symlinks=follow_symlinks, **k)

    monkeypatch.setattr(os, "stat", as_windows_tells_it)
    if sys.platform != "win32":  # links, where a test may make them
        (library / "state" / "a-link").symlink_to(elsewhere / "inside", target_is_directory=True)
        (library / "curation" / "linked.json").symlink_to(elsewhere / "inside" / "not-the-library.json")
        (settings / "rules" / "linked.toml").symlink_to(elsewhere / "linked.toml")
    run(library, top)
    names = in_the_zip(top)
    assert not [n for n in names if "junction" in n or "a-link" in n or "linked" in n or "not-the-library" in n]
    assert "settings/rules/mine.toml" in names
    plain = types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755)
    assert hc.is_link_stat is storage.is_link_stat  # HyllyCopy's listing goes by storage's own, no rule of its own
    assert storage.is_link_stat(seen_as_junction) and storage.is_link(junction) and not storage.is_link_stat(plain)


def test_readme_writes_its_folders_with_a_slash_names_the_settings_folder_and_says_what_the_zips_hold(library, top, settings):
    """The page read after a disaster, with no program at hand, has paths that can be typed as written
    on any system, says where the settings folder is on each, and that the zips hold what must stay private."""
    run(library, top)
    readme = (top / backup.NOTE).read_text(encoding="utf-8")
    windows = ("%LOCALAPPDATA%\\Hylly\\data", "%LOCALAPPDATA%\\Hylly", "%APPDATA%\\Hylly", str(top))  # as Windows writes them
    assert "\\" not in functools.reduce(lambda text, place: text.replace(place, ""), windows, readme)
    assert all(f"{name}/" in readme for name in ("library", "changed", "saved", "state/db-backup", "secrets"))
    assert "NEW-FOLDER/_settings-from-backup" in readme
    assert "%LOCALAPPDATA%\\Hylly on Windows" in readme and "~/.local/share/Hylly on Linux and macOS" in readme  # all in one
    assert all(place in readme for place in ("%APPDATA%\\Hylly on Windows", "~/.config/hylly on Linux", "~/.local/share/hylly on Linux"))
    words = " ".join(readme.split())
    assert all(said in words for said in ("everyone's reading", "password hashes", "Humble's order keys", "Keep this disk private"))


def test_the_backup_s_note_has_a_name_of_its_own_and_a_readme_of_someone_s_is_left_alone(library, top, settings):
    """The target may be the top of a disk, or a folder of backups with a note of someone's: the backup writes its own as
    HYLLY-BACKUP.txt, always. A README.txt an older backup wrote, which starts as its note does, goes once, as the note
    has its own name now."""
    top.mkdir(parents=True)
    (top / "README.txt").write_text("Ann Writer's notes on this disk.\n", encoding="utf-8")
    run(library, top)
    assert (top / "README.txt").read_text(encoding="utf-8") == "Ann Writer's notes on this disk.\n"
    assert (top / backup.NOTE).read_text(encoding="utf-8").startswith(f"{backup.FIRST_WORDS} Last finished: ")
    (top / "README.txt").write_text(backup.README.format(last="Last finished: a while ago.", top=top), encoding="utf-8")
    run(library, top)
    assert not (top / "README.txt").exists() and "hylly restore" in (top / backup.NOTE).read_text(encoding="utf-8")


def own_zips(top, *names: str) -> None:
    """Zips of earlier backups, by name only: keep goes by their names."""
    backup.saved_of(top).mkdir(parents=True, exist_ok=True)
    for name in names:
        with zipfile.ZipFile(backup.saved_of(top) / name, "w") as z:
            z.writestr("state/made-up.json", name)


MONTHS = ("2026-08-01 030000.zip", "2026-08-15 030000.zip", "2026-09-01 030000.zip", "2026-09-20 030000.zip",
          "2026-10-05 030000.zip", "2026-10-06 030000.zip", "2026-10-06 150000.zip", "2026-10-07 030000.zip",
          "2026-10-07 030000 (2).zip")  # oldest first: a made-up row of a backup's zips across three months


def test_keep_thins_the_zips_as_restic_s_forget_does_and_only_the_backups_own(top):
    """Going from the newest, the newest of each of the latest days, weeks and months stays, as many as keep says,
    and the oldest too while a count has some left; any other file in saved/ is never touched, nor counted."""
    own_zips(top, *MONTHS, "my own notes.zip", "2026-10-08 030000.zip.part")
    gone = [p.name for p in backup.to_thin(top, {"daily": 3, "weekly": 2, "monthly": 4})]
    assert gone == ["2026-09-01 030000.zip", "2026-10-06 030000.zip", "2026-10-07 030000.zip"]
    assert backup.to_thin(top, {}) == [] and backup.to_thin(top, {"daily": 0, "monthly": 0}) == []  # every zip stays
    assert [p.name for p in backup.to_thin(top, {"monthly": 1})] == list(MONTHS[:-1])  # never the newest
    coming = [p.name for p in backup.to_thin(top, {"daily": 3}, coming=datetime(2026, 11, 16, 4))]  # a plan's: the next one
    assert coming == [n for n in MONTHS if n not in ("2026-10-06 150000.zip", "2026-10-07 030000 (2).zip")]
    assert backup.keep_words({"daily": 7, "monthly": 1}) == ("keep = { daily = 7, monthly = 1 } under [backup] keeps the newest "
                                                          "zip of each of the 7 latest days and the latest month, counting "
                                                          "only those with a zip")


def test_a_backup_thins_by_keep_in_the_settings_says_which_and_why_and_a_dry_run_thins_nothing(library, top, settings, capsys, monkeypatch):
    """keep under [backup] thins after a backup whose zip is whole, says which went and why, and writes it down in
    saved/thinned.txt; a dry run says which would go, and removes nothing; a zip that isn't whole leaves them all."""
    own_zips(top, *MONTHS, "my own notes.zip")
    (settings / "settings.toml").write_text("[backup]\nkeep = { monthly = 2 }\n", encoding="utf-8")
    run(library, top, "--dry-run")
    out = " ".join(capsys.readouterr().out.split())
    assert ("Then 8 older zips would go, as keep = { monthly = 2 } under [backup] keeps the newest zip of each of the 2 "
            "latest months, counting only those with a zip: 2026-08-01 030000.zip") in out
    assert len(backup.saved_zips(top)) == len(MONTHS) + 1  # nothing removed
    with monkeypatch.context() as m:
        m.setattr(backup, "fresh_records", lambda root, now=None: (None, "In the zip, the records are older: made up."))
        assert code_of(top, "--yes", "--library", library) == PARTLY  # a zip that isn't whole thins nothing
    assert "No older zip went" in capsys.readouterr().out and len(backup.saved_zips(top)) == len(MONTHS) + 2
    first = [p.name for p in backup.saved_zips(top) if p.name not in (*MONTHS, "my own notes.zip")]  # the zip the backup just made
    run(library, top, "--json")
    said = json.loads(capsys.readouterr().out)
    assert said["thinned"] == [*MONTHS[:-1], *first]  # this month's earlier one too: the newest of a month stays
    kept = [p.name for p in backup.saved_zips(top)]
    assert kept == [MONTHS[-1], said["zip"], "my own notes.zip"]
    log = (backup.saved_of(top) / backup.THINNED).read_text(encoding="utf-8")
    assert "2026-08-01 030000.zip went, as keep = { monthly = 2 } under [backup]" in log and "my own notes" not in log
    assert len(log.splitlines()) == 9


def test_a_plain_backup_goes_to_the_target_in_the_settings_and_one_typed_wins(library, top, settings, tmp_path, capsys, monkeypatch):
    """target under [backup], so that a timer runs a plain hylly backup, and its check; the command line's wins.
    Neither: a setting missing (exit code 3), told which to give, with nothing done, and a note to the phone, as a
    timer's run would otherwise fail unseen."""
    from hylly import settings as the_settings

    said = notes_kept(monkeypatch)
    assert code_of("--library", library, "--yes") == exits.NOT_SET_UP
    assert "target = '...' under [backup]" in capsys.readouterr().err and not top.exists()
    assert [title for title, _, _ in said] == ["A backup stopped"]
    (settings / "settings.toml").write_text(f"[backup]\ntarget = '{top}'\n", encoding="utf-8")
    the_settings.reload()  # the next command is a process of its own
    cli.main(["backup", "--library", str(library), "--yes"])
    assert backup.saved_zips(top) and (top / "library" / "comics" / "A Comic 1.cbz").is_file()
    cli.main(["backup", "check"])
    assert "read back the same" in capsys.readouterr().out
    elsewhere = tmp_path / "disk" / "another"
    run(library, elsewhere)
    assert backup.saved_zips(elsewhere) and len(backup.saved_zips(top)) == 1


@pytest.mark.parametrize("written, said", [
    ("[backup]\nkeep = { yearly = 2 }\n", "how many zips to keep"),
    ("[backup]\nkeep = { daily = -1 }\n", "keep = { daily = -1 }: how many zips to keep"),
    ("[backup]\nkeep = { daily = 0 }\n", "keep only the newest zip"),
    ("[backup]\nkeep = 7\n", "how many zips to keep"),
    ("[backup]\ntarget = 'backups/here'\n", "the backup's folder by its full path"),
    ("[backup]\ntargett = '/mnt/backup'\n", "[backup] targett isn't one of its settings"),
], ids=["no-such-count", "below-0", "all-0", "not-a-table", "not-a-full-path", "mistyped"])
def test_settings_check_names_a_wrong_backup_setting(settings, written, said):
    """hylly settings check names each mistake under [backup], keep written as settings.toml has it."""
    from hylly import settings as the_settings

    (settings / "settings.toml").write_text(written, encoding="utf-8")
    mistakes, left = the_settings.problems()
    assert any(said in line for line in mistakes + left), (mistakes, left)


def test_settings_show_writes_keep_as_settings_toml_has_it(settings):
    from hylly import settings as the_settings

    (settings / "settings.toml").write_text("[backup]\nkeep = { daily = 7, weekly = 4 }\n", encoding="utf-8")
    the_settings.reload()
    assert any(line.strip().startswith("keep = { daily = 7, weekly = 4 }") for line in the_settings.listing())
    (settings / "settings.toml").write_text("", encoding="utf-8")
    the_settings.reload()
    assert any(line.strip().startswith("keep = {} (every zip stays)") for line in the_settings.listing())


def test_the_timers_run_a_plain_hylly_backup_download_and_lookup_every_night():
    """The house way: a systemd service and timer for each, beside hylly-jobs', and a launchd
    agent for the backup on macOS; the help gives Windows' line, quoted, its questions ended at once by < NUL. Their
    commands are ones the parser takes as written. The backup's hides the shops' logins; the download's and the
    lookup's can't, as they need them. Each runs at an hour of its own."""
    import html
    import plistlib
    import shlex
    import subprocess

    from hylly import commands

    repo = Path(__file__).resolve().parents[1]
    examples, hours = repo / "examples", set()
    jobs = (examples / "hylly-jobs.service").read_text(encoding="utf-8").splitlines()
    tool = next(line for line in jobs if line.startswith("ExecStart=")).split("=", 1)[1].split()[0]  # the tool's own hylly
    for name, argv in (("backup", ["backup"]), ("download", ["download"]), ("lookup", ["catalog", "--lookup"])):
        service = (examples / f"hylly-{name}.service").read_text(encoding="utf-8").splitlines()
        timer = (examples / f"hylly-{name}.timer").read_text(encoding="utf-8").splitlines()
        start = next(line for line in service if line.startswith("ExecStart="))
        assert shlex.split(start.split("=", 1)[1]) == [tool, *argv], name
        assert commands.build_parser().parse_args(argv).command == argv[0]
        assert "Type=oneshot" in service and "User=someone" in service and "NoNewPrivileges=yes" in service
        assert any("ssh-agent" in line for line in service if line.startswith("#")), name
        hidden = "InaccessiblePaths=/home/someone/.local/share/hylly/logins" in service
        assert hidden == (name == "backup") and ("UMask=0002" in service) == (name != "backup"), name  # these write the library
        assert "Persistent=true" in timer and "WantedBy=timers.target" in timer
        hours.add(next(line for line in timer if line.startswith("OnCalendar=")))
        if shutil.which("systemd-analyze"):  # systemd's own check, where there is one
            done = subprocess.run(["systemd-analyze", "verify", str(examples / f"hylly-{name}.service"),
                                   str(examples / f"hylly-{name}.timer")], capture_output=True, text=True)
            for word in ("Unknown key", "Unknown section", "Invalid", "Missing", "Failed to parse"):
                assert word not in done.stderr, done.stderr
    assert len(hours) == 3
    agent = plistlib.loads((examples / "hylly-backup.plist").read_bytes())
    assert agent["ProgramArguments"][1:] == ["backup"] and agent["Label"] == "io.github.janisc.hylly-backup"
    assert set(agent["StartCalendarInterval"]) == {"Hour", "Minute"}
    page = html.unescape((repo / "src" / "hylly" / "help.html").read_text(encoding="utf-8"))
    line = '/TR "cmd /c \\"\\"%USERPROFILE%\\.local\\bin\\hylly.exe\\" backup ^< NUL\\""'
    assert line in page and as_command_prompt_saves(line.split(" ", 1)[1]) == 'cmd /c ""%USERPROFILE%\\.local\\bin\\hylly.exe" backup < NUL"'


def as_command_prompt_saves(typed: str) -> str:
    """What schtasks gets for /TR from a line typed in Command Prompt, by cmd's rules: each " flips its quoting, and
    outside the quotes a ^ takes the next sign as it is; then, as Windows splits a program's arguments, \\" is a " in
    the value and a bare " opens or closes it. A < left outside the quotes would be cmd's own, and none would reach
    the task."""
    out, quoted, caret = "", False, False
    for sign in typed:
        if caret:
            out, caret = out + sign, False
        elif sign == "^" and not quoted:
            caret = True
        elif sign == "<" and not quoted:
            raise AssertionError("cmd would take < for itself")
        else:
            quoted = quoted != (sign == '"')
            out += sign
    value, i = "", 0
    while i < len(out):
        if out[i] == "\\" and out[i + 1:i + 2] == '"':
            value, i = value + '"', i + 2
        elif out[i] != '"':
            value, i = value + out[i], i + 1
        else:
            i += 1
    return value


def test_a_copy_of_the_records_dated_more_than_two_days_ahead_is_never_taken_and_is_said_once(library, top, settings, monkeypatch, tmp_path, capsys):
    """How far ahead is too far: time zones differ by a day at most, so a copy dated two days ahead may be a server's,
    and is taken; one dated further ahead was made under a clock that was wrong, and would be taken for ever: the
    backup with no fresh copy and the restore pass it over, and say so once."""
    from hylly import db

    owner = db.file()
    tag(library, "Knitting")
    now = datetime.now()
    marker = json.loads(db.owner_file(library).read_text(encoding="utf-8"))
    db.owner_file(library).write_text(json.dumps({**marker, "computer": "server-test"}), encoding="utf-8")
    yesterday = db.nightly(library, now - timedelta(days=1), where=owner).name
    later = (now + timedelta(days=3)).strftime("%Y-%m-%d")
    shutil.copytree(db.backups(library) / yesterday, db.backups(library) / later)  # as a clock days ahead made it
    computer(monkeypatch, tmp_path / "another-computer")  # no [remote] here: no fresh copy
    db.close_all()
    assert code_of(top, "--yes", "--library", library) == PARTLY
    out = capsys.readouterr().out
    assert f"are their copy of {yesterday}" in out and out.count("Passed over: 1 copy") == 1 and f"({later})" in out
    assert {n.split("/")[2] for n in in_the_zip(top) if n.startswith("state/db-backup/")} == {yesterday}
    ahead = (now + timedelta(days=2)).strftime("%Y-%m-%d")
    shutil.copytree(db.backups(library) / yesterday, db.backups(library) / ahead)  # a server's a day or two ahead: taken
    assert code_of(top, "--yes", "--library", library) == PARTLY
    assert {n.split("/")[2] for n in in_the_zip(top) if n.startswith("state/db-backup/")} == {ahead}
    placed, said = backup.records_in_place(library)
    assert placed and f"as copied on {ahead}" in said
    db.close_all()
    os.remove(db.file())
    shutil.rmtree(db.backups(library) / ahead)
    shutil.rmtree(db.backups(library) / yesterday)  # only the one dated far ahead: none to take
    placed, said = backup.records_in_place(library)
    assert not placed and "start afresh" in said and "Passed over: 1 copy" in said and not db.file().exists()


def test_keep_never_thins_the_zip_the_backup_has_just_made_whatever_the_others_are_named(library, top, settings, capsys):
    """The zip this run made is the newest, as the plan's coming one is, though a zip named ahead (a clock that
    was wrong, by two hours or by years) sorts after it: the plan and the run agree, and the new zip stays."""
    soon = datetime.now() + timedelta(hours=2)
    ahead = (f"{soon:%Y-%m-%d %H%M%S}.zip", "2099-01-01 000000.zip")
    own_zips(top, "2026-10-07 030000.zip", *ahead)
    (settings / "settings.toml").write_text("[backup]\nkeep = { daily = 1 }\n", encoding="utf-8")
    run(library, top, "--dry-run", "--json")
    planned = json.loads(capsys.readouterr().out)["plan"]["would_thin"]
    assert sorted(planned) == sorted(["2026-10-07 030000.zip", *ahead])
    run(library, top, "--json")
    done = json.loads(capsys.readouterr().out)
    assert sorted(done["thinned"]) == sorted(planned)
    assert [p.name for p in backup.saved_zips(top)] == [done["zip"]]
    made = backup.saved_of(top) / done["zip"]
    own_zips(top, ahead[0])  # daily 7, weekly 4, monthly 12, and a zip two hours ahead beside the one just made
    assert made not in backup.to_thin(top, {"daily": 7, "weekly": 4, "monthly": 12}, made=made)
