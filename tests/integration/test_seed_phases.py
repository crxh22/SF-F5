"""Integration smoke for `cli seed-phases` (phase-seeding design §8, D-0024).

Temp factory git repo with the macro plan COMMITTED + temp workspace git repo
bootstrapped per the runbook (integration branch, committed scripts/test.sh,
non-empty _factory/contracts/), test config with test_command set → seed a
2-phase plan through the REAL cli entry point → drive the REAL scheduler with
stub routes (`run_until_blocked`) → the first phase must reach PLANNING from
the seeded row (the sanctioned path feeds dispatch unchanged).

Fixtures beyond the frozen conftests live locally (design §9); `make_env`
(integration conftest) provides the wired environment + canon files.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from sf_factory import db as fdb
from sf_factory.cli import main as cli_main
from sf_factory.models import Level, Phase, PhaseState


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def _bootstrap_workspace(workspace: Path) -> None:
    """Runbook §3 result on the make_env workspace repo: committed Tier-1
    indirection + non-empty contracts; .worktrees/ ignored (the phase checkout
    make_env pre-created must never be swept into the bootstrap commit)."""
    (workspace / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    (workspace / "scripts").mkdir()
    (workspace / "scripts" / "test.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    contracts = workspace / "_factory" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "api-contract.md").write_text("# ratified contract v0\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "workspace bootstrap: contracts v0 + test indirection")


def _commit_macro_plan(home: Path) -> Path:
    """Factory home as a git repo holding the committed plan (§2.3.4 anchor)."""
    _git(home, "init", "-q", "-b", "main")
    _git(home, "config", "user.email", "factory@test")
    _git(home, "config", "user.name", "factory")
    plan_rel = "docs/projects/proj/macro-plan.json"
    plan_path = home / plan_rel
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        json.dumps(
            {
                "project": "proj",
                "phases": [
                    {"id": "foundation", "name": "Foundation"},
                    {"id": "inventory", "name": "Inventory & Procurement"},
                ],
                "dag_edges": [["foundation", "inventory"]],
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    _git(home, "add", "--", plan_rel)
    _git(home, "commit", "-q", "-m", "macro plan ratified")
    return plan_path


async def test_seed_phases_then_first_phase_reaches_planning(make_env) -> None:
    env = make_env(test_command=["bash", "scripts/test.sh"], use_config_db=True)
    _bootstrap_workspace(env.workspace)
    plan_path = _commit_macro_plan(env.home)
    anchor = _git(env.home, "rev-parse", "HEAD")

    env.cli_init()
    cfg_path = env.home / "factory.config.yaml"  # written by cli_init
    assert cli_main(["-c", str(cfg_path), "seed-phases", str(plan_path)]) == 0

    # The seeded rows are exactly the §2.3.5 shape.
    conn = env.db.read()
    seeded = {p.id: p for p in fdb.list_units(conn, Level.PHASE) if isinstance(p, Phase)}
    assert set(seeded) == {"foundation", "inventory"}
    for phase in seeded.values():
        assert phase.state is PhaseState.PENDING
        assert phase.branch is None and phase.plan_artifact_id is None
        assert phase.project == "proj"
    assert fdb.list_dag_edges(conn, Level.PHASE) == [("foundation", "inventory")]
    ref = fdb.latest_artifact(conn, "factory", "proj", "macro_plan")
    assert ref is not None and ref.git_commit == anchor
    assert len(env.events("foundation", "phase_seeded")) == 1

    # Stub routes, real scheduler: the seeded row dispatches normally — the
    # first phase consumes it into PLANNING (the empty driver playbook then
    # fails the plan contract loudly, which is out of this smoke's scope).
    await env.scheduler().run_until_blocked()

    assert ("PENDING", "PLANNING") in env.transitions("foundation")
    # The PLANNING step ran through the REAL AgentRunner (stub route spawned).
    assert env.processes(role="phase_architect")
    inventory = fdb.get_phase(env.db.read(), "inventory")
    assert inventory is not None and inventory.state is PhaseState.PENDING  # deps gate
