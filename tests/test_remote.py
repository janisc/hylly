"""Commands passed on to the server computer, which owns the library's data (remote.py, docs/architecture.md)."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from hylly import cli, exits, paths, remote


@pytest.fixture
def on_another_computer(monkeypatch):
    """This computer's settings name the server, and ssh is a stand-in that says what it was asked."""
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text('[remote]\nssh = "server"\nprograms = "~/.local/bin"\n', encoding="utf-8")
    asked = []

    def ssh(command, **kwargs):
        asked.append(command)
        return subprocess.CompletedProcess(command, 3)

    monkeypatch.setattr(remote.subprocess, "run", ssh)
    return asked


def test_nothing_is_passed_on_without_the_setting():
    assert remote.server() is None and not remote.wanted("doctor")


def test_a_command_is_passed_on_with_its_words_and_says_how_it_ended(on_another_computer, capsys):
    with pytest.raises(SystemExit) as ended:
        cli.main(["tag", "add", "Two words", "--search", "it's"])
    assert ended.value.code == 3  # the command's own exit code, from over there
    said = capsys.readouterr()
    assert said.err == "Running on server.\n" and said.out == ""  # where it runs, on standard error: the output is the server's
    command = on_another_computer[0]
    assert os.path.basename(command[0]).lower() in ("ssh", "ssh.exe") and command[-3:-1] == ["--", "server"] and "-o" in command
    assert command[-1] == "~/.local/bin/hylly --here tag add 'Two words' --search 'it'\"'\"'s'"  # ~ is the server's home; the words quoted


def test_the_server_is_named_by_remote_then_hylly_remote_then_the_settings(on_another_computer, monkeypatch, capsys):
    """As docker -H and DOCKER_HOST: the option wins over the variable, which wins over [remote] ssh; hylly's own options,
    before the command, aren't passed on."""
    with pytest.raises(SystemExit):
        cli.main(["-H", "other", "people", "list"])
    assert on_another_computer[-1][-3:] == ["--", "other", "~/.local/bin/hylly --here people list"]
    monkeypatch.setenv("HYLLY_REMOTE", "from-the-variable")
    with pytest.raises(SystemExit):
        cli.main(["people", "list"])
    assert on_another_computer[-1][-2] == "from-the-variable"
    with pytest.raises(SystemExit):
        cli.main(["--remote=named", "people", "list", "--json"])
    assert on_another_computer[-1][-2:] == ["named", "~/.local/bin/hylly --here people list --json"]
    assert "Running on named." in capsys.readouterr().err
    assert remote.server() is not None and remote.choose(None) is None  # each command's own choice, cleared after it


def test_hylly_remote_alone_names_the_server_without_the_setting(monkeypatch):
    asked = []
    monkeypatch.setattr(remote.subprocess, "run", lambda command, **kw: asked.append(command) or subprocess.CompletedProcess(command, 0))
    monkeypatch.setenv("HYLLY_REMOTE", "server")
    with pytest.raises(SystemExit) as ended:
        cli.main(["jobs"])
    assert ended.value.code == 0 and asked[0][-1] == "hylly --here jobs"


@pytest.mark.parametrize("host", ["-oProxyCommand=sh", "two words", "a;b"])
def test_a_server_named_on_the_command_line_that_ssh_would_take_for_more_is_refused(host, monkeypatch, capsys):
    monkeypatch.setattr(remote.subprocess, "run", lambda *a, **k: pytest.fail("ssh must not be asked"))
    with pytest.raises(SystemExit) as ended:
        cli.main([f"--remote={host}", "jobs"])
    assert ended.value.code == 5 and "-H should be the server computer's name" in capsys.readouterr().err


def test_only_hylly_s_own_options_before_the_command_are_left_out_of_what_s_passed_on():
    assert remote.words(["-H", "srv", "tag", "add", "x"]) == ["tag", "add", "x"]
    assert remote.words(["-Hsrv", "--here", "jobs"]) == ["jobs"]
    assert remote.words(["--remote", "srv", "tag", "list", "--here"]) == ["tag", "list", "--here"]  # after the command: its own


def test_a_long_job_starts_on_its_own_there(on_another_computer):
    with pytest.raises(SystemExit):
        cli.main(["catalog"])
    assert on_another_computer[0][-1] == "~/.local/bin/hylly --here internal job catalog"
    with pytest.raises(SystemExit):
        cli.main(["jobs", "stop", "catalog"])  # stopping one isn't a job
    assert on_another_computer[1][-1] == "~/.local/bin/hylly --here jobs stop catalog"


@pytest.mark.parametrize("words", [["catalog", "--dry-run"], ["download", "-n"], ["humble", "download", "--dry-run"],
                                   ["lookup", "-n"], ["server", "jobs", "-n"]])
def test_a_dry_run_of_a_long_job_runs_while_ssh_does(words, on_another_computer):
    """A dry run holds no job and changes nothing (cli.job_of), so it isn't started there on its own as a long job,
    which hylly jobs stop couldn't find: it runs, and ends, with ssh."""
    with pytest.raises(SystemExit):
        cli.main(words)
    assert on_another_computer[0][-1] == "~/.local/bin/hylly --here " + " ".join(words)


@pytest.mark.parametrize("words", [["download"], ["server", "jobs"]])
def test_what_holds_no_job_itself_but_is_long_starts_there_on_its_own(words, on_another_computer):
    """hylly download, each of whose shops' runs is a job, and hylly server jobs, whose own long jobs (the nightly copy,
    the Inbox's pass) write where it writes: attached to ssh, they would have its terminal, which ends with it, or its
    pipes, which ssh waits on (cli.STARTED_APART)."""
    with pytest.raises(SystemExit):
        cli.main(words)
    assert on_another_computer[0][-1] == "~/.local/bin/hylly --here internal job " + " ".join(words)


def test_whats_about_this_computer_stays_here(on_another_computer):
    cli.main(["commands"])
    for words in (["backup", "--help"], ["restore", "--help"], ["-H", "other", "copy", "--help"]):
        with pytest.raises(SystemExit):
            cli.main(words)
    assert on_another_computer == []


def test_here_keeps_any_command_here(on_another_computer, capsys, monkeypatch):
    monkeypatch.setenv("HYLLY_REMOTE", "server")
    cli.main(["--here", "people", "list"])
    said = capsys.readouterr()
    assert on_another_computer == [] and "Nothing is kept" in said.out and "Running on" not in said.err
    with pytest.raises(SystemExit) as ended:
        cli.main(["--here", "-H", "server", "jobs"])  # one or the other
    assert ended.value.code == 2


@pytest.mark.parametrize("host", ["-oProxyCommand=sh -c id", "server; id", "two words", "-l root"])
def test_a_server_name_that_ssh_would_take_for_more_is_refused(host):
    """The settings name the server; whatever can write them mustn't choose ssh's options, or more commands."""
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text(f'[remote]\nssh = "{host}"\n', encoding="utf-8")
    with pytest.raises(SystemExit, match="as ssh knows it"):
        remote.server()


def test_the_programs_folder_is_quoted_but_its_home_kept():
    assert remote.command_line(["jobs"], False, "~/my programs") == "~/'my programs/hylly' --here jobs"
    assert remote.command_line(["jobs"], False, "/opt/x; id") == "'/opt/x; id/hylly' --here jobs"
    assert remote.command_line(["jobs"], False, "") == "hylly --here jobs"
    assert remote.command_line(["catalog"], True, "") == "hylly --here internal job catalog"


@pytest.mark.skipif(os.name != "nt", reason="Windows finds a program in the current folder first")
def test_an_ssh_in_the_current_folder_isnt_run(tmp_path, monkeypatch):
    (tmp_path / "ssh.exe").write_bytes(b"MZ not ssh")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f".{os.pathsep}{os.environ.get('PATH', '')}")
    try:
        found = remote._ssh()
    except FileNotFoundError:
        return  # no ssh on this computer at all: nothing found is right too
    assert os.path.isabs(found) and os.path.dirname(found) != str(tmp_path)


def test_no_ssh_says_so(monkeypatch):
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text('[remote]\nssh = "server"\n', encoding="utf-8")

    def none(*args, **kwargs):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(remote.subprocess, "run", none)
    with pytest.raises(SystemExit) as ended:
        cli.main(["doctor"])
    assert ended.value.code != 0


def test_a_job_started_from_elsewhere_runs_on_its_own_and_is_followed_until_it_ends(capfdbinary):
    code = remote.as_job(["commands"])
    said = capfdbinary.readouterr()
    out, note = said.out.decode("utf-8", "replace"), said.err.decode("utf-8", "replace")
    assert code == 0 and "Started on" in note and "hylly jobs stop NAME" in note  # a note: the output is the job's
    kept = [p for p in paths.job_output_dir().glob("*-commands-*.txt") if not p.name.endswith(".err.txt")]
    said = kept[0].read_text(encoding="utf-8", errors="replace")
    assert len(kept) == 1 and len(said) > 500 and said.strip().splitlines()[-1] in out  # all it said, here too
    assert not paths.job_output_dir().is_relative_to(paths.config_dir())  # out of the settings folder's way


def test_a_job_from_elsewhere_keeps_its_result_and_what_it_says_on_the_way_apart(capfdbinary, tmp_path):
    """Its standard output here has its result alone, as the command run here would: hylly X --json > file from another
    computer gets the one object, and its notes go to standard error, each in a file of its own there too."""
    code = remote.as_job(["catalog", "-n", "--json", "--library", str(tmp_path / "not there")])  # refused: no library there
    said = capfdbinary.readouterr()
    out, note = said.out.decode("utf-8", "replace"), said.err.decode("utf-8", "replace")
    found = json.loads(out)  # all of standard output is the one object
    assert code == found["exit_code"] == 5 and "No Hylly library" in found["message"] and "No Hylly library" in note
    result = [p for p in paths.job_output_dir().glob("*-catalog-*.txt") if not p.name.endswith(".err.txt")]
    assert len(result) == 1 and json.loads(result[0].read_text(encoding="utf-8")) == found
    assert "No Hylly library" in result[0].with_suffix(".err.txt").read_text(encoding="utf-8")


LONG_JOBS = [["catalog"], ["download", "-n"], ["humble", "download"], ["storybundle", "download"], ["drivethrurpg", "download"],
             ["itchio", "download"], ["add", "ebooks"], ["lookup"]]


@pytest.mark.parametrize("words", LONG_JOBS)
def test_a_library_given_before_a_long_job_reaches_the_server_in_a_form_it_takes(words, on_another_computer, tmp_path):
    """hylly --library F CMD, passed on as a job of its own (hylly --here internal job ...), comes there as CMD --library F:
    internal job takes nothing before its command's own word."""
    with pytest.raises(SystemExit):
        cli.main(["--library", str(tmp_path), *words])
    import shlex

    sent = shlex.split(on_another_computer[-1][-1])  # what ssh runs there: hylly --here [internal job] WORDS
    outer = cli.build_parser().parse_args(sent[1:])
    inner = outer.words if outer.leaf == "internal job" else sent[2:]
    args = cli.build_parser().parse_args(["--here", *inner])  # as the job itself is started there (remote.as_job)
    assert args.library == str(tmp_path) and args.leaf == cli.build_parser().parse_args(words).leaf


def test_a_link_in_a_jobs_way_leads_nowhere(tmp_path, capfdbinary):
    """What a walled server might leave where the next job's output goes: a link to a file of yours, under the names
    the coming seconds give, and the folder itself turned into a link to another of yours. Neither is written to, nor
    emptied."""
    import os
    from datetime import datetime, timedelta

    from hylly.storage import is_link

    yours = tmp_path / "yours.pth"
    yours.write_text("as it was", encoding="utf-8")
    folder = paths.job_output_dir()
    folder.mkdir(parents=True)
    now = datetime.now()
    try:
        for s in range(5):
            os.symlink(yours, folder / f"{now + timedelta(seconds=s):%Y-%m-%d_%H%M%S}-commands.txt")
    except OSError:
        pytest.skip("this computer won't make links here")
    assert remote.as_job(["commands"]) == 0
    capfdbinary.readouterr()
    assert yours.read_text(encoding="utf-8") == "as it was"  # the job's output went to a file of its own
    notes = tmp_path / "notes"
    notes.mkdir()
    for n in range(OUTPUTS := remote.OUTPUTS_KEPT + 5):
        (notes / f"2020-01-01_0000{n:02d}-mine.txt").write_text("mine", encoding="utf-8")
    for link in folder.iterdir():
        link.unlink() if is_link(link) else None
    for left in folder.iterdir():
        left.unlink()
    folder.rmdir()
    os.symlink(notes, folder, target_is_directory=True)
    with pytest.raises(SystemExit):
        remote.as_job(["commands"])
    assert len(list(notes.iterdir())) == OUTPUTS  # nothing of yours removed


def test_the_server_side_starts_a_long_job_on_its_own(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(remote, "as_job", lambda argv: seen.append(argv) or 5)
    with pytest.raises(SystemExit) as ended:
        cli.main(["--here", "internal", "job", "catalog", "--lookup"])
    assert ended.value.code == 5 and seen == [["catalog", "--lookup"]]
    cli.main(["--here", "internal", "job", "--dry-run", "catalog"])
    assert "It would start hylly catalog on its own" in capsys.readouterr().out and len(seen) == 1



def test_a_backups_note_goes_to_the_server_when_this_computer_cant_write_the_library(on_another_computer, monkeypatch, tmp_path):
    import json
    from pathlib import Path
    from types import SimpleNamespace

    from hylly import backup

    sent = []
    monkeypatch.setattr(remote, "ask", lambda argv, text="", timeout=0: sent.append((argv, json.loads(text))))

    def read_only(root, note):
        raise PermissionError("the share is read-only")

    monkeypatch.setattr(backup, "keep_note", read_only)
    done = SimpleNamespace(listing=SimpleNamespace(files=[1, 2, 3]), size=30, failed=[])
    (tmp_path / "disk").mkdir()  # the backup's, its note written there only
    backup.finish(tmp_path / "library", tmp_path / "disk", done, Path("2026-11-09.zip"), 0)
    assert sent[0][0] == ["internal", "backup-noted", str(tmp_path / "disk")] and sent[0][1]["files"] == 3 and sent[0][1]["saved"] == "2026-11-09.zip"


def test_a_question_for_the_server_says_what_it_answered_or_why_not(on_another_computer, monkeypatch):
    answers = iter([subprocess.CompletedProcess([], 0, b"Copied: there.\n", b""), subprocess.CompletedProcess([], 3, b"", b"not here")])
    monkeypatch.setattr(remote.subprocess, "run", lambda command, **kwargs: on_another_computer.append(command) or next(answers))
    assert remote.ask(["internal", "fresh-copy"]) == "Copied: there.\n"
    assert on_another_computer[-1][-1] == "~/.local/bin/hylly --here internal fresh-copy"
    with pytest.raises(OSError, match="server said no: not here"):
        remote.ask(["internal", "fresh-copy"])


def test_the_server_keeps_another_computers_note_and_nothing_else(monkeypatch, tmp_path, capsys):
    import io
    import json

    from hylly import backup

    root = tmp_path / "library"
    (root / "state").mkdir(parents=True)
    note = {**{key: 1 for key in backup.NOTE_KEYS}, "when": "2026-11-09T22:00:00", "target": "D:/disk", "computer": "pc", "saved": "x.zip"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(note)))
    cli.main(["internal", "backup-noted", "D:/disk", "--library", str(root)])
    assert backup.last(root)["files"] == 1 and "Noted" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"files": 1, "secret": "x"})))
    with pytest.raises(SystemExit):
        cli.main(["internal", "backup-noted", "D:/disk", "--library", str(root)])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({**note, "files": 7})))
    cli.main(["internal", "backup-noted", "D:/disk", "--library", str(root), "--dry-run"])  # only checked
    assert backup.last(root)["files"] == 1 and "would be kept" in capsys.readouterr().out


def test_every_shop_of_a_download_run_here_runs_here_too(on_another_computer, monkeypatch, tmp_path):
    """hylly download runs each shop's download as a command of its own: where the download runs, so do they."""
    ran = []
    for name in ("cmd_humble_download", "cmd_storybundle", "cmd_drivethrurpg", "cmd_itchio"):
        monkeypatch.setattr(cli, name, lambda args, name=name: ran.append(name))
    (tmp_path / "login").write_text("x", encoding="utf-8")
    monkeypatch.setattr(cli, "session_path", lambda shop: tmp_path / "login")  # every shop has a login
    cli.main(["--here", "download", "--library", str(tmp_path)])
    assert on_another_computer == [] and len(ran) == 4
    cli.main(["--here", "download", "--dry-run", "--library", str(tmp_path)])  # a dry run too
    assert on_another_computer == [] and len(ran) == 8


def test_the_stop_of_a_copy_or_a_backup_stays_where_they_run(monkeypatch, capsys):
    """A copy, a backup and a restore run where they're typed, so their stop does too; the other jobs' go to the server."""
    sent = []
    monkeypatch.setattr(remote, "forward", lambda argv, job: sent.append(argv) or 0)
    monkeypatch.setenv("HYLLY_REMOTE", "server")
    for job in ("copy", "backup"):
        cli.main(["jobs", "stop", job])
        assert "is running on this computer" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["jobs", "stop", "lookup"])
    with pytest.raises(SystemExit):
        cli.main(["jobs"])
    assert sent == [["jobs", "stop", "lookup"], ["jobs"]]
    capsys.readouterr()
    cli.main(["-H", "other", "jobs", "stop", "copy"])  # said, so that -H isn't left out in silence
    said = capsys.readouterr()
    assert len(sent) == 2 and "runs on the computer it's typed on, so its stop is here" in said.err
    cli.main(["commands", "--json"])
    data = {c["command"]: c["runs"] for c in json.loads(capsys.readouterr().out)["commands"]}
    assert "copy or backup here" in data["hylly jobs stop"]


@pytest.mark.parametrize("words", [["-H", "", "jobs"], ["--remote=", "jobs"], ["--remote", " ", "jobs"]])
def test_a_server_named_by_no_name_is_refused_not_run_here(words, monkeypatch, capsys):
    monkeypatch.setattr(remote.subprocess, "run", lambda *a, **k: pytest.fail("nothing is run"))
    with pytest.raises(SystemExit) as ended:
        cli.main(words)
    assert ended.value.code == 2 and "names no server computer" in capsys.readouterr().err


def test_hylly_remote_set_and_empty_is_refused(monkeypatch, capsys):
    monkeypatch.setenv("HYLLY_REMOTE", "")
    with pytest.raises(SystemExit) as ended:
        cli.main(["jobs"])
    assert ended.value.code == 5 and "HYLLY_REMOTE, set and empty," in capsys.readouterr().err
    cli.main(["--here", "jobs"])  # --here needs no server at all


def test_the_stop_of_a_copy_runs_here_whatever_hylly_remote_says(monkeypatch, capsys):
    """Its note about -H doesn't ask whether the name will do: an empty HYLLY_REMOTE is refused for what goes to a server,
    not for a stop that runs here all the same."""
    monkeypatch.setattr(remote, "forward", lambda argv, job: pytest.fail("nothing is passed on"))
    monkeypatch.setenv("HYLLY_REMOTE", "")
    cli.main(["jobs", "stop", "copy"])
    assert "is running on this computer" in capsys.readouterr().out
    monkeypatch.setenv("HYLLY_REMOTE", "server")
    cli.main(["jobs", "stop", "backup"])
    assert "its stop is here" in capsys.readouterr().err


def test_a_mistake_elsewhere_in_the_settings_doesn_t_stop_a_command_going_to_the_server(on_another_computer, capsys):
    """Only [remote] is read to know where a command runs: the server has settings of its own."""
    paths.settings_file().write_text('[remote]\nssh = "server"\n\n[reader]\ncache_gb = "lots"\n', encoding="utf-8")
    with pytest.raises(SystemExit) as ended:
        cli.main(["doctor", "--json"])
    assert ended.value.code == 3 and on_another_computer  # passed on, and the server's code came back
    paths.settings_file().write_text("[remote]\nssh = 3\n", encoding="utf-8")
    with pytest.raises(SystemExit) as ended:
        cli.main(["people", "list"])
    assert ended.value.code == 5 and "[remote] ssh = 3: a text in quotes" in capsys.readouterr().err


def test_the_doctor_says_what_s_wrong_with_settings_it_can_t_read(monkeypatch, capsys):
    """A wrong value, or a file that isn't TOML at all: the doctor runs here and says so in its report, JSON and all, where
    it used to stop with code 5 and nothing on standard output."""
    from hylly import settings

    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    for text in ('[reader]\ncache_gb = "lots"\n', "this isn't toml\n"):
        paths.settings_file().write_text(text, encoding="utf-8")
        settings.reload()
        with pytest.raises(SystemExit) as ended:
            cli.main(["doctor", "--json"])
        found = json.loads(capsys.readouterr().out)
        assert ended.value.code == 1 and any(c["what"] == "Settings" and c["state"] == "problem" for c in found["checks"])


@pytest.mark.parametrize("named", ["ssh", "empty"])
def test_the_doctor_counts_a_server_named_wrong_as_a_problem_with_what_to_write(named, monkeypatch, capsys):
    """It runs here, as every other command stops at the name, and says what's wrong as a problem, with the next step:
    exit code 1, not a clean bill of health."""
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    if named == "ssh":
        paths.settings_file().write_text("[remote]\nssh = '-bad host'\n", encoding="utf-8")
    else:
        monkeypatch.setenv("HYLLY_REMOTE", "")
    monkeypatch.setattr(remote, "forward", lambda *a, **k: pytest.fail("nothing is passed on"))
    with pytest.raises(SystemExit) as ended:
        cli.main(["doctor", "--json"])
    found = json.loads(capsys.readouterr().out)
    (row,) = [c for c in found["checks"] if c["what"] == "Remote"]
    assert ended.value.code == 1 and found["problems"] >= 1 and row["state"] == "problem" and "[remote] ssh" in row["next"]


def test_a_library_moved_after_the_command_goes_before_a_double_dash_of_its_own():
    assert remote.words(["--library", "L", "tag", "add", "x", "--search", "y", "--", "-odd"]) == [
        "tag", "add", "x", "--search", "y", "--library", "L", "--", "-odd"]
    assert remote.words(["-H", "srv", "--library=L", "catalog"]) == ["catalog", "--library=L"]


def test_a_server_ssh_can_t_reach_ends_here_with_the_local_object(on_another_computer, monkeypatch, capsys):
    """ssh's own 255: nothing ran there, so there's no object from there; this run gives its own, code 6."""
    monkeypatch.setattr(remote.subprocess, "run", lambda command, **kw: subprocess.CompletedProcess(command, 255))
    with pytest.raises(SystemExit) as ended:
        cli.main(["catalog", "--json"])
    found = json.loads(capsys.readouterr().out)
    assert ended.value.code == found["exit_code"] == 6 and "couldn't reach the server computer, server" in found["message"]


def test_a_server_ssh_can_t_reach_is_said_with_what_names_it_and_the_way_to_run_it_here(on_another_computer, monkeypatch, capsys):
    """ssh's own 255 is in no table of Hylly's: one line says that the command was passed on, to which server, because
    of what (the settings, HYLLY_REMOTE or -H), and that --here runs it here; code 6."""
    monkeypatch.setattr(remote.subprocess, "run", lambda command, **kw: subprocess.CompletedProcess(command, 255))
    for words, names in ((["tag", "list"], f"server, that [remote] ssh in {paths.settings_file()} names"),
                         (["-H", "elsewhere", "tag", "list"], "elsewhere, that -H names")):
        with pytest.raises(SystemExit) as ended:
            cli.main(words)
        said = capsys.readouterr().err
        assert ended.value.code == exits.LOCAL and f"ssh couldn't reach the server computer, {names}, or was refused" in said
        assert "hylly --here runs a command on this computer instead" in said and said.startswith("Running on ")
    monkeypatch.setenv("HYLLY_REMOTE", "another")
    with pytest.raises(SystemExit) as ended:
        cli.main(["tag", "list"])
    assert ended.value.code == exits.LOCAL and "the server computer, another, that HYLLY_REMOTE names" in capsys.readouterr().err



def test_a_windows_server_refuses_what_came_quoted_for_another_shell(monkeypatch, capsys):
    """[remote] needs a Linux or macOS server computer. Windows' ssh server hands a passed-on command to cmd.exe,
    where the quotes put round a word for a Unix shell are letters of it. A Windows server tells it by what only passing
    on does (--here first, in an ssh session) and a word with a single quote at its start or end, or a long job, which
    would end with the connection, and refuses it: nothing is run. What needs no quoting arrives as typed, and runs."""
    line = remote.command_line(["tag", "add", "Two words", "--search", "a"], False, "")
    as_cmd_reads_it = line.split()[1:]  # no double quotes in it: split at the spaces, and the single quotes kept
    assert as_cmd_reads_it == ["--here", "tag", "add", "'Two", "words'", "--search", "a"]
    monkeypatch.setattr(paths, "WINDOWS", True)
    monkeypatch.delenv("SSH_CLIENT", raising=False)  # set when the suite itself runs over ssh
    monkeypatch.setenv("SSH_CONNECTION", "192.0.2.20 50000 192.0.2.10 22")
    assert remote.from_another_shell(as_cmd_reads_it)
    assert remote.from_another_shell(["--here", "internal", "job", "catalog"])  # a long job
    assert not remote.from_another_shell(["--here", "jobs"])  # nothing to quote: as typed
    assert not remote.from_another_shell(["tag", "add", "'Two", "words'"])  # typed at this computer: not passed on
    ran = []
    monkeypatch.setattr(cli, "cmd_tag", lambda args: ran.append(args))
    with pytest.raises(SystemExit) as e:
        cli.main(as_cmd_reads_it)
    assert e.value.code == exits.NOT_SET_UP and ran == [] and "needs a Linux or macOS server computer" in capsys.readouterr().err
    monkeypatch.delenv("SSH_CONNECTION")
    assert not remote.from_another_shell(as_cmd_reads_it)  # not over ssh: someone at this computer itself
    monkeypatch.setattr(paths, "WINDOWS", False)
    monkeypatch.setenv("SSH_CONNECTION", "192.0.2.20 50000 192.0.2.10 22")
    assert not remote.from_another_shell(as_cmd_reads_it)  # Linux or macOS: the shell took the quotes away


def test_a_shop_list_goes_to_and_from_the_server_through_ssh_s_own_streams(on_another_computer, capsys, tmp_path):
    """The shop list on the server computer, which owns the library's data: - writes the list to standard output and a
    LIST of - is read from standard input, through ssh (a download given its list so runs as long as ssh does, as a job
    started on its own would have no standard input); a file of this computer's, which the server can't see, is
    refused with what to type instead, nothing run."""
    for words in (["humble", "list", "-", "--kind", "comic"], ["list", "-"]):
        with pytest.raises(SystemExit):
            cli.main(words)
        assert on_another_computer.pop()[-1] == "~/.local/bin/hylly --here " + " ".join(words)
    with pytest.raises(SystemExit):
        cli.main(["humble", "download", "-"])
    assert on_another_computer.pop()[-1] == "~/.local/bin/hylly --here humble download -"  # not a job of its own
    with pytest.raises(SystemExit):
        cli.main(["humble", "download"])
    assert on_another_computer.pop()[-1] == "~/.local/bin/hylly --here internal job humble download"  # as ever
    for words, said in ((["humble", "list"], "would write humble-list.csv there: hylly humble list - > humble-list.csv "
                                             "writes it here"),
                        (["list", "mine.csv"], "would write mine.csv there: hylly list - > mine.csv writes it here"),
                        (["download", "mine.csv", "--dry-run"], "can't see mine.csv on this one: hylly download - --dry-run "
                                                                "< mine.csv sends it there")):
        with pytest.raises(SystemExit) as refused:
            cli.main(words)
        assert refused.value.code == exits.USAGE and said in str(refused.value) and on_another_computer == [], words
        assert str(refused.value).endswith("Nothing was run.")
    with pytest.raises(SystemExit) as refused:  # quoted as it is to be typed
        cli.main(["humble", "list", "--title", "Lantern Wolf"])
    assert "hylly humble list --title 'Lantern Wolf' - > humble-list.csv writes it here" in str(refused.value)
    capsys.readouterr()
