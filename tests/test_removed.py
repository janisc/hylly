"""The files the Server page removes into _Removed (removed.py): never written over, never moved or deleted through a
link or a junction, Empty only once _Removed holds what the page showed, and a file removed taken for no file missing
only when it was removed after its own record was made."""

from __future__ import annotations

import errno
import os
import unicodedata
from urllib.parse import quote

import pytest

from hylly import db, manifest, removed


def card(id_: str, *files: str) -> dict:
    """A card as the catalog has one: its id, its name and its files, each by its place, quoted."""
    return {"id": id_, "t": "Hedge Lore", "f": [[quote(f), f.rsplit(".", 1)[-1], 1] for f in files]}


def write(path, data: bytes = b"made up") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def a_link(link, target) -> None:
    """A symbolic link, where this computer lets a test make one."""
    try:
        os.symlink(target, link, target_is_directory=os.path.isdir(target))
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"no symbolic links here: {e}")


def test_a_removal_never_writes_over_a_file_in_removed_whatever_the_disk_takes_for_one_name(library, monkeypatch):
    """A name taken in _Removed, as the disk itself sees it (Café written with one letter for é, or two, is one name on
    macOS), gets " (2)"; and a name taken just between the look and the move refuses the move, nothing written over, the
    file left where it was. On a disk that keeps no hard links, or another disk reached by copying, the same: a copy goes
    only into a file made new."""
    nfc, nfd = unicodedata.normalize("NFC", "ebooks/Café.epub"), unicodedata.normalize("NFD", "ebooks/Café.epub")
    write(library / nfc, b"the copy")
    write(library / "_Removed" / nfd, b"removed before")
    (moved,), trouble = removed.remove(library, removed.going(library, [card("f:a", nfc)]), by="owner")
    assert trouble == "" and (library / "_Removed" / nfd).read_bytes() == b"removed before" and not (library / nfc).exists()
    assert (library / "_Removed" / moved["held"]).read_bytes() == b"the copy"
    taken = library / "_Removed" / "ebooks" / "Hedge Lore.epub"
    write(library / "ebooks" / "Hedge Lore.epub", b"the copy")
    write(taken, b"removed before")
    with monkeypatch.context() as m:  # its name taken just then: the move is refused
        m.setattr(removed, "_free", lambda path: taken)
        done, trouble = removed.remove(library, removed.going(library, [card("f:b", "ebooks/Hedge Lore.epub")]), by="owner")
    assert done == [] and "took the name it was to have" in trouble
    assert taken.read_bytes() == b"removed before" and (library / "ebooks" / "Hedge Lore.epub").read_bytes() == b"the copy"
    if os.name != "nt":  # a disk that keeps no hard links, and another disk: a rename once nothing has the name, a copy
        for why in (errno.EPERM, errno.EXDEV):  # into a file made new
            def no_link(src, dest, why=why):
                raise OSError(why, os.strerror(why))

            with monkeypatch.context() as m:
                m.setattr(os, "link", no_link)
                with pytest.raises(FileExistsError):
                    removed._new_name(library / "ebooks" / "Hedge Lore.epub", taken)
                assert taken.read_bytes() == b"removed before" and (library / "ebooks" / "Hedge Lore.epub").is_file()
                there = taken.with_name(f"Hedge Lore {why}.epub")
                removed._new_name(library / "ebooks" / "Hedge Lore.epub", there)
                assert there.read_bytes() == b"the copy" and not (library / "ebooks" / "Hedge Lore.epub").exists()
                os.rename(there, library / "ebooks" / "Hedge Lore.epub")


def test_a_link_or_a_junction_in_removed_steers_nothing(library, tmp_path):
    """A folder of _Removed that is a link (into state, say) refuses a removal into it, nothing moved; _Removed that is
    a link itself (into the books, say) refuses removals, the list, Put back and Empty, nothing deleted; and a link
    inside _Removed is a file of its own there, which Empty takes away, never what it leads to."""
    write(library / "ebooks" / "Hedge Lore.epub")
    (library / "_Removed").mkdir()
    a_link(library / "_Removed" / "ebooks", library / "state")
    done, trouble = removed.remove(library, removed.going(library, [card("f:a", "ebooks/Hedge Lore.epub")]), by="owner")
    assert done == [] and "_Removed/ebooks is a link or a junction" in trouble
    assert (library / "ebooks" / "Hedge Lore.epub").is_file() and not (library / "state" / "Hedge Lore.epub").exists()
    (library / "_Removed" / "ebooks").unlink()
    (library / "_Removed").rmdir()
    a_link(library / "_Removed", library / "ebooks")  # _Removed itself leads to the books
    for refused in (lambda: removed.listing(library), lambda: removed.empty(library, "0" * 64),
                    lambda: removed.put_back(library, "Hedge Lore.epub", by="owner")):
        with pytest.raises(removed.Refused, match="_Removed is a link or a junction"):
            refused()
    with pytest.raises(removed.Refused, match="isn't a file of the library that may be removed"):  # ebooks is _Removed
        removed.going(library, [card("f:a", "ebooks/Hedge Lore.epub")])
    planned = [{"path": "ebooks/Hedge Lore.epub", "size": 9, "title": "f:a", "t": "Hedge Lore"}]
    done, trouble = removed.remove(library, planned, by="owner")
    assert done == [] and "is a link or a junction" in trouble and (library / "ebooks" / "Hedge Lore.epub").is_file()
    (library / "_Removed").unlink()
    outside = tmp_path / "elsewhere"
    write(outside / "keep.epub", b"not the library's")
    (library / "_Removed").mkdir()
    a_link(library / "_Removed" / "linked", outside)
    found = removed.listing(library)
    assert [f["held"] for f in found["files"]] == ["linked"] and found["by_hand"] == 1  # a file of its own, not gone into
    assert removed.empty(library, found["digest"])["deleted"] == 1
    assert (outside / "keep.epub").read_bytes() == b"not the library's" and not (library / "_Removed").exists()


def test_empty_deletes_only_what_the_page_showed(library):
    """Each file's place and size, in the listing's digest: a file renamed since, though the count and the size are the
    same, refuses Empty, nothing deleted; and how many were put there by hand is said."""
    write(library / "_Removed" / "ebooks" / "Hedge Lore.epub", b"one")
    write(library / "_Removed" / "ebooks" / "Moonwake.epub", b"two")
    found = removed.listing(library)
    assert (found["count"], found["bytes"], found["by_hand"]) == (2, 6, 2)
    os.rename(library / "_Removed" / "ebooks" / "Moonwake.epub", library / "_Removed" / "ebooks" / "Lantern Wolf.epub")
    assert removed.listing(library)["count"] == 2 and removed.listing(library)["bytes"] == 6
    with pytest.raises(removed.Refused, match="holds other files now than the page showed"):
        removed.empty(library, found["digest"])
    assert sorted(p.name for p in (library / "_Removed" / "ebooks").iterdir()) == ["Hedge Lore.epub", "Lantern Wolf.epub"]
    assert removed.empty(library, removed.listing(library)["digest"])["deleted"] == 2


def test_a_file_removed_counts_as_yours_only_when_it_is_the_file_recorded(library):
    """hylly verify's and hylly add's look at a recorded file missing from its place: put into _Removed by hand at that
    very place, it is yours when it is the file recorded, by its size (and with --hash its MD5), whatever its times say
    (a move keeps them on Windows; a share's own notes set them anew elsewhere), so one recorded anew as another file,
    and lost, is missing though the old one sits there; removed on the Server page, by its removal's time, after the
    file's own record, also once emptied. A shop's file with no answer that it was moved is missing."""
    import hashlib

    rel, later = "ebooks/Hedge Lore.epub", "2099-01-01T00:00:00+00:00"
    copy = {"src": "/elsewhere/Hedge Lore.epub", "dest": rel, "size": 3, "md5": hashlib.md5(b"old").hexdigest(),
            "copied_at": later}  # its record younger than the file put there, as a slow copy's may be
    write(library / "_Removed" / rel, b"old")  # put there by hand
    os.utime(library / "_Removed" / rel, (0, 0))  # its times long before its record: they don't count
    assert removed.by_you(library, {rel: ("", copy)}) == removed.by_you(library, {rel: ("", copy)}, check_hash=True) == {rel}
    again = {**copy, "size": 5, "md5": hashlib.md5(b"anew!").hexdigest()}  # recorded anew as another file, and lost
    assert removed.by_you(library, {rel: ("", again)}) == set()
    same_size = {**copy, "md5": hashlib.md5(b"new").hexdigest()}  # of the same size: only --hash tells them apart
    assert removed.by_you(library, {rel: ("", same_size)}) == {rel}
    assert removed.by_you(library, {rel: ("", same_size)}, check_hash=True) == set()
    found = removed.listing(library)
    removed.empty(library, found["digest"])  # its removal kept, as of now
    assert not (library / "_Removed").exists()
    assert removed.by_you(library, {rel: ("", {**copy, "copied_at": db.now()})}) == {rel}  # removed after its record
    assert removed.by_you(library, {rel: ("", copy)}) == set()  # recorded anew since (later), and lost
    manifest.add_copy(library, copy)
    assert removed.by_you(library, {"ebooks/nonesuch.epub": ("", copy)}) == set()
    shop = {"path": "storybundle/B/T/x.epub", "item": "7", "file": 0, "size": 3, "downloaded_at": db.now()}
    assert removed.by_you(library, {shop["path"]: ("storybundle", shop)}) == set()  # no answer that it was moved


def test_a_move_takes_the_name_itself_and_looks_last_where_no_hard_link_can_be(library, monkeypatch):
    """A hard link of the name itself (follow_symlinks off, where the system takes it), never what a link leads to; and
    on a disk that keeps no hard links, the look for a file in the way comes just before the rename: one there refuses
    it, nothing written over."""
    src, dest = library / "ebooks" / "Hedge Lore.epub", library / "_Removed" / "ebooks" / "Hedge Lore.epub"
    write(src, b"the copy")
    dest.parent.mkdir(parents=True)
    if os.name != "nt":
        linked, real = [], os.link
        monkeypatch.setattr(os, "link", lambda a, b, **k: linked.append(k) or real(a, b, **k))
        monkeypatch.setattr(os, "supports_follow_symlinks", os.supports_follow_symlinks | {os.link})
        removed._new_name(src, dest)
        assert dest.read_bytes() == b"the copy" and not src.exists() and linked == [{"follow_symlinks": False}]
        os.rename(dest, src)
    write(dest, b"removed before")
    with pytest.raises(FileExistsError):
        removed._rename_after_a_look(src, dest)
    assert dest.read_bytes() == b"removed before" and src.read_bytes() == b"the copy"


def test_the_digest_is_each_file_s_place_and_size_in_any_order():
    assert removed._digest([("b", 1), ("a", 2)]) == removed._digest([("a", 2), ("b", 1)])
    assert removed._digest([("a", 2), ("b", 1)]) not in (removed._digest([("a", 2), ("c", 1)]), removed._digest([("a", 2), ("b", 2)]))
    assert len(removed._digest([])) == 64
