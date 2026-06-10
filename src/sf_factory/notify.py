"""ntfy HTTP publisher — title + deep link only, never artifact content (D-0004).

Design §1/§4 ``notify``: the founder push channel. Payloads are minimal BY
SHAPE: ``publish`` accepts a title and a dashboard deep link and nothing else,
so the D-0004 constraint ("never artifact content") is enforced structurally,
not by caller discipline.

Concurrency (§7): the blocking ``urllib`` POST runs off-loop via
``asyncio.to_thread``, bounded by ``founder_channel.ntfy.timeout_s`` — a hung
ntfy connection must never stall the scheduler loop or the liveness heartbeat.
Every delivery failure raises ``NotifyError`` (§6: callers record
``alert_delivery_failed``; unit state is never changed by a notification
failure).

Founder-facing titles are Romanian (founder protocol §6) while HTTP headers
are latin-1-only in ``http.client`` — non-ASCII header values are therefore
RFC 2047-encoded (the mechanism ntfy documents for UTF-8 headers).

May import: models, config (design §1) — never db.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import socket
import urllib.error
import urllib.parse
import urllib.request

from sf_factory.config import FactoryConfig
from sf_factory.models import NotifyError

#: RFC 3986 reserved + unreserved characters, plus ``%`` so an already
#: percent-encoded URL passes through byte-identical (idempotent quoting).
_URL_SAFE = ":/?#[]@!$&'()*+,;=%-._~"


def _header_text(value: str) -> str:
    """One-line, header-safe form of ``value``.

    Whitespace runs (including CR/LF — header injection) collapse to single
    spaces; non-ASCII text is RFC 2047 base64-encoded so ``http.client``'s
    latin-1 header encoding can never fail on Romanian diacritics.
    """
    flat = " ".join(value.split())
    if flat.isascii():
        return flat
    encoded = base64.b64encode(flat.encode("utf-8")).decode("ascii")
    return f"=?UTF-8?B?{encoded}?="


def _ascii_url(link: str) -> str:
    """Percent-encode anything outside the URL character set (idempotent for
    well-formed ASCII URLs); the Click header must stay a literal URL, so it is
    never RFC 2047-encoded."""
    return urllib.parse.quote(link, safe=_URL_SAFE)


class NtfyPublisher:
    """Binds founder_channel.ntfy server/topic/priorities/timeout_s."""

    def __init__(self, cfg: FactoryConfig) -> None:
        ntfy = cfg.founder_channel.ntfy
        self._publish_url = f"{ntfy.server.rstrip('/')}/{urllib.parse.quote(ntfy.topic, safe='')}"
        self._timeout_s = ntfy.timeout_s
        #: Config-named priorities, bound here so callers route by key
        #: (founder_channel.ntfy.priority_decision / priority_alert) and never
        #: hardcode a priority string (Doctrine §14).
        self.priority_decision = ntfy.priority_decision
        self.priority_alert = ntfy.priority_alert

    async def publish(
        self, title: str, *, link: str | None = None, priority: str = "default"
    ) -> None:
        """POST to ntfy: title + deep link only, never artifact content (D-0004).

        The blocking HTTP call runs off-loop (``asyncio.to_thread``) with
        ``founder_channel.ntfy.timeout_s`` — a hung ntfy connection must never
        stall the scheduler loop or the liveness heartbeat (§7); raises
        ``NotifyError`` on timeout/HTTP error.
        """
        await asyncio.to_thread(self._post, title, link, priority)

    def _post(self, title: str, link: str | None, priority: str) -> None:
        """Synchronous worker: one POST, total error surface = NotifyError."""
        headers = {
            "Title": _header_text(title),
            "Priority": _header_text(priority),
        }
        if link is not None:
            headers["Click"] = _ascii_url(link)
        # Body = the deep link again (visible in clients that do not surface
        # the Click action); still title + deep link only, per D-0004.
        body = (link or "").encode("utf-8")
        request = urllib.request.Request(
            self._publish_url, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            raise NotifyError(
                f"ntfy POST {self._publish_url} failed: HTTP {exc.code} {exc.reason}"
            ) from exc
        except (OSError, http.client.HTTPException) as exc:
            # OSError covers URLError, connection failures and socket timeouts
            # (TimeoutError is an OSError); HTTPException covers malformed
            # responses (e.g. BadStatusLine).
            raise NotifyError(f"ntfy POST {self._publish_url} failed: {exc!r}") from exc


def dashboard_link(cfg: FactoryConfig, fragment: str) -> str:
    """Deep link into the dashboard for a unit/decision.

    Host = this machine's hostname: the dashboard serves only the tailnet
    interface (``founder_channel.dashboard.bind: tailscale``) and the founder's
    devices resolve the hostname over Tailscale MagicDNS (environment audit:
    ``server-e9``). Plain http — Tailscale is the transport boundary (DoD §9).
    The fragment is percent-encoded, so the result is always an ASCII URL.
    """
    port = cfg.founder_channel.dashboard.port
    encoded_fragment = urllib.parse.quote(fragment, safe="/")
    return f"http://{socket.gethostname()}:{port}/#{encoded_fragment}"
