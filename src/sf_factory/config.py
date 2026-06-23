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


#: Built-in claude tool names a route allowlist may name (CLI-verified against the
#: installed claude `--tools` set, 2026-06-15). The set is a TYPO guardrail, not an
#: exhaustive registry — the runner passes whatever names survive validation straight
#: to `--tools`, so an unknown-but-real future tool fails here rather than silently
#: drifting. Extend this list when a route legitimately needs a tool not yet named.
_KNOWN_CLAUDE_TOOLS: frozenset[str] = frozenset(
    {
        "Task",
        "Bash",
        "Glob",
        "Grep",
        "Read",
        "Edit",
        "Write",
        "NotebookEdit",
        "WebFetch",
        "WebSearch",
    }
)


class ModelRoute(_StrictModel):
    """cli: Literal['claude','codex','stub']; model: str; mode: Literal['print','interactive'];
    tools: Literal['all','none'] | list[str] = 'all' (CCR-3/D-0017: tools-off Decision
    Sessions — structural no-write enforcement; honored by the runner adapters). A LIST
    is a per-role ALLOWLIST (context-budget fix, 2026-06-15): the claude adapter spawns
    with `--tools <name> <name> …` so only those built-in tools load — the merge-gate
    integration_validator carries the full 32-tool built-in set (~half its prompt) but
    only needs Read+Write, and the unused schemas pushed its opus prompt past the 1M
    window. Validated as a non-empty list of unique known tool names (see
    ``_KNOWN_CLAUDE_TOOLS``); 'all'/'none' keep their CCR-3 meaning.
    effort: Literal['low','medium','high','xhigh','max'] | None = None (CCR-6/D-0038:
    per-role reasoning knob — claude `--effort <v>`; codex `-c model_reasoning_effort=`
    (codex tops at 'xhigh', 'max' is claude-only); cross-checked in FactoryConfig;
    the stub CLI has no effort knob)."""

    cli: Literal["claude", "codex", "stub"]
    model: str
    mode: Literal["print", "interactive"]
    tools: Literal["all", "none"] | list[str] = "all"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None

    @model_validator(mode="after")
    def _check_tools_allowlist(self) -> ModelRoute:
        """A tool-list ``tools`` is a per-role allowlist: it must be a non-empty
        list of unique, non-empty, known built-in tool names (fail-explicit at
        load, Doctrine §7 — a typo'd or empty allowlist would spawn an agent that
        cannot do its job). 'all'/'none' are unaffected."""
        if isinstance(self.tools, str):
            return self
        if not self.tools:
            raise ValueError(
                "tools allowlist must be a non-empty list of built-in tool names "
                "(use 'none' to disable all tools, 'all' for the full set)"
            )
        seen: set[str] = set()
        for name in self.tools:
            if not isinstance(name, str) or not name.strip():
                raise ValueError(
                    f"tools allowlist entries must be non-empty tool-name strings, got {name!r}"
                )
            if name in seen:
                raise ValueError(f"tools allowlist has duplicate entry {name!r}")
            seen.add(name)
            if name not in _KNOWN_CLAUDE_TOOLS:
                raise ValueError(
                    f"tools allowlist names unknown built-in tool {name!r} "
                    f"(known: {sorted(_KNOWN_CLAUDE_TOOLS)})"
                )
        return self


class ModelPrice(_StrictModel):
    """USD per 1M tokens for one ledger model string (design §11.1, CCR-10):
    input/output list prices, founder-tunable. Used ONLY where the CLI reports
    no exact cost (token_ledger.cost_usd NULL) — estimates render with `~`."""

    input: float = Field(gt=0)
    output: float = Field(gt=0)


class PricingCfg(_StrictModel):
    """pricing.usd_per_mtok.<ledger-model> -> ModelPrice (design §11.1). Keys are
    LEDGER model strings (`fable`, `sonnet`, `haiku`, `default`, …). A NULL-cost
    row whose model has no key renders the explicit missing-price marker —
    fail-visible, never a silent zero (Doctrine §7)."""

    usd_per_mtok: dict[str, ModelPrice] = {}


class CapacityGovernorCfg(_StrictModel):
    """capacity_governor (CCR-11 / D-0037): auto-drain on a usage-limit
    detection, periodic haiku probe, auto-resume of limit-class failures.
    ``enabled: false`` ⇒ scheduler behavior byte-identical to pre-CCR-11
    (pinned by test). The default is DISABLED — the golden config flips it on
    explicitly (the optional-``pricing`` precedent: minimal fixtures predate
    the section and must keep validating without a capacity_probe route)."""

    enabled: bool = False
    probe_interval_s: float = Field(default=300, gt=0)
    #: On hold LIFT also page the architect ("reia lucrul") so it resumes
    #: autonomously (D-0042, robustness UNIT 3). Default-on preserves the
    #: durable resume signal; the founder (noise-sensitive) can suppress it.
    notify_architect_on_resume: bool = True

    #: PROACTIVE %-threshold limit management (D-0059, founder-directed
    #: 19-06-2026). The orchestrator OAuth-polls the LIVE usage every
    #: ``proactive_poll_interval_s`` and, BEFORE the wall, holds new claude
    #: spawns once the 5h OR weekly utilization crosses its threshold —
    #: running agents finish (the SAME ``blocks`` gate as the reactive hold),
    #: and the hold lifts on its own once BOTH utilizations fall back under
    #: their thresholds (i.e. after the reset). The reactive ``note_match``
    #: signature path stays the backstop (Doctrine §7/§20). Gated UNDER
    #: ``enabled`` so ``enabled: false`` stays byte-identical to pre-CCR-11.
    proactive_enabled: bool = False
    proactive_poll_interval_s: float = Field(default=300, gt=0)
    #: Hold when five_hour utilization% >= this (founder: 80 for the 5h window).
    five_hour_threshold_pct: float = Field(default=80, gt=0, le=100)
    #: Hold when seven_day utilization% >= this (founder: 90 for the weekly
    #: window — the longer-binding limit, drained later than the 5h).
    seven_day_threshold_pct: float = Field(default=90, gt=0, le=100)
    #: OAuth usage source (sf-limit.sh parity, D-0058): GET endpoint, the beta
    #: header it requires, and the credentials file holding the bearer token
    #: (``claudeAiOauth.accessToken``). ``~`` is expanded at query time.
    usage_endpoint: str = "https://api.anthropic.com/api/oauth/usage"
    usage_beta_header: str = "oauth-2025-04-20"
    oauth_credentials_path: str = "~/.claude/.credentials.json"
    usage_poll_timeout_s: float = Field(default=20, gt=0)


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
    # Option A (founder-ratified pre-authored plans): factory-home-relative dir
    # holding pre-authored phase-plan.{json,md} per phase id (<dir>/<phase-id>/);
    # None ⇒ legacy — the Phase Architect authors the stage decomposition itself.
    # When set and a phase's plan is present, PLANNING adopts it byte-exactly and
    # narrows the architect to authoring contracts only (mechanical guarantee:
    # ingested stages == approved stages).
    prefrozen_phase_plans: Path | None = None


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
    # robustness UNIT 2 / D-0042 (T_architect): an escalation OPEN longer than this
    # (or RESOLVED-but-unit-still-ESCALATED longer than this) -> the stuck-detector
    # bumps target one rung + pages the architect. Minutes; Doctrine §14.
    stuck_escalation_threshold_min: int = Field(ge=1)
    # Loop-cap (incident 22-06): a stage whose merge-gate Tier-1 SUITE fails this
    # many times since its last escalation (the builder cannot fix it — a no-op
    # rework loop; env/infra, not a code defect) is ESCALATED instead of
    # re-looping forever (Doctrine §8/§20). Defaulted so existing/test configs
    # validate without it; the live YAML sets it explicitly.
    merge_gate_max_tier1_failures: int = Field(ge=1, default=3)
    # Spec dual-audit rework loop-cap: a stage whose SPEC_AUDIT step loops back to
    # SPEC (a real spec defect the spec agent reworks) this many times since its
    # last FRESH spec entry is ESCALATED instead of looping forever (the BLOCKING
    # spec-rework loop's bound; mirrors merge_gate_max_tier1_failures, Doctrine
    # §8/§20). Defaulted so existing/test configs validate without it.
    spec_audit_max_rework: int = Field(ge=1, default=2)


class RiskClassCfg(_StrictModel):
    validator: str
    audits: list[str]
    #: Spec dual-audit roles (config models.* keys) — the auditors that review
    #: spec.md after SPEC and before BUILD. EMPTY default = backward-compatible:
    #: the SPEC_AUDIT step is never reached, SPEC goes straight to BUILD (the
    #: change is INERT for any risk class that does not opt in). The golden config
    #: opts ALL classes in. Cross-checked against models in FactoryConfig.
    spec_audits: list[str] = []
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
    # D-0046: total-prompt byte ceiling for the Tier-2 integration_validator. Above
    # it, sibling diffs render as file+@@ hunk headers (not full bodies) so the
    # print-mode prompt stays under the agent's 1M context window.
    tier2_max_total_bytes: int = Field(ge=1)
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
    # D-0040: an ADDITIVE layer (not an exclusive bundle) — appended on top of a
    # role's base bundle when the role is in canon.architect_roles. Default empty
    # = pre-D-0040 behavior (no architect layer). The Main-Architect session takes
    # this layer via the launcher's flat CANON_FILES instead.
    architect: list[str] = []
    # An ADDITIVE front-gated layer — appended on top of the base bundle for a
    # stage agent (builder/validator/auditor) only when the stage's kind is
    # 'frontend' (runner._canon_text stage_kind). Default empty = no frontend
    # layer. Never composed for consultation calls (pure functions) or backend/
    # kind=None stages.
    frontend: list[str] = []


class CanonCfg(_StrictModel):
    """Canon-injection map (D-0009): files by key, per-role-class inject lists."""

    files: dict[str, str]
    inject: CanonInjectCfg
    founder_facing_roles: list[str]
    # D-0040: roles that receive the additive architect layer on top of their base
    # bundle (composition, not an exclusive bundle — a role may be both
    # founder-facing AND architect and gets the union). Default empty.
    architect_roles: list[str] = []

    @model_validator(mode="after")
    def _check_inject_refs(self) -> CanonCfg:
        declared = set(self.files)
        bundles = (
            "pipeline_agents",
            "founder_facing",
            "consultation_points",
            "architect",
            "frontend",
        )
        for bundle_name in bundles:
            for key in getattr(self.inject, bundle_name):
                if key not in declared:
                    raise ValueError(
                        f"canon.inject.{bundle_name}: {key!r} is not a declared canon.files key"
                    )
        return self


class StageSizeLimitsCfg(_StrictModel):
    """planning.stage_size_limits — bounds for the mechanical small-stage gate
    (integration safety net, step-5; Doctrine §14 tunables). Upper bounds cap a stage
    at one-pass builder confidence; the floor (min_*) flags over-split. Defaults are the
    founder-locked starter values; the cross-check pins each min < its max."""

    max_acceptance_criteria: int = Field(ge=1, default=7)
    max_touched: int = Field(ge=1, default=6)
    max_dependency_degree: int = Field(ge=1, default=6)
    min_acceptance_criteria: int = Field(ge=0, default=1)
    min_touched: int = Field(ge=0, default=1)

    @model_validator(mode="after")
    def _check_floor_below_ceiling(self) -> StageSizeLimitsCfg:
        if self.min_acceptance_criteria >= self.max_acceptance_criteria:
            raise ValueError(
                "planning.stage_size_limits.min_acceptance_criteria "
                f"({self.min_acceptance_criteria}) must be < max_acceptance_criteria "
                f"({self.max_acceptance_criteria})"
            )
        if self.min_touched >= self.max_touched:
            raise ValueError(
                "planning.stage_size_limits.min_touched "
                f"({self.min_touched}) must be < max_touched ({self.max_touched})"
            )
        return self


class PlanningCfg(_StrictModel):
    """planning (integration safety net, step-5): OPTIONAL top-level section, default
    all-baseline — the size-gate thresholds + its mode. ``stage_size_gate_mode`` starts
    at 'warn' (report + escalate, never block); 'hard' wires the blocking path (NOT the
    default). The pricing/capacity_governor precedent: minimal fixtures predate the
    section and must keep validating without it."""

    stage_size_limits: StageSizeLimitsCfg = StageSizeLimitsCfg()
    stage_size_gate_mode: Literal["warn", "hard"] = "warn"


class FactoryConfig(_StrictModel):
    """Typed mirror of factory.config.yaml: factory, projects, models, budgets, escalation,
    risk_classes, economics, consultation_points, founder_channel, process, canon (D-0009),
    pricing (CCR-10, optional), planning (step-5, optional). extra='forbid' everywhere."""

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
    #: CCR-10 (design §11.3.1): OPTIONAL top-level section, default empty — the
    #: golden config carries the real table; minimal test fixtures need none.
    pricing: PricingCfg = PricingCfg()
    #: CCR-11 (D-0037): OPTIONAL top-level section, default DISABLED — the
    #: golden config enables it and declares the models.capacity_probe route.
    capacity_governor: CapacityGovernorCfg = CapacityGovernorCfg()
    #: step-5 (integration safety net): OPTIONAL top-level section, default
    #: all-baseline (size limits + 'warn' mode) — the golden config carries it
    #: explicitly; minimal test fixtures predate it and validate without it.
    planning: PlanningCfg = PlanningCfg()

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
            for spec_auditor in rc.spec_audits:
                if spec_auditor not in self.models:
                    raise ValueError(
                        f"risk_classes.{rc_name}.spec_audits: {spec_auditor!r}"
                        " is not a models.* key"
                    )
        # CCR-6 + D-0038: per-role reasoning effort. claude → `--effort <v>`
        # (§5.1 argv literal); codex (gpt-5.x) → `-c model_reasoning_effort="<v>"`
        # (codex tops at 'xhigh' — 'max' is claude-only). The stub CLI has no
        # effort knob. A misrouted/over-ranged effort fails at load, never silently
        # no-ops (Doctrine §7).
        codex_efforts = {"low", "medium", "high", "xhigh"}
        for role_name, route in self.models.items():
            if route.effort is None:
                continue
            if route.cli == "claude":
                continue
            if route.cli == "codex":
                if route.effort not in codex_efforts:
                    raise ValueError(
                        f"models.{role_name}: effort={route.effort!r} is not a codex "
                        f"reasoning level ({sorted(codex_efforts)}; 'max' is claude-only)"
                    )
                continue
            raise ValueError(
                f"models.{role_name}: effort={route.effort!r} requires cli='claude' or "
                f"'codex', got cli={route.cli!r} — the stub CLI has no effort knob"
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
        # CCR-11 (D-0037): an ENABLED capacity governor needs its canary route —
        # the probe is the only hold exit, so a missing/non-print route would
        # wedge every hold forever (Doctrine §7: fail-explicit at load).
        if self.capacity_governor.enabled:
            probe = self.models.get("capacity_probe")
            if probe is None:
                raise ValueError(
                    "capacity_governor.enabled requires a models.capacity_probe"
                    " route (the hold-exit canary, CCR-11/D-0037)"
                )
            if probe.mode != "print":
                raise ValueError(
                    "models.capacity_probe must be mode='print' — the governor"
                    f" probes through the print-mode runner, got {probe.mode!r}"
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
