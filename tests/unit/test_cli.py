"""Unit tests for cli.py (design §8): init creates db + migrates with env sanity
checks; run/resume hold the single-instance flock — SECOND-INSTANCE REFUSAL is
the §8-mandated case — and wire the frozen §4 object graph; status renders the
generated view from a mode=ro connection without ever opening a write
transaction; decide answers a pending decision through answer_decision +
artifact registration + event, atomically, with the answer artifact committed
in the factory repo first (D-0015).

tests/conftest.py is frozen (design §9): all extra fixtures live here. The
run/resume tests inject fake ``sf_factory.scheduler`` / ``sf_factory.consultation``
modules into ``sys.modules`` (wave-3 lanes are file-disjoint; the fakes also
keep these tests hermetic once the real modules land) — cli must construct them
with the exact frozen constructor signatures.

CCR-4 (dashboard design §1/§9): ``cli run`` wires the REAL ``DashboardServer``
and ``start()``s it eagerly before recovery; the test config overrides
``founder_channel.dashboard`` to ``127.0.0.1`` + port ``0`` so unit tests bind
loopback/ephemeral only — NEVER a real tailnet socket. ``resume`` stays
``dashboard=None``.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from sf_factory import watchdog
from sf_factory.artifacts import sha256_file
from sf_factory.cli import main
from sf_factory.config import FactoryConfig
from sf_factory.dashboard import DashboardServer
from sf_factory.db import (
    Database,
    insert_artifact_ref,
    insert_decision_request,
    insert_escalation,
    insert_event,
    insert_phase,
    insert_process,
    insert_stage,
    insert_token_usage,
)
from sf_factory.models import (
    ArtifactRef,
    DecisionRequest,
    Escalation,
    FactoryError,
    Level,
    Phase,
    PhaseState,
    ProcessRecord,
    Stage,
    StageState,
    utc_now,
)
from sf_factory.notify import NtfyPublisher
from sf_factory.statemachine import StateMachine

# --------------------------------------------------------------- local fixtures


@pytest.fixture()
def cli_env(tmp_path: Path, config_dict: dict[str, Any]) -> SimpleNamespace:
    """Config file on disk + factory-home scaffolding (canon files exist so the
    init env sanity check passes; conftest routes are all stub already)."""
    home = Path(config_dict["factory"]["home"])
    for rel in config_dict["canon"]["files"].values():
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("canon body\n", encoding="utf-8")
    # CCR-4 (dashboard design §9): unit tests must NEVER bind a real tailnet
    # socket — `cli run`'s eager DashboardServer.start() binds loopback on an
    # ephemeral port instead (conftest's `bind: tailscale` is production truth).
    config_dict["founder_channel"]["dashboard"]["bind"] = "127.0.0.1"
    config_dict["founder_channel"]["dashboard"]["port"] = 0
    config_path = tmp_path / "factory.config.yaml"
    config_path.write_text(yaml.safe_dump(config_dict), encoding="utf-8")
    return SimpleNamespace(
        home=home,
        config_path=config_path,
        config=config_dict,
        db_path=Path(config_dict["process"]["db_path"]),
        pid_file=Path(config_dict["process"]["pid_file"]),
        liveness_file=Path(config_dict["process"]["liveness_file"]),
    )


def _cli(env: SimpleNamespace, *argv: str) -> int:
    return main(["--config", str(env.config_path), *argv])


def _install_fake_graph(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake sf_factory.scheduler / sf_factory.consultation with the FROZEN §4
    constructor signatures; records wiring + calls. sys.modules injection wins
    over the real modules for the duration of the test."""
    record: dict[str, Any] = {}

    class FakeScheduler:
        def __init__(
            self,
            db: Any,
            sm: Any,
            cfg: Any,
            executors: Any,
            notify: Any,
            dashboard: Any = None,
        ) -> None:
            record["scheduler_args"] = (db, sm, cfg, executors, notify)
            # CCR-3 kwarg: the (real) DashboardServer cli run wires, None on resume.
            record["scheduler_dashboard"] = dashboard
            self._dashboard = dashboard

        def recover(self) -> SimpleNamespace:
            record["recovered"] = True
            # Pins the §1 eager-start order: the dashboard is already BOUND
            # when the recovery scan begins (None = no dashboard wired).
            record["dashboard_bound_at_recover"] = (
                None if self._dashboard is None else self._dashboard.bound_address
            )
            return SimpleNamespace()

        async def run_forever(self) -> None:
            record["ran"] = "forever"

        async def run_until_blocked(self) -> None:
            record["ran"] = "until_blocked"

    class FakeStageExecutor:
        def __init__(self, *args: Any) -> None:
            record["stage_executor_args"] = args

    class FakePhaseExecutor:
        def __init__(self, *args: Any) -> None:
            record["phase_executor_args"] = args

    class FakeConsultor:
        def __init__(self, *args: Any) -> None:
            record["consultor_args"] = args

    sched_mod = types.ModuleType("sf_factory.scheduler")
    sched_mod.Scheduler = FakeScheduler  # type: ignore[attr-defined]
    sched_mod.StageExecutor = FakeStageExecutor  # type: ignore[attr-defined]
    sched_mod.PhaseExecutor = FakePhaseExecutor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sf_factory.scheduler", sched_mod)

    cons_mod = types.ModuleType("sf_factory.consultation")
    cons_mod.Consultor = FakeConsultor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sf_factory.consultation", cons_mod)
    return record


def _open_factory_db(env: SimpleNamespace) -> Database:
    db = Database(env.db_path, busy_timeout_ms=5000)
    db.open()
    return db


def _seed_units(conn: Any, *, stage_state: StageState = StageState.BUILD) -> tuple[str, str]:
    """One phase + one stage; returns (phase_id, stage_id)."""
    now = utc_now()
    insert_phase(
        conn,
        Phase(
            id="ph-demo",
            project="proj",
            name="Demo phase",
            state=PhaseState.RUNNING,
            branch="phase/ph-demo",
            plan_artifact_id=None,
            created_at=now,
            updated_at=now,
        ),
    )
    insert_stage(
        conn,
        Stage(
            id="st-demo",
            phase_id="ph-demo",
            name="Demo stage",
            risk_class="critical",
            state=stage_state,
            branch="stage/st-demo",
            worktree_path=None,
            spec_artifact_id=None,
            created_at=now,
            updated_at=now,
        ),
    )
    return "ph-demo", "st-demo"


def _seed_decision(env: SimpleNamespace, *, stage_state: StageState = StageState.BUILD) -> int:
    """Phase + stage + a pending decision request (FK-complete); returns its id."""
    db = _open_factory_db(env)
    try:
        with db.transaction() as conn:
            _, stage_id = _seed_units(conn, stage_state=stage_state)
            now = utc_now()
            ref_id = insert_artifact_ref(
                conn,
                ArtifactRef(
                    id=None,
                    unit_level="stage",
                    unit_id=stage_id,
                    kind="decision_request",
                    repo="factory",
                    path=f"_factory/stages/{stage_id}/decision-request.md",
                    sha256="0" * 64,
                    git_commit=None,
                    created_at=now,
                ),
            )
            request_id = insert_decision_request(
                conn,
                DecisionRequest(
                    id=None,
                    unit_level="stage",
                    unit_id=stage_id,
                    gate_kind="critical_stage",
                    request_artifact_id=ref_id,
                    status="pending",
                    answer=None,
                    answer_artifact_id=None,
                    created_at=now,
                    alerted_at=None,
                    answered_at=None,
                ),
            )
    finally:
        db.close()
    return request_id


def _git_init_home(home: Path) -> None:
    """factory.home as a git repo — production truth (factory.config.yaml
    points home at the SF-F5 repo): `decide` commits its answer artifact there
    (D-0015), so the decide tests need a real repo with an initial commit."""
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "factory@test"],
        ["git", "config", "user.name", "factory"],
    ):
        subprocess.run(args, cwd=home, check=True, capture_output=True)
    (home / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "--", "seed.txt"], cwd=home, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"], cwd=home, check=True, capture_output=True
    )


# ------------------------------------------------------------------------- init


def test_init_creates_db_migrates_and_is_idempotent(cli_env: SimpleNamespace) -> None:
    assert _cli(cli_env, "init") == 0
    assert cli_env.db_path.is_file()
    # Operational dirs created (log dir / pid dir / liveness dir).
    assert Path(cli_env.config["process"]["ndjson_log_dir"]).is_dir()
    assert cli_env.pid_file.parent.is_dir()
    db = _open_factory_db(cli_env)
    try:
        versions = [
            row[0]
            for row in db.read().execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
    finally:
        db.close()
    assert versions and versions[0] == 1
    # Second init: nothing pending, still success.
    assert _cli(cli_env, "init") == 0


def test_init_fails_on_missing_canon_file(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    doctrine = cli_env.home / cli_env.config["canon"]["files"]["doctrine"]
    doctrine.unlink()
    assert _cli(cli_env, "init") == 1
    assert "canon.files.doctrine" in capsys.readouterr().err
    assert not cli_env.db_path.exists()  # sanity check failed BEFORE creating the db


def test_init_fails_on_missing_stub_agent(
    tmp_path: Path,
    cli_env: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_env.config["process"]["stub_agent_path"] = str(tmp_path / "no-such-stub.py")
    cli_env.config_path.write_text(yaml.safe_dump(cli_env.config), encoding="utf-8")
    assert _cli(cli_env, "init") == 1
    assert "stub_agent_path" in capsys.readouterr().err


def test_init_fails_on_invalid_config(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = dict(cli_env.config)
    del broken["budgets"]
    cli_env.config_path.write_text(yaml.safe_dump(broken), encoding="utf-8")
    assert _cli(cli_env, "init") == 1
    assert "sf-factory:" in capsys.readouterr().err


def test_unreadable_config_path_fails_explicitly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config", str(tmp_path / "absent.yaml"), "init"]) == 1
    assert "cannot read config" in capsys.readouterr().err


# ------------------------------------------------------------------ run / resume


def test_run_refuses_second_instance_flock(
    cli_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """THE §8 cli case: a held flock on process.pid_file refuses a second run."""
    assert _cli(cli_env, "init") == 0
    record = _install_fake_graph(monkeypatch)
    original = b"99999\nsome-live-orchestrator --flag\n"
    cli_env.pid_file.write_bytes(original)
    holder_fd = os.open(cli_env.pid_file, os.O_RDWR)
    try:
        fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the "first instance"
        rc = _cli(cli_env, "run")
    finally:
        content_after = cli_env.pid_file.read_bytes()
        os.close(holder_fd)
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to start a second instance" in err
    assert "99999" in err  # the recorded holder pid is named in the message
    assert "recovered" not in record  # refused BEFORE recovery / scheduler wiring
    assert "scheduler_args" not in record
    assert content_after == original  # a refused instance never clobbers the pidfile


def test_run_refuses_when_recorded_pid_alive(
    cli_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Flock acquirable (lost/replaced pidfile scenario) but the recorded
    pid+cmdline is alive -> refuse (design §4 belt-and-braces)."""
    assert _cli(cli_env, "init") == 0
    record = _install_fake_graph(monkeypatch)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        live_cmdline = (
            Path(f"/proc/{child.pid}/cmdline")
            .read_bytes()
            .replace(b"\0", b" ")
            .decode("utf-8")
            .strip()
        )
        cli_env.pid_file.write_text(f"{child.pid}\n{live_cmdline}\n", encoding="utf-8")
        rc = _cli(cli_env, "run")
        assert rc == 1
        assert "alive" in capsys.readouterr().err
        assert "recovered" not in record
        # Refusal preserved the live instance's pidfile content.
        assert cli_env.pid_file.read_text(encoding="utf-8").splitlines()[0] == str(child.pid)
    finally:
        child.kill()
        child.wait()


def test_run_wires_frozen_graph_recovers_and_runs_forever(
    cli_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _cli(cli_env, "init") == 0
    record = _install_fake_graph(monkeypatch)
    assert _cli(cli_env, "run") == 0
    assert record["recovered"] is True
    assert record["ran"] == "forever"

    # Frozen §4 constructor wiring (positional): Scheduler(db, sm, cfg, executors,
    # notify) + the CCR-3 dashboard kwarg (asserted separately below).
    db, sm, cfg, executors, notify = record["scheduler_args"]
    assert isinstance(db, Database)
    assert isinstance(sm, StateMachine)
    assert isinstance(cfg, FactoryConfig)
    assert isinstance(notify, NtfyPublisher)
    assert set(executors) == {Level.STAGE, Level.PHASE}
    # StageExecutor(db, sm, cfg, runner, wt, thresholds, consultor, notify) = 8 args;
    # PhaseExecutor(db, sm, cfg, runner, wt, notify) = 6; Consultor(cfg, db, runner) = 3.
    assert len(record["stage_executor_args"]) == 8
    assert len(record["phase_executor_args"]) == 6
    assert len(record["consultor_args"]) == 3
    assert record["consultor_args"][0] is cfg
    assert record["consultor_args"][1] is db

    # CCR-4 production wiring (dashboard design §1/§6): run constructs the REAL
    # DashboardServer, hands the same instance to Scheduler(dashboard=...), and
    # start()s it EAGERLY — already bound when recover() began, on the loopback/
    # ephemeral test bind (never a tailnet socket; port 0 was requested).
    dashboard = record["scheduler_dashboard"]
    assert isinstance(dashboard, DashboardServer)
    assert record["dashboard_bound_at_recover"] is not None
    host, port = record["dashboard_bound_at_recover"]
    assert host == "127.0.0.1"
    assert port > 0

    # Pidfile per the watchdog content contract: pid line + normalized cmdline line.
    pid_line, cmdline_line = cli_env.pid_file.read_text(encoding="utf-8").splitlines()[:2]
    assert int(pid_line) == os.getpid()
    expected = (
        Path("/proc/self/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8").strip()
    )
    assert cmdline_line == expected

    # The flock is held for the process lifetime ONLY: released after main returned.
    probe_fd = os.open(cli_env.pid_file, os.O_RDWR)
    try:
        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
    finally:
        os.close(probe_fd)


def test_resume_runs_until_blocked(
    cli_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _cli(cli_env, "init") == 0
    record = _install_fake_graph(monkeypatch)
    assert _cli(cli_env, "resume") == 0
    assert record["recovered"] is True
    assert record["ran"] == "until_blocked"
    # CCR-4: resume keeps dashboard=None — no DashboardServer, no bind at all.
    assert record["scheduler_dashboard"] is None
    assert record["dashboard_bound_at_recover"] is None


def test_run_aborts_in_foreground_when_dashboard_bind_fails(
    cli_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dashboard design §1: `run` start()s the dashboard EAGERLY — a first
    resolve/bind FactoryError aborts orchestrator start in the foreground
    (clear message, rc 1) BEFORE the recovery scan, never inside the §7
    supervised restart loop; the instance lock is released on the way out."""
    assert _cli(cli_env, "init") == 0
    record = _install_fake_graph(monkeypatch)

    def boom(self: DashboardServer) -> None:
        raise FactoryError("dashboard bind failed on 127.0.0.1:0: (test) — start aborts")

    monkeypatch.setattr(DashboardServer, "start", boom)
    assert _cli(cli_env, "run") == 1
    err = capsys.readouterr().err
    assert "sf-factory: dashboard bind failed" in err
    assert "recovered" not in record  # aborted BEFORE recovery / the loop
    assert record.get("ran") is None
    # The flock is not left held by the aborted instance.
    probe_fd = os.open(cli_env.pid_file, os.O_RDWR)
    try:
        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
    finally:
        os.close(probe_fd)


def test_run_without_init_fails_after_lock(
    cli_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record = _install_fake_graph(monkeypatch)
    assert _cli(cli_env, "run") == 1
    assert "sf-factory init" in capsys.readouterr().err
    assert "recovered" not in record


def test_pidfile_written_by_run_satisfies_watchdog_reader(
    cli_env: SimpleNamespace, factory_config: FactoryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-module contract: the pidfile cli writes is accepted by the public
    watchdog check (beyond its startup grace) while this process is alive."""
    assert _cli(cli_env, "init") == 0
    _install_fake_graph(monkeypatch)
    assert _cli(cli_env, "run") == 0
    # Age the pidfile past the staleness grace so the real pid+cmdline checks run.
    threshold = cli_env.config["founder_channel"]["watchdog"]["staleness_threshold_s"]
    old = time.time() - threshold - 60
    os.utime(cli_env.pid_file, (old, old))
    cli_env.liveness_file.parent.mkdir(parents=True, exist_ok=True)
    cli_env.liveness_file.write_text("", encoding="utf-8")  # fresh liveness tick
    assert watchdog.check_once(factory_config) is True


# ------------------------------------------------------------------------ status


def _seed_status_fixture(env: SimpleNamespace) -> None:
    db = _open_factory_db(env)
    try:
        with db.transaction() as conn:
            phase_id, stage_id = _seed_units(conn)
            now = utc_now()
            ref_id = insert_artifact_ref(
                conn,
                ArtifactRef(
                    id=None,
                    unit_level="stage",
                    unit_id=stage_id,
                    kind="decision_request",
                    repo="factory",
                    path=f"_factory/stages/{stage_id}/decision-request.md",
                    sha256="1" * 64,
                    git_commit=None,
                    created_at=now,
                ),
            )
            insert_decision_request(
                conn,
                DecisionRequest(
                    id=None,
                    unit_level="stage",
                    unit_id=stage_id,
                    gate_kind="critical_stage",
                    request_artifact_id=ref_id,
                    status="pending",
                    answer=None,
                    answer_artifact_id=None,
                    created_at=now,
                    alerted_at=None,
                    answered_at=None,
                ),
            )
            insert_escalation(
                conn,
                Escalation(
                    id=None,
                    unit_level="stage",
                    unit_id=stage_id,
                    trigger="max_fix_iterations",
                    target="phase_architect",
                    payload_artifact_id=None,
                    event_seq=None,
                    status="open",
                    resolution=None,
                    created_at=now,
                    resolved_at=None,
                ),
            )
            process_id = insert_process(
                conn,
                ProcessRecord(
                    id=None,
                    unit_level="stage",
                    unit_id=stage_id,
                    kind="agent",
                    role="builder_routine",
                    cp_id=None,
                    session_id=None,
                    pid=424242,
                    cmdline="stub-agent",
                    cwd=None,
                    state="running",
                    exit_code=None,
                    ndjson_log_path=None,
                    spawned_at=now,
                    heartbeat_at=now,
                    ended_at=None,
                ),
            )
            insert_token_usage(
                conn,
                process_id=process_id,
                unit_level="stage",
                unit_id=stage_id,
                role="builder_routine",
                model="stub-model",
                tokens_in=100,
                tokens_out=50,
                cost_usd=None,
            )
            insert_event(
                conn,
                unit_level="stage",
                unit_id=stage_id,
                event_type="spawn",
                actor="control_plane",
            )
    finally:
        db.close()


def test_status_json_view_from_read_only_connection(
    cli_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _cli(cli_env, "init") == 0
    _seed_status_fixture(cli_env)
    # Make a project workspace a real git repo: the git section's happy path.
    workspace = Path(cli_env.config["projects"]["proj"]["workspace"])
    workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "seed",
        ],
        check=True,
    )

    open_calls: list[bool] = []
    real_open = Database.open

    def spy_open(self: Database, *, read_only: bool = False) -> None:
        open_calls.append(read_only)
        real_open(self, read_only=read_only)

    monkeypatch.setattr(Database, "open", spy_open)

    def no_write_tx(self: Database) -> None:
        raise AssertionError("status must never open a write transaction")

    monkeypatch.setattr(Database, "transaction", no_write_tx)

    capsys.readouterr()  # flush init/seed output
    assert _cli(cli_env, "status", "--json") == 0
    assert open_calls == [True]  # exactly one connection, mode=ro (§2 sanctioned read)

    view = json.loads(capsys.readouterr().out)
    assert view["canonical"] is False
    assert view["orchestrator"]["alive"] is False
    assert view["orchestrator"]["liveness_stale"] is True
    phases = {p["id"]: p for p in view["phases"]}
    assert phases["ph-demo"]["state"] == "RUNNING"
    stages = {s["id"]: s for s in phases["ph-demo"]["stages"]}
    assert stages["st-demo"]["state"] == "BUILD"
    assert stages["st-demo"]["tokens"] == 150
    assert [d["unit_id"] for d in view["decisions_pending"]] == ["st-demo"]
    assert [e["trigger"] for e in view["escalations_open"]] == ["max_fix_iterations"]
    assert [p["pid"] for p in view["processes_live"]] == [424242]
    assert any(e["event_type"] == "spawn" for e in view["events_recent"])
    assert view["git"]["project:proj"]["available"] is True
    assert view["git"]["project:proj"]["branch"] == "main"
    assert "available" in view["git"]["factory"]


def test_status_text_and_write_status_md(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(cli_env, "init") == 0
    _seed_status_fixture(cli_env)
    capsys.readouterr()
    assert _cli(cli_env, "status", "--write") == 0
    out = capsys.readouterr().out
    assert "st-demo" in out
    assert "Decisions awaited (1)" in out
    assert "non-canonical" in out
    status_md = cli_env.home / "STATUS.md"
    assert status_md.is_file()
    text = status_md.read_text(encoding="utf-8")
    assert "GENERATED VIEW" in text  # Doctrine §9 marker in the written view
    assert "max_fix_iterations" in text


def test_status_without_db_fails_explicitly(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(cli_env, "status") == 1
    assert "sf-factory init" in capsys.readouterr().err


# ------------------------------------------------------------------------ decide


def test_decide_answers_pending_decision_atomically(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(cli_env, "init") == 0
    _git_init_home(cli_env.home)
    request_id = _seed_decision(cli_env, stage_state=StageState.AWAITING_HUMAN)
    assert _cli(cli_env, "decide", str(request_id), "approve") == 0
    assert "answered decision" in capsys.readouterr().out

    artifact_path = cli_env.home / "_factory" / "stages" / "st-demo" / (
        f"decision-answer-{request_id}.md"
    )
    assert artifact_path.is_file()
    body = artifact_path.read_text(encoding="utf-8")
    assert "answer: approve" in body
    assert "emergency fallback" in body

    db = _open_factory_db(cli_env)
    try:
        conn = db.read()
        row = conn.execute(
            "SELECT * FROM decision_requests WHERE id = ?", (request_id,)
        ).fetchone()
        assert row["status"] == "answered"
        assert row["answer"] == "approve"
        assert row["answered_at"] is not None
        ref = conn.execute(
            "SELECT * FROM artifact_refs WHERE id = ?", (row["answer_artifact_id"],)
        ).fetchone()
        assert ref["kind"] == "decision_answer"
        assert ref["repo"] == "factory"
        assert ref["path"] == f"_factory/stages/st-demo/decision-answer-{request_id}.md"
        assert ref["sha256"] == sha256_file(artifact_path)  # registered ref matches disk
        # D-0015: committed in the factory repo BEFORE the recording tx — the
        # ref must resolve at the recorded commit (else the next recover()'s
        # verify_integrity pass would abort the orchestrator start).
        assert ref["git_commit"]
        shown = subprocess.run(
            ["git", "show", f"{ref['git_commit']}:{ref['path']}"],
            cwd=cli_env.home,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert shown == body
        message = subprocess.run(
            ["git", "log", "-1", "--format=%B", ref["git_commit"]],
            cwd=cli_env.home,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "Factory-Unit: stage/st-demo" in message
        event = conn.execute(
            "SELECT * FROM events WHERE event_type = 'decision_answered'"
        ).fetchone()
        assert event is not None
        assert event["actor"] == "founder"
        payload = json.loads(event["payload_json"])
        assert payload["via"] == "cli"
        assert payload["answer"] == "approve"
        assert payload["answer_artifact_id"] == ref["id"]
    finally:
        db.close()


def test_decide_rejects_unknown_and_already_answered(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(cli_env, "init") == 0
    _git_init_home(cli_env.home)
    request_id = _seed_decision(cli_env)
    assert _cli(cli_env, "decide", str(request_id + 999), "approve") == 1
    err = capsys.readouterr().err
    assert "no PENDING decision request" in err
    assert str(request_id) in err  # helpful: lists the actually-pending ids

    assert _cli(cli_env, "decide", str(request_id), "approve") == 0
    assert _cli(cli_env, "decide", str(request_id), "approve") == 1  # answered = no longer pending
    assert "no PENDING decision request" in capsys.readouterr().err


def test_decide_rejects_empty_answer(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(cli_env, "init") == 0
    request_id = _seed_decision(cli_env)
    assert _cli(cli_env, "decide", str(request_id), "   ") == 1
    assert "empty answer" in capsys.readouterr().err


def test_decide_without_db_fails_explicitly(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(cli_env, "decide", "1", "approve") == 1
    assert "sf-factory init" in capsys.readouterr().err
