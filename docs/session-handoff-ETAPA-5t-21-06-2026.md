# Session handoff — ETAPA-5s → ETAPA-5t, 21-06-2026

**For ETAPA-5t (Main-Architect successor).** POINTER doc (Doctrine §9). Read FIRST:
- **`docs/decision-log.md` → D-0061** — the 5r record (queue #87/#88/#89, backend-OOM proof, ntfy outage).
- **Auto-loaded MEMORY** — especially **[[founder-applies-approvals-via-architect]]** (the 21-06 DELEGATION — see §1 below; it is LAW now), [[mechanical-guarantees-over-attention]], [[founder-profile]], [[dashboard-usage-limits-block]].
- `work-protocols/architect-operations.md` §1 (contest resolution), §3 (rework:MERGE_GATE), §4 (escalation routing).
- `docs/runbooks/session-succession.md` — this procedure.

**FIRST duties** (your launch prompt repeats them): write your session_id into `~/.claude/sf-architect-session` (replace — claim the guard), update the monitor header (`~/.claude/sf-architect-monitor.sh`: 5s→5t) + launch it `run_in_background`, AND relaunch the returns watcher (`~/.claude/sf-returns-merge-watch.sh`) — see §4. The 5s watchers die with this session.

## ⚠️ §1. THE DELEGATION — founder, 21-06-2026 (LAW; in memory [[founder-applies-approvals-via-architect]])
**The architect now HOLDS the integration/approval gate.** For ANY stage reaching the `critical_stage` / `phase_signoff` AWAITING_HUMAN gate, **regardless of risk class**, you APPROVE on the founder's behalf once **validation + audit passed** — do NOT wait for him. His words: *"orice etapă care ajunge la gate de integrare nu mai e necesară aprobarea mea... eu oricum o dau mecanic atât timp cât e trecută validarea și auditul."* HOW: monitor exit 11 → verify at SOURCE that VALIDATE passed + AUDIT clean (don't just trust it reached the gate) + glance for an obvious business/money/fiscal error → `sf-factory decide <id> approved`. This does NOT cover budget overruns / escalation_tradeoff / contests — those stay architect-handled (apply his KNOWN rulings, careful call to keep moving; PARK + flag only genuinely irreversible+business with no basis). **The founder is OFFLINE overnight** — keep the factory moving per this.

## Lineage
5q (restart LIVE) → 5r (resolved #87/#88/#89; proved backend-OOM) → **5s** (this session: deployed the backend pytest memory gate to ERP main; resolved returns contest #93; approved returns #17 per the delegation; parked stocktaking #18 + handled the live cascade) → **you = 5t**.

## ✅ LIVE STATE (verify, don't trust this snapshot — 20-06 ~21:25Z)
- Orchestrator pid **140670** alive (`.factory/orchestrator.pid`, `/proc/<pid>/cmdline` — NOT pgrep). `main`, leash 22G, `max_parallel_agents=2`, `budget.critical=450M` (runtime_settings).
- **Backend pytest memory gate DEPLOYED** — ERP main commit **`7207394`** (`/home/artur/projects/erp-workspace`). Mirrors the frontend Layer-2 vitest cap. `backend/_pytest_memgate.py` + `conftest.py` hooks + skeleton spec note. Sizes CONCURRENT pytest processes to the cgroup budget (3 slots @22G). PROVEN live: returns' Tier-1 merge suite ran GREEN with it (rebased onto main → got the gate; `tests_failed: false`, no OOM). Constants env-overridable; `ERP_PYTEST_GATE_DISABLE=1` off.
- **Stages:** returns = **self-healing** (BUILD, fixing Tier-2 integration finding RSC-INT-001; will re-flow BUILD→VALIDATE→AUDIT→critical gate→MERGE_GATE; **AUTO-APPROVE its next critical gate per §1**; on **DONE → clear stock-views #15**). stocktaking = PARKED at AWAITING_HUMAN (founder card **#18**, see §2). stock-views = PARKED (card **#15**, see §3). phase-integration = PENDING (waits on stocktaking). 23 DONE. 0 open escalations.

## §2. Founder card #18 — stocktaking budget tripwire (FOUNDER MORNING DECISION — do NOT auto-approve)
2nd budget trip: **456M vs the 450M cap** (+1.4%); 16 BUILD rounds, 18 VALIDATE iters — the factory's costliest stage. This is the EXACT tripwire the founder asked to reconsider WITH him (the 20-06 "too big / split" dialogue: "las-o să termine, reconsiderăm dacă mai trece de buget"). Backend CONVERGED (recent findings UI-only). 2 open findings, both **ST-UI-OWNPJ-001** (the stocktaking UI exposes no own_pj picker → only la-negru stock reconcilable, not official; high cross_model; the backend endpoint already supports own_pj → frontend gap, OPEN-ST5 peel precedent). **MY RECOMMENDATION (present in chat, his terms):** ACCEPT the backend (converged+correct) + peel the UI own_pj completeness to the frontend/phase scope — the recurring stocktaking UI findings ARE the "too big = backend+UI scope-mixing" symptom, so peeling the UI out IS the right "split", not discarding the backend; then bump `budget.critical` ~480M to close + dispose ST-UI-OWNPJ-001 (settle as OPEN-ST item) so the merge gate passes. Alt: rework the picker (more cost + UI churn). The full rationale is in escalation #94's resolution `--reason` (events).

## §3. Founder card #15 — stock-views (PRE-RULED, apply yourself)
Founder PRE-RULED: clear after the backend fix. **Trigger: returns DONE** (so stock-views runs as a SINGLE backend suite — mechanically no concurrent OOM; do NOT clear while returns' merge suite runs). Steps: bump `budget.routine` 30M→80M (`db.set_runtime_setting` + `runtime_setting_changed` event — mirror the dashboard write path; TEMPORARY) then `sf-factory decide 15 rework:BUILD`. It re-runs clean (single stage). Revert budget.routine after stock-views DONE.

## §4. Watchers you MUST relaunch (5s's die with this session)
- **Monitor** `~/.claude/sf-architect-monitor.sh` (header 5s→5t; run_in_background). Exits: 10 open-esc set, 11 pending-dec set, 12 orch death, 13 5h-limit, 14 routing/recurrence. Baseline now: open_esc=[], pending_dec=[15,18].
- **Returns watcher** `~/.claude/sf-returns-merge-watch.sh` — wakes on returns DONE (exit 20 → clear #15) / kickback (exit 21). NOTE: returns is currently in BUILD; the script fires exit 21 on BUILD — EDIT it to fire only on DONE / ESCALATED / FAILED before relaunching (else it loops), OR just watch returns via the monitor + periodic polls.
- (5s's criticals-poller is moot — stocktaking parked, won't reach DONE until #18.)

## §5. FOLLOW-UPS (don't drop — also tasks/memory)
- **Morning founder brief** — what 5s did overnight (gate deployed; returns #93/#17; #18 parked) + the 2 cards (#18 rec, #15 outcome). Present in chat, Romanian.
- **Revert `budget.critical`→364M** ONLY after BOTH criticals (returns + stocktaking) are DONE (now 450M; stocktaking needs ≥456M to close — do NOT revert early).
- **ntfy founder-page channel DOWN** (egress-blocked ntfy.sh; verified 20-06 21:xx). All autonomous pages fail. Founder's channel = the session. NOT fixable here (his infra). §20 gap: monitor doesn't surface `alert_delivery_failed`.
- **Auditors can't start PostgreSQL** (returns cross_model audit: pg.sh "could not create IPv4 socket 127.0.0.1: Operation not permitted") → static-only findings, weakens assurance + drives churn. Sandbox/cap restriction (NOT the OOM gate). Investigate the auditor agent env. Flag as factory-health.
- **Dashboard usage-limits block** ([[dashboard-usage-limits-block]]) — founder wants the dashboard's FIRST block to show live 5h+weekly Claude limit usage + reset + countdown, refetched every query. Parked, not urgent.
- **Returns single-regime** is DONE (the §1 fiscal rework landed correctly: §4.6 invariant + `RETURN_MIXED_VAT_REGIME` enforcement + XM-006 fix; audit clean). No follow-up.

## §6. WORKING-MODE LEARNINGS
- **VERIFY at SOURCE** (Doctrine §4/§5): every contest/gate this session was checked against the actual spec.md lines + audit reports, not the executor's paraphrase. Approving returns #17 = read the amended spec confirming single-regime + XM-006 before approving.
- **MECHANICAL guarantees** ([[mechanical-guarantees-over-attention]]): the backend gate was PROVEN (systemd-scope math + semaphore tests + a live green merge suite), not asserted.
- **Founder protocol:** Romanian, plain, his terms (cost/speed/risk), DD-MM-YYYY, NEVER AskUserQuestion, no naked IDs, long→SendUserFile. He gives rulings in chat, you APPLY (`decide`/`resolve-escalation`/`set_runtime_setting`). Map plain ruling → token.
- **Verify pidfile via /proc, never pgrep; verify DB schema before queries** (`risk_class` not `kind`, `gate_kind` not `kind`).

## §7. YOUR SUCCESSION (later → ETAPA-5u)
Finish your unit → write `docs/session-handoff-ETAPA-5u-DD-MM-YYYY.md` → launch 5u → VERIFY 5u's RC on the founder's phone BEFORE going silent (if the founder is reachable) → hand the marker. Never two architects writing.
