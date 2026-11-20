"""Google Books (books.google.com): publishers' descriptions, categories and ISBNs. A free API key.
Asked about books Open Library left without a description, and comics Metron and Comic Vine didn't find
(collected editions are sold as books; single issues aren't there).

How it works (2026-10-31, from the Books API documentation):
- GET https://www.googleapis.com/books/v1/volumes?q=<query>&key=<key> searches. The query is
  "isbn:<isbn>", or "intitle:<words> inauthor:<surname>". The answer is {"totalItems", "items": [{"id",
  "volumeInfo": {title, subtitle, authors, publisher, publishedDate, description, industryIdentifiers
  [{type, identifier}], categories, canonicalVolumeLink, infoLink}}]}; "fields" keeps it small.
- Problems come as {"error": {"code", "message", "errors": [{"reason"}]}}. A spent quota is 403 or 429 with
  reason dailyLimitExceeded, rateLimitExceeded, userRateLimitExceeded or quotaExceeded, or status
  RESOURCE_EXHAUSTED. A wrong key is 400 with API_KEY_INVALID or keyInvalid; an API not enabled is 403
  with accessNotConfigured. Only these reasons say the key is refused (REFUSED): any other 400 or 403 is about
  that one question, and is Changed, with its reasons, so a key that works isn't sent to be fixed.
- The daily quota isn't clearly published (1,000 a day was the old default); this plugin keeps under 900
  (per_day in sites.LOOKUPS).
- A book asked by title alone (no author, no ISBN) takes the match's authors, publisher, year and ISBN only when its
  publisher or its year, which every answer holds, agrees with the title's, or its title (before the colon of
  "match") is the title asked, of three words or more (lookups.borrowed).
"""

from __future__ import annotations

import re

from . import (Changed, Quota, Query, Refused, Session, Source, bare_title, comic_name, names, plain_html, similar,
               texts, their_words)

API = "https://www.googleapis.com/books/v1/volumes"
FIELDS = ("totalItems,items(id,volumeInfo(title,subtitle,authors,publisher,publishedDate,description,"
          "industryIdentifiers,categories,canonicalVolumeLink,infoLink))")
QUOTA = {"dailylimitexceeded", "ratelimitexceeded", "userratelimitexceeded", "quotaexceeded", "resource_exhausted"}
REFUSED = {"keyinvalid", "api_key_invalid", "accessnotconfigured", "forbidden", "service_disabled", "permission_denied"}


class GoogleBooks(Source):
    id = "googlebooks"  # its name, its key and its budget: sites.LOOKUPS
    kinds = frozenset({"book", "tech", "rpg", "comic"})
    interval = 1.0
    home = "https://books.google.com"
    terms = "Google APIs terms: shown with a link to Google Books."
    verified = "2026-10-31"  # a first live run, with no errors

    def _search(self, s: Session, query: str):
        status, data = s.json(API, {"q": query, "key": self.key_value, "maxResults": 5, "printType": "books",
                                    "fields": FIELDS}, own=frozenset({400, 403}))
        if status in (400, 403):  # a spent quota, the key refused, or a question it couldn't take: by its reasons
            said = self._reasons(data)
            reasons = {reason.lower() for reason in said}
            if reasons & QUOTA:
                raise Quota("Google Books: today's quota is spent; later runs continue.")
            if reasons & REFUSED:
                why = their_words(", ".join(x for x in said if x.lower() in REFUSED), 100)
                raise Refused(f"Google Books refused the API key ({why}): see that it was copied whole, that the Books API "
                              "is enabled in its project, and that the key's API restrictions allow the Books API.")
            # anything else says nothing about the key: one title's question it couldn't take, or a new way of saying no
            raise Changed(f"Google Books: HTTP {status} "
                          + (f"({their_words(', '.join(said), 100)})." if said else "with no reason given."))
        if status == 404:
            return []
        if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
            raise Changed("Google Books' answer looks different from what this plugin knows.")
        items = [i for i in data.get("items", []) if isinstance(i, dict) and isinstance(i.get("volumeInfo"), dict)]
        if not all(names(i["volumeInfo"].get(k)) for i in items for k in ("authors", "categories")):
            raise Changed("Google Books' answer has its authors or categories otherwise than as a list of names.")
        if not all(self._readable(i["volumeInfo"]) for i in items):
            raise Changed("Google Books' answer looks different from what this plugin knows.")
        return items

    @staticmethod
    def _readable(v: dict) -> bool:
        """Whether a volume's details are as this plugin reads them: text where it reads words, and its identifiers a
        list of them, each with its type and its number as text."""
        ids = v.get("industryIdentifiers") or []
        return (texts(v, "title", "subtitle", "publisher", "description", "canonicalVolumeLink", "infoLink")
                and isinstance(ids, list) and all(isinstance(i, dict) and texts(i, "type", "identifier") for i in ids))

    @staticmethod
    def _reasons(data) -> list[str]:
        """What a problem's answer gives as its reasons, as Google spells them, each once: those of errors[] and
        details[], and its status. Its message is never used, as it may quote the question. data: its JSON, None for
        an answer that isn't JSON."""
        err = (data.get("error") or {}) if isinstance(data, dict) else {}  # JSON that isn't an object has none
        if not isinstance(err, dict):
            return []
        found = [e.get("reason") for key in ("errors", "details") if isinstance(err.get(key), list)
                 for e in err[key] if isinstance(e, dict)]
        return list(dict.fromkeys(str(x) for x in [*found, err.get("status")] if x))

    def check(self, s: Session) -> str:
        self._search(s, "isbn:9780141439518")  # any well-known book
        return "Google Books accepted the key"

    def find(self, s: Session, q: Query) -> dict | None:
        if q.kind == "comic":
            return self._comic(s, q)
        if q.isbn:
            items = self._search(s, f"isbn:{q.isbn}")
            if items:
                return self._found(items[0]["volumeInfo"], 1.0)
        title = bare_title(q.title)  # "(ebook - psychology) Life After Death: A Study" is "Life After Death"
        if not title:
            return None
        query = f"intitle:{title}" + (f" inauthor:{q.authors[0].split()[-1]}" if q.authors else "")
        best, best_score = None, 0.0
        for item in self._search(s, query):
            v = item["volumeInfo"]
            score = max(similar(title, v.get("title", "")), similar(q.title, f"{v.get('title', '')} {v.get('subtitle', '')}"))
            if q.authors and v.get("authors"):
                score += 0.05 if any(similar(q.authors[0], a) >= 0.6 for a in v["authors"]) else -0.3  # a wrong author outweighs a right title
            if score > best_score:
                best, best_score = v, min(score, 1.0)
        return self._found(best, best_score) if best else None

    def _comic(self, s: Session, q: Query) -> dict | None:
        name = comic_name(q.title, q.series, q.number)
        if not name.series or name.collected is False:
            return None  # single issues aren't sold as books
        query = f"intitle:{name.series}" + (f" {name.number}" if name.number else "")
        best, best_score = None, 0.0
        for item in self._search(s, query):
            v = item["volumeInfo"]
            full = f"{v.get('title', '')} {v.get('subtitle', '')}"
            title = re.sub(r"\s+", " ", v.get("title", ""))  # a run of spaces as one, before a pattern with \s* in it
            score = similar(name.series, re.sub(r"[,:]?\s*(vol(ume)?\.?|book)\s*\d+.*$", "", title, flags=re.I))
            if name.number and not re.search(rf"\b(vol(ume)?\.?|book|#)?\s*0*{re.escape(name.number)}\b", full, re.I):
                score -= 0.4  # another volume of the series
            if q.publisher and v.get("publisher"):
                score += 0.05 if similar(q.publisher, v["publisher"]) >= 0.6 else 0
            if score > best_score:
                best, best_score = v, min(score, 1.0)
        return self._found(best, best_score) if best else None

    @staticmethod
    def _found(v: dict, score: float) -> dict:
        isbn = next((i.get("identifier", "") for i in v.get("industryIdentifiers") or [] if i.get("type") == "ISBN_13"), "")
        return {
            "authors": (v.get("authors") or [])[:8],  # each a name, as _search has them
            "description": plain_html(v.get("description") or ""),
            "subjects": (v.get("categories") or [])[:8],
            "year": str(v.get("publishedDate") or "")[:4], "publisher": v.get("publisher") or "", "isbn": isbn,
            "url": v.get("canonicalVolumeLink") or v.get("infoLink") or "", "score": round(score, 2),
            "match": ": ".join(x for x in (v.get("title"), v.get("subtitle")) if x),  # its title before a colon
        }
