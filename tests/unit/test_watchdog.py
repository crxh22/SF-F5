"""Unit tests for watchdog.py (design §8): pid + cmdline match, liveness mtime
staleness, startup/recovery grace via a young pidfile, max-priority publish on
failure (stubbed — never the real ntfy), file-only reads anchored at
factory.home, and main()'s cron exit codes.

tests/conftest.py is frozen (design §9): all extra fixtures live here.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

import sf_factory.watchdog as watchdog
from sf_factory.config import FactoryConfig
from sf_factory.models import NotifyError
from sf_factory.notify import NtfyPublisher
from sf_factory.watchdog import check_once, main

# --------------------------------------------------------------- local fixtures


@pytest.fixture()
def publish_calls(monkeypatch) -> list[dict[str, Any]]:
    """Record NtfyPublisher.publish calls instead of doing HTTP."""
    calls: list[dict[str, Any]] = []

    async def fake_publish(self, title: str, *, link: str | None = None,
                           priority: str = "default") -> None:
        calls.append({"title": title, "link": link, "priority": priority})

    monkeypatch.setattr(NtfyPublisher, "publish", fake_publish)
    return calls


@pytest.fixture()
def spawn():
    """Spawn helper child processes; kill + reap them at teardown."""
    children: list[subprocess.Popen] = []

    def _spawn(argv: list[str]) -> subprocess.Popen:
        child = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append(child)
        return child

    yield _spawn
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait()


def _paths(cfg: FactoryConfig) -> tuple[Path, Path]:
    """(pid_path, liveness_path) as the watchdog resolves them."""
    home = cfg.factory.home
    return (
        watchdog._resolve(home, cfg.process.pid_file),
        watchdog._resolve(home, cfg.process.liveness_file),
    )


def _age(path: Path, seconds: float) -> None:
    """Backdate a file's mtime by ``seconds``."""
    past = time.time() - seconds
    os.utime(path, (past, past))


def _write_pidfile(path: Path, content: str, *, age_s: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if age_s is not None:
        _age(path, age_s)


def _touch_liveness(path: Path, *, age_s: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    if age_s is not None:
        _age(path, age_s)


def _own_pidfile_content() -> str:
    """pid + cmdline of THIS test process — a genuinely alive, matching entry."""
    live = watchdog._proc_cmdline(os.getpid())
    assert live  # the test process must be visible in /proc
    return f"{os.getpid()}\n{live}\n"


# ----------------------------------------------------------------- healthy path


def test_healthy_orchestrator_is_silent(factory_config, publish_calls) -> None:
    pid_path, liveness_path = _paths(factory_config)
    _write_pidfile(pid_path, _own_pidfile_content(), age_s=3600)  # past grace
    _touch_liveness(liveness_path)

    assert check_once(factory_config) is True
    assert publish_calls == []  # silence means healthy (DoD §9)


def test_fresh_pidfile_grants_startup_grace(factory_config, publish_calls) -> None:
    """A just-(re)started orchestrator must not page even before liveness exists."""
    pid_path, _ = _paths(factory_config)
    _write_pidfile(pid_path, "garbage not even a pid")  # mtime = now

    assert check_once(factory_config) is True
    assert publish_calls == []


def test_recorded_cmdline_exact_match_counts_alive(factory_config, publish_calls, spawn) -> None:
    child = spawn(["sleep", "60"])
    pid_path, liveness_path = _paths(factory_config)
    _write_pidfile(pid_path, f"{child.pid}\nsleep 60\n", age_s=3600)
    _touch_liveness(liveness_path)

    assert check_once(factory_config) is True
    assert publish_calls == []


def test_pid_only_pidfile_falls_back_to_package_token(
    factory_config, publish_calls, spawn
) -> None:
    child = spawn([sys.executable, "-c", "import time; time.sleep(60)", "sf_factory"])
    pid_path, liveness_path = _paths(factory_config)
    _write_pidfile(pid_path, f"{child.pid}\n", age_s=3600)  # no recorded cmdline
    _touch_liveness(liveness_path)

    assert check_once(factory_config) is True
    assert publish_calls == []


# ---------------------------------------------------------------- failure paths


def test_missing_pidfile_pages_at_max_priority(factory_config, publish_calls) -> None:
    _, liveness_path = _paths(factory_config)
    _touch_liveness(liveness_path)  # liveness alone is not enough

    assert check_once(factory_config) is False
    assert len(publish_calls) == 1
    call = publish_calls[0]
    assert call["priority"] == factory_config.founder_channel.ntfy.priority_alert
    assert call["title"]  # founder-facing title present
    assert call["link"] is not None and call["link"].startswith("http://")


def test_dead_pid_pages(factory_config, publish_calls, capsys, spawn) -> None:
    child = spawn(["sleep", "0"])
    child.wait()  # reaped: the pid no longer exists
    pid_path, liveness_path = _paths(factory_config)
    _write_pidfile(pid_path, f"{child.pid}\nsleep 0\n", age_s=3600)
    _touch_liveness(liveness_path)

    assert check_once(factory_config) is False
    assert len(publish_calls) == 1
    assert "orchestrator DOWN" in capsys.readouterr().err


def test_zombie_pid_counts_dead(factory_config, publish_calls, spawn) -> None:
    """A zombie passes kill(pid, 0) but is no orchestrator — must page."""
    child = spawn(["sleep", "0"])
    deadline = time.time() + 10
    while watchdog._proc_cmdline(child.pid) != "":
        assert time.time() < deadline, "child never became a zombie"
        time.sleep(0.02)
    pid_path, liveness_path = _paths(factory_config)
    _write_pidfile(pid_path, f"{child.pid}\n", age_s=3600)
    _touch_liveness(liveness_path)

    assert check_once(factory_config) is False
    assert len(publish_calls) == 1


def test_cmdline_mismatch_pages(factory_config, publish_calls, capsys, spawn) -> None:
    """Pid reuse: live process at the recorded pid is not the orchestrator."""
    child = spawn(["sleep", "61"])
    pid_path, liveness_path = _paths(factory_config)
    _write_pidfile(pid_path, f"{child.pid}\nthe orchestrator that died\n", age_s=3600)
    _touch_liveness(liveness_path)

    assert check_once(factory_config) is False
    assert "is not the orchestrator" in capsys.readouterr().err


def test_stale_liveness_pages_even_with_live_pid(
    factory_config, publish_calls, capsys
) -> None:
    """The wedged-loop case (Doctrine §20): process alive, tick stopped."""
    pid_path, liveness_path = _paths(factory_config)
    _write_pidfile(pid_path, _own_pidfile_content(), age_s=3600)
    _touch_liveness(liveness_path, age_s=3600)

    assert check_once(factory_config) is False
    assert len(publish_calls) == 1
    assert "stale" in capsys.readouterr().err


def test_missing_liveness_pages(factory_config, publish_calls) -> None:
    pid_path, _ = _paths(factory_config)
    _write_pidfile(pid_path, _own_pidfile_content(), age_s=3600)

    assert check_once(factory_config) is False
    assert len(publish_calls) == 1


@pytest.mark.parametrize("content", ["", "not-a-pid\n", "0\n", "-7\n"])
def test_unparseable_pidfile_pages(factory_config, publish_calls, content) -> None:
    pid_path, liveness_path = _paths(factory_config)
    _write_pidfile(pid_path, content, age_s=3600)
    _touch_liveness(liveness_path)

    assert check_once(factory_config) is False
    assert len(publish_calls) == 1


def test_failed_publish_still_reports_down(factory_config, monkeypatch, capsys) -> None:
    """ntfy down while the factory is down: exit path stays honest (no crash)."""

    async def failing_publish(self, title: str, *, link: str | None = None,
                              priority: str = "default") -> None:
        raise NotifyError("ntfy unreachable")

    monkeypatch.setattr(NtfyPublisher, "publish", failing_publish)

    assert check_once(factory_config) is False  # nothing on disk at all
    err = capsys.readouterr().err
    assert "orchestrator DOWN" in err
    assert "alert delivery failed" in err


# ------------------------------------------------- path resolution (cron cwd)


def test_relative_paths_resolve_against_factory_home(
    config_dict, publish_calls, tmp_path, monkeypatch
) -> None:
    config_dict["process"]["pid_file"] = ".factory/orchestrator.pid"
    config_dict["process"]["liveness_file"] = ".factory/liveness"
    cfg = FactoryConfig.model_validate(config_dict)
    assert cfg.factory.home == tmp_path
    _write_pidfile(tmp_path / ".factory" / "orchestrator.pid", _own_pidfile_content(),
                   age_s=3600)
    _touch_liveness(tmp_path / ".factory" / "liveness")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # cron-like arbitrary cwd

    assert check_once(cfg) is True
    assert publish_calls == []


# ------------------------------------------------------------------ main / cron


def _write_config_yaml(tmp_path: Path, config_dict: dict[str, Any]) -> Path:
    path = tmp_path / "factory.config.yaml"
    path.write_text(yaml.safe_dump(config_dict), encoding="utf-8")
    return path


def test_main_exits_zero_when_healthy(config_dict, publish_calls, tmp_path) -> None:
    config_path = _write_config_yaml(tmp_path, config_dict)
    cfg = FactoryConfig.model_validate(config_dict)
    pid_path, liveness_path = _paths(cfg)
    _write_pidfile(pid_path, _own_pidfile_content(), age_s=3600)
    _touch_liveness(liveness_path)

    assert main(["--config", str(config_path)]) == 0
    assert publish_calls == []


def test_main_exits_one_when_down(config_dict, publish_calls, tmp_path) -> None:
    config_path = _write_config_yaml(tmp_path, config_dict)  # no pidfile, no liveness

    assert main(["--config", str(config_path)]) == 1
    assert len(publish_calls) == 1


def test_main_exits_one_on_unloadable_config(tmp_path, capsys) -> None:
    assert main(["--config", str(tmp_path / "absent.yaml")]) == 1
    assert "cannot load config" in capsys.readouterr().err


# ------------------------------------------------------------------- structure


def test_watchdog_imports_only_models_config_notify() -> None:
    """Design §1 import DAG: watchdog = models + config + notify — never db."""
    tree = ast.parse(Path(watchdog.__file__).read_text(encoding="utf-8"))
    sf_imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("sf_factory")
    }
    assert sf_imports <= {"sf_factory.models", "sf_factory.config", "sf_factory.notify"}
