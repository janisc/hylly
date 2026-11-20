"""The database work's weak spots, each kept from coming back: the nightly copies by their names, one user's logins at
most, one library per database and one install per library, the Database page's reach, the secrets out of the plain
export, logins that end with a rename or a removal, and a new user who doesn't inherit someone else's reading."""

from __future__ import annotations

import json
import os
import secrets
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from hylly import access, cli, curation, db, dbpage, paths, people, shelves, storage
from hylly.exits import NEEDS_YOU

from helpers import CLOCK_TIMES, as_hylly_left_it_before, build_library, in_helsinki


# ---- a record's time ------------------------------------------------------------------------------

def test_a_record_s_time_has_its_offset_and_one_from_before_reads_as_this_computer_s_time_then():
    """db.now stamps every record with the local time and its offset; db.moment reads that, and a record's time from
    before (the local time alone) as this computer's local time then, so both side by side sort as the moments do: in
    summer, in winter and in the hour the clocks go back."""
    assert datetime.fromisoformat(db.now()).utcoffset() is not None
    for when, base in CLOCK_TIMES.items():
        before = (base - timedelta(hours=2)).isoformat()  # as records had it: the local time alone
        assert db.moment(before) == (base - timedelta(hours=2)).astimezone(), when
        assert db.moment(before).timestamp() == (base - timedelta(hours=2)).timestamp(), when
        after = (base.astimezone() + timedelta(hours=1)).isoformat()  # db.now's form, three hours on
        elsewhere = base.astimezone(timezone(timedelta(hours=-9))).isoformat()  # another zone's, two hours on
        assert sorted([after, elsewhere, before], key=db.moment) == [before, elsewhere, after], when
    with pytest.raises(ValueError):
        db.moment("soon")
    with pytest.raises(TypeError):
        db.moment(12)


def test_an_old_stamp_in_the_hour_the_clocks_go_back_is_read_as_the_first_of_the_two(monkeypatch):
    """In Finland 03:00 to 04:00 comes twice as the clocks go back: a time from before records had their offset, in
    that hour, can't tell which it was, and is read as the first (fold 0), and sorted by that moment, not by its text."""
    with in_helsinki(monkeypatch):
        assert db.moment("2026-10-25T03:30:00") == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
        assert sorted(["2026-10-25T03:10:00+02:00", "2026-10-25T03:30:00"], key=db.moment) == [
            "2026-10-25T03:30:00", "2026-10-25T03:10:00+02:00"]


# ---- the nightly copies ---------------------------------------------------------------------------

def test_only_real_nights_count_and_nothing_else_in_db_backup_is_removed(tmp_path):
    people.Person("rory").place({"id": "h:novel", "t": "The Novel", "f": "humble/b/novel.epub"}, 12, 0.25)
    db.own(db.database(), tmp_path)
    where = db.backups(tmp_path)
    for name in [f"9999-12-0{n}" for n in range(1, 8)] + ["my own copy"]:  # planted to sort last, and a folder of yours
        (where / name).mkdir(parents=True)
        (where / name / "check.json").write_text('{"integrity": "ok"}', encoding="utf-8")
    (where / "2026-11-03").mkdir()  # tonight's name, empty: put in its place
    made = db.nightly(tmp_path, now=datetime(2026, 11, 3, 4))
    assert made == where / "2026-11-03" and (made / db.FILE).is_file()
    assert db.last_backup(tmp_path)[0] == "2026-11-03"  # not a day yet to come
    for n in range(4, 12):
        db.nightly(tmp_path, now=datetime(2026, 11, n, 4))
    left = sorted(p.name for p in where.iterdir())
    assert [n for n in left if n.startswith("2026")] == [f"2026-11-{n:02d}" for n in range(5, 12)]  # a week of real nights
    assert "my own copy" in left and "9999-12-07" in left  # none of the rest is touched


def test_nightly_copies_also_go_by_age_once_a_newer_one_is_there(tmp_path):
    """On a server that runs only now and then, seven copies once spanned weeks, and the words
    taken away from a review with them, though the help said a week. Now none more than NIGHTS_DAYS old stays once a
    newer one is made, and the newest stays whatever its age."""
    people.Person("rory").place({"id": "h:novel", "t": "The Novel", "f": "humble/b/novel.epub"}, 12, 0.25)
    db.own(db.database(), tmp_path)
    for day in (1, 5, 12):  # a server that runs at weekends, say
        db.nightly(tmp_path, now=datetime(2026, 10, day, 4))
    assert [p.name for p in db.nights(tmp_path, "2026-12-31")] == ["2026-10-01", "2026-10-05", "2026-10-12"]
    db.nightly(tmp_path, now=datetime(2026, 10, 20, 4))  # 2026-10-01 and 2026-10-05 are more than 14 days old now
    assert [p.name for p in db.nights(tmp_path, "2026-12-31")] == ["2026-10-12", "2026-10-20"]
    db.nightly(tmp_path, now=datetime(2026, 12, 24, 4))  # after two months away, only the newest
    assert [p.name for p in db.nights(tmp_path, "2026-12-31")] == ["2026-12-24"]
    assert db.NIGHTS_DAYS == 14 and db.BACKUPS_KEPT == 7


def test_only_a_good_copy_makes_others_go_by_age_and_one_dated_ahead_never_does(tmp_path, monkeypatch):
    """The age is counted from this computer's day, so a copy dated far ahead (a clock that
    was wrong one night) neither counts nor makes others go; and copies go by their age only after a new one whose
    check is good, so that a damaged copy is never all that's left."""
    import shutil

    people.Person("rory").place({"id": "h:novel", "t": "The Novel", "f": "humble/b/novel.epub"}, 12, 0.25)
    db.own(db.database(), tmp_path)
    for day in (1, 5):
        db.nightly(tmp_path, now=datetime(2026, 10, day, 4))
    shutil.copytree(db.backups(tmp_path) / "2026-10-05", db.backups(tmp_path) / "2027-06-01")  # dated ahead
    db.nightly(tmp_path, now=datetime(2026, 10, 12, 4))
    names = lambda: sorted(p.name for p in db.backups(tmp_path).iterdir())  # noqa: E731
    assert names() == ["2026-10-01", "2026-10-05", "2026-10-12", "2027-06-01"]  # nothing went for the one ahead
    real = db.export
    monkeypatch.setattr(db, "export", lambda source, folder: {**real(source, folder), "integrity": "*** in database main ***"})
    db.nightly(tmp_path, now=datetime(2026, 10, 20, 4))  # 2026-10-01 and -05: old enough, but this copy isn't good
    assert names() == ["2026-10-01", "2026-10-05", "2026-10-12", "2026-10-20", "2027-06-01"]
    monkeypatch.setattr(db, "export", real)
    db.nightly(tmp_path, now=datetime(2026, 10, 21, 4))  # a good one: they go now; the one ahead stays as it is
    assert names() == ["2026-10-12", "2026-10-20", "2026-10-21", "2027-06-01"]


# ---- logins ----------------------------------------------------------------------------------------

def hashes() -> dict[str, str]:
    """Each user's password hash, by name: what the login check gives a browser's session to go by."""
    return {name: stored for name, (_, stored) in access.load_accounts().items()}


def test_one_user_logging_in_again_and_again_ends_only_their_own_oldest():
    access.add_user("rory", "correct horse", role="owner")
    access.add_user("bob", "battery staple", role="borrower")
    sessions = access.Sessions()
    rorys = sessions.start("rory", access.load_accounts()["rory"][1])
    bobs = [sessions.start("bob", access.load_accounts()["bob"][1]) for _ in range(access.SESSIONS_EACH + 5)]
    users = hashes()
    assert sessions.user(rorys, users) == "rory"  # still logged in
    assert sessions.user(bobs[0], users) is None and sessions.user(bobs[-1], users) == "bob"
    with access.store().read() as c:
        assert c.execute("SELECT count(*) FROM browser_login WHERE name = 'bob'").fetchone()[0] == access.SESSIONS_EACH


def test_renaming_back_or_removing_ends_the_logins_for_good():
    access.add_user("alice", "correct horse")
    sessions = access.Sessions()
    token = sessions.start("alice", access.load_accounts()["alice"][1])
    assert access.rename_user("alice", "amy") and access.rename_user("amy", "alice")
    assert sessions.user(token, hashes()) is None  # renamed back, still ended
    token = sessions.start("alice", access.load_accounts()["alice"][1])
    assert access.remove_user("alice")
    access.add_user("alice", "correct horse")  # the same name and password, back
    assert sessions.user(token, hashes()) is None


def test_a_new_user_doesnt_inherit_what_was_kept_of_someone_before():
    people.Person("eve").place({"id": "h:novel", "t": "A private book", "f": "humble/b/novel.epub"}, 12, 0.25)  # no account
    access.add_user("eve", "correct horse")
    assert people.Person("eve").latest() == {}
    access.add_user("finn", "battery staple")
    people.Person("finn").place({"id": "h:novel", "t": "Finn's", "f": "humble/b/novel.epub"}, 3, 0.1)
    access.add_user("gus", "battery staple")
    assert access.remove_user("gus") is True  # nothing kept of gus
    people.Person("gus").place({"id": "h:novel", "t": "Someone's", "f": "humble/b/novel.epub"}, 3, 0.1)  # kept without a user
    assert access.rename_user("finn", "gus")  # what's kept about finn goes with them, in the same change
    found = people.Person("gus").latest()
    assert list(found) == ["h:novel"] and found["h:novel"].get("t") == "Finn's"


# ---- one library per database ------------------------------------------------------------------------

def test_a_second_library_isnt_taken_on_while_the_first_is_there(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    build_library(first)
    second.mkdir()
    curation.change(first, "add", "Knitting", [{"id": "h:novel", "t": "The Novel", "f": [["humble/b/novel.epub"]]}], "owen")
    with pytest.raises(db.NotHere, match="doesn't take on"):
        curation.load(second)
    moved = tmp_path / "moved"
    first.rename(moved)  # the same library, moved: taken on, records and all
    assert curation.load(moved).of(["h:novel"]) == ["Knitting"]


def test_a_markers_computer_name_is_shown_only_if_it_looks_like_one(tmp_path):
    build_library(tmp_path)
    db.owner_file(tmp_path).write_text(json.dumps({"id": "0" * 32, "computer": "\x1b[2Jrun rm -rf / there"}), encoding="utf-8")
    db.close_all()  # as at the next start
    with pytest.raises(db.NotHere) as refused:
        curation.load(tmp_path)
    assert "\x1b" not in str(refused.value) and "another computer" in str(refused.value)


def test_the_marker_is_read_and_judged_in_one_place_for_all_who_read_it(tmp_path):
    """db.owner: the marker as it says, its id there; one damaged, a list, or one that names no id is a ValueError to
    each reader of it, and none at all a FileNotFoundError, so that each answers as it does for those."""
    from hylly import backup

    build_library(tmp_path)
    mine = db.owner(tmp_path)
    assert isinstance(mine["id"], str) and db.marked(tmp_path) == mine["id"]
    db._copied_here(tmp_path, db.file())  # the database the marker names: copied
    for damaged in ("[1, 2]", '{"id": ', '{"computer": "elsewhere"}', '{"id": 12}', '"text"'):
        db.owner_file(tmp_path).write_text(damaged, encoding="utf-8")
        with pytest.raises(ValueError):
            db.owner(tmp_path)
        assert db.marked(tmp_path) is None, damaged
        with pytest.raises(db.NotHere, match="can't be read"):
            db._copied_here(tmp_path, db.file())
        assert backup.fresh_records(tmp_path)[1], damaged  # its words say why there's no fresh copy, and nothing trips
    db.owner_file(tmp_path).unlink()
    with pytest.raises(FileNotFoundError):
        db.owner(tmp_path)
    assert db.marked(tmp_path) is None
    with pytest.raises(db.NotHere, match="isn't there"):
        db._copied_here(tmp_path, db.file())


def test_no_copy_goes_while_the_newest_isnt_good(tmp_path, monkeypatch):
    """A damaged database copied night after night once pushed the last good copies out a week later: now none goes
    after a copy whose check isn't good, by their number or their age, until a good one is made."""
    people.Person("rory").place({"id": "h:novel", "t": "The Novel", "f": "humble/b/novel.epub"}, 12, 0.25)
    db.own(db.database(), tmp_path)
    for day in range(1, 8):
        db.nightly(tmp_path, now=datetime(2026, 10, day, 4))
    real = db.export
    monkeypatch.setattr(db, "export", lambda source, folder: {**real(source, folder), "integrity": "*** in database main ***"})
    for day in range(8, 12):
        db.nightly(tmp_path, now=datetime(2026, 10, day, 4))
    assert [p.name for p in db.nights(tmp_path, "2026-12-31")] == [f"2026-10-{day:02d}" for day in range(1, 12)]
    monkeypatch.setattr(db, "export", real)
    db.nightly(tmp_path, now=datetime(2026, 10, 12, 4))
    assert [p.name for p in db.nights(tmp_path, "2026-12-31")] == [f"2026-10-{day:02d}" for day in range(6, 13)]


# ---- one install per library: the marker's token ----------------------------------------------------


def test_a_marker_as_hylly_wrote_it_before_is_taken_on_quietly_and_tied_to_this_install_once(tmp_path):
    """The move from before: a library's live server opens its marker as the version before wrote it. Never refused: it
    names this database, so this install's token is written into it, once, with a line for the server's log; and from
    then on it's read as any other."""
    root = tmp_path / "library"
    before = as_hylly_left_it_before(root)
    with db.database().write() as c:  # a tag the library had then
        curation._put(c, {"at": "2026-11-01T10:00:00", "by": "owen", "field": "tag", "op": "add", "value": "Knitting",
                          "titles": [{"id": "h:novel", "t": "The Novel", "f": "humble/b/novel.epub"}]})
    assert not db.token_file().exists()
    db.close_all()  # as at the start of the new version
    found, notes = db.database(), []
    found.on_note = notes.append
    db.own(found, root)
    marker = json.loads(db.owner_file(root).read_text(encoding="utf-8"))
    token = db.token_file().read_text(encoding="utf-8").strip()
    assert marker == {**before, "token": token} and len(token) == 32  # its id, computer and since as they were
    assert len(notes) == 1 and "names this install too" in notes[0]
    if os.name != "nt":
        assert db.token_file().stat().st_mode & 0o777 == 0o600
    tied = db.owner_file(root).stat()
    db.close_all()  # the next start, and the commands
    db.own(db.database(), root)
    assert curation.load(root).of(["h:novel"]) == ["Knitting"]
    curation.change(root, "add", "Weaving", [{"id": "h:novel", "t": "The Novel", "f": [["humble/b/novel.epub"]]}], "owen")
    after = db.owner_file(root).stat()
    assert (after.st_mtime_ns, after.st_ino) == (tied.st_mtime_ns, tied.st_ino)  # written once
    assert db.token_file().read_text(encoding="utf-8").strip() == token


def test_a_marker_as_hylly_wrote_it_before_that_cant_be_tied_just_now_still_counts_as_it_did(tmp_path, monkeypatch):
    """A library's disk that's read-only for a while, say: never a reason to refuse the records to the database the
    marker names. It's tied at a later start."""
    root = tmp_path / "library"
    before = as_hylly_left_it_before(root)
    db.close_all()
    with monkeypatch.context() as m:
        m.setattr(db, "atomic_write", lambda *a, **k: (_ for _ in ()).throw(PermissionError(30, "Read-only file system")))
        db.own(db.database(), root)
        curation.change(root, "add", "Knitting", [{"id": "h:novel", "t": "The Novel", "f": [["humble/b/novel.epub"]]}], "owen")
    assert json.loads(db.owner_file(root).read_text(encoding="utf-8")) == before
    db.close_all()
    db.own(db.database(), root)
    tied = json.loads(db.owner_file(root).read_text(encoding="utf-8"))
    assert tied["token"] == db.token_file().read_text(encoding="utf-8").strip()


def test_a_marker_as_hylly_wrote_it_before_naming_another_database_is_still_refused(tmp_path):
    root = tmp_path / "library"
    as_hylly_left_it_before(root)
    db.close_all()  # first: Windows moves no file that's open
    shutil.move(db.file(), tmp_path / "theirs.db")  # that database is another computer's: this one has none of it
    with pytest.raises(db.NotHere, match="first stop the server on"):
        curation.load(root)
    assert "token" not in json.loads(db.owner_file(root).read_text(encoding="utf-8"))


def other_install(monkeypatch, where) -> None:
    """Another settings folder from here on, as another computer, or another install on this one, has."""
    monkeypatch.setenv("HYLLY_HOME", str(where))
    db.close_all()


def test_a_copy_of_the_database_elsewhere_is_refused_until_it_takes_the_records_over(tmp_path, monkeypatch, capsys):
    """The same hylly.db in two settings folders once passed the owner check from both, each keeping its own tags. A
    copy has the id, not the install's token: refused, with the one command that takes the records over, which asks
    first; after it, the one that kept them is the copy."""
    root, first, second = tmp_path / "library", os.environ["HYLLY_HOME"], tmp_path / "second"
    build_library(root)
    curation.change(root, "add", "Knitting", [{"id": "h:novel", "t": "The Novel", "f": [["humble/b/novel.epub"]]}], "owen")
    db.database().checkpoint()
    copied = db.file()
    db.close_all()
    other_install(monkeypatch, second)
    db.file().parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(copied, db.file())  # the database, copied: its token stays where it was
    with pytest.raises(db.NotHere, match="If this database is a copy of the one that keeps them") as refused:
        curation.load(root)  # this data folder has no token: a copy, or its own gone astray, which nothing tells apart
    assert "hylly server take-over --library" in str(refused.value)
    with pytest.raises(db.NotHere):
        db.nightly(root, datetime(2026, 11, 5, 4))  # nor are its copies made over the first's
    cli.main(["server", "take-over", "--library", str(root), "--dry-run"])
    out = capsys.readouterr().out
    assert "another copy of this same database" in out and "hylly server stop" in out and "Nothing changed." in out
    with pytest.raises(SystemExit) as ended:  # nobody at the keyboard to say yes
        cli.main(["server", "take-over", "--library", str(root)])
    assert ended.value.code == NEEDS_YOU
    cli.main(["server", "take-over", "--library", str(root), "--yes"])
    assert "kept in this computer's database from now on" in capsys.readouterr().out
    assert curation.load(root).of(["h:novel"]) == ["Knitting"]  # its records, as copied
    cli.main(["server", "take-over", "--library", str(root)])
    assert "keeps the records of the library at" in capsys.readouterr().out  # nothing to do: no question
    other_install(monkeypatch, first)
    with pytest.raises(db.NotHere, match="but as a copy"):
        curation.load(root)  # the one that kept them is the copy now


def test_an_install_whose_token_was_left_behind_is_told_so_and_how_to_mend_it(tmp_path, monkeypatch, capsys):
    """An install's one folder split by kind, records-token left behind: the records were refused as a copy's, the
    words naming another data folder. Now they say the token is missing here, and both ways out work."""
    root = tmp_path / "library"
    build_library(root)
    token = db.token_file().read_text(encoding="utf-8")
    db.token_file().unlink()
    db.close_all()
    with pytest.raises(db.NotHere) as refused:
        curation.load(root)
    said = str(refused.value)
    assert f"{db.token_file()}, which holds it beside hylly.db, is missing" in said and "put it back" in said
    assert "hylly server take-over --library" in said
    db.token_file().write_text(token, encoding="utf-8")  # put back
    db.close_all()
    assert curation.load(root).of(["h:novel"]) == []
    db.token_file().write_text("spoiled by hand\n", encoding="utf-8")
    db.close_all()
    with pytest.raises(db.NotHere, match="holds no token"):
        curation.load(root)
    cli.main(["server", "take-over", "--library", str(root), "--yes"])  # or taken over: a new token in its place
    assert "kept in this computer's database from now on" in capsys.readouterr().out
    assert curation.load(root).of(["h:novel"]) == [] and db.token_file().read_text(encoding="utf-8") != token


@pytest.mark.slow
def test_programs_of_one_install_tying_a_marker_from_before_at_once_agree_on_its_token(tmp_path, monkeypatch):
    """The server starting and a command, or the jobs' timer, meeting a marker from before tokens at the same moment:
    one once found the token file made but not yet written, took it away and made its own, and the marker could be
    left naming a token no longer on the disk: every command after refused as a copy's. Now the token is written whole
    and linked into place, never taken away, and each ties the marker with the one in place."""
    import threading
    import time

    root = tmp_path / "library"
    before = as_hylly_left_it_before(root)
    real_fdopen = db.os.fdopen

    def a_slow_disk(*args, **kwargs):  # a moment between making a file and writing into it
        time.sleep(0.01)
        return real_fdopen(*args, **kwargs)

    monkeypatch.setattr(db.os, "fdopen", a_slow_disk)
    for _ in range(25):
        db.token_file().unlink(missing_ok=True)
        storage.atomic_write(db.owner_file(root), json.dumps(before, indent=1))
        db.close_all()
        start, failed = threading.Barrier(4), []

        def one():
            found = db.Database(db.file())  # a program of its own: its own connection, nothing remembered
            try:
                start.wait()
                db.own(found, root)
            except Exception as e:  # noqa: BLE001 - said below
                failed.append(e)
            finally:
                found.close()

        programs = [threading.Thread(target=one) for _ in range(4)]
        for program in programs:
            program.start()
        for program in programs:
            program.join(30)
        assert not failed, failed
        marker = json.loads(db.owner_file(root).read_text(encoding="utf-8"))
        assert marker["token"] == db.token_file().read_text(encoding="utf-8").strip()
        assert not [p for p in db.token_file().parent.iterdir() if p.name.endswith(".tmp")]
    monkeypatch.setattr(db.os, "fdopen", real_fdopen)
    db.close_all()
    assert curation.load(root).of(["h:novel"]) == []  # the next start: still its own


def spoiled_by_a_crash(root, said: str = "") -> dict:
    """A marker from before tokens, and the token file made in place but never written whole: a crash on a disk without
    hard links, where _place_token makes it by an exclusive create, leaves it empty, or cut short; and the file it was
    first written to, on its way, is left too."""
    before = as_hylly_left_it_before(root)
    db.token_file().write_text(said, encoding="utf-8")
    left = db.token_file().with_name(f"{db.TOKEN}.0a1b2c3d.tmp")
    left.write_text(secrets.token_hex(16) + "\n", encoding="utf-8")
    os.utime(left, (left.stat().st_mtime - 120,) * 2)  # two minutes ago
    db.close_all()
    return before


@pytest.mark.parametrize("said", ["", "3fa9c0\n"])
def test_a_token_left_empty_or_cut_short_by_a_crash_is_made_again_whole_and_the_marker_tied(tmp_path, said):
    """Every program waited a second for it, and never tied the marker: the file held no token, and only take-over, after
    a yes, made a new one. Now one that stays so counts as missing beside a marker from before tokens: made again
    whole, as a new one is, and the marker tied to it; the records were never refused meanwhile, and aren't now. What
    was left of the crash's own writing goes once it's a minute old; one younger may be another program's, on its way."""
    root = tmp_path / "library"
    before = spoiled_by_a_crash(root, said)
    young = db.token_file().with_name(f"{db.TOKEN}.4e5f6a7b.tmp")
    young.write_text("being written\n", encoding="utf-8")
    db.own(db.database(), root)
    token = db.token_file().read_text(encoding="utf-8").strip()
    assert db.TOKEN_TEXT.fullmatch(token) and json.loads(db.owner_file(root).read_text(encoding="utf-8")) == {**before, "token": token}
    assert [p.name for p in db.token_file().parent.glob(f"{db.TOKEN}.*.tmp")] == [young.name]
    db.close_all()
    curation.change(root, "add", "Knitting", [{"id": "h:novel", "t": "The Novel", "f": [["humble/b/novel.epub"]]}], "owen")
    assert curation.load(root).of(["h:novel"]) == ["Knitting"] and db.token_file().read_text(encoding="utf-8").strip() == token


@pytest.mark.slow
def test_programs_of_one_install_meeting_a_token_left_empty_at_once_agree_on_the_new_one(tmp_path, monkeypatch):
    """The server and a timer at a start, meeting a token left empty beside a marker from before tokens: each made a new
    one in its place and tied the marker, and the last marker written and the last token could be different programs':
    every command after refused the records, until take-over. One makes it at a time now, the rest reading it again
    once their turn comes: one token, which the marker names, and the records are this install's after."""
    import threading
    import time

    root = tmp_path / "library"
    before = as_hylly_left_it_before(root)
    real_replace = db.os.replace

    def a_slow_disk(src, dst):  # a moment for a write to the token file
        time.sleep(0.01)
        return real_replace(src, dst)

    monkeypatch.setattr(db.os, "replace", a_slow_disk)
    monkeypatch.setattr(db, "READ_BACK", 4)  # a short look for one being written, as nothing writes it here
    for _ in range(12):
        db.token_file().write_text("", encoding="utf-8")
        storage.atomic_write(db.owner_file(root), json.dumps(before, indent=1))
        db.close_all()
        start, failed = threading.Barrier(3), []

        def one():
            found = db.Database(db.file())  # a program of its own: its own connection, nothing remembered
            try:
                start.wait()
                db.own(found, root)
            except Exception as e:  # noqa: BLE001 - said below
                failed.append(e)
            finally:
                found.close()

        programs = [threading.Thread(target=one) for _ in range(3)]
        for program in programs:
            program.start()
        for program in programs:
            program.join(30)
        assert not failed, failed
        marker = json.loads(db.owner_file(root).read_text(encoding="utf-8"))
        assert marker["token"] == db.token_file().read_text(encoding="utf-8").strip()
        db.close_all()
        assert curation.load(root).of(["h:novel"]) == []  # the next command: not refused
    db.close_all()


def test_where_no_turn_can_be_had_a_token_left_empty_is_left_for_take_over(tmp_path, monkeypatch, capsys):
    """On a disk that keeps no locks, programs meeting a token left empty at once would each put their own in its place
    again, and the marker could be left naming one that went. There, none replaces it: the marker from before tokens
    counts as it did, the records are never refused, the doctor names the file, and take-over, the owner's, mends it."""
    import threading

    from hylly import doctor, instance

    root = tmp_path / "library"
    before = spoiled_by_a_crash(root)
    monkeypatch.setattr(instance, "_lock", lambda fh: False)  # no turn to be had, ever
    monkeypatch.setattr(db, "MAKING_WAIT", 0.2)
    monkeypatch.setattr(db, "READ_BACK", 4)
    for _ in range(3):
        db.close_all()
        start, failed = threading.Barrier(3), []

        def one():
            found = db.Database(db.file())
            try:
                start.wait()
                db.own(found, root)
            except Exception as e:  # noqa: BLE001 - said below
                failed.append(e)
            finally:
                found.close()

        programs = [threading.Thread(target=one) for _ in range(3)]
        for program in programs:
            program.start()
        for program in programs:
            program.join(30)
        assert not failed, failed
        assert json.loads(db.owner_file(root).read_text(encoding="utf-8")) == before and db.token_file().read_text(encoding="utf-8") == ""
    db.close_all()
    assert curation.load(root).of(["h:novel"]) == []  # never refused
    (found,) = [c for c in doctor._underneath(root) if c["id"] == "records"]
    assert found["state"] == doctor.PROBLEM and str(db.token_file()) in found["says"] and "hylly server take-over" in found["next"]
    db.close_all()
    cli.main(["server", "take-over", "--library", str(root)])
    assert "made again, as it held none." in capsys.readouterr().out
    token = db.token_file().read_text(encoding="utf-8").strip()
    assert db.TOKEN_TEXT.fullmatch(token) and json.loads(db.owner_file(root).read_text(encoding="utf-8")) == {**before, "token": token}
    db.close_all()
    assert curation.load(root).of(["h:novel"]) == [] and [c for c in doctor._underneath(root) if c["id"] == "records"] == []


def test_take_over_mends_a_token_left_empty_by_a_crash(tmp_path, capsys):
    """take-over said this install keeps the records already, nothing changed, and left the token as it was. It still
    says it keeps them, and says what it did: the token made again, and the marker tied to it."""
    root = tmp_path / "library"
    before = spoiled_by_a_crash(root)
    cli.main(["server", "take-over", "--library", str(root)])
    out = capsys.readouterr().out
    assert "keeps the records of the library at" in out and "Nothing changed" not in out
    assert f"now names this install too, by the token in {db.token_file()}, made again, as it held none." in out
    token = db.token_file().read_text(encoding="utf-8").strip()
    assert db.TOKEN_TEXT.fullmatch(token) and json.loads(db.owner_file(root).read_text(encoding="utf-8")) == {**before, "token": token}


def test_a_dry_run_leaves_what_a_crash_left_beside_the_token(tmp_path, capsys):
    """A dry run changes nothing: its look at the token deletes none of the files a crash left beside it, and makes
    no token."""
    root = tmp_path / "library"
    spoiled_by_a_crash(root)
    left = sorted(p.name for p in db.token_file().parent.glob(f"{db.TOKEN}.*.tmp"))
    cli.main(["server", "take-over", "--library", str(root), "--dry-run"])
    assert "Nothing changed." in capsys.readouterr().out
    assert sorted(p.name for p in db.token_file().parent.glob(f"{db.TOKEN}.*.tmp")) == left and left
    assert db.token_file().read_text(encoding="utf-8") == ""


def test_the_doctor_names_a_token_left_empty_that_can_t_be_made_again(tmp_path, monkeypatch):
    """The doctor said nothing of it. Where it can't be made again (a data folder that can't be written, say), the
    records are still this install's, as the marker is from before tokens, and the doctor names the file and the way
    out; with the token whole, nothing."""
    from hylly import doctor

    root = tmp_path / "library"
    build_library(root)
    marker = json.loads(db.owner_file(root).read_text(encoding="utf-8"))
    storage.atomic_write(db.owner_file(root), json.dumps({k: v for k, v in marker.items() if k != "token"}, indent=1))
    db.token_file().write_text("", encoding="utf-8")
    db.close_all()
    with monkeypatch.context() as m:
        m.setattr(db, "_place_token", lambda path, instead=False: (_ for _ in ()).throw(PermissionError(13, "Permission denied")))
        assert curation.load(root).of(["h:novel"]) == []  # never refused for it
        (found,) = [c for c in doctor._underneath(root) if c["id"] == "records"]
    assert found["state"] == doctor.PROBLEM and found["owner"] and str(db.token_file()) in found["says"]
    assert "hylly server take-over" in found["next"] and str(db.token_file().parent) in found["next"]
    db.close_all()
    curation.load(root)  # made again now, and tied
    assert [c for c in doctor._underneath(root) if c["id"] == "records"] == []


def test_a_whole_token_and_the_marker_tied_to_it_are_left_as_they_are(tmp_path, capsys):
    """A whole token, and the library's marker tied to it: no command, take-over or doctor writes either again, or makes
    another token."""
    from hylly import doctor

    root = tmp_path / "library"
    build_library(root)
    token, marker = db.token_file(), db.owner_file(root)
    was = [(p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in (token, marker)]
    db.close_all()
    curation.change(root, "add", "Knitting", [{"id": "h:novel", "t": "The Novel", "f": [["humble/b/novel.epub"]]}], "owen")
    cli.main(["server", "take-over", "--library", str(root)])
    out = capsys.readouterr().out
    assert "keeps the records of the library at" in out and "already. Nothing changed." in out
    assert [c for c in doctor._underneath(root) if c["id"] == "records"] == []
    db.close_all()
    assert curation.load(root).of(["h:novel"]) == ["Knitting"]
    assert [(p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in (token, marker)] == was


def test_with_the_library_s_disk_away_another_folder_isnt_taken_on_in_its_place_until_it_s_said_to_be_it(tmp_path, monkeypatch, capsys):
    """With the first library's disk unplugged, another folder was once taken on under the same id, its catalog
    written over the first's records. Now only an old place that can be read, a library still but without its marker,
    lets a folder be taken as it, moved: else hylly server take-over says it is."""
    disk = tmp_path / "disk"
    first, other = disk / "library", tmp_path / "other"
    build_library(first)
    curation.change(first, "add", "Knitting", [{"id": "h:novel", "t": "The Novel", "f": [["humble/b/novel.epub"]]}], "owen")
    other.mkdir()
    disk.rename(tmp_path / "unplugged")  # gone, as a disk away is on Windows
    for unmounted in (False, True):
        if unmounted:  # and on Linux and macOS, its folder left there, empty
            first.mkdir(parents=True)
            monkeypatch.setattr(paths, "REQUIRE_MARKER", True)
            paths.marker_file(other).parent.mkdir(parents=True, exist_ok=True)
            paths.marker_file(other).write_text('{"id": "made-up"}', encoding="utf-8")
        db.close_all()
        with pytest.raises(db.NotHere, match="can't be reached now") as refused:
            curation.load(other)
        assert "hylly server take-over --library" in str(refused.value) and not db.owner_file(other).exists()
        with db.database().read() as c:
            assert c.execute("SELECT root FROM library").fetchone()[0] == os.path.abspath(first)
    cli.main(["server", "take-over", "--library", str(other), "--yes"])
    assert "is that library, moved" in capsys.readouterr().out
    assert curation.load(other).of(["h:novel"]) == ["Knitting"]


def test_a_library_moved_away_from_a_place_still_there_without_its_marker_is_taken_on_as_before(tmp_path):
    first, moved = tmp_path / "first", tmp_path / "moved"
    build_library(first)
    curation.change(first, "add", "Knitting", [{"id": "h:novel", "t": "The Novel", "f": [["humble/b/novel.epub"]]}], "owen")
    shutil.copytree(first, moved)
    db.owner_file(first).unlink()  # the old place can be read, and names no database now
    db.close_all()
    assert curation.load(moved).of(["h:novel"]) == ["Knitting"]


def test_of_two_computers_taking_a_new_library_on_at_once_only_the_one_its_marker_names_goes_on(tmp_path, monkeypatch):
    """Both once passed: the marker was written with a plain replace and not read back. Now it's made only if there
    is none, and read back: the one it doesn't name is refused, its library row taken back."""
    root = tmp_path / "library"
    (root / "state").mkdir(parents=True)
    theirs = {"id": "f" * 32, "computer": "elsewhere", "since": "2026-11-18T10:00:00+02:00", "token": "e" * 32}
    real, made = db.secrets.token_hex, []

    def as_this_one_makes_its_id(n):  # after this one found no marker, and before it writes its own: theirs
        if not made:
            made.append(n)
            storage.atomic_write(db.owner_file(root), json.dumps(theirs, indent=1))
        return real(n)

    monkeypatch.setattr(db.secrets, "token_hex", as_this_one_makes_its_id)
    with pytest.raises(db.NotHere, match="kept in the database on elsewhere"):
        db.own(db.database(), root)
    assert json.loads(db.owner_file(root).read_text(encoding="utf-8")) == theirs  # never written over
    with db.database().read() as c:
        assert c.execute("SELECT count(*) FROM library").fetchone()[0] == 0
    assert db.database().owned == {}


def test_a_folder_that_isnt_the_library_now_is_taken_on_by_nothing(tmp_path, monkeypatch):
    """An unmounted disk's empty folder once got a marker of the records, and state folders, at a look at the shelves:
    now it's no library to take on, and the look fails as a change does."""
    monkeypatch.setattr(paths, "REQUIRE_MARKER", True)
    mount = tmp_path / "mnt" / "books"
    mount.mkdir(parents=True)
    with pytest.raises(OSError, match="isn't there"):
        db.own(db.database(), mount)
    with pytest.raises(OSError, match="isn't there"):
        shelves.Shared(mount).read()
    assert list(mount.iterdir()) == []


# ---- the Database page, and the plain export -------------------------------------------------------

def test_the_database_page_reads_only_the_databases_own_tables_and_no_dangerous_function():
    access.add_user("rory", "correct horse", role="owner")
    with access.store().write() as c:
        c.execute("INSERT INTO shared_shelf (id, body) VALUES ('pasted', ?)",  # a hash where no hash should be
                  (db.dump({"comment": "#bob:reader:scrypt$16384$8$1$c2FsdA==$aGFzaA=="}),))
    c, _ = dbpage.connect()
    try:
        for sql in ("SELECT fts3_tokenizer('simple')", "SELECT * FROM dbstat", "SELECT * FROM sqlite_dbpage",
                    "SELECT load_extension('x')"):
            with pytest.raises(dbpage.Refused):
                dbpage._ask(c, sql)
        with pytest.raises(dbpage.Refused):
            dbpage._ask(c, "SELECT length(zeroblob(100000000))")  # more than a question may make
        assert dbpage._ask(c, "SELECT count(*) FROM downloaded")["rows"] == [[0]]  # a view of the database's own
        assert dbpage._ask(c, "SELECT count(*) FROM json_each('[1, 2]')")["rows"] == [[2]]  # JSON taken apart
        assert dbpage._ask(c, "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n LIMIT 3) SELECT count(*) FROM n")["rows"] == [[3]]
        shown = dbpage._ask(c, "SELECT body FROM shared_shelf")["rows"][0][0]
        assert "aGFzaA" not in shown and "scrypt$" in shown
        assert dbpage._ask(c, "SELECT hash FROM account")["rows"] == [[None]]
    finally:
        c.close()


def test_the_plain_export_beside_a_nightly_copy_leaves_the_secrets_out(tmp_path):
    access.add_user("rory", "correct horse", role="owner")
    access.Sessions().start("rory", access.load_accounts()["rory"][1])
    db.own(db.database(), tmp_path)
    night = db.nightly(tmp_path, now=datetime(2026, 11, 3, 4))
    account = [json.loads(line) for line in (night / "account.jsonl").read_text(encoding="utf-8").splitlines()]
    logins = [json.loads(line) for line in (night / "browser_login.jsonl").read_text(encoding="utf-8").splitlines()]
    assert account and "hash" not in account[0] and account[0]["name"] == "rory"
    assert logins and "digest" not in logins[0] and "stamp" not in logins[0]
    copy = db.connect(night / db.FILE, readonly=True)
    try:
        assert copy.execute("SELECT hash FROM account").fetchone()[0].startswith("scrypt$")  # the copy itself has them, to go back to
    finally:
        copy.close()


def test_a_tag_of_another_kind_than_words_is_passed_over(tmp_path):
    build_library(tmp_path)
    with curation.store(tmp_path).write() as c:
        curation._put(c, {"at": "2026-11-10T10:00:00", "by": "x", "field": "tag", "op": "add", "value": 5,
                          "titles": [{"id": "h:novel", "t": "The Novel", "f": "humble/b/novel.epub"}]})
    assert curation.load(tmp_path).of(["h:novel"]) == []
