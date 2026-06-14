# Session handoff — ETAPA-5d → ETAPA-5e, written 14-06-2026 ~15:30 UTC (succession per D-0037)

**For ETAPA-5e (Main-Architect successor).** POINTER doc (Doctrine §9) — authoritative history = `docs/decision-log.md`: read **D-0043** end to end (ETAPA-5d's shift). You launch on **opus @ effort max**, **Remote-Control ON named ETAPA-5e**, with the **architect-operations** canon layer. RC = the founder drives you from his phone.

**Why this handoff:** ETAPA-5d at a CLEAN boundary — slice 2 built/verified/merged, the robustness program designed+ruled (ready to build, like you received slice 2), escalations handled or clearly handed. Proactive clean handoff before a context-filling build stretch (§0/§6; the founder ran `/context`, reinforcing the timing). Not a context-guard trigger — self-judgment per the succession discipline.

## YOUR FIRST DUTIES (in order)
1. **Write your session id** to `~/.claude/sf-architect-session` (newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/`, born at your startup — verify via birth time, not just mtime; the predecessor's large `.jsonl` will share the mtime). You take the context guard.
2. **Start your OWN monitor** (`bash ~/.claude/sf-architect-monitor.sh`, run_in_background) — and **kill 5d's** (it re-invokes the silent 5d, not you; `ps -eo pid,cmd | grep 'bash /home/artur/.claude/sf-architect-monitor.sh'`). The script header says ETAPA-5d — update to 5e. Exit codes: 10=escalations changed, 11=decisions changed, 12=orchestrator absent ~135s, 0=6h heartbeat. CAVEAT: it does NOT catch resolved-but-not-advanced stalls — that is exactly what robustness-program UNIT 2 fixes durably.
3. **Read `docs/decision-log.md` D-0043**, then `uv run sf-factory status`.

## Where everything lives
| What | Where |
|---|---|
| Factory history (spine) | `docs/decision-log.md` D-0001…**D-0043** (read D-0043) |
| **Slice 2 — BUILT/VERIFIED/MERGED, deploy pending** | design `docs/design-slice2-noaction-disposition.md`; on factory main @ ea9ebd4+7c14f55 (817 tests green) |
| **Robustness program — DESIGNED+RULED, ready to build** | **`docs/design-robustness-antistall-14-06-2026.md`** (UNIT 1→2→3; the "Architect rulings — ETAPA-5d" section makes it decision-complete) |
| Architect rules | `work-protocols/architect-operations.md` (§1 contest-resolution, §2 carry-WHY, §3 rework:MERGE_GATE constraint) |
| Runbooks | `docs/runbooks/first-live-run.md` (deploy ritual: disarm watchdog → C-c → fresh tmux `factory` → re-arm) + `session-succession.md` |
| Live state | `uv run sf-factory status` / dashboard `http://server-e9:8377` (+ /costuri) — never trust this doc's snapshot |
| Context guard | hook `~/.claude/hooks/sf-architect-context-guard.sh`; marker `~/.claude/sf-architect-session` |
| Live orchestrator restart | tmux `factory`, `.venv/bin/sf-factory run 2>&1 \| tee -a .factory/run-live.log`, cwd `/home/artur/projects/SF-F5`, PATH incl. `~/.local/bin` + `~/.nvm/versions/node/v24.16.0/bin` |

## State at handoff (SNAPSHOT — re-check via status)
- Factory LIVE, healthy, watchdog ARMED, governor `enabled:true`. Orchestrator pid 710047. `max_parallel_agents=4`.
- foundation: skeleton/config-registry/core-entities/document-engine/print-pdf DONE. **auth-access BUILD** (re-specced [23] fix). dependency-cascade **ESCALATED [21]** (held). register-schemas **ESCALATED [26]** (open). status-notifications VALIDATE. media-attachments + others in flight. Rest PENDING. Proving hold post-foundation.
- **Open escalations: [21] dependency-cascade (HELD for slice-2 `settled`), [26] register-schemas round-2 (OPEN — you adjudicate).** Pending decisions: none.
- Today's budget window: wall hit 14:10 → governor drained → reset 14:51 → resumed. Fresh window now.

## Your main work (priority order)
1. **DEPLOY slice 2** at the next clean 0-agent window — it is the CHURN-STOPPER (its audit-memory + `settled` disposition end the regenerating `unresolved_contest` re-raises now hitting register-schemas/dependency-cascade). Migration `0002` + the new code apply at the restart. Ritual per first-live-run.md. **Confirm the cap-5 option with the founder before the restart** (he is OPEN to it for throughput; it is his cost call). **Sequencing law: build/recalibrate → deploy → resolve.** Post-deploy: settle **[21]** with `resolve-escalation 21 settled` (routes MERGE_GATE→DONE), then handle **[26]** (now `settled`-able if it is accurate-no-action, OR `rework:BUILD` if F-RS-CROSS-001 medium is a real defect — adjudicate per architect-operations §1, the D-0043 [22] method).
2. **BUILD the robustness program** (`docs/design-robustness-antistall-14-06-2026.md`) — UNIT 1 (scheduler-fairness, HIGH/silent-stall) → UNIT 2 (escalation routing + 30-min stuck-detector + orchestrator-owned ≤5min notification) → UNIT 3 (budget-reset architect page). Builder (worktree) → INDEPENDENT clean-context adversarial verifier per unit (Doctrine §4). Decision-complete (rulings in the doc). **When UNIT 2 ships, update the monitor + architect-operations/session-succession to grep the new events** (`escalation_opened_notice|escalation_bumped|escalation_stuck_resolved`). Batch into the slice-2 deploy or the next window.
3. **Slice 3** (recurrence flag — db query for a `settled`/`overruled` finding reappearing + dashboard surface; depends on now-merged slice 2) + **Slice 4** (documentary spec-amendment path — touches `VALID_STAGE_TRANSITIONS`, heaviest blast radius, build LAST with fullest verification).
4. **Watch [23]'s re-confirm gate:** auth-access will return to its `critical_stage` human gate on a now-GREEN full suite — the founder re-confirms (his earlier approval rested on a false premise; surface this clearly when the card appears).

## Working-mode learnings (keep)
- The proven loop: incident → root-cause (§11) → micro-slice → **clean-context adversarial verify (Doctrine §4, mutation-test the safety-critical pins) — NEVER skip** → builder (worktree, Agent tool; no SendMessage available — spawn a fresh agent into the same worktree to continue a unit) → non-executor verifier → merge → D-entry → deploy at a clean window.
- **Resolution mechanics matter:** an `unresolved_contest` resolution marks ALL contested findings the same way and routes to ONE state (`rework:VALIDATE`→overruled, `rework:BUILD`→sustained, `settled`→`_leave_clean_audit` risk-route). A MIXED comply+contest stage needs `rework:BUILD` (the complies must build); `settled` only fits a PURE no-action stage. Carry the WHY into rework_context (architect-operations §2) — it reaches the re-entered agent.
- **The factory advances factory-repo main itself** (it commits decision answers, e.g. `448be6d`) — you interleave with it; cherry-pick/rebase cleanly.
- **Deploy needs a clean 0-agent window** (C-c kills in-flight agents). Windows RARE (busy conveyor). The governor DRAINING on a budget wall also creates a 0-agent window — but don't deploy mid-hold.
- Founder protocol: Romanian, glossed (NO bare IDs/acronyms), options+recommendation, **brutal honesty** (he catches errors — verify before asserting, §21; today he probed the [23] cost rationale sharply and correctly). He drives by phone (RC), reads /costuri, UX-first, deeply values robustness (nothing stuck/lost silently) + cost-consciousness (the 4-agent cap is his knob).
- **§8 discipline:** the [23] cross-stage-test seam and the budget-reset-notification gap were FIRST occurrences → root-fixed/folded, not preventively over-built. The robustness program is the founder-APPROVED durable layer, not speculation.

## Pending founder threads
- **cap-5 option** (more throughput at the next deploy — his cost call; confirm before the restart).
- **[23] re-confirm** — auth-access's critical gate card when it returns (his earlier OK was on a false "all-green" premise).
- **Dashboard redesign** — FUTURE dedicated session (mobile-first, UX-first, after foundation); he asked for when/how, I advised "after foundation or a lull, his priority call"; prep a short intake brief when it nears.
- Founder was ACTIVELY engaged today (answered the A4 gate in 90s, flagged the budget-reset gap) — he is reachable.

## Your succession (when YOUR context-guard fires)
Finish the work unit, write the handoff, launch ETAPA-5f (`SFF5_TMUX_SESSION=etapa-5f SFF5_RC_NAME=ETAPA-5f ./claude_canon.sh "<prompt>"` — RC+name+opus+max auto), VERIFY 5f's RC on the founder's phone BEFORE going silent (predecessor RC is the fallback if the successor's silently fails), hand the marker, go silent.
