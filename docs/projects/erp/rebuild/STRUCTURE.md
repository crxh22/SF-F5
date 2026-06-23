# ERP Rebuild — Stage Structure (master view) — ARH-04, 23-06-2026

**Status:** Authored by ARH-04 via focused per-layer subagents + integration; every phase-plan
mechanically validated against the live factory code (`read_phase_plan` + `evaluate_stage_sizes`,
`scripts/validate_phase_plan.py`). PENDING dual-audit (opus+codex) + codex cross-verify + founder
approval. Machine artifacts: `macro-plan.json` + `phase-plans/<phase_id>/phase-plan.{json,md}`.
Authoring rationale + factory mechanics: [`../../../design/erp-rebuild-stage-authoring-notes.md`].

**What this is:** ALL unfinished ERP work, re-derived as 10 dependency-ordered, founder-testable LAYERS
(phases) — abstracting from the old 7 domain phases (no structural value). 40 stages, **20 backend / 20
frontend** (the hard back/front split), contract-first, small-stage-gate-clean. (Was 36/18/18 before the
dual-audit reconciliation split four oversized bundles at planning: l5 `docs-lifecycle-api`→+`docs-history-read`,
l6 `ordering-fe`→`ordering-reception-fe`+`supplier-invoice-fe` and `issue-return-fe`→`issue-fe`+`returns-stocktake-fe`,
l8 `payment-producers`→+`payment-allocation`.)

## ⚠ SCOPE BOUNDARY — what this rebuild DELIVERS vs what it DELIBERATELY DEFERS (founder must ratify)

This round delivers **L0–L9 = the core-usable ERP**: master data + the commercial/stock/money spine +
config — exactly the gap-audit's BLOCKS-BASIC-USE + BLOCKS-TESTING priorities (G1–G7 + the G11 spine),
built layer-by-layer and founder-verified. It is **NOT the entire ERP.** Both independent audits
(opus + codex) flagged as a BLOCKER that the old 7-phase plan's later domains are absent — so the
deferral is stated here for a CONSCIOUS founder decision, not discovered later:

| Deferred (NOT in these 10 layers) | Was | Why deferred (sound sequencing) |
|---|---|---|
| **Payroll / salariu** — angajat-salary algorithms, calculation document, payouts, cabinet | old phase `payroll` | a separate domain; L8 ships only the `state_payment` producer, NOT salary calc |
| **Reporting / projections** — R01–R21, KPIs, sinecost, WIP, **period-closing workflow** | old phase `reporting` | downstream of operational data; not needed to USE the ERP day-to-day |
| **1C migration / cutover** — master-data import, **opening balances**, open-document import, reconciliation | old phase `migration-1c` | the cutover happens AFTER the system is built + founder-verified |
| **Service-orders job-flow beyond cont+ZN** — **defectare** (defect-assessment doc #1), vehicle **intake/`act primire`/custody transitions**, **production** tracking, **release/act-predare**, sale/incomplete-delivery docs | part of old `service-orders` | L7 delivers the cont(quote)+ZN commercial SPINE; the full job lifecycle builds on it next round. Interim: L5's Vehicle form is the quick vehicle-creation path; custody columns stay read-only until intake exists |
| **Global search** (gap G8) | never scoped | nice-to-have; cross-entity search |
| **In-app config/users/rights UI** (L9) | deferred-as-planned (ADR-0002) | **DECIDED (founder 23-06): BUILD the in-app screens** — NOT Django-admin-v1. The 3 L9 stages ship as authored (`config-rights-rest` + `users-rights-fe` are critical-gated) |

**Why the deferral is correct:** the methodology builds a usable core first, verified layer by layer; the
deferred items are separate domains (payroll), downstream of working operations (reporting), a post-build
cutover (migration), or service-orders DEPTH that builds on this round's spine. Sequencing them after a
solid verified core is sound — but **the founder ratifies THIS round = L0–L9, a future round = the rest.**

## Macro DAG (linear — each layer founder-verified before the next)
`l0-shell → l1-nomencl → l2-money-base → l3-parties → l4-catalog → l5-engine-docs → l6-stock-ops →
l7-service-orders → l8-treasury → l9-config`

Strict-linear is a DELIBERATE choice: the methodology forbids building a later layer before the founder
verifies the prior (it trades factory cross-layer parallelism for per-layer testability). Parallelism
lives WITHIN each layer (the phase-plan DAG). `proving_phases=[l0-shell]` proves the new pipeline on the
smallest layer before fan-out.

## The 40 stages

Legend: **C**=role:contract (seam-freezer) · risk r=routine / s=structural / **K**=critical(founder
human-gate) · BE/FE=kind. Mark: NEW/KEEP(re-verify)/REBUILD(starting-point)/RE-SLOT(moved).

### L0 `l0-shell` — navigation shell + module registry (FE-only; PROVING phase)
| stage | kind | role | risk | mark · scope |
|---|---|---|---|---|
| `menu-registry` | FE | **C** | s | REBUILD · typed sectioned registry screens self-register into (the FE seam) |
| `mount-orphans-home` | FE | leaf | s | REBUILD · mount orphaned issue/return routes + registry-driven launcher Home |
DAG: `menu-registry→mount-orphans-home`. Gate: structured modular menu; orphaned screens reachable.

### L1 `l1-nomencl` — root money + classification nomenclatures + generic-CRUD framework
| stage | kind | role | risk | mark · scope |
|---|---|---|---|---|
| `nomencl-rest-verify` | BE | **C** | r | KEEP/VERIFY-ONLY · re-verify `/api/nomenclature/<key>/` (GET/POST/PUT; 405 DELETE/PATCH; soft-delete=PUT) for 9 keys; defect → ESCALATE, never patch in place |
| `crud-framework-skeleton` | FE | **C** | s | NEW · config-driven CatalogScreen framework proven on Currency (visual tone-setter) |
| `instantiate-catalogs` | FE | leaf | s | NEW · drive the framework for the other 8 catalogs (config-per-entity) |
DAG: `nomencl-rest-verify→crud-framework-skeleton→instantiate-catalogs`. Gate: create/edit/soft-delete
root catalogs; persist + appear in pickers.

### L2 `l2-money-base` — OwnPJ + money locations + exchange rates
| stage | kind | role | risk | mark · scope |
|---|---|---|---|---|
| `own-pj-rest` | BE | **C** | s | NEW · `parties/api.py` slice 1 — OwnPJ CRUD; save() spawns system TVA/Impozit counterparties (read-only view); +nullable `active` col + migration (soft-delete target) |
| `money-loc-rest` | BE | leaf | r | KEEP/VERIFY-ONLY · re-verify cash_desk/bank_account CRUD; clean() (requires_pj/one-currency) through the API; defect → ESCALATE, never patch in place |
| `rate-rest` | BE | leaf | s | RECONCILE/EXPOSE (not green-field) · expose append-only create+history REST over the EXISTING `set_default_rate`; dedup the treasury duplicate writer |
| `money-fe` | FE | leaf | s | NEW · OwnPJ/CashDesk/BankAccount/set-rate screens; clean() as UI validation |
DAG: `own-pj-rest→{money-loc-rest,rate-rest,money-fe}`, `money-loc-rest→money-fe`, `rate-rest→money-fe`.
Gate: OwnPJ (system counterparties auto-appear), CashDesk (PJ rule), BankAccount, set EUR rate + history.

### L3 `l3-parties` — Counterparty (+roles) + Contract (biggest NEW backend)
| stage | kind | role | risk | mark · scope |
|---|---|---|---|---|
| `counterparty-rest` | BE | **C** | s | NEW · Counterparty+Role CRUD, COMMERCIAL-only (system rows guarded on the write path); role filters; +nullable `active` col + migration (soft-delete target) |
| `contract-rest` | BE | **C** | **K** | NEW · Contract CRUD; full clean() as 400s (alb/negru, ≤1 money location, pay-cat direction). Founder gate |
| `counterparty-fe` | FE | leaf | s | NEW · list+create/edit (multi-role chips); the app-wide add-new-inline counterparty picker |
| `contract-fe` | FE | leaf | s | NEW · conditional fields by alb/negru + money-location XOR; inline add-new |
DAG: `counterparty-rest→{contract-rest,counterparty-fe,contract-fe}`, `contract-rest→contract-fe`.
Gate: commercial Counterparty (2 roles) + Contract (alb) with money/legal validation blocking bad combos.

### L4 `l4-catalog` — parts catalog + production nomenclatures + Warehouse
| stage | kind | role | risk | mark · scope |
|---|---|---|---|---|
| `parts-prod-rest` | BE | **C** | r | KEEP/VERIFY-ONLY · re-verify+freeze catalog+production seam (real keys generic_parts_catalog/specific_parts_catalog; M2M); defect → ESCALATE, never patch in place |
| `warehouse-rest` | BE | leaf | r | KEEP/VERIFY-ONLY · re-verify warehouse CRUD (nullable responsible Counterparty FK); defect → ESCALATE, never patch in place |
| `parts-catalog-fe` | FE | leaf | s | NEW · two-layer Generic→Specific manager (OEM/aftermarket); EXTENDS the search-only PartsPicker |
| `prod-warehouse-fe` | FE | leaf | s | NEW · Stage/StageState/Department/Work/Executor/Warehouse screens; M2M editors |
DAG: `parts-prod-rest→{warehouse-rest,parts-catalog-fe,prod-warehouse-fe}`, `warehouse-rest→prod-warehouse-fe`.
Gate: add Generic+Specific (OEM), Stage/Department/Executor/Warehouse — all selectable.

### L5 `l5-engine-docs` — Vehicle + engine re-verify + documents-lifecycle API (KEYSTONE)
| stage | kind | role | risk | mark · scope |
|---|---|---|---|---|
| `vehicle-rest` | BE | **C** | s | NEW · Vehicle slice of parties/api.py (custody/follow-up read-only; intake=L7); +nullable `active` col + migration (soft-delete target) |
| `docs-lifecycle-api` | BE | **C** | **K** | NEW · `documents/api.py` over engine.py: the MUTATE verbs finalize/edit/cancel-storno. The standing keystone contract L6/L7/L8 plug into. Founder gate. (edit = generic verb over each producer's existing create serializer — no per-type adapter.) |
| `docs-history-read` | BE | leaf | s | NEW · the READ half of `documents/api.py`: version-history + dependency-cascade-PREVIEW GETs; descends from the lifecycle contract |
| `engine-reverify` | BE | leaf | r | KEEP/VERIFY-ONLY · re-verify posting/registers/snapshot/period + fiscal-numbering THROUGH the new API; idempotency + write-guards; engine defect → ESCALATE, never patch in place |
| `vehicle-fe` | FE | leaf | s | NEW · Vehicle screen + the REUSABLE document-actions controls (cancel/storno/edit/history) operational screens mount |
DAG: `docs-lifecycle-api→{docs-history-read,engine-reverify,vehicle-fe}`, `vehicle-rest→vehicle-fe`,
`docs-history-read→vehicle-fe`. Gate: create Vehicle; cancel/storno+history on an existing document;
registers net to zero. **Prereq carried: seed/admin-enter Diapazon (fiscal ranges, FK→OwnPJ) per issuing
OwnPJ before the L6 fiscal-invoice flow.**

### L6 `l6-stock-ops` — inventory & procurement OPERATIONS surface
| stage | kind | role | risk | mark · scope |
|---|---|---|---|---|
| `procurement-reverify` | BE | **C** | r | KEEP/VERIFY-ONLY · re-verify merged ordering/reception/issue/return/stocktaking/reservation producers + R2/R3/R6/R8 on real data; defect → ESCALATE, never patch in place |
| `order-to-stock` | BE | leaf | s | REBUILD · extend ordering+reception to target OWN stock (not only a ZnLine); unblocks parts-resale |
| `ordering-reception-fe` | FE | leaf | s | REBUILD · ordering + reception screens + the order-to-stock entry + L5 lifecycle verbs (also descends from order-to-stock) |
| `supplier-invoice-fe` | FE | leaf | s | REBUILD · supplier-fiscal-invoice screen + L5 lifecycle verbs + link-to-purchase-docs |
| `issue-fe` | FE | leaf | s | REBUILD · warehouse-issue + painter-material-issue screens + lifecycle |
| `returns-stocktake-fe` | FE | leaf | s | REBUILD · return-from-executor/to-supplier/from-client + stocktaking + reservation screens + lifecycle |
DAG: `procurement-reverify→{order-to-stock,ordering-reception-fe,supplier-invoice-fe,issue-fe,returns-stocktake-fe}`,
`order-to-stock→ordering-reception-fe`. Gate: full stock cycle (order-to-stock→receive→issue→return→stocktake)
with an edit/cancel; registers consistent.

### L7 `l7-service-orders` — Cont de plata (quote) + ZN (the commercial spine)
| stage | kind | role | risk | mark · scope |
|---|---|---|---|---|
| `cont-quote-land` | BE | **C** | s | REBUILD · graft cont-quote-core; re-verify the EXISTING `/api/quotes/` + discount Σ≡total + lifecycle + R6 payment-state on now-real deps; resolve #104 (contested finding ASM-006); merge |
| `quote-editor-lines` | FE | leaf | s | NEW · peeled editor part 1: header + lines grid; writes quote lines via `/api/quotes/` (ContLine/QuoteLine); ZnLine anchor created server-side by the quotes API (no direct zn-core dep) |
| `quote-editor-flow` | FE | leaf | s | NEW · editor part 2: discount scope/method (Σ≡total) + send/accept/refuse + history |
| `zn-core` | BE | **C** | s | NEW · ZN+ZnLine over frozen L1 skeletons (status layers, restant chain, cont→ZN mapping) |
| `zn-screen` | FE | leaf | s | NEW · ZN board (status layers, lines, links back to Cont) |
DAG: `cont-quote-land→{quote-editor-lines,quote-editor-flow,zn-core}`, `quote-editor-lines→quote-editor-flow`,
`zn-core→zn-screen`. Gate: create Cont (lines+discounts Σ≡total), accept, walk the resulting ZN's statuses.

### L8 `l8-treasury` — payments OPERATIONS (money operations, late per methodology)
| stage | kind | role | risk | mark · scope |
|---|---|---|---|---|
| `treasury-found` | BE | **C** | s | RE-SLOT · graft treasury abstract Payment base/conformity/rate-resolver + ConformityTicket; re-verify on real money nomenclatures; inherit L2's single rate writer |
| `payment-producers` | BE | leaf | **K** | NEW · concrete payment types (incasare/plata_contragent/plata_directa/state_payment); R5/R6/R8 postings (producers only). Founder gate |
| `payment-allocation` | BE | leaf | **K** | NEW · allocate/advance vs ACCEPTED Conts (NEGATIVE R6, cont_reference=Cont.id) → payment_state seam; conformity ticket at recording; descends from payment-producers. Founder gate |
| `payment-fe` | FE | leaf | s | NEW · take-payment (channel→currency, rate prefill+override, MDL equiv), allocate-to-cont, conformity surfacing |
DAG: `treasury-found→payment-producers→payment-allocation→payment-fe`. Gate: FX payment (MDL equiv),
allocate to a Cont, payment-state change, conformity ticket on alb/negru mismatch.

### L9 `l9-config` — config / users / rights management (LAST; build-vs-Django-admin = FOUNDER decision)
| stage | kind | role | risk | mark · scope |
|---|---|---|---|---|
| `config-rights-rest` | BE | **C** | **K** | NEW · Parameter history-preserving set_param REST + CURATED non-destructive rights endpoints. Critical — the rights-mutation backend is the security fault line. Founder gate |
| `config-params-fe` | FE | leaf | s | NEW · parameters editor (typed+history) + exchange-rate/Diapazon admin + print-template overrides + StatusLabel/TicketType |
| `users-rights-fe` | FE | leaf | **K** | NEW · users create/edit + device approve/revoke + per-dimension rights editor (non-destructive). Founder gate |
DAG: `config-rights-rest→{config-params-fe,users-rights-fe}`. Gate: edit a parameter (history kept),
create a scoped user, approve a device — no Django admin.

## Distribution
- **20 backend / 20 frontend** (even, per the back/front rule). **14 contract** stages (seam-freezers).
- risk: **6 routine** (all KEEP re-verify — codex build, spec-audit only; VERIFY-ONLY = a code-change defect
  ESCALATES to a separate structural fix stage, never an in-place patch), **28 structural** (dual code
  audit), **6 critical** (founder human-gate: `contract-rest`, `docs-lifecycle-api`, `payment-producers`,
  `payment-allocation`, `config-rights-rest`, `users-rights-fe` — the money/legal/engine/rights/auth
  correctness points).

## Open-question dispositions (integrator)
- **Soft-delete `active` target** — `Counterparty`, `OwnPJ`, and `Vehicle` have NO `active` column in real
  code, so the standing soft-delete law (deactivate = PUT active=false, never DELETE) needs a target. RESOLVED:
  each owning backend stage bakes a nullable-`active` column + a reversible migration in — `own-pj-rest` (l2),
  `counterparty-rest` (l3), `vehicle-rest` (l5). [applied at planning, was autoresolve/spec-time]
- **#104 contest (contested finding ASM-006, cont-quote-core)** — NOT a seed blocker; resolved at L7-time.
  **Reseed action: export the dossier `/artifact/1200` to a carry-forward doc BEFORE the DB is archived**
  (else the L7 spec loses it). Record decision #26 as "superseded by L7 rebuild" before archiving.
- **Per-layer founder verification** — realized by per-layer drain-lift (playbook §5.9: architect holds
  drain, founder tests the deployed layer, architect lifts for the next) — NOT the per-stage critical gate
  (which fires mid-build, before the layer is deployable). The **6 critical gates** (`contract-rest`,
  `docs-lifecycle-api`, `payment-producers`, `payment-allocation`, `config-rights-rest`, `users-rights-fe`)
  are correctness sign-offs, a distinct concern. (Brutal-honest note: drain-lift is
  architect-attention-managed; if the founder wants a fully-mechanical per-layer gate, switch to
  incremental seeding — seed layer N+1 only after he verifies N. Surfaced for his call.)
- **Diapazon** data prereq before L6: it is a registry nomenclature (generic-CRUD + admin already) —
  seed/admin-enter a fiscal range per issuing OwnPJ as a re-seed setup step BEFORE the L6 fiscal-invoice
  gate (its in-app management screen is L9). **L9 build-vs-Django-admin-v1** — founder decision (stages
  build as authored — **DECIDED (founder 23-06): in-app screens, NOT Django-admin-v1**). **Vehicle intake `act primire`** is DEFERRED to the next-round
  service-orders job-flow (see the Scope Boundary) — NOT a silent L7 item; L5's Vehicle form is the interim path.

## Two factory-mechanics decisions that gate USABILITY (see the founder summary)
1. **Landing mechanism** — `_step_planning` always spawns phase_architect to AUTHOR the plan; no
   pre-place/ingest path. Need: (A) mechanical ingest-wiring [recommended] vs (B) prompt-adopt. Founder call.
2. **Builder-routing fix** — backend structural/critical currently route to opus, not codex (a Step-2
   wiring gap vs the founder-approved "backend→codex" intent). Surgical fix → backend gets codex build +
   dual audit. ARH-04 prepares it.
