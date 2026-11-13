"""Outbound HTTP for the dashboard's own calls (AI providers, Discord, weather).

Redirects are never followed: every address we call is either fixed or one the owner typed, so a
redirect would only ever send a request (and sometimes an API key header) somewhere unintended.
"""
from __future__ import annotations

import urllib.error
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise urllib.error.HTTPError(req.full_url, code, "redirects are not followed", headers, fp)


def open_url(req: urllib.request.Request, timeout: float, direct: bool = False):
    """Like urlopen, without redirects. direct=True also ignores any HTTP proxy settings (LAN calls)."""
    handlers: list = [_NoRedirect()]
    if direct:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers).open(req, timeout=timeout)
