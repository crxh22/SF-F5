"""DoD §8 mechanical escalation triggers (design §1/§2/§4): counter recording +
SQL evaluation. Decides nothing beyond firing.

Triggers fire from orchestrator-measured signals, never from agent
self-assessment or anyone's attentiveness (Doctrine §20). What happens to a
firing belongs to the caller: the reset-vs-escalate choice on ``context_budget``
(§2, bounded by ``escalation.max_context_resets``) and the threshold-then-CP-1
routing (§3.1) live in the executor, not here.

Every §8 value is read from ``FactoryConfig`` by key — ``escalation.max_fix_iterations``,
``escalation.churn_threshold``, ``escalation.churn_region_lines``,
``escalation.max_context_resets``, ``budgets.per_stage`` — never hardcoded
(Doctrine §14).

May import: models, config, db (+ stdlib) — design §1.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator

from sf_factory.config import FactoryConfig
from sf_factory.db import (
    Database,
    bump_churn,
    effective_token_sum,
    insert_fix_iteration,
    open_escalation,
)
from sf_factory.models import (
    ConfigError,
    FactoryError,
    Level,
    Stage,
    Trigger,
    TriggerFiring,
    ValidationSummary,
)

# ------------------------------------------------------------- §2 trigger SQL
#
# max_fix_iterations — both §2 corrections are structural here:
# 1. The window is scoped to iterations recorded AFTER the last RESOLVED
#    max_fix_iterations escalation for the stage (created_at > MAX(resolved_at),
#    COALESCE'd to '' so an un-escalated stage keeps its full history): the
#    trigger re-arms cleanly after rework instead of firing at most once per
#    stage lifetime.
# 2. LAG is computed AFTER the LIMIT subset, so ``prev`` is window-local.
#    LAG-before-LIMIT would give every window row a non-NULL predecessor once
#    history exceeds :n — silencing full stagnation (n comparisons, never n-1)
#    and firing on progress (the boundary pair absorbs the in-window decrease).
_FIX_WINDOW_CTE = """
WITH win AS (
  SELECT iteration, failing_tests
  FROM fix_iterations
  WHERE stage_id = :s
    AND created_at > COALESCE(
      (SELECT MAX(resolved_at) FROM escalations
       WHERE unit_level = 'stage' AND unit_id = :s
         AND "trigger" = 'max_fix_iterations' AND status = 'resolved'),
      '')
  ORDER BY iteration DESC
  LIMIT :n
)
"""

_MAX_FIX_ITERATIONS_SQL = _FIX_WINDOW_CTE + """
SELECT
  (SELECT COUNT(*) FROM (
     SELECT failing_tests,
            LAG(failing_tests) OVER (ORDER BY iteration) AS prev
     FROM win)
   WHERE prev IS NOT NULL AND failing_tests >= prev)              AS nondecreasing_pairs,
  (SELECT COUNT(*) FROM win)                                      AS window_rows,
  (SELECT failing_tests FROM win ORDER BY iteration DESC LIMIT 1) AS latest_failing
"""

_FIX_WINDOW_ROWS_SQL = _FIX_WINDOW_CTE + """
SELECT iteration, failing_tests FROM win ORDER BY iteration
"""

# churn_threshold — §2 verbatim (ORDER BY added only for deterministic evidence).
_CHURN_SQL = """
SELECT file_path, region, edit_count
FROM churn
WHERE stage_id = :s AND edit_count >= :threshold
ORDER BY file_path, region
"""

# Always-fire sentinels (contract_change_request / agent_declared_failure) —
# §2 verbatim: an event is covered once an escalation records a cursor
# (escalations.event_seq) at or beyond its seq, REGARDLESS of escalation
# status — each sentinel event escalates exactly once, and a new sentinel
# written after rework is a new event and fires again by design (§5.4).
_SENTINEL_SQL = """
SELECT seq
FROM events
WHERE unit_level = :l AND unit_id = :s AND event_type = :event_type
  AND seq > COALESCE(
    (SELECT MAX(event_seq) FROM escalations
     WHERE unit_level = :l AND unit_id = :s AND "trigger" = :trigger),
    0)
ORDER BY seq
"""

# context_budget evidence: count of state-preserving resets already executed —
# the caller compares it against escalation.max_context_resets (§2).
_CONTEXT_RESETS_SQL = """
SELECT COUNT(*)
FROM events
WHERE unit_level = 'stage' AND unit_id = :s AND event_type = 'context_reset'
"""

#: Runner role of orchestrator-mediated founder Decision Sessions — a config
#: models.* key referenced by name (the CP1_ID pattern). The §2 context_budget
#: trigger excludes its ledger rows (CCR-3/D-0017, OPEN-D4): the cap governs the
#: conveyor, not founder conversation; dashboard burn figures still sum everything
#: (db.unit_token_total is unchanged).
_DECISION_SESSION_ROLE = "decision_session"

# §2 context_budget sum — per-aggregate COALESCE (an all-NULL column reads 0,
# never NULLing the total), MINUS role='decision_session' rows (CCR-3 amendment).
_CONTEXT_BUDGET_TOKENS_SQL = """
SELECT COALESCE(SUM(tokens_in), 0) + COALESCE(SUM(tokens_out), 0)
FROM token_ledger
WHERE unit_level = 'stage' AND unit_id = :s AND role <> :excluded_role
"""

# ------------------------------------------------------- unified-diff parsing

#: Hunk header: ``@@ -<old>[,<n>] +<new>[,<n>] @@[ context]`` — group 1 is the
#: NEW-side start line, the churn bucket input (§2 DDL: region = start line //
#: escalation.churn_region_lines).
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _diff_path(raw: str, prefix: str) -> str | None:
    """Path from a ``--- ``/``+++ `` file-header line body.

    Strips a trailing tab field, surrounding git C-quoting (quotes only — the
    bucket key just has to be stable per file), and the side's ``a/``/``b/``
    prefix. ``/dev/null`` (file added/deleted) returns None.
    """
    path = raw.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    if path.startswith(prefix):
        path = path[len(prefix):]
    return path


def _iter_hunks(diff_text: str) -> Iterator[tuple[str, int]]:
    """Yield ``(file_path, new_start_line)`` per hunk of a unified diff.

    File attribution follows git's structure: ``--- ``/``+++ `` lines are file
    headers only between a ``diff --git`` line and that file's first hunk —
    content lines inside hunks that happen to start with ``+++``/``---`` are
    never misread as headers. A hunk header that cannot be attributed to a file
    (or cannot be parsed) raises ``FactoryError``: a silently skipped hunk
    would silently under-count churn (Doctrine §7/§20).
    """
    current_file: str | None = None
    source_path: str | None = None
    in_file_header = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            in_file_header = True
            current_file = None
            source_path = None
            continue
        if in_file_header and line.startswith("--- "):
            source_path = _diff_path(line[4:], "a/")
            continue
        if in_file_header and line.startswith("+++ "):
            target_path = _diff_path(line[4:], "b/")
            current_file = target_path if target_path is not None else source_path
            continue
        if line.startswith("@@"):
            match = _HUNK_HEADER_RE.match(line)
            if match is None:
                raise FactoryError(f"malformed unified-diff hunk header: {line!r}")
            if current_file is None:
                raise FactoryError(
                    f"unified-diff hunk header without a preceding file header: {line!r}"
                )
            in_file_header = False
            yield current_file, int(match.group(1))


# ------------------------------------------------------------------- evaluator


class ThresholdEvaluator:
    """DoD §8 mechanical triggers: counter recording + §2 SQL evaluation."""

    def __init__(self, db: Database, cfg: FactoryConfig) -> None:
        """Binds §8 config values to SQL."""
        self._db = db
        self._cfg = cfg

    def record_validation(
        self,
        conn: sqlite3.Connection,
        stage_id: str,
        summary: ValidationSummary,
        report_artifact_id: int | None,
    ) -> int:
        """Insert next fix_iterations row inside the caller's tx (coupled with the
        VALIDATE transition); returns iteration."""
        return insert_fix_iteration(conn, stage_id, summary.failing, report_artifact_id)

    def record_churn(self, conn: sqlite3.Connection, stage_id: str, diff_text: str) -> None:
        """Parse unified-diff hunk headers; bump churn per
        (file, start_line // churn_region_lines) bucket."""
        region_lines = self._cfg.escalation.churn_region_lines
        for file_path, start_line in _iter_hunks(diff_text):
            bump_churn(conn, stage_id, file_path, start_line // region_lines)

    def evaluate(self, stage: Stage) -> list[TriggerFiring]:
        """Run the §2 trigger SQL set; return firings not yet covered by an open
        escalation. Pure reads.

        A trigger with an OPEN escalation for this stage never re-fires
        (``uq_open_escalation`` allows exactly one open row per trigger, so an
        uncovered firing is always insertable by the caller); sentinel triggers
        additionally dedup via the ``escalations.event_seq`` cursor, so a
        resolved escalation never re-fires for the events it covered, while
        still-uncovered newer sentinel events fire again after resolution.
        Firing order is the ``models.Trigger`` declaration (= §2 enumeration)
        order — deterministic for the caller and the tests.
        """
        conn = self._db.read()
        checks = (
            (Trigger.MAX_FIX_ITERATIONS, self._check_max_fix_iterations),
            (Trigger.CHURN_THRESHOLD, self._check_churn_threshold),
            (Trigger.CONTRACT_CHANGE_REQUEST, self._check_contract_change_request),
            (Trigger.AGENT_DECLARED_FAILURE, self._check_agent_declared_failure),
            (Trigger.CONTEXT_BUDGET, self._check_context_budget),
        )
        firings: list[TriggerFiring] = []
        for trigger, check in checks:
            if open_escalation(conn, Level.STAGE.value, stage.id, trigger.value) is not None:
                continue
            evidence = check(conn, stage)
            if evidence is not None:
                firings.append(
                    TriggerFiring(
                        trigger=trigger,
                        unit_level=Level.STAGE.value,
                        unit_id=stage.id,
                        evidence=evidence,
                    )
                )
        return firings

    # ------------------------------------------------------- per-trigger checks
    # Each returns the evidence dict (the SQL row(s) that fired) or None.

    def _check_max_fix_iterations(
        self, conn: sqlite3.Connection, stage: Stage
    ) -> dict | None:
        """§2: the last :n in-window iterations show no decrease and tests still fail."""
        n = self._cfg.escalation.max_fix_iterations
        params = {"s": stage.id, "n": n}
        row = conn.execute(_MAX_FIX_ITERATIONS_SQL, params).fetchone()
        window_rows = int(row["window_rows"])
        if window_rows < n:
            return None  # fewer than :n in-window iterations
        if int(row["nondecreasing_pairs"]) != n - 1:
            return None  # at least one in-window decrease — progress, no firing
        latest_failing = row["latest_failing"]
        if latest_failing is None or int(latest_failing) <= 0:
            return None  # tests pass — stagnation at zero is success, not a loop
        window = conn.execute(_FIX_WINDOW_ROWS_SQL, params).fetchall()
        return {
            "iterations": [
                {"iteration": int(r["iteration"]), "failing_tests": int(r["failing_tests"])}
                for r in window
            ],
            "max_fix_iterations": n,
        }

    def _check_churn_threshold(self, conn: sqlite3.Connection, stage: Stage) -> dict | None:
        """§2: any (file, region) bucket edited escalation.churn_threshold+ times."""
        threshold = self._cfg.escalation.churn_threshold
        rows = conn.execute(_CHURN_SQL, {"s": stage.id, "threshold": threshold}).fetchall()
        if not rows:
            return None
        return {
            "regions": [
                {
                    "file_path": r["file_path"],
                    "region": int(r["region"]),
                    "edit_count": int(r["edit_count"]),
                }
                for r in rows
            ],
            "churn_threshold": threshold,
        }

    def _check_contract_change_request(
        self, conn: sqlite3.Connection, stage: Stage
    ) -> dict | None:
        return self._check_sentinel(
            conn, stage, "contract_change_request", Trigger.CONTRACT_CHANGE_REQUEST
        )

    def _check_agent_declared_failure(
        self, conn: sqlite3.Connection, stage: Stage
    ) -> dict | None:
        return self._check_sentinel(
            conn, stage, "declared_failure", Trigger.AGENT_DECLARED_FAILURE
        )

    def _check_sentinel(
        self, conn: sqlite3.Connection, stage: Stage, event_type: str, trigger: Trigger
    ) -> dict | None:
        """§2 always-fire sentinels: events beyond the escalations.event_seq cursor.

        ``event_seq`` in the evidence is the newest uncovered seq — the value
        the caller must write into ``escalations.event_seq`` so the cursor
        advances past everything reported here (§5.4 sentinel lifecycle).
        """
        rows = conn.execute(
            _SENTINEL_SQL,
            {
                "l": Level.STAGE.value,
                "s": stage.id,
                "event_type": event_type,
                "trigger": trigger.value,
            },
        ).fetchall()
        if not rows:
            return None
        seqs = [int(r["seq"]) for r in rows]
        return {"event_seqs": seqs, "event_seq": seqs[-1]}

    def _check_context_budget(self, conn: sqlite3.Connection, stage: Stage) -> dict | None:
        """§2: per-aggregate EFFECTIVE token sum >= budgets.per_stage[risk_class].

        EFFECTIVE (founder 20-06, [[dashboard-mandate]]) = the COALESCE sum MINUS
        the spend of agent runs that FAILED and delivered nothing (db._FAILED_RUN_SQL):
        an infra failure (OOM-kill, timeout) must not push a stage toward a
        spurious context reset/escalation. Estimated rows count (ordinary ledger
        rows); ``role='decision_session'`` rows are EXCLUDED (CCR-3/D-0017, OPEN-D4):
        founder conversation must never count against the conveyor cap. The
        evidence carries effective + total (the gap = failed-run spend) and
        context_resets vs escalation.max_context_resets so the caller can apply the
        §2 reset-then-escalate rule mechanically.
        """
        budget = self._cfg.budgets.per_stage.get(stage.risk_class)
        if budget is None:
            raise ConfigError(
                f"stage {stage.id!r} has risk_class {stage.risk_class!r} with no "
                "budgets.per_stage entry — config/DB drift, cannot evaluate context_budget"
            )
        effective = effective_token_sum(
            conn, "stage", stage.id, exclude_role=_DECISION_SESSION_ROLE
        )
        if effective < budget:
            return None
        # Budget hit: compute the total (excl decision_session, INCL failed runs)
        # for the evidence — its gap to effective is the wasted failed-run spend.
        total = int(
            conn.execute(
                _CONTEXT_BUDGET_TOKENS_SQL,
                {"s": stage.id, "excluded_role": _DECISION_SESSION_ROLE},
            ).fetchone()[0]
        )
        resets = int(conn.execute(_CONTEXT_RESETS_SQL, {"s": stage.id}).fetchone()[0])
        return {
            "effective_tokens": effective,
            "total_tokens": total,
            "budget": budget,
            "risk_class": stage.risk_class,
            "context_resets": resets,
            "max_context_resets": self._cfg.escalation.max_context_resets,
        }
