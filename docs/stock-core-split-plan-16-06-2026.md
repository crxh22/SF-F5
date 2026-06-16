# stock-core split plan — for ETAPA-5j (founder-directed, 16-06-2026)

**Context:** the founder directed (10:09 UTC) that `inventory-procurement.stock-core` be SPLIT into smaller
stages and rebuilt (D-0050), after it reworked 5× (round after round of real correctness bugs — it does not
converge; it is too big). The founder then directed (10:15) that the SPLIT itself be done by the fresh
successor session (this is you, 5j) rather than by ETAPA-5i. **The factory is STOPPED** (orchestrator killed,
watchdog DISARMED, monitor killed, DB free) precisely so you can do this surgery offline. Do the split FIRST,
then restart.

This plan = the output of a focused code-investigation subagent (ETAPA-5i ran it). It is a well-researched
plan, NOT gospel — **VERIFY the load-bearing claims yourself before the DB write** (they are flagged ⚠️).

## Why surgical DB edit (Route B), not `replan` (Route A)
- The scheduler is **100% DB-row-driven** (`Scheduler._scan_units` scheduler.py:5190 → `list_units` = `SELECT * FROM stages`; `_dispatch` scheduler.py:5222 skips only terminal states). The phase-plan.json is read ONLY at ingest and never reconciles. So an orphaned stage ROW (present in DB, absent from a new plan) **is still dispatched** and, if it's a DAG predecessor, **blocks its dependents forever**.
- `replan`→PLANNING needs an OPEN phase escalation (`resolve-escalation`) or PENDING phase decision (`decide`) — **neither exists**, and there is NO CLI to create one or force a state (CLI = init/run/resume/seed-phases/status/decide/resolve-escalation only). So Route A is not actionable, and even if forced it re-plans the WHOLE phase (LLM churns the other stages' acceptance/contracts) AND still leaves the old stock-core row dangling → identical hand-cancel needed. Route B is strictly less risky + higher control.
- The 2 new stages will be PENDING; the normal SPEC→BUILD conveyor specs+builds them (phase_architect runs per-stage at SPEC). **No phase replan needed to get them specced.**

## The split (proposed; refine the scope/acceptance text as you see fit)
Replace `stock-core` (lot + state + reservation + picking + release + E5/E10/E11 + edit/storno) with TWO `structural` stages:
- `stock-core-foundation` — Lot model + stock-state projection (incl. the edit/storno netting that crashed: SC-EDIT-INBOUND) + picking.
- `stock-core-reservation-release` — reservation register + reservation_action producer + the E5/E10/E11 cross-phase release/redistribute services.

New chain: `parts-catalog → stock-core-foundation → stock-core-reservation-release → {the 7 dependents}`.
**⚠️ The old stock-core has 7 outgoing DAG edges (DB-confirmed), incl. `stock-views` (the original prompt said 6).** Verify the exact set before rewiring: `negative-stock-guard, ordering, reception, returns-supplier-client, stock-views, stocktaking, warehouse-issue`.

## Stage-id / plan facts
- DB stage ids are namespaced `inventory-procurement.<plan-id>`; the plan JSON uses BARE ids (`stock-core`).
- risk_class `structural` (∈ config risk_classes {routine,structural,critical}).
- Plan sidecar (committed on branch `phase/inventory-procurement`, read at `_step_ingest` scheduler.py:3880):
  `/home/artur/projects/erp-workspace/.worktrees/inventory-procurement/_factory/phases/inventory-procurement/phase-plan.json`
  schema `{stages:[{id,name,risk_class,acceptance}], dag_edges:[[from,to]]}`, `extra=forbid`; validation (`read_phase_plan` artifacts.py:199): unique ids, id grammar `^[A-Za-z0-9][A-Za-z0-9._-]*$`, risk_class∈config, every edge endpoint declared, no duplicate edges, acyclic. It is RE-VALIDATED at every recovery — your hand-edited plan MUST pass.

## EXECUTABLE SEQUENCE (orchestrator STAYS STOPPED throughout)

**Step 0 — confirm stopped + back up (reversibility).**
- `ps -p $(head -1 .factory/orchestrator.pid 2>/dev/null) 2>/dev/null` → dead (already true).
- `cp .factory/factory.db .factory/factory.db.bak-$(date +%s)`
- `git -C /home/artur/projects/erp-workspace branch backup/pre-stockcore-split phase/inventory-procurement`

**⚠️ Step 0.5 — VERIFY the two load-bearing claims before any write:**
1. **Replacement-registered escalation:** read `scheduler.py:3933-3969` (`_step_running` + `_replacement_registered`). Confirm: a CANCELLED child with NO `replacement_registered` event escalates the phase; an event of type `replacement_registered` for the cancelled stage id suppresses that. (If this is wrong, the phase escalates on first tick.)
2. **FK refs (DELETE would fail → must CANCEL):** confirm `fix_iterations`, `churn`, `audit_findings` have FK `REFERENCES stages(id)` and rows for stock-core. CANCEL (state change) is safe; DELETE is not.

**Step 1 — edit + commit the plan on `phase/inventory-procurement`** (phase worktree above): remove the `stock-core` stage object; add `stock-core-foundation` + `stock-core-reservation-release` (structural, acceptance split from stock-core's); in `dag_edges` replace `["parts-catalog","stock-core"]`→`["parts-catalog","stock-core-foundation"]`, add `["stock-core-foundation","stock-core-reservation-release"]`, replace each `["stock-core",X]`→`["stock-core-reservation-release",X]` for all 7 dependents. Validate locally BEFORE commit:
`cd /home/artur/projects/SF-F5 && uv run python -c "from sf_factory.artifacts import read_phase_plan; read_phase_plan('/home/artur/projects/erp-workspace/.worktrees/inventory-procurement/_factory/phases/inventory-procurement/phase-plan.json', {'routine','structural','critical'}); print('OK')"`
Commit (also update phase-plan.md narrative).

**Step 2 — ONE DB transaction** (`sqlite3 .factory/factory.db`, wrap BEGIN/COMMIT; `now=strftime('%Y-%m-%dT%H:%M:%fZ','now')`):
1. `UPDATE stages SET state='CANCELLED', updated_at=<now> WHERE id='inventory-procurement.stock-core';`
2. `INSERT INTO stages(id,phase_id,name,risk_class,state,branch,worktree_path,spec_artifact_id,created_at,updated_at) VALUES ('inventory-procurement.stock-core-foundation','inventory-procurement','<name>','structural','PENDING','stage/inventory-procurement.stock-core-foundation',NULL,NULL,<now>,<now>);` (+ same for `…stock-core-reservation-release`) — match the row shape `_step_ingest` uses (scheduler.py:3902); verify columns against `.schema stages` first.
3. Rewire `dag_edges` (level `'stage'`, namespaced ids): DELETE the 7 `from_id='inventory-procurement.stock-core'` + the 1 `to_id='inventory-procurement.stock-core'`; INSERT parts-catalog→foundation, foundation→reservation-release, reservation-release→each of the 7 dependents.
4. **⚠️ Register the replacement (or the phase escalates):** `INSERT INTO events(unit_level,unit_id,event_type,from_state,to_state,actor,payload_json,created_at) VALUES ('stage','inventory-procurement.stock-core','replacement_registered',NULL,NULL,'main_architect','{"replaced_by":["inventory-procurement.stock-core-foundation","inventory-procurement.stock-core-reservation-release"]}',<now>);`

**Step 3 — verify in DB pre-restart:** `SELECT id,state FROM stages WHERE phase_id='inventory-procurement' ORDER BY id;` → 13 rows (parts-catalog DONE, stock-core CANCELLED, 2 new PENDING, 10 PENDING). Re-run the `read_phase_plan` validator. Eyeball the edges (no typos — `dag_edges` has no FK; a typo'd from_id blocks a dependent forever, db.py:399).

**Step 4 — old worktree (after cancel):** `git -C /home/artur/projects/erp-workspace worktree remove .worktrees/inventory-procurement.stock-core --force` (+ optionally `branch -D stage/inventory-procurement.stock-core`). Keep `backup/...` until proven. New-stage worktrees are auto-created by `_step_dispatch`.

**Step 5 — restart** (the usual ritual): fresh tmux `factory` running `.venv/bin/sf-factory run | tee -a .factory/run-live.log` → wait `recovery complete — entering scheduler loop` → re-arm watchdog (`sudo -n systemctl enable --now sf-factory-watchdog.timer`) → relaunch your monitor. On startup: recovery treats CANCELLED stock-core artifacts as terminal (warnings, no abort); `_step_running` sees it CANCELLED+replacement-registered → no escalation; `stock-core-foundation` (deps: parts-catalog DONE) dispatches to SPEC.

## Risks (all mitigated above)
R1 phase escalation if no `replacement_registered` (Step 2.4) — the #1 non-obvious step. R2 FK violation if DELETE not CANCEL. R3 stale committed plan breaking re-ingest (Step 1 commit). R4 plan-validation rejection (run the validator). R5 edge typo = permanent block (Step 3). R7 backups (Step 0) make it fully reversible.

## Code citations
DB-driven dispatch: scheduler.py:5190/5222 (terminal skip 5248-5253), db.py:302/397, models.py:232. Ingest keep-prior-rows: scheduler.py:3876 (3901 guard, 3917-3920 edges add-only). Replan maps: models.py:309 + scheduler.py:158 + models.py:274; cli.py:1170/1255 (reject non-open/non-pending). Cancel/replacement: scheduler.py:3933-3969; transitions models.py:103-163; FKs migrations/0001_init.sql:19-110, db.py:76; verify_integrity terminal-downgrade artifacts.py:489-554; _requeue_scan scheduler.py:5119.
