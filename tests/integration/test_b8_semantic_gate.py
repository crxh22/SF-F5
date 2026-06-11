"""DoD §12.B8 — semantic gate ROUTING harness (design §8 integration list).

A stub Integration Validator returns a seeded Tier-2 finding at the stage
merge gate; both resolution paths complete through the real conveyor:

- COMPLY: the finding routes the stage back to BUILD, the rework re-gates
  clean, the merge completes and the finding closes mechanically ('complied').
- CONTEST: on a structural stage the open Tier-2 finding enters the §5.2
  executor triage; a contest logs the rationale artifact and escalates
  ('unresolved_contest'); the architect's resolution overrules the finding and
  the stage completes.

Scope honesty (design §8): this proves ROUTING ONLY — never Tier-2 input
sufficiency. B8 is marked done only when the real seeded-conflict fixture
(two stages, Tier 1 green, shared invariant broken — built with real agents at
criterion time) passes the full §3.1 Tier-2 input contract; that fixture is
the hard gate blocking DoD §12.A6 and is OUT of bootstrap-wave scope.
"""

from __future__ import annotations

import json

from harness import FactoryEnv, git

from sf_factory import db as fdb
from sf_factory.models import StageState

_FINDING = {
    "ref": "INT-1",
    "severity": "major",
    "summary": "shared invariant broken in substance",
    "location": "impl.txt:1",
}


def _merge_commits(env: FactoryEnv, stage_id: str) -> list[str]:
    out = git(
        "log", env.phase_branch, f"--grep=Stage-Id: {stage_id}", "--format=%H", cwd=env.workspace
    )
    return [line for line in out.splitlines() if line]


async def test_b8_comply_path_routes_back_and_closes_finding(make_env) -> None:
    env = make_env()
    env.seed_phase()
    env.seed_freeze_event()
    env.seed_stage("s1", StageState.PENDING, risk="routine")
    env.write_playbook(
        {
            "spec": {"default": {}},
            "builder": {
                "calls": [
                    {"write_files": {"impl.txt": "v1 violates invariant\n"}, "notes": False},
                    {"write_files": {"impl.txt": "v2 reworked per INT-1\n"}, "notes": False},
                ]
            },
            "validator": {"default": {"failing": 0}},
            "tier2": {"calls": [{"findings": [_FINDING]}], "default": {"findings": []}},
        }
    )

    await env.stage_executor().execute("s1")

    # The seeded finding was routed back to the owning unit (MERGE_GATE -> BUILD).
    routed = [
        e
        for e in env.events("s1", "transition")
        if e["from_state"] == "MERGE_GATE" and e["to_state"] == "BUILD"
    ]
    assert len(routed) == 1
    assert json.loads(routed[0]["payload_json"])["finding_refs"] == ["INT-1"]

    # Resolution loop completed: rework re-gated clean, merged, finding closed.
    assert env.stage_state("s1") is StageState.DONE
    (finding,) = env.findings("s1")
    assert finding["auditor_role"] == "integration_validator"
    assert finding["finding_ref"] == "INT-1"
    assert finding["status"] == "complied" and finding["resolved_by"] == "executor"
    assert env.escalations("s1") == []

    # Exactly one integration merge carries the stage, with the reworked content.
    (merge_sha,) = _merge_commits(env, "s1")
    assert (
        git("show", f"{merge_sha}:impl.txt", cwd=env.workspace)
        == "v2 reworked per INT-1"
    )
    # Two Tier-2 invocations (finding, then clean) — both with sibling scope.
    gates = env.events("s1", "tier2_gate")
    assert [json.loads(e["payload_json"])["findings"] for e in gates] == [["INT-1"], []]


async def test_b8_contest_path_logs_escalates_and_completes(make_env) -> None:
    env = make_env()
    env.seed_phase()
    env.seed_freeze_event()
    env.seed_stage("s1", StageState.PENDING, risk="structural")
    env.write_playbook(
        {
            "spec": {"default": {}},
            "builder": {
                "calls": [
                    {"write_files": {"impl.txt": "v1\n"}, "notes": False},
                    {"write_files": {"impl.txt": "v2 after tier2 finding\n"}, "notes": False},
                ]
            },
            "validator": {"default": {"failing": 0}},
            "audit": {"default": {"findings": []}},
            "tier2": {"calls": [{"findings": [_FINDING]}], "default": {"findings": []}},
            "respond": {"calls": [{"action_default": "contest"}]},
        }
    )

    executor = env.stage_executor()
    await executor.execute("s1")

    # The Tier-2 finding entered the §5.2 triage on the next AUDIT pass; the
    # executor contested: rationale artifact logged + escalation, stage blocked.
    assert env.stage_state("s1") is StageState.ESCALATED
    (finding,) = env.findings("s1")
    assert finding["status"] == "contested"
    assert finding["contest_artifact_id"] is not None  # contests always logged
    (esc,) = env.escalations("s1", status="open")
    assert esc["trigger"] == "unresolved_contest" and esc["target"] == "phase_architect"

    # Architect resolves the contest in the executor's favor (rework target
    # VALIDATE = contest prevailed, §5.2 Resolve) — the loop then completes.
    with env.db.transaction() as conn:
        fdb.resolve_escalation(conn, esc["id"], "rework:VALIDATE")
    await executor.execute("s1")

    assert env.stage_state("s1") is StageState.DONE
    (finding,) = env.findings("s1")
    assert finding["status"] == "overruled"
    assert finding["resolved_by"] == "phase_architect"
    assert env.escalations("s1", status="open") == []
    assert len(_merge_commits(env, "s1")) == 1

    # Routing shape: auditors ran twice each (clean both rounds), the triage
    # ran once, and the contested content merged WITHOUT a forced rework.
    assert len(env.processes(role="auditor_same_model")) == 2
    assert len(env.processes(role="auditor_cross_model")) == 2
    # builder_heavy = structural builds (2) + executor triage (1).
    assert len(env.processes(role="builder_heavy")) == 3
