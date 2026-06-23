"""Unit tests for sf_factory.usage.get_usage — the shared serve-stale-on-failure
cache that fronts the Claude OAuth usage endpoint (reliability fix, 23-06).

Every test injects a fake ``fetch`` (so no network) + an explicit ``now`` clock
+ a tmp ``cache_path``, mirroring the scheduler tests' stub-the-IO style. The
cache file format is ``{"fetched_at": float, "raw": dict}``.
"""

from __future__ import annotations

import json
from pathlib import Path

from sf_factory.usage import get_usage

_RAW = {
    "five_hour": {"utilization": 42, "resets_at": "2026-06-23T10:00:00Z"},
    "seven_day": {"utilization": 71, "resets_at": "2026-06-25T00:00:00Z"},
    "extra_usage": {"is_enabled": False},
}
_RAW2 = {
    "five_hour": {"utilization": 88, "resets_at": "2026-06-23T11:00:00Z"},
    "seven_day": {"utilization": 90, "resets_at": "2026-06-25T00:00:00Z"},
    "extra_usage": {"is_enabled": True},
}

# Default knobs mirroring config.CapacityGovernorCfg, so the tests read like the
# real call site.
_DEFAULTS: dict = {
    "endpoint": "https://api.anthropic.com/api/oauth/usage",
    "beta_header": "oauth-2025-04-20",
    "timeout_s": 20.0,
    "fresh_ttl_s": 90.0,
    "max_stale_s": 900.0,
}


def _write_credentials(tmp_path: Path, token: str = "tok-abc") -> str:
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}), encoding="utf-8")
    return str(path)


def _write_cache(cache_path: Path, fetched_at: float, raw: dict) -> None:
    cache_path.write_text(json.dumps({"fetched_at": fetched_at, "raw": raw}), encoding="utf-8")


class _CountingFetch:
    """Records call count; returns ``result`` or raises ``error`` if set."""

    def __init__(self, result: dict | None = None, error: BaseException | None = None) -> None:
        self.calls = 0
        self._result = result
        self._error = error

    def __call__(self, token: str, endpoint: str, beta_header: str, timeout_s: float) -> dict:
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _call(tmp_path: Path, *, now: float, fetch, creds: str | None = None) -> dict | None:
    return get_usage(
        credentials_path=creds if creds is not None else _write_credentials(tmp_path),
        cache_path=str(tmp_path / "usage-cache.json"),
        now=now,
        fetch=fetch,
        **_DEFAULTS,
    )


# (a) fresh cache -> no fetch call ------------------------------------------------


def test_fresh_cache_returns_without_fetching(tmp_path: Path) -> None:
    cache = tmp_path / "usage-cache.json"
    _write_cache(cache, fetched_at=1000.0, raw=_RAW)
    fetch = _CountingFetch(error=AssertionError("must not fetch on a fresh cache"))
    # now is 60s after fetched_at, inside the 90s fresh window.
    result = _call(tmp_path, now=1060.0, fetch=fetch)
    assert result == _RAW
    assert fetch.calls == 0


# (b) stale-but-within-max_stale + fetch raises -> returns stale raw --------------


def test_stale_within_max_stale_serves_stale_on_fetch_failure(tmp_path: Path) -> None:
    cache = tmp_path / "usage-cache.json"
    _write_cache(cache, fetched_at=1000.0, raw=_RAW)
    fetch = _CountingFetch(error=OSError("429 Too Many Requests"))
    # now is 300s later: past fresh (90s) but well within max_stale (900s).
    result = _call(tmp_path, now=1300.0, fetch=fetch)
    assert result == _RAW  # the anti-blindness serve-stale
    assert fetch.calls == 1  # it DID attempt a refresh first


# (c) older than max_stale + fetch raises -> None --------------------------------


def test_too_stale_and_fetch_fails_returns_none(tmp_path: Path) -> None:
    cache = tmp_path / "usage-cache.json"
    _write_cache(cache, fetched_at=1000.0, raw=_RAW)
    fetch = _CountingFetch(error=OSError("network down"))
    # now is 1000s later: past max_stale (900s) -> fail-explicit, not a guess.
    result = _call(tmp_path, now=2000.0, fetch=fetch)
    assert result is None
    assert fetch.calls == 1


# (d) successful fetch writes the cache file and returns -------------------------


def test_successful_fetch_writes_cache_and_returns(tmp_path: Path) -> None:
    cache = tmp_path / "usage-cache.json"
    assert not cache.exists()
    fetch = _CountingFetch(result=_RAW2)
    result = _call(tmp_path, now=5000.0, fetch=fetch)
    assert result == _RAW2
    assert fetch.calls == 1
    # The cache file now holds {fetched_at: now, raw}.
    written = json.loads(cache.read_text(encoding="utf-8"))
    assert written == {"fetched_at": 5000.0, "raw": _RAW2}


# (e) two sequential calls within fresh_ttl -> exactly one fetch -----------------


def test_two_calls_within_fresh_ttl_fetch_once(tmp_path: Path) -> None:
    fetch = _CountingFetch(result=_RAW)
    creds = _write_credentials(tmp_path)
    first = _call(tmp_path, now=7000.0, fetch=fetch, creds=creds)
    # second call 30s later (inside the 90s fresh window) reads the cache the
    # first call wrote — no new fetch.
    second = _call(tmp_path, now=7030.0, fetch=fetch, creds=creds)
    assert first == _RAW
    assert second == _RAW
    assert fetch.calls == 1


# extra: no token -> serve a recent cache if present, else None ------------------


def test_no_token_serves_recent_cache(tmp_path: Path) -> None:
    cache = tmp_path / "usage-cache.json"
    _write_cache(cache, fetched_at=1000.0, raw=_RAW)
    # Credentials file missing entirely -> no token. now is 300s later (stale but
    # within max_stale) -> serve the recent cache (still useful to the drain).
    fetch = _CountingFetch(error=AssertionError("no token => must not fetch"))
    result = get_usage(
        credentials_path=str(tmp_path / "absent-creds.json"),
        cache_path=str(cache),
        now=1300.0,
        fetch=fetch,
        **_DEFAULTS,
    )
    assert result == _RAW
    assert fetch.calls == 0


def test_no_token_no_cache_returns_none(tmp_path: Path) -> None:
    fetch = _CountingFetch(error=AssertionError("no token => must not fetch"))
    result = get_usage(
        credentials_path=str(tmp_path / "absent-creds.json"),
        cache_path=str(tmp_path / "absent-cache.json"),
        now=1.0,
        fetch=fetch,
        **_DEFAULTS,
    )
    assert result is None
    assert fetch.calls == 0


# extra: another caller holds the fetch lock -> serve cache, never block ----------


def test_lock_contention_serves_existing_cache_without_fetching(tmp_path: Path) -> None:
    import fcntl

    cache = tmp_path / "usage-cache.json"
    _write_cache(cache, fetched_at=1000.0, raw=_RAW)
    lock_path = str(cache) + ".lock"
    # Simulate a concurrent caller mid-fetch by holding the exclusive lock.
    holder = open(lock_path, "w", encoding="utf-8")  # noqa: SIM115 — held for the test body
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        fetch = _CountingFetch(error=AssertionError("locked => must not fetch here"))
        # now is 300s later: not fresh, so it tries to lock, fails (held), and
        # serves the existing (stale) cache instead of blocking.
        result = _call(tmp_path, now=1300.0, fetch=fetch)
        assert result == _RAW
        assert fetch.calls == 0
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()


def test_lock_contention_no_cache_returns_none(tmp_path: Path) -> None:
    import fcntl

    cache = tmp_path / "usage-cache.json"  # no cache written
    lock_path = str(cache) + ".lock"
    holder = open(lock_path, "w", encoding="utf-8")  # noqa: SIM115 — held for the test body
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        fetch = _CountingFetch(error=AssertionError("locked => must not fetch here"))
        result = _call(tmp_path, now=10.0, fetch=fetch)
        assert result is None
        assert fetch.calls == 0
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()


# extra: a malformed cache is ignored (treated as absent) ------------------------


def test_malformed_cache_is_ignored(tmp_path: Path) -> None:
    cache = tmp_path / "usage-cache.json"
    cache.write_text("{not valid json", encoding="utf-8")
    fetch = _CountingFetch(result=_RAW2)
    result = _call(tmp_path, now=42.0, fetch=fetch)
    assert result == _RAW2  # fell through to a live fetch
    assert fetch.calls == 1
    assert json.loads(cache.read_text(encoding="utf-8")) == {"fetched_at": 42.0, "raw": _RAW2}
