"""DoD §12.B9 — consultation contract (design §8 integration list).

CP-1 through the REAL stack (Consultor -> AgentRunner -> canonical stub
subprocess -> NDJSON parse -> consultations row): a happy path returns a
schema-valid verdict; an injected ``invalid_verdict`` engages the deterministic
fallback (``escalate``) with ``fallback_used=1`` logged. A third test proves
the fallback ROUTES: the StageExecutor executes the fallback verdict as a real
``cp1_verdict`` escalation.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from harness import FactoryEnv

from sf_factory.consultation import Consultor, _canonical_payload
from sf_factory.models import StageState
from sf_factory.runner import AgentRunner

#: Input keys must equal the registry's declared inputs (config CP-1).
_INPUTS = {
    "validation_report": "failing: 2\n- test_x asserts the spec boundary",
    "diff_digest": "src/x.py | 4 +--\n@@ -1,4 +1,4 @@",
    "spec": "# spec\nboundary must hold\n",
}


def _consultor(env: FactoryEnv) -> Consultor:
    return Consultor(env.cfg, env.db, AgentRunner(env.cfg, env.db))


async def test_b9_valid_verdict_round_trip(make_env, monkeypatch) -> None:
    env = make_env(stub="canonical")
    monkeypatch.setenv("SF_STUB_SCENARIO", "valid_verdict:rebuild")

    verdict = await _consultor(env).consult(
        "CP-1", unit_level="stage", unit_id="s1", inputs=dict(_INPUTS)
    )

    assert verdict.cp_id == "CP-1"
    assert verdict.value == "rebuild"
    assert verdict.fallback_used is False
    assert verdict.rationale  # cited rationale, non-empty by contract

    (row,) = env.consultations()
    assert row["id"] == verdict.consultation_id
    assert row["schema_valid"] == 1 and row["fallback_used"] == 0
    assert row["verdict"] == "rebuild"
    assert row["model"] == "stub-model"
    # Full call logging (DoD §3.4): digest of the canonical payload + raw stream.
    assert row["input_digest"] == hashlib.sha256(_canonical_payload(_INPUTS)).hexdigest()
    assert Path(row["raw_log_path"]).is_file()
    assert row["latency_ms"] is not None and row["tokens_in"] is not None

    # The spawn went through the runner tagged as a consultation (§2 creep scan).
    (proc,) = env.processes(role="cp1_triage")
    assert proc["kind"] == "consultation" and proc["cp_id"] == "CP-1"
    assert proc["state"] == "exited" and proc["exit_code"] == 0


async def test_b9_invalid_verdict_engages_deterministic_fallback(
    make_env, monkeypatch
) -> None:
    env = make_env(stub="canonical")
    monkeypatch.setenv("SF_STUB_SCENARIO", "invalid_verdict")

    verdict = await _consultor(env).consult(
        "CP-1", unit_level="stage", unit_id="s1", inputs=dict(_INPUTS)
    )

    # Registry fallback for CP-1 is 'escalate' (config) — executed, never guessed.
    assert verdict.value == "escalate"
    assert verdict.fallback_used is True
    assert "fallback" in verdict.rationale

    (row,) = env.consultations()
    assert row["schema_valid"] == 0 and row["fallback_used"] == 1
    assert row["verdict"] == "escalate"
    assert Path(row["raw_log_path"]).is_file()  # the garbage reply is evidence


async def test_b9_fallback_routes_to_cp1_verdict_escalation(make_env) -> None:
    """End-to-end fallback routing: a failing VALIDATE consults CP-1, the stub
    returns an out-of-set verdict, the Consultor falls back to ``escalate``,
    and the StageExecutor executes it — stage ESCALATED with an open
    ``cp1_verdict`` escalation."""
    env = make_env()
    env.seed_phase()
    env.seed_freeze_event()
    worktree = env.create_stage_worktree("s1")
    env.seed_stage("s1", StageState.BUILD, worktree=worktree)
    env.write_playbook(
        {
            "builder": {"calls": [{"write_files": {"impl.txt": "v1\n"}, "notes": False}]},
            "validator": {"calls": [{"failing": 2}]},
            "cp1": {"calls": [{"raw": '{"verdict": "not_in_set", "rationale": "x"}'}]},
        }
    )

    await env.stage_executor().execute("s1")

    assert env.stage_state("s1") is StageState.ESCALATED
    (esc,) = env.escalations("s1", status="open")
    assert esc["trigger"] == "cp1_verdict"
    (row,) = env.consultations()
    assert row["fallback_used"] == 1 and row["verdict"] == "escalate"
    # The executed transition records the fallback verdict, never the garbage.
    last = env.events("s1", "transition")[-1]
    assert last["to_state"] == "ESCALATED"
