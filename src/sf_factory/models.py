"""Domain vocabulary for the SF-F5 control plane (design §1, §3, §4).

Enums, frozen dataclasses, the transition tables, the error taxonomy and the
``utc_now``/``new_id`` helpers. Zero I/O; imports stdlib only. Every other
module builds on this vocabulary — nothing here may grow side effects.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

# --------------------------------------------------------------------------- enums


class Level(StrEnum):
    """Unit level: PHASE='phase', STAGE='stage'."""

    PHASE = "phase"
    STAGE = "stage"


class RiskClass(StrEnum):
    """ROUTINE, STRUCTURAL, CRITICAL (config risk_classes keys).

    Vocabulary for the standard classes only: the authoritative set is
    config-defined and validated at insert against FactoryConfig (design §2),
    which is why ``Stage.risk_class`` is a plain ``str``.
    """

    ROUTINE = "routine"
    STRUCTURAL = "structural"
    CRITICAL = "critical"


class StageState(StrEnum):
    """PENDING SPEC BUILD VALIDATE AUDIT AWAITING_HUMAN MERGE_GATE ESCALATED DONE FAILED
    CANCELLED."""

    PENDING = "PENDING"
    SPEC = "SPEC"
    BUILD = "BUILD"
    VALIDATE = "VALIDATE"
    AUDIT = "AUDIT"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    MERGE_GATE = "MERGE_GATE"
    ESCALATED = "ESCALATED"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PhaseState(StrEnum):
    """PENDING PLANNING CONTRACTS_FROZEN RUNNING INTEGRATING AWAITING_SIGNOFF AWAITING_HUMAN
    ESCALATED DONE FAILED CANCELLED."""

    PENDING = "PENDING"
    PLANNING = "PLANNING"
    CONTRACTS_FROZEN = "CONTRACTS_FROZEN"
    RUNNING = "RUNNING"
    INTEGRATING = "INTEGRATING"
    AWAITING_SIGNOFF = "AWAITING_SIGNOFF"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    ESCALATED = "ESCALATED"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SchedCategory(StrEnum):
    """WAITING RUNNABLE RUNNING BLOCKED TERMINAL_OK TERMINAL_FAIL."""

    WAITING = "WAITING"
    RUNNABLE = "RUNNABLE"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    TERMINAL_OK = "TERMINAL_OK"
    TERMINAL_FAIL = "TERMINAL_FAIL"


class Trigger(StrEnum):
    """MAX_FIX_ITERATIONS CHURN_THRESHOLD CONTRACT_CHANGE_REQUEST AGENT_DECLARED_FAILURE
    CONTEXT_BUDGET.

    Values are the lowercase strings stored in ``escalations.trigger`` and used
    literally by the §2 trigger SQL.
    """

    MAX_FIX_ITERATIONS = "max_fix_iterations"
    CHURN_THRESHOLD = "churn_threshold"
    CONTRACT_CHANGE_REQUEST = "contract_change_request"
    AGENT_DECLARED_FAILURE = "agent_declared_failure"
    CONTEXT_BUDGET = "context_budget"


# ------------------------------------------------------------- transition tables

#: §3.1 stage flow. Terminal states map to an empty frozenset.
VALID_STAGE_TRANSITIONS: Mapping[StageState, frozenset[StageState]] = MappingProxyType(
    {
        StageState.PENDING: frozenset({StageState.SPEC, StageState.CANCELLED}),
        StageState.SPEC: frozenset(
            {StageState.BUILD, StageState.ESCALATED, StageState.CANCELLED}
        ),
        StageState.BUILD: frozenset(
            {StageState.VALIDATE, StageState.ESCALATED, StageState.CANCELLED}
        ),
        StageState.VALIDATE: frozenset(
            {
                StageState.MERGE_GATE,
                StageState.AUDIT,
                StageState.BUILD,
                StageState.SPEC,
                StageState.ESCALATED,
                StageState.CANCELLED,
            }
        ),
        StageState.AUDIT: frozenset(
            {
                StageState.MERGE_GATE,
                StageState.AWAITING_HUMAN,
                StageState.BUILD,
                StageState.ESCALATED,
                StageState.CANCELLED,
            }
        ),
        StageState.AWAITING_HUMAN: frozenset(
            {
                StageState.MERGE_GATE,
                StageState.BUILD,
                StageState.SPEC,
                StageState.ESCALATED,
                StageState.CANCELLED,
            }
        ),
        StageState.MERGE_GATE: frozenset(
            {
                StageState.DONE,
                StageState.BUILD,
                StageState.ESCALATED,
                StageState.CANCELLED,
            }
        ),
        StageState.ESCALATED: frozenset(
            {
                StageState.SPEC,
                StageState.BUILD,
                StageState.VALIDATE,
                StageState.AWAITING_HUMAN,
                StageState.FAILED,
                StageState.CANCELLED,
            }
        ),
        StageState.DONE: frozenset(),
        StageState.FAILED: frozenset(),
        StageState.CANCELLED: frozenset(),
    }
)

#: §3.2 phase flow. Terminal states map to an empty frozenset.
VALID_PHASE_TRANSITIONS: Mapping[PhaseState, frozenset[PhaseState]] = MappingProxyType(
    {
        PhaseState.PENDING: frozenset({PhaseState.PLANNING, PhaseState.CANCELLED}),
        PhaseState.PLANNING: frozenset(
            {PhaseState.CONTRACTS_FROZEN, PhaseState.ESCALATED, PhaseState.CANCELLED}
        ),
        PhaseState.CONTRACTS_FROZEN: frozenset(
            {PhaseState.RUNNING, PhaseState.CANCELLED}
        ),
        PhaseState.RUNNING: frozenset(
            {
                PhaseState.INTEGRATING,
                PhaseState.ESCALATED,
                PhaseState.AWAITING_HUMAN,
                PhaseState.CANCELLED,
            }
        ),
        PhaseState.INTEGRATING: frozenset(
            {
                PhaseState.AWAITING_SIGNOFF,
                PhaseState.RUNNING,
                PhaseState.ESCALATED,
                PhaseState.CANCELLED,
            }
        ),
        PhaseState.AWAITING_SIGNOFF: frozenset(
            {PhaseState.DONE, PhaseState.RUNNING, PhaseState.CANCELLED}
        ),
        PhaseState.AWAITING_HUMAN: frozenset(
            {PhaseState.RUNNING, PhaseState.PLANNING, PhaseState.CANCELLED}
        ),
        PhaseState.ESCALATED: frozenset(
            {
                PhaseState.PLANNING,
                PhaseState.RUNNING,
                PhaseState.AWAITING_HUMAN,
                PhaseState.FAILED,
                PhaseState.CANCELLED,
            }
        ),
        PhaseState.DONE: frozenset(),
        PhaseState.FAILED: frozenset(),
        PhaseState.CANCELLED: frozenset(),
    }
)

_RUNNING_STAGE_STATES = frozenset(
    {
        StageState.SPEC,
        StageState.BUILD,
        StageState.VALIDATE,
        StageState.AUDIT,
        StageState.MERGE_GATE,
    }
)
_RUNNING_PHASE_STATES = frozenset(
    {
        PhaseState.PLANNING,
        PhaseState.CONTRACTS_FROZEN,
        PhaseState.RUNNING,
        PhaseState.INTEGRATING,
    }
)
_BLOCKED_STATES = frozenset({"AWAITING_HUMAN", "AWAITING_SIGNOFF", "ESCALATED"})


def sched_category(level: Level, state: str, deps_done: bool) -> SchedCategory:
    """Map a concrete unit state to its level-agnostic scheduling category (§3.3).

    Raises TransitionError on a state string that is not a valid state of
    ``level`` — feeding the scheduler an unknown state is a control-plane bug,
    never silently categorized.
    """
    level = Level(level)
    try:
        if level is Level.STAGE:
            concrete: StageState | PhaseState = StageState(state)
        else:
            concrete = PhaseState(state)
    except ValueError as exc:
        raise TransitionError(f"unknown {level.value} state: {state!r}") from exc

    if concrete.value == "PENDING":
        return SchedCategory.RUNNABLE if deps_done else SchedCategory.WAITING
    if concrete.value == "DONE":
        return SchedCategory.TERMINAL_OK
    if concrete.value in ("FAILED", "CANCELLED"):
        return SchedCategory.TERMINAL_FAIL
    if concrete.value in _BLOCKED_STATES:
        return SchedCategory.BLOCKED
    # Remaining states are the in-flight set per level (§3.3).
    assert concrete in (_RUNNING_STAGE_STATES | _RUNNING_PHASE_STATES)
    return SchedCategory.RUNNING


#: DoD §9 gate-answer vocabulary by ``(unit_level, gate_kind)`` — the ONE source
#: (Doctrine §9) consumed by BOTH the scheduler executors and the dashboard
#: option buttons (CCR-3 / D-0017, moved out of scheduler.py privates). A
#: ``(level, gate_kind)`` with no entry (the DDL enumerates ``business``, which
#: no executor consumes yet) renders without buttons and answers only via
#: ``cli decide``. Deliberate behavioral edit ratified with the move: the
#: signoff executor's ``changes_requested`` alias is DROPPED — ``changes`` is
#: the only accepted token (D-0017 rider item 4).
GATE_ANSWERS: Mapping[tuple[str, str], tuple[str, ...]] = MappingProxyType(
    {
        ("stage", "critical_stage"): ("approved", "rework:BUILD", "rework:SPEC"),
        ("stage", "escalation_tradeoff"): ("approved", "rework:BUILD", "rework:SPEC"),
        ("phase", "phase_signoff"): ("approved", "changes"),
        ("phase", "escalation_tradeoff"): ("resume", "replan"),
    }
)

#: ESCALATED-exit resolution vocabulary (§2 ``escalations.resolution``) — the
#: ONE source (Doctrine §9) consumed by BOTH the scheduler's ``_step_escalated``
#: routing and ``cli resolve-escalation`` validation (CCR-7 / D-0027, moved out
#: of scheduler.py privates — the GATE_ANSWERS precedent). Resolution string ->
#: target state per level; anything else = unknown -> explicit 'alert' event,
#: the unit stays put (never guessed, Doctrine §7). Every value is a legal
#: ESCALATED exit of its level's transition table (pinned by test).
STAGE_ESCALATION_RESOLUTIONS: Mapping[str, StageState] = MappingProxyType(
    {
        "rework:BUILD": StageState.BUILD,
        "rework:SPEC": StageState.SPEC,
        "respec": StageState.SPEC,
        "rework:VALIDATE": StageState.VALIDATE,
        "awaiting_human": StageState.AWAITING_HUMAN,
        "failed": StageState.FAILED,
        "cancelled": StageState.CANCELLED,
    }
)
PHASE_ESCALATION_RESOLUTIONS: Mapping[str, PhaseState] = MappingProxyType(
    {
        "replan": PhaseState.PLANNING,
        "resume": PhaseState.RUNNING,
        "awaiting_human": PhaseState.AWAITING_HUMAN,
        "failed": PhaseState.FAILED,
        "cancelled": PhaseState.CANCELLED,
    }
)


# ------------------------------------------------------------------------ helpers


def utc_now() -> str:
    """ISO 8601 UTC timestamp 'YYYY-MM-DDTHH:MM:SSZ'."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    """'<prefix>-<12 hex chars>' unique id."""
    return f"{prefix}-{secrets.token_hex(6)}"


# -------------------------------------------------------------------- dataclasses


@dataclass(frozen=True, slots=True)
class Phase:
    """id, project, name, state: PhaseState, branch, plan_artifact_id, created_at, updated_at."""

    id: str
    project: str
    name: str
    state: PhaseState
    branch: str | None
    plan_artifact_id: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Stage:
    """id, phase_id, name, risk_class, state: StageState, branch, worktree_path,
    spec_artifact_id, created_at, updated_at."""

    id: str
    phase_id: str
    name: str
    risk_class: str
    state: StageState
    branch: str | None
    worktree_path: str | None
    spec_artifact_id: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Event:
    """seq, unit_level, unit_id, event_type, from_state, to_state, actor, payload: dict,
    created_at."""

    seq: int
    unit_level: str
    unit_id: str | None
    event_type: str
    from_state: str | None
    to_state: str | None
    actor: str
    payload: dict
    created_at: str


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """id, unit_level, unit_id, kind, repo, path, sha256, git_commit, created_at."""

    id: int | None
    unit_level: str
    unit_id: str
    kind: str
    repo: str
    path: str
    sha256: str
    git_commit: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    """id, unit_level, unit_id, kind, role, cp_id, session_id, pid, cmdline, cwd, state,
    exit_code, ndjson_log_path, spawned_at, heartbeat_at, ended_at."""

    id: int | None
    unit_level: str | None
    unit_id: str | None
    kind: str
    role: str
    cp_id: str | None
    #: CLI session id from the init/result NDJSON line (continue_session resume
    #: support, DoD §3.4 — CCR-1).
    session_id: str | None
    pid: int | None
    cmdline: str
    cwd: str | None
    state: str
    exit_code: int | None
    ndjson_log_path: str | None
    spawned_at: str
    heartbeat_at: str | None
    ended_at: str | None


@dataclass(frozen=True, slots=True)
class Escalation:
    """id, unit_level, unit_id, trigger, target, payload_artifact_id, event_seq, status,
    resolution, created_at, resolved_at."""

    id: int | None
    unit_level: str
    unit_id: str
    trigger: str
    target: str
    payload_artifact_id: int | None
    #: events.seq that fired this escalation — the §2 dedup cursor of the
    #: always-fire sentinel triggers (CCR-1).
    event_seq: int | None
    status: str
    resolution: str | None
    created_at: str
    resolved_at: str | None


@dataclass(frozen=True, slots=True)
class Finding:
    """id, stage_id, auditor_role, finding_ref, severity, report_artifact_id, status,
    contest_artifact_id, resolved_by, created_at, updated_at."""

    id: int | None
    stage_id: str
    auditor_role: str
    finding_ref: str
    severity: str | None
    report_artifact_id: int
    status: str
    contest_artifact_id: int | None
    resolved_by: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """id, unit_level, unit_id, gate_kind, request_artifact_id, status, answer,
    answer_artifact_id, created_at, alerted_at, answered_at."""

    id: int | None
    unit_level: str
    unit_id: str
    gate_kind: str
    request_artifact_id: int
    status: str
    answer: str | None
    answer_artifact_id: int | None
    created_at: str
    alerted_at: str | None
    answered_at: str | None


@dataclass(frozen=True, slots=True)
class TriggerFiring:
    """trigger: Trigger, unit_level, unit_id, evidence: dict (the SQL row(s) that fired)."""

    trigger: Trigger
    unit_level: str
    unit_id: str
    evidence: dict


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    """failing: int, passing: int, total: int — parsed from validation-report.json."""

    failing: int
    passing: int
    total: int


# ---------------------------------------------------------------- error taxonomy
# All subclass FactoryError; semantics and handling per design §6.


class FactoryError(Exception):
    """Base of the factory error taxonomy (design §6)."""


class ConfigError(FactoryError):
    """Invalid factory.config.yaml — abort startup, no factory without valid config."""


class MigrationError(FactoryError):
    """DB schema migration failed — abort startup."""


class TransitionError(FactoryError):
    """Illegal transition attempt = control-plane bug."""


class IntegrityError(FactoryError):
    """Artifact ref unresolved / hash mismatch."""


class GitError(FactoryError):
    """Worktree/commit/merge mechanics failed."""


class ProcessError(FactoryError):
    """Process spawn impossible (CLI missing, etc.)."""


class ArtifactContractError(FactoryError):
    """Agent broke an artifact contract (missing sidecar, malformed plan)."""


class ConsultationBreachError(FactoryError):
    """LLM call outside the consultation registry attempted — caller bug."""


class NotifyError(FactoryError):
    """ntfy unreachable / timed out."""
