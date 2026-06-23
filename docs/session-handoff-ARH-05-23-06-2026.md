# Session handoff — ARH-05 → ARH-06, 23-06-2026

**For the Main-Architect successor.** POINTER doc (Doctrine §9). ARH-05 executed the re-seed, proved the
new pipeline end-to-end on L0's first stage, landed a root-cause fix, and DRAINED the factory to protect
the weekly budget (proactive limit-protection went blind). Your job = resume + finish L0 after the weekly
reset, then drive the 38-stage build. Durable memory: **[[erp-rebuild-structure-authored-23-06]]** +
**[[factory-weekly-governor-usable-runway]]** + **[[founder-model-effort-policy]]** + **[[erp-rebuild-redesign-22-06]]**.

> ## ⛔ ABSOLUTE RULES (carry forward)
> 1. **NEVER `pkill -f` / `pgrep -f`** (or any broad kill) with a pattern that can appear in a session's
>    launch prompt — kills ALL architect sessions at once. Stop a task ONLY by EXACT PID or EXACT tmux
>    name. ([[never-prompt-matching-pkill]])
> 2. **NEVER kill/exit a PREDECESSOR session (ARH-03 `arh-03`, ARH-04 `arh-04`, ARH-05 `arh-05`).** Leave
>    them attached + idle; the FOUNDER retires them. (`session-launch-protocol.md §B`)

## NAMING — you are ARH-06
`ARH - 06` (phone RC label; tmux slug `arh-06`). Runbooks: `session-succession.md` + `session-launch-protocol.md`.

## FIRST duties (in order)
1. **Claim the marker.** Your id = newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/`
   containing a UNIQUE phrase from YOUR launch prompt (NOT mtime — verify it is a top-level USER turn +
   the file is GROWING; the harness scratchpad path also encodes your id). Write it into
   `~/.claude/sf-architect-session`, REPLACING `065e79e9-fd44-469e-84c6-22a340f56fc9`.
2. **Verify RC** (`ARH - 06` on the founder's phone). Do NOT go silent until confirmed; he is reachable on
   ARH-05's live RC meanwhile.
3. **The factory is RUNNING but DRAINED (not stopped)** — so DO start the session monitor (it only loops
   exit-12 when the orchestrator is ABSENT; it's present). `bash ~/.claude/sf-architect-monitor.sh` via the
   Bash tool `run_in_background:true`. Grep the escalation events. (Header already bumped to ARH-05 — bump to ARH-06.)
4. **Do NOT kill ARH-03/04/05.** All stay idle/readable.

## 🏭 STATE (verify fresh)
**Factory RUNNING + DRAINED** (orchestrator pid was 867598 in tmux `factory`; watchdog ARMED; dashboard
`http://100.69.221.108:8377`). `drain.manual=true` (I set it manually — see capacity below). The new
10-layer structure is SEEDED and L0 is mid-proof.

**Commits this session (SF-F5 main, tree clean):**
- `dcacec7` — contest #104 / decision #26 carry-forward (exported before the Strategy-A archive).
- `4363a50` — `proving_phases: [l0-shell]`.
- `730fcd2` — **ROOT-CAUSE FIX**: Spec Agent now grounds in the existing repo before writing; spec auditor
  verifies realizability. **Activates at the NEXT orchestrator restart** (NOT yet live). 176 tests green.

## ⚠️ CAPACITY — the live risk (READ [[factory-weekly-governor-usable-runway]])
- **Weekly ~92% used** (founder-confirmed on the dashboard), resets **25-06 ~03:00 UTC**. 5h ~32%, resets
  **23-06 06:50 UTC**. Governor weekly threshold RAISED to **95%** (founder-directed; runtime_settings
  `governor.seven_day_threshold_pct=95`).
- **The OAuth limit query FAILS INTERMITTENTLY** → the proactive %-drain went BLIND (founder got `[arhitect]`/
  governor ntfy 04:25 + 05:10 UTC). `sf-limit.sh` works for me NOW but not always — likely 429 from too many
  concurrent pollers (governor 300s + monitor ~5min + dashboard + manual). **FIX THIS** (reduce poll
  pressure / token refresh) OR keep relying on MANUAL drain. I drained MANUALLY because the automation is
  unreliable at 92% — do not lift drain until weekly resets (or you've fixed the query AND have headroom).
- **The architect session shares this weekly budget** — heavy activity near the wall hastens overrun. Be lean.

## 🎯 YOUR MANDATE — resume + finish L0 after the weekly reset, then the big build
**L0 proof status: the WHOLE new pipeline is PROVEN end-to-end** (Option A byte-exact plan adoption →
narrowed contracts → spec dual-audit + rework-cap escalation → BUILD → VALIDATE → code dual-audit →
contest handling → my rework:BUILD + rework:SPEC_DOC resolutions). What remains is mechanical completion:
- **`l0-shell.menu-registry` (stage 1):** code BUILT + green (tsc/eslint/vitest all pass), spec prose
  reconciled to the as-built (esc #2 rework:SPEC_DOC). It was in AUDIT/near-merge when I drained — HELD at
  the merge gate by drain. Lift drain (after reset) → it merges → DONE.
- **`l0-shell.mount-orphans-home` (stage 2):** PENDING (DAG-waits on stage 1).
- **FOUNDER'S PLAN (deferred to Monday by capacity):** when stage 1 is DONE, RESTART the factory (loads the
  730fcd2 grounding fix) so stage 2's SPEC runs on the NEW prompts — then directly compare stage 2's
  spec-audit rounds vs stage 1's (stage 1 took 3 spec rounds + hit the cap, all from the un-grounded spec).
  The restart-trigger watcher pattern I used: auto-drain on `menu-registry` DONE, then restart, then lift.
- **After L0 proves (founder tests the 2 deployed screens): the 38-stage build** — widen/empty
  `proving_phases`, lift drain layer-by-layer with the founder's per-layer gate. Graft the 2 parked branches
  at L7/L8 (`erp-rebuild-reseed-playbook.md §8b` steps 8-9); seed Diapazon before L6.

## RESEED / STRUCTURE / MECHANICS
- Reseed playbook + checklist: `docs/design/erp-rebuild-reseed-playbook.md §8b`. Structure:
  `docs/projects/erp/rebuild/` (macro-plan.json, phase-plans/<l*>/, STRUCTURE.md, SUMAR-FONDATOR.md).
- Option A wiring: `scheduler._step_planning` (~4518) — copies `phase-plans/<phase-id>/phase-plan.{json,md}`
  byte-exact, sha-verifies the JSON after the narrowed phase_architect runs (escalates `prefrozen_plan_modified`).
- Contest #104 carry-forward (for the L7 `cont-quote-land` REBUILD spec):
  `docs/projects/erp/rebuild/phase-plans/l7-service-orders/CONTEST-104-carry-forward.md`.
- Escalation resolution CLI: `.venv/bin/sf-factory resolve-escalation <id> <token> --reason "<rework_context>"`.
  Stage tokens: rework:SPEC | rework:SPEC_DOC (documentary, settles+skips BUILD→VALIDATE) | rework:BUILD |
  rework:VALIDATE | rework:MERGE_GATE | awaiting_human | cancelled | failed | respec. The `--reason` BECOMES
  the re-entered agent's prompt (architect-operations §2 — carry the WHY: exact file+line+contradiction).

## OPEN / WATCH
- **Fix the intermittent limit-query failure** (above) — the #1 operational risk.
- **Per-layer founder gate = drain-lift** (he tests the deployed layer, you lift for the next); per-stage
  critical gates are architect-auto-approved (founder DELEGATED the integration gate — [[founder-applies-approvals-via-architect]]).
- **The grounding fix (730fcd2) is unproven in-flight** — its first real test is whenever the next SPEC runs
  post-restart (stage 2, or the big build). Watch whether spec-audit rounds drop.
- **Codex BUILD resume** still gated (`RESUME_VERIFIED_CLIS={claude,stub}`); the first backend stage (L1
  `l1-nomencl`) is the codex-resume test. L0 is frontend (opus) — unaffected.

## WORKING MODE / SUCCESSION
Romanian to founder, plain, DD-MM-YYYY, copy-paste, **NEVER `AskUserQuestion`**, no bare IDs. Brutal honesty
over validation. **Architect commits to main.** **VERIFY before merge** (diff + tests). `ruff check` before
commit. ntfy founder topic `claude-artur-md-hello` (`[arhitect]` prefix). When YOU hand off, follow
`session-launch-protocol.md` verbatim (auto-launch `ARH - 07`; never kill predecessors).
