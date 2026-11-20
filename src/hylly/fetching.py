"""What Hylly fetches from an address a shop or a lookup source gave it, or sent it on to: only over https, and never
from this computer, the home network or the tailnet. A shop or a source that is hostile or broken could otherwise have
Hylly fetch a device at home, or a page over plain http, and file the answer as a book or a description.

The clients that fetch such addresses (download.Downloader's, lookups.Session's) take these as their httpx event hooks
(hooks): httpx calls the request hook before each request it sends, the first one and each redirect it follows, and
the response hook as each answer's headers come, before anything of it is read. The request hook refuses an address
that isn't https, and one whose name resolves, here and now, to an address that isn't public (public). httpx lets no
check of ours into its connection itself, so the response hook looks at the address the connection really reached,
and refuses an answer from one that isn't public. What that leaves: a name whose answer changes between the two looks
(DNS rebinding) can have one request go out to the home network, without a login, and nothing of its answer is taken.
Through a proxy, the proxy connects, and only the address's name is looked at. A name that can't be resolved here, or
not within the request's own time to connect, is left to the request, which then fails as it would have, or to the
proxy; so is a connection gone before the address it reached could be read, as network trouble, asked again.

So a resolver that answers names on the internet with addresses that aren't public (a proxy in fake-IP mode, a cache at
home that rewrites names, a cloud's private-endpoint names) stops the downloads and lookups: the address a name leads
to is all there is to go by.

No address is ever said: an order key or a source's key may travel in it.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import urllib.request

import httpx


class Refused(Exception):
    """An address that isn't fetched, in words that say why, never the address."""


NOT_HTTPS = "an address that isn't https"
NOT_PUBLIC = "an address on this computer, the home network or the tailnet"


def public(address: str) -> bool:
    """Whether an IP address is one on the internet: not this computer's, a home network's, a tailnet's, a link-local,
    a multicast or a reserved one. An IPv4 address written as IPv6 (::ffff:192.168.1.5) goes by its IPv4 one."""
    found = ipaddress.ip_address(address.partition("%")[0])
    found = getattr(found, "ipv4_mapped", None) or found
    return found.is_global and not found.is_multicast


def resolve(host: str, port: int) -> list[str]:
    """The addresses a name resolves to here, as the connection would resolve it; [] when it can't be resolved."""
    try:
        return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
    except (OSError, UnicodeError):
        return []


def resolved(host: str, port: int, within: float | None) -> list[str]:
    """resolve's answer, waited for at most within seconds (the request's own time to connect), as the system's resolver
    keeps no time of httpx's: a resolver that doesn't answer would otherwise make every request wait twice, here and as
    it connects. Past that, [], as for a name that can't be resolved, and the lookup goes on in its thread until the
    system gives up. None: waited for as long as it takes."""
    if within is None:
        return resolve(host, port)
    found: list[list[str]] = []
    asking = threading.Thread(target=lambda: found.append(resolve(host, port)), name="resolve", daemon=True)
    asking.start()
    asking.join(within)
    return found[0] if found else []


def check(request) -> None:
    """The request hook: Refused for an address that isn't https, or whose name resolves to one that isn't public."""
    url = request.url
    if url.scheme != "https":
        raise Refused(NOT_HTTPS)
    host = url.raw_host.decode("ascii")  # as it goes out: a name in another alphabet in its xn-- form
    try:
        addresses = [str(ipaddress.ip_address(host))]  # written as an address: no name to resolve
    except ValueError:  # a name, or an address in another form (2130706433), which the system resolves as it connects
        addresses = resolved(host, url.port or 443, (request.extensions.get("timeout") or {}).get("connect"))
    if not all(public(address) for address in addresses):
        raise Refused(NOT_PUBLIC)


def check_answer(response) -> None:
    """The response hook: Refused for an answer from an address that isn't public, as the connection reached it. An
    answer that tells none (a test's made-up one) passes. A connection gone before its address could be read (reset,
    or closed) is network trouble, httpx's own ReadError, which is asked again as any other is: nothing of the answer
    could come over it anyway, and refusing would fail a file, or count against a source, for good."""
    stream = response.extensions.get("network_stream")
    try:
        reached = stream.get_extra_info("server_addr") if stream is not None else None
    except OSError as e:  # never the address: its kind alone
        raise httpx.ReadError(f"the connection ended before its address could be checked ({type(e).__name__})",
                              request=response.request) from None
    if reached and not public(str(reached[0])):
        raise Refused(NOT_PUBLIC)


def hooks() -> dict[str, list]:
    """The event hooks for a client that fetches what shops and sources give (httpx.Client(event_hooks=...)). Through a
    proxy for https, which httpx takes from the environment and the system as urllib reads them, the connection is
    the proxy's: then only the request hook."""
    proxies = urllib.request.getproxies()
    return {"request": [check], "response": [] if proxies.get("https") or proxies.get("all") else [check_answer]}
