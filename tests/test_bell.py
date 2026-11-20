"""Alarms: only what is worth a look goes out at once, each kind at most once in a while; the rest is a daily summary."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import httpx
import pytest

from hylly import access, bell, catalog, serve

from helpers import WINDOWS_FIREFOX, build_library, watch


IPAD = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.6 Safari/605.1.15"


def test_kinds_of_device():
    assert bell.device(IPAD) == "Mac or iPad · Safari"
    assert bell.device("Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 Version/26.6 Mobile/15E148 Safari/604.1") == "iPhone · Safari"
    assert bell.device(WINDOWS_FIREFOX) == "Windows · Firefox"
    assert bell.device("Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/141.0 Safari/537.36 Edg/141.0") == "Windows · Edge"
    assert bell.device("Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/141.0 Mobile Safari/537.36") == "Android · Chrome"
    assert bell.device("Yomu/100 CFNetwork/1500.0.1 Darwin/24.0.0") == "Yomu (app)"
    assert bell.device("KOReader/2026.07") == "KOReader (app)" and bell.device("") == "unknown app"


def test_wrong_passwords_for_a_real_user():
    w, sent, clock = watch(wrong_passwords=3)
    for _ in range(5):
        w.event("wrong", name="admin", ip="203.0.113.9", known=False)  # a bot's guess: not an alarm
    assert sent == []
    w.event("wrong", name="alice", ip="203.0.113.9", known=True)
    w.event("wrong", name="alice", ip="198.51.100.4", known=True)
    assert sent == []
    w.event("wrong", name="alice", ip="203.0.113.9", known=True)
    assert len(sent) == 1 and "3 wrong passwords for alice" in sent[0][1] and "1 other address" in sent[0][1]
    w.event("wrong", name="alice", ip="203.0.113.9", known=True)
    assert len(sent) == 1  # the same alarm again only after quiet_minutes
    clock[0] += 61 * 60
    for _ in range(3):
        w.event("wrong", name="alice", ip="203.0.113.9", known=True)
    assert len(sent) == 2
    clock[0] += 16 * 60  # older ones fall out of the 15 minutes
    w.event("wrong", name="alice", ip="203.0.113.9", known=True)
    assert len(sent) == 2


def test_a_lockout_is_an_alarm():
    w, sent, clock = watch()
    w.event("locked", ip="203.0.113.9", name="root")
    assert sent[0][0] == "An address locked out" and "203.0.113.9" in sent[0][1] and "'root'" in sent[0][1]
    clock[0] += 61 * 60
    w.event("locked", ip="203.0.113.9", name=access.unknown_name("Tulip Kettle 42"), known=False)  # as the login check sends
    assert "(last tried as an unknown name of 15 characters)" in sent[1][1]  # one that isn't a user's: its length only


def test_a_new_kind_of_device_once():
    w, sent, _ = watch()
    w.event("login", user="alice", ip="203.0.113.9", agent=IPAD, via="login page")
    w.event("login", user="alice", ip="198.51.100.4", agent=IPAD, via="login page")  # another address, the same iPad
    assert len(sent) == 1 and "alice logged in from Mac or iPad · Safari (login page)" in sent[0][1]
    again, sent2, _ = watch()  # remembered across a restart
    again.event("login", user="alice", ip="203.0.113.9", agent=IPAD)
    again.event("login", user="alice", ip="203.0.113.9", agent=WINDOWS_FIREFOX)
    assert len(sent2) == 1 and "Windows · Firefox" in sent2[0][1]
    off, sent3, _ = watch(new_devices=False)
    off.event("login", user="ben", ip="203.0.113.9", agent=WINDOWS_FIREFOX)
    assert sent3 == []


def test_a_renamed_user_keeps_their_devices_and_a_later_user_of_the_old_name_has_none():
    """The devices go by name, as logins do, and a rename is made by a command, in the database, while the server holds
    bell.json in its memory: the server follows each user by what a rename keeps, so the renamed user gets no new-device
    alarm under the new name, a new user given the old name inherits nothing, and what the server saves next says so."""
    access.add_user("ann", "correct horse")
    w, sent, clock = watch()
    w.event("login", user="ann", ip="203.0.113.9", agent=IPAD)
    assert [title for title, _, _ in sent] == ["New device: ann"]
    access.rename_user("ann", "anna")  # hylly user rename, while the server runs
    w.event("login", user="anna", ip="203.0.113.9", agent=IPAD)
    assert len(sent) == 1  # theirs already, under the new name
    access.add_user("ann", "battery staple")  # someone new given the old name
    clock[0] += 61 * 60  # past quiet_minutes, which holds back the same alarm again
    w.event("login", user="ann", ip="198.51.100.4", agent=IPAD)
    assert [title for title, _, _ in sent] == ["New device: ann", "New device: ann"]
    saved = json.loads(bell.state_file().read_text(encoding="utf-8"))
    assert saved["devices"] == {"anna": ["Mac or iPad · Safari"], "ann": ["Mac or iPad · Safari"]}
    access.add_user("anna", "a new password")  # a new password: the same user, the same devices
    again, sent2, _ = watch()  # and over a restart
    again.event("login", user="anna", ip="203.0.113.9", agent=IPAD)
    assert sent2 == []


def test_the_minute_s_look_moves_a_renamed_user_s_devices_and_lets_a_removed_one_s_go():
    """Each minute's look at the users follows them too, logins or not: a rename made while the server was stopped is
    followed at its first look, and the devices of a user removed go, so that a later user of the name has none."""
    access.add_user("owen", "correct horse")
    access.add_user("ben", "correct horse")
    w, sent, _ = watch()
    for user in ("owen", "ben"):
        w.event("login", user=user, ip="203.0.113.9", agent=WINDOWS_FIREFOX)
    access.rename_user("ben", "benny")
    w.check_users()
    assert json.loads(bell.state_file().read_text(encoding="utf-8"))["devices"] == {"owen": ["Windows · Firefox"],
                                                                                     "benny": ["Windows · Firefox"]}
    access.remove_user("benny")
    again, sent2, _ = watch()  # a server started afresh
    again.check_users()
    assert list(json.loads(bell.state_file().read_text(encoding="utf-8"))["devices"]) == ["owen"]
    access.add_user("benny", "battery staple")
    again.event("login", user="benny", ip="203.0.113.9", agent=WINDOWS_FIREFOX)
    again.event("login", user="owen", ip="203.0.113.9", agent=WINDOWS_FIREFOX)
    assert [title for title, _, _ in sent2 if title.startswith("New device")] == ["New device: benny"]


def test_a_state_file_from_before_users_were_followed_keeps_its_devices():
    """bell.json as servers kept it before: each list by name alone. Read as it always was, each user's list theirs, and
    from the first look on followed through a rename; a list of a name no user has now is no one's."""
    access.add_user("ann", "correct horse")
    bell.state_file().parent.mkdir(parents=True, exist_ok=True)
    bell.state_file().write_text(json.dumps({"devices": {"ann": ["Mac or iPad · Safari"], "gone": ["Yomu (app)"]}}), encoding="utf-8")
    w, sent, _ = watch()
    w.event("login", user="ann", ip="203.0.113.9", agent=IPAD)
    assert sent == []
    access.rename_user("ann", "anna")
    w.event("login", user="anna", ip="203.0.113.9", agent=IPAD)
    assert sent == []
    assert json.loads(bell.state_file().read_text(encoding="utf-8"))["devices"] == {"anna": ["Mac or iPad · Safari"]}


def test_downloads_counted_per_login_and_file():
    w, sent, clock = watch(downloads_per_hour=3)
    for path in ("/comics/a.cbz", "/comics/a.cbz", "/catalog/covers/x.webp", "/opds/new", "/comics/b.cbz"):
        w.request("203.0.113.9", "alice", "GET", path, 206 if path.endswith("a.cbz") else 200)
    assert sent == []  # a range, the same file again, covers and feeds don't count
    w.request("203.0.113.9", "alice", "GET", "/books/c.epub", 200)
    assert len(sent) == 1 and "alice got 3 files within an hour, the latest 'c.epub'." in sent[0][1]
    w.request("192.168.1.20", None, "GET", "/books/d.epub", 200)  # someone else: counted on their own
    assert len(sent) == 1
    clock[0] += 3601
    w.request("203.0.113.9", "alice", "GET", "/books/e.epub", 200)
    assert len(sent) == 1  # an hour later the count starts over
    off, sent2, _ = watch(downloads_per_hour=0)
    for n in range(20):
        off.request("203.0.113.9", "alice", "GET", f"/books/{n}.epub", 200)
    assert sent2 == []


def test_the_reader_and_the_icons_are_not_downloads():
    w, sent, _ = watch(downloads_per_hour=3)
    for path in ("/foliate/78914aef/view.js", "/foliate/overlayer.js", "/foliate/epubcfi.js", "/fonts/faces.css",
                 "/fonts/literata-normal-200-900-latin.woff2", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
                 "/icon-512.png", "/favicon.ico", "/manifest.webmanifest"):
        w.request("203.0.113.9", "alice", "GET", path, 200)
    assert sent == []  # opening a book in the page fetches all of these
    assert not any(map(bell.is_file, ("/foliate/view.js", "/fonts/faces.css", "/icon-192.png")))
    assert bell.is_file("/collections/Fonts/fonts.pdf") and bell.is_file("/humble/Icons/icon-set.zip")


def test_changed_users():
    w, sent, _ = watch()
    access.add_user("alice", "correct horse", role="reader")
    w.check_users()  # the first look only notes them
    assert sent == []
    access.add_user("ben", "correct horse", role="librarian")
    access.set_role("alice", "owner")
    w.check_users()
    assert len(sent) == 1 and "ben added (librarian)" in sent[0][1] and "alice: reader → owner" in sent[0][1]
    access.add_user("alice", "a new password")
    access.remove_user("ben")
    w.check_users()
    assert "alice: a new password" in sent[1][1] and "ben removed" in sent[1][1]
    w.check_users()
    assert len(sent) == 2  # nothing new


# ---- the library's disk ------------------------------------------------------------------------------

@pytest.fixture
def library(tmp_path):
    root = tmp_path / "lib"
    marker = bell.paths.marker_file(root)
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    return root


def away(root):
    bell.paths.marker_file(root).rename(root / "away")


def home(root):
    (root / "away").rename(bell.paths.marker_file(root))


def watching(root, tmp_path, conf=None, clock=None):
    told, clock = [], clock or [1_800_000_000.0]
    w = bell.Watch({**bell.DEFAULTS, "toast": False, **(conf or {})}, send=lambda title, text, prio, public=None: told.append((title, text, prio, public)),
                   now=lambda: clock[0], log=lambda m: None, state=tmp_path / "bell-state.json", library=root)
    return w, told, clock


def test_a_library_that_goes_is_said_after_some_minutes_once_and_when_its_back(library, tmp_path):
    w, told, clock = watching(library, tmp_path)
    w.check_library()
    assert told == []
    away(library)
    w.check_library()
    clock[0] += 4 * 60
    w.check_library()
    assert told == []  # a hiccup passes without a word
    clock[0] += 60
    w.check_library()
    assert [t[0] for t in told] == ["The library can't be reached"] and str(library) in told[0][1] and told[0][2] == "high"
    assert told[0][1].endswith("Is the disk plugged in and mounted, or the share up?")  # nothing of one household's USB cable
    for _ in range(3):
        clock[0] += 60
        w.check_library()
    assert len(told) == 1
    home(library)
    clock[0] += 60
    w.check_library()
    assert [t[0] for t in told][1:] == ["The library is back"] and told[1][2] == "default"
    clock[0] += 60
    w.check_library()
    assert len(told) == 2


def test_a_disk_that_comes_and_goes_still_gets_its_alarm(library, tmp_path):
    w, told, clock = watching(library, tmp_path)
    w.check_library()
    away(library)
    w.check_library()
    clock[0] += 5 * 60
    w.check_library()
    home(library)
    clock[0] += 60
    w.check_library()
    assert [t[0] for t in told] == ["The library can't be reached", "The library is back"]
    away(library)  # gone for good this time, within quiet_minutes of the first
    clock[0] += 60
    w.check_library()
    clock[0] += 10 * 60
    w.check_library()
    assert len(told) == 2  # kept quiet for now, but not forgotten
    clock[0] += 60 * 60
    w.check_library()
    assert [t[0] for t in told][2:] == ["The library can't be reached"]
    clock[0] += 60
    w.check_library()
    assert len(told) == 3


def test_a_hiccup_says_nothing_either_way(library, tmp_path):
    w, told, clock = watching(library, tmp_path)
    w.check_library()
    away(library)
    w.check_library()
    clock[0] += 60
    home(library)
    w.check_library()
    away(library)  # and it starts counting anew
    clock[0] += 60
    w.check_library()
    clock[0] += 4 * 60
    w.check_library()
    assert told == []


def test_the_phone_isnt_told_where_the_library_is(library, tmp_path):
    w, told, clock = watching(library, tmp_path)
    w.check_library()
    away(library)
    w.check_library()
    clock[0] += 5 * 60
    w.check_library()
    title, text = told[0][3]
    assert title == "The library can't be reached" and "5 minutes" in text and str(library) not in text and "lib" not in text.replace("library", "")


def test_a_restart_with_the_disk_gone_says_so_and_remembers(library, tmp_path):
    w, told, clock = watching(library, tmp_path)
    w.check_library()
    away(library)
    again, told2, _ = watching(library, tmp_path, clock=clock)  # the machine restarted, and the disk didn't come back
    again.check_library()
    assert told2 == []
    clock[0] += 5 * 60
    again.check_library()
    assert len(told2) == 1
    third, told3, _ = watching(library, tmp_path, clock=clock)  # another restart: already said
    third.check_library()
    assert told3 == []
    home(library)
    third.check_library()
    assert [t[0] for t in told3] == ["The library is back"]


def test_still_gone_a_day_later_it_says_so_again(library, tmp_path):
    w, told, clock = watching(library, tmp_path)
    w.check_library()
    away(library)
    w.check_library()
    clock[0] += 5 * 60
    w.check_library()
    clock[0] += 23 * 3600
    w.check_library()
    assert len(told) == 1
    clock[0] += 3600
    w.check_library()
    assert len(told) == 2


def test_a_folder_never_seen_as_the_library_is_no_alarm(tmp_path):
    root = tmp_path / "not yet a library"
    root.mkdir()
    w, told, clock = watching(root, tmp_path)
    for _ in range(3):
        w.check_library()
        clock[0] += 3600
    assert told == []


def test_moving_to_another_library_is_no_alarm(library, tmp_path):
    w, told, clock = watching(library, tmp_path)
    w.check_library()
    other = tmp_path / "new one"
    other.mkdir()
    moved, told2, _ = watching(other, tmp_path, clock=clock)
    moved.check_library()
    clock[0] += 3600
    moved.check_library()
    assert told2 == []


def test_a_dead_disks_errors_and_a_hanging_one_count_as_gone(library, tmp_path, monkeypatch):
    w, told, clock = watching(library, tmp_path)
    w.check_library()

    class Dead:
        def stat(self):
            raise OSError(107, "Transport endpoint is not connected")  # what a FUSE disk says, pulled out while mounted

    monkeypatch.setattr(bell.paths, "marker_file", lambda root: Dead())
    w.check_library()
    clock[0] += 5 * 60
    w.check_library()
    assert len(told) == 1

    hold, looks = threading.Event(), []

    class Hanging:
        def stat(self):
            looks.append(1)
            hold.wait(5)

    apart = tmp_path / "another state"
    apart.mkdir()
    w2, told2, clock2 = watching(library, apart)
    monkeypatch.setattr(bell, "LIBRARY_LOOK", 0.05)
    monkeypatch.setattr(bell.paths, "marker_file", lambda root: library / "state" / "library.json")
    w2.check_library()
    monkeypatch.setattr(bell.paths, "marker_file", lambda root: Hanging())
    w2.check_library()
    clock2[0] += 5 * 60
    w2.check_library()
    hold.set()
    assert len(told2) == 1  # and the check came back, for the other alarms
    assert len(looks) == 1  # with no pile of stuck looks behind it, one a minute


def test_the_alarm_can_be_turned_off_and_has_room_kept_for_it(library, tmp_path):
    w, told, clock = watching(library, tmp_path, {"library_missing": 0})
    w.check_library()
    away(library)
    clock[0] += 3600
    w.check_library()
    assert told == []
    home(library)
    apart = tmp_path / "another state"
    apart.mkdir()
    busy, told2, clock = watching(library, apart, {"alarms_per_hour": 1})
    busy.check_library()
    busy.alarm("users", "a", "Something else", "It used up the hour.")
    away(library)
    busy.check_library()
    clock[0] += 5 * 60
    busy.check_library()
    assert [t[0] for t in told2] == ["Something else", "The library can't be reached"]


def test_the_server_keeps_an_eye_on_its_library(tmp_path):
    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    try:
        assert srv.RequestHandlerClass.watch.library == tmp_path
    finally:
        srv.server_close()


def test_a_damaged_record_of_the_library_is_forgotten(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"library": {"root": 7, "gone": "x"}}', encoding="utf-8")
    assert "library" not in bell.read_state(path)
    path.write_text('{"library": {"root": "/a", "gone": "x", "told": 5}}', encoding="utf-8")
    assert bell.read_state(path)["library"] == {"root": "/a", "told": 5}


def test_the_daily_summary():
    def line(ip, user, when, target, status, agent="-"):
        return f'{ip} - {user} [{when}] "GET {target} HTTP/1.1" {status} 12 "-" "{agent}"'

    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    lines = [line("203.0.113.9", "-", "02/Nov/2026:03:00:00 +0000", "/.env", 401),
             line("203.0.113.10", "-", "02/Nov/2026:03:00:01 +0000", "/.env", 401),
             line("203.0.113.9", "-", "02/Nov/2026:03:00:02 +0000", "/wp-login.php", 401),
             line("192.168.1.20", "-", "02/Nov/2026:03:00:03 +0000", "/nothing", 404),  # at home too: every request logs in
             line("203.0.113.9", "alice", "02/Nov/2026:07:00:00 +0000", "/catalog/index.html", 200, IPAD),
             line("203.0.113.9", "alice", "02/Nov/2026:07:01:00 +0000", "/comics/a.cbz", 200, IPAD),
             line("203.0.113.9", "alice", "02/Nov/2026:07:02:00 +0000", "/comics/a.cbz", 206, IPAD),
             line("203.0.113.9", "-", "30/Oct/2026:07:00:00 +0000", "/old", 401)]  # older than 24 hours
    text = bell.summary(lines, now)
    assert "Refused: 4 requests without a login, from 3 addresses, most often '/.env' (2), '/wp-login.php' (1), '/nothing' (1)." in text
    assert "Refused: 4 requests without a login, from 3 addresses." in bell.summary(lines, now, counts_only=True)
    assert "outside" not in text
    assert "Logins: alice (Mac or iPad · Safari)." in text and "Downloads: alice 1." in text and "/old" not in text
    assert bell.summary([], now) == "Quiet: nothing refused, no logins, no downloads."


def test_the_daily_summary_quotes_the_paths_strangers_asked_for_cleaned_and_cut():
    """A refused path is anyone's to choose: the summary, which bell.log, hylly bell and the phone repeat, quotes each
    as an alarm quotes a name typed at the login (typed): in quotes it can't close, nothing in it that could move the
    cursor or turn the text round, at most 40 characters. So does the downloads alarm a file's name (the next test)."""
    def line(target, when="02/Nov/2026:03:00:00 +0000"):
        return f'203.0.113.9 - - [{when}] "GET {target} HTTP/1.1" 404 12 "-" "-"'

    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    text = bell.summary([line("/x\u009b31m\u202eLantern"), line("/" + "w" * 60)], now)
    assert "most often '/x?31mLantern' (1), '/" + "w" * 39 + "' (1)." in text
    assert "\u009b" not in text and "\u202e" not in text
    w, sent, _ = watch(downloads_per_hour=1)
    w.request("203.0.113.9", "alice", "GET", "/books/Velvet\u202eJinx\u009b.epub", 200)
    assert "the latest 'VelvetJinx?.epub'." in sent[0][1] and "\u202e" not in sent[0][1]


def test_the_downloads_alarm_cuts_a_long_file_name_in_its_middle_so_its_kind_stays():
    """The downloads alarm names the latest file as typed() quotes a name, at most 40 characters: a longer name loses
    its middle, with …, not its end, which says what kind of file it is."""
    w, sent, _ = watch(downloads_per_hour=1)
    w.request("203.0.113.9", "alice", "GET", "/books/Velvet Jinx and the Lantern Wolf of Moonwake Harbour, part 2.epub", 200)
    assert "the latest 'Velvet Jinx and the Lantern… part 2.epub'." in sent[0][1]
    assert len("Velvet Jinx and the Lantern… part 2.epub") == 40
    assert bell.file_named("Velvet Jinx.epub") == "'Velvet Jinx.epub'" and bell.file_named("x" * 40) == repr("x" * 40)


def test_one_unreadable_request_line_never_stops_the_daily_summary():
    # a scanner's target that urlsplit refuses ("Invalid IPv6 URL") stopped the whole summary at 08:00
    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    lines = ['203.0.113.9 - - [02/Nov/2026:03:00:00 +0000] "GET http://[bad/x HTTP/1.1" 400 12 "-" "-"',
             '203.0.113.9 - - [02/Nov/2026:03:00:01 +0000] "GET //[::1/.env?x=1 HTTP/1.1" 401 12 "-" "-"',
             '203.0.113.10 - - [02/Nov/2026:03:00:02 +0000] "GET /.env HTTP/1.1" 401 12 "-" "-"']
    text = bell.summary(lines, now)
    assert "Refused: 2 requests without a login, from 2 addresses" in text
    assert bell.request_path("http://[bad/x") == "http://[bad/x" and bell.request_path("/a/b?c=1") == "/a/b"


def test_the_daily_summary_comes_once_a_day():
    w, sent, clock = watch(daily_summary=8)
    hour = datetime.fromtimestamp(clock[0]).astimezone().hour
    w.conf["daily_summary"] = hour  # due now
    w.maybe_summary()
    w.maybe_summary()
    assert len(sent) == 1 and sent[0][0] == "Library: the last 24 hours" and sent[0][2] == "low"
    off, sent2, _ = watch(daily_summary=-1)
    off.maybe_summary()
    assert sent2 == []


def test_a_nightly_copy_late_by_a_day_and_a_half_is_said_once(library, tmp_path):
    """A copy that hangs or fails every night, or nothing running the server's jobs: said once for the copy it's late
    after, and only where this computer's database keeps the library's records."""
    import time as clock_of

    from hylly import db

    w, sent, clock = watch()
    w.library = library
    clock[0] = clock_of.time() + 3600
    w.check_copies()
    assert sent == []  # no database keeps the records here: not this computer's to say
    db.own(db.database(), library)
    assert db.nightly(library, datetime(2026, 11, 10, 4))  # made now, whatever day it's named
    w._copies_looked = -bell.COPY_LOOK
    w.check_copies()
    assert sent == []  # an hour old
    clock[0] += bell.COPY_LATE
    w.check_copies()
    assert [s[0] for s in sent] == ["The nightly copy is late"] and "since 2026-11-10" in sent[0][1]
    clock[0] += 2 * bell.COPY_LOOK
    w.check_copies()
    assert len(sent) == 1  # said once for that copy
    clock[0] += bell.COPY_LOOK / 2
    w.check_copies()
    assert len(sent) == 1 and bell.read_state(bell.state_file())["copies"] == "2026-11-10"


def test_the_day_s_summary_goes_out_once_whichever_of_the_two_sends_it():
    """The server keeps the day it sent it in bell.json, hylly server jobs (jobs = "external") in bell-jobs.json: each
    looks at the other's too, so a switch between them on the day doesn't send it twice, either way."""
    server, sent, clock = watch()
    server.conf["daily_summary"] = datetime.fromtimestamp(clock[0]).astimezone().hour  # due now
    timer = bell.Watch(dict(server.conf), send=lambda title, text, prio, public=None: sent.append((title, text, prio)),
                       now=lambda: clock[0], log=lambda m: None, state=bell.job_state_file())
    server.maybe_summary()
    timer.maybe_summary()  # switched to the timer the same day
    assert len(sent) == 1
    clock[0] += 86400  # the next day: the timer sends it, then the server it was switched back to doesn't
    timer.maybe_summary()
    server.maybe_summary()
    assert len(sent) == 2


def test_ntfy_messages():
    url, body = bell.ntfy_message("https://ntfy.sh/my-topic_42", "Wrong passwords for Zoë", "text", "high")
    assert url == "https://ntfy.sh" and body["topic"] == "my-topic_42" and body["title"] == "Wrong passwords for Zoë"
    assert body["priority"] == 4 and bell.ntfy_message("https://example.org/ntfy/t", "t", "x", "low")[0] == "https://example.org/ntfy"
    for bad in ("ntfy.sh/topic", "https://ntfy.sh/", "https://ntfy.sh/two words", "ftp://ntfy.sh/t"):
        with pytest.raises(ValueError):
            bell.ntfy_message(bad, "t", "x", "high")
    assert bell.channels({**bell.DEFAULTS, "ntfy": "https://ntfy.sh/secret-topic"}) == [
        "bell.log", "notifications on this computer", "ntfy (ntfy.sh)"]  # never the topic


@pytest.mark.parametrize("event, one, many", [
    ("review", "1 review waits for you", "3 reviews wait for you"),
    ("report", "1 reported title waits for you", "3 reported titles wait for you")])
def test_one_waiting_review_or_report_reads_as_one(tmp_path, event, one, many):
    """One waiting reads "1 review waits", as the same line says "it" for one, in bell.log, on this computer and on
    the phone."""
    for waiting, said, it in ((1, one, "look at it"), (3, many, "look at them")):
        told = []
        w = bell.Watch({**bell.DEFAULTS, "toast": False}, send=lambda title, text, prio, public=None: told.append((text, public)),
                       log=lambda m: None, state=tmp_path / f"{event}-{waiting}.json")
        w.event(event, waiting=waiting)
        text, public = told[0]
        assert text.startswith(f"{said} to {it}:") and public[1].startswith(f"{said} in the library page:")
        assert f"{said} to {it}" in bell.log_file().read_text(encoding="utf-8")
        item = "Reviews to check" if event == "review" else "Reported titles"  # in Manage, where the page has it
        assert f"Manage, {item}." in text and f"Manage, {item}." in public[1] and "account menu" not in text + public[1]


def test_the_alarm_table_in_the_help_has_a_row_for_every_alarm():
    """Every kind of alarm the code raises has its row in the help's table, under the name the table gives it, with
    a link where it has a section of its own; and no row of an alarm or a file the code no longer has."""
    import html
    import re
    from pathlib import Path

    rows = {"wrong": "Wrong passwords", "locked": "Wrong passwords", "device": "New device", "downloads": "Downloads",
            "users": "Users", "people": "What's kept for a user", "library": "The library can't be reached",
            "copies": "The nightly copy is late", "review": "A review waits for you", "report": "A title was reported",
            "job": None}  # long jobs' notes: the list under the table
    source = Path(bell.__file__).read_text(encoding="utf-8")
    assert set(re.findall(r'self\.alarm\(\s*"(\w+)"', source)) | {"job"} == set(rows)
    page = (Path(bell.__file__).parent / "help.html").read_text(encoding="utf-8")
    table = re.search(r'<section id="bell">.*?<table>(.*?)</table>', page, re.S)[1]
    named = {html.unescape(name): cell for name, cell in re.findall(r"<tr><td><b>(.*?)</b></td><td>(.*?)</td></tr>", table)}
    assert set(named) == {name for name in rows.values() if name}
    assert 'href="#stars"' in named["A review waits for you"] and 'href="#reports"' in named["A title was reported"]
    assert "sessions.damaged" not in page and "Logins forgotten</b></td>" not in page


@pytest.mark.parametrize("written, why", [
    ('"mail -s Alarm ann@example.org"', "the program and its arguments as a list of words"),
    ('["mail -s Alarm ann@example.org"]', "each word in quotes of its own"),
    ('["", "-s", "Alarm"]', "the first word is the program"),
    ("[1, 2]", "the program and its arguments as a list of words"),
    ('["bin/mail"]', "the program by its full path"),
    ('["mail", "-s", "Library\\u0000alarm"]', "a word with a NUL character in it")])
def test_a_wrong_command_is_named_by_the_settings_check(written, why):
    """[bell] command is the program and its arguments, each a word of its own, as no shell reads them."""
    from hylly import paths, settings

    paths.settings_file().write_text(f"[bell]\ncommand = {written}\n", encoding="utf-8")
    settings.reload()
    with pytest.raises(SystemExit, match=f"\\[bell\\] command = .*: {why}"):
        bell.settings()


def test_a_command_is_its_words_and_on_windows_a_full_path(monkeypatch):
    from hylly import paths, settings

    monkeypatch.setattr(paths, "WINDOWS", False)  # elsewhere: a name alone is looked for on the PATH
    for written, read in (("[]", []), ('["mail", "-s", "Library alarm", "ann@example.org"]', ["mail", "-s", "Library alarm", "ann@example.org"]),
                          ('[" /usr/bin/mail", "-s", " two  spaces "]', ["/usr/bin/mail", "-s", " two  spaces "])):
        paths.settings_file().write_text(f"[bell]\ncommand = {written}\n", encoding="utf-8")
        settings.reload()
        assert bell.settings()["command"] == read
    monkeypatch.setattr(paths, "WINDOWS", True)  # there, by its name alone, the folder the server started in comes first
    paths.settings_file().write_text('[bell]\ncommand = ["curl", "-d", "@-"]\n', encoding="utf-8")
    settings.reload()
    with pytest.raises(SystemExit, match="by its full path, such as C:"):
        bell.settings()
    paths.settings_file().write_text('[bell]\ncommand = [\'C:\\Windows\\System32\\curl.exe\', "-d", "@-"]\n', encoding="utf-8")
    settings.reload()
    assert bell.settings()["command"] == ["C:\\Windows\\System32\\curl.exe", "-d", "@-"]


def test_settings_are_checked(monkeypatch):
    from hylly import paths

    monkeypatch.setattr(paths, "section", lambda name: {"downloads_per_hour": "ten"} if name == "bell" else {})
    with pytest.raises(SystemExit):
        bell.settings()
    monkeypatch.setattr(paths, "section", lambda name: {"downloads_per_hour": 4, "unknown": 1} if name == "bell" else {})
    assert bell.settings()["downloads_per_hour"] == 4


def test_the_server_reports_to_the_alarms(tmp_path):
    """Through a real server: a real user's wrong passwords and a new device. Nothing is answered without a login, so
    there is no door to be left open, and no alarm for one."""
    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    access.add_user("alice", "correct horse")
    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    sent = []
    srv.RequestHandlerClass.watch._send = lambda title, text, prio, public=None: sent.append(title)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    outside = {"X-Forwarded-For": "203.0.113.9"}
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}") as client:
            for _ in range(3):
                client.get("/opds", auth=("alice", "wrong"), headers=outside)
            client.get("/opds", auth=("alice", "correct horse"), headers={**outside, "User-Agent": "Yomu/100 CFNetwork/1500"})
            for headers in (outside, {}):  # through a tunnel, or here: refused, and nothing to raise an alarm of
                assert client.get("/opds", headers=headers).status_code == 401
        assert sent == ["Wrong passwords for alice", "New device: alice"]
        assert "Wrong passwords for alice" in bell.log_file().read_text(encoding="utf-8")
        assert "open" not in bell.URGENT and "open_door" not in bell.settings()
    finally:
        srv.shutdown()
