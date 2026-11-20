"""Open Library (openlibrary.org): descriptions, subjects and authors of books, free and without a login.

How it works (2026-11-03):
- GET /isbn/<isbn>.json gives an edition, whose "works" names its work.
- Without an ISBN, GET /search.json?title=<title>&author=<first author>&fields=key,title,subject,author_name,
  publisher,first_publish_year finds works; the first one is used if its title is close enough. Asked by title alone
  (no author), its authors count only when its title ("match") is the title asked, of three words or more, or when
  one of its publishers, or the year it first came out, agrees with the title's (lookups.borrowed): those go in the
  answer as publishers and years. Not every edition's year: a work printed for long has one in almost every year, and
  a PDF's own year is often the year its file was made. The last two fields are the search documents' own, not yet
  seen in a live answer here: without them, a shorter title asked alone takes no authors from here.
- GET <work key>.json has the description (text, or {"value": text}) and subjects. A work's key is /works/OL<digits>W;
  one written otherwise is Changed, and isn't asked about.
- No published limit; one request a second is the polite pace.
"""

from __future__ import annotations

import re

from . import Changed, Query, Session, Source, bare_title, names, similar, texts

SITE = "https://openlibrary.org"
WORK = re.compile(r"/works/OL\d+W")  # a work's key, as it goes after SITE: anything else could lead elsewhere


class OpenLibrary(Source):
    id = "openlibrary"  # its name and its budgets: sites.LOOKUPS
    kinds = frozenset({"book", "tech", "rpg"})
    wants = frozenset({"description", "subjects"})
    interval = 1.0
    home = SITE
    terms = "Open data; no login."
    verified = "2026-11-03"

    def find(self, s: Session, q: Query) -> dict | None:
        found: dict = {}
        work, score = None, 0.0
        if q.isbn:
            edition = s.json(f"{SITE}/isbn/{q.isbn}.json")
            if isinstance(edition, dict):
                works = edition.get("works") or []
                if not isinstance(works, list):
                    raise Changed("Open Library's answer about an edition looks different from what this plugin knows.")
                work, score = (works[0].get("key") if works and isinstance(works[0], dict) else None), 1.0
        if not work:
            title = bare_title(q.title)  # "(ebook - psychology) Life After Death: A Study" is "Life After Death"
            if not title:
                return None  # Open Library answers an empty title with HTTP 500
            params = {"title": title, "limit": 1,
                      "fields": "key,title,subject,author_name,publisher,first_publish_year"}
            if q.authors:
                params["author"] = q.authors[0]
            data = s.json(f"{SITE}/search.json", params)
            if data is not None and not isinstance(data, dict):
                raise Changed("Open Library's search answer looks different from what this plugin knows.")
            docs = (data or {}).get("docs") or []
            if not isinstance(docs, list) or docs and isinstance(docs[0], dict) and not readable_doc(docs[0]):
                raise Changed("Open Library's search answer looks different from what this plugin knows.")
            if docs and isinstance(docs[0], dict):
                score = similar(title, docs[0].get("title", ""))
                work = docs[0].get("key")
                found["subjects"] = (docs[0].get("subject") or [])[:15]
                found["authors"] = docs[0].get("author_name") or []
                found["match"] = docs[0].get("title", "")
                if not q.authors:  # asked by title alone: its authors count only with its very title, or a
                    found.update(vouching(docs[0]))  # publisher or year that agrees, for each title asked so (borrowed)
        if not work:
            return None
        if not isinstance(work, str) or not WORK.fullmatch(work):
            raise Changed("Open Library named a work in a way this plugin doesn't know, so it wasn't asked about.")
        w = s.json(f"{SITE}{work}.json")
        if w is not None and not (isinstance(w, dict) and readable_work(w)):
            raise Changed("Open Library's answer about a work looks different from what this plugin knows.")
        if isinstance(w, dict):
            d = w.get("description")
            found["description"] = ((d.get("value") if isinstance(d, dict) else d) or "").strip()
            found.setdefault("subjects", (w.get("subjects") or [])[:15])
        found = {k: v for k, v in found.items() if v}
        found.update(url=f"{SITE}{work}", score=round(score, 2))
        return found


def readable_doc(doc: dict) -> bool:
    """Whether a work the search found is as this plugin reads it: its title text, its authors and subjects names."""
    return texts(doc, "title") and names(doc.get("subject")) and names(doc.get("author_name"))


def readable_work(w: dict) -> bool:
    """Whether a work's own answer is as this plugin reads it: its description text, or {"value": text}, and its
    subjects names."""
    d = w.get("description")
    return (texts(d, "value") if isinstance(d, dict) else texts(w, "description")) and names(w.get("subjects"))


PUBLISHERS = 50  # of a work's publishers kept in an answer, at most: a work in print for long has hundreds


def vouching(doc: dict) -> dict:
    """What can vouch for the authors of a work found by the title alone: its publishers and the year it first came
    out, as publishers and years in the answer. They only vouch (lookups.borrowed), and fill nothing in the catalog.
    Every title asked the same way shares the answer, so all the publishers are kept, not the one that agrees with
    the title asked. Not every edition's year (publish_year): a work printed for long has one in almost every year."""
    publishers = doc.get("publisher") if isinstance(doc.get("publisher"), list) else []
    named = [p.strip() for p in publishers if isinstance(p, str) and p.strip()]
    first = doc.get("first_publish_year")
    return {"publishers": list(dict.fromkeys(named))[:PUBLISHERS],
            "years": [str(first)] if isinstance(first, int) and not isinstance(first, bool) else []}
