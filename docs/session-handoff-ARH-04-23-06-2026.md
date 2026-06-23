# Session handoff — ARH-04 → ARH-05, 23-06-2026

**For the Main-Architect successor.** POINTER doc (Doctrine §9). ARH-04 authored the new stage structure,
got it dual-audited + founder-approved, built the landing wiring, and the founder just authorized the
**re-seed + an L0 proving build**. Your job = EXECUTE that. Durable context: memory
**[[erp-rebuild-structure-authored-23-06]]** + **[[erp-rebuild-redesign-22-06]]** +
**[[founder-model-effort-policy]]** — READ FIRST.

> ## ⛔ ABSOLUTE RULES (carry forward)
> 1. **NEVER `pkill -f` / `pgrep -f`** (or any broad kill) with a pattern that can appear in a session's
>    launch prompt — kills ALL architect sessions at once. Stop a task ONLY by EXACT PID
>    (`/proc/<pid>/cmdline` first) or EXACT tmux name. ([[never-prompt-matching-pkill]])
> 2. **NEVER kill/exit a PREDECESSOR session (ARH-03 `arh-03`, ARH-04 `arh-04`).** An architect kill DROPS
>    the predecessor's claude.ai/code dashboard history (founder 22-06). Leave them attached + idle; the
>    FOUNDER retires them. (`session-launch-protocol.md §B`)

## NAMING — you are ARH-05
`ARH - 05` (phone RC label; tmux slug `arh-05`). Runbooks: `docs/runbooks/session-succession.md` +
`session-launch-protocol.md`.

## FIRST duties (in order)
1. **Claim the marker.** Your id = newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/`
   containing a UNIQUE phrase from YOUR launch prompt (NOT mtime — predecessors' transcripts also contain
   it; the LIVE one GROWS; verify with a phrase + that the file is growing / has your just-run command).
   Write it into `~/.claude/sf-architect-session`, replacing `15577713-5e65-4f8e-b226-bb81f3844eb8`.
2. **Verify RC** (`ARH - 05` on the founder's phone). **The founder is ONLINE right now** — he just said
   "1" (go). Do NOT go silent until RC confirmed; he is reachable on ARH-04's live RC meanwhile.
3. **Do NOT start the session monitor YET** — the factory is STOPPED. Start it (and grep the escalation
   events: `escalation_opened_notice` / `escalation_bumped` / `escalation_stuck_resolved`; recognize the
   `[arhitect]` ntfy prefix) ONLY when you RESTART the factory at re-seed (`session-succession.md §monitor`).
4. **Do NOT kill ARH-03 or ARH-04.** Both stay idle/readable.

## 🏭 STATE (verify fresh)
Factory **STOPPED** (orchestrator down, monitor OFF). The **structure-authoring phase is DONE + committed**
(5 ARH-04 commits on SF-F5 main, tree clean):
- `00c857f` — builder routing fix (backend structural/critical → codex; was wrongly opus).
- `e1adf51` — the 10-layer / **40-stage** structure under `docs/projects/erp/rebuild/`.
- `7f2cb3a` — reseed playbook §8 + founder summary.
- `90fb3dd` — **Option A landing wiring** (`prefrozen_phase_plans` adoption + sha-verify in `_step_planning`).
- `c8aeefb` — founder 23-06 decisions applied in docs.

**Capacity (founder rule [[founder-model-effort-policy]]):** USE it, never "wait for reset" to conserve.
At ARH-04 handoff: **weekly ~13%, resets 25-06 ~03:00 UTC**; 5h fresh. The 40-stage build CANNOT finish on
13% — L0 prove is cheap; **hold the big build until after the 25-06 reset** (a pacing call, not conservation).

## 🎯 YOUR MANDATE — the founder authorized **"1"** = re-seed NOW + prove L0, rest after reset
Execute the re-seed, then prove the smallest layer (`l0-shell`, 2 frontend stages) end-to-end to validate the
WHOLE new mechanism (Option A plan-adoption → contracts → build → dual-audit → merge), THEN drive the
remaining layers after the weekly reset.

**The re-seed checklist is `docs/design/erp-rebuild-reseed-playbook.md` §8b — follow it.** Key steps + gotchas:
- Export the **#104 (ASM-006)** contest dossier (`/artifact/1200`) + decision #26 to a carry-forward doc
  BEFORE archiving the DB (record #26 "superseded by L7 `cont-quote-land`").
- Backup `factory.db`(+wal+shm); Strategy A archive + `sf-factory init` fresh; re-insert `runtime_settings`
  VERBATIM (`drain.manual=true`, `max_parallel_agents=2`, `budget.critical=500000000`, `budget.routine=80000000`).
- `sf-factory seed-phases docs/projects/erp/rebuild/macro-plan.json --dry-run` → verify 10 phases + 9 edges
  → seed real. (macro-plan committed + clean — `e1adf51`.)
- **Option A is LIVE:** `factory.config.yaml projects.erp.prefrozen_phase_plans: docs/projects/erp/rebuild/phase-plans`.
  At each phase's PLANNING the scheduler adopts the pre-authored `phase-plan.{json,md}` byte-exactly
  (sha-verified) and narrows phase_architect to contracts-only. **Nothing to "place" — the plans are
  committed where the config points.** (Verify on L0: the PLANNING step must adopt, not regenerate.)
- **Graft the 2 parked branches** (HIGHEST-RISK manual step — verify diff): `cont-quote-core` → the L7
  `cont-quote-land` stage branch; `treasury-app-foundations` → L8 `treasury-found`. Both 1 commit behind
  main, merge clean. (Not needed for L0 — defer to when L7/L8 build.)
- Seed **Diapazon** (a fiscal range per issuing OwnPJ) before the L6 fiscal gate (not needed for L0).
- Set `proving_phases: [l0-shell]` in `factory.config.yaml` (restart-only) → only L0 dispatches first.
- **Founder copy-paste:** disarm watchdog → restart factory (`sf-factory run` in tmux `factory`, replicate
  PATH incl nvm node) → re-arm watchdog. THEN start your monitor. **Keep drain ON**; lift it for L0 only.

**STOP after L0 proves** (founder verifies the deployed menu + reachable issue/return screens on the test
ERP). Then HOLD the remaining 38 stages until the 25-06 weekly reset (capacity), unless the founder pushes.

## WHERE EVERYTHING LIVES
- Structure: `docs/projects/erp/rebuild/` — `macro-plan.json`, `phase-plans/<l*>/phase-plan.{json,md}`,
  `STRUCTURE.md` (master view + SCOPE BOUNDARY), `SUMAR-FONDATOR.md` (Romanian founder summary + his answers).
- Reseed mechanics: `docs/design/erp-rebuild-reseed-playbook.md` (§8 = ARH-04 update).
- Authoring rationale + factory mechanics + re-verification corrections: `docs/design/erp-rebuild-stage-authoring-notes.md`.
- Plan validator: `scripts/validate_phase_plan.py` (read_phase_plan + size gate, the live factory code).
- Option A wiring: `scheduler._step_planning` + `_planning_prompt(prefrozen=)` + `config.ProjectCfg.prefrozen_phase_plans` (`90fb3dd`).
- Source ERP docs: `/home/artur/projects/ERP-start`, `docs/projects/erp/PROJECT.md`; gap-audit
  `docs/research/erp-gap-audit-22-06-2026.md`; UI/UX `docs/design/ui-ux-concept.md` + `work-protocols/ui-ux-laws.md`.

## OPEN / WATCH
- **Per-layer founder gate** = drain-lift (you hold drain, founder tests the deployed layer, you lift for
  the next) — NOT the per-stage critical gate. 6 critical stages are correctness sign-offs (contract-rest,
  docs-lifecycle-api, payment-producers, payment-allocation, config-rights-rest, users-rights-fe).
- **Codex BUILD resume** still gated (`RESUME_VERIFIED_CLIS={claude,stub}`); codex backend builds safely
  downgrade continue_session→rebuild until verified in-flow post-reseed. L0 is frontend (opus) — unaffected;
  verify codex resume when the first backend stage (L1 `nomencl-rest-verify`) runs.
- **SCOPE BOUNDARY** ratified (founder added nothing): deferred = payroll, reporting/period-close, 1C
  migration, service-orders job-flow beyond cont+ZN, global search.

## WORKING MODE / SUCCESSION
Romanian to founder, plain, his terms, DD-MM-YYYY, copy-paste, **NEVER `AskUserQuestion`**, no bare IDs.
Brutal honesty over validation. **Architect commits to main.** **VERIFY before merge** (diff + affected
tests). `ruff check` before commit. Founder delegation: he gives chat decisions, you apply
([[founder-applies-approvals-via-architect]]); he DELEGATED the integration gate (auto-approve val+audit-passed
stages, all risk). ntfy founder topic `claude-artur-md-hello` (`[arhitect]` prefix). Resolution CLI:
`.venv/bin/sf-factory resolve-escalation`. When YOU hand off, follow `session-launch-protocol.md` verbatim
(auto-launch successor `ARH - 06`; never kill predecessors).
