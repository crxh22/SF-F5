"""Unit tests for db.py (design §8): migrations idempotent, monotonic event seq,
partial-unique open-escalation index, consultation-tagging CHECK, transaction
re-entrancy guard — plus repository round-trips and WAL/FK connection settings."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sf_factory import db as dbmod
from sf_factory.db import (
    MIGRATIONS_DIR,
    Database,
    answer_decision,
    bump_churn,
    deps_done,
    finalize_process,
    find_artifact_ref,
    findings,
    get_phase,
    get_stage,
    heartbeat_process,
    insert_artifact_ref,
    insert_consultation,
    insert_dag_edge,
    insert_decision_request,
    insert_escalation,
    insert_event,
    insert_finding,
    insert_fix_iteration,
    insert_phase,
    insert_process,
    insert_stage,
    insert_token_usage,
    iter_latest_artifact_refs,
    last_session_id,
    latest_artifact,
    list_units,
    mark_decision_alerted,
    open_escalation,
    pending_decisions,
    processes_in_state,
    resolve_escalation,
    set_finding_status,
    set_stage_worktree,
    set_unit_state,
    unit_token_total,
)
from sf_factory.models import (
    ArtifactRef,
    DecisionRequest,
    Escalation,
    FactoryError,
    Finding,
    Level,
    MigrationError,
    Phase,
    PhaseState,
    ProcessRecord,
    Stage,
    StageState,
    utc_now,
)

T0 = "2026-06-10T00:00:00Z"

EXPECTED_TABLES = {
    "schema_migrations",
    "phases",
    "stages",
    "dag_edges",
    "events",
    "fix_iterations",
    "churn",
    "consultations",
    "escalations",
    "audit_findings",
    "token_ledger",
    "process_registry",
    "artifact_refs",
    "decision_requests",
}


def make_phase(phase_id: str = "ph-1", state: PhaseState = PhaseState.PENDING) -> Phase:
    return Phase(phase_id, "proj", f"Phase {phase_id}", state, None, None, T0, T0)


def make_stage(
    stage_id: str = "st-1",
    phase_id: str = "ph-1",
    state: StageState = StageState.PENDING,
    risk_class: str = "routine",
) -> Stage:
    return Stage(stage_id, phase_id, f"Stage {stage_id}", risk_class, state, None, None,
                 None, T0, T0)


def make_artifact(unit_id: str = "st-1", kind: str = "spec", path: str | None = None,
                  sha: str = "a" * 64) -> ArtifactRef:
    return ArtifactRef(None, "stage", unit_id, kind, "workspace",
                       path or f"_factory/stages/{unit_id}/spec.md", sha, None, T0)


def make_process(kind: str = "agent", role: str = "builder_routine",
                 cp_id: str | None = None, state: str = "spawned",
                 unit_id: str = "st-1", session_id: str | None = None) -> ProcessRecord:
    return ProcessRecord(None, "stage", unit_id, kind, role, cp_id, session_id, 4242,
                         "stub --scenario x", "/tmp", state, None, "/tmp/log.ndjson",
                         T0, None, None)


def make_escalation(unit_id: str = "st-1", trigger: str = "max_fix_iterations",
                    status: str = "open", event_seq: int | None = None) -> Escalation:
    return Escalation(None, "stage", unit_id, trigger, "phase_architect", None, event_seq,
                      status, None, T0, None)


def make_finding(stage_id: str = "st-1", report_artifact_id: int = 1,
                 status: str = "open") -> Finding:
    return Finding(None, stage_id, "auditor_cross_model", "F-1", "major",
                   report_artifact_id, status, None, None, T0, T0)


def make_decision(unit_id: str = "st-1", request_artifact_id: int = 1,
                  created_at: str = T0, alerted_at: str | None = None) -> DecisionRequest:
    return DecisionRequest(None, "stage", unit_id, "critical_stage", request_artifact_id,
                           "pending", None, None, created_at, alerted_at, None)


@pytest.fixture()
def seeded_stage(db: Database):
    """A phase + stage pair every stage-FK table can hang rows on."""
    with db.transaction() as conn:
        insert_phase(conn, make_phase())
        insert_stage(conn, make_stage())
    return db


# ------------------------------------------------------------------- migrations


class TestMigrations:
    def test_fresh_migrate_applies_0001(self, db_path: Path):
        database = Database(db_path, busy_timeout_ms=5000)
        database.open()
        try:
            assert database.migrate(MIGRATIONS_DIR) == [1]
        finally:
            database.close()

    def test_migrate_idempotent(self, db: Database):
        # `db` fixture already migrated; a second run applies nothing.
        assert db.migrate(MIGRATIONS_DIR) == []

    def test_all_tables_created(self, db: Database):
        rows = db.read().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r[0] for r in rows} - {"sqlite_sequence"}
        assert names == EXPECTED_TABLES

    def test_expected_indices_exist(self, db: Database):
        rows = db.read().execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        assert {r[0] for r in rows} >= {
            "idx_dag_to", "idx_events_unit", "idx_events_type", "uq_open_escalation",
            "idx_findings_stage", "idx_token_unit", "idx_proc_state", "idx_artifacts_unit",
        }

    def test_schema_migrations_row_recorded(self, db: Database):
        row = db.read().execute(
            "SELECT version, description, applied_at FROM schema_migrations"
        ).fetchone()
        assert row["version"] == 1
        assert row["description"] == "init"
        assert row["applied_at"]  # ISO timestamp written

    def test_failed_migration_rolls_back_whole_file(self, db_path: Path, tmp_path: Path):
        bad_dir = tmp_path / "migs"
        bad_dir.mkdir()
        (bad_dir / "0001_bad.sql").write_text(
            "CREATE TABLE will_vanish (id INTEGER PRIMARY KEY);\n"
            "CREATE TABLE oops (broken syntax here;\n",
            encoding="utf-8",
        )
        database = Database(db_path, busy_timeout_ms=5000)
        database.open()
        try:
            with pytest.raises(MigrationError):
                database.migrate(bad_dir)
            names = {
                r[0]
                for r in database.read()
                .execute("SELECT name FROM sqlite_master WHERE type='table'")
                .fetchall()
            }
            assert "will_vanish" not in names  # first statement rolled back too
            assert "schema_migrations" not in names  # nothing recorded
        finally:
            database.close()

    def test_misnamed_migration_rejected(self, db_path: Path, tmp_path: Path):
        bad_dir = tmp_path / "migs"
        bad_dir.mkdir()
        (bad_dir / "init.sql").write_text("CREATE TABLE x (id INTEGER);", encoding="utf-8")
        database = Database(db_path, busy_timeout_ms=5000)
        database.open()
        try:
            with pytest.raises(MigrationError, match="misnamed"):
                database.migrate(bad_dir)
        finally:
            database.close()

    def test_missing_migrations_dir_rejected(self, db: Database, tmp_path: Path):
        with pytest.raises(MigrationError, match="not found"):
            db.migrate(tmp_path / "no-such-dir")

    def test_out_of_order_new_migration_rejected(self, db: Database, tmp_path: Path):
        # db already has version 1 applied from the real dir; a dir whose only pending
        # file is 0000_* (below the applied max) must fail explicitly, never run silently.
        stale_dir = tmp_path / "migs"
        stale_dir.mkdir()
        (stale_dir / "0000_late.sql").write_text(
            "CREATE TABLE late (id INTEGER);", encoding="utf-8"
        )
        with pytest.raises(MigrationError, match="out-of-order"):
            db.migrate(stale_dir)

    def test_followup_migration_applies_in_order(self, db: Database, tmp_path: Path):
        two_dir = tmp_path / "migs"
        two_dir.mkdir()
        for f in MIGRATIONS_DIR.glob("*.sql"):
            (two_dir / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        (two_dir / "0002_extra.sql").write_text(
            "CREATE TABLE extra (id INTEGER PRIMARY KEY);", encoding="utf-8"
        )
        assert db.migrate(two_dir) == [2]
        versions = [
            r[0]
            for r in db.read().execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert versions == [1, 2]

    def test_migrate_read_only_rejected(self, db: Database, db_path: Path):
        ro = Database(db_path, busy_timeout_ms=5000)
        ro.open(read_only=True)
        try:
            with pytest.raises(MigrationError, match="read-only"):
                ro.migrate(MIGRATIONS_DIR)
        finally:
            ro.close()


# ------------------------------------------------------------ connection settings


class TestConnection:
    def test_wal_and_pragmas(self, db: Database):
        conn = db.read()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL

    def test_foreign_keys_enforced(self, db: Database):
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                insert_stage(conn, make_stage(phase_id="ghost-phase"))

    def test_read_before_open_raises(self, db_path: Path):
        database = Database(db_path, busy_timeout_ms=100)
        with pytest.raises(FactoryError, match="not open"):
            database.read()

    def test_double_open_raises(self, db: Database):
        with pytest.raises(FactoryError, match="already open"):
            db.open()

    def test_read_only_connection_cannot_write(self, db: Database, db_path: Path):
        ro = Database(db_path, busy_timeout_ms=100)
        ro.open(read_only=True)
        try:
            with pytest.raises(sqlite3.OperationalError):
                ro.read().execute(
                    "INSERT INTO phases VALUES ('x','p','n','PENDING',NULL,NULL,'t','t')"
                )
            with pytest.raises(FactoryError):
                with ro.transaction():
                    pass
        finally:
            ro.close()

    def test_read_only_sees_writer_committed_state(self, db: Database, db_path: Path):
        with db.transaction() as conn:
            insert_phase(conn, make_phase("ph-ro"))
        ro = Database(db_path, busy_timeout_ms=100)
        ro.open(read_only=True)
        try:
            assert get_phase(ro.read(), "ph-ro") is not None
        finally:
            ro.close()


# ------------------------------------------------------------------ transactions


class TestTransaction:
    def test_commit_persists(self, db: Database):
        with db.transaction() as conn:
            insert_phase(conn, make_phase("ph-tx"))
        assert get_phase(db.read(), "ph-tx") is not None

    def test_exception_rolls_back_all_coupled_writes(self, db: Database):
        class Boom(Exception):
            pass

        with pytest.raises(Boom):
            with db.transaction() as conn:
                insert_phase(conn, make_phase("ph-rb"))
                insert_event(conn, unit_level="phase", unit_id="ph-rb",
                             event_type="transition", actor="control_plane")
                raise Boom()
        conn = db.read()
        assert get_phase(conn, "ph-rb") is None
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0

    def test_reentrancy_guard_raises(self, db: Database):
        with db.transaction():
            with pytest.raises(FactoryError, match="re-entrant"):
                with db.transaction():
                    pass

    def test_usable_after_reentrancy_error(self, db: Database):
        # The guard must not wedge the outer transaction or the connection.
        with db.transaction() as conn:
            with pytest.raises(FactoryError):
                with db.transaction():
                    pass
            insert_phase(conn, make_phase("ph-after-guard"))
        assert get_phase(db.read(), "ph-after-guard") is not None
        with db.transaction() as conn:
            insert_phase(conn, make_phase("ph-next"))
        assert get_phase(db.read(), "ph-next") is not None

    def test_transaction_is_immediate(self, db: Database):
        with db.transaction() as conn:
            assert conn.in_transaction  # BEGIN issued up front, not lazily


# ------------------------------------------------------------- units and events


class TestUnitsRepo:
    def test_phase_roundtrip(self, db: Database):
        phase = make_phase("ph-x", PhaseState.PLANNING)
        with db.transaction() as conn:
            insert_phase(conn, phase)
        assert get_phase(db.read(), "ph-x") == phase

    def test_get_phase_missing_is_none(self, db: Database):
        assert get_phase(db.read(), "nope") is None

    def test_stage_roundtrip(self, seeded_stage: Database):
        db = seeded_stage
        stage = get_stage(db.read(), "st-1")
        assert stage == make_stage()

    def test_get_stage_missing_is_none(self, db: Database):
        assert get_stage(db.read(), "nope") is None

    def test_list_units_filters_and_orders(self, db: Database):
        with db.transaction() as conn:
            insert_phase(conn, make_phase("ph-b", PhaseState.RUNNING))
            insert_phase(conn, make_phase("ph-a", PhaseState.PENDING))
            insert_phase(conn, make_phase("ph-c", PhaseState.DONE))
        conn = db.read()
        assert [p.id for p in list_units(conn, Level.PHASE)] == ["ph-a", "ph-b", "ph-c"]
        running = list_units(conn, Level.PHASE, states=("RUNNING", "DONE"))
        assert [p.id for p in running] == ["ph-b", "ph-c"]

    def test_list_units_stage_level(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            insert_stage(conn, make_stage("st-2"))
        units = list_units(db.read(), Level.STAGE, states=("PENDING",))
        assert [u.id for u in units] == ["st-1", "st-2"]
        assert all(isinstance(u, Stage) for u in units)

    def test_set_unit_state_updates_state_and_timestamp(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            set_unit_state(conn, Level.STAGE, "st-1", "SPEC")
        stage = get_stage(db.read(), "st-1")
        assert stage.state is StageState.SPEC
        assert stage.updated_at >= T0
        assert stage.created_at == T0

    def test_set_unit_state_phase_level(self, db: Database):
        with db.transaction() as conn:
            insert_phase(conn, make_phase("ph-s"))
            set_unit_state(conn, Level.PHASE, "ph-s", "PLANNING")
        assert get_phase(db.read(), "ph-s").state is PhaseState.PLANNING

    def test_set_unit_state_unknown_unit_raises(self, db: Database):
        with pytest.raises(FactoryError, match="unknown"):
            with db.transaction() as conn:
                set_unit_state(conn, Level.STAGE, "ghost", "SPEC")

    def test_set_unit_state_rejects_non_enum_state_via_ddl_check(self, seeded_stage: Database):
        db = seeded_stage
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                set_unit_state(conn, Level.STAGE, "st-1", "HALF_DONE")

    def test_set_stage_worktree(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            set_stage_worktree(conn, "st-1", "stage/st-1", "/work/.worktrees/st-1")
        stage = get_stage(db.read(), "st-1")
        assert stage.branch == "stage/st-1"
        assert stage.worktree_path == "/work/.worktrees/st-1"

    def test_set_stage_worktree_unknown_raises(self, db: Database):
        with pytest.raises(FactoryError, match="unknown"):
            with db.transaction() as conn:
                set_stage_worktree(conn, "ghost", "b", "p")


class TestEventsRepo:
    def test_seq_monotonic_and_payload_roundtrip(self, db: Database):
        with db.transaction() as conn:
            s1 = insert_event(conn, unit_level="factory", unit_id=None,
                              event_type="alert", actor="control_plane",
                              payload={"reason": "stall", "n": 3})
            s2 = insert_event(conn, unit_level="factory", unit_id=None,
                              event_type="alert", actor="control_plane")
        assert s2 == s1 + 1
        row = db.read().execute("SELECT * FROM events WHERE seq = ?", (s1,)).fetchone()
        assert row["payload_json"] == '{"reason": "stall", "n": 3}'
        assert row["unit_id"] is None

    def test_seq_never_reused_after_delete(self, db: Database):
        # AUTOINCREMENT guarantee: the §2 escalation cursors rely on it.
        with db.transaction() as conn:
            insert_event(conn, unit_level="factory", unit_id=None,
                         event_type="alert", actor="control_plane")
            last = insert_event(conn, unit_level="factory", unit_id=None,
                                event_type="alert", actor="control_plane")
        with db.transaction() as conn:
            conn.execute("DELETE FROM events WHERE seq = ?", (last,))
        with db.transaction() as conn:
            new_seq = insert_event(conn, unit_level="factory", unit_id=None,
                                   event_type="alert", actor="control_plane")
        assert new_seq > last

    def test_transition_fields_stored(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            seq = insert_event(conn, unit_level="stage", unit_id="st-1",
                               event_type="transition", actor="control_plane",
                               from_state="PENDING", to_state="SPEC")
        row = db.read().execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
        assert (row["from_state"], row["to_state"]) == ("PENDING", "SPEC")

    def test_null_unit_id_only_for_factory_level(self, db: Database):
        with pytest.raises(FactoryError, match="factory"):
            with db.transaction() as conn:
                insert_event(conn, unit_level="stage", unit_id=None,
                             event_type="transition", actor="control_plane")

    def test_unknown_unit_level_rejected_by_ddl(self, db: Database):
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                insert_event(conn, unit_level="galaxy", unit_id="g-1",
                             event_type="transition", actor="control_plane")


# ------------------------------------------------------------------ DAG and deps


class TestDagRepo:
    @pytest.fixture()
    def dag_db(self, db: Database) -> Database:
        with db.transaction() as conn:
            insert_phase(conn, make_phase())
            for sid in ("st-a", "st-b", "st-c"):
                insert_stage(conn, make_stage(sid))
            insert_dag_edge(conn, Level.STAGE, "st-a", "st-c")
            insert_dag_edge(conn, Level.STAGE, "st-b", "st-c")
        return db

    def test_no_deps_is_done(self, dag_db: Database):
        assert deps_done(dag_db.read(), Level.STAGE, "st-a") is True

    def test_unmet_deps_block(self, dag_db: Database):
        assert deps_done(dag_db.read(), Level.STAGE, "st-c") is False

    def test_partial_deps_still_block(self, dag_db: Database):
        with dag_db.transaction() as conn:
            set_unit_state(conn, Level.STAGE, "st-a", "DONE")
        assert deps_done(dag_db.read(), Level.STAGE, "st-c") is False

    def test_all_deps_done_unblocks(self, dag_db: Database):
        with dag_db.transaction() as conn:
            set_unit_state(conn, Level.STAGE, "st-a", "DONE")
            set_unit_state(conn, Level.STAGE, "st-b", "DONE")
        assert deps_done(dag_db.read(), Level.STAGE, "st-c") is True

    def test_dangling_prerequisite_blocks(self, dag_db: Database):
        # An edge from a unit that has no row must block, not silently pass (§4 deps_done).
        with dag_db.transaction() as conn:
            insert_dag_edge(conn, Level.STAGE, "ghost", "st-a")
        assert deps_done(dag_db.read(), Level.STAGE, "st-a") is False

    def test_levels_are_separate_namespaces(self, dag_db: Database):
        with dag_db.transaction() as conn:
            insert_phase(conn, make_phase("ph-2"))
            insert_dag_edge(conn, Level.PHASE, "ph-1", "ph-2")
        conn = dag_db.read()
        assert deps_done(conn, Level.PHASE, "ph-2") is False  # ph-1 PENDING
        with dag_db.transaction() as tx:
            set_unit_state(tx, Level.PHASE, "ph-1", "PLANNING")
            set_unit_state(tx, Level.PHASE, "ph-1", "CONTRACTS_FROZEN")
        assert deps_done(conn, Level.PHASE, "ph-2") is False

    def test_duplicate_edge_rejected(self, dag_db: Database):
        with pytest.raises(sqlite3.IntegrityError):
            with dag_db.transaction() as conn:
                insert_dag_edge(conn, Level.STAGE, "st-a", "st-c")


# -------------------------------------------------------------------- artifacts


class TestArtifactsRepo:
    def test_insert_and_latest(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            first = insert_artifact_ref(conn, make_artifact(sha="1" * 64))
            second = insert_artifact_ref(conn, make_artifact(sha="2" * 64))
        assert second == first + 1
        latest = latest_artifact(db.read(), "stage", "st-1", "spec")
        assert latest.id == second
        assert latest.sha256 == "2" * 64

    def test_latest_missing_is_none(self, db: Database):
        assert latest_artifact(db.read(), "stage", "ghost", "spec") is None

    def test_unique_repo_path_sha_enforced(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            insert_artifact_ref(conn, make_artifact())
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                insert_artifact_ref(conn, make_artifact())

    def test_same_path_new_hash_allowed(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            insert_artifact_ref(conn, make_artifact(sha="a" * 64))
            insert_artifact_ref(conn, make_artifact(sha="b" * 64))

    def test_iter_latest_artifact_refs(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            insert_artifact_ref(conn, make_artifact(kind="spec", sha="1" * 64))
            keep_spec = insert_artifact_ref(conn, make_artifact(kind="spec", sha="2" * 64))
            keep_report = insert_artifact_ref(
                conn,
                make_artifact(kind="validation_report",
                              path="_factory/stages/st-1/validation-report.md",
                              sha="3" * 64),
            )
            keep_other_unit = insert_artifact_ref(
                conn,
                make_artifact(unit_id="st-2", kind="spec",
                              path="_factory/stages/st-2/spec.md", sha="4" * 64),
            )
        got = {ref.id for ref in iter_latest_artifact_refs(db.read())}
        assert got == {keep_spec, keep_report, keep_other_unit}

    def test_repo_value_constrained_by_ddl(self, seeded_stage: Database):
        db = seeded_stage
        bad = ArtifactRef(None, "stage", "st-1", "spec", "elsewhere", "p", "c" * 64, None, T0)
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                insert_artifact_ref(conn, bad)

    def test_find_artifact_ref_hit(self, seeded_stage: Database):
        # CCR-1: the get-or-create probe of artifacts.register_artifact.
        db = seeded_stage
        ref = make_artifact(sha="5" * 64)
        with db.transaction() as conn:
            ref_id = insert_artifact_ref(conn, ref)
        found = find_artifact_ref(db.read(), ref.repo, ref.path, ref.sha256)
        assert found is not None
        assert found.id == ref_id
        assert (found.repo, found.path, found.sha256) == (ref.repo, ref.path, ref.sha256)
        assert (found.unit_level, found.unit_id, found.kind) == ("stage", "st-1", "spec")

    def test_find_artifact_ref_miss(self, seeded_stage: Database):
        db = seeded_stage
        ref = make_artifact(sha="5" * 64)
        with db.transaction() as conn:
            insert_artifact_ref(conn, ref)
        conn = db.read()
        assert find_artifact_ref(conn, ref.repo, ref.path, "6" * 64) is None  # other hash
        assert find_artifact_ref(conn, ref.repo, "other/path.md", ref.sha256) is None
        assert find_artifact_ref(conn, "factory", ref.path, ref.sha256) is None  # other repo

    def test_find_artifact_ref_resolves_unique_triple(self, seeded_stage: Database):
        # Same path re-registered with a new hash: each triple resolves to its own row.
        db = seeded_stage
        with db.transaction() as conn:
            first = insert_artifact_ref(conn, make_artifact(sha="a" * 64))
            second = insert_artifact_ref(conn, make_artifact(sha="b" * 64))
        conn = db.read()
        path = make_artifact().path
        assert find_artifact_ref(conn, "workspace", path, "a" * 64).id == first
        assert find_artifact_ref(conn, "workspace", path, "b" * 64).id == second


# -------------------------------------------------------------------- processes


class TestProcessRepo:
    def test_roundtrip_and_states(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            pid = insert_process(conn, make_process())
        rows = processes_in_state(db.read(), "spawned")
        assert [r.id for r in rows] == [pid]
        rec = rows[0]
        assert rec.kind == "agent"
        assert rec.role == "builder_routine"
        assert rec.cp_id is None
        assert rec.cmdline == "stub --scenario x"

    def test_heartbeat_and_finalize(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            pid = insert_process(conn, make_process())
        beat = utc_now()
        with db.transaction() as conn:
            heartbeat_process(conn, pid, beat)
            finalize_process(conn, pid, state="exited", exit_code=0, ended_at=beat)
        row = db.read().execute(
            "SELECT * FROM process_registry WHERE id = ?", (pid,)
        ).fetchone()
        assert (row["state"], row["exit_code"], row["heartbeat_at"], row["ended_at"]) == (
            "exited", 0, beat, beat,
        )
        assert processes_in_state(db.read(), "spawned") == []

    def test_finalize_unknown_process_raises(self, db: Database):
        with pytest.raises(FactoryError, match="unknown process"):
            with db.transaction() as conn:
                finalize_process(conn, 999, state="exited", exit_code=0, ended_at=T0)

    def test_heartbeat_unknown_process_raises(self, db: Database):
        with pytest.raises(FactoryError, match="unknown process"):
            with db.transaction() as conn:
                heartbeat_process(conn, 999, T0)

    def test_consultation_tagging_check_requires_cp_id(self, db: Database):
        # DDL CHECK: (kind='consultation') = (cp_id IS NOT NULL) — §2 creep-scan backstop.
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                insert_process(conn, make_process(kind="consultation", role="cp1_triage",
                                                  cp_id=None))

    def test_consultation_tagging_check_forbids_cp_id_on_agent(self, db: Database):
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                insert_process(conn, make_process(kind="agent", cp_id="CP-1"))

    def test_consultation_tagging_check_forbids_cp_id_on_tests(self, db: Database):
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                insert_process(conn, make_process(kind="tests", role="test_suite",
                                                  cp_id="CP-1"))

    def test_valid_taggings_accepted(self, db: Database):
        with db.transaction() as conn:
            insert_process(conn, make_process(kind="consultation", role="cp1_triage",
                                              cp_id="CP-1"))
            insert_process(conn, make_process(kind="agent"))
            insert_process(conn, make_process(kind="tests", role="test_suite"))

    def test_invalid_kind_and_state_rejected(self, db: Database):
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                insert_process(conn, make_process(kind="daemon"))
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                insert_process(conn, make_process(state="hibernating"))

    # ------------------------------------------------------- session_id (CCR-1)

    def test_session_id_roundtrip_via_insert(self, seeded_stage: Database):
        # A resumed spawn knows its session up front: insert persists it as-is.
        db = seeded_stage
        with db.transaction() as conn:
            insert_process(conn, make_process(session_id="sess-resume"))
        rec = processes_in_state(db.read(), "spawned")[0]
        assert rec.session_id == "sess-resume"

    def test_finalize_writes_session_id(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            pid = insert_process(conn, make_process())
        assert processes_in_state(db.read(), "spawned")[0].session_id is None
        with db.transaction() as conn:
            finalize_process(conn, pid, state="exited", exit_code=0, ended_at=T0,
                             session_id="sess-from-stream")
        rec = processes_in_state(db.read(), "exited")[0]
        assert rec.session_id == "sess-from-stream"
        assert rec.ended_at == T0

    def test_finalize_none_session_id_leaves_existing(self, seeded_stage: Database):
        # None = leave unchanged: a stream without a result line must never clobber
        # the session id recorded at (resumed) spawn time.
        db = seeded_stage
        with db.transaction() as conn:
            pid = insert_process(conn, make_process(session_id="sess-keep"))
        with db.transaction() as conn:
            finalize_process(conn, pid, state="timed_out", exit_code=None, ended_at=T0)
        assert processes_in_state(db.read(), "timed_out")[0].session_id == "sess-keep"


class TestLastSessionId:
    """db.last_session_id (CCR-1): latest non-NULL session_id among FINALIZED
    processes of that unit+role — what continue_session resumes after a restart."""

    def test_picks_the_latest_finalized(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            p1 = insert_process(conn, make_process())
            p2 = insert_process(conn, make_process())
            finalize_process(conn, p1, state="exited", exit_code=0, ended_at=T0,
                             session_id="sess-old")
            finalize_process(conn, p2, state="exited", exit_code=0, ended_at=T0,
                             session_id="sess-new")
        assert last_session_id(db.read(), unit_level="stage", unit_id="st-1",
                               role="builder_routine") == "sess-new"

    def test_skips_finalized_rows_without_session(self, seeded_stage: Database):
        # A later run whose CLI never reported a session must not shadow the
        # latest resumable one.
        db = seeded_stage
        with db.transaction() as conn:
            p1 = insert_process(conn, make_process())
            p2 = insert_process(conn, make_process())
            finalize_process(conn, p1, state="exited", exit_code=0, ended_at=T0,
                             session_id="sess-resumable")
            finalize_process(conn, p2, state="killed", exit_code=None, ended_at=T0)
        assert last_session_id(db.read(), unit_level="stage", unit_id="st-1",
                               role="builder_routine") == "sess-resumable"

    def test_ignores_non_finalized_rows(self, seeded_stage: Database):
        # An in-flight process (even with a known session id) is not finalized.
        db = seeded_stage
        with db.transaction() as conn:
            insert_process(conn, make_process(session_id="sess-inflight", state="running"))
        assert last_session_id(db.read(), unit_level="stage", unit_id="st-1",
                               role="builder_routine") is None

    def test_filters_by_role(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            p1 = insert_process(conn, make_process(role="builder_routine"))
            p2 = insert_process(conn, make_process(role="validator"))
            finalize_process(conn, p1, state="exited", exit_code=0, ended_at=T0,
                             session_id="sess-builder")
            finalize_process(conn, p2, state="exited", exit_code=0, ended_at=T0,
                             session_id="sess-validator")
        conn = db.read()
        assert last_session_id(conn, unit_level="stage", unit_id="st-1",
                               role="builder_routine") == "sess-builder"
        assert last_session_id(conn, unit_level="stage", unit_id="st-1",
                               role="validator") == "sess-validator"

    def test_filters_by_unit(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            insert_stage(conn, make_stage("st-2"))
            p1 = insert_process(conn, make_process(unit_id="st-2"))
            finalize_process(conn, p1, state="exited", exit_code=0, ended_at=T0,
                             session_id="sess-other-unit")
        conn = db.read()
        assert last_session_id(conn, unit_level="stage", unit_id="st-1",
                               role="builder_routine") is None
        assert last_session_id(conn, unit_level="stage", unit_id="st-2",
                               role="builder_routine") == "sess-other-unit"

    def test_none_when_no_rows(self, db: Database):
        assert last_session_id(db.read(), unit_level="stage", unit_id="ghost",
                               role="builder_routine") is None


# ----------------------------------------------------------------- token ledger


class TestTokenLedger:
    @pytest.fixture()
    def proc_id(self, seeded_stage: Database) -> int:
        with seeded_stage.transaction() as conn:
            return insert_process(conn, make_process())

    def test_total_sums_in_and_out(self, seeded_stage: Database, proc_id: int):
        db = seeded_stage
        with db.transaction() as conn:
            insert_token_usage(conn, process_id=proc_id, unit_level="stage", unit_id="st-1",
                               role="builder_routine", model="sonnet", tokens_in=100,
                               tokens_out=50, cost_usd=0.01)
            insert_token_usage(conn, process_id=proc_id, unit_level="stage", unit_id="st-1",
                               role="validator", model="sonnet", tokens_in=10,
                               tokens_out=5, cost_usd=None)
        assert unit_token_total(db.read(), "stage", "st-1") == 165

    def test_all_null_usage_reads_zero(self, seeded_stage: Database, proc_id: int):
        # §2: per-aggregate COALESCE — an unreported-usage stage reads 0, not NULL.
        db = seeded_stage
        with db.transaction() as conn:
            insert_token_usage(conn, process_id=proc_id, unit_level="stage", unit_id="st-1",
                               role="builder_routine", model="sonnet", tokens_in=None,
                               tokens_out=None, cost_usd=None)
        total = unit_token_total(db.read(), "stage", "st-1")
        assert total == 0
        assert isinstance(total, int)

    def test_no_rows_reads_zero(self, db: Database):
        assert unit_token_total(db.read(), "stage", "ghost") == 0

    def test_mixed_null_and_values(self, seeded_stage: Database, proc_id: int):
        db = seeded_stage
        with db.transaction() as conn:
            insert_token_usage(conn, process_id=proc_id, unit_level="stage", unit_id="st-1",
                               role="builder_routine", model="sonnet", tokens_in=None,
                               tokens_out=7, cost_usd=None)
        assert unit_token_total(db.read(), "stage", "st-1") == 7

    def test_estimated_defaults_to_zero(self, seeded_stage: Database, proc_id: int):
        db = seeded_stage
        with db.transaction() as conn:
            insert_token_usage(conn, process_id=proc_id, unit_level="stage", unit_id="st-1",
                               role="builder_routine", model="sonnet", tokens_in=1,
                               tokens_out=1, cost_usd=None)
        row = db.read().execute("SELECT estimated FROM token_ledger").fetchone()
        assert row["estimated"] == 0

    def test_estimated_flag_persisted(self, seeded_stage: Database, proc_id: int):
        # CCR-1: usage_missing_policy='estimate' rows are marked estimated=1.
        db = seeded_stage
        with db.transaction() as conn:
            insert_token_usage(conn, process_id=proc_id, unit_level="stage", unit_id="st-1",
                               role="builder_routine", model="sonnet", tokens_in=512,
                               tokens_out=None, cost_usd=None, estimated=True)
        row = db.read().execute(
            "SELECT estimated, tokens_in FROM token_ledger"
        ).fetchone()
        assert (row["estimated"], row["tokens_in"]) == (1, 512)

    def test_estimated_rows_count_toward_budget_total(self, seeded_stage: Database,
                                                      proc_id: int):
        # §2 context_budget: estimated rows count toward the total like any other.
        db = seeded_stage
        with db.transaction() as conn:
            insert_token_usage(conn, process_id=proc_id, unit_level="stage", unit_id="st-1",
                               role="builder_routine", model="sonnet", tokens_in=100,
                               tokens_out=20, cost_usd=0.01)
            insert_token_usage(conn, process_id=proc_id, unit_level="stage", unit_id="st-1",
                               role="builder_routine", model="sonnet", tokens_in=400,
                               tokens_out=None, cost_usd=None, estimated=True)
        assert unit_token_total(db.read(), "stage", "st-1") == 520


# ------------------------------------------------------------- fix loops / churn


class TestFixIterationsAndChurn:
    def test_iterations_assigned_sequentially(self, seeded_stage: Database):
        db = seeded_stage
        got = []
        for failing in (5, 5, 4):
            with db.transaction() as conn:
                got.append(insert_fix_iteration(conn, "st-1", failing, None))
        assert got == [1, 2, 3]

    def test_iterations_isolated_per_stage(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            insert_stage(conn, make_stage("st-2"))
            assert insert_fix_iteration(conn, "st-1", 3, None) == 1
            assert insert_fix_iteration(conn, "st-2", 9, None) == 1

    def test_iteration_rows_recorded(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            ref_id = insert_artifact_ref(conn, make_artifact(kind="validation_sidecar"))
            insert_fix_iteration(conn, "st-1", 2, ref_id)
        row = db.read().execute("SELECT * FROM fix_iterations").fetchone()
        assert (row["stage_id"], row["iteration"], row["failing_tests"],
                row["report_artifact_id"]) == ("st-1", 1, 2, ref_id)

    def test_churn_buckets_increment_independently(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            assert bump_churn(conn, "st-1", "src/a.py", 0) == 1
            assert bump_churn(conn, "st-1", "src/a.py", 0) == 2
            assert bump_churn(conn, "st-1", "src/a.py", 1) == 1
            assert bump_churn(conn, "st-1", "src/b.py", 0) == 1
            assert bump_churn(conn, "st-1", "src/a.py", 0) == 3
        rows = db.read().execute(
            "SELECT file_path, region, edit_count FROM churn ORDER BY file_path, region"
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            ("src/a.py", 0, 3), ("src/a.py", 1, 1), ("src/b.py", 0, 1),
        ]


# ---------------------------------------------------------------- consultations


class TestConsultationsRepo:
    def _row(self) -> dict:
        return {
            "cp_id": "CP-1",
            "unit_level": "stage",
            "unit_id": "st-1",
            "input_digest": "d" * 64,
            "schema_valid": 1,
            "fallback_used": 0,
            "verdict": "rebuild",
            "rationale": "tests regressed on iteration 2",
            "model": "haiku",
            "latency_ms": 1200,
            "cost_usd": 0.002,
            "tokens_in": 1500,
            "tokens_out": 80,
            "raw_log_path": ".factory/logs/77.ndjson",
        }

    def test_roundtrip(self, db: Database):
        with db.transaction() as conn:
            cid = insert_consultation(conn, self._row())
        row = db.read().execute(
            "SELECT * FROM consultations WHERE id = ?", (cid,)
        ).fetchone()
        assert row["verdict"] == "rebuild"
        assert row["fallback_used"] == 0
        assert row["created_at"]  # defaulted

    def test_explicit_created_at_kept(self, db: Database):
        data = self._row() | {"created_at": T0}
        with db.transaction() as conn:
            cid = insert_consultation(conn, data)
        row = db.read().execute(
            "SELECT created_at FROM consultations WHERE id = ?", (cid,)
        ).fetchone()
        assert row["created_at"] == T0

    def test_unknown_column_raises(self, db: Database):
        with pytest.raises(FactoryError, match="unknown consultations column"):
            with db.transaction() as conn:
                insert_consultation(conn, self._row() | {"verdcit": "typo"})

    def test_boolean_checks_enforced(self, db: Database):
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                insert_consultation(conn, self._row() | {"schema_valid": 2})


# ------------------------------------------------------------------ escalations


class TestEscalationsRepo:
    def test_roundtrip_and_open_lookup(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            esc_id = insert_escalation(conn, make_escalation())
        esc = open_escalation(db.read(), "stage", "st-1", "max_fix_iterations")
        assert esc is not None
        assert esc.id == esc_id
        assert esc.status == "open"
        assert esc.target == "phase_architect"

    def test_open_lookup_none_when_absent(self, db: Database):
        assert open_escalation(db.read(), "stage", "st-1", "churn_threshold") is None

    def test_partial_unique_index_blocks_second_open(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            insert_escalation(conn, make_escalation())
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                insert_escalation(conn, make_escalation())

    def test_different_trigger_may_be_open_concurrently(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            insert_escalation(conn, make_escalation(trigger="max_fix_iterations"))
            insert_escalation(conn, make_escalation(trigger="churn_threshold"))

    def test_resolved_escalation_frees_the_slot(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            first = insert_escalation(conn, make_escalation())
        with db.transaction() as conn:
            resolve_escalation(conn, first, "rework:BUILD")
        with db.transaction() as conn:
            insert_escalation(conn, make_escalation())  # re-arm after resolution
        resolved = db.read().execute(
            "SELECT * FROM escalations WHERE id = ?", (first,)
        ).fetchone()
        assert resolved["status"] == "resolved"
        assert resolved["resolution"] == "rework:BUILD"
        assert resolved["resolved_at"] is not None

    def test_resolve_twice_raises(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            esc_id = insert_escalation(conn, make_escalation())
        with db.transaction() as conn:
            resolve_escalation(conn, esc_id, "respec")
        with pytest.raises(FactoryError, match="no open escalation"):
            with db.transaction() as conn:
                resolve_escalation(conn, esc_id, "respec")

    def test_target_constrained_by_ddl(self, seeded_stage: Database):
        db = seeded_stage
        bad = Escalation(None, "stage", "st-1", "internal_error", "intern", None, None,
                         "open", None, T0, None)
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                insert_escalation(conn, bad)

    # -------------------------------------------------- event_seq cursor (CCR-1)

    def test_event_seq_write_read_roundtrip(self, seeded_stage: Database):
        db = seeded_stage
        with db.transaction() as conn:
            seq = insert_event(conn, unit_level="stage", unit_id="st-1",
                               event_type="contract_change_request", actor="builder_routine")
            insert_escalation(conn, make_escalation(trigger="contract_change_request",
                                                    event_seq=seq))
        esc = open_escalation(db.read(), "stage", "st-1", "contract_change_request")
        assert esc is not None
        assert esc.event_seq == seq

    def test_event_seq_null_roundtrip(self, seeded_stage: Database):
        # Non-sentinel triggers carry no cursor: NULL must survive the round-trip.
        db = seeded_stage
        with db.transaction() as conn:
            insert_escalation(conn, make_escalation(trigger="churn_threshold"))
        esc = open_escalation(db.read(), "stage", "st-1", "churn_threshold")
        assert esc.event_seq is None

    def test_event_seq_cursor_dedups_sentinel_scan(self, seeded_stage: Database):
        """The §2 always-fire sentinel SQL, literally: events newer than the
        MAX(event_seq) cursor fire; covered ones do not; no cursor -> all fire."""
        db = seeded_stage
        cursor_sql = (
            "SELECT seq FROM events WHERE unit_level = :l AND unit_id = :s"
            " AND event_type = 'contract_change_request' AND seq > COALESCE("
            "  (SELECT MAX(event_seq) FROM escalations WHERE unit_level = :l"
            "   AND unit_id = :s AND trigger = 'contract_change_request'), 0)"
        )
        params = {"l": "stage", "s": "st-1"}
        with db.transaction() as conn:
            first = insert_event(conn, unit_level="stage", unit_id="st-1",
                                 event_type="contract_change_request", actor="builder_routine")
        conn = db.read()
        # No escalation yet: COALESCE base 0 -> the sentinel fires.
        assert [r["seq"] for r in conn.execute(cursor_sql, params)] == [first]
        with db.transaction() as tx:
            esc_id = insert_escalation(tx, make_escalation(
                trigger="contract_change_request", event_seq=first))
        # Cursor written: the same event no longer fires.
        assert conn.execute(cursor_sql, params).fetchall() == []
        # A NEW sentinel event after rework is a new fact and fires again —
        # even with the prior escalation resolved (MAX over all rows keeps the cursor).
        with db.transaction() as tx:
            resolve_escalation(tx, esc_id, "rework:BUILD")
            second = insert_event(tx, unit_level="stage", unit_id="st-1",
                                  event_type="contract_change_request",
                                  actor="builder_routine")
        assert [r["seq"] for r in conn.execute(cursor_sql, params)] == [second]


# --------------------------------------------------------------------- findings


class TestFindingsRepo:
    @pytest.fixture()
    def finding_db(self, seeded_stage: Database) -> tuple[Database, int]:
        with seeded_stage.transaction() as conn:
            report = insert_artifact_ref(conn, make_artifact(kind="audit_report"))
            fid = insert_finding(conn, make_finding(report_artifact_id=report))
        return seeded_stage, fid

    def test_roundtrip(self, finding_db):
        db, fid = finding_db
        rows = findings(db.read(), "st-1")
        assert [f.id for f in rows] == [fid]
        assert rows[0].status == "open"

    def test_status_filter(self, finding_db):
        db, fid = finding_db
        with db.transaction() as conn:
            report = insert_artifact_ref(conn, make_artifact(kind="audit_report",
                                                             sha="9" * 64))
            insert_finding(conn, make_finding(report_artifact_id=report, status="complied"))
        conn = db.read()
        assert len(findings(conn, "st-1")) == 2
        assert [f.id for f in findings(conn, "st-1", statuses=("open",))] == [fid]
        assert len(findings(conn, "st-1", statuses=("open", "complied"))) == 2

    def test_set_status_with_resolution_fields(self, finding_db):
        db, fid = finding_db
        with db.transaction() as conn:
            contest = insert_artifact_ref(conn, make_artifact(kind="contest_rationale",
                                                              sha="8" * 64))
            set_finding_status(conn, fid, "contested", contest_artifact_id=contest)
            set_finding_status(conn, fid, "sustained", resolved_by="phase_architect")
        row = findings(db.read(), "st-1")[0]
        assert row.status == "sustained"
        assert row.contest_artifact_id == contest
        assert row.resolved_by == "phase_architect"

    def test_set_status_unknown_finding_raises(self, db: Database):
        with pytest.raises(FactoryError, match="unknown finding"):
            with db.transaction() as conn:
                set_finding_status(conn, 12345, "complied")

    def test_status_constrained_by_ddl(self, finding_db):
        db, fid = finding_db
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                set_finding_status(conn, fid, "shrugged")


# -------------------------------------------------------------------- decisions


class TestDecisionsRepo:
    @pytest.fixture()
    def request_artifact(self, seeded_stage: Database) -> int:
        with seeded_stage.transaction() as conn:
            return insert_artifact_ref(conn, make_artifact(kind="decision_request"))

    def test_roundtrip_and_pending(self, seeded_stage: Database, request_artifact: int):
        db = seeded_stage
        with db.transaction() as conn:
            rid = insert_decision_request(conn, make_decision(request_artifact_id=request_artifact))
        rows = pending_decisions(db.read())
        assert [r.id for r in rows] == [rid]
        assert rows[0].gate_kind == "critical_stage"

    def test_answer_decision(self, seeded_stage: Database, request_artifact: int):
        db = seeded_stage
        with db.transaction() as conn:
            rid = insert_decision_request(conn, make_decision(request_artifact_id=request_artifact))
        with db.transaction() as conn:
            answer_decision(conn, rid, "option_b", None)
        assert pending_decisions(db.read()) == []
        row = db.read().execute(
            "SELECT * FROM decision_requests WHERE id = ?", (rid,)
        ).fetchone()
        assert (row["status"], row["answer"]) == ("answered", "option_b")
        assert row["answered_at"] is not None

    def test_answer_twice_raises(self, seeded_stage: Database, request_artifact: int):
        db = seeded_stage
        with db.transaction() as conn:
            rid = insert_decision_request(conn, make_decision(request_artifact_id=request_artifact))
        with db.transaction() as conn:
            answer_decision(conn, rid, "a", None)
        with pytest.raises(FactoryError, match="no pending decision"):
            with db.transaction() as conn:
                answer_decision(conn, rid, "b", None)

    def test_unalerted_latency_filter(self, seeded_stage: Database, request_artifact: int):
        db = seeded_stage
        stale = "2026-06-01T00:00:00Z"  # far older than any cutoff
        fresh = "2099-01-01T00:00:00Z"  # far newer than any cutoff
        with db.transaction() as conn:
            old_id = insert_decision_request(
                conn, make_decision(request_artifact_id=request_artifact, created_at=stale))
            insert_decision_request(
                conn, make_decision(request_artifact_id=request_artifact, created_at=fresh))
            insert_decision_request(
                conn, make_decision(request_artifact_id=request_artifact, created_at=stale,
                                    alerted_at=T0))
        overdue = pending_decisions(db.read(), unalerted_older_than_h=24)
        assert [r.id for r in overdue] == [old_id]

    def test_status_constrained_by_ddl(self, seeded_stage: Database, request_artifact: int):
        db = seeded_stage
        bad = DecisionRequest(None, "stage", "st-1", "business", request_artifact,
                              "maybe", None, None, T0, None, None)
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                insert_decision_request(conn, bad)

    # -------------------------------------------- mark_decision_alerted (CCR-1)

    def test_mark_alerted_stops_latency_query_matching(self, seeded_stage: Database,
                                                       request_artifact: int):
        # §2 decision latency: pending + alerted_at IS NULL + stale. After marking,
        # the row stops matching — the alert must not re-fire every tick.
        db = seeded_stage
        stale = "2026-06-01T00:00:00Z"
        with db.transaction() as conn:
            rid = insert_decision_request(
                conn, make_decision(request_artifact_id=request_artifact, created_at=stale))
        assert [r.id for r in pending_decisions(db.read(), unalerted_older_than_h=24)] == [rid]
        alerted_at = utc_now()
        with db.transaction() as conn:
            mark_decision_alerted(conn, rid, alerted_at)
        assert pending_decisions(db.read(), unalerted_older_than_h=24) == []
        # Still pending and unanswered — only the alert state changed.
        row = pending_decisions(db.read())[0]
        assert (row.id, row.status, row.alerted_at, row.answered_at) == (
            rid, "pending", alerted_at, None,
        )

    def test_mark_alerted_leaves_other_rows_matching(self, seeded_stage: Database,
                                                     request_artifact: int):
        db = seeded_stage
        stale = "2026-06-01T00:00:00Z"
        with db.transaction() as conn:
            first = insert_decision_request(
                conn, make_decision(request_artifact_id=request_artifact, created_at=stale))
            second = insert_decision_request(
                conn, make_decision(request_artifact_id=request_artifact, created_at=stale))
        with db.transaction() as conn:
            mark_decision_alerted(conn, first, utc_now())
        overdue = pending_decisions(db.read(), unalerted_older_than_h=24)
        assert [r.id for r in overdue] == [second]

    def test_mark_alerted_unknown_request_raises(self, db: Database):
        with pytest.raises(FactoryError, match="unknown decision request"):
            with db.transaction() as conn:
                mark_decision_alerted(conn, 4321, T0)


# -------------------------------------------------- module-level sanity


class TestModuleSurface:
    def test_migrations_dir_constant_points_at_packaged_sql(self):
        assert MIGRATIONS_DIR.is_dir()
        assert (MIGRATIONS_DIR / "0001_init.sql").is_file()

    def test_repository_functions_exported(self):
        # The frozen §4 repository surface must exist by name.
        for name in (
            "insert_phase", "get_phase", "insert_stage", "get_stage", "list_units",
            "set_unit_state", "set_stage_worktree", "insert_event", "insert_dag_edge",
            "deps_done", "insert_artifact_ref", "latest_artifact", "find_artifact_ref",
            "iter_latest_artifact_refs", "insert_process", "finalize_process",
            "heartbeat_process", "processes_in_state", "last_session_id",
            "insert_token_usage", "unit_token_total", "insert_fix_iteration", "bump_churn",
            "insert_consultation", "insert_escalation", "resolve_escalation",
            "open_escalation", "insert_finding", "set_finding_status", "findings",
            "insert_decision_request", "answer_decision", "mark_decision_alerted",
            "pending_decisions",
        ):
            assert callable(getattr(dbmod, name)), name
