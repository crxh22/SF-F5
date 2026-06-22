"""Operator entry (design §4): ``init`` / ``run`` / ``resume`` / ``seed-phases`` /
``status`` / ``decide`` / ``resolve-escalation``.

Command contracts (design §4 ``cli.main`` docstring, implemented exactly; the
``seed-phases`` subcommand and the ``_InstanceLock.acquire(claim=...)`` flock
narrative are the phase-seeding design CCR-5 amendments, D-0024):

- ``init`` — validate config, create the DB + apply migrations, environment
  sanity check (git, model-route CLIs, canon files, operational dirs).
- ``seed-phases <plan.json> [--dry-run]`` — THE sanctioned phase-creation path
  (phase-seeding design §2.3): claim-free flock on the run/resume pidfile inode
  (mutual exclusion without touching pidfile bytes/mtime — a seeder must never
  grant the watchdog's freshness grace or record itself as the orchestrator),
  strict validation (macro plan, DB DAG, workspace bootstrap, committed plan),
  then ONE transaction inserting phases + dag_edges + exactly one factory-level
  ``macro_plan`` ref + one ``phase_seeded`` event per phase.
- ``run`` / ``resume`` — FIRST acquire an exclusive ``flock`` on
  ``process.pid_file`` and hold it for the process lifetime; if it is held, or
  the recorded pid+cmdline is alive, abort with a clear message (a second
  instance would orphan-sweep the live instance's agents, double-write the DB
  behind busy_timeout, and mask watchdog death detection). Only then
  ``Scheduler.recover()`` + ``run_forever()`` (``resume``: ``run_until_blocked()``).
  ``run`` also constructs the dashboard (CCR-4, dashboard design §1) and calls
  ``DashboardServer.start()`` EAGERLY before recovery: a first resolve/bind
  failure aborts orchestrator start in the foreground, never inside the
  supervised restart loop. ``resume`` runs with ``dashboard=None``.
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
- ``resolve-escalation <escalation_id> <resolution> [--reason <text>]`` —
  operator escalation disposition (dashboard design §10.6, CCR-7/D-0027 —
  the D-0026 gap c): vocabulary single-sourced from
  ``models.STAGE_ESCALATION_RESOLUTIONS`` / ``PHASE_ESCALATION_RESOLUTIONS``
  by the escalation's unit level; exactly ONE short transaction =
  ``db.resolve_escalation`` (frozen ``WHERE status='open'`` guard) + one
  ``escalation_resolved`` event (actor ``main_architect``, payload
  resolution/reason/via='cli'). Works against a live orchestrator under the
  same D-0015 second-OS-process bounds (extended by the D-0027 rider); busy
  database = explicit error, zero partial state. No artifact requirement —
  an architect disposition is operational control-flow, not founder canon.

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
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from sf_factory.artifacts import (
    MacroPlan,
    read_macro_plan,
    register_artifact,
    sha256_file,
    unit_artifact_dir,
)
from sf_factory.config import FactoryConfig, load_config
from sf_factory.db import (
    MIGRATIONS_DIR,
    Database,
    answer_decision,
    insert_dag_edge,
    insert_event,
    insert_phase,
    latest_artifact,
    list_dag_edges,
    list_units,
    pending_decisions,
    processes_in_state,
    resolve_escalation,
    unit_token_total,
)
from sf_factory.models import (
    PHASE_ESCALATION_RESOLUTIONS,
    PHASE_NOACTION_RESOLUTION,
    STAGE_ESCALATION_RESOLUTIONS,
    STAGE_NOACTION_RESOLUTION,
    DecisionRequest,
    FactoryError,
    GitError,
    Level,
    Phase,
    PhaseState,
    Stage,
    utc_now,
)
from sf_factory.worktrees import commit_paths, run_git

if TYPE_CHECKING:  # import cycle-free typing only; runtime import is lazy (§9 lanes)
    from sf_factory.dashboard import DashboardServer
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

    ``claim=False`` (phase-seeding design §2.3.1, ``seed-phases``): take the
    SAME exclusive flock on the SAME inode (mutual exclusion with run/resume)
    but SKIP the pidfile truncate/write/fsync entirely — bytes AND mtime stay
    untouched. A short-lived seeder must never (a) reset the pidfile mtime,
    which grants the watchdog's freshness grace and can silence an
    actively-paging watchdog for up to staleness_threshold_s while the
    orchestrator is down, nor (b) record itself as "the orchestrator" for
    ``cli status`` and the next ``run``'s pid-liveness refusal.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self, *, claim: bool = True) -> None:
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
            if claim:
                # Both checks passed: claim the file (pidfile contract, watchdog
                # reads it). claim=False holds the flock only — content untouched.
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


def _build_scheduler(
    cfg: FactoryConfig, db: Database, *, with_dashboard: bool
) -> tuple[Scheduler, DashboardServer | None]:
    """Wire the §4 object graph with the FROZEN constructor signatures.

    Imports live here, not at module top: only run/resume need the graph, and
    wave-3 lanes are file-disjoint (design §9) — init/status/decide must not
    depend on sibling-lane modules being importable.

    ``with_dashboard`` (CCR-4, dashboard design §1/§6): ``run`` constructs
    ``DashboardServer(cfg, db, runner, notify)`` and hands it to the Scheduler
    (CCR-3 kwarg — ``run_forever`` hosts the supervised serve() task);
    ``resume`` keeps ``dashboard=None`` (``run_until_blocked`` is a bounded
    catch-up loop, not the founder-facing steady state).
    """
    from sf_factory.consultation import Consultor
    from sf_factory.dashboard import DashboardServer
    from sf_factory.notify import NtfyPublisher
    from sf_factory.runner import AgentRunner
    from sf_factory.scheduler import (
        CapacityGovernor,
        PhaseExecutor,
        Scheduler,
        StageExecutor,
    )
    from sf_factory.statemachine import StateMachine
    from sf_factory.thresholds import ThresholdEvaluator
    from sf_factory.worktrees import WorktreeManager

    sm = StateMachine(db)
    runner = AgentRunner(cfg, db)
    wt = WorktreeManager(cfg)
    thresholds = ThresholdEvaluator(db, cfg)
    consultor = Consultor(cfg, db, runner)
    notify = NtfyPublisher(cfg)
    # CCR-11 (D-0037): ONE shared capacity governor — a hold entered by either
    # executor gates both, and the Scheduler loop runs its hold-exit probe.
    governor = CapacityGovernor(db, cfg, runner, notify)
    executors = {
        Level.STAGE: StageExecutor(
            db, sm, cfg, runner, wt, thresholds, consultor, notify, governor=governor
        ),
        Level.PHASE: PhaseExecutor(db, sm, cfg, runner, wt, notify, governor=governor),
    }
    dashboard = DashboardServer(cfg, db, runner, notify) if with_dashboard else None
    return (
        Scheduler(
            db, sm, cfg, executors, notify, dashboard=dashboard, governor=governor
        ),
        dashboard,
    )


def cmd_run(cfg: FactoryConfig, *, until_blocked: bool) -> int:
    """``run`` (run_forever) / ``resume`` (run_until_blocked) per §4: flock FIRST,
    then (run only) the eager dashboard bind — dashboard design §1: a bind failure
    aborts start in the foreground — then recover, then the loop. The lock is held
    for the whole process lifetime."""
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
            scheduler, dashboard = _build_scheduler(cfg, db, with_dashboard=not until_blocked)
            if dashboard is not None:
                # Eager first bind (dashboard design §1): a resolve/bind failure
                # is a foreground FactoryError abort BEFORE recovery — never
                # deferred into the §7 supervised restart loop, which would run
                # the pipeline founder-less behind a retry cycle.
                dashboard.start()
                if dashboard.bound_address is not None:
                    host, port = dashboard.bound_address
                    print(
                        f"sf-factory: dashboard bound on http://{host}:{port}/",
                        file=sys.stderr,
                    )
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


# ------------------------------------------------------------------- seed-phases

#: The operator bootstrap runbook every workspace-precondition abort points at.
_SEED_RUNBOOK = "docs/runbooks/first-live-run.md"


def _seed_git(repo: Path, *args: str) -> tuple[int, str, str]:
    """Synchronous read-only git call for the seed-phases preconditions;
    returns (exit_code, stdout, stderr) stripped — never raises on nonzero exit."""
    try:
        proc = subprocess.run(  # noqa: S603 — fixed git argv, read-only queries
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FactoryError(f"seed-phases: git {' '.join(args)} failed in {repo}: {exc}") from exc
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _seed_head(repo: Path) -> str:
    code, head, err = _seed_git(repo, "rev-parse", "HEAD")
    if code != 0:
        raise FactoryError(
            f"seed-phases: git rev-parse HEAD failed in the factory repo {repo}: {err or head}"
        )
    return head


def _seed_replay_or_divergence(
    conn: sqlite3.Connection,
    home: Path,
    plan: MacroPlan,
    plan_path: Path,
    rel_posix: str,
    existing_ids: set[str],
) -> str:
    """Idempotent-replay rule (design §2.3.2): some plan id already exists.
    EVERY id existing AND the registered macro_plan ref matching this file's
    (path, sha256, git_commit) = crash replay → return the exit-0 message;
    any divergence → FactoryError naming the differing ids / ref mismatch."""
    plan_ids = [mp.id for mp in plan.phases]
    overlap = [pid for pid in plan_ids if pid in existing_ids]
    new_ids = [pid for pid in plan_ids if pid not in existing_ids]
    if new_ids:
        raise FactoryError(
            f"seed-phases: phase id(s) {overlap} already exist in the DB while "
            f"{new_ids} are new — divergent plan; refusing the partial overlap"
        )
    ref = latest_artifact(conn, "factory", plan.project, "macro_plan")
    digest = sha256_file(plan_path)
    head = _seed_head(home)
    if (
        ref is not None
        and ref.path == rel_posix
        and ref.sha256 == digest
        and ref.git_commit == head
    ):
        return f"already seeded at {ref.created_at} — nothing to do"
    recorded = (
        f"recorded (path={ref.path}, sha256={ref.sha256[:12]}…, git_commit={ref.git_commit})"
        if ref is not None
        else "no macro_plan ref is registered"
    )
    raise FactoryError(
        f"seed-phases: phase id(s) {sorted(overlap)} already exist but the registered "
        f"macro_plan ref does not match this file's (path={rel_posix}, "
        f"sha256={digest[:12]}…, git_commit={head}) — {recorded} — divergent plan"
    )


def _seed_check_edges(
    conn: sqlite3.Connection, plan: MacroPlan, existing: dict[str, Phase]
) -> list[tuple[str, str]]:
    """§2.3.2 edge rules: endpoint ∈ plan ∪ DB; DB-resolving endpoints not in a
    dead state; edge not already in the DB; combined graph acyclic. Returns the
    existing DB edges (also the duplicate-edge probe input)."""
    plan_ids = {mp.id for mp in plan.phases}
    known = plan_ids | set(existing)
    dead = {PhaseState.FAILED, PhaseState.CANCELLED}
    db_edges = list_dag_edges(conn, Level.PHASE)
    db_edge_set = set(db_edges)
    for from_id, to_id in plan.dag_edges:
        for endpoint in (from_id, to_id):
            if endpoint not in known:
                raise FactoryError(
                    f"seed-phases: dag edge {from_id} -> {to_id} references unknown "
                    f"phase {endpoint!r} (neither in the plan nor in the DB)"
                )
            unit = existing.get(endpoint)
            if unit is not None and unit.state in dead:
                raise FactoryError(
                    f"seed-phases: dag edge {from_id} -> {to_id} references phase "
                    f"{endpoint!r} in dead state {unit.state.value} — deps_done "
                    "requires DONE, so a dead prerequisite seeds a "
                    "permanently-WAITING unit"
                )
        if (from_id, to_id) in db_edge_set:
            raise FactoryError(
                f"seed-phases: dag edge {from_id} -> {to_id} already exists in the DB"
            )
    _assert_combined_acyclic(plan.dag_edges, db_edges, known)
    return db_edges


def _assert_combined_acyclic(
    plan_edges: Sequence[tuple[str, str]],
    db_edges: Sequence[tuple[str, str]],
    nodes: set[str],
) -> None:
    """Kahn toposort over existing ∪ plan edges; remainder = cycle (§2.3.2 —
    named abort, the plan-local check in read_macro_plan covers only the plan)."""
    edges = [*db_edges, *plan_edges]
    all_nodes = set(nodes)
    for from_id, to_id in edges:
        all_nodes.add(from_id)
        all_nodes.add(to_id)
    indegree: dict[str, int] = dict.fromkeys(all_nodes, 0)
    adjacency: dict[str, list[str]] = {node: [] for node in all_nodes}
    for from_id, to_id in edges:
        adjacency[from_id].append(to_id)
        indegree[to_id] += 1
    ready = sorted(node for node, deg in indegree.items() if deg == 0)
    processed = 0
    while ready:
        node = ready.pop(0)
        processed += 1
        for dependent in adjacency[node]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
        ready.sort()
    if processed != len(all_nodes):
        cyclic = sorted(node for node, deg in indegree.items() if deg > 0)
        raise FactoryError(
            "seed-phases: combined phase DAG (existing ∪ plan edges) would be "
            f"cyclic (phases in/behind a cycle: {cyclic}) — nothing written"
        )


def _seed_workspace_preconditions(cfg: FactoryConfig, project_id: str) -> None:
    """§2.3.3 fail-early workspace preconditions (not at the first gate, after
    the full SPEC/BUILD/VALIDATE token spend); each abort points at the
    bootstrap runbook."""
    project = cfg.projects[project_id]
    home = cfg.factory.home
    workspace = _resolve(home, project.workspace)
    hint = f"bootstrap the workspace first ({_SEED_RUNBOOK})"
    if not workspace.is_dir():
        raise FactoryError(f"seed-phases: workspace {workspace} does not exist — {hint}")
    # The workspace must be its OWN repo root: a bare --is-inside-work-tree
    # probe walks up and false-passes a plain dir nested inside another repo.
    code, toplevel, _ = _seed_git(workspace, "rev-parse", "--show-toplevel")
    if code != 0 or Path(toplevel).resolve() != workspace.resolve():
        raise FactoryError(
            f"seed-phases: workspace {workspace} is not a git repository"
            f" (its own repo root) — {hint}"
        )
    branch = project.integration_branch
    code, _, _ = _seed_git(workspace, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if code != 0:
        raise FactoryError(
            f"seed-phases: workspace {workspace} has no integration branch {branch!r} — {hint}"
        )
    if project.test_command is None:
        raise FactoryError(
            f"seed-phases: projects.{project_id}.test_command is unset (OPEN-2) — a null "
            "command otherwise dies as ConfigError at the first MERGE_GATE, after the "
            f"full stage token spend; set it before seeding ({_SEED_RUNBOOK})"
        )
    argv = (
        shlex.split(project.test_command)
        if isinstance(project.test_command, str)
        else [str(part) for part in project.test_command]
    )
    for token in argv:
        # Workspace-relative script reference = a relative token with a path
        # separator (the runbook's `scripts/test.sh` shape). Options, bare
        # command names and absolute paths are not workspace files.
        if token.startswith(("-", "/")) or "/" not in token:
            continue
        rel = str(PurePosixPath(token))  # normalizes a leading './'
        candidate = workspace / rel
        if not candidate.is_file():
            raise FactoryError(
                f"seed-phases: test_command references workspace-relative script "
                f"{token!r} which does not exist at {candidate} — {hint}"
            )
        code, _, _ = _seed_git(workspace, "cat-file", "-e", f"{branch}:{rel}")
        if code != 0:
            raise FactoryError(
                f"seed-phases: test_command script {token!r} exists but is not "
                f"committed on {branch!r} in {workspace} — {hint}"
            )
    code, out, _ = _seed_git(
        workspace, "ls-tree", "-r", "--name-only", branch, "--", "_factory/contracts"
    )
    if code != 0 or not out.strip():
        raise FactoryError(
            f"seed-phases: _factory/contracts/ is empty or missing on {branch!r} in "
            f"{workspace} — an empty contracts dir would make every Tier-2 gate "
            f"validate against nothing (D-0022); {hint}"
        )


def _seed_plan_precondition(home: Path, rel_posix: str) -> str:
    """§2.3.4 committed-plan precondition in the FACTORY repo: tracked
    (`git ls-files --error-unmatch` — porcelain-empty alone false-passes a
    gitignored file, whose blob would resolve at no commit and poison the next
    recover()), unmodified, anchored at HEAD. Returns the anchor sha."""
    code, _, _ = _seed_git(home, "ls-files", "--error-unmatch", "--", rel_posix)
    if code != 0:
        raise FactoryError(
            f"seed-phases: plan file {rel_posix} is not tracked in the factory repo "
            f"{home} (untracked or gitignored) — commit it first"
        )
    code, out, err = _seed_git(home, "status", "--porcelain", "--", rel_posix)
    if code != 0:
        raise FactoryError(
            f"seed-phases: git status failed for {rel_posix} in the factory repo "
            f"{home}: {err or out}"
        )
    if out.strip():
        raise FactoryError(
            f"seed-phases: plan file {rel_posix} has uncommitted changes in the "
            f"factory repo {home} — commit them first (the macro_plan ref anchors at HEAD)"
        )
    return _seed_head(home)


def cmd_seed_phases(cfg: FactoryConfig, plan_arg: Path, *, dry_run: bool) -> int:
    """THE sanctioned phase-creation path (phase-seeding design §2.3, D-0024):
    claim-free flock guard → read_macro_plan → DB checks (single-project guard,
    all-new ids / idempotent replay, endpoint resolution plan ∪ DB,
    dead-prerequisite + duplicate-edge + combined-cycle aborts) → workspace
    preconditions → committed-plan precondition (anchor = factory HEAD) → ONE
    transaction: phases (PENDING, branch/plan_artifact_id NULL) + phase
    dag_edges + exactly ONE factory-level macro_plan ref + one phase_seeded
    event per phase. ``--dry-run`` runs all validation, prints the would-be
    inserts, writes nothing. Exit nonzero on any precondition failure; zero
    writes on failure (single tx)."""
    home = Path(cfg.factory.home).resolve()
    plan_path = plan_arg if plan_arg.is_absolute() else Path.cwd() / plan_arg
    plan_path = plan_path.resolve()
    try:
        rel_posix = plan_path.relative_to(home).as_posix()
    except ValueError:
        raise FactoryError(
            f"seed-phases: plan {plan_path} is not inside the factory repo {home} — "
            "the committed-plan precondition anchors the macro_plan ref there"
        ) from None

    # §2.3.1 exclusive-instance guard: same flock inode as run/resume, but
    # claim-free — a seeder never rewrites pidfile bytes or mtime.
    lock = _InstanceLock(_resolve(cfg.factory.home, cfg.process.pid_file))
    try:
        lock.acquire(claim=False)
    except FactoryError as exc:
        raise FactoryError(
            "orchestrator running — stop it first (runbook: seed only while stopped)"
        ) from exc
    try:
        plan = read_macro_plan(plan_path, projects=set(cfg.projects))
        db = _open_db(cfg)
        try:
            conn = db.read()
            phases = [p for p in list_units(conn, Level.PHASE) if isinstance(p, Phase)]
            # §2.3.2 single-project guard (D-0022 item 3 / D-0023).
            foreign = sorted({p.project for p in phases} - {plan.project})
            if foreign:
                raise FactoryError(
                    f"seed-phases: the DB already contains phases of project(s) "
                    f"{foreign} — seeding project {plan.project!r} alongside them is "
                    "refused: the MVP posture is fresh-DB-per-project (D-0022 item 3; "
                    "finished DBs are archived, D-0023). A second project in a live DB "
                    "would make every subsequent recover() abort at _repo_roots."
                )
            existing = {p.id: p for p in phases}
            if any(mp.id in existing for mp in plan.phases):
                message = _seed_replay_or_divergence(
                    conn, home, plan, plan_path, rel_posix, set(existing)
                )
                print(message)
                return 0
            _seed_check_edges(conn, plan, existing)
            _seed_workspace_preconditions(cfg, plan.project)
            anchor = _seed_plan_precondition(home, rel_posix)

            plan_ids = [mp.id for mp in plan.phases]
            edge_str = (
                ", ".join(f"{f} -> {t}" for f, t in plan.dag_edges)
                if plan.dag_edges
                else "none"
            )
            if dry_run:
                digest = sha256_file(plan_path)
                print(
                    f"dry-run: would seed {len(plan.phases)} phase(s) for project "
                    f"{plan.project}: "
                    + ", ".join(f"{mp.id} ({mp.name})" for mp in plan.phases)
                )
                print(f"dry-run: would insert phase dag edge(s): {edge_str}")
                print(
                    f"dry-run: would register ONE macro_plan ref (repo=factory, "
                    f"path={rel_posix}, sha256={digest[:12]}…, git_commit={anchor}) "
                    f"+ {len(plan.phases)} phase_seeded event(s)"
                )
                print(f"dry-run: anchor commit {anchor} — nothing written")
                return 0

            now = utc_now()
            with db.transaction() as tx:
                for mp in plan.phases:
                    insert_phase(
                        tx,
                        Phase(
                            id=mp.id,
                            project=plan.project,
                            name=mp.name,
                            state=PhaseState.PENDING,
                            branch=None,  # dispatch derives phase/<id>
                            plan_artifact_id=None,  # consistent with _step_planning
                            created_at=now,
                            updated_at=now,
                        ),
                    )
                for from_id, to_id in plan.dag_edges:
                    insert_dag_edge(tx, Level.PHASE, from_id, to_id)
                ref = register_artifact(
                    tx,
                    unit_level="factory",
                    unit_id=plan.project,
                    kind="macro_plan",
                    repo="factory",
                    repo_root=home,
                    path=plan_path,
                    git_commit=anchor,
                )
                for mp in plan.phases:
                    insert_event(
                        tx,
                        unit_level=Level.PHASE.value,
                        unit_id=mp.id,
                        event_type="phase_seeded",
                        actor="main_architect",
                        payload={
                            "plan": rel_posix,
                            "anchor": anchor,
                            "macro_plan_ref": ref.id,
                        },
                    )
            print(
                f"seeded {len(plan.phases)} phase(s) [{', '.join(plan_ids)}] and "
                f"{len(plan.dag_edges)} dag edge(s) [{edge_str}] for project {plan.project}"
            )
            print(
                f"macro_plan ref {ref.id} (path {rel_posix}) anchored at factory "
                f"commit {anchor}"
            )
            return 0
        finally:
            db.close()
    finally:
        lock.release()


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

    # Proving-phases dispatch hold (phase-seeding design §5b): the held state is
    # the scheduler's pure predicate — imported lazily so init/status/decide
    # keep their module-level independence from the run/resume object graph.
    from sf_factory.scheduler import proving_held_phase_ids

    held = proving_held_phase_ids(cfg, phases)

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
            "held": "proving" if phase.id in held else None,
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
        # Phase-seeding design §5b: the dispatch hold is visible, never silent.
        held = ", held: proving" if phase.get("held") == "proving" else ""
        lines.append(
            f"### {phase['id']} — {phase['state']}{held} (project {phase['project']},"
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


# -------------------------------------------------------------- resolve-escalation


def cmd_resolve_escalation(
    cfg: FactoryConfig, escalation_id: int, resolution: str, reason: str | None
) -> int:
    """Operator escalation disposition (§4 / dashboard design §10.6 — the
    D-0026 gap c, CCR-7): validate the escalation is OPEN and the resolution
    belongs to the ``models.*_ESCALATION_RESOLUTIONS`` vocabulary of its unit
    level (unknown values rejected listing the valid set), then exactly ONE
    short transaction = ``db.resolve_escalation`` (frozen ``WHERE
    status='open'`` guard; rowcount≠1 → explicit error, the tx rolls back
    whole) + one ``escalation_resolved`` event (actor='main_architect',
    payload: resolution, reason, via='cli'). No flock and no artifact: the
    single short DB write from a second OS process runs under the
    D-0015-ratified emergency-exception bounds, extended to this command by
    the D-0027 rider (WAL + busy_timeout serialize it; busy database =
    explicit error, never partial state); an architect disposition is
    operational control-flow, not founder canon — the rationale lives in the
    event payload. The running orchestrator's ``_step_escalated`` consumes the
    resolution on its next tick (existing mechanics, untouched)."""
    token = resolution.strip()
    if not token:
        raise FactoryError(
            "resolve-escalation: empty resolution — pass the disposition token"
        )
    db = _open_db(cfg)
    try:
        open_rows = _open_escalations(db.read())
        matches = [row for row in open_rows if row["id"] == escalation_id]
        if not matches:
            open_ids = sorted(row["id"] for row in open_rows)
            raise FactoryError(
                f"no OPEN escalation with id {escalation_id}"
                f" (open ids: {open_ids or 'none'})"
            )
        esc_row = matches[0]
        try:
            level = Level(esc_row["unit_level"])
        except ValueError as exc:
            raise FactoryError(
                f"escalation {escalation_id} has non-unit level"
                f" {esc_row['unit_level']!r} — no resolution vocabulary applies"
            ) from exc
        # `settled` (the no-action disposition) is special-cased in the scheduler
        # at BOTH levels (not a static *_ESCALATION_RESOLUTIONS map key), so it is
        # admitted here per level but lives outside those maps: a STAGE settles
        # contested audit findings; a PHASE accepts an accurate Tier-2 integration
        # finding and routes to sign-off (architect-operations.md §1, D-0062).
        vocabulary: set[str] = (
            set(STAGE_ESCALATION_RESOLUTIONS) | {STAGE_NOACTION_RESOLUTION}
            if level is Level.STAGE
            else set(PHASE_ESCALATION_RESOLUTIONS) | {PHASE_NOACTION_RESOLUTION}
        )
        if token not in vocabulary:
            raise FactoryError(
                f"unknown resolution {token!r} for a {level.value} escalation —"
                f" valid: {', '.join(sorted(vocabulary))}"
            )
        try:
            with db.transaction() as conn:
                resolve_escalation(conn, escalation_id, token)
                insert_event(
                    conn,
                    unit_level=esc_row["unit_level"],
                    unit_id=esc_row["unit_id"],
                    event_type="escalation_resolved",
                    actor="main_architect",
                    payload={
                        "escalation_id": escalation_id,
                        "resolution": token,
                        "reason": reason,
                        "via": "cli",
                    },
                )
        except sqlite3.OperationalError as exc:
            raise FactoryError(
                f"database busy (live orchestrator writing?) — escalation not"
                f" resolved, retry: {exc}"
            ) from exc
    finally:
        db.close()
    print(
        f"resolved escalation {escalation_id} for"
        f" {esc_row['unit_level']}/{esc_row['unit_id']}: {token!r}"
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
    seed-phases <plan.json> [--dry-run]: THE sanctioned phase-creation path
    (phase-seeding design §2.3, D-0024) — claim-free flock on the same pidfile
    inode (mutual exclusion with run/resume; pidfile bytes/mtime untouched),
    strict macro-plan + DB + workspace + committed-plan validation, then ONE
    transaction seeding phases + DAG + the factory-level macro_plan ref + events.
    status [--json] [--write]: render generated view from a READ-ONLY db connection
    (mode=ro — legitimately concurrent with a live orchestrator, §2) + git
    (non-canonical, Doctrine §9). decide <request_id> <option>: emergency-fallback
    answer path (DoD §9 plumbing; the expected founder path is the dashboard answer
    endpoint, §1). resolve-escalation <escalation_id> <resolution> [--reason]:
    architect escalation disposition (dashboard design §10.6, CCR-7/D-0027) — one
    short tx (resolve + escalation_resolved event) under the D-0015 bounds."""
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
    p_seed = sub.add_parser(
        "seed-phases",
        help="seed a ratified, committed macro plan into phases + DAG"
        " (the sanctioned path; orchestrator must be stopped)",
    )
    p_seed.add_argument(
        "plan", type=Path, help="path to the committed macro-plan.json in the factory repo"
    )
    p_seed.add_argument(
        "--dry-run",
        action="store_true",
        help="run all validation, print the would-be inserts, write nothing",
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
    p_resolve = sub.add_parser(
        "resolve-escalation",
        help="architect escalation disposition: one tx = resolve + event"
        " (dashboard design §10.6, CCR-7/D-0027)",
    )
    p_resolve.add_argument(
        "escalation_id", type=int, help="escalations.id to resolve (must be open)"
    )
    p_resolve.add_argument(
        "resolution",
        help="disposition token from models.STAGE_/PHASE_ESCALATION_RESOLUTIONS"
        " for the escalation's unit level",
    )
    p_resolve.add_argument(
        "--reason",
        default=None,
        help="short operator rationale, recorded in the event payload",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
        if args.command == "init":
            return cmd_init(cfg)
        if args.command == "run":
            return cmd_run(cfg, until_blocked=False)
        if args.command == "resume":
            return cmd_run(cfg, until_blocked=True)
        if args.command == "seed-phases":
            return cmd_seed_phases(cfg, args.plan, dry_run=args.dry_run)
        if args.command == "status":
            return cmd_status(cfg, as_json=args.as_json, write=args.write)
        if args.command == "decide":
            return cmd_decide(cfg, args.request_id, args.option)
        if args.command == "resolve-escalation":
            return cmd_resolve_escalation(
                cfg, args.escalation_id, args.resolution, args.reason
            )
        raise FactoryError(f"unknown command: {args.command!r}")  # unreachable
    except KeyboardInterrupt:
        print("sf-factory: interrupted — shutting down", file=sys.stderr)
        return 130
    except FactoryError as exc:
        print(f"sf-factory: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # python -m sf_factory.cli
    raise SystemExit(main())
