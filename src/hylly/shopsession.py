"""The web session every shop's client asks its shop through (ShopSession): requests at the shop's pace however many
threads ask, network trouble and a busy shop asked again, the shop's Retry-After waited out by every request to it, and
errors that say in words what went wrong, never the address, as order keys travel in addresses and these messages get
pasted into public places.

Each shop keeps its own addresses, its reading of the answers and its words. What a shop does differently from the
others is said where it makes its session, as the session's options: which answers are asked again (`again`), a new
login on a 401 (`relogin`), the answers that say the login wasn't taken (`refused_when`), and a cookie jar for a shop
that rewrites its login cookie (`cookies`). A request of its own may leave an answer's body unread (`read`, of
request and send: for a file's link, which is in the redirect).

The lookup sources have a session of their own (lookups.Session), with budgets kept on disk; both read Retry-After with
logins.retry_after."""

from __future__ import annotations

import threading
import time
from typing import Callable

import httpx

from . import USER_AGENT
from .logins import LONGEST_WAIT, AuthError, ShopError, retry_after

TRIES = 5  # of one request, the first one too


def busy(status: int) -> bool:
    """The shop busy or in trouble (429, 5xx): worth asking again in a moment."""
    return status == 429 or status >= 500


class ShopSession:
    """HTTP for one shop's site or API, never followed on to a redirect: the shop's answer is read as it is.

    pace: the seconds from one request going out to the next, however many threads ask (downloads ask for fresh links
    side by side), and from the last answer that came before the next one goes: an answer still on its way then can't
    count. backoff: the first wait after trouble, doubled after each. A wait after trouble, or the shop's Retry-After,
    is every request's to that shop: no request goes out before it's over, those already waiting their turn too. Each
    such wait a request sits out counts as one of its tries, as if it had been told to wait itself, so that a shop busy
    for everyone is given up on after about as many waits as one request would sit out, however many downloads ask.
    Waits told at the same time overlap. On a request's last try nothing more is waited for: its Retry-After holds no
    one, and the others go on at the pace. tries: of each request, the first one too; a login's check asks once.
    unanswered: of those, the tries when nothing answers at all (1: a login's check says so at once). sleep: for
    tests; by default time.sleep as it is when the wait comes."""

    def __init__(self, name: str, base_url: str, *, pace: float = 1.0, tries: int = TRIES, backoff: float = 2.0,
                 headers: dict[str, str] | None = None, cookies: httpx.Cookies | dict[str, str] | None = None,
                 again: Callable[[int], bool] = busy, relogin: Callable[[], None] | None = None,
                 refused_when: Callable[[httpx.Response], bool] | None = None, refused: str = "",
                 transport: httpx.BaseTransport | None = None, timeout: float = 60.0, unanswered: int | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        self.name = name
        self.http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport, follow_redirects=False,
                                 cookies=cookies, headers={"User-Agent": USER_AGENT, **(headers or {})})
        self.pace, self.tries, self.backoff = pace, max(1, tries), backoff
        self.unanswered = self.tries if unanswered is None else max(1, unanswered)
        self.again, self.relogin, self.refused_when, self.refused = again, relogin, refused_when, refused
        self._sleep = sleep
        self._turn = threading.Lock()  # held while a request waits for its turn
        self._sent = 0.0  # when the last request went out (time.monotonic)
        self._answered = 0.0  # when the last answer came: written without the lock, so that no answer waits for it
        self._paused = threading.Lock()  # for the wait's own fields alone: held a moment, never through a wait
        self._until = 0.0  # no request goes out before this: a wait after trouble, the shop's or ours
        self._waits = 0  # the waits begun so far, each one that moved _until on
        self._by = 0  # the thread whose trouble began the wait now in _until
        self._trouble: httpx.Response | str = ""  # what began it: the shop's answer, or the network error's kind

    def __enter__(self) -> ShopSession:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.http.close()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            (self._sleep or time.sleep)(seconds)

    def _wait_turn(self) -> int:
        """One request's turn, at the pace and after any wait: an answer or a wait that came while it slept is waited
        for too, so a request already waiting its turn when the shop asks to wait doesn't go out meanwhile. Returns how
        many waits another request's trouble began that it sat out: those begun while it waited, and one going on when
        it came."""
        me, waits = threading.get_ident(), self._waits
        going_on = self._until > time.monotonic() and self._by != me
        with self._turn:
            while True:
                seen = self._answered, self._until
                self.sleep(max(max(self._sent, seen[0]) + self.pace, seen[1]) - time.monotonic())
                if (self._answered, self._until) == seen:
                    break
            self._sent = time.monotonic()
        return self._waits - waits + going_on

    def _pause(self, seconds: float, trouble: httpx.Response | str) -> None:
        """A wait after trouble, from now: every request to the shop waits it out. Two at once overlap; neither shortens
        the other. trouble: the shop's answer that began it, or the network error's kind."""
        with self._paused:
            until = time.monotonic() + seconds
            if seconds > 0 and until > self._until:
                self._until, self._waits, self._by, self._trouble = until, self._waits + 1, threading.get_ident(), trouble

    def _no_answer(self, kind: str, tries: int) -> ShopError:
        after = f" after {tries} tries" if tries > 1 else ""
        return ShopError(f"No answer from {self.name}{after} ({kind}).", no_answer=True)

    def _send(self, method: str, path: str, params, data, headers, read, body: dict | None = None) -> httpx.Response:
        body = body or {}  # content= or files=, as httpx takes them: a body of the request's own (Humble's actions)
        try:
            if read is None:
                return self.http.request(method, path, params=params, data=data, headers=headers, **body)
            with self.http.stream(method, path, params=params, data=data, headers=headers, **body) as r:
                if read(r):
                    r.read()
            return r
        finally:
            self._answered = time.monotonic()

    def send(self, method: str, path: str, *, params=None, data=None, headers: dict[str, str] | None = None,
             read: Callable[[httpx.Response], bool] | None = None) -> httpx.Response:
        """One request in its turn, asked once: the network's trouble comes through as httpx raises it. read: whether
        to read an answer's body, told its status and headers; one not read never comes, and only its status and
        headers can be looked at (a file's link is in the redirect: the file itself isn't fetched)."""
        self._wait_turn()
        return self._send(method, path, params, data, headers, read)

    def request(self, method: str, path: str, *, params=None, data=None, headers: dict[str, str] | None = None,
                read: Callable[[httpx.Response], bool] | None = None, login_again: bool = True, content: bytes | None = None,
                files: list | None = None) -> httpx.Response:
        """The shop's answer. Network trouble and the answers `again` names (429 and 5xx unless told otherwise) are
        asked again, `tries` times in all, after a wait that doubles each time, or the shop's own Retry-After when
        that's longer; a wait it sat out for another request counts as one of its tries too, and when those are spent
        that way, it ends with the answer that began the last of them (or that answer's no answer), as the shop is as
        busy for this one. A Retry-After past LONGEST_WAIT isn't waited out: that answer comes back as it is, for the
        shop to say it's busy. A 401 logs in again once (relogin), unless this is the login itself (login_again False)
        or its last try.
        An answer that shows the login wasn't taken (refused_when) is an AuthError with the words `refused`; no answer
        at all, after every try, a ShopError with no_answer. Any other answer comes back as it is: each shop reads its
        own. content and files: a body other than data's form, as httpx takes them (Humble's actions: a JSON text, or a
        multipart form)."""
        body = {k: v for k, v in (("content", content), ("files", files)) if v is not None}
        relogged, attempt = False, 0
        while True:
            attempt += self._wait_turn()
            if attempt >= self.tries:  # spent sitting out the waits other requests' trouble began
                trouble = self._trouble
                if isinstance(trouble, str):
                    raise self._no_answer(trouble, self.tries)
                return trouble
            last = attempt == self.tries - 1
            try:
                r = self._send(method, path, params, data, headers, read, body)
            except httpx.TransportError as e:  # by its kind alone: its message may have the address
                if last or attempt + 1 >= self.unanswered:
                    raise self._no_answer(type(e).__name__, attempt + 1) from None
                self._pause(self.backoff * 2 ** attempt, type(e).__name__)
                attempt += 1
                continue
            if r.status_code == 401 and self.relogin and login_again and not relogged and not last:  # last: refused
                self.relogin()
                relogged, attempt = True, attempt + 1
                continue
            if self.again(r.status_code) and not last:
                wait = retry_after(r) or 0.0
                if wait > LONGEST_WAIT:
                    break
                self._pause(max(self.backoff * 2 ** attempt, wait), r)
                attempt += 1
                continue
            break
        if self.refused_when and self.refused_when(r):
            raise AuthError(self.refused)
        return r
