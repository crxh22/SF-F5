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
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
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
    CapacityGovernor,
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
    runner (the context-reset consumption rule reads it). ``outcomes[role]``
    is a FIFO of AgentResult field overrides (e.g. {"exit_code": 1} or
    {"exit_code": None, "timed_out": True, "killed": True}) consumed one per
    call — exhausted or absent means the default clean exit-0 result."""

    def __init__(self, db: Any = None) -> None:
        self.db = db
        self.behaviors: dict[str, Any] = {}
        self.outcomes: dict[str, list[dict[str, Any]]] = {}
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
        fields: dict[str, Any] = {
            "process_id": 0,
            "exit_code": 0,
            "timed_out": False,
            "killed": False,
            "declared_failure": False,
            "result_text": "",
            "session_id": None,
            "tokens_in": 1,
            "tokens_out": 1,
            "cost_usd": None,
            "garbage_lines": 0,
            "ndjson_log_path": "(fake)",
            "stderr_path": "(fake)",
            "duration_ms": 1,
        }
        queue = self.outcomes.get(role)
        if queue:
            fields |= queue.pop(0)
        return AgentResult(**fields)


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


#: States whose ScriptedExecutor drive models an agent spawn — the test double's
#: mirror of the real `_step_spawn_roles` contract (scheduler.py:1384/3489): the
#: conveyor/work states spawn; dispatch-entry (PENDING), gates, and the BLOCKED
#: states (ESCALATED/AWAITING_*) spawn nothing. Keyed by level + concrete state.
_SPAWNING_STATES: dict[Level, frozenset[str]] = {
    Level.STAGE: frozenset({"SPEC", "BUILD", "VALIDATE", "AUDIT", "MERGE_GATE"}),
    Level.PHASE: frozenset({"PLANNING", "INTEGRATING"}),
}


@dataclass
class ScriptedExecutor:
    """One class for BOTH levels — driven through the same Scheduler loop, this
    is the §8 level-agnosticism proof object."""

    level: Level
    db: Any
    sm: StateMachine
    hold_s: float = 0.0
    #: Per-unit hold override (robustness UNIT 1 pin): a unit_id here holds its
    #: slot for the given seconds instead of `hold_s` — lets ONE spawner pin the
    #: cap for the whole test window while no-spawn escalation pickups (hold 0)
    #: run promptly through the SAME level-keyed executor.
    hold_unit_s: dict[str, float] = field(default_factory=dict)
    respect_blocked: bool = False
    fail_units: frozenset[str] = frozenset()
    #: Where a resolved escalation routes an ESCALATED unit (robustness UNIT 1
    #: pin) — mirrors `_step_escalated`'s resolution routing. Default
    #: AWAITING_HUMAN: a no-spawn blocked target so the drive transitions OUT of
    #: ESCALATED and stops (no further spawn), keeping the cap provably intact.
    escalation_resolves_to: str = "AWAITING_HUMAN"
    started: list[tuple[str, str]] = field(default_factory=list)
    finished: list[tuple[str, str]] = field(default_factory=list)
    concurrent: int = 0
    max_concurrent: int = 0

    def spawn_roles(self, unit: object) -> tuple[str, ...]:
        """UnitExecutor surface — the real StageExecutor/PhaseExecutor expose
        this so the scheduler can keep no-spawn drives out of the agent-slot cap
        (robustness UNIT 1). Mirrors `_step_spawn_roles`: `()` for the no-spawn
        states (PENDING, gates, ESCALATED), a role tuple for the conveyor."""
        state = unit.state.value  # type: ignore[attr-defined]
        if state in _SPAWNING_STATES[self.level]:
            return ("scripted_agent",)
        return ()

    async def execute(self, unit_id: str) -> None:
        self.started.append((self.level.value, unit_id))
        if unit_id in self.fail_units:
            raise RuntimeError(f"scripted failure for {unit_id}")
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            hold = self.hold_unit_s.get(unit_id, self.hold_s)
            if hold:
                await asyncio.sleep(hold)
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
                    elif (
                        state == "ESCALATED"
                        and sched_mod._open_escalation_count(
                            self.db.read(), self.level.value, unit_id
                        )
                        == 0
                        and sched_mod._latest_resolved_escalation(
                            self.db.read(), self.level.value, unit_id
                        )
                        is not None
                    ):
                        # Resolved escalation: the no-spawn `_step_escalated`
                        # pickup routes the unit forward (robustness UNIT 1 pin).
                        self.sm.transition(
                            self.level,
                            unit_id,
                            self.escalation_resolves_to,
                            actor="control_plane",
                            reason="scripted escalation resolution",
                        )
                        return
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


def _resolve_escalation_for(db, level: Level, unit_id: str, resolution: str) -> int:
    """Insert an OPEN escalation on an ESCALATED unit, then resolve it — the
    'resolved-but-not-yet-routed' shape that re-enters dispatch only to run the
    no-spawn `_step_escalated` pickup (robustness UNIT 1)."""
    with db.transaction() as conn:
        esc_id = fdb.insert_escalation(
            conn,
            Escalation(
                id=None,
                unit_level=level.value,
                unit_id=unit_id,
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
        fdb.resolve_escalation(conn, esc_id, resolution)
    return esc_id


async def test_no_spawn_escalation_proceeds_when_cap_full(db, config_dict) -> None:
    """THE INVARIANT PIN (robustness UNIT 1): a no-spawn ESCALATED resolution
    pickup transitions OUT of ESCALATED even while the agent-slot cap is FULL of
    long-running SPAWNING stages.

    MUTATION NOTE: without the fix, `_dispatch` gates EVERY drive behind
    `if len(self._tasks) >= cap: break` — the ESCALATED unit (no agent needed)
    would stay put until a SPAWNING slot frees (here: ~10s, far past the
    observation window). Confirmed by reverting the `_drive_spawns`/`continue`
    change: the assertion below then fails (stage stays ESCALATED). The spawner's
    long hold makes the difference observable: only the cap-exemption lets the
    control-plane pickup run within the window."""
    cfg = make_config(config_dict, max_parallel_agents=1)  # cap = 1 (one slot)
    insert_phase(db, "ph")
    # The one slot is held by a SPAWNING stage stuck mid-BUILD for the whole
    # test. Its id sorts FIRST in the scan (list_units ORDER BY id) so it is
    # dispatched and fills the cap BEFORE the escalated unit is considered —
    # this is what makes the buggy `break` gate strand the no-spawn pickup.
    insert_stage(db, "a_spawner", "ph", StageState.BUILD)
    # A resolved-but-not-routed escalation on a stage that sorts AFTER the
    # spawner: re-enters dispatch only to run the no-spawn `_step_escalated`
    # pickup. With the old `break` gate it waits behind the full cap; the fix
    # exempts it.
    insert_stage(db, "z_escalated", "ph", StageState.ESCALATED)
    _resolve_escalation_for(db, Level.STAGE, "z_escalated", "rework:VALIDATE")
    sm = StateMachine(db)
    # 'a_spawner' holds the only slot for 10s; the ESCALATED pickup has no hold
    # (hold 0) so it runs promptly through the SAME level-keyed executor.
    stage_exec = ScriptedExecutor(
        Level.STAGE, db, sm, hold_unit_s={"a_spawner": 10.0}, respect_blocked=True
    )
    scheduler, _ = make_scheduler(db, cfg, {Level.STAGE: stage_exec})

    task = asyncio.create_task(scheduler.run_forever())
    try:
        # A handful of ticks (loop_tick_s=0.01) — well under the spawner's 10s
        # hold, so the cap stays full the entire window.
        for _ in range(50):
            if stage_state(db, "z_escalated") is not StageState.ESCALATED:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # The cap was full of the SPAWNING 'a_spawner' the whole window...
    assert stage_state(db, "a_spawner") is StageState.BUILD
    # ...yet the no-spawn ESCALATED pickup still ran (transitioned OUT).
    assert stage_state(db, "z_escalated") is not StageState.ESCALATED
    assert stage_state(db, "z_escalated") is StageState.AWAITING_HUMAN


async def test_spawning_steps_still_capped(db, config_dict) -> None:
    """The EXISTING cap invariant must hold (robustness UNIT 1 must not weaken
    it): N+1 SPAWNING stages with cap=N -> at most N concurrent agent spawns,
    the (N+1)th waits. Real concurrent agents never exceed max_parallel_agents
    (design Falsifiability §10) — the companion to THE PIN."""
    cfg = make_config(config_dict, max_parallel_agents=2)  # N = 2
    insert_phase(db, "ph")
    # Three stages all entering at a SPAWNING state (BUILD) — every drive runs an
    # agent, so all three compete for the capped slots.
    for sid in ("a", "b", "c"):
        insert_stage(db, sid, "ph", StageState.BUILD)
    sm = StateMachine(db)
    stage_exec = ScriptedExecutor(Level.STAGE, db, sm, hold_s=0.08)
    scheduler, _ = make_scheduler(db, cfg, {Level.STAGE: stage_exec})
    await run_blocked(scheduler)

    # Never more than N=2 concurrent spawning drives, despite 3 ready stages.
    assert stage_exec.max_concurrent == cfg.process.max_parallel_agents == 2
    assert all(stage_state(db, s) is StageState.DONE for s in ("a", "b", "c"))


async def test_no_spawn_does_not_starve_later_no_spawn(db, config_dict) -> None:
    """The `continue`-not-`break` pin (robustness UNIT 1): with the cap full of
    spawners, TWO no-spawn ESCALATED units later in the scan BOTH proceed in the
    same window. A `break` (the old gate) would strand the second behind the
    capped spawner; `continue` keeps it reachable."""
    cfg = make_config(config_dict, max_parallel_agents=1)  # one slot, held below
    insert_phase(db, "ph")
    # 'a_spawner' sorts FIRST (list_units ORDER BY id) and fills the only slot;
    # the two ESCALATED units sort AFTER it, so the OLD `break` gate would
    # strand BOTH behind the full cap. `continue` keeps them reachable.
    insert_stage(db, "a_spawner", "ph", StageState.BUILD)  # holds the only slot
    insert_stage(db, "z_esc1", "ph", StageState.ESCALATED)
    insert_stage(db, "z_esc2", "ph", StageState.ESCALATED)
    _resolve_escalation_for(db, Level.STAGE, "z_esc1", "rework:VALIDATE")
    _resolve_escalation_for(db, Level.STAGE, "z_esc2", "rework:VALIDATE")
    sm = StateMachine(db)
    stage_exec = ScriptedExecutor(
        Level.STAGE, db, sm, hold_unit_s={"a_spawner": 10.0}, respect_blocked=True
    )
    scheduler, _ = make_scheduler(db, cfg, {Level.STAGE: stage_exec})

    task = asyncio.create_task(scheduler.run_forever())
    try:
        for _ in range(50):
            if all(
                stage_state(db, s) is not StageState.ESCALATED
                for s in ("z_esc1", "z_esc2")
            ):
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert stage_state(db, "a_spawner") is StageState.BUILD  # cap still full
    # BOTH no-spawn units advanced — the second was not stranded behind the cap.
    assert stage_state(db, "z_esc1") is StageState.AWAITING_HUMAN
    assert stage_state(db, "z_esc2") is StageState.AWAITING_HUMAN


async def test_rework_routing_overshoots_cap_by_bounded_k_accepted_residual(
    db, config_dict
) -> None:
    """DOCUMENTS the consciously-accepted UNIT-1 rework-routing residual (see the
    design doc UNIT 1 Falsifiability note). `execute()` walks every legal step in
    ONE task, so an EXEMPT BLOCKED drive whose resolution routes to a SPAWNING
    rework state (ESCALATED -> rework:BUILD) walks from the no-spawn pickup INTO
    that spawn in the same task — spawning once past the cap, and (because exempt
    drives never enter `self._spawning`) fresh spawners fill the full cap on top.
    Net peak concurrent drives in a SPAWNING state = cap + K, where K = the number
    of rework-routing ESCALATED drives resolved in one tick (K bounded by
    simultaneously-resolved rework escalations <= cap; the capacity governor's
    batch auto-resolve at a budget reset is the K~cap case). This is accepted as a
    bounded economic-cap residual (the cap protects the §7 process budget, not a
    hard safety limit — §8). Steady-state cap still holds (the spawning-SET stays
    <= cap); the cap+K spike is transient (one tick).

    This test PINS the bound: a future change that makes the overshoot WORSE than
    cap+K (drift toward unbounded) trips `peak <= cap + K`, and the `peak > cap`
    assertion keeps the accepted residual a visible, tested fact. If a future
    change ELIMINATES the residual (peak == cap), update the `peak > cap`
    assertion AND the design note's amendment together.
    """
    cap = 2
    K = 3
    cfg = make_config(config_dict, max_parallel_agents=cap)  # N = 2
    insert_phase(db, "ph")
    # K rework-routing ESCALATED drives: each has a resolved-but-not-routed
    # escalation, and its `_step_escalated` pickup routes it to BUILD (a SPAWNING
    # state). They are cap-EXEMPT at pickup (ESCALATED spawns nothing) yet walk
    # into the BUILD spawn in the same task. Their ids sort AFTER the spawners.
    for i in range(K):
        sid = f"z_esc{i}"
        insert_stage(db, sid, "ph", StageState.ESCALATED)
        _resolve_escalation_for(db, Level.STAGE, sid, "rework:BUILD")
    # `cap` fresh BUILD spawners that sort FIRST and fill every capped slot; held
    # 10s so the cap stays full for the whole observation window.
    for i in range(cap):
        insert_stage(db, f"a_spawner{i}", "ph", StageState.BUILD)
    sm = StateMachine(db)
    stage_exec = ScriptedExecutor(
        Level.STAGE,
        db,
        sm,
        hold_unit_s={f"a_spawner{i}": 10.0 for i in range(cap)},
        respect_blocked=True,
        escalation_resolves_to="BUILD",  # resolution routes into a SPAWNING state
    )
    scheduler, _ = make_scheduler(db, cfg, {Level.STAGE: stage_exec})

    spawning_states = _SPAWNING_STATES[Level.STAGE]

    def peak_spawning_drives() -> int:
        # Real agents in production = units in a SPAWNING state with a live drive
        # task. The held spawners (in `_spawning`) plus the K rework-routers that
        # walked into BUILD in their still-pending drive coexist for one tick.
        conn = db.read()
        live = set(scheduler._tasks)
        return sum(
            1
            for u in fdb.list_units(conn, Level.STAGE)
            if u.state.value in spawning_states and (Level.STAGE, u.id) in live
        )

    peak = 0
    task = asyncio.create_task(scheduler.run_forever())
    try:
        # Dense per-loop-turn sampling (sleep(0)) over the cap-full window catches
        # the one-tick cap+K spike deterministically; the spawners hold the cap.
        for _ in range(2000):
            peak = max(peak, peak_spawning_drives())
            if peak >= cap + K:
                break
            await asyncio.sleep(0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # The REGRESSION GUARD: the overshoot stays bounded by cap + K. If a future
    # change lets it grow past this, the residual is no longer self-limiting —
    # fail loudly so it gets re-ruled (the deferred Semaphore fix, design note).
    assert peak <= cap + K
    # DOCUMENTS that the residual is REAL: real concurrent spawning drives exceed
    # the cap on the rework-routing tick (the consciously-accepted UNIT-1 residual,
    # design UNIT 1 Falsifiability note). If a future change makes peak == cap,
    # update THIS assertion and the design note's amendment together.
    assert peak > cap
    # Steady-state still holds: the cap denominator (the spawning SET) never
    # exceeds the cap — only the transient in-task walk overshoots.
    assert len(scheduler._spawning) <= cap


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


# ----------------------------------- robustness UNIT 2: stuck-escalation detector


def _min_ago(minutes: int) -> str:
    """An ISO-UTC timestamp `minutes` in the past (models.utc_now format) — drives
    the detector's created_at/resolved_at age clocks past the threshold."""
    return (datetime.now(UTC) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_escalation(
    db,
    *,
    unit_id: str,
    level: Level = Level.STAGE,
    target: str = "phase_architect",
    status: str = "open",
    created_at: str | None = None,
    resolved_at: str | None = None,
    resolution: str | None = None,
) -> int:
    """Insert ONE escalation row with full control over target/status/age — the
    stuck-detector's read shapes. Returns the escalation id."""
    with db.transaction() as conn:
        return fdb.insert_escalation(
            conn,
            Escalation(
                id=None,
                unit_level=level.value,
                unit_id=unit_id,
                trigger="cp1_verdict",
                target=target,
                payload_artifact_id=None,
                event_seq=None,
                status=status,
                resolution=resolution,
                created_at=created_at or utc_now(),
                resolved_at=resolved_at,
            ),
        )


def _arhitect_pushes(notify: FakeNotify) -> list[tuple[str, str | None, str]]:
    return [p for p in notify.published if p[0].startswith("[arhitect]")]


def _escalation_row(db, esc_id: int) -> dict:
    return dict(
        db.read().execute("SELECT * FROM escalations WHERE id = ?", (esc_id,)).fetchone()
    )


def _set_created_at(db, esc_id: int, created_at: str) -> None:
    """Re-age an open escalation's created_at clock between ticks (simulating the
    passage of time so the stateless (2a) age-derived climb advances a rung)."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE escalations SET created_at = ? WHERE id = ?", (created_at, esc_id)
        )


async def test_stuck_open_escalation_climbs_to_founder_spaced(db, config_dict) -> None:
    """Case (2a) — STATELESS age-derived spaced climb to the founder. At age ∈
    [threshold, 2·threshold) the target reaches main_architect; at age ≥ 2·threshold
    it reaches founder; then clamps. NO cascade: at most one bump per tick, and the
    SAME age on the next tick does not re-bump (the persisted target is the latch)."""
    cfg = make_config(config_dict)
    threshold = cfg.escalation.stuck_escalation_threshold_min  # 30
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.ESCALATED)
    # One threshold old -> expected rung main_architect (one rung up, NOT founder).
    esc_id = _insert_escalation(
        db, unit_id="s1", target="phase_architect", created_at=_min_ago(threshold + 1)
    )
    sm = StateMachine(db)
    scheduler, notify = make_scheduler(
        db, cfg, {Level.STAGE: ScriptedExecutor(Level.STAGE, db, sm, respect_blocked=True)}
    )
    state_before = stage_state(db, "s1")
    await run_blocked(scheduler)

    assert _escalation_row(db, esc_id)["target"] == "main_architect"  # one rung, not founder
    assert _escalation_row(db, esc_id)["status"] == "open"  # status untouched
    # NO-TRANSITION MANDATE: the climb relabels the target but NEVER advances the
    # unit (a stray transition injected into the (2a) branch must fail this).
    assert stage_state(db, "s1") == state_before == StageState.ESCALATED
    bumped = events_of(db, "s1", "escalation_bumped")
    assert len(bumped) == 1  # exactly ONE bump this tick (no cascade to founder)
    payload = json.loads(bumped[0]["payload_json"])
    assert payload["from_target"] == "phase_architect"
    assert payload["to_target"] == "main_architect"
    bump_pushes = [p for p in _arhitect_pushes(notify) if "ridicată" in p[0]]
    assert len(bump_pushes) == 1 and bump_pushes[0][2] == "max"

    await run_blocked(scheduler)  # SAME age -> no re-bump (target == expected rung)
    assert _escalation_row(db, esc_id)["target"] == "main_architect"
    assert len(events_of(db, "s1", "escalation_bumped")) == 1
    assert len([p for p in _arhitect_pushes(notify) if "ridicată" in p[0]]) == 1

    # Age past TWO thresholds -> the climb now reaches the founder (the durable
    # backstop when the architect session is dead, D-0042).
    _set_created_at(db, esc_id, _min_ago(2 * threshold + 1))
    await run_blocked(scheduler)

    assert _escalation_row(db, esc_id)["target"] == "founder"  # reached the founder rung
    bumped = events_of(db, "s1", "escalation_bumped")
    assert len(bumped) == 2  # one more bump (main_architect -> founder), still one/tick
    payload = json.loads(bumped[-1]["payload_json"])
    assert payload["from_target"] == "main_architect"
    assert payload["to_target"] == "founder"
    assert len([p for p in _arhitect_pushes(notify) if "ridicată" in p[0]]) == 2

    await run_blocked(scheduler)  # clamped at founder -> no further bump/page
    assert _escalation_row(db, esc_id)["target"] == "founder"
    assert len(events_of(db, "s1", "escalation_bumped")) == 2
    assert len([p for p in _arhitect_pushes(notify) if "ridicată" in p[0]]) == 2


async def test_stuck_resolved_not_advanced_pages(db, config_dict) -> None:
    """Case (b) — the incident-[20] pin: a RESOLVED escalation whose unit is STILL
    ESCALATED pages ONCE; the row is NOT re-resolved, NOT re-created, status
    UNCHANGED (the detector NEVER mutates a resolved escalation)."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.ESCALATED)  # resolution never picked up
    esc_id = _insert_escalation(
        db,
        unit_id="s1",
        target="phase_architect",
        status="resolved",
        created_at=_min_ago(90),
        resolved_at=_min_ago(45),  # resolved 45 min ago, threshold 30
        resolution="rework:VALIDATE",
    )
    before = _escalation_row(db, esc_id)
    sm = StateMachine(db)
    # respect_blocked=False: the ScriptedExecutor must NOT advance the unit, so the
    # resolved-but-stuck condition persists for the detector to observe.
    scheduler, notify = make_scheduler(
        db, cfg, {Level.STAGE: ScriptedExecutor(Level.STAGE, db, sm)}
    )
    await run_blocked(scheduler)

    stuck = events_of(db, "s1", "escalation_stuck_resolved")
    assert len(stuck) == 1
    stuck_pushes = [p for p in _arhitect_pushes(notify) if "neavansată" in p[0]]
    assert len(stuck_pushes) == 1 and stuck_pushes[0][2] == "max"
    # NO-TRANSITION MANDATE: the detector pages but NEVER advances the unit — it is
    # still ESCALATED (a stray transition injected here must fail this).
    assert stage_state(db, "s1") == StageState.ESCALATED
    # MUTATION GUARD: row identical (no re-resolve), and exactly ONE escalation row.
    assert _escalation_row(db, esc_id) == before
    n_rows = db.read().execute(
        "SELECT COUNT(*) FROM escalations WHERE unit_id = 's1'"
    ).fetchone()[0]
    assert n_rows == 1  # NOT re-created

    await run_blocked(scheduler)  # once per episode
    assert len(events_of(db, "s1", "escalation_stuck_resolved")) == 1
    assert len([p for p in _arhitect_pushes(notify) if "neavansată" in p[0]]) == 1


async def test_resolved_and_advanced_does_not_page(db, config_dict) -> None:
    """A resolved escalation whose unit has MOVED ON (not ESCALATED) is silent —
    the resolution landed, there is nothing stuck."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.BUILD)  # advanced past ESCALATED
    _insert_escalation(
        db,
        unit_id="s1",
        status="resolved",
        created_at=_min_ago(90),
        resolved_at=_min_ago(45),
        resolution="rework:BUILD",
    )
    sm = StateMachine(db)
    scheduler, notify = make_scheduler(
        db, cfg, {Level.STAGE: ScriptedExecutor(Level.STAGE, db, sm, respect_blocked=True)}
    )
    await run_blocked(scheduler)

    assert events_of(db, "s1", "escalation_stuck_resolved") == []
    assert [p for p in _arhitect_pushes(notify) if "neavansată" in p[0]] == []


async def test_stuck_resolved_skips_superseded_when_reescalated(db, config_dict) -> None:
    """case-2b over-fire fix (ETAPA-5f): a unit with a HISTORY of old resolved
    escalations, re-ESCALATED for a NEW reason (a current OPEN escalation), pages
    (2b) ZERO times. The OPEN escalation is the unit's live episode (surfaced by
    (2a)/first-notice); the old resolved rows are superseded, not stuck-resolved.
    Before the fix EACH old resolved row matched (resolved + old + unit ESCALATED)
    and paged once each — the production flood (~32 false [arhitect] pages,
    register-schemas with a 4-resolution history)."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.ESCALATED)
    # Four OLD resolved escalations (the register-schemas-style history).
    for _ in range(4):
        _insert_escalation(
            db,
            unit_id="s1",
            status="resolved",
            created_at=_min_ago(180),
            resolved_at=_min_ago(90),  # older than threshold 30
            resolution="rework:BUILD",
        )
    # Re-ESCALATED for a NEW reason: a current OPEN escalation = the live episode.
    open_id = _insert_escalation(db, unit_id="s1", target="phase_architect")  # age 0
    sm = StateMachine(db)
    scheduler, notify = make_scheduler(
        db, cfg, {Level.STAGE: ScriptedExecutor(Level.STAGE, db, sm, respect_blocked=True)}
    )
    await run_blocked(scheduler)

    # (2b) fires ZERO — no OLD resolved escalation is the unit's most-recent.
    assert events_of(db, "s1", "escalation_stuck_resolved") == []
    assert [p for p in _arhitect_pushes(notify) if "neavansată" in p[0]] == []
    # The live OPEN escalation still gets its first-notice (signal NOT suppressed).
    notices = events_of(db, "s1", "escalation_opened_notice")
    assert len(notices) == 1
    assert json.loads(notices[0]["payload_json"])["escalation_id"] == open_id


async def test_stuck_resolved_fires_only_for_latest_of_many(db, config_dict) -> None:
    """case-2b scope (ETAPA-5f): a unit with MULTIPLE old resolved escalations and
    NO open one, still ESCALATED (the resolution never advanced it), pages (2b)
    exactly ONCE — for its MOST-RECENT escalation only, never once per resolved row."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.ESCALATED)
    _insert_escalation(
        db,
        unit_id="s1",
        status="resolved",
        created_at=_min_ago(180),
        resolved_at=_min_ago(120),  # older episode
        resolution="rework:BUILD",
    )
    latest_id = _insert_escalation(
        db,
        unit_id="s1",
        status="resolved",
        created_at=_min_ago(90),
        resolved_at=_min_ago(45),  # most-recent, still older than threshold 30
        resolution="rework:VALIDATE",
    )
    sm = StateMachine(db)
    scheduler, notify = make_scheduler(
        db, cfg, {Level.STAGE: ScriptedExecutor(Level.STAGE, db, sm)}
    )
    await run_blocked(scheduler)

    events = events_of(db, "s1", "escalation_stuck_resolved")
    assert len(events) == 1  # ONCE, not once-per-resolved-row
    assert json.loads(events[0]["payload_json"])["escalation_id"] == latest_id
    assert len([p for p in _arhitect_pushes(notify) if "neavansată" in p[0]]) == 1


async def test_architect_learns_on_open(db, config_dict) -> None:
    """The ≤5-min law (Q2): a freshly-open architect-targeted escalation pages the
    architect + emits escalation_opened_notice on the FIRST tick, before any
    threshold; ONCE (second tick silent)."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.ESCALATED)
    # age 0 (created now), architect-targeted -> first-notice fires immediately.
    _insert_escalation(db, unit_id="s1", target="phase_architect")
    sm = StateMachine(db)
    scheduler, notify = make_scheduler(
        db, cfg, {Level.STAGE: ScriptedExecutor(Level.STAGE, db, sm, respect_blocked=True)}
    )
    state_before = stage_state(db, "s1")
    await run_blocked(scheduler)

    notices = events_of(db, "s1", "escalation_opened_notice")
    assert len(notices) == 1
    open_pushes = [p for p in _arhitect_pushes(notify) if "escaladare nesemnalată" in p[0]]
    assert len(open_pushes) == 1 and open_pushes[0][2] == "max"
    # NO-TRANSITION MANDATE: first-notice pages but NEVER advances the unit.
    assert stage_state(db, "s1") == state_before == StageState.ESCALATED
    # No bump (under threshold), no stuck-resolved.
    assert events_of(db, "s1", "escalation_bumped") == []

    await run_blocked(scheduler)  # latch: one notice only
    assert len(events_of(db, "s1", "escalation_opened_notice")) == 1
    assert len([p for p in _arhitect_pushes(notify) if "escaladare nesemnalată" in p[0]]) == 1


async def test_open_notice_skips_founder_target(db, config_dict) -> None:
    """A founder-targeted OPEN escalation is NOT treated as an architect
    first-notice (the founder's domain is the trade-off-card path, not the
    architect channel) — no escalation_opened_notice, no [arhitect] page."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.ESCALATED)
    _insert_escalation(db, unit_id="s1", target="founder")  # age 0, founder rung
    sm = StateMachine(db)
    scheduler, notify = make_scheduler(
        db, cfg, {Level.STAGE: ScriptedExecutor(Level.STAGE, db, sm, respect_blocked=True)}
    )
    await run_blocked(scheduler)

    assert events_of(db, "s1", "escalation_opened_notice") == []
    assert _arhitect_pushes(notify) == []


async def test_ladder_clamps_at_founder(db, config_dict) -> None:
    """A founder-targeted OPEN escalation over the open threshold is a NO-OP: the
    founder is the ladder cap, so its current rung is never BELOW the age-expected
    rung (current_idx 2 >= expected_idx, capped at 2) — no relabel, no event, no
    page. (Semantics change vs the old single-bump, which self-bumped founder->founder
    and re-paged founder; the stateless age-derived climb has nowhere higher to go,
    so it correctly stays silent — the founder is reached via the climb FROM the
    architect rungs, never by re-paging an already-founder escalation.)"""
    cfg = make_config(config_dict)
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.ESCALATED)
    esc_id = _insert_escalation(db, unit_id="s1", target="founder", created_at=_min_ago(40))
    sm = StateMachine(db)
    scheduler, notify = make_scheduler(
        db, cfg, {Level.STAGE: ScriptedExecutor(Level.STAGE, db, sm, respect_blocked=True)}
    )
    await run_blocked(scheduler)

    assert _escalation_row(db, esc_id)["target"] == "founder"  # clamped, unchanged
    assert events_of(db, "s1", "escalation_bumped") == []  # no infinite climb, no self-bump
    assert [p for p in _arhitect_pushes(notify) if "ridicată" in p[0]] == []  # no founder spam

    await run_blocked(scheduler)  # still silent (clamped)
    assert events_of(db, "s1", "escalation_bumped") == []


async def test_stuck_detector_delivery_failure_logs_once(db, config_dict) -> None:
    """FakeNotify(fail=True): a page failure logs ONE alert_delivery_failed event
    per streak, NEVER raises, and leaves the latch un-set so it retries (the
    escalation is not silently dropped) — the stall/latency contract."""
    cfg = make_config(config_dict)
    insert_phase(db, "ph")
    insert_stage(db, "s1", "ph", StageState.ESCALATED)
    esc_id = _insert_escalation(
        db, unit_id="s1", target="phase_architect", created_at=_min_ago(40)
    )
    sm = StateMachine(db)
    scheduler, _ = make_scheduler(
        db,
        cfg,
        {Level.STAGE: ScriptedExecutor(Level.STAGE, db, sm, respect_blocked=True)},
        notify=FakeNotify(fail=True),
    )
    await run_blocked(scheduler)  # must not raise

    failures = (
        db.read()
        .execute("SELECT * FROM events WHERE event_type='alert_delivery_failed'")
        .fetchall()
    )
    # First-notice + open-too-long both target s1; each failed page logs once per
    # its own streak key, and neither re-logs every tick.
    kinds = {json.loads(dict(f)["payload_json"])["kind"] for f in failures}
    assert kinds <= {"escalation_opened_notice", "escalation_bumped"}
    n_before = len(failures)
    await run_blocked(scheduler)  # same streak -> no new failure events
    n_after = (
        db.read()
        .execute("SELECT COUNT(*) FROM events WHERE event_type='alert_delivery_failed'")
        .fetchone()[0]
    )
    assert n_after == n_before
    # Page failed -> the target was NOT bumped (un-latched, retried next tick), the
    # row is untouched and the escalation is never silently lost.
    assert _escalation_row(db, esc_id)["target"] == "phase_architect"


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
    db, cfg: FactoryConfig, runner=None, wt=None, notify=None, governor=None
) -> PhaseExecutor:
    return PhaseExecutor(
        db,
        StateMachine(db),
        cfg,
        runner or FakeRunner(),
        wt or FakeWorktrees(Path(cfg.factory.home) / "scratch"),
        notify or FakeNotify(),
        governor=governor,
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


def test_render_sibling_diffs_budget_switches_to_hunk_headers() -> None:
    """D-0046: the Tier-2 sibling block renders full diff bodies under the total
    byte budget and collapses to file+@@ hunk headers above it; the gating
    unit's diff (the caller's ``fixed_bytes``) is never the helper's to touch.
    Boundary is inclusive (<=) and has teeth — one byte over flips to headers."""
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,3 +1,4 @@\n"
        " context\n"
        "-removed_body\n"
        "+ADDED_BODY_MARKER\n"
        " tail\n"
    )
    siblings = {"unit-b": diff, "unit-a": diff}  # unsorted on purpose

    # empty -> the caller's empty_text verbatim, never headers
    assert sched_mod._render_sibling_diffs({}, 0, 10, "(none)") == (["(none)"], False)

    full_lines = [f"--- merged unit {u} ---\n{diff}" for u in ("unit-a", "unit-b")]
    full_bytes = sum(len(s.encode("utf-8")) for s in full_lines)
    fixed = 500

    # exactly at budget -> full bodies (inclusive), sorted by unit id
    lines, used = sched_mod._render_sibling_diffs(
        siblings, fixed, fixed + full_bytes, "(none)"
    )
    assert used is False
    assert lines == full_lines
    assert "ADDED_BODY_MARKER" in "\n".join(lines)

    # one byte over -> hunk headers: bodies gone, changed regions + file headers kept
    lines, used = sched_mod._render_sibling_diffs(
        siblings, fixed, fixed + full_bytes - 1, "(none)"
    )
    assert used is True
    joined = "\n".join(lines)
    assert "ADDED_BODY_MARKER" not in joined  # body elided
    assert "removed_body" not in joined  # body elided
    assert "@@ -1,3 +1,4 @@" in joined  # changed region kept
    assert "diff --git a/foo.py b/foo.py" in joined  # file header kept
    assert joined.count("hunk headers only") == 2  # both siblings flagged
    assert joined.index("unit-a") < joined.index("unit-b")  # sorted


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
    # CCR-9: the resolution transition payload carries the dedicated
    # 'rework_context' key — with no escalation_resolved event rationale the
    # deterministic fallback applies.
    entry = [
        e for e in events_of(db, "ph.s1", "transition") if e["to_state"] == "FAILED"
    ]
    payload = json.loads(entry[-1]["payload_json"])
    assert payload["escalation_id"] == esc_id
    assert payload["resolution"] == "failed"
    assert payload["rework_context"] == "escalation resolved: failed"


async def test_escalation_resolution_payload_carries_operator_reason(
    db, config_dict, tmp_path
) -> None:
    """CCR-9: the operator's `resolve-escalation --reason` rationale — recorded
    only in the escalation_resolved event payload — travels in the resolution
    transition payload's dedicated 'rework_context' key into the rework target,
    where the re-entered role's prompt builder reads it as 'Rework context'."""
    env = make_stage_env(db, config_dict, tmp_path)
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "ESCALATED")
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
    with db.transaction() as conn:  # the CLI's short tx: resolve + event
        fdb.resolve_escalation(conn, esc_id, "rework:SPEC")
        fdb.insert_event(
            conn,
            unit_level="stage",
            unit_id="ph.s1",
            event_type="escalation_resolved",
            actor="main_architect",
            payload={
                "escalation_id": esc_id,
                "resolution": "rework:SPEC",
                "reason": "spec is internally contradictory",
                "via": "cli",
            },
        )
    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None
    progressed = await env.executor._step_escalated(stage)

    assert progressed is True
    assert stage_state(db, "ph.s1") is StageState.SPEC
    entry = [
        e for e in events_of(db, "ph.s1", "transition") if e["to_state"] == "SPEC"
    ]
    payload = json.loads(entry[-1]["payload_json"])
    assert payload["escalation_id"] == esc_id
    assert payload["resolution"] == "rework:SPEC"
    assert payload["rework_context"] == "spec is internally contradictory"


async def test_escalation_rework_merge_gate_routes_back_to_merge_gate(
    db, config_dict, tmp_path
) -> None:
    """D-0041 merge-gate re-entry: a 'rework:MERGE_GATE' resolution on an open
    stage escalation re-enters ONLY the merge gate (Tier-1 rebase+suite + Tier-2
    integration_validator) — no re-validate, no re-audit. Mirrors the
    rework:SPEC/BUILD routing tests: _step_escalated transitions ESCALATED ->
    MERGE_GATE (now a legal exit), skips the contested-findings settlement (the
    target is not in BUILD/SPEC/VALIDATE — a stage at the gate closed its
    findings at audit) and the AWAITING_HUMAN wrapper, and carries the operator
    reason on the dedicated 'rework_context' key."""
    env = make_stage_env(db, config_dict, tmp_path)
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "ESCALATED")
        esc_id = fdb.insert_escalation(
            conn,
            Escalation(
                id=None,
                unit_level="stage",
                unit_id="ph.s1",
                trigger="agent_run_failed",
                target="phase_architect",
                payload_artifact_id=None,
                event_seq=None,
                status="open",
                resolution=None,
                created_at=utc_now(),
                resolved_at=None,
            ),
        )
    with db.transaction() as conn:  # the CLI's short tx: resolve + event
        fdb.resolve_escalation(conn, esc_id, "rework:MERGE_GATE")
        fdb.insert_event(
            conn,
            unit_level="stage",
            unit_id="ph.s1",
            event_type="escalation_resolved",
            actor="main_architect",
            payload={
                "escalation_id": esc_id,
                "resolution": "rework:MERGE_GATE",
                "reason": "Tier-2 overflow fixed; re-run the gate only",
                "via": "cli",
            },
        )
    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None
    # No contested findings exist at the gate; settlement must be a no-op (the
    # transition still succeeds and routes straight to MERGE_GATE).
    progressed = await env.executor._step_escalated(stage)

    assert progressed is True
    assert stage_state(db, "ph.s1") is StageState.MERGE_GATE
    entry = [
        e
        for e in events_of(db, "ph.s1", "transition")
        if e["to_state"] == "MERGE_GATE"
    ]
    payload = json.loads(entry[-1]["payload_json"])
    assert payload["escalation_id"] == esc_id
    assert payload["resolution"] == "rework:MERGE_GATE"
    assert payload["rework_context"] == "Tier-2 overflow fixed; re-run the gate only"
    assert open_escalations(db, "ph.s1") == []


def _seed_finding(
    db,
    *,
    ref: str,
    status: str,
    severity: str = "major",
    auditor_role: str = "auditor_cross_model",
    stage_id: str = "ph.s1",
) -> int:
    """Insert one audit_finding (with its required report_artifact_id) and return
    its id — the slice-2 settled/audit-memory fixtures build findings this way.
    The report ref's (path, sha) is varied by `ref` so multiple seeds never
    collide on the artifact_refs UNIQUE (repo, path, sha256)."""
    from sf_factory.models import ArtifactRef, Finding

    with db.transaction() as conn:
        report = fdb.insert_artifact_ref(
            conn,
            ArtifactRef(
                id=None,
                unit_level="stage",
                unit_id=stage_id,
                kind="audit_report",
                repo="workspace",
                path=f"_factory/stages/{stage_id}/audit-{ref}.json",
                sha256=hashlib.sha256(ref.encode()).hexdigest(),
                git_commit=None,
                created_at=utc_now(),
            ),
        )
        return fdb.insert_finding(
            conn,
            Finding(
                id=None,
                stage_id=stage_id,
                auditor_role=auditor_role,
                finding_ref=ref,
                severity=severity,
                report_artifact_id=report,
                status=status,
                contest_artifact_id=None,
                resolved_by=None,
                created_at=utc_now(),
                updated_at=utc_now(),
            ),
        )


async def test_escalation_settled_routine_routes_merge_gate_and_settles_findings(
    db, config_dict, tmp_path
) -> None:
    """Slice-2 Unit A: the no-action `settled` disposition on a ROUTINE stage
    flips the open escalation's contested findings to `settled`
    (resolved_by=phase_architect) and routes ESCALATED -> MERGE_GATE via
    _leave_clean_audit (no human gate). `settled` is special-cased BEFORE the
    static map, so it does NOT trip the unknown-resolution alert."""
    env = make_stage_env(db, config_dict, tmp_path, risk="routine")
    fid = _seed_finding(db, ref="F-1", status="contested")
    fid_other = _seed_finding(db, ref="F-2", status="complied")  # untouched control
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "ESCALATED")
        esc_id = fdb.insert_escalation(
            conn,
            Escalation(
                id=None,
                unit_level="stage",
                unit_id="ph.s1",
                trigger="unresolved_contest",
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
        fdb.resolve_escalation(conn, esc_id, "settled")
    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None

    progressed = await env.executor._step_escalated(stage)

    assert progressed is True
    assert stage_state(db, "ph.s1") is StageState.MERGE_GATE
    rows = {f.id: f for f in fdb.findings(db.read(), "ph.s1")}
    assert rows[fid].status == "settled"
    assert rows[fid].resolved_by == "phase_architect"
    assert rows[fid_other].status == "complied"  # non-contested untouched
    # No unknown-resolution alert was emitted (settled is recognized).
    assert events_of(db, "ph.s1", "alert") == []


async def test_escalation_settled_critical_routes_awaiting_human(
    db, config_dict, tmp_path
) -> None:
    """Slice-2 Unit A: `settled` on a CRITICAL stage settles the contested
    findings and routes ESCALATED -> AWAITING_HUMAN with a pending
    critical_stage decision request (the §9 human gate), via _leave_clean_audit
    (risk-dependent forward state — the reason `settled` is NOT a static map key
    can encode)."""
    env = make_stage_env(db, config_dict, tmp_path, risk="critical")
    _seed_finding(db, ref="F-1", status="contested")
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "ESCALATED")
        esc_id = fdb.insert_escalation(
            conn,
            Escalation(
                id=None,
                unit_level="stage",
                unit_id="ph.s1",
                trigger="unresolved_contest",
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
        fdb.resolve_escalation(conn, esc_id, "settled")
    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None

    progressed = await env.executor._step_escalated(stage)

    # _leave_clean_audit returns False for the human-gate path (it parks the
    # stage at AWAITING_HUMAN rather than progressing further).
    assert progressed is False
    assert stage_state(db, "ph.s1") is StageState.AWAITING_HUMAN
    assert fdb.findings(db.read(), "ph.s1")[0].status == "settled"
    assert fdb.findings(db.read(), "ph.s1")[0].resolved_by == "phase_architect"
    pending = fdb.pending_decisions(db.read())
    assert [d.gate_kind for d in pending] == ["critical_stage"]
    assert events_of(db, "ph.s1", "alert") == []


def test_audit_prompt_lists_settled_and_overruled_only_safety_pin(
    db, config_dict, tmp_path
) -> None:
    """SAFETY PIN (the single most safety-critical assertion in slice-2 Unit A):
    the _audit_prompt do-not-re-raise memory lists ONLY findings whose status is
    `settled` or `overruled`. `sustained`, `complied`, and `duplicate` may be
    genuinely unfixed and MUST stay re-raisable — they must NEVER appear. This
    test FAILS if the suppress set ever widens."""
    env = make_stage_env(db, config_dict, tmp_path)
    _seed_finding(db, ref="SET-1", status="settled", severity="minor",
                  auditor_role="auditor_same_model")
    _seed_finding(db, ref="OVR-1", status="overruled", severity="major")
    # The MUST-NOT-appear set — each is potentially a still-live bug.
    _seed_finding(db, ref="SUS-1", status="sustained")
    _seed_finding(db, ref="CMP-1", status="complied")
    _seed_finding(db, ref="DUP-1", status="duplicate")
    _seed_finding(db, ref="OPN-1", status="open")

    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None
    prompt = env.executor._audit_prompt(stage, "auditor_cross_model", env.worktree)

    assert "PREVIOUSLY ADJUDICATED" in prompt
    assert "SET-1" in prompt  # settled -> listed
    assert "OVR-1" in prompt  # overruled -> listed
    # The safety pin: none of these statuses may be suppressed.
    for must_not in ("SUS-1", "CMP-1", "DUP-1", "OPN-1"):
        assert must_not not in prompt, f"{must_not} must remain re-raisable"
    # Refs carry severity + auditor_role (no summary field — no schema change).
    assert "auditor_same_model" in prompt
    assert "minor" in prompt


def test_audit_prompt_no_block_when_no_prior_adjudications(
    db, config_dict, tmp_path
) -> None:
    """No settled/overruled history -> the prompt carries no adjudication block
    (it stays byte-for-byte the original audit instruction plus layout note)."""
    env = make_stage_env(db, config_dict, tmp_path)
    _seed_finding(db, ref="OPN-1", status="open")  # not adjudicated
    _seed_finding(db, ref="SUS-1", status="sustained")  # not in the suppress set
    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None
    prompt = env.executor._audit_prompt(stage, "auditor_cross_model", env.worktree)
    assert "PREVIOUSLY ADJUDICATED" not in prompt


def test_tier2_prompt_carries_prior_adjudications_memory(
    db, config_dict, tmp_path
) -> None:
    """D-0048: the merge-gate integration_validator prompt MUST carry the same
    settled/overruled do-not-re-raise memory as _audit_prompt — else a
    `settled` integration finding (architect-operations §1 no-action) regenerates
    on the re-run merge gate → BUILD → re-contest → loop. Same safety pin:
    settled/overruled listed; sustained/complied/duplicate/open never."""
    env = make_stage_env(db, config_dict, tmp_path)
    _seed_finding(db, ref="SN-INT-001", status="settled", severity="low",
                  auditor_role="integration_validator")
    _seed_finding(db, ref="OVR-1", status="overruled", severity="major")
    for ref, status in (("SUS-1", "sustained"), ("CMP-1", "complied"),
                        ("DUP-1", "duplicate"), ("OPN-1", "open")):
        _seed_finding(db, ref=ref, status=status)
    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None
    prompt = env.executor._tier2_prompt(
        stage, {}, {}, "diff --git a/x b/x\n@@ -1 +1 @@\n+x\n", {}
    )
    assert "PREVIOUSLY ADJUDICATED" in prompt
    assert "SN-INT-001" in prompt  # settled integration finding -> not re-raised
    assert "OVR-1" in prompt  # overruled -> listed
    for must_not in ("SUS-1", "CMP-1", "DUP-1", "OPN-1"):
        assert must_not not in prompt, f"{must_not} must remain re-raisable"


async def test_escalation_rework_build_reason_reaches_builder_prompt(
    db, config_dict, tmp_path
) -> None:
    """CCR-9 emergent benefit: a rework:BUILD resolution with an operator
    reason re-enters BUILD whose entry payload carries that reason on the
    dedicated 'rework_context' key, so the re-spawned Builder's prompt renders
    it as 'Rework context'."""
    env = make_stage_env(db, config_dict, tmp_path)
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "ESCALATED")
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
        fdb.resolve_escalation(conn, esc_id, "rework:BUILD")
        fdb.insert_event(
            conn,
            unit_level="stage",
            unit_id="ph.s1",
            event_type="escalation_resolved",
            actor="main_architect",
            payload={
                "escalation_id": esc_id,
                "resolution": "rework:BUILD",
                "reason": "builder skipped the edge cases",
                "via": "cli",
            },
        )
    env.runner.behaviors["builder_routine"] = builder_writing([0])
    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None
    assert await env.executor._step_escalated(stage) is True
    assert stage_state(db, "ph.s1") is StageState.BUILD
    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None
    assert await env.executor._step_build(stage) is True

    (builder_call,) = [c for c in env.runner.calls if c.role == "builder_routine"]
    assert "Rework context: builder skipped the edge cases." in builder_call.prompt


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


def _audit_comply_with_stray_env(db, config_dict, tmp_path, *, stray: bool):
    """Shared harness for the slice-2 Unit B [20] write-isolation pair: a
    structural stage in AUDIT whose two auditors each raise one finding (round 1
    only), and whose executor COMPLIES with both while ALSO scribbling a stray
    uncommitted SOURCE edit during the response step (the D-0042 incident-20
    shape). ``stray=False`` runs the identical flow without the scribble (the
    clean control). Returns the env; the caller drives env.executor.execute."""
    env = make_stage_env(db, config_dict, tmp_path, risk="structural")
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "AUDIT")
    # The stage worktree must carry a committed source file the stray "edits".
    src = env.worktree / "module.py"
    src.write_text("ORIGINAL = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=env.worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed source"],
        cwd=env.worktree, check=True, capture_output=True,
    )

    raised = {"done": False}  # round-1-only: re-audit after the clean build is empty

    def auditor(ref: str):
        def behavior(cwd: Path, unit_id: str, resume) -> None:
            d = cwd / "_factory" / "stages" / unit_id
            d.mkdir(parents=True, exist_ok=True)
            role = ref.split(":")[0]
            (d / f"audit-{role}.md").write_text(f"finding {ref}", encoding="utf-8")
            payload = (
                {"findings": [{"ref": ref, "severity": "major", "summary": "s"}]}
                if not raised["done"]
                else {"findings": []}
            )
            (d / f"audit-{role}.json").write_text(json.dumps(payload), encoding="utf-8")

        return behavior

    env.runner.behaviors["auditor_same_model"] = auditor("auditor_same_model:F1")
    env.runner.behaviors["auditor_cross_model"] = auditor("auditor_cross_model:F2")

    calls = {"builder": 0}

    def builder(cwd: Path, unit_id: str, resume) -> None:
        calls["builder"] += 1
        d = cwd / "_factory" / "stages" / unit_id
        if calls["builder"] == 1:
            # The triage RESPONSE step: comply with both findings...
            (d / "findings-response.json").write_text(
                json.dumps(
                    {
                        "responses": [
                            {"ref": "auditor_same_model:F1", "action": "comply",
                             "rationale": "will fix"},
                            {"ref": "auditor_cross_model:F2", "action": "comply",
                             "rationale": "will fix"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            if stray:
                # ...and ALSO scribble a stray uncommitted source edit ([20]).
                src.write_text("ORIGINAL = 1\nSCRIBBLED = 2\n", encoding="utf-8")
                (cwd / "stray_new.py").write_text("LEAK = 1\n", encoding="utf-8")
        else:
            # The BUILD rework step: re-audit will find nothing -> close out.
            raised["done"] = True

    env.runner.behaviors["builder_heavy"] = builder
    env.runner.behaviors["validator_structural"] = validator_writing(failing=0)
    return env, src


async def test_audit_comply_discards_stray_triage_writes_unit_b(
    db, config_dict, tmp_path
) -> None:
    """slice-2 Unit B [20] (D-0042): a triage executor that COMPLIES yet also
    edits source in-place during the RESPONSE step no longer wedges comply->BUILD.
    The response sidecar is committed first, then _discard_uncommitted drops the
    stray; the comply->BUILD transition proceeds past the §3.1 isolation gate, the
    stray is gone, and the discarded entries land on the transition payload for
    forensics. The conveyor reaches MERGE_GATE -> OPEN-2 (suite unset)."""
    env, src = _audit_comply_with_stray_env(db, config_dict, tmp_path, stray=True)
    # Natural stop point (proven precedent): MERGE_GATE with no suite command.
    with pytest.raises(ConfigError, match="OPEN-2"):
        await env.executor.execute("ph.s1")

    # Both findings complied; comply routed BUILD (never an IntegrityError).
    findings = {f.finding_ref: f for f in fdb.findings(db.read(), "ph.s1")}
    assert findings["auditor_same_model:F1"].status == "complied"
    assert findings["auditor_cross_model:F2"].status == "complied"
    assert ("AUDIT", "BUILD") in transitions_of(db, "ph.s1")

    # The stray is GONE (reset --hard) — the §3.1 gate before BUILD saw a clean
    # tree, so it never raised; the in-place edit reverted to the committed line.
    assert not (env.worktree / "stray_new.py").exists()
    assert src.read_text(encoding="utf-8") == "ORIGINAL = 1\n"

    # Forensics: the discarded porcelain entries ride the comply->BUILD payload.
    build_entry = [
        e for e in events_of(db, "ph.s1", "transition") if e["to_state"] == "BUILD"
    ]
    assert build_entry, "expected a comply->BUILD transition"
    discarded = json.loads(build_entry[0]["payload_json"]).get("discarded")
    assert discarded is not None
    joined = "\n".join(discarded)
    assert "stray_new.py" in joined and "module.py" in joined


async def test_audit_comply_without_stray_still_clean_unit_b(
    db, config_dict, tmp_path
) -> None:
    """Control for the [20] fix: the identical comply flow WITHOUT a stray edit
    still routes comply->BUILD cleanly, with an empty `discarded` list on the
    payload (the unconditional discard is a safe no-op when the tree is clean)."""
    env, _src = _audit_comply_with_stray_env(db, config_dict, tmp_path, stray=False)
    with pytest.raises(ConfigError, match="OPEN-2"):
        await env.executor.execute("ph.s1")
    assert ("AUDIT", "BUILD") in transitions_of(db, "ph.s1")
    build_entry = [
        e for e in events_of(db, "ph.s1", "transition") if e["to_state"] == "BUILD"
    ]
    assert build_entry
    assert json.loads(build_entry[0]["payload_json"]).get("discarded") == []


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


# ------------------------------------ incident 7 (D-0035): agent-run success gate


def _head(worktree: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


async def test_agent_run_failed_build_exit1_escalates_no_noop_no_commit(
    db, config_dict, tmp_path
) -> None:
    """Incident 7 (D-0035): an exit-1 builder with no changes and no sentinel
    is a DEAD run, never a 'build_noop_accepted' — the gate escalates (literal
    trigger 'agent_run_failed', target phase_architect, evidence payload),
    transitions to ESCALATED, commits nothing, and pages the founder channel."""
    env = make_stage_env(db, config_dict, tmp_path)
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "BUILD")
    env.runner.behaviors["builder_routine"] = lambda cwd, unit_id, resume: None
    env.runner.outcomes["builder_routine"] = [{"exit_code": 1}]
    head_before = _head(env.worktree)

    await env.executor.execute("ph.s1")  # _AgentRunFailed contained in execute()

    assert stage_state(db, "ph.s1") is StageState.ESCALATED
    (esc,) = open_escalations(db, "ph.s1")
    assert esc["trigger"] == "agent_run_failed"
    assert esc["target"] == "phase_architect"
    (event,) = events_of(db, "ph.s1", "agent_run_failed")
    assert esc["event_seq"] == event["seq"]
    payload = json.loads(event["payload_json"])
    assert payload["role"] == "builder_routine"
    assert payload["exit_code"] == 1
    assert payload["killed"] is False
    assert {"process_id", "duration_ms", "stderr_path"} <= set(payload)
    # Zero post-gate consumption: no noop acceptance, no commit-all, no
    # validator spawn — and the founder page went out (§8 B7).
    assert events_of(db, "ph.s1", "build_noop_accepted") == []
    assert _head(env.worktree) == head_before
    assert [c.role for c in env.runner.calls] == ["builder_routine"]
    assert any(
        "agent_run_failed" in title and priority == "max"
        for title, _link, priority in env.notify.published
    )


async def test_agent_run_failed_validator_stale_report_not_recounted(
    db, config_dict, tmp_path
) -> None:
    """Incident 7 manifestation (b)/(c): a dead validator (exit 1, wrote
    nothing) over a scratch that still CONTAINS an old passing report must not
    re-count it as a fresh fix_iteration (the zombie cycle) — the gate
    escalates instead, nothing crosses the isolation boundary, and the scratch
    is disposed (§5.4)."""
    env = make_stage_env(db, config_dict, tmp_path)
    # The stale committed report materializes in the recreated scratch (real
    # worktrees re-sync tracked content; FakeWorktrees.create keeps the dir).
    stale_dir = tmp_path / "scratch" / "ph.s1-validate" / "_factory" / "stages" / "ph.s1"
    stale_dir.mkdir(parents=True)
    (stale_dir / "validation-report.md").write_text("stale PASS\n", encoding="utf-8")
    (stale_dir / "validation-report.json").write_text(
        json.dumps({"failing": 0, "passing": 3, "total": 3}), encoding="utf-8"
    )
    env.runner.outcomes["validator"] = [{"exit_code": 1}]

    await env.executor.execute("ph.s1")

    assert stage_state(db, "ph.s1") is StageState.ESCALATED
    (esc,) = open_escalations(db, "ph.s1")
    assert esc["trigger"] == "agent_run_failed"
    rows = db.read().execute(
        "SELECT COUNT(*) FROM fix_iterations WHERE stage_id='ph.s1'"
    ).fetchone()
    assert rows[0] == 0  # the stale report was never counted as an iteration
    unit_dir = env.worktree / "_factory" / "stages" / "ph.s1"
    assert not (unit_dir / "validation-report.json").exists()  # nothing crossed
    assert env.consultor.calls == []  # CP-1 never consulted on a corpse
    scratch = env.wt.scratch_root / "ph.s1-validate"
    assert env.wt.removed == [scratch]  # §5.4 disposal still ran (finally)


async def test_agent_run_failed_sentinel_precedence_no_double_escalation(
    db, config_dict, tmp_path
) -> None:
    """Gate precedence: an agent that writes _DECLARED_FAILURE.md and THEN
    exits 1 declared itself — only the existing 'agent_declared_failure'
    always-fire path escalates; no 'agent_run_failed' row or event lands."""
    env = make_stage_env(db, config_dict, tmp_path)
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "BUILD")

    def declaring_builder(cwd: Path, unit_id: str, resume) -> None:
        d = cwd / "_factory" / "stages" / unit_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "_DECLARED_FAILURE.md").write_text("cannot proceed", encoding="utf-8")

    env.runner.behaviors["builder_routine"] = declaring_builder
    env.runner.outcomes["builder_routine"] = [{"exit_code": 1}]
    await env.executor.execute("ph.s1")

    assert stage_state(db, "ph.s1") is StageState.ESCALATED
    triggers = [r["trigger"] for r in open_escalations(db, "ph.s1")]
    assert triggers == ["agent_declared_failure"]
    assert events_of(db, "ph.s1", "agent_run_failed") == []


async def test_agent_run_failed_timeout_killed_same_gate(
    db, config_dict, tmp_path
) -> None:
    """A timeout-killed run (killed=True, returncode None — the runner's
    terminate→kill ladder) is every bit as dead as exit 1: same gate, same
    literal trigger, evidence carries the kill shape."""
    env = make_stage_env(db, config_dict, tmp_path)
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "BUILD")
    env.runner.behaviors["builder_routine"] = lambda cwd, unit_id, resume: None
    env.runner.outcomes["builder_routine"] = [
        {"exit_code": None, "timed_out": True, "killed": True}
    ]

    await env.executor.execute("ph.s1")

    assert stage_state(db, "ph.s1") is StageState.ESCALATED
    (esc,) = open_escalations(db, "ph.s1")
    assert esc["trigger"] == "agent_run_failed"
    payload = json.loads(events_of(db, "ph.s1", "agent_run_failed")[0]["payload_json"])
    assert payload["exit_code"] is None
    assert payload["timed_out"] is True
    assert payload["killed"] is True


async def test_agent_run_failed_rework_build_reenters_cleanly(
    db, config_dict, tmp_path
) -> None:
    """§5.5d at-least-once: after an 'agent_run_failed' escalation the standard
    'rework:BUILD' resolution re-enters BUILD cleanly — the dead builder's
    uncommitted partial writes were discarded at the gate (recorded as
    evidence), so the §3.1 isolation assertion passes and a healthy exit-0
    agent completes the stage through VALIDATE."""
    env = make_stage_env(db, config_dict, tmp_path)
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "BUILD")
    env.runner.behaviors["builder_routine"] = builder_writing([0])
    env.runner.behaviors["validator"] = validator_writing(0)
    env.runner.outcomes["builder_routine"] = [{"exit_code": 1}]

    await env.executor.execute("ph.s1")  # dead builder wrote impl-1.py, then died

    assert stage_state(db, "ph.s1") is StageState.ESCALATED
    (esc,) = open_escalations(db, "ph.s1")
    assert esc["trigger"] == "agent_run_failed"
    payload = json.loads(events_of(db, "ph.s1", "agent_run_failed")[0]["payload_json"])
    assert any("impl-1.py" in entry for entry in payload["discarded"])
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=env.worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert porcelain == ""  # corpse leftovers discarded — re-entry stays legal

    with db.transaction() as conn:
        fdb.resolve_escalation(conn, esc["id"], "rework:BUILD")
    # Healthy re-run: BUILD commits, VALIDATE passes (failing=0, routine has no
    # audits) and the conveyor reaches MERGE_GATE, where the unset Tier-1 suite
    # raises the explicit OPEN-2 ConfigError (never a skip).
    with pytest.raises(ConfigError, match="OPEN-2"):
        await env.executor.execute("ph.s1")

    assert stage_state(db, "ph.s1") is StageState.MERGE_GATE
    assert open_escalations(db, "ph.s1") == []
    builder_calls = [c for c in env.runner.calls if c.role == "builder_routine"]
    assert len(builder_calls) == 2  # the failed spawn + the healthy completion


# ------------------------------------ CCR-11 (D-0037): capacity governor


class _RefusalRunner(FakeRunner):
    """FakeRunner that injects a usage-limit refusal text into the results of
    the given roles (others — the probe role included — stay clean); the
    per-role FIFO ``outcomes`` still controls the exit shape."""

    def __init__(self, db, refusal_text: str, roles: set[str]) -> None:
        super().__init__(db)
        self._text = refusal_text
        self._roles = set(roles)

    async def run_agent(self, role: str, prompt: str, **kwargs: Any) -> AgentResult:
        result = await super().run_agent(role, prompt, **kwargs)
        if role in self._roles:
            result = replace(result, result_text=self._text)
        return result


def _enable_governor(
    config_dict,
    *,
    probe_interval_s: float = 0.01,
    notify_architect_on_resume: bool = True,
) -> None:
    config_dict["capacity_governor"] = {
        "enabled": True,
        "probe_interval_s": probe_interval_s,
        "notify_architect_on_resume": notify_architect_on_resume,
    }
    config_dict["models"]["capacity_probe"] = {
        "cli": "claude",
        "model": "haiku",
        "mode": "print",
    }


def make_governor_env(
    db,
    config_dict,
    tmp_path: Path,
    *,
    risk: str = "routine",
    runner=None,
    notify_architect_on_resume: bool = True,
):
    """make_stage_env shape with the governor ENABLED and SHARED (the cli
    wiring contract): one CapacityGovernor instance across executor + tests."""
    _enable_governor(
        config_dict, notify_architect_on_resume=notify_architect_on_resume
    )
    cfg = make_config(config_dict)
    insert_phase(db, "ph", PhaseState.RUNNING)
    worktree = tmp_path / "stage-wt"
    init_repo(worktree)
    insert_stage(db, "ph.s1", "ph", StageState.VALIDATE, risk=risk, worktree=worktree)
    runner = runner if runner is not None else FakeRunner(db)
    notify = FakeNotify()
    governor = CapacityGovernor(db, cfg, runner, notify)
    executor = StageExecutor(
        db,
        StateMachine(db),
        cfg,
        runner,
        FakeWorktrees(tmp_path / "scratch"),
        ThresholdEvaluator(db, cfg),
        FakeConsultor([]),
        notify,
        governor=governor,
    )
    return SimpleNamespace(
        cfg=cfg,
        worktree=worktree,
        runner=runner,
        notify=notify,
        governor=governor,
        executor=executor,
    )


def _factory_events(db, event_type: str) -> list[dict]:
    rows = (
        db.read()
        .execute(
            "SELECT * FROM events WHERE unit_level = 'factory' AND event_type = ?"
            " ORDER BY seq",
            (event_type,),
        )
        .fetchall()
    )
    return [dict(row) for row in rows]


def _all_escalations(db) -> list[dict]:
    rows = db.read().execute("SELECT * FROM escalations ORDER BY id").fetchall()
    return [dict(row) for row in rows]


async def test_capacity_hold_starts_on_signature_match_and_marks_gate(
    db, config_dict, tmp_path
) -> None:
    """HOLD entry (D-0037 item 1+2): a detector match enters the capacity hold
    mechanically — ONE factory-level 'capacity_hold_started' event (signature,
    role, process_id) — and the dead run's incident-7 escalation evidence
    carries the limit mark ``usage_limit: true``."""
    runner = _RefusalRunner(db, "claude: usage limit reached", {"validator"})
    env = make_governor_env(db, config_dict, tmp_path, runner=runner)
    env.runner.outcomes["validator"] = [{"exit_code": 1, "process_id": 9}]

    await env.executor.execute("ph.s1")

    assert env.governor.held is True
    (hold,) = _factory_events(db, "capacity_hold_started")
    assert hold["actor"] == "control_plane"
    assert json.loads(hold["payload_json"]) == {
        "signature": "usage limit",
        "role": "validator",
        "process_id": 9,
    }
    assert stage_state(db, "ph.s1") is StageState.ESCALATED
    (esc,) = open_escalations(db, "ph.s1")
    assert esc["trigger"] == "agent_run_failed"
    (event,) = events_of(db, "ph.s1", "agent_run_failed")
    assert json.loads(event["payload_json"])["usage_limit"] is True
    # The CCR-6 detector behavior is untouched: event + page still land.
    assert len(events_of(db, "ph.s1", "usage_limit_suspected")) == 1
    # A second match while held adds no second hold event (one per episode).
    insert_stage(db, "ph.s2", "ph", StageState.BUILD, worktree=env.worktree)
    runner._roles.add("builder_routine")
    env.runner.outcomes["builder_routine"] = [{"exit_code": 1}]
    # builder_routine routes to the stub here — the hold does not block it.
    await env.executor.execute("ph.s2")
    assert len(_factory_events(db, "capacity_hold_started")) == 1


async def test_capacity_hold_blocks_claude_steps_codex_proceeds(
    db, config_dict, tmp_path
) -> None:
    """HOLD semantics (D-0037 item 3): while held, a step whose spawn set
    contains a claude-route role does not run this tick — state untouched, NO
    event, NO escalation — while codex-routed steps proceed through the
    conveyor (cross-provider independence) up to the next claude-gated step."""
    config_dict["models"]["builder_routine"]["cli"] = "claude"
    config_dict["models"]["builder_heavy"]["cli"] = "codex"
    config_dict["models"]["validator_structural"]["cli"] = "codex"
    config_dict["models"]["integration_validator"]["cli"] = "claude"
    config_dict["risk_classes"]["structural"]["audits"] = []
    env = make_governor_env(db, config_dict, tmp_path, risk="routine")
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "BUILD")
    codex_wt = tmp_path / "stage-wt-2"
    init_repo(codex_wt)
    insert_stage(db, "ph.s2", "ph", StageState.BUILD, risk="structural", worktree=codex_wt)
    env.governor.note_match(signature="usage limit", role="validator", process_id=1)

    env.runner.behaviors["builder_heavy"] = builder_writing([0])
    env.runner.behaviors["validator_structural"] = validator_writing(0)
    await env.executor.execute("ph.s1")  # claude builder — held
    await env.executor.execute("ph.s2")  # codex builder + validator — proceeds

    assert stage_state(db, "ph.s1") is StageState.BUILD  # untouched
    assert events_of(db, "ph.s1") == []  # holding writes NOTHING for the unit
    assert open_escalations(db, "ph.s1") == []  # holding never escalates
    assert [c.role for c in env.runner.calls if c.unit_id == "ph.s1"] == []
    # The codex stage flowed BUILD -> VALIDATE -> MERGE_GATE, then held at the
    # claude-routed Tier-2 spawn BEFORE the OPEN-2 ConfigError the gate would
    # raise — proof the held MERGE_GATE step never started.
    assert stage_state(db, "ph.s2") is StageState.MERGE_GATE
    assert [c.role for c in env.runner.calls if c.unit_id == "ph.s2"] == [
        "builder_heavy",
        "validator_structural",
    ]
    assert open_escalations(db, "ph.s2") == []


async def test_capacity_probe_failure_keeps_hold_and_never_escalates(
    db, config_dict, tmp_path
) -> None:
    """PROBE failure (D-0037 item 4): a dead canary (exit 1) and a canary that
    answers with a refusal text both keep the hold — and NEITHER produces an
    escalation row (probes are exempt from the incident-7 gate by
    construction: spawned directly through the runner)."""
    env = make_governor_env(db, config_dict, tmp_path)
    env.governor.note_match(signature="usage limit", role="validator", process_id=1)
    escalations_before = _all_escalations(db)

    env.runner.outcomes["capacity_probe"] = [{"exit_code": 1}]
    await asyncio.sleep(0.02)  # past probe_interval_s=0.01
    await env.governor.tick()
    assert env.governor.held is True

    # Exit-0 refusal: the result text still matches a signature — held.
    env.runner.outcomes["capacity_probe"] = [
        {"exit_code": 0, "result_text": "error: usage limit reached, retry later"}
    ]
    await asyncio.sleep(0.02)
    await env.governor.tick()
    assert env.governor.held is True

    probe_calls = [c for c in env.runner.calls if c.role == "capacity_probe"]
    assert len(probe_calls) == 2
    assert all(c.unit_id == "factory" for c in probe_calls)
    assert _factory_events(db, "capacity_hold_ended") == []
    assert _all_escalations(db) == escalations_before  # NO escalation rows
    assert env.notify.published == []  # no resume page while held


async def test_capacity_probe_success_lifts_hold_pages_and_auto_resolves_strictly(
    db, config_dict, tmp_path
) -> None:
    """PROBE success + AUTO-RESOLVE (D-0037 items 4+5): the hold lifts with a
    'capacity_hold_ended' event and the Romanian founder page, and EXACTLY the
    limit-marked agent_run_failed escalations resolve (rework token from the
    ESCALATED transition's from_state) — a non-limit agent_run_failed and an
    unresolved_contest stay open (pinned)."""
    runner = _RefusalRunner(db, "error: usage limit reached", {"validator"})
    env = make_governor_env(db, config_dict, tmp_path, runner=runner)
    # Limit-marked corpse: validator dies with the refusal -> usage_limit: true.
    env.runner.outcomes["validator"] = [{"exit_code": 1}]
    await env.executor.execute("ph.s1")
    assert env.governor.held is True
    # Non-limit corpse on a sibling: clean exit-1 builder, no signature.
    wt2 = tmp_path / "stage-wt-2"
    init_repo(wt2)
    insert_stage(db, "ph.s2", "ph", StageState.BUILD, worktree=wt2)
    env.runner.outcomes["builder_routine"] = [{"exit_code": 1}]
    await env.executor.execute("ph.s2")
    assert json.loads(
        events_of(db, "ph.s2", "agent_run_failed")[0]["payload_json"]
    )["usage_limit"] is False
    # A different open trigger on a third stage: NEVER auto-resolved.
    insert_stage(db, "ph.s3", "ph", StageState.ESCALATED)
    with db.transaction() as conn:
        fdb.insert_escalation(
            conn,
            Escalation(
                id=None,
                unit_level="stage",
                unit_id="ph.s3",
                trigger="unresolved_contest",
                target="phase_architect",
                payload_artifact_id=None,
                event_seq=None,
                status="open",
                resolution=None,
                created_at=utc_now(),
                resolved_at=None,
            ),
        )

    await asyncio.sleep(0.02)
    await env.governor.tick()  # default FakeRunner result: exit 0, clean text

    assert env.governor.held is False
    (ended,) = _factory_events(db, "capacity_hold_ended")
    assert "probe_process_id" in json.loads(ended["payload_json"])
    # The resume page (the detector/B7 pages preceded it during the outage).
    (resume,) = [
        p
        for p in env.notify.published
        if p[0] == "Capacitate revenită — fabrica a reluat singură"
    ]
    assert resume[2] == "max" and resume[1] is not None and resume[1].startswith("http")
    # STRICT scope: only the limit-marked ph.s1 row resolved.
    rows = {(r["unit_id"], r["trigger"]): r for r in _all_escalations(db)}
    s1 = rows[("ph.s1", "agent_run_failed")]
    assert s1["status"] == "resolved"
    assert s1["resolution"] == "rework:VALIDATE"  # from_state VALIDATE
    assert rows[("ph.s2", "agent_run_failed")]["status"] == "open"
    assert rows[("ph.s3", "unresolved_contest")]["status"] == "open"
    (resolved_event,) = events_of(db, "ph.s1", "escalation_resolved")
    assert resolved_event["actor"] == "capacity_governor"
    payload = json.loads(resolved_event["payload_json"])
    assert payload["resolution"] == "rework:VALIDATE"
    assert payload["reason"] == (
        "capacity hold lifted — limit-class failure auto-resumed (D-0037)"
    )
    # The normal _step_escalated pickup routes the stage back to VALIDATE and
    # the conveyor completes to MERGE_GATE (OPEN-2 raises there, as always).
    env.runner.behaviors["validator"] = validator_writing(0)
    runner._roles.discard("validator")
    with pytest.raises(ConfigError, match="OPEN-2"):
        await env.executor.execute("ph.s1")
    assert ("ESCALATED", "VALIDATE") in transitions_of(db, "ph.s1")


_FOUNDER_RESUME = "Capacitate revenită — fabrica a reluat singură"
_ARCHITECT_RESUME = "[arhitect] capacitate revenită — reia lucrul"


class _ArchitectFailNotify(FakeNotify):
    """FakeNotify that delivers every push EXCEPT the architect resume page —
    isolates UNIT 3's containment (founder page lands, architect page fails)."""

    async def publish(self, title: str, *, link: str | None = None, priority: str = "default"):
        if title.startswith("[arhitect]"):
            raise NotifyError("ntfy down for the architect push (fake)")
        await super().publish(title, link=link, priority=priority)


async def _drain_to_hold(db, env) -> None:
    """Drive ph.s1's limit-killed validator through the incident-7 gate so the
    governor enters the hold (the proven drain dance), leaving exactly one open
    limit-marked agent_run_failed escalation to auto-resolve on lift."""
    env.runner.outcomes["validator"] = [{"exit_code": 1}]
    await env.executor.execute("ph.s1")
    assert env.governor.held is True


async def test_capacity_resume_pages_architect(db, config_dict, tmp_path) -> None:
    """UNIT 3 (D-0042): on hold LIFT the governor now emits TWO pushes — the
    unchanged founder "Capacitate revenită…" AND the distinct '[arhitect]'
    resume page so the architect resumes alone. MUTATION: without UNIT 3 only
    the founder push exists."""
    runner = _RefusalRunner(db, "error: usage limit reached", {"validator"})
    env = make_governor_env(db, config_dict, tmp_path, runner=runner)
    await _drain_to_hold(db, env)

    await asyncio.sleep(0.02)
    await env.governor.tick()

    assert env.governor.held is False
    titles = [p[0] for p in env.notify.published]
    assert _FOUNDER_RESUME in titles  # founder page UNCHANGED
    (architect,) = [p for p in env.notify.published if p[0] == _ARCHITECT_RESUME]
    assert architect[2] == "max"  # priority_alert
    assert architect[1] is not None and architect[1].startswith("http")
    # The lift fact is intact.
    assert len(_factory_events(db, "capacity_hold_ended")) == 1


async def test_capacity_resume_architect_page_suppressed_when_disabled(
    db, config_dict, tmp_path
) -> None:
    """notify_architect_on_resume:false suppresses ONLY the architect page — the
    founder resume page still fires and the hold STILL lifts with its
    'capacity_hold_ended' event (the suppress toggle never touches the lift)."""
    runner = _RefusalRunner(db, "error: usage limit reached", {"validator"})
    env = make_governor_env(
        db, config_dict, tmp_path, runner=runner, notify_architect_on_resume=False
    )
    await _drain_to_hold(db, env)

    await asyncio.sleep(0.02)
    await env.governor.tick()

    assert env.governor.held is False
    titles = [p[0] for p in env.notify.published]
    assert _FOUNDER_RESUME in titles  # founder page still present
    assert _ARCHITECT_RESUME not in titles  # architect page suppressed
    assert len(_factory_events(db, "capacity_hold_ended")) == 1


async def test_capacity_resume_architect_page_failure_is_contained(
    db, config_dict, tmp_path
) -> None:
    """CONTAINMENT (Doctrine §7): a failed architect push logs ONE
    'alert_delivery_failed' (kind capacity_hold_ended_architect), the founder
    page still lands, the hold STILL lifts and 'capacity_hold_ended' is STILL
    written, and NOTHING raises out of _lift_hold."""
    runner = _RefusalRunner(db, "error: usage limit reached", {"validator"})
    env = make_governor_env(db, config_dict, tmp_path, runner=runner)
    # Swap in the architect-only-failing publisher AFTER the drain so the hold
    # entry's clean pages aren't disturbed; the governor holds this instance.
    failing = _ArchitectFailNotify()
    await _drain_to_hold(db, env)
    env.governor._notify = failing

    await asyncio.sleep(0.02)
    await env.governor.tick()  # must NOT raise

    assert env.governor.held is False  # lift committed despite the failed page
    assert len(_factory_events(db, "capacity_hold_ended")) == 1  # event written
    # The founder page on THIS publisher landed; the architect page did not.
    titles = [p[0] for p in failing.published]
    assert _FOUNDER_RESUME in titles
    assert _ARCHITECT_RESUME not in titles
    # Exactly one contained delivery-failure record, with the architect kind.
    failures = [
        json.loads(e["payload_json"])
        for e in _factory_events(db, "alert_delivery_failed")
    ]
    arch_failures = [f for f in failures if f["kind"] == "capacity_hold_ended_architect"]
    assert len(arch_failures) == 1
    assert "error" in arch_failures[0]


async def test_capacity_resolution_map_audit_maps_to_rework_validate(
    db, config_dict, tmp_path
) -> None:
    """from_state -> token mapping (D-0037 item 5): AUDIT resolves as
    'rework:VALIDATE' (no rework:AUDIT exists in the vocabulary — pinned),
    MERGE_GATE resolves as 'rework:MERGE_GATE' (D-0057), the map's values all
    belong to STAGE_ESCALATION_RESOLUTIONS, and the routing re-enters VALIDATE
    through the untouched transition table."""
    from sf_factory.models import STAGE_ESCALATION_RESOLUTIONS

    assert sched_mod._CAPACITY_RESOLUTIONS == {
        "SPEC": "rework:SPEC",
        "BUILD": "rework:BUILD",
        "VALIDATE": "rework:VALIDATE",
        "AUDIT": "rework:VALIDATE",
        "MERGE_GATE": "rework:MERGE_GATE",
    }
    assert set(sched_mod._CAPACITY_RESOLUTIONS.values()) <= set(
        STAGE_ESCALATION_RESOLUTIONS
    )
    # D-0057: MERGE_GATE IS now mapped (deliberately ABSENT pre-D-0057). A
    # limit-killed Tier-2 (agent_run_failed + usage_limit) is the ONE failure
    # class the architect could not recover manually — frozen by the SAME weekly
    # limit (incidents [61]/[53]). The capacity governor auto-resolves it to
    # 'rework:MERGE_GATE' (re-runs ONLY the gate); safe because _auto_resolve's
    # trigger='agent_run_failed' filter structurally excludes unresolved_contest
    # and a stage at merge-gate has passed structural validation + dual AUDIT.
    assert sched_mod._CAPACITY_RESOLUTIONS["MERGE_GATE"] == "rework:MERGE_GATE"

    env = make_governor_env(db, config_dict, tmp_path, risk="structural")
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "AUDIT")
    sm = StateMachine(db)

    def coupled(conn) -> None:
        seq = fdb.insert_event(
            conn,
            unit_level="stage",
            unit_id="ph.s1",
            event_type="agent_run_failed",
            actor="control_plane",
            payload={"role": "auditor_same_model", "usage_limit": True},
        )
        fdb.insert_escalation(
            conn,
            Escalation(
                id=None,
                unit_level="stage",
                unit_id="ph.s1",
                trigger="agent_run_failed",
                target="phase_architect",
                payload_artifact_id=None,
                event_seq=seq,
                status="open",
                resolution=None,
                created_at=utc_now(),
                resolved_at=None,
            ),
        )

    sm.transition(
        Level.STAGE,
        "ph.s1",
        StageState.ESCALATED.value,
        actor="control_plane",
        reason="agent run failed: not-cleanly-zero exit, no sentinel declared",
        coupled=coupled,
    )
    env.governor.note_match(
        signature="usage limit", role="auditor_same_model", process_id=1
    )
    await asyncio.sleep(0.02)
    await env.governor.tick()

    (row,) = _all_escalations(db)
    assert row["status"] == "resolved" and row["resolution"] == "rework:VALIDATE"


async def test_capacity_governor_disabled_is_byte_identical(
    db, config_dict, tmp_path
) -> None:
    """enabled:false pin (D-0037 item 7): without the section (default
    disabled) a signature-matching dead run behaves EXACTLY like pre-CCR-11 —
    same escalation with NO usage_limit key in the evidence, no hold events,
    no blocking, no probe spawns; the CCR-6 detector page is unchanged."""
    assert "capacity_governor" not in config_dict
    runner = _RefusalRunner(db, "claude: usage limit reached", {"validator"})
    env = make_stage_env(db, config_dict, tmp_path)
    executor = StageExecutor(
        db,
        StateMachine(db),
        env.cfg,
        runner,
        FakeWorktrees(tmp_path / "scratch"),
        ThresholdEvaluator(db, env.cfg),
        FakeConsultor([]),
        env.notify,
    )
    runner.outcomes["validator"] = [{"exit_code": 1}]

    await executor.execute("ph.s1")

    assert stage_state(db, "ph.s1") is StageState.ESCALATED
    (event,) = events_of(db, "ph.s1", "agent_run_failed")
    assert "usage_limit" not in json.loads(event["payload_json"])  # byte-identical
    assert _factory_events(db, "capacity_hold_started") == []
    assert [c.role for c in runner.calls] == ["validator"]  # no probe spawn
    assert len(events_of(db, "ph.s1", "usage_limit_suspected")) == 1  # CCR-6 kept
    # Holding never engages: a later step on a fresh stage spawns normally.
    insert_stage(db, "ph.s2", "ph", StageState.BUILD, worktree=env.worktree)
    runner.behaviors["builder_routine"] = builder_writing([0])
    runner.behaviors["validator"] = validator_writing(0)
    runner._roles.clear()
    with pytest.raises(ConfigError, match="OPEN-2"):
        await executor.execute("ph.s2")
    assert stage_state(db, "ph.s2") is StageState.MERGE_GATE


async def test_phase_planning_signature_enters_hold_and_blocks_planning(
    db, config_dict, tmp_path
) -> None:
    """PhaseExecutor wiring: a planning-spawn match enters the SHARED hold,
    and while held a claude-routed PLANNING step does not run (the phase
    executor's spawn path is gated like the stage conveyor)."""
    _enable_governor(config_dict)
    config_dict["models"]["phase_architect"]["cli"] = "claude"
    config_dict["models"]["phase_architect"]["mode"] = "print"
    cfg = make_config(config_dict)
    insert_phase(db, "ph-a", PhaseState.PLANNING)
    insert_phase(db, "ph-b", PhaseState.PLANNING)
    notify = FakeNotify()
    runner = _SignatureRunner(db, "claude: usage limit reached for this window")
    governor = CapacityGovernor(db, cfg, runner, notify)
    executor = make_phase_executor(
        db, cfg, runner=runner, notify=notify, governor=governor
    )

    await executor.execute("ph-a")  # match -> hold (plan also fails validation)
    assert governor.held is True
    assert len(_factory_events(db, "capacity_hold_started")) == 1

    calls_before = len(runner.calls)
    await executor.execute("ph-b")  # held claude planning: does not run
    assert len(runner.calls) == calls_before
    assert phase_state(db, "ph-b") is PhaseState.PLANNING
    assert open_escalations(db, "ph-b") == []


async def test_scheduler_loop_probes_and_resumes_alone(db, config_dict) -> None:
    """Loop wiring (D-0037 item 4): Scheduler.run_until_blocked drives the
    governor's tick — a due probe runs and a successful canary lifts the hold
    with the resume page, no architect in the loop."""
    _enable_governor(config_dict)
    cfg = make_config(config_dict)
    notify = FakeNotify()
    runner = FakeRunner(db)
    governor = CapacityGovernor(db, cfg, runner, notify)
    sm = StateMachine(db)
    executors = {
        Level.PHASE: ScriptedExecutor(Level.PHASE, db, sm),
        Level.STAGE: ScriptedExecutor(Level.STAGE, db, sm),
    }
    scheduler = Scheduler(db, sm, cfg, executors, notify, governor=governor)
    governor.note_match(signature="usage limit", role="validator", process_id=1)
    await asyncio.sleep(0.02)

    await run_blocked(scheduler)

    assert governor.held is False
    assert [c.role for c in runner.calls] == ["capacity_probe"]
    assert len(_factory_events(db, "capacity_hold_ended")) == 1
    assert notify.published[0][0] == "Capacitate revenită — fabrica a reluat singură"


def test_recover_reconciles_stale_hold_event_pair(db, config_dict) -> None:
    """Restart honesty (D-0037 item 6): holds are in-memory — recover() closes
    an unclosed capacity_hold_started pair from a previous process so the
    dashboard read-path never shows a hold no live governor owns; a closed
    pair is left alone."""
    _enable_governor(config_dict)
    cfg = make_config(config_dict)
    with db.transaction() as conn:
        fdb.insert_event(
            conn,
            unit_level="factory",
            unit_id=None,
            event_type="capacity_hold_started",
            actor="control_plane",
            payload={"signature": "usage limit", "role": "validator", "process_id": 1},
        )
    notify = FakeNotify()
    governor = CapacityGovernor(db, cfg, FakeRunner(db), notify)
    sm = StateMachine(db)
    executors = {
        Level.PHASE: ScriptedExecutor(Level.PHASE, db, sm),
        Level.STAGE: ScriptedExecutor(Level.STAGE, db, sm),
    }
    scheduler = Scheduler(db, sm, cfg, executors, notify, governor=governor)
    scheduler.recover()
    (ended,) = _factory_events(db, "capacity_hold_ended")
    assert "restart" in json.loads(ended["payload_json"])["reason"]
    scheduler.recover()  # closed pair: idempotent, no second closing event
    assert len(_factory_events(db, "capacity_hold_ended")) == 1


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


def test_spec_prompt_rework_context_and_read_first_extras(
    db, config_dict, tmp_path
) -> None:
    """CCR-9: a SPEC re-entry payload's 'rework_context' renders as 'Rework
    context' and the prompt lists exactly the existing re-entry artifacts as
    'Read first' extras (the CCR-8 _build_prompt pattern, mirrored)."""
    env = make_stage_env(db, config_dict, tmp_path)
    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None
    phase = fdb.get_phase(db.read(), "ph")
    assert phase is not None
    unit_dir = env.worktree / "_factory" / "stages" / "ph.s1"
    unit_dir.mkdir(parents=True)
    (unit_dir / "validation-report.md").write_text("findings", encoding="utf-8")
    (unit_dir / "build-notes.md").write_text("notes", encoding="utf-8")
    prompt = env.executor._spec_prompt(
        stage, phase, env.worktree, {"rework_context": "spec contradiction X"}
    )
    assert "Rework context: spec contradiction X." in prompt
    assert (
        "Read first: _factory/stages/ph.s1/validation-report.md, "
        "_factory/stages/ph.s1/build-notes.md." in prompt
    )
    assert "escalation-payload.md" not in prompt  # absent file is never listed


async def test_spec_prompt_fresh_entry_has_no_rework_context(
    db, config_dict, tmp_path
) -> None:
    """CCR-9 fresh-dispatch regression pin: the REAL PENDING -> SPEC dispatch
    transition merges its generic reason into the stored payload; the Spec
    Agent prompt built from that payload exactly as _step_spec builds it must
    stay context-free."""
    env = make_stage_env(db, config_dict, tmp_path)
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "PENDING")
    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None
    assert await env.executor._step_dispatch(stage) is True
    assert stage_state(db, "ph.s1") is StageState.SPEC

    entry = sched_mod._last_transition_payload(
        db.read(), Level.STAGE.value, "ph.s1", StageState.SPEC.value
    )
    # The defect's precondition is real: the state machine merged the generic
    # transition reason into the entry payload the prompt builder receives.
    assert entry["reason"] == "DAG deps DONE, dispatched"
    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None
    phase = fdb.get_phase(db.read(), "ph")
    assert phase is not None
    assert stage.worktree_path is not None
    prompt = env.executor._spec_prompt(stage, phase, Path(stage.worktree_path), entry)
    assert "Rework context" not in prompt
    assert "Read first" not in prompt


async def test_build_prompt_fresh_entry_has_no_rework_context(
    db, config_dict, tmp_path
) -> None:
    """CCR-9 fresh-build regression pin (pre-existing CCR-8 noise): the REAL
    SPEC -> BUILD transition ('spec artifact registered', via _step_spec)
    carries no rework_context, so the Builder spawned by _step_build gets a
    context-free prompt despite the merged generic reason."""
    env = make_stage_env(db, config_dict, tmp_path)
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "SPEC")

    def spec_agent(cwd: Path, unit_id: str, resume) -> None:
        d = cwd / "_factory" / "stages" / unit_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "spec.md").write_text("spec body\n", encoding="utf-8")

    env.runner.behaviors["spec_agent"] = spec_agent
    env.runner.behaviors["builder_routine"] = builder_writing([0])
    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None
    assert await env.executor._step_spec(stage) is True
    assert stage_state(db, "ph.s1") is StageState.BUILD
    entry = sched_mod._last_transition_payload(
        db.read(), Level.STAGE.value, "ph.s1", StageState.BUILD.value
    )
    assert entry["reason"] == "spec artifact registered"  # the merge is real
    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None
    assert await env.executor._step_build(stage) is True

    (builder_call,) = [c for c in env.runner.calls if c.role == "builder_routine"]
    assert "Rework context" not in builder_call.prompt


async def test_build_prompt_audit_comply_rework_context(
    db, config_dict, tmp_path
) -> None:
    """CCR-9: the audit-comply rework re-entry sets the dedicated
    'rework_context' key (the same sentence as its transition reason), and the
    re-spawned Builder's prompt renders it. Replays the exact _step_audit
    comply transition (reason + payload) through the real state machine."""
    env = make_stage_env(db, config_dict, tmp_path)
    with db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "AUDIT")
    StateMachine(db).transition(
        Level.STAGE,
        "ph.s1",
        StageState.BUILD.value,
        actor="control_plane",
        reason="executor complies with audit finding(s) — rework",
        payload={
            "complied": ["F-1"],
            "rework_context": "executor complies with audit finding(s) — rework",
        },
    )
    env.runner.behaviors["builder_routine"] = builder_writing([0])
    stage = fdb.get_stage(db.read(), "ph.s1")
    assert stage is not None
    assert await env.executor._step_build(stage) is True

    (builder_call,) = [c for c in env.runner.calls if c.role == "builder_routine"]
    assert (
        "Rework context: executor complies with audit finding(s) — rework."
        in builder_call.prompt
    )
