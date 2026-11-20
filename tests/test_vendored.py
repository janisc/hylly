"""The reader's engine, foliate-js (src/hylly/foliate/), is kept as it came from the commit its README names, but for the
lines the README tells of: each file's SHA-256 is held here, beside the address the page asks for the files under.
Browsers keep these files for a year by that address (serve.py), so a file changed in passing, by a formatter or a
well-meant patch, would reach some readers and not others, and the licence notes would no longer be true. Whether they
are still that commit's files is a look at foliate-js itself, which needs the network and is done by hand before a
release; this test keeps them as they are between such looks."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from hylly import serve

FOLDER = Path(serve.__file__).with_name("foliate")

# The address the page asks for the files under (FOLIATE in catalog_page.html: the first letters of the README's commit,
# one more for each change made here), and each file's SHA-256 with its line endings as LF, so a checkout with CRLF counts
# the same. One table, renewed together and only for a change that is meant: the README tells of the change and of the
# address, which the page takes too, and the failing test prints the lines for the files as they are then.
ADDRESS = "78914aef44"
FINGERPRINTS = {
    "LICENSE": "b621ffc351fe263bc9b09dd0bc1c9332fda9c462a3904c1e4f0b4d3236b9c28c",
    "epub.js": "dfe347495039e9b5991478986ec6b7713cf9efbcd8f475a77ef7b967c5176298",
    "epubcfi.js": "e1a688ad5c84bf31f33cef467f92c7a6d060a0f17ec37c1d3c048e9c7b1cf645",
    "fb2.js": "09a7890bf191f1b6c6dd435fae2868b86c75e73e00731b0051c02f2a998a180a",
    "fixed-layout.js": "b38b8f5530529304dc8999a8cf48f2d5cd83da8061dd7c6d6f6d15b45100b040",
    "mobi.js": "4003c128410e6c5b106314b7b80555baf87d797721367654f03e13d02022bb9c",
    "overlayer.js": "d3d3396e3083277ccbd4f17c407621159b22a407107e43ce75ca3014578094fb",
    "paginator.js": "d76be63b9eccf5b4859a7a8105f253f43277fe02c0804ca45561ce2b5800147e",
    "progress.js": "544f5e90babc0cb31c5caf971a5f06dd6249ec06600cb62f672fa7179811fde1",
    "search.js": "31e846bdc095a645de0e94f7baa2c9e136561a363a2f774d4032e72cfa1fb0ef",
    "text-walker.js": "4ad740ab6142b5be428700ef4d270253cef6649cf6e047ae7d2698a7aec65e92",
    "vendor/LICENSE-fflate.txt": "0a1df3a083d0c010560aa342e87959c8c1070e6fd54545741f083f22d0c8b551",
    "vendor/LICENSE-zip.js.txt": "1b7ebc8d7889ed25491484ab2b102370742ca6c0b26650a0c62cc2269b579b84",
    "vendor/fflate.js": "e63bcc21f83a43e5f71d5cbefd351ff4d4dbd57bef8b29ad65c8bd65752f6e6b",
    "vendor/zip.js": "2a0d43157e4a447c6e43351225758795b75a33e8dfec10c6767248df2182949a",
    "view.js": "e0093af921feb03bb58fb51aeedc321fa108a4158d215f6913ecf7925415c945",
}


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def vendored(folder: Path = FOLDER) -> dict[str, Path]:
    """Every file of the folder but its README, which is Hylly's own, and what a computer leaves beside them (.DS_Store,
    __pycache__)."""
    return {p.relative_to(folder).as_posix(): p for p in sorted(folder.rglob("*"))
            if p.is_file() and p.name != "README.md" and not p.name.startswith(".") and "__pycache__" not in p.parts}


def differences(folder: Path = FOLDER) -> str:
    """What isn't as the table has it, and what to do, with the table's lines for the files as they are: "" if all is."""
    now = {name: fingerprint(path) for name, path in vendored(folder).items()}
    changed = sorted(name for name, digest in now.items() if FINGERPRINTS.get(name) != digest)
    gone = sorted(set(FINGERPRINTS) - set(now))
    if not changed and not gone:
        return ""
    lines = "\n".join(f'    "{name}": "{digest}",' for name, digest in now.items())
    return (f"foliate/ isn't what the address /foliate/{ADDRESS} stands for, whose files browsers keep for a year: "
            + "; ".join(part for part in (changed and f"changed or new: {', '.join(changed)}",
                                          gone and f"gone: {', '.join(gone)}") if part)
            + ". Put them back as they came. Or, for a change that is meant: say it in foliate/README.md, take one more of "
              "the commit's letters into the address there and in FOLIATE (catalog_page.html), and renew ADDRESS and "
              f"FINGERPRINTS in {Path(__file__).name} together, with these lines:\n{lines}")


def test_the_table_is_for_the_address_the_page_asks_for_and_the_readme_names():
    page = (FOLDER.parent / "catalog_page.html").read_text(encoding="utf-8")
    readme = (FOLDER / "README.md").read_text(encoding="utf-8")
    assert re.search(r'const FOLIATE = "/foliate/([0-9a-f]+)"', page)[1] == ADDRESS
    assert re.search(r"\b([0-9a-f]{40})\b", readme)[1].startswith(ADDRESS) and f"`{ADDRESS}`" in readme


def test_the_vendored_files_are_the_ones_the_address_stands_for():
    assert not (found := differences()), found


def test_a_file_changed_in_passing_is_found_and_line_endings_alone_are_no_change(tmp_path):
    """A made-up copy of the folder: its files as they are pass, with CRLF too; a line added to one, a file gone and one
    new are each found, and the message gives the lines to renew the table with."""
    copy = tmp_path / "foliate"
    for name, path in vendored().items():
        (copy / name).parent.mkdir(parents=True, exist_ok=True)
        (copy / name).write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    (copy / "README.md").write_text("Ours, and not in the table.\n", encoding="utf-8")
    (copy / ".DS_Store").write_bytes(b"\0")
    assert differences(copy) == ""

    with (copy / "view.js").open("ab") as fh:
        fh.write(b"// formatted\r\n")
    (copy / "vendor" / "zip.js").unlink()
    (copy / "tts.js").write_text("export {}\n", encoding="utf-8")
    found = differences(copy)
    assert "changed or new: tts.js, view.js; gone: vendor/zip.js." in found
    assert f'"view.js": "{fingerprint(copy / "view.js")}",' in found and '"vendor/zip.js"' not in found


def test_what_hylly_uses_of_foliate_is_written_down_and_each_name_is_there_on_both_sides():
    """The page leans on parts of foliate that are no promised interface (the renderer's sideProp, the book's
    transformTarget and its data event, the loader zip.js reads), and a name renamed in a newer commit fails quietly.
    The README lists them for whoever updates the files, and each name in the list is in foliate's files and in the
    page: one gone from foliate is a part to look at again, one gone from the page a line the list no longer needs."""
    readme = (FOLDER / "README.md").read_text(encoding="utf-8")
    page = (FOLDER.parent / "catalog_page.html").read_text(encoding="utf-8")
    assert "`FOLIATE` in `catalog_page.html`, changes with the\ncommit" in readme
    uses = readme[readme.index("## What Hylly uses"):]
    names = {n for n in re.findall(r"`([^`]+)`", uses) if re.fullmatch(r"#?[A-Za-z_][\w-]*", n)}
    assert {"page", "pages", "atStart", "atEnd", "sideProp", "bookDir", "next", "prev", "goTo", "transformTarget",
            "data", "load", "unload", "getSize", "rawFilename", "uncompressedSize"} <= names  # the review's, at least
    foliate = "\n".join(p.read_text(encoding="utf-8") for name, p in vendored().items() if name.endswith(".js"))
    # the page's own, at its top level (FOLIATE_FILES): in the page only
    own = {n for n in names if re.search(rf"^(?:async )?(?:function|const|let|class) {re.escape(n)}\b", page, re.M)}

    def named(n: str, text: str) -> bool:
        return bool(re.search(rf"(?<![\w#-]){re.escape(n)}(?![\w-])", text))
    assert [n for n in sorted(names - own) if not named(n, foliate)] == []
    assert [n for n in sorted(names) if not named(n, page)] == []
