"""Load + pydantic-validate ``factory.config.yaml`` into ``FactoryConfig`` (design §4).

Typed mirror of the config file — every tunable is read from here by key, never
hardcoded (Doctrine §14). ``extra='forbid'`` everywhere: an unknown key is a
config defect, not a silent passenger. Cross-field checks live in
``FactoryConfig`` validators; any defect raises ``ConfigError`` (design §6:
abort startup, no factory without valid config).

May import: models (+ stdlib, pydantic, yaml per stack decision D-0007).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from sf_factory.models import ConfigError


class _StrictModel(BaseModel):
    """Base for all config sections: unknown keys rejected."""

    model_config = ConfigDict(extra="forbid")


class ModelRoute(_StrictModel):
    """cli: Literal['claude','codex','stub']; model: str; mode: Literal['print','interactive'];
    tools: Literal['all','none'] = 'all' (CCR-3/D-0017: tools-off Decision Sessions —
    structural no-write enforcement; honored by the runner adapters);
    effort: Literal['low','medium','high','xhigh','max'] | None = None (CCR-6:
    per-role claude `--effort` reasoning knob, §5.1 argv literal — cross-checked
    claude-only in FactoryConfig; codex/stub have no effort knob)."""

    cli: Literal["claude", "codex", "stub"]
    model: str
    mode: Literal["print", "interactive"]
    tools: Literal["all", "none"] = "all"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


class ConsultationPointCfg(_StrictModel):
    """id, purpose, inputs: list[str], verdicts: list[str], fallback: str, role: str,
    max_input_bytes: int."""

    id: str
    purpose: str
    inputs: list[str]
    verdicts: list[str]
    fallback: str
    role: str
    max_input_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def _check_closed_set(self) -> ConsultationPointCfg:
        if not self.verdicts:
            raise ValueError(f"consultation point {self.id}: empty verdict set")
        if len(set(self.verdicts)) != len(self.verdicts):
            raise ValueError(f"consultation point {self.id}: duplicate verdicts")
        if self.fallback not in self.verdicts:
            raise ValueError(
                f"consultation point {self.id}: fallback {self.fallback!r} "
                f"not in verdicts {self.verdicts}"
            )
        if len(set(self.inputs)) != len(self.inputs):
            raise ValueError(f"consultation point {self.id}: duplicate inputs")
        return self


class FactorySection(_StrictModel):
    home: Path
    timezone_founder: str


class ProjectCfg(_StrictModel):
    docs_repo: Path
    workspace: Path
    integration_branch: str
    worktrees_dir: Path
    # OPEN-2: null until the founder sets the canonical suite command; string or argv list.
    test_command: str | list[str] | None
    proving_phases: list[str]
    # Phase-seeding design §4: factory-repo-relative PROJECT.md path; None ⇒ the
    # Phase Architect project-context block is omitted (synthetic/b8 projects).
    project_md: Path | None = None


class BudgetsCfg(_StrictModel):
    per_stage: dict[str, int]  # keys mirror risk_classes — cross-checked in FactoryConfig
    usage_missing_policy: Literal["estimate", "escalate_after"]
    usage_missing_max_per_stage: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_budgets(self) -> BudgetsCfg:
        for risk_class, cap in self.per_stage.items():
            if cap <= 0:
                raise ValueError(f"budgets.per_stage.{risk_class}: cap must be > 0, got {cap}")
        return self


class EscalationCfg(_StrictModel):
    max_fix_iterations: int = Field(ge=1)
    churn_threshold: int = Field(ge=1)
    churn_region_lines: int = Field(ge=1)  # divisor of the churn bucket — zero would crash
    max_context_resets: int = Field(ge=0)
    decision_latency_alert_h: int = Field(ge=1)


class RiskClassCfg(_StrictModel):
    validator: str
    audits: list[str]
    human_gate: bool = False


class EconomicsCfg(_StrictModel):
    dual_audit_structural: bool


class NtfyCfg(_StrictModel):
    server: str
    topic: str
    priority_decision: str
    priority_alert: str
    # Float: a hung ntfy connection must never stall the loop (§7); ints coerce losslessly.
    timeout_s: float = Field(gt=0)


class DashboardCfg(_StrictModel):
    """founder_channel.dashboard — keys ratified by D-0017 (dashboard design §6).

    Defaults are the ratified values; ``port`` admits 0 (ephemeral bind — the
    dashboard-design §8 integration tests bind 127.0.0.1:0 and read the real
    port from ``DashboardServer.bound_address``). Durations are floats so tests
    may run sub-second (config ints coerce losslessly).
    """

    bind: str
    port: int = Field(ge=0, lt=65536)
    refresh_s: int = Field(default=30, ge=1)
    #: Bounds every POST thread→loop marshal (answer + session message) — §1/§6.
    answer_timeout_s: float = Field(default=60, gt=0)
    #: Bounds GET marshals (session page/poll snapshots) AND the per-socket
    #: read timeout (slow/hung clients tie up one daemon thread, bounded).
    read_timeout_s: float = Field(default=10, gt=0)
    max_request_bytes: int = Field(default=65536, ge=1)
    restart_delay_s: float = Field(default=30, gt=0)
    page_every_n_restarts: int = Field(default=20, ge=1)
    bind_recheck_s: float = Field(default=300, gt=0)


class DecisionSessionCfg(_StrictModel):
    """founder_channel.decision_session (OPEN-4 slice, ratified D-0017)."""

    max_turns: int = Field(default=10, ge=1)
    turn_timeout_s: int = Field(default=300, ge=1)
    budget_tokens: int = Field(default=200000, ge=1)
    poll_s: float = Field(default=3, gt=0)


class WatchdogCfg(_StrictModel):
    check_interval_s: int = Field(ge=1)
    staleness_threshold_s: int = Field(ge=1)


class FounderChannelCfg(_StrictModel):
    ntfy: NtfyCfg
    dashboard: DashboardCfg
    watchdog: WatchdogCfg
    decision_session: DecisionSessionCfg
    #: CCR-6: lowercase substrings the scheduler's usage-limit detector matches
    #: case-insensitively against agent result text + the stderr tail
    #: (provenance: the D-0021 billing-403 incident class — founder asked to be
    #: paged on capacity events, 12-06-2026). Default = the ratified list
    #: (DashboardCfg precedent: defaults are the ratified values).
    usage_limit_signatures: list[str] = [
        "subscription access",
        "usage limit",
        "rate limit",
        "limit reached",
    ]

    @model_validator(mode="after")
    def _check_signatures(self) -> FounderChannelCfg:
        if not self.usage_limit_signatures:
            raise ValueError(
                "founder_channel.usage_limit_signatures must be a non-empty list"
            )
        for signature in self.usage_limit_signatures:
            if not signature or signature != signature.lower():
                raise ValueError(
                    "founder_channel.usage_limit_signatures entries must be non-empty"
                    f" lowercase substrings, got {signature!r} (the detector lowercases"
                    " the scanned text, never the signatures)"
                )
        return self


class ProcessCfg(_StrictModel):
    agent_timeout_s: int = Field(ge=1)
    max_parallel_agents: int = Field(ge=1)
    ndjson_log_dir: Path
    db_path: Path
    db_busy_timeout_ms: int = Field(ge=1)
    liveness_file: Path
    pid_file: Path
    # Grace/tick durations are floats so tests may run sub-second; config ints coerce losslessly.
    terminate_grace_s: float = Field(gt=0)
    kill_grace_s: float = Field(gt=0)
    ndjson_max_line_bytes: int = Field(ge=1024)
    test_suite_timeout_s: int = Field(ge=1)
    loop_tick_s: float = Field(gt=0)
    heartbeat_min_interval_s: float = Field(gt=0)
    tier2_max_diff_bytes_per_unit: int = Field(ge=1)
    stub_agent_path: Path
    # Build/test droppings the §3.1 Validator-isolation assertion ignores:
    # fnmatch globs against each porcelain path and its path segments.
    isolation_ignore_globs: list[str] = [
        "__pycache__/",
        "*.pyc",
        ".pytest_cache/",
        ".ruff_cache/",
    ]


class CanonInjectCfg(_StrictModel):
    pipeline_agents: list[str]
    founder_facing: list[str]
    consultation_points: list[str]


class CanonCfg(_StrictModel):
    """Canon-injection map (D-0009): files by key, per-role-class inject lists."""

    files: dict[str, str]
    inject: CanonInjectCfg
    founder_facing_roles: list[str]

    @model_validator(mode="after")
    def _check_inject_refs(self) -> CanonCfg:
        declared = set(self.files)
        for bundle_name in ("pipeline_agents", "founder_facing", "consultation_points"):
            for key in getattr(self.inject, bundle_name):
                if key not in declared:
                    raise ValueError(
                        f"canon.inject.{bundle_name}: {key!r} is not a declared canon.files key"
                    )
        return self


class FactoryConfig(_StrictModel):
    """Typed mirror of factory.config.yaml: factory, projects, models, budgets, escalation,
    risk_classes, economics, consultation_points, founder_channel, process, canon (D-0009).
    extra='forbid' everywhere."""

    factory: FactorySection
    projects: dict[str, ProjectCfg]
    models: dict[str, ModelRoute]
    budgets: BudgetsCfg
    escalation: EscalationCfg
    risk_classes: dict[str, RiskClassCfg]
    economics: EconomicsCfg
    consultation_points: list[ConsultationPointCfg]
    founder_channel: FounderChannelCfg
    process: ProcessCfg
    canon: CanonCfg

    @model_validator(mode="after")
    def _cross_checks(self) -> FactoryConfig:
        # §4: budgets.per_stage keys == risk_classes keys (exact, both directions).
        budget_keys = set(self.budgets.per_stage)
        risk_keys = set(self.risk_classes)
        if budget_keys != risk_keys:
            raise ValueError(
                "budgets.per_stage keys must equal risk_classes keys: "
                f"per_stage={sorted(budget_keys)} risk_classes={sorted(risk_keys)}"
            )
        # §4: risk_classes roles ∈ models.
        for rc_name, rc in self.risk_classes.items():
            if rc.validator not in self.models:
                raise ValueError(
                    f"risk_classes.{rc_name}.validator {rc.validator!r} is not a models.* key"
                )
            for auditor in rc.audits:
                if auditor not in self.models:
                    raise ValueError(
                        f"risk_classes.{rc_name}.audits: {auditor!r} is not a models.* key"
                    )
        # CCR-6: models.<role>.effort is the claude `--effort` flag (§5.1 argv
        # literal) — codex/stub have no effort knob, so a route declaring one
        # would silently no-op (Doctrine §7: fail-explicit at load).
        for role_name, route in self.models.items():
            if route.effort is not None and route.cli != "claude":
                raise ValueError(
                    f"models.{role_name}: effort={route.effort!r} requires cli='claude',"
                    f" got cli={route.cli!r} — codex/stub have no effort knob"
                )
        # Consultation registry: unique ids; each role resolvable to a model route
        # (the runner spawns CP calls via config.models[role], design §4/§5.1).
        seen_ids: set[str] = set()
        for cp in self.consultation_points:
            if cp.id in seen_ids:
                raise ValueError(f"consultation_points: duplicate id {cp.id!r}")
            seen_ids.add(cp.id)
            if cp.role not in self.models:
                raise ValueError(
                    f"consultation_points[{cp.id}].role {cp.role!r} is not a models.* key"
                )
        # Documented relation (design §10 / config comment): watchdog staleness must be
        # >= 10x the scheduler tick, or a healthy orchestrator pages the founder.
        watchdog = self.founder_channel.watchdog
        if watchdog.staleness_threshold_s < 10 * self.process.loop_tick_s:
            raise ValueError(
                "founder_channel.watchdog.staleness_threshold_s "
                f"({watchdog.staleness_threshold_s}) must be >= 10x process.loop_tick_s "
                f"({self.process.loop_tick_s})"
            )
        return self


def load_config(path: Path) -> FactoryConfig:
    """Parse + validate YAML; cross-checks (fallback∈verdicts; risk_classes roles∈models;
    budgets.per_stage keys==risk_classes keys); raises ConfigError."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a mapping, got {type(data).__name__} in {path}")
    try:
        return FactoryConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid config {path}:\n{exc}") from exc
