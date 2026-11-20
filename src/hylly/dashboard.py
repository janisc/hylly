"""The server page, /server: how the running library server is doing, with Restart and Stop, and whether it starts at
logon.

It answers only the library's owners, logged in from anywhere, and its buttons need the key the page was
served with, so another website open in the browser cannot press them. Everything it does is also a
command: hylly server status, restart, stop. The start at logon it only shows: a web page shouldn't write the
system's start-up entries, so the page says which command does (hylly server autostart on|off).
"""

from __future__ import annotations

import contextlib
import ipaddress
import threading
import time
from pathlib import Path

import psutil

from . import access, bell, instance, paths, reader, settings, updates
from .words import fill

HERE = Path(__file__).parent
CLIENTS_KEPT = 20


def _code_stamp() -> float:
    return max(p.stat().st_mtime for p in [*HERE.glob("*.py"), *HERE.glob("*.html")])


STARTED_CODE = _code_stamp()  # the program as it was when this server started
PROC = psutil.Process()  # kept, so cpu_percent() measures since the page last asked


class Stats:
    """Requests since the start, and the devices they came from (the latest few)."""

    def __init__(self) -> None:
        self.requests = 0
        self.clients: dict[str, dict] = {}
        self._lock = threading.Lock()

    def note(self, ip: str, user_agent: str) -> None:
        with self._lock:
            self.requests += 1
            seen = self.clients.setdefault(ip, {"n": 0})
            seen.update(n=seen["n"] + 1, last=time.time(), app=app_name(user_agent))
            if len(self.clients) > CLIENTS_KEPT:
                del self.clients[min(self.clients, key=lambda k: self.clients[k]["last"])]


def app_name(user_agent: str) -> str:
    """'Yomu/2.1 CFNetwork/...' -> 'Yomu 2.1'; browsers by name."""
    ua = user_agent or ""
    if ua.startswith("Mozilla/"):
        for token, name in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari")):
            if token in ua:
                return f"{name} (browser)"
        return "a browser"
    first = ua.split(" ")[0] if ua else ""
    return first.replace("/", " ", 1) or "unknown"


_logon = {"at": 0.0, "value": None}


def logon_start() -> bool | None:
    """instance.autostart_state(), asked at most once a minute (it runs schtasks)."""
    if time.monotonic() - _logon["at"] > 60:
        _logon.update(at=time.monotonic(), value=instance.autostart_state())
    return _logon["value"]


def _servers_own(ip: str) -> bool:
    """Whether a device's address is the server's own computer's: a loopback address, or its own at home."""
    try:
        return ipaddress.ip_address(ip).is_loopback or ip == access.lan_address()
    except ValueError:
        return False


def status(handler, roles: dict[str, str]) -> dict:
    """What the page shows, from the running server's handler. The addresses: on this computer, which the page shows
    only to a browser on it (here: the request came from the server's own computer, as the server tells its sender,
    handler.sender, the real one behind a trusted proxy); for other devices (the address people use, [server] address,
    else this computer's own when it listens beyond itself), a browser's first, then the reading apps' feeds; None for
    those while it listens on this computer only. With address set, the plain one at home as well, for a device that
    can't use address (home_too), or None. The addresses are access's, beside who is inside: the server that asks for
    this page imports it, and so it imports nothing of the server's. The devices: the one looking (you), and the
    server's own computer (server). roles: each user's role by name, as the request read the accounts (serve.Caller),
    which it doesn't read again."""
    info, port, listen = handler.info, handler.server.server_port, str(handler.server.server_address[0])
    typed = [f"--{name}" for name in ("port", "lan", "library", "rules") if name in (info.get("given") or {})]
    restarted = None
    if typed:  # where a Restart brings it back: the settings are a server's only options in the background
        with contextlib.suppress(SystemExit):  # a mistake in the settings, which the restart would say
            saved = settings.get("server")  # what serve.options gives a server in the background: the settings alone
            restarted = f"{access.here(saved['listen'], int(saved['port']))}/"
    others, home = access.other_devices(listen, port, handler.address), access.home_too(listen, port, handler.address)
    proc, cores = PROC, psutil.cpu_count() or 1
    uptime = max(time.time() - proc.create_time(), 1)
    viewer = handler.sender  # who is looking, as the access log and the devices list name it
    with handler.stats._lock:
        clients = sorted(({"ip": ip, **c, "you": ip == viewer, "server": _servers_own(ip)}
                          for ip, c in handler.stats.clients.items()), key=lambda c: -c["last"])
        requests = handler.stats.requests
    try:
        items, library_gone = handler.catalog.get(), False
    except OSError:  # the share dropped out: the server is fine, and this page is where you would look
        items, library_gone = [], True
    return {
        "library_gone": library_gone, "pid": proc.pid, "started": info.get("started", ""), "uptime": uptime, "background": bool(info.get("background")),
        "pc": f"{access.here(listen, port)}/", "here": _servers_own(viewer), "listen": listen, "browser": f"{others}/" if others else None,
        "opds": f"{others}/opds" if others else None, "ereader": f"{others}/ereader/opds" if others else None,
        "at_home": f"{home}/" if home else None,
        "ereader_titles": sum(1 for it in items if handler.ereader.suits(it)),
        "logon": logon_start(), "service": instance.in_system_service(), "system": instance.SYSTEM, "library": str(handler.root),
        "typed": typed, "restarted": restarted,  # what it was started with for that run alone, and the address after a Restart
        "settings": str(paths.settings_file()), "titles": len(items), "generated": handler.catalog.generated,
        "memory_mb": proc.memory_info().rss / 2**20, "ram_gb": psutil.virtual_memory().total / 2**30,
        "cpu_now": proc.cpu_percent(None) / cores, "cpu_avg": 100 * sum(proc.cpu_times()[:2]) / uptime / cores,
        "pages": reader.status(),
        "requests": requests, "clients": clients,
        "log": access.tail(instance.log_file(), 15),  # however it runs: it writes them there itself
        "users": [f"{name} ({role})" for name, role in sorted(roles.items())],
        "bell_log": access.tail(bell.log_file(), 10), "bell_log_file": str(bell.log_file()),
        "bell_to": bell.channels(handler.watch.conf),
        "access_log": access.tail(access.access_log_file(), 12) if handler.access_log.enabled else None,
        "access_log_file": str(access.access_log_file()),
        "log_file": str(instance.log_file()), "updated": _code_stamp() > STARTED_CODE, "behind": list(updates.behind_once()),
    }


def page(name: str, key: str, **values: str) -> str:
    """One of the owners' own pages beside this file: the Server page (server_page.html) or the Database page
    (database_page.html, dbpage.py), with the key their questions and buttons carry, and the values each shows, every
    one in double braces, escaped. Only those named are filled: whatever else is in braces stays as it is."""
    return fill(HERE.joinpath(name).read_text(encoding="utf-8"), {"key": key, **values})


def which_server(info: dict | None) -> dict[str, str]:
    """For the Server page: the server that wrote it, its process and when it started (info: the server's note) as
    status says them, so that a server started again before the page's first status is seen to be another one."""
    return {"pid": str(PROC.pid), "started": str((info or {}).get("started", ""))}


def restart_later() -> None:
    """Restart from the page: a separate, detached `hylly server restart` stops this server and starts
    the new one, so nothing depends on this process surviving its own end. A server the system started, the system
    restarts: a moment later, so that the page gets its answer first."""
    if instance.system_owns(PROC.pid):
        threading.Timer(0.5, instance.restart_through_system).start()
    else:
        instance.spawn(instance.RESTART, errors_to_log=True)  # the servers log their own stop and start
