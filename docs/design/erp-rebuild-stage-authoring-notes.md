# ERP Rebuild — Stage-Authoring Working Notes (ARH-04, 22-06-2026)

**Status:** WORKING doc for the structure-authoring mandate. Integrator source-of-truth; survives
compaction; briefs the per-layer authoring subagents; feeds the founder approval summary. NOT yet a
ratified artifact — the ratified outputs are `macro-plan.json` + the 10 `phase-plan.json` files.

**Mandate (ARH-04):** re-derive ALL unfinished ERP work as a NEW stage structure, abstracting from the
old 7 domain phases (no structural value). Basis = `erp-rebuild-plan-DRAFT.md` (10 layers, RE-EXAMINED).
Small stages, short ids, `kind` backend/frontend, contract-first, size-gate-clean. Method: focused
per-layer subagents → I integrate → dual-audit (opus+codex) → codex cross-verify → READY for founder
approval. Prepare re-seed; STOP before build.

---

## 1. RE-VERIFICATION CORRECTIONS (independent code re-check vs `erp-workspace` — these CHANGE the plan)

The DRAFT was verified by ARH-01; a fresh ARH-04 subagent re-checked HARD. Confirmed holes + corrections:

**CONFIRMED (safe to build on):**
- `parties` app (Counterparty, Contract, Vehicle, OwnPJ, CounterpartyRole, **+ContractType**) has **NO
  `api.py`** and is **NOT mounted** in `backend/erp/urls.py` (admin-only). → L2/L3/L5 NEW backend stands.
  `OwnPJ.save()`→`ensure_system_counterparties()` spawns TVA (always) + Impozit-pe-venit (if vat_payer).
  `Contract.clean()` enforces alb⇒own_pj / negru⇒forbidden / at-most-one money location / pay-cat
  direction-match (also DB CheckConstraints).
- `documents` app has **NO `api.py`** — finalize/edit(reverse-and-repost)/storno/cascade/version engine
  is callable-Python-only (`documents/engine.py`). → L5.2 documents-lifecycle-API stands.
- Nomenclature registry = **exactly 23 keys**, generic CRUD at `/api/nomenclature/<key>/`, **GET/POST/PUT
  only; PATCH+DELETE → 405** (delete = soft `active=false`, `nomenclature/api.py` RetrieveUpdateAPIView).
  Parts keys are **`generic_parts_catalog` / `specific_parts_catalog`** (NOT generic_part). `cash_desk`,
  `bank_account` ARE registered keys.
- Apps on workspace `main` (14): accounts, attachments, documents, fiscal, inventory, nomenclature,
  notifications, parties, printing, procurement, registers, service_orders, status_layers, tickets.
  `so_quotes` + `treasury` exist ONLY on their stage branches.
- Import-linter layering real + enforced (`pyproject.toml [tool.importlinter]`): **registers + fiscal**
  reference `service_orders` by `BigIntegerField` (not FK) and are in `forbidden` contracts.

**CORRECTIONS THAT CHANGE THE PLAN:**
1. **so_quotes is NOT REST-less.** `stage/service-orders.cont-quote-core` already ships `api.py` (400
   lines) + `urls.py` mounting **`/api/quotes/`** (6 endpoints: Collection/Detail/SendForCoordination/
   Accept/Refuse/Relink) + patches `erp/urls.py`. "Peeled" = only the **React editor** is absent. → L7
   backend = **re-verify + land the existing API**, NOT build-from-scratch. Smaller than the DRAFT implies.
2. **Procurement does NOT use the BigInteger-not-FK pattern.** Procurement is the L6 top layer and
   **imports `service_orders` directly** (lazy `from apps.service_orders.models import ZnLine/Zn`). Only
   **registers + fiscal** are decoupled. The DRAFT's "registers/procurement reference SO by BigInteger"
   is wrong for procurement. No decoupling stage needed for procurement.
3. **`DefaultExchangeRate` already has writers on `main`** — Python `nomenclature/exchange_rates.py:
   set_default_rate` + a Django-admin add-path. The treasury branch adds a **SECOND** Python writer
   `treasury/currency.py:set_exchange_rate` (append-only, same table). → L2.3 = **expose a REST endpoint
   over the existing `set_default_rate` + RECONCILE/dedup the treasury duplicate**, NOT green-field.
4. **Both stage branches merge cleanly** onto current main (0 conflicts via merge-tree), each only **1
   commit behind** (the trivial test-PG `settings/base.py` line). Low-friction graft. App sizes:
   so_quotes app = **37 files**, treasury app = **15 files** (the DRAFT's "58/34" were full diffstats
   incl `_factory/` docs).

---

## 2. FACTORY MECHANICS — exact contracts my JSON must satisfy

**Seeding model:** `cli seed-phases <macro-plan.json>` seeds **phases only** (one tx: phase rows PENDING
+ phase `dag_edges` + 1 `macro_plan` ref + `phase_seeded` events; idempotent). **Stages are NOT
CLI-seeded** — they materialize at RUNTIME in phase PLANNING→CONTRACTS_FROZEN→RUNNING, ingesting a
`phase-plan.json` (`scheduler._step_planning`/`_step_ingest`). See §5 — the landing fork.

**Schemas (`artifacts.py`):**
- `macro-plan.json`: `{project:str, phases:[{id,name}], dag_edges:[[from,to]]}`, `extra=forbid`.
- `phase-plan.json`: `{stages:[{id,name,risk_class,acceptance, kind?, acceptance_criteria?, touched?,
  role?}], dag_edges:[[from,to]]}`, `extra=forbid`.
- Ids: `^[A-Za-z0-9][A-Za-z0-9._-]*$`, unique within plan, no `..`, no trailing `.` (feed branch
  names + dirs). Hyphens OK.
- `kind`: `"backend"|"frontend"|null`. `role`: `"contract"|"leaf"|null`. `acceptance_criteria`/`touched`:
  `list[str]|null`. `acceptance`: **mandatory dense prose DoD paragraph** (NOT a list — the list is the
  separate `acceptance_criteria`).
- File locations: macro-plan → factory repo `docs/projects/erp/macro-plan.json`. phase-plan →
  workspace worktree `_factory/phases/<phase_id>/phase-plan.{json,md}` (BOTH the .json sidecar AND a
  .md companion required, else ingest escalates).

**Risk classes (exactly 3) + what each triggers** (`factory.config.yaml`, `risk_classes`):
| risk_class | validator | code audits (post-build) | spec dual-audit | human gate | per-stage budget cap |
|---|---|---|---|---|---|
| `routine` | sonnet/max | **[] — NONE** | yes (opus+codex xhigh) | no | 30M |
| `structural` | opus/xhigh | dual (opus + codex xhigh) | yes | no | 250M |
| `critical` | opus/xhigh | dual (opus + codex xhigh) | yes | **YES (founder)** | 364M |
- Spec dual-audit (SPEC→SPEC_AUDIT→BUILD) runs at **ALL 3 classes**. routine has **no post-build code
  audit** — weakest verification. critical adds the founder human_gate ({approved|rework:BUILD|rework:SPEC}).
- Budgets are CAPS (runaway backstop), not spend targets.

**Small-stage size gate (`evaluate_stage_sizes`, mode=`warn` — reports, never blocks; honor anyway):**
- max_acceptance_criteria **7**, max_touched **6**, max_dependency_degree **6** (degree = in+out edges of
  the stage in this plan's dag_edges), min_acceptance_criteria **1**, min_touched **1**.
- Over-split floor (both AC<1 AND touched<1 → "under") — trivially satisfied; contract stages are
  floor-exempt anyway.
- ALWAYS emit `acceptance_criteria` + `touched` (a null axis logs a visible `skipped` finding).

**Contract-first reachability (`_assert_contract_reachable`, HARD — rejects in read_phase_plan):**
- Gated on presence: IFF any stage in the plan has `role=="contract"`, **every** non-contract stage in
  THAT SAME plan must be a DAG descendant (path in dag_edges) of some contract stage. No contract stage →
  check skipped. → Reachability is **INTRA-phase-plan**; cross-layer seams are handled by the macro DAG
  (phase deps require DONE), not by this check.

**Stage id → branch + socket:** factory namespaces plan-local id to `<phase_id>.<stage_id>` and the
branch is `stage/<phase_id>.<stage_id>` (I write the SHORT plan-local id; factory prepends the phase).
**The AF_UNIX socket-overflow root cause is FIXED on erp-workspace main** (`bb95800`: socket moved to
fixed-length `/tmp/sfpg-<12hex>/...`). **Short-ids are NO LONGER load-bearing for the socket** — but I
keep ids short anyway (readability + branch hygiene). Merge-gate loop-cap also landed
(`merge_gate_max_tier1_failures: 3`). → Both treasury-12×-loop root causes are closed.

**`proving_phases`** (`factory.config.yaml`): a PENDING phase whose id is NOT listed is dispatch-HELD
while ANY listed phase is non-DONE. Currently `[foundation, inventory-procurement]`. After re-seed MUST
be updated to new layer ids (or emptied) + restart, else nothing dispatches. Plan: set to
`[l0-shell]` (prove the new pipeline on the smallest layer before fan-out).

**Phase dag_edges + deps_done:** a dependent dispatches only when EVERY prereq phase is DONE; a dangling
edge (to an unseeded id) or a FAILED/CANCELLED prereq **permanently WAITs** the dependent. Order edges so
prereqs can reach DONE; never edge to an unseeded phase.

**kind → builder routing + frontend injection:** `kind:frontend` → opus build + UI/UX-laws injected
(`canon.inject.frontend=[ui_ux_laws]`, builder+validator+auditor, frontend stages only). `kind:backend`
→ codex **ONLY for routine** today (see §3 bug). `role` is plan-validation-only (NOT persisted, NO
runtime effect beyond the reachability check); `kind` IS persisted + drives routing/injection — set it on
every stage.

---

## 3. THE BUILDER-ROUTING BUG (backend structural/critical wrongly → opus, not codex)

`_builder_role(cfg, risk, kind)` builds candidate `builder_<kind>_<risk_class>` with the LITERAL
risk_class. Config defines only `builder_backend_routine` (codex) + `builder_backend_heavy` (codex) —
there is **no `builder_backend_structural`/`_critical`**. So for a structural/critical backend stage the
candidates `builder_backend_structural` → `builder_backend` → `builder_structural` all miss → it falls
to `builder_heavy` = **opus**. **`builder_backend_heavy` (codex) is configured but UNREACHABLE dead
config.** Documented intent ("backend → codex for capacity offload") is defeated for every non-routine
backend stage.

**Consequence for risk_class strategy WITHOUT a fix:** a false choice — codex-offload requires `routine`
(which has NO code audit), while dual-audit requires `structural`/`critical` (which routes to opus, NOT
codex). The founder wants BOTH (quality mandate: strong audit; capacity mandate: codex-offload). The
config can't deliver both for the same backend stage.

**Fix (surgical, aligned with documented intent):** in `_builder_role`, map risk→builder-tier
(`routine→routine`, `structural|critical→heavy`) when building candidate 1, so structural/critical
backend resolves to `builder_backend_heavy` (codex). Then backend stages get **codex build + dual audit**
— exactly the founder's intent. ~2 lines + a test. Class = pre-reseed correctness fix (like the
playbook §7 fixes). PLAN: fix on a branch, dual-audit, test, surface to founder; lean toward merging
(clean bug vs documented intent) but flag it. → Task #5.

---

## 4. RISK_CLASS + KIND STRATEGY (decision rules for authoring, ASSUMES §3 fix lands)

- `kind`: backend stage = server/REST/models/migrations; frontend stage = React screens. Back/front
  split is a HARD rule (separate stages). Frontend stages auto-get UI/UX laws + the founder visual gate.
- `risk_class`:
  - **`critical`** — money/posting/auth/rights correctness + irreversible engine paths + the founder must
    personally gate. FINAL set (6): `contract-rest` (money/legal Contract clean()), `docs-lifecycle-api`
    (storno/edit engine exposure), `payment-producers` (R5/R6/R8 postings), `payment-allocation`
    (allocation vs accepted Conts + conformity), `config-rights-rest` (rights-mutation backend = the
    security fault line), `users-rights-fe`. Founder human_gate is the per-stage verification.
    NOTE (audit reconciliation): OwnPJ REST + the exchange-rate write path are **structural, NOT critical**
    — they are master-data CRUD / an append-only rate journal (dual audit is proportionate); the founder
    gates actual money MOVEMENT (payments/allocation) and the legal Contract + rights planes, not every
    money-adjacent master-data write.
  - **`structural`** — substantive NEW backend/frontend with cross-stage seams or non-trivial logic:
    parties API (Counterparty/Contract), the generic-CRUD framework, ZN core, cont/quote editor,
    most operational screens. Dual code audit + opus validator. (Backend → codex after §3 fix.)
  - **`routine`** — thin re-verify-only KEEP stages (prove an existing endpoint in reality) + trivial
    config-only fan-out (instantiate catalogs from an existing framework). NO code audit — acceptable
    only because there's little/no new code. Backend routine → codex; frontend routine → opus.
- Per-layer **founder-verification gate**: realized either by marking the layer's terminal frontend
  stage `critical` (mechanical human_gate) OR by the architect lifting drain per-layer after the founder
  tests the deployed layer (playbook §5.9). Decide per layer during authoring; prefer the mechanical
  human_gate where a natural capstone screen exists.

---

## 5. THE LANDING-MECHANISM FORK (how architect-authored stages reach the factory)

`_step_planning` ALWAYS spawns `phase_architect` to AUTHOR the phase-plan at runtime (scheduler.py:4511)
— there is **NO pre-place/ingest path**; pre-placing is flagged UNVERIFIED in the reseed playbook §6. The
redesign's "phase_architect narrows to contracts/spec, not runtime stage-generation" was **Proposed,
awaiting founder confirm — NOT built**. So my authored structure has no mechanical landing path TODAY.

**Options (the #1 founder decision on return):**
- **(A) Mechanical ingest-wiring (RECOMMENDED).** Modify `_step_planning`: if a ratified
  `phase-plan.json` is pre-committed at the canonical path, VALIDATE (read_phase_plan) + ADOPT it
  verbatim and freeze contracts — skip agent stage-generation. Makes the dual-audited structure the thing
  that actually runs = the mechanical guarantee the founder repeatedly demands. Cost: a bounded
  pipeline-semantics change (must be dual-audited + tested). It IS a structural pipeline change that was
  only "Proposed" → do NOT merge unilaterally while he's offline; design + (capacity permitting)
  prototype on a branch so it's one-approval-away.
- **(B) Prompt-adopt (no code change).** Commit the authored phase-plan + enrich `_planning_prompt` to
  "a ratified plan exists at this path — adopt verbatim, do not re-derive; write the .md; freeze
  contracts." read_phase_plan validates structure; the proving-ground checkpoint (stop after PLANNING,
  review, resume) catches deviation. Lower friction, but agent-mediated = NOT a mechanical guarantee
  (the agent could deviate). Conflicts with the founder's mechanical-guarantee value.

→ Author the STRUCTURE (identical content either way) now; resolve the fork as a founder decision. Task #4.

---

## 6. LAYER → PHASE MAPPING (10 phases, short ids; linear macro DAG l0→…→l9)

| phase id | layer | kind mix | notes |
|---|---|---|---|
| `l0-shell` | L0 navigation shell + module registry | FE | menu-registry contract; mount orphans; proving phase |
| `l1-nomencl` | L1 root money + classification nomenclatures | BE re-verify + FE | generic-CRUD framework contract; instantiate catalogs |
| `l2-money-base` | L2 OwnPJ + CashDesk/BankAccount + DefaultExchangeRate | BE+FE | parties/api.py slice 1; exchange-rate REST (reconcile) |
| `l3-parties` | L3 Counterparty + Contract | BE+FE | biggest NEW backend (parties API) |
| `l4-catalog` | L4 parts catalog + production nomenclatures | BE re-verify + FE | management UIs around existing REST/PartsPicker |
| `l5-engine-docs` | L5 Vehicle + engine re-verify + documents-lifecycle API | BE+FE | documents/api.py contract (storno/edit/history) — keystone |
| `l6-stock-ops` | L6 inventory & procurement operations surface | BE re-verify + FE | mostly UI + lifecycle verbs; order-to-stock REBUILD |
| `l7-service-orders` | L7 cont de plata (quote) + ZN | BE land + FE | graft cont-quote-core branch (API EXISTS); editor UI NEW; ZN NEW |
| `l8-treasury` | L8 payments operations | BE+FE | graft treasury branch (RE-SLOT); producers + screens NEW |
| `l9-config` | L9 config / users / rights | BE+FE | LAST; may stay partly Django-admin v1 (founder call) |

Macro DAG: linear `l0-shell→l1-nomencl→…→l9-config` (each layer founder-testable before the next; the
methodology forbids building a later layer before the prior is verified — within a layer, stages
parallelize via the phase-plan DAG). Reconsider only if a clearly-independent layer pair warrants
parallelism (none obvious — the dependency chain is real).

---

## 7. AUTHORING CONVENTIONS (binding for every per-layer phase-plan)

- **Back/front split:** every UI-bearing capability = a backend stage + a SEPARATE frontend stage.
- **Contract-first per layer:** if the layer has fan-out (≥2 leaves building on a shared seam), include
  ONE `role:contract` stage that freezes that seam (thin: the interface/types/endpoint shape), and make
  every other stage in the layer a DAG descendant of it. A layer with a single linear chain may set the
  head stage `role:contract`. Contract stages are floor-exempt (may be thin) but still ≤7 AC / ≤6 touched.
- **Marks** (carry from DRAFT, re-verified): NEW / KEEP(re-verify) / REBUILD(starting-point) /
  RE-SLOT(moved). KEEP-only stages = `routine` (prove existing endpoint/UI in reality). NEW substantive =
  `structural`/`critical` per §4. State the mark in the stage `name` or `acceptance`.
- **Fields per stage (ALL required for new-shape):** `id` (short, kebab, unique-in-plan), `name`
  (`[mark] one-line scope`), `risk_class`, `acceptance` (dense prose DoD paragraph — mirror the
  foundation phase-plan voice), `kind`, `acceptance_criteria` (≤7 crisp checkable items), `touched`
  (≤6 files/components/endpoints), `role`.
- **Standing acceptance items (factory law):** every operational FE stage MUST expose
  edit/cancel/storno/history (the create-only pattern never recurs). Every picker FE stage MUST support
  "add-new inline". Soft-delete = deactivate (PUT `active=false`; never PATCH/DELETE — they 405).
- **Size:** ≤6 dependency-degree (in+out) per stage — keep intra-layer DAGs shallow; if a stage needs
  >6 edges, split or restructure. Keep each stage to one builder pass.
- **Use the REAL nomenclature keys** (`generic_parts_catalog` etc.); authenticate before asserting an
  endpoint is unmounted (unregistered device → 403 before routing).

**Reference template — the real foundation phase-plan shape (mirror density/voice):** stage =
`{"id","name","risk_class","acceptance"}` with `acceptance` a single dense paragraph; dag_edges are
`[from,to]` of plan-local ids. The new structured fields (`kind`,`role`,`acceptance_criteria`,`touched`)
were only ever in test fixtures — this structure is the first real use.
