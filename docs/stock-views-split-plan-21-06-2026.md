# stock-views split plan — 21-06-2026 (founder-directed)

**Author:** design+verification subagent (clean context), read-only on the factory. Every load-bearing
claim below was re-verified against **today's** code (`src/sf_factory/*`), the **schema-4** DB
(`/home/artur/projects/SF-F5/.factory/factory.db`), and the **canonical** committed phase-plan.json. Where
the current state diverges from the D-0053/D-0054 record, it is flagged ⚠️.

**This is a reviewable PLAN. The architect executes the surgery with the orchestrator STOPPED.** Nothing here
was written to the DB / git / orchestrator.

> **✅ EXECUTED (ETAPA-5v, 21-06-2026, ~11:26 UTC) — LIVE, not stopped.** The architect executed this plan but
> deliberately chose the **live** path (orchestrator NOT stopped) over the recommended stopped one, because:
> (1) unlike stock-core (which had a **live BUILD agent** that forced a stop), stock-views was **ESCALATED/parked
> with no running agent** — the stop was only for cleanliness, not to kill anything; (2) verified the running
> orchestrator re-reads the plan ONLY at recovery/ingest and via `_acceptance_text` when DISPATCHING a stage to
> SPEC (phase is RUNNING, no `_step_ingest`), so committing the plan FIRST makes the new acceptance available
> before the DB tx creates the stages; (3) the DB tx is atomic + row-count-guarded + DB/git backed up, so the
> factory stayed UP with zero downtime and a bounded, reversible worst-case (vs. a fidelity-critical restart).
> Order executed: DB `.bak` + git `backup/pre-stockviews-split` → plan edit + `read_phase_plan` validate (14
> stages / 29 edges) → **commit `6d767d1`** on `phase/inventory-procurement` → atomic guarded DB tx (`/tmp/sv-db-tx.py`:
> #97 resolved, stock-views CANCELLED, replacement_registered, +2 PENDING structural stages, 5 edges → 6).
> Verified post-state: DONE 11 / PENDING 2 / SPEC 1 / CANCELLED 2 = 16 stages; 0 open escalations; the
> orchestrator immediately dispatched `stock-views-backend` to SPEC (spec_agent live, worktree carries the new
> acceptance). `stock-views-ui` PENDING (waits on backend); `phase-integration` PENDING (waits on ui).

---

## 0. Decision & precedent (why this surgery, what it mirrors)

Split `inventory-procurement.stock-views` (risk_class=**routine**, currently **ESCALATED** on escalation **#97
`context_budget`**) into TWO **structural** stages, built FRESH:

- **`inventory-procurement.stock-views-backend`** — old spec §5: the ~11 read endpoints
  (`apps/inventory/stock_views.py` + `stock_rights.py` + the procurement extensions in
  `apps/procurement/api.py`/`urls.py`) + the `view_tx`/`view_balances` rights boundary + tests §7.1–7.4.
- **`inventory-procurement.stock-views-ui`** — old spec §6: `frontend/src/features/stock/` «Stoc» + the typed
  `src/api/stock.ts` client + tests §7.5. **Depends on the backend stage.**

The old stock-views worktree doom-looped on rebase conflicts (88M tokens wasted; #97 is a `context_budget`
escalation — it blew the **routine** 30M cap). The split mirrors the **D-0053/D-0054 stock-core split**
(stock-core CANCELLED → `stock-core-foundation` + `stock-core-reservation-release`, both then converged in
hours). That surgery's executable recipe is `docs/stock-core-split-plan-16-06-2026.md`; its executed record is
**D-0054** in `docs/decision-log.md`. This plan re-uses that recipe verbatim, with line numbers re-verified
and TWO differences from the stock-core case flagged explicitly (§6).

**Why structural, not routine (a substantive change, not cosmetic):** routine stages get NO auditor
(`risk_classes.routine.audits: []`) and a 30M token cap. structural gets BOTH `auditor_same_model` +
`auditor_cross_model` and a 250M cap (`factory.config.yaml:65-66, 108-109`). In the stock-core split it was the
**cross-model** auditor that caught the real correctness bugs both rounds (D-0055). The rights boundary + the
own_pj distinguishability are correctness-critical, so the audit coverage is warranted, and the 250M cap (vs
the 30M that #97 blew) removes the budget trip. **Takes effect for these stages because risk_class is a
per-stage column read at scheduling; the cap is `per_stage[risk_class]`, load-once at orchestrator start.**

---

## 1. Stage definitions (phase-plan.json objects + DB rows)

### 1.1 The phase-plan.json schema (verified)

Canonical sidecar, committed on branch **`phase/inventory-procurement`** (NOT on `main` — see §3):
`/home/artur/projects/erp-workspace/.worktrees/inventory-procurement/_factory/phases/inventory-procurement/phase-plan.json`

Schema (confirmed by reading the file + `read_phase_plan` at `artifacts.py:199`):

```
{ "stages":    [ { "id", "name", "risk_class", "acceptance" } ],     # EXACTLY these 4 keys, extra=forbid
  "dag_edges": [ [ "<from-id>", "<to-id>" ], ... ] }                  # BARE ids (no "inventory-procurement." prefix)
```

- Stage objects carry **`acceptance`** (a single rich prose string), NOT `objective`/`brief`/`description`/
  `depends_on`. There is no per-stage dependency field — **dependencies live ONLY in the `dag_edges` array.**
  ⚠️ The task brief asked for `objective`/`brief` text and a `depends_on` field; the real schema has neither —
  the rich text goes into the single `acceptance` string, and the UI→backend dependency goes into `dag_edges`.
  (Mirrors how the existing 13 stage objects are shaped — e.g. `stock-core-foundation`, `reception`.)
- Validation (`read_phase_plan`, re-run at EVERY recovery): unique ids; id grammar
  `^[A-Za-z0-9][A-Za-z0-9._-]*$`; `risk_class ∈ {routine,structural,critical}`; every `dag_edges` endpoint is a
  declared stage id; no duplicate edges; the combined graph is acyclic. **The hand-edited plan MUST pass this**
  (a local validation command is given in §3).
- Plan ids are **bare**; DB ids are **namespaced** `inventory-procurement.<bare-id>`. The scheduler maps bare→
  namespaced at ingest. New bare ids: `stock-views-backend`, `stock-views-ui`.

### 1.2 The two NEW stage objects (paste into the `stages` array; REMOVE the old `stock-views` object)

```json
    {
      "id": "stock-views-backend",
      "name": "Stock visibility READ API — rights-checked projections (la-negru/official distinguishable, depozit keyed on Warehouse.code)",
      "risk_class": "structural",
      "acceptance": "Old stock-views spec §5 — the backend half ONLY (the typed client + «Stoc» UI are the sibling stage stock-views-ui, which depends on this one): the eleven READ projections (V1..V11) as rights-checked, F1-enveloped, page-number-paginated GET endpoints over the as-built lot/state model + R2/R3/R4 registers + the R12 procurement tracker — NO write, NO posting, NO register, NO migration (read-only over the as-built model). Inventory views (V1-V6, V8, V10) live in a new apps/inventory/stock_views.py + apps/inventory/stock_rights.py reading the §3 as-built services (stock_state/project_lots/lot_state/reserved_qty/undelivered_for_zn, ReturnFromExecutor); procurement views (V7, V9, V11) extend apps/procurement/api.py + urls.py over apps.procurement.tracking (reusing OrderingViewPermission verbatim). The view_tx-vs-view_balances boundary (F5 §4) is the spine and MUST be proven mechanically: balance views (V1-V6, V8) check view_balances ONLY and DENY a view_tx-only caller with 403 right_missing (never a silent empty 200); the transaction view (V10) checks view_tx and exposes NO aggregate/summed column (the non-reconstruction shape fence); the three R12 tracker views ride the ordering view_tx gate. IsAuthenticated is paired FIRST so anonymous meets not_authenticated before right_missing. TWO MANDATORY corrections over the old (doom-looped) stock-views code, both load-bearing business requirements: (1) LA-NEGRU vs OFFICIAL DISTINGUISHABILITY — the founder must be able to tell own_pj IS NULL (la-negru) stock apart from own_pj-set (official) stock. Expose an own_pj_isnull tri-state selector (true=la-negru only, false=official only, absent=both) on V1 AND every balance view that lists lots/quantities (V1, V4, V5, V6, V8), and surface the own_pj-null-ness on each row so the two are visibly distinguishable. The OLD code only had an own_pj=<id> integer filter (a specific-pj filter) and did NOT carry this own_pj_isnull la-negru/official selector — add it. (2) DEPOZIT RIGHTS KEYED ON Warehouse.code — the depozit F5 rights dimension keys on nomenclature.Warehouse.code (a unique CharField JUST merged to the integration branch; apps/nomenclature/models/catalog.py:51, whose own comment says the depozit dimension keys on THIS code, never the auto-increment pk). filter_by_rights and every depozit-narrowing/filter MUST key on Warehouse.code (lookup ('depozit','depozit__code')), NOT the lot's depozit_id pk the old code used. Pagination via erp.api.pagination.StandardPagination (page_size 20, max 200) returning {count,next,previous,results}; bad params -> the F1 custom_exception_handler {error:{code,message,details}} typed 400, never a 500/bare error. Value columns are cost-layer (qty x lot.cost_basis), never sinecost, and appear ONLY on view_balances endpoints. NOTE (already handled elsewhere, do NOT re-spec): the mixed-VAT supplier-return rule / V6 showing the VAT regime is owned by the returns-supplier-client stage. No new F5 dimension, no F7 param key, no new app (procurement->inventory is the allowed edge; inventory->procurement stays forbidden). Tests are the gate (old spec §7.1-7.4, write FIRST, all green via bash scripts/test.sh): §7.1 per-view correctness against seeded movements through the real producers (incl. an explicit own_pj_isnull la-negru-vs-official assertion on V1); §7.2 reconciliation invariants (WIP partition V3+V8, value-is-cost-layer); §7.3 the rights boundary BOTH directions (view_balances denied on V10, view_tx denied on balance views, filter_by_rights narrows by Warehouse.code, procurement views require the ordering right, auth-before-right, V10 exposes no aggregate); §7.4 pagination + error-envelope conformance. backend/tests/test_quality.py stays green (ruff/format/mypy + the import-linter layer contract; no new app). Falsifiability (Doctrine §6/§10): if the read set overruns one builder pass on SCOPE (not bugs), the backend further splits inventory-views vs procurement-views — do not patch stage-by-stage; if the view_tx/view_balances boundary cannot be expressed over the as-built rights API, escalate (an F5-consumption gap), never ship a weaker gate."
    },
    {
      "id": "stock-views-ui",
      "name": "«Stoc» feature — design-system tabbed UI + typed src/api client over the stock READ API",
      "risk_class": "structural",
      "acceptance": "Old stock-views spec §6 — the frontend half, built ON TOP of the stock-views-backend READ API (this stage DEPENDS ON stock-views-backend; that dependency is the dag_edge stock-views-backend -> stock-views-ui). frontend/src/features/stock/: a single StockPage (AppPage title «Stoc») hosting AppTabs, one tab per view grouped Depozit {V1,V4,V5,V6} / Angajament {V2,V8,V3} / Aprovizionare {V9,V7,V11} / Trasabilitate {V10}; each tab a filterable, paginated AppTable<Row> with AppSelect/AppDatePicker/AppInput filters, AppEmpty empty state, AppTag chips, pagination wired to ?page/?page_size. The la-negru-vs-official distinction the backend exposes (own_pj_isnull) is surfaced in the UI (a filter and/or a visible per-row indicator) so the founder can SEE official vs la-negru stock — the whole point of the requirement. V10 renders a chronological signed ledger; selecting a vehicle/ZN in V2/V3/V7/V8 deep-links into V10 filtered to it. Saved filters persist via useViewPreference. Typed client frontend/src/api/stock.ts: one typed function per view returning Promise<Page<Row>> via apiFetch (the F1 envelope mapped to ApiError in client.ts; params via URLSearchParams), TanStack Query wrappers in features/stock/useStock.ts, query keys in api/queryClient.ts, explicit TS Row interfaces (strict). RO strings centralized in features/stock/strings.ts (as const); the route+nav entry added to src/shell/routes.tsx (path /stock) + nav.stock to src/shell/strings.ts. The TRIPLE antd-fence stays green (eslint + the src/test/antd-fence.test.ts vitest scan + the backend scan): NO import from \"antd\" anywhere under features/stock or api/stock.ts — design-system barrel (src/ui) ONLY (memory: antd-fence-dual-gate; self-proofs/comments avoid the literal too). Tests (old spec §7.5, all green via bash scripts/test.sh): features/stock/StockPage.test.tsx renders a tab, applies a filter, paginates a mocked Page<Row>, shows AppEmpty on empty — asserting design-system components only; the antd-fence test stays green; npm run check (tsc strict + eslint) stays green. Falsifiability: a tab needs data the backend API does not expose -> it is a stock-views-backend gap (extend the API there), never a direct DB/model read from the frontend; antd reached for directly -> use the design-system barrel or escalate a missing primitive, never a # eslint-disable or a raw antd import."
    }
```

### 1.3 The corresponding DB stage rows (namespaced ids; the INSERT shapes are in §2)

| DB id | risk_class | state | branch | worktree_path | spec_artifact_id |
|---|---|---|---|---|---|
| `inventory-procurement.stock-views-backend` | structural | PENDING | `stage/inventory-procurement.stock-views-backend` | NULL | NULL |
| `inventory-procurement.stock-views-ui` | structural | PENDING | `stage/inventory-procurement.stock-views-ui` | NULL | NULL |

(Row shape mirrors the current PENDING `inventory-procurement.phase-integration` row and the D-0054 inserts:
`branch='stage/<id>'`, `worktree_path=NULL`, `spec_artifact_id=NULL`. Worktrees are auto-created by the
dispatcher when each stage reaches SPEC.)

---

## 2. The exact DB surgery (copy-pasteable; orchestrator STOPPED throughout)

> **Timestamp format — verified.** `models.utc_now()` = `strftime('%Y-%m-%dT%H:%M:%SZ')` — **second** precision.
> Use `strftime('%Y-%m-%dT%H:%M:%SZ','now')`. ⚠️ The stock-core PLAN doc suggested `%f` (ms) but D-0054
> explicitly recorded that as a slip and used `%S`; existing rows are all second-precision. Do NOT use `%f`.

### Step 0 — confirm stopped + back up (reversibility)

```bash
# orchestrator must be dead (this surgery requires it stopped)
ps -p "$(head -1 /home/artur/projects/SF-F5/.factory/orchestrator.pid 2>/dev/null)" 2>/dev/null || echo "orchestrator not running (good)"

# DB backup
cp /home/artur/projects/SF-F5/.factory/factory.db /home/artur/projects/SF-F5/.factory/factory.db.bak-$(date +%s)

# git backup of the phase branch (where the plan lives)
git -C /home/artur/projects/erp-workspace branch backup/pre-stockviews-split phase/inventory-procurement
```

### Step 1 — edit + commit the phase-plan.json on `phase/inventory-procurement` (see §3 — do this BEFORE the DB tx so a mid-surgery recovery re-ingests a CONSISTENT plan)

### Step 2 — ONE sqlite transaction

Run with `sqlite3 -bail /home/artur/projects/SF-F5/.factory/factory.db` (no live writer — orchestrator stopped).
All five sub-steps in ONE `BEGIN`/`COMMIT`:

```sql
BEGIN;

-- (a) RESOLVE escalation #97 (it is OPEN; a terminal stage with an open escalation is inconsistent).
--     ⚠️ DIFFERENCE FROM STOCK-CORE: stock-core had NO open escalation at cancel time; stock-views DOES (#97).
--     Resolve it in-tx (status='resolved', resolution='cancelled', resolved_at) AND emit the escalation_resolved
--     event the CLI would have emitted (so the dashboard/stuck-detector see a clean resolution). Setting the stage
--     terminal (step c) means _step_escalated never runs ESCALATED->CANCELLED on restart (the stage is no longer
--     ESCALATED), so doing the state change directly here is correct and avoids a restart-time double transition.
UPDATE escalations
   SET status='resolved', resolution='cancelled', resolved_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
 WHERE id=97 AND status='open';
-- expect: 1 row changed. If 0, STOP — #97 already closed or id changed; re-check before proceeding.

INSERT INTO events (unit_level, unit_id, event_type, from_state, to_state, actor, payload_json, created_at)
VALUES ('stage','inventory-procurement.stock-views','escalation_resolved',NULL,NULL,'main_architect',
        json('{"escalation_id":97,"resolution":"cancelled","reason":"superseded by the stock-views split into stock-views-backend + stock-views-ui (founder-directed)","via":"surgery"}'),
        strftime('%Y-%m-%dT%H:%M:%SZ','now'));

-- (b) CANCEL the old stage (DIRECT state set — D-0054 precedent; DELETE is forbidden, see §4 FK refs).
UPDATE stages
   SET state='CANCELLED', updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
 WHERE id='inventory-procurement.stock-views' AND state='ESCALATED';
-- expect: 1 row changed. If 0, STOP — the stage left ESCALATED since this plan was written; re-verify state first.

-- (c) REGISTER THE REPLACEMENT (without this the phase ESCALATES on the next tick — _step_running, scheduler.py:4266-4277).
--     VERIFIED match shape: _replacement_registered (scheduler.py:4293-4299) checks ONLY
--       unit_level='stage' AND unit_id=<cancelled id> AND event_type='replacement_registered'  (MAX(seq)>0).
--     The payload is NOT read by the code (the replaced_by list is informational only — mirrors the stock-core event).
INSERT INTO events (unit_level, unit_id, event_type, from_state, to_state, actor, payload_json, created_at)
VALUES ('stage','inventory-procurement.stock-views','replacement_registered',NULL,NULL,'main_architect',
        json('{"replaced_by":["inventory-procurement.stock-views-backend","inventory-procurement.stock-views-ui"]}'),
        strftime('%Y-%m-%dT%H:%M:%SZ','now'));

-- (d) INSERT the 2 new PENDING structural stages (all NOT-NULL columns populated; branch='stage/<id>', worktree/spec NULL).
INSERT INTO stages (id, phase_id, name, risk_class, state, branch, worktree_path, spec_artifact_id, created_at, updated_at)
VALUES ('inventory-procurement.stock-views-backend','inventory-procurement',
        'Stock visibility READ API — rights-checked projections (la-negru/official distinguishable, depozit keyed on Warehouse.code)',
        'structural','PENDING','stage/inventory-procurement.stock-views-backend',NULL,NULL,
        strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now'));

INSERT INTO stages (id, phase_id, name, risk_class, state, branch, worktree_path, spec_artifact_id, created_at, updated_at)
VALUES ('inventory-procurement.stock-views-ui','inventory-procurement',
        '«Stoc» feature — design-system tabbed UI + typed src/api client over the stock READ API',
        'structural','PENDING','stage/inventory-procurement.stock-views-ui',NULL,NULL,
        strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now'));

-- (e) DAG REWIRE (level='stage', NAMESPACED ids). Delete the 5 edges touching the old stage; insert 6 new.
--     Old incoming (4): reception, warehouse-issue, ordering, stock-core-reservation-release  -> stock-views
--     Old outgoing (1): stock-views -> phase-integration
DELETE FROM dag_edges WHERE level='stage' AND to_id='inventory-procurement.stock-views';   -- removes the 4 incoming
DELETE FROM dag_edges WHERE level='stage' AND from_id='inventory-procurement.stock-views';  -- removes the 1 outgoing
-- expect: 5 rows deleted total.

-- New edges: the SAME 4 producers now feed the BACKEND; backend -> ui; ui -> phase-integration.
INSERT INTO dag_edges (level, from_id, to_id) VALUES
  ('stage','inventory-procurement.stock-core-reservation-release','inventory-procurement.stock-views-backend'),
  ('stage','inventory-procurement.reception',                     'inventory-procurement.stock-views-backend'),
  ('stage','inventory-procurement.warehouse-issue',              'inventory-procurement.stock-views-backend'),
  ('stage','inventory-procurement.ordering',                     'inventory-procurement.stock-views-backend'),
  ('stage','inventory-procurement.stock-views-backend',          'inventory-procurement.stock-views-ui'),
  ('stage','inventory-procurement.stock-views-ui',              'inventory-procurement.phase-integration');

COMMIT;
```

### Step 3 — verify in-DB before restart (read-only)

```bash
DB=/home/artur/projects/SF-F5/.factory/factory.db

# 3a. stage count by state (expected AFTER: DONE 11, PENDING 3, CANCELLED 2 — total 16)
sqlite3 -readonly -column -header "$DB" \
 "SELECT state, COUNT(*) n FROM stages WHERE phase_id='inventory-procurement' GROUP BY state ORDER BY state;"

# 3b. the 3 PENDING are the 2 new + phase-integration; the 2 CANCELLED are stock-core + stock-views
sqlite3 -readonly -column -header "$DB" \
 "SELECT id,risk_class,state,branch FROM stages WHERE state IN ('PENDING','CANCELLED') AND phase_id='inventory-procurement' ORDER BY id;"

# 3c. replacement registered for stock-views (must be > 0)
sqlite3 -readonly "$DB" \
 "SELECT COALESCE(MAX(seq),0)>0 AS replacement_ok FROM events WHERE unit_level='stage' AND unit_id='inventory-procurement.stock-views' AND event_type='replacement_registered';"

# 3d. ZERO open escalations (the surgery resolved #97)
sqlite3 -readonly -column -header "$DB" "SELECT id,unit_id,trigger,status FROM escalations WHERE status='open';"

# 3e. no residual edges touch the old stage; the new edges exist
sqlite3 -readonly -column -header "$DB" \
 "SELECT from_id,to_id FROM dag_edges WHERE level='stage' AND (from_id LIKE '%stock-views%' OR to_id LIKE '%stock-views%') ORDER BY to_id,from_id;"
# expect EXACTLY: 4 *_-> stock-views-backend, stock-views-backend -> stock-views-ui, stock-views-ui -> phase-integration. ZERO bare 'inventory-procurement.stock-views'.
```

### Step 4 — old worktree (optional cleanup, AFTER cancel)

```bash
# Harmless to keep (verify_integrity downgrades a terminal stage's worktree to a warning, artifacts.py).
# D-0054 KEPT the cancelled worktree for rollback. Recommend the same: leave it until the split is proven.
# If removing later:  git -C /home/artur/projects/erp-workspace worktree remove .worktrees/inventory-procurement.stock-views --force
```

### Step 5 — restart ritual (the usual clean-window sequence)

Fresh tmux `factory` running `sf-factory run` → wait `recovery complete — entering scheduler loop` → re-arm the
watchdog → relaunch the monitor. On startup: recovery treats the CANCELLED stock-views artifacts/worktree as
terminal (warnings, no abort); `_step_running` sees stock-views CANCELLED **with** a `replacement_registered`
event → **no phase escalation**; `stock-views-backend` (deps: reception/warehouse-issue/ordering/
reservation-release all DONE) is RUNNABLE → dispatched to SPEC; `stock-views-ui` stays PENDING (WAITING on the
backend).

---

## 2bis. Alternative for sub-step (a): the `resolve-escalation` CLI (NOT recommended for this surgery)

The canonical way to close an escalation is:
```bash
cd /home/artur/projects/SF-F5 && .venv/bin/sf-factory resolve-escalation 97 cancelled --reason "superseded by stock-views split"
```
`cancelled` IS a valid stage resolution token (`STAGE_ESCALATION_RESOLUTIONS['cancelled'] = CANCELLED`,
models.py:307). **But** the CLI only resolves the escalation + emits the event — it does NOT change the stage
state; the orchestrator's `_step_escalated` performs the ESCALATED→CANCELLED transition on its **next tick**
(scheduler.py:3052+). During a STOPPED surgery that tick never happens, and on restart that transition would
race the manual rows we wrote. **Recommendation:** do it all in the one offline tx (Step 2a) — resolve #97 +
set CANCELLED directly + register the replacement — exactly the D-0054 pattern (which set state directly), so
the restart sees a fully-consistent terminal stage and never re-transitions it. (Functionally the CLI route +
letting the orchestrator transition would also converge, but it's a live-orchestrator path, not an offline one.)

---

## 3. The phase-plan.json edit (git-committed)

**File (the ONE canonical copy):**
`/home/artur/projects/erp-workspace/.worktrees/inventory-procurement/_factory/phases/inventory-procurement/phase-plan.json`
on branch **`phase/inventory-procurement`**.

⚠️ **DIVERGENCE FROM THE TASK BRIEF / a gotcha:** the brief said "check the main checkout
`_factory/phases/inventory-procurement/phase-plan.json` on the current branch." **It is NOT there.** On `main`,
`git ls-files` tracks ONLY `_factory/phases/foundation/phase-plan.json`. The inventory-procurement plan exists
only inside worktrees, and the **canonical, scheduler-read** copy is the one on `phase/inventory-procurement`
(the phase worktree) — that is the branch D-0054 committed the stock-core split to (commit `b4c5bf7`), and
`_step_ingest`/recovery reads the plan from the phase branch. Edit + commit THERE. (The copies under the
`inventory-procurement.stock-core` and `inventory-procurement.stock-views` STAGE worktrees are stale stage-branch
snapshots — do not edit those.)

**The edits (3):**
1. **Remove** the `stock-views` stage object from `stages[]`.
2. **Add** the two objects from §1.2 (`stock-views-backend`, `stock-views-ui`) to `stages[]`.
3. In `dag_edges[]` (BARE ids): **remove** the 5 old edges
   `["reception","stock-views"]`, `["warehouse-issue","stock-views"]`, `["ordering","stock-views"]`,
   `["stock-core-reservation-release","stock-views"]`, `["stock-views","phase-integration"]`;
   **add** the 6 new edges
   `["stock-core-reservation-release","stock-views-backend"]`, `["reception","stock-views-backend"]`,
   `["warehouse-issue","stock-views-backend"]`, `["ordering","stock-views-backend"]`,
   `["stock-views-backend","stock-views-ui"]`, `["stock-views-ui","phase-integration"]`.

**Validate locally BEFORE commit** (the same validator recovery runs — id grammar, risk_class, edge endpoints,
acyclicity):
```bash
cd /home/artur/projects/SF-F5 && uv run python -c "from sf_factory.artifacts import read_phase_plan; p=read_phase_plan('/home/artur/projects/erp-workspace/.worktrees/inventory-procurement/_factory/phases/inventory-procurement/phase-plan.json', {'routine','structural','critical'}); print('OK', len(p.stages), 'stages,', len(p.dag_edges), 'edges')"
```
Expected: **OK 14 stages, 29 edges** (the plan is CURRENTLY 13 stages / **28** edges — verified with the
validator; the 28 already includes the D-0054 stock-core-split edges. This edit is −1 +2 stages = 14 and −5 +6
edges = **29**). Then commit (also update the phase-plan `.md` narrative if one is tracked alongside).

> The committed plan must be CONSISTENT with the DB rows before restart, because recovery re-validates and
> re-ingests it. Ingest is keep-prior-rows + add-only (it never deletes a DB stage/edge), so the committed plan
> mainly has to (i) pass validation and (ii) declare the two new ids so their edges are legal; the DB tx (§2) is
> what actually creates the rows and removes the old edges.

---

## 4. Verification section (the irreversible-surgery checklist)

### 4.1 Every NOT NULL / CHECK column accounted for in the INSERTs

`stages` (`.schema stages`): `id, phase_id, name, risk_class, state` are NOT NULL — all populated.
`state='PENDING'` ∈ the CHECK set. `branch/worktree_path/spec_artifact_id` are nullable —
branch set to `stage/<id>`, the other two NULL (matches the live PENDING phase-integration row). `created_at,
updated_at` NOT NULL — both set via `strftime(...'now')`. `risk_class` validated against config keys
(`{routine,structural,critical}`) → `structural` ✓.

`events` (`.schema events`): `unit_level`(CHECK in phase/stage/factory)=`'stage'` ✓, `unit_id` non-NULL (required
unless level='factory'), `event_type` NOT NULL, `actor` NOT NULL=`'main_architect'`, `payload_json` NOT NULL
DEFAULT '{}' (explicit `json(...)` given), `created_at` NOT NULL. `from_state/to_state` nullable → NULL (the
stock-core `replacement_registered` precedent also has them NULL).

`dag_edges` (`.schema dag_edges`): PK `(level, from_id, to_id)`, `level` CHECK in {phase,stage} → `'stage'`.
No FK on `from_id`/`to_id` (so a typo silently wedges a dependent — Step 3e eyeballs them). All 6 inserts unique.

`escalations`: the UPDATE only writes `status`/`resolution`/`resolved_at` (all within their domains; `resolution`
is free text, `'cancelled'` matches the CLI vocabulary). The `uq_open_escalation` partial unique index is on
`status='open'` rows only — moving #97 to `resolved` cannot collide.

### 4.2 The `replacement_registered` shape matches the scheduler (re-verified TODAY, schema-4)

`_replacement_registered` (scheduler.py:4293-4299) → `_last_event_seq_of_type(conn, stage_id,
"replacement_registered") > 0`, where the helper (scheduler.py:295-309) matches
`unit_level='stage' AND unit_id=? AND event_type=?`. **It does NOT read the payload.** The cancelled-child
detector `_step_running` (scheduler.py:4257-4291): `cancelled` children whose id is NOT in
`_replacement_registered` → phase `child_failed` escalation. Our event satisfies it → no escalation. ✓
(Identical to the stock-core precedent event at seq 1533, which is the only other `replacement_registered` in
the DB.)

### 4.3 The FK refs that forbid DELETE (so we CANCEL)

Tables `REFERENCES stages(id)`: `fix_iterations`, `churn`, `audit_findings` (+ `dag_edges`/`events` carry stage
ids but no FK). For `inventory-procurement.stock-views`: **`fix_iterations`=4 rows, `churn`=95 rows**,
`audit_findings`=0. A `DELETE FROM stages` would raise a FK violation on the 4+95 child rows → **CANCEL (state
change), never DELETE.** (Plus 78 `events` rows of history we preserve.) ✓ Matches D-0054's reasoning for
stock-core.

### 4.4 `sched_category` maps CANCELLED → terminal (no re-spawn / no requeue)

`sched_category` (models.py:233-254): `state ∈ ("FAILED","CANCELLED")` → `SchedCategory.TERMINAL_FAIL`. The
dispatcher skips terminal categories and the requeue scan is RUNNING-only, so the CANCELLED stock-views row is
never re-dispatched or requeued (same mechanism D-0054 verified for stock-core). The CANCELLED state has an
EMPTY legal-transition set (models.py:162) — it is a true sink. ✓

### 4.5 Complete BEFORE → AFTER DAG edge list (stage level, the stock-views neighborhood)

**BEFORE (5 edges touch `…stock-views`):**
```
reception                      -> stock-views
warehouse-issue                -> stock-views
ordering                       -> stock-views
stock-core-reservation-release -> stock-views
stock-views                    -> phase-integration
```
**AFTER (6 edges; old stage has ZERO):**
```
reception                      -> stock-views-backend
warehouse-issue                -> stock-views-backend
ordering                       -> stock-views-backend
stock-core-reservation-release -> stock-views-backend
stock-views-backend            -> stock-views-ui
stock-views-ui                 -> phase-integration
```
Net: −5 +6 = **+1 edge** (the phase has **28** stage-level edges now → **29** after; verified against the live
DB and the validator — the current 28 already includes the D-0054 stock-core-split edges). The backend inherits exactly the 4 producers
that fed old stock-views; the UI depends on the backend; the UI (not the backend) gates phase-integration — so
phase-integration still cannot start until BOTH new stages are DONE (it transitively depends on the backend
through the UI). Acyclic: the new chain is a linear extension (`…→backend→ui→phase-integration`), no cycle.

### 4.6 Post-surgery stage COUNT (state-by-state)

| state | BEFORE | AFTER | delta |
|---|---|---|---|
| DONE | 11 | 11 | — |
| ESCALATED | 1 (stock-views) | 0 | −1 |
| PENDING | 1 (phase-integration) | 3 (+backend +ui) | +2 |
| CANCELLED | 1 (stock-core) | 2 (+stock-views) | +1 |
| **TOTAL** | **14** | **16** | **+2** |

Open escalations: 1 (#97) → **0**. Pending decisions: 0 → 0 (decision_request #15 is already `answered`).

### 4.7 Pre-flight safety (re-confirm at execution time — state may drift before the architect runs this)

- stock-views is **ESCALATED** now (updated_at 2026-06-21T01:48:50Z); escalation **#97** is the ONLY open
  escalation; no pending decisions. Both Step-2 UPDATEs are guarded (`AND status='open'` / `AND
  state='ESCALATED'`) → if state drifted, they change 0 rows and the architect must STOP and re-assess rather
  than write inconsistent state.
- The orchestrator must be **stopped** (Step 0). The 1279-row `process_registry` is historical (stale foundation-
  phase rows), not live processes.

---

## 5. Business-requirement evidence (the two corrections are real, verified against the doom-looped code)

1. **own_pj la-negru/official distinguishability — MISSING in the old code (must add).** The old
   `apps/inventory/stock_views.py` (in the stock-views worktree) parses `own_pj` as an **integer id**
   (`own_pj_id = _parse_int_param(request,"own_pj")`, then `qs.filter(own_pj_id=own_pj_id)`) — a *specific-pj*
   filter. `grep own_pj_isnull|own_pj__isnull|la-negru` over that file is **empty** → it does NOT carry the
   `own_pj IS NULL` (la-negru) vs `own_pj` set (official) tri-state selector. The new backend MUST add
   `own_pj_isnull` to V1 + all balance views, with the null-ness visible per row.
2. **depozit keyed on Warehouse.code — old code keyed on pk (must change).** The old `stock_views.py` narrows
   `filter_by_rights(qs, user, "view_balances", ("depozit","depozit_id"))` and filters `qs.filter(
   depozit_id=...)` — the **auto-increment pk**. `nomenclature.Warehouse.code` (a `unique CharField(max_length=
   100)`) was JUST merged (`apps/nomenclature/models/catalog.py:51`), and its own in-code comment states the
   `depozit` F5 rights dimension keys on **THIS code, never the auto-increment pk**. The new backend MUST key
   the depozit dimension on `Warehouse.code` (lookup `("depozit","depozit__code")`).
3. **Secondary (do NOT re-spec):** the mixed-VAT supplier-return rule / V6 showing the VAT regime is owned by
   `returns-supplier-client` (DONE). Noted in the backend `acceptance`, not specified.

---

## 6. Differences from the D-0053/D-0054 (stock-core) precedent — flagged for the architect

1. **An OPEN escalation must be resolved (stock-core had none).** stock-core was at AUDIT→BUILD with no open
   escalation when cancelled; stock-views is **ESCALATED with open #97 `context_budget`**. → Step 2a adds the
   `escalations` UPDATE + `escalation_resolved` event. Without resolving #97 the dashboard would show a
   resolved-state mismatch and the stuck-escalation detector could eventually re-page (it ages by
   `resolved_at` and checks the unit is still blocked — moot once the stage is terminal CANCELLED, but resolve
   it cleanly anyway).
2. **risk_class CHANGES routine→structural (stock-core stayed structural→structural).** This is intentional and
   substantive: it turns on the same-model + **cross-model** auditors (the cross-model auditor caught the real
   stock-core split bugs, D-0055) and lifts the token cap 30M→250M (the cap #97 blew). Confirm the founder's
   "both structural" direction is still in force (the task brief states it is).
3. **5 edges rewired, not 8** (stock-views has 4 incoming + 1 outgoing; stock-core had 1 incoming + 7
   outgoing). Counts re-verified against the live DB and the canonical plan.
4. **Timestamp precision:** use `%S` (second), NOT the `%f` the stock-core *plan draft* suggested — D-0054
   recorded that as a slip and the DB rows are all second-precision.

---

## 7. Risks (all mitigated above)

- **R1 — phase escalates if `replacement_registered` is omitted** (the #1 non-obvious step). → Step 2c; verified
  match shape (§4.2).
- **R2 — FK violation if DELETE not CANCEL.** → §4.3 (4 fix_iterations + 95 churn rows); CANCEL only.
- **R3 — stale/inconsistent committed plan breaks re-ingest.** → Step 1 edits + commits the canonical
  `phase/inventory-procurement` copy; validator run (§3) before commit.
- **R4 — plan validation rejection at recovery.** → run `read_phase_plan` locally first (§3); expect 14 stages /
  28 edges.
- **R5 — a `dag_edges` typo (no FK) permanently wedges a dependent.** → Step 3e enumerates the exact expected
  edge set; eyeball for any bare `inventory-procurement.stock-views`.
- **R6 — editing the WRONG plan copy** (the brief pointed at the main checkout / a stage worktree). → §3: the
  canonical copy is on `phase/inventory-procurement`; the stage-worktree copies are stale.
- **R7 — open escalation #97 left dangling / double-transition on restart.** → resolve it in-tx + set CANCELLED
  directly (Step 2a/2b); do NOT use the live-orchestrator `resolve-escalation` route for an offline surgery
  (§2bis).
- **R8 — backups** (Step 0: DB `.bak-<ts>` + git `backup/pre-stockviews-split`) make the whole thing reversible.

---

## 8. Code/DB citations (re-verified today, schema 4)

- `_replacement_registered` scheduler.py:4293-4299; `_last_event_seq_of_type` scheduler.py:295-309; cancelled-
  child detector `_step_running` scheduler.py:4257-4291.
- `sched_category` CANCELLED→TERMINAL_FAIL models.py:233-254; CANCELLED transition sink models.py:162;
  ESCALATED legal exits (incl. CANCELLED) models.py:149-159.
- `STAGE_ESCALATION_RESOLUTIONS['cancelled']=CANCELLED` models.py:286-308; `_step_escalated` (CLI route caveat)
  scheduler.py:3021+; `resolve_escalation` db.py:915-923; `cmd_resolve_escalation` cli.py:1255-1338.
- `insert_stage` db.py:277-294; `insert_dag_edge` db.py:379-383; `insert_event` db.py (events INSERT);
  `utc_now()` = `%Y-%m-%dT%H:%M:%SZ` models.py:353-355.
- `.schema stages|dag_edges|events|escalations|decision_requests` — DB
  `/home/artur/projects/SF-F5/.factory/factory.db`. FK refs `fix_iterations|churn|audit_findings REFERENCES
  stages(id)`.
- `read_phase_plan` artifacts.py:199; risk_classes/per_stage caps factory.config.yaml:60-72,107-109.
- `Warehouse.code` (unique CharField; "depozit dimension keys on THIS code, never the pk")
  `erp-workspace/.worktrees/inventory-procurement/backend/apps/nomenclature/models/catalog.py:45-51`.
- Old stock-views code deltas (own_pj=int filter / depozit_id pk; NO own_pj_isnull)
  `erp-workspace/.worktrees/inventory-procurement.stock-views/backend/apps/inventory/stock_views.py:73-87,293-295`
  + `stock_rights.py:45,53,58,66,71`.
- D-0053/D-0054/D-0055 `docs/decision-log.md:483-560`; stock-core recipe
  `docs/stock-core-split-plan-16-06-2026.md`.
- Canonical phase-plan.json
  `erp-workspace/.worktrees/inventory-procurement/_factory/phases/inventory-procurement/phase-plan.json`
  (branch `phase/inventory-procurement`).
