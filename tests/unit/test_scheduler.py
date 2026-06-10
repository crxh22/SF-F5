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
    env = make_stage_env(db, config_dict, tmp_path)
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "BUILD")
    (env.worktree / "leaked_validator_test.py").write_text("leak", encoding="utf-8")
    with pytest.raises(IntegrityError, match="Validator-isolation"):
        await env.executor.execute("ph.s1")


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


async def test_escalation_to_awaiting_human_creates_decision_atomically(
    db, config_dict, tmp_path
) -> None:
    """§9.4: 'awaiting_human' resolution -> AWAITING_HUMAN with the
    escalation-tradeoff decision request inserted in the SAME tx — the gate can
    never sit with only a stale answered decision to consume."""
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

    assert stage_state(db, "ph.s1") is StageState.AWAITING_HUMAN
    pending = fdb.pending_decisions(db.read())
    assert len(pending) == 1 and pending[0].gate_kind == "escalation_tradeoff"
    assert pending[0].unit_id == "ph.s1"
    assert [p for p in env.notify.published if "escaladare" in p[0]]


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
