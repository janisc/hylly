"""Humble's plan against a synthetic library shaped like Humble's order data: the file each title is downloaded in, its
kind, where it is read and its folder."""

from __future__ import annotations

import dataclasses
import json
import random
import re

from hylly import download as dl, paths, plan, rules, snapshot
from hylly.inventory import MIB, FileOption
from hylly.manifest import Manifest

from helpers import ICON, ORDERS, ascii_titles, order, sub


def titles_by_id(result):
    return {t.product.machine_name: t for t in result.titles}


def test_choices_and_devices():
    result = plan.build(snapshot.sanitize(ORDERS))
    t = titles_by_id(result)

    assert (t["novel"].kind, t["novel"].choice.ext, t["novel"].device) == ("book", "epub", "ereader")
    assert t["mobionly"].choice.ext == "mobi"
    assert (t["pdfnovel"].choice.ext, t["pdfnovel"].device) == ("pdf", "tablet")
    assert t["artbook"].device == "tablet"  # 80 MB EPUB

    assert (t["lwolf1"].kind, t["lwolf1"].choice.ext, t["lwolf1"].device) == ("comic", "cbz", "tablet")
    assert t["lwolf2"].choice.ext == "cbz"  # CBZ first on the tablet's list, despite the 3x larger PDF
    assert (t["lwnovel"].kind, t["lwnovel"].device) == ("book", "ereader")  # prose in a comic bundle

    assert (t["shrinefox1"].kind, t["shrinefox1"].choice.ext) == ("comic", "pdf")
    assert (t["fieldrules"].kind, t["fieldrules"].choice.filename) == ("rpg", "fr_hq.pdf")
    assert (t["clanledger"].kind, t["clanledger"].choice.ext) == ("rpg", "pdf")  # PDF despite the larger EPUB
    assert (t["isnovel"].kind, t["isnovel"].device) == ("book", "ereader")  # a novel in an RPG bundle

    pss = t["pss"]
    assert (pss.kind, pss.choice.ext, pss.device) == ("tech", "epub", "either")
    assert [f.filename for f in pss.extras] == ["pss_code.zip"]


def test_languages_parts_and_extras():
    t = titles_by_id(plan.build(ORDERS))
    assert t["manual"].choice.filename == "manual_en.pdf"  # larger German file skipped
    assert t["pixelpost"].choice.filename == "48.pdf" and [f.filename for f in t["pixelpost"].extras] == ["36.pdf"]
    puzzles = t["puzzles"]
    assert puzzles.choice.filename == "pz.epub"
    assert [f.filename for f in puzzles.extras] == ["pz_maps.pdf"]  # the video is not an extra
    assert "pdfquality_warning_comics" not in {p.machine_name for p in plan.build(ORDERS).products}


def settings(text: str) -> None:
    from hylly import paths

    paths.settings_file().write_text(text, encoding="utf-8")


def test_largest_rule():
    settings('[download]\ntablet = ["largest", "cbz", "cbr", "cb7", "pdf", "epub"]\n')  # as comic_format = "largest" was
    t = titles_by_id(plan.build(ORDERS))
    assert t["lwolf1"].choice.ext == "cbz"  # within 5% of the PDF
    assert t["lwolf2"].choice.ext == "pdf"  # PDF far larger than CBZ
    assert t["shrinefox1"].choice.ext == "pdf"  # nothing else offered


def test_download_and_device_settings():
    settings('[download]\nrpg = []\nereader = ["mobi", "epub"]\nlanguage = "german"\n[devices]\nimage_heavy_mb = 100\n')
    mine = paths.settings_file().parent / "rules"
    mine.mkdir(exist_ok=True)
    (mine / "mine.toml").write_text('[kinds.rpg]\ndevice = "either"\n', encoding="utf-8")  # an RPG book's device, its own
    rules.reset()
    t = titles_by_id(plan.build(ORDERS))
    assert t["clanledger"].choice.ext == "epub"  # an RPG book by its device's list, the e-reader's here: not the PDF
    assert t["novel"].choice.ext == "mobi"  # MOBI before EPUB
    assert t["manual"].choice.filename == "manual_de.pdf"  # the German file kept
    assert t["fieldrules"].device == "tablet"  # still a PDF: fixed layout
    assert (t["artbook"].device, t["isnovel"].device, t["pss"].device) == ("ereader", "ereader", "either")


def test_a_kind_you_choose_chooses_the_file_of_a_title_not_downloaded_yet_and_keeps_one_downloaded():
    """Your kind, given for any purchase of a title (by its Humble id), wins over the rules': a title not
    downloaded yet takes its file by that kind's list and is read where that kind is; one downloaded keeps its format,
    as a change of the order does, and only says a better one is offered."""
    plain = titles_by_id(plan.build(ORDERS))["novel"]
    assert (plain.kind, plain.found, plain.choice.ext, plain.device) == ("book", "book", "epub", "ereader")
    t = titles_by_id(plan.build(ORDERS, kinds={"novel": "rpg"}))["novel"]
    assert (t.kind, t.found, t.choice.ext, t.device) == ("rpg", "book", "pdf", "tablet")  # an RPG book's list: the PDF
    assert t.kind_reason == "your correction (the rules find book)" and t.alternatives[0].ext == "epub"
    kept = titles_by_id(plan.build(ORDERS, kinds={"novel": "rpg"}, kept={"novel": {"epub"}}))["novel"]
    assert (kept.kind, kept.choice.ext, kept.device, kept.better) == ("rpg", "epub", "tablet", "pdf")  # nothing comes again
    same = titles_by_id(plan.build(ORDERS, kinds={"lwolf1": "comic"}))["lwolf1"]
    assert (same.kind, same.kind_reason) == ("comic", "your correction")  # as the rules find it


def test_settings_say_what_is_wrong():
    import pytest

    settings('[download]\ntablet = "cbz"\n')
    with pytest.raises(SystemExit, match=r"\[download\] tablet = 'cbz': a list of formats in quotes"):
        plan.build(ORDERS)
    settings('[download]\nlanguage = "zorbish"\n')
    with pytest.raises(SystemExit, match=r"language = 'zorbish': use one of the \[languages\]"):
        plan.build(ORDERS)


def test_duplicates():
    result = plan.build(ORDERS)
    t = titles_by_id(result)
    # Same Humble id bought twice: one title, the copy with more formats is kept.
    assert t["novel"].product.bundle == "Humble Book Bundle: Fantasy by Lantern Press"
    assert sorted(c.bundle for c in t["novel"].copies) == ["Humble Book Bundle: Fantasy Again", "Humble Book Bundle: Fantasy by Lantern Press"]
    # Identical file under the same title, different ids: merged.
    shared = [x for x in result.titles if x.product.title == "Shared Story"]
    assert len(shared) == 1 and len(shared[0].copies) == 2
    # Identical file under different titles: kept apart.
    assert "samefile_other" in t and "samefile_other2" in t
    # Audio and games never become ebook titles.
    assert "audiobook" not in t and "somegame" not in t


def old_norm_title(title: str) -> str:
    """The old plan.norm_title: only the letters a to z and digits were kept."""
    return re.sub(r"[^a-z0-9]+", "", title.lower().replace("&", "and"))


def test_a_humble_title_in_plain_ascii_is_compared_as_it_always_was():
    # so that no grouping of those changes: the same file under the same title, or the same title with other files
    wrong = [t for t in ascii_titles() if plan.norm_title(t) != old_norm_title(t)]
    assert not wrong, wrong[:5]


def test_humble_titles_in_any_script_are_compared_by_their_letters():
    assert plan.norm_title("Книга первая") == "книгапервая" and plan.norm_title("ΟΔΟΣ") == plan.norm_title("Οδός") != ""
    assert plan.norm_title("Tähti") == plan.norm_title("Tahti") == "tahti" and plan.norm_title("Moss & Stone") == "mossandstone"
    orders = {
        "K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
            sub("kniga1", "Книга первая", "Moonwake Press", [("EPUB", "kniga1.epub", 1, "one-file")]),
            sub("kniga2", "Книга вторая", "Moonwake Press", [("EPUB", "kniga2.epub", 1, "one-file")]),
            sub("minato", "ガラスの港", "Moonwake Press", [("EPUB", "minato.epub", 1)]),
            sub("odos_caps", "ΟΔΟΣ", "Moonwake Press", [("EPUB", "odos_caps.epub", 1)]),
            sub("tahti", "Tähti", "Moonwake Press", [("EPUB", "tahti.epub", 1, "star-file")])]),
        "K2": order("K2", "2025-01-01T10:00:00", "Humble Book Bundle: Lantern Again", [
            sub("odos", "Οδός", "Moonwake Press", [("EPUB", "odos.epub", 1)]),
            sub("tahti2", "Tahti", "Moonwake Press", [("EPUB", "tahti2.epub", 1, "star-file")])]),
    }
    t = titles_by_id(plan.build(orders))
    assert "kniga1" in t and "kniga2" in t  # one file under two titles: kept apart, as in Latin letters (test_duplicates)
    assert len(t["tahti"].copies) == 2 and "tahti2" not in t  # one file under one title, an accent apart: one title
    assert "odos" in t and "odos_caps" in t  # the same title, other files: two titles, as in Latin letters


def test_folders_are_safe():
    t = titles_by_id(plan.build(ORDERS))
    assert t["shrinefox1"].path == "humble/Humble Manga Bundle - Hanabi Press/Shrine Fox - Stray Spirit 1/shrinefox_strayspirit_vol1.pdf"
    assert t["con"].folder.endswith("/Con_")


def test_a_file_name_loses_the_dots_and_spaces_windows_would_drop():
    # itch.io's downloads judge a file's kind by this name (itchio.kind_of): "Setup.exe." is saved, and run, as an EXE
    for name in ("Setup.exe.", "Setup.exe ", "Setup.exe . ."):
        assert paths.safe_filename(name) == "Setup.exe"
    assert paths.safe_filename("notes.pdf:hidden.bat") == "notes.pdf_hidden.bat"  # never a stream of another file
    assert paths.safe_filename("CON.pdf") == "CON_.pdf"


def _old_name(name: str, names: set[str]) -> str:
    """The loop each shop had for a new file's name, as it was (humble._job, drivethrurpg.select, itchio.select)."""
    stem, dot, ext = name.rpartition(".") if "." in name else (name, "", "")
    n = 1
    while name.lower() in names:
        n += 1
        name = f"{stem} ({n}).{ext}" if dot else f"{stem} ({n})"
    names.add(name.lower())
    return name


def _old_folder(base: str, used: set[str]) -> str:
    """The loop each shop had for a new title's folder, as it was (plan.assign_folders, storybundle.select,
    drivethrurpg.select, itchio.select)."""
    folder, n = base, 1
    while folder.lower() in used:
        n += 1
        folder = f"{base} ({n})"
    used.add(folder.lower())
    return folder


def test_every_shop_s_free_names_are_the_ones_its_own_loop_gave():
    """One free_name and one free_folder for every shop, giving byte for byte the names each shop's loop gave, in
    any order of names met, and with the same names taken after."""
    names = ["Guide.pdf", "guide.PDF", "Guide.pdf", "Guide (2).pdf", "README", "readme", "README", ".bat", ".BAT",
             "a.b.c.pdf", "A.B.C.pdf", "Lantern Wolf.tar.gz", "lantern wolf.tar.gz", "x.", "X.", "Ann Writer - Moonwake.epub",
             "ann writer - moonwake.EPUB", "Guide (3).pdf"]
    folders = ["humble/Moonwake Bundle/Lantern Wolf", "humble/moonwake bundle/lantern wolf", "humble/Moonwake Bundle/Lantern Wolf",
               "humble/Moonwake Bundle/Lantern Wolf (2)", "drivethrurpg/Moonwake Press/Clan Ledger", "itchio/Ann Writer/Quiet Engineers"]
    for seed in range(40):
        order, before = random.Random(seed).sample(names, len(names)), {"guide (2).pdf"} if seed % 2 else set()
        old, new = set(before), set(before)
        assert [_old_name(n, old) for n in order] == [plan.free_name(n, new) for n in order] and old == new, order
        order, before = random.Random(seed).sample(folders, len(folders)), {"humble/moonwake bundle/lantern wolf"} if seed % 3 else set()
        old, new = set(before), set(before)
        assert [_old_folder(f, old) for f in order] == [plan.free_folder(f, new) for f in order] and old == new, order
    taken: set[str] = set()
    assert [plan.free_name(n, taken) for n in ("Map.pdf", "MAP.PDF", "map.pdf", "Notes", "notes")] == [
        "Map.pdf", "MAP (2).PDF", "map (3).pdf", "Notes", "notes (2)"]
    used = {"storybundle/the moonwake bundle/first tide"}
    assert plan.free_folder("storybundle/The Moonwake Bundle/First Tide", used) == "storybundle/The Moonwake Bundle/First Tide (2)"
    assert "storybundle/the moonwake bundle/first tide (2)" in used


def test_a_name_humble_s_link_escapes_is_saved_as_it_reads():
    orders = {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
        sub("lantern", "Lantern Wolf", "Moonwake Press", [("EPUB", "Lantern%20Wolf%20%E2%80%93%20Ann%20Writer.epub", 1)]),
        sub("drive", "Drive Notes", "Moonwake Press", [("PDF", "C%3Adrive.pdf", 1)]),
        sub("plain", "Plain Notes", "Moonwake Press", [("PDF", "plain_notes.pdf", 1)])])}
    t = titles_by_id(plan.build(orders))
    assert t["lantern"].path.endswith("/Lantern Wolf – Ann Writer.epub") and t["lantern"].choice.ext == "epub"
    assert t["drive"].path.endswith("/C_drive.pdf")  # decoded, then made safe as every name is
    assert t["plain"].path.endswith("/plain_notes.pdf")  # a name with nothing escaped, as ever


def test_sanitize_strips_signatures_and_keys():
    text = json.dumps(snapshot.sanitize(ORDERS))
    assert "SECRET" not in text and "ttl=" not in text
    assert "AAAAA-BBBBB-CCCCC" not in text and '"key_redeemed": true' in text
    assert ICON in text  # image links keep their own parameters


def test_your_format_choice():
    t = titles_by_id(plan.build(ORDERS, formats={"novel": "pdf", "lwnovel": "pdf"}))
    assert (t["novel"].choice.filename, t["novel"].choice_reason, t["novel"].device) == ("novel.pdf", "PDF: your choice", "tablet")
    assert [f.filename for f in t["novel"].alternatives] == ["novel.epub", "novel.mobi"]
    assert t["lwnovel"].choice.filename == "lwn.epub" and "PDF, your choice, is not offered" in t["lwnovel"].choice_reason
    assert titles_by_id(plan.build(ORDERS))["novel"].choice.filename == "novel.epub"


# ---- one order of formats, said once -----------------------------------------------------------------------------------

def old_choose(kind, books, prefer=None):
    """The old plan.choose, with its default settings then: comic_format "cbz-first", rpg_format "pdf",
    text_order and layout_order as below, size_tie 0.95, language "". Its picks are what a library downloaded so far."""
    text_order = ["epub", "azw3", "mobi", "azw", "prc", "pdf", "djvu", "cbz", "cbr", "cb7", "txt"]
    layout_order = ["cbz", "cbr", "cb7", "pdf", "epub"]
    rank = lambda order, ext: order.index(ext) if ext in order else len(order)  # noqa: E731
    if prefer:
        mine = [f for f in books if f.ext == prefer]
        if mine:
            return old_choose(kind, mine)[0], f"{prefer.upper()}: your choice"
        best, why = old_choose(kind, books)
        return best, f"{why} ({prefer.upper()}, your choice, is not offered)"
    r = rules.current()
    books = [f for f in books if not r.other_language(f.label, "")] or books
    if kind == "rpg":
        pdfs = [f for f in books if f.ext == "pdf"]
        if pdfs:
            return max(pdfs, key=lambda f: f.size), "PDF keeps the print layout" if len(books) > 1 else "only format offered"
    if kind in ("comic", "rpg"):
        candidates = [f for f in books if f.ext in layout_order] or books
        comic_files = [f for f in candidates if f.ext in r.comic_formats]
        if comic_files:
            best = max(comic_files, key=lambda f: f.size)
            return best, f"{best.ext.upper()} preferred when offered"
        top = max(f.size for f in candidates)
        near = [f for f in candidates if f.size >= top * 0.95]
        best = min(near, key=lambda f: (rank(layout_order, f.ext), -f.size))
        if len(candidates) == 1:
            return best, "only format offered"
        if best.size == top:
            return best, "largest file"
        return best, f"within 5% of the largest; {best.ext.upper()} preferred"
    best = min(books, key=lambda f: (rank(text_order, f.ext), -f.size))
    if len({f.ext for f in books}) == 1:
        return best, "only format offered"
    return best, f"{best.ext.upper()} preferred for text"


def offer(*files) -> list[FileOption]:
    """A title's files as Humble offers them: (label, MB), the format the label's first word."""
    return [FileOption(label, label.split()[0].lower(), f"{n}.{label.split()[0].lower()}", int(mb * MIB), None, "book")
            for n, (label, mb) in enumerate(files)]


def the_known_change(kind, old, new, books) -> bool:
    """The one pick that changes: a comic, or an RPG book without a PDF, offered in more than one comic format takes the
    first of CBZ, CBR and CB7 offered (in the language to keep), where it took the largest of them."""
    kept = [f for f in books if not rules.current().other_language(f.label, "")] or books
    first = next((x for x in ("cbz", "cbr", "cb7") if any(f.ext == x for f in kept)), None)
    return kind in ("comic", "rpg") and old.ext in ("cbz", "cbr", "cb7") and new.ext == first != old.ext


def test_the_order_keeps_every_pick_of_before_but_cbz_over_cbr():
    """Made-up offers in every case plan.choose had: books, tech books and manuals by text_order, comics and RPG books
    with their size tie, one format only, a format of your own offered or not, and the language. Each pick and its
    reason are as before, but where a comic is offered as CBZ and as a larger CBR (or CB7): CBZ now."""
    rng = random.Random(1511)
    labels = ["EPUB", "MOBI", "AZW3", "AZW", "PRC", "TXT", "PDF", "PDF (HQ)", "PDF (Deutsch)", "PDF (English)", "DJVU",
              "CBZ", "CBR", "CB7", "EPUB (Deutsch)", "MOBI (Fixed Layout)"]
    sizes = [1, 2, 8, 9.5, 10, 19, 20, 50, 95, 96, 97, 100, 150, 200]
    changed = same = 0
    for _ in range(1500):
        books = offer(*((rng.choice(labels), rng.choice(sizes)) for _ in range(rng.randint(1, 5))))
        kind = rng.choice(["book", "tech", "manual", "comic", "rpg"])
        prefer = rng.choice([None] * 6 + ["pdf", "epub", "cbz", "cbr", "mobi"])
        old, new = old_choose(kind, books, prefer), plan.choose(kind, books, prefer)
        if old[0] is not new[0] or old[1] != new[1]:
            assert the_known_change(kind, old[0], new[0], books), (kind, prefer, [(f.label, f.size) for f in books], old, new)
            changed += 1
        else:
            same += 1
    assert changed > 5 and same > 1000, (changed, same)


def pick(kind: str, *files, prefer: str | None = None) -> tuple[str, str]:
    """The label of the file plan.choose picks of these made-up offers, and why."""
    best, why = plan.choose(kind, offer(*files), prefer)
    return best.label, why


def test_each_case_of_the_choice_by_name():
    assert pick("book", ("EPUB", 2), ("MOBI", 4), ("PDF", 6)) == ("EPUB", "EPUB preferred for text")
    assert pick("book", ("MOBI", 1)) == ("MOBI", "only format offered")
    assert pick("book", ("EPUB", 2), ("MOBI", 4), prefer="pdf") == ("EPUB", "EPUB preferred for text (PDF, your choice, is not offered)")
    assert pick("book", ("EPUB", 2), ("PDF", 6), prefer="pdf") == ("PDF", "PDF: your choice")
    assert pick("manual", ("PDF (Deutsch)", 4), ("PDF (English)", 3)) == ("PDF (English)", "only format offered")  # the language
    assert pick("comic", ("PDF", 100), ("EPUB", 97)) == ("PDF", "largest file")
    assert pick("comic", ("PDF", 96), ("EPUB", 100)) == ("PDF", "within 5% of the largest; PDF preferred")  # the size tie
    assert pick("comic", ("PDF", 50), ("EPUB", 100)) == ("EPUB", "largest file")
    assert pick("comic", ("PDF", 50), ("EPUB", 97), ("MOBI", 200)) == ("EPUB", "largest file")  # of PDF and EPUB only
    assert pick("comic", ("MOBI", 200), ("TXT", 1)) == ("MOBI", "largest file")  # neither: the largest of any
    assert pick("comic", ("PDF", 309)) == ("PDF", "only format offered")
    assert pick("comic", ("CBZ", 100), ("PDF", 300)) == ("CBZ", "CBZ preferred when offered")
    assert pick("rpg", ("EPUB", 19), ("PDF", 15)) == ("PDF", "PDF keeps the print layout")
    assert pick("rpg", ("PDF", 50), ("PDF (HQ)", 120)) == ("PDF (HQ)", "PDF keeps the print layout")
    assert pick("rpg", ("CBR", 30), ("EPUB", 40)) == ("CBR", "CBR preferred when offered")
    assert pick("rpg", ("EPUB", 2)) == ("EPUB", "only format offered")
    # the one known change: CBZ first, where the larger of the two won
    assert old_choose("comic", offer(("CBZ", 100), ("CBR", 150)))[0].ext == "cbr"
    assert pick("comic", ("CBZ", 100), ("CBR", 150)) == ("CBZ", "CBZ preferred when offered")


def test_a_title_downloaded_already_stays_on_its_format(tmp_path):
    """The order decides for a title with no file yet. One downloaded as its own choice stays in that format while it is
    offered, unless hylly set format names another: a comic downloaded as CBR before isn't fetched again as CBZ."""
    both = offer(("CBZ", 100), ("CBR", 150))
    assert plan.choose("comic", both, had={"cbr"}) == (both[1], "CBR: as downloaded before")
    assert plan.choose("comic", both, had={"cbz", "cbr"}) == (both[0], "CBZ preferred when offered")
    assert plan.choose("comic", both, prefer="cbz", had={"cbr"}) == (both[0], "CBZ: your choice")
    assert plan.choose("comic", both[:1], had={"cbr"}) == (both[0], "CBZ preferred when offered")  # no CBR offered now
    comics = [sub(f"lw{n}", f"Lantern Wolf Vol. {n}", "Gloamlight Comics", [("CBZ", f"lw{n}.cbz", 100), ("CBR", f"lw{n}.cbr", 150)])
              for n in range(1, 5)]
    orders = {"K1": order("K1", "2024-01-01T10:00:00", "Humble Comics Bundle: Lantern Wolf", comics)}
    record = Manifest(tmp_path, "humble")
    folder = "humble/Humble Comics Bundle - Lantern Wolf"
    record.add({"path": f"{folder}/Lantern Wolf Vol. 1/lw1.cbr", "shop_id": "lw1", "size": 1, "role": "book", "ext": "cbr"})
    record.add({"path": f"{folder}/Lantern Wolf Vol. 2/lw2.cbr", "shop_id": "lw2", "size": 1, "role": "book", "ext": "cbr",
                "instead_of": f"{folder}/Lantern Wolf Vol. 2/lw2.cbz"})  # a substitute: the first choice is tried again
    record.add({"path": f"{folder}/Lantern Wolf Vol. 3/lw3.cbr", "shop_id": "lw3", "size": 1, "role": "extra", "ext": "cbr"})
    record.add({"path": f"{folder}/Lantern Wolf Vol. 4/lw4.cbr", "shop_id": "lw4", "size": 1})  # an older record, no ext
    assert plan.kept_formats(list(record.entries.values())) == {"lw1": {"cbr"}, "lw4": {"cbr"}}
    t = titles_by_id(plan.for_library(orders, tmp_path))
    assert [t[f"lw{n}"].choice.ext for n in range(1, 5)] == ["cbr", "cbz", "cbz", "cbr"]
    assert t["lw1"].choice_reason == "CBR: as downloaded before" and t["lw2"].choice_reason == "CBZ preferred when offered"
    assert [f.ext for f in t["lw1"].alternatives] == ["cbz"]  # the fallback, should the CBR fail
    assert titles_by_id(plan.build(orders))["lw1"].choice.ext == "cbz"  # nothing downloaded: by the order
    # a better format offered now doesn't download by itself: hylly humble download counts them, set format moves one
    assert [x.product.machine_name for x in plan.kept_behind(plan.for_library(orders, tmp_path))] == ["lw1", "lw4"]
    assert t["lw1"].better == "cbz" and not t["lw2"].better
    moved = titles_by_id(plan.build(orders, formats={"lw1": "cbr"}, kept={"lw1": {"cbr"}}))["lw1"]
    assert (moved.choice.ext, moved.better) == ("cbr", "")  # your own choice: nothing better to tell of


def humble_answers(monkeypatch, orders: dict) -> None:
    """Humble's site as a stand-in, answering with these orders: nothing reaches past this computer."""
    from hylly import humble

    from helpers import HumbleSite

    monkeypatch.setattr(humble, "HumbleClient", HumbleSite(orders).client)
    monkeypatch.setattr(humble, "PAUSE", 0)
    monkeypatch.setenv("HYLLY_HUMBLE_SESSION", "c" * 80)


def test_hylly_humble_download_counts_the_titles_a_better_format_is_offered_for(tmp_path, monkeypatch, capsys):
    """What hylly humble status said, a download says now, a dry run too: which titles stay in the format downloaded
    before though a better one is offered, in its words and its --json."""
    from hylly import cli

    orders = {"K1": order("K1", "2024-01-01T10:00:00", "Humble Comics Bundle: Lantern Wolf", [
        sub("lw1", "Lantern Wolf Vol. 1", "Gloamlight Comics", [("CBZ", "lw1.cbz", 100), ("CBR", "lw1.cbr", 150)]),
        sub("novel", "The Novel", "Lantern Press", [("EPUB", "novel.epub", 2), ("MOBI", "novel.mobi", 4)])])}
    humble_answers(monkeypatch, orders)
    record = Manifest(tmp_path, "humble")
    record.add({"path": "humble/B/Lantern Wolf Vol. 1/lw1.cbr", "shop_id": "lw1", "size": 1, "role": "book", "ext": "cbr"})
    record.add({"path": "humble/B/The Novel/novel.mobi", "shop_id": "novel", "size": 1, "role": "book", "ext": "mobi"})
    cli.main(["humble", "download", "--dry-run", "--library", str(tmp_path)])
    out = capsys.readouterr().out
    assert ("2 titles stay in the format downloaded before, though one earlier on the list under [download] is offered "
            "now: hylly set format FORMAT --id ID downloads it for one (hylly humble download --dry-run --json lists each "
            "title") in out
    cli.main(["humble", "download", "--dry-run", "--library", str(tmp_path), "--json"])
    assert sorted(json.loads(capsys.readouterr().out)["kept_behind"], key=lambda x: x["id"]) == [
        {"title": "Lantern Wolf Vol. 1", "id": "h:lw1", "kept_format": "cbr", "better_format": "cbz"},
        {"title": "The Novel", "id": "h:novel", "kept_format": "mobi", "better_format": "epub"}]


def test_a_format_set_after_a_file_you_chose_wins_only_over_a_file_of_another_format(tmp_path, capsys):
    """A format set after a file answer (hylly set format, or the
    library page's Format to keep) has its way over a file of another format, and set back to automatic the file counts
    again; a format that agrees with the file (one PDF of two chosen, then PDF) keeps it; a file chosen after a format
    wins in its turn. hylly set file auto takes the file correction away even while a format overrides it, so it
    doesn't count again later. The edits replay so, and each undoes as any correction does."""
    from hylly import cli, curation

    def choice(title: str = "clanledger") -> str:
        return titles_by_id(plan.for_library(ORDERS, tmp_path))[title].choice.filename

    ledger = [{"id": "h:clanledger", "t": "Clan Ledger", "f": [[""]]}]
    assert choice() == "cl.pdf"  # an RPG book: its PDF, as the rules have it
    curation.change_single(tmp_path, "file", "cl.epub", ledger, by="rory")  # a which-file answer, say
    assert choice() == "cl.epub"
    curation.change_single(tmp_path, "format", "pdf", ledger, by="rory")  # set since, of another format: it wins
    assert choice() == "cl.pdf" and curation.files(tmp_path) == {}
    curation.change_single(tmp_path, "format", "auto", ledger, by="rory")  # undone: the file counts again
    assert choice() == "cl.epub" and curation.files(tmp_path) == {"clanledger": "cl.epub"}
    curation.change_single(tmp_path, "format", "pdf", ledger, by="rory")
    assert choice() == "cl.pdf"
    changed, was = curation.change_single(tmp_path, "file", "cl.epub", ledger, by="rory")  # chosen again, after the format
    assert changed and was == {"h:clanledger": None} and choice() == "cl.epub"  # the newer: in effect, an edit of its own
    curation.change_single(tmp_path, "file", "auto", ledger, by="rory")  # taken back: the format again
    assert choice() == "cl.pdf"

    rules_ = [{"id": "h:fieldrules", "t": "Iron Stride Field Rules", "f": [[""]]}]  # a PDF and a PDF (HQ)
    assert choice("fieldrules") == "fr_hq.pdf"  # the larger, as the rules guess
    curation.change_single(tmp_path, "file", "fr.pdf", rules_, by="rory")
    curation.change_single(tmp_path, "format", "pdf", rules_, by="rory")  # agrees with the file chosen: it stays
    assert choice("fieldrules") == "fr.pdf" and curation.files(tmp_path)["fieldrules"] == "fr.pdf"

    curation.change_single(tmp_path, "file", "cl.epub", ledger, by="rory")
    curation.change_single(tmp_path, "format", "pdf", ledger, by="rory")  # the file overridden now, by the format
    snapshot.save(ORDERS, paths.state_dir(tmp_path), "humble")  # Humble's orders, which name the title for hylly set
    capsys.readouterr()
    cli.main(["set", "file", "auto", "--id", "h:clanledger", "--yes", "--library", str(tmp_path)])
    assert "Nothing to do" not in capsys.readouterr().out and "file" not in curation.load(tmp_path).fixed["h:clanledger"]
    curation.change_single(tmp_path, "format", "auto", ledger, by="rory")
    assert choice() == "cl.pdf"  # gone for good: the rules again, not the file


# ---- a title's place: its folder, kept whatever changes around it (once in test_storage.py) --------------------------

def test_a_title_keeps_its_folder_whatever_changes_around_it():
    first = plan.build(ORDERS)
    recorded = {t.product.machine_name: t.folder for t in first.titles}
    again = plan.build(ORDERS, folders=recorded)
    assert [t.folder for t in again.titles] == [t.folder for t in first.titles]

    moved = {**recorded, "novel": "humble/Somewhere Older/The Novel"}  # as a rule from an earlier version put it
    titles = {t.product.machine_name: t for t in plan.build(ORDERS, folders=moved).titles}
    assert titles["novel"].folder == "humble/Somewhere Older/The Novel"
    assert titles["novel"].path.startswith("humble/Somewhere Older/The Novel/")


def test_a_recorded_folder_is_found_under_any_of_a_titles_purchases():
    t = next(t for t in plan.build(ORDERS).titles if t.product.machine_name == "novel")
    first = dataclasses.replace(t.product, machine_name="novel_bought_first")
    t2 = plan.Title(product=t.product, copies=[first, t.product])
    plan.assign_folders([t2], {first.machine_name: "humble/First Bundle/The Novel"})
    assert t2.folder == "humble/First Bundle/The Novel"
    # both have a record: it stays with the copy that gets downloaded, or it would download again
    t3 = plan.Title(product=t.product, copies=[first, t.product])
    plan.assign_folders([t3], {first.machine_name: "humble/First Bundle/The Novel", t.product.machine_name: "humble/Later Bundle/The Novel"})
    assert t3.folder == "humble/Later Bundle/The Novel"


def test_a_new_title_never_gets_a_folder_someone_has():
    titles = plan.build(ORDERS).titles
    taken = titles[0].folder
    for t in titles:
        t.folder = ""
    plan.assign_folders(titles, {titles[1].product.machine_name: taken})
    assert titles[1].folder == taken and titles[0].folder.lower() != taken.lower()
    assert len({t.folder.lower() for t in titles}) == len(titles)


def test_the_plan_reads_the_folders_from_the_record(tmp_path):
    dl.Manifest(tmp_path, "humble").add({"path": "humble/Old/Book/a.epub", "shop_id": "novel", "size": 1})
    assert plan.recorded_folders(tmp_path) == {"novel": "humble/Old/Book"}
    novel = next(t for t in plan.for_library(ORDERS, tmp_path).titles if t.product.machine_name == "novel")
    assert novel.folder == "humble/Old/Book"


def test_file_names_stay_short_and_allowed():
    long = "x" * 300 + ".epub"
    assert len(paths.safe_filename(long)) == 150 and paths.safe_filename(long).endswith(".epub")
    assert len(paths.safe_filename("ä" * 300 + ".pdf").encode()) <= 240 and paths.safe_filename("ä" * 300 + ".pdf").endswith(".pdf")
    assert paths.safe_filename("CONIN$.epub") == "CONIN$_.epub" and paths.safe_filename("Ordinary Book.epub") == "Ordinary Book.epub"
    assert paths.clean("CONOUT$") == "CONOUT$_"


def test_a_name_windows_takes_for_a_device_gets_a_underscore_after_that_part_wherever_a_dot_follows():
    """As Windows reads a name, by what comes before its first dot (paths.windows_cant_hold), with COM0 and LPT0 among
    its devices: a folder "Con. Artist" would be CON to it, as a file "NUL .epub" would be NUL."""
    assert [paths.clean(n) for n in ("COM0", "Con. Artist", "LPT9 ", "Nul.Moonwake Press", "console", "COM10", "Prn Garden")] == [
        "COM0_", "Con_. Artist", "LPT9_", "Nul_.Moonwake Press", "console", "COM10", "Prn Garden"]
    assert [paths.safe_filename(n) for n in ("COM0.pdf", "NUL .epub", "lpt0.cbz", "conout$.txt", "COM10.pdf", "aux")] == [
        "COM0_.pdf", "NUL _.epub", "lpt0_.cbz", "conout$_.txt", "COM10.pdf", "aux_"]
    assert paths.clean("Com\u00b2") == "Com\u00b2_" and paths.safe_filename("lpt\u00b9.pdf") == "lpt\u00b9_.pdf"  # COM² and LPT¹


def test_a_file_name_cut_short_is_no_device_s_name_either():
    """A name that is no device's until it is cut short to fit (a long run of spaces between CON and the rest, which
    the cut leaves out) is given its _ all the same."""
    assert paths.safe_filename("CON" + " " * 200 + "x.pdf") == "CON_.pdf"
    assert paths.safe_filename("Nul" + " " * 300 + "Points.epub") == "Nul_.epub"
    assert paths.safe_filename("Console" + " " * 200 + "x.pdf") == "Console.pdf"  # an ordinary name stays one


def test_a_text_file_is_image_heavy_by_a_disk_s_mb():
    """[devices] image_heavy_mb counts a disk's MB (words.py), and its default, 21, keeps the 20 MiB it was within 28,480
    bytes: an EPUB of 21,000,000 bytes is still the e-reader's, one of a byte more a tablet's, and the why says its size
    as every size is said."""
    assert plan.device_for("book", "epub", 21_000_000)[0] == plan.device_for("book", "epub", 20 * MIB)[0] == "ereader"
    assert plan.device_for("book", "epub", 21_000_001) == ("tablet", "EPUB is 21.0 MB, probably image-heavy")
