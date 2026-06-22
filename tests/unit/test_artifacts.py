"""Unit tests for sf_factory.artifacts (design §8): hashing + get-or-create
registration (byte-identical re-registration, crash replay), sidecar contract
rejection, phase-plan schema + cycle rejection, sentinel detection (archived
sentinels excluded), integrity mismatch detection + terminal-unit downgrade.

Extra fixtures live locally (tests/conftest.py is frozen with wave 1).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from sf_factory import artifacts
from sf_factory.db import Database, insert_phase, insert_stage, latest_artifact
from sf_factory.models import (
    ArtifactContractError,
    FactoryError,
    IntegrityError,
    Level,
    Phase,
    PhaseState,
    Stage,
    StageState,
    ValidationSummary,
    utc_now,
)

# ------------------------------------------------------------- local fixtures


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}"
    return proc.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(path)], capture_output=True, check=True
    )
    _git(path, "config", "user.name", "SF-F5 Test")
    _git(path, "config", "user.email", "test@sf-f5.local")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "init")


def _commit_file(repo: Path, rel: str, content: str, message: str = "commit") -> str:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Real temp git repo standing in for the workspace integration checkout."""
    path = tmp_path / "artifacts-repo"
    _init_repo(path)
    return path


def _add_phase(db: Database, phase_id: str = "ph", state: PhaseState = PhaseState.RUNNING) -> None:
    with db.transaction() as conn:
        insert_phase(
            conn,
            Phase(
                id=phase_id,
                project="proj",
                name=phase_id,
                state=state,
                branch=None,
                plan_artifact_id=None,
                created_at=utc_now(),
                updated_at=utc_now(),
            ),
        )


def _add_stage(
    db: Database,
    stage_id: str,
    state: StageState,
    *,
    worktree_path: str | None = None,
    phase_id: str = "ph",
) -> None:
    with db.transaction() as conn:
        insert_stage(
            conn,
            Stage(
                id=stage_id,
                phase_id=phase_id,
                name=stage_id,
                risk_class="routine",
                state=state,
                branch=None,
                worktree_path=worktree_path,
                spec_artifact_id=None,
                created_at=utc_now(),
                updated_at=utc_now(),
            ),
        )


def _register(db: Database, repo: Path, rel: str, **overrides) -> artifacts.ArtifactRef:
    kwargs = dict(
        unit_level="stage",
        unit_id="s1",
        kind="spec",
        repo="workspace",
        repo_root=repo,
        path=repo / rel,
        git_commit=None,
    )
    kwargs.update(overrides)
    with db.transaction() as conn:
        return artifacts.register_artifact(conn, **kwargs)


# ------------------------------------------------------------ path conventions


def test_stage_artifacts_layout_is_the_frozen_contract():
    assert dict(artifacts.STAGE_ARTIFACTS) == {
        "spec": "spec.md",
        "build_notes": "build-notes.md",
        "validation_report": "validation-report.md",
        "validation_sidecar": "validation-report.json",
        "audit_report": "audit-<role>.md",
        "declared_failure": "_DECLARED_FAILURE.md",
        "contract_change_request": "_CONTRACT_CHANGE_REQUEST.md",
    }


def test_phase_artifacts_layout_is_the_frozen_contract():
    assert dict(artifacts.PHASE_ARTIFACTS) == {
        "phase_plan": "phase-plan.md",
        "phase_plan_sidecar": "phase-plan.json",
    }


def test_artifact_maps_are_read_only():
    with pytest.raises(TypeError):
        artifacts.STAGE_ARTIFACTS["spec"] = "other.md"  # type: ignore[index]


def test_unit_artifact_dir_per_level(tmp_path: Path):
    root = tmp_path
    assert artifacts.unit_artifact_dir(root, Level.STAGE, "s1") == (
        root / "_factory" / "stages" / "s1"
    )
    assert artifacts.unit_artifact_dir(root, Level.PHASE, "found") == (
        root / "_factory" / "phases" / "found"
    )
    # str level coerces like the rest of the codebase
    assert artifacts.unit_artifact_dir(root, "stage", "s1").name == "s1"  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_id", ["", "a/b", "a\\b", "..", "a..b", "."])
def test_unit_artifact_dir_refuses_unsafe_ids(tmp_path: Path, bad_id: str):
    with pytest.raises(FactoryError):
        artifacts.unit_artifact_dir(tmp_path, Level.STAGE, bad_id)


# ------------------------------------------------------------------- sha256


def test_sha256_file_streams_expected_digest(tmp_path: Path):
    payload = b"factory artifact content\n" * 1000
    target = tmp_path / "spec.md"
    target.write_bytes(payload)
    assert artifacts.sha256_file(target) == hashlib.sha256(payload).hexdigest()


def test_sha256_file_unreadable_raises_integrity_error(tmp_path: Path):
    with pytest.raises(IntegrityError):
        artifacts.sha256_file(tmp_path / "missing.md")
    with pytest.raises(IntegrityError):
        artifacts.sha256_file(tmp_path)  # a directory is unreadable as a file


# -------------------------------------------------------- register_artifact


def test_register_artifact_inserts_and_stores_relative_posix_path(db, repo: Path):
    sha = _commit_file(repo, "_factory/stages/s1/spec.md", "the spec\n")
    ref = _register(db, repo, "_factory/stages/s1/spec.md", git_commit=sha)
    assert ref.id is not None
    assert ref.path == "_factory/stages/s1/spec.md"
    assert ref.sha256 == hashlib.sha256(b"the spec\n").hexdigest()
    assert ref.git_commit == sha
    found = latest_artifact(db.read(), "stage", "s1", "spec")
    assert found is not None and found.id == ref.id


def test_register_artifact_accepts_repo_relative_path(db, repo: Path):
    (repo / "notes.md").write_text("notes\n", encoding="utf-8")
    ref = _register(db, repo, "notes.md", path=Path("notes.md"), kind="build_notes")
    assert ref.path == "notes.md"


def test_register_artifact_byte_identical_returns_existing_ref(db, repo: Path):
    (repo / "spec.md").write_text("same bytes\n", encoding="utf-8")
    first = _register(db, repo, "spec.md")
    second = _register(db, repo, "spec.md")
    assert second.id == first.id
    assert second == first


def test_register_artifact_twice_inside_one_transaction(db, repo: Path):
    """Re-registration must never abort the enclosing transition (UNIQUE conflict)."""
    (repo / "spec.md").write_text("same bytes\n", encoding="utf-8")
    with db.transaction() as conn:
        first = artifacts.register_artifact(
            conn,
            unit_level="stage",
            unit_id="s1",
            kind="spec",
            repo="workspace",
            repo_root=repo,
            path=repo / "spec.md",
            git_commit=None,
        )
        second = artifacts.register_artifact(
            conn,
            unit_level="stage",
            unit_id="s1",
            kind="spec",
            repo="workspace",
            repo_root=repo,
            path=repo / "spec.md",
            git_commit=None,
        )
    assert first.id == second.id


def test_register_artifact_crash_replay_returns_existing_ref(db, repo: Path):
    """A crash-replayed step re-registers in a fresh tx and gets the same row."""
    (repo / "report.json").write_text('{"failing": 0}\n', encoding="utf-8")
    first = _register(db, repo, "report.json", kind="validation_sidecar")
    replay = _register(db, repo, "report.json", kind="validation_sidecar")
    assert replay.id == first.id


def test_register_artifact_changed_content_creates_new_row(db, repo: Path):
    (repo / "spec.md").write_text("v1\n", encoding="utf-8")
    first = _register(db, repo, "spec.md")
    (repo / "spec.md").write_text("v2\n", encoding="utf-8")
    second = _register(db, repo, "spec.md")
    assert second.id != first.id
    assert second.sha256 != first.sha256


def test_register_artifact_outside_repo_root_is_a_caller_bug(db, repo: Path, tmp_path: Path):
    stray = tmp_path / "outside.md"
    stray.write_text("outside\n", encoding="utf-8")
    with db.transaction() as conn:
        with pytest.raises(FactoryError, match="not under repo root"):
            artifacts.register_artifact(
                conn,
                unit_level="stage",
                unit_id="s1",
                kind="spec",
                repo="workspace",
                repo_root=repo,
                path=stray,
                git_commit=None,
            )


def test_register_artifact_missing_file_raises_integrity_error(db, repo: Path):
    with db.transaction() as conn:
        with pytest.raises(IntegrityError):
            artifacts.register_artifact(
                conn,
                unit_level="stage",
                unit_id="s1",
                kind="spec",
                repo="workspace",
                repo_root=repo,
                path=repo / "never-written.md",
                git_commit=None,
            )


# ------------------------------------------------------- validation sidecar


def _write_sidecar(tmp_path: Path, payload) -> Path:
    path = tmp_path / "validation-report.json"
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )
    return path


def test_read_validation_sidecar_happy_path(tmp_path: Path):
    path = _write_sidecar(tmp_path, {"failing": 2, "passing": 7, "total": 10})
    assert artifacts.read_validation_sidecar(path) == ValidationSummary(
        failing=2, passing=7, total=10
    )


def test_read_validation_sidecar_missing_file(tmp_path: Path):
    with pytest.raises(ArtifactContractError):
        artifacts.read_validation_sidecar(tmp_path / "absent.json")


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all {",
        '["failing", 1]',
        {"failing": 1, "passing": 1},  # missing key
        {"failing": 1, "passing": 1, "total": 2, "skipped": 0},  # extra key
        {"failing": -1, "passing": 1, "total": 2},  # negative
        {"failing": True, "passing": 1, "total": 2},  # bool is not an int here
        {"failing": 1.0, "passing": 1, "total": 2},  # float
        {"failing": "1", "passing": 1, "total": 2},  # string
        {"failing": 5, "passing": 6, "total": 10},  # failing+passing > total
    ],
)
def test_read_validation_sidecar_rejects_contract_breaches(tmp_path: Path, payload):
    path = _write_sidecar(tmp_path, payload)
    with pytest.raises(ArtifactContractError):
        artifacts.read_validation_sidecar(path)


# --------------------------------------------------------------- phase plan


def _plan(stages, edges) -> dict:
    return {
        "stages": [
            {"id": s, "name": f"Stage {s}", "risk_class": "routine", "acceptance": "works"}
            for s in stages
        ],
        "dag_edges": edges,
    }


def _write_plan(tmp_path: Path, payload) -> Path:
    path = tmp_path / "phase-plan.json"
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )
    return path


RISK_CLASSES = ("routine", "structural", "critical")


def test_read_phase_plan_happy_diamond(tmp_path: Path):
    path = _write_plan(
        tmp_path, _plan(["a", "b", "c", "d"], [["a", "b"], ["a", "c"], ["b", "d"], ["c", "d"]])
    )
    plan = artifacts.read_phase_plan(path, RISK_CLASSES)
    assert [s.id for s in plan.stages] == ["a", "b", "c", "d"]
    assert plan.dag_edges == [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]
    assert plan.stages[0].acceptance == "works"


def test_read_phase_plan_single_stage_no_edges(tmp_path: Path):
    path = _write_plan(tmp_path, _plan(["solo"], []))
    plan = artifacts.read_phase_plan(path, RISK_CLASSES)
    assert plan.dag_edges == []


@pytest.mark.parametrize(
    "payload",
    [
        "{ truncated",  # not JSON
        {"stages": []},  # missing dag_edges key
        _plan([], []),  # plans nothing
        {**_plan(["a"], []), "surprise": 1},  # extra top-level key
        {
            "stages": [{"id": "a", "name": "A", "risk_class": "routine"}],
            "dag_edges": [],
        },  # missing acceptance
        _plan(["a", "a"], []),  # duplicate stage id
        _plan(["a", "b"], [["a", "zz"]]),  # undeclared endpoint
        _plan(["a", "b"], [["a", "b"], ["a", "b"]]),  # duplicate edge
        _plan(["a"], [["a", "a"]]),  # self-loop
        _plan(["a", "b"], [["a", "b"], ["b", "a"]]),  # 2-cycle
        _plan(["a", "b", "c"], [["a", "b"], ["b", "c"], ["c", "b"]]),  # cycle behind a chain
        _plan(["a", "b"], [["a", "b", "c"]]),  # 3-element edge
    ],
)
def test_read_phase_plan_rejects_malformed_or_cyclic(tmp_path: Path, payload):
    path = _write_plan(tmp_path, payload)
    with pytest.raises(ArtifactContractError):
        artifacts.read_phase_plan(path, RISK_CLASSES)


def test_read_phase_plan_missing_file(tmp_path: Path):
    with pytest.raises(ArtifactContractError):
        artifacts.read_phase_plan(tmp_path / "absent.json", RISK_CLASSES)


def test_read_phase_plan_rejects_unknown_risk_class(tmp_path: Path):
    payload = _plan(["a"], [])
    payload["stages"][0]["risk_class"] = "experimental"
    path = _write_plan(tmp_path, payload)
    with pytest.raises(ArtifactContractError, match="risk_class"):
        artifacts.read_phase_plan(path, RISK_CLASSES)


def test_read_phase_plan_kind_omitted_defaults_none(tmp_path: Path):
    # Backward compat: a plan stage WITHOUT `kind` (every plan that predates the
    # dimension) parses fine and yields kind=None.
    path = _write_plan(tmp_path, _plan(["a"], []))
    plan = artifacts.read_phase_plan(path, RISK_CLASSES)
    assert plan.stages[0].kind is None


def test_read_phase_plan_accepts_explicit_kind(tmp_path: Path):
    payload = _plan(["fe", "be"], [["be", "fe"]])
    payload["stages"][0]["kind"] = "frontend"
    payload["stages"][1]["kind"] = "backend"
    path = _write_plan(tmp_path, payload)
    plan = artifacts.read_phase_plan(path, RISK_CLASSES)
    assert [s.kind for s in plan.stages] == ["frontend", "backend"]


def test_read_phase_plan_rejects_unknown_kind(tmp_path: Path):
    # The Literal['backend','frontend'] guard at the plan-contract layer: a bogus
    # kind is a malformed plan, not silently coerced.
    payload = _plan(["a"], [])
    payload["stages"][0]["kind"] = "fullstack"
    path = _write_plan(tmp_path, payload)
    with pytest.raises(ArtifactContractError):
        artifacts.read_phase_plan(path, RISK_CLASSES)


@pytest.mark.parametrize("bad_id", ["has space", "../escape", "a/b", "-flag", "a..b", "end."])
def test_read_phase_plan_rejects_unsafe_stage_ids(tmp_path: Path, bad_id: str):
    """Plan ids feed branch names and artifact dirs — unsafe ids are malformed."""
    path = _write_plan(tmp_path, _plan([bad_id], []))
    with pytest.raises(ArtifactContractError):
        artifacts.read_phase_plan(path, RISK_CLASSES)


# ---------------------------------------------------------------- sentinels


def test_detect_sentinels_empty_and_missing_dir(tmp_path: Path):
    assert artifacts.detect_sentinels(tmp_path) == []
    assert artifacts.detect_sentinels(tmp_path / "never-created") == []


def test_detect_sentinels_reports_kinds_in_fixed_order(tmp_path: Path):
    (tmp_path / "_CONTRACT_CHANGE_REQUEST.md").write_text("stop\n", encoding="utf-8")
    assert artifacts.detect_sentinels(tmp_path) == ["contract_change_request"]
    (tmp_path / "_DECLARED_FAILURE.md").write_text("cannot proceed\n", encoding="utf-8")
    assert artifacts.detect_sentinels(tmp_path) == [
        "declared_failure",
        "contract_change_request",
    ]


def test_detect_sentinels_excludes_archived_resolved_files(tmp_path: Path):
    (tmp_path / "_DECLARED_FAILURE.resolved-12.md").write_text("old\n", encoding="utf-8")
    (tmp_path / "_CONTRACT_CHANGE_REQUEST.resolved-3.md").write_text("old\n", encoding="utf-8")
    assert artifacts.detect_sentinels(tmp_path) == []


def test_detect_sentinels_ignores_directories_with_sentinel_names(tmp_path: Path):
    (tmp_path / "_DECLARED_FAILURE.md").mkdir()
    assert artifacts.detect_sentinels(tmp_path) == []


# ----------------------------------------------------------- verify_integrity


def test_verify_integrity_all_green(db, repo: Path):
    _add_phase(db)
    _add_stage(db, "s1", StageState.BUILD, worktree_path=str(repo))
    sha = _commit_file(repo, "_factory/stages/s1/spec.md", "spec body\n")
    _register(db, repo, "_factory/stages/s1/spec.md", git_commit=sha)
    report = artifacts.verify_integrity(db, {"workspace": repo})
    assert report.ok
    assert report.checked == 1
    assert report.failures == () and report.warnings == ()


def test_verify_integrity_detects_mismatch_on_non_terminal_unit(db, repo: Path):
    _add_phase(db)
    _add_stage(db, "s1", StageState.BUILD, worktree_path=str(repo))
    target = repo / "_factory/stages/s1/spec.md"
    target.parent.mkdir(parents=True)
    target.write_text("original\n", encoding="utf-8")
    _register(db, repo, "_factory/stages/s1/spec.md")  # never committed
    target.write_text("tampered\n", encoding="utf-8")
    report = artifacts.verify_integrity(db, {"workspace": repo})
    assert not report.ok
    assert len(report.failures) == 1
    assert "unresolved" in report.failures[0].problem


def test_verify_integrity_downgrades_terminal_unit_to_warning(db, repo: Path):
    _add_phase(db)
    _add_stage(db, "s1", StageState.FAILED, worktree_path=str(repo))
    target = repo / "_factory/stages/s1/spec.md"
    target.parent.mkdir(parents=True)
    target.write_text("original\n", encoding="utf-8")
    _register(db, repo, "_factory/stages/s1/spec.md")
    target.unlink()  # worktree artifact legitimately gone
    report = artifacts.verify_integrity(db, {"workspace": repo})
    assert report.ok  # downgraded: no failures
    assert len(report.warnings) == 1
    assert report.warnings[0].unit_id == "s1"


@pytest.mark.parametrize(
    ("state", "expect_failure"),
    [
        (StageState.BUILD, True),
        (StageState.DONE, False),
        (StageState.CANCELLED, False),
    ],
)
def test_verify_integrity_missing_git_commit_by_terminality(
    db, repo: Path, state: StageState, expect_failure: bool
):
    """A recorded-but-nonexistent commit fails non-terminal units even when the
    worktree file still matches (both frozen conditions are independent)."""
    _add_phase(db)
    _add_stage(db, "s1", state, worktree_path=str(repo))
    target = repo / "spec.md"
    target.write_text("fine\n", encoding="utf-8")
    _register(db, repo, "spec.md", git_commit="0" * 40)
    report = artifacts.verify_integrity(db, {"workspace": repo})
    issues = report.failures if expect_failure else report.warnings
    assert report.ok is not expect_failure
    assert any("git_commit" in issue.problem for issue in issues)


def test_verify_integrity_resolves_via_recorded_commit_when_worktree_gone(db, repo: Path):
    _add_phase(db)
    sha = _commit_file(repo, "spec.md", "committed content\n")
    _add_stage(db, "s1", StageState.MERGE_GATE, worktree_path=None)
    _register(db, repo, "spec.md", git_commit=sha)
    # File later changed on HEAD; the recorded commit still resolves it.
    _commit_file(repo, "spec.md", "newer content\n", "rewrite")
    report = artifacts.verify_integrity(db, {"workspace": repo})
    assert report.ok and not report.warnings


def test_verify_integrity_falls_back_to_worktree_then_commit(db, repo: Path):
    """Precedence: a tampered worktree copy is fine when the recorded commit matches."""
    _add_phase(db)
    sha = _commit_file(repo, "spec.md", "v1\n")
    _add_stage(db, "s1", StageState.BUILD, worktree_path=str(repo))
    _register(db, repo, "spec.md", git_commit=sha)
    (repo / "spec.md").write_text("locally tampered\n", encoding="utf-8")
    report = artifacts.verify_integrity(db, {"workspace": repo})
    assert report.ok


def test_verify_integrity_resolves_phase_ref_via_head(db, repo: Path):
    _add_phase(db, "ph", PhaseState.RUNNING)
    _commit_file(repo, "_factory/phases/ph/phase-plan.json", '{"stages": []}\n')
    _register(
        db,
        repo,
        "_factory/phases/ph/phase-plan.json",
        unit_level="phase",
        unit_id="ph",
        kind="phase_plan_sidecar",
    )
    report = artifacts.verify_integrity(db, {"workspace": repo})
    assert report.ok and not report.warnings


def test_verify_integrity_unknown_unit_is_a_failure(db, repo: Path):
    (repo / "spec.md").write_text("x\n", encoding="utf-8")
    _register(db, repo, "spec.md", unit_id="ghost")  # no stage row inserted
    report = artifacts.verify_integrity(db, {"workspace": repo})
    assert not report.ok
    assert any("unknown stage" in issue.problem for issue in report.failures)


def test_verify_integrity_missing_repo_root_is_a_failure(db, repo: Path):
    _add_phase(db)
    _add_stage(db, "s1", StageState.BUILD, worktree_path=str(repo))
    (repo / "spec.md").write_text("x\n", encoding="utf-8")
    _register(db, repo, "spec.md")
    report = artifacts.verify_integrity(db, {"factory": repo})  # no 'workspace' key
    assert not report.ok
    assert any("no repo root" in issue.problem for issue in report.failures)


def test_verify_integrity_checks_only_latest_ref_per_kind(db, repo: Path):
    """Superseded versions of the same kind are not re-verified (DoD A2 scope)."""
    _add_phase(db)
    _add_stage(db, "s1", StageState.BUILD, worktree_path=str(repo))
    (repo / "spec.md").write_text("v1\n", encoding="utf-8")
    _register(db, repo, "spec.md")  # v1 ref — will become unresolvable
    sha = _commit_file(repo, "spec.md", "v2\n", "v2")
    _register(db, repo, "spec.md", git_commit=sha)  # latest ref resolves
    report = artifacts.verify_integrity(db, {"workspace": repo})
    assert report.checked == 1
    assert report.ok


# ----------------------------- macro-plan contract (phase-seeding design §2.2/§8)


def _macro(
    project: str = "proj",
    phase_ids: list[str] | None = None,
    edges: list[list[str]] | None = None,
) -> dict:
    ids = phase_ids if phase_ids is not None else ["a", "b"]
    return {
        "project": project,
        "phases": [{"id": pid, "name": f"Phase {pid}"} for pid in ids],
        "dag_edges": edges if edges is not None else [],
    }


def _write_macro(tmp_path: Path, payload) -> Path:
    path = tmp_path / "macro-plan.json"
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )
    return path


PROJECTS = ("proj", "erp")


def test_read_macro_plan_happy(tmp_path: Path):
    path = _write_macro(tmp_path, _macro(phase_ids=["found", "inv"], edges=[["found", "inv"]]))
    plan = artifacts.read_macro_plan(path, projects=PROJECTS)
    assert plan.project == "proj"
    assert [(p.id, p.name) for p in plan.phases] == [
        ("found", "Phase found"),
        ("inv", "Phase inv"),
    ]
    assert plan.dag_edges == [("found", "inv")]


def test_read_macro_plan_rejects_unknown_project(tmp_path: Path):
    path = _write_macro(tmp_path, _macro(project="ghost"))
    with pytest.raises(ArtifactContractError, match="unknown project"):
        artifacts.read_macro_plan(path, projects=PROJECTS)


def test_read_macro_plan_tolerates_foreign_edge_endpoints(tmp_path: Path):
    """Edge endpoints NOT declared in the plan may resolve to existing DB phases —
    tolerated HERE; the caller (seed-phases) owns resolution + the combined-graph
    re-check (design §2.2)."""
    path = _write_macro(
        tmp_path,
        _macro(phase_ids=["new1"], edges=[["already-in-db", "new1"], ["x", "y"]]),
    )
    plan = artifacts.read_macro_plan(path, projects=PROJECTS)
    assert plan.dag_edges == [("already-in-db", "new1"), ("x", "y")]


def test_read_macro_plan_plan_local_cycle_uses_only_declared_phases(tmp_path: Path):
    """The cycle check runs over the subgraph induced by the plan's OWN phases:
    a cycle through declared ids is rejected even when other edges are foreign."""
    payload = _macro(phase_ids=["a", "b"], edges=[["a", "b"], ["b", "a"], ["db-ph", "a"]])
    with pytest.raises(ArtifactContractError, match="cyclic"):
        artifacts.read_macro_plan(_write_macro(tmp_path, payload), projects=PROJECTS)


@pytest.mark.parametrize(
    "payload",
    [
        "{ truncated",  # not JSON
        {"project": "proj", "phases": []},  # missing dag_edges key
        _macro(phase_ids=[]),  # plans nothing
        {**_macro(), "surprise": 1},  # extra top-level key
        {
            "project": "proj",
            "phases": [{"id": "a", "name": "A", "owner": "x"}],
            "dag_edges": [],
        },  # extra phase key
        {"project": "proj", "phases": [{"id": "a"}], "dag_edges": []},  # missing name
        _macro(phase_ids=["a", "a"]),  # duplicate phase id
        _macro(phase_ids=["a", "b"], edges=[["a", "b"], ["a", "b"]]),  # duplicate edge
        _macro(phase_ids=["a"], edges=[["a", "a"]]),  # self-loop on a declared id
        _macro(phase_ids=["a", "b"], edges=[["a", "b", "c"]]),  # 3-element edge
    ],
)
def test_read_macro_plan_rejects_malformed(tmp_path: Path, payload):
    with pytest.raises(ArtifactContractError):
        artifacts.read_macro_plan(_write_macro(tmp_path, payload), projects=PROJECTS)


@pytest.mark.parametrize("bad_id", ["", "has space", "../escape", "a/b", "-flag", "a..b", "end."])
def test_read_macro_plan_rejects_unsafe_phase_ids(tmp_path: Path, bad_id: str):
    """Phase ids feed branch names ('phase/<id>'), artifact dirs and stage
    namespacing ('<phase>.<stage>') — same _PLAN_ID_RE grammar as stage ids; a
    malformed id must die here, not at dispatch (design §2.2)."""
    path = _write_macro(tmp_path, _macro(phase_ids=[bad_id]))
    with pytest.raises(ArtifactContractError):
        artifacts.read_macro_plan(path, projects=PROJECTS)


def test_read_macro_plan_missing_file(tmp_path: Path):
    with pytest.raises(ArtifactContractError):
        artifacts.read_macro_plan(tmp_path / "absent.json", projects=PROJECTS)
