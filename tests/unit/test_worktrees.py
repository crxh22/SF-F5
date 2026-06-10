"""Unit tests for sf_factory.worktrees against REAL temp git repos (design §8):
idempotent create (+ prune of half-registered leftovers), wrong-branch refusal,
rebase-conflict payload, failing/timed-out test suite, heal_git_state on wedged
rebase/merge/cherry-pick, gate-lock serialization of two concurrent gates,
integrate with Stage-Id trailer, merged_unit_diffs, full_diff vs diff_digest.

Extra fixtures live locally (tests/conftest.py is frozen with wave 1).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sf_factory.config import FactoryConfig
from sf_factory.models import GitError
from sf_factory.worktrees import (
    StaleGateError,
    Tier1Result,
    WorktreeManager,
    commit_paths,
    run_git,
)

PASS_CMD = [sys.executable, "-c", "raise SystemExit(0)"]
FAIL_CMD = [sys.executable, "-c", "print('BOOM-FAILING-TEST'); raise SystemExit(1)"]
SLEEP_CMD = [sys.executable, "-c", "import time; time.sleep(60)"]

# ------------------------------------------------------------- local fixtures


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}"
    return proc.stdout


def _git_fails(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode != 0, f"expected git {' '.join(args)} to fail"
    return proc.stdout + proc.stderr


def _init_repo(path: Path) -> None:
    """Real repo with local identity — commits fail without user.name/email."""
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


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _branch_of(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()


@pytest.fixture()
def workspace(config_dict: dict[str, Any]) -> Path:
    """Git repo at exactly the configured project workspace path."""
    path = Path(config_dict["projects"]["proj"]["workspace"])
    _init_repo(path)
    return path


@pytest.fixture()
def manager(config_dict: dict[str, Any], workspace: Path) -> WorktreeManager:
    return WorktreeManager(FactoryConfig.model_validate(config_dict))


# ------------------------------------------------------------------- run_git


async def test_run_git_success(workspace: Path):
    code, out, err = await run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=workspace)
    assert code == 0
    assert out.strip() == "main"


async def test_run_git_nonzero_exit_does_not_raise(workspace: Path):
    code, out, err = await run_git("rev-parse", "--verify", "no-such-ref", cwd=workspace)
    assert code != 0
    assert err  # stderr captured, not raised


# -------------------------------------------------------------- commit_paths


async def test_commit_paths_commits_with_trailer_block(workspace: Path):
    (workspace / "spec.md").write_text("spec\n", encoding="utf-8")
    sha = await commit_paths(
        workspace,
        [Path("spec.md")],
        "stage s1: register spec",
        trailers={"Stage-Id": "s1", "Factory-Step": "spec"},
    )
    assert sha == _head(workspace)
    body = _git(workspace, "log", "-1", "--format=%B")
    assert "stage s1: register spec" in body
    assert "Stage-Id: s1" in body
    assert "Factory-Step: spec" in body
    # exactly one blank line separates body from the trailer block
    assert body.rstrip().endswith("register spec\n\nStage-Id: s1\nFactory-Step: spec")


async def test_commit_paths_returns_none_when_nothing_to_commit(workspace: Path):
    (workspace / "a.md").write_text("a\n", encoding="utf-8")
    first = await commit_paths(workspace, [Path("a.md")], "add a", trailers={})
    assert first is not None
    again = await commit_paths(workspace, [Path("a.md")], "add a again", trailers={})
    assert again is None


async def test_commit_paths_scopes_to_named_paths_only(workspace: Path):
    (workspace / "wanted.md").write_text("w\n", encoding="utf-8")
    (workspace / "unrelated.md").write_text("u\n", encoding="utf-8")
    _git(workspace, "add", "unrelated.md")  # pre-staged by someone else
    await commit_paths(workspace, [Path("wanted.md")], "scoped", trailers={})
    shown = _git(workspace, "show", "--name-only", "--format=", "HEAD")
    assert "wanted.md" in shown
    assert "unrelated.md" not in shown


async def test_commit_paths_refuses_non_worktree_root(workspace: Path):
    sub = workspace / "sub"
    sub.mkdir()
    (sub / "f.md").write_text("f\n", encoding="utf-8")
    with pytest.raises(GitError, match="work tree root"):
        await commit_paths(sub, [Path("f.md")], "msg", trailers={})


async def test_commit_paths_refuses_detached_head(workspace: Path):
    _git(workspace, "checkout", "--detach", "main")
    (workspace / "f.md").write_text("f\n", encoding="utf-8")
    with pytest.raises(GitError, match="detached"):
        await commit_paths(workspace, [Path("f.md")], "msg", trailers={})


async def test_commit_paths_refuses_empty_paths(workspace: Path):
    with pytest.raises(GitError, match="no paths"):
        await commit_paths(workspace, [], "msg", trailers={})


async def test_commit_paths_rejects_bad_trailers(workspace: Path):
    (workspace / "f.md").write_text("f\n", encoding="utf-8")
    with pytest.raises(GitError, match="trailer key"):
        await commit_paths(workspace, [Path("f.md")], "msg", trailers={"Bad Key": "v"})
    with pytest.raises(GitError, match="single-line"):
        await commit_paths(workspace, [Path("f.md")], "msg", trailers={"Key": "a\nb"})


async def test_commit_paths_missing_path_is_explicit_failure(workspace: Path):
    with pytest.raises(GitError, match="git add failed"):
        await commit_paths(workspace, [Path("ghost.md")], "msg", trailers={})


async def test_commit_paths_works_in_linked_worktree(workspace: Path, manager: WorktreeManager):
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    (wt / "built.py").write_text("x = 1\n", encoding="utf-8")
    sha = await commit_paths(wt, [Path("built.py")], "build", trailers={"Stage-Id": "u1"})
    assert sha == _head(wt)
    assert _branch_of(wt) == "stage/u1"
    assert _head(workspace) != sha  # main untouched


# -------------------------------------------------------- WorktreeManager.create


async def test_create_makes_worktree_on_new_branch(workspace: Path, manager: WorktreeManager):
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    assert wt == (workspace / ".worktrees" / "u1").resolve()
    assert (wt / "README.md").is_file()
    assert _branch_of(wt) == "stage/u1"
    assert _head(wt) == _head(workspace)  # branched from base tip


async def test_create_is_idempotent(workspace: Path, manager: WorktreeManager):
    first = await manager.create(workspace, "u1", "stage/u1", "main")
    second = await manager.create(workspace, "u1", "stage/u1", "main")
    assert first == second
    assert _branch_of(second) == "stage/u1"


async def test_create_refuses_wrong_branch(workspace: Path, manager: WorktreeManager):
    await manager.create(workspace, "u1", "stage/u1", "main")
    with pytest.raises(GitError, match="wrong branch"):
        await manager.create(workspace, "u1", "stage/other", "main")


async def test_create_prunes_half_registered_leftovers(
    workspace: Path, manager: WorktreeManager
):
    """A crash-orphaned registration (dir gone, admin entry left) is cleaned, not
    escalated; the surviving branch is re-attached, not re-created."""
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    marker = _commit_file(wt, "work.py", "w = 1\n")
    subprocess.run(["rm", "-rf", str(wt)], check=True)  # simulate crash-lost dir
    recreated = await manager.create(workspace, "u1", "stage/u1", "main")
    assert recreated == wt
    assert _branch_of(recreated) == "stage/u1"
    assert _head(recreated) == marker  # attached to the surviving branch state


async def test_create_attaches_when_branch_already_exists(
    workspace: Path, manager: WorktreeManager
):
    _git(workspace, "branch", "stage/u9", "main")
    wt = await manager.create(workspace, "u9", "stage/u9", "main", new_branch=True)
    assert _branch_of(wt) == "stage/u9"


async def test_create_refuses_unregistered_nonempty_path(
    workspace: Path, manager: WorktreeManager
):
    squatter = workspace / ".worktrees" / "u1"
    squatter.mkdir(parents=True)
    (squatter / "junk.txt").write_text("junk\n", encoding="utf-8")
    with pytest.raises(GitError, match="not a registered worktree"):
        await manager.create(workspace, "u1", "stage/u1", "main")


async def test_create_scratch_worktree_is_detached_at_branch_tip(
    workspace: Path, manager: WorktreeManager
):
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    tip = _commit_file(wt, "feature.py", "f = 1\n")
    scratch = await manager.create(
        workspace, "u1-validate", "stage/u1", "main", new_branch=False
    )
    assert scratch == (workspace / ".worktrees" / "u1-validate").resolve()
    assert _branch_of(scratch) == "HEAD"  # detached: the branch stays checked out in wt
    assert _head(scratch) == tip
    assert (scratch / "feature.py").is_file()


async def test_create_scratch_worktree_resyncs_to_advanced_branch(
    workspace: Path, manager: WorktreeManager
):
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    _commit_file(wt, "v1.py", "v = 1\n")
    scratch = await manager.create(workspace, "u1-validate", "stage/u1", "main", new_branch=False)
    new_tip = _commit_file(wt, "v2.py", "v = 2\n")
    resynced = await manager.create(
        workspace, "u1-validate", "stage/u1", "main", new_branch=False
    )
    assert resynced == scratch
    assert _head(scratch) == new_tip
    assert (scratch / "v2.py").is_file()


async def test_create_scratch_requires_existing_branch(
    workspace: Path, manager: WorktreeManager
):
    with pytest.raises(GitError, match="does not exist"):
        await manager.create(workspace, "x-validate", "stage/ghost", "main", new_branch=False)


async def test_create_refuses_unknown_workspace(manager: WorktreeManager, tmp_path: Path):
    stranger = tmp_path / "not-a-project"
    _init_repo(stranger)
    with pytest.raises(GitError, match="no configured project workspace"):
        await manager.create(stranger, "u1", "stage/u1", "main")


async def test_create_refuses_unsafe_unit_id(workspace: Path, manager: WorktreeManager):
    with pytest.raises(GitError, match="unsafe unit id"):
        await manager.create(workspace, "../escape", "stage/x", "main")


# -------------------------------------------------------- WorktreeManager.remove


async def test_remove_unregisters_and_deletes(workspace: Path, manager: WorktreeManager):
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    (wt / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")  # --force covers it
    await manager.remove(workspace, wt)
    assert not wt.exists()
    assert str(wt) not in _git(workspace, "worktree", "list", "--porcelain")


async def test_remove_unknown_worktree_raises(workspace: Path, manager: WorktreeManager):
    with pytest.raises(GitError, match="worktree remove failed"):
        await manager.remove(workspace, workspace / ".worktrees" / "ghost")


# -------------------------------------------------------------- heal_git_state


async def test_heal_clean_worktree_takes_no_action(workspace: Path, manager: WorktreeManager):
    assert await manager.heal_git_state(workspace) == []


async def test_heal_aborts_wedged_rebase(workspace: Path, manager: WorktreeManager):
    _commit_file(workspace, "conflict.txt", "base\n", "base")
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    before = _commit_file(wt, "conflict.txt", "mine\n", "mine")
    _commit_file(workspace, "conflict.txt", "theirs\n", "theirs")
    _git_fails(wt, "rebase", "main")  # wedge it
    git_dir = Path(_git(wt, "rev-parse", "--path-format=absolute", "--git-dir").strip())
    assert (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()

    actions = await manager.heal_git_state(wt)

    assert actions == ["rebase --abort"]
    assert not (git_dir / "rebase-merge").exists()
    assert not (git_dir / "rebase-apply").exists()
    assert _branch_of(wt) == "stage/u1"
    assert _head(wt) == before  # mechanically restored
    assert _git(wt, "status", "--porcelain").strip() == ""


async def test_heal_aborts_wedged_merge(workspace: Path, manager: WorktreeManager):
    _commit_file(workspace, "m.txt", "base\n", "base")
    _git(workspace, "branch", "feature", "main")
    before = _commit_file(workspace, "m.txt", "main side\n", "main side")
    wt = await manager.create(workspace, "feat", "feature", "main")
    _commit_file(wt, "m.txt", "feature side\n", "feature side")
    _git_fails(workspace, "merge", "feature")
    git_dir = Path(_git(workspace, "rev-parse", "--path-format=absolute", "--git-dir").strip())
    assert (git_dir / "MERGE_HEAD").exists()

    actions = await manager.heal_git_state(workspace)

    assert actions == ["merge --abort"]
    assert not (git_dir / "MERGE_HEAD").exists()
    assert _head(workspace) == before


async def test_heal_aborts_wedged_cherry_pick(workspace: Path, manager: WorktreeManager):
    _commit_file(workspace, "c.txt", "base\n", "base")
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    pick = _commit_file(wt, "c.txt", "picked\n", "picked")
    before = _commit_file(workspace, "c.txt", "moved on\n", "moved on")
    _git_fails(workspace, "cherry-pick", pick)
    git_dir = Path(_git(workspace, "rev-parse", "--path-format=absolute", "--git-dir").strip())
    assert (git_dir / "CHERRY_PICK_HEAD").exists()

    actions = await manager.heal_git_state(workspace)

    assert actions == ["cherry-pick --abort"]
    assert not (git_dir / "CHERRY_PICK_HEAD").exists()
    assert _head(workspace) == before


async def test_heal_non_repo_raises(manager: WorktreeManager, tmp_path: Path):
    bare_dir = tmp_path / "plain-dir"
    bare_dir.mkdir()
    with pytest.raises(GitError, match="not a git worktree"):
        await manager.heal_git_state(bare_dir)


# ----------------------------------------------------------------- tier1_gate


async def test_tier1_gate_pass_rebases_and_runs_suite(
    workspace: Path, manager: WorktreeManager
):
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    _commit_file(wt, "feature.py", "f = 1\n")
    main_tip = _commit_file(workspace, "other.py", "o = 1\n")  # sibling progress on main

    result = await manager.tier1_gate(wt, "main", PASS_CMD, timeout_s=30)

    assert result == Tier1Result(
        passed=True,
        rebase_conflict=False,
        conflict_payload="",
        tests_failed=False,
        test_output_path=result.test_output_path,
    )
    assert result.test_output_path is not None
    assert Path(result.test_output_path).is_file()
    # the branch now sits on top of main's tip (really rebased)
    _git(wt, "merge-base", "--is-ancestor", main_tip, "HEAD")


async def test_tier1_gate_conflict_returns_payload_and_heals(
    workspace: Path, manager: WorktreeManager
):
    _commit_file(workspace, "conflict.txt", "base\n", "base")
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    before = _commit_file(wt, "conflict.txt", "mine\n", "mine")
    _commit_file(workspace, "conflict.txt", "theirs\n", "theirs")

    result = await manager.tier1_gate(wt, "main", PASS_CMD, timeout_s=30)

    assert not result.passed
    assert result.rebase_conflict
    assert not result.tests_failed
    assert result.test_output_path is None
    assert "conflict.txt" in result.conflict_payload
    # the worktree is healed: rebase aborted, branch restored, status clean
    assert _branch_of(wt) == "stage/u1"
    assert _head(wt) == before
    assert _git(wt, "status", "--porcelain").strip() == ""


async def test_tier1_gate_failing_suite_blocks_merge(
    workspace: Path, manager: WorktreeManager
):
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    _commit_file(wt, "feature.py", "f = 1\n")

    result = await manager.tier1_gate(wt, "main", FAIL_CMD, timeout_s=30)

    assert not result.passed
    assert result.tests_failed
    assert not result.rebase_conflict
    assert result.test_output_path is not None
    assert "BOOM-FAILING-TEST" in Path(result.test_output_path).read_text(encoding="utf-8")


async def test_tier1_gate_suite_timeout_is_a_failure(
    workspace: Path, manager: WorktreeManager
):
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    _commit_file(wt, "feature.py", "f = 1\n")

    result = await manager.tier1_gate(wt, "main", SLEEP_CMD, timeout_s=1)

    assert not result.passed
    assert result.tests_failed
    assert result.test_output_path is not None
    assert "timed out" in Path(result.test_output_path).read_text(encoding="utf-8")


async def test_tier1_gate_refuses_empty_test_cmd(workspace: Path, manager: WorktreeManager):
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    with pytest.raises(GitError, match="empty test_cmd"):
        await manager.tier1_gate(wt, "main", [], timeout_s=5)


async def test_tier1_gate_refuses_detached_worktree(
    workspace: Path, manager: WorktreeManager
):
    await manager.create(workspace, "u1", "stage/u1", "main")
    scratch = await manager.create(workspace, "u1-validate", "stage/u1", "main", new_branch=False)
    with pytest.raises(GitError, match="detached"):
        await manager.tier1_gate(scratch, "main", PASS_CMD, timeout_s=5)


async def test_tier1_gate_serializes_on_the_target_branch_lock(
    workspace: Path, manager: WorktreeManager
):
    """Two concurrent gates on the same target must not overlap (rebase→test→…
    runs whole under the per-target-branch lock)."""
    wt1 = await manager.create(workspace, "u1", "stage/u1", "main")
    _commit_file(wt1, "one.py", "x = 1\n")
    wt2 = await manager.create(workspace, "u2", "stage/u2", "main")
    _commit_file(wt2, "two.py", "x = 2\n")
    marker_cmd = [
        sys.executable,
        "-c",
        (
            "import pathlib, time; "
            "pathlib.Path('gate.start').write_text(str(time.time_ns())); "
            "time.sleep(0.3); "
            "pathlib.Path('gate.end').write_text(str(time.time_ns()))"
        ),
    ]

    r1, r2 = await asyncio.gather(
        manager.tier1_gate(wt1, "main", marker_cmd, timeout_s=30),
        manager.tier1_gate(wt2, "main", marker_cmd, timeout_s=30),
    )

    assert r1.passed and r2.passed
    s1 = int((wt1 / "gate.start").read_text())
    e1 = int((wt1 / "gate.end").read_text())
    s2 = int((wt2 / "gate.start").read_text())
    e2 = int((wt2 / "gate.end").read_text())
    assert e1 <= s2 or e2 <= s1, "gate sections overlapped — lock not honored"


# ------------------------------------------------------------------ integrate


async def test_integrate_merges_with_stage_id_trailer(
    workspace: Path, manager: WorktreeManager
):
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    _commit_file(wt, "feature.py", "f = 1\n")
    gate = await manager.tier1_gate(wt, "main", PASS_CMD, timeout_s=30)
    assert gate.passed

    sha = await manager.integrate(workspace, "stage/u1", "main")

    assert sha == _head(workspace)
    body = _git(workspace, "log", "-1", "--format=%B")
    assert "Stage-Id: u1" in body
    parents = _git(workspace, "log", "-1", "--format=%P").split()
    assert len(parents) == 2  # --no-ff: a real merge commit carries the trailer
    assert (workspace / "feature.py").is_file()


async def test_integrate_stale_gate_raises_stale_gate_error(
    workspace: Path, manager: WorktreeManager
):
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    _commit_file(wt, "feature.py", "f = 1\n")
    gate = await manager.tier1_gate(wt, "main", PASS_CMD, timeout_s=30)
    assert gate.passed
    _commit_file(workspace, "sibling.py", "s = 1\n")  # target moved after the gate

    with pytest.raises(StaleGateError):
        await manager.integrate(workspace, "stage/u1", "main")
    # StaleGateError is a GitError: callers without the refinement still catch it
    assert issubclass(StaleGateError, GitError)


async def test_integrate_requires_checkout_on_target_branch(
    workspace: Path, manager: WorktreeManager
):
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    _commit_file(wt, "feature.py", "f = 1\n")
    _git(workspace, "checkout", "-b", "elsewhere")
    try:
        with pytest.raises(GitError, match="checked out on"):
            await manager.integrate(workspace, "stage/u1", "main")
    finally:
        _git(workspace, "checkout", "main")


async def test_integrate_missing_branch_raises(workspace: Path, manager: WorktreeManager):
    with pytest.raises(GitError, match="does not exist"):
        await manager.integrate(workspace, "stage/ghost", "main")


# ----------------------------------------------------------- merged_unit_diffs


async def _integrate_unit(
    manager: WorktreeManager, workspace: Path, unit: str, rel: str, content: str
) -> str:
    wt = await manager.create(workspace, unit, f"stage/{unit}", "main")
    _commit_file(wt, rel, content)
    gate = await manager.tier1_gate(wt, "main", PASS_CMD, timeout_s=30)
    assert gate.passed
    return await manager.integrate(workspace, f"stage/{unit}", "main")


async def test_merged_unit_diffs_keyed_by_stage_id_trailers(
    workspace: Path, manager: WorktreeManager
):
    freeze = _head(workspace)  # the contract-freeze commit
    first_merge = await _integrate_unit(manager, workspace, "s1", "a.py", "alpha = 1\n")
    await _integrate_unit(manager, workspace, "s2", "b.py", "beta = 2\n")

    diffs = await manager.merged_unit_diffs(workspace, "main", freeze, 100_000)

    assert set(diffs) == {"s1", "s2"}
    assert "+alpha = 1" in diffs["s1"]
    assert "+beta = 2" in diffs["s2"]
    assert "beta" not in diffs["s1"]
    assert "alpha" not in diffs["s2"]

    only_after_first = await manager.merged_unit_diffs(workspace, "main", first_merge, 100_000)
    assert set(only_after_first) == {"s2"}


async def test_merged_unit_diffs_skips_non_factory_merges(
    workspace: Path, manager: WorktreeManager
):
    freeze = _head(workspace)
    await _integrate_unit(manager, workspace, "s1", "a.py", "alpha = 1\n")
    _git(workspace, "branch", "hotfix", "main")
    wt = await manager.create(workspace, "hf", "hotfix", "main")
    _commit_file(wt, "hf.py", "h = 1\n")
    _git(workspace, "merge", "--no-ff", "-m", "manual merge without trailer", "hotfix")

    diffs = await manager.merged_unit_diffs(workspace, "main", freeze, 100_000)

    assert set(diffs) == {"s1"}


async def test_merged_unit_diffs_bounds_each_unit(workspace: Path, manager: WorktreeManager):
    freeze = _head(workspace)
    await _integrate_unit(manager, workspace, "s1", "a.py", "alpha = 'x' * 999\n" * 50)

    diffs = await manager.merged_unit_diffs(workspace, "main", freeze, 200)

    assert len(diffs["s1"].encode("utf-8")) <= 200
    assert "[truncated]" in diffs["s1"]


async def test_merged_unit_diffs_concatenates_repeat_merges_of_one_unit(
    workspace: Path, manager: WorktreeManager
):
    """A reworked unit merged twice contributes both diffs, chronologically,
    under its single Stage-Id key."""
    freeze = _head(workspace)
    await _integrate_unit(manager, workspace, "s1", "a.py", "first_pass = 1\n")
    wt = (workspace / ".worktrees" / "s1").resolve()
    _commit_file(wt, "a2.py", "rework_pass = 2\n")
    gate = await manager.tier1_gate(wt, "main", PASS_CMD, timeout_s=30)
    assert gate.passed
    await manager.integrate(workspace, "stage/s1", "main")

    diffs = await manager.merged_unit_diffs(workspace, "main", freeze, 100_000)

    assert set(diffs) == {"s1"}
    assert "+first_pass = 1" in diffs["s1"]
    assert "+rework_pass = 2" in diffs["s1"]
    assert diffs["s1"].index("first_pass") < diffs["s1"].index("rework_pass")


async def test_merged_unit_diffs_bad_since_ref_fails_explicitly(
    workspace: Path, manager: WorktreeManager
):
    with pytest.raises(GitError, match="git log"):
        await manager.merged_unit_diffs(workspace, "main", "no-such-ref", 1000)


# ------------------------------------------------- full_diff vs diff_digest


async def test_full_diff_carries_bodies_digest_carries_headers_only(
    workspace: Path, manager: WorktreeManager
):
    """§3.1 Tier-2 contract: full_diff = bodies (Tier-2 input); diff_digest =
    stat + hunk headers (CP-1 input only)."""
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    _commit_file(wt, "body.py", "SECRET_BODY_LINE = 42\n")

    full = await manager.full_diff(wt, "main", 100_000)
    digest = await manager.diff_digest(wt, "main", 100_000)

    assert "+SECRET_BODY_LINE = 42" in full
    assert "@@" in full
    assert "SECRET_BODY_LINE" not in digest  # bodies never leak into the digest
    assert "@@" in digest  # hunk headers present
    assert "body.py" in digest  # diffstat names the file
    assert "== diffstat ==" in digest and "== hunks ==" in digest


async def test_diff_primitives_are_bounded(workspace: Path, manager: WorktreeManager):
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    _commit_file(wt, "big.py", "x = 'y' * 100\n" * 200)

    full = await manager.full_diff(wt, "main", 150)
    digest = await manager.diff_digest(wt, "main", 64)

    assert len(full.encode("utf-8")) <= 150
    assert "[truncated]" in full
    assert len(digest.encode("utf-8")) <= 64


async def test_full_diff_is_committed_state_only(workspace: Path, manager: WorktreeManager):
    """Uncommitted residue (e.g. validator files) never enters gate diffs —
    committed git state is the only canonical step input (§5.5d)."""
    wt = await manager.create(workspace, "u1", "stage/u1", "main")
    _commit_file(wt, "real.py", "committed = True\n")
    (wt / "scratch.py").write_text("UNCOMMITTED_NOISE = 1\n", encoding="utf-8")

    full = await manager.full_diff(wt, "main", 100_000)

    assert "+committed = True" in full
    assert "UNCOMMITTED_NOISE" not in full
