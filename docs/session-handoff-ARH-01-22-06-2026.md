# Session handoff — ARH-01 → ARH-02, 22-06-2026

**For the Main-Architect successor.** POINTER doc (Doctrine §9), but the founder wants MAXIMAL
detail — long on purpose. ARH-01 hit ~707k context mid-pre-start-prep; the founder directed succession.

> ## ⛔ ABSOLUTE RULE (carry forward — it killed 5u/5v/5w; memory [[never-prompt-matching-pkill]])
> NEVER `pkill -f` / `pgrep -f` (or any broad kill) with a pattern that can appear in a session's
> launch prompt (`sf-architect-monitor.sh`, `sf-factory`, `orchestrator`, `erp-backend`, any factory/stage
> word). It matches the FULL cmdline and kills ALL active sessions at once. Stop a task by EXACT PID
> (verify `/proc/<pid>/cmdline`) or EXACT tmux session name (`tmux kill-session -t <name>`), never `-f`.

## NAMING — you are ARH-02
ARH-01 (me) → **`ARH - 02`**. The phone RC label = `ARH - 02`; tmux slug = `arh-02`. Runbook:
`docs/runbooks/session-succession.md`.

## FIRST duties (in order)
1. Write your session_id into `~/.claude/sf-architect-session` (replace `d83dcdf9-b079-4e30-a123-ea72375e98be`).
   Your id = newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/` containing your launch
   prompt — verify with a UNIQUE phrase (NOT mtime; my transcript also contains it). The live one GROWS.
   **NOTE:** my tmux session restarted mid-work as `sf-f5` (the founder closed the `arh-01` window by
   accident; the session RESUMED, same id `d83dcdf9`, full context). Cosmetic only.
2. **Do NOT start the session monitor yet.** The factory is STOPPED (below), so the monitor would just
   fire exit-12 (orchestrator absent) on a loop. Start it (and update its header ARH-01→ARH-02) only
   when you RESTART the factory at re-seed. Monitor = `~/.claude/sf-architect-monitor.sh`.
3. Verify your RC shows on the founder's phone as `ARH - 02`. Do NOT go silent until confirmed.
4. Do NOT pkill anything.

## ⭐ THE BIG PICTURE — where we are
The ERP rebuild replan is **reviewed, refined, and effectively approved** — we are in **PRE-START PREP**.
The founder's established sequence: **dashboard (DONE) + factory fixes (loop-cap DONE, socket-path PENDING)
→ pipeline review (his NEXT ask) → re-seed the factory → build the 10 layers.** He has NOT yet said the
final "pornește" for the re-seed — the pipeline review comes first, then his go.

## ✅ DONE THIS SESSION (ARH-01) — all committed on SF-F5 main
- **Rebuild plan refined + independently code-verified** (`ef9db0a`): `docs/design/erp-rebuild-plan-DRAFT.md`
  — 10 dependency-ordered layers, **36 stages**, back/front separate, founder-gate per layer, money
  nomenclatures at the BASE. PART 6 = the verification + the empirical-usability-audit corrections
  (`28080fa`): real nomenclature keys `generic_parts_catalog`/`specific_parts_catalog`; generic CRUD is
  **PUT-only** (PATCH+DELETE 405; "delete"=deactivate); the 403-before-routing verify caveat.
- **Re-seed playbook** (`4970ed8` + §7 `a189ed2`): `docs/design/erp-rebuild-reseed-playbook.md` — the full
  factory re-seed mechanics + §7 the pre-re-seed factory fixes.
- **UI inputs captured** (`f9ad819`): `docs/design/ui-ux-concept.md §7` — reference apps **NetSuite /
  Dynamics 365 / Odoo** + the founder's ERP-UX philosophy ("optimize for the operator's 500th use, not
  first impression"; keyboard-first; density>whitespace; process-centric; lists surface exceptions;
  structural error prevention) + role-based + role-adapted-responsive (accountant=desktop, mechanic=phone).
  `docs/design/deferred-todo.md` = the 2 post-replan items (drain semantics + dashboard limits block).
- **Dashboard — APPROVED by founder ("dashboard ok")**: unified stage-detail view (`b46d5fd`: history rows
  link to the SAME rich `/stage/<id>` page + a Cost bloc); founder block reorder + queue dependencies +
  Limite Claude block (`50a2e14`). 99 dashboard tests pass.
- **Merge-gate loop-cap** (`c066103`) — the treasury silent-loop fix: `_step_merge_gate` escalates
  (trigger `merge_gate_loop`) after `escalation.merge_gate_max_tier1_failures` (=3) Tier-1 suite failures
  instead of cycling forever. Behavioral test in `tests/integration/test_tier1_gate.py`.
- **Treasury loop diagnosed + factory STOPPED** — memory [[treasury-merge-gate-socket-loop]].

## ⏳ PENDING — re-seed prep (do these BEFORE the re-seed; tracked in playbook §7)
1. **Socket-path fix (HIGH — the treasury root cause).** Test-PG unix socket lives INSIDE the worktree
   (`.../.worktrees/<stage_id>/.devpg/.s.PGSQL.5433`); a long stage_id → >107 bytes → PG can't bind → all
   DB tests ERROR → the merge-gate loop. **The dev test instance is now STOPPED (I reverted its edits +
   killed erp-* tmux), so there is NO entanglement anymore — safe to apply.** Exact fix (from a read-only
   investigation):
   - `erp-workspace/scripts/pg.sh`: add `PG_SOCK_DIR="/tmp/sfpg-$(printf '%s' "$ROOT" | sha256sum | cut -c1-12)"; mkdir -p "$PG_SOCK_DIR"; chmod 700 "$PG_SOCK_DIR"`. Point PGHOST (`:24`), `unix_socket_directories` (`:98`), and the readiness check (`:38`) at `$PG_SOCK_DIR`. Keep DATA/cluster/logs in `.devpg`.
   - `erp-workspace/backend/erp/settings/base.py` (`:81-84`): derive the **same** `/tmp/sfpg-<hash>` for the Django client (same input = the worktree root abspath, same algorithm). Assert pg.sh `$PG_SOCK_DIR` == Django's for a worktree.
   - GOTCHA: existing `.devpg` clusters carry the OLD `unix_socket_directories` line → wipe `.devpg/data` for affected worktrees (ephemeral) OR rewrite the conf line each start. Don't leave a duplicate key.
   - Belt-and-suspenders: keep the new layer stage-ids SHORT (≤~40 chars) when authoring the seed JSON.
2. **Limite Claude block — wire the data.** The dashboard already RENDERS the block reading
   `/tmp/sf-dash-limits.json` (shape `{checked_at, five_h_pct, weekly_pct, five_h_reset, weekly_reset}`,
   all ISO). It shows "indisponibil" until a poller WRITES that cache. Wire a poller (the session monitor,
   or a factory thread) to write it every ~5 min from `~/.claude/sf-limit.sh` (429-safe cadence). Pure
   read on the dashboard side — do NOT make the dashboard fetch live.

## 🎯 THE FOUNDER'S NEXT ASK — the PIPELINE REVIEW (do this next, before the re-seed)
Founder (verbatim intent): "vreau sa revizui si modul de lucru a pipeline. sa-mi dai date despre cum e
actual, ce agenti cu ce roluri clare sunt definite (in forma grafica si foarte clara), care din agenti
exista doar in teorie." → produce a **clear graphical map** of the factory pipeline + every agent role
(spec/builder/validator/auditors/integration_validator/phase_architect/…), flagging which agents are
**real (actually spawned) vs theoretical (defined but never used)**. Source: `src/sf_factory/scheduler.py`
(the conveyor + `models.*` role keys) + `factory.config.yaml` (canon.architect_roles, risk classes).

## 📦 THE RE-SEED (the big step, AFTER the pipeline review + founder "pornește")
Full mechanics: `docs/design/erp-rebuild-reseed-playbook.md`. Summary: orchestrator must be STOPPED
(it is); `seed-phases` is the offline CLI; STAGES are runtime-generated at PLANNING ingest (no
`seed-stages`). Lean **Strategy A** (archive + fresh DB). Author `erp-rebuild-macro-plan.json` (10 layer
ids + L0→…→L9 edges) + 10 `phase-plan.json` (the 36 stages, SHORT ids). Inject the UI/UX law into
`work-protocols/architect-operations.md` + the `_planning_prompt`/`_spec_prompt` (playbook §4). PRESERVE:
the cont-quote-core + treasury-app-foundations branches (graft, don't rebuild), the merged
foundation+inventory-procurement on main, `runtime_settings`. Resolve/ carry decision #26. Founder runs
the stop/restart copy-paste commands (disarm watchdog FIRST). Keep drain ON; lift it layer-by-layer.

## 🏭 FACTORY STATE (verify fresh)
- **STOPPED** (orchestrator down, `sf-factory-watchdog.timer` DISARMED/inactive, `drain.manual=true`).
  ARH-01 stopped it 22-06 to halt the treasury budget-burn loop. Dashboard (:8377) is down with it.
  passwordless `sudo` works on this box.
- **0 open escalations.** Pending **decision #26** (`escalation_tradeoff`, cont-quote-core) — leave; it
  resolves when L7 is reached.
- **PARKED stages (don't advance):** `cont-quote-core` = AWAITING_HUMAN (so_quotes backend, branch
  `stage/service-orders.cont-quote-core`); `treasury-app-foundations` = BUILD (frozen mid-loop, branch
  `stage/treasury-payments.treasury-app-foundations` intact). Both RE-SLOT/REBUILD into L7/L8.
- **Session monitor: OFF** (intentional — factory down). Start at re-seed restart.

## 🖥️ ERP TEST INSTANCE — STOPPED + CLEANED (done by ARH-01)
The founder confirmed testing is over ("nu am cum sa le fac" — the master-data gap blocks everything).
ARH-01 reverted the 2 dev-cookie edits (`git -C erp-workspace checkout backend/erp/settings/dev.py
backend/apps/accounts/api.py`) + killed the `erp-backend`/`erp-frontend`/`erp-deviceapprover` tmux.
Nothing to clean. A subagent empirically confirmed **9 of 11 core user tasks are impossible** today
(the master-data gap) — the plan's Layers 0–5 cover them one-to-one.

## WORKING MODE / SUCCESSION
- Romanian to founder, plain, his terms, DD-MM-YYYY, copy-paste commands, NEVER `AskUserQuestion`, no bare
  IDs. `ruff check` before commit. Verify DB schema before queries (escalations: `trigger`/`target`, no
  `kind`, trigger is free-text/no-CHECK so scheduler-literal triggers need no migration; decision_requests:
  `gate_kind`; events: `payload_json`). Founder delegation: he gives chat decisions, you apply
  ([[founder-applies-approvals-via-architect]]); brutal honesty over validation (Doctrine §21); mechanical
  guarantees, not "I'm careful" ([[mechanical-guarantees-over-attention]]).
- Resolution CLI: `.venv/bin/sf-factory resolve-escalation <id> <token> --reason …`; `… decide <req> <opt>`.
  ntfy founder topic `claude-artur-md-hello` (`[arhitect]` prefix). Memories updated:
  [[treasury-merge-gate-socket-loop]], [[erp-ui-planning-reset-22-06]] (reference apps DELIVERED).
