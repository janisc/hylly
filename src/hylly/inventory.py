"""Flattening Humble order data into products and their downloadable files."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

MIB = 1024 * 1024

GAME_PLATFORMS = {"windows", "mac", "linux", "android", "asmjs", "ios"}
# Which formats are books, audio or video (formats.toml), and the labels that make a file an extra, audio or
# video whatever its format ([labels] in kinds.toml), are rules: rules.current().


@dataclass
class FileOption:
    label: str  # Humble's own name for it, e.g. "EPUB" or "PDF (HQ)"
    ext: str
    filename: str
    size: int
    md5: str | None
    role: str  # book | extra | audio | video | game | other
    url: str = ""  # signed and working only in freshly fetched data, not in snapshots

    @property
    def mb(self) -> float:
        return self.size / MIB


@dataclass
class Product:
    """One item of one purchase. The same book bought twice is two products."""

    gamekey: str
    purchased: str  # YYYY-MM-DD
    bundle: str
    machine_name: str
    title: str
    publisher: str
    icon: str | None
    files: list[FileOption] = field(default_factory=list)
    external_links: list[tuple[str, str]] = field(default_factory=list)  # (platform, url)

    def by_role(self, role: str) -> list[FileOption]:
        return [f for f in self.files if f.role == role]

    @property
    def books(self) -> list[FileOption]:
        return self.by_role("book")


@dataclass
class KeyItem:
    """A redeemable key (Steam and so on) that came with a purchase."""

    purchased: str
    bundle: str
    title: str
    key_type: str
    redeemed: bool


def link_name(url: str) -> str:
    """A file's name from its link: the link's last part, decoded (%20 a space), as StoryBundle's names are. None of
    Humble's links has been seen escaping a name, so this changes no file's path; it is a safeguard. The download
    finds a file's fresh link by this name (humble.Library.link), so both read a link the same way."""
    return unquote(PurePosixPath(urlsplit(url).path).name)


def file_ext(filename: str, label: str) -> str:
    suffix = PurePosixPath(filename).suffix.lower().lstrip(".")
    if suffix:
        return suffix
    words = re.findall(r"[a-z0-9]+", (label or "").lower())
    return words[0] if words else ""


def file_role(platform: str, ext: str, label: str) -> str:
    from . import rules

    r = rules.current()
    if platform == "audio" or ext in r.audio_formats or r.labels["audio"].find(label or "") is not None:
        return "audio"
    if platform == "video" or ext in r.video_formats or r.labels["video"].find(label or "") is not None:
        return "video"
    if platform in GAME_PLATFORMS:
        return "game"
    if platform == "ebook" and ext in r.shop_formats and r.labels["extra"].find(label or "") is None:
        return "book"
    if platform in ("ebook", "other"):
        return "extra"
    return "other"


def parse_orders(orders: dict) -> tuple[list[Product], list[KeyItem]]:
    """Products oldest purchase first, and every key that came with a purchase."""
    products: list[Product] = []
    keys: list[KeyItem] = []
    for gamekey, order in sorted(orders.items(), key=lambda kv: (kv[1] or {}).get("created") or ""):
        if not order:
            continue
        info = order.get("product") or {}
        bundle = (info.get("human_name") or "Unknown purchase").strip()
        purchased = (order.get("created") or "")[:10]
        for sub in order.get("subproducts") or []:
            if sub.get("library_family_name") == "hidden" and not sub.get("downloads"):
                continue  # notices such as "PDF Quality", not items
            products.append(_product(gamekey, purchased, bundle, sub))
        for tpk in (order.get("tpkd_dict") or {}).get("all_tpks") or []:
            keys.append(
                KeyItem(
                    purchased=purchased,
                    bundle=bundle,
                    title=(tpk.get("human_name") or tpk.get("machine_name") or "").strip(),
                    key_type=tpk.get("key_type_human_name") or tpk.get("key_type") or "",
                    redeemed=bool(tpk.get("key_redeemed") or tpk.get("redeemed_key_val")),
                )
            )
    return products, keys


def _product(gamekey: str, purchased: str, bundle: str, sub: dict) -> Product:
    p = Product(
        gamekey=gamekey,
        purchased=purchased,
        bundle=bundle,
        machine_name=sub.get("machine_name") or "",
        title=(sub.get("human_name") or sub.get("machine_name") or "Untitled").strip(),
        publisher=((sub.get("payee") or {}).get("human_name") or "").strip(),
        icon=sub.get("icon"),
    )
    seen: set[tuple[str, str | None]] = set()
    for dl in sub.get("downloads") or []:
        platform = (dl.get("platform") or "").lower()
        for struct in dl.get("download_struct") or []:
            url = (struct.get("url") or {}).get("web")
            if not url:
                if struct.get("external_link"):
                    p.external_links.append((platform, struct["external_link"]))
                continue
            filename = link_name(url)
            md5 = struct.get("md5")
            if (filename, md5) in seen:  # the same file listed twice
                continue
            seen.add((filename, md5))
            label = struct.get("name") or ""
            ext = file_ext(filename, label)
            p.files.append(
                FileOption(
                    label=label,
                    ext=ext,
                    filename=filename,
                    size=int(struct.get("file_size") or 0),
                    md5=md5,
                    role=file_role(platform, ext, label),
                    url=url,
                )
            )
    return p
