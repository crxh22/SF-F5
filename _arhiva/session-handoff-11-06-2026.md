# Session handoff — written 11-06-2026 23:35 EEST, at the Etapa-5 boundary

**For the next Main-Architect session.** Read this first; it is a POINTER document (Doctrine §9) —
the authoritative history is the decision log, the designs, and git. Auto-memory gives you the
founder/infra/project profile; the canon arrives via the launcher's system-prompt injection.

## Where everything lives

| What | Where |
|---|---|
| Binding scope (MVP DoD) | `_FRAMEWORK_MVP_DoD.md` (v3) governed by `00 - DOCTRINA.md` |
| Authoritative history | `docs/decision-log.md` — **D-0001…D-0023**, read it END TO END (it is the spine) |
| Control-plane design | `docs/design/control-plane-design.md` v1.4 (CCR-1..3 in its Review log) |
| Dashboard design | `docs/design/dashboard-design.md` v1.1 + CCR-4 + §3.1a (D-0019) |
| Live-run procedure | `docs/runbooks/first-live-run.md` (tmux ritual, watchdog coupling, billing-403 signature) |
| B8 fixture (permanent) | `tests/fixtures/b8-seeded-conflict/` |
| Kickoff artifacts | `docs/environment-audit-10-06-2026.md`, `docs/decision-request-kickoff-10-06-2026.md` |
| Founder channel | ntfy topic `claude-artur-md-hello`; dashboard `http://100.69.221.108:8377/` (tailnet; phone `galaxy-s24-ultra` on tailnet) |

## State at handoff

- **Code COMPLETE and green**: control plane (13 modules) + dashboard; ~640 tests, ruff clean; all
  committed through `bf4269f`. Working tree clean except founder-owned untracked noise (none known).
- **Criteria**: B7 ✓ (integration suite), B8 ✓ **live** (D-0022/D-0023 — real codex catch, fixture
  preserved), B9 fallback ✓ / real-ambiguous-case pending first real CP-1 consultation.
  **A1–A6 + C10 = Etapa 5 work on the real ERP.**
- **Demo environment retired** (D-0023): orchestrator STOPPED, watchdog DISARMED
  (`sf-factory-watchdog.timer` installed, disabled — arm it only while an orchestrator runs),
  demo DBs in `.factory/archive/`, synthetic configs in `.factory/` (gitignored).
- **Backlog with triggers**: D-0021 needs_architect list (no phase-creation command — Etapa 5
  must define the sanctioned path; dashboard_link hostname; SIGTERM; resume dashboard-less;
  watchdog config path; OPEN-2 test_command) + D-0022 item 3 (multi-project DB mapping —
  fresh-DB-per-project is the MVP posture).

## Etapa 5 — what comes next (the reason for this handoff)

1. **Founder finalized the ERP documentation on 11-06-2026** (`~/projects/ERP-start`) — re-read it
   FRESH (do not rely on the 10-06 recon summary; it predates his changes).
2. **Intake interview with the founder** (~1–2h, his window TBD): per DoD §3.3/§14 it stays
   interactive. Agenda to prepare: Foundation phase scope confirmation (core schema: contragenți,
   nomenclature, document primitives, auth/access per DoD §3.2 + the ADRs in ERP-start);
   `projects.erp.test_command` (OPEN-2, with the Django stack from ADR-0002); the sanctioned
   phase-creation path (backlog #1 — intake output should land as the first real PROJECT.md +
   phase plan, and the operator/architect path for seeding it gets defined HERE); workspace
   bootstrap (`/home/artur/projects/erp-workspace` per factory.config.yaml — git init + Django
   skeleton decisions are part of Foundation, not pre-work).
3. **Architect role prompts** (OPEN-D1): author them at intake time, with the
   `Recomandare: <option-token>` marker contract and config-key references (DoD §11), founder
   protocol for founder-facing outputs (D-0009 canon classes already wired in config).
4. **A-criteria runs** on real ERP stages per the runbook (production `factory.config.yaml`,
   FRESH DB, real model routes), then **C10** (cross-phase contracts + phase DAG with ≥2 phases
   parallelizable after Foundation — planning artifact, execution out of MVP scope).

## Working mode (proven over D-0008 bootstrap — keep it)

- Design → 2 adversarial reviewers → revise → architect ratifies (D-entries) → single-builder or
  waved builds with **enumerated deltas** → non-executor verification → commit. CCR discipline is
  real and has fired 4× — agents STOP at lane boundaries; only the architect writes
  `docs/decision-log.md`.
- Ops gotchas, each learned the hard way: tmux sessions created WITH a command die with it;
  disarm watchdog BEFORE any planned stop; seed phases only while the orchestrator is stopped
  (sole writer); absolute paths and no `cd`/error-silencing in compound shell commands; the
  founder launcher `./claude_canon.sh` re-attaches via tmux `-A` (exit the old claude first for a
  fresh conversation) and passes `--effort max`.
- Founder protocol: Romanian, plain, glossed ids, options-with-recommendation; he is ops-novice
  but architecturally sharp; front-load decisions, run long, page only at real gates.
