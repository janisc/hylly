"""How far the browser reads ahead: the [reader] settings, with their limits. How much memory the pages take is with the
rest of how pages are kept (test_drawn_pages.py)."""

from __future__ import annotations

import threading

import httpx
import pytest

from hylly import catalog, serve, settings
from hylly.exits import Exit

from helpers import build_library


def test_the_defaults_hold_with_no_settings(toml):
    toml("")
    said = settings.get("reader")
    assert {key: said[key] for key in settings.DEFAULTS["reader"]} == settings.DEFAULTS["reader"]  # as reviewed: test_rules.py


@pytest.mark.parametrize("key, value, went", [("memory_mb", 1048576, 16384), ("memory_mb", 1, 16), ("pages_ahead", 500, 30), ("pages_ahead_fast", 1000, 60),
                                              ("sharp_ahead", 99, 5), ("tiles_ahead", 99999, 30), ("draw_ahead", 500, 200)])
def test_a_number_past_its_limits_is_told_and_the_nearest_goes(toml, capsys, key, value, went):
    toml(f"{key} = {value}\n")
    assert settings.get("reader")[key] == went
    err = capsys.readouterr().err
    assert key in err and "past its limits" in err and str(went) in err


def test_a_number_within_them_is_used_and_nothing_is_said(toml, capsys):
    toml("memory_mb = 2048\npages_ahead = 0\npages_ahead_fast = 20\n")
    said = settings.get("reader")
    assert (said["memory_mb"], said["pages_ahead"], said["pages_ahead_fast"]) == (2048, 0, 20)
    assert "limits" not in capsys.readouterr().err


@pytest.mark.parametrize("text", ['pages_ahead = "lots"', "pages_ahead = 2.5", "memory_mb = -1", "memory_mb = inf", "memory_mb = true"])
def test_what_isnt_a_whole_number_stops_as_other_mistakes_do(toml, text):
    toml(text + "\n")
    with pytest.raises(Exit):
        settings.get("reader")


def test_the_page_is_told_how_far(tmp_path, toml, log_in):
    toml("pages_ahead = 3\npages_ahead_fast = 1\nsharp_ahead = 0\ntiles_ahead = 12\n")
    root = tmp_path / "lib"
    build_library(root)
    catalog.build(root, log=lambda s: None)
    srv, _ = serve.make_server(root, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}"), "reader") as client:
            said = client.get("/api/session").json()["ahead"]
    finally:
        srv.shutdown()
        srv.server_close()
    assert said == {"pages": 3, "fast": 3, "sharp": 0, "tiles": 12, "drawn": 32}  # turning fast is never less than the usual
