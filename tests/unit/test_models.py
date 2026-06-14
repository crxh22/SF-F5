"""Unit tests for models.py (design §8): transition-table closure properties,
sched_category mapping, helpers, dataclass discipline, error taxonomy."""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime

import pytest

from sf_factory import models
from sf_factory.models import (
    VALID_PHASE_TRANSITIONS,
    VALID_STAGE_TRANSITIONS,
    ArtifactRef,
    ConfigError,
    DecisionRequest,
    Escalation,
    Event,
    FactoryError,
    Finding,
    Level,
    Phase,
    PhaseState,
    ProcessRecord,
    RiskClass,
    SchedCategory,
    Stage,
    StageState,
    TransitionError,
    Trigger,
    TriggerFiring,
    ValidationSummary,
    new_id,
    sched_category,
    utc_now,
)

# ----------------------------------------------------------- transition tables


class TestTransitionTables:
    def test_stage_table_matches_design_3_1_exactly(self):
        s = StageState
        expected = {
            s.PENDING: {s.SPEC, s.CANCELLED},
            s.SPEC: {s.BUILD, s.ESCALATED, s.CANCELLED},
            s.BUILD: {s.VALIDATE, s.ESCALATED, s.CANCELLED},
            s.VALIDATE: {s.MERGE_GATE, s.AUDIT, s.BUILD, s.SPEC, s.ESCALATED, s.CANCELLED},
            s.AUDIT: {s.MERGE_GATE, s.AWAITING_HUMAN, s.BUILD, s.ESCALATED, s.CANCELLED},
            s.AWAITING_HUMAN: {s.MERGE_GATE, s.BUILD, s.SPEC, s.ESCALATED, s.CANCELLED},
            s.MERGE_GATE: {s.DONE, s.BUILD, s.ESCALATED, s.CANCELLED},
            s.ESCALATED: {
                s.SPEC,
                s.BUILD,
                s.VALIDATE,
                s.MERGE_GATE,
                s.AWAITING_HUMAN,
                s.FAILED,
                s.CANCELLED,
            },
            s.DONE: set(),
            s.FAILED: set(),
            s.CANCELLED: set(),
        }
        assert dict(VALID_STAGE_TRANSITIONS) == {k: frozenset(v) for k, v in expected.items()}

    def test_phase_table_matches_design_3_2_exactly(self):
        p = PhaseState
        expected = {
            p.PENDING: {p.PLANNING, p.CANCELLED},
            p.PLANNING: {p.CONTRACTS_FROZEN, p.ESCALATED, p.CANCELLED},
            p.CONTRACTS_FROZEN: {p.RUNNING, p.CANCELLED},
            p.RUNNING: {p.INTEGRATING, p.ESCALATED, p.AWAITING_HUMAN, p.CANCELLED},
            p.INTEGRATING: {p.AWAITING_SIGNOFF, p.RUNNING, p.ESCALATED, p.CANCELLED},
            p.AWAITING_SIGNOFF: {p.DONE, p.RUNNING, p.CANCELLED},
            p.AWAITING_HUMAN: {p.RUNNING, p.PLANNING, p.CANCELLED},
            p.ESCALATED: {p.PLANNING, p.RUNNING, p.AWAITING_HUMAN, p.FAILED, p.CANCELLED},
            p.DONE: set(),
            p.FAILED: set(),
            p.CANCELLED: set(),
        }
        assert dict(VALID_PHASE_TRANSITIONS) == {k: frozenset(v) for k, v in expected.items()}

    @pytest.mark.parametrize(
        ("table", "state_enum"),
        [(VALID_STAGE_TRANSITIONS, StageState), (VALID_PHASE_TRANSITIONS, PhaseState)],
        ids=["stage", "phase"],
    )
    def test_closure_every_state_is_a_key(self, table, state_enum):
        assert set(table.keys()) == set(state_enum)

    @pytest.mark.parametrize(
        ("table", "state_enum"),
        [(VALID_STAGE_TRANSITIONS, StageState), (VALID_PHASE_TRANSITIONS, PhaseState)],
        ids=["stage", "phase"],
    )
    def test_closure_targets_are_valid_states(self, table, state_enum):
        for targets in table.values():
            assert targets <= set(state_enum)

    @pytest.mark.parametrize(
        "table",
        [VALID_STAGE_TRANSITIONS, VALID_PHASE_TRANSITIONS],
        ids=["stage", "phase"],
    )
    def test_closure_terminals_empty_and_only_terminals(self, table):
        for state, targets in table.items():
            if state.value in ("DONE", "FAILED", "CANCELLED"):
                assert targets == frozenset(), f"terminal {state} must have no exits"
            else:
                assert targets, f"non-terminal {state} must have at least one exit"

    @pytest.mark.parametrize(
        ("table", "state_enum"),
        [(VALID_STAGE_TRANSITIONS, StageState), (VALID_PHASE_TRANSITIONS, PhaseState)],
        ids=["stage", "phase"],
    )
    def test_closure_all_states_reachable_from_pending(self, table, state_enum):
        start = state_enum("PENDING")
        seen = {start}
        frontier = [start]
        while frontier:
            for target in table[frontier.pop()]:
                if target not in seen:
                    seen.add(target)
                    frontier.append(target)
        assert seen == set(state_enum)

    @pytest.mark.parametrize(
        "table",
        [VALID_STAGE_TRANSITIONS, VALID_PHASE_TRANSITIONS],
        ids=["stage", "phase"],
    )
    def test_no_self_transitions(self, table):
        for state, targets in table.items():
            assert state not in targets

    def test_tables_are_immutable_mappings(self):
        with pytest.raises(TypeError):
            VALID_STAGE_TRANSITIONS[StageState.DONE] = frozenset()  # type: ignore[index]
        with pytest.raises(TypeError):
            VALID_PHASE_TRANSITIONS[PhaseState.DONE] = frozenset()  # type: ignore[index]


# ------------------------------------------------------------------ enum values


class TestEnumValues:
    def test_level_values_match_ddl(self):
        assert Level.PHASE.value == "phase"
        assert Level.STAGE.value == "stage"

    def test_risk_class_values_match_config_keys(self):
        assert {rc.value for rc in RiskClass} == {"routine", "structural", "critical"}

    def test_trigger_values_are_the_sql_literals(self):
        # §2 trigger SQL uses these exact lowercase strings.
        assert Trigger.MAX_FIX_ITERATIONS.value == "max_fix_iterations"
        assert Trigger.CHURN_THRESHOLD.value == "churn_threshold"
        assert Trigger.CONTRACT_CHANGE_REQUEST.value == "contract_change_request"
        assert Trigger.AGENT_DECLARED_FAILURE.value == "agent_declared_failure"
        assert Trigger.CONTEXT_BUDGET.value == "context_budget"

    def test_state_values_match_ddl_check_sets(self):
        assert {s.value for s in StageState} == {
            "PENDING", "SPEC", "BUILD", "VALIDATE", "AUDIT", "AWAITING_HUMAN",
            "MERGE_GATE", "ESCALATED", "DONE", "FAILED", "CANCELLED",
        }
        assert {p.value for p in PhaseState} == {
            "PENDING", "PLANNING", "CONTRACTS_FROZEN", "RUNNING", "INTEGRATING",
            "AWAITING_SIGNOFF", "AWAITING_HUMAN", "ESCALATED", "DONE", "FAILED",
            "CANCELLED",
        }

    def test_strenum_str_interoperability(self):
        # StrEnum members must bind/compare as their plain string values.
        assert StageState.PENDING == "PENDING"
        assert f"{Level.STAGE}" == "stage"


# --------------------------------------------------------------- sched_category


class TestSchedCategory:
    @pytest.mark.parametrize("level", [Level.STAGE, Level.PHASE])
    def test_pending_splits_on_deps(self, level):
        assert sched_category(level, "PENDING", False) is SchedCategory.WAITING
        assert sched_category(level, "PENDING", True) is SchedCategory.RUNNABLE

    @pytest.mark.parametrize("state", ["SPEC", "BUILD", "VALIDATE", "AUDIT", "MERGE_GATE"])
    def test_stage_running_states(self, state):
        assert sched_category(Level.STAGE, state, True) is SchedCategory.RUNNING

    @pytest.mark.parametrize(
        "state", ["PLANNING", "CONTRACTS_FROZEN", "RUNNING", "INTEGRATING"]
    )
    def test_phase_running_states(self, state):
        assert sched_category(Level.PHASE, state, True) is SchedCategory.RUNNING

    @pytest.mark.parametrize(
        ("level", "state"),
        [
            (Level.STAGE, "AWAITING_HUMAN"),
            (Level.STAGE, "ESCALATED"),
            (Level.PHASE, "AWAITING_HUMAN"),
            (Level.PHASE, "AWAITING_SIGNOFF"),
            (Level.PHASE, "ESCALATED"),
        ],
    )
    def test_blocked_states(self, level, state):
        assert sched_category(level, state, True) is SchedCategory.BLOCKED

    @pytest.mark.parametrize("level", [Level.STAGE, Level.PHASE])
    def test_terminal_states(self, level):
        assert sched_category(level, "DONE", True) is SchedCategory.TERMINAL_OK
        assert sched_category(level, "FAILED", True) is SchedCategory.TERMINAL_FAIL
        assert sched_category(level, "CANCELLED", False) is SchedCategory.TERMINAL_FAIL

    def test_deps_flag_irrelevant_outside_pending(self):
        assert sched_category(Level.STAGE, "BUILD", False) is SchedCategory.RUNNING
        assert sched_category(Level.PHASE, "DONE", False) is SchedCategory.TERMINAL_OK

    def test_unknown_state_raises(self):
        with pytest.raises(TransitionError):
            sched_category(Level.STAGE, "NOT_A_STATE", True)

    def test_cross_level_state_raises(self):
        with pytest.raises(TransitionError):
            sched_category(Level.STAGE, "AWAITING_SIGNOFF", True)  # phase-only state
        with pytest.raises(TransitionError):
            sched_category(Level.PHASE, "MERGE_GATE", True)  # stage-only state

    def test_every_concrete_state_categorizes(self):
        for state in StageState:
            assert isinstance(sched_category(Level.STAGE, state.value, True), SchedCategory)
        for state in PhaseState:
            assert isinstance(sched_category(Level.PHASE, state.value, True), SchedCategory)


# ---------------------------------------------------------------------- helpers


class TestHelpers:
    def test_utc_now_format(self):
        stamp = utc_now()
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp)

    def test_utc_now_is_utc_now(self):
        parsed = datetime.strptime(utc_now(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        assert abs((datetime.now(UTC) - parsed).total_seconds()) < 5

    def test_utc_now_sorts_lexicographically(self):
        # The §2 SQL compares *_at columns with '<' / '>': format must sort correctly.
        earlier = "2026-06-10T09:59:59Z"
        later = "2026-06-10T10:00:00Z"
        assert earlier < later

    def test_new_id_format_and_prefix(self):
        uid = new_id("stage")
        assert re.fullmatch(r"stage-[0-9a-f]{12}", uid)

    def test_new_id_unique(self):
        assert len({new_id("x") for _ in range(1000)}) == 1000


# ------------------------------------------------------------------ dataclasses


def _sample_instances():
    return [
        Phase("ph-1", "erp", "Foundation", PhaseState.PENDING, None, None,
              "2026-06-10T00:00:00Z", "2026-06-10T00:00:00Z"),
        Stage("st-1", "ph-1", "Schema", "routine", StageState.PENDING, None, None, None,
              "2026-06-10T00:00:00Z", "2026-06-10T00:00:00Z"),
        Event(1, "stage", "st-1", "transition", "PENDING", "SPEC", "control_plane", {},
              "2026-06-10T00:00:00Z"),
        ArtifactRef(None, "stage", "st-1", "spec", "workspace", "_factory/stages/st-1/spec.md",
                    "ab" * 32, None, "2026-06-10T00:00:00Z"),
        ProcessRecord(None, "stage", "st-1", "agent", "builder_routine", None, "sess-1", 123,
                      "claude -p", "/tmp", "spawned", None, None, "2026-06-10T00:00:00Z",
                      None, None),
        Escalation(None, "stage", "st-1", "max_fix_iterations", "phase_architect", None, 42,
                   "open", None, "2026-06-10T00:00:00Z", None),
        Finding(None, "st-1", "auditor_cross_model", "F-1", "major", 1, "open", None, None,
                "2026-06-10T00:00:00Z", "2026-06-10T00:00:00Z"),
        DecisionRequest(None, "stage", "st-1", "critical_stage", 1, "pending", None, None,
                        "2026-06-10T00:00:00Z", None, None),
        TriggerFiring(Trigger.CHURN_THRESHOLD, "stage", "st-1", {"edit_count": 5}),
        ValidationSummary(failing=1, passing=9, total=10),
    ]


class TestDataclasses:
    @pytest.mark.parametrize("instance", _sample_instances(), ids=lambda i: type(i).__name__)
    def test_frozen(self, instance):
        field_name = dataclasses.fields(instance)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(instance, field_name, "mutated")

    @pytest.mark.parametrize("instance", _sample_instances(), ids=lambda i: type(i).__name__)
    def test_slots(self, instance):
        assert not hasattr(instance, "__dict__")

    def test_field_names_match_frozen_interface(self):
        def names(cls):
            return [f.name for f in dataclasses.fields(cls)]

        assert names(Phase) == ["id", "project", "name", "state", "branch",
                                "plan_artifact_id", "created_at", "updated_at"]
        assert names(Stage) == ["id", "phase_id", "name", "risk_class", "state", "branch",
                                "worktree_path", "spec_artifact_id", "created_at", "updated_at"]
        assert names(Event) == ["seq", "unit_level", "unit_id", "event_type", "from_state",
                                "to_state", "actor", "payload", "created_at"]
        assert names(ArtifactRef) == ["id", "unit_level", "unit_id", "kind", "repo", "path",
                                      "sha256", "git_commit", "created_at"]
        # CCR-1: session_id after cp_id; event_seq after payload_artifact_id.
        assert names(ProcessRecord) == ["id", "unit_level", "unit_id", "kind", "role", "cp_id",
                                        "session_id", "pid", "cmdline", "cwd", "state",
                                        "exit_code", "ndjson_log_path", "spawned_at",
                                        "heartbeat_at", "ended_at"]
        assert names(Escalation) == ["id", "unit_level", "unit_id", "trigger", "target",
                                     "payload_artifact_id", "event_seq", "status",
                                     "resolution", "created_at", "resolved_at"]
        assert names(Finding) == ["id", "stage_id", "auditor_role", "finding_ref", "severity",
                                  "report_artifact_id", "status", "contest_artifact_id",
                                  "resolved_by", "created_at", "updated_at"]
        assert names(DecisionRequest) == ["id", "unit_level", "unit_id", "gate_kind",
                                          "request_artifact_id", "status", "answer",
                                          "answer_artifact_id", "created_at", "alerted_at",
                                          "answered_at"]
        assert names(TriggerFiring) == ["trigger", "unit_level", "unit_id", "evidence"]
        assert names(ValidationSummary) == ["failing", "passing", "total"]

    def test_value_equality(self):
        a = ValidationSummary(failing=0, passing=3, total=3)
        b = ValidationSummary(failing=0, passing=3, total=3)
        assert a == b


# --------------------------------------------------------------- error taxonomy


class TestErrorTaxonomy:
    def test_all_errors_subclass_factory_error(self):
        names = [
            "ConfigError", "MigrationError", "TransitionError", "IntegrityError",
            "GitError", "ProcessError", "ArtifactContractError",
            "ConsultationBreachError", "NotifyError",
        ]
        for name in names:
            cls = getattr(models, name)
            assert issubclass(cls, FactoryError)
            assert issubclass(cls, Exception)

    def test_factory_error_is_catchable_base(self):
        with pytest.raises(FactoryError):
            raise ConfigError("bad config")


# ------------------------------------- escalation-resolution vocabulary (CCR-7)
# Appended with the founder-channel UX slice (dashboard design §10.6, D-0027):
# the maps moved from scheduler.py privates, so the old scheduler↔models
# equality has no second operand — the tight invariant is R-B3: every value is
# a LEGAL ESCALATED exit of its level's §3 transition table. Function-level
# imports keep the frozen import block untouched (test_cli.py precedent).


class TestEscalationResolutionVocabulary:
    def test_stage_values_are_legal_escalated_exits(self):
        from sf_factory.models import STAGE_ESCALATION_RESOLUTIONS

        assert STAGE_ESCALATION_RESOLUTIONS  # never silently empty
        for token, target in STAGE_ESCALATION_RESOLUTIONS.items():
            assert isinstance(target, StageState), token
            assert target in VALID_STAGE_TRANSITIONS[StageState.ESCALATED], token

    def test_merge_gate_reentry_token_and_transition(self):
        """D-0041: the manual 'rework:MERGE_GATE' resolution maps to MERGE_GATE,
        which is a legal ESCALATED exit (re-enters ONLY the merge gate — Tier-1
        rebase+suite + Tier-2 integration_validator — no re-validate/re-audit)."""
        from sf_factory.models import STAGE_ESCALATION_RESOLUTIONS

        assert StageState.MERGE_GATE in VALID_STAGE_TRANSITIONS[StageState.ESCALATED]
        assert (
            STAGE_ESCALATION_RESOLUTIONS["rework:MERGE_GATE"] is StageState.MERGE_GATE
        )

    def test_noaction_resolution_is_not_a_map_key(self):
        """Slice-2 Unit A pin: `settled` (the no-action disposition) is a
        first-class STAGE resolution token but deliberately NOT a key in
        STAGE_ESCALATION_RESOLUTIONS — settling routes by risk (MERGE_GATE /
        AWAITING_HUMAN), which the token->ONE-state map cannot encode, so the
        scheduler special-cases it. Keeping it out preserves the one-token->
        one-state invariant the other tests pin."""
        from sf_factory.models import (
            STAGE_ESCALATION_RESOLUTIONS,
            STAGE_NOACTION_RESOLUTION,
        )

        assert STAGE_NOACTION_RESOLUTION == "settled"
        assert STAGE_NOACTION_RESOLUTION not in STAGE_ESCALATION_RESOLUTIONS

    def test_phase_values_are_legal_escalated_exits(self):
        from sf_factory.models import PHASE_ESCALATION_RESOLUTIONS

        assert PHASE_ESCALATION_RESOLUTIONS
        for token, target in PHASE_ESCALATION_RESOLUTIONS.items():
            assert isinstance(target, PhaseState), token
            assert target in VALID_PHASE_TRANSITIONS[PhaseState.ESCALATED], token

    def test_maps_are_read_only(self):
        from sf_factory.models import (
            PHASE_ESCALATION_RESOLUTIONS,
            STAGE_ESCALATION_RESOLUTIONS,
        )

        with pytest.raises(TypeError):
            STAGE_ESCALATION_RESOLUTIONS["new"] = StageState.BUILD  # type: ignore[index]
        with pytest.raises(TypeError):
            PHASE_ESCALATION_RESOLUTIONS["new"] = PhaseState.RUNNING  # type: ignore[index]


# --------------------------------------- escalation-routing ladder (robustness UNIT 2)


class TestEscalationTargetLadder:
    """The escalate-UP ladder the stuck-detector climbs (D-0042). Pins the ladder
    is the single source for the routing vocabulary and stays in lock-step with
    the escalations.target DDL CHECK set ∪ {founder} (design §UNIT 2)."""

    def test_ladder_equals_ddl_target_check_set(self):
        """The ladder values are EXACTLY the escalations.target DDL CHECK set
        (phase_architect, main_architect, founder) — creation sites write the
        first two, the detector bumps toward founder. If a migration changes the
        CHECK set, this fails so the ladder is updated in lock-step."""
        import re

        from sf_factory.db import MIGRATIONS_DIR
        from sf_factory.models import ESCALATION_TARGET_LADDER

        ddl = (MIGRATIONS_DIR / "0001_init.sql").read_text(encoding="utf-8")
        m = re.search(r"target\s+TEXT NOT NULL CHECK \(target IN \(([^)]*)\)\)", ddl)
        assert m, "could not locate escalations.target CHECK in the init migration"
        check_set = {tok.strip().strip("'") for tok in m.group(1).split(",")}
        assert set(ESCALATION_TARGET_LADDER) == check_set
        # Ordered low->high authority; founder is the top rung.
        assert ESCALATION_TARGET_LADDER[-1] == "founder"

    def test_ladder_matches_dashboard_gloss_tokens(self):
        """The dashboard glosses every ladder rung (dashboard.py §10.4) — a bumped
        target never renders bare. One source, kept in sync (design pin)."""
        from sf_factory.dashboard import GLOSS
        from sf_factory.models import ESCALATION_TARGET_LADDER

        for rung in ESCALATION_TARGET_LADDER:
            assert rung in GLOSS, rung

    def test_next_target_climbs_one_rung(self):
        from sf_factory.models import next_escalation_target

        assert next_escalation_target("phase_architect") == "main_architect"
        assert next_escalation_target("main_architect") == "founder"

    def test_next_target_clamps_at_founder(self):
        """No infinite climb: the top rung bumps to itself (the detector still
        re-pages founder, but never invents a rung above it)."""
        from sf_factory.models import next_escalation_target

        assert next_escalation_target("founder") == "founder"

    def test_next_target_unknown_is_returned_unchanged(self):
        """An off-ladder value is never guessed forward (Doctrine §7) — returned
        as-is (treated as already at the top); the detector still pages it."""
        from sf_factory.models import next_escalation_target

        assert next_escalation_target("nonsense") == "nonsense"

    def test_ladder_is_a_tuple_immutable(self):
        from sf_factory.models import ESCALATION_TARGET_LADDER

        assert isinstance(ESCALATION_TARGET_LADDER, tuple)
