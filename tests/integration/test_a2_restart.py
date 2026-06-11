"""DoD §12.A2 — restart integrity (design §8 integration list + §5.5 a–d).

A REAL orchestrator OS process (`python -m sf_factory.cli resume`) drives a
scripted stage; the tests SIGKILL it at controlled points and assert that a
second `resume` recovers mechanically: orphans killed by process group,
dirty worktrees reset with evidence, wedged git state healed, integrity
verified — and the stage completes with no information loss. A corrupted
artifact byte must instead ABORT the start (IntegrityError, no silent repair).

Determinism: the kill window is held open by a hanging stub agent (it streams
its init line, then sleeps), so every kill lands at a known step boundary;
all waits are deadline-bounded polls. The phase row is seeded terminal (DONE)
so the surgical stage flows run without phase-executor interference.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from harness import (
    FactoryEnv,
    commit_all,
    git,
    pid_alive,
    poll,
)

from sf_factory.models import PhaseState, StageState

_BASE_PLAYBOOK = {
    "spec": {"default": {}},
    "builder": {"default": {}},
    "validator": {"default": {"failing": 0}},
    "tier2": {"default": {"findings": []}},
}


def _seed_pending_stage(env: FactoryEnv) -> None:
    env.seed_phase(PhaseState.DONE)  # terminal: inert for the scheduler loop
    env.seed_freeze_event()
    env.seed_stage("s1", StageState.PENDING)


def _playbook_with(env: FactoryEnv, role_calls: dict) -> None:
    playbook = json.loads(json.dumps(_BASE_PLAYBOOK))
    for key, calls in role_calls.items():
        playbook.setdefault(key, {})["calls"] = calls
    env.write_playbook(playbook)


def _running_pid(env: FactoryEnv, role: str) -> dict | None:
    rows = [
        p
        for p in env.processes(role=role)
        if p["state"] == "running" and p["pid"] is not None
    ]
    return rows[0] if rows else None


def _merge_shas(env: FactoryEnv) -> list[str]:
    out = git(
        "log", env.phase_branch, "--merges", "--format=%H", cwd=env.workspace
    )
    return [line for line in out.splitlines() if line]


def _assert_no_failures(env: FactoryEnv) -> None:
    assert env.escalations() == []
    assert env.events(None, "integrity_failure") == []
    assert env.events(None, "internal_error") == []


async def test_a2_sigkill_between_steps_resume_completes(make_env) -> None:
    """Kill after BUILD is fully recorded while VALIDATE has produced nothing
    (the validator hangs at its start) — the §5.5d inter-step boundary. The
    second resume sweeps the orphan, passes integrity, re-runs the step from
    disk, and the stage completes with no human input and no escalation."""
    env = make_env(use_config_db=True)
    _seed_pending_stage(env)
    _playbook_with(env, {"validator": [{"sleep_s": 600, "skip_result": True}]})

    run1 = env.spawn_orchestrator("resume", log_name="resume1")
    row = poll(lambda: _running_pid(env, "validator"), what="hung validator running")
    env.track_pid(row["pid"])
    assert env.stage_state("s1") is StageState.VALIDATE  # BUILD fully recorded
    run1.sigkill()
    # The runner's PR_SET_PDEATHSIG backstop kills the direct child with its
    # supervisor — the registry row is left behind in 'running'.
    poll(lambda: not pid_alive(row["pid"]), what="stub death via PDEATHSIG")

    run2 = env.spawn_orchestrator("resume", log_name="resume2")
    assert run2.wait() == 0

    # §5.5a: the stale row was finalized 'orphaned' + event.
    (orphan_row,) = [p for p in env.processes() if p["id"] == row["id"]]
    assert orphan_row["state"] == "orphaned"
    assert env.events("s1", "orphaned")
    # Integrity green, stage resumed from SQLite + git and completed.
    assert "recovery complete" in run2.output
    assert env.stage_state("s1") is StageState.DONE
    (merge_sha,) = _merge_shas(env)
    assert "Stage-Id: s1" in git("show", "-s", "--format=%B", merge_sha, cwd=env.workspace)
    _assert_no_failures(env)
    assert "founder" not in {e["actor"] for e in env.events("s1")}


async def test_a2_sigkill_mid_step_orphan_group_killed_and_dirty_reset(make_env) -> None:
    """Kill MID-STEP while the builder streams: it has written uncommitted
    junk into the stage worktree and spawned a same-group grandchild. Resume
    must (a) kill the whole process GROUP (the grandchild outlives PDEATHSIG),
    (b) save dirty-worktree evidence + hard-reset (§5.5b), (c) complete the
    stage from the canonical committed state."""
    env = make_env(use_config_db=True)
    _seed_pending_stage(env)
    _playbook_with(
        env,
        {
            "builder": [
                {
                    "write_files": {"junk-dirty.txt": "uncommitted junk from a dying agent\n"},
                    "notes": False,
                    "grandchild": True,
                    "sleep_s": 600,
                    "skip_result": True,
                }
            ]
        },
    )

    run1 = env.spawn_orchestrator("resume", log_name="resume1")
    row = poll(lambda: _running_pid(env, "builder_routine"), what="hung builder running")
    env.track_pid(row["pid"])

    def grandchild_pid() -> int | None:
        try:
            lines = Path(row["ndjson_log_path"]).read_text().splitlines()
        except OSError:
            return None
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "stub_grandchild":
                return int(obj["pid"])
        return None

    gc_pid = poll(grandchild_pid, what="grandchild pid in the NDJSON log")
    env.track_pid(gc_pid)
    run1.sigkill()
    poll(lambda: not pid_alive(row["pid"]), what="builder stub death via PDEATHSIG")
    assert pid_alive(gc_pid)  # the grandchild survived the supervisor's death

    run2 = env.spawn_orchestrator("resume", log_name="resume2")
    assert run2.wait() == 0

    # (a) registry row 'orphaned', process GROUP actually dead.
    (orphan_row,) = [p for p in env.processes() if p["id"] == row["id"]]
    assert orphan_row["state"] == "orphaned"
    (event,) = env.events("s1", "orphaned")
    assert json.loads(event["payload_json"])["group_killed"] is True
    poll(lambda: not pid_alive(gc_pid), what="grandchild killed by the orphan sweep")

    # (b) dirty-worktree evidence written, then hard reset.
    (reset_event,) = env.events("s1", "dirty_worktree_reset")
    evidence = Path(json.loads(reset_event["payload_json"])["evidence"])
    assert evidence.is_file()
    assert "junk-dirty.txt" in evidence.read_text()

    # (c) the stage completed from canonical state; the junk never merged.
    assert env.stage_state("s1") is StageState.DONE
    (merge_sha,) = _merge_shas(env)
    tree = git("ls-tree", "-r", "--name-only", merge_sha, cwd=env.workspace)
    assert "junk-dirty.txt" not in tree.splitlines()
    _assert_no_failures(env)


async def test_a2_corrupt_artifact_byte_aborts_resume(make_env) -> None:
    """Corrupt one registered artifact (worktree copy AND its git blob object)
    after a mid-flight kill: verify_integrity must detect the non-terminal-unit
    mismatch and ABORT the start — IntegrityError, exit 1, no silent repair,
    no state advance (design §5.5c)."""
    env = make_env(use_config_db=True)
    _seed_pending_stage(env)
    _playbook_with(env, {"validator": [{"sleep_s": 600, "skip_result": True}]})

    run1 = env.spawn_orchestrator("resume", log_name="resume1")
    row = poll(lambda: _running_pid(env, "validator"), what="hung validator running")
    env.track_pid(row["pid"])
    run1.sigkill()
    poll(lambda: not pid_alive(row["pid"]), what="stub death via PDEATHSIG")

    ref = env.db.read().execute(
        "SELECT path, git_commit FROM artifact_refs WHERE unit_id='s1' AND kind='spec'"
    ).fetchone()
    assert ref is not None and ref["git_commit"]
    # Corrupt the worktree copy (one byte) ...
    stage_row = env.db.read().execute("SELECT worktree_path FROM stages").fetchone()
    worktree_copy = Path(stage_row["worktree_path"]) / ref["path"]
    data = bytearray(worktree_copy.read_bytes())
    data[0] ^= 0xFF
    worktree_copy.write_bytes(bytes(data))
    # ... and the committed blob object (bit rot in the object store), so no
    # resolution-precedence step can mask the loss.
    blob_sha = git(
        "rev-parse", f"{ref['git_commit']}:{ref['path']}", cwd=env.workspace
    )
    obj = env.workspace / ".git" / "objects" / blob_sha[:2] / blob_sha[2:]
    assert obj.is_file()
    obj.chmod(0o644)
    obj.write_bytes(b"corrupted-object")

    run2 = env.spawn_orchestrator("resume", log_name="resume2")
    assert run2.wait() == 1  # start aborted, nonzero exit (design §6)

    assert "integrity" in run2.output
    failures = env.events(None, "integrity_failure")
    assert len(failures) == 1
    assert "spec" in json.dumps(json.loads(failures[0]["payload_json"]))
    # No silent repair, no progress: the stage is exactly where the kill left it.
    assert env.stage_state("s1") is StageState.VALIDATE
    assert env.transitions("s1")[-1][1] == "VALIDATE"
    assert _merge_shas(env) == []


def _seed_merge_gate_stage(env: FactoryEnv) -> Path:
    env.seed_phase(PhaseState.DONE)
    env.seed_freeze_event()
    worktree = env.create_stage_worktree("s1")
    (worktree / "impl.txt").write_text("stage implementation\n", encoding="utf-8")
    commit_all(worktree, "stage impl")
    env.seed_stage("s1", StageState.MERGE_GATE, worktree=worktree)
    return worktree


def _worktree_git_dir(worktree: Path) -> Path:
    return Path(git("rev-parse", "--absolute-git-dir", cwd=worktree))


async def test_a2_sigkill_mid_merge_gate_rebase_wedge_heals_mechanically(
    make_env,
) -> None:
    """A SIGKILL mid-Tier-1-rebase leaves `.git/.../rebase-merge` (§5.5b).
    Manufacture exactly that wedged state in a MERGE_GATE stage worktree, then
    resume: heal_git_state must abort it and the gate re-runs mechanically to
    DONE — never a human escalation."""
    env = make_env(use_config_db=True)
    worktree = _seed_merge_gate_stage(env)
    env.write_playbook(_BASE_PLAYBOOK)

    # Wedge: an interrupted rebase onto a throwaway conflicting branch — the
    # on-disk state a SIGKILL mid-rebase leaves behind.
    git("branch", "wedge-tmp", env.seed_commit, cwd=env.workspace)
    tmp_wt = env.worktrees_dir / "wedge-tmp-wt"
    git("worktree", "add", "-q", str(tmp_wt), "wedge-tmp", cwd=env.workspace)
    (tmp_wt / "impl.txt").write_text("conflicting wedge content\n", encoding="utf-8")
    commit_all(tmp_wt, "wedge conflicting impl")
    rebase = subprocess.run(
        ["git", "rebase", "wedge-tmp"], cwd=worktree, capture_output=True, text=True
    )
    assert rebase.returncode != 0  # conflict — rebase left in progress
    git_dir = _worktree_git_dir(worktree)
    assert (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()
    git("worktree", "remove", "--force", str(tmp_wt), cwd=env.workspace)
    git("branch", "-D", "wedge-tmp", cwd=env.workspace)

    run = env.spawn_orchestrator("resume", log_name="resume-wedge")
    assert run.wait() == 0

    assert not (git_dir / "rebase-merge").exists()
    assert not (git_dir / "rebase-apply").exists()
    assert env.stage_state("s1") is StageState.DONE
    gate_payloads = [json.loads(e["payload_json"]) for e in env.events("s1", "tier1_gate")]
    assert gate_payloads[-1]["passed"] is True
    assert len(_merge_shas(env)) == 1
    _assert_no_failures(env)


async def test_a2_sigkill_mid_integrate_merge_wedge_heals_mechanically(
    make_env,
) -> None:
    """A SIGKILL mid-integrate leaves MERGE_HEAD in the target-branch checkout
    (§5.5b). Manufacture it in the phase checkout, then resume: the half-merge
    is aborted mechanically and the gate re-runs to a single clean merge."""
    env = make_env(use_config_db=True)
    _seed_merge_gate_stage(env)
    env.write_playbook(_BASE_PLAYBOOK)

    git("merge", "--no-commit", "--no-ff", "stage/s1", cwd=env.phase_checkout)
    phase_git_dir = _worktree_git_dir(env.phase_checkout)
    assert (phase_git_dir / "MERGE_HEAD").exists()

    run = env.spawn_orchestrator("resume", log_name="resume-merge-wedge")
    assert run.wait() == 0

    assert not (phase_git_dir / "MERGE_HEAD").exists()
    assert env.stage_state("s1") is StageState.DONE
    # Exactly ONE merge commit: the manufactured half-merge was aborted, the
    # gate's own integrate produced the only one.
    (merge_sha,) = _merge_shas(env)
    assert "Stage-Id: s1" in git("show", "-s", "--format=%B", merge_sha, cwd=env.workspace)
    _assert_no_failures(env)


async def test_a2_resume_is_idempotent_when_nothing_pending(make_env) -> None:
    """A third resume over a completed factory is a no-op: recovery green,
    nothing requeued, immediate clean exit (at-least-once safety, §5.5d)."""
    env = make_env(use_config_db=True)
    _seed_pending_stage(env)
    env.write_playbook(_BASE_PLAYBOOK)
    first = env.spawn_orchestrator("resume", log_name="resume-one")
    assert first.wait() == 0
    assert env.stage_state("s1") is StageState.DONE
    events_before = len(env.events(None))

    again = env.spawn_orchestrator("resume", log_name="resume-two")
    assert again.wait() == 0

    assert env.stage_state("s1") is StageState.DONE
    assert len(_merge_shas(env)) == 1  # no double-merge
    _assert_no_failures(env)
    # The completed unit was not re-driven (terminal categories never dispatch).
    assert [e["event_type"] for e in env.events(None)][events_before:] == []
