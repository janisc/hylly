"""Your tags: the edits replayed, the hylly tag command, and tags that survive moves, merges and rebuilds."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
import shutil
import threading
import unicodedata
import zipfile
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import httpx
import pytest

from hylly import catalog, cli, curation, exits, matching, paths, people, rules, serve
from hylly.library import Catalog, Device

from helpers import FIRST_RELEASE_RECORDS, OPF, at_the_first_step, make_epub, named_now, near_miss, png, rec
from helpers import in_the_page, logged_in, two_moonwakes


ATOM = "{http://www.w3.org/2005/Atom}"


def records(root):
    return Catalog(root).get()


def waiting(root) -> str:
    """The titles gone whose tags and corrections wait, as the catalog's place questions name them: a line
    each, its title, its file then and what is kept."""
    return "\n".join(f"{s['t'] or s['id']} ({s['f'] or 'its file unknown'}): {s['kept']}"
                     for q in curation.questions(root) if q["kind"] == "place" for s in q["subjects"])


def test_replay():
    lines = [
        {"field": "tag", "op": "add", "value": "Knitting", "titles": [{"id": "a"}, {"id": "b"}]},
        {"field": "tag", "op": "add", "value": "knitting", "titles": [{"id": "c"}]},  # the same tag, however typed
        {"field": "tag", "op": "remove", "value": "KNITTING", "titles": [{"id": "b"}]},
        {"field": "rating", "op": "set", "value": 5, "titles": [{"id": "a"}]},  # unknown to this version: ignored
        {"op": "reattach", "ids": {"c": "c2"}},
    ]
    edits = curation.Edits(lines)
    assert edits.of(["a"]) == ["Knitting"] and edits.of(["b"]) == [] and edits.of(["c2"]) == ["Knitting"] and edits.of(["c"]) == []
    assert edits.of(["a", "c2"]) == ["Knitting"]  # a merged title's copies
    with pytest.raises(SystemExit):
        curation.tag_name("   ")


def test_tag_command(library, capsys):
    run = lambda *a: cli.main(["tag", *a, "--library", str(library)])  # noqa: E731
    run("add", "Weird Fiction", "--folder", "collections/Poetry", "--yes")
    tagged = [i for i in records(library) if i.get("tg")]
    assert [i["tg"] for i in tagged] == [["Weird Fiction"]] and "Poetry" in tagged[0]["f"][0][0]
    assert not (library / "catalog" / "tags.js").exists()  # no copy for the page to fall behind; the server lays them
    shown = json.loads(Catalog(library).page_script()[1].decode().removeprefix("window.CATALOG = ").removesuffix(";\n"))
    assert [i["tg"] for i in shown["items"] if i.get("tg")] == [["Weird Fiction"]]  # what the page gets
    capsys.readouterr()
    run("add", "weird fiction", "--folder", "collections/Poetry", "--yes")
    assert "Nothing to do" in capsys.readouterr().out  # tags match without regard to case
    run("list")
    assert "1  Weird Fiction" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        run("add", "Everything", "--yes")  # adding needs a choice of titles
    run("remove", "Weird Fiction", "--yes")  # removing without a choice: from every title that has it
    assert not any(i.get("tg") for i in records(library))
    assert [e["op"] for e in curation.load_lines(library)] == ["add", "remove"] and not (curation.folder(library) / "edits.jsonl").exists()


def test_a_choice_that_finds_no_title_says_so_and_isn_t_a_success(library, capsys):
    """A mistyped id or a search that finds nothing isn't "all of them have it already", with code 0: it says no title
    matches, naming the ids the catalog hasn't, and ends with code 2, nothing changed."""
    before = curation.load_lines(library)
    for words, said in ((["tag", "add", "scifi", "--search", "zzzznotitle"], 'No title matches --search "zzzznotitle"'),
                        (["set", "kind", "comic", "--id", "nosuch"], "the catalog has no title with the id nosuch"),
                        (["tag", "remove", "scifi", "--id", "nosuch", "other"], "no title with the id nosuch, other")):
        with pytest.raises(SystemExit) as e:
            cli.main([*words, "--yes", "--library", str(library)])
        assert e.value.code == exits.USAGE and said in capsys.readouterr().err, words
    assert curation.load_lines(library) == before
    cli.main(["tag", "add", "Knitting", "--id", "h:novel", "nosuch", "--yes", "--library", str(library)])  # one of them is there
    said = capsys.readouterr()
    assert "Added 'Knitting' to 1 title." in said.out and "no title with the id nosuch: left out" in said.err


def test_hylly_tag_counts_one_title_as_one_and_lists_the_first_fifteen_suggestions(library, capsys, monkeypatch):
    """Each count with its word, one or many (words.many): "1 title", never "1 titles", in a list, a change and its
    question alike; a run's suggestions, the first 15 with how many more, as every list of the commands says it."""
    novel = ["--id", "h:novel", "--library", str(library)]
    cli.main(["tag", "add", "Knitting", *novel, "--yes"])
    assert "Added 'Knitting' to 1 title." in capsys.readouterr().out
    cli.main(["tag", "list", "Knitting", "--library", str(library)])
    assert capsys.readouterr().out.startswith("1 title tagged Knitting:\n    ")
    cli.main(["tag", "add", "knitting", *novel])
    assert capsys.readouterr().out.startswith("Nothing to do: 1 title chosen, and all of them have the tag already.")
    monkeypatch.setattr(cli, "at_keyboard", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    cli.main(["tag", "remove", "Knitting", *novel])
    said = capsys.readouterr()
    assert "Remove the tag 'Knitting' from this title? [y/N]" in said.err and said.out.endswith("Nothing changed.\n")
    two = [i["id"] for i in records(library) if i["id"] != "h:novel"][:1] + ["h:novel"]
    cli.main(["tag", "add", "Knitting", "--id", *two, "--library", str(library), "--dry-run"])
    assert "(1 chosen has it already)" in capsys.readouterr().out
    found = [{"t": f"Title {i}", "tags": [{"tag": "Moon", "why": "moon"}]} for i in range(17)]
    for n, counts in ((1, "1 suggestion for 1 title: Moon 1"), (17, "17 suggestions for 17 titles: Moon 17")):
        monkeypatch.setattr(curation, "find_suggestions", lambda root, items, n=n: found[:n])
        cli.main(["tag", "suggest", "--library", str(library)])
        lines = capsys.readouterr().out.splitlines()
        assert lines[1] == counts and lines[2] == "    Title 0: Moon (moon)"
    assert lines[16:18] == ["    Title 14: Moon (moon)", "  ... and 2 more"] and len(lines) == 19


def test_tags_follow_a_moved_file_and_wait_for_a_removed_one(library):
    poem = next(i for i in records(library) if "Poetry" in i["f"][0][0])
    novel = next(i for i in records(library) if i["id"] == "h:novel")
    sb = next(i for i in records(library) if i["src"] == "storybundle")
    curation.change(library, "add", "Keep", [poem, sb], by="test")
    moved = library / "collections" / "Strange Verse"
    shutil.move(library / "collections" / "Poetry", moved)  # a new path: a new id for the folder title
    catalog.build(library, log=lambda s: None)
    now = next(i for i in records(library) if "Strange%20Verse" in i["f"][0][0])
    assert now["id"] != poem["id"] and now["tg"] == ["Keep"]
    assert next(i for i in records(library) if i["id"] == sb["id"])["tg"] == ["Keep"]  # shop ids never change
    assert not novel.get("tg")
    shutil.rmtree(moved)
    log = []
    catalog.build(library, log=log.append)
    assert any("no longer in the library are kept" in x for x in log)
    assert curation.load(library).of([now["id"]]) == ["Keep"]  # still in the file, for when it returns


def test_a_merged_copy_keeps_its_tags(library):
    copy = library / "collections" / "Ellery" / "The Novel - Marlow Ellery.epub"
    copy.parent.mkdir(parents=True)
    make_epub(copy, OPF.replace("The Long Dust", "The Novel"))
    catalog.build(library, log=lambda s: None)
    novel = next(i for i in records(library) if i["id"] == "h:novel")
    assert len(novel["ids"]) == 1 and novel["ids"][0].startswith("f:")
    curation.change(library, "add", "Ellery", [{"id": novel["ids"][0], "t": "The Novel", "f": [["collections/x.epub"]]}], by="test")
    assert next(i for i in records(library) if i["id"] == "h:novel")["tg"] == ["Ellery"]


# ---- what is taken away from a merged title goes from every id it is read by ----------------------------------------

def tag_line(op, value, *ids, **more):
    return {"field": "tag", "op": op, "value": value, "titles": [{"id": i, "t": "The Quiet Shore", "f": f"shelf/{i}.epub"} for i in ids],
            **more}


def fix_line(field, op, value, *ids, **more):
    return {"field": field, "op": op, **({} if op == "clear" else {"value": value}), "titles": [{"id": i} for i in ids], **more}


SHORE = ["h:lead", "f:copy"]  # a title bought at a shop, with the copy that stood alone in a folder of yours merged under it
BEFORE = [  # as edits were written before: on a title's own id only, whatever its copies
    tag_line("add", "Knitting", "f:copy"), fix_line("kind", "set", "comic", "f:copy"), fix_line("title", "set", "Shore", "f:copy"),
    tag_line("decline", "Crochet", "f:copy"), tag_line("decline", "Lantern Wolf", "f:copy"),  # the copy on its own, then merged
    tag_line("remove", "Knitting", "h:lead"), fix_line("kind", "clear", None, "h:lead"), tag_line("undecline", "Crochet", "h:lead"),
    tag_line("add", "Moonwake", "h:lead"), fix_line("ereader", "set", "never", "h:lead"), fix_line("year", "set", "soon", "h:lead"),
]


def test_edits_made_before_replay_as_they_always_did_on_a_merged_title():
    edits = curation.Edits(BEFORE)
    assert {i: edits.of([i]) for i in SHORE} == {"f:copy": ["Knitting"], "h:lead": ["Moonwake"]} and edits.of(SHORE) == ["Knitting", "Moonwake"]
    assert edits.fixed == {"f:copy": {"kind": "comic", "title": "Shore"}, "h:lead": {"ereader": "never"}}
    assert edits.fixes_of(SHORE) == {"kind": "comic", "title": "Shore", "ereader": "never"}  # "soon" is no year: ignored
    assert edits.declined == {"f:copy": {"crochet": None, "lantern wolf": None}, "h:lead": {}}
    assert edits.decided == {"f:copy": {"knitting", "crochet", "lantern wolf"}, "h:lead": {"knitting", "moonwake"}}
    assert edits.seen["f:copy"] == {"id": "f:copy", "t": "The Quiet Shore", "f": "shelf/f:copy.epub"}


def test_a_removal_a_no_taken_back_and_a_correction_cleared_go_from_every_id_of_a_merged_title():
    copies = {"copies": {"h:lead": ["f:copy"]}}  # as change and change_single write them now
    edits = curation.Edits([*BEFORE, tag_line("remove", "Knitting", "h:lead", **copies),
                            fix_line("kind", "clear", None, "h:lead", **copies), fix_line("title", "clear", None, "h:lead", **copies),
                            tag_line("undecline", "Crochet", "h:lead", **copies), tag_line("add", "Lantern Wolf", "h:lead", **copies)])
    assert edits.of(SHORE) == ["Lantern Wolf", "Moonwake"] and {i: edits.of([i]) for i in SHORE} == {"h:lead": ["Lantern Wolf", "Moonwake"], "f:copy": []}
    assert edits.fixes_of(SHORE) == {"ereader": "never"}
    assert edits.declined_of(SHORE) == set()  # an added tag overrides the copy's no too
    assert edits.decided_of(SHORE) == {"knitting", "moonwake", "lantern wolf"}  # Crochet: asked again
    given = curation.Edits([*BEFORE, fix_line("kind", "set", "tech", "h:lead", **copies)])
    assert given.fixed["f:copy"]["kind"] == "comic" and given.fixes_of(SHORE)["kind"] == "tech"  # what is given goes on the lead
    unreadable = curation.Edits([*BEFORE, tag_line("remove", "Knitting", "h:lead", copies=["f:copy"])])
    assert unreadable.of(SHORE) == ["Knitting", "Moonwake"]  # copies not as written: the title's own id only, as before


def merged_copy(library):
    """The Novel with a copy of it in a folder of yours, merged under the shop's: the title, and the copy as a title of its
    own, as it was before (for a change made under its id then)."""
    copy = library / "collections" / "Ellery" / "The Novel - Marlow Ellery.epub"
    copy.parent.mkdir(parents=True)
    make_epub(copy, OPF.replace("The Long Dust", "The Novel"))
    catalog.build(library, log=lambda s: None)
    novel = next(i for i in records(library) if i["id"] == "h:novel")
    (copy_id,) = novel["ids"]
    alone = {"id": copy_id, "t": "The Novel", "f": [["collections/Ellery/The%20Novel%20-%20Marlow%20Ellery.epub"]]}
    curation.change(library, "add", "Ellery", [alone], by="test")  # a tag and a kind given to the copy when it stood alone
    curation.change_single(library, "kind", "comic", [alone], by="test")
    novel = next(i for i in records(library) if i["id"] == "h:novel")
    assert novel["tg"] == ["Ellery"] and novel["k"] == "comic"
    return novel, copy_id


def test_a_tag_and_kind_first_given_to_a_merged_copy_can_be_removed_and_cleared_from_the_command_line(library, capsys):
    _, copy_id = merged_copy(library)
    run = lambda *a: cli.main([*a, "--id", "h:novel", "--yes", "--library", str(library)])  # noqa: E731
    run("tag", "remove", "Ellery")
    run("set", "kind", "auto")
    novel = next(i for i in records(library) if i["id"] == "h:novel")
    assert not novel.get("tg") and novel["k"] == "book"
    assert [(e["op"], e.get("copies")) for e in curation.load_lines(library)[-2:]] == [("remove", {"h:novel": [copy_id]}),
                                                                                    ("clear", {"h:novel": [copy_id]})]
    assert not (library / "catalog" / "tags.js").exists()
    latest = curation.history(library)[0]  # what the page's Recent changes shows, and its Undo puts back
    assert (latest["by"], latest["ids"], latest["count"], latest["was"]) == ("command line", ["h:novel"], 1, {"h:novel": "comic"})
    capsys.readouterr()
    run("tag", "remove", "Ellery")
    run("set", "kind", "auto")  # once is enough: no more edits on trying again
    assert capsys.readouterr().out.count("Nothing to do") == 2 and len(curation.load_lines(library)) == 4


def test_a_tag_and_kind_first_given_to_a_merged_copy_can_be_removed_and_cleared_from_the_page_and_undone(served):
    client, srv, library = served
    merged_copy(library)
    key = {"X-Server-Key": client.get("/api/session").json()["key"]}  # the owner's own
    tag = lambda op: client.post("/api/tags", json={"op": op, "tag": "Ellery", "ids": ["h:novel"]}, headers=key).json()  # noqa: E731
    kind = lambda value: client.post("/api/fix", json={"field": "kind", "value": value, "ids": ["h:novel"]},  # noqa: E731
                                     headers=key).json()
    assert tag("remove") == {"ok": True, "op": "remove", "tag": "Ellery", "changed": ["h:novel"], "tags": {"h:novel": []}}
    cleared = kind("auto")
    assert cleared["was"] == {"h:novel": "comic"} and cleared["items"]["h:novel"]["title"]["k"] == "book"
    assert "fx" not in cleared["items"]["h:novel"]["title"]  # nothing of it corrected now
    latest = client.get("/api/edits").json()["edits"]
    assert [(e["op"], e["ids"], e["count"]) for e in latest[:2]] == [("clear", ["h:novel"], 1), ("remove", ["h:novel"], 1)]
    assert tag("remove")["changed"] == [] and kind("auto")["changed"] == []  # gone for good
    assert tag("add")["tags"] == {"h:novel": ["Ellery"]}  # what Undo sends: the opposite change, and the kind it had
    assert kind(cleared["was"]["h:novel"])["items"]["h:novel"]["title"]["k"] == "comic"
    assert next(i for i in records(library) if i["id"] == "h:novel")["tg"] == ["Ellery"]


def test_changes_from_the_command_line_are_never_a_librarian_s_named_as_this_computer_s_account(library, monkeypatch):
    from hylly import access

    monkeypatch.setattr(cli.getpass, "getuser", lambda: "reader")  # the account a terminal runs as, named as a librarian is
    access.add_user("reader", "correct horse", role="librarian")
    cli.main(["tag", "add", "Knitting", "--id", "h:novel", "--yes", "--library", str(library)])
    cli.main(["set", "kind", "tech", "--id", "h:novel", "--yes", "--library", str(library)])
    assert [e["by"] for e in curation.load_lines(library)] == ["command line", "command line"]
    srv, _ = serve.make_server(library, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}", auth=("reader", "correct horse"),
                          headers={"Host": "192.168.1.5:8770"}) as client:
            assert client.get("/api/edits").json() == {"edits": []}  # a librarian's own changes only
    finally:
        srv.shutdown()


# ---- a title whose id changed: only to a title new in the build, by its file, or one of the same name and size -----------

def comic(path, pages=1):
    """A CBZ of so many pages: their number tells same-named issues apart by size, so that they aren't one book's copies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for n in range(pages):
            z.writestr(f"{n:02}.jpg", png())
    return path


def issue(root, folder):
    """The title whose first file is in comics/<folder>/, with your tags and corrections on it."""
    return next(i for i in records(root) if unquote(i["f"][0][0]).startswith(f"comics/{folder}/"))


def test_tags_and_corrections_follow_a_renamed_folder_not_another_folder_s_file_of_the_same_name(library):
    comic(library / "comics" / "Lantern Wolf" / "001.cbz", pages=1)
    comic(library / "comics" / "Rust Orchard" / "001.cbz", pages=2)  # the same bare title "001", and the same file name
    catalog.build(library, log=lambda s: None)
    wolf = issue(library, "Lantern Wolf")
    curation.change(library, "add", "Keep", [wolf], by="test")
    curation.change_single(library, "title", "Lantern Wolf 1", [wolf], by="test")
    shutil.move(library / "comics" / "Lantern Wolf", library / "comics" / "Lantern Wolf (2019)")
    said = []
    catalog.build(library, log=said.append)
    now = issue(library, "Lantern Wolf (2019)")
    assert now["id"] != wolf["id"] and now["tg"] == ["Keep"] and now["t"] == "Lantern Wolf 1"
    assert not issue(library, "Rust Orchard").get("tg") and issue(library, "Rust Orchard")["t"] != "Lantern Wolf 1"
    assert any("followed 1 title that moved" in line for line in said)


def test_a_file_of_the_same_name_tells_new_titles_apart_by_its_size(library):
    comic(library / "comics" / "Lantern Wolf" / "001.cbz", pages=1)
    catalog.build(library, log=lambda s: None)
    curation.change(library, "add", "Keep", [issue(library, "Lantern Wolf")], by="test")
    shutil.move(library / "comics" / "Lantern Wolf", library / "comics" / "Lantern Wolf (2019)")
    comic(library / "comics" / "Neon Dusk" / "001.cbz", pages=3)  # new in the same build, with a file of that name
    catalog.build(library, log=lambda s: None)
    assert issue(library, "Lantern Wolf (2019)")["tg"] == ["Keep"] and not issue(library, "Neon Dusk").get("tg")


def test_a_folder_away_for_one_build_keeps_its_tags_off_other_titles_and_has_them_again_when_back(library):
    comic(library / "comics" / "Lantern Wolf" / "001.cbz", pages=1)
    comic(library / "comics" / "Rust Orchard" / "001.cbz", pages=2)
    catalog.build(library, log=lambda s: None)
    wolf = issue(library, "Lantern Wolf")
    curation.change(library, "add", "Keep", [wolf], by="test")
    curation.change_single(library, "title", "Lantern Wolf 1", [wolf], by="test")
    away = library / "_away"
    shutil.move(library / "comics" / "Lantern Wolf", away)  # its disk not mounted for one build
    said = []
    catalog.build(library, log=said.append)
    orchard = issue(library, "Rust Orchard")
    assert not orchard.get("tg") and orchard["t"] != "Lantern Wolf 1"  # the only other 001.cbz, but it was there before
    told = "\n".join(said)  # a short count, and the place question that asks where it went
    assert ("Your tags and corrections of 1 title no longer in the library are kept, should it return: hylly decide asks "
            "where it went.") in told and "1 question to decide (new): hylly decide lists them" in told
    (q,) = [q for q in curation.questions(library) if q["kind"] == "place"]
    assert (q["catalog_id"], q["options"][q["guess"]]["answer"]) == (wolf["id"], "keep")  # the guess: keep waiting
    assert "tags Keep; your title" in q["why"] and "comics/Lantern Wolf/001.cbz" in q["why"]
    shutil.move(away, library / "comics" / "Lantern Wolf")
    catalog.build(library, log=lambda s: None)
    assert not [q for q in curation.questions(library) if q["kind"] == "place"]  # back: found no more, so gone
    back = issue(library, "Lantern Wolf")
    assert back["id"] == wolf["id"] and back["tg"] == ["Keep"] and back["t"] == "Lantern Wolf 1"
    assert not issue(library, "Rust Orchard").get("tg")


def shore(path):
    """A copy of one book, The Quiet Shore: copies of it in several of your folders are one title, and the copy in the
    folder first by name leads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    make_epub(path, OPF.replace("The Long Dust", "The Quiet Shore"))
    return path


def the_shore(root):
    return next(i for i in records(root) if i["t"] == "The Quiet Shore")


def test_tags_stay_on_a_title_when_its_leading_copy_goes(library):
    lead = shore(library / "collections" / "Shore" / "The Quiet Shore - Marlow Ellery.epub")
    shore(library / "shelf" / "The Quiet Shore - Marlow Ellery.epub")
    catalog.build(library, log=lambda s: None)
    both = the_shore(library)
    assert unquote(both["f"][0][0]).startswith("collections/") and len(both["ids"]) == 1
    curation.change(library, "add", "Keep", [both], by="test")  # on the leading copy's id
    lead.unlink()
    said = []
    catalog.build(library, log=said.append)
    now = the_shore(library)
    assert now["id"] == both["ids"][0] and now["tg"] == ["Keep"] and not now.get("ids")
    assert not any("no longer in the library" in line for line in said)


def test_tags_of_a_copy_on_its_own_stay_when_a_copy_that_leads_came_and_it_went(library):
    alone = shore(library / "shelf" / "The Quiet Shore - Marlow Ellery.epub")
    catalog.build(library, log=lambda s: None)
    curation.change(library, "add", "Keep", [the_shore(library)], by="test")
    shore(library / "collections" / "Shore" / "The Quiet Shore - Marlow Ellery.epub")  # a better copy, later: it leads
    catalog.build(library, log=lambda s: None)
    both = the_shore(library)
    assert unquote(both["f"][0][0]).startswith("collections/") and both["tg"] == ["Keep"]
    alone.unlink()  # the strays reconciled: the first copy goes
    said = []
    catalog.build(library, log=said.append)
    now = the_shore(library)
    assert now["id"] == both["id"] and now["tg"] == ["Keep"] and not now.get("ids")
    assert not any("no longer in the library" in line for line in said)


def test_tags_wait_when_the_copy_left_has_joined_another_book(library):
    def copy(folder, author):  # The Quiet Shore, by author, or by nobody it says
        path = library / folder / "The Quiet Shore.epub"
        path.parent.mkdir(parents=True, exist_ok=True)
        opf = OPF.replace("The Long Dust", "The Quiet Shore").replace('<dc:creator opf:role="ill">Some Illustrator</dc:creator>', "")
        make_epub(path, opf.replace("Marlow Ellery", author) if author else
                  opf.replace('<dc:creator opf:role="aut">Marlow Ellery</dc:creator>', ""))
        return path

    smith = copy("collections/Shore", "Ann Smith")  # leads: first by name
    copy("shelf", "")  # no author: it joins the first title it may
    copy("stacks", "Bo Jones")  # another author: a book of its own
    catalog.build(library, log=lambda s: None)
    shores = [i for i in records(library) if i["t"] == "The Quiet Shore"]
    (tagged,) = [i for i in shores if unquote(i["f"][0][0]).startswith("collections/")]
    assert len(shores) == 2 and len(tagged["ids"]) == 1 and len(tagged["f"]) == 2
    curation.change(library, "add", "Keep", [tagged], by="test")
    smith.unlink()
    said = []
    catalog.build(library, log=said.append)
    (now,) = [i for i in records(library) if i["t"] == "The Quiet Shore"]  # the copy without an author went to Jones's
    assert any(unquote(f[0]).startswith("stacks/") for f in now["f"]) and not now.get("tg")
    assert "The Quiet Shore (collections/Shore/The Quiet Shore.epub): tags Keep" in waiting(library)  # a place question


def test_a_build_moves_kept_things_only_to_a_title_new_in_it_by_its_file_or_one_of_the_same_name_and_size():
    before = matching.Before({"f:gone": [("comics/A/001.cbz", 100)], "f:there": [("comics/B/001.cbz", 100)],
                              "f:apart": [(unicodedata.normalize("NFD", "comics/Pää/001.cbz"), 7)]})
    there = {"id": "f:there", "t": "001", "f": [["comics/B/001.cbz", "cbz", 100]]}
    assert matching.Titles([there], before).follow("f:gone") is None  # the same name and size, but already there
    new = {"id": "f:new", "t": "001", "f": [["comics/A%20(2019)/001.cbz", "cbz", 100]]}
    assert matching.Titles([there, new], before).follow("f:gone") == "f:new"
    twin = {"id": "f:twin", "t": "001", "f": [["comics/C/001.cbz", "cbz", 100]]}
    assert matching.Titles([there, new, twin], before).follow("f:gone") is None  # two new ones alike: no answer
    copy = {"id": "h:lead", "ids": ["f:copy"], "t": "001", "f": [["humble/x/001.cbz", "cbz", 9], ["comics/A2/001.cbz", "cbz", 100]]}
    before.files["h:lead"] = [("humble/x/001.cbz", 9)]
    assert matching.Titles([there, copy], before).follow("f:gone") == "h:lead"  # a new copy merged into a title there
    composed = {"id": "f:composed", "t": "Pää", "f": [["comics/P%C3%A4%C3%A4/001.cbz", "cbz", 7]]}
    assert matching.Titles([composed], before).follow("f:apart") == "f:composed"  # the same file, in the other form
    assert matching.Titles([new], before).follow("f:unknown", {"f": "comics/a (2019)/001.CBZ"}) == "f:new"  # as an edit saw it
    assert matching.Titles([there], before).find({"t": "001", "f": "comics/A/001.cbz"}) == "f:there"  # a person's: by name
    pair = {"f:a": {"f:a", "f:b"}, "f:b": {"f:a", "f:b"}}  # one title last time, of two copies
    left = {"id": "f:b", "t": "A", "f": [["y/a.epub", "epub", 1]]}
    assert matching.Titles([left], matching.Before({"f:a": [], "f:b": []}, pair)).follow("f:a") == "f:b"  # there, but the same title


def test_a_name_written_with_its_accents_apart_is_the_same_title(library):
    composed = comic(library / "comics" / "Pääkallo" / "Pääkallo 001.cbz")
    catalog.build(library, log=lambda s: None)
    title = issue(library, "Pääkallo")
    curation.change(library, "add", "Keep", [title], by="test")
    people.Person("rory").place(curation.snapshot(title), 12, 0.25)
    composed.rename(composed.with_name(unicodedata.normalize("NFD", composed.name)))  # as a Mac-made disk gives it back
    said = []
    catalog.build(library, log=said.append)
    now = issue(library, "Pääkallo")
    assert unquote(now["f"][0][0]).endswith(unicodedata.normalize("NFD", "Pääkallo 001.cbz"))  # the file as it is on the disk
    assert now["id"] == title["id"] and now["tg"] == ["Keep"] and not any("new title" in line for line in said)
    place = people.Person("rory").latest(matching.Titles(records(library)))[title["id"]]
    assert place["at"] == 12 and "gone" not in place


def test_a_decomposed_name_keeps_its_tag_first_day_and_place_through_the_first_build_in_one_form(library, monkeypatch):
    def as_before(rel):  # the id as builds made it before names were put in one form
        return "f:" + hashlib.sha1(rel.lower().encode()).hexdigest()[:12]
    name = unicodedata.normalize("NFD", "Pää 01.cbz")
    comic(library / "comics" / "Pää" / name, pages=1)
    comic(library / "comics" / "Pää Extra" / name, pages=2)  # the same name: only its folder tells them apart
    with monkeypatch.context() as earlier:
        earlier.setattr(catalog, "folder_id", as_before)
        catalog.build(library, log=lambda s: None)  # they came in this build: new, with the day
    old = issue(library, "Pää")
    assert old["id"] == as_before(unquote(old["f"][0][0])) != catalog.folder_id(unquote(old["f"][0][0])) and old["nw"]
    curation.change(library, "add", "Keep", [old], by="test")
    people.Person("rory").place(curation.snapshot(old), 12, 0.25)
    said = []
    catalog.build(library, log=said.append)  # the first build that puts names in one form: a new id
    now = issue(library, "Pää")
    assert now["id"] != old["id"] and now["tg"] == ["Keep"] and now["nw"] == old["nw"]
    assert not issue(library, "Pää Extra").get("tg") and not any("new title" in line for line in said)
    found = people.Person("rory").latest(matching.Titles(records(library)))
    assert found[now["id"]]["at"] == 12 and "gone" not in found[now["id"]] and old["id"] not in found


# ---- names compared in any script and files in one order on every system: the first build after ----------------------

def as_before(stem: str) -> str:
    """Which files were one title before: only a to z and 0 to 9 of their names counted."""
    return re.sub(r"[^a-z0-9]+", "", stem.lower())


def in_folder(root, folder: str) -> dict[str, dict]:
    """The titles in comics/<folder>/, by the name of their first file."""
    return {Path(unquote(i["f"][0][0])).name: i for i in records(root) if unquote(i["f"][0][0]).startswith(f"comics/{folder}/")}


def test_a_title_whose_first_file_changed_with_the_one_order_keeps_its_tag_day_and_place(library, monkeypatch):
    comic(library / "comics" / "Moonwake" / "Moonwake 01.cbz")
    make_epub(library / "comics" / "Moonwake" / "MOONWAKE 01.epub", OPF.replace("The Long Dust", "Moonwake 01"))
    with monkeypatch.context() as earlier:
        earlier.setattr(catalog, "path_order", lambda rel: rel)  # by code point, as Linux and macOS listed them before
        catalog.build(library, log=lambda s: None)
    old = issue(library, "Moonwake")
    assert old["id"] == catalog.folder_id("comics/Moonwake/MOONWAKE 01.epub") and len(old["f"]) == 2 and old["nw"]
    curation.change(library, "add", "Keep", [old], by="test")
    people.Person("rory").place(curation.snapshot(old), 12, 0.25)
    said = []
    catalog.build(library, log=said.append)  # in the one order: Moonwake 01.cbz leads, and gives the title a new id
    now = issue(library, "Moonwake")
    assert now["id"] == catalog.folder_id("comics/Moonwake/Moonwake 01.cbz") != old["id"]
    assert now["tg"] == ["Keep"] and now["nw"] == old["nw"] and not any("new title" in line for line in said)
    found = people.Person("rory").latest(matching.Titles(records(library)))
    assert found[now["id"]]["at"] == 12 and "gone" not in found[now["id"]] and old["id"] not in found


def test_a_title_that_was_two_books_keeps_its_tag_on_the_one_that_led_it(library, monkeypatch):
    comic(library / "comics" / "Полка" / "Книга 1.cbz", pages=1)
    comic(library / "comics" / "Полка" / "Другая 1.cbz", pages=2)
    with monkeypatch.context() as earlier:
        earlier.setattr(catalog, "_stem_key", as_before)  # both were "1": one title of two files
        catalog.build(library, log=lambda s: None)
    (both,) = in_folder(library, "Полка").values()
    assert len(both["f"]) == 2 and both["id"] == catalog.folder_id("comics/Полка/Другая 1.cbz")
    curation.change(library, "add", "Keep", [both], by="test")
    said = []
    catalog.build(library, log=said.append)
    now = in_folder(library, "Полка")
    assert now["Другая 1.cbz"]["id"] == both["id"] and now["Другая 1.cbz"]["tg"] == ["Keep"] and not now["Книга 1.cbz"].get("tg")
    assert not any("no longer in the library" in line for line in said)


def test_two_titles_that_are_one_book_now_keep_the_leading_one_s_tag_and_the_other_s_waits(library, monkeypatch):
    apart = "Cafe\u0301 Lantern 01.cbz"  # its accent apart, as a Mac-made disk writes it: the same name as the EPUB's
    comic(library / "comics" / "Lanterns" / apart)
    make_epub(library / "comics" / "Lanterns" / "Café Lantern 01.epub", OPF.replace("The Long Dust", "Café Lantern"))
    with monkeypatch.context() as earlier:
        earlier.setattr(catalog, "_stem_key", as_before)  # "cafelantern01" and "caflantern01": two titles
        catalog.build(library, log=lambda s: None)
    two = in_folder(library, "Lanterns")
    curation.change(library, "add", "Keep", [two[apart]], by="test")
    curation.change(library, "add", "Lanterns", [two["Café Lantern 01.epub"]], by="test")
    said = []
    catalog.build(library, log=said.append)
    (one,) = in_folder(library, "Lanterns").values()
    assert len(one["f"]) == 2 and one["id"] == two[apart]["id"] and one["tg"] == ["Keep"]
    told = "\n".join(said)  # the other's tag: kept, and a place question asks where it went
    assert "no longer in the library are kept" in told and "(comics/Lanterns/Café Lantern 01.epub): tags Lanterns" in waiting(library)
    assert curation.load(library).of([two["Café Lantern 01.epub"]["id"]]) == ["Lanterns"]


def test_tags_in_search_and_feeds(library, log_in):
    novel = next(i for i in records(library) if i["id"] == "h:novel")
    curation.change(library, "add", "Knitting", [novel], by="test")
    srv, _ = serve.make_server(library, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")) as client:
            for base in ("/opds", "/ereader/opds"):
                nav = [e.find(f"{ATOM}title").text for e in ET.fromstring(client.get(base).text).findall(f"{ATOM}entry")]
                assert "By tag" in nav
                tagged = ET.fromstring(client.get(f"{base}/tag/Knitting").text).findall(f"{ATOM}entry")
                assert [e.find(f"{ATOM}title").text for e in tagged] == ["The Novel"]
            found = ET.fromstring(client.get("/opds/search", params={"q": "knitting"}).text).findall(f"{ATOM}entry")
            assert [e.find(f"{ATOM}title").text for e in found] == ["The Novel"]
            info = ET.fromstring(client.get("/ereader/opds/tag/Knitting").text).find(f"{ATOM}entry").find(f"{ATOM}summary").text
            assert "Tags: Knitting" in info
            curation.change(library, "remove", "Knitting", [novel], by="test")  # the server notices at once
            assert not ET.fromstring(client.get("/opds/search", params={"q": "knitting"}).text).findall(f"{ATOM}entry")
    finally:
        srv.shutdown()


@pytest.fixture
def served(library, serving):
    srv, client = serving(library)
    return client, srv, library


def test_editing_from_the_page(served, log_in):
    client, srv, library = served
    session = client.get("/api/session").json()
    assert session["edit"] and session["by"] == "owner"
    assert session["key"] == hmac.new(srv.RequestHandlerClass.editors_secret, b"owner", hashlib.sha256).hexdigest()  # theirs
    key, origin = {"X-Server-Key": session["key"]}, {"Origin": f"http://127.0.0.1:{srv.server_port}"}
    change = {"op": "add", "tag": "Knitting", "ids": ["h:novel"]}
    assert client.post("/api/tags", json=change).status_code == 403  # no key: not from the page
    assert client.post("/api/tags", json=change, headers={**key, "Origin": "https://elsewhere.example"}).status_code == 403
    assert client.post("/api/tags", json={**change, "op": "rename"}, headers=key).status_code == 400
    assert client.post("/api/tags", json={**change, "tag": "  "}, headers=key).status_code == 400
    done = client.post("/api/tags", json=change, headers={**key, **origin}).json()
    assert done == {"ok": True, "op": "add", "tag": "Knitting", "changed": ["h:novel"], "tags": {"h:novel": ["Knitting"]}}
    assert '"tg":["Knitting"]' in client.get("/catalog/data.js").text and not (library / "catalog" / "tags.js").exists()
    latest = client.get("/api/edits").json()["edits"][0]
    assert latest["op"] == "add" and latest["tag"] == "Knitting" and latest["ids"] == ["h:novel"] and latest["by"] == "owner"
    undone = client.post("/api/tags", json={**change, "op": "remove"}, headers=key).json()  # what Undo sends
    assert undone["changed"] == ["h:novel"] and undone["tags"] == {"h:novel": []}
    other = {"Host": "192.168.1.5:8770"}  # a reader at another device at home: reads, but may not change anything
    with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}", headers=other), "reader") as reader:
        seen = reader.get("/api/session").json()
        assert {k: seen[k] for k in ("edit", "by", "key", "suggestions")} == {"edit": False, "by": "reader", "key": None, "suggestions": 0}
        assert "h:novel" in seen["ereader"]  # which titles the e-reader feed lists: for the page's filter, anyone may read it
        assert reader.post("/api/tags", json=change, headers=key).status_code == 403
        refused = reader.get("/api/edits")  # not for a reader: told so, as the page reads every refusal
        assert refused.status_code == 403 and refused.json() == {
            "ok": False, "error": "Recent changes are for the library's owners and librarians."}


def test_each_editor_s_key_is_their_own_so_a_page_left_open_for_one_changes_nothing_as_another(library, serving):
    """The editors' key for the library's changes (X-Server-Key) was one for the whole server, as the key for one's own
    writes once was: a page left open for lucy, a librarian, with owen logged in since in that browser, sent its tags
    and corrections with owen's login, and they were made as his. Each editor's key is their own now, their name signed
    with the start's secret: lucy's page gets 403 (and, asking /api/session, finds owen there and starts afresh as
    his), and nothing changes; owen's own page changes as him. A restart makes every key anew."""
    from hylly import access

    access.add_user("owen", "correct horse", role="owner")
    access.add_user("lucy", "correct horse", role="librarian")
    srv, _ = serving(library, login=None)
    tag, year = {"op": "add", "tag": "Knitting", "ids": ["h:novel"]}, {"field": "year", "value": "1999", "ids": ["h:novel"]}
    with logged_in(srv, "lucy") as browser:
        lucy = browser.get("/api/session").json()["key"]
        assert lucy and browser.get("/api/session").json()["key"] == lucy  # the same all through this start
        assert browser.post("/login", data={"username": "owen", "password": "correct horse", "next": "/"}).status_code == 303
        session = browser.get("/api/session").json()  # owen, in the same browser, as in another tab
        assert session["by"] == "owen" and session["key"] not in (None, lucy)
        before = curation.history(library)
        for path, body in (("/api/tags", tag), ("/api/fix", year)):
            refused = browser.post(path, json=body, headers={"X-Server-Key": lucy})  # lucy's page, with owen's login
            assert refused.status_code == 403 and refused.json() == {
                "ok": False, "error": "Not from the library page; reload it and try again."}
        assert curation.history(library) == before  # nothing as owen, nor as lucy
        assert browser.post("/api/tags", json=tag, headers={"X-Server-Key": session["key"]}).json()["changed"] == ["h:novel"]
        assert curation.history(library)[0]["by"] == "owen"  # owen's own page
    again, _ = serving(library, login=None)
    with logged_in(again, "owen") as browser:
        assert browser.get("/api/session").json()["key"] != session["key"]


EDITORS_PAGE = """
let ME = { by: "lucy", key: "p-lucy" }, EDIT = { on: true, key: "the start before's", by: "lucy" }, leaving = false;
let reloaded = 0, now = "lucy";
const location = { reload() { reloaded++; } }, SENT = [], KEYS = { lucy: "e-lucy", owen: "e-owen" };
const fetch = async (url, init = {}) => {  // the server: each editor's own key, for whoever is logged in now
  if (url === "/api/session") return { json: async () => ({ by: now, key: KEYS[now], me: { key: "p-" + now } }) };
  const sent = init.headers["X-Server-Key"], ok = sent === KEYS[now];
  SENT.push(sent);
  return { status: ok ? 200 : 403, json: async () => ok ? { ok: true } : { ok: false, error: "Not from the library page." } };
};
/* part 1 */
(async () => {
  const out = {};
  for (const [when, who] of [["after the deploy", "lucy"], ["owen logged in since", "owen"]]) {
    now = who;
    SENT.length = 0;
    const { r } = await sendKeyed("/api/tags", { op: "add", tag: "Knitting", ids: ["h:novel"] });
    out[when] = { status: r.status, sent: [...SENT], key: EDIT.key, reloaded, leaving };
  }
  console.log(JSON.stringify(out));
})();
"""


def test_an_editor_s_page_left_open_sends_again_with_its_own_key_or_starts_afresh_for_someone_else():
    """What an editor's page left open does with the editors' key each their own, as the library page has it
    (sendKeyed, keyRenewed): across a restart (an update's, say) its change gets one 403, the page asks /api/session
    for its key and sends the change again, once, and it's kept; with someone else logged in since in that browser it
    sends nothing more, and starts afresh as theirs."""
    out = in_the_page(EDITORS_PAGE, (r"// A change sent with one of the page's keys", r"async function postJson\("))
    assert out["after the deploy"] == {"status": 200, "sent": ["the start before's", "e-lucy"], "key": "e-lucy",
                                       "reloaded": 0, "leaving": False}
    assert out["owen logged in since"] == {"status": 403, "sent": ["e-lucy"], "key": "e-lucy", "reloaded": 1, "leaving": True}


def test_a_logged_in_librarian_may_edit(library):
    from hylly import access

    access.add_user("reader", "correct horse", role="librarian")
    srv, _ = serve.make_server(library, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}", auth=("reader", "correct horse"),
                          headers={"Host": "192.168.1.5:8770"}) as client:
            session = client.get("/api/session").json()
            assert session["edit"] and session["by"] == "reader"
            done = client.post("/api/tags", json={"op": "add", "tag": "Read", "ids": ["h:novel"]},
                               headers={"X-Server-Key": session["key"], "Origin": "http://192.168.1.5:8770"}).json()
            assert done["changed"] == ["h:novel"] and client.get("/api/edits").json()["edits"][0]["by"] == "reader"
    finally:
        srv.shutdown()


def test_declines_and_asking_again():
    tag = lambda op, value, i: {"field": "tag", "op": op, "value": value, "titles": [{"id": i}]}  # noqa: E731
    edits = curation.Edits([tag("decline", "Knitting", "a"), tag("undecline", "knitting", "a"),  # asked again
                            tag("decline", "Knitting", "b"), tag("add", "Knitting", "b"),  # changed my mind: adding wins
                            tag("add", "Crochet", "c"), tag("remove", "Crochet", "c")])
    assert edits.decided_of(["a"]) == set() and edits.declined_of(["a"]) == set()
    assert edits.of(["b"]) == ["Knitting"] and edits.declined_of(["b"]) == set()
    assert edits.decided_of(["c"]) == {"crochet"}  # a removed tag isn't suggested again either


RULES = """
["Video Games"]
keywords = ["video game", "rover"]

[Rangers]
keywords = ["ranger"]
not = ["neon"]

[Apocalypse]
keywords = ["apocalyptic"]
"""


def test_suggestions(library, capsys):
    curation.rules_file(library).parent.mkdir(parents=True, exist_ok=True)
    curation.rules_file(library).write_text(RULES, encoding="utf-8")
    recs = records(library)
    arcade = next(i for i in recs if i["src"] == "storybundle")
    manual = next(i for i in recs if i["k"] == "manual")
    wolves = [i for i in recs if "Lantern Wolf" in i["t"]]
    curation.change(library, "add", "Lantern Wolf", wolves[:1], by="test")  # a tag in use suggests itself by its name
    found = {s["id"]: s["tags"] for s in curation.find_suggestions(library, recs)}
    assert found[arcade["id"]] == [{"tag": "Video Games", "why": '"video game" in the bundle', "group": f"bundle: {arcade['b']}"}]
    assert found[manual["id"]][0]["why"] == '"rover" in the folder'
    assert not any(t["tag"] == "Rangers" for tags in found.values() for t in tags)  # "neon" says no
    assert not any(t["tag"] == "Apocalypse" for tags in found.values() for t in tags)  # only in a description
    assert found[wolves[1]["id"]][0]["tag"] == "Lantern Wolf" and wolves[0]["id"] not in found
    curation.rules_file(library).write_text(RULES.replace('keywords = ["apocalyptic"]', 'keywords = ["apocalyptic"]\ndescription = true'),
                                            encoding="utf-8")
    assert {"tag": "Apocalypse", "why": '"apocalyptic" in the description', "group": ""} in \
        {s["id"]: s["tags"] for s in curation.suggest(library, recs)}["h:novel"]
    index = {i: r for r in recs for i in curation.ids_of(r)}
    curation.change(library, "decline", "Video Games", [arcade], by="test")
    waiting = curation.pending(library, index)["items"]
    assert arcade["id"] not in [s["id"] for s in waiting] and manual["id"] in [s["id"] for s in waiting]
    assert curation.declined(library, index) == [{"id": arcade["id"], "t": arcade["t"], "tag": "Video Games"}]
    tags_for = lambda rec: [t["tag"] for s in curation.suggest(library, recs) if s["id"] == rec["id"] for t in s["tags"]]  # noqa: E731
    assert "Video Games" not in tags_for(arcade)  # a no is remembered
    curation.change(library, "undecline", "Video Games", [arcade], by="test")
    assert "Video Games" in tags_for(arcade)  # asked again
    capsys.readouterr()
    cli.main(["tag", "suggest", "--library", str(library)])
    out = capsys.readouterr().out
    assert "suggestions for" in out and "Video Games" in out and "tags.toml" in out


def test_a_keyword_in_any_script_suggests_its_tag(library):
    curation.rules_file(library).parent.mkdir(parents=True, exist_ok=True)
    curation.rules_file(library).write_text('["Гавани"]\nkeywords = ["гаван"]\n\n["月"]\nnot = ["第3巻"]\n', encoding="utf-8")
    recs = [{"id": n, "t": t, "f": [[f"shelf/{n}.epub"]]} for n, t in (("x1", "Тихая ГАВАНЬ"), ("x2", "月の庭 第2巻"),
                                                                      ("x3", "月の庭 第3巻"), ("x4", "Другая 1"))]
    found = {s["id"]: [(t["tag"], t["why"]) for t in s["tags"]] for s in curation.suggest(library, recs)}
    assert found == {"x1": [("Гавани", '"гаван" in the title')], "x2": [("月", '"月" in the title')]}


def test_a_mistake_in_tags_toml_names_the_file_the_tag_and_the_key(tmp_path):
    """tags.toml is read as a rules file is, and each tag's table checked: keywords = "knit", a text and no list, was
    read letter by letter and its tag suggested for nearly every title; a not or a description of the wrong kind went
    by unsaid; and a tags.toml that couldn't be read stopped hylly tag suggest with Python's own words."""
    tags = curation.rules_file(tmp_path)
    tags.parent.mkdir(parents=True)
    for written, said in (('[Knitting]\nkeywords = "knit"\n', r"\[Knitting\] keywords: a list of words in quotes"),
                          ('[Knitting]\nkeywords = ["knit", true]\n', r"\[Knitting\] keywords: a list of words in quotes"),
                          ('["Weird Fiction"]\nkeywords = ["weird"]\nnot = "weirdo"\n',
                           r'\["Weird Fiction"\] not: a list of words in quotes, such as \["knit", "purl"\]\.$'),
                          ('[Knitting]\nkeywords = ["knit"]\ndescription = "yes"\n', r"\[Knitting\] description: true or false\.$")):
        tags.write_text(written, encoding="utf-8")
        with pytest.raises(SystemExit, match=rf"^{re.escape(str(tags))}, {said}") as stopped:
            curation.load_rules(tmp_path, curation.Edits([]))
        assert stopped.value.code == exits.NOT_SET_UP
    tags.write_text('[Knitting]\nkeywords = ["knit"]\nnot = ["tightly knit"]\ndescription = true\n', encoding="utf-8")
    found = curation.load_rules(tmp_path, curation.Edits([]))
    assert [(r.tag, [w for w, _ in r.keywords], r.nots, r.description) for r in found] == [
        ("Knitting", ["Knitting", "knit"], [["tightly", "knit"]], True)]
    tags.write_text('[Year]\nkeywords = [1984, 2.5]\nnot = [1985]\n', encoding="utf-8")  # a number is a word, as always
    found = curation.load_rules(tmp_path, curation.Edits([]))
    assert [(r.tag, [w for w, _ in r.keywords], r.nots) for r in found] == [("Year", ["Year", "1984", "2.5"], [["1985"]])]
    recs = [{"id": "x1", "t": "Moonwake 1984", "f": [["shelf/x1.epub"]]}, {"id": "x2", "t": "Moonwake 1985", "f": [["shelf/x2.epub"]]}]
    assert [s["id"] for s in curation.suggest(tmp_path, recs)] == ["x1"]
    tags.unlink()
    tags.mkdir()  # there, but no file that can be read
    with pytest.raises(SystemExit, match=rf"^{re.escape(str(tags))}: can't be read \(.+\)\.$"):
        curation.load_rules(tmp_path, curation.Edits([]))


def test_a_first_run_writes_example_rules(library):
    assert not curation.rules_file(library).exists()
    curation.find_suggestions(library, records(library))
    assert curation.rules_file(library).read_text(encoding="utf-8") == curation.STARTER
    assert curation.pending(library, {})["items"] == [] and curation.pending(library, {})["made"]  # only examples, all commented


def test_reviewing_from_the_page(served, log_in):
    client, srv, library = served
    curation.rules_file(library).parent.mkdir(parents=True, exist_ok=True)
    curation.rules_file(library).write_text(RULES, encoding="utf-8")
    key = {"X-Server-Key": client.get("/api/session").json()["key"]}  # the owner's own
    assert client.post("/api/suggest").status_code == 403  # a change: it needs the page's key
    done = client.post("/api/suggest", headers=key).json()
    assert done["ok"] and done["titles"] == 2
    assert client.get("/api/session").json()["suggestions"] == 2
    first = client.get("/api/suggestions").json()["items"][0]
    no = client.post("/api/tags", json={"op": "decline", "tag": first["tags"][0]["tag"], "ids": [first["id"]]}, headers=key).json()
    assert no["changed"] == [first["id"]] and no["tags"] == {first["id"]: []}
    assert len(client.get("/api/suggestions").json()["items"]) == 1 and client.get("/api/session").json()["suggestions"] == 1
    assert client.get("/api/declined").json()["items"][0]["id"] == first["id"]
    assert client.get("/api/edits").json()["edits"][0]["op"] == "decline"
    with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}"), "librarian") as librarian:
        assert librarian.get("/api/suggestions").status_code == 403  # the owner's only


def test_corrections_replay_and_follow_a_move():
    fix = lambda field, op, value, i: {"field": field, "op": op, "value": value, "titles": [{"id": i}]}  # noqa: E731
    edits = curation.Edits([fix("kind", "set", "tech", "a"), fix("ereader", "set", "never", "a"), fix("kind", "set", "poem", "b"),
                            fix("ereader", "clear", None, "a"), {"op": "reattach", "ids": {"a": "a2"}}])
    assert edits.fixes_of(["a2"]) == {"kind": "tech"} and edits.fixes_of(["b"]) == {}  # "poem" is no kind: ignored
    assert curation.Edits([fix("kind", "set", "comic", "copy"), fix("kind", "set", "book", "lead")]).fixes_of(["lead", "copy"]) == {"kind": "book"}


def test_kind_and_ereader_feed_corrections(served):
    client, srv, library = served
    key = {"X-Server-Key": client.get("/api/session").json()["key"]}  # the owner's own
    manual = next(i for i in records(library) if i["k"] == "manual")
    listed = lambda: set(client.get("/api/session").json()["ereader"])  # noqa: E731
    assert "h:novel" in listed() and manual["id"] not in listed()
    fix = lambda field, value, i: client.post("/api/fix", json={"field": field, "value": value, "ids": [i]}, headers=key).json()  # noqa: E731
    never = fix("ereader", "never", "h:novel")
    assert never["changed"] == ["h:novel"] and never["was"] == {"h:novel": None} and never["items"]["h:novel"]["in_ereader"] is False
    assert "h:novel" not in listed() and not entries_of(client.get("/ereader/opds/search", params={"q": "novel"}).text)
    assert not fix("ereader", "always", manual["id"])["items"][manual["id"]]["in_ereader"]  # a PDF from your own folder: not by itself
    srv.RequestHandlerClass.ereader = Device(pdf_from=("any",))  # pdf_from widens it
    kind = fix("kind", "book", manual["id"])["items"][manual["id"]]
    assert kind["in_ereader"] is True and (kind["title"]["k"], kind["title"]["ereader"]) == ("book", "always")
    assert kind["title"]["fx"] == {"ereader": "always", "kind": "book"} and kind["title"]["o"] == {"k": "manual"}  # as found
    assert manual["t"] in [e.find(f"{ATOM}title").text for e in entries_of(client.get("/opds/kind/book").text)]
    assert fix("ereader", "auto", "h:novel")["was"] == {"h:novel": "never"} and "h:novel" in listed()  # what undo sends
    latest = client.get("/api/edits").json()["edits"][0]
    assert latest["field"] == "ereader" and latest["op"] == "clear" and latest["was"] == {"h:novel": "never"}
    assert client.post("/api/fix", json={"field": "kind", "value": "poem", "ids": ["h:novel"]}, headers=key).status_code == 400
    assert client.post("/api/fix", json={"field": "kind", "value": "book", "ids": ["h:novel"]}).status_code == 403
    shown = json.loads(client.get("/catalog/data.js").text.removeprefix("window.CATALOG = ").removesuffix(";\n"))
    assert {i["id"]: i["fx"] for i in shown["items"] if i.get("fx")} == {manual["id"]: {"kind": "book", "ereader": "always"}}


def test_set_command(library, capsys):
    cli.main(["set", "kind", "tech", "--search", "novel ellery", "--library", str(library), "--yes"])
    assert next(i for i in records(library) if i["id"] == "h:novel")["k"] == "tech"
    cli.main(["set", "kind", "auto", "--id", "h:novel", "--library", str(library), "--yes"])
    assert next(i for i in records(library) if i["id"] == "h:novel")["k"] == "book"
    with pytest.raises(SystemExit):
        cli.main(["set", "ereader", "maybe", "--id", "h:novel", "--library", str(library), "--yes"])
    cli.main(["set", "ereader", "never", "--id", "h:novel", "--library", str(library), "--yes"])
    assert curation.load(library).fixes_of(["h:novel"]).get("ereader") == "never"
    capsys.readouterr()
    manual = next(i for i in records(library) if i["k"] == "manual")
    cli.main(["set", "ereader", "always", "--id", manual["id"], "--library", str(library), "--yes"])
    assert "1 title still won't be listed" in capsys.readouterr().out  # a PDF from a folder of your own


def entries_of(xml_text: str) -> list:
    return ET.fromstring(xml_text).findall(f"{ATOM}entry")


def test_set_format(library, capsys):
    from hylly.download import Manifest

    cli.main(["set", "format", "pdf", "--id", "h:novel", "--library", str(library), "--yes"])
    assert curation.formats(library) == {"novel": "pdf"} and "hylly humble download" in capsys.readouterr().out
    cli.main(["set", "format", "cbz", "--id", "h:novel", "--library", str(library), "--yes"])  # not offered
    assert "Left out: 1" in capsys.readouterr().err and curation.formats(library) == {"novel": "pdf"}  # a note
    novel = lambda: next(i for i in records(library) if i["id"] == "h:novel")  # noqa: E731
    epub = novel()["f"][0][0]
    catalog.build(library, log=lambda s: None)  # the PDF isn't downloaded yet: the EPUB stays listed
    assert [f[1] for f in novel()["f"]] == ["epub"]
    pdf = library / epub.replace("%20", " ").replace("novel.epub", "novel.pdf")
    pdf.write_bytes(b"%PDF-1.4")
    Manifest(library, "humble").add({"path": pdf.relative_to(library).as_posix(), "size": pdf.stat().st_size,
                                                        "role": "book", "ext": "pdf", "device": "tablet", "md5": "z"})
    catalog.build(library, log=lambda s: None)  # now the PDF comes first, the EPUB is kept, and the title is for a tablet
    assert [f[1] for f in novel()["f"]] == ["pdf", "epub"] and novel()["dev"] == "tablet"
    humble = {i.id: i for i in catalog.humble_items(library)}
    assert humble["h:novel"].primary[1] == "pdf"  # the chosen file is its main file, though EPUB comes first on the list
    capsys.readouterr()
    cli.main(["set", "format", "auto", "--id", "h:novel", "--library", str(library), "--yes"])
    assert curation.formats(library) == {}
    said = capsys.readouterr().out
    assert "one not downloaded yet goes by the order under [download]" in said and "one downloaded keeps the format it has" in said


def test_a_format_chosen_before_stands_while_the_rules_know_it(library):
    cli.main(["set", "format", "pdf", "--id", "h:novel", "--library", str(library), "--yes"])
    mine = paths.settings_file().parent / "rules" / "mine.toml"
    mine.parent.mkdir(exist_ok=True)
    mine.write_text("[formats.pdf]\nkeep = false\n", encoding="utf-8")
    rules.reset()  # as the next command reads them
    assert curation.formats(library) == {"novel": "pdf"}  # keep says what can be chosen now; this was chosen before
    with pytest.raises(SystemExit, match="format is one of: epub, "):
        cli.main(["set", "format", "pdf", "--id", "h:novel", "--library", str(library), "--yes"])
    mine.write_text("[formats.pdf]\noff = true\n", encoding="utf-8")
    rules.reset()
    assert curation.formats(library) == {}  # a format the rules don't know: left out


def test_editing_what_a_title_shows(served):
    client, srv, library = served
    key = {"X-Server-Key": client.get("/api/session").json()["key"]}  # the owner's own
    fix = lambda field, value: client.post("/api/fix", json={"field": field, "value": value, "ids": ["h:novel"]}, headers=key)  # noqa: E731
    found = next(i for i in records(library) if i["id"] == "h:novel")
    assert fix("title", "A Better Title").json()["was"] == {"h:novel": None}
    authors = fix("authors", ["Writer, Ann", "Second Person"]).json()["items"]["h:novel"]["title"]
    assert authors["a"] == authors["fx"]["authors"] == ["Writer, Ann", "Second Person"] and authors["ak"] == "ann writer"  # as written: "Writer, Ann" is no "First Last"
    fix("series", "Long Roads")
    fix("number", "2")
    fix("year", "1999")
    fix("publisher", "Small Press")
    entry = next(e for e in entries_of(client.get("/opds/search", params={"q": "better title"}).text))  # the new title is found
    assert entry.find(f"{ATOM}title").text == "A Better Title"
    assert [a.find(f"{ATOM}name").text for a in entry.findall(f"{ATOM}author")] == ["Writer, Ann", "Second Person"]
    assert entry.find("{http://purl.org/dc/terms/}publisher").text == "Small Press"
    assert entry.find("{http://purl.org/dc/terms/}issued").text == "1999"
    assert entry.find(f"{ATOM}summary").text.startswith("Series: Long Roads #2.")
    assert fix("year", "99").status_code == 400 and fix("title", "  ").status_code == 400  # not a year; a title can't be empty
    assert fix("authors", "not a list").json()["items"]["h:novel"]["title"]["fx"]["authors"] == ["not a list"]  # text: one name
    assert fix("series", "").json()["ok"]  # empty: no series at all
    assert "Series:" not in next(e for e in entries_of(client.get("/opds/search", params={"q": "better title"}).text)).find(f"{ATOM}summary").text
    assert fix("title", "auto").json()["was"] == {"h:novel": "A Better Title"}  # what undo and Use found send
    assert next(i for i in records(library) if i["id"] == "h:novel")["t"] == found["t"]
    latest = client.get("/api/edits").json()["edits"][0]
    assert (latest["field"], latest["op"]) == ("title", "clear")


def test_text_corrections_replay_and_hold_their_shape():
    line = lambda field, value, op="set": {"field": field, "op": op, "value": value, "titles": [{"id": "a"}]}  # noqa: E731
    edits = curation.Edits([line("authors", "Brennan, Jo; Mary K. Smith"), line("year", "nineteen"), line("series", ""),
                            line("title", "The Lantern Keeper"), line("title", None, "clear")])
    assert edits.fixes_of(["a"]) == {"authors": ["Brennan, Jo", "Mary K. Smith"], "series": ""}  # a bad year is ignored


def test_set_command_for_details(library, capsys):
    cli.main(["set", "authors", "Ann Writer; Bea Other", "--id", "h:novel", "--library", str(library), "--yes"])
    cli.main(["set", "series", "", "--id", "h:novel", "--library", str(library), "--yes"])
    novel = next(i for i in records(library) if i["id"] == "h:novel")
    assert novel["a"] == ["Ann Writer", "Bea Other"] and "s" not in novel
    assert "Set the authors to Ann Writer, Bea Other for 1 title." in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["set", "year", "soon", "--id", "h:novel", "--library", str(library), "--yes"])


# ---- in the database, and which computer keeps it ---------------------------------------------------

def test_the_library_says_which_computers_database_keeps_its_records(library, monkeypatch, tmp_path_factory):
    from hylly import db

    marker = json.loads(db.owner_file(library).read_text(encoding="utf-8"))
    assert marker["id"] and marker["computer"]
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path_factory.mktemp("another-computer")))  # another computer's settings
    db.close_all()
    with pytest.raises(db.NotHere, match="run this there"):
        curation.load(library)
    cli.main(["tag", "list", "--library", str(library)])  # reading: as the library published it
    with pytest.raises(SystemExit):  # changing: only where its records are kept
        cli.main(["tag", "add", "Knitting", "--id", "h:novel", "--yes", "--library", str(library)])
    db.owner_file(library).unlink()  # a library restored here, to start afresh: on this computer's disk or a share alike
    assert curation.load(library).of(["h:novel"]) == []
    assert json.loads(db.owner_file(library).read_text(encoding="utf-8"))["id"] != marker["id"]


def as_the_test_versions_left_it(where: Path, step: int = 9, journal: str = "WAL") -> None:
    """A database as a test version of Hylly from before its first release left it, in WAL mode as they did (or DELETE,
    as a nightly copy is): one that came
    through all nine of their steps has the first step's very tables, but two of them grown by ALTER TABLE, as theirs
    were (so their SQL reads otherwise), a table made and dropped, as their eighth did, and their header: no
    application id, and user_version the step it came to. With a person, rory, and some of what's kept of them."""
    import sqlite3

    from hylly import db, reviews

    where.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(where, isolation_level=None)
    old.execute("BEGIN")
    for statement in db.statements(db.migrations()[0][2]):
        old.execute(statement)
    for statement in ("DROP INDEX list_featured", "ALTER TABLE list DROP COLUMN featured", "ALTER TABLE list DROP COLUMN apps",
                      "ALTER TABLE person DROP COLUMN own_look",
                      "ALTER TABLE list ADD COLUMN apps INTEGER NOT NULL DEFAULT 0 CHECK (apps IN (0, 1))",
                      "ALTER TABLE list ADD COLUMN featured INTEGER NOT NULL DEFAULT 0 CHECK (featured IN (0, 1) AND (featured = 0 OR apps = 1))",
                      "CREATE UNIQUE INDEX list_featured ON list (featured) WHERE featured = 1",
                      "ALTER TABLE person ADD COLUMN own_look TEXT CHECK (own_look IS NULL OR json_valid(own_look))",
                      "CREATE TABLE moved (what TEXT PRIMARY KEY, at TEXT NOT NULL, said TEXT NOT NULL) STRICT", "DROP TABLE moved"):
        old.execute(statement)
    review = reviews.step(None, {"op": "write", "id": "h:novel", "t": "The Novel", "f": "humble/b/novel.epub", "stars": 4,
                                 "text": "Lovely to read aloud.", "when": "2026-11-09T10:00:00+02:00", "time": 1.0})
    rory = old.execute("INSERT INTO person (name, since, look, own_look) VALUES ('rory', '2026-11-09', 'yours', ?)",
                       (db.dump(OWN_LOOK),)).lastrowid
    old.execute("INSERT INTO place (person, title, title_t, at, pct, time) VALUES (?, 'h:novel', 'The Novel', '12', 0.25, 1.0)", (rory,))
    old.execute("INSERT INTO review (person, title, body) VALUES (?, 'h:novel', ?)", (rory, db.dump(review)))
    old.execute("INSERT INTO list (person, id, name, made, apps, featured) VALUES (?, 'l1', 'Harbour reads', 1, 1, 1)", (rory,))
    old.execute(f"PRAGMA user_version = {step}")
    old.execute("COMMIT")
    old.execute(f"PRAGMA journal_mode = {journal}")
    old.close()


OWN_LOOK = {"base": "paper", "light": {"page": "#fdf6e3"}, "dark": {}}


SCHEMA_STEP_9 = Path(__file__).with_name("data") / "schema-step9.sql"  # what the test versions' nine steps left, frozen


def sql_of(c) -> dict:
    """Each table's, index's and view's SQL, with comments and spaces taken out: what it says, however it was written."""
    def plain(sql: str) -> str:
        sql = re.sub(r"\s+", " ", re.sub(r"--[^\n]*", "", sql))
        return sql.replace("( ", "(").replace(" )", ")").replace(" ,", ",").strip()

    return {(row[0], row[1]): plain(row[2]) for row in c.execute("SELECT type, name, sql FROM sqlite_schema WHERE sql IS NOT NULL")}


def test_the_first_step_makes_exactly_what_the_test_versions_nine_steps_left():
    """The databases the test versions made, the owner's among them, are taken on as the first step as they are, so it
    must make their very tables: held to a frozen copy of what their nine steps left (tests/data/schema-step9.sql), by
    SQLite's own account of every column, and by the SQL of every table, index and view, comments aside."""
    from hylly import db

    then, now = db.connect(":memory:"), db.connect(":memory:")
    try:
        for statement in db.statements(SCHEMA_STEP_9.read_text(encoding="utf-8")):
            then.execute(statement)
        for statement in db.statements(db.migrations()[0][2]):
            now.execute(statement)
        assert db._shape(now) == db._shape(then) and len(db._shape(now)) > 30
        assert sql_of(now) == sql_of(then)
    finally:
        then.close()
        now.close()


def test_a_new_database_is_made_by_the_one_step_and_says_it_s_hyllys():
    from hylly import db

    with db.database().read() as c:
        found = set(db.tables(c))
        person = {row["name"] for row in c.execute("PRAGMA table_info(person)")}
        header = [c.execute(f"PRAGMA {name}").fetchone()[0] for name in ("application_id", "user_version")]
    assert {"person", "place", "review", "report_decision", "library", "edit", "edit_title", "shared_shelf", "suggestion",
            "title", "account", "download", "copied"} <= found and {"own_look", "look"} <= person
    assert not {"moved", "kept_record", "review_event"} & found and "format" not in person  # nothing that nothing uses
    assert header == [db.APPLICATION_ID, 5] == [db.APPLICATION_ID, len(db.migrations())] and {"question", "removed"} <= found
    assert not list(db.file().parent.glob("hylly.before-*.db"))  # nothing in use to copy first


def test_the_second_step_names_every_shop_s_records_alike_and_takes_any_shop_s():
    """A database of the first release opens with its download records rewritten in place, in one step, each in
    its place: the title and the file by the same fields for every shop (item, file), what the shop listed as
    listed_md5 and listed_size, the shops' own names for these gone, and every other field as it was. A copy is taken
    first. The table takes any shop's records now, so that a new shop needs no step of its own, and the step done
    again changes nothing."""
    import sqlite3

    from hylly import db

    at_the_first_step(db.file())
    with db.database().read() as c:
        header = [c.execute(f"PRAGMA {name}").fetchone()[0] for name in ("application_id", "user_version")]
        rows = [(row["seq"], row["shop"], json.loads(row["body"])) for row in c.execute("SELECT * FROM download ORDER BY seq")]
        columns = [(row["item"], row["file"]) for row in c.execute("SELECT item, file FROM downloaded ORDER BY seq")]
    assert header == [db.APPLICATION_ID, len(db.migrations())]  # and on through the steps after it
    assert rows == [(n, shop, named_now(old, change)) for n, (shop, old, change) in enumerate(FIRST_RELEASE_RECORDS, 1)]
    assert not [k for _, _, body in rows for k in body if k.startswith(("storybundle_", "drivethrurpg_", "itchio_"))]
    assert columns[3:5] == [("78", 0), ("1001", 0)]  # for SQL too
    with contextlib.closing(sqlite3.connect(db.file().with_name("hylly.before-0002.db"))) as kept:  # the copy taken first
        assert kept.execute("PRAGMA user_version").fetchone()[0] == 1
        assert [json.loads(b) for (b,) in kept.execute("SELECT body FROM download ORDER BY seq")] == [o for _, o, _ in FIRST_RELEASE_RECORDS]
    with db.database().write() as c:  # any shop's now, with no step of its own
        c.execute("INSERT INTO download (shop, body) VALUES ('lanternshop', ?)", (db.dump({"path": "lanternshop/a/b.pdf"}),))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO download (shop, body) VALUES ('', '{}')")  # but always someone's
        before = c.execute("SELECT seq, shop, body FROM download ORDER BY seq").fetchall()
        for statement in db.statements(db.migrations()[1][2]):  # the step once more: nothing changes
            c.execute(statement)
        assert c.execute("SELECT seq, shop, body FROM download ORDER BY seq").fetchall() == before
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_two_programs_opening_a_database_of_the_first_release_at_once_take_one_copy_and_the_step_once(monkeypatch):
    """The server and a job, say, opening it at once after the update: the copy is taken holding the write lock, under a
    name of the program's own until it's whole, so one of them takes it, whole, and the step; the other waits, and finds
    both done. Neither fails, and nothing is left beside it."""
    import sqlite3
    import time

    from hylly import db

    at_the_first_step(db.file())
    real = db.copy_to

    def slowly(c, target):  # the copy takes its time, so that the other comes meanwhile
        time.sleep(0.5)
        real(c, target)

    monkeypatch.setattr(db, "copy_to", slowly)
    start, found, errors = threading.Barrier(2), [], []

    def opener() -> None:
        mine = db.Database(db.file())  # a connection of its own, as another program's
        start.wait()
        try:
            with mine.read() as c:
                found.append(c.execute("PRAGMA user_version").fetchone()[0])
        except Exception as e:  # noqa: BLE001 - said below
            errors.append(e)
        finally:
            mine.close()

    both = [threading.Thread(target=opener) for _ in range(2)]
    [t.start() for t in both]
    [t.join(60) for t in both]
    assert not errors and found == [len(db.migrations())] * 2
    assert sorted(p.name for p in db.file().parent.iterdir() if p.name.startswith("hylly.before")) == ["hylly.before-0002.db"]
    assert not list(db.file().parent.glob("*.part"))
    with contextlib.closing(sqlite3.connect(db.file().with_name("hylly.before-0002.db"))) as kept:  # whole: as it was
        assert kept.execute("PRAGMA user_version").fetchone()[0] == 1
        assert kept.execute("SELECT count(*) FROM download").fetchone()[0] == len(FIRST_RELEASE_RECORDS)


def test_a_copy_from_before_a_step_already_there_is_never_written_over():
    """A database put back from a copy of before the step, after the step was taken once: its copy before the step is
    taken anew, named for when it was taken, and the first one stays as it is."""
    import sqlite3

    from hylly import db

    earlier = db.file().with_name("hylly.before-0002.db")
    earlier.parent.mkdir(parents=True, exist_ok=True)
    earlier.write_bytes(b"the copy taken the first time")
    at_the_first_step(db.file())
    with db.database().read() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.migrations())
    assert earlier.read_bytes() == b"the copy taken the first time"
    new, = [p for p in db.file().parent.glob("hylly.before-0002-*.db")]
    assert re.fullmatch(r"hylly\.before-0002-\d{4}-\d\d-\d\d_\d{6}\.db", new.name)
    with contextlib.closing(sqlite3.connect(new)) as kept:
        assert kept.execute("PRAGMA user_version").fetchone()[0] == 1


STORYBUNDLE_KINDS = [  # (shop, a record at the second step, what the third makes of it)
    ("storybundle", {"path": "storybundle/The Moonwake Bundle/Second Tide/second-tide.epub", "item": "78", "file": 0,
                     "kind": "storybundle", "size": 30, "bonus": False}, {"kind": "book"}),  # its own flow's word
    ("storybundle", {"path": "storybundle/The Lantern Bundle/Third Tide/third-tide.pdf", "item": "79", "file": 0,
                     "kind": "comic", "size": 31}, {}),  # a kind of the rules' already: as it is
    ("storybundle", {"path": "storybundle/The Lantern Bundle/Old/old.epub", "item": "80", "size": 2}, {}),  # none said
    ("humble", {"path": "humble/Moonwake Press Fantasy Bundle/The Novel/novel.epub", "item": "novel", "file": "novel.epub",
                "kind": "storybundle", "size": 3}, {}),  # another shop's, whatever it says: never touched
]


def test_the_third_step_names_storybundle_s_old_kind_book_and_nothing_else(monkeypatch):
    """A database at the second step whose StoryBundle records from before the shops' core say kind storybundle,
    no kind of the rules', opens with those saying book, each in its place, every other field and every other record
    as it was; a copy is taken first, at the second step, and the step done again changes nothing. A version of Hylly
    that knows two steps leaves a database at the third alone, and says why (db.Newer)."""
    import sqlite3

    from hylly import db

    two = db.file().parent / "two-steps"
    two.mkdir(parents=True)
    for path in sorted(db.MIGRATIONS.glob("*.sql"))[:2]:
        shutil.copy(path, two)
    at_the_first_step(db.file(), records=STORYBUNDLE_KINDS)
    with monkeypatch.context() as m:  # as a Hylly that knows two steps opens it: the second step only
        m.setattr(db, "MIGRATIONS", two)
        with db.database().read() as c:
            assert c.execute("PRAGMA user_version").fetchone()[0] == 2
        db.close_all()
    with db.database().read() as c:  # this version's: the third step too, and those after it
        assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.migrations()) >= 3
        rows = [(row["seq"], row["shop"], json.loads(row["body"])) for row in c.execute("SELECT * FROM download ORDER BY seq")]
    assert rows == [(n, shop, {**old, **change}) for n, (shop, old, change) in enumerate(STORYBUNDLE_KINDS, 1)]
    with contextlib.closing(sqlite3.connect(db.file().with_name("hylly.before-0003.db"))) as kept:  # the copy first, at 2
        assert kept.execute("PRAGMA user_version").fetchone()[0] == 2
        assert [json.loads(b)["kind"] for (b,) in kept.execute("SELECT body FROM download WHERE seq = 1")] == ["storybundle"]
    with db.database().write() as c:
        before = c.execute("SELECT seq, shop, body FROM download ORDER BY seq").fetchall()
        for statement in db.statements(db.migrations()[2][2]):  # the step once more: nothing changes
            c.execute(statement)
        assert c.execute("SELECT seq, shop, body FROM download ORDER BY seq").fetchall() == before
    db.close_all()
    steps = len(db.migrations())
    with monkeypatch.context() as m:  # the code before it, as after a rollback: left alone, and said
        m.setattr(db, "MIGRATIONS", two)
        with pytest.raises(db.Newer, match=rf"is from a newer version of Hylly \(its schema is {steps}, this version knows 2\), "
                                           r"so it's left alone, and nothing is kept in it until Hylly is updated"):
            with db.database().read():
                pass
        db.close_all()


def test_the_fourth_step_makes_the_questions_table_after_a_copy_and_done_again_changes_nothing(monkeypatch):
    """A database at the third step opens with the table of questions (question), empty, every other table and
    record as it was; a copy is taken first, at the third step (hylly.before-0004.db), and the step done again changes
    nothing. A Hylly that knows three steps leaves a database at the fourth alone, and says why (db.Newer), as one that
    knew two did one at the third."""
    import sqlite3

    from hylly import db

    three, four = db.file().parent / "three-steps", db.file().parent / "four-steps"
    for folder, steps in ((three, 3), (four, 4)):
        folder.mkdir(parents=True)
        for path in sorted(db.MIGRATIONS.glob("*.sql"))[:steps]:
            shutil.copy(path, folder)
    at_the_first_step(db.file(), records=STORYBUNDLE_KINDS)

    def every_row(c) -> dict:
        return {t: [tuple(row) for row in c.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in db.tables(c)}

    with monkeypatch.context() as m:  # as a Hylly that knows three steps opens it: the third step at most
        m.setattr(db, "MIGRATIONS", three)
        with db.database().read() as c:
            assert c.execute("PRAGMA user_version").fetchone()[0] == 3
            before = every_row(c)
        db.close_all()
    assert "question" not in before and before["download"]
    with monkeypatch.context() as m:  # the version that brought the fourth step (a later one's are its tests')
        m.setattr(db, "MIGRATIONS", four)
        with db.database().read() as c:
            assert c.execute("PRAGMA user_version").fetchone()[0] == 4
            after = every_row(c)
        assert after == {**before, "question": []}
        with contextlib.closing(sqlite3.connect(db.file().with_name("hylly.before-0004.db"))) as kept:  # the copy first, at 3
            assert kept.execute("PRAGMA user_version").fetchone()[0] == 3 and "question" not in db.tables(kept)
        with db.database().write() as c:
            for statement in db.statements(db.migrations()[3][2]):  # the step once more: nothing changes
                c.execute(statement)
            assert every_row(c) == after
        db.close_all()
    with monkeypatch.context() as m:  # the code before it, as after a rollback: left alone, and said
        m.setattr(db, "MIGRATIONS", three)
        with pytest.raises(db.Newer, match=r"is from a newer version of Hylly \(its schema is 4, this version knows 3\)"):
            with db.database().read():
                pass
        db.close_all()


def test_the_fifth_step_makes_the_table_of_files_removed_after_a_copy_and_done_again_changes_nothing(monkeypatch):
    """A database at the fourth step, with a question in it, opens with the table of files removed into _Removed
    (removed), empty, every other table and record as it was; a copy is taken first, at the fourth step
    (hylly.before-0005.db), and the step done again changes nothing. The table keeps only what a removal is: a row
    without its place, or of a state there's no such, is refused. A Hylly that knows four steps leaves it alone."""
    import sqlite3

    from hylly import db, removed

    four = db.file().parent / "four-steps"
    four.mkdir(parents=True)
    for path in sorted(db.MIGRATIONS.glob("*.sql"))[:4]:
        shutil.copy(path, four)
    at_the_first_step(db.file(), records=STORYBUNDLE_KINDS)

    def every_row(c) -> dict:
        return {t: [tuple(row) for row in c.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in db.tables(c)}

    with monkeypatch.context() as m:  # as a Hylly that knows four steps opens it, and keeps a question
        m.setattr(db, "MIGRATIONS", four)
        with db.database().write() as c:
            assert c.execute("PRAGMA user_version").fetchone()[0] == 4
            curation.QUESTION.insert(c, {**a_question("71"), "shop": "drivethrurpg", "first": "2026-11-20T10:00:00+02:00",
                                         "last": "2026-11-20T10:00:00+02:00"})
            before = every_row(c)
        db.close_all()
    assert "removed" not in before and before["question"] and before["download"]
    with db.database().read() as c:  # this version's: the fifth step too
        assert c.execute("PRAGMA user_version").fetchone()[0] == 5
        after = every_row(c)
    assert after == {**before, "removed": []}
    with contextlib.closing(sqlite3.connect(db.file().with_name("hylly.before-0005.db"))) as kept:  # the copy first, at 4
        assert kept.execute("PRAGMA user_version").fetchone()[0] == 4 and "removed" not in db.tables(kept)
    with db.database().write() as c:
        for statement in db.statements(db.migrations()[4][2]):  # the step once more: nothing changes
            c.execute(statement)
        assert every_row(c) == after
        row = {"path": "ebooks/Hedge Lore.epub", "held": "ebooks/Hedge Lore.epub", "size": 10, "title": "f:a", "t": "Hedge Lore",
               "shop": "", "at": db.now(), "state": "removed"}
        removed.REMOVED.insert(c, row)
        for wrong in ({**row, "path": ""}, {**row, "state": "gone"}, {**row, "size": -1}, {**row, "at": None}):
            with pytest.raises(sqlite3.IntegrityError):  # that statement undone alone, the change going on
                removed.REMOVED.insert(c, wrong)
        assert [removed.REMOVED.record(r) for r in c.execute("SELECT * FROM removed")] == [row]
    db.close_all()
    with monkeypatch.context() as m:  # the code before it, as after a rollback: left alone, and said
        m.setattr(db, "MIGRATIONS", four)
        with pytest.raises(db.Newer, match=r"is from a newer version of Hylly \(its schema is 5, this version knows 4\)"):
            with db.database().read():
                pass
        db.close_all()


def a_question(item: str, **more) -> dict:
    """A question as a run finds it: of a file gone from its place, at the made-up shop's letter d (DriveThruRPG's)."""
    return {"kind": "moved", "item": item, "file": "0", "title": f"Lantern Wolf {item}", "options": list(curation.MOVED),
            "guess": 0, "held": False, "why": "lantern.pdf is gone from its folder", **more}


def test_a_run_s_questions_are_kept_once_each_found_again_by_their_subject_and_gone_when_found_no_more(tmp_path):
    """One list of questions, kept with the library's records: a question a run finds again (its kind and its
    subject: the shop, the title, the file) is the same one, with when it was first found, never a second; one the
    run covered (its title looked at) but found no more is gone; one it didn't cover stays; a dry run's are shown,
    never kept; an answer takes one off the open list, and a run that finds it all the same puts it back."""
    found, new = curation.keep_questions(tmp_path, "drivethrurpg", [a_question("71"), a_question("72"), a_question("71")],
                                         lambda q: True)
    assert new == 2 and [(q["catalog_id"], q["kind"], q["file"], q["guess"]) for q in found] == [
        ("d:71", "moved", "0", 0), ("d:72", "moved", "0", 0)]
    first = found[0]["first_found"]
    assert curation.questions(tmp_path) == found and curation.asked(tmp_path, "drivethrurpg") == {
        ("moved", "71", "0"), ("moved", "72", "0")}
    again, new = curation.keep_questions(tmp_path, "drivethrurpg", [a_question("71", held=True), a_question("73")],
                                         lambda q: q["item"] in {"71", "72", "73"}, keep=False)  # a dry run's
    assert new == 1 and [q["catalog_id"] for q in again] == ["d:71", "d:73"] and again[1]["id"] is None
    assert curation.questions(tmp_path) == found  # nothing kept
    again, new = curation.keep_questions(tmp_path, "drivethrurpg", [a_question("71", held=True)], lambda q: q["item"] == "71")
    assert new == 0 and [(q["id"], q["held"], q["first_found"]) for q in again] == [
        (found[0]["id"], True, first), (found[1]["id"], False, found[1]["first_found"])]  # 72 not looked at: it stays
    curation.answer(tmp_path, found[1]["id"], "keep", by=curation.COMMAND_LINE)
    assert [q["catalog_id"] for q in curation.questions(tmp_path)] == ["d:71"]  # answered: off the open list
    gone, _ = curation.keep_questions(tmp_path, "drivethrurpg", [], lambda q: q["item"] == "71")
    assert gone == [] and curation.questions(tmp_path) == []  # found no more where looked at
    back, new = curation.keep_questions(tmp_path, "drivethrurpg", [a_question("72")], lambda q: True)
    assert new == 0 and [q["id"] for q in back] == [found[1]["id"]]  # found all the same: open again, the same one
    assert curation.questions(tmp_path, "itchio") == [] and curation.questions(tmp_path, "drivethrurpg") == back


def test_an_answer_is_an_ordinary_edit_replayed_undone_and_following_its_title(tmp_path):
    """Answering is the edit that settles the question: a file's option a file correction (hylly set file), any
    other its word in the title's answers, with what the question offered; both replay from the edits, are undone with
    auto as any correction (and the question is no longer settled), and follow a title whose id changed (reattach). A
    number that isn't an option's, or a question not open, is a mistake that changes nothing."""
    files = [curation.file_option("fr.pdf", "pdf", 50), curation.file_option("fr_hq.pdf", "pdf", 120, "PDF (HQ)")]
    curation.keep_questions(tmp_path, "humble", [
        {"kind": "which-file", "item": "fieldrules", "file": "", "title": "Iron Stride Field Rules", "options": files,
         "guess": 1, "held": True, "why": "Humble offers 2 PDF files, and the largest was taken"},
        {**a_question("71"), "item": "lantern", "title": "Lantern Wolf"}], lambda q: True)
    which, moved = curation.questions(tmp_path)
    for choice, says in (("3", "with the number of one of its options, 1 to 2, a file's name among them, or keep"),
                         ("x", "1 to 2"), ("0", "1 to 2")):
        with pytest.raises(SystemExit, match=says):
            curation.answer(tmp_path, which["id"], choice, by="rory")
    with pytest.raises(SystemExit, match="No question 99 is open"):
        curation.answer(tmp_path, 99, "1", by="rory")
    assert curation.answer(tmp_path, which["id"], "1", by="rory", dry_run=True)["field"] == "file"
    assert curation.load_lines(tmp_path) == [] and len(curation.questions(tmp_path)) == 2  # a dry run: nothing made
    done = curation.answer(tmp_path, which["id"], "1", by="rory")
    assert (done["field"], done["id"], done["option"]["file"]) == ("file", "h:fieldrules", "fr.pdf")
    curation.answer(tmp_path, moved["id"], "2", by="rory")
    assert curation.files(tmp_path) == {"fieldrules": "fr.pdf"}  # replayed from the edits, as the plan reads them
    said = curation.answers(tmp_path)["h:lantern"]
    assert said == {"moved:0": {"a": "moved", "o": curation.fingerprint(list(curation.MOVED))}}
    assert curation.settled(said, "moved", "0", list(curation.MOVED)) == "moved" and curation.questions(tmp_path) == []
    assert curation.settled(said, "moved", "1", list(curation.MOVED)) == ""  # another file's: not answered
    last = curation.load_lines(tmp_path)[-1]
    assert (last["field"], last["by"], last["was"], last["titles"][0]["id"]) == ("answers", "rory", {"h:lantern": None}, "h:lantern")
    curation.change_single(tmp_path, "file", "auto", [{"id": "h:fieldrules", "t": "", "f": [[""]]}], by="rory")  # undone
    assert curation.files(tmp_path) == {}
    titles = matching.Titles([{"id": "h:lantern-2", "t": "Lantern Wolf", "f": [["humble/b/lantern.pdf", "pdf", 3]]}])
    titles.follow = lambda old, was: "h:lantern-2" if old == "h:lantern" else None
    left_out = {"title:": {"a": "keep", "o": curation.fingerprint([curation.LEAVE_OUT])}}  # an item kept out: never a title
    curation.change_single(tmp_path, "answers", left_out, [{"id": "h:prints", "t": "Prints", "f": [[""]]}], by="rory")
    assert curation.reattach(tmp_path, titles, log=lambda line: None) == []  # answers alone are no title lost to tell of
    assert curation.answers(tmp_path) == {"h:lantern-2": said, "h:prints": left_out}  # followed its title, as any does
    curation.change_single(tmp_path, "answers", "auto", [{"id": "h:lantern-2", "t": "", "f": [[""]]}], by="rory")
    assert curation.answers(tmp_path) == {"h:prints": left_out}  # taken back: that title's alone
    with pytest.raises(SystemExit, match="hylly set answers auto takes them back"):
        curation.change_single(tmp_path, "answers", {"moved:0": {"a": "lost"}}, [{"id": "h:x", "t": "", "f": [[""]]}], by="rory")


def test_a_step_that_frees_much_of_the_file_gives_it_back():
    """The second step makes the downloads' table anew: the old one's pages, which hold nothing any more (secure_delete),
    are given back, so that the file and its nightly copies don't stay twice their size. The copy before it is whole,
    taken before anything is written."""
    import sqlite3

    from hylly import db

    many = [(shop, {**old, "path": f"{old['path']}.{n}", "padding": "x" * 400}, change)
            for n in range(150) for shop, old, change in FIRST_RELEASE_RECORDS]
    at_the_first_step(db.file(), many)
    before = db.file().stat().st_size
    with db.database().read() as c:
        assert c.execute("PRAGMA freelist_count").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM download").fetchone()[0] == len(many)
    db.close_all()
    assert db.file().stat().st_size <= before * 1.2  # the records as they were, renamed: not twice them
    with contextlib.closing(sqlite3.connect(db.file().with_name("hylly.before-0002.db"))) as kept:  # the copy first, whole
        assert kept.execute("SELECT count(*) FROM download").fetchone()[0] == len(many)
        assert kept.execute("PRAGMA user_version").fetchone()[0] == 1


def test_a_database_of_the_test_versions_ninth_step_opens_as_the_first_with_its_people_reviews_and_places():
    """The two databases in use before the first release came through all nine of the test versions' steps: they open
    as this version's first, with a copy taken first, and only their header changes."""
    import sqlite3

    from hylly import db, people, reviews

    as_the_test_versions_left_it(db.file())
    with contextlib.closing(sqlite3.connect(db.file())) as c:
        before = {table: c.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in ("person", "place", "review", "list")}
        sql = {row[0]: row[1] for row in c.execute("SELECT name, sql FROM sqlite_schema")}
    rory = people.Person("rory")
    assert rory.reviews()[0]["text"] == "Lovely to read aloud."  # opened: taken on as it is
    assert rory.latest()["h:novel"]["at"] == 12 and (rory.look(), rory.own_look()) == ("yours", OWN_LOOK)
    assert [(found["name"], found["featured"]) for found in people.in_the_apps()] == [("Harbour reads", True)]
    with db.database().read() as c:  # and brought through this version's next steps, which rewrite only the downloads' table
        # and make the questions' table (the fourth) and the removed files' (the fifth)
        assert [c.execute(f"PRAGMA {name}").fetchone()[0] for name in ("application_id", "user_version")] == [
            db.APPLICATION_ID, len(db.migrations())]
        assert {table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in before} == before
        anew = {"download", "download_by_path", "downloaded", "question", "sqlite_autoindex_question_1", "sqlite_sequence", "removed"}
        assert {k: v for k, v in sql.items() if k not in anew} == {row[0]: row[1] for row in c.execute(
            "SELECT name, sql FROM sqlite_schema") if row[0] not in anew}  # nothing else changed
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and c.execute("PRAGMA foreign_key_check").fetchall() == []
    with contextlib.closing(sqlite3.connect(db.file().with_name("hylly.before-0001.db"))) as kept:  # the copy taken first: as it was
        assert [kept.execute(f"PRAGMA {name}").fetchone()[0] for name in ("application_id", "user_version")] == [0, 9]
        assert kept.execute("SELECT name FROM person").fetchall() == [("rory",)]
    assert rory.write({"id": "h:novel", "t": "The Novel"}, 4, "", False, wait=False)["state"] == reviews.RATED  # and changes go on
    db.close_all()
    with db.database().read() as c:  # and the next time, it's this version's own
        assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.migrations())


def test_a_database_of_an_earlier_test_versions_step_is_left_as_it_is_and_says_what_to_do(tmp_path):
    """One that stopped at the test versions' fifth step (an older nightly copy put back, say) can't be told by its
    number alone, which this version would read as its own: it's refused, never read nor written, and the words say how
    to bring it on. So are its dry run and the doctor's look."""
    from hylly import db, doctor

    as_the_test_versions_left_it(db.file(), step=5, journal="DELETE")  # as a nightly copy is: nothing may turn WAL on
    was = db.file().read_bytes()
    for ask in (people.everyone, lambda: db.database().read().__enter__()):
        with pytest.raises(db.Older, match=r"from before its first release.*came through 5 of them.*all 9 \(user_version 9, "
                                           r"with no application id yet\).*knows all 9 of those steps.*hylly people list"):
            ask()
        db.close_all()
    db.start_dry_run()
    try:
        with pytest.raises(db.Older):
            db.database().read().__enter__()
    finally:
        db.end_dry_run()
    said = doctor._people(tmp_path)
    assert said[0]["state"] == doctor.PROBLEM and "came through 5" in said[0]["says"] and "next" not in said[0]
    with pytest.raises(people.Newer) as refused:  # left alone, as a newer version's is, wherever that's caught
        people.Person("rory").place({"id": "h:novel", "t": "The Novel"}, 1, 0.1)
    assert refused.value.older  # and said in words of its own (serve.Handler._left_alone)
    assert db.file().read_bytes() == was and sorted(p.name for p in db.file().parent.iterdir()) == [db.FILE]  # no copy either


def test_a_change_refused_as_the_database_is_left_alone_says_which_kind_of_database():
    from types import SimpleNamespace

    said = []
    handler = SimpleNamespace(_json=lambda data, status: said.append((status, data["error"])), LEFT_ALONE=serve.Handler.LEFT_ALONE)
    serve.Handler._left_alone(handler, people.Newer("rory"))
    serve.Handler._left_alone(handler, people.Newer("rory", older=True), "The database")
    # 503, as the page's outbox keeps a change answered so and sends it again later: a 409 was refused for good, and let go
    assert said[0] == (503, "What's kept for you is from a newer version of Hylly, and is left alone until this one is updated.")
    assert said[1][0] == 503 and said[1][1].startswith("The database is from a test version of Hylly from before its first release")
    assert "hylly doctor" in said[1][1] and "updated" not in said[1][1]


def test_a_change_the_database_leaves_alone_is_one_the_page_waits_on_and_records_kept_elsewhere_are_refused(monkeypatch):
    """What nothing else caught was answered 409 for a database of a newer version too, which the page's outbox lets go
    as refused for good. A newer version's (and a test version's, left alone as one) is 503 now, which the outbox waits
    on; a database that doesn't keep this library's records stays a change refused (409), until the owner moves them."""
    from types import SimpleNamespace

    from hylly import db

    monkeypatch.setattr(serve.Handler, "FAILED", set())
    monkeypatch.setattr(serve.Handler, "FAILED_SAID", {})
    said = []
    handler = SimpleNamespace(_status=None, _json=lambda data, status: said.append((status, data["error"])), UNUSABLE=serve.Handler.UNUSABLE)
    for refused in (db.Newer("made-up: a newer one"), db.Older("made-up: a test version's"), db.NotHere("made-up: another computer's")):
        serve.Handler._failed(handler, refused)
    assert [status for status, _ in said] == [503, 503, 409]
    assert said[0][1].startswith("The library's database is from a newer version of Hylly")
    assert said[1][1] == said[2][1] == serve.Handler.UNUSABLE


def test_a_database_that_says_it_came_through_nine_steps_but_has_other_tables_is_left_as_it_is():
    import sqlite3

    from hylly import db

    as_the_test_versions_left_it(db.file(), journal="DELETE")
    with contextlib.closing(sqlite3.connect(db.file(), isolation_level=None)) as c:
        c.execute("ALTER TABLE person DROP COLUMN own_look")  # by hand, say
    was = db.file().read_bytes()
    with pytest.raises(db.Older, match=r"all 9, but its tables aren't what they made \(person\)"):
        db.database().read().__enter__()
    assert db.file().read_bytes() == was and sorted(p.name for p in db.file().parent.iterdir()) == [db.FILE]


def test_the_read_only_looks_take_the_ninth_step_as_it_is_and_an_earlier_one_not_at_all(library):
    """hylly doctor's look at the catalog's files, and hylly init's at the titles kept, open the database read-only, so
    nothing brings it up to date first: a database of the test versions' ninth step is read as this version's first,
    and left so; one of an earlier step isn't read at all."""
    import sqlite3

    from hylly import db, doctor

    with catalog.store(library).read() as c:
        kept = c.execute("SELECT count(*) FROM title").fetchone()[0]
    db.close_all()
    with contextlib.closing(sqlite3.connect(db.file(), isolation_level=None)) as c:  # their tables: the downloads' as the first step's
        c.execute("DROP VIEW downloaded")
        c.execute("ALTER TABLE download RENAME TO download_now")
        c.execute("DROP INDEX download_by_path")
        for statement in db.statements(db.migrations()[0][2]):
            if re.match(r"CREATE (TABLE|INDEX|VIEW) download", re.sub(r"--[^\n]*\n", "", statement).strip()):
                c.execute(statement)
        c.execute("INSERT INTO download (seq, shop, body) SELECT seq, shop, body FROM download_now")
        c.execute("DROP TABLE download_now")
        c.execute("DROP TABLE question")  # the fourth step's, which the first hadn't
        c.execute("DROP TABLE removed")  # nor the fifth's
    for step, files, titles in ((9, doctor.OK, kept), (5, doctor.PROBLEM, 0)):
        with contextlib.closing(sqlite3.connect(db.file(), isolation_level=None)) as c:
            c.execute("PRAGMA application_id = 0")
            c.execute(f"PRAGMA user_version = {step}")  # as the test versions left it
        assert doctor._underneath(library)[0]["state"] == files
        assert cli._titles_kept_for(library) == titles
        with contextlib.closing(sqlite3.connect(db.file())) as c:
            assert [c.execute(f"PRAGMA {name}").fetchone()[0] for name in ("application_id", "user_version")] == [0, step]  # only looked at
    assert kept > 0


def test_another_programs_database_is_left_as_it_is():
    import sqlite3

    from hylly import db

    db.file().parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(db.file(), isolation_level=None)) as c:
        c.execute("PRAGMA application_id = 1")
        c.execute("CREATE TABLE person (id INTEGER PRIMARY KEY) STRICT")
    was = db.file().read_bytes()
    with pytest.raises(db.Unusable, match="another program's"):
        db.database().read().__enter__()
    assert db.file().read_bytes() == was and sorted(p.name for p in db.file().parent.iterdir()) == [db.FILE]  # as it was


def test_a_newer_database_leaves_the_catalog_as_found_and_lets_nobody_in(library):
    """Nobody is known to a database of a newer version of Hylly: nothing can be asked or changed, and the login page
    says why, not that there are no users."""
    from hylly import db

    curation.change(library, "add", "Knitting", [r for r in records(library) if r["id"] == "h:novel"], "owen")
    with db.database().write() as c:
        c.execute("PRAGMA user_version = 99")
    db.close_all()
    srv, _ = serve.make_server(library, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        assert not any(i.get("tg") for i in srv.RequestHandlerClass.catalog.get())  # as found, without your tags
        assert httpx.get(f"{base}/api/session", auth=("owen", "correct horse")).status_code == 401
        answer = httpx.post(f"{base}/api/tags", json={"op": "add", "tag": "Crochet", "ids": ["h:novel"]}, headers={"Origin": base})
        assert answer.status_code == 401
        page = httpx.get(f"{base}/login").text
        assert "the library&#x27;s database can&#x27;t be used from here" in page and "hylly user add" not in page
    finally:
        srv.shutdown()


def test_the_catalog_on_another_computer_shows_as_built_without_the_tags_it_cant_read(library, monkeypatch, tmp_path_factory):
    """The tags and corrections are in the database of the computer that keeps the library's records; nothing copies them
    into the library any more. A server elsewhere (the PC, with the library on a share) shows the catalog as built."""
    from hylly import db

    novel = [r for r in records(library) if r["id"] == "h:novel"]
    built = novel[0]["s"]
    curation.change(library, "add", "Knitting", novel, "owen")
    curation.change_single(library, "series", "Yarns", novel, "owen")
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path_factory.mktemp("the-pc")))  # the PC, with the library on a share
    db.close_all()
    shown = next(i for i in Catalog(library).get() if i["id"] == "h:novel")
    assert "tg" not in shown and shown["s"] == built


def test_a_question_s_number_is_never_given_again(tmp_path):
    """When the newest question goes, the next new one gets a number no question has had
    (AUTOINCREMENT), so an answer typed from an older list never reaches another question; and that answer is refused."""
    curation.keep_questions(tmp_path, "drivethrurpg", [a_question("71"), a_question("72")], lambda q: True)
    old = [q["id"] for q in curation.questions(tmp_path)]
    curation.keep_questions(tmp_path, "drivethrurpg", [a_question("71")], lambda q: True)  # 72's goes
    curation.keep_questions(tmp_path, "drivethrurpg", [a_question("71"), a_question("99")], lambda q: True)
    now = {q["catalog_id"]: q["id"] for q in curation.questions(tmp_path)}
    assert now["d:71"] == old[0] and now["d:99"] > old[1]
    with pytest.raises(SystemExit, match=f"No question {old[1]} is open"):
        curation.answer(tmp_path, old[1], "2", by="rory")
    assert curation.answers(tmp_path) == {}


def test_an_answer_goes_by_what_was_offered_by_a_file_s_name_and_says_what_it_did(tmp_path):
    """An answer given with what the question offered (offered) is refused, nothing
    changed, once the question offers other options (a file the shop added); a choice may be a file's name; and the
    answer says what it did. A number or a choice that can't be one is a mistake, never a crash."""
    files = [curation.file_option("art.pdf", "pdf", 50)]
    found = {"kind": "title", "item": "prints", "file": "", "title": "Moonwake Art Prints", "options": [*files, curation.LEAVE_OUT],
             "guess": 1, "held": False, "why": "its only file is no book by the rules, so it was left out"}
    curation.keep_questions(tmp_path, "humble", [found], lambda q: True)
    (q,) = curation.questions(tmp_path)
    offered = q["offered"]
    more = [*files, curation.file_option("art_hq.pdf", "pdf", 120)]
    curation.keep_questions(tmp_path, "humble", [{**found, "options": [*more, curation.LEAVE_OUT], "guess": 2}], lambda q: True)
    with pytest.raises(SystemExit) as refused:
        curation.answer(tmp_path, q["id"], "2", by="rory", offered=offered)  # was "leave it out"; art_hq.pdf now
    assert refused.value.code == exits.NEEDS_YOU and "offers other options now" in str(refused.value)
    assert curation.load_lines(tmp_path) == []  # nothing changed
    (q,) = curation.questions(tmp_path)
    done = curation.answer(tmp_path, q["id"], "ART_HQ.PDF", by="rory", offered=q["offered"])  # by a file's name
    assert (done["field"], done["option"]["file"]) == ("file", "art_hq.pdf")
    assert done["did"] == "makes it a title, with art_hq.pdf as its book: the next download fetches it"
    for number, choice in ((10**30, "1"), (-1, "1"), (0, "1"), (True, "1"), ("7", "1"), (2**63, "1"), (1, "9" * 5000),
                           (1, ["1"]), (1, True)):
        with pytest.raises(SystemExit) as wrong:
            curation.answer(tmp_path, number, choice, by="rory")
        assert wrong.value.code == exits.USAGE, (number, str(choice)[:10])


def test_a_question_of_a_shop_this_hylly_doesn_t_know_is_left_out_and_said_once(tmp_path, capsys):
    """A row of a shop no longer in shops.SHOPS (an older or a newer Hylly's) is left out of
    the list, so hylly decide, the doctor and the Server page go on; said once."""
    from hylly import db

    curation.keep_questions(tmp_path, "drivethrurpg", [a_question("71")], lambda q: True)
    with curation.store(tmp_path).write() as c:
        c.execute("INSERT INTO question (kind, shop, item, file, options, first_found, last_found) "
                  "VALUES ('moved', 'oldshop', '9', '', '[]', 'x', 'x')")
    curation._told.discard("oldshop")
    assert [q["catalog_id"] for q in curation.questions(tmp_path)] == ["d:71"]
    assert [q["catalog_id"] for q in curation.questions(tmp_path)] == ["d:71"]
    assert capsys.readouterr().err.count("Questions of a shop this Hylly doesn't know (oldshop)") == 1
    with curation.store(tmp_path).read() as c:
        assert db.tables(c) and c.execute("SELECT count(*) FROM question").fetchone()[0] == 2  # kept, as it was


def test_a_correction_by_id_of_a_title_not_downloaded_yet_is_no_title_lost(tmp_path):
    """A kind, file or answers correction made by --id for a shop's title its newest snapshot
    lists, or a question names, isn't said at every catalog build to be of a title no longer in the library; one of an
    id nothing lists is, as before."""
    from hylly import shopping, snapshot

    shopping.snapshot("drivethrurpg", [shopping.Title("71", "Lantern Wolf", "Moonwake Press")], tmp_path / "state")
    curation.keep_questions(tmp_path, "humble", [{**a_question("prints"), "kind": "title"}], lambda q: True)
    for cid, field, value in (("d:71", "kind", "tech"), ("h:prints", "file", "prints.pdf"), ("d:99", "kind", "tech")):
        curation.change_single(tmp_path, field, value, [{"id": cid, "t": cid, "f": [[""]]}], by="rory")
    unplaced = curation.reattach(tmp_path, matching.Titles([]), log=lambda line: None)
    assert [u["id"] for u in unplaced] == ["d:99"] and snapshot.latest(tmp_path / "state", "drivethrurpg")


def test_humble_keeps_its_items_questions_while_a_purchase_has_no_data_anywhere(tmp_path):
    """While Humble gave no data for a purchase that no snapshot has either, the run
    finds the items' questions over a plan without it, so it doesn't count as finding them wholly (covers), and those
    not found stay, with their numbers; a run with every purchase lets them go."""
    from hylly import humble

    lib = humble.Library("c" * 80, tmp_path)
    lib.left = ["K9"]
    assert (lib.incomplete, lib.covers) == (True, ())
    lib.left = []
    assert (lib.incomplete, lib.covers) == (False, ("title",))
    item = {**a_question("prints"), "kind": "title", "options": [curation.LEAVE_OUT]}
    curation.keep_questions(tmp_path, "humble", [item], lambda q: q["kind"] in ("title",))
    (q,) = curation.questions(tmp_path)
    curation.keep_questions(tmp_path, "humble", [], lambda q: q["kind"] in ())  # K9's item not found: no data
    assert [x["id"] for x in curation.questions(tmp_path)] == [q["id"]]


def test_hylly_decide_takes_what_was_offered_a_file_s_name_and_refuses_what_is_no_answer_with_its_code(tmp_path, capsys):
    """At the command line: --offered (the question's offered in
    --json) refuses an answer to a question that offers other options by then (code 8, nothing changed); a file's name
    is a choice; the answer says what it did, its --json too; a number too big or no number, and a choice of thousands
    of digits, are mistakes (code 2), never a traceback."""
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "library.json").write_text("{}", encoding="utf-8")
    files = [curation.file_option("fr.pdf", "pdf", 50), curation.file_option("fr_hq.pdf", "pdf", 120)]
    curation.keep_questions(tmp_path, "humble", [
        {"kind": "which-file", "item": "fieldrules", "file": "", "title": "Iron Stride Field Rules", "options": files,
         "guess": 1, "held": False, "why": "Humble offers 2 PDF files, and the largest was taken"}], lambda q: True)
    library = ["--library", str(tmp_path)]
    cli.main(["decide", "--json", *library])
    (q,) = json.loads(capsys.readouterr().out)["questions"]
    for argv in ([str(10**30), "1"], ["-5", "1"], ["x", "1"], [str(q["id"]), "9" * 5000], [str(q["id"]), "nothing.pdf"]):
        with pytest.raises(SystemExit) as wrong:
            cli.main(["decide", *argv, *library])
        assert wrong.value.code == exits.USAGE, argv[0][:20]
        assert "Traceback" not in capsys.readouterr().err
    with pytest.raises(SystemExit) as refused:
        cli.main(["decide", str(q["id"]), "1", "--offered", "0" * 12, *library])
    assert refused.value.code == exits.NEEDS_YOU and "offers other options now" in capsys.readouterr().err
    assert curation.load_lines(tmp_path) == []
    cli.main(["decide", str(q["id"]), "FR.PDF", "--offered", q["offered"], "--json", *library])
    said = json.loads(capsys.readouterr().out)
    assert (said["field"], said["answer"]["file"]) == ("file", "fr.pdf")
    assert said["did"] == "keeps fr.pdf as its file: the next download fetches it, if it isn't there"
    assert curation.files(tmp_path) == {"fieldrules": "fr.pdf"}


# ---- the catalog's questions: same book?, right match?, which title?, files in a folder -------------------------------

def catalog_questions(root, kind: str | None = None, answered: bool = False) -> list[dict]:
    """The catalog's questions, of every kind, the quiet ones too (a lookup's near matches)."""
    return [q for q in curation.questions(root, answered=answered, kinds=set(curation.QUESTIONS))
            if q["shop"] == curation.CATALOG and (kind is None or q["kind"] == kind)]


def moonwakes(root) -> list[dict]:
    return [i for i in records(root) if i["t"] == "Moonwake Saga"]


def test_same_book_is_asked_only_where_the_rule_is_unsure():
    """Same book?: two cards with the same title words that merge_copies keeps apart, where its rule is
    unsure (a title one doesn't confirm, a short one, kinds that don't join), and never where it is sure (manuals, authors
    with no surname in common), nor where three cards share the words (a magazine's issues). The guess is apart."""
    def asked(*copies):
        merged = catalog.merge_copies(list(copies))
        return [(q["item"], q["why"].split(":")[0]) for q in catalog.copies_to_ask(list(copies), merged, [])]

    road = rec("The Lantern Road", "shelf", 1, a=["Jo Brennan"])
    assert asked(road, rec("The Lantern Road", "stacks", 2, a=["Jo Brennan"], trust=False)) == [
        ("shelf:The Lantern Road:1|stacks:The Lantern Road:2", "the title of one isn't in its file's name")]
    assert asked(rec("Moon", "shelf", 1), rec("Moon", "stacks", 2))[0][1] == "its title is short"
    assert asked(rec("Moonwake Saga", "comics", 1, k="comic"), rec("Moonwake Saga", "shelf", 2))[0][1] == \
        "one is a comic, the other a book"  # kinds that don't join
    assert asked(road, rec("The Lantern Road", "stacks", 2, a=["Jo Brennan"])) == []  # one card: the rule merged them
    assert asked(rec("Campaign Map", "manuals", 1, k="manual"), rec("Campaign Map", "manuals", 2, k="manual")) == []
    assert asked(rec("Gathered Verses", "shelf", 1, a=["Ann Poet"], trust=False),
                 rec("Gathered Verses", "stacks", 2, a=["Ben Verse"])) == []
    assert asked(*(rec("Monthly Games Magazine", "shelf", n, ext="pdf", trust=False) for n in (1, 2, 3))) == []
    two = rec("The Lantern Road", "stacks", 2, trust=False)
    (q,) = catalog.copies_to_ask([road, two], catalog.merge_copies([road, two]), [])
    assert ([o["answer"] for o in q["options"]], q["guess"], [s["id"] for s in q["subjects"]]) == (
        ["same", "apart"], 1, [road["id"], two["id"]])


def test_your_same_or_apart_merges_or_parts_copies_over_the_rules():
    """merge_copies with your answers (same_pairs): the same book is one card whatever its kinds say, the lead as the
    order has it; two apart never share a card, though the rule would merge them. Without any, it merges as it did."""
    comic, book = rec("Moonwake Saga", "comics", 1, k="comic"), rec("Moonwake Saga", "humble", 2)
    assert len(catalog.merge_copies([comic, book])) == 2
    (one,) = catalog.merge_copies([comic, book], ["humble", "comics"], [(comic["id"], book["id"], "same", {})])
    assert one["id"] == book["id"] and one["ids"] == [comic["id"]]  # the shop's copy leads, as the order has it
    a, b = rec("The Lantern Road", "shelf", 1, a=["Jo Brennan"]), rec("The Lantern Road", "stacks", 2, a=["Jo Brennan"])
    assert len(catalog.merge_copies([a, b])) == 1 and len(catalog.merge_copies([a, b], None, [(a["id"], b["id"], "apart", {})])) == 2
    assert catalog.merge_copies([a, b], None, []) == catalog.merge_copies([a, b])


def test_a_same_answer_merges_the_copies_in_every_build_replays_is_taken_back_and_follows_a_rename(library, capsys):
    """A same question found by a build, once (no doubles); answered "the same" it is an edit on both
    copies (op update), and every build merges them, the lead as the order has it, the question answered; a copy
    renamed keeps the answer by its file's name and size; taken back (hylly decide N auto) the copies part and the
    question is open again; apart, kept apart, never asked again while they are the same files."""
    two_moonwakes(library)
    catalog.build(library, log=lambda s: None)
    catalog.build(library, log=lambda s: None)
    (q,) = catalog_questions(library, "same")
    assert len(moonwakes(library)) == 2 and q["options"][q["guess"]]["answer"] == "apart" and "a comic" in q["why"]
    cli.main(["decide", str(q["id"]), "1", "--library", str(library)])
    assert f"(hylly decide {q['id']} auto takes it back)" in capsys.readouterr().out
    last = curation.load_lines(library)[-1]
    assert (last["field"], last["op"], len(last["titles"])) == ("catalog", "update", 2)  # a field of their own
    catalog.build(library, log=lambda s: None)
    (one,) = moonwakes(library)
    assert len(one["f"]) == 2 and catalog_questions(library) == []
    assert [x["id"] for x in catalog_questions(library, answered=True)] == [q["id"]]
    shutil.move(library / "shelf", library / "stacks")  # renamed: its id changes, its file's name and size don't
    catalog.build(library, log=lambda s: None)
    assert len(moonwakes(library)) == 1 and catalog_questions(library, "same") == []
    cli.main(["decide", str(q["id"]), "auto", "--library", str(library)])
    assert (f"Took back your answer to question {q['id']}, Moonwake Saga (" in (out := capsys.readouterr().out)
            and out.endswith(": the next run or hylly catalog asks again where the rules are still unsure.\n"))
    catalog.build(library, log=lambda s: None)
    assert len(moonwakes(library)) == 2
    (again,) = catalog_questions(library, "same")
    curation.answer(library, again["id"], "keep", by="rory")  # apart, the guess
    catalog.build(library, log=lambda s: None)
    catalog.build(library, log=lambda s: None)
    assert len(moonwakes(library)) == 2 and catalog_questions(library) == []  # never asked again


def test_a_same_answer_holds_through_a_build_in_which_both_copies_moved(library):
    """Two copies of one file (its name and size the same) in two folders whose kinds don't join, answered the same
    book: both folders renamed in one build, both ids change, and neither copy alone tells which side it was. The two
    that have that file are the pair, together (same_pairs): one card still, not asked again, the answer standing."""
    two_moonwakes(library)
    shutil.copy(library / "shelf" / "Moonwake Saga.epub", library / "comics" / "Sagas" / "Moonwake Saga.epub")  # one file
    catalog.build(library, log=lambda s: None)
    (q,) = catalog_questions(library, "same")
    curation.answer(library, q["id"], "1", by="rory")
    catalog.build(library, log=lambda s: None)
    (before,) = moonwakes(library)
    shutil.move(library / "shelf", library / "stacks")
    shutil.move(library / "comics" / "Sagas", library / "comics" / "Saga Comics")
    catalog.build(library, log=lambda s: None)
    (one,) = moonwakes(library)
    assert not {one["id"], *one["ids"]} & {before["id"], *before["ids"]} and len(one["f"]) == 2
    assert catalog_questions(library) == [] and [x["id"] for x in catalog_questions(library, answered=True)] == [q["id"]]
    catalog.build(library, log=lambda s: None)  # and in every build after it
    assert len(moonwakes(library)) == 1 and catalog_questions(library) == []


def test_hylly_set_copies_apart_parts_a_card_the_rules_merged_and_auto_lets_the_rules_decide_again(library, capsys):
    """The reverse: a card that is two books is set apart by command (and in its details), no question; it
    stays two cards, never asked about; auto takes it back."""
    shore(library / "collections" / "Shore" / "The Quiet Shore.epub")
    shore(library / "shelf" / "The Quiet Shore.epub")
    catalog.build(library, log=lambda s: None)
    (card,) = [i for i in records(library) if i["t"] == "The Quiet Shore"]
    assert len(card["ids"]) == 1
    cli.main(["set", "copies", "apart", "--search", "quiet shore", "--yes", "--library", str(library)])
    assert "Done for 1 title" in capsys.readouterr().out
    catalog.build(library, log=lambda s: None)
    assert len([i for i in records(library) if i["t"] == "The Quiet Shore"]) == 2 and catalog_questions(library, "same") == []
    cli.main(["set", "copies", "auto", "--search", "quiet shore", "--yes", "--library", str(library)])
    catalog.build(library, log=lambda s: None)
    assert len([i for i in records(library) if i["t"] == "The Quiet Shore"]) == 1
    with pytest.raises(SystemExit) as wrong:
        cli.main(["set", "copies", "together", "--search", "quiet shore", "--yes", "--library", str(library)])
    assert wrong.value.code == exits.USAGE


def test_a_near_miss_is_asked_once_a_use_takes_it_into_every_build_and_none_is_kept(library, capsys):
    """Right match?: a lookup's near miss above the floor (lookups.NEAR) is one question for the title,
    each source's match and none of these (the guess: none used). Use it: that match fills the title in every build,
    an edit, until taken back; none: refused, kept, the question gone for good. One under the floor isn't asked."""
    near_miss(library, "s:7", 0.7)
    near_miss(library, "h:novel", 0.4, match="A Novel Elsewhere")  # under the floor
    catalog.build(library, log=lambda s: None)
    (q,) = catalog_questions(library, "match")
    assert (q["catalog_id"], q["options"][0]["source"], q["options"][q["guess"]]["answer"]) == ("s:7", "openlibrary", "none")
    sb = lambda: next(i for i in records(library) if i["id"] == "s:7")  # noqa: E731
    assert "lk" not in sb()
    done = curation.answer(library, q["id"], "1", by="rory")
    assert done["did"] == "uses Open Library's details for it from the next hylly catalog"
    catalog.build(library, log=lambda s: None)
    assert sb()["lk"][0][0] == "Open Library" and "About Arcade Worlds II" in sb()["sub"]  # what its file lacks
    assert catalog_questions(library) == []
    catalog.build(library, log=lambda s: None)
    assert "About Arcade Worlds II" in sb()["sub"]  # replayed: in every build
    curation.answer(library, q["id"], "auto", by="rory")
    catalog.build(library, log=lambda s: None)
    assert "lk" not in sb()
    (again,) = catalog_questions(library, "match")
    assert again["id"] == q["id"]  # the same question, open again
    curation.answer(library, again["id"], "keep", by="rory")  # none of these
    assert curation.lookup_answers(curation.load(library)) == {"s:7": {"openlibrary": {"a": "refused", "m": q["options"][0]["m"]}}}
    catalog.build(library, log=lambda s: None)
    assert "lk" not in sb() and catalog_questions(library) == []


def test_a_used_match_that_is_wrong_is_refused_by_command_and_auto_takes_it_back(library, capsys):
    """A match the build used (scored at or above accept) and that is another book is refused by an edit,
    hylly set refused SOURCE, in place of the refused line once written into the cache by hand: its answer isn't used,
    nor asked again; auto takes the refusal back."""
    near_miss(library, "s:7", 0.9)
    catalog.build(library, log=lambda s: None)
    sb = lambda: next(i for i in records(library) if i["id"] == "s:7")  # noqa: E731
    assert sb()["lk"][0][0] == "Open Library"
    cli.main(["set", "refused", "Open Library", "--id", "s:7", "--yes", "--library", str(library)])
    assert "the sources refused aren't asked about them again" in capsys.readouterr().out
    catalog.build(library, log=lambda s: None)
    assert "lk" not in sb() and "About Arcade Worlds II" not in sb().get("sub", []) and catalog_questions(library, "match") == []
    with pytest.raises(SystemExit) as wrong:
        cli.main(["set", "refused", "nosuchsource", "--id", "s:7", "--yes", "--library", str(library)])
    assert wrong.value.code == exits.USAGE
    cli.main(["set", "refused", "auto", "--id", "s:7", "--yes", "--library", str(library)])
    catalog.build(library, log=lambda s: None)
    assert sb()["lk"][0][0] == "Open Library"


def test_a_place_question_puts_tags_on_the_title_answered_forgets_them_or_keeps_them_waiting(library, capsys):
    """Which title?: tags and corrections that lost their title (a file renamed to another name, which a
    build can't follow) are a question, its options the titles that may be it (new, of the same size, alike in name:
    best first), keep waiting (the guess) and forget them. Answered with a title: a reattach edit, the tags on it; taken
    back: off it, waiting again. Forget: gone from the waiting, and back when taken back. Keep: not asked again while the
    same titles are offered."""
    tale = library / "shelf" / "Lantern Tale.epub"
    tale.parent.mkdir(parents=True)
    make_epub(tale, OPF.replace("The Long Dust", "Lantern Tale"))
    catalog.build(library, log=lambda s: None)
    old = next(i for i in records(library) if i["t"] == "Lantern Tale")
    curation.change(library, "add", "Keep", [old], by="test")
    tale.rename(library / "shelf" / "Lantern Tale (Ann Writer).epub")  # another name: no build follows it
    catalog.build(library, log=lambda s: None)
    now = next(i for i in records(library) if i["t"] == "Lantern Tale")
    (q,) = catalog_questions(library, "place")
    assert (q["catalog_id"], [o["answer"] for o in q["options"]], q["guess"]) == (old["id"], ["to", "keep", "forget"], 1)
    assert q["options"][0]["id"] == now["id"] and not now.get("tg")
    curation.answer(library, q["id"], "1", by="rory")
    assert next(i for i in records(library) if i["id"] == now["id"])["tg"] == ["Keep"]  # at once: an edit, laid over
    catalog.build(library, log=lambda s: None)
    assert catalog_questions(library) == [] and [x["id"] for x in catalog_questions(library, answered=True)] == [q["id"]]
    curation.answer(library, q["id"], "auto", by="rory")  # taken back: off it, waiting again
    assert not next(i for i in records(library) if i["id"] == now["id"]).get("tg")
    assert curation.load(library).of([old["id"]]) == ["Keep"]
    catalog.build(library, log=lambda s: None)
    (q,) = catalog_questions(library, "place")
    curation.answer(library, q["id"], "3", by="rory")  # forget them
    catalog.build(library, log=lambda s: None)
    assert catalog_questions(library) == [] and curation.load(library).of([old["id"]]) == []
    curation.answer(library, q["id"], "auto", by="rory")
    assert curation.load(library).of([old["id"]]) == ["Keep"]  # brought back, exactly
    catalog.build(library, log=lambda s: None)
    (q,) = catalog_questions(library, "place")
    curation.answer(library, q["id"], "keep", by="rory")
    catalog.build(library, log=lambda s: None)
    assert catalog_questions(library) == []  # waiting, not asked again


def test_a_place_answer_taken_back_takes_the_tags_off_a_title_whose_id_had_moved_on_before(library):
    """A title whose id a build had moved on (its folder renamed: its tags followed) and which is a title again later
    (a book put where it was) may be the answer to where a title gone went. Taken back, the tags come off that very
    title, not off the one the earlier move led to: never left on both."""
    lantern, moon = library / "shelf" / "Lantern Tale.epub", library / "nook" / "Moon Tale.epub"
    for book, title in ((lantern, "Lantern Tale"), (moon, "Moon Tale")):
        book.parent.mkdir(parents=True)
        make_epub(book, OPF.replace("The Long Dust", title))
    catalog.build(library, log=lambda s: None)
    by_title = lambda t: next(i for i in records(library) if i["t"] == t)  # noqa: E731
    old_lantern, gone = by_title("Lantern Tale"), by_title("Moon Tale")
    curation.change(library, "add", "Old", [old_lantern], by="test")
    curation.change(library, "add", "Keep", [gone], by="test")
    shutil.move(library / "shelf", library / "stacks")  # the tag Old follows it: a reattach of its id
    catalog.build(library, log=lambda s: None)
    assert by_title("Lantern Tale")["tg"] == ["Old"] and by_title("Lantern Tale")["id"] != old_lantern["id"]
    lantern.parent.mkdir()
    shutil.move(moon, lantern)  # Moon Tale under the name and in the place Lantern Tale had: that id is a title again
    catalog.build(library, log=lambda s: None)
    back = next(i for i in records(library) if i["id"] == old_lantern["id"])
    (q,) = catalog_questions(library, "place")
    assert q["catalog_id"] == gone["id"] and q["options"][0]["id"] == back["id"]
    curation.answer(library, q["id"], "1", by="rory")
    assert next(i for i in records(library) if i["id"] == back["id"])["tg"] == ["Keep"]
    curation.answer(library, q["id"], "auto", by="rory")
    tags = {i["t"]: i.get("tg") for i in records(library)}
    assert not next(i for i in records(library) if i["id"] == back["id"]).get("tg") and tags["Lantern Tale"] == ["Old"]
    assert curation.load(library).of([gone["id"]]) == ["Keep"]  # waiting again, on the title gone alone


def test_a_place_answer_taken_back_gives_the_title_gone_back_every_tag_it_had_decided(library):
    """Taken back, a title gone has again all it had decided about its tags (not to be suggested again), those the
    title it was put on had decided too among them; that title keeps its own."""
    tale = library / "shelf" / "Lantern Tale.epub"
    tale.parent.mkdir(parents=True)
    make_epub(tale, OPF.replace("The Long Dust", "Lantern Tale"))
    catalog.build(library, log=lambda s: None)
    old = next(i for i in records(library) if i["t"] == "Lantern Tale")
    curation.change(library, "add", "Keep", [old], by="test")
    tale.rename(library / "shelf" / "Lantern Tale (Ann Writer).epub")  # another name: no build follows it
    catalog.build(library, log=lambda s: None)
    now = next(i for i in records(library) if i["t"] == "Lantern Tale")
    curation.change(library, "add", "Keep", [now], by="test")  # its own, as well
    (q,) = catalog_questions(library, "place")
    curation.answer(library, q["id"], "1", by="rory")
    curation.answer(library, q["id"], "auto", by="rory")
    edits = curation.load(library)
    assert edits.of([old["id"]]) == edits.of([now["id"]]) == ["Keep"]
    assert edits.decided_of([old["id"]]) == edits.decided_of([now["id"]]) == {"keep"}


def test_files_of_a_kind_the_rules_don_t_take_are_one_question_a_folder_and_books_takes_them_in(library, capsys):
    """Files in a folder: files of a suffix the rules don't take as books, in one of your folders, are one
    question for the folder and the suffix, never one each; never asked of a hidden file, the systems' own, a picture
    beside a book, what tools keep beside books, a format the rules know. Books: that folder's books from then on (an
    edit, data: its answers); not books: passed over, never asked again; taken back, as before."""
    shelf = library / "shelf"
    shelf.mkdir()
    for n in range(3):
        (shelf / f"Ann Writer - Tale {n}.xyz").write_bytes(b"x" * (n + 10))
    for name in ("cover.jpg", "._Ann Writer - Tale 0.xyz", "Thumbs.db", "desktop.ini", "metadata.opf", "readme.txt", "song.mp3", "setup.exe"):
        (shelf / name).write_bytes(b"x")
    catalog.build(library, log=lambda s: None)
    catalog.build(library, log=lambda s: None)
    (q,) = catalog_questions(library, "files")
    assert (q["title"], q["file"], q["catalog_id"], q["guess"]) == ("3 .xyz files in Shelf", ".xyz", "", 0)
    curation.answer(library, q["id"], "2", by="rory")  # books
    catalog.build(library, log=lambda s: None)
    tales = sorted(i["t"] for i in records(library) if i["src"] == "shelf")
    assert tales == ["Tale 0", "Tale 1", "Tale 2"] and catalog_questions(library) == []
    assert [e for e in curation.history(library) if e["field"] == "catalog"][0]["said"] == "answered files in a folder (.xyz): books"
    curation.answer(library, q["id"], "auto", by="rory")
    catalog.build(library, log=lambda s: None)
    assert not [i for i in records(library) if i["src"] == "shelf"]
    (q,) = catalog_questions(library, "files")
    curation.answer(library, q["id"], "keep", by="rory")  # not books
    catalog.build(library, log=lambda s: None)
    assert catalog_questions(library) == [] and not [i for i in records(library) if i["src"] == "shelf"]


def test_hylly_decide_lists_the_answered_and_takes_one_back_alone(library, capsys):
    """hylly decide --answered lists the questions answered, their answers marked, and hylly decide
    N auto takes that one answer back alone (another of the same title stays), after which the next build asks again."""
    near_miss(library, "s:7", 0.7)
    shelf = library / "shelf"
    shelf.mkdir()
    (shelf / "a.xyz").write_bytes(b"x")
    (shelf / "b.qqq").write_bytes(b"x")
    catalog.build(library, log=lambda s: None)
    found = {q["kind"] + q["file"]: q for q in catalog_questions(library)}
    for q in found.values():
        curation.answer(library, q["id"], "keep", by="rory")
    capsys.readouterr()
    cli.main(["decide", "--answered", "--library", str(library)])
    out = capsys.readouterr().out
    assert out.startswith("2 questions answered, from the catalog:") and "<- your answer (rory" in out  # match: quiet
    assert "hylly decide NUMBER auto takes one answer back" in out
    cli.main(["decide", str(found["files.xyz"]["id"]), "auto", "--library", str(library)])
    assert curation.folder_answers(curation.load(library)) == {("shelf", ".qqq"): "not"}  # the other answer stays
    catalog.build(library, log=lambda s: None)
    assert [q["file"] for q in catalog_questions(library)] == [".xyz"]
    with pytest.raises(SystemExit) as wrong:
        cli.main(["decide", str(found["files.xyz"]["id"]), "auto", "--library", str(library)])
    assert wrong.value.code == exits.USAGE and "has an answer to take back" in capsys.readouterr().err
    cli.main(["decide", "--answered", "--json", "--library", str(library)])
    said = json.loads(capsys.readouterr().out)
    assert said["answered"] and sorted(q["kind"] for q in said["questions"]) == ["files"]
    cli.main(["decide", "--answered", "--kind", "match", "--json", "--library", str(library)])
    assert [q["kind"] for q in json.loads(capsys.readouterr().out)["questions"]] == ["match"]  # the quiet kind, asked for


def test_a_download_answer_taken_back_alone_leaves_the_title_s_other_answers(tmp_path):
    """At the downloads: two answers of one title, each taken back by its question alone, the other kept; and
    the answered list keeps a question while its answer stands, though no run finds it."""
    curation.keep_questions(tmp_path, "drivethrurpg", [a_question("71", file="0"), a_question("71", file="1")], lambda q: True)
    first, second = curation.questions(tmp_path)
    curation.answer(tmp_path, first["id"], "2", by="rory")
    curation.answer(tmp_path, second["id"], "2", by="rory")
    curation.keep_questions(tmp_path, "drivethrurpg", [], lambda q: True)  # settled: found no more, but answered
    assert [q["id"] for q in curation.questions(tmp_path, answered=True)] == [first["id"], second["id"]]
    curation.answer(tmp_path, first["id"], "auto", by="rory")
    assert set(curation.answers(tmp_path)["d:71"]) == {"moved:1"}
    assert [q["id"] for q in curation.questions(tmp_path)] == [first["id"]]  # open again, until a run finds it or not
    curation.change_single(tmp_path, "answers", "auto", [{"id": "d:71", "t": "", "f": [[""]]}], by="rory")
    curation.keep_questions(tmp_path, "drivethrurpg", [], lambda q: True)
    assert curation.questions(tmp_path, answered=True) == []  # its answer taken back otherwise: gone with the next run


def test_undo_brings_back_a_format_in_its_place_so_a_file_chosen_after_it_stays(tmp_path):
    """A format set, then a file answer, then the format's auto, then the page's undo: exactly as before the auto,
    the file chosen after the format still the one kept (the undo's format is as old as it was)."""
    title = [{"id": "h:guide", "t": "Field Guide", "f": [[""]]}]
    curation.change_single(tmp_path, "format", "epub", title, by="rory")
    curation.change_single(tmp_path, "file", "guide.pdf", title, by="rory")
    assert curation.files(tmp_path) == {"guide": "guide.pdf"}
    curation.change_single(tmp_path, "format", "auto", title, by="rory")
    curation.change_single(tmp_path, "format", "epub", title, by="rory", undo=True)
    assert curation.files(tmp_path) == {"guide": "guide.pdf"} and curation.formats(tmp_path) == {"guide": "epub"}
    curation.change_single(tmp_path, "format", "auto", title, by="rory")
    curation.change_single(tmp_path, "format", "epub", title, by="rory")  # set again, not an undo: newer, so it wins
    assert curation.files(tmp_path) == {}


def test_a_file_answer_s_format_is_its_option_s_not_its_name_s(tmp_path):
    """A Humble file listed without an extension, chosen by its question, has the format its option said: a
    format of that sort set later keeps it, one of another sort overrides it."""
    curation.keep_questions(tmp_path, "humble", [
        {"kind": "which-file", "item": "guide", "file": "", "title": "Field Guide", "guess": 1, "held": False,
         "options": [curation.file_option("guide_print", "pdf", 50), curation.file_option("guide_hq", "pdf", 90)]}],
        lambda q: True)
    (q,) = curation.questions(tmp_path)
    curation.answer(tmp_path, q["id"], "1", by="rory")
    title = [{"id": "h:guide", "t": "Field Guide", "f": [[""]]}]
    curation.change_single(tmp_path, "format", "pdf", title, by="rory")
    assert curation.files(tmp_path) == {"guide": "guide_print"}  # its format, pdf, as the option had it
    curation.change_single(tmp_path, "format", "epub", title, by="rory")
    assert curation.files(tmp_path) == {}


def test_recent_changes_say_the_file_and_answers_edits_in_words(tmp_path):
    """The library page's Recent changes lists the edits a question's answer makes, in words, with what was
    before (for its Undo)."""
    curation.keep_questions(tmp_path, "humble", [
        {"kind": "which-file", "item": "guide", "file": "", "title": "Field Guide", "guess": 1, "held": False,
         "options": [curation.file_option("guide.pdf", "pdf", 50), curation.file_option("guide_hq.pdf", "pdf", 90)]}],
        lambda q: True)
    curation.keep_questions(tmp_path, "drivethrurpg", [a_question("71")], lambda q: True)
    which, moved = curation.questions(tmp_path)
    curation.answer(tmp_path, which["id"], "1", by="rory")
    curation.answer(tmp_path, moved["id"], "2", by="rory")
    said = [(e["field"], e["said"], e["was"]) for e in curation.history(tmp_path)]
    assert said == [("answers", "answered a file gone (0): moved by hand", {"d:71": None}),
                    ("file", "chose guide.pdf as the file to keep", {"h:guide": None})]


def test_two_copies_answers_replay_follow_a_reattach_and_find_a_copy_whose_id_changed():
    """An answer about two copies (op update, on both) replays, follows a title whose id a reattach moved, as any edit
    does, and finds a copy whose id changed by its first file's name and size (same_pairs)."""
    entry = {"a": "same", "w": {"f:a": ["moonwake.epub", 3]}}
    edits = curation.Edits([
        {"field": "catalog", "op": "update", "value": {"same:f:a|h:b": entry}, "titles": [{"id": "f:a"}, {"id": "h:b"}]},
        {"op": "reattach", "ids": {"f:a": "f:a2"}}])
    assert curation.same_answers(edits) == {"same:f:a|h:b": entry} and "same:f:a|h:b" in edits.fixed["f:a2"]["catalog"]
    pre = [{"id": "f:a2", "t": "Moonwake", "f": [["shelf/moonwake.epub", "epub", 3]]}, {"id": "h:b", "t": "Moonwake", "f": []}]
    assert catalog.same_pairs(pre, curation.same_answers(edits)) == [("f:a2", "h:b", "same", entry, "same:f:a|h:b")]
    assert catalog.same_pairs(pre[1:], curation.same_answers(edits)) == []  # one side gone: no pair


# ---- the questions, case by case: near matches, files never asked about, answers taken back --------------------------

def test_the_lookups_near_matches_are_quiet_counted_apart_and_listed_by_their_kind(library, capsys):
    """A lookup's near match is found and kept as any question, but left out of hylly decide's list and the counts
    (the build's line, doctor's), which end with a short line of their own; hylly decide --kind match lists them, and
    --kind any one kind."""
    from hylly import doctor

    near_miss(library, "s:7", 0.7)
    (library / "shelf").mkdir()
    (library / "shelf" / "a.xyz").write_bytes(b"x")
    said = []
    catalog.build(library, log=said.append)
    assert "  1 question to decide (new): hylly decide lists them and takes your answers." in said
    assert "  and 1 near match from the lookups, in its title's details (hylly decide --kind match)." in said
    assert [q["kind"] for q in curation.questions(library)] == ["files"]  # as it comes: the quiet kind left out
    capsys.readouterr()
    cli.main(["decide", "--library", str(library)])
    out = capsys.readouterr().out
    assert out.startswith("1 question to decide, from the catalog:") and "Arcade Worlds" not in out
    assert out.endswith("and 1 near match from the lookups, in its title's details (hylly decide --kind match).\n")
    cli.main(["decide", "--kind", "match", "--library", str(library)])
    out = capsys.readouterr().out
    assert out.startswith("1 question to decide, from the catalog:") and "Arcade Worlds" in out and "near match" not in out
    cli.main(["decide", "--kind", "files", "--json", "--library", str(library)])
    assert [q["kind"] for q in json.loads(capsys.readouterr().out)["questions"]] == ["files"]
    (check,) = doctor._questions(library)
    assert check["says"] == "1 question to decide from the catalog."
    (match,) = catalog_questions(library, "match")
    cli.main(["decide", str(match["id"]), "--library", str(library)])  # by its number: any kind
    assert "Arcade Worlds" in capsys.readouterr().out


def test_hylly_decide_s_kinds_are_the_questions_own():
    """hylly decide --kind takes every kind of question there is, and only those."""
    from hylly import commands

    parser = commands.build_parser()
    decide = next(a for a in parser._subparsers._group_actions[0].choices["decide"]._actions if a.dest == "kind")
    assert set(decide.choices) == set(curation.QUESTIONS)


def test_the_reading_apps_sidecars_a_stylesheet_and_what_is_in_a_sdr_folder_are_never_asked_about(library):
    """Never asked: a Kindle's .mbp, .mbp1, .mbs, .tan, .apnx, .phl beside a book, a file_id.diz, a
    stylesheet, and what is inside a KOReader .sdr folder; a web page, an office document, a .chm, .pdb or .rar are."""
    shelf = library / "shelf"
    (shelf / "Lantern Wolf.sdr").mkdir(parents=True)
    (shelf / "Lantern Wolf.sdr" / "metadata.epub.lua").write_bytes(b"-- KOReader's")
    for name in ("Lantern Wolf.mbp", "Lantern Wolf.mbp1", "Lantern Wolf.mbs", "Lantern Wolf.tan", "Lantern Wolf.apnx",
                 "Lantern Wolf.phl", "file_id.diz", "style.css", "Lantern Wolf.htm", "Lantern Wolf.chm", "Notes.odt"):
        (shelf / name).write_bytes(b"x")
    catalog.build(library, log=lambda s: None)
    assert sorted(q["file"] for q in catalog_questions(library, "files")) == [".chm", ".htm", ".odt"]


def test_a_folder_read_only_in_part_keeps_its_open_files_questions(library, monkeypatch):
    """A subfolder that can't be read just now, with no titles in it, is passed over, and its folder's open files
    questions stay, with their numbers, as the build couldn't look at all of the folder."""
    stacks = library / "stacks"
    (stacks / "deep").mkdir(parents=True)
    (stacks / "deep" / "only.odt").write_bytes(b"an odt alone in a folder")
    catalog.build(library, log=lambda s: None)
    (q,) = catalog_questions(library, "files")
    deep, real = stacks / "deep", catalog.os.scandir

    def stalled(path="."):  # as the walk lists each folder
        if Path(path) == deep:
            raise PermissionError(13, "Access is denied", str(path))
        return real(path)

    with monkeypatch.context() as m:
        m.setattr(catalog.os, "scandir", stalled)
        said = []
        catalog.build(library, log=said.append)
    assert any("Passed over 1 folder" in line for line in said)
    assert [x["id"] for x in catalog_questions(library, "files")] == [q["id"]]
    catalog.build(library, log=lambda s: None)
    assert [x["id"] for x in catalog_questions(library, "files")] == [q["id"]]


def test_a_suffix_taken_as_books_never_leads_a_title_that_has_a_file_of_the_rules_own_formats(library):
    """Lantern Pair.abc beside Lantern Pair.epub, .abc answered books: it joins the EPUB's title, whose id, first file
    and cover stay; it never leads it, though its name sorts first."""
    stacks = library / "stacks"
    stacks.mkdir()
    make_epub(stacks / "Lantern Pair.epub", OPF.replace("The Long Dust", "Lantern Pair"))
    (stacks / "Lantern Pair.abc").write_bytes(b"the same book, another file")
    catalog.build(library, log=lambda s: None)
    before = next(i for i in records(library) if i["t"] == "Lantern Pair")
    (q,) = catalog_questions(library, "files")
    curation.answer(library, q["id"], "2", by="rory")  # books
    catalog.build(library, log=lambda s: None)
    after = next(i for i in records(library) if i["t"] == "Lantern Pair")
    assert after["id"] == before["id"] and after["f"][0] == before["f"][0] and after.get("c") == before.get("c")
    assert [unquote(f[0]).rsplit(".", 1)[1] for f in after["f"]] == ["epub", "abc"]


def test_taking_back_books_is_no_build_refused_as_shrunk(library):
    """A folder of titles only your books answer took in: taken back, they leave by your own word, and the next build
    is no build refused as having far fewer titles."""
    shelf = library / "shelf"
    shelf.mkdir()
    for n in range(14):
        (shelf / f"Ann Writer - Tale {n:02}.xyz").write_bytes(b"x" * (n + 10))
    catalog.build(library, log=lambda s: None)
    (q,) = catalog_questions(library, "files")
    curation.answer(library, q["id"], "2", by="rory")
    catalog.build(library, log=lambda s: None)
    assert len([i for i in records(library) if i["src"] == "shelf"]) == 14
    curation.answer(library, q["id"], "auto", by="rory")
    catalog.build(library, log=lambda s: None)  # no Exit NEEDS_YOU: nothing is missing that you didn't take back
    assert not [i for i in records(library) if i["src"] == "shelf"]


def test_a_detach_after_the_tag_went_or_the_title_moved_on_replays_without_fail():
    """A reattach taken back after the tag it brought was removed replays (no KeyError: a tag is kept as None); and
    one taken back after the title moved on takes what came back from where it is now, not leaving it on both."""
    tag = lambda op, i: {"field": "tag", "op": op, "value": "Keep", "titles": [{"id": i}]}  # noqa: E731
    edits = curation.Edits([tag("add", "f:a"), {"op": "reattach", "ids": {"f:a": "f:b"}}, tag("remove", "f:b"),
                            {"op": "detach", "ids": {"f:a": "f:b"}}])
    assert edits.of(["f:a"]) == ["Keep"] and edits.of(["f:b"]) == []
    edits = curation.Edits([tag("add", "f:a"), {"op": "reattach", "ids": {"f:a": "f:b"}},
                            {"op": "reattach", "ids": {"f:b": "f:c"}}, {"op": "detach", "ids": {"f:a": "f:b"}}])
    assert edits.of(["f:a"]) == ["Keep"] and edits.of(["f:c"]) == [] and edits.of(["f:b"]) == []


def test_any_sequence_of_edits_replays_without_fail():
    """Random sequences of tags, corrections, reattaches, detaches, forgets, answers and
    undos replay, and what is read of them reads, without an exception."""
    import random

    ids = ["f:a", "f:b", "f:c", "h:d"]

    def one(r):
        i, j = r.sample(ids, 2)
        k = r.randrange(12)
        if k < 3:
            return {"field": "tag", "op": r.choice(curation.OPS), "value": r.choice(["Keep", "Old"]), "titles": [{"id": i}]}
        if k == 3:
            return {"field": "kind", "op": r.choice(["set", "clear"]), "value": "tech", "titles": [{"id": i}]}
        if k == 4:
            return {"op": "reattach", "ids": {i: j}}
        if k == 5:
            return {"op": "detach", "ids": {i: j}}
        if k == 6:
            return {"op": r.choice(["forget", "unforget"]), "ids": [i]}
        if k == 7:
            return {"field": "catalog", "op": "update", "value": {"same:f:a|f:b": r.choice([{"a": "apart"}, None])},
                    "titles": [{"id": i}, {"id": j}]}
        if k == 8:
            return {"field": "format", "op": "set", "value": "pdf", "titles": [{"id": i}], **({"undo": True} if r.random() < .5 else {})}
        if k == 9:
            return {"field": "file", "op": r.choice(["set", "clear"]), "value": "x.epub", "titles": [{"id": i}],
                    **({"format": "epub"} if r.random() < .5 else {})}
        if k == 10:
            return {"field": "answers", "op": r.choice(["set", "clear"]), "value": {"moved:0": {"a": "moved", "o": "x"}},
                    "titles": [{"id": i}]}
        return {"field": "catalog", "op": r.choice(["set", "clear"]), "value": {"lookup:openlibrary": {"a": "refused"}},
                "titles": [{"id": i}]}

    for seed in range(3000):
        r = random.Random(seed)
        edits = curation.Edits([one(r) for _ in range(r.randrange(2, 14))])
        for i in ids:
            edits.overridden(i), edits.in_effect([i], "file"), edits.of([i]), edits.now(i)
        curation.same_answers(edits), curation.lookup_answers(edits)


def test_hylly_set_copies_apart_keeps_each_copys_first_file_so_it_holds_after_a_move(library, capsys):
    """hylly set copies apart (and Not one book) keeps each folder copy's first file's name and size, as a question's
    answer does: once a copy moves, the two stay apart."""
    shore(library / "collections" / "Shore" / "The Quiet Shore.epub")
    shore(library / "shelf" / "The Quiet Shore.epub")
    catalog.build(library, log=lambda s: None)
    cli.main(["set", "copies", "apart", "--search", "quiet shore", "--yes", "--library", str(library)])
    (entry,) = curation.same_answers(curation.load(library)).values()
    assert sorted(name for name, _ in entry["w"].values()) == ["The Quiet Shore.epub", "The Quiet Shore.epub"]
    catalog.build(library, log=lambda s: None)
    assert len([i for i in records(library) if i["t"] == "The Quiet Shore"]) == 2
    shutil.move(library / "shelf", library / "stacks")  # the shelf copy's id changes; its file's name and size don't
    catalog.build(library, log=lambda s: None)
    assert len([i for i in records(library) if i["t"] == "The Quiet Shore"]) == 2 and catalog_questions(library, "same") == []


def which_file(root, *names: str) -> dict:
    curation.keep_questions(root, "humble", [
        {"kind": "which-file", "item": "guide", "file": "", "title": "Field Guide", "guess": len(names) - 1, "held": False,
         "options": [curation.file_option(name, "pdf", 50 + n) for n, name in enumerate(names)]}], lambda q: True)
    (q,) = [q for q in curation.questions(root) if q["kind"] == "which-file"]
    return q


def test_a_file_answer_taken_back_puts_back_the_file_before_in_its_place(tmp_path):
    """The take-back of a file answer is exact. A file chosen before a format of another
    sort (which overrides it) stays overridden once the answer is taken back, so the next download is as before."""
    title = [{"id": "h:guide", "t": "Field Guide", "f": [[""]]}]
    curation.change_single(tmp_path, "file", "guide_old.epub", title, by="rory")
    curation.change_single(tmp_path, "format", "pdf", title, by="rory")
    assert curation.files(tmp_path) == {}  # the format wins over the file chosen before it
    q = which_file(tmp_path, "guide_print", "guide_hq")
    curation.answer(tmp_path, q["id"], "1", by="rory")
    assert curation.files(tmp_path) == {"guide": "guide_print"}
    curation.answer(tmp_path, q["id"], "auto", by="rory")
    assert curation.files(tmp_path) == {} and curation.formats(tmp_path) == {"guide": "pdf"}  # as before the answer


def test_a_file_answer_taken_back_puts_back_the_file_before_with_its_format(tmp_path):
    """A file listed without an extension, chosen before, comes back with the format its
    option had, so a format of that sort set later doesn't override it."""
    title, other = [{"id": "h:guide", "t": "Field Guide", "f": [[""]]}], tmp_path
    curation.change_single(other, "file", "guide_print", title, by="rory", also={"format": "pdf"})  # as an answer keeps it
    q = which_file(other, "guide_hq", "guide_big")  # the guess: guide_big; the answer another file, guide_hq
    curation.answer(other, q["id"], "1", by="rory")
    assert curation.files(other) == {"guide": "guide_hq"}
    curation.answer(other, q["id"], "auto", by="rory")
    curation.change_single(other, "format", "pdf", title, by="rory")
    assert curation.files(other) == {"guide": "guide_print"}  # its option's format came back with it: pdf agrees


def test_a_used_match_follows_a_title_that_moved_in_the_same_build(library):
    """Your answers about a lookup's match are read after the build has moved what was kept of a title that moved
    (a folder renamed): the very build that moves it shows the match used, not the one after it."""
    from hylly import lookups

    tale = library / "shelf" / "Copper Bell.epub"
    tale.parent.mkdir(parents=True)
    make_epub(tale, OPF.replace("The Long Dust", "Copper Bell").replace("Marlow Ellery", "Ann Writer").replace(
        '<dc:identifier opf:scheme="ISBN">978-0-00-000000-2</dc:identifier>', ""))  # asked by its title, its own key
    catalog.build(library, log=lambda s: None)
    rec = next(i for i in records(library) if i["t"] == "Copper Bell")
    source = lookups.in_order()["openlibrary"]()
    found = {"subjects": ["About Copper Bell"], "score": 0.7, "match": "Copper Bell II", "url": "https://openlibrary.org/works/OL11W"}
    cache = library / "state" / "lookups" / "lookup-openlibrary.jsonl"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"key": source.key(lookups.Query.from_record(rec)), "ids": [rec["id"]], "status": "none",
                                 "at": "2099-01-01T00:00:00", "found": {"near": found}}) + "\n", encoding="utf-8")
    catalog.build(library, log=lambda s: None)
    (q,) = catalog_questions(library, "match")
    curation.answer(library, q["id"], "1", by="rory")
    catalog.build(library, log=lambda s: None)
    assert next(i for i in records(library) if i["t"] == "Copper Bell")["lk"][0][0] == "Open Library"
    shutil.move(library / "shelf", library / "stacks")  # a new id, its file's name and size kept: the build follows it
    catalog.build(library, log=lambda s: None)
    now = next(i for i in records(library) if i["t"] == "Copper Bell")
    assert now["id"] != rec["id"] and now["lk"][0][0] == "Open Library" and catalog_questions(library, "match") == []


def test_an_answered_question_whose_subject_is_gone_leaves_the_answered_list_and_its_edit_stays(library):
    """Two copies answered the same book, both renamed (their files' names too): neither is found any more, and the
    answered question leaves the answered list at the next complete build; the answer's edit stays as it was."""
    two_moonwakes(library)
    catalog.build(library, log=lambda s: None)
    (q,) = catalog_questions(library, "same")
    curation.answer(library, q["id"], "1", by="rory")
    catalog.build(library, log=lambda s: None)
    assert [x["id"] for x in catalog_questions(library, answered=True)] == [q["id"]]
    (library / "comics" / "Sagas" / "Moonwake Saga.epub").rename(library / "comics" / "Sagas" / "Moonwake Saga (comic).epub")
    (library / "shelf" / "Moonwake Saga.epub").rename(library / "shelf" / "Moonwake Saga (book).epub")
    catalog.build(library, log=lambda s: None)
    key = "same:" + "|".join(sorted(s["id"] for s in q["subjects"]))
    assert catalog_questions(library, answered=True) == [] and curation.same_answers(curation.load(library))[key]["a"] == "same"


def test_an_answer_taken_back_another_way_is_off_the_answered_list_and_may_be_answered_again(library, capsys):
    """The answered list shows only the answers that stand. One taken back another way than by its
    question (hylly set ... auto, Recent changes' Undo, an older Hylly's writes) leaves it at once, never shown as your
    answer; it is on neither list until the next build finds it again, but a title's details see it at once (lapsed),
    and it may be answered again."""
    shelf = library / "shelf"
    shelf.mkdir()
    (shelf / "a.xyz").write_bytes(b"x")
    catalog.build(library, log=lambda s: None)
    (q,) = catalog_questions(library, "files")
    curation.answer(library, q["id"], "2", by="rory")  # books
    assert [x["id"] for x in catalog_questions(library, answered=True)] == [q["id"]]
    curation.change_single(library, "catalog", "auto", [{"id": curation.folder_key("shelf"), "t": "Shelf", "f": [[""]]}],
                           by="rory")  # taken back another way: the folder's answers cleared
    assert catalog_questions(library, answered=True) == [] and catalog_questions(library) == []
    assert [x["id"] for x in curation.questions(library, kinds=set(curation.QUESTIONS), lapsed=True)] == [q["id"]]
    capsys.readouterr()
    cli.main(["decide", "--answered", "--library", str(library)])
    assert "<- your answer" not in capsys.readouterr().out
    curation.answer(library, q["id"], "1", by="rory")  # answered anew: not books
    assert [x["answer"]["option"] for x in catalog_questions(library, answered=True)] == [1]
    catalog.build(library, log=lambda s: None)
    assert catalog_questions(library) == [] and [x["id"] for x in catalog_questions(library, answered=True)] == [q["id"]]


def test_the_undo_of_taking_back_a_file_chosen_brings_back_its_format(tmp_path):
    """Recent changes' Undo of "took back the file chosen" (the page sends the file again, undo: true)
    brings back the format its question's option had: a file listed without an extension isn't overridden by a format
    of that sort set later."""
    title = [{"id": "h:guide", "t": "Field Guide", "f": [[""]]}]
    curation.change_single(tmp_path, "file", "guide_print", title, by="rory", also={"format": "pdf"})  # as an answer keeps it
    curation.change_single(tmp_path, "file", "auto", title, by="rory")
    taken = curation.history(tmp_path)[0]
    assert (taken["said"], taken["was"]) == ("took back the file chosen", {"h:guide": "guide_print"})
    curation.change_single(tmp_path, "file", taken["was"]["h:guide"], title, by="rory", undo=True)  # the page's Undo
    curation.change_single(tmp_path, "format", "pdf", title, by="rory")
    assert curation.files(tmp_path) == {"guide": "guide_print"}  # its format came back with it: pdf agrees


def test_recent_changes_say_a_take_back_and_where_a_title_gone_s_tags_went(tmp_path):
    """Recent changes says taking back one answer of a title with others as a take-back, never as answering
    the others; and lists a reattach, a detach, a forget and an unforget in words, with no Undo of their own."""
    curation.keep_questions(tmp_path, "drivethrurpg", [a_question("71", file="0"), a_question("71", file="1")], lambda q: True)
    first, second = curation.questions(tmp_path)
    curation.answer(tmp_path, first["id"], "2", by="rory")
    curation.answer(tmp_path, second["id"], "2", by="rory")
    curation.answer(tmp_path, first["id"], "auto", by="rory")
    assert curation.history(tmp_path)[0]["said"] == "took back the answer about a file gone (0)"
    tag = {"field": "tag", "op": "add", "value": "Keep", "titles": [{"id": "f:a", "t": "Gone Tale"}], "at": "x", "by": "rory"}
    for line in (tag, {"op": "reattach", "ids": {"f:a": "f:b"}, "by": "rory"}, {"op": "detach", "ids": {"f:a": "f:b"}, "by": "rory"},
                 {"op": "forget", "ids": ["f:a"], "by": "rory"}, {"op": "unforget", "ids": ["f:a"], "by": "rory"},
                 {"op": "reattach", "ids": {"f:x": "f:y", "f:z": "f:w"}, "by": "hylly catalog"}):
        with curation.store(tmp_path).write() as c:
            curation._put(c, line)
    said = [(e["op"], e["said"], e.get("undo"), e["titles"]) for e in curation.history(tmp_path)[:5]]
    assert said == [("reattach", "followed 2 titles that moved or were renamed, with their tags and corrections", False, ["", ""]),
                    ("unforget", "brought back the tags and corrections of a title gone", False, ["Gone Tale"]),
                    ("forget", "forgot the tags and corrections of a title gone", False, ["Gone Tale"]),
                    ("detach", "took back where the tags and corrections of a title gone were put", False, ["Gone Tale"]),
                    ("reattach", "put the tags and corrections of a title gone on the title it became", False, ["Gone Tale"])]


# ---- the files a shop list left out (shoplist.py, curation.LEFT) ----------------------------------------------------

def test_files_left_out_by_a_list_are_an_edit_of_their_own_that_replays_undoes_and_follows_its_title(tmp_path):
    """A shop list's unticked rows are kept as an ordinary edit, each file by its key in the title's left-out (a field of
    its own): it replays, follows a title whose id changed (reattach) as any correction does, is undone exactly, key
    by key (undo_update, as the library page's Undo does), and is taken back with auto (hylly set left-out auto); a
    title not downloaded yet with nothing else kept waits with its id, no title gone to tell of."""
    ledger = {"id": "d:71", "t": "Clan Ledger", "f": [[""]]}
    assert curation.leave_out(tmp_path, {"d:71": ("Clan Ledger", {"2": "maps.zip", "3": ""}),
                                         "s:78": ("Second Tide", {"0": ""})}, by=curation.COMMAND_LINE) == 2
    assert curation.left_out(tmp_path) == {"d:71": {"2", "3"}, "s:78": {"0"}}
    last = curation.load_lines(tmp_path)[-1]
    assert (last["field"], last["op"], last["was"]) == ("left-out", "update", {"s:78": None})  # one edit per same change
    curation.leave_out(tmp_path, {"d:71": ("Clan Ledger", {"3": None})}, by=curation.COMMAND_LINE)  # ticked again
    assert curation.left_out(tmp_path) == {"d:71": {"2"}, "s:78": {"0"}}
    undo = curation.history(tmp_path, 1)[0]
    assert undo["said"] == "took back 1 file left out" and undo["field"] == "left-out"
    for entries, ids in curation.undo_update(list(undo["tag"]), undo["was"]):
        curation.change_answers(tmp_path, entries, [{**ledger, "id": i} for i in ids], "owner", field="left-out", undo=True)
    assert curation.left_out(tmp_path) == {"d:71": {"2", "3"}, "s:78": {"0"}}  # exactly as it was
    titles = matching.Titles([{"id": "d:72", "t": "Clan Ledger", "f": [["drivethrurpg/b/ledger.pdf", "pdf", 3]]}])
    titles.follow = lambda old, was: "d:72" if old == "d:71" else None
    assert curation.reattach(tmp_path, titles, log=lambda line: None) == []  # s:78 waits: files left out are no title lost
    assert curation.left_out(tmp_path) == {"d:72": {"2", "3"}, "s:78": {"0"}}
    curation.change_single(tmp_path, "left-out", "auto", [{**ledger, "id": "d:72"}], by=curation.COMMAND_LINE)
    assert curation.left_out(tmp_path) == {"s:78": {"0"}}
    with pytest.raises(SystemExit, match="hylly set answers auto takes them back"):  # only hylly's own codes
        curation.change_answers(tmp_path, {"out:1": {"a": "keep"}}, [ledger], by="rory", field="left-out")


def test_an_older_hylly_reads_and_writes_its_own_edits_as_before_and_never_drops_the_files_left_out(tmp_path, monkeypatch):
    """The files left out are in a field of their own (left-out), so that a Hylly from before them (5fe64fa) reads the
    edits it knows as it always did, ignoring the new one, and its writes of a title's answers leave it as it is."""
    moved = {"moved:2": {"a": "moved", "o": curation.fingerprint(list(curation.MOVED))}}
    ledger = [{"id": "d:71", "t": "Clan Ledger", "f": [[""]]}]
    curation.change_single(tmp_path, "answers", moved, ledger, by="rory")
    curation.leave_out(tmp_path, {"d:71": ("Clan Ledger", {"2": "maps.zip"})}, by=curation.COMMAND_LINE)
    with monkeypatch.context() as older:  # as 5fe64fa had them: no left-out among the fields it knows
        older.setattr(curation, "ANSWERED", ("file", "answers", "catalog"))
        older.setattr(curation, "ENTRIES", ("answers", "catalog"))
        assert curation.answers(tmp_path) == {"d:71": moved} and "left-out" not in curation.load(tmp_path).fixed["d:71"]
        curation.change_single(tmp_path, "answers", "auto", ledger, by="rory")  # its own write: the answers taken back
    assert curation.answers(tmp_path) == {} and curation.left_out(tmp_path) == {"d:71": {"2"}}  # these stay


def test_a_title_of_many_files_left_out_at_once_is_kept_in_a_few_edits_never_stopping_the_run(tmp_path):
    """An edit holds a title's entries up to its bound (answer_value: 500): a list that leaves out more files of one
    title at once is kept in a few edits, each within it, and taken back the same way."""
    files = {str(n): f"page-{n}.cbz" for n in range(900)}
    assert curation.leave_out(tmp_path, {"i:71": ("Lantern Wolf Sketches", files)}, by=curation.COMMAND_LINE) == 1
    assert curation.left_out(tmp_path) == {"i:71": set(files)}
    edits = [e for e in curation.load_lines(tmp_path) if e.get("field") == "left-out"]
    assert len(edits) == 3 and all(len(e["value"]) <= curation.ENTRIES_MOST for e in edits)
    curation.leave_out(tmp_path, {"i:71": ("Lantern Wolf Sketches", {key: None for key in files})}, by=curation.COMMAND_LINE)
    assert curation.left_out(tmp_path) == {}


# ---- changes side by side all reach the journal (once in test_storage.py) --------------------------------------------

def test_changes_from_side_by_side_all_reach_the_journal(tmp_path):
    threads = [threading.Thread(target=curation._append, args=(tmp_path, {"at": "x", "n": n})) for n in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(e["n"] for e in curation.load_lines(tmp_path)) == list(range(20))
