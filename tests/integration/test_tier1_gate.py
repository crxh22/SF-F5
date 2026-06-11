"""Tier-1 merge gate (design §8 integration list): seeded textual conflict ->
conflict payload routed to the owning unit; seeded failing suite -> merge
blocked. Real WorktreeManager gates over real git history, driven through the
real StageExecutor conveyor; both scenarios then complete (bounded loops):
the builder resolves the conflict / fixes the suite and the stage merges.
"""

from __future__ import annotations

import json

from harness import MARKER_SUITE, FactoryEnv, commit_all, git

from sf_factory.models import StageState


def _seed_merge_gate_stage(env: FactoryEnv, stage_id: str = "s1"):
    """Stage parked AT MERGE_GATE with a manually built worktree (committed
    impl), as the conveyor leaves it after VALIDATE on a routine stage."""
    env.seed_phase()
    env.seed_freeze_event()
    worktree = env.create_stage_worktree(stage_id)
    env.seed_stage(stage_id, StageState.MERGE_GATE, worktree=worktree)
    return worktree


async def test_tier1_seeded_textual_conflict_payload_routed_then_resolved(
    make_env,
) -> None:
    env = make_env()
    worktree = _seed_merge_gate_stage(env)
    # Seed the textual conflict: the stage edits conflict.txt on its branch...
    (worktree / "conflict.txt").write_text("stage version\n", encoding="utf-8")
    commit_all(worktree, "stage edit of conflict.txt")
    # ...and the phase integration branch advanced with a diverging edit.
    (env.phase_checkout / "conflict.txt").write_text("phase version\n", encoding="utf-8")
    commit_all(env.phase_checkout, "phase edit of conflict.txt")

    env.write_playbook(
        {
            # Rework call: the builder resolves the conflict the way a real
            # agent does — rebase in its worktree, fix the file, continue.
            "builder": {
                "calls": [
                    {
                        "notes": False,
                        "script": [
                            {"op": "git", "args": ["rebase", "phase/ph1"], "allow_fail": True},
                            {
                                "op": "write",
                                "path": "conflict.txt",
                                "content": "resolved version\n",
                            },
                            {"op": "git", "args": ["add", "conflict.txt"]},
                            {"op": "git", "args": ["rebase", "--continue"]},
                            {
                                "op": "append",
                                "path": "_factory/stages/s1/build-notes.md",
                                "content": "resolved tier1 conflict\n",
                            },
                        ],
                    }
                ]
            },
            "validator": {"default": {"failing": 0}},
            "tier2": {"default": {"findings": []}},
        }
    )

    await env.stage_executor().execute("s1")

    # First gate: rebase conflict detected, payload captured and routed back.
    gates = [json.loads(e["payload_json"]) for e in env.events("s1", "tier1_gate")]
    assert gates[0]["rebase_conflict"] is True
    routed = [
        e
        for e in env.events("s1", "transition")
        if e["from_state"] == "MERGE_GATE" and e["to_state"] == "BUILD"
    ]
    assert len(routed) == 1
    ref = env.db.read().execute(
        "SELECT * FROM artifact_refs WHERE unit_id='s1' AND kind='tier1_conflict'"
    ).fetchone()
    assert ref is not None and ref["git_commit"]
    # The payload is evidence: the conflicting rebase output, not narrative.
    payload_text = git(
        "show", f"{ref['git_commit']}:{ref['path']}", cwd=env.workspace
    )
    assert "conflict" in payload_text.lower() and "git rebase phase/ph1" in payload_text

    # Resolution loop completed mechanically: re-gate clean, merged.
    assert env.stage_state("s1") is StageState.DONE
    assert gates[-1]["passed"] is True
    assert env.escalations("s1") == []
    assert (
        git("show", "phase/ph1:conflict.txt", cwd=env.workspace) == "resolved version"
    )


async def test_tier1_seeded_failing_suite_blocks_merge_until_fixed(make_env) -> None:
    env = make_env(test_command=MARKER_SUITE)  # fails until suite-ok.marker exists
    _seed_merge_gate_stage(env)
    env.write_playbook(
        {
            "builder": {
                "calls": [{"write_files": {"suite-ok.marker": "ok\n"}, "notes": False}]
            },
            "validator": {"default": {"failing": 0}},
            "tier2": {"default": {"findings": []}},
        }
    )

    await env.stage_executor().execute("s1")

    # First gate: full suite failed -> merge BLOCKED, stage routed to BUILD.
    gates = [json.loads(e["payload_json"]) for e in env.events("s1", "tier1_gate")]
    assert gates[0]["tests_failed"] is True and gates[0]["passed"] is False
    routed = [
        e
        for e in env.events("s1", "transition")
        if e["from_state"] == "MERGE_GATE" and e["to_state"] == "BUILD"
    ]
    assert len(routed) == 1
    # The suite ran as a registered kind='tests' process both times: fail, pass.
    suites = env.processes(role="test_suite")
    assert [s["exit_code"] for s in suites] == [1, 0]
    assert all(s["kind"] == "tests" for s in suites)

    # No merge landed while the suite failed; exactly one after the fix.
    merges = [
        line
        for line in git(
            "log", env.phase_branch, "--merges", "--format=%H", cwd=env.workspace
        ).splitlines()
        if line
    ]
    assert len(merges) == 1
    assert env.stage_state("s1") is StageState.DONE
    assert gates[-1]["passed"] is True
    # The suite's failure evidence file exists (escalation-payload material).
    assert gates[0]["test_output_path"]
