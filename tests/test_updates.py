"""What is too old, and how to update it (updates.py): the floors of the project's libraries and the versions its locks
give, Hylly's own libraries, Python and 7-Zip each told with the next step for the way Hylly was installed, the server
page's line, and the weekly audit of what the lock installs. They came with the PDF worker, as MuPDF was the first library
given a floor of its own, and were kept among its tests (test_pdfworker.py) until each module's tests went to the file
named after it."""

from __future__ import annotations

import re
import sys
import time
import tomllib
from datetime import date
from pathlib import Path

import pytest

from hylly import archives, updates

from helpers import REPO, workflow

REAL_SEVEN_ZIP_VERSION = updates.seven_zip_version  # tests/conftest.py stands in for it: this computer's 7-Zip isn't asked


# ---- the floors, and the locks that keep to them ---------------------------------------------------------------------

def test_the_floors_here_are_those_of_the_project():
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    floors = dict(req.split(">=") for req in project["project"]["dependencies"])
    for name, (_, floor, _) in updates.PACKAGES.items():
        assert floors[name] == floor


def test_the_tool_overrides_say_what_the_project_says():
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    written = [line.strip() for line in (REPO / "tool-overrides.txt").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert written == project["tool"]["uv"]["override-dependencies"]


def test_the_tool_constraints_are_the_versions_of_the_lock():
    """uv tool install takes no lock, so the hylly command gets uv.lock's versions as constraints, the ones the tests
    and the weekly audit check: each library the program needs, from the lock, and nothing else. What the overrides leave
    out (sys_platform == 'never') isn't there, and nor is what only the tests need."""
    lock = {(p["name"], p["version"]): p for p in tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))["package"]}
    by_name = {}
    for (name, version), package in lock.items():
        by_name.setdefault(name, []).append(package)
    left_out = {line.split(";")[0].strip() for line in (REPO / "tool-overrides.txt").read_text(encoding="utf-8").splitlines() if line.strip()}
    needed, waiting = set(), [d["name"] for d in by_name["hylly"][0].get("dependencies", [])]
    while waiting:
        name = waiting.pop()
        if name in left_out or name in {n for n, _ in needed}:
            continue
        for package in by_name[name]:
            needed.add((name, package["version"]))
            waiting += [d["name"] for d in package.get("dependencies", [])]
    lines = [line.strip() for line in (REPO / "tool-constraints.txt").read_text(encoding="utf-8").splitlines()]
    written = {tuple(line.split(";")[0].strip().split("==")) for line in lines if line and not line.startswith("#")}
    assert written == needed and {"pymupdf", "pillow", "httpx"} <= {n for n, _ in written}
    for place in ("README.md", "src/hylly/help.html", "docs/architecture.md"):  # every install the docs give takes them
        text = (REPO / place).read_text(encoding="utf-8")
        if place.endswith(".html"):  # the install as the help is given it
            text = text.replace("{{install}}", updates.install_command())
        installs = [re.split(r"[<`\n]", found)[0] for found in text.split("uv tool install")[1:]]
        assert installs and all(re.search(r"--constraints (?:https://\S+/)?tool-constraints\.txt\b", found) for found in installs), (place, installs)
        assert all("tool-overrides.txt" in found for found in installs), (place, installs)


# ---- Hylly's own libraries and Python, and how to update them --------------------------------------------------------

def test_an_old_library_is_told_with_how_to_update_it():
    old = updates._packages(lambda name: {"pymupdf": "1.25.5", "pillow": "13.0.0"}[name])
    assert [(f["what"], f["have"], f["want"]) for f in old] == [("PyMuPDF", "1.25.5", "2.1")]
    assert "CVE-2026-3308" in old[0]["why"] and "uv" in old[0]["next"]
    assert updates._packages(lambda name: "99.0") == []


def test_how_to_update_goes_by_how_it_was_installed(tmp_path, monkeypatch):
    """Run from a checkout by uv run: uv sync there. Installed with uv tool install: that install again, over itself, as
    upgrade keeps the versions it was given and the lock's new ones come with a new install; from the project's address
    with its constraints, or for one installed from a checkout, in that checkout, as its receipt names it."""
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    assert updates._sync() == "In Hylly's checkout: uv sync"
    (tmp_path / "uv-receipt.toml").write_text('[tool]\nrequirements = [{ name = "hylly", git = "https://example.org/x" }]\n',
                                              encoding="utf-8")
    assert updates._sync() == updates.install_command()
    assert updates.install_command().startswith(
        "uv tool install --force --python 3.14 git+https://github.com/janisc/hylly@main --overrides https://")
    assert updates.install_command().endswith(" --constraints https://raw.githubusercontent.com/janisc/hylly/main/tool-constraints.txt")
    (tmp_path / "uv-receipt.toml").write_text('[tool]\nrequirements = [{ name = "hylly", editable = "/home/ann/hylly" }]\n',
                                              encoding="utf-8")
    assert updates._sync() == (
        "In /home/ann/hylly: uv tool install --force --editable . --overrides tool-overrides.txt --constraints tool-constraints.txt")
    newest = ".".join(map(str, max(updates.PYTHONS)))  # another Python: the same install, as upgrade would also take the
    assert updates._python((3, 12, 11), date(2026, 11, 4))[0]["next"] == (  # newest Hylly under the old versions, untried
        f"In /home/ann/hylly: uv tool install --force --python {newest} --editable . --overrides tool-overrides.txt "
        "--constraints tool-constraints.txt (uv fetches that Python).")
    (tmp_path / "uv-receipt.toml").write_text("not toml [", encoding="utf-8")  # one that can't be read: as from the address
    assert updates._sync() == updates.install_command()
    assert updates._python((3, 12, 11), date(2026, 11, 4))[0]["next"] == (  # the install names that Python already
        updates.install_command() + " (uv fetches that Python).") and f" --python {newest} " in updates.install_command()
    assert "upgrade" not in updates._python((3, 12, 11), date(2026, 11, 4))[0]["next"]
    assert updates._sync("3.15") == updates.install_command().replace(f" --python {newest} ", " --python 3.15 ")


def test_an_installed_package_without_a_checkout_is_told_to_update_as_it_was_installed(tmp_path, monkeypatch):
    """Neither uv's tool nor a checkout of the source (a package installed into a site-packages, as a container's
    image may have it): it was told "In Hylly's checkout: uv sync", with no checkout to go to. Now the words are for an
    installed package, with the install as the help gives it. A checkout, uv run's or an editable install's, as before."""
    monkeypatch.setattr(sys, "prefix", str(tmp_path))  # no uv receipt
    package = tmp_path / "lib" / "python3.14" / "site-packages" / "hylly"
    package.mkdir(parents=True)
    monkeypatch.setattr(updates, "HERE", package)
    said = updates._sync()
    assert "checkout:" not in said and "uv sync" not in said and "installed package" in said
    assert said.endswith(f"install it as the help says: {updates.install_command()}")
    newest = ".".join(map(str, max(updates.PYTHONS)))
    later = updates._sync("3.15")
    assert "and Python 3.15;" in later and later.endswith(updates.install_command().replace(f" --python {newest} ", " --python 3.15 "))
    (tmp_path / "src" / "hylly").mkdir(parents=True)  # a checkout: its pyproject.toml beside src
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"hylly\"\n", encoding="utf-8")
    monkeypatch.setattr(updates, "HERE", tmp_path / "src" / "hylly")
    assert updates._sync() == "In Hylly's checkout: uv sync" and updates._from_a_checkout()


def test_the_install_asks_uv_for_the_newest_python_the_table_knows():
    """uv tool install, and uv sync in a checkout, took any Python they found that would do, an older one too, which
    the doctor then called old. The install asks for the newest Python whose fixes PYTHONS lists, and so do the docs'
    uv sync and editable installs, by the same number; no .python-version file."""
    import hylly

    newest = ".".join(map(str, max(updates.PYTHONS)))
    assert hylly.PYTHON == newest and f"uv tool install --force --python {newest} git+" in hylly.INSTALL
    assert not (REPO / ".python-version").exists()
    for place in ("README.md", "CONTRIBUTING.md", "AGENTS.md", "docs/architecture.md"):
        text = (REPO / place).read_text(encoding="utf-8")
        steps = re.findall(r"uv sync[^`\n]*|uv tool install [^`\n]*", text)
        assert steps and all(f"--python {newest}" in step for step in steps), (place, steps)


def test_an_early_python_of_the_install_s_own_version_is_told_to_upgrade_it_first(tmp_path, monkeypatch):
    """An early 3.14 (3.14.0, under the 3.14.1 that has the fixes) was told to install again with --python 3.14, which
    takes the same 3.14.0 uv has already. Now it's told uv python install --upgrade first, then the install again; an
    older version than the install's, as before, the install again alone, as uv fetches that Python."""
    newest = ".".join(map(str, max(updates.PYTHONS)))
    early = (*max(updates.PYTHONS), 0)
    assert early < updates.PYTHONS[max(updates.PYTHONS)]
    monkeypatch.setattr(updates, "_is_a_tool", lambda: False)
    (found,) = updates._python(early, date(2026, 11, 4))
    assert found["next"] == (f"uv python install --upgrade {newest} first, which brings uv's own newest {newest}, as the "
                             f"install again alone would take this {'.'.join(map(str, early))} again; then: "
                             f"In Hylly's checkout: uv sync --python {newest}.")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    (tmp_path / "uv-receipt.toml").write_text('[tool]\nrequirements = [{ name = "hylly", git = "https://example.org/x" }]\n',
                                              encoding="utf-8")
    monkeypatch.setattr(updates, "_is_a_tool", lambda: True)
    (found,) = updates._python(early, date(2026, 11, 4))
    assert found["next"].startswith(f"uv python install --upgrade {newest} first") and found["next"].endswith(
        f"then: {updates.install_command()}.")
    (older,) = updates._python((3, 12, 11), date(2026, 11, 4))
    assert older["next"] == updates.install_command() + " (uv fetches that Python)." and "uv python" not in older["next"]


def test_an_old_python_is_told():
    now = date(2026, 11, 4)
    assert [f["want"] for f in updates._python((3, 12, 11), now)] == ["3.12.12"]
    assert updates._python((3, 12, 12), now) == [] and updates._python((3, 14, 1), now) == []
    assert updates._python((3, 99, 0), now) == []  # one newer than this knows of
    ended = updates._python((3, 11, 18), date(2027, 11, 1))
    assert "no more security fixes" in ended[0]["why"]


def test_the_python_to_move_to_is_the_newest_the_table_knows(monkeypatch):
    """The next step names the newest Python whose fixes PYTHONS lists, not a number written by hand beside it,
    and the help's Keeping it up to date says the same one."""
    monkeypatch.setattr(updates, "_is_a_tool", lambda: False)
    newest = ".".join(map(str, max(updates.PYTHONS)))
    assert updates._python((3, 12, 11), date(2026, 11, 4))[0]["next"] == f"In Hylly's checkout: uv sync --python {newest} (uv fetches that Python)."
    monkeypatch.setitem(updates.PYTHONS, (3, 15), (3, 15, 0))
    assert "--python 3.15 " in updates._python((3, 12, 11), date(2026, 11, 4))[0]["next"]
    page = (REPO / "src" / "hylly" / "help.html").read_text(encoding="utf-8")
    assert set(re.findall(r"--python (\d+\.\d+)", page)) == {newest}


def test_a_version_is_compared_as_a_version():
    assert updates.numbers("2.1") == (2, 1, 0, 0) and updates.numbers("13.0.0") == updates.numbers("13.0")
    assert updates.numbers("13rc1") < updates.numbers("13.0") and updates.numbers("13.0.0b2") < updates.numbers("13.0")
    assert updates.numbers("2.1.post1") == (2, 1, 0, 0) and updates.numbers("nonsense") < updates.numbers("0.1")
    assert len(updates.numbers("9" * 5000)) == 4  # far too long for Python to read as a number: no trouble here


# ---- 7-Zip -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("printed, version", [
    (b"\r\n7-Zip 26.02 (x64) : Copyright (c) 1999-2026 Igor Pavlov : 2026-06-25\r\n\r\nUsage: 7z <command>", (26, 2)),
    (b"7-Zip (z) 24.09 (x64) : Copyright (c) 1999-2024 Igor Pavlov : 2024-11-29\n", (24, 9)),
    (b"7-Zip [64] 16.02 : Copyright (c) 1999-2016 Igor Pavlov : 2016-05-21\np7zip Version 16.02 (locale=C,Utf16=off)", (16, 2)),
    (b"something else altogether 1.2\n", None),
])
def test_7_zip_says_which_it_is(monkeypatch, printed, version):
    monkeypatch.setattr(updates.subprocess, "run", lambda *a, **k: k["stdout"].write(printed))
    assert REAL_SEVEN_ZIP_VERSION("7z") == version


def test_an_old_7_zip_is_told_and_a_missing_one_isnt(monkeypatch):
    monkeypatch.setattr(archives, "sevenzip", lambda: "/usr/bin/7z")
    monkeypatch.setattr(updates, "seven_zip_version", lambda tool: (16, 2))
    found = updates._seven_zip()
    assert (found[0]["have"], found[0]["want"]) == ("16.02", "26.02") and "CVE-2026-14266" in found[0]["why"]
    monkeypatch.setattr(updates, "seven_zip_version", lambda tool: (26, 3))
    assert updates._seven_zip() == []
    monkeypatch.setattr(updates, "seven_zip_version", lambda tool: None)
    assert updates._seven_zip() == []
    monkeypatch.setattr(archives, "sevenzip", lambda: "")
    assert updates._seven_zip() == []


def test_7_zip_that_cant_be_run_says_nothing():
    assert REAL_SEVEN_ZIP_VERSION(str(Path("nowhere") / "7z")) is None


@pytest.mark.skipif(sys.platform == "win32", reason="a shell script")
def test_a_7_zip_that_starts_something_else_keeps_to_the_time(tmp_path):
    tool = tmp_path / "7z"
    tool.write_text("#!/bin/sh\nsleep 30 &\necho '7-Zip (z) 26.02 (x64)'\n", encoding="utf-8")
    tool.chmod(0o755)
    began = time.monotonic()
    assert REAL_SEVEN_ZIP_VERSION(str(tool)) == (26, 2) and time.monotonic() - began < 8


# ---- the server page, and the weekly audit ---------------------------------------------------------------------------

def test_the_server_page_is_told_too(monkeypatch):
    updates.behind_once.cache_clear()
    monkeypatch.setattr(updates, "behind", lambda: [{"what": "7-Zip", "have": "16.02", "want": "26.02", "why": "Fixes.", "next": "Get it."}])
    assert updates.behind_once()[0]["what"] == "7-Zip"
    assert updates.line(updates.behind_once()[0]).startswith("Worth updating: 7-Zip 16.02, older than 26.02.")
    updates.behind_once.cache_clear()


def test_the_audit_runs_on_what_is_installed():
    """pip-audit on what uv.lock installs, as GitHub reads the workflow: every week, and on any change to the lock or
    to what it's made from."""

    audit = workflow("audit.yml")
    assert [when["cron"] for when in audit["on"]["schedule"]] and "workflow_dispatch" in audit["on"]
    assert all({"pyproject.toml", "uv.lock"} <= set(audit["on"][event]["paths"]) for event in ("push", "pull_request"))
    runs = [step.get("run", "") for step in audit["jobs"]["audit"]["steps"]]
    exported = next(run for run in runs if run.startswith("uv export"))
    assert "--frozen" in exported.split() and "-o requirements.txt" in exported  # what the lock has, not newer
    assert any("pip-audit" in run and "-r requirements.txt" in run for run in runs)


def test_nothing_odd_about_the_versions_trips_the_server_up(monkeypatch):
    assert updates._packages(lambda name: None) == []
    updates.behind_once.cache_clear()
    monkeypatch.setattr(updates, "behind", lambda: 1 / 0)
    assert updates.behind_once() == ()
    updates.behind_once.cache_clear()
