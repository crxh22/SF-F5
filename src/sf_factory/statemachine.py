"""Transactional unit state transitions (design §3/§4): the only writer of unit
``state`` columns.

``StateMachine.transition`` is the single code path through which any phase or
stage changes state (DoD §6): validate against the §3.1/§3.2 transition tables
(``models.VALID_*_TRANSITIONS``), update the unit row, append the ``events``
row, and run the caller's coupled writes — all inside one
``Database.transaction()`` block, so a failure anywhere rolls back everything
(no half-recorded transitions). Anything outside the tables raises
``TransitionError``: an illegal transition attempt is a control-plane bug
(design §6), reported explicitly, never guessed around (Doctrine §7).

May import: models, db (+ stdlib) — design §1.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping

from sf_factory.db import Database, get_phase, get_stage, insert_event, set_unit_state
from sf_factory.models import (
    VALID_PHASE_TRANSITIONS,
    VALID_STAGE_TRANSITIONS,
    Level,
    PhaseState,
    StageState,
    TransitionError,
)

#: Concrete unit state of either level (the two state enums share no values
#: beyond the common lifecycle names; ``Level`` selects the table).
type UnitState = StageState | PhaseState


class StateMachine:
    """Sole authority over unit state columns (design §1/§4).

    Every other module changes unit state only by calling ``transition``;
    ``db.set_unit_state`` is never called from anywhere else.
    """

    def __init__(self, db: Database) -> None:
        """Sole authority over unit state columns."""
        self._db = db

    def transition(
        self,
        level: Level,
        unit_id: str,
        to_state: str,
        *,
        actor: str,
        reason: str,
        payload: dict | None = None,
        coupled: Callable[[sqlite3.Connection], None] | None = None,
    ) -> int:
        """Atomically (one tx, DoD §6): validate against VALID_*_TRANSITIONS, set state,
        append event, run coupled writes (e.g. fix-iteration insert). Returns event seq;
        raises TransitionError.

        Mechanics:
        - ``level``/``to_state`` are coerced to their enums; unknown values raise
          ``TransitionError`` (a garbage level or state string is a caller bug,
          never silently categorized).
        - The current state is read inside the transaction; a missing unit row
          raises ``TransitionError``.
        - The ``events`` row has ``event_type='transition'``, the from/to states,
          and ``payload`` merged with ``{"reason": reason}`` — the explicit
          ``reason`` argument wins over any ``"reason"`` key in ``payload``.
        - ``coupled`` runs last, on the same connection, inside the same
          transaction: if it raises, the state update and the event roll back
          with it (§8 atomicity requirement). The block is synchronous
          end-to-end — ``coupled`` must not await (§7 invariant 1).
        """
        level = _coerce_level(level)
        target = _coerce_state(level, to_state)
        event_payload = dict(payload) if payload else {}
        event_payload["reason"] = reason
        with self._db.transaction() as conn:
            current = _current_state(conn, level, unit_id)
            allowed = _transition_table(level)[current]
            if target not in allowed:
                allowed_repr = sorted(s.value for s in allowed) or "none (terminal state)"
                raise TransitionError(
                    f"illegal {level.value} transition for unit {unit_id!r}: "
                    f"{current.value} -> {target.value} (allowed: {allowed_repr})"
                )
            set_unit_state(conn, level, unit_id, target.value)
            seq = insert_event(
                conn,
                unit_level=level.value,
                unit_id=unit_id,
                event_type="transition",
                actor=actor,
                from_state=current.value,
                to_state=target.value,
                payload=event_payload,
            )
            if coupled is not None:
                coupled(conn)
        return seq


# --------------------------------------------------------------- private helpers


def _coerce_level(level: Level) -> Level:
    try:
        return Level(level)
    except ValueError as exc:
        raise TransitionError(f"unknown unit level: {level!r}") from exc


def _coerce_state(level: Level, state: str) -> UnitState:
    try:
        if level is Level.STAGE:
            return StageState(state)
        return PhaseState(state)
    except ValueError as exc:
        raise TransitionError(f"unknown {level.value} state: {state!r}") from exc


def _transition_table(level: Level) -> Mapping[UnitState, frozenset[UnitState]]:
    if level is Level.STAGE:
        return VALID_STAGE_TRANSITIONS
    return VALID_PHASE_TRANSITIONS


def _current_state(conn: sqlite3.Connection, level: Level, unit_id: str) -> UnitState:
    unit = get_stage(conn, unit_id) if level is Level.STAGE else get_phase(conn, unit_id)
    if unit is None:
        raise TransitionError(f"unknown {level.value} unit: {unit_id!r}")
    return unit.state
