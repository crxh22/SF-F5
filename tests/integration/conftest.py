"""Wave-4 integration fixtures: thin wrappers over tests/integration/harness.py
(design §9: fixtures beyond the frozen tests/conftest.py live locally here).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from harness import (
    CANONICAL_STUB,
    DRIVER_PATH,
    GREEN_SUITE,
    FactoryEnv,
    RecordingNotify,
    _write_canon_files,
    git,
    init_repo,
)

from sf_factory.config import FactoryConfig
from sf_factory.db import MIGRATIONS_DIR, Database


@pytest.fixture()
def make_env(db, config_dict, tmp_path: Path, monkeypatch):
    """Build a FactoryEnv: real workspace repo + phase branch checkout, config
    routed at the playbook driver (or the canonical stub), canon files on disk.

    ``use_config_db=True`` (subprocess-orchestrator tests) binds the env to the
    database at ``process.db_path`` — the file `sf-factory resume` opens — and
    migrates it; the test then reads it concurrently (WAL) while seeding only
    before/between runs.
    """
    envs: list[FactoryEnv] = []
    extra_dbs: list[Database] = []

    def _build(
        *,
        stub: str = "driver",
        test_command: list[str] | None = None,
        config_overrides: dict[str, dict[str, Any]] | None = None,
        use_config_db: bool = False,
    ) -> FactoryEnv:
        home = Path(config_dict["factory"]["home"])
        _write_canon_files(home)
        workspace = Path(config_dict["projects"]["proj"]["workspace"])
        seed = init_repo(workspace)

        config_dict["process"]["stub_agent_path"] = str(
            DRIVER_PATH if stub == "driver" else CANONICAL_STUB
        )
        config_dict["process"]["loop_tick_s"] = 0.05
        config_dict["projects"]["proj"]["test_command"] = list(
            test_command if test_command is not None else GREEN_SUITE
        )
        for section, values in (config_overrides or {}).items():
            config_dict[section].update(values)
        cfg = FactoryConfig.model_validate(config_dict)

        worktrees_dir = Path(config_dict["projects"]["proj"]["worktrees_dir"])
        playbook = tmp_path / "playbook.json"
        playbook.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("SF_DRIVER_PLAYBOOK", str(playbook))

        if use_config_db:
            db_path = Path(config_dict["process"]["db_path"])
            db_path.parent.mkdir(parents=True, exist_ok=True)
            env_db = Database(db_path, config_dict["process"]["db_busy_timeout_ms"])
            env_db.open()
            env_db.migrate(MIGRATIONS_DIR)
            extra_dbs.append(env_db)
        else:
            env_db = db

        env = FactoryEnv(
            cfg=cfg,
            db=env_db,
            home=home,
            workspace=workspace,
            worktrees_dir=worktrees_dir,
            seed_commit=seed,
            playbook_path=playbook,
            notify=RecordingNotify(),
            config_data=json.loads(json.dumps(config_dict)),
        )
        # Phase integration branch + its durable checkout (created at phase
        # dispatch in production; pre-created for surgically seeded stages).
        worktrees_dir.mkdir(parents=True, exist_ok=True)
        git(
            "worktree",
            "add",
            "-q",
            "-b",
            env.phase_branch,
            str(env.phase_checkout),
            "main",
            cwd=workspace,
        )
        envs.append(env)
        return env

    yield _build
    for env in envs:
        env.cleanup()
    for extra in extra_dbs:
        extra.close()
