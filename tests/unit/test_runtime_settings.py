"""Unit tests for sf_factory.runtime_settings — the live-edit override layer
(founder dashboard, 20-06): the db round-trip + the EffectiveConfig overlay."""

from __future__ import annotations

from sf_factory import db as fdb
from sf_factory import runtime_settings as rs

_AT = "2026-06-20T12:00:00Z"


# --- db accessors -------------------------------------------------------------


def test_set_get_runtime_settings_roundtrips_json_scalars(db):
    with db.transaction() as conn:
        fdb.set_runtime_setting(conn, "max_parallel_agents", 4, updated_by="founder", at=_AT)
        fdb.set_runtime_setting(
            conn, "governor.five_hour_threshold_pct", 82.5, updated_by="founder", at=_AT
        )
        fdb.set_runtime_setting(conn, "drain.manual", True, updated_by="founder", at=_AT)
        fdb.set_runtime_setting(conn, "budget.critical", 364_000_000, updated_by="founder", at=_AT)
    got = fdb.get_runtime_settings(db.read())
    assert got == {
        "max_parallel_agents": 4,
        "governor.five_hour_threshold_pct": 82.5,
        "drain.manual": True,
        "budget.critical": 364_000_000,
    }
    # types survive the JSON round-trip (int stays int, float float, bool bool)
    assert isinstance(got["max_parallel_agents"], int)
    assert isinstance(got["governor.five_hour_threshold_pct"], float)
    assert got["drain.manual"] is True


def test_set_runtime_setting_upserts_and_stamps(db):
    with db.transaction() as conn:
        fdb.set_runtime_setting(conn, "max_parallel_agents", 4, updated_by="founder", at=_AT)
    with db.transaction() as conn:
        fdb.set_runtime_setting(
            conn, "max_parallel_agents", 2, updated_by="control_plane", at="2026-06-20T13:00:00Z"
        )
    assert fdb.get_runtime_settings(db.read()) == {"max_parallel_agents": 2}
    row = db.read().execute(
        "SELECT updated_at, updated_by FROM runtime_settings WHERE key = 'max_parallel_agents'"
    ).fetchone()
    assert row["updated_at"] == "2026-06-20T13:00:00Z"
    assert row["updated_by"] == "control_plane"


def test_get_runtime_settings_empty(db):
    assert fdb.get_runtime_settings(db.read()) == {}


# --- EffectiveConfig overlay --------------------------------------------------


def test_effective_config_falls_back_to_yaml_when_unset(factory_config):
    eff = rs.EffectiveConfig({}, factory_config)
    assert eff.max_parallel_agents == factory_config.process.max_parallel_agents
    assert eff.agent_timeout_s == factory_config.process.agent_timeout_s
    assert eff.gov_five_hour_pct == factory_config.capacity_governor.five_hour_threshold_pct
    assert eff.gov_seven_day_pct == factory_config.capacity_governor.seven_day_threshold_pct
    assert eff.autodrenaj == factory_config.capacity_governor.proactive_enabled
    assert eff.drain_manual is False  # no YAML fallback -> NORMAL
    rc = next(iter(factory_config.budgets.per_stage))
    assert eff.budget(rc) == factory_config.budgets.per_stage[rc]


def test_effective_config_override_wins(factory_config):
    rc = next(iter(factory_config.budgets.per_stage))
    overrides = {
        rs.KEY_MAX_PARALLEL: 1,
        rs.KEY_AGENT_TIMEOUT: 999,
        rs.KEY_GOV_5H: 70.0,
        rs.KEY_GOV_AUTODRENAJ: True,
        rs.KEY_DRAIN_MANUAL: True,
        rs.budget_key(rc): 123_456_789,
    }
    eff = rs.EffectiveConfig(overrides, factory_config)
    assert eff.max_parallel_agents == 1
    assert eff.agent_timeout_s == 999
    assert eff.gov_five_hour_pct == 70.0
    assert eff.autodrenaj is True
    assert eff.drain_manual is True
    assert eff.budget(rc) == 123_456_789


def test_is_writable_key_allowlist():
    assert rs.is_writable_key(rs.KEY_MAX_PARALLEL)
    assert rs.is_writable_key(rs.KEY_DRAIN_MANUAL)
    assert rs.is_writable_key("budget.routine")  # any budget.<risk_class>
    assert not rs.is_writable_key("models.spec.model")  # structural -> never live
    assert not rs.is_writable_key("pricing.usd_per_mtok.sonnet")
