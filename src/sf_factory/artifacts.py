"""Artifact path conventions, hashing, registration and contracts (design §1/§4).

Sole responsibility: artifact path conventions, sha256 hashing, registration
(the DB stores path+hash only, never content — DoD §2.8/§6), the sentinel /
validation-sidecar / phase-plan contracts, and integrity verification
(DoD §12.A2).

No git *state* operations live here (those are worktrees.py's single home);
the only git access is the read-only ``git cat-file`` resolution required by
the frozen ``verify_integrity`` contract.

May import: models, db (+ stdlib; pydantic per stack decision D-0007 — the
frozen ``PhasePlan`` contract is a pydantic model).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import subprocess
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, ValidationError

from sf_factory.db import (
    Database,
    find_artifact_ref,
    get_phase,
    get_stage,
    insert_artifact_ref,
    iter_latest_artifact_refs,
)
from sf_factory.models import (
    ArtifactContractError,
    ArtifactRef,
    FactoryError,
    IntegrityError,
    Level,
    PhaseState,
    StageState,
    ValidationSummary,
    utc_now,
)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------- path conventions

STAGE_ARTIFACTS: Mapping[str, str] = MappingProxyType(
    {
        "spec": "spec.md",
        "build_notes": "build-notes.md",
        "validation_report": "validation-report.md",
        "validation_sidecar": "validation-report.json",
        "audit_report": "audit-<role>.md",
        "declared_failure": "_DECLARED_FAILURE.md",
        "contract_change_request": "_CONTRACT_CHANGE_REQUEST.md",
    }
)
"""kind -> filename under _factory/stages/<stage_id>/: spec='spec.md',
build_notes='build-notes.md', validation_report='validation-report.md',
validation_sidecar='validation-report.json', audit_report='audit-<role>.md'
(callers substitute the auditing role for '<role>'), declared_failure=
'_DECLARED_FAILURE.md', contract_change_request='_CONTRACT_CHANGE_REQUEST.md'.
Layout is a frozen contract (referenced by role prompts), not a tunable —
changing it is a migration, not a config edit."""

PHASE_ARTIFACTS: Mapping[str, str] = MappingProxyType(
    {
        "phase_plan": "phase-plan.md",
        "phase_plan_sidecar": "phase-plan.json",
    }
)
"""kind -> filename under _factory/phases/<phase_id>/: phase_plan='phase-plan.md',
phase_plan_sidecar='phase-plan.json'; contracts live under _factory/contracts/.
Same frozen-contract status as STAGE_ARTIFACTS."""

#: Sentinel kinds detected by ``detect_sentinels`` — detection order is fixed.
_SENTINEL_KINDS: tuple[str, ...] = ("declared_failure", "contract_change_request")


def unit_artifact_dir(root: Path, level: Level, unit_id: str) -> Path:
    """_factory/stages/<id>/ or _factory/phases/<id>/ under the given repo/worktree root.

    Refuses unit ids that could escape the artifact tree (path separators,
    '..', empty) — fail-explicit at the path chokepoint, never a silent
    traversal (Doctrine §7).
    """
    level = Level(level)
    if not unit_id or "/" in unit_id or "\\" in unit_id or ".." in unit_id or unit_id == ".":
        raise FactoryError(f"unsafe unit id for artifact dir: {unit_id!r}")
    sub = "stages" if level is Level.STAGE else "phases"
    return Path(root) / "_factory" / sub / unit_id


# ----------------------------------------------------------- hashing + registry


def sha256_file(path: Path) -> str:
    """Streaming sha256 hex digest; raises IntegrityError if unreadable."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while chunk := fh.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise IntegrityError(f"cannot hash artifact file {path}: {exc}") from exc
    return digest.hexdigest()


def register_artifact(
    conn: sqlite3.Connection,
    *,
    unit_level: str,
    unit_id: str,
    kind: str,
    repo: str,
    repo_root: Path,
    path: Path,
    git_commit: str | None,
) -> ArtifactRef:
    """Hash file and GET-OR-CREATE the artifact_refs row (path+hash only, never content)
    in the caller's tx: on (repo, path, sha256) conflict return the EXISTING ref —
    byte-identical re-registration is normal operation (unchanged sidecar across fix
    iterations, crash-replayed steps) and must never abort the enclosing transition;
    per-iteration linkage lives in fix_iterations.report_artifact_id.

    ``path`` may be absolute or relative to ``repo_root``; the stored path is
    always POSIX-relative to the repo root (§2 DDL). A path outside the repo
    root is a caller bug -> FactoryError.
    """
    root = Path(repo_root).resolve()
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = root / file_path
    file_path = file_path.resolve()
    try:
        rel = file_path.relative_to(root)
    except ValueError as exc:
        raise FactoryError(
            f"artifact path {file_path} is not under repo root {root}"
        ) from exc
    digest = sha256_file(file_path)
    rel_posix = rel.as_posix()

    existing = find_artifact_ref(conn, repo, rel_posix, digest)
    if existing is not None:
        return existing

    ref = ArtifactRef(
        id=None,
        unit_level=unit_level,
        unit_id=unit_id,
        kind=kind,
        repo=repo,
        path=rel_posix,
        sha256=digest,
        git_commit=git_commit,
        created_at=utc_now(),
    )
    ref_id = insert_artifact_ref(conn, ref)
    return replace(ref, id=ref_id)


# --------------------------------------------------------- phase-plan contract

#: Plan stage ids feed branch names ('stage/<id>') and artifact dirs — the id
#: grammar is part of the malformed-plan rejection (read_phase_plan).
_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PhasePlanStage(BaseModel):
    """One stage row of phase-plan.json: id, name, risk_class, acceptance (criteria text)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    risk_class: str
    acceptance: str


class PhasePlan(BaseModel):
    """Schema of phase-plan.json: stages[{id, name, risk_class, acceptance}],
    dag_edges[[from_id, to_id]]; extra='forbid'."""

    model_config = ConfigDict(extra="forbid")

    stages: list[PhasePlanStage]
    dag_edges: list[tuple[str, str]]


def read_phase_plan(path: Path, risk_classes: Collection[str]) -> PhasePlan:
    """Validate the LLM-produced plan BEFORE any scheduler ingestion: unique stage ids,
    risk_class ∈ risk_classes, every edge endpoint declared, DAG acyclic (toposort).
    Malformed or cyclic → ArtifactContractError → escalation — same contract as the
    validation sidecar; an unvalidated cyclic plan would leave all units WAITING
    forever with the watchdog green (Doctrine §20's silent slow death)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ArtifactContractError(f"cannot read phase plan {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ArtifactContractError(f"phase plan {path} is not valid JSON: {exc}") from exc
    try:
        plan = PhasePlan.model_validate(data)
    except ValidationError as exc:
        raise ArtifactContractError(f"phase plan {path} violates the schema:\n{exc}") from exc

    if not plan.stages:
        raise ArtifactContractError(f"phase plan {path} declares no stages")

    ids: list[str] = [stage.id for stage in plan.stages]
    id_set = set(ids)
    if len(id_set) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ArtifactContractError(f"phase plan {path}: duplicate stage ids {dupes}")
    for stage in plan.stages:
        # Ids become branch names and artifact dirs: enforce the safe grammar
        # (no separators, no '..', no leading '-'/'.') as part of "malformed".
        if not _PLAN_ID_RE.fullmatch(stage.id) or ".." in stage.id or stage.id.endswith("."):
            raise ArtifactContractError(f"phase plan {path}: malformed stage id {stage.id!r}")
        if stage.risk_class not in risk_classes:
            raise ArtifactContractError(
                f"phase plan {path}: stage {stage.id!r} has unknown risk_class "
                f"{stage.risk_class!r} (known: {sorted(risk_classes)})"
            )

    seen_edges: set[tuple[str, str]] = set()
    for from_id, to_id in plan.dag_edges:
        if (from_id, to_id) in seen_edges:
            raise ArtifactContractError(
                f"phase plan {path}: duplicate dag edge {[from_id, to_id]}"
            )
        seen_edges.add((from_id, to_id))
        for endpoint in (from_id, to_id):
            if endpoint not in id_set:
                raise ArtifactContractError(
                    f"phase plan {path}: dag edge {[from_id, to_id]} references "
                    f"undeclared stage {endpoint!r}"
                )

    _assert_acyclic(plan, path)
    return plan


# --------------------------------------------------------- macro-plan contract


class MacroPhase(BaseModel):
    """One phase row of macro-plan.json: id, name."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str


class MacroPlan(BaseModel):
    """Schema of macro-plan.json: project, phases[{id, name}],
    dag_edges[[from_id, to_id]]; extra='forbid'."""

    model_config = ConfigDict(extra="forbid")

    project: str
    phases: list[MacroPhase]
    dag_edges: list[tuple[str, str]]


def read_macro_plan(path: Path, *, projects: Collection[str]) -> MacroPlan:
    """Strict-validate the ratified macro plan BEFORE any DB write (phase-seeding
    design §2.2; mirrors the read_phase_plan contract): project ∈ projects; phase
    ids unique, non-empty, and matching the same id grammar as plan-local stage
    ids (_PLAN_ID_RE — ids feed branch names, artifact dirs and stage
    namespacing; a malformed id must die here, not at dispatch); dag edges
    cycle-checked over the subgraph induced by the plan's OWN phases — edge
    endpoints NOT declared in the plan are tolerated here (they may resolve to
    existing DB phases; the CALLER owns that resolution and the combined-graph
    re-check, because this module's file-contract validators stay DB-free even
    though the module may import db — placement rationale, not an import-rule
    claim). Malformed → ArtifactContractError (fail-explicit, Doctrine §7)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ArtifactContractError(f"cannot read macro plan {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ArtifactContractError(f"macro plan {path} is not valid JSON: {exc}") from exc
    try:
        plan = MacroPlan.model_validate(data)
    except ValidationError as exc:
        raise ArtifactContractError(f"macro plan {path} violates the schema:\n{exc}") from exc

    if plan.project not in projects:
        raise ArtifactContractError(
            f"macro plan {path}: unknown project {plan.project!r} "
            f"(configured: {sorted(projects)})"
        )
    if not plan.phases:
        raise ArtifactContractError(f"macro plan {path} declares no phases")

    ids: list[str] = [phase.id for phase in plan.phases]
    id_set = set(ids)
    if len(id_set) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ArtifactContractError(f"macro plan {path}: duplicate phase ids {dupes}")
    for phase in plan.phases:
        # Same safe grammar as plan-local stage ids: no separators, no '..',
        # no leading '-'/'.', no trailing '.' (ids feed branch names + dirs).
        if not _PLAN_ID_RE.fullmatch(phase.id) or ".." in phase.id or phase.id.endswith("."):
            raise ArtifactContractError(f"macro plan {path}: malformed phase id {phase.id!r}")

    seen_edges: set[tuple[str, str]] = set()
    for from_id, to_id in plan.dag_edges:
        if (from_id, to_id) in seen_edges:
            raise ArtifactContractError(
                f"macro plan {path}: duplicate dag edge {[from_id, to_id]}"
            )
        seen_edges.add((from_id, to_id))

    _assert_macro_acyclic(plan, path)
    return plan


def _assert_macro_acyclic(plan: MacroPlan, path: Path) -> None:
    """Kahn toposort over the subgraph induced by the plan's OWN phases; edges
    with a foreign endpoint are excluded here (caller resolves them against the
    DB and re-checks the combined graph). Remainder = cycle -> ArtifactContractError."""
    indegree: dict[str, int] = {phase.id: 0 for phase in plan.phases}
    adjacency: dict[str, list[str]] = {phase.id: [] for phase in plan.phases}
    for from_id, to_id in plan.dag_edges:
        if from_id not in indegree or to_id not in indegree:
            continue  # foreign endpoint: tolerated, resolved by the caller
        adjacency[from_id].append(to_id)
        indegree[to_id] += 1
    ready = sorted(uid for uid, deg in indegree.items() if deg == 0)
    processed = 0
    while ready:
        unit = ready.pop(0)
        processed += 1
        for dependent in adjacency[unit]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort()
    if processed != len(indegree):
        cyclic = sorted(uid for uid, deg in indegree.items() if deg > 0)
        raise ArtifactContractError(
            f"macro plan {path}: phase DAG is cyclic (phases in/behind a cycle: {cyclic})"
        )


def _assert_acyclic(plan: PhasePlan, path: Path) -> None:
    """Kahn toposort; any unprocessed remainder = cycle -> ArtifactContractError."""
    indegree: dict[str, int] = {stage.id: 0 for stage in plan.stages}
    adjacency: dict[str, list[str]] = {stage.id: [] for stage in plan.stages}
    for from_id, to_id in plan.dag_edges:
        adjacency[from_id].append(to_id)
        indegree[to_id] += 1
    ready = sorted(uid for uid, deg in indegree.items() if deg == 0)
    processed = 0
    while ready:
        unit = ready.pop(0)
        processed += 1
        for dependent in adjacency[unit]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort()
    if processed != len(indegree):
        cyclic = sorted(uid for uid, deg in indegree.items() if deg > 0)
        raise ArtifactContractError(
            f"phase plan {path}: stage DAG is cyclic (stages in/behind a cycle: {cyclic})"
        )


# --------------------------------------------------- validation-sidecar contract


def read_validation_sidecar(path: Path) -> ValidationSummary:
    """Parse validator's machine-readable JSON sidecar; raises ArtifactContractError
    (no guessing, Doctrine §7).

    Contract (OPEN-5): exactly the keys {failing, passing, total}, all
    non-negative integers, with failing + passing <= total.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ArtifactContractError(f"cannot read validation sidecar {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ArtifactContractError(
            f"validation sidecar {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ArtifactContractError(
            f"validation sidecar {path} must be a JSON object, got {type(data).__name__}"
        )
    expected = {"failing", "passing", "total"}
    if set(data) != expected:
        raise ArtifactContractError(
            f"validation sidecar {path} must have exactly keys {sorted(expected)}, "
            f"got {sorted(data)}"
        )
    values: dict[str, int] = {}
    for key in ("failing", "passing", "total"):
        value = data[key]
        if type(value) is not int or value < 0:  # bools are ints — rejected on purpose
            raise ArtifactContractError(
                f"validation sidecar {path}: {key} must be a non-negative integer, "
                f"got {value!r}"
            )
        values[key] = value
    if values["failing"] + values["passing"] > values["total"]:
        raise ArtifactContractError(
            f"validation sidecar {path}: failing+passing exceeds total ({values})"
        )
    return ValidationSummary(
        failing=values["failing"], passing=values["passing"], total=values["total"]
    )


# ------------------------------------------------------------ sentinel contract


def detect_sentinels(unit_dir: Path) -> list[str]:
    """Return present sentinel kinds ('declared_failure','contract_change_request') —
    mechanical detection; archived sentinels (`*.resolved-<id>.md`, §5.4) do not match.

    Exact-filename match only (STAGE_ARTIFACTS layout); a missing unit dir
    simply has no sentinels.
    """
    base = Path(unit_dir)
    present: list[str] = []
    for kind in _SENTINEL_KINDS:
        if (base / STAGE_ARTIFACTS[kind]).is_file():
            present.append(kind)
    return present


# ----------------------------------------------------------- integrity (DoD A2)


@dataclass(frozen=True, slots=True)
class IntegrityIssue:
    """One unverifiable artifact ref: identity fields + the concrete problem."""

    unit_level: str
    unit_id: str
    kind: str
    repo: str
    path: str
    problem: str


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """verify_integrity output: failures abort a start (§5.5c); warnings are
    terminal-unit downgrades, logged but non-blocking."""

    checked: int
    failures: tuple[IntegrityIssue, ...]
    warnings: tuple[IntegrityIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


_TERMINAL_STAGE_STATES = frozenset(
    {StageState.DONE, StageState.FAILED, StageState.CANCELLED}
)
_TERMINAL_PHASE_STATES = frozenset(
    {PhaseState.DONE, PhaseState.FAILED, PhaseState.CANCELLED}
)


def verify_integrity(db: Database, repo_roots: Mapping[str, Path]) -> IntegrityReport:
    """DoD §12.A2: every latest artifact ref of a NON-TERMINAL unit resolves and sha256
    matches; recorded git_commit exists. Resolution precedence: stage worktree_path if
    present → `git cat-file <commit>:<path>` → integration branch. Terminal units
    (FAILED/CANCELLED, merged DONE) downgrade mismatches to logged warnings — their
    worktrees/branches are legitimately gone. Returns report; never repairs silently."""
    conn = db.read()
    checked = 0
    failures: list[IntegrityIssue] = []
    warnings: list[IntegrityIssue] = []
    for ref in iter_latest_artifact_refs(conn):
        checked += 1
        terminal, worktree_path, unit_problems = _unit_status(conn, ref)
        problems = list(unit_problems)
        root = repo_roots.get(ref.repo)
        if root is None:
            problems.append(f"no repo root provided for repo {ref.repo!r}")
        else:
            problems.extend(_resolve_ref(ref, Path(root), worktree_path))
        for problem in problems:
            issue = IntegrityIssue(
                unit_level=ref.unit_level,
                unit_id=ref.unit_id,
                kind=ref.kind,
                repo=ref.repo,
                path=ref.path,
                problem=problem,
            )
            if terminal:
                logger.warning(
                    "integrity (terminal unit, downgraded): %s/%s %s %s: %s",
                    ref.unit_level,
                    ref.unit_id,
                    ref.kind,
                    ref.path,
                    problem,
                )
                warnings.append(issue)
            else:
                logger.error(
                    "integrity failure: %s/%s %s %s: %s",
                    ref.unit_level,
                    ref.unit_id,
                    ref.kind,
                    ref.path,
                    problem,
                )
                failures.append(issue)
    return IntegrityReport(
        checked=checked, failures=tuple(failures), warnings=tuple(warnings)
    )


def _unit_status(
    conn: sqlite3.Connection, ref: ArtifactRef
) -> tuple[bool, str | None, list[str]]:
    """(terminal?, stage worktree_path, unit-level problems) for a ref's owner.

    An unknown stage/phase row is itself an integrity problem and is never
    terminal (strict). Levels without unit rows (e.g. 'factory') check strictly.
    """
    if ref.unit_level == Level.STAGE.value:
        stage = get_stage(conn, ref.unit_id)
        if stage is None:
            return False, None, [f"artifact ref points at unknown stage {ref.unit_id!r}"]
        return stage.state in _TERMINAL_STAGE_STATES, stage.worktree_path, []
    if ref.unit_level == Level.PHASE.value:
        phase = get_phase(conn, ref.unit_id)
        if phase is None:
            return False, None, [f"artifact ref points at unknown phase {ref.unit_id!r}"]
        return phase.state in _TERMINAL_PHASE_STATES, None, []
    return False, None, []


def _resolve_ref(ref: ArtifactRef, repo_root: Path, worktree_path: str | None) -> list[str]:
    """Apply the resolution precedence; return [] when the ref verifies.

    Both frozen requirements are checked: (a) content resolves with a matching
    sha256 at some precedence step, and (b) a recorded git_commit exists —
    independently, so a matching worktree file never masks a vanished commit.
    """
    problems: list[str] = []
    tried: list[str] = []
    resolved = False

    if worktree_path:
        candidate = Path(worktree_path) / ref.path
        if candidate.is_file():
            try:
                if sha256_file(candidate) == ref.sha256:
                    resolved = True
                else:
                    tried.append(f"worktree file {candidate} has a different sha256")
            except IntegrityError as exc:
                tried.append(str(exc))
        else:
            tried.append(f"not present in worktree {worktree_path}")

    if ref.git_commit:
        if not _git_commit_exists(repo_root, ref.git_commit):
            problems.append(f"recorded git_commit {ref.git_commit} does not exist")
        elif not resolved:
            blob = _git_blob_bytes(repo_root, f"{ref.git_commit}:{ref.path}")
            if blob is None:
                tried.append(f"path not found at recorded commit {ref.git_commit}")
            elif hashlib.sha256(blob).hexdigest() == ref.sha256:
                resolved = True
            else:
                tried.append(f"blob at {ref.git_commit} has a different sha256")

    if not resolved:
        blob = _git_blob_bytes(repo_root, f"HEAD:{ref.path}")
        if blob is None:
            tried.append("path not found at the integration checkout HEAD")
        elif hashlib.sha256(blob).hexdigest() == ref.sha256:
            resolved = True
        else:
            tried.append("blob at the integration checkout HEAD has a different sha256")

    if not resolved:
        problems.append("unresolved: " + "; ".join(tried))
    return problems


def _run_git_read(repo_root: Path, *args: str) -> tuple[int, bytes]:
    """Read-only git plumbing call (binary-safe stdout); never mutates state."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args], capture_output=True, check=False
        )
    except FileNotFoundError as exc:  # cannot check anything — fail loud, never guess
        raise IntegrityError("git executable not found — cannot verify integrity") from exc
    return proc.returncode, proc.stdout


def _git_commit_exists(repo_root: Path, sha: str) -> bool:
    code, _ = _run_git_read(repo_root, "cat-file", "-e", f"{sha}^{{commit}}")
    return code == 0


def _git_blob_bytes(repo_root: Path, spec: str) -> bytes | None:
    code, out = _run_git_read(repo_root, "cat-file", "blob", spec)
    return out if code == 0 else None
