"""What leaves for the phone, and what a long job says when it stops."""

from __future__ import annotations

import contextlib
import json
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from hylly import bell, cli, exits, lookups, notify, runs, settings
from hylly.exits import Exit
from hylly.logins import AuthError
from hylly.lookups import openlibrary

from helpers import Clock, WINDOWS_FIREFOX, note_threads_waited_for, session_for


NAME, ADDRESS = "lanternkeeper", "203.0.113.77"
TOPIC = "https://my.example/alarms"


@pytest.fixture
def phone(monkeypatch):
    """What reached the phone, and what this computer showed."""
    posted, shown = [], []
    monkeypatch.setattr(httpx, "post", lambda url, json=None, **k: posted.append(json) or httpx.Response(200))
    monkeypatch.setattr(notify, "toast", lambda title, text, link="": shown.append((title, text)) or True)
    return posted, shown


def watching(**conf):
    clock = [1_800_000_000.0]
    w = bell.Watch({**bell.DEFAULTS, "ntfy": TOPIC, **conf}, now=lambda: clock[0], log=lambda m: None)
    w._send = w.push  # at once, not in the background
    return w, clock


def every_alarm(w, clock, monkeypatch):
    from hylly import access

    for _ in range(3):
        w.event("wrong", name=NAME, ip=ADDRESS, known=True)
    w.event("locked", ip=ADDRESS, name=NAME)
    w.event("login", user=NAME, ip=ADDRESS, agent=WINDOWS_FIREFOX)
    for n in range(10):
        w.request(ADDRESS, NAME, "GET", f"/humble/{NAME}-{n}.epub", 200)
    monkeypatch.setattr(access, "load_accounts", lambda: {"someone": ("reader", "x")})
    w.check_users()
    monkeypatch.setattr(access, "load_accounts", lambda: {NAME: ("reader", "x")})
    w.check_users()


def test_the_phone_gets_counts_and_this_computer_the_details(phone, monkeypatch):
    posted, shown = phone
    w, clock = watching()
    every_alarm(w, clock, monkeypatch)
    assert len(posted) >= 5  # wrong passwords, a lockout, a new device, downloads, users changed
    for body in posted:
        said = json.dumps(body)
        assert NAME not in said and ADDRESS not in said and ".epub" not in said, said
    assert any(NAME in title + text or ADDRESS in title + text for title, text in shown)
    assert NAME in bell.log_file().read_text(encoding="utf-8")


def test_the_phone_gets_it_all_if_you_ask(phone, monkeypatch):
    posted, _ = phone
    w, clock = watching(remote_detail="full")
    every_alarm(w, clock, monkeypatch)
    assert any(NAME in json.dumps(body) for body in posted)


def test_an_alarm_with_no_words_of_its_own_for_the_phone_says_little(phone):
    posted, _ = phone
    w, _ = watching()
    w.alarm("new-kind", "", f"About {NAME}", f"{NAME} from {ADDRESS}")
    assert posted[0]["title"] == bell.SOMETHING[0] and NAME not in json.dumps(posted)


def test_the_daily_summary_for_the_phone_has_only_counts():
    line = f'{ADDRESS} - {NAME} [02/Nov/2026:07:00:00 +0000] "GET /humble/{NAME}.epub HTTP/1.1" 200 10 "-" "{WINDOWS_FIREFOX}"'
    bad = f'{ADDRESS} - - [02/Nov/2026:07:00:00 +0000] "GET /wp-login.php HTTP/1.1" 404 10 "-" "x"'
    now = datetime(2026, 11, 2, 8, tzinfo=timezone.utc)
    full, counts = bell.summary([line, bad], now), bell.summary([line, bad], now, counts_only=True)
    assert NAME in full
    assert NAME not in counts and "wp-login" not in counts and ".epub" not in counts and "Downloads: 1 file." in counts and "Logins: 1 user, 1 kind of device" in counts


def test_a_mistaken_setting_says_so(monkeypatch):
    monkeypatch.setattr(bell.paths, "section", lambda name: {"remote_detail": "everything"})
    with pytest.raises(SystemExit, match="counts"):
        bell.settings()


def secret_file(name: str, value: str) -> None:
    settings.secrets_dir().mkdir(parents=True, exist_ok=True)
    (settings.secrets_dir() / name).write_text(value + "\n", encoding="utf-8")


def test_the_topic_stays_off_the_screen_and_where_it_is_kept_is_said(monkeypatch, capsys):
    """Only the topic's end, and the file it's in, which its owner can open: --show-topic, which showed all of it at a
    terminal, is gone with the topic's place in settings.toml."""
    topic = "https://ntfy.sh/a-topic-" + "x" * 16 + "wxyz"
    secret_file("ntfy-url", topic)
    cli.main(["bell"])
    out = capsys.readouterr().out
    assert topic not in out and "https://ntfy.sh/...wxyz" in out and "counts and kinds" in out
    assert f"subscribe to the address in {settings.secrets_dir() / 'ntfy-url'}" in out
    with pytest.raises(SystemExit) as refused:
        cli.main(["bell", "--show-topic"])
    assert refused.value.code == exits.USAGE
    capsys.readouterr()
    data = json.loads((cli.main(["bell", "--json"]), capsys.readouterr().out)[1])
    assert data["ntfy"] == "https://ntfy.sh/...wxyz" and data["ntfy_from"] == str(settings.secrets_dir() / "ntfy-url")
    assert topic not in json.dumps(data) and "ntfy_token" not in data
    assert bell.masked("https://[oops/x") == "(an address that looks mistyped)"


def test_with_no_phone_set_hylly_bell_says_where_its_address_goes(capsys):
    cli.main(["bell"])
    out = capsys.readouterr().out
    assert ("Phone: none. For alarms on your phone, put an ntfy address, https://ntfy.sh/ and a long random topic, in "
            f"{settings.secrets_dir() / 'ntfy-url'}\n") in out


def test_a_stronger_topic_is_suggested_on_a_screen_only(monkeypatch, capsys):
    """One made now, printed into a pipe, a log or an assistant's transcript, would be known before its owner set
    it. Elsewhere hylly bell says only that the topic is short, and that it suggests one in a terminal."""
    monkeypatch.setattr(bell, "settings", lambda: {**bell.DEFAULTS, "ntfy": "https://ntfy.sh/alarms"})
    cli.main(["bell"])
    err = capsys.readouterr().err
    assert "short enough to guess" in err and "Run hylly bell in a terminal, and it suggests a stronger one." in err
    assert "ntfy.sh" not in err  # neither a new topic nor the old one
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    cli.main(["bell"])  # standard error still in a file, as a log is
    assert "ntfy.sh/" not in capsys.readouterr().err
    monkeypatch.setattr(cli.sys.stderr, "isatty", lambda: True, raising=False)
    cli.main(["bell"])
    err = capsys.readouterr().err
    assert re.search(r"subscribe to on your phone: https://ntfy\.sh/[A-Za-z0-9_-]{24}\n", err) and "in a terminal" not in err
    cli.main(["bell", "--json"])
    assert "ntfy.sh/" not in capsys.readouterr().err  # nor to a script, at a terminal or not


def test_the_alarms_secrets_come_from_the_environment_a_named_file_systemd_or_their_own_files(monkeypatch, tmp_path):
    """The first that has the address counts, each without its line end, Notepad's start or PowerShell 5's UTF-16;
    the token only from where the address is, never one kept for another server. A file named but missing, or one that
    can't be read, stops as a mistake in the settings does, saying which and never what's in it."""
    import conftest  # which takes the owner's own away from every test

    assert {f"{v}{end}" for _, v in settings.SECRETS.values() for end in ("", "_FILE")} <= set(conftest._secret_variables())
    assert bell.settings()["ntfy"] == "" and settings.secret_from("ntfy-url") == ("", "")
    secret_file("ntfy-token", "a-made-up-token-in-its-file")
    assert settings.secret_from("ntfy-token") == ("a-made-up-token-in-its-file", str(settings.secrets_dir() / "ntfy-token"))  # shown
    (tmp_path / "ntfy-url").write_bytes(b"\xef\xbb\xbfhttps://ntfy.sh/made-up-from-a-file\r\n")
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "ntfy-url").write_text("https://ntfy.sh/made-up-from-systemd\n", encoding="utf-8")
    (settings.secrets_dir() / "ntfy-url").write_bytes("https://ntfy.sh/made-up-in-secrets\r\n".encode("utf-16"))  # echo > in PowerShell 5
    assert settings.secret_from("ntfy-url") == ("https://ntfy.sh/made-up-in-secrets", str(settings.secrets_dir() / "ntfy-url"))
    assert bell.settings()["ntfy_token"] == "a-made-up-token-in-its-file"  # both in secrets/
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    assert settings.secret_from("ntfy-url")[0] == "https://ntfy.sh/made-up-from-systemd"
    assert settings.secret("ntfy-token") == ""  # the address from systemd, which gives no token: not the folder's
    (credentials / "ntfy-token").write_text("a-made-up-token-from-systemd\n", encoding="utf-8")
    assert settings.secret("ntfy-token") == "a-made-up-token-from-systemd"
    monkeypatch.setenv("HYLLY_NTFY_URL_FILE", str(tmp_path / "ntfy-url"))
    assert settings.secret_from("ntfy-url") == ("https://ntfy.sh/made-up-from-a-file", f"{tmp_path / 'ntfy-url'} (HYLLY_NTFY_URL_FILE)")
    assert settings.secret("ntfy-token") == ""
    monkeypatch.setenv("HYLLY_NTFY_URL", " https://ntfy.sh/made-up-from-the-environment ")
    assert settings.secret_from("ntfy-url") == ("https://ntfy.sh/made-up-from-the-environment", "HYLLY_NTFY_URL in the environment")
    conf = bell.settings()
    assert conf["ntfy"] == "https://ntfy.sh/made-up-from-the-environment" and conf["ntfy_token"] == ""
    monkeypatch.setenv("HYLLY_NTFY_TOKEN", "a-made-up-token-from-the-environment")
    assert bell.settings()["ntfy_token"] == "a-made-up-token-from-the-environment"
    monkeypatch.delenv("HYLLY_NTFY_URL")
    monkeypatch.delenv("HYLLY_NTFY_TOKEN")
    monkeypatch.setenv("HYLLY_NTFY_URL_FILE", str(tmp_path / "gone"))
    with pytest.raises(SystemExit, match="HYLLY_NTFY_URL_FILE names .*gone, which isn't there") as stopped:
        bell.settings()
    assert stopped.value.code == exits.NOT_SET_UP
    monkeypatch.setenv("HYLLY_NTFY_URL_FILE", str(tmp_path))  # a folder, which can't be read as one
    with pytest.raises(SystemExit, match="which holds the alarms' ntfy, can't be read") as stopped:
        bell.settings()
    assert "made-up" not in str(stopped.value)


@pytest.mark.parametrize("damage", ["[]", "null", "5", '{"last": [], "sent": 5, "held": "x", "devices": [], "users": [1]}',
                                    '{"last": {"a|": "noon"}, "sent": ["noon"], "devices": {"alice": 3}, "users": {"alice": "x"}}',
                                    '{"whose": [], "devices": {"alice": ["Windows · Firefox"]}}',
                                    '{"whose": {"alice": [1, "x"], "bob": "y", "eve": ["a", "b", "c"]}, "devices": {"alice": []}}'])
def test_a_damaged_state_file_loses_no_alarm(phone, damage, monkeypatch):
    from hylly import access

    posted, _ = phone
    bell.state_file().parent.mkdir(parents=True, exist_ok=True)
    bell.state_file().write_text(damage, encoding="utf-8")
    w, clock = watching()
    assert w.alarm("wrong", "alice", "Wrong passwords for alice", "text", public=("Wrong passwords", "text"))
    w.event("login", user="alice", ip=ADDRESS, agent=WINDOWS_FIREFOX)
    monkeypatch.setattr(access, "load_accounts", lambda: {"alice": ("reader", "x")})
    w.check_users()  # the first look, as the old ones are forgotten
    w.tell_of_held()
    w.maybe_summary()
    assert len(posted) >= 2 and json.loads(bell.state_file().read_text(encoding="utf-8"))["last"]


def test_a_job_note_with_a_damaged_state_file_still_goes(phone, monkeypatch):
    posted, _ = phone
    monkeypatch.setattr(bell, "settings", lambda: {**bell.DEFAULTS, "ntfy": TOPIC})
    bell.job_state_file().parent.mkdir(parents=True, exist_ok=True)
    bell.job_state_file().write_text('{"last": 3}', encoding="utf-8")
    bell.job_note("A lookup stopped", "text")
    assert len(posted) == 1


def test_a_job_note_waits_while_hylly_server_jobs_keeps_bell_jobs_json(phone, monkeypatch):
    """Long jobs' notes and hylly server jobs' alarms keep bell-jobs.json, each in memory while it runs: one reads,
    changes and writes it while the other waits, so neither's count of what went out (alarms_per_hour, quiet_minutes)
    is written over by the other's. What goes to the phone goes after, so a slow phone service holds up neither."""
    posted, _ = phone
    monkeypatch.setattr(bell, "settings", lambda: {**bell.DEFAULTS, "ntfy": TOPIC})
    noted = []
    with bell.one_writer():  # hylly server jobs' alarms, at their look
        note = threading.Thread(target=lambda: noted.append(bell.job_note("A lookup stopped", "text", wait=10)))
        note.start()
        note.join(0.5)
        assert note.is_alive() and not bell.job_state_file().exists() and posted == []  # it waits its turn
    note.join(10)
    assert noted == [True] and len(posted) == 1
    bell.job_note("A catalog build stopped", "more text")
    assert len(json.loads(bell.job_state_file().read_text(encoding="utf-8"))["sent"]) == 2  # both counted


def test_a_short_topic_or_a_password_doesn_t_show():
    assert bell.masked("https://ntfy.sh/abcd") == "https://ntfy.sh/..."
    at = "@"  # no address that looks like an e-mail's, for the check before publishing
    shown = bell.masked(f"https://me:secret{at}my.example:8443/a-long-topic-wxyz")
    assert shown == "https://my.example:8443/...wxyz"
    assert "secret" not in " ".join(bell.channels({**bell.DEFAULTS, "ntfy": f"https://me:secret{at}my.example/x"}))


def test_a_second_test_alarm_is_in_the_log_too(phone, monkeypatch):
    posted, _ = phone
    monkeypatch.setattr(bell, "settings", lambda: {**bell.DEFAULTS, "ntfy": TOPIC})
    cli.main(["bell", "test"])
    cli.main(["bell", "test"])
    assert len(posted) == 2 and bell.log_file().read_text(encoding="utf-8").count("A test alarm") == 2


def test_a_restart_doesn_t_say_the_same_alarm_again(phone):
    posted, _ = phone
    w, clock = watching()
    assert w.alarm("wrong", "alice", "Wrong passwords for alice", "text")
    again = bell.Watch(w.conf, now=lambda: clock[0], log=lambda m: None)
    again._send = again.push
    assert not again.alarm("wrong", "alice", "Wrong passwords for alice", "text") and len(posted) == 1
    clock[0] += 3601
    assert again.alarm("wrong", "alice", "Wrong passwords for alice", "text")
    assert list(again._state["last"]) == ["wrong|alice"]  # old ones don't pile up


# ---- long jobs -----------------------------------------------------------------------------------------

@pytest.fixture
def notes(monkeypatch):
    said = []
    monkeypatch.setattr(bell, "job_note", lambda title, text, public="": said.append((title, text, public)))
    return said


def job_that(monkeypatch, does):
    monkeypatch.setattr(cli, "cmd_storybundle", lambda args: does())


def test_an_expired_login_tells_you(tmp_path, monkeypatch, notes):
    def expired():
        raise AuthError(f"The cookie for {NAME} has expired.")

    job_that(monkeypatch, expired)
    with pytest.raises(SystemExit) as e:
        cli.main(["storybundle", "download", "--library", str(tmp_path)])
    assert e.value.code == exits.LOGIN and notes[0][0] == "A StoryBundle download stopped" and NAME in notes[0][1]
    assert NAME not in notes[0][2] and "Log in again" in notes[0][2]


@pytest.mark.parametrize("why", [Exit(exits.INTERRUPTED, "Stopped. What was done is kept."), SystemExit(None), SystemExit(0)])
def test_no_note_for_a_stop_you_asked_for(tmp_path, monkeypatch, notes, why):
    def stops():
        raise why

    job_that(monkeypatch, stops)
    with pytest.raises(SystemExit):
        cli.main(["storybundle", "download", "--library", str(tmp_path)])
    assert not notes


def test_a_job_that_gave_up_says_why(tmp_path, monkeypatch, notes):
    def gives_up():
        raise SystemExit("Not enough space: 3 GB needed.")

    job_that(monkeypatch, gives_up)
    with pytest.raises(SystemExit):
        cli.main(["storybundle", "download", "--library", str(tmp_path)])
    assert notes == [("A StoryBundle download stopped", "Not enough space: 3 GB needed.", "")]


def test_a_dry_run_says_nothing(tmp_path, monkeypatch, notes):
    def expired():
        raise AuthError("expired")

    job_that(monkeypatch, expired)
    with pytest.raises(SystemExit):
        cli.main(["storybundle", "download", "--dry-run", "--library", str(tmp_path)])
    assert not notes


def test_a_mistake_at_the_keyboard_doesn_t_reach_your_phone(tmp_path, monkeypatch, notes):
    def mistaken():
        raise SystemExit("No source called nosuch.")

    job_that(monkeypatch, mistaken)
    monkeypatch.setattr(cli, "at_keyboard", lambda: True)
    with pytest.raises(SystemExit):
        cli.main(["storybundle", "download", "--library", str(tmp_path)])
    assert not notes
    monkeypatch.setattr(cli, "ON_THE_SPOT", 0)  # after a long while you may have gone
    with pytest.raises(SystemExit):
        cli.main(["storybundle", "download", "--library", str(tmp_path)])
    assert notes


def test_a_library_that_isn_t_there_tells_a_scheduled_run(tmp_path, monkeypatch, notes):
    from hylly import paths

    monkeypatch.setattr(paths, "REQUIRE_MARKER", True)
    job_that(monkeypatch, lambda: pytest.fail("it shouldn't run"))
    with pytest.raises(SystemExit, match="No Hylly library"):
        cli.main(["storybundle", "download", "--library", str(tmp_path / "unmounted")])
    assert notes[0][0] == "A StoryBundle download stopped" and "No Hylly library" in notes[0][1]


def test_ctrl_c_during_a_note_leaves_the_job_s_end_alone(monkeypatch):
    def interrupted(self, timeout=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(bell.threading.Thread, "join", interrupted)
    with note_threads_waited_for():
        bell.job_note("A lookup stopped", "text")  # no exception: the job ends as it was going to


def test_a_slow_phone_service_doesn_t_hold_up_the_job(monkeypatch):
    import time

    let_go = threading.Event()
    monkeypatch.setattr(bell, "NOTE_WAIT", 0.05)
    monkeypatch.setattr(bell, "settings", lambda: {**bell.DEFAULTS, "ntfy": TOPIC, "toast": False})
    monkeypatch.setattr(httpx, "post", lambda *a, **k: let_go.wait(10) or httpx.Response(200))
    with note_threads_waited_for():
        began = time.monotonic()
        bell.job_note("A lookup stopped", "text")
        waited = time.monotonic() - began
        let_go.set()
    assert waited < 5


def test_only_a_long_job_says_it_has_finished(tmp_path, monkeypatch, notes):
    job_that(monkeypatch, lambda: None)
    cli.main(["storybundle", "download", "--library", str(tmp_path)])
    assert not notes
    monkeypatch.setattr(cli, "LONG_JOB", 0)
    cli.main(["storybundle", "download", "--library", str(tmp_path)])
    assert notes[0][:2] == ("A StoryBundle download finished", "It took under a minute.")  # seconds are no minute


def test_every_long_job_keeps_the_computer_awake_and_lets_go(tmp_path, monkeypatch, notes):
    calls = []
    monkeypatch.setattr(cli, "_keep_awake", calls.append)

    def fails():
        assert calls == [True]
        raise SystemExit("It failed.")

    job_that(monkeypatch, fails)
    with pytest.raises(SystemExit):
        cli.main(["storybundle", "download", "--library", str(tmp_path)])
    assert calls == [True, False]


def test_a_second_run_refused_says_nothing(tmp_path, monkeypatch, notes):
    job_that(monkeypatch, lambda: pytest.fail("it shouldn't run"))
    with runs.only_one("storybundle"):
        with pytest.raises(runs.Busy):
            cli.main(["storybundle", "download", "--library", str(tmp_path)])
    assert not notes


def test_hylly_download_keeps_the_reason_in_its_summary(tmp_path, monkeypatch, notes, capsys):
    from hylly.logins import SITES

    for shop in ("humble", "storybundle", "drivethrurpg", "itchio"):
        monkeypatch.setenv(SITES[shop][2], "a login")

    def expired(args):
        raise AuthError("The login has expired: log in again.")

    for name in ("cmd_humble", "cmd_drivethrurpg", "cmd_itchio"):
        monkeypatch.setattr(cli, name, lambda args: None)
    monkeypatch.setattr(cli, "cmd_storybundle", expired)
    with pytest.raises(SystemExit):
        cli.main(["download", "--library", str(tmp_path)])
    assert "StoryBundle: stopped: The login has expired: log in again." in capsys.readouterr().out


def test_a_job_s_note_reaches_the_phone_without_details_and_can_t_break_the_job(phone, monkeypatch):
    posted, shown = phone
    monkeypatch.setattr(bell, "settings", lambda: {**bell.DEFAULTS, "ntfy": TOPIC})
    bell.job_note("A lookup stopped", f"{NAME} said no", "Its source said no.")
    assert posted[0]["title"] == "A lookup stopped" and NAME not in json.dumps(posted) and NAME in shown[0][1]

    def broken():
        raise SystemExit("a mistake in the settings")

    monkeypatch.setattr(bell, "settings", broken)
    bell.job_note("A lookup stopped", "text")  # says nothing, and goes on


def test_lookups_notes_go_through_hyllybell(monkeypatch, tmp_path):
    from hylly.library import Catalog

    seen = {}
    monkeypatch.setattr(Catalog, "get", lambda self: [{"id": "a", "t": "A Title"}])
    monkeypatch.setattr(lookups, "available", lambda chosen=None: ([], []))
    monkeypatch.setattr(lookups, "run", lambda *a, **k: seen.update(k) or {})
    cli.main(["lookup", "--library", str(tmp_path)])
    assert seen["notify"] is bell.job_note


def test_a_lookup_that_can_t_write_says_so_once(tmp_path, monkeypatch, notes):
    from hylly.library import Catalog

    def cannot_write(*a, **k):
        raise lookups.LookupProblem("Can't write the lookup cache. Nothing is lost; run it again later.")

    monkeypatch.setattr(Catalog, "get", lambda self: [{"id": "a", "t": "A Title"}])
    monkeypatch.setattr(lookups, "available", lambda chosen=None: ([], []))
    monkeypatch.setattr(lookups, "run", cannot_write)
    with pytest.raises(SystemExit):
        cli.main(["lookup", "--library", str(tmp_path)])
    assert [n[0] for n in notes] == ["A lookup stopped"] and "Nothing is lost" in notes[0][1]


def test_a_running_server_doesn_t_forget_what_jobs_said(phone, monkeypatch):
    posted, _ = phone
    monkeypatch.setattr(bell, "settings", lambda: {**bell.DEFAULTS, "ntfy": TOPIC})
    server = bell.Watch(log=lambda m: None)  # holds its state in memory, and writes it every minute
    bell.job_note("A lookup stopped", "text")
    server._keep_state()
    bell.job_note("A lookup stopped", "text")
    assert len(posted) == 1 and posted[0]["priority"] == 3  # not as loud as an alarm


def test_a_test_alarm_says_what_it_is_on_the_phone_too(phone, monkeypatch):
    posted, _ = phone
    monkeypatch.setattr(bell, "settings", lambda: {**bell.DEFAULTS, "ntfy": TOPIC})
    cli.main(["bell", "test"])
    assert posted[0]["title"] == "A test alarm" and "alarms work" in posted[0]["message"]


def test_a_crash_says_only_its_kind(tmp_path, monkeypatch, notes):
    def crashes():
        raise RuntimeError("https://a-shop.example/?key=secret went wrong")

    job_that(monkeypatch, crashes)
    with pytest.raises(SystemExit) as e:
        cli.main(["storybundle", "download", "--library", str(tmp_path)])
    assert e.value.code == exits.BUG  # a bug, not partly done: its note says so, and of the crash only its kind
    assert notes[0][0] == "A StoryBundle download stopped by a bug" and notes[0][1].startswith("RuntimeError. This is a bug")
    assert "secret" not in str(notes) and "a-shop" not in str(notes)


def test_a_full_disk_tells_you(tmp_path, monkeypatch, notes):
    def full():
        raise OSError(28, "No space left on device")

    job_that(monkeypatch, full)
    with pytest.raises(SystemExit):
        cli.main(["storybundle", "download", "--library", str(tmp_path)])
    assert "No space left" in notes[0][1] and notes[0][2] == "What was done is kept. bell.log says more."


def test_a_download_says_how_it_ended(tmp_path, monkeypatch, notes):
    def run(failed, stopped=0):
        def does():
            return cli.Ended(text=f"120 files done, {failed} failed.", failed=failed, stopped=stopped)
        job_that(monkeypatch, does)
        cli.main(["storybundle", "download", "--library", str(tmp_path)])

    run(0)
    assert not notes  # short, and all went well
    run(3)
    assert notes[-1][0] == "A StoryBundle download finished" and "120 files done, 3 failed." in notes[-1][2]  # any failure says so
    notes.clear()
    monkeypatch.setattr(cli, "LONG_JOB", 0)
    run(0, stopped=5)
    assert not notes  # you pressed Ctrl-C


def last_run(job: str) -> dict | None:
    """How hylly jobs says the job's last run ended, or None if none is recorded."""
    return next((r for r in runs.last_runs() if r["job"] == job), None)


FAILED = "2 files failed: see state/storybundle/failures.csv."


def partly() -> Exit:
    return Exit(exits.PARTLY, FAILED)


@pytest.mark.parametrize(("case", "ends", "keyboard", "long", "note", "recorded"), [
    ("finished, after a long while", cli.Ended(text="120 files done, 0 failed."), False, True,
     ("finished", "It took under a minute. 120 files done, 0 failed."), (0, "120 files done, 0 failed.")),
    ("finished soon, all well", cli.Ended(text="3 files done, 0 failed."), False, False, None, (0, "3 files done, 0 failed.")),
    ("finished, saying nothing of itself", None, False, True, ("finished", "It took under a minute."), (0, "")),
    ("failed files, which are a finish", lambda: cli.Ended(text="118 files done, 2 failed.", failed=2).carried(partly()),
     False, False, ("finished", "It took under a minute. 118 files done, 2 failed."), (1, FAILED)),
    ("failed files, with someone at the keyboard", lambda: cli.Ended(failed=2).carried(partly()), True, False,
     ("finished", "It took under a minute."), (1, FAILED)),
    ("partly done, no file failed: a stop", partly, False, False, ("stopped", FAILED), (1, FAILED)),
    ("stopped before its end", cli.Ended(text="2 files done, 0 failed.", stopped=4), False, True, None,
     (0, "2 files done, 0 failed.")),
    ("stopped by its disk", lambda: Exit(exits.LOCAL, "The disk is full."), False, False, ("stopped", "The disk is full."),
     (exits.LOCAL, "The disk is full.")),
    ("stopped by its disk, at once at the keyboard", lambda: Exit(exits.LOCAL, "The disk is full."), True, False, None,
     (exits.LOCAL, "The disk is full.")),
    ("its own note told already", lambda: cli.Ended(told=True).carried(Exit(exits.LOCAL, "The disk is full.")), False,
     False, None, (exits.LOCAL, "The disk is full.")),
    ("its words in counts, its end without words", lambda: cli.Ended(text="1 problem found.", failed=1, told=True).carried(
        Exit(exits.PARTLY)), False, False, None, (exits.PARTLY, "1 problem found.")),
    ("needing a person, its work done", lambda: cli.Ended(ran=True).carried(Exit(exits.NEEDS_YOU, "Nothing was kept.")),
     True, False, None, (exits.NEEDS_YOU, "Nothing was kept.")),
    ("needing a person before it began", lambda: Exit(exits.NEEDS_YOU, "It asks first."), False, False, None, None),
    ("an expired login, its Ended carried through", lambda: cli.Ended(stopped=3).carried(AuthError("It has expired.")),
     False, False, ("stopped", "Error: It has expired."), (exits.LOGIN, "Error: It has expired.")),
])
def test_how_a_run_ended_is_returned_or_carried_to_its_note_and_hylly_jobs(case, ends, keyboard, long, note, recorded,
                                                                           tmp_path, monkeypatch, notes):
    """A long job's handler returns how its run ended (cli.Ended), or the Exit it ends with carries it: its watch makes
    the phone's note and what hylly jobs records of that and the exit code, each as before there was an Ended."""
    def does():
        if callable(ends):
            raise ends()
        return ends

    job_that(monkeypatch, does)
    monkeypatch.setattr(cli, "at_keyboard", lambda: keyboard)
    if long:
        monkeypatch.setattr(cli, "LONG_JOB", 0)
    with contextlib.suppress(SystemExit):
        cli.main(["storybundle", "download", "--library", str(tmp_path)])
    assert [(title.removeprefix("A StoryBundle download "), text) for title, text, _ in notes] == ([note] if note else []), case
    last = last_run("storybundle")
    assert (None if last is None else (last["exit"], last["said"])) == recorded, case


def test_what_a_run_said_of_itself_counts_when_another_error_takes_its_end_s_place(tmp_path, monkeypatch, notes):
    """An error raised as the run's own end is being handled (in a finally or an except: a note that couldn't go, the
    words _in_words makes of a disk's trouble) takes that end's place, and what the run said of itself counts all the
    same, as does what its handler returned before its lock's end refused it: one that told its stop already gets no
    second note, and hylly jobs keeps its words."""
    def told_then_trouble():
        try:
            raise cli.Ended(text="It stopped.", told=True).carried(Exit(exits.LOCAL, "The target's disk is gone."))
        finally:
            raise OSError(5, "Input/output error")

    job_that(monkeypatch, told_then_trouble)
    with pytest.raises(SystemExit) as e:
        cli.main(["storybundle", "download", "--library", str(tmp_path)])
    assert e.value.code == exits.LOCAL and not notes and last_run("storybundle")["said"] == "Can't go on: [Errno 5] Input/output error"

    @contextlib.contextmanager
    def refused_at_its_end(job, root=None):
        yield
        raise Exit(exits.LOCAL, "The lock couldn't be let go.")

    job_that(monkeypatch, lambda: cli.Ended(text="12 files done, 0 failed.", told=True))
    monkeypatch.setattr(runs, "only_one", refused_at_its_end)
    with pytest.raises(SystemExit):
        cli.main(["storybundle", "download", "--library", str(tmp_path)])
    assert not notes and last_run("storybundle")["exit"] == exits.LOCAL


def test_a_run_begun_while_another_s_end_is_handled_says_only_its_own(tmp_path, monkeypatch, notes):
    """A run that begins in an except, while another run's end is being handled: what that one said of itself (it told
    its stop already) is not this one's, whose own stop gets its note."""
    def gone():
        raise Exit(exits.LOCAL, "The library's disk is gone.")

    job_that(monkeypatch, gone)
    monkeypatch.setattr(cli, "at_keyboard", lambda: False)
    try:
        raise cli.Ended(text="It stopped.", told=True).carried(Exit(exits.LOCAL, "An earlier run's end."))
    except SystemExit:
        with pytest.raises(SystemExit) as e:
            cli.main(["storybundle", "download", "--library", str(tmp_path)])
    assert e.value.code == exits.LOCAL
    assert [title for title, _, _ in notes] == ["A StoryBundle download stopped"]


def test_a_second_run_refused_is_neither_noted_nor_recorded(tmp_path, monkeypatch, notes):
    """Busy: the run that holds the job goes on, and this one, which never began, is no run of hylly jobs'."""
    job_that(monkeypatch, lambda: pytest.fail("it shouldn't run"))
    runs.remember("storybundle", "2026-11-01 10:00", 60, 0, "All 12 files done.")
    for keyboard in (False, True):
        monkeypatch.setattr(cli, "at_keyboard", lambda: keyboard)
        with runs.only_one("storybundle"), pytest.raises(runs.Busy):
            cli.main(["storybundle", "download", "--library", str(tmp_path)])
    assert not notes and last_run("storybundle")["said"] == "All 12 files done."


def test_each_shop_s_run_of_hylly_download_says_only_how_it_ended(tmp_path, monkeypatch, notes):
    """hylly download runs each shop's download as a command of its own: what one shop's run said of itself (two files
    failed) is never another's, whose note says only how long it took."""
    from hylly.logins import SITES

    for shop in ("humble", "storybundle", "drivethrurpg", "itchio"):
        monkeypatch.setenv(SITES[shop][2], "a login")
    def failed(args):
        raise cli.Ended(text="7 files done, 2 failed.", failed=2).carried(Exit(exits.PARTLY, "2 files failed."))

    monkeypatch.setattr(cli, "cmd_humble", failed)
    for name in ("cmd_storybundle", "cmd_drivethrurpg", "cmd_itchio"):
        monkeypatch.setattr(cli, name, lambda args: None)
    monkeypatch.setattr(cli, "LONG_JOB", 0)
    with pytest.raises(SystemExit):
        cli.main(["download", "--library", str(tmp_path)])
    assert [text for _, text, _ in notes] == ["It took under a minute. 7 files done, 2 failed."] + ["It took under a minute."] * 3
    assert last_run("storybundle")["said"] == "" and last_run("humble")["said"] == "2 files failed."


def test_a_catalog_build_kept_at_the_keyboard_after_a_long_while_says_it_finished(tmp_path, monkeypatch, notes, capsys):
    """Its question of far fewer titles, answered yes at the keyboard, made the run's facts say only that it ran: a build
    that had taken more than half an hour then ended in a KeyError, exit code 70, after it was kept. Now it finishes,
    and its note says how long it took."""
    from hylly import catalog

    def build(root, fewer=None, **kw):
        assert fewer("This build has far fewer titles than the last one:\n  ebooks: 2 of 20")
        return {"titles": 2}

    monkeypatch.setattr(catalog, "build", build)
    monkeypatch.setattr(catalog, "leftovers", lambda root, log: None)
    monkeypatch.setattr(cli, "at_keyboard", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    monkeypatch.setattr(cli, "LONG_JOB", 0)
    cli.main(["catalog", "--library", str(tmp_path)])
    assert notes == [("A catalog build finished", "It took under a minute.", "It took under a minute.")]
    assert last_run("catalog")["exit"] == 0 and "Keep this build all the same" in capsys.readouterr().err


# ---- a day's budget that rolls --------------------------------------------------------------------------

def test_a_rerun_the_next_day_waits_for_the_budget_rather_than_ending(tmp_path):
    clock = Clock()
    source = openlibrary.OpenLibrary()
    s = session_for(source, lambda request: httpx.Response(200), clock, tmp_path, per_day=2)
    s.budget.note()
    clock.sleep(600)
    s.budget.note()
    clock.t += 86400 - 900  # the same hour the next day, 5 minutes before the first place comes free
    clock.slept.clear()
    assert s.get("https://openlibrary.org/x").status_code == 200
    assert 200 < max(clock.slept) <= 900


def test_a_full_day_doesn_t_skip_the_hour_s_limit(tmp_path):
    budget = lookups.Budget(tmp_path / "budget.txt", per_hour=1, per_day=1)
    budget.now = lambda: 1_000_000.0
    budget.times = [1_000_000.0 - 86400 + 5]  # the day's place comes free in 5 seconds...
    assert budget.wait() == 5.0
    budget.per_day = 2
    budget.times = [1_000_000.0 - 86400 + 5, 1_000_000.0 - 10]  # ...and the hour's was used 10 seconds ago
    assert budget.wait() > 3000


def test_a_spent_day_says_when_there_is_more(tmp_path):
    clock = Clock()
    s = session_for(openlibrary.OpenLibrary(), lambda request: httpx.Response(200), clock, tmp_path, per_day=1)
    s.budget.note()
    with pytest.raises(lookups.Quota, match="more from"):
        s.get("https://openlibrary.org/x")


# ---- a program of your own beside ntfy ([bell] command) -----------------------------------------------------------

SAYS = """import json, os, subprocess, sys, time
out, how = sys.argv[1], sys.argv[2]


def child(**options):  # a program it starts, as a script's mail program would be: says its number, and waits
    found = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **options)
    with open(out + ".child.part", "w") as fh:  # whole or not there: a test that sees the file reads its number
        fh.write(str(found.pid))
    os.replace(out + ".child.part", out + ".child")


if how == "slow":
    time.sleep(30)
if how == "child":
    child()
    time.sleep(30)
if how == "detach":  # one of its own session, which keeps this one's standard error open after it ends
    child(start_new_session=True)
    sys.exit(0)
if how in ("flood", "flood-forever"):
    for n in iter(int, 1) if how == "flood-forever" else range(512):  # 32 MB, or no end
        sys.stderr.write("x" * 65535 + "\\n")
    sys.stderr.write("the end of what it said\\n")
    sys.exit(3)
if how == "meet":  # started while ntfy is asked: each waits for the other's mark, up to 10 seconds
    open(out + ".started", "w").close()
    until = time.monotonic() + 10
    while not os.path.exists(out + ".posted") and time.monotonic() < until:
        time.sleep(0.02)
with open(out, "w", encoding="utf-8") as fh:
    json.dump({"argv": sys.argv[1:], "stdin": sys.stdin.read(),
               "env": {k: v for k, v in os.environ.items() if k.startswith("HYLLY_")}}, fh)
if how == "fail":
    sys.stderr.write("made-up trouble\\nmail: no mail server here\\n")
    sys.exit(3)
"""


def a_command(tmp_path, how: str = "well") -> tuple[list[str], Path]:
    """A made-up program of the owner's, as [bell] command names it: it writes what it got, its arguments, its standard
    input and its environment, to a file, and does as how says (fail, slow, meet)."""
    script = tmp_path / "says.py"
    script.write_text(SAYS, encoding="utf-8")
    return [sys.executable, str(script), str(tmp_path / "said.json"), how], tmp_path / "said.json"


def test_a_command_of_yours_gets_what_the_phone_gets_never_on_its_command_line(phone, tmp_path, monkeypatch):
    """The alarm's title and text in its environment and on its standard input, what the phone gets (counts, unless
    remote_detail says otherwise), and nothing of the alarm among its arguments; Hylly's secrets not in its environment;
    and ntfy as ever."""
    posted, _ = phone
    monkeypatch.setenv("HYLLY_NTFY_TOKEN", "tk_madeupmadeupmadeup")
    monkeypatch.setenv("HYLLY_HUMBLE_SESSION", "made-up-cookie")
    words, out = a_command(tmp_path)
    w, clock = watching(command=words)
    for _ in range(3):
        w.event("wrong", name=NAME, ip=ADDRESS, known=True)
    said = json.loads(out.read_text(encoding="utf-8"))
    env = said["env"]
    assert said["argv"] == words[2:]  # its own words, nothing added
    assert env["HYLLY_ALARM_TITLE"] == "Wrong passwords" == posted[0]["title"]
    assert env["HYLLY_ALARM_TEXT"] == posted[0]["message"] and env["HYLLY_ALARM_PRIORITY"] == "high"
    assert datetime.fromisoformat(env["HYLLY_ALARM_TIME"]).tzinfo is not None
    assert said["stdin"] == f"Wrong passwords\n\n{posted[0]['message']}\n"
    assert NAME not in out.read_text(encoding="utf-8") and ADDRESS not in out.read_text(encoding="utf-8")
    assert "HYLLY_NTFY_TOKEN" not in env and "HYLLY_HUMBLE_SESSION" not in env
    assert len(posted) == 1 and bell.COMMAND_FAILED not in bell.log_file().read_text(encoding="utf-8")
    full, _ = watching(command=words, remote_detail="full")
    full.alarm("wrong", "in full", f"Wrong passwords for {NAME}", f"3 wrong passwords for {NAME}, from {ADDRESS}.",
               public=("Wrong passwords", "3 wrong passwords for a real user."))
    said = json.loads(out.read_text(encoding="utf-8"))
    assert said["env"]["HYLLY_ALARM_TEXT"] == f"3 wrong passwords for {NAME}, from {ADDRESS}." and said["argv"] == words[2:]
    assert NAME in said["stdin"]


@pytest.mark.parametrize("how, why", [("fail", r"python\S* ended with code 3: mail: no mail server here"),
                                      ("missing", r"no-such-program wasn't found"),
                                      ("slow", r"python\S* hadn't ended after 1 seconds, and was stopped")])
def test_a_command_that_fails_is_in_bell_log_and_stops_nothing(phone, tmp_path, monkeypatch, how, why):
    """A code other than 0, a program that isn't there, or one past its time: in bell.log and the server's log, and
    neither the phone nor the next alarm waits for it longer than its limit."""
    posted, _ = phone
    monkeypatch.setattr(bell, "COMMAND_LIMIT", 1)
    words, _ = a_command(tmp_path, how)
    if how == "missing":
        words[0] = str(tmp_path / "no-such-program")
    logged = []
    w = bell.Watch({**bell.DEFAULTS, "ntfy": TOPIC, "command": words}, log=logged.append)
    started = time.monotonic()
    problems = w.push("Users changed", "ann added (owner).", "high", ("Users changed", "1 change."))
    assert time.monotonic() - started < 8 and len(posted) == 1  # the phone got it all the same
    assert len(problems) == 1 and problems[0].startswith("the alarm command failed: ") and re.search(why, problems[0])
    assert re.search(rf"\d command: {bell.COMMAND_FAILED}\. .*{why}", bell.log_file().read_text(encoding="utf-8"))
    assert logged == [f"Alarms: {problems[0]}."] and re.search(why, bell.command_trouble()[1])


def test_the_first_run_that_works_after_a_failure_says_so_once(phone, tmp_path):
    words, _ = a_command(tmp_path, "fail")
    w, _ = watching(command=words)
    w.push("A test alarm", "text", "high")
    assert bell.command_trouble() is not None
    w.conf["command"] = [*words[:-1], "well"]
    w.push("A test alarm", "text", "high")
    w.push("A test alarm", "text", "high")
    assert bell.log_file().read_text(encoding="utf-8").count(bell.COMMAND_WORKS) == 1 and bell.command_trouble() is None


def test_ntfy_and_the_command_go_side_by_side(tmp_path, monkeypatch):
    """Neither waits for the other: each waits up to 10 seconds for the other's mark, so one after the other would take
    that long."""
    words, out = a_command(tmp_path, "meet")

    def post(url, json=None, **k):
        until = time.monotonic() + 10
        while not Path(f"{out}.started").exists() and time.monotonic() < until:
            time.sleep(0.02)
        Path(f"{out}.posted").touch()
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "post", post)
    w, _ = watching(command=words, toast=False)
    started = time.monotonic()
    assert w.push("A test alarm", "text", "high") == []
    assert time.monotonic() - started < 8 and out.is_file()


def test_with_no_command_set_nothing_runs(phone, monkeypatch):
    posted, _ = phone
    ran = []
    monkeypatch.setattr(bell, "run_command", lambda *a, **k: ran.append(a) or "")
    assert bell.settings()["command"] == [] and not any("command" in c for c in bell.channels(bell.settings()))
    w, _ = watching()
    assert w.alarm("users", "x", "Users changed", "ann added (owner).") and len(posted) == 1 and ran == []


def test_hylly_bell_test_runs_the_command_too_and_says_what_went_wrong(phone, tmp_path, monkeypatch, capsys):
    words, out = a_command(tmp_path)
    monkeypatch.setattr(bell, "settings", lambda: {**bell.DEFAULTS, "ntfy": TOPIC, "command": words})
    cli.main(["bell", "test"])
    assert json.loads(out.read_text(encoding="utf-8"))["env"]["HYLLY_ALARM_TITLE"] == "A test alarm"
    assert f"the command {bell.program(words)}" in capsys.readouterr().out
    failing, _ = a_command(tmp_path, "fail")
    monkeypatch.setattr(bell, "settings", lambda: {**bell.DEFAULTS, "ntfy": TOPIC, "command": failing})
    with pytest.raises(SystemExit) as e:
        cli.main(["bell", "test"])
    assert e.value.code == exits.PARTLY and "But: the alarm command failed: " in capsys.readouterr().out


def test_a_job_s_note_runs_the_command_and_the_phone_alone_again_doesn_t(phone, tmp_path, monkeypatch):
    posted, _ = phone
    words, out = a_command(tmp_path)
    monkeypatch.setattr(bell, "settings", lambda: {**bell.DEFAULTS, "ntfy": TOPIC, "command": words})
    assert bell.job_note("A lookup stopped", f"{NAME} said no", "Its source said no.")
    env = json.loads(out.read_text(encoding="utf-8"))["env"]
    assert (env["HYLLY_ALARM_TITLE"], env["HYLLY_ALARM_TEXT"], env["HYLLY_ALARM_PRIORITY"]) == ("A lookup stopped", "Its source said no.", "default")
    out.unlink()
    assert bell.phone_again("A lookup stopped", f"{NAME} said no", "Its source said no.") and len(posted) == 2
    assert not out.exists()  # it ran for the note already


def gone(pid: int, within: float = 5.0) -> bool:
    """Whether a made-up program of a test has ended (a zombie waiting for its parent counts), within a few seconds."""
    import psutil

    until = time.monotonic() + within
    while time.monotonic() < until:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


def test_a_command_that_says_a_lot_fills_no_memory_and_holds_nothing_up(tmp_path, monkeypatch):
    """Of its standard error only the end is kept, as it comes; one past its limit, saying more all the while, is done
    with within its limit and a moment."""
    import tracemalloc

    words, _ = a_command(tmp_path, "flood")
    tracemalloc.start()
    try:
        said = bell.run_command(words, "A test alarm", "text", "high", limit=30)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert re.fullmatch(r"python\S* ended with code 3: the end of what it said", said) and peak < 8 << 20  # of 32 MB said
    words, _ = a_command(tmp_path, "flood-forever")
    started = time.monotonic()
    said = bell.run_command(words, "A test alarm", "text", "high", limit=1)
    assert said.endswith("hadn't ended after 1 seconds, and was stopped") and time.monotonic() - started < 4


def test_a_command_past_its_time_is_stopped_with_what_it_started(tmp_path):
    """On Linux and macOS its session, on Windows its job object: a script's own program ends with it."""
    words, out = a_command(tmp_path, "child")
    said = bell.run_command(words, "A test alarm", "text", "high", limit=2)
    child = int(Path(f"{out}.child").read_text(encoding="utf-8"))
    assert said.endswith("hadn't ended after 2 seconds, and was stopped") and gone(child)


@pytest.mark.skipif(sys.platform == "win32", reason="a session of its own is Linux's and macOS's")
def test_a_command_that_leaves_a_program_of_its_own_running_has_ended_well(tmp_path):
    """One that started a program in a session of its own and ended: it ended, and says so at once, though that program
    keeps its standard error open; that program is its own, and runs on (ended here by the test)."""
    import os
    import signal

    words, out = a_command(tmp_path, "detach")
    started = time.monotonic()
    said = bell.run_command(words, "A test alarm", "text", "high", limit=10)
    child = int(Path(f"{out}.child").read_text(encoding="utf-8"))
    try:
        assert said == "" and time.monotonic() - started < 5 and not gone(child, within=0.5)
    finally:
        os.kill(child, signal.SIGKILL)  # the test's own made-up program, by its number


def test_a_ctrl_c_during_hylly_bell_test_or_a_job_s_note_ends_the_command(phone, tmp_path, monkeypatch):
    """In a session of its own it hears no Ctrl-C: the program that waits for it ends it."""
    join = threading.Thread.join
    words, out = a_command(tmp_path, "child")

    def interrupted(self, timeout=None):  # Ctrl-C reaches the main thread, once the command and its program run
        if threading.current_thread() is not threading.main_thread():
            return join(self, timeout)
        until = time.monotonic() + 10
        while not Path(f"{out}.child").exists() and time.monotonic() < until:
            time.sleep(0.02)
        raise KeyboardInterrupt

    monkeypatch.setattr(bell, "settings", lambda: {**bell.DEFAULTS, "ntfy": TOPIC, "command": words})
    monkeypatch.setattr(bell.threading.Thread, "join", interrupted)
    started = time.monotonic()
    bell.job_note("A lookup stopped", "text")
    assert time.monotonic() - started < 10 and gone(int(Path(f"{out}.child").read_text(encoding="utf-8")))
    stopped = f"command: {bell.COMMAND_FAILED}. python"
    until = time.monotonic() + 5  # the note's own thread writes it, a moment after
    while bell.log_file().read_text(encoding="utf-8").count(stopped) < 1 and time.monotonic() < until:
        time.sleep(0.05)
    assert re.search(r"command: The alarm command failed\. python\S* was stopped by a Ctrl-C", bell.log_file().read_text(encoding="utf-8"))
    Path(f"{out}.child").unlink()
    w, _ = watching(command=words)
    with pytest.raises(KeyboardInterrupt):
        w.push("A test alarm", "text", "high")
    assert gone(int(Path(f"{out}.child").read_text(encoding="utf-8")))
    assert len(re.findall(r"python\S* was stopped by a Ctrl-C", bell.log_file().read_text(encoding="utf-8"))) == 2


def test_a_command_where_the_server_can_write_isn_t_run(phone, tmp_path, monkeypatch):
    """Its program, or a file its words name (alone, after =, after curl's @), in the settings folder, secrets/ too, or
    the library, even by a link from elsewhere: refused at every run, as 7-Zip there is, and said why."""
    import shutil

    from hylly import paths

    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setenv("HYLLY_LIBRARY", str(library))
    words, out = a_command(tmp_path)
    (paths.config_dir() / "secrets").mkdir(parents=True, exist_ok=True)
    inside = [shutil.copy(words[1], paths.config_dir() / "says.py"), shutil.copy(words[1], library / "says.py")]
    config = paths.config_dir() / "secrets" / "webhook.curl"
    config.write_text('url = "https://hooks.example.org/made-up"\n', encoding="utf-8")
    link = tmp_path / "elsewhere.py"
    link.symlink_to(inside[0])
    for given in ([words[0], str(inside[0]), *words[2:]], [words[0], str(inside[1]), *words[2:]],
                  [words[0], str(link), *words[2:]], [*words, f"--config={config}"], [*words, f"@{config}"],
                  [*words, "--config", str(config)], [*words, f"-K{config}"], [*words, f"-d@{config}"],
                  [*words, f"--data=@{config}"]):
        said = bell.run_command(given, "A test alarm", "text", "high")
        assert re.fullmatch(r"\S+ is in one of Hylly's own folders or the library, so it isn't run: the server can write there, "
                            r"and your own commands run the alarm command too", said), given
        assert not out.exists()
    w, _ = watching(command=[*words, f"@{config}"])
    assert w.push("A test alarm", "text", "high")[0].startswith("the alarm command failed: ") and not out.exists()
    assert "isn't run" in bell.command_trouble()[1]
    assert bell.run_command(words, "A test alarm", "text", "high") == "" and out.exists()  # from elsewhere: it runs


def test_the_command_s_latest_word_is_found_in_the_log_moved_aside_too():
    """bell.log moves aside as bell.log.1 when it grows: the latest word on the command may be there. Only a line's
    start counts, its time and "command: "."""
    log = bell.log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.with_suffix(".log.1").write_text(f"2026-11-16 08:00:00 command: {bell.COMMAND_FAILED}. mail ended with code 1\n", encoding="utf-8")
    log.write_text("2026-11-16 08:05:00 users: Users changed. ann added (owner).\n", encoding="utf-8")
    assert bell.command_trouble() == ("2026-11-16 08:00:00", "mail ended with code 1")
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"2026-11-16 08:10:00 command: {bell.COMMAND_WORKS}. It ran and ended well.\n")
    assert bell.command_trouble() is None


def test_7_zip_and_the_alarm_command_go_by_one_rule():
    from hylly import archives

    assert archives._server_can_write.__module__ == "hylly.archives"  # it asks bell's, with the library looked at now
    told = []
    with pytest.MonkeyPatch.context() as m:
        m.setattr(bell, "server_can_write", lambda path, now=False: told.append((path, now)) or True)
        assert archives._server_can_write("/opt/7zz") and told == [("/opt/7zz", True)]


def test_an_alarm_never_asks_the_library_s_disk_which_may_hang(phone, tmp_path, monkeypatch):
    """"The library can't be reached" is an alarm when a share may hang whatever asks it: the guard compares with the
    library's path as written, and as it resolved when the alarms last saw it there, and asks its disk nothing. Here
    any look at the library hangs (stood in), and the alarm goes on."""
    import pathlib

    real = tmp_path / "disk" / "books"
    real.mkdir(parents=True)
    library = tmp_path / "books"
    library.symlink_to(real, target_is_directory=True)  # the library, as written, a link to where it is
    monkeypatch.setenv("HYLLY_LIBRARY", str(library))
    (real / "state").mkdir()
    bell.paths.marker_file(real).write_text("{}", encoding="utf-8")
    w, _ = watching()
    w.library = library
    assert w._library_there() and bell._seen[str(library)] == real.resolve()  # remembered, while it's there
    words, out = a_command(tmp_path)
    elsewhere = tmp_path / "elsewhere.py"
    elsewhere.symlink_to(Path(words[1]).rename(real / "says.py"))  # a link from elsewhere to a script on the library's disk
    hung, touched = threading.Event(), []
    resolve, stat = pathlib.Path.resolve, pathlib.Path.stat
    on_its_disk = (str(library), str(real.resolve()), str(real))

    def asks(path) -> bool:
        if str(path).startswith(on_its_disk):
            touched.append(str(path))
            hung.wait(30)  # a share that doesn't answer
            return True
        return False

    monkeypatch.setattr(pathlib.Path, "resolve", lambda self, strict=False: (asks(self), resolve(self, strict))[1])
    monkeypatch.setattr(pathlib.Path, "stat", lambda self, **k: (asks(self), stat(self, **k))[1])
    said = []
    try:
        for given in ([words[0], str(library / "says.py"), *words[2:]], [words[0], str(real / "says.py"), *words[2:]]):
            going = threading.Thread(target=lambda given=given: said.append(bell.command_refused(given)), daemon=True)
            going.start()
            going.join(10)
            assert not going.is_alive() and touched == [], touched
        assert all("isn't run" in reason for reason in said) and len(said) == 2
    finally:
        hung.set()
    assert "isn't run" in bell.command_refused([words[0], str(elsewhere), *words[2:]])  # resolved: where it was seen
