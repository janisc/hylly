"""Order snapshots on disk, without the parts that work as credentials: each shop's in its own folder in the library's
state folder (shops.Shop.state), named by when it was made, <date>_<time>.json (shops.Shop.snapshot, NAME). A run saves
one only when what the shop listed has changed since the newest: the history keeps the changes, not a copy a run. After
a new one is saved, older ones are thinned (thin): a shop keeps its newest KEEP and the newest of each month before
them, and never the one just saved. The run says which went (Thinned), and thinning is housekeeping: what it can't do is
said, and stops nothing.

Download links are signed and expire; stripped of their signature they no longer work
but still tell us the file name. Redeemed game keys are replaced by a yes/no flag.
The order keys themselves stay (fetching fresh links needs them), so the state folder must stay private.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from .shops import SHOPS
from .storage import atomic_write

SIGNING_PARAMS = {"ttl", "t", "gamekey", "signature", "expires", "key-pair-id", "policy", "token", "hmac"}
SECRET_FIELDS = {"redeemed_key_val"}
NAME = re.compile(r"(\d{4}-\d\d)-\d\d_\d{4}\.json")  # a snapshot's name in its shop's folder: nothing else there is one
KEEP = 5  # the newest snapshots a shop keeps, with the newest of each month before them


def _strip_url(value: str) -> str:
    parts = urlsplit(value)
    if not parts.query:
        return value
    names = {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
    if names & SIGNING_PARAMS:
        return urlunsplit(parts._replace(query=""))
    return value


def sanitize(data):
    """A copy of the order data with signed links unsigned and redeemed keys removed."""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if k in SECRET_FIELDS:
                out["key_redeemed"] = bool(v)
            else:
                out[k] = sanitize(v)
        return out
    if isinstance(data, list):
        return [sanitize(v) for v in data]
    if isinstance(data, str) and data.startswith(("http://", "https://")):
        return _strip_url(data)
    return data


def folder(state: Path, shop: str) -> Path:
    """The shop's own folder in the library's state folder (shops.Shop.state)."""
    return state / SHOPS[shop].state


@dataclass
class Thinned:
    """What thinning did as a shop's new snapshot was saved (put): the older snapshots it removed, and what it couldn't
    do, each with why (those stay, for the next time)."""
    removed: list[str] = field(default_factory=list)
    trouble: list[str] = field(default_factory=list)

    def lines(self, shop: str) -> list[str]:
        """What a run says of it: a line for what was removed, and one for what couldn't be; none for nothing."""
        where = f"state/{SHOPS[shop].state}/"
        out = []
        if self.removed:
            n = len(self.removed)
            out.append(f"Older snapshots thinned: {n} removed from {where} (the newest {KEEP} and the newest of each month "
                       f"before them stay): {', '.join(self.removed)}.")
        if self.trouble:
            out.append(f"Older snapshots not thinned in {where}: {'; '.join(self.trouble)}. They stay, and the next run "
                       "thins them.")
        return out


def put(state: Path, shop: str, text: str, thinned: Thinned | None = None) -> Path:
    """text as the shop's new snapshot, and then its older ones thinned (thin), never this one: what that did is in
    thinned. Returns where it is. Thinning is housekeeping, so trouble with it (a folder that can't be listed just now)
    is said in thinned, and the run goes on: the snapshot is saved. A text the same as the shop's newest snapshot's
    isn't saved again, as a run with no change at the shop would only add a copy: that one holds it, and is returned,
    and nothing is thinned."""
    same = _newest_if_same(state, shop, text)
    if same is not None:
        return same
    path = state / SHOPS[shop].snapshot(datetime.now())
    atomic_write(path, text)
    done = thinned if thinned is not None else Thinned()
    try:
        thin(state, shop, also=path.name, into=done)
    except OSError as e:
        done.trouble.append(f"its folder can't be listed just now ({e.strerror or e})")
    return path


def _newest_if_same(state: Path, shop: str, text: str) -> Path | None:
    """The shop's newest snapshot by name when it holds this very text; None for none, another text, or one that can't
    be read just now (a new one is saved then, as ever)."""
    try:
        found = names(state, shop)
        newest = folder(state, shop) / found[-1] if found else None
        return newest if newest is not None and newest.read_text(encoding="utf-8") == text else None
    except (OSError, ValueError):  # ValueError: a file that isn't UTF-8 text (UnicodeDecodeError)
        return None


def save(orders: dict, state: Path, shop: str, thinned: Thinned | None = None) -> Path:
    """The shop's orders as a new snapshot, sanitized (put)."""
    return put(state, shop, json.dumps(sanitize(orders), indent=1, ensure_ascii=False, sort_keys=True), thinned)


def names(state: Path, shop: str) -> list[str]:
    """The names of the shop's snapshots, oldest first: those named as a snapshot is (NAME), and nothing else of its
    folder. A folder that isn't there has none; one that can't be listed raises OSError, as Path.glob would say none
    (Python 3.12) and the catalog would then drop every title of that shop."""
    try:
        found = os.listdir(folder(state, shop))
    except FileNotFoundError:
        return []
    return sorted(name for name in found if NAME.fullmatch(name))


def kept(found: list[str], keep: int = KEEP) -> set[str]:
    """Of a shop's snapshots by name, oldest first, those thinning keeps: the newest keep of them, and of each month
    before, its newest. The newest of all is always one of them."""
    out, months = set(found[-keep:] if keep > 0 else found[-1:]), set()
    for name in reversed(found):
        month = NAME.fullmatch(name)[1]
        if month not in months:
            months.add(month)
            out.add(name)
    return out


def thin(state: Path, shop: str, keep: int = KEEP, also: str | None = None, into: Thinned | None = None) -> list[str]:
    """The shop's older snapshots removed, but its newest keep and the newest of each month before them (kept), and
    also, the one just saved, whatever its name: a clock that was ahead may have named others later. The snapshots are
    a history of what it listed, and one a run would be thousands in a few years. Returns the names removed, and puts
    them in into, with each that couldn't be removed now (held open on a share, say), which stays for the next time. A
    folder that can't be listed raises OSError (names)."""
    found = names(state, shop)
    stay = kept(found, keep) | ({also} if also else set())
    into = into if into is not None else Thinned()
    gone = []
    for name in found:
        if name in stay:
            continue
        try:
            os.remove(folder(state, shop) / name)
        except FileNotFoundError:  # another run's thinning took it just now
            continue
        except OSError as e:
            into.trouble.append(f"{name} ({e.strerror or e})")
            continue
        gone.append(name)
    into.removed += gone
    return gone


def latest(state: Path, shop: str) -> tuple[Path, object] | None:
    """The shop's newest snapshot that can be read, with what it holds (load's): one cut short (by an older version, or
    a copy) is passed over. Read once, as it's read whole to tell it's whole: a caller goes by what it holds, rather
    than read a few megabytes of orders again."""
    return next(newest_first(state, shop), None)


def newest_first(state: Path, shop: str) -> Iterator[tuple[Path, object]]:
    """Every snapshot of the shop's that can be read, newest first, each with what it holds (latest's rules)."""
    for name in reversed(names(state, shop)):
        path = folder(state, shop) / name
        try:
            data = load(path)
        except ValueError:
            continue
        yield path, data


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))
