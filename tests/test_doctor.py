"""hylly doctor (doctor.py): how things stand, a check at a time, in words and as JSON for a script or an assistant, with
an id for each check and the command that mends it; 7-Zip and its RAR part; what is too old, told as a check of its own.
Kept until now among the tests of what an assistant goes by (test_assistants.py) and of the PDF worker
(test_pdfworker.py), which brought the doctor its first check of what is too old."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

from hylly import archives, bell, cli, doctor, exits, paths, updates

from helpers import build_library, exit_of, states

REAL_SEVENZIP_INFO = archives.sevenzip_info  # tests/conftest.py stands in for it: no test asks this computer's 7-Zip
COOKIE = "c" * 80  # a made-up shop's login
RAR, ZIP = b"Rar!\x1a\x07\x00", b"PK\x03\x04"  # the first bytes of a RAR archive and of a ZIP


# ---- hylly doctor --------------------------------------------------------------------------------------------

def test_the_doctor_with_nothing_set_up(capsys):
    code = exit_of(capsys, "doctor")[0]
    assert code == exits.PARTLY
    cli_text = doctor.text(doctor.report())
    assert "NOT Library: No library set" in cli_text and "hylly demo" in cli_text


def test_the_doctor_names_no_secrets(tmp_path, monkeypatch):
    secret, token = "https://my.example/a-long-secret-topic-" + "x" * 12, "a-token-value"
    monkeypatch.setenv("HYLLY_HUMBLE_SESSION", COOKIE)
    monkeypatch.setattr(bell, "settings", lambda: {**bell.DEFAULTS, "ntfy": secret, "ntfy_token": token})
    result = doctor.report(str(tmp_path))
    said = json.dumps(result) + doctor.text(result)
    assert secret not in said and token not in said and COOKIE not in said
    assert "Saved: humble" in said


def test_the_doctor_tells_of_a_library_that_isn_t_there_or_isn_t_marked(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "REQUIRE_MARKER", True)
    assert states(doctor.report(str(tmp_path / "unmounted")))["Library"] == "problem"
    tmp_path.joinpath("empty").mkdir()
    result = doctor.report(str(tmp_path / "empty"))
    said = doctor.text(result)
    assert states(result)["Library"] == "problem" and "hylly init" in said
    assert said.index("disk or share there") < said.index("hylly init")  # an unmounted disk first: marking it is the last thing


def test_the_doctor_tells_of_the_library_s_records_refused_here_as_a_copy_s_and_the_way_out(tmp_path):
    """Its database has the records, but the marker names them by a token not in this data folder: every change is
    refused, so the doctor says so, rather than nothing."""
    from hylly import db


    root = tmp_path / "library"
    build_library(root)
    assert "Records" not in states(doctor.report(str(root)))
    db.token_file().unlink()  # left behind, as when the data folder moved without it
    found = [c for c in doctor.report(str(root))["checks"] if c["id"] == "records"]
    assert len(found) == 1 and found[0]["state"] == doctor.PROBLEM and "is missing" in found[0]["says"]
    assert "hylly server take-over" in found[0]["next"] and "records-token back" in found[0]["next"] and found[0]["owner"]


def a_built_library(tmp_path) -> Path:
    """A library whose records this computer's database keeps, its catalog built."""
    from hylly import catalog

    root = tmp_path / "library"
    build_library(root)
    catalog.build(root, log=lambda line: None)
    return root


def test_a_marker_still_damaged_after_a_moment_is_said_with_the_way_out_and_where_to_run_it(tmp_path, monkeypatch):
    """The library's marker read and judged as its every reader does (db.owner), and looked at again a moment apart
    while it reads as damaged (another computer may be writing it): one still cut short, damaged, a list or naming no
    id is a problem of its own, said with what's wrong and the command that writes it anew, on the computer whose
    server has kept the records lately and only there, in words, as nothing here can tell which that is; not a mistake
    of the doctor's ('list' has no 'get'), nor silence."""
    from hylly import db

    root = a_built_library(tmp_path)
    assert [(c["id"], c["state"]) for c in doctor._underneath(root)] == [("catalog-files", doctor.OK)]
    slept = []
    monkeypatch.setattr(doctor.time, "sleep", slept.append)
    for damaged, why in (('{"id": ', "Expecting value"), ("[1, 2]", "no id in it"), ('{"computer": "elsewhere"}', "no id in it"),
                         ('{"id": 12}', "no id in it")):
        db.owner_file(root).write_text(damaged, encoding="utf-8")
        slept.clear()
        found = doctor._underneath(root, str(root))
        assert [(c["id"], c["state"], c.get("owner")) for c in found] == [("records", doctor.PROBLEM, True)], damaged
        assert slept == [doctor.MARKER_AGAIN] * (db.READ_BACK - 1), damaged  # looked at again, as db._read_back does
        said = found[0]["says"]
        assert said.startswith(f"The library's owner marker ({db.owner_file(root)}) can't be read: ") and why in said
        then = found[0]["next"]
        assert then.startswith(f"Run hylly server take-over --library {root} on the computer whose server has kept this "
                               "library's records lately, and only there:"), damaged
        assert "start the records afresh or put older ones in place" in then and "This computer" not in then
    said = doctor.report(str(root))
    assert "'list' object" not in doctor.text(said) and states(said)["Records"] == doctor.PROBLEM
    assert doctor._underneath(root)[0]["next"].startswith("Run hylly server take-over on the computer")  # no --library given
    db.owner_file(root).unlink()  # none: no database has taken the records on yet, nothing to say
    assert doctor._underneath(root) == []


def test_a_marker_whole_on_a_second_look_is_no_problem(tmp_path, monkeypatch):
    """Another computer writing the marker in place: seen cut short for a moment, it's whole a moment later."""
    from hylly import db

    root = a_built_library(tmp_path)
    whole = db.owner_file(root).read_text(encoding="utf-8")
    db.owner_file(root).write_text(whole[: len(whole) // 2], encoding="utf-8")
    monkeypatch.setattr(doctor.time, "sleep", lambda seconds: db.owner_file(root).write_text(whole, encoding="utf-8"))
    assert [(c["id"], c["state"]) for c in doctor._underneath(root)] == [("catalog-files", doctor.OK)]


@pytest.mark.skipif(os.name == "nt", reason="Windows goes by the folder's permissions")
def test_a_marker_this_account_may_not_read_points_at_the_disk_and_its_rights_never_at_a_take_over(tmp_path):
    from hylly import db

    root = a_built_library(tmp_path)
    db.owner_file(root).chmod(0)
    try:
        try:
            db.owner_file(root).read_bytes()
            pytest.skip("this account reads a file it has no right to read (root, or a disk that keeps no rights)")
        except PermissionError:
            pass
        (found,) = doctor._underneath(root, str(root))
    finally:
        db.owner_file(root).chmod(0o644)
    assert (found["id"], found["state"], found.get("owner")) == ("records", doctor.PROBLEM, True)
    assert found["says"].startswith(f"The library's owner marker ({db.owner_file(root)}) can't be read now: ")
    assert "Permission denied" in found["says"] and "may this account read" in found["next"]
    assert "take-over" not in found["next"] and "write nothing over it" in found["next"]


def test_a_marker_a_stalled_share_won_t_give_now_points_at_the_disk_never_at_a_take_over(tmp_path, monkeypatch):
    """Any trouble of the disk's own (here an I/O error, as a share that stalls gives one): the marker may be whole, so
    it's looked at once, and no command that writes it anew is offered."""
    import errno

    from hylly import db

    root = a_built_library(tmp_path)
    slept = []
    monkeypatch.setattr(doctor.time, "sleep", slept.append)

    def stalled(where):
        raise OSError(errno.EIO, "Input/output error", str(db.owner_file(where)))

    monkeypatch.setattr(db, "owner", stalled)
    (found,) = doctor._underneath(root, str(root))
    assert (found["id"], found["state"], found.get("owner"), slept) == ("records", doctor.PROBLEM, True, [])
    assert "can't be read now: [Errno 5] Input/output error" in found["says"] and "disk or share" in found["next"]
    assert "take-over" not in found["next"]


def test_the_doctor_tells_of_mistakes_in_the_settings(tmp_path, monkeypatch):
    (tmp_path / "settings.toml").write_text("this isn't toml", encoding="utf-8")
    monkeypatch.setenv("HYLLY_SETTINGS", str(tmp_path / "settings.toml"))
    assert states(doctor.report(str(tmp_path)))["Settings"] == "problem"


def test_one_mistake_is_one_problem(tmp_path, monkeypatch):
    (tmp_path / "settings.toml").write_text("this isn't toml", encoding="utf-8")
    monkeypatch.setenv("HYLLY_SETTINGS", str(tmp_path / "settings.toml"))
    assert doctor.report()["problems"] == 1


def test_the_doctor_writes_nothing(tmp_path):
    before = sorted(tmp_path.rglob("*"))
    doctor.report(str(tmp_path))
    assert sorted(tmp_path.rglob("*")) == before


def test_the_doctor_reads_the_catalog_once_for_all_its_looks_at_it(tmp_path, monkeypatch):
    """The doctor looks at the catalog four ways (its titles, the books no reader opens, the files the server refuses,
    its CBR comics for 7-Zip): one reading of data.js for them all, as a big library's takes a while."""
    from hylly import catalog

    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    monkeypatch.setattr(archives, "sevenzip", lambda: None)  # so that the CBR comics are looked at too
    data, reads, real = paths.catalog_dir(tmp_path) / "data.js", [], Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *a, **k: (reads.append(path.name) if path == data else None) or real(path, *a, **k))
    checks = {c["id"]: c for c in doctor.report(str(tmp_path))["checks"]}
    assert {"catalog", "7-zip"} <= set(checks) and checks["catalog"]["state"] == doctor.OK and reads == ["data.js"]


# ---- hylly doctor --json: an id for each check, the command an assistant may run, and what is the owner's --------------

NOT_TO_RUN = re.compile(r"\bsudo\b|--yes\b|(?<![\w-])-y\b|--run\b|\blogin\b|password|\bhylly (?:user|people|init|backup|restore|"
                        r"copy|decide|tag|set|itchio|download|serve|server|bell)\b", re.I)


def doctor_source():
    import ast

    text = Path(doctor.__file__).read_text(encoding="utf-8")
    return text, ast.parse(text)


def a_marked_library(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    paths.marker_file(root).parent.mkdir(parents=True, exist_ok=True)
    paths.marker_file(root).write_text('{"id": "made-up"}', encoding="utf-8")
    return root


def held_to_its_fields(result: dict) -> list[dict]:
    """Every check of a report as --json gives it: an id from the table with that id's what, a run only an assistant
    may run, owner only as true, and never both; the checks, for more."""
    for c in result["checks"]:
        assert c["id"] in doctor.CHECKS and c["what"] == doctor.CHECKS[c["id"]], c
        assert not ("run" in c and "owner" in c) and c.get("owner", True) is True, c
        assert "run" not in c or ("next" in c and not NOT_TO_RUN.search(c["run"])), c
        assert "run" not in c or any(c["run"] == " ".join(words) or c["run"].startswith(" ".join(words) + " ") for words in RUNS), c
    return result["checks"]


def test_every_check_the_doctor_can_make_has_an_id_from_its_one_table(tmp_path, monkeypatch):
    """The id is what a script or an assistant goes by, as what can be several checks' (Settings, Library, Address):
    written as it is wherever a check is made, each from the one table, short, in small letters and dashes, and in the
    table once; a look that fails says so under an id of its own too, and every id in the table is one the doctor
    makes. What is older than it should be has an id for each thing updates.behind looks at."""
    import ast

    from hylly import updates

    text, tree = doctor_source()
    table = next(node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                 and any(getattr(target, "id", "") == "CHECKS" for target in node.targets))
    keys = [key.value for key in table.keys]
    assert len(keys) == len(set(keys)) == len(doctor.CHECKS)  # none twice, which a dict would quietly take
    assert all(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", key) and len(key) <= 24 for key in keys), keys
    made = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("_check", "_safely"):
            first = node.args[0]
            if isinstance(first, ast.Constant):
                assert first.value in doctor.CHECKS, first.value
                made.add(first.value)
            else:  # _safely's own key, and the updates' ids, made below
                assert getattr(first, "id", "") == "key" or isinstance(first, ast.BinOp), ast.dump(first)
    behind = [*(label for label, _, _ in updates.PACKAGES.values()), "Python", "7-Zip"]
    with monkeypatch.context() as m:
        m.setattr(updates, "behind", lambda: [{"what": what, "have": "1", "want": "2", "why": "Old.", "next": "Update it."}
                                              for what in behind])
        found = doctor._behind()
    assert [c["what"] for c in found] == behind and all(c["id"] in doctor.CHECKS for c in found)  # its words as before
    made |= {c["id"] for c in found}
    report = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "report")
    looks = sorted({node.id for node in ast.walk(report) if isinstance(node, ast.Name) and node.id.startswith("_")
                    and callable(getattr(doctor, node.id, None)) and node.id not in ("_check", "_safely", "_library")})
    assert len(looks) > 20
    for name in looks:  # each look trips over a mistake of its own: a problem under an id of the table's
        monkeypatch.setattr(doctor, name, lambda *a, name=name, **k: (_ for _ in ()).throw(RuntimeError(f"made up in {name}")))
    tripped = [c for c in held_to_its_fields(doctor.report(str(a_marked_library(tmp_path / "library"))))
               if c["says"].startswith("made up in")]
    assert len(tripped) == len(looks) and all(c["state"] == "problem" for c in tripped)
    made |= {c["id"] for c in tripped}
    assert made == set(doctor.CHECKS)


# The commands a run may be, as the doctor's own words begin, here a second time, so that a new one is a decision:
# hylly's that only read or make again what Hylly makes (the catalog's with --library and the library's full path), and
# taking others' rights away from Hylly's own folders, by their full paths.
RUNS = {("hylly", "catalog"), ("hylly", "catalog", "--rebuild"), ("hylly", "rules"), ("chmod", "-R", "go-rwx")}


def a_run_as_written(node) -> str:
    """What is wrong with a run as the doctor's code writes it, or "": words of its own, from RUNS, then for the catalog
    only *_library_option(...), for chmod only *_absolute(...) of one or more paths; () for none."""
    import ast

    if not isinstance(node, ast.Tuple):
        return "a run is written as its words, (...) or ()"
    words, rest = [], list(node.elts)
    while rest and (isinstance(rest[0], ast.Constant) or (isinstance(rest[0], ast.Starred) and getattr(rest[0].value, "id", "") == "PRIVATE")):
        first = rest.pop(0)
        words += [first.value] if isinstance(first, ast.Constant) else list(doctor.PRIVATE)
    if not words:
        return "" if not rest else "a run begins with its command's own words"
    if tuple(words) not in RUNS:
        return f"not a command a run may be: {words}"
    takes = "_absolute" if tuple(words) == doctor.PRIVATE else "_library_option" if words[:2] == ["hylly", "catalog"] else None
    if any(not (isinstance(e, ast.Starred) and isinstance(e.value, ast.Call) and getattr(e.value.func, "id", "") == takes) for e in rest):
        return f"after {words}: only *{takes}(...)" if takes else f"nothing may follow {words}"
    if takes == "_absolute" and len(rest) != 1:
        return "chmod takes its paths from one *_absolute(...)"
    if takes == "_library_option" and len(rest) > 1:
        return "one --library at most"
    return ""


def test_a_run_is_one_of_the_few_an_assistant_may_run_and_never_the_owner_s_step(monkeypatch):
    """Every run the doctor's code can give, as it is written, wherever it is: a run= to a check, and every run = ...
    it passes on, is one of RUNS, in full paths; nothing else is written to a run, and no run comes with owner: true
    on any line, tried or not. The doctor itself refuses any other (_command), and a run beside owner (_check)."""
    import ast

    text, tree = doctor_source()
    assert set(doctor.RUNS) | {doctor.PRIVATE} == RUNS  # the doctor's own list: one more is a decision, made here too
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_check"]
    assigned = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                and any(isinstance(n, ast.Name) and n.id == "run" for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
                        for n in ast.walk(t))]
    assert assigned and calls
    for node in assigned:  # only run = (...), its words
        assert isinstance(node, ast.Assign) and [getattr(t, "id", "") for t in node.targets] == ["run"], ast.get_source_segment(text, node)
        assert not a_run_as_written(node.value), (a_run_as_written(node.value), ast.get_source_segment(text, node))
    for call in calls:
        said = ast.get_source_segment(text, call)
        assert len(call.args) <= 4, said  # run and owner only by their names
        given = {keyword.arg: keyword.value for keyword in call.keywords}
        run = given.get("run")
        if run is None:
            continue
        if isinstance(run, ast.Name):  # passed on: only the one, written as above
            assert run.id == "run", said
            owner = given.get("owner")
            assert owner is None or ast.dump(owner) == ast.dump(ast.parse("not run", mode="eval").body), said
        else:
            assert not a_run_as_written(run), (a_run_as_written(run), said)
            assert "owner" not in given or (isinstance(given["owner"], ast.Constant) and given["owner"].value is False), said
    for words in (("hylly", "humble", "download"), ("chmod", "-R", "go+rwx", "/x"), ("rm", "-rf", "/x"), ("chmod", "-R", "go-rwx"),
                  ("chmod", "-R", "go-rwx", "relative"), ("hylly", "catalog", "--library", "relative"), ("hylly", "catalog", "--yes"),
                  ("hylly", "rules", "--library", "/x"), ("hylly", "user", "add", "ann"), ("sudo", "chown", "-R", "x", "/x")):
        with pytest.raises(ValueError):  # planted, they never reach a line
            doctor._check("catalog", doctor.PROBLEM, "Made up.", "Made up.", run=words)
    with pytest.raises(ValueError):
        doctor._check("state-copies", doctor.PROBLEM, "Made up.", "Made up.", run=(*doctor.PRIVATE, "/x"), owner=True)
    for planted, wrong in (('run=f"chmod go+rwx {x}"', "words"), ('run=("hylly", "humble", "download")', "not a command"),
                           ('run=("rm", "-rf", x)', "not a command"), ('run=(*PRIVATE, x)', "only *_absolute"),
                           ('run=("hylly", "catalog", "--library", x)', "not a command")):
        assert wrong in a_run_as_written(ast.parse(f"_check(k, s, w, n, {planted})").body[0].value.keywords[0].value), planted


def test_a_command_to_run_is_one_an_assistant_may_run_and_the_owner_s_steps_say_so(tmp_path, monkeypatch, capsys):
    """run is the exact command, and only where the step is one command an assistant may run: never sudo, a password or
    a login, a --yes or a --run, nor what lets more people in. A step that needs sudo, a password, the owner's choice or
    someone at the server says owner: true instead, and next stays the words for people. A run's hylly command is for
    the library the doctor looked at, with --library when it was given one."""
    root = a_marked_library(tmp_path / "a library")  # no catalog yet
    checks = held_to_its_fields(doctor.report(str(root)))
    catalog = next(c for c in checks if c["id"] == "catalog")
    assert catalog["next"] == "hylly catalog" and catalog["run"] == f"hylly catalog --library {paths.typed(root)}"
    monkeypatch.setenv("HYLLY_LIBRARY", str(root))  # the settings' own library: as the doctor's next says it
    assert next(c for c in held_to_its_fields(doctor.report()) if c["id"] == "catalog")["run"] == "hylly catalog"
    access_ = next(c for c in checks if c["id"] == "access")  # no users yet: a first user takes a password
    assert access_["owner"] is True and "hylly user add" in access_["next"] and "run" not in access_
    from hylly.shops import SHOPS

    shop = next(iter(SHOPS.values()))  # files that failed: another download at the shop, on the owner's login
    (paths.state_dir(root) / shop.failures).parent.mkdir(parents=True, exist_ok=True)
    (paths.state_dir(root) / shop.failures).write_text("path,why\nebooks/a.epub,made up\n", encoding="utf-8")
    failed = next(c for c in held_to_its_fields(doctor.report(str(root))) if c["id"] == "downloads-failed")
    assert failed["owner"] is True and "run" not in failed
    paths.settings_file().write_text('[bell]\nntfy = "https://ntfy.sh/a-made-up-topic"\n', encoding="utf-8")
    old = next(c for c in held_to_its_fields(doctor.report(str(root))) if c["id"] == "settings-old-secrets")
    assert old["owner"] is True and "run" not in old and "a-made-up-topic" not in json.dumps(old)  # a secret: the owner's
    paths.settings_file().write_text("this isn't toml", encoding="utf-8")
    mistake = next(c for c in held_to_its_fields(doctor.report(str(root))) if c["id"] == "settings")
    assert mistake["state"] == "problem" and mistake["owner"] is True
    paths.settings_file().write_text("", encoding="utf-8")
    monkeypatch.setattr(paths, "REQUIRE_MARKER", True)
    (tmp_path / "unmarked").mkdir()
    unmarked = next(c for c in held_to_its_fields(doctor.report(str(tmp_path / "unmarked"))) if c["id"] == "library")
    assert unmarked["owner"] is True and "hylly init" in unmarked["next"] and "run" not in unmarked  # never to get past it
    with pytest.raises(SystemExit):  # a problem or two: exit code 1
        cli.main(["doctor", "--json", "--library", str(root)])
    assert held_to_its_fields(json.loads(capsys.readouterr().out))  # as --json prints it


# ---- hylly doctor: the walled service against the settings ----------------------------------------------------

def test_a_walled_service_that_differs_from_the_settings_is_a_problem_with_its_line_and_the_fix(monkeypatch):
    """Each difference the look at the installed service finds (instance.service_differences: the user, the library, the
    port) is a problem of the walls, naming its file and line, with the line wanted and the printed service to compare;
    the owner's, as it takes sudo. A file that can't be read is a note, and the walls are still there. The look itself is
    tried on made-up services in test_other_systems.py."""
    from hylly import instance

    monkeypatch.setattr(instance, "SYSTEM", "linux")
    monkeypatch.setattr(instance, "system_service", lambda: True)
    monkeypatch.setattr(instance, "running", lambda: None)
    unit = "/etc/systemd/system/hylly.service"
    monkeypatch.setattr(instance, "service_differences", lambda: {"files": [unit], "unread": [], "differences": [
        {"line": "SocketBindAllow=tcp:8770", "file": unit, "says": "SocketBindAllow=tcp:8770, with SocketBindDeny=any, "
         "doesn't let the server listen on port 8780", "want": "SocketBindAllow=tcp:8780"}]})
    found = doctor._walls()
    assert [(c["id"], c["state"], c.get("owner")) for c in found] == [("walls", "problem", True)] and "run" not in found[0]
    assert found[0]["says"] == (f"The system-wide service ({unit}) doesn't match the settings: SocketBindAllow=tcp:8770, "
                                "with SocketBindDeny=any, doesn't let the server listen on port 8780.")
    assert found[0]["next"].startswith(f"Make it SocketBindAllow=tcp:8780 in {unit}: hylly serve --service-file prints")
    assert found[0]["next"].endswith("sudo systemctl daemon-reload and sudo systemctl restart hylly.")
    monkeypatch.setattr(instance, "service_differences", lambda: {"files": [unit], "unread": [unit + ".d/x.conf"], "differences": []})
    assert [c["state"] for c in doctor._walls()] == ["info", "ok"] and "can't be read here" in doctor._walls()[0]["says"]
    monkeypatch.setattr(instance, "service_differences", lambda: None)  # masked, or none to compare
    assert [c["state"] for c in doctor._walls()] == ["ok"]


# ---- hylly doctor: 7-Zip and its RAR part -------------------------------------------------------------------

NO_RAR_PART = ("Formats:\n 0  .....P.....M....a.m....  Rar   rar r00  Rar!..\n 0  .....P.....M....a.m....  Rar5  rar r00  Rar!...\n\n"
               "Codecs:\n 0 4ED   303011B BCJ2\n 0  ED  40108 Deflate\n 0  ED  21 LZMA2\n\nHashers:\n 0    4        1 CRC32\n")


RAR_PART = NO_RAR_PART.replace(" 0  ED  21 LZMA2\n", " 0  ED  21 LZMA2\n 1   D   40301 Rar1\n 1   D   40305 Rar5\n")


def made_up_7zip(folder: Path, listing: str) -> str:
    """A 7-Zip of our own, never this computer's: its banner whatever it's asked, and for 7z i the listing given (Debian's
    7zip without 7zip-rar names Rar and Rar5 among the formats, and no Rar codec). Each run is a line in runs.txt."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "listing.txt").write_text(listing, encoding="utf-8")
    tool = folder / "7z"
    tool.write_text('#!/bin/sh\necho run >> "$(dirname "$0")/runs.txt"\n'
                    "echo '7-Zip (z) 26.02 (x64) : Copyright (c) 1999-2026 Igor Pavlov : 2026-06-25'\n"
                    'if [ "$1" = i ]; then cat "$(dirname "$0")/listing.txt"; fi\n', encoding="utf-8")
    tool.chmod(0o755)
    return str(tool)


def runs_of(tool: str) -> int:
    runs = Path(tool).with_name("runs.txt")
    return len(runs.read_text(encoding="utf-8").splitlines()) if runs.is_file() else 0


def catalog_listing(root: Path, *files: str, heads: dict[str, bytes] | None = None) -> Path:
    """A catalog as a build writes it, listing these files, and the files themselves: each CBR a RAR archive, as most
    are, unless heads gives it other first bytes (a ZIP's, say), or None for one gone from the disk."""
    from urllib.parse import quote

    heads = heads or {}
    for rel in files:
        head = heads.get(rel, RAR if rel.lower().endswith(".cbr") else b"x")
        if head is not None:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(head + b"\0" * 60)
    items = [{"id": f"t{n}", "t": Path(rel).stem, "k": "comic", "src": "comics", "f": [[quote(rel), Path(rel).suffix[1:], 1]]}
             for n, rel in enumerate(files)]
    (root / "catalog").mkdir(parents=True, exist_ok=True)
    (root / "catalog" / "data.js").write_text("window.CATALOG = " + json.dumps({"items": items}) + ";\n", encoding="utf-8")
    return root


@pytest.mark.skipif(sys.platform == "win32", reason="a shell script")
def test_the_doctor_tells_whether_7_zip_has_the_rar_part_most_cbr_comics_need(tmp_path, monkeypatch):
    """By 7-Zip's own list of what it unpacks with: a problem when the catalog lists CBR comics that are RAR archives,
    which then open only if their pages are stored uncompressed, and a note when it lists none, each with the package
    that adds the part. 7-Zip is run once, for its version and its list alike."""
    comics = catalog_listing(tmp_path / "comics", "comics/Lantern Wolf 001.cbr", "comics/Lantern Wolf 002.CBR",
                             "comics/Moonwake 001.cbz")
    others = catalog_listing(tmp_path / "others", "comics/Moonwake 001.cbz")
    with_part, without = made_up_7zip(tmp_path / "with", RAR_PART), made_up_7zip(tmp_path / "without", NO_RAR_PART)
    monkeypatch.setattr(archives, "sevenzip", lambda: with_part)
    monkeypatch.setattr(archives, "sevenzip_info", REAL_SEVENZIP_INFO)  # the made-up ones are asked
    assert archives.sevenzip_info(with_part) == ((26, 2), True) and runs_of(with_part) == 1
    assert doctor._tools(comics) == {"id": "7-zip", "what": "7-Zip", "state": "ok", "says": "It's there (26.02), with its RAR part, for CBR comics."}
    assert runs_of(with_part) == 2  # once for the version and the list
    monkeypatch.setattr(archives, "sevenzip", lambda: without)
    assert archives.sevenzip_info(without) == ((26, 2), False)  # though its formats name Rar5
    found = doctor._tools(comics)
    assert found["state"] == "problem" and "without its RAR part" in found["says"]
    assert "the catalog lists 2 CBR files, and RAR archives among them, such as comics/Lantern Wolf 001.cbr." in found["says"]
    assert found["next"] == archives.no_rar_part() and "7zip-rar" in found["next"] and "7-zip.org" in found["next"]
    found = doctor._tools(others)  # a catalog without CBR comics
    assert found["state"] == "info" and found["says"].endswith("The catalog lists no CBR file now.") and "7zip-rar" in found["next"]
    found = doctor._tools(None)  # no library at all: nothing of a catalog
    assert found["state"] == "info" and "catalog" not in found["says"] and found["says"].endswith("as most aren't.")
    assert states(doctor.report(str(comics)))["7-Zip"] == "problem"  # the report asks of its own library's catalog
    assert states(doctor.report(str(others)))["7-Zip"] == "info"


@pytest.mark.skipif(sys.platform == "win32", reason="a shell script")
def test_a_zip_named_cbr_needs_no_rar_part_and_one_not_read_may_be_a_rar(tmp_path, monkeypatch):
    """Many CBR files are ZIPs, which the reader opens without 7-Zip: told by their first bytes, by the catalog's paths
    only, at most LOOKED_INTO of them. One not read may be a RAR archive, so it still counts; one gone isn't asked."""
    without = made_up_7zip(tmp_path / "without", NO_RAR_PART)
    monkeypatch.setattr(archives, "sevenzip", lambda: without)
    monkeypatch.setattr(archives, "sevenzip_info", REAL_SEVENZIP_INFO)
    names = ["comics/Lantern Wolf 001.cbr", "comics/Lantern Wolf 002.cbr", "comics/Lantern Wolf 003.cbr"]
    zips = catalog_listing(tmp_path / "zips", *names, heads={names[0]: ZIP, names[1]: ZIP, names[2]: None})
    assert doctor._cbr_comics(zips) == (3, "", 0)
    found = doctor._tools(zips)
    assert found["state"] == "info" and found["says"].endswith("The catalog's 3 CBR files are ZIPs, which don't need it.")
    monkeypatch.setattr(doctor, "LOOKED_INTO", 1)
    found = doctor._tools(zips)
    assert found["state"] == "problem" and "those read are ZIPs, which don't need it, but 2 weren't read" in found["says"]
    monkeypatch.setattr(archives, "sevenzip", lambda: "")  # none at all: the same count
    found = doctor._tools(zips)
    assert found["state"] == "problem" and found["says"].startswith("Not found") and found["next"] == archives.no_sevenzip()
    monkeypatch.setattr(doctor, "LOOKED_INTO", 200)
    assert doctor._tools(zips)["state"] == "info"


@pytest.mark.skipif(sys.platform == "win32", reason="a shell script")
def test_a_7_zip_that_doesn_t_list_what_it_unpacks_with_is_a_note_and_on_macos_says_why_it_may_be(tmp_path, monkeypatch):
    """A 7zz downloaded from 7-zip.org may be held back by macOS the first time it runs, and then says nothing."""
    from hylly import instance

    comics = catalog_listing(tmp_path / "comics", "comics/Lantern Wolf 001.cbr")
    quiet = made_up_7zip(tmp_path / "quiet", "Usage: 7z <command> [<switches>...]\n")
    monkeypatch.setattr(archives, "sevenzip", lambda: quiet)
    monkeypatch.setattr(archives, "sevenzip_info", REAL_SEVENZIP_INFO)
    assert archives.sevenzip_info(quiet) == ((26, 2), None)
    monkeypatch.setattr(instance, "SYSTEM", "linux")
    found = doctor._tools(comics)
    assert found["state"] == "info" and "isn't known" in found["says"] and "26.02" in found["says"]
    assert "Privacy & Security" not in found["next"]
    monkeypatch.setattr(instance, "SYSTEM", "mac")
    assert "Privacy & Security" in doctor._tools(comics)["next"] and "right-click, Open" in doctor._tools(comics)["next"]
    assert archives.sevenzip_info(str(tmp_path / "nowhere" / "7z")) == (None, None)  # one that can't be run says nothing


def test_no_7_zip_is_a_problem_only_when_the_catalog_lists_cbr_comics(tmp_path, monkeypatch):
    monkeypatch.setattr(archives, "sevenzip", lambda: "")
    comics = catalog_listing(tmp_path / "comics", "comics/Lantern Wolf 001.cbr")
    found = doctor._tools(comics)
    assert found["state"] == "problem" and found["says"].startswith("Not found") and found["says"].endswith("lists 1 CBR file, a RAR archive, comics/Lantern Wolf 001.cbr.")
    assert found["next"] == archives.no_sevenzip()
    with monkeypatch.context() as m:  # elsewhere than Windows, both name the package that adds the RAR part
        m.setattr(paths, "WINDOWS", False)
        assert "7zip-rar" in archives.no_sevenzip() and "7zip-rar" in archives.no_rar_part()
    for none in (catalog_listing(tmp_path / "others", "ebooks/Ann Writer - Tide.epub"), tmp_path / "unbuilt", None):
        found = doctor._tools(none)
        assert found["state"] == "info" and found["says"].endswith("Other formats don't need it.")
    assert "catalog" not in doctor._tools(None)["says"]


# ---- what is too old, as a check of its own --------------------------------------------------------------------------

def test_the_doctor_tells_of_what_is_old_and_that_it_works(monkeypatch):
    monkeypatch.setattr(updates, "behind", lambda: [{"what": "Python", "have": "3.12.11", "want": "3.12.12", "why": "Fixes.", "next": "uv sync"}])
    result = doctor.report()
    old = [c for c in result["checks"] if c["state"] == doctor.UPDATE]
    assert old[0]["what"] == "Python" and old[0]["next"] == "uv sync" and result["updates"] == 1
    said = doctor.text(result)
    assert "old Python: 3.12.11" in said and "1 thing to update." in said
