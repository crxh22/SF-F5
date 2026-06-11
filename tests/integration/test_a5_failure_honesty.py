"""DoD §12.A5 — failure honesty (design §8 integration list).

An agent that cannot proceed writes ``_DECLARED_FAILURE.md`` instead of
guessing (canonical stub scenario ``declared_inability``); the control plane
detects the sentinel mechanically, escalates (``agent_declared_failure``),
and NEVER retries — exactly one agent spawn in ``process_registry``.
"""

from __future__ import annotations

from sf_factory.models import StageState


async def test_a5_declared_inability_escalates_with_zero_retries(
    make_env, monkeypatch
) -> None:
    env = make_env(stub="canonical")
    env.seed_phase()
    env.seed_stage("s1", StageState.PENDING)
    # The stage worktree is created by the dispatch step at this deterministic
    # path; the stub drops the sentinel into the §4 frozen stage layout there.
    sentinel_dir = env.worktrees_dir / "s1" / "_factory" / "stages" / "s1"
    monkeypatch.setenv("SF_STUB_SCENARIO", "declared_inability")
    monkeypatch.setenv("SF_STUB_SENTINEL_DIR", str(sentinel_dir))

    await env.stage_executor().execute("s1")

    # Sentinel on disk, event recorded, escalation routed up — stage blocked.
    assert (sentinel_dir / "_DECLARED_FAILURE.md").is_file()
    assert env.stage_state("s1") is StageState.ESCALATED
    (esc,) = env.escalations("s1", status="open")
    assert esc["trigger"] == "agent_declared_failure"
    assert esc["target"] == "phase_architect"
    (declared,) = env.events("s1", "declared_failure")
    # §5.4 dedup cursor: the escalation records the firing events.seq.
    assert esc["event_seq"] == declared["seq"]

    # Failure honesty: ZERO retries — exactly one agent process ever spawned,
    # and it exited cleanly (the inability was declared, not crashed).
    procs = env.processes()
    assert len(procs) == 1
    assert procs[0]["role"] == "spec_agent" and procs[0]["kind"] == "agent"
    assert procs[0]["state"] == "exited" and procs[0]["exit_code"] == 0

    # The escalation paged the founder channel mechanically (§8/Doctrine §20).
    assert any("agent_declared_failure" in title for title, _, _ in env.notify.published)
    # No human input anywhere in the flow.
    assert "founder" not in {e["actor"] for e in env.events("s1")}
