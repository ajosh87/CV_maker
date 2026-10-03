"""Keep a personal-data app reachable only by its owner, from their own browser.

The app runs on the user's laptop with no accounts, so the OS login is the trust boundary. What
remains are the standard attacks on any localhost web app:

* Network access: only loopback connections are served (so `--host 0.0.0.0` does not expose it).
* DNS rebinding: a hostile site that re-points its domain at 127.0.0.1 is refused because the Host
  header must be a localhost name.
* Cross-site request forgery: a page on another site (or another localhost port) cannot make the
  browser change settings, upload, or trigger work here. Browsers label every request with
  Sec-Fetch-Site / Origin; state-changing requests that come from anywhere but this app are refused.
* Clickjacking and caching: pages cannot be framed, and personal pages are not cached.

Escape hatches for unusual setups (e.g. a container): CV_TAILOR_ALLOWED_HOSTS=host1,host2 and
CV_TAILOR_ALLOW_REMOTE=1. Both are off by default.
"""
import ipaddress
import os
from urllib.parse import urlparse

from flask import Flask, render_template, request

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}


def _hostname(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):  # [::1]:5000
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _is_loopback(addr: str | None) -> bool:
    try:
        ip = ipaddress.ip_address((addr or "").split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _allowed_hosts() -> set[str]:
    extra = {h.strip().lower() for h in os.environ.get("CV_TAILOR_ALLOWED_HOSTS", "").split(",") if h.strip()}
    return _LOCAL_NAMES | extra


def blocked_reason() -> str | None:
    if os.environ.get("CV_TAILOR_ALLOW_REMOTE") != "1" and not _is_loopback(request.remote_addr):
        return "This app only accepts connections from this computer."
    name = _hostname(request.host)
    if name not in _allowed_hosts() and not name.endswith(".localhost"):
        return "This app only answers on localhost. Open it at http://127.0.0.1 or http://localhost."
    if request.method in _SAFE_METHODS:
        return None
    site = request.headers.get("Sec-Fetch-Site")
    if site is not None:
        return None if site in ("same-origin", "none") else "Blocked a request that came from another website."
    origin = request.headers.get("Origin")
    if origin is not None:
        if origin == "null" or urlparse(origin).netloc.lower() != request.host.lower():
            return "Blocked a request that came from another website."
    return None  # no browser fetch metadata: a same-origin form in an older browser, or a non-browser client


def install_local_only_guard(app: Flask) -> None:
    @app.before_request
    def _guard():
        reason = blocked_reason()
        if reason:
            app.logger.warning("Blocked %s %s from %s (host=%s): %s", request.method, request.path,
                               request.remote_addr, request.host, reason)
            return render_template("message.html", title="Request blocked", message=reason), 403
        return None

    @app.after_request
    def _headers(response):
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if not request.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response
