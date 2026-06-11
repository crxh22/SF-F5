"""Operator entry (design §4): ``init`` / ``run`` / ``status`` / ``resume`` / ``decide``.

Command contracts (design §4 ``cli.main`` docstring, implemented exactly):

- ``init`` — validate config, create the DB + apply migrations, environment
  sanity check (git, model-route CLIs, canon files, operational dirs).
- ``run`` / ``resume`` — FIRST acquire an exclusive ``flock`` on
  ``process.pid_file`` and hold it for the process lifetime; if it is held, or
  the recorded pid+cmdline is alive, abort with a clear message (a second
  instance would orphan-sweep the live instance's agents, double-write the DB
  behind busy_timeout, and mask watchdog death detection). Only then
  ``Scheduler.recover()`` + ``run_forever()`` (``resume``: ``run_until_blocked()``).
- ``status [--json] [--write]`` — render a generated view from a READ-ONLY db
  connection (``mode=ro`` — legitimately concurrent with a live orchestrator,
  §2) + git (non-canonical, Doctrine §9). ``--write`` also writes the rendered
  view to ``<factory.home>/STATUS.md`` (DoD §6: STATUS.md is a generated view).
- ``decide <request_id> <option>`` — emergency-fallback decision-answer path
  (DoD §9 plumbing; the expected founder path is the dashboard answer
  endpoint, §1): writes the answer artifact, commits it in the factory repo
  (D-0015), then in ONE transaction registers it
  (``artifacts.register_artifact``) + ``db.answer_decision`` + event. The
  direct DB write from a second OS process is the D-0015-ratified emergency
  exception to the §2 sole-writer rule.

Pidfile content contract (shared with ``watchdog.py``, the reader): line 1 =
orchestrator pid (decimal); line 2 (optional) = its command line —
``/proc/<pid>/cmdline`` with NUL separators replaced by single spaces.

Relative config paths resolve against ``factory.home`` (the operator may run
from any cwd). All tunables are read from config by key (Doctrine §14).

SQL boundary note: every DB access goes through ``db.py`` repository functions
except two module-private READ-ONLY SELECTs of the status view (open
escalations list, recent-events tail) — presentation queries over the §2 DDL
on the ``mode=ro`` connection, with no business rules and no writes.

May import: all (design §1). The run/resume object graph (scheduler,
consultation, runner, thresholds, statemachine, notify) is imported inside
``_build_scheduler`` — only those commands need it, and wave-3 builds are
file-disjoint (§9): ``init``/``status``/``decide`` stay importable and
testable independently of sibling-lane modules. ``worktrees`` (wave 2, no
lane concern) is imported at module level: ``decide`` commits its artifact.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sf_factory.artifacts import register_artifact, unit_artifact_dir
from sf_factory.config import FactoryConfig, load_config
from sf_factory.db import (
    MIGRATIONS_DIR,
    Database,
    answer_decision,
    insert_event,
    list_units,
    pending_decisions,
    processes_in_state,
    unit_token_total,
)
from sf_factory.models import (
    DecisionRequest,
    FactoryError,
    GitError,
    Level,
    Phase,
    Stage,
    utc_now,
)
from sf_factory.worktrees import commit_paths, run_git

if TYPE_CHECKING:  # import cycle-free typing only; runtime import is lazy (§9 lanes)
    from sf_factory.scheduler import Scheduler

logger = logging.getLogger(__name__)

#: Package identity tokens for the pid-reuse cmdline match — same fixed package
#: names as watchdog.py (pyproject module path / console-script name), not tunables.
_PACKAGE_TOKENS = ("sf_factory", "sf-factory")

#: Generated-view marker line (Doctrine §9: generated views are never canonical).
_VIEW_MARKER = "GENERATED VIEW — non-canonical (Doctrine §9); sources: SQLite (mode=ro) + git."


def _resolve(home: Path, path: Path) -> Path:
    """Anchor a relative config path at ``factory.home`` (operator cwd is arbitrary)."""
    return path if path.is_absolute() else home / path


# --------------------------------------------------------- pid/cmdline helpers
# Pidfile format per the module-docstring contract shared with watchdog.py.


def _self_cmdline() -> str:
    """This process's ``/proc/self/cmdline`` normalized (NUL → space, stripped).

    Empty string on a /proc-less platform: the pidfile is then written pid-only,
    which the watchdog reader explicitly tolerates (package-name fallback).
    """
    try:
        raw = Path("/proc/self/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()


def _proc_cmdline(pid: int) -> str | None:
    """Normalized ``/proc/<pid>/cmdline``; None = no such process (or unreadable);
    empty string = zombie — dead for instance-check purposes."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()


def _parse_pidfile_text(text: str) -> tuple[int, str | None] | None:
    """Parse pidfile content; ``(pid, recorded_cmdline_or_None)`` or None if
    empty/unparseable (same tolerance as the watchdog reader)."""
    lines = text.splitlines()
    if not lines:
        return None
    try:
        pid = int(lines[0].strip())
    except ValueError:
        return None
    if pid <= 0:
        return None
    recorded = lines[1].strip() if len(lines) > 1 else ""
    return pid, (recorded or None)


def _cmdline_matches(recorded: str | None, live: str) -> bool:
    """True when ``live`` plausibly IS an orchestrator. Deliberately tolerant
    (exact recorded match OR package token): this check only fires after the
    flock was somehow lost (file replaced), and erring toward refusal can never
    double-start an orchestrator, while a strict mismatch could."""
    if not live:
        return False
    if recorded is not None and recorded == live:
        return True
    return any(token in live for token in _PACKAGE_TOKENS)


class _InstanceLock:
    """Exclusive flock on ``process.pid_file`` held for the process lifetime (§4).

    ``acquire`` refuses (FactoryError) when the flock is already held by another
    process OR — belt and braces against a replaced/unlinked pidfile that broke
    the flock chain — when the recorded pid is alive with an orchestrator-like
    cmdline. Only after both checks does it claim the file (truncate + write
    pid/cmdline). The fd stays open until ``release``; children never inherit it
    (asyncio subprocesses close fds on exec).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        if self._fd is not None:
            raise FactoryError(f"instance lock already acquired: {self._path}")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # O_RDWR|O_CREAT only — never truncate content we do not own yet.
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                holder = _parse_pidfile_text(self._read_all(fd))
                detail = f" (pidfile records pid {holder[0]})" if holder else ""
                raise FactoryError(
                    f"another orchestrator instance holds the lock on {self._path}"
                    f"{detail} — refusing to start a second instance (a second"
                    " instance would orphan-sweep the live one's agents and"
                    " double-write the DB)"
                ) from exc
            recorded = _parse_pidfile_text(self._read_all(fd))
            if recorded is not None:
                pid, rec_cmd = recorded
                if pid != os.getpid():
                    live = _proc_cmdline(pid)
                    if live is not None and _cmdline_matches(rec_cmd, live):
                        raise FactoryError(
                            f"pidfile {self._path} records pid {pid} which is alive"
                            f" with an orchestrator cmdline ({live!r}) — refusing to"
                            " start a second instance (flock was lost: pidfile"
                            " replaced?)"
                        )
            # Both checks passed: claim the file (pidfile contract, watchdog reads it).
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            cmdline = _self_cmdline()
            content = f"{os.getpid()}\n" + (f"{cmdline}\n" if cmdline else "")
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
        except BaseException:
            os.close(fd)  # releases the flock if we held it
            raise
        self._fd = fd

    def release(self) -> None:
        """Close the fd (kernel releases the flock). The pidfile itself stays:
        its content is evidence and the next ``run`` tolerates a dead pid."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    @staticmethod
    def _read_all(fd: int) -> str:
        size = os.fstat(fd).st_size
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, size).decode("utf-8", errors="replace") if size else ""


# ------------------------------------------------------------------ open helpers


def _db_path(cfg: FactoryConfig) -> Path:
    return _resolve(cfg.factory.home, cfg.process.db_path)


def _open_db(cfg: FactoryConfig, *, read_only: bool = False) -> Database:
    """Open the factory DB; a missing file is an explicit 'run init first' error,
    never an implicitly created empty database (Doctrine §7)."""
    path = _db_path(cfg)
    if not path.is_file():
        raise FactoryError(f"database not found at {path} — run `sf-factory init` first")
    db = Database(path, cfg.process.db_busy_timeout_ms)
    try:
        db.open(read_only=read_only)
    except sqlite3.Error as exc:
        raise FactoryError(f"cannot open database {path}: {exc}") from exc
    return db


# ------------------------------------------------------------------------- init


def cmd_init(cfg: FactoryConfig) -> int:
    """Validate config (already loaded), env sanity check, create DB + migrate."""
    home = cfg.factory.home
    problems: list[str] = []
    if not home.is_dir():
        problems.append(f"factory.home is not a directory: {home}")
    if shutil.which("git") is None:
        problems.append("`git` not found on PATH — worktree/merge mechanics need it")
    for cli_name in sorted({route.cli for route in cfg.models.values()}):
        if cli_name == "stub":
            stub = _resolve(home, cfg.process.stub_agent_path)
            if not stub.is_file():
                problems.append(f"process.stub_agent_path not found: {stub}")
        elif shutil.which(cli_name) is None:
            problems.append(
                f"model routes use cli '{cli_name}' but it is not on PATH"
            )
    for key, rel in sorted(cfg.canon.files.items()):
        path = _resolve(home, Path(rel))
        if not path.is_file():
            problems.append(
                f"canon.files.{key} not found: {path} (D-0009 injection would fail)"
            )
    if problems:
        for problem in problems:
            print(f"sf-factory init: {problem}", file=sys.stderr)
        return 1

    # Operational dirs (gitignored .factory tree): create before first use.
    db_path = _db_path(cfg)
    for directory in (
        db_path.parent,
        _resolve(home, cfg.process.ndjson_log_dir),
        _resolve(home, cfg.process.liveness_file).parent,
        _resolve(home, cfg.process.pid_file).parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    db = Database(db_path, cfg.process.db_busy_timeout_ms)
    db.open()
    try:
        applied = db.migrate(MIGRATIONS_DIR)
    finally:
        db.close()
    if applied:
        print(f"initialized {db_path}: applied migration(s) {applied}")
    else:
        print(f"initialized {db_path}: schema already up to date")

    for name, project in sorted(cfg.projects.items()):
        workspace = _resolve(home, project.workspace)
        if not workspace.is_dir():
            print(
                f"sf-factory init: note: projects.{name}.workspace does not exist yet"
                f" ({workspace}) — created at project kickoff",
                file=sys.stderr,
            )
    return 0


# ------------------------------------------------------------------ run / resume


def _build_scheduler(cfg: FactoryConfig, db: Database) -> Scheduler:
    """Wire the §4 object graph with the FROZEN constructor signatures.

    Imports live here, not at module top: only run/resume need the graph, and
    wave-3 lanes are file-disjoint (design §9) — init/status/decide must not
    depend on sibling-lane modules being importable.
    """
    from sf_factory.consultation import Consultor
    from sf_factory.notify import NtfyPublisher
    from sf_factory.runner import AgentRunner
    from sf_factory.scheduler import PhaseExecutor, Scheduler, StageExecutor
    from sf_factory.statemachine import StateMachine
    from sf_factory.thresholds import ThresholdEvaluator
    from sf_factory.worktrees import WorktreeManager

    sm = StateMachine(db)
    runner = AgentRunner(cfg, db)
    wt = WorktreeManager(cfg)
    thresholds = ThresholdEvaluator(db, cfg)
    consultor = Consultor(cfg, db, runner)
    notify = NtfyPublisher(cfg)
    executors = {
        Level.STAGE: StageExecutor(db, sm, cfg, runner, wt, thresholds, consultor, notify),
        Level.PHASE: PhaseExecutor(db, sm, cfg, runner, wt, notify),
    }
    return Scheduler(db, sm, cfg, executors, notify)


def cmd_run(cfg: FactoryConfig, *, until_blocked: bool) -> int:
    """``run`` (run_forever) / ``resume`` (run_until_blocked) per §4: flock FIRST,
    then recover, then the loop. The lock is held for the whole process lifetime."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    lock = _InstanceLock(_resolve(cfg.factory.home, cfg.process.pid_file))
    lock.acquire()
    try:
        db = _open_db(cfg)
        try:
            applied = db.migrate(MIGRATIONS_DIR)  # idempotent; pending-only
            if applied:
                print(f"sf-factory: applied pending migration(s) {applied}", file=sys.stderr)
            scheduler = _build_scheduler(cfg, db)
            print(f"sf-factory: recovery scan starting (pid {os.getpid()})", file=sys.stderr)
            scheduler.recover()
            print("sf-factory: recovery complete — entering scheduler loop", file=sys.stderr)
            if until_blocked:
                asyncio.run(scheduler.run_until_blocked())
                print(
                    "sf-factory: nothing runnable or running — resume loop done",
                    file=sys.stderr,
                )
            else:
                asyncio.run(scheduler.run_forever())
        finally:
            db.close()
    finally:
        lock.release()
    return 0


# ------------------------------------------------------------------------ status


def _orchestrator_health(cfg: FactoryConfig) -> dict[str, Any]:
    """File-level liveness facts (same files/contract the watchdog reads):
    recorded pid + aliveness, liveness file age vs the config staleness threshold."""
    home = cfg.factory.home
    pid_path = _resolve(home, cfg.process.pid_file)
    liveness_path = _resolve(home, cfg.process.liveness_file)
    threshold_s = float(cfg.founder_channel.watchdog.staleness_threshold_s)

    pid: int | None = None
    alive = False
    try:
        parsed = _parse_pidfile_text(pid_path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        parsed = None
    if parsed is not None:
        pid, recorded = parsed
        live = _proc_cmdline(pid)
        alive = live is not None and _cmdline_matches(recorded, live)

    liveness_age_s: float | None
    try:
        liveness_age_s = round(time.time() - liveness_path.stat().st_mtime, 1)
    except OSError:
        liveness_age_s = None
    return {
        "pid": pid,
        "alive": alive,
        "pid_file": str(pid_path),
        "liveness_file": str(liveness_path),
        "liveness_age_s": liveness_age_s,
        "liveness_stale": liveness_age_s is None or liveness_age_s >= threshold_s,
        "staleness_threshold_s": threshold_s,
    }


def _git_summary(repo_root: Path) -> dict[str, Any]:
    """Branch + short HEAD of a repo — explicitly non-canonical (Doctrine §9)."""
    if not repo_root.is_dir():
        return {"available": False, "reason": "directory missing"}
    out: dict[str, Any] = {"available": True}
    for key, args in (("branch", ("--abbrev-ref", "HEAD")), ("head", ("--short", "HEAD"))):
        try:
            proc = subprocess.run(  # noqa: S603 — fixed git argv, read-only query
                ["git", "-C", str(repo_root), "rev-parse", *args],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"available": False, "reason": str(exc)}
        if proc.returncode != 0:
            return {"available": False, "reason": proc.stderr.strip() or "git error"}
        out[key] = proc.stdout.strip()
    return out


def _open_escalations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Read-only presentation SELECT over the §2 DDL (see module docstring)."""
    rows = conn.execute(
        "SELECT id, unit_level, unit_id, trigger, target, created_at"
        " FROM escalations WHERE status = 'open' ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


def _recent_events(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    """Read-only presentation SELECT over the §2 DDL (see module docstring)."""
    rows = conn.execute(
        "SELECT seq, unit_level, unit_id, event_type, actor, created_at"
        " FROM events ORDER BY seq DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


#: Events shown in the status tail — presentation depth of a generated view,
#: not an operational threshold (no behavior depends on it).
_STATUS_EVENT_TAIL = 10


def _collect_status(cfg: FactoryConfig, db: Database) -> dict[str, Any]:
    """Assemble the generated status view (DoD §9 founder view: decisions awaited,
    what is running, where risk appeared, what was delivered) from the ro
    connection + git. Pure reads."""
    conn = db.read()
    phases = [p for p in list_units(conn, Level.PHASE) if isinstance(p, Phase)]
    stages = [s for s in list_units(conn, Level.STAGE) if isinstance(s, Stage)]

    stage_views: dict[str, list[dict[str, Any]]] = {}
    for stage in stages:
        stage_views.setdefault(stage.phase_id, []).append(
            {
                "id": stage.id,
                "name": stage.name,
                "risk_class": stage.risk_class,
                "state": stage.state.value,
                "branch": stage.branch,
                "tokens": unit_token_total(conn, Level.STAGE.value, stage.id),
            }
        )
    phase_views = [
        {
            "id": phase.id,
            "project": phase.project,
            "name": phase.name,
            "state": phase.state.value,
            "branch": phase.branch,
            "tokens": unit_token_total(conn, Level.PHASE.value, phase.id),
            "stages": stage_views.get(phase.id, []),
        }
        for phase in phases
    ]
    decisions = [
        {
            "id": dr.id,
            "unit_level": dr.unit_level,
            "unit_id": dr.unit_id,
            "gate_kind": dr.gate_kind,
            "created_at": dr.created_at,
            "alerted_at": dr.alerted_at,
        }
        for dr in pending_decisions(conn)
    ]
    processes = [
        {
            "id": rec.id,
            "unit_level": rec.unit_level,
            "unit_id": rec.unit_id,
            "kind": rec.kind,
            "role": rec.role,
            "pid": rec.pid,
            "state": rec.state,
            "spawned_at": rec.spawned_at,
            "heartbeat_at": rec.heartbeat_at,
        }
        for state in ("running", "spawned")
        for rec in processes_in_state(conn, state)
    ]
    git_view: dict[str, Any] = {"factory": _git_summary(cfg.factory.home)}
    for name, project in sorted(cfg.projects.items()):
        git_view[f"project:{name}"] = _git_summary(_resolve(cfg.factory.home, project.workspace))
    return {
        "generated_at": utc_now(),
        "canonical": False,
        "db": {"path": str(_db_path(cfg))},
        "orchestrator": _orchestrator_health(cfg),
        "phases": phase_views,
        "decisions_pending": decisions,
        "escalations_open": _open_escalations(conn),
        "processes_live": processes,
        "git": git_view,
        "events_recent": _recent_events(conn, _STATUS_EVENT_TAIL),
    }


def _render_status(view: dict[str, Any]) -> str:
    """Markdown rendering of the view — also the STATUS.md body (``--write``)."""
    lines: list[str] = [
        "# SF-F5 factory status",
        "",
        f"Generated {view['generated_at']} — {_VIEW_MARKER}",
        "",
        "## Orchestrator",
    ]
    orch = view["orchestrator"]
    if orch["alive"]:
        lines.append(f"- pid {orch['pid']}: alive")
    elif orch["pid"] is not None:
        lines.append(f"- pid {orch['pid']}: NOT running")
    else:
        lines.append("- no orchestrator pid recorded")
    if orch["liveness_age_s"] is None:
        lines.append("- liveness file missing (never started, or operational dir cleaned)")
    else:
        staleness = " (STALE)" if orch["liveness_stale"] else ""
        lines.append(f"- last liveness tick {orch['liveness_age_s']}s ago{staleness}")
    lines.append(f"- db: {view['db']['path']}")

    decisions = view["decisions_pending"]
    lines += ["", f"## Decisions awaited ({len(decisions)})"]
    for dr in decisions:
        alerted = dr["alerted_at"] or "never"
        lines.append(
            f"- [{dr['id']}] {dr['unit_level']}/{dr['unit_id']} — gate {dr['gate_kind']}"
            f" — created {dr['created_at']}, alerted {alerted}"
        )
    if not decisions:
        lines.append("- none")

    escalations = view["escalations_open"]
    lines += ["", f"## Open escalations ({len(escalations)})"]
    for esc in escalations:
        lines.append(
            f"- [{esc['id']}] {esc['unit_level']}/{esc['unit_id']} — {esc['trigger']}"
            f" -> {esc['target']} — created {esc['created_at']}"
        )
    if not escalations:
        lines.append("- none")

    lines += ["", "## Phases"]
    for phase in view["phases"]:
        lines.append(
            f"### {phase['id']} — {phase['state']} (project {phase['project']},"
            f" tokens {phase['tokens']})"
        )
        for stage in phase["stages"]:
            lines.append(
                f"- {stage['id']}: {stage['state']} ({stage['risk_class']},"
                f" tokens {stage['tokens']})"
            )
        if not phase["stages"]:
            lines.append("- no stages planned yet")
    if not view["phases"]:
        lines.append("- no phases")

    processes = view["processes_live"]
    lines += ["", f"## Live agent processes ({len(processes)})"]
    for proc in processes:
        unit = f"{proc['unit_level']}/{proc['unit_id']}" if proc["unit_id"] else "factory"
        lines.append(
            f"- [{proc['id']}] {proc['role']} ({proc['kind']}) on {unit}"
            f" pid={proc['pid']} state={proc['state']} heartbeat={proc['heartbeat_at']}"
        )
    if not processes:
        lines.append("- none")

    lines += ["", "## Git (non-canonical)"]
    for name, summary in view["git"].items():
        if summary.get("available"):
            lines.append(f"- {name}: {summary['branch']} @ {summary['head']}")
        else:
            lines.append(f"- {name}: unavailable ({summary.get('reason', 'unknown')})")

    events = view["events_recent"]
    lines += ["", f"## Recent events (last {len(events)})"]
    for event in events:
        unit = f"{event['unit_level']}/{event['unit_id']}" if event["unit_id"] else "factory"
        lines.append(
            f"- {event['seq']}: {event['event_type']} {unit} by {event['actor']}"
            f" at {event['created_at']}"
        )
    if not events:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def cmd_status(cfg: FactoryConfig, *, as_json: bool, write: bool) -> int:
    """Render the generated view from a mode=ro connection (§2 sanctioned read)."""
    db = _open_db(cfg, read_only=True)
    try:
        try:
            view = _collect_status(cfg, db)
        except sqlite3.Error as exc:
            raise FactoryError(
                f"cannot read database {_db_path(cfg)} (unmigrated or corrupt —"
                f" run `sf-factory init`): {exc}"
            ) from exc
    finally:
        db.close()
    text = _render_status(view)
    print(json.dumps(view, indent=2) if as_json else text, end="" if not as_json else "\n")
    if write:
        out_path = cfg.factory.home / "STATUS.md"
        out_path.write_text(text, encoding="utf-8")
        print(f"sf-factory: wrote {out_path}", file=sys.stderr)
    return 0


# ------------------------------------------------------------------------ decide


def _render_decision_answer(dr: DecisionRequest, answer: str, answered_at: str) -> str:
    """Decision-answer artifact body (kind='decision_answer')."""
    return (
        f"# Decision answer — request {dr.id}\n\n"
        f"- request_id: {dr.id}\n"
        f"- unit: {dr.unit_level}/{dr.unit_id}\n"
        f"- gate_kind: {dr.gate_kind}\n"
        f"- request_artifact_id: {dr.request_artifact_id}\n"
        f"- answer: {answer}\n"
        f"- answered_at: {answered_at}\n"
        f"- answered_via: cli (emergency fallback, DoD §9 — expected path is the"
        f" dashboard answer endpoint)\n"
        f"- actor: founder\n"
    )


async def _commit_decision_answer(
    home: Path, artifact_path: Path, dr: DecisionRequest
) -> str:
    """Commit the decision-answer artifact in the factory repo BEFORE the
    recording tx (D-0015): an uncommitted-but-registered factory-repo ref has
    no worktree, no commit and no HEAD blob to resolve against, so the §5.5c
    verify_integrity pass would abort the next orchestrator start while the
    unit is non-terminal. Git-side safety: the orchestrator commits workspace
    worktrees only, never the factory repo, so this cross-process git write
    races none of §7's; commit_paths scopes add+commit to the named path. A
    re-run after a failed recording tx finds the identical content already
    committed (commit_paths returns None) and pins the ref to current HEAD."""
    sha = await commit_paths(
        home,
        [artifact_path],
        f"decision {dr.id}: answer recorded via cli decide",
        trailers={"Factory-Unit": f"{dr.unit_level}/{dr.unit_id}"},
    )
    if sha is not None:
        return sha
    code, out, err = await run_git("rev-parse", "HEAD", cwd=home)
    if code != 0:
        raise GitError(f"git rev-parse HEAD failed in {home}: {(err or out).strip()}")
    return out.strip()


def cmd_decide(cfg: FactoryConfig, request_id: int, option: str) -> int:
    """Emergency decision-answer path (§4): artifact file first, committed to
    the factory repo (D-0015 — verify_integrity must resolve the ref at the
    next recover), then ONE transaction = register_artifact + answer_decision
    + event (§7 step order). No flock: the single short DB write from a second
    OS process is the D-0015-ratified emergency exception to the §2
    sole-writer rule (WAL + busy_timeout serialize it; busy database = explicit
    error, never a partial answer); the scheduler picks the answer up on its
    next tick."""
    answer = option.strip()
    if not answer:
        raise FactoryError("decide: empty answer — pass the chosen option text")
    db = _open_db(cfg)
    try:
        pending = pending_decisions(db.read())
        matches = [dr for dr in pending if dr.id == request_id]
        if not matches:
            pending_ids = sorted(dr.id for dr in pending if dr.id is not None)
            raise FactoryError(
                f"no PENDING decision request with id {request_id}"
                f" (pending ids: {pending_ids or 'none'})"
            )
        dr = matches[0]
        try:
            level = Level(dr.unit_level)
        except ValueError as exc:
            raise FactoryError(
                f"decision request {request_id} has non-unit level {dr.unit_level!r} —"
                " cannot derive an artifact dir"
            ) from exc

        home = cfg.factory.home
        unit_dir = unit_artifact_dir(home, level, dr.unit_id)
        unit_dir.mkdir(parents=True, exist_ok=True)
        answered_at = utc_now()
        artifact_path = unit_dir / f"decision-answer-{dr.id}.md"
        # §7 fixed step order: artifact file on disk, committed (D-0015),
        # BEFORE the recording tx.
        artifact_path.write_text(
            _render_decision_answer(dr, answer, answered_at), encoding="utf-8"
        )
        sha = asyncio.run(_commit_decision_answer(home, artifact_path, dr))
        try:
            with db.transaction() as conn:
                ref = register_artifact(
                    conn,
                    unit_level=dr.unit_level,
                    unit_id=dr.unit_id,
                    kind="decision_answer",
                    repo="factory",
                    repo_root=home,
                    path=artifact_path,
                    git_commit=sha,
                )
                answer_decision(conn, request_id, answer, ref.id)
                insert_event(
                    conn,
                    unit_level=dr.unit_level,
                    unit_id=dr.unit_id,
                    event_type="decision_answered",
                    actor="founder",
                    payload={
                        "request_id": request_id,
                        "answer": answer,
                        "answer_artifact_id": ref.id,
                        "via": "cli",
                    },
                )
        except sqlite3.OperationalError as exc:
            raise FactoryError(
                f"database busy (live orchestrator writing?) — answer not recorded,"
                f" retry: {exc}"
            ) from exc
    finally:
        db.close()
    print(
        f"answered decision {request_id} for {dr.unit_level}/{dr.unit_id}:"
        f" {answer!r} (artifact {artifact_path})"
    )
    return 0


# -------------------------------------------------------------------------- main


def main(argv: Sequence[str] | None = None) -> int:
    """Operator entry. init: validate config, create db + migrate, env sanity check.
    run / resume: FIRST acquire an exclusive flock on process.pid_file and hold it
    for the process lifetime — if held, or the recorded pid+cmdline is alive, abort
    with a clear message (a second instance would orphan-sweep the live instance's
    agents, double-write the DB behind busy_timeout, and mask watchdog death
    detection); only then recover() + run_forever() (resume: run_until_blocked()).
    status [--json] [--write]: render generated view from a READ-ONLY db connection
    (mode=ro — legitimately concurrent with a live orchestrator, §2) + git
    (non-canonical, Doctrine §9). decide <request_id> <option>: emergency-fallback
    answer path (DoD §9 plumbing; the expected founder path is the dashboard answer
    endpoint, §1)."""
    parser = argparse.ArgumentParser(
        prog="sf-factory",
        description="SF-F5 factory control plane — operator entry (design §4).",
    )
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=Path("factory.config.yaml"),
        help="path to factory.config.yaml (default: ./factory.config.yaml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="validate config, create db + migrate, env sanity check")
    sub.add_parser(
        "run",
        help="single-instance orchestrator: flock, recover, run_forever",
    )
    sub.add_parser(
        "resume",
        help="single-instance orchestrator: flock, recover, run_until_blocked",
    )
    p_status = sub.add_parser("status", help="generated view from a read-only db connection")
    p_status.add_argument("--json", action="store_true", dest="as_json", help="emit JSON")
    p_status.add_argument(
        "--write",
        action="store_true",
        help="also write the rendered view to <factory.home>/STATUS.md",
    )
    p_decide = sub.add_parser(
        "decide", help="emergency decision-answer path (expected path: dashboard)"
    )
    p_decide.add_argument("request_id", type=int, help="decision_requests.id to answer")
    p_decide.add_argument("option", help="the chosen option text (recorded verbatim)")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
        if args.command == "init":
            return cmd_init(cfg)
        if args.command == "run":
            return cmd_run(cfg, until_blocked=False)
        if args.command == "resume":
            return cmd_run(cfg, until_blocked=True)
        if args.command == "status":
            return cmd_status(cfg, as_json=args.as_json, write=args.write)
        if args.command == "decide":
            return cmd_decide(cfg, args.request_id, args.option)
        raise FactoryError(f"unknown command: {args.command!r}")  # unreachable
    except KeyboardInterrupt:
        print("sf-factory: interrupted — shutting down", file=sys.stderr)
        return 130
    except FactoryError as exc:
        print(f"sf-factory: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # python -m sf_factory.cli
    raise SystemExit(main())
