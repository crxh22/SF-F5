"""Unit tests for sf_factory.scheduler (design §8 scheduler list): DAG ordering,
parallel cap, level-agnosticism (the SAME loop and the SAME fake-executor class
drive a fake phase + fake stages), stall detector, phase reaction to a child
stage entering FAILED — plus recover() §5.5 steps a–d, the §3.1
thresholds-first-then-CP-1 routing, CP-1 verdict execution (continue_session
resume + verdict_downgraded), Validator-isolation assertion, decision-latency
alerts and §6 executor-boundary containment.

Fixtures beyond the frozen conftest are defined locally (design §9).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sf_factory import db as fdb
from sf_factory import scheduler as sched_mod
from sf_factory.config import FactoryConfig
from sf_factory.models import (
    ConfigError,
    DecisionRequest,
    Escalation,
    IntegrityError,
    Level,
    NotifyError,
    Phase,
    PhaseState,
    ProcessRecord,
    Stage,
    StageState,
    Trigger,
    utc_now,
)
from sf_factory.runner import AgentResult
from sf_factory.scheduler import (
    PhaseExecutor,
    RecoveryReport,
    Scheduler,
    StageExecutor,
    UnitExecutor,
)
from sf_factory.statemachine import StateMachine
from sf_factory.thresholds import ThresholdEvaluator

# --------------------------------------------------------------------- helpers


def make_config(config_dict: dict[str, Any], **process_overrides: Any) -> FactoryConfig:
    config_dict["process"].update({"loop_tick_s": 0.01, **process_overrides})
    return FactoryConfig.model_validate(config_dict)


def insert_phase(
    db,
    phase_id: str,
    state: PhaseState = PhaseState.RUNNING,
    *,
    project: str = "proj",
    branch: str | None = None,
) -> None:
    now = utc_now()
    with db.transaction() as conn:
        fdb.insert_phase(
            conn,
            Phase(
                id=phase_id,
                project=project,
                name=f"Phase {phase_id}",
                state=state,
                branch=branch or f"phase/{phase_id}",
                plan_artifact_id=None,
                created_at=now,
                updated_at=now,
            ),
        )


def insert_stage(
    db,
    stage_id: str,
    phase_id: str,
    state: StageState,
    *,
    risk: str = "routine",
    worktree: Path | None = None,
) -> None:
    now = utc_now()
    with db.transaction() as conn:
        fdb.insert_stage(
            conn,
            Stage(
                id=stage_id,
                phase_id=phase_id,
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


def init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "factory@test"],
        ["git", "config", "user.name", "factory"],
    ):
        subprocess.run(args, cwd=path, check=True, capture_output=True)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"], cwd=path, check=True, capture_output=True
    )


def events_of(db, unit_id: str, event_type: str | None = None) -> list[dict]:
    sql = "SELECT * FROM events WHERE unit_id = ?"
    params: list[Any] = [unit_id]
    if event_type:
        sql += " AND event_type = ?"
        params.append(event_type)
    rows = db.read().execute(sql + " ORDER BY seq", params).fetchall()
    return [dict(row) for row in rows]


def transitions_of(db, unit_id: str) -> list[tuple[str | None, str | None]]:
    return [(e["from_state"], e["to_state"]) for e in events_of(db, unit_id, "transition")]


def open_escalations(db, unit_id: str) -> list[dict]:
    rows = (
        db.read()
        .execute(
            "SELECT * FROM escalations WHERE unit_id = ? AND status='open' ORDER BY id",
            (unit_id,),
        )
        .fetchall()
    )
    return [dict(row) for row in rows]


def stage_state(db, stage_id: str) -> StageState:
    stage = fdb.get_stage(db.read(), stage_id)
    assert stage is not None
    return stage.state


def phase_state(db, phase_id: str) -> PhaseState:
    phase = fdb.get_phase(db.read(), phase_id)
    assert phase is not None
    return phase.state


# ----------------------------------------------------------------------- fakes


class FakeNotify:
    priority_decision = "high"
    priority_alert = "max"

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, str | None, str]] = []
        self.fail = fail

    async def publish(self, title: str, *, link: str | None = None, priority: str = "default"):
        if self.fail:
            raise NotifyError("ntfy down (fake)")
        self.published.append((title, link, priority))


@dataclass
class FakeVerdict:
    value: str
    rationale: str = "cited rationale"
    fallback_used: bool = False
    consultation_id: int = 1
    cp_id: str = "CP-1"


class FakeConsultor:
    def __init__(self, verdicts: list[str]) -> None:
        self.verdicts = list(verdicts)
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def consult(self, cp_id: str, *, unit_level: str, unit_id: str, inputs):
        self.calls.append((cp_id, dict(inputs)))
        assert self.verdicts, "unexpected CP-1 consultation"
        return FakeVerdict(self.verdicts.pop(0))


class FakeRunner:
    """Role-scripted stand-in for AgentRunner: behaviors[role] gets (cwd,
    unit_id, resume_session) and writes whatever files the step expects.
    With ``db`` set it also writes the §5.1 'spawn' event like the real
    runner (the context-reset consumption rule reads it)."""

    def __init__(self, db: Any = None) -> None:
        self.db = db
        self.behaviors: dict[str, Any] = {}
        self.calls: list[SimpleNamespace] = []

    async def run_agent(
        self,
        role: str,
        prompt: str,
        *,
        unit_level: str,
        unit_id: str,
        cwd: Path,
        kind: str = "agent",
        cp_id: str | None = None,
        timeout_s: int | None = None,
        resume_session: str | None = None,
    ) -> AgentResult:
        self.calls.append(
            SimpleNamespace(
                role=role,
                prompt=prompt,
                unit_id=unit_id,
                cwd=Path(cwd),
                resume_session=resume_session,
            )
        )
        if self.db is not None:
            with self.db.transaction() as conn:
                fdb.insert_event(
                    conn,
                    unit_level=unit_level,
                    unit_id=unit_id,
                    event_type="spawn",
                    actor="control_plane",
                    payload={"role": role, "kind": kind, "cp_id": cp_id},
                )
        behavior = self.behaviors.get(role)
        if behavior is not None:
            behavior(Path(cwd), unit_id, resume_session)
        return AgentResult(
            process_id=0,
            exit_code=0,
            timed_out=False,
            killed=False,
            declared_failure=False,
            result_text="",
            session_id=None,
            tokens_in=1,
            tokens_out=1,
            cost_usd=None,
            garbage_lines=0,
            ndjson_log_path="(fake)",
            stderr_path="(fake)",
            duration_ms=1,
        )


class FakeWorktrees:
    """Scratch-dir provider for executor tests; gate methods are unreachable in
    the covered paths and assert if hit."""

    def __init__(self, scratch_root: Path) -> None:
        self.scratch_root = scratch_root
        self.created: list[tuple[str, str, str, bool]] = []
        self.removed: list[Path] = []

    async def create(self, repo_root, unit_id, branch, base_branch, *, new_branch=True):
        self.created.append((unit_id, branch, base_branch, new_branch))
        path = self.scratch_root / unit_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def remove(self, repo_root, worktree):
        self.removed.append(Path(worktree))
        # Real disposal semantics (§5.4): a removed scratch leaves no untracked
        # residue behind — the next create() starts from a clean dir.
        shutil.rmtree(worktree, ignore_errors=True)

    async def diff_digest(self, worktree, target_branch, max_bytes):
        return "== diffstat ==\n(fake digest)"

    async def full_diff(self, worktree, target_branch, max_bytes):
        return "(fake full diff)"

    async def merged_unit_diffs(self, repo_root, target_branch, since_ref, max_bytes):
        return {}

    async def tier1_gate(self, *a, **k):
        raise AssertionError("tier1_gate not expected in this test")

    async def integrate(self, *a, **k):
        raise AssertionError("integrate not expected in this test")

    async def heal_git_state(self, worktree):
        return []


_BLOCKED_STATES = {"AWAITING_HUMAN", "AWAITING_SIGNOFF", "ESCALATED"}

#: Happy next-state chains the scripted executor walks (legal §3 transitions).
_NEXT: dict[Level, dict[str, str]] = {
    Level.STAGE: {
        "PENDING": "SPEC",
        "SPEC": "BUILD",
        "BUILD": "VALIDATE",
        "VALIDATE": "MERGE_GATE",
        "MERGE_GATE": "DONE",
        "AWAITING_HUMAN": "MERGE_GATE",
    },
    Level.PHASE: {
        "PENDING": "PLANNING",
        "PLANNING": "CONTRACTS_FROZEN",
        "CONTRACTS_FROZEN": "RUNNING",
        "RUNNING": "INTEGRATING",
        "INTEGRATING": "AWAITING_SIGNOFF",
        "AWAITING_SIGNOFF": "DONE",
    },
}


@dataclass
class ScriptedExecutor:
    """One class for BOTH levels — driven through the same Scheduler loop, this
    is the §8 level-agnosticism proof object."""

    level: Level
    db: Any
    sm: StateMachine
    hold_s: float = 0.0
    respect_blocked: bool = False
    fail_units: frozenset[str] = frozenset()
    started: list[tuple[str, str]] = field(default_factory=list)
    finished: list[tuple[str, str]] = field(default_factory=list)
    concurrent: int = 0
    max_concurrent: int = 0

    async def execute(self, unit_id: str) -> None:
        self.started.append((self.level.value, unit_id))
        if unit_id in self.fail_units:
            raise RuntimeError(f"scripted failure for {unit_id}")
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.hold_s:
                await asyncio.sleep(self.hold_s)
            while True:
                unit = (
                    fdb.get_stage(self.db.read(), unit_id)
                    if self.level is Level.STAGE
                    else fdb.get_phase(self.db.read(), unit_id)
                )
                assert unit is not None
                state = unit.state.value
                if self.respect_blocked and state in _BLOCKED_STATES:
                    if (
                        state == "AWAITING_HUMAN"
                        and sched_mod._pending_decision_count(
                            self.db.read(), self.level.value, unit_id
                        )
                        == 0
                        and sched_mod._latest_decision(
                            self.db.read(), self.level.value, unit_id
                        )
                        is not None
                    ):
                        pass  # answered -> walk on
                    else:
                        return
                nxt = _NEXT[self.level].get(state)
                if nxt is None:
                    return
                self.sm.transition(
                    self.level, unit_id, nxt, actor="control_plane", reason="scripted"
                )
        finally:
            self.concurrent -= 1
            self.finished.append((self.level.value, unit_id))


def make_scheduler(
    db, cfg: FactoryConfig, executors: dict[Level, Any], notify: FakeNotify | None = None
) -> tuple[Scheduler, FakeNotify]:
    notify = notify or FakeNotify()
    return Scheduler(db, StateMachine(db), cfg, executors, notify), notify


async def run_blocked(scheduler: Scheduler, timeout: float = 15.0) -> None:
    await asyncio.wait_for(scheduler.run_until_blocked(), timeout=timeout)


# ------------------------------------------------------------ protocol surface


def test_executors_satisfy_the_frozen_protocol() -> None:
    assert StageExecutor.level is Level.STAGE
    assert PhaseExecutor.level is Level.PHASE
    for cls in (StageExecutor, PhaseExecutor):
        assert callable(cls.execute)
    assert isinstance(UnitExecutor, type)  # importable contract object


# ------------------------------------------------------- scheduler: DAG + cap


async def test_dag_ordering_prerequisite_completes_first(db, config_dict) -> None:
    cfg = make_config(config_dict)
    insert_phase(db, "ph")
    insert_stage(db, "A", "ph", StageState.PENDING)
    insert_stage(db, "B", "ph", StageState.PENDING)
    with db.transaction() as conn:
        fdb.insert_dag_edge(conn, Level.STAGE, "A", "B")
    sm = StateMachine(db)
    stage_exec = ScriptedExecutor(Level.STAGE, db, sm)
    scheduler, _ = make_scheduler(db, cfg, {Level.STAGE: stage_exec})
    await run_blocked(scheduler)

    assert stage_state(db, "A") is StageState.DONE
    assert stage_state(db, "B") is StageState.DONE
    started_ids = [unit for _, unit in stage_exec.started]
    # B is WAITING until A is DONE: A finishes before B ever starts.
    assert started_ids.index("A") < started_ids.index("B")
    assert stage_exec.finished.index(("stage", "A")) < started_ids.index("B")


async def test_parallel_cap_bounds_concurrent_units(db, config_dict) -> None:
    cfg = make_config(config_dict)  # conftest: max_parallel_agents = 2
    insert_phase(db, "ph")
    for sid in ("s1", "s2", "s3", "s4"):
        insert_stage(db, sid, "ph", StageState.PENDING)
    sm = StateMachine(db)
    stage_exec = ScriptedExecutor(Level.STAGE, db, sm, hold_s=0.08)
    scheduler, _ = make_scheduler(db, cfg, {Level.STAGE: stage_exec})
    await run_blocked(scheduler)

    assert stage_exec.max_concurrent == cfg.process.max_parallel_agents == 2
    assert all(stage_state(db, s) is StageState.DONE for s in ("s1", "s2", "s3", "s4"))


async def test_level_agnostic_same_loop_drives_phase_and_stages(db, config_dict) -> None:
    """§8: level-agnosticism proven by driving a fake phase + fake stages
    through the SAME loop with the SAME executor class."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph", PhaseState.PENDING)
    insert_stage(db, "ph.s1", "ph", StageState.PENDING)
    insert_stage(db, "ph.s2", "ph", StageState.PENDING)
    with db.transaction() as conn:
        fdb.insert_dag_edge(conn, Level.STAGE, "ph.s1", "ph.s2")
    sm = StateMachine(db)
    phase_exec = ScriptedExecutor(Level.PHASE, db, sm)
    stage_exec = ScriptedExecutor(Level.STAGE, db, sm)
    assert type(phase_exec) is type(stage_exec)
    scheduler, _ = make_scheduler(
        db, cfg, {Level.PHASE: phase_exec, Level.STAGE: stage_exec}
    )
    await run_blocked(scheduler)

    assert phase_state(db, "ph") is PhaseState.DONE
    assert stage_state(db, "ph.s1") is StageState.DONE
    assert stage_state(db, "ph.s2") is StageState.DONE
    driven_levels = {lvl for lvl, _ in phase_exec.started} | {
        lvl for lvl, _ in stage_exec.started
    }
    assert driven_levels == {"phase", "stage"}


# -------------------------------------------------------- scheduler: stall etc.


async def test_stall_detector_pages_on_wedged_factory(db, config_dict) -> None:
    """Non-terminal units exist, nothing RUNNABLE/RUNNING, no open decision or
    escalation -> one 'alert' event + one ntfy page (latched per episode)."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph")
    insert_stage(db, "wedged", "ph", StageState.PENDING)
    with db.transaction() as conn:
        # Dangling prerequisite: deps never done -> WAITING forever (§2 deps_done).
        fdb.insert_dag_edge(conn, Level.STAGE, "ghost", "wedged")
    sm = StateMachine(db)
    scheduler, notify = make_scheduler(
        db, cfg, {Level.STAGE: ScriptedExecutor(Level.STAGE, db, sm)}
    )
    await run_blocked(scheduler)

    alerts = [
        e
        for e in events_of(db, None) + events_of(db, "wedged")
        if e["event_type"] == "alert"
    ]
    factory_alerts = (
        db.read()
        .execute("SELECT * FROM events WHERE unit_level='factory' AND event_type='alert'")
        .fetchall()
    )
    assert len(factory_alerts) == 1
    payload = json.loads(factory_alerts[0]["payload_json"])
    assert payload["kind"] == "stall"
    assert "stage:wedged" in payload["non_terminal_units"]
    stall_pushes = [p for p in notify.published if "blocat" in p[0]]
    assert len(stall_pushes) == 1 and stall_pushes[0][2] == "max"
    assert alerts is not None  # events_of also exercised


async def test_no_stall_alert_when_a_decision_is_pending(db, config_dict) -> None:
    cfg = make_config(config_dict)
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.AWAITING_HUMAN)
    with db.transaction() as conn:
        fdb.insert_artifact_ref(
            conn,
            __import__("sf_factory.models", fromlist=["ArtifactRef"]).ArtifactRef(
                id=None,
                unit_level="stage",
                unit_id="s1",
                kind="decision_request",
                repo="workspace",
                path="_factory/stages/s1/decision-request.md",
                sha256="0" * 64,
                git_commit=None,
                created_at=utc_now(),
            ),
        )
        fdb.insert_decision_request(
            conn,
            DecisionRequest(
                id=None,
                unit_level="stage",
                unit_id="s1",
                gate_kind="critical_stage",
                request_artifact_id=1,
                status="pending",
                answer=None,
                answer_artifact_id=None,
                created_at=utc_now(),
                alerted_at=None,
                answered_at=None,
            ),
        )
    sm = StateMachine(db)
    scheduler, notify = make_scheduler(
        db, cfg, {Level.STAGE: ScriptedExecutor(Level.STAGE, db, sm, respect_blocked=True)}
    )
    await run_blocked(scheduler)

    assert stage_state(db, "s1") is StageState.AWAITING_HUMAN
    assert notify.published == []  # no stall page, no latency alert (fresh request)


async def test_blocked_unit_redispatched_after_answer(db, config_dict) -> None:
    """answer_decision writes no event — the BLOCKED snapshot (pending-decision
    count) must wake the unit on the next run."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.AWAITING_HUMAN)
    with db.transaction() as conn:
        fdb.insert_decision_request(
            conn,
            DecisionRequest(
                id=None,
                unit_level="stage",
                unit_id="s1",
                gate_kind="critical_stage",
                request_artifact_id=_seed_artifact(conn, "s1"),
                status="pending",
                answer=None,
                answer_artifact_id=None,
                created_at=utc_now(),
                alerted_at=None,
                answered_at=None,
            ),
        )
    sm = StateMachine(db)
    stage_exec = ScriptedExecutor(Level.STAGE, db, sm, respect_blocked=True)
    scheduler, _ = make_scheduler(db, cfg, {Level.STAGE: stage_exec})
    await run_blocked(scheduler)
    assert stage_state(db, "s1") is StageState.AWAITING_HUMAN

    with db.transaction() as conn:
        fdb.answer_decision(conn, 1, "approved", None)
    await run_blocked(scheduler)  # same instance: snapshot diff must re-dispatch
    assert stage_state(db, "s1") is StageState.DONE


def _seed_artifact(conn, unit_id: str) -> int:
    from sf_factory.models import ArtifactRef

    return fdb.insert_artifact_ref(
        conn,
        ArtifactRef(
            id=None,
            unit_level="stage",
            unit_id=unit_id,
            kind="decision_request",
            repo="workspace",
            path=f"_factory/stages/{unit_id}/decision-request.md",
            sha256="1" * 64,
            git_commit=None,
            created_at=utc_now(),
        ),
    )


async def test_decision_latency_alert_fires_once_and_marks(db, config_dict) -> None:
    cfg = make_config(config_dict)
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.AWAITING_HUMAN)
    old = (datetime.now(UTC) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.transaction() as conn:
        fdb.insert_decision_request(
            conn,
            DecisionRequest(
                id=None,
                unit_level="stage",
                unit_id="s1",
                gate_kind="critical_stage",
                request_artifact_id=_seed_artifact(conn, "s1"),
                status="pending",
                answer=None,
                answer_artifact_id=None,
                created_at=old,
                alerted_at=None,
                answered_at=None,
            ),
        )
    sm = StateMachine(db)
    scheduler, notify = make_scheduler(
        db, cfg, {Level.STAGE: ScriptedExecutor(Level.STAGE, db, sm, respect_blocked=True)}
    )
    await run_blocked(scheduler)

    latency_pushes = [p for p in notify.published if "așteptare" in p[0]]
    assert len(latency_pushes) == 1 and latency_pushes[0][2] == "max"
    row = db.read().execute("SELECT alerted_at FROM decision_requests WHERE id=1").fetchone()
    assert row["alerted_at"] is not None
    assert len(events_of(db, "s1", "alert")) == 1  # decision_latency alert event

    await run_blocked(scheduler)  # marked -> never re-fires
    assert len([p for p in notify.published if "așteptare" in p[0]]) == 1


async def test_delivery_failure_logged_and_unmarked(db, config_dict) -> None:
    cfg = make_config(config_dict)
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.AWAITING_HUMAN)
    old = (datetime.now(UTC) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.transaction() as conn:
        fdb.insert_decision_request(
            conn,
            DecisionRequest(
                id=None,
                unit_level="stage",
                unit_id="s1",
                gate_kind="critical_stage",
                request_artifact_id=_seed_artifact(conn, "s1"),
                status="pending",
                answer=None,
                answer_artifact_id=None,
                created_at=old,
                alerted_at=None,
                answered_at=None,
            ),
        )
    sm = StateMachine(db)
    scheduler, _ = make_scheduler(
        db,
        cfg,
        {Level.STAGE: ScriptedExecutor(Level.STAGE, db, sm, respect_blocked=True)},
        notify=FakeNotify(fail=True),
    )
    await run_blocked(scheduler)

    row = db.read().execute("SELECT alerted_at FROM decision_requests WHERE id=1").fetchone()
    assert row["alerted_at"] is None  # unmarked -> retried next tick
    failures = (
        db.read()
        .execute("SELECT * FROM events WHERE event_type='alert_delivery_failed'")
        .fetchall()
    )
    assert len(failures) == 1  # one event per failure streak, not per tick


async def test_containment_escalates_failed_unit_siblings_continue(
    db, config_dict
) -> None:
    cfg = make_config(config_dict)
    insert_phase(db, "ph")
    insert_stage(db, "bad", "ph", StageState.BUILD)  # category RUNNING, never driven
    insert_stage(db, "ok", "ph", StageState.PENDING)
    sm = StateMachine(db)
    stage_exec = ScriptedExecutor(Level.STAGE, db, sm, fail_units=frozenset({"bad"}))
    scheduler, _ = make_scheduler(db, cfg, {Level.STAGE: stage_exec})
    await run_blocked(scheduler)

    assert stage_state(db, "ok") is StageState.DONE
    assert stage_state(db, "bad") is StageState.ESCALATED
    rows = open_escalations(db, "bad")
    assert [r["trigger"] for r in rows] == ["internal_error"]
    assert rows[0]["target"] == "phase_architect"
    internal = events_of(db, "bad", "internal_error")
    # Each contained crash is recorded; the OPEN escalation row stays unique
    # (uq_open_escalation), so re-drives never duplicate the page.
    assert len(internal) >= 1
    trace_path = Path(json.loads(internal[0]["payload_json"])["traceback_path"])
    assert trace_path.is_file()
    assert "scripted failure" in trace_path.read_text(encoding="utf-8")


async def test_liveness_and_pidfile_refreshed(db, config_dict) -> None:
    cfg = make_config(config_dict)
    scheduler, _ = make_scheduler(db, cfg, {})
    await run_blocked(scheduler)

    liveness = Path(cfg.process.liveness_file)
    pidfile = Path(cfg.process.pid_file)
    assert liveness.is_file()
    lines = pidfile.read_text(encoding="utf-8").splitlines()
    assert int(lines[0]) > 0  # watchdog pidfile contract: line 1 = pid
    assert len(lines) >= 2 and lines[1]  # line 2 = cmdline (NULs -> spaces)


# ------------------------------------------------------ PhaseExecutor reactions


def make_phase_executor(
    db, cfg: FactoryConfig, runner=None, wt=None, notify=None
) -> PhaseExecutor:
    return PhaseExecutor(
        db,
        StateMachine(db),
        cfg,
        runner or FakeRunner(),
        wt or FakeWorktrees(Path(cfg.factory.home) / "scratch"),
        notify or FakeNotify(),
    )


async def test_phase_reacts_to_child_stage_entering_failed(db, config_dict) -> None:
    """§8: phase transition on a child stage entering FAILED — the phase must
    never wedge in RUNNING (§3.2)."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph", PhaseState.RUNNING)
    insert_stage(db, "ph.good", "ph", StageState.DONE)
    insert_stage(db, "ph.bad", "ph", StageState.FAILED)
    executor = make_phase_executor(db, cfg)
    await executor.execute("ph")

    assert phase_state(db, "ph") is PhaseState.ESCALATED
    rows = open_escalations(db, "ph")
    assert [r["trigger"] for r in rows] == ["child_failed"]
    assert rows[0]["target"] == "phase_architect"
    payload = json.loads(events_of(db, "ph", "transition")[-1]["payload_json"])
    assert payload["failed"] == ["ph.bad"]


async def test_phase_waits_while_children_run(db, config_dict) -> None:
    cfg = make_config(config_dict)
    insert_phase(db, "ph", PhaseState.RUNNING)
    insert_stage(db, "ph.s1", "ph", StageState.BUILD)
    executor = make_phase_executor(db, cfg)
    await executor.execute("ph")
    assert phase_state(db, "ph") is PhaseState.RUNNING
    assert open_escalations(db, "ph") == []


async def test_phase_advances_to_integrating_when_children_done(db, config_dict) -> None:
    """All children TERMINAL_OK -> INTEGRATING; with OPEN-2 unset the gate then
    escalates explicitly (never a silently skipped suite)."""
    cfg = make_config(config_dict)  # test_command: None (OPEN-2)
    insert_phase(db, "ph", PhaseState.RUNNING)
    insert_stage(db, "ph.s1", "ph", StageState.DONE)
    insert_stage(db, "ph.s2", "ph", StageState.DONE)
    executor = make_phase_executor(db, cfg)
    await executor.execute("ph")

    assert ("RUNNING", "INTEGRATING") in transitions_of(db, "ph")
    assert phase_state(db, "ph") is PhaseState.ESCALATED
    rows = open_escalations(db, "ph")
    assert rows and rows[0]["trigger"] == "internal_error"


async def test_phase_ingests_validated_plan_into_stages_and_dag(db, config_dict) -> None:
    """CONTRACTS_FROZEN -> RUNNING: read_phase_plan strict ingestion (§3.2),
    stage ids namespaced by phase, edges inserted, all in one tx."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph", PhaseState.CONTRACTS_FROZEN)
    worktree = Path(cfg.projects["proj"].worktrees_dir) / "ph"
    plan_dir = worktree / "_factory" / "phases" / "ph"
    plan_dir.mkdir(parents=True)
    (plan_dir / "phase-plan.json").write_text(
        json.dumps(
            {
                "stages": [
                    {"id": "s1", "name": "one", "risk_class": "routine", "acceptance": "a"},
                    {"id": "s2", "name": "two", "risk_class": "critical", "acceptance": "b"},
                ],
                "dag_edges": [["s1", "s2"]],
            }
        ),
        encoding="utf-8",
    )
    executor = make_phase_executor(db, cfg)
    await executor.execute("ph")

    assert phase_state(db, "ph") is PhaseState.RUNNING
    s1 = fdb.get_stage(db.read(), "ph.s1")
    s2 = fdb.get_stage(db.read(), "ph.s2")
    assert s1 is not None and s1.state is StageState.PENDING and s1.risk_class == "routine"
    assert s2 is not None and s2.risk_class == "critical" and s2.branch == "stage/ph.s2"
    assert not fdb.deps_done(db.read(), Level.STAGE, "ph.s2")  # edge s1 -> s2 live
    assert fdb.deps_done(db.read(), Level.STAGE, "ph.s1")


async def test_phase_rejects_cyclic_plan_without_silent_state_change(
    db, config_dict
) -> None:
    cfg = make_config(config_dict)
    insert_phase(db, "ph", PhaseState.CONTRACTS_FROZEN)
    worktree = Path(cfg.projects["proj"].worktrees_dir) / "ph"
    plan_dir = worktree / "_factory" / "phases" / "ph"
    plan_dir.mkdir(parents=True)
    (plan_dir / "phase-plan.json").write_text(
        json.dumps(
            {
                "stages": [
                    {"id": "a", "name": "a", "risk_class": "routine", "acceptance": "x"},
                    {"id": "b", "name": "b", "risk_class": "routine", "acceptance": "y"},
                ],
                "dag_edges": [["a", "b"], ["b", "a"]],
            }
        ),
        encoding="utf-8",
    )
    executor = make_phase_executor(db, cfg)
    await executor.execute("ph")

    # No ESCALATED edge from CONTRACTS_FROZEN (§3.2): state unchanged, but the
    # breach is fully visible — escalation row + event, and no stage rows.
    assert phase_state(db, "ph") is PhaseState.CONTRACTS_FROZEN
    rows = open_escalations(db, "ph")
    assert [r["trigger"] for r in rows] == ["artifact_contract"]
    assert fdb.get_stage(db.read(), "ph.a") is None
    assert events_of(db, "ph", "escalation")


async def test_phase_planning_sentinel_escalates(db, config_dict) -> None:
    cfg = make_config(config_dict)
    insert_phase(db, "ph", PhaseState.PLANNING)
    runner = FakeRunner()

    def architect(cwd: Path, unit_id: str, resume) -> None:
        d = cwd / "_factory" / "phases" / unit_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "_DECLARED_FAILURE.md").write_text("cannot decompose", encoding="utf-8")

    runner.behaviors["phase_architect"] = architect
    executor = make_phase_executor(db, cfg, runner=runner)
    await executor.execute("ph")

    assert phase_state(db, "ph") is PhaseState.ESCALATED
    rows = open_escalations(db, "ph")
    assert [r["trigger"] for r in rows] == ["agent_declared_failure"]
    assert rows[0]["target"] == "main_architect"
    assert rows[0]["event_seq"] == events_of(db, "ph", "declared_failure")[0]["seq"]


_VALID_PLAN_JSON = json.dumps(
    {
        "stages": [
            {"id": "s1", "name": "one", "risk_class": "routine", "acceptance": "a"}
        ],
        "dag_edges": [],
    }
)


async def test_phase_planning_sentinel_archived_on_resolution_no_reescalate(
    db, config_dict
) -> None:
    """§5.4 at phase level (wave-3 fix round): a _DECLARED_FAILURE.md written
    by the phase architect into the DURABLE phase worktree during PLANNING is
    archived (rename + commit) when the escalation resolves to 'replan' — the
    re-run must not re-detect the stale sentinel (a NEW events.seq past the
    cursor) and re-escalate forever even though the agent now succeeds."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph", PhaseState.PLANNING)
    worktree = Path(cfg.projects["proj"].worktrees_dir) / "ph"
    init_repo(worktree)
    runner = FakeRunner(db)
    calls = [0]

    def architect(cwd: Path, unit_id: str, resume) -> None:
        calls[0] += 1
        d = cwd / "_factory" / "phases" / unit_id
        d.mkdir(parents=True, exist_ok=True)
        if calls[0] == 1:
            (d / "_DECLARED_FAILURE.md").write_text("cannot decompose", encoding="utf-8")
            return
        (d / "phase-plan.md").write_text("plan\n", encoding="utf-8")
        (d / "phase-plan.json").write_text(_VALID_PLAN_JSON, encoding="utf-8")

    runner.behaviors["phase_architect"] = architect
    executor = make_phase_executor(db, cfg, runner=runner)
    await executor.execute("ph")
    assert phase_state(db, "ph") is PhaseState.ESCALATED
    (esc,) = open_escalations(db, "ph")

    with db.transaction() as conn:
        fdb.resolve_escalation(conn, esc["id"], "replan")
    await executor.execute("ph")

    unit_dir = worktree / "_factory" / "phases" / "ph"
    assert not (unit_dir / "_DECLARED_FAILURE.md").exists()
    archived = unit_dir / f"_DECLARED_FAILURE.resolved-{esc['id']}.md"
    assert archived.is_file()  # renamed + committed BEFORE the replan re-run
    assert calls[0] == 2
    assert len(events_of(db, "ph", "declared_failure")) == 1  # stale never re-fired
    assert open_escalations(db, "ph") == []
    assert phase_state(db, "ph") is PhaseState.RUNNING  # replan completed + ingested


async def test_phase_planning_replay_identical_plan_anchors_freeze_on_head(
    db, config_dict
) -> None:
    """§5.5d at-least-once replay (wave-3 fix round): orchestrator died after
    the plan commit but before the CONTRACTS_FROZEN tx; the re-run produces
    byte-identical plan files, so commit_paths returns None — the freeze
    anchor must fall back to the current HEAD (cli._commit_decision_answer
    pattern), never record {'commit': None}, which would leave
    _contract_freeze_commit anchorless at the first stage MERGE_GATE."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph", PhaseState.PLANNING)
    worktree = Path(cfg.projects["proj"].worktrees_dir) / "ph"
    init_repo(worktree)
    plan_dir = worktree / "_factory" / "phases" / "ph"
    plan_dir.mkdir(parents=True)
    (plan_dir / "phase-plan.md").write_text("plan\n", encoding="utf-8")
    (plan_dir / "phase-plan.json").write_text(_VALID_PLAN_JSON, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "pre-crash plan commit"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    executor = make_phase_executor(db, cfg)  # re-run agent writes nothing new
    await executor.execute("ph")

    frozen = [
        e
        for e in events_of(db, "ph", "transition")
        if e["to_state"] == "CONTRACTS_FROZEN"
    ]
    assert json.loads(frozen[0]["payload_json"])["commit"] == head
    ref = fdb.latest_artifact(db.read(), "phase", "ph", "phase_plan_sidecar")
    assert ref is not None and ref.git_commit == head
    assert phase_state(db, "ph") is PhaseState.RUNNING  # replay continued cleanly


async def test_phase_tier2_sibling_window_survives_tier1_rebase(
    db, config_dict, tmp_path
) -> None:
    """§3.1 phase-INTEGRATING regression (wave-3 fix round): a sibling unit
    merged into the target BEFORE the gate must reach the Integration
    Validator as a full diff. The Tier-1 rebase moves this phase's fork point
    to the target head, so the sibling-window anchor must be captured
    PRE-rebase — a post-rebase merge-base yields an empty window and the DoD
    §5.3 seeded scenario becomes structurally uncatchable."""
    from sf_factory.worktrees import WorktreeManager, commit_paths

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    config_dict["projects"]["proj"]["test_command"] = "true"
    cfg = make_config(config_dict)
    workspace = Path(cfg.projects["proj"].workspace)
    init_repo(workspace)
    wt = WorktreeManager(cfg)

    insert_phase(db, "ph", PhaseState.INTEGRATING)
    phase_wt = await wt.create(workspace, "ph", "phase/ph", "main")
    plan = phase_wt / "_factory" / "phases" / "ph" / "phase-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("plan body\n", encoding="utf-8")
    await commit_paths(
        phase_wt, [plan], "phase ph: plan", trailers={"Factory-Unit": "phase/ph"}
    )

    # Sibling unit merged into main AFTER ph forked and BEFORE the gate runs —
    # the rebase will absorb it, so only the pre-rebase anchor can see it.
    git("switch", "-q", "-c", "phase/sib", cwd=workspace)
    (workspace / "sibling.py").write_text("SIBLING_MARKER = 'windowed'\n", encoding="utf-8")
    git("add", "--", "sibling.py", cwd=workspace)
    git("commit", "-q", "-m", "sibling work", cwd=workspace)
    git("switch", "-q", "main", cwd=workspace)
    await wt.integrate(workspace, "phase/sib", "main")

    runner = FakeRunner(db)

    def validator(cwd: Path, unit_id: str, resume) -> None:
        d = cwd / "_factory" / "phases" / unit_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "integration-report.md").write_text("clean\n", encoding="utf-8")
        (d / "integration-report.json").write_text('{"findings": []}', encoding="utf-8")

    runner.behaviors["integration_validator"] = validator
    executor = PhaseExecutor(db, StateMachine(db), cfg, runner, wt, FakeNotify())
    await executor.execute("ph")

    calls = [c for c in runner.calls if c.role == "integration_validator"]
    assert len(calls) == 1
    prompt = calls[0].prompt
    assert "--- merged unit sib ---" in prompt  # the sibling reached Tier 2
    assert "SIBLING_MARKER" in prompt  # ...as a full diff body, not a header
    assert "(none)" not in prompt  # the pre-fix symptom: empty sibling window
    (gate,) = events_of(db, "ph", "tier2_gate")
    assert json.loads(gate["payload_json"])["siblings"] == ["sib"]
    assert phase_state(db, "ph") is PhaseState.AWAITING_SIGNOFF  # gates passed
    # Wave-3 fix round: both phase Tier-2 reports register as phase-level
    # audit_report refs (uniform with _step_audit), and the scratch is gone.
    refs = (
        db.read()
        .execute(
            "SELECT path FROM artifact_refs WHERE unit_level='phase'"
            " AND unit_id='ph' AND kind='audit_report' ORDER BY id"
        )
        .fetchall()
    )
    assert [Path(r["path"]).name for r in refs] == [
        "integration-report.md",
        "integration-report.json",
    ]
    assert not (Path(cfg.projects["proj"].worktrees_dir) / "ph-tier2").exists()


async def test_phase_tier2_sentinel_attributed_to_integration_validator(
    db, config_dict, tmp_path
) -> None:
    """§6 audit-trail attribution: a _DECLARED_FAILURE.md written by the
    Integration Validator at the phase Tier-2 gate must be recorded with
    actor='integration_validator' in the append-only events trail — not
    misattributed to the phase architect (the pre-fix symptom)."""
    from sf_factory.worktrees import WorktreeManager

    config_dict["projects"]["proj"]["test_command"] = "true"
    cfg = make_config(config_dict)
    workspace = Path(cfg.projects["proj"].workspace)
    init_repo(workspace)
    wt = WorktreeManager(cfg)

    insert_phase(db, "ph", PhaseState.INTEGRATING)
    await wt.create(workspace, "ph", "phase/ph", "main")

    runner = FakeRunner(db)

    def validator(cwd: Path, unit_id: str, resume) -> None:
        d = cwd / "_factory" / "phases" / unit_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "_DECLARED_FAILURE.md").write_text("cannot validate", encoding="utf-8")

    runner.behaviors["integration_validator"] = validator
    executor = PhaseExecutor(db, StateMachine(db), cfg, runner, wt, FakeNotify())
    await executor.execute("ph")

    assert phase_state(db, "ph") is PhaseState.ESCALATED
    (event,) = events_of(db, "ph", "declared_failure")
    assert event["actor"] == "integration_validator"
    rows = open_escalations(db, "ph")
    assert [r["trigger"] for r in rows] == ["agent_declared_failure"]
    assert rows[0]["target"] == "main_architect"
    # Wave-3 fix round: the scratch is disposed on the ESCALATION exit too —
    # a leaked tier2 scratch would re-detect this stale sentinel (untracked,
    # so create()'s tracked-content re-sync never clears it) at every later
    # integration gate run, and the phase could never pass Tier 2 again.
    assert not (Path(cfg.projects["proj"].worktrees_dir) / "ph-tier2").exists()


async def test_stage_merge_gate_tier2_registers_both_reports_and_disposes_scratch(
    db, config_dict, tmp_path
) -> None:
    """Stage Tier-2 gate (wave-3 fix round): the prose integration report is
    registered ALONGSIDE the findings sidecar (both kind='audit_report',
    uniform with _step_audit — committed-but-unregistered output would escape
    verify_integrity), and the '-tier2' scratch worktree is disposed before
    the gate concludes."""
    from sf_factory.worktrees import WorktreeManager

    config_dict["projects"]["proj"]["test_command"] = "true"
    cfg = make_config(config_dict)
    workspace = Path(cfg.projects["proj"].workspace)
    init_repo(workspace)
    wt = WorktreeManager(cfg)

    insert_phase(db, "ph", PhaseState.RUNNING, branch="main")
    worktree = await wt.create(workspace, "ph.s1", "stage/ph.s1", "main")
    insert_stage(db, "ph.s1", "ph", StageState.MERGE_GATE, worktree=worktree)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with db.transaction() as conn:  # the §3.1 freeze anchor the stage gate reads
        fdb.insert_event(
            conn,
            unit_level="phase",
            unit_id="ph",
            event_type="transition",
            actor="control_plane",
            from_state="PLANNING",
            to_state="CONTRACTS_FROZEN",
            payload={"contracts": 0, "commit": head},
        )

    runner = FakeRunner(db)

    def validator(cwd: Path, unit_id: str, resume) -> None:
        d = cwd / "_factory" / "stages" / unit_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "integration-report.md").write_text("clean\n", encoding="utf-8")
        (d / "integration-report.json").write_text('{"findings": []}', encoding="utf-8")

    runner.behaviors["integration_validator"] = validator
    executor = StageExecutor(
        db,
        StateMachine(db),
        cfg,
        runner,
        wt,
        ThresholdEvaluator(db, cfg),
        FakeConsultor([]),
        FakeNotify(),
    )
    await executor.execute("ph.s1")

    assert stage_state(db, "ph.s1") is StageState.DONE
    refs = (
        db.read()
        .execute(
            "SELECT path, git_commit FROM artifact_refs WHERE unit_level='stage'"
            " AND unit_id='ph.s1' AND kind='audit_report' ORDER BY id"
        )
        .fetchall()
    )
    assert [Path(r["path"]).name for r in refs] == [
        "integration-report.md",
        "integration-report.json",
    ]
    assert all(r["git_commit"] for r in refs)
    assert events_of(db, "ph.s1", "tier2_gate")
    assert not (Path(cfg.projects["proj"].worktrees_dir) / "ph.s1-tier2").exists()


# --------------------------------------------------------- StageExecutor paths


def make_stage_env(db, config_dict, tmp_path: Path, *, risk: str = "routine"):
    """Real db/sm/thresholds + real git stage worktree; fake runner/wt/consultor."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph", PhaseState.RUNNING)
    worktree = tmp_path / "stage-wt"
    init_repo(worktree)
    insert_stage(db, "ph.s1", "ph", StageState.VALIDATE, risk=risk, worktree=worktree)
    runner = FakeRunner(db)
    wt = FakeWorktrees(tmp_path / "scratch")
    consultor = FakeConsultor([])
    notify = FakeNotify()
    executor = StageExecutor(
        db,
        StateMachine(db),
        cfg,
        runner,
        wt,
        ThresholdEvaluator(db, cfg),
        consultor,
        notify,
    )
    return SimpleNamespace(
        cfg=cfg,
        worktree=worktree,
        runner=runner,
        wt=wt,
        consultor=consultor,
        notify=notify,
        executor=executor,
    )


def validator_writing(failing: int):
    def behavior(cwd: Path, unit_id: str, resume) -> None:
        d = cwd / "_factory" / "stages" / unit_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "validation-report.md").write_text(
            f"failing={failing} report\n", encoding="utf-8"
        )
        (d / "validation-report.json").write_text(
            json.dumps({"failing": failing, "passing": 3, "total": 3 + failing}),
            encoding="utf-8",
        )

    return behavior


def builder_writing(counter: list[int]):
    def behavior(cwd: Path, unit_id: str, resume) -> None:
        counter[0] += 1
        (cwd / f"impl-{counter[0]}.py").write_text(
            f"VALUE = {counter[0]}\n", encoding="utf-8"
        )

    return behavior


async def test_thresholds_decide_before_cp1(db, config_dict, tmp_path) -> None:
    """§3.1: when a deterministic trigger decides, CP-1 is never consulted."""
    env = make_stage_env(db, config_dict, tmp_path)
    env.runner.behaviors["validator"] = validator_writing(failing=5)
    with db.transaction() as conn:  # two prior non-decreasing iterations
        fdb.insert_fix_iteration(conn, "ph.s1", 5, None)
        fdb.insert_fix_iteration(conn, "ph.s1", 5, None)
    await env.executor.execute("ph.s1")

    assert stage_state(db, "ph.s1") is StageState.ESCALATED
    rows = open_escalations(db, "ph.s1")
    assert [r["trigger"] for r in rows] == ["max_fix_iterations"]
    assert env.consultor.calls == []  # thresholds first — CP-1 never reached
    payload_ref = rows[0]["payload_artifact_id"]
    assert payload_ref is not None  # evidence committed as an artifact (DoD §8)


async def test_cp1_continue_session_resumes_builder(db, config_dict, tmp_path) -> None:
    """CP-1 continue_session: the Builder re-spawns with its last registered
    session id; the resume id travels in the BUILD-entry transition payload."""
    env = make_stage_env(db, config_dict, tmp_path)
    env.runner.behaviors["validator"] = validator_writing(failing=2)
    env.runner.behaviors["builder_routine"] = builder_writing([0])
    env.consultor.verdicts = ["continue_session", "escalate"]
    with db.transaction() as conn:  # a finalized builder session to resume
        pid = fdb.insert_process(
            conn,
            ProcessRecord(
                id=None,
                unit_level="stage",
                unit_id="ph.s1",
                kind="agent",
                role="builder_routine",
                cp_id=None,
                session_id=None,
                pid=None,
                cmdline="stub",
                cwd=None,
                state="spawned",
                exit_code=None,
                ndjson_log_path=None,
                spawned_at=utc_now(),
                heartbeat_at=None,
                ended_at=None,
            ),
        )
        fdb.finalize_process(
            conn, pid, state="exited", exit_code=0, ended_at=utc_now(), session_id="sess-1"
        )
    await env.executor.execute("ph.s1")

    builder_calls = [c for c in env.runner.calls if c.role == "builder_routine"]
    assert len(builder_calls) == 1 and builder_calls[0].resume_session == "sess-1"
    entry = [
        e
        for e in events_of(db, "ph.s1", "transition")
        if e["to_state"] == "BUILD"
    ]
    assert json.loads(entry[0]["payload_json"])["resume_session"] == "sess-1"
    assert len(env.consultor.calls) == 2  # iteration 2 -> scripted escalate
    assert stage_state(db, "ph.s1") is StageState.ESCALATED
    assert [r["trigger"] for r in open_escalations(db, "ph.s1")] == ["cp1_verdict"]
    # CP-1 inputs were assembled exactly per the registry declaration.
    assert set(env.consultor.calls[0][1]) == {"validation_report", "diff_digest", "spec"}


async def test_cp1_continue_session_downgrades_without_session(
    db, config_dict, tmp_path
) -> None:
    """No finalized builder session -> rebuild + explicit verdict_downgraded
    event (§3.1: recorded, never silent)."""
    env = make_stage_env(db, config_dict, tmp_path)
    env.runner.behaviors["validator"] = validator_writing(failing=2)
    env.runner.behaviors["builder_routine"] = builder_writing([0])
    env.consultor.verdicts = ["continue_session", "escalate"]
    await env.executor.execute("ph.s1")

    builder_calls = [c for c in env.runner.calls if c.role == "builder_routine"]
    assert len(builder_calls) == 1 and builder_calls[0].resume_session is None
    downgrades = events_of(db, "ph.s1", "verdict_downgraded")
    assert len(downgrades) == 1
    payload = json.loads(downgrades[0]["payload_json"])
    assert payload["executed_as"] == "rebuild" and "session" in payload["reason"]


async def test_cp1_continue_session_downgrades_on_unverified_resume_route(
    db, config_dict, tmp_path
) -> None:
    """§3.1/OPEN-3 hard gate: a builder route whose CLI lacks verified session
    resume (codex, until wave-4 A2 verifies it against the real CLI) executes
    continue_session as rebuild + explicit verdict_downgraded — even though a
    finalized, resumable builder session exists."""
    config_dict["models"]["builder_routine"]["cli"] = "codex"
    env = make_stage_env(db, config_dict, tmp_path)
    env.runner.behaviors["validator"] = validator_writing(failing=2)
    env.runner.behaviors["builder_routine"] = builder_writing([0])
    env.consultor.verdicts = ["continue_session", "escalate"]
    with db.transaction() as conn:  # finalized session: the route is the ONLY reason
        pid = fdb.insert_process(
            conn,
            ProcessRecord(
                id=None,
                unit_level="stage",
                unit_id="ph.s1",
                kind="agent",
                role="builder_routine",
                cp_id=None,
                session_id=None,
                pid=None,
                cmdline="codex",
                cwd=None,
                state="spawned",
                exit_code=None,
                ndjson_log_path=None,
                spawned_at=utc_now(),
                heartbeat_at=None,
                ended_at=None,
            ),
        )
        fdb.finalize_process(
            conn, pid, state="exited", exit_code=0, ended_at=utc_now(), session_id="sess-1"
        )
    await env.executor.execute("ph.s1")

    builder_calls = [c for c in env.runner.calls if c.role == "builder_routine"]
    assert len(builder_calls) == 1 and builder_calls[0].resume_session is None  # rebuild ran
    downgrades = events_of(db, "ph.s1", "verdict_downgraded")
    assert len(downgrades) == 1
    payload = json.loads(downgrades[0]["payload_json"])
    assert payload["executed_as"] == "rebuild"
    assert "codex" in payload["reason"] and "OPEN-3" in payload["reason"]
    entry = [
        e for e in events_of(db, "ph.s1", "transition") if e["to_state"] == "BUILD"
    ]
    assert json.loads(entry[0]["payload_json"])["executed_as"] == "rebuild"


async def test_usage_missing_escalate_after_escalates_stage(
    db, config_dict, tmp_path
) -> None:
    """D-0014(1): under budgets.usage_missing_policy='escalate_after' the
    StageExecutor itself counts the runner's 'usage_missing' events and, past
    budgets.usage_missing_max_per_stage, inserts the escalation row directly
    (trigger string, like 'internal_error' — NO Trigger enum member) and
    transitions the stage to ESCALATED: a usage-blind stage must still hit a
    budget (Doctrine §20)."""
    config_dict["budgets"]["usage_missing_policy"] = "escalate_after"
    env = make_stage_env(db, config_dict, tmp_path)
    allowance = env.cfg.budgets.usage_missing_max_per_stage
    with db.transaction() as conn:
        for _ in range(allowance + 1):  # strictly MORE than the allowance
            fdb.insert_event(
                conn,
                unit_level="stage",
                unit_id="ph.s1",
                event_type="usage_missing",
                actor="runner",
                payload={"policy": "escalate_after"},
            )
    await env.executor.execute("ph.s1")

    assert stage_state(db, "ph.s1") is StageState.ESCALATED
    rows = open_escalations(db, "ph.s1")
    assert [r["trigger"] for r in rows] == ["usage_missing"]
    assert rows[0]["target"] == "phase_architect"
    assert rows[0]["event_seq"] == events_of(db, "ph.s1", "usage_missing")[-1]["seq"]
    assert rows[0]["payload_artifact_id"] is not None  # evidence artifact (DoD §8)
    assert "usage_missing" not in {t.value for t in Trigger}  # D-0014: no enum member
    payload = json.loads(events_of(db, "ph.s1", "transition")[-1]["payload_json"])
    assert payload["triggers"] == ["usage_missing"]
    assert env.consultor.calls == []  # escalated before any CP-1 consult


async def test_stage_sibling_anchor_survives_phase_artifact_reresolve(
    db, config_dict, tmp_path
) -> None:
    """§3.1 secondary regression (wave-3 fix round): the stage-gate sibling
    anchor is read from the append-only CONTRACTS_FROZEN transition payload.
    The phase-level Tier-1 rebase re-resolves the phase_plan_sidecar ref's
    git_commit to the rebased HEAD — anchoring on the ref would silently void
    the sibling window for stage gates re-run after a signoff 'changes' loop."""
    from sf_factory.artifacts import register_artifact

    env = make_stage_env(db, config_dict, tmp_path)
    phase = fdb.get_phase(db.read(), "ph")
    assert phase is not None
    sidecar = env.worktree / "_factory" / "phases" / "ph" / "phase-plan.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text('{"stages": [], "dag_edges": []}', encoding="utf-8")
    with db.transaction() as conn:
        # What _step_planning writes at PLANNING -> CONTRACTS_FROZEN: the
        # registered sidecar ref AND the transition payload carrying 'commit'.
        register_artifact(
            conn,
            unit_level="phase",
            unit_id="ph",
            kind="phase_plan_sidecar",
            repo="workspace",
            repo_root=env.worktree,
            path=sidecar,
            git_commit="c0ffee0",
        )
        fdb.insert_event(
            conn,
            unit_level="phase",
            unit_id="ph",
            event_type="transition",
            actor="control_plane",
            from_state="PLANNING",
            to_state="CONTRACTS_FROZEN",
            payload={"contracts": 0, "commit": "c0ffee0"},
        )
    assert env.executor._contract_freeze_commit(phase) == "c0ffee0"

    with db.transaction() as conn:  # the phase-level post-rebase re-resolve
        updated = sched_mod._reresolve_artifact_commits(
            conn, "phase", "ph", env.worktree, "rebased1"
        )
    assert updated == 1
    ref = fdb.latest_artifact(db.read(), "phase", "ph", "phase_plan_sidecar")
    assert ref is not None and ref.git_commit == "rebased1"  # the ref DID move
    # ...but the anchor did not: the window still opens at contract freeze.
    assert env.executor._contract_freeze_commit(phase) == "c0ffee0"


async def test_cp1_inputs_bounded_to_registry_max_before_consult(
    db, config_dict, tmp_path
) -> None:
    """CP-1 caller-side bound (wave-3 fix round): a validation report larger
    than the registry's max_input_bytes consults TRUNCATED — the canonical
    payload (exactly what Consultor.consult measures for the §6 breach check)
    fits the bound, so a legitimately oversized input is an input-size fact,
    never a recorded cp_breach_attempt polluting the DoD §13 creep scan."""
    from sf_factory.consultation import _canonical_payload

    config_dict["consultation_points"][0]["max_input_bytes"] = 4096
    env = make_stage_env(db, config_dict, tmp_path)
    big = "failing=2 report\n" + ("validator evidence line\n" * 4096)

    def validator(cwd: Path, unit_id: str, resume) -> None:
        d = cwd / "_factory" / "stages" / unit_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "validation-report.md").write_text(big, encoding="utf-8")
        (d / "validation-report.json").write_text(
            json.dumps({"failing": 2, "passing": 3, "total": 5}), encoding="utf-8"
        )

    env.runner.behaviors["validator"] = validator
    env.consultor.verdicts = ["escalate"]
    await env.executor.execute("ph.s1")

    assert len(env.consultor.calls) == 1
    _, inputs = env.consultor.calls[0]
    assert set(inputs) == {"validation_report", "diff_digest", "spec"}
    assert len(_canonical_payload(inputs)) <= 4096  # the Consultor's measure
    assert inputs["validation_report"].startswith("failing=2 report")
    assert inputs["validation_report"].endswith("[truncated]")
    assert events_of(db, "ph.s1", "cp_breach_attempt") == []


async def test_validator_runs_isolated_and_only_reports_cross(
    db, config_dict, tmp_path
) -> None:
    """§3.1 Validator isolation: the validator runs in a '-validate' scratch
    checkout (new_branch=False) and only the two report files reach the stage
    worktree."""
    env = make_stage_env(db, config_dict, tmp_path)

    def validator(cwd: Path, unit_id: str, resume) -> None:
        validator_writing(0)(cwd, unit_id, resume)
        (cwd / "test_derived_internals.py").write_text("# secret tests", encoding="utf-8")

    env.runner.behaviors["validator"] = validator
    with pytest.raises(Exception):  # noqa: B017 — MERGE_GATE hits OPEN-2 (ConfigError)
        await env.executor.execute("ph.s1")

    assert env.wt.created[0] == ("ph.s1-validate", "stage/ph.s1", "stage/ph.s1", False)
    validator_call = [c for c in env.runner.calls if c.role == "validator"][0]
    assert validator_call.cwd == env.wt.scratch_root / "ph.s1-validate"
    assert (env.worktree / "_factory/stages/ph.s1/validation-report.md").is_file()
    assert (env.worktree / "_factory/stages/ph.s1/validation-report.json").is_file()
    assert not (env.worktree / "test_derived_internals.py").exists()
    assert env.wt.removed  # scratch cleaned up after the run
    # Validation passed (failing=0, routine has no audits) -> MERGE_GATE reached;
    # the explicit OPEN-2 ConfigError documents the unset suite, never a skip.
    assert stage_state(db, "ph.s1") is StageState.MERGE_GATE
    iteration = db.read().execute(
        "SELECT iteration, failing_tests FROM fix_iterations WHERE stage_id='ph.s1'"
    ).fetchone()
    assert (iteration["iteration"], iteration["failing_tests"]) == (1, 0)


async def test_build_asserts_validator_isolation(db, config_dict, tmp_path) -> None:
    """§3.1: a genuinely unregistered file trips the assertion even with
    ignorable droppings alongside — the isolation_ignore_globs filter must
    never mask a real leak (and the listing excludes the ignored entries)."""
    env = make_stage_env(db, config_dict, tmp_path)
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "BUILD")
    pycache = env.worktree / "__pycache__"
    pycache.mkdir()
    (pycache / "seed.cpython-312.pyc").write_bytes(b"\x00")
    (env.worktree / "leaked_validator_test.py").write_text("leak", encoding="utf-8")
    with pytest.raises(IntegrityError, match="Validator-isolation") as excinfo:
        await env.executor.execute("ph.s1")
    assert "leaked_validator_test.py" in str(excinfo.value)
    assert "__pycache__" not in str(excinfo.value)
    assert not env.runner.calls  # the Builder never spawned over a dirty worktree


async def test_build_isolation_ignores_build_test_droppings(
    db, config_dict, tmp_path
) -> None:
    """Bytecode/test-cache droppings left by the factory's own Tier-1 suite run
    (process.isolation_ignore_globs: __pycache__/, *.pyc, .pytest_cache/,
    .ruff_cache/) never trip the §3.1 assertion — including nested
    'tests/__pycache__/' porcelain entries, matched per path segment."""
    env = make_stage_env(db, config_dict, tmp_path)
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "BUILD")
    # Track tests/ so the droppings surface as '?? tests/__pycache__/' (the
    # live-incident porcelain shape), not as a collapsed '?? tests/'.
    tests_dir = env.worktree / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_seed.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "-A"], cwd=env.worktree, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "tests"],
        cwd=env.worktree,
        check=True,
        capture_output=True,
    )
    for dropping in ("__pycache__", "tests/__pycache__", ".pytest_cache", ".ruff_cache"):
        d = env.worktree / dropping
        d.mkdir()
        (d / "cache.bin").write_bytes(b"\x00")
    (env.worktree / "stray.pyc").write_bytes(b"\x00")

    env.runner.behaviors["builder_routine"] = builder_writing([0])
    env.runner.behaviors["validator"] = validator_writing(0)
    # No IntegrityError: BUILD runs and the conveyor reaches MERGE_GATE, where
    # the unset suite raises the explicit OPEN-2 ConfigError (never a skip).
    with pytest.raises(ConfigError, match="OPEN-2"):
        await env.executor.execute("ph.s1")
    assert env.runner.calls[0].role == "builder_routine"
    assert stage_state(db, "ph.s1") is StageState.MERGE_GATE


async def test_spec_declared_failure_escalates_with_cursor(
    db, config_dict, tmp_path
) -> None:
    """§5.4: sentinel -> event -> always-fire trigger -> escalation carrying the
    events.seq dedup cursor; never retried."""
    env = make_stage_env(db, config_dict, tmp_path)
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "SPEC")

    def spec_agent(cwd: Path, unit_id: str, resume) -> None:
        d = cwd / "_factory" / "stages" / unit_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "_DECLARED_FAILURE.md").write_text("cannot proceed", encoding="utf-8")

    env.runner.behaviors["spec_agent"] = spec_agent
    await env.executor.execute("ph.s1")

    assert stage_state(db, "ph.s1") is StageState.ESCALATED
    rows = open_escalations(db, "ph.s1")
    assert [r["trigger"] for r in rows] == ["agent_declared_failure"]
    sentinel_events = events_of(db, "ph.s1", "declared_failure")
    assert rows[0]["event_seq"] == sentinel_events[-1]["seq"]
    spec_calls = [c for c in env.runner.calls if c.role == "spec_agent"]
    assert len(spec_calls) == 1  # never blind-retried


async def test_escalation_resolution_routes_and_archives_sentinel(
    db, config_dict, tmp_path
) -> None:
    env = make_stage_env(db, config_dict, tmp_path)
    unit_dir = env.worktree / "_factory" / "stages" / "ph.s1"
    unit_dir.mkdir(parents=True)
    (unit_dir / "_DECLARED_FAILURE.md").write_text("stale sentinel", encoding="utf-8")
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "ESCALATED")
        esc_id = fdb.insert_escalation(
            conn,
            Escalation(
                id=None,
                unit_level="stage",
                unit_id="ph.s1",
                trigger="agent_declared_failure",
                target="phase_architect",
                payload_artifact_id=None,
                event_seq=None,
                status="open",
                resolution=None,
                created_at=utc_now(),
                resolved_at=None,
            ),
        )
    executor = env.executor
    await executor.execute("ph.s1")  # still open -> blocked, no movement
    assert stage_state(db, "ph.s1") is StageState.ESCALATED

    with db.transaction() as conn:
        fdb.resolve_escalation(conn, esc_id, "failed")
    await executor.execute("ph.s1")

    assert stage_state(db, "ph.s1") is StageState.FAILED
    assert not (unit_dir / "_DECLARED_FAILURE.md").exists()
    archived = unit_dir / f"_DECLARED_FAILURE.resolved-{esc_id}.md"
    assert archived.is_file()  # §5.4 archive: stale sentinel can never re-fire


async def test_validator_declared_failure_scratch_disposed_next_pass_completes(
    db, config_dict, tmp_path
) -> None:
    """§5.4 sentinel lifecycle regression (wave-3 fix round): a validator
    _DECLARED_FAILURE.md lands in the VALIDATE scratch worktree, so the
    escalation exit must DISPOSE the scratch — a leaked scratch is reused by
    create() with only TRACKED content re-synced, so the untracked stale
    sentinel would be re-detected after EVERY later validator run (a NEW
    events.seq past the escalations.event_seq cursor) and the stage could
    never pass VALIDATE again."""
    env = make_stage_env(db, config_dict, tmp_path)
    calls = [0]

    def validator(cwd: Path, unit_id: str, resume) -> None:
        calls[0] += 1
        if calls[0] == 1:
            d = cwd / "_factory" / "stages" / unit_id
            d.mkdir(parents=True, exist_ok=True)
            (d / "_DECLARED_FAILURE.md").write_text("cannot test", encoding="utf-8")
        else:
            validator_writing(0)(cwd, unit_id, resume)

    env.runner.behaviors["validator"] = validator
    await env.executor.execute("ph.s1")

    assert stage_state(db, "ph.s1") is StageState.ESCALATED
    (esc,) = open_escalations(db, "ph.s1")
    assert esc["trigger"] == "agent_declared_failure"
    scratch = env.wt.scratch_root / "ph.s1-validate"
    assert env.wt.removed == [scratch]  # disposed on the ESCALATION exit too
    assert not scratch.exists()

    with db.transaction() as conn:
        fdb.resolve_escalation(conn, esc["id"], "rework:VALIDATE")
    # Next pass: the re-created scratch is clean, the validator succeeds and
    # the stage must leave VALIDATE (pre-fix: the stale sentinel survived the
    # scratch reuse, re-fired the always-fire trigger and re-escalated).
    with pytest.raises(Exception):  # noqa: B017 — MERGE_GATE hits OPEN-2 (ConfigError)
        await env.executor.execute("ph.s1")

    assert calls[0] == 2
    assert stage_state(db, "ph.s1") is StageState.MERGE_GATE
    assert len(events_of(db, "ph.s1", "declared_failure")) == 1  # never re-detected
    assert open_escalations(db, "ph.s1") == []


async def test_escalation_to_awaiting_human_creates_decision_atomically(
    db, config_dict, tmp_path
) -> None:
    """§9.4: 'awaiting_human' resolution -> AWAITING_HUMAN with the
    escalation-tradeoff decision request inserted in the SAME tx — the gate can
    never sit with only a stale answered decision to consume. Since CCR-3/D-0017
    the request anchors a ROMANIAN request-wrapper artifact (committed BEFORE
    the recording tx) that links the payload via /artifact/<ref> and carries NO
    recommendation line (genuine trade-off)."""
    env = make_stage_env(db, config_dict, tmp_path)
    unit_dir = env.worktree / "_factory" / "stages" / "ph.s1"
    unit_dir.mkdir(parents=True)
    (unit_dir / "spec.md").write_text("spec", encoding="utf-8")
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "ESCALATED")
        from sf_factory.artifacts import register_artifact

        spec_ref = register_artifact(
            conn,
            unit_level="stage",
            unit_id="ph.s1",
            kind="spec",
            repo="workspace",
            repo_root=env.worktree,
            path=unit_dir / "spec.md",
            git_commit=None,
        )
        esc_id = fdb.insert_escalation(
            conn,
            Escalation(
                id=None,
                unit_level="stage",
                unit_id="ph.s1",
                trigger="cp1_verdict",
                target="phase_architect",
                payload_artifact_id=None,
                event_seq=None,
                status="open",
                resolution=None,
                created_at=utc_now(),
                resolved_at=None,
            ),
        )
    with db.transaction() as conn:
        fdb.resolve_escalation(conn, esc_id, "awaiting_human")
    await env.executor.execute("ph.s1")

    assert stage_state(db, "ph.s1") is StageState.AWAITING_HUMAN
    pending = fdb.pending_decisions(db.read())
    assert len(pending) == 1 and pending[0].gate_kind == "escalation_tradeoff"
    assert pending[0].unit_id == "ph.s1"
    assert [p for p in env.notify.published if "escaladare" in p[0]]

    # §2a wrapper: registered kind='decision_request', committed, Romanian,
    # payload LINKED (here the spec fallback anchor), no Recomandare line.
    row = (
        db.read()
        .execute(
            "SELECT * FROM artifact_refs WHERE id = ?",
            (pending[0].request_artifact_id,),
        )
        .fetchone()
    )
    assert row["kind"] == "decision_request"
    assert row["path"].endswith("decision-request-escalation.md")
    assert row["git_commit"]  # committed BEFORE the recording tx (§7 order)
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", row["path"]],
        cwd=env.worktree,
        capture_output=True,
    )
    assert tracked.returncode == 0
    body = (env.worktree / row["path"]).read_text(encoding="utf-8")
    assert "Cerere de decizie" in body
    assert "compromis de produs la escaladare (escalation_tradeoff)" in body
    assert f"/artifact/{spec_ref.id}" in body
    for token in ("approved", "rework:BUILD", "rework:SPEC"):
        assert f"- {token} — " in body  # every declared option + consequence
    assert "Recomandare:" not in body  # never invented for a genuine trade-off


async def test_stage_tradeoff_wrapper_replay_is_idempotent(
    db, config_dict, tmp_path
) -> None:
    """§5.5d at-least-once replay: re-running the resolution step re-writes a
    byte-identical wrapper (commit_paths -> None) and pins the ref to HEAD —
    never a NULL commit, never a duplicate page-killing failure."""
    env = make_stage_env(db, config_dict, tmp_path)
    unit_dir = env.worktree / "_factory" / "stages" / "ph.s1"
    unit_dir.mkdir(parents=True)
    (unit_dir / "spec.md").write_text("spec", encoding="utf-8")
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "ESCALATED")
        from sf_factory.artifacts import register_artifact

        register_artifact(
            conn,
            unit_level="stage",
            unit_id="ph.s1",
            kind="spec",
            repo="workspace",
            repo_root=env.worktree,
            path=unit_dir / "spec.md",
            git_commit=None,
        )
        esc_id = fdb.insert_escalation(
            conn,
            Escalation(
                id=None,
                unit_level="stage",
                unit_id="ph.s1",
                trigger="cp1_verdict",
                target="phase_architect",
                payload_artifact_id=None,
                event_seq=None,
                status="open",
                resolution=None,
                created_at=utc_now(),
                resolved_at=None,
            ),
        )
    with db.transaction() as conn:
        fdb.resolve_escalation(conn, esc_id, "awaiting_human")
    await env.executor.execute("ph.s1")
    # Simulate the crash-replay: back to ESCALATED, the wrapper file already
    # committed; the step must converge (commit_paths None -> rev-parse HEAD).
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "ESCALATED")
    await env.executor.execute("ph.s1")

    assert stage_state(db, "ph.s1") is StageState.AWAITING_HUMAN
    rows = (
        db.read()
        .execute(
            "SELECT dr.id, ar.git_commit FROM decision_requests dr"
            " JOIN artifact_refs ar ON ar.id = dr.request_artifact_id"
            " WHERE dr.unit_id = 'ph.s1' ORDER BY dr.id"
        )
        .fetchall()
    )
    assert len(rows) == 2  # one pending request per resolution pass
    assert all(r["git_commit"] for r in rows)  # never NULL (D-0015 contract)


async def test_context_budget_reset_then_escalate_ladder(
    db, config_dict, tmp_path
) -> None:
    """§2 context_budget: first firing within max_context_resets = a
    state-preserving reset (and the NEXT iteration must not resume a session —
    verdict_downgraded); the firing past the allowance escalates."""
    env = make_stage_env(db, config_dict, tmp_path)
    env.runner.behaviors["validator"] = validator_writing(failing=2)
    env.runner.behaviors["builder_routine"] = builder_writing([0])
    env.consultor.verdicts = ["continue_session"]
    with db.transaction() as conn:
        proc_id = fdb.insert_process(
            conn,
            ProcessRecord(
                id=None,
                unit_level="stage",
                unit_id="ph.s1",
                kind="agent",
                role="builder_routine",
                cp_id=None,
                session_id=None,
                pid=None,
                cmdline="stub",
                cwd=None,
                state="spawned",
                exit_code=None,
                ndjson_log_path=None,
                spawned_at=utc_now(),
                heartbeat_at=None,
                ended_at=None,
            ),
        )
        fdb.finalize_process(
            conn, proc_id, state="exited", exit_code=0, ended_at=utc_now(),
            session_id="sess-1",
        )
        # Burn the whole routine budget (conftest: 10000 tokens).
        fdb.insert_token_usage(
            conn,
            process_id=proc_id,
            unit_level="stage",
            unit_id="ph.s1",
            role="builder_routine",
            model="stub-model",
            tokens_in=9000,
            tokens_out=1000,
            cost_usd=None,
        )
    await env.executor.execute("ph.s1")

    # Iteration 1: budget fired -> reset (no escalation), CP-1 still consulted,
    # continue_session granted but downgraded by the pending reset.
    resets = events_of(db, "ph.s1", "context_reset")
    assert len(resets) == 1
    downgrades = events_of(db, "ph.s1", "verdict_downgraded")
    assert len(downgrades) == 1
    assert json.loads(downgrades[0]["payload_json"])["reason"] == "context_reset"
    builder_calls = [c for c in env.runner.calls if c.role == "builder_routine"]
    assert len(builder_calls) == 1 and builder_calls[0].resume_session is None
    # The fresh-context builder run consumed the reset; the budget is still
    # blown at the next evaluation -> reset allowance (1) exhausted -> escalate
    # (no further CP-1 consult: exactly one in total).
    assert len(env.consultor.calls) == 1
    assert stage_state(db, "ph.s1") is StageState.ESCALATED
    assert [r["trigger"] for r in open_escalations(db, "ph.s1")] == ["context_budget"]


async def test_phase_signoff_approved_integrates_and_completes(
    db, config_dict, tmp_path
) -> None:
    """AWAITING_SIGNOFF + founder 'approved' -> integrate into the project
    branch, phase DONE, worktree removed."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph", PhaseState.AWAITING_SIGNOFF)
    with db.transaction() as conn:
        fdb.insert_decision_request(
            conn,
            DecisionRequest(
                id=None,
                unit_level="phase",
                unit_id="ph",
                gate_kind="phase_signoff",
                request_artifact_id=_seed_artifact(conn, "ph"),
                status="pending",
                answer=None,
                answer_artifact_id=None,
                created_at=utc_now(),
                alerted_at=None,
                answered_at=None,
            ),
        )
        fdb.answer_decision(conn, 1, "approved", None)

    class IntegratingFake(FakeWorktrees):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.integrated: list[tuple[str, str]] = []

        async def integrate(self, repo_root, branch, target_branch):
            self.integrated.append((branch, target_branch))
            return "merge-sha"

    wt = IntegratingFake(tmp_path / "scratch")
    executor = make_phase_executor(db, cfg, wt=wt)
    await executor.execute("ph")

    assert phase_state(db, "ph") is PhaseState.DONE
    assert wt.integrated == [("phase/ph", "main")]
    assert wt.removed  # phase checkout cleaned up after DONE
    payload = json.loads(events_of(db, "ph", "transition")[-1]["payload_json"])
    assert payload["merge_commit"] == "merge-sha"


def test_gate_answers_is_the_one_executor_vocabulary() -> None:
    """CCR-3 pin: the executors' accepted answers EQUAL models.GATE_ANSWERS —
    the same object the dashboard renders as buttons (Doctrine §9 one source);
    the `changes_requested` alias is gone from the vocabulary."""
    from sf_factory.models import GATE_ANSWERS

    assert set(sched_mod._STAGE_ANSWER_TARGETS) == set(
        GATE_ANSWERS[("stage", "critical_stage")]
    )
    assert set(sched_mod._STAGE_ANSWER_TARGETS) == set(
        GATE_ANSWERS[("stage", "escalation_tradeoff")]
    )
    assert set(sched_mod._PHASE_ANSWER_TARGETS) == set(
        GATE_ANSWERS[("phase", "escalation_tradeoff")]
    )
    assert GATE_ANSWERS[("phase", "phase_signoff")] == ("approved", "changes")
    for options in GATE_ANSWERS.values():
        assert "changes_requested" not in options


async def test_critical_gate_decision_request_is_romanian_with_recommendation(
    db, config_dict, tmp_path
) -> None:
    """§2a re-authored control-plane template: the critical_stage request the
    founder sees is Romanian, glosses the gate kind and every option with its
    consequence, and carries the mechanical machine-readable `Recomandare:
    approved` marker (R3 contract) — committed before the recording tx."""
    env = make_stage_env(db, config_dict, tmp_path, risk="critical")
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "AUDIT")

    def clean_auditor(role: str):
        def behavior(cwd: Path, unit_id: str, resume) -> None:
            d = cwd / "_factory" / "stages" / unit_id
            d.mkdir(parents=True, exist_ok=True)
            (d / f"audit-{role}.md").write_text("clean\n", encoding="utf-8")
            (d / f"audit-{role}.json").write_text(
                json.dumps({"findings": []}), encoding="utf-8"
            )

        return behavior

    env.runner.behaviors["auditor_same_model"] = clean_auditor("auditor_same_model")
    env.runner.behaviors["auditor_cross_model"] = clean_auditor("auditor_cross_model")
    await env.executor.execute("ph.s1")

    assert stage_state(db, "ph.s1") is StageState.AWAITING_HUMAN
    (pending,) = fdb.pending_decisions(db.read())
    assert pending.gate_kind == "critical_stage"
    row = (
        db.read()
        .execute(
            "SELECT * FROM artifact_refs WHERE id = ?", (pending.request_artifact_id,)
        )
        .fetchone()
    )
    assert row["git_commit"]  # committed BEFORE the recording tx (§7 order)
    body = (env.worktree / row["path"]).read_text(encoding="utf-8")
    assert "Cerere de decizie" in body and "Întrebare" in body
    assert "etapă critică — aprobare necesară (critical_stage)" in body
    assert "risc critic (critical)" in body  # risk class glossed, never bare
    for token in ("approved", "rework:BUILD", "rework:SPEC"):
        assert f"- {token} — " in body  # option + one-line consequence
    assert "Recomandare: approved" in body  # R3 machine-readable marker
    assert "Answer with one of" not in body  # the English template is gone
    assert "findings closed" not in body  # internal reasons stay in the journal


async def test_signoff_request_is_romanian_with_recommendation(
    db, config_dict, tmp_path
) -> None:
    """§2a re-authored phase_signoff template (driven through the real
    _enter_signoff path)."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph", PhaseState.INTEGRATING)
    worktree = tmp_path / "phase-wt"
    init_repo(worktree)
    executor = make_phase_executor(db, cfg)
    await executor._enter_signoff(fdb.get_phase(db.read(), "ph"), worktree)

    assert phase_state(db, "ph") is PhaseState.AWAITING_SIGNOFF
    (pending,) = fdb.pending_decisions(db.read())
    assert pending.gate_kind == "phase_signoff"
    row = (
        db.read()
        .execute(
            "SELECT * FROM artifact_refs WHERE id = ?", (pending.request_artifact_id,)
        )
        .fetchone()
    )
    assert row["git_commit"]
    body = (worktree / row["path"]).read_text(encoding="utf-8")
    assert "semnătură de fază (phase_signoff)" in body
    assert "- approved — " in body and "- changes — " in body
    assert "changes_requested" not in body  # the alias is OUT of the vocabulary
    assert "Recomandare: approved" in body
    assert "All stages DONE" not in body  # the English template is gone


async def test_signoff_changes_requested_alias_is_refused(db, config_dict) -> None:
    """CCR-3 deliberate behavioral edit (D-0017 rider 4): the signoff executor
    no longer tolerates `changes_requested` — unknown-answer alert naming the
    GATE_ANSWERS vocabulary, phase stays blocked; plain `changes` still works."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph", PhaseState.AWAITING_SIGNOFF)
    with db.transaction() as conn:
        ref_id = _seed_artifact(conn, "ph")
        fdb.insert_decision_request(
            conn,
            DecisionRequest(
                id=None,
                unit_level="phase",
                unit_id="ph",
                gate_kind="phase_signoff",
                request_artifact_id=ref_id,
                status="pending",
                answer=None,
                answer_artifact_id=None,
                created_at=utc_now(),
                alerted_at=None,
                answered_at=None,
            ),
        )
        fdb.answer_decision(conn, 1, "changes_requested", None)
    executor = make_phase_executor(db, cfg)
    await executor.execute("ph")

    assert phase_state(db, "ph") is PhaseState.AWAITING_SIGNOFF  # refused, blocked
    alerts = events_of(db, "ph", "alert")
    payload = json.loads(alerts[-1]["payload_json"])
    assert payload["kind"] == "unknown_decision_answer"
    assert payload["answer"] == "changes_requested"
    assert payload["known"] == ["approved", "changes"]

    with db.transaction() as conn:  # the in-vocabulary token still routes
        fdb.insert_decision_request(
            conn,
            DecisionRequest(
                id=None,
                unit_level="phase",
                unit_id="ph",
                gate_kind="phase_signoff",
                request_artifact_id=ref_id,
                status="pending",
                answer=None,
                answer_artifact_id=None,
                created_at=utc_now(),
                alerted_at=None,
                answered_at=None,
            ),
        )
        fdb.answer_decision(conn, 2, "changes", None)
    await executor.execute("ph")
    assert phase_state(db, "ph") is PhaseState.RUNNING


async def test_phase_tradeoff_wrapper_links_payload_no_recommendation(
    db, config_dict, tmp_path
) -> None:
    """§2a phase escalation_tradeoff wrapper: Romanian, resume/replan options
    with consequences, /artifact/<ref> link to the anchor, no recommendation."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph", PhaseState.ESCALATED)
    worktree = Path(cfg.projects["proj"].worktrees_dir) / "ph"
    init_repo(worktree)
    plan_path = worktree / "_factory" / "phases" / "ph" / "phase-plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# plan\n", encoding="utf-8")
    with db.transaction() as conn:
        from sf_factory.artifacts import register_artifact

        plan_ref = register_artifact(
            conn,
            unit_level="phase",
            unit_id="ph",
            kind="phase_plan",
            repo="workspace",
            repo_root=worktree,
            path=plan_path,
            git_commit=None,
        )
        esc_id = fdb.insert_escalation(
            conn,
            Escalation(
                id=None,
                unit_level="phase",
                unit_id="ph",
                trigger="internal_error",
                target="main_architect",
                payload_artifact_id=None,
                event_seq=None,
                status="open",
                resolution=None,
                created_at=utc_now(),
                resolved_at=None,
            ),
        )
    with db.transaction() as conn:
        fdb.resolve_escalation(conn, esc_id, "awaiting_human")
    executor = make_phase_executor(db, cfg)
    await executor.execute("ph")

    assert phase_state(db, "ph") is PhaseState.AWAITING_HUMAN
    (pending,) = fdb.pending_decisions(db.read())
    assert pending.gate_kind == "escalation_tradeoff"
    row = (
        db.read()
        .execute(
            "SELECT * FROM artifact_refs WHERE id = ?", (pending.request_artifact_id,)
        )
        .fetchone()
    )
    assert row["path"].endswith("decision-request-escalation.md")
    assert row["git_commit"]
    body = (worktree / row["path"]).read_text(encoding="utf-8")
    assert "compromis de produs la escaladare (escalation_tradeoff)" in body
    assert "- resume — " in body and "- replan — " in body
    assert f"/artifact/{plan_ref.id}" in body
    assert "Recomandare:" not in body


async def test_audit_contest_logs_and_escalates(db, config_dict, tmp_path) -> None:
    """DoD §7: dual audit -> executor triages the union; a contested finding is
    logged with its rationale artifact and escalates unresolved."""
    env = make_stage_env(db, config_dict, tmp_path, risk="structural")
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "AUDIT")

    def auditor(ref: str):
        def behavior(cwd: Path, unit_id: str, resume) -> None:
            d = cwd / "_factory" / "stages" / unit_id
            d.mkdir(parents=True, exist_ok=True)
            role = ref.split(":")[0]
            (d / f"audit-{role}.md").write_text(f"finding {ref}", encoding="utf-8")
            (d / f"audit-{role}.json").write_text(
                json.dumps(
                    {"findings": [{"ref": ref, "severity": "major", "summary": "s"}]}
                ),
                encoding="utf-8",
            )

        return behavior

    env.runner.behaviors["auditor_same_model"] = auditor("auditor_same_model:F1")
    env.runner.behaviors["auditor_cross_model"] = auditor("auditor_cross_model:F2")

    def responder(cwd: Path, unit_id: str, resume) -> None:
        d = cwd / "_factory" / "stages" / unit_id
        (d / "findings-response.json").write_text(
            json.dumps(
                {
                    "responses": [
                        {
                            "ref": "auditor_same_model:F1",
                            "action": "contest",
                            "rationale": "spec says otherwise",
                        },
                        {
                            "ref": "auditor_cross_model:F2",
                            "action": "comply",
                            "rationale": "will fix",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

    env.runner.behaviors["builder_heavy"] = responder
    await env.executor.execute("ph.s1")

    assert stage_state(db, "ph.s1") is StageState.ESCALATED
    rows = open_escalations(db, "ph.s1")
    assert [r["trigger"] for r in rows] == ["unresolved_contest"]
    findings = {f.finding_ref: f for f in fdb.findings(db.read(), "ph.s1")}
    assert findings["auditor_same_model:F1"].status == "contested"
    assert findings["auditor_same_model:F1"].contest_artifact_id is not None
    assert findings["auditor_cross_model:F2"].status == "complied"
    assert findings["auditor_cross_model:F2"].resolved_by == "executor"


# ------------------------------------------------------------- recover() §5.5


def make_recovery_scheduler(db, cfg, notify=None) -> tuple[Scheduler, FakeNotify]:
    notify = notify or FakeNotify()
    sm = StateMachine(db)
    executors = {
        Level.PHASE: ScriptedExecutor(Level.PHASE, db, sm),
        Level.STAGE: ScriptedExecutor(Level.STAGE, db, sm),
    }
    return Scheduler(db, sm, cfg, executors, notify), notify


def test_recover_orphan_sweep_kills_group_and_marks(db, config_dict) -> None:
    """§5.5a: spawned/running rows -> process group SIGKILLed (pid + cmdline
    match), rows marked 'orphaned' + event."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.BUILD)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True
    )
    cmdline = f"{sys.executable} -c 'import time; time.sleep(600)'"
    try:
        with db.transaction() as conn:
            proc_id = fdb.insert_process(
                conn,
                ProcessRecord(
                    id=None,
                    unit_level="stage",
                    unit_id="s1",
                    kind="agent",
                    role="builder_routine",
                    cp_id=None,
                    session_id=None,
                    pid=None,
                    cmdline=cmdline,
                    cwd=None,
                    state="spawned",
                    exit_code=None,
                    ndjson_log_path=None,
                    spawned_at=utc_now(),
                    heartbeat_at=None,
                    ended_at=None,
                ),
            )
            fdb.mark_process_running(conn, proc_id, pid=child.pid, at=utc_now())
        scheduler, _ = make_recovery_scheduler(db, cfg)
        report = scheduler.recover()

        assert isinstance(report, RecoveryReport)
        assert report.orphaned == (proc_id,)
        assert report.killed_groups == (child.pid,)
        assert child.wait(timeout=10) == -9  # SIGKILL reached the group
        row = db.read().execute(
            "SELECT state FROM process_registry WHERE id=?", (proc_id,)
        ).fetchone()
        assert row["state"] == "orphaned"
        assert events_of(db, "s1", "orphaned")
        assert "stage:s1" in report.requeued  # §5.5d: BUILD re-enters the queue
        assert Path(cfg.process.liveness_file).is_file()  # touched during the scan
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_recover_pid_reuse_is_never_killed(db, config_dict) -> None:
    cfg = make_config(config_dict)
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.BUILD)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True
    )
    try:
        with db.transaction() as conn:
            proc_id = fdb.insert_process(
                conn,
                ProcessRecord(
                    id=None,
                    unit_level="stage",
                    unit_id="s1",
                    kind="agent",
                    role="builder_routine",
                    cp_id=None,
                    session_id=None,
                    pid=None,
                    cmdline="claude --model x -p prompt",  # NOT the live cmdline
                    cwd=None,
                    state="spawned",
                    exit_code=None,
                    ndjson_log_path=None,
                    spawned_at=utc_now(),
                    heartbeat_at=None,
                    ended_at=None,
                ),
            )
            fdb.mark_process_running(conn, proc_id, pid=child.pid, at=utc_now())
        scheduler, _ = make_recovery_scheduler(db, cfg)
        report = scheduler.recover()

        assert report.killed_groups == ()  # foreign cmdline: never killed
        assert child.poll() is None  # still alive
        assert report.orphaned == (proc_id,)  # but the row never wedges
    finally:
        child.kill()
        child.wait()


def test_recover_resets_dirty_worktree_with_evidence(db, config_dict, tmp_path) -> None:
    """§5.5b: dirty unit worktree -> evidence diff saved + event, then
    hard-reset + clean -fd to the committed step state."""
    cfg = make_config(config_dict)
    worktree = tmp_path / "wt-s1"
    init_repo(worktree)
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.BUILD, worktree=worktree)
    (worktree / "seed.txt").write_text("orphan kept writing\n", encoding="utf-8")
    (worktree / "untracked.tmp").write_text("junk", encoding="utf-8")

    scheduler, _ = make_recovery_scheduler(db, cfg)
    report = scheduler.recover()

    assert str(worktree) in report.dirty_reset
    assert (worktree / "seed.txt").read_text(encoding="utf-8") == "seed\n"
    assert not (worktree / "untracked.tmp").exists()
    assert str(worktree) in report.healed
    events = events_of(db, "s1", "dirty_worktree_reset")
    assert len(events) == 1
    evidence = Path(json.loads(events[0]["payload_json"])["evidence"])
    assert evidence.is_file()
    text = evidence.read_text(encoding="utf-8")
    assert "orphan kept writing" in text and "untracked.tmp" in text


def test_recover_aborts_on_integrity_failure(db, config_dict, tmp_path) -> None:
    """§5.5c: a non-terminal-unit artifact mismatch aborts the start with an
    alert — no silent repair."""
    cfg = make_config(config_dict)
    workspace = Path(cfg.projects["proj"].workspace)
    init_repo(workspace)
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.BUILD)
    from sf_factory.models import ArtifactRef

    with db.transaction() as conn:
        fdb.insert_artifact_ref(
            conn,
            ArtifactRef(
                id=None,
                unit_level="stage",
                unit_id="s1",
                kind="spec",
                repo="workspace",
                path="_factory/stages/s1/spec.md",
                sha256="ab" * 32,  # resolves nowhere
                git_commit=None,
                created_at=utc_now(),
            ),
        )
    notify = FakeNotify()
    scheduler, _ = make_recovery_scheduler(db, cfg, notify=notify)
    with pytest.raises(IntegrityError, match="integrity"):
        scheduler.recover()
    failures = (
        db.read().execute("SELECT * FROM events WHERE event_type='integrity_failure'")
    ).fetchall()
    assert len(failures) == 1
    assert any("integritatea" in title for title, _, _ in notify.published)


def test_recover_clean_state_reports_requeue_only(db, config_dict, tmp_path) -> None:
    cfg = make_config(config_dict)
    insert_phase(db, "ph", PhaseState.RUNNING)
    insert_stage(db, "s1", "ph", StageState.AWAITING_HUMAN)
    insert_stage(db, "s2", "ph", StageState.VALIDATE)
    scheduler, _ = make_recovery_scheduler(db, cfg)
    report = scheduler.recover()

    # RUNNING-category units re-enter the queue; AWAITING_* stay blocked (§5.5d).
    assert set(report.requeued) == {"phase:ph", "stage:s2"}
    assert report.integrity_checked == 0 and report.heal_errors == ()


def test_recover_failed_heal_persists_heal_failed_alert(
    db, config_dict, tmp_path
) -> None:
    """§6 fail-explicit at §5.5b: a failed git heal must leave durable evidence
    at recovery time — an 'alert' event with kind='heal_failed' — because
    RecoveryReport.heal_errors is in-memory only (cli.cmd_run discards
    recover()'s return value)."""
    cfg = make_config(config_dict)
    not_a_repo = tmp_path / "wt-broken"
    not_a_repo.mkdir()  # exists on disk, but holds no git state to heal
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.BUILD, worktree=not_a_repo)

    scheduler, _ = make_recovery_scheduler(db, cfg)
    report = scheduler.recover()

    assert len(report.heal_errors) == 1
    assert str(not_a_repo) in report.heal_errors[0]
    (event,) = events_of(db, "s1", "alert")
    assert event["unit_level"] == "stage"
    assert event["actor"] == "control_plane"
    payload = json.loads(event["payload_json"])
    assert payload["kind"] == "heal_failed"
    assert payload["path"] == str(not_a_repo)
    assert "not a git worktree" in payload["error"]


# ----------------- phase-seeding design §4/§5/§5b (Phase-Architect context,
# ----------------- out-of-bounds detector, proving-phases dispatch hold)


def _mk_phase(
    phase_id: str, state: PhaseState, *, project: str = "proj"
) -> Phase:
    now = utc_now()
    return Phase(phase_id, project, f"Phase {phase_id}", state, None, None, now, now)


def _phase_executor(db, cfg, tmp_path: Path) -> PhaseExecutor:
    return PhaseExecutor(
        db, StateMachine(db), cfg, FakeRunner(), FakeWorktrees(tmp_path), FakeNotify()
    )


def test_planning_prompt_context_block_present_iff_project_md(
    db, config_dict, tmp_path
) -> None:
    """Design §4: the project-context block appears when projects.<p>.project_md
    is set — docs_repo abs path, <factory.home>/<project_md> path, contracts
    READ-ONLY note, intra-phase namespace instruction."""
    config_dict["projects"]["proj"]["project_md"] = "docs/projects/proj/PROJECT.md"
    cfg = make_config(config_dict)
    prompt = _phase_executor(db, cfg, tmp_path)._planning_prompt(
        _mk_phase("ph1", PhaseState.PLANNING)
    )
    # The driver/role marker stays the prompt's first line (agent_driver contract).
    assert prompt.startswith("You are the Phase Architect for phase 'ph1'")
    assert "Project context (read before planning):" in prompt
    assert config_dict["projects"]["proj"]["docs_repo"] in prompt  # abs docs_repo
    home = Path(config_dict["factory"]["home"])
    assert str(home / "docs/projects/proj/PROJECT.md") in prompt
    assert "READ-ONLY" in prompt and "_CONTRACT_CHANGE_REQUEST.md" in prompt
    assert "Write YOUR intra-phase contracts under _factory/contracts/phase-ph1/" in prompt


def test_planning_prompt_block_absent_for_b8_style_project(
    db, config_dict, tmp_path
) -> None:
    """Design §4: project_md is None (the conftest/b8 shape) ⇒ the block is fully
    absent — but the existing freeze line is amended to the namespaced path
    unconditionally."""
    cfg = make_config(config_dict)  # conftest 'proj' has no project_md key
    assert cfg.projects["proj"].project_md is None
    prompt = _phase_executor(db, cfg, tmp_path)._planning_prompt(
        _mk_phase("ph1", PhaseState.PLANNING)
    )
    assert "Project context" not in prompt
    assert "Business documentation" not in prompt
    assert "READ-ONLY" not in prompt
    # Namespaced intra-phase contracts dir (design §4 amendment), not the root:
    assert "as files under _factory/contracts/phase-ph1/ BEFORE any fan-out" in prompt
    assert "as files under _factory/contracts/ BEFORE" not in prompt


# ------------------------------------------------------- §5b proving-phases hold


def test_proving_held_phase_ids_semantics(config_dict) -> None:
    """The §5b pure predicate: held while ANY listed proving row is non-DONE;
    proving phases themselves never held; release on all-DONE; only EXISTING
    rows gate; PENDING-scoped (a dispatch filter, not a state)."""
    cfg = make_config(config_dict)  # proj: proving_phases == ["foundation"]
    held = sched_mod.proving_held_phase_ids
    pending = _mk_phase("ph-x", PhaseState.PENDING)

    # Held while the proving phase is non-DONE (PENDING / RUNNING / even FAILED).
    for state in (PhaseState.PENDING, PhaseState.RUNNING, PhaseState.FAILED):
        assert held(cfg, [_mk_phase("foundation", state), pending]) == {"ph-x"}
    # The proving phase itself is never held.
    assert "foundation" not in held(
        cfg, [_mk_phase("foundation", PhaseState.PENDING), pending]
    )
    # Released once every proving phase is DONE — the DAG governs alone.
    assert held(cfg, [_mk_phase("foundation", PhaseState.DONE), pending]) == frozenset()
    # A listed-but-unseeded proving id holds nothing (b8/pre-seed states).
    assert held(cfg, [pending]) == frozenset()
    # Non-PENDING units are not "held" — the hold is a dispatch filter only.
    assert held(
        cfg,
        [_mk_phase("foundation", PhaseState.RUNNING), _mk_phase("ph-x", PhaseState.RUNNING)],
    ) == frozenset()
    # Unknown project: no config, no hold.
    assert held(cfg, [_mk_phase("g1", PhaseState.PENDING, project="ghost")]) == frozenset()


def test_proving_held_empty_list_means_no_hold(config_dict) -> None:
    config_dict["projects"]["proj"]["proving_phases"] = []
    cfg = make_config(config_dict)
    phases = [_mk_phase("foundation", PhaseState.FAILED), _mk_phase("ph-x", PhaseState.PENDING)]
    assert sched_mod.proving_held_phase_ids(cfg, phases) == frozenset()


async def test_proving_hold_blocks_dispatch_while_proving_non_done(
    db, config_dict
) -> None:
    """Loop-level §5b: a deps-free PENDING phase outside proving_phases is NOT
    dispatched while the proving phase is non-DONE — it stays PENDING (no
    transition), invisible to RUNNABLE selection."""
    cfg = make_config(config_dict)
    insert_phase(db, "foundation", PhaseState.FAILED)  # non-DONE, terminal: never redispatched
    insert_phase(db, "ph-x", PhaseState.PENDING)
    executor = ScriptedExecutor(level=Level.PHASE, db=db, sm=StateMachine(db))
    scheduler, _ = make_scheduler(db, cfg, {Level.PHASE: executor})
    await run_blocked(scheduler)
    assert ("phase", "ph-x") not in executor.started
    assert phase_state(db, "ph-x") is PhaseState.PENDING


async def test_proving_hold_releases_when_proving_done(db, config_dict) -> None:
    cfg = make_config(config_dict)
    insert_phase(db, "foundation", PhaseState.DONE)
    insert_phase(db, "ph-x", PhaseState.PENDING)
    executor = ScriptedExecutor(level=Level.PHASE, db=db, sm=StateMachine(db))
    scheduler, _ = make_scheduler(db, cfg, {Level.PHASE: executor})
    await run_blocked(scheduler)
    assert ("phase", "ph-x") in executor.started
    assert phase_state(db, "ph-x") is PhaseState.DONE


async def test_proving_hold_empty_list_dispatches_normally(db, config_dict) -> None:
    config_dict["projects"]["proj"]["proving_phases"] = []
    cfg = make_config(config_dict)
    insert_phase(db, "foundation", PhaseState.FAILED)  # would gate if it were listed
    insert_phase(db, "ph-x", PhaseState.PENDING)
    executor = ScriptedExecutor(level=Level.PHASE, db=db, sm=StateMachine(db))
    scheduler, _ = make_scheduler(db, cfg, {Level.PHASE: executor})
    await run_blocked(scheduler)
    assert ("phase", "ph-x") in executor.started
    assert phase_state(db, "ph-x") is PhaseState.DONE


# ------------------------------------------------ §5 out-of-bounds detector


def _git_factory_home(home: Path) -> None:
    """factory.home as a git repo for detector tests; the conftest db files and
    the .factory operational tree are gitignored so only deliberate dirt shows."""
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "factory@test"],
        ["git", "config", "user.name", "factory"],
    ):
        subprocess.run(args, cwd=home, check=True, capture_output=True)
    (home / ".gitignore").write_text("factory.db*\n.factory/\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "--", ".gitignore"], cwd=home, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "home seed"], cwd=home, check=True, capture_output=True
    )


def _oob_alerts(db, event_type: str = "alert") -> list[dict]:
    rows = (
        db.read()
        .execute("SELECT * FROM events WHERE event_type = ? ORDER BY seq", (event_type,))
        .fetchall()
    )
    return [
        dict(row)
        for row in rows
        if json.loads(row["payload_json"]).get("kind") == "out_of_bounds"
    ]


def test_recover_out_of_bounds_dirt_alerts_once_per_streak(
    db, config_dict, tmp_path
) -> None:
    """Design §5: unexpected dirt in the factory repo at recover() → ONE 'alert'
    event (kind=out_of_bounds, repo, paths) + ONE ntfy at alert priority per
    consecutive-dirty streak; a clean observation clears the latch and future
    dirt re-alerts — never a silent pass, never a page-per-tick."""
    home = Path(config_dict["factory"]["home"])
    _git_factory_home(home)
    rogue = home / "rogue.txt"
    rogue.write_text("foreign write outside any worktree\n", encoding="utf-8")
    cfg = make_config(config_dict)
    scheduler, notify = make_scheduler(db, cfg, {})

    scheduler.recover()
    (alert,) = _oob_alerts(db)
    payload = json.loads(alert["payload_json"])
    assert alert["unit_level"] == "factory" and alert["unit_id"] is None
    assert payload["repo"] == "factory"
    assert "rogue.txt" in payload["paths"][0]
    assert payload["where"] == "recover"
    assert notify.published == [
        (
            "Scriere în afara limitelor detectată în factory",
            notify.published[0][1],
            "max",
        )
    ]

    # Same streak: a second recover() re-observes the SAME dirt — no re-alert.
    scheduler.recover()
    assert len(_oob_alerts(db)) == 1
    assert len(notify.published) == 1

    # Clean observation ends the streak; new dirt is a NEW streak → re-alert.
    rogue.unlink()
    scheduler.recover()
    (home / "rogue2.txt").write_text("again\n", encoding="utf-8")
    scheduler.recover()
    assert len(_oob_alerts(db)) == 2
    assert len(notify.published) == 2


def test_out_of_bounds_ignores_droppings_and_clean_repo(db, config_dict) -> None:
    """Design §5: porcelain output is filtered through
    process.isolation_ignore_globs (D-0022/c50bf37 precedent) — build/test
    droppings never alert; a clean repo produces no event and no publish."""
    home = Path(config_dict["factory"]["home"])
    _git_factory_home(home)
    pycache = home / "__pycache__"
    pycache.mkdir()
    (pycache / "junk.cpython-312.pyc").write_bytes(b"\x00")
    cfg = make_config(config_dict)
    scheduler, notify = make_scheduler(db, cfg, {})
    scheduler.recover()
    assert _oob_alerts(db) == []
    assert notify.published == []


def test_out_of_bounds_skips_non_repo_roots(db, config_dict) -> None:
    """A root that is not a git repo (pre-bootstrap workspace, plain test home)
    has no git state to monitor — skipped, never a crash or a false alert."""
    cfg = make_config(config_dict)  # home is a plain tmp dir; workspace absent
    scheduler, notify = make_scheduler(db, cfg, {})
    scheduler.recover()
    assert _oob_alerts(db) == []
    assert notify.published == []


async def test_merge_gate_entry_runs_out_of_bounds_detector(
    db, config_dict, tmp_path
) -> None:
    """Design §5: the detector runs at stage MERGE_GATE ENTRY — the alert lands
    even when the gate then aborts (here: OPEN-2 null test_command →
    ConfigError), so a foreign write is observed at the next gate, not lost."""
    home = Path(config_dict["factory"]["home"])
    _git_factory_home(home)
    (home / "rogue.txt").write_text("agent wrote outside its worktree\n", encoding="utf-8")
    cfg = make_config(config_dict)
    insert_phase(db, "ph1")
    insert_stage(db, "s1", "ph1", StageState.MERGE_GATE, worktree=tmp_path / "wt")
    notify = FakeNotify()
    executor = StageExecutor(
        db,
        StateMachine(db),
        cfg,
        FakeRunner(db),
        FakeWorktrees(tmp_path / "scratch"),
        ThresholdEvaluator(db, cfg),
        FakeConsultor([]),
        notify,
    )
    stage = fdb.get_stage(db.read(), "s1")
    assert stage is not None
    with pytest.raises(ConfigError, match="test_command"):
        await executor._step_merge_gate(stage)
    (alert,) = _oob_alerts(db)
    payload = json.loads(alert["payload_json"])
    assert payload["repo"] == "factory" and payload["where"] == "merge_gate"
    assert any("rogue.txt" in p for p in payload["paths"])
    assert [(t, p) for t, _, p in notify.published] == [
        ("Scriere în afara limitelor detectată în factory", "max")
    ]


def test_out_of_bounds_worktrees_dir_is_sanctioned(db, config_dict) -> None:
    """Configured worktrees_dirs under a scanned root are factory-managed
    checkouts (worktree add / §5.5b canonicalization territory) — their
    porcelain entries are never out-of-bounds dirt. Production shape: the
    workspace contains its own .worktrees/ before any .gitignore exists."""
    workspace = Path(config_dict["projects"]["proj"]["workspace"])
    init_repo(workspace)  # real repo; worktrees_dir == workspace/.worktrees
    wt_dir = Path(config_dict["projects"]["proj"]["worktrees_dir"])
    (wt_dir / "ph1").mkdir(parents=True)
    (wt_dir / "ph1" / "scratch.txt").write_text("factory-managed\n", encoding="utf-8")
    cfg = make_config(config_dict)
    scheduler, notify = make_scheduler(db, cfg, {})
    scheduler.recover()
    assert _oob_alerts(db) == []
    assert notify.published == []


# ----------------------------------------------- CCR-6 usage-limit detector


def _usage_result(
    result_text: str, *, stderr_path: str = "(absent)", process_id: int = 7
) -> AgentResult:
    """AgentResult shell for detector tests — only the scanned fields vary."""
    return AgentResult(
        process_id=process_id,
        exit_code=0,
        timed_out=False,
        killed=False,
        declared_failure=False,
        result_text=result_text,
        session_id=None,
        tokens_in=1,
        tokens_out=1,
        cost_usd=None,
        garbage_lines=0,
        ndjson_log_path="(fake)",
        stderr_path=stderr_path,
        duration_ms=1,
    )


def _usage_detector(db, config_dict, notify=None):
    """Detector on the conftest config (usage_limit_signatures = the ratified
    default list — the frozen conftest predates the key)."""
    notify = notify or FakeNotify()
    return sched_mod._UsageLimitDetector(db, make_config(config_dict), notify), notify


async def test_usage_limit_signature_in_result_text_pages_once(db, config_dict) -> None:
    """CCR-6: a configured signature in result_text → ONE unit-scoped
    'usage_limit_suspected' event (role, signature, process_id) + ONE ntfy page
    at alert priority with a Romanian title + dashboard deep link."""
    detector, notify = _usage_detector(db, config_dict)
    await detector.check(
        _usage_result("error: usage limit reached, retry after the window resets"),
        unit_level="stage",
        unit_id="stg-1",
        role="builder_routine",
    )
    (event,) = events_of(db, "stg-1", "usage_limit_suspected")
    assert event["unit_level"] == "stage" and event["actor"] == "control_plane"
    payload = json.loads(event["payload_json"])
    assert payload == {"role": "builder_routine", "signature": "usage limit", "process_id": 7}
    ((title, link, priority),) = notify.published
    assert title.startswith("Limită de utilizare suspectată")
    assert link is not None and link.startswith("http")
    assert priority == "max"


async def test_usage_limit_second_consecutive_match_no_second_page(db, config_dict) -> None:
    """Streak dedup: a second consecutive match inserts no second event and
    sends no second page."""
    detector, notify = _usage_detector(db, config_dict)
    hit = _usage_result("HTTP 429: rate limit exceeded")
    await detector.check(hit, unit_level="stage", unit_id="stg-1", role="validator")
    await detector.check(hit, unit_level="stage", unit_id="stg-1", role="validator")
    assert len(events_of(db, "stg-1", "usage_limit_suspected")) == 1
    assert len(notify.published) == 1


async def test_usage_limit_clean_check_clears_latch_then_repages(db, config_dict) -> None:
    """A clean check ends the streak: the next match is a NEW streak — event +
    page land again (never a one-shot alarm, never a page-per-spawn)."""
    detector, notify = _usage_detector(db, config_dict)
    hit = _usage_result("HTTP 429: rate limit exceeded")
    clean = _usage_result("all tests green; stage complete")
    await detector.check(hit, unit_level="stage", unit_id="stg-1", role="validator")
    await detector.check(clean, unit_level="stage", unit_id="stg-1", role="validator")
    await detector.check(hit, unit_level="stage", unit_id="stg-1", role="validator")
    assert len(events_of(db, "stg-1", "usage_limit_suspected")) == 2
    assert len(notify.published) == 2


async def test_usage_limit_detected_in_stderr_tail_only(db, config_dict, tmp_path) -> None:
    """Signature absent from result_text but present in the LAST ~2KB of the
    stderr file → detected (the CLIs print capacity errors to stderr last)."""
    stderr = tmp_path / "proc.stderr"
    stderr.write_text(("x" * 4096) + "\nHTTP 403: usage limit hit\n", encoding="utf-8")
    detector, notify = _usage_detector(db, config_dict)
    await detector.check(
        _usage_result("", stderr_path=str(stderr)),
        unit_level="stage",
        unit_id="stg-1",
        role="builder_heavy",
    )
    (event,) = events_of(db, "stg-1", "usage_limit_suspected")
    assert json.loads(event["payload_json"])["signature"] == "usage limit"
    assert len(notify.published) == 1


async def test_usage_limit_stderr_scan_is_tail_bounded(db, config_dict, tmp_path) -> None:
    """Only the stderr TAIL is scanned: a signature buried >2KB before EOF is
    out of scope (bounded read, never a full-file scan)."""
    stderr = tmp_path / "proc.stderr"
    stderr.write_text("usage limit\n" + ("x" * 4096) + "\n", encoding="utf-8")
    detector, notify = _usage_detector(db, config_dict)
    await detector.check(
        _usage_result("", stderr_path=str(stderr)),
        unit_level="stage",
        unit_id="stg-1",
        role="builder_heavy",
    )
    assert events_of(db, "stg-1", "usage_limit_suspected") == []
    assert notify.published == []


async def test_usage_limit_match_is_case_insensitive(db, config_dict) -> None:
    """Signatures are lowercase by config contract; the scanned text is
    lowercased, so any casing in the agent output matches."""
    detector, notify = _usage_detector(db, config_dict)
    await detector.check(
        _usage_result("ERROR: Subscription Access required to continue"),
        unit_level="phase",
        unit_id="ph-1",
        role="phase_architect",
    )
    (event,) = events_of(db, "ph-1", "usage_limit_suspected")
    assert json.loads(event["payload_json"])["signature"] == "subscription access"
    assert len(notify.published) == 1


async def test_usage_limit_missing_stderr_file_tolerated(db, config_dict, tmp_path) -> None:
    """A missing/unreadable stderr file never fails the check — the scan simply
    covers result_text alone."""
    detector, notify = _usage_detector(db, config_dict)
    await detector.check(
        _usage_result("clean run", stderr_path=str(tmp_path / "absent.stderr")),
        unit_level="stage",
        unit_id="stg-1",
        role="validator",
    )
    assert events_of(db, "stg-1", "usage_limit_suspected") == []
    assert notify.published == []


async def test_usage_limit_no_match_inserts_nothing(db, config_dict) -> None:
    detector, notify = _usage_detector(db, config_dict)
    await detector.check(
        _usage_result("build finished; 42 tests passed"),
        unit_level="stage",
        unit_id="stg-1",
        role="builder_routine",
    )
    assert events_of(db, "stg-1", "usage_limit_suspected") == []
    assert notify.published == []


async def test_usage_limit_delivery_failure_contained_and_retried(db, config_dict) -> None:
    """NotifyError containment (the existing patterns): one
    'alert_delivery_failed' event (kind=usage_limit_suspected), no exception
    into the step; the publish retries on the next match of the SAME streak
    while the event latch holds."""
    detector, notify = _usage_detector(db, config_dict, notify=FakeNotify(fail=True))
    hit = _usage_result("upstream says rate limit")
    await detector.check(hit, unit_level="stage", unit_id="stg-1", role="validator")
    assert len(events_of(db, "stg-1", "usage_limit_suspected")) == 1
    failures = [
        dict(row)
        for row in db.read()
        .execute("SELECT * FROM events WHERE event_type = 'alert_delivery_failed'")
        .fetchall()
        if json.loads(row["payload_json"]).get("kind") == "usage_limit_suspected"
    ]
    assert len(failures) == 1
    notify.fail = False
    await detector.check(hit, unit_level="stage", unit_id="stg-1", role="validator")
    assert len(notify.published) == 1  # publish retried until delivered
    assert len(events_of(db, "stg-1", "usage_limit_suspected")) == 1  # event latch held


class _SignatureRunner(FakeRunner):
    """FakeRunner whose returned result carries a fixed result_text — the
    wiring tests drive the REAL executor-owned detector with it."""

    def __init__(self, db, result_text: str) -> None:
        super().__init__(db)
        self._result_text = result_text

    async def run_agent(self, role: str, prompt: str, **kwargs: Any) -> AgentResult:
        await super().run_agent(role, prompt, **kwargs)
        return _usage_result(self._result_text)


async def test_planning_run_with_signature_inserts_usage_limit_event(
    db, config_dict
) -> None:
    """CCR-6 wiring (PhaseExecutor._step_planning): a planning agent result
    carrying a signature lands a phase-scoped 'usage_limit_suspected' event +
    page via the executor-owned detector — even though the (absent) plan then
    fails strict validation and the phase escalates."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph-u", PhaseState.PLANNING)
    notify = FakeNotify()
    runner = _SignatureRunner(db, "claude: usage limit reached for this 5h window")
    executor = make_phase_executor(db, cfg, runner=runner, notify=notify)
    await executor.execute("ph-u")
    (event,) = events_of(db, "ph-u", "usage_limit_suspected")
    payload = json.loads(event["payload_json"])
    assert payload["role"] == "phase_architect"
    assert payload["signature"] == "usage limit"
    assert any(t.startswith("Limită de utilizare suspectată") for t, _, _ in notify.published)


async def test_stage_agent_run_with_signature_inserts_usage_limit_event(
    db, config_dict, tmp_path
) -> None:
    """CCR-6 wiring (StageExecutor._run_step_agent): a conveyor agent result
    carrying a signature lands a stage-scoped event right after the runner
    returns (here: the VALIDATE step's validator spawn)."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph", PhaseState.RUNNING)
    worktree = tmp_path / "stage-wt"
    init_repo(worktree)
    insert_stage(db, "ph.s1", "ph", StageState.VALIDATE, worktree=worktree)
    runner = _SignatureRunner(db, "codex: subscription access expired")
    runner.behaviors["validator"] = validator_writing(failing=0)
    notify = FakeNotify()
    executor = StageExecutor(
        db,
        StateMachine(db),
        cfg,
        runner,
        FakeWorktrees(tmp_path / "scratch"),
        ThresholdEvaluator(db, cfg),
        FakeConsultor([]),
        notify,
    )
    with pytest.raises(Exception):  # noqa: B017 — MERGE_GATE hits OPEN-2 (ConfigError)
        await executor.execute("ph.s1")
    (event,) = events_of(db, "ph.s1", "usage_limit_suspected")
    payload = json.loads(event["payload_json"])
    assert payload["role"] == "validator"
    assert payload["signature"] == "subscription access"
    assert any(t.startswith("Limită de utilizare suspectată") for t, _, _ in notify.published)

# --------------------------------- BUILD no-op acceptance (CCR-8, incident 2)


async def test_build_noop_accepted_transitions_to_validate(
    db, config_dict, tmp_path
) -> None:
    """CCR-8 (§5.5d idempotent re-entry): a builder that exits clean with
    NOTHING to commit and no declared-failure sentinel is a LEGAL no-op — the
    step inserts 'build_noop_accepted' and proceeds to VALIDATE exactly like
    the with-changes path. No ArtifactContractError, no escalation:
    independent validation is the gate, and a no-op hiding missing work fails
    there and loops back via the §8-bounded fix loop."""
    env = make_stage_env(db, config_dict, tmp_path)
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "BUILD")
    # Rework-re-entry shape: the agent verifies prior committed work, writes
    # nothing, exits 0 (FakeRunner's default result).
    env.runner.behaviors["builder_routine"] = lambda cwd, unit_id, resume: None

    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None
    progressed = await env.executor._step_build(stage)

    assert progressed is True
    assert stage_state(db, "ph.s1") is StageState.VALIDATE
    (noop,) = events_of(db, "ph.s1", "build_noop_accepted")
    payload = json.loads(noop["payload_json"])
    assert payload["note"] == "no new changes; validation is the gate"
    assert "process_id" in payload
    assert open_escalations(db, "ph.s1") == []
    # The VALIDATE entry is the same shape as the with-changes path (commit
    # payload present, None = nothing was committed).
    entry = [
        e for e in events_of(db, "ph.s1", "transition") if e["to_state"] == "VALIDATE"
    ]
    assert len(entry) == 1
    assert json.loads(entry[0]["payload_json"])["commit"] is None


async def test_build_with_changes_path_unchanged_no_noop_event(
    db, config_dict, tmp_path
) -> None:
    """CCR-8 regression guard: a builder that DOES change files keeps the
    pre-amendment behavior byte-for-byte — commit recorded on the VALIDATE
    entry, churn recorded, and NO 'build_noop_accepted' event."""
    env = make_stage_env(db, config_dict, tmp_path)
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "BUILD")
    env.runner.behaviors["builder_routine"] = builder_writing([0])

    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None
    progressed = await env.executor._step_build(stage)

    assert progressed is True
    assert stage_state(db, "ph.s1") is StageState.VALIDATE
    assert events_of(db, "ph.s1", "build_noop_accepted") == []
    entry = [
        e for e in events_of(db, "ph.s1", "transition") if e["to_state"] == "VALIDATE"
    ]
    assert json.loads(entry[0]["payload_json"])["commit"]  # real sha recorded
    assert open_escalations(db, "ph.s1") == []


async def test_build_declared_failure_path_unchanged_escalates(
    db, config_dict, tmp_path
) -> None:
    """CCR-8 regression guard: the declared-failure path is untouched — a
    builder writing _DECLARED_FAILURE.md escalates via the §8 always-fire
    trigger (thresholds run BEFORE the no-op check) and never lands a
    'build_noop_accepted' event."""
    env = make_stage_env(db, config_dict, tmp_path)
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "BUILD")

    def declaring_builder(cwd: Path, unit_id: str, resume) -> None:
        d = cwd / "_factory" / "stages" / unit_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "_DECLARED_FAILURE.md").write_text("cannot proceed", encoding="utf-8")

    env.runner.behaviors["builder_routine"] = declaring_builder
    await env.executor.execute("ph.s1")

    assert stage_state(db, "ph.s1") is StageState.ESCALATED
    triggers = [r["trigger"] for r in open_escalations(db, "ph.s1")]
    assert triggers == ["agent_declared_failure"]
    assert events_of(db, "ph.s1", "build_noop_accepted") == []


def test_build_prompt_forbids_self_commit(db, config_dict, tmp_path) -> None:
    """CCR-8: the Builder prompt carries the no-self-commit line — an agent
    committing its own work would hide changes from the control plane's
    commit-all step (and from churn accounting)."""
    env = make_stage_env(db, config_dict, tmp_path)
    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None
    prompt = env.executor._build_prompt(stage, env.worktree, {})
    assert "Never run `git commit` yourself — the control plane commits your work." in prompt


def test_build_prompt_forbids_factory_artifact_mutation(db, config_dict, tmp_path) -> None:
    """D-0030: the Builder prompt carries the spec-boundary line — a rework
    builder mutating the registered spec.md broke the Spec Agent's role
    boundary and tripped the start-time integrity abort at the next start."""
    env = make_stage_env(db, config_dict, tmp_path)
    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None
    prompt = env.executor._build_prompt(stage, env.worktree, {})
    assert "Never modify spec.md or any other _factory/ artifact" in prompt
