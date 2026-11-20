"""Metron (metron.cloud): a community comic database, open data (CC BY-SA 4.0), good on US publishers.

How it works (2026-10-31, from Metron's own source and its Python client, mokkari):
- Login: an API token from the account page, sent as "Authorization: Bearer <token>".
- GET /api/issue/?series_name=<words>&number=<n> lists matching issues (every word of series_name must be in the
  series name, accents ignored), paged as {"count", "next", "results"}. Each result: id, number, issue (its
  display name), cover_date, image and series {id, name, volume, year_began}.
- GET /api/issue/<id>/ has the details: publisher {name}, series {name, series_type {name}, genres [{name}],
  year_began}, title (a collected edition's own title), desc, isbn, cover_date, credits
  [{creator, role [{name}]}] and resource_url.
- Collected editions are series of their own, with series_type "Trade Paperback", "Hardcover", "Graphic Novel"
  or "Omnibus"; their issue 1 is "Vol. 1".
- Limits: 20 requests a minute; a daily budget (5,000, more for donors) that the X-RateLimit-Sustained-*
  headers report. A 429 always comes with Retry-After.
"""

from __future__ import annotations

from . import Changed, Query, Session, Source, comic_name, norm, similar, whole

API = "https://metron.cloud/api/"
COLLECTED_TYPES = {"trade paperback", "hardcover", "graphic novel", "omnibus"}
WRITERS = {"writer", "script", "story", "plotter", "scripter"}
ARTISTS = {"artist", "penciller", "illustrator", "pencils"}


class Metron(Source):
    id = "metron"  # its name, its token and its budget: sites.LOOKUPS
    kinds = frozenset({"comic"})
    interval = 3.2  # 20 a minute, with room to spare
    home = "https://metron.cloud"
    terms = "Data CC BY-SA 4.0: shown with a link to Metron."
    verified = "2026-10-31"  # a first live run, with no errors

    def __init__(self, key: str = "") -> None:
        super().__init__(key)
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}

    def check(self, s: Session) -> str:
        data = s.json(f"{API}publisher/", {"name": "Image"})
        if not isinstance(data, dict) or "results" not in data:
            raise Changed("Metron's publisher list looks different from what this plugin knows.")
        return "Metron accepted the token"

    def find(self, s: Session, q: Query) -> dict | None:
        name = comic_name(q.title, q.series, q.number)
        if not name.series:
            return None
        number = name.number or "1"
        found = self._issues(s, name.series, number)
        if not found and ":" in name.series:  # "Glass Harbor: The Long Tide" may be filed under its first part
            found = self._issues(s, name.series.split(":")[0], number)
        if not found:
            return None
        ranked = sorted(found, key=lambda r: -self._first_score(r, name))[:3]
        best, best_score = None, 0.0
        for r in ranked:
            if self._first_score(r, name) < 0.6:
                break
            detail = s.json(f"{API}issue/{whole(r['id'])}/")
            if not isinstance(detail, dict) or "series" not in detail:
                raise Changed("A Metron issue's details look different from what this plugin knows.")
            score = self._score(detail, name, q)
            if score > best_score:
                best, best_score = detail, score
        return self._found(best, best_score) if best else None

    def _issues(self, s: Session, series: str, number: str) -> list[dict]:
        data = s.json(f"{API}issue/", {"series_name": series, "number": number})
        if data is None:
            return []
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise Changed("Metron's issue list looks different from what this plugin knows.")
        found = [r for r in data["results"] if isinstance(r, dict) and isinstance(r.get("series"), dict)]
        if any(whole(r.get("id")) is None for r in found):  # an issue's id goes in the next question's path
            raise Changed("Metron's issue list looks different from what this plugin knows.")
        return found

    @staticmethod
    def _first_score(r: dict, name) -> float:
        score = similar(name.series, r["series"].get("name", ""))
        if name.year and str(r["series"].get("year_began", "")) == name.year:
            score += 0.05
        return score

    @staticmethod
    def _score(d: dict, name, q: Query) -> float:
        series = d.get("series") or {}
        score = similar(name.series, series.get("name", ""))
        kind = norm((series.get("series_type") or {}).get("name", ""))
        if name.collected is not None and (kind in COLLECTED_TYPES) != name.collected:
            score -= 0.3  # a single issue where a collected edition was asked for, or the other way round
        publisher = (d.get("publisher") or {}).get("name", "")
        if q.publisher and publisher:
            score += 0.05 if similar(q.publisher, publisher) >= 0.6 else -0.05  # an imprint and its parent differ
        if name.year and str(series.get("year_began", "")) == name.year:
            score += 0.05
        if name.subtitle and d.get("title") and similar(name.subtitle, d["title"]) >= 0.6:
            score += 0.05
        return min(score, 1.0)

    @staticmethod
    def _found(d: dict, score: float) -> dict:
        writers, artists = [], []
        for c in d.get("credits") or []:
            roles = {norm(r.get("name", "")) for r in c.get("role") or [] if isinstance(r, dict)}
            who = (c.get("creator") or "").strip()
            if who and roles & WRITERS:
                writers.append(who)
            elif who and roles & ARTISTS:
                artists.append(who)
        series = d.get("series") or {}
        return {
            "authors": list(dict.fromkeys(writers + artists))[:8],
            "description": (d.get("desc") or "").strip(),
            "subjects": [g["name"] for g in series.get("genres") or [] if isinstance(g, dict) and g.get("name")],
            "series": series.get("name", ""), "number": str(d.get("number") or ""),
            "year": str(d.get("cover_date") or "")[:4], "publisher": (d.get("publisher") or {}).get("name", ""),
            "isbn": d.get("isbn") or "", "url": d.get("resource_url") or f"https://metron.cloud/issue/{d.get('id')}/",
            "score": round(score, 2), "match": f"{series.get('name', '')} #{d.get('number', '')}",
        }
