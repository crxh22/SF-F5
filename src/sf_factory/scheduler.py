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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from sf_factory import db as fdb
from sf_factory.artifacts import (
    PHASE_ARTIFACTS,
    STAGE_ARTIFACTS,
    detect_sentinels,
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
    GATE_ANSWERS,
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
from sf_factory.statemachine import StateMachine
from sf_factory.thresholds import ThresholdEvaluator
from sf_factory.worktrees import StaleGateError, WorktreeManager, commit_paths, run_git

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
RESUME_VERIFIED_CLIS = frozenset({"claude", "stub"})

#: Sentinel artifact kind -> (filename, events.event_type, escalations.trigger).
_SENTINEL_EVENTS: Mapping[str, tuple[str, str]] = {
    "declared_failure": ("declared_failure", Trigger.AGENT_DECLARED_FAILURE.value),
    "contract_change_request": (
        "contract_change_request",
        Trigger.CONTRACT_CHANGE_REQUEST.value,
    ),
}

#: ESCALATED-exit resolution vocabulary (§2 escalations.resolution examples):
#: resolution string -> target state per level. Anything else = unknown ->
#: explicit 'alert' event, unit stays put (never guessed, Doctrine §7).
_STAGE_RESOLUTIONS: Mapping[str, StageState] = {
    "rework:BUILD": StageState.BUILD,
    "rework:SPEC": StageState.SPEC,
    "respec": StageState.SPEC,
    "rework:VALIDATE": StageState.VALIDATE,
    "awaiting_human": StageState.AWAITING_HUMAN,
    "failed": StageState.FAILED,
    "cancelled": StageState.CANCELLED,
}
_PHASE_RESOLUTIONS: Mapping[str, PhaseState] = {
    "replan": PhaseState.PLANNING,
    "resume": PhaseState.RUNNING,
    "awaiting_human": PhaseState.AWAITING_HUMAN,
    "failed": PhaseState.FAILED,
    "cancelled": PhaseState.CANCELLED,
}

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


def _builder_role(cfg: FactoryConfig, risk_class: str) -> str:
    """Builder route per risk class. Convention over config (the risk_classes
    section declares validator+audits only): prefer the models key
    'builder_<risk_class>' when declared, else the conservative
    'builder_heavy'. Raises ConfigError when neither route exists."""
    candidate = f"builder_{risk_class}"
    if candidate in cfg.models:
        return candidate
    if "builder_heavy" in cfg.models:
        return "builder_heavy"
    raise ConfigError(
        f"no builder route for risk class {risk_class!r}: neither models.{candidate}"
        " nor models.builder_heavy is configured"
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
                        link=dashboard_link(self._cfg, "health"),
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

    def _stderr_tail(self, stderr_path: str) -> str:
        """Last ~2KB of the agent's stderr file; missing/unreadable = '' — a
        tail read must never fail a step (the file may not exist for fakes)."""
        try:
            with open(stderr_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(size - _USAGE_LIMIT_STDERR_TAIL_BYTES, 0))
                return fh.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _match(self, result: AgentResult) -> str | None:
        """First configured signature found in result_text + the stderr tail
        (config validates signatures lowercase; lowercasing the haystack makes
        the match case-insensitive)."""
        haystack = (
            result.result_text + "\n" + self._stderr_tail(result.stderr_path)
        ).lower()
        for signature in self._cfg.founder_channel.usage_limit_signatures:
            if signature in haystack:
                return signature
        return None

    async def check(
        self, result: AgentResult, *, unit_level: str, unit_id: str, role: str
    ) -> None:
        """Scan one agent result; page on a match (streak-deduplicated)."""
        signature = self._match(result)
        if signature is None:
            # Clean observation ends the streak: a future match re-pages.
            self._event_logged = False
            self._published = False
            self._delivery_failed_logged = False
            return
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
                    link=dashboard_link(self._cfg, "health"),
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


# ------------------------------------------------------------- frozen interfaces


class UnitExecutor(Protocol):
    """Per-level step-sequence driver (design §4)."""

    level: Level

    async def execute(self, unit_id: str) -> None:
        """Drive one unit from its current state until BLOCKED or terminal; every step:
        run agent, register artifacts, evaluate thresholds first, CP-1 only when
        thresholds do not decide, transition."""
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
    ) -> None:
        """Wires the stage conveyor; no policy outside config."""
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

    # ---------------------------------------------------------------- protocol

    async def execute(self, unit_id: str) -> None:
        """Drive one stage until BLOCKED, terminal, or no further progress is
        possible without external input; each step follows the §4 contract."""
        steps = {
            StageState.PENDING: self._step_dispatch,
            StageState.SPEC: self._step_spec,
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
            if not await step(stage):
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
        )
        # CCR-6: capacity-event scan before anything consumes the result — a
        # usage-limited CLI exits "successfully" with a refusal text, and the
        # founder asked to be paged on the first suspicion (D-0021 class).
        await self._usage_limit.check(
            result, unit_level=Level.STAGE.value, unit_id=stage.id, role=role
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
        return result

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
            f"unit/stage/{stage.id}",
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
        await self._run_step_agent(
            stage, "spec_agent", self._spec_prompt(stage, phase, worktree), cwd=worktree
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
        self._sm.transition(
            Level.STAGE,
            stage.id,
            StageState.BUILD.value,
            actor=_ACTOR,
            reason="spec artifact registered",
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
        the entry transition payload), commit-all, churn recording, -> VALIDATE."""
        worktree = self._worktree(stage)
        await self._assert_no_unregistered_files(stage, worktree)

        conn = self._db.read()
        role = _builder_role(self._cfg, stage.risk_class)
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

        await self._run_step_agent(
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
        if sha is None and not churn_diff:
            raise ArtifactContractError(
                f"builder produced no committed changes for stage {stage.id}"
                " and declared no failure"
            )
        notes_path = self._unit_dir(worktree, stage) / STAGE_ARTIFACTS["build_notes"]

        def coupled(tx: sqlite3.Connection) -> None:
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
            role = _builder_role(self._cfg, stage.risk_class)
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
                    payload=base_payload | {"executed_as": "rebuild"},
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
                payload=base_payload,
            )
            return True
        if value == "respec":
            self._sm.transition(
                Level.STAGE,
                stage.id,
                StageState.SPEC.value,
                actor=_ACTOR,
                reason="CP-1 verdict respec",
                payload=base_payload,
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
        builder = _builder_role(self._cfg, stage.risk_class)
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
                payload={"contested": [f.finding_ref for f in contested]},
                coupled=couple_statuses,
            )
            return False
        if complied:
            self._sm.transition(
                Level.STAGE,
                stage.id,
                StageState.BUILD.value,
                actor=_ACTOR,
                reason="executor complies with audit finding(s) — rework",
                payload={"complied": [f.finding_ref for f in complied]},
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
        """Publish a decision request (priority_decision); delivery failure =
        'alert_delivery_failed' event, state unchanged (§6 NotifyError row)."""
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
        target = _STAGE_RESOLUTIONS.get(last.resolution or "")
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
                        "known": sorted(_STAGE_RESOLUTIONS),
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

        self._sm.transition(
            Level.STAGE,
            stage.id,
            target.value,
            actor="phase_architect",
            reason=f"escalation resolved: {last.resolution}",
            payload={"escalation_id": last.id, "resolution": last.resolution},
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
            if not sidecar.is_file():
                raise ArtifactContractError(
                    f"integration validator produced no findings sidecar at {sidecar}"
                )
            findings = _read_findings_sidecar(sidecar, auditor_role="integration_validator")
            # Only the report crosses into the stage worktree (§3.1 isolation).
            unit_dir = self._unit_dir(worktree, stage)
            unit_dir.mkdir(parents=True, exist_ok=True)
            copied = [unit_dir / report.name, unit_dir / sidecar.name]
            if report.is_file():
                shutil.copyfile(report, copied[0])
            else:
                copied[0].write_text("(no prose report)\n", encoding="utf-8")
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

    def _spec_prompt(self, stage: Stage, phase: Phase, worktree: Path) -> str:
        return (
            f"You are the Spec Agent for stage '{stage.id}' ({stage.name}), risk class "
            f"{stage.risk_class}, of phase '{phase.id}'.\n"
            f"Acceptance criteria: {self._acceptance_text(stage, phase, worktree)}\n"
            "Contracts in force are read-only under _factory/contracts/.\n"
            f"Write the spec to _factory/stages/{stage.id}/spec.md — depth scaled to the "
            "risk class, test-first.\n" + self._layout_note(stage)
        )

    def _build_prompt(self, stage: Stage, worktree: Path, entry_payload: dict) -> str:
        unit_rel = f"_factory/stages/{stage.id}"
        context = ""
        reason = entry_payload.get("reason")
        if reason:
            context = f"\nRework context: {reason}."
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
            "before finishing (run what you can). Do NOT modify _factory/contracts/ "
            "(a needed change = _CONTRACT_CHANGE_REQUEST.md + stop). You may write "
            f"{unit_rel}/build-notes.md.{context}\n" + self._layout_note(stage)
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
            '"location": "..."}]} (empty list = clean).\n' + self._layout_note(stage)
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
        parts = [
            f"You are the Integration Validator at the merge gate of stage '{stage.id}' "
            "(clean context). Check contract conformance IN SUBSTANCE, cross-boundary "
            "invariant violations, duplicate/divergent implementations, and assumptions "
            "contradicted between units. Cite concrete locations.",
            "\n== CONTRACTS IN FORCE ==",
        ]
        for name, text in contracts.items():
            parts.append(f"--- {name} ---\n{_bounded(text, max_bytes)}")
        parts.append("\n== PHASE PLAN ==")
        for name, text in plan_texts.items():
            parts.append(f"--- {name} ---\n{_bounded(text, max_bytes)}")
        parts.append(f"\n== FULL DIFF OF GATING UNIT {stage.id} ==\n{full_diff}")
        parts.append("\n== FULL DIFFS OF SIBLINGS MERGED SINCE CONTRACT FREEZE ==")
        if sibling_diffs:
            for unit_id, diff in sorted(sibling_diffs.items()):
                parts.append(f"--- merged unit {unit_id} ---\n{diff}")
        else:
            parts.append("(none merged since contract freeze)")
        unit_rel = f"_factory/stages/{stage.id}"
        parts.append(
            f"\nWrite {unit_rel}/integration-report.md (prose) and "
            f"{unit_rel}/integration-report.json — EXACTLY "
            '{"findings": [{"ref": "...", "severity": "...", "summary": "...", '
            '"location": "..."}]} (empty list = no findings).\n' + self._layout_note(stage)
        )
        return "\n".join(parts)


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
    ) -> None:
        """Ingests phase-plan.json strictly via artifacts.read_phase_plan (schema +
        acyclicity validated BEFORE the CONTRACTS_FROZEN→RUNNING transition; failure =
        ArtifactContractError → escalation) into stages+dag_edges. An LLM-produced plan
        is never trusted unvalidated (Doctrine §7)."""
        self._db = db
        self._sm = sm
        self._cfg = cfg
        self._runner = runner
        self._wt = wt
        self._notify = notify
        #: CCR-6: usage-limit scan after the planning spawn.
        self._usage_limit = _UsageLimitDetector(db, cfg, notify)

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
        result = await self._runner.run_agent(
            "phase_architect",
            self._planning_prompt(phase),
            unit_level=Level.PHASE.value,
            unit_id=phase.id,
            cwd=worktree,
        )
        # CCR-6: capacity-event scan right after the spawn (same contract as
        # the stage conveyor's _run_step_agent).
        await self._usage_limit.check(
            result, unit_level=Level.PHASE.value, unit_id=phase.id, role="phase_architect"
        )
        if await self._detect_phase_sentinels(phase, worktree):
            return False
        unit_dir = self._unit_dir(worktree, phase)
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

        parts = [
            f"You are the Integration Validator at the integration gate of phase"
            f" '{phase.id}' (clean context). Check contract conformance in substance,"
            " cross-boundary invariants, duplicate/divergent implementations,"
            " contradicted assumptions. Cite concrete locations.",
            "\n== CONTRACTS IN FORCE ==",
            *(f"--- {n} ---\n{_bounded(t, max_bytes)}" for n, t in contracts.items()),
            "\n== PHASE PLAN ==",
            *(f"--- {n} ---\n{_bounded(t, max_bytes)}" for n, t in plan_texts.items()),
            f"\n== FULL DIFF OF PHASE {phase.id} vs {target} ==\n{full_diff}",
            "\n== FULL DIFFS OF UNITS MERGED INTO THE TARGET SINCE FORK ==",
        ]
        if sibling_diffs:
            parts += [
                f"--- merged unit {uid} ---\n{diff}"
                for uid, diff in sorted(sibling_diffs.items())
            ]
        else:
            parts.append("(none)")
        unit_rel = f"_factory/phases/{phase.id}"
        parts.append(
            f"\nWrite {unit_rel}/integration-report.md and {unit_rel}/integration-report.json"
            ' — EXACTLY {"findings": [{"ref": "...", "severity": "...", "summary": "...",'
            ' "location": "..."}]} (empty list = no findings). If you cannot proceed,'
            f" write {unit_rel}/_DECLARED_FAILURE.md instead of guessing."
        )

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
        target = _PHASE_RESOLUTIONS.get(last.resolution or "")
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
                        "known": sorted(_PHASE_RESOLUTIONS),
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

    # ----------------------------------------------------------------- prompts

    def _planning_prompt(self, phase: Phase) -> str:
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
        return (
            f"You are the Phase Architect for phase '{phase.id}' ({phase.name}).\n"
            + context
            + "Decompose the phase into stages sized at the upper bound of one-pass "
            "builder confidence; declare per-stage acceptance criteria and risk class; "
            "freeze the intra-phase contracts (shared schemas, API signatures, named "
            f"invariants) as files under {contracts_ns} BEFORE any fan-out.\n"
            f"Write {unit_rel}/phase-plan.md (rationale) and {unit_rel}/phase-plan.json — "
            'EXACTLY {"stages": [{"id": "<plan-local-id>", "name": "...", '
            '"risk_class": "<one of ' + "|".join(risk_classes) + '>", '
            '"acceptance": "..."}], "dag_edges": [["<from>", "<to>"]]} — '
            "ids unique, every edge endpoint declared, DAG acyclic.\n"
            f"If you cannot proceed, write {unit_rel}/_DECLARED_FAILURE.md; a cross-phase "
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
    ) -> None:
        """Level-agnostic loop over sched categories + dag_edges; max
        process.max_parallel_agents concurrent units. ``dashboard`` (CCR-3,
        optional, default None — tests/run_until_blocked unaffected): when
        present, ``_run`` hosts the contained ``_dashboard_supervisor``."""
        self._db = db
        self._sm = sm
        self._cfg = cfg
        self._executors = dict(executors)
        self._notify = notify
        self._dashboard = dashboard
        #: Total dashboard supervisor restarts (paging-dedup counter, design §6).
        self._dashboard_restarts = 0
        #: Internal worktree manager for recover()'s §5.5b git healing — a
        #: mechanics helper, not an executor dependency.
        self._wt = WorktreeManager(cfg)
        #: Phase-seeding design §5: out-of-bounds check during recover().
        self._oob = _OutOfBoundsDetector(db, cfg, notify)
        self._tasks: dict[tuple[Level, str], asyncio.Task] = {}
        #: max events.seq at the end of a unit's last drive — the re-dispatch
        #: edge trigger (a no-progress unit is not respun until facts change).
        self._last_seq: dict[tuple[Level, str], int] = {}
        #: (open escalations, pending decisions) snapshot at last drive end —
        #: wakes BLOCKED units on resolutions/answers even if the answering
        #: plumbing wrote no event.
        self._blocked_snapshot: dict[tuple[Level, str], tuple[int, int]] = {}
        self._stall_event_logged = False
        self._stall_published = False
        #: One alert_delivery_failed event per consecutive-failure streak (the
        #: retry itself continues every tick; only the event is deduplicated).
        self._delivery_failed_logged: set[object] = set()

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
                    link=dashboard_link(self._cfg, "health"),
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
                    await self._decision_latency_alerts()
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

    def _scan_units(self) -> list[tuple[Level, str, SchedCategory]]:
        """One categorized pass over all units (feeds dispatch + stall detector).

        Phase-seeding design §5b: the RUNNABLE selection applies the
        proving-phases dispatch hold at PHASE level — a held phase is
        categorized WAITING (state stays PENDING, never transitioned), so it is
        neither dispatched nor mistaken for progress by the stall detector."""
        conn = self._db.read()
        scan: list[tuple[Level, str, SchedCategory]] = []
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
                scan.append((level, unit.id, category))
        return scan

    def _dispatch(
        self, tg: asyncio.TaskGroup, scan: list[tuple[Level, str, SchedCategory]]
    ) -> int:
        """Dispatch eligible units up to the max_parallel_agents cap. RUNNABLE
        always dispatches; category-RUNNING re-dispatches when events advanced
        since its last drive (or it was never driven — crash resume, §5.5d);
        BLOCKED re-dispatches additionally when its open-escalation/pending-
        decision counts changed (answers may arrive without events)."""
        conn = self._db.read()
        cap = self._cfg.process.max_parallel_agents
        seq = _max_event_seq(conn)
        dispatched = 0
        for level, unit_id, category in scan:
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
                eligible = key not in self._last_seq or seq > self._last_seq[key]
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
            if len(self._tasks) >= cap:
                break  # economics cap (§7) — the rest waits for a free slot
            self._tasks[key] = tg.create_task(self._drive(level, unit_id))
            dispatched += 1
        return dispatched

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

    async def _stall_detector(
        self, scan: list[tuple[Level, str, SchedCategory]]
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
            for _, _, category in non_terminal
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
        wedged = [f"{level.value}:{unit_id}" for level, unit_id, _ in non_terminal]
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
                    link=dashboard_link(self._cfg, "health"),
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
                link=dashboard_link(self._cfg, "health"),
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
