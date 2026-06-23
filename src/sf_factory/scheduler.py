"""Level-agnostic DAG scheduler + per-level unit executors (design §3.3/§4/§5.5).

One fan-out/queue/gate code path drives BOTH levels (DoD §3.2): the
``Scheduler`` operates only on ``sched_category`` + ``dag_edges``; everything
level-specific lives in the ``UnitExecutor`` implementations (``StageExecutor``
= the §3.1 SPEC→…→MERGE_GATE conveyor, ``PhaseExecutor`` = the §3.2
plan→freeze→fan-out→integrate flow). Crash recovery (``Scheduler.recover``)
implements §5.5 steps a–d and gates the loop start.

Concurrency (§7): single asyncio loop; every DB write happens on the loop
thread inside a synchronous ``Database.transaction()`` block (never an await
inside); notification I/O only via the async ``NtfyPublisher``.

Caller-side SQL note: a handful of private read helpers (and the §4
``tier1_gate``-mandated ``artifact_refs.git_commit`` re-resolve UPDATE) run
their SQL here rather than in db.py — the design assigns these behaviors to
this module's classes explicitly (the same pattern as thresholds.py's §2
trigger SQL); each is a small, commented, single-statement query.

May import: all of models/config/db/statemachine/runner/artifacts/worktrees/
thresholds/consultation, plus notify (design §1).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import signal
import sqlite3
import sys
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from sf_factory import db as fdb
from sf_factory.artifacts import (
    PHASE_ARTIFACTS,
    STAGE_ARTIFACTS,
    PhasePlan,
    StageSizeLimits,
    detect_sentinels,
    evaluate_stage_sizes,
    read_phase_plan,
    read_validation_sidecar,
    register_artifact,
    sha256_file,
    unit_artifact_dir,
    verify_integrity,
)
from sf_factory.config import ConsultationPointCfg, FactoryConfig, ProjectCfg
from sf_factory.consultation import _canonical_payload

# Scheduler imports dashboard — never the reverse (dashboard design §6, no cycle):
# DashboardServer for the optional CCR-3 wiring, GLOSS for the R2-glossed tokens
# inside the re-authored Romanian decision-request templates, resolve_bind_host
# for the §7 IP-drift re-check.
from sf_factory.dashboard import GLOSS, DashboardServer, resolve_bind_host
from sf_factory.db import Database
from sf_factory.models import (
    ESCALATION_TARGET_LADDER,
    GATE_ANSWERS,
    PHASE_ESCALATION_RESOLUTIONS,
    PHASE_NOACTION_RESOLUTION,
    STAGE_ESCALATION_RESOLUTIONS,
    STAGE_NOACTION_RESOLUTION,
    STAGE_SPEC_DOC_RESOLUTION,
    ArtifactContractError,
    ConfigError,
    DecisionRequest,
    Escalation,
    FactoryError,
    Finding,
    GitError,
    IntegrityError,
    Level,
    NotifyError,
    Phase,
    PhaseState,
    ProcessError,
    ProcessRecord,
    SchedCategory,
    Stage,
    StageState,
    TransitionError,
    Trigger,
    new_id,
    sched_category,
    utc_now,
)
from sf_factory.notify import NtfyPublisher, dashboard_link
from sf_factory.runner import AgentResult, AgentRunner, cmdline_matches
from sf_factory.runtime_settings import EffectiveConfig
from sf_factory.statemachine import StateMachine
from sf_factory.thresholds import ThresholdEvaluator
from sf_factory.worktrees import (
    StaleGateError,
    WorktreeManager,
    _hunk_headers,
    commit_paths,
    run_git,
)

if TYPE_CHECKING:  # type-only: Consultor/Verdict instances are injected, never built here
    from sf_factory.consultation import Consultor, Verdict

# ------------------------------------------------------------------ vocabulary

#: The feedback-triage consultation point (DoD §3.4) — a registry id referenced
#: by name, like config role keys; the registry itself stays in config.
CP1_ID = "CP-1"

#: CLIs with verified session resume (OPEN-3: claude native; the stub echoes
#: --resume). codex stays OUT until its resume is verified against the real
#: CLI: D-0011 smoke-tested `codex exec resume` syntax only, and the §3.1
#: hard gate holds until verified in code — wave-4 A2 integration territory,
#: like the D-0014(2) cmdline check. A route outside this set executes
#: `continue_session` as `rebuild` + `verdict_downgraded` (§3.1).
#: NOTE (2-D routing, 22-06): the new backend builders route to codex, so codex
#: builder resume is now reachable in-flow — but it is intentionally NOT yet added
#: here. codex builders therefore downgrade a CP-1 `continue_session` to `rebuild`
#: (safe) until in-flow codex resume is verified post-re-seed; codex joins this set
#: only then (founder's sequencing — do not pre-empt by adding it now).
RESUME_VERIFIED_CLIS = frozenset({"claude", "stub"})

#: Sentinel artifact kind -> (filename, events.event_type, escalations.trigger).
_SENTINEL_EVENTS: Mapping[str, tuple[str, str]] = {
    "declared_failure": ("declared_failure", Trigger.AGENT_DECLARED_FAILURE.value),
    "contract_change_request": (
        "contract_change_request",
        Trigger.CONTRACT_CHANGE_REQUEST.value,
    ),
}


class _AgentRunFailed(Exception):
    """Incident 7 (D-0035) control-flow signal: ``_run_step_agent`` detected a
    failed agent run (nonzero/None exit or killed, with NO sentinel declared),
    already inserted the 'agent_run_failed' escalation and transitioned the
    stage to ESCALATED — the raising step must unwind WITHOUT consuming any
    artifact (freshness of the RUN, not presence of artifacts, is the
    contract). Caught in ``StageExecutor.execute`` so it never reaches the
    ``Scheduler._drive`` §6 boundary, which would double-escalate it as
    'internal_error'; sibling units keep running either way."""

#: ESCALATED-exit resolution vocabulary: models.STAGE_ESCALATION_RESOLUTIONS /
#: models.PHASE_ESCALATION_RESOLUTIONS (CCR-7 / D-0027 — moved out of the
#: former module privates; one source consumed by _step_escalated AND
#: `cli resolve-escalation`).

#: Target-state ROUTING for the models.GATE_ANSWERS vocabulary (CCR-3/D-0017:
#: GATE_ANSWERS is the one answer-token source consumed by the executors AND
#: the dashboard; these private maps only route an ACCEPTED token to its §3
#: transition target — membership is always checked against GATE_ANSWERS, and a
#: pin test asserts these keys equal it). The pre-CCR-3 `changes_requested`
#: alias is dropped (deliberate behavioral edit, D-0017 rider item 4).
_STAGE_ANSWER_TARGETS: Mapping[str, StageState] = {
    "approved": StageState.MERGE_GATE,
    "rework:BUILD": StageState.BUILD,
    "rework:SPEC": StageState.SPEC,
}
_PHASE_ANSWER_TARGETS: Mapping[str, PhaseState] = {
    "resume": PhaseState.RUNNING,
    "replan": PhaseState.PLANNING,
}

_ACTOR = "control_plane"


def _ro_glossed(token: str) -> str:
    """'<gloss> (<token>)' via the dashboard GLOSS table (R2 one-source);
    unknown token -> visible '<token> (etichetă lipsă)' marker."""
    gloss = GLOSS.get(token)
    if gloss is None:
        return f"{token} (etichetă lipsă)"
    return f"{gloss} ({token})"


#: One-line Romanian consequence per gate-answer token (§2a: every declared
#: option rendered with its consequence in the founder's terms).
_ANSWER_CONSEQUENCES_RO: Mapping[str, str] = {
    "approved": "aprobă — lucrarea merge mai departe spre integrare",
    "rework:BUILD": (
        "refă construcția — implementarea se reface pe aceeași specificație"
    ),
    "rework:SPEC": (
        "refă specificația — specificația se rescrie, apoi implementarea se reface"
    ),
    "changes": "cere modificări — faza se redeschide pentru lucru",
    "resume": "reia — faza continuă din starea curentă",
    "replan": "replanifică — planul fazei se reface, apoi etapele rezultate",
}


def _ro_options_block(level: str, gate_kind: str) -> str:
    """'## Opțiuni' body: each declared GATE_ANSWERS token + its consequence."""
    lines = []
    for token in GATE_ANSWERS.get((level, gate_kind), ()):
        consequence = _ANSWER_CONSEQUENCES_RO.get(token, _ro_glossed(token))
        lines.append(f"- {token} — {consequence}.")
    return "\n".join(lines)


# ------------------------------------------------------- private SQL read helpers


def _max_event_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
    return int(row[0])


def _open_escalation_count(conn: sqlite3.Connection, level: str, unit_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM escalations WHERE unit_level = ? AND unit_id = ?"
        " AND status = 'open'",
        (level, unit_id),
    ).fetchone()
    return int(row[0])


def _total_open_escalations(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM escalations WHERE status = 'open'").fetchone()
    return int(row[0])


def _pending_decision_count(conn: sqlite3.Connection, level: str, unit_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM decision_requests WHERE unit_level = ? AND unit_id = ?"
        " AND status = 'pending'",
        (level, unit_id),
    ).fetchone()
    return int(row[0])


def _latest_decision(
    conn: sqlite3.Connection, level: str, unit_id: str
) -> DecisionRequest | None:
    row = conn.execute(
        "SELECT * FROM decision_requests WHERE unit_level = ? AND unit_id = ?"
        " ORDER BY id DESC LIMIT 1",
        (level, unit_id),
    ).fetchone()
    if row is None:
        return None
    return DecisionRequest(
        id=row["id"],
        unit_level=row["unit_level"],
        unit_id=row["unit_id"],
        gate_kind=row["gate_kind"],
        request_artifact_id=row["request_artifact_id"],
        status=row["status"],
        answer=row["answer"],
        answer_artifact_id=row["answer_artifact_id"],
        created_at=row["created_at"],
        alerted_at=row["alerted_at"],
        answered_at=row["answered_at"],
    )


def _latest_resolved_escalation(
    conn: sqlite3.Connection, level: str, unit_id: str
) -> Escalation | None:
    row = conn.execute(
        "SELECT * FROM escalations WHERE unit_level = ? AND unit_id = ?"
        " AND status = 'resolved' ORDER BY id DESC LIMIT 1",
        (level, unit_id),
    ).fetchone()
    if row is None:
        return None
    return Escalation(
        id=row["id"],
        unit_level=row["unit_level"],
        unit_id=row["unit_id"],
        trigger=row["trigger"],
        target=row["target"],
        payload_artifact_id=row["payload_artifact_id"],
        event_seq=row["event_seq"],
        status=row["status"],
        resolution=row["resolution"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )


def _dag_edge_exists(
    conn: sqlite3.Connection, level: Level, from_id: str, to_id: str
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM dag_edges WHERE level = ? AND from_id = ? AND to_id = ?",
        (Level(level).value, from_id, to_id),
    ).fetchone()
    return row is not None


def _last_event_seq_of_type(
    conn: sqlite3.Connection, unit_id: str, event_type: str, *, role: str | None = None
) -> int:
    """Latest seq of an event type for a stage; with ``role``, only events whose
    payload role matches (the runner writes role into spawn payloads, §5.1)."""
    if role is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE unit_level = 'stage'"
            " AND unit_id = ? AND event_type = ?",
            (unit_id, event_type),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE unit_level = 'stage'"
            " AND unit_id = ? AND event_type = ?"
            " AND json_extract(payload_json, '$.role') = ?",
            (unit_id, event_type, role),
        ).fetchone()
    return int(row[0])


def _last_transition_payload(
    conn: sqlite3.Connection, level: str, unit_id: str, to_state: str
) -> dict:
    """Payload of the unit's most recent transition INTO ``to_state`` — the
    crash-safe carrier of the CP-1 `continue_session` resume id (§3.1)."""
    row = conn.execute(
        "SELECT payload_json FROM events WHERE unit_level = ? AND unit_id = ?"
        " AND event_type = 'transition' AND to_state = ? ORDER BY seq DESC LIMIT 1",
        (level, unit_id, to_state),
    ).fetchone()
    if row is None:
        return {}
    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _note_finding_recurrence(
    conn: sqlite3.Connection,
    stage_id: str,
    findings: Sequence[Mapping[str, object]],
    *,
    auditor: str,
) -> None:
    """D-0059 mechanical recurrence backstop (architect-operations §1): if an audit
    raises a ``finding_ref`` already SETTLED or OVERRULED on this stage, the root
    was not actually fixed. Emit ONE 'finding_recurrence' event (durable —
    dashboard-surfaced + monitor-greppable) listing the recurring refs + their
    prior disposition. Called in the SAME tx as the finding insertion, BEFORE it,
    so the just-raised (status 'open') rows never self-match."""
    recurred = [
        {"ref": str(f["ref"]), "prior_disposition": prior}
        for f in findings
        if (prior := fdb.prior_disposed_finding(conn, stage_id, str(f["ref"]), auditor))
        is not None
    ]
    if recurred:
        fdb.insert_event(
            conn,
            unit_level=Level.STAGE.value,
            unit_id=stage_id,
            event_type="finding_recurrence",
            actor=_ACTOR,
            payload={"auditor": auditor, "recurred": recurred},
        )


def _resolution_reason(
    conn: sqlite3.Connection, level: str, unit_id: str, escalation_id: int
) -> str | None:
    """Operator rationale of the escalation_resolved event for this escalation
    (cli --reason); None when absent — the caller supplies its fallback."""
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE unit_level = ? AND unit_id = ?"
        " AND event_type = 'escalation_resolved' ORDER BY seq DESC",
        (level, unit_id),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("escalation_id") != escalation_id:
            continue
        reason = payload.get("reason")
        if isinstance(reason, str) and reason:
            return reason
    return None


def _unit_artifact_rows(
    conn: sqlite3.Connection, level: str, unit_id: str, repo: str
) -> list[tuple[int, str, str]]:
    rows = conn.execute(
        "SELECT id, path, sha256 FROM artifact_refs WHERE unit_level = ?"
        " AND unit_id = ? AND repo = ? ORDER BY id",
        (level, unit_id, repo),
    ).fetchall()
    return [(int(r["id"]), r["path"], r["sha256"]) for r in rows]


def _reresolve_artifact_commits(
    conn: sqlite3.Connection, level: str, unit_id: str, worktree: Path, new_head: str
) -> int:
    """§4 tier1_gate contract, assigned to the CALLER: after a successful rebase
    re-resolve the unit's artifact_refs.git_commit at the new branch head —
    mechanical (same path + same sha256, verified against the rebased checkout);
    rows whose content no longer matches are left untouched. Returns updates."""
    updated = 0
    for ref_id, rel_path, sha in _unit_artifact_rows(conn, level, unit_id, "workspace"):
        candidate = worktree / rel_path
        if not candidate.is_file():
            continue
        try:
            if sha256_file(candidate) != sha:
                continue
        except IntegrityError:
            continue
        conn.execute(
            "UPDATE artifact_refs SET git_commit = ? WHERE id = ?", (new_head, ref_id)
        )
        updated += 1
    return updated


# ----------------------------------------------------------------- misc helpers


async def _dispose_worktree(
    db: Database,
    wt: WorktreeManager,
    repo_root: Path,
    worktree: Path,
    *,
    unit_level: str,
    unit_id: str,
) -> None:
    """Best-effort worktree disposal: failure is an 'alert' event, never an
    exception. Used (a) for scratch worktrees on EVERY exit of a gate step —
    a leaked scratch gets reused by WorktreeManager.create, which re-syncs
    TRACKED content only, so an untracked stale sentinel (or a previous
    validator's derived tests) would survive into every later run and re-fire
    the §5.4 always-fire triggers forever — and (b) for post-DONE unit
    checkouts, where cleanup must never un-done a merged unit."""
    try:
        await wt.remove(repo_root, worktree)
    except GitError as exc:
        with db.transaction() as conn:
            fdb.insert_event(
                conn,
                unit_level=unit_level,
                unit_id=unit_id,
                event_type="alert",
                actor=_ACTOR,
                payload={"kind": "worktree_remove_failed", "error": str(exc)},
            )


def _resolve(home: Path, path: Path) -> Path:
    """Anchor a relative config path at factory.home (same rule as watchdog/runner)."""
    return path if path.is_absolute() else home / path


async def _find_branch_checkout(repo_root: Path, branch: str) -> Path | None:
    """The working tree (main checkout or linked worktree) where ``branch`` is
    checked out, or None. ``worktrees.integrate`` merges INSIDE the target-branch
    checkout (`_current_branch(root) == target_branch` is its precondition), and
    a stage's target is the PHASE branch — checked out at the phase worktree,
    never at the workspace root (which stays on the project integration branch).
    Read-only `git worktree list --porcelain` scan; mechanics only."""
    code, out, err = await run_git("worktree", "list", "--porcelain", cwd=repo_root)
    if code != 0:
        raise GitError(
            f"git worktree list failed in {repo_root}: {(err or out).strip()}"
        )
    expected = f"refs/heads/{branch}"
    current_path: str | None = None
    for raw_line in out.splitlines():
        line = raw_line.strip()
        if line.startswith("worktree "):
            current_path = line.removeprefix("worktree ")
        elif line.startswith("branch ") and current_path is not None:
            if line.removeprefix("branch ") == expected:
                return Path(current_path)
    return None


def _bounded(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    return data[:max_bytes].decode("utf-8", errors="replace") + "\n[truncated]"


def _render_sibling_diffs(
    sibling_diffs: Mapping[str, str],
    fixed_bytes: int,
    max_total_bytes: int,
    empty_text: str,
) -> tuple[list[str], bool]:
    """Render the merged-sibling diff block for a Tier-2 integration prompt,
    bounding the integration_validator's print-mode context (D-0046: at the
    Nth-merging unit the FULL sibling bodies dominate the prompt — measured
    ~2.06MB of ~2.36MB at posting-engine — and overflow the agent's 1M window).

    Full diff bodies when the WHOLE assembled prompt fits ``max_total_bytes``
    (``fixed_bytes`` = the gating unit's full diff + contracts + plan +
    boilerplate, already counted by the caller, PLUS the sibling bodies);
    otherwise each sibling collapses to file + ``@@`` hunk headers
    (``_hunk_headers``) — changed-region visibility kept, bulk dropped. The
    gating unit's diff + the contracts ALWAYS stay verbatim. Returns
    ``(lines_to_append, used_headers)``; ``used_headers`` lets the caller warn
    the validator it is seeing regions, not full bodies."""
    if not sibling_diffs:
        return ([empty_text], False)
    ordered = sorted(sibling_diffs.items())
    full = [f"--- merged unit {uid} ---\n{diff}" for uid, diff in ordered]
    if fixed_bytes + sum(len(s.encode("utf-8")) for s in full) <= max_total_bytes:
        return (full, False)
    headers = [
        f"--- merged unit {uid} (diff bodies elided to fit the integration "
        f"context budget — file + @@ hunk headers only) ---\n{_hunk_headers(diff)}"
        for uid, diff in ordered
    ]
    return (headers, True)


#: Appended to the sibling-diff section header when bodies were elided to headers,
#: so the validator judges from regions + the gating unit's full diff + contracts
#: and does NOT flag the elision itself as an integration finding.
_SIBLING_ELISION_NOTE = (
    "(NOTE: some sibling diff bodies exceeded the integration context budget and "
    "are shown as file + @@ hunk headers only — judge cross-unit integration from "
    "these changed regions together with the gating unit's full diff and the "
    "contracts in force; do not raise the elision itself as a finding.)"
)


def _fit_consultation_inputs(
    inputs: Mapping[str, str], max_input_bytes: int
) -> dict[str, str]:
    """Bound the ASSEMBLED consultation inputs to the registry's
    ``max_input_bytes`` exactly as the Consultor measures it
    (``consultation._canonical_payload``: key-sorted compact JSON, UTF-8):
    truncate the largest input until the canonical payload fits. A
    legitimately oversized input (a huge validation report, say) then consults
    bounded instead of landing a ``cp_breach_attempt`` governance event in the
    DoD §13 creep scan; the Consultor-side breach check stays as the backstop
    for actual caller bugs (§6) — this caller just never trips it on size.

    Termination: each pass removes at least ``excess + 32`` raw bytes from the
    largest value, and removed raw bytes shrink the canonical payload by at
    least their own count, while the truncation marker adds < 32 — so the
    canonical size strictly decreases. ConfigError when even empty inputs
    cannot fit (the JSON envelope alone exceeds the registry bound)."""
    fitted = dict(inputs)
    while (excess := len(_canonical_payload(fitted)) - max_input_bytes) > 0:
        key = max(fitted, key=lambda k: len(fitted[k].encode("utf-8")))
        size = len(fitted[key].encode("utf-8"))
        if size == 0:
            raise ConfigError(
                f"consultation max_input_bytes={max_input_bytes} is smaller than"
                f" the canonical JSON envelope of its empty inputs {sorted(fitted)}"
                " — registry bound unusable"
            )
        keep = size - excess - 32
        fitted[key] = _bounded(fitted[key], keep) if keep > 0 else ""
    return fitted


def _read_text(path: Path, *, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ArtifactContractError(f"cannot read {what} at {path}: {exc}") from exc


def _isolation_ignored(path: str, ignore_globs: Sequence[str]) -> bool:
    """True when a ``git status --porcelain`` path is a build/test dropping per
    process.isolation_ignore_globs (e.g. ``__pycache__/`` left by the factory's
    own Tier-1 suite run). fnmatch-style: a glob matches the exact porcelain
    path (directories keep their trailing ``/``) or any single path segment —
    so ``tests/__pycache__/`` matches the ``__pycache__/`` glob."""
    segments = [seg for seg in path.split("/") if seg]
    for glob in ignore_globs:
        stem = glob.rstrip("/")
        if fnmatch(path, glob) or any(fnmatch(seg, stem) for seg in segments):
            return True
    return False


def _builder_role(cfg: FactoryConfig, risk_class: str, kind: str | None = None) -> str:
    """Builder route by stage KIND × RISK (convention over config — risk_classes
    declares validator+audits only). Returns the FIRST models key that exists,
    in this order:

      1. ``builder_<kind>_<tier>`` where ``tier`` collapses the real risk_class onto
         the two declared builder tiers (``routine`` → routine, ``structural`` /
         ``critical`` → heavy), e.g. ``builder_backend_heavy`` → codex for a
         structural/critical backend stage, ``builder_frontend_routine`` → opus.
      2. ``builder_<kind>``              (kind given — kind-wide fallback)
      3. ``builder_<risk_class>``        (legacy 1-D route; the ONLY path taken
         when kind is None → byte-identical to the pre-2-D behavior)
      4. ``builder_heavy``              (conservative final fallback)

    The tier collapse is load-bearing: config declares only ``builder_<kind>_routine``
    and ``builder_<kind>_heavy``, but the risk_classes are routine/structural/critical.
    Using the LITERAL risk_class in step 1 made ``builder_backend_structural`` /
    ``_critical`` never exist, so every non-routine backend stage silently fell through
    to ``builder_heavy`` (opus), defeating the founder-approved "backend → codex"
    routing (Step-2). Collapsing to the tier restores it: codex builds backend at every
    risk, opus builds frontend at every risk, while AUDIT stays dual-family.

    Backward-compat guarantee: with ``kind=None`` only steps 3–4 run, so kind=None
    legacy stages keep resolving exactly as before. Raises ConfigError when none of
    the candidates is configured."""
    # routine -> the 'routine' (light) builder tier; structural/critical -> 'heavy'.
    tier = "routine" if risk_class == "routine" else "heavy"
    candidates: list[str] = []
    if kind:
        candidates.append(f"builder_{kind}_{tier}")
        candidates.append(f"builder_{kind}")
    candidates.append(f"builder_{risk_class}")
    candidates.append("builder_heavy")
    for candidate in candidates:
        if candidate in cfg.models:
            return candidate
    raise ConfigError(
        f"no builder route for risk class {risk_class!r} (kind {kind!r}): none of"
        f" {candidates} is configured under models.*"
    )


def _cp_point(cfg: FactoryConfig, cp_id: str) -> ConsultationPointCfg:
    for cp in cfg.consultation_points:
        if cp.id == cp_id:
            return cp
    raise ConfigError(f"consultation point {cp_id!r} is not registered in config")


def _project_for_phase(cfg: FactoryConfig, phase: Phase) -> ProjectCfg:
    project = cfg.projects.get(phase.project)
    if project is None:
        raise ConfigError(
            f"phase {phase.id!r} references unknown project {phase.project!r}"
        )
    return project


def _test_cmd(project: ProjectCfg) -> list[str]:
    """projects.<id>.test_command as argv; None = OPEN-2 still open — explicit
    failure, never a silently skipped merge-gate suite (Doctrine §7)."""
    cmd = project.test_command
    if cmd is None:
        raise ConfigError(
            "projects.*.test_command is unset (OPEN-2): the Tier-1 merge gate"
            " cannot run the full test suite — set the canonical suite command"
        )
    if isinstance(cmd, str):
        argv = shlex.split(cmd)
    else:
        argv = [str(part) for part in cmd]
    if not argv:
        raise ConfigError("projects.*.test_command is empty")
    return argv


def _read_findings_sidecar(path: Path, *, auditor_role: str) -> list[dict]:
    """Strict findings sidecar contract (mirrors the OPEN-5 validation sidecar):
    a JSON object {"findings": [{"ref": str, "severity": str?, "summary": str?,
    "location": str?}, ...]}; an empty list = clean report. Missing/malformed →
    ArtifactContractError — agent reports are never parsed best-effort."""
    text = _read_text(path, what=f"findings sidecar of {auditor_role}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ArtifactContractError(
            f"findings sidecar {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict) or set(data) != {"findings"}:
        raise ArtifactContractError(
            f"findings sidecar {path} must be exactly {{'findings': [...]}}"
        )
    findings = data["findings"]
    if not isinstance(findings, list):
        raise ArtifactContractError(f"findings sidecar {path}: 'findings' must be a list")
    allowed = {"ref", "severity", "summary", "location"}
    out: list[dict] = []
    seen_refs: set[str] = set()
    for item in findings:
        if not isinstance(item, dict) or not isinstance(item.get("ref"), str):
            raise ArtifactContractError(
                f"findings sidecar {path}: each finding needs a string 'ref', got {item!r}"
            )
        unknown = set(item) - allowed
        if unknown:
            raise ArtifactContractError(
                f"findings sidecar {path}: unknown finding keys {sorted(unknown)}"
            )
        if item["ref"] in seen_refs:
            raise ArtifactContractError(
                f"findings sidecar {path}: duplicate finding ref {item['ref']!r}"
            )
        seen_refs.add(item["ref"])
        out.append(item)
    return out


def _read_response_sidecar(path: Path, open_refs: Sequence[str]) -> dict[str, dict]:
    """Strict executor-response contract: {"responses": [{"ref", "action":
    comply|contest|duplicate, "rationale"}]}; every open finding ref must be
    addressed exactly once (no silent skips, Doctrine §7)."""
    text = _read_text(path, what="audit response sidecar")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ArtifactContractError(
            f"audit response {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict) or set(data) != {"responses"}:
        raise ArtifactContractError(
            f"audit response {path} must be exactly {{'responses': [...]}}"
        )
    responses: dict[str, dict] = {}
    if not isinstance(data["responses"], list):
        raise ArtifactContractError(f"audit response {path}: 'responses' must be a list")
    for item in data["responses"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("ref"), str)
            or item.get("action") not in ("comply", "contest", "duplicate")
            or not isinstance(item.get("rationale"), str)
        ):
            raise ArtifactContractError(
                f"audit response {path}: each response needs ref/action"
                f"(comply|contest|duplicate)/rationale, got {item!r}"
            )
        if item["ref"] in responses:
            raise ArtifactContractError(
                f"audit response {path}: duplicate response for ref {item['ref']!r}"
            )
        responses[item["ref"]] = item
    missing = [ref for ref in open_refs if ref not in responses]
    if missing:
        raise ArtifactContractError(
            f"audit response {path} does not address finding(s) {missing}"
        )
    return responses


def proving_held_phase_ids(
    cfg: FactoryConfig, phases: Sequence[Phase]
) -> frozenset[str]:
    """Phase-seeding design §5b proving-ground dispatch hold — the pure predicate
    behind the scheduler's RUNNABLE selection AND the `cli status`
    'held: proving' marker (one source, no drifting copies).

    A PENDING phase whose id is NOT in its project's ``proving_phases`` is held
    (not dispatched, state untouched) while ANY phase row of that project whose
    id IS listed there is non-DONE. Once every proving phase is DONE the hold
    dissolves and the DAG governs alone. Empty/absent list = no hold. Only
    EXISTING rows gate: a listed-but-unseeded proving id holds nothing —
    synthetic/b8 projects and pre-seed states must never wedge behind a config
    string that has no unit behind it.
    """
    held: set[str] = set()
    gating_by_project: dict[str, bool] = {}
    for phase in phases:
        if phase.state is not PhaseState.PENDING:
            continue  # the hold is a dispatch filter; only PENDING units dispatch
        project = cfg.projects.get(phase.project)
        if project is None or not project.proving_phases:
            continue
        proving = project.proving_phases
        if phase.id in proving:
            continue  # proving phases themselves are never held
        if phase.project not in gating_by_project:
            gating_by_project[phase.project] = any(
                other.project == phase.project
                and other.id in proving
                and other.state is not PhaseState.DONE
                for other in phases
            )
        if gating_by_project[phase.project]:
            held.add(phase.id)
    return frozenset(held)


class _OutOfBoundsDetector:
    """Phase-seeding design §5 out-of-bounds detector (mechanical, Doctrine §20):
    `git status --porcelain` on (a) factory.home and (b) every project workspace
    integration checkout, filtered through process.isolation_ignore_globs
    (precedent: D-0022/c50bf37) — unexpected dirt = an agent (or anything else)
    wrote outside its worktree. The §10 falsifiability trigger made observable:
    detected by the machine at the next gate/recover, never a silent pass.

    Run at every stage MERGE_GATE entry and during Scheduler.recover(). Alerts
    are deduplicated per streak per repo (the _stall_published /
    _delivery_failed_logged pattern): one 'alert' event + one ntfy per
    consecutive-dirty streak; a clean observation clears the latch. Configured
    worktrees_dirs under a scanned root are control-plane-managed state
    (worktree add / §5.5b canonicalization territory), not agent dirt. A root
    that is missing or not a git repo has no git state to monitor (pre-bootstrap
    workspace) and is skipped. Residual risk accepted explicitly in the design:
    foreign writes to the SQLite DB / gitignored operational files stay
    undetected until a first incident justifies a tripwire (Doctrine §8).
    """

    def __init__(self, db: Database, cfg: FactoryConfig, notify: NtfyPublisher) -> None:
        self._db = db
        self._cfg = cfg
        self._notify = notify
        #: One 'alert' event per consecutive-dirty streak per repo label.
        self._event_logged: set[str] = set()
        #: One successful ntfy publish per streak (retried until delivered).
        self._published: set[str] = set()
        #: One alert_delivery_failed event per delivery-failure streak.
        self._delivery_failed_logged: set[str] = set()

    def _roots(self) -> list[tuple[str, Path]]:
        home = self._cfg.factory.home
        roots: list[tuple[str, Path]] = [("factory", Path(home))]
        for name, project in sorted(self._cfg.projects.items()):
            roots.append((f"workspace:{name}", _resolve(home, project.workspace)))
        return roots

    def _sanctioned_subtrees(self, root: Path) -> list[str]:
        """Root-relative POSIX prefixes of configured worktrees_dirs under this
        root — factory-managed checkouts, never out-of-bounds dirt."""
        prefixes: list[str] = []
        for project in self._cfg.projects.values():
            wt_dir = _resolve(self._cfg.factory.home, project.worktrees_dir)
            try:
                rel = wt_dir.relative_to(root).as_posix()
            except ValueError:
                continue
            if rel not in ("", "."):
                prefixes.append(rel)
        return prefixes

    def _unexpected_paths(self, status_out: str, root: Path) -> list[str]:
        ignore_globs = self._cfg.process.isolation_ignore_globs
        sanctioned = self._sanctioned_subtrees(root)
        paths: list[str] = []
        for line in status_out.splitlines():
            if not line.strip():
                continue
            path = line[3:]
            if _isolation_ignored(path, ignore_globs):
                continue
            norm = path.rstrip("/")
            if any(norm == s or norm.startswith(s + "/") for s in sanctioned):
                continue
            paths.append(path)
        return paths

    async def check(self, *, where: str) -> None:
        """Scan all roots; alert on unexpected dirt (streak-deduplicated)."""
        for repo, root in self._roots():
            if not root.is_dir():
                continue
            code, out, _err = await run_git("status", "--porcelain", cwd=root)
            if code != 0:
                continue  # not a git repo yet (pre-bootstrap) — nothing to monitor
            paths = self._unexpected_paths(out, root)
            if not paths:
                # Clean observation ends the streak: future dirt re-alerts.
                self._event_logged.discard(repo)
                self._published.discard(repo)
                self._delivery_failed_logged.discard(repo)
                continue
            if repo not in self._event_logged:
                self._event_logged.add(repo)
                with self._db.transaction() as conn:
                    fdb.insert_event(
                        conn,
                        unit_level="factory",
                        unit_id=None,
                        event_type="alert",
                        actor=_ACTOR,
                        payload={
                            "kind": "out_of_bounds",
                            "repo": repo,
                            "paths": paths[:100],
                            "where": where,
                        },
                    )
            if repo not in self._published:
                try:
                    await self._notify.publish(
                        f"Scriere în afara limitelor detectată în {repo}",
                        link=dashboard_link(self._cfg, "acum"),
                        priority=self._notify.priority_alert,
                    )
                    self._published.add(repo)
                    self._delivery_failed_logged.discard(repo)
                except NotifyError as exc:
                    if repo not in self._delivery_failed_logged:
                        self._delivery_failed_logged.add(repo)
                        with self._db.transaction() as conn:
                            fdb.insert_event(
                                conn,
                                unit_level="factory",
                                unit_id=None,
                                event_type="alert_delivery_failed",
                                actor=_ACTOR,
                                payload={
                                    "kind": "out_of_bounds",
                                    "repo": repo,
                                    "error": str(exc),
                                },
                            )


#: How much of the agent's stderr file tail the usage-limit detector scans —
#: the CLIs print capacity errors last; a full read of a huge stderr is waste.
_USAGE_LIMIT_STDERR_TAIL_BYTES = 2048


def _usage_limit_stderr_tail(stderr_path: str) -> str:
    """Last ~2KB of the agent's stderr file; missing/unreadable = '' — a tail
    read must never fail a step (the file may not exist for fakes)."""
    try:
        with open(stderr_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(size - _USAGE_LIMIT_STDERR_TAIL_BYTES, 0))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _usage_limit_match(cfg: FactoryConfig, result: AgentResult) -> str | None:
    """First configured signature found in result_text + the stderr tail
    (config validates signatures lowercase; lowercasing the haystack makes the
    match case-insensitive). One source for BOTH the CCR-6 detector and the
    CCR-11 capacity-probe success check (Doctrine §9: no drifting copies)."""
    haystack = (
        result.result_text + "\n" + _usage_limit_stderr_tail(result.stderr_path)
    ).lower()
    for signature in cfg.founder_channel.usage_limit_signatures:
        if signature in haystack:
            return signature
    return None


class _UsageLimitDetector:
    """CCR-6 usage-limit detector (mechanical, Doctrine §20): scan each agent
    result's ``result_text`` plus the LAST ~2KB of its stderr file for the
    configured ``founder_channel.usage_limit_signatures`` (lowercase substrings,
    matched case-insensitively). Provenance: the D-0021 billing-403 incident
    class — a capacity/usage-limit event starves every later spawn, and the
    founder asked to be paged on the first suspicion (12-06-2026), not after a
    stage wedges.

    Run after every StageExecutor conveyor spawn and after the PhaseExecutor
    planning spawn. Alerts are deduplicated per consecutive-match streak (the
    _OutOfBoundsDetector latch pattern): one 'usage_limit_suspected' event +
    one ntfy page per streak; a clean check clears the latch. ntfy delivery
    failure = 'alert_delivery_failed' event, never an exception into the step.
    """

    def __init__(self, db: Database, cfg: FactoryConfig, notify: NtfyPublisher) -> None:
        self._db = db
        self._cfg = cfg
        self._notify = notify
        #: One 'usage_limit_suspected' event per consecutive-match streak.
        self._event_logged = False
        #: One successful ntfy publish per streak (retried until delivered).
        self._published = False
        #: One alert_delivery_failed event per delivery-failure streak.
        self._delivery_failed_logged = False

    async def check(
        self, result: AgentResult, *, unit_level: str, unit_id: str, role: str
    ) -> str | None:
        """Scan one agent result; page on a match (streak-deduplicated).
        Returns the matched signature (None on a clean check) — CCR-11: the
        caller feeds the match into the capacity governor's HOLD entry and into
        the incident-7 gate's ``usage_limit`` evidence mark."""
        signature = _usage_limit_match(self._cfg, result)
        if signature is None:
            # Clean observation ends the streak: a future match re-pages.
            self._event_logged = False
            self._published = False
            self._delivery_failed_logged = False
            return None
        if not self._event_logged:
            self._event_logged = True
            with self._db.transaction() as conn:
                fdb.insert_event(
                    conn,
                    unit_level=unit_level,
                    unit_id=unit_id,
                    event_type="usage_limit_suspected",
                    actor=_ACTOR,
                    payload={
                        "role": role,
                        "signature": signature,
                        "process_id": result.process_id,
                    },
                )
        if not self._published:
            try:
                await self._notify.publish(
                    "Limită de utilizare suspectată — fabrica poate avea nevoie de pauză",
                    link=dashboard_link(self._cfg, "acum"),
                    priority=self._notify.priority_alert,
                )
                self._published = True
                self._delivery_failed_logged = False
            except NotifyError as exc:
                if not self._delivery_failed_logged:
                    self._delivery_failed_logged = True
                    with self._db.transaction() as conn:
                        fdb.insert_event(
                            conn,
                            unit_level="factory",
                            unit_id=None,
                            event_type="alert_delivery_failed",
                            actor=_ACTOR,
                            payload={
                                "kind": "usage_limit_suspected",
                                "role": role,
                                "error": str(exc),
                            },
                        )
        return signature


class CapacityGovernor:
    """CCR-11 (D-0037) capacity governor (mechanical, Doctrine §20): when the
    CCR-6 detector matches a usage-limit signature, the factory drains itself,
    probes cheaply and resumes ALONE — no architect in the loop for limit-class
    failures.

    HOLD entry (``note_match``): in-memory flag + ONE persistent
    'capacity_hold_started' event per hold episode (factory level; payload:
    matched signature, role, process_id). The detector keeps its own CCR-6
    page/event streak dedup — the governor never pages on entry.

    HOLD semantics (``blocks``): while held, the executors skip any step whose
    spawn set contains a claude-route role; codex/stub-routed steps proceed
    (proven cross-provider independence — Tier-2/cross-audit keep flowing,
    Tier-1 is mechanical and unaffected). A held step simply does not run this
    tick: state untouched, NO event, NO escalation; the orchestrator keeps
    ticking, so the §7 concurrency model and the watchdog's liveness
    expectations are untouched.

    PROBE (``tick``): every ``capacity_governor.probe_interval_s`` while held,
    one canary run on the declared ``models.capacity_probe`` route (cheapest
    claude). The probe is an ORDINARY registered run — process registry +
    token ledger record it like everything else (role 'capacity_probe',
    factory-level unit: the factory-level events precedent; the runner's unit
    plumbing needs a non-NULL unit_id, so the honest minimal is the literal
    'factory'). It bypasses the hold gate (it IS the gate's exit) and the
    incident-7 gate (spawned directly through the runner, never through
    ``_run_step_agent``), so a dead probe can NEVER escalate — explicitly:
    probe failure is the held-state signal, nothing more, no escalation row.

    AUTO-RESOLVE on hold lift: STRICTLY the open 'agent_run_failed'
    escalations whose evidence carries ``usage_limit: true`` resolve through
    the EXISTING machinery (db.resolve_escalation + 'escalation_resolved'
    event, actor 'capacity_governor'); ``_step_escalated`` picks them up on
    the next tick. Everything else stays open for the architect.

    Hold state is in-memory ONLY (restart honesty, D-0037): an orchestrator
    restart during an outage re-discovers the limit on its first dead claude
    spawn — one wasted cheap spawn, accepted; ``reconcile_restart`` closes a
    stale event pair at recovery so the dashboard read-path never lies.
    """

    #: The canary's config role key (models.capacity_probe, CCR-11).
    PROBE_ROLE = "capacity_probe"

    def __init__(
        self, db: Database, cfg: FactoryConfig, runner: AgentRunner, notify: NtfyPublisher
    ) -> None:
        self._db = db
        self._cfg = cfg
        self._runner = runner
        self._notify = notify
        self._held = False
        #: Loop-clock deadline of the next probe while held.
        self._next_probe_at = 0.0
        #: One alert_delivery_failed event per founder-page delivery-failure streak.
        self._delivery_failed_logged = False
        #: Same dedup, separate streak, for the UNIT 3 architect resume page.
        self._architect_resume_failed_logged = False
        #: D-0059 PROACTIVE limit hold — independent of the reactive ``_held``
        #: (entry: a %-threshold cross on the live OAuth usage; exit: usage back
        #: under BOTH thresholds, NEVER the canary probe). The ``blocks`` gate is
        #: shared, so a step held by either reason behaves identically.
        self._proactive_held = False
        #: Loop-clock deadline of the next OAuth usage poll (0.0 ⇒ poll on the
        #: first tick so the limit state is known at startup).
        self._next_limit_poll_at = 0.0
        #: One alert event + one founder page per usage-poll-failure streak (the
        #: proactive guard is blind while the query fails; reactive still backs up).
        self._limit_poll_failed_logged = False
        #: Separate streak latch for the proactive founder pages (never shares the
        #: reactive ``_delivery_failed_logged``, so a proactive success can't clear
        #: the reactive latch and vice-versa).
        self._proactive_page_failed_logged = False

    @property
    def enabled(self) -> bool:
        return self._cfg.capacity_governor.enabled

    @property
    def held(self) -> bool:
        """Held for EITHER reason — the reactive signature hold or the D-0059
        proactive %-threshold hold. The executor gate reads this."""
        return self._held or self._proactive_held

    def blocks(self, roles: Sequence[str]) -> bool:
        """True when a capacity hold (reactive OR proactive) gates a step that
        would spawn any claude-route role. Config-unknown roles never block
        here — their fail-explicit ConfigError belongs to the step itself."""
        if not self.held:
            return False
        models = self._cfg.models
        return any(role in models and models[role].cli == "claude" for role in roles)

    def note_match(
        self, *, signature: str, role: str, process_id: int | None
    ) -> None:
        """Mechanical HOLD entry (Doctrine §20) on a detector match. Idempotent
        while held (one 'capacity_hold_started' event per episode); a no-op
        when the governor is disabled (enabled:false = byte-identical to
        pre-CCR-11, pinned by test)."""
        if not self.enabled or self._held:
            return
        self._held = True
        # First probe one full interval AFTER entry: capacity just proved
        # dead — an immediate canary is a known-fail spawn.
        self._next_probe_at = (
            asyncio.get_running_loop().time()
            + self._cfg.capacity_governor.probe_interval_s
        )
        with self._db.transaction() as conn:
            fdb.insert_event(
                conn,
                unit_level="factory",
                unit_id=None,
                event_type="capacity_hold_started",
                actor=_ACTOR,
                payload={
                    "signature": signature,
                    "role": role,
                    "process_id": process_id,
                },
            )

    def reconcile_restart(self) -> None:
        """Recovery-time honesty (D-0037 item 6): holds do NOT survive
        restarts, so an unclosed capacity_hold_started/_ended event pair from
        a previous process is closed here — otherwise the dashboard's
        event-pair read-path would show a hold no live governor owns (forever,
        if capacity returned while the orchestrator was down)."""
        if self._hold_pair_open(self._db.read()):
            with self._db.transaction() as conn:
                fdb.insert_event(
                    conn,
                    unit_level="factory",
                    unit_id=None,
                    event_type="capacity_hold_ended",
                    actor=_ACTOR,
                    payload={
                        "reason": (
                            "orchestrator restart — hold state is in-memory and"
                            " is re-discovered on the first dead claude spawn"
                            " (D-0037)"
                        ),
                    },
                )
        # D-0059: the proactive hold is in-memory too — close a stale pair so the
        # dashboard never shows a proactive hold no live governor owns; the next
        # usage poll re-discovers it if still over threshold.
        if self._proactive_hold_pair_open(self._db.read()):
            with self._db.transaction() as conn:
                fdb.insert_event(
                    conn,
                    unit_level="factory",
                    unit_id=None,
                    event_type="proactive_limit_hold_ended",
                    actor=_ACTOR,
                    payload={
                        "reason": (
                            "orchestrator restart — proactive hold is in-memory and"
                            " is re-discovered on the next usage poll (D-0059)"
                        ),
                    },
                )

    @staticmethod
    def _hold_pair_open(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT"
            " COALESCE(MAX(CASE WHEN event_type='capacity_hold_started' THEN seq END), 0),"
            " COALESCE(MAX(CASE WHEN event_type='capacity_hold_ended' THEN seq END), 0)"
            " FROM events WHERE event_type IN"
            " ('capacity_hold_started','capacity_hold_ended')"
        ).fetchone()
        return int(row[0]) > int(row[1])

    @staticmethod
    def _proactive_hold_pair_open(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT"
            " COALESCE(MAX(CASE WHEN event_type='proactive_limit_hold_started' THEN seq END), 0),"
            " COALESCE(MAX(CASE WHEN event_type='proactive_limit_hold_ended' THEN seq END), 0)"
            " FROM events WHERE event_type IN"
            " ('proactive_limit_hold_started','proactive_limit_hold_ended')"
        ).fetchone()
        return int(row[0]) > int(row[1])

    async def tick(self) -> None:
        """Scheduler-loop hook (every loop_tick_s). TWO independent concerns:
        FIRST the D-0059 proactive usage poll (own interval) so a fresh
        threshold cross holds new spawns before this tick's dispatch; THEN —
        only while a REACTIVE hold is active and its probe interval elapsed —
        the canary hold-exit probe run INLINE. The await is bounded well under
        the watchdog staleness threshold (see ``_probe``), and running executor
        tasks continue concurrently — only new dispatches wait out the canary."""
        await self._proactive_limit_tick()
        if not self._held:
            return
        now = asyncio.get_running_loop().time()
        if now < self._next_probe_at:
            return
        self._next_probe_at = now + self._cfg.capacity_governor.probe_interval_s
        await self._probe()

    async def _proactive_limit_tick(self) -> None:
        """D-0059 (founder-directed 19-06-2026): poll the LIVE OAuth usage on its
        own interval and hold/lift the PROACTIVE cap. Active only under
        ``enabled`` + ``proactive_enabled`` (a disabled governor never polls —
        the byte-identical invariant). A failed query is fail-explicit (Doctrine
        §7): the current hold state is kept and the reactive signature path backs
        it up — never a guessed limit. The hold lifts only when BOTH the 5h and
        weekly utilizations are back under their thresholds (i.e. after the
        reset); the canary probe NEVER lifts a proactive hold (it would succeed
        at 80% and defeat the purpose)."""
        cg = self._cfg.capacity_governor
        # Live overrides (founder dashboard, 5f/item 4): `autodrenaj` is the live
        # replacement for cfg.proactive_enabled — it DEFAULTS to that YAML value
        # (byte-identical when unset), so the founder can flip the proactive
        # auto-drain ON/OFF from the panel without a restart. The thresholds are
        # likewise live (gov_*_pct default to the YAML thresholds).
        eff = EffectiveConfig(fdb.get_runtime_settings(self._db.read()), self._cfg)
        if not (self.enabled and eff.autodrenaj):
            return
        now = asyncio.get_running_loop().time()
        if now < self._next_limit_poll_at:
            return
        self._next_limit_poll_at = now + cg.proactive_poll_interval_s
        usage = await asyncio.to_thread(self._query_usage)
        if usage is None:
            await self._note_poll_failure()
            return
        self._limit_poll_failed_logged = False  # streak cleared on a good poll
        five_hour, seven_day = usage
        over = (
            five_hour >= eff.gov_five_hour_pct
            or seven_day >= eff.gov_seven_day_pct
        )
        if over and not self._proactive_held:
            await self._enter_proactive_hold(
                five_hour,
                seven_day,
                five_hour_threshold=eff.gov_five_hour_pct,
                seven_day_threshold=eff.gov_seven_day_pct,
            )
        elif not over and self._proactive_held:
            await self._lift_proactive_hold(five_hour, seven_day)

    def _query_usage(self) -> tuple[float, float] | None:
        """Blocking OAuth usage GET — runs OFF-loop via ``asyncio.to_thread``
        (the notify.py precedent, §7). Returns ``(five_hour%, seven_day%)`` or
        ``None`` on ANY failure (missing/short token file, network, malformed
        body) — never a guessed number (Doctrine §7). sf-limit.sh parity
        (D-0058): same endpoint, beta header, and credentials key."""
        cg = self._cfg.capacity_governor
        try:
            path = os.path.expanduser(cg.oauth_credentials_path)
            with open(path, encoding="utf-8") as handle:
                token = json.load(handle)["claudeAiOauth"]["accessToken"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        request = urllib.request.Request(
            cg.usage_endpoint,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": cg.usage_beta_header,
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=cg.usage_poll_timeout_s
            ) as response:
                data = json.load(response)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return None
        try:
            five_hour = float((data.get("five_hour") or {}).get("utilization"))
            seven_day = float((data.get("seven_day") or {}).get("utilization"))
        except (AttributeError, TypeError, ValueError):
            return None
        return five_hour, seven_day

    async def _enter_proactive_hold(
        self,
        five_hour: float,
        seven_day: float,
        *,
        five_hour_threshold: float,
        seven_day_threshold: float,
    ) -> None:
        """Threshold crossed: hold new claude spawns (running agents finish via
        the SHARED ``blocks`` gate — exactly the founder's "termină în siguranță
        cei care lucrează"). ONE 'proactive_limit_hold_started' event + an
        informational founder page (the factory paused ITSELF — notable, but
        working-as-designed, so default priority, not an alert)."""
        self._proactive_held = True
        with self._db.transaction() as conn:
            fdb.insert_event(
                conn,
                unit_level="factory",
                unit_id=None,
                event_type="proactive_limit_hold_started",
                actor=_ACTOR,
                payload={
                    "five_hour": five_hour,
                    "seven_day": seven_day,
                    # The thresholds the decision ACTUALLY used (effective/live),
                    # not the YAML defaults — so the recorded evidence explains the
                    # real reason this hold fired when the founder has edited them.
                    "five_hour_threshold": five_hour_threshold,
                    "seven_day_threshold": seven_day_threshold,
                },
            )
        await self._page(
            f"Fabrica drenează proactiv — limită aproape (5h {five_hour:.0f}%"
            f" / săpt {seven_day:.0f}%): agenții curenți termină, fără spawn-uri noi"
        )

    async def _lift_proactive_hold(self, five_hour: float, seven_day: float) -> None:
        """Usage fell back under BOTH thresholds (the reset): release the hold —
        the next dispatch resumes claude spawns on its own. ONE
        'proactive_limit_hold_ended' event + a founder page."""
        self._proactive_held = False
        with self._db.transaction() as conn:
            fdb.insert_event(
                conn,
                unit_level="factory",
                unit_id=None,
                event_type="proactive_limit_hold_ended",
                actor=_ACTOR,
                payload={"five_hour": five_hour, "seven_day": seven_day},
            )
        await self._page(
            f"Capacitate sub prag (5h {five_hour:.0f}% / săpt {seven_day:.0f}%)"
            f" — fabrica a reluat singură"
        )

    async def _note_poll_failure(self) -> None:
        """Fail-explicit (Doctrine §7/§20): the proactive query failed — log ONE
        'alert' event + page the founder ONCE per failure streak (the proactive
        guard is blind; the reactive signature path still protects). The streak
        latch clears on the next successful poll."""
        if self._limit_poll_failed_logged:
            return
        self._limit_poll_failed_logged = True
        with self._db.transaction() as conn:
            fdb.insert_event(
                conn,
                unit_level="factory",
                unit_id=None,
                event_type="alert",
                actor=_ACTOR,
                payload={"kind": "proactive_limit_poll_failed"},
            )
        await self._page(
            "Interogarea limitei eșuează — protecția proactivă e oarbă"
            " (rămâne doar cea reactivă)",
            priority=self._notify.priority_alert,
        )

    async def _page(self, title: str, *, priority: str | None = None) -> None:
        """Founder ntfy page that NEVER raises into the loop (§7): the durable
        event has already landed; a delivery failure is logged ONCE per streak
        (its own latch, never the reactive one) and swallowed."""
        try:
            await self._notify.publish(
                title,
                link=dashboard_link(self._cfg, "acum"),
                priority=priority if priority is not None else "default",
            )
            self._proactive_page_failed_logged = False
        except NotifyError as exc:
            if not self._proactive_page_failed_logged:
                self._proactive_page_failed_logged = True
                with self._db.transaction() as conn:
                    fdb.insert_event(
                        conn,
                        unit_level="factory",
                        unit_id=None,
                        event_type="alert_delivery_failed",
                        actor=_ACTOR,
                        payload={"kind": "proactive_limit_page", "error": str(exc)},
                    )

    async def _probe(self) -> None:
        """One canary run. Success = exit 0 (not killed/timed out) AND no
        usage-limit signature in the result — then the hold lifts; anything
        else keeps the hold for the next interval. EXPLICITLY no escalation on
        any probe outcome: the probe bypasses the incident-7 gate by
        construction (direct runner spawn), and a dead canary every interval
        would spam escalations that mean nothing beyond 'still held'."""
        # Bound the inline await well under the watchdog staleness threshold:
        # liveness is only touched between scheduler ticks, and a hung CLI must
        # not turn the canary into a false 'orchestrator down' page.
        timeout_s = max(
            1,
            min(
                self._cfg.process.agent_timeout_s,
                int(self._cfg.founder_channel.watchdog.staleness_threshold_s) // 2,
            ),
        )
        try:
            result = await self._runner.run_agent(
                self.PROBE_ROLE,
                'Răspunde cu un singur cuvânt: "pong".',
                unit_level="factory",
                unit_id="factory",
                cwd=self._cfg.factory.home,
                timeout_s=timeout_s,
            )
        except ProcessError as exc:
            # Spawn impossibility (CLI missing, log dir unwritable, …): stay
            # held, durable evidence, NO escalation row (probes never escalate).
            with self._db.transaction() as conn:
                fdb.insert_event(
                    conn,
                    unit_level="factory",
                    unit_id=None,
                    event_type="alert",
                    actor=_ACTOR,
                    payload={"kind": "capacity_probe_spawn_failed", "error": str(exc)},
                )
            return
        dead = result.exit_code != 0 or result.timed_out or result.killed
        if dead or _usage_limit_match(self._cfg, result) is not None:
            return  # still limited — hold stays, next probe in one interval
        await self._lift_hold(probe_process_id=result.process_id)

    async def _lift_hold(self, *, probe_process_id: int) -> None:
        """Capacity is back: ONE tx = 'capacity_hold_ended' event + the strict
        auto-resolve sweep (atomic resume facts), then the founder page OUTSIDE
        the transaction (§7); delivery failure recorded, never raised."""
        with self._db.transaction() as conn:
            fdb.insert_event(
                conn,
                unit_level="factory",
                unit_id=None,
                event_type="capacity_hold_ended",
                actor=_ACTOR,
                payload={"probe_process_id": probe_process_id},
            )
            resolved = self._auto_resolve(conn)
        self._held = False
        try:
            await self._notify.publish(
                "Capacitate revenită — fabrica a reluat singură",
                link=dashboard_link(self._cfg, "acum"),
                priority=self._notify.priority_alert,
            )
            self._delivery_failed_logged = False
        except NotifyError as exc:
            if not self._delivery_failed_logged:
                self._delivery_failed_logged = True
                with self._db.transaction() as conn:
                    fdb.insert_event(
                        conn,
                        unit_level="factory",
                        unit_id=None,
                        event_type="alert_delivery_failed",
                        actor=_ACTOR,
                        payload={
                            "kind": "capacity_hold_ended",
                            "resolved": resolved,
                            "error": str(exc),
                        },
                    )
        # Robustness UNIT 3 (D-0042): EXIT-ONLY architect resume page so the
        # architect resumes alone (the founder no longer has to relay the reset).
        # Same '[arhitect]' prefix + one-shared-topic transport as the Scheduler's
        # _notify_architect, inlined here because the governor holds no Scheduler
        # reference; gated on notify_architect_on_resume (founder suppress toggle,
        # default-on). Its OWN never-raise guard + separate streak latch: a failed
        # architect page can NEVER block the lift (already committed above) or lose
        # the capacity_hold_ended event (already written in the tx above).
        if self._cfg.capacity_governor.notify_architect_on_resume:
            try:
                await self._notify.publish(
                    "[arhitect] capacitate revenită — reia lucrul",
                    link=dashboard_link(self._cfg, "acum"),
                    priority=self._notify.priority_alert,
                )
                self._architect_resume_failed_logged = False
            except NotifyError as exc:
                if not self._architect_resume_failed_logged:
                    self._architect_resume_failed_logged = True
                    with self._db.transaction() as conn:
                        fdb.insert_event(
                            conn,
                            unit_level="factory",
                            unit_id=None,
                            event_type="alert_delivery_failed",
                            actor=_ACTOR,
                            payload={
                                "kind": "capacity_hold_ended_architect",
                                "resolved": resolved,
                                "error": str(exc),
                            },
                        )

    def _auto_resolve(self, conn: sqlite3.Connection) -> list[int]:
        """STRICT auto-resolve scope (D-0037, pinned by test): open escalations
        with trigger 'agent_run_failed' whose agent_run_failed EVENT evidence
        carries ``usage_limit: true`` — nothing else, ever. Resolution token =
        the stage's pre-escalation step, read from its ESCALATED transition's
        from_state via ``_CAPACITY_RESOLUTIONS`` (AUDIT → 'rework:VALIDATE':
        no rework:AUDIT exists in the vocabulary — the re-validation cost is
        accepted; the transition tables stay untouched). Missing facts
        (no event_seq, no limit mark, or a from_state absent from the map)
        leave the row open for the architect — never guessed (Doctrine §7).
        Phase-level rows are out of scope by construction (the incident-7 gate
        only inserts stage rows — phase spawns are the D-0036 watch item); the
        unit_level filter pins that defensively. Resolution machinery = the
        EXISTING pair (db.resolve_escalation + 'escalation_resolved' event,
        the cli.cmd_resolve_escalation shape); the normal ``_step_escalated``
        pickup routes the stage on the next tick."""
        rows = conn.execute(
            "SELECT * FROM escalations WHERE status = 'open'"
            " AND trigger = 'agent_run_failed' AND unit_level = 'stage'"
            " ORDER BY id"
        ).fetchall()
        resolved: list[int] = []
        for row in rows:
            if row["event_seq"] is None:
                continue  # no evidence anchor — stays open, never guessed
            erow = conn.execute(
                "SELECT payload_json FROM events WHERE seq = ?", (row["event_seq"],)
            ).fetchone()
            if erow is None:
                continue
            try:
                evidence = json.loads(erow["payload_json"])
            except json.JSONDecodeError:
                continue
            if not isinstance(evidence, dict) or evidence.get("usage_limit") is not True:
                continue  # the limit mark is the WHOLE basis — no mark, no resume
            trow = conn.execute(
                "SELECT from_state FROM events WHERE unit_level = 'stage'"
                " AND unit_id = ? AND event_type = 'transition'"
                " AND to_state = 'ESCALATED' ORDER BY seq DESC LIMIT 1",
                (row["unit_id"],),
            ).fetchone()
            token = (
                None
                if trow is None
                else _CAPACITY_RESOLUTIONS.get(trow["from_state"] or "")
            )
            if token is None:
                continue  # unmapped pre-escalation step — architect territory
            fdb.resolve_escalation(conn, int(row["id"]), token)
            fdb.insert_event(
                conn,
                unit_level=Level.STAGE.value,
                unit_id=row["unit_id"],
                event_type="escalation_resolved",
                actor="capacity_governor",
                payload={
                    "escalation_id": int(row["id"]),
                    "resolution": token,
                    "reason": _CAPACITY_RESOLVE_REASON,
                    "via": "capacity_governor",
                },
            )
            resolved.append(int(row["id"]))
        return resolved


#: D-0037 auto-resolve rationale — lands in the escalation_resolved event and,
#: via _resolution_reason, in the re-entered step's rework_context prompt line.
_CAPACITY_RESOLVE_REASON = (
    "capacity hold lifted — limit-class failure auto-resumed (D-0037)"
)

#: ESCALATED-transition from_state -> resolution token for the D-0037
#: auto-resolve (every value ∈ models.STAGE_ESCALATION_RESOLUTIONS — pinned by
#: test; the §3 transition tables are NOT touched). AUDIT maps to
#: 'rework:VALIDATE' because no rework:AUDIT token exists in the vocabulary —
#: the limit-killed auditor re-enters through a fresh validation pass
#: (re-validation cost accepted). SPEC_AUDIT mirrors that: it maps to
#: 'rework:SPEC' (no rework:SPEC_AUDIT token) — a limit-killed spec-audit
#: re-runs through a fresh spec pass, which then re-enters SPEC_AUDIT.
#: MERGE_GATE maps to 'rework:MERGE_GATE'
#: (D-0057): a limit-killed Tier-2 run (agent_run_failed + usage_limit) is the
#: ONE failure class the architect could not also recover manually, being
#: frozen by the SAME weekly limit (incidents [61]/[53]). Re-entering ONLY the
#: gate is safe — the trigger='agent_run_failed' filter structurally excludes
#: unresolved_contest, and a stage AT merge-gate has definitionally passed
#: structural validation + dual AUDIT, so the architect-operations §3
#: misapplication risks (unresolved_contest / pre-AUDIT) cannot arise.
_CAPACITY_RESOLUTIONS: Mapping[str, str] = {
    "SPEC": "rework:SPEC",
    "SPEC_AUDIT": "rework:SPEC",
    "BUILD": "rework:BUILD",
    "VALIDATE": "rework:VALIDATE",
    "AUDIT": "rework:VALIDATE",
    "MERGE_GATE": "rework:MERGE_GATE",
}


# ------------------------------------------------------------- frozen interfaces


class UnitExecutor(Protocol):
    """Per-level step-sequence driver (design §4)."""

    level: Level

    async def execute(self, unit_id: str) -> None:
        """Drive one unit from its current state until BLOCKED or terminal; every step:
        run agent, register artifacts, evaluate thresholds first, CP-1 only when
        thresholds do not decide, transition."""
        ...

    def spawn_roles(self, unit: object) -> tuple[str, ...]:
        """Roles the unit's CURRENT step may spawn — `()` when the step runs no
        agent (dispatch bookkeeping, gates, ESCALATED pickup). The scheduler-
        fairness predicate (robustness UNIT 1): no-spawn control-plane work is
        exempt from the agent-slot cap, so a resolved escalation's pickup is not
        starved behind routine agents. The same per-step contract the capacity
        governor consumes (CCR-11/D-0037)."""
        ...


@dataclass(frozen=True)
class RecoveryReport:
    """Outcome of Scheduler.recover() (§5.5 steps a–d)."""

    #: process_registry ids flipped to 'orphaned' (step a).
    orphaned: tuple[int, ...]
    #: pids whose process GROUP was SIGKILLed during the sweep (step a).
    killed_groups: tuple[int, ...]
    #: checkout path -> heal_git_state actions taken (step b).
    healed: Mapping[str, tuple[str, ...]]
    #: paths whose healing failed (recorded, unit escalates on next drive).
    heal_errors: tuple[str, ...]
    #: unit worktrees hard-reset after dirty-state evidence capture (step b).
    dirty_reset: tuple[str, ...]
    #: verify_integrity counters (step c; failures abort before a report exists).
    integrity_checked: int
    integrity_warnings: int
    #: '<level>:<unit_id>' units in RUNNING-category states re-entering the queue (step d).
    requeued: tuple[str, ...]


# ----------------------------------------------------------------- StageExecutor


class StageExecutor:
    """Implements UnitExecutor(level=STAGE): the §3.1 SPEC→…→MERGE_GATE conveyor,
    including Validator scratch-worktree isolation, thresholds-first-then-CP-1
    routing, the §3.1 Tier-2 input contract and CP-1 verdict execution."""

    level: Level = Level.STAGE

    def __init__(
        self,
        db: Database,
        sm: StateMachine,
        cfg: FactoryConfig,
        runner: AgentRunner,
        wt: WorktreeManager,
        thresholds: ThresholdEvaluator,
        consultor: Consultor,
        notify: NtfyPublisher,
        governor: CapacityGovernor | None = None,
    ) -> None:
        """Wires the stage conveyor; no policy outside config. ``governor``
        (CCR-11, optional): the SHARED capacity governor — cli wiring passes
        one instance to both executors and the Scheduler so a hold entered
        here gates the phase executor too and the loop probes it; standalone
        construction (tests) gets a private instance."""
        self._db = db
        self._sm = sm
        self._cfg = cfg
        self._runner = runner
        self._wt = wt
        self._thresholds = thresholds
        self._consultor = consultor
        self._notify = notify
        #: Phase-seeding design §5: out-of-bounds check at every MERGE_GATE entry.
        self._oob = _OutOfBoundsDetector(db, cfg, notify)
        #: CCR-6: usage-limit scan after every conveyor agent run.
        self._usage_limit = _UsageLimitDetector(db, cfg, notify)
        #: CCR-11 (D-0037): capacity hold entry + step gate.
        self._governor = governor or CapacityGovernor(db, cfg, runner, notify)

    # ---------------------------------------------------------------- protocol

    async def execute(self, unit_id: str) -> None:
        """Drive one stage until BLOCKED, terminal, or no further progress is
        possible without external input; each step follows the §4 contract."""
        steps = {
            StageState.PENDING: self._step_dispatch,
            StageState.SPEC: self._step_spec,
            StageState.SPEC_AUDIT: self._step_spec_audit,
            StageState.BUILD: self._step_build,
            StageState.VALIDATE: self._step_validate,
            StageState.AUDIT: self._step_audit,
            StageState.MERGE_GATE: self._step_merge_gate,
            StageState.AWAITING_HUMAN: self._step_awaiting_human,
            StageState.ESCALATED: self._step_escalated,
        }
        while True:
            stage = self._stage(unit_id)
            step = steps.get(stage.state)
            if step is None:  # DONE / FAILED / CANCELLED
                return
            spawn_roles = self._step_spawn_roles(stage)
            if spawn_roles and EffectiveConfig(
                fdb.get_runtime_settings(self._db.read()), self._cfg
            ).drain_manual:
                # founder manual DRAIN at AGENT granularity (5e correction): this
                # step would spawn a NEW agent — park here (state untouched, NO
                # event). Agents of earlier steps already finished (execute()
                # awaits sequentially), so the stage winds down to the AGENT
                # boundary, not the stage end. Drain-lift re-dispatches us (see
                # _dispatch drain_lifted). Only read runtime_settings when a step
                # actually spawns — cheap no-spawn steps must not pay the read.
                return
            if self._governor.held and self._governor.blocks(spawn_roles):
                # CCR-11 (D-0037) capacity hold: this step would spawn a
                # claude-route agent — it simply does not run this tick (state
                # untouched, NO event, NO escalation); codex/stub-routed steps
                # proceed, and the hold-exit probe's events re-dispatch us.
                return
            try:
                progressed = await step(stage)
            except _AgentRunFailed:
                # Incident 7 (D-0035): the gate already escalated (trigger
                # 'agent_run_failed') + transitioned to ESCALATED; stop driving
                # THIS unit only — containment here keeps the §6 boundary from
                # re-escalating it as 'internal_error'.
                return
            if not progressed:
                return

    # ------------------------------------------------------------ shared bits

    def _stage(self, stage_id: str) -> Stage:
        stage = fdb.get_stage(self._db.read(), stage_id)
        if stage is None:
            raise FactoryError(f"unknown stage unit: {stage_id!r}")
        return stage

    def _context(self, stage: Stage) -> tuple[Phase, ProjectCfg, Path, str]:
        """(phase, project, repo_root, target_branch) for a stage."""
        phase = fdb.get_phase(self._db.read(), stage.phase_id)
        if phase is None:
            raise FactoryError(f"stage {stage.id!r} references unknown phase")
        project = _project_for_phase(self._cfg, phase)
        target_branch = phase.branch or f"phase/{phase.id}"
        return phase, project, Path(project.workspace), target_branch

    def _worktree(self, stage: Stage) -> Path:
        if not stage.worktree_path:
            raise FactoryError(f"stage {stage.id!r} has no worktree (not dispatched?)")
        return Path(stage.worktree_path)

    def _unit_dir(self, root: Path, stage: Stage) -> Path:
        return unit_artifact_dir(root, Level.STAGE, stage.id)

    def _step_spawn_roles(self, stage: Stage) -> tuple[str, ...]:
        """Roles the CURRENT step may spawn — the capacity-hold predicate
        (CCR-11/D-0037), evaluated only while held. Conservative by design: a
        step is held when ANY role it might spawn routes to claude (VALIDATE
        includes the conditional CP-1 consult; AUDIT includes the responder),
        because a mid-step block would leave a half-run step behind. Steps
        with no LLM spawn (dispatch, gates, ESCALATED — the auto-resolve
        pickup) return () and are never held."""
        if stage.state is StageState.SPEC:
            return ("spec_agent",)
        if stage.state is StageState.SPEC_AUDIT:
            # The spec auditors + the spec_agent triage executor (it owns the spec).
            return (*self._risk_cfg(stage).spec_audits, "spec_agent")
        if stage.state is StageState.BUILD:
            return (_builder_role(self._cfg, stage.risk_class, stage.kind),)
        if stage.state is StageState.VALIDATE:
            return (self._validator_role(stage), _cp_point(self._cfg, CP1_ID).role)
        if stage.state is StageState.AUDIT:
            return (
                *self._risk_cfg(stage).audits,
                _builder_role(self._cfg, stage.risk_class, stage.kind),
            )
        if stage.state is StageState.MERGE_GATE:
            return ("integration_validator",)
        return ()

    def spawn_roles(self, unit: object) -> tuple[str, ...]:
        """Public UnitExecutor surface over `_step_spawn_roles` (robustness UNIT
        1): the scheduler asks 'does this stage's current step spawn an agent?'
        to keep no-spawn control-plane work (ESCALATED pickup, gates, dispatch)
        out of the agent-slot cap. Single source — never re-derive the no-spawn
        set (Doctrine §0/§9)."""
        assert isinstance(unit, Stage), f"StageExecutor.spawn_roles got {type(unit)!r}"
        return self._step_spawn_roles(unit)

    async def _run_step_agent(
        self,
        stage: Stage,
        role: str,
        prompt: str,
        *,
        cwd: Path,
        resume_session: str | None = None,
    ) -> AgentResult:
        """Spawn one conveyor agent and run the §5.4 mechanical post-conditions:
        detect sentinels where the agent ran and persist their events (the §2
        always-fire trigger inputs); evaluation/escalation is the caller's
        thresholds pass."""
        result = await self._runner.run_agent(
            role,
            prompt,
            unit_level=Level.STAGE.value,
            unit_id=stage.id,
            cwd=cwd,
            resume_session=resume_session,
            stage_kind=stage.kind,
        )
        # CCR-6: capacity-event scan before anything consumes the result — a
        # usage-limited CLI exits "successfully" with a refusal text, and the
        # founder asked to be paged on the first suspicion (D-0021 class).
        matched = await self._usage_limit.check(
            result, unit_level=Level.STAGE.value, unit_id=stage.id, role=role
        )
        if matched is not None:
            # CCR-11 (D-0037): mechanical HOLD entry — the detector already
            # paged (its own streak dedup); the governor records the episode.
            self._governor.note_match(
                signature=matched, role=role, process_id=result.process_id
            )
        sentinels = detect_sentinels(self._unit_dir(cwd, stage))
        if sentinels:
            with self._db.transaction() as conn:
                for kind in sentinels:
                    event_type, _ = _SENTINEL_EVENTS[kind]
                    fdb.insert_event(
                        conn,
                        unit_level=Level.STAGE.value,
                        unit_id=stage.id,
                        event_type=event_type,
                        actor=role,
                        payload={
                            "sentinel": str(self._unit_dir(cwd, stage) / STAGE_ARTIFACTS[kind]),
                            "process_id": result.process_id,
                        },
                    )
        # Incident 7 (D-0035) success gate: a step may consume agent output
        # only when the run succeeded (exit 0) or the agent explicitly declared
        # itself (sentinel present — the §5.4 always-fire path wins, even on a
        # nonzero exit, so a declare-then-exit-1 agent never double-escalates).
        # Everything not cleanly zero (nonzero, killed, timed out, or a None
        # returncode) is a DEAD run: whatever sits in the worktree is a stale
        # artifact of an EARLIER run, never this run's output — escalate and
        # stop the step before any consuming code touches it. The runner has
        # already ledgered the run's tokens (runner.run_agent finalize tx).
        if not sentinels and (
            result.exit_code != 0 or result.timed_out or result.killed
        ):
            # CCR-11 (D-0037): limit-mark the gate — the evidence carries
            # usage_limit so the governor's auto-resolve can tell limit-class
            # corpses from genuine failures. Recorded only while the governor
            # is enabled (enabled:false stays byte-identical to pre-CCR-11).
            await self._escalate_agent_run_failed(
                stage,
                role,
                result,
                cwd,
                usage_limit=(matched is not None) if self._governor.enabled else None,
            )
            raise _AgentRunFailed(
                f"stage {stage.id}: {role} run failed (exit_code={result.exit_code},"
                f" timed_out={result.timed_out}, killed={result.killed})"
                " — escalated, step stopped before consuming artifacts"
            )
        return result

    async def _escalate_agent_run_failed(
        self,
        stage: Stage,
        role: str,
        result: AgentResult,
        cwd: Path,
        *,
        usage_limit: bool | None = None,
    ) -> None:
        """Incident 7 (D-0035) gate bookkeeping: discard the dead run's
        uncommitted leftovers (stage worktree only — a scratch cwd is disposed
        by its step's ``finally``), then escalation row with the
        scheduler-literal trigger 'agent_run_failed' (the 'cp1_verdict' /
        'usage_missing' pattern — no Trigger enum member, no DDL change) +
        one transition to ESCALATED + the §8 B7 founder page. §5.5d replay is
        untouched: committed prior work survives — only the corpse's
        uncommitted writes are dropped, so a 'rework:<STEP>' resolution
        re-enters cleanly (the §3.1 BUILD isolation assertion would otherwise
        wedge on the leftovers)."""
        evidence: dict[str, object] = {
            "role": role,
            "process_id": result.process_id,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "killed": result.killed,
            "duration_ms": result.duration_ms,
            "stderr_path": result.stderr_path,
        }
        if usage_limit is not None:
            # CCR-11 (D-0037): the limit mark — the governor's auto-resolve
            # scope filter (True ⇔ the dead run matched a usage_limit
            # signature). None = governor disabled: key omitted, payload
            # byte-identical to pre-CCR-11.
            evidence["usage_limit"] = usage_limit
        if (
            stage.worktree_path
            and cwd.resolve() == Path(stage.worktree_path).resolve()
        ):
            evidence["discarded"] = await self._discard_uncommitted(cwd)

        def coupled(conn: sqlite3.Connection) -> None:
            seq = fdb.insert_event(
                conn,
                unit_level=Level.STAGE.value,
                unit_id=stage.id,
                event_type="agent_run_failed",
                actor=_ACTOR,
                payload=evidence,
            )
            if not fdb.open_escalation(
                conn, Level.STAGE.value, stage.id, "agent_run_failed"
            ):  # uq_open_escalation: one open row per trigger
                fdb.insert_escalation(
                    conn,
                    Escalation(
                        id=None,
                        unit_level=Level.STAGE.value,
                        unit_id=stage.id,
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

        try:
            self._sm.transition(
                Level.STAGE,
                stage.id,
                StageState.ESCALATED.value,
                actor=_ACTOR,
                reason="agent run failed: not-cleanly-zero exit, no sentinel declared",
                payload=evidence | {"triggers": ["agent_run_failed"]},
                coupled=coupled,
            )
        except TransitionError:
            # A concurrent sibling (the AUDIT gather) already escalated this
            # stage: the event + (deduped) escalation row still land —
            # visible, never silent (the _contain_failure precedent).
            with self._db.transaction() as conn:
                coupled(conn)
        await self._publish_alert(
            f"Escaladare: etapa {stage.name} — agent_run_failed",
            "escaladari",
            context={"unit_id": stage.id, "triggers": ["agent_run_failed"]},
        )

    async def _discard_uncommitted(self, worktree: Path) -> list[str]:
        """Drop a failed run's uncommitted writes (evidence first): a dead
        agent's partial files are corpse output nobody may consume, and left
        in place they wedge the §5.5d rework re-entry on the §3.1 isolation
        assertion. Returns the discarded porcelain entries; the run's ndjson +
        stderr logs persist for forensics (§5.5b dirty-reset precedent)."""
        code, out, err = await run_git("status", "--porcelain", cwd=worktree)
        if code != 0:
            raise GitError(f"git status failed in {worktree}: {(err or out).strip()}")
        discarded = [line for line in out.splitlines() if line.strip()]
        if not discarded:
            return []
        code, out, err = await run_git("reset", "--hard", cwd=worktree)
        if code != 0:
            raise GitError(
                f"git reset --hard failed in {worktree}: {(err or out).strip()}"
            )
        code, out, err = await run_git("clean", "-fd", cwd=worktree)
        if code != 0:
            raise GitError(f"git clean -fd failed in {worktree}: {(err or out).strip()}")
        return discarded[:200]  # bounded evidence, like the stderr tail scans

    async def _commit_unit_paths(
        self, stage: Stage, worktree: Path, paths: Sequence[Path], message: str
    ) -> str | None:
        return await commit_paths(
            worktree, paths, message, trailers={"Factory-Unit": f"stage/{stage.id}"}
        )

    async def _apply_thresholds(self, stage: Stage, worktree: Path | None) -> bool:
        """§8-first routing: evaluate the §2 trigger set; execute every firing
        mechanically. context_budget within escalation.max_context_resets =
        state-preserving reset (event 'context_reset'); everything else (and a
        budget firing past the reset allowance) = escalation rows + one
        transition to ESCALATED. Returns True when the stage escalated."""
        firings = self._thresholds.evaluate(stage)
        escalating: list[tuple[str, dict]] = []
        for firing in firings:
            if firing.trigger is Trigger.CONTEXT_BUDGET:
                read = self._db.read()
                reset_seq = _last_event_seq_of_type(read, stage.id, "context_reset")
                spawn_seq = _last_event_seq_of_type(read, stage.id, "spawn")
                if reset_seq > spawn_seq:
                    # A reset is pending and no agent has consumed it yet (no
                    # spawn since): the breach was already answered — counting
                    # it again within the same step would burn the allowance
                    # AND escalate on one breach. Skip; it re-fires after the
                    # next (fresh-context) spawn if the budget is still blown.
                    continue
                resets = int(firing.evidence.get("context_resets", 0))
                if resets < self._cfg.escalation.max_context_resets:
                    with self._db.transaction() as conn:
                        fdb.insert_event(
                            conn,
                            unit_level=Level.STAGE.value,
                            unit_id=stage.id,
                            event_type="context_reset",
                            actor=_ACTOR,
                            payload=firing.evidence,
                        )
                    continue
            escalating.append((firing.trigger.value, firing.evidence))
        # D-0014(1): budgets.usage_missing_policy='escalate_after' is owned by
        # the StageExecutor as a direct events-table count — a usage-blind
        # stage must still hit a budget (Doctrine §20). NOT a Trigger enum
        # member: the enum stays the set of §8 SQL-evaluated triggers.
        breach = self._usage_missing_breach(stage)
        if breach is not None:
            escalating.append(("usage_missing", breach))
        if not escalating:
            return False
        payload_ref_id = await self._escalation_payload(stage, worktree, escalating)

        def coupled(conn: sqlite3.Connection) -> None:
            for trigger, evidence in escalating:
                if fdb.open_escalation(conn, Level.STAGE.value, stage.id, trigger):
                    continue  # uq_open_escalation: one open row per trigger
                fdb.insert_escalation(
                    conn,
                    Escalation(
                        id=None,
                        unit_level=Level.STAGE.value,
                        unit_id=stage.id,
                        trigger=trigger,
                        target="phase_architect",
                        payload_artifact_id=payload_ref_id,
                        event_seq=evidence.get("event_seq"),
                        status="open",
                        resolution=None,
                        created_at=utc_now(),
                        resolved_at=None,
                    ),
                )

        self._sm.transition(
            Level.STAGE,
            stage.id,
            StageState.ESCALATED.value,
            actor=_ACTOR,
            reason="threshold trigger(s) fired",
            payload={"triggers": [t for t, _ in escalating]},
            coupled=coupled,
        )
        # §8 B7: a mechanical escalation pages the founder channel without any
        # human prompting ("escalation row + ntfy stub called") — title + deep
        # link only (D-0004), strictly OUTSIDE the transaction (§7); delivery
        # failure = 'alert_delivery_failed' event, rows already landed.
        await self._publish_alert(
            f"Escaladare: etapa {stage.name} — "
            + ", ".join(t for t, _ in escalating),
            # §10.4/D-0027 (A-4): deep links land on a REAL rendered anchor —
            # the dashboard's open-escalations block — never a dead fragment.
            "escaladari",
            context={"unit_id": stage.id, "triggers": [t for t, _ in escalating]},
        )
        return True

    def _usage_missing_breach(self, stage: Stage) -> dict | None:
        """D-0014(1) check, executor-owned (same pattern as 'internal_error'):
        under 'escalate_after', count this stage's 'usage_missing' events (the
        runner writes them, §5.1); past budgets.usage_missing_max_per_stage the
        executor escalates directly. Returns the evidence dict, or None."""
        if self._cfg.budgets.usage_missing_policy != "escalate_after":
            return None
        row = self._db.read().execute(
            "SELECT COUNT(*), COALESCE(MAX(seq), 0) FROM events"
            " WHERE unit_level = 'stage' AND unit_id = ?"
            " AND event_type = 'usage_missing'",
            (stage.id,),
        ).fetchone()
        count, last_seq = int(row[0]), int(row[1])
        if count <= self._cfg.budgets.usage_missing_max_per_stage:
            return None
        return {
            "usage_missing_events": count,
            "max_per_stage": self._cfg.budgets.usage_missing_max_per_stage,
            "event_seq": last_seq,
        }

    async def _escalation_payload(
        self,
        stage: Stage,
        worktree: Path | None,
        escalating: Sequence[tuple[str, dict]],
    ) -> int | None:
        """Escalation payload = artifacts, not narrative (DoD §8): the firing
        evidence lands as a committed stage artifact when a worktree exists."""
        if worktree is None:
            return None
        unit_dir = self._unit_dir(worktree, stage)
        unit_dir.mkdir(parents=True, exist_ok=True)
        path = unit_dir / "escalation-payload.md"
        body = ["# Escalation payload (mechanical trigger evidence)", ""]
        for trigger, evidence in escalating:
            body.append(f"## {trigger}")
            body.append("```json")
            body.append(json.dumps(evidence, indent=2, sort_keys=True))
            body.append("```")
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        sha = await self._commit_unit_paths(
            stage, worktree, [path], f"stage {stage.id}: escalation payload"
        )
        with self._db.transaction() as conn:
            ref = register_artifact(
                conn,
                unit_level=Level.STAGE.value,
                unit_id=stage.id,
                kind="escalation_payload",
                repo="workspace",
                repo_root=worktree,
                path=path,
                git_commit=sha,
            )
        return ref.id

    # ------------------------------------------------------------------- steps

    async def _step_dispatch(self, stage: Stage) -> bool:
        """PENDING -> SPEC: idempotent worktree create off the phase integration
        branch, then one tx (transition + worktree columns)."""
        phase, _project, repo_root, target_branch = self._context(stage)
        branch = stage.branch or f"stage/{stage.id}"
        path = await self._wt.create(repo_root, stage.id, branch, target_branch)
        self._sm.transition(
            Level.STAGE,
            stage.id,
            StageState.SPEC.value,
            actor=_ACTOR,
            reason="DAG deps DONE, dispatched",
            payload={"branch": branch, "worktree": str(path)},
            coupled=lambda conn: fdb.set_stage_worktree(conn, stage.id, branch, str(path)),
        )
        return True

    async def _step_spec(self, stage: Stage) -> bool:
        """SPEC: run the Spec Agent; spec.md committed + registered, then -> BUILD."""
        phase, _project, _repo_root, _target = self._context(stage)
        worktree = self._worktree(stage)
        unit_dir = self._unit_dir(worktree, stage)
        conn = self._db.read()
        entry = _last_transition_payload(
            conn, Level.STAGE.value, stage.id, StageState.SPEC.value
        )
        # D-0059 documentary path: the architect asserted a TEXT-ONLY amendment
        # (rework:SPEC_DOC) — skip BUILD (no code re-generation) and go straight to
        # VALIDATE, where VALIDATE + AUDIT mechanically verify the amended spec
        # against the UNCHANGED code (a misclassified edit is caught there, NOT
        # trusted on the architect's word).
        documentary = bool(entry and entry.get("documentary"))
        await self._run_step_agent(
            stage,
            "spec_agent",
            self._spec_prompt(stage, phase, worktree, entry),
            cwd=worktree,
        )
        if await self._apply_thresholds(stage, worktree):
            return False
        spec_path = unit_dir / STAGE_ARTIFACTS["spec"]
        if not spec_path.is_file():
            raise ArtifactContractError(
                f"spec agent produced no {spec_path} for stage {stage.id}"
            )
        sha = await self._commit_unit_paths(
            stage, worktree, [spec_path], f"stage {stage.id}: spec"
        )
        # Non-documentary exit: SPEC_AUDIT when the risk class declares spec_audits,
        # else straight to BUILD (the change is INERT for spec_audits-empty classes;
        # mirrors the VALIDATE -> AUDIT-if-audits-else-MERGE_GATE pattern). The
        # documentary path (rework:SPEC_DOC) still goes SPEC -> VALIDATE, bypassing
        # SPEC_AUDIT — the code is unchanged, so re-auditing the SPEC adds nothing.
        if documentary:
            non_doc_target = StageState.VALIDATE
        else:
            non_doc_target = (
                StageState.SPEC_AUDIT
                if self._risk_cfg(stage).spec_audits
                else StageState.BUILD
            )
        self._sm.transition(
            Level.STAGE,
            stage.id,
            non_doc_target.value,
            actor=_ACTOR,
            reason=(
                "spec amended (documentary) — BUILD skipped, re-validating"
                if documentary
                else "spec artifact registered"
            ),
            coupled=lambda conn: register_artifact(
                conn,
                unit_level=Level.STAGE.value,
                unit_id=stage.id,
                kind="spec",
                repo="workspace",
                repo_root=worktree,
                path=spec_path,
                git_commit=sha,
            ),
        )
        return True

    async def _step_build(self, stage: Stage) -> bool:
        """BUILD: Validator-isolation assertion, Builder run (resume honored per
        the entry transition payload), commit-all, churn recording, -> VALIDATE.
        CCR-8: a clean exit with nothing to commit and no declared failure is
        ACCEPTED (event 'build_noop_accepted') and proceeds to VALIDATE — §5.5d
        idempotent re-entry; independent validation is the gate."""
        worktree = self._worktree(stage)
        await self._assert_no_unregistered_files(stage, worktree)

        conn = self._db.read()
        role = _builder_role(self._cfg, stage.risk_class, stage.kind)
        entry = _last_transition_payload(
            conn, Level.STAGE.value, stage.id, StageState.BUILD.value
        )
        resume = entry.get("resume_session")
        if resume is not None and not isinstance(resume, str):
            resume = None
        if resume is not None:
            # A context_reset after the builder's last spawn forbids resuming —
            # that is what the reset resets (§2/§3.1). Recorded, never silent.
            reset_seq = _last_event_seq_of_type(conn, stage.id, "context_reset")
            spawn_seq = _last_event_seq_of_type(conn, stage.id, "spawn", role=role)
            if reset_seq > spawn_seq:
                with self._db.transaction() as tx:
                    fdb.insert_event(
                        tx,
                        unit_level=Level.STAGE.value,
                        unit_id=stage.id,
                        event_type="verdict_downgraded",
                        actor=_ACTOR,
                        payload={
                            "verdict": "continue_session",
                            "executed_as": "rebuild",
                            "reason": "context_reset",
                        },
                    )
                resume = None

        result = await self._run_step_agent(
            stage,
            role,
            self._build_prompt(stage, worktree, entry),
            cwd=worktree,
            resume_session=resume,
        )

        # Stage the full build (new files included) so churn sees every hunk.
        code, out, err = await run_git("add", "-A", cwd=worktree)
        if code != 0:
            raise GitError(f"git add -A failed in {worktree}: {(err or out).strip()}")
        code, churn_diff, err = await run_git("diff", "--cached", cwd=worktree)
        if code != 0:
            raise GitError(f"git diff --cached failed in {worktree}: {err.strip()}")
        sha = await self._commit_unit_paths(
            stage, worktree, [Path(".")], f"stage {stage.id}: build"
        )
        if await self._apply_thresholds(stage, worktree):
            return False
        # CCR-8: a builder that exits clean (no declared-failure sentinel — the
        # thresholds pass above already escalated that) with NOTHING to commit
        # is a LEGAL no-op, not a contract breach. The §5.5d at-least-once model
        # REQUIRES idempotent re-entry: a rework re-run may find the prior
        # builder's work already committed on the stage branch, verify it and
        # change nothing. VALIDATE is the arbiter — an empty no-op build with
        # missing work FAILS independent validation and loops back via the
        # §8-bounded fix loop; commit-counting was a false gate that turned
        # legal idempotent re-entries into spurious escalations.
        noop = sha is None and not churn_diff
        notes_path = self._unit_dir(worktree, stage) / STAGE_ARTIFACTS["build_notes"]

        def coupled(tx: sqlite3.Connection) -> None:
            if noop:
                fdb.insert_event(
                    tx,
                    unit_level=Level.STAGE.value,
                    unit_id=stage.id,
                    event_type="build_noop_accepted",
                    actor=_ACTOR,
                    payload={
                        "process_id": result.process_id,
                        "note": "no new changes; validation is the gate",
                    },
                )
            self._thresholds.record_churn(tx, stage.id, churn_diff)
            if notes_path.is_file():
                register_artifact(
                    tx,
                    unit_level=Level.STAGE.value,
                    unit_id=stage.id,
                    kind="build_notes",
                    repo="workspace",
                    repo_root=worktree,
                    path=notes_path,
                    git_commit=sha,
                )

        self._sm.transition(
            Level.STAGE,
            stage.id,
            StageState.VALIDATE.value,
            actor=_ACTOR,
            reason="build committed",
            payload={"commit": sha},
            coupled=coupled,
        )
        return True

    async def _assert_no_unregistered_files(self, stage: Stage, worktree: Path) -> None:
        """§3.1 Validator isolation: before each BUILD step the stage worktree
        must contain nothing uncommitted/unregistered — otherwise the Builder
        could code to leaked Validator internals from iteration 2 onward.
        Build/test droppings (process.isolation_ignore_globs: ``__pycache__/``
        etc.) are not Validator internals and never trip the assertion."""
        code, out, err = await run_git("status", "--porcelain", cwd=worktree)
        if code != 0:
            raise GitError(f"git status failed in {worktree}: {(err or out).strip()}")
        ignore_globs = self._cfg.process.isolation_ignore_globs
        offending = [
            line
            for line in out.splitlines()
            # Porcelain v1: 2-char status + space, path starts at column 4.
            if line.strip() and not _isolation_ignored(line[3:], ignore_globs)
        ]
        if offending:
            listing = "\n".join(offending)
            raise IntegrityError(
                f"stage {stage.id} worktree has unregistered files before BUILD"
                f" (Validator-isolation assertion, §3.1):\n{listing}"
            )

    async def _step_validate(self, stage: Stage) -> bool:
        """VALIDATE in a scratch worktree (§3.1 isolation); record the fix
        iteration; route thresholds-first, then CP-1, executing the verdict."""
        phase, _project, repo_root, target_branch = self._context(stage)
        worktree = self._worktree(stage)
        branch = stage.branch or f"stage/{stage.id}"
        validator_role = self._validator_role(stage)

        scratch = await self._wt.create(
            repo_root, f"{stage.id}-validate", branch, branch, new_branch=False
        )
        try:
            await self._run_step_agent(
                stage, validator_role, self._validate_prompt(stage, scratch), cwd=scratch
            )
            if await self._apply_thresholds(stage, worktree):
                return False  # e.g. validator declared failure / contract change

            # Only the two reports cross the isolation boundary (§3.1).
            scratch_dir = self._unit_dir(scratch, stage)
            unit_dir = self._unit_dir(worktree, stage)
            unit_dir.mkdir(parents=True, exist_ok=True)
            sources: list[Path] = []
            for kind in ("validation_report", "validation_sidecar"):
                src = scratch_dir / STAGE_ARTIFACTS[kind]
                if not src.is_file():
                    raise ArtifactContractError(
                        f"validator produced no {STAGE_ARTIFACTS[kind]} for stage {stage.id}"
                        f" (expected at {src})"
                    )
                sources.append(src)
            # Strict-parse in the SCRATCH worktree BEFORE anything crosses into the
            # stage worktree: a contract-violating sidecar escalates without
            # leaving uncommitted files behind that would later trip the §3.1
            # BUILD-isolation assertion after a 'rework:BUILD' resolution.
            summary = read_validation_sidecar(sources[1])
            copied: list[Path] = []
            for src in sources:
                dst = unit_dir / src.name
                shutil.copyfile(src, dst)
                copied.append(dst)
            report_path, sidecar_path = copied
            sha = await self._commit_unit_paths(
                stage, worktree, copied, f"stage {stage.id}: validation report"
            )
        finally:
            # §5.4 sentinel lifecycle: dispose the scratch on EVERY exit (the
            # escalation and contract-violation exits included) — a leaked
            # scratch would resurrect its untracked stale sentinel at the next
            # create() and re-escalate after every subsequent validator run.
            await _dispose_worktree(
                self._db,
                self._wt,
                repo_root,
                scratch,
                unit_level=Level.STAGE.value,
                unit_id=stage.id,
            )

        with self._db.transaction() as conn:
            register_artifact(
                conn,
                unit_level=Level.STAGE.value,
                unit_id=stage.id,
                kind="validation_report",
                repo="workspace",
                repo_root=worktree,
                path=report_path,
                git_commit=sha,
            )
            sidecar_ref = register_artifact(
                conn,
                unit_level=Level.STAGE.value,
                unit_id=stage.id,
                kind="validation_sidecar",
                repo="workspace",
                repo_root=worktree,
                path=sidecar_path,
                git_commit=sha,
            )
            iteration = self._thresholds.record_validation(
                conn, stage.id, summary, sidecar_ref.id
            )

        # Deterministic thresholds FIRST (now including this iteration).
        if await self._apply_thresholds(stage, worktree):
            return False

        if summary.failing == 0:
            audits = self._risk_cfg(stage).audits
            to_state = StageState.AUDIT if audits else StageState.MERGE_GATE
            self._sm.transition(
                Level.STAGE,
                stage.id,
                to_state.value,
                actor=_ACTOR,
                reason="validation passed",
                payload={"iteration": iteration, "failing": 0},
            )
            return True

        # Thresholds did not decide -> CP-1 (DoD §3.4), verdict executed as frozen.
        verdict = await self._consult_cp1(stage, report_path, worktree, target_branch)
        return self._execute_cp1_verdict(stage, worktree, verdict, iteration)

    def _risk_cfg(self, stage: Stage):
        rc = self._cfg.risk_classes.get(stage.risk_class)
        if rc is None:
            raise ConfigError(
                f"stage {stage.id!r} has unknown risk_class {stage.risk_class!r}"
            )
        return rc

    def _validator_role(self, stage: Stage) -> str:
        return self._risk_cfg(stage).validator

    async def _consult_cp1(
        self, stage: Stage, report_path: Path, worktree: Path, target_branch: str
    ) -> Verdict:
        """Assemble CP-1 inputs exactly per the config registry declaration,
        bounded HERE to the registry's max_input_bytes: every assembled input
        is raw-capped, then the canonical payload is fitted as a whole — a
        legitimately oversized validation report must consult (truncated), not
        register as a cp_breach_attempt governance breach (§6 backstop)."""
        cp = _cp_point(self._cfg, CP1_ID)
        spec_ref = fdb.latest_artifact(
            self._db.read(), Level.STAGE.value, stage.id, "spec"
        )
        spec_text = (
            _read_text(worktree / spec_ref.path, what="spec artifact")
            if spec_ref is not None
            else ""
        )
        sources = {
            "validation_report": _bounded(
                _read_text(report_path, what="validation report"), cp.max_input_bytes
            ),
            "diff_digest": await self._wt.diff_digest(
                worktree, target_branch, cp.max_input_bytes
            ),
            "spec": _bounded(spec_text, cp.max_input_bytes),
        }
        unknown = [key for key in cp.inputs if key not in sources]
        if unknown:
            raise ConfigError(
                f"consultation point {cp.id} declares inputs {unknown} that the"
                " stage executor cannot assemble"
            )
        inputs = _fit_consultation_inputs(
            {key: sources[key] for key in cp.inputs}, cp.max_input_bytes
        )
        return await self._consultor.consult(
            cp.id, unit_level=Level.STAGE.value, unit_id=stage.id, inputs=inputs
        )

    def _execute_cp1_verdict(
        self, stage: Stage, worktree: Path, verdict: Verdict, iteration: int
    ) -> bool:
        """DoD §3.4 closed set — every verdict executable, never silently collapsed."""
        value = verdict.value
        base_payload = {
            "verdict": value,
            "fallback_used": verdict.fallback_used,
            "consultation_id": verdict.consultation_id,
            "iteration": iteration,
        }
        if value == "continue_session":
            role = _builder_role(self._cfg, stage.risk_class, stage.kind)
            conn = self._db.read()
            session_id = fdb.last_session_id(
                conn, unit_level=Level.STAGE.value, unit_id=stage.id, role=role
            )
            route = self._cfg.models[role]
            downgrade_reason: str | None = None
            if route.cli not in RESUME_VERIFIED_CLIS:
                downgrade_reason = f"route cli {route.cli!r} lacks verified resume (OPEN-3)"
            elif session_id is None:
                downgrade_reason = "no finalized builder session to resume"
            if downgrade_reason is not None:
                def downgrade(tx: sqlite3.Connection) -> None:
                    fdb.insert_event(
                        tx,
                        unit_level=Level.STAGE.value,
                        unit_id=stage.id,
                        event_type="verdict_downgraded",
                        actor=_ACTOR,
                        payload={
                            "verdict": "continue_session",
                            "executed_as": "rebuild",
                            "reason": downgrade_reason,
                        },
                    )

                self._sm.transition(
                    Level.STAGE,
                    stage.id,
                    StageState.BUILD.value,
                    actor=_ACTOR,
                    reason="CP-1 continue_session downgraded to rebuild",
                    payload=base_payload
                    | {
                        "executed_as": "rebuild",
                        # CCR-9: rework re-entry — the downgrade rationale
                        # reaches the re-spawned Builder's prompt.
                        "rework_context": (
                            "CP-1 continue_session downgraded to rebuild: "
                            f"{downgrade_reason}"
                        ),
                    },
                    coupled=downgrade,
                )
                return True
            self._sm.transition(
                Level.STAGE,
                stage.id,
                StageState.BUILD.value,
                actor=_ACTOR,
                reason="CP-1 verdict continue_session",
                payload=base_payload | {"resume_session": session_id},
            )
            return True
        if value == "rebuild":
            self._sm.transition(
                Level.STAGE,
                stage.id,
                StageState.BUILD.value,
                actor=_ACTOR,
                reason="CP-1 verdict rebuild",
                # CCR-9: rework re-entry — dedicated prompt-context key.
                payload=base_payload | {"rework_context": "CP-1 verdict rebuild"},
            )
            return True
        if value == "respec":
            self._sm.transition(
                Level.STAGE,
                stage.id,
                StageState.SPEC.value,
                actor=_ACTOR,
                reason="CP-1 verdict respec",
                # CCR-9: rework re-entry — dedicated prompt-context key.
                payload=base_payload | {"rework_context": "CP-1 verdict respec"},
            )
            return True
        if value == "escalate":
            def coupled(tx: sqlite3.Connection) -> None:
                if not fdb.open_escalation(
                    tx, Level.STAGE.value, stage.id, "cp1_verdict"
                ):
                    fdb.insert_escalation(
                        tx,
                        Escalation(
                            id=None,
                            unit_level=Level.STAGE.value,
                            unit_id=stage.id,
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

            self._sm.transition(
                Level.STAGE,
                stage.id,
                StageState.ESCALATED.value,
                actor=_ACTOR,
                reason="CP-1 verdict escalate",
                payload=base_payload,
                coupled=coupled,
            )
            return False
        raise FactoryError(
            f"CP-1 returned verdict {value!r} outside the executable set"
        )  # consultation validated the closed set — reaching here is a bug

    async def _step_spec_audit(self, stage: Stage) -> bool:
        """SPEC_AUDIT: spec auditors review spec.md (NOT code) in parallel; the
        spec_agent (it owns the spec) triages the union of findings — comply ->
        SPEC rework (BLOCKING; loop-capped at escalation.spec_audit_max_rework),
        contest -> unresolved-contest escalation, clean -> BUILD. A near-clone of
        _step_audit, but it targets the SPEC and loops to SPEC instead of BUILD.
        The spec auditors use DEDICATED role names (spec_auditor_*), so their
        findings never conflate with the post-build code audit's (which keys every
        recurrence/prior-adjudication query on auditor_role); and by the time a
        stage reaches AUDIT these spec findings are non-'open', so AUDIT's
        open-findings query never consumes them either (no audit_target column
        needed — role-name + status filtering is sufficient)."""
        worktree = self._worktree(stage)
        unit_dir = self._unit_dir(worktree, stage)
        rc = self._risk_cfg(stage)
        roles = list(rc.spec_audits)
        if not roles:
            # Defensive: _step_spec only routes here when spec_audits is non-empty.
            raise FactoryError(
                f"stage {stage.id} entered SPEC_AUDIT with no spec_audit roles"
            )

        existing_open = fdb.findings(self._db.read(), stage.id, ("open",))
        if not existing_open:
            await asyncio.gather(
                *(
                    self._run_step_agent(
                        stage, role, self._spec_audit_prompt(stage, role, worktree),
                        cwd=worktree,
                    )
                    for role in roles
                )
            )
            if await self._apply_thresholds(stage, worktree):
                return False
            to_commit: list[Path] = []
            parsed: list[tuple[str, Path, Path, list[dict]]] = []
            for role in roles:
                report = unit_dir / STAGE_ARTIFACTS["audit_report"].replace("<role>", role)
                sidecar = report.with_suffix(".json")
                if not report.is_file():
                    raise ArtifactContractError(
                        f"spec auditor {role} produced no report at {report}"
                    )
                findings = _read_findings_sidecar(sidecar, auditor_role=role)
                parsed.append((role, report, sidecar, findings))
                to_commit += [report, sidecar]
            sha = await self._commit_unit_paths(
                stage, worktree, to_commit, f"stage {stage.id}: spec audit reports"
            )
            with self._db.transaction() as conn:
                for role, report, sidecar, findings in parsed:
                    ref = register_artifact(
                        conn,
                        unit_level=Level.STAGE.value,
                        unit_id=stage.id,
                        kind="audit_report",
                        repo="workspace",
                        repo_root=worktree,
                        path=report,
                        git_commit=sha,
                    )
                    register_artifact(
                        conn,
                        unit_level=Level.STAGE.value,
                        unit_id=stage.id,
                        kind="audit_report",
                        repo="workspace",
                        repo_root=worktree,
                        path=sidecar,
                        git_commit=sha,
                    )
                    now = utc_now()
                    _note_finding_recurrence(conn, stage.id, findings, auditor=role)
                    for finding in findings:
                        fdb.insert_finding(
                            conn,
                            Finding(
                                id=None,
                                stage_id=stage.id,
                                auditor_role=role,
                                finding_ref=finding["ref"],
                                severity=finding.get("severity"),
                                report_artifact_id=ref.id,
                                status="open",
                                contest_artifact_id=None,
                                resolved_by=None,
                                created_at=now,
                                updated_at=now,
                            ),
                        )
            existing_open = fdb.findings(self._db.read(), stage.id, ("open",))

        if not existing_open:
            return self._leave_clean_spec_audit(stage)

        # Spec executor triage of the union of findings (the spec_agent owns the spec).
        await self._run_step_agent(
            stage,
            "spec_agent",
            self._respond_prompt(stage, existing_open, worktree),
            cwd=worktree,
        )
        if await self._apply_thresholds(stage, worktree):
            return False
        response_path = unit_dir / "findings-response.json"
        responses = _read_response_sidecar(
            response_path, [f.finding_ref for f in existing_open]
        )
        sha = await self._commit_unit_paths(
            stage, worktree, [response_path], f"stage {stage.id}: spec audit response"
        )
        # Drop any stray uncommitted writes the triage step left (mirror _step_audit's
        # [20] write-isolation): the response sidecar is committed above, so a SPEC or
        # BUILD re-entry's §3.1 isolation assertion never wedges on triage droppings.
        discarded = await self._discard_uncommitted(worktree)
        contested = [f for f in existing_open if responses[f.finding_ref]["action"] == "contest"]
        complied = [f for f in existing_open if responses[f.finding_ref]["action"] == "comply"]

        def couple_statuses(conn: sqlite3.Connection) -> None:
            response_ref = register_artifact(
                conn,
                unit_level=Level.STAGE.value,
                unit_id=stage.id,
                kind="contest_rationale",
                repo="workspace",
                repo_root=worktree,
                path=response_path,
                git_commit=sha,
            )
            for finding in existing_open:
                action = responses[finding.finding_ref]["action"]
                assert finding.id is not None
                if action == "comply":
                    fdb.set_finding_status(
                        conn, finding.id, "complied", resolved_by="executor"
                    )
                elif action == "duplicate":
                    fdb.set_finding_status(
                        conn, finding.id, "duplicate", resolved_by="executor"
                    )
                else:
                    fdb.set_finding_status(
                        conn,
                        finding.id,
                        "contested",
                        contest_artifact_id=response_ref.id,
                    )
            if contested and not fdb.open_escalation(
                conn, Level.STAGE.value, stage.id, "unresolved_contest"
            ):
                fdb.insert_escalation(
                    conn,
                    Escalation(
                        id=None,
                        unit_level=Level.STAGE.value,
                        unit_id=stage.id,
                        trigger="unresolved_contest",
                        target="phase_architect",
                        payload_artifact_id=response_ref.id,
                        event_seq=None,
                        status="open",
                        resolution=None,
                        created_at=utc_now(),
                        resolved_at=None,
                    ),
                )

        if contested:
            self._sm.transition(
                Level.STAGE,
                stage.id,
                StageState.ESCALATED.value,
                actor=_ACTOR,
                reason="contested spec-audit finding(s) escalate to the phase architect",
                payload={
                    "contested": [f.finding_ref for f in contested],
                    "discarded": discarded,
                },
                coupled=couple_statuses,
            )
            return False
        if complied:
            return await self._spec_audit_rework_or_escalate(
                stage, complied, discarded, couple_statuses
            )
        # Only duplicates: close them and leave SPEC_AUDIT clean -> BUILD.
        with self._db.transaction() as conn:
            couple_statuses(conn)
        return self._leave_clean_spec_audit(stage)

    def _leave_clean_spec_audit(self, stage: Stage) -> bool:
        """Spec findings closed: the spec is clean -> BUILD (unconditional; the §9
        human gate is a POST-BUILD/AUDIT concern, never a spec-review one)."""
        self._sm.transition(
            Level.STAGE,
            stage.id,
            StageState.BUILD.value,
            actor=_ACTOR,
            reason="spec audit clean",
        )
        return True

    async def _spec_audit_rework_or_escalate(
        self,
        stage: Stage,
        complied: list[Finding],
        discarded: list[str],
        couple_statuses: Callable[[sqlite3.Connection], None],
    ) -> bool:
        """Comply path: loop SPEC_AUDIT -> SPEC to rework the spec (BLOCKING),
        unless this stage has already looped spec_audit_max_rework times since its
        last FRESH spec entry — then ESCALATE instead of looping forever (the
        spec-rework loop's bound; mirrors the merge-gate loop-cap, Doctrine §8/§20).
        A 'fresh' spec entry is a SPEC transition NOT coming from SPEC_AUDIT (i.e.
        PENDING->SPEC or ESCALATED->SPEC): loops are counted only after it."""
        with self._db.transaction() as conn:
            # The seq of the last fresh spec entry (a transition INTO SPEC whose
            # from_state is NOT SPEC_AUDIT); loops are SPEC_AUDIT->SPEC after it.
            fresh = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events WHERE unit_level = 'stage'"
                " AND unit_id = ? AND event_type = 'transition' AND to_state = 'SPEC'"
                " AND from_state IS NOT 'SPEC_AUDIT'",
                (stage.id,),
            ).fetchone()[0]
            loops = conn.execute(
                "SELECT COUNT(*) FROM events WHERE unit_level = 'stage'"
                " AND unit_id = ? AND event_type = 'transition' AND to_state = 'SPEC'"
                " AND from_state = 'SPEC_AUDIT' AND seq > ?",
                (stage.id, fresh),
            ).fetchone()[0]
        cap = self._cfg.escalation.spec_audit_max_rework
        complied_refs = [f.finding_ref for f in complied]
        if loops >= cap:
            loop_evidence = {
                "spec_audit_reworks": loops,
                "cap": cap,
                "complied": complied_refs,
            }

            def couple_loop(conn: sqlite3.Connection) -> None:
                couple_statuses(conn)
                seq = fdb.insert_event(
                    conn,
                    unit_level=Level.STAGE.value,
                    unit_id=stage.id,
                    event_type="spec_audit_loop",
                    actor=_ACTOR,
                    payload=loop_evidence,
                )
                if not fdb.open_escalation(
                    conn, Level.STAGE.value, stage.id, "spec_audit_loop"
                ):  # uq_open_escalation: one open row per (stage, trigger)
                    fdb.insert_escalation(
                        conn,
                        Escalation(
                            id=None,
                            unit_level=Level.STAGE.value,
                            unit_id=stage.id,
                            trigger="spec_audit_loop",
                            target="phase_architect",
                            payload_artifact_id=None,
                            event_seq=seq,
                            status="open",
                            resolution=None,
                            created_at=utc_now(),
                            resolved_at=None,
                        ),
                    )

            self._sm.transition(
                Level.STAGE,
                stage.id,
                StageState.ESCALATED.value,
                actor=_ACTOR,
                reason=(
                    f"spec audit reworked the spec {loops}x (cap {cap}) with no clean"
                    " spec — stuck loop, escalated"
                ),
                payload=loop_evidence | {"triggers": ["spec_audit_loop"]},
                coupled=couple_loop,
            )
            return False
        rework = "spec agent complies with spec-audit finding(s) — spec rework"
        self._sm.transition(
            Level.STAGE,
            stage.id,
            StageState.SPEC.value,
            actor=_ACTOR,
            reason=rework,
            payload={
                "complied": complied_refs,
                # CCR-9: carry the WHY into the re-entered spec_agent's prompt.
                "rework_context": rework,
                "discarded": discarded,
            },
            coupled=couple_statuses,
        )
        return True

    async def _step_audit(self, stage: Stage) -> bool:
        """AUDIT: risk-routed auditors in parallel; the executor (Builder role)
        triages the union of findings — comply -> BUILD rework, contest ->
        unresolved-contest escalation, clean -> MERGE_GATE or human gate."""
        worktree = self._worktree(stage)
        unit_dir = self._unit_dir(worktree, stage)
        rc = self._risk_cfg(stage)
        roles = list(rc.audits)
        if not roles:
            raise FactoryError(f"stage {stage.id} entered AUDIT with no audit roles")

        existing_open = fdb.findings(self._db.read(), stage.id, ("open",))
        if not existing_open:
            await asyncio.gather(
                *(
                    self._run_step_agent(
                        stage, role, self._audit_prompt(stage, role, worktree), cwd=worktree
                    )
                    for role in roles
                )
            )
            if await self._apply_thresholds(stage, worktree):
                return False
            to_commit: list[Path] = []
            parsed: list[tuple[str, Path, Path, list[dict]]] = []
            for role in roles:
                report = unit_dir / STAGE_ARTIFACTS["audit_report"].replace("<role>", role)
                sidecar = report.with_suffix(".json")
                if not report.is_file():
                    raise ArtifactContractError(
                        f"auditor {role} produced no report at {report}"
                    )
                findings = _read_findings_sidecar(sidecar, auditor_role=role)
                parsed.append((role, report, sidecar, findings))
                to_commit += [report, sidecar]
            sha = await self._commit_unit_paths(
                stage, worktree, to_commit, f"stage {stage.id}: audit reports"
            )
            with self._db.transaction() as conn:
                for role, report, sidecar, findings in parsed:
                    ref = register_artifact(
                        conn,
                        unit_level=Level.STAGE.value,
                        unit_id=stage.id,
                        kind="audit_report",
                        repo="workspace",
                        repo_root=worktree,
                        path=report,
                        git_commit=sha,
                    )
                    register_artifact(
                        conn,
                        unit_level=Level.STAGE.value,
                        unit_id=stage.id,
                        kind="audit_report",
                        repo="workspace",
                        repo_root=worktree,
                        path=sidecar,
                        git_commit=sha,
                    )
                    now = utc_now()
                    _note_finding_recurrence(conn, stage.id, findings, auditor=role)
                    for finding in findings:
                        fdb.insert_finding(
                            conn,
                            Finding(
                                id=None,
                                stage_id=stage.id,
                                auditor_role=role,
                                finding_ref=finding["ref"],
                                severity=finding.get("severity"),
                                report_artifact_id=ref.id,
                                status="open",
                                contest_artifact_id=None,
                                resolved_by=None,
                                created_at=now,
                                updated_at=now,
                            ),
                        )
            existing_open = fdb.findings(self._db.read(), stage.id, ("open",))

        if not existing_open:
            return await self._leave_clean_audit(stage, worktree)

        # Executor triage of the union of findings (DoD §7).
        builder = _builder_role(self._cfg, stage.risk_class, stage.kind)
        await self._run_step_agent(
            stage,
            builder,
            self._respond_prompt(stage, existing_open, worktree),
            cwd=worktree,
        )
        if await self._apply_thresholds(stage, worktree):
            return False
        response_path = unit_dir / "findings-response.json"
        responses = _read_response_sidecar(
            response_path, [f.finding_ref for f in existing_open]
        )
        sha = await self._commit_unit_paths(
            stage, worktree, [response_path], f"stage {stage.id}: audit response"
        )
        # [20] write-isolation (D-0042): the triage executor may COMPLY yet also
        # scribble stray source edits during the response step; the response
        # sidecar is already committed above, so unconditionally drop any leftover
        # uncommitted writes here — corpse output in every triage outcome — before
        # the comply->BUILD transition's §3.1 isolation assertion would wedge on
        # them. Forensics: record the discarded entries on the payload (mirror the
        # FAILED path ~1524). Stage worktree only (destructive; D-0035 inc-7).
        discarded = await self._discard_uncommitted(worktree)
        contested = [f for f in existing_open if responses[f.finding_ref]["action"] == "contest"]
        complied = [f for f in existing_open if responses[f.finding_ref]["action"] == "comply"]

        def couple_statuses(conn: sqlite3.Connection) -> None:
            response_ref = register_artifact(
                conn,
                unit_level=Level.STAGE.value,
                unit_id=stage.id,
                kind="contest_rationale",
                repo="workspace",
                repo_root=worktree,
                path=response_path,
                git_commit=sha,
            )
            for finding in existing_open:
                action = responses[finding.finding_ref]["action"]
                assert finding.id is not None
                if action == "comply":
                    fdb.set_finding_status(
                        conn, finding.id, "complied", resolved_by="executor"
                    )
                elif action == "duplicate":
                    fdb.set_finding_status(
                        conn, finding.id, "duplicate", resolved_by="executor"
                    )
                else:
                    fdb.set_finding_status(
                        conn,
                        finding.id,
                        "contested",
                        contest_artifact_id=response_ref.id,
                    )
            if contested and not fdb.open_escalation(
                conn, Level.STAGE.value, stage.id, "unresolved_contest"
            ):
                fdb.insert_escalation(
                    conn,
                    Escalation(
                        id=None,
                        unit_level=Level.STAGE.value,
                        unit_id=stage.id,
                        trigger="unresolved_contest",
                        target="phase_architect",
                        payload_artifact_id=response_ref.id,
                        event_seq=None,
                        status="open",
                        resolution=None,
                        created_at=utc_now(),
                        resolved_at=None,
                    ),
                )

        if contested:
            self._sm.transition(
                Level.STAGE,
                stage.id,
                StageState.ESCALATED.value,
                actor=_ACTOR,
                reason="contested audit finding(s) escalate to the phase architect",
                payload={
                    "contested": [f.finding_ref for f in contested],
                    # [20] forensics: stray triage-step writes dropped post-commit.
                    "discarded": discarded,
                },
                coupled=couple_statuses,
            )
            return False
        if complied:
            rework = "executor complies with audit finding(s) — rework"
            self._sm.transition(
                Level.STAGE,
                stage.id,
                StageState.BUILD.value,
                actor=_ACTOR,
                reason=rework,
                payload={
                    "complied": [f.finding_ref for f in complied],
                    # CCR-9: genuine rework re-entry — same sentence as the
                    # transition reason, on the dedicated prompt-context key.
                    "rework_context": rework,
                    # [20] forensics: stray triage-step writes dropped post-commit.
                    "discarded": discarded,
                },
                coupled=couple_statuses,
            )
            return True
        # Only duplicates: close them and leave AUDIT clean.
        with self._db.transaction() as conn:
            couple_statuses(conn)
        return await self._leave_clean_audit(stage, worktree)

    async def _leave_clean_audit(self, stage: Stage, worktree: Path) -> bool:
        """Findings closed: MERGE_GATE, or the §9 human gate for critical stages."""
        rc = self._risk_cfg(stage)
        if not rc.human_gate:
            self._sm.transition(
                Level.STAGE,
                stage.id,
                StageState.MERGE_GATE.value,
                actor=_ACTOR,
                reason="findings closed, no human gate",
            )
            return True
        await self._enter_awaiting_human(
            stage,
            worktree,
            from_reason="findings closed, critical human gate (DoD §9)",
            gate_kind="critical_stage",
        )
        return False

    async def _enter_awaiting_human(
        self, stage: Stage, worktree: Path, *, from_reason: str, gate_kind: str
    ) -> None:
        """Write + register the decision-request artifact, insert the pending
        decision row, transition to AWAITING_HUMAN — then publish (off-tx).

        The artifact is founder-visible verbatim (the dashboard card body), so
        it is authored in Romanian per the founder protocol (dashboard design
        §2a, D-0017): question + glossed unit/gate context + every declared
        option with its one-line consequence + a mechanical ``Recomandare:
        approved`` marker — justified, not fabricated: this gate fires only
        after every validation/audit gate passed (the marker line is the R3
        machine-readable contract ratified with the design)."""
        unit_dir = self._unit_dir(worktree, stage)
        unit_dir.mkdir(parents=True, exist_ok=True)
        path = unit_dir / "decision-request.md"
        path.write_text(
            self._decision_request_body(stage, gate_kind=gate_kind),
            encoding="utf-8",
        )
        # Committed BEFORE the recording tx (§7 fixed step sequence) — an
        # uncommitted request file would later trip the §3.1 BUILD-isolation
        # assertion when the founder routes the stage back to BUILD.
        sha = await self._commit_unit_paths(
            stage, worktree, [path], f"stage {stage.id}: decision request"
        )
        request_id: int | None = None

        def coupled(conn: sqlite3.Connection) -> None:
            nonlocal request_id
            ref = register_artifact(
                conn,
                unit_level=Level.STAGE.value,
                unit_id=stage.id,
                kind="decision_request",
                repo="workspace",
                repo_root=worktree,
                path=path,
                git_commit=sha,
            )
            assert ref.id is not None
            request_id = fdb.insert_decision_request(
                conn,
                DecisionRequest(
                    id=None,
                    unit_level=Level.STAGE.value,
                    unit_id=stage.id,
                    gate_kind=gate_kind,
                    request_artifact_id=ref.id,
                    status="pending",
                    answer=None,
                    answer_artifact_id=None,
                    created_at=utc_now(),
                    alerted_at=None,
                    answered_at=None,
                ),
            )

        self._sm.transition(
            Level.STAGE,
            stage.id,
            StageState.AWAITING_HUMAN.value,
            actor=_ACTOR,
            reason=from_reason,
            payload={"gate_kind": gate_kind},
            coupled=coupled,
        )
        # Notification I/O strictly OUTSIDE the transaction (§7); the row is
        # already pending, so a failed publish is caught by the latency alert.
        await self._publish_decision(request_id, f"Decizie necesară: etapa {stage.name}")

    def _decision_request_body(self, stage: Stage, *, gate_kind: str) -> str:
        """Romanian decision-request template for the stage human gate (§2a).
        The founder protocol binds this content verbatim: no internal English
        text leaks in — the machine reason stays in the transition event."""
        recommendation = ""
        if gate_kind == "critical_stage":
            recommendation = (
                "Recomandare: approved\n"
                "(Recomandare mecanică: toate verificările automate au trecut —"
                " validare și audituri închise; nu a fost exercitată judecată de"
                " produs.)\n\n"
            )
        return (
            f"# Cerere de decizie — Etapa: {stage.name} ({stage.id})\n\n"
            f"Tip poartă: {_ro_glossed(gate_kind)}\n\n"
            "## Întrebare\n"
            f"Aprobi rezultatul etapei „{stage.name}” ({stage.id}) pentru"
            " integrare?\n\n"
            "## Context\n"
            "Toate porțile mecanice au trecut până aici: validarea a trecut,"
            " constatările de audit sunt închise. Etapa este de clasă"
            f" {_ro_glossed(stage.risk_class)} — politica fabricii cere aprobarea"
            " fondatorului înainte de integrare.\n\n"
            "## Opțiuni\n"
            f"{_ro_options_block(Level.STAGE.value, gate_kind)}\n\n"
            f"{recommendation}"
            "## Legături\n"
            f"Artefactele etapei (specificație, rapoarte): directorul"
            f" `_factory/stages/{stage.id}/` pe ramura `{stage.branch}` —"
            " vizibile și în panou, la cardul deciziei.\n\n"
            "Răspunde din panou (butoanele de opțiuni) sau, în caz de urgență,"
            " din terminal cu „cli decide”.\n"
        )

    async def _publish_decision(self, request_id: int | None, title: str) -> None:
        """Publish a decision request (priority_decision). On SUCCESS, stamp
        published_at so the per-tick backstop (_publish_pending_decisions) never
        re-pages a delivered gate. On failure: 'alert_delivery_failed' event, state
        unchanged (§6) — published_at stays NULL so the backstop retries every tick
        until the page lands (founder 20-06: a transient ntfy 429 no longer loses
        the gate until the 24h latency alert)."""
        link = dashboard_link(self._cfg, f"decision/{request_id}")
        try:
            await self._notify.publish(
                title, link=link, priority=self._notify.priority_decision
            )
        except NotifyError as exc:
            with self._db.transaction() as conn:
                fdb.insert_event(
                    conn,
                    unit_level="factory",
                    unit_id=None,
                    event_type="alert_delivery_failed",
                    actor=_ACTOR,
                    payload={"decision_request_id": request_id, "error": str(exc)},
                )
            return
        if request_id is not None:
            with self._db.transaction() as conn:
                fdb.mark_decision_published(conn, request_id, utc_now())

    async def _publish_alert(
        self, title: str, fragment: str, *, context: dict
    ) -> None:
        """Publish a max-priority alert (§8 B7 escalation page); delivery
        failure = 'alert_delivery_failed' event, state unchanged (§6)."""
        try:
            await self._notify.publish(
                title,
                link=dashboard_link(self._cfg, fragment),
                priority=self._notify.priority_alert,
            )
        except NotifyError as exc:
            with self._db.transaction() as conn:
                fdb.insert_event(
                    conn,
                    unit_level="factory",
                    unit_id=None,
                    event_type="alert_delivery_failed",
                    actor=_ACTOR,
                    payload=context | {"error": str(exc)},
                )

    async def _step_awaiting_human(self, stage: Stage) -> bool:
        """AWAITING_HUMAN: consume the latest ANSWERED decision for this stage;
        an unknown answer is reported and leaves the stage blocked (Doctrine §7).
        Accepted answers = models.GATE_ANSWERS[(level, gate_kind)] — the same
        object the dashboard renders as buttons (CCR-3 one source)."""
        decision = _latest_decision(self._db.read(), Level.STAGE.value, stage.id)
        if decision is None or decision.status != "answered" or decision.answer is None:
            return False
        allowed = GATE_ANSWERS.get((Level.STAGE.value, decision.gate_kind), ())
        target = (
            _STAGE_ANSWER_TARGETS.get(decision.answer)
            if decision.answer in allowed
            else None
        )
        if target is None:
            with self._db.transaction() as conn:
                fdb.insert_event(
                    conn,
                    unit_level=Level.STAGE.value,
                    unit_id=stage.id,
                    event_type="alert",
                    actor=_ACTOR,
                    payload={
                        "kind": "unknown_decision_answer",
                        "decision_request_id": decision.id,
                        "answer": decision.answer,
                        "known": list(allowed),
                    },
                )
            return False
        self._sm.transition(
            Level.STAGE,
            stage.id,
            target.value,
            actor="founder",
            reason=f"human gate answered: {decision.answer}",
            payload={"decision_request_id": decision.id, "answer": decision.answer},
        )
        return True

    async def _step_escalated(self, stage: Stage) -> bool:
        """ESCALATED: blocked while any escalation is open; on resolution,
        archive sentinels (§5.4), settle contested findings, route per the
        resolution vocabulary."""
        conn = self._db.read()
        if _open_escalation_count(conn, Level.STAGE.value, stage.id) > 0:
            return False
        last = _latest_resolved_escalation(conn, Level.STAGE.value, stage.id)
        if last is None:
            return False  # escalated without a row = operator surgery; stay put
        # No-action (`settled`) disposition (architect-operations.md §1): the
        # finding is accurate but the behavior is accepted. SPECIAL-CASED here,
        # BEFORE the static STAGE_ESCALATION_RESOLUTIONS lookup, because settling
        # routes forward by RISK (MERGE_GATE non-critical / AWAITING_HUMAN
        # critical) — a token->ONE-state map cannot express that, so `settled` is
        # deliberately kept OUT of the map (mirrors the rework:MERGE_GATE special
        # handling, D-0042). Mark the open escalation's contested findings
        # `settled`, then delegate the forward transition to _leave_clean_audit
        # (the same risk-routed close the audit step uses when findings clear).
        if last.resolution == STAGE_NOACTION_RESOLUTION:
            if stage.worktree_path:
                await self._archive_sentinels(stage, Path(stage.worktree_path), last)
            with self._db.transaction() as tx:
                for finding in fdb.findings(tx, stage.id, ("contested",)):
                    assert finding.id is not None
                    fdb.set_finding_status(
                        tx, finding.id, "settled", resolved_by="phase_architect"
                    )
            # Settle write committed above; route forward and return EARLY,
            # skipping the target-based static-map block below.
            return await self._leave_clean_audit(stage, self._worktree(stage))
        target = STAGE_ESCALATION_RESOLUTIONS.get(last.resolution or "")
        if target is None:
            with self._db.transaction() as tx:
                fdb.insert_event(
                    tx,
                    unit_level=Level.STAGE.value,
                    unit_id=stage.id,
                    event_type="alert",
                    actor=_ACTOR,
                    payload={
                        "kind": "unknown_escalation_resolution",
                        "escalation_id": last.id,
                        "resolution": last.resolution,
                        "known": sorted(STAGE_ESCALATION_RESOLUTIONS),
                    },
                )
            return False
        if stage.worktree_path:
            await self._archive_sentinels(stage, Path(stage.worktree_path), last)

        # §2a/D-0017: the escalation-tradeoff gate gets a minimal ROMANIAN
        # request-wrapper artifact (question + glossed options + /artifact/<ref>
        # link to the payload — linked, never inlined), written + COMMITTED
        # before the recording tx (§7 fixed step order) and registered as the
        # request anchor. No recommendation line: a genuine product trade-off.
        wrapper_path: Path | None = None
        wrapper_sha: str | None = None
        if target is StageState.AWAITING_HUMAN:
            worktree = self._worktree(stage)
            payload_ref = self._escalation_anchor_ref(stage, last)
            unit_dir = self._unit_dir(worktree, stage)
            unit_dir.mkdir(parents=True, exist_ok=True)
            wrapper_path = unit_dir / "decision-request-escalation.md"
            wrapper_path.write_text(
                self._tradeoff_request_body(stage, last, payload_ref),
                encoding="utf-8",
            )
            wrapper_sha = await self._commit_unit_paths(
                stage,
                worktree,
                [wrapper_path],
                f"stage {stage.id}: escalation trade-off decision request",
            )
            if wrapper_sha is None:  # byte-identical replay: pin to HEAD
                code, out, err = await run_git("rev-parse", "HEAD", cwd=worktree)
                if code != 0:
                    raise GitError(
                        f"git rev-parse HEAD failed in {worktree}: {(err or out).strip()}"
                    )
                wrapper_sha = out.strip()

        request_id: int | None = None

        def coupled(tx: sqlite3.Connection) -> None:
            # Contested findings settle with the architect's routing (§5.2
            # Resolve): rework targets sustain the finding; a VALIDATE re-entry
            # means the contest prevailed.
            status = (
                "overruled" if target is StageState.VALIDATE else "sustained"
            )
            if target in (StageState.BUILD, StageState.SPEC, StageState.VALIDATE):
                for finding in fdb.findings(tx, stage.id, ("contested",)):
                    assert finding.id is not None
                    fdb.set_finding_status(
                        tx, finding.id, status, resolved_by="phase_architect"
                    )
            if target is StageState.AWAITING_HUMAN:
                # §9.4 product trade-off: the gate needs a decision request —
                # inserted in the SAME tx as the transition, so the unit can
                # never sit in AWAITING_HUMAN with only a stale answered
                # decision to (mis)consume.
                assert wrapper_path is not None
                ref = register_artifact(
                    tx,
                    unit_level=Level.STAGE.value,
                    unit_id=stage.id,
                    kind="decision_request",
                    repo="workspace",
                    repo_root=self._worktree(stage),
                    path=wrapper_path,
                    git_commit=wrapper_sha,
                )
                assert ref.id is not None
                nonlocal request_id
                request_id = fdb.insert_decision_request(
                    tx,
                    DecisionRequest(
                        id=None,
                        unit_level=Level.STAGE.value,
                        unit_id=stage.id,
                        gate_kind="escalation_tradeoff",
                        request_artifact_id=ref.id,
                        status="pending",
                        answer=None,
                        answer_artifact_id=None,
                        created_at=utc_now(),
                        alerted_at=None,
                        answered_at=None,
                    ),
                )

        assert last.id is not None  # a resolved DB row always carries its id
        # CCR-9: the re-entered role's prompt context is the entry payload's
        # DEDICATED 'rework_context' key — set explicitly at genuine rework
        # re-entries only, never inferred from the generic transition reason
        # (StateMachine.transition merges ``reason`` into EVERY stored payload,
        # so consuming it would caption fresh entries too). The operator's
        # --reason rationale (escalation_resolved event) is preferred; the
        # deterministic 'escalation resolved: <resolution>' string stays as the
        # fallback. The same string doubles as the transition reason, so the
        # event log and dashboard tell the same story.
        reason = _resolution_reason(conn, Level.STAGE.value, stage.id, last.id) or (
            f"escalation resolved: {last.resolution}"
        )
        self._sm.transition(
            Level.STAGE,
            stage.id,
            target.value,
            actor="phase_architect",
            reason=reason,
            payload={
                "escalation_id": last.id,
                "resolution": last.resolution,
                "rework_context": reason,
                # D-0059: documentary path — _step_spec reads this off the SPEC
                # entry payload and skips BUILD (→VALIDATE). Only true for the
                # rework:SPEC_DOC verb; every other resolution flows normally.
                "documentary": last.resolution == STAGE_SPEC_DOC_RESOLUTION,
            },
            coupled=coupled,
        )
        if request_id is not None:
            await self._publish_decision(
                request_id, f"Decizie necesară (escaladare): etapa {stage.name}"
            )
        return True

    def _escalation_anchor_ref(self, stage: Stage, escalation: Escalation) -> int:
        """artifact_refs.id of the escalation payload the wrapper links to
        (payload -> latest escalation_payload -> spec; none = fail-explicit)."""
        conn = self._db.read()
        payload_ref = escalation.payload_artifact_id
        if payload_ref is None:
            latest = fdb.latest_artifact(
                conn, Level.STAGE.value, stage.id, "escalation_payload"
            )
            payload_ref = latest.id if latest else None
        if payload_ref is None:
            spec = fdb.latest_artifact(conn, Level.STAGE.value, stage.id, "spec")
            payload_ref = spec.id if spec else None
        if payload_ref is None:
            raise FactoryError(
                f"stage {stage.id}: no artifact to anchor the escalation"
                " trade-off decision request"
            )
        return payload_ref

    def _tradeoff_request_body(
        self, stage: Stage, escalation: Escalation, payload_ref: int
    ) -> str:
        """Romanian escalation-tradeoff request wrapper (§2a): question + glossed
        options + /artifact/<ref> link; NO recommendation line (genuine
        trade-off — the options' substance lives in the linked payload)."""
        return (
            f"# Cerere de decizie — Etapa: {stage.name} ({stage.id})\n\n"
            f"Tip poartă: {_ro_glossed('escalation_tradeoff')}\n\n"
            "## Întrebare\n"
            f"Etapa „{stage.name}” ({stage.id}) a fost escaladată — mecanismele"
            " automate nu pot decide singure. Cum continuăm?\n\n"
            "## Context\n"
            f"Declanșator: {_ro_glossed(escalation.trigger)} — escaladarea"
            f" #{escalation.id}. Dovezile mecanice sunt în dosarul legat mai"
            " jos — citește-l înainte de a alege.\n\n"
            "## Opțiuni\n"
            f"{_ro_options_block(Level.STAGE.value, 'escalation_tradeoff')}\n\n"
            "## Legături\n"
            f"Dosarul escaladării (dovezi mecanice): /artifact/{payload_ref}\n\n"
            "Răspunde din panou (butoanele de opțiuni) sau, în caz de urgență,"
            " din terminal cu „cli decide”.\n"
        )

    async def _archive_sentinels(
        self, stage: Stage, worktree: Path, escalation: Escalation
    ) -> None:
        """§5.4 sentinel lifecycle: archive (rename + commit) any present
        sentinel BEFORE re-running steps — a stale sentinel must not re-fire,
        while a NEW one written after rework fires again by design."""
        unit_dir = self._unit_dir(worktree, stage)
        renamed = False
        for kind in detect_sentinels(unit_dir):
            src = unit_dir / STAGE_ARTIFACTS[kind]
            dst = src.with_name(f"{src.stem}.resolved-{escalation.id}.md")
            src.rename(dst)
            renamed = True
        if renamed:
            # Commit the whole unit artifact dir: it stages the rename pair even
            # when the sentinel was never tracked (agents write it uncommitted —
            # a vanished untracked path would fail a per-file git add).
            await self._commit_unit_paths(
                stage,
                worktree,
                [unit_dir],
                f"stage {stage.id}: archive resolved sentinel(s)",
            )

    # -------------------------------------------------------------- merge gate

    async def _step_merge_gate(self, stage: Stage) -> bool:
        """MERGE_GATE: out-of-bounds detector at gate ENTRY (phase-seeding design
        §5 — the §10 falsifiability trigger checked by the machine at every
        gate), then Tier 1 (mechanical rebase+suite), then the §3.1 Tier-2
        contract, then the serialized integration merge."""
        await self._oob.check(where="merge_gate")
        phase, project, repo_root, target_branch = self._context(stage)
        worktree = self._worktree(stage)
        test_cmd = _test_cmd(project)
        started_at = utc_now()
        tier1 = await self._wt.tier1_gate(
            worktree, target_branch, test_cmd, self._cfg.process.test_suite_timeout_s
        )
        with self._db.transaction() as conn:
            # §4 tier1_gate contract: the suite is registered as a kind='tests'
            # process (final row — the gate already ran it to completion).
            fdb.insert_process(
                conn,
                ProcessRecord(
                    id=None,
                    unit_level=Level.STAGE.value,
                    unit_id=stage.id,
                    kind="tests",
                    role="test_suite",
                    cp_id=None,
                    session_id=None,
                    pid=None,
                    cmdline=shlex.join(test_cmd),
                    cwd=str(worktree),
                    state="exited",
                    exit_code=1 if tier1.tests_failed else 0,
                    ndjson_log_path=tier1.test_output_path,
                    spawned_at=started_at,
                    heartbeat_at=None,
                    ended_at=utc_now(),
                ),
            )
            fdb.insert_event(
                conn,
                unit_level=Level.STAGE.value,
                unit_id=stage.id,
                event_type="tier1_gate",
                actor=_ACTOR,
                payload={
                    "passed": tier1.passed,
                    "rebase_conflict": tier1.rebase_conflict,
                    "tests_failed": tier1.tests_failed,
                    "test_output_path": tier1.test_output_path,
                },
            )

        if tier1.rebase_conflict:
            # Conflict payload routed back to the owning unit (DoD §5.1).
            unit_dir = self._unit_dir(worktree, stage)
            unit_dir.mkdir(parents=True, exist_ok=True)
            conflict_path = unit_dir / "tier1-conflict.md"
            conflict_path.write_text(tier1.conflict_payload, encoding="utf-8")
            sha = await self._commit_unit_paths(
                stage, worktree, [conflict_path], f"stage {stage.id}: tier1 conflict"
            )
            self._sm.transition(
                Level.STAGE,
                stage.id,
                StageState.BUILD.value,
                actor=_ACTOR,
                reason="Tier-1 rebase conflict routed back",
                payload={"conflict_artifact": str(conflict_path)},
                coupled=lambda conn: register_artifact(
                    conn,
                    unit_level=Level.STAGE.value,
                    unit_id=stage.id,
                    kind="tier1_conflict",
                    repo="workspace",
                    repo_root=worktree,
                    path=conflict_path,
                    git_commit=sha,
                ),
            )
            return True
        # Rebase rewrote history: re-resolve this stage's artifact commits at
        # the new head (§4 tier1_gate caller duty) — mechanical, same path+sha.
        # Runs whenever the rebase succeeded, EVEN when the suite then failed:
        # the pre-rebase commits are already reflog-only, and waiting for a
        # later full pass would leave verify_integrity hostage to reflog/GC
        # timing across the rework loop.
        code, head_out, err = await run_git("rev-parse", "HEAD", cwd=worktree)
        if code != 0:
            raise GitError(f"git rev-parse HEAD failed in {worktree}: {err.strip()}")
        new_head = head_out.strip()
        with self._db.transaction() as conn:
            updated = _reresolve_artifact_commits(
                conn, Level.STAGE.value, stage.id, worktree, new_head
            )
            fdb.insert_event(
                conn,
                unit_level=Level.STAGE.value,
                unit_id=stage.id,
                event_type="artifact_commits_reresolved",
                actor=_ACTOR,
                payload={"new_head": new_head, "updated": updated},
            )

        if tier1.tests_failed:
            # Loop-cap (incident 22-06: treasury-app-foundations looped 12x at the
            # merge gate on a PG-socket-path-too-long Tier-1 failure the builder
            # could NOT fix — a no-op rework loop; env/infra, not a code defect;
            # Doctrine §8/§20, the silent slow death). Count this stage's Tier-1
            # SUITE failures since its last escalation; at the cap, ESCALATE (loud,
            # paged, human-resolved) instead of routing back to BUILD forever.
            with self._db.transaction() as conn:
                since = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM events WHERE unit_level = 'stage'"
                    " AND unit_id = ? AND event_type = 'transition'"
                    " AND to_state = 'ESCALATED'",
                    (stage.id,),
                ).fetchone()[0]
                tier1_failures = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE unit_level = 'stage'"
                    " AND unit_id = ? AND event_type = 'tier1_gate'"
                    " AND json_extract(payload_json, '$.tests_failed') = 1"
                    " AND seq > ?",
                    (stage.id, since),
                ).fetchone()[0]
            cap = self._cfg.escalation.merge_gate_max_tier1_failures
            loop_evidence = {
                "tier1_failures": tier1_failures,
                "cap": cap,
                "test_output_path": tier1.test_output_path,
            }
            if tier1_failures >= cap:

                def couple_loop(conn: sqlite3.Connection) -> None:
                    seq = fdb.insert_event(
                        conn,
                        unit_level=Level.STAGE.value,
                        unit_id=stage.id,
                        event_type="merge_gate_loop",
                        actor=_ACTOR,
                        payload=loop_evidence,
                    )
                    if not fdb.open_escalation(
                        conn, Level.STAGE.value, stage.id, "merge_gate_loop"
                    ):  # uq_open_escalation: one open row per (stage, trigger)
                        fdb.insert_escalation(
                            conn,
                            Escalation(
                                id=None,
                                unit_level=Level.STAGE.value,
                                unit_id=stage.id,
                                trigger="merge_gate_loop",
                                target="phase_architect",
                                payload_artifact_id=None,
                                event_seq=seq,
                                status="open",
                                resolution=None,
                                created_at=utc_now(),
                                resolved_at=None,
                            ),
                        )

                try:
                    self._sm.transition(
                        Level.STAGE,
                        stage.id,
                        StageState.ESCALATED.value,
                        actor=_ACTOR,
                        reason=(
                            f"merge-gate Tier-1 suite failed {tier1_failures}x"
                            f" (cap {cap}) with no fixing rework — stuck loop, escalated"
                        ),
                        payload=loop_evidence | {"triggers": ["merge_gate_loop"]},
                        coupled=couple_loop,
                    )
                    return False
                except TransitionError:
                    pass  # ESCALATED edge illegal here — fall through to route-back
            self._sm.transition(
                Level.STAGE,
                stage.id,
                StageState.BUILD.value,
                actor=_ACTOR,
                reason="Tier-1 test suite failed",
                payload={"test_output_path": tier1.test_output_path},
            )
            return True

        findings = await self._tier2(stage, phase, worktree, repo_root, target_branch)
        if findings is None:
            return False  # a threshold trigger decided during Tier 2 — escalated
        if findings:
            self._sm.transition(
                Level.STAGE,
                stage.id,
                StageState.BUILD.value,
                actor=_ACTOR,
                reason="Tier-2 finding(s) routed back",
                payload={"finding_refs": [f["ref"] for f in findings]},
            )
            return True

        # Integration-revealed fix (wave 4): integrate() merges INSIDE a
        # checkout of the TARGET branch — for a stage that is the phase
        # worktree (worktrees_dir/<phase_id>), never the workspace root,
        # which stays on the project integration branch. Resolve where the
        # target branch is checked out; recreate the (derived, recomputable)
        # phase checkout when a crash removed it — idempotent wt.create
        # attaches the existing branch.
        target_checkout = await _find_branch_checkout(repo_root, target_branch)
        if target_checkout is None:
            target_checkout = await self._wt.create(
                repo_root, phase.id, target_branch, project.integration_branch
            )
        try:
            merge_sha = await self._wt.integrate(
                target_checkout, stage.branch or f"stage/{stage.id}", target_branch
            )
        except StaleGateError:
            with self._db.transaction() as conn:
                fdb.insert_event(
                    conn,
                    unit_level=Level.STAGE.value,
                    unit_id=stage.id,
                    event_type="stale_gate",
                    actor=_ACTOR,
                    payload={"target_branch": target_branch},
                )
            return True  # loop re-enters MERGE_GATE -> re-gate against new HEAD

        def close_findings(conn: sqlite3.Connection) -> None:
            # Previously routed-back Tier-2 findings are now reworked and the
            # gate re-ran clean: mechanical closure (§5.2 Resolve, comply path).
            for finding in fdb.findings(conn, stage.id, ("open",)):
                if finding.auditor_role == "integration_validator":
                    assert finding.id is not None
                    fdb.set_finding_status(
                        conn, finding.id, "complied", resolved_by="executor"
                    )

        self._sm.transition(
            Level.STAGE,
            stage.id,
            StageState.DONE.value,
            actor=_ACTOR,
            reason="Tier 1 + Tier 2 passed, merged",
            payload={"merge_commit": merge_sha},
            coupled=close_findings,
        )
        await self._remove_worktree_after_done(stage, repo_root, worktree)
        return True

    async def _remove_worktree_after_done(
        self, stage: Stage, repo_root: Path, worktree: Path
    ) -> None:
        """Post-DONE cleanup; failure is reported as an event, never an
        exception (the stage IS merged — cleanup must not un-done it)."""
        await _dispose_worktree(
            self._db,
            self._wt,
            repo_root,
            worktree,
            unit_level=Level.STAGE.value,
            unit_id=stage.id,
        )

    async def _tier2(
        self, stage: Stage, phase: Phase, worktree: Path, repo_root: Path, target_branch: str
    ) -> list[dict] | None:
        """§3.1 Tier-2 invocation contract: contracts in force + phase plan +
        FULL diff of the gating unit + full diffs of every sibling merged since
        contract freeze — run in an isolated scratch worktree; findings land in
        audit_findings. Returns None when a threshold trigger (e.g. the
        validator's declared-failure sentinel) escalated the stage mid-gate."""
        max_bytes = self._cfg.process.tier2_max_diff_bytes_per_unit
        contracts = self._collect_dir_texts(worktree / "_factory" / "contracts")
        plan_dir = unit_artifact_dir(worktree, Level.PHASE, phase.id)
        plan_texts = self._collect_dir_texts(plan_dir)
        full_diff = await self._wt.full_diff(worktree, target_branch, max_bytes)
        since_ref = self._contract_freeze_commit(phase)
        sibling_diffs = await self._wt.merged_unit_diffs(
            repo_root, target_branch, since_ref, max_bytes
        )
        sibling_diffs = {uid: d for uid, d in sibling_diffs.items() if uid != stage.id}

        branch = stage.branch or f"stage/{stage.id}"
        scratch = await self._wt.create(
            repo_root, f"{stage.id}-tier2", branch, branch, new_branch=False
        )
        try:
            prompt = self._tier2_prompt(
                stage, contracts, plan_texts, full_diff, sibling_diffs
            )
            await self._run_step_agent(stage, "integration_validator", prompt, cwd=scratch)
            if await self._apply_thresholds(stage, worktree):
                return None  # e.g. validator declared failure mid-gate — escalated
            scratch_dir = self._unit_dir(scratch, stage)
            report = scratch_dir / "integration-report.md"
            sidecar = scratch_dir / "integration-report.json"
            # D-0056: the integration_validator runs with cwd=scratch, but its prompt's
            # FULL DIFF carries the REGULAR worktree's absolute paths, so the agent
            # sometimes writes its report into the regular stage worktree's frozen layout
            # dir instead of its scratch cwd (observed on stock-core-reservation-release
            # ×2; foundation wrote to the scratch). The report is findings-only (no code —
            # the worktree git status shows only the two report files untracked), so
            # accept BOTH locations: prefer the isolated scratch, else fall back to the
            # regular stage worktree dir (== the copy target below, where the agent writes).
            if not sidecar.is_file():
                fb = self._unit_dir(worktree, stage)
                if (fb / "integration-report.json").is_file():
                    scratch_dir, report, sidecar = (
                        fb,
                        fb / "integration-report.md",
                        fb / "integration-report.json",
                    )
            if not sidecar.is_file():
                raise ArtifactContractError(
                    f"integration validator produced no findings sidecar at {sidecar}"
                )
            findings = _read_findings_sidecar(sidecar, auditor_role="integration_validator")
            # Only the report crosses into the stage worktree (§3.1 isolation).
            unit_dir = self._unit_dir(worktree, stage)
            unit_dir.mkdir(parents=True, exist_ok=True)
            copied = [unit_dir / report.name, unit_dir / sidecar.name]
            # When the agent wrote into the stage worktree directly (D-0056 fallback),
            # source == destination — skip the self-copy (shutil raises SameFileError).
            if report.is_file():
                if report.resolve() != copied[0].resolve():
                    shutil.copyfile(report, copied[0])
            else:
                copied[0].write_text("(no prose report)\n", encoding="utf-8")
            if sidecar.resolve() != copied[1].resolve():
                shutil.copyfile(sidecar, copied[1])
            sha = await self._commit_unit_paths(
                stage, worktree, copied, f"stage {stage.id}: tier2 report"
            )
        finally:
            # §5.4: dispose on EVERY exit (escalation + contract-violation
            # included) — a leaked tier2 scratch re-detects its stale sentinel
            # after every later gate run and the stage can never merge again.
            await _dispose_worktree(
                self._db,
                self._wt,
                repo_root,
                scratch,
                unit_level=Level.STAGE.value,
                unit_id=stage.id,
            )
        with self._db.transaction() as conn:
            # Canonical-output registration uniform with _step_audit: prose
            # report AND findings sidecar both register as kind='audit_report'
            # (findings keep referencing the sidecar ref, as before).
            register_artifact(
                conn,
                unit_level=Level.STAGE.value,
                unit_id=stage.id,
                kind="audit_report",
                repo="workspace",
                repo_root=worktree,
                path=copied[0],
                git_commit=sha,
            )
            ref = register_artifact(
                conn,
                unit_level=Level.STAGE.value,
                unit_id=stage.id,
                kind="audit_report",
                repo="workspace",
                repo_root=worktree,
                path=copied[1],
                git_commit=sha,
            )
            now = utc_now()
            _note_finding_recurrence(
                conn, stage.id, findings, auditor="integration_validator"
            )
            for finding in findings:
                fdb.insert_finding(
                    conn,
                    Finding(
                        id=None,
                        stage_id=stage.id,
                        auditor_role="integration_validator",
                        finding_ref=finding["ref"],
                        severity=finding.get("severity"),
                        report_artifact_id=ref.id,
                        status="open",
                        contest_artifact_id=None,
                        resolved_by=None,
                        created_at=now,
                        updated_at=now,
                    ),
                )
            fdb.insert_event(
                conn,
                unit_level=Level.STAGE.value,
                unit_id=stage.id,
                event_type="tier2_gate",
                actor=_ACTOR,
                payload={
                    "findings": [f["ref"] for f in findings],
                    "siblings": sorted(sibling_diffs),
                },
            )
        return findings

    def _contract_freeze_commit(self, phase: Phase) -> str:
        """The contract-freeze commit = the commit that captured the phase plan
        sidecar, read from the PLANNING -> CONTRACTS_FROZEN transition payload
        ('commit'). The events table is the durable anchor: the sidecar's
        artifact_refs.git_commit gets mechanically re-resolved to the rebased
        head after the phase-level Tier-1 rebase, which would silently void
        the §3.1 sibling window for stage gates re-run after an
        AWAITING_SIGNOFF 'changes' loop. The registered sidecar ref stays as
        the fallback for rows frozen before the payload carried the anchor;
        no anchor at all stays fail-loud."""
        payload = _last_transition_payload(
            self._db.read(),
            Level.PHASE.value,
            phase.id,
            PhaseState.CONTRACTS_FROZEN.value,
        )
        commit = payload.get("commit")
        if isinstance(commit, str) and commit:
            return commit
        ref = fdb.latest_artifact(
            self._db.read(), Level.PHASE.value, phase.id, "phase_plan_sidecar"
        )
        if ref is None or not ref.git_commit:
            raise ArtifactContractError(
                f"phase {phase.id} has no committed phase-plan sidecar — the"
                " Tier-2 sibling window (since contract freeze) is undefined"
            )
        return ref.git_commit

    def _collect_dir_texts(self, directory: Path) -> dict[str, str]:
        if not directory.is_dir():
            return {}
        texts: dict[str, str] = {}
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                texts[str(path.relative_to(directory))] = _read_text(
                    path, what=f"file under {directory}"
                )
        return texts

    # ----------------------------------------------------------------- prompts

    def _layout_note(self, stage: Stage) -> str:
        return (
            f"Stage artifact directory (frozen layout): _factory/stages/{stage.id}/ — "
            "if you cannot proceed, write _DECLARED_FAILURE.md there instead of guessing; "
            "if a frozen contract must change, STOP and write _CONTRACT_CHANGE_REQUEST.md."
        )

    def _acceptance_text(self, stage: Stage, phase: Phase, worktree: Path) -> str:
        plan_path = (
            unit_artifact_dir(worktree, Level.PHASE, phase.id)
            / PHASE_ARTIFACTS["phase_plan_sidecar"]
        )
        plan_stage_id = stage.id.removeprefix(f"{phase.id}.")
        if plan_path.is_file():
            try:
                plan = read_phase_plan(plan_path, set(self._cfg.risk_classes))
            except ArtifactContractError:
                return "(phase plan unreadable — see _factory/phases/)"
            for ps in plan.stages:
                if ps.id == plan_stage_id:
                    return ps.acceptance
        return "(acceptance criteria: see the phase plan under _factory/phases/)"

    def _spec_prompt(
        self, stage: Stage, phase: Phase, worktree: Path, entry_payload: dict
    ) -> str:
        unit_rel = f"_factory/stages/{stage.id}"
        context = ""
        # CCR-9: prompts consume ONLY the dedicated 'rework_context' key —
        # never the generic 'reason' the state machine merges into every
        # stored payload (fresh entries must stay context-free).
        rework_context = entry_payload.get("rework_context")
        if rework_context:
            context = f"\nRework context: {rework_context}."
        if entry_payload.get("documentary"):
            context += (
                " This is a DOCUMENTARY amendment (rework:SPEC_DOC): change ONLY the"
                " wording the rework context names so the spec matches the EXISTING"
                " code — do NOT add, remove, or alter any requirement. The code is NOT"
                " rebuilt; VALIDATE + AUDIT re-check the amended spec against the"
                " unchanged code, so a substantive (non-text) change WILL be caught"
                " and bounced back."
            )
        extras = []
        if (Path(worktree) / unit_rel / "validation-report.md").is_file():
            extras.append(f"{unit_rel}/validation-report.md")
        if (Path(worktree) / unit_rel / "escalation-payload.md").is_file():
            extras.append(f"{unit_rel}/escalation-payload.md")
        if (Path(worktree) / unit_rel / "build-notes.md").is_file():
            extras.append(f"{unit_rel}/build-notes.md")
        if extras:
            context += "\nRead first: " + ", ".join(extras) + "."
        return (
            f"You are the Spec Agent for stage '{stage.id}' ({stage.name}), risk class "
            f"{stage.risk_class}, of phase '{phase.id}'.\n"
            f"Acceptance criteria: {self._acceptance_text(stage, phase, worktree)}\n"
            "Contracts in force are read-only under _factory/contracts/.\n"
            "GROUND THE SPEC IN THE EXISTING REPO before writing — a spec that is only "
            "internally consistent but unrealizable against the actual codebase costs "
            "rework rounds: (1) read the toolchain the merge gate runs (the project test "
            "command + its lint/type config) and specify nothing that would fail it "
            "(e.g. unused-symbol pins, banned escape hatches); (2) for anything you "
            "specify — test idioms, file names, patterns, helpers — FIND and FOLLOW the "
            "existing repo convention and cite the example file; (3) never assert an "
            "as-built behavior (a component renders X, an endpoint returns Y) you have "
            "not verified by reading the actual code.\n"
            f"Write the spec to _factory/stages/{stage.id}/spec.md — depth scaled to the "
            f"risk class, test-first.{context}\n" + self._layout_note(stage)
        )

    def _build_prompt(self, stage: Stage, worktree: Path, entry_payload: dict) -> str:
        unit_rel = f"_factory/stages/{stage.id}"
        context = ""
        # CCR-9: dedicated 'rework_context' key only — see _spec_prompt.
        rework_context = entry_payload.get("rework_context")
        if rework_context:
            context = f"\nRework context: {rework_context}."
        extras = []
        if (Path(worktree) / unit_rel / "validation-report.md").is_file():
            extras.append(f"{unit_rel}/validation-report.md")
        if (Path(worktree) / unit_rel / "tier1-conflict.md").is_file():
            extras.append(f"{unit_rel}/tier1-conflict.md")
        if (Path(worktree) / unit_rel / "integration-report.md").is_file():
            extras.append(f"{unit_rel}/integration-report.md")
        if extras:
            context += "\nRead first: " + ", ".join(extras) + "."
        return (
            f"You are the Builder for stage '{stage.id}' ({stage.name}).\n"
            f"Implement EXACTLY the spec at {unit_rel}/spec.md; verify your own work "
            "before finishing (run what you can). Never modify spec.md or any other "
            "_factory/ artifact — the only _factory/ file you may write is "
            f"{unit_rel}/build-notes.md (plus _DECLARED_FAILURE.md / "
            "_CONTRACT_CHANGE_REQUEST.md when you must stop; a needed contract "
            "change = _CONTRACT_CHANGE_REQUEST.md + stop). Never run `git commit` "
            f"yourself — the control plane commits your work.{context}\n"
            + self._layout_note(stage)
        )

    def _validate_prompt(self, stage: Stage, scratch: Path) -> str:
        unit_rel = f"_factory/stages/{stage.id}"
        return (
            f"You are the Validator for stage '{stage.id}' ({stage.name}); clean context.\n"
            f"Derive tests INDEPENDENTLY from the spec at {unit_rel}/spec.md (never from "
            "the implementation), run them here in this isolated checkout, then write:\n"
            f"- {unit_rel}/validation-report.md (human-readable findings)\n"
            f'- {unit_rel}/validation-report.json — EXACTLY {{"failing": N, "passing": N, '
            '"total": N}} (machine-read; malformed = contract violation).\n'
            "Your derived test files stay HERE — they are never copied to the stage "
            "worktree.\n" + self._layout_note(stage)
        )

    def _audit_prompt(self, stage: Stage, role: str, worktree: Path) -> str:
        unit_rel = f"_factory/stages/{stage.id}"
        return (
            f"You are auditor '{role}' for stage '{stage.id}' ({stage.name}, risk class "
            f"{stage.risk_class}).\nAudit the implementation against {unit_rel}/spec.md "
            "and the contracts under _factory/contracts/. Cite concrete locations.\n"
            f"Write {unit_rel}/audit-{role}.md (prose) and {unit_rel}/audit-{role}.json — "
            'EXACTLY {"findings": [{"ref": "<id>", "severity": "...", "summary": "...", '
            '"location": "..."}]} (empty list = clean).\n'
            + self._prior_adjudications_note(stage)
            + self._layout_note(stage)
        )

    def _spec_audit_prompt(self, stage: Stage, role: str, worktree: Path) -> str:
        unit_rel = f"_factory/stages/{stage.id}"
        return (
            f"You are spec auditor '{role}' for stage '{stage.id}' ({stage.name}, risk "
            f"class {stage.risk_class}).\n"
            f"Review the SPECIFICATION at {unit_rel}/spec.md against the frozen contracts "
            "under _factory/contracts/. There is no stage implementation yet — do not "
            "review nonexistent stage code — BUT DO verify the spec is REALIZABLE against "
            "the EXISTING repo: the toolchain/lint-type gate it must pass, the existing "
            "conventions it must follow, and the as-built code it references (flag any "
            "false as-built claim). Look for: internal contradictions; non-conformance to "
            "the frozen contracts; ambiguity; missing or untestable acceptance criteria; "
            "incomplete edge-case coverage (e.g. a 'delete X' spec that never says what "
            "happens to records referencing X); unrealizability against the actual "
            "toolchain/conventions/as-built; and anything that would stop a builder from "
            "implementing it UNAMBIGUOUSLY. Cite concrete locations.\n"
            f"Write {unit_rel}/audit-{role}.md (prose) and {unit_rel}/audit-{role}.json — "
            'EXACTLY {"findings": [{"ref": "<id>", "severity": "...", "summary": "...", '
            '"location": "..."}]} (empty list = clean).\n'
            + self._prior_adjudications_note(stage)
            + self._layout_note(stage)
        )

    def _prior_adjudications_note(self, stage: Stage) -> str:
        """Do-not-re-raise memory for the clean-context auditor: findings already
        permanently closed in a prior round (architect-operations.md §1). SAFETY
        PIN — select ONLY `settled` and `overruled`. NEVER include `sustained`,
        `complied`, or `duplicate`: those may be genuinely unfixed and MUST stay
        re-raisable; suppressing them would silently mask real bugs. A dedicated
        test fails if this set ever widens. Refs only — Finding has no summary
        field (no schema change). Bounded to the 30 most recent by id."""
        rows = self._db.read().execute(
            "SELECT finding_ref, severity, auditor_role FROM audit_findings"
            " WHERE stage_id = ? AND status IN ('settled', 'overruled')"
            " ORDER BY id DESC LIMIT 30",
            (stage.id,),
        ).fetchall()
        if not rows:
            return ""
        listing = "\n".join(
            f"- {r['finding_ref']} (severity {r['severity'] or 'n/a'},"
            f" by {r['auditor_role']})"
            for r in rows
        )
        return (
            "== PREVIOUSLY ADJUDICATED — do NOT re-raise unless the implementation"
            " MATERIALLY CHANGED into a genuinely new defect ==\n"
            f"{listing}\n"
        )

    def _respond_prompt(self, stage: Stage, findings: Sequence[Finding], worktree: Path) -> str:
        unit_rel = f"_factory/stages/{stage.id}"
        listing = "\n".join(
            f"- {f.finding_ref} (by {f.auditor_role}, severity {f.severity or 'n/a'})"
            for f in findings
        )
        return (
            f"You are the stage executor for '{stage.id}'. Triage the union of audit "
            f"findings (deduplicate overlaps):\n{listing}\n"
            f"Reports: {unit_rel}/audit-*.md. For EVERY finding ref above answer in "
            f"{unit_rel}/findings-response.json — EXACTLY "
            '{"responses": [{"ref": "...", "action": "comply|contest|duplicate", '
            '"rationale": "..."}]}. comply = you will rework; contest = reasoned '
            "disagreement (logged, escalates); duplicate = covered by another finding.\n"
            "If a finding restates an observation already permanently closed in a "
            "prior round, answer `duplicate`.\n"
            "Respond ONLY by writing findings-response.json. Do NOT edit code or any "
            "other file — rework happens in the BUILD step after a comply. Never git "
            "commit.\n"
            + self._layout_note(stage)
        )

    def _tier2_prompt(
        self,
        stage: Stage,
        contracts: Mapping[str, str],
        plan_texts: Mapping[str, str],
        full_diff: str,
        sibling_diffs: Mapping[str, str],
    ) -> str:
        max_bytes = self._cfg.process.tier2_max_diff_bytes_per_unit
        pre = [
            f"You are the Integration Validator at the merge gate of stage '{stage.id}' "
            "(clean context). Check contract conformance IN SUBSTANCE, cross-boundary "
            "invariant violations, duplicate/divergent implementations, and assumptions "
            "contradicted between units. Cite concrete locations.",
            "\n== CONTRACTS IN FORCE ==",
        ]
        for name, text in contracts.items():
            pre.append(f"--- {name} ---\n{_bounded(text, max_bytes)}")
        pre.append("\n== PHASE PLAN ==")
        for name, text in plan_texts.items():
            pre.append(f"--- {name} ---\n{_bounded(text, max_bytes)}")
        pre.append(f"\n== FULL DIFF OF GATING UNIT {stage.id} ==\n{full_diff}")
        pre.append("\n== SIBLING DIFFS MERGED SINCE CONTRACT FREEZE ==")
        unit_rel = f"_factory/stages/{stage.id}"
        tail = [
            f"\nWrite {unit_rel}/integration-report.md (prose) and "
            f"{unit_rel}/integration-report.json — EXACTLY "
            '{"findings": [{"ref": "...", "severity": "...", "summary": "...", '
            '"location": "..."}]} (empty list = no findings).\n'
            # D-0048: the integration_validator re-derives findings clean-context
            # every merge-gate run, so a `settled`/`overruled` integration finding
            # (architect-operations §1 no-action disposition) would otherwise
            # regenerate on the re-run gate → BUILD → re-contest → loop. Same
            # do-not-re-raise memory the structural _audit_prompt carries.
            + self._prior_adjudications_note(stage)
            + self._layout_note(stage)
        ]
        fixed_bytes = sum(len(p.encode("utf-8")) for p in (*pre, *tail))
        sib_lines, used_headers = _render_sibling_diffs(
            sibling_diffs,
            fixed_bytes,
            self._cfg.process.tier2_max_total_bytes,
            "(none merged since contract freeze)",
        )
        if used_headers:
            pre.append(_SIBLING_ELISION_NOTE)
        return "\n".join((*pre, *sib_lines, *tail))


# ----------------------------------------------------------------- PhaseExecutor


class PhaseExecutor:
    """Implements UnitExecutor(level=PHASE): plan -> freeze contracts -> fan out
    stages -> integrate (§3.2), with strict phase-plan ingestion."""

    level: Level = Level.PHASE

    def __init__(
        self,
        db: Database,
        sm: StateMachine,
        cfg: FactoryConfig,
        runner: AgentRunner,
        wt: WorktreeManager,
        notify: NtfyPublisher,
        governor: CapacityGovernor | None = None,
    ) -> None:
        """Ingests phase-plan.json strictly via artifacts.read_phase_plan (schema +
        acyclicity validated BEFORE the CONTRACTS_FROZEN→RUNNING transition; failure =
        ArtifactContractError → escalation) into stages+dag_edges. An LLM-produced plan
        is never trusted unvalidated (Doctrine §7). ``governor`` (CCR-11,
        optional): the SHARED capacity governor — see StageExecutor."""
        self._db = db
        self._sm = sm
        self._cfg = cfg
        self._runner = runner
        self._wt = wt
        self._notify = notify
        #: CCR-6: usage-limit scan after the planning spawn.
        self._usage_limit = _UsageLimitDetector(db, cfg, notify)
        #: CCR-11 (D-0037): capacity hold entry + step gate.
        self._governor = governor or CapacityGovernor(db, cfg, runner, notify)

    # ---------------------------------------------------------------- protocol

    async def execute(self, unit_id: str) -> None:
        """Drive one phase until BLOCKED, terminal, or waiting on children."""
        steps = {
            PhaseState.PENDING: self._step_dispatch,
            PhaseState.PLANNING: self._step_planning,
            PhaseState.CONTRACTS_FROZEN: self._step_ingest,
            PhaseState.RUNNING: self._step_running,
            PhaseState.INTEGRATING: self._step_integrating,
            PhaseState.AWAITING_SIGNOFF: self._step_awaiting_signoff,
            PhaseState.AWAITING_HUMAN: self._step_awaiting_human,
            PhaseState.ESCALATED: self._step_escalated,
        }
        while True:
            phase = self._phase(unit_id)
            step = steps.get(phase.state)
            if step is None:
                return
            spawn_roles = self._step_spawn_roles(phase)
            if spawn_roles and EffectiveConfig(
                fdb.get_runtime_settings(self._db.read()), self._cfg
            ).drain_manual:
                # founder manual DRAIN at AGENT granularity (5e correction): same
                # contract as the stage conveyor — this step would spawn a NEW
                # agent, so park here (state untouched, NO event). Earlier steps'
                # agents already finished (execute() awaits sequentially), so the
                # phase winds down to the AGENT boundary, not the phase end.
                # Drain-lift re-dispatches us (see _dispatch drain_lifted). Only
                # read runtime_settings when a step actually spawns.
                return
            if self._governor.held and self._governor.blocks(spawn_roles):
                # CCR-11 (D-0037) capacity hold — same contract as the stage
                # conveyor: a held step does not run this tick, no new state.
                return
            if not await step(phase):
                return

    # ------------------------------------------------------------ shared bits

    def _phase(self, phase_id: str) -> Phase:
        phase = fdb.get_phase(self._db.read(), phase_id)
        if phase is None:
            raise FactoryError(f"unknown phase unit: {phase_id!r}")
        return phase

    def _branch(self, phase: Phase) -> str:
        return phase.branch or f"phase/{phase.id}"

    def _worktree(self, phase: Phase) -> Path:
        """Deterministic phase checkout path (phases carry no worktree column —
        the path is derived, recomputable state, never authoritative)."""
        project = _project_for_phase(self._cfg, phase)
        return Path(project.worktrees_dir) / phase.id

    def _unit_dir(self, root: Path, phase: Phase) -> Path:
        return unit_artifact_dir(root, Level.PHASE, phase.id)

    def _step_spawn_roles(self, phase: Phase) -> tuple[str, ...]:
        """Roles the CURRENT phase step may spawn — the capacity-hold
        predicate (CCR-11/D-0037; the StageExecutor contract). PLANNING spawns
        the Phase Architect; INTEGRATING spawns the phase-level Tier-2
        validator (Tier-1 is mechanical); everything else (dispatch, ingest,
        RUNNING child reactions, gates, ESCALATED) spawns no LLM and is never
        held."""
        if phase.state is PhaseState.PLANNING:
            return ("phase_architect",)
        if phase.state is PhaseState.INTEGRATING:
            return ("integration_validator",)
        return ()

    def spawn_roles(self, unit: object) -> tuple[str, ...]:
        """Public UnitExecutor surface over `_step_spawn_roles` (robustness UNIT
        1): identical contract to the StageExecutor — a phase in ESCALATED has
        the same no-spawn starvation, so it is exempt from the cap too."""
        assert isinstance(unit, Phase), f"PhaseExecutor.spawn_roles got {type(unit)!r}"
        return self._step_spawn_roles(unit)

    def _stage_id(self, phase: Phase, plan_stage_id: str) -> str:
        """Plan-local stage ids are namespaced by phase (stages.id is a global PK)."""
        return f"{phase.id}.{plan_stage_id}"

    def _children(self, phase: Phase) -> list[Stage]:
        stages = fdb.list_units(self._db.read(), Level.STAGE)
        return [s for s in stages if isinstance(s, Stage) and s.phase_id == phase.id]

    def _escalate(
        self,
        phase: Phase,
        *,
        trigger: str,
        target: str,
        reason: str,
        payload: dict,
        event_seq: int | None = None,
        payload_artifact_id: int | None = None,
        transition: bool = True,
    ) -> None:
        """Escalation row (+ ESCALATED transition when legal from the current
        state). When the §3.2 table has no ESCALATED edge (CONTRACTS_FROZEN),
        the row + event still land — visible, paged, never silent."""

        def insert_row(conn: sqlite3.Connection) -> None:
            if not fdb.open_escalation(conn, Level.PHASE.value, phase.id, trigger):
                fdb.insert_escalation(
                    conn,
                    Escalation(
                        id=None,
                        unit_level=Level.PHASE.value,
                        unit_id=phase.id,
                        trigger=trigger,
                        target=target,
                        payload_artifact_id=payload_artifact_id,
                        event_seq=event_seq,
                        status="open",
                        resolution=None,
                        created_at=utc_now(),
                        resolved_at=None,
                    ),
                )

        if transition:
            try:
                self._sm.transition(
                    Level.PHASE,
                    phase.id,
                    PhaseState.ESCALATED.value,
                    actor=_ACTOR,
                    reason=reason,
                    payload=payload | {"trigger": trigger},
                    coupled=insert_row,
                )
                return
            except TransitionError:
                pass  # fall through: record without a state change
        with self._db.transaction() as conn:
            fdb.insert_event(
                conn,
                unit_level=Level.PHASE.value,
                unit_id=phase.id,
                event_type="escalation",
                actor=_ACTOR,
                payload=payload | {"trigger": trigger, "reason": reason},
            )
            insert_row(conn)

    async def _detect_phase_sentinels(
        self, phase: Phase, cwd: Path, *, actor: str = "phase_architect"
    ) -> bool:
        """§5.4 post-conditions at phase level (no ThresholdEvaluator here — it
        is stage-scoped by its frozen signature): sentinel file -> event ->
        escalation to the owning (main) architect. Returns True if escalated.
        ``actor`` attributes the event to the role that wrote the sentinel —
        the tier-2 caller passes 'integration_validator' (§6 audit trail)."""
        unit_dir = self._unit_dir(cwd, phase)
        sentinels = detect_sentinels(unit_dir)
        if not sentinels:
            return False
        for kind in sentinels:
            event_type, trigger = _SENTINEL_EVENTS[kind]
            with self._db.transaction() as conn:
                seq = fdb.insert_event(
                    conn,
                    unit_level=Level.PHASE.value,
                    unit_id=phase.id,
                    event_type=event_type,
                    actor=actor,
                    payload={"sentinel": str(unit_dir / STAGE_ARTIFACTS[kind])},
                )
            self._escalate(
                phase,
                trigger=trigger,
                target="main_architect",
                reason=f"phase-level sentinel: {kind}",
                payload={"sentinel": kind},
                event_seq=seq,
            )
        return True

    # ------------------------------------------------------------------- steps

    async def _step_dispatch(self, phase: Phase) -> bool:
        """PENDING -> PLANNING: phase integration branch + checkout off the
        project integration branch."""
        project = _project_for_phase(self._cfg, phase)
        branch = self._branch(phase)
        path = await self._wt.create(
            Path(project.workspace), phase.id, branch, project.integration_branch
        )
        self._sm.transition(
            Level.PHASE,
            phase.id,
            PhaseState.PLANNING.value,
            actor=_ACTOR,
            reason="DAG deps DONE, dispatched",
            payload={"branch": branch, "worktree": str(path)},
        )
        return True

    async def _step_planning(self, phase: Phase) -> bool:
        """PLANNING: Phase Architect produces phase-plan.md/.json + contracts;
        the plan is validated BEFORE freezing (a defective plan escalates from
        PLANNING — CONTRACTS_FROZEN has no ESCALATED edge in §3.2)."""
        worktree = self._worktree(phase)
        # Option A (founder-ratified pre-authored plans): when the project pins a
        # prefrozen_phase_plans dir AND this phase has a plan there, ADOPT it
        # byte-exactly into the worktree before the spawn, and narrow the Phase
        # Architect to authoring contracts only. The mechanical guarantee (re-
        # asserted after the spawn): the ingested stage structure is exactly the
        # founder-RATIFIED one — the architect may not regenerate/edit/move it.
        proj = _project_for_phase(self._cfg, phase)
        prefrozen = proj.prefrozen_phase_plans
        adopted = False
        frozen_sha: str | None = None
        if prefrozen is not None:
            src_dir = _resolve(self._cfg.factory.home, prefrozen) / phase.id
            src_json = src_dir / PHASE_ARTIFACTS["phase_plan_sidecar"]
            src_md = src_dir / PHASE_ARTIFACTS["phase_plan"]
            if src_json.is_file():
                if not src_md.is_file():
                    self._escalate(
                        phase,
                        trigger="artifact_contract",
                        target="main_architect",
                        reason="prefrozen plan missing .md companion",
                        payload={"src_json": str(src_json), "src_md": str(src_md)},
                    )
                    return False
                unit_dir = self._unit_dir(worktree, phase)
                unit_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src_json, unit_dir / PHASE_ARTIFACTS["phase_plan_sidecar"])
                shutil.copyfile(src_md, unit_dir / PHASE_ARTIFACTS["phase_plan"])
                frozen_sha = sha256_file(unit_dir / PHASE_ARTIFACTS["phase_plan_sidecar"])
                adopted = True
        result = await self._runner.run_agent(
            "phase_architect",
            self._planning_prompt(phase, prefrozen=adopted),
            unit_level=Level.PHASE.value,
            unit_id=phase.id,
            cwd=worktree,
        )
        # CCR-6: capacity-event scan right after the spawn (same contract as
        # the stage conveyor's _run_step_agent).
        matched = await self._usage_limit.check(
            result, unit_level=Level.PHASE.value, unit_id=phase.id, role="phase_architect"
        )
        if matched is not None:
            # CCR-11 (D-0037): mechanical HOLD entry from the planning spawn —
            # phase-level escalations stay OUT of the auto-resolve scope (the
            # incident-7 gate does not cover phase spawns yet, D-0036), but
            # the hold itself is factory-wide.
            self._governor.note_match(
                signature=matched,
                role="phase_architect",
                process_id=result.process_id,
            )
        if await self._detect_phase_sentinels(phase, worktree):
            return False
        unit_dir = self._unit_dir(worktree, phase)
        if adopted:
            # THE mechanical guarantee (Option A): the stage structure that gets
            # ingested is byte-exactly the founder-RATIFIED one. The narrowed
            # architect was told not to touch the frozen plan — verify it didn't.
            after_sha = sha256_file(unit_dir / PHASE_ARTIFACTS["phase_plan_sidecar"])
            if after_sha != frozen_sha:
                self._escalate(
                    phase,
                    trigger="prefrozen_plan_modified",
                    target="main_architect",
                    reason="phase_architect modified the frozen pre-authored phase-plan",
                    payload={
                        "phase_plan_sidecar": str(
                            unit_dir / PHASE_ARTIFACTS["phase_plan_sidecar"]
                        ),
                        "frozen_sha256": frozen_sha,
                        "observed_sha256": after_sha,
                    },
                )
                with self._db.transaction() as conn:
                    fdb.insert_event(
                        conn,
                        unit_level=Level.PHASE.value,
                        unit_id=phase.id,
                        event_type="prefrozen_plan_modified",
                        actor=_ACTOR,
                        payload={
                            "frozen_sha256": frozen_sha,
                            "observed_sha256": after_sha,
                        },
                    )
                return False
        plan_md = unit_dir / PHASE_ARTIFACTS["phase_plan"]
        plan_json = unit_dir / PHASE_ARTIFACTS["phase_plan_sidecar"]
        try:
            read_phase_plan(plan_json, set(self._cfg.risk_classes))
            if not plan_md.is_file():
                raise ArtifactContractError(
                    f"phase architect produced no {plan_md} for phase {phase.id}"
                )
        except ArtifactContractError as exc:
            with self._db.transaction() as conn:
                fdb.insert_event(
                    conn,
                    unit_level=Level.PHASE.value,
                    unit_id=phase.id,
                    event_type="artifact_contract_violation",
                    actor=_ACTOR,
                    payload={"error": str(exc)},
                )
            self._escalate(
                phase,
                trigger="artifact_contract",
                target="main_architect",
                reason="phase plan failed strict validation",
                payload={"error": str(exc)},
            )
            return False

        contracts_dir = Path(worktree) / "_factory" / "contracts"
        contract_paths = (
            sorted(p for p in contracts_dir.rglob("*") if p.is_file())
            if contracts_dir.is_dir()
            else []
        )
        sha = await commit_paths(
            worktree,
            [plan_md, plan_json, *contract_paths],
            f"phase {phase.id}: plan + frozen contracts",
            trailers={"Factory-Unit": f"phase/{phase.id}"},
        )
        if sha is None:
            # §5.5d at-least-once replay: a crash between this commit and the
            # CONTRACTS_FROZEN tx re-runs the step, and a byte-identical plan
            # leaves nothing to commit — but the freeze anchor (payload
            # ['commit'], read by _contract_freeze_commit at every stage
            # MERGE_GATE) must never be None: pin it to the current HEAD,
            # the same pattern as cli._commit_decision_answer.
            code, out, err = await run_git("rev-parse", "HEAD", cwd=worktree)
            if code != 0:
                raise GitError(
                    f"git rev-parse HEAD failed in {worktree}: {(err or out).strip()}"
                )
            sha = out.strip()

        def coupled(conn: sqlite3.Connection) -> None:
            register_artifact(
                conn,
                unit_level=Level.PHASE.value,
                unit_id=phase.id,
                kind="phase_plan",
                repo="workspace",
                repo_root=worktree,
                path=plan_md,
                git_commit=sha,
            )
            register_artifact(
                conn,
                unit_level=Level.PHASE.value,
                unit_id=phase.id,
                kind="phase_plan_sidecar",
                repo="workspace",
                repo_root=worktree,
                path=plan_json,
                git_commit=sha,
            )
            for contract in contract_paths:
                register_artifact(
                    conn,
                    unit_level=Level.PHASE.value,
                    unit_id=phase.id,
                    kind="contract",
                    repo="workspace",
                    repo_root=worktree,
                    path=contract,
                    git_commit=sha,
                )

        self._sm.transition(
            Level.PHASE,
            phase.id,
            PhaseState.CONTRACTS_FROZEN.value,
            actor=_ACTOR,
            reason="plan + contracts registered & committed",
            # payload['commit'] is the DURABLE §3.1 freeze anchor:
            # _contract_freeze_commit reads it from this append-only event —
            # the sidecar's artifact_refs.git_commit gets re-resolved after
            # the phase-level rebase and cannot anchor the sibling window.
            payload={"contracts": len(contract_paths), "commit": sha},
            coupled=coupled,
        )
        return True

    def _apply_stage_size_gate(self, phase: Phase, plan: PhasePlan) -> bool:
        """Integration safety net (step-5): run the mechanical small-stage size gate
        over the validated plan and surface its findings. Returns True when ingest may
        proceed, False only in 'hard' mode WITH an over/under finding (blocks).

        - over/under: 'warn' (default) -> an ``oversized_stage`` event (stage_id, axis,
          value, limit, kind) PLUS a non-blocking escalation to the architect (the
          ``transition=False`` pattern) — ingest still proceeds; 'hard' -> the same
          event + a blocking escalation and ingest is refused (no state change).
        - skipped: a ``size_gate_skipped`` event per un-checkable axis (VISIBLE, never
          an escalation) so a legacy plan's coverage gap is recorded, not silent.
        The no-violation path emits nothing — byte-identical to pre-step-5."""
        limits_cfg = self._cfg.planning.stage_size_limits
        violations = evaluate_stage_sizes(
            plan,
            StageSizeLimits(
                max_acceptance_criteria=limits_cfg.max_acceptance_criteria,
                max_touched=limits_cfg.max_touched,
                max_dependency_degree=limits_cfg.max_dependency_degree,
                min_acceptance_criteria=limits_cfg.min_acceptance_criteria,
                min_touched=limits_cfg.min_touched,
            ),
        )
        if not violations:
            return True

        mode = self._cfg.planning.stage_size_gate_mode
        flagged = [v for v in violations if v.kind != "skipped"]
        skipped = [v for v in violations if v.kind == "skipped"]

        for v in skipped:
            with self._db.transaction() as conn:
                fdb.insert_event(
                    conn,
                    unit_level=Level.PHASE.value,
                    unit_id=phase.id,
                    event_type="size_gate_skipped",
                    actor=_ACTOR,
                    payload={"stage_id": v.stage_id, "axis": v.axis},
                )

        for v in flagged:
            with self._db.transaction() as conn:
                fdb.insert_event(
                    conn,
                    unit_level=Level.PHASE.value,
                    unit_id=phase.id,
                    event_type="oversized_stage",
                    actor=_ACTOR,
                    payload={
                        "stage_id": v.stage_id,
                        "axis": v.axis,
                        "kind": v.kind,
                        "value": v.value,
                        "limit": v.limit,
                        "mode": mode,
                    },
                )

        if flagged:
            offenders = sorted({v.stage_id for v in flagged})
            self._escalate(
                phase,
                trigger="stage_size_gate",
                target="main_architect",
                reason=f"phase plan stage(s) {offenders} violate the size limits ({mode})",
                payload={
                    "mode": mode,
                    "violations": [
                        {
                            "stage_id": v.stage_id,
                            "axis": v.axis,
                            "kind": v.kind,
                            "value": v.value,
                            "limit": v.limit,
                        }
                        for v in flagged
                    ],
                },
                transition=False,
            )
            if mode == "hard":
                return False  # block ingest: a hard-mode size violation, no state change
        return True

    async def _step_ingest(self, phase: Phase) -> bool:
        """CONTRACTS_FROZEN -> RUNNING: re-validate the plan strictly, then one
        tx inserts stage rows + stage DAG + the transition (all-or-nothing)."""
        worktree = self._worktree(phase)
        plan_path = self._unit_dir(worktree, phase) / PHASE_ARTIFACTS["phase_plan_sidecar"]
        try:
            plan = read_phase_plan(plan_path, set(self._cfg.risk_classes))
        except ArtifactContractError as exc:
            # No ESCALATED edge from CONTRACTS_FROZEN (§3.2): record the breach
            # + open escalation without a state change — paged, never silent.
            self._escalate(
                phase,
                trigger="artifact_contract",
                target="main_architect",
                reason="frozen phase plan failed re-validation at ingestion",
                payload={"error": str(exc)},
                transition=False,
            )
            return False

        # Integration safety net (step-5): mechanical small-stage size gate. In
        # 'warn' (the default) it REPORTS + escalates non-blocking, ingest proceeds;
        # in 'hard' it blocks (escalate without a state change, like the contract
        # breach above). The happy path (no violations) is byte-identical to before.
        if not self._apply_stage_size_gate(phase, plan):
            return False

        now = utc_now()

        def coupled(conn: sqlite3.Connection) -> None:
            for ps in plan.stages:
                sid = self._stage_id(phase, ps.id)
                if fdb.get_stage(conn, sid) is None:  # replan: keep prior rows
                    fdb.insert_stage(
                        conn,
                        Stage(
                            id=sid,
                            phase_id=phase.id,
                            name=ps.name,
                            risk_class=ps.risk_class,
                            state=StageState.PENDING,
                            branch=f"stage/{sid}",
                            worktree_path=None,
                            spec_artifact_id=None,
                            created_at=now,
                            updated_at=now,
                            kind=ps.kind,
                        ),
                    )
            for from_id, to_id in plan.dag_edges:
                f, t = self._stage_id(phase, from_id), self._stage_id(phase, to_id)
                if not _dag_edge_exists(conn, Level.STAGE, f, t):
                    fdb.insert_dag_edge(conn, Level.STAGE, f, t)

        self._sm.transition(
            Level.PHASE,
            phase.id,
            PhaseState.RUNNING.value,
            actor=_ACTOR,
            reason="phase plan validated; stages + DAG ingested",
            payload={"stages": [self._stage_id(phase, s.id) for s in plan.stages]},
            coupled=coupled,
        )
        return True

    async def _step_running(self, phase: Phase) -> bool:
        """RUNNING: react to children. A FAILED child (or CANCELLED without a
        registered replacement) escalates — a failed child must never wedge the
        phase in RUNNING forever (§3.2); all children TERMINAL_OK (or replaced)
        -> INTEGRATING; otherwise wait."""
        children = self._children(phase)
        if not children:
            return False  # plan ingested no stages yet — nothing to react to
        failed = [s.id for s in children if s.state is StageState.FAILED]
        cancelled = [s.id for s in children if s.state is StageState.CANCELLED]
        unreplaced = [
            sid for sid in cancelled if not self._replacement_registered(sid)
        ]
        if failed or unreplaced:
            self._escalate(
                phase,
                trigger="child_failed",
                target="phase_architect",
                reason="child stage(s) FAILED or CANCELLED without replacement",
                payload={"failed": failed, "cancelled_unreplaced": unreplaced},
            )
            return False
        if all(
            s.state is StageState.DONE or s.id in cancelled for s in children
        ):
            self._sm.transition(
                Level.PHASE,
                phase.id,
                PhaseState.INTEGRATING.value,
                actor=_ACTOR,
                reason="no child stage outside TERMINAL_OK",
                payload={"children": [s.id for s in children]},
            )
            return True
        return False

    def _replacement_registered(self, stage_id: str) -> bool:
        """A CANCELLED child blocks integration unless a replacement stage was
        registered against it (event 'replacement_registered' with the new id)."""
        return (
            _last_event_seq_of_type(self._db.read(), stage_id, "replacement_registered")
            > 0
        )

    async def _step_integrating(self, phase: Phase) -> bool:
        """INTEGRATING: the same Tier-1 + Tier-2 gates at phase integration
        (DoD §3.2 'same merge gates'); pass -> sign-off decision request."""
        project = _project_for_phase(self._cfg, phase)
        repo_root = Path(project.workspace)
        worktree = self._worktree(phase)
        target = project.integration_branch
        try:
            test_cmd = _test_cmd(project)
        except ConfigError as exc:
            self._escalate(
                phase,
                trigger="internal_error",
                target="main_architect",
                reason="phase merge gate cannot run (OPEN-2)",
                payload={"error": str(exc)},
            )
            return False
        # §3.1 sibling-window anchor, read BEFORE the Tier-1 rebase moves it:
        # after a successful rebase the fork point IS the target head, so a
        # post-rebase merge-base would return an empty window — every sibling
        # merged into the target pre-gate would vanish from the Tier-2 inputs
        # (exactly the DoD §5.3 structurally-uncatchable failure mode).
        code, base_out, err = await run_git(
            "merge-base", self._branch(phase), target, cwd=repo_root
        )
        if code != 0:
            raise GitError(
                f"git merge-base failed in {repo_root}: {(err or base_out).strip()}"
            )
        sibling_since = base_out.strip()
        started_at = utc_now()
        tier1 = await self._wt.tier1_gate(
            worktree, target, test_cmd, self._cfg.process.test_suite_timeout_s
        )
        with self._db.transaction() as conn:
            fdb.insert_process(
                conn,
                ProcessRecord(
                    id=None,
                    unit_level=Level.PHASE.value,
                    unit_id=phase.id,
                    kind="tests",
                    role="test_suite",
                    cp_id=None,
                    session_id=None,
                    pid=None,
                    cmdline=shlex.join(test_cmd),
                    cwd=str(worktree),
                    state="exited",
                    exit_code=1 if tier1.tests_failed else 0,
                    ndjson_log_path=tier1.test_output_path,
                    spawned_at=started_at,
                    heartbeat_at=None,
                    ended_at=utc_now(),
                ),
            )
            fdb.insert_event(
                conn,
                unit_level=Level.PHASE.value,
                unit_id=phase.id,
                event_type="tier1_gate",
                actor=_ACTOR,
                payload={
                    "passed": tier1.passed,
                    "rebase_conflict": tier1.rebase_conflict,
                    "tests_failed": tier1.tests_failed,
                    "test_output_path": tier1.test_output_path,
                },
            )
        if tier1.rebase_conflict:
            # Conflict payload = committed phase artifact (DoD §8) — never
            # inlined into events.payload_json (§2: small facts only); same
            # pattern as the stage merge gate's tier1-conflict.md.
            unit_dir = self._unit_dir(worktree, phase)
            unit_dir.mkdir(parents=True, exist_ok=True)
            conflict_path = unit_dir / "tier1-conflict.md"
            conflict_path.write_text(tier1.conflict_payload, encoding="utf-8")
            sha = await commit_paths(
                worktree,
                [conflict_path],
                f"phase {phase.id}: tier1 conflict",
                trailers={"Factory-Unit": f"phase/{phase.id}"},
            )
            with self._db.transaction() as conn:
                ref = register_artifact(
                    conn,
                    unit_level=Level.PHASE.value,
                    unit_id=phase.id,
                    kind="tier1_conflict",
                    repo="workspace",
                    repo_root=worktree,
                    path=conflict_path,
                    git_commit=sha,
                )
            self._escalate(
                phase,
                trigger="integration_conflict",
                target="main_architect",
                reason="phase Tier-1 gate failed",
                payload={
                    "rebase_conflict": True,
                    "tests_failed": tier1.tests_failed,
                    "conflict_artifact": str(conflict_path),
                    "test_output_path": tier1.test_output_path,
                },
                payload_artifact_id=ref.id,
            )
            return False

        # Rebase rewrote history: re-resolve at the new head even when the
        # suite then failed (§4 tier1_gate caller duty — the pre-rebase
        # commits are already reflog-only; same rule as the stage gate).
        code, head_out, err = await run_git("rev-parse", "HEAD", cwd=worktree)
        if code != 0:
            raise GitError(f"git rev-parse HEAD failed in {worktree}: {err.strip()}")
        with self._db.transaction() as conn:
            _reresolve_artifact_commits(
                conn, Level.PHASE.value, phase.id, worktree, head_out.strip()
            )

        if tier1.tests_failed:
            self._escalate(
                phase,
                trigger="integration_conflict",
                target="main_architect",
                reason="phase Tier-1 gate failed",
                payload={
                    "rebase_conflict": False,
                    "tests_failed": True,
                    "test_output_path": tier1.test_output_path,
                },
            )
            return False

        findings = await self._tier2(
            phase, project, worktree, repo_root, target, sibling_since
        )
        if findings is None:
            return False  # validator declared failure — already escalated
        if findings:
            self._escalate(
                phase,
                trigger="semantic_conflict",
                target="main_architect",
                reason="phase Tier-2 finding(s) — architect routes stage rework",
                payload={"finding_refs": [f["ref"] for f in findings]},
            )
            return False

        await self._enter_signoff(phase, worktree)
        return False  # AWAITING_SIGNOFF is BLOCKED — nothing further to drive

    async def _tier2(
        self,
        phase: Phase,
        project: ProjectCfg,
        worktree: Path,
        repo_root: Path,
        target: str,
        since_ref: str,
    ) -> list[dict] | None:
        """Phase-level §3.1 Tier-2 contract on the same code path. The sibling
        window opens at this phase's fork point from the integration branch —
        ``since_ref``, the merge-base the CALLER captured BEFORE the Tier-1
        rebase moved it (cross-phase contract freeze is a planning artifact in
        MVP — DoD §3.2 scopes phase-level execution as first production use)."""
        max_bytes = self._cfg.process.tier2_max_diff_bytes_per_unit
        contracts = {}
        contracts_dir = Path(worktree) / "_factory" / "contracts"
        if contracts_dir.is_dir():
            contracts = {
                str(p.relative_to(contracts_dir)): _read_text(p, what="contract")
                for p in sorted(contracts_dir.rglob("*"))
                if p.is_file()
            }
        plan_dir = self._unit_dir(worktree, phase)
        plan_texts = {
            p.name: _read_text(p, what="phase plan")
            for p in sorted(plan_dir.glob("*"))
            if p.is_file()
        }
        full_diff = await self._wt.full_diff(worktree, target, max_bytes)
        sibling_diffs = await self._wt.merged_unit_diffs(
            repo_root, target, since_ref, max_bytes
        )
        sibling_diffs = {uid: d for uid, d in sibling_diffs.items() if uid != phase.id}

        pre = [
            f"You are the Integration Validator at the integration gate of phase"
            f" '{phase.id}' (clean context). Check contract conformance in substance,"
            " cross-boundary invariants, duplicate/divergent implementations,"
            " contradicted assumptions. Cite concrete locations.",
            "\n== CONTRACTS IN FORCE ==",
            *(f"--- {n} ---\n{_bounded(t, max_bytes)}" for n, t in contracts.items()),
            "\n== PHASE PLAN ==",
            *(f"--- {n} ---\n{_bounded(t, max_bytes)}" for n, t in plan_texts.items()),
            f"\n== FULL DIFF OF PHASE {phase.id} vs {target} ==\n{full_diff}",
            "\n== UNIT DIFFS MERGED INTO THE TARGET SINCE FORK ==",
        ]
        unit_rel = f"_factory/phases/{phase.id}"
        tail = [
            f"\nWrite {unit_rel}/integration-report.md and {unit_rel}/integration-report.json"
            ' — EXACTLY {"findings": [{"ref": "...", "severity": "...", "summary": "...",'
            ' "location": "..."}]} (empty list = no findings). If you cannot proceed,'
            f" write {unit_rel}/_DECLARED_FAILURE.md instead of guessing."
        ]
        fixed_bytes = sum(len(p.encode("utf-8")) for p in (*pre, *tail))
        sib_lines, used_headers = _render_sibling_diffs(
            sibling_diffs, fixed_bytes, self._cfg.process.tier2_max_total_bytes, "(none)"
        )
        if used_headers:
            pre.append(_SIBLING_ELISION_NOTE)
        parts = [*pre, *sib_lines, *tail]

        branch = self._branch(phase)
        scratch = await self._wt.create(
            repo_root, f"{phase.id}-tier2", branch, branch, new_branch=False
        )
        try:
            await self._runner.run_agent(
                "integration_validator",
                "\n".join(parts),
                unit_level=Level.PHASE.value,
                unit_id=phase.id,
                cwd=scratch,
            )
            if await self._detect_phase_sentinels(
                phase, scratch, actor="integration_validator"
            ):
                return None  # sentinel path already escalated — no second escalation
            scratch_dir = self._unit_dir(scratch, phase)
            sidecar_src = scratch_dir / "integration-report.json"
            if not sidecar_src.is_file():
                raise ArtifactContractError(
                    f"integration validator produced no findings sidecar at {sidecar_src}"
                )
            findings = _read_findings_sidecar(
                sidecar_src, auditor_role="integration_validator"
            )
            unit_dir = self._unit_dir(worktree, phase)
            unit_dir.mkdir(parents=True, exist_ok=True)
            report_src = scratch_dir / "integration-report.md"
            report_dst = unit_dir / "integration-report.md"
            sidecar_dst = unit_dir / "integration-report.json"
            if report_src.is_file():
                shutil.copyfile(report_src, report_dst)
            else:
                report_dst.write_text("(no prose report)\n", encoding="utf-8")
            shutil.copyfile(sidecar_src, sidecar_dst)
            sha = await commit_paths(
                worktree,
                [report_dst, sidecar_dst],
                f"phase {phase.id}: tier2 report",
                trailers={"Factory-Unit": f"phase/{phase.id}"},
            )
        finally:
            # §5.4: dispose on EVERY exit (sentinel + contract-violation
            # included) — a leaked phase tier2 scratch re-detects its stale
            # sentinel at every later integration gate run.
            await _dispose_worktree(
                self._db,
                self._wt,
                repo_root,
                scratch,
                unit_level=Level.PHASE.value,
                unit_id=phase.id,
            )
        with self._db.transaction() as conn:
            # Same canonical-output treatment as the stage gates (_step_audit /
            # stage Tier-2): both reports register as phase-level audit_report
            # refs — verify_integrity guards them and findings stay event-only
            # (audit_findings.stage_id is NOT NULL by design).
            for path in (report_dst, sidecar_dst):
                register_artifact(
                    conn,
                    unit_level=Level.PHASE.value,
                    unit_id=phase.id,
                    kind="audit_report",
                    repo="workspace",
                    repo_root=worktree,
                    path=path,
                    git_commit=sha,
                )
            fdb.insert_event(
                conn,
                unit_level=Level.PHASE.value,
                unit_id=phase.id,
                event_type="tier2_gate",
                actor=_ACTOR,
                payload={
                    "findings": [f["ref"] for f in findings],
                    "siblings": sorted(sibling_diffs),
                },
            )
        return findings

    async def _enter_signoff(self, phase: Phase, worktree: Path) -> None:
        """Phase merge gates passed -> AWAITING_SIGNOFF + decision request
        (gate_kind='phase_signoff', DoD §9.3) + founder push.

        The artifact is founder-visible verbatim (dashboard card body) — authored
        in Romanian per the founder protocol (§2a, D-0017), with the mechanical
        ``Recomandare: approved`` marker (R3): this gate fires only after every
        stage finished and both phase merge gates passed."""
        unit_dir = self._unit_dir(worktree, phase)
        unit_dir.mkdir(parents=True, exist_ok=True)
        path = unit_dir / "signoff-request.md"
        path.write_text(
            f"# Cerere de decizie — Faza: {phase.name} ({phase.id})\n\n"
            f"Tip poartă: {_ro_glossed('phase_signoff')}\n\n"
            "## Întrebare\n"
            f"Închizi faza „{phase.name}” ({phase.id})? Toate etapele sunt gata"
            " și porțile de integrare ale fazei au trecut.\n\n"
            "## Context\n"
            "Toate etapele fazei sunt finalizate; porțile mecanice de integrare"
            " (rebazare + toată suita de teste, plus verificarea semantică"
            " încrucișată) au trecut.\n\n"
            "## Opțiuni\n"
            f"{_ro_options_block(Level.PHASE.value, 'phase_signoff')}\n\n"
            "Recomandare: approved\n"
            "(Recomandare mecanică: toate porțile automate au trecut; nu a fost"
            " exercitată judecată de produs.)\n\n"
            "## Legături\n"
            f"Planul și rapoartele fazei: directorul `_factory/phases/{phase.id}/`"
            f" pe ramura `{self._branch(phase)}` — vizibile și în panou, la"
            " cardul deciziei.\n\n"
            "Răspunde din panou (butoanele de opțiuni) sau, în caz de urgență,"
            " din terminal cu „cli decide”.\n",
            encoding="utf-8",
        )
        sha = await commit_paths(
            worktree,
            [path],
            f"phase {phase.id}: sign-off request",
            trailers={"Factory-Unit": f"phase/{phase.id}"},
        )
        request_id: int | None = None

        def coupled(conn: sqlite3.Connection) -> None:
            nonlocal request_id
            ref = register_artifact(
                conn,
                unit_level=Level.PHASE.value,
                unit_id=phase.id,
                kind="decision_request",
                repo="workspace",
                repo_root=worktree,
                path=path,
                git_commit=sha,
            )
            assert ref.id is not None
            request_id = fdb.insert_decision_request(
                conn,
                DecisionRequest(
                    id=None,
                    unit_level=Level.PHASE.value,
                    unit_id=phase.id,
                    gate_kind="phase_signoff",
                    request_artifact_id=ref.id,
                    status="pending",
                    answer=None,
                    answer_artifact_id=None,
                    created_at=utc_now(),
                    alerted_at=None,
                    answered_at=None,
                ),
            )

        self._sm.transition(
            Level.PHASE,
            phase.id,
            PhaseState.AWAITING_SIGNOFF.value,
            actor=_ACTOR,
            reason="phase merge gates pass (DoD §9.3)",
            coupled=coupled,
        )
        assert request_id is not None  # coupled ran inside the committed tx
        await self._publish_signoff_decision(
            request_id, f"Semnătură de fază necesară: {phase.name}"
        )

    async def _step_awaiting_signoff(self, phase: Phase) -> bool:
        """AWAITING_SIGNOFF: 'approved' -> integrate into the project branch +
        DONE; 'changes' -> RUNNING; unknown answers reported, never guessed."""
        decision = _latest_decision(self._db.read(), Level.PHASE.value, phase.id)
        if decision is None or decision.status != "answered" or decision.answer is None:
            return False
        project = _project_for_phase(self._cfg, phase)
        repo_root = Path(project.workspace)
        if decision.answer == "approved":
            try:
                merge_sha = await self._wt.integrate(
                    repo_root, self._branch(phase), project.integration_branch
                )
            except StaleGateError:
                # Target moved after the gate: §3.2 path back through RUNNING ->
                # INTEGRATING re-runs the gates mechanically.
                self._sm.transition(
                    Level.PHASE,
                    phase.id,
                    PhaseState.RUNNING.value,
                    actor=_ACTOR,
                    reason="integration target moved since the gate — re-gating",
                    payload={"decision_request_id": decision.id},
                )
                return True
            self._sm.transition(
                Level.PHASE,
                phase.id,
                PhaseState.DONE.value,
                actor="founder",
                reason="founder sign-off",
                payload={"decision_request_id": decision.id, "merge_commit": merge_sha},
            )
            try:
                await self._wt.remove(repo_root, self._worktree(phase))
            except GitError as exc:
                with self._db.transaction() as conn:
                    fdb.insert_event(
                        conn,
                        unit_level=Level.PHASE.value,
                        unit_id=phase.id,
                        event_type="alert",
                        actor=_ACTOR,
                        payload={"kind": "worktree_remove_failed", "error": str(exc)},
                    )
            return True
        if decision.answer == "changes":
            # CCR-3/D-0017: the pre-swap `changes_requested` alias is DROPPED —
            # GATE_ANSWERS[('phase','phase_signoff')] is the one vocabulary the
            # dashboard buttons and this executor share.
            self._sm.transition(
                Level.PHASE,
                phase.id,
                PhaseState.RUNNING.value,
                actor="founder",
                reason="sign-off: changes requested",
                payload={"decision_request_id": decision.id},
            )
            return True
        with self._db.transaction() as conn:
            fdb.insert_event(
                conn,
                unit_level=Level.PHASE.value,
                unit_id=phase.id,
                event_type="alert",
                actor=_ACTOR,
                payload={
                    "kind": "unknown_decision_answer",
                    "decision_request_id": decision.id,
                    "answer": decision.answer,
                    "known": list(
                        GATE_ANSWERS[(Level.PHASE.value, "phase_signoff")]
                    ),
                },
            )
        return False

    async def _step_awaiting_human(self, phase: Phase) -> bool:
        decision = _latest_decision(self._db.read(), Level.PHASE.value, phase.id)
        if decision is None or decision.status != "answered" or decision.answer is None:
            return False
        allowed = GATE_ANSWERS.get((Level.PHASE.value, decision.gate_kind), ())
        target = (
            _PHASE_ANSWER_TARGETS.get(decision.answer)
            if decision.answer in allowed
            else None
        )
        if target is None:
            with self._db.transaction() as conn:
                fdb.insert_event(
                    conn,
                    unit_level=Level.PHASE.value,
                    unit_id=phase.id,
                    event_type="alert",
                    actor=_ACTOR,
                    payload={
                        "kind": "unknown_decision_answer",
                        "decision_request_id": decision.id,
                        "answer": decision.answer,
                        "known": list(allowed),
                    },
                )
            return False
        self._sm.transition(
            Level.PHASE,
            phase.id,
            target.value,
            actor="founder",
            reason=f"human gate answered: {decision.answer}",
            payload={"decision_request_id": decision.id, "answer": decision.answer},
        )
        return True

    async def _step_escalated(self, phase: Phase) -> bool:
        """ESCALATED: blocked while any escalation is open; on resolution,
        archive sentinels (§5.4, same rule as the stage path) and route per
        the resolution vocabulary."""
        conn = self._db.read()
        if _open_escalation_count(conn, Level.PHASE.value, phase.id) > 0:
            return False
        last = _latest_resolved_escalation(conn, Level.PHASE.value, phase.id)
        if last is None:
            return False
        # No-action (`settled`) disposition at the PHASE level (architect-operations.md
        # §1, D-0062): the architect/founder accepts an accurate Tier-2 integration
        # finding. SPECIAL-CASED before the static PHASE_ESCALATION_RESOLUTIONS lookup
        # (mirrors the stage settled path, ~3040). There are no contested finding ROWS at
        # phase level (the finding lives in the tier2_gate event; the acceptance rationale
        # in the escalation_resolved event the CLI wrote), so this only archives sentinels
        # then routes forward to sign-off exactly as a clean Tier-2 would
        # (the ESCALATED→AWAITING_SIGNOFF accepted-finding edge).
        if last.resolution == PHASE_NOACTION_RESOLUTION:
            worktree = self._worktree(phase)
            if not worktree.is_dir():
                # Derived, recomputable state — recreate (idempotent branch attach,
                # the merge-gate rule; mirrors the AWAITING_HUMAN recreate below).
                project = _project_for_phase(self._cfg, phase)
                worktree = await self._wt.create(
                    Path(project.workspace),
                    phase.id,
                    self._branch(phase),
                    project.integration_branch,
                )
            await self._archive_sentinels(phase, worktree, last)
            await self._enter_signoff(phase, worktree)
            return True
        target = PHASE_ESCALATION_RESOLUTIONS.get(last.resolution or "")
        if target is None:
            with self._db.transaction() as tx:
                fdb.insert_event(
                    tx,
                    unit_level=Level.PHASE.value,
                    unit_id=phase.id,
                    event_type="alert",
                    actor=_ACTOR,
                    payload={
                        "kind": "unknown_escalation_resolution",
                        "escalation_id": last.id,
                        "resolution": last.resolution,
                        "known": sorted(PHASE_ESCALATION_RESOLUTIONS),
                    },
                )
            return False
        worktree = self._worktree(phase)
        if worktree.is_dir():
            await self._archive_sentinels(phase, worktree, last)

        # §2a/D-0017: Romanian request-wrapper artifact for the phase trade-off
        # gate — written + COMMITTED before the recording tx (§7 step order),
        # registered as the request anchor; payload linked, never inlined; no
        # recommendation line.
        wrapper_path: Path | None = None
        wrapper_sha: str | None = None
        if target is PhaseState.AWAITING_HUMAN:
            if not worktree.is_dir():
                # The phase checkout is derived, recomputable state — recreate it
                # (idempotent attach of the existing branch, the merge-gate rule).
                project = _project_for_phase(self._cfg, phase)
                worktree = await self._wt.create(
                    Path(project.workspace),
                    phase.id,
                    self._branch(phase),
                    project.integration_branch,
                )
            anchor_ref = self._tradeoff_anchor_ref(phase, last)
            unit_dir = self._unit_dir(worktree, phase)
            unit_dir.mkdir(parents=True, exist_ok=True)
            wrapper_path = unit_dir / "decision-request-escalation.md"
            wrapper_path.write_text(
                self._tradeoff_request_body(phase, last, anchor_ref),
                encoding="utf-8",
            )
            wrapper_sha = await commit_paths(
                worktree,
                [wrapper_path],
                f"phase {phase.id}: escalation trade-off decision request",
                trailers={"Factory-Unit": f"phase/{phase.id}"},
            )
            if wrapper_sha is None:  # byte-identical replay: pin to HEAD
                code, out, err = await run_git("rev-parse", "HEAD", cwd=worktree)
                if code != 0:
                    raise GitError(
                        f"git rev-parse HEAD failed in {worktree}: {(err or out).strip()}"
                    )
                wrapper_sha = out.strip()

        request_id: int | None = None
        wrapper_root = worktree

        def coupled(tx: sqlite3.Connection) -> None:
            if target is PhaseState.AWAITING_HUMAN:
                # Same crash-safety rule as the stage path: the gate's decision
                # request lands in the SAME tx as the transition.
                assert wrapper_path is not None
                ref = register_artifact(
                    tx,
                    unit_level=Level.PHASE.value,
                    unit_id=phase.id,
                    kind="decision_request",
                    repo="workspace",
                    repo_root=wrapper_root,
                    path=wrapper_path,
                    git_commit=wrapper_sha,
                )
                assert ref.id is not None
                nonlocal request_id
                request_id = fdb.insert_decision_request(
                    tx,
                    DecisionRequest(
                        id=None,
                        unit_level=Level.PHASE.value,
                        unit_id=phase.id,
                        gate_kind="escalation_tradeoff",
                        request_artifact_id=ref.id,
                        status="pending",
                        answer=None,
                        answer_artifact_id=None,
                        created_at=utc_now(),
                        alerted_at=None,
                        answered_at=None,
                    ),
                )

        self._sm.transition(
            Level.PHASE,
            phase.id,
            target.value,
            actor="main_architect",
            reason=f"escalation resolved: {last.resolution}",
            payload={"escalation_id": last.id, "resolution": last.resolution},
            coupled=coupled,
        )
        if request_id is not None:
            await self._publish_signoff_decision(
                request_id, f"Decizie necesară (escaladare): faza {phase.name}"
            )
        return True

    def _tradeoff_anchor_ref(self, phase: Phase, escalation: Escalation) -> int:
        """artifact_refs.id the phase wrapper links to: the escalation payload
        when recorded, else the newest phase artifact (plan -> request ->
        contract); none = fail-explicit (a phase with no artifact cannot pose a
        trade-off)."""
        if escalation.payload_artifact_id is not None:
            return escalation.payload_artifact_id
        conn = self._db.read()
        for kind in ("phase_plan_sidecar", "phase_plan", "decision_request", "contract"):
            ref = fdb.latest_artifact(conn, Level.PHASE.value, phase.id, kind)
            if ref is not None:
                assert ref.id is not None
                return ref.id
        raise FactoryError(
            f"phase {phase.id}: no artifact to anchor the escalation"
            " trade-off decision request"
        )

    def _tradeoff_request_body(
        self, phase: Phase, escalation: Escalation, payload_ref: int
    ) -> str:
        """Romanian escalation-tradeoff request wrapper for a phase (§2a)."""
        return (
            f"# Cerere de decizie — Faza: {phase.name} ({phase.id})\n\n"
            f"Tip poartă: {_ro_glossed('escalation_tradeoff')}\n\n"
            "## Întrebare\n"
            f"Faza „{phase.name}” ({phase.id}) a fost escaladată — mecanismele"
            " automate nu pot decide singure. Cum continuăm?\n\n"
            "## Context\n"
            f"Declanșator: {_ro_glossed(escalation.trigger)} — escaladarea"
            f" #{escalation.id}. Dovezile sunt în artefactul legat mai jos —"
            " citește-l înainte de a alege.\n\n"
            "## Opțiuni\n"
            f"{_ro_options_block(Level.PHASE.value, 'escalation_tradeoff')}\n\n"
            "## Legături\n"
            f"Contextul escaladării (artefact): /artifact/{payload_ref}\n\n"
            "Răspunde din panou (butoanele de opțiuni) sau, în caz de urgență,"
            " din terminal cu „cli decide”.\n"
        )

    async def _archive_sentinels(
        self, phase: Phase, worktree: Path, escalation: Escalation
    ) -> None:
        """§5.4 sentinel lifecycle at phase level, mirroring the stage path:
        archive (rename + commit) any present sentinel in the DURABLE phase
        worktree BEFORE re-running steps — a stale PLANNING sentinel would
        otherwise be re-detected after a 'replan'/'resume' resolution as a NEW
        events.seq past the escalations.event_seq cursor and re-escalate
        forever; a NEW sentinel written after rework fires again by design."""
        unit_dir = self._unit_dir(worktree, phase)
        renamed = False
        for kind in detect_sentinels(unit_dir):
            src = unit_dir / STAGE_ARTIFACTS[kind]
            dst = src.with_name(f"{src.stem}.resolved-{escalation.id}.md")
            src.rename(dst)
            renamed = True
        if renamed:
            # Commit the whole unit artifact dir: it stages the rename pair even
            # when the sentinel was never tracked (same rule as the stage path).
            await commit_paths(
                worktree,
                [unit_dir],
                f"phase {phase.id}: archive resolved sentinel(s)",
                trailers={"Factory-Unit": f"phase/{phase.id}"},
            )

    async def _publish_signoff_decision(self, request_id: int, title: str) -> None:
        """Phase-signoff decision publish — same published_at delivered-signal +
        per-tick retry backstop as ``_publish_decision`` (founder 20-06)."""
        try:
            await self._notify.publish(
                title,
                link=dashboard_link(self._cfg, f"decision/{request_id}"),
                priority=self._notify.priority_decision,
            )
        except NotifyError as exc:
            with self._db.transaction() as conn:
                fdb.insert_event(
                    conn,
                    unit_level="factory",
                    unit_id=None,
                    event_type="alert_delivery_failed",
                    actor=_ACTOR,
                    payload={"decision_request_id": request_id, "error": str(exc)},
                )
            return
        with self._db.transaction() as conn:
            fdb.mark_decision_published(conn, request_id, utc_now())

    # ----------------------------------------------------------------- prompts

    def _planning_prompt(self, phase: Phase, *, prefrozen: bool = False) -> str:
        risk_classes = sorted(self._cfg.risk_classes)
        unit_rel = f"_factory/phases/{phase.id}"
        contracts_ns = f"_factory/contracts/phase-{phase.id}/"
        project = _project_for_phase(self._cfg, phase)
        context = ""
        if project.project_md is not None:
            # Phase-seeding design §4: config-driven project-context block —
            # fully absent when projects.<p>.project_md is None (synthetic/b8).
            home = self._cfg.factory.home
            docs_repo = _resolve(home, project.docs_repo)
            project_md = _resolve(home, project.project_md)
            context = (
                "Project context (read before planning):\n"
                f"- Business documentation (canonical source of truth): {docs_repo}\n"
                f"- Macro plan & project brief: {project_md} (PROJECT.md; the macro "
                "decision log sits next to it)\n"
                "- Cross-phase contracts already in force: _factory/contracts/*.md "
                "(READ-ONLY — a needed change is a _CONTRACT_CHANGE_REQUEST.md + stop)\n"
                f"- Write YOUR intra-phase contracts under {contracts_ns} (namespace "
                "convention: cross-phase files at the root are never edited by a phase; "
                "both Tier-2 collection sites rglob recursively, so namespaced contracts "
                "are picked up unchanged)\n"
            )
        if prefrozen:
            # Option A: the stage decomposition is ALREADY FROZEN (founder-RATIFIED,
            # adopted into the worktree by the scheduler). Narrow the architect to
            # authoring the intra-phase seam contracts only — the plan is verified
            # byte-unchanged after this spawn, so touching it only escalates.
            body = (
                f"The stage plan ({unit_rel}/phase-plan.json + .md) is ALREADY FROZEN "
                "and present — do NOT regenerate, edit, move, or delete it. Author ONLY "
                f"the intra-phase {contracts_ns} seam specs (shared schemas, API "
                "signatures, named invariants) that the existing stages reference, "
                "freezing them as files BEFORE any fan-out.\n"
            )
        else:
            body = (
                "Decompose the phase into stages sized at the upper bound of one-pass "
                "builder confidence; declare per-stage acceptance criteria and risk class; "
                "freeze the intra-phase contracts (shared schemas, API signatures, named "
                f"invariants) as files under {contracts_ns} BEFORE any fan-out.\n"
                f"Write {unit_rel}/phase-plan.md (rationale) and {unit_rel}/phase-plan.json — "
                'EXACTLY {"stages": [{"id": "<plan-local-id>", "name": "...", '
                '"risk_class": "<one of ' + "|".join(risk_classes) + '>", '
                '"acceptance": "...", "acceptance_criteria": ["...", "..."], '
                '"touched": ["path/or/component", "..."], "role": "<contract|leaf>"}], '
                '"dag_edges": [["<from>", "<to>"]]} — '
                "ids unique, every edge endpoint declared, DAG acyclic.\n"
                "Per stage ALSO emit: acceptance_criteria (the acceptance as a checklist "
                "of discrete, testable items); touched (the files / components / "
                "contract-symbols the stage will modify); role ('contract' for a thin "
                "seam-freezing stage that fixes a shared schema/API signature/invariant, "
                "'leaf' otherwise).\n"
                "CONTRACT-FIRST: when the phase has shared seams, author a role='contract' "
                "stage that freezes them FIRST and add dag_edges so EVERY dependent (leaf) "
                "stage is a DAG descendant of a contract stage — dependents then build "
                "against a frozen contract (a leaf with no contract ancestor is rejected).\n"
                "SIZE each stage within the limits: <=7 acceptance_criteria, <=6 touched, "
                "<=6 dependency-degree (in+out edges); split anything larger, and do not "
                "over-split a leaf below 1 criterion / 1 touched (contract stages may be "
                "thinner). Oversized stages are flagged to the architect.\n"
            )
        return (
            f"You are the Phase Architect for phase '{phase.id}' ({phase.name}).\n"
            + context
            + body
            + f"If you cannot proceed, write {unit_rel}/_DECLARED_FAILURE.md; a cross-phase "
            f"contract change needs {unit_rel}/_CONTRACT_CHANGE_REQUEST.md + stop."
        )


# -------------------------------------------------------------------- Scheduler


class Scheduler:
    """Level-agnostic loop over sched categories + dag_edges (design §4); max
    process.max_parallel_agents concurrent units; crash recovery entry."""

    def __init__(
        self,
        db: Database,
        sm: StateMachine,
        cfg: FactoryConfig,
        executors: Mapping[Level, UnitExecutor],
        notify: NtfyPublisher,
        dashboard: DashboardServer | None = None,
        governor: CapacityGovernor | None = None,
    ) -> None:
        """Level-agnostic loop over sched categories + dag_edges; max
        process.max_parallel_agents concurrent units. ``dashboard`` (CCR-3,
        optional, default None — tests/run_until_blocked unaffected): when
        present, ``_run`` hosts the contained ``_dashboard_supervisor``.
        ``governor`` (CCR-11, optional, default None): the SHARED capacity
        governor instance also wired into both executors — when present, the
        loop runs its hold-exit probe every tick; None (tests that wire the
        graph by hand) skips probing, exactly like enabled:false."""
        self._db = db
        self._sm = sm
        self._cfg = cfg
        self._executors = dict(executors)
        self._notify = notify
        self._dashboard = dashboard
        self._governor = governor
        #: Total dashboard supervisor restarts (paging-dedup counter, design §6).
        self._dashboard_restarts = 0
        #: Internal worktree manager for recover()'s §5.5b git healing — a
        #: mechanics helper, not an executor dependency.
        self._wt = WorktreeManager(cfg)
        #: Phase-seeding design §5: out-of-bounds check during recover().
        self._oob = _OutOfBoundsDetector(db, cfg, notify)
        self._tasks: dict[tuple[Level, str], asyncio.Task] = {}
        #: Keys of live drives that spawn an agent (robustness UNIT 1) — the cap
        #: denominator. No-spawn control-plane drives (ESCALATED pickup, gates)
        #: are excluded so they are never starved by a full cap; reconciled to
        #: `_tasks` in `_spawning_count` and cleared in `_reap`/`_drive.finally`.
        self._spawning: set[tuple[Level, str]] = set()
        #: max events.seq at the end of a unit's last drive — the re-dispatch
        #: edge trigger (a no-progress unit is not respun until facts change).
        self._last_seq: dict[tuple[Level, str], int] = {}
        #: (open escalations, pending decisions) snapshot at last drive end —
        #: wakes BLOCKED units on resolutions/answers even if the answering
        #: plumbing wrote no event.
        self._blocked_snapshot: dict[tuple[Level, str], tuple[int, int]] = {}
        #: Last tick's manual-DRAIN state (5e correction): on a True->False edge
        #: the agent-level drain hold lifts, so RUNNING units parked at an agent
        #: boundary (Part A) must re-dispatch even without a new event — the
        #: capacity probe re-dispatches governor-held units, but manual drain has
        #: no such fallback. See _dispatch's `drain_lifted`.
        self._prev_drain_manual: bool = False
        self._stall_event_logged = False
        self._stall_published = False
        #: One alert_delivery_failed event per consecutive-failure streak (the
        #: retry itself continues every tick; only the event is deduplicated).
        self._delivery_failed_logged: set[object] = set()
        #: Stuck-escalation detector latches (robustness UNIT 2). Each fires ONCE
        #: per episode/rung so the §9 channel never thrashes (alarm fatigue):
        #:  • _escalation_opened_notified — open architect-targeted escalations
        #:    already given their first-notice page (Q2 ≤5-min law);
        #:  • _escalation_stuck_resolved_notified — resolved-but-unadvanced episodes
        #:    already paged once.
        #: All keyed by esc_id and self-clearing: an esc_id absent from the live
        #: open/resolved read is pruned each tick (the escalation closed/advanced),
        #: so a future re-open with a fresh id re-arms cleanly. The open-too-long
        #: climb (2a) needs NO latch: it derives the expected rung from the
        #: escalation's age and bumps only when target lags it (stateless — the
        #: persisted target IS the latch), so it survives restart on its own.
        self._escalation_opened_notified: set[int] = set()
        self._escalation_stuck_resolved_notified: set[int] = set()

    # ----------------------------------------------------------- liveness files

    def _liveness_path(self) -> Path:
        return _resolve(self._cfg.factory.home, self._cfg.process.liveness_file)

    def _pid_path(self) -> Path:
        return _resolve(self._cfg.factory.home, self._cfg.process.pid_file)

    def _touch_liveness(self) -> None:
        """mtime = last orchestrator tick (the watchdog's staleness input)."""
        path = self._liveness_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(utc_now() + "\n", encoding="utf-8")

    def _refresh_pidfile(self) -> None:
        """Pidfile content contract shared with cli/watchdog (§4/watchdog doc):
        line 1 = pid, line 2 = /proc/<pid>/cmdline with NULs as spaces. The
        rewrite refreshes mtime in place (same inode — the cli flock survives)."""
        path = self._pid_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            raw = Path("/proc/self/cmdline").read_bytes()
            cmdline = raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
        except OSError:  # non-/proc platform: pid-only file (watchdog tolerates)
            cmdline = ""
        with open(path, "r+" if path.exists() else "w", encoding="utf-8") as fh:
            fh.seek(0)
            fh.write(f"{os.getpid()}\n{cmdline}\n")
            fh.truncate()

    # -------------------------------------------------------------- §5.5 recover

    def recover(self) -> RecoveryReport:
        """Crash recovery (DoD §12.A2), only under the cli single-instance flock.
        Touches the liveness file at entry and periodically during the scan (a healthy
        restart must not page the watchdog). Steps: (a) orphan sweep — kill by PROCESS
        GROUP, mark 'orphaned' + event; (b) git healing — worktrees.heal_git_state on
        every known worktree + the integration checkout, `git worktree prune`; then
        worktree canonicalization: `git status --porcelain` per unit worktree — if dirty
        (an orphan kept writing until killed), save the dirty diff to ndjson_log_dir as
        evidence + event, hard-reset + `clean -fd` to the step's base commit (committed
        git state is the ONLY canonical step input — the idempotency precondition of
        §5.5d); (c) verify_integrity — abort start on a non-terminal-unit mismatch;
        BLOCKED/RUNNING units re-enter the queue and resume from SQLite state + on-disk
        artifacts."""
        self._touch_liveness()
        return asyncio.run(self._recover_async())

    async def _recover_async(self) -> RecoveryReport:
        if self._governor is not None:
            # CCR-11 (D-0037) restart honesty: holds are in-memory only —
            # close a stale capacity_hold_started/_ended event pair from the
            # previous process so the dashboard read-path never shows a hold
            # no live governor owns. The limit, if still real, re-discovers
            # itself on the first dead claude spawn (one cheap spawn wasted —
            # accepted).
            self._governor.reconcile_restart()
        orphaned, killed_groups = self._orphan_sweep()  # (a)
        self._touch_liveness()
        healed, heal_errors, dirty_reset = await self._heal_git()  # (b)
        self._touch_liveness()
        # Phase-seeding design §5: out-of-bounds detector — dirt in the factory
        # repo / workspace integration checkout left across the down window is
        # alerted at recovery, never a silent pass (Doctrine §20).
        await self._oob.check(where="recover")
        self._touch_liveness()
        checked, warnings = await self._integrity_gate()  # (c)
        self._touch_liveness()
        requeued = self._requeue_scan()  # (d) — the loop drives them from disk
        return RecoveryReport(
            orphaned=tuple(orphaned),
            killed_groups=tuple(killed_groups),
            healed=healed,
            heal_errors=tuple(heal_errors),
            dirty_reset=tuple(dirty_reset),
            integrity_checked=checked,
            integrity_warnings=warnings,
            requeued=tuple(requeued),
        )

    def _orphan_sweep(self) -> tuple[list[int], list[int]]:
        """§5.5a: every 'spawned'/'running' registry row — pid alive with a
        matching cmdline (or a dead leader with a live group: our descendants)
        -> SIGKILL the process GROUP; every such row -> 'orphaned' + event.
        A live pid with a foreign cmdline is pid reuse — never killed."""
        conn = self._db.read()
        rows = fdb.processes_in_state(conn, "spawned") + fdb.processes_in_state(
            conn, "running"
        )
        orphaned: list[int] = []
        killed: list[int] = []
        for rec in rows:
            assert rec.id is not None
            kill = False
            if rec.pid is not None:
                if _pid_alive(rec.pid):
                    kill = _proc_cmdline_matches(rec.pid, rec.cmdline)
                else:
                    kill = _group_alive(rec.pid)  # leader gone, group ours
            if kill and rec.pid is not None:
                try:
                    os.killpg(rec.pid, signal.SIGKILL)
                    killed.append(rec.pid)
                except (ProcessLookupError, PermissionError):
                    pass
            with self._db.transaction() as tx:
                fdb.finalize_process(
                    tx, rec.id, state="orphaned", exit_code=None, ended_at=utc_now()
                )
                fdb.insert_event(
                    tx,
                    unit_level=rec.unit_level or "factory",
                    unit_id=rec.unit_id,
                    event_type="orphaned",
                    actor=_ACTOR,
                    payload={
                        "process_id": rec.id,
                        "pid": rec.pid,
                        "role": rec.role,
                        "group_killed": kill,
                    },
                )
            orphaned.append(rec.id)
        return orphaned, killed

    def _known_checkouts(self) -> tuple[list[tuple[str, Path]], list[Path]]:
        """(unit worktrees as (label, path), integration checkouts). Only paths
        that exist on disk — a configured-but-uncreated workspace has no git
        state to heal."""
        conn = self._db.read()
        unit_worktrees: list[tuple[str, Path]] = []
        for stage in fdb.list_units(conn, Level.STAGE):
            assert isinstance(stage, Stage)
            if stage.worktree_path and Path(stage.worktree_path).is_dir():
                unit_worktrees.append((f"stage:{stage.id}", Path(stage.worktree_path)))
        for phase in fdb.list_units(conn, Level.PHASE):
            assert isinstance(phase, Phase)
            project = self._cfg.projects.get(phase.project)
            if project is None:
                continue
            path = Path(project.worktrees_dir) / phase.id
            if path.is_dir():
                unit_worktrees.append((f"phase:{phase.id}", path))
        checkouts = [
            Path(project.workspace)
            for project in self._cfg.projects.values()
            if Path(project.workspace).is_dir()
        ]
        return unit_worktrees, checkouts

    async def _heal_git(
        self,
    ) -> tuple[dict[str, tuple[str, ...]], list[str], list[str]]:
        unit_worktrees, checkouts = self._known_checkouts()
        healed: dict[str, tuple[str, ...]] = {}
        heal_errors: list[str] = []
        dirty_reset: list[str] = []
        for checkout in checkouts:
            try:
                actions = await self._wt.heal_git_state(checkout)
                healed[str(checkout)] = tuple(actions)
            except GitError as exc:
                self._record_heal_failure(
                    heal_errors,
                    unit_level="factory",
                    unit_id=None,
                    path=checkout,
                    error=str(exc),
                )
            code, out, err = await run_git("worktree", "prune", cwd=checkout)
            if code != 0:
                self._record_heal_failure(
                    heal_errors,
                    unit_level="factory",
                    unit_id=None,
                    path=checkout,
                    error=f"worktree prune: {(err or out).strip()}",
                )
            self._touch_liveness()
        for label, worktree in unit_worktrees:
            try:
                actions = await self._wt.heal_git_state(worktree)
                healed[str(worktree)] = tuple(actions)
                if await self._canonicalize_worktree(label, worktree):
                    dirty_reset.append(str(worktree))
            except GitError as exc:
                level, _, unit_id = label.partition(":")
                self._record_heal_failure(
                    heal_errors,
                    unit_level=level,
                    unit_id=unit_id,
                    path=worktree,
                    error=str(exc),
                )
            self._touch_liveness()
        return healed, heal_errors, dirty_reset

    def _record_heal_failure(
        self,
        heal_errors: list[str],
        *,
        unit_level: str,
        unit_id: str | None,
        path: Path,
        error: str,
    ) -> None:
        """§6 fail-explicit at §5.5b: a failed heal must leave durable evidence
        at recovery time — RecoveryReport.heal_errors alone is in-memory only
        (cli.cmd_run discards recover()'s return). One 'alert' event per
        failure, same transaction-on-the-loop-thread pattern as the
        dirty_worktree_reset event."""
        heal_errors.append(f"{path}: {error}")
        with self._db.transaction() as conn:
            fdb.insert_event(
                conn,
                unit_level=unit_level,
                unit_id=unit_id,
                event_type="alert",
                actor=_ACTOR,
                payload={"kind": "heal_failed", "path": str(path), "error": error},
            )

    async def _canonicalize_worktree(self, label: str, worktree: Path) -> bool:
        """§5.5b worktree canonicalization: dirty unit worktree -> evidence file
        in ndjson_log_dir + event, then hard-reset + clean -fd to the step's
        base commit (= the committed HEAD: every step commits before the tx
        that records it, §7)."""
        code, status_out, err = await run_git("status", "--porcelain", cwd=worktree)
        if code != 0:
            raise GitError(f"git status failed in {worktree}: {(err or status_out).strip()}")
        if not status_out.strip():
            return False
        code, diff_out, _ = await run_git("diff", "HEAD", cwd=worktree)
        if code != 0:
            diff_out = "(git diff HEAD failed)"
        log_dir = _resolve(self._cfg.factory.home, self._cfg.process.ndjson_log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        evidence = log_dir / f"{new_id('recovery-dirty')}.diff"
        evidence.write_text(
            f"# dirty worktree evidence: {label} at {worktree}\n"
            f"## git status --porcelain\n{status_out}\n## git diff HEAD\n{diff_out}",
            encoding="utf-8",
        )
        level, _, unit_id = label.partition(":")
        with self._db.transaction() as conn:
            fdb.insert_event(
                conn,
                unit_level=level,
                unit_id=unit_id,
                event_type="dirty_worktree_reset",
                actor=_ACTOR,
                payload={"worktree": str(worktree), "evidence": str(evidence)},
            )
        for args in (("reset", "--hard", "HEAD"), ("clean", "-fd")):
            code, out, err = await run_git(*args, cwd=worktree)
            if code != 0:
                raise GitError(
                    f"git {' '.join(args)} failed in {worktree}: {(err or out).strip()}"
                )
        return True

    async def _integrity_gate(self) -> tuple[int, int]:
        """§5.5c: verify_integrity over factory + workspace roots; non-terminal
        mismatch -> alert + IntegrityError (start aborted, no silent repair)."""
        report = verify_integrity(self._db, self._repo_roots())
        if report.failures:
            summary = [
                f"{i.unit_level}/{i.unit_id} {i.kind} {i.path}: {i.problem}"
                for i in report.failures
            ]
            with self._db.transaction() as conn:
                fdb.insert_event(
                    conn,
                    unit_level="factory",
                    unit_id=None,
                    event_type="integrity_failure",
                    actor=_ACTOR,
                    payload={"failures": summary[:50], "count": len(summary)},
                )
            try:
                await self._notify.publish(
                    "Fabrica nu poate porni: integritatea artefactelor a eșuat",
                    link=dashboard_link(self._cfg, "acum"),
                    priority=self._notify.priority_alert,
                )
            except NotifyError as exc:
                with self._db.transaction() as conn:
                    fdb.insert_event(
                        conn,
                        unit_level="factory",
                        unit_id=None,
                        event_type="alert_delivery_failed",
                        actor=_ACTOR,
                        payload={"kind": "integrity_failure", "error": str(exc)},
                    )
            raise IntegrityError(
                "artifact integrity check failed for non-terminal unit(s); start "
                "aborted (no silent repair): " + "; ".join(summary[:10])
            )
        return report.checked, len(report.warnings)

    def _repo_roots(self) -> dict[str, Path]:
        """artifact_refs.repo -> root. 'factory' = factory.home; 'workspace' =
        the project workspace — unambiguous in MVP (single project); with
        several configured, the one the DB's phases actually reference."""
        roots = {"factory": self._cfg.factory.home}
        projects = self._cfg.projects
        if len(projects) == 1:
            roots["workspace"] = Path(next(iter(projects.values())).workspace)
            return roots
        referenced = {
            phase.project
            for phase in fdb.list_units(self._db.read(), Level.PHASE)
            if isinstance(phase, Phase)
        }
        known = referenced & set(projects)
        if len(known) == 1:
            roots["workspace"] = Path(projects[next(iter(known))].workspace)
            return roots
        if not known:
            return roots  # no workspace refs can exist yet
        raise FactoryError(
            "cannot map artifact repo 'workspace' to a single project workspace: "
            f"phases reference projects {sorted(known)}"
        )

    def _requeue_scan(self) -> list[str]:
        """§5.5d: units in RUNNING-category states re-enter the queue (the loop
        re-runs their current step from disk); AWAITING_* stay blocked."""
        conn = self._db.read()
        requeued: list[str] = []
        for level in self._levels():
            for unit in fdb.list_units(conn, level):
                category = sched_category(level, unit.state.value, True)
                if category is SchedCategory.RUNNING:
                    requeued.append(f"{level.value}:{unit.id}")
        return requeued

    # ----------------------------------------------------------------- the loop

    async def run_forever(self) -> None:
        """Main loop, tick = process.loop_tick_s: refresh liveness file + pidfile,
        dispatch RUNNABLE units, reap finished tasks, fire decision-latency alerts, and
        run the STALL DETECTOR — non-terminal units exist, nothing RUNNABLE/RUNNING, and
        no open decision_request/escalation → 'alert' event + ntfy (a wedged factory
        must page, never idle green — Doctrine §20). One asyncio TaskGroup; ALL db
        writes happen on this loop thread; notification I/O only via the async
        NtfyPublisher (never blocks the loop)."""
        await self._run(stop_when_blocked=False)

    async def run_until_blocked(self) -> None:
        """Same loop; returns when nothing is RUNNABLE/RUNNING (tests, criterion runs).
        Quiescence is observed at the dispatch boundary: no live executor task and no
        unit eligible for (re-)dispatch — i.e. every remaining unit is terminal,
        WAITING, BLOCKED, or category-RUNNING with no new facts to act on."""
        await self._run(stop_when_blocked=True)

    async def _run(self, *, stop_when_blocked: bool) -> None:
        async with asyncio.TaskGroup() as tg:
            # CCR-3: the dashboard supervisor task is EXCLUDED from self._tasks /
            # quiescence accounting and cancelled on every _run exit path — else
            # run_until_blocked's TaskGroup would never close (design §6).
            supervisor: asyncio.Task | None = None
            if self._dashboard is not None:
                supervisor = tg.create_task(self._dashboard_supervisor())
            try:
                while True:
                    self._touch_liveness()
                    self._refresh_pidfile()
                    self._reap()
                    scan = self._scan_units()
                    await self._stall_detector(scan)
                    await self._publish_pending_decisions()
                    await self._decision_latency_alerts()
                    await self._stuck_escalation_detector(scan)
                    if self._governor is not None:
                        # CCR-11 (D-0037): hold-exit probe — a no-op unless a
                        # capacity hold is active and the interval elapsed; a
                        # successful canary lifts the hold + auto-resolves the
                        # limit-marked escalations BEFORE this tick's dispatch,
                        # so the freed units re-enter in the same tick.
                        await self._governor.tick()
                    dispatched = self._dispatch(tg, scan)
                    if stop_when_blocked and not self._tasks and dispatched == 0:
                        return
                    await asyncio.sleep(self._cfg.process.loop_tick_s)
            finally:
                if supervisor is not None:
                    supervisor.cancel()

    def _levels(self) -> list[Level]:
        return sorted(self._executors, key=lambda level: level.value)

    def _reap(self) -> None:
        for key in [k for k, task in self._tasks.items() if task.done()]:
            del self._tasks[key]
            self._spawning.discard(key)  # a finished spawning drive frees its slot

    def _scan_units(self) -> list[tuple[Level, str, SchedCategory, Phase | Stage]]:
        """One categorized pass over all units (feeds dispatch + stall detector).

        The unit OBJECT is carried alongside its category (robustness UNIT 1):
        `_dispatch` needs it to ask the executor `spawn_roles(unit)` without a
        second per-candidate read — one `list_units` pass, both consumers
        (`_dispatch`, `_stall_detector`) share the snapshot.

        Phase-seeding design §5b: the RUNNABLE selection applies the
        proving-phases dispatch hold at PHASE level — a held phase is
        categorized WAITING (state stays PENDING, never transitioned), so it is
        neither dispatched nor mistaken for progress by the stall detector."""
        conn = self._db.read()
        scan: list[tuple[Level, str, SchedCategory, Phase | Stage]] = []
        for level in self._levels():
            units = fdb.list_units(conn, level)
            held: frozenset[str] = frozenset()
            if level is Level.PHASE:
                held = proving_held_phase_ids(
                    self._cfg, [u for u in units if isinstance(u, Phase)]
                )
            for unit in units:
                state = unit.state.value
                deps = (
                    fdb.deps_done(conn, level, unit.id) if state == "PENDING" else True
                )
                category = sched_category(level, state, deps)
                if category is SchedCategory.RUNNABLE and unit.id in held:
                    category = SchedCategory.WAITING  # §5b hold: not dispatched
                scan.append((level, unit.id, category, unit))
        return scan

    def _dispatch(
        self, tg: asyncio.TaskGroup, scan: list[tuple[Level, str, SchedCategory, Phase | Stage]]
    ) -> int:
        """Dispatch eligible units up to the max_parallel_agents cap. RUNNABLE
        always dispatches; category-RUNNING re-dispatches when events advanced
        since its last drive (or it was never driven — crash resume, §5.5d);
        BLOCKED re-dispatches additionally when its open-escalation/pending-
        decision counts changed (answers may arrive without events).

        Scheduler fairness (robustness UNIT 1): the cap bounds only SPAWNING
        drives — a drive whose execute() will run an agent subprocess. No-spawn
        control-plane work (the ESCALATED resolution pickup, AWAITING gates)
        proceeds past a full cap so a resolved-but-not-routed escalation on a
        critical stage is not starved behind routine agents. The cap still
        bounds real concurrent agents because spawns are sequential within one
        execute() task and every spawning drive holds a `_spawning` slot for its
        whole walk. `continue`, not `break`: a later no-spawn unit in the scan
        must stay reachable behind a capped spawning one."""
        conn = self._db.read()
        # Live overrides (founder dashboard, 5e/item 4): the cap and the manual
        # DRAIN switch are read per-tick through EffectiveConfig. Empty overrides
        # => byte-identical to the load-once cfg (the existing cap tests hold).
        eff = EffectiveConfig(fdb.get_runtime_settings(conn), self._cfg)
        cap = eff.max_parallel_agents
        # Manual-DRAIN ON->OFF edge (5e correction): a RUNNING unit parked at an
        # agent boundary by the execute-level drain hold (Part A) wrote no new
        # event, so its `_last_seq` already covers `seq` and it is not otherwise
        # eligible. Force RUNNING units eligible for this one tick so they
        # re-dispatch and resume; drain now being off, neither the dispatch check
        # nor the execute check holds them again.
        drain_lifted = self._prev_drain_manual and not eff.drain_manual
        self._prev_drain_manual = eff.drain_manual
        seq = _max_event_seq(conn)
        dispatched = 0
        for level, unit_id, category, unit in scan:
            key = (level, unit_id)
            if key in self._tasks:
                continue
            if category in (
                SchedCategory.WAITING,
                SchedCategory.TERMINAL_OK,
                SchedCategory.TERMINAL_FAIL,
            ):
                continue
            if category is SchedCategory.RUNNABLE:
                eligible = True
            elif category is SchedCategory.RUNNING:
                eligible = (
                    drain_lifted
                    or key not in self._last_seq
                    or seq > self._last_seq[key]
                )
            else:  # BLOCKED
                snapshot = (
                    _open_escalation_count(conn, level.value, unit_id),
                    _pending_decision_count(conn, level.value, unit_id),
                )
                eligible = (
                    key not in self._last_seq
                    or seq > self._last_seq[key]
                    or self._blocked_snapshot.get(key) != snapshot
                )
            if not eligible:
                continue
            spawns = self._drive_spawns(level, category, unit)
            if spawns and eff.drain_manual:
                continue  # founder manual DRAIN (5e): hold every NEW agent spawn —
                # running drives finish on their own. Same `continue` discipline as
                # the cap below: no-spawn control-plane work (ESCALATED resolution
                # pickup, AWAITING gates) still proceeds, so the factory winds down
                # cleanly instead of stalling mid-flight.
            if spawns and self._spawning_count() >= cap:
                continue  # economics cap (§7): a SPAWNING drive waits for a free
                # slot; keep scanning so no-spawn control-plane work still runs.
            self._tasks[key] = tg.create_task(self._drive(level, unit_id))
            if spawns:
                self._spawning.add(key)
            dispatched += 1
        return dispatched

    def _drive_spawns(
        self, level: Level, category: SchedCategory, unit: Phase | Stage
    ) -> bool:
        """Will dispatching this unit's drive run an agent subprocess? — the cap
        predicate (robustness UNIT 1). `execute()` walks every legal step in ONE
        task without re-entering the scheduler, so a drive is SPAWNING if ANY
        step it will reach this drive spawns, not only its entry step. Two
        sources, OR-ed:
          - the executor's `spawn_roles(unit)` for the CURRENT step (the single
            source the capacity governor uses — never re-derived);
          - RUNNABLE: a PENDING unit's entry step (dispatch bookkeeping) spawns
            nothing, but the SAME task immediately walks into SPEC/PLANNING which
            DOES spawn — so a RUNNABLE drive is always cap-bounded (without this
            the existing cap invariant breaks: N PENDING units would all dispatch
            and each spawn past the cap on its next step).
        Genuinely no-spawn drives — a BLOCKED unit still open/awaiting, or whose
        resolution routes to a terminal/blocked state — return False and are
        exempt. A BLOCKED drive whose resolution routes to a rework SPAWNING state
        is the one residual: `execute()` walks straight from the exempt no-spawn
        pickup INTO that rework spawn in the SAME task, spawning once past the cap
        within its walk (it is never added to `self._spawning`). The PER-UNIT
        overshoot is one agent, but the AGGREGATE is cap + K: with K such drives
        resolved in one tick, fresh spawners still fill the cap on top, so peak
        concurrent SPAWNING drives = cap + K. K is bounded by simultaneously-
        resolved rework escalations (≤ cap; the capacity governor's batch
        auto-resolve at a budget reset is the K≈cap case). ACCEPTED as an
        economic-cap residual: the cap protects the §7 process budget, not a hard
        safety limit (§8 — no heavier mechanism without an observed incident).
        Steady-state cap holds; the spike is transient (one tick) and the capacity
        governor re-holds if it re-trips the wall. Pinned by
        test_rework_routing_overshoots_cap_by_bounded_k_accepted_residual."""
        if category is SchedCategory.RUNNABLE:
            return True
        return bool(self._executors[level].spawn_roles(unit))

    def _spawning_count(self) -> int:
        """Live SPAWNING drives (the cap denominator). Snapshot-at-dispatch set,
        reconciled to `_tasks` so a reaped/finished drive frees its slot."""
        self._spawning &= set(self._tasks)
        return len(self._spawning)

    async def _drive(self, level: Level, unit_id: str) -> None:
        """Run one executor; unit-scoped failures are contained here (§6) so
        parallel siblings keep running; bookkeeping feeds the edge trigger."""
        try:
            await self._executors[level].execute(unit_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — §6 containment boundary
            self._contain_failure(level, unit_id, exc)
        finally:
            conn = self._db.read()
            key = (level, unit_id)
            # Robustness UNIT 1: release the agent-slot the moment this spawning
            # drive ends (it spawns sequentially within this one task, so no
            # agent of THIS unit is live past here) — `_reap` only runs at the
            # next tick top, so discard here too to free the slot promptly.
            self._spawning.discard(key)
            self._last_seq[key] = _max_event_seq(conn)
            self._blocked_snapshot[key] = (
                _open_escalation_count(conn, level.value, unit_id),
                _pending_decision_count(conn, level.value, unit_id),
            )

    def _contain_failure(self, level: Level, unit_id: str, exc: Exception) -> None:
        """§6: executor error -> unit ESCALATED (trigger='internal_error',
        traceback artifact under ndjson_log_dir); where the transition table
        has no ESCALATED edge the escalation row + event still land. A failure
        INSIDE this handler propagates — that is orchestrator-scoped (§6)."""
        log_dir = _resolve(self._cfg.factory.home, self._cfg.process.ndjson_log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        trace_path = log_dir / f"{new_id('error')}.traceback.txt"
        trace_path.write_text(
            "".join(traceback.format_exception(exc)), encoding="utf-8"
        )
        target = "phase_architect" if level is Level.STAGE else "main_architect"
        payload = {
            "error": repr(exc),
            "error_type": type(exc).__name__,
            "traceback_path": str(trace_path),
        }

        def coupled(conn: sqlite3.Connection) -> None:
            fdb.insert_event(
                conn,
                unit_level=level.value,
                unit_id=unit_id,
                event_type="internal_error",
                actor=_ACTOR,
                payload=payload,
            )
            if not fdb.open_escalation(conn, level.value, unit_id, "internal_error"):
                fdb.insert_escalation(
                    conn,
                    Escalation(
                        id=None,
                        unit_level=level.value,
                        unit_id=unit_id,
                        trigger="internal_error",
                        target=target,
                        payload_artifact_id=None,
                        event_seq=None,
                        status="open",
                        resolution=None,
                        created_at=utc_now(),
                        resolved_at=None,
                    ),
                )

        escalated_state = (
            StageState.ESCALATED.value if level is Level.STAGE else PhaseState.ESCALATED.value
        )
        try:
            self._sm.transition(
                level,
                unit_id,
                escalated_state,
                actor=_ACTOR,
                reason=f"internal error contained at executor boundary: {exc!r}",
                payload=payload,
                coupled=coupled,
            )
        except TransitionError:
            # No ESCALATED edge from the current state (e.g. PENDING/terminal):
            # the escalation row + event still land — visible, never silent.
            with self._db.transaction() as conn:
                coupled(conn)

    # ------------------------------------------------------------------ alerts

    async def _decision_latency_alerts(self) -> None:
        """§2 decision-latency trigger: pending decisions never alerted and older
        than escalation.decision_latency_alert_h -> ntfy (priority_alert), then
        mark_decision_alerted so the alert never re-fires every tick (CCR-1)."""
        stale = fdb.pending_decisions(
            self._db.read(),
            unalerted_older_than_h=self._cfg.escalation.decision_latency_alert_h,
        )
        for decision in stale:
            assert decision.id is not None
            streak_key = ("decision_latency", decision.id)
            try:
                await self._notify.publish(
                    "Decizie în așteptare de prea mult timp: "
                    f"{decision.unit_level} {decision.unit_id}",
                    link=dashboard_link(self._cfg, f"decision/{decision.id}"),
                    priority=self._notify.priority_alert,
                )
            except NotifyError as exc:
                if streak_key not in self._delivery_failed_logged:
                    self._delivery_failed_logged.add(streak_key)
                    with self._db.transaction() as conn:
                        fdb.insert_event(
                            conn,
                            unit_level="factory",
                            unit_id=None,
                            event_type="alert_delivery_failed",
                            actor=_ACTOR,
                            payload={
                                "kind": "decision_latency",
                                "decision_request_id": decision.id,
                                "error": str(exc),
                            },
                        )
                continue  # unmarked -> retried next tick until delivered
            self._delivery_failed_logged.discard(streak_key)
            with self._db.transaction() as conn:
                fdb.mark_decision_alerted(conn, decision.id, utc_now())
                fdb.insert_event(
                    conn,
                    unit_level=decision.unit_level,
                    unit_id=decision.unit_id,
                    event_type="alert",
                    actor=_ACTOR,
                    payload={
                        "kind": "decision_latency",
                        "decision_request_id": decision.id,
                        "alert_after_h": self._cfg.escalation.decision_latency_alert_h,
                    },
                )

    async def _publish_pending_decisions(self) -> None:
        """Per-tick re-publish backstop (founder 20-06): page every pending decision
        whose published_at IS NULL — those whose immediate publish hit a transient
        ntfy 429 (or that predate this code / a restart). DB-backed (published_at),
        so the retry survives a restart. On success, stamp published_at (drops it
        from the set); on failure, log ONE 'alert_delivery_failed' per consecutive-
        failure streak and leave it unpublished for the next tick — the exact
        _decision_latency_alerts contract, on a DISTINCT signal (published_at, NOT
        the 24h alerted_at latch). The happy path never reaches here: _publish_decision
        stamps published_at on its own successful publish, so a delivered gate is
        skipped; only a failed/never-attempted page lands in this worklist."""
        pending = fdb.pending_unpublished_decisions(self._db.read())
        for decision in pending:
            assert decision.id is not None
            streak_key = ("decision_publish", decision.id)
            try:
                await self._notify.publish(
                    f"Decizie necesară: {decision.unit_level} {decision.unit_id}",
                    link=dashboard_link(self._cfg, f"decision/{decision.id}"),
                    priority=self._notify.priority_decision,
                )
            except NotifyError as exc:
                if streak_key not in self._delivery_failed_logged:
                    self._delivery_failed_logged.add(streak_key)
                    with self._db.transaction() as conn:
                        fdb.insert_event(
                            conn,
                            unit_level="factory",
                            unit_id=None,
                            event_type="alert_delivery_failed",
                            actor=_ACTOR,
                            payload={
                                "kind": "decision_publish",
                                "decision_request_id": decision.id,
                                "error": str(exc),
                            },
                        )
                continue  # unpublished -> retried next tick until delivered
            self._delivery_failed_logged.discard(streak_key)
            with self._db.transaction() as conn:
                fdb.mark_decision_published(conn, decision.id, utc_now())

    async def _notify_architect(
        self, title: str, *, link: str | None, streak_key: object, context: dict
    ) -> bool:
        """Page the architect (robustness UNIT 2, reused by UNIT 3). There is ONE
        ntfy topic (the founder's, D-0004); the architect signal is a DISTINCT
        ``[arhitect]`` title prefix on that same topic — the founder relays it and
        a phone watcher disambiguates, while the GREPPABLE event the caller writes
        is the architect monitor's machine signal. Delivery failure NEVER raises:
        it logs ONE ``alert_delivery_failed`` event per consecutive-failure streak
        (``streak_key`` in ``_delivery_failed_logged``) and returns ``False`` so the
        caller leaves its own latch un-set and retries next tick — the exact
        ``_decision_latency_alerts`` contract. Returns ``True`` on delivery."""
        try:
            await self._notify.publish(
                f"[arhitect] {title}",
                link=link,
                priority=self._notify.priority_alert,
            )
        except NotifyError as exc:
            if streak_key not in self._delivery_failed_logged:
                self._delivery_failed_logged.add(streak_key)
                with self._db.transaction() as conn:
                    fdb.insert_event(
                        conn,
                        unit_level="factory",
                        unit_id=None,
                        event_type="alert_delivery_failed",
                        actor=_ACTOR,
                        payload=context | {"error": str(exc)},
                    )
            return False
        self._delivery_failed_logged.discard(streak_key)
        return True

    async def _stuck_escalation_detector(
        self, scan: list[tuple[Level, str, SchedCategory, Phase | Stage]]
    ) -> None:
        """Robustness UNIT 2 (D-0042, founder-approved MECHANICAL layer): make
        ``escalations.target`` a live routing signal and guarantee no escalation
        sits silently. Three pure-DB-predicate behaviors, all detection + notify;
        the ONLY mutation is a target relabel (``bump_escalation_target``). It
        NEVER resolves, transitions a unit, or spawns an agent — the founder's
        no-resolver-agent mandate (falsifiable: any status flip / transition /
        spawn here is an overstep).

          1. FIRST-NOTICE (Q2, the ≤5-min code law): the moment an architect-
             targeted escalation (target ∈ {phase_architect, main_architect}) is
             seen ``open`` and un-notified — age 0, before any threshold — emit
             ``escalation_opened_notice`` + one ``[arhitect]`` page. This makes
             "the architect learns within ≤5 min" a CODE law surviving a dead
             session monitor (the whole point of replacing the session-monitor
             cârpă). founder-targeted escalations are NOT first-noticed here (they
             are the founder's domain via the trade-off-card path, not the
             architect's first-notice channel).
          2a. OPEN-TOO-LONG: ``open`` whose ``created_at`` age says it should sit
             HIGHER on the ladder than its current ``target`` -> bump straight to
             the age-derived rung, page that rung, emit ``escalation_bumped``. The
             expected rung is ``min(age // threshold, len(ladder)-1)``: one
             threshold old -> ``main_architect``, two -> ``founder``, then clamps.
             STATELESS (no per-episode latch): the comparison ``current_idx <
             expected_idx`` IS the latch — once ``target == ladder[expected_idx]``
             it won't re-fire until age crosses the NEXT threshold, and at founder
             ``expected_idx`` is pinned at the cap so it never re-pages. The climb
             reaches the founder (the durability point: the founder is the backstop
             when the architect session is dead, D-0042) yet never cascades — at
             most one bump per escalation per tick, and the persisted target (which
             survives restart) replaces the old in-memory ``_escalation_bumped_at``
             as both the climb latch and the delivery-retry guard.
          2b. RESOLVED-NOT-ADVANCED (the incident-[20] pin): ``resolved`` with
             ``resolved_at`` older than the threshold AND the unit STILL in
             ``ESCALATED`` (the resolution never got picked up) -> page the current
             ``target`` + emit ``escalation_stuck_resolved``, ONCE per episode. The
             row is already resolved; the silence is what's wrong — do NOT
             re-resolve / re-create / transition (UNIT 1 fixes the pickup cause;
             this is the loud backstop, D-0042 "nothing sits silently >30min").

        Distinct ``escalation_*`` event kinds keep this off the stall detector's
        turf (which only fires when there are ZERO open escalations) and out of the
        dashboard "Ultimul incident" thrash. The architect's session monitor greps
        these events (≤5-min via its 45s poll); the ``[arhitect]`` ntfy is the human
        backstop. Latches self-prune to the live read so a re-opened escalation
        (fresh id) re-arms cleanly."""
        threshold = self._cfg.escalation.stuck_escalation_threshold_min
        # ONE wall-clock snapshot for the whole tick — the SAME source the DB age
        # filters use (datetime.now(UTC) vs the created_at written by utc_now), so
        # the (2a) climb shares one clock with no intra-tick skew. Tests drive an
        # old created_at exactly like the decision-latency path.
        now = datetime.now(UTC)
        conn = self._db.read()
        open_now = fdb.list_escalations_by_status(conn, "open")
        open_ids = {e.id for e in open_now}

        # --- (1) first-notice: architect-targeted open escalations, un-notified.
        for esc in open_now:
            assert esc.id is not None
            if esc.target not in ("phase_architect", "main_architect"):
                continue  # founder-rung escalations are not the architect's first-notice
            if esc.id in self._escalation_opened_notified:
                continue
            streak_key = ("escalation_opened_notice", esc.id)
            delivered = await self._notify_architect(
                f"escaladare nesemnalată către {esc.target}: {esc.unit_level} {esc.unit_id}",
                link=dashboard_link(self._cfg, "acum"),
                streak_key=streak_key,
                context={"kind": "escalation_opened_notice", "escalation_id": esc.id},
            )
            if not delivered:
                continue  # un-latched -> retried next tick until the page lands
            self._escalation_opened_notified.add(esc.id)
            with self._db.transaction() as tx:
                fdb.insert_event(
                    tx,
                    unit_level=esc.unit_level,
                    unit_id=esc.unit_id,
                    event_type="escalation_opened_notice",
                    actor=_ACTOR,
                    payload={"escalation_id": esc.id, "target": esc.target},
                )

        # --- (2a) open-too-long: STATELESS age-derived climb. Bump straight to the
        # rung the escalation's AGE says it should be at; the persisted target is
        # the latch (no _escalation_bumped_at). One threshold old -> main_architect,
        # two -> founder, then clamps. The founder is the durable backstop when the
        # architect session is dead (D-0042). At most one bump per esc per tick, and
        # it won't re-fire until age crosses the NEXT threshold.
        ladder = ESCALATION_TARGET_LADDER
        for esc in fdb.list_escalations_by_status(conn, "open", older_than_min=threshold):
            assert esc.id is not None
            try:
                current_idx = ladder.index(esc.target)
            except ValueError:
                continue  # unknown target (off-ladder) -> never guess forward, no-op
            # Age in whole minutes from the SAME wall clock the DB filter used.
            age_min = max(
                0,
                int(
                    (
                        now
                        - datetime.strptime(esc.created_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                            tzinfo=UTC
                        )
                    ).total_seconds()
                    // 60
                ),
            )
            expected_idx = min(age_min // threshold, len(ladder) - 1)
            if current_idx >= expected_idx:
                continue  # already at/above where its age says -> implicit latch + clamp
            new_target = ladder[expected_idx]
            streak_key = ("escalation_bumped", esc.id)
            delivered = await self._notify_architect(
                f"escaladare blocată de peste {threshold} min, ridicată la "
                f"{new_target}: {esc.unit_level} {esc.unit_id}",
                link=dashboard_link(self._cfg, "acum"),
                streak_key=streak_key,
                context={"kind": "escalation_bumped", "escalation_id": esc.id},
            )
            if not delivered:
                continue  # page failed -> target un-bumped, no event -> retried next tick
            with self._db.transaction() as tx:
                # Target relabel ONLY; status/resolution untouched.
                fdb.bump_escalation_target(tx, esc.id, new_target)
                fdb.insert_event(
                    tx,
                    unit_level=esc.unit_level,
                    unit_id=esc.unit_id,
                    event_type="escalation_bumped",
                    actor=_ACTOR,
                    payload={
                        "escalation_id": esc.id,
                        "from_target": esc.target,
                        "to_target": new_target,
                        "threshold_min": threshold,
                    },
                )

        # --- (2b) resolved-but-unit-still-ESCALATED: page once, NEVER mutate.
        # SCOPE to each unit's MOST-RECENT escalation (case-2b over-fire fix,
        # ETAPA-5f): an OLDER resolved escalation of a unit re-ESCALATED for a NEWER
        # reason is superseded by that newer escalation (open -> covered by (2a)/
        # first-notice; another resolved -> that one is the live episode), NOT a
        # genuine stuck-resolved. Without this, EVERY old resolved escalation of a
        # currently-ESCALATED unit matched (resolved + old + unit ESCALATED) and
        # paged once each — a flood (~32 false [arhitect] pages in production:
        # register-schemas, 4-resolution history, re-ESCALATED on a new budget breach).
        latest_by_unit = fdb.latest_escalation_ids_by_unit(conn)
        resolved_ids: set[int] = set()
        for esc in fdb.list_escalations_by_status(conn, "resolved", older_than_min=threshold):
            assert esc.id is not None
            if esc.id != latest_by_unit.get((esc.unit_level, esc.unit_id)):
                continue  # superseded by a newer escalation -> not this unit's live episode
            unit = (
                fdb.get_stage(conn, esc.unit_id)
                if esc.unit_level == Level.STAGE.value
                else fdb.get_phase(conn, esc.unit_id)
            )
            if unit is None or unit.state.value != "ESCALATED":
                continue  # the unit advanced (or vanished) -> the resolution landed
            resolved_ids.add(esc.id)
            if esc.id in self._escalation_stuck_resolved_notified:
                continue
            streak_key = ("escalation_stuck_resolved", esc.id)
            delivered = await self._notify_architect(
                f"escaladare rezolvată dar neavansată de peste {threshold} min: "
                f"{esc.unit_level} {esc.unit_id}",
                link=dashboard_link(self._cfg, "acum"),
                streak_key=streak_key,
                context={"kind": "escalation_stuck_resolved", "escalation_id": esc.id},
            )
            if not delivered:
                continue  # un-latched -> retried next tick until the page lands
            self._escalation_stuck_resolved_notified.add(esc.id)
            with self._db.transaction() as tx:
                fdb.insert_event(
                    tx,
                    unit_level=esc.unit_level,
                    unit_id=esc.unit_id,
                    event_type="escalation_stuck_resolved",
                    actor=_ACTOR,
                    payload={
                        "escalation_id": esc.id,
                        "target": esc.target,
                        "threshold_min": threshold,
                    },
                )

        # Self-prune the latches to the live read: an escalation that closed
        # (no longer open) or whose unit advanced (no longer a stuck-resolved
        # episode) drops out, so a future re-open with a fresh id re-arms. The
        # (2a) climb keeps NO latch (stateless — the persisted target is the
        # latch), so nothing to prune for it.
        self._escalation_opened_notified &= open_ids
        self._escalation_stuck_resolved_notified &= resolved_ids

    async def _stall_detector(
        self, scan: list[tuple[Level, str, SchedCategory, Phase | Stage]]
    ) -> None:
        """§4 STALL DETECTOR: non-terminal units exist, nothing RUNNABLE/RUNNING,
        and no open decision_request/escalation -> 'alert' event + ntfy. Fires
        once per stall episode (the latch clears when anything moves again) —
        a wedged factory pages, it does not page every tick."""
        non_terminal = [
            entry
            for entry in scan
            if entry[2] not in (SchedCategory.TERMINAL_OK, SchedCategory.TERMINAL_FAIL)
        ]
        stalled = bool(non_terminal) and not any(
            category in (SchedCategory.RUNNABLE, SchedCategory.RUNNING)
            for _, _, category, _ in non_terminal
        )
        if stalled:
            conn = self._db.read()
            if fdb.pending_decisions(conn) or _total_open_escalations(conn) > 0:
                stalled = False
        if not stalled:
            self._stall_event_logged = False
            self._stall_published = False
            self._delivery_failed_logged.discard("stall")
            return
        wedged = [f"{level.value}:{unit_id}" for level, unit_id, _, _ in non_terminal]
        if not self._stall_event_logged:
            self._stall_event_logged = True
            with self._db.transaction() as conn:
                fdb.insert_event(
                    conn,
                    unit_level="factory",
                    unit_id=None,
                    event_type="alert",
                    actor=_ACTOR,
                    payload={"kind": "stall", "non_terminal_units": wedged[:100]},
                )
        if not self._stall_published:
            try:
                await self._notify.publish(
                    "Fabrica este blocată: nicio unitate nu poate avansa",
                    link=dashboard_link(self._cfg, "acum"),
                    priority=self._notify.priority_alert,
                )
                self._stall_published = True
                self._delivery_failed_logged.discard("stall")
            except NotifyError as exc:
                if "stall" not in self._delivery_failed_logged:
                    self._delivery_failed_logged.add("stall")
                    with self._db.transaction() as conn:
                        fdb.insert_event(
                            conn,
                            unit_level="factory",
                            unit_id=None,
                            event_type="alert_delivery_failed",
                            actor=_ACTOR,
                            payload={"kind": "stall", "error": str(exc)},
                        )

    # ------------------------------------------------- dashboard supervisor (CCR-3)

    async def _dashboard_supervisor(self) -> None:
        """Containment + restart loop for the dashboard serve() task (design
        §6/§7 row 1): contains ALL exceptions per iteration — NOTHING ever
        escapes into the TaskGroup except cancellation. Paging is deduplicated
        (first crash, then every dashboard.page_every_n_restarts-th; an 'alert'
        event lands on EVERY restart, restart counter in the payload). Between
        restarts the bind is re-checked every dashboard.bind_recheck_s (§7
        IP-drift row): a drifted tailscale IP restarts serve() to re-resolve."""
        dashboard = self._dashboard
        assert dashboard is not None
        dcfg = self._cfg.founder_channel.dashboard
        while True:
            serve_task = asyncio.create_task(dashboard.serve())
            reason = "dashboard serve() returned unexpectedly"
            try:
                while True:
                    done, _pending = await asyncio.wait(
                        {serve_task}, timeout=dcfg.bind_recheck_s
                    )
                    if done:
                        exc = serve_task.exception()
                        if exc is not None:
                            reason = repr(exc)
                        break
                    drift = await self._dashboard_bind_drift(dashboard)
                    if drift is not None:
                        reason = drift
                        serve_task.cancel()
                        await self._reap_serve_task(serve_task)
                        break
            except asyncio.CancelledError:
                serve_task.cancel()
                with contextlib.suppress(BaseException):
                    await serve_task
                raise
            except Exception as exc:  # noqa: BLE001 — supervisor's own defect: contained
                reason = f"dashboard supervisor internal error: {exc!r}"
                serve_task.cancel()
                await self._reap_serve_task(serve_task)
            self._dashboard_restarts += 1
            await self._dashboard_crash_alert(reason)
            try:
                await asyncio.sleep(dcfg.restart_delay_s)
            except asyncio.CancelledError:
                raise

    @staticmethod
    async def _reap_serve_task(serve_task: asyncio.Task) -> None:
        """Await a just-cancelled serve task, suppressing only ITS outcome —
        its CancelledError result or any crash. The supervisor's OWN
        cancellation, if it lands during this await, re-raises
        (current_task().cancelling() > 0): Scheduler._run's finally calls
        supervisor.cancel() exactly once, so a suppress(BaseException) here
        could swallow that single cancel on the drift / internal-error
        branches, keep the loop running and leave the TaskGroup never closing
        (shutdown hang)."""
        try:
            await serve_task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling() > 0:
                raise  # the supervisor itself is being cancelled — propagate
        except Exception:  # noqa: BLE001 — serve_task's crash: contained (§7 row 1)
            pass

    async def _dashboard_bind_drift(self, dashboard: DashboardServer) -> str | None:
        """§7 IP-drift row: re-resolve the bind host (off-loop — `tailscale ip`
        is a subprocess) and compare against bound_address; mismatch OR a
        resolve failure = drift (restart re-resolves, loudly). Never raises."""
        bound = dashboard.bound_address
        if bound is None:
            return None  # not bound (serve restarting) — nothing to compare
        try:
            current = await asyncio.to_thread(resolve_bind_host, self._cfg)
        except Exception as exc:  # noqa: BLE001 — drift check must never escape
            return f"dashboard bind re-check failed: {exc!r}"
        if current != bound[0]:
            return (
                f"dashboard bind address drifted: bound {bound[0]!r},"
                f" resolved {current!r}"
            )
        return None

    async def _dashboard_crash_alert(self, reason: str) -> None:
        """'alert' event on EVERY restart (audit trail) + DEDUPLICATED
        max-priority page (first crash, then every Nth — alarm fatigue degrades
        the DoD §9 minimal-attention channel). The publish follows the §6
        NotifyError contract: 'alert_delivery_failed' event, NEVER re-raise —
        an unwrapped publish would tear down the orchestrator exactly when the
        founder channel is already down. The guard is `except Exception`
        (hardening beyond the declared §6 NotifyError): a publisher defect of
        any type is contained the same way, never escaping into the TaskGroup
        (§7 row 1's 'nothing ever escapes'). DB failures degrade to stderr —
        the supervisor never lets anything escape (§7 row 1)."""
        restarts = self._dashboard_restarts
        try:
            with self._db.transaction() as conn:
                fdb.insert_event(
                    conn,
                    unit_level="factory",
                    unit_id=None,
                    event_type="alert",
                    actor=_ACTOR,
                    payload={
                        "kind": "dashboard_crashed",
                        "reason": reason,
                        "restarts": restarts,
                    },
                )
        except Exception as exc:  # noqa: BLE001 — containment boundary (§7 row 1)
            print(
                f"sf-factory: dashboard crash event write failed: {exc!r}",
                file=sys.stderr,
            )
        every_n = self._cfg.founder_channel.dashboard.page_every_n_restarts
        if not (restarts == 1 or restarts % every_n == 0):
            return
        try:
            await self._notify.publish(
                "Dashboard căzut — decizia pe telefon nu funcționează;"
                " fallback: cli decide",
                link=dashboard_link(self._cfg, "acum"),
                priority=self._notify.priority_alert,
            )
        except Exception as exc:  # noqa: BLE001 — any publisher defect: contained (§7 row 1)
            try:
                with self._db.transaction() as conn:
                    fdb.insert_event(
                        conn,
                        unit_level="factory",
                        unit_id=None,
                        event_type="alert_delivery_failed",
                        actor=_ACTOR,
                        payload={
                            "kind": "dashboard_crashed",
                            "restarts": restarts,
                            "error": str(exc),
                        },
                    )
            except Exception as db_exc:  # noqa: BLE001 — never escape (§7 row 1)
                print(
                    f"sf-factory: alert_delivery_failed write failed: {db_exc!r}",
                    file=sys.stderr,
                )


# ------------------------------------------------------------ process predicates


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _proc_cmdline_matches(pid: int, recorded_cmdline: str) -> bool:
    """§5.5a 'pid alive with matching cmdline': compare /proc/<pid>/cmdline with
    the registry cmdline (recorded as shlex.join(argv) at spawn). Unreadable
    /proc => no match — never kill what cannot be identified.

    Delegates to the PUBLIC ``runner.cmdline_matches`` (promoted by CCR-3,
    closing the D-0016 disposition) — the single tolerant predicate (D-0014
    item 2): interpreter wrapping (codex = ``#!/usr/bin/env node`` script,
    observed live as ``node /…/bin/codex …``) breaks strict equality, and a
    drifting second copy here would silently exempt the §5.5a orphan sweep from
    that fix (Doctrine §9: index -> source)."""
    return cmdline_matches(pid, recorded_cmdline)
