"""Live-editable factory settings (founder dashboard, 20-06-2026).

The dashboard Configurare tab writes overrides via ``db.set_runtime_setting``;
the scheduler reads ``db.get_runtime_settings(conn)`` each tick and wraps it with
``EffectiveConfig`` to get the live value of each governed parameter, layered
over the load-once ``FactoryConfig``. Survives restart (persisted in the DB).

Structural params (models, prices, ports, risk classes) are NOT governed here —
they stay in YAML and change only on restart. Doctrine §9: the override KEY names
and the override-vs-default precedence live ONCE in this module; every consumer
(scheduler cap, governor gate, runner timeout, thresholds budget) reads through
it, never a raw ``runtime_settings`` row.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sf_factory.config import FactoryConfig

# --- override keys (the runtime_settings.key values) — the SINGLE source -------
KEY_MAX_PARALLEL = "max_parallel_agents"
KEY_AGENT_TIMEOUT = "agent_timeout_s"
KEY_BUDGET_PREFIX = "budget."  # + risk_class, e.g. "budget.critical"
KEY_GOV_5H = "governor.five_hour_threshold_pct"
KEY_GOV_7D = "governor.seven_day_threshold_pct"
#: The „autodrenaj la limită" flag — gates the proactive limit governor on/off.
KEY_GOV_AUTODRENAJ = "governor.autodrenaj"
#: The manual DRAIN<->NORMAL switch (True = DRAIN: hold new agent spawns).
KEY_DRAIN_MANUAL = "drain.manual"

#: Keys the dashboard may write (allow-list — a write to anything else is
#: rejected at the POST boundary). Budget keys are validated by prefix.
WRITABLE_KEYS: frozenset[str] = frozenset(
    {
        KEY_MAX_PARALLEL,
        KEY_AGENT_TIMEOUT,
        KEY_GOV_5H,
        KEY_GOV_7D,
        KEY_GOV_AUTODRENAJ,
        KEY_DRAIN_MANUAL,
    }
)


def budget_key(risk_class: str) -> str:
    """The runtime_settings key for a risk class's per-stage budget override."""
    return f"{KEY_BUDGET_PREFIX}{risk_class}"


def is_writable_key(key: str) -> bool:
    """True for a dashboard-writable key (the allow-list + any budget.<rc>)."""
    return key in WRITABLE_KEYS or key.startswith(KEY_BUDGET_PREFIX)


@dataclass(frozen=True)
class EffectiveConfig:
    """The LIVE config: DB overrides layered over the load-once FactoryConfig.

    Built once per scheduler tick — ``EffectiveConfig(db.get_runtime_settings(conn),
    cfg)`` — and read by property. An absent/None override falls back to the YAML
    value; a present override wins. Pure (no DB/I/O) so it is trivially testable.
    """

    overrides: Mapping[str, object]
    cfg: FactoryConfig

    @property
    def max_parallel_agents(self) -> int:
        v = self.overrides.get(KEY_MAX_PARALLEL)
        return int(v) if v is not None else self.cfg.process.max_parallel_agents

    @property
    def agent_timeout_s(self) -> int:
        v = self.overrides.get(KEY_AGENT_TIMEOUT)
        return int(v) if v is not None else self.cfg.process.agent_timeout_s

    def budget(self, risk_class: str) -> int | None:
        """Effective per-stage token budget for a risk class (None when neither
        an override nor a YAML entry exists — a config/DB drift the caller flags)."""
        v = self.overrides.get(budget_key(risk_class))
        if v is not None:
            return int(v)
        return self.cfg.budgets.per_stage.get(risk_class)

    @property
    def gov_five_hour_pct(self) -> float:
        v = self.overrides.get(KEY_GOV_5H)
        return float(v) if v is not None else self.cfg.capacity_governor.five_hour_threshold_pct

    @property
    def gov_seven_day_pct(self) -> float:
        v = self.overrides.get(KEY_GOV_7D)
        return float(v) if v is not None else self.cfg.capacity_governor.seven_day_threshold_pct

    @property
    def autodrenaj(self) -> bool:
        """The „autodrenaj la limită" flag — when True the proactive limit
        governor may hold new spawns near the API caps. Defaults to the YAML
        ``capacity_governor.proactive_enabled`` (off by default) until the
        founder flips it from the dashboard."""
        v = self.overrides.get(KEY_GOV_AUTODRENAJ)
        return bool(v) if v is not None else self.cfg.capacity_governor.proactive_enabled

    @property
    def drain_manual(self) -> bool:
        """The manual DRAIN<->NORMAL switch — True holds new agent spawns (the
        running ones finish). No YAML fallback: defaults NORMAL (False)."""
        return bool(self.overrides.get(KEY_DRAIN_MANUAL, False))
