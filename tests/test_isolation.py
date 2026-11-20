"""The tests keep to folders of their own, whatever a test does with monkeypatch: an undo() takes the fixtures' changes
away too, and what's under them is the session's own (conftest.SAFE), never the owner's settings folder, home or
network."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

import conftest
from hylly import bell, hyllycopy, paths


def test_an_undo_falls_back_to_the_session_s_own_folders_never_the_owner_s(monkeypatch):
    monkeypatch.undo()  # the fixtures' HYLLY_HOME, LOCALAPPDATA, HYLLY_SETTINGS and network guard are gone with it
    for where in (*(paths.place(kind) for kind in paths.PLACES), paths.settings_file(), bell.log_file(), hyllycopy._targets_file(),
                  paths.job_output_dir(), Path.home()):
        assert Path(where).is_relative_to(conftest.SESSION) and not conftest.in_the_owners(where), where
    assert paths.config_dir() == conftest.SAFE["HYLLY_HOME"] and os.environ["HYLLY_NO_NOTIFY"] == "1"
    before = len(conftest.ASKED_ANYWHERE)
    with pytest.raises(httpx.ConnectError):  # the session's guard stays: no phone note, nor anything else, leaves
        httpx.post("https://ntfy.sh/a-made-up-topic", content=b"a test")
    assert conftest.ASKED_ANYWHERE[before:] == ["POST https://ntfy.sh/a-made-up-topic"]
    del conftest.ASKED_ANYWHERE[before:]  # said, and asked for here: the check after the test needn't fail it


def test_a_kind_s_own_variable_of_the_owner_s_reaches_no_test(monkeypatch):
    """HYLLY_CONFIG_DIR and the rest, set where the tests are run, are taken away for the session and for each test; and
    HYLLY_HOME, which every test has, is every kind's over them anyway."""
    assert not [name for name in conftest.PLACE_VARIABLES if name in os.environ]
    monkeypatch.setenv("HYLLY_DATA_DIR", str(conftest.SESSION / "not-a-test-s"))
    assert paths.place("data") == paths.config_dir() == Path(os.environ["HYLLY_HOME"])


def test_the_owner_s_own_folders_are_told_apart_from_the_session_s(tmp_path):
    for real in conftest.REAL:
        assert conftest.in_the_owners(Path(real) / "settings.toml") and conftest.in_the_owners(real)
    assert not conftest.in_the_owners(tmp_path) and not conftest.in_the_owners(conftest.SESSION)


def test_no_test_runs_as_root_or_as_an_administrator_whoever_runs_the_suite(monkeypatch):
    """In a container the suite runs as root, and on Windows maybe from an administrator's terminal: every command but
    the doctor would then refuse, and the doctor count one problem more. The tests of root and of an administrator's
    terminal say so themselves (test_assistants.py)."""
    import subprocess

    from hylly import cli, doctor

    assert ("HYLLY_ALLOW_ROOT" in os.environ) == conftest.AS_ROOT  # a hylly a test starts as a process runs as root too
    monkeypatch.setattr(paths.os, "geteuid", lambda: 0, raising=False)  # root, as a container's user is
    monkeypatch.delenv("HYLLY_ALLOW_ROOT", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("nothing asked of this computer's system"))
    assert not paths.is_root() and not paths.running_as_root() and not doctor._elevated()
    cli.main(["user", "list"])  # a command that would refuse as root


def test_no_test_reaches_this_computer_s_start_at_logon_its_services_or_its_7_zip(monkeypatch):
    """A test's settings folder is never the usual one, so nothing a test does can write or take away the start at
    logon, the whole user's; and the system's Task Scheduler, systemd, launchd and 7-Zip answer as nothing would,
    without being asked, so that what a test sees is the same on every computer."""
    import subprocess

    import shutil

    from hylly import archives, dashboard, instance, updates

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("nothing asked of this computer's system"))
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")  # as if this computer had them all
    assert not paths.usual_settings_folder() and not paths.usual_settings_folder(settings_file_counts=False)
    assert instance.autostart_state() is None and instance.logon_found() is None and not instance.system_service()
    assert instance.service_differences() is None  # nor its walled service's files, whatever this computer has
    with pytest.raises(SystemExit):  # refused, as for an assistant's Hylly apart from the usual one
        instance.autostart_off()
    for tool in set(conftest.SYSTEM_TOOLS) - {"_lingering"}:
        assert getattr(instance, tool)("show", "-p", "MainPID").returncode == 1, tool
    assert not instance.system_owns(os.getpid()) and not instance._lingering()  # nor loginctl, which reads this user's
    for system in ("mac", "linux"):  # a restart through the system goes through those ways too, and is refused
        monkeypatch.setattr(instance, "SYSTEM", system)
        with pytest.raises(SystemExit, match="did not restart"):
            instance.restart_through_system()
    assert archives.sevenzip() == ""  # none found, whatever this computer has on its PATH
    assert updates.seven_zip_version("7z") is None and archives.sevenzip_info("7z") == (None, None)
    assert dashboard.logon_start() is None


def test_a_test_of_the_start_at_logon_has_the_usual_folder_with_nothing_of_this_computer_s_beneath(usual_folder, monkeypatch):
    """usual_folder: the test's settings folder taken for the usual one, for the start at logon, never a walled
    service's; subprocess.run runs nothing, and instance's ways of asking the system go through it, as for real."""
    import subprocess

    from hylly import instance

    assert paths.usual_settings_folder() and not paths.usual_settings_folder(settings_file_counts=False)
    assert subprocess.run(["a-program-nobody-has"]).returncode == 1  # not run: no such program would give an answer
    asked = []
    monkeypatch.setattr(subprocess, "run", lambda command, **k: asked.append(command) or subprocess.CompletedProcess(command, 1, "", ""))
    monkeypatch.setattr(instance, "SYSTEM", "windows")
    assert instance.autostart_state() is False and not instance.autostart_off()  # none there, so none taken away
    assert asked and all(command[1:] == ["/Query", "/TN", instance.TASK] for command in asked)


# ---- what only costs time is cut short, and nothing else ---------------------------------------------------------------

def test_a_server_a_test_starts_stops_at_once():
    """A server served with serve_forever(), as the tests' are, looks whether it should stop every conftest.STOP_POLL
    seconds: its shutdown() takes a moment, where Python's half second was waited out at every stop."""
    import inspect
    import socketserver
    import threading
    import time

    assert inspect.signature(socketserver.BaseServer.serve_forever).parameters["poll_interval"].default == conftest.STOP_POLL
    with socketserver.TCPServer(("127.0.0.1", 0), socketserver.BaseRequestHandler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        time.sleep(0.05)  # well into its first wait
        began = time.monotonic()
        server.shutdown()
        assert time.monotonic() - began < 0.25


def test_the_settings_and_the_rules_are_read_once_as_a_command_reads_them():
    """A change written in the middle of a test is read once the test says so (settings.reload, rules.reset), as a new
    command would read it: the helpers that write the settings or the rules do."""
    from hylly import rules, settings

    assert not settings.WATCH and not rules.WATCH and settings.get("server")["port"] == 8770
    paths.settings_file().write_text("[server]\nport = 8771\n", encoding="utf-8")
    assert settings.get("server")["port"] == 8770
    settings.reload()
    assert settings.get("server")["port"] == 8771


def test_watching_looks_at_them_again_at_every_use_as_a_running_server_does(watching):
    from hylly import rules, settings

    assert settings.WATCH and rules.WATCH and settings.get("server")["port"] == 8770
    paths.settings_file().write_text("[server]\nport = 8771\n", encoding="utf-8")
    assert settings.get("server")["port"] == 8771
