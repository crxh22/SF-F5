#!/usr/bin/env python3
"""Playbook-driven agent stand-in for the wave-4 integration tests (design §8).

Spawned through the REAL ``AgentRunner`` via ``cli: stub`` routes by pointing
``process.stub_agent_path`` at this file — same spawn surface as
``tests/stub_agent.py`` (own process group, NDJSON streaming, registry,
token ledger), emitting the same claude-shaped NDJSON the StubAdapter parses.
The canonical stub stays the source for single-role scenarios (its env-var
scenarios cannot express the per-role FILE-WRITING contracts of a full
conveyor: spec.md, validation sidecars, audit/tier-2 findings sidecars); this
driver adds exactly that, scripted per role from a JSON playbook.

Stdlib only — it simulates an EXTERNAL binary with an arbitrary cwd.

Prompt channel (CCR-8, same as tests/stub_agent.py): the runner passes flags
only in argv and feeds the prompt on STDIN — the driver drains stdin to EOF
when no argv positional is given (the positional remains a direct-invocation
fallback).

Playbook (path in env ``SF_DRIVER_PLAYBOOK``): a JSON object mapping a role
KIND (detected from the prompt's fixed first line, written by scheduler.py's
prompt builders) to ``{"calls": [spec, ...], "default": spec | null}``. Each
driver invocation pops calls[0] under an exclusive flock (concurrent auditors
pop distinct keys safely), falling back to "default"; no spec at all = loud
exit 64 (the test then fails on its state assertions, never silently).

Prompt-marker -> kind:
  "You are consultation point"        -> cp1        (emit verdict JSON, no files)
  "You are the Spec Agent"            -> spec       (writes <unit>/spec.md)
  "You are the Builder"               -> builder    (writes files; appends build-notes.md)
  "You are the Validator"             -> validator  (writes validation-report.md + .json)
  "You are auditor '<role>'"          -> audit      (writes audit-<role>.md + .json;
                                                     key "audit:<role>" wins over "audit")
  "You are the stage executor"        -> respond    (writes findings-response.json,
                                                     refs parsed from the prompt listing)
  "You are the Integration Validator" -> tier2      (writes integration-report.md + .json)
  "You are the Phase Architect"       -> phase_plan (writes phase-plan.md + .json)

Call-spec fields (all optional; applied in this order):
  write_files: {relpath: content}   raw writes relative to cwd
  script:      [{"op": "write"|"append", "path": p, "content": c} |
                {"op": "git", "args": [...], "allow_fail": bool}]
                ordered ops (e.g. resolve a Tier-1 conflict: rebase, write, add,
                rebase --continue); git runs with GIT_EDITOR=true
  failing/passing (validator)       sidecar counts; default failing=0, passing=1
  findings (audit/tier2)            list for the findings sidecar; default []
  actions / action_default (respond) per-ref or blanket comply|contest|duplicate
  verdict | raw (cp1)               closed-set verdict, or raw result text
  plan / plan_md (phase_plan)       phase-plan.json object / .md text
  spec_text (spec)                  spec.md body
  notes: false (builder)            suppress the default build-notes.md append
  grandchild: true                  spawn a same-process-group sleeper, report its
                                    pid as a {"type": "stub_grandchild"} NDJSON line
  sleep_s: float                    sleep AFTER writes/emits (SIGKILL-window tests)
  skip_result: true                 no result line (hang/kill scenarios)
  result_text: str                  override the result text
  exit_code: int                    process exit code (default 0)
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

USAGE = {
    "input_tokens": 90,
    "cache_creation_input_tokens": 10,
    "cache_read_input_tokens": 5,
    "output_tokens": 40,
}

_MARKERS: list[tuple[str, str]] = [
    ("You are consultation point", "cp1"),
    ("You are the Spec Agent", "spec"),
    ("You are the Builder", "builder"),
    ("You are the Validator", "validator"),
    ("You are auditor", "audit"),
    ("You are the stage executor", "respond"),
    ("You are the Integration Validator", "tier2"),
    ("You are the Phase Architect", "phase_plan"),
]


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _fail(message: str) -> int:
    sys.stderr.write(f"agent_driver: {message}\n")
    sys.stderr.flush()
    return 64


def _detect_kind(prompt: str) -> str | None:
    for marker, kind in _MARKERS:
        if prompt.startswith(marker):
            return kind
    return None


def _unit_rel(prompt: str) -> str | None:
    # Unit ids are word chars/dots/dashes (new_id / plan-local ids) — a tight
    # charset so prose like "see the phase plan under _factory/phases/)" never
    # captures punctuation as a unit id.
    m = re.search(r"_factory/(stages|phases)/([A-Za-z0-9._-]+)", prompt)
    return f"_factory/{m.group(1)}/{m.group(2)}" if m else None


def _audit_role(prompt: str) -> str | None:
    m = re.search(r"You are auditor '([^']+)'", prompt)
    return m.group(1) if m else None


def _respond_refs(prompt: str) -> list[str]:
    return re.findall(r"^- (\S+) \(by ", prompt, flags=re.MULTILINE)


def _pop_spec(playbook_path: Path, key: str, fallback_key: str | None) -> dict | None:
    """Pop calls[0] for ``key`` (else ``fallback_key``) under an exclusive flock;
    fall back to that entry's "default". None = nothing scripted."""
    with open(playbook_path, "r+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        data = json.load(fh)
        entry = data.get(key)
        used_key = key
        if entry is None and fallback_key is not None:
            entry = data.get(fallback_key)
            used_key = fallback_key
        if entry is None:
            return None
        calls = entry.get("calls", [])
        if calls:
            spec = calls.pop(0)
            data[used_key]["calls"] = calls
            fh.seek(0)
            fh.truncate()
            json.dump(data, fh, indent=1)
            fh.flush()
            return spec
        return entry.get("default")


def _write(path: Path, content: str, *, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a" if append else "w", encoding="utf-8") as fh:
        fh.write(content)


def _run_script(cwd: Path, ops: list[dict]) -> None:
    env = dict(os.environ, GIT_EDITOR="true")
    for op in ops:
        if op["op"] in ("write", "append"):
            _write(cwd / op["path"], op["content"], append=op["op"] == "append")
        elif op["op"] == "git":
            proc = subprocess.run(
                ["git", *op["args"]], cwd=cwd, env=env, capture_output=True, text=True
            )
            if proc.returncode != 0 and not op.get("allow_fail"):
                raise RuntimeError(
                    f"git {' '.join(op['args'])} failed: {proc.stderr or proc.stdout}"
                )
        else:
            raise RuntimeError(f"unknown script op {op!r}")


def _synthesize(kind: str, spec: dict, cwd: Path, prompt: str) -> str | None:
    """Role-contract file writes; returns an override result text (cp1) or None."""
    rel = _unit_rel(prompt)
    unit_dir = (cwd / rel) if rel else None

    if kind == "cp1":
        if "raw" in spec:
            return spec["raw"]
        return json.dumps(
            {"verdict": spec["verdict"], "rationale": "scripted driver verdict"}
        )
    if unit_dir is None:
        raise RuntimeError(f"prompt for kind {kind!r} carries no _factory unit path")

    if kind == "spec":
        _write(unit_dir / "spec.md", spec.get("spec_text", "# spec\nscripted (driver)\n"))
    elif kind == "builder":
        if spec.get("notes", True):
            _write(unit_dir / "build-notes.md", "build pass (driver)\n", append=True)
    elif kind == "validator":
        failing = int(spec.get("failing", 0))
        passing = int(spec.get("passing", 1))
        _write(
            unit_dir / "validation-report.md",
            f"# validation report (driver)\nfailing: {failing}\n",
        )
        _write(
            unit_dir / "validation-report.json",
            json.dumps({"failing": failing, "passing": passing, "total": failing + passing}),
        )
    elif kind == "audit":
        role = _audit_role(prompt)
        if role is None:
            raise RuntimeError("auditor prompt carries no role name")
        findings = spec.get("findings", [])
        _write(unit_dir / f"audit-{role}.md", f"# audit {role} (driver)\n")
        _write(unit_dir / f"audit-{role}.json", json.dumps({"findings": findings}))
    elif kind == "tier2":
        findings = spec.get("findings", [])
        _write(unit_dir / "integration-report.md", "# integration report (driver)\n")
        _write(unit_dir / "integration-report.json", json.dumps({"findings": findings}))
    elif kind == "respond":
        refs = _respond_refs(prompt)
        actions = spec.get("actions", {})
        default_action = spec.get("action_default", "comply")
        responses = [
            {
                "ref": ref,
                "action": actions.get(ref, default_action),
                "rationale": "scripted driver triage",
            }
            for ref in refs
        ]
        _write(unit_dir / "findings-response.json", json.dumps({"responses": responses}))
    elif kind == "phase_plan":
        _write(unit_dir / "phase-plan.md", spec.get("plan_md", "# plan (driver)\n"))
        _write(unit_dir / "phase-plan.json", json.dumps(spec["plan"]))
    else:
        raise RuntimeError(f"unhandled kind {kind!r}")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--append-system-prompt", dest="system_append", default=None)
    parser.add_argument("--resume", dest="resume", default=None)
    parser.add_argument("prompt", nargs="?", default=None)
    args = parser.parse_args(argv)
    session_id = args.resume or os.environ.get("SF_STUB_SESSION_ID", "driver-sess-0001")
    cwd = Path.cwd()

    # CCR-8: the runner feeds the prompt on stdin (argv carries flags only);
    # drain it to EOF so the feeder never blocks. argv positional = fallback
    # for direct invocation; tty/absent stdin reads empty (never hangs).
    prompt = args.prompt
    if prompt is None:
        prompt = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()

    kind = _detect_kind(prompt)
    if kind is None:
        return _fail(f"no role marker recognized in prompt: {prompt[:120]!r}")
    playbook_env = os.environ.get("SF_DRIVER_PLAYBOOK")
    if not playbook_env or not Path(playbook_env).is_file():
        return _fail(f"SF_DRIVER_PLAYBOOK unset or missing: {playbook_env!r}")

    key, fallback = kind, None
    if kind == "audit":
        role = _audit_role(prompt)
        key, fallback = (f"audit:{role}", "audit") if role else ("audit", None)
    spec = _pop_spec(Path(playbook_env), key, fallback)
    if spec is None:
        return _fail(f"playbook has no calls/default for key {key!r}")

    sys.stderr.write(f"agent_driver: kind={kind} spec={json.dumps(spec)[:200]}\n")
    sys.stderr.flush()
    _emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "driver": {"kind": kind, "pgid_is_self": os.getpgid(0) == os.getpid()},
        }
    )

    for rel, content in spec.get("write_files", {}).items():
        _write(cwd / rel, content)
    if "script" in spec:
        _run_script(cwd, spec["script"])
    result_text = _synthesize(kind, spec, cwd, prompt)

    if spec.get("grandchild"):
        child = subprocess.Popen(  # same process group as the driver (group-kill tests)
            [sys.executable, "-c", "import time; time.sleep(600)"]
        )
        _emit({"type": "stub_grandchild", "pid": child.pid})

    if "sleep_s" in spec:
        time.sleep(float(spec["sleep_s"]))

    if not spec.get("skip_result"):
        _emit(
            {
                "type": "result",
                "subtype": "success",
                "result": spec.get("result_text", result_text or "driver done"),
                "session_id": session_id,
                "usage": dict(USAGE),
                "total_cost_usd": 0.001,
            }
        )
    return int(spec.get("exit_code", 0))


if __name__ == "__main__":
    sys.exit(main())
