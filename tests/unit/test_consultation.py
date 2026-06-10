"""Unit tests for sf_factory.consultation (design §8: valid verdict, invalid
JSON → fallback, unknown cp_id → breach — plus the full §4 contract: bounded
input assembly, exactly-one-JSON-object strictness, deterministic fallback,
complete consultations logging, raw stream path).

End-to-end cases drive the REAL AgentRunner against the stub agent (consultation
builds on runner, design §1); the parse matrix uses a scripted fake runner for
precise result_text control. Fixtures beyond the frozen conftest are local
(design §9).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sf_factory.config import FactoryConfig
from sf_factory.consultation import Consultor, Verdict
from sf_factory.models import ConsultationBreachError
from sf_factory.runner import AgentResult, AgentRunner

#: Keys exactly as declared by the conftest CP-1 registry entry.
CP1_INPUTS: dict[str, str] = {
    "validation_report": "2 failing: test_alpha, test_beta",
    "diff_digest": "M src/x.py | @@ -1,4 +1,9 @@",
    "spec": "spec body: the stage must do the thing",
}


def _expected_digest(inputs: dict[str, str]) -> str:
    """The documented canonical-payload contract: key-sorted compact JSON, UTF-8."""
    payload = json.dumps(
        dict(inputs), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _consultations(database) -> list:
    return database.read().execute("SELECT * FROM consultations ORDER BY id").fetchall()


def _breach_events(database) -> list:
    return (
        database.read()
        .execute(
            "SELECT * FROM events WHERE event_type = 'cp_breach_attempt' ORDER BY seq"
        )
        .fetchall()
    )


def _proc_rows(database) -> list:
    return database.read().execute("SELECT * FROM process_registry ORDER BY id").fetchall()


# ------------------------------------------------------------- fake-runner rig


class FakeRunner:
    """Duck-typed AgentRunner: records every call, returns a scripted
    AgentResult — the parse matrix needs exact result_text control without
    subprocess costs. The signature mirrors AgentRunner.run_agent."""

    def __init__(
        self,
        result_text: str = "",
        *,
        timed_out: bool = False,
        exit_code: int | None = 0,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result_text = result_text
        self._timed_out = timed_out
        self._exit_code = exit_code

    async def run_agent(
        self,
        role: str,
        prompt: str,
        *,
        unit_level: str,
        unit_id: str,
        cwd: Path,
        kind: str = "agent",
        cp_id: str | None = None,
        timeout_s: int | None = None,
        resume_session: str | None = None,
    ) -> AgentResult:
        self.calls.append(
            {
                "role": role,
                "prompt": prompt,
                "unit_level": unit_level,
                "unit_id": unit_id,
                "cwd": cwd,
                "kind": kind,
                "cp_id": cp_id,
                "timeout_s": timeout_s,
                "resume_session": resume_session,
            }
        )
        return AgentResult(
            process_id=1,
            exit_code=self._exit_code,
            timed_out=self._timed_out,
            killed=False,
            declared_failure=False,
            result_text=self._result_text,
            session_id="fake-sess",
            tokens_in=11,
            tokens_out=7,
            cost_usd=0.001,
            garbage_lines=0,
            ndjson_log_path="/fake/logs/proc-fake.ndjson",
            stderr_path="/fake/logs/proc-fake.stderr",
            duration_ms=5,
        )


def _fake_env(config_dict: dict[str, Any], database, result_text: str = "", **result_kwargs):
    cfg = FactoryConfig.model_validate(config_dict)
    fake = FakeRunner(result_text, **result_kwargs)
    return SimpleNamespace(
        cfg=cfg, db=database, fake=fake, consultor=Consultor(cfg, database, fake)
    )


async def _consult_cp1(env, inputs: dict[str, str] | None = None) -> Verdict:
    return await env.consultor.consult(
        "CP-1",
        unit_level="stage",
        unit_id="stg-1",
        inputs=CP1_INPUTS if inputs is None else inputs,
    )


# ------------------------------------------------------------ real-runner rig


@pytest.fixture()
def cenv(config_dict: dict[str, Any], db) -> SimpleNamespace:
    """Consultor wired to the REAL runner with the conftest stub routes.

    Consultations get the (empty by default) consultation canon bundle, so no
    canon files are needed on disk (D-0009 / config inject.consultation_points)."""
    cfg = FactoryConfig.model_validate(config_dict)
    return SimpleNamespace(cfg=cfg, db=db, consultor=Consultor(cfg, db, AgentRunner(cfg, db)))


# ---------------------------------------------------- end-to-end via the stub


async def test_valid_verdict_round_trip_fully_logged(
    cenv: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SF_STUB_SCENARIO", "valid_verdict:rebuild")
    verdict = await _consult_cp1(cenv)

    assert verdict == Verdict(
        cp_id="CP-1",
        value="rebuild",
        rationale="scripted verdict (stub)",
        fallback_used=False,
        consultation_id=1,
    )

    (row,) = _consultations(cenv.db)
    assert row["cp_id"] == "CP-1"
    assert row["unit_level"] == "stage" and row["unit_id"] == "stg-1"
    assert row["schema_valid"] == 1 and row["fallback_used"] == 0
    assert row["verdict"] == "rebuild"
    assert row["rationale"] == "scripted verdict (stub)"
    assert row["model"] == "stub-model"  # from config models[cp1_triage].model
    assert row["input_digest"] == _expected_digest(CP1_INPUTS)
    assert row["latency_ms"] >= 0
    assert row["tokens_in"] == 120 and row["tokens_out"] == 45  # stub usage
    assert row["cost_usd"] == pytest.approx(0.0042)
    assert row["created_at"] is not None

    # Raw stream path (§4): the runner's NDJSON log, on disk, non-empty, and
    # exactly what the process registry recorded as authoritative.
    (proc,) = _proc_rows(cenv.db)
    assert row["raw_log_path"] == proc["ndjson_log_path"]
    log_path = Path(row["raw_log_path"])
    assert log_path.is_file() and log_path.stat().st_size > 0

    # Tagged at the spawn boundary (§2 creep-scan precondition).
    assert proc["kind"] == "consultation" and proc["cp_id"] == "CP-1"
    assert proc["role"] == "cp1_triage"
    assert proc["state"] == "exited"

    assert _breach_events(cenv.db) == []  # a clean consult is never a breach


@pytest.mark.parametrize("value", ["continue_session", "rebuild", "respec", "escalate"])
async def test_every_closed_set_verdict_parses(
    config_dict: dict[str, Any], db, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _fake_env(
        config_dict, db, json.dumps({"verdict": value, "rationale": "cited evidence"})
    )
    verdict = await _consult_cp1(env)
    assert verdict.value == value
    assert verdict.fallback_used is False


async def test_invalid_verdict_engages_deterministic_fallback(
    cenv: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SF_STUB_SCENARIO", "invalid_verdict")
    verdict = await _consult_cp1(cenv)
    assert verdict.value == "escalate"  # CP-1 registry fallback
    assert verdict.fallback_used is True
    assert verdict.rationale.startswith("deterministic fallback 'escalate':")
    assert "schema-invalid" in verdict.rationale

    (row,) = _consultations(cenv.db)
    assert row["schema_valid"] == 0 and row["fallback_used"] == 1
    assert row["verdict"] == "escalate"
    assert Path(row["raw_log_path"]).is_file()  # fallback calls are logged too
    assert _breach_events(cenv.db) == []  # invalid OUTPUT is fallback, not breach


async def test_prose_only_output_falls_back_end_to_end(
    cenv: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SF_STUB_SCENARIO", "success")  # result text: 'stub success'
    verdict = await _consult_cp1(cenv)
    assert verdict.value == "escalate"
    assert verdict.fallback_used is True
    assert "found 0" in verdict.rationale
    (row,) = _consultations(cenv.db)
    assert row["schema_valid"] == 0 and row["fallback_used"] == 1


async def test_scratch_cwd_is_outside_any_worktree(
    cenv: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SF_STUB_SCENARIO", "valid_verdict:escalate")
    await _consult_cp1(cenv)
    (proc,) = _proc_rows(cenv.db)
    log_dir = cenv.cfg.process.ndjson_log_dir
    assert Path(proc["cwd"]).is_relative_to(log_dir)  # operational space, not a repo


# ------------------------------------------------------------ breach detection


async def test_unknown_cp_id_logs_breach_event_and_raises(cenv: SimpleNamespace) -> None:
    with pytest.raises(ConsultationBreachError):
        await cenv.consultor.consult(
            "CP-9", unit_level="stage", unit_id="stg-1", inputs=CP1_INPUTS
        )
    (event,) = _breach_events(cenv.db)
    assert event["unit_level"] == "stage" and event["unit_id"] == "stg-1"
    payload = json.loads(event["payload_json"])
    assert payload["cp_id"] == "CP-9"
    assert payload["registered"] == ["CP-1"]
    assert _consultations(cenv.db) == []  # a breach never becomes a consultation
    assert _proc_rows(cenv.db) == []  # and never spawns


@pytest.mark.parametrize(
    ("inputs", "missing", "unexpected"),
    [
        (  # missing one declared key
            {"validation_report": "r", "diff_digest": "d"},
            ["spec"],
            [],
        ),
        (  # undeclared extra key
            dict(CP1_INPUTS, extra="x"),
            [],
            ["extra"],
        ),
        (  # disjoint set
            {"wrong": "w"},
            ["diff_digest", "spec", "validation_report"],
            ["wrong"],
        ),
    ],
)
async def test_input_keys_must_equal_declared_inputs(
    config_dict: dict[str, Any], db, inputs, missing, unexpected
) -> None:
    env = _fake_env(config_dict, db)
    with pytest.raises(ConsultationBreachError):
        await _consult_cp1(env, inputs=inputs)
    (event,) = _breach_events(db)
    payload = json.loads(event["payload_json"])
    assert payload["missing"] == missing
    assert payload["unexpected"] == unexpected
    assert env.fake.calls == []  # rejected before any spawn
    assert _consultations(db) == []


async def test_non_string_input_value_is_breach(config_dict: dict[str, Any], db) -> None:
    env = _fake_env(config_dict, db)
    bad = dict(CP1_INPUTS)
    bad["spec"] = 42  # type: ignore[assignment]
    with pytest.raises(ConsultationBreachError):
        await _consult_cp1(env, inputs=bad)
    (event,) = _breach_events(db)
    assert json.loads(event["payload_json"])["non_string_keys"] == ["spec"]
    assert env.fake.calls == []


async def test_oversized_input_is_breach_not_truncation(
    config_dict: dict[str, Any], db
) -> None:
    config_dict["consultation_points"][0]["max_input_bytes"] = 64
    env = _fake_env(config_dict, db)
    with pytest.raises(ConsultationBreachError):
        await _consult_cp1(env)  # canonical payload of CP1_INPUTS far exceeds 64
    (event,) = _breach_events(db)
    payload = json.loads(event["payload_json"])
    assert payload["max_input_bytes"] == 64
    assert payload["payload_bytes"] > 64
    assert env.fake.calls == []  # never spawned with a truncated guess (Doctrine §7)
    assert _consultations(db) == []


async def test_input_at_exact_bound_is_allowed(config_dict: dict[str, Any], db) -> None:
    inputs = {"validation_report": "r", "diff_digest": "d", "spec": "s"}
    bound = len(
        json.dumps(dict(inputs), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        .encode("utf-8")
    )
    config_dict["consultation_points"][0]["max_input_bytes"] = bound
    env = _fake_env(
        config_dict, db, json.dumps({"verdict": "rebuild", "rationale": "ok"})
    )
    verdict = await _consult_cp1(env, inputs=inputs)
    assert verdict.fallback_used is False  # ≤ is in-bound; > breaches
    assert _breach_events(db) == []


# ------------------------------------------------- strict parse (exactly one)


@pytest.mark.parametrize(
    ("text", "reason_fragment"),
    [
        ("", "found 0"),
        ("no json here at all", "found 0"),
        ("{broken json", "found 0"),
        ('[1, 2, 3] and ["not", "an", "object"]', "found 0"),  # non-objects never count
        (
            '{"verdict": "rebuild", "rationale": "a"}\n{"verdict": "respec", "rationale": "b"}',
            "found 2",  # two verdicts = ambiguous (§4: ≠1 object)
        ),
        ('{"verdict": "rebuild"}', "schema-invalid"),  # rationale missing
        ('{"rationale": "no verdict given"}', "schema-invalid"),
        ('{"verdict": "ship_it", "rationale": "x"}', "schema-invalid"),  # outside closed set
        ('{"verdict": "rebuild", "rationale": "x", "confidence": 1}', "schema-invalid"),
        ('{"verdict": "rebuild", "rationale": 42}', "schema-invalid"),  # strict: no coercion
        ('{"verdict": "rebuild", "rationale": "   "}', "empty rationale"),
    ],
)
async def test_invalid_or_ambiguous_output_falls_back(
    config_dict: dict[str, Any], db, text: str, reason_fragment: str
) -> None:
    env = _fake_env(config_dict, db, text)
    verdict = await _consult_cp1(env)
    assert verdict.value == "escalate"
    assert verdict.fallback_used is True
    assert reason_fragment in verdict.rationale
    (row,) = _consultations(db)  # every call logged, fallback included
    assert row["schema_valid"] == 0 and row["fallback_used"] == 1
    assert row["verdict"] == "escalate"
    assert row["raw_log_path"] == "/fake/logs/proc-fake.ndjson"


async def test_single_object_wrapped_in_prose_and_fence_is_valid(
    config_dict: dict[str, Any], db
) -> None:
    text = (
        "Here is my triage decision:\n```json\n"
        '{"verdict": "continue_session", "rationale": "failing count dropped 5 -> 1"}'
        "\n```\nGood luck!"
    )
    env = _fake_env(config_dict, db, text)
    verdict = await _consult_cp1(env)
    assert verdict.value == "continue_session"
    assert verdict.fallback_used is False
    assert verdict.rationale == "failing count dropped 5 -> 1"


async def test_nested_braces_inside_the_object_count_once(
    config_dict: dict[str, Any], db
) -> None:
    text = json.dumps(
        {"verdict": "respec", "rationale": 'spec contradicts itself: {"x": 1} vs {"x": 2}'}
    )
    env = _fake_env(config_dict, db, text)
    verdict = await _consult_cp1(env)
    assert verdict.value == "respec"
    assert verdict.fallback_used is False


async def test_timed_out_call_falls_back_and_is_still_logged(
    config_dict: dict[str, Any], db
) -> None:
    env = _fake_env(config_dict, db, "", timed_out=True, exit_code=None)
    verdict = await _consult_cp1(env)
    assert verdict.value == "escalate" and verdict.fallback_used is True
    (row,) = _consultations(db)
    assert row["fallback_used"] == 1  # a dead consultation still leaves its trace


# ----------------------------------------------------------- digest + prompt


async def test_input_digest_is_canonical_and_order_independent(
    config_dict: dict[str, Any], db
) -> None:
    env = _fake_env(
        config_dict, db, json.dumps({"verdict": "rebuild", "rationale": "ok"})
    )
    scrambled = {k: CP1_INPUTS[k] for k in ["spec", "validation_report", "diff_digest"]}
    await _consult_cp1(env, inputs=CP1_INPUTS)
    await _consult_cp1(env, inputs=scrambled)
    changed = dict(CP1_INPUTS, spec="a different spec")
    await _consult_cp1(env, inputs=changed)

    rows = _consultations(db)
    digests = [r["input_digest"] for r in rows]
    assert digests[0] == digests[1] == _expected_digest(CP1_INPUTS)
    assert digests[2] == _expected_digest(changed)
    assert digests[2] != digests[0]
    assert all(len(d) == 64 and set(d) <= set("0123456789abcdef") for d in digests)


async def test_prompt_contract_and_spawn_tagging(config_dict: dict[str, Any], db) -> None:
    env = _fake_env(
        config_dict, db, json.dumps({"verdict": "escalate", "rationale": "ok"})
    )
    await _consult_cp1(env)
    (call,) = env.fake.calls
    assert call["role"] == "cp1_triage"  # registry role, never caller-chosen
    assert call["kind"] == "consultation" and call["cp_id"] == "CP-1"
    assert call["unit_level"] == "stage" and call["unit_id"] == "stg-1"
    assert call["resume_session"] is None  # pure functions never resume sessions
    assert Path(call["cwd"]).is_dir()  # runner precondition: cwd exists

    prompt = call["prompt"]
    for verdict in ("continue_session", "rebuild", "respec", "escalate"):
        assert verdict in prompt  # the closed set is spelled out
    assert "'escalate' is executed" in prompt  # fallback consequence, from config
    positions = [prompt.index(f"--- input: {key} ---") for key in CP1_INPUTS]
    assert positions == sorted(positions)  # registry-declared input order
    for value in CP1_INPUTS.values():
        assert value in prompt  # the bounded inputs themselves


async def test_fresh_scratch_cwd_per_call(config_dict: dict[str, Any], db) -> None:
    env = _fake_env(
        config_dict, db, json.dumps({"verdict": "rebuild", "rationale": "ok"})
    )
    await _consult_cp1(env)
    await _consult_cp1(env)
    cwds = [c["cwd"] for c in env.fake.calls]
    assert cwds[0] != cwds[1]
    assert all(Path(c).is_dir() for c in cwds)


# ------------------------------------------------------ registry from config


async def test_registry_is_config_driven_second_point(
    config_dict: dict[str, Any], db
) -> None:
    """A second registered point works end-to-end with ITS verdicts and ITS
    fallback — nothing about CP-1 (or 'escalate') is hardcoded."""
    config_dict["models"]["cp2_review"] = {
        "cli": "stub", "model": "stub-model-2", "mode": "print",
    }
    config_dict["consultation_points"].append(
        {
            "id": "CP-2",
            "purpose": "merge-order hint (test-only registration)",
            "inputs": ["evidence"],
            "verdicts": ["approve", "reject"],
            "fallback": "reject",
            "role": "cp2_review",
            "max_input_bytes": 1000,
        }
    )

    valid = _fake_env(
        config_dict, db, json.dumps({"verdict": "approve", "rationale": "fine"})
    )
    verdict = await valid.consultor.consult(
        "CP-2", unit_level="phase", unit_id="ph-1", inputs={"evidence": "e"}
    )
    assert verdict == Verdict(
        cp_id="CP-2", value="approve", rationale="fine",
        fallback_used=False, consultation_id=1,
    )
    (call,) = valid.fake.calls
    assert call["role"] == "cp2_review" and call["cp_id"] == "CP-2"

    # CP-1's verdict vocabulary is INVALID for CP-2: falls back to CP-2's own fallback.
    invalid = _fake_env(
        config_dict, db, json.dumps({"verdict": "rebuild", "rationale": "wrong set"})
    )
    verdict = await invalid.consultor.consult(
        "CP-2", unit_level="phase", unit_id="ph-1", inputs={"evidence": "e"}
    )
    assert verdict.value == "reject"  # config fallback, not a hardcoded 'escalate'
    assert verdict.fallback_used is True

    rows = _consultations(db)
    assert [r["cp_id"] for r in rows] == ["CP-2", "CP-2"]
    assert rows[0]["model"] == "stub-model-2"  # route model of the registry role


# ---------------------------------------------------------------- Verdict type


def test_verdict_dataclass_is_frozen() -> None:
    verdict = Verdict(
        cp_id="CP-1", value="escalate", rationale="r", fallback_used=True,
        consultation_id=1,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.value = "rebuild"  # type: ignore[misc]
