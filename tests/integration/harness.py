"""Wave-4 integration harness (design §8/§9: fixtures and helpers beyond the
frozen tests/conftest.py live locally in the owning wave's modules).

The harness wires the REAL object graph — Database, StateMachine, AgentRunner
(spawning real stub subprocesses), WorktreeManager (real git repos),
ThresholdEvaluator, Consultor — with exactly one test double: a recording
notify publisher (the §8 "ntfy stub"). Subprocess-orchestrator tests (A2)
drive ``python -m sf_factory.cli resume`` against a generated config file.

Determinism: every wait is a deadline-bounded poll (`poll`/`poll_async`);
every spawned orchestrator/child is killed in fixture/finally cleanup.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sf_factory import db as fdb
from sf_factory.config import FactoryConfig
from sf_factory.consultation import Consultor
from sf_factory.db import Database
from sf_factory.models import (
    Level,
    Phase,
    PhaseState,
    Stage,
    StageState,
    utc_now,
)
from sf_factory.runner import AgentRunner
from sf_factory.scheduler import PhaseExecutor, Scheduler, StageExecutor
from sf_factory.statemachine import StateMachine
from sf_factory.thresholds import ThresholdEvaluator
from sf_factory.worktrees import WorktreeManager

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVER_PATH = Path(__file__).resolve().parent / "agent_driver.py"
CANONICAL_STUB = REPO_ROOT / "tests" / "stub_agent.py"

#: Trivially-green / conditionally-green Tier-1 suite commands (OPEN-2 stub).
GREEN_SUITE = [sys.executable, "-c", "import sys; sys.exit(0)"]
MARKER_SUITE = [
    sys.executable,
    "-c",
    "import os, sys; sys.exit(0 if os.path.exists('suite-ok.marker') else 1)",
]


# ----------------------------------------------------------------- low-level


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def init_repo(path: Path) -> str:
    """git init -b main + seed commit; returns the seed commit sha."""
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "factory@test", cwd=path)
    git("config", "user.name", "factory", cwd=path)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", "seed", cwd=path)
    return git("rev-parse", "HEAD", cwd=path)


def commit_all(worktree: Path, message: str) -> str:
    git("add", "-A", cwd=worktree)
    git("commit", "-q", "-m", message, cwd=worktree)
    return git("rev-parse", "HEAD", cwd=worktree)


def poll(
    predicate: Callable[[], Any], *, timeout: float = 30.0, what: str = "condition"
) -> Any:
    """Deadline-bounded synchronous poll; returns the first truthy value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


async def poll_async(
    predicate: Callable[[], Any], *, timeout: float = 30.0, what: str = "condition"
) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_group_quiet(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# -------------------------------------------------------------- notify double


class RecordingNotify:
    """The §8 'ntfy stub': records publishes; never does network I/O."""

    priority_decision = "high"
    priority_alert = "max"

    def __init__(self) -> None:
        self.published: list[tuple[str, str | None, str]] = []

    async def publish(
        self, title: str, *, link: str | None = None, priority: str = "default"
    ) -> None:
        self.published.append((title, link, priority))


# ------------------------------------------------------------------- harness


@dataclass
class FactoryEnv:
    """One wired integration environment over a real workspace git repo."""

    cfg: FactoryConfig
    db: Database
    home: Path
    workspace: Path
    worktrees_dir: Path
    seed_commit: str
    playbook_path: Path
    notify: RecordingNotify
    #: Plain copy of the config data (the subprocess-orchestrator YAML source).
    config_data: dict = field(default_factory=dict)
    phase_id: str = "ph1"
    _cleanup_pids: list[int] = field(default_factory=list)
    _orchestrators: list[OrchestratorProc] = field(default_factory=list)

    # ---- object graph (real everything except notify) ----

    def stage_executor(self) -> StageExecutor:
        runner = AgentRunner(self.cfg, self.db)
        return StageExecutor(
            self.db,
            StateMachine(self.db),
            self.cfg,
            runner,
            WorktreeManager(self.cfg),
            ThresholdEvaluator(self.db, self.cfg),
            Consultor(self.cfg, self.db, runner),
            self.notify,
        )

    def scheduler(self) -> Scheduler:
        runner = AgentRunner(self.cfg, self.db)
        wt = WorktreeManager(self.cfg)
        sm = StateMachine(self.db)
        executors = {
            Level.STAGE: StageExecutor(
                self.db,
                sm,
                self.cfg,
                runner,
                wt,
                ThresholdEvaluator(self.db, self.cfg),
                Consultor(self.cfg, self.db, runner),
                self.notify,
            ),
            Level.PHASE: PhaseExecutor(self.db, sm, self.cfg, runner, wt, self.notify),
        }
        return Scheduler(self.db, sm, self.cfg, executors, self.notify)

    # ---- seeding ----

    @property
    def phase_branch(self) -> str:
        return f"phase/{self.phase_id}"

    @property
    def phase_checkout(self) -> Path:
        return self.worktrees_dir / self.phase_id

    def seed_phase(self, state: PhaseState = PhaseState.RUNNING) -> None:
        now = utc_now()
        with self.db.transaction() as conn:
            fdb.insert_phase(
                conn,
                Phase(
                    id=self.phase_id,
                    project="proj",
                    name=f"Phase {self.phase_id}",
                    state=state,
                    branch=self.phase_branch,
                    plan_artifact_id=None,
                    created_at=now,
                    updated_at=now,
                ),
            )

    def seed_freeze_event(self, commit: str | None = None) -> None:
        """The §3.1 contract-freeze anchor every stage MERGE_GATE reads."""
        with self.db.transaction() as conn:
            fdb.insert_event(
                conn,
                unit_level="phase",
                unit_id=self.phase_id,
                event_type="transition",
                actor="control_plane",
                from_state="PLANNING",
                to_state="CONTRACTS_FROZEN",
                payload={"contracts": 0, "commit": commit or self.seed_commit},
            )

    def seed_stage(
        self,
        stage_id: str,
        state: StageState,
        *,
        risk: str = "routine",
        worktree: Path | None = None,
    ) -> None:
        now = utc_now()
        with self.db.transaction() as conn:
            fdb.insert_stage(
                conn,
                Stage(
                    id=stage_id,
                    phase_id=self.phase_id,
                    name=f"Stage {stage_id}",
                    risk_class=risk,
                    state=state,
                    branch=f"stage/{stage_id}",
                    worktree_path=str(worktree) if worktree else None,
                    spec_artifact_id=None,
                    created_at=now,
                    updated_at=now,
                ),
            )

    def create_stage_worktree(self, stage_id: str) -> Path:
        """Manual stage worktree off the phase branch (mid-conveyor seeding)."""
        path = self.worktrees_dir / stage_id
        git(
            "worktree",
            "add",
            "-q",
            "-b",
            f"stage/{stage_id}",
            str(path),
            self.phase_branch,
            cwd=self.workspace,
        )
        return path

    def write_playbook(self, playbook: dict) -> None:
        self.playbook_path.write_text(json.dumps(playbook, indent=1), encoding="utf-8")

    # ---- DB readers ----

    def events(self, unit_id: str | None, event_type: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM events WHERE 1=1", []
        if unit_id is not None:
            sql += " AND unit_id = ?"
            params.append(unit_id)
        if event_type is not None:
            sql += " AND event_type = ?"
            params.append(event_type)
        rows = self.db.read().execute(sql + " ORDER BY seq", params).fetchall()
        return [dict(r) for r in rows]

    def transitions(self, unit_id: str) -> list[tuple[str | None, str | None]]:
        return [
            (e["from_state"], e["to_state"])
            for e in self.events(unit_id, "transition")
        ]

    def escalations(self, unit_id: str | None = None, status: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM escalations WHERE 1=1", []
        if unit_id is not None:
            sql += " AND unit_id = ?"
            params.append(unit_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        rows = self.db.read().execute(sql + " ORDER BY id", params).fetchall()
        return [dict(r) for r in rows]

    def processes(self, *, role: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM process_registry WHERE 1=1", []
        if role is not None:
            sql += " AND role = ?"
            params.append(role)
        rows = self.db.read().execute(sql + " ORDER BY id", params).fetchall()
        return [dict(r) for r in rows]

    def consultations(self) -> list[dict]:
        rows = self.db.read().execute("SELECT * FROM consultations ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def stage_state(self, stage_id: str) -> StageState:
        stage = fdb.get_stage(self.db.read(), stage_id)
        assert stage is not None
        return stage.state

    def findings(self, stage_id: str) -> list[dict]:
        rows = (
            self.db.read()
            .execute(
                "SELECT * FROM audit_findings WHERE stage_id = ? ORDER BY id", (stage_id,)
            )
            .fetchall()
        )
        return [dict(r) for r in rows]

    # ---- subprocess orchestrator (A2) ----

    def write_config_yaml(self) -> Path:
        """Materialize the config for `python -m sf_factory.cli` (JSON ⊂ YAML)."""
        path = self.home / "factory.config.yaml"
        path.write_text(json.dumps(self.config_data, indent=1), encoding="utf-8")
        return path

    def cli_init(self) -> None:
        """`sf-factory init` in-process: creates + migrates the config DB."""
        from sf_factory.cli import main as cli_main

        assert cli_main(["-c", str(self.write_config_yaml()), "init"]) == 0

    def spawn_orchestrator(self, command: str = "resume", *, log_name: str) -> OrchestratorProc:
        """Spawn `python -m sf_factory.cli <command>` as a real OS process
        (combined output to a log file — never an undrained pipe)."""
        cfg_path = self.write_config_yaml()
        log_path = self.home / f"{log_name}.log"
        with open(log_path, "wb") as log_file:
            proc = subprocess.Popen(
                [sys.executable, "-m", "sf_factory.cli", "-c", str(cfg_path), command],
                cwd=self.home,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
            )
        run = OrchestratorProc(proc=proc, log_path=log_path)
        self._orchestrators.append(run)
        return run

    # ---- cleanup ----

    def track_pid(self, pid: int) -> None:
        self._cleanup_pids.append(pid)

    def cleanup(self) -> None:
        for run in self._orchestrators:
            if run.proc.poll() is None:
                run.proc.kill()
                run.proc.wait()
        for pid in self._cleanup_pids:
            kill_group_quiet(pid)


@dataclass
class OrchestratorProc:
    """A live `sf-factory run/resume` subprocess + its combined output log."""

    proc: subprocess.Popen
    log_path: Path

    @property
    def pid(self) -> int:
        return self.proc.pid

    def sigkill(self) -> None:
        """SIGKILL the orchestrator itself (its agent children die via the
        runner's PR_SET_PDEATHSIG backstop; grandchildren may survive — that
        is exactly what the §5.5a orphan sweep is for)."""
        self.proc.kill()
        self.proc.wait()

    def wait(self, timeout: float = 120.0) -> int:
        return self.proc.wait(timeout=timeout)

    @property
    def output(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")


def _write_canon_files(home: Path) -> None:
    """Canon files (D-0009) at the paths the frozen conftest config declares."""
    (home / "00 - DOCTRINA.md").write_text("doctrine body (test canon)\n", encoding="utf-8")
    protocols = home / "work-protocols"
    protocols.mkdir(parents=True, exist_ok=True)
    (protocols / "conventions.md").write_text("conventions body\n", encoding="utf-8")
    (protocols / "protocol_interactiune_founder.md").write_text(
        "founder protocol body\n", encoding="utf-8"
    )
    (protocols / "architect-operations.md").write_text(  # D-0040
        "architect operations body\n", encoding="utf-8"
    )
