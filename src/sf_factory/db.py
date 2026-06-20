"""SQLite layer (design §2/§4): WAL connection, versioned migrations, the transaction
primitive, and typed repository functions. Pure SQL — no business rules.

The orchestrator process is the sole writer (DoD §6); ``cli status`` / the dashboard
use ``open(read_only=True)``. Every state transition composes its writes inside one
``Database.transaction()`` block (synchronous end-to-end — §7 invariant 1).

May import: models (+ stdlib).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

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

#: Packaged migrations (``NNNN_*.sql``) applied by ``Database.migrate``.
MIGRATIONS_DIR: Path = Path(__file__).resolve().parent / "migrations"

_MIGRATION_NAME_RE = re.compile(r"(\d{4})_(.+)\.sql")


class Database:
    """Owns the single SQLite connection (WAL, sole writer by §2 read/write rules)."""

    def __init__(self, path: Path, busy_timeout_ms: int) -> None:
        """Bind path; no I/O yet."""
        self._path = Path(path)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._conn: sqlite3.Connection | None = None
        self._read_only = False
        self._in_tx = False

    def open(self, *, read_only: bool = False) -> None:
        """Connect (mode=ro when read_only — `cli status`/dashboard reads); PRAGMA
        journal_mode=WAL, synchronous=NORMAL (WAL-safe tradeoff, stated: an OS crash may
        lose the last committed tx — acceptable because git+artifacts lead state and steps
        are re-runnable), foreign_keys=ON, busy_timeout."""
        if self._conn is not None:
            raise FactoryError(f"database already open: {self._path}")
        if read_only:
            # The path must be percent-encoded inside the URI (CCR-2 minor fix):
            # a literal '?'/'#' would truncate it into query/fragment and '%' would
            # be mis-decoded. quote() keeps '/' and ordinary path chars untouched —
            # behavior is identical for normal paths.
            conn = sqlite3.connect(
                f"file:{quote(str(self._path))}?mode=ro", uri=True, isolation_level=None
            )
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            conn.execute("PRAGMA foreign_keys = ON")
            if not read_only:
                mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                if mode != "wal":
                    raise FactoryError(
                        f"could not enable WAL on {self._path}: journal_mode={mode!r}"
                    )
                conn.execute("PRAGMA synchronous = NORMAL")
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        self._read_only = read_only

    def close(self) -> None:
        """Close the connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._in_tx = False

    def migrate(self, migrations_dir: Path) -> list[int]:
        """Apply pending NNNN_*.sql in order, each in its own tx, record in
        schema_migrations; raises MigrationError."""
        conn = self._require_open()
        if self._read_only:
            raise MigrationError("cannot migrate a read-only connection")
        migrations = _discover_migrations(Path(migrations_dir))
        applied = _applied_versions(conn)
        max_applied = max(applied, default=0)
        pending = [m for m in migrations if m[0] not in applied]
        for version, _, path in pending:
            if version < max_applied:
                raise MigrationError(
                    f"out-of-order migration {path.name}: version {version} is below "
                    f"already-applied version {max_applied}"
                )
        applied_now: list[int] = []
        for version, description, path in pending:
            try:
                sql = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise MigrationError(f"cannot read migration {path}: {exc}") from exc
            try:
                with self.transaction() as tx:
                    for statement in _split_statements(sql, source=path.name):
                        tx.execute(statement)
                    tx.execute(
                        "INSERT INTO schema_migrations (version, description, applied_at)"
                        " VALUES (?, ?, ?)",
                        (version, description, utc_now()),
                    )
            except MigrationError:
                raise
            except Exception as exc:
                raise MigrationError(f"migration {path.name} failed: {exc}") from exc
            applied_now.append(version)
        return applied_now

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE; yield conn; commit, rollback on exception. THE composition
        primitive for atomic writes. Invariant (§7): the block is synchronous end-to-end
        — no await inside; raises if a transaction is already active on this connection
        (re-entrancy guard, enforced not assumed)."""
        conn = self._require_open()
        if self._read_only:
            raise FactoryError("cannot start a write transaction on a read-only connection")
        if self._in_tx or conn.in_transaction:
            raise FactoryError(
                "re-entrant transaction: a transaction is already active on this connection"
            )
        self._in_tx = True
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
        finally:
            self._in_tx = False

    def read(self) -> sqlite3.Connection:
        """Connection for reads outside a write tx."""
        return self._require_open()

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise FactoryError(f"database not open: {self._path}")
        return self._conn


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, str, Path]]:
    if not migrations_dir.is_dir():
        raise MigrationError(f"migrations directory not found: {migrations_dir}")
    found: dict[int, tuple[int, str, Path]] = {}
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _MIGRATION_NAME_RE.fullmatch(path.name)
        if match is None:
            raise MigrationError(
                f"misnamed migration file {path.name}: expected NNNN_<description>.sql"
            )
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version}: "
                f"{found[version][2].name} and {path.name}"
            )
        found[version] = (version, match.group(2), path)
    return [found[v] for v in sorted(found)]


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row is None:
        return set()
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}


def _split_statements(sql: str, *, source: str) -> Iterator[str]:
    """Split a migration script into complete statements (semicolon-aware via
    sqlite3.complete_statement — safe for ';' inside string literals)."""
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if statement:
                yield statement
    residue = [
        ln for ln in buffer.splitlines() if ln.strip() and not ln.lstrip().startswith("--")
    ]
    if residue:
        raise MigrationError(
            f"migration {source} has an unterminated trailing statement: {residue[0].strip()!r}"
        )


# ------------------------------------------------------------ repository: units
# Pure SQL, no business rules; conn comes from Database.transaction()/read().


def _unit_table(level: Level) -> str:
    return "phases" if Level(level) is Level.PHASE else "stages"


def _phase_from_row(row: sqlite3.Row) -> Phase:
    return Phase(
        id=row["id"],
        project=row["project"],
        name=row["name"],
        state=PhaseState(row["state"]),
        branch=row["branch"],
        plan_artifact_id=row["plan_artifact_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _stage_from_row(row: sqlite3.Row) -> Stage:
    return Stage(
        id=row["id"],
        phase_id=row["phase_id"],
        name=row["name"],
        risk_class=row["risk_class"],
        state=StageState(row["state"]),
        branch=row["branch"],
        worktree_path=row["worktree_path"],
        spec_artifact_id=row["spec_artifact_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def insert_phase(conn: sqlite3.Connection, phase: Phase) -> None:
    conn.execute(
        "INSERT INTO phases (id, project, name, state, branch, plan_artifact_id,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            phase.id,
            phase.project,
            phase.name,
            phase.state.value,
            phase.branch,
            phase.plan_artifact_id,
            phase.created_at,
            phase.updated_at,
        ),
    )


def get_phase(conn: sqlite3.Connection, phase_id: str) -> Phase | None:
    row = conn.execute("SELECT * FROM phases WHERE id = ?", (phase_id,)).fetchone()
    return None if row is None else _phase_from_row(row)


def insert_stage(conn: sqlite3.Connection, stage: Stage) -> None:
    conn.execute(
        "INSERT INTO stages (id, phase_id, name, risk_class, state, branch,"
        " worktree_path, spec_artifact_id, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            stage.id,
            stage.phase_id,
            stage.name,
            stage.risk_class,
            stage.state.value,
            stage.branch,
            stage.worktree_path,
            stage.spec_artifact_id,
            stage.created_at,
            stage.updated_at,
        ),
    )


def get_stage(conn: sqlite3.Connection, stage_id: str) -> Stage | None:
    row = conn.execute("SELECT * FROM stages WHERE id = ?", (stage_id,)).fetchone()
    return None if row is None else _stage_from_row(row)


def list_units(
    conn: sqlite3.Connection, level: Level, states: Sequence[str] = ()
) -> list[Phase | Stage]:
    level = Level(level)
    sql = f"SELECT * FROM {_unit_table(level)}"  # noqa: S608 — table name is enum-derived
    params: tuple[str, ...] = ()
    if states:
        placeholders = ", ".join("?" * len(states))
        sql += f" WHERE state IN ({placeholders})"
        params = tuple(str(s) for s in states)
    sql += " ORDER BY id"
    rows = conn.execute(sql, params).fetchall()
    mapper = _phase_from_row if level is Level.PHASE else _stage_from_row
    return [mapper(row) for row in rows]


def set_unit_state(conn: sqlite3.Connection, level: Level, unit_id: str, state: str) -> None:
    """Update the unit state column. Called ONLY by statemachine (design §1/§4)."""
    cur = conn.execute(
        f"UPDATE {_unit_table(level)} SET state = ?, updated_at = ? WHERE id = ?",  # noqa: S608
        (str(state), utc_now(), unit_id),
    )
    if cur.rowcount != 1:
        raise FactoryError(f"unknown {Level(level).value} unit: {unit_id!r}")


def set_stage_worktree(
    conn: sqlite3.Connection, stage_id: str, branch: str, worktree_path: str
) -> None:
    cur = conn.execute(
        "UPDATE stages SET branch = ?, worktree_path = ?, updated_at = ? WHERE id = ?",
        (branch, worktree_path, utc_now(), stage_id),
    )
    if cur.rowcount != 1:
        raise FactoryError(f"unknown stage unit: {stage_id!r}")


# ------------------------------------------------------------ repository: events


def insert_event(
    conn: sqlite3.Connection,
    *,
    unit_level: str,
    unit_id: str | None,
    event_type: str,
    actor: str,
    from_state: str | None = None,
    to_state: str | None = None,
    payload: dict | None = None,
) -> int:
    """Append one event row; returns its monotonic seq."""
    if unit_id is None and unit_level != "factory":
        raise FactoryError(
            f"unit_id may be NULL only for unit_level='factory', got {unit_level!r}"
        )
    cur = conn.execute(
        "INSERT INTO events (unit_level, unit_id, event_type, from_state, to_state,"
        " actor, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            unit_level,
            unit_id,
            event_type,
            from_state,
            to_state,
            actor,
            json.dumps(payload if payload is not None else {}),
            utc_now(),
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


# --------------------------------------------------------------- repository: DAG


def insert_dag_edge(conn: sqlite3.Connection, level: Level, from_id: str, to_id: str) -> None:
    conn.execute(
        "INSERT INTO dag_edges (level, from_id, to_id) VALUES (?, ?, ?)",
        (Level(level).value, from_id, to_id),
    )


def list_dag_edges(conn: sqlite3.Connection, level: Level) -> list[tuple[str, str]]:
    """All (from_id, to_id) edges at one level — the seed-phases read path
    (phase-seeding design §2.3.2: duplicate-edge named abort + combined-graph
    acyclicity over existing ∪ plan edges). Deterministic ordering."""
    rows = conn.execute(
        "SELECT from_id, to_id FROM dag_edges WHERE level = ? ORDER BY from_id, to_id",
        (Level(level).value,),
    ).fetchall()
    return [(row["from_id"], row["to_id"]) for row in rows]


def deps_done(conn: sqlite3.Connection, level: Level, unit_id: str) -> bool:
    """True iff every prerequisite of the unit is DONE. A dangling prerequisite
    (edge whose from_id has no unit row) counts as NOT done — it must block, and
    the §4 stall detector pages rather than letting a broken plan run."""
    level = Level(level)
    done = (StageState.DONE if level is Level.STAGE else PhaseState.DONE).value
    row = conn.execute(
        f"""
        SELECT NOT EXISTS (
          SELECT 1 FROM dag_edges d
          LEFT JOIN {_unit_table(level)} u ON u.id = d.from_id
          WHERE d.level = ? AND d.to_id = ? AND (u.state IS NULL OR u.state != ?)
        )
        """,  # noqa: S608
        (level.value, unit_id, done),
    ).fetchone()
    return bool(row[0])


# ---------------------------------------------------------- repository: artifacts


def _artifact_from_row(row: sqlite3.Row) -> ArtifactRef:
    return ArtifactRef(
        id=row["id"],
        unit_level=row["unit_level"],
        unit_id=row["unit_id"],
        kind=row["kind"],
        repo=row["repo"],
        path=row["path"],
        sha256=row["sha256"],
        git_commit=row["git_commit"],
        created_at=row["created_at"],
    )


def insert_artifact_ref(conn: sqlite3.Connection, ref: ArtifactRef) -> int:
    """Plain insert (``ref.id`` ignored — assigned by the DB). The get-or-create
    semantics on (repo, path, sha256) live in artifacts.register_artifact."""
    cur = conn.execute(
        "INSERT INTO artifact_refs (unit_level, unit_id, kind, repo, path, sha256,"
        " git_commit, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ref.unit_level,
            ref.unit_id,
            ref.kind,
            ref.repo,
            ref.path,
            ref.sha256,
            ref.git_commit,
            ref.created_at,
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def latest_artifact(
    conn: sqlite3.Connection, unit_level: str, unit_id: str, kind: str
) -> ArtifactRef | None:
    row = conn.execute(
        "SELECT * FROM artifact_refs WHERE unit_level = ? AND unit_id = ? AND kind = ?"
        " ORDER BY id DESC LIMIT 1",
        (unit_level, unit_id, kind),
    ).fetchone()
    return None if row is None else _artifact_from_row(row)


def find_artifact_ref(
    conn: sqlite3.Connection, repo: str, path: str, sha256: str
) -> ArtifactRef | None:
    """Probe the UNIQUE (repo, path, sha256) key — the get-or-create lookup of
    artifacts.register_artifact (CCR-1): byte-identical re-registration returns
    the existing ref instead of violating the constraint."""
    row = conn.execute(
        "SELECT * FROM artifact_refs WHERE repo = ? AND path = ? AND sha256 = ?",
        (repo, path, sha256),
    ).fetchone()
    return None if row is None else _artifact_from_row(row)


def iter_latest_artifact_refs(conn: sqlite3.Connection) -> Iterator[ArtifactRef]:
    """Latest ref per (unit_level, unit_id, kind) — the §4 verify_integrity input."""
    cur = conn.execute(
        "SELECT * FROM artifact_refs WHERE id IN ("
        " SELECT MAX(id) FROM artifact_refs GROUP BY unit_level, unit_id, kind"
        ") ORDER BY id"
    )
    for row in cur:
        yield _artifact_from_row(row)


# ---------------------------------------------------------- repository: processes


def _process_from_row(row: sqlite3.Row) -> ProcessRecord:
    return ProcessRecord(
        id=row["id"],
        unit_level=row["unit_level"],
        unit_id=row["unit_id"],
        kind=row["kind"],
        role=row["role"],
        cp_id=row["cp_id"],
        session_id=row["session_id"],
        pid=row["pid"],
        cmdline=row["cmdline"],
        cwd=row["cwd"],
        state=row["state"],
        exit_code=row["exit_code"],
        ndjson_log_path=row["ndjson_log_path"],
        spawned_at=row["spawned_at"],
        heartbeat_at=row["heartbeat_at"],
        ended_at=row["ended_at"],
    )


def insert_process(conn: sqlite3.Connection, rec: ProcessRecord) -> int:
    cur = conn.execute(
        "INSERT INTO process_registry (unit_level, unit_id, kind, role, cp_id,"
        " session_id, pid, cmdline, cwd, state, exit_code, ndjson_log_path, spawned_at,"
        " heartbeat_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rec.unit_level,
            rec.unit_id,
            rec.kind,
            rec.role,
            rec.cp_id,
            rec.session_id,
            rec.pid,
            rec.cmdline,
            rec.cwd,
            rec.state,
            rec.exit_code,
            rec.ndjson_log_path,
            rec.spawned_at,
            rec.heartbeat_at,
            rec.ended_at,
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def mark_process_running(
    conn: sqlite3.Connection, process_id: int, *, pid: int, at: str
) -> None:
    """'spawned'→'running' (CCR-2): persist the child pid post-exec — the §5.5a
    cross-restart orphan sweep kills by ``process_registry.pid`` — and write
    ``heartbeat_at = at`` as the INITIAL heartbeat, so staleness math is sound
    from exec time. Strictly guarded on state='spawned': a finalized (or already
    running) row is left untouched and raises — ``ended_at``/``session_id`` are
    never written here, so ``last_session_id``'s finalized predicate is
    unaffected. Raises FactoryError on rowcount != 1."""
    cur = conn.execute(
        "UPDATE process_registry SET state = 'running', pid = ?, heartbeat_at = ?"
        " WHERE id = ? AND state = 'spawned'",
        (pid, at, process_id),
    )
    if cur.rowcount != 1:
        row = conn.execute(
            "SELECT state FROM process_registry WHERE id = ?", (process_id,)
        ).fetchone()
        if row is None:
            raise FactoryError(f"unknown process id: {process_id}")
        raise FactoryError(
            f"cannot mark process {process_id} running: state is {row['state']!r},"
            " not 'spawned' (finalized rows are never reverted)"
        )


def finalize_process(
    conn: sqlite3.Connection,
    process_id: int,
    *,
    state: str,
    exit_code: int | None,
    ended_at: str,
    session_id: str | None = None,
) -> None:
    """Finalize a registry row. ``session_id`` (CCR-1): the CLI session id captured
    from the NDJSON stream — written only when provided; ``None`` leaves any
    previously recorded session id unchanged (never clobbered to NULL)."""
    if session_id is None:
        cur = conn.execute(
            "UPDATE process_registry SET state = ?, exit_code = ?, ended_at = ?"
            " WHERE id = ?",
            (state, exit_code, ended_at, process_id),
        )
    else:
        cur = conn.execute(
            "UPDATE process_registry SET state = ?, exit_code = ?, ended_at = ?,"
            " session_id = ? WHERE id = ?",
            (state, exit_code, ended_at, session_id, process_id),
        )
    if cur.rowcount != 1:
        raise FactoryError(f"unknown process id: {process_id}")


def heartbeat_process(conn: sqlite3.Connection, process_id: int, at: str) -> None:
    cur = conn.execute(
        "UPDATE process_registry SET heartbeat_at = ? WHERE id = ?", (at, process_id)
    )
    if cur.rowcount != 1:
        raise FactoryError(f"unknown process id: {process_id}")


def processes_in_state(conn: sqlite3.Connection, state: str) -> list[ProcessRecord]:
    rows = conn.execute(
        "SELECT * FROM process_registry WHERE state = ? ORDER BY id", (state,)
    ).fetchall()
    return [_process_from_row(row) for row in rows]


def last_session_id(
    conn: sqlite3.Connection, *, unit_level: str, unit_id: str, role: str
) -> str | None:
    """Latest non-NULL session_id among FINALIZED processes of that unit+role —
    continue_session support across restarts (CCR-1). Finalized = the row went
    through ``finalize_process`` (``ended_at`` set); in-flight rows never feed a
    resume. Latest = highest registry id (AUTOINCREMENT, insertion-ordered)."""
    row = conn.execute(
        "SELECT session_id FROM process_registry"
        " WHERE unit_level = ? AND unit_id = ? AND role = ?"
        " AND ended_at IS NOT NULL AND session_id IS NOT NULL"
        " ORDER BY id DESC LIMIT 1",
        (unit_level, unit_id, role),
    ).fetchone()
    return None if row is None else row["session_id"]


# -------------------------------------------------------- repository: token ledger


def insert_token_usage(
    conn: sqlite3.Connection,
    *,
    process_id: int,
    unit_level: str,
    unit_id: str,
    role: str,
    model: str,
    tokens_in: int | None,
    tokens_out: int | None,
    cost_usd: float | None,
    estimated: bool = False,
) -> None:
    """Insert one ledger row. ``estimated=True`` (CCR-1) marks rows filled by the
    ``budgets.usage_missing_policy='estimate'`` estimator (token_ledger.estimated=1);
    estimated rows count toward the §2 context_budget total like any other."""
    conn.execute(
        "INSERT INTO token_ledger (process_id, unit_level, unit_id, role, model,"
        " tokens_in, tokens_out, cost_usd, estimated, recorded_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            process_id,
            unit_level,
            unit_id,
            role,
            model,
            tokens_in,
            tokens_out,
            cost_usd,
            int(estimated),
            utc_now(),
        ),
    )


def unit_token_total(conn: sqlite3.Connection, unit_level: str, unit_id: str) -> int:
    """Per-aggregate COALESCE (§2): an all-NULL column reads 0, never NULL —
    an unreported-usage unit must still be able to reach its budget cap."""
    row = conn.execute(
        "SELECT COALESCE(SUM(tokens_in), 0) + COALESCE(SUM(tokens_out), 0)"
        " FROM token_ledger WHERE unit_level = ? AND unit_id = ?",
        (unit_level, unit_id),
    ).fetchone()
    return int(row[0])


#: A process run that FAILED and delivered nothing — its token spend is EXCLUDED
#: from EFFECTIVE consumption (founder 20-06: the budget applies to effective,
#: not total). Maps the founder's own words: „picat” = exit_code != 0; „omorât” =
#: killed; „expirat” = timed_out (+ orphaned). An exit-0 declared-failure run is
#: treated as DELIVERED — not DB-distinguishable, rarer, and visible per-run in
#: /costuri. Running/spawned (in-flight) and clean exit-0 rows COUNT — a live
#: runaway must still reach its cap. The SINGLE source of the predicate (§9).
_FAILED_RUN_SQL = (
    "(pr.state IN ('timed_out', 'killed', 'orphaned')"
    " OR (pr.state = 'exited' AND COALESCE(pr.exit_code, 0) <> 0))"
)


def effective_token_sum(
    conn: sqlite3.Connection,
    unit_level: str,
    unit_id: str,
    *,
    exclude_role: str | None = None,
) -> int:
    """EFFECTIVE token consumption for one unit (founder 20-06): the per-aggregate
    COALESCE sum (§2) MINUS the spend of agent runs that FAILED and delivered
    nothing (``_FAILED_RUN_SQL``). INNER JOIN process_registry on the NOT-NULL
    ``token_ledger.process_id`` FK. ``exclude_role`` drops a role (the §2
    context-budget decision_session carve-out). Counterpart of
    ``unit_token_total`` (the TOTAL): the two render side by side (never merged)
    and the §2 budget trigger sums EFFECTIVE."""
    params: list = [unit_level, unit_id]
    role_clause = ""
    if exclude_role is not None:
        role_clause = " AND tl.role <> ?"
        params.append(exclude_role)
    row = conn.execute(
        "SELECT COALESCE(SUM(tl.tokens_in), 0) + COALESCE(SUM(tl.tokens_out), 0)"
        " FROM token_ledger tl JOIN process_registry pr ON pr.id = tl.process_id"
        f" WHERE tl.unit_level = ? AND tl.unit_id = ?{role_clause}"
        f" AND NOT {_FAILED_RUN_SQL}",
        params,
    ).fetchone()
    return int(row[0])


def get_runtime_settings(conn: sqlite3.Connection) -> dict[str, object]:
    """All live runtime overrides as key -> decoded JSON value (founder dashboard,
    20-06; empty when unset). The dashboard writes via ``set_runtime_setting``; the
    scheduler reads this each tick and wraps it in ``runtime_settings.EffectiveConfig``
    to layer the founder's live edits over the load-once config."""
    return {
        row["key"]: json.loads(row["value"])
        for row in conn.execute("SELECT key, value FROM runtime_settings").fetchall()
    }


def set_runtime_setting(
    conn: sqlite3.Connection, key: str, value: object, *, updated_by: str, at: str
) -> None:
    """Upsert one runtime override (``value`` JSON-encoded). Pure SQL — the caller
    records the audit event (``runtime_setting_changed``), keeping the business
    rule out of the storage layer."""
    conn.execute(
        "INSERT INTO runtime_settings (key, value, updated_at, updated_by)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET"
        " value = excluded.value, updated_at = excluded.updated_at,"
        " updated_by = excluded.updated_by",
        (key, json.dumps(value), at, updated_by),
    )


def list_token_ledger(
    conn: sqlite3.Connection, unit_level: str, unit_id: str
) -> list[sqlite3.Row]:
    """All ledger rows for one unit — the §11 per-agent cost table source
    (dashboard design §11.3.2, CCR-10). Ordered by ledger ``id`` (insertion
    order): ``recorded_at`` is second-precision and ties are real (review F7) —
    ``recorded_at`` is displayed, ``id`` orders. LEFT JOINs process_registry on
    process_id for the run's timing (proc_spawned_at/proc_ended_at — founder
    per-agent start/finish/duration, 20-06) and outcome (proc_state/exit_code —
    the effective-tokens 'failed, delivered nothing' marker). Pure SQL."""
    return conn.execute(
        "SELECT tl.id, tl.process_id, tl.role, tl.model, tl.tokens_in,"
        " tl.tokens_out, tl.cost_usd, tl.estimated, tl.recorded_at,"
        " pr.spawned_at AS proc_spawned_at, pr.ended_at AS proc_ended_at,"
        " pr.state AS proc_state, pr.exit_code AS proc_exit_code"
        " FROM token_ledger tl"
        " LEFT JOIN process_registry pr ON pr.id = tl.process_id"
        " WHERE tl.unit_level = ? AND tl.unit_id = ? ORDER BY tl.id",
        (unit_level, unit_id),
    ).fetchall()


def sum_token_cost(
    conn: sqlite3.Connection, *, since: str | None = None
) -> list[sqlite3.Row]:
    """Cost aggregate per (unit_level, unit_id, model) — the §11 summary source
    (dashboard design §11.3.2, CCR-10). ``exact_usd`` sums the CLI-reported
    costs (NULL when the group has none — exact-where-reported precedence);
    ``est_tokens_in``/``est_tokens_out`` sum ONLY NULL-cost rows (the inputs of
    the config-price estimation); ``null_cost_rows`` counts them, so a model
    missing from ``pricing`` renders the explicit missing-price marker, never a
    silent zero (Doctrine §7). Optional ``since`` bounds ``recorded_at``
    (ISO-UTC, inclusive) — the „Astăzi” founder-TZ-midnight cut (review F5)."""
    where = " WHERE recorded_at >= ?" if since is not None else ""
    params: tuple = (since,) if since is not None else ()
    return conn.execute(
        "SELECT unit_level, unit_id, model,"
        " SUM(CASE WHEN cost_usd IS NOT NULL THEN cost_usd END) AS exact_usd,"
        " COALESCE(SUM(CASE WHEN cost_usd IS NULL THEN COALESCE(tokens_in, 0) END), 0)"
        " AS est_tokens_in,"
        " COALESCE(SUM(CASE WHEN cost_usd IS NULL THEN COALESCE(tokens_out, 0) END), 0)"
        " AS est_tokens_out,"
        " SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS null_cost_rows"
        f" FROM token_ledger{where} GROUP BY unit_level, unit_id, model",
        params,
    ).fetchall()


# ------------------------------------------------- repository: fix loops and churn


def insert_fix_iteration(
    conn: sqlite3.Connection,
    stage_id: str,
    failing_tests: int,
    report_artifact_id: int | None,
) -> int:
    """Insert the next 1-based iteration row for the stage; returns the iteration.
    Atomic with its surrounding writes when called inside Database.transaction()."""
    row = conn.execute(
        "SELECT COALESCE(MAX(iteration), 0) + 1 FROM fix_iterations WHERE stage_id = ?",
        (stage_id,),
    ).fetchone()
    iteration = int(row[0])
    conn.execute(
        "INSERT INTO fix_iterations (stage_id, iteration, failing_tests,"
        " report_artifact_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (stage_id, iteration, failing_tests, report_artifact_id, utc_now()),
    )
    return iteration


def bump_churn(conn: sqlite3.Connection, stage_id: str, file_path: str, region: int) -> int:
    """Increment the (stage, file, region) edit counter; returns the new count."""
    row = conn.execute(
        "INSERT INTO churn (stage_id, file_path, region, edit_count, updated_at)"
        " VALUES (?, ?, ?, 1, ?)"
        " ON CONFLICT (stage_id, file_path, region)"
        " DO UPDATE SET edit_count = edit_count + 1, updated_at = excluded.updated_at"
        " RETURNING edit_count",
        (stage_id, file_path, region, utc_now()),
    ).fetchone()
    return int(row[0])


# ------------------------------------------------------ repository: consultations

_CONSULTATION_COLUMNS = frozenset(
    {
        "cp_id",
        "unit_level",
        "unit_id",
        "input_digest",
        "schema_valid",
        "fallback_used",
        "verdict",
        "rationale",
        "model",
        "latency_ms",
        "cost_usd",
        "tokens_in",
        "tokens_out",
        "raw_log_path",
        "created_at",
    }
)


def insert_consultation(conn: sqlite3.Connection, row: Mapping[str, object]) -> int:
    """Insert one CP call log row (DoD §3.4). Keys must be consultations columns
    (id excluded); created_at defaults to now. Unknown keys fail explicitly."""
    data = dict(row)
    unknown = set(data) - _CONSULTATION_COLUMNS
    if unknown:
        raise FactoryError(f"unknown consultations column(s): {sorted(unknown)}")
    data.setdefault("created_at", utc_now())
    columns = sorted(data)
    placeholders = ", ".join("?" * len(columns))
    cur = conn.execute(
        f"INSERT INTO consultations ({', '.join(columns)}) VALUES ({placeholders})",  # noqa: S608
        tuple(data[c] for c in columns),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


# -------------------------------------------------------- repository: escalations


def _escalation_from_row(row: sqlite3.Row) -> Escalation:
    return Escalation(
        id=row["id"],
        unit_level=row["unit_level"],
        unit_id=row["unit_id"],
        trigger=row["trigger"],
        target=row["target"],
        payload_artifact_id=row["payload_artifact_id"],
        event_seq=row["event_seq"],
        status=row["status"],
        resolution=row["resolution"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )


def insert_escalation(conn: sqlite3.Connection, esc: Escalation) -> int:
    """Insert one escalation row. ``event_seq`` is the §2 dedup cursor of the
    always-fire sentinel triggers — written at insert (CCR-1), read back by
    ``open_escalation`` and the MAX(event_seq) cursor scans."""
    cur = conn.execute(
        "INSERT INTO escalations (unit_level, unit_id, trigger, target,"
        " payload_artifact_id, event_seq, status, resolution, created_at, resolved_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            esc.unit_level,
            esc.unit_id,
            esc.trigger,
            esc.target,
            esc.payload_artifact_id,
            esc.event_seq,
            esc.status,
            esc.resolution,
            esc.created_at,
            esc.resolved_at,
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def resolve_escalation(conn: sqlite3.Connection, esc_id: int, resolution: str) -> None:
    """Resolve an OPEN escalation; resolving a missing or already-resolved row is a bug."""
    cur = conn.execute(
        "UPDATE escalations SET status = 'resolved', resolution = ?, resolved_at = ?"
        " WHERE id = ? AND status = 'open'",
        (resolution, utc_now(), esc_id),
    )
    if cur.rowcount != 1:
        raise FactoryError(f"no open escalation with id {esc_id}")


def open_escalation(
    conn: sqlite3.Connection, unit_level: str, unit_id: str, trigger: str
) -> Escalation | None:
    row = conn.execute(
        "SELECT * FROM escalations WHERE unit_level = ? AND unit_id = ? AND trigger = ?"
        " AND status = 'open'",
        (unit_level, unit_id, str(trigger)),
    ).fetchone()
    return None if row is None else _escalation_from_row(row)


def list_escalations_by_status(
    conn: sqlite3.Connection, status: str, *, older_than_min: int | None = None
) -> list[Escalation]:
    """Escalations in ``status``; with ``older_than_min``, only those whose age
    exceeds the threshold. The age clock is ``created_at`` for ``open`` and
    ``resolved_at`` for ``resolved`` (the stuck-detector's two read predicates,
    robustness UNIT 2). Mirrors ``pending_decisions(unalerted_older_than_h=…)``."""
    if older_than_min is None:
        rows = conn.execute(
            "SELECT * FROM escalations WHERE status = ? ORDER BY id",
            (status,),
        ).fetchall()
    else:
        # 'resolved' ages by resolved_at (when the fix landed but the unit never
        # advanced); everything else by created_at (open-too-long). Don't confuse
        # the two clocks (design §UNIT 2 foot-gun 2).
        age_column = "resolved_at" if status == "resolved" else "created_at"
        cutoff = (datetime.now(UTC) - timedelta(minutes=older_than_min)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"  # same format as models.utc_now
        )
        rows = conn.execute(
            f"SELECT * FROM escalations WHERE status = ? AND {age_column} < ? ORDER BY id",
            (status, cutoff),
        ).fetchall()
    return [_escalation_from_row(row) for row in rows]


def bump_escalation_target(
    conn: sqlite3.Connection, esc_id: int, new_target: str
) -> None:
    """Re-label an escalation's routing ``target`` (robustness UNIT 2 escalate-UP).
    Target ONLY — status/resolution/timestamps untouched (the detector NEVER
    resolves or transitions; the founder's mechanical-only mandate). Mirrors
    ``resolve_escalation``'s single-row guard."""
    cur = conn.execute(
        "UPDATE escalations SET target = ? WHERE id = ?",
        (new_target, esc_id),
    )
    if cur.rowcount != 1:
        raise FactoryError(f"no escalation with id {esc_id}")


def latest_escalation_ids_by_unit(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], int]:
    """``(unit_level, unit_id) -> MAX(escalation id)`` across ALL statuses — the
    id of each unit's MOST-RECENT escalation (ids are monotonic, so max id == latest).

    The stuck-detector's (2b) resolved-not-advanced check fires ONLY for a unit's
    most-recent escalation (case-2b over-fire fix, ETAPA-5f). An OLDER resolved
    escalation of a unit re-ESCALATED for a NEWER reason is superseded by that newer
    escalation (an open one -> covered by (2a)/first-notice; another resolved one ->
    that one is the live episode), NOT a genuine stuck-resolved. Without this scope,
    EVERY old resolved escalation of a currently-ESCALATED unit matched
    ``resolved + older_than_threshold + unit ESCALATED`` and paged once each — a
    flood (~32 false [arhitect] pages observed in production, register-schemas with
    a 4-resolution history re-ESCALATED on a new budget breach)."""
    rows = conn.execute(
        "SELECT unit_level, unit_id, MAX(id) AS max_id FROM escalations "
        "GROUP BY unit_level, unit_id"
    ).fetchall()
    return {(r["unit_level"], r["unit_id"]): int(r["max_id"]) for r in rows}


# ----------------------------------------------------------- repository: findings


def _finding_from_row(row: sqlite3.Row) -> Finding:
    return Finding(
        id=row["id"],
        stage_id=row["stage_id"],
        auditor_role=row["auditor_role"],
        finding_ref=row["finding_ref"],
        severity=row["severity"],
        report_artifact_id=row["report_artifact_id"],
        status=row["status"],
        contest_artifact_id=row["contest_artifact_id"],
        resolved_by=row["resolved_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def insert_finding(conn: sqlite3.Connection, f: Finding) -> int:
    cur = conn.execute(
        "INSERT INTO audit_findings (stage_id, auditor_role, finding_ref, severity,"
        " report_artifact_id, status, contest_artifact_id, resolved_by, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f.stage_id,
            f.auditor_role,
            f.finding_ref,
            f.severity,
            f.report_artifact_id,
            f.status,
            f.contest_artifact_id,
            f.resolved_by,
            f.created_at,
            f.updated_at,
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def set_finding_status(
    conn: sqlite3.Connection,
    finding_id: int,
    status: str,
    *,
    resolved_by: str | None = None,
    contest_artifact_id: int | None = None,
) -> None:
    """Update finding status (+ updated_at); resolved_by / contest_artifact_id are
    written only when provided (None = leave unchanged)."""
    sets = ["status = ?", "updated_at = ?"]
    params: list[object] = [status, utc_now()]
    if resolved_by is not None:
        sets.append("resolved_by = ?")
        params.append(resolved_by)
    if contest_artifact_id is not None:
        sets.append("contest_artifact_id = ?")
        params.append(contest_artifact_id)
    params.append(finding_id)
    cur = conn.execute(
        f"UPDATE audit_findings SET {', '.join(sets)} WHERE id = ?",  # noqa: S608
        params,
    )
    if cur.rowcount != 1:
        raise FactoryError(f"unknown finding id: {finding_id}")


def findings(
    conn: sqlite3.Connection, stage_id: str, statuses: Sequence[str] = ()
) -> list[Finding]:
    sql = "SELECT * FROM audit_findings WHERE stage_id = ?"
    params: list[object] = [stage_id]
    if statuses:
        placeholders = ", ".join("?" * len(statuses))
        sql += f" AND status IN ({placeholders})"
        params.extend(str(s) for s in statuses)
    sql += " ORDER BY id"
    return [_finding_from_row(row) for row in conn.execute(sql, params).fetchall()]


def prior_disposed_finding(
    conn: sqlite3.Connection, stage_id: str, finding_ref: str, auditor_role: str
) -> str | None:
    """D-0059 recurrence signal (architect-operations §1): the most-recent SETTLED
    or OVERRULED disposition of ``(stage_id, finding_ref, auditor_role)``, or None.
    A NEW audit finding whose ref the SAME auditor already settled/overruled on this
    stage means the root was not actually fixed — the caller emits a
    'finding_recurrence' event. The auditor_role is part of the match: finding_ref
    is report-scoped (not stage-unique), so two different auditors reusing one ref
    string (e.g. 'F-1') are DISTINCT findings, not a recurrence."""
    row = conn.execute(
        "SELECT status FROM audit_findings WHERE stage_id = ? AND finding_ref = ?"
        " AND auditor_role = ? AND status IN ('settled', 'overruled')"
        " ORDER BY id DESC LIMIT 1",
        (stage_id, finding_ref, auditor_role),
    ).fetchone()
    return row["status"] if row else None


# ----------------------------------------------------------- repository: decisions


def _decision_from_row(row: sqlite3.Row) -> DecisionRequest:
    return DecisionRequest(
        id=row["id"],
        unit_level=row["unit_level"],
        unit_id=row["unit_id"],
        gate_kind=row["gate_kind"],
        request_artifact_id=row["request_artifact_id"],
        status=row["status"],
        answer=row["answer"],
        answer_artifact_id=row["answer_artifact_id"],
        created_at=row["created_at"],
        alerted_at=row["alerted_at"],
        answered_at=row["answered_at"],
    )


def insert_decision_request(conn: sqlite3.Connection, dr: DecisionRequest) -> int:
    cur = conn.execute(
        "INSERT INTO decision_requests (unit_level, unit_id, gate_kind,"
        " request_artifact_id, status, answer, answer_artifact_id, created_at,"
        " alerted_at, answered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            dr.unit_level,
            dr.unit_id,
            dr.gate_kind,
            dr.request_artifact_id,
            dr.status,
            dr.answer,
            dr.answer_artifact_id,
            dr.created_at,
            dr.alerted_at,
            dr.answered_at,
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def answer_decision(
    conn: sqlite3.Connection, request_id: int, answer: str, answer_artifact_id: int | None
) -> None:
    """Answer a PENDING decision; answering a missing or already-answered one is a bug."""
    cur = conn.execute(
        "UPDATE decision_requests SET status = 'answered', answer = ?,"
        " answer_artifact_id = ?, answered_at = ? WHERE id = ? AND status = 'pending'",
        (answer, answer_artifact_id, utc_now(), request_id),
    )
    if cur.rowcount != 1:
        raise FactoryError(f"no pending decision request with id {request_id}")


def mark_decision_alerted(conn: sqlite3.Connection, request_id: int, at: str) -> None:
    """Set alerted_at after a successful publish (CCR-1) — an alerted row stops
    matching the §2 decision-latency query, so the alert never re-fires every tick.
    Marking a nonexistent request is a control-plane bug."""
    cur = conn.execute(
        "UPDATE decision_requests SET alerted_at = ? WHERE id = ?", (at, request_id)
    )
    if cur.rowcount != 1:
        raise FactoryError(f"unknown decision request id: {request_id}")


def mark_decision_published(conn: sqlite3.Connection, request_id: int, at: str) -> None:
    """Set published_at after a SUCCESSFUL decision publish (founder 20-06): a
    published row stops matching ``pending_unpublished_decisions``, so the per-tick
    re-publish backstop never re-pages a delivered decision. DISTINCT from
    ``mark_decision_alerted`` (the 24h latency latch) — the two are independent.
    Marking a nonexistent request is a control-plane bug."""
    cur = conn.execute(
        "UPDATE decision_requests SET published_at = ? WHERE id = ?", (at, request_id)
    )
    if cur.rowcount != 1:
        raise FactoryError(f"unknown decision request id: {request_id}")


def pending_unpublished_decisions(conn: sqlite3.Connection) -> list[DecisionRequest]:
    """Pending decisions whose page was never delivered (published_at IS NULL) —
    the per-tick re-publish backstop's worklist (founder 20-06). A transient ntfy
    429 leaves published_at NULL, so the page is retried each tick until it lands;
    a successful publish sets published_at and drops the row from this set."""
    rows = conn.execute(
        "SELECT * FROM decision_requests WHERE status = 'pending'"
        " AND published_at IS NULL ORDER BY id"
    ).fetchall()
    return [_decision_from_row(row) for row in rows]


def pending_decisions(
    conn: sqlite3.Connection, *, unalerted_older_than_h: int | None = None
) -> list[DecisionRequest]:
    """All pending decisions; with unalerted_older_than_h, only those never alerted
    and created more than that many hours ago (§2 decision-latency trigger)."""
    if unalerted_older_than_h is None:
        rows = conn.execute(
            "SELECT * FROM decision_requests WHERE status = 'pending' ORDER BY id"
        ).fetchall()
    else:
        cutoff = (datetime.now(UTC) - timedelta(hours=unalerted_older_than_h)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"  # same format as models.utc_now
        )
        rows = conn.execute(
            "SELECT * FROM decision_requests WHERE status = 'pending'"
            " AND alerted_at IS NULL AND created_at < ? ORDER BY id",
            (cutoff,),
        ).fetchall()
    return [_decision_from_row(row) for row in rows]
