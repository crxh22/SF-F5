"""Unit tests for sf_factory.thresholds — each §8 trigger as a pure SQL fixture
(design §2/§8).

Covers, per the §8 test strategy: the non-decreasing window at n+1 consecutive
non-decreasing iterations (the naive LAG-before-LIMIT form fails exactly there,
in both directions: silencing stagnation and firing on progress), re-arm after
a resolved escalation (window scoped by created_at > resolved_at, strictly),
churn buckets from unified-diff hunk headers, sentinel dedup via the
escalations.event_seq cursor, and all-NULL-usage budgets (per-aggregate
COALESCE).

Fixtures beyond tests/conftest.py (frozen, wave 1) are defined locally here.
created_at timestamps are seeded explicitly where the §2 window scoping
compares them against escalations.resolved_at — the repo helpers stamp
utc_now(), useless for deterministic re-arm fixtures.
"""

from __future__ import annotations

import json

import pytest

from sf_factory.config import FactoryConfig
from sf_factory.db import (
    Database,
    insert_escalation,
    insert_event,
    insert_phase,
    insert_process,
    insert_stage,
    insert_token_usage,
)
from sf_factory.models import (
    ConfigError,
    Escalation,
    FactoryError,
    Phase,
    PhaseState,
    ProcessRecord,
    Stage,
    StageState,
    Trigger,
    TriggerFiring,
    ValidationSummary,
    utc_now,
)
from sf_factory.thresholds import ThresholdEvaluator

# ------------------------------------------------------------------ local helpers


def _ts(seconds: int) -> str:
    """Deterministic ISO 8601 UTC timestamp, ``seconds`` after a fixed origin."""
    return f"2026-06-10T{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}Z"


def _seed_stage(db: Database, stage_id: str, risk_class: str = "routine") -> Stage:
    now = utc_now()
    phase = Phase(
        id=f"ph-{stage_id}",
        project="proj",
        name=f"ph-{stage_id}",
        state=PhaseState.PENDING,
        branch=None,
        plan_artifact_id=None,
        created_at=now,
        updated_at=now,
    )
    stage = Stage(
        id=stage_id,
        phase_id=phase.id,
        name=stage_id,
        risk_class=risk_class,
        state=StageState.BUILD,
        branch=None,
        worktree_path=None,
        spec_artifact_id=None,
        created_at=now,
        updated_at=now,
    )
    with db.transaction() as conn:
        insert_phase(conn, phase)
        insert_stage(conn, stage)
    return stage


def _seed_iterations(db: Database, stage_id: str, rows: list[tuple[int, int, str]]) -> None:
    """rows = [(iteration, failing_tests, created_at)] with explicit timestamps."""
    with db.transaction() as conn:
        for iteration, failing, created_at in rows:
            conn.execute(
                "INSERT INTO fix_iterations (stage_id, iteration, failing_tests,"
                " report_artifact_id, created_at) VALUES (?, ?, ?, NULL, ?)",
                (stage_id, iteration, failing, created_at),
            )


def _seed_event(db: Database, unit_id: str, event_type: str, unit_level: str = "stage") -> int:
    with db.transaction() as conn:
        return insert_event(
            conn,
            unit_level=unit_level,
            unit_id=unit_id,
            event_type=event_type,
            actor="control_plane",
        )


def _seed_escalation(
    db: Database,
    stage_id: str,
    trigger: str,
    *,
    status: str = "open",
    event_seq: int | None = None,
    resolved_at: str | None = None,
) -> int:
    esc = Escalation(
        id=None,
        unit_level="stage",
        unit_id=stage_id,
        trigger=trigger,
        target="phase_architect",
        payload_artifact_id=None,
        event_seq=event_seq,
        status=status,
        resolution="rework:BUILD" if status == "resolved" else None,
        created_at=utc_now(),
        resolved_at=resolved_at,
    )
    with db.transaction() as conn:
        return insert_escalation(conn, esc)


def _resolve(db: Database, esc_id: int, resolved_at: str) -> None:
    """Resolve with a CONTROLLED resolved_at (db.resolve_escalation stamps utc_now)."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE escalations SET status='resolved', resolution='rework:BUILD',"
            " resolved_at=? WHERE id=? AND status='open'",
            (resolved_at, esc_id),
        )


def _seed_process(db: Database, stage_id: str) -> int:
    now = utc_now()
    rec = ProcessRecord(
        id=None,
        unit_level="stage",
        unit_id=stage_id,
        kind="agent",
        role="builder_routine",
        cp_id=None,
        session_id=None,
        pid=None,
        cmdline="stub --scenario success",
        cwd=None,
        state="exited",
        exit_code=0,
        ndjson_log_path=None,
        spawned_at=now,
        heartbeat_at=None,
        ended_at=now,
    )
    with db.transaction() as conn:
        return insert_process(conn, rec)


def _seed_usage(
    db: Database,
    process_id: int,
    stage_id: str,
    tokens_in: int | None,
    tokens_out: int | None,
    *,
    estimated: bool = False,
) -> None:
    with db.transaction() as conn:
        insert_token_usage(
            conn,
            process_id=process_id,
            unit_level="stage",
            unit_id=stage_id,
            role="builder_routine",
            model="stub-model",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=None,
            estimated=estimated,
        )


def _churn_rows(db: Database, stage_id: str) -> dict[tuple[str, int], int]:
    rows = db.read().execute(
        "SELECT file_path, region, edit_count FROM churn WHERE stage_id = ?", (stage_id,)
    ).fetchall()
    return {(r["file_path"], r["region"]): r["edit_count"] for r in rows}


def _triggers(firings: list[TriggerFiring]) -> list[Trigger]:
    return [f.trigger for f in firings]


def _diff(path: str, starts: list[int]) -> str:
    """Minimal unified diff for one file with hunks at the given new-side start lines."""
    lines = [
        f"diff --git a/{path} b/{path}",
        "index 1111111..2222222 100644",
        f"--- a/{path}",
        f"+++ b/{path}",
    ]
    for start in starts:
        lines.append(f"@@ -{start},2 +{start},3 @@ def ctx():")
        lines.append("+added line")
        lines.append(" context line")
    return "\n".join(lines) + "\n"


@pytest.fixture()
def evaluator(db: Database, factory_config: FactoryConfig) -> ThresholdEvaluator:
    return ThresholdEvaluator(db, factory_config)


# ------------------------------------------------------------- max_fix_iterations


def test_fires_after_n_nondecreasing_failing_iterations(db, evaluator, factory_config):
    n = factory_config.escalation.max_fix_iterations
    stage = _seed_stage(db, "st-fire")
    _seed_iterations(db, "st-fire", [(i + 1, 5, _ts(10 * (i + 1))) for i in range(n)])
    firings = evaluator.evaluate(stage)
    assert _triggers(firings) == [Trigger.MAX_FIX_ITERATIONS]
    firing = firings[0]
    assert (firing.unit_level, firing.unit_id) == ("stage", "st-fire")
    assert firing.evidence == {
        "iterations": [
            {"iteration": i + 1, "failing_tests": 5} for i in range(n)
        ],
        "max_fix_iterations": n,
    }


def test_does_not_fire_below_n_iterations(db, evaluator, factory_config):
    n = factory_config.escalation.max_fix_iterations
    stage = _seed_stage(db, "st-few")
    _seed_iterations(db, "st-few", [(i + 1, 5, _ts(10 * (i + 1))) for i in range(n - 1)])
    assert evaluator.evaluate(stage) == []


def test_decrease_within_window_does_not_fire(db, evaluator, factory_config):
    n = factory_config.escalation.max_fix_iterations
    stage = _seed_stage(db, "st-progress")
    failing = [5] * (n - 1) + [4]  # last pair decreases
    _seed_iterations(
        db, "st-progress", [(i + 1, f, _ts(10 * (i + 1))) for i, f in enumerate(failing)]
    )
    assert evaluator.evaluate(stage) == []


def test_n_plus_one_consecutive_nondecreasing_iterations_still_fire(
    db, evaluator, factory_config
):
    """THE corrected-§2-SQL case: with LAG computed before LIMIT, every window row
    has a non-NULL predecessor once history exceeds n — n comparisons instead of
    n-1, silencing exactly this full-stagnation case."""
    n = factory_config.escalation.max_fix_iterations
    stage = _seed_stage(db, "st-nplus1")
    _seed_iterations(db, "st-nplus1", [(i + 1, 5, _ts(10 * (i + 1))) for i in range(n + 1)])
    firings = evaluator.evaluate(stage)
    assert _triggers(firings) == [Trigger.MAX_FIX_ITERATIONS]
    # The window is the LAST n iterations (2..n+1), not the full history.
    assert [r["iteration"] for r in firings[0].evidence["iterations"]] == list(
        range(2, n + 2)
    )


def test_progress_after_long_stagnation_does_not_fire(db, evaluator, factory_config):
    """The naive form's second failure mode: the window-boundary pair (5>=5) would
    substitute for the in-window decrease and fire on PROGRESS (failing dropped)."""
    n = factory_config.escalation.max_fix_iterations
    stage = _seed_stage(db, "st-improved")
    failing = [5] * n + [1]
    _seed_iterations(
        db, "st-improved", [(i + 1, f, _ts(10 * (i + 1))) for i, f in enumerate(failing)]
    )
    assert evaluator.evaluate(stage) == []


def test_window_uses_only_the_last_n_iterations(db, evaluator, factory_config):
    """An old decrease before the window must not mask in-window stagnation."""
    n = factory_config.escalation.max_fix_iterations
    stage = _seed_stage(db, "st-window")
    failing = [9] + [5] * n  # 9 -> 5 decreased once, then stagnation for n iterations
    _seed_iterations(
        db, "st-window", [(i + 1, f, _ts(10 * (i + 1))) for i, f in enumerate(failing)]
    )
    assert _triggers(evaluator.evaluate(stage)) == [Trigger.MAX_FIX_ITERATIONS]


def test_stagnation_at_zero_failing_does_not_fire(db, evaluator, factory_config):
    n = factory_config.escalation.max_fix_iterations
    stage = _seed_stage(db, "st-green")
    _seed_iterations(db, "st-green", [(i + 1, 0, _ts(10 * (i + 1))) for i in range(n)])
    assert evaluator.evaluate(stage) == []


def test_single_iteration_window_fires_immediately(db, config_dict):
    """n=1 edge: any failing iteration is already a no-decrease window."""
    config_dict["escalation"]["max_fix_iterations"] = 1
    evaluator = ThresholdEvaluator(db, FactoryConfig.model_validate(config_dict))
    stage = _seed_stage(db, "st-n1")
    _seed_iterations(db, "st-n1", [(1, 2, _ts(10))])
    assert _triggers(evaluator.evaluate(stage)) == [Trigger.MAX_FIX_ITERATIONS]
    passing = _seed_stage(db, "st-n1-green")
    _seed_iterations(db, "st-n1-green", [(1, 0, _ts(10))])
    assert evaluator.evaluate(passing) == []


def test_open_escalation_suppresses_max_fix_firing(db, evaluator, factory_config):
    n = factory_config.escalation.max_fix_iterations
    stage = _seed_stage(db, "st-open")
    _seed_iterations(db, "st-open", [(i + 1, 5, _ts(10 * (i + 1))) for i in range(n)])
    _seed_escalation(db, "st-open", Trigger.MAX_FIX_ITERATIONS.value)
    assert evaluator.evaluate(stage) == []


def test_rearm_after_resolved_escalation(db, evaluator, factory_config):
    """§2 re-arm: the window is scoped to iterations created STRICTLY after the
    last resolved max_fix_iterations escalation — the trigger fires again on
    fresh post-rework stagnation, never on the pre-escalation history."""
    n = factory_config.escalation.max_fix_iterations
    stage = _seed_stage(db, "st-rearm")
    _seed_iterations(db, "st-rearm", [(i + 1, 5, _ts(10 * (i + 1))) for i in range(n)])
    assert _triggers(evaluator.evaluate(stage)) == [Trigger.MAX_FIX_ITERATIONS]

    esc_id = _seed_escalation(db, "st-rearm", Trigger.MAX_FIX_ITERATIONS.value)
    assert evaluator.evaluate(stage) == []  # open escalation covers the firing

    _resolve(db, esc_id, resolved_at=_ts(100))
    assert evaluator.evaluate(stage) == []  # old window excluded: cleanly re-armed

    # One iteration in the SAME second as the resolution (excluded — strict '>')
    # plus n-1 after it: only n-1 rows are in-window, so no firing yet. An
    # inclusive '>=' would see n stagnant rows here and fire early.
    _seed_iterations(
        db,
        "st-rearm",
        [(n + 1, 4, _ts(100))] + [(n + 1 + k, 4, _ts(100 + 10 * k)) for k in range(1, n)],
    )
    assert evaluator.evaluate(stage) == []

    _seed_iterations(db, "st-rearm", [(2 * n + 1, 4, _ts(100 + 10 * n))])
    firings = evaluator.evaluate(stage)
    assert _triggers(firings) == [Trigger.MAX_FIX_ITERATIONS]
    assert [r["iteration"] for r in firings[0].evidence["iterations"]] == list(
        range(n + 2, 2 * n + 2)
    )


# ------------------------------------------------------------ churn recording


def test_record_churn_buckets_hunks_by_region(db, evaluator, factory_config):
    region_lines = factory_config.escalation.churn_region_lines
    _seed_stage(db, "st-buckets")
    starts = [1, region_lines - 1, region_lines, 5 * region_lines]
    with db.transaction() as conn:
        evaluator.record_churn(conn, "st-buckets", _diff("src/app.py", starts))
        evaluator.record_churn(conn, "st-buckets", "")  # empty diff is a no-op
    assert _churn_rows(db, "st-buckets") == {
        ("src/app.py", 0): 2,  # lines 1 and region_lines-1 share bucket 0
        ("src/app.py", 1): 1,
        ("src/app.py", 5): 1,
    }


def test_record_churn_increments_existing_buckets(db, evaluator):
    _seed_stage(db, "st-bump")
    with db.transaction() as conn:
        evaluator.record_churn(conn, "st-bump", _diff("a.py", [1]))
    with db.transaction() as conn:
        evaluator.record_churn(conn, "st-bump", _diff("a.py", [1]))
    assert _churn_rows(db, "st-bump") == {("a.py", 0): 2}


def test_record_churn_handles_new_and_deleted_files(db, evaluator):
    _seed_stage(db, "st-newdel")
    diff_text = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "index 1111111..0000000\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n"
        "-x\n"
        "-y\n"
        "-z\n"
        "diff --git a/pkg/new.py b/pkg/new.py\n"
        "new file mode 100644\n"
        "index 0000000..2222222\n"
        "--- /dev/null\n"
        "+++ b/pkg/new.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+a\n"
        "+b\n"
    )
    with db.transaction() as conn:
        evaluator.record_churn(conn, "st-newdel", diff_text)
    assert _churn_rows(db, "st-newdel") == {("gone.py", 0): 1, ("pkg/new.py", 0): 1}


def test_record_churn_ignores_header_lookalikes_in_hunk_content(
    db, evaluator, factory_config
):
    """Added/removed content lines rendering as '+++ ...' / '--- ...' must never be
    misread as file headers — the second hunk still belongs to real.py."""
    region_lines = factory_config.escalation.churn_region_lines
    _seed_stage(db, "st-sneaky")
    diff_text = (
        "diff --git a/real.py b/real.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/real.py\n"
        "+++ b/real.py\n"
        "@@ -1,2 +1,4 @@\n"
        "+++ /dev/null\n"  # added line whose content is '++ /dev/null'
        "--- a/fake.py\n"  # removed line whose content is '-- a/fake.py'
        " context\n"
        f"@@ -{region_lines},2 +{region_lines},3 @@\n"
        "+more\n"
    )
    with db.transaction() as conn:
        evaluator.record_churn(conn, "st-sneaky", diff_text)
    assert _churn_rows(db, "st-sneaky") == {("real.py", 0): 1, ("real.py", 1): 1}


def test_record_churn_unquotes_git_quoted_paths(db, evaluator):
    _seed_stage(db, "st-quoted")
    diff_text = (
        'diff --git "a/sp ace.py" "b/sp ace.py"\n'
        '--- "a/sp ace.py"\n'
        '+++ "b/sp ace.py"\n'
        "@@ -1,1 +1,2 @@\n"
        "+x\n"
    )
    with db.transaction() as conn:
        evaluator.record_churn(conn, "st-quoted", diff_text)
    assert _churn_rows(db, "st-quoted") == {("sp ace.py", 0): 1}


def test_record_churn_rejects_hunk_without_file_header(db, evaluator):
    _seed_stage(db, "st-orphan")
    with db.transaction() as conn:
        with pytest.raises(FactoryError):
            evaluator.record_churn(conn, "st-orphan", "@@ -1,2 +1,2 @@\n+x\n")


def test_record_churn_rejects_malformed_hunk_header(db, evaluator):
    _seed_stage(db, "st-malformed")
    diff_text = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ junk @@\n"
    with db.transaction() as conn:
        with pytest.raises(FactoryError):
            evaluator.record_churn(conn, "st-malformed", diff_text)


# ------------------------------------------------------------ churn_threshold


def test_churn_threshold_fires_at_threshold(db, evaluator, factory_config):
    threshold = factory_config.escalation.churn_threshold
    stage = _seed_stage(db, "st-churn")
    for _ in range(threshold - 1):
        with db.transaction() as conn:
            evaluator.record_churn(conn, "st-churn", _diff("hot.py", [1]))
    assert evaluator.evaluate(stage) == []  # threshold - 1 edits: below the line
    with db.transaction() as conn:
        evaluator.record_churn(conn, "st-churn", _diff("hot.py", [1]))
    firings = evaluator.evaluate(stage)
    assert _triggers(firings) == [Trigger.CHURN_THRESHOLD]
    assert firings[0].evidence == {
        "regions": [{"file_path": "hot.py", "region": 0, "edit_count": threshold}],
        "churn_threshold": threshold,
    }


def test_churn_threshold_scoped_per_stage(db, evaluator, factory_config):
    threshold = factory_config.escalation.churn_threshold
    stage = _seed_stage(db, "st-mine")
    _seed_stage(db, "st-theirs")
    for _ in range(threshold):
        with db.transaction() as conn:
            evaluator.record_churn(conn, "st-theirs", _diff("hot.py", [1]))
    assert evaluator.evaluate(stage) == []


# ------------------------------------------------------ always-fire sentinels


def test_contract_change_request_fires_and_dedups_via_event_seq_cursor(db, evaluator):
    stage = _seed_stage(db, "st-ccr")
    seq1 = _seed_event(db, "st-ccr", "contract_change_request")
    firings = evaluator.evaluate(stage)
    assert _triggers(firings) == [Trigger.CONTRACT_CHANGE_REQUEST]
    assert firings[0].evidence == {"event_seqs": [seq1], "event_seq": seq1}

    # An escalation recording the cursor covers the event while open...
    esc_id = _seed_escalation(
        db, "st-ccr", Trigger.CONTRACT_CHANGE_REQUEST.value, event_seq=seq1
    )
    assert evaluator.evaluate(stage) == []
    # ...and STILL covers it after resolution (the §2 cursor ignores status:
    # each sentinel event escalates exactly once).
    _resolve(db, esc_id, resolved_at=utc_now())
    assert evaluator.evaluate(stage) == []

    # A new sentinel event after rework is a new fact and fires again (§5.4).
    seq2 = _seed_event(db, "st-ccr", "contract_change_request")
    firings = evaluator.evaluate(stage)
    assert _triggers(firings) == [Trigger.CONTRACT_CHANGE_REQUEST]
    assert firings[0].evidence == {"event_seqs": [seq2], "event_seq": seq2}


def test_sentinel_open_escalation_suppresses_even_newer_events(db, evaluator):
    """While an escalation is open no second one is insertable (uq_open_escalation),
    so evaluate withholds the newer event until resolution — then it fires."""
    stage = _seed_stage(db, "st-ccr2")
    seq1 = _seed_event(db, "st-ccr2", "contract_change_request")
    esc_id = _seed_escalation(
        db, "st-ccr2", Trigger.CONTRACT_CHANGE_REQUEST.value, event_seq=seq1
    )
    seq2 = _seed_event(db, "st-ccr2", "contract_change_request")
    assert evaluator.evaluate(stage) == []
    _resolve(db, esc_id, resolved_at=utc_now())
    firings = evaluator.evaluate(stage)
    assert _triggers(firings) == [Trigger.CONTRACT_CHANGE_REQUEST]
    assert firings[0].evidence == {"event_seqs": [seq2], "event_seq": seq2}


def test_multiple_uncovered_sentinel_events_yield_one_firing_with_max_cursor(db, evaluator):
    stage = _seed_stage(db, "st-multi")
    seq1 = _seed_event(db, "st-multi", "declared_failure")
    seq2 = _seed_event(db, "st-multi", "declared_failure")
    firings = evaluator.evaluate(stage)
    assert _triggers(firings) == [Trigger.AGENT_DECLARED_FAILURE]
    assert firings[0].evidence == {"event_seqs": [seq1, seq2], "event_seq": seq2}


def test_agent_declared_failure_fires(db, evaluator):
    stage = _seed_stage(db, "st-adf")
    seq = _seed_event(db, "st-adf", "declared_failure")
    firings = evaluator.evaluate(stage)
    assert _triggers(firings) == [Trigger.AGENT_DECLARED_FAILURE]
    assert firings[0].evidence["event_seq"] == seq


def test_sentinel_events_scoped_to_stage_level_and_unit(db, evaluator):
    stage = _seed_stage(db, "st-scope")
    _seed_stage(db, "st-other")
    _seed_event(db, "st-other", "contract_change_request")  # other unit
    _seed_event(db, "st-scope", "contract_change_request", unit_level="phase")  # other level
    assert evaluator.evaluate(stage) == []


# ---------------------------------------------------------------- context_budget


def test_context_budget_fires_at_exactly_the_budget(db, evaluator, factory_config):
    budget = factory_config.budgets.per_stage["routine"]
    stage = _seed_stage(db, "st-budget", risk_class="routine")
    pid = _seed_process(db, "st-budget")
    _seed_usage(db, pid, "st-budget", budget - 4000, 3999)
    assert evaluator.evaluate(stage) == []  # one token under: no firing
    _seed_usage(db, pid, "st-budget", None, 1)
    firings = evaluator.evaluate(stage)
    assert _triggers(firings) == [Trigger.CONTEXT_BUDGET]
    assert firings[0].evidence == {
        "total_tokens": budget,
        "budget": budget,
        "risk_class": "routine",
        "context_resets": 0,
        "max_context_resets": factory_config.escalation.max_context_resets,
    }


def test_all_null_usage_reads_zero_not_null(db, evaluator):
    """§2 per-aggregate COALESCE: a stage whose CLI never reported usage must read
    total 0 (not NULL) — and therefore must not fire."""
    stage = _seed_stage(db, "st-nulls")
    pid = _seed_process(db, "st-nulls")
    for _ in range(3):
        _seed_usage(db, pid, "st-nulls", None, None)
    assert evaluator.evaluate(stage) == []


def test_one_all_null_column_does_not_null_out_the_sum(db, evaluator, factory_config):
    """The naive COALESCE(SUM(a)+SUM(b),0) reads 0 whenever EITHER column is
    all-NULL — this fixture fires only with per-aggregate COALESCE."""
    budget = factory_config.budgets.per_stage["routine"]
    stage = _seed_stage(db, "st-onecol", risk_class="routine")
    pid = _seed_process(db, "st-onecol")
    _seed_usage(db, pid, "st-onecol", None, None)
    _seed_usage(db, pid, "st-onecol", None, budget)  # tokens_in stays all-NULL
    firings = evaluator.evaluate(stage)
    assert _triggers(firings) == [Trigger.CONTEXT_BUDGET]
    assert firings[0].evidence["total_tokens"] == budget


def test_estimated_rows_count_toward_the_budget(db, evaluator, factory_config):
    budget = factory_config.budgets.per_stage["routine"]
    stage = _seed_stage(db, "st-est", risk_class="routine")
    pid = _seed_process(db, "st-est")
    _seed_usage(db, pid, "st-est", budget, 0, estimated=True)
    assert _triggers(evaluator.evaluate(stage)) == [Trigger.CONTEXT_BUDGET]


def test_budget_bound_per_risk_class(db, evaluator, factory_config):
    routine_budget = factory_config.budgets.per_stage["routine"]
    structural_budget = factory_config.budgets.per_stage["structural"]
    assert structural_budget > routine_budget  # fixture sanity
    stage = _seed_stage(db, "st-structural", risk_class="structural")
    pid = _seed_process(db, "st-structural")
    _seed_usage(db, pid, "st-structural", routine_budget, 0)
    assert evaluator.evaluate(stage) == []  # routine cap is not the structural cap
    _seed_usage(db, pid, "st-structural", structural_budget - routine_budget, 0)
    firings = evaluator.evaluate(stage)
    assert _triggers(firings) == [Trigger.CONTEXT_BUDGET]
    assert firings[0].evidence["budget"] == structural_budget


def test_context_budget_evidence_counts_context_resets(db, evaluator, factory_config):
    budget = factory_config.budgets.per_stage["routine"]
    stage = _seed_stage(db, "st-resets", risk_class="routine")
    pid = _seed_process(db, "st-resets")
    _seed_usage(db, pid, "st-resets", budget, 0)
    _seed_event(db, "st-resets", "context_reset")
    firings = evaluator.evaluate(stage)
    assert _triggers(firings) == [Trigger.CONTEXT_BUDGET]
    assert firings[0].evidence["context_resets"] == 1


def test_unknown_risk_class_raises_config_error(db, evaluator):
    stage = _seed_stage(db, "st-exotic", risk_class="exotic")
    with pytest.raises(ConfigError, match="exotic"):
        evaluator.evaluate(stage)


def _seed_session_usage(db, process_id: int, stage_id: str, tokens: int) -> None:
    """token_ledger row under role='decision_session' (founder conversation)."""
    with db.transaction() as conn:
        insert_token_usage(
            conn,
            process_id=process_id,
            unit_level="stage",
            unit_id=stage_id,
            role="decision_session",
            model="fable",
            tokens_in=tokens,
            tokens_out=0,
            cost_usd=None,
        )


def test_context_budget_excludes_decision_session_rows(db, evaluator, factory_config):
    """CCR-3/D-0017 (OPEN-D4): founder Decision-Session burn must NEVER push a
    blocked stage over its cap — the trigger sums conveyor rows only."""
    budget = factory_config.budgets.per_stage["routine"]
    stage = _seed_stage(db, "st-session", risk_class="routine")
    pid = _seed_process(db, "st-session")
    _seed_session_usage(db, pid, "st-session", budget * 3)  # far beyond the cap
    assert evaluator.evaluate(stage) == []


def test_context_budget_conveyor_rows_fire_despite_session_noise(
    db, evaluator, factory_config
):
    """The exclusion subtracts ONLY role='decision_session': conveyor rows alone
    still reach the cap, and the evidence total counts conveyor tokens only —
    while db.unit_token_total (the §2b dashboard burn figure) keeps summing
    everything (visibility unchanged)."""
    from sf_factory.db import unit_token_total

    budget = factory_config.budgets.per_stage["routine"]
    stage = _seed_stage(db, "st-mixed", risk_class="routine")
    pid = _seed_process(db, "st-mixed")
    _seed_session_usage(db, pid, "st-mixed", budget)  # session noise
    _seed_usage(db, pid, "st-mixed", budget - 1, 0)  # conveyor: one under
    assert evaluator.evaluate(stage) == []
    _seed_usage(db, pid, "st-mixed", 1, 0)  # conveyor crosses the cap
    firings = evaluator.evaluate(stage)
    assert _triggers(firings) == [Trigger.CONTEXT_BUDGET]
    assert firings[0].evidence["total_tokens"] == budget  # session rows excluded
    assert unit_token_total(db.read(), "stage", "st-mixed") == budget * 2


# ----------------------------------------------------------- record_validation


def test_record_validation_inserts_sequential_iterations(db, evaluator):
    _seed_stage(db, "st-rv")
    with db.transaction() as conn:
        first = evaluator.record_validation(
            conn, "st-rv", ValidationSummary(failing=5, passing=0, total=5), None
        )
        second = evaluator.record_validation(
            conn, "st-rv", ValidationSummary(failing=3, passing=2, total=5), None
        )
    assert (first, second) == (1, 2)
    rows = db.read().execute(
        "SELECT iteration, failing_tests, report_artifact_id FROM fix_iterations"
        " WHERE stage_id = 'st-rv' ORDER BY iteration"
    ).fetchall()
    assert [(r["iteration"], r["failing_tests"], r["report_artifact_id"]) for r in rows] == [
        (1, 5, None),
        (2, 3, None),
    ]


def test_record_validation_feeds_the_max_fix_trigger(db, evaluator, factory_config):
    """End-to-end through the public recording path (real utc_now timestamps)."""
    n = factory_config.escalation.max_fix_iterations
    stage = _seed_stage(db, "st-rv-fire")
    for _ in range(n):
        with db.transaction() as conn:
            evaluator.record_validation(
                conn, "st-rv-fire", ValidationSummary(failing=7, passing=0, total=7), None
            )
    assert _triggers(evaluator.evaluate(stage)) == [Trigger.MAX_FIX_ITERATIONS]


# ------------------------------------------------------------------- evaluate


def test_evaluate_returns_all_firings_in_trigger_declaration_order(
    db, evaluator, factory_config
):
    n = factory_config.escalation.max_fix_iterations
    threshold = factory_config.escalation.churn_threshold
    budget = factory_config.budgets.per_stage["routine"]
    stage = _seed_stage(db, "st-all", risk_class="routine")
    _seed_iterations(db, "st-all", [(i + 1, 5, _ts(10 * (i + 1))) for i in range(n)])
    for _ in range(threshold):
        with db.transaction() as conn:
            evaluator.record_churn(conn, "st-all", _diff("hot.py", [1]))
    _seed_event(db, "st-all", "contract_change_request")
    _seed_event(db, "st-all", "declared_failure")
    pid = _seed_process(db, "st-all")
    _seed_usage(db, pid, "st-all", budget, 0)

    before = {
        table: db.read().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
        for table in ("events", "escalations", "fix_iterations", "churn", "token_ledger")
    }
    firings = evaluator.evaluate(stage)
    after = {
        table: db.read().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
        for table in ("events", "escalations", "fix_iterations", "churn", "token_ledger")
    }
    assert _triggers(firings) == [
        Trigger.MAX_FIX_ITERATIONS,
        Trigger.CHURN_THRESHOLD,
        Trigger.CONTRACT_CHANGE_REQUEST,
        Trigger.AGENT_DECLARED_FAILURE,
        Trigger.CONTEXT_BUDGET,
    ]
    # Evidence is escalation-payload material: it must serialize as-is.
    json.dumps([f.evidence for f in firings])
    # evaluate is pure reads (§4): nothing was written anywhere.
    assert after == before


def test_open_escalations_suppress_every_trigger(db, evaluator, factory_config):
    n = factory_config.escalation.max_fix_iterations
    threshold = factory_config.escalation.churn_threshold
    budget = factory_config.budgets.per_stage["routine"]
    stage = _seed_stage(db, "st-covered", risk_class="routine")
    _seed_iterations(db, "st-covered", [(i + 1, 5, _ts(10 * (i + 1))) for i in range(n)])
    for _ in range(threshold):
        with db.transaction() as conn:
            evaluator.record_churn(conn, "st-covered", _diff("hot.py", [1]))
    ccr_seq = _seed_event(db, "st-covered", "contract_change_request")
    adf_seq = _seed_event(db, "st-covered", "declared_failure")
    pid = _seed_process(db, "st-covered")
    _seed_usage(db, pid, "st-covered", budget, 0)

    _seed_escalation(db, "st-covered", Trigger.MAX_FIX_ITERATIONS.value)
    _seed_escalation(db, "st-covered", Trigger.CHURN_THRESHOLD.value)
    _seed_escalation(
        db, "st-covered", Trigger.CONTRACT_CHANGE_REQUEST.value, event_seq=ccr_seq
    )
    _seed_escalation(
        db, "st-covered", Trigger.AGENT_DECLARED_FAILURE.value, event_seq=adf_seq
    )
    _seed_escalation(db, "st-covered", Trigger.CONTEXT_BUDGET.value)
    assert evaluator.evaluate(stage) == []
