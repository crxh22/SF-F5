"""External orchestrator liveness check — cron/systemd timer entry (DoD §9).

A separate PROCESS from the orchestrator: the OS scheduler is the root of
trust, so the watchdog itself needs no monitor (Doctrine §20 — mechanical
detection, never someone remembering to look). Silence means healthy: it
publishes one max-priority ntfy push per failing check and writes nothing
anywhere — its only outputs are the push, stderr diagnostics for the cron
mail / systemd journal, and the exit code.

It reads exactly two files and NEVER the DB (design §2 read/write paths):

- pidfile (``process.pid_file``) — written by ``cli run`` at startup (also the
  single-instance flock target) and refreshed every scheduler tick (§4
  ``run_forever``). Content contract, shared with ``cli.py`` (the writer):
  line 1 = orchestrator pid (decimal); line 2 (optional) = its command line —
  ``/proc/<pid>/cmdline`` with NUL separators replaced by single spaces.
  The reader tolerates a pid-only file by falling back to a package-name
  cmdline check. The cmdline match is defense-in-depth against pid reuse
  only: real death is independently caught by liveness staleness (nothing
  else refreshes the liveness file), so matching errs tolerant — a strict
  mismatch could page on a healthy orchestrator, while a tolerant match can
  never silence a dead one.
- liveness file (``process.liveness_file``) — content irrelevant; mtime =
  last orchestrator tick (refreshed by ``run_forever`` every
  ``process.loop_tick_s`` and by ``Scheduler.recover()`` while scanning).

Relative config paths resolve against ``factory.home``: a cron job's cwd is
arbitrary and must not change what the watchdog checks.

May import: models, config, notify (design §1).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from sf_factory.config import FactoryConfig, load_config
from sf_factory.models import ConfigError, NotifyError
from sf_factory.notify import NtfyPublisher, dashboard_link

#: Package identity tokens (module path / console-script name) — the pid-only
#: pidfile fallback for the cmdline match. Not tunables: they are the package's
#: own name, fixed by pyproject, like an import statement.
_PACKAGE_TOKENS = ("sf_factory", "sf-factory")

#: Founder-facing page title (founder protocol §6: Romanian, plain language;
#: D-0004: title + deep link only — details go to stderr/journal, not the push).
_DOWN_TITLE = "Fabrica s-a oprit: orchestratorul nu mai răspunde"

#: Dashboard fragment for the health strip (DoD §9: it shows the last
#: orchestrator liveness timestamp for on-demand confirmation). CCR-8: the
#: D-0027 UX slice renders the anchors 'acum'/'escaladari'/'decizii' and the
#: health strip lives inside <section id='acum'> — a deep link must land on a
#: REAL rendered anchor, never the dead '#health' fragment this replaced.
_HEALTH_FRAGMENT = "acum"


def _resolve(home: Path, path: Path) -> Path:
    """Anchor a relative config path at ``factory.home`` (cron cwd is arbitrary)."""
    return path if path.is_absolute() else home / path


def _mtime_age_s(path: Path, now: float) -> float | None:
    """Seconds since ``path`` was last touched; None if missing/unreadable
    (either way it cannot attest liveness — fail toward paging, never silence)."""
    try:
        return now - path.stat().st_mtime
    except OSError:
        return None


def _read_pidfile(path: Path) -> tuple[int, str | None] | None:
    """Parse the pidfile per the module-docstring contract.

    Returns ``(pid, recorded_cmdline_or_None)``; None when the file is
    missing, unreadable, or its first line is not a positive integer.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines:
        return None
    try:
        pid = int(lines[0].strip())
    except ValueError:
        return None
    if pid <= 0:  # 0/negative address process groups, never a single process
        return None
    recorded = lines[1].strip() if len(lines) > 1 else ""
    return pid, (recorded or None)


def _proc_cmdline(pid: int) -> str | None:
    """Normalized ``/proc/<pid>/cmdline`` (NUL → space, stripped).

    None = no such process (or unreadable); empty string = zombie — dead for
    liveness purposes, which a bare ``kill(pid, 0)`` would miss.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()


def _cmdline_matches(recorded: str | None, live: str) -> bool:
    """True when the live cmdline plausibly IS the orchestrator (see module
    docstring for why this match is deliberately tolerant)."""
    if not live:
        return False  # zombie / unreadable — never a live orchestrator
    if recorded is not None and recorded == live:
        return True
    return any(token in live for token in _PACKAGE_TOKENS)


def check_once(cfg: FactoryConfig) -> bool:
    """Pid alive (pidfile + cmdline match) AND liveness file mtime fresher than
    staleness_threshold_s; a pidfile younger than staleness_threshold_s counts
    as startup/recovery grace (recover() also touches the liveness file, so a
    healthy restart never pages the founder). On failure publish max-priority
    ntfy; silence means healthy (DoD §9). Reads files only, never the DB.

    The grace doubles as the healthy fast path: ``run_forever`` refreshes the
    pidfile every tick, so a fresh pidfile already proves a ticking loop, and
    a wedged or dead orchestrator stops refreshing it — the grace then expires
    within one staleness window and the real checks run. Returns True =
    healthy. Diagnostics go to stderr (the cron mail / journal evidence
    channel); a failed publish is reported there too and still returns False.
    """
    now = time.time()
    threshold_s = float(cfg.founder_channel.watchdog.staleness_threshold_s)
    home = cfg.factory.home
    pid_path = _resolve(home, cfg.process.pid_file)
    liveness_path = _resolve(home, cfg.process.liveness_file)

    pidfile_age = _mtime_age_s(pid_path, now)
    if pidfile_age is not None and pidfile_age < threshold_s:
        return True  # startup/recovery grace (§4)

    failures: list[str] = []

    parsed = _read_pidfile(pid_path)
    if parsed is None:
        failures.append(f"pidfile {pid_path} missing or unparseable")
    else:
        pid, recorded = parsed
        live = _proc_cmdline(pid)
        if live is None:
            failures.append(f"orchestrator pid {pid} not running")
        elif not _cmdline_matches(recorded, live):
            failures.append(f"pid {pid} cmdline {live!r} is not the orchestrator (pid reuse?)")

    liveness_age = _mtime_age_s(liveness_path, now)
    if liveness_age is None:
        failures.append(f"liveness file {liveness_path} missing")
    elif liveness_age >= threshold_s:
        failures.append(
            f"liveness file stale: {liveness_age:.0f}s old >= threshold {threshold_s:.0f}s"
        )

    if not failures:
        return True

    print(f"watchdog: orchestrator DOWN: {'; '.join(failures)}", file=sys.stderr)
    publisher = NtfyPublisher(cfg)
    try:
        asyncio.run(
            publisher.publish(
                _DOWN_TITLE,
                link=dashboard_link(cfg, _HEALTH_FRAGMENT),
                priority=publisher.priority_alert,
            )
        )
    except NotifyError as exc:
        # The watchdog has no DB to record alert_delivery_failed (§6) — the OS
        # scheduler's stderr capture is its evidence channel.
        print(f"watchdog: alert delivery failed: {exc}", file=sys.stderr)
    return False


def main(argv: Sequence[str] | None = None) -> int:
    """Entry for cron/systemd timer: load config, check_once, exit 0/1.

    0 = healthy; 1 = orchestrator down (push attempted) or config unloadable
    (reported on stderr — the watchdog cannot page without a valid ntfy
    config, but cron/systemd still see the nonzero exit and the message).
    Schedule it every ``founder_channel.watchdog.check_interval_s`` seconds.
    """
    parser = argparse.ArgumentParser(
        prog="python -m sf_factory.watchdog",
        description="External orchestrator liveness check (DoD §9); silent when healthy.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("factory.config.yaml"),
        help="path to factory.config.yaml (default: ./factory.config.yaml; "
        "cron entries should pass an absolute path)",
    )
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"watchdog: cannot load config: {exc}", file=sys.stderr)
        return 1
    return 0 if check_once(cfg) else 1


if __name__ == "__main__":  # python -m sf_factory.watchdog (design §1)
    raise SystemExit(main())
