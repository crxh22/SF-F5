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
        # OPEN-2: erp test_command is null until founder sets it; must not fail validation.
        assert cfg.projects["erp"].test_command is None


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
