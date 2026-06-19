# Factory-management inventory — accumulated work-problem cases (ETAPA-5a → 5m)

**Compiled:** 19-06-2026, ETAPA-5m — founder step-3 mandate: inventory ALL accumulated
work-problem cases from ETAPA-5a onward; identify what was planned-but-undone and what needs
fixing in the **factory**. **Sources:** decision-log D-0001..D-0059 (3-agent extraction), code +
DB verification, design docs. **State:** factory TESTS-ONLY pending founder production go (D-0059).

Legend — category: **FACTORY** = orchestrator / control-plane / infra / process / limit-mgmt /
escalation-routing / budget / watchdog / monitor / succession. **PRODUCT** = the ERP being built.

---

## A. Resolved THIS session (D-0059) — closure record
- **P1** proactive %-threshold limit mgmt (5h 80% / weekly 90%, graceful drain + auto-resume) — BUILT, 864 tests green, live-verified.
- **P4** monitor routing-event grep (opened/bumped/stuck) — FIXED (exit 14).
- **P3** weekly limit — folded into P1. **P5** pgrep/watchdog — verified safe (watchdog is pid-anchored). **P6** budget-reset→architect page — exists (UNIT 3). **P8** decision-log re-derive — done.
- **P2** architect-session auto-resume (option A) — BUILT INERT + dry-run-proven; **install pending founder go**.

---

## B. FACTORY — needs build / fix / decision (actionable; surfaced or sharpened this session)

### B1. ⚠️ CANON GHOSTS — `architect-operations §1` references TWO mechanisms that were never built
Verified in code this session (no `recurr` in dashboard.py; only `rework:SPEC` full-rebuild exists):
- **Dashboard recurrence flag (Slice 3 — D-0040/42/43).** §1 states "the mechanical recurrence flag on the dashboard is the backstop: if a finding you settled or overruled reappears…". **NOT built.** The D-0048 do-not-re-raise prompt-memory (scheduler.py:3661) partly serves the GOAL (prevents regeneration at the source), but the named dashboard backstop does not exist.
- **Documentary spec-amendment path (Slice 4 — D-0040/42/43).** §1 instructs: "If the amendment is purely documentary (it changes no code), route it down the documentary path so it does not force a needless rebuild." **NOT built.** Only `rework:SPEC` exists → SPEC→BUILD→VALIDATE→AUDIT FULL rebuild (~tens of M tokens). A doc-only spec fix today forces that needless rebuild — directly against the current budget/limit focus.
- **DECISION (founder):** build these (Slice 4 has real budget value), OR amend the canon so it stops naming non-existent mechanisms (Doctrine §19 cascade — the canon was not updated when the slices were deprioritized at D-0047). Recommended: amend the canon now (stop the lie) + build Slice 4 (the documentary path) for the budget win; Slice 3 is low value given D-0048.

### B2. Monitor exit-12 (orchestrator-DEATH detection) is still pgrep-fragile
`~/.claude/sf-architect-monitor.sh:39` uses `pgrep -f '\.venv/bin/sf-factory run'` for the liveness miss-counter. A substring match: a parallel/succession claude session whose PROMPT embeds that path (the D-0058 morning-resume command does) reads as "alive" → the exit-12 death alarm is SUPPRESSED. P2's new resume script got the PID-ANCHORED fix (liveness mtime + pidfile cmdline); the monitor did not. **Fix:** port the pid-anchored check to the monitor (mode-3, quick). P5/(e) was closed for the watchdog (correctly pid-anchored) but the monitor's exit-12 is the residual of that same class.

### B3. Phase-level spawns bypass the incident-7 gate (D-0036 watch — sharpened by the limit focus)
`_step_planning` (scheduler.py:3987) + a phase-level spawn (4434) call `run_agent` DIRECTLY, not the gated `_run_step_agent` (1741). A limit-killed/failed PHASE-level agent gets NO `agent_run_failed` escalation + `usage_limit` mark → the capacity-governor auto-resolve cannot recover it (manual triage). **P1 mitigates** (the proactive hold stops phase spawns before the wall too), but a spawn already running at the wall, or a non-limit failure, still bypasses. Own slice on first incident (still valid).

### B4. Smaller robustness backlog (trigger-gated watch-items)
- **Killed-run spend unledgered** (D-0036 / dashboard F9): tokens from killed runs are not written to the cost ledger → invisible to budgets/totals.
- **CCR-11 forever-failing probe** = indefinite silent drain (re-page candidate) — more relevant now that P1 adds a second hold path.
- **needs_architect backlog** (D-0021, 5 items, each trigger-gated): (1) no operator command to create a phase (manual DB insert); (2) `notify.dashboard_link` hardcodes `gethostname()`; (3) no SIGTERM graceful shutdown in `cli run` (trigger: before a systemd-managed orchestrator); (4) `cli resume` runs dashboard-less; (5) watchdog unit hardcodes the config path.
- **integration_validator smarter diff-scoping** (D-0041 durable fix; partly addressed by the D-0047 hunk-header elision; "diffs touching shared-contract surfaces only" still unbuilt).

### B5. Doc hygiene — stale design-doc status headers (handoff item h: "verify live vs deferred")
`docs/design-robustness-antistall-14-06-2026.md` and `docs/design-slice2-noaction-disposition.md` both say "**NOT yet built**" but shipped (D-0043/44 robustness UNITs 1/2/3 DEPLOYED; slice 2 `settled` no-action MERGED + in use at [21]/[72]). Update headers to BUILT so they stop misleading a successor.

---

## C. FACTORY — production-resume queue (DEFERRED, gated on founder production go — D-0058/D-0059)
- **[73]** warehouse-issue → `rework:MERGE_GATE` (hand-fix `2353b1e` + 280M) → DONE, then **P7: revert critical budget 280M→250M**.
- **[78]** returns-supplier-client `unresolved_contest` — untouched (D-0049 pattern).
- **stocktaking own_pj_isnull** rebuild (build killed by the drain; re-runs from `e888600`).
- **Deploy on restart:** `ce03942` (merge-gate auto-recovery fix) + the P1 proactive governor (both live-on-restart via editable install).

---

## D. PRODUCT / ERP — carry-forwards (gated on reaching those phases; NOT factory-mgmt, listed for completeness)
- ⚠️ **C1 R2-lot-column contract-watch** at `service-orders` (D-0055 / OPEN-WI4) — most consequential; lot-exact COGS may force a C1 contract change.
- **Rights-seam repoint** (`STATUS_TRANSITION_RIGHTS_BACKEND` / `NOTIFICATION_RIGHT_HOLDERS_BACKEND` → data-driven `has_right`) BEFORE service-orders (D-0042/48).
- **UI-A3** concurrent-reauth overlay at the first scoped-screen phase (D-0049).
- **OPEN-RCP5** reception storno-after-delivery (deferred-no-action); **OPEN-IP2** C3 `SpecificPart` re-sync; **AUD-RCP-3** overdue-worklist → overdue-detection/phase-integration; **ordering URL-split** → phase-integration.
- **Snapshot axis-discriminator** decision (reporting Phase Architect, PROJECT.md §2); **per-user view-preferences** concrete set (founder-deferred); **stage-granularity directive** (standing, PROJECT.md §5 — finer stages for the 4 phases after inventory-procurement).

---

## E. Notable CLOSED-LATER (excluded from the open set, for audit)
case-2b over-fire (D-0044→fixed D-0045); integration_validator 1M overflow (D-0041/46→Part B D-0047); monitor routing-event grep (open D-0042..D-0047 → P4 D-0059); OPEN-2 test_command (D-0011..D-0021→D-0025); stock-core split (D-0053→DONE D-0055/56).
