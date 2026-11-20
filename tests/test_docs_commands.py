"""Every hylly and hyllyd command the docs and the commands' own help write parses: a command that stopped existing can't
stay in them unnoticed, nor an option written alone (`--stop`) that no command takes. The help page's lists of commands
(what takes --json, --library and --rules, what asks first, what stays here) are made from the parser
(helppage.command_lists); the lists still written by hand, in the playbooks, the settings example and the architecture,
are held to it here."""

from __future__ import annotations

import contextlib
import html
import io
import re
import shlex
from pathlib import Path

import pytest

from hylly import cli, commands as words, paths
from hylly.commands import EXAMPLES

REPO = Path(__file__).parents[1]
SKILLS = sorted(p.relative_to(REPO).as_posix() for p in (REPO / ".claude" / "skills").glob("*/SKILL.md"))  # every one
PLACES = ["README.md", "AGENTS.md", "CONTRIBUTING.md", "settings.example.toml", "src/hylly/help.html", "docs/architecture.md",
          "docs/catalog.md", "docs/database.md", "docs/downloads.md", "docs/lookups.md", "docs/rules.md", "docs/glossary.md",
          "docs/playbooks.md", "docs/ways-to-run-it.md", *SKILLS]
STAND_INS = {"SITE": "humble", "SHOP": "humble", "NAME": "someone", "FOLDER": "own", "TAG": "a-tag", "PLACE": "own"}
REFUSED: dict[tuple[str, str], str] = {  # (place, command): why the docs show it as refused on purpose
    ("src/hylly/help.html", "humble seed FOLDER --limit 5"): "an option of another action",
    ("src/hylly/help.html", "backup TARGET --dry"): "an option shortened",
}
# Options of other programs that the docs write alone: rsync's in architecture.md, and uv's in the help, for the install
# with another Python.
OTHERS = {"--delete": "rsync's", "-c": "rsync's", "--partial": "rsync's", "--append-verify": "rsync's",
          "--python": "uv's", "--force": "uv's"}
PLACEHOLDER = re.compile(r"<([^<>\s]+)>")  # <id>, <folders>: a value of the reader's own
MORE = re.compile(r"\s*(?:…|\.\.\.)\s*$")  # FOLDER …: more of the same
TOP = {name for names in words.AREAS.values() for name in names}


def commands(text: str, program: str = "hylly") -> list[str]:
    """What follows the program's name wherever the text writes a command: in `...` or <code>, on a line of its own,
    or in a <pre> block, with any <i> or <b> in it left out."""
    name = re.escape(program)
    text = re.sub(r"</?(?:i|b|em|var)>", "", text)  # hylly jobs stop <i>NAME</i> is as much a command as without them
    found = re.findall(rf"`(?:uv run )?{name} ([^`\n]+)`", text)
    found += [html.unescape(c) for c in re.findall(rf"<code>(?:uv run )?{name} ([^<\n]+)</code>", text)]
    found += [html.unescape(c) for c in re.findall(rf"^(?:uv run )?{name} ([^<\n]+)$", text, re.M)]
    for block in re.findall(r"<pre>(.*?)</pre>", text, re.S):
        found += [html.unescape(c) for c in re.findall(rf"(?:^|\n)\s*(?:uv run )?{name} ([^\n]+)", block)]
    return found


def text_of(place: str) -> str:
    return (REPO / place).read_text(encoding="utf-8")


def everything(program: str = "hylly") -> list[tuple[str, str]]:
    """Each command as written, without its comment, or the file a shell feeds it (< password.txt) or writes what it says
    to (> list.csv), which aren't its words."""
    return sorted({(place, command.split(" #")[0].split(" < ")[0].split(" > ")[0].strip()) for place in PLACES
                   for command in commands(text_of(place), program)})


def alternatives(command: str) -> list[str]:
    """An "a or b" form as each of its commands. One after the "or" of more than a word that starts with a command is
    whole; any other stands for as many words at the end of the first: jobs stop copy or backup is jobs stop copy and
    jobs stop backup."""
    first, *rest = command.split(" or ")
    out = [first]
    for other in rest:
        said = other.split()
        out.append(other if len(said) > 1 and said[0] in TOP else " ".join(first.split()[:-len(said)] + said))
    return out


def argv_of(command: str) -> tuple[list[str], set[str]]:
    """The words, with the docs' placeholders as values (a word of STAND_INS by its stand-in, a <word> by the word, and
    a … at the end left out), and which words stand in."""
    standing: set[str] = set()

    def stand(m: re.Match) -> str:
        standing.add(m[1])
        return m[1]

    argv = [STAND_INS.get(w, w) for w in shlex.split(PLACEHOLDER.sub(stand, MORE.sub("", command)), posix=False)]
    return argv, standing | {STAND_INS[w] for w in command.split() if w in STAND_INS}


def refusal(argv: list[str], standing: set[str], parser=cli.build_parser) -> str | None:
    """What argparse says against the words, or None when they parse, or end in a help or the version. A stand-in
    where a choice goes, or a word of a comma list (commands.comma_list), is tried as the first choice: a placeholder
    can be any of them (hylly jobs stop NAME, hylly lookup --source <id>)."""
    argv = list(argv)
    while True:
        said = io.StringIO()
        try:
            with contextlib.redirect_stderr(said), contextlib.redirect_stdout(io.StringIO()):
                parser().parse_args(argv)
            return None
        except SystemExit as e:
            if e.code == 0:
                return None
        said = said.getvalue().strip()
        chosen = (re.search(r"invalid choice: '([^']*)' \(choose from '?([^',)\s]+)", said)
                  or re.search(r"error: argument \S+: ([^?\s,]+)\? Use ([^,\s]+)", said))
        if not (chosen and chosen[1] in standing and chosen[1] in argv):
            return said
        argv[argv.index(chosen[1])], standing = chosen[2], standing - {chosen[1]}


def names_a_command(words: list[str], said: str) -> bool:
    """Whether the words name a command as a sentence does, with only some of what it takes (hylly user remove, hylly set
    format): no option at all, and nothing argparse finds wrong but the words left out."""
    return not any(w.startswith("-") for w in words) and said.splitlines()[-1].split("error: ", 1)[-1].startswith(
        "the following arguments are required")


def test_the_docs_write_commands_at_all():
    assert len(everything()) > 60 and everything("hyllyd")


@pytest.mark.parametrize(("place", "command"), everything())
def test_a_written_command_parses(place, command):
    """Every command the docs write runs as written, every one in the help too: whatever argparse would say against
    it, a missing word too. Each command of an "a or b" form; a placeholder as a value, <id> too. Naming a command in a
    sentence, without what it takes, is fine; a refusal shown on purpose is listed in REFUSED, with why, and must be
    one."""
    if re.search(r"\bCOMMAND\b|^\d", command):  # any command at all, or the version
        pytest.skip("stands for every command")
    if not MORE.sub("", command):
        pytest.skip("not a command")
    for form in alternatives(command):
        argv, standing = argv_of(form)
        said = refusal(argv, standing)
        if (place, command) in REFUSED:
            assert said and not names_a_command(argv, said), f"{place}: hylly {form} is shown refused ({REFUSED[place, command]})"
        else:
            assert said is None or names_a_command(argv, said), f"{place}: hylly {form}: {said}"


@pytest.mark.parametrize(("place", "command"), everything("hyllyd"))
def test_a_written_hyllyd_command_parses(place, command):
    """hyllyd takes only what starting the server needs (commands.daemon_parser): hyllyd serve, say, is gone."""
    argv, standing = argv_of(command)
    assert refusal(argv, standing, words.daemon_parser) is None, f"{place}: hyllyd {command}"


def options_alone() -> list[tuple[str, str]]:
    """The options the docs write on their own, as in `--dry-run` or <code>--rules FILE</code>, with no command."""
    out = set()
    for place in PLACES:
        text = text_of(place)
        found = re.findall(r"<code>(--?[A-Za-z][\w-]*)(?:[ =][^<]*)?</code>", text)
        found += re.findall(r"`(--?[A-Za-z][\w-]*)(?:[ =][^`\n]*)?`", text)
        out |= {(place, option) for option in found}
    return sorted(out)


def every_option() -> set[str]:
    parsers = [p for _, p, _ in words.leaves(internal=True)] + [cli.build_parser(), words.daemon_parser()]
    return {option for p in parsers for a in p._actions for option in a.option_strings}


KNOWN = every_option()


@pytest.mark.parametrize(("place", "option"), options_alone())
def test_an_option_written_alone_is_one_that_hylly_or_hyllyd_takes(place, option):
    """An option written without its command can't be parsed, but it can still be one no command takes any more, as
    --stop, --verify and --run aren't: hylly jobs stop, hylly copy check and copying at once took their place."""
    assert option in KNOWN or option in OTHERS, f"{place}: {option}"


@pytest.mark.parametrize(("command", "forms"), [("jobs stop copy or backup", ["jobs stop copy", "jobs stop backup"]),
                                                ("serve --lan or server restart --lan", ["serve --lan", "server restart --lan"]),
                                                ("copy or backup", ["copy", "backup"]), ("logs download", ["logs download"])])
def test_an_or_form_is_each_of_its_commands(command, forms):
    assert alternatives(command) == forms


@pytest.mark.parametrize(("command", "parses"), [("jobs stop NAME", True), ("login <id> --clipboard", True),
                                                 ("humble seed FOLDER …", True), ("jobs stop lantern", False),
                                                 ("copy --verify <target>", False), ("login <id> --library own", False)])
def test_a_placeholder_is_any_value_and_the_rest_is_still_checked(command, parses):
    assert (refusal(*argv_of(command)) is None) == parses


@pytest.mark.parametrize(("words", "named"), [(["copy", "--verify", "T"], False), (["backup", "--stop"], False),
                                              (["serve", "--status"], False), (["backup", "T", "--run"], False),
                                              (["import"], False), (["user", "remove", "--yes"], False), (["user", "remove"], True),
                                              (["copy", "check"], True), (["tag"], True), (["set", "format"], True)])
def test_what_the_docs_may_write_is_told_from_an_old_form(words, named, capsys):
    """What the test above goes by: a form gone since, an option where none goes, or a word missing from a command with
    an option fails; only a command named with some of its words, as a sentence names one, passes."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(words)
    assert names_a_command(words, capsys.readouterr().err.strip()) == named


def test_the_help_keeps_none_of_the_old_forms():
    """Commands and options gone since, hylly import, --stop alone and hyllyd serve, are nowhere in the help."""
    assert not [c for place, c in everything() if place.endswith("help.html") and c.startswith("import")]
    assert ("src/hylly/help.html", "--stop") not in options_alone() and "hyllyd serve" not in text_of("src/hylly/help.html")


@pytest.mark.parametrize(("name", "example"), [(name, line.split(" #")[0].strip()) for name, lines in EXAMPLES.items()
                                               for line in lines])
def test_every_example_in_a_command_s_own_help_parses_as_it_is_written(name, example):
    """hylly COMMAND --help ends with examples (commands.EXAMPLES): each one is that command, or one of its actions,
    and runs as it is: an option another action takes is refused by argparse itself."""
    said = shlex.split(example)
    assert said[:len(name.split()) + 1] == ["hylly", *name.split()]
    args = cli.build_parser().parse_args(said[1:])  # a mistake ends the test in argparse's SystemExit
    assert args.leaf.startswith(name)


def test_every_option_that_takes_words_comma_separated_checks_them_by_one_type(tmp_path, capsys):
    """A mistyped word in a comma list once chose nothing, and the run ended with code 0 as if nothing were new: every
    option whose help says comma-separated has the one type (commands.comma_list), whose usage error names the words
    there are; --kind's are the rules' (yours and --rules' too), so it names those that come and checks them as the
    command runs (cli._kinds)."""
    import argparse

    lists = [(name, a) for name, p, _ in words.leaves(cli.build_parser()) for a in words.options_of(p)
             if "comma-separated" in (a.help or "")]
    assert {a.option_strings[0] for _, a in lists} == {"--kind", "--device", "--types", "--source"}
    for name, a in lists:
        assert getattr(a.type, "__qualname__", "") == "comma_list.<locals>.words", (name, a.option_strings)
        with pytest.raises(argparse.ArgumentTypeError, match=r"^no \w+\? Use \w+"):
            a.type(" , ")
        if a.option_strings != ["--kind"]:
            with pytest.raises(argparse.ArgumentTypeError, match=r"^nosuch\? Use \w+"):
                a.type("NoSuch")
    with pytest.raises(SystemExit) as stopped:
        cli.main(["lookup", "--source", "metron,nosuch", "--library", str(tmp_path)])
    assert stopped.value.code == 2 and "--source: nosuch? Use metron, comicvine" in capsys.readouterr().err


def test_the_docs_lists_of_commands_can_be_held_to_the_parser():
    """For the help page and the playbooks, which list the commands that take --json and those that ask first: these
    give the lists as the parser has them, by name. The help's own, once written by hand, lacked reviews, reports,
    theme and people list among those that take --json, and people forget among those that ask."""
    with_json = words.taking("--json")
    assert {"doctor", "jobs list", "user list", "people list", "reviews list", "reports", "theme list", "server status"} <= set(with_json)
    assert "jobs stop" not in with_json and "people forget" not in with_json
    asking = words.asks_first()
    assert {"people forget", "user remove", "tag add", "set", "itchio claim", "init", "copy", "backup"} <= set(asking)
    assert "people list" not in asking and all(name in dict((n, p) for n, p, _ in words.leaves()) for name in asking)
    assert not {"copy", "theme list", "people list", "user list", "login"} & set(words.taking("--library"))


# ---- the help page's lists, made from the parser ------------------------------------------------------------------

def linked(listed: str) -> list[str]:
    """What a list of commands in the help writes, each a link to its entry in the reference, with "alone" after it
    where it says so."""
    found = re.findall(r'<a href="#cmd-([\w-]+)"><code>([^<]+)</code></a>( alone)?', listed)
    assert all(anchor == said.replace(" ", "-") or said.startswith(anchor.replace("-", " ") + " ") for anchor, said, _ in found)
    return [html.unescape(said) + alone for _, said, alone in found]


def meant(listed: str) -> set[str]:
    """The commands a list means, as the help says it: a command written as it's typed alone (backup TARGET, or rules
    alone) is that one; a command of several actions written by its name is each of them (humble); any other is itself."""
    every, out = [n for n, _, _ in words.leaves()], set()
    for said in linked(listed):
        name = " ".join(w for w in said.removesuffix(" alone").split() if not w.isupper())  # backup TARGET is backup
        its = [n for n in every if n.split()[0] == said]
        out |= set(its) if len(its) > 1 else {name}
    return out


def names_just(listed: str, names: list[str]) -> None:
    """A list means these and no other: backup alone must not say backup, which would be backup check too."""
    assert meant(listed) == set(names), (linked(listed), names)


def test_the_help_s_lists_of_commands_are_made_from_the_parser(tmp_path):
    from hylly import helppage, remote

    lists = helppage.command_lists()
    template = (REPO / "src" / "hylly" / "help.html").read_text(encoding="utf-8")
    assert set(helppage.LIST.findall(template)) == set(lists)  # each list is used, and each placeholder is one
    page = helppage.help_page(tmp_path, generic=True)
    assert not helppage.LIST.search(page) and all(listed in page for listed in lists.values())
    asking, yes = words.asks_first(), words.taking("--yes")
    names_just(lists["json"], words.taking("--json"))
    assert set(words.one_object()) == cli.ONE_OBJECT  # a --json of one object however it ends, and main's own list
    names_just(lists["one-object"], sorted(cli.ONE_OBJECT))
    assert "server jobs" in meant(lists["one-object"]) and "server status" not in meant(lists["one-object"])
    names_just(lists["library"], words.taking("--library"))
    names_just(lists["rules"], words.taking("--rules"))
    names_just(lists["asks"], [name for name in asking if name in yes])
    names_just(lists["needs-a-person"], [name for name in asking if name not in yes])
    here = [name for name, _, _ in words.leaves() if name.split()[0] in remote.LOCAL]
    names_just(lists["here"], here + [f"jobs stop {job}" for job in remote.LOCAL_JOBS])
    assert "people forget" in linked(lists["asks"]) and "humble" in linked(lists["library"])  # a command alone: each action
    assert "humble" in linked(lists["rules"]) and "humble download" not in linked(lists["rules"])  # each of its actions
    # backup takes --library, backup check doesn't; rules takes --rules, rules copy doesn't: each is told from its group
    assert "backup TARGET" in linked(lists["library"]) and "backup check" not in meant(lists["library"])
    assert "rules alone" in linked(lists["rules"]) and "rules copy" not in meant(lists["rules"])
    assert "copy SOURCE TARGET" in linked(lists["asks"]) and "copy" in linked(lists["here"])  # the copy, or copy and its check


@pytest.mark.parametrize(("listed", "what"), [('<a href="#cmd-backup"><code>backup</code></a>', {"backup", "backup check"}),
                                              ('<a href="#cmd-backup"><code>backup TARGET</code></a>', {"backup"}),
                                              ('<a href="#cmd-rules"><code>rules</code></a> alone', {"rules"}),
                                              ('<a href="#cmd-humble-seed"><code>humble seed</code></a>', {"humble seed"})])
def test_a_list_tells_a_command_alone_from_all_of_its_actions(listed, what):
    assert meant(listed) == what


def test_the_lists_of_what_stays_here_written_by_hand_name_all_of_it():
    """settings.example.toml and docs/architecture.md say in words what always runs on this computer (remote.LOCAL), and
    which stops stay with it (remote.LOCAL_JOBS)."""
    from hylly import remote

    example = (REPO / "settings.example.toml").read_text(encoding="utf-8").split("\n[remote]\n", 1)[1].split("\nssh =", 1)[0]
    said = " ".join(example.replace("#", " ").split())
    named = re.search(r"; ([a-z, ]+) stay here", said)[1]
    assert re.split(r", | and ", named) == list(remote.LOCAL)
    assert all(f"hylly jobs stop {job}" in said for job in remote.LOCAL_JOBS)
    runs = (REPO / "docs" / "architecture.md").read_text(encoding="utf-8").split("- **Where each runs:**", 1)[1].split("\n- ", 1)[0]
    runs = " ".join(runs.split())
    assert re.findall(r"`(\w+)`", re.search(r"`remote\.LOCAL`: (.*?), and `remote\.LOCAL_JOBS`", runs)[1]) == list(remote.LOCAL)
    assert all(f"`hylly jobs stop {job}`" in runs for job in remote.LOCAL_JOBS)


def test_a_packaged_app_s_copy_of_the_settings_is_mentioned_by_the_doctor(tmp_path, monkeypatch, capsys):
    from hylly import doctor

    if paths.os.name != "nt":
        pytest.skip("only Windows does this")
    monkeypatch.delenv("HYLLY_HOME")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))  # where Windows has the settings folder, and gives an app its copy
    assert doctor.app_copies() == []
    copy = tmp_path / "Packages" / "Some.App_abc" / "LocalCache" / "Local" / "Hylly"
    copy.mkdir(parents=True)
    assert doctor.app_copies() == [copy]
    assert any(str(copy) in check["says"] and check["state"] == doctor.INFO for check in doctor._settings())
    cli.main(["user", "list"])  # and hylly user, which an assistant in such an app may run
    assert str(copy) in capsys.readouterr().err
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "a-sandbox"))  # a settings folder elsewhere has no such copies
    assert doctor.app_copies() == []
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "Hylly"))  # but the usual one named by HYLLY_HOME does
    assert doctor.app_copies() == [copy]
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "a-sandbox" / ".." / "Hylly"))  # however it's written
    assert doctor.app_copies() == [copy]
    (tmp_path / "Hylly").mkdir()
    try:
        (tmp_path / "a-link").symlink_to(tmp_path / "Hylly", target_is_directory=True)
    except OSError:
        return  # this computer won't make links: the rest was tried
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "a-link"))  # and through a link, as usual_settings_folder has it
    assert doctor.app_copies() == [copy]


def test_hylly_user_says_so_when_an_app_keeps_its_own_copy_of_the_settings(tmp_path, monkeypatch, capsys):
    """A user added from inside a packaged app, by an AI assistant say, lands in the app's copy of the settings folder,
    which the library server never sees: every hylly user says so first, on standard error, but with --json, whose
    standard output is its result alone. (Where Windows has such copies: the doctor's test above.)"""
    from hylly import access, doctor

    copy = tmp_path / "Packages" / "Some.App_abc" / "LocalCache" / "Local" / "Hylly"
    monkeypatch.setattr(doctor, "app_copies", lambda: [copy])
    monkeypatch.setattr("sys.stdin", io.StringIO("correct horse\n"))
    cli.main(["user", "add", "ann", "--password-stdin"])
    said = capsys.readouterr()
    assert f"an app keeps its own copy of this settings folder, {copy}" in said.err and "your own terminal" in said.err
    assert str(copy) not in said.out and "ann" in access.load_accounts()
    for typed in (["user", "list"], ["user", "role", "ann", "reader", "--yes"], ["user", "link", "ann", "-n"]):
        cli.main(typed)
        assert str(copy) in capsys.readouterr().err, typed
    cli.main(["user", "list", "--json"])
    said = capsys.readouterr()
    assert str(copy) not in said.err + said.out and said.out.startswith("{")
    monkeypatch.setattr(doctor, "app_copies", lambda: [])
    cli.main(["user", "list"])
    assert "own copy" not in capsys.readouterr().err  # none: nothing said


# ---- the playbooks ---------------------------------------------------------------------------------------------

def playbook(heading: str) -> str:
    """One section of docs/playbooks.md, by its heading."""
    return (REPO / "docs" / "playbooks.md").read_text(encoding="utf-8").split(f"\n## {heading}\n")[1].split("\n## ")[0]


def test_the_playbooks_keep_an_assistant_out_of_every_place_a_secret_is_kept(tmp_path):
    from hylly import backup, db, settings

    owners = playbook("What is the owner's")
    for place in (paths.logins_dir().name, settings.secrets_dir().name, db.FILE, f"state/{db.backups(tmp_path).name}",
                  f"{backup.saved_of(tmp_path).name}/"):
        assert f"`{place}`" in owners, place
    assert len(settings.SECRETS) == 2 and "the ntfy topic or token" in owners  # both of the server's secrets
    # the ways to log in and to add a user without a keyboard, which only the owner's own scripts use
    assert "`--stdin`, `--cookies` and `--password-stdin` are for the owner's own scripts" in owners
    assert "`curation/`" not in playbook("Tags and corrections")  # hylly tag and hylly set write to the database
    # nor into a login's environment variable, which hylly commands names under secret
    assert "an environment variable" in owners and "under `secret`" in owners and "secret" in words.commands_data()
    assert f"`{words.commands_data()['secret'][0]}`" in owners


def test_the_playbooks_name_every_command_that_needs_a_person():
    """Each by its own name (`hylly people forget`), or a command whose actions all ask by the command's (`hylly tag`)."""
    owners = playbook("What is the owner's")
    for name in words.asks_first():
        assert f"`hylly {name}" in owners or f"`hylly {name.split()[0]}`" in owners, name


def test_the_playbooks_exit_codes_are_the_ones_hylly_ends_with():
    from hylly import exits

    assert sorted(int(code) for code in re.findall(r"^\| (\d+) \|", playbook("Exit codes"), re.M)) == sorted(exits.TABLE)


SET_UP = ("First setup", "Letting people in", "A second computer", "Backup and restore", "Where am I running")
STATES_AND_FIELDS = {"ok", "info", "update", "problem", "users", "owners", "listens"}  # what a check's line may name besides
PROVES_NOTHING = {  # (playbook, step): why no check of the doctor's proves it
    ("Letting people in", 2): "a one-time link, which the owner makes and gives on, and which no check sees",
    ("Backup and restore", 1): "a dry run: it changes nothing, and shows the plan",
    ("Backup and restore", 3): "a check of the backup, which reads it again and changes nothing",
    ("Where am I running", 1): "it only looks, as the whole playbook does",
    ("Where am I running", 4): "it only looks, as the whole playbook does",
}


def test_each_set_up_playbook_asks_first_proves_each_step_by_a_doctor_check_and_says_what_is_the_owner_s():
    """The set-up playbooks, from installing to a backup and its restore: what to ask the owner first, the steps in order,
    and what only the owner types; and each numbered step proved by a check of hylly doctor --json, by its id as
    doctor.CHECKS has it, but those PROVES_NOTHING names, each with why. A step's proof taken away, or an id renamed or
    gone in the doctor, can't go unnoticed."""
    from hylly import doctor

    unproved = set()
    for heading in SET_UP:
        text = playbook(heading)
        assert text.lstrip().startswith("**Ask first:**") and "\n**The owner's alone:**" in text, heading
        steps = re.findall(r"^(\d+)\. (.*)$", text, re.M)
        assert [int(n) for n, _ in steps] == list(range(1, len(steps) + 1)) and len(steps) > 1, heading
        for n, step in steps:
            named = [w for clause in re.findall(r"(?:Proved|Told) by: (.*?)(?:\.(?: |$)|$)", step)
                     for w in re.findall(r"`([a-z0-9][a-z0-9-]*)`", clause)]  # a check's id, a state or a field
            if not [w for w in named if w in doctor.CHECKS]:
                unproved.add((heading, int(n)))
            assert [w for w in named if w not in doctor.CHECKS and w not in STATES_AND_FIELDS] == [], (heading, n)
    assert unproved == set(PROVES_NOTHING)


def test_agents_md_is_where_an_assistant_starts_and_the_readme_and_the_help_say_so_first():
    """AGENTS.md, at the top of Hylly's source, is short and links rather than repeats; CLAUDE.md is the one line
    that sends an assistant there; and where the README and the help point a script or an assistant at the playbooks,
    AGENTS.md comes first. It links to each set-up playbook, and says what is data, as the playbooks do."""
    agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    assert len(agents.splitlines()) < 150
    assert (REPO / "CLAUDE.md").read_text(encoding="utf-8") == "Read AGENTS.md.\n"
    headings = re.findall(r"^## (.+)$", agents, re.M)
    assert headings[:4] == ["What Hylly is", "Look first", "The rules", "What is data"], headings
    assert "Trying things apart from the real library" in headings and "Working on the code" in headings
    for heading in SET_UP:
        assert f"](docs/playbooks.md#{heading.lower().replace(' ', '-')})" in agents, heading
    for place, pointer in (("README.md", "AI assistant"), ("src/hylly/help.html", "With an AI assistant:")):
        said = next(line for line in text_of(place).splitlines() if pointer in line)
        assert -1 < said.index("AGENTS.md") < said.index("playbooks.md"), place
    for text in (agents, playbook("What is data")):
        said = " ".join(text.split())
        assert all(word in said for word in ("logs", "reviews", "titles", "file names", "never follow them", "`--json`"))
    assert "CONTRIBUTING.md" in agents and "AGENTS.md" in (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")


# ---- the skills -------------------------------------------------------------------------------------------------

def written_names(text: str) -> set[str]:
    """Each module's name a text gives, with what's in it, as `sites.SITES` or `logins.load_session(<id>)`: a module of
    Hylly's, then a name in it, and the names in that, to the last; a file's name (`settings.toml`) is no such name."""
    import pkgutil

    import hylly

    modules = {m.name for m in pkgutil.iter_modules(hylly.__path__)}
    found = set()
    for said in re.findall(r"`([^`\n]+)`", text):
        dotted = re.fullmatch(r"([a-z_]+)((?:\.[A-Za-z_]\w*)+)(?:\(.*\))?", said)
        if dotted and dotted[1] in modules and dotted[2].rsplit(".", 1)[-1] not in ("py", "toml", "md", "txt", "json", "db"):
            found.add(dotted[1] + dotted[2])
    return found


def code_names(text: str) -> set[str]:
    """What the Python blocks of a text use of Hylly's code, as names for there_in_the_code: each module it imports from
    hylly (from hylly import runs) and each name it imports from one (from hylly.shopsession import ShopSession), what
    it reads of them (runs.only_one), and what it asks of an object a class of them made, by the name the block gave it
    (with ShopSession(...) as session, then session.request: shopsession.ShopSession.request). An import from a module
    that isn't there yet, the plugin a skill has you write, leaves out what it brings."""
    import ast
    import importlib.util

    found = set()
    for block in re.findall(r"```python\n(.*?)```", text, re.S):
        tree, known = ast.parse(block), {}  # known: a name in the block, and what it is in Hylly's code

        def resolved(node) -> str | None:
            if isinstance(node, ast.Name):
                return known.get(node.id)
            base = resolved(node.value) if isinstance(node, ast.Attribute) else None
            return f"{base}.{node.attr}" if base else None

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "hylly":
                inner = node.module.removeprefix("hylly").lstrip(".")
                if inner and importlib.util.find_spec(node.module) is None:
                    continue
                for alias in node.names:
                    known[alias.asname or alias.name] = f"{inner}.{alias.name}" if inner else alias.name
        made = [(item.optional_vars, item.context_expr) for node in ast.walk(tree) if isinstance(node, ast.With)
                for item in node.items] + [(target, node.value) for node in ast.walk(tree) if isinstance(node, ast.Assign)
                                          for target in node.targets]
        for name, value in made:  # an object a class made, by its name: its class's names are what it has
            maker = resolved(value.func) if isinstance(value, ast.Call) else None
            if isinstance(name, ast.Name) and maker and maker.rsplit(".", 1)[-1][:1].isupper():
                known[name.id] = maker
        found |= set(known.values()) | {resolved(node) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    return found - {None}


def there_in_the_code(name: str) -> bool:
    """Whether the module is Hylly's, has the name, and each name after it is in the one before (shopping.File: a class
    of shopping); a dataclass's field with no default counts as the last name (shopping.File.name)."""
    import importlib

    module, *names = name.split(".")
    try:
        value = importlib.import_module(f"hylly.{module}")
    except ModuleNotFoundError:
        return False
    for n, part in enumerate(names):
        if hasattr(value, part):
            value = getattr(value, part)
        else:
            return n == len(names) - 1 and part in getattr(value, "__dataclass_fields__", {})
    return True


def test_a_name_given_from_the_code_is_found_there_and_a_file_s_name_is_none():
    assert written_names("`sites.SITES`, `logins.load_session(<id>)`, `settings.toml`, `Library.link`, `shopping.File.name`") \
        == {"sites.SITES", "logins.load_session", "shopping.File.name"}
    assert there_in_the_code("shopping.File") and there_in_the_code("shopping.File.name")  # a field with no default
    assert not there_in_the_code("commands.SHOP_ACTIONS") and not there_in_the_code("shopping.File.nonesuch")
    assert not there_in_the_code("nonesuch.thing")


def test_a_python_block_s_names_from_the_code_are_read_and_a_made_up_one_is_found_missing():
    block = ("```python\nfrom hylly import paths, runs\nfrom hylly.shopsession import ShopSession\n"
             "from hylly.lookups.yours import Yours\n"  # a plugin still to be written: left out
             "with runs.only_twice('x', paths.library_root()), ShopSession('A', 'https://a.example/') as session:\n"
             "    answer = session.fetch('GET', 'library')\n    mine = Yours(); mine.anything\n```\n")
    names = code_names(block)
    assert {"runs.only_twice", "shopsession.ShopSession.fetch", "paths.library_root", "shopsession.ShopSession"} <= names
    assert not any(name.startswith(("lookups", "Yours")) for name in names)
    missing = sorted(name for name in names if not there_in_the_code(name))
    assert missing == ["runs.only_twice", "shopsession.ShopSession.fetch"]


def test_the_database_s_design_note_gives_only_names_there_are():
    """docs/database.md names much of db.py and the modules that write the tables: each name is there in the code."""
    text = (REPO / "docs" / "database.md").read_text(encoding="utf-8")
    names = written_names(text)
    assert len(names) > 30 and [name for name in sorted(names) if not there_in_the_code(name)] == []


def test_each_skill_is_shaped_as_the_first_and_gives_only_what_there_is():
    """The skills in .claude/skills, playbooks of one task for any assistant: each named for its folder, with when to use
    it; its hard rules first, then a method in which every step says when it's done, a checklist, and what to do when it
    breaks later, the last. Every name it gives from the code is there (`shops.SHOPS`, `runs.only_one`), in its words
    and in its Python (a probe's session.request); every test it names is one of tests/, and every check of the
    doctor's that says a step is done is one of doctor.CHECKS; AGENTS.md names each, and the docs' tests read each
    (PLACES here, and tests/test_docs_paths.py). So a skill renamed, or a name it gives gone or never there, can't go
    unnoticed."""
    from hylly import doctor

    assert {".claude/skills/add-lookup-source/SKILL.md", ".claude/skills/add-shop/SKILL.md",
            ".claude/skills/set-up-server/SKILL.md"} <= set(SKILLS)
    agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    tests = "".join(p.read_text(encoding="utf-8") for p in (REPO / "tests").glob("test_*.py"))
    for place in SKILLS:
        text = (REPO / place).read_text(encoding="utf-8")
        head = re.match(r"---\nname: ([\w-]+)\ndescription: (.+)\n---\n", text)
        assert head and head[1] == Path(place).parent.name and "Use when" in head[2], place
        headings = re.findall(r"^## (.+)$", text, re.M)
        assert headings[0].startswith("Rules") and "Method" in headings and headings[-1] == "When it breaks later", place
        assert any(h.startswith("Checklist") for h in headings) and len(re.findall(r"^- \[ \] ", text, re.M)) >= 4, place
        method = text.split("\n## Method\n", 1)[1].split("\n## ", 1)[0]
        assert "| Step | Do | Done when |" in method, place
        rows = [row.strip("|").split(" | ") for row in re.findall(r"^\| \d+\. .*\|$", method, re.M)]
        assert len(rows) >= 5 and all(len(row) == 3 and row[2].strip() for row in rows), place
        done = [w for row in rows for w in re.findall(r"`([a-z0-9][a-z0-9-]*)`", row[2])]  # a check's id, a state, a field
        assert [w for w in done if w not in doctor.CHECKS and w not in STATES_AND_FIELDS] == [], place
        assert [name for name in written_names(text) | code_names(text) if not there_in_the_code(name)] == [], place
        assert [t for t in set(re.findall(r"`(test_\w+)`", text)) if f"def {t}(" not in tests] == [], place
        assert f"`{place}`" in agents, place
    probe = code_names((REPO / ".claude" / "skills" / "add-shop" / "SKILL.md").read_text(encoding="utf-8"))
    assert {"runs.only_one", "shopsession.ShopSession.request", "logins.load_session"} <= probe  # its probe is read too
    set_up = (REPO / ".claude" / "skills" / "set-up-server" / "SKILL.md").read_text(encoding="utf-8")
    proved = set(re.findall(r"`([a-z0-9][a-z0-9-]*)`", set_up.split("\n## Method\n", 1)[1].split("\n## ", 1)[0]))
    assert len(proved & set(doctor.CHECKS)) >= 10  # the set-up's steps, each proved by the doctor
    for heading in SET_UP:  # and each set-up playbook it goes through, by a link that finds its heading (below)
        assert f"](../../../docs/playbooks.md#{slug(heading)})" in set_up, heading


def slug(heading: str) -> str:
    """A heading's anchor, as GitHub makes it: in small letters, without its marks and punctuation, a dash for a space."""
    return re.sub(r"[^\w\- ]", "", re.sub(r"[`*]", "", heading).strip().lower()).replace(" ", "-")


def test_a_link_to_a_heading_in_the_docs_finds_it():
    """A link such as docs/playbooks.md#first-setup, in the Markdown docs held to the parser, leads to a heading of that
    file: a heading renamed can't leave AGENTS.md or another doc pointing at nothing."""
    assert slug("What is the owner's") == "what-is-the-owners" and slug("Claiming at itch.io") == "claiming-at-itchio"
    found = []
    for place in (p for p in PLACES if p.endswith(".md")):
        for target in re.findall(r"\]\(([^)\s#]*)#([^)\s]+)\)", text_of(place)):
            linked = Path(place).parent / target[0] if target[0] else Path(place)
            if linked.suffix != ".md":
                continue
            text = re.sub(r"```.*?```", "", (REPO / linked).read_text(encoding="utf-8"), flags=re.S)
            found.append((place, "#".join(target), target[1] in {slug(h) for h in re.findall(r"^#{1,6} (.+)$", text, re.M)}))
    assert len(found) > 20 and [f"{place}: {link}" for place, link, there in found if not there] == []
