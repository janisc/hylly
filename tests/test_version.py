"""One version: pyproject.toml's, which the installed package reports, the same as __version__; hylly --version says it."""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

import pytest

import hylly
from hylly import cli, commands


def test_the_version_is_one_number_and_hylly_version_says_it(capsys, monkeypatch):
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == metadata.version("hylly") == hylly.__version__ == commands.version()
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--version"])
    assert stopped.value.code == 0 and capsys.readouterr().out == f"hylly {project['version']}\n"

    def not_installed(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", not_installed)  # run from its source alone: __version__
    assert commands.version() == hylly.__version__
