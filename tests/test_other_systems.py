"""Linux and macOS: what differs from Windows, as far as it can be tried on any computer. The real thing is tried on a real
one too, with no fakes."""

from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import conftest
from hylly import archives, cli, daemon, dashboard, download, helppage, instance, logins, notify, paths, serve, settings
from hylly.logins import AuthError

from helpers import workflow

REAL_USUAL_FOLDER, REAL_JOB_OUTPUT = paths.usual_settings_folder, paths.job_output_dir  # usual_folder and conftest stand in
REAL_SAME_FOLDER = paths._same_folder  # which made_up_home stands in for, as Linux's disks have it
REAL_LOGON_FOUND = instance.logon_found  # and for this one, which looks at this computer's own start at logon
REAL_SERVICE_DIFFERENCES = instance.service_differences  # and its walled service, against the settings
REAL_SEVENZIP = archives.sevenzip  # and this, which would find this computer's 7-Zip
ARGS = ["-m", "hylly.daemon", "--in-background", "/books/My 100% $Library"]


def test_the_systemd_service_runs_the_server_itself_and_keeps_odd_names_whole():
    text = instance.unit_text("/home/a/.venv/bin/python", ARGS, "/home/a/hylly", {"HYLLY_LIBRARY": '/books/"x"'})
    assert '"/books/My 100%% $$Library"' in text  # systemd's own % and $ stay out of it
    assert 'Environment="HYLLY_LIBRARY=/books/\\"x\\""' in text
    dollars = instance.unit_text("/py", [], "/a", {"HYLLY_LIBRARY": "/books/$x 5%"})
    assert 'Environment="HYLLY_LIBRARY=/books/$x 5%%"' in dollars  # no $ to double there: it would reach the server doubled
    assert '"--in-background"' in text and '"--background"' not in text  # or systemd would think it had ended
    assert "Restart=on-failure" in text and "WantedBy=default.target" in text and "StartLimitBurst=5" in text
    assert "Standard" not in text  # its messages to the journal; the server writes server.log itself
    assert "WorkingDirectory=/home/a/hylly\n" in text  # no quotes: systemd refuses those there


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd's own check")
def test_systemd_itself_accepts_the_service(tmp_path):
    folder = tmp_path / "My 100% Folder"
    folder.mkdir()
    unit = tmp_path / "hylly.service"
    unit.write_text(instance.unit_text(sys.executable, ARGS, str(folder), {"HYLLY_LIBRARY": '/books/"x" $y 5%'}))
    done = subprocess.run(["systemd-analyze", "--user", "verify", str(unit)], capture_output=True, text=True)
    assert done.returncode == 0 and "hylly.service" not in done.stderr, done.stderr


def fake_systemd(monkeypatch, tmp_path, linger="no", main_pid="0"):
    """systemd made up, beneath instance's real ways of asking it: a test that uses this takes usual_folder."""
    calls = []

    def run(command, **k):
        calls.append(command)
        out = linger if command[0] == "loginctl" else main_pid if "MainPID" in command else ""
        return subprocess.CompletedProcess(command, 0, out + "\n", "")

    monkeypatch.setattr(instance, "SYSTEM", "linux")
    monkeypatch.setattr(instance.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(instance.subprocess, "run", run)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return calls


def test_the_start_at_logon_on_linux_is_printed_with_this_computer_s_paths_and_nothing_installed(monkeypatch, tmp_path, capsys, usual_folder):
    """The system runs the server itself, from a systemd user unit; hylly prints it, its paths filled in, and the
    commands that install it, and asks systemd nothing. Its output, as it is, is the file."""
    calls = fake_systemd(monkeypatch, tmp_path)
    home = tmp_path / "home" / "someone" / ".local" / "share" / "Hylly"  # a folder of its own: Linux makes no /home/someone
    monkeypatch.setenv("HYLLY_HOME", str(home))
    hylly = tmp_path / "a checkout" / ".venv" / "bin" / "hylly"  # the one running: uv run's, say
    hylly.parent.mkdir(parents=True)
    hylly.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(hylly), "server", "autostart", "on"])
    cli.main(["server", "autostart", "on"])
    said = capsys.readouterr()
    unit = tmp_path / "systemd" / "user" / "hylly.service"
    assert not unit.exists() and not any(command[0] == "systemctl" for command in calls)
    assert instance._unit_words(said.out) == [instance._python(), "-P", "-m", "hylly.daemon", "--in-background"]
    # in the home folder, not where the program is, which an update or an uninstall takes away
    assert f"WorkingDirectory={Path.home()}" in said.out and "a checkout" not in said.out.split("ExecStart=")[1].split("\n", 1)[1]
    assert 'Environment="HYLLY_HOME=' + str(home).replace('\\', '\\\\') + '"' in said.out  # as systemd quotes it
    assert said.out.startswith("# Hylly's start at logon for this computer") and "Standard" not in said.out
    me, new = shlex.quote(str(hylly)), shlex.quote(f"{unit}.new")  # the steps work as typed: this hylly, by its full path
    for step in (f"{me} server stop", f"{me} server autostart on > {new} && mv {new} {shlex.quote(str(unit))}",
                 "systemctl --user daemon-reload", "systemctl --user enable --now hylly.service"):
        assert f"#   {step}" in said.out and f"  {step}" in said.err  # the file only when it went right: never an empty one
    assert "Nothing was installed" in said.err and "loginctl enable-linger" in said.err  # yours to choose: it goes for all your services
    unit.parent.mkdir(parents=True)
    unit.write_text(said.out, encoding="utf-8")
    cli.main(["server", "autostart", "off"])
    said = capsys.readouterr()
    assert unit.exists() and not any(command[0] == "systemctl" for command in calls)  # said, not done
    assert "systemctl --user disable --now hylly.service" in said.out and f"rm {shlex.quote(str(unit))}" in said.out
    cli.main(["server", "autostart", "on", "--dry-run"])
    assert "Nothing is installed either way" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["-m"])  # python -m hylly: the steps say so
    assert instance.this_hylly() == f"{shlex.quote(instance._python())} -P -m hylly"
    monkeypatch.setattr(instance, "system_service", lambda: True)  # refused: nothing at all on standard output
    with pytest.raises(SystemExit):
        cli.main(["server", "autostart", "on"])
    assert capsys.readouterr().out == ""


def test_it_says_where_lingering_stands_and_leaves_it_to_you(monkeypatch, tmp_path, usual_folder):
    fake_systemd(monkeypatch, tmp_path, linger="no")
    monkeypatch.setattr(instance.getpass, "getuser", lambda: "someone")
    assert "loginctl enable-linger someone" in instance.logon_start()[2]
    assert instance.after_autostart_off() is None
    fake_systemd(monkeypatch, tmp_path, linger="yes")
    assert "starts with the computer" in instance.logon_start()[2]
    assert "loginctl disable-linger" in instance.logon_stop()[1]


def test_a_server_the_system_started_is_restarted_through_the_system(monkeypatch, tmp_path, usual_folder):
    calls = fake_systemd(monkeypatch, tmp_path, main_pid="4242")
    assert instance.system_owns(4242) and not instance.system_owns(99) and not instance.system_owns(None)
    instance.restart_through_system()
    assert ["systemctl", "--user", "--no-block", "restart", "hylly.service"] in calls


def test_the_server_page_leaves_the_restart_to_the_system_that_started_it(monkeypatch):
    done, spawned = threading.Event(), []
    monkeypatch.setattr(dashboard.instance, "spawn", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(dashboard.instance, "restart_through_system", done.set)
    monkeypatch.setattr(dashboard.instance, "system_owns", lambda pid: True)
    dashboard.restart_later()
    assert done.wait(5) and not spawned
    monkeypatch.setattr(dashboard.instance, "system_owns", lambda pid: False)
    dashboard.restart_later()
    assert spawned


def test_the_restart_command_goes_through_the_system_too(monkeypatch, capsys):
    notes = iter([{"pid": 7, "port": 8770}, {"pid": 8, "port": 8770}])
    restarted = []
    monkeypatch.setattr(instance, "running", lambda: next(notes))
    monkeypatch.setattr(instance, "system_owns", lambda pid: pid == 7)
    monkeypatch.setattr(instance, "restart_through_system", lambda: restarted.append(1))
    monkeypatch.setattr(instance, "stop", lambda: pytest.fail("the system does this"))
    monkeypatch.setattr(serve, "describe", lambda info: f"process {info['pid']}")
    cli.main(["server", "restart", "--no-browser"])
    assert restarted and "process 8" in capsys.readouterr().out


def test_a_server_the_system_starts_takes_the_settings(monkeypatch, tmp_path):
    """No options are passed between processes, nor remembered: hyllyd --in-background reads the settings, and a
    service set up before, which still passes --no-browser, starts all the same."""
    seen = {}
    monkeypatch.setattr(serve, "run", lambda root, **k: seen.update(k, root=root))
    paths.settings_file().write_text(f"library = '{tmp_path.as_posix()}'\n[server]\nport = 8791\n", encoding="utf-8")
    daemon.main(["--in-background", "--no-browser"])
    assert seen["port"] == 8791 and seen["root"] == tmp_path and seen["given"] == {} and seen["background"] and not seen["open_browser"]


def test_a_second_server_started_by_the_system_isn_t_a_failure_to_retry(monkeypatch, tmp_path):
    def taken():
        raise instance.AlreadyRunning("A library server is already running.")

    monkeypatch.setattr(instance, "Claim", taken)
    with pytest.raises(SystemExit) as e:
        serve.run(tmp_path, port=8791, background=True)
    assert e.value.code == 0
    with pytest.raises(SystemExit, match="already running"):
        serve.run(tmp_path, port=8791)


def test_a_linux_without_systemd_has_no_start_at_logon(monkeypatch, usual_folder):
    monkeypatch.setattr(instance, "SYSTEM", "linux")
    monkeypatch.setattr(instance.shutil, "which", lambda name: None)
    assert instance.autostart_state() is None
    with pytest.raises(SystemExit, match="no systemd") as e:
        instance.logon_start()
    assert e.value.code == 2  # it can't be done here: a usage error, not partly done


def test_the_launchd_agent_is_printed_and_runs_the_server_itself(monkeypatch, tmp_path, capsys, usual_folder):
    """As the systemd unit: the agent with this computer's paths, a comment at its top that says how to install
    it, and nothing installed. launchd reads it, comment and all."""
    calls = []
    monkeypatch.setattr(instance, "SYSTEM", "mac")
    monkeypatch.setattr(instance.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(instance, "_gui", lambda: "gui/501")
    monkeypatch.setattr(instance.subprocess, "run", lambda command, **k: calls.append(command) or subprocess.CompletedProcess(command, 0, "", ""))
    assert instance.autostart_state() is False
    cli.main(["server", "autostart", "on"])
    said = capsys.readouterr()
    path = tmp_path / "Library" / "LaunchAgents" / "io.github.janisc.hylly.plist"
    assert not path.exists() and not calls
    agent = plistlib.loads(said.out.encode("utf-8"))
    assert agent["RunAtLoad"] and agent["KeepAlive"] == {"SuccessfulExit": False} and agent["Label"] == instance.AGENT
    assert agent["ProgramArguments"] == [instance._python(), "-P", "-m", "hylly.daemon", "--in-background"]
    assert agent["WorkingDirectory"] == str(Path.home()) and "StandardOutPath" not in agent and "StandardErrorPath" not in agent
    step = f"launchctl bootstrap gui/501 {shlex.quote(str(path))}"
    assert step.replace("--", "- -") in said.out and step in said.err  # in the comment, and for the person at the terminal
    assert "Python may run in the background" in said.err
    cli.main(["server", "autostart", "off"])
    assert f"launchctl bootout gui/501/{instance.AGENT}" in capsys.readouterr().out and not calls


def test_a_notification_takes_its_texts_as_arguments_on_each_system():
    sneaky = 'x" & do shell script "touch /tmp/owned'
    mac = notify.command("A title", sneaky, "darwin", notifier="")
    assert mac[0] == "osascript" and mac[-2:] == ["A title", sneaky] and not any(sneaky in part for part in mac[:-1])
    linux = notify.command("-A title", sneaky, "linux")
    assert linux[:3] == ["notify-send", "--app-name=Hylly", "--"] and linux[-2] == "-A title"
    assert notify.command("t", "<a href='x'>y</a> & z", "linux")[-1] == "&lt;a href='x'&gt;y&lt;/a&gt; &amp; z"  # read as markup there
    assert notify.command("t", "x", "win32")[0].endswith(r"\System32\WindowsPowerShell\v1.0\powershell.exe")
    assert sneaky not in " ".join(notify.command("t", sneaky, "win32"))  # there it goes in the environment


def test_windows_own_programs_are_started_by_their_full_path_never_by_their_name(monkeypatch, usual_folder):
    """By its name alone, Windows looks for a program in the current folder first, where anyone who can write it could
    have put one of the same name."""
    monkeypatch.setenv("SystemRoot", r"D:\Win")
    powershell, schtasks = r"D:\Win\System32\WindowsPowerShell\v1.0\powershell.exe", r"D:\Win\System32\schtasks.exe"
    assert paths.windows_tool("powershell") == powershell and paths.windows_tool("schtasks") == schtasks
    assert notify.command("t", "x", "win32")[0] == powershell and logins.clipboards("win32")[0][0] == powershell
    ran = []
    monkeypatch.setattr(instance.subprocess, "run", lambda command, **k: ran.append(command) or subprocess.CompletedProcess(command, 0, "", ""))
    instance._schtasks("/Query", "/TN", instance.TASK)
    assert ran == [[schtasks, "/Query", "/TN", instance.TASK]]
    monkeypatch.setenv("SystemRoot", "Windows")  # not a whole path: it would lead back to the current folder
    assert paths.windows_tool("schtasks") == r"C:\Windows\System32\schtasks.exe"
    monkeypatch.delenv("SystemRoot")
    assert paths.windows_tool("schtasks") == r"C:\Windows\System32\schtasks.exe"


def test_every_system_tool_is_run_one_way_its_words_read_as_text_whatever_they_hold(monkeypatch, usual_folder):
    """Task Scheduler's, systemd's (the user's and the computer's), launchd's and loginctl's tools all go through one
    runner (instance._tool): what each says is read as text, with what isn't text in it replaced, and its exit code is
    the caller's to look at; launchctl's own options go along."""
    ran = []
    monkeypatch.setattr(instance.subprocess, "run", lambda command, **k: ran.append((command, k)) or subprocess.CompletedProcess(command, 0, "yes\n", ""))
    monkeypatch.setattr(instance.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(instance.getpass, "getuser", lambda: "someone")
    instance._schtasks("/Query")
    instance._systemctl("is-enabled", instance.UNIT)
    instance._systemctl_wide("is-active", "hylly")
    instance._launchctl("kickstart", "-k", "gui/501/x", start_new_session=True)
    assert instance._lingering()
    usual = {"capture_output": True, "text": True, "errors": "replace"}
    assert ran == [([paths.windows_tool("schtasks"), "/Query"], usual), (["systemctl", "--user", "is-enabled", instance.UNIT], usual),
                   (["systemctl", "is-active", "hylly"], usual), (["launchctl", "kickstart", "-k", "gui/501/x"], {**usual, "start_new_session": True}),
                   (["loginctl", "show-user", "someone", "-p", "Linger", "--value"], usual)]


def test_a_notification_that_can_t_be_shown_is_only_a_no(monkeypatch):
    monkeypatch.delenv("HYLLY_NO_NOTIFY")

    def missing(*a, **k):
        raise FileNotFoundError(2, "no such program")

    monkeypatch.setattr(notify.subprocess, "run", missing)
    assert notify.toast("A title", "text") is False


@pytest.mark.parametrize(("platform", "tool"), [("darwin", "pbpaste"), ("linux", "xclip"), ("win32", r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")])
def test_the_clipboard_is_read_with_what_the_system_has(monkeypatch, platform, tool):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")

    def run(command, **k):
        if command[0] != tool:
            raise FileNotFoundError(2, "no such program")
        return subprocess.CompletedProcess(command, 0, "the value", "")

    monkeypatch.setattr(logins.subprocess, "run", run)
    assert logins.read_clipboard(platform) == "the value"


def test_a_clipboard_that_isn_t_text_is_refused_later_not_a_crash(monkeypatch):
    seen = {}

    def run(command, **k):
        seen.update(k)
        return subprocess.CompletedProcess(command, 0, "\ufffd\ufffd", "")

    monkeypatch.setattr(logins.subprocess, "run", run)
    assert not logins.looks_like_cookie(logins.read_clipboard("darwin"), "humble") and seen["errors"] == "replace"


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_no_clipboard_says_what_to_do_instead(monkeypatch, platform):
    def run(command, **k):  # as a Windows without PowerShell where it belongs answers too
        raise FileNotFoundError(2, "no such program")

    monkeypatch.setattr(logins.subprocess, "run", run)
    with pytest.raises(AuthError, match="paste instead"):
        logins.read_clipboard(platform)


def test_a_program_that_refuses_isn_t_taken_for_an_empty_clipboard(monkeypatch):
    monkeypatch.setattr(logins.subprocess, "run", lambda command, **k: subprocess.CompletedProcess(command, 1, "", "no display"))
    with pytest.raises(AuthError):
        logins.read_clipboard("linux")


@pytest.mark.parametrize(("platform", "tool"), [("darwin", "caffeinate"), ("linux", "systemd-inhibit")])
def test_a_program_keeps_the_computer_awake_and_ends_with_the_run(monkeypatch, platform, tool):
    started, ended = [], []

    class Program:
        def __init__(self, command, **k):
            started.append(command)

        def poll(self):
            return None

        def terminate(self):
            ended.append(1)

    monkeypatch.setattr(download, "_awake", None)
    monkeypatch.setattr(download.subprocess, "Popen", Program)
    monkeypatch.setattr(download, "_without_asking", lambda: [])
    download.keep_awake(True, platform)
    download.keep_awake(True, platform)  # one is enough
    assert len(started) == 1 and started[0][0] == tool and any(str(download.os.getpid()) in part for part in started[0])
    download.keep_awake(False, platform)
    assert ended == [1] and download._awake is None
    download.keep_awake(False, platform)  # nothing to end


def test_over_ssh_or_from_a_service_it_settles_for_holding_off_idling(monkeypatch):
    started = []

    class Program:
        def __init__(self, command, **k):
            started.append(command)
            self.refused = "--what=sleep:idle" in command

        def poll(self):
            return 1 if self.refused else None

    monkeypatch.setattr(download, "_awake", None)
    monkeypatch.setattr(download.subprocess, "Popen", Program)
    monkeypatch.setattr(download.time, "sleep", lambda s: None)
    monkeypatch.setattr(download, "_without_asking", lambda: [])
    download.keep_awake(True, "linux")
    assert [c[1] for c in started] == ["--what=sleep:idle", "--what=idle"] and download._awake is not None
    monkeypatch.setattr(download, "_awake", None)


def test_keeping_awake_never_asks_for_a_password_where_systemd_inhibit_can_be_told_so(monkeypatch):
    started = []

    class Program:
        def __init__(self, command, **k):
            started.append(command)

        def poll(self):
            return None

        def terminate(self):
            pass

    for help_text, flag in (("  --no-ask-password Do not prompt for password\n", True), ("  --what=WHAT\n", False)):
        started.clear()
        monkeypatch.setattr(download, "_awake", None)
        monkeypatch.setattr(download.subprocess, "Popen", Program)
        monkeypatch.setattr(download.subprocess, "run", lambda *a, help_text=help_text, **k: subprocess.CompletedProcess(a, 0, help_text, ""))
        download.keep_awake(True, "linux")
        (command,) = started
        assert ("--no-ask-password" in command) is flag and command.index("tail") > command.index("--why=A long job is running")
        download.keep_awake(False, "linux")


def test_keeping_awake_without_the_program_is_no_reason_to_stop(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError(2, "no such program")

    monkeypatch.setattr(download, "_awake", None)
    monkeypatch.setattr(download.subprocess, "Popen", missing)
    download.keep_awake(True, "linux")
    assert download._awake is None


def test_the_help_writes_folders_with_this_system_s_slash():
    text = r"<p>not C:\this</p><code>state\lookups\x</code><pre>uv run hylly # catalog\index.html</pre>"
    assert helppage.for_this_system(text, windows=True) == text
    assert helppage.for_this_system(text, windows=False) == r"<p>not C:\this</p><code>state/lookups/x</code><pre>uv run hylly # catalog/index.html</pre>"
    for only_windows in (r"<code>%LOCALAPPDATA%\Hylly</code>", r"<code>C:\Program Files\Tailscale\tailscale.exe</code>",
                         r'<pre>$env:HYLLY_LIBRARY = "E:\Library"</pre>'):
        assert helppage.for_this_system(only_windows, windows=False) == only_windows


def test_the_workflow_reader_takes_what_github_does(tmp_path):
    """Comments after a value, a value on the lines below (run: |, and > folded), quoted words with a # in them: read
    as GitHub reads them, so that a workflow written another way fails no test that holds it to the code."""
    flows = tmp_path / ".github" / "workflows"
    flows.mkdir(parents=True)
    (flows / "made-up.yml").write_text("""name: made-up  # a comment after a value
on:
  workflow_dispatch:
jobs:
  one:
    runs-on: ubuntu-latest
    steps:
      - run: |
          uv sync --locked
          # a comment of the shell's, in the script
          uv run python -m pytest -q

      - name: "a #hash in quotes"
        run: >
          echo one
          echo two
    timeout-minutes: 30  # an hour is too long
""", encoding="utf-8")
    read = workflow("made-up.yml", tmp_path)
    assert read["name"] == "made-up" and read["on"] == {"workflow_dispatch": None}
    steps = read["jobs"]["one"]["steps"]
    assert steps[0] == {"run": "uv sync --locked\n# a comment of the shell's, in the script\nuv run python -m pytest -q"}
    assert steps[1] == {"name": "a #hash in quotes", "run": "echo one echo two"}
    assert read["jobs"]["one"]["timeout-minutes"] == "30"


def test_there_is_a_test_run_for_each_system():
    """The whole suite on Linux, Windows and macOS, as GitHub reads the file: on every push and pull request, and by
    hand if wanted, never a quick part of it: no marker or test chosen, however written."""
    tests = workflow("tests.yml")
    assert set(tests["on"]) in ({"push", "pull_request"}, {"push", "pull_request", "workflow_dispatch"})
    job = tests["jobs"]["tests"]
    assert set(job["strategy"]["matrix"]["os"]) == {"ubuntu-latest", "windows-latest", "macos-latest"}
    assert job["runs-on"] == "${{ matrix.os }}"
    (run,) = [step["run"] for step in job["steps"] if "pytest" in step.get("run", "")]
    assert "slow" not in json.dumps(tests)  # -m "not slow" as -m'not slow', -m="not slow", or in an environment
    assert not re.search(r"\s(-[mk]|--markexpr|--keyword|--deselect|--ignore)", run.partition("pytest")[2])  # all of them


def test_the_test_runs_take_the_oldest_python_hylly_runs_on_and_the_newest():
    """requires-python's floor on Linux, and the newest Python updates.PYTHONS knows on every system, each named to uv:
    a break at either end shows in the tests, not first on a user's computer."""
    import tomllib

    from hylly import updates

    root = Path(__file__).parents[1]
    job = workflow("tests.yml")["jobs"]["tests"]
    floor = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["requires-python"]
    oldest, newest = (".".join(map(str, end(updates.PYTHONS))) for end in (min, max))
    assert re.fullmatch(r">=\s*(\d+\.\d+)", floor)[1] == oldest
    assert job["strategy"]["matrix"]["python"] == [newest]
    assert job["strategy"]["matrix"]["include"] == [{"os": "ubuntu-latest", "python": oldest}]
    assert "uv sync --locked --python ${{ matrix.python }}" in [step.get("run") for step in job["steps"]]


def test_a_test_run_that_hangs_says_where_and_ends():
    """A test still going after faulthandler_timeout seconds has every thread's stack written out (pytest's own, no
    dependency), and the run ends at timeout-minutes, well before GitHub's six hours, with time for the stack first."""
    import tomllib

    root = Path(__file__).parents[1]
    options = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
    minutes = int(workflow("tests.yml")["jobs"]["tests"]["timeout-minutes"])
    assert 0 < int(options["faulthandler_timeout"]) <= minutes * 60 / 4 and minutes <= 120


# ---- a system-wide service, with walls around the server ------------------------------------------------------

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="its signal is for Linux and macOS")
# A systemd unit's paths are Linux's: Windows' own, with a drive and backslashes, can't stand in for them
linux_paths = pytest.mark.skipif(os.name == "nt", reason="a systemd unit's paths are Linux's")


def wide_systemd(monkeypatch, tmp_path, service="[Service]\nUser=me\n", main_pid="4242", enabled=True, answers=True):
    """The system's own service manager: a service file, and what it says when asked, if it answers at all, beneath
    instance's real ways of asking it: a test that uses this takes usual_folder."""
    calls = []

    def run(command, **k):
        calls.append(command)
        out = main_pid if answers and "MainPID" in command and "--user" not in command else ""
        code = 0 if answers and (enabled or "is-enabled" not in command) else 1
        return subprocess.CompletedProcess(command, code, out + "\n", "")

    (tmp_path / "units").mkdir(exist_ok=True)
    file = tmp_path / "units" / "hylly.service"
    file.unlink(missing_ok=True)
    if service is not None:
        file.write_text(service, encoding="utf-8")
    monkeypatch.setattr(instance, "UNIT_FOLDERS", (str(tmp_path / "units"),))
    monkeypatch.setattr(instance, "SYSTEM", "linux")
    monkeypatch.setattr(instance, "_me", lambda: "me")
    monkeypatch.setattr(instance.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(instance.subprocess, "run", run)
    monkeypatch.setattr(instance.paths, "usual_settings_folder", lambda settings_file_counts=True: True)
    return calls


def test_a_system_service_is_told_by_its_file_with_nothing_to_ask(monkeypatch, tmp_path, usual_folder):
    calls = wide_systemd(monkeypatch, tmp_path)
    assert instance.system_service() and not instance.in_system_service() and not calls
    wide_systemd(monkeypatch, tmp_path, service=None)
    assert not instance.system_service()


def test_a_masked_or_silent_service_still_counts(monkeypatch, tmp_path, usual_folder):
    wide_systemd(monkeypatch, tmp_path, service="")  # a masked one reads as empty: /dev/null
    assert instance.system_service()
    wide_systemd(monkeypatch, tmp_path, service="[Service]\nExecStart=/bin/true\n")
    assert instance.system_service()


def test_a_service_that_can_t_be_read_still_counts(monkeypatch, tmp_path, usual_folder):
    wide_systemd(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(PermissionError("no")))
    assert instance.system_service()


def test_another_user_s_service_says_nothing_about_yours(monkeypatch, tmp_path, usual_folder):
    wide_systemd(monkeypatch, tmp_path, service="[Service]\nUser=someone\n")
    assert not instance.system_service()


def test_settings_of_your_own_folder_are_no_way_around_it(monkeypatch, tmp_path, usual_folder):
    wide_systemd(monkeypatch, tmp_path)
    monkeypatch.setattr(instance.paths, "usual_settings_folder", REAL_USUAL_FOLDER)
    home = tmp_path / "home"
    usual = home / "AppData" / "Local" if sys.platform == "win32" else home / ".local" / "share"  # where this system has it
    (usual / "Hylly").mkdir(parents=True)
    monkeypatch.setattr(instance.Path, "home", lambda: home)
    monkeypatch.setenv("HYLLY_HOME", str(usual / ".." / usual.name / "Hylly"))  # the same place, written otherwise
    monkeypatch.setenv("HYLLY_SETTINGS", str(tmp_path / "elsewhere.toml"))  # its lock and note stay in that folder
    assert instance.system_service()
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "a test's own"))  # a test's or an assistant's: another server altogether
    assert not instance.system_service()


def test_a_hylly_with_a_folder_of_its_own_has_no_say(monkeypatch, tmp_path, usual_folder):
    wide_systemd(monkeypatch, tmp_path)
    monkeypatch.setattr(instance.paths, "usual_settings_folder", lambda settings_file_counts=True: False)
    assert not instance.system_service()


def test_what_jobs_from_elsewhere_say_goes_where_the_system_keeps_it_whatever_localappdata_says(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "Hylly"))
    for windows_own in (str(tmp_path / "appdata"), None):
        if windows_own:
            monkeypatch.setenv("LOCALAPPDATA", windows_own)
        else:
            monkeypatch.delenv("LOCALAPPDATA")
        if sys.platform == "win32":
            assert REAL_JOB_OUTPUT() == tmp_path / "Hylly-jobs"  # beside the settings folder, as ever
        else:  # where the walls keep a walled server from it, set for a test or not
            assert REAL_JOB_OUTPUT() == tmp_path / "home" / ".local" / "state" / "hylly" / "job-output"


# ---- where Hylly keeps its own files, by kind (paths.PLACES) ------------------------------------------------------------

KINDS = ("config", "data", "state", "cache", "logins")


def made_up_home(monkeypatch, tmp_path, system: str, by_spelling: bool = True, **environment: str) -> Path:
    """A home folder of its own on the system named, with none of Hylly's variables, nor the XDG folders or Windows'
    AppData variables, but those given: as a person's who never set one. by_spelling: folders told apart by their names'
    spelling, as on a disk that tells capitals from small letters, as Linux's do, whatever this computer's disk does
    (a Mac's takes ~/.local/share/hylly for ~/.local/share/Hylly)."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "SYSTEM", system)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(paths, "job_output_dir", REAL_JOB_OUTPUT)
    for name in ("HYLLY_HOME", "HYLLY_SETTINGS", *conftest.PLACE_VARIABLES, *paths.XDG, *paths.APPDATA):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(paths, "_same_folder", (lambda one, other: str(one) == str(other)) if by_spelling else REAL_SAME_FOLDER)
    paths.forget_one_folder()  # decided once for each process: afresh for each made-up home
    return home


def where(kind: str) -> tuple[Path, str]:
    found = {p["kind"]: (Path(p["folder"]), p["from"]) for p in paths.places()}
    assert [p["kind"] for p in paths.places()] == list(KINDS) and all(paths.place(k) == found[k][0] for k in KINDS)
    return found[kind]


def test_a_new_install_keeps_each_kind_in_its_own_folder_where_each_system_keeps_it(monkeypatch, tmp_path):
    """Linux: the XDG folders, as their variables set them if they're full paths; macOS: Application Support, with the
    state in it, and Caches; Windows: the roaming AppData for the settings, the local one for the rest, each in a folder
    inside Hylly's there, as files in it would make it the folder of before. The logins in the data folder."""
    home = made_up_home(monkeypatch, tmp_path, "linux")
    assert [where(k) for k in KINDS] == [(home / ".config" / "hylly", "default"), (home / ".local" / "share" / "hylly", "default"),
                                         (home / ".local" / "state" / "hylly", "default"), (home / ".cache" / "hylly", "default"),
                                         (home / ".local" / "share" / "hylly" / "logins", "default")]
    assert paths.job_output_dir() == home / ".local" / "state" / "hylly" / "job-output"  # in the state folder, as before
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/cache")  # not a full path: as the XDG rules say, not taken
    assert where("config")[0] == tmp_path / "xdg-config" / "hylly" and where("cache")[0] == home / ".cache" / "hylly"
    home = made_up_home(monkeypatch, tmp_path, "mac")
    support = home / "Library" / "Application Support" / "Hylly"
    assert [where(k)[0] for k in KINDS] == [support, support, support / "state", home / "Library" / "Caches" / "Hylly", support / "logins"]
    assert paths.job_output_dir() == support / "state" / "job-output" and paths.settings_file() == support / "settings.toml"
    home = made_up_home(monkeypatch, tmp_path, "windows", APPDATA=str(tmp_path / "Roaming"), LOCALAPPDATA=str(tmp_path / "Local"))
    local = tmp_path / "Local" / "Hylly"
    assert [where(k)[0] for k in KINDS] == [tmp_path / "Roaming" / "Hylly", local / "data", local / "state", local / "cache",
                                            local / "data" / "logins"]
    (local / "data").mkdir(parents=True)
    (local / "data" / "hylly.db").write_bytes(b"")  # what a new install writes there: it stays a new install's
    assert where("config")[0] == tmp_path / "Roaming" / "Hylly" and paths.all_in_one() is None


def test_an_install_s_one_folder_is_used_all_in_one_on_every_system_and_nothing_moves(monkeypatch, tmp_path):
    """The folder every install had: ~/.local/share/Hylly on Linux and macOS, %LOCALAPPDATA%\\Hylly on Windows. Holding
    any of Hylly's own files (settings.toml, hylly.db, logins/ and the rest), it is every kind's, as HYLLY_HOME naming it
    would be, with the logins and the cache in theirs inside it, and what long jobs said where it always was. One there
    but empty is no install's: the kinds go apart."""
    for system in ("linux", "mac", "windows"):
        local = tmp_path / system / "home" / "AppData" / "Local"  # Windows' own, as it sets LOCALAPPDATA
        home = made_up_home(monkeypatch, tmp_path / system, system, **({"LOCALAPPDATA": str(local)} if system == "windows" else {}))
        one = (local if system == "windows" else home / ".local" / "share") / "Hylly"
        one.mkdir(parents=True)
        assert where("data")[1] == "default" and paths.all_in_one() is None  # empty: nothing to keep
        for sign in paths.OLD_SIGNS:
            (one / sign).mkdir() if "." not in sign else (one / sign).write_bytes(b"")
            paths.forget_one_folder()  # as a command started now would find it
            assert [where(k) for k in KINDS] == [(one, paths.ONE_FOLDER)] * 3 + [
                (one / "cache", paths.ONE_FOLDER), (one / "logins", paths.ONE_FOLDER)], (system, sign)
            assert paths.all_in_one() == one and paths.settings_file() == one / "settings.toml"
            (one / sign).rmdir() if "." not in sign else (one / sign).unlink()
        (one / "hylly.db").write_bytes(b"")
        paths.forget_one_folder()
        from hylly import access, bell, db, hyllycopy, reader, runs, settings

        assert db.file() == one / "hylly.db" and instance.lock_file().parent == one and runs.lock_file("copy").parent == one
        assert {access.access_log_file().parent, bell.log_file().parent, hyllycopy._targets_file().parent} == {one}
        assert settings.secrets_dir() == one / "secrets" and reader.cache_dir() == one / "cache" / "pages"
        jobs = one.parent / "Hylly-jobs" if system == "windows" else home / ".local" / "state" / "hylly" / "job-output"
        assert paths.job_output_dir() == jobs  # outside it, where it always was
        assert paths.usual_settings_folder()


def test_each_kind_s_own_variable_takes_it_elsewhere_and_hylly_home_keeps_them_all_in_one(monkeypatch, tmp_path):
    """HYLLY_CONFIG_DIR and the rest, each a folder by its full path, over the system's default and over the one folder
    of an install alike; the logins go with the data unless they have one of their own. HYLLY_HOME, set, is every kind's,
    over their own variables too, so that one variable keeps a Hylly tried apart whole."""
    home = made_up_home(monkeypatch, tmp_path, "linux")
    one = home / ".local" / "share" / "Hylly"
    one.mkdir(parents=True)
    (one / "settings.toml").write_bytes(b"")
    paths.forget_one_folder()
    for kind, variable in zip(KINDS, conftest.PLACE_VARIABLES):
        assert paths.PLACES[kind][1] == variable
        monkeypatch.setenv(variable, str(tmp_path / kind))
        assert where(kind) == (tmp_path / kind, variable)
        if kind == "data":  # the logins go with it until they have a variable of their own
            assert where("logins")[0] == one / "logins"  # but an install's one folder keeps its own
    monkeypatch.delenv("HYLLY_LOGINS_DIR")
    (one / "settings.toml").unlink()
    paths.forget_one_folder()
    assert where("logins") == (tmp_path / "data" / "logins", "default") and where("cache") == (tmp_path / "cache", "HYLLY_CACHE_DIR")
    assert paths.job_output_dir() == tmp_path / "state" / "job-output" and not paths.usual_settings_folder()
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "apart"))
    assert [where(k) for k in KINDS] == [(tmp_path / "apart", "HYLLY_HOME")] * 3 + [
        (tmp_path / "apart" / "cache", "HYLLY_HOME"), (tmp_path / "apart" / "logins", "HYLLY_HOME")]
    assert paths.all_in_one() == tmp_path / "apart" and not paths.usual_settings_folder()
    assert paths.job_output_dir() == home / ".local" / "state" / "hylly" / "job-output"  # as all in one has it


def test_the_usual_folders_are_either_way_a_hylly_has_them_with_nothing_set(monkeypatch, tmp_path):
    """A start at logon, and a system-wide service's say, are for the user's usual Hylly alone, told by HYLLY_HOME and the
    settings folder: the settings where this system keeps them, or all in the one folder of an install, HYLLY_HOME naming
    it or not, there yet or not. A HYLLY_HOME or a HYLLY_CONFIG_DIR naming another place makes another Hylly, as a test's
    or an assistant's; a variable of the data, the state, the cache or the logins doesn't, as it's the user's own way of
    keeping that kind."""
    home = made_up_home(monkeypatch, tmp_path, "linux")
    assert paths.usual_settings_folder()
    one = home / ".local" / "share" / "Hylly"
    monkeypatch.setenv("HYLLY_HOME", str(one))  # not there yet: still the user's own
    assert paths.usual_settings_folder()
    monkeypatch.setenv("HYLLY_HOME", str(home / ".local" / "share" / "hylly"))  # all in one, in the data folder: another
    assert paths.usual_settings_folder() is (os.name == "nt")  # but Windows' paths take hylly for Hylly, as its disks do
    monkeypatch.delenv("HYLLY_HOME")
    for variable in ("HYLLY_DATA_DIR", "HYLLY_STATE_DIR", "HYLLY_CACHE_DIR", "HYLLY_LOGINS_DIR"):
        monkeypatch.setenv(variable, str(tmp_path / "a disk of its own" / variable))
    assert paths.usual_settings_folder()
    monkeypatch.setenv("HYLLY_CONFIG_DIR", str(tmp_path / "a test's own"))
    assert not paths.usual_settings_folder()
    monkeypatch.setenv("HYLLY_CONFIG_DIR", str(home / ".config" / ".." / ".config" / "hylly"))  # the usual, written otherwise
    assert paths.usual_settings_folder()
    monkeypatch.setenv("HYLLY_SETTINGS", str(tmp_path / "elsewhere.toml"))
    assert not paths.usual_settings_folder() and paths.usual_settings_folder(settings_file_counts=False)


@linux_paths
def test_a_folder_of_its_own_for_the_data_keeps_the_start_at_logon_and_the_walled_service_s_guard(monkeypatch, tmp_path, usual_folder):
    """A variable of the data, the state, the cache or the logins, set in the user's own shell, keeps this the usual
    Hylly: the start at logon is printed, and carries it, so that the server it starts keeps those kinds where the
    commands do; and a system-wide service still keeps any other server from starting outside its walls."""
    data, cache = tmp_path / "big disk" / "hylly", tmp_path / "fast" / "cache"
    made_up_home(monkeypatch, tmp_path, "linux", HYLLY_DATA_DIR=str(data), HYLLY_CACHE_DIR=str(cache))
    monkeypatch.setattr(paths, "usual_settings_folder", REAL_USUAL_FOLDER)
    fake_systemd(monkeypatch, tmp_path)
    monkeypatch.setattr(instance, "UNIT_FOLDERS", (str(tmp_path / "units"),))  # no system-wide service yet
    assert paths.usual_settings_folder()
    unit = instance.logon_start()[0]
    assert f'Environment="HYLLY_DATA_DIR={data}"' in unit and f'Environment="HYLLY_CACHE_DIR={cache}"' in unit
    (tmp_path / "units").mkdir()
    (tmp_path / "units" / "hylly.service").write_text("[Service]\nUser=me\n", encoding="utf-8")
    monkeypatch.setattr(instance, "_me", lambda: "me")
    monkeypatch.setenv("HYLLY_STATE_DIR", str(tmp_path / "state of its own"))
    assert instance.system_service()  # the walls' say stands
    with pytest.raises(SystemExit):
        instance.logon_start()
    monkeypatch.setenv("HYLLY_CONFIG_DIR", str(tmp_path / "a test's own"))  # another Hylly altogether: no say
    assert not paths.usual_settings_folder() and not instance.system_service()


@pytest.mark.skipif(os.name == "nt", reason="Windows' folders are private by their place in the profile")
def test_every_command_makes_the_folders_a_walled_service_hides_so_that_none_starts_without_its_walls(monkeypatch, tmp_path):
    """examples/hylly.service hides the logins and what long jobs said without a -, so that a start fails rather than
    leave one in the open: every command makes them, the user's alone, with the rest of Hylly's folders. With the
    folders all in one, what long jobs said is outside the one folder, where the walls don't reach, and nothing is made
    for it ahead."""
    import stat

    home = made_up_home(monkeypatch, tmp_path, "linux")
    paths.keep_private()
    for folder in (*(paths.place(kind) for kind in KINDS), paths.job_output_dir()):
        assert folder.is_dir() and stat.S_IMODE(folder.stat().st_mode) == 0o700, folder
    assert paths.job_output_dir() == home / ".local" / "state" / "hylly" / "job-output"
    home = made_up_home(monkeypatch, tmp_path / "one", "linux")
    one = home / ".local" / "share" / "Hylly"
    one.mkdir(parents=True)
    (one / "settings.toml").write_bytes(b"")
    paths.forget_one_folder()
    paths.keep_private()
    assert (one / "logins").is_dir() and (one / "cache").is_dir() and not (home / ".local" / "state").exists()


def test_the_one_folder_is_decided_once_for_each_process(monkeypatch, tmp_path):
    """An install's one folder is looked for once in a process: a moment it can't be seen, a share's or a disk's
    hiccup, never sends a write to the folders by kind, nor the other way round. Another home, or another LOCALAPPDATA
    or XDG_DATA_HOME, is looked at afresh."""
    home = made_up_home(monkeypatch, tmp_path, "linux")
    one = home / ".local" / "share" / "Hylly"
    one.mkdir(parents=True)
    (one / "hylly.db").write_bytes(b"")
    assert paths.place("data") == one
    one.rename(home / "away")  # gone for a moment
    assert paths.place("data") == one and paths.place("state") == one
    (home / "away").rename(one)
    other = made_up_home(monkeypatch, tmp_path / "another", "linux")  # another home: looked at afresh
    assert paths.place("data") == other / ".local" / "share" / "hylly"


def test_on_a_disk_that_takes_capitals_for_small_letters_the_data_folder_is_no_install_s_one_folder(monkeypatch, tmp_path):
    """On Linux, ~/.local/share/hylly, the data folder, and ~/.local/share/Hylly, an install's one folder, are the same
    folder on a disk that doesn't tell capitals from small letters (a Mac's, as a rule). The database and the logins
    written there by an install whose kinds are apart don't make it the one folder of an install, so the settings stay
    where they are; an install's one folder there is one still while it holds the settings, as only such a one does."""
    home = made_up_home(monkeypatch, tmp_path, "linux", by_spelling=False)
    one, data = home / ".local" / "share" / "Hylly", home / ".local" / "share" / "hylly"
    data.mkdir(parents=True)
    (data / "hylly.db").write_bytes(b"")
    (data / "logins").mkdir()
    if not one.exists():  # this disk tells them apart: the same folder by a link, as the other kind of disk has it
        one.symlink_to(data, target_is_directory=True)
    paths.forget_one_folder()
    assert where("data") == (data, "default") and where("config") == (home / ".config" / "hylly", "default")
    (one / "settings.toml").write_text("", encoding="utf-8")  # an install's one folder, with its settings
    paths.forget_one_folder()
    assert where("config") == (one, paths.ONE_FOLDER) and where("data") == (one, paths.ONE_FOLDER)


def test_hylly_settings_places_and_the_doctor_say_where_each_kind_is_and_what_chose_it(monkeypatch, tmp_path, capsys):
    from hylly import doctor

    home = made_up_home(monkeypatch, tmp_path, "linux", HYLLY_CACHE_DIR=str(tmp_path / "fast" / "cache"))
    cli.main(["settings", "places"])
    said = capsys.readouterr().out
    assert f"The settings folder: {home / '.config' / 'hylly'}  (this system's default)" in said
    assert f"The cache folder:    {tmp_path / 'fast' / 'cache'}  (HYLLY_CACHE_DIR)" in said and "HYLLY_HOME keeps them all in one" in said
    assert f"what long jobs said is in {home / '.local' / 'state' / 'hylly' / 'job-output'}" in said
    cli.main(["settings", "places", "--json"])
    found = json.loads(capsys.readouterr().out)
    assert found["places"] == paths.places() and found["all_in_one"] is None
    assert [(p["kind"], p["name"], p["from"]) for p in found["places"]][3] == ("cache", "the cache folder", "HYLLY_CACHE_DIR")
    check = doctor._places()[0]
    assert check["state"] == "info" and check["places"] == paths.places() and check["says"].startswith(
        f"Hylly keeps its own files by kind: the settings folder in {home / '.config' / 'hylly'} (this system's default);")
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "apart"))
    cli.main(["settings", "places"])
    assert f"All in one folder, {tmp_path / 'apart'}." in capsys.readouterr().out
    assert "the one HYLLY_HOME names" in doctor._places()[0]["says"]
    monkeypatch.delenv("HYLLY_HOME")  # an install's one folder, with the settings taken elsewhere: what is so, said
    one = home / ".local" / "share" / "Hylly"
    one.mkdir(parents=True, exist_ok=True)  # a Mac's disk took the data folder hylly, made by the commands above, for it
    (one / "hylly.db").write_bytes(b"")
    paths.forget_one_folder()
    monkeypatch.setenv("HYLLY_CONFIG_DIR", str(tmp_path / "settings"))
    said = doctor._places()[0]["says"]
    assert said.startswith(f"Hylly keeps its own files all in one folder, {one}, as this install has them. Apart from it: "
                           f"the settings folder in {tmp_path / 'settings'} (HYLLY_CONFIG_DIR); the cache folder in")
    assert paths.all_in_one() == one and [p["from"] for p in paths.places()][1] == "one-folder"


def test_two_settings_folders_side_by_side_keep_their_jobs_apart_on_windows(monkeypatch, tmp_path):
    """A job tidies away the oldest of what jobs said, beyond the last 50: one settings folder's mustn't go for the
    other's. On Linux and macOS there is one place, out of a walled server's sight, by design."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    outputs = []
    for name in ("Hylly", "Hylly-try", "Hylly-try/../Hylly-try"):
        monkeypatch.setenv("HYLLY_HOME", str(tmp_path / name))
        outputs.append(REAL_JOB_OUTPUT())
    if sys.platform == "win32":
        assert outputs == [tmp_path / "Hylly-jobs", tmp_path / "Hylly-try-jobs", tmp_path / "Hylly-try-jobs"]
    else:
        assert outputs == [tmp_path / "home" / ".local" / "state" / "hylly" / "job-output"] * 3


def test_the_system_service_is_only_for_linux(monkeypatch, tmp_path, usual_folder):
    wide_systemd(monkeypatch, tmp_path)
    monkeypatch.setattr(instance, "SYSTEM", "mac")
    assert not instance.system_service()


def test_the_server_in_a_system_service_owns_up_and_needs_no_system_to_say_so(monkeypatch, tmp_path, usual_folder):
    calls = wide_systemd(monkeypatch, tmp_path)
    monkeypatch.setenv(instance.SERVICE_FLAG, "1")
    assert instance.in_system_service() and instance.system_service()
    assert instance.system_owns(instance.os.getpid()) and not instance.system_owns(99)
    assert instance.autostart_state() is True
    assert not calls  # inside the walls there is no asking systemctl


def test_a_command_knows_the_server_by_what_the_system_says(monkeypatch, tmp_path, usual_folder):
    wide_systemd(monkeypatch, tmp_path, main_pid="4242")
    assert instance.system_owns(4242) and not instance.system_owns(99) and not instance.system_owns(None)
    assert instance.autostart_state() is True
    wide_systemd(monkeypatch, tmp_path, enabled=False)
    assert instance.autostart_state() is False


@posix_only
def test_a_restart_from_outside_asks_the_server_to_end_for_it(monkeypatch, tmp_path, usual_folder):
    calls, signals = wide_systemd(monkeypatch, tmp_path), []
    monkeypatch.setattr(instance, "running", lambda: {"pid": 4242})
    monkeypatch.setattr(instance.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    instance.restart_through_system()
    assert signals == [(4242, instance.signal.SIGUSR1)]
    assert not any("restart" in command for command in calls)  # systemctl would ask for sudo


@posix_only
def test_a_restart_from_the_server_page_asks_itself(monkeypatch, tmp_path, usual_folder):
    wide_systemd(monkeypatch, tmp_path)
    signals, done = [], threading.Event()
    monkeypatch.setenv(instance.SERVICE_FLAG, "1")
    monkeypatch.setattr(dashboard, "PROC", type("P", (), {"pid": instance.os.getpid()})())
    monkeypatch.setattr(instance.os, "kill", lambda pid, sig: (signals.append((pid, sig)), done.set()))
    monkeypatch.setattr(dashboard.instance, "spawn", lambda *a, **k: pytest.fail("outside the walls"))
    dashboard.restart_later()
    assert done.wait(5) and signals == [(instance.os.getpid(), instance.signal.SIGUSR1)]


def test_with_no_server_running_nothing_is_asked_to_end(monkeypatch, tmp_path, usual_folder):
    wide_systemd(monkeypatch, tmp_path)
    monkeypatch.setattr(instance, "running", lambda: None)
    monkeypatch.setattr(instance.os, "kill", lambda *a: pytest.fail("no one to tell"))
    instance.restart_through_system()


def test_the_start_at_logon_keeps_out_of_a_system_service(monkeypatch, tmp_path, usual_folder):
    """Refused as something to set up first, code 5, on and off alike: not 1, which says run it again."""
    from hylly import exits

    wide_systemd(monkeypatch, tmp_path)
    with pytest.raises(SystemExit, match="system-wide service") as on:
        instance.logon_start()
    with pytest.raises(SystemExit, match="system-wide service") as off:
        instance.logon_stop()
    assert on.value.code == off.value.code == exits.NOT_SET_UP


STARTS = [["serve", "--background"], ["serve", "--no-browser"], ["hyllyd", "--in-background"], ["hyllyd"], ["server", "restart"],
          ["server", "autostart", "on"], ["serve", "--background", "--port", "8791"], ["serve", "--port", "8791"]]


def start(words: list[str]) -> None:
    """hylly's command, or hyllyd with its options."""
    daemon.main(words[1:]) if words[0] == "hyllyd" else cli.main(words)


def no_way_out(monkeypatch):
    monkeypatch.setattr(instance, "start_background", lambda *a, **k: pytest.fail("outside the walls"))
    monkeypatch.setattr(instance, "stop", lambda *a, **k: pytest.fail("the one in the walls, stopped for one outside"))
    monkeypatch.setattr(serve, "run", lambda *a, **k: pytest.fail("outside the walls"))


@pytest.mark.parametrize("words", STARTS)
def test_nothing_starts_a_server_outside_the_walls(monkeypatch, tmp_path, capsys, words, usual_folder):
    wide_systemd(monkeypatch, tmp_path)
    monkeypatch.setattr(instance, "running", lambda: None)
    no_way_out(monkeypatch)
    with pytest.raises(SystemExit):
        start(words)
    assert "sudo systemctl start hylly" in capsys.readouterr().err


@pytest.mark.parametrize("words", [w for w in STARTS if "--no-browser" not in w or len(w) > 2])
def test_with_the_system_silent_the_one_in_the_walls_isn_t_replaced(monkeypatch, tmp_path, capsys, words, usual_folder):
    """D-Bus down, say: asking gives nothing, and the running server isn't known for the service's own. It stays."""
    wide_systemd(monkeypatch, tmp_path, answers=False)
    monkeypatch.setattr(instance, "running", lambda: {"pid": 4242, "port": 8770})
    monkeypatch.setattr(serve, "describe", lambda info: f"process {info['pid']}")
    no_way_out(monkeypatch)
    try:
        start(words)
    except SystemExit:
        pass
    assert "Started" not in capsys.readouterr().out


def test_a_server_already_running_in_the_walls_is_just_said(monkeypatch, tmp_path, capsys, usual_folder):
    wide_systemd(monkeypatch, tmp_path, main_pid="4242")
    monkeypatch.setattr(instance, "running", lambda: {"pid": 4242, "port": 8770})
    monkeypatch.setattr(serve, "describe", lambda info: f"process {info['pid']}")
    monkeypatch.setattr(instance, "stop", lambda: pytest.fail("not asked to"))
    cli.main(["serve", "--no-browser"])
    assert "already running" in capsys.readouterr().out


@posix_only
def test_the_restart_command_goes_through_the_server_itself(monkeypatch, tmp_path, capsys, usual_folder):
    wide_systemd(monkeypatch, tmp_path, main_pid="4242")
    notes = iter([{"pid": 4242, "port": 8770}, {"pid": 4242, "port": 8770}, {"pid": 5000, "port": 8770}])
    signals = []
    monkeypatch.setattr(instance, "running", lambda: next(notes))
    monkeypatch.setattr(instance.os, "kill", lambda pid, sig: signals.append(sig))
    monkeypatch.setattr(instance, "start_background", lambda *a, **k: pytest.fail("the system starts it"))
    monkeypatch.setattr(serve, "describe", lambda info: f"process {info['pid']}")
    cli.main(["server", "restart", "--no-browser"])
    assert signals == [instance.signal.SIGUSR1] and "process 5000" in capsys.readouterr().out


def test_the_status_says_it_starts_with_the_computer(monkeypatch, tmp_path, usual_folder):
    wide_systemd(monkeypatch, tmp_path, main_pid="4242")
    monkeypatch.setattr(instance, "running", lambda: None)
    assert "Starts with the computer (a system-wide service): yes" in serve.status()


def test_a_second_server_in_the_walls_fails_for_all_to_see(monkeypatch, tmp_path):
    """The one running is then outside them: the system should show it, not take it for a clean end."""
    def taken():
        raise instance.AlreadyRunning("A library server is already running.")

    monkeypatch.setattr(instance, "Claim", taken)
    monkeypatch.setenv(instance.SERVICE_FLAG, "1")
    with pytest.raises(SystemExit) as e:
        serve.run(tmp_path, port=8791, background=True)
    assert e.value.code != 0


def test_the_server_page_is_told_it_is_in_the_walls(monkeypatch, tmp_path, log_in):
    """By its status, which the running server answers from the flag its service sets: the page then says what starts
    it again (its words: test_wording.py)."""
    import httpx

    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}", timeout=60)) as client:
            assert client.get("/server/status").json()["service"] is False
            monkeypatch.setenv(instance.SERVICE_FLAG, "1")
            assert instance.in_system_service() and client.get("/server/status").json()["service"] is True
    finally:
        srv.shutdown()
        srv.server_close()


@posix_only
def test_a_walled_in_server_ends_with_the_code_that_gets_it_started_again(tmp_path):
    import os
    import signal
    import socket
    import time

    library = tmp_path / "library"
    (library / "state").mkdir(parents=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    (tmp_path / "settings.toml").write_text(f"library = '{library.as_posix()}'\n[server]\nport = {port}\n", encoding="utf-8")
    env = {**os.environ, instance.SERVICE_FLAG: "1", "HYLLY_SETTINGS": str(tmp_path / "settings.toml"),
           "HYLLY_HOME": str(tmp_path / "Hylly"), "HYLLY_NO_NOTIFY": "1"}
    child = subprocess.Popen([sys.executable, "-m", "hylly.daemon", "--in-background"], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    note = tmp_path / "Hylly" / "server.json"
    for _ in range(150):
        if note.exists() or child.poll() is not None:
            break
        time.sleep(0.2)
    assert child.poll() is None and note.exists(), child.stdout.read().decode(errors="replace")
    time.sleep(0.5)  # the handler is set just after the note
    child.send_signal(signal.SIGUSR1)
    assert child.wait(30) == instance.RESTART_CODE
    told = (tmp_path / "Hylly" / "server.log").read_text(encoding="utf-8")  # written by the server itself
    assert "Library server started: port" in told and "Stopped." in told


def example_service() -> str:
    return (Path(__file__).parent.parent / "examples" / "hylly.service").read_text(encoding="utf-8")


def test_the_example_service_has_its_walls_and_the_flag():
    text = example_service()
    assert "Environment=HYLLY_SYSTEM_SERVICE=1" in text and instance.SERVICE_FLAG == "HYLLY_SYSTEM_SERVICE"
    assert "--in-background" in text and "Restart=on-failure" in text
    lines = text.splitlines()  # its restart, by code 75: no failure, and started again
    assert f"SuccessExitStatus={instance.RESTART_CODE}" in lines and f"RestartForceExitStatus={instance.RESTART_CODE}" in lines
    for wall in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=tmpfs", "CapabilityBoundingSet=\n", "TemporaryFileSystem=",
                 "PrivateUsers=yes", "MemoryDenyWriteExecute=yes", "SystemCallErrorNumber="):
        assert wall in text


def test_the_example_service_doesn_t_open_its_log_as_root():
    """StandardOutput=append: is opened by root, outside the walls, and follows a link the server could put in its place.
    The server writes server.log itself, in its settings folder, which the walls let it write; hyllyd is started
    as it is, with no shell around it."""
    lines = [line for line in example_service().splitlines() if not line.startswith("#")]
    assert not any(line.startswith(("StandardOutput=", "StandardError=")) for line in lines)
    assert [line for line in lines if line.startswith("ExecStart=")] == [
        "ExecStart=/home/someone/.local/share/uv/tools/hylly/bin/hyllyd --in-background"]
    written = [w for line in lines if line.startswith("BindPaths=") for w in line.split("=", 1)[1].split()]
    assert "/home/someone/.local/state/hylly" in written  # the state folder, where server.log is


def test_the_example_service_leaves_its_files_to_systemd_s_own_umask():
    """0022 as systemd has it; group-writable files only where a group must change them, said beside the line."""
    lines = example_service().splitlines()
    assert not any(line.startswith("UMask=") for line in lines) and "#UMask=0002" in lines
    above = "\n".join(lines[lines.index("#UMask=0002") - 3:lines.index("#UMask=0002")])
    assert "0022" in above and "Samba" in above and "group" in above


def test_the_example_service_has_no_way_to_other_programs_sockets():
    families = next(line for line in example_service().splitlines() if line.startswith("RestrictAddressFamilies="))
    assert "AF_UNIX" not in families and "/run:ro" in example_service()


def test_the_example_service_binds_the_one_port_and_says_where_to_change_it():
    """One port, the settings' own as it comes; the header's list of what to put in names it, and so does
    a comment at SocketBindAllow."""
    from hylly import settings

    lines = example_service().splitlines()
    allowed = [line for line in lines if line.startswith("SocketBindAllow=")]
    assert allowed == [f"SocketBindAllow=tcp:{settings.DEFAULTS['server']['port']}"] and "SocketBindDeny=any" in lines
    header = "\n".join(lines[:lines.index("[Unit]")])
    assert "[server] port" in header and "SocketBindAllow" in header
    above = lines[lines.index(allowed[0]) - 1]
    assert above.startswith("#") and "[server] port" in lines[lines.index(allowed[0]) - 2] + above


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd's own check")
def test_systemd_itself_accepts_the_example_service():
    done = subprocess.run(["systemd-analyze", "verify", str(Path(__file__).parent.parent / "examples" / "hylly.service")],
                          capture_output=True, text=True)
    for word in ("Unknown key", "Unknown section", "Invalid", "Missing", "Failed to parse"):
        assert word not in done.stderr, done.stderr


# ---- the doctor and the start at logon: what it starts, and one an older Hylly made ------------------------------

def logon_said(monkeypatch) -> dict:
    """What hylly doctor says of the start at logon: its state, what it says, and the next step."""
    from hylly import doctor

    monkeypatch.setattr(instance, "logon_found", REAL_LOGON_FOUND)
    found = doctor._logon_start()
    return {"state": found[0]["state"], "says": found[0]["says"], "next": found[0].get("next", "")} if found else {}


def test_the_doctor_says_what_a_walled_service_lacks_of_today_s_example(monkeypatch, tmp_path, usual_folder):
    """The system-wide service is the owner's own copy of examples/hylly.service, or one written by hand: the doctor
    says what it lacks of the one the example gives today, each thing only where it's so, never who made it."""
    server = (tmp_path / "hyllyd").as_posix()
    Path(server).write_text("", encoding="utf-8")
    start = f"[Service]\nUser=me\nExecStart={server} --in-background\n"
    codes = "SuccessExitStatus=75\nRestartForceExitStatus=75\n"
    wide_systemd(monkeypatch, tmp_path, service=start + codes)
    assert logon_said(monkeypatch)["state"] == "info"  # today's
    wide_systemd(monkeypatch, tmp_path, service=start + "SuccessExitStatus=75\n")  # lacking only the restart's code
    said = logon_said(monkeypatch)
    assert said["state"] == "update" and "isn't the one examples/hylly.service gives today" in said["says"]
    words = set(re.findall(r"[a-z]+", said["says"].replace(str(tmp_path), "").lower()))  # not the folder's own: /var/folders
    assert "lacks RestartForceExitStatus=75" in said["says"] and not words & {"shell", "older"}
    assert said["next"].startswith("Take SuccessExitStatus and RestartForceExitStatus from examples/hylly.service")
    wide_systemd(monkeypatch, tmp_path, service=f'[Service]\nUser=me\nExecStart=/bin/sh -c "exec {server} --in-background '
                                                f'>> /x/server.log"\n{codes}')  # only its log written by a shell
    said = logon_said(monkeypatch)
    assert said["state"] == "update" and "a shell writes the server's log" in said["says"]
    assert "RestartForceExitStatus" not in said["says"]
    assert said["next"].startswith("Take ExecStart from examples/hylly.service")


# A walled service as examples/hylly.service gave it while Hylly kept everything in one folder, ~/.local/share/Hylly:
# its program's folder as WorkingDirectory and read only, and that one folder bound, the logins hidden in it
ONE_FOLDER_UNIT = """[Service]
User=me
Group=me
WorkingDirectory=/home/someone/hylly
ExecStart={server} --in-background
Environment=HYLLY_SYSTEM_SERVICE=1
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=2
SuccessExitStatus=75
RestartForceExitStatus=75
TemporaryFileSystem=/srv:ro /run:ro
BindPaths=/srv/books
ProtectHome=tmpfs
BindPaths=/home/someone/.local/share/Hylly
InaccessiblePaths=-/home/someone/.local/share/Hylly/logins
ReadOnlyPaths=/home/someone/.local/share/Hylly/settings.toml /home/someone/.local/share/Hylly/secrets -/home/someone/.local/share/Hylly/rules
BindReadOnlyPaths=/home/someone/hylly
BindReadOnlyPaths=/home/someone/.local/share/uv/tools/hylly
"""


def test_a_walled_service_of_the_one_folder_is_as_good_as_one_of_today_s_example(monkeypatch, tmp_path, usual_folder):
    """A system-wide service made from the example of the one folder of all in one, its program's folder its working
    one, is no older for the doctor than one made from today's example of the folders by kind: each says only what it
    starts. Neither the walls' folders nor where it runs count, only what it starts and how it restarts."""
    server = (tmp_path / "hyllyd").as_posix()
    Path(server).write_text("", encoding="utf-8")
    wide_systemd(monkeypatch, tmp_path, service=ONE_FOLDER_UNIT.format(server=server))
    said = logon_said(monkeypatch)
    assert said["state"] == "info" and said["says"] == f"It starts {server} ({tmp_path / 'units' / 'hylly.service'})." and not said["next"]
    today = example_service().replace("/home/someone/.local/share/uv/tools/hylly/bin/hyllyd", server).replace("User=someone", "User=me")
    wide_systemd(monkeypatch, tmp_path, service=today)
    assert logon_said(monkeypatch)["state"] == "info"


def test_inside_the_walls_of_the_one_folder_hylly_keeps_using_it_all_in_one(monkeypatch, tmp_path):
    """Inside the walls of a service made from the example of the one folder, as Linux sets them up: the home folder
    empty but for that folder, bound, with Hylly's files in it, no HYLLY_HOME and no XDG folder set. Every kind is that
    folder, as before, the logins and the cache in theirs inside it, and what long jobs said stays outside it, where
    the walls hide it: nothing moved, and the doctor says so in a note, no more."""
    from hylly import doctor

    home = tmp_path / "home" / "someone"
    one = home / ".local" / "share" / "Hylly"
    one.mkdir(parents=True)
    (one / "settings.toml").write_text("", encoding="utf-8")
    (one / "logins").mkdir()
    monkeypatch.setattr(paths, "SYSTEM", "linux")
    monkeypatch.setattr(Path, "home", lambda: home)
    for name in ("HYLLY_HOME", "HYLLY_SETTINGS", *paths.XDG):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(paths, "job_output_dir", REAL_JOB_OUTPUT)
    assert [(p["kind"], p["folder"], p["from"]) for p in paths.places()] == [
        ("config", str(one), "one-folder"), ("data", str(one), "one-folder"), ("state", str(one), "one-folder"),
        ("cache", str(one / "cache"), "one-folder"), ("logins", str(one / "logins"), "one-folder")]
    assert paths.settings_file() == one / "settings.toml" and paths.job_output_dir() == home / ".local" / "state" / "hylly" / "job-output"
    assert instance.lock_file() == one / "server.lock" and instance.log_file() == one / "server.log"
    paths.keep_private()  # as every command does: nothing made outside that folder, where the walls would refuse it
    made = [".local", "Hylly", *(["cache"] if os.name != "nt" else []), "logins", "share"]  # Windows makes none ahead
    assert sorted(p.name for p in home.rglob("*") if p.is_dir()) == made
    found = doctor._places()[0]
    assert found["state"] == "info" and found["says"].startswith(f"Hylly keeps its own files all in one folder, {one}, as this install has them.")
    assert "Where things are" in found["says"] and "next" not in found and found["places"] == paths.places()


def test_the_doctor_names_what_a_systemd_unit_starts_and_notices_one_from_before(monkeypatch, tmp_path, usual_folder):
    fake_systemd(monkeypatch, tmp_path)
    unit = tmp_path / "systemd" / "user" / "hylly.service"
    assert logon_said(monkeypatch) == {}  # none at all
    unit.parent.mkdir(parents=True)
    unit.write_text(instance.logon_start()[0], encoding="utf-8")  # as hylly server autostart on prints it now
    said = logon_said(monkeypatch)
    assert said["state"] == "info" and f"It starts {instance._python()}" in said["says"]
    python = instance.unit_text(instance._python(), [], "/x", {}).split('ExecStart=')[1].splitlines()[0]
    unit.write_text(f'[Service]\nExecStart={python} "-P" "-m" "hylly.daemon" "--in-background" "--no-browser"\n'
                    'StandardOutput=append:/x/server.log\nRestart=on-failure\n', encoding="utf-8")  # an old one, with no options
    said = logon_said(monkeypatch)
    assert said["state"] == "update" and "an older Hylly" in said["says"] and "hylly server autostart on" in said["next"]
    for before in (f'ExecStart={python} "-P" "-m" "hylly.daemon" "--in-background" "--no-browser" "--port" "8770"\n',
                   f'ExecStart={python} "-m" "hylly" "serve" "--in-background" "--no-browser"\n'):  # with options; the one before
        unit.write_text(f"[Service]\n{before}Restart=on-failure\n", encoding="utf-8")
        said = logon_said(monkeypatch)  # refused now, at every start: a problem, not an update
        assert said["state"] == "problem" and "fails at every start now" in said["says"], said
        assert said["next"].startswith("Run hylly server autostart on again, and the commands it prints")
    unit.write_text('[Service]\nExecStart="/gone/with/hylly/bin/python" "-P" "-m" "hylly.daemon" "--in-background"\n', encoding="utf-8")
    said = logon_said(monkeypatch)
    assert said["state"] == "problem" and "/gone/with/hylly/bin/python" in said["says"] and "isn't there any more" in said["says"]
    assert "hylly server autostart off" in said["next"]


def test_the_doctor_notices_a_launchd_agent_and_a_task_from_before(monkeypatch, tmp_path, usual_folder):
    monkeypatch.setattr(instance, "SYSTEM", "mac")
    monkeypatch.setattr(instance.Path, "home", lambda: tmp_path)
    path = tmp_path / "Library" / "LaunchAgents" / f"{instance.AGENT}.plist"
    path.parent.mkdir(parents=True)
    path.write_bytes(instance.logon_start()[0].encode("utf-8"))
    assert logon_said(monkeypatch)["state"] == "info"
    path.write_bytes(plistlib.dumps({"Label": instance.AGENT, "ProgramArguments": [instance._python(), *instance.LOGON, "--no-browser"],
                                     "StandardOutPath": "/x/server.log"}))
    assert logon_said(monkeypatch)["state"] == "update"
    path.write_bytes(plistlib.dumps({"Label": instance.AGENT, "ProgramArguments": [instance._python(), *instance.LOGON, "--port", "8770"]}))
    assert logon_said(monkeypatch)["state"] == "problem"

    tasks = {}
    monkeypatch.setattr(instance, "SYSTEM", "windows")
    monkeypatch.setattr(instance, "_schtasks", lambda *args: subprocess.CompletedProcess(args, 0 if tasks else 1, tasks.get("xml", ""), ""))
    assert logon_said(monkeypatch) == {}
    windowless = str(Path(sys.executable))  # a program that is there
    for arguments, state in ((subprocess.list2cmdline(instance.TASK_ARGUMENTS), "info"),
                             ("-m hylly serve --background --no-browser", "update"),  # from before --here and -P
                             (subprocess.list2cmdline([*instance.TASK_ARGUMENTS, "--lan", "--idle", "30"]), "problem"),
                             (subprocess.list2cmdline([*instance.TASK_ARGUMENTS, "--library", "D:\\My Books"]), "problem")):
        tasks["xml"] = instance.task_xml(windowless, arguments, instance.start_folder(), r"PC\someone")
        said = logon_said(monkeypatch)
        assert said["state"] == state, said
        if state == "update":
            assert "hylly server autostart on: it makes the task again" in said["next"]
    assert said["next"].startswith("Run hylly server autostart on again: it makes the task anew") and "fails at every start" in said["says"]
    tasks["xml"] = instance.task_xml(r"C:\Gone\Hylly\.venv\Scripts\pythonw.exe", subprocess.list2cmdline(instance.TASK_ARGUMENTS),
                                     instance.start_folder(), r"PC\someone")
    assert logon_said(monkeypatch)["state"] == "problem"


def test_the_doctor_notices_a_walled_service_from_before(monkeypatch, tmp_path, usual_folder):
    """The owner's own: a shell around hyllyd that writes its log, and no exit-status lines for its restart."""
    hyllyd = tmp_path / "tools" / "hylly" / "bin" / "hyllyd"  # a program that is there
    hyllyd.parent.mkdir(parents=True)
    hyllyd.write_text("", encoding="utf-8")
    hyllyd = hyllyd.as_posix()
    wide_systemd(monkeypatch, tmp_path, service=f"[Service]\nUser=me\nExecStart=/bin/sh -c 'exec \"$$0\" \"$$@\" >>/x/server.log 2>&1' "
                                                f"{hyllyd} --in-background --no-browser\nRestart=on-failure\n")
    said = logon_said(monkeypatch)
    assert said["state"] == "update" and "system-wide service" in said["says"] and "examples/hylly.service" in said["next"]
    wide_systemd(monkeypatch, tmp_path, service=f"[Service]\nUser=me\nExecStart={hyllyd} --in-background\nSuccessExitStatus=75\n"
                                                "RestartForceExitStatus=75\n")
    said = logon_said(monkeypatch)
    assert said["state"] == "info" and f"It starts {hyllyd}" in said["says"]
    wide_systemd(monkeypatch, tmp_path, service=f"[Service]\nUser=me\nExecStart={hyllyd} --in-background --port 8771\n"
                                                "SuccessExitStatus=75\nRestartForceExitStatus=75\n")  # options: refused now
    said = logon_said(monkeypatch)
    assert said["state"] == "problem" and "fails at every start now" in said["says"] and "Take the options off" in said["next"]
    wide_systemd(monkeypatch, tmp_path, service="[Service]\nUser=me\nExecStart=/gone/hyllyd --in-background\n")
    assert logon_said(monkeypatch)["next"].endswith("sudo systemctl daemon-reload")


# ---- 7-Zip: a flaw in a server with walls around it mustn't get to choose what your commands run --------------------

@pytest.fixture
def on_linux(monkeypatch, tmp_path):
    from hylly import archives, paths, settings

    library = tmp_path / "library"
    (library / "state").mkdir(parents=True)
    monkeypatch.setattr(paths, "WINDOWS", False)
    monkeypatch.setenv("HYLLY_LIBRARY", str(library))
    monkeypatch.setattr(archives, "sevenzip", REAL_SEVENZIP)  # made-up ones, below
    return archives, settings, library


def tool_in(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "7zz").write_bytes(b"not really")
    return folder / "7zz"


@posix_only
def test_a_7zip_in_the_settings_folder_or_the_library_isn_t_run(on_linux, monkeypatch):
    archives, settings, library = on_linux
    folder = instance.paths.config_dir()
    for tool in (tool_in(folder), tool_in(folder / "cache"), tool_in(library / "comics")):
        monkeypatch.setattr(settings, "get", lambda section, tool=tool: {"sevenzip": str(tool)})
        assert archives.sevenzip() == ""


def test_the_check_looks_through_the_way_it_is_written(on_linux, tmp_path):
    archives, _, library = on_linux
    assert archives._server_can_write(str(instance.paths.config_dir() / "x" / ".." / "7zz"))
    assert archives._server_can_write(str(library / "7zz"))
    assert not archives._server_can_write(str(tmp_path / "elsewhere" / "7zz"))


def test_a_7zip_elsewhere_is_found_as_ever(on_linux, monkeypatch, tmp_path):
    archives, settings, _ = on_linux
    tool = tool_in(tmp_path / "bin")
    monkeypatch.setattr(archives, "_server_can_write", lambda found: False)
    monkeypatch.setattr(settings, "get", lambda section: {"sevenzip": "7zz"})
    monkeypatch.setattr(shutil, "which", lambda name: str(tool))
    assert archives.sevenzip() == str(tool)


def test_a_name_that_leads_to_the_settings_folder_is_refused_too(on_linux, monkeypatch):
    archives, settings, _ = on_linux
    tool = tool_in(instance.paths.config_dir())
    monkeypatch.setattr(settings, "get", lambda section: {"sevenzip": "../7zz"})
    monkeypatch.setattr(shutil, "which", lambda name: str(tool))
    assert archives.sevenzip() == ""


def test_a_file_whose_rights_can_t_be_changed_doesn_t_leave_the_others(monkeypatch):
    folder = instance.paths.config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "a.txt").write_text("a", encoding="utf-8")
    (folder / "b.txt").write_text("b", encoding="utf-8")
    changed, real = [], instance.paths.os.chmod

    def chmod(path, mode):
        if Path(path).name == "a.txt":
            raise PermissionError("root's")
        changed.append(Path(path).name)

    if sys.platform == "win32":
        pytest.skip("permissions are for Linux and macOS")
    for name in ("a.txt", "b.txt"):
        real(folder / name, 0o644)
    monkeypatch.setattr(instance.paths.os, "chmod", chmod)
    instance.paths.keep_private()
    assert "b.txt" in changed


# ---- the walled service: printed for this computer from the settings, and the one installed against them ------------

def a_linux_of_ann_s(monkeypatch, tmp_path, port: int = 8780) -> Path:
    """A Linux computer of a made-up user's, ann, with a home of her own (made_up_home), the settings' library and [server]
    port, the program where uv tool install puts it, and no walled service installed yet, the system's folders for one
    all made up. Returns the library."""
    home = made_up_home(monkeypatch, tmp_path, "linux")
    library = tmp_path / "srv" / "Lantern Wolf books"
    monkeypatch.setattr(instance, "SYSTEM", "linux")
    monkeypatch.setattr(instance, "_me", lambda: "ann")
    monkeypatch.setattr(instance, "_group", lambda: "ann")
    monkeypatch.setattr(instance, "WALLED_FILE", str(tmp_path / "etc" / "hylly.service"))
    monkeypatch.setattr(instance, "UNIT_FOLDERS", (str(tmp_path / "etc"),))
    monkeypatch.setattr(instance, "CONTROL_FOLDERS", (str(tmp_path / "control"),))
    tool = home / ".local" / "share" / "uv" / "tools" / "hylly"
    monkeypatch.setattr(instance, "_walled_program", lambda: ([str(tool / "bin" / "hyllyd")], [str(tool)], None))
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text(f"library = '{library}'\n[server]\nport = {port}\n", encoding="utf-8")
    settings.reload()
    return library


@linux_paths
def test_the_walled_service_is_printed_for_this_computer_from_the_settings_and_nothing_is_written(monkeypatch, tmp_path):
    library = a_linux_of_ann_s(monkeypatch, tmp_path)
    home = Path.home()
    before = sorted(tmp_path.rglob("*"))
    text, steps, also = instance.service_file()
    assert sorted(tmp_path.rglob("*")) == before  # it only prints
    lines = text.splitlines()
    assert "User=ann" in lines and "Group=ann" in lines and "SocketBindAllow=tcp:8780" in lines  # the settings' port
    assert f'BindPaths="{library}"' in lines and f'RequiresMountsFor="{library}"' in lines
    # the library's parent is hidden but where ProtectHome hides it already (pytest's own folder is in /home on some
    # computers): the unit's own rule, NOT_HIDDEN_FROM
    hidden_already = any(library.parent == Path(f) or Path(f) in library.parent.parents for f in instance.NOT_HIDDEN_FROM)
    assert ("TemporaryFileSystem=/run:ro" if hidden_already else f"TemporaryFileSystem={library.parent}:ro /run:ro") in lines
    data, state, cache = home / ".local" / "share" / "hylly", home / ".local" / "state" / "hylly", home / ".cache" / "hylly"
    assert f"BindPaths={data} {state} {cache}" in lines and f"WorkingDirectory={data}" in lines
    assert f"InaccessiblePaths={data / 'logins'}" in lines and f"InaccessiblePaths={state / 'job-output'}" in lines
    assert f"BindReadOnlyPaths={home / '.config' / 'hylly'}" in lines
    assert f"ExecStart={home}/.local/share/uv/tools/hylly/bin/hyllyd --in-background" in lines
    assert steps[1:] == ["sudo cp hylly.service /etc/systemd/system/", "sudo systemctl daemon-reload",
                         "sudo systemctl enable --now hylly"] and steps[0].endswith("> hylly.service.new && mv hylly.service.new hylly.service")
    assert all(f"#   {step}" in lines for step in steps) and "this computer" in text.split("[Unit]")[0]
    assert also.startswith("Not there yet, and needed before it starts: ") and str(library) in also
    library.mkdir(parents=True)
    paths.keep_private()  # as any command makes Hylly's own folders
    assert instance.service_file()[2] == "Everything it names is there now."
    Path(instance.WALLED_FILE).parent.mkdir()
    Path(instance.WALLED_FILE).write_text(text, encoding="utf-8")  # installed: put in its place
    text, steps, _ = instance.service_file()
    assert steps[-1] == "sudo systemctl restart hylly" and f"#   diff {instance.WALLED_FILE} hylly.service" in text.splitlines()


def test_the_walled_service_is_printed_only_on_linux_and_only_for_the_usual_hylly(monkeypatch, tmp_path):
    a_linux_of_ann_s(monkeypatch, tmp_path)
    monkeypatch.setattr(instance, "SYSTEM", "mac")
    with pytest.raises(SystemExit, match="for Linux") as e:
        instance.service_file()
    assert e.value.code == 2
    monkeypatch.setattr(instance, "SYSTEM", "linux")
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "a test's own"))  # another Hylly altogether
    with pytest.raises(SystemExit, match="settings of its own"):
        instance.service_file()


@linux_paths
def test_hylly_serve_service_file_prints_it_on_standard_output_and_the_steps_on_standard_error(monkeypatch, tmp_path, capsys):
    """The file alone on standard output, so that > hylly.service is the file; the sudo lines on standard error. Beside a
    walled service installed and running, which no hylly serve may start another of, it prints all the same; with an
    option typed for one run it prints nothing, as the server it starts takes the settings alone."""
    a_linux_of_ann_s(monkeypatch, tmp_path)
    cli.main(["serve", "--service-file"])
    said = capsys.readouterr()
    assert said.out == instance.service_file()[0] and "sudo systemctl enable --now hylly" in said.err
    assert said.err.startswith("Nothing was installed, and nothing written but what every command makes")
    with pytest.raises(SystemExit) as e:
        cli.main(["serve", "--service-file", "--port", "8790"])
    said = capsys.readouterr()
    assert e.value.code == 2 and said.out == "" and "--port can't be given with it" in said.err
    cli.main(["serve", "--service-file", "--dry-run"])
    assert capsys.readouterr().out.startswith("It would print the system-wide service")
    (tmp_path / "etc").mkdir()
    Path(instance.WALLED_FILE).write_text("[Service]\nUser=ann\n", encoding="utf-8")  # one walled in, and running
    monkeypatch.setattr(instance, "running", lambda: {"pid": 7, "port": 8780})
    assert instance.system_service()
    cli.main(["serve", "--service-file"])
    assert "[Unit]" in capsys.readouterr().out


def walled_and_compared(monkeypatch, tmp_path, unit: str, port: int = 8780, drop_ins: dict | None = None,
                        control: dict | None = None) -> dict:
    """The walled service installed as unit says (and its drop-ins, by file name, and those systemctl set-property
    keeps: control), compared with the settings by the doctor's own look (instance.service_differences)."""
    a_linux_of_ann_s(monkeypatch, tmp_path, port)
    monkeypatch.setattr(instance, "service_differences", REAL_SERVICE_DIFFERENCES)
    monkeypatch.setattr(paths, "usual_settings_folder", lambda settings_file_counts=True: True)
    etc = tmp_path / "etc"
    for folder in (etc / "hylly.service.d", tmp_path / "control"):
        shutil.rmtree(folder, ignore_errors=True)
    etc.mkdir(exist_ok=True)
    (etc / "hylly.service").write_text(unit, encoding="utf-8")
    for folder, given in ((etc, drop_ins), (tmp_path / "control", control)):
        for name, text in (given or {}).items():
            (folder / "hylly.service.d").mkdir(parents=True, exist_ok=True)
            (folder / "hylly.service.d" / name).write_text(text, encoding="utf-8")
    return instance.service_differences()


@linux_paths
def test_the_walled_service_printed_and_installed_as_it_is_matches_the_settings(monkeypatch, tmp_path):
    a_linux_of_ann_s(monkeypatch, tmp_path)
    printed = instance.service_file()[0]
    assert walled_and_compared(monkeypatch, tmp_path, printed)["differences"] == []
    from hylly import doctor

    monkeypatch.setattr(instance, "running", lambda: None)
    assert [(c["id"], c["state"]) for c in doctor._walls()] == [("walls", "ok")]


@posix_only
def test_a_walled_service_saying_the_same_another_way_is_no_difference(monkeypatch, tmp_path):
    """The live server's own is the owner's, edited by hand: the library through a link or a bind of a folder it's in,
    a port range or none at all, quoted paths, the user by number, a drop-in that clears a list and says it again,
    the one-folder example of before. None of it is a difference; nor is what can't be told here (a specifier)."""
    library = a_linux_of_ann_s(monkeypatch, tmp_path)
    real = tmp_path / "disk" / "books"
    real.mkdir(parents=True)
    library.parent.mkdir(parents=True)
    library.symlink_to(real)  # the settings name the link
    walls = "[Service]\nProtectSystem=strict\nProtectHome=tmpfs\nTemporaryFileSystem=/run:ro\nSocketBindDeny=any\n"
    for unit, drop_ins in (
            (f"{walls}User=ann\nBindPaths={real}\nSocketBindAllow=tcp:8780\n", {}),  # the library where its link leads
            (f'{walls}User=ann\nBindPaths="{tmp_path}"\nSocketBindAllow=ipv4:tcp:8700-8799\n', {}),  # a folder it's in, quoted
            (f'{walls}User={os.getuid()}\nBindPaths={real}:"{library}":rbind\nSocketBindAllow=8780\n', {}),
            (f"{walls}User=ann\nBindPaths={real}\nSocketBindAllow=tcp:8770\n",
             {"50-port.conf": "[Service]\nSocketBindAllow=\nSocketBindAllow=tcp:8780\n"}),
            (f"{walls}User=ann\nBindPaths=%h/books\nSocketBindAllow=tcp:8780\n", {}),  # %h: systemd's to say
            (ONE_FOLDER_UNIT.format(server="/x/hyllyd").replace("User=me", "User=ann").replace("/srv/books", str(real)), {})):
        found = walled_and_compared(monkeypatch, tmp_path, unit, drop_ins=drop_ins)
        assert found["differences"] == [], (unit, found)
        shutil.rmtree(tmp_path / "etc")


@linux_paths
def test_a_walled_service_that_differs_from_the_settings_is_named_by_its_line_and_the_fix(monkeypatch, tmp_path):
    from hylly import doctor

    library = a_linux_of_ann_s(monkeypatch, tmp_path)
    unit = (f"[Service]\nUser=ann\nProtectSystem=strict\nProtectHome=tmpfs\nTemporaryFileSystem={library.parent}:ro /run:ro\n"
            f'BindPaths="{library.parent / "old books"}"\nSocketBindAllow=tcp:8770\nSocketBindDeny=any\n')
    found = walled_and_compared(monkeypatch, tmp_path, unit)["differences"]  # another port, another library
    file = str(tmp_path / "etc" / "hylly.service")
    assert [(d["line"], d["file"], d["want"]) for d in found] == [
        (f"TemporaryFileSystem={library.parent}:ro /run:ro", file, f'BindPaths="{library}"'),
        ("SocketBindAllow=tcp:8770", file, "SocketBindAllow=tcp:8780")]
    assert "hides it, and no BindPaths= brings it back" in found[0]["says"] and str(library) in found[0]["says"]
    assert "port 8780" in found[1]["says"] and "SocketBindDeny=any" in found[1]["says"]
    monkeypatch.setattr(instance, "running", lambda: None)
    said = doctor._walls()
    assert [c["state"] for c in said] == ["problem", "problem"] and all(c["owner"] for c in said)
    assert said[1]["says"].startswith(f"The system-wide service ({file}) doesn't match the settings: SocketBindAllow=tcp:8770")
    assert said[1]["next"].startswith(f"Make it SocketBindAllow=tcp:8780 in {file}: hylly serve --service-file prints")
    right = unit.replace("SocketBindAllow=tcp:8770", "SocketBindAllow=tcp:8780").replace("old books", "Lantern Wolf books")
    found = walled_and_compared(monkeypatch, tmp_path, right.replace("User=ann\n", ""))  # no user: root
    assert [d["want"] for d in found["differences"]] == ["User=ann"] and "as root" in found["differences"][0]["says"]
    over = walled_and_compared(monkeypatch, tmp_path, right, drop_ins={"override.conf": "[Service]\nUser=bea\n"})
    assert [(d["line"], d["file"]) for d in over["differences"]] == [("User=bea", str(tmp_path / "etc" / "hylly.service.d" / "override.conf"))]
    read_only = walled_and_compared(monkeypatch, tmp_path, right.replace("BindPaths=", "BindReadOnlyPaths="))
    assert "only to read" in read_only["differences"][0]["says"]


@posix_only
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads anything")
def test_a_walled_service_file_that_can_t_be_read_is_compared_with_nothing(monkeypatch, tmp_path):
    from hylly import doctor

    found = walled_and_compared(monkeypatch, tmp_path, "[Service]\nUser=ann\n", drop_ins={"secret.conf": "[Service]\nUser=bea\n"})
    assert found["differences"] == [{"line": "User=bea", "file": str(tmp_path / "etc" / "hylly.service.d" / "secret.conf"),
                                     "says": found["differences"][0]["says"], "want": "User=ann"}]
    (tmp_path / "etc" / "hylly.service.d" / "secret.conf").chmod(0)
    try:
        found = instance.service_differences()
        assert found["differences"] == [] and found["unread"] == [str(tmp_path / "etc" / "hylly.service.d" / "secret.conf")]
        monkeypatch.setattr(instance, "running", lambda: None)
        assert [c["state"] for c in doctor._walls()] == ["info", "ok"]
    finally:
        (tmp_path / "etc" / "hylly.service.d" / "secret.conf").chmod(0o600)



@linux_paths
def test_the_walled_service_starts_hyllyd_as_uv_tool_install_put_it_and_sees_its_python(monkeypatch, tmp_path):
    """The program's own folder, read only, and the folder of uv's Pythons, which the tool's python links to; hyllyd
    beside the tool's python, else that python with -P -m hylly.daemon. A Python of the system's needs no line."""
    tool = tmp_path / "home" / ".local" / "share" / "uv" / "tools" / "hylly"
    (tool / "bin").mkdir(parents=True)
    (tool / "bin" / "hyllyd").write_text("", encoding="utf-8")
    pythons = tmp_path / "home" / ".local" / "share" / "uv" / "python"
    monkeypatch.setattr(sys, "executable", str(tool / "bin" / "python"))
    monkeypatch.setattr(sys, "prefix", str(tool))
    monkeypatch.setattr(sys, "base_prefix", str(pythons / "cpython-3.14-linux"))
    program, read_only, found = instance._walled_program()
    source = str(Path(instance.__file__).resolve().parents[1])  # this checkout's, outside the tool: it's seen too
    assert program == [str(tool / "bin" / "hyllyd")] and read_only == [str(tool), source] and found == str(pythons.resolve())
    (tool / "bin" / "hyllyd").unlink()
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    program, _, found = instance._walled_program()
    assert program == [instance._python(), *instance.DAEMON] and found is None


@linux_paths
def test_a_walled_service_s_library_is_the_one_its_server_gets_never_this_shell_s(monkeypatch, tmp_path):
    """The service's own Environment= HYLLY_LIBRARY, else the settings' library: a HYLLY_LIBRARY in the shell the doctor
    runs in is no difference, and the words say which library it is. What can't be told from the files, an
    EnvironmentFile=, or settings the service names another way than this shell, is said and not compared."""
    library = a_linux_of_ann_s(monkeypatch, tmp_path)
    walls = (f"[Service]\nUser=ann\nProtectSystem=strict\nTemporaryFileSystem={library.parent}:ro /run:ro\n"
             f'BindPaths="{library}"\nSocketBindAllow=tcp:8780\nSocketBindDeny=any\n')
    monkeypatch.setenv("HYLLY_LIBRARY", str(tmp_path / "srv" / "a shell's own"))
    assert walled_and_compared(monkeypatch, tmp_path, walls) == {
        "files": [str(tmp_path / "etc" / "hylly.service")], "differences": [], "unread": [], "skipped": []}
    other = library.parent / "Moonwake books"
    found = walled_and_compared(monkeypatch, tmp_path, walls + f'Environment="HYLLY_LIBRARY={other}"\n')["differences"]
    assert [d["want"] for d in found] == [f'BindPaths="{other}"']
    assert found[0]["says"].endswith(f'the library its Environment="HYLLY_LIBRARY={other}" names, {other}')
    found = walled_and_compared(monkeypatch, tmp_path, walls.replace("tcp:8780", "tcp:8770") + "EnvironmentFile=-/etc/hylly.env\n")
    assert found["differences"] == [] and "EnvironmentFile=-/etc/hylly.env" in found["skipped"][0]
    found = walled_and_compared(monkeypatch, tmp_path, walls.replace("tcp:8780", "tcp:8770") + "Environment=HYLLY_HOME=/srv/hylly\n")
    assert found["differences"] == [] and "another way than this shell does (HYLLY_HOME)" in found["skipped"][0]
    from hylly import doctor

    monkeypatch.setattr(instance, "running", lambda: None)
    assert [c["state"] for c in doctor._walls()] == ["info", "ok"] and "not compared" in doctor._walls()[0]["says"]


def test_the_drop_ins_systemctl_set_property_keeps_are_read_first(monkeypatch, tmp_path):
    """/etc/systemd/system.control and /run/systemd/system.control, before the unit folders: of two drop-ins of one
    name, theirs counts."""
    library = a_linux_of_ann_s(monkeypatch, tmp_path)
    unit = f'[Service]\nUser=ann\nBindPaths="{library}"\nSocketBindAllow=tcp:8770\nSocketBindDeny=any\n'
    port = "[Service]\nSocketBindAllow=\nSocketBindAllow=tcp:{}\n"
    assert walled_and_compared(monkeypatch, tmp_path, unit, control={"50-SocketBindAllow.conf": port.format(8780)},
                               drop_ins={"50-SocketBindAllow.conf": port.format(8770)})["differences"] == []
    found = walled_and_compared(monkeypatch, tmp_path, unit.replace("8770", "8780"), control={"50-User.conf": "[Service]\nUser=bea\n"})
    assert [(d["line"], d["file"]) for d in found["differences"]] == [
        ("User=bea", str(tmp_path / "control" / "hylly.service.d" / "50-User.conf"))]


def test_a_user_in_quotes_is_no_user_systemd_knows(monkeypatch, tmp_path):
    """systemd takes User= as it is written: in quotes, it names nobody, and the service doesn't start."""
    library = a_linux_of_ann_s(monkeypatch, tmp_path)
    unit = f'[Service]\nUser=ann\nBindPaths="{library}"\n'
    found = walled_and_compared(monkeypatch, tmp_path, unit, drop_ins={"override.conf": '[Service]\nUser="ann"\n'})
    assert [d["line"] for d in found["differences"]] == ['User="ann"'] and "in quotes" in found["differences"][0]["says"]
    assert walled_and_compared(monkeypatch, tmp_path, unit.replace("User=ann", 'User="ann"')) is None  # not ann's at all
    assert not instance.system_service()


@posix_only
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads anything")
def test_a_drop_in_folder_that_can_t_be_listed_compares_nothing(monkeypatch, tmp_path):
    found = walled_and_compared(monkeypatch, tmp_path, "[Service]\nUser=ann\n", drop_ins={"override.conf": "[Service]\nUser=bea\n"})
    assert [d["line"] for d in found["differences"]] == ["User=bea"]
    folder = tmp_path / "etc" / "hylly.service.d"
    folder.chmod(0)
    try:
        found = instance.service_differences()
        assert found["differences"] == [] and found["unread"] == [str(folder)]
    finally:
        folder.chmod(0o700)


@linux_paths
def test_all_in_one_folder_the_printed_service_starts_without_rules_or_themes(monkeypatch, tmp_path):
    """settings.toml and secrets/, which hylly settings edit and every command make, must be there; the rules and
    themes folders, which nothing makes, are walled where they are there, so a service printed without them starts."""
    library = a_linux_of_ann_s(monkeypatch, tmp_path)
    one = Path.home() / ".local" / "share" / "Hylly"
    one.mkdir(parents=True)
    (one / "settings.toml").write_text(paths.settings_file().read_text(encoding="utf-8"), encoding="utf-8")
    paths.forget_one_folder()
    assert paths.settings_file() == one / "settings.toml"
    library.mkdir(parents=True)
    paths.keep_private()
    settings.make_secrets_dir()  # as every command does
    text, _, also = instance.service_file()
    assert f"ReadOnlyPaths={one}/settings.toml {one}/secrets -{one}/rules -{one}/themes" in text.splitlines()
    assert also == "Everything it names is there now." and not (one / "rules").exists()


def test_a_mac_notification_opens_the_server_page_where_terminal_notifier_is_installed(monkeypatch):
    """terminal-notifier, where it's installed, also in Homebrew's folders that a server started at logon has no PATH to:
    its notification opens the link when clicked, the texts its own arguments, one starting as an option escaped; where
    it isn't, AppleScript's, as before; the Server page's address from [server] address, else this computer's."""
    tool = "/opt/homebrew/bin/terminal-notifier"
    mac = notify.command("Hylly: New device", "-ann logged in", "darwin", link="http://localhost:8770/server", notifier=tool)
    assert mac == [tool, "-title", "Hylly: New device", "-message", "\\-ann logged in", "-open", "http://localhost:8770/server"]
    assert notify.command("t", "x", "darwin", notifier=tool) == [tool, "-title", "t", "-message", "x"]
    assert notify.command("t", "x", "darwin", notifier="")[0] == "osascript"
    seen = []
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(notify.shutil, "which", lambda name, path=None: seen.append(path) or None)
    assert notify.mac_notifier() == "" and "/opt/homebrew/bin" in seen[0] and "/usr/local/bin" in seen[0]
    from hylly import bell

    for conf, page in (({"address": "https://moonwake.example/", "listen": "0.0.0.0", "port": 8770}, "https://moonwake.example/server"),
                       ({"address": "", "listen": "0.0.0.0", "port": 8770}, "http://localhost:8770/server"),
                       ({"address": "", "listen": "192.0.2.10", "port": 8771}, "http://192.0.2.10:8771/server"),
                       ({"address": "", "listen": "::1", "port": 8770}, "http://localhost:8770/server"),
                       ({"listen": "nowhere"}, "")):
        monkeypatch.setattr(bell, "settings_section", lambda s, conf=conf: conf)
        assert bell.server_page() == page
