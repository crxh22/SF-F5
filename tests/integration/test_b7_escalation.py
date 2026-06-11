"""DoD §12.B7 — escalation fires (design §8 integration list).

A persistently failing stage (validator reports a non-decreasing failing-test
count across BUILD→VALIDATE loops) must trigger the ``max_fix_iterations``
escalation WITHOUT human prompting: pre-threshold iterations are routed by a
real CP-1 consultation (stub verdict ``rebuild``, executed as a fresh BUILD),
the threshold fires mechanically at the §2 SQL window, the escalation row
lands, and the founder channel is paged (ntfy stub called).

Real components end-to-end: StageExecutor + StateMachine + ThresholdEvaluator +
Consultor + AgentRunner spawning real stub subprocesses + real git worktrees.
Only notify is a recording stub.
"""

from __future__ import annotations

import json

from harness import FactoryEnv

from sf_factory.models import StageState

MAX_FIX = 3  # mirrors tests/conftest.py escalation.max_fix_iterations


async def _drive_persistent_failure(env: FactoryEnv, failing_seq: list[int]) -> None:
    """Seed a stage at BUILD and drive it: builder writes a distinct file per
    call (no churn interference — max_fix must be the deciding trigger),
    validator reports the scripted failing counts, CP-1 answers ``rebuild``
    for every pre-threshold iteration."""
    env.seed_phase()
    env.seed_freeze_event()
    worktree = env.create_stage_worktree("s1")
    env.seed_stage("s1", StageState.BUILD, worktree=worktree)
    builds = len(failing_seq)
    env.write_playbook(
        {
            "builder": {
                "calls": [
                    {"write_files": {f"src_build_{i}.txt": f"attempt {i}\n"}, "notes": False}
                    for i in range(1, builds + 1)
                ],
                "default": None,
            },
            "validator": {
                "calls": [{"failing": n} for n in failing_seq],
                "default": None,
            },
            "cp1": {
                "calls": [{"verdict": "rebuild"} for _ in range(builds - 1)],
                "default": None,
            },
        }
    )
    await env.stage_executor().execute("s1")


async def test_b7_max_fix_iterations_escalates_without_human_input(make_env) -> None:
    env = make_env()
    await _drive_persistent_failure(env, [3] * MAX_FIX)

    # Escalation row fired mechanically and the stage is blocked on it.
    assert env.stage_state("s1") is StageState.ESCALATED
    (esc,) = env.escalations("s1", status="open")
    assert esc["trigger"] == "max_fix_iterations"
    assert esc["target"] == "phase_architect"
    # Payload = artifacts, not narrative (DoD §8): the evidence artifact is
    # registered and committed in the stage worktree.
    assert esc["payload_artifact_id"] is not None
    evidence = json.loads(
        env.events("s1", "transition")[-1]["payload_json"]
    )
    assert evidence["triggers"] == ["max_fix_iterations"]

    # Exactly max_fix_iterations non-decreasing iterations were recorded.
    rows = env.db.read().execute(
        "SELECT iteration, failing_tests FROM fix_iterations WHERE stage_id='s1'"
        " ORDER BY iteration"
    ).fetchall()
    assert [(r["iteration"], r["failing_tests"]) for r in rows] == [
        (i, 3) for i in range(1, MAX_FIX + 1)
    ]

    # Pre-threshold iterations consulted CP-1 (thresholds did not decide), and
    # every consultation was a real schema-valid 'rebuild' through the runner.
    consultations = env.consultations()
    assert len(consultations) == MAX_FIX - 1
    assert all(
        c["cp_id"] == "CP-1"
        and c["verdict"] == "rebuild"
        and c["schema_valid"] == 1
        and c["fallback_used"] == 0
        for c in consultations
    )
    cp_rows = env.processes(role="cp1_triage")
    assert len(cp_rows) == MAX_FIX - 1
    assert all(r["kind"] == "consultation" and r["cp_id"] == "CP-1" for r in cp_rows)

    # The loop really iterated: one builder + one validator spawn per iteration.
    assert len(env.processes(role="builder_routine")) == MAX_FIX
    assert len(env.processes(role="validator")) == MAX_FIX

    # ntfy stub called (§8 B7), max priority, no human input anywhere.
    alerts = [p for p in env.notify.published if "Escaladare" in p[0]]
    assert alerts and alerts[0][2] == "max"
    assert "max_fix_iterations" in alerts[0][0]
    actors = {e["actor"] for e in env.events("s1")}
    assert "founder" not in actors
    assert env.db.read().execute("SELECT COUNT(*) FROM decision_requests").fetchone()[0] == 0


async def test_b7_variant_fires_at_n_plus_one_iterations(make_env) -> None:
    """The n+1-iterations variant (§8): an early DECREASE pushes the firing to
    iteration n+1 — with >n rows of history the corrected §2 SQL (LAG computed
    after LIMIT) must still fire; the naive LAG-before-LIMIT form goes silent
    exactly here."""
    env = make_env()
    await _drive_persistent_failure(env, [4, 3, 3, 3])  # fires at iteration 4 = n+1

    assert env.stage_state("s1") is StageState.ESCALATED
    (esc,) = env.escalations("s1", status="open")
    assert esc["trigger"] == "max_fix_iterations"

    rows = env.db.read().execute(
        "SELECT failing_tests FROM fix_iterations WHERE stage_id='s1' ORDER BY iteration"
    ).fetchall()
    assert [r["failing_tests"] for r in rows] == [4, 3, 3, 3]

    # Iterations 1..3 did not decide (3 CP-1 rebuilds), iteration 4 fired.
    assert [c["verdict"] for c in env.consultations()] == ["rebuild"] * MAX_FIX
    assert len(env.processes(role="builder_routine")) == MAX_FIX + 1
    assert [p for p in env.notify.published if "max_fix_iterations" in p[0]]
