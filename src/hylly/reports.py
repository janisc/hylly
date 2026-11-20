"""Reports of a title's file: a bad scan, missing pages or the wrong file, told in a small dialog with set reasons and a
comment, so that a better copy can be found. Anyone logged in may report; owners and librarians see the reports and go
through them; each report is kept with what was decided.

Two kinds of record:
- A person's report, with what's kept about them (people.py): which title, the reasons, a comment. A newer one of
  theirs on the same title replaces it; they can take it back. Renaming, removing or exporting a person takes theirs along.
- The library's word on a title, here, in the database (report_decision): "better", a better copy is wanted and the
  title stays marked for a later batch of replacing; or "fine", nothing to do. Owners and librarians decide.

A title waits while it has a report newer than the last decision on it; after "better" and nothing newer, it's marked;
after "fine" and nothing newer, it's clear. A decision names the newest report its maker saw, so that a report that came
meanwhile still waits."""

from __future__ import annotations

import math
import time
from pathlib import Path

from . import db, reviews

# The reasons, in the order shown, and how many characters a report's words may have: the library page has them from
# /api/session (serve.py), with no copy of its own, so that what it offers is what's kept here
REASONS = {
    "quality": "Bad quality",
    "resolution": "Low resolution",
    "pages": "Pages missing or out of order",
    "wrong": "Wrong file",
    "content": "Questionable content",
    "else": "Something else",
}
MOST_WORDS = 500
WAITING, BETTER = "waiting", "better"
DECISIONS = {"better": "Better copy wanted", "fine": "Fine as it is"}


class Refused(ValueError):
    """What was sent isn't a report: the words say why."""


def clean(reasons, comment) -> tuple[list[str], str]:
    """The reasons, known ones only, each once, in REASONS' order; the comment cleaned as a review's words are
    (reviews.clean: no control characters but line ends, nothing that turns text round or hides in it, the joiners emoji
    and Persian need kept). Refused when there's no reason, an unknown one, too many words, or "Something else" alone
    with nothing said."""
    if not isinstance(reasons, list) or not reasons or not all(isinstance(r, str) for r in reasons):
        raise Refused("at least one reason, please")
    unknown = [r for r in reasons if r not in REASONS]
    if unknown:
        raise Refused("no such reason")
    if comment is None:
        comment = ""
    if not isinstance(comment, str):
        raise Refused("the comment is words")
    try:
        comment = reviews.clean(comment, MOST_WORDS)
    except reviews.Refused as e:  # too long
        raise Refused(str(e)) from None
    picked = [r for r in REASONS if r in reasons]
    if picked == ["else"] and not comment:
        raise Refused("say what, for something else")
    return picked, comment


DECISION = db.Shape("report_decision", (("id", "title", "text"), ("t", "title_t", "text"), ("f", "title_f", "text"),
                                        ("decision", "decision", "text"), ("by", "decided_by", "text"), ("upto", "upto", "real"),
                                        ("when", "local_time", "text"), ("time", "time", "real")), op="decide")


def store(where: Path | None = None) -> db.Database:
    """The database: the data folder's, unless named."""
    return db.database(where)


class Decisions:
    """The library's word on reported titles, in the database (report_decision)."""

    def __init__(self, where: Path | None = None) -> None:
        self.where = where

    def latest(self) -> dict[str, dict]:
        """The last decision on each title, by its id."""
        with store(self.where).read() as c:
            rows = c.execute("SELECT * FROM report_decision WHERE seq IN (SELECT max(seq) FROM report_decision GROUP BY title) ORDER BY seq").fetchall()
        return {row["title"]: DECISION.record(row) for row in rows}

    def decide(self, title: dict, decision: str, by: str, upto: float) -> dict:
        """A decision on a title (curation.snapshot of it), covering the reports up to the time upto."""
        if decision not in DECISIONS:
            raise Refused("no such decision")
        if upto_of({"upto": upto}) != upto:  # a finite number reads back as itself; NaN would be kept as nothing
            raise Refused("no such time")
        record = {"op": "decide", "id": title["id"], "t": title.get("t", ""), "f": title.get("f", ""), "decision": decision,
                  "by": by, "upto": round(float(upto), 3), "when": db.now(), "time": round(time.time(), 3)}
        with store(self.where).write() as c:
            DECISION.insert(c, record)
        return record


def following(decided: dict[str, dict], titles) -> dict[str, dict]:
    """The last decisions (Decisions.latest), each by the id its title has now: one whose id no title has any more goes
    by the title it names (titles.find, on the name and first file the decision keeps), as a person's reports do
    (people.Person.reports), so a title found fine or marked for a better copy stays so after its file moves. Where two
    come to one title, the later counts; one that finds no title keeps its own id. titles: matching.Titles."""
    out: dict[str, dict] = {}
    for title_id, decision in decided.items():  # in the order made: a later one on the same title takes its place
        now = title_id if title_id in titles else titles.find(decision) or title_id
        out[now] = {**decision, "id": now}
    return out


def upto_of(decision: dict | None) -> float:
    """The newest report time a decision covers; -1, before every report, when there's no decision, or when what is
    kept there is no time (the database keeps a NaN as nothing; an infinity would hide every later report). Such a
    decision covers nothing, so its title's reports wait again, to be decided anew."""
    value = decision.get("upto") if decision else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return -1.0
    try:
        value = float(value)
    except OverflowError:  # a whole number past every time, kept as JSON
        return -1.0
    return value if math.isfinite(value) else -1.0


def states(everyones: dict[str, list[dict]], decided: dict[str, dict]) -> dict[str, dict]:
    """Each reported or marked title, by id: {"state": WAITING or BETTER, "reports": the reports since the last decision
    (or all, once marked), each with "by", "decided": that decision or None, "newest": the newest report's time}. A title
    reported and then found fine, with nothing newer, isn't here."""
    by_title: dict[str, list[dict]] = {}
    for name, reports in everyones.items():
        for r in reports:
            if not r.get("gone"):
                by_title.setdefault(r["id"], []).append({**r, "by": name})
    out = {}
    for title_id in set(by_title) | {i for i, d in decided.items() if d["decision"] == "better"}:
        reports = sorted(by_title.get(title_id, []), key=lambda r: r["time"])
        decision = decided.get(title_id)
        upto = upto_of(decision)
        newer = [r for r in reports if r["time"] > upto]
        if newer:
            out[title_id] = {"state": WAITING, "reports": newer, "decided": decision, "newest": newer[-1]["time"]}
        elif decision and decision["decision"] == "better":
            out[title_id] = {"state": BETTER, "reports": reports, "decided": decision, "newest": reports[-1]["time"] if reports else 0}
    return out
