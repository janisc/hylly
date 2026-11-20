"""One library server at a time: options from settings, the lock, stopping it, its log, and the logon task."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from xml.etree import ElementTree as ET

import pytest

from hylly import exits, instance, serve, storage


def test_options_come_from_what_was_typed_for_the_run_then_settings(monkeypatch, tmp_path):
    from hylly import settings as the_settings

    assert serve.options() == (8770, "127.0.0.1")  # nothing set: this PC only
    settings = tmp_path / "settings.toml"
    settings.write_text("library = 'x'\n[server]\nport = 9000\nlisten = \"::\"\n", encoding="utf-8")
    monkeypatch.setenv("HYLLY_SETTINGS", str(settings))
    the_settings.reload()  # as the next command reads them
    assert serve.options() == (9000, "::")
    assert serve.options({"port": 9100, "lan": False}) == (9100, "127.0.0.1")  # --no-lan
    assert serve.options({"lan": True}) == (9000, "0.0.0.0")  # --lan: every IPv4 address, that run


def running_already(monkeypatch, note: dict) -> tuple[list, list, list]:
    """A server running, with this note: what hylly serve would stop, start in the background, and run here."""
    stopped, started, ran = [], [], []
    monkeypatch.setattr(instance, "running", lambda: note)
    monkeypatch.setattr(instance, "stop", lambda: stopped.append(1) or {})
    monkeypatch.setattr(instance, "start_background", lambda: started.append(1) or {"port": 8770})
    monkeypatch.setattr(serve, "run", lambda root, **kw: ran.append(kw))
    monkeypatch.setattr(serve, "describe", lambda info: f"  process {info.get('pid')}")
    return stopped, started, ran


def test_a_plain_start_with_a_server_running_says_where_it_is_and_restarts_nothing(monkeypatch, capsys):
    """One started by hand with options of its own (the help's --idle 30 was one) is left as it is by a plain
    hylly serve, the launchers' too, in the foreground or not: nothing remembers options, so nothing differs."""
    from hylly import cli

    stopped, started, ran = running_already(monkeypatch, {"pid": 7, "port": 8790, "listen": "0.0.0.0", "given": {"port": 8790, "lan": True}})
    for words in (["serve", "--no-browser"], ["serve", "--background", "--no-browser"]):
        cli.main(words)
        assert "The library server is already running.\n  process 7" in capsys.readouterr().out
    assert stopped == started == ran == []


def test_options_typed_with_a_server_running_restart_nothing_and_say_so(monkeypatch, capsys):
    from hylly import cli

    stopped, started, ran = running_already(monkeypatch, {"pid": 7, "port": 8770, "listen": "127.0.0.1"})
    with pytest.raises(SystemExit) as e:
        cli.main(["serve", "--lan", "--no-browser"])
    said = capsys.readouterr()
    assert e.value.code == exits.BUSY and "already running" in said.out and "nothing was restarted" in said.err
    assert stopped == started == ran == []


def test_a_typed_port_or_library_the_running_server_hasn_t_is_named_and_ends_busy(monkeypatch, tmp_path, capsys):
    """A script that asked for a port, another library, --lan or rules files mustn't take the server running for the one
    it asked for: the difference is said in one line, with exit code 4, its dry run too, and nothing is stopped or
    started."""
    from hylly import cli

    library, other = tmp_path / "library", tmp_path / "other"
    stopped, started, ran = running_already(monkeypatch, {"pid": 7, "port": 8813, "listen": "127.0.0.1", "library": str(library)})
    for words, says in ((["--port", "8814"], "It runs on port 8813, not 8814, and nothing was restarted"),
                        (["--library", str(other)], f"It runs for the library in {library}, not {other}, and nothing"),
                        (["--port", "8814", "--library", str(other), "--dry-run"], f"on port 8813, not 8814 and for the library in {library}")):
        with pytest.raises(SystemExit) as e:
            cli.main(["serve", *words, "--no-browser"])
        assert e.value.code == exits.BUSY and says in capsys.readouterr().err, words
    rules = tmp_path / "mine.toml"
    for words, says in ((["--lan"], "It runs listening on 127.0.0.1, not 0.0.0.0, and nothing"),
                        (["--rules", str(rules)], f"It runs with no rules files of --rules, not {rules}, and nothing")):
        with pytest.raises(SystemExit) as e:
            cli.main(["serve", *words, "--no-browser"])
        assert e.value.code == exits.BUSY and says in capsys.readouterr().err, words
    with pytest.raises(SystemExit) as e:  # what it has already, typed: nothing differs to name, and still nothing restarted
        cli.main(["serve", "--port", "8813", "--library", str(library), "--no-lan", "--no-browser"])
    assert e.value.code == exits.BUSY and "It runs as it was started, and nothing was restarted" in capsys.readouterr().err
    again = running_already(monkeypatch, {"pid": 7, "port": 8813, "listen": "0.0.0.0", "given": {"rules": [str(rules)]}})
    with pytest.raises(SystemExit) as e:  # one started with these rules and --lan: the same, so nothing to name
        cli.main(["serve", "--rules", str(rules), "--lan", "--no-browser"])
    assert e.value.code == exits.BUSY and "It runs as it was started" in capsys.readouterr().err
    assert stopped == started == ran == [] and again == ([], [], [])


def test_a_server_in_the_background_takes_the_settings_only(monkeypatch, tmp_path, capsys):
    """hylly serve --background, the Windows task's start, takes no options, as a restart or the next logon
    would bring back another server; hyllyd --in-background, a service's, neither. One here, in the foreground, takes
    them for that run."""
    from hylly import cli, daemon

    stopped, started, ran = running_already(monkeypatch, None)
    for words in (["serve", "--background", "--lan"], ["serve", "--background", "--port", "8790"],
                  ["serve", "--background", "--library", str(tmp_path)], ["serve", "--background", "--rules", "mine.toml"]):
        with pytest.raises(SystemExit) as e:
            cli.main(words)
        assert e.value.code == exits.USAGE and "takes its options from the settings" in capsys.readouterr().err
    # in the server's log too: a Windows task from before runs without a window, and hylly server status says it
    assert instance.log_file().read_text(encoding="utf-8").count("takes its options from the settings") == 4
    with pytest.raises(SystemExit) as e:  # a dry run says it too, and changes nothing, the log neither
        cli.main(["serve", "--background", "--port", "8790", "--dry-run"])
    assert e.value.code == exits.USAGE and "takes its options from the settings" in capsys.readouterr().err
    assert instance.log_file().read_text(encoding="utf-8").count("takes its options from the settings") == 4
    assert "Its last message" in serve.status() and "--rules can't be given with it" in serve.status()
    with pytest.raises(SystemExit) as e:
        daemon.main(["--in-background", "--port", "8790"])
    assert e.value.code == exits.USAGE and "--port can't be given with --in-background" in capsys.readouterr().err
    assert started == ran == []
    monkeypatch.setenv("HYLLY_LIBRARY", str(tmp_path))
    cli.main(["serve", "--background", "--no-browser"])
    assert started == [1]  # with no options at all: it reads the settings itself


def test_rules_typed_for_a_run_go_to_that_run_only(monkeypatch, tmp_path):
    """--rules, typed on hylly serve or hyllyd in the foreground, by their full paths, for that run; a restart takes the
    settings' rules only (nothing is passed between processes but what the settings say)."""
    from hylly import cli, daemon, rules

    stopped, started, ran = running_already(monkeypatch, None)
    monkeypatch.chdir(tmp_path)
    mine = str(tmp_path / "mine.toml")
    cli.main(["serve", "--no-browser", "--library", str(tmp_path), "--rules", "mine.toml"])
    assert ran[-1]["given"] == {"library": str(tmp_path), "rules": [mine]} and rules.EXTRA == [mine]
    assert ran[-1]["open_browser"] is False
    daemon.main(["--library", str(tmp_path), "--rules", "mine.toml"])  # hyllyd in a terminal: the same, and no browser ever
    assert ran[-1]["given"]["rules"] == [mine] and ran[-1]["open_browser"] is False and ran[-1]["background"] is False
    monkeypatch.setattr(instance, "running", lambda: {"pid": 7, "port": 8770, "given": ran[-1]["given"]})
    monkeypatch.setenv("HYLLY_LIBRARY", str(tmp_path))  # the settings' library: the hidden server's
    cli.main(["server", "restart", "--no-browser"])  # as Restart on the server page does
    assert stopped == [1] and started == [1]  # its rules are the settings' now


def test_the_idle_stop_is_gone_and_its_setting_is_named_once(monkeypatch, capsys):
    """The server runs until it is stopped. idle_minutes in the settings is named as it starts, and left out; --idle
    is no option any more; the page's session answer says nothing of it."""
    from hylly import cli, paths, settings

    paths.settings_file().write_text("[server]\nidle_minutes = 30\n", encoding="utf-8")
    settings.reload()
    assert "idle_minutes" not in settings.get("server")
    told = capsys.readouterr().err
    assert "[server] idle_minutes is no longer used, so it is left out. The server no longer stops by itself" in told
    with pytest.raises(SystemExit) as e:
        cli.main(["serve", "--idle", "30"])
    assert e.value.code == exits.USAGE
    assert not hasattr(serve, "expired") and "idle" not in serve.describe({"pid": 1, "port": 8770}).lower()
    page = (instance.Path(serve.__file__).parent / "catalog_page.html").read_text(encoding="utf-8")
    assert "s.idle" not in page and "idle_minutes" not in page  # the page sends no "still open" every minute


@pytest.mark.slow
def test_one_server_at_a_time():
    assert instance.running() is None
    first = instance.Claim()
    assert instance.running() == {}  # running, but it has not said where yet
    first.announce(pid=os.getpid(), port=8770)
    assert instance.running() == {"pid": os.getpid(), "port": 8770}
    with pytest.raises(instance.AlreadyRunning):
        instance.Claim()
    first.release()
    assert instance.running() is None and not instance.info_file().exists()
    instance.Claim().release()  # free again


def test_stop_ends_the_server_process():
    script = ("import os, time\nfrom datetime import datetime\nfrom hylly import instance\nclaim = instance.Claim()\n"
              "claim.announce(pid=os.getpid(), started=datetime.now().isoformat(timespec='seconds'), began=time.time())\n"
              "time.sleep(60)")
    child = subprocess.Popen([sys.executable, "-c", script])  # on Windows a venv launcher, with the real Python under it
    deadline = time.monotonic() + 20
    while not instance.running():
        assert child.poll() is None and time.monotonic() < deadline
        time.sleep(0.1)
    assert instance.stop()["pid"]
    assert child.wait(timeout=10) is not None and instance.running() is None
    assert instance.stop() is None  # nothing left to stop


@pytest.mark.slow
def test_a_note_left_behind_by_a_killed_server_ends_no_other_program():
    """On Windows a stop ends the server at once, and its note stays; the number goes to another program in time.
    While the lock is held for a moment (a start, a check), a stop ends nothing; with the lock free the note goes."""
    import json
    from datetime import datetime, timedelta

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # some program, begun after the note
    try:
        old = (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")
        with instance._open_lock() as fh:
            instance.info_file().write_text(json.dumps({"pid": other.pid, "port": 8770, "started": old}), encoding="utf-8")
            assert instance._lock(fh)
            with pytest.raises(SystemExit, match=f"tells of process {other.pid}, which has ended or is another program"):
                instance.stop()
            instance._unlock(fh)
        assert other.poll() is None  # it runs on
        assert instance.stop() is None and not instance.info_file().exists()  # a leftover, gone
    finally:
        other.kill()
        other.wait()


@pytest.fixture
def sleeper():
    """Some program, running: what a note's number may lead to."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    time.sleep(0.5)  # begun, and after it the notes below
    yield child
    child.kill()
    child.wait()


def test_a_note_is_its_process_s_whatever_the_time_zone_says_later(sleeper, monkeypatch):
    """The note keeps time.time() of the start (began), which no change of time zone moves, nor the hour the
    clocks go back; its local time (started) is only shown. Written in another zone, or read in one, the stop still
    knows its process."""
    from datetime import datetime, timedelta

    elsewhere = (datetime.now() - timedelta(hours=5)).isoformat(timespec="seconds")  # as the clock said in another zone
    note = {"pid": sleeper.pid, "started": elsewhere, "began": time.time()}
    assert instance.wrote(note)
    assert not instance.wrote({**note, "began": time.time() - 600})  # a program begun after the note: not it
    if hasattr(time, "tzset"):  # Linux and macOS: the zone changed between the start and the stop
        try:
            with monkeypatch.context() as zone:
                zone.setenv("TZ", "UTC+5")
                time.tzset()
                assert instance.wrote(note)
        finally:
            time.tzset()


def test_a_clock_set_forward_doesn_t_hide_the_server_from_its_stop(sleeper, monkeypatch):
    """On Linux and macOS the system counts a process's start from the clock as it is now: set forward (a Raspberry Pi
    without a clock, after NTP), every process seems to have begun after its note. There, one whose command line is the
    note's is taken for it: for the server's note hyllyd itself, by its program word or the module after -m; for a job's,
    hylly with the very words the note keeps. Another program with the number is not, an editor with hylly's file open
    or a running download neither, nor anything on Windows, whose count stays."""
    import psutil

    note = {"pid": sleeper.pid, "started": "2026-11-14T10:00:00", "began": time.time()}
    job = {**note, "command": "hylly lookup --source metron"}
    monkeypatch.setattr(psutil.Process, "create_time", lambda self: time.time() + 3600)
    tool = "/home/a/.local/share/uv/tools/hylly/bin"
    for system, command, server, lookup in (
            ("linux", ["/usr/bin/python3", "-P", "-m", "hylly.daemon", "--in-background"], True, False),
            ("mac", [f"{tool}/hyllyd", "--in-background"], True, False),
            ("linux", [f"{tool}/python", f"{tool}/hyllyd", "--in-background"], True, False),  # the tool's script, by its shebang
            ("linux", [f"{tool}/python", f"{tool}/hylly", "lookup", "--source", "metron"], False, True),
            ("linux", ["/usr/bin/python3", "-P", "-m", "hylly", "lookup", "--source", "metron"], False, True),
            ("linux", [f"{tool}/hylly", "download"], False, False),  # another job: not the lookup's
            ("linux", ["/usr/bin/vim", f"{tool}/hyllyd", f"{tool}/hylly"], False, False),  # only the program word counts
            ("linux", ["/usr/bin/python3", "-c", "import time; time.sleep(60)"], False, False),
            ("windows", [r"C:\Programs\Hylly\.venv\Scripts\python.exe", "-P", "-m", "hylly.daemon"], False, False)):
        monkeypatch.setattr(instance, "SYSTEM", system)
        monkeypatch.setattr(psutil.Process, "cmdline", lambda self, command=command: command)
        assert instance.wrote(note) is server and instance.wrote(job, job=True) is lookup, (system, command)


def test_a_note_from_before_still_stops_its_server(sleeper):
    """A server an older Hylly started wrote only its local time, to the second (a job's, to the minute)."""
    from datetime import datetime

    assert instance.wrote({"pid": sleeper.pid, "started": datetime.now().isoformat(timespec="seconds")})
    assert instance.wrote({"pid": sleeper.pid, "started": time.strftime("%Y-%m-%d %H:%M")})
    assert not instance.wrote({"pid": sleeper.pid}) and not instance.wrote({"started": "2026-11-14T10:00:00"})


def test_a_busy_port_is_an_error(tmp_path):
    first, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    try:
        with pytest.raises(serve.CantListen):  # an OSError, with the system's own number and words
            serve.make_server(tmp_path, "127.0.0.1", first.server_port)
    finally:
        first.server_close()


def test_logon_task():
    xml = instance.task_xml(r"C:\Program & Co\pythonw.exe", '-m hylly serve --background --library "D:\\My Books"',
                            r"C:\Program & Co", r"PC\someone")
    task = ET.fromstring(xml.encode("utf-16"))
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert task.find("t:Triggers/t:LogonTrigger/t:UserId", ns).text == r"PC\someone"
    assert task.find("t:Principals/t:Principal/t:LogonType", ns).text == "InteractiveToken"
    assert task.find("t:Actions/t:Exec/t:Command", ns).text == r"C:\Program & Co\pythonw.exe"
    assert task.find("t:Actions/t:Exec/t:Arguments", ns).text.endswith('--library "D:\\My Books"')


def test_the_server_is_started_without_the_current_folder_on_its_import_path(monkeypatch, usual_folder):
    """-P: a hylly of anyone's in the folder a command is run from (Downloads, a share) mustn't stand in for Hylly's own,
    in the background, at logon or after a restart from the server page."""
    started, tasks = [], []
    monkeypatch.setattr(instance.subprocess, "Popen", lambda command, **k: started.append(command))
    instance.spawn([*instance.DAEMON, "--in-background"])  # hyllyd, the server itself
    instance.spawn(instance.RESTART, errors_to_log=True)  # the server page's Restart, apart from the server it stops
    assert started == [[instance._python(), "-P", "-m", "hylly.daemon", "--in-background"],  # with --here: this computer's own,
                       [instance._python(), "-P", "-m", "hylly", "--here", "server", "restart", "--no-browser"]]  # whatever the settings name

    def run(command, **k):  # Task Scheduler, faked: what it would be given
        with open(command[command.index("/XML") + 1], "rb") as fh:
            tasks.append(ET.fromstring(fh.read()))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(instance, "SYSTEM", "windows")
    monkeypatch.setenv("USERNAME", "someone")
    monkeypatch.setattr(instance.subprocess, "run", run)
    instance.autostart_on()
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert tasks[0].find("t:Actions/t:Exec/t:Arguments", ns).text == "-P -m hylly --here serve --background --no-browser"


def test_a_server_gets_none_of_the_shops_logins_or_the_sources_keys_however_it_starts(monkeypatch, tmp_path):
    """Given in the environment, they are for the commands that download and look up: spawn leaves them out of the
    hidden server's environment, and a server started any other way (hylly serve at a terminal, a service) takes them
    out of its own as it starts, before anything it starts could get them. What the server needs stays: where Hylly
    keeps its files, and the alarms' ntfy address; and the command that starts one keeps its own."""
    from hylly.sites import SITES

    logins = {variable for _, _, variable in SITES.values()}
    for variable in logins:
        monkeypatch.setenv(variable, "A-MADE-UP-LOGIN")
    monkeypatch.setenv("HYLLY_NTFY_URL", "https://ntfy.example.org/a-made-up-topic")
    given = []
    monkeypatch.setattr(instance.subprocess, "Popen", lambda command, **k: given.append(k["env"]))
    instance.spawn([*instance.DAEMON, "--in-background"])  # hyllyd, the server itself
    instance.spawn(instance.RESTART, errors_to_log=True)  # the server page's Restart, which starts one in turn
    for env in given:
        assert not logins & set(env) and env["HYLLY_HOME"] == os.environ["HYLLY_HOME"] and env["PYTHONUNBUFFERED"] == "1"
        assert env["HYLLY_NTFY_URL"] == "https://ntfy.example.org/a-made-up-topic"
    assert all(os.environ[variable] == "A-MADE-UP-LOGIN" for variable in logins)
    seen = {}
    monkeypatch.setattr(serve, "_run", lambda root, **options: seen.update(os.environ))  # the server, before anything else
    serve.run(tmp_path, port=8790, open_browser=False)
    assert seen and not logins & set(seen) and not logins & set(os.environ)
    assert seen["HYLLY_NTFY_URL"] == "https://ntfy.example.org/a-made-up-topic"


def test_windows_task_starts_the_server_with_the_settings_and_now(monkeypatch, tmp_path, capsys):
    """The task is written without options, whatever the running server was started with, so the next logon brings
    the settings' server back; and hylly server autostart on starts it now if none runs."""
    from hylly import cli

    registered, started = [], []
    monkeypatch.setattr(instance, "SYSTEM", "windows")
    monkeypatch.setattr(instance, "autostart_on", lambda dry_run=False: registered.append(dry_run))
    monkeypatch.setattr(instance, "running", lambda: None)
    monkeypatch.setattr(instance, "start_background", lambda: started.append(1) or {"pid": 2, "port": 8770})
    monkeypatch.setattr(serve, "describe", lambda info: "")
    monkeypatch.setenv("HYLLY_LIBRARY", str(tmp_path))
    cli.main(["server", "autostart", "on", "--no-browser"])
    assert registered == [False] and started == [1] and "with the settings" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cli.main(["server", "autostart", "on", "--port", "8790"])
    assert e.value.code == exits.USAGE  # no options there: the settings are the server's


def test_the_server_writes_its_own_log_and_keeps_it_short(monkeypatch):
    """Every line with its time, beyond the limit moved to server.log.1, and the one before that goes; whatever
    is said on standard error while it runs goes there too, a traceback as well, however it was started."""
    log = instance.log_file()
    monkeypatch.setattr(instance, "LOG_LIMIT", 200)
    instance.write_log("Library server started.")
    assert instance.STAMPED.match(log.read_text(encoding="utf-8")) and log.read_text(encoding="utf-8").endswith(" Library server started.\n")
    log.with_suffix(".log.1").write_text("the oldest\n", encoding="utf-8")
    log.write_text("x" * 300, encoding="utf-8")
    instance.write_log("A line after the limit.")
    assert "the oldest" not in log.with_suffix(".log.1").read_text(encoding="utf-8") and log.with_suffix(".log.1").stat().st_size == 300
    assert log.read_text(encoding="utf-8").endswith(" A line after the limit.\n") and log.stat().st_size < 100
    # two over the limit at once: the other has moved the big one already, and begun a new one, which stays, every line
    storage._take_aside(log, instance.LOG_LIMIT)
    assert log.read_text(encoding="utf-8").endswith(" A line after the limit.\n") and log.with_suffix(".log.1").stat().st_size == 300
    assert sorted(p.name for p in log.parent.glob("server.log*")) == ["server.log", "server.log.1"]  # nothing left aside
    log.unlink()
    with instance.logged():
        serve._stamp("Said by the server.")
        sys.stderr.write("Traceback (most recent call last):\n  File x\n")
        with instance.logged():  # once, however often it's asked
            serve._stamp("Once.")
        sys.stderr.write("ValueError: the last, without its line break")
    lines = log.read_text(encoding="utf-8").splitlines()
    assert [instance.STAMPED.sub("", line) for line in lines] == ["Said by the server.", "Traceback (most recent call last):",
                                                                    "  File x", "Once.", "ValueError: the last, without its line break"]
    assert all(instance.STAMPED.match(line) for line in lines) and not isinstance(sys.stderr, instance.Logged)
    log.unlink()
    with log.open("a", encoding="utf-8") as into_the_log:  # a service from before: its standard error is server.log itself
        monkeypatch.setattr(sys, "stderr", into_the_log)
        with instance.logged():
            serve._stamp("Said once.")
    assert [instance.STAMPED.sub("", line) for line in log.read_text(encoding="utf-8").splitlines()] == ["Said once."]


def test_a_hidden_server_that_fails_says_why_from_its_own_lines(monkeypatch):
    """The start in the background reads only what was written to the log since it began, without its time: an older
    line there is no reason for this one."""
    class Ended:
        returncode = exits.LOCAL

        def poll(self):
            return self.returncode

    instance.write_log("An old reason, from another start.")
    monkeypatch.setattr(instance, "spawn", lambda arguments, errors_to_log=False: Ended())
    with pytest.raises(SystemExit, match=r"did not start: it ended at once, without a word \(exit code 6\)"):
        instance.start_background()

    def failing(arguments, errors_to_log=False):
        assert arguments == [*instance.DAEMON, "--in-background"]  # the settings are its options
        instance.write_log("Port 8770 is taken by another program.")
        return Ended()

    monkeypatch.setattr(instance, "spawn", failing)
    with pytest.raises(SystemExit, match="did not start: Port 8770 is taken by another program.\n"):
        instance.start_background()


def test_a_browser_is_opened_only_where_there_is_a_desktop(monkeypatch):
    """On Windows and macOS, or with DISPLAY or WAYLAND_DISPLAY set; over ssh to a Linux server, never."""
    from hylly import cli

    for name in ("DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    for system in ("windows", "mac"):
        monkeypatch.setattr(instance, "SYSTEM", system)
        assert serve.a_desktop()
    monkeypatch.setattr(instance, "SYSTEM", "linux")
    assert not serve.a_desktop()
    for name in ("DISPLAY", "WAYLAND_DISPLAY"):
        with monkeypatch.context() as m:
            m.setenv(name, ":0" if name == "DISPLAY" else "wayland-0")
            assert serve.a_desktop()
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    monkeypatch.setattr(instance, "running", lambda: {"pid": 7, "port": 8770, "listen": "127.0.0.1"})
    monkeypatch.setattr(serve, "describe", lambda info: "")
    cli.main(["serve"])  # a server running, a Linux over ssh: said, and no browser
    assert opened == []
    monkeypatch.setattr(instance, "SYSTEM", "windows")
    cli.main(["serve"])
    assert opened == ["http://127.0.0.1:8770/"]


def test_the_launchers_start_and_stop_this_computer_s_own_server():
    """Never one that [remote] ssh or HYLLY_REMOTE would name: --here, before the command."""
    from pathlib import Path

    scripts = Path(__file__).parents[1] / "scripts"
    assert not (scripts / "Library for the home network.cmd").exists()  # retired: listen = "0.0.0.0" in the settings does it
    for name in ("Library.cmd", "Stop library server.cmd"):
        line = next(line for line in (scripts / name).read_text(encoding="utf-8").splitlines() if "hylly" in line and "uv run" in line)
        assert " hylly --here serv" in line, name


# ---- the walled service: printed from the settings, and read back as systemd reads it ----------------------------

SOMEONE = "/home/someone"  # the made-up user of examples/hylly.service
BY_KIND = {"config": f"{SOMEONE}/.config/hylly", "data": f"{SOMEONE}/.local/share/hylly", "state": f"{SOMEONE}/.local/state/hylly",
           "cache": f"{SOMEONE}/.cache/hylly", "logins": f"{SOMEONE}/.local/share/hylly/logins",
           "job-output": f"{SOMEONE}/.local/state/hylly/job-output"}


def made_up_unit(**changed) -> str:
    """The walled service for examples/hylly.service's made-up user, library, folders and program, as walled_unit makes it."""
    given = {"user": "someone", "group": "someone", "library": "/srv/books", "places": BY_KIND,
             "program": [f"{SOMEONE}/.local/share/uv/tools/hylly/bin/hyllyd"], "read_only": [f"{SOMEONE}/.local/share/uv/tools/hylly"],
             "pythons": f"{SOMEONE}/.local/share/uv/python", "port": 8770}
    return instance.walled_unit(**{**given, **changed})


def test_the_example_service_is_the_one_printed_for_its_made_up_user_and_names_its_port():
    """examples/hylly.service, below its header, is what hylly serve --service-file prints for its made-up user, library,
    folders and program: the one the doctor compares against, and the example, can't drift apart. Its header names the
    one port, the settings' [server] port, and says where it comes from."""
    from pathlib import Path

    text = (Path(__file__).parents[1] / "examples" / "hylly.service").read_text(encoding="utf-8")
    header, _, body = text.partition("[Unit]\n")
    assert "[Unit]\n" + body == made_up_unit()
    assert "\n".join(instance.ABOUT) in header and "hylly serve --service-file" in header and "hylly doctor" in header
    assert "8770" in header and "[server] port in the settings" in header and "SocketBindAllow" in header


def test_the_walled_service_takes_its_folders_and_port_from_what_it_s_given():
    unit = made_up_unit(user="ann", group="readers", library="/mnt/Lantern Wolf/100% books", port=8780,
                        environment={"HYLLY_LIBRARY": "/mnt/Lantern Wolf/100% books"}, cache_folder="/mnt/fast/pages")
    lines = unit.splitlines()
    assert "User=ann" in lines and "Group=readers" in lines and "SocketBindAllow=tcp:8780" in lines
    assert 'RequiresMountsFor="/mnt/Lantern Wolf/100%% books"' in lines and 'BindPaths="/mnt/Lantern Wolf/100%% books"' in lines
    assert 'TemporaryFileSystem="/mnt/Lantern Wolf":ro /run:ro' in lines  # its parent hidden, and /run
    assert 'Environment="HYLLY_LIBRARY=/mnt/Lantern Wolf/100%% books"' in lines and "BindPaths=-/mnt/fast/pages" in lines
    assert "BindPaths=-/mnt/fast/hylly-cache" not in unit
    at_home = made_up_unit(library=f"{SOMEONE}/Books").splitlines()  # ProtectHome hides its parent already
    assert "TemporaryFileSystem=/run:ro" in at_home and f"BindPaths={SOMEONE}/Books" in at_home


def test_all_in_one_folder_the_settings_own_files_in_it_are_walled_one_by_one():
    """The one folder is the server's to write, so the settings' own files in it are walled read-only each:
    settings.toml and secrets/ without a -, so that one not there stops the start rather than leave the server to make
    it; the rules and themes folders, which nothing makes for you, with one. The logins inside it are hidden without a -
    too; what long jobs said is outside it, hidden only where it's there."""
    one = f"{SOMEONE}/.local/share/Hylly"
    unit = made_up_unit(places={"config": one, "data": one, "state": one, "cache": f"{one}/cache", "logins": f"{one}/logins",
                                "job-output": f"{SOMEONE}/.local/state/hylly/job-output"})
    lines = unit.splitlines()
    assert f"BindPaths={one}" in lines and f"WorkingDirectory={one}" in lines  # the cache inside it with it
    assert f"InaccessiblePaths={one}/logins" in lines and f"InaccessiblePaths=-{SOMEONE}/.local/state/hylly/job-output" in lines
    assert f"ReadOnlyPaths={one}/settings.toml {one}/secrets -{one}/rules -{one}/themes" in lines  # nothing makes those
    assert not any(line.startswith("BindReadOnlyPaths=" + one) for line in lines)


@pytest.mark.parametrize("odd", ["/srv/books: two", '/srv/"books"', "/srv/books\\x", "/srv/books\nUser=root", "books"])
def test_a_folder_a_unit_can_t_hold_as_it_is_is_refused_before_anything_is_printed(odd):
    with pytest.raises(SystemExit) as e:
        made_up_unit(library=odd)
    assert e.value.code == exits.USAGE and "Nothing was printed" in str(e.value)


def test_a_unit_s_lines_are_read_as_systemd_reads_them():
    text = ("# a comment\n[Service]\n; another\nUser = someone\nBindPaths=/srv/books \\\n  /srv/more\n"
            "SocketBindAllow=\n[Install]\nWantedBy=multi-user.target\n")
    assert instance._unit_lines(text) == [("Service", "User", "someone"), ("Service", "BindPaths", "/srv/books  /srv/more"),
                                          ("Service", "SocketBindAllow", ""), ("Install", "WantedBy", "multi-user.target")]
    assert instance._listed_words('"/srv/my books" -/a\\ b /c', colons=True) == [["/srv/my books"], ["-/a b"], ["/c"]]
    assert instance._listed_words("/mnt/disk/books:/srv/books:rbind", colons=True) == [["/mnt/disk/books", "/srv/books", "rbind"]]
    assert instance._listed_words('"/srv/a:b"', colons=True) == [["/srv/a:b"]]  # a colon in quotes is the path's own
    assert instance._listed_words('"/srv/100%% books"') == [["/srv/100% books"]]
    assert instance._listed_words("%h/Books") is None and instance._listed_words('"/srv/open') is None  # can't be told here


@pytest.mark.parametrize("allow, deny, family, port, allowed", [
    (["tcp:8770"], ["any"], "ipv4", 8770, True),
    (["tcp:8770"], ["any"], "ipv4", 8780, False),
    (["tcp:8700-8799"], ["any"], "ipv4", 8780, True),  # a range
    (["8780"], ["any"], "ipv6", 8780, True),  # any protocol, any family
    (["ipv6:tcp:8780"], ["any"], "ipv4", 8780, False),  # [server] listen an IPv4 address
    (["udp:8780"], ["any"], "ipv4", 8780, False),
    (["tcp:8770"], [], "ipv4", 8780, True),  # nothing denied: allowed
    ([], ["tcp:8000-9000"], "ipv4", 8780, False),
    (["tcp:87x0"], ["any"], "ipv4", 8780, None),  # one systemd wouldn't take: can't be told
])
def test_a_port_is_allowed_as_systemd_allows_it(allow, deny, family, port, allowed):
    assert instance._port_allowed(allow, deny, family, port) is allowed


def test_what_the_walls_leave_of_a_path_goes_by_the_deepest_wall_that_names_it(tmp_path):
    """Shortest path first, as systemd mounts them: a bind of the library, or of a folder it's in, shows it again over
    the parent a TemporaryFileSystem hid; at one path the more restrictive wins; ProtectHome hides the home folders, by
    any of systemd's words for yes; a path is the same through a link."""
    unit = tmp_path / "hylly.service"

    def found(text: str) -> dict:
        unit.write_text("[Service]\n" + text, encoding="utf-8")
        return instance._walled_reading([unit])[0]

    walls = "ProtectSystem=strict\nProtectHome=tmpfs\nTemporaryFileSystem=/srv:ro /run:ro\n"
    assert instance._what_shows("/srv/books", found(walls + "BindPaths=/srv/books\n"))[0] == "writable"
    assert instance._what_shows("/mnt/books/comics", found(walls + "BindPaths=/mnt\n"))[0] == "writable"  # a folder it's in
    assert instance._what_shows("/srv/books", found(walls + "BindPaths=/srv/old-books\n")) == (
        "hidden", ("/srv:ro /run:ro", str(unit), "TemporaryFileSystem=/srv:ro /run:ro"))
    assert instance._what_shows("/srv/books", found(walls + "BindReadOnlyPaths=/srv/books\n"))[0] == "read-only"
    assert instance._what_shows("/srv/books", found(walls + "BindPaths=/srv/books\nInaccessiblePaths=/srv/books\n"))[0] == "hidden"
    assert instance._what_shows("/srv/books", found(walls + "BindPaths=/srv/books\nReadOnlyPaths=/srv/books\n"))[0] == "read-only"
    assert instance._what_shows("/srv/books", found(walls + "BindReadOnlyPaths=/srv/books\nBindPaths=\nBindPaths=/srv/books\n"))[0] \
        == "writable"  # BindPaths= and BindReadOnlyPaths= are one list: an empty one clears both
    assert instance._what_shows("/home/ann/Books", found(walls))[1][2] == "ProtectHome=tmpfs"
    assert instance._what_shows("/home/ann/Books", found("ProtectHome=y\n"))[0] == "hidden"  # systemd's yes, as y
    assert instance._what_shows("/usr/share/books", found("ProtectSystem=t\n"))[0] == "read-only"
    assert instance._what_shows("/mnt/books", found(walls))[0] == "read-only"  # ProtectSystem=strict alone
    assert instance._what_shows("/mnt/books", found(walls + "ReadWritePaths=-/mnt\n"))[0] == "writable"
    assert instance._what_shows("/mnt/books", found("")) == ("writable", None)  # no walls at all
    assert instance._what_shows("/srv/books", found(walls + "BindPaths=/mnt/disk/books:/srv/books:rbind\n"))[0] == "writable"
    real, link = tmp_path / "disk" / "books", tmp_path / "books"
    real.mkdir(parents=True)
    link.symlink_to(real)
    assert instance._what_shows(str(link), found(f"TemporaryFileSystem={tmp_path}:ro\nBindPaths={real}\n"))[0] == "writable"
    assert instance._what_shows("/srv/books", found(walls + "BindPaths=%h/books\n")) is None  # a specifier: can't be told


def test_a_value_a_unit_can_t_hold_is_refused_and_the_working_folder_is_written_without_quotes():
    with pytest.raises(SystemExit, match="line break") as e:
        made_up_unit(environment={"HYLLY_LIBRARY": "/srv/books\nUser=root"})
    assert e.value.code == exits.USAGE
    with pytest.raises(SystemExit, match="can't go in a systemd unit"):
        made_up_unit(places={**BY_KIND, "data": '/home/someone/"data"'})
    spaced = made_up_unit(places={**BY_KIND, "data": "/home/someone/Lantern Wolf", "logins": "/home/someone/Lantern Wolf/logins"})
    assert "WorkingDirectory=/home/someone/Lantern Wolf" in spaced.splitlines()  # one value: quotes would be its own
