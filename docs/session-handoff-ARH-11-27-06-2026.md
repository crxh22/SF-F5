# Session handoff — ARH-11 → ARH-12, 27-06-2026

**For the Main-Architect successor.** POINTER doc (Doctrine §9). ARH-11 drove L2-money-base to
completion (resolved 5 stage escalations + 1 phase decision appeared), landed a control-plane
config fix (spec-audit cap 2→4) with a clean orchestrator restart, and is handing off **two
founder-requested tasks** the founder explicitly asked to pass to the successor (27-06): **(1) set
up L2 for founder testing**, and **(2) redo the "Claude" palette properly**. Durable memory:
**[[erp-claude-style-palette]]** (UPDATED — read it), **[[factory-build-isolation-findings-response-dropping]]**
(NEW — deferred fixes), **[[erp-local-test-instance]]**, **[[founder-applies-approvals-via-architect]]**,
**[[evidence-over-guessing-and-budget-headroom]]**, **[[sf-f5-github-remote-manual-push]]**,
**[[never-prompt-matching-pkill]]**, **[[factory-drain-first-fix-on-running-phase]]**.

> ## ⛔ ABSOLUTE RULES (carry forward)
> 1. **NEVER `pkill -f` / `pgrep -f`** a prompt-matchable pattern (e.g. `sf-architect-monitor`,
>    `sf-factory`, `orchestrator`) — it matches every architect session's `claude` cmdline and kills
>    them all. Stop a task ONLY by EXACT PID or EXACT tmux name. (I slipped once with a read-only
>    `pgrep -f` — harmless but DON'T.)
> 2. **NEVER kill/exit a PREDECESSOR architect session** (arh-03 … arh-11). Leave attached + idle; the FOUNDER retires them.

## NAMING — you are ARH-12
`ARH - 12` (phone RC label; tmux slug `arh-12`). Runbooks: `session-succession.md` + `session-launch-protocol.md`.

## FIRST duties (in order)
1. **Claim the marker.** Your id = newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/`
   that contains a UNIQUE phrase from YOUR launch prompt (NOT mtime — the live one GROWS as a top-level
   USER turn; the scratchpad path also encodes your id). Write it into `~/.claude/sf-architect-session`,
   REPLACING `09162ca4-619d-4463-9399-c82cfdbd3f9c`.
2. **Verify RC** (`ARH - 12` on the founder's phone). Confirm via `/proc/<pane_pid>/cmdline` if `ps|grep` misses it.
3. **START the monitor** (factory RUNNING): `bash ~/.claude/sf-architect-monitor.sh` via Bash `run_in_background:true`.
   Bump its header to ARH-12. Exits: 10=escalation set changed, 11=decision set changed, 12=orch dead, 13=5h-limit, 14=routing. Restart it EACH exit.
4. **Do NOT kill arh-03 … arh-11.**

## 🎯 FOUNDER TASK 1 — set up L2 for TESTING (he wants to test BEFORE signing)
The founder answered the L2 signoff (decision #4, still `pending`) with **"l2 vreau sa testez"** — he
wants to click through L2 LIVE before signing, exactly like L1. **Do NOT auto-sign #4.** Get L2 onto
the test instance, tell him it's ready, let him test → he signs (`cli decide 4 approved` on his nod)
→ L2 integrates to main → L3 planning starts.
- L2 = own legal entity (OwnPJ), money locations (cash desk/bank), exchange rates, + their FE screens.
  Branch `phase/l2-money-base`, worktree `/home/artur/projects/erp-workspace/.worktrees/l2-money-base/`.
- Test instance ([[erp-local-test-instance]]): `tmux erp-fe` serves `erp-workspace/frontend` (MAIN) on
  :5173; `tmux erp-be` = l0-shell orphan backend on :8000 with DEV HACKS (SESSION/CSRF_COOKIE_SECURE=False
  + CSRF_TRUSTED_ORIGINS in dev.py). To test L2 you must point BOTH at L2's code (repoint to the L2
  worktree OR a throwaway test branch) and **run migrations** (L2 adds parties + exchange-rate models).
  Watch the Secure-cookie-over-HTTP gotcha (`device_not_registered` loop) — the dev hacks handle it.
  URL `http://100.69.221.108:5173` (artur/test1234). **The too-warm palette is live there — see Task 2.**

## 🎨 FOUNDER TASK 2 — redo the palette, SYSTEMATICALLY, with the REAL current Claude colors
Read **[[erp-claude-style-palette]]** first (just updated). Founder 27-06, verbatim intent:
- **Method:** do NOT change a few components + ask. Take the FULL LIST of every color parameter in the
  app and, for EACH, apply the corresponding REAL Claude color — or, where Claude has none, a color that
  corresponds to + harmonizes with the rest of the Claude set. Centralized in
  `frontend/src/ui/theme/tokens.ts` (+ `ConfigProvider.tsx` antd map + `shell/AppShell.tsx` nav).
- **🔴 Use the ACTUAL current claude.ai palette — FETCH it (WebFetch/WebSearch/deep-research), do NOT use
  your memory.** My first attempt was memory-built (warm neutrals + terracotta `#C96442`, bg `#FAF9F5`)
  and the founder REJECTED it: **"sunt prea calde culorile"** (too warm). Get real values, then map.
- **State:** the too-warm attempt is UNCOMMITTED on `erp-workspace` main (3 files: tokens.ts,
  ConfigProvider.tsx, AppShell.tsx) — REPLACE it. When the founder approves the look: `npm run check` in
  `frontend`, commit the 3 files to main, update `docs/design/ui-ux-concept.md` §7 palette. (Tie this to
  Task 1: ideally fix the palette so he tests L2 with the CORRECT colors.)

## 🏭 FACTORY STATE (verify fresh)
- **Orchestrator RESTARTED by me** — new pid **1510107** in tmux `factory` (`.venv/bin/sf-factory run`).
  I raised `spec_audit_max_rework` **2→4** (committed+pushed `e154fa7`) and restarted clean (SIGINT 1s,
  DB fully preserved) during the L2-signoff idle window — so L3+ spec audits get 4 rounds (fewer
  spec_audit_loop escalations). Dashboard `http://100.69.221.108:8377`.
- **L0 DONE, L1 DONE, L2-money-base DONE** (all 4 stages: own-pj-rest, money-loc-rest, rate-rest, money-fe)
  — **AWAITING_SIGNOFF**, decision #4 pending (founder will sign after testing — Task 1). **L3-L9 PENDING**
  (no stages planned yet; L3 plans after #4 approved). `drain.manual=false`, `governor.seven_day_threshold_pct=97`.
- **Budget healthy:** ~5h 26% / weekly 40% at 04:23Z (money-fe alone burned 122M — frontend stages are
  expensive; watch the weekly governor as L3+ runs).

## 🔧 THIS SESSION (ARH-11) — what happened
5 stage escalations + the cap fix, all from L2 shaking out (factory machinery, NOT bad work):
- **#12 own-pj-rest internal_error** = §3.1 pre-BUILD assertion tripped on an uncommitted findings-response.json
  dropping. Cleaned worktree + `rework:BUILD`. ([[factory-build-isolation-findings-response-dropping]])
- **#13 own-pj-rest internal_error** = Tier-2 integration_validator concluded PASS but emitted text instead of
  writing `integration-report.json` (one-off compliance miss, not overflow). `rework:MERGE_GATE` → passed.
- **#14 money-loc-rest spec_audit_loop** (cap 2) = documentary nits. `rework:SPEC` (resets loop counter) + exact-fix guidance → converged.
- **#15 rate-rest unresolved_contest** = spec_agent contested 2 cross-model findings (admin single-writer scope, int-guard) — I UPHELD the contest (spec conforms to the FROZEN M3 seam; auditor over-reached beyond it, partly from the non-binding treasury sibling). `rework:BUILD`. Built+merged clean, no post-build re-raise.
- **#16 money-fe spec_audit_loop** (cap 2) = real test-coverage gaps + a founder-facing date-format gap. `rework:SPEC` + concrete guidance → converged (money-fe burned 122M).

## ⏳ DEFERRED (carry forward — see [[factory-build-isolation-findings-response-dropping]] for detail)
1. **§3.1 dropping code fix** — make `_leave_clean_spec_audit` discard droppings (needs a sync→async change
   touching 2 callers + an orchestrator restart). Rare (bit once); reactively handleable (clean worktree +
   `rework:BUILD`). Do with full test verification at a quiescent window.
2. **M3 contract hygiene (SM-3)** — FROZEN `_factory/contracts/phase-l2-money-base/m3-exchange-rate-rest-seam.md:39`
   + that phase `README.md:21` falsely claim `git log --all -- backend/apps/treasury/**` is empty (3 commits on
   the treasury sibling). Spec correctly diverges; reconcile the frozen text (Doctrine §9/§19). Inert.
3. **ERP code is NOT on GitHub** — `crxh22/ERP-start` (the only repo with a remote) is DOCS-ONLY, frozen at
   `c225fbc` (11-12 June). The factory build workspace `erp-workspace` has NO remote + a separate history. So
   the built product code is unpublished. Flagged to founder as a security/strategy decision (publish or not);
   NOT resolved — awaits his call. ([[sf-f5-github-remote-manual-push]]) SF-F5 (framework) IS pushed (e154fa7).

## 📋 CLI / PRECEDENT
- `.venv/bin/sf-factory resolve-escalation <id> <token> --reason "<why>"` (tokens: rework:SPEC|SPEC_DOC|BUILD|
  VALIDATE|MERGE_GATE|settled|approved|changes). Carry the WHY (reaches the re-entered agent). `settled` routes
  POST-BUILD (→MERGE_GATE/AWAITING_HUMAN) — NEVER for a pre-build spec-audit contest (use rework:BUILD).
- `.venv/bin/sf-factory decide <id> <option>`; `.venv/bin/sf-factory status`. Read the agent's OWN evidence
  (worktree `_factory/stages/<id>/` + DB `events.payload_json` traceback) BEFORE resolving — never guess.
- Orchestrator restart (verified safe): `tmux send-keys -t factory C-c` (graceful, flock releases) → verify
  death → `tmux send-keys -t factory '.venv/bin/sf-factory run' Enter` → verify new pid alive. Botched = paused
  factory + intact DB = recoverable. My script: `scratchpad/orch_restart.sh`.

## WORKING MODE / SUCCESSION
Romanian to founder, plain, DD-MM-YYYY, copy-paste, **NEVER `AskUserQuestion`**, no bare IDs, brutal honesty,
recommendation-first. Architect commits to main, gate before commit, **DRAIN/quiesce before a fix on a RUNNING
phase**. Reproduce bugs as the FULL browser (cookie+token+Origin). Phase signoff is the FOUNDER's gate (he tests
first, like L1); stage integration is delegated to you (auto-approve val+audit-passed). When YOU hand off,
follow `session-launch-protocol.md` verbatim (auto-launch `ARH - 13`; never kill predecessors).
