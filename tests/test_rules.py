"""The rules files: the shipped ones, your own on top of them by the four rules, where yours are found, what a
mistake says, and hylly rules."""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
import tomllib
import unicodedata
from pathlib import Path
from urllib.parse import unquote

import pytest

from hylly import archives, catalog, cli, commands, lookups, meta, paths, plan, rules, words
from hylly.exits import NOT_SET_UP
from hylly.library import Device, ereader_feed, kinds_table

from helpers import page_data

REAL_SEVENZIP = archives.sevenzip  # tests/conftest.py stands in for it, as none found: this computer's isn't asked


def mine(text: str, name: str = "mine.toml"):
    """A rules file of your own, in the rules folder next to the settings: read with the rest as the next command would
    read them, or, while watching, as a running server finds it."""
    folder = paths.settings_file().parent / "rules"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")
    if not rules.WATCH:
        rules.reset()
    return folder / name


def test_the_shipped_files_load_and_are_neutral():
    r = rules.shipped()
    assert [p.name for p in r.files] == [f"{n}.toml" for n in rules.SHIPPED]
    assert len(r.genres) >= 20 and len(r.games) >= 40 and {"epub", "pdf", "cbz"} <= r.shop_formats
    assert "chm" not in r.formats  # not a book: Windows opens it in a viewer where its scripts run
    assert meta.from_filename("The Moon Garden (shelf copy)") == ("The Moon Garden (shelf copy)", [])  # tags are yours to add
    assert rules.current().files == r.files  # tests have no rules of their own


def test_the_shipped_rules_are_as_reviewed(request):
    """Every value in src/hylly/data, in its order (the first genre or game a name finds wins), against
    tests/shipped_rules.json, so that no change to the shipped rules goes by unseen. After a change that is meant,
    and reviewed, write it again, with tests/shipped_word_answers.json (what the series and junk words find), and
    commit them together with the change:
        uv run python -m pytest tests/test_rules.py -k as_reviewed --new-snapshot"""
    import difflib

    snapshot = Path(__file__).with_name("shipped_rules.json")
    now = json.dumps(rules.shipped().data, ensure_ascii=False, indent=1) + "\n"
    if request.config.getoption("--new-snapshot"):
        snapshot.write_text(now, encoding="utf-8")
    was = snapshot.read_text(encoding="utf-8").splitlines()
    differ = [line.strip() for line in difflib.unified_diff(was, now.splitlines(), lineterm="", n=0)
              if line[:1] in "-+" and line[:3] not in ("---", "+++")]
    assert now.splitlines() == was, ("A shipped rule in src/hylly/data changed (a word added, say): tests/shipped_rules.json "
                                     "has every value as reviewed. Once the change is reviewed and meant, write it again, "
                                     "and tests/shipped_word_answers.json with it, and commit them together: uv run "
                                     "python -m pytest tests/test_rules.py -k as_reviewed --new-snapshot. What differs "
                                     f"(- as reviewed, + now): {differ[:8]}{' and more' if len(differ) > 8 else ''}")


def test_the_default_settings_are_as_reviewed():
    """settings.DEFAULTS and the e-reader feed's, and settings.example.toml and docs/rules.md saying the same: change
    them together."""
    from hylly import PROJECT_URL, settings

    windows = r"C:\Program Files\7-Zip\7z.exe"
    feed = {"kinds": ["book", "tech"], "formats": [], "max_mb": 63, "skip_tablet": True,  # []: the e-reader's that flow
            "pdf_from": ["humble", "storybundle", "drivethrurpg"]}
    reviewed = {
        "download": {"ereader": ["epub", "azw3", "mobi", "azw", "prc", "fb2", "pdf", "djvu", "cbz", "cbr", "cb7", "txt"],
                     "tablet": ["cbz", "cbr", "cb7", "largest", "pdf", "epub"],
                     "rpg": ["pdf", "cbz", "cbr", "cb7", "largest", "epub"], "size_tie": 0.95, "language": "",
                     "unsure": "guess"},  # guess and list, by default
        "devices": {"fixed_layout": "tablet", "image_heavy_mb": 21},  # where a kind is read: its [kinds.X] device
        "reader": {"cache_gb": 10, "cache_folder": "", "book_fonts": True, "word_meanings": True, "memory_mb": 128, "pages_ahead": 5, "pages_ahead_fast": 9, "sharp_ahead": 2, "tiles_ahead": 6,
                   "draw_ahead": 32},
        "lookups": {"enabled": True, "order": [], "accept": 0.8, "none_days": 180, "contact": ""},  # []: every source
        "reviews": {"who": "off", "moderation": "off"},
        "look": {"theme": "hylly", "themes": []},
        "tools": {"sevenzip": windows},
        "remote": {"ssh": "", "programs": ""},
        "server": {"name": "Library", "listen": "127.0.0.1", "port": 8770, "access_log": True,
                   "proxy_header": "X-Forwarded-For", "trusted_proxies": ["127.0.0.0/8", "::1"], "allowed_hosts": [], "address": "",
                   "jobs": "internal", "source_url": PROJECT_URL},  # Hylly's own source, the project's page
        "bell": {"toast": True, "wrong_passwords": 3, "downloads_per_hour": 10, "new_devices": True,
                 "library_missing": 5, "quiet_minutes": 60, "alarms_per_hour": 10, "remote_detail": "counts",
                 "daily_summary": 8, "command": []},  # the ntfy address and token: secrets, in files of their own;
                                                       # command: none, so only ntfy as before
        "ereader_feed": feed,
        "backup": {"target": "", "keep": {}},  # no target, and every zip kept, as before
        "inbox": {"folder": "", "folders": [], "into": {}, "lookup": True, "strip_suffix": ""},  # no Inbox; into: as the
    }                                                                                            # rules give the kinds folders
    here = {**reviewed, "tools": {"sevenzip": windows if paths.WINDOWS else ""}}  # elsewhere: 7z or 7zz on the PATH
    assert settings.DEFAULTS == here and list(settings.DEFAULTS) == list(reviewed)
    kinds = {"book": [], "tech": [], "rpg": reviewed["download"]["rpg"], "comic": [], "manual": []}  # []: its device's
    assert settings.section_defaults("download") == {"ereader": reviewed["download"]["ereader"],  # a list for each kind
                                                     "tablet": reviewed["download"]["tablet"], **kinds, "size_tie": 0.95,
                                                     "language": "", "unsure": "guess"}
    assert list(settings.section_defaults("download")) == ["ereader", "tablet", *kinds, "size_tie", "language", "unsure"]
    reviewed = {**reviewed, "download": settings.section_defaults("download")}  # as the examples show them
    plain = ereader_feed()
    assert {key: list(v) if isinstance(v, tuple) else v for key, v in vars(plain).items() if key in feed} == feed
    assert Device() == plain
    repo = Path(__file__).resolve().parents[1]
    every_key = [s for s in reviewed if s not in ("server", "bell", "inbox")]  # some commented out, or not offered:
    # the topic, say, and the Inbox's strip_suffix
    windows_only = {section: reviewed[section] for section in every_key} | {"tools": {}}  # a copy elsewhere looks on the PATH
    seven = f"# sevenzip = '{windows}'"
    text = (repo / "settings.example.toml").read_text(encoding="utf-8")
    example = tomllib.loads(text)
    assert {section: example[section] for section in every_key} == windows_only
    for section in ("server", "bell", "inbox"):
        assert example[section] == {key: reviewed[section][key] for key in example[section]}, section
    assert list(example["inbox"]) == ["folder", "folders", "into", "lookup"]  # all but strip_suffix
    docs = (repo / "docs" / "rules.md").read_text(encoding="utf-8")
    block = re.search(r"\n## Settings\n.*?```toml\n(.*?)```", docs, re.S)[1]
    shown = tomllib.loads(block)
    assert {section: shown[section] for section in every_key} == windows_only and seven in text and seven in block
    assert ereader_feed().offers() == ("epub", "azw3", "mobi", "azw", "prc", "fb2")  # the books that flow


def test_tables_merge_by_name_and_a_value_replaces():
    mine('[genres."Railway Stories"]\nwords = ["railway", "branch line"]\n'
         '[genres."Travel"]\nwords = ["voyage"]\n'
         '[kinds]\nprose_max_mb = 3\n')
    r = rules.current()
    assert r.genres_of("Notes from a Branch-Line Station") == ["Railway Stories"]  # a genre of your own, last
    assert r.genres_of("A Voyage Out") == ["Travel"] and r.genres_of("Travel Notes") == []  # your words replace
    assert "Horror" in dict(r.genres) and r.prose_max_mb == 3  # the others stay as shipped
    assert [g for g, _ in r.genres][-1] == "Railway Stories"


def test_add_and_drop_change_a_list():
    mine('[genres."Travel"]\nwords_add = ["guidebook*"]\nwords_drop = ["TRAVEL*"]\n'
         '[filename]\nstrip_add = ["[scan]"]\nstrip_patterns_add = [\'~[a-z]+~$\']\n')
    r = rules.current()
    assert r.genres_of("The Guidebook of Hollow Bay") == ["Travel"] and r.genres_of("Travel Notes") == []
    assert meta.from_filename("The Moon Garden [scan] - Ann Writer~xyz~") == ("The Moon Garden", ["Ann Writer"])
    assert meta.from_filename("Moon Garden (ebook)") == ("Moon Garden", [])  # the shipped token is still there


def test_off_takes_an_entry_away_and_inherit_false_starts_empty():
    mine('[genres."Romance"]\noff = true\n[games]\ninherit = false\n[games."Pocket Picnic"]\nwords = ["pocket picnic"]\n')
    r = rules.current()
    assert "Romance" not in dict(r.genres) and r.genres_of("A Love Story") == []
    assert [g for g, _, _ in r.games] == ["Pocket Picnic"] and r.game_names("Pathfinder Pocket Picnic") == ["Pocket Picnic"]


def test_files_are_read_in_name_order_and_the_last_word_wins():
    mine('[kinds]\nprose_max_mb = 5\n', "b.toml")
    mine('[kinds]\nprose_max_mb = 4\n', "a.toml")
    assert rules.current().prose_max_mb == 5
    assert [p.name for p in rules.current().files][-2:] == ["a.toml", "b.toml"]


def test_the_rules_folder_from_the_settings_the_environment_or_the_command(tmp_path, monkeypatch, capsys, watching):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "library.toml").write_text('[genres."Lighthouses"]\nwords = ["lighthouse*"]\n', encoding="utf-8")
    paths.settings_file().write_text(f"rules = '{shared}'\n", encoding="utf-8")
    assert rules.folder() == shared and "Lighthouses" in dict(rules.current().genres)
    paths.settings_file().write_text("", encoding="utf-8")
    assert "Lighthouses" not in dict(rules.current().genres)
    monkeypatch.setenv("HYLLY_RULES", str(shared))
    assert "Lighthouses" in dict(rules.current().genres)
    monkeypatch.delenv("HYLLY_RULES")
    one = tmp_path / "one.toml"
    one.write_text('[genres."Lanterns"]\nwords = ["lantern*"]\n', encoding="utf-8")
    cli.main(["rules", "try", "The Lantern Keeper.epub", "--rules", str(one), "--rules", str(shared)])
    out = capsys.readouterr().out
    assert "Genres:        Lanterns" in out and str(one) in "".join(str(p) for p in rules.current().files)
    assert [p.name for p in rules.current().files][-2:] == ["one.toml", "library.toml"]


def test_rules_may_name_one_file(tmp_path):
    one = tmp_path / "library.toml"
    one.write_text('[genres."Lighthouses"]\nwords = ["lighthouse*"]\n', encoding="utf-8")
    paths.settings_file().write_text(f"rules = '{one}'\n", encoding="utf-8")
    assert "Lighthouses" in dict(rules.current().genres) and rules.current().files[-1] == one
    with pytest.raises(SystemExit, match="Your rules are one file"):
        cli.main(["rules", "copy", "genres"])
    paths.settings_file().write_text(f"rules = '{tmp_path / 'nowhere'}'\n", encoding="utf-8")
    rules.reset()
    with pytest.raises(SystemExit, match=r"rules = .*nowhere \(in .*settings\.toml, or HYLLY_RULES\): there is no such"):
        rules.current()


def test_which_rules_are_read_when_several_places_say(tmp_path, monkeypatch, capsys, watching):
    """rules = is read instead of the rules folder next to settings.toml, HYLLY_RULES wins over rules =, and --rules
    is read last, so what it says wins over the folder."""
    mine('[genres."Lighthouses"]\nwords = ["lighthouse*"]\n[kinds]\nprose_max_mb = 4\n')  # next to settings.toml
    setting, env = tmp_path / "setting", tmp_path / "env"
    for folder, genre in ((setting, "Harbors"), (env, "Lanterns")):
        folder.mkdir()
        (folder / "shared.toml").write_text(f'[genres."{genre}"]\nwords = ["{genre.lower()}"]\n', encoding="utf-8")
    assert [g for g, _ in rules.current().genres][-1] == "Lighthouses" and rules.current().prose_max_mb == 4
    paths.settings_file().write_text(f"rules = '{setting}'\n", encoding="utf-8")
    genres = [g for g, _ in rules.current().genres]
    assert genres[-1] == "Harbors" and "Lighthouses" not in genres  # instead of the folder, not as well
    monkeypatch.setenv("HYLLY_RULES", str(env))
    genres = [g for g, _ in rules.current().genres]
    assert genres[-1] == "Lanterns" and "Harbors" not in genres
    cli.main(["rules"])
    assert f"Your rules folder (HYLLY_RULES): {env}\n" in capsys.readouterr().out
    monkeypatch.delenv("HYLLY_RULES")
    paths.settings_file().write_text("", encoding="utf-8")
    extra = tmp_path / "extra.toml"
    extra.write_text("[kinds]\nprose_max_mb = 5\n", encoding="utf-8")
    cli.main(["rules", "--rules", str(extra)])
    capsys.readouterr()
    r = rules.current()
    assert r.prose_max_mb == 5 and [p.name for p in r.files][-2:] == ["mine.toml", "extra.toml"]  # --rules last
    monkeypatch.setenv("HYLLY_RULES", str(tmp_path / "away"))
    rules.reset()
    with pytest.raises(SystemExit, match=r"rules = .*away \(in .*, or HYLLY_RULES\): there is no such file or folder"):
        rules.current()


def test_a_mistake_names_the_file_the_table_and_the_entry(tmp_path):
    path = mine('[genres."Railway Stories"]\npatterns = [\'steam(\']\n')
    with pytest.raises(SystemExit, match=r'mine\.toml, \[genres\."Railway Stories"\] patterns: \'steam\(\' isn\'t a pattern'):
        rules.current()
    for text, said in (('[genres."Railway Stories"]\nwordz = ["rail"]\n', r'\[genres\."Railway Stories"\]: wordz isn\'t one of its keys'),
                       ('[genre."Railway Stories"]\nwords = ["rail"]\n', r"\[genre\] isn't a table of rules"),
                       ('[formats.EPUB]\nbook = true\n', r"\[formats\.EPUB\]: a format is its extension"),
                       ('[formats.epub]\nlayout = "wavy"\n', r"\[formats\.epub\] layout = 'wavy': use reflowable, fixed, comic"),
                       ('[kinds.Poetry]\nlabel = "Poem"\n', r"\[kinds\.Poetry\]: a kind is one word in small letters"),
                       ('[kinds.zine]\ndevice = "phone"\n', r"\[kinds\.zine\] device = 'phone': use ereader, tablet, either"),
                       ('[kinds.zine]\nlayout = "wavy"\n', r"\[kinds\.zine\] layout = 'wavy': use reflowable, fixed, comic"),
                       ('[kinds.language]\nlabel = "Language"\n', r"\[kinds\.language\]: language is one of \[download\]'s own "
                                                                     r"settings in settings\.toml \(ereader, tablet, size_tie, language, unsure\)"),
                       ('[kind_genres.zine]\ngenre = "Zines"\n', r"\[kind_genres\.zine\]: zine isn't a kind\. They are book, tech"),
                       ('[kinds.zine]\nfolders = ["Comics"]\n', r"\[kinds\.zine\] folders: 'comics' is \[kinds\.comic\]'s already"),
                       ('[filename]\nstrip = "(scan)"\n', r"\[filename\] strip: a list"),
                       ('[filename]\nstrip = ["unclosed\n', r"not readable as TOML"),
                       ('[genres."Railway Stories"]\nwords = [" * "]\n', r"words: ' \* ' has no word in it"),
                       ('[series]\nfold_words_add = ["´¨"]\n', r"fold_words_add: '´¨' has no word in it"),
                       ("[genres.Trains]\npatterns = ['(?P<w>steam) train', 'rail(?P<w>way)']\n",
                        r"\[genres\.Trains\] patterns: each pattern can be read alone, but not all of them joined"),
                       ("[filename]\nnumber_pattern = 'x)|(y'\n", r"number_pattern: 'x\)\|\(y' isn't a pattern"),
                       ("[genres]\noff = true\n", r"\[genres\] off = true: use inherit = false to start \[genres\] empty"),
                       ("[kinds]\nprose_max_mb = inf\n", r"\[kinds\] prose_max_mb: a number"),
                       ('[itchio]\nfetch_add = ["*"]\n', r"\[itchio\] fetch_add: '\*' isn't a kind of file")):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(SystemExit, match=said):
            rules.current()
        assert str(path) in str(pytest.raises(SystemExit, rules.current).value)
    for data in ('[genres."Café Stories"]\nwords = ["café"]\n'.encode("cp1252"),  # saved as ANSI
                 '[genres."Café Stories"]\nwords = ["café"]\n'.encode("utf-16")):  # as Windows PowerShell's > does
        path.write_bytes(data)
        with pytest.raises(SystemExit, match=r"mine\.toml isn't saved as UTF-8 \(line 1\): save it as UTF-8"):
            rules.current()
    path.write_text("[filename]\nstrip = " + "[" * 5000 + "]" * 5000 + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match=r"mine\.toml: not readable as TOML"):
        rules.current()
    path.write_text("[genres.Trains]\npatterns = ['(?P<w>steam) train']\n", encoding="utf-8")
    mine("[genres.Trains]\npatterns_add = ['rail(?P<w>way)']\n", "more.toml")  # each file is fine alone
    with pytest.raises(SystemExit, match=r"mine\.toml, more\.toml, \[genres\.Trains\]: each pattern can be read alone"):
        rules.current()
    with pytest.raises(SystemExit, match=r"--rules .*nowhere\.toml: there is no such file or folder"):
        rules.EXTRA.append(str(tmp_path / "nowhere.toml"))
        rules.current()


def test_rules_put_where_nothing_reads_them_are_named(capsys):
    """Slips that left the rules unread without a word, so that a catalog was built without them: a rules file Notepad
    saved as mine.toml.txt, one in a folder inside the rules folder, one beside settings.toml, a table of rules written
    into settings.toml, and rules = added at the end of settings.toml, under its last section. hylly rules names each
    (what a system leaves in a folder aside); the settings say the two of settings.toml as they are read, and hylly
    settings check lists them, with where each goes."""
    from hylly import settings

    folder = paths.settings_file().parent / "rules"
    (folder / "old").mkdir(parents=True)
    (folder / "old" / "games.toml").write_text('[games."Pocket Picnic"]\nwords = ["pocket picnic"]\n', encoding="utf-8")
    (folder / "mine.toml.txt").write_text('[genres."Lanterns"]\nwords = ["lantern*"]\n', encoding="utf-8")
    (folder / ".DS_Store").write_bytes(b"")
    (folder / "Thumbs.db").write_bytes(b"")
    (paths.settings_file().parent / "kinds.toml").write_text('[kinds.zine]\nbundle_words = ["zine"]\n', encoding="utf-8")
    for name in ("settings.example.toml", "settings-old.toml", "notes.toml"):  # none of them rules gone astray
        (paths.settings_file().parent / name).write_text('[reader]\ncache_gb = 3\n', encoding="utf-8")
    (paths.settings_file().parent / "settings.example.toml").write_text('[genres.X]\nwords = ["x"]\n', encoding="utf-8")
    paths.settings_file().write_text('[reader]\ncache_gb = 2\n\n[genres."Railway Stories"]\nwords = ["railway"]\n\n'
                                     '[folders.shelf]\nnamed = true\nrules = \'D:\\NAS\\Rules\'\nlibrary = \'D:\\NAS\\Books\'\n',
                                     encoding="utf-8")
    settings.reload()
    rules.reset()
    r = rules.current()
    assert r.genres_of("The Railway Lantern") == [] and "zine" not in r.kinds and [p.parent for p in r.files] == [rules.DATA] * 6
    cli.main(["rules"])
    out = capsys.readouterr().out
    assert "  Not read: mine.toml.txt: its name ends in .txt, not .toml, so it isn't read\n" in out
    assert "  Not read: old: a folder, which isn't looked in\n" in out
    assert "  Not read: kinds.toml, beside settings.toml: rules files go in the rules folder\n" in out
    table = (f"[genres] is a table of rules, not of settings, so it is left out here: it goes in a .toml file in your "
             f"rules folder, {folder} (hylly rules copy genres starts one there).")
    under = ("[folders.shelf] rules is one of the settings at the top, above the first [section], so it is left out "
             "here: move it up there.")
    assert f"  Not read: settings.toml: {table}\n" in out and f"  Not read: settings.toml: {under}\n" in out
    assert ".DS_Store" not in out and "Thumbs.db" not in out and "library" not in out  # hylly rules: the rules alone
    assert "settings.example.toml" not in out and "settings-old.toml" not in out and "notes.toml" not in out
    assert settings.get("reader")["cache_gb"] == 2
    err = capsys.readouterr().err
    assert f"{paths.settings_file()}: {table}\n" in err and f"{paths.settings_file()}: {under}\n" in err
    assert f"{paths.settings_file()}: {under.replace('] rules', '] library')}\n" in err
    mistakes, left = settings.problems()
    assert not mistakes and f"{paths.settings_file()}: {table}" in left and f"{paths.settings_file()}: {under}" in left
    paths.settings_file().write_text('[reader]\ncache_gb = 2\n[folders]\nrules = "x"\n', encoding="utf-8")  # under [folders]
    settings.reload()
    settings.get("reader")
    assert "[folders] rules is one of the settings at the top, above the first [section]" in capsys.readouterr().err
    paths.settings_file().write_text("", encoding="utf-8")
    settings.reload()


def test_a_rules_file_beside_settings_toml_that_is_read_is_never_said_to_be_unread(tmp_path, capsys, monkeypatch):
    """One read by --rules from the settings folder, or by rules = through a link to it, was named "Not read" right
    above what it changed: the paths were compared as written."""
    extra = paths.settings_file().parent / "extra.toml"
    extra.write_text('[genres."Railway Stories"]\nwords = ["railway"]\n', encoding="utf-8")
    monkeypatch.chdir(extra.parent)
    cli.main(["rules", "--rules", "extra.toml"])  # by its name, in the settings folder
    out = capsys.readouterr().out
    assert "extra.toml: [genres] 1 added" in out and "Not read" not in out
    rules.EXTRA.clear()
    link = tmp_path / "settings-link"
    try:
        link.symlink_to(paths.settings_file().parent, target_is_directory=True)
    except OSError:  # Windows without the right to make links: the same comparison, by the --rules run above
        return
    paths.settings_file().write_text(f"rules = '{link / 'extra.toml'}'\n", encoding="utf-8")
    rules.reset()
    cli.main(["rules"])
    out = capsys.readouterr().out
    assert "extra.toml: [genres] 1 added" in out and "Not read" not in out
    paths.settings_file().write_text("", encoding="utf-8")


def test_no_table_of_rules_has_the_name_of_a_settings_section():
    """The settings tell a table of rules written into settings.toml by its name (settings.astray), whatever the
    table: a section of the settings named as a table of rules would be told as one, and left out."""
    from hylly import settings

    assert not set(rules.SPECS) & {*settings.DEFAULTS, "folders", *settings.TOP}
    assert all(settings.astray("", name) == "" for name in (*settings.DEFAULTS, "folders"))


def test_strip_author_separators_and_unless_go_case_and_underscores_as_the_rules_page_says():
    """docs/rules.md says strip and the [names] lists hold items as written, case not counting. strip_add =
    ["_retail"] never matched, as a file's name has its _ as spaces by then: it is looked for as written now, before
    they are, so it never takes the word out of "The Retail Giant"; " AND " didn't split a PDF's authors as " and "
    does; and [kind_genres.comic] unless = ["manga"] didn't find the genre Manga, so a manga had both."""
    assert meta.split_authors("Ann Writer AND Bo Painter; Cy Doe And Di Roe & Ed Poe") == [
        "Ann Writer", "Bo Painter", "Cy Doe", "Di Roe", "Ed Poe"]
    assert meta.split_authors("Sandra Anderson") == ["Sandra Anderson"]  # a separator is as written: " and ", spaces too
    mine('[filename]\nstrip_add = ["_retail", "(Shelf_Copy)", "_", "(scan)"]\n[kind_genres.comic]\nunless = ["manga"]\n')
    assert meta.from_filename("Lights_retail") == ("Lights", [])
    assert meta.from_filename("The Moon Garden_RETAIL - Ann Writer") == ("The Moon Garden", ["Ann Writer"])
    assert meta.from_filename("The Retail Giant") == ("The Retail Giant", [])  # the word itself stays
    assert meta.from_filename("Modern Retailers_retail") == ("Modern Retailers", [])
    assert meta.from_filename("The Moon Garden (shelf_copy)") == ("The Moon Garden", [])
    assert meta.from_filename("The Moon Garden (shelf copy)") == ("The Moon Garden (shelf copy)", [])  # as written
    assert meta.from_filename("The Moon Garden_(scan)") == ("The Moon Garden", [])  # with no _, as before
    assert meta.from_filename("Harbor_Lights_Book_2") == ("Harbor Lights Book 2", [])  # "_" alone takes nothing out
    assert catalog.kind_genre("comic", ["Manga"]) == ["Manga"]
    assert catalog.kind_genre("comic", ["Fantasy"]) == ["Comics & Graphic Novels", "Fantasy"]


def test_a_file_may_start_with_a_bom_and_a_pattern_with_its_own_i():
    path = mine("")
    path.write_bytes(b"\xef\xbb\xbf" + "[genres.Lanterns]\npatterns = ['(?i)lantern', '(?i)lamp']\n"
                     "[filename]\nnumber_pattern = '(?i)^vol\\.\\d+ '\n".encode("utf-8"))
    r = rules.current()
    assert r.genres_of("THE LAMP KEEPER") == ["Lanterns"] and r.strip_number("VOL.2 Harbor Lights") == "Harbor Lights"


def test_a_running_server_keeps_the_rules_from_before_a_mistake(tmp_path, capsys, watching):
    path = mine('[genres."Lanterns"]\nwords = ["lantern*"]\n')
    assert "Lanterns" in dict(rules.current().genres)
    path.write_text('[genres."Lanterns"]\nwords = "lantern"\n', encoding="utf-8")
    assert "Lanterns" in dict(rules.current().genres)  # the mistake is told, the rules stay
    assert "mine.toml" in capsys.readouterr().err
    assert "Lanterns" in dict(rules.current().genres) and capsys.readouterr().err == ""  # once
    path.write_text('[genres."Lanterns"]\nwords = ["lamp*"]\n', encoding="utf-8")  # put right: read again
    assert rules.current().genres_of("The Lamp Keeper") == ["Lanterns"]
    path.write_text('[genres."Lanterns"]\nwords = "lantern"\n', encoding="utf-8")  # the same mistake again: told again
    assert rules.current().genres_of("The Lamp Keeper") == ["Lanterns"] and "mine.toml" in capsys.readouterr().err
    path.write_bytes('[genres."Lanterns"]\nwords = ["café*"]\n'.encode("cp1252"))
    assert rules.current().genres_of("The Lamp Keeper") == ["Lanterns"]
    assert "mine.toml isn't saved as UTF-8" in capsys.readouterr().err
    path.write_text('[genres."Lanterns"]\nwords = ["lamp*"]\n', encoding="utf-8")
    mine("[genres.Lanterns]\npatterns = ['(?P<w>beacon)']\n", "more.toml")
    mine("[genres.Lanterns]\npatterns_add = ['(?P<w>signal)']\n", "most.toml")  # the two together don't go
    assert rules.current().genres_of("The Lamp Keeper") == ["Lanterns"]
    assert "more.toml, most.toml, [genres.Lanterns]: each pattern" in capsys.readouterr().err
    (path.parent / "most.toml").unlink()
    extra = tmp_path / "shared"
    extra.mkdir()
    (extra / "harbors.toml").write_text('[genres."Harbors"]\nwords = ["harbor*"]\n', encoding="utf-8")
    rules.EXTRA.append(str(extra))
    assert "Harbors" in dict(rules.current().genres)
    away = tmp_path / "away"
    for _ in range(2):  # a --rules folder on a share that is away a while, twice: told each time
        extra.rename(away)
        assert "Harbors" in dict(rules.current().genres) and "--rules" in capsys.readouterr().err
        away.rename(extra)  # back as it was: nothing to read again, and nothing to tell
        assert "Harbors" in dict(rules.current().genres) and capsys.readouterr().err == ""
    bad = '[genres."Moths"]\nwords = "moth"\n'
    for _ in range(2):  # a new file with a mistake, taken away, and made again: told again
        mine(bad, "zz.toml")
        assert "Harbors" in dict(rules.current().genres) and "zz.toml" in capsys.readouterr().err
        (path.parent / "zz.toml").unlink()
        assert "Moths" not in dict(rules.current().genres) and capsys.readouterr().err == ""


def test_a_command_reads_the_rules_and_the_settings_once(monkeypatch):
    from hylly import reader, settings

    monkeypatch.setattr(rules, "WATCH", False)
    monkeypatch.setattr(settings, "WATCH", False)
    path = mine('[genres."Lanterns"]\nwords = ["lantern*"]\n')
    assert rules.current().genres_of("The Lantern Keeper") == ["Lanterns"] and reader.cache_limit() == 10 * words.GB
    path.write_text('[genres."Lanterns"]\nwords = ["lamp*"]\n', encoding="utf-8")  # saved while a catalog is built
    paths.settings_file().write_text("[reader]\ncache_gb = 12\n", encoding="utf-8")
    assert rules.current().genres_of("The Lantern Keeper") == ["Lanterns"]  # one rule set for the whole command
    assert reader.cache_limit() == 10 * words.GB


def test_in_one_table_a_value_comes_before_its_drop_and_its_add():
    mine(r"""[genres."Travel"]
words_add = ["guidebook*"]
words = ["voyage*"]
words_drop = ["VOYAGE*"]
[filename]
strip_patterns_drop = ['\d+x']
strip_patterns = ['\D+x', '\d+x']
""")
    r = rules.current()
    assert r.data["genres"]["Travel"]["words"] == ["guidebook*"]  # words cast case aside, whatever the order
    assert r.data["filename"]["strip_patterns"] == [r"\D+x"]  # a pattern's case counts: \D is not \d


def test_what_isnt_there_to_take_away_is_noted(capsys):
    mine('[genres."Romanse"]\noff = true\n[genres."Travel"]\nwords_drop = ["guidebok*"]\n'
         '[genres."Classics"]\ninherit = false\nwords_add = ["old books"]\n')
    r = rules.current()
    assert "Romance" in dict(r.genres) and r.genres_of("Travel Notes") == ["Travel"]  # nothing went, and nothing stops
    assert r.data["genres"]["Classics"] == {"words": ["old books"]} and r.genres_of("Old Books of Harbor") == ["Classics"]
    cli.main(["rules"])
    out = capsys.readouterr().out
    assert "    nothing to take away: [genres.Romanse] off = true\n" in out
    assert '    nothing to take away: [genres.Travel] words_drop = ["guidebok*"]\n' in out
    cli.main(["rules", "show", "genres"])
    assert "# nothing to take away: [genres.Romanse] off = true (in mine.toml)" in capsys.readouterr().out


def test_a_genre_or_game_named_in_another_case_changes_the_shipped_one_which_keeps_its_spelling(capsys):
    """Tables merged by the name exactly as written: [genres."travel"] words_add made a second genre "travel" beside
    the shipped Travel, and a title got both; [games."call of cthulhu"] did the same for a game; and hylly rules said
    only "added". The name goes with case not counting now, and the shipped spelling stays; an _add or a _drop on an
    entry that isn't there makes a new one, as before, and hylly rules says so."""
    mine('[genres."travel"]\nwords_add = ["guidebook"]\n[games."call of cthulhu"]\nwords_add = ["lantern cult"]\n'
         '[genres."Travle"]\nwords_add = ["voyage"]\nwords_drop = ["cruise"]\n[genres."ROMANCE"]\noff = true\n')
    r = rules.current()
    genres, games = [name for name, _ in r.genres], [name for name, _, _ in r.games]
    assert genres.count("Travel") == 1 and "travel" not in genres and "Romance" not in genres and genres[-1] == "Travle"
    assert genres.index("Travel") == list(rules.shipped().data["genres"]).index("Travel") - 1  # in its place, as shipped
    assert r.genres_of("A Guidebook to Harbor Lights") == ["Travel"] and r.genres_of("Travel Notes") == ["Travel"]
    assert games.count("Call of Cthulhu") == 1 and "call of cthulhu" not in games
    assert r.game_names("The Lantern Cult") == ["Call of Cthulhu"] and r.game_names("Call of Cthulhu") == ["Call of Cthulhu"]
    assert r.mark("genres", "Travel") == "changed in mine.toml" and r.mark("games", "Call of Cthulhu") == "changed in mine.toml"
    cli.main(["rules"])
    out = capsys.readouterr().out
    assert "mine.toml: [genres] 1 added, 1 changed, 1 taken away; [games] 1 changed\n" in out
    assert "    a new entry, as there was none of that name: [genres.Travle] words_add, words_drop\n" in out
    assert '    nothing to take away: [genres.Travle] words_drop = ["cruise"]\n' in out  # as before
    cli.main(["rules", "show", "genres"])
    out = capsys.readouterr().out
    assert "[genres.Travel]  # changed in mine.toml\n" in out and "[genres.travel]" not in out
    assert "# a new entry, as there was none of that name: [genres.Travle] words_add, words_drop (in mine.toml)" in out
    mine('[genres."Railway Stories"]\nwords = ["railway"]\n[genres."railway stories"]\nwords_add = ["branch line"]\n',
         "more.toml")  # one of yours, named again in another case in a later file: the first's spelling
    r = rules.current()
    assert [name for name, _ in r.genres].count("Railway Stories") == 1 and "railway stories" not in dict(r.genres)
    assert r.genres_of("The Branch Line") == ["Railway Stories"]


def test_two_files_of_one_name_are_told_apart(tmp_path, capsys):
    folder_file = mine('[genres."Lanterns"]\nwords = ["lantern*"]\n', "genres.toml")
    other = tmp_path / "other" / "genres.toml"
    other.parent.mkdir()
    other.write_text('[games."Pocket Picnic"]\nwords = ["pocket picnic"]\n', encoding="utf-8")
    cli.main(["rules", "--rules", str(other)])
    out = capsys.readouterr().out
    assert f"  {folder_file}: [genres] 1 added\n" in out and f"  {other}: [games] 1 added\n" in out
    r = rules.current()
    assert r.mark("genres", "Lanterns") == f"from {folder_file}" and r.mark("games", "Pocket Picnic") == f"from {other}"


def test_a_copied_file_changes_nothing_until_a_line_is_taken_back_in(capsys, watching):
    mine('[genres."Romance"]\noff = true\n[genres."Travel"]\nwords_add = ["guidebook*"]\n', "a-mine.toml")
    before = rules.current()
    cli.main(["rules", "copy", "genres"])  # read after a-mine.toml
    assert "changes nothing until you take the # off a line" in capsys.readouterr().out
    after = rules.current()
    assert after.data == before.data and "genres.toml" in [p.name for p in after.files]
    cli.main(["rules"])
    assert "genres.toml: changes nothing" in capsys.readouterr().out
    copied = paths.settings_file().parent / "rules" / "genres.toml"
    text = copied.read_text(encoding="utf-8")
    copied.write_text(text.replace('# words = ["travel*"]', 'words = ["travel*", "voyage*"]'), encoding="utf-8")
    r = rules.current()
    assert r.genres_of("A Voyage Out") == ["Travel"] and "Romance" not in dict(r.genres)  # that line, and only that


def leaves(table: dict, at: tuple = ()) -> dict:
    """A TOML file's values by where they are: (table, entry, key)."""
    found = {}
    for key, value in table.items():
        found.update(leaves(value, (*at, key)) if isinstance(value, dict) else {(*at, key): value})
    return found


def test_a_copied_file_has_each_list_on_one_line_so_any_line_taken_in_is_whole_toml():
    """hylly rules copy commented a shipped file out line by line, and sixteen shipped lists run over several lines:
    the # taken off the first line of one alone made the next table's name an item of the list, and the mistake was
    said of a line never touched, with a hint about patterns. Each rule is one line in the copy now: any one taken in
    reads as TOML and is that rule as shipped, and all of them taken in are the shipped file."""
    joined = 0
    for name in rules.SHIPPED:
        text = (rules.DATA / f"{name}.toml").read_text(encoding="utf-8")
        shipped, copy = leaves(tomllib.loads(text)), rules.commented(text).splitlines()
        notes = {line for line in text.splitlines() if line.lstrip().startswith("#")}
        taken = [i for i, line in enumerate(copy) if line.startswith("# ") and line not in notes]
        assert not leaves(tomllib.loads("\n".join(copy)))  # nothing in effect until a line is taken in
        assert leaves(tomllib.loads("\n".join(x[2:] if i in taken else x for i, x in enumerate(copy)))) == shipped
        for k in taken:
            one = leaves(tomllib.loads("\n".join(x[2:] if i == k else x for i, x in enumerate(copy))))
            assert len(one) == 1 and all(shipped[at] == value for at, value in one.items()), (name, copy[k])
        joined += len(text.splitlines()) - len(copy)
    assert joined >= 16  # the lines the long lists took
    said = 'a = ["one",  # why one\n     "two"]  # why two\nb = 1  # one\n'
    assert rules.commented(said) == '# why two\n# a = ["one", "two"]  # why one\n# b = 1  # one\n'


def test_the_hint_about_patterns_comes_only_with_a_backslash_toml_can_t_read():
    path = mine('[genres."Science Fiction"]\nwords = ["sf",\n# "space opera*"]\n\n[genres.Fantasy]\nwords = ["elf*"]\n')
    with pytest.raises(SystemExit, match=r"mine\.toml: not readable as TOML \(Invalid value \(at line 5, column 2\)\)\.$"):
        rules.current()  # a list's first line alone: nothing about patterns
    for written in ('[filename]\nstrip_patterns = ["\\d+x"]\n', '[filename]\nstrip = ["C:\\NAS\\x"]\n'):
        path.write_text(written, encoding="utf-8")
        with pytest.raises(SystemExit, match=r"not readable as TOML \(.+\)\. Patterns go in single quotes: '\.\.\.'\.$"):
            rules.current()


def test_hylly_rules_try(capsys):
    cli.main(["rules", "try", "Neon Rangers 002 (2009).cbz", "--bundle", "Humble Comics Bundle: Neon Nights"])
    out = capsys.readouterr().out
    assert "Title:         Neon Rangers 002 (2009)" in out and "Series:        Neon Rangers, number 2" in out
    assert "Kind:          comic (CBZ offered)" in out and "Genres:        Comics & Graphic Novels" in out
    assert "Device:        tablet (comic)" in out  # a comic's device, by its entry in the kinds
    cli.main(["rules", "try", "Ann Writer - The Lantern Keeper (Harbor Lights Book 2).epub", "--mb", "2"])
    out = capsys.readouterr().out
    assert "Title:         The Lantern Keeper" in out and "Authors:       Ann Writer" in out
    assert "Series:        Harbor Lights, number 2" in out and "Device:        ereader" in out
    cli.main(["rules", "try", "Iron Stride Field Rules.pdf", "--bundle", "Humble RPG Bundle: Pathfinder Nights"])
    out = capsys.readouterr().out
    assert "Kind:          rpg" in out and "Games:         Pathfinder" in out
    # a shop labels a file by its format, so the name's words only say what they would do in a label
    cli.main(["rules", "try", "Moonwake Companion 01 (Deutsch).cbz", "--bundle", "Comics Bundle"])
    out = capsys.readouterr().out
    assert "Kind:          comic (CBZ offered)" in out and "Series:        Moonwake Companion, number 1" in out
    assert "A shop's file: the book, labelled CBZ\n" in out
    assert ('In a label:    "Companion" would make a shop\'s file an extra ([labels] extra); names a language: '
            "german\n") in out
    cli.main(["rules", "try", "The Lantern Keeper.epub"])
    assert "In a label:    no word that would make a shop's file an extra\n" in capsys.readouterr().out


def test_hylly_rules_try_says_what_a_build_makes_of_the_same_file(tmp_path, capsys):
    """rules try makes its title with the catalog's own record(), so it says what a build makes of a file named
    so in a folder of yours whose files are named by their titles (named = true), with no details of its own: its
    authors tidied ("Writer, Ann" is Ann Writer) and a name that is no person's left out ("Unknown Author"), as the build
    has them, where try's own reading of the name gave them as written."""
    paths.settings_file().write_text("[folders.shelf]\nnamed = true\n[folders.comics]\nnamed = true\n", encoding="utf-8")
    names = {"shelf": ["The Moon Garden - Writer, Ann.epub", "Unknown Author - The Salt Ledger.epub",
                       "Ann Writer - The Lantern Keeper (Harbor Lights Book 2).epub", "Tuulen talo - Sävel Ränninmäki.epub",
                       "Moonwake Mysteries.epub"],
             "comics": ["Neon Rangers 002 (2009).cbz"]}
    for folder, files in names.items():
        (tmp_path / folder).mkdir()
        for name in files:
            (tmp_path / folder / name).write_bytes(b"")  # no details of its own: its title from its name
    (tmp_path / "state").mkdir()
    catalog.build(tmp_path, log=lambda s: None)
    built = {Path(unquote(i["f"][0][0])).name: i for i in page_data(tmp_path)["items"]}
    for name in [n for files in names.values() for n in files]:
        cli.main(["rules", "try", name])
        said = dict(line.split(":", 1) for line in capsys.readouterr().out.splitlines())
        said = {k: v.strip() for k, v in said.items()}
        rec = built[name]
        assert said["Title"] == rec["t"] and said["Authors"] == ("; ".join(rec.get("a", [])) or "(none in the name)"), name
        assert said["Series"] == (f"{rec['s']}, number {rec['n']}" if rec.get("s") else "(none)"), name
        assert said["Kind"].split()[0] == rec["k"] and said["Genres"] == (", ".join(rec.get("g", [])) or "(none)"), name
        assert said["Device"].split()[0] == rec["dev"], name
    assert built["The Moon Garden - Writer, Ann.epub"]["a"] == ["Ann Writer"] and not built["Unknown Author - The Salt Ledger.epub"].get("a")
    assert built["Moonwake Mysteries.epub"]["g"] == ["Mystery & Thriller"]
    cli.main(["rules", "try", "Moonwake Saga.zip", "--mb", "100"])  # a ZIP by its kind: its own size never counts,
    assert "Device:        ereader\n" in capsys.readouterr().out  # so the line doesn't name it
    cli.main(["rules", "try", "Moonwake Saga.epub", "--mb", "100"])
    assert "Device:        tablet (EPUB is 100.0 MB, probably image-heavy), at 100 MB\n" in capsys.readouterr().out


def test_rules_try_only_says_what_the_catalog_s_one_function_makes_of_a_name_as_a_build_does(tmp_path, capsys):
    """What the rules make of a name is one function of the catalog's, catalog.from_name, by the build's own
    record(), and hylly rules try only prints it: a title that is only "Volume 8" is "Harbor Lights Volume 8" with its
    series in front, as a build has it, where try's own steps once left it bare."""
    paths.settings_file().write_text("[folders.shelf]\nnamed = true\n", encoding="utf-8")
    names = ["Volume 8 (Harbor Lights Book 8).epub", "Ann Writer - The Lantern Keeper (Harbor Lights Book 2).epub",
             "Moonwake Saga Vol. 3.epub", "Writer, Ann - Iron Stride Field Rules.pdf"]
    (tmp_path / "shelf").mkdir()
    for name in names:
        (tmp_path / "shelf" / name).write_bytes(b"")  # no details of its own: its title from its name
    (tmp_path / "state").mkdir()
    catalog.build(tmp_path, log=lambda s: None)
    built = {Path(unquote(i["f"][0][0])).name: i for i in page_data(tmp_path)["items"]}
    for name in names:
        found, rec = catalog.from_name(f"D:\\NAS\\{name}"), built[name]  # a path's folders are no part of it
        assert found["name"] == name and (found["title"], found["authors"], found["series"], found["number"]) == (
            rec["t"], rec.get("a", []), rec.get("s", ""), rec.get("n", "")), name
        assert (found["kind"], found["genres"], found["games"], found["device"]) == (
            rec["k"], rec.get("g", []), rec.get("gm", []), rec["dev"]), name
    assert built["Volume 8 (Harbor Lights Book 8).epub"]["t"] == "Harbor Lights Volume 8"
    cli.main(["rules", "try", names[0]])
    out = capsys.readouterr().out
    assert "Title:         Harbor Lights Volume 8\n" in out and "Series:        Harbor Lights, number 8\n" in out
    found = catalog.from_name("Iron Stride Field Rules.pdf", bundle="Humble RPG Bundle: Pathfinder Nights", size=3 << 20)
    assert (found["kind"], found["why_kind"], found["games"]) == ("rpg", 'bundle "RPG"', ["Pathfinder"])
    assert catalog.from_name("Iron Stride Field Rules.pdf", kind="comic")["why_kind"] == ""  # as given: try says --kind


def test_hylly_rules_lists_shows_and_copies(capsys):
    mine('[genres."Lanterns"]\nwords = ["lantern*"]\n[genres."Travel"]\nwords_add = ["voyage"]\n[genres."Romance"]\noff = true\n')
    cli.main(["rules"])
    out = capsys.readouterr().out
    assert "mine.toml: [genres] 1 added, 1 changed, 1 taken away" in out and "hylly settings show" in out
    cli.main(["settings", "show"])  # the settings have a command of their own
    out = capsys.readouterr().out
    assert 'tablet = ["cbz", "cbr", "cb7", "largest", "pdf", "epub"]' in out and "# default" in out and "[ereader_feed]" in out
    cli.main(["rules", "show", "genres"])
    out = capsys.readouterr().out
    assert '[genres.Lanterns]  # from mine.toml' in out and '[genres.Travel]  # changed in mine.toml' in out
    assert '[genres.Horror]  # shipped' in out and "Romance] is taken away (off = true in mine.toml)" in out
    assert "[kind_genres.manual]  # shipped" in out
    mine("[filename]\nnumber_pattern = '^\\d{1,3}\\.\\d{1,2}[A-Z]?\\s+'\n", "numbers.toml")
    cli.main(["rules", "show", "filename"])
    out = capsys.readouterr().out
    assert "number_pattern = '^\\d{1,3}" in out and "[filename]  # changed in numbers.toml" in out  # as the files write them
    cli.main(["rules", "copy", "games"])
    copied = paths.settings_file().parent / "rules" / "games.toml"
    assert copied.read_text(encoding="utf-8") == rules.commented((rules.DATA / "games.toml").read_text(encoding="utf-8"))
    assert '[games."Pathfinder"]\n# words = ["pathfinder*"]\n' in copied.read_text(encoding="utf-8")
    with pytest.raises(SystemExit, match="exists already"):
        cli.main(["rules", "copy", "games"])
    with pytest.raises(SystemExit, match="Which file"):
        cli.main(["rules", "copy", "everything"])


def test_a_name_rule_changes_titles_without_reading_the_files_again(tmp_path, monkeypatch):
    shelf = tmp_path / "shelf"
    shelf.mkdir()
    (shelf / "The Moon Garden [scan] - Ann Writer.djvu").write_bytes(b"AT&T")
    (tmp_path / "state").mkdir()
    catalog.build(tmp_path, log=lambda s: None)
    (rec,) = page_data(tmp_path)["items"]
    assert rec["t"] == "The Moon Garden [scan]" and rec["a"] == ["Ann Writer"]
    mine('[filename]\nstrip_add = ["[scan]"]\n')
    monkeypatch.setattr(meta, "read", lambda *a: pytest.fail("read again"))
    catalog.build(tmp_path, log=lambda s: None)
    data = page_data(tmp_path)
    assert data["items"][0]["t"] == "The Moon Garden"
    assert data["items"][0]["ak"] == "writer ann" and "rules" not in data  # the page gets the sort keys, not the rules


def test_a_file_cached_before_the_cache_said_what_came_from_its_name_takes_the_authors_its_name_gives(tmp_path, monkeypatch):
    """A line of the files' cache made by an early Hylly doesn't say which details came from the file's name (named),
    and isn't read again while its file stays as it was: one without authors takes those its name gives by the rules
    now (catalog.renamed), as a file cached since does. Its title stays as it was cached."""
    shelf = tmp_path / "shelf"
    shelf.mkdir()
    (shelf / "The Moon Garden - Ann Writer.djvu").write_bytes(b"AT&T")
    (tmp_path / "state").mkdir()
    catalog.build(tmp_path, log=lambda s: None)
    cache = tmp_path / "catalog" / "cache" / "files.jsonl"
    (line,) = [json.loads(text) for text in cache.read_text(encoding="utf-8").splitlines()]
    line["meta"] = {k: v for k, v in line["meta"].items() if k not in ("named", "authors")}  # as it was cached then
    cache.write_text(json.dumps(line) + "\n", encoding="utf-8")
    monkeypatch.setattr(meta, "read", lambda *a: pytest.fail("read again"))
    catalog.build(tmp_path, log=lambda s: None)
    (rec,) = page_data(tmp_path)["items"]
    assert (rec["t"], rec["a"]) == ("The Moon Garden", ["Ann Writer"])


def test_a_list_s_number_is_taken_off_titles_only_by_a_rules_file_of_yours_as_docs_rules_md_shows(tmp_path):
    """As the rules come no number before a title is taken off it, so a title that starts with a decimal number
    keeps it, in a file's name as in the title a file gives; the pattern docs/rules.md shows, put in a rules file of
    yours, takes a list's number off both ("53.01 The Silent Orbit"), as it did for everyone before."""
    assert rules.shipped().data["filename"]["number_pattern"] == "" and rules.current().number is None
    titled = lambda title, stem: catalog.titled(catalog.Item(  # noqa: E731 - a file in one of your folders, its own title
        id="f:1", source="shelf", kind="book", title="", files=[(f"shelf/{stem}.epub", "epub", 1)], meta={"title": title}))[0]
    assert meta.from_filename("2.5 Moons over Harbor Lights - Ann Writer") == ("2.5 Moons over Harbor Lights", ["Ann Writer"])
    assert titled("2.5 Moons over Harbor Lights", "moons") == "2.5 Moons over Harbor Lights"
    docs = (Path(__file__).resolve().parents[1] / "docs" / "rules.md").read_text(encoding="utf-8")
    shown = re.search(r"^number_pattern = '[^'\n]+'$", docs, re.M)[0]  # the example to turn on
    mine(f"[filename]\n{shown}\n")
    assert meta.from_filename("53.01 The Silent Orbit - Ann Writer") == ("The Silent Orbit", ["Ann Writer"])
    assert titled("53.01 The Silent Orbit", "orbit") == "The Silent Orbit"
    assert meta.from_filename("2.5 Moons over Harbor Lights") == ("Moons over Harbor Lights", [])  # the pattern's, then


def test_a_plain_plural_is_a_word_of_its_own_and_the_demo_s_mysteries_have_their_genre(tmp_path, demo_made_once):
    """A * finds what starts with a word, so a plural that changes the word's end is a word of its own in the
    shipped genres (mysteries, fantasies, histories, love stories, RPGs), never a shorter stem, which would find
    "fantastic" too. Dragons stay as they were: what starts with "dragons" is Fantasy, a dragon alone (a tattoo, a boat)
    isn't. The demo's three books in ebooks/Mysteries got no genre before, though the help says a folder's name gives
    one."""
    from hylly import demo

    r = rules.shipped()
    for name, genre in (("Mysteries", "Mystery & Thriller"), ("Fantasies", "Fantasy"), ("Histories", "History"),
                         ("Dragons", "Fantasy"), ("The Dragonslayer's Daughter", "Fantasy"), ("Love Stories", "Romance"),
                         ("RPGs", "Tabletop RPG"), ("Thrillers", "Mystery & Thriller"), ("Romances", "Romance")):
        assert r.genres_of(name) == [genre], name
    for name in ("Fantastic Voyages", "Dragonfly Summer", "Mystique", "Historian's Daughter", "The Dragon Boat"):
        assert r.genres_of(name) == [], name
    root = demo.make(tmp_path / "demo")
    catalog.build(root, log=lambda s: None)
    items = page_data(root)["items"]
    mysteries = [i.get("g") for i in items if i.get("col") == "Mysteries"]
    assert mysteries == [["Mystery & Thriller"]] * 3


def test_a_name_has_its_own_particles_and_the_sort_by_author_its_own():
    assert meta.from_filename("Anna van den Berg - The Book") == ("Anna van den Berg - The Book", [])
    mine('[names]\nname_particles_add = ["den"]\n')
    r = rules.current()
    assert meta.from_filename("Anna van den Berg - The Book") == ("The Book", ["Anna van den Berg"])
    assert len(r.particles) == 15 and "du" in r.particles and not r.is_name_word("du") and r.is_name_word("den")


def test_a_mark_in_a_phrase_needs_no_spaces_around_it():
    r = rules.current()
    for name in ("Dungeons & Dragons", "Dungeons &Dragons", "Dungeons&Dragons", "dungeonsanddragons", "D &D", "D& D", "D&D"):
        assert r.game_name(name) == "Dungeons & Dragons", name
    assert r.game_name("Dungeons Dragons") == "Dungeons Dragons"  # the & is still wanted
    assert r.kinds["comic"].words.find("Humble Book Bundle: Bande Dessinees by Moonwake Press") and r.kinds["comic"].words.find("bandes dessinée")
    assert r.labels["extra"].find("PDF_Bonus") == "Bonus"  # _ ends a word, as it does between a phrase's words
    mine('[genres."Salt & Pepper"]\nwords = ["salt & pepper"]\n')
    assert rules.current().genres_of("Cooking with Salt&Pepper") == ["Home, Garden & Outdoors", "Salt & Pepper"]


def test_words_are_read_alike_in_every_table():
    # in [junk] and [series] too: case and accents not counting, any punctuation between a phrase's words, a trailing
    # * for "starts with"; each where Hylly's own pattern has them, such as a title's start for [junk]
    mine('[junk]\ntitles_add = ["sans titre"]\nauthors_add = ["propriete", "scanner*"]\n'
         '[series]\nwords_add = ["numero"]\nfold_words_add = ["edition integrale", "collect*", "box set"]\n'
         'collected_words_add = ["integrale"]\npart_words_add = ["tome"]\ncomic_noise_add = ["edition integrale"]\n')
    r = rules.current()
    assert r.is_junk_title("Sans-titre-1") and r.is_junk_title("SANS TITRE") and not r.is_junk_title("Un sans titre")
    assert r.is_junk_author("Propriété") and r.is_junk_author("Scanners Room 2") and not r.is_junk_author("Ann Propriete")
    assert meta.series_of("The Moon Garden Número 3") == ("The Moon Garden", "3")
    folded = {r.series_key(s) for s in ("Moonwake", "Moonwake Édition intégrale", "Moonwake: Edition-Integrale",
                                         "Moonwake Collected", "Moonwake Box-Set", "Moonwake Box  Set")}
    assert folded == {"moonwake"} and r.series_key("Moonwake Boxes") == "moonwakeboxes"  # whole words
    assert r.collected.search("Moonwake Intégrale") and r.part_label.search("PDF Tome 2")
    assert lookups.comic_name("Moonwake Édition Intégrale").series == "Moonwake"
    assert r.series_key("Café Lantern") == r.series_key("Cafe Lantern") == "cafelantern"  # shipped words too


def test_an_accent_written_as_a_mark_of_its_own_counts_as_one():
    # "e" and a combining acute, as a file name from a Mac may have it, in a word of the rules or in a text
    mine('[genres."Lanterns"]\nwords = ["lanterné*"]\n[junk]\nauthors_add = ["propriété"]\n'
         '[series]\nfold_words_add = ["édition intégrale"]\ncomic_noise_add = ["édition intégrale"]\n')
    r = rules.current()
    for title in ("Lanternés du Nord", "Lanternés du Nord", "Lanternes du Nord"):
        assert r.genres_of(title) == ["Lanterns"], title
    assert r.is_junk_author("Propriété") and r.is_junk_author("Propriété")
    assert meta.series_of("Lantern Wolf Vól. 3") == ("Lantern Wolf", "3")  # a shipped word, the text's mark
    keys = {r.series_key(s) for s in ("Moonwake", "Moonwake Édition Intégrale", "Moonwake Édition Intégrale")}
    assert keys == {"moonwake"} and lookups.comic_name("Moonwake Édition Intégrale").series == "Moonwake"


def _found(what: str, rx, text: str):
    if what in ("notes", "noise"):
        return rx.sub(" ", text)
    hit = rx.match(text) if what in ("series", "bare") else rx.search(text)
    if what in ("series", "bracket"):
        return [hit["series"], hit["num"]] if hit else None
    return bool(hit)


def now(r: rules.Rules, what: str, text: str):
    """What the rules in effect find: a series and its number, whether a title or an author is junk, the title with a
    comic's notes or noise taken out, a series' key."""
    if what in ("key", "junk_title", "junk_author"):
        return {"key": r.series_key, "junk_title": r.is_junk_title, "junk_author": r.is_junk_author}[what](text)
    return _found(what, {"series": r.series, "bracket": r.bracket_series, "bare": r.bare_volume, "part": r.part_label,
                         "collected": r.collected, "notes": r.comic_notes, "noise": r.comic_noise}[what], text)


def _each(*lists, together: list) -> list[tuple]:
    """Every value of together's lists with every other of them (their product), and the other lists' values taken in
    turn beside them, as a counter's digits turn: every word with every separator, the names, cases and numbers coming
    round in their combinations, in far fewer titles than every combination of all."""
    out = []
    for n, picks in enumerate(itertools.product(*together)):
        beside = []
        for values in lists:
            beside.append(values[n % len(values)])
            n //= len(values)
        out.append((*picks, *beside))
    return out


def shipped_and(hand: list[str], *lists: str) -> list[str]:
    """The words written here, and after them every shipped one of these lists ("series.words", say) that isn't among
    them: a word added to the shipped rules is asked about as these are, and fails the test until its answers are in
    tests/shipped_word_answers.json."""
    data = rules.shipped().data
    return list(dict.fromkeys([*hand, *(w for at in lists for w in data[at.split(".")[0]][at.split(".")[1]])]))


def word_titles() -> list[tuple[str, str]]:
    """Made-up titles with every shipped word of [series] (words, bracket_words, part_words, fold_words,
    collected_words, comic_notes, comic_noise) and [junk] (titles, title_extensions, authors), as (what is asked of it,
    the title): with no accents or _, and a phrase's words one space apart; some near ones besides (vols, booklet,
    thence); [junk]'s patterns (title_patterns, author_patterns) by titles written for them here (document12,
    Lantern.pm6); and the cases where accents, punctuation between a phrase's words or a _ next to a word once hid a
    word, which the shipped words now find (FOUND_MORE)."""
    names = ["Lantern Wolf", "The Moon Garden", "Harbor Lights", "Glass Harbor: The Long Tide", "Tour du Monde"]
    seps, cases = [" ", ", ", ": ", " - ", "-", ".", ""], [str, str.upper, str.title]
    out = []
    words = shipped_and(["vol", "volume", "book", "tome", "issue", "no", "part", "chapter", "episode", "vols", "novel"],
                        "series.words", "series.part_words", "series.bracket_words")
    for w, sep, name, case, num, dot in _each(names, cases, ["3", "4.5"], ["", "."], together=[words, seps]):
        out += [("series", f"{name}{sep}{case(w)}{dot} {num}"), ("series", f"{name}{sep}{case(w)}{dot}{num}"),
                ("part", f"PDF{sep}{case(w)}{dot}{num}"), ("bare", f"{case(w)}{dot} {num}"), ("series", f"{name} #{num}"),
                ("bare", f"#{num}")]
    for w, case, name, num in _each(names, ["3", "4.5"], together=[words, cases]):
        out += [("bracket", f"The Lantern Keeper ({name} {case(w)} 2)"), ("bracket", f"Ann Writer - X ({name}  {case(w)} {num}) "),
                ("bracket", f"({name} {case(w)}{num})")]
    junk = shipped_and(["untitled", "microsoft word", "document", "document12", "title", "unknown", "test", "cover",
                        "untitledness"], "junk.titles")
    for w, tail, case in _each(cases, together=[junk, ["", " 1", "s", ".doc", "-x", ": x", " - The Lantern Keeper"]]):
        out += [("junk_title", case(w) + tail), ("junk_title", "The Lantern Keeper " + case(w) + tail)]
    out += [("junk_title", t) for t in ["ch01.indd", "The Moon Garden.pdf", "http://example.org", "www.example.org",
                                         "c:\\books", "00 1234_FM_Intro", "Lantern.pm6", ".hidden", "Cover:", "Cover."]]
    for ext, case in _each(cases, together=[shipped_and([], "junk.title_extensions")]):
        out += [("junk_title", f"Glass Harbor.{case(ext)}"), ("junk_title", f"Glass Harbor {case(ext)}")]  # a file's name
    authors = shipped_and(["administrator", "admin", "user", "owner", "unknown", "author", "calibre", "adobe", "default",
                           "pc", "test", "users"], "junk.authors")
    for w, tail, case in _each(cases, together=[authors, ["", " 1", "s", ".", " Writer", "-x"]]):
        out += [("junk_author", case(w) + tail), ("junk_author", "Ann " + case(w) + tail)]
    collected = shipped_and(["vol", "vols", "volume", "volumes", "book", "tpb", "hc", "hardcover", "omnibus", "compendium",
                             "graphic novel", "deluxe", "library edition", "collection", "archive", "archives", "treasury",
                             "the complete", "complete", "booklet", "hca"], "series.collected_words", "series.comic_noise")
    for w, sep, name, case in _each(names[:3], cases, together=[collected, [" ", "-", ": ", "(", ""]]):
        numbered = str.title if case is str.upper and sep == "-" else case  # capitals, a dash and a number read as
        # an id to the check of what is published, which reads the file of answers whole
        out += [("collected", f"{name}{sep}{case(w)}"), ("collected", f"{name} {numbered(w)}{sep}2"),
                ("noise", f"{name}{sep}{case(w)}"), ("noise", f"{case(w)}{sep}{name}")]
    notes = shipped_and(["digital", "digital edition", "graphic novel", "of 6", "2 of 6", "digitale", "digital editions"],
                        "series.comic_notes")
    for note, case, name in _each(names[:3], together=[notes, cases]):
        out += [("notes", f"{name} 3 ({case(note)})"), ("notes", f"{name} 3 ( {case(note)} )")]
    folds = shipped_and(["the complete", "the", "complete", "collection", "collections", "thence"], "series.fold_words")
    for w, sep, name, case in _each(names, cases, together=[folds, seps]):
        out += [("key", f"{name}{sep}{case(w)}"), ("key", f"{case(w)}{sep}{name}"),
                ("key", f"{name}{sep}{case(w)}{sep}Trail Logs")]
    return list(dict.fromkeys(out + FOUND_MORE))


# Where the rules before the rules files counted accents, punctuation between a phrase's words, or a _ next to a word,
# the shipped words find more: their answers are among the others, in tests/shipped_word_answers.json.
FOUND_MORE = [("junk_title", "Ünknown"), ("junk_title", "Microsoft-Word - The Lantern Keeper"), ("junk_title", "Microsoft  Word"),
              ("junk_title", "Unknown_"), ("junk_author", "Ádministrator"), ("junk_author", "admin_2"),
              ("series", "Lantern Wolf Vól. 3"), ("series", "Lantern Wolf_Vol 3"), ("bracket", "The Lantern Keeper (Harbor Lights Bóok 2)"),
              ("bare", "Vólume 8"), ("part", "PDF_Part2"), ("part", "PDF Pàrt 2"), ("collected", "Moonwake Graphic-Novel"),
              ("collected", "Moonwake_TPB"), ("collected", "Moonwake Ómnibus"), ("notes", "Moonwake 3 (Digital-Edition)"),
              ("notes", "Moonwake 3 (Dígital)"), ("noise", "The-Complete Moonwake"), ("noise", "Moonwake_TPB"),
              ("key", "Café Lantern"), ("key", "The_Lantern Keeper"), ("key", "Lumière Collectión")]
WORD_ANSWERS = Path(__file__).with_name("shipped_word_answers.json")


def test_the_shipped_series_and_junk_words_answer_as_reviewed(request):
    """What the shipped [series] and [junk] words find in made-up titles (word_titles: every word with every separator,
    and the cases they once missed), against tests/shipped_word_answers.json, today's answers as reviewed: a change to
    how a shipped word matches shows here, title by title, where tests/shipped_rules.json shows only that a value
    changed. After a change that is meant, and reviewed, write it again, with the other snapshot, and commit them together:
        uv run python -m pytest tests/test_rules.py -k as_reviewed --new-snapshot"""
    r, found = rules.current(), {}
    for what, text in word_titles():
        found.setdefault(what, {})[text] = now(r, what, text)
    assert len(word_titles()) > 1000
    if request.config.getoption("--new-snapshot"):  # a title a line, so that a change reads as the titles it changes
        WORD_ANSWERS.write_text("{\n" + ",\n".join(f" {json.dumps(what)}: {{\n" + ",\n".join(
            f"  {json.dumps(text, ensure_ascii=False)}: {json.dumps(answer, ensure_ascii=False)}" for text, answer in answers.items())
            + "\n }" for what, answers in found.items()) + "\n}\n", encoding="utf-8")
    reviewed = json.loads(WORD_ANSWERS.read_text(encoding="utf-8"))
    differ = [(what, text, reviewed.get(what, {}).get(text, "(not there)"), answer) for what, answers in found.items()
              for text, answer in answers.items() if reviewed.get(what, {}).get(text, "(not there)") != answer]
    assert found == reviewed, ("The shipped words answer otherwise now: (what, title, as reviewed, now) "
                               f"{differ[:5]}{' and more' if len(differ) > 5 else ''}. Once the change is reviewed and "
                               "meant, write tests/shipped_word_answers.json again and commit the two together: uv run "
                               "python -m pytest tests/test_rules.py -k as_reviewed --new-snapshot")


# Made-up names and what the library page sorted them by before the build made the keys (its surnameFirst and seriesKey,
# compared under node with the page as it was then): the same now, from Python, with the shipped rules and with
# words of your own
AUTHOR_KEYS = {"Ann K. Le Moonwake": "le moonwake ann k", "Anna van den Berg": "van den berg anna", "Jo Brennan Jr.": "brennan jo",
               "Karl von der Lantern III": "von der lantern karl", "José Ángel de la Moonwake": "de la moonwake jose angel",
               "Writer": "writer", "": "", "A. B. C. Lantern-ish": "ish a b c lantern", "Brennan, Jo": "jo brennan",
               "Øystein Ålantern": "alantern øystein", "Ηλίας Φαροφύλακας": "φαροφυλακασ ηλιασ", "Иван ван Фонарёв": "фонарев иван ван",
               "月野 灯": "灯 月野", "Ann Writer PhD": "writer ann", "Jr": "jr", "Van Moonwake": "van moonwake",
               "Bea O'Lantern": "lantern bea o", "Cafe\u0301 Ann": "ann cafe", "Ann-Marie Writer": "writer ann marie",
               "Ann bin Writer": "writer ann bin", "Ann Writer Esq": "esq ann writer"}
SERIES_KEYS = {"Moonwake": "moonwake", "The Complete Moonwake": "moonwake", "Moonwake: Collection": "moonwake",
               "Moonwake Omnibus": "moonwakeomnibus", "Cafe\u0301 Lantern": "cafelantern", "D & D Lanterns": "ddlanterns",
               "Лунный сад: полное собрание": "лунныисадполноесобрание", "月の庭 完全版": "月の庭完全版"}


def test_the_keys_the_page_sorts_by_are_made_by_the_rules_as_the_page_made_them():
    r = rules.current()
    assert {name: r.author_key(name) for name in AUTHOR_KEYS} == AUTHOR_KEYS
    assert {s: r.series_key(s) for s in SERIES_KEYS} == SERIES_KEYS
    assert catalog.sort_keys({"t": "x", "s": "The Complete Moonwake", "a": ["Anna van den Berg", "Ann Writer"]}) == \
        {"sk": "moonwake", "ak": "van den berg anna"}
    assert catalog.sort_keys({"t": "x", "s": "?!", "a": [" "]}) == {}  # no letter or digit: nothing to sort by
    mine('[names]\nparticles_add = ["bin"]\nsuffixes_add = ["esq"]\n[series]\nfold_words_add = ["omnibus", "полное собрание", "d & d"]\n')
    r = rules.current()
    assert (r.author_key("Ann bin Writer"), r.author_key("Ann Writer Esq")) == ("bin writer ann", "writer ann")
    assert [r.series_key(s) for s in ("Moonwake Omnibus", "Лунный сад: полное собрание", "D & D Lanterns")] == \
        ["moonwake", "лунныисад", "lanterns"]
    assert catalog.sort_keys({"t": "x", "s": "Moonwake Omnibus", "a": ["Ann Writer Esq"]}) == {"sk": "moonwake", "ak": "writer ann"}


def in_the_page(script: str) -> object:
    """What the page's code for the words typed search and the shelves go by (fold, norm, words), and then the script,
    give under node."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't on this computer: the page's code runs only in a browser here")
    page = (rules.DATA.parent / "catalog_page.html").read_text(encoding="utf-8")
    code = re.search(r"^const FOLDED = .*?^const words = [^\n]*\n", page, re.S | re.M)[0]
    script = f"{code}\n{script}\n"
    out = subprocess.run([node, "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(out.stdout)


# Made-up names in many scripts, and the hard cases: each of them has words, on the page as in Python
NAMES = ["Книга 1", "Другая 1", "ЁЛКИ И ЛУНА", "Ёлки и луна", "月の庭 第2巻", "月の庭 完全版", "ガラスの港", "ｶﾞﾗｽ ﾗﾝﾀﾝ",
         "ΟΔΟΣ ΤΟΥ ΦΑΡΟΥ", "Οδός του φάρου", "Ἀστέρες", "달의 정원", "גן הירח", "حَدِيقَة القَمَر", "चाँद का बगीचा",
         "தொடர் 1", "গোলাপ বাগান", "สวนจันทร์", "Café Lantern", "Cafe\u0301 Lantern", "Ärger über Brød", "Straße der Laternen",
         "C", "C++", "C#", "İnci the Brave", "The ﬁrst Lantern", "Лунный сад: полное собрание", "Moonwake_Vol 2"]


def test_names_in_any_script_have_the_same_words_on_the_page_and_their_own_series():
    assert rules.words_of("Книга 1") == ["книга", "1"] and rules.words_of("Другая 1") == ["другая", "1"]
    assert rules.words_of("ΟΔΟΣ ΤΟΥ ΦΑΡΟΥ") == rules.words_of("Οδός του φάρου") == ["οδοσ", "του", "φαρου"]  # ς is σ
    assert rules.words_of("Cafe\u0301 Lantern") == rules.words_of("Café Lantern") == ["cafe", "lantern"]
    assert rules.words_of("Ärger über Brød") == ["arger", "uber", "brød"] and rules.words_of("C++ C#") == ["c", "c"]
    assert rules.words_of("月の庭 第2巻") == ["月の庭", "第2巻"] and rules.words_of("달의 정원") == ["달의", "정원"]
    assert rules.words_of("தொடர் 1") == ["தொடர", "1"]  # a vowel sign stays in its word; the virama, an accent, goes
    assert rules.words_of("चाँद का बगीचा") == ["चाँद", "का", "बगीचा"]
    assert rules.words_of("حَدِيقَة القَمَر") == rules.words_of("حديقة القمر") == ["حديقة", "القمر"]  # vowel points: accents
    assert rules.words_of("גַּן הַיָּרֵחַ") == rules.words_of("גן הירח") == ["גן", "הירח"]
    found = in_the_page(f"process.stdout.write(JSON.stringify({json.dumps(NAMES)}.map(words)));")
    assert dict(zip(NAMES, found)) == {s: rules.words_of(s) for s in NAMES}
    mine('[series]\nfold_words_add = ["полное собрание", "完全版"]\n')  # and their series keys, as the build makes them
    r = rules.current()
    assert r.series_key("Лунный сад: полное собрание") == r.series_key("ЛУННЫЙ САД") == "лунныисад"  # й's breve: an accent
    assert r.series_key("月の庭 完全版") == "月の庭" and len({r.series_key(s) for s in ("Книга 1", "Другая 1", "C")}) == 3


def test_every_letter_and_digit_folds_alike_on_the_page():
    # each character that the page's Unicode and Python's both know, through the page's words() and rules.words_of
    known, changed = [], {}
    for code in range(0x110000):
        c = chr(code)
        if unicodedata.category(c) in ("Cn", "Co", "Cs"):  # not given out yet, for private use, half of a pair
            continue
        if known and known[-1][1] == code - 1:
            known[-1][1] = code
        else:
            known.append([code, code])
        found = " ".join(rules.words_of(c))
        if found != c:
            changed[code] = found
    wrong = in_the_page(f"const known = {json.dumps(known)}, changed = {json.dumps(changed, ensure_ascii=False)}, wrong = [];\n"
                        "for (const [a, b] of known) for (let code = a; code <= b; code++) {\n"
                        "  const c = String.fromCodePoint(code), want = changed[code] ?? c;\n"
                        "  if (!/\\p{Cn}/u.test(c) && words(c).join(' ') !== want) wrong.push([code.toString(16), want, words(c)]);\n"
                        "}\nprocess.stdout.write(JSON.stringify(wrong));")
    assert wrong == []
    assert changed[ord("É")] == "e" and changed[ord("Ж")] == "ж" and changed[ord("+")] == "" and len(changed) > 10000


def test_older_rules_files_of_the_kinds_mean_what_they_meant():
    """Under 9d8c10b, [kinds] and [kinds.X] held only words: inherit = false and off = true there act on the words alone
    still (and on [kinds]' prose_max_mb, as before), never on a kind or what else its entry says, so a file written
    then works as it did. A kind can be added, but never taken away."""
    mine('[kinds]\ninherit = false\n[kinds.comic]\nbundle_words = ["lantern strips"]\n[kinds.rpg]\n'
         'publishers = ["moonwake games"]\n[kinds.tech]\nbundle_words = ["moonwake code"]\n')
    rules.reset()
    r = rules.current()
    assert list(r.kinds) == ["book", "tech", "rpg", "comic", "manual"] and r.prose_max_mb == 0  # the table started empty
    assert (r.kinds["comic"].device, r.kinds["comic"].layout, r.kinds["rpg"].itchio) == ("tablet", "comic", ("tabletop", "game"))
    assert r.kind_of("book", ["Humble Comics Bundle"], "", []) == ("book", "")  # the shipped words gone
    assert r.kind_of("book", ["The Lantern Strips Bundle"], "", []) == ("comic", 'bundle "Lantern Strips"')  # yours
    assert r.kind_of("book", [], "Moonwake Games", [("epub", 1 << 20)]) == ("rpg", "publisher Moonwake Games")  # no prose_max_mb
    assert r.kind_of("book", [], "", [("cbz", 1)]) == ("comic", "CBZ offered")  # a comic by its format, as before
    mine('[kinds.comic]\noff = true\n[kinds.rpg]\ninherit = false\nbundle_words = ["moonwake rpg"]\n')
    rules.reset()
    r = rules.current()
    comic, rpg = r.kinds["comic"], r.kinds["rpg"]
    assert (comic.words, comic.publishers, comic.layout, comic.folders, comic.label) == (None, (), "comic", ("comics",), "Comic")
    assert r.kind_of("book", ["Humble Comics Bundle"], "Moonwake Comics", [("pdf", 30 << 20)]) == ("book", "")  # words off
    assert r.kind_of("book", ["Humble RPG Bundle"], "", [("pdf", 1)]) == ("book", "")  # rpg's words, yours alone
    assert r.kind_of("book", ["Moonwake RPG Bundle"], "", [("pdf", 1)])[0] == "rpg" and rpg.device == "tablet" and rpg.prose
    assert r.kind_of("book", [], "", [("cbz", 1)])[0] == "comic" and r.prose_max_mb == 8.4  # the shipped one, a disk's MB
    assert r.mark("kinds", "comic") == "changed in mine.toml"  # turned off: its words
    mine('[kinds.book]\noff = true\n[kinds.manual]\ninherit = false\n')  # a kind with no words: nothing to take away
    rules.reset()
    assert list(rules.current().kinds) == ["book", "tech", "rpg", "comic", "manual"]


def test_a_publisher_is_found_by_whole_words_at_every_shop():
    """A publisher's name in the kinds is found as whole words of who made a title, after casefolding, at every shop,
    Humble's too: what stands next to it is no letter or digit, or the name's start or end."""
    r = rules.current()
    for maker in ("Pseudonym Press", "Samsara Games", "Avertical Studio"):
        assert r.kind_of("book", [], maker, [("pdf", 30 << 20)]) == ("book", ""), maker
    assert r.kind_of("book", [], "Udon Lantern Press", [("pdf", 30 << 20)]) == ("comic", "publisher Udon Lantern Press")
    assert r.kind_of("rpg", [], "SAMS Publishing", [("pdf", 30 << 20)])[0] == "tech"  # in capitals: casefolded
    assert r.kind_of("book", [], "Make: Community", [("pdf", 1)])[0] == "tech" and r.kind_of("book", [], "Makers", [])[0] == "book"


def test_the_words_of_the_shop_s_own_kind_change_nothing():
    """At DriveThruRPG, whose titles are RPG books, an RPG publisher's name says nothing more: a small EPUB of it stays
    an RPG book, as without the name. The prose rule is for words that would make a title another kind; Humble's stay
    as they were (a book is its own kind, and has no words)."""
    mine('[kinds.rpg]\npublishers_add = ["lantern wolf games"]\n')
    rules.reset()
    r = rules.current()
    assert r.kind_of("rpg", [], "Lantern Wolf Games", [("epub", 1 << 20)]) == ("rpg", "")
    assert r.kind_of("rpg", [], "Duskwater Press", [("epub", 1 << 20)]) == ("rpg", "")
    assert r.kind_of("book", [], "Lantern Wolf Games", [("epub", 1 << 20)]) == ("book", "prose despite publisher Lantern Wolf Games")
    assert r.kind_of("book", [], "Lantern Wolf Games", [("pdf", 30 << 20)]) == ("rpg", "publisher Lantern Wolf Games")


def test_hylly_settings_check_says_first_when_the_rules_can_t_be_read(capsys):
    """A kind your settings name is checked against the rules' kinds: when the rules can't be read, that is said first,
    and why, before any kind the settings name (the shipped kinds go meanwhile)."""
    from hylly import settings

    mine('[kinds.zine]\ndevice = "ereader"\n[genres.Trains]\nwordz = ["rail"]\n')
    paths.settings_file().write_text('[folders.shelf]\nkind = "zine"\n', encoding="utf-8")
    settings.reload()
    rules.reset()
    mistakes, _ = settings.problems()
    assert mistakes[0].startswith("The rules can't be read, so the kinds below are checked against the shipped ones: ")
    assert "mine.toml, [genres.Trains]: wordz isn't one of its keys" in mistakes[0]
    assert "[folders.shelf] kind = 'zine'" in mistakes[1]


def test_the_kinds_are_one_table_and_each_says_where_it_is_read():
    """The five kinds, their names and where each is best read, as one table in kinds.toml, in the order a Humble
    run fetches them: a kind's device is its own entry's, where [devices] tablet_kinds and either_kinds said it."""
    from hylly import humble, settings

    r = rules.current()
    assert list(r.kinds) == ["book", "tech", "rpg", "comic", "manual"] == list(humble.kind_order())
    assert [(k.label, k.plural, k.device) for k in r.kinds.values()] == [
        ("Book", "Books", "ereader"), ("Tech book", "Tech books", "either"), ("RPG book", "RPG books", "tablet"),
        ("Comic", "Comics", "tablet"), ("Manual", "Manuals", "ereader")]
    assert kinds_table()["rpg"] == {"label": "RPG book", "plural": "RPG books", "device": "tablet", "text_first": False}
    assert plan.device_for("comic", "epub", 1) == ("tablet", "comic") and plan.device_for("manual", "epub", 1)[0] == "ereader"
    assert plan.device_for("rpg", "pdf", 9)[1] == "RPG book" and [k.word for k in r.kinds.values()] == [
        "book", "tech book", "RPG book", "comic", "manual"]  # as a reason says each, as it always has
    assert plan.device_for("tech", "epub", 1)[1] == "tech book: fine on an e-reader, better on a tablet for code and diagrams"
    assert [k.name for k in r.kinds.values() if k.joins_books] == ["tech", "rpg"]  # their copies join a book's card
    assert [k.name for k in r.kinds.values() if k.text_first] == ["book"]  # its e-book opens first, before its PDF
    assert [k.name for k in r.kinds.values() if k.folder_named] == ["manual"]  # one named by its folder goes by its name
    assert plan.device_for("tech", "epub", 1)[0] == "either" and settings.formats_for("comic") == (settings.get("download")["tablet"], False)
    mine('[kinds.tech]\ndevice = "tablet"\n[kinds.comic]\ndevice = "either"\n')  # where tablet_kinds = ["rpg", "tech"] was
    rules.reset()
    assert plan.device_for("tech", "epub", 1)[0] == "tablet" and plan.device_for("comic", "epub", 1)[0] == "either"
    assert settings.formats_for("tech") == (settings.get("download")["tablet"], False)  # its device's list, the tablet's
    assert settings.formats_for("comic") == (settings.get("download")["ereader"], True)


def test_a_kind_added_by_one_entry_is_a_kind_everywhere(capsys):
    """Adding a kind is one table entry, here in a rules file of yours: the settings' lists, your folders, your
    corrections, every --kind, the e-reader feed, the library page and Humble's order take it, and its words find it."""
    from hylly import curation, folders, humble, settings

    mine('[kinds.zine]\nlabel = "Zine"\ndevice = "either"\nbundle_words = ["zine", "zines"]\nfolders = ["zines"]\n')
    rules.reset()
    paths.settings_file().write_text('[download]\nzine = ["pdf", "epub"]\n[folders.shelf]\nkind = "zine"\n'
                                     '[ereader_feed]\nkinds = ["book", "zine"]\n', encoding="utf-8")
    settings.reload()
    assert settings.get("download")["zine"] == ["pdf", "epub"] and settings.formats_for("zine") == (["pdf", "epub"], True)
    assert list(settings.section_defaults("download"))[2:8] == ["book", "tech", "rpg", "comic", "manual", "zine"]
    assert folders.folder("shelf").kind == "zine" and folders.folder("zines").kind == "zine"  # its folders, by name
    assert "zine" in curation.single("kind") and ereader_feed().kinds == ("book", "zine")
    assert kinds_table()["zine"] == {"label": "Zine", "plural": "Zines", "device": "either",  # an s, by default
                                           "text_first": False}
    assert cli._kinds(commands.kinds("zine,comic")) == {"zine", "comic"} and list(humble.kind_order())[-1] == "zine"
    assert rules.current().kind_of("book", ["The Lantern Zines Bundle"], "", []) == ("zine", 'bundle "Zines"')
    assert plan.device_for("zine", "epub", 1) == ("either", "zine: fine on an e-reader, better on a tablet")  # no words of its own
    cli.main(["rules", "try", "Moonwake Notes.pdf", "--kind", "zine"])
    assert "Kind:          zine (--kind)" in capsys.readouterr().out
    assert capsys.readouterr().err == ""  # nothing told: every setting of it is one


def test_a_kind_no_rules_have_is_refused_as_a_command_runs(capsys, tmp_path):
    """--kind is checked against the kinds in effect as the command runs (cli._kinds), on the computer it runs on: a
    kind your rules add is one, and one written wrong says which there are, before anything else is said (hylly
    lookup's lines on the keys saved, say)."""
    from hylly.exits import USAGE

    with pytest.raises(SystemExit, match=r"--kind: comix\? The kinds are"):
        cli.main(["lookup", "--kind", "comix", "--library", str(tmp_path)])
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("--kind: comix? The kinds are")  # its own words first: nothing before them
    with pytest.raises(SystemExit, match=r"--kind: comix\? The kinds are book, tech, rpg, comic, manual") as stopped:
        cli._kinds(commands.kinds("book,comix"))
    assert stopped.value.code == USAGE and cli._kinds(None) is None
    with pytest.raises(SystemExit) as stopped:
        cli.main(["humble", "download", "--kind", ","])  # no kind at all: argparse's own word
    assert stopped.value.code == 2


def test_every_shop_s_title_is_found_its_kind_by_the_same_rule():
    """Rules.kind_of, which every shop's titles go by: a comic format offered, else the words of the most
    particular kind (a comic before an RPG book before a tech book) in a bundle's name or who made it, a novel among
    them a book, over the shop's own kind."""
    r = rules.current()
    mine('[kinds.comic]\npublishers_add = ["moonwake comics"]\n[kinds.rpg]\npublishers_add = ["lantern wolf games"]\n')
    rules.reset()
    r = rules.current()
    pdf, cbz, epub = ("pdf", 30 << 20), ("cbz", 30 << 20), ("epub", 1 << 20)
    assert r.kind_of("rpg", [], "Moonwake Comics", [pdf]) == ("comic", "publisher Moonwake Comics")  # DriveThruRPG's comic
    assert r.kind_of("rpg", [], "Lantern Wolf Games", [epub]) == ("rpg", "")  # its own kind's words: nothing more
    assert r.kind_of("rpg", [], "Duskwater Press", [pdf]) == ("rpg", "")  # no words: the shop's own kind
    assert r.kind_of("book", ["The Graphic Novels Bundle"], "", []) == ("comic", 'bundle "Graphic Novels"')  # a StoryBundle book
    assert r.kind_of("book", ["Tabletop Comics Bundle"], "", [pdf]) == ("comic", 'bundle "Comics"')  # comic before rpg
    assert r.kind_of("book", ["Python and Tabletop Bundle"], "", [epub]) == ("tech", 'bundle "Python"')  # rpg prose: tech
    assert r.kind_of("rpg", ["Anything"], "", [cbz, pdf]) == ("comic", "CBZ offered")
    assert [r.itchio_kind(g) for g in ("book", "other", "comic", "tabletop", "game", "program", None)] == [
        "book", "book", "comic", "rpg", "rpg", "", ""]  # itch.io's classifications, as the survey groups them


def test_the_old_device_settings_are_told_with_what_took_their_place(capsys):
    """[devices] tablet_kinds and either_kinds, and a folder's devices, are no longer read: a kind's device is its own
    entry's in the rules, and every title of every folder gets one. Each is told once with what took its place, hylly
    settings check lists them, and the devices are as they were with none set."""
    from hylly import settings

    paths.settings_file().write_text('[devices]\ntablet_kinds = ["comic"]\neither_kinds = ["tech", "rpg"]\n'
                                     '[folders.shelf]\ndevices = true\n', encoding="utf-8")
    assert plan.device_for("rpg", "epub", 1)[0] == "tablet" and settings.get("folders")["shelf"]["_unknown"] == {"devices": True}
    err = capsys.readouterr().err
    for key in ("tablet_kinds", "either_kinds"):
        assert (f"[devices] {key} is no longer used, so it is left out. Where a kind is best read is its own now, device = "
                '"tablet", "either" or "ereader" under [kinds.X] in a rules file of yours (hylly rules copy kinds makes one)') in err
    assert ("[folders.shelf] devices is no longer used, so it is left out. Where a title is best read is worked out for every "
            "title now") in err
    cli.main(["settings", "check"])  # nothing wrong in it: what is left out is listed, with what took its place
    out = capsys.readouterr().out
    assert "[devices] tablet_kinds is no longer used" in out and "[folders.shelf] devices is no longer used" in out
    cli.main(["settings", "show"])
    out = capsys.readouterr().out
    assert re.search(r"tablet_kinds = \(not shown\) +# no longer used: left out", out)
    assert re.search(r"devices = \(not shown\) +# no longer used: left out", out)


def test_formats_are_one_table():
    mine('[formats.txt]\nbook = true\n[formats.cb7]\nkeep = true\n[main_file]\n# order = ["epub"]\n')  # an old copy's
    r = rules.current()
    assert ".txt" in r.book_suffixes and "main_file" not in r.data
    from hylly import curation

    assert "cb7" in curation.single("format") and "txt" not in curation.single("format")
    assert plan.device_for("book", "cb7", 1) == ("tablet", "CB7 only (fixed layout)")


def test_itch_io_takes_the_kinds_on_its_list_which_hold_every_format_and_no_program():
    r = rules.shipped()
    assert set(r.formats) <= r.itchio_fetch and not r.itchio_fetch & r.programs
    assert {"zip", "rar", "7z", "png", "jpg", "docx", "md"} <= r.itchio_fetch and not {"html", "mp3", "svg"} & r.itchio_fetch
    assert {"bat", "cmd", "scr", "lnk", "url", "scf", "ps1", "com", "vbs", "js", "wsf", "hta", "msi", "reg", "jar"} <= r.programs
    mine('[itchio]\nfetch_add = [".MP3", " ogg "]\n')  # written as an extension may be
    assert {"mp3", "ogg"} <= rules.current().itchio_fetch


def test_settings_with_their_defaults(watching):
    from hylly import lookups, reader, settings

    assert reader.cache_limit() == settings.DEFAULTS["reader"]["cache_gb"] * words.GB  # as reviewed: test_the_default_settings_are_as_reviewed
    assert list(lookups.in_order()) == ["metron", "comicvine", "openlibrary", "googlebooks"]
    paths.settings_file().write_text('[reader]\ncache_gb = 1\n[lookups]\norder = ["openlibrary", "metron"]\naccept = 0.9\n'
                                     '[ereader_feed]\nformats = ["epub"]\n', encoding="utf-8")
    assert reader.cache_limit() == 1 * words.GB and list(lookups.in_order()) == ["openlibrary", "metron"]
    assert lookups.accept() == 0.9 and settings.get("lookups")["_set"] == ["accept", "order"]
    assert ereader_feed().formats == ("epub",)
    paths.settings_file().write_text('[lookups]\norder = ["openlibrary", "nowhere"]\n', encoding="utf-8")
    with pytest.raises(SystemExit, match=r"\[lookups\] order: nowhere\?"):
        lookups.in_order()
    paths.settings_file().write_text('[devices]\nfixed_layout = "phone"\n', encoding="utf-8")
    settings.reload()  # a command that starts with it
    with pytest.raises(SystemExit, match=r"\[devices\] fixed_layout = 'phone': use tablet or either or ereader"):
        plan.device_for("book", "pdf", 1)


def test_a_running_server_keeps_the_settings_from_before_a_mistake(capsys, monkeypatch, watching):
    from hylly import reader

    monkeypatch.setattr(archives, "sevenzip", REAL_SEVENZIP)  # as the settings say, before a mistake and after

    paths.settings_file().write_text("[reader]\ncache_gb = 1\n", encoding="utf-8")
    assert reader.cache_limit() == 1 * words.GB
    seven = archives.sevenzip()
    paths.settings_file().write_text('[reader]\ncache_gb = "lots"\n', encoding="utf-8")
    assert reader.cache_limit() == 1 * words.GB and archives.sevenzip() == seven  # CBR pages go on, whatever the section
    err = capsys.readouterr().err
    assert "[reader] cache_gb = 'lots': a number" in err and "The settings from before stay" in err
    assert reader.cache_limit() == 1 * words.GB and capsys.readouterr().err == ""  # told once
    paths.settings_file().write_text("[reader]\ncache_gb = 12\n", encoding="utf-8")  # put right: read again
    assert reader.cache_limit() == 12 * words.GB
    paths.settings_file().write_text('[reader]\ncache_gb = "lots"\n', encoding="utf-8")  # the same again: told again
    assert reader.cache_limit() == 12 * words.GB and "cache_gb" in capsys.readouterr().err
    paths.settings_file().write_text('[reader]\ncache_gb = 2\nlibrary = "D:\\Books"\n', encoding="utf-8")
    assert reader.cache_limit() == 12 * words.GB
    assert re.search(r"settings\.toml: not readable as TOML \(.*line 3", capsys.readouterr().err)
    good = paths.settings_file()
    good.write_text("[reader]\ncache_gb = 4\n", encoding="utf-8")
    assert reader.cache_limit() == 4 * words.GB
    st = good.stat()
    for _ in range(2):  # a mistake, then the file put back as it was read (a copy of it, say): told each time
        good.write_text('[reader]\ncache_gb = "lots"\n', encoding="utf-8")
        assert reader.cache_limit() == 4 * words.GB and "cache_gb" in capsys.readouterr().err
        good.write_text("[reader]\ncache_gb = 4\n", encoding="utf-8")
        os.utime(good, ns=(st.st_atime_ns, st.st_mtime_ns))
        assert reader.cache_limit() == 4 * words.GB and capsys.readouterr().err == ""


def test_settings_toml_written_wrong_names_the_line():
    from hylly import settings

    paths.settings_file().write_text('[reader]\ncache_gb = 2\nlibrary = "D:\\Books"\n', encoding="utf-8")
    with pytest.raises(SystemExit, match=r"settings\.toml: not readable as TOML \(.*line 3.*Paths go in single quotes"):
        paths.library_root()
    paths.settings_file().write_bytes("[reader]\n# Café\ncache_gb = 2\n".encode("cp1252"))
    with pytest.raises(SystemExit, match=r"settings\.toml isn't saved as UTF-8 \(line 2\): save it as UTF-8"):
        settings.get("reader")
    paths.settings_file().write_bytes(b"\xef\xbb\xbf[reader]\ncache_gb = 2\n")  # Notepad's UTF-8, with a BOM
    assert settings.get("reader")["cache_gb"] == 2


def test_every_toml_file_people_edit_is_read_alike_a_bom_fine_and_a_mistake_named_by_file_and_line(tmp_path):
    """settings.toml, a rules file, tags.toml and a theme, each read by paths.read_toml: a BOM at the start, as
    Notepad may write, is fine; a file not in UTF-8, or not TOML, is one mistake naming the file and the line, which each
    says in its own words. tags.toml and a theme took neither before: tomllib's "Invalid statement" for a BOM, and a
    tags.toml in Windows' ANSI stopped hylly tag suggest with Python's own UnicodeDecodeError."""
    from hylly import curation, settings, themes

    bom = b"\xef\xbb\xbf"
    tags = curation.rules_file(tmp_path)
    tags.parent.mkdir(parents=True)
    tags.write_bytes(bom + '["Lantern Stories"]\nkeywords = ["lantern"]\n'.encode("utf-8"))
    assert [r.tag for r in curation.load_rules(tmp_path, curation.Edits([]))] == ["Lantern Stories"]
    birch = (themes.SHIPPED / "birch.toml").read_text(encoding="utf-8")
    theme = tmp_path / "mine-theme.toml"
    theme.write_bytes(bom + birch.encode("utf-8"))
    assert themes.check(theme).name == "Birch"
    # a CR alone ends a line, as an old Mac's editor writes them: tags.toml and themes were read as text, which took it
    tags.write_bytes('["Lantern Stories"]\rkeywords = ["lantern"]\r'.encode("utf-8"))
    assert [r.tag for r in curation.load_rules(tmp_path, curation.Edits([]))] == ["Lantern Stories"]
    theme.write_bytes(birch.replace("\n", "\r").encode("utf-8"))
    assert themes.check(theme).name == "Birch"
    mine("").write_bytes(b'[genres.Lanterns]\rwords = ["lantern*"]\r')
    assert rules.current().genres_of("The Lantern Keeper") == ["Lanterns"]
    old_mac = mine("")
    old_mac.write_bytes('[x]\r# Caf\xe9\r'.encode("latin-1"))  # its line counted by every kind of line end
    with pytest.raises(paths.NotToml, match=r"isn't saved as UTF-8 \(line 2\)"):
        paths.read_toml(old_mac)
    assert paths.read_toml(mine("")) == {}
    for path in (paths.settings_file(), mine(""), tags, theme):
        path.write_bytes(bom + '# Café\n[x]\n'.encode("cp1252"))  # in Windows' ANSI, the é on line 1
        with pytest.raises(paths.NotToml) as found:
            paths.read_toml(path)
        assert (found.value.kind, found.value.line, found.value.path) == ("encoding", 1, path)
        assert str(found.value) == f"{path} isn't saved as UTF-8 (line 1): save it as UTF-8."
        path.write_text("[x]\na = 1\n= 2\n", encoding="utf-8")
        with pytest.raises(paths.NotToml) as found:
            paths.read_toml(path)
        assert (found.value.kind, found.value.line) == ("syntax", 3) and str(path) in str(found.value)
    tags.write_bytes('["Lantern Stories"]\nkeywords = ["café"]\n'.encode("cp1252"))
    with pytest.raises(SystemExit, match=r"tags\.toml isn't saved as UTF-8 \(line 2\): save it as UTF-8\.") as stopped:
        curation.load_rules(tmp_path, curation.Edits([]))
    assert stopped.value.code == NOT_SET_UP
    tags.write_text('[Lantern Stories]\nkeywords = ["lantern"]\n', encoding="utf-8")  # a name with a space, unquoted
    with pytest.raises(SystemExit, match=r"tags\.toml: not readable as TOML \(.*line 1.*\)\. A tag's name with spaces goes"):
        curation.load_rules(tmp_path, curation.Edits([]))
    theme.write_bytes(birch.replace('name = "Birch"', 'name = "Björk"').encode("cp1252"))
    line = birch[:birch.index('name = "Birch"')].count("\n") + 1
    with pytest.raises(themes.NotATheme, match=rf"^mine-theme\.toml isn't saved as UTF-8 \(line {line}\): save it as UTF-8\.$"):
        themes.check(theme)  # by its name alone, as a theme's mistakes are said
    paths.settings_file().write_text("", encoding="utf-8")
    settings.reload()


def test_numbers_are_finite_and_days_countable():
    from hylly import settings

    for text, said in (("[reader]\ncache_gb = inf\n", r"\[reader\] cache_gb = inf: a number"),
                       ("[reader]\ncache_gb = nan\n", r"cache_gb = nan: a number"),
                       ("[lookups]\naccept = nan\n", r"accept = nan: a number"),
                       ("[lookups]\nnone_days = 9999999999\n", r"none_days = 9999999999: a number of days, at most 999999999")):
        settings.reload()
        paths.settings_file().write_text(text, encoding="utf-8")
        with pytest.raises(SystemExit, match=said):
            settings.get("reader")
    settings.reload()
    paths.settings_file().write_text("[lookups]\nnone_days = 999999999\n", encoding="utf-8")
    assert settings.get("lookups")["none_days"] == 999999999


def test_a_setting_its_section_doesnt_have_is_told_once_and_listed(capsys, watching):
    from hylly import settings

    paths.settings_file().write_text('[download]\ncomic_formats = "largest"\n', encoding="utf-8")
    assert settings.get("download")["tablet"][0] == "cbz"
    assert ("[download] comic_formats isn't one of its settings (ereader, tablet, book, tech, rpg, comic, manual, size_tie, "
            "language, unsure)" in capsys.readouterr().err)  # a list for each kind, in the kinds' order (kinds.toml)
    paths.settings_file().write_text('[download]\ncomic_formats = "largest"\nsize_tie = 0.9\n', encoding="utf-8")
    assert settings.get("download")["size_tie"] == 0.9 and capsys.readouterr().err == ""  # once
    cli.main(["settings", "show"])  # the settings' own command: hylly rules no longer lists them
    assert re.search(r"comic_formats = \(not shown\) +# not one of \[download\]'s settings: left out", capsys.readouterr().out)


def test_a_setting_of_the_wrong_kind_stops_with_the_file_the_key_and_what_it_takes():
    """Every section is checked as the first eight always were: [server], [bell], [ereader_feed], [folders.NAME] and
    what is at the top. A "false" in quotes is a text, which bool() read as true; access_log = "no" left the log on;
    port = "abc" ended in a bare ValueError. Where the server listens, the address people use and the trusted proxies
    are checked as the server uses them."""
    from hylly import access, bell, folders, serve, settings
    from hylly.exits import NOT_SET_UP

    for text, ask, said in (
            ('[server]\naccess_log = "false"\n', access.settings, "[server] access_log = 'false': true or false, without quotes."),
            ('[server]\nlisten = "everywhere"\n', serve.options, "[server] listen = 'everywhere': an address of this computer's in "
                                                                  'quotes: "127.0.0.1" for this computer only, "0.0.0.0"'),
            ('[server]\nlisten = 0\n', serve.options, "[server] listen = 0: an address of this computer's in quotes"),
            ('[server]\naddress = "books.example.org"\n', serve.Hosts.from_settings,
             "[server] address = 'books.example.org': the address people use, in full and with nothing after its name and port"),
            ('[server]\naddress = "https://books.example.org/library/"\n', serve.Hosts.from_settings, "[server] address = "),
            ('[server]\naddress = "ftp://books.example.org"\n', serve.Hosts.from_settings, "[server] address = "),
            ('[server]\naddress = "https://books.example.org:port"\n', serve.Hosts.from_settings, "[server] address = "),
            ("[server]\naddress = 8770\n", serve.Hosts.from_settings, "[server] address = 8770: a text in quotes."),
            ('[server]\ntrusted_proxies = ["my proxy"]\n', access.trusted_proxies,
             "[server] trusted_proxies = ['my proxy']: addresses or networks in quotes, such as"),
            ('[server]\ntrusted_proxies = "127.0.0.1"\n', access.trusted_proxies,
             "[server] trusted_proxies = '127.0.0.1': a list in quotes, such as"),
            ('[server]\ntrusted_proxies = ["127.0.0.1", "0.0.0.0/0"]\n', access.trusted_proxies,
             "[server] trusted_proxies = ['127.0.0.1', '0.0.0.0/0']: 0.0.0.0/0 holds every address there is, so anyone's "
             "forwarding headers would count: name your proxies' own"),
            ('[server]\ntrusted_proxies = ["::/0"]\n', access.trusted_proxies, "[server] trusted_proxies = ['::/0']: ::/0 holds every address"),
            ('[server]\ntrusted_proxies = ["::ffff:0:0/96"]\n', access.trusted_proxies,
             "[server] trusted_proxies = ['::ffff:0:0/96']: ::ffff:0:0/96 holds every IPv4 address, written as IPv6"),
            ('[server]\ntrusted_proxies = ["::/1"]\n', access.trusted_proxies, "[server] trusted_proxies = ['::/1']: ::/1 holds every IPv4"),
            ('[server]\nport = "abc"\n', serve.options, "[server] port = 'abc': a port, a whole number from 1 to 65535, such as 8770."),
            ("[server]\nport = 70000\n", serve.options, "[server] port = 70000: a port, a whole number from 1 to 65535"),
            ('[server]\naccess_log = "no"\n', access.settings, "[server] access_log = 'no': true or false."),
            ('[server]\nproxy_header = "X-Real"\n', access.proxy_header, "[server] proxy_header = 'X-Real': use X-Forwarded-For or"),
            ('[server]\nallowed_hosts = "books.example.org"\n', serve.Hosts.from_settings,
             "[server] allowed_hosts = 'books.example.org': a list in quotes, such as ['books.example.org']."),
            ("[server]\nname = 5\n", serve.library_name, "[server] name = 5: a text in quotes."),
            ('[bell]\nnew_devices = "false"\n', bell.settings, "[bell] new_devices = 'false': true or false, without quotes."),
            ("[bell]\ndaily_summary = 8.5\n", bell.settings, "[bell] daily_summary = 8.5: a whole number, such as 8."),
            ('[ereader_feed]\nskip_tablet = "true"\n', ereader_feed, "[ereader_feed] skip_tablet = 'true': true or false, without"),
            ('[ereader_feed]\nkinds = ["Book", "comics"]\n', ereader_feed,
             "[ereader_feed] kinds = ['Book', 'comics']: kinds are book, tech, rpg, comic, manual."),  # the rules' kinds
            ('[ereader_feed]\nformats = ["epub", "largest"]\n', ereader_feed,
             '[ereader_feed] formats = [\'epub\', \'largest\']: "largest" is for the lists under [download]'),
            ('[ereader_feed]\nformats = "epub"\n', ereader_feed, "[ereader_feed] formats = 'epub': a list of formats in quotes"),
            ('[download]\nereader = ["epub", "."]\n', lambda: settings.get("download"),
             "[download] ereader = ['epub', '.']: a list of formats in quotes, such as [\"epub\", \"pdf\"]."),
            ('[download]\nrpg = "pdf"\n', lambda: settings.get("download"), "[download] rpg = 'pdf': a list of formats in quotes"),
            ('[download]\ntablet = ["cbz", "comic"]\n', lambda: settings.get("download"),
             "[download] tablet = ['cbz', 'comic']: 'comic' isn't a format Hylly knows. They are epub, mobi, azw3, azw, prc, "
             'txt, fb2, pdf, djvu, cbz, cbr, cb7, zip, and "largest" (formats.toml).'),
            ('[ereader_feed]\nformats = ["epbu"]\n', ereader_feed,
             "[ereader_feed] formats = ['epbu']: 'epbu' isn't a format Hylly knows. They are epub, mobi,"),
            ("[download]\nereader = []\n", lambda: settings.get("download"),
             "[download] ereader = []: a device's list of formats, best first, such as ['epub', 'azw3', 'mobi']: an empty "
             "one would keep only the largest file of every title. Take the line out for the list as it comes."),
            ("[download]\ntablet = []\n", lambda: settings.get("download"), "[download] tablet = []: a device's list of formats"),
            ('[folders.shelf]\ngenres = "false"\n', folders.configured, "[folders.shelf] genres = 'false': true or false, without quotes."),
            ('[folders.shelf]\nkind = "comics"\n', folders.configured, "[folders.shelf] kind = 'comics': use book or tech or rpg"),
            ('[folders.shelf]\nkinds = { "Code" = "techs" }\n', folders.configured, "[folders.shelf] kinds = {'Code': 'techs'}: kinds are book"),
            ("library = 5\n", serve.options, "library = 5: a text in quotes."),
            ("server = 1\n", serve.options, "server = ...: a section, [server], with its settings on the lines under it.")):
        settings.reload()  # a command, which a mistake stops
        paths.settings_file().write_text(text, encoding="utf-8")
        with pytest.raises(SystemExit) as stopped:
            ask()
        assert stopped.value.code == NOT_SET_UP and str(stopped.value).startswith(f"{paths.settings_file()}: {said}"), text
    settings.reload()
    paths.settings_file().write_text('storybundle_user = 12345\n[server]\nlisten = "0.0.0.0"\nport = "8790"\n'
                                     'proxy_header = "cf-connecting-ip"\naddress = "HTTPS://Books.Example.org/"\n'
                                     'trusted_proxies = [" 172.18.0.0/16 ", "::1"]\n[bell]\ndaily_summary = -1\nwrong_passwords = -1\n'
                                     '[ereader_feed]\nkinds = ["Book"]\n', encoding="utf-8")
    assert serve.options() == (8790, "0.0.0.0") and access.settings() is True  # as the server always read them
    assert settings.get("server")["address"] == "HTTPS://Books.Example.org" and serve.Hosts.from_settings().extra == {"books.example.org"}
    assert [str(n) for n in access.trusted_proxies()] == ["172.18.0.0/16", "::1/128"]
    assert access.proxy_header() == "CF-Connecting-IP" and bell.settings()["daily_summary"] == -1  # -1: no summary
    assert ereader_feed().kinds == ("book",)


def test_source_url_is_a_whole_web_address_hylly_s_own_as_it_comes_and_never_empty(watching):
    """[server] source_url, for a changed Hylly run for others, is where its Source links lead: a whole http or
    https address, a path too, in a web address's own characters, its scheme and name in small letters as the library
    page's own check reads them (a capital HTTPS left its line hidden), the rest as written. "", a name or password
    before an @ (an empty one too), and anything else stop with the key and what it takes, as the links must lead to a
    source, and nothing but a web address may go into them."""
    from hylly import PROJECT_URL, serve, settings

    assert serve.source_url() == PROJECT_URL == settings.DEFAULTS["server"]["source_url"]
    for written in ("https://code.example.org/lantern-wolf/hylly", "http://192.0.2.10:3000/moonwake/hylly.git?tab=src&x=1",
                    " https://code.example.org/hylly/ ", "https://[2001:db8::1]:8443/hylly"):
        paths.settings_file().write_text(f"[server]\nsource_url = '{written}'\n", encoding="utf-8")
        assert serve.source_url() == written.strip(), written
    paths.settings_file().write_text("[server]\nsource_url = 'HTTPS://Code.Example.ORG:8443/Lantern-Wolf/Hylly?Tab=A'\n",
                                     encoding="utf-8")
    assert serve.source_url() == "https://code.example.org:8443/Lantern-Wolf/Hylly?Tab=A"  # the path as written
    for wrong in ("", "  ", "code.example.org/hylly", "javascript:alert(1)", "ftp://code.example.org/hylly", "https://",
                  "https://ann:secret@code.example.org/hylly", "https://ann@code.example.org/hylly",
                  "https://@code.example.org/hylly", "https://:@code.example.org/hylly", "https://code.example.org/my hylly",
                  "https://code.example.org:port/hylly", 'https://code.example.org/"><b>x</b>', "https://[2001:db8::1/hylly",
                  "https://code.example.org/h\u00fclly"):
        settings.reload()  # a command, which a mistake stops
        paths.settings_file().write_text(f"[server]\nsource_url = '{wrong}'\n", encoding="utf-8")
        with pytest.raises(SystemExit) as stopped:
            serve.source_url()
        said = f"{paths.settings_file()}: [server] source_url = {wrong!r}: the whole address of this Hylly's source, such as"
        assert stopped.value.code == NOT_SET_UP and str(stopped.value).startswith(said), wrong
        assert f'"{PROJECT_URL}" (Hylly\'s own)' in str(stopped.value) and "take the line out" in str(stopped.value)
    settings.reload()
    paths.settings_file().write_text("[server]\nsource_url = 5\n", encoding="utf-8")
    with pytest.raises(SystemExit, match=r"\[server\] source_url = 5: the whole address"):
        serve.source_url()
    settings.reload()


def test_a_number_in_quotes_is_read_as_the_number_but_true_or_false_in_quotes_never():
    """cache_gb = "2" can only mean 2, and float() always read it so, where "false" in quotes would read as
    true. Every number the settings take goes by the one check: [server]'s, [bell]'s and [ereader_feed]'s, and those
    of [download], [devices], [reader] and [lookups] too."""
    from hylly import bell, serve, settings

    paths.settings_file().write_text('[server]\nport = " 8790 "\n[reader]\ncache_gb = "2"\npages_ahead = "7"\n'
                                     '[lookups]\naccept = "0.9"\n[devices]\nimage_heavy_mb = "25.5"\n[bell]\ndaily_summary = "-1"\n'
                                     'wrong_passwords = "4"\n[ereader_feed]\nmax_mb = "80"\n', encoding="utf-8")
    assert serve.options() == (8790, "127.0.0.1") and ereader_feed().max_mb == 80
    assert settings.get("reader")["cache_gb"] == 2 and settings.get("reader")["pages_ahead"] == 7
    assert settings.get("lookups")["accept"] == 0.9 and settings.get("devices")["image_heavy_mb"] == 25.5
    assert bell.settings()["daily_summary"] == -1 and bell.settings()["wrong_passwords"] == 4
    for text, said in (('[server]\naccess_log = "true"\n', "[server] access_log = 'true': true or false, without quotes."),
                       ('[reader]\ncache_gb = "-2"\n', "[reader] cache_gb = '-2': a number, such as 10."),
                       ('[reader]\ncache_gb = "inf"\n', "[reader] cache_gb = 'inf': a number, such as 10."),
                       ('[lookups]\naccept = "1.5"\n', "[lookups] accept = '1.5': a number from 0 to 1, such as 0.8."),
                       ('[reader]\npages_ahead = "7.5"\n', "[reader] pages_ahead = '7.5': a whole number, such as 5."),
                       ('[bell]\ndaily_summary = "8.5"\n', "[bell] daily_summary = '8.5': a whole number, such as 8."),
                       ('[server]\nport = "8770.5"\n', "[server] port = '8770.5': a port, a whole number from 1 to 65535")):
        settings.reload()  # a command, which a mistake stops
        paths.settings_file().write_text(text, encoding="utf-8")
        with pytest.raises(SystemExit) as stopped:
            settings.get("server")
        assert str(stopped.value).startswith(f"{paths.settings_file()}: {said}"), text


def test_an_unknown_key_section_or_name_at_the_top_is_named_once_and_the_rest_is_read(capsys, watching):
    from hylly import access, folders, serve

    paths.settings_file().write_text(
        'libary = "D:/Made-up Books"\nlisten = "0.0.0.0"\n[server]\nlogn = "all"\nlisten = "0.0.0.0"\nport = 8790\n'
        'ntfy = "https://ntfy.sh/made-up-topic-under-server"\n[Server]\nlogin = "all"\n'
        '[reeder]\ncache_gb = 2\n[folders.shelf]\nknd = "comic"\nlabel = "Shelf"\nsession = "made-up-cookie-value"\n'
        '[bell]\ntopic = "https://ntfy.sh/another-made-up-topic"\n', encoding="utf-8")
    assert serve.options() == (8790, "0.0.0.0") and access.settings() is True  # [Server] isn't [server]
    assert folders.configured() == {"shelf": {"label": "Shelf"}}
    err = capsys.readouterr().err
    for said in ("[server] logn isn't one of its settings (name, listen, port, access_log",
                 "[Server] isn't one of its sections (download, devices, reader, lookups, reviews, look, tools, remote, server, "
                 "bell, ereader_feed, backup, inbox, folders.<name>), so it is left out.",
                 "[reeder] isn't one of its sections",
                 "libary, at the top before any [section], isn't one of the settings there (library, rules, storybundle_user), "
                 "so it is left out.",
                 "listen, at the top before any [section], isn't one of the settings there (library, rules, storybundle_user; it "
                 "belongs under [server]), so it is left out.",
                 "[folders.shelf] knd isn't one of its settings (label, kind, kinds, genres", "[bell] topic isn't one of its settings"):
        assert err.count(said) == 1, said
    assert "[server] ntfy isn't one of its settings" in err and "[folders.shelf] session isn't one of its settings" in err
    assert "made-up" not in err and "Made-up Books" not in err  # by name only: it may be a secret
    paths.settings_file().write_text(paths.settings_file().read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert serve.options() == (8790, "0.0.0.0") and capsys.readouterr().err == ""  # read again, as a server does: told once
    cli.main(["settings", "show"])  # the settings' own command: hylly rules no longer lists them
    out = capsys.readouterr().out
    assert "made-up" not in out and "Made-up Books" not in out  # a topic or a cookie in the wrong place: never shown
    assert re.search(r"\n    logn = \(not shown\) +# not one of \[server\]'s settings: left out\n", out)
    assert re.search(r"\n    ntfy = \(not shown\) +# not one of \[server\]'s settings: left out\n", out)
    assert re.search(r"\n    session = \(not shown\) +# not one of \[folders.shelf\]'s settings: left out\n", out)
    assert re.search(r"\n    topic = \(not shown\) +# not one of \[bell\]'s settings: left out\n", out)
    assert re.search(r"\n  \[reeder\] +# not one of the sections: left out", out) and re.search(r"\n  libary +# at the top", out)
    assert re.search(r"\n    port = 8790 +# settings.toml\n", out) and re.search(r'\n    trusted_proxies = \["127.0.0.0/8", "::1"\] +# default\n', out)
    assert re.search(r'\n  \[folders.shelf\]\n    label = "Shelf" +# settings.toml\n    kind = "book" +# default\n', out)


def test_every_example_in_the_settings_example_the_docs_and_the_help_is_read_without_a_word(capsys):
    """settings.example.toml with every commented-out key taken in, the settings in docs/rules.md and the help's own
    folder: no key that works is refused or called unknown."""
    import html

    from hylly import access, bell, folders, serve, settings

    repo = Path(__file__).resolve().parents[1]
    example = re.sub(r"(?m)^# (\[folders\.\w+\]|[a-z_]+ = .*)$", r"\1", (repo / "settings.example.toml").read_text(encoding="utf-8"))
    assert all(f"\n{key} = " in example for key in ("listen", "address", "trusted_proxies", "allowed_hosts",
                                                    "storybundle_user"))  # strip_suffix: read, not offered
    assert not any(f"\n{key} = " in example for key in ("login", "home_networks", "open_door", "lan", "tunnel_port",
                                                        "public_address", "ntfy", "ntfy_token"))  # no longer used, or secret
    docs = re.search(r"\n## Settings\n.*?```toml\n(.*?)```", (repo / "docs" / "rules.md").read_text(encoding="utf-8"), re.S)[1]
    help_page = (repo / "src" / "hylly" / "help.html").read_text(encoding="utf-8")
    shelf = html.unescape(re.search(r"<pre>(\[folders\.ebooks\].*?)</pre>", help_page, re.S)[1])
    for written in (example, re.sub(r"(?m)^# (sevenzip = .*)$", r"\1", docs), shelf):
        settings.reload()
        paths.settings_file().write_text(written, encoding="utf-8")
        for section in settings.DEFAULTS:
            settings.get(section)
        serve.options(), access.settings(), access.proxy_header(), bell.settings(), ereader_feed(), folders.configured()
        serve.Hosts.from_settings(), access.trusted_proxies(), serve.library_name()
        assert capsys.readouterr().err == "", written
    assert folders.configured()["ebooks"]["kinds"] == {"Graphic Novels": "comic", "Programming": "tech"}


def test_each_sections_defaults_are_what_its_own_code_used_before():
    """Nothing set: every value as the .get() defaults in serve.py, access.py, bell.py and folders.py had it before
    those sections went into the table (settings.py)."""
    from hylly import access, bell, folders, serve, settings

    assert serve.options() == (8770, "127.0.0.1") and serve.PORT == settings.DEFAULTS["server"]["port"]
    assert access.settings() is True and access.proxy_header() == "X-Forwarded-For" and access.trusted_proxies() == access.LOOPBACK
    assert serve.library_name() == "Library" and serve.Hosts.from_settings().extra == set()
    assert bell.settings() == {"ntfy": "", "ntfy_token": "", "toast": True, "wrong_passwords": 3, "downloads_per_hour": 10,
                               "new_devices": True, "library_missing": 5, "quiet_minutes": 60,
                               "alarms_per_hour": 10, "remote_detail": "counts", "daily_summary": 8, "command": []}
    assert ereader_feed() == Device()
    assert folders.configured() == {} and folders.folder("shelf") == folders.Folder(name="shelf", label="Shelf")
    assert folders.folder("comics").kind == "comic" and folders.folder("manuals").kind == "manual"
    plain = folders.Folder(name="shelf", label="")
    assert {key: getattr(plain, key) for key in settings.FOLDER} == settings.FOLDER  # Folder's own defaults, key for key
    assert access.PROXY_HEADERS == ("X-Forwarded-For", "Forwarded",
                                                                                  "CF-Connecting-IP", "X-Real-IP")


# ---- the alarms' secrets, out of settings.toml --------------------------------------------------------------------------------

TOPIC = "https://ntfy.sh/A-MADE-UP-SECRET-TOPIC"
OLD_PHONE = ('[server]\r\nname = "Made-up Library"  # ours\r\n\r\n[bell]\r\nntfy = "' + TOPIC + '"  # the phone\r\n'
             '# ntfy_token = "A-MADE-UP-SECRET-TOKEN-OFF"   # turned off\r\n# ntfy = ""\r\nntfy_token = "A-MADE-UP-SECRET-TOKEN"\r\n'
             'wrong_passwords = 3\r\n')


def the_secrets() -> dict[str, str]:
    from hylly import settings

    folder = settings.secrets_dir()
    return {p.name: p.read_text(encoding="utf-8") for p in folder.iterdir()} if folder.is_dir() else {}


def test_the_alarms_secrets_under_bell_are_never_read_nor_moved_and_each_is_told_with_its_file_now(capsys):
    """Where an older Hylly kept them, set by hand, at the top as bell.ntfy too: no command moves them or reads them any
    more. Each is told once, by name, as no longer used, with the file in secrets/ it goes in now, and never its value;
    settings.toml stays as written, and nothing is put beside it."""
    from hylly import bell, settings

    file = paths.settings_file()
    for written in (OLD_PHONE, 'bell.ntfy = "' + TOPIC + '"\nbell.ntfy_token = "A-MADE-UP-SECRET-TOKEN"\n'):
        file.write_bytes(written.encode("utf-8"))
        settings.reload()
        cli.main(["settings", "path"])  # any command but a dry run
        assert bell.settings()["ntfy"] == "" and bell.settings()["ntfy_token"] == ""  # no longer read from there
        err = capsys.readouterr().err
        for key, name in (("ntfy", "ntfy-url"), ("ntfy_token", "ntfy-token")):
            assert err.count(f"[bell] {key} is no longer used, so it is left out. It is a secret, kept in a file of its own "
                             f"now, secrets/{name} in the settings folder: put its value there, and take the line out of "
                             "settings.toml. hylly bell says where that file is.") == 1
        assert "MADE-UP" not in err and "move-secrets" not in err
        assert file.read_bytes() == written.encode("utf-8") and not list(file.parent.glob("settings.toml?*"))
        assert the_secrets() == {} and settings.old_secrets(paths.section("bell")) == ["ntfy", "ntfy_token"]
        mistakes, left = settings.problems()
        assert mistakes == [] and sum("is no longer used" in line for line in left) == 2 and "MADE-UP" not in str(left)
    with pytest.raises(SystemExit):
        cli.main(["settings", "move-secrets"])  # the command that moved them is gone with the move


def test_the_format_lists_take_a_format_as_an_extension_is_written(watching):
    """The e-reader feed's formats matched a title's files only as written: ".epub" or "EPUB" matched none, and the
    feed was empty. A dot before one is left out now, in [download]'s lists too, and capitals don't count."""
    from hylly import settings

    paths.settings_file().write_text('[ereader_feed]\nformats = [".EPUB", " .mobi "]\nkinds = ["Book", " TECH "]\n'
                                     '[download]\nereader = [".AZW3", "epub"]\ncomic = ["CBR", "Largest", ".pdf"]\n',
                                     encoding="utf-8")
    feed = ereader_feed()
    assert feed.formats == ("epub", "mobi") and feed.kinds == ("book", "tech") and feed.ereader_order == ("azw3", "epub")
    assert feed.best_file({"k": "book", "f": [["a/b.mobi", "mobi", 1]]}) == ["a/b.mobi", "mobi", 1]
    assert settings.get("download")["comic"] == ["cbr", "largest", "pdf"]
    assert settings.formats_for("comic") == (["cbr", "largest", "pdf"], False)  # its own list
    assert settings.formats_for("rpg")[0][0] == "pdf" and settings.formats_for("manual") == (["azw3", "epub"], True)
    paths.settings_file().write_text('[download]\nbook = []\nrpg = []\n', encoding="utf-8")
    assert settings.get("download")["rpg"] == [] and settings.formats_for("rpg")[0][0] == "cbz"  # its device's list
    mine('[formats.lwbook]\nlayout = "reflowable"\nshop = true\n')  # a format your rules know
    paths.settings_file().write_text("[download]\nereader = ['lwbook', 'epub']\n", encoding="utf-8")
    settings.reload()
    assert settings.get("download")["ereader"] == ["lwbook", "epub"]
    gone = (paths.settings_file().parent / "rules-not-there-yet").as_posix()  # on a share not up yet: no format is
    paths.settings_file().write_text(f"rules = '{gone}'\n[download]\nereader = ['lwbook', 'epub']\n", encoding="utf-8")
    settings.reload()  # refused meanwhile, for no command, nor the server's start, to stop for it
    assert settings.get("download")["ereader"] == ["lwbook", "epub"]
    paths.settings_file().write_text(f"rules = '{gone}'\n[download]\nereader = []\n", encoding="utf-8")
    settings.reload()
    with pytest.raises(SystemExit, match="ereader = \\[\\]: a device's list of formats"):  # the rest of the check as ever
        settings.get("download")


def test_the_old_format_settings_are_told_with_what_took_their_place(capsys):
    """comic_format, rpg_format, text_order and layout_order under [download], and always_formats under [ereader_feed],
    are no longer read: each is told once, by name with the key that took its place, and left out, as a key a section
    hasn't is; the defaults go."""
    from hylly import settings

    paths.settings_file().write_text('[download]\ncomic_format = "largest"\nrpg_format = "like-comics"\n'
                                     'text_order = ["mobi", "epub"]\nlayout_order = ["pdf"]\n'
                                     '[ereader_feed]\nalways_formats = ["epub"]\n', encoding="utf-8")
    assert settings.get("download")["ereader"][0] == "epub" and settings.get("download")["tablet"][0] == "cbz"
    assert ereader_feed() == Device()
    err = capsys.readouterr().err
    assert "[download] text_order is no longer used, so it is left out. It is renamed: ereader = [...] under [download]" in err
    for key in ("comic_format", "layout_order"):
        assert (f"[download] {key} is no longer used, so it is left out. Comics and RPG books go by a list now, tablet "
                "under [download] (rpg for RPG books)") in err
    assert ('comic_format = "cbz-first" is tablet = ["cbz", "cbr", "cb7", "largest", "pdf", "epub"], as it comes; '
            '"largest" is tablet = ["largest", "cbz", "cbr", "cb7", "pdf", "epub"].') in err  # each old value, as it is now
    assert ('"like-comics" is rpg = [], which reads them by the tablet list while RPG books are read on a tablet '
            '([kinds.rpg] device = "tablet" in the rules, as it comes)') in err
    assert "[download] rpg_format is no longer used, so it is left out. RPG books have a list of their own now, rpg" in err
    assert ("[ereader_feed] always_formats is no longer used, so it is left out. A title you put in the feed yourself "
            "offers the first of formats under [ereader_feed], then of ereader under [download]") in err
    cli.main(["settings", "show"])
    out = capsys.readouterr().out
    assert re.search(r"text_order = \(not shown\) +# no longer used: left out", out)
    assert re.search(r"rpg = \[.*\] +# default", out) and re.search(r"book = \[\] \(its device's list", out)
    mistakes, left = settings.problems()
    assert mistakes == [] and len(left) == 5 and all("is no longer used" in x for x in left)


def test_a_main_file_table_of_an_older_version_says_where_it_went():
    """[main_file] is no longer read: the main file goes by the settings' order, as the download does. One with rules in
    it stops, saying so; an empty one, as a copy of an older formats.toml has it, changes nothing (test_formats_are_one_table)."""
    mine('[main_file]\norder = ["pdf", "epub"]\n')
    with pytest.raises(rules.RulesError, match=r"mine\.toml: \[main_file\] is no longer read: a title's main file is the "
                                                r"first of its kind's formats under \[download\] in settings\.toml"):
        rules.current()


def test_any_command_makes_the_folder_for_the_alarms_secrets_and_a_walled_unit_needs(capsys):
    """secrets/ in the settings folder, its owner's only, for the secrets to be put in, and for a walled server's unit,
    which names it: made as any command but a dry run starts."""
    from hylly import settings

    assert not settings.secrets_dir().exists()
    cli.main(["settings", "edit", "--dry-run"])
    assert not settings.secrets_dir().exists()  # a dry run changes nothing
    cli.main(["settings", "path"])
    assert settings.secrets_dir().is_dir() and the_secrets() == {}
    if os.name != "nt":  # the rights of Linux and macOS
        assert settings.secrets_dir().stat().st_mode & 0o777 == 0o700


def test_the_doctor_tells_of_the_old_secrets_in_settings_toml_and_of_a_file_it_cant_read():
    """Set by hand under [bell], where they are no longer read: the phone gets nothing, and the doctor says where they
    go now, which is the owner's to do; and a secret's file that can't be read is the file to put right, not
    settings.toml."""
    from hylly import doctor, settings

    file = paths.settings_file()
    file.write_text(OLD_PHONE.replace("\r\n", "\n"), encoding="utf-8")
    found = doctor._settings()
    problem = next(c for c in found if c["state"] == "problem")
    assert problem["id"] == "settings-old-secrets" and problem["owner"] is True and "run" not in problem
    assert f"{file} still has the alarms' ntfy address or token under [bell]" in problem["says"]
    assert problem["next"].startswith(f"Put the address in {settings.secrets_dir() / 'ntfy-url'}, and a token")
    assert "MADE-UP" not in str(found)
    file.write_text('[bell]\n# ntfy = ""\nntfy_token = ""\nwrong_passwords = 3\n', encoding="utf-8")  # none set
    assert not [c for c in doctor._settings() if c["state"] == "problem"]
    settings.secrets_dir().mkdir(parents=True)
    (settings.secrets_dir() / "ntfy-url").write_bytes(b"\x80\x81 not text")
    problem = doctor._settings()[0]
    assert problem["state"] == "problem" and problem["next"] == f"Put it right in {settings.secrets_dir() / 'ntfy-url'}."
