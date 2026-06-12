"""Founder dashboard — the orchestrator's founder surface (dashboard design
v1.1 D-0017; §10 founder-channel UX slice v1.2 D-0027: :root token visual
system, tables over bullets, the open-escalations block, options-above-body
cards, the ANSWERED confirmation page, session-page-only textarea; §11
per-stage agent cost breakdown v1.3 CCR-10: cost pairs on the main page, the
«Astăzi» line, and the refresh-free read-only ``GET /costuri`` per-agent
tables — exact-where-reported, ``~``-estimate-where-not, never merged).

In-process module owned by the ``Scheduler`` (design §1): stdlib
``ThreadingHTTPServer`` worker threads supervised by an asyncio task. GET
handlers NEVER touch the orchestrator's rw ``Database`` — each opens its own
short-lived ``mode=ro`` connection (control-plane §2 sanctioned read). Exactly
one decision/state write path exists: ``POST /decision/<id>/answer`` →
``DashboardServer.answer`` marshalled onto the orchestrator loop (§3, D-0015
order). Decision Sessions (§4) converse tools-off and never write state; only
the founder's explicit option tap does.

Founder-protocol conformance (§5) is enforced structurally: every
founder-visible literal lives in ``RO`` (R1); every internal token renders
through ``GLOSS`` (R2); recommendations are parsed, never invented (R3); dates
via ``fmt_founder_ts`` (R4); ALL artifact/agent/founder text passes ``esc()``
into ``<pre>`` — markdown is never interpreted (R5); cards always present
prepared options (R6).

May import: models, config, db, artifacts, worktrees, runner, notify
(+ stdlib). The scheduler imports this module; this module never imports the
scheduler (no cycle, design §6).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import html
import http.server
import json
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from collections.abc import Coroutine, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from sf_factory import db as fdb
from sf_factory.artifacts import register_artifact, unit_artifact_dir
from sf_factory.config import FactoryConfig
from sf_factory.db import Database
from sf_factory.models import (
    GATE_ANSWERS,
    DecisionRequest,
    FactoryError,
    GitError,
    Level,
    SchedCategory,
    sched_category,
    utc_now,
)
from sf_factory.notify import NtfyPublisher
from sf_factory.runner import AgentRunner
from sf_factory.worktrees import commit_paths, run_git

# ----------------------------------------------------------------- vocabulary

#: Runner role of Decision Sessions — config models.* key referenced by name
#: (the scheduler CP1_ID pattern); routed tools-off (§4, D-0017).
_SESSION_ROLE = "decision_session"

#: event_type values surfaced as „Ultimul incident” (§2b — frozen constant;
#: ``escalation_resolved`` joined with §10.4/D-0027: a resolved escalation is
#: cold-return-visible news, not silence).
INCIDENT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "alert",
        "declared_failure",
        "contract_change_request",
        "alert_delivery_failed",
        "cp_breach_attempt",
        "timeout",
        "usage_missing",
        "escalation_resolved",
    }
)

#: Every founder-visible literal, keyed by slug (R1 audit surface). Romanian
#: only, plain language (founder protocol §6); ids always carried WITH a gloss
#: by the render code (R2).
RO: Mapping[str, str] = {
    "page_title": "SF-F5 — panoul fondatorului",
    "page_heading": "Fabrica SF-F5 — panoul fondatorului",
    "section_now": "Acum în lucru",
    "section_decisions": "Decizii așteptate",
    "section_plan": "Plan & istoric",
    "plan_footer": (
        "Vedere generată din planurile în git + stările din baza de date — "
        "nu este sursă canonică"
    ),
    "pulse_label": "Puls orchestrator",
    "pulse_missing": "fișierul de puls lipsește — orchestratorul nu a pornit încă",
    "pulse_stale": "posibil căzut",
    "pulse_now": "acum",
    "capacity_hold": "pauză de capacitate — sondez la fiecare {minutes} min",
    "phases_label": "Faze",
    "queue_label": "Coadă etape",
    "queue_waiting": "în așteptarea dependențelor",
    "queue_runnable": "gata de pornire",
    "queue_none_running": "nicio etapă în lucru",
    "budget_label": "Buget",
    "budget_total": "Total fabrică",
    "budget_tokens": "tokeni",
    "budget_estimated_part": "din care estimat",
    "budget_cost": "cost",
    "budget_today": "Astăzi",
    "missing_price": "— (preț lipsă în config)",
    "cost_details": "detalii →",
    "cost_total_row": "Total",
    "cost_legend": (
        "costurile = raportate exact de CLI (includ reducerile de cache); "
        "~ = estimare din prețurile din config; sumele sunt echivalent-API "
        "(abonamentul se facturează separat); agentul în lucru apare la "
        "finalul rulării sale"
    ),
    "costs_title": "Costuri pe agenți",
    "costs_phase_agents": "agenți de fază",
    "costs_none": "nicio cheltuială înregistrată încă",
    "incident_label": "Ultimul incident",
    "incident_none": "niciun incident înregistrat",
    "decisions_none": "Nicio decizie în așteptare — fabrica merge singură.",
    "decision_word": "Decizia",
    "stage_word": "Etapa",
    "phase_word": "Faza",
    "factory_word": "Fabrica",
    "created_word": "creată",
    "ago_word": "acum",
    "recommended_badge": "★ Recomandat",
    "options_label": "Opțiuni",
    "artifacts_label": "Artefacte mecanice",
    "request_summary": "Cererea completă",
    "banner_decisions_one": "O decizie așteaptă răspunsul tău",
    "banner_decisions_many": "decizii așteaptă răspunsul tău",
    "escalations_label": "Escaladări deschise",
    "escalations_none": "nicio escaladare deschisă",
    "escalation_last_resolved": "ultima escaladare rezolvată",
    "escalation_dossier": "dosar de escaladare",
    "escalation_reassurance": "în lucru la {target}; nu cere acțiunea ta",
    "escalation_founder_action": "necesită decizia fondatorului",
    "escalation_decision_link": "vezi cardul deciziei",
    "running_label": "Etape în lucru",
    "col_unit": "Unitate",
    "col_trigger": "Declanșator",
    "col_since": "De când",
    "col_phase": "Fază",
    "col_state": "Stare",
    "col_progress": "Progres",
    "col_stage": "Etapă",
    "col_step": "Pas atins",
    "col_risk": "Clasă de risc",
    "col_tokens": "Tokeni",
    "col_burn": "Consum",
    "col_cap": "Plafon",
    "col_pct": "%",
    "col_kind": "Tip",
    "col_file": "Fișier",
    "col_when": "Când",
    "col_cost": "Cost",
    "col_agent": "Agent",
    "col_model": "Model",
    "col_tokens_in": "Tokeni intrare",
    "col_tokens_out": "Tokeni ieșire",
    "no_buttons_notice": (
        "Acest tip de decizie nu are încă butoane de răspuns în panou — "
        "răspunde din terminal cu comanda de urgență „cli decide” "
        "(sf-factory decide <numărul deciziei> <opțiunea>)."
    ),
    "card_error": (
        "Această decizie nu a putut fi afișată (defect de redare — echipa "
        "tehnică vede detaliile în jurnal). Celelalte decizii rămân valabile; "
        "în caz de urgență folosește „cli decide”."
    ),
    "session_open": "Discută înainte de a decide",
    "session_title": "Sesiune de discuție",
    "session_intro": (
        "Discuție liberă despre această decizie. Conversația NU execută nimic — "
        "decizia se confirmă doar prin butoanele de opțiuni."
    ),
    "session_busy": "Agentul scrie un răspuns… pagina se actualizează singură.",
    "session_busy_refuse": "Agentul încă răspunde la mesajul anterior — așteaptă răspunsul.",
    "session_answering_refuse": (
        "Decizia se înregistrează chiar acum — sesiunea s-a închis; "
        "mesajul nu a fost trimis."
    ),
    "session_empty_message": "Mesajul este gol — scrie întrebarea înainte de a trimite.",
    "session_request_answered": "Decizia a fost deja înregistrată — sesiunea s-a închis.",
    "session_unknown_request": "Nu există această cerere de decizie.",
    "session_turns_exhausted": (
        "Sesiunea a atins numărul maxim de schimburi — decide prin butoanele "
        "de opțiuni sau cere o reanaliză prin escaladare."
    ),
    "session_budget_exhausted": (
        "Sesiunea a atins bugetul de discuție — decide prin butoanele de opțiuni."
    ),
    "session_unavailable": (
        "Sesiunile de discuție nu sunt pornite acum — reîncarcă pagina sau "
        "decide prin butoanele de opțiuni."
    ),
    "session_turn_failed": (
        "(Tura agentului a eșuat — mesajul tău rămâne în transcript; poți "
        "trimite altul. Nimic nu se reia automat.)"
    ),
    "session_turns_left": "schimburi rămase",
    "session_send": "Trimite",
    "session_message_placeholder": "Întrebarea ta pentru agent…",
    "session_confirm_label": "Confirmă decizia (acțiune definitivă)",
    "session_back": "Înapoi la panou",
    "founder_label": "Fondator",
    "agent_label": "Agent",
    "answered_ok": "Decizia a fost înregistrată. Etapele blocate repornesc automat.",
    "answered_already": "Decizia a fost deja înregistrată — nicio modificare.",
    "answer_unknown": "Nu există această cerere de decizie.",
    "answer_invalid_option": "Opțiune necunoscută pentru această decizie. Opțiuni valabile:",
    "answer_timeout": (
        "Răspunsul se procesează — reîncarcă pagina în câteva secunde "
        "(reîncercarea este sigură: nimic nu se înregistrează de două ori)."
    ),
    "answer_error": (
        "Înregistrarea deciziei a eșuat — nimic nu a fost salvat parțial. "
        "Reîncearcă; dacă persistă, folosește „cli decide”."
    ),
    "not_found": "Pagina cerută nu există.",
    "server_error": "Eroare internă — detaliile sunt în jurnalul tehnic.",
    "request_too_large": "Cererea este prea mare.",
    "loop_unavailable": "Panoul pornește — reîncarcă pagina în câteva secunde.",
    "artifact_title": "Artefact",
    "artifact_missing": "Artefactul cerut nu există sau nu a putut fi citit.",
    "back_to_dashboard": "Înapoi la panou",
    "missing_gloss": "etichetă lipsă",
    "progress_of": "din",
    "progress_done": "etape gata",
    "estimated_mark": "estimat",
    "plan_done_group": "Finalizate",
    "plan_running_group": "În lucru",
    "plan_pending_group": "Planificate",
    "plan_no_stages": "fază neîncepută — etapele apar după planificare",
    "plan_artifact_link": "planul fazei",
    "seconds_short": "s",
    "minutes_short": "min",
    "hours_short": "h",
    "days_short": "zile",
}

#: Romanian gloss per internal token rendered (R2). Closure pinned by tests:
#: all StageState/PhaseState/SchedCategory members, GATE_ANSWERS tokens,
#: INCIDENT_EVENT_TYPES, the full DDL gate_kind set, the golden config's
#: risk_classes keys (+ artifact kinds for the §2a mechanical links).
GLOSS: Mapping[str, str] = {
    # unit states (stage + phase; shared names glossed once)
    "PENDING": "planificată — așteaptă pornirea",
    "SPEC": "specificare în lucru",
    "BUILD": "construcție în lucru",
    "VALIDATE": "validare în lucru",
    "AUDIT": "audit în lucru",
    "AWAITING_HUMAN": "așteaptă decizia fondatorului",
    "MERGE_GATE": "poartă de integrare",
    "ESCALATED": "escaladată — așteaptă arhitectul",
    "DONE": "gata",
    "FAILED": "eșuată",
    "CANCELLED": "anulată",
    "PLANNING": "planificare în lucru",
    "CONTRACTS_FROZEN": "contracte înghețate",
    "RUNNING": "în derulare",
    "INTEGRATING": "integrare în lucru",
    "AWAITING_SIGNOFF": "așteaptă semnătura fondatorului",
    # scheduling categories
    "WAITING": "în așteptarea dependențelor",
    "RUNNABLE": "gata de pornire",
    "BLOCKED": "blocată — așteaptă o decizie sau o escaladare",
    "TERMINAL_OK": "finalizată",
    "TERMINAL_FAIL": "eșuată sau anulată",
    # gate kinds (full DDL set — glossed even where no executor consumes it yet)
    "critical_stage": "etapă critică — aprobare necesară",
    "business": "decizie de business",
    "phase_signoff": "semnătură de fază",
    "escalation_tradeoff": "compromis de produs la escaladare",
    # gate answer tokens (GATE_ANSWERS vocabulary)
    "approved": "aprobă",
    "rework:BUILD": "refă construcția",
    "rework:SPEC": "refă specificația",
    "changes": "cere modificări",
    "resume": "reia",
    "replan": "replanifică",
    # incident event types (§2b)
    "alert": "alertă",
    "declared_failure": "agentul a declarat eșec",
    "contract_change_request": "cerere de schimbare de contract",
    "alert_delivery_failed": "notificare nelivrată",
    "cp_breach_attempt": "încălcare de guvernanță (apel LLM neînregistrat)",
    "timeout": "timp depășit",
    "usage_missing": "consum de tokeni neraportat",
    "escalation_resolved": "escaladare rezolvată",
    # risk classes (golden config keys)
    "routine": "risc de rutină",
    "structural": "risc structural",
    "critical": "risc critic",
    # escalation triggers (§2 escalations.trigger vocabulary — FULL closure per
    # §10.4/D-0027: Trigger enum + the DDL-comment extras + the scheduler's
    # literal inserts; rendered by the escalations block and the re-authored
    # escalation-tradeoff request wrapper)
    "max_fix_iterations": "prea multe încercări de reparare fără progres",
    "churn_threshold": "prea multe modificări repetate în aceeași zonă de cod",
    "agent_declared_failure": "agentul a declarat eșec",
    "context_budget": "buget de tokeni depășit",
    "cp1_verdict": "triajul automat a cerut escaladare",
    "unresolved_contest": "constatare de audit contestată, nerezolvată",
    "semantic_conflict": "conflict semantic la integrare",
    "internal_error": "eroare internă a fabricii",
    "artifact_contract": "artefact neconform cu contractul",
    "child_failed": "o etapă din fază a eșuat",
    "integration_conflict": "conflict la integrare",
    # introduced by a follow-up slice (glossed now with §11 so the trigger never
    # renders bare — the closure tests are one-directional, token -> gloss)
    "agent_run_failed": "agentul a eșuat la rulare (oprire fără rezultat)",
    # escalation targets (DDL CHECK set, §10.4 — who handles it)
    "phase_architect": "arhitectul de fază",
    "main_architect": "arhitectul principal",
    "founder": "fondatorul — necesită decizia ta",
    # escalation resolutions (models.*_ESCALATION_RESOLUTIONS vocabulary, CCR-7 —
    # rendered by the „ultima escaladare rezolvată” line)
    "rework:VALIDATE": "reia validarea",
    "respec": "refă specificația",
    "awaiting_human": "trimisă la decizia fondatorului",
    "failed": "eșuată definitiv",
    "cancelled": "anulată",
    # artifact kinds (mechanical links, §2a)
    "spec": "specificație",
    "build_notes": "note de construcție",
    "validation_report": "raport de validare",
    "validation_sidecar": "raport de validare (date)",
    "audit_report": "raport de audit",
    "contract": "contract",
    "phase_plan": "plan de fază",
    "phase_plan_sidecar": "plan de fază (date)",
    "decision_request": "cerere de decizie",
    "decision_answer": "răspuns la decizie",
    "escalation_payload": "dosar de escaladare",
    "contest_rationale": "motivație de contestare",
    "transcript": "transcript de sesiune",
    "tier1_conflict": "conflict la integrare (nivel 1)",
    # agent roles (§11/F6: the golden config's models.* keys are the closure
    # source — roles are config-defined, not enum-defined; rendered by the
    # /costuri per-agent tables. phase_architect/main_architect/founder are
    # glossed above as escalation targets, one gloss per token.)
    "spec_agent": "agent de specificații",
    "builder_routine": "constructor (etape ușoare)",
    "builder_heavy": "constructor (etape grele)",
    "validator": "validator",
    "validator_structural": "validator structural",
    "integration_validator": "validator de integrare",
    "auditor_same_model": "auditor (același model)",
    "auditor_cross_model": "auditor încrucișat (codex)",
    "cp1_triage": "triaj CP-1",
    "decision_session": "sesiune de decizie",
    "capacity_probe": "sondă de capacitate",
    # ledger model tokens (§11/F6: models.*.model values + pricing.usd_per_mtok
    # keys; codex rows record 'default' — the §11.5.4 attribution watch item)
    "fable": "Claude Fable",
    "sonnet": "Claude Sonnet",
    "haiku": "Claude Haiku",
    "opus-4-8": "Claude Opus 4.8",
    "default": "codex — model implicit",
}

#: §10.2 state -> chip category (running=accent, blocked/awaiting=warn,
#: escalated/failed=err, done=ok). Color is SUPPLEMENTARY — the text gloss
#: always renders inside the chip. Closure over ALL StageState/PhaseState
#: members pinned by test; an unknown state at runtime falls back to
#: 'neutral' explicitly (R-B6), never a KeyError.
STATE_CHIPS: Mapping[str, str] = {
    "PENDING": "neutral",
    "SPEC": "accent",
    "BUILD": "accent",
    "VALIDATE": "accent",
    "AUDIT": "accent",
    "MERGE_GATE": "accent",
    "PLANNING": "accent",
    "CONTRACTS_FROZEN": "accent",
    "RUNNING": "accent",
    "INTEGRATING": "accent",
    "AWAITING_HUMAN": "warn",
    "AWAITING_SIGNOFF": "warn",
    "ESCALATED": "err",
    "FAILED": "err",
    "CANCELLED": "err",
    "DONE": "ok",
}


class DashboardError(FactoryError):
    """Module-local taxonomy leaf (models taxonomy unchanged) — founder-facing
    refusals (session busy/locked/exhausted) carry their RO message here."""


# -------------------------------------------------------------- pure helpers


def esc(text: str) -> str:
    """html.escape wrapper — the ONLY path any artifact/agent/founder text takes
    into HTML (R5)."""
    return html.escape(str(text), quote=True)


def fmt_founder_ts(utc_iso: str, tz: str) -> str:
    """ISO-8601-UTC -> 'DD-MM-YYYY HH:MM' in factory.timezone_founder (conventions.md)."""
    try:
        moment = datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (ValueError, TypeError) as exc:
        raise DashboardError(f"unparseable machine timestamp: {utc_iso!r}") from exc
    return moment.astimezone(ZoneInfo(tz)).strftime("%d-%m-%Y %H:%M")


def _fmt_int(value: int) -> str:
    """Numbers grouped Romanian-style: 300000 -> '300.000' (R4)."""
    return f"{int(value):,}".replace(",", ".")


_THIN_SPACE = " "  # §11.2 _fmt_usd: thin space before the '$'


def _fmt_usd(value: float) -> str:
    """§11.2 money format: two decimals, Romanian decimal COMMA (thousands
    grouped with dots, R4), thin space + '$'; sub-cent non-zero -> '<0,01 $'."""
    if 0 < value < 0.01:
        return f"<0,01{_THIN_SPACE}$"
    grouped = f"{value:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return f"{grouped}{_THIN_SPACE}$"


def _age_seconds(utc_iso: str, now_iso: str) -> int:
    then = datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    now = datetime.strptime(now_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return max(0, int((now - then).total_seconds()))


def _age_text(seconds: int) -> str:
    """'acum Xs / X min / Xh / X zile' — founder-facing age."""
    if seconds < 60:
        return f"{RO['ago_word']} {seconds}{RO['seconds_short']}"
    if seconds < 3600:
        return f"{RO['ago_word']} {seconds // 60} {RO['minutes_short']}"
    if seconds < 86400:
        return f"{RO['ago_word']} {seconds // 3600}{RO['hours_short']}"
    return f"{RO['ago_word']} {seconds // 86400} {RO['days_short']}"


def _founder_day_start_utc(now_iso: str, tz: str) -> str:
    """Founder-TZ midnight of `now`'s local day, converted to ISO-UTC — the
    «Astăzi» ledger cut (§11.2, F5): the founder's day, never the UTC day."""
    moment = datetime.strptime(now_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    local = moment.astimezone(ZoneInfo(tz))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _glossed(token: str) -> str:
    """'<gloss> (<token>)' (R2); unknown token -> '<token> (etichetă lipsă)' —
    visible defect, the page never dies on it."""
    gloss = GLOSS.get(token)
    if gloss is None:
        return f"{token} ({RO['missing_gloss']})"
    return f"{gloss} ({token})"


def _label(token: str) -> str:
    """Romanian label for a token; unknown -> visible '<token> (etichetă lipsă)'."""
    gloss = GLOSS.get(token)
    return gloss if gloss is not None else f"{token} ({RO['missing_gloss']})"


def _chip(state: str) -> str:
    """State gloss inside a colored chip (§10.2): category via STATE_CHIPS with
    an explicit 'neutral' fallback (R-B6); the text gloss is always present —
    color is supplementary, never the information."""
    category = STATE_CHIPS.get(state, "neutral")
    return f"<span class='chip chip-{category}'>{esc(_glossed(state))}</span>"


def resolve_bind_host(cfg: FactoryConfig) -> str:
    """'tailscale' -> first `tailscale ip -4` address; other values literal;
    failure -> FactoryError (abort start, §1/OPEN-D2)."""
    bind = cfg.founder_channel.dashboard.bind
    if bind != "tailscale":
        return bind
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, read-only query
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FactoryError(
            f"cannot resolve the tailnet bind address (`tailscale ip -4`): {exc} — "
            "the dashboard is the founder's only decision surface; start aborted (D-0017)"
        ) from exc
    if proc.returncode != 0:
        raise FactoryError(
            "`tailscale ip -4` failed "
            f"(exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()} — start aborted"
        )
    first = proc.stdout.strip().splitlines()
    if not first or not first[0].strip():
        raise FactoryError("`tailscale ip -4` returned no address — start aborted")
    return first[0].strip()


#: R3 marker contract (ratified with the design): a machine-readable line
#: 'Recomandare: <option-token>' (or 'Recommendation:') in the request artifact.
_RECOMMEND_RE = re.compile(r"^\s*(?:Recomandare|Recommendation)\s*:\s*(\S+)\s*$", re.MULTILINE)


def _parse_recommendation(text: str, options: tuple[str, ...]) -> str | None:
    """First marker line whose value matches a DECLARED option token; absent or
    unmatched -> None (a badge is never invented, R3)."""
    for match in _RECOMMEND_RE.finditer(text):
        if match.group(1) in options:
            return match.group(1)
    return None


def _resolve(home: Path, path: Path) -> Path:
    return path if path.is_absolute() else home / path


# ------------------------------------------------------- private read helpers
# Module-private READ-ONLY SQL over the §2 DDL (the cli.py status-view pattern):
# presentation queries, no business rules, no writes.


def _open_ro(cfg: FactoryConfig) -> Database:
    """Fresh short-lived mode=ro connection for one GET (§1 read path)."""
    db = Database(_resolve(cfg.factory.home, cfg.process.db_path), cfg.process.db_busy_timeout_ms)
    db.open(read_only=True)
    return db


def _get_decision(conn: sqlite3.Connection, request_id: int) -> DecisionRequest | None:
    row = conn.execute(
        "SELECT * FROM decision_requests WHERE id = ?", (request_id,)
    ).fetchone()
    if row is None:
        return None
    return DecisionRequest(
        id=row["id"],
        unit_level=row["unit_level"],
        unit_id=row["unit_id"],
        gate_kind=row["gate_kind"],
        request_artifact_id=row["request_artifact_id"],
        status=row["status"],
        answer=row["answer"],
        answer_artifact_id=row["answer_artifact_id"],
        created_at=row["created_at"],
        alerted_at=row["alerted_at"],
        answered_at=row["answered_at"],
    )


def _artifact_row(conn: sqlite3.Connection, ref_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM artifact_refs WHERE id = ?", (ref_id,)).fetchone()


def _latest_artifact_rows(
    conn: sqlite3.Connection, unit_level: str, unit_id: str
) -> list[sqlite3.Row]:
    """Latest registered artifact per kind for a unit (mechanical links, §2a)."""
    return conn.execute(
        "SELECT * FROM artifact_refs WHERE id IN ("
        " SELECT MAX(id) FROM artifact_refs WHERE unit_level = ? AND unit_id = ?"
        " GROUP BY kind) ORDER BY id DESC",
        (unit_level, unit_id),
    ).fetchall()


def _unit_name(conn: sqlite3.Connection, unit_level: str, unit_id: str) -> str:
    table = "stages" if unit_level == Level.STAGE.value else "phases"
    if unit_level not in (Level.STAGE.value, Level.PHASE.value):
        return unit_id
    row = conn.execute(f"SELECT name FROM {table} WHERE id = ?", (unit_id,)).fetchone()
    return row["name"] if row is not None else unit_id


def _workspace_root(cfg: FactoryConfig, conn: sqlite3.Connection) -> Path | None:
    """artifact_refs.repo='workspace' -> project workspace root (single-project
    MVP rule, mirroring scheduler._repo_roots)."""
    projects = cfg.projects
    if len(projects) == 1:
        return _resolve(cfg.factory.home, next(iter(projects.values())).workspace)
    referenced = {
        row["project"]
        for row in conn.execute("SELECT DISTINCT project FROM phases").fetchall()
    } & set(projects)
    if len(referenced) == 1:
        return _resolve(cfg.factory.home, projects[next(iter(referenced))].workspace)
    return None


def _git_blob(repo_root: Path, spec: str) -> str | None:
    try:
        proc = subprocess.run(  # noqa: S603 — fixed git argv, read-only query
            ["git", "-C", str(repo_root), "cat-file", "blob", spec],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def _artifact_text(cfg: FactoryConfig, conn: sqlite3.Connection, ref: sqlite3.Row) -> str:
    """Resolve registered artifact content — the verify_integrity precedence
    (§1): stage worktree file -> `git cat-file <commit>:<path>` -> repo HEAD."""
    if ref["unit_level"] == Level.STAGE.value:
        stage_row = conn.execute(
            "SELECT worktree_path FROM stages WHERE id = ?", (ref["unit_id"],)
        ).fetchone()
        if stage_row is not None and stage_row["worktree_path"]:
            candidate = Path(stage_row["worktree_path"]) / ref["path"]
            if candidate.is_file():
                try:
                    return candidate.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
    if ref["repo"] == "factory":
        root: Path | None = cfg.factory.home
    else:
        root = _workspace_root(cfg, conn)
    if root is None:
        raise DashboardError(f"no repo root for artifact repo {ref['repo']!r}")
    if ref["git_commit"]:
        blob = _git_blob(root, f"{ref['git_commit']}:{ref['path']}")
        if blob is not None:
            return blob
    direct = root / ref["path"]
    if direct.is_file():
        try:
            return direct.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    blob = _git_blob(root, f"HEAD:{ref['path']}")
    if blob is not None:
        return blob
    raise DashboardError(f"artifact {ref['id']} unresolved at {ref['repo']}:{ref['path']}")


# ------------------------------------------------------------ §11 cost shapes
# CCR-10 (design §11): per-stage agent cost breakdown over token_ledger. The
# honesty rule (Doctrine §21, §11.1): cost_usd non-NULL renders EXACT as-is
# (the CLI's own cache-aware figure); NULL estimates from pricing.usd_per_mtok
# with a `~` prefix; NULL cost + missing pricing key renders the explicit
# missing-price marker, never a silent zero; exact and estimated sums always
# form a PAIR — never merged, including every summary line (F8).


@dataclass(frozen=True)
class CostSummary:
    """Exact/estimated cost pair for one scope (unit, phase, day, factory)."""

    exact_usd: float | None = None  # None = no CLI-reported-cost rows in scope
    est_usd: float | None = None  # None = no estimable NULL-cost rows in scope
    missing_price: bool = False  # NULL-cost rows whose model has no pricing key

    @property
    def empty(self) -> bool:
        """True = NO cost cell renders for this scope (e.g. a PENDING stage)."""
        return self.exact_usd is None and self.est_usd is None and not self.missing_price


_NO_COST = CostSummary()


@dataclass(frozen=True)
class AgentCostRow:
    """One token_ledger row — a /costuri per-agent table row (§11.2; F7:
    ordered by ledger id, recorded_at displayed)."""

    ledger_id: int
    role: str
    model: str
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    estimated: bool  # token counts estimated (bytes/4 fallback) -> keeps `~`
    recorded_at: str


@dataclass(frozen=True)
class StageCosts:
    """/costuri: one stage's per-agent table (anchor id=<stage_id>)."""

    stage_id: str
    name: str
    rows: tuple[AgentCostRow, ...]
    total: CostSummary


@dataclass(frozen=True)
class PhaseCosts:
    """/costuri: one phase bloc — the total INCLUDES the phase's own
    unit_level='phase' ledger rows (F3: the PLANNING agent is an involved
    agent; stage-only derivation silently drops its spend)."""

    phase_id: str
    name: str
    phase_rows: tuple[AgentCostRow, ...]
    stages: tuple[StageCosts, ...]
    total: CostSummary


@dataclass(frozen=True)
class CostsView:
    """Pure render input of GET /costuri (read-only, refresh-free)."""

    generated_at: str
    phases: tuple[PhaseCosts, ...]


def _estimate_usd(
    cfg: FactoryConfig, model: str, tokens_in: int, tokens_out: int
) -> float | None:
    """§11.1 estimation: tokens/1e6 × pricing.usd_per_mtok.<model>; None when
    the model has no pricing key (the caller renders the explicit marker)."""
    price = cfg.pricing.usd_per_mtok.get(model)
    if price is None:
        return None
    return tokens_in / 1e6 * price.input + tokens_out / 1e6 * price.output


def _summary_from_groups(cfg: FactoryConfig, groups: Iterable[sqlite3.Row]) -> CostSummary:
    """db.sum_token_cost group rows -> CostSummary (§11.1 precedence)."""
    exact: float | None = None
    est: float | None = None
    missing = False
    for group in groups:
        if group["exact_usd"] is not None:
            exact = (exact or 0.0) + float(group["exact_usd"])
        if group["null_cost_rows"]:
            estimate = _estimate_usd(
                cfg,
                group["model"],
                int(group["est_tokens_in"]),
                int(group["est_tokens_out"]),
            )
            if estimate is None:
                missing = True
            else:
                est = (est or 0.0) + estimate
    return CostSummary(exact, est, missing)


def _summary_from_rows(cfg: FactoryConfig, rows: Iterable[AgentCostRow]) -> CostSummary:
    """AgentCostRow sequence -> CostSummary (same §11.1 precedence per row)."""
    exact: float | None = None
    est: float | None = None
    missing = False
    for row in rows:
        if row.cost_usd is not None:
            exact = (exact or 0.0) + float(row.cost_usd)
            continue
        estimate = _estimate_usd(cfg, row.model, row.tokens_in or 0, row.tokens_out or 0)
        if estimate is None:
            missing = True
        else:
            est = (est or 0.0) + estimate
    return CostSummary(exact, est, missing)


def _combine_summaries(parts: Iterable[CostSummary]) -> CostSummary:
    """Sum pairs componentwise — exact and estimated NEVER cross (F8)."""
    exact: float | None = None
    est: float | None = None
    missing = False
    for part in parts:
        if part.exact_usd is not None:
            exact = (exact or 0.0) + part.exact_usd
        if part.est_usd is not None:
            est = (est or 0.0) + part.est_usd
        missing = missing or part.missing_price
    return CostSummary(exact, est, missing)


def _fmt_cost_pair(summary: CostSummary) -> str:
    """«12,40 $ + ~0,85 $» — the exact part, the `~` estimated part and the
    missing-price marker as SEPARATE addends (F8); '' when nothing is in scope
    (no cost cell renders — §11.4 PENDING case)."""
    parts: list[str] = []
    if summary.exact_usd is not None:
        parts.append(_fmt_usd(summary.exact_usd))
    if summary.est_usd is not None:
        parts.append(f"~{_fmt_usd(summary.est_usd)}")
    if summary.missing_price:
        parts.append(RO["missing_price"])
    return " + ".join(parts)


def _fmt_row_cost(cfg: FactoryConfig, row: AgentCostRow) -> str:
    """One ledger row's cost cell (§11.1): non-NULL cost_usd -> exact as-is;
    NULL -> `~` config-price estimate; NULL + missing key -> explicit marker;
    estimated=1 token counts keep the `~` regardless of cost source."""
    if row.cost_usd is not None:
        text = _fmt_usd(row.cost_usd)
        return f"~{text}" if row.estimated else text
    estimate = _estimate_usd(cfg, row.model, row.tokens_in or 0, row.tokens_out or 0)
    if estimate is None:
        return RO["missing_price"]
    return f"~{_fmt_usd(estimate)}"


# --------------------------------------------------------------- view shapes
# Constituents of DashboardView per its frozen §6 docstring ("decision cards,
# health strip, plan rows — assembled, pre-glossed").


@dataclass(frozen=True)
class ArtifactLink:
    """One §2a mechanical link: /artifact/<ref_id>, kind-glossed label + filename."""

    ref_id: int
    kind: str
    filename: str


@dataclass(frozen=True)
class DecisionCard:
    """One pending decision, fully assembled for render (§2a)."""

    request_id: int
    unit_level: str
    unit_id: str
    unit_name: str
    gate_kind: str
    created_at: str
    created_display: str  # founder format + age (pre-rendered, R4)
    request_text: str  # FULL request artifact content (raw; esc() at render)
    options: tuple[str, ...]  # declared GATE_ANSWERS tokens; () = unmapped gate
    recommended: str | None  # R3 parsed token, never invented
    artifact_links: tuple[ArtifactLink, ...]
    error: str | None = None  # per-card containment: RO error text replaces body


@dataclass(frozen=True)
class PhaseHealth:
    """§2b 'Faze' row: one non-terminal phase."""

    phase_id: str
    name: str
    state: str
    stages_done: int
    stages_total: int


@dataclass(frozen=True)
class RunningStage:
    """§2b/§10.3 'Etape în lucru' row: one RUNNING-category stage."""

    stage_id: str
    name: str
    state: str
    risk_class: str
    tokens: int
    cost: CostSummary = _NO_COST  # §11.2: the right-aligned cost pair


@dataclass(frozen=True)
class EscalationRow:
    """§10.4 'Escaladări deschise' row (escalations WHERE status='open')."""

    escalation_id: int
    unit_level: str
    unit_id: str
    unit_name: str
    trigger: str
    target: str
    created_at: str
    payload_artifact_id: int | None
    #: founder-target rows only: the unit's newest PENDING decision (the card
    #: the founder must answer), None when no card exists (§10.4).
    decision_request_id: int | None


@dataclass(frozen=True)
class ResolvedEscalation:
    """§10.4 'ultima escaladare rezolvată' line (S2 cold-return visibility)."""

    unit_name: str
    unit_id: str
    resolution: str
    resolved_at: str


@dataclass(frozen=True)
class BudgetRow:
    """§2b 'Buget' row for one active stage."""

    stage_id: str
    name: str
    risk_class: str
    tokens: int
    budget: int | None


@dataclass(frozen=True)
class Incident:
    """§2b 'Ultimul incident' (newest INCIDENT_EVENT_TYPES event)."""

    event_type: str
    unit_level: str
    unit_id: str | None
    unit_name: str
    created_at: str


@dataclass(frozen=True)
class HealthStrip:
    """§2b assembled health data (+ the §10.4 escalations view)."""

    liveness_age_s: int | None  # None = liveness file missing
    liveness_display: str  # pre-rendered RO line
    liveness_stale: bool
    phases: tuple[PhaseHealth, ...]
    running_stages: tuple[RunningStage, ...]
    waiting_count: int
    runnable_count: int
    budgets: tuple[BudgetRow, ...]
    total_tokens: int
    total_estimated_tokens: int
    #: §11.2 (F11): the factory lifetime cost as the exact/estimated PAIR —
    #: a merged SUM made codex spend invisible.
    factory_cost: CostSummary
    #: §11.2 (F5): «Astăzi» — ledger rows since founder-TZ midnight (UTC cut).
    today_cost: CostSummary
    incident: Incident | None
    escalations: tuple[EscalationRow, ...]
    last_resolved: ResolvedEscalation | None
    #: CCR-11 (D-0037): pre-rendered RO hold line when a capacity hold is
    #: active (a capacity_hold_started event without a later _ended one —
    #: read-path only); None otherwise.
    capacity_hold_display: str | None = None


@dataclass(frozen=True)
class PlanStage:
    """§2c stage row."""

    stage_id: str
    name: str
    state: str
    risk_class: str
    #: §11.2: cost pair + «detalii →» link to /costuri#<stage_id> when the
    #: stage has ledger rows; empty for PENDING stages (no cost row/link).
    cost: CostSummary = _NO_COST


@dataclass(frozen=True)
class PlanPhase:
    """§2c phase block: stages grouped done/running/pending, DAG order."""

    phase_id: str
    name: str
    state: str
    done: tuple[PlanStage, ...]
    running: tuple[PlanStage, ...]
    pending: tuple[PlanStage, ...]
    plan_artifact_id: int | None
    #: §11.2 phase total pair = stage rows + the phase's OWN unit_level='phase'
    #: ledger rows (F3 — matches the figure `status` shows the founder).
    cost: CostSummary = _NO_COST


@dataclass(frozen=True)
class DashboardView:
    """Pure render input: decision cards, health strip, plan rows (assembled,
    pre-glossed)."""

    generated_at: str
    cards: tuple[DecisionCard, ...]
    health: HealthStrip
    plan: tuple[PlanPhase, ...]


@dataclass(frozen=True)
class Turn:
    """n: int, author: Literal['founder','agent'], text: str, at: str (ISO UTC)."""

    n: int
    author: Literal["founder", "agent"]
    text: str
    at: str


@dataclass(frozen=True)
class SessionSnapshot:
    """request_id, turns: tuple[Turn, ...], busy: bool, locked: str | None, turns_left: int."""

    request_id: int
    turns: tuple[Turn, ...]
    busy: bool
    locked: str | None
    turns_left: int


class AnswerOutcome(StrEnum):
    """ANSWERED ALREADY_ANSWERED UNKNOWN_REQUEST INVALID_OPTION."""

    ANSWERED = "ANSWERED"
    ALREADY_ANSWERED = "ALREADY_ANSWERED"
    UNKNOWN_REQUEST = "UNKNOWN_REQUEST"
    INVALID_OPTION = "INVALID_OPTION"


@dataclass(frozen=True)
class AnswerResult:
    """outcome: AnswerOutcome, request_id: int, option: str | None,
    answer_artifact_path: str | None."""

    outcome: AnswerOutcome
    request_id: int
    option: str | None
    answer_artifact_path: str | None


# ----------------------------------------------------------------- build_view


def _build_card(
    cfg: FactoryConfig, conn: sqlite3.Connection, dr: DecisionRequest, now: str
) -> DecisionCard:
    assert dr.id is not None
    unit_name = _unit_name(conn, dr.unit_level, dr.unit_id)
    options = GATE_ANSWERS.get((dr.unit_level, dr.gate_kind), ())
    ref = _artifact_row(conn, dr.request_artifact_id)
    if ref is None:
        raise DashboardError(f"decision {dr.id}: request artifact ref missing")
    request_text = _artifact_text(cfg, conn, ref)
    links = tuple(
        ArtifactLink(ref_id=int(row["id"]), kind=row["kind"], filename=Path(row["path"]).name)
        for row in _latest_artifact_rows(conn, dr.unit_level, dr.unit_id)
    )
    created_display = (
        f"{RO['created_word']} {fmt_founder_ts(dr.created_at, cfg.factory.timezone_founder)}"
        f" · {_age_text(_age_seconds(dr.created_at, now))}"
    )
    return DecisionCard(
        request_id=dr.id,
        unit_level=dr.unit_level,
        unit_id=dr.unit_id,
        unit_name=unit_name,
        gate_kind=dr.gate_kind,
        created_at=dr.created_at,
        created_display=created_display,
        request_text=request_text,
        options=options,
        recommended=_parse_recommendation(request_text, options),
        artifact_links=links,
    )


def _error_card(dr: DecisionRequest, exc: Exception) -> DecisionCard:
    print(
        f"dashboard: decision card {dr.id} failed to assemble: {exc!r}\n"
        + "".join(traceback.format_exception(exc)),
        file=sys.stderr,
    )
    return DecisionCard(
        request_id=dr.id or 0,
        unit_level=dr.unit_level,
        unit_id=dr.unit_id,
        unit_name=dr.unit_id,
        gate_kind=dr.gate_kind,
        created_at=dr.created_at,
        created_display="",
        request_text="",
        options=(),
        recommended=None,
        artifact_links=(),
        error=RO["card_error"],
    )


def _phase_dag_order(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Phases in DAG order (§2c): Kahn over level='phase' dag_edges, insertion
    order as tiebreak; a defective cycle falls back to insertion order — a
    render view must degrade, never crash the page."""
    rows = conn.execute("SELECT * FROM phases ORDER BY created_at, id").fetchall()
    ids = [r["id"] for r in rows]
    by_id = {r["id"]: r for r in rows}
    edges = conn.execute(
        "SELECT from_id, to_id FROM dag_edges WHERE level = 'phase'"
    ).fetchall()
    indegree = dict.fromkeys(ids, 0)
    children: dict[str, list[str]] = {pid: [] for pid in ids}
    for edge in edges:
        if edge["from_id"] in by_id and edge["to_id"] in by_id:
            indegree[edge["to_id"]] += 1
            children[edge["from_id"]].append(edge["to_id"])
    queue = [pid for pid in ids if indegree[pid] == 0]
    ordered: list[str] = []
    while queue:
        pid = queue.pop(0)
        ordered.append(pid)
        for child in children[pid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(ordered) != len(ids):  # cycle — fall back, never die
        ordered = ids
    return [by_id[pid] for pid in ordered]


def _cost_buckets(
    cfg: FactoryConfig, conn: sqlite3.Connection
) -> dict[tuple[str, str], CostSummary]:
    """One §11 ledger aggregate pass -> per-(unit_level, unit_id) cost pair."""
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for group in fdb.sum_token_cost(conn):
        grouped.setdefault((group["unit_level"], group["unit_id"]), []).append(group)
    return {key: _summary_from_groups(cfg, groups) for key, groups in grouped.items()}


def _build_health(
    cfg: FactoryConfig,
    conn: sqlite3.Connection,
    now: str,
    costs: Mapping[tuple[str, str], CostSummary],
) -> HealthStrip:
    liveness = _resolve(cfg.factory.home, cfg.process.liveness_file)
    threshold = float(cfg.founder_channel.watchdog.staleness_threshold_s)
    try:
        mtime: float | None = liveness.stat().st_mtime
    except OSError:
        mtime = None
    if mtime is None:
        age: int | None = None
        display = RO["pulse_missing"]
        stale = True
    else:
        age = max(0, int(time.time() - mtime))
        mtime_iso = datetime.fromtimestamp(mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        local = fmt_founder_ts(mtime_iso, cfg.factory.timezone_founder)
        display = f"{RO['pulse_now']} {age}{RO['seconds_short']} · {local}"
        stale = age >= threshold

    phase_rows = conn.execute("SELECT * FROM phases ORDER BY created_at, id").fetchall()
    stage_rows = conn.execute("SELECT * FROM stages ORDER BY created_at, id").fetchall()
    stages_by_phase: dict[str, list[sqlite3.Row]] = {}
    for srow in stage_rows:
        stages_by_phase.setdefault(srow["phase_id"], []).append(srow)

    terminal_phase = {"DONE", "FAILED", "CANCELLED"}
    phases = tuple(
        PhaseHealth(
            phase_id=prow["id"],
            name=prow["name"],
            state=prow["state"],
            stages_done=sum(
                1 for s in stages_by_phase.get(prow["id"], ()) if s["state"] == "DONE"
            ),
            stages_total=len(stages_by_phase.get(prow["id"], ())),
        )
        for prow in phase_rows
        if prow["state"] not in terminal_phase
    )

    running: list[RunningStage] = []
    waiting = runnable = 0
    budgets: list[BudgetRow] = []
    active_states = {"SPEC", "BUILD", "VALIDATE", "AUDIT", "MERGE_GATE", "AWAITING_HUMAN",
                     "ESCALATED"}
    for srow in stage_rows:
        state = srow["state"]
        deps = fdb.deps_done(conn, Level.STAGE, srow["id"]) if state == "PENDING" else True
        category = sched_category(Level.STAGE, state, deps)
        tokens = (
            fdb.unit_token_total(conn, Level.STAGE.value, srow["id"])
            if state in active_states
            else 0
        )
        if category is SchedCategory.RUNNING:
            running.append(
                RunningStage(
                    stage_id=srow["id"],
                    name=srow["name"],
                    state=state,
                    risk_class=srow["risk_class"],
                    tokens=tokens,
                    cost=costs.get((Level.STAGE.value, srow["id"]), _NO_COST),
                )
            )
        elif category is SchedCategory.WAITING:
            waiting += 1
        elif category is SchedCategory.RUNNABLE:
            runnable += 1
        if state in active_states:
            budgets.append(
                BudgetRow(
                    stage_id=srow["id"],
                    name=srow["name"],
                    risk_class=srow["risk_class"],
                    tokens=tokens,
                    budget=cfg.budgets.per_stage.get(srow["risk_class"]),
                )
            )

    totals = conn.execute(
        "SELECT COALESCE(SUM(tokens_in), 0) + COALESCE(SUM(tokens_out), 0) AS tokens"
        " FROM token_ledger"
    ).fetchone()
    estimated = conn.execute(
        "SELECT COALESCE(SUM(tokens_in), 0) + COALESCE(SUM(tokens_out), 0) AS tokens"
        " FROM token_ledger WHERE estimated = 1"
    ).fetchone()
    # §11.2: the factory lifetime PAIR (F11) and the founder's-day pair (F5 —
    # founder-TZ midnight converted to a UTC ledger cut).
    factory_cost = _combine_summaries(costs.values())
    today_cost = _summary_from_groups(
        cfg,
        fdb.sum_token_cost(
            conn, since=_founder_day_start_utc(now, cfg.factory.timezone_founder)
        ),
    )

    placeholders = ",".join("?" for _ in INCIDENT_EVENT_TYPES)
    incident_row = conn.execute(
        f"SELECT * FROM events WHERE event_type IN ({placeholders})"
        " ORDER BY seq DESC LIMIT 1",
        sorted(INCIDENT_EVENT_TYPES),
    ).fetchone()
    incident = None
    if incident_row is not None:
        unit_id = incident_row["unit_id"]
        incident = Incident(
            event_type=incident_row["event_type"],
            unit_level=incident_row["unit_level"],
            unit_id=unit_id,
            unit_name=(
                _unit_name(conn, incident_row["unit_level"], unit_id)
                if unit_id
                else RO["factory_word"]
            ),
            created_at=incident_row["created_at"],
        )

    # §10.4 (D-0026 gap b): open escalations, oldest first; founder-target rows
    # carry the unit's newest pending decision so the row can link the card.
    escalations: list[EscalationRow] = []
    for erow in conn.execute(
        "SELECT * FROM escalations WHERE status = 'open' ORDER BY id"
    ).fetchall():
        decision_request_id = None
        if erow["target"] == "founder":
            drow = conn.execute(
                "SELECT id FROM decision_requests WHERE unit_level = ?"
                " AND unit_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
                (erow["unit_level"], erow["unit_id"]),
            ).fetchone()
            decision_request_id = int(drow["id"]) if drow is not None else None
        escalations.append(
            EscalationRow(
                escalation_id=int(erow["id"]),
                unit_level=erow["unit_level"],
                unit_id=erow["unit_id"],
                unit_name=_unit_name(conn, erow["unit_level"], erow["unit_id"]),
                trigger=erow["trigger"],
                target=erow["target"],
                created_at=erow["created_at"],
                payload_artifact_id=erow["payload_artifact_id"],
                decision_request_id=decision_request_id,
            )
        )
    # CCR-11 (D-0037): the capacity hold is active when the newest
    # capacity_hold_started event has no later capacity_hold_ended — pure
    # read-path (the governor owns the writes; recover() closes stale pairs).
    hold_row = conn.execute(
        "SELECT"
        " COALESCE(MAX(CASE WHEN event_type='capacity_hold_started' THEN seq END), 0)"
        " AS started,"
        " COALESCE(MAX(CASE WHEN event_type='capacity_hold_ended' THEN seq END), 0)"
        " AS ended"
        " FROM events WHERE event_type IN"
        " ('capacity_hold_started','capacity_hold_ended')"
    ).fetchone()
    capacity_hold_display = None
    if int(hold_row["started"]) > int(hold_row["ended"]):
        minutes = max(1, round(cfg.capacity_governor.probe_interval_s / 60))
        capacity_hold_display = RO["capacity_hold"].format(minutes=minutes)

    resolved_row = conn.execute(
        "SELECT * FROM escalations WHERE status = 'resolved'"
        " ORDER BY resolved_at DESC, id DESC LIMIT 1"
    ).fetchone()
    last_resolved = None
    if resolved_row is not None:
        last_resolved = ResolvedEscalation(
            unit_name=_unit_name(
                conn, resolved_row["unit_level"], resolved_row["unit_id"]
            ),
            unit_id=resolved_row["unit_id"],
            resolution=resolved_row["resolution"] or "",
            resolved_at=resolved_row["resolved_at"] or resolved_row["created_at"],
        )

    return HealthStrip(
        liveness_age_s=age,
        liveness_display=display,
        liveness_stale=stale,
        phases=phases,
        running_stages=tuple(running),
        waiting_count=waiting,
        runnable_count=runnable,
        budgets=tuple(budgets),
        total_tokens=int(totals["tokens"]),
        total_estimated_tokens=int(estimated["tokens"]),
        factory_cost=factory_cost,
        today_cost=today_cost,
        incident=incident,
        escalations=tuple(escalations),
        last_resolved=last_resolved,
        capacity_hold_display=capacity_hold_display,
    )


def _build_plan(
    conn: sqlite3.Connection, costs: Mapping[tuple[str, str], CostSummary]
) -> tuple[PlanPhase, ...]:
    plan: list[PlanPhase] = []
    stage_rows = conn.execute("SELECT * FROM stages ORDER BY created_at, id").fetchall()
    by_phase: dict[str, list[sqlite3.Row]] = {}
    for srow in stage_rows:
        by_phase.setdefault(srow["phase_id"], []).append(srow)
    for prow in _phase_dag_order(conn):
        done: list[PlanStage] = []
        running: list[PlanStage] = []
        pending: list[PlanStage] = []
        stage_costs: list[CostSummary] = []
        for srow in by_phase.get(prow["id"], ()):
            stage_cost = costs.get((Level.STAGE.value, srow["id"]), _NO_COST)
            stage_costs.append(stage_cost)
            stage = PlanStage(
                stage_id=srow["id"],
                name=srow["name"],
                state=srow["state"],
                risk_class=srow["risk_class"],
                cost=stage_cost,
            )
            if srow["state"] == "DONE":
                done.append(stage)
            elif srow["state"] == "PENDING":
                pending.append(stage)
            elif srow["state"] not in ("FAILED", "CANCELLED"):
                running.append(stage)
            else:  # terminal-fail stages stay visible in the done group's place
                done.append(stage)
        plan.append(
            PlanPhase(
                phase_id=prow["id"],
                name=prow["name"],
                state=prow["state"],
                done=tuple(done),
                running=tuple(running),
                pending=tuple(pending),
                plan_artifact_id=prow["plan_artifact_id"],
                # F3: phase total = stage rows + the phase's OWN ledger rows.
                cost=_combine_summaries(
                    [costs.get((Level.PHASE.value, prow["id"]), _NO_COST), *stage_costs]
                ),
            )
        )
    return tuple(plan)


def build_view(cfg: FactoryConfig, *, now: str | None = None) -> DashboardView:
    """Assemble §2 a/b/c from a fresh mode=ro connection + liveness mtime +
    artifact files (read-only; never the orchestrator's rw connection; never
    writes)."""
    moment = now or utc_now()
    db = _open_ro(cfg)
    try:
        conn = db.read()
        cards: list[DecisionCard] = []
        for dr in fdb.pending_decisions(conn):
            try:
                cards.append(_build_card(cfg, conn, dr, moment))
            except Exception as exc:  # noqa: BLE001 — §2a per-card containment
                cards.append(_error_card(dr, exc))
        costs = _cost_buckets(cfg, conn)
        health = _build_health(cfg, conn, moment, costs)
        plan = _build_plan(conn, costs)
    finally:
        db.close()
    return DashboardView(generated_at=moment, cards=tuple(cards), health=health, plan=plan)


def _ledger_rows(
    conn: sqlite3.Connection, unit_level: str, unit_id: str
) -> tuple[AgentCostRow, ...]:
    """db.list_token_ledger rows -> AgentCostRow tuple (id order, F7)."""
    return tuple(
        AgentCostRow(
            ledger_id=int(row["id"]),
            role=row["role"],
            model=row["model"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            cost_usd=row["cost_usd"],
            estimated=bool(row["estimated"]),
            recorded_at=row["recorded_at"],
        )
        for row in fdb.list_token_ledger(conn, unit_level, unit_id)
    )


def build_costs_view(cfg: FactoryConfig, *, now: str | None = None) -> CostsView:
    """Assemble GET /costuri (§11.2, CCR-10) from a fresh mode=ro connection —
    same read path as build_view, never the orchestrator's rw connection, never
    writes. One PhaseCosts per phase with ledger rows (phase-level or stage-
    level); units without rows render nothing (§11.4 PENDING case)."""
    moment = now or utc_now()
    db = _open_ro(cfg)
    try:
        conn = db.read()
        units_with_rows = {
            (group["unit_level"], group["unit_id"]) for group in fdb.sum_token_cost(conn)
        }
        by_phase: dict[str, list[sqlite3.Row]] = {}
        for srow in conn.execute("SELECT * FROM stages ORDER BY created_at, id").fetchall():
            by_phase.setdefault(srow["phase_id"], []).append(srow)
        phases: list[PhaseCosts] = []
        for prow in _phase_dag_order(conn):
            phase_rows: tuple[AgentCostRow, ...] = (
                _ledger_rows(conn, Level.PHASE.value, prow["id"])
                if (Level.PHASE.value, prow["id"]) in units_with_rows
                else ()
            )
            stages: list[StageCosts] = []
            for srow in by_phase.get(prow["id"], ()):
                if (Level.STAGE.value, srow["id"]) not in units_with_rows:
                    continue  # no ledger rows -> no table, no anchor (§11.4)
                rows = _ledger_rows(conn, Level.STAGE.value, srow["id"])
                stages.append(
                    StageCosts(
                        stage_id=srow["id"],
                        name=srow["name"],
                        rows=rows,
                        total=_summary_from_rows(cfg, rows),
                    )
                )
            if not phase_rows and not stages:
                continue
            phases.append(
                PhaseCosts(
                    phase_id=prow["id"],
                    name=prow["name"],
                    phase_rows=phase_rows,
                    stages=tuple(stages),
                    # F3: the pair includes the phase's own PLANNING-agent rows.
                    total=_combine_summaries(
                        [_summary_from_rows(cfg, phase_rows), *(s.total for s in stages)]
                    ),
                )
            )
    finally:
        db.close()
    return CostsView(generated_at=moment, phases=tuple(phases))


# -------------------------------------------------------------------- render

# §10.2 visual system: CSS custom properties in :root are the SINGLE token
# source (change once, propagates); every rule below consumes tokens. The
# token-discipline test pins: no hex colors and no px spacing/size literals in
# declaration values outside :root (exempt: @media condition literals, bare 0,
# rgba() shadow values). --fs-base is 16px (mobile zoom-avoidance, §10.5);
# --tap-min is the 44px thumb target (S1).
_CSS = """
:root{--space-1:.25rem;--space-2:.5rem;--space-3:.8rem;--space-4:1.4rem;
  --c-bg:#f5f4f0;--c-card:#fff;--c-border:#d8d2c6;--c-accent:#155c8d;
  --c-ok:#1c6b35;--c-warn:#8a5a00;--c-err:#b3261e;--c-muted:#5a564d;
  --c-text:#1c1c1c;--c-tint:#edeae0;--radius:8px;--border-w:1px;--tap-min:44px;
  --fs-base:16px;--fs-small:.85rem;--fs-h1:1.25rem;--fs-h2:1.05rem;
  --shadow:0 1px 2px rgba(0,0,0,.06)}
*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;margin:0 auto;max-width:60rem;
  padding:0 var(--space-2) var(--space-4);background:var(--c-bg);
  color:var(--c-text);font-size:var(--fs-base)}
h1{font-size:var(--fs-h1);margin:var(--space-3) 0}
section,article.card{background:var(--c-card);
  border:var(--border-w) solid var(--c-border);border-radius:var(--radius);
  padding:var(--space-3);margin:var(--space-3) 0;box-shadow:var(--shadow)}
section>h2{font-size:var(--fs-h2);background:var(--c-tint);
  border-bottom:var(--border-w) solid var(--c-border);
  margin:calc(-1*var(--space-3)) calc(-1*var(--space-3)) var(--space-3);
  padding:var(--space-2) var(--space-3);
  border-radius:var(--radius) var(--radius) 0 0}
.bloc{border:var(--border-w) solid var(--c-border);border-radius:var(--radius);
  padding:var(--space-2);margin:0 0 var(--space-3);background:var(--c-card)}
.bloc h3{font-size:var(--fs-base);background:var(--c-tint);
  margin:calc(-1*var(--space-2)) calc(-1*var(--space-2)) var(--space-2);
  padding:var(--space-1) var(--space-2);
  border-bottom:var(--border-w) solid var(--c-border);
  border-radius:var(--radius) var(--radius) 0 0}
article.card.eroare{border-color:var(--c-err)}
article.card h3{margin:0 0 var(--space-2)}
pre{white-space:pre-wrap;word-break:break-word;background:var(--c-bg);
  border:var(--border-w) solid var(--c-border);border-radius:var(--radius);
  padding:var(--space-2);font-size:var(--fs-small);max-height:30rem;
  overflow-x:auto;overflow-y:auto}
.tabel{overflow-x:auto;margin:var(--space-2) 0}
table{width:100%;border-collapse:collapse;font-size:var(--fs-small)}
th,td{border-bottom:var(--border-w) solid var(--c-border);
  padding:var(--space-1) var(--space-2);text-align:left;vertical-align:top;
  word-break:break-word}
th{color:var(--c-muted);font-weight:600}
td.num,th.num{text-align:right}
tr.grup th{background:var(--c-tint);color:var(--c-text)}
.token{display:block;color:var(--c-muted);font-size:var(--fs-small)}
.chip{display:inline-block;border:var(--border-w) solid var(--c-border);
  border-radius:var(--radius);padding:0 var(--space-1);background:var(--c-card)}
.chip-accent{border-color:var(--c-accent);color:var(--c-accent)}
.chip-warn{border-color:var(--c-warn);color:var(--c-warn)}
.chip-err{border-color:var(--c-err);color:var(--c-err)}
.chip-ok{border-color:var(--c-ok);color:var(--c-ok)}
.chip-neutral{color:var(--c-muted)}
.opt{display:block;margin:var(--space-2) 0}
.opt button{display:block;width:100%;min-height:var(--tap-min);
  font-size:var(--fs-base);padding:var(--space-2) var(--space-3);
  border-radius:var(--radius);border:var(--border-w) solid var(--c-muted);
  background:var(--c-card);cursor:pointer;text-align:left}
.opt button:hover{background:var(--c-tint)}
.opt button.recomandat{border:calc(2*var(--border-w)) solid var(--c-accent)}
.badge{background:var(--c-ok);color:var(--c-card);border-radius:var(--radius);
  padding:0 var(--space-1);font-size:var(--fs-small);margin-left:var(--space-1)}
a.btn{display:block;width:100%;min-height:var(--tap-min);text-align:center;
  border:var(--border-w) solid var(--c-accent);border-radius:var(--radius);
  color:var(--c-accent);text-decoration:none;font-size:var(--fs-base);
  padding:var(--space-2) var(--space-3);margin:var(--space-2) 0}
a.banner{display:block;min-height:var(--tap-min);background:var(--c-warn);
  color:var(--c-card);border-radius:var(--radius);font-weight:700;
  padding:var(--space-2) var(--space-3);margin:var(--space-3) 0;
  text-decoration:none}
.rosu{color:var(--c-err);font-weight:700}
.meta{color:var(--c-muted);font-size:var(--fs-small)}
details{margin:var(--space-2) 0}
summary{cursor:pointer;font-weight:600;min-height:var(--tap-min);
  padding:var(--space-2) 0}
ul{margin:var(--space-1) 0;padding-left:var(--space-4)}
.tura{margin:var(--space-2) 0}
.tura.fondator pre{background:var(--c-tint)}
footer{color:var(--c-muted);font-size:var(--fs-small);
  margin-top:var(--space-3);border-top:var(--border-w) solid var(--c-border);
  padding-top:var(--space-2)}
textarea{width:100%;font-size:var(--fs-base);padding:var(--space-2);
  border:var(--border-w) solid var(--c-muted);border-radius:var(--radius);
  margin:0 0 var(--space-2)}
#mesaj-form button{display:block;min-height:var(--tap-min);
  font-size:var(--fs-base);padding:var(--space-2) var(--space-3);
  border-radius:var(--radius);border:var(--border-w) solid var(--c-muted);
  background:var(--c-card);cursor:pointer}
@media (max-width:720px){
  body{padding:0 var(--space-1) var(--space-3)}
  #mesaj-form button{width:100%}
}
"""

def _render_option_forms(card: DecisionCard, *, confirm: bool = False) -> str:
    """§2a option buttons, §10.2 shape: ≥ --tap-min full-width buttons; the
    „★ Recomandat” badge INSIDE the recommended button's label (accent border
    on that button), never a wrapping sibling; the internal token small-print
    on its own line inside the button (A-12)."""
    parts: list[str] = []
    if not card.options:
        parts.append(f"<p class='meta'>{esc(RO['no_buttons_notice'])}</p>")
        return "".join(parts)
    label = RO["session_confirm_label"] if confirm else RO["options_label"]
    parts.append(f"<p><strong>{esc(label)}</strong></p>")
    for token in card.options:
        recommended = card.recommended == token
        badge = (
            f" <span class='badge'>{esc(RO['recommended_badge'])}</span>"
            if recommended
            else ""
        )
        button_class = " class='recomandat'" if recommended else ""
        label_text = _label(token)
        parts.append(
            "<form class='opt' method='post'"
            f" action='/decision/{card.request_id}/answer'>"
            f"<input type='hidden' name='option' value='{esc(token)}'>"
            f"<button{button_class}>{esc(label_text)}{badge}"
            f"<span class='token'>({esc(token)})</span></button>"
            "</form>"
        )
    return "".join(parts)


def _render_card(card: DecisionCard) -> str:
    """§2a card, §10.1-S1 order: title + gate gloss + OPTION BUTTONS first
    (the options markup precedes the request <pre> — pinned), the full request
    collapsed in a zero-JS <details> below, mechanical links as a 2-col table
    (§10.3), and the session entry as a full-width LINK-BUTTON to the session
    page (S3/A-1: no free-text input on the auto-refreshing main page)."""
    unit_word = RO["stage_word"] if card.unit_level == "stage" else RO["phase_word"]
    title = (
        f"{RO['decision_word']} #{card.request_id} — {unit_word}:"
        f" {card.unit_name} ({card.unit_id})"
    )
    if card.error is not None:
        return (
            f"<article class='card eroare' id='decision/{card.request_id}'>"
            f"<h3>{esc(title)}</h3><p>{esc(card.error)}</p></article>"
        )
    links = "".join(
        f"<tr><td>{esc(_label(link.kind))}</td>"
        f"<td><a href='/artifact/{link.ref_id}'>{esc(link.filename)}</a></td></tr>"
        for link in card.artifact_links
    )
    links_block = (
        f"<p><strong>{esc(RO['artifacts_label'])}</strong></p>"
        "<div class='tabel'><table>"
        f"<tr><th>{esc(RO['col_kind'])}</th><th>{esc(RO['col_file'])}</th></tr>"
        f"{links}</table></div>"
        if links
        else ""
    )
    return (
        f"<article class='card' id='decision/{card.request_id}'>"
        f"<h3>{esc(title)}</h3>"
        f"<p class='meta'>{esc(_glossed(card.gate_kind))} · {esc(card.created_display)}</p>"
        f"{_render_option_forms(card)}"
        f"<a class='btn' href='/decision/{card.request_id}/session'>"
        f"{esc(RO['session_open'])}</a>"
        f"<details><summary>{esc(RO['request_summary'])}</summary>"
        f"<pre>{esc(card.request_text)}</pre></details>"
        f"{links_block}"
        "</article>"
    )


def _table(header_cells: str, body_rows: str) -> str:
    """One §10.3 table inside its overflow-x:auto wrapper (A-7)."""
    head = f"<tr>{header_cells}</tr>" if header_cells else ""
    return f"<div class='tabel'><table>{head}{body_rows}</table></div>"


def _bloc(heading: str, body: str, *, anchor: str | None = None) -> str:
    """One delimited health-strip sub-block: h3 header row + content (§10.2,
    finding 3 at both levels)."""
    anchor_attr = f" id='{anchor}'" if anchor else ""
    return f"<div class='bloc'{anchor_attr}><h3>{esc(heading)}</h3>{body}</div>"


def _render_escalations(view: DashboardView, cfg: FactoryConfig) -> str:
    """§10.4 'Escaladări deschise' (D-0026 gap b): anchor id='escaladari' is
    ALWAYS rendered — the scheduler's notify fragments must land on a real
    anchor even after every escalation is resolved. Per-row id='escalation/<id>';
    each row splits in two physical rows (A-7): unit+trigger+age, then the
    per-target line (architect reassurance / founder action + card link) and
    the optional „dosar” link. Empty set -> explicit notice; the newest
    resolved line gives cold-return closure (S2)."""
    health = view.health
    if health.escalations:
        rows: list[str] = []
        for row in health.escalations:
            age = _age_text(_age_seconds(row.created_at, view.generated_at))
            if row.target == "founder":
                detail = esc(RO["escalation_founder_action"])
                if row.decision_request_id is not None:
                    detail += (
                        f" — <a href='#decision/{row.decision_request_id}'>"
                        f"{esc(RO['escalation_decision_link'])}</a>"
                    )
            else:
                detail = esc(
                    RO["escalation_reassurance"].format(target=_glossed(row.target))
                )
            if row.payload_artifact_id is not None:
                detail += (
                    f" · <a href='/artifact/{row.payload_artifact_id}'>"
                    f"{esc(RO['escalation_dossier'])}</a>"
                )
            rows.append(
                f"<tr id='escalation/{row.escalation_id}'>"
                f"<td>{esc(row.unit_name)}"
                f"<span class='token'>({esc(row.unit_id)})</span></td>"
                f"<td>{esc(_glossed(row.trigger))}</td>"
                f"<td class='num'>{esc(age)}</td></tr>"
                f"<tr><td colspan='3' class='meta'>{detail}</td></tr>"
            )
        body = _table(
            f"<th>{esc(RO['col_unit'])}</th><th>{esc(RO['col_trigger'])}</th>"
            f"<th class='num'>{esc(RO['col_since'])}</th>",
            "".join(rows),
        )
    else:
        body = f"<p class='meta'>{esc(RO['escalations_none'])}</p>"
    if health.last_resolved is not None:
        res = health.last_resolved
        when = fmt_founder_ts(res.resolved_at, cfg.factory.timezone_founder)
        body += (
            f"<p class='meta'>{esc(RO['escalation_last_resolved'])}:"
            f" {esc(res.unit_name)} ({esc(res.unit_id)}) —"
            f" {esc(_glossed(res.resolution))}, {esc(when)}</p>"
        )
    return _bloc(RO["escalations_label"], body, anchor="escaladari")


def _render_health(view: DashboardView, cfg: FactoryConfig) -> str:
    """§2b health strip, §10.3 shape: each data group its own sub-block (h3 +
    table); 'Escaladări deschise' FIRST when non-empty (exceptional state
    outranks routine telemetry), LAST otherwise — the anchor always exists."""
    health = view.health
    blocks: list[str] = []

    escalations_block = _render_escalations(view, cfg)
    if health.escalations:
        blocks.append(escalations_block)

    stale_mark = (
        f" <span class='rosu'>{esc(RO['pulse_stale'])}</span>" if health.liveness_stale else ""
    )
    # CCR-11 (D-0037): one extra Puls line while a capacity hold is active —
    # the founder sees the factory paused itself and is probing, nothing more.
    hold_line = (
        f"<p class='rosu'>{esc(health.capacity_hold_display)}</p>"
        if health.capacity_hold_display
        else ""
    )
    blocks.append(
        _bloc(
            RO["pulse_label"],
            f"<p>{esc(health.liveness_display)}{stale_mark}</p>{hold_line}",
        )
    )

    if health.phases:
        phase_rows = "".join(
            f"<tr><td>{esc(ph.name)}<span class='token'>({esc(ph.phase_id)})</span></td>"
            f"<td>{_chip(ph.state)}</td>"
            f"<td class='num'>{ph.stages_done} {esc(RO['progress_of'])}"
            f" {ph.stages_total} {esc(RO['progress_done'])}</td></tr>"
            for ph in health.phases
        )
        blocks.append(
            _bloc(
                RO["phases_label"],
                _table(
                    f"<th>{esc(RO['col_phase'])}</th><th>{esc(RO['col_state'])}</th>"
                    f"<th class='num'>{esc(RO['col_progress'])}</th>",
                    phase_rows,
                ),
            )
        )

    if health.running_stages:
        # §11.2: the cost column (right-aligned, after tokens) carries the
        # exact/estimated pair; empty when the stage has no ledger rows.
        running_rows = "".join(
            f"<tr><td>{esc(st.name)}<span class='token'>({esc(st.stage_id)})</span></td>"
            f"<td>{_chip(st.state)}</td>"
            f"<td>{esc(_glossed(st.risk_class))}</td>"
            f"<td class='num'>{esc(_fmt_int(st.tokens))}</td>"
            f"<td class='num'>{esc(_fmt_cost_pair(st.cost))}</td></tr>"
            for st in health.running_stages
        )
        running_body = _table(
            f"<th>{esc(RO['col_stage'])}</th><th>{esc(RO['col_step'])}</th>"
            f"<th>{esc(RO['col_risk'])}</th><th class='num'>{esc(RO['col_tokens'])}</th>"
            f"<th class='num'>{esc(RO['col_cost'])}</th>",
            running_rows,
        )
    else:
        running_body = f"<p class='meta'>{esc(RO['queue_none_running'])}</p>"
    blocks.append(_bloc(RO["running_label"], running_body))

    blocks.append(
        _bloc(
            RO["queue_label"],
            _table(
                "",
                f"<tr><td>{esc(RO['queue_waiting'])}</td>"
                f"<td class='num'>{health.waiting_count}</td></tr>"
                f"<tr><td>{esc(RO['queue_runnable'])}</td>"
                f"<td class='num'>{health.runnable_count}</td></tr>",
            ),
        )
    )

    budget_rows = []
    for row in health.budgets:
        if row.budget:
            pct_cell = f"{int(round(100 * row.tokens / row.budget))}%"
            cap_cell = _fmt_int(row.budget)
        else:
            pct_cell = "—"
            cap_cell = "—"
        budget_rows.append(
            f"<tr><td>{esc(row.name)}<span class='token'>({esc(row.stage_id)} ·"
            f" {esc(_glossed(row.risk_class))})</span></td>"
            f"<td class='num'>{esc(_fmt_int(row.tokens))}</td>"
            f"<td class='num'>{esc(cap_cell)}</td>"
            f"<td class='num'>{esc(pct_cell)}</td></tr>"
        )
    estimated_part = (
        f" · {esc(RO['budget_estimated_part'])}:"
        f" {esc(_fmt_int(health.total_estimated_tokens))} ({esc(RO['estimated_mark'])})"
        if health.total_estimated_tokens
        else ""
    )
    # §11.2: factory lifetime cost as the PAIR (F11) + the «Astăzi» day line
    # (F5); both render only when any ledger row exists — the same condition
    # under which the §11 legend renders (no cost cells, no legend).
    costs_present = not health.factory_cost.empty
    cost_part = (
        f" · {esc(RO['budget_cost'])}: {esc(_fmt_cost_pair(health.factory_cost))}"
        if costs_present
        else ""
    )
    today_line = (
        f"<p class='meta'>{esc(RO['budget_today'])}:"
        f" {esc(_fmt_cost_pair(health.today_cost) or _fmt_usd(0.0))}</p>"
        if costs_present
        else ""
    )
    legend = f"<p class='meta'>{esc(RO['cost_legend'])}</p>" if costs_present else ""
    budget_table = (
        _table(
            f"<th>{esc(RO['col_stage'])}</th><th class='num'>{esc(RO['col_burn'])}</th>"
            f"<th class='num'>{esc(RO['col_cap'])}</th>"
            f"<th class='num'>{esc(RO['col_pct'])}</th>",
            "".join(budget_rows),
        )
        if budget_rows
        else ""
    )
    blocks.append(
        _bloc(
            RO["budget_label"],
            f"{budget_table}"
            f"<p class='meta'>{esc(RO['budget_total'])}:"
            f" {esc(_fmt_int(health.total_tokens))} {esc(RO['budget_tokens'])}"
            f"{estimated_part}{cost_part}</p>"
            f"{today_line}{legend}",
        )
    )

    if health.incident is not None:
        inc = health.incident
        when = fmt_founder_ts(inc.created_at, cfg.factory.timezone_founder)
        incident_body = _table(
            f"<th>{esc(RO['col_kind'])}</th><th>{esc(RO['col_unit'])}</th>"
            f"<th class='num'>{esc(RO['col_when'])}</th>",
            f"<tr><td>{esc(_glossed(inc.event_type))}</td>"
            f"<td>{esc(inc.unit_name)}"
            f"<span class='token'>({esc(inc.unit_id or 'factory')})</span></td>"
            f"<td class='num'>{esc(when)}</td></tr>",
        )
    else:
        incident_body = f"<p class='meta'>{esc(RO['incident_none'])}</p>"
    blocks.append(_bloc(RO["incident_label"], incident_body))

    if not health.escalations:
        blocks.append(escalations_block)

    return (
        f"<section id='acum'><h2>{esc(RO['section_now'])}</h2>{''.join(blocks)}</section>"
    )


def _stage_cost_cell(stage: PlanStage) -> str:
    """§11.2 plan-row cost cell: the pair + the «detalii →» link to the stage's
    /costuri anchor when ledger rows exist; EMPTY for a PENDING (no-ledger)
    stage — no cost row, no link (§11.4)."""
    if stage.cost.empty:
        return ""
    return (
        f"{esc(_fmt_cost_pair(stage.cost))} "
        f"<a href='/costuri#{stage.stage_id}'>{esc(RO['cost_details'])}</a>"
    )


def _render_plan(view: DashboardView) -> str:
    """§2c plan & history, §10.3 shape: per phase ONE table (etapă · stare/pas
    · clasă risc) with the Finalizate/În lucru/Planificate groups as table
    sections (header rows), not nested bullets."""
    parts = [f"<h2>{esc(RO['section_plan'])}</h2>"]
    for phase in view.plan:
        done_n = len(phase.done)
        total_n = done_n + len(phase.running) + len(phase.pending)
        # §11.2: the phase header carries the phase total PAIR (incl. the
        # phase's own unit_level='phase' ledger rows, F3).
        phase_cost = (
            f" — {esc(_fmt_cost_pair(phase.cost))}" if not phase.cost.empty else ""
        )
        parts.append(
            f"<h3>{esc(phase.name)} ({esc(phase.phase_id)}) — {_chip(phase.state)} —"
            f" {done_n} {esc(RO['progress_of'])}"
            f" {total_n} {esc(RO['progress_done'])}{phase_cost}</h3>"
        )
        if total_n == 0:
            link = (
                f" <a href='/artifact/{phase.plan_artifact_id}'>"
                f"{esc(RO['plan_artifact_link'])}</a>"
                if phase.plan_artifact_id is not None
                else ""
            )
            parts.append(f"<p class='meta'>{esc(RO['plan_no_stages'])}{link}</p>")
            continue
        rows: list[str] = []
        for label_key, group in (
            ("plan_done_group", phase.done),
            ("plan_running_group", phase.running),
            ("plan_pending_group", phase.pending),
        ):
            if not group:
                continue
            rows.append(f"<tr class='grup'><th colspan='4'>{esc(RO[label_key])}</th></tr>")
            rows.extend(
                f"<tr><td>{esc(st.name)}<span class='token'>({esc(st.stage_id)})</span></td>"
                f"<td>{_chip(st.state)}</td>"
                f"<td>{esc(_glossed(st.risk_class))}</td>"
                f"<td class='num'>{_stage_cost_cell(st)}</td></tr>"
                for st in group
            )
        parts.append(
            _table(
                f"<th>{esc(RO['col_stage'])}</th><th>{esc(RO['col_state'])}</th>"
                f"<th>{esc(RO['col_risk'])}</th><th class='num'>{esc(RO['col_cost'])}</th>",
                "".join(rows),
            )
        )
    parts.append(f"<footer>{esc(RO['plan_footer'])}</footer>")
    return f"<section id='plan'>{''.join(parts)}</section>"


def render_page(view: DashboardView, cfg: FactoryConfig) -> str:
    """The single server-rendered HTML page: three sections, meta-refresh, zero
    JS; all dynamic text via esc(). §10.1-S6: when decision cards exist, a
    one-line top banner anchor-links #decizii (the founder's to-do outranks the
    taller strip). The meta-refresh lives ONLY here — never on a page that
    renders a textarea (S3/A-1, pinned by test)."""
    refresh = cfg.founder_channel.dashboard.refresh_s
    cards: list[str] = []
    for card in view.cards:
        try:
            cards.append(_render_card(card))
        except Exception as exc:  # noqa: BLE001 — §2a per-card containment
            cards.append(_render_card(_error_card(_card_as_request(card), exc)))
    cards_html = "".join(cards) if cards else f"<p class='meta'>{esc(RO['decisions_none'])}</p>"
    count = len(view.cards)
    if count == 1:
        banner_text = RO["banner_decisions_one"]
    else:
        banner_text = f"{count} {RO['banner_decisions_many']}"
    banner = (
        f"<a class='banner' href='#decizii'>{esc(banner_text)}</a>" if count else ""
    )
    return (
        "<!doctype html><html lang='ro'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<meta http-equiv='refresh' content='{refresh}'>"
        f"<title>{esc(RO['page_title'])}</title>"
        f"<style>{_CSS}</style></head><body>"
        f"<h1>{esc(RO['page_heading'])}</h1>"
        f"{banner}"
        f"{_render_health(view, cfg)}"
        f"<section id='decizii'><h2>{esc(RO['section_decisions'])}</h2>{cards_html}</section>"
        f"{_render_plan(view)}"
        "</body></html>"
    )


def _render_cost_table(rows: tuple[AgentCostRow, ...], cfg: FactoryConfig) -> str:
    """One §11.2 per-agent table: rol (glossed) · model (glossed) · tokeni
    intrare · tokeni ieșire · cost — one row per ledger entry in ledger-id
    order (F7; recorded_at displayed as small print), a re-run role appearing
    twice is the truth of what was spent; the total row last renders the PAIR."""
    body: list[str] = []
    sum_in = 0
    sum_out = 0
    for row in rows:
        when = fmt_founder_ts(row.recorded_at, cfg.factory.timezone_founder)
        sum_in += row.tokens_in or 0
        sum_out += row.tokens_out or 0
        in_cell = _fmt_int(row.tokens_in) if row.tokens_in is not None else "—"
        out_cell = _fmt_int(row.tokens_out) if row.tokens_out is not None else "—"
        body.append(
            f"<tr><td>{esc(_glossed(row.role))}"
            f"<span class='token'>{esc(when)}</span></td>"
            f"<td>{esc(_glossed(row.model))}</td>"
            f"<td class='num'>{esc(in_cell)}</td>"
            f"<td class='num'>{esc(out_cell)}</td>"
            f"<td class='num'>{esc(_fmt_row_cost(cfg, row))}</td></tr>"
        )
    total = _summary_from_rows(cfg, rows)
    body.append(
        f"<tr class='grup'><th>{esc(RO['cost_total_row'])}</th><th></th>"
        f"<th class='num'>{esc(_fmt_int(sum_in))}</th>"
        f"<th class='num'>{esc(_fmt_int(sum_out))}</th>"
        f"<th class='num'>{esc(_fmt_cost_pair(total))}</th></tr>"
    )
    return _table(
        f"<th>{esc(RO['col_agent'])}</th><th>{esc(RO['col_model'])}</th>"
        f"<th class='num'>{esc(RO['col_tokens_in'])}</th>"
        f"<th class='num'>{esc(RO['col_tokens_out'])}</th>"
        f"<th class='num'>{esc(RO['col_cost'])}</th>",
        "".join(body),
    )


def render_costs_page(view: CostsView, cfg: FactoryConfig) -> str:
    """GET /costuri (§11.2, CCR-10): read-only, refresh-free, zero JS, NO
    inputs — the stateful reading surface the meta-refreshing main page must
    not carry (F2; the §10.5 session-page precedent — NO meta-refresh here,
    pinned by test). One bloc per phase: header with the total pair (incl. the
    phase's own rows, F3), the „agenți de fază” table for unit_level='phase'
    rows, then per stage (anchor id=<stage_id>, the «detalii →» landing) the
    per-agent table; the §11 legend renders iff any cost cell does."""
    parts: list[str] = [f"<h1>{esc(RO['costs_title'])}</h1>"]
    for phase in view.phases:
        blocs: list[str] = []
        if phase.phase_rows:
            blocs.append(
                _bloc(RO["costs_phase_agents"], _render_cost_table(phase.phase_rows, cfg))
            )
        blocs.extend(
            _bloc(
                f"{stage.name} ({stage.stage_id})",
                _render_cost_table(stage.rows, cfg),
                anchor=stage.stage_id,
            )
            for stage in phase.stages
        )
        parts.append(
            f"<section><h2>{esc(phase.name)} ({esc(phase.phase_id)}) —"
            f" {esc(_fmt_cost_pair(phase.total))}</h2>{''.join(blocs)}</section>"
        )
    if view.phases:
        parts.append(f"<p class='meta'>{esc(RO['cost_legend'])}</p>")
    else:
        parts.append(f"<p class='meta'>{esc(RO['costs_none'])}</p>")
    parts.append(f"<p><a href='/'>{esc(RO['back_to_dashboard'])}</a></p>")
    return (
        "<!doctype html><html lang='ro'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{esc(RO['costs_title'])} — {esc(RO['page_title'])}</title>"
        f"<style>{_CSS}</style></head><body>"
        f"{''.join(parts)}"
        "</body></html>"
    )


def _card_as_request(card: DecisionCard) -> DecisionRequest:
    """Adapter for the per-card containment path inside render."""
    return DecisionRequest(
        id=card.request_id,
        unit_level=card.unit_level,
        unit_id=card.unit_id,
        gate_kind=card.gate_kind,
        request_artifact_id=0,
        status="pending",
        answer=None,
        answer_artifact_id=None,
        created_at=card.created_at,
        alerted_at=None,
        answered_at=None,
    )


def render_session_page(
    snap: SessionSnapshot, view_card: DecisionCard, cfg: FactoryConfig, nonce: str
) -> str:
    """Decision-Session page: server-rendered transcript + confirm buttons + the
    one inline poll script (JS-free it still works via reload + form POST)."""
    unit_word = RO["stage_word"] if view_card.unit_level == "stage" else RO["phase_word"]
    title = (
        f"{RO['decision_word']} #{view_card.request_id} — {unit_word}:"
        f" {view_card.unit_name} ({view_card.unit_id})"
    )
    turns_html = []
    for turn in snap.turns:
        author = RO["founder_label"] if turn.author == "founder" else RO["agent_label"]
        css = "fondator" if turn.author == "founder" else "agent"
        when = fmt_founder_ts(turn.at, cfg.factory.timezone_founder)
        turns_html.append(
            f"<div class='tura {css}'><strong>{esc(author)}</strong>"
            f" <span class='meta'>{esc(when)}</span><pre>{esc(turn.text)}</pre></div>"
        )
    busy_note = (
        f"<p class='meta' id='stare-sesiune'>{esc(RO['session_busy'])}</p>"
        if snap.busy
        else "<p class='meta' id='stare-sesiune'></p>"
    )
    locked_note = (
        f"<p class='rosu'>{esc(snap.locked)}</p>" if snap.locked is not None else ""
    )
    # §10.5 (finding 1): a multi-line textarea, SESSION PAGE ONLY — the main
    # page's meta-refresh would destroy form state mid-composition (A-1). Both
    # ids stay pinned: the poll script locates #mesaj-form/#mesaj-text by id.
    input_form = (
        ""
        if snap.locked is not None
        else (
            f"<form id='mesaj-form' method='post'"
            f" action='/decision/{snap.request_id}/session/message'>"
            f"<textarea id='mesaj-text' name='text' rows='4'"
            f" placeholder='{esc(RO['session_message_placeholder'])}'></textarea>"
            f"<button>{esc(RO['session_send'])}</button></form>"
            f"<p class='meta'>{esc(RO['session_turns_left'])}: {snap.turns_left}</p>"
        )
    )
    poll_ms = int(cfg.founder_channel.decision_session.poll_s * 1000)
    last_n = snap.turns[-1].n if snap.turns else 0
    script = (
        f"<script nonce='{esc(nonce)}'>\n"
        "(function () {\n"
        f"  var after = {last_n};\n"
        f"  var busy = {'true' if snap.busy else 'false'};\n"
        f"  var pollMs = {poll_ms};\n"
        f"  var founderLabel = {json.dumps(RO['founder_label'])};\n"
        f"  var agentLabel = {json.dumps(RO['agent_label'])};\n"
        f"  var busyText = {json.dumps(RO['session_busy'])};\n"
        "  var list = document.getElementById('transcript');\n"
        "  var stare = document.getElementById('stare-sesiune');\n"
        "  var form = document.getElementById('mesaj-form');\n"
        "  var input = document.getElementById('mesaj-text');\n"
        "  function addTurn(t) {\n"
        "    var div = document.createElement('div');\n"
        "    div.className = 'tura ' + (t.author === 'founder' ? 'fondator' : 'agent');\n"
        "    var head = document.createElement('strong');\n"
        "    head.textContent = t.author === 'founder' ? founderLabel : agentLabel;\n"
        "    var body = document.createElement('pre');\n"
        "    body.textContent = t.text;\n"
        "    div.appendChild(head); div.appendChild(body);\n"
        "    list.appendChild(div);\n"
        "    if (t.n > after) { after = t.n; }\n"
        "  }\n"
        "  function poll() {\n"
        f"    fetch('/decision/{snap.request_id}/session/poll?after=' + after)\n"
        "      .then(function (r) { return r.json(); })\n"
        "      .then(function (s) {\n"
        "        s.turns.forEach(addTurn);\n"
        "        busy = s.busy;\n"
        "        stare.textContent = busy ? busyText : '';\n"
        "        if (busy) { setTimeout(poll, pollMs); }\n"
        "      })\n"
        "      .catch(function () { setTimeout(poll, pollMs); });\n"
        "  }\n"
        "  if (form) {\n"
        "    form.addEventListener('submit', function (ev) {\n"
        "      ev.preventDefault();\n"
        "      var text = input.value.trim();\n"
        "      if (!text) { return; }\n"
        "      var body = new URLSearchParams();\n"
        "      body.append('text', text);\n"
        f"      fetch('/decision/{snap.request_id}/session/message',"
        " {method: 'POST', body: body})\n"
        "        .then(function (r) {\n"
        "          if (r.ok) { input.value = ''; busy = true;"
        " stare.textContent = busyText; setTimeout(poll, pollMs); }\n"
        "          else { window.location.reload(); }\n"
        "        })\n"
        "        .catch(function () { window.location.reload(); });\n"
        "    });\n"
        "  }\n"
        "  if (busy) { setTimeout(poll, pollMs); }\n"
        "})();\n"
        "</script>"
    )
    return (
        "<!doctype html><html lang='ro'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{esc(RO['session_title'])} — {esc(title)}</title>"
        f"<style>{_CSS}</style></head><body>"
        f"<h1>{esc(RO['session_title'])}</h1>"
        f"<h3>{esc(title)}</h3>"
        f"<p class='meta'>{esc(_glossed(view_card.gate_kind))}</p>"
        f"<p>{esc(RO['session_intro'])}</p>"
        f"<div id='transcript'>{''.join(turns_html)}</div>"
        f"{busy_note}{locked_note}{input_form}"
        f"{_render_option_forms(view_card, confirm=True)}"
        f"<p><a href='/#decision/{snap.request_id}'>{esc(RO['session_back'])}</a></p>"
        f"{script}"
        "</body></html>"
    )


# -------------------------------------------------------- DecisionSessionManager


@dataclass
class _Session:
    """Loop-confined mutable state of one Decision Session (§4)."""

    request_id: int
    unit_level: str
    unit_id: str
    gate_kind: str
    transcript: Path
    turns: list[Turn] = field(default_factory=list)
    busy: bool = False
    locked: str | None = None
    last_session_id: str | None = None
    tokens_used: int = 0
    agent_turns: int = 0
    #: The ONE in-flight agent turn task (§4), kept so the §3.1a answer-path
    #: quiesce (D-0019) can cancel-and-await it; cleared by the turn's finally.
    turn_task: asyncio.Task[None] | None = None


_TRANSCRIPT_HEAD_RE = re.compile(
    r"^## (Fondator|Agent) — (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)$", re.MULTILINE
)


class DecisionSessionManager:
    """Loop-confined session state; transcript files under the factory-repo unit
    dir (§4). HTTP threads only marshal; all mutation happens on the loop."""

    def __init__(self, cfg: FactoryConfig, db: Database, runner: AgentRunner) -> None:
        """Loop-confined session state; transcript files under the factory-repo
        unit dir (§4)."""
        self._cfg = cfg
        self._db = db
        self._runner = runner
        self._sessions: dict[int, _Session] = {}
        #: Request ids whose answer is mid-flight (the §3.1a *answering* flag,
        #: D-0019): post_message refuses with an RO notice while set. Lifecycle
        #: is owned by DashboardServer.answer() (_begin_answer/_end_answer);
        #: keyed by request id, not session, so a session opened inside the
        #: answer's commit window is refused too.
        self._answering: set[int] = set()
        #: Session-turn TaskGroup, hosted inside DashboardServer.serve() (§4) —
        #: a supervisor restart cancels in-flight turns cleanly; manager state
        #: (this object) survives the restart.
        self._taskgroup: asyncio.TaskGroup | None = None

    # ------------------------------------------------------------- public

    async def post_message(self, request_id: int, text: str) -> SessionSnapshot:
        """Validate bounds/pending -> append founder turn to transcript file ->
        spawn ONE agent turn task; DashboardError when answering/busy/locked/
        exhausted (explicit, never silently queued)."""
        message = text.strip()
        if not message:
            raise DashboardError(RO["session_empty_message"])
        if request_id in self._answering:
            # §3.1a/§4 (D-0019): an answer for this request is mid-flight — the
            # answer semantically ends the session; nothing may write the
            # transcript after it. Zero writes here.
            raise DashboardError(RO["session_answering_refuse"])
        dr = _get_decision(self._db.read(), request_id)
        if dr is None:
            raise DashboardError(RO["session_unknown_request"])
        if dr.status != "pending":
            raise DashboardError(RO["session_request_answered"])
        session = self._session_for(dr)
        if session.busy:
            raise DashboardError(RO["session_busy_refuse"])
        if session.locked is not None:
            raise DashboardError(session.locked)
        ds_cfg = self._cfg.founder_channel.decision_session
        if session.agent_turns >= ds_cfg.max_turns:
            session.locked = RO["session_turns_exhausted"]
            raise DashboardError(session.locked)
        if session.tokens_used >= ds_cfg.budget_tokens:
            session.locked = RO["session_budget_exhausted"]
            raise DashboardError(session.locked)
        if self._taskgroup is None:
            raise DashboardError(RO["session_unavailable"])
        prompt, resume = self._build_prompt(session, dr, message)
        # Founder turn appended to the FILE first (crash-durable, §4), then memory.
        turn = Turn(n=len(session.turns) + 1, author="founder", text=message, at=utc_now())
        self._append_transcript(session, turn)
        session.turns.append(turn)
        session.busy = True
        session.turn_task = self._taskgroup.create_task(
            self._agent_turn(session, prompt, resume)
        )
        return self._snapshot_of(session)

    async def snapshot(self, request_id: int) -> SessionSnapshot:
        """Copy of session state for render/poll."""
        session = self._sessions.get(request_id)
        if session is None:
            dr = _get_decision(self._db.read(), request_id)
            if dr is None:
                raise DashboardError(RO["session_unknown_request"])
            session = self._session_for(dr)
        return self._snapshot_of(session)

    def transcript_path(self, request_id: int) -> Path | None:
        """Existing transcript file, else None (loop-confined: reads the
        orchestrator's own connection)."""
        session = self._sessions.get(request_id)
        if session is not None:
            return session.transcript if session.transcript.is_file() else None
        dr = _get_decision(self._db.read(), request_id)
        if dr is None:
            return None
        path = self._transcript_path_for(dr)
        return path if path.is_file() else None

    # ------------------------------------------------------------ internals

    def _set_taskgroup(self, tg: asyncio.TaskGroup | None) -> None:
        """Wired by DashboardServer.serve() — the turn tasks live (and die) with it."""
        self._taskgroup = tg

    async def _begin_answer(self, request_id: int) -> None:
        """§3.1a quiesce (race fix, D-0019) — wired by DashboardServer.answer()
        inside its lock, after step-1 validation: set the per-request answering
        flag (post_message refuses with an RO notice while set), then cancel
        any in-flight agent turn and AWAIT its termination — its try/finally
        appends the cancelled-turn notice, so only after this returns is the
        transcript byte-stable for the §3.2 commit. A turn left appending
        inside the commit window made register_artifact hash post-append bytes
        against the pre-append commit (a registered ref resolving nowhere)."""
        self._answering.add(request_id)
        session = self._sessions.get(request_id)
        task = session.turn_task if session is not None else None
        if task is not None and not task.done():
            task.cancel()
            # asyncio.wait never re-raises the turn's CancelledError (its
            # teardown is contained, §4) and still propagates OUR OWN
            # cancellation correctly.
            await asyncio.wait({task})

    def _end_answer(self, request_id: int) -> None:
        """Clear the §3.1a answering flag — DashboardServer.answer() calls this
        on EVERY exit path (success AND failure): a failed answer must not
        wedge the session read-only forever (D-0019)."""
        self._answering.discard(request_id)

    def _transcript_path_for(self, dr: DecisionRequest) -> Path:
        return (
            unit_artifact_dir(self._cfg.factory.home, Level(dr.unit_level), dr.unit_id)
            / f"decision-session-{dr.id}.md"
        )

    def _session_for(self, dr: DecisionRequest) -> _Session:
        assert dr.id is not None
        session = self._sessions.get(dr.id)
        if session is None:
            session = _Session(
                request_id=dr.id,
                unit_level=dr.unit_level,
                unit_id=dr.unit_id,
                gate_kind=dr.gate_kind,
                transcript=self._transcript_path_for(dr),
            )
            if session.transcript.is_file():
                # Orchestrator restart: rebuild the visible conversation from the
                # crash-durable transcript (§4); the CLI session id is lost — the
                # next turn re-feeds the transcript as context.
                session.turns = self._parse_transcript(session.transcript)
            self._sessions[dr.id] = session
        return session

    def _parse_transcript(self, path: Path) -> list[Turn]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        turns: list[Turn] = []
        matches = list(_TRANSCRIPT_HEAD_RE.finditer(text))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = text[match.end() : end].strip()
            author: Literal["founder", "agent"] = (
                "founder" if match.group(1) == "Fondator" else "agent"
            )
            turns.append(Turn(n=index + 1, author=author, text=body, at=match.group(2)))
        return turns

    def _append_transcript(self, session: _Session, turn: Turn) -> None:
        head = RO["founder_label"] if turn.author == "founder" else RO["agent_label"]
        session.transcript.parent.mkdir(parents=True, exist_ok=True)
        new_file = not session.transcript.exists()
        with open(session.transcript, "a", encoding="utf-8") as fh:
            if new_file:
                fh.write(
                    f"# {RO['session_title']} — {RO['decision_word']}"
                    f" #{session.request_id} ({session.unit_level}/{session.unit_id})\n"
                )
            fh.write(f"\n## {head} — {turn.at}\n\n{turn.text}\n")

    def _build_prompt(
        self, session: _Session, dr: DecisionRequest, message: str
    ) -> tuple[str, str | None]:
        """(prompt, resume_session). Later turns resume the CLI session with just
        the new message; the first turn (or a restart-lost session) carries the
        full frame: request artifact + unit/gate metadata + transcript so far."""
        if session.last_session_id is not None:
            return message, session.last_session_id
        conn = self._db.read()
        ref = _artifact_row(conn, dr.request_artifact_id)
        request_text = ""
        if ref is not None:
            try:
                request_text = _artifact_text(self._cfg, conn, ref)
            except DashboardError:
                request_text = "(cererea de decizie nu a putut fi citită)"
        unit_name = _unit_name(conn, dr.unit_level, dr.unit_id)
        unit_word = RO["stage_word"] if dr.unit_level == "stage" else RO["phase_word"]
        parts = [
            "Ești agentul de discuție pentru o decizie a fondatorului în fabrica"
            " SF-F5 (sesiune de decizie, doar conversație).",
            "Reguli stricte: discută opțiunile și compromisurile în termenii"
            " fondatorului (cost / viteză / risc / impact); NU pretinde că execuți"
            " ceva — nu poți modifica nimic; fondatorul confirmă DOAR prin"
            " butoanele din panou, nu prin acest chat.",
            f"Context: {unit_word} {unit_name} ({dr.unit_id}) —"
            f" {_glossed(dr.gate_kind)} — {RO['decision_word']} #{dr.id}.",
            "=== CEREREA DE DECIZIE ===",
            request_text,
            "=== SFÂRȘIT CERERE ===",
        ]
        prior = list(session.turns)
        if prior:
            parts.append("=== CONVERSAȚIA DE PÂNĂ ACUM (restaurată din transcript) ===")
            for turn in prior:
                head = RO["founder_label"] if turn.author == "founder" else RO["agent_label"]
                parts.append(f"[{head}] {turn.text}")
            parts.append("=== SFÂRȘIT CONVERSAȚIE ===")
        parts.append(f"Mesajul fondatorului: {message}")
        return "\n\n".join(parts), None

    def _session_cwd(self, request_id: int) -> Path:
        path = _resolve(self._cfg.factory.home, Path(".factory") / "sessions" / str(request_id))
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _snapshot_of(self, session: _Session) -> SessionSnapshot:
        ds_cfg = self._cfg.founder_channel.decision_session
        return SessionSnapshot(
            request_id=session.request_id,
            turns=tuple(session.turns),
            busy=session.busy,
            locked=session.locked,
            turns_left=max(0, ds_cfg.max_turns - session.agent_turns),
        )

    async def _agent_turn(self, session: _Session, prompt: str, resume: str | None) -> None:
        """One agent turn; teardown is try/finally — on failure AND cancellation
        the in-flight flag clears and a failed-turn notice lands in the
        transcript (§4: a cancelled turn must never wedge the session busy)."""
        ds_cfg = self._cfg.founder_channel.decision_session
        ok = False
        try:
            try:
                result = await self._runner.run_agent(
                    _SESSION_ROLE,
                    prompt,
                    unit_level=session.unit_level,
                    unit_id=session.unit_id,
                    cwd=self._session_cwd(session.request_id),
                    timeout_s=ds_cfg.turn_timeout_s,
                    resume_session=resume,
                )
            except Exception as exc:  # noqa: BLE001 — one bad turn never kills serve()
                print(
                    f"dashboard: decision-session turn failed (request"
                    f" {session.request_id}): {exc!r}",
                    file=sys.stderr,
                )
                result = None
            if result is not None:
                session.tokens_used += (result.tokens_in or 0) + (result.tokens_out or 0)
                if result.session_id:
                    session.last_session_id = result.session_id
                reply = result.result_text.strip()
                failed = (
                    result.timed_out
                    or result.killed
                    or (result.exit_code not in (0, None))
                    or not reply
                )
                if not failed:
                    session.agent_turns += 1
                    turn = Turn(
                        n=len(session.turns) + 1, author="agent", text=reply, at=utc_now()
                    )
                    self._append_transcript(session, turn)
                    session.turns.append(turn)
                    ok = True
                    if session.agent_turns >= ds_cfg.max_turns:
                        session.locked = RO["session_turns_exhausted"]
                    elif session.tokens_used >= ds_cfg.budget_tokens:
                        session.locked = RO["session_budget_exhausted"]
        finally:
            session.busy = False
            session.turn_task = None
            if not ok:
                notice = Turn(
                    n=len(session.turns) + 1,
                    author="agent",
                    text=RO["session_turn_failed"],
                    at=utc_now(),
                )
                try:
                    self._append_transcript(session, notice)
                except OSError:
                    pass  # the in-memory notice still renders
                session.turns.append(notice)


# ------------------------------------------------------------ DashboardServer

_CSP_BASE = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)


def _csp_session(nonce: str) -> str:
    # connect-src DOES fall back to default-src 'none' — without 'self' the §4
    # poll script is dead on arrival (silent CSP death; pinned by a unit test).
    return f"{_CSP_BASE}; script-src 'nonce-{nonce}'; connect-src 'self'"


class _DashboardHTTPServer(http.server.ThreadingHTTPServer):
    """Thread-per-connection (§1): daemon threads die with the process and the
    per-socket timeout bounds them; block_on_close=False so one hung client,
    legitimately alive up to read_timeout_s, never stalls shutdown/restart."""

    daemon_threads = True
    block_on_close = False
    dashboard: DashboardServer  # set right after construction in start()


_DECISION_ANSWER_RE = re.compile(r"^/decision/(\d+)/answer$")
_SESSION_PAGE_RE = re.compile(r"^/decision/(\d+)/session$")
_SESSION_POLL_RE = re.compile(r"^/decision/(\d+)/session/poll$")
_SESSION_MESSAGE_RE = re.compile(r"^/decision/(\d+)/session/message$")
_ARTIFACT_RE = re.compile(r"^/artifact/(\d+)$")


class _Handler(http.server.BaseHTTPRequestHandler):
    """Worker-thread HTTP handler. GETs open their own mode=ro connections;
    POSTs (and session snapshots) marshal onto the orchestrator loop with
    BOUNDED waits (§1) — expiry → 504 in Romanian."""

    server: _DashboardHTTPServer
    server_version = "SFF5Dashboard"
    sys_version = ""

    def setup(self) -> None:  # per-socket read timeout (§1 slow-client row)
        self.timeout = self.server.dashboard._cfg.founder_channel.dashboard.read_timeout_s
        super().setup()

    # ------------------------------------------------------------ responses

    def _send(
        self,
        status: int,
        body: str,
        *,
        content_type: str = "text/html; charset=utf-8",
        csp: str = _CSP_BASE,
        location: str | None = None,
    ) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        if location is not None:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(data)

    def _page(self, status: int, message: str, *, extra_html: str = "") -> None:
        body = (
            "<!doctype html><html lang='ro'><head><meta charset='utf-8'>"
            f"<title>{esc(RO['page_title'])}</title><style>{_CSS}</style></head>"
            f"<body><p>{esc(message)}</p>{extra_html}"
            f"<p><a href='/'>{esc(RO['back_to_dashboard'])}</a></p></body></html>"
        )
        self._send(status, body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        sys.stderr.write(
            f"dashboard: {self.address_string()} {format % args}\n"
        )

    # ------------------------------------------------------------- marshal

    def _marshal(self, coro: Coroutine, timeout_s: float) -> tuple[bool, object]:
        """run_coroutine_threadsafe with a BOUNDED wait (§1); DashboardError →
        409 RO; timeout → 504 RO; no loop yet → 503 RO. Returns (ok, value)."""
        loop = self.server.dashboard._loop
        if loop is None:
            self._page(503, RO["loop_unavailable"])
            coro.close()
            return False, None
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return True, future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            self._page(504, RO["answer_timeout"])
            return False, None
        except DashboardError as exc:
            self._page(409, str(exc))
            return False, None

    # -------------------------------------------------------------- routes

    def do_GET(self) -> None:  # noqa: N802 — http.server contract
        try:
            self._route_get()
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 — explicit 500, never a blank 200
            print(
                "dashboard: GET handler error: "
                + "".join(traceback.format_exception(exc)),
                file=sys.stderr,
            )
            try:
                self._page(500, RO["server_error"])
            except OSError:
                pass

    def do_POST(self) -> None:  # noqa: N802 — http.server contract
        try:
            self._route_post()
        except BrokenPipeError:
            pass
        except GitError as exc:
            print(f"dashboard: git failure on POST: {exc}", file=sys.stderr)
            self._page(500, RO["answer_error"])
        except Exception as exc:  # noqa: BLE001
            print(
                "dashboard: POST handler error: "
                + "".join(traceback.format_exception(exc)),
                file=sys.stderr,
            )
            try:
                self._page(500, RO["server_error"])
            except OSError:
                pass

    def _route_get(self) -> None:
        dashboard = self.server.dashboard
        cfg = dashboard._cfg
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/":
            view = build_view(cfg)
            self._send(200, render_page(view, cfg))
            return
        if path == "/costuri":
            # §11.2 (CCR-10): read-only, refresh-free, same mode=ro read path.
            self._send(200, render_costs_page(build_costs_view(cfg), cfg))
            return
        if match := _ARTIFACT_RE.match(path):
            self._artifact_page(cfg, int(match.group(1)))
            return
        if match := _SESSION_PAGE_RE.match(path):
            self._session_page(cfg, int(match.group(1)))
            return
        if match := _SESSION_POLL_RE.match(path):
            query = urllib.parse.parse_qs(parsed.query)
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError:
                after = 0
            self._session_poll(cfg, int(match.group(1)), after)
            return
        self._page(404, RO["not_found"])

    def _artifact_page(self, cfg: FactoryConfig, ref_id: int) -> None:
        db = _open_ro(cfg)
        try:
            conn = db.read()
            row = _artifact_row(conn, ref_id)
            if row is None:
                self._page(404, RO["artifact_missing"])
                return
            try:
                text = _artifact_text(cfg, conn, row)
            except DashboardError:
                self._page(404, RO["artifact_missing"])
                return
            kind_label = GLOSS.get(row["kind"], f"{row['kind']} ({RO['missing_gloss']})")
            body = (
                "<!doctype html><html lang='ro'><head><meta charset='utf-8'>"
                f"<title>{esc(RO['artifact_title'])} #{ref_id}</title>"
                f"<style>{_CSS}</style></head><body>"
                f"<h1>{esc(RO['artifact_title'])} #{ref_id} — {esc(kind_label)}"
                f" — {esc(Path(row['path']).name)}</h1>"
                f"<pre>{esc(text)}</pre>"
                f"<p><a href='/'>{esc(RO['back_to_dashboard'])}</a></p></body></html>"
            )
            self._send(200, body)
        finally:
            db.close()

    def _card_for(self, cfg: FactoryConfig, request_id: int) -> DecisionCard | None:
        db = _open_ro(cfg)
        try:
            conn = db.read()
            dr = _get_decision(conn, request_id)
            if dr is None:
                return None
            try:
                return _build_card(cfg, conn, dr, utc_now())
            except Exception as exc:  # noqa: BLE001 — §2a containment
                return _error_card(dr, exc)
        finally:
            db.close()

    def _session_page(self, cfg: FactoryConfig, request_id: int) -> None:
        card = self._card_for(cfg, request_id)
        if card is None:
            self._page(404, RO["answer_unknown"])
            return
        dashboard = self.server.dashboard
        ok, snap = self._marshal(
            dashboard._sessions.snapshot(request_id),
            cfg.founder_channel.dashboard.read_timeout_s,
        )
        if not ok:
            return
        nonce = secrets.token_urlsafe(16)
        self._send(
            200, render_session_page(snap, card, cfg, nonce), csp=_csp_session(nonce)
        )

    def _session_poll(self, cfg: FactoryConfig, request_id: int, after: int) -> None:
        dashboard = self.server.dashboard
        ok, snap = self._marshal(
            dashboard._sessions.snapshot(request_id),
            cfg.founder_channel.dashboard.read_timeout_s,
        )
        if not ok:
            return
        payload = {
            "turns": [
                {"n": t.n, "author": t.author, "text": t.text, "at": t.at}
                for t in snap.turns
                if t.n > after
            ],
            "busy": snap.busy,
            "locked": snap.locked,
            "turns_left": snap.turns_left,
        }
        self._send(
            200,
            json.dumps(payload, ensure_ascii=False),
            content_type="application/json; charset=utf-8",
        )

    def _read_form(self) -> dict[str, str] | None:
        cfg = self.server.dashboard._cfg
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        # Reject negatives too: rfile.read(-1) reads until EOF — an unbounded
        # body read pinning one daemon thread while the client keeps streaming
        # (read_timeout_s bounds stalls, not a steadily-fed stream).
        if length < 0 or length > cfg.founder_channel.dashboard.max_request_bytes:
            self._page(413, RO["request_too_large"])
            return None
        raw = self.rfile.read(length) if length > 0 else b""
        pairs = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"))
        return {key: values[0] for key, values in pairs.items() if values}

    def _route_post(self) -> None:
        dashboard = self.server.dashboard
        cfg = dashboard._cfg
        path = urllib.parse.urlsplit(self.path).path
        if match := _DECISION_ANSWER_RE.match(path):
            request_id = int(match.group(1))
            form = self._read_form()
            if form is None:
                return
            option = form.get("option", "")
            ok, result = self._marshal(
                dashboard.answer(request_id, option, via="dashboard"),
                cfg.founder_channel.dashboard.answer_timeout_s,
            )
            if not ok:
                return
            self._answer_response(cfg, result)
            return
        if match := _SESSION_MESSAGE_RE.match(path):
            request_id = int(match.group(1))
            form = self._read_form()
            if form is None:
                return
            text = form.get("text", "")
            ok, _snap = self._marshal(
                dashboard._sessions.post_message(request_id, text),
                cfg.founder_channel.dashboard.answer_timeout_s,
            )
            if not ok:
                return
            self._send(
                303,
                "",
                location=f"/decision/{request_id}/session",
            )
            return
        self._page(404, RO["not_found"])

    def _answer_response(self, cfg: FactoryConfig, result: AnswerResult) -> None:
        if result.outcome is AnswerOutcome.ANSWERED:
            # §10.1-S1 (A-3): an explicit confirmation page — the old 303 landed
            # on an anchor that no longer exists (the card left pending) with
            # zero acknowledgment; _page() carries the link back to '/'.
            self._page(200, RO["answered_ok"])
            return
        if result.outcome is AnswerOutcome.ALREADY_ANSWERED:
            self._page(200, RO["answered_already"])
            return
        if result.outcome is AnswerOutcome.UNKNOWN_REQUEST:
            self._page(404, RO["answer_unknown"])
            return
        # INVALID_OPTION: list the valid options in Romanian (zero writes done).
        db = _open_ro(cfg)
        try:
            dr = _get_decision(db.read(), result.request_id)
        finally:
            db.close()
        options = GATE_ANSWERS.get((dr.unit_level, dr.gate_kind), ()) if dr else ()
        listing = "".join(f"<li>{esc(_glossed(token))}</li>" for token in options)
        extra = f"<ul>{listing}</ul>" if listing else f"<p>{esc(RO['no_buttons_notice'])}</p>"
        self._page(400, RO["answer_invalid_option"], extra_html=extra)


class DashboardServer:
    """The orchestrator's founder surface (design §6)."""

    def __init__(
        self, cfg: FactoryConfig, db: Database, runner: AgentRunner, notify: NtfyPublisher
    ) -> None:
        """db = the orchestrator's OWN Database (the write path runs on the
        loop); GET handlers open their own mode=ro connections (§1)."""
        self._cfg = cfg
        self._db = db
        self._runner = runner
        self._notify = notify
        self._sessions = DecisionSessionManager(cfg, db, runner)
        self._lock = asyncio.Lock()
        self._server: _DashboardHTTPServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        #: (host, port) after a successful bind, None before — the readiness
        #: signal (§6); integration tests bind 127.0.0.1:0 and read the real
        #: ephemeral port here.
        self.bound_address: tuple[str, int] | None = None

    # ----------------------------------------------------------------- bind

    def start(self) -> None:
        """Resolve bind host + bind (host, dashboard.port) — no traffic yet;
        sets bound_address. FactoryError on resolve/bind failure. cli run calls
        this EAGERLY before run_forever (§1): a first-bind failure aborts
        orchestrator start in the foreground, never inside the restart loop."""
        if self._server is not None:
            return  # already bound (serve() re-checks; cli's eager call is first)
        host = resolve_bind_host(self._cfg)
        port = self._cfg.founder_channel.dashboard.port
        try:
            server = _DashboardHTTPServer((host, port), _Handler)
        except OSError as exc:
            raise FactoryError(
                f"dashboard bind failed on {host}:{port}: {exc} — the dashboard is "
                "the founder's only decision surface; orchestrator start aborts (D-0017)"
            ) from exc
        server.dashboard = self
        self._server = server
        self.bound_address = (server.server_address[0], server.server_address[1])

    async def serve(self) -> None:
        """start() if not yet bound (supervised restarts re-run it, re-resolving
        the tailscale IP), run the ThreadingHTTPServer on a daemon thread, hold
        until cancelled; on CancelledError run shutdown()/server_close() via
        asyncio.to_thread (never block the loop); hosts the §4 session-turn
        TaskGroup, so cancellation tears down in-flight turns with it."""
        if self._server is None:
            self.start()
        server = self._server
        assert server is not None
        self._loop = asyncio.get_running_loop()
        thread = threading.Thread(
            target=server.serve_forever, name="sf-dashboard-http", daemon=True
        )
        thread.start()
        try:
            async with asyncio.TaskGroup() as tg:
                self._sessions._set_taskgroup(tg)
                try:
                    await asyncio.Event().wait()  # hold until cancelled
                finally:
                    self._sessions._set_taskgroup(None)
        finally:
            self._loop = None
            self._server = None
            self.bound_address = None
            await asyncio.to_thread(self._teardown, server)

    @staticmethod
    def _teardown(server: _DashboardHTTPServer) -> None:
        server.shutdown()
        server.server_close()

    # ----------------------------------------------------------- write path

    async def answer(self, request_id: int, option: str, *, via: str) -> AnswerResult:
        """THE single write path (§3): loop-confined, lock-serialized; validate
        -> quiesce the session (§3.1a, D-0019: answering flag + cancel-and-await
        any in-flight turn, transcript byte-stable before the commit) -> answer
        artifact (+ transcript) committed to the factory repo (D-0015 order;
        commit_paths None -> rev-parse HEAD) -> ONE sync tx
        (register_artifact + answer_decision + 'decision_answered' event,
        actor='founder'). Already answered = explicit no-op — incl. a lost
        cross-process race vs cli decide, caught at the tx's pending guard and
        mapped to ALREADY_ANSWERED (§3.3), never a 500."""
        async with self._lock:
            # Step 1 — validate (loop-side read; zero writes on every miss).
            conn = self._db.read()
            dr = _get_decision(conn, request_id)
            if dr is None:
                return AnswerResult(AnswerOutcome.UNKNOWN_REQUEST, request_id, None, None)
            if dr.status != "pending":
                return AnswerResult(
                    AnswerOutcome.ALREADY_ANSWERED, request_id, dr.answer, None
                )
            allowed = GATE_ANSWERS.get((dr.unit_level, dr.gate_kind), ())
            if option not in allowed:
                return AnswerResult(AnswerOutcome.INVALID_OPTION, request_id, None, None)

            # Step 1a — quiesce the session FIRST (race fix, D-0019): set the
            # per-request answering flag (post_message refuses with an RO
            # notice while set), cancel any in-flight agent turn and AWAIT its
            # termination — its try/finally appends the cancelled-turn notice,
            # so the transcript is byte-stable BEFORE the step-2 commit window
            # opens; the answer semantically ends the session, nothing may
            # write the transcript after it. The finally clears the flag on
            # EVERY exit path (success AND failure): a failed answer must not
            # wedge the session.
            try:
                await self._sessions._begin_answer(request_id)

                # Step 2 — artifact first, committed (D-0015 order, mirrored
                # exactly).
                home = self._cfg.factory.home
                unit_dir = unit_artifact_dir(home, Level(dr.unit_level), dr.unit_id)
                unit_dir.mkdir(parents=True, exist_ok=True)
                answered_at = utc_now()
                artifact_path = unit_dir / f"decision-answer-{dr.id}.md"
                artifact_path.write_text(
                    self._render_answer_artifact(dr, option, answered_at, via),
                    encoding="utf-8",
                )
                to_commit = [artifact_path]
                transcript = self._sessions.transcript_path(request_id)
                if transcript is not None:
                    to_commit.append(transcript)
                sha = await commit_paths(
                    home,
                    to_commit,
                    f"decision {dr.id}: answer recorded via {via}",
                    trailers={"Factory-Unit": f"{dr.unit_level}/{dr.unit_id}"},
                )
                if sha is None:
                    # Byte-identical retry after a commit-succeeded/tx-failed
                    # crash: register with the commit that already contains the
                    # bytes — same contract as cli decide (§3.2), never a NULL
                    # factory ref.
                    code, out, err = await run_git("rev-parse", "HEAD", cwd=home)
                    if code != 0:
                        raise GitError(
                            f"git rev-parse HEAD failed in {home}: {(err or out).strip()}"
                        )
                    sha = out.strip()

                # Step 3 — ONE synchronous transaction (§7: no await inside).
                try:
                    with self._db.transaction() as tx:
                        ref = register_artifact(
                            tx,
                            unit_level=dr.unit_level,
                            unit_id=dr.unit_id,
                            kind="decision_answer",
                            repo="factory",
                            repo_root=home,
                            path=artifact_path,
                            git_commit=sha,
                        )
                        payload: dict = {
                            "request_id": request_id,
                            "answer": option,
                            "via": via,
                        }
                        if transcript is not None:
                            tref = register_artifact(
                                tx,
                                unit_level=dr.unit_level,
                                unit_id=dr.unit_id,
                                kind="transcript",
                                repo="factory",
                                repo_root=home,
                                path=transcript,
                                git_commit=sha,
                            )
                            payload["transcript_artifact_id"] = tref.id
                        fdb.answer_decision(tx, request_id, option, ref.id)
                        fdb.insert_event(
                            tx,
                            unit_level=dr.unit_level,
                            unit_id=dr.unit_id,
                            event_type="decision_answered",
                            actor="founder",
                            payload=payload,
                        )
                except FactoryError:
                    # §3.3 lost cross-process race: a cli decide completed
                    # inside the step-2 await window — its answer hit the
                    # frozen WHERE status='pending' guard first. Re-validate:
                    # answered by someone else = explicit no-op; anything else
                    # is a real bug.
                    fresh = _get_decision(self._db.read(), request_id)
                    if fresh is not None and fresh.status != "pending":
                        return AnswerResult(
                            AnswerOutcome.ALREADY_ANSWERED, request_id, fresh.answer, None
                        )
                    raise
                return AnswerResult(
                    AnswerOutcome.ANSWERED, request_id, option, str(artifact_path)
                )
            finally:
                self._sessions._end_answer(request_id)

    @staticmethod
    def _render_answer_artifact(
        dr: DecisionRequest, option: str, answered_at: str, via: str
    ) -> str:
        """Same renderer contract as cli decide's decision-answer artifact."""
        return (
            f"# Decision answer — request {dr.id}\n\n"
            f"- request_id: {dr.id}\n"
            f"- unit: {dr.unit_level}/{dr.unit_id}\n"
            f"- gate_kind: {dr.gate_kind}\n"
            f"- request_artifact_id: {dr.request_artifact_id}\n"
            f"- answer: {option}\n"
            f"- answered_at: {answered_at}\n"
            f"- answered_via: {via}\n"
            f"- actor: founder\n"
        )
