"""Process runner (design §1/§4/§5): the only LLM spawn path in the factory.

Spawns ``claude -p`` / ``codex exec`` / the test stub as subprocesses in their
OWN process groups, injects the canon bundle (D-0009), streams NDJSON
line-tolerantly to a log file, captures session ids and token usage, enforces
timeout via a terminate→kill ladder signalled to the process GROUP, and
finalizes the process registry + token ledger in one transaction. Tagging
(kind/cp_id/role) is enforced at this boundary — the precondition of the §2
consultation-creep scan.

Print mode only (OPEN-4): interactive routes are operator-driven outside the
orchestrator in MVP.

May import: models, config, db (design §1).

CCR-2 (approved, design v1.3 / D-0013): after exec the registry row flips
'spawned'→'running' via ``db.mark_process_running`` — child pid persisted,
``heartbeat_at`` written as the initial heartbeat — so the §5.5a cross-restart
orphan sweep finds its pids in ``process_registry`` alone; ``kill_running``
uses in-memory handles for this process's own children plus those persisted
pids for rows from a previous run.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shlex
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Literal, Protocol

from sf_factory import db
from sf_factory.config import FactoryConfig, ModelRoute
from sf_factory.db import Database
from sf_factory.models import (
    ConsultationBreachError,
    ProcessError,
    ProcessRecord,
    new_id,
    utc_now,
)

# --------------------------------------------------------------------- constants

#: Stream classification vocabulary (design §4: "init|text|result|usage|other").
StreamKind = Literal["init", "text", "result", "usage", "other"]

#: Truncation marker appended to the NDJSON log when an oversized line is
#: swallowed (§5.2) — itself a valid JSON line so log replays stay parseable.
TRUNCATION_MARKER = b'{"_sf_truncated_oversized_line": true}\n'

_PR_SET_PDEATHSIG = 1  # linux/prctl.h

if sys.platform == "linux":
    # Loaded ONCE in the parent: the preexec closure must not dlopen after fork.
    _LIBC = ctypes.CDLL(None, use_errno=True)

    def _pdeathsig_preexec() -> None:
        """Runs in the child between fork and exec (§5.1 Linux backstop):
        the kernel SIGKILLs this child if the orchestrator dies — agent trees
        must die with their supervisor, never run unsupervised until resume.
        A failed prctl aborts the spawn loudly (fail-explicit, Doctrine §7)."""
        if _LIBC.prctl(_PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")

    _PREEXEC: Callable[[], None] | None = _pdeathsig_preexec
else:  # pragma: no cover - non-Linux: start_new_session still isolates the group
    _PREEXEC = None


# ----------------------------------------------------------------- result types


@dataclass(frozen=True, slots=True)
class StreamItem:
    """One classified NDJSON object (design §4 ``parse_line``): ``kind`` is
    init|text|result|usage|other; the optional fields carry whatever that line
    reported (a claude ``result`` line carries text+session+usage+cost at once)."""

    kind: StreamKind
    session_id: str | None = None
    text: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class AgentResult:
    """process_id, exit_code, timed_out: bool, killed: bool, declared_failure: bool,
    result_text: str, session_id: str|None (from the CLI init/result NDJSON line —
    continue_session support), tokens_in: int|None, tokens_out: int|None,
    cost_usd: float|None, garbage_lines: int, ndjson_log_path: str, stderr_path: str,
    duration_ms: int."""

    process_id: int
    exit_code: int | None
    timed_out: bool
    killed: bool
    declared_failure: bool
    result_text: str
    session_id: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    garbage_lines: int
    ndjson_log_path: str
    stderr_path: str
    duration_ms: int


# --------------------------------------------------------------------- adapters


class CliAdapter(Protocol):
    """Per-CLI argv/stream contract (design §4). Implementations are stateless."""

    def build_cmd(
        self,
        route: ModelRoute,
        prompt: str,
        *,
        system_append: str | None = None,
        resume_session: str | None = None,
    ) -> list[str]:
        """argv for a one-shot NDJSON-streaming run. system_append = canon bundle
        (D-0009: claude `--append-system-prompt`); resume_session = resume that
        CLI session (claude `--resume <id>`)."""
        ...

    def materialize_workspace(self, cwd: Path, system_append: str | None) -> None:
        """Hook for CLIs without a system-prompt flag (codex: write AGENTS.md into
        cwd before spawn, D-0009)."""
        ...

    def parse_line(self, obj: dict) -> StreamItem:
        """Classify one NDJSON object: init|text|result|usage|other."""
        ...


class ClaudeAdapter:
    """claude CLI adapter — argv per design §5.1, stream-json line shapes."""

    def build_cmd(
        self,
        route: ModelRoute,
        prompt: str,
        *,
        system_append: str | None = None,
        resume_session: str | None = None,
    ) -> list[str]:
        # §5.1 literal argv order: claude --model <m> --output-format stream-json
        # --verbose [--tools "" | --permission-mode bypassPermissions]
        # --append-system-prompt <canon> [--resume <id>] -p <prompt>.
        cmd = ["claude", "--model", route.model, "--output-format", "stream-json", "--verbose"]
        if route.tools == "none":
            # CCR-3/D-0017 tools-off spawn (Decision Sessions): structural
            # no-write enforcement. Exact flagset verified against the installed
            # CLI at build (dashboard design §4): `--tools <tools...>` — "Use ""
            # to disable all tools" — disables the FULL built-in set in one flag,
            # with no tool-name enumeration to drift as the CLI grows.
            cmd += ["--tools", ""]
        else:
            # Phase-seeding design §5 (D-0024): claude print mode default-DENIES
            # writes and a print-mode agent has no human to answer prompts — a
            # denied write is a wedged stage, so every tools-on pipeline agent
            # bypasses claude's own gating. NOT symmetric with the codex
            # OS-enforced `--sandbox workspace-write`: the lost guardrail is
            # replaced by the control plane's out-of-bounds detector
            # (scheduler, §5); narrowing later is the pre-registered
            # `models.<role>.permission_mode` config key shape — a config
            # addition, not a contract change. Tools-off sessions unchanged.
            cmd += ["--permission-mode", "bypassPermissions"]
        if system_append is not None:
            cmd += ["--append-system-prompt", system_append]
        if resume_session is not None:
            cmd += ["--resume", resume_session]
        cmd += ["-p", prompt]
        return cmd

    def materialize_workspace(self, cwd: Path, system_append: str | None) -> None:
        """No-op: claude takes the canon via --append-system-prompt (D-0009)."""

    def parse_line(self, obj: dict) -> StreamItem:
        kind = obj.get("type")
        if kind == "system" and obj.get("subtype") == "init":
            return StreamItem(kind="init", session_id=_opt_str(obj.get("session_id")))
        if kind == "assistant":
            message = obj.get("message")
            texts: list[str] = []
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text")
                            if isinstance(text, str):
                                texts.append(text)
            return StreamItem(
                kind="text",
                session_id=_opt_str(obj.get("session_id")),
                text="".join(texts) if texts else None,
            )
        if kind == "result":
            usage = obj.get("usage")
            tokens_in = tokens_out = None
            if isinstance(usage, dict):
                # Budget accounting counts ALL context flowing in: cache creation
                # and cache reads are separate fields in claude usage (not included
                # in input_tokens), so summing never double-counts (§2 context_budget).
                tokens_in = _sum_opt_ints(
                    usage.get("input_tokens"),
                    usage.get("cache_creation_input_tokens"),
                    usage.get("cache_read_input_tokens"),
                )
                tokens_out = _opt_int(usage.get("output_tokens"))
            return StreamItem(
                kind="result",
                session_id=_opt_str(obj.get("session_id")),
                text=obj.get("result") if isinstance(obj.get("result"), str) else None,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=_opt_float(obj.get("total_cost_usd")),
            )
        return StreamItem(kind="other")


class CodexAdapter:
    """codex CLI adapter — facts verified by smoke test (D-0011/OPEN-3):
    `codex exec --json` emits JSONL `thread.started{thread_id}`,
    `item.completed{type: agent_message, text}`, `turn.completed{usage{...}}`;
    resume = `codex exec resume <thread_id>`; needs `--skip-git-repo-check`
    and stdin=devnull (the runner spawns every CLI with stdin=devnull)."""

    def build_cmd(
        self,
        route: ModelRoute,
        prompt: str,
        *,
        system_append: str | None = None,
        resume_session: str | None = None,
    ) -> list[str]:
        if route.tools == "none":
            # CCR-3: no VERIFIED codex tools-off flagset exists (only the claude
            # set was verified at build, dashboard design §4) — spawning a
            # tools-on process under a tools-off contract would silently void
            # the structural no-write guarantee. Fail-explicit (Doctrine §7).
            raise ProcessError(
                "route requests tools='none' but the codex adapter has no verified "
                "tools-off flagset (dashboard design §4) — route tools-off roles "
                "to the claude CLI"
            )
        cmd = ["codex", "exec"]
        if resume_session is not None:
            cmd += ["resume", resume_session]
        # B8 live run: codex defaults to a read-only sandbox and refused the
        # report writes the artifact contract requires — grant workspace-scoped
        # writes (CLI-verified: `--sandbox <read-only|workspace-write|danger-full-access>`).
        cmd += ["--json", "--skip-git-repo-check", "--sandbox", "workspace-write"]
        if route.model != "default":  # 'default' = let the codex config decide (D-0005)
            cmd += ["--model", route.model]
        cmd.append(prompt)
        return cmd

    def materialize_workspace(self, cwd: Path, system_append: str | None) -> None:
        """Write the canon bundle as AGENTS.md into cwd (D-0009: codex has no
        system-prompt flag). Idempotent on identical content; an existing
        DIFFERENT AGENTS.md is never clobbered silently (Doctrine §7) — that
        would both destroy workspace content and pollute the stage diff."""
        if system_append is None:
            return
        target = Path(cwd) / "AGENTS.md"
        try:
            if target.exists():
                if target.read_text(encoding="utf-8") == system_append:
                    return
                raise ProcessError(
                    f"refusing to overwrite existing divergent AGENTS.md at {target} "
                    "with the canon bundle (D-0009 materialization)"
                )
            target.write_text(system_append, encoding="utf-8")
        except OSError as exc:
            raise ProcessError(f"cannot materialize AGENTS.md at {target}: {exc}") from exc

    def parse_line(self, obj: dict) -> StreamItem:
        kind = obj.get("type")
        if kind == "thread.started":
            return StreamItem(kind="init", session_id=_opt_str(obj.get("thread_id")))
        if kind == "item.completed":
            item = obj.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                return StreamItem(kind="text", text=text if isinstance(text, str) else None)
            return StreamItem(kind="other")
        if kind == "turn.completed":
            usage = obj.get("usage")
            if isinstance(usage, dict):
                # output_tokens already includes reasoning_output_tokens (subset
                # detail field) — adding it would double-count.
                return StreamItem(
                    kind="usage",
                    tokens_in=_opt_int(usage.get("input_tokens")),
                    tokens_out=_opt_int(usage.get("output_tokens")),
                )
            return StreamItem(kind="usage")
        return StreamItem(kind="other")


class StubAdapter(ClaudeAdapter):
    """Test stub adapter (§8): spawns ``process.stub_agent_path`` under the
    current interpreter; the stub emits claude-shaped NDJSON, so parsing and
    workspace handling are inherited from ClaudeAdapter. AgentRunner binds the
    script path from config; the module-level ADAPTERS['stub'] entry is unbound
    and refuses to build argv (fail-explicit, never a half-spawn).
    ``ModelRoute.tools`` is ignored (dashboard design §4: the stub spawns no
    tools to disable; its argv carries no tools flag)."""

    def __init__(self, script_path: Path | None = None) -> None:
        self._script_path = script_path

    def build_cmd(
        self,
        route: ModelRoute,
        prompt: str,
        *,
        system_append: str | None = None,
        resume_session: str | None = None,
    ) -> list[str]:
        if self._script_path is None:
            raise ProcessError(
                "stub adapter is unbound: AgentRunner binds process.stub_agent_path from "
                "config; the module-level ADAPTERS['stub'] entry cannot spawn"
            )
        cmd = [sys.executable, str(self._script_path)]
        if system_append is not None:
            cmd += ["--append-system-prompt", system_append]
        if resume_session is not None:
            cmd += ["--resume", resume_session]
        cmd.append(prompt)
        return cmd


#: 'claude', 'codex', 'stub' — selected by config models.<role>.cli (design §4).
ADAPTERS: Mapping[str, CliAdapter] = MappingProxyType(
    {"claude": ClaudeAdapter(), "codex": CodexAdapter(), "stub": StubAdapter()}
)


# ----------------------------------------------------------------- parse helpers


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, int | float) else None


def _sum_opt_ints(*values: object) -> int | None:
    ints = [v for v in (_opt_int(v) for v in values) if v is not None]
    return sum(ints) if ints else None


class _StreamState:
    """Mutable per-run accumulator for the §5.2 streaming loop."""

    __slots__ = (
        "session_id",
        "result_text",
        "last_text",
        "tokens_in",
        "tokens_out",
        "cost_usd",
        "garbage_lines",
        "bytes_logged",
        "last_heartbeat",
    )

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.result_text: str | None = None
        self.last_text: str | None = None
        self.tokens_in: int | None = None
        self.tokens_out: int | None = None
        self.cost_usd: float | None = None
        self.garbage_lines = 0
        self.bytes_logged = 0
        self.last_heartbeat = float("-inf")

    def absorb(self, item: StreamItem) -> None:
        if item.session_id is not None:
            self.session_id = item.session_id
        if item.kind == "text" and item.text is not None:
            self.last_text = item.text
        if item.kind == "result" and item.text is not None:
            self.result_text = item.text
        if item.tokens_in is not None:
            self.tokens_in = (self.tokens_in or 0) + item.tokens_in
        if item.tokens_out is not None:
            self.tokens_out = (self.tokens_out or 0) + item.tokens_out
        if item.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + item.cost_usd


# ----------------------------------------------------------------------- runner


class AgentRunner:
    """Only LLM spawn path in the factory."""

    def __init__(self, cfg: FactoryConfig, db: Database) -> None:
        """Only LLM spawn path in the factory.

        The ``db`` parameter (frozen §4 name) shadows the module import only
        within this scope; repository functions are not called here."""
        self._cfg = cfg
        self._db = db
        # Tagging maps (§2 creep-scan precondition, enforced in run_agent).
        self._cp_by_id = {cp.id: cp for cp in cfg.consultation_points}
        self._consultation_roles = frozenset(cp.role for cp in cfg.consultation_points)
        self._pipeline_roles = frozenset(cfg.models) - self._consultation_roles
        # Adapters: the stub is bound to process.stub_agent_path from config.
        adapters: dict[str, CliAdapter] = dict(ADAPTERS)
        adapters["stub"] = StubAdapter(self._resolve_path(cfg.process.stub_agent_path))
        self._adapters: Mapping[str, CliAdapter] = MappingProxyType(adapters)
        #: Live child handles by registry id — same-process recovery source for
        #: kill_running; rows from a previous run are reached via the pid
        #: persisted by db.mark_process_running (CCR-2).
        self._live: dict[int, asyncio.subprocess.Process] = {}

    # ------------------------------------------------------------------ public

    async def run_agent(
        self,
        role: str,
        prompt: str,
        *,
        unit_level: str,
        unit_id: str,
        cwd: Path,
        kind: str = "agent",
        cp_id: str | None = None,
        timeout_s: int | None = None,
        resume_session: str | None = None,
    ) -> AgentResult:
        """Spawn per config models[role] in its OWN process group (start_new_session=True;
        Linux backstop PR_SET_PDEATHSIG=SIGKILL via preexec_fn — agent trees must die with
        the orchestrator, not run unsupervised until resume). Canon bundle resolved from
        cfg.canon by role class (D-0009) and passed to the adapter. Tagging enforced at
        this boundary (precondition of the §2 creep scan): kind='consultation' ⇔ cp_id set
        ⇔ role ∈ registry consultation roles; kind='agent' ⇒ role ∈ config pipeline roles —
        else ConsultationBreachError. Register process; stderr → <process>.stderr file
        (inherited fd, no drain task); stream NDJSON line-tolerantly to log file; heartbeat
        throttled to process.heartbeat_min_interval_s; capture session_id; enforce timeout
        (terminate->kill grace from config, signals to the process GROUP); finalize
        registry+token_ledger in one tx; detect declared-failure sentinel. Raises
        ProcessError only on spawn impossibility."""
        self._enforce_tagging(role=role, kind=kind, cp_id=cp_id)
        route = self._cfg.models[role]
        if route.mode != "print":
            raise ProcessError(
                f"role {role!r} routes to mode={route.mode!r}: the runner implements "
                "print mode only (OPEN-4); interactive sessions are operator-driven"
            )
        adapter = self._adapters[route.cli]
        canon = self._canon_text(role=role, kind=kind)
        adapter.materialize_workspace(Path(cwd), canon)
        argv = adapter.build_cmd(
            route, prompt, system_append=canon, resume_session=resume_session
        )
        cmdline = shlex.join(argv)

        log_dir = self._resolve_path(self._cfg.process.ndjson_log_dir)
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProcessError(f"cannot create ndjson_log_dir {log_dir}: {exc}") from exc
        # Log files are token-named: ndjson_log_path must be written at insert
        # (frozen db.py has no updater) while the registry id is DB-assigned;
        # consumers always read the column / AgentResult, never rebuild names.
        token = new_id("proc")
        ndjson_path = log_dir / f"{token}.ndjson"
        stderr_path = log_dir / f"{token}.stderr"

        spawned_at = utc_now()
        with self._db.transaction() as conn:
            process_id = db.insert_process(
                conn,
                ProcessRecord(
                    id=None,
                    unit_level=unit_level,
                    unit_id=unit_id,
                    kind=kind,
                    role=role,
                    cp_id=cp_id,
                    session_id=None,
                    pid=None,
                    cmdline=cmdline,
                    cwd=str(cwd),
                    state="spawned",
                    exit_code=None,
                    ndjson_log_path=str(ndjson_path),
                    spawned_at=spawned_at,
                    heartbeat_at=None,
                    ended_at=None,
                ),
            )

        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            log_file: BinaryIO = ndjson_path.open("wb")
        except OSError as exc:
            self._finalize_spawn_failure(process_id, unit_level, unit_id, str(exc))
            raise ProcessError(f"cannot open NDJSON log {ndjson_path}: {exc}") from exc
        try:
            with stderr_path.open("wb") as stderr_file:
                # §5.1: stderr redirected at spawn to a FILE — inherited fd, no
                # drain task, crash-safe evidence; never a PIPE (a full 64KB pipe
                # deadlocks the child into a spurious timeout), never merged into
                # stdout (would corrupt the NDJSON stream). stdin=devnull: codex
                # reads piped stdin otherwise (D-0011); harmless for the others.
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        cwd=str(cwd),
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=stderr_file,
                        start_new_session=True,
                        preexec_fn=_PREEXEC,
                        limit=self._cfg.process.ndjson_max_line_bytes,
                    )
                except (OSError, ValueError) as exc:
                    self._finalize_spawn_failure(process_id, unit_level, unit_id, str(exc))
                    raise ProcessError(f"cannot spawn {argv[0]!r}: {exc}") from exc
        except OSError as exc:  # stderr file unopenable
            log_file.close()
            self._finalize_spawn_failure(process_id, unit_level, unit_id, str(exc))
            raise ProcessError(f"cannot open stderr file {stderr_path}: {exc}") from exc

        self._live[process_id] = proc
        state = _StreamState()
        timeout = float(timeout_s if timeout_s is not None else self._cfg.process.agent_timeout_s)
        try:
            # CCR-2 (§5.1): flip the registry row 'spawned'→'running' after exec —
            # persist the child pid (the §5.5a cross-restart orphan sweep kills by
            # process_registry.pid) and write the initial heartbeat at exec time,
            # so staleness math is sound before the first stream line arrives.
            with self._db.transaction() as conn:
                db.mark_process_running(conn, process_id, pid=proc.pid, at=utc_now())
                db.insert_event(
                    conn,
                    unit_level=unit_level,
                    unit_id=unit_id,
                    event_type="spawn",
                    actor="control_plane",
                    payload={
                        "process_id": process_id,
                        "pid": proc.pid,
                        "role": role,
                        "kind": kind,
                        "cp_id": cp_id,
                    },
                )
            timed_out, killed = await self._supervise(
                proc, adapter, log_file, state, process_id, deadline=started + timeout
            )
        except BaseException as exc:
            # Cancellation or a control-plane bug: the child must not outlive its
            # supervision (§5.1 — PDEATHSIG only covers orchestrator DEATH).
            _signal_group(proc, signal.SIGKILL)
            try:
                self._finalize_failure(process_id, unit_level, unit_id, proc, repr(exc))
            except Exception as finalize_exc:  # keep the original failure primary
                exc.add_note(f"finalize after supervision failure also failed: {finalize_exc}")
            raise
        finally:
            self._live.pop(process_id, None)
            log_file.close()

        ended_at = utc_now()
        duration_ms = int((loop.time() - started) * 1000)
        exit_code = proc.returncode
        final_state = "timed_out" if timed_out else "exited"
        usage_missing = state.tokens_in is None and state.tokens_out is None

        row_tokens_in, row_tokens_out, estimated = state.tokens_in, state.tokens_out, False
        if usage_missing and self._cfg.budgets.usage_missing_policy == "estimate":
            # §2: conservative logged-stream-bytes/4 into the ledger, estimated=1 —
            # a usage-blind stage must still reach its budget cap.
            row_tokens_out, estimated = state.bytes_logged // 4, True

        with self._db.transaction() as conn:
            db.finalize_process(
                conn,
                process_id,
                state=final_state,
                exit_code=exit_code,
                ended_at=ended_at,
                session_id=state.session_id,
            )
            db.insert_token_usage(
                conn,
                process_id=process_id,
                unit_level=unit_level,
                unit_id=unit_id,
                role=role,
                model=route.model,
                tokens_in=row_tokens_in,
                tokens_out=row_tokens_out,
                cost_usd=state.cost_usd,
                estimated=estimated,
            )
            if usage_missing:
                db.insert_event(
                    conn,
                    unit_level=unit_level,
                    unit_id=unit_id,
                    event_type="usage_missing",
                    actor="control_plane",
                    payload={
                        "process_id": process_id,
                        "role": role,
                        "policy": self._cfg.budgets.usage_missing_policy,
                        "estimated_tokens_out": row_tokens_out if estimated else None,
                    },
                )
            db.insert_event(
                conn,
                unit_level=unit_level,
                unit_id=unit_id,
                event_type="timeout" if timed_out else "exit",
                actor="control_plane",
                payload={
                    "process_id": process_id,
                    "exit_code": exit_code,
                    "killed": killed,
                    "garbage_lines": state.garbage_lines,
                    "duration_ms": duration_ms,
                    "stderr_path": str(stderr_path),
                },
            )

        result_text = (
            state.result_text if state.result_text is not None else (state.last_text or "")
        )
        return AgentResult(
            process_id=process_id,
            exit_code=exit_code,
            timed_out=timed_out,
            killed=killed,
            declared_failure=self._declared_failure(Path(cwd), unit_level, unit_id),
            result_text=result_text,
            session_id=state.session_id,
            tokens_in=state.tokens_in,
            tokens_out=state.tokens_out,
            cost_usd=state.cost_usd,
            garbage_lines=state.garbage_lines,
            ndjson_log_path=str(ndjson_path),
            stderr_path=str(stderr_path),
            duration_ms=duration_ms,
        )

    async def kill_running(self) -> int:
        """Kill (by process group) every process_registry row in 'spawned'/'running'
        whose pid is alive (recovery). Returns count.

        Pid sources: the in-memory handle table for this process's own children;
        ``process_registry.pid`` (persisted by db.mark_process_running, CCR-2)
        for rows from a previous run.
        A leader pid that is alive but whose /proc cmdline mismatches the recorded
        one is pid reuse — never killed. A dead leader with a live process group is
        our orphaned descendants (start_new_session ⇒ pgid == child pid) — killed."""
        conn = self._db.read()
        rows = db.processes_in_state(conn, "spawned") + db.processes_in_state(conn, "running")
        count = 0
        for rec in rows:
            verified_ours = False
            pid = rec.pid
            live = self._live.get(rec.id) if rec.id is not None else None
            if live is not None and live.returncode is None:
                pid = live.pid
                verified_ours = True
            if pid is None:
                continue
            if not verified_ours:
                if _leader_alive(pid):
                    if not cmdline_matches(pid, rec.cmdline):
                        continue  # pid reused by a foreign process — never kill strangers
                elif not _group_alive(pid):
                    continue  # leader and group both gone
                # else: leader dead, group alive → our orphaned descendants.
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                continue
            count += 1
        return count

    # ------------------------------------------------------------ spawn helpers

    def _enforce_tagging(self, *, role: str, kind: str, cp_id: str | None) -> None:
        """§4/§2 creep-scan precondition: kind='consultation' ⇔ cp_id set ⇔ role ∈
        registry consultation roles; kind='agent' ⇒ role ∈ config pipeline roles."""
        if kind == "consultation":
            if cp_id is None:
                raise ConsultationBreachError(
                    f"kind='consultation' requires cp_id (role={role!r})"
                )
            cp = self._cp_by_id.get(cp_id)
            if cp is None:
                raise ConsultationBreachError(
                    f"cp_id {cp_id!r} is not in the consultation registry"
                )
            if role != cp.role:
                raise ConsultationBreachError(
                    f"consultation {cp_id!r} is registered for role {cp.role!r}, "
                    f"not {role!r}"
                )
        elif kind == "agent":
            if cp_id is not None:
                raise ConsultationBreachError(
                    f"kind='agent' must not carry cp_id (got {cp_id!r}, role={role!r})"
                )
            if role not in self._pipeline_roles:
                raise ConsultationBreachError(
                    f"role {role!r} is not a config pipeline role"
                    + (
                        " (it is a consultation role — spawn it as kind='consultation')"
                        if role in self._consultation_roles
                        else ""
                    )
                )
        else:
            raise ConsultationBreachError(
                f"run_agent spawns kind 'agent' or 'consultation' only, got {kind!r}"
            )

    def _canon_text(self, *, role: str, kind: str) -> str | None:
        """Assemble the canon bundle for the role class (D-0009): consultation
        points get cfg.canon.inject.consultation_points (empty by default),
        founder-facing roles the founder_facing bundle, every other pipeline
        agent the pipeline_agents bundle. Missing/empty canon file = spawn
        impossibility — a partial canon is worse than a noisy stop."""
        canon = self._cfg.canon
        if kind == "consultation":
            keys = canon.inject.consultation_points
        elif role in canon.founder_facing_roles:
            keys = canon.inject.founder_facing
        else:
            keys = canon.inject.pipeline_agents
        if not keys:
            return None
        home = self._cfg.factory.home
        parts = [
            "=== SF-F5 CANON === This block is the SF-F5 canon, assembled at spawn "
            "from the source files below.\n"
        ]
        for key in keys:
            rel = canon.files[key]  # key membership validated by config (CanonCfg)
            path = Path(rel) if Path(rel).is_absolute() else home / rel
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ProcessError(f"canon file {key!r} unreadable at {path}: {exc}") from exc
            if not text.strip():
                raise ProcessError(f"canon file {key!r} at {path} is empty")
            parts.append(f"\n--- {rel} ---\n{text}")
        parts.append("\n=== END SF-F5 CANON ===\n")
        return "".join(parts)

    def _resolve_path(self, p: Path) -> Path:
        """Relative config paths resolve against factory.home (e.g. the real
        config's `.factory/logs`, `tests/stub_agent.py`)."""
        return p if p.is_absolute() else self._cfg.factory.home / p

    def _declared_failure(self, cwd: Path, unit_level: str, unit_id: str) -> bool:
        """§5.4 sentinel flag: `_DECLARED_FAILURE.md` present in the unit artifact
        dir. The layout is the §4 STAGE_ARTIFACTS/PHASE_ARTIFACTS frozen contract
        (`_factory/stages|phases/<id>/`), restated here because runner may not
        import artifacts (§1 import DAG). Exact-name match only: archived
        sentinels (`*.resolved-<id>.md`, §5.4) never re-flag. Event emission and
        escalation stay with the executor's detect_sentinels pass (§5.4)."""
        subdir = {"stage": "stages", "phase": "phases"}.get(unit_level)
        if subdir is None:
            return False
        return (cwd / "_factory" / subdir / unit_id / "_DECLARED_FAILURE.md").is_file()

    def _finalize_spawn_failure(
        self, process_id: int, unit_level: str, unit_id: str, error: str
    ) -> None:
        """§6 ProcessError row handling: registry row finalized 'killed' + event."""
        with self._db.transaction() as conn:
            db.finalize_process(
                conn, process_id, state="killed", exit_code=None, ended_at=utc_now()
            )
            db.insert_event(
                conn,
                unit_level=unit_level,
                unit_id=unit_id,
                event_type="spawn_failed",
                actor="control_plane",
                payload={"process_id": process_id, "error": error},
            )

    def _finalize_failure(
        self,
        process_id: int,
        unit_level: str,
        unit_id: str,
        proc: asyncio.subprocess.Process,
        error: str,
    ) -> None:
        """Finalize after a supervision failure/cancellation: group already
        SIGKILLed by the caller; the row must never wedge in 'spawned'/'running'."""
        with self._db.transaction() as conn:
            db.finalize_process(
                conn,
                process_id,
                state="killed",
                exit_code=proc.returncode,
                ended_at=utc_now(),
            )
            db.insert_event(
                conn,
                unit_level=unit_level,
                unit_id=unit_id,
                event_type="exit",
                actor="control_plane",
                payload={"process_id": process_id, "killed": True, "error": error},
            )

    # ------------------------------------------------------------- stream + kill

    async def _supervise(
        self,
        proc: asyncio.subprocess.Process,
        adapter: CliAdapter,
        log_file: BinaryIO,
        state: _StreamState,
        process_id: int,
        *,
        deadline: float,
    ) -> tuple[bool, bool]:
        """Drain the NDJSON stream until EOF or deadline, then enforce the §5.3
        terminate→kill ladder (signals to the process GROUP). Returns
        (timed_out, killed); ``killed`` = SIGKILL was required."""
        loop = asyncio.get_running_loop()
        outcome = await self._drain(proc, adapter, log_file, state, process_id, deadline)
        if outcome == "eof":
            try:
                await asyncio.wait_for(proc.wait(), max(deadline - loop.time(), 0.001))
                return False, False
            except TimeoutError:
                pass  # stdout closed but the process lingers past the deadline
        # Deadline exceeded → SIGTERM the group; keep draining during the grace so
        # a final result/session line flushed on SIGTERM is still captured.
        _signal_group(proc, signal.SIGTERM)
        grace_deadline = loop.time() + self._cfg.process.terminate_grace_s
        outcome = await self._drain(proc, adapter, log_file, state, process_id, grace_deadline)
        if outcome == "eof":
            try:
                await asyncio.wait_for(proc.wait(), max(grace_deadline - loop.time(), 0.001))
                return True, False
            except TimeoutError:
                pass
        _signal_group(proc, signal.SIGKILL)
        kill_deadline = loop.time() + self._cfg.process.kill_grace_s
        await self._drain(proc, adapter, log_file, state, process_id, kill_deadline)
        try:
            await asyncio.wait_for(proc.wait(), max(kill_deadline - loop.time(), 0.001))
        except TimeoutError:
            # Unkillable (e.g. D-state): registry still says timed_out; exit_code
            # stays None — never block the loop forever on a dead-but-stuck child.
            pass
        return True, True

    async def _drain(
        self,
        proc: asyncio.subprocess.Process,
        adapter: CliAdapter,
        log_file: BinaryIO,
        state: _StreamState,
        process_id: int,
        deadline: float,
    ) -> str:
        """Line-tolerant NDJSON pump (§5.2): every raw line lands in the log file
        first (crash-safe evidence), then parses; garbage never aborts the stream.
        Returns 'eof' or 'deadline'."""
        assert proc.stdout is not None
        reader = proc.stdout
        loop = asyncio.get_running_loop()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return "deadline"
            try:
                line = await asyncio.wait_for(reader.readline(), remaining)
            except TimeoutError:
                return "deadline"
            except ValueError:
                # Oversized line: readline discarded its buffered head (asyncio
                # semantics). Swallow to the next newline, log marker + recovered
                # tail, count ONE garbage line, continue — an oversized line must
                # never abort the stream and lose the result line (§5.2).
                state.garbage_lines += 1
                outcome = await self._swallow_oversized(reader, log_file, state, deadline)
                if outcome != "ok":
                    return outcome
                continue
            if not line:
                return "eof"
            log_file.write(line)
            if not line.endswith(b"\n"):  # trailing partial line at EOF
                log_file.write(b"\n")
            log_file.flush()
            state.bytes_logged += len(line)
            self._maybe_heartbeat(process_id, state, loop.time())
            self._parse_line_into(adapter, line, state)

    async def _swallow_oversized(
        self,
        reader: asyncio.StreamReader,
        log_file: BinaryIO,
        state: _StreamState,
        deadline: float,
    ) -> str:
        """Consume the remainder of an oversized line up to its newline; append
        the truncation marker plus the last recovered tail chunk (bounded by the
        readline limit) to the log. Returns 'ok', 'eof' or 'deadline'."""
        loop = asyncio.get_running_loop()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                self._write_truncation(log_file, b"", state)
                return "deadline"
            try:
                chunk = await asyncio.wait_for(reader.readline(), remaining)
            except TimeoutError:
                self._write_truncation(log_file, b"", state)
                return "deadline"
            except ValueError:
                continue  # still inside the oversized line; another window discarded
            self._write_truncation(log_file, chunk, state)
            if not chunk:
                return "eof"
            # readline returns a non-newline-terminated chunk only at EOF.
            return "ok" if chunk.endswith(b"\n") else "eof"

    def _write_truncation(self, log_file: BinaryIO, tail: bytes, state: _StreamState) -> None:
        log_file.write(TRUNCATION_MARKER)
        if tail:
            log_file.write(tail)
            if not tail.endswith(b"\n"):
                log_file.write(b"\n")
            state.bytes_logged += len(tail)
        log_file.flush()

    def _parse_line_into(self, adapter: CliAdapter, line: bytes, state: _StreamState) -> None:
        stripped = line.strip()
        if not stripped:
            return  # blank keepalive lines are not garbage
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, UnicodeDecodeError):
            state.garbage_lines += 1
            return
        if not isinstance(obj, dict):
            state.garbage_lines += 1
            return
        state.absorb(adapter.parse_line(obj))

    def _maybe_heartbeat(self, process_id: int, state: _StreamState, now: float) -> None:
        """§5.2: heartbeat_at written at most once per heartbeat_min_interval_s —
        per-line commits are pure write amplification."""
        if now - state.last_heartbeat < self._cfg.process.heartbeat_min_interval_s:
            return
        state.last_heartbeat = now
        with self._db.transaction() as conn:
            db.heartbeat_process(conn, process_id, utc_now())


# ------------------------------------------------------------- process utilities


def _signal_group(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    """Deliver a signal to the child's process GROUP (§5.3): agent CLIs spawn
    their own subprocess trees which must die with them. start_new_session ⇒
    pgid == child pid. Already-dead group = goal achieved, never an error."""
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _leader_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cmdline_matches(pid: int, recorded_cmdline: str) -> bool:
    """Compare /proc/<pid>/cmdline against the registry cmdline (§5.5a 'pid alive
    with matching cmdline'). Unreadable/absent /proc ⇒ no match — never kill what
    cannot be identified.

    PUBLIC since CCR-3 (closes the D-0016 disposition): the single tolerant
    predicate, consumed by both this runner's ``kill_running`` and the
    scheduler's §5.5a orphan sweep — one source, no drifting copies.

    Tolerant form (D-0014 item 2, empirically verified 2026-06-11 on server e9):
    interpreter wrapping rewrites the live argv of script-shebang CLIs —
    spawning ``codex …`` (``#!/usr/bin/env node`` script) yields a live cmdline
    of ``node /…/bin/codex …``, while ``claude`` (native ELF) and the test stub
    (spawned as an explicit ``<python> <script> …`` argv) match exactly. The
    strict equality would therefore misread every live codex orphan as pid
    reuse and leave its process group running unsupervised. Accepted matches:

    - exact: ``shlex.join(live) == recorded`` (no wrapping); or
    - wrapped: the recorded argv is a SUFFIX of the live argv, where the
      recorded argv[0] matches its aligned live token by basename (PATH/shebang
      resolution may absolutize it) and every following recorded token matches
      exactly. Interpreter prefixes (``node``, ``python3``, …) before that
      suffix are tolerated; any argument divergence still refuses the match —
      never kill what cannot be identified.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    live = [p.decode("utf-8", errors="surrogateescape") for p in raw.split(b"\0") if p]
    if not live:
        return False
    if shlex.join(live) == recorded_cmdline:
        return True
    try:
        recorded = shlex.split(recorded_cmdline)
    except ValueError:
        return False
    if not recorded or len(live) < len(recorded):
        return False
    anchor = len(live) - len(recorded)
    if live[anchor + 1 :] != recorded[1:]:
        return False
    return Path(live[anchor]).name == Path(recorded[0]).name
