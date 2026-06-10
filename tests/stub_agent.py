#!/usr/bin/env python3
"""Scripted stand-in for an agent CLI (design §8) — stdlib only, no sf_factory
imports (it simulates an EXTERNAL binary and runs with an arbitrary cwd).

Emits claude-shaped NDJSON on stdout (the runner's StubAdapter parses it with
the claude line parser) and a marker line on stderr (stderr-capture tests).

Scenario selection: ``--scenario <name>`` argv wins, else env
``SF_STUB_SCENARIO``, default ``success``. Scenarios (design §8):

- ``success``             init + text + result with usage/cost.
- ``persistent_failure``  as validator: writes ``validation-report.json`` with
                          ``failing`` = SF_STUB_FAILING (default 3) into
                          SF_STUB_REPORT_DIR (default cwd); callers hold the
                          value constant across calls to simulate non-decreasing.
- ``declared_inability``  writes ``_DECLARED_FAILURE.md`` into
                          SF_STUB_SENTINEL_DIR (default cwd), clean exit.
- ``timeout``             sleeps SF_STUB_SLEEP_S (default 3600) past any deadline;
                          SF_STUB_IGNORE_TERM=1 ignores SIGTERM (forces SIGKILL);
                          SF_STUB_GRANDCHILD=1 spawns a sleeper child in the same
                          process group and reports its pid (group-kill tests).
- ``garbage``             non-JSON lines, a JSON-but-not-object line, an oversized
                          line of SF_STUB_OVERSIZE_BYTES (default 200000) 'x's,
                          then a valid result — asserts §5.2 truncate-and-continue.
- ``invalid_verdict``     CP role: result text is JSON with a verdict outside any
                          closed set.
- ``valid_verdict:<value>`` CP role: well-formed verdict ``<value>`` + rationale.
- ``crash``               nonzero exit (13) mid-stream, no result line.

Other env knobs: SF_STUB_SESSION_ID (session id when not resuming). The runner
passes ``--resume <id>`` on session resume; the stub then echoes that id as its
session_id. ``--append-system-prompt`` is accepted and its byte length echoed in
the init line (canon-injection assertions read it from the NDJSON log).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

USAGE = {
    "input_tokens": 100,
    "cache_creation_input_tokens": 12,
    "cache_read_input_tokens": 8,
    "output_tokens": 45,
}
COST_USD = 0.0042


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _pdeathsig() -> int | None:
    """PR_GET_PDEATHSIG — lets tests verify the runner's Linux backstop wiring."""
    if sys.platform != "linux":
        return None
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        value = ctypes.c_int(-1)
        if libc.prctl(2, ctypes.byref(value), 0, 0, 0) != 0:  # PR_GET_PDEATHSIG
            return None
        return value.value
    except Exception:
        return None


def _emit_init(session_id: str, system_append: str | None, prompt: str) -> None:
    _emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "stub": {
                "pgid_is_self": os.getpgid(0) == os.getpid(),
                "pdeathsig": _pdeathsig(),
                "system_append_bytes": len(system_append.encode("utf-8"))
                if system_append is not None
                else None,
                "prompt": prompt,
            },
        }
    )


def _emit_text(session_id: str, text: str) -> None:
    _emit(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
            "session_id": session_id,
        }
    )


def _emit_result(session_id: str, text: str, *, with_usage: bool = True) -> None:
    obj: dict = {
        "type": "result",
        "subtype": "success",
        "result": text,
        "session_id": session_id,
    }
    if with_usage:
        obj["usage"] = dict(USAGE)
        obj["total_cost_usd"] = COST_USD
    _emit(obj)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default=None)
    parser.add_argument("--append-system-prompt", dest="system_append", default=None)
    parser.add_argument("--resume", dest="resume", default=None)
    parser.add_argument("prompt", nargs="?", default="")
    args = parser.parse_args(argv)

    scenario = args.scenario or os.environ.get("SF_STUB_SCENARIO", "success")
    session_id = args.resume or os.environ.get("SF_STUB_SESSION_ID", "stub-sess-0001")

    sys.stderr.write(f"stub-stderr: scenario={scenario}\n")
    sys.stderr.flush()

    if scenario == "success":
        _emit_init(session_id, args.system_append, args.prompt)
        _emit_text(session_id, "stub working")
        _emit_result(session_id, "stub success")
        return 0

    if scenario == "persistent_failure":
        failing = int(os.environ.get("SF_STUB_FAILING", "3"))
        report_dir = Path(os.environ.get("SF_STUB_REPORT_DIR", "."))
        report_dir.mkdir(parents=True, exist_ok=True)
        report = {"failing": failing, "passing": 1, "total": failing + 1}
        (report_dir / "validation-report.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        _emit_init(session_id, args.system_append, args.prompt)
        _emit_result(session_id, f"validation failed: {failing} failing tests")
        return 0

    if scenario == "declared_inability":
        sentinel_dir = Path(os.environ.get("SF_STUB_SENTINEL_DIR", "."))
        sentinel_dir.mkdir(parents=True, exist_ok=True)
        (sentinel_dir / "_DECLARED_FAILURE.md").write_text(
            "# Declared failure\n\nI cannot proceed: scripted inability (stub).\n",
            encoding="utf-8",
        )
        _emit_init(session_id, args.system_append, args.prompt)
        _emit_result(session_id, "declared inability, wrote _DECLARED_FAILURE.md")
        return 0

    if scenario == "timeout":
        if os.environ.get("SF_STUB_IGNORE_TERM") == "1":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _emit_init(session_id, args.system_append, args.prompt)
        if os.environ.get("SF_STUB_GRANDCHILD") == "1":
            child = subprocess.Popen(  # same process group as the stub
                [sys.executable, "-c", "import time; time.sleep(3600)"]
            )
            _emit({"type": "stub_grandchild", "pid": child.pid})
        _emit_text(session_id, "sleeping past the deadline")
        time.sleep(float(os.environ.get("SF_STUB_SLEEP_S", "3600")))
        return 0  # unreachable under a working kill ladder

    if scenario == "garbage":
        _emit_init(session_id, args.system_append, args.prompt)
        sys.stdout.write("this is not json at all\n")
        sys.stdout.write("{broken json line\n")
        sys.stdout.write("[1, 2, 3]\n")  # valid JSON, not an object
        oversize = int(os.environ.get("SF_STUB_OVERSIZE_BYTES", "200000"))
        sys.stdout.write("x" * oversize + "\n")
        sys.stdout.flush()
        _emit_text(session_id, "survived the garbage")
        _emit_result(session_id, "stub success")
        return 0

    if scenario == "invalid_verdict":
        _emit_init(session_id, args.system_append, args.prompt)
        _emit_result(
            session_id,
            json.dumps(
                {"verdict": "not_in_any_closed_set", "rationale": "scripted invalid verdict"}
            ),
        )
        return 0

    if scenario.startswith("valid_verdict:"):
        verdict = scenario.split(":", 1)[1]
        _emit_init(session_id, args.system_append, args.prompt)
        _emit_result(
            session_id,
            json.dumps({"verdict": verdict, "rationale": "scripted verdict (stub)"}),
        )
        return 0

    if scenario == "crash":
        _emit_init(session_id, args.system_append, args.prompt)
        _emit_text(session_id, "about to crash")
        return 13

    sys.stderr.write(f"stub_agent: unknown scenario {scenario!r}\n")
    return 64


if __name__ == "__main__":
    sys.exit(main())
