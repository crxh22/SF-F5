"""Unit tests for config.py (design §8): golden load of the REAL factory.config.yaml,
rejection of unknown/missing keys, and the §4 cross-checks."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from sf_factory.config import (
    CanonCfg,
    ConsultationPointCfg,
    FactoryConfig,
    ModelRoute,
    load_config,
)
from sf_factory.models import ConfigError

# ------------------------------------------------- golden test: the real config


class TestGoldenRealConfig:
    """The repo's factory.config.yaml must load and pass every cross-check.
    Assertions target DoD-locked structure, not founder-tunable values."""

    @pytest.fixture()
    def cfg(self, real_config_path: Path) -> FactoryConfig:
        return load_config(real_config_path)

    def test_loads_and_validates(self, cfg: FactoryConfig):
        assert isinstance(cfg, FactoryConfig)

    def test_budgets_per_stage_nesting_mirrors_risk_classes(self, cfg: FactoryConfig):
        assert set(cfg.budgets.per_stage) == set(cfg.risk_classes)
        assert set(cfg.risk_classes) == {"routine", "structural", "critical"}
        for cap in cfg.budgets.per_stage.values():
            assert isinstance(cap, int) and cap > 0

    def test_cp1_is_the_only_registered_point(self, cfg: FactoryConfig):
        # DoD §4: a single point — CP-1. No others.
        assert [cp.id for cp in cfg.consultation_points] == ["CP-1"]

    def test_cp1_closed_verdict_set_and_fallback(self, cfg: FactoryConfig):
        cp1 = cfg.consultation_points[0]
        assert set(cp1.verdicts) == {"continue_session", "rebuild", "respec", "escalate"}
        assert cp1.fallback == "escalate"  # DoD §3.4 deterministic fallback
        assert cp1.fallback in cp1.verdicts
        assert cp1.role in cfg.models

    def test_risk_class_roles_resolve_to_model_routes(self, cfg: FactoryConfig):
        for rc in cfg.risk_classes.values():
            assert rc.validator in cfg.models
            for auditor in rc.audits:
                assert auditor in cfg.models

    def test_risk_class_routing_shape(self, cfg: FactoryConfig):
        # DoD §7 table: routine has no audits; critical has the human gate.
        assert cfg.risk_classes["routine"].audits == []
        assert cfg.risk_classes["routine"].human_gate is False
        assert cfg.risk_classes["structural"].human_gate is False
        assert len(cfg.risk_classes["structural"].audits) == 2
        assert cfg.risk_classes["critical"].human_gate is True

    def test_model_routes_typed(self, cfg: FactoryConfig):
        for route in cfg.models.values():
            assert isinstance(route, ModelRoute)
            assert route.cli in ("claude", "codex", "stub")
            assert route.mode in ("print", "interactive")

    def test_canon_section_modeled(self, cfg: FactoryConfig):
        # CCR-1: FactoryConfig models the canon section (D-0009) — under
        # extra='forbid' the golden load would fail if it were not declared.
        assert isinstance(cfg.canon, CanonCfg)
        assert cfg.canon.files  # canon injection cannot be vacuous

    def test_canon_inject_references_declared_files(self, cfg: FactoryConfig):
        declared = set(cfg.canon.files)
        assert set(cfg.canon.inject.pipeline_agents) <= declared
        assert set(cfg.canon.inject.founder_facing) <= declared
        assert set(cfg.canon.inject.consultation_points) <= declared

    def test_watchdog_staleness_documented_relation(self, cfg: FactoryConfig):
        watchdog = cfg.founder_channel.watchdog
        assert watchdog.staleness_threshold_s >= 10 * cfg.process.loop_tick_s

    def test_known_open_parameter_test_command_is_nullable(self, cfg: FactoryConfig):
        # OPEN-2 closed (intake interview 12-06-2026, D-ERP-0001): the canonical
        # suite command is set. Amendment pre-authorized at ratification (R1-8,
        # phase-seeding design §3); the SCHEMA stays nullable for new projects.
        assert cfg.projects["erp"].test_command == "bash scripts/test.sh"

    def test_effort_routing_ratified(self, cfg: FactoryConfig):
        # CCR-6 (12-06-2026) + D-0038: per-role reasoning effort. The codex roles
        # (integration_validator, auditor_cross_model) now carry xhigh too (gpt-5.5
        # via `-c model_reasoning_effort`). Omissions remain deliberate:
        # main_architect (interactive — the launcher owns --effort), cp1_triage (haiku).
        for role in (
            "phase_architect",
            "spec_agent",
            "builder_heavy",
            "validator_structural",
            "auditor_same_model",
            "integration_validator",
            "auditor_cross_model",
        ):
            assert cfg.models[role].effort == "xhigh", role
        for role in ("builder_routine", "validator", "decision_session"):
            assert cfg.models[role].effort == "high", role
        for role in ("main_architect", "cp1_triage"):
            assert cfg.models[role].effort is None, role

    def test_usage_limit_signatures_shape(self, cfg: FactoryConfig):
        # CCR-6: non-empty lowercase substrings (the detector lowercases the
        # scanned text, never the signatures); entries are founder-tunable but
        # the D-0021 incident-class anchor must stay covered.
        signatures = cfg.founder_channel.usage_limit_signatures
        assert signatures
        assert all(s and s == s.lower() for s in signatures)
        assert "usage limit" in signatures

    def test_capacity_governor_declared(self, cfg: FactoryConfig):
        # CCR-11 (D-0037): the governor is ON in production and its hold-exit
        # canary route is declared — cheapest claude, print mode. The interval
        # VALUE is founder-tunable; enabled + route shape are DoD-locked.
        assert cfg.capacity_governor.enabled is True
        assert cfg.capacity_governor.probe_interval_s == 300
        probe = cfg.models["capacity_probe"]
        assert probe.cli == "claude"
        assert probe.model == "haiku"
        assert probe.mode == "print"

    def test_pricing_table_structure(self, cfg: FactoryConfig):
        # CCR-10 (dashboard design §11.1): pricing.usd_per_mtok keyed by LEDGER
        # model strings — VALUES are founder-tunable, the structure is not. The
        # claude route models + 'default' (codex ledger rows) must be priced so
        # NULL-cost rows estimate instead of hitting the missing-price marker.
        # 'opus' is the D-0038 live heavy-role model (the D-0025 downshift
        # reservation, formerly keyed 'opus-4-8', became real on the Fable outage).
        table = cfg.pricing.usd_per_mtok
        route_models = {
            route.model for route in cfg.models.values() if route.cli == "claude"
        }
        assert route_models <= set(table)
        assert "default" in table  # codex ledger rows record model 'default'
        assert "opus" in table
        for model, price in table.items():
            assert price.input > 0, model
            assert price.output > 0, model


# --------------------------------------------------------- minimal fixture config


class TestMinimalConfig:
    def test_minimal_fixture_validates(self, factory_config: FactoryConfig):
        assert factory_config.consultation_points[0].id == "CP-1"
        assert factory_config.models["builder_routine"].cli == "stub"

    def test_grace_durations_accept_numbers(self, config_dict):
        config_dict["process"]["terminate_grace_s"] = 0.25
        config_dict["process"]["kill_grace_s"] = 0.1
        cfg = FactoryConfig.model_validate(config_dict)
        assert cfg.process.terminate_grace_s == 0.25


# ----------------------------------------------------- load_config error surface


class TestLoadConfigErrors:
    def test_missing_file_raises_config_error(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="cannot read"):
            load_config(tmp_path / "absent.yaml")

    def test_invalid_yaml_raises_config_error(self, tmp_path: Path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("factory: [unclosed", encoding="utf-8")
        with pytest.raises(ConfigError, match="invalid YAML"):
            load_config(bad)

    def test_non_mapping_root_raises_config_error(self, tmp_path: Path):
        bad = tmp_path / "list.yaml"
        bad.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="mapping"):
            load_config(bad)

    def test_load_config_on_serialized_minimal(self, tmp_path: Path, config_dict):
        path = tmp_path / "mini.yaml"
        path.write_text(yaml.safe_dump(config_dict), encoding="utf-8")
        cfg = load_config(path)
        assert cfg.escalation.max_fix_iterations == 3


# ------------------------------------------------ unknown / missing key rejection


def _expect_config_error(tmp_path: Path, config_dict: dict, match: str | None = None) -> None:
    """Serialize the mutated dict and assert the REAL load path rejects it."""
    path = tmp_path / "rejected.yaml"
    path.write_text(yaml.safe_dump(config_dict), encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        load_config(path)


class TestSchemaRejection:
    def test_unknown_top_level_key(self, tmp_path, config_dict):
        config_dict["surprise_section"] = {}
        _expect_config_error(tmp_path, config_dict)

    def test_unknown_nested_key_process(self, tmp_path, config_dict):
        config_dict["process"]["bogus_knob"] = 1
        _expect_config_error(tmp_path, config_dict)

    def test_unknown_nested_key_route(self, tmp_path, config_dict):
        config_dict["models"]["builder_routine"]["temperature"] = 0.2
        _expect_config_error(tmp_path, config_dict)

    def test_missing_section(self, tmp_path, config_dict):
        del config_dict["escalation"]
        _expect_config_error(tmp_path, config_dict)

    def test_missing_nested_key(self, tmp_path, config_dict):
        del config_dict["escalation"]["max_fix_iterations"]
        _expect_config_error(tmp_path, config_dict)

    def test_missing_test_command_key_rejected(self, tmp_path, config_dict):
        # Nullable but never absent: the key must exist explicitly (OPEN-2 visibility).
        del config_dict["projects"]["proj"]["test_command"]
        _expect_config_error(tmp_path, config_dict)

    def test_bad_cli_literal(self, tmp_path, config_dict):
        config_dict["models"]["builder_routine"]["cli"] = "gemini"
        _expect_config_error(tmp_path, config_dict)

    def test_bad_mode_literal(self, tmp_path, config_dict):
        config_dict["models"]["builder_routine"]["mode"] = "batch"
        _expect_config_error(tmp_path, config_dict)

    def test_bad_usage_missing_policy(self, tmp_path, config_dict):
        config_dict["budgets"]["usage_missing_policy"] = "ignore"
        _expect_config_error(tmp_path, config_dict)

    def test_nonpositive_budget_cap(self, tmp_path, config_dict):
        config_dict["budgets"]["per_stage"]["routine"] = 0
        _expect_config_error(tmp_path, config_dict)

    def test_zero_churn_region_lines(self, tmp_path, config_dict):
        config_dict["escalation"]["churn_region_lines"] = 0
        _expect_config_error(tmp_path, config_dict)

    def test_bad_effort_literal(self, tmp_path, config_dict):
        # CCR-6: effort is a closed Literal set — an unknown level is rejected.
        config_dict["models"]["builder_routine"]["effort"] = "ultra"
        _expect_config_error(tmp_path, config_dict, match="effort")

    def test_empty_usage_limit_signatures_rejected(self, tmp_path, config_dict):
        config_dict["founder_channel"]["usage_limit_signatures"] = []
        _expect_config_error(tmp_path, config_dict, match="usage_limit_signatures")

    def test_non_lowercase_usage_limit_signature_rejected(self, tmp_path, config_dict):
        # The detector lowercases the scanned text, never the signatures — an
        # uppercase signature would silently never match (fail-explicit at load).
        config_dict["founder_channel"]["usage_limit_signatures"] = ["Rate Limit"]
        _expect_config_error(tmp_path, config_dict, match="lowercase")

    # ------------------------------------------------------- CCR-10: pricing

    def test_pricing_optional_default_empty(self, config_dict):
        # §11.3.1: pricing is an OPTIONAL top-level section, default empty —
        # the minimal fixture carries none and must keep validating.
        assert "pricing" not in config_dict
        cfg = FactoryConfig.model_validate(config_dict)
        assert cfg.pricing.usd_per_mtok == {}

    def test_pricing_nonpositive_price_rejected(self, tmp_path, config_dict):
        config_dict["pricing"] = {"usd_per_mtok": {"fable": {"input": 0, "output": 50}}}
        _expect_config_error(tmp_path, config_dict)
        config_dict["pricing"] = {"usd_per_mtok": {"fable": {"input": 10, "output": -1}}}
        _expect_config_error(tmp_path, config_dict)

    def test_pricing_unknown_key_rejected(self, tmp_path, config_dict):
        # extra='forbid' on both PricingCfg and ModelPrice.
        config_dict["pricing"] = {"usd_per_mtok": {}, "currency": "EUR"}
        _expect_config_error(tmp_path, config_dict)
        config_dict["pricing"] = {
            "usd_per_mtok": {"fable": {"input": 10, "output": 50, "cached": 1}}
        }
        _expect_config_error(tmp_path, config_dict)


# ----------------------------------------------------------- §4 cross-checks


class TestCrossChecks:
    def test_budgets_keys_subset_mismatch(self, tmp_path, config_dict):
        del config_dict["budgets"]["per_stage"]["critical"]
        _expect_config_error(tmp_path, config_dict, match="per_stage")

    def test_budgets_keys_superset_mismatch(self, tmp_path, config_dict):
        config_dict["budgets"]["per_stage"]["experimental"] = 1000
        _expect_config_error(tmp_path, config_dict, match="per_stage")

    def test_risk_class_validator_not_in_models(self, tmp_path, config_dict):
        config_dict["risk_classes"]["routine"]["validator"] = "ghost_validator"
        _expect_config_error(tmp_path, config_dict, match="ghost_validator")

    def test_risk_class_auditor_not_in_models(self, tmp_path, config_dict):
        config_dict["risk_classes"]["structural"]["audits"] = ["ghost_auditor"]
        _expect_config_error(tmp_path, config_dict, match="ghost_auditor")

    def test_cp_fallback_not_in_verdicts(self, tmp_path, config_dict):
        config_dict["consultation_points"][0]["fallback"] = "give_up"
        _expect_config_error(tmp_path, config_dict, match="fallback")

    def test_cp_empty_verdicts(self, tmp_path, config_dict):
        config_dict["consultation_points"][0]["verdicts"] = []
        _expect_config_error(tmp_path, config_dict, match="verdict")

    def test_cp_duplicate_verdicts(self, tmp_path, config_dict):
        config_dict["consultation_points"][0]["verdicts"] = ["escalate", "escalate"]
        _expect_config_error(tmp_path, config_dict, match="duplicate")

    def test_cp_role_not_in_models(self, tmp_path, config_dict):
        config_dict["consultation_points"][0]["role"] = "ghost_role"
        _expect_config_error(tmp_path, config_dict, match="ghost_role")

    # ------------------------------------------- CCR-11: capacity governor

    def test_capacity_governor_optional_default_disabled(self, config_dict):
        # The section is OPTIONAL, default DISABLED (the pricing precedent):
        # the frozen minimal fixture predates it and must keep validating.
        assert "capacity_governor" not in config_dict
        cfg = FactoryConfig.model_validate(config_dict)
        assert cfg.capacity_governor.enabled is False
        assert cfg.capacity_governor.probe_interval_s == 300

    def test_capacity_governor_enabled_requires_probe_route(
        self, tmp_path, config_dict
    ):
        # The probe is the ONLY hold exit — enabling without the canary route
        # would wedge every hold forever (fail-explicit at load).
        config_dict["capacity_governor"] = {"enabled": True}
        _expect_config_error(tmp_path, config_dict, match="capacity_probe")

    def test_capacity_governor_probe_route_must_be_print(self, tmp_path, config_dict):
        config_dict["capacity_governor"] = {"enabled": True}
        config_dict["models"]["capacity_probe"] = {
            "cli": "claude",
            "model": "haiku",
            "mode": "interactive",
        }
        _expect_config_error(tmp_path, config_dict, match="print")

    def test_capacity_governor_nonpositive_interval_rejected(
        self, tmp_path, config_dict
    ):
        config_dict["capacity_governor"] = {"enabled": False, "probe_interval_s": 0}
        _expect_config_error(tmp_path, config_dict)

    def test_cp_duplicate_ids(self, tmp_path, config_dict):
        config_dict["consultation_points"].append(
            copy.deepcopy(config_dict["consultation_points"][0])
        )
        _expect_config_error(tmp_path, config_dict, match="duplicate")

    def test_canon_inject_unknown_file_key(self, tmp_path, config_dict):
        config_dict["canon"]["inject"]["pipeline_agents"] = ["doctrine", "ghost_file"]
        _expect_config_error(tmp_path, config_dict, match="ghost_file")

    def test_watchdog_staleness_below_10x_tick(self, tmp_path, config_dict):
        config_dict["founder_channel"]["watchdog"]["staleness_threshold_s"] = 9
        config_dict["process"]["loop_tick_s"] = 1
        _expect_config_error(tmp_path, config_dict, match="staleness")

    def test_codex_effort_accepted_but_max_rejected(self, tmp_path, config_dict):
        # D-0038: codex DOES carry a reasoning knob now (gpt-5.5,
        # `-c model_reasoning_effort`). Valid codex levels load; 'max' is
        # claude-only and is rejected fail-explicit, naming the offending role.
        config_dict["models"]["auditor_cross_model"] = {
            "cli": "codex",
            "model": "gpt-5.5",
            "mode": "print",
            "effort": "xhigh",
        }
        ok_path = tmp_path / "codex_xhigh.yaml"
        ok_path.write_text(yaml.safe_dump(config_dict), encoding="utf-8")
        load_config(ok_path)  # valid codex effort: loads without error
        config_dict["models"]["auditor_cross_model"]["effort"] = "max"
        _expect_config_error(tmp_path, config_dict, match="auditor_cross_model")

    def test_effort_on_stub_route_rejected(self, tmp_path, config_dict):
        # Same cross-check for the stub CLI (the conftest test routes).
        config_dict["models"]["builder_routine"]["effort"] = "high"
        _expect_config_error(tmp_path, config_dict, match="builder_routine")

    def test_cp_model_standalone_fallback_check(self):
        with pytest.raises(Exception, match="fallback"):
            ConsultationPointCfg(
                id="CP-X",
                purpose="t",
                inputs=["a"],
                verdicts=["go"],
                fallback="stop",
                role="r",
                max_input_bytes=10,
            )
