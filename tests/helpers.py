"""What the tests share, besides conftest's fixtures: made-up books and comics, Humble's made-up orders, the made-up
library (build_library) and the catalog's data.js read back, the library server's helpers, databases as earlier versions
left them, a made-up clock, the made-up shops (a file server, itch.io, DriveThruRPG, StoryBundle, each shaped like the
real one when it was looked at), the library page's code run under node, and this project's own files. Test files
import from here, never from one another: what one file's tests need of another's lives here or in conftest.py, so that
a change to it is a change to everyone's, made in one place."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import io
import json
import os
import random
import re
import secrets
import shutil
import socket
import sqlite3
import struct as binary  # as struct, the name of a Humble download, below
import subprocess
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from PIL import Image

from hylly import bell, cli, db, itchio, itchio_survey, lookups, paths, people, plan, serve, snapshot, storage
from hylly import download as dl
from hylly.download import Manifest
from hylly.humble import HumbleClient as _REAL_HUMBLE
from hylly.inventory import MIB


# ---- books and pictures, made up -------------------------------------------------------------------------------------

def png(color=(200, 30, 30), size=(60, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


OPF = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
  <dc:title>The Long Dust</dc:title>
  <dc:creator opf:role="aut">Marlow Ellery</dc:creator>
  <dc:creator opf:role="ill">Some Illustrator</dc:creator>
  <dc:description>&lt;p&gt;A post-apocalyptic &lt;b&gt;novella&lt;/b&gt;.&lt;/p&gt;</dc:description>
  <dc:subject>FICTION / Science Fiction / General</dc:subject>
  <dc:publisher>Lantern Press</dc:publisher>
  <dc:date>2014-03-04</dc:date>
  <dc:identifier opf:scheme="ISBN">978-0-00-000000-2</dc:identifier>
  <dc:language>en</dc:language>
  <meta name="cover" content="cov"/>
  <meta name="calibre:series" content="Dust and Ember"/>
  <meta name="calibre:series_index" content="1.0"/>
 </metadata>
 <manifest><item id="cov" href="images/cover.png" media-type="image/png"/></manifest>
</package>"""


def make_epub(path, opf=OPF, cover=True):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        if cover:
            z.writestr("OEBPS/images/cover.png", png())


def write_book(path) -> None:
    """An EPUB with something to read: a chapter of a few pages."""
    text = "".join(f"<p>Paragraph {n}: the lantern road ran on past the harbour lights, and on.</p>" for n in range(120))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("content.opf", '<package xmlns="http://www.idpf.org/2007/opf" version="2.0"><metadata '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>The Novel</dc:title></metadata><manifest>'
                   '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c1"/>'
                   '</spine></package>')
        z.writestr("c1.xhtml", f'<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>One</h1>{text}</body></html>')


def mobi(title="The Silent Orbit", author="Ann Writer", cover=b"COVERIMG", exth=None, claimed=None, text=b"") -> bytes:
    """exth: its EXTH records as (kind, value), or (kind, value, the size it says); claimed: how many it says there are;
    text: a record between the first and the cover."""
    exth_records = exth if exth is not None else \
        [(100, author.encode()), (503, title.encode()), (103, b"Mara, a pilot."), (201, binary.pack(">I", 0))]
    body = b"".join(binary.pack(">II", r[0], r[2] if len(r) > 2 else len(r[1]) + 8) + r[1] for r in exth_records)
    said = len(exth_records) if claimed is None else claimed
    exth_block = b"EXTH" + binary.pack(">II", 12 + len(body), said) + body
    header_len = 232
    rec0 = bytearray(16 + header_len)
    rec0[16:20] = b"MOBI"
    binary.pack_into(">I", rec0, 20, header_len)
    binary.pack_into(">I", rec0, 28, 65001)
    binary.pack_into(">I", rec0, 108, 2 if text else 1)  # first image record
    binary.pack_into(">I", rec0, 128, 0x40)  # EXTH present
    name = b"Short name"
    binary.pack_into(">II", rec0, 84, len(rec0) + len(exth_block), len(name))
    rec0 = bytes(rec0) + exth_block + name
    records = [rec0, text, cover] if text else [rec0, cover]
    start = 78 + 8 * len(records)
    palm = bytearray(78)
    binary.pack_into(">H", palm, 76, len(records))
    offsets, pos = b"", start
    for r in records:
        offsets += binary.pack(">II", pos, 0)
        pos += len(r)
    return bytes(palm) + offsets + b"".join(records)


def real_mobi(encryption=0) -> bytes:
    """The made-up MOBI above, saying what kind of file it is and how its text is encrypted, as a real one does."""
    data = bytearray(mobi())
    data[60:68] = b"BOOKMOBI"
    binary.pack_into(">H", data, binary.unpack(">I", data[78:82])[0] + 12, encryption)
    return bytes(data)


A_SHOP_S_LOCK = "urn:example:a-shop-s-lock"


ENCRYPTED = ('<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
             '<enc:EncryptedData><enc:EncryptionMethod Algorithm="%s"/><enc:CipherData><enc:CipherReference URI="%s"/>'
             "</enc:CipherData></enc:EncryptedData></encryption>")


def locked_epub(path, listing=ENCRYPTED % (A_SHOP_S_LOCK, "OEBPS/ch1.html"), title="The Long Dust"):
    make_epub(path, OPF.replace("The Long Dust", title))
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("META-INF/encryption.xml", listing)
    return path


def damage_a_mobi(root):
    (root / "collections" / "Poetry" / "Herman Melville - Bartleby the Scrivener.mobi").write_bytes(b"x")


COLOURS = {"page1.jpg": (200, 0, 0), "page2.jpg": (0, 200, 0), "page10.png": (0, 0, 200)}


def picture(colour, size=(1800, 2700), fmt="JPEG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, fmt)
    return out.getvalue()


def make_comic(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ComicInfo.xml", "<ComicInfo/>")
        z.writestr("__MACOSX/._page1.jpg", b"not a picture")
        z.writestr(".hidden.jpg", picture((9, 9, 9)))
        for name, colour in reversed(list(COLOURS.items())):  # stored out of order on purpose
            z.writestr(name, picture(colour, fmt="PNG" if name.endswith(".png") else "JPEG"))
        z.writestr("small.jpg", picture((255, 255, 0), size=(400, 600)))


PAINTED = ((1, 0, 0), (0, 1, 0), (0, 0, 1))  # a painted PDF's pages: red, green, blue


def make_pdf(path, pages=3, title="", password=None, painted=False):
    """A PDF of so many blank pages of 300 by 400, its details giving a title if asked for (and a made-up author, keywords
    and date beside it). painted: three pages of 600 by 900 instead, red, green and blue, each filled with its colour, for
    the tests that look at what was drawn. password: locked with it. Returns the path."""
    import pymupdf

    doc = pymupdf.open()
    if painted:
        for colour in PAINTED:
            page = doc.new_page(width=600, height=900)
            page.draw_rect(page.rect, color=colour, fill=colour)
    else:
        for _ in range(pages):
            doc.new_page(width=300, height=400)
    if title:
        doc.set_metadata({"title": title, "author": "Pat Sample", "keywords": "dice, maps", "creationDate": "D:20190102"})
    path.parent.mkdir(parents=True, exist_ok=True)
    if password:
        doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password)
    else:
        doc.save(path)
    doc.close()
    return path


# ---- Humble's orders, made up ----------------------------------------------------------------------------------------

ICON = "https://hb.imgix.net/abc.png?auto=compress&w=100&s=ffff"


def struct(label, filename, size_mb, md5=None):
    return {
        "name": label,
        "url": {"web": f"https://dl.humble.com/{filename}?gamekey=SECRET&ttl=1700000000&t=sig", "mirror": f"https://dl.humble.com/mirror/{filename}?gamekey=SECRET&ttl=1&t=sig"},
        "file_size": int(size_mb * MIB),
        "md5": md5 or f"md5-{filename}",
    }


def sub(machine, title, publisher, files, platform="ebook"):
    return {
        "machine_name": machine,
        "human_name": title,
        "payee": {"human_name": publisher},
        "icon": ICON,
        "downloads": [{"platform": platform, "download_struct": [struct(*f) for f in files]}],
    }


def order(key, created, bundle, subs, tpks=()):
    return {
        "gamekey": key,
        "created": created,
        "product": {"human_name": bundle, "machine_name": key + "_bundle", "category": "bundle"},
        "subproducts": subs,
        "tpkd_dict": {"all_tpks": list(tpks)},
    }


ORDERS = {
    "K1": order("K1", "2019-03-01T10:00:00", "Humble Book Bundle: Fantasy by Lantern Press", [
        sub("novel", "The Novel", "Lantern Press", [("EPUB", "novel.epub", 2), ("MOBI", "novel.mobi", 4), ("PDF", "novel.pdf", 6)]),
        sub("mobionly", "Old Mobi Book", "Lantern Press", [("MOBI", "old.mobi", 1)]),
        sub("pdfnovel", "PDF Novel", "Lantern Press", [("PDF", "pdfnovel.pdf", 3)]),
        sub("artbook", "The Art of Things", "Lantern Press", [("EPUB", "art.epub", 80)]),
        sub("audiobook", "The Novel (Audio)", "Lantern Press", [("MP3", "novel_mp3.zip", 300)], platform="audio"),
        sub("dupfile_a", "Shared Story", "Lantern Press", [("EPUB", "shared.epub", 1, "same-md5")]),
        sub("samefile_other", "Different Story", "Lantern Press", [("EPUB", "different.epub", 1, "md5-x")]),
    ]),
    "K2": order("K2", "2020-05-01T10:00:00", "Humble Comics Bundle: Lantern Wolf", [
        sub("lwolf1", "Lantern Wolf Vol. 1", "Gloamlight Comics", [("CBZ", "lw1.cbz", 200), ("PDF", "lw1.pdf", 205), ("EPUB", "lw1.epub", 150)]),
        sub("lwolf2", "Lantern Wolf Vol. 2", "Gloamlight Comics", [("CBZ", "lw2.cbz", 100), ("PDF", "lw2.pdf", 300)]),
        sub("lwnovel", "Lantern Wolf: The Novel", "Gloamlight Comics", [("EPUB", "lwn.epub", 1), ("MOBI", "lwn.mobi", 2)]),
    ]),
    "K3": order("K3", "2021-01-01T10:00:00", "Humble Manga Bundle: Hanabi Press", [
        sub("shrinefox1", "Shrine Fox: Stray Spirit 1", "Hanabi Press", [("PDF", "shrinefox_strayspirit_vol1.pdf", 309)]),
    ]),
    "K4": order("K4", "2022-01-01T10:00:00", "Humble RPG Bundle: Iron Stride", [
        sub("fieldrules", "Iron Stride Field Rules", "Forgeworks Games", [("PDF", "fr.pdf", 50), ("PDF (HQ)", "fr_hq.pdf", 120)]),
        sub("clanledger", "Clan Ledger", "Duskwater Games", [("EPUB", "cl.epub", 19), ("PDF", "cl.pdf", 15)]),
        sub("isnovel", "Thunder at Kestrel Gap", "Forgeworks Games", [("EPUB", "tkg.epub", 2), ("MOBI", "tkg.mobi", 3)]),
    ]),
    "K5": order("K5", "2023-01-01T10:00:00", "Humble Tech Book Bundle: Python by Brightwire", [
        {**sub("pss", "Python in Small Steps", "Brightwire Press", [("EPUB", "pss.epub", 10), ("MOBI", "pss.mobi", 15), ("PDF", "pss.pdf", 20)]),
         "downloads": [
             {"platform": "ebook", "download_struct": [struct("EPUB", "pss.epub", 10), struct("MOBI", "pss.mobi", 15), struct("PDF", "pss.pdf", 20), struct("Supplement", "pss_code.zip", 5)]},
         ]},
        sub("dupfile_b", "Shared Story", "Lantern Press", [("EPUB", "shared2.epub", 1, "same-md5")]),
        sub("samefile_other2", "Another Story", "Lantern Press", [("EPUB", "another.epub", 1, "md5-x")]),
    ]),
    "K6": order("K6", "2024-01-01T10:00:00", "Humble Book Bundle: Fantasy Again", [
        sub("novel", "The Novel", "Lantern Press", [("EPUB", "novel.epub", 2), ("MOBI", "novel.mobi", 4)]),
        sub("con", "Con", "Lantern Press", [("EPUB", "con.epub", 1)]),
    ], tpks=[{"human_name": "Some Game", "key_type": "steam", "redeemed_key_val": "AAAAA-BBBBB-CCCCC"}]),
    "K7": order("K7", "2025-01-01T10:00:00", "Humble Bundle: Some Game", [
        sub("somegame", "Some Game", "Dev", [("Download", "game.zip", 900)], platform="windows"),
    ]),
    "K8": order("K8", "2025-06-01T10:00:00", "Humble Weekly Bundle: Adventures", [
        sub("manual", "The Manual", "Dev", [("PDF (Deutsch)", "manual_de.pdf", 4), ("PDF (English)", "manual_en.pdf", 3)]),
        sub("pixelpost", "Two Issues of Pixel Post", "Pixel Post", [("Issue 36", "36.pdf", 27), ("Issue 48", "48.pdf", 37)]),
        sub("puzzles", "Puzzle Book", "Riddleworks", [("EPUB", "pz.epub", 10), ("PDF", "pz.pdf", 37), ("Maps (PDF)", "pz_maps.pdf", 23), ("Video", "pz_video.zip", 900)]),
        {"human_name": "PDF Quality", "machine_name": "pdfquality_warning_comics", "downloads": [], "library_family_name": "hidden"},
    ]),
}



class HumbleSite:
    """Humble's site as its library page reads it (humble.py's docstring), made from orders in the shape the
    order API gave them (order, sub, struct): whether you're signed in, the library page and a title's page with the
    scripts they list, the scripts naming the two actions, the list (filterUserLibrary) a page at a time, each
    purchase's page with its items' files, and an item's links (getSignedUrlsForEntitlements), each its listed link with
    t signed afresh. No date, publisher or cover, as the site gives none. A purchase's id is its order key, an item's
    entitlement id the key and its machine name (key~machine name), and a download's key the machine name and its platform. answer: the
    file host's (dl.humble.com), or None; answered: the purchases whose pages answer (None: all), the others 404."""

    LIST_ID, LINK_ID = "60" + "a" * 40, "70" + "b" * 40
    RSC = "a1b2c"  # Next.js's check value for a page asked for as the router asks: without it, a 307 to the same page

    def __init__(self, orders: dict, answer=None, signed_in: bool = True, answered: set | None = None):
        self.orders, self.answer, self.signed_in, self.answered = orders, answer, signed_in, answered
        self.asked: list[tuple[str, str]] = []  # (method, path), and an action by its name
        self.links = 0  # sets of links made

    def items(self) -> list[dict]:
        out = []
        for key, o in sorted(self.orders.items(), key=lambda kv: (kv[1] or {}).get("created") or "", reverse=True):
            for s in (o or {}).get("subproducts") or []:
                if s.get("library_family_name") == "hidden":
                    continue
                has = any(d.get("download_struct") for d in s.get("downloads") or [])
                ent = f"{key}~{s['machine_name']}"
                out.append({"id": ent, "title": s.get("human_name"), "subTitle": (o.get("product") or {}).get("human_name"),
                            "itemType": "content-entitlement", "type": "download_drm_free" if has else "steam",
                            "userId": "$undefined", "contentId": s["machine_name"], "productEntitlementId": key,
                            "collection": "content-entitlements", "deliveryMethods": ["download" if has else "steam"],
                            "entitlements": {"keys": [], "downloads": [{"id": ent, "title": s.get("human_name"),
                                                                        "downloads": "$undefined", "signedUrls": "$undefined"}]},
                            "preOrder": "$undefined", "releaseDate": "$undefined", "choiceDate": "$undefined"})
        return out

    @staticmethod
    def rsc(*values, first: str = "") -> bytes:
        """Rows as Next.js sends them: a script listed, a text row with a line end in it, and the values."""
        text = 'not\na row'
        lines = [first or '0:{"b":"build","q":""}', '2:"/_next/static/chunks/0common.js"', f"3:T{len(text.encode()):x},{text}"]
        lines += [f"{n + 4:x}:{json.dumps(v)}" for n, v in enumerate(values)]
        return ("\n".join(lines) + "\n").encode()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, action = request.url.path, request.headers.get("next-action", "")
        self.asked.append((request.method, path))
        if request.url.host != "www.humblebundle.com":
            return self.answer(request) if self.answer else httpx.Response(404)
        if path == "/api/auth/session-status":
            return httpx.Response(200, json={"status": "authenticated" if self.signed_in else "anonymous"})
        if path.startswith("/_next/static/chunks/"):
            named = {"/_next/static/chunks/list.js": (self.LIST_ID, "filterUserLibrary"),
                     "/_next/static/chunks/link.js": (self.LINK_ID, "getSignedUrlsForEntitlements")}.get(path)
            js = f'let a=(0,x.createServerReference)("{named[0]}",x.callServer,void 0,x.findSourceMapURL,"{named[1]}");' if named else "let b=1;"
            return httpx.Response(200, text=js, headers={"content-type": "application/javascript"})
        rows = {"content-type": "text/x-component"}
        if request.method == "GET" and request.headers.get("rsc") and request.url.params.get("_rsc", None) != self.RSC:
            self.asked.append(("redirect", path))
            return httpx.Response(307, headers={"location": f"{path}?_rsc={self.RSC}", "content-type": "text/html"})
        if request.method == "POST" and action not in (self.LIST_ID, self.LINK_ID):
            return httpx.Response(404, text="Server action not found.")
        if request.method == "POST" and action == self.LIST_ID:
            self.asked.append(("action", "filterUserLibrary"))
            form = dict(re.findall(rb'name="_1_(\w+)"\r\n\r\n([^\r]*)\r\n', request.content))
            page, limit = int(form[b"page"]), int(form[b"limit"])
            every = self.items()
            pages = max(1, -(-len(every) // limit))
            answer = {"results": every[(page - 1) * limit:page * limit], "bundlesCount": 0, "success": True,
                      "pagination": {"page": page, "totalPages": pages, "totalDocs": len(every)}, "requestId": "1"}
            return httpx.Response(200, content=f'0:{{"a":"$@1","b":"build"}}\n1:{json.dumps(answer)}\n'.encode(), headers=rows)
        if request.method == "POST":  # the link's
            self.asked.append(("action", "getSignedUrlsForEntitlements"))
            (ent,) = json.loads(request.content)[0]
            self.asked.append(("link", ent))
            key, _, machine = ent.partition("~")
            s = next(s for s in self.orders[key]["subproducts"] if s.get("machine_name") == machine)
            self.links += 1
            links = {f"{machine}_{d.get('platform')}": {
                         x["url"]["web"].split("?")[0].rsplit("/", 1)[1]: x["url"]["web"].split("?")[0] + f"?t=fresh{self.links}"
                         for x in d.get("download_struct") or [] if (x.get("url") or {}).get("web")}
                     for d in s.get("downloads") or []}
            return httpx.Response(200, headers=rows, content=(
                '0:{"a":"$@1"}\n1:' + json.dumps({"ok": True, "data": {"signedUrls": {machine: links},
                                                                         "expiresAt": "2099-01-01T00:00:00.000Z"}}) + "\n").encode())
        if path == "/home/library" or path.startswith("/home/library/content/"):  # a page, for the scripts it lists
            script = "list.js" if path == "/home/library" else "link.js"
            return httpx.Response(200, headers=rows, content=self.rsc({"x": f"/_next/static/chunks/{script}"}))
        key = path.removeprefix("/home/library/")
        if key not in self.orders or (self.answered is not None and key not in self.answered):
            return httpx.Response(404, text="<html>not found</html>", headers={"content-type": "text/html"})
        o = self.orders[key]
        ents = [{"id": f"{key}~{s['machine_name']}", "title": s.get("human_name"), "type": "download_drm_free",
                 "redemptionData": {}, "signedUrls": "$undefined",
                 "downloads": [{"id": f"d{n}", "key": f"{s['machine_name']}_{d.get('platform')}", "platform": d.get("platform"),
                                "downloadStruct": [{"id": "x", "name": x.get("name"), "md5": x.get("md5"), "sha1": "$undefined",
                                                    "fileSize": x.get("file_size"), "size": None,
                                                    "url": {"web": x["url"]["web"].split("?")[0].rsplit("/", 1)[1]}}
                                                   for x in d.get("download_struct") or [] if (x.get("url") or {}).get("web")]}
                               for n, d in enumerate(s.get("downloads") or [])]}
                for s in o.get("subproducts") or [] if s.get("library_family_name") != "hidden"]
        item = {"id": key, "title": (o.get("product") or {}).get("human_name"), "itemType": "content-entitlement",
                "type": "bundle", "entitlements": {"keys": [], "downloads": "$5", "redemptionGroups": []}}
        return httpx.Response(200, headers=rows, content=self.rsc(
            {"x": "/_next/static/chunks/link.js"}, ents, ["$", "$L9", None, {"userLibraryItem": item, "productEntitlementId": key}]))

    def client(self, session, **options):
        """The Humble client, against this site: the real one, whatever a test has put in its place."""
        return _REAL_HUMBLE(session, transport=httpx.MockTransport(self.handler), **options)


def ascii_titles() -> list[str]:
    """Names in plain ASCII, made up and made at random, with every ASCII character among them: what a change to how
    names are compared must read just as before (test_plan.py's tests, and test_lookups.py's)."""
    names = ["The Moon Garden", "Lantern Wolf Vol. 1", "Lantern Wolf: The Novel", "Moonwake #3", "Ironclad Hearts #31",
             "Neon Rangers 088", "neon_rangers_088", "starlight-drifters-vol-4", "Glass Harbor Vol. 4: The Long Tide",
             "Rust Cowboy (Vol. 1) #1", "Shrine Fox: Stray Spirit 1", "Python in Small Steps", "C++ in Small Steps",
             "Moss & Stone", "Q&A for Moonwake", "&", "the", "The", "THE END", "the the", "Theory of Moss", "The & Wolf",
             "  The  Long   Dust  ", "Writer, Ann", "Ann Writer", "Moonwake Press", "Moonwake's Garden", "100% Lantern",
             "(ebook - psychology) Life After Moss", "A/B Notes", "Tab\tand\nnew line", "", " ", "...", "Vol. 2.5"]
    every = [chr(c) for c in range(128)]
    names += every + [f"Moon{c}Wolf" for c in every] + [f"the{c}moon" for c in every] + [f"{c}The Moon" for c in every]
    pool = every + list("the THE and & ") * 4 + list("Moonwake Lantern Wolf 0123456789") * 2
    rng = random.Random(1311)
    return names + ["".join(rng.choice(pool) for _ in range(rng.randint(0, 30))) for _ in range(2000)]


# ---- the made-up library and its catalog -----------------------------------------------------------------------------

# A folder of your own whose books are filed by genre (folders.py): what [folders.<name>] in the settings can say.
EBOOKS = ('[folders.ebooks]\nlabel = "My e-books"\nfirst = true\nnamed = true\ngenres = true\n'
          'kinds = { "Comics & Graphic Novels" = "comic", "Programming" = "tech" }\n')


def with_settings(text: str) -> None:
    """Adds to the settings, which are read afresh with the rules, as the next command would read them."""
    from hylly import rules, settings

    file = paths.settings_file()
    file.write_text((file.read_text(encoding="utf-8") if file.is_file() else "") + text, encoding="utf-8")
    settings.reload()
    rules.reset()


def build_library(root):
    state = root / "state"
    state.mkdir(parents=True)
    snap = state / "humble" / "2026-01-01_0000.json"  # each shop's snapshots in its own folder of the state folder
    snap.parent.mkdir()
    snap.write_text(json.dumps(snapshot.sanitize(ORDERS)), encoding="utf-8")
    titles = {t.product.machine_name: t for t in plan.build(ORDERS).titles}
    m = Manifest(state.parent, "humble")
    for mid in ("novel", "lwolf1", "lwolf2", "pss"):
        t = titles[mid]
        f = root / t.path
        f.parent.mkdir(parents=True, exist_ok=True)
        if f.suffix == ".epub":
            make_epub(f)
        else:
            with zipfile.ZipFile(f, "w") as z:
                z.writestr("p1.jpg", png())
        m.add({"path": t.path, "md5": "x", "size": f.stat().st_size})
    # StoryBundle
    (state / "storybundle").mkdir()
    (state / "storybundle" / "2026-01-01_0000.json").write_text(json.dumps([{"name": "The Lantern Video Game Bundle", "purchased": "2020-01-01",
        "bonus": False, "books": [{"id": "7", "title": "Arcade Worlds", "approx_size": 1, "cover": ""}]}]), encoding="utf-8")
    sb = root / "storybundle" / "The Lantern Video Game Bundle" / "Arcade Worlds" / "Arcade_Worlds_-_Ann_Writer.epub"
    sb.parent.mkdir(parents=True)
    make_epub(sb, cover=False)
    Manifest(state.parent, "storybundle").add({"path": sb.relative_to(root).as_posix(), "size": sb.stat().st_size,
                                                        "item": "7", "file": 0, "ext": "epub", "md5": "y"})
    # folders
    poetry = root / "collections" / "Poetry"
    poetry.mkdir(parents=True)
    make_epub(poetry / "Herman Melville - Bartleby the Scrivener.epub")
    (poetry / "Herman Melville - Bartleby the Scrivener.mobi").write_bytes(real_mobi())  # the same book, another format
    comics = root / "comics" / "Neon Comics"
    comics.mkdir(parents=True)
    for n in ("008", "002"):
        with zipfile.ZipFile(comics / f"Neon Rangers {n}.cbz", "w") as z:
            z.writestr("01.jpg", png())
    kane = root / "comics" / "Marshal Kane The Complete Trail Logs"  # "Marshal Kane - ..." names the series, not an author
    kane.mkdir(parents=True)
    with zipfile.ZipFile(kane / "Marshal Kane - The Complete Trail Logs 006 (Deluxe Colour Edition).cbz", "w") as z:
        z.writestr("01.jpg", png())
    manual = root / "manuals" / "PC" / "Wasteland Rover"
    manual.mkdir(parents=True)
    (manual / "Manual.pdf").write_bytes(b"%PDF-broken")
    # books bought elsewhere and filed by genre, named by their titles; their OPF has no subjects and no series
    with_settings(EBOOKS)
    other_opf = (OPF.replace("<dc:subject>FICTION / Science Fiction / General</dc:subject>", "")
                  .replace('<meta name="calibre:series" content="Dust and Ember"/>', ""))
    sf = root / "ebooks" / "Science Fiction"
    sf.mkdir(parents=True)
    make_epub(sf / "The Lantern Keeper (Harbor Lights Book 2).epub", other_opf.replace("The Long Dust", "The Lantern Keeper (Harbor Lights)")
              .replace("Marlow Ellery", "Brennan, Jo"))
    comics_folder = root / "ebooks" / "Comics & Graphic Novels"
    comics_folder.mkdir(parents=True)
    make_epub(comics_folder / "Velvet Jinx 1.epub", other_opf.replace("The Long Dust", "Velvet Jinx 1")
              .replace("Marlow Ellery", "Unknown"))


def page_data(root) -> dict:
    """What the library page is given: the catalog's data.js, read as the page reads it (its titles in items, the shops'
    and folders' names in sources)."""
    text = (root / "catalog" / "data.js").read_text(encoding="utf-8")
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


def data(root) -> list[dict]:
    """The catalog's titles, as the library page has them."""
    return page_data(root)["items"]


def items(root) -> dict[str, dict]:
    """The catalog's titles by their ids."""
    return {i["id"]: i for i in data(root)}


def rec(t, src, size, ext="epub", a=None, k="book", trust=True, **more):
    r = {"id": f"{src}:{t}:{size}", "t": t, "src": src, "k": k, "f": [[f"{src}/{t}.{ext}", ext, size]],
         "_file": (ext, size), "_trust": trust, **more}
    return {**r, "a": a} if a else r


def two_moonwakes(root) -> None:
    """One book, Moonwake Saga, in comics (a comic, as that folder's kind) and in a folder of books: kinds that don't join,
    so merge_copies keeps them apart, unsure."""
    for folder in ("comics/Sagas", "shelf"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    make_epub(root / "comics" / "Sagas" / "Moonwake Saga.epub", OPF.replace("The Long Dust", "Moonwake Saga"))
    make_epub(root / "shelf" / "Moonwake Saga.epub", OPF.replace("The Long Dust", "Moonwake Saga").replace("Dust and Ember", "Saga"))


def near_miss(root, title_id: str, score: float, source: str = "openlibrary", match: str = "Arcade Worlds II") -> None:
    """A lookup's answer for a title in its source's cache: under accept (0.8) a none with its near miss, as lookups.run
    keeps one; at or above it, found."""
    found = {"description": f"A made-up tale ({match}).", "subjects": [f"About {match}"], "score": score, "match": match,
             "url": f"https://openlibrary.org/works/OL{len(match)}W"}
    cache = root / "state" / "lookups" / f"lookup-{source}.jsonl"
    cache.parent.mkdir(parents=True, exist_ok=True)
    line = {"key": f"k:{title_id}", "ids": [title_id], "at": "2099-01-01T00:00:00",
            **({"status": "found", "found": found} if score >= 0.8 else {"status": "none", "found": {"near": found}})}
    with cache.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


# ---- the library's server --------------------------------------------------------------------------------------------

HOME = {"Host": "192.168.1.5:8770", "Origin": "http://192.168.1.5:8770"}  # a device at home, not this computer
EVERY_ROLE = {"bea": "borrower", "rory": "reader", "lucy": "librarian", "owen": "owner"}  # a user of each role, by name
TITLE = {"id": "h:novel", "t": "The Novel", "f": "humble/b/novel.epub"}


def start(root, **kwargs):
    """A server for the library at root on a port of its own, serving in a thread, and a client for it (not logged in):
    for a test that starts its own, and stops it itself (shutdown, then server_close)."""
    srv, _ = serve.make_server(root, "127.0.0.1", 0, **kwargs)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")


@contextlib.contextmanager
def logged_in(srv, name):
    """A browser at home logged in to the server as that user, by the login page, with the password every made-up user
    has (conftest.PASSWORD)."""
    with httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}", headers=HOME) as browser:
        assert browser.post("/login", data={"username": name, "password": "correct horse", "next": "/"}).status_code == 303
        yield browser


def change(client, ids, op="add", tag="Read"):
    key = client.get("/api/session").json()["key"] or "none"
    return client.post("/api/tags", json={"op": op, "tag": tag, "ids": ids}, headers={"X-Server-Key": key})


def fix(client, ids, field="year", value="1999"):
    key = client.get("/api/session").json()["key"] or "none"
    return client.post("/api/fix", json={"field": field, "value": value, "ids": ids}, headers={"X-Server-Key": key})


# ---- the database, and what earlier versions left --------------------------------------------------------------------

def rows(table, where="1"):
    """How many rows a table of the database has: what has been written."""
    with db.database().read() as c:
        return c.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0]


def changes(name="rory"):
    """How many changes of theirs the database has had."""
    with db.database().read() as c:
        found = c.execute("SELECT rev FROM person WHERE name = ? AND removed IS NULL", (name,)).fetchone()
    return found[0] if found else None


def again(told=None):
    """The same person as a new server start finds them."""
    return people.Person("rory", (lambda name, what: told.append((name, what))) if told is not None else None)


def newer_database():
    """The database as a newer version of Hylly leaves it, and this program starting afresh."""
    with db.database().write() as c:
        c.execute("PRAGMA user_version = 99")
    db.close_all()


def made_before_the_library_was_kept(night):
    """A nightly copy as it was made before the library's records were kept in the database: its database at the first
    of the test versions' steps, before the first release, with no library table, and a check.json that doesn't name a
    library."""
    check = json.loads((night / "check.json").read_text(encoding="utf-8"))
    check.pop("library", None)
    (night / "check.json").write_text(json.dumps(check), encoding="utf-8")
    (night / db.FILE).unlink()
    with contextlib.closing(sqlite3.connect(night / db.FILE)) as c:
        c.execute("CREATE TABLE person (id INTEGER PRIMARY KEY, name TEXT NOT NULL) STRICT")
        c.execute("PRAGMA user_version = 1")
        c.commit()
    return night


SB_OLD = {"path": "storybundle/The Moonwake Bundle/Second Tide/second-tide.epub", "size": 30, "md5": None, "md5_source": "transfer",
          "source": "storybundle", "storybundle_id": "78", "bonus": False}


DT_OLD = {"path": "drivethrurpg/Moonwake Press/Lantern Wolf Field Guide/field_guide.pdf", "size": 88, "md5": "d" * 32,
          "md5_source": "shop", "source": "drivethrurpg", "drivethrurpg_id": "1001", "drivethrurpg_index": 0,
          "drivethrurpg_md5": "d" * 32, "publisher": "Moonwake Press"}


FIRST_RELEASE_RECORDS = [  # (shop, a record as the first release's downloads wrote it, the same record as this version names it)
    ("humble", {"path": "humble/Moonwake Press Fantasy Bundle/The Novel/novel.epub", "shop_id": "novel", "md5": "a" * 32,
                "size": 3, "md5_source": "shop", "role": "book"},
     {"item": "novel", "file": "novel.epub"}),
    ("humble", {"path": "humble/Moonwake Press Fantasy Bundle/The Novel/novel (2).pdf", "shop_id": "novel", "md5": "b" * 32,
                "size": 4, "listed_md5": "c" * 32, "listed_size": 5, "instead_of": "humble/Moonwake Press Fantasy Bundle/The Novel/novel.epub"},
     {"item": "novel", "file": "novel (2).pdf"}),
    ("humble", {"path": "humble/Old Bundle/Ödön's Tale/tale.pdf", "size": 1}, {"file": "tale.pdf"}),  # one that named no title
    ("storybundle", SB_OLD, {"item": "78", "file": 0, "storybundle_id": ...}),
    ("drivethrurpg", DT_OLD, {"item": "1001", "file": 0, "listed_md5": "d" * 32, "drivethrurpg_id": ...,
                              "drivethrurpg_index": ..., "drivethrurpg_md5": ...}),
    ("drivethrurpg", {"path": "drivethrurpg/Duskwater Games/Clan Ledger/maps.zip", "size": 500, "md5": "f" * 32, "drivethrurpg_id": "1002",
                      "drivethrurpg_index": 2, "drivethrurpg_md5": None, "drivethrurpg_size": 500, "publisher": "Duskwater Games"},
     {"item": "1002", "file": 2, "listed_md5": None, "listed_size": 500, "drivethrurpg_id": ..., "drivethrurpg_index": ...,
      "drivethrurpg_md5": ..., "drivethrurpg_size": ...}),
    ("drivethrurpg", {"path": "drivethrurpg/Duskwater Games/Old Kit/kit.pdf", "size": 9, "drivethrurpg_id": "1003"},
     {"item": "1003", "drivethrurpg_id": ...}),  # one with only its product: no file field comes of none
    ("itchio", {"path": "itchio/Moonwake Press/Lantern Wolf/lantern-wolf.pdf", "size": 45, "md5": "e" * 32, "source": "itchio",
                "itchio_id": 21, "itchio_upload": 2100, "creator": "Ann Writer", "itchio_md5": "e" * 32, "itchio_size": 45,
                "gone": "2026-11-02T10:00:00", "tags": {"kept": [True, None]}},
     {"item": 21, "file": 2100, "listed_md5": "e" * 32, "listed_size": 45, "itchio_id": ..., "itchio_upload": ...,
      "itchio_md5": ..., "itchio_size": ...}),
    ("itchio", {"path": "itchio/Ann Writer/Quiet Engineers/quiet.epub", "size": 7, "itchio_id": 23, "itchio_upload": {"odd": True}},
     {"item": 23, "file": {"odd": True}, "itchio_id": ..., "itchio_upload": ...}),  # whatever a value was, it stays so
]


def named_now(old: dict, change: dict) -> dict:
    """A record of before as this version names it: change's fields put in, and those it gives ... taken out."""
    return {**{k: v for k, v in old.items() if change.get(k) is not ...}, **{k: v for k, v in change.items() if v is not ...}}


def at_the_first_step(where: Path, records=None, library: str | None = None) -> None:
    """A database as the first release left it: its first step's tables alone, its header, and the download records
    its shops wrote, each shop with fields of its own (FIRST_RELEASE_RECORDS). library: the id of the library it keeps."""
    import sqlite3

    from hylly import db

    where.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(where, isolation_level=None)) as old:
        old.execute("BEGIN")
        for statement in db.statements(db.migrations()[0][2]):
            old.execute(statement)
        for shop, body, _ in FIRST_RELEASE_RECORDS if records is None else records:
            old.execute("INSERT INTO download (shop, body) VALUES (?, ?)", (shop, db.dump(body)))
        if library:
            old.execute("INSERT INTO library (id, root, since) VALUES (?, '/elsewhere', '2026-11-12')", (library,))
        old.execute(f"PRAGMA application_id = {db.APPLICATION_ID}")
        old.execute("PRAGMA user_version = 1")
        old.execute("COMMIT")


def as_hylly_left_it_before(root) -> dict:
    """A library taken on by Hylly before markers named an install, as the live ones are when this version comes: the
    database's library row and the library's marker exactly as db.own wrote them then (an id, the computer's name and
    since when, by atomic_write, indented by one), and nothing beside the database. What the marker says."""
    library_id, now = secrets.token_hex(16), datetime.now().astimezone().isoformat(timespec="seconds")
    with db.database().write() as c:
        c.execute("INSERT INTO library (id, root, since) VALUES (?, ?, ?)", (library_id, os.path.abspath(root), now))
    db.owner_file(root).parent.mkdir(parents=True, exist_ok=True)
    storage.atomic_write(db.owner_file(root), json.dumps({"id": library_id, "computer": socket.gethostname(), "since": now}, indent=1))
    return json.loads(db.owner_file(root).read_text(encoding="utf-8"))


# ---- the command line and the doctor ---------------------------------------------------------------------------------

def exit_of(capsys, *argv) -> tuple[int, str]:
    """A command run as hylly would run it, which ends by SystemExit: its exit code and what it wrote to standard error."""
    with pytest.raises(SystemExit) as e:
        cli.main(list(argv))
    return e.value.code, capsys.readouterr().err


def states(result) -> dict:
    """The doctor's checks (doctor.report) by what each looks at: its state."""
    return {c["what"]: c["state"] for c in result["checks"]}


# ---- time, made up ---------------------------------------------------------------------------------------------------

class Clock:
    """Fake time: sleeping only moves the clock, so a run that waits hours takes no time."""

    def __init__(self):
        self.t = 1_800_000_000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


# A record's times to test by, whatever the day the tests run: local times alone, as records had them before they had
# their offsets. In summer, in winter, and in the hour the clocks go back in Finland (03:00 to 04:00 comes twice that
# Sunday), which such a time is read as the first of (fold 0, as datetime reads it).
CLOCK_TIMES = {"summer": datetime(2026, 7, 15, 12, 0), "winter": datetime(2026, 1, 15, 12, 0),
               "the hour the clocks go back": datetime(2026, 10, 25, 3, 30)}


@contextlib.contextmanager
def in_helsinki(monkeypatch):
    """Finland's time zone (EET, EEST) for this program while the block runs, whatever the zone the tests run in: the
    clocks' change at a known hour. Only where a program can set its zone (time.tzset), so not on Windows."""
    if not hasattr(time, "tzset"):
        pytest.skip("a program can't set its own time zone on Windows")
    try:
        with monkeypatch.context() as m:
            m.setenv("TZ", "Europe/Helsinki")
            time.tzset()
            if time.tzname != ("EET", "EEST"):
                pytest.skip("this computer has no Europe/Helsinki zone")
            yield
    finally:
        time.tzset()  # the zone the tests run in, again


def session_for(source, fake_handler, clock, tmp_path, per_hour=0, per_day=0):
    budget = lookups.Budget(tmp_path / f"budget-{source.id}.json", per_hour or source.per_hour, per_day or source.per_day, clock.now)
    return lookups.Session(source, budget, transport=httpx.MockTransport(fake_handler), sleep=clock.sleep, now=clock.now)


# ---- a file server, made up ------------------------------------------------------------------------------------------

LAST_MODIFIED = "Thu, 01 Jan 2026 00:00:00 GMT"


class FakeCDN:
    """Serves files by URL path. `fail[path]` is a list of one-shot behaviours."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.fail: dict[str, list] = {}
        self.etags: dict[str, str] = {}
        self.requests: list[tuple[str, str | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        rng = request.headers.get("range")
        self.requests.append((path, rng))
        behaviours = self.fail.get(path) or []
        b = behaviours.pop(0) if behaviours else None
        data = self.files[path]
        if callable(b):
            return b(request, data)
        if b in (403, 404, 410, 429, 500):
            return httpx.Response(b)
        headers = {"last-modified": LAST_MODIFIED}
        if path in self.etags:
            headers["etag"] = f'"{self.etags[path]}"'
        if b == "corrupt":  # damaged on the way, past its first bytes
            data = data[:8] + b"x" * (len(data) - 8)
        if b == "bare-416":  # refuses the range without saying how long the file is
            return httpx.Response(416)
        if rng and b != "ignore-range":
            start = int(rng.split("=")[1].rstrip("-"))
            if start >= len(data):
                return httpx.Response(416, headers={"content-range": f"bytes */{len(data)}"})
            return httpx.Response(206, content=data[start:], headers={
                **headers, "content-range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
        if b == "short":  # the connection ends early, but the server announced the full length
            return httpx.Response(200, content=data[: len(data) // 2], headers={**headers, "content-length": str(len(data))})
        return httpx.Response(200, content=data, headers=headers)


def make_job(name: str, data: bytes, kind: str = "book", folder: str | None = None, with_md5: bool = True) -> dl.Job:
    folder = f"humble/Bundle/{folder or name}"
    return dl.Job(
        path=f"{folder}/{name}", folder=folder, url=f"https://cdn.test/{name}?gamekey=K&t=sig", filename=name,
        size=len(data), md5=hashlib.md5(data).hexdigest() if with_md5 else None, role="book", label="EPUB",
        ext=name.rsplit(".", 1)[-1], title=name, bundle="Bundle", shop_id=name, kind=kind,
        device="ereader", purchased="2020-01-01",
    )


STARTS = {"epub": b"PK\x03\x04", "cbz": b"PK\x03\x04", "pdf": b"%PDF-1.7\n"}


def blob(n: int, seed: int = 1, kind: str = "epub") -> bytes:
    """n bytes that start as a file of that kind does: a download looks at its file's first bytes."""
    return (STARTS[kind] + bytes((i * seed + i // 7) % 251 for i in range(n)))[:n]


class Gone:
    """A connection reset, or closed, before the address it reached could be read: the system says so (getpeername)."""

    def get_extra_info(self, what):
        raise OSError(57, "Socket is not connected")


# ---- itch.io, made up ------------------------------------------------------------------------------------------------

ITCHIO_KEY = "k" * 40


COOKIE = "c" * 30


MB = 1024 ** 2


GAMES = {  # id: title, classification, creator, files (name, size, storage, traits); 1-7 are owned
    1: ("A Field Guide to Moths", "book", "Ann Writer", [("guide.pdf", 30 * MB, "hosted", []), ("guide.epub", 2 * MB, "hosted", {})]),
    2: ("Lantern Comics #1", "comic", "Bea Artist", [("issue1.cbz", 50 * MB, "hosted", [])]),
    3: ("Hexcrawl Kit", "physical_game", "Cy Maker", [("hexcrawl.pdf", 10 * MB, "hosted", []), ("maps.zip", 200 * MB, "hosted", [])]),
    4: ("Space Blaster", "game", "Dee Coder", [("blaster-win.zip", 100 * MB, "hosted", ["p_windows"]), ("blaster.apk", 80 * MB, "hosted", [])]),
    5: ("Pocket Rules", "game", "Eve Designer", [("rules.pdf", 1 * MB, "hosted", [])]),  # a rulebook filed as a game
    6: ("Sprite Pack", "assets", "Fay Pixel", [("sprites.zip", 500 * MB, "hosted", [])]),
    7: ("Drive Zine", "other", "Gus Folder", [("zine on Google Drive", 0, "external", [])]),
    11: ("Tide Pool Tales", "book", "Hal Poet", [("tales.epub", 3 * MB, "hosted", {})]),  # free, so its files show
    12: ("Ironwood RPG", "physical_game", "Ida Press", [("core.pdf", 90 * MB, "hosted", {})] * 4),  # paid: they don't
    13: ("Neon Drift", "game", "Jo Racer", [("drift.exe", 300 * MB, "hosted", ["p_windows"])]),
    14: ("Paper Moons", "comic", "Kay Ink", [("moons1.cbz", 40 * MB, "hosted", {}), ("moons2.cbz", 40 * MB, "hosted", {})]),
    15: ("Hidden Thing", "book", "Lou Gone", [("thing.pdf", 5 * MB, "hosted", {})]),  # itch.io refuses to describe it
}


PRICES = {11: 0, 12: 1500, 13: 999, 14: 500, 15: 0}


ITCH_FILES = "files.itch.zone"


def body(upload: int) -> bytes:
    return f"itch.io file {upload}\n".encode() * 64


def game(i: int) -> dict:
    return {"id": i, "title": GAMES[i][0], "classification": GAMES[i][1], "url": f"https://maker.itch.io/{i}",
            "short_text": f"About {GAMES[i][0]}.", "cover_url": f"https://img.itch.zone/{i}.png", "min_price": PRICES.get(i, 1000),
            "published_at": "2021-05-01T10:00:00Z", "user": {"username": GAMES[i][2].lower(), "display_name": GAMES[i][2]}}


class FakeItch:
    def __init__(self):
        self.asked: list[str] = []
        self.gone: set[int] = set()  # uploads itch.io no longer has
        self.hidden = {15}  # games itch.io refuses to describe
        self.bodies: dict[int, bytes] = {}  # uploads whose file a creator has replaced, under the same upload id

    def body(self, upload: int) -> bytes:
        return self.bodies.get(upload, body(upload))

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        if request.url.host == ITCH_FILES:  # the storage a download link points to: no key needed, the link is signed
            assert request.headers.get("authorization") is None and request.url.params.get("sig")
            return httpx.Response(200, content=self.body(int(path.strip("/"))))
        self.asked.append(path)
        if request.headers.get("authorization") != ITCHIO_KEY:
            return httpx.Response(401, json={"errors": ["invalid key"]})
        if path == "/profile":
            return httpx.Response(200, json={"user": {"id": 9, "username": "reader", "display_name": "A Reader"}})
        if path == "/profile/owned-keys":
            ids = {"1": [1, 2, 3, 4], "2": [5, 6, 7]}.get(params["page"], [])
            return httpx.Response(200, json={"page": int(params["page"]), "per_page": 4, "owned_keys": [
                {"id": 1000 + i, "game_id": i, "downloads": 0, "game": game(i)} for i in ids]})
        if path.startswith("/games/") and path.endswith("/uploads"):
            i = int(path.split("/")[2])
            owned, key = i <= 7, params.get("download_key_id")
            if (owned and key != str(1000 + i)) or (not owned and PRICES[i]):
                return httpx.Response(200, json={"uploads": {}})  # not yours, and not free: nothing to see
            return httpx.Response(200, json={"uploads": [
                {"id": 100 * i + n, "filename": name, "size": size or None, "md5_hash": hashlib.md5(self.body(100 * i + n)).hexdigest(), "type": "default",
                 "storage": storage, "traits": traits} for n, (name, size, storage, traits) in enumerate(GAMES[i][3])]})
        if path.startswith("/uploads/") and path.endswith("/download"):
            upload = int(path.split("/")[2])
            if params.get("download_key_id") != str(1000 + upload // 100):
                return httpx.Response(400, json={"errors": ["invalid download key"]})
            if upload in self.gone:
                return httpx.Response(404, json={"errors": ["not found"]})
            return httpx.Response(302, headers={"location": f"https://{ITCH_FILES}/{upload}?sig=SIGNED{upload}"})
        if path in {f"/games/{i}" for i in self.hidden}:
            return httpx.Response(400, json={"errors": ["invalid game"]})
        if path.startswith("/games/"):
            return httpx.Response(200, json={"game": game(int(path.split("/")[2]))})
        return httpx.Response(404, json={"errors": ["not found"]})


def row(i: int, platforms=(), claimed: bool = False, flipped: bool = False) -> str:
    """An item as a bundle's page lists it; flipped writes the attributes in another order, as itch.io does now and then."""
    title, _, creator, files = GAMES[i]
    url = f"https://maker.itch.io/{i}"
    href = f"{url}/download/SECRETKEY{i}" if claimed else url
    icons = "".join(f'<span aria-hidden="true" class="icon" title="Available for {p}"></span> ' for p in platforms)
    game_id = f'<input type="hidden" name="game_id" value="{i}"/>' if flipped else f'<input value="{i}" name="game_id" type="hidden"/>'
    button = (f'<a href="{href}" class="button game_download_btn">Download</a>' if claimed else
              f'<form class="form" method="post"><input value="tok" name="csrf_token" type="hidden"/>{game_id}'
              f'<button value="claim" name="action" class="button">Download</button></form>')
    link = f'<a data-label="game:{i}:title" class="title game_link" href="{href}">' if flipped else f'<a href="{href}">'
    return (f'<div class="game_row"><a href="{href}"><div class="game_thumb"></div></a><div class="game_row_data">'
            f'<h2 class="game_title">{link}{title}</a></h2><div class="meta_row game_short_text">About {title}</div>'
            f'<div class="meta_row game_author">by <a href="https://maker.itch.io">{creator}</a></div>'
            f'<div class="meta_row file_count">{len(files)} file{"s" if len(files) != 1 else ""}</div><div class="meta_row">{icons}</div>'
            f'<div class="button_row">{button}</div></div></div>')


DELETED = ('<div class="game_row"><a href="https://maker.itch.io/deleted-1"><div class="game_thumb"></div></a>'
           '<div class="game_row_data"><h2 class="game_title"><a href="https://maker.itch.io/deleted-1">(deleted)</a></h2>'
           '<div class="meta_row game_author">by <a href="https://maker.itch.io">Nobody</a></div><div class="button_row"></div></div></div>')


class ItchSite:
    PAGES = {  # each page's items: (game id, what row() is told), or a row as it is
        ("/bundle/download/AAAkey111", "1"): [(3, {}), (11, {}), (12, {})],
        ("/bundle/download/AAAkey111", "2"): [(13, {"platforms": ["Windows"]}), (14, {}), DELETED],  # no claim button: gone
        ("/bundle/download/BBBkey222", "1"): [(12, {"flipped": True}), (15, {})],
    }

    def __init__(self):
        self.asked: list[str] = []
        self.claimed = {3}  # in your library already
        self.refuse: set[int] = set()  # claims itch.io won't take

    def rows(self, path: str, page: str) -> list[str]:
        return [spec if isinstance(spec, str) else row(spec[0], claimed=spec[0] in self.claimed, **spec[1])
                for spec in self.PAGES.get((path, page), [])]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request.url.path)
        if request.headers.get("cookie") != f"itchio={COOKIE}":
            return httpx.Response(302, headers={"location": "https://itch.io/login"})
        if request.method == "POST":  # Download on a bundle's page: the form posts to the page itself
            form = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
            gid, page = int(form["game_id"]), request.url.params.get("page", "1")
            on_page = any(not isinstance(s, str) and s[0] == gid for s in self.PAGES.get((request.url.path, page), []))
            if form != {"csrf_token": "tok", "game_id": str(gid), "action": "claim"} or not on_page or gid in self.refuse:
                return httpx.Response(200, text="<title>Something went wrong</title>")
            self.claimed.add(gid)
            return httpx.Response(302, headers={"location": f"https://maker.itch.io/{gid}/download/NEWKEY{gid}"})
        if request.url.path == "/my-purchases/bundles":
            return httpx.Response(200, text=(
                '<a href="/my-purchases/bundles" class="header_tab active">Bundles</a>'
                '<h2><a class="collection_title" href="/bundle/download/AAAkey111"><span class="collection_title_wrap">Bundle for Testing</span></a></h2>'
                '<h2><a href="https://itch.io/bundle/download/BBBkey222" class="collection_title"><span class="collection_title_wrap">Second &amp; Last</span></a></h2>'))
        page = request.url.params.get("page", "1")
        rows = self.rows(request.url.path, page)
        pager = '<a href="?page=2">2</a>' if request.url.path.endswith("AAAkey111") else ""
        return httpx.Response(200, text=f"<title>Download a bundle</title>{''.join(rows)}{pager}")


def itch(fake: FakeItch, key: str = ITCHIO_KEY) -> itchio.Itch:
    return itchio.Itch(key, transport=httpx.MockTransport(fake.handler), delay=0, sleep=lambda s: None)


def web(site: ItchSite, cookie: str = COOKIE) -> itchio_survey.Web:
    return itchio_survey.Web(cookie, transport=httpx.MockTransport(site.handler), delay=0, sleep=lambda s: None)


def survey(tmp_path, fake=None, site=None, **kw):
    with itch(fake or FakeItch()) as api, web(site or ItchSite()) as pages:
        return itchio_survey.survey(api, tmp_path, pages, log=lambda line: None, **kw)


# ---- DriveThruRPG, made up -------------------------------------------------------------------------------------------

DRIVETHRURPG_KEY = "k" * 32


BODIES = {(1, 0): b"referee rulebook " * 500, (1, 1): b"sheet " * 50, (2, 0): b"referee screen " * 80}


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def product(pid: int, name: str, publisher: str, files: list[tuple[int, str]], game: str = "Traveller") -> dict:
    """Shaped like the real answer (2026-10-28), trimmed to what is read plus a few fields that aren't."""
    return {
        "productId": 1000 + pid, "orderProductId": pid, "orderId": 77, "name": name, "isbn": "9780000000002" if pid == 1 else "",
        "datePurchased": None, "order": {"dateCreated": "2020-01-02T03:04:05-06:00", "orderId": 77},
        "publisher": {"name": publisher, "publisherId": 2, "slug": "x"}, "fileLastModified": "2026-01-02T03:04:05-05:00",
        "product": {"image": f"2/{1000 + pid}.jpg", "description": {"name": name, "shortDescription": "Science fiction\ufffdroleplaying..."}},
        "filters": [
            {"filterId": 1, "parentFilterId": 44826, "name": game, "parentName": "Rule System"},
            {"filterId": 2, "parentFilterId": 2140, "name": "Game Master Guides", "parentName": "Product Type"},
            {"filterId": 3, "parentFilterId": 200, "name": "Space Opera", "parentName": "Genre"},
            {"filterId": 4, "parentFilterId": 1000032, "name": "Human-Created Without AI", "parentName": "Format"},
        ],
        "files": [{"index": i, "filename": fn, "title": fn, "size": len(BODIES[(pid, i)]), "sizeMB": "0.01", "checksums": [
            {"checksum": "0" * 32, "checksumDate": "2024-01-01T00:00:00-05:00"},  # an older version
            {"checksum": md5(BODIES[(pid, i)]), "checksumDate": "2025-06-01T00:00:00-05:00"},
        ]} for i, fn in files],
    }


class DriveThruRPGAPI:
    def __init__(self):
        self.products = [
            product(1, "Far Stars: Referee Rulebook", "Example Games Inc.", [(0, "Referee Rulebook.pdf"), (1, "Crew Sheet.pdf")]),
            product(2, "Referee Screen Pack", "Example Games Inc.", [(0, "Referee Screen.pdf")]),
        ]
        self.tokens = 0
        self.expire_token_once = False
        self.preparing = {}  # (order id, index) -> how many more times to say "Preparing"
        self.links = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "api.drivethrurpg.com":
            path = url.path.removeprefix("/api/vBeta/")
            if path == "auth_key":
                if url.params.get("applicationKey") != DRIVETHRURPG_KEY:
                    return httpx.Response(401, json={"message": "invalid"})
                self.tokens += 1
                return httpx.Response(200, json={"token": f"T{self.tokens}", "refreshToken": "R", "refreshTokenTTL": 1})
            if request.headers.get("authorization") != f"T{self.tokens}":
                return httpx.Response(401)
            if self.expire_token_once:
                self.expire_token_once = False
                return httpx.Response(401)
            if path == "order_products":
                page, size = int(url.params["page"]), int(url.params["pageSize"])
                return httpx.Response(200, json=self.products[(page - 1) * size: page * size])
            parts = path.split("/")  # order_products/<id>/prepare|check
            order_id, index = int(parts[1]), int(url.params["index"])
            if self.preparing.get((order_id, index)):
                self.preparing[(order_id, index)] -= 1
                return httpx.Response(200, json={"url": "", "status": "Preparing download..."})
            self.links += 1
            return httpx.Response(200, json={"url": f"https://files.example.com/{order_id}/{index}?sig={self.links}", "status": "Ready"})
        if url.host == "files.example.com":
            order_id, index = (int(x) for x in url.path.strip("/").split("/"))
            return httpx.Response(200, content=BODIES[(order_id, index)])
        return httpx.Response(404)


# ---- StoryBundle, made up --------------------------------------------------------------------------------------------

LIBRARY_HTML = """<h2>MY BOOK BUNDLES</h2><div id="download_list_wrapper"><table><tbody>
<tr><td>
  <label><a href="https://storybundle.com/downloads/KEYA">The Example Game Book Bundle</a></label>
  <em>Purchased on January 1, 2020</em>
</td><td class="right"></td></tr>
<tr><td>
  <label><a href="https://storybundle.com/downloads/KEYB">The Example Newsletter Bonus</a></label>
  <em>Purchased on March 1, 2010</em>
</td><td class="right"></td></tr>
</tbody></table></div>"""


def book_li(book_id: int, title: str, size: str) -> str:
    return f"""<li class="book">
    <a rel="nofollow" data-method="post" href="https://storybundle.com/books/{book_id}/download/K{book_id}">
      <div class='image_wrapper'><img src="https://storybundle.com/system/book_covers/{book_id}/profile/c.jpg?17" /></div>
      <div class='title'><label title="{title}">
          {title}
            ({size})
        </label><input name="download" class="download_button" type="button" value="DOWNLOAD" /></div>
</a>  </li>"""


def bonus_li(book_id: int, cover: str) -> str:  # newsletter bonus pages: no class, no title label, no size
    return f"""<li>
          <a rel="nofollow" data-method="post" href="https://storybundle.com/books/{book_id}/download/K{book_id}">
            <img src="https://storybundle.com/system/book_covers/{book_id}/featured/{cover}?17" />
            <div class='title'><input name="download" class="download_button" type="button" value="DOWNLOAD" /></div>
            <p class="process">Please wait ...</p>
</a>        </li>"""


HEADER = '<ul><li class="text"><a href="https://storybundle.com/faq">What is StoryBundle?</a></li></ul>'


PAGES = {
    "/downloads/KEYA": '<meta name="csrf-token" content="TOKEN" />' + HEADER + book_li(1, "Opening the Box", "0.4MB") + book_li(2, "Day of the Lantern", "0.73MB"),
    "/downloads/KEYB": '<meta name="csrf-token" content="TOKEN" />' + HEADER + "<ul>" + bonus_li(2, "Day_of_the_Lantern_Cover.jpg")
    + bonus_li(3, "Hexes_and_Charms_Cover_Final.jpg") + "</ul>",
}


FILES = {1: ("Opening_the_Box_-_Ann_Writer.epub", b"PK\x03\x04" + b"x" * 410000),  # as big as listed, and as they start
         2: ("Day_of_the_Lantern.epub", b"PK\x03\x04" + b"y" * 760000), 3: ("Hexes_and_Charms.pdf", b"%PDF-1.4\n" + b"z" * 9000)}


class StoryBundleSite:
    def __init__(self):
        self.logged_in = True
        self.expired_once: set[int] = set()
        self.posts = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "storybundle.com":
            if not self.logged_in:
                return httpx.Response(302, headers={"location": "https://storybundle.com/"})
            if path == "/users/12345":
                return httpx.Response(200, text=LIBRARY_HTML)
            if path in PAGES:
                return httpx.Response(200, text=PAGES[path])
            if request.method == "POST" and path.startswith("/books/"):
                assert parse_qs(request.content.decode())["authenticity_token"] == ["TOKEN"]
                self.posts += 1
                book_id = int(path.split("/")[2])
                name = FILES[book_id][0]
                return httpx.Response(302, headers={"location": f"https://storybundle.s3.us-west-2.amazonaws.com/system/downloads/media/{book_id}/{name}?X-Amz-Expires=60&X-Amz-Signature=s{self.posts}"})
        if host.endswith("amazonaws.com"):
            book_id = int(path.split("/")[4])
            if book_id in self.expired_once:
                self.expired_once.discard(book_id)
                return httpx.Response(403, text="<Error><Code>AccessDenied</Code></Error>")
            data = FILES[book_id][1]
            return httpx.Response(200, content=data, headers={"etag": f'"{hashlib.md5(data).hexdigest()}"'})
        return httpx.Response(404)


# ---- the library page's code, under node -----------------------------------------------------------------------------

def page_part(start: str, end: str) -> str:
    """The page's code from the line that starts with the pattern start up to the line that starts with end."""
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    return re.search(rf"^{start}.*?(?=^{end})", page, re.S | re.M)[0]


def in_the_page(script: str, *parts: tuple[str, str]) -> object:
    """Runs the script under node with the page's code put in it: each part from the line that starts with its first
    pattern up to the line that starts with its second, in place of /* part N */. What the script writes is JSON."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't on this computer: the page's reader runs only in a browser here")
    for n, (start, end) in enumerate(parts, 1):
        script = script.replace(f"/* part {n} */", page_part(start, end))
    out = subprocess.run([node, "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(out.stdout)


OUTBOX_PAGE = """
const STORE = new Map(), SENT = [], said = [], shelvesRefused = [];
let REFUSE = false, OFF = false, reloaded = 0, leaving = false;
const localStorage = {  // REFUSE: writes fail, as in a full storage; OFF: reads too, as with it turned off
  getItem(k) { if (OFF) throw new Error("SecurityError"); return STORE.has(k) ? STORE.get(k) : null; },
  setItem(k, v) { if (OFF || REFUSE) throw new Error("QuotaExceededError"); STORE.set(k, String(v)); },
  removeItem(k) { STORE.delete(k); },
  get length() { return STORE.size; }, key: i => [...STORE.keys()][i] ?? null,
};
let answer = () => ({ status: 200, body: { ok: true } }), session = () => ({ by: "rory", me: { key: "k2" } });
const fetch = async (url, init = {}) => {  // "away": no network
  if (url === "/api/session") {
    const s = await session();
    if (s === "away") throw new TypeError("Failed to fetch");
    return { status: 200, ok: true, json: async () => s };
  }
  if (init.keepalive && new TextEncoder().encode(init.body).length > 65536) throw new TypeError("Failed to fetch");  // as a browser does
  const body = init.body === undefined ? null : JSON.parse(init.body);
  SENT.push([url, body, (init.headers || {})["X-Personal-Key"], !!init.keepalive]);
  const a = await answer(url, body);
  if (a === "away") throw new TypeError("Failed to fetch");
  return { status: a.status, ok: a.status >= 200 && a.status < 300,
           json: async () => { if (typeof a.body === "string") throw new SyntaxError("Unexpected token"); return a.body; } };
};
const setTimeout = () => 7, clearTimeout = () => {};
const location = { reload() { reloaded++; } };
const toast = text => said.push(text), sayLater = toast;
const shelfRefused = (key, p, d) => { shelvesRefused.push([key, d.error]); withdraw(key, p); };
const shelvesRefusedSaid = () => {};
const ITEMS = [{ id: "h:novel", ids: ["h:old"] }, { id: "s:7" }];
""" + page_part(r"// This browser's storage, as JSON", r"const ITEMS = ") + page_part(r"const idsOf = ", r"const SHOPS = ") + """
const R = { placeTimer: 0 }, EDIT = {};
let ME = { by: "rory", key: "k1" };
const box = () => JSON.parse(STORE.get("hylly.outbox.rory") || "{}");
const put = entries => STORE.set("hylly.outbox.rory", JSON.stringify(entries));
const tick = () => new Promise(done => setImmediate(done));
/* part 1 */
/* part 2 */
/* part 3 */
/* part 4 */
"""


def outbox_in_node(script: str, *more: tuple[str, str]) -> object:
    """Runs the page's outbox and its sender under node, with this browser's storage (STORE), the server (answer(url,
    body) gives {status, body}, or "away") and its /api/session (session()) as the script makes them, and ME rory. More
    parts of the page go where the script has /* part 5 */ and on. What the script writes is JSON."""
    return in_the_page(OUTBOX_PAGE + script, (r"const PLACE_WAIT = ", r"// The outbox: your changes"),
                       (r"const OUTBOX = ", r"function placeMoved\("), (r"// One change to the server\.", r"function ownPlace\("),
                       (r"// The session as the server has it now", r"async function keyRenewed\("), *more)


# ---- alarms and notes ------------------------------------------------------------------------------------------------

WINDOWS_FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"


def watch(**conf):
    sent, clock = [], [1_800_000_000.0]
    w = bell.Watch({**bell.DEFAULTS, "toast": False, **conf}, send=lambda title, text, prio, public=None: sent.append((title, text, prio)),
                     now=lambda: clock[0], log=lambda m: None)
    return w, sent, clock


JOIN = threading.Thread.join  # the real one: a test in test_bell_notes makes it raise, as Ctrl-C would


@contextlib.contextmanager
def note_threads_waited_for():
    """A note's thread goes on after job_note has stopped waiting for it. Left running past the test, it would write to the
    bell.log of the settings put back by then, your own, and could reach your phone: so the threads started inside are
    waited for before the test ends."""
    before = set(threading.enumerate())
    try:
        yield
    finally:
        started = set(threading.enumerate()) - before
        for thread in started:
            JOIN(thread, 10)
    assert not [thread.name for thread in started if thread.is_alive()]


# ---- this project's own files ----------------------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[1]


def git(*args: str, given: str = "") -> str:
    return subprocess.run(["git", *args], cwd=REPO, input=given, capture_output=True, text=True, encoding="utf-8",
                          check=True).stdout


def folders_of(files) -> set[str]:
    return {"/".join(f.split("/")[:n]) for f in files for n in range(1, f.count("/") + 1)}


@functools.cache
def kept() -> tuple[str, ...] | None:
    """The files git keeps or would take in (not what .gitignore keeps out), or None without git."""
    try:
        listed = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    except (OSError, subprocess.CalledProcessError):
        return None
    return tuple(f for f in listed if f and (REPO / f).is_file())


@functools.cache
def published() -> frozenset[str] | None:
    """What git archive would publish: the files kept, less those marked export-ignore themselves or by a folder they
    are in, and the folders they're in. None without git."""
    files = kept()
    if files is None:
        return None
    asked = sorted(set(files) | folders_of(files))
    said = git("check-attr", "-z", "--stdin", "export-ignore", given="\0".join(asked)).split("\0")
    left_out = {path for path, value in zip(said[0::3], said[2::3]) if value == "set"}
    out = [f for f in files if not left_out & ({f} | folders_of([f]))]
    return frozenset(out) | frozenset(folders_of(out))


BLOCK = {"|": "\n", "|-": "\n", "|+": "\n", ">": " ", ">-": " ", ">+": " "}  # a value on the lines below, joined so


def workflow(name: str, root: Path = Path(__file__).parents[1]) -> dict:
    """A workflow of .github/workflows as GitHub reads it, as far as Hylly's go: mappings, lists in brackets or an item
    a line, plain and quoted words, a value on the lines below (run: |), and comments, of a line or after a value.
    YAML's own reader isn't one of Hylly's dependencies, and a file this reader can't take fails the test that reads
    it."""
    lines: list[tuple[int, str, bool]] = []  # (indent, text, a line of a value on the lines below)
    below = None  # the indent of the key whose value is on the lines below, while they are read
    for raw in (root / ".github" / "workflows" / name).read_text(encoding="utf-8").splitlines():
        indent, text = len(raw) - len(raw.lstrip(" ")), raw.strip()
        if below is not None and (not text or indent > below):
            lines.append((indent, raw.rstrip(), True))
            continue
        below = None
        if not text or text.startswith("#"):
            continue
        lines.append((indent, text, False))
        if uncommented(text).rpartition(":")[2].strip() in BLOCK:
            below = indent + (2 if text.startswith("- ") else 0)

    def word(text: str):
        text = uncommented(text).strip()
        if text.startswith("[") and text.endswith("]"):
            return [word(part) for part in text[1:-1].split(",") if part.strip()]
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
            return text[1:-1]
        return text or None

    def mapping(i: int, indent: int) -> tuple[dict, int]:
        found = {}
        while i < len(lines) and lines[i][0] == indent and not lines[i][2] and not lines[i][1].startswith("- "):
            key, colon, rest = lines[i][1].partition(":")
            assert colon, f"{name}: not a key and its value: {lines[i][1]}"
            i += 1
            if uncommented(rest).strip() in BLOCK:
                text = []
                while i < len(lines) and lines[i][2]:
                    text.append(lines[i][1])
                    i += 1
                least = min((len(t) - len(t.lstrip(" ")) for t in text if t.strip()), default=0)
                found[key] = BLOCK[uncommented(rest).strip()].join(t[least:] for t in text).strip()
            elif rest.strip():
                found[key] = word(rest)
            elif i < len(lines) and (lines[i][0] > indent or lines[i][1].startswith("- ")):
                found[key], i = block(i, lines[i][0])
            else:
                found[key] = None
        return found, i

    def block(i: int, indent: int):
        if not lines[i][1].startswith("- "):
            return mapping(i, indent)
        items = []
        while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
            item = lines[i][1][2:]
            if re.match(r"[\w-]+:(\s|$)", item):  # an item that is a mapping, its other keys below its first
                lines[i] = (indent + 2, item, False)
                value, i = mapping(i, indent + 2)
            else:
                value, i = word(item), i + 1
            items.append(value)
        return items, i

    found, end = mapping(0, 0)
    assert end == len(lines), f"{name}: read only as far as {lines[end][1]}"
    return found


def uncommented(text: str) -> str:
    """A line without its comment: from a # after a space, outside quotes, to its end."""
    quote = ""
    for n, c in enumerate(text):
        if quote:
            quote = "" if c == quote else quote
        elif c in "\"'":
            quote = c
        elif c == "#" and (n == 0 or text[n - 1] in " \t"):
            return text[:n].rstrip()
    return text
