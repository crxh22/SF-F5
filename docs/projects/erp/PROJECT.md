# PROJECT — ERP Elita-9 (autoservice management accounting)

**Status: v1.0 — RATIFIED by the founder at the intake interview, 12-06-2026 (D-ERP-0001 in `decision-log.md` here).**
**Owner:** Main Architect. **Governed by:** `00 - DOCTRINA.md`; pipeline per `_FRAMEWORK_MVP_DoD.md`.

## 1. Mission and source of truth

Replace 1C Dalion as the management-accounting system for the founder's autoservice business (directions: `body`, `service`), per the finalized business documentation in **`/home/artur/projects/ERP-start`** (the `docs_repo` of `projects.erp` in `factory.config.yaml`).

That repository is the **single source of business truth** (Doctrine §9). This file is an index and a plan — it restates nothing it can point to:

| Question | Canonical answer (paths repo-root-relative in ERP-start) |
|---|---|
| Architecture decisions | `docs/Business/adr/0001…0006` (all Accepted) |
| Stack | ADR-0002: Django + PostgreSQL + DRF, React (TS) + Ant Design behind a project-owned UI abstraction, Celery+Redis, WeasyPrint/ReportLab |
| Document catalog (35 types) | `docs/Business/blueprint/document-catalog.md` |
| Registers (R1–R18 + 15A/B + config) | `docs/Business/blueprint/register-model.md` |
| Which document posts which register | `docs/Business/blueprint/document-effect-matrix.md` + per-register producer lists — **NORMATIVE** (ADR-006 P0) |
| Status layers (7) | `docs/Business/blueprint/status-layer-model.md` |
| Business parameters | `docs/Business/blueprint/parameters.md` (values = config, never constants) |
| Domain behavior | `docs/Business/domain/*.md` (12 files) |
| 1C migration scope | `docs/Business/migration/1c-mapping.md` (field mapping awaits founder's export decision) |

Reference pin: ERP-start commit `51e32b0` (documentation decision pass closed 11-06-2026). Doc changes after this pin propagate via Doctrine §19, never silently.

## 2. Phase map (the macro plan — `macro-plan.json` is the machine form)

```
                        ┌────────────────────────┐
            ┌──────────►│ inventory-procurement  ├──────────────┐
            │           └────────────────────────┘              ▼
┌───────────┴┐          ┌────────────────────────┐        ┌───────────┐     ┌──────────────┐
│ foundation ├─────────►│ service-orders         ├───┬───►│ reporting ├────►│ migration-1c │
└───────────┬┘          └────────────────────────┘   │    └───────────┘     └──────────────┘
            │           ┌────────────────────────┐   │          ▲
            └──────────►│ treasury-payments      ├───┤          │
                        └────────────────────────┘   ▼          │
                                                ┌─────────┐     │
                                                │ payroll ├─────┘
                                                └─────────┘
```

Three phases run in parallel after foundation (DoD §12.C10 requires ≥2). Phase-level fan-out **execution** is first production use; the plan above is the C10 planning artifact.

### foundation — the frozen shared core
Everything every other phase codes against. The phase BUILDS the core; the cross-phase contracts v1 are frozen **by the Main Architect at foundation sign-off** from the as-built state (§3) — foundation never edits the cross-phase contract files itself.
- Workspace skeleton: Django+PostgreSQL backend, React+TS frontend (monorepo `backend/`+`frontend/`), test harness (`scripts/test.sh` becomes real: `uv run pytest`), CI-grade lint/type config, **UI foundation**: design system (style tokens/theme in one place) + the ADR-0002 component abstraction layer (zero direct `antd` imports) + per-user view-preferences mechanism (storage + read pattern; the concrete preference set is deferred by founder decision — `docs/Business/domain/technical-context.md` §UI/UX Requirements).
- Core schema: counterparty, contract (all types as data: service, angajat, vânzare în rate, achiziție TVA, outsource), own-PJ (+ VAT flag), vehicle, user + configurable rights + device registry (actors-and-access.md v1 auth: device_token + session cookie), nomenclature framework (R16/R17), unified ticket entity (R14), system tax counterparties per PJ, parameters registry (`docs/Business/blueprint/parameters.md` as config), **cont-de-plata / ZN / ZN-line skeleton entities** (identity + the cross-phase-read surface per contract C3 §10; behavior and flows belong to service-orders).
- Document engine: base document model (draft/final, managerial+official dates, fiscal-mode inheritance), per-type audit trail + central index (R11), storno engine (ADR-006: edit = reverse+repost, cancellation = zero-effect version), dependency graph + atomic cascade (ADR-005), status-layer framework (stable keys, configurable labels, transition-date fields with backward clearing), **notification framework** (per-document-type rules + personal-cabinet inbox — actors-and-access.md edit-rules consumers).
- Posting engine + register infra (ADR-003/006): movement tables for R1–R18 per register-model dims, posting API with idempotency (P4), pe-alb/negru TVA precondition, period-snapshot **storage shape** (snapshot tables + close/reopen workflow land in reporting — C1 freezes the shape so nothing drifts), fiscal-invoice number **machinery** (15A/15B registers, diapazon + e-factura lifecycles; producer-side flows live in the domain phases).
- Cross-cutting output subsystems: **print/PDF framework** (WeasyPrint/ReportLab base + customizable per-document-type templates — warehouse issue, custody acts, quote printouts all need it before reporting exists; domain phases own their concrete forms), **media/attachment subsystem** (filesystem→S3 abstraction, photo model linked to documents/events, `photos_expected_per_event` parameter).
- Risk classes here: schema + posting engine + auth = `critical`/`structural`; skeleton/scaffolding = `routine`.

### inventory-procurement ∥ — stock truth
Two-layer parts catalogs (progressive resolution, enrichment-at-reception, code search — the tables themselves are foundation R16 entries), depozite, ordering (comanda furnizor + stock-availability warning), reception (document de achiziție: R2/R3/R6/R9A/R12 producer), supplier fiscal invoice linkage (15A producer + VAT-attribute upstream cascade, E4), warehouse issue + painter issue (R7 painter-debt producer, E9), return-from-executor / retur furnizor (+ its fiscal form, 15B) / retur de la client (+ form, 15B), stocktaking document, reservation flows (auto/manual/reassign/remove + redistribution at ZN-line split — consumer side of E10), overdue-parts detection + overdue-part tickets (R12/R13 feed), negative-stock policy + retroactive chronological negativity check, readiness auto-update producer (E11).

### service-orders ∥ — the operational spine
Defectare flow (2-cont model + conversion window), cont de plata (statuses, re-coordination, discounts, tip_cont: obișnuit / defectare / vânzare_piese / revânzare_tva), ZN + verification (left-right UI) + cont→ZN splitting + **ZN-line quantity split (sole owner; reservation-redistribution seam = E10)** + etape/departamente/lucrări + scheduling, custody acts + photos + mileage (R1 producer), vehicle-release procedure → sale document (R2 release / R3 consume / R6 debt / R9A / R10 4%-accrual / R18 producer) + act predare, incomplete delivery + restant chains, expense-only closure, supplementation document, upsale detection/marking, outsource confirmation on ZN (producer side of E12), sales fiscal invoice (15B producer) + accounting daily working list, MVP stage-tracking incorporation (technical-context.md).

### treasury-payments ∥ — money truth
Încasare / plată către contragent / plată directă cheltuieli / convertare (+ clearing subtype with commission), payment-allocation model (1..n portions per payment), avans neutilizat + redistribution action, cash desks & bank accounts seeding (organizations-and-treasury.md), currency model (MDL base + default-rate registry), advance-VAT producers (R9A/B), state payments (plata stat) + **monthly income-tax provision action + year-end correction (R10)**, alb/negru conformity tickets, reference-bonus pro-rata accrual on allocation, service acquisition / service sale documents (outsource ZN-linked use via E12 + the Method-1 confidential leg — separate access scopes), reciprocal settlement act, reconciliation/balance-correction document.

### payroll — after service-orders + treasury-payments
Contract angajat + composable algorithm framework (trigger/selection/period/parameters all configurable), payroll calculation document (R7+R6 producer; weekly Wed–Tue default), salary-eligibility consumption (earliest of ready_for_release/released), payouts with deductions (tool installments, painter debt, overpayment withholding), ZN-reopen recalculation (atomic storno+new), on-time-bonus + upsale + team-upsale algorithms, executor personal cabinet (mobile).

### reporting — after the four above
R01–R21 + KPI widgets (management-accounting-and-reporting.md is the catalog: maximal v1, founder-confirmed), sinecost projection, WIP, P&L/balance/cash-flow + forecast, TVA management + threshold monitor + divergence, M1/M2 monitoring (contract-level metrics), expense-distribution mechanism, period-snapshot tables + close/reopen workflow (ADR-003 amendment; storage shape frozen in C1), report rendering over the foundation print framework.

**OPEN DECISION — snapshot acceleration axis** (surfaced 14-06-2026 from `foundation.register-schemas` RS-AUDIT-02, the intended C1 §6/§8 seam pointer; owner: **reporting Phase Architect**; deferred per Doctrine §12). Snapshot-accelerated `balance()` is currently **axis-agnostic** — it fires on dim-match alone, matching the frozen snapshot shape (C1 §6 / F3 §6: period + dims + balance columns; spec §3.9 as written). This is correct for foundation. Reporting must decide one of: (a) add an **axis discriminator** to the `<table>_snapshot` shape so official vs managerial balances are distinguishable at the storage level — a **C1 contract change**, Main-Architect-versioned + re-synced per §3; or (b) adopt a **managerial-only-snapshots** policy (official balances always pure-sum, snapshots never authoritative for official figures) — **no schema change**. Deciding trigger: when reporting's snapshot/close semantics are designed. No contract change until then.

### migration-1c — last, founder-gated
Per `migration/1c-mapping.md` decided scope: cleaned master data, opening-balance documents per register family (atomic batch + reconciliation report), open operational documents, condensed vehicle history, ≥1y settlement history; cutover after a 1C month close; doubles as the first full ERP calculation test. **Blocked on the founder's 1C export decision — blocks nothing else.**

## 3. Cross-phase contracts (drafts in `contracts/`, frozen at foundation exit)

| Id | Contract | Parallel phases code against it |
|---|---|---|
| C1 | `c1-registers-and-posting.md` — register schemas, posting API + idempotency, stock lot/reservation semantics, settlement types, TVA movement vocabulary, effect-matrix conformance rule | all |
| C2 | `c2-document-engine.md` — document lifecycle, versioning/audit, storno, dependency cascade, status-layer framework | all |
| C3 | `c3-core-schema.md` — counterparty/contract/PJ/vehicle/rights/nomenclature/ticket schemas | all |
| C4 | `c4-domain-events.md` — cross-domain events: salary eligibility, release, payment allocation, VAT-attribute cascade, reference accrual, fiscal-number lifecycle | producers ↔ consumers per file |

**Canonical home (Doctrine §9):** from the workspace-bootstrap commit on, the contracts live canonically in `erp-workspace/_factory/contracts/` (the only place the Tier-2 gates read); `docs/projects/erp/contracts/` is replaced by a one-line pointer in the same ratification commit. The v1 freeze at foundation sign-off = a Main Architect commit in the workspace + a `D-ERP` decision-log entry. Phases write their intra-phase contracts under `_factory/contracts/phase-<id>/` — the root namespace is never edited by a phase.

Contract changes after freeze = versioned by Main Architect + re-sync of affected phases (DoD §5.2 Prevent) — never edited in place by a phase.

## 4. Risk-class guidance for Phase Architects (DoD §7 defaults)

`critical` (human gate): anything touching money/tax posting correctness (posting engine, sale document, TVA/income-tax producers, payroll formulas, fiscal-invoice numbering, M1/M2), access control/device auth, migration cutover, irreversible schema changes on live data. `structural`: shared schemas, document engine, register dims, cross-module flows. `routine`: bounded UI, list views, print forms, isolated CRUD with full test cover.

## 5. Standing constraints

- Configurability is law: every business value from `blueprint/parameters.md` reads config/nomenclature — a hardcoded business constant is a defect (Doctrine §14; founder doctrine P14).
- Atomicity is law: the operations-overview transactional-consistency rule + effect-matrix items 1–26 are acceptance criteria, not advice.
- Glossary identifiers are canonical (`glossary.md` column 2): `cont_de_plata`, `zn`, `sinecost`, `casa_cec`… — never re-translated.
- Founder-facing language: Romanian (conventions.md); code/identifiers/docs: English.
- UX-first is law for UI stages (founder, 12-06-2026): a UI-bearing stage's SPEC starts from the user's real problem, the flow they traverse, and scenario simulations — UI design only after. Phase Architects encode this in the acceptance criteria of every UI-bearing stage; all UI goes through the design system + abstraction layer, never direct library imports (`technical-context.md` §UI/UX Requirements).
- Workspace: `/home/artur/projects/erp-workspace`, monorepo, integration branch `main`, Tier-1 command `bash scripts/test.sh` (stable indirection; the config value `projects.erp.test_command` lands as an explicit deliverable of the D-ERP-0001 ratification — OPEN-2).

## 6. Open at draft time (interview agenda items)

1. Phase map confirmation (3-parallel variant vs conservative 2-parallel).
2. Foundation scope boundary (this draft's list).
3. `projects.erp.test_command` confirmation (config edit = D-ERP-0001 deliverable).
4. M1/M2 placement nuance (treasury vs service-orders split as drafted).
5. Fan-out timing after foundation: the full plan is seeded up front (C10 in the DB DAG), and the `proving_phases` dispatch hold keeps post-foundation dispatch to inventory-procurement until it completes (DoD §15.3 order) — confirm or adjust the hold list.
6. One-time PLANNING checkpoint: the first real phase plan (foundation) gets an operator review between PLANNING and dispatch (stop/review/resume) — proving-ground validation of the planning mechanism itself.
7. Start signal for `seed-phases` + first `cli run`.
