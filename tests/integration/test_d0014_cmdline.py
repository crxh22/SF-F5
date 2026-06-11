"""D-0014 item 2 — /proc/<pid>/cmdline vs the registry cmdline (design §5.5a).

Empirical findings (server e9, 2026-06-11, this wave):

- ``claude`` is a native ELF binary (symlink to ``~/.local/share/claude/
  versions/2.1.173``): a spawned child's /proc cmdline equals the spawned argv
  EXACTLY — no interpreter wrapping.
- ``codex`` is a ``#!/usr/bin/env node`` script: spawning ``codex --help``
  shows a LIVE cmdline of ``['node', '/…/bin/codex', '--help']`` while the
  registry records ``codex --help`` — strict equality fails, so the §5.5a
  orphan sweep would misread every live codex orphan as pid reuse and leave
  its process group running unsupervised.

Consequence (applied under the wave-4 bug-fix exception, never silently):
``runner._cmdline_matches`` accepts the documented tolerant form — the
recorded argv as a SUFFIX of the live argv with the executable matched by
basename — and ``scheduler._proc_cmdline_matches`` delegates to it (one
predicate, Doctrine §9). These tests pin the live-stub exact match, the
wrapped-interpreter tolerance (reproduced hermetically with a shebang
script — no network/auth/quota dependency), the never-kill-strangers
refusals, and that recover()'s orphan sweep actually kills a wrapped child.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import subprocess
import sys
from pathlib import Path

from harness import (
    FactoryEnv,
    RecordingNotify,
    group_alive,
    kill_group_quiet,
    poll,
    poll_async,
)

from sf_factory import db as fdb
from sf_factory.models import ProcessRecord, utc_now
from sf_factory.runner import AgentRunner, _cmdline_matches
from sf_factory.scheduler import Scheduler, _proc_cmdline_matches
from sf_factory.statemachine import StateMachine


def _live_argv(pid: int) -> list[str]:
    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    return [p.decode() for p in raw.split(b"\0") if p]


def _running_row(env: FactoryEnv) -> dict | None:
    rows = [p for p in env.processes() if p["state"] == "running" and p["pid"]]
    return rows[0] if rows else None


async def test_stub_child_cmdline_matches_registry_exactly(
    make_env, monkeypatch
) -> None:
    """A real runner-spawned child (canonical stub, held alive) shows a /proc
    cmdline byte-identical to the recorded one: direct ``<python> <script>``
    argv is not interpreter-wrapped — the strict and tolerant forms agree."""
    env = make_env(stub="canonical")
    monkeypatch.setenv("SF_STUB_SCENARIO", "timeout")
    monkeypatch.setenv("SF_STUB_SLEEP_S", "600")
    runner = AgentRunner(env.cfg, env.db)

    task = asyncio.create_task(
        runner.run_agent(
            "builder_routine",
            "hold for the cmdline probe",
            unit_level="stage",
            unit_id="s1",
            cwd=env.workspace,
            timeout_s=120,
        )
    )
    try:
        row = await poll_async(lambda: _running_row(env), what="running registry row")
        env.track_pid(row["pid"])
        live = _live_argv(row["pid"])
        assert shlex.join(live) == row["cmdline"]  # exact — no wrapping observed
        assert _cmdline_matches(row["pid"], row["cmdline"]) is True
        # Never-kill-strangers: a divergent recorded argv refuses the match.
        assert _cmdline_matches(row["pid"], row["cmdline"] + " --extra") is False
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await poll_async(lambda: not group_alive(row["pid"]), what="stub group death")


async def test_wrapped_interpreter_child_matches_tolerantly(tmp_path: Path) -> None:
    """Hermetic reproduction of the codex case: a shebang script's live argv is
    ``[<interpreter>, <script>, …]`` while the registry would record the bare
    spawned argv — the tolerant suffix/basename form matches, strict does not,
    and foreign cmdlines still refuse."""
    fakecli = tmp_path / "fakecli"
    fakecli.write_text(
        f"#!{sys.executable}\nimport time\ntime.sleep(600)\n", encoding="utf-8"
    )
    fakecli.chmod(0o755)
    child = subprocess.Popen(
        [str(fakecli), "--model", "default", "do the thing"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        live = poll(lambda: _live_argv(child.pid), what="live cmdline")
        # Interpreter wrapping observed: argv[0] is the interpreter.
        assert Path(live[0]).name.startswith("python")
        assert live[1] == str(fakecli)

        # The runner records what it spawned — claude-style bare argv[0].
        recorded = shlex.join(["fakecli", "--model", "default", "do the thing"])
        assert shlex.join(live) != recorded  # strict equality fails (the bug)
        assert _cmdline_matches(child.pid, recorded) is True  # tolerant form
        # Absolutized recorded argv[0] (basename-aligned) also matches.
        assert _cmdline_matches(
            child.pid, shlex.join([str(fakecli), "--model", "default", "do the thing"])
        )
        # Never kill strangers: different executable or diverging args refuse.
        assert not _cmdline_matches(
            child.pid, shlex.join(["otherbin", "--model", "default", "do the thing"])
        )
        assert not _cmdline_matches(
            child.pid, shlex.join(["fakecli", "--model", "default", "другое"])
        )
        assert not _cmdline_matches(child.pid, "")
        # scheduler delegates to the same predicate (no drifting copy).
        assert _proc_cmdline_matches(child.pid, recorded) is True
    finally:
        kill_group_quiet(child.pid)
        child.wait()


def test_orphan_sweep_kills_wrapped_interpreter_child(
    make_env, tmp_path: Path
) -> None:
    """§5.5a end-to-end: a registry row whose recorded cmdline is the bare
    spawned argv, while the live child is interpreter-wrapped (the codex
    shape), is still identified as OURS by recover()'s orphan sweep — process
    group SIGKILLed, row finalized 'orphaned'. Under the strict matcher this
    child would be misread as pid reuse and survive."""
    env = make_env()
    fakecli = tmp_path / "fakecli"
    fakecli.write_text(
        f"#!{sys.executable}\nimport time\ntime.sleep(600)\n", encoding="utf-8"
    )
    fakecli.chmod(0o755)
    child = subprocess.Popen(
        [str(fakecli), "exec", "--json", "audit the diff"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    env.track_pid(child.pid)
    poll(lambda: _live_argv(child.pid), what="live child")
    with env.db.transaction() as conn:
        process_id = fdb.insert_process(
            conn,
            ProcessRecord(
                id=None,
                unit_level="stage",
                unit_id="s1",
                kind="agent",
                role="auditor_cross_model",
                cp_id=None,
                session_id=None,
                pid=None,
                cmdline=shlex.join(["fakecli", "exec", "--json", "audit the diff"]),
                cwd=str(tmp_path),
                state="spawned",
                exit_code=None,
                ndjson_log_path=None,
                spawned_at=utc_now(),
                heartbeat_at=None,
                ended_at=None,
            ),
        )
        fdb.mark_process_running(conn, process_id, pid=child.pid, at=utc_now())

    scheduler = Scheduler(env.db, StateMachine(env.db), env.cfg, {}, RecordingNotify())
    report = scheduler.recover()

    assert process_id in report.orphaned
    assert child.pid in report.killed_groups
    child.wait(timeout=30)  # reap (we are the parent; a zombie holds the group)
    poll(lambda: not group_alive(child.pid), what="wrapped child group death")
    (row,) = [p for p in env.processes() if p["id"] == process_id]
    assert row["state"] == "orphaned"
    (event,) = env.events("s1", "orphaned")
    assert json.loads(event["payload_json"])["group_killed"] is True
