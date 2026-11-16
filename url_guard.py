"""
url_guard.py — keep backend-to-store requests off the backend's own network.

WHY
  A WooCommerce store's address (wp_base_url) comes from the plugin at
  activation, and /activate-free is unauthenticated. Without a check, anyone
  could register http://127.0.0.1:6379 or http://169.254.169.254 as their
  "store" and the backend would send requests there: once for the site-key
  check, then on every catalogue build and refresh (blind SSRF).

WHAT IS ALLOWED
  - scheme http or https, default port only (80 / 443)
  - no user:password@ in the URL
  - every address the host name resolves to must be a public (global)
    address: loopback, private, link-local (cloud metadata), CGNAT, multicast,
    reserved and unspecified addresses are refused, including IPv4 written as
    a number or embedded in IPv6

TWO PLACES IT IS USED
  check_store_url()  — at activation, to refuse an unsafe wp_base_url before
                       anything is fetched or stored. A host that does not
                       resolve is NOT refused here (dev uses https://shop.test,
                       and an unreachable site is accepted at activation).
  GuardedSession     — a requests.Session that re-checks EVERY request it
                       sends, including each redirect hop. A public site that
                       answers 302 Location: http://127.0.0.1/ is stopped at
                       the hop, and a row stored before this check existed is
                       stopped at the first request.

  Known limit: the address is resolved here and again by urllib3 when it
  connects, so a DNS server answering differently a few milliseconds apart
  (DNS rebinding) is not covered. Pinning the checked IP per connection would
  close that; the egress firewall is the backstop.

LOCAL DEVELOPMENT
  MIRAQ_ALLOW_PRIVATE_STORE_URLS=1 skips the address check (scheme, port and
  credential rules still apply) so a WordPress on localhost or the LAN can be
  used. Never set it in production.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

import requests

from chat_logger import get_logger

logger = get_logger("miraq_chat")

ALLOW_PRIVATE_STORE_URLS = os.getenv("MIRAQ_ALLOW_PRIVATE_STORE_URLS", "").strip().lower() in (
    "1", "true", "yes", "on",
)
if ALLOW_PRIVATE_STORE_URLS:
    logger.warning(
        "⚠️  MIRAQ_ALLOW_PRIVATE_STORE_URLS is set — store URLs on loopback/private "
        "addresses are allowed. Development only."
    )

_ALLOWED_SCHEMES = {"http", "https"}
_DEFAULT_PORTS = {"http": 80, "https": 443}

# Resolution results, so a catalogue build making hundreds of requests to one
# store does not do hundreds of lookups. Short, so a DNS change is seen soon.
_CACHE_TTL_S = 60
_cache: Dict[Tuple[str, int], Tuple[float, Optional[str]]] = {}
_cache_lock = threading.Lock()


class UnsafeURLError(requests.exceptions.InvalidURL):
    """Raised by GuardedSession for a request it refuses to send. A
    RequestException, so existing `except requests.RequestException` /
    `except Exception` handlers treat it like any failed request."""


def _blocked_address(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        # ::ffff:127.0.0.1 and friends — judge the embedded IPv4 address.
        if ip.ipv4_mapped is not None:
            return _blocked_address(ip.ipv4_mapped)
        if ip.sixtofour is not None and _blocked_address(ip.sixtofour):
            return True
    return (
        not ip.is_global
        or ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_and_check(host: str, port: int) -> Optional[str]:
    """None when every address of host is public; otherwise the reason.
    'unresolvable' when the name does not resolve."""
    key = (host, port)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        reason: Optional[str] = "unresolvable"
    else:
        reason = None
        for info in infos:
            addr = info[4][0].split("%", 1)[0]  # drop an IPv6 zone id
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                reason = f"unparseable address {addr!r}"
                break
            if _blocked_address(ip):
                reason = f"resolves to non-public address {ip}"
                break
        if reason is None and not infos:
            reason = "unresolvable"

    with _cache_lock:
        _cache[key] = (now + _CACHE_TTL_S, reason)
    return reason


def check_store_url(url: str, *, require_resolvable: bool = False) -> Optional[str]:
    """None when url is safe for the backend to request; otherwise a short
    reason. With require_resolvable=False a host that does not resolve is
    accepted (activation); GuardedSession passes True."""
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port  # raises ValueError on a non-numeric / out-of-range port
    except ValueError:
        return "malformed URL"

    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return "scheme must be http or https"
    if parts.username is not None or parts.password is not None:
        return "credentials in URL are not allowed"
    host = (parts.hostname or "").rstrip(".")
    if not host:
        return "missing host"
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        return f"port {port} is not allowed (only {_DEFAULT_PORTS[scheme]} for {scheme})"

    if ALLOW_PRIVATE_STORE_URLS:
        return None

    reason = _resolve_and_check(host.lower(), port or _DEFAULT_PORTS[scheme])
    if reason == "unresolvable" and not require_resolvable:
        return None
    return reason


def _short(url: str) -> str:
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.hostname}{p.path}"[:200]
    except ValueError:
        return "<malformed>"


class GuardedSession(requests.Session):
    """requests.Session that refuses to send to a non-public address.

    Session.send is called for the first request and again for every redirect
    hop (resolve_redirects), so checking here covers both."""

    def send(self, request, **kwargs):
        reason = check_store_url(request.url, require_resolvable=True)
        if reason:
            logger.warning(f"url_guard: blocked outbound request | {_short(request.url)} | {reason}")
            raise UnsafeURLError(f"blocked outbound request to {_short(request.url)}: {reason}")
        return super().send(request, **kwargs)
