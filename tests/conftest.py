"""Shared fixtures for all waves (design §8): tmp-path DB + minimal FactoryConfig.

FROZEN with wave 1 (design §9): later builders define any additional fixtures
locally in their own test modules — this file is never edited concurrently.

Fixture contract:
- ``db_path``        tmp path for the SQLite file (open extra connections on it freely).
- ``db``             opened + fully migrated ``Database`` (WAL, rw), closed on teardown.
- ``config_dict``    plain-dict minimal config (tmp paths, stub CLI routes, fast
                     timeouts). Mutate it in a test, then build a FactoryConfig —
                     the supported way to vary config without touching this file.
- ``factory_config`` validated minimal ``FactoryConfig`` built from ``config_dict``.
- ``real_config_path`` path to the repo's real factory.config.yaml (golden tests).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sf_factory.config import FactoryConfig
from sf_factory.db import MIGRATIONS_DIR, Database

REPO_ROOT = Path(__file__).resolve().parent.parent

_STUB_ROUTE: dict[str, str] = {"cli": "stub", "model": "stub-model", "mode": "print"}

#: Role keys mirror the real factory.config.yaml so later-wave tests exercise the
#: real routing vocabulary, but every route points at the stub CLI.
_STUB_ROLES = (
    "phase_architect",
    "spec_agent",
    "builder_routine",
    "builder_heavy",
    "validator",
    "validator_structural",
    "integration_validator",
    "auditor_same_model",
    "auditor_cross_model",
    "cp1_triage",
)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Location of the test database file (under pytest's tmp_path)."""
    return tmp_path / "factory.db"


@pytest.fixture()
def db(db_path: Path):
    """Open + migrated Database on a tmp path; closed at teardown."""
    database = Database(db_path, busy_timeout_ms=5000)
    database.open()
    database.migrate(MIGRATIONS_DIR)
    yield database
    database.close()


@pytest.fixture()
def real_config_path() -> Path:
    """The real factory.config.yaml — the config golden-test input (design §8)."""
    path = REPO_ROOT / "factory.config.yaml"
    assert path.is_file(), f"real factory.config.yaml not found at {path}"
    return path


@pytest.fixture()
def config_dict(tmp_path: Path) -> dict[str, Any]:
    """Minimal-but-complete config data: tmp paths, stub routes, fast timeouts.

    Tests mutate this dict (then validate via FactoryConfig.model_validate or the
    ``factory_config`` fixture) instead of editing this frozen file.
    """
    workspace = tmp_path / "workspace"
    state_dir = tmp_path / ".factory"
    models: dict[str, Any] = {name: dict(_STUB_ROUTE) for name in _STUB_ROLES}
    models["main_architect"] = {"cli": "stub", "model": "stub-model", "mode": "interactive"}
    return {
        "factory": {"home": str(tmp_path), "timezone_founder": "Europe/Chisinau"},
        "projects": {
            "proj": {
                "docs_repo": str(tmp_path / "docs-repo"),
                "workspace": str(workspace),
                "integration_branch": "main",
                "worktrees_dir": str(workspace / ".worktrees"),
                "test_command": None,
                "proving_phases": ["foundation"],
            }
        },
        "models": models,
        "budgets": {
            "per_stage": {"routine": 10000, "structural": 20000, "critical": 40000},
            "usage_missing_policy": "estimate",
            "usage_missing_max_per_stage": 3,
        },
        "escalation": {
            "max_fix_iterations": 3,
            "churn_threshold": 4,
            "churn_region_lines": 40,
            "max_context_resets": 1,
            "decision_latency_alert_h": 24,
        },
        "risk_classes": {
            "routine": {"validator": "validator", "audits": []},
            "structural": {
                "validator": "validator_structural",
                "audits": ["auditor_same_model", "auditor_cross_model"],
            },
            "critical": {
                "validator": "validator_structural",
                "audits": ["auditor_same_model", "auditor_cross_model"],
                "human_gate": True,
            },
        },
        "economics": {"dual_audit_structural": True},
        "consultation_points": [
            {
                "id": "CP-1",
                "purpose": "feedback-loop triage when deterministic thresholds do not decide",
                "inputs": ["validation_report", "diff_digest", "spec"],
                "verdicts": ["continue_session", "rebuild", "respec", "escalate"],
                "fallback": "escalate",
                "role": "cp1_triage",
                "max_input_bytes": 200000,
            }
        ],
        "founder_channel": {
            "ntfy": {
                "server": "http://127.0.0.1:1",  # unroutable on purpose: tests stub publishing
                "topic": "sf-f5-test-topic",
                "priority_decision": "high",
                "priority_alert": "max",
                "timeout_s": 2,
            },
            "dashboard": {"bind": "tailscale", "port": 8377},
            "watchdog": {"check_interval_s": 1, "staleness_threshold_s": 10},
            "decision_session": {},
        },
        "process": {
            "agent_timeout_s": 30,
            "max_parallel_agents": 2,
            "ndjson_log_dir": str(state_dir / "logs"),
            "db_path": str(state_dir / "factory.db"),
            "db_busy_timeout_ms": 5000,
            "liveness_file": str(state_dir / "liveness"),
            "pid_file": str(state_dir / "orchestrator.pid"),
            "terminate_grace_s": 2,
            "kill_grace_s": 1,
            "ndjson_max_line_bytes": 65536,
            "test_suite_timeout_s": 60,
            "loop_tick_s": 1,
            "heartbeat_min_interval_s": 1,
            "tier2_max_diff_bytes_per_unit": 100000,
            "stub_agent_path": str(REPO_ROOT / "tests" / "stub_agent.py"),
        },
        "canon": {
            "files": {
                "doctrine": "00 - DOCTRINA.md",
                "conventions": "work-protocols/conventions.md",
                "founder_protocol": "work-protocols/protocol_interactiune_founder.md",
                "architect_ops": "work-protocols/architect-operations.md",
            },
            "inject": {
                "pipeline_agents": ["doctrine", "conventions"],
                "founder_facing": ["doctrine", "conventions", "founder_protocol"],
                "consultation_points": [],
                "architect": ["architect_ops"],
            },
            "architect_roles": ["main_architect", "phase_architect", "spec_agent"],
            "founder_facing_roles": [
                "intake",
                "decision_session",
                "main_architect",
                "phase_architect",
                "notify",
            ],
        },
    }


@pytest.fixture()
def factory_config(config_dict: dict[str, Any]) -> FactoryConfig:
    """Validated minimal FactoryConfig (design §8)."""
    return FactoryConfig.model_validate(config_dict)
