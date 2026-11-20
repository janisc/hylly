"""Comic Vine (comicvine.gamespot.com): the largest comic database. Free API key, non-commercial use only.
Asked only about comics the sources before it (Metron) didn't find.

How it works (2026-10-31, from the API documentation):
- Every request carries api_key=<key> and format=json; the answer is {"status_code", "error", "results"}.
  status_code 1 is OK, 100 a wrong key, 101 not found, 104 a filter error, and 107 the rate limit.
- GET /api/search/?resources=volume&query=<series> finds volumes (a "volume" is a series run): id, name,
  start_year, publisher {name}, count_of_issues.
- GET /api/issues/?filter=volume:<id>,issue_number:<n> finds the issue in a volume.
- GET /api/issue/4000-<id>/ has the details: name, issue_number, cover_date, description (HTML), deck,
  person_credits [{name, role}] (role: "writer", "artist, penciler", ...), volume {name}, site_detail_url.
- Collected editions (trade paperbacks, hardcovers) are volumes of their own, often with the same name as the
  series they collect, and nothing in a volume says which kind it is. The issue's name ("Volume 2", "TPB") or its
  description ("Collects issues #1-5") does: a collected edition is only matched with that evidence, and a
  single issue only without it.
- field_list=a,b,c keeps answers small. Limits: about 200 requests an hour per resource, and a block for
  bursts ("velocity detection"), so this plugin keeps to one request every 3 seconds and 190 an hour.
"""

from __future__ import annotations

import re

from . import (TAG, Changed, Quota, Query, Refused, Session, Source, comic_name, plain_html, similar, texts,
               their_words, whole)

API = "https://comicvine.gamespot.com/api/"
COLLECTED = re.compile(r"\bcollect(s|ed|ing)\b|\btrade paperback\b|\btpb\b|\bhardcover\b|\bomnibus\b|\bgraphic novel\b"
                       r"|^\s*vol(ume)?\.?\s*\d|\bvol(ume)?\.?\s*\d+\s*[:\-]", re.I)
WRITERS = ("writer", "script", "plot", "story")
ARTISTS = ("artist", "penciler", "penciller")
NUMBER = re.compile(r"\d{1,6}(\.\d+)?")  # an issue number that can be compared: "31", "3.5"; not "1A", "Annual 1" or "1/2"


class ComicVine(Source):
    id = "comicvine"  # its name, its key and its budget: sites.LOOKUPS
    kinds = frozenset({"comic"})
    interval = 3.0
    home = "https://comicvine.gamespot.com"
    terms = "Non-commercial use only; shown with a link to Comic Vine."
    verified = "2026-10-31"  # a first live run, which tells collected editions from issues

    def _call(self, s: Session, path: str, params: dict):
        data = s.json(API + path, {"api_key": self.key_value, "format": "json", **params})
        if data is None:
            return None
        if not isinstance(data, dict) or "status_code" not in data:
            raise Changed("Comic Vine answered in a shape this plugin doesn't know.")
        code = data["status_code"]
        if code == 100:
            raise Refused("Comic Vine refused the API key.")
        if code == 107:
            raise Quota("Comic Vine's rate limit is reached; later runs continue.")
        if code == 101:
            return None
        if code != 1:
            raise Changed(f"Comic Vine answered status {their_words(code, 20)} ({their_words(data.get('error', ''))}).")
        return data.get("results")

    def check(self, s: Session) -> str:
        if self._call(s, "publishers/", {"limit": 1, "field_list": "id,name"}) is None:
            raise Changed("Comic Vine's publisher list looks different from what this plugin knows.")
        return "Comic Vine accepted the key"

    def find(self, s: Session, q: Query) -> dict | None:
        name = comic_name(q.title, q.series, q.number)
        if not name.series:
            return None
        volumes = self._call(s, "search/", {"resources": "volume", "query": name.series, "limit": 10,
                                            "field_list": "id,name,start_year,publisher,count_of_issues"}) or []
        found = [v for v in volumes if isinstance(v, dict) and v.get("id")] if isinstance(volumes, list) else None
        if found is None or not all(map(self._readable_volume, found)):
            raise Changed("Comic Vine's search answer looks different from what this plugin knows.")
        ranked = sorted(found, key=lambda v: -self._score(v, name, q))
        best, best_score = None, 0.0
        for v in ranked[:4 if name.collected else 2]:  # a collected edition hides among volumes of the same name
            score = self._score(v, name, q)
            if score < 0.75:
                break
            issues = self._call(s, "issues/", {"filter": f"volume:{whole(v['id'])},issue_number:{name.number or '1'}",
                                                "field_list": "id,issue_number,cover_date"}) or []
            if not self._readable_issues(issues):
                raise Changed("Comic Vine's list of a volume's issues looks different from what this plugin knows.")
            if not issues:
                continue
            detail = self._call(s, f"issue/4000-{whole(issues[0]['id'])}/", {
                "field_list": "name,issue_number,cover_date,description,deck,person_credits,volume,site_detail_url"})
            if not isinstance(detail, dict):
                continue
            if not self._readable_issue(detail):
                raise Changed("A Comic Vine issue's details look different from what this plugin knows.")
            text = " ".join(str(detail.get(k) or "") for k in ("name", "deck", "description"))
            looks_collected = bool(COLLECTED.search(TAG.sub(" ", text)))
            if name.collected is not None and looks_collected != name.collected:
                score -= 0.3  # a single issue where a collected edition was asked for, or the other way round
            if score > best_score:
                best, best_score = (detail, v), score
            if score >= 0.95:
                break
        return self._found(*best, best_score) if best else None

    @staticmethod
    def _readable_volume(v: dict) -> bool:
        """Whether a volume found is as this plugin reads it: its id a whole number, its name and its publisher's as
        text."""
        publisher = v.get("publisher")
        return (whole(v["id"]) is not None and texts(v, "name")
                and (not isinstance(publisher, dict) or texts(publisher, "name")))

    @staticmethod
    def _readable_issues(issues) -> bool:
        """Whether a volume's issues are as this plugin reads them: a list, the first with a whole number for its id."""
        return isinstance(issues, list) and (not issues or isinstance(issues[0], dict)
                                             and whole(issues[0].get("id")) is not None)

    @staticmethod
    def _readable_issue(d: dict) -> bool:
        """Whether an issue's details are as this plugin reads them: text where it reads words, and its credits a list
        of names, each with its roles as text."""
        credits = d.get("person_credits") or []
        return (texts(d, "name", "deck", "description", "site_detail_url") and isinstance(credits, list)
                and all(isinstance(p, dict) and texts(p, "name", "role") for p in credits))

    @staticmethod
    def _score(v: dict, name, q: Query) -> float:
        score = similar(name.series, v.get("name", ""))
        publisher = (v.get("publisher") or {}).get("name", "") if isinstance(v.get("publisher"), dict) else ""
        if q.publisher and publisher:
            score += 0.05 if similar(q.publisher, publisher) >= 0.6 else -0.05
        if name.year and str(v.get("start_year", "")) == name.year:
            score += 0.05
        count = str(v.get("count_of_issues", ""))
        if re.fullmatch(r"\d{1,6}", count) and NUMBER.fullmatch(name.number) and int(count) < int(float(name.number)):
            score -= 0.3  # the volume is too short to have this number
        return min(score, 1.0)

    @staticmethod
    def _found(d: dict, v: dict, score: float) -> dict:
        writers, artists = [], []
        for p in d.get("person_credits") or []:
            roles = (p.get("role") or "").lower()
            who = (p.get("name") or "").strip()
            if who and any(r in roles for r in WRITERS):
                writers.append(who)
            elif who and any(r in roles for r in ARTISTS):
                artists.append(who)
        return {
            "authors": list(dict.fromkeys(writers + artists))[:8],
            "description": plain_html(d.get("description") or d.get("deck") or ""),
            "series": v.get("name", ""), "number": str(d.get("issue_number") or ""),
            "year": str(d.get("cover_date") or "")[:4],
            "publisher": (v.get("publisher") or {}).get("name", "") if isinstance(v.get("publisher"), dict) else "",
            "url": d.get("site_detail_url") or "", "score": round(score, 2),
            "match": f"{v.get('name', '')} ({v.get('start_year', '')}) #{d.get('issue_number', '')}",
        }
