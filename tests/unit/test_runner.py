"""Unit tests for sf_factory.runner (design §8: oversized-line survival, tagging
enforcement, process-group kill — plus the full §5 lifecycle against the stub).

Fixtures beyond the frozen conftest are defined locally (design §9).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sf_factory import db as fdb
from sf_factory.config import FactoryConfig, ModelRoute
from sf_factory.models import (
    ConsultationBreachError,
    ProcessError,
    ProcessRecord,
    utc_now,
)
from sf_factory.runner import (
    ADAPTERS,
    TRUNCATION_MARKER,
    AgentRunner,
    ClaudeAdapter,
    CodexAdapter,
    StubAdapter,
)

CANON_DOCTRINE = "doctrine body marker-D\n"
CANON_CONVENTIONS = "conventions body marker-C\n"
CANON_FOUNDER = "founder protocol body marker-F\n"


def _write_canon_files(home: Path) -> None:
    (home / "00 - DOCTRINA.md").write_text(CANON_DOCTRINE, encoding="utf-8")
    protocols = home / "work-protocols"
    protocols.mkdir(exist_ok=True)
    (protocols / "conventions.md").write_text(CANON_CONVENTIONS, encoding="utf-8")
    (protocols / "protocol_interactiune_founder.md").write_text(
        CANON_FOUNDER, encoding="utf-8"
    )


def _build_env(
    config_dict: dict[str, Any], database, tmp_path: Path, **process_overrides
) -> SimpleNamespace:
    """Runner + config on tmp paths: canon files materialized under factory.home,
    fast kill grace, stub routes from the frozen conftest."""
    home = Path(config_dict["factory"]["home"])
    _write_canon_files(home)
    config_dict["process"]["terminate_grace_s"] = 0.4
    config_dict["process"]["kill_grace_s"] = 0.4
    config_dict["process"].update(process_overrides)
    cfg = FactoryConfig.model_validate(config_dict)
    cwd = tmp_path / "worktree"
    cwd.mkdir(exist_ok=True)
    return SimpleNamespace(cfg=cfg, runner=AgentRunner(cfg, database), cwd=cwd, db=database)


@pytest.fixture()
def renv(config_dict, db, tmp_path: Path) -> SimpleNamespace:
    return _build_env(config_dict, db, tmp_path)


def _proc_rows(database) -> list:
    return database.read().execute("SELECT * FROM process_registry ORDER BY id").fetchall()


def _events(database, event_type: str) -> list:
    return (
        database.read()
        .execute(
            "SELECT * FROM events WHERE event_type = ? ORDER BY seq", (event_type,)
        )
        .fetchall()
    )


def _ledger_rows(database) -> list:
    return database.read().execute("SELECT * FROM token_ledger ORDER BY id").fetchall()


def _log_objects(path: str) -> list[dict]:
    objects = []
    for line in Path(path).read_bytes().splitlines():
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict):
            objects.append(obj)
    return objects


async def _poll(predicate, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


# ------------------------------------------------------------- adapter contracts


def test_adapters_registry_keys() -> None:
    assert set(ADAPTERS) == {"claude", "codex", "stub"}


def test_claude_build_cmd_full_argv_order() -> None:
    # Amended by the phase-seeding design (§5/§8, D-0024): tools-on print-mode
    # agents carry `--permission-mode bypassPermissions` (print mode default-
    # denies writes; a denied write is a wedged stage), inserted after the
    # tools handling, before --append-system-prompt.
    # Amended by CCR-8 (E2BIG): the prompt is NOT in argv — trailing `-p`
    # reads it from stdin (CLI-verified against the installed claude).
    route = ModelRoute(cli="claude", model="fable", mode="print")
    cmd = ClaudeAdapter().build_cmd(
        route, "do it", system_append="CANON", resume_session="sid-1"
    )
    assert cmd == [
        "claude",
        "--model",
        "fable",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
        "--append-system-prompt",
        "CANON",
        "--resume",
        "sid-1",
        "-p",
    ]


def test_claude_build_cmd_minimal() -> None:
    # Amended by the phase-seeding design (§5/§8, D-0024) — see the full-argv
    # golden. Amended by CCR-8: trailing `-p`, prompt on stdin.
    route = ModelRoute(cli="claude", model="sonnet", mode="print")
    cmd = ClaudeAdapter().build_cmd(route, "hi")
    assert cmd == [
        "claude", "--model", "sonnet", "--output-format", "stream-json", "--verbose",
        "--permission-mode", "bypassPermissions",
        "-p",
    ]


# ------------------------------------------------- ModelRoute.tools (CCR-3/D-0017)


def test_claude_tools_off_flagset() -> None:
    """The decision_session tools-off spawn (dashboard design §4): tools='none'
    -> the installed CLI's verified flagset `--tools ""` (disables the FULL
    built-in set); resume/canon flags unaffected. Amended by CCR-8: trailing
    `-p`, prompt on stdin."""
    route = ModelRoute(cli="claude", model="fable", mode="print", tools="none")
    cmd = ClaudeAdapter().build_cmd(
        route, "discuss", system_append="CANON", resume_session="sid-7"
    )
    assert cmd == [
        "claude",
        "--model",
        "fable",
        "--output-format",
        "stream-json",
        "--verbose",
        "--tools",
        "",
        "--append-system-prompt",
        "CANON",
        "--resume",
        "sid-7",
        "-p",
    ]


def test_claude_tools_default_all_adds_no_flag() -> None:
    """Default tools='all' preserves every existing route's argv byte-for-byte."""
    route = ModelRoute(cli="claude", model="sonnet", mode="print")
    assert route.tools == "all"
    assert "--tools" not in ClaudeAdapter().build_cmd(route, "hi")


def test_codex_tools_off_is_fail_explicit() -> None:
    """No VERIFIED codex tools-off flagset exists — spawning tools-on under a
    tools-off contract would silently void the §4 structural no-write guarantee."""
    route = ModelRoute(cli="codex", model="default", mode="print", tools="none")
    with pytest.raises(ProcessError, match="tools"):
        CodexAdapter().build_cmd(route, "x")


def test_stub_ignores_tools_field(tmp_path: Path) -> None:
    """The stub spawns no tools to disable; its argv carries no tools flag
    (design §4: 'stub ignores it')."""
    route = ModelRoute(cli="stub", model="stub-model", mode="print", tools="none")
    cmd = StubAdapter(tmp_path / "stub.py").build_cmd(route, "x")
    assert "--tools" not in cmd


def test_claude_parse_lines() -> None:
    adapter = ClaudeAdapter()
    init = adapter.parse_line({"type": "system", "subtype": "init", "session_id": "s-1"})
    assert (init.kind, init.session_id) == ("init", "s-1")
    text = adapter.parse_line(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "a"},
                    {"type": "tool_use", "name": "Bash"},
                    {"type": "text", "text": "b"},
                ]
            },
        }
    )
    assert (text.kind, text.text) == ("text", "ab")
    result = adapter.parse_line(
        {
            "type": "result",
            "result": "done",
            "session_id": "s-1",
            "usage": {
                "input_tokens": 1,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 3,
                "output_tokens": 4,
            },
            "total_cost_usd": 0.5,
        }
    )
    assert result.kind == "result"
    assert result.text == "done"
    assert result.tokens_in == 6  # input + cache creation + cache read (§2 budget)
    assert result.tokens_out == 4
    assert result.cost_usd == pytest.approx(0.5)
    assert adapter.parse_line({"type": "user"}).kind == "other"


def test_codex_build_cmd_and_default_model() -> None:
    # Amended by CCR-8: the trailing positional is `-` ("instructions are read
    # from stdin", CLI-verified) — the prompt never rides argv.
    adapter = CodexAdapter()
    route = ModelRoute(cli="codex", model="default", mode="print")
    assert adapter.build_cmd(route, "build it") == [
        "codex", "exec", "--json", "--skip-git-repo-check",
        "--sandbox", "workspace-write", "-",
    ]
    named = ModelRoute(cli="codex", model="o3", mode="print")
    assert adapter.build_cmd(named, "x") == [
        "codex", "exec", "--json", "--skip-git-repo-check",
        "--sandbox", "workspace-write", "--model", "o3", "-",
    ]


def test_codex_build_cmd_reasoning_effort() -> None:
    # D-0038: codex reasoning level via `-c model_reasoning_effort="<v>"` — the
    # literal double-quotes are part of the TOML override syntax and must reach
    # argv verbatim (no shell). gpt-5.5 + xhigh is the founder-directed route.
    route = ModelRoute(cli="codex", model="gpt-5.5", mode="print", effort="xhigh")
    assert CodexAdapter().build_cmd(route, "audit it") == [
        "codex", "exec", "--json", "--skip-git-repo-check",
        "--sandbox", "workspace-write", "--model", "gpt-5.5",
        "-c", 'model_reasoning_effort="xhigh"', "-",
    ]


def test_codex_build_cmd_resume_subcommand() -> None:
    # Amended by CCR-8: `-` positional — `codex exec resume [SESSION_ID]
    # [PROMPT]` documents the same stdin contract for `-`.
    route = ModelRoute(cli="codex", model="default", mode="print")
    assert CodexAdapter().build_cmd(route, "continue", resume_session="tid-9") == [
        "codex", "exec", "resume", "tid-9", "--json", "--skip-git-repo-check",
        "--sandbox", "workspace-write", "-",
    ]


def test_codex_parse_lines_d0011_shapes() -> None:
    adapter = CodexAdapter()
    init = adapter.parse_line({"type": "thread.started", "thread_id": "t-1"})
    assert (init.kind, init.session_id) == ("init", "t-1")
    text = adapter.parse_line(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}}
    )
    assert (text.kind, text.text) == ("text", "hello")
    other_item = adapter.parse_line(
        {"type": "item.completed", "item": {"type": "command_execution"}}
    )
    assert other_item.kind == "other"
    usage = adapter.parse_line(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 3,
                "output_tokens": 7,
                "reasoning_output_tokens": 2,  # subset of output_tokens — not added
            },
        }
    )
    assert (usage.kind, usage.tokens_in, usage.tokens_out) == ("usage", 10, 7)
    assert adapter.parse_line({"type": "turn.started"}).kind == "other"


def test_codex_materialize_agents_md(tmp_path: Path) -> None:
    adapter = CodexAdapter()
    adapter.materialize_workspace(tmp_path, None)
    assert not (tmp_path / "AGENTS.md").exists()
    adapter.materialize_workspace(tmp_path, "CANON BODY")
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "CANON BODY"
    adapter.materialize_workspace(tmp_path, "CANON BODY")  # idempotent re-spawn
    with pytest.raises(ProcessError):  # never clobber divergent workspace content
        adapter.materialize_workspace(tmp_path, "DIFFERENT CANON")


def test_module_level_stub_adapter_is_unbound() -> None:
    route = ModelRoute(cli="stub", model="stub-model", mode="print")
    with pytest.raises(ProcessError):
        StubAdapter().build_cmd(route, "x")


# ----------------------------------------------------------- tagging enforcement


async def test_tagging_breaches_raise_and_insert_nothing(renv: SimpleNamespace) -> None:
    cases = [
        # kind='agent' must not carry cp_id.
        {"role": "builder_routine", "kind": "agent", "cp_id": "CP-1"},
        # kind='consultation' requires cp_id.
        {"role": "cp1_triage", "kind": "consultation", "cp_id": None},
        # unknown cp_id.
        {"role": "cp1_triage", "kind": "consultation", "cp_id": "CP-9"},
        # role not matching the registered consultation role.
        {"role": "builder_routine", "kind": "consultation", "cp_id": "CP-1"},
        # consultation role spawned as plain agent (§2 creep scan).
        {"role": "cp1_triage", "kind": "agent", "cp_id": None},
        # role outside the config role set.
        {"role": "ghost_role", "kind": "agent", "cp_id": None},
        # runner spawns agents and consultations only.
        {"role": "builder_routine", "kind": "tests", "cp_id": None},
    ]
    for case in cases:
        with pytest.raises(ConsultationBreachError):
            await renv.runner.run_agent(
                case["role"],
                "prompt",
                unit_level="stage",
                unit_id="stg-1",
                cwd=renv.cwd,
                kind=case["kind"],
                cp_id=case["cp_id"],
            )
    assert _proc_rows(renv.db) == []
    assert renv.db.read().execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


async def test_interactive_route_is_process_error(renv: SimpleNamespace) -> None:
    with pytest.raises(ProcessError):  # OPEN-4: runner is print-mode only
        await renv.runner.run_agent(
            "main_architect", "p", unit_level="stage", unit_id="stg-1", cwd=renv.cwd
        )
    assert _proc_rows(renv.db) == []


# ----------------------------------------------------------------- success path


async def test_success_lifecycle(renv: SimpleNamespace) -> None:
    result = await renv.runner.run_agent(
        "builder_routine", "build the thing", unit_level="stage", unit_id="stg-1",
        cwd=renv.cwd,
    )
    assert result.exit_code == 0
    assert not result.timed_out and not result.killed and not result.declared_failure
    assert result.result_text == "stub success"
    assert result.session_id == "stub-sess-0001"
    assert result.tokens_in == 120  # 100 + 12 cache creation + 8 cache read
    assert result.tokens_out == 45
    assert result.cost_usd == pytest.approx(0.0042)
    assert result.garbage_lines == 0
    assert result.duration_ms >= 0

    row = _proc_rows(renv.db)[0]
    assert row["state"] == "exited"
    assert row["exit_code"] == 0
    assert row["kind"] == "agent" and row["cp_id"] is None
    assert row["role"] == "builder_routine"
    assert row["session_id"] == "stub-sess-0001"
    assert row["cwd"] == str(renv.cwd)
    assert row["ndjson_log_path"] == result.ndjson_log_path
    assert isinstance(row["pid"], int)  # persisted at exec (CCR-2)
    assert row["heartbeat_at"] is not None  # initial heartbeat + stream refreshes
    assert row["ended_at"] is not None

    ledger = _ledger_rows(renv.db)
    assert len(ledger) == 1
    assert ledger[0]["tokens_in"] == 120 and ledger[0]["tokens_out"] == 45
    assert ledger[0]["estimated"] == 0
    assert ledger[0]["model"] == "stub-model"

    (spawn_event,) = _events(renv.db, "spawn")
    payload = json.loads(spawn_event["payload_json"])
    assert payload["process_id"] == result.process_id
    assert payload["pid"] == row["pid"]  # event evidence matches the registry column
    (exit_event,) = _events(renv.db, "exit")
    exit_payload = json.loads(exit_event["payload_json"])
    assert exit_payload["exit_code"] == 0
    assert exit_payload["stdin_fed"] is True  # CCR-8: prompt fed + stdin closed cleanly
    assert _events(renv.db, "usage_missing") == []

    # stderr captured to its own file, never merged into the NDJSON stream (§5.1).
    stderr_text = Path(result.stderr_path).read_text(encoding="utf-8")
    assert "stub-stderr: scenario=success" in stderr_text
    log_bytes = Path(result.ndjson_log_path).read_bytes()
    assert b"stub-stderr" not in log_bytes

    # Own process group + Linux PDEATHSIG backstop, reported by the stub itself.
    init = next(o for o in _log_objects(result.ndjson_log_path) if o.get("subtype") == "init")
    assert init["stub"]["pgid_is_self"] is True
    if sys.platform == "linux":
        assert init["stub"]["pdeathsig"] == int(signal.SIGKILL)


# ----------------------------------------------------------------- canon (D-0009)


async def test_canon_pipeline_bundle_in_cmdline(renv: SimpleNamespace) -> None:
    await renv.runner.run_agent(
        "builder_routine", "p", unit_level="stage", unit_id="stg-1", cwd=renv.cwd
    )
    cmdline = _proc_rows(renv.db)[0]["cmdline"]
    assert CANON_DOCTRINE.strip() in cmdline
    assert CANON_CONVENTIONS.strip() in cmdline
    assert CANON_FOUNDER.strip() not in cmdline  # pipeline bundle excludes it


async def test_canon_founder_facing_bundle(renv: SimpleNamespace) -> None:
    await renv.runner.run_agent(
        "phase_architect", "p", unit_level="phase", unit_id="ph-1", cwd=renv.cwd
    )
    cmdline = _proc_rows(renv.db)[0]["cmdline"]
    assert CANON_FOUNDER.strip() in cmdline


async def test_consultation_gets_no_canon_and_is_tagged(renv: SimpleNamespace) -> None:
    result = await renv.runner.run_agent(
        "cp1_triage", "triage this", unit_level="stage", unit_id="stg-1", cwd=renv.cwd,
        kind="consultation", cp_id="CP-1",
    )
    row = _proc_rows(renv.db)[0]
    assert row["kind"] == "consultation" and row["cp_id"] == "CP-1"
    assert "--append-system-prompt" not in row["cmdline"]  # D-0009 CP exception
    assert result.exit_code == 0


async def test_missing_canon_file_is_spawn_impossibility(renv: SimpleNamespace) -> None:
    (Path(renv.cfg.factory.home) / "00 - DOCTRINA.md").unlink()
    with pytest.raises(ProcessError):
        await renv.runner.run_agent(
            "builder_routine", "p", unit_level="stage", unit_id="stg-1", cwd=renv.cwd
        )
    assert _proc_rows(renv.db) == []  # failed before registration


# ------------------------------------------------------------------ spawn failure


async def test_missing_cli_binary_finalizes_killed(
    config_dict, db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dict["models"]["builder_heavy"] = {
        "cli": "claude", "model": "fable", "mode": "print",
    }
    env = _build_env(config_dict, db, tmp_path)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    with pytest.raises(ProcessError):
        await env.runner.run_agent(
            "builder_heavy", "p", unit_level="stage", unit_id="stg-1", cwd=env.cwd
        )
    row = _proc_rows(db)[0]
    assert row["state"] == "killed"
    assert row["ended_at"] is not None
    (event,) = _events(db, "spawn_failed")
    assert json.loads(event["payload_json"])["process_id"] == row["id"]


# ----------------------------------------------------------------- session resume


async def test_resume_session_plumbed_through(renv: SimpleNamespace) -> None:
    result = await renv.runner.run_agent(
        "builder_routine", "continue work", unit_level="stage", unit_id="stg-1",
        cwd=renv.cwd, resume_session="sess-resume-7",
    )
    assert result.session_id == "sess-resume-7"  # stub echoes the resumed session
    row = _proc_rows(renv.db)[0]
    assert "--resume sess-resume-7" in row["cmdline"]
    assert row["session_id"] == "sess-resume-7"
    assert (
        fdb.last_session_id(
            renv.db.read(), unit_level="stage", unit_id="stg-1", role="builder_routine"
        )
        == "sess-resume-7"
    )


# ------------------------------------------------- line tolerance (§5.2 semantics)


async def test_garbage_and_oversized_line_survival(
    renv: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SF_STUB_SCENARIO", "garbage")
    result = await renv.runner.run_agent(
        "builder_routine", "p", unit_level="stage", unit_id="stg-1", cwd=renv.cwd
    )
    # The stream survived: the final result line was parsed.
    assert result.result_text == "stub success"
    assert result.exit_code == 0
    assert result.tokens_in == 120
    # 2 non-JSON lines + 1 JSON-non-object line + 1 oversized line.
    assert result.garbage_lines == 4
    log_bytes = Path(result.ndjson_log_path).read_bytes()
    assert TRUNCATION_MARKER.strip() in log_bytes
    row = _proc_rows(renv.db)[0]
    assert row["state"] == "exited"


# --------------------------------------------------------- usage-missing policies


async def test_crash_usage_missing_estimate_policy(
    renv: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SF_STUB_SCENARIO", "crash")
    result = await renv.runner.run_agent(
        "builder_routine", "p", unit_level="stage", unit_id="stg-1", cwd=renv.cwd
    )
    assert result.exit_code == 13
    assert not result.timed_out
    assert result.result_text == "about to crash"  # last text line; no result line
    assert result.tokens_in is None and result.tokens_out is None
    (ledger,) = _ledger_rows(renv.db)
    assert ledger["estimated"] == 1  # §2: logged-stream-bytes/4, estimated=1
    assert ledger["tokens_out"] > 0
    assert ledger["tokens_in"] is None
    (event,) = _events(renv.db, "usage_missing")
    assert json.loads(event["payload_json"])["policy"] == "estimate"
    assert _proc_rows(renv.db)[0]["state"] == "exited"


async def test_crash_usage_missing_escalate_after_policy(
    config_dict, db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dict["budgets"]["usage_missing_policy"] = "escalate_after"
    env = _build_env(config_dict, db, tmp_path)
    monkeypatch.setenv("SF_STUB_SCENARIO", "crash")
    await env.runner.run_agent(
        "builder_routine", "p", unit_level="stage", unit_id="stg-1", cwd=env.cwd
    )
    (ledger,) = _ledger_rows(db)
    assert ledger["tokens_in"] is None and ledger["tokens_out"] is None
    # NULL row; StageExecutor's direct events-count check applies the
    # escalate_after policy (D-0014).
    assert ledger["estimated"] == 0
    (event,) = _events(db, "usage_missing")
    assert json.loads(event["payload_json"])["policy"] == "escalate_after"


# ------------------------------------------------------------- sentinel detection


async def test_declared_inability_sets_flag(
    renv: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit_dir = renv.cwd / "_factory" / "stages" / "stg-1"
    monkeypatch.setenv("SF_STUB_SCENARIO", "declared_inability")
    monkeypatch.setenv("SF_STUB_SENTINEL_DIR", str(unit_dir))
    result = await renv.runner.run_agent(
        "builder_routine", "p", unit_level="stage", unit_id="stg-1", cwd=renv.cwd
    )
    assert result.declared_failure is True
    assert result.exit_code == 0  # explicit inability is a clean exit (Doctrine §7)
    assert (unit_dir / "_DECLARED_FAILURE.md").is_file()


async def test_archived_sentinel_does_not_flag(
    renv: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit_dir = renv.cwd / "_factory" / "stages" / "stg-1"
    unit_dir.mkdir(parents=True)
    (unit_dir / "_DECLARED_FAILURE.md.resolved-7.md").write_text("archived", encoding="utf-8")
    result = await renv.runner.run_agent(
        "builder_routine", "p", unit_level="stage", unit_id="stg-1", cwd=renv.cwd
    )
    assert result.declared_failure is False


# ------------------------------------------------------------ validator stub aids


async def test_persistent_failure_writes_report_sidecar(
    renv: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report_dir = tmp_path / "reports"
    monkeypatch.setenv("SF_STUB_SCENARIO", "persistent_failure")
    monkeypatch.setenv("SF_STUB_REPORT_DIR", str(report_dir))
    monkeypatch.setenv("SF_STUB_FAILING", "4")
    result = await renv.runner.run_agent(
        "validator", "validate", unit_level="stage", unit_id="stg-1", cwd=renv.cwd
    )
    assert result.exit_code == 0
    sidecar = json.loads((report_dir / "validation-report.json").read_text(encoding="utf-8"))
    assert sidecar == {"failing": 4, "passing": 1, "total": 5}


async def test_verdict_scenarios_round_trip(
    renv: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SF_STUB_SCENARIO", "valid_verdict:rebuild")
    result = await renv.runner.run_agent(
        "cp1_triage", "triage", unit_level="stage", unit_id="stg-1", cwd=renv.cwd,
        kind="consultation", cp_id="CP-1",
    )
    assert json.loads(result.result_text)["verdict"] == "rebuild"

    monkeypatch.setenv("SF_STUB_SCENARIO", "invalid_verdict")
    result = await renv.runner.run_agent(
        "cp1_triage", "triage", unit_level="stage", unit_id="stg-1", cwd=renv.cwd,
        kind="consultation", cp_id="CP-1",
    )
    assert json.loads(result.result_text)["verdict"] == "not_in_any_closed_set"


# -------------------------------------------------------------- timeout + groups


async def test_timeout_sigterm_within_grace(
    renv: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SF_STUB_SCENARIO", "timeout")
    result = await renv.runner.run_agent(
        "builder_routine", "p", unit_level="stage", unit_id="stg-1", cwd=renv.cwd,
        timeout_s=1,
    )
    assert result.timed_out is True
    assert result.killed is False  # SIGTERM sufficed within terminate_grace_s
    assert result.exit_code == -int(signal.SIGTERM)
    row = _proc_rows(renv.db)[0]
    assert row["state"] == "timed_out"
    (event,) = _events(renv.db, "timeout")
    assert json.loads(event["payload_json"])["killed"] is False


async def test_timeout_sigkill_kills_whole_group(
    renv: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SF_STUB_SCENARIO", "timeout")
    monkeypatch.setenv("SF_STUB_IGNORE_TERM", "1")
    monkeypatch.setenv("SF_STUB_GRANDCHILD", "1")
    result = await renv.runner.run_agent(
        "builder_routine", "p", unit_level="stage", unit_id="stg-1", cwd=renv.cwd,
        timeout_s=1,
    )
    assert result.timed_out is True
    assert result.killed is True  # SIGTERM ignored → SIGKILL to the group
    assert result.exit_code == -int(signal.SIGKILL)
    assert _proc_rows(renv.db)[0]["state"] == "timed_out"
    # The grandchild (same process group) must be dead too — a "killed" agent's
    # subprocess tree must not keep mutating the worktree (§5.3).
    marker = next(
        o for o in _log_objects(result.ndjson_log_path) if o.get("type") == "stub_grandchild"
    )
    grandchild_pid = marker["pid"]

    def grandchild_gone() -> bool:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            return True
        return False

    assert await _poll(grandchild_gone, timeout=3.0)


# ------------------------------------------------------------------ kill_running


async def test_kill_running_kills_live_in_memory_child(
    renv: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SF_STUB_SCENARIO", "timeout")
    task = asyncio.create_task(
        renv.runner.run_agent(
            "builder_routine", "p", unit_level="stage", unit_id="stg-1", cwd=renv.cwd,
            timeout_s=30,
        )
    )

    def child_streaming() -> bool:
        rows = _proc_rows(renv.db)
        return bool(rows) and rows[0]["heartbeat_at"] is not None

    assert await _poll(child_streaming)
    row = _proc_rows(renv.db)[0]
    assert row["state"] == "running"  # CCR-2: flipped at exec, while streaming
    assert isinstance(row["pid"], int)  # persisted pid — the §5.5a sweep's key
    killed = await renv.runner.kill_running()
    assert killed == 1
    result = await asyncio.wait_for(task, timeout=5.0)
    assert result.exit_code == -int(signal.SIGKILL)
    assert _proc_rows(renv.db)[0]["state"] == "exited"  # EOF before deadline


async def test_kill_running_after_restart_uses_persisted_pid(
    renv: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§5.5a cross-restart orphan sweep, end-to-end: a FRESH AgentRunner (empty
    in-memory handle table — the orchestrator restarted) kills a still-streaming
    agent purely via the non-NULL process_registry.pid that run_agent persisted
    with db.mark_process_running (CCR-2)."""
    monkeypatch.setenv("SF_STUB_SCENARIO", "timeout")
    task = asyncio.create_task(
        renv.runner.run_agent(
            "builder_routine", "p", unit_level="stage", unit_id="stg-1", cwd=renv.cwd,
            timeout_s=30,
        )
    )

    def child_running() -> bool:
        rows = _proc_rows(renv.db)
        return bool(rows) and rows[0]["state"] == "running"

    assert await _poll(child_running)
    row = _proc_rows(renv.db)[0]
    assert isinstance(row["pid"], int)
    restarted = AgentRunner(renv.cfg, renv.db)  # no handle for this child
    killed = await restarted.kill_running()
    assert killed == 1
    result = await asyncio.wait_for(task, timeout=5.0)
    assert result.exit_code == -int(signal.SIGKILL)
    assert _proc_rows(renv.db)[0]["state"] == "exited"  # EOF before deadline


async def test_kill_running_stale_rows_with_pid_reuse_guard(renv: SimpleNamespace) -> None:
    """Rows from a previous orchestrator run (§5.5a): matching cmdline → group
    killed; mismatching cmdline (pid reuse) → never killed; dead pid → skipped."""
    argv_ours = [sys.executable, "-c", "import time; time.sleep(600)"]
    ours = subprocess.Popen(argv_ours, start_new_session=True)
    argv_stranger = [sys.executable, "-c", "import time; time.sleep(601)"]
    stranger = subprocess.Popen(argv_stranger, start_new_session=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    dead.wait()

    def _record(pid: int, cmdline: str) -> ProcessRecord:
        return ProcessRecord(
            id=None, unit_level="stage", unit_id="stg-1", kind="agent",
            role="builder_routine", cp_id=None, session_id=None, pid=pid,
            cmdline=cmdline, cwd=None, state="running", exit_code=None,
            ndjson_log_path=None, spawned_at=utc_now(), heartbeat_at=None,
            ended_at=None,
        )

    try:
        with renv.db.transaction() as conn:
            fdb.insert_process(conn, _record(ours.pid, shlex.join(argv_ours)))
            fdb.insert_process(conn, _record(stranger.pid, "completely different argv"))
            fdb.insert_process(conn, _record(dead.pid, shlex.join([sys.executable, "-c", "pass"])))
        killed = await renv.runner.kill_running()
        assert killed == 1
        assert ours.wait(timeout=5) == -int(signal.SIGKILL)
        assert stranger.poll() is None  # cmdline mismatch = pid reuse → untouched
    finally:
        for proc in (ours, stranger):
            if proc.poll() is None:
                proc.kill()
                proc.wait()


# -------------------------------------------------------------------- heartbeats


async def test_heartbeat_throttled_by_min_interval(
    config_dict, db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = fdb.heartbeat_process

    def spy(conn, process_id: int, at: str) -> None:
        calls.append(at)
        real(conn, process_id, at)

    monkeypatch.setattr("sf_factory.db.heartbeat_process", spy)

    config_dict["process"]["heartbeat_min_interval_s"] = 1000.0
    env = _build_env(config_dict, db, tmp_path)
    await env.runner.run_agent(
        "builder_routine", "p", unit_level="stage", unit_id="stg-1", cwd=env.cwd
    )
    assert len(calls) == 1  # first line beats; the rest are throttled

    calls.clear()
    config_dict["process"]["heartbeat_min_interval_s"] = 0.000001
    env2 = _build_env(config_dict, db, tmp_path)
    await env2.runner.run_agent(
        "builder_routine", "p", unit_level="stage", unit_id="stg-2", cwd=env2.cwd
    )
    assert len(calls) >= 2  # init + at least one more line


# ------------------------------------------------------------------- stub script


def test_stub_agent_is_executable() -> None:
    stub = Path(__file__).resolve().parent.parent / "stub_agent.py"
    assert stub.is_file()
    assert os.access(stub, os.X_OK), "stub_agent.py must be executable (design §8)"


def test_stub_agent_unknown_scenario_fails_explicitly(tmp_path: Path) -> None:
    stub = Path(__file__).resolve().parent.parent / "stub_agent.py"
    proc = subprocess.run(
        [sys.executable, str(stub), "--scenario", "nonsense", "p"],
        capture_output=True, cwd=tmp_path, timeout=30,
    )
    assert proc.returncode == 64
    assert b"unknown scenario" in proc.stderr


# ------------------- claude print-mode permissions (phase-seeding design §5/§8)


def test_claude_bypass_flag_present_iff_tools_enabled() -> None:
    """Phase-seeding design §5: `--permission-mode bypassPermissions` is appended
    exactly when route.tools != 'none' (print mode default-denies writes — a
    denied write is a wedged stage); tools-off Decision Sessions stay unchanged
    (their structural no-write guarantee must not be voided)."""
    adapter = ClaudeAdapter()
    tools_on = adapter.build_cmd(ModelRoute(cli="claude", model="fable", mode="print"), "x")
    tools_off = adapter.build_cmd(
        ModelRoute(cli="claude", model="fable", mode="print", tools="none"), "x"
    )
    assert tools_on[tools_on.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--permission-mode" not in tools_off
    assert "bypassPermissions" not in tools_off


def test_claude_bypass_flag_position_after_tools_before_canon() -> None:
    """§5.1 argv-order literal: the bypass flag sits after the tools handling and
    before --append-system-prompt (the position the frozen comment documents)."""
    route = ModelRoute(cli="claude", model="sonnet", mode="print")
    cmd = ClaudeAdapter().build_cmd(route, "go", system_append="CANON")
    assert cmd.index("--verbose") < cmd.index("--permission-mode")
    assert cmd.index("--permission-mode") < cmd.index("--append-system-prompt")


def test_stub_adapter_argv_carries_no_bypass_flag(tmp_path: Path) -> None:
    """The stub adapter overrides build_cmd entirely — test routes never grow the
    claude permission flag."""
    route = ModelRoute(cli="stub", model="stub-model", mode="print")
    cmd = StubAdapter(tmp_path / "stub.py").build_cmd(route, "x")
    assert "--permission-mode" not in cmd


# --------------------------- claude reasoning effort (CCR-6, §5.1 argv literal)


def test_claude_effort_flag_in_documented_position() -> None:
    """CCR-6: `--effort <e>` sits immediately after --verbose, before the
    tools/permission handling — the amended §5.1 argv-order literal.
    Amended by CCR-8: trailing `-p`, prompt on stdin."""
    route = ModelRoute(cli="claude", model="fable", mode="print", effort="xhigh")
    cmd = ClaudeAdapter().build_cmd(
        route, "do it", system_append="CANON", resume_session="sid-1"
    )
    assert cmd == [
        "claude",
        "--model",
        "fable",
        "--output-format",
        "stream-json",
        "--verbose",
        "--effort",
        "xhigh",
        "--permission-mode",
        "bypassPermissions",
        "--append-system-prompt",
        "CANON",
        "--resume",
        "sid-1",
        "-p",
    ]


def test_claude_effort_none_leaves_argv_unchanged() -> None:
    """effort=None (the default) adds no flag — every pre-CCR-6 route's argv
    stays byte-identical. Amended by CCR-8: trailing `-p`, prompt on stdin."""
    route = ModelRoute(cli="claude", model="sonnet", mode="print")
    assert route.effort is None
    cmd = ClaudeAdapter().build_cmd(route, "hi")
    assert "--effort" not in cmd
    assert cmd == [
        "claude", "--model", "sonnet", "--output-format", "stream-json", "--verbose",
        "--permission-mode", "bypassPermissions",
        "-p",
    ]


def test_claude_tools_off_with_effort_decision_session_shape() -> None:
    """The ratified decision_session route shape: tools-off AND effort=high —
    `--effort` precedes `--tools ""`; no permission bypass on a tools-off spawn.
    Amended by CCR-8: trailing `-p`, prompt on stdin."""
    route = ModelRoute(cli="claude", model="fable", mode="print", tools="none", effort="high")
    cmd = ClaudeAdapter().build_cmd(route, "discuss")
    assert cmd == [
        "claude", "--model", "fable", "--output-format", "stream-json", "--verbose",
        "--effort", "high",
        "--tools", "",
        "-p",
    ]


# ------------------------------- prompt via stdin (CCR-8, E2BIG incident fix)

#: Assertion budget for ONE argv element: comfortably under the ~128KB Linux
#: MAX_ARG_STRLEN cap that produced `[Errno 7] Argument list too long`.
MAX_SAFE_ARGV_ELEMENT_BYTES = 64 * 1024


def _assert_argv_elements_within_budget(cmd: list[str]) -> None:
    """CCR-8 helper: no single argv element may approach the kernel cap."""
    oversized = {
        arg[:48]: len(arg.encode("utf-8"))
        for arg in cmd
        if len(arg.encode("utf-8")) > MAX_SAFE_ARGV_ELEMENT_BYTES
    }
    assert oversized == {}, f"argv elements past the E2BIG budget: {oversized}"


def test_no_adapter_argv_element_exceeds_e2big_budget(tmp_path: Path) -> None:
    """§5.1 (CCR-8): argv carries flags only — with a Tier-2-sized prompt no
    adapter may emit any argv element near MAX_ARG_STRLEN, and the prompt
    itself never rides argv."""
    huge = "x" * (300 * 1024 + 17)  # the config-bounded size class that E2BIGed
    cmds = [
        ClaudeAdapter().build_cmd(
            ModelRoute(cli="claude", model="fable", mode="print"),
            huge,
            system_append="CANON",
            resume_session="sid-1",
        ),
        CodexAdapter().build_cmd(
            ModelRoute(cli="codex", model="default", mode="print"), huge
        ),
        CodexAdapter().build_cmd(
            ModelRoute(cli="codex", model="default", mode="print"),
            huge,
            resume_session="tid-9",
        ),
        StubAdapter(tmp_path / "stub.py").build_cmd(
            ModelRoute(cli="stub", model="stub-model", mode="print"), huge
        ),
    ]
    for cmd in cmds:
        _assert_argv_elements_within_budget(cmd)
        assert huge not in cmd


async def test_large_prompt_spawns_and_completes(renv: SimpleNamespace) -> None:
    """E2BIG regression (CCR-8, incident 1): a >300KB prompt — past the ~128KB
    MAX_ARG_STRLEN single-argv-string cap that killed the Tier-2 spawn on the
    first real ERP stage — spawns and completes because the prompt travels on
    stdin. The old argv path failed at exec before any NDJSON flowed."""
    prompt = "tier2 contracts+plan+diffs " + "x" * (300 * 1024)
    result = await renv.runner.run_agent(
        "builder_routine", prompt, unit_level="stage", unit_id="stg-1", cwd=renv.cwd
    )
    assert result.exit_code == 0
    assert not result.timed_out and not result.killed
    assert result.result_text == "stub success"
    # The stub DRAINED the full prompt from stdin: length + digest echoed in
    # its init line (never the raw prompt — NDJSON line bound).
    raw = prompt.encode("utf-8")
    init = next(o for o in _log_objects(result.ndjson_log_path) if o.get("subtype") == "init")
    assert init["stub"]["prompt_bytes"] == len(raw)
    assert init["stub"]["prompt_sha256"] == hashlib.sha256(raw).hexdigest()
    # Feeder wrote everything and closed stdin cleanly (EOF reached the stub).
    (exit_event,) = _events(renv.db, "exit")
    assert json.loads(exit_event["payload_json"])["stdin_fed"] is True
    assert _proc_rows(renv.db)[0]["state"] == "exited"


async def test_early_exit_child_with_pending_prompt_is_contained(
    renv: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CCR-8 containment: the 'crash' stub exits 13 WITHOUT ever reading stdin
    while >300KB of prompt is still pending in the feeder (well past the ~64KB
    pipe buffer). BrokenPipeError/ConnectionResetError on the feed is a NORMAL
    path: no deadlock against the readline loop, no unhandled exception —
    AgentResult carries the exit semantics, the exit event records
    stdin_fed=False."""
    monkeypatch.setenv("SF_STUB_SCENARIO", "crash")
    prompt = "y" * (300 * 1024)
    result = await asyncio.wait_for(
        renv.runner.run_agent(
            "builder_routine", prompt, unit_level="stage", unit_id="stg-1", cwd=renv.cwd
        ),
        timeout=20.0,
    )
    assert result.exit_code == 13
    assert not result.timed_out and not result.killed
    assert result.result_text == "about to crash"  # the stream was still parsed
    (exit_event,) = _events(renv.db, "exit")
    assert json.loads(exit_event["payload_json"])["stdin_fed"] is False
    assert _proc_rows(renv.db)[0]["state"] == "exited"


# ---------------- AGENTS.md lifecycle (D-0029: the codex D-0009 artifact scrub)
#
# These runs spawn hermetic fake `codex`/`claude` executables steered via PATH
# (precedent: test_missing_cli_binary_finalizes_killed) so the REAL adapters —
# argv, materialization, NDJSON shapes — are exercised end-to-end without the
# installed binaries or any network.


def _install_fake_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, body: str
) -> None:
    """Install an executable fake CLI named `name` ahead of everything on PATH.
    The script runs under the current interpreter; every body drains stdin
    first (the CCR-8 prompt contract) and emits CLI-shaped NDJSON."""
    bin_dir = tmp_path / "fake-cli-bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / name
    script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


_FAKE_CODEX_OBSERVER = """
import json, os, sys
sys.stdin.read()  # CCR-8: the prompt arrives on stdin — drain it
exists = os.path.exists("AGENTS.md")
canon = exists and "SF-F5 CANON" in open("AGENTS.md", encoding="utf-8").read()
lines = [
    {"type": "thread.started", "thread_id": "thr-fake-audit"},
    {"type": "item.completed",
     "item": {"type": "agent_message", "text": "agents_md=%s canon=%s" % (exists, canon)}},
    {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 3}},
]
for line in lines:
    print(json.dumps(line), flush=True)
"""

_FAKE_CODEX_CLOBBERER = """
import json, sys
sys.stdin.read()
with open("AGENTS.md", "w", encoding="utf-8") as fh:
    fh.write("CLOBBERED BY THE AGENT MID-RUN\\n")
lines = [
    {"type": "thread.started", "thread_id": "thr-fake-clobber"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "clobbered"}},
    {"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 2}},
]
for line in lines:
    print(json.dumps(line), flush=True)
"""

_FAKE_CLAUDE = """
import json, sys
sys.stdin.read()
init = {"type": "system", "subtype": "init", "session_id": "sess-fake-claude"}
result = {"type": "result", "result": "claude done", "session_id": "sess-fake-claude",
          "usage": {"input_tokens": 3, "output_tokens": 2}, "total_cost_usd": 0.001}
print(json.dumps(init), flush=True)
print(json.dumps(result), flush=True)
"""

_FAKE_CODEX_CWD_NUKER = """
import json, os, sys
sys.stdin.read()
lines = [
    {"type": "thread.started", "thread_id": "thr-fake-nuke"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "nuked"}},
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
]
for line in lines:
    print(json.dumps(line), flush=True)
cwd = os.getcwd()
for name in os.listdir(cwd):
    os.remove(os.path.join(cwd, name))
os.rmdir(cwd)
"""


async def test_codex_route_leaves_no_agents_md_after_run(
    config_dict, db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-0029 regression (first hit: foundation.config-registry): the canon the
    codex adapter materializes as cwd/AGENTS.md (D-0009) is gone once the run
    is over — left behind in the stage worktree it trips the §3.1
    validator-isolation assertion (`?? AGENTS.md` → IntegrityError → spurious
    escalation) when audit findings route the stage back to BUILD."""
    _install_fake_cli(tmp_path, monkeypatch, "codex", _FAKE_CODEX_OBSERVER)
    config_dict["models"]["auditor_cross_model"] = {
        "cli": "codex", "model": "default", "mode": "print",
    }
    env = _build_env(config_dict, db, tmp_path)
    assert not (env.cwd / "AGENTS.md").exists()
    result = await env.runner.run_agent(
        "auditor_cross_model", "audit the stage", unit_level="stage", unit_id="stg-1",
        cwd=env.cwd,
    )
    assert result.exit_code == 0
    assert not result.timed_out and not result.killed
    assert result.session_id == "thr-fake-audit"
    # The child really saw the materialized canon in its cwd during the run...
    assert result.result_text == "agents_md=True canon=True"
    # ...and the artifact did not outlive it.
    assert not (env.cwd / "AGENTS.md").exists()
    assert _proc_rows(db)[0]["state"] == "exited"


async def test_codex_route_restores_overwritten_preexisting_agents_md(
    config_dict, db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing cwd/AGENTS.md comes back byte-identical after a codex run
    that overwrote it. The adapter itself never overwrites divergent content
    (ProcessError, D-0009) — the overwriter is the AGENT (codex runs under
    `--sandbox workspace-write`), so the run is a consultation spawn: CPs get
    no canon (D-0009 exception) and are the codex route that admits a
    divergent pre-existing file."""
    _install_fake_cli(tmp_path, monkeypatch, "codex", _FAKE_CODEX_CLOBBERER)
    config_dict["models"]["cp1_triage"] = {"cli": "codex", "model": "default", "mode": "print"}
    env = _build_env(config_dict, db, tmp_path)
    original = "# workspace-owned AGENTS.md\nbyte-exact content — ñ\n".encode()
    (env.cwd / "AGENTS.md").write_bytes(original)
    result = await env.runner.run_agent(
        "cp1_triage", "triage", unit_level="stage", unit_id="stg-1", cwd=env.cwd,
        kind="consultation", cp_id="CP-1",
    )
    assert result.exit_code == 0
    assert result.result_text == "clobbered"  # the agent really overwrote it mid-run
    assert (env.cwd / "AGENTS.md").read_bytes() == original


async def test_claude_route_preexisting_agents_md_untouched(
    config_dict, db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude materialization is a no-op (canon rides --append-system-prompt,
    D-0009) — the D-0029 snapshot/restore must not delete or rewrite a
    workspace's own AGENTS.md on claude routes: content AND mtime unchanged
    (an untouched file is never rewritten in place)."""
    _install_fake_cli(tmp_path, monkeypatch, "claude", _FAKE_CLAUDE)
    config_dict["models"]["builder_routine"] = {
        "cli": "claude", "model": "fable", "mode": "print",
    }
    env = _build_env(config_dict, db, tmp_path)
    original = b"# workspace-owned AGENTS.md\nnot the factory's artifact\n"
    target = env.cwd / "AGENTS.md"
    target.write_bytes(original)
    mtime_before = target.stat().st_mtime_ns
    result = await env.runner.run_agent(
        "builder_routine", "build", unit_level="stage", unit_id="stg-1", cwd=env.cwd
    )
    assert result.exit_code == 0
    assert result.result_text == "claude done"
    assert target.read_bytes() == original
    assert target.stat().st_mtime_ns == mtime_before


async def test_agents_md_cleanup_oserror_contained(
    config_dict, db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup failure must never mask the agent result (D-0029; §5.5b recovery
    owns crash-window residue): the agent deletes its entire cwd, so the
    post-run restore of the pre-existing AGENTS.md hits ENOENT — run_agent
    neither raises nor degrades the AgentResult, and the registry row still
    finalizes 'exited'."""
    _install_fake_cli(tmp_path, monkeypatch, "codex", _FAKE_CODEX_CWD_NUKER)
    config_dict["models"]["cp1_triage"] = {"cli": "codex", "model": "default", "mode": "print"}
    env = _build_env(config_dict, db, tmp_path)
    (env.cwd / "AGENTS.md").write_bytes(b"original bytes\n")
    result = await env.runner.run_agent(
        "cp1_triage", "triage", unit_level="stage", unit_id="stg-1", cwd=env.cwd,
        kind="consultation", cp_id="CP-1",
    )
    assert not env.cwd.exists()  # the agent really removed the directory
    assert result.exit_code == 0
    assert not result.timed_out and not result.killed and not result.declared_failure
    assert result.result_text == "nuked"
    assert result.session_id == "thr-fake-nuke"
    assert _proc_rows(db)[0]["state"] == "exited"
