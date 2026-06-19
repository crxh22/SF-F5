# Session handoff — ETAPA-5l → ETAPA-5m, 19-06-2026 (D-0037 context-guard succession @ ~558k)

**For ETAPA-5m (Main-Architect successor).** POINTER doc. Authoritative state + the deferred resume plan = `docs/decision-log.md` **D-0058** (read FIRST). Launch: opus, effort max, RC ON named ETAPA-5m, architect-operations canon layer.

## ⚠️ NEW FOUNDER MANDATE (19-06-2026) — OVERRIDES the D-0058 "resume production" order
The founder REPRIORITIZED toward factory robustness. **Do NOT start the factory for PRODUCTION until he explicitly confirms there is nothing else to do first.** Until then the factory may be started **ONLY for TESTS.** Before asking for that confirmation, do these THREE things (in his words):

**1. Verify — and fix/build where missing — the proactive LIMIT-MANAGEMENT mechanism.** Required behavior: detect approaching **X% of the 5h limit** OR **Y% of the weekly limit**; on crossing → **do NOT start new agents, safely finish the ones currently working, and return to normal after the reset.**
   - EXISTS: (a) factory capacity-governor (CCR-11, D-0037) — REACTIVE: drains on a usage-limit SIGNATURE match when agents die (holds new claude spawns, lets current finish, haiku-probes, auto-resumes). (b) ETAPA-5l added (D-0058): `~/.claude/sf-limit.sh` = DIRECT OAuth query of 5h/weekly utilization% + reset time (`GET api.anthropic.com/api/oauth/usage`, bearer from `~/.claude/.credentials.json`); the architect monitor polls it (~5min, exit 13 at 80% 5h) → architect drains MANUALLY.
   - MISSING (the founder's ask): the PROACTIVE %-threshold drain INSIDE THE FACTORY (orchestrator-level), for BOTH 5h AND weekly, that holds new spawns + finishes current + auto-resumes after reset WITHOUT the architect draining by hand. = the "fuller fix" (D-0058 + [[redesign-interactive-tmux-fleet]] research: orchestrator OAuth-polls ~5min + drains at thresholds). DESIGNED, NOT built.
   - ACTION: verify each existing piece actually works (capacity-governor end-to-end; sf-limit; monitor poll); build the missing orchestrator-level proactive mechanism (5h + weekly); present to founder. (Doctrine §10: prove it, don't assume.)

**2. Compile + PRESENT THE FOUNDER the list of outstanding factory-MANAGEMENT problems** (robustness/infra we planned but haven't done). VERIFY each against code + decision-log, complete it. Seed list:
   - (a) [=directive 1] proactive %-threshold limit-management in the orchestrator (5h + weekly) + graceful drain + auto-resume — designed, not built.
   - (b) Architect-SESSION auto-resume after the 5h reset (the D-0056 freeze root). sf-limit + monitor = proactive DRAIN, but there is NO auto-RESUME — relies on the founder's manual wake-up. No cron/at/scheduled wake exists.
   - (c) WEEKLY-limit (seven_day) management specifically — it's the longer binding limit (a week to reset); verify it is handled distinctly from the 5h, not just the 5h.
   - (d) Monitor event-greps (D-0042 / D-0057 carry-forward #6): the architect monitor still watches the legacy open-escalation SET, NOT `escalation_opened_notice|escalation_bumped|escalation_stuck_resolved` (architect-operations §4 says it MUST).
   - (e) `pgrep -f 'sf-factory run'` false-match class: 5l fixed the architect monitor's presence check to `\.venv/bin/sf-factory run` (plain pattern false-matches parallel claude/redesign sessions whose PROMPT contains the path, AND self-matches the checking command). AUDIT the factory code + watchdog unit for the same bug.
   - (f) Budget-reset → architect notification (D-0043 UNIT 3) — verify it fires on `capacity_hold_ended`.
   - (g) The 280M→250M critical-budget REVERT (warehouse-issue one-time exception, D-0058) — after warehouse-issue DONE.
   - (h) Re-derive the full list from the decision-log (D-0034 pause posture, D-0037 governor, D-0043 robustness UNITs 1/2/3, D-0046 budget, D-0057 carry-forwards) — verify what's live vs deferred.

**3. Factory = TESTS ONLY until the founder confirms.** No production conveyor until his go.

## THE DEFERRED RESUME PLAN (D-0058) — execute ONLY after the founder's confirmation
Restart factory (1 restart delivers `ce03942` merge-gate fix + `e0c31d8` 280M) → re-arm watchdog + monitor → resolve **[73]** warehouse-issue `rework:MERGE_GATE` (hand-fix `2353b1e` + 280M → DONE) → REVERT 280M→250M → handle **[78]** returns-supplier-client (D-0049) → verify **own_pj** fix rebuilds (`e888600`). Full detail in D-0058.

## STATE SNAPSHOT (19-06-2026 morning)
- **Factory PAUSED**: orchestrator dead (stale pidfile `1590847`), watchdog DISARMED (`inactive`), `factory` tmux GONE. 5h limit FRESH (~1%, resets ~08:09Z), weekly 34%. **Re-arming the watchdog is part of the production restart, NOT the test runs** — leave it disarmed while testing.
- **Open escalations**: [73] warehouse-issue `context_budget` (prepped), [78] returns-supplier-client `unresolved_contest` (NEW, untouched).
- **Commits (factory main)**: `591e5e4` D-0058, `e0c31d8` 280M, `ce03942` merge-gate fix, reception §9 [10] DONE. own_pj spec/IP2 `e888600` (stocktaking branch). warehouse-issue hand-fix `2353b1e` (its branch).
- **Tools**: `~/.claude/sf-limit.sh` (limit query; exit 2=≥threshold, 3=query-fail), `~/.claude/sf-architect-monitor.sh` (now: proactive limit-poll exit 13 + fixed presence check). Launcher: `claude_canon.sh`.

## YOUR SUCCESSION (later): finish your unit, write the handoff, launch ETAPA-5n, VERIFY 5n's RC on the founder's phone BEFORE going silent, hand the marker.
