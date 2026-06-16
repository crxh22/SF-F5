# Session handoff — ETAPA-5i → ETAPA-5j, written 16-06-2026 (succession per D-0037; founder-directed early handoff)

**For ETAPA-5j (Main-Architect successor).** POINTER doc (Doctrine §9). Authoritative history = `docs/decision-log.md` **D-0046 → D-0052** + `docs/projects/erp/decision-log.md` **D-ERP-0002** + this transcript. Launch: opus, effort max, Remote-Control ON named ETAPA-5j, architect-operations canon layer. RC = the founder drives you from his phone.

**Why this handoff (NOT a context-guard succession):** the founder explicitly directed (10:15 UTC) that the **stock-core split** be done by a fresh session, not by 5i (who was at ~45% context and had just finished investigating it). So this is an early, deliberate hand-off with ONE concrete first task queued.

## YOUR FIRST DUTIES (in order)
1. **Write your session id** to `~/.claude/sf-architect-session` (newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/` — verify via BIRTH time `stat -c %W "$f"`, NOT mtime).
2. **Update the monitor header to ETAPA-5j** (`~/.claude/sf-architect-monitor.sh` line 1-2) but **do NOT start it yet** — start it only AFTER you restart the factory (step in the split plan), because the factory is currently STOPPED (the monitor's orchestrator-absent check would fire immediately). 5i's monitor is already killed. Kill any leftover by exact cmdline `bash /home/artur/.claude/sf-architect-monitor.sh` (NOT pkill -f).
3. **Read** `docs/decision-log.md` D-0052 + `docs/projects/erp/decision-log.md` D-ERP-0002, then **`docs/stock-core-split-plan-16-06-2026.md`** (your first task), then `uv run sf-factory status`.

## YOUR FIRST TASK — execute the stock-core split (founder-directed)
**The factory is STOPPED on purpose so you can do this offline.** Full executable plan + the why + the ⚠️load-bearing claims to verify: **`docs/stock-core-split-plan-16-06-2026.md`**. Summary: `inventory-procurement.stock-core` reworked 5× (too big, does not converge); the founder directed splitting it into 2 smaller `structural` stages (`stock-core-foundation` = lot+state+picking+edit-storno; `stock-core-reservation-release` = reservation register + reservation_action + E5/E10/E11 services) via a surgical DB edit (CANCEL old + register replacement + INSERT 2 PENDING + rewire DAG + commit the hand-edited plan), then restart + re-arm watchdog + start your monitor. **The plan is a researched recommendation — VERIFY the 2 flagged claims (the `replacement_registered`-suppresses-escalation logic at scheduler.py:3933-3969; the FK refs) before the write, and back up first (DB .bak + git backup branch).** It's fully reversible.

## STATE SNAPSHOT (16-06 ~10:16 UTC — RE-CHECK)
- **Factory STOPPED** (orchestrator killed ~10:11, watchdog DISARMED, 5i monitor killed, DB free, 0 expensive agents). Last live orch pid was 1447987 (dead). run-live.log baseline 7216 lines.
- **factory repo main @ `b6e7a7e`** (D-0052 freeze docs + D-0050). **workspace main @ `5cf7e2a`** (C1–C4 v1 freeze). Working trees clean.
- **foundation phase: DONE** (founder signed off 01:04Z, D-ERP-0002). **C1–C4 FROZEN v1**; E6 15A settled.
- **inventory-procurement: RUNNING** (a proving phase, meant to run — D-ERP-0001§5). Stages: `parts-catalog` DONE; `stock-core` BUILD (to be split — your first task); 10 PENDING (negative-stock-guard, ordering, reception, supplier-fiscal-invoice, warehouse-issue, returns-supplier-client, stocktaking, overdue-detection, stock-views, phase-integration).
- **0 open escalations, 0 pending decisions.**

## WHAT 5i DID (full detail: D-0052 + D-ERP-0002)
- Presented foundation sign-off → founder approved → DONE. Froze C1–C4 v1 (workspace `5cf7e2a`) + settled E6 15A (R15A linked-document set = generic `documents.Document` M2M; treasury M1 service_acquisition links identically; no producer-build contract change). Added **D-0050 to PROJECT.md §5** (smaller stages, binding for ALL domain phases — full proactive effect on the 4 phases after inventory-procurement).
- **inventory-procurement auto-advanced** to RUNNING the instant foundation went DONE (before the freeze/D-0050 window) → 5i reviewed the produced 12-stage plan, found it good + finer than foundation, **ACCEPTED it** (re-decompose-mid-RUNNING has no clean mechanism). stock-core was flagged as the one oversize watch-item — which is exactly what is now being split.
- **Handled 4 stage escalations** (the recurring D-0049 contest pattern): parts-catalog [45] rework:BUILD (F-1 disabled-prop gap) → [46] settled (F-2/F-3 no-action re-loop) → DONE; stock-core [47] rework:BUILD (5 real bugs) → [48] rework:BUILD (SC-EDIT-INBOUND) → and it kept auto-reworking (round 5) → founder said split.

## RECURRING CONTEST-PATTERN GUIDANCE (you WILL hit this on every stage — D-0049)
Each stage's AUDIT raises real "comply" findings + 0-2 "accurate-but-no-action" findings the executor CONTESTS → `unresolved_contest` escalation (target `phase_architect` = you; you resolve via `uv run sf-factory resolve-escalation <id> <disposition> --reason "..."`). Verify against BOTH audit reports (same-model + cross-model — they sometimes disagree; cross-model caught 4 HIGH bugs same-model missed on stock-core). Pattern:
- **Real comply bug(s) present →** `rework:BUILD` (fixes them); the contested no-action findings get `sustained` (one-token vocabulary gap).
- **Sole finding(s) = the no-action contest re-looped →** `settled` (the no-action disposition; do-not-re-raise memory stops the loop; structural→MERGE_GATE). This is what closed parts-catalog.
- Carry your full rationale in `--reason` (architect-operations §2 — it reaches the rework build's context). Dismissed-but-real architectural questions → register as tracked OPEN items (see tasks), don't unilaterally patch frozen surfaces (Doctrine §13).

## TRACKED OPEN ITEMS (in the TaskList; do not lose)
- **#10 Phase-Architect spec/contract reconciliations from stock-core**: CROSS-STOCK-005 (pick_for_issue as-of vs invariant monotonic-time scope — at warehouse-issue build), SC-A2 (IP2 §5 signature/prose align), SC-BALANCE-R2 (spec §4.2 wording). NOTE: the split may re-home these in the new stages — re-confirm after the split.
- **#8 OPEN-IP2**: Main-Architect C3 §6 re-sync for `nomenclature.SpecificPart` Layer-2 enrichment (`default=list`), at inventory-procurement integration.
- **#4 (D-0048)** rights-seam repoint before service-orders; **#5 (D-0049)** UI-A3 concurrent-reauth at first scoped-screen phase; **#9** founder DoD §13 retrospective (restart-ERP-code? — architect read = KEEP); **#6** monitor event-update (D-0042) + finding-regen (the contest pattern is systemic — a combined rework+settle disposition would ~halve escalation rounds; deferred, the pattern works).

## WORKING-MODE (keep)
- Founder: Romanian, glossed (no bare IDs/codes), brutal honesty (§21), cost-conscious, drives by phone, answers gates on the dashboard, front-loads decisions, watches closely. He directed both D-0050 and this split.
- Redeploy/stop ritual + clean-window: `docs/runbooks/first-live-run.md` + D-0047. `pgrep -af` on factory agents DUMPS THE CANON into context — use the `comm -23 <(pgrep -f 'output-format stream-json|codex exec'|sort) <(pgrep -f -- '--remote-control'|sort)` form. Measure, don't estimate (§10).

## YOUR SUCCESSION
Finish your work unit, write the handoff, launch ETAPA-5k, **VERIFY 5k's RC on the founder's phone BEFORE going silent** (predecessor RC is the fallback), hand the marker, go silent. Don't keep working after the successor takes the marker.
