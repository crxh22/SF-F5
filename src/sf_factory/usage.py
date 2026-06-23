"""Shared Claude OAuth usage-limit query with a single cross-process cache.

ONE source of truth for the live 5h/weekly usage that the proactive auto-drain
relies on. Multiple INDEPENDENT callers used to each hit
``GET api.anthropic.com/api/oauth/usage`` with the SAME bearer token on their
own interval (the architect monitor via ``sf-limit.sh``, the capacity governor's
``_query_usage`` poll, and the dashboard poller through ``sf-limit.sh``). The
endpoint fails INTERMITTENTLY (429 clustering, transient/refresh-gap) and every
such failure blinded the drain — the founder's #1 operational risk.

``get_usage`` collapses those callers onto one cache file with
serve-stale-on-failure:

* a FRESH cache (within ``fresh_ttl_s``) is returned with NO network call —
  this alone removes most of the redundant 429-inducing traffic;
* concurrent callers coordinate via a NON-BLOCKING ``fcntl.flock`` so only one
  fetches at a time (the others serve the existing cache rather than pile on);
* a fetch FAILURE serves the last cached value while it is still within
  ``max_stale_s`` — a RECENT cached number is evidence, not a guess, so this
  stays inside Doctrine §7 fail-explicit (a too-stale / absent cache IS the
  explicit ``None`` failure).

Pure + testable: the ``now`` clock and the ``fetch`` callable are injected, so
unit tests never touch the wall clock or the network. The wall clock (NOT an
asyncio loop clock) is the right base for freshness because the cache file is
shared across processes.

May import: stdlib only (no factory deps) — it is imported by both the package
(scheduler) and a bare ``python -c``-style invocation from ``sf-limit.sh``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

#: Endpoint fetcher signature: (token, endpoint, beta_header, timeout_s) -> raw dict.
Fetch = Callable[[str, str, str, float], dict[str, Any]]


def default_fetch(token: str, endpoint: str, beta_header: str, timeout_s: float) -> dict[str, Any]:
    """The real OAuth usage GET (sf-limit.sh / governor parity, D-0058): Bearer
    token + ``anthropic-beta`` header. Raises on any network/parse failure — the
    caller (``get_usage``) turns that into serve-stale or an explicit ``None``."""
    request = urllib.request.Request(
        endpoint,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": beta_header,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310 — fixed https endpoint
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError(f"usage endpoint returned a non-object body: {type(data).__name__}")
    return data


def _read_token(credentials_path: str) -> str | None:
    """Read ``claudeAiOauth.accessToken`` from the credentials JSON; ``None`` on
    any missing-file / malformed / missing-key failure (never raises)."""
    try:
        with open(os.path.expanduser(credentials_path), encoding="utf-8") as handle:
            token = json.load(handle)["claudeAiOauth"]["accessToken"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return token if isinstance(token, str) and token else None


def _read_cache(cache_path: str) -> tuple[float, dict[str, Any]] | None:
    """Read the cache JSON ``{"fetched_at": float, "raw": dict}``; ``None`` on any
    missing / malformed / wrong-shape failure (never raises)."""
    try:
        with open(os.path.expanduser(cache_path), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    fetched_at = data.get("fetched_at")
    raw = data.get("raw")
    if not isinstance(fetched_at, (int, float)) or not isinstance(raw, dict):
        return None
    return float(fetched_at), raw


def _write_cache(cache_path: str, fetched_at: float, raw: dict[str, Any]) -> None:
    """Atomically write the cache (temp file + ``os.replace``) so a concurrent
    reader never sees a half-written file. Best-effort: a write failure is
    swallowed (the fetched ``raw`` is still returned to the caller)."""
    expanded = os.path.expanduser(cache_path)
    directory = os.path.dirname(expanded) or "."
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".sf-usage-", suffix=".tmp")
    except OSError:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"fetched_at": fetched_at, "raw": raw}, handle)
        os.replace(tmp, expanded)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def get_usage(
    *,
    credentials_path: str,
    endpoint: str,
    beta_header: str,
    timeout_s: float,
    cache_path: str,
    fresh_ttl_s: float,
    max_stale_s: float,
    now: float,
    fetch: Fetch = default_fetch,
) -> dict[str, Any] | None:
    """Return the raw OAuth usage dict (``five_hour``/``seven_day``/
    ``extra_usage``) or ``None``, via the shared cache.

    Logic (single source for every caller — see the module docstring):

    1. If the cache is FRESH (``now - fetched_at < fresh_ttl_s``) -> return it,
       NO network.
    2. Else take a NON-BLOCKING ``flock`` on ``<cache_path>.lock``. If NOT
       acquired (another caller is fetching right now) -> return the existing
       cache ``raw`` if present (even slightly stale) else ``None`` — never
       block.
    3. If acquired: RE-READ the cache (the holder may have just refreshed it) ->
       if now fresh, return it. Else ``fetch``:

       * success -> atomically write the cache, return the raw dict;
       * failure (ANY exception) -> serve the STALE cached raw while it is within
         ``max_stale_s`` (the anti-blindness behavior), otherwise ``None``
         (fail-explicit, Doctrine §7).

    Never raises: a missing token, an unreadable cache, or any unexpected error
    degrades to ``None``. The lock is always released in a ``finally``.
    """
    token = _read_token(credentials_path)

    cache = _read_cache(cache_path)
    if cache is not None and now - cache[0] < fresh_ttl_s:
        return cache[1]

    # No token => we cannot fetch at all. Serve a recent cached value if we have
    # one (still useful to the drain), else fail-explicit.
    if token is None:
        if cache is not None and now - cache[0] < max_stale_s:
            return cache[1]
        return None

    lock_path = os.path.expanduser(cache_path) + ".lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        # Cannot even open the lock file — fall back to the best cache we have.
        if cache is not None and now - cache[0] < max_stale_s:
            return cache[1]
        return None

    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Another caller holds the lock (is fetching). Don't block — serve the
            # existing cache if present (even slightly stale), else None.
            return cache[1] if cache is not None else None

        # We hold the lock. Re-read: the previous holder may have just refreshed.
        cache = _read_cache(cache_path)
        if cache is not None and now - cache[0] < fresh_ttl_s:
            return cache[1]

        try:
            raw = fetch(token, endpoint, beta_header, timeout_s)
        except Exception:  # noqa: BLE001 — any fetch failure -> serve-stale or fail-explicit
            if cache is not None and now - cache[0] < max_stale_s:
                return cache[1]
            return None

        _write_cache(cache_path, now, raw)
        return raw
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
