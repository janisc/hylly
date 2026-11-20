"""What people think of a title: their stars, 1 to 5, and a few words if they like, shown to everyone who may see the
library while [reviews] in settings.toml has them on (who); off by default. With moderation on, the words wait for an
owner to look at them before anyone else sees them; stars never wait.

Each person's are kept with the rest of what's kept about them (people.py), in the database's table review: each as it
now stands, which step() made of it. The events themselves aren't kept. Renaming, removing and exporting a person take
them along. Everyone's are read together for what a title's details show, and for the owners' queue.

The rules are a table, STEPS: in a state, an event leads to another state, or isn't allowed there. step() applies one
event to what a person has said of one title and does nothing else, no database and no clock, so that the tests can try
every state with every event. Whether words wait is the server's to say: it sends submit for words that wait and write
for words that don't, so step() needn't know.

    state            what others see       what the writer sees
    (none)           nothing
    rated            the stars
    waiting          the stars             their words, waiting
    shown            the stars and words
    edit-waiting     the earlier words     their change, waiting
    declined         the stars             their words, not released, and the owner's note
    edit-declined    the earlier words     their change, not released, and the note

    the writer's     rate n       any state: those stars, the state as it was
                     unrate       rated: nothing kept (with words, delete)
                     write        any state: shown, the words out at once
                     submit       none, rated, waiting, declined: waiting; shown, edit-waiting, edit-declined: edit-waiting
                     unwrite      any state with words: rated, the stars kept
                     delete       any state: nothing kept
    an owner's       release      waiting, edit-waiting: shown (the change, for an edit)
                     decline      waiting: declined; edit-waiting: edit-declined, with a note
                     remove       any state with words: rated, the words gone

release and decline name the version (rev) the owner saw: words changed since are refused, so nobody releases words they
didn't see. Words taken away (unwrite, remove, delete) are gone from the database's file too, not only from what's
shown (people.py).
"""

from __future__ import annotations

import re
import unicodedata

from .access import ROLES

RATED, WAITING, SHOWN, EDIT_WAITING, DECLINED, EDIT_DECLINED = "rated", "waiting", "shown", "edit-waiting", "declined", "edit-declined"
STATES = (RATED, WAITING, SHOWN, EDIT_WAITING, DECLINED, EDIT_DECLINED)
WORDS = (WAITING, SHOWN, EDIT_WAITING, DECLINED, EDIT_DECLINED)  # states with words in them
SEEN = (SHOWN, EDIT_WAITING, EDIT_DECLINED)  # states whose words (text) others see
FIRST, EDITED = (None, RATED, WAITING, DECLINED), (SHOWN, EDIT_WAITING, EDIT_DECLINED)
STEPS: dict[tuple[str | None, str], str | None] = {  # (state, event): the state after it; None: nothing kept
    **{(s, "rate"): s or RATED for s in (None, *STATES)},
    (RATED, "unrate"): None,
    **{(s, "write"): SHOWN for s in (None, *STATES)},
    **{(s, "submit"): WAITING for s in FIRST},
    **{(s, "submit"): EDIT_WAITING for s in EDITED},
    **{(s, "unwrite"): RATED for s in WORDS},
    **{(s, "delete"): None for s in STATES},
    (WAITING, "release"): SHOWN, (EDIT_WAITING, "release"): SHOWN,
    (WAITING, "decline"): DECLINED, (EDIT_WAITING, "decline"): EDIT_DECLINED,
    **{(s, "remove"): RATED for s in WORDS},
}
EVENTS = ("rate", "unrate", "write", "submit", "unwrite", "delete", "release", "decline", "remove")
OWNERS = ("release", "decline", "remove")  # an owner's events; the rest are the writer's own
STARS = (1, 2, 3, 4, 5)
TEXT_MAX, NOTE_MAX = 1000, 300  # characters, a review's words and an owner's note: the library page has them from /api/session
WHO = ("off", "borrower", "reader", "librarian", "owner")  # [reviews] who: the lowest role that may give stars and write; off: none, and nothing shows
# What hides in words or turns them round, which nobody means to type, as clean takes it out: the zero-width space and
# the byte order mark, and the marks that override or isolate the direction of the text after them. The two joiners
# (U+200C, U+200D), which Persian, the Indic scripts and emoji need (a family emoji is faces joined by U+200D), and
# the marks of direction alone (U+200E, U+200F) stay. Written as escapes, so that what is taken can be read here.
UNSEEN = re.compile("[\u200b\ufeff\u202a-\u202e\u2066-\u2069]")
# What makes markup anywhere in a line: emphasis, code, links and pictures, a table, a strike, HTML and the addresses
# in angle brackets, and & only where it starts what reads as a character's name (&amp;)
MARKDOWN_OWN = re.compile(r"([\\`*_\[\]~|<]|&(?=#?[0-9A-Za-z]+;))")
# What makes a block at a line's start: a heading (# and a space), a quote, a list (- or + and a space, 3. and a space:
# 3.5 is no list), a heading's underline or a rule (a line of = or -). * and _ are escaped wherever they are
BLOCK_START = re.compile(r"^([ \t]*)(#{1,6}(?=[ \t]|$)|>|[+-](?=[ \t]|$)|=+(?=[ \t]*$)|-+(?=[ \t]*$)"
                         r"|\d{1,9}[.)](?=[ \t]|$))", re.M)
CLOSING = re.compile(r"(?<=[ \t])#+$")  # what a heading's line ends with that Markdown would take away


class Refused(ValueError):
    """An event that isn't allowed where a review stands, or isn't one at all."""


def state_of(review: dict | None) -> str | None:
    return review.get("state") if review else None


def clean(text, most: int = TEXT_MAX) -> str:
    """Words as a person typed them, made safe to keep and show: no control characters but line ends, nor half of a
    character (a lone surrogate, which JSON can carry and no database can keep), nothing that turns text round or hides
    in it (UNSEEN), at most two empty lines together, trimmed. Every other character stays as typed, the joiners emoji
    and Persian need, a soft hyphen and a private character among them. The one cleaner for what people write: a
    review's words, an owner's note on them, and a report's comment (reports.clean). Raises Refused for what isn't text
    or is too long."""
    if not isinstance(text, str):
        raise Refused("words are text")
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = UNSEEN.sub("", "".join(c for c in text if c == "\n" or unicodedata.category(c) not in ("Cc", "Cs")))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in text.split("\n"))).strip()
    if len(text) > most:
        raise Refused(f"at most {most} characters")
    return text


def markdown(text, one_line: bool = False) -> str:
    """Someone's words, or a title, as Markdown shows them as they are (best_of), and as they read in the file itself,
    which is a text to share or print: a \\ before each of Markdown's own characters only where it would make markup
    (MARKDOWN_OWN, BLOCK_START), the way CommonMark keeps any of them as it is, so the words make no HTML, link,
    picture, heading or list of their own. one_line: every line end and run of spaces as one space, for a title, an
    author or a name, which a line end would carry out of its heading; it starts no line, and a # at its end stays."""
    text = str(text)
    if one_line:
        return CLOSING.sub(lambda m: f"\\{m[0]}", MARKDOWN_OWN.sub(r"\\\1", " ".join(text.split())))
    text = MARKDOWN_OWN.sub(r"\\\1", text)
    return BLOCK_START.sub(lambda m: m[1] + (f"{m[2][:-1]}\\{m[2][-1]}" if m[2][0].isdigit() else f"\\{m[2]}"), text)


def _stars(event: dict) -> int:
    stars = event.get("stars")
    if isinstance(stars, bool) or stars not in STARS:
        raise Refused(f"{stars!r} stars")
    return stars


def _words(event: dict) -> str:
    text = event.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX:
        raise Refused(f"words, 1 to {TEXT_MAX} characters")
    return text


def step(review: dict | None, event: dict) -> dict | None:
    """What one event makes of a person's review of a title: the review after it, or None for nothing kept. Neither is
    changed. Raises Refused for an event the table doesn't allow there, or one that isn't whole."""
    op, now = event.get("op"), state_of(review)
    if (now, op) not in STEPS:
        raise Refused(f"{op!r} where it stands {now or 'unrated'}")
    after = STEPS[now, op]
    if after is None:
        return None
    if op in ("release", "decline") and event.get("rev") != review.get("rev"):
        raise Refused("the words have changed since")
    was = dict(review or {})
    stamp = {"when": event.get("when", ""), "time": event.get("time", 0)}
    if op == "rate":
        return {**was, "state": after, "id": event["id"], "t": event.get("t", was.get("t", "")), "f": event.get("f", was.get("f", "")),
                "stars": _stars(event), **stamp}
    if op in ("write", "submit"):
        words, spoiler = _words(event), bool(event.get("spoiler"))
        base = {**was, "state": after, "id": event["id"], "t": event.get("t", was.get("t", "")), "f": event.get("f", was.get("f", "")),
                "stars": _stars(event), "rev": was.get("rev", 0) + 1, "note": "", **stamp}
        if after == EDIT_WAITING:  # the words shown stay shown; the change waits beside them
            return {**base, "change": words, "change_spoiler": spoiler}
        return {**base, "text": words, "spoiler": spoiler, "change": "", "change_spoiler": False}
    if op in ("unwrite", "remove"):
        return {k: v for k, v in was.items() if k not in ("text", "spoiler", "change", "change_spoiler", "note")} | {"state": after, **stamp}
    if op == "release":
        if now == EDIT_WAITING:
            return {**was, "state": after, "text": was["change"], "spoiler": was.get("change_spoiler", False), "change": "", "change_spoiler": False, "note": ""}
        return {**was, "state": after, "note": ""}
    if op == "decline":
        return {**was, "state": after, "note": clean(event.get("note", ""), NOTE_MAX)}
    raise Refused(f"{op!r}")  # in the table, but nothing here says what it does


def diagram() -> str:
    """The table as a Mermaid state diagram, for docs/reviews.md: a test keeps the two alike, so the picture can't drift
    from the rules."""
    ident = lambda s: "[*]" if s is None else s.replace("-", "_")  # noqa: E731
    lines = ["stateDiagram-v2"] + [f'    state "{s}" as {ident(s)}' for s in STATES if "-" in s]
    joined: dict[tuple[str, str], list[str]] = {}
    for (now, op), after in STEPS.items():
        joined.setdefault((ident(now), ident(after)), []).append(op)
    lines += [f"    {a} --> {b}: {', '.join(ops)}" for (a, b), ops in joined.items()]
    return "\n".join(lines)


def best_of(found: dict[str, list[dict]], titles: dict[str, dict], year: str) -> str:
    """A year's reviews as Markdown, for a family's "best of the year": the titles rated that year, the best first, each
    with who gave what and the words others may read. found: each person's reviews; titles: the catalog's, by any id."""
    by_title: dict[str, list[tuple[str, dict]]] = {}
    for name, said in found.items():
        for r in said:
            item = titles.get(r["id"])
            if item and not r.get("gone") and str(r.get("when", "")).startswith(year):
                by_title.setdefault(item["id"], []).append((name, r))
    def rank(entry):
        stars = [r["stars"] for _, r in entry[1]]
        return (-sum(stars) / len(stars), -len(stars), titles[entry[0]].get("t", ""))
    out = [f"# The best of {year}", ""]
    for title_id, said in sorted(by_title.items(), key=rank):
        item, stars = titles[title_id], [r["stars"] for _, r in said]
        by = ", ".join(markdown(a, one_line=True) for a in (item.get("a") or [])[:2])
        out += [f"## {markdown(item.get('t', title_id), one_line=True)}" + (f", by {by}" if by else ""), "",
                f"{'★' * round(sum(stars) / len(stars))} {sum(stars) / len(stars):.1f} from {len(stars)}: "
                + " · ".join(f"{markdown(name, one_line=True)} {r['stars']}"
                             for name, r in sorted(said, key=lambda x: (-x[1]["stars"], x[0]))), ""]
        for name, r in sorted(said, key=lambda x: x[0]):
            if r.get("state") in SEEN:  # words others may read: as they were written, never as markup (markdown),
                # each line a line of its own (a hard break, two spaces, at its end), an empty one a paragraph's end
                ending = " (tells how it ends)" if r.get("spoiler") else ""
                out += [f"> {line}  " if line else ">" for line in markdown(r["text"]).split("\n")]
                out += [f"> *{markdown(name, one_line=True)}*{ending}", ""]
    if len(out) == 2:
        out.append(f"No titles were rated in {year}.")
    return "\n".join(out).rstrip() + "\n"


def may_rate(who: str, role: str) -> bool:
    """Whether someone of this role may give stars and write, when the lowest allowed is who."""
    return who in WHO[1:] and role in ROLES and ROLES.index(role) >= ROLES.index(who)


def waits(moderation: str, role: str) -> bool:
    """Whether words from someone of this role wait for an owner. An owner's own never do."""
    return role != "owner" and (moderation == "on" or (moderation == "borrowers" and role == "borrower"))


def public(review: dict) -> dict:
    """What others see of someone's review: the stars, and the words that are out."""
    seen = review.get("state") in SEEN
    return {"stars": review["stars"], "when": review.get("when", ""), **({"text": review["text"], "spoiler": bool(review.get("spoiler"))} if seen else {})}


def own(review: dict) -> dict:
    """What the writer sees of their own: all of it."""
    keys = ("state", "stars", "text", "spoiler", "change", "change_spoiler", "note", "rev", "when")
    return {k: review[k] for k in keys if k in review}
