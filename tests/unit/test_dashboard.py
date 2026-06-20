"""Unit tests for sf_factory.dashboard (dashboard design §8 unit list).

Covers: hostile-content escaping (R5) + CSP pinning (incl. the session page's
connect-src 'self'); RO/GLOSS closure (R1/R2 — DDL gate kinds + golden-config
risk classes); per-card containment; the unmapped `business` gate card;
fmt_founder_ts golden; R3 recommendation parsing; the §3 answer-path matrix
(zero writes, never KeyError), idempotent double-tap (sequential + concurrent),
the cross-process loser, and D-0015 order under fault injection; start()/
resolve_bind_host failure modes; the §6 dashboard supervisor (containment,
paging dedup); Decision-Session manager bounds, transcript-before-spawn, resume
continuity, restart rebuild, cancellation teardown and the tools-off route
chain (§4); the D-0019 §3.1a answer-path quiesce pins (answer during a busy
turn -> byte-stable registered transcript + green verify_integrity,
post_message refused while answering with zero writes, a failed answer clears
the answering flag).

Fixtures beyond the frozen tests/conftest.py are defined locally (design §9).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import stat
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sf_factory import dashboard as dash
from sf_factory import db as fdb
from sf_factory import runtime_settings as rs
from sf_factory import scheduler as sched_mod
from sf_factory.artifacts import verify_integrity
from sf_factory.config import FactoryConfig, load_config
from sf_factory.db import MIGRATIONS_DIR, Database
from sf_factory.models import (
    GATE_ANSWERS,
    DecisionRequest,
    FactoryError,
    GitError,
    NotifyError,
    Phase,
    PhaseState,
    SchedCategory,
    Stage,
    StageState,
    utc_now,
)
from sf_factory.runner import ADAPTERS, AgentResult

REPO_ROOT = Path(__file__).resolve().parents[2]

# ----------------------------------------------------------------- local env


def _init_git(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "factory@test"],
        ["git", "config", "user.name", "factory"],
    ):
        subprocess.run(args, cwd=path, check=True, capture_output=True)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"], cwd=path, check=True, capture_output=True
    )


def _git(path: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


class FakeNotify:
    priority_decision = "high"
    priority_alert = "max"

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, str | None, str]] = []
        self.fail = fail

    async def publish(self, title, *, link=None, priority="default"):
        if self.fail:
            raise NotifyError("ntfy down (fake)")
        self.published.append((title, link, priority))


def _agent_result(
    *,
    result_text: str = "răspuns de la agent",
    session_id: str | None = "sess-1",
    tokens_in: int | None = 10,
    tokens_out: int | None = 10,
    timed_out: bool = False,
    exit_code: int | None = 0,
) -> AgentResult:
    return AgentResult(
        process_id=1,
        exit_code=exit_code,
        timed_out=timed_out,
        killed=False,
        declared_failure=False,
        result_text=result_text,
        session_id=session_id,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=None,
        garbage_lines=0,
        ndjson_log_path="(fake)",
        stderr_path="(fake)",
        duration_ms=1,
    )


class FakeSessionRunner:
    """run_agent stand-in for DecisionSessionManager tests: scripted results,
    optional hold gate (in-flight turns), records every call."""

    def __init__(self) -> None:
        self.calls: list[SimpleNamespace] = []
        self.results: list[AgentResult | Exception] = []
        self.gate: asyncio.Event | None = None
        self.transcript_at_call: list[str] = []
        self.transcript_probe: Path | None = None

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
        self.calls.append(
            SimpleNamespace(
                role=role,
                prompt=prompt,
                unit_id=unit_id,
                cwd=Path(cwd),
                timeout_s=timeout_s,
                resume_session=resume_session,
            )
        )
        if self.transcript_probe is not None:
            self.transcript_at_call.append(
                self.transcript_probe.read_text(encoding="utf-8")
                if self.transcript_probe.is_file()
                else ""
            )
        if self.gate is not None:
            await self.gate.wait()
        result = self.results.pop(0) if self.results else _agent_result()
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture()
def denv(config_dict: dict[str, Any], tmp_path: Path):
    """Dashboard test env: git factory repo at factory.home, the DB at
    process.db_path (the path GET handlers' ro connections open), fast bounds,
    a stub decision_session route."""
    home = Path(config_dict["factory"]["home"])
    _init_git(home)
    config_dict["founder_channel"]["dashboard"] = {
        "bind": "127.0.0.1",
        "port": 0,
        "refresh_s": 30,
        "answer_timeout_s": 5,
        "read_timeout_s": 2,
        "max_request_bytes": 4096,
        "restart_delay_s": 0.02,
        "page_every_n_restarts": 3,
        "bind_recheck_s": 60,
    }
    config_dict["founder_channel"]["decision_session"] = {
        "max_turns": 3,
        "turn_timeout_s": 7,
        "budget_tokens": 1000,
        "poll_s": 0.05,
    }
    config_dict["models"]["decision_session"] = {
        "cli": "stub",
        "model": "stub-model",
        "mode": "print",
        "tools": "none",
    }
    # §11 (CCR-10): pricing for the cost-surface tests; models WITHOUT a key
    # (e.g. 'model-fara-pret') exercise the explicit missing-price marker.
    config_dict["pricing"] = {
        "usd_per_mtok": {
            "stub-model": {"input": 2.0, "output": 10.0},
            "fable": {"input": 10, "output": 50},
            "sonnet": {"input": 3, "output": 15},
        }
    }
    cfg = FactoryConfig.model_validate(config_dict)
    database = Database(Path(config_dict["process"]["db_path"]), busy_timeout_ms=5000)
    database.open()
    database.migrate(MIGRATIONS_DIR)
    yield SimpleNamespace(cfg=cfg, db=database, home=home)
    database.close()


def _seed_unit(env, *, stage_id: str = "ph.s1", risk: str = "critical") -> None:
    now = utc_now()
    with env.db.transaction() as conn:
        fdb.insert_phase(
            conn,
            Phase(
                id="ph",
                project="proj",
                name="Fundația",
                state=PhaseState.RUNNING,
                branch="phase/ph",
                plan_artifact_id=None,
                created_at=now,
                updated_at=now,
            ),
        )
        fdb.insert_stage(
            conn,
            Stage(
                id=stage_id,
                phase_id="ph",
                name="Schema de bază",
                risk_class=risk,
                state=StageState.AWAITING_HUMAN,
                branch=f"stage/{stage_id}",
                worktree_path=None,
                spec_artifact_id=None,
                created_at=now,
                updated_at=now,
            ),
        )


def _seed_decision(
    env,
    *,
    stage_id: str = "ph.s1",
    gate_kind: str = "critical_stage",
    body: str | None = None,
    created_at: str | None = None,
) -> int:
    """Pending decision whose request artifact is a real factory-repo file."""
    unit_dir = env.home / "_factory" / "stages" / stage_id
    unit_dir.mkdir(parents=True, exist_ok=True)
    path = unit_dir / "decision-request.md"
    path.write_text(
        body
        if body is not None
        else "# Cerere de decizie\n\nÎntrebare de test.\n\nRecomandare: approved\n",
        encoding="utf-8",
    )
    from sf_factory.artifacts import register_artifact

    with env.db.transaction() as conn:
        ref = register_artifact(
            conn,
            unit_level="stage",
            unit_id=stage_id,
            kind="decision_request",
            repo="factory",
            repo_root=env.home,
            path=path,
            git_commit=None,
        )
        return fdb.insert_decision_request(
            conn,
            DecisionRequest(
                id=None,
                unit_level="stage",
                unit_id=stage_id,
                gate_kind=gate_kind,
                request_artifact_id=ref.id,
                status="pending",
                answer=None,
                answer_artifact_id=None,
                created_at=created_at or utc_now(),
                alerted_at=None,
                answered_at=None,
            ),
        )


def _write_counts(env) -> tuple[int, int, int]:
    conn = env.db.read()
    events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    refs = conn.execute("SELECT COUNT(*) FROM artifact_refs").fetchone()[0]
    answered = conn.execute(
        "SELECT COUNT(*) FROM decision_requests WHERE status='answered'"
    ).fetchone()[0]
    return int(events), int(refs), int(answered)


def _server(env) -> dash.DashboardServer:
    return dash.DashboardServer(env.cfg, env.db, FakeSessionRunner(), FakeNotify())


# ----------------------------------------------------- R1/R2 closure + format


def test_gloss_closure_covers_every_rendered_token_set(real_config_path) -> None:
    for member in (*StageState, *PhaseState, *SchedCategory):
        assert member.value in dash.GLOSS, member
    for options in GATE_ANSWERS.values():
        for token in options:
            assert token in dash.GLOSS, token
    for event_type in dash.INCIDENT_EVENT_TYPES:
        assert event_type in dash.GLOSS, event_type
    # The FULL DDL gate_kind set — glossed even where no executor consumes it yet.
    for gate_kind in ("critical_stage", "business", "phase_signoff", "escalation_tradeoff"):
        assert gate_kind in dash.GLOSS, gate_kind
    golden = load_config(real_config_path)
    for risk_class in golden.risk_classes:
        assert risk_class in dash.GLOSS, risk_class


def test_ro_values_carry_no_english_ui_words() -> None:
    """R1 denylist spot-check over every founder-visible literal."""
    import re

    denylist = re.compile(
        r"\b(the|and|answer|decision|request|stage|phase|error|failed|pending|"
        r"please|click|loading|unknown)\b",
        re.IGNORECASE,
    )
    for table in (dash.RO, dash.GLOSS):
        for key, value in table.items():
            match = denylist.search(value)
            assert match is None, f"{key}: english word {match.group(0)!r} in {value!r}"


def test_unknown_token_renders_visible_missing_gloss_marker() -> None:
    assert dash._glossed("no_such_token") == "no_such_token (etichetă lipsă)"


def test_fmt_founder_ts_golden_chisinau() -> None:
    # Winter (EET, UTC+2) and summer (EEST, UTC+3) — DD-MM-YYYY HH:MM (R4).
    assert dash.fmt_founder_ts("2026-01-15T10:00:00Z", "Europe/Chisinau") == "15-01-2026 12:00"
    assert dash.fmt_founder_ts("2026-06-11T10:00:00Z", "Europe/Chisinau") == "11-06-2026 13:00"
    with pytest.raises(dash.DashboardError):
        dash.fmt_founder_ts("not-a-timestamp", "Europe/Chisinau")


def test_romanian_number_grouping() -> None:
    assert dash._fmt_int(300000) == "300.000"
    assert dash._fmt_int(999) == "999"


def test_fmt_ktok_thousands_no_decimals() -> None:
    """Founder 20-06: token counts in THOUSANDS, rounded, no decimals."""
    assert dash._fmt_ktok(12_547_709) == "12.548"  # the founder's golden example
    assert dash._fmt_ktok(364_000_000) == "364.000"
    assert dash._fmt_ktok(499) == "0"  # rounds down below 500
    assert dash._fmt_ktok(1500) == "2"  # 1.5 -> 2
    assert dash._fmt_ktok(0) == "0"


def test_fmt_mem_gb_mb_boundary() -> None:
    """Founder memory panel: >=1 GiB in GB (Romanian comma), below in MB; None -> '—'."""
    assert dash._fmt_mem(23_622_320_128) == "22,0 GB"  # the 22 GiB leash
    assert dash._fmt_mem(2_147_483_648) == "2,0 GB"  # 2 GiB swap
    assert dash._fmt_mem(680_000_000) == "648 MB"  # sub-GiB -> MB
    assert dash._fmt_mem(None) == "—"


def test_fmt_dur_founder_per_agent_timing() -> None:
    """Founder 20-06 per-agent duration: s / min / h Ym; 'în lucru'/'—' edges."""
    assert dash._fmt_dur("2026-06-20T10:00:00Z", "2026-06-20T10:00:45Z") == "45s"
    assert dash._fmt_dur("2026-06-20T10:00:00Z", "2026-06-20T10:18:00Z") == "18 min"
    assert dash._fmt_dur("2026-06-20T10:00:00Z", "2026-06-20T12:30:00Z") == "2h 30min"
    assert dash._fmt_dur("2026-06-20T10:00:00Z", "2026-06-20T12:00:00Z") == "2h"
    assert dash._fmt_dur("2026-06-20T10:00:00Z", None) == dash.RO["duration_running"]
    assert dash._fmt_dur(None, None) == "—"


# ------------------------------------------------------------ R3 recommendation


def test_recommendation_parsed_only_from_declared_options() -> None:
    options = ("approved", "rework:BUILD", "rework:SPEC")
    assert dash._parse_recommendation("text\nRecomandare: approved\n", options) == "approved"
    assert (
        dash._parse_recommendation("Recommendation: rework:BUILD\n", options)
        == "rework:BUILD"
    )
    assert dash._parse_recommendation("no marker at all", options) is None
    # Unmatched token: NEVER invented (R3).
    assert dash._parse_recommendation("Recomandare: deploy_now\n", options) is None
    assert dash._parse_recommendation("Recomandare: approved", ()) is None


# ------------------------------------------------------- render: hostile + cards

_HOSTILE = (
    "# Titlu\n<script>alert(1)</script>\n"
    '<img src=x onerror="alert(2)">\n'
    "[link](javascript:alert(3))\n"
    "```\n<svg/onload=alert(4)>\n```\n"
    "broken utf8 marker: �\n"
    "Recomandare: approved\n"
)


def test_hostile_artifact_content_is_escaped_never_executable(denv) -> None:
    _seed_unit(denv)
    _seed_decision(denv, body=_HOSTILE)
    view = dash.build_view(denv.cfg)
    page = dash.render_page(view, denv.cfg)
    assert "<script" not in page  # zero JS on the main page — escaped or absent
    assert "<img" not in page  # no live tag — only the &lt;img …&gt; text remains
    assert "<svg" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page  # visible as text
    assert "&lt;img src=x onerror=" in page  # escaped, structure preserved (R5)
    assert "javascript:alert(3)" in page  # plain text, never a hyperlink
    assert "<a href='javascript:" not in page
    # The R3 marker still parsed mechanically from the hostile body:
    assert dash.RO["recommended_badge"] in page


def test_broken_utf8_artifact_survives_replacement(denv, tmp_path) -> None:
    _seed_unit(denv)
    unit_dir = denv.home / "_factory" / "stages" / "ph.s1"
    unit_dir.mkdir(parents=True, exist_ok=True)
    raw = unit_dir / "decision-request.md"
    raw.write_bytes(b"intrebare \xff\xfe invalid bytes\n")
    from sf_factory.artifacts import register_artifact

    with denv.db.transaction() as conn:
        ref = register_artifact(
            conn,
            unit_level="stage",
            unit_id="ph.s1",
            kind="decision_request",
            repo="factory",
            repo_root=denv.home,
            path=raw,
            git_commit=None,
        )
        fdb.insert_decision_request(
            conn,
            DecisionRequest(
                id=None,
                unit_level="stage",
                unit_id="ph.s1",
                gate_kind="critical_stage",
                request_artifact_id=ref.id,
                status="pending",
                answer=None,
                answer_artifact_id=None,
                created_at=utc_now(),
                alerted_at=None,
                answered_at=None,
            ),
        )
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    assert "invalid bytes" in page  # rendered with replacement, page alive


def test_per_card_containment_one_poisoned_card_rest_renders(denv) -> None:
    _seed_unit(denv)
    good_id = _seed_decision(denv, body="Întrebare bună.\n")
    # Poisoned: the request ref resolves to NOTHING (no worktree, no commit, no
    # file, no HEAD blob) — assembly raises, containment must hold.
    from sf_factory.models import ArtifactRef

    with denv.db.transaction() as conn:
        ref_id = fdb.insert_artifact_ref(
            conn,
            ArtifactRef(
                id=None,
                unit_level="stage",
                unit_id="ph.s1",
                kind="decision_request",
                repo="factory",
                path="_factory/stages/ph.s1/vanished.md",
                sha256="2" * 64,
                git_commit=None,
                created_at=utc_now(),
            ),
        )
        bad_id = fdb.insert_decision_request(
            conn,
            DecisionRequest(
                id=None,
                unit_level="stage",
                unit_id="ph.s1",
                gate_kind="critical_stage",
                request_artifact_id=ref_id,
                status="pending",
                answer=None,
                answer_artifact_id=None,
                created_at=utc_now(),
                alerted_at=None,
                answered_at=None,
            ),
        )
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    assert "Întrebare bună." in page  # the good card rendered
    assert dash.RO["card_error"][:40] in page  # the bad one = explicit RO error card
    assert f"id='decision/{good_id}'" in page
    assert f"id='decision/{bad_id}'" in page  # anchor still present (deep links land)


def test_unmapped_business_gate_renders_without_buttons_naming_cli_decide(denv) -> None:
    _seed_unit(denv)
    request_id = _seed_decision(denv, gate_kind="business", body="Decizie de business.\n")
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    assert f"action='/decision/{request_id}/answer'" not in page  # no buttons
    assert "cli decide" in page  # RO notice names the emergency path
    assert "decizie de business (business)" in page  # gate kind glossed (R2)


def test_card_title_age_and_anchor_format(denv) -> None:
    _seed_unit(denv)
    request_id = _seed_decision(denv, created_at="2026-06-11T10:00:00Z")
    view = dash.build_view(denv.cfg, now="2026-06-11T10:00:45Z")
    card = next(c for c in view.cards if c.request_id == request_id)
    assert card.unit_name == "Schema de bază"
    assert card.created_display.startswith("creată 11-06-2026 13:00 · acum 45s")
    page = dash.render_page(view, denv.cfg)
    assert f"Decizia #{request_id} — Etapa: Schema de bază (ph.s1)" in page
    assert "etapă critică — aprobare necesară (critical_stage)" in page


def test_health_strip_budget_and_incident(denv) -> None:
    _seed_unit(denv)
    with denv.db.transaction() as conn:
        pid = fdb.insert_process(
            conn,
            __import__("sf_factory.models", fromlist=["ProcessRecord"]).ProcessRecord(
                id=None,
                unit_level="stage",
                unit_id="ph.s1",
                kind="agent",
                role="builder_routine",
                cp_id=None,
                session_id=None,
                pid=None,
                cmdline="stub",
                cwd=None,
                state="exited",
                exit_code=0,
                ndjson_log_path=None,
                spawned_at=utc_now(),
                heartbeat_at=None,
                ended_at=utc_now(),
            ),
        )
        fdb.insert_token_usage(
            conn,
            process_id=pid,
            unit_level="stage",
            unit_id="ph.s1",
            role="builder_routine",
            model="stub-model",
            tokens_in=120000,
            tokens_out=22000,
            cost_usd=1.25,
        )
        fdb.insert_event(
            conn,
            unit_level="stage",
            unit_id="ph.s1",
            event_type="usage_missing",
            actor="control_plane",
            payload={},
        )
    # Fresh liveness file -> no red marker.
    liveness = denv.home / ".factory" / "liveness"
    liveness.parent.mkdir(parents=True, exist_ok=True)
    liveness.write_text("tick\n", encoding="utf-8")
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    assert "142 mii tokeni" in page  # tokens in THOUSANDS: 142_000 -> "142" (founder 20-06)
    assert "Efectiv (mii)" in page and "Total (mii)" in page  # budget table: effective vs total
    assert "%" in page
    assert dash.RO["pulse_stale"] not in page
    assert "consum de tokeni neraportat (usage_missing)" in page  # Ultimul incident
    # Stale liveness -> red marker.
    old = 10_000
    os.utime(liveness, (os.stat(liveness).st_mtime - old, os.stat(liveness).st_mtime - old))
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    assert dash.RO["pulse_stale"] in page


def test_health_strip_capacity_hold_line(denv) -> None:
    """CCR-11 (D-0037 item 8): an open capacity_hold_started/_ended event pair
    renders ONE extra Puls line („pauză de capacitate — sondez la fiecare X
    min", X from capacity_governor.probe_interval_s); a closed pair renders
    nothing. Read-path only — no governor needs to be running."""
    _seed_unit(denv)
    expected = dash.RO["capacity_hold"].format(minutes=5)  # default 300s
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    assert expected not in page  # no hold events yet
    with denv.db.transaction() as conn:
        fdb.insert_event(
            conn,
            unit_level="factory",
            unit_id=None,
            event_type="capacity_hold_started",
            actor="control_plane",
            payload={"signature": "usage limit", "role": "validator", "process_id": 1},
        )
    view = dash.build_view(denv.cfg)
    assert view.health.capacity_hold_display == expected
    page = dash.render_page(view, denv.cfg)
    assert expected in page
    with denv.db.transaction() as conn:
        fdb.insert_event(
            conn,
            unit_level="factory",
            unit_id=None,
            event_type="capacity_hold_ended",
            actor="control_plane",
            payload={"probe_process_id": 2},
        )
    view = dash.build_view(denv.cfg)
    assert view.health.capacity_hold_display is None
    assert expected not in dash.render_page(view, denv.cfg)


def test_health_strip_finding_recurrence_line(denv) -> None:
    """D-0059: a finding_recurrence event on an ACTIVE (non-terminal) stage renders
    a Puls warning line (the recurrence backstop, architect-operations §1); once the
    stage is DONE the line clears (a recurrence on a finished stage is history)."""
    from sf_factory.models import Level

    _seed_unit(denv)  # stage ph.s1
    with denv.db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "AUDIT")  # active
    assert "recurență" not in dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    with denv.db.transaction() as conn:
        fdb.insert_event(
            conn,
            unit_level="stage",
            unit_id="ph.s1",
            event_type="finding_recurrence",
            actor="control_plane",
            payload={
                "auditor": "auditor_cross_model",
                "recurred": [{"ref": "CE-1", "prior_disposition": "settled"}],
            },
        )
    view = dash.build_view(denv.cfg)
    assert view.health.finding_recurrence_display == dash.RO["finding_recurrence"].format(n=1)
    assert "recurență" in dash.render_page(view, denv.cfg)
    # a recurrence on a DONE stage is history -> the line clears
    with denv.db.transaction() as conn:
        fdb.set_unit_state(conn, Level.STAGE, "ph.s1", "DONE")
    assert dash.build_view(denv.cfg).health.finding_recurrence_display is None


def test_plan_section_groups_and_footer(denv) -> None:
    _seed_unit(denv)
    now = utc_now()
    with denv.db.transaction() as conn:
        fdb.insert_stage(
            conn,
            Stage(
                id="ph.done",
                phase_id="ph",
                name="Etapa gata",
                risk_class="routine",
                state=StageState.DONE,
                branch=None,
                worktree_path=None,
                spec_artifact_id=None,
                created_at=now,
                updated_at=now,
            ),
        )
        fdb.insert_stage(
            conn,
            Stage(
                id="ph.todo",
                phase_id="ph",
                name="Etapa planificată",
                risk_class="routine",
                state=StageState.PENDING,
                branch=None,
                worktree_path=None,
                spec_artifact_id=None,
                created_at=now,
                updated_at=now,
            ),
        )
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    assert dash.RO["plan_done_group"] in page
    assert dash.RO["plan_pending_group"] in page
    assert dash.RO["plan_footer"] in page


# --------------------------------------------------------------- answer matrix


async def test_answer_unknown_request_zero_writes(denv) -> None:
    server = _server(denv)
    before = _write_counts(denv)
    result = await server.answer(424242, "approved", via="dashboard")
    assert result.outcome is dash.AnswerOutcome.UNKNOWN_REQUEST
    assert _write_counts(denv) == before


async def test_answer_invalid_option_and_unmapped_gate_zero_writes(denv) -> None:
    _seed_unit(denv)
    critical = _seed_decision(denv)
    business = _seed_decision(denv, gate_kind="business", body="Business.\n")
    server = _server(denv)
    before = _write_counts(denv)
    result = await server.answer(critical, "deploy_now", via="dashboard")
    assert result.outcome is dash.AnswerOutcome.INVALID_OPTION
    # Unmapped (level, gate_kind): every option -> INVALID_OPTION, never KeyError.
    for option in ("approved", "yes", ""):
        result = await server.answer(business, option, via="dashboard")
        assert result.outcome is dash.AnswerOutcome.INVALID_OPTION
    assert _write_counts(denv) == before


async def test_answer_happy_path_d0015_order_and_redispatch_signal(denv) -> None:
    _seed_unit(denv)
    request_id = _seed_decision(denv)
    server = _server(denv)
    result = await server.answer(request_id, "approved", via="dashboard")
    assert result.outcome is dash.AnswerOutcome.ANSWERED

    conn = denv.db.read()
    row = conn.execute(
        "SELECT * FROM decision_requests WHERE id = ?", (request_id,)
    ).fetchone()
    assert row["status"] == "answered" and row["answer"] == "approved"
    ref = conn.execute(
        "SELECT * FROM artifact_refs WHERE id = ?", (row["answer_artifact_id"],)
    ).fetchone()
    assert ref["kind"] == "decision_answer" and ref["repo"] == "factory"
    assert ref["git_commit"] == _git(denv.home, "rev-parse", "HEAD")
    shown = _git(denv.home, "show", f"{ref['git_commit']}:{ref['path']}")
    assert "answer: approved" in shown and "answered_via: dashboard" in shown
    events = conn.execute(
        "SELECT * FROM events WHERE event_type='decision_answered'"
    ).fetchall()
    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    assert payload["via"] == "dashboard" and events[0]["actor"] == "founder"


async def test_answer_double_tap_sequential_and_concurrent(denv) -> None:
    _seed_unit(denv)
    request_id = _seed_decision(denv)
    server = _server(denv)
    first, second = await asyncio.gather(
        server.answer(request_id, "approved", via="dashboard"),
        server.answer(request_id, "approved", via="dashboard"),
    )
    outcomes = {first.outcome, second.outcome}
    assert outcomes == {dash.AnswerOutcome.ANSWERED, dash.AnswerOutcome.ALREADY_ANSWERED}
    third = await server.answer(request_id, "rework:BUILD", via="dashboard")
    assert third.outcome is dash.AnswerOutcome.ALREADY_ANSWERED
    conn = denv.db.read()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='decision_answered'"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT answer FROM decision_requests WHERE id = ?", (request_id,)
        ).fetchone()["answer"]
        == "approved"
    )


async def test_answer_cross_process_loser_maps_to_already_answered(
    denv, monkeypatch
) -> None:
    """§3.3: a cli decide completing its WHOLE commit+tx inside step 2's await
    window collides only at answer_decision's pending guard -> explicit no-op,
    never a 500, no second answer row."""
    _seed_unit(denv)
    request_id = _seed_decision(denv)
    server = _server(denv)
    real_commit_paths = dash.commit_paths

    async def rival_commit_paths(*args, **kwargs):
        sha = await real_commit_paths(*args, **kwargs)
        with denv.db.transaction() as conn:  # the rival lands its full answer here
            fdb.answer_decision(conn, request_id, "rework:SPEC", None)
        return sha

    monkeypatch.setattr(dash, "commit_paths", rival_commit_paths)
    result = await server.answer(request_id, "approved", via="dashboard")
    assert result.outcome is dash.AnswerOutcome.ALREADY_ANSWERED
    conn = denv.db.read()
    row = conn.execute(
        "SELECT * FROM decision_requests WHERE id = ?", (request_id,)
    ).fetchone()
    assert row["answer"] == "rework:SPEC"  # the rival's answer stands
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='decision_answered'"
        ).fetchone()[0]
        == 0
    )  # the dashboard's tx rolled back whole


async def test_answer_tx_failure_then_retry_converges_with_head_commit(
    denv, monkeypatch
) -> None:
    """D-0015 order: tx fails -> artifact already committed, row still pending;
    the retry's commit_paths returns None and the ref pins rev-parse HEAD.

    Time is frozen for the WHOLE test: the artifact embeds a second-granularity
    answered_at, so the §3.2 byte-identical-retry branch (commit_paths -> None
    -> rev-parse HEAD) is only deterministic when both answer() calls render
    the same timestamp — without the freeze, a wall-clock second boundary under
    suite load produces a (harmless, but HEAD-moving) superseding commit."""
    _seed_unit(denv)
    request_id = _seed_decision(denv)
    server = _server(denv)
    monkeypatch.setattr(dash, "utc_now", lambda: "2026-06-11T12:00:00Z")

    def boom(*args, **kwargs):
        raise RuntimeError("injected tx failure")

    # The boom injection gets its OWN context: undoing it must not undo the
    # frozen utc_now above (monkeypatch.undo() would drop both).
    with pytest.MonkeyPatch.context() as boom_patch:
        boom_patch.setattr(dash.fdb, "answer_decision", boom)
        with pytest.raises(RuntimeError, match="injected"):
            await server.answer(request_id, "approved", via="dashboard")

    conn = denv.db.read()
    row = conn.execute(
        "SELECT status FROM decision_requests WHERE id = ?", (request_id,)
    ).fetchone()
    assert row["status"] == "pending"  # tx rolled back whole
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM artifact_refs WHERE kind='decision_answer'"
        ).fetchone()[0]
        == 0
    )
    answer_file = f"_factory/stages/ph.s1/decision-answer-{request_id}.md"
    head = _git(denv.home, "rev-parse", "HEAD")
    assert "answer: approved" in _git(denv.home, "show", f"HEAD:{answer_file}")

    result = await server.answer(request_id, "approved", via="dashboard")
    assert result.outcome is dash.AnswerOutcome.ANSWERED
    assert _git(denv.home, "rev-parse", "HEAD") == head  # nothing new to commit
    ref = (
        denv.db.read()
        .execute("SELECT * FROM artifact_refs WHERE kind='decision_answer'")
        .fetchone()
    )
    assert ref["git_commit"] == head  # rev-parse HEAD, never NULL


async def test_answer_commit_failure_means_zero_db_writes(denv, monkeypatch) -> None:
    _seed_unit(denv)
    request_id = _seed_decision(denv)
    server = _server(denv)

    async def git_down(*args, **kwargs):
        raise GitError("index locked (injected)")

    monkeypatch.setattr(dash, "commit_paths", git_down)
    before = _write_counts(denv)
    with pytest.raises(GitError):
        await server.answer(request_id, "approved", via="dashboard")
    assert _write_counts(denv) == before
    row = (
        denv.db.read()
        .execute("SELECT status FROM decision_requests WHERE id = ?", (request_id,))
        .fetchone()
    )
    assert row["status"] == "pending"


# ------------------------------------------------------- bind / start failures


def test_resolve_bind_host_literal_stub_and_failure(
    denv, tmp_path, monkeypatch
) -> None:
    assert dash.resolve_bind_host(denv.cfg) == "127.0.0.1"  # literal pass-through

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "tailscale"
    fake.write_text("#!/bin/sh\necho 100.64.0.7\necho 100.64.0.8\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    cfg_dict = json.loads(denv.cfg.model_dump_json())
    cfg_dict["founder_channel"]["dashboard"]["bind"] = "tailscale"
    cfg = FactoryConfig.model_validate(cfg_dict)
    assert dash.resolve_bind_host(cfg) == "100.64.0.7"  # first address

    fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(FactoryError):
        dash.resolve_bind_host(cfg)

    fake.write_text("#!/bin/sh\necho\n", encoding="utf-8")  # empty output
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(FactoryError):
        dash.resolve_bind_host(cfg)


def test_start_on_in_use_port_is_factory_error(denv) -> None:
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        cfg_dict = json.loads(denv.cfg.model_dump_json())
        cfg_dict["founder_channel"]["dashboard"]["port"] = port
        cfg = FactoryConfig.model_validate(cfg_dict)
        server = dash.DashboardServer(cfg, denv.db, FakeSessionRunner(), FakeNotify())
        with pytest.raises(FactoryError, match="bind"):
            server.start()
        assert server.bound_address is None
    finally:
        blocker.close()


def test_start_sets_bound_address_with_real_ephemeral_port(denv) -> None:
    server = _server(denv)
    server.start()
    try:
        assert server.bound_address is not None
        host, port = server.bound_address
        assert host == "127.0.0.1" and port > 0  # the §6 readiness signal
        server.start()  # idempotent re-call keeps the same bind
        assert server.bound_address == (host, port)
    finally:
        # Bound but never served: close the socket directly (shutdown() waits
        # on serve_forever, which only serve() runs).
        server._server.server_close()


# ------------------------------------------------------------- HTTP + CSP pins


async def _serving(server: dash.DashboardServer):
    server.start()
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server._loop is not None:
            break
        await asyncio.sleep(0.01)
    return task


def _http(method: str, url: str, body: bytes | None = None):
    request = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


async def test_http_csp_pins_and_routes(denv) -> None:
    _seed_unit(denv)
    request_id = _seed_decision(denv)
    server = _server(denv)
    task = await _serving(server)
    try:
        host, port = server.bound_address
        base = f"http://{host}:{port}"

        status, headers, page = await asyncio.to_thread(_http, "GET", f"{base}/")
        assert status == 200
        csp = headers["Content-Security-Policy"]
        # form-action/base-uri/frame-ancestors do NOT fall back to default-src:
        for directive in (
            "default-src 'none'",
            "style-src 'unsafe-inline'",
            "form-action 'self'",
            "base-uri 'none'",
            "frame-ancestors 'none'",
        ):
            assert directive in csp
        assert "script-src" not in csp  # zero JS on the main page
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert f"id='decision/{request_id}'" in page

        status, headers, body = await asyncio.to_thread(
            _http, "GET", f"{base}/decision/{request_id}/session"
        )
        assert status == 200
        csp = headers["Content-Security-Policy"]
        assert "connect-src 'self'" in csp  # the only thing keeping the poll alive
        nonce = csp.split("'nonce-")[1].split("'")[0]
        assert f"nonce='{nonce}'" in body  # script carries the SAME nonce
        for directive in ("form-action 'self'", "base-uri 'none'", "frame-ancestors 'none'"):
            assert directive in csp

        status, headers, body = await asyncio.to_thread(
            _http, "GET", f"{base}/decision/{request_id}/session/poll?after=0"
        )
        assert status == 200
        assert headers["Content-Type"].startswith("application/json")
        assert json.loads(body)["turns"] == []

        status, _, body = await asyncio.to_thread(_http, "GET", f"{base}/nu-exista")
        assert status == 404 and dash.RO["not_found"] in body

        # POST body bounded by max_request_bytes -> 413 RO.
        status, _, body = await asyncio.to_thread(
            _http,
            "POST",
            f"{base}/decision/{request_id}/answer",
            b"option=" + b"x" * 8192,
        )
        assert status == 413 and dash.RO["request_too_large"] in body

        # Invalid option over HTTP: 400 listing the valid options in Romanian.
        status, _, body = await asyncio.to_thread(
            _http, "POST", f"{base}/decision/{request_id}/answer", b"option=deploy"
        )
        assert status == 400
        assert dash.RO["answer_invalid_option"] in body
        assert "aprobă (approved)" in body

        # The single write path over HTTP: 303 back to the card anchor.
        status, headers, _ = await asyncio.to_thread(
            _http, "POST", f"{base}/decision/{request_id}/answer", b"option=approved"
        )
        # urllib follows the 303 to GET / -> lands 200 on the page.
        assert status == 200
        row = (
            denv.db.read()
            .execute("SELECT status FROM decision_requests WHERE id=?", (request_id,))
            .fetchone()
        )
        assert row["status"] == "answered"

        # Double-tap over HTTP: explicit RO no-op page, still 200.
        status, _, body = await asyncio.to_thread(
            _http, "POST", f"{base}/decision/{request_id}/answer", b"option=approved"
        )
        assert status == 200 and dash.RO["answered_already"] in body
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_artifact_view_escapes_content(denv) -> None:
    _seed_unit(denv)
    _seed_decision(denv, body="<script>boom()</script>\n")
    server = _server(denv)
    task = await _serving(server)
    try:
        host, port = server.bound_address
        ref_id = (
            denv.db.read()
            .execute("SELECT MIN(id) FROM artifact_refs")
            .fetchone()[0]
        )
        status, _, body = await asyncio.to_thread(
            _http, "GET", f"http://{host}:{port}/artifact/{ref_id}"
        )
        assert status == 200
        assert "<script>boom" not in body and "&lt;script&gt;boom" in body
        status, _, body = await asyncio.to_thread(
            _http, "GET", f"http://{host}:{port}/artifact/999999"
        )
        assert status == 404 and dash.RO["artifact_missing"] in body
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_post_negative_content_length_rejected_413(denv) -> None:
    """A negative Content-Length must never reach rfile.read(): read(-1) is an
    unbounded read-until-EOF that pins one handler thread while the client
    keeps the socket open (read_timeout_s bounds stalls, not a fed stream).
    Rejected 413, exactly like an oversize body."""
    _seed_unit(denv)
    request_id = _seed_decision(denv)
    server = _server(denv)
    task = await _serving(server)
    try:
        host, port = server.bound_address

        def raw_post() -> bytes:
            with socket.create_connection((host, port), timeout=5) as sock:
                sock.sendall(
                    (
                        f"POST /decision/{request_id}/answer HTTP/1.1\r\n"
                        f"Host: {host}:{port}\r\n"
                        "Content-Length: -1\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii")
                )
                chunks = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks += chunk
                return chunks

        response = await asyncio.to_thread(raw_post)
        assert b" 413 " in response.split(b"\r\n", 1)[0]
        assert dash.RO["request_too_large"].encode("utf-8") in response
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ------------------------------------------------------------ supervisor (§6/§7)


class CrashingDashboard:
    """Duck-typed stand-in: serve() always crashes; no bind to drift-check."""

    def __init__(self) -> None:
        self.serves = 0
        self.bound_address = None

    async def serve(self) -> None:
        self.serves += 1
        raise RuntimeError(f"boom #{self.serves}")


def _make_scheduler(db, cfg, *, notify: FakeNotify, dashboard) -> sched_mod.Scheduler:
    from sf_factory.statemachine import StateMachine

    return sched_mod.Scheduler(db, StateMachine(db), cfg, {}, notify, dashboard=dashboard)


async def _run_scheduler_until(scheduler, predicate, timeout: float = 10.0) -> None:
    task = asyncio.create_task(scheduler.run_forever())
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return
            assert not task.done(), task
            await asyncio.sleep(0.02)
        raise AssertionError("supervisor condition not reached in time")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_supervisor_contains_crashes_pages_deduped(denv) -> None:
    """§7 row 1: crash -> 'alert' event EVERY restart (counter in payload),
    max-priority page on crash 1 then every Nth (page_every_n_restarts=3),
    scheduler loop unaffected (it keeps ticking the liveness file)."""
    crashing = CrashingDashboard()
    notify = FakeNotify()
    scheduler = _make_scheduler(denv.db, denv.cfg, notify=notify, dashboard=crashing)

    def four_restarts() -> bool:
        rows = (
            denv.db.read()
            .execute(
                "SELECT COUNT(*) FROM events WHERE event_type='alert'"
                " AND json_extract(payload_json, '$.kind')='dashboard_crashed'"
            )
            .fetchone()
        )
        return int(rows[0]) >= 4

    await _run_scheduler_until(scheduler, four_restarts)
    rows = (
        denv.db.read()
        .execute(
            "SELECT payload_json FROM events WHERE event_type='alert'"
            " AND json_extract(payload_json, '$.kind')='dashboard_crashed'"
            " ORDER BY seq"
        )
        .fetchall()
    )
    counters = [json.loads(r["payload_json"])["restarts"] for r in rows]
    assert counters[:4] == [1, 2, 3, 4]  # audit trail on EVERY restart
    pages = [p for p in notify.published if "Dashboard căzut" in p[0]]
    # Pages fire on restart 1 then every 3rd — never one per restart; the last
    # in-flight iteration may have been cancelled between event and publish,
    # hence the ±1 tolerance.
    expected = len([c for c in counters if c == 1 or c % 3 == 0])
    assert expected - 1 <= len(pages) <= expected
    assert 1 <= len(pages) < len(counters)  # deduplicated, but never silent
    assert all(p[2] == "max" for p in pages)
    # The scheduler loop kept ticking: the liveness file exists and is fresh.
    liveness = denv.home / ".factory" / "liveness"
    assert liveness.is_file()


async def test_supervisor_publish_failure_never_escapes(denv) -> None:
    """The supervisor's own page follows the §6 NotifyError contract:
    alert_delivery_failed event, never re-raise — the loop keeps running."""
    crashing = CrashingDashboard()
    scheduler = _make_scheduler(
        denv.db, denv.cfg, notify=FakeNotify(fail=True), dashboard=crashing
    )

    def delivery_failed_logged() -> bool:
        rows = (
            denv.db.read()
            .execute(
                "SELECT COUNT(*) FROM events WHERE event_type='alert_delivery_failed'"
                " AND json_extract(payload_json, '$.kind')='dashboard_crashed'"
            )
            .fetchone()
        )
        return int(rows[0]) >= 1 and crashing.serves >= 2

    await _run_scheduler_until(scheduler, delivery_failed_logged)


async def test_supervisor_publish_unexpected_exception_never_escapes(denv) -> None:
    """Hardening beyond the declared §6 NotifyError: a publisher defect of ANY
    exception type is contained the same way (alert_delivery_failed event,
    supervisor keeps restarting) — nothing escapes into the TaskGroup."""

    class DefectiveNotify(FakeNotify):
        async def publish(self, title, *, link=None, priority="default"):
            raise RuntimeError("publisher bug (injected)")

    crashing = CrashingDashboard()
    scheduler = _make_scheduler(
        denv.db, denv.cfg, notify=DefectiveNotify(), dashboard=crashing
    )

    def delivery_failed_logged() -> bool:
        rows = (
            denv.db.read()
            .execute(
                "SELECT COUNT(*) FROM events WHERE event_type='alert_delivery_failed'"
                " AND json_extract(payload_json, '$.kind')='dashboard_crashed'"
            )
            .fetchone()
        )
        return int(rows[0]) >= 1 and crashing.serves >= 2

    await _run_scheduler_until(scheduler, delivery_failed_logged)


async def test_reap_serve_task_suppresses_outcome_propagates_own_cancel() -> None:
    """_reap_serve_task contract: the serve task's OWN outcome (cancelled or
    crashed) is suppressed, but the supervisor's own cancellation landing
    during the await re-raises — Scheduler._run cancels the supervisor exactly
    once, so a swallowed cancel would leave the TaskGroup never closing."""
    # 1. Cancelled serve task: its CancelledError outcome is suppressed.
    hung = asyncio.create_task(asyncio.sleep(3600))
    await asyncio.sleep(0)
    hung.cancel()
    await sched_mod.Scheduler._reap_serve_task(hung)  # returns, raises nothing

    # 2. Crashed serve task: its exception is suppressed (and retrieved).
    async def crash() -> None:
        raise RuntimeError("serve crashed (injected)")

    crashed = asyncio.create_task(crash())
    await asyncio.sleep(0)
    await sched_mod.Scheduler._reap_serve_task(crashed)

    # 3. The reaper's OWN cancellation propagates even while serve_task
    # resists its cancel — the exact swallowed-shutdown-cancel scenario.
    release = asyncio.Event()

    async def stubborn() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()  # ignore the first cancel for a moment
            raise

    serve = asyncio.create_task(stubborn())
    await asyncio.sleep(0)  # stubborn parks on its event
    serve.cancel()
    reaper = asyncio.create_task(sched_mod.Scheduler._reap_serve_task(serve))
    await asyncio.sleep(0.05)  # reaper parks on `await serve_task`
    assert not reaper.done()
    reaper.cancel()  # the one-shot supervisor.cancel() from Scheduler._run
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await reaper
    assert reaper.cancelled()  # NOT swallowed — shutdown proceeds
    with pytest.raises(asyncio.CancelledError):
        await serve


async def test_run_until_blocked_cancels_supervisor_and_returns(denv) -> None:
    """The supervisor task is excluded from quiescence accounting and cancelled
    on the run_until_blocked exit path — the TaskGroup closes (§6)."""
    crashing = CrashingDashboard()
    scheduler = _make_scheduler(denv.db, denv.cfg, notify=FakeNotify(), dashboard=crashing)
    await asyncio.wait_for(scheduler.run_until_blocked(), timeout=10)


# --------------------------------------------------------- decision sessions §4


def _session_env(denv) -> SimpleNamespace:
    _seed_unit(denv)
    request_id = _seed_decision(denv, body="Întrebare?\n")
    runner = FakeSessionRunner()
    manager = dash.DecisionSessionManager(denv.cfg, denv.db, runner)
    return SimpleNamespace(request_id=request_id, runner=runner, manager=manager)


async def test_session_transcript_appended_before_spawn_and_resume_continuity(
    denv,
) -> None:
    env = _session_env(denv)
    env.runner.transcript_probe = env.manager._transcript_path_for(
        dash._get_decision(denv.db.read(), env.request_id)
    )
    async with asyncio.TaskGroup() as tg:
        env.manager._set_taskgroup(tg)
        snap = await env.manager.post_message(env.request_id, "Care e riscul?")
        assert snap.busy is True
        assert [t.author for t in snap.turns] == ["founder"]
    # TaskGroup exit waited the turn out.
    snap = await env.manager.snapshot(env.request_id)
    assert [t.author for t in snap.turns] == ["founder", "agent"]
    assert snap.busy is False and snap.locked is None
    # Crash-durability: the founder message was IN the file before the spawn.
    assert "Care e riscul?" in env.runner.transcript_at_call[0]
    first_call = env.runner.calls[0]
    assert first_call.role == "decision_session"
    assert first_call.resume_session is None
    assert "Întrebare?" in first_call.prompt  # request artifact fed to the frame
    assert str(first_call.cwd).endswith(f".factory/sessions/{env.request_id}")
    assert first_call.timeout_s == denv.cfg.founder_channel.decision_session.turn_timeout_s

    async with asyncio.TaskGroup() as tg:
        env.manager._set_taskgroup(tg)
        await env.manager.post_message(env.request_id, "Și costul?")
    second_call = env.runner.calls[1]
    assert second_call.resume_session == "sess-1"  # claude --resume continuity
    assert second_call.prompt == "Și costul?"  # later turns: just the message


async def test_session_busy_locked_and_bounds_refuse_explicitly(denv) -> None:
    env = _session_env(denv)
    env.runner.gate = asyncio.Event()
    host = asyncio.create_task(_host_sessions(env.manager))
    await asyncio.sleep(0.01)
    try:
        await env.manager.post_message(env.request_id, "primul")
        with pytest.raises(dash.DashboardError, match="încă răspunde"):
            await env.manager.post_message(env.request_id, "al doilea")  # busy
        env.runner.gate.set()
        await _wait_idle(env.manager, env.request_id)

        # Budget bound: 1000-token budget, each turn burns 20 -> force over.
        env.runner.results = [_agent_result(tokens_in=600, tokens_out=600)]
        env.runner.gate = None
        await env.manager.post_message(env.request_id, "scump")
        await _wait_idle(env.manager, env.request_id)
        snap = await env.manager.snapshot(env.request_id)
        assert snap.locked == dash.RO["session_budget_exhausted"]
        with pytest.raises(dash.DashboardError):
            await env.manager.post_message(env.request_id, "după buget")
    finally:
        host.cancel()
        with pytest.raises(asyncio.CancelledError):
            await host


async def test_session_max_turns_locks(denv) -> None:
    env = _session_env(denv)
    host = asyncio.create_task(_host_sessions(env.manager))
    await asyncio.sleep(0.01)
    try:
        for index in range(denv.cfg.founder_channel.decision_session.max_turns):
            await env.manager.post_message(env.request_id, f"mesaj {index}")
            await _wait_idle(env.manager, env.request_id)
        snap = await env.manager.snapshot(env.request_id)
        assert snap.turns_left == 0
        assert snap.locked == dash.RO["session_turns_exhausted"]
        with pytest.raises(dash.DashboardError):
            await env.manager.post_message(env.request_id, "peste limită")
    finally:
        host.cancel()
        with pytest.raises(asyncio.CancelledError):
            await host


async def test_session_cancelled_turn_clears_busy_appends_notice_next_accepted(
    denv,
) -> None:
    """§4/§7: a serve() restart cancels in-flight turns — the try/finally
    teardown must clear busy and append the failed-turn notice, or the session
    wedges 'busy' forever."""
    env = _session_env(denv)
    env.runner.gate = asyncio.Event()  # never set: the turn hangs until cancelled
    host = asyncio.create_task(_host_sessions(env.manager))
    await asyncio.sleep(0.01)
    await env.manager.post_message(env.request_id, "în zbor")
    await asyncio.sleep(0.02)
    host.cancel()  # the serve()-restart equivalent
    with pytest.raises(asyncio.CancelledError):
        await host

    snap = await env.manager.snapshot(env.request_id)
    assert snap.busy is False  # never wedged
    assert snap.turns[-1].text == dash.RO["session_turn_failed"]
    assert dash.RO["session_turn_failed"] in env.manager.transcript_path(
        env.request_id
    ).read_text(encoding="utf-8")

    env.runner.gate = None
    host = asyncio.create_task(_host_sessions(env.manager))
    await asyncio.sleep(0.01)
    try:
        await env.manager.post_message(env.request_id, "după restart")  # accepted
        await _wait_idle(env.manager, env.request_id)
    finally:
        host.cancel()
        with pytest.raises(asyncio.CancelledError):
            await host


async def test_session_failed_turn_notice_not_retried(denv) -> None:
    env = _session_env(denv)
    env.runner.results = [RuntimeError("spawn exploded")]
    async with asyncio.TaskGroup() as tg:
        env.manager._set_taskgroup(tg)
        await env.manager.post_message(env.request_id, "salut")
    snap = await env.manager.snapshot(env.request_id)
    assert snap.busy is False
    assert snap.turns[-1].text == dash.RO["session_turn_failed"]
    assert len(env.runner.calls) == 1  # never auto-retried


async def test_session_restart_rebuilds_from_transcript(denv) -> None:
    env = _session_env(denv)
    async with asyncio.TaskGroup() as tg:
        env.manager._set_taskgroup(tg)
        await env.manager.post_message(env.request_id, "înainte de restart")
    # New manager = orchestrator restart (in-memory state lost, file survives).
    fresh_runner = FakeSessionRunner()
    fresh = dash.DecisionSessionManager(denv.cfg, denv.db, fresh_runner)
    snap = await fresh.snapshot(env.request_id)
    assert [t.author for t in snap.turns] == ["founder", "agent"]
    assert snap.turns[0].text == "înainte de restart"
    async with asyncio.TaskGroup() as tg:
        fresh._set_taskgroup(tg)
        await fresh.post_message(env.request_id, "după restart")
    call = fresh_runner.calls[0]
    assert call.resume_session is None  # CLI session lost with the process
    assert "înainte de restart" in call.prompt  # transcript fed back as context


async def test_session_refused_on_answered_request_and_pending_validation(denv) -> None:
    env = _session_env(denv)
    server = dash.DashboardServer(denv.cfg, denv.db, env.runner, FakeNotify())
    result = await server.answer(env.request_id, "approved", via="dashboard")
    assert result.outcome is dash.AnswerOutcome.ANSWERED
    async with asyncio.TaskGroup() as tg:
        env.manager._set_taskgroup(tg)
        with pytest.raises(dash.DashboardError, match="deja înregistrată"):
            await env.manager.post_message(env.request_id, "prea târziu")
        with pytest.raises(dash.DashboardError):
            await env.manager.post_message(424242, "nimeni")
        with pytest.raises(dash.DashboardError, match="gol"):
            await env.manager.post_message(env.request_id, "   ")


async def test_answer_commits_and_registers_transcript_with_answer(denv) -> None:
    """§3 step 2/3: an existing session transcript rides the SAME commit and the
    SAME tx as the answer (kind='transcript')."""
    env = _session_env(denv)
    async with asyncio.TaskGroup() as tg:
        env.manager._set_taskgroup(tg)
        await env.manager.post_message(env.request_id, "discuție înainte")
    server = dash.DashboardServer(denv.cfg, denv.db, env.runner, FakeNotify())
    server._sessions = env.manager  # the orchestrator-owned manager instance
    result = await server.answer(env.request_id, "approved", via="dashboard")
    assert result.outcome is dash.AnswerOutcome.ANSWERED
    conn = denv.db.read()
    transcript_ref = conn.execute(
        "SELECT * FROM artifact_refs WHERE kind='transcript'"
    ).fetchone()
    answer_ref = conn.execute(
        "SELECT * FROM artifact_refs WHERE kind='decision_answer'"
    ).fetchone()
    assert transcript_ref is not None
    assert transcript_ref["git_commit"] == answer_ref["git_commit"]  # same commit
    event = conn.execute(
        "SELECT payload_json FROM events WHERE event_type='decision_answered'"
    ).fetchone()
    assert json.loads(event["payload_json"])["transcript_artifact_id"] == transcript_ref["id"]


# ----------------------------------------------- answer-path quiesce (D-0019)


async def test_answer_during_busy_turn_registers_byte_stable_transcript(denv) -> None:
    """D-0019 pin (§3.1a): answering while an agent turn is composing cancels
    and AWAITS the turn, so the registered transcript ref resolves AT its
    recorded commit (sha256 == committed blob bytes), verify_integrity stays
    green for the non-terminal unit, and the cancelled-turn notice IS in the
    committed transcript. Pre-fix, a turn appending inside the commit window
    registered post-append bytes against the pre-append commit — a ref
    resolving nowhere, aborting the next orchestrator start."""
    env = _session_env(denv)
    # The seeded request artifact must ride a commit too: verify_integrity
    # checks EVERY latest ref of the non-terminal unit, not only the new ones.
    _git(denv.home, "add", "-A")
    _git(denv.home, "commit", "-q", "-m", "seed decision request")
    env.runner.gate = asyncio.Event()  # the turn composes until cancelled
    server = dash.DashboardServer(denv.cfg, denv.db, env.runner, FakeNotify())
    server._sessions = env.manager  # the orchestrator-owned manager instance
    real_commit_paths = dash.commit_paths

    async def racing_commit_paths(*args, **kwargs):
        # Inside the §3.2 commit window, give a surviving turn every chance to
        # append (the reproduced race: git pins pre-append bytes, register
        # hashes post-append). With the §3.1a quiesce the turn is already
        # terminated here: the gate release is a no-op and busy is False.
        sha = await real_commit_paths(*args, **kwargs)
        env.runner.gate.set()
        for _ in range(100):
            if not (await env.manager.snapshot(env.request_id)).busy:
                break
            await asyncio.sleep(0.01)
        return sha

    host = asyncio.create_task(_host_sessions(env.manager))
    await asyncio.sleep(0.01)
    try:
        await env.manager.post_message(env.request_id, "întrebare în zbor")
        await asyncio.sleep(0.02)  # the turn task is genuinely in flight
        assert (await env.manager.snapshot(env.request_id)).busy is True
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(dash, "commit_paths", racing_commit_paths)
            result = await server.answer(env.request_id, "approved", via="dashboard")
        assert result.outcome is dash.AnswerOutcome.ANSWERED
    finally:
        host.cancel()
        with pytest.raises(asyncio.CancelledError):
            await host

    conn = denv.db.read()
    tref = conn.execute("SELECT * FROM artifact_refs WHERE kind='transcript'").fetchone()
    assert tref is not None
    blob = subprocess.run(
        ["git", "cat-file", "blob", f"{tref['git_commit']}:{tref['path']}"],
        cwd=denv.home,
        check=True,
        capture_output=True,
    ).stdout
    assert hashlib.sha256(blob).hexdigest() == tref["sha256"]  # resolves AT its commit
    committed = blob.decode("utf-8")
    assert "întrebare în zbor" in committed
    assert dash.RO["session_turn_failed"] in committed  # cancelled-turn notice
    # The live file never diverged from its registered ref either (§3.1a
    # rationale for quiesce over register-by-committed-blob).
    assert (denv.home / tref["path"]).read_bytes() == blob
    report = verify_integrity(denv.db, {"factory": denv.home})
    assert report.ok and report.failures == ()


async def test_post_message_refused_while_answering_zero_writes(denv) -> None:
    """D-0019 pin (§4): a founder message landing inside answer()'s commit
    window -> explicit RO refusal, ZERO writes (no transcript append, no turn
    spawned, no DB rows) — and the refused text is NOT in the committed
    transcript."""
    env = _session_env(denv)
    server = dash.DashboardServer(denv.cfg, denv.db, env.runner, FakeNotify())
    server._sessions = env.manager
    host = asyncio.create_task(_host_sessions(env.manager))
    await asyncio.sleep(0.01)
    real_commit_paths = dash.commit_paths
    probe: dict[str, Any] = {}

    async def window_commit_paths(*args, **kwargs):
        transcript = env.manager.transcript_path(env.request_id)
        bytes_before = transcript.read_bytes()
        counts_before = _write_counts(denv)
        calls_before = len(env.runner.calls)
        with pytest.raises(dash.DashboardError) as excinfo:
            await env.manager.post_message(env.request_id, "mesaj în fereastră")
        probe["notice"] = str(excinfo.value)
        probe["bytes_unchanged"] = transcript.read_bytes() == bytes_before
        probe["db_unchanged"] = _write_counts(denv) == counts_before
        probe["no_turn_spawned"] = len(env.runner.calls) == calls_before
        return await real_commit_paths(*args, **kwargs)

    try:
        await env.manager.post_message(env.request_id, "discuție normală")
        await _wait_idle(env.manager, env.request_id)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(dash, "commit_paths", window_commit_paths)
            result = await server.answer(env.request_id, "approved", via="dashboard")
        assert result.outcome is dash.AnswerOutcome.ANSWERED
        assert probe["notice"] == dash.RO["session_answering_refuse"]
        assert probe["bytes_unchanged"] is True
        assert probe["db_unchanged"] is True
        assert probe["no_turn_spawned"] is True
    finally:
        host.cancel()
        with pytest.raises(asyncio.CancelledError):
            await host

    tref = (
        denv.db.read()
        .execute("SELECT * FROM artifact_refs WHERE kind='transcript'")
        .fetchone()
    )
    committed = _git(denv.home, "show", f"{tref['git_commit']}:{tref['path']}")
    assert "mesaj în fereastră" not in committed
    assert "discuție normală" in committed


async def test_answer_failure_clears_answering_flag_session_not_wedged(denv) -> None:
    """D-0019 pin: a FAILED answer (commit explodes mid-path) clears the
    answering flag on the failure exit too — the quiesced session is not
    wedged read-only: a later post_message is accepted and spawns a turn."""
    env = _session_env(denv)
    server = dash.DashboardServer(denv.cfg, denv.db, env.runner, FakeNotify())
    server._sessions = env.manager
    env.runner.gate = asyncio.Event()  # never set: the first turn hangs
    host = asyncio.create_task(_host_sessions(env.manager))
    await asyncio.sleep(0.01)

    async def git_down(*args, **kwargs):
        raise GitError("index locked (injected)")

    try:
        await env.manager.post_message(env.request_id, "primul mesaj")
        await asyncio.sleep(0.02)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(dash, "commit_paths", git_down)
            with pytest.raises(GitError):
                await server.answer(env.request_id, "approved", via="dashboard")
        # Quiesce ran (turn cancelled + notice), the flag is cleared, and the
        # request is still pending (commit failure = zero DB writes).
        snap = await env.manager.snapshot(env.request_id)
        assert snap.busy is False
        assert snap.turns[-1].text == dash.RO["session_turn_failed"]
        env.runner.gate = None
        snap = await env.manager.post_message(env.request_id, "după eșecul răspunsului")
        assert snap.busy is True  # accepted, not refused
        await _wait_idle(env.manager, env.request_id)
        assert len(env.runner.calls) == 2  # cancelled first + accepted second
    finally:
        host.cancel()
        with pytest.raises(asyncio.CancelledError):
            await host


async def _host_sessions(manager: dash.DecisionSessionManager) -> None:
    """serve()-shaped session host: the turn TaskGroup lives (and dies) here."""
    async with asyncio.TaskGroup() as tg:
        manager._set_taskgroup(tg)
        try:
            await asyncio.Event().wait()
        finally:
            manager._set_taskgroup(None)


async def _wait_idle(
    manager: dash.DecisionSessionManager, request_id: int, timeout: float = 5.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        snap = await manager.snapshot(request_id)
        if not snap.busy:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("session turn never finished")


# -------------------------------------------------------- tools-off chain (§4)


def test_decision_session_route_is_tools_off_claude_in_golden_config(
    real_config_path,
) -> None:
    """OPEN-D3/D-0017: the ratified route — and the closure of the §4 claim:
    session turns spawn role='decision_session' (pinned above), whose route is
    tools='none' on the claude CLI, whose argv carries the verified tools-off
    flagset (pinned here + in test_runner's adapter tests)."""
    golden = load_config(real_config_path)
    route = golden.models["decision_session"]
    assert route.cli == "claude" and route.model == "opus" and route.tools == "none"
    argv = ADAPTERS["claude"].build_cmd(route, "discuss")
    index = argv.index("--tools")
    assert argv[index + 1] == ""  # the FULL built-in set disabled


def test_session_page_render_includes_confirm_buttons_and_textcontent_only(
    denv,
) -> None:
    _seed_unit(denv)
    request_id = _seed_decision(denv)
    view = dash.build_view(denv.cfg)
    card = next(c for c in view.cards if c.request_id == request_id)
    snap = dash.SessionSnapshot(
        request_id=request_id,
        turns=(dash.Turn(n=1, author="founder", text="<b>bold</b>", at=utc_now()),),
        busy=False,
        locked=None,
        turns_left=3,
    )
    html_page = dash.render_session_page(snap, card, denv.cfg, "NONCE123")
    assert "&lt;b&gt;bold&lt;/b&gt;" in html_page  # founder echo escaped (R5)
    assert f"action='/decision/{request_id}/answer'" in html_page  # §3 confirm path
    assert "textContent" in html_page and "innerHTML" not in html_page
    assert dash.RO["session_confirm_label"] in html_page


# ===================================================================== §10 (v1.2)
# Founder-channel UX slice (D-0027): visual tokens, tables, the open-escalations
# block, options-above-body cards, ANSWERED confirmation, session-page textarea.
# Appended per the §10.8 lane; helpers stay local (frozen conftest).

import re as _re  # noqa: E402 — §10 append; the wave-3 import block stays frozen

from sf_factory.models import (  # noqa: E402 — §10 append (same convention)
    PHASE_ESCALATION_RESOLUTIONS,
    STAGE_ESCALATION_RESOLUTIONS,
    Trigger,
)


def _seed_escalation_row(
    env,
    *,
    unit_level: str = "stage",
    unit_id: str = "ph.s1",
    trigger: str = "max_fix_iterations",
    target: str = "phase_architect",
    payload_artifact_id: int | None = None,
    status: str = "open",
    resolution: str | None = None,
    resolved_at: str | None = None,
) -> int:
    from sf_factory.models import Escalation

    with env.db.transaction() as conn:
        return fdb.insert_escalation(
            conn,
            Escalation(
                id=None,
                unit_level=unit_level,
                unit_id=unit_id,
                trigger=trigger,
                target=target,
                payload_artifact_id=payload_artifact_id,
                event_seq=None,
                status=status,
                resolution=resolution,
                created_at=utc_now(),
                resolved_at=resolved_at,
            ),
        )


# --------------------------------------------------- §10.2 token discipline
# Contract (R-A13/R-B1), regex + exemption list STATED HERE next to the test:
#   - the :root block is the single token source and is excluded from the scan;
#   - @media condition literals are exempt (CSS forbids var() in conditions);
#   - bare `0` values are exempt (the px regex needs a digit run before 'px',
#     so unitless zeros never match);
#   - rgba() shadow values are exempt (declarations carrying rgba(...) are
#     skipped; rgba also carries no '#', so the hex regex never chases it).
_CSS_HEX_RE = _re.compile(r"#[0-9a-fA-F]{3,8}")
_CSS_PX_RE = _re.compile(r"\d+px")
_CSS_DECL_VALUE_RE = _re.compile(r":([^;{}]*)[;}]")

_REQUIRED_TOKENS = (
    "--space-1", "--space-2", "--space-3", "--space-4",
    "--c-bg", "--c-card", "--c-border", "--c-accent",
    "--c-ok", "--c-warn", "--c-err", "--c-muted",
    "--radius", "--fs-base", "--fs-small", "--fs-h1", "--fs-h2",
    "--border-w", "--tap-min",
)


def test_css_token_discipline_outside_root() -> None:
    css = dash._CSS
    root = _re.search(r":root\{[^}]*\}", css)
    assert root is not None, "the :root token block must exist (§10.2)"
    for token in _REQUIRED_TOKENS:
        assert f"{token}:" in root.group(0), f"token {token} missing from :root"
    rest = css.replace(root.group(0), "")
    rest = _re.sub(r"@media[^{]*\{", "@media{", rest)  # exemption: conditions
    for match in _CSS_DECL_VALUE_RE.finditer(rest):
        value = match.group(1)
        if "rgba(" in value:
            continue  # exemption: rgba() shadow values
        assert not _CSS_HEX_RE.search(value), f"hex literal outside :root: {value!r}"
        assert not _CSS_PX_RE.search(value), f"px literal outside :root: {value!r}"


def test_css_tap_target_overflow_and_input_font_rules_exist() -> None:
    assert "--tap-min:44px" in dash._CSS and "--fs-base:16px" in dash._CSS
    button_rule = _re.search(r"\.opt button\{[^}]*\}", dash._CSS)
    assert button_rule and "min-height:var(--tap-min)" in button_rule.group(0)
    assert "width:100%" in button_rule.group(0)  # S1 full-width thumb targets
    wrapper_rule = _re.search(r"\.tabel\{[^}]*\}", dash._CSS)
    assert wrapper_rule and "overflow-x:auto" in wrapper_rule.group(0)  # A-7
    assert _re.search(r"td\.num,th\.num\{[^}]*text-align:right", dash._CSS)
    # Internal-token small-print renders on its OWN line (§10.2/A-7/A-12).
    token_rule = _re.search(r"\.token\{[^}]*\}", dash._CSS)
    assert token_rule and "display:block" in token_rule.group(0)
    textarea_rule = _re.search(r"textarea\{[^}]*\}", dash._CSS)
    assert textarea_rule and "font-size:var(--fs-base)" in textarea_rule.group(0)
    pre_rule = _re.search(r"pre\{[^}]*\}", dash._CSS)
    assert pre_rule and "overflow-x:auto" in pre_rule.group(0)
    assert "white-space:pre-wrap" in pre_rule.group(0)


# ------------------------------------------------------- §10.2 state chips


def test_state_chip_closure_and_neutral_fallback() -> None:
    valid = {"accent", "warn", "err", "ok", "neutral"}
    for member in (*StageState, *PhaseState):
        assert member.value in dash.STATE_CHIPS, member
        assert dash.STATE_CHIPS[member.value] in valid, member
    # The §10.2 named categories, spot-pinned.
    assert dash.STATE_CHIPS["BUILD"] == "accent"
    assert dash.STATE_CHIPS["AWAITING_HUMAN"] == "warn"
    assert dash.STATE_CHIPS["ESCALATED"] == "err"
    assert dash.STATE_CHIPS["DONE"] == "ok"
    # Explicit neutral fallback (R-B6) — and the text gloss is always present.
    chip = dash._chip("NO_SUCH_STATE")
    assert "chip-neutral" in chip
    assert "NO_SUCH_STATE (etichetă lipsă)" in chip


# ------------------------------------- §10.4 R2 closure: triggers + targets


def test_gloss_closure_full_trigger_vocabulary_targets_and_resolutions() -> None:
    import inspect

    for member in Trigger:
        assert member.value in dash.GLOSS, member
    # DDL-comment extras (migrations/0001 escalations.trigger) + the
    # executor-owned usage_missing trigger (D-0014(1)).
    for extra in (
        "cp1_verdict",
        "unresolved_contest",
        "semantic_conflict",
        "internal_error",
        "usage_missing",
    ):
        assert extra in dash.GLOSS, extra
    # The scheduler literal set, harvested mechanically from the source —
    # self-updating: a new literal escalation insert without a gloss fails
    # here, the A-6 incident class.
    literals = set(
        _re.findall(r'trigger="([a-z_0-9]+)"', inspect.getsource(sched_mod))
    )
    assert {"child_failed", "integration_conflict"} <= literals
    for token in literals:
        assert token in dash.GLOSS, token
    assert dash.GLOSS["child_failed"] == "o etapă din fază a eșuat"
    assert dash.GLOSS["integration_conflict"] == "conflict la integrare"
    # The escalations target CHECK set (§2 DDL).
    for target in ("phase_architect", "main_architect", "founder"):
        assert target in dash.GLOSS, target
    # The resolution vocabulary renders on the „ultima escaladare rezolvată”
    # line — glossed end to end (R2).
    for token in (*STAGE_ESCALATION_RESOLUTIONS, *PHASE_ESCALATION_RESOLUTIONS):
        assert token in dash.GLOSS, token
    # escalation_resolved joined the incident vocabulary (§10.4).
    assert "escalation_resolved" in dash.INCIDENT_EVENT_TYPES
    assert "escalation_resolved" in dash.GLOSS


# ------------------------------------------------ §10.4 escalations block


def test_escalations_block_rows_glossed_anchors_first_in_strip(denv) -> None:
    from sf_factory.models import ArtifactRef

    _seed_unit(denv)
    decision_id = _seed_decision(denv)
    with denv.db.transaction() as conn:
        ref_id = fdb.insert_artifact_ref(
            conn,
            ArtifactRef(
                id=None,
                unit_level="stage",
                unit_id="ph.s1",
                kind="escalation_payload",
                repo="factory",
                path="_factory/stages/ph.s1/escalation-payload.md",
                sha256="3" * 64,
                git_commit=None,
                created_at=utc_now(),
            ),
        )
    arch_id = _seed_escalation_row(denv, payload_artifact_id=ref_id)
    founder_id = _seed_escalation_row(denv, trigger="weird_trigger", target="founder")
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)

    # Anchor + per-row ids (the notify deep links land HERE).
    assert "id='escaladari'" in page
    assert f"id='escalation/{arch_id}'" in page
    assert f"id='escalation/{founder_id}'" in page
    # FIRST in the strip when non-empty: exceptional state outranks telemetry.
    assert page.index("id='escaladari'") < page.index(dash.RO["pulse_label"])
    # Unit + trigger glossed (R2).
    assert "Schema de bază" in page
    assert "prea multe încercări de reparare fără progres (max_fix_iterations)" in page
    # Unknown trigger -> visible missing-gloss marker, the page never dies.
    assert "weird_trigger (etichetă lipsă)" in page
    # Architect target: glossed + the load-bearing reassurance line.
    assert "arhitectul de fază (phase_architect)" in page
    assert "nu cere acțiunea ta" in page
    # Founder target: action line + the decision-card link, NEVER reassurance.
    assert dash.RO["escalation_founder_action"] in page
    assert (
        f"<a href='#decision/{decision_id}'>{dash.RO['escalation_decision_link']}</a>"
        in page
    )
    # „dosar de escaladare” link when the payload ref exists (A-9).
    assert f"<a href='/artifact/{ref_id}'>{dash.RO['escalation_dossier']}</a>" in page


def test_escalations_empty_state_last_resolved_line_and_last_position(denv) -> None:
    _seed_unit(denv)
    _seed_escalation_row(
        denv,
        status="resolved",
        resolution="rework:BUILD",
        resolved_at="2026-06-12T08:00:00Z",
    )
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    assert "id='escaladari'" in page  # the anchor ALWAYS renders (A-4 landing)
    assert dash.RO["escalations_none"] in page
    # S2 resolution visibility: „nothing open” alone is indistinguishable from
    # „nothing was ever wrong”.
    assert dash.RO["escalation_last_resolved"] in page
    assert "refă construcția (rework:BUILD)" in page
    assert "12-06-2026" in page  # R4 founder format
    # Empty set does NOT outrank telemetry: the block renders last.
    assert page.index(dash.RO["pulse_label"]) < page.index("id='escaladari'")


# ---------------------------------------------------- §10.3 tables, §10.2 blocks


def test_tables_structure_chips_and_plan_groups(denv) -> None:
    _seed_unit(denv)
    now = utc_now()
    with denv.db.transaction() as conn:
        fdb.insert_stage(
            conn,
            Stage(
                id="ph.b",
                phase_id="ph",
                name="Etapa activă",
                risk_class="routine",
                state=StageState.BUILD,
                branch=None,
                worktree_path=None,
                spec_artifact_id=None,
                created_at=now,
                updated_at=now,
            ),
        )
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    assert "<div class='tabel'><table>" in page  # A-7 wrapper in the markup
    # Health-strip tables: Faze / Etape în lucru / Buget headers + numeric cells.
    assert dash.RO["col_phase"] in page and dash.RO["col_progress"] in page
    assert dash.RO["running_label"] in page and dash.RO["col_step"] in page
    assert dash.RO["col_effective"] in page and dash.RO["col_total_tok"] in page
    assert dash.RO["col_cap"] in page
    assert "class='num'" in page
    # Chips: color supplementary, gloss text always inside.
    assert _re.search(r"<span class='chip chip-accent'>[^<]*\(BUILD\)</span>", page)
    assert _re.search(r"<span class='chip chip-warn'>[^<]*\(AWAITING_HUMAN\)</span>", page)
    # Plan groups as table sections, not nested bullets. [AMENDED with §11
    # (CCR-10): the plan table gained the cost column -> colspan 3 became 4;
    # old-HTML-shape assertion, §10.8 carve-out — enumerated in the build report.]
    assert _re.search(r"<tr class='grup'><th colspan='4'>", page)
    assert dash.RO["plan_running_group"] in page


# ------------------------------------------- §10.1 S1: card order + confirmation


def test_card_order_options_precede_collapsed_request(denv) -> None:
    _seed_unit(denv)
    request_id = _seed_decision(denv)
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    card = _re.search(
        rf"<article class='card' id='decision/{request_id}'>.*?</article>",
        page,
        _re.DOTALL,
    )
    assert card is not None
    chunk = card.group(0)
    options_at = chunk.index(f"action='/decision/{request_id}/answer'")
    details_at = chunk.index("<details>")
    pre_at = chunk.index("<pre>")
    assert options_at < details_at < pre_at  # options markup precedes the <pre>
    assert f"<details><summary>{dash.RO['request_summary']}</summary>" in chunk
    # The session entry is a full-width LINK-BUTTON (S3/A-1) — no free-text
    # field anywhere on the auto-refreshing main page.
    assert f"<a class='btn' href='/decision/{request_id}/session'>" in chunk
    assert "<input type='text'" not in page
    assert "<textarea" not in page
    # Mechanical links render as the 2-col table (§10.3).
    assert f"<tr><th>{dash.RO['col_kind']}</th><th>{dash.RO['col_file']}</th></tr>" in chunk


def test_recommended_badge_inside_button_label_with_accent_style(denv) -> None:
    _seed_unit(denv)
    _seed_decision(denv)  # default body carries 'Recomandare: approved'
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    button = _re.search(r"<button class='recomandat'>.*?</button>", page, _re.DOTALL)
    assert button is not None
    chunk = button.group(0)
    # Badge INSIDE the button's label, token small-print on its own line (A-12).
    assert f"<span class='badge'>{dash.RO['recommended_badge']}</span>" in chunk
    assert "<span class='token'>(approved)</span>" in chunk
    assert page.count("class='badge'") == 1  # never a wrapping sibling elsewhere
    accent_rule = _re.search(r"\.opt button\.recomandat\{[^}]*\}", dash._CSS)
    assert accent_rule and "var(--c-accent)" in accent_rule.group(0)


async def test_answered_renders_confirmation_page_with_back_link(denv) -> None:
    """S1/A-3: ANSWERED -> an explicit RO confirmation page (the old 303 landed
    on an anchor that no longer exists, with zero acknowledgment)."""
    _seed_unit(denv)
    request_id = _seed_decision(denv)
    server = _server(denv)
    task = await _serving(server)
    try:
        host, port = server.bound_address
        status, _, body = await asyncio.to_thread(
            _http,
            "POST",
            f"http://{host}:{port}/decision/{request_id}/answer",
            b"option=approved",
        )
        assert status == 200
        assert dash.RO["answered_ok"] in body
        assert "<a href='/'>" in body  # the link back to the dashboard
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_every_ro_entry_is_referenced_dead_string_audit() -> None:
    """A-3's blind side closed: an RO literal nobody renders is dead UX copy —
    every key must be referenced (quoted) in the module source."""
    source = Path(dash.__file__).read_text(encoding="utf-8")
    for key in dash.RO:
        assert f'"{key}"' in source or f"'{key}'" in source, f"dead RO string: {key}"


# --------------------------------------------------- §10.1 S6: top banner


def test_banner_links_decizii_only_when_cards_exist(denv) -> None:
    _seed_unit(denv)
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    assert "class='banner'" not in page  # no decisions -> no banner
    _seed_decision(denv)
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    assert (
        f"<a class='banner' href='#decizii'>{dash.RO['banner_decisions_one']}</a>"
        in page
    )
    _seed_decision(denv)  # a second pending card -> plural, count prefixed
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    assert (
        f"<a class='banner' href='#decizii'>2 {dash.RO['banner_decisions_many']}</a>"
        in page
    )
    assert "id='decizii'" in page  # the anchor the banner lands on


# ------------------------------------------- §10.5 session textarea (S3/A-1)


def test_textarea_only_on_session_page_and_never_with_meta_refresh(denv) -> None:
    _seed_unit(denv)
    request_id = _seed_decision(denv)
    view = dash.build_view(denv.cfg)
    main_page = dash.render_page(view, denv.cfg)
    assert "http-equiv='refresh'" in main_page  # the main page auto-refreshes
    assert "<textarea" not in main_page  # so it may NEVER carry a textarea

    card = next(c for c in view.cards if c.request_id == request_id)
    snap = dash.SessionSnapshot(
        request_id=request_id, turns=(), busy=False, locked=None, turns_left=3
    )
    session_page = dash.render_session_page(snap, card, denv.cfg, "N0NCE")
    # BOTH ids pinned (R-B5): the poll script locates form + field by id; a
    # lost id leaves submit intercepted-then-dead, invisible to headless tests.
    assert "<form id='mesaj-form'" in session_page
    assert "<textarea id='mesaj-text' name='text' rows='4'" in session_page
    assert "http-equiv" not in session_page  # no meta-refresh with a textarea
    # JS-free path intact: plain form POST to the message endpoint.
    assert f"action='/decision/{request_id}/session/message'" in session_page

    # Locked session: the form disappears — still no meta-refresh anywhere.
    locked_snap = dash.SessionSnapshot(
        request_id=request_id, turns=(), busy=False, locked="blocat", turns_left=0
    )
    locked_page = dash.render_session_page(locked_snap, card, denv.cfg, "N0NCE")
    assert "<textarea" not in locked_page
    assert "http-equiv" not in locked_page


# ===================================================================== §11 (v1.3)
# Per-stage agent cost breakdown (CCR-10): _fmt_usd, the §11.1 precedence/marker
# honesty rule, exact/estimated pairs never merged, the «Astăzi» founder-TZ cut,
# the refresh-free read-only GET /costuri, and the role/model gloss closure.
# Appended per the §11.3 lane; helpers stay local (frozen conftest).

_TS = " "  # the _fmt_usd thin space (§11.2)


def _seed_ledger_row(
    env,
    *,
    unit_level: str = "stage",
    unit_id: str = "ph.s1",
    role: str = "builder_routine",
    model: str = "sonnet",
    tokens_in: int | None = 1000,
    tokens_out: int | None = 500,
    cost_usd: float | None = None,
    estimated: bool = False,
    recorded_at: str | None = None,
) -> None:
    """One token_ledger row (with its FK process row); recorded_at overridable —
    insert_token_usage stamps utc_now and the «Astăzi» cut needs fixed clocks."""
    from sf_factory.models import ProcessRecord

    with env.db.transaction() as conn:
        pid = fdb.insert_process(
            conn,
            ProcessRecord(
                id=None,
                unit_level=unit_level,
                unit_id=unit_id,
                kind="agent",
                role=role,
                cp_id=None,
                session_id=None,
                pid=None,
                cmdline="stub",
                cwd=None,
                state="exited",
                exit_code=0,
                ndjson_log_path=None,
                spawned_at=utc_now(),
                heartbeat_at=None,
                ended_at=utc_now(),
            ),
        )
        fdb.insert_token_usage(
            conn,
            process_id=pid,
            unit_level=unit_level,
            unit_id=unit_id,
            role=role,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            estimated=estimated,
        )
        if recorded_at is not None:
            conn.execute(
                "UPDATE token_ledger SET recorded_at = ?"
                " WHERE id = (SELECT MAX(id) FROM token_ledger)",
                (recorded_at,),
            )


def _seed_build_stage(env, stage_id: str = "ph.b", *, state=None) -> None:
    """One extra stage in phase 'ph' (default BUILD = RUNNING category)."""
    now = utc_now()
    with env.db.transaction() as conn:
        fdb.insert_stage(
            conn,
            Stage(
                id=stage_id,
                phase_id="ph",
                name="Etapa activă",
                risk_class="routine",
                state=state or StageState.BUILD,
                branch=None,
                worktree_path=None,
                spec_artifact_id=None,
                created_at=now,
                updated_at=now,
            ),
        )


def test_fmt_usd_romanian_comma_subcent_thin_space() -> None:
    assert dash._fmt_usd(12.4) == f"12,40{_TS}$"
    assert dash._fmt_usd(0.0) == f"0,00{_TS}$"
    assert dash._fmt_usd(0.01) == f"0,01{_TS}$"
    # Sub-cent non-zero is never rounded into '0,00 $' or '0,01 $' (§11.2).
    assert dash._fmt_usd(0.004) == f"<0,01{_TS}$"
    assert dash._fmt_usd(0.0099) == f"<0,01{_TS}$"
    # Thousands grouped Romanian-style (R4), decimal COMMA.
    assert dash._fmt_usd(1234.5) == f"1.234,50{_TS}$"


def test_founder_day_start_utc_golden_chisinau() -> None:
    # Summer (EEST, UTC+3) and winter (EET, UTC+2): the «Astăzi» cut is the
    # FOUNDER'S midnight converted to UTC, never the UTC day boundary (F5).
    assert (
        dash._founder_day_start_utc("2026-06-12T10:00:00Z", "Europe/Chisinau")
        == "2026-06-11T21:00:00Z"
    )
    assert (
        dash._founder_day_start_utc("2026-01-15T10:00:00Z", "Europe/Chisinau")
        == "2026-01-14T22:00:00Z"
    )


def test_row_cost_precedence_exact_estimate_marker_and_estimated_flag(denv) -> None:
    """§11.1: non-NULL cost_usd renders EXACT as-is (config prices NEVER applied
    over a reported cost); NULL estimates from pricing with `~`; NULL + missing
    pricing key -> the explicit marker, never a silent zero; estimated=1 token
    counts keep `~` regardless of cost source."""

    def row(**kw):
        base = dict(
            ledger_id=1,
            role="builder_routine",
            model="sonnet",
            tokens_in=1_000_000,
            tokens_out=100_000,
            cost_usd=None,
            estimated=False,
            recorded_at="2026-06-12T08:00:00Z",
        )
        base.update(kw)
        return dash.AgentCostRow(**base)

    # Exact passthrough: config arithmetic would say 3 + 1.5 = 4.50 — the
    # CLI-reported 1.20 wins (Doctrine §21, F4).
    assert dash._fmt_row_cost(denv.cfg, row(cost_usd=1.2)) == f"1,20{_TS}$"
    # NULL -> config-price estimate with `~`: 1M×3/1M + 100k×15/1M = 4.50.
    assert dash._fmt_row_cost(denv.cfg, row()) == f"~4,50{_TS}$"
    # NULL + missing pricing key -> explicit marker, never zero.
    assert dash._fmt_row_cost(denv.cfg, row(model="model-fara-pret")) == dash.RO[
        "missing_price"
    ]
    # estimated=1 keeps the `~` even over an exact cost.
    assert dash._fmt_row_cost(denv.cfg, row(cost_usd=1.2, estimated=True)) == f"~1,20{_TS}$"
    # NULL token counts estimate as zero flow, not a crash.
    assert (
        dash._fmt_row_cost(denv.cfg, row(tokens_in=None, tokens_out=None))
        == f"~0,00{_TS}$"
    )


def test_pair_never_merged_totals_summaries_factory_line_and_phase_rows(denv) -> None:
    """§11.2 (F8/F11/F3): every total renders «exact + ~estimat» as SEPARATE
    addends — running cell, plan stage cell (+ «detalii →» link), phase header
    (INCLUDING the phase's own unit_level='phase' rows) and the §2b factory
    line + «Astăzi» in the Buget block."""
    _seed_unit(denv)
    _seed_build_stage(denv)  # ph.b, BUILD
    # Stage ph.b: one exact row + one NULL-cost row estimated from config.
    _seed_ledger_row(denv, unit_id="ph.b", model="sonnet", cost_usd=1.2)
    _seed_ledger_row(
        denv,
        unit_id="ph.b",
        role="validator",
        model="fable",
        tokens_in=1_000_000,
        tokens_out=100_000,
        cost_usd=None,
    )  # 10 + 5 = ~15,00 $
    # The phase's OWN ledger row (the PLANNING agent, F3).
    _seed_ledger_row(
        denv, unit_level="phase", unit_id="ph", role="phase_architect",
        model="fable", cost_usd=0.5,
    )
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)

    stage_pair = f"1,20{_TS}$ + ~15,00{_TS}$"
    phase_pair = f"1,70{_TS}$ + ~15,00{_TS}$"  # 1,20 + 0,50 exact; phase rows IN
    # Running table: the right-aligned cost column carries the stage pair.
    assert f"<td class='num'>{stage_pair}</td>" in page
    # Plan stage row: pair + the «detalii →» link to the /costuri anchor.
    assert f"{stage_pair} <a href='/costuri#ph.b'>{dash.RO['cost_details']}</a>" in page
    # Phase header: the phase total INCLUDES the phase-level row (1,70 not 1,20).
    assert phase_pair in page
    # §2b factory line + «Astăzi» (rows recorded now -> today == lifetime).
    assert f"{dash.RO['budget_cost']}: {phase_pair}" in page
    assert f"{dash.RO['budget_today']}: {phase_pair}" in page
    # NEVER merged: no cell anywhere shows the blended sums.
    for merged in (f"16,70{_TS}$", f"16,20{_TS}$", f"4,70{_TS}$"):
        assert merged not in page

    # /costuri renders the same pairs: stage total row + phase header (F3).
    costs_page = dash.render_costs_page(dash.build_costs_view(denv.cfg), denv.cfg)
    assert stage_pair in costs_page
    assert phase_pair in costs_page
    assert f"16,70{_TS}$" not in costs_page and f"16,20{_TS}$" not in costs_page
    # The phase-agents table exists and carries the glossed planning role.
    assert dash.RO["costs_phase_agents"] in costs_page
    assert "arhitectul de fază (phase_architect)" in costs_page


def test_missing_price_marker_never_silent_zero_on_pages(denv) -> None:
    _seed_unit(denv)
    _seed_build_stage(denv)
    _seed_ledger_row(denv, unit_id="ph.b", model="model-fara-pret", cost_usd=None)
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    assert dash.RO["missing_price"] in page
    assert f"0,00{_TS}$" not in page  # the gap is named, never zeroed
    costs_page = dash.render_costs_page(dash.build_costs_view(denv.cfg), denv.cfg)
    assert dash.RO["missing_price"] in costs_page
    assert f"0,00{_TS}$" not in costs_page


def test_costuri_per_agent_rows_id_order_glossed_and_recorded_at(denv) -> None:
    """§11.2 (F7): one row per ledger entry, ordered by ledger id (a re-run role
    appears twice — the truth of what was spent); role/model glossed (R2),
    recorded_at displayed (R4), numeric columns right-aligned, total row last."""
    _seed_unit(denv)
    _seed_build_stage(denv)
    _seed_ledger_row(
        denv, unit_id="ph.b", role="builder_routine", model="sonnet",
        cost_usd=1.0, recorded_at="2026-06-12T08:00:00Z",
    )
    _seed_ledger_row(
        denv, unit_id="ph.b", role="validator", model="sonnet",
        cost_usd=2.0, recorded_at="2026-06-12T08:00:00Z",  # same second: id orders
    )
    _seed_ledger_row(
        denv, unit_id="ph.b", role="builder_routine", model="sonnet",
        cost_usd=3.0, recorded_at="2026-06-12T08:00:00Z",
    )
    costs_page = dash.render_costs_page(dash.build_costs_view(denv.cfg), denv.cfg)
    # Anchor = the «detalii →» landing.
    assert "id='ph.b'" in costs_page
    # Header row: glossed columns, numerics right-aligned. Pornit + Durată
    # (founder per-agent timing, 20-06) sit between Agent and Model.
    assert (
        f"<th>{dash.RO['col_agent']}</th>"
        f"<th class='num'>{dash.RO['col_started']}</th>"
        f"<th class='num'>{dash.RO['col_duration']}</th>"
        f"<th>{dash.RO['col_model']}</th>"
        f"<th class='num'>{dash.RO['col_tokens_in']}</th>"
        f"<th class='num'>{dash.RO['col_tokens_out']}</th>"
        f"<th class='num'>{dash.RO['col_cost']}</th>" in costs_page
    )
    # One row per ledger entry in id order: 1,00 / 2,00 / 3,00.
    first = costs_page.index(f"1,00{_TS}$")
    second = costs_page.index(f"2,00{_TS}$")
    third = costs_page.index(f"3,00{_TS}$")
    assert first < second < third
    # The re-run role renders twice; both roles and the model are glossed.
    assert costs_page.count("constructor (etape ușoare) (builder_routine)") == 2
    assert "validator (validator)" in costs_page
    assert "Claude Sonnet (sonnet)" in costs_page
    # recorded_at displayed in founder format (R4: 08:00Z -> 11:00 Chisinau EEST).
    assert "12-06-2026 11:00" in costs_page
    # Total row last, as the pair (all-exact here -> single exact addend).
    total_at = costs_page.index(dash.RO["cost_total_row"])
    assert total_at > third
    assert f"6,00{_TS}$" in costs_page


def test_astazi_line_uses_founder_tz_midnight_cut(denv) -> None:
    """§11.2 (F5): «Astăzi» sums rows with recorded_at >= founder-TZ midnight
    converted to UTC — a 23:59-local-yesterday row is OUT, 00:30-local is IN."""
    _seed_unit(denv)
    _seed_build_stage(denv)
    now = "2026-06-12T10:00:00Z"  # 13:00 Chisinau (EEST) -> cut 2026-06-11T21:00:00Z
    _seed_ledger_row(
        denv, unit_id="ph.b", cost_usd=5.0, recorded_at="2026-06-11T20:59:00Z"
    )  # yesterday, founder time
    _seed_ledger_row(
        denv, unit_id="ph.b", cost_usd=0.25, recorded_at="2026-06-11T21:30:00Z"
    )  # 00:30 founder time — today
    _seed_ledger_row(
        denv, unit_id="ph.b", cost_usd=0.5, recorded_at="2026-06-12T09:00:00Z"
    )
    view = dash.build_view(denv.cfg, now=now)
    assert view.health.today_cost == dash.CostSummary(exact_usd=0.75)
    assert view.health.factory_cost.exact_usd == pytest.approx(5.75)
    page = dash.render_page(view, denv.cfg)
    assert f"{dash.RO['budget_today']}: 0,75{_TS}$" in page
    assert f"{dash.RO['budget_cost']}: 5,75{_TS}$" in page

    # Nothing recorded today -> an explicit exact zero, not a missing line
    # (zero rows since the cut IS exactly zero recorded spend).
    with denv.db.transaction() as conn:
        conn.execute(
            "DELETE FROM token_ledger WHERE recorded_at >= ?", ("2026-06-11T21:00:00Z",)
        )
    view = dash.build_view(denv.cfg, now=now)
    assert view.health.today_cost.empty
    page = dash.render_page(view, denv.cfg)
    assert f"{dash.RO['budget_today']}: 0,00{_TS}$" in page


async def test_costuri_route_refresh_free_read_only_no_inputs(denv) -> None:
    """§11.2 (F2): /costuri is the stateful reading surface — NO meta-refresh
    (pinned like the session page), NO inputs of any kind, read-only over HTTP
    with the base CSP (zero JS)."""
    _seed_unit(denv)
    _seed_build_stage(denv)
    _seed_ledger_row(denv, unit_id="ph.b", cost_usd=1.2)
    server = _server(denv)
    task = await _serving(server)
    try:
        host, port = server.bound_address
        status, headers, body = await asyncio.to_thread(
            _http, "GET", f"http://{host}:{port}/costuri"
        )
        assert status == 200
        csp = headers["Content-Security-Policy"]
        for directive in (
            "default-src 'none'",
            "style-src 'unsafe-inline'",
            "form-action 'self'",
            "base-uri 'none'",
            "frame-ancestors 'none'",
        ):
            assert directive in csp
        assert "script-src" not in csp  # zero JS, like the main page
        assert "http-equiv" not in body  # NO meta-refresh — the F2 pin
        for forbidden in ("<form", "<input", "<textarea", "<button", "<script"):
            assert forbidden not in body
        assert "id='ph.b'" in body  # the «detalii →» anchor target
        assert f"<a href='/'>{dash.RO['back_to_dashboard']}</a>" in body
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_pending_stage_no_cost_row_no_link_no_anchor(denv) -> None:
    _seed_unit(denv)
    _seed_build_stage(denv)  # has ledger rows
    _seed_build_stage(denv, "ph.todo", state=StageState.PENDING)  # no rows
    _seed_ledger_row(denv, unit_id="ph.b", cost_usd=1.2)
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    assert "/costuri#ph.b" in page
    assert "/costuri#ph.todo" not in page  # §11.4: PENDING -> no cost row/link
    costs_page = dash.render_costs_page(dash.build_costs_view(denv.cfg), denv.cfg)
    assert "id='ph.b'" in costs_page
    assert "id='ph.todo'" not in costs_page


def test_cost_legend_renders_iff_any_cost_cell_does(denv) -> None:
    _seed_unit(denv)
    # No ledger rows anywhere: no cost cells -> no legend, no «Astăzi», no
    # factory cost part; /costuri shows the explicit empty notice instead.
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    assert dash.RO["cost_legend"] not in page
    assert dash.RO["budget_today"] not in page
    assert f"{dash.RO['budget_cost']}:" not in page
    costs_page = dash.render_costs_page(dash.build_costs_view(denv.cfg), denv.cfg)
    assert dash.RO["cost_legend"] not in costs_page
    assert dash.RO["costs_none"] in costs_page
    # One ledger row -> cost cells exist -> the legend renders on BOTH pages.
    _seed_ledger_row(denv, unit_id="ph.s1", cost_usd=0.1)
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    assert dash.RO["cost_legend"] in page
    costs_page = dash.render_costs_page(dash.build_costs_view(denv.cfg), denv.cfg)
    assert dash.RO["cost_legend"] in costs_page
    assert dash.RO["costs_none"] not in costs_page


def test_gloss_closure_role_keys_and_model_tokens(real_config_path) -> None:
    """§11.4 (F6) closure extension: role keys = the golden config's models.*
    keys; model tokens = the routes' model strings + the pricing table keys."""
    golden = load_config(real_config_path)
    for role in golden.models:
        assert role in dash.GLOSS, role
    model_tokens = {route.model for route in golden.models.values()} | set(
        golden.pricing.usd_per_mtok
    )
    for token in model_tokens:
        assert token in dash.GLOSS, token
    assert dash.GLOSS["default"] == "codex — model implicit"
    # The §11 forward gloss for the follow-up slice's escalation trigger — added
    # now so the trigger never renders bare when that slice lands.
    assert dash.GLOSS["agent_run_failed"] == (
        "agentul a eșuat la rulare (oprire fără rezultat)"
    )


# --------------------------------------------- per-stage „Detalii” page (20-06)
# A focused read-only detail page for ONE running stage: state history, one
# result row per agent run (the running agent visually distinct), and audit
# findings with the report/contest content rendered inline. Local seed helpers
# (the frozen-conftest convention); reuse _seed_unit/_seed_build_stage above.


def _seed_proc(
    env,
    *,
    unit_id: str = "ph.s1",
    role: str = "builder_routine",
    state: str = "exited",
    exit_code: int | None = 0,
    spawned_at: str | None = None,
    ended_at: str | None = None,
    tokens: tuple[int, int] | None = None,
) -> int:
    """One process_registry kind='agent' run for a stage (+ an optional ledger
    row so the run's token sum is non-empty). Returns the process id."""
    from sf_factory.models import ProcessRecord

    now = utc_now()
    with env.db.transaction() as conn:
        pid = fdb.insert_process(
            conn,
            ProcessRecord(
                id=None,
                unit_level="stage",
                unit_id=unit_id,
                kind="agent",
                role=role,
                cp_id=None,
                session_id=None,
                pid=None,
                cmdline="stub",
                cwd=None,
                state=state,
                exit_code=exit_code,
                ndjson_log_path=None,
                spawned_at=spawned_at or now,
                heartbeat_at=None,
                ended_at=ended_at,
            ),
        )
        if tokens is not None:
            fdb.insert_token_usage(
                conn,
                process_id=pid,
                unit_level="stage",
                unit_id=unit_id,
                role=role,
                model="sonnet",
                tokens_in=tokens[0],
                tokens_out=tokens[1],
                cost_usd=None,
            )
        return pid


def _seed_transition(env, *, unit_id: str, from_state: str, to_state: str) -> None:
    with env.db.transaction() as conn:
        fdb.insert_event(
            conn,
            unit_level="stage",
            unit_id=unit_id,
            event_type="transition",
            actor="state_machine",
            from_state=from_state,
            to_state=to_state,
        )


def _seed_finding(
    env,
    *,
    unit_id: str = "ph.s1",
    finding_ref: str = "F-1",
    severity: str | None = "major",
    auditor_role: str = "auditor_cross_model",
    status: str = "open",
    report_body: str = "# Raport de audit\n\nProblema constatată.\n",
    contest_body: str | None = None,
) -> None:
    """One audit_findings row whose report (and optional contest) artifact is a
    real factory-repo file resolvable by the /artifact content helper."""
    from sf_factory.artifacts import register_artifact
    from sf_factory.models import Finding

    unit_dir = env.home / "_factory" / "stages" / unit_id / "audit"
    unit_dir.mkdir(parents=True, exist_ok=True)
    report_path = unit_dir / f"{finding_ref}-report.md"
    report_path.write_text(report_body, encoding="utf-8")
    now = utc_now()
    with env.db.transaction() as conn:
        report_ref = register_artifact(
            conn,
            unit_level="stage",
            unit_id=unit_id,
            kind="audit_report",
            repo="factory",
            repo_root=env.home,
            path=report_path,
            git_commit=None,
        )
        contest_id = None
        if contest_body is not None:
            contest_path = unit_dir / f"{finding_ref}-contest.md"
            contest_path.write_text(contest_body, encoding="utf-8")
            contest_ref = register_artifact(
                conn,
                unit_level="stage",
                unit_id=unit_id,
                kind="contest_rationale",
                repo="factory",
                repo_root=env.home,
                path=contest_path,
                git_commit=None,
            )
            contest_id = contest_ref.id
        fdb.insert_finding(
            conn,
            Finding(
                id=None,
                stage_id=unit_id,
                auditor_role=auditor_role,
                finding_ref=finding_ref,
                severity=severity,
                report_artifact_id=report_ref.id,
                status=status,
                contest_artifact_id=contest_id,
                resolved_by=None,
                created_at=now,
                updated_at=now,
            ),
        )


def test_stage_detail_renders_four_sections(denv) -> None:
    """build_stage_detail + render_stage_page show, as _bloc sections: the
    header (name+id+state chip+risk gloss), Istoric (transitions oldest→newest),
    Agenți și rezultate (one result row per run), Constatări audit (+ inline
    report content). No meta-refresh (it has no inputs)."""
    _seed_unit(denv)  # phase ph (+ stage ph.s1)
    _seed_build_stage(denv)  # ph.b, BUILD (RUNNING category)
    _seed_transition(denv, unit_id="ph.b", from_state="SPEC", to_state="BUILD")
    _seed_proc(
        denv,
        unit_id="ph.b",
        role="builder_routine",
        state="exited",
        exit_code=0,
        spawned_at="2026-06-20T10:00:00Z",
        ended_at="2026-06-20T10:18:00Z",
        tokens=(120000, 22000),
    )
    _seed_finding(
        denv,
        unit_id="ph.b",
        finding_ref="F-7",
        severity="major",
        status="open",
        report_body="Constatare detaliată în raport.\n",
    )
    detail = dash.build_stage_detail(denv.cfg, "ph.b")
    assert detail is not None
    page = dash.render_stage_page(detail, denv.cfg)

    # No meta-refresh on a no-input page (the costs/session precedent).
    assert "http-equiv='refresh'" not in page
    # Header: name + id + state chip + glossed risk class.
    assert "Etapa activă (ph.b)" in page
    assert "construcție în lucru (BUILD)" in page  # state chip gloss (StageState.BUILD)
    assert "risc de rutină (routine)" in page
    # Istoric: the transition row with both chips + founder timestamp.
    assert dash.RO["detail_history"] in page
    assert "specificare în lucru (SPEC)" in page
    # Agenți și rezultate: glossed role, founder start, duration, outcome, tokens.
    assert dash.RO["detail_agents"] in page
    assert "constructor (etape ușoare) (builder_routine)" in page
    assert "20-06-2026 13:00" in page  # spawned_at 10:00Z -> 13:00 Chisinau (EEST)
    assert "18 min" in page  # _fmt_dur 10:00->10:18
    assert dash.RO["outcome_success"] in page  # exited + exit_code 0 -> reușit
    assert "142" in page  # _fmt_ktok(120000+22000) -> "142"
    # Constatări audit: ref, severity, glossed auditor, status gloss, inline report.
    assert dash.RO["detail_findings"] in page
    assert "F-7" in page
    assert "auditor încrucișat (codex) (auditor_cross_model)" in page
    assert dash.RO["finding_status_open"] in page
    assert "Constatare detaliată în raport." in page  # report content inline


def test_stage_detail_outcomes_and_running_agent_distinct(denv) -> None:
    """OUTCOME mapping: running/spawned -> „în lucru” (current agent, visually
    distinct chip); exited+0 -> „reușit”; otherwise „eșuat (<state>[ cod N])”.
    A run with no ledger rows shows „—” for tokens."""
    _seed_unit(denv)
    _seed_build_stage(denv)
    # A failed run (non-zero exit), an old success, and the CURRENT running agent.
    _seed_proc(denv, unit_id="ph.b", role="validator", state="exited", exit_code=2,
               spawned_at="2026-06-20T09:00:00Z", ended_at="2026-06-20T09:05:00Z")
    _seed_proc(denv, unit_id="ph.b", role="builder_routine", state="running",
               exit_code=None, spawned_at="2026-06-20T10:00:00Z", ended_at=None)
    detail = dash.build_stage_detail(denv.cfg, "ph.b")
    assert detail is not None
    page = dash.render_stage_page(detail, denv.cfg)
    # Failed run names the state + code; never a silent success.
    assert f"{dash.RO['outcome_failure']} (exited cod 2)" in page
    # The running agent: „în lucru” inside the accent chip (visually distinct).
    assert f"<span class='chip chip-accent'>{dash.RO['outcome_running']}</span>" in page
    # Neither run has ledger rows -> tokens render the em dash, not "0".
    assert "<td class='num'>—</td>" in page
    # Order is oldest→newest: the 09:00 failed run precedes the 10:00 running one.
    assert page.index("validator") < page.index("builder_routine")


def test_stage_detail_audit_content_truncated_with_link(denv) -> None:
    """Audit report/contest content renders INLINE in an escaped <pre>; content
    over the cap is truncated with the „… (trunchiat)” marker; the full-artifact
    link to /artifact/<id> is always present. The contest artifact renders too."""
    _seed_unit(denv)
    _seed_build_stage(denv)
    long_body = "X" * (dash._ARTIFACT_INLINE_CAP + 500)
    _seed_finding(
        denv,
        unit_id="ph.b",
        finding_ref="F-9",
        status="contested",
        report_body=long_body,
        contest_body="Motivația executorului.\n",
    )
    detail = dash.build_stage_detail(denv.cfg, "ph.b")
    assert detail is not None
    page = dash.render_stage_page(detail, denv.cfg)
    # Truncation: capped content + the marker; the over-cap tail is NOT present.
    assert "X" * dash._ARTIFACT_INLINE_CAP in page
    assert "X" * (dash._ARTIFACT_INLINE_CAP + 1) not in page
    assert dash.RO["artifact_truncated"] in page
    # The full-artifact link is rendered (resolve the report ref id from the DB).
    report_id = (
        denv.db.read()
        .execute("SELECT report_artifact_id FROM audit_findings WHERE finding_ref='F-9'")
        .fetchone()[0]
    )
    assert f"/artifact/{report_id}" in page
    # The contest content + its label render inline.
    assert dash.RO["detail_contest"] in page
    assert "Motivația executorului." in page
    # Contested status maps to the founder-clear „contestat”.
    assert dash.RO["finding_status_contested"] in page


def test_stage_detail_empty_sections_show_notices(denv) -> None:
    """A stage with no transitions / runs / findings still renders all four
    blocs — each empty section shows its explicit RO notice, never a blank."""
    _seed_unit(denv)
    _seed_build_stage(denv)
    detail = dash.build_stage_detail(denv.cfg, "ph.b")
    assert detail is not None
    page = dash.render_stage_page(detail, denv.cfg)
    assert dash.RO["detail_history_none"] in page
    assert dash.RO["detail_agents_none"] in page
    assert dash.RO["detail_findings_none"] in page


def test_running_stage_row_links_to_detail_page(denv) -> None:
    """The „Acum în lucru” running-stages table carries a „Detalii →” link to
    /stage/<stage_id> per running row (founder asked for it on running only)."""
    _seed_unit(denv)
    _seed_build_stage(denv)  # ph.b is RUNNING category
    page = dash.render_page(dash.build_view(denv.cfg), denv.cfg)
    assert f"<a href='/stage/ph.b'>{dash.RO['stage_detail_link']}</a>" in page


async def test_stage_route_serves_page_and_unknown_404s(denv) -> None:
    """GET /stage/<id> returns the page for a seeded stage; an unknown id 404s
    via the existing _page error path. The regex admits dotted/hyphened ids."""
    _seed_unit(denv)
    _seed_build_stage(denv, "inventory-procurement.stocktaking")
    _seed_transition(
        denv,
        unit_id="inventory-procurement.stocktaking",
        from_state="SPEC",
        to_state="BUILD",
    )
    server = _server(denv)
    task = await _serving(server)
    try:
        host, port = server.bound_address
        base = f"http://{host}:{port}"
        # Dotted + hyphened stage id resolves (the [\w.\-]+ route regex).
        status, _, body = await asyncio.to_thread(
            _http, "GET", f"{base}/stage/inventory-procurement.stocktaking"
        )
        assert status == 200
        assert "(inventory-procurement.stocktaking)" in body
        assert dash.RO["detail_history"] in body
        # Unknown stage id -> 404 with the RO notice (existing _page path).
        status, _, body = await asyncio.to_thread(
            _http, "GET", f"{base}/stage/nu.exista"
        )
        assert status == 404
        assert dash.RO["stage_unknown"] in body
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ----------------------------------------------- ⚙ Configurare (items 4+5)

_CFG_AT = "2026-06-20T12:00:00Z"


def _full_form(cfg, eff, **changes) -> dict[str, str]:
    """A COMPLETE Configurare form at the current effective values (exactly what the
    rendered page submits); **changes overrides individual fields the founder edits."""
    form = {
        "drain_manual": "drenaj" if eff.drain_manual else "normal",
        "autodrenaj_submitted": "1",
        "max_parallel": str(eff.max_parallel_agents),
        "agent_timeout": str(eff.agent_timeout_s),
        "gov_5h": str(eff.gov_five_hour_pct),
        "gov_7d": str(eff.gov_seven_day_pct),
    }
    if eff.autodrenaj:
        form["autodrenaj"] = "on"
    for rc in cfg.budgets.per_stage:
        form[f"budget_{rc}"] = str(eff.budget(rc))
    form.update(changes)
    return form


def test_configure_view_baseline_no_overrides(denv) -> None:
    """GET /configurare with no overrides shows the cfg baselines, nothing marked
    «modificat», and renders every editable field."""
    view = dash.build_configure_view(denv.cfg)
    assert view.drain_manual.value is False and view.drain_manual.overridden is False
    assert view.max_parallel.value == denv.cfg.process.max_parallel_agents
    assert view.agent_timeout_s.value == denv.cfg.process.agent_timeout_s
    assert view.autodrenaj.value == denv.cfg.capacity_governor.proactive_enabled
    html = dash.render_configure_page(view, denv.cfg)
    assert dash.RO["cfg_heading"] in html
    for name in ("drain_manual", "autodrenaj", "max_parallel", "agent_timeout", "gov_5h", "gov_7d"):
        assert f"name='{name}'" in html
    for rc in denv.cfg.budgets.per_stage:
        assert f"name='budget_{rc}'" in html
    assert dash.RO["cfg_overridden"] not in html  # nothing overridden yet


def test_configure_view_reflects_overrides(denv) -> None:
    """An override wins over cfg and is marked «modificat la viu»; the DRENAJ radio
    renders checked."""
    with denv.db.transaction() as conn:
        fdb.set_runtime_setting(conn, rs.KEY_MAX_PARALLEL, 1, updated_by="founder", at=_CFG_AT)
        fdb.set_runtime_setting(conn, rs.KEY_DRAIN_MANUAL, True, updated_by="founder", at=_CFG_AT)
    view = dash.build_configure_view(denv.cfg)
    assert view.max_parallel.value == 1 and view.max_parallel.overridden is True
    assert view.drain_manual.value is True
    assert view.last_change == (_CFG_AT, "founder")
    html = dash.render_configure_page(view, denv.cfg)
    assert dash.RO["cfg_overridden"] in html
    assert "value='drenaj' checked" in html


async def test_update_settings_writes_changed_only(denv) -> None:
    """A POST writes ONLY the changed keys (one runtime_setting_changed event each);
    fields left at their current value produce no write/event."""
    server = _server(denv)
    eff = rs.EffectiveConfig(fdb.get_runtime_settings(denv.db.read()), denv.cfg)
    form = _full_form(denv.cfg, eff)
    form["drain_manual"] = "drenaj"  # the only change
    result = await server.update_settings(form, via="founder")
    assert result.errors == () and result.changed == 1
    assert fdb.get_runtime_settings(denv.db.read()) == {rs.KEY_DRAIN_MANUAL: True}
    events = denv.db.read().execute(
        "SELECT COUNT(*) FROM events WHERE event_type='runtime_setting_changed'"
    ).fetchone()[0]
    assert events == 1


async def test_update_settings_max_parallel_guard_rejects_below_running(denv) -> None:
    """The max-parallel guard rejects a value below the live running-agent count;
    NOTHING is written (all-or-nothing)."""
    _seed_unit(denv, stage_id="ph.s1", risk="routine")
    for _ in range(3):
        _seed_proc(denv, unit_id="ph.s1", state="running", exit_code=None)
    server = _server(denv)
    form = _full_form(denv.cfg, rs.EffectiveConfig({}, denv.cfg), max_parallel="2")
    result = await server.update_settings(form, via="founder")
    assert result.changed == 0
    assert any("rulează 3" in e for e in result.errors)
    assert fdb.get_runtime_settings(denv.db.read()) == {}


async def test_update_settings_budget_guard_rejects_below_consumed(denv) -> None:
    """The budget guard rejects a per-class budget below what a RUNNING stage of
    that class has already consumed (else it would escalate at once)."""
    _seed_unit(denv, stage_id="ph.s1", risk="routine")  # AWAITING_HUMAN: non-terminal
    _seed_proc(denv, unit_id="ph.s1", state="exited", exit_code=0, tokens=(6000, 0))
    server = _server(denv)
    form = _full_form(denv.cfg, rs.EffectiveConfig({}, denv.cfg))
    form["budget_routine"] = "5000"  # below the 6000 already consumed
    result = await server.update_settings(form, via="founder")
    assert result.changed == 0
    assert any("consumat deja 6000" in e for e in result.errors)
    assert rs.budget_key("routine") not in fdb.get_runtime_settings(denv.db.read())


async def test_update_settings_all_or_nothing(denv) -> None:
    """A valid edit alongside an invalid one writes NOTHING — the valid drain flip
    must not land while the bad timeout is rejected."""
    server = _server(denv)
    form = _full_form(denv.cfg, rs.EffectiveConfig({}, denv.cfg))
    form["drain_manual"] = "drenaj"  # valid
    form["agent_timeout"] = "-5"  # invalid (not positive)
    result = await server.update_settings(form, via="founder")
    assert result.changed == 0 and result.errors
    assert fdb.get_runtime_settings(denv.db.read()) == {}


async def test_update_settings_pct_out_of_range_rejected(denv) -> None:
    """A threshold outside (0, 100] is rejected; nothing written."""
    server = _server(denv)
    form = _full_form(denv.cfg, rs.EffectiveConfig({}, denv.cfg))
    form["gov_5h"] = "150"
    result = await server.update_settings(form, via="founder")
    assert result.changed == 0 and result.errors
    assert fdb.get_runtime_settings(denv.db.read()) == {}


async def test_update_settings_budget_edit_above_consumed_succeeds(denv) -> None:
    """Raising a budget above the running stage's consumption is allowed and lands
    as a budget.<rc> override with its audit event."""
    _seed_unit(denv, stage_id="ph.s1", risk="routine")
    _seed_proc(denv, unit_id="ph.s1", state="exited", exit_code=0, tokens=(6000, 0))
    server = _server(denv)
    form = _full_form(denv.cfg, rs.EffectiveConfig({}, denv.cfg))
    form["budget_routine"] = "9000"  # above 6000 consumed, below the 10000 baseline
    result = await server.update_settings(form, via="founder")
    assert result.errors == () and result.changed == 1
    assert fdb.get_runtime_settings(denv.db.read()) == {rs.budget_key("routine"): 9000}
