"""Integration tests for the founder dashboard (dashboard design §8 integration
list): real Database + temp git repos + stub agent, server bound on
127.0.0.1:0 (tests call start() and read bound_address — deterministic
readiness, §6).

File name note: the design names this file tests/integration/test_dashboard.py,
but pytest's prepend import mode (frozen pyproject, packageless test tree)
refuses duplicate basenames against tests/unit/test_dashboard.py — renamed with
the _integration suffix; content implements the §8 integration list verbatim.

1. Pending critical_stage decision created through the REAL scheduler
   gate-entry path (driver-stubbed auditors entering AWAITING_HUMAN) — the
   founder-protocol assertions run against the REAL re-authored Romanian
   control-plane template.
2. POST answer -> factory-repo artifact committed + registered, row answered,
   event — then a scheduler tick (same instance) re-dispatches the unit
   (§12.A4 minus the phone).
3. Double-POST -> one answer row, explicit no-op page.
4. Health strip: fresh vs stale liveness; budget figures from the seeded
   token_ledger.
5. Decision Session round trip with the real stub agent: message -> poll busy
   -> agent reply -> confirm -> transcript committed + registered in the SAME
   commit/tx as the answer.
5a. Confirm-while-busy (D-0019 §3.1a, end-to-end): the answer POST lands while
   the real stub turn is composing -> the turn is cancelled-and-awaited (real
   runner kill path), the cancelled-turn notice is IN the committed transcript,
   every registered ref resolves and verify_integrity stays green.
6. Hung client never blocks a parallel GET; injected dashboard crash ->
   'alert' event + supervised restart serves again, scheduler loop ticking.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from harness import RecordingNotify, git, poll_async

from sf_factory import dashboard as dash
from sf_factory import db as fdb
from sf_factory.artifacts import register_artifact, verify_integrity
from sf_factory.models import (
    DecisionRequest,
    Level,
    SchedCategory,
    StageState,
    sched_category,
    utc_now,
)
from sf_factory.notify import dashboard_link
from sf_factory.runner import AgentRunner
from sf_factory.scheduler import Scheduler
from sf_factory.statemachine import StateMachine

_DASHBOARD_OVERRIDES: dict[str, Any] = {
    "founder_channel": {
        "dashboard": {
            "bind": "127.0.0.1",
            "port": 0,
            "refresh_s": 30,
            "answer_timeout_s": 10,
            "read_timeout_s": 5,
            "max_request_bytes": 65536,
            "restart_delay_s": 0.05,
            "page_every_n_restarts": 3,
            "bind_recheck_s": 60,
        },
        "decision_session": {
            "max_turns": 5,
            "turn_timeout_s": 30,
            "budget_tokens": 200000,
            "poll_s": 0.05,
        },
    },
    "models": {
        "decision_session": {
            "cli": "stub",
            "model": "stub-model",
            "mode": "print",
            "tools": "none",
        }
    },
}


def _git_init_home(home: Path) -> None:
    """The answer path commits into the FACTORY repo — factory.home must be one."""
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "factory@test"],
        ["git", "config", "user.name", "factory"],
    ):
        subprocess.run(args, cwd=home, check=True, capture_output=True)
    (home / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=home, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"], cwd=home, check=True, capture_output=True
    )


def _http(method: str, url: str, body: bytes | None = None):
    request = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


async def _serving(server: dash.DashboardServer) -> asyncio.Task:
    server.start()
    task = asyncio.create_task(server.serve())
    await poll_async(lambda: server._loop is not None, what="dashboard serve() loop")
    return task


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _dashboard_for(env) -> dash.DashboardServer:
    return dash.DashboardServer(
        env.cfg, env.db, AgentRunner(env.cfg, env.db), RecordingNotify()
    )


class _AnswerConsumingExecutor:
    """Stubbed §8 executor harness: walks AWAITING_HUMAN -> MERGE_GATE -> DONE
    once the founder's answer exists (re-dispatch proof, not conveyor logic)."""

    level = Level.STAGE

    def __init__(self, db) -> None:
        self._db = db
        self._sm = StateMachine(db)

    async def execute(self, unit_id: str) -> None:
        while True:
            stage = fdb.get_stage(self._db.read(), unit_id)
            assert stage is not None
            if stage.state is StageState.AWAITING_HUMAN:
                row = (
                    self._db.read()
                    .execute(
                        "SELECT * FROM decision_requests WHERE unit_id = ?"
                        " AND status='answered' ORDER BY id DESC LIMIT 1",
                        (unit_id,),
                    )
                    .fetchone()
                )
                if row is None:
                    return  # still blocked
                self._sm.transition(
                    Level.STAGE,
                    unit_id,
                    StageState.MERGE_GATE.value,
                    actor="founder",
                    reason="scripted consume",
                )
            elif stage.state is StageState.MERGE_GATE:
                self._sm.transition(
                    Level.STAGE,
                    unit_id,
                    StageState.DONE.value,
                    actor="control_plane",
                    reason="scripted merge",
                )
            else:
                return


async def test_critical_gate_card_answer_and_redispatch(make_env) -> None:
    """§8 integration 1 + 2 + 3: real gate-entry -> Romanian card -> POST answer
    -> committed artifact + answered row + event -> scheduler re-dispatch ->
    double-POST no-op."""
    env = make_env(use_config_db=True, config_overrides=_DASHBOARD_OVERRIDES)
    _git_init_home(env.home)
    env.seed_phase()
    stage_id = "ph1.critical"
    worktree = env.create_stage_worktree(stage_id)
    env.seed_stage(stage_id, StageState.AUDIT, risk="critical", worktree=worktree)
    env.write_playbook({"audit": {"default": {"findings": []}}})

    # The REAL gate-entry path: clean critical audit -> _enter_awaiting_human.
    await env.stage_executor().execute(stage_id)
    assert env.stage_state(stage_id) is StageState.AWAITING_HUMAN
    (pending,) = fdb.pending_decisions(env.db.read())
    assert pending.gate_kind == "critical_stage"
    request_id = pending.id

    server = _dashboard_for(env)
    task = await _serving(server)
    try:
        host, port = server.bound_address
        base = f"http://{host}:{port}"
        status, _, page = await asyncio.to_thread(_http, "GET", f"{base}/")
        assert status == 200

        # The card: anchor matches notify.dashboard_link's fragment format.
        link = dashboard_link(env.cfg, f"decision/{request_id}")
        fragment = link.split("#", 1)[1]
        assert f"id='{fragment}'" in page

        # Protocol conformance against the REAL re-authored template (§2a):
        assert f"Decizia #{request_id} — Etapa: Stage {stage_id} ({stage_id})" in page
        assert "etapă critică — aprobare necesară (critical_stage)" in page
        assert "Aprobi rezultatul etapei" in page  # full artifact text rendered
        assert "Answer with one of" not in page  # the English template is dead
        for token in ("approved", "rework:BUILD", "rework:SPEC"):
            assert f"value='{token}'" in page  # one button per declared option
        assert dash.RO["recommended_badge"] in page  # parsed Recomandare: approved
        assert "Recomandare: approved" in page  # marker visible in the body text

        # §8 integration 2 — the single write path over HTTP.
        status, _, _ = await asyncio.to_thread(
            _http, "POST", f"{base}/decision/{request_id}/answer", b"option=approved"
        )
        assert status == 200  # urllib followed the 303 back to /
        conn = env.db.read()
        row = conn.execute(
            "SELECT * FROM decision_requests WHERE id = ?", (request_id,)
        ).fetchone()
        assert row["status"] == "answered" and row["answer"] == "approved"
        ref = conn.execute(
            "SELECT * FROM artifact_refs WHERE id = ?", (row["answer_artifact_id"],)
        ).fetchone()
        assert ref["repo"] == "factory" and ref["kind"] == "decision_answer"
        shown = git("show", f"{ref['git_commit']}:{ref['path']}", cwd=env.home)
        assert "answer: approved" in shown  # sha resolvable, content committed
        events = env.events(stage_id, "decision_answered")
        assert len(events) == 1
        assert json.loads(events[0]["payload_json"])["via"] == "dashboard"

        # §12.A4 mechanics minus the phone: the next tick re-dispatches the
        # BLOCKED unit (pending-decision count changed) on the SAME scheduler.
        scheduler = Scheduler(
            env.db,
            StateMachine(env.db),
            env.cfg,
            {Level.STAGE: _AnswerConsumingExecutor(env.db)},
            RecordingNotify(),
        )
        await asyncio.wait_for(scheduler.run_until_blocked(), timeout=30)
        assert env.stage_state(stage_id) is StageState.DONE

        # §8 integration 3 — double-POST: explicit no-op page, ONE answer row.
        status, _, body = await asyncio.to_thread(
            _http, "POST", f"{base}/decision/{request_id}/answer", b"option=approved"
        )
        assert status == 200 and dash.RO["answered_already"] in body
        count = (
            env.db.read()
            .execute(
                "SELECT COUNT(*) FROM events WHERE event_type='decision_answered'"
            )
            .fetchone()[0]
        )
        assert count == 1
    finally:
        await _stop(task)


def _seed_pending_decision(env, stage_id: str) -> int:
    """File-backed pending decision in the factory repo (non-gate-entry tests)."""
    unit_dir = env.home / "_factory" / "stages" / stage_id
    unit_dir.mkdir(parents=True, exist_ok=True)
    path = unit_dir / "decision-request.md"
    path.write_text(
        "# Cerere de decizie\n\nÎntrebare de integrare.\n\nRecomandare: approved\n",
        encoding="utf-8",
    )
    with env.db.transaction() as conn:
        ref = register_artifact(
            conn,
            unit_level="stage",
            unit_id=stage_id,
            kind="decision_request",
            repo="factory",
            repo_root=env.home,
            path=path,
            git_commit=None,
        )
        return fdb.insert_decision_request(
            conn,
            DecisionRequest(
                id=None,
                unit_level="stage",
                unit_id=stage_id,
                gate_kind="critical_stage",
                request_artifact_id=ref.id,
                status="pending",
                answer=None,
                answer_artifact_id=None,
                created_at=utc_now(),
                alerted_at=None,
                answered_at=None,
            ),
        )


async def test_health_strip_liveness_and_budget(make_env) -> None:
    """§8 integration 4: fresh liveness -> no red marker; stale -> red marker;
    budget figures match the seeded token_ledger."""
    env = make_env(use_config_db=True, config_overrides=_DASHBOARD_OVERRIDES)
    _git_init_home(env.home)
    env.seed_phase()
    env.seed_stage("ph1.s1", StageState.BUILD, risk="routine")
    from sf_factory.models import ProcessRecord

    with env.db.transaction() as conn:
        pid = fdb.insert_process(
            conn,
            ProcessRecord(
                id=None,
                unit_level="stage",
                unit_id="ph1.s1",
                kind="agent",
                role="builder_routine",
                cp_id=None,
                session_id=None,
                pid=None,
                cmdline="stub",
                cwd=None,
                state="exited",
                exit_code=0,
                ndjson_log_path=None,
                spawned_at=utc_now(),
                heartbeat_at=None,
                ended_at=utc_now(),
            ),
        )
        fdb.insert_token_usage(
            conn,
            process_id=pid,
            unit_level="stage",
            unit_id="ph1.s1",
            role="builder_routine",
            model="stub-model",
            tokens_in=4000,
            tokens_out=1000,
            cost_usd=None,
        )
    liveness = Path(env.cfg.process.liveness_file)
    liveness.parent.mkdir(parents=True, exist_ok=True)
    liveness.write_text("tick\n", encoding="utf-8")

    server = _dashboard_for(env)
    task = await _serving(server)
    try:
        host, port = server.bound_address
        status, _, page = await asyncio.to_thread(_http, "GET", f"http://{host}:{port}/")
        assert status == 200
        assert dash.RO["pulse_stale"] not in page  # fresh liveness
        # 5.000 / 10.000 tokeni · 50% — seeded ledger vs conftest routine budget.
        assert "5.000 / 10.000 tokeni · 50%" in page

        import os

        old = os.stat(liveness).st_mtime - 10_000
        os.utime(liveness, (old, old))
        status, _, page = await asyncio.to_thread(_http, "GET", f"http://{host}:{port}/")
        assert dash.RO["pulse_stale"] in page  # stale -> red marker
    finally:
        await _stop(task)


async def test_decision_session_round_trip_with_stub_agent(make_env) -> None:
    """§8 integration 5: POST message -> poll busy -> the stub agent's reply ->
    confirm an option -> transcript committed + registered (kind='transcript')
    in the SAME commit/tx as the answer."""
    env = make_env(
        stub="canonical", use_config_db=True, config_overrides=_DASHBOARD_OVERRIDES
    )
    _git_init_home(env.home)
    env.seed_phase()
    env.seed_stage("ph1.sess", StageState.AWAITING_HUMAN, risk="critical")
    request_id = _seed_pending_decision(env, "ph1.sess")

    server = _dashboard_for(env)
    task = await _serving(server)
    try:
        host, port = server.bound_address
        base = f"http://{host}:{port}"
        status, _, body = await asyncio.to_thread(
            _http,
            "POST",
            f"{base}/decision/{request_id}/session/message",
            "text=Ce risc are opțiunea approved?".encode(),
        )
        assert status == 200  # 303 followed to the session page
        assert dash.RO["session_title"] in body

        async def agent_replied():
            _, _, poll_body = await asyncio.to_thread(
                _http, "GET", f"{base}/decision/{request_id}/session/poll?after=0"
            )
            snap = json.loads(poll_body)
            done = not snap["busy"] and len(snap["turns"]) >= 2
            return snap if done else None

        deadline = asyncio.get_running_loop().time() + 60
        snap = None
        while asyncio.get_running_loop().time() < deadline:
            snap = await agent_replied()
            if snap:
                break
            await asyncio.sleep(0.1)
        assert snap, "agent turn never completed"
        authors = [t["author"] for t in snap["turns"]]
        assert authors == ["founder", "agent"]
        assert snap["turns"][1]["text"] == "stub success"  # the real stub's reply

        # Session usage landed in the ledger under the decision's unit (§2b burn).
        burn = fdb.unit_token_total(env.db.read(), "stage", "ph1.sess")
        assert burn > 0

        # Confirm an option — same single write path (§3).
        status, _, _ = await asyncio.to_thread(
            _http, "POST", f"{base}/decision/{request_id}/answer", b"option=approved"
        )
        assert status == 200
        conn = env.db.read()
        transcript_ref = conn.execute(
            "SELECT * FROM artifact_refs WHERE kind='transcript'"
        ).fetchone()
        answer_ref = conn.execute(
            "SELECT * FROM artifact_refs WHERE kind='decision_answer'"
        ).fetchone()
        assert transcript_ref is not None and answer_ref is not None
        assert transcript_ref["git_commit"] == answer_ref["git_commit"]  # same commit
        committed = git(
            "show", f"{transcript_ref['git_commit']}:{transcript_ref['path']}", cwd=env.home
        )
        assert "Ce risc are opțiunea approved?" in committed
        assert "stub success" in committed
        event = conn.execute(
            "SELECT payload_json FROM events WHERE event_type='decision_answered'"
        ).fetchone()
        assert (
            json.loads(event["payload_json"])["transcript_artifact_id"]
            == transcript_ref["id"]
        )
    finally:
        await _stop(task)


async def test_confirm_while_busy_turn_quiesces_end_to_end(make_env, monkeypatch) -> None:
    """§8 integration 5a (D-0019 §3.1a, end-to-end): the founder taps an option
    WHILE the real stub agent is composing — answer() cancels and awaits the
    in-flight turn (the runner's BaseException path kills the child), the
    cancelled-turn notice lands in the transcript BEFORE the commit, the
    registered transcript resolves at its recorded commit and verify_integrity
    stays green (the reproduced incident aborted the next orchestrator start)."""
    env = make_env(
        stub="canonical", use_config_db=True, config_overrides=_DASHBOARD_OVERRIDES
    )
    _git_init_home(env.home)
    env.seed_phase()
    env.seed_stage("ph1.busy", StageState.AWAITING_HUMAN, risk="critical")
    request_id = _seed_pending_decision(env, "ph1.busy")
    # The seeded request artifact rides a commit too: verify_integrity checks
    # EVERY latest ref of the non-terminal unit.
    subprocess.run(["git", "add", "-A"], cwd=env.home, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed request"],
        cwd=env.home,
        check=True,
        capture_output=True,
    )
    # The stub composes "forever" (timeout scenario): the turn is
    # deterministically in flight until the answer quiesces it.
    monkeypatch.setenv("SF_STUB_SCENARIO", "timeout")
    monkeypatch.setenv("SF_STUB_SLEEP_S", "3600")

    server = _dashboard_for(env)
    task = await _serving(server)
    try:
        host, port = server.bound_address
        base = f"http://{host}:{port}"
        status, _, _ = await asyncio.to_thread(
            _http,
            "POST",
            f"{base}/decision/{request_id}/session/message",
            "text=Întrebare în zbor".encode(),
        )
        assert status == 200

        def turn_running():
            return (
                env.db.read()
                .execute(
                    "SELECT id FROM process_registry WHERE role='decision_session'"
                    " AND state='running'"
                )
                .fetchone()
            )

        await poll_async(turn_running, what="in-flight decision_session turn")

        # The founder-normal action that reproduced the incident: confirm now.
        status, _, _ = await asyncio.to_thread(
            _http, "POST", f"{base}/decision/{request_id}/answer", b"option=approved"
        )
        assert status == 200
        conn = env.db.read()
        row = conn.execute(
            "SELECT * FROM decision_requests WHERE id = ?", (request_id,)
        ).fetchone()
        assert row["status"] == "answered" and row["answer"] == "approved"
        tref = conn.execute(
            "SELECT * FROM artifact_refs WHERE kind='transcript'"
        ).fetchone()
        assert tref is not None
        blob = subprocess.run(
            ["git", "cat-file", "blob", f"{tref['git_commit']}:{tref['path']}"],
            cwd=env.home,
            check=True,
            capture_output=True,
        ).stdout
        assert hashlib.sha256(blob).hexdigest() == tref["sha256"]  # resolves AT its commit
        committed = blob.decode("utf-8")
        assert "Întrebare în zbor" in committed
        assert dash.RO["session_turn_failed"] in committed  # cancelled-turn notice
        # The cancelled turn's child did not survive its supervision (§5.1).
        assert turn_running() is None
        report = verify_integrity(env.db, {"factory": env.home})
        assert report.ok and report.failures == ()
    finally:
        await _stop(task)


class _FlakyOnceDashboard(dash.DashboardServer):
    """serve() crashes exactly once, then behaves — the §8 integration-6 crash."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.crashed = False

    async def serve(self) -> None:
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("injected dashboard crash")
        await super().serve()


async def test_hung_client_and_crash_restart_keep_factory_alive(make_env) -> None:
    """§8 integration 6: a hung client occupies one daemon thread while a
    parallel GET succeeds; an injected dashboard-task crash -> 'alert' event +
    supervised restart serves the page again, scheduler loop still ticking."""
    env = make_env(use_config_db=True, config_overrides=_DASHBOARD_OVERRIDES)
    _git_init_home(env.home)
    env.seed_phase()
    env.seed_stage("ph1.s1", StageState.BUILD, risk="routine")

    flaky = _FlakyOnceDashboard(
        env.cfg, env.db, AgentRunner(env.cfg, env.db), RecordingNotify()
    )
    scheduler = Scheduler(
        env.db, StateMachine(env.db), env.cfg, {}, RecordingNotify(), dashboard=flaky
    )
    run_task = asyncio.create_task(scheduler.run_forever())
    try:
        # Crash contained -> alert event with the restart counter -> restarted
        # serve() binds and answers requests.
        await poll_async(
            lambda: flaky.bound_address is not None, what="dashboard restart bind"
        )
        crash_events = env.events(None, "alert")
        crash_payloads = [
            json.loads(e["payload_json"])
            for e in crash_events
            if json.loads(e["payload_json"]).get("kind") == "dashboard_crashed"
        ]
        assert crash_payloads and crash_payloads[0]["restarts"] == 1

        host, port = flaky.bound_address
        base = f"http://{host}:{port}"

        # Hung client: open a socket, send NOTHING — one daemon thread blocks,
        # the parallel GET still answers (thread-per-connection, §1).
        hung = socket.create_connection((host, port), timeout=10)
        try:
            status, _, page = await asyncio.to_thread(_http, "GET", f"{base}/")
            assert status == 200
            assert dash.RO["page_heading"] in page
        finally:
            hung.close()

        # The scheduler loop never stopped ticking: liveness stays fresh.
        liveness = Path(env.cfg.process.liveness_file)
        await poll_async(liveness.is_file, what="liveness file")
        before = liveness.read_text(encoding="utf-8")
        await poll_async(
            lambda: liveness.read_text(encoding="utf-8") != before,
            what="liveness tick after dashboard crash",
        )
        assert not run_task.done()
    finally:
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task


def test_scan_vocabulary_pin() -> None:
    """The §2b queue view categorizes through the SAME sched_category the
    scheduler uses (one source) — pinned cheaply here."""
    assert sched_category(Level.STAGE, "AWAITING_HUMAN", True) is SchedCategory.BLOCKED
    assert sched_category(Level.STAGE, "BUILD", True) is SchedCategory.RUNNING
