"""Unit tests for sf_factory.statemachine (design §3/§4/§8).

Covers: legal/illegal transitions against both §3 tables (full closure sweep),
atomicity of coupled writes (fault injection mid-tx rolls back state + event +
coupled rows together), the event row contract (type/from/to/actor/payload),
and fail-explicit handling of unknown units/states/levels.

Fixtures beyond tests/conftest.py (frozen, wave 1) are defined locally here.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from sf_factory.db import (
    Database,
    get_phase,
    get_stage,
    insert_phase,
    insert_stage,
)
from sf_factory.models import (
    VALID_PHASE_TRANSITIONS,
    VALID_STAGE_TRANSITIONS,
    Level,
    Phase,
    PhaseState,
    Stage,
    StageState,
    TransitionError,
    utc_now,
)
from sf_factory.statemachine import StateMachine

# ------------------------------------------------------------------ local helpers


def _phase_row(phase_id: str, state: PhaseState = PhaseState.PENDING) -> Phase:
    now = utc_now()
    return Phase(
        id=phase_id,
        project="proj",
        name=phase_id,
        state=state,
        branch=None,
        plan_artifact_id=None,
        created_at=now,
        updated_at=now,
    )


def _stage_row(
    stage_id: str, phase_id: str, state: StageState = StageState.PENDING
) -> Stage:
    now = utc_now()
    return Stage(
        id=stage_id,
        phase_id=phase_id,
        name=stage_id,
        risk_class="routine",
        state=state,
        branch=None,
        worktree_path=None,
        spec_artifact_id=None,
        created_at=now,
        updated_at=now,
    )


def _seed_stage(db: Database, stage_id: str, state: StageState = StageState.PENDING) -> None:
    with db.transaction() as conn:
        insert_phase(conn, _phase_row(f"ph-{stage_id}"))
        insert_stage(conn, _stage_row(stage_id, f"ph-{stage_id}", state=state))


def _seed_phase(db: Database, phase_id: str, state: PhaseState = PhaseState.PENDING) -> None:
    with db.transaction() as conn:
        insert_phase(conn, _phase_row(phase_id, state=state))


def _events(db: Database, unit_level: str, unit_id: str) -> list[sqlite3.Row]:
    return db.read().execute(
        "SELECT * FROM events WHERE unit_level = ? AND unit_id = ? ORDER BY seq",
        (unit_level, unit_id),
    ).fetchall()


def _fix_iteration_count(db: Database, stage_id: str) -> int:
    row = db.read().execute(
        "SELECT COUNT(*) FROM fix_iterations WHERE stage_id = ?", (stage_id,)
    ).fetchone()
    return int(row[0])


@pytest.fixture()
def sm(db: Database) -> StateMachine:
    return StateMachine(db)


# -------------------------------------------------------------- legal transitions


def test_legal_stage_transition_updates_state_and_appends_event(db, sm):
    _seed_stage(db, "st-1")
    seq = sm.transition(
        Level.STAGE, "st-1", StageState.SPEC.value, actor="control_plane", reason="deps done"
    )
    stage = get_stage(db.read(), "st-1")
    assert stage is not None and stage.state is StageState.SPEC
    events = _events(db, "stage", "st-1")
    assert len(events) == 1
    event = events[0]
    assert event["seq"] == seq
    assert event["event_type"] == "transition"
    assert event["from_state"] == "PENDING"
    assert event["to_state"] == "SPEC"
    assert event["actor"] == "control_plane"
    assert json.loads(event["payload_json"]) == {"reason": "deps done"}


def test_legal_phase_transition_updates_state_and_appends_event(db, sm):
    _seed_phase(db, "ph-1")
    seq = sm.transition(
        Level.PHASE, "ph-1", PhaseState.PLANNING.value, actor="control_plane", reason="deps done"
    )
    phase = get_phase(db.read(), "ph-1")
    assert phase is not None and phase.state is PhaseState.PLANNING
    events = _events(db, "phase", "ph-1")
    assert len(events) == 1
    assert events[0]["seq"] == seq
    assert (events[0]["from_state"], events[0]["to_state"]) == ("PENDING", "PLANNING")


def test_transition_accepts_enum_to_state_and_string_level(db, sm):
    _seed_stage(db, "st-enum")
    # StageState is a StrEnum and Level coerces from its value — both spellings work.
    sm.transition("stage", "st-enum", StageState.SPEC, actor="control_plane", reason="r")
    stage = get_stage(db.read(), "st-enum")
    assert stage is not None and stage.state is StageState.SPEC


def test_transition_returns_monotonic_event_seq(db, sm):
    _seed_stage(db, "st-seq")
    seq1 = sm.transition(Level.STAGE, "st-seq", "SPEC", actor="control_plane", reason="a")
    seq2 = sm.transition(Level.STAGE, "st-seq", "BUILD", actor="control_plane", reason="b")
    assert seq2 > seq1


def test_reason_and_payload_merged_with_reason_winning(db, sm):
    _seed_stage(db, "st-pay")
    payload = {"detail": "x", "reason": "stale"}
    sm.transition(
        Level.STAGE, "st-pay", "SPEC", actor="control_plane", reason="real", payload=payload
    )
    event = _events(db, "stage", "st-pay")[0]
    assert json.loads(event["payload_json"]) == {"detail": "x", "reason": "real"}
    # The caller's dict is never mutated.
    assert payload == {"detail": "x", "reason": "stale"}


# ------------------------------------------------------------ illegal transitions


def test_illegal_stage_transition_raises_and_writes_nothing(db, sm):
    _seed_stage(db, "st-bad")
    with pytest.raises(TransitionError):
        sm.transition(Level.STAGE, "st-bad", "BUILD", actor="control_plane", reason="skip SPEC")
    stage = get_stage(db.read(), "st-bad")
    assert stage is not None and stage.state is StageState.PENDING
    assert _events(db, "stage", "st-bad") == []


def test_terminal_stage_states_reject_every_transition(db, sm):
    for terminal in (StageState.DONE, StageState.FAILED, StageState.CANCELLED):
        stage_id = f"st-term-{terminal.value}"
        _seed_stage(db, stage_id, state=terminal)
        for to_state in StageState:
            with pytest.raises(TransitionError):
                sm.transition(
                    Level.STAGE, stage_id, to_state.value, actor="control_plane", reason="r"
                )
        stage = get_stage(db.read(), stage_id)
        assert stage is not None and stage.state is terminal


def test_unknown_unit_raises_transition_error(db, sm):
    with pytest.raises(TransitionError):
        sm.transition(Level.STAGE, "st-ghost", "SPEC", actor="control_plane", reason="r")
    with pytest.raises(TransitionError):
        sm.transition(Level.PHASE, "ph-ghost", "PLANNING", actor="control_plane", reason="r")


def test_unknown_state_string_raises_transition_error(db, sm):
    _seed_stage(db, "st-warp")
    with pytest.raises(TransitionError):
        sm.transition(Level.STAGE, "st-warp", "WARP", actor="control_plane", reason="r")
    # A phase-only state is unknown at stage level (and vice versa).
    with pytest.raises(TransitionError):
        sm.transition(Level.STAGE, "st-warp", "PLANNING", actor="control_plane", reason="r")
    assert _events(db, "stage", "st-warp") == []


def test_unknown_level_raises_transition_error(db, sm):
    with pytest.raises(TransitionError):
        sm.transition("factory", "st-1", "SPEC", actor="control_plane", reason="r")


# ----------------------------------------------------------- full closure sweeps


def test_stage_transition_table_closure(db, sm):
    """Every §3.1 table entry executes; every non-entry raises and changes nothing."""
    states = list(StageState)
    with db.transaction() as conn:
        insert_phase(conn, _phase_row("ph-sweep"))
        for from_state in states:
            for to_state in states:
                insert_stage(
                    conn,
                    _stage_row(
                        f"st-{from_state.value}-{to_state.value}", "ph-sweep", state=from_state
                    ),
                )
    for from_state in states:
        for to_state in states:
            stage_id = f"st-{from_state.value}-{to_state.value}"
            if to_state in VALID_STAGE_TRANSITIONS[from_state]:
                sm.transition(
                    Level.STAGE, stage_id, to_state.value, actor="control_plane", reason="sweep"
                )
                expected = to_state
            else:
                with pytest.raises(TransitionError):
                    sm.transition(
                        Level.STAGE,
                        stage_id,
                        to_state.value,
                        actor="control_plane",
                        reason="sweep",
                    )
                expected = from_state
            stage = get_stage(db.read(), stage_id)
            assert stage is not None and stage.state is expected, (from_state, to_state)


def test_phase_transition_table_closure(db, sm):
    """Every §3.2 table entry executes; every non-entry raises and changes nothing."""
    states = list(PhaseState)
    with db.transaction() as conn:
        for from_state in states:
            for to_state in states:
                insert_phase(
                    conn,
                    _phase_row(f"ph-{from_state.value}-{to_state.value}", state=from_state),
                )
    for from_state in states:
        for to_state in states:
            phase_id = f"ph-{from_state.value}-{to_state.value}"
            if to_state in VALID_PHASE_TRANSITIONS[from_state]:
                sm.transition(
                    Level.PHASE, phase_id, to_state.value, actor="control_plane", reason="sweep"
                )
                expected = to_state
            else:
                with pytest.raises(TransitionError):
                    sm.transition(
                        Level.PHASE,
                        phase_id,
                        to_state.value,
                        actor="control_plane",
                        reason="sweep",
                    )
                expected = from_state
            phase = get_phase(db.read(), phase_id)
            assert phase is not None and phase.state is expected, (from_state, to_state)


# ------------------------------------------------------------------ coupled writes


def test_coupled_write_commits_with_transition(db, sm):
    """The §4 example: a fix-iteration insert coupled with the BUILD->VALIDATE
    transition lands in the same transaction."""
    from sf_factory.db import insert_fix_iteration

    _seed_stage(db, "st-coupled", state=StageState.BUILD)
    iterations: list[int] = []

    def coupled(conn: sqlite3.Connection) -> None:
        iterations.append(insert_fix_iteration(conn, "st-coupled", 5, None))

    sm.transition(
        Level.STAGE,
        "st-coupled",
        "VALIDATE",
        actor="control_plane",
        reason="build committed",
        coupled=coupled,
    )
    assert iterations == [1]
    assert _fix_iteration_count(db, "st-coupled") == 1
    stage = get_stage(db.read(), "st-coupled")
    assert stage is not None and stage.state is StageState.VALIDATE


def test_coupled_failure_rolls_back_state_and_event(db, sm):
    """§8: fault injection mid-tx — state update, event row AND coupled rows all
    roll back together; the exception propagates unmasked."""
    from sf_factory.db import insert_fix_iteration

    _seed_stage(db, "st-atomic", state=StageState.BUILD)

    def exploding(conn: sqlite3.Connection) -> None:
        insert_fix_iteration(conn, "st-atomic", 5, None)  # would commit if tx were split
        raise RuntimeError("injected fault after coupled insert")

    with pytest.raises(RuntimeError, match="injected fault"):
        sm.transition(
            Level.STAGE,
            "st-atomic",
            "VALIDATE",
            actor="control_plane",
            reason="r",
            coupled=exploding,
        )
    stage = get_stage(db.read(), "st-atomic")
    assert stage is not None and stage.state is StageState.BUILD
    assert _events(db, "stage", "st-atomic") == []
    assert _fix_iteration_count(db, "st-atomic") == 0


def test_coupled_runs_inside_the_same_transaction(db, sm):
    """The coupled callback sees the already-updated state and the already-written
    event on its connection — one transaction, not three."""
    _seed_stage(db, "st-sametx", state=StageState.BUILD)
    seen: dict[str, object] = {}

    def coupled(conn: sqlite3.Connection) -> None:
        stage = get_stage(conn, "st-sametx")
        assert stage is not None
        seen["state"] = stage.state
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE unit_level='stage' AND unit_id='st-sametx'"
        ).fetchone()
        seen["events"] = int(row[0])

    sm.transition(
        Level.STAGE, "st-sametx", "VALIDATE", actor="control_plane", reason="r", coupled=coupled
    )
    assert seen == {"state": StageState.VALIDATE, "events": 1}
