"""Hylly's parts keep to their own: every import, at the top of a module or inside a function, written relative or with
hylly's name in full, goes only where parts.py says that part may go; nothing of Hylly's is imported by a name given at
run time, which no reading could follow; the library server, from its modules and from the program hyllyd, can't
reach the shops' logins or the code that talks to the shops by any road at all; and no module reaches itself again
through the others, beyond the cycles still left to undo (CYCLES_LEFT)."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

from hylly import parts

SRC = Path(parts.__file__).parent
SCRIPTS = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]


def modules() -> dict[str, list[Path]]:
    """Each module by its name; a package (lookups) is one, made of its files."""
    out = {p.stem: [p] for p in SRC.glob("*.py")}
    for package in (d for d in SRC.iterdir() if d.is_dir() and (d / "__init__.py").is_file()):
        out[package.name] = sorted(package.glob("*.py"))
    return out


def imports(files: list[Path], package: bool) -> set[str]:
    """The Hylly modules these files import, however and wherever: relative (from . import x, from .x import y) or
    with hylly's name in full (import hylly.x, from hylly import x, from hylly.x import y), the habit of many."""
    found = set()
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name.split(".") for a in node.names]
                found |= {name[1] if len(name) > 1 else "__init__" for name in names if name[0] == "hylly"}
            elif isinstance(node, ast.ImportFrom) and not node.level:
                if node.module == "hylly":
                    found |= {a.name for a in node.names}
                elif (node.module or "").startswith("hylly."):
                    found.add(node.module.split(".")[1])
            elif isinstance(node, ast.ImportFrom):
                up = node.level - (1 if package else 0)  # inside a package, one more level up is hylly
                if up > 1 or (package and up < 1):
                    continue  # within the package itself
                if node.module:
                    found.add(node.module.split(".")[0])
                else:
                    found |= {a.name for a in node.names}
    return found


IMPORTERS = ("import_module", "__import__")  # importlib's and the built-in one


def importers(tree: ast.AST) -> set[str]:
    """The names a file calls IMPORTERS by: their own, and those it binds to them, by an import (from importlib import
    import_module as load) or an assignment (load = importlib.import_module, load = __import__, other = load)."""
    names, more = set(IMPORTERS), True
    while more:
        more = False
        for node in ast.walk(tree):
            bound: list[ast.AST] = []
            if isinstance(node, ast.ImportFrom):
                bound = [ast.Name(a.asname) for a in node.names if a.name in IMPORTERS and a.asname]
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                value = node.value
                if (isinstance(value, ast.Name) and value.id in names) or (isinstance(value, ast.Attribute)
                                                                           and value.attr in IMPORTERS):
                    bound = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in bound:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    more = True
    return names


def by_name(files: list[Path]) -> list[str]:
    """Imports by a name given at run time (importlib.import_module, __import__, or another name bound to either), which
    no reading of the imports can follow, as file:line: each one whose name may be Hylly's, written in full or relative,
    or can't be told. A package looking into itself passes (f"{__name__}.{sid}", how lookups finds its sources), and so
    does another library's."""
    found = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = importers(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute) and node.func.attr in IMPORTERS
                    or isinstance(node.func, ast.Name) and node.func.id in names):
                continue
            name = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "name"), None)
            head = name.values[0] if isinstance(name, ast.JoinedStr) and name.values else name
            if isinstance(head, ast.FormattedValue) and isinstance(head.value, ast.Name) and head.value.id == "__name__":
                continue  # within itself
            if isinstance(head, ast.Constant) and isinstance(head.value, str) and not head.value.startswith(("hylly", ".")):
                continue  # another library's
            found.append(f"{path.relative_to(SRC.parent) if path.is_relative_to(SRC.parent) else path.name}:{node.lineno}")
    return found


GRAPH = {name: imports(files, len(files) > 1 or files[0].parent != SRC) - {name} for name, files in modules().items()}
GRAPH = {name: {d for d in deps if d in GRAPH} for name, deps in GRAPH.items()}  # a name imported from __init__ isn't a module


def entry(program: str) -> tuple[str, str]:
    """The module and the function a program that pyproject.toml installs starts in: hyllyd's is daemon's main."""
    module, function = SCRIPTS[program].split(":")
    return module.removeprefix("hylly."), function


def test_every_module_is_in_one_part():
    placed = [m for ms in parts.PARTS.values() for m in ms]
    assert len(placed) == len(set(placed)), "a module in two parts"
    assert set(placed) == set(GRAPH), f"not placed: {set(GRAPH) - set(placed)}; gone: {set(placed) - set(GRAPH)}"


def test_each_part_imports_only_where_it_may():
    wrong = sorted(f"{m} ({parts.part_of(m)}) imports {d} ({parts.part_of(d)})" for m, deps in GRAPH.items() for d in deps
                   if parts.part_of(d) not in parts.MAY[parts.part_of(m)])
    assert not wrong, "\n".join(wrong)


def reach(start: set[str], graph: dict[str, set[str]] = GRAPH) -> set[str]:
    seen, todo = set(start), list(start)
    while todo:
        for d in graph[todo.pop()]:
            if d not in seen:
                seen.add(d)
                todo.append(d)
    return seen


def test_the_server_cant_reach_the_shops_or_their_logins_by_any_road():
    """From every module of the server's part, and from the one hyllyd starts in, wherever pyproject.toml has it."""
    reached = reach(set(parts.PARTS["server"]) | {entry("hyllyd")[0]})
    assert not {m for m in reached if parts.part_of(m) in parts.SERVER_NEVER}, sorted(reached)
    assert "logins" not in reached and "download" not in reached and "cli" not in reached


def test_nothing_of_hylly_s_is_imported_by_a_name_given_at_run_time():
    """importlib.import_module("hylly.logins") in the server would pass every reading of its imports: a plain import says
    the same and is read."""
    assert not by_name([path for files in modules().values() for path in files])


@pytest.mark.parametrize("line", ["from hylly import logins", "import hylly.logins", "import hylly.logins as kept",
                                  "from hylly.logins import load", "from hylly.lookups import comicvine",
                                  "from . import logins"])
def test_an_import_written_in_full_is_read_as_a_relative_one_is(tmp_path, line):
    """A made-up module of the server's that imports the shops' logins, or a lookup source, inside a function: however
    it's written, the road to them is found."""
    planted = tmp_path / "planted.py"
    planted.write_text(f'"""Made up."""\n\n\ndef later():\n    {line}\n', encoding="utf-8")
    graph = {**GRAPH, "planted": imports([planted], package=False) & set(GRAPH)}
    assert {m for m in reach({"planted"}, graph) if parts.part_of(m) in parts.SERVER_NEVER}


@pytest.mark.parametrize("call, caught", [('importlib.import_module("hylly.logins")', True), ('__import__("hylly.logins")', True),
                                          ('import_module(".logins", "hylly")', True), ("importlib.import_module(name)", True),
                                          ('importlib.import_module(f"hylly.{name}")', True),
                                          ('importlib.import_module(name=f"{name}.logins")', True),
                                          ('im("hylly.logins")', True), ('load("hylly.logins")', True), ('get("hylly.logins")', True),
                                          ('again("hylly.logins")', True), ('builtins.__import__("hylly.logins")', True),
                                          ('importlib.import_module("json")', False), ('im("json")', False),
                                          ('importlib.import_module(f"{__name__}.{name}")', False)])
def test_an_import_by_a_name_given_at_run_time_is_caught(tmp_path, call, caught):
    """Only another library, or a package looking into itself, passes: a name that may be Hylly's, or that can't be
    told, is caught, whatever name the importer is called by."""
    planted = tmp_path / "planted.py"
    planted.write_text(f'"""Made up."""\n\nimport builtins\nimport importlib\nfrom importlib import import_module, import_module as im\n\n'
                       f'load = importlib.import_module\nget = __import__\nagain = load\n\n\ndef later(name):\n'
                       f'    return {call}\n', encoding="utf-8")
    assert by_name([planted]) == (["planted.py:13"] if caught else [])


def cycles(graph: dict[str, set[str]] = GRAPH) -> set[frozenset[str]]:
    """The import cycles: each group of modules that reach one another (two or more), by every import, at the top of a
    module or inside a function alike, as GRAPH has them."""
    return {frozenset(m for m in reach({start}, graph) if start in reach({m}, graph)) for start in graph
            if any(start in reach({d}, graph) for d in graph[start])}


# The cycles still left, each through imports inside functions: each to be undone by what both sides need going below
# them both, as the archives' helpers are below meta, the reader and the PDF worker, and then taken off this list. None
# may be added.
CYCLES_LEFT = {frozenset({"rules", "settings"}), frozenset({"copies", "download", "shoplist", "shopping"})}


def test_no_module_reaches_itself_again_by_any_road():
    """No import cycle beyond those still left, at the top of a module or hidden behind an import inside a function:
    such an import, moved to the top by a tidy contributor or a tool, breaks the program's start. None goes through
    cli, the server, the reader or meta: what the doctor, the server page and the PDF worker take from them is below."""
    found = cycles()
    assert not found - CYCLES_LEFT, f"new import cycles: {[sorted(c) for c in found - CYCLES_LEFT]}"
    assert not CYCLES_LEFT - found, f"undone, so off CYCLES_LEFT now: {[sorted(c) for c in CYCLES_LEFT - found]}"
    assert not {"cli", "serve", "reader", "meta"} & {m for c in found for m in c}


def test_an_import_inside_a_function_that_closes_a_cycle_is_found(tmp_path):
    """A made-up module that imports the archives' helpers inside a function, under a made-up archives that imports it
    at the top: the cycle is found, though no import at the top of both closes it."""
    planted = tmp_path / "planted.py"
    planted.write_text('"""Made up."""\n\n\ndef later():\n    from .archives import read_member\n', encoding="utf-8")
    graph = {**GRAPH, "planted": imports([planted], package=False) & set(GRAPH), "archives": GRAPH["archives"] | {"planted"}}
    assert frozenset({"archives", "planted"}) in cycles(graph) and frozenset({"archives", "planted"}) not in cycles()


def test_the_copies_need_nothing_but_the_core():
    assert {parts.part_of(m) for m in reach(set(parts.PARTS["copy"]))} <= {"core", "copy"}


def test_the_periodic_jobs_are_the_server_s_and_the_nightly_copy_is_a_copy():
    """periodic.py, the server's periodic jobs in one place, is the server's, and reaches no shop by any road (above);
    the nightly copy, moved out of people.py, is with the copies, and needs nothing but the core."""
    assert parts.part_of("periodic") == "server" and parts.part_of("dbcopy") == "copy"
    assert {"bell", "dbcopy", "updates"} <= GRAPH["periodic"] and "dbcopy" not in GRAPH["people"]
    assert {parts.part_of(m) for m in reach({"dbcopy"})} <= {"core", "copy"}


def test_the_catalog_as_served_is_the_records_and_the_feeds_are_the_server_s():
    """library.py, the catalog as the server shows it, your changes laid over it, is in the records' part and reaches
    nothing of the server's, so the commands that read the catalog so (hylly lookup, reviews, reports, tag and set) and
    the doctor's looks at it take it from there, without the server; the feeds (opds.py) are the server's, and serve.py
    takes them from there."""
    assert parts.part_of("library") == "records" and parts.part_of("opds") == "server"
    assert not {m for m in reach({"library"}) if parts.part_of(m) == "server"}
    assert {"library", "opds"} <= GRAPH["serve"] and "library" in GRAPH["opds"]
    for module, wanted in (("cli", {"cmd_lookup", "cmd_reviews", "cmd_reports", "cmd_tag", "cmd_set"}),
                           ("doctor", {"_catalog", "_cant_open", "_cbr_comics"})):
        tree = ast.parse((SRC / f"{module}.py").read_text(encoding="utf-8"))
        functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in wanted]
        assert len(functions) == len(wanted), module
        for function in functions:
            taken = {(n.module or a.name).split(".")[0] for n in ast.walk(function) if isinstance(n, ast.ImportFrom)
                     and n.level == 1 for a in n.names}
            assert "library" in taken and "serve" not in taken, (module, function.name, taken)


def test_a_command_that_stays_on_this_computer_starts_without_the_network_s_code(tmp_path):
    """hylly --version, and the commands that never go online, start without loading httpx or the shops' code: only a
    command that goes online loads it, where it's used (Humble's login check, the downloads, the lookups), and the
    network's trouble is caught as such only once it has been (cli._network_trouble)."""
    import os
    import subprocess
    import sys

    code = ("import sys\nfrom hylly import cli\ntry:\n    cli.main(['--version'])\nexcept SystemExit:\n    pass\n"
            "print(' '.join(sorted(m for m in sys.modules if m.split('.')[0] in ('httpx', 'hylly'))))")
    env = {**os.environ, "PYTHONPATH": str(SRC.parent), "HYLLY_HOME": str(tmp_path / "home"), "LOCALAPPDATA": str(tmp_path)}
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr
    loaded = set(done.stdout.split("\n")[-2].split())
    assert "hylly.cli" in loaded and not {m for m in loaded if m.split(".")[0] == "httpx"}
    assert not loaded & {f"hylly.{m}" for m in ("humble", "shopping", "shopsession", "storybundle", "drivethrurpg", "itchio", "lookups")}


def test_hyllyd_is_the_servers_and_loads_none_of_the_shops_or_their_logins(tmp_path):
    """Not only by its imports as written, but as it runs: hyllyd's options parsed, and its start up to the serving
    (a dry run), in a Python of its own, load no module of a part the server may never reach, nor the code that talks
    to the shops or the lookup sources."""
    import os
    import subprocess
    import sys

    module, function = entry("hyllyd")  # the program as pyproject.toml installs it
    assert parts.part_of(module) == "server"
    code = (f"import sys, hylly.{module} as d; from hylly.commands import daemon_parser; daemon_parser().parse_args(['--port', '8810']); "
            f"d.{function}(['--dry-run', '--library', {str(tmp_path)!r}]); "
            "print(' '.join(sorted(m.split('.')[1] for m in sys.modules if m.startswith('hylly.'))))")
    env = {**os.environ, "PYTHONPATH": str(SRC.parent), "HYLLY_HOME": str(tmp_path / "home"), "LOCALAPPDATA": str(tmp_path)}
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr
    loaded = set(done.stdout.split("\n")[-2].split())
    assert {module, "serve", "commands"} <= loaded
    never = {m for m in loaded if parts.part_of(m) in parts.SERVER_NEVER}
    assert not never and not loaded & {"logins", "humble", "storybundle", "drivethrurpg", "itchio", "download", "lookups", "cli"}


def table(name: str) -> str:
    """The table of docs/architecture.md that a comment names as made from that file, up to its end."""
    doc = (Path(__file__).parents[1] / "docs" / "architecture.md").read_text(encoding="utf-8")
    start = doc.index(f"<!-- {name} -->")
    return doc[start:doc.index("<!-- end -->", start)]


def test_the_architecture_doc_lists_the_parts_as_they_are():
    block = table("parts.py")
    for part, modules in parts.PARTS.items():
        row = next((line for line in block.splitlines() if line.startswith(f"| {part} |")), "")
        assert row and all(f"`{m}`" in row for m in modules), f"docs/architecture.md: the {part} row is out of date"
    assert block.count("\n| ") - 1 == len(parts.PARTS)


def test_the_architecture_doc_lists_the_programs_as_they_are_installed():
    """Its table of programs, kept by hand, is held to what pyproject.toml installs, each program in the part of
    the module it starts, as the parts table is to parts.py: a program added, gone or moved can't leave it behind."""
    import tomllib

    scripts = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in table("pyproject.toml").splitlines()[3:]]
    assert {row[0]: row[-1] for row in rows} == {f"`{name}`": parts.part_of(entry.split(":")[0].removeprefix("hylly."))
                                                 for name, entry in scripts.items()}
