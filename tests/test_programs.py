"""Hylly's programs: hylly takes every command, each action a parser of its own with its own --help; hyllyd is the
library server and only that; and they're the two installed. Bare hylly says where to start, and no option may be
shortened."""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

import pytest

from hylly import cli, commands, daemon, exits, paths, runs, shops

REPO = Path(__file__).parents[1]
REAL_JOB_OUTPUT = paths.job_output_dir  # tests/conftest.py stands in for it


def choices() -> set[str]:
    sub = next(a for a in commands.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    return set(sub.choices)  # those listed: not the hidden ones (commands.HIDDEN)


def every_parser(p: argparse.ArgumentParser):
    yield p
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            for q in dict(a.choices.items()).values():
                yield from every_parser(q)


def test_every_command_is_in_one_area():
    placed = [c for cs in commands.AREAS.values() for c in cs]
    assert len(placed) == len(set(placed)) and set(placed) == choices()
    assert not choices() & commands.HIDDEN


@pytest.mark.parametrize("command", sorted(choices()))
def test_a_command_s_own_help_says_what_it_does_and_how_it_s_used(command, capsys, monkeypatch):
    """Its line in the list of commands, as the first words of its own --help; then, for most, an example or two, one
    to a line in a window of 80 columns."""
    monkeypatch.setenv("COLUMNS", "80")
    sub = next(a for a in commands.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    about = "".join(next(a.help for a in sub._choices_actions if a.dest == command).split())
    with pytest.raises(SystemExit):
        cli.main([command, "--help"])
    said = capsys.readouterr().out
    want = about if about.startswith("itch.io") else about[:1].upper() + about[1:]  # a name keeps its own capitals
    assert "".join(said.split("\n\n")[1].split()) == want + "."  # after the usage
    if command in commands.EXAMPLES:
        assert said.endswith("\n\nExamples:\n" + "".join(f"  {line}\n" for line in commands.EXAMPLES[command]))
    assert {"humble", "itchio", "user", "tag", "set", "backup", "rules", "server"} <= set(commands.EXAMPLES)
    assert {name.split()[0] for name in commands.EXAMPLES} <= choices()


@pytest.mark.parametrize("command, first", [("doctor", "What shape"), ("itchio", "itch.io: survey"), ("humble", "Humble Bundle:"),
                                            ("copy", "HyllyCopy:"), ("user", "Users, their roles")])
def test_a_command_s_own_help_starts_with_a_capital_but_a_name_keeps_its_own(command, first, capsys):
    with pytest.raises(SystemExit):
        cli.main([command, "--help"])
    assert capsys.readouterr().out.split("\n\n")[1].startswith(first)


def test_each_action_has_a_help_of_its_own_with_only_its_options(capsys):
    with pytest.raises(SystemExit):
        cli.main(["humble", "download", "--help"])
    said = capsys.readouterr().out
    assert said.startswith("usage: hylly humble download ") and "--kind" in said and "--max-gb" in said
    assert "--snapshot" not in said and "the folders to look in" not in said  # plan's and seed's
    with pytest.raises(SystemExit):
        cli.main(["copy", "check", "--help"])
    said = capsys.readouterr().out
    assert said.startswith("usage: hylly copy check ") and "--everything" not in said


@pytest.mark.parametrize("words", [["humble", "seed", "x", "--max-gb", "3"], ["tag", "list", "--search", "x"],
                                   ["reviews", "export", "--json"], ["theme", "list", "my.toml"], ["itchio", "survey", "--every", "30"],
                                   ["user", "list", "--yes"], ["bell", "test", "--json"], ["server", "autostart", "off", "--port", "1"]])
def test_an_option_another_action_takes_is_refused(words, capsys):
    """Each action is a parser of its own, so an option it doesn't read is a mistake argparse says, not one quietly
    ignored with exit code 0."""
    with pytest.raises(SystemExit) as e:
        cli.main(words)
    assert e.value.code == exits.USAGE and "unrecognized arguments" in capsys.readouterr().err


def test_hylly_alone_says_where_to_start(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main([])
    said = capsys.readouterr()
    assert e.value.code == exits.USAGE and said.out == ""  # no command is a usage error: a script that lost its word fails
    assert said.err.startswith("Hylly: a home library") and "hylly demo" in said.err and "hylly --help" in said.err
    assert "error:" not in said.err and "{" not in said.err


def test_hylly_s_help_lists_the_commands_by_what_they_re_for(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    said = capsys.readouterr().out
    assert "{" not in said.split("\n")[0]  # COMMAND, not every name in braces
    for area, names in commands.AREAS.items():
        assert re.search(rf"^  {re.escape(area)}:\s+{re.escape(', '.join(names))}$", said, re.M), area
    flat = " ".join(said.split())
    assert "To start: hylly demo" in flat and "--dry-run (-n) only says what it would do" in flat
    assert "people forget" in flat and "internal" not in flat


def test_no_option_may_be_shortened(capsys):
    """A half-typed --yes mustn't act as --yes, nor a half-typed option stand for one a later version adds."""
    assert all(not p.allow_abbrev for p in every_parser(commands.build_parser()))
    assert not commands.daemon_parser().allow_abbrev
    for words in (["tag", "remove", "scifi", "--search", "the", "--y"], ["copy", "a", "b", "--ye"], ["server", "status", "--js"],
                  ["--her", "jobs"]):
        with pytest.raises(SystemExit) as e:
            cli.main(words)
        assert e.value.code == exits.USAGE, words
    assert "unrecognized arguments" in capsys.readouterr().err


def test_the_short_options_are_the_usual_ones_and_mean_the_same_on_every_command():
    """Only where the long one is there and common: -n --dry-run, -y --yes, -o --out, -h --help, and -q --quiet and
    -v --verbose should one come; -H --remote before the command. The same letter is never another option."""
    meaning: dict[str, set[str]] = {}
    for p in [*every_parser(commands.build_parser()), commands.daemon_parser()]:
        for a in p._actions:
            short = [o for o in a.option_strings if re.fullmatch(r"-[a-zA-Z]", o)]
            for o in short:
                meaning.setdefault(o, set()).update(x for x in a.option_strings if x.startswith("--"))
    usual = {"-h": {"--help"}, "-n": {"--dry-run"}, "-y": {"--yes"}, "-o": {"--out"}, "-q": {"--quiet"}, "-v": {"--verbose"},
             "-H": {"--remote"}}
    assert {o: longs for o, longs in meaning.items()} == {o: usual[o] for o in meaning}
    assert {"-h", "-n", "-y", "-o", "-H"} <= set(meaning)


def test_the_library_goes_before_the_command_too_as_git_c_does(tmp_path, capsys):
    """hylly --library FOLDER COMMAND is the same as after it; before a command that uses no library, or with another one
    after it, it's a mistake, code 2."""
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "lookup-log.txt").write_text("one\ntwo\n", encoding="utf-8")
    cli.main(["--library", str(tmp_path), "logs", "lookup", "--tail", "1"])
    assert capsys.readouterr().out.splitlines() == ["two"]
    cli.main(["--library", str(tmp_path), "logs", "lookup", "--tail", "1", "--library", str(tmp_path)])  # the same: fine
    assert capsys.readouterr().out.splitlines() == ["two"]
    for words, said in ((["--library", str(tmp_path), "commands"], "doesn't use a library"),
                        (["--library", str(tmp_path), "logs", "--library", str(tmp_path / "other")], "Two libraries")):
        with pytest.raises(SystemExit) as e:
            cli.main(words)
        assert e.value.code == exits.USAGE and said in capsys.readouterr().err
    assert "--library" in [o["option"] for o in commands.commands_data()["options"]]


def test_every_number_an_option_takes_is_checked_as_it_s_read(tmp_path, capsys):
    """A mistyped number is argparse's usage error, exit code 2, with the numbers it takes: never a Python traceback,
    and never after anything was done, as hylly catalog --rebuild --jobs 0 once set the covers aside first and then
    ended in a ValueError, or hylly serve --port 70000 in an OverflowError when it came to listen."""
    plain = [(" ".join(p.prog.split()[1:]), a.option_strings) for p in [*every_parser(commands.build_parser()), commands.daemon_parser()]
             for a in p._actions if a.type is int]
    assert plain == []
    library = tmp_path / "library"
    for words, says in ((["catalog", "--rebuild", "--jobs", "0"], "--jobs: a number from 1 to 64"),
                        (["catalog", "--jobs", "65"], "--jobs: a number from 1 to 64"),
                        (["serve", "--port", "70000", "--no-browser"], "--port: a port, a whole number from 1 to 65535"),
                        (["serve", "--port", "0"], "--port: a port, a whole number from 1 to 65535"),
                        (["demo", str(tmp_path / "demo"), "--port", "-1"], "--port: a port, a whole number from 1 to 65535")):
        with pytest.raises(SystemExit) as e:
            cli.main([*words, "--library", str(library)] if words[0] != "demo" else words)
        assert e.value.code == exits.USAGE and says in capsys.readouterr().err, words
    with pytest.raises(SystemExit) as e:
        daemon.main(["--port", "65536", "--dry-run"])
    assert e.value.code == exits.USAGE and "a whole number from 1 to 65535" in capsys.readouterr().err
    assert not library.exists() and not (tmp_path / "demo").exists()
    assert commands.read_jobs("64") == 64 and commands.port("8770") == 8770
    from hylly import settings

    paths.settings_file().write_text("[server]\nport = 70000\n", encoding="utf-8")  # the settings' port, as they're read
    settings.reload()
    with pytest.raises(SystemExit) as e:
        cli.main(["settings", "check"])
    assert e.value.code == exits.NOT_SET_UP and "[server] port = 70000: a port, a whole number from 1 to 65535" in capsys.readouterr().out


def test_each_command_of_hylly_commands_says_what_it_does_in_its_own_words():
    """Each action's line is its own, as its --help's is: once every action of hylly humble had the command's."""
    rows = commands.commands_data(internal=True)["commands"]
    abouts = [c["about"] for c in rows]
    assert all(abouts) and len(set(abouts)) == len(abouts), [a for a in abouts if abouts.count(a) > 1]


def test_hylly_and_hyllyd_are_the_programs_installed():
    scripts = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts == {"hylly": "hylly.cli:main", "hyllyd": "hylly.daemon:main"}
    assert callable(cli.main) and callable(daemon.main)


def test_hyllyd_takes_only_what_starting_the_server_needs(capsys):
    options = {o for a in commands.daemon_parser()._actions for o in a.option_strings}
    assert options == {"-h", "--help", "--version", "--port", "--lan", "--no-lan", "--no-browser", "--library",
                       "--rules", "--in-background", "-n", "--dry-run"}  # --no-browser only as services set up before pass it
    for words in (["serve"], ["user", "add", "someone"], ["server", "status"]):
        with pytest.raises(SystemExit) as e:
            daemon.main(words)
        assert e.value.code == exits.USAGE
    assert "unrecognized arguments" in capsys.readouterr().err


def test_hyllyd_s_dry_run_says_what_it_would_serve(tmp_path, capsys):
    from hylly import instance, paths, settings

    daemon.main(["--dry-run", "--port", "8810", "--library", str(tmp_path)])  # in the foreground, for that run
    said = capsys.readouterr().out
    assert str(tmp_path) in said and "8810" in said and "127.0.0.1" in said and "Nothing was started" in said
    paths.settings_file().write_text(f"library = '{tmp_path.as_posix()}'\n[server]\nport = 8811\n", encoding="utf-8")
    settings.reload()  # the next run is a process of its own
    daemon.main(["--dry-run", "--in-background"])  # unwatched: the settings
    said = capsys.readouterr().out
    assert "8811" in said and str(instance.log_file()) in said and "Nothing was started" in said


def test_the_servers_walls_hide_the_logins_and_it_starts_as_hyllyd():
    unit = (REPO / "examples" / "hylly.service").read_text(encoding="utf-8")
    hidden = [w for line in unit.splitlines() if line.startswith("InaccessiblePaths=") for w in line.split("=", 1)[1].split()]
    assert "/home/someone/.local/share/hylly/logins" in hidden  # the data folder's: without a -, as every command makes it
    assert re.search(r"^ExecStart=/\S+/bin/hyllyd --in-background$", unit, re.M)
    line = re.search(r"^ExecStart=/\S+/bin/hyllyd (.*)$", unit, re.M)[1].split()
    commands.daemon_parser().parse_args(line)  # the unit's options are hyllyd's


def test_the_walls_let_the_server_and_its_jobs_read_the_alarms_secrets_and_never_change_them(monkeypatch):
    """secrets/, apart from the logins, read only to both, as settings.toml and the rules are: the whole settings folder,
    bound read only, so that what the server could write there, which would steer where the owner's own commands send
    their notes and the token, it can't, nor make a secrets/ of its own there. Named without a -, so that a unit started
    before the folder is there fails, rather than start without the settings. The data, state and cache folders it
    writes, but for the logins and what the jobs from another computer said: each as Hylly has it on Linux, for the
    user the units name, whose home has no folder of before."""
    from hylly import settings

    monkeypatch.setattr(paths, "SYSTEM", "linux")
    monkeypatch.setattr(paths, "job_output_dir", REAL_JOB_OUTPUT)
    monkeypatch.setattr(paths.Path, "home", lambda: paths.Path("/home/someone"))
    for name in ("HYLLY_HOME", *paths.XDG):  # as a system service has it: none of these set
        monkeypatch.delenv(name, raising=False)
    home, folder = "/home/someone", paths.config_dir().as_posix()
    assert folder == "/home/someone/.config/hylly" and settings.secrets_dir().parent == paths.config_dir()
    assert [paths.place(kind).as_posix() for kind in ("data", "state", "cache", "logins")] == [
        f"{home}/.local/share/hylly", f"{home}/.local/state/hylly", f"{home}/.cache/hylly", f"{home}/.local/share/hylly/logins"]
    assert paths.job_output_dir().as_posix() == f"{home}/.local/state/hylly/job-output"
    for name in ("hylly.service", "hylly-jobs.service"):
        lines = (REPO / "examples" / name).read_text(encoding="utf-8").splitlines()
        read_only = [w for line in lines if line.startswith(("ReadOnlyPaths=", "BindReadOnlyPaths=")) for w in line.split("=", 1)[1].split()]
        written = [w for line in lines if line.startswith("BindPaths=") for w in line.split("=", 1)[1].split()]
        hidden = [w for line in lines if line.startswith(("InaccessiblePaths=", "TemporaryFileSystem=")) for w in line.split("=", 1)[1].split()]
        assert folder in read_only and not [w for w in hidden if "secrets" in w or "config" in w], name
        assert {f"{home}/.local/share/hylly", f"{home}/.local/state/hylly", f"{home}/.cache/hylly"} <= set(written), name
        assert "/home/someone/.local/state/hylly/job-output" in hidden and not [w for w in written if "Hylly" in w], name


def test_the_logins_have_a_folder_of_their_own(monkeypatch):
    from hylly import logins, paths
    monkeypatch.delenv(logins.SITES["drivethrurpg"][2], raising=False)
    assert logins.save_session("m" * 32, "drivethrurpg").parent == paths.logins_dir() == paths.config_dir() / "logins"
    assert logins.load_session("drivethrurpg") == "m" * 32


def test_the_reference_has_every_action_by_area_and_hyllyd_too():
    reference = commands.reference_html()
    assert all(f'id="area-{re.sub(r"[^a-z]+", "-", area.lower()).strip("-")}"' in reference for area in commands.AREAS)
    for name, _, _ in commands.leaves():
        assert f'id="cmd-{name.replace(" ", "-")}"' in reference, name
    assert "<code>hylly humble download</code>" in reference and "<code>hylly copy check</code>" in reference
    assert 'id="cmd-hyllyd"' in reference and 'id="asks-first"' in reference and 'id="cmd-internal' not in reference


def test_every_shop_has_the_shop_list_from_the_table_and_a_list_after_its_download():
    """hylly SHOP list [FILE], hylly list [FILE] and a LIST after hylly SHOP download and hylly download: every
    shop's list action comes from the table (commands.shop_lists), none is in a shop's own actions, no option is called
    --list, and each download, hylly download too, takes one word: its LIST."""
    parser = commands.build_parser()
    top = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert "list" in choices()
    for shop in shops.SHOPS.values():
        assert set(commands.actions_of(top.choices[shop.id])) == {*shop.actions, "list"} and "list" not in shop.actions
    for name, p, _ in commands.leaves(parser):
        if name.split()[0] in (*shops.SHOPS, "download"):
            assert not any("--list" in a.option_strings for a in p._actions), name
            if name.endswith("download"):
                assert [a.dest for a in commands.options_of(p) if not a.option_strings] == ["list"], name


# ---- what each table says of a command ---------------------------------------------------------------------------------

# Rows of effects.py whose job is said in words: the command takes its locks itself, so no job of runs.JOBS is held
# around its whole run (cli.job_of says none), and each is named here with why
OWN_LOCKS = {
    "download": "each shop's run of it is a command of its own, which holds that shop's job",
    "server jobs": "each of its jobs has a lock of its own, in server-jobs/",
    "internal inbox": "the pass takes add's lock itself, and leaves the Inboxes to a hylly add that holds it",
    "internal server-job": "the job's own lock, in server-jobs/",
}
# Passed on to the server computer, the commands that start there on their own though they hold no job themselves, as
# cli.STARTED_APART names them, and the answers for some command lines written out: the test's own, so that a slip in
# the code's list or in its rule is found
APART = {"download", "server jobs"}
STARTED_THERE = {  # a command line: whether, passed on, it starts on the server on its own
    "download": True, "download --dry-run": False, "download --json": False, "download -": False,
    "server jobs": True, "server jobs --dry-run": False, "server jobs --json": False,
    "catalog": True, "catalog --dry-run": False, "catalog --json": False, "lookup": True, "lookup --dry-run": False,
    "humble download": True, "humble download --dry-run": False, "humble download -": False, "humble list -": False,
    "itchio survey": True, "itchio survey --dry-run": False, "add x": True, "add x --dry-run": False,
    "jobs stop catalog": False, "tag add x": False, "internal inbox": False,
}
SAMPLE = {"target": "T", "source": "S", "to": "NEW", "folders": "F", "words": "catalog", "list": "L.csv", "file": "F.csv"}


def command_lines() -> list[list[str]]:
    """Every command line the parser takes, by each action: as it comes, with each of its words that may be left out
    given (a LIST, as a file and as -), and each of those with --dry-run, --json, and both; hylly jobs stop with each
    job's name."""
    def given(a: argparse.Action) -> list[str]:
        return ["catalog"] if a.nargs == argparse.REMAINDER else [list(a.choices)[0] if a.choices else SAMPLE.get(a.dest, "x")]

    out = []
    for name, p, _ in commands.leaves(commands.build_parser(), internal=True):
        words = [a for a in commands.options_of(p) if not a.option_strings]
        needed = [w for a in words if a.nargs not in ("?", "*") for w in given(a)]
        bases = [[*name.split(), job] for job in runs.JOBS] if name == "jobs stop" else [[*name.split(), *needed]]
        for base in bases:
            lines = [base, *([*base, w] for a in words if a.nargs in ("?", "*")
                             for w in (["-", "L.csv"] if a.dest in ("list", "file") else given(a)))]
            options = {o for a in p._actions for o in a.option_strings}
            out += [[*line, *more] for line in lines for more in ([], ["--dry-run"], ["--json"], ["--json", "--dry-run"])
                    if set(more) <= options]
    return out


def test_every_command_s_tables_say_the_same_of_it(monkeypatch, capsys):
    """A command's facts are kept in a table each, by the module that uses them: the parser (commands.build_parser),
    effects.EFFECTS (a row for each action, with the job it holds), runs.JOBS (the jobs, which hylly jobs stop takes),
    remote.LOCAL and LOCAL_JOBS (what runs here), and cli's HANDLERS, NEEDS_THE_LIBRARY, job_of and asks_a_shop. For
    every command line the parser takes, they say the same: the job a run holds is its row's (none for a dry run, and
    for what takes its locks itself, OWN_LOCKS), one of runs.JOBS; hylly jobs stop takes each job, and each is held by
    some command; only what runs here holds a job whose stop stays here (LOCAL_JOBS); a command whose run checks the
    library with its lock has its dry run check it too; and passed on to the server computer, a run starts there on its
    own just when it holds a job, or is one of APART, but never a dry run, nor one with its LIST on standard input or
    with --json (STARTED_THERE has some written out)."""
    from hylly import effects, remote

    sent, ran, held, checked = [], [], set(), set()
    parsers = {name: p for name, p, _ in commands.leaves(commands.build_parser(), internal=True)}
    monkeypatch.setattr(remote, "forward", lambda argv, job: sent.append(job) or 0)
    monkeypatch.setattr(cli, "_run", lambda args: ran.append(args.leaf))
    monkeypatch.setenv("HYLLY_REMOTE", "server")
    lines = command_lines()
    assert len(lines) > 250 and ["download", "--dry-run"] in lines and ["humble", "download", "-"] in lines
    for words in lines:
        args = commands.build_parser().parse_args(words)
        row, job, dry = effects.EFFECTS[args.leaf], cli.job_of(args), bool(getattr(args, "dry_run", False))
        assert callable(getattr(cli, cli.HANDLERS[args.command])), words
        if dry or args.leaf in OWN_LOCKS:
            assert job is None and (dry or row["job"]), words
        else:
            assert job == row["job"], words
        assert job is None or job in runs.JOBS, words
        held.add(job)
        assert (job in (None, *remote.LOCAL_JOBS)) if args.command in remote.LOCAL else (job not in remote.LOCAL_JOBS), words
        if job and job not in ("copy", "backup"):  # its real run checks the library as it takes its lock (cli._run)
            assert args.command in cli.NEEDS_THE_LIBRARY, words
        if args.command in cli.NEEDS_THE_LIBRARY:
            assert any("--library" in a.option_strings for a in parsers[args.leaf]._actions), words
        if shop := cli.asks_a_shop(args):  # a shop's list, or a dry run that reads the site: it waits its turn
            assert shop == args.command and shop in shops.SHOPS, words
            assert args.action == "list" or (dry and row["dry_run"] == effects.SITE), words
        sent.clear(), ran.clear()
        try:
            cli.main(words)
        except SystemExit as e:
            if e.code == exits.USAGE and not sent:  # a FILE on this computer, which the server can't see: said, not run
                assert "Nothing was run" in capsys.readouterr().err, words
                continue
        capsys.readouterr()
        if args.command in remote.LOCAL or (args.leaf == "jobs stop" and args.job in remote.LOCAL_JOBS):
            assert (ran, sent) == ([args.leaf], []), words  # here, where it was typed
            continue
        line = " ".join(words)
        rule = ((job is not None or args.leaf in APART) and not dry and not getattr(args, "json", False)
                and getattr(args, "list", None) != "-")
        assert ran == [] and sent == [rule] and STARTED_THERE.get(line, rule) == rule, words
        checked.add(line)
    assert set(STARTED_THERE) <= checked and set(cli.STARTED_APART) == APART
    assert held - {None} == set(runs.JOBS)  # every job is held by some command: none hylly jobs stop takes for nothing
    assert list(next(a for a in parsers["jobs stop"]._actions if a.dest == "job").choices) == list(runs.JOBS)
    named = {name.split()[0] for name, _, _ in commands.leaves(internal=True)}
    assert set(cli.HANDLERS) == named and set(remote.LOCAL) <= named and cli.NEEDS_THE_LIBRARY <= named
    assert set(remote.LOCAL_JOBS) <= set(runs.JOBS) and set(OWN_LOCKS) <= set(effects.EFFECTS)
