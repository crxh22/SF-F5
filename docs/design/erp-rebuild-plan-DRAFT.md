# ERP Rebuild / Replan — DRAFT (dependency-ordered, founder-testable layers)

**Status:** REFINED by ARH-01 (22-06-2026), dependency graph **independently code-verified** (a fresh subagent re-checked every load-bearing claim against the real `erp-workspace` code — see PART 6). Pending founder final approval + reference apps. Methodology approved by founder:
build in **dependency-ordered, founder-testable LAYERS** (not by domain category); master-data /
nomenclatures first (money nomenclatures included at the BASE); each layer testable + founder-VERIFIED
before the next; back/front split into separate stages; navigation is a living "Layer 0" registry;
config/users/rights is the LAST module.

**Inputs:** gap audit (`docs/research/erp-gap-audit-22-06-2026.md`), UI/UX concept + 5 mechanisms
(`docs/design/ui-ux-concept.md`), and the REAL data model read from
`erp-workspace/backend/apps/*/models.py` (read-only) plus the two in-flight backend branches
(`stage/service-orders.cont-quote-core` = `so_quotes`; `stage/treasury-payments.treasury-app-foundations` = `treasury`).

> **Marking legend (per stage):**
> **NEW** = build from scratch · **KEEP** = existing, re-verify only (no rebuild) · **RE-SLOT** =
> existing base work physically moved/re-sequenced earlier · **REBUILD** = existing work taken as a
> STARTING POINT, re-verified and corrected/completed to fit the new reality (new deps, UI/UX rules,
> prior defects). Nothing existing is assumed good — KEEP still means "re-verify in reality".

---

## PART 1 — The REAL entity dependency graph (derived from FK relationships)

Read from the actual models. `→` = "FK-depends on" (PROTECT unless noted). Cross-app lazy-string FKs
resolved. Abstract bases that other apps subclass are noted because they drive build order even though
they carry no row.

### 1.1 Tier table (depth = longest FK chain to a root)

| Depth | Entity (app) | FK-depends on |
|---|---|---|
| **0 (roots, no business FK)** | `User` (accounts) | — (Django AbstractUser) |
| | `Currency`, `Bank`, `Direction`, `CashDeskType`, `BankAccountType`, `BankAccount`†, `Stage`, `StageState`, `GenericPart`, `ContractType`, `StatusLabel`, `RightsTemplate`, `TicketType` (nomenclature/tickets) | — (NomenclatureBase only) |
| | `Parameter` (+`ParameterHistory`) (nomenclature) | → User (audit only) |
| **1** | `OwnPJ` (parties) | — (but `save()` spawns system `Counterparty` rows) |
| | `ExpenseCategory` (nomenclature) | → Direction |
| | `PaymentCategory` (nomenclature) | — (has its own `direction` enum, NOT the Direction table) |
| | `PriceLevel`, `Diapazon`-cols (nomenclature) | → (PriceLevel: none; Diapazon: OwnPJ) |
| | `SpecificPart` (nomenclature) | → GenericPart |
| | `Document`, `DocumentVersion`, `AuditIndexEntry`, `DocumentLink` (documents) | → User |
| **2** | `Counterparty` (+`CounterpartyRole`) (parties) | → OwnPJ (system rows only; commercial rows have no FK) |
| | `CashDesk` (nomenclature) | → CashDeskType, Currency, OwnPJ |
| | `BankAccount`† (nomenclature) | → Bank, BankAccountType, Currency, OwnPJ |
| | `DefaultExchangeRate` (nomenclature, append-only R16) | → Currency, User |
| | `Department` (nomenclature) | → Stage (M2M base/foreign) |
| | `Work` (nomenclature) | → Stage (M2M) |
| **3** | `Warehouse` (nomenclature) | → Counterparty (responsible, nullable) |
| | `Executor` (nomenclature) | → Counterparty (O2O, nullable), Department |
| | `Vehicle` (parties) | → Counterparty (owner) |
| | `Contract` (parties) | → Counterparty, ContractType, OwnPJ, CashDesk, BankAccount, PriceLevel, PaymentCategory×2 |
| | `Device`/`DeviceSession`/`DeviceAudit`, `Right`, `ViewPreference` (accounts) | → User (+Device) |
| | `Lot` (inventory) | → SpecificPart, Warehouse, OwnPJ, Document |
| **4 (engine: movements/registers — write-only)** | `MovementBase` subclasses R1–R10/R18 (registers) | → Document (+ reversal self) + their dims (Vehicle, SpecificPart, Warehouse, Executor, CashDesk, BankAccount, Currency, Counterparty, Contract, PaymentCategory, Direction, ExpenseCategory, OwnPJ) |
| | snapshots (registers), `RegistersPeriodState` | → same dims; PeriodState → User |
| | `ProcurementTracking` (R12), `DeliveryFollowUp` (R13) | → Document, Counterparty, Vehicle |
| | `SupplierFiscalInvoice` (R15A), `IssuedFiscalInvoice` (R15B) (fiscal) | → Counterparty/OwnPJ, Document, Diapazon |
| **5 (L1 service-order skeletons — frozen columns)** | `Cont`, `ContLine`, `ContReferenceContributor` (service_orders) | → Contract, Vehicle, Counterparty×n, GenericPart, SpecificPart, Executor |
| | `Zn`, `ZnLine` (service_orders) | → Cont, ContLine, GenericPart, SpecificPart, Executor, Counterparty |
| **6 (operational documents — DocumentBacked)** | `SupplierOrder`, `PurchaseReception`, `SupplierFiscalInvoiceDocument` (procurement) | → Document base + nomenclature/parties dims; reference `ZnLine`/`Cont` by **BigInteger ref, NOT FK** (layering) |
| | `WarehouseIssue`, `PainterMaterialIssue`, `ReturnFromExecutor`, `ReturnToSupplier`, `ReturnFromClient`, `*FiscalInvoice`, `InventoryDocument` (stocktaking), `ReservationAction` (inventory) | → Document base + dims |
| **7 (top layer — commercial/treasury extensions)** | `ContCommercial`, `QuoteLine`, `ContAuditEntry` (`so_quotes`) | → Document base, `service_orders.Cont`/`ContLine` (O2O), User |
| | `Payment` (abstract), `ConformityTicket` (`treasury`) | → Document base, CashDesk/BankAccount, PaymentCategory, Counterparty, Currency-derived, `service_orders.Cont` |
| | `Ticket` (tickets), `Attachment`, `Notification`, `PrintTemplateOverride`, `StatusTransitionLog` (status_layers) | cross-cutting → User/Document/TicketType (GFK where layering forbids FK) |

† `BankAccount` reads as depth-0 by class but its **required** FKs (Bank, Currency, OwnPJ) make it
effectively depth-2 — listed at both for clarity. It must be built AFTER Bank/Currency/OwnPJ.

### 1.2 The load-bearing chains a founder will hit (why order matters)

- **To create one Contract** you need: ContractType, OwnPJ, Counterparty, (optionally CashDesk/BankAccount → which need CashDeskType/BankAccountType/Bank/Currency/OwnPJ), PriceLevel, PaymentCategory×2. → Contract is a **deep** master-data node; everything money-nomenclature sits beneath it.
- **To create one Cont de plata (quote)** you need a Contract (above) + optionally a Vehicle (→ Counterparty) + GenericPart/SpecificPart + Executor (→ Department → Stage; Counterparty). → the entire master-data + money-nomenclature base must exist first.
- **To take one Payment** you need a CashDesk or BankAccount + PaymentCategory + Currency + (DefaultExchangeRate for non-MDL) + Counterparty + a Cont to allocate against. → money nomenclatures at the BASE; money OPERATIONS late.
- **The engine (documents + registers + fiscal numbering)** sits UNDER every operational document but ABOVE the dims it references. It is built (foundation) and is re-verified, not rebuilt.

### 1.3 Two structural facts that shape the layering

1. **`parties` (Counterparty / Contract / Vehicle / OwnPJ) has NO REST API** — no `api.py`, not mounted (`backend/erp/urls.py`). This is the single biggest **backend** hole in the master-data base: it must be built NEW (serializers + viewsets) before any parties management UI. Every nomenclature, by contrast, already has generic CRUD REST + admin (the `@nomenclature` registry) — so nomenclature layers are **frontend-mostly**.
2. **Layering forbids FK up the stack** (`registers`/`procurement` reference `service_orders` by BigInteger reference, not FK; treasury/so_quotes are the top layer that may reference both). This is already enforced by import-linter and is correct — the rebuild must preserve it (no new upward FKs).

---

## PART 2 — Dependency-ordered LAYERS (Layer 0..N)

Each capability = a **backend stage** + a **separate, small frontend stage** (hard rule). Stages are
sized well under one builder pass. "Add-new-inline from any picker" (UI/UX #3) is a standing
acceptance item on every frontend stage that renders a picker. Every frontend stage runs the UI/UX
gate: §2 questionnaire → mechanical token/component guards → visual self-loop → founder visual review.

> Notation: **[BE]** backend stage · **[FE]** frontend stage. Each marked NEW/KEEP/RE-SLOT/REBUILD.

---

### LAYER 0 — Navigation shell + module registry (the living surface)
*Delivered:* a real app shell with a **menu config/registry** (single source, not scattered links),
a Master-Data / Nomenclatoare section scaffold, and the **mount of the orphaned** `IssueCounter` /
`ReturnCounter` routes (gap G7). Grows incrementally as every later layer registers its screens.

| Stage | Mark | Scope (one line) |
|---|---|---|
| 0.1 [FE] menu registry | **REBUILD** | Replace the flat `appRoutes` array + `Nav` (`shell/routes.tsx`, `AppShell.tsx`) with a typed, sectioned **menu registry** (groups: Operațiuni / Nomenclatoare / Date de bază / Config) that screens self-register into; keep `AppLayout`/tokens. |
| 0.2 [FE] mount orphans + home | **REBUILD** | Mount `warehouse-issue/IssueCounter` + `returns/ReturnCounter` on real routes; turn the empty `HomePage` stub into a minimal landing/launcher. |

**Founder-verification gate L0:** founder opens the app, sees a structured menu with empty
Nomenclatoare/Date-de-bază sections, and can navigate to the (previously unreachable) issue/return
screens. No data yet — this gate is purely "the frame and the menu are real and modular".

---

### LAYER 1 — Root money + classification nomenclatures (no-dependency catalogs)
*Delivered:* the depth-0/1 catalogs every money and commercial flow needs, each with full CRUD UI +
add-new-inline: **Currency, Bank, Direction, ExpenseCategory, PaymentCategory, CashDeskType,
BankAccountType, PriceLevel, ContractType**. Backend REST already exists (nomenclature registry) — so
this is mostly frontend, on a **generic nomenclature CRUD screen** driven by config.

| Stage | Mark | Scope |
|---|---|---|
| 1.1 [BE] nomenclature REST re-verify | **KEEP** | Re-verify generic `/api/nomenclature/<key>/` CRUD (create/update + **soft-delete = `active=false`; DELETE/PATCH return 405 by design**, so every "delete" affordance means *deactivate*) + field metadata for these keys; confirm `direction`/`requires_pj` enums surface. No new code expected — prove it in reality. |
| 1.2 [FE] generic CRUD framework (skeleton, on ONE entity) | **NEW** | Build the ONE reusable, config-driven catalog screen (list + create/edit drawer + deactivate + "add new" entry point) and prove it on a SINGLE entity (Currency). This is the **visual tone-setter** — runs the full UI/UX gate against the founder's reference app. *Split from instantiation (verification flagged the combined stage as oversized vs the small-stage mandate).* |
| 1.3 [FE] instantiate L1 catalogs | **NEW** | Drive the 1.2 framework for the remaining Layer-1 keys (Bank, Direction, ExpenseCategory, PaymentCategory, CashDeskType, BankAccountType, PriceLevel, ContractType). Config-per-entity only; no new framework code. |

**Founder-verification gate L1:** founder creates a Currency (e.g. MDL, EUR), a Bank, two
PaymentCategories (one IN, one OUT), an ExpenseCategory, a CashDeskType (one `requires_pj=true`),
edits one, soft-deletes one — and sees them persist + sort + reappear in pickers.

---

### LAYER 2 — Own legal entities + money locations (the money base)
*Delivered:* **OwnPJ** (+ its auto-spawned system tax counterparties), **CashDesk**, **BankAccount**,
**DefaultExchangeRate** (append-only rate journal). These are the MONEY NOMENCLATURES the methodology
puts at the base. CashDesk/BankAccount depend on Layer-1 catalogs + OwnPJ; their `clean()` rules
(requires_pj, one-currency-per-desk) must surface as UI validation.

| Stage | Mark | Scope |
|---|---|---|
| 2.1 [BE] OwnPJ REST | **NEW** | `parties` has no API → build serializer + viewset for `OwnPJ` (create/edit/list; `vat_payer` flag), exposing that `save()` idempotently ensures system counterparties. First slice of the new `parties/api.py`. |
| 2.2 [BE] CashDesk/BankAccount re-verify | **KEEP** | Re-verify nomenclature REST for `cash_desk`, `bank_account`; confirm `clean()` (requires_pj / forbids_pj / one-money-location) is enforced through the API, not just admin. |
| 2.3 [BE] DefaultExchangeRate write endpoint | **NEW** | ⚠️ **Correctness fix (PART 6):** `DefaultExchangeRate` is NOT in the nomenclature registry and has **NO REST on `main`** — the only `set_exchange_rate` writer lives on the *unmerged* treasury branch (`currency.py`). Build a small append-only **create + history-list** endpoint (lift the `set_exchange_rate` logic from the treasury branch so treasury L8 reuses it). *Was wrongly marked KEEP.* |
| 2.4 [FE] PJ + money-location + rate screens | **NEW** | Screens for OwnPJ (read-only view of spawned TVA/Impozit counterparties), CashDesk, BankAccount, and a "set current exchange rate" form (calls 2.3, shows rate history). Picker validation mirrors `clean()` rules. |

**Founder-verification gate L2:** founder creates an OwnPJ (vat_payer on), confirms its `TVA …` /
`Impozit pe venit …` counterparties auto-appear, creates a CashDesk (blocked correctly when PJ
missing/forbidden), creates a BankAccount, sets an EUR exchange rate and sees it in the rate history.

---

### LAYER 3 — Commercial parties: Counterparty + Contract (the daily-clerk master data)
*Delivered:* **Counterparty (+roles)** and **Contract** management — the gap-audit's #1 alarm (clerks
touch these daily; today admin-only, no REST). Contract is a deep node (depends on Layer-1/2). This is
the first layer that needs substantial NEW backend.

| Stage | Mark | Scope |
|---|---|---|
| 3.1 [BE] Counterparty REST | **NEW** | `parties/api.py`: Counterparty + CounterpartyRole CRUD (commercial rows only — exclude/guard system_counterparty rows via `commercial()` queryset); role add/remove; supplier/client/employee/outsourcer filters. |
| 3.2 [BE] Contract REST | **NEW** | Contract CRUD with full `clean()` surfaced (alb⇒own_pj required, negru⇒forbidden; at-most-one money location; payment-category direction match). Read-only/derived fields documented. |
| 3.3 [FE] Counterparty screen | **NEW** | List + create/edit (multi-role chips), "add new" inline target for every counterparty picker app-wide (procurement, contracts, executors, warehouses…). |
| 3.4 [FE] Contract screen | **NEW** | Create/edit with conditional fields driven by alb/negru + money-location-XOR; inline "add new" for Counterparty/PriceLevel/PaymentCategory from within the form. |

**Founder-verification gate L3:** founder creates a commercial Counterparty with two roles, then a
Contract (alb) against it + an OwnPJ, watches the alb/negru validation block bad combos, sets default
money location + price level — and confirms the contract is now selectable where a quote will need it.

---

### LAYER 4 — Parts catalog + production nomenclatures
*Delivered:* the two-layer parts catalog (**GenericPart / SpecificPart** with OEM/aftermarket codes)
and the production nomenclatures (**Stage, StageState, Department, Work, Executor**). Backend REST +
the `PartsPicker` search already exist; this is the management UI that was "forgotten" (gap G2).
Warehouse + Executor depend on Counterparty (Layer 3) → correctly after it.

| Stage | Mark | Scope |
|---|---|---|
| 4.1 [BE] parts/production REST re-verify | **KEEP** | Re-verify `generic_parts_catalog`/`specific_parts_catalog` REST (incl. the `resolve_or_create_specific` race-safe path + original_code uniqueness) and `stages/stage_state/departments/works/executor` CRUD incl. M2M (department base/foreign works; work↔stages). |
| 4.2 [BE] Warehouse REST re-verify | **KEEP** | Re-verify `warehouse` CRUD incl. nullable `responsible` Counterparty FK. |
| 4.3 [FE] parts catalog screen | **NEW** | Two-layer catalog manager (Generic → its Specifics; OEM/aftermarket codes, manufacturer); reuses the generic CRUD framework + a parts-specific detail. Extends, does not replace, the existing search-only `PartsPicker`. |
| 4.4 [FE] production + warehouse screens | **NEW** | Stage/StageState/Department/Work/Executor/Warehouse screens (M2M editors for department↔stage and work↔stage; executor → counterparty + department + percent). |

**Founder-verification gate L4:** founder adds a GenericPart and a SpecificPart under it (with an OEM
code), creates a Stage, a Department mapping base/foreign stages, an Executor linked to a counterparty,
and a Warehouse with a responsible person — then finds them all in pickers.

---

### LAYER 5 — Vehicle (master data) + engine re-verification + document-lifecycle REST
*Delivered:* **Vehicle** management (interim master-data form; full intake/`act primire` stays with
service-orders), a **re-verification of the foundation engine** (documents + registers + fiscal
numbering) in reality, and — critically — the **`documents` REST surface** (gap G5: today the
storno/edit/cancel/cascade engine is callable-Python-only, no `documents/api.py`). Exposing it here
makes "edit / cancel / storno / view history" reusable by every later operational layer.

| Stage | Mark | Scope |
|---|---|---|
| 5.1 [BE] Vehicle REST | **NEW** | Vehicle CRUD (owner Counterparty, make/model/reg/mileage; custody/follow-up columns read-only here — transitions belong to service-orders). Completes `parties/api.py`. |
| 5.2 [BE] documents lifecycle API | **NEW** | Build `documents/api.py`: expose finalize / **edit (reverse-and-repost)** / **cancel (zero-effect storno)** / version-history / dependency-cascade over the existing engine. A standing contract every later doc-type plugs into. |
| 5.3 [BE] engine re-verify | **KEEP** | Re-run posting/register/snapshot/period + fiscal-numbering (R15A/B, diapazon) integration in the test ERP; confirm idempotency + write-guards hold. No rebuild — prove the "asset" is sound on this data. |
| 5.4 [FE] Vehicle screen + reusable doc-lifecycle controls | **NEW** | Vehicle screen + a reusable "document actions" UI (cancel/storno/edit/history) component the operational screens will mount. The create-only pattern stops here. |

**Founder-verification gate L5:** founder creates a Vehicle; then on an EXISTING procurement/inventory
document (from inventory-procurement) uses the new cancel/storno + history actions and sees the
reversal post correctly (registers net to zero). This is the first gate that proves the engine + the
new lifecycle verbs end-to-end on real screens.

---

### LAYER 6 — Inventory & procurement OPERATIONS surface (existing backend, finish the UI + verbs)
*Delivered:* the operational screens for the already-built inventory-procurement backend, now with the
Layer-5 lifecycle verbs, plus the **standalone order-to-stock** path (gap G4) and the parts-resale
prerequisite. Backend is DONE + merged → mostly re-verify + small extensions; frontend exists for
create-only and gets completed.

> **Data prerequisite (PART-6 verification):** the fiscal invoices here need **Diapazon** (fiscal
> number ranges per OwnPJ) to already exist. Diapazon's *management screen* is L9, but its **data must
> be seeded or admin-entered before L6** (it is already generic-CRUD + admin-registered). Gate add-on:
> a diapazon range exists for each issuing OwnPJ before the L6 fiscal-invoice flow is tested.

| Stage | Mark | Scope |
|---|---|---|
| 6.1 [BE] procurement/inventory re-verify | **KEEP** | Re-verify the merged ordering/reception/supplier-invoice/issue/return/stocktaking/reservation producers + their R2/R3/R6/R8/R9 postings on this data. |
| 6.2 [BE] standalone order-to-stock | **REBUILD** | Extend ordering+reception so a purchase can target own stock (not only a `ZnLine`) — unblocks `vanzare_piese`/`revanzare_tva`. Corrects the "ZN-only" assumption (gap G4). |
| 6.3 [FE] ordering/reception/invoice + lifecycle | **REBUILD** | Take the existing create-only ordering/reception/supplierInvoices screens and add edit/cancel/storno/history (L5 controls) + the order-to-stock entry point; align to new menu + UI/UX rules. |
| 6.4 [FE] issue/return/stocktaking + lifecycle | **REBUILD** | Same treatment for the (now-mounted) issue/painter-issue/return/stocktaking/reservation screens. |

**Founder-verification gate L6:** founder runs a full stock cycle on real data — order to stock,
receive, issue to workshop, return, stocktake — editing and cancelling at least one finalized
document, and confirms stock + money registers stay consistent.

---

### LAYER 7 — Service-orders core: Cont de plata (quote) + ZN
*Delivered:* the commercial spine. The backend `so_quotes` app (cont_de_plata document, header/lines,
**discount engine**, commercial + derived payment state) already exists on
`stage/service-orders.cont-quote-core` and **passed independent validation 75/75** — but it is
unmerged, has an **open contested audit finding (escalation #103/#104, unresolved_contest)**, and its
editor UI (§12) was deliberately **peeled to a sibling stage**. So: re-verify+land the backend
(resolve the contest first), then build the peeled UI, then ZN.

| Stage | Mark | Scope |
|---|---|---|
| 7.1 [BE] cont-quote-core land | **REBUILD** | Resolve escalation #103/#104 (approve / rework:BUILD / rework:SPEC — architect+founder decision, see Open Q), re-verify `so_quotes` (discount matrix Σ≡total, lifecycle, 62-day auto-refuse, R6-derived payment state, relink guard) against the NEW deps (Layers 1-5 now real, not fixtures), then merge. Starting point, not assumed good. |
| 7.2 [FE] cont/quote editor — header + lines | **NEW** | The peeled `cont-quote-editor-ui`, part 1: create/edit a quote header + the lines grid, with the part picker writing the L1 `ZnLine`. Runs the full UI/UX gate (an "important screen" for founder visual review). *Split from the discount/lifecycle controls (verification flagged the combined editor as oversized).* |
| 7.3 [FE] cont/quote editor — discounts + lifecycle | **NEW** | Part 2 on the 7.2 editor: discount-scope/method controls (Σ lines ≡ total), send-for-coordination / accept / refuse actions, and the lifecycle/history controls. |
| 7.4 [BE] ZN core | **NEW** | ZN + ZnLine behaviour over the frozen L1 skeletons (verification/procurement/production status layers, restant chain, cont→ZN line mapping). Plan-only today. |
| 7.5 [FE] ZN screen | **NEW** | ZN board/screen (status layers, lines, links back to its Cont); add-new-inline for parts/executors. |

**Founder-verification gate L7:** founder creates a real Cont de plata against a real Contract +
Vehicle, adds work/part/material lines, applies each discount scope/method and sees Σ lines ≡ total to
the cent, sends for coordination + accepts, then opens the resulting ZN and walks its statuses.

---

### LAYER 8 — Treasury & payments OPERATIONS (money operations — late, per methodology)
*Delivered:* the money OPERATIONS (payments, allocation/advance, conformity, currency conversion) that
the methodology explicitly puts LATE. The base plumbing already exists on
`stage/treasury-payments.treasury-app-foundations` (the abstract `Payment` DocumentBacked base,
currency-rate resolver, conformity helper, `ConformityTicket`) — backend-only, RE-SLOTTED here as the
foundation, then the concrete payment producers + UI built on top.

| Stage | Mark | Scope |
|---|---|---|
| 8.1 [BE] treasury-app-foundations | **RE-SLOT** | Land the existing treasury skeleton/layering/currency-rate-resolver/abstract `Payment`/conformity helper + `ConformityTicket`. Re-verify on real Layer-1/2 money nomenclatures (currencies, channels, rates) instead of fixtures. Base plumbing moved to its dependency-correct slot. |
| 8.2 [BE] payment producers | **NEW** | Concrete payment doc types (incasare / plata_contragent / plata_directa_cheltuieli / state_payment) registering on the abstract base; R5/R6/R8 postings; allocation/advance against accepted Conts (payment-state seam already in `so_quotes`). |
| 8.3 [FE] payment + allocation screens | **NEW** | Take-a-payment screen (channel→currency derived, category direction, rate prefill+override, MDL equivalent), allocate-to-cont UI, conformity-ticket surfacing; lifecycle/history controls. Important screens → founder visual review. |

**Founder-verification gate L8:** founder records a payment in a foreign-currency channel (sees the
MDL equivalent), allocates it against an accepted Cont, sees the Cont's derived payment state change,
and triggers an alb/negru mismatch to watch the conformity ticket appear.

---

### LAYER 9 — Config / users / rights management UI (LAST, by founder decision)
*Delivered:* the curated in-app surface for what is currently Django-admin-only and founder-only
(nomenclature/parameter/user/rights/company config). Per recorded decision (OPEN-CR1 / ADR-0002) this
is intentionally LAST and may even stay partly on Django admin for v1 — included here so the sequence
is complete; scope/extent is a founder call.

| Stage | Mark | Scope |
|---|---|---|
| 9.1 [BE] parameter + rights REST | **NEW** | Expose `Parameter` (history-preserving `set_param`) + curated rights endpoints (today only destructive superuser template ops exist) for an in-app config surface. |
| 9.2 [FE] config / parameters screen | **NEW** | Parameters editor (typed values, history), exchange-rate/diapazon admin, print-template overrides, and the residual config nomenclatures **StatusLabel + TicketType** (verification flagged these as otherwise unhomed). |
| 9.3 [FE] users + rights screen | **NEW** | User create/edit, device approve/revoke, per-dimension rights editor + template apply/copy (non-destructive). |

**Founder-verification gate L9:** founder edits a business parameter and sees history kept, creates a
second user with scoped rights, approves a device — all without touching Django admin.

---

## PART 3 — Per-layer founder-verification gates (summary)

| Layer | The founder personally tests… |
|---|---|
| 0 | Menu is structured + modular; previously-orphaned issue/return screens are reachable. |
| 1 | Create/edit/soft-delete root catalogs (currency, payment categories, expense category, cash-desk type); they persist + appear in pickers. |
| 2 | Create OwnPJ (system counterparties auto-appear), CashDesk (PJ rule enforced), BankAccount, set an exchange rate with history. |
| 3 | Create a commercial Counterparty (multi-role) + a Contract with alb/negru + money-location validation. |
| 4 | Add Generic+Specific parts (OEM code), Stage/Department/Executor/Warehouse; all selectable. |
| 5 | Create a Vehicle; cancel/storno + view history on an existing document; registers net to zero. |
| 6 | Full stock cycle (order-to-stock → receive → issue → return → stocktake) with an edit/cancel; registers stay consistent. |
| 7 | Create a Cont de plata (lines + discounts, Σ≡total), accept it, walk the resulting ZN's statuses. |
| 8 | Record a FX payment (MDL equivalent), allocate to a Cont, see payment state change + a conformity ticket. |
| 9 | Edit a parameter (history kept), create a scoped user, approve a device — no Django admin. |

**Gate rule (mechanical, per UI/UX §4.5):** a layer's frontend stages are not "done" until the
founder has walked the rendered screens on the test ERP and signed off. The verification is "founder
creates real data + runs real flows + looks at the screens," not a checkbox.

---

## PART 4 — Where existing built assets land

| Existing asset | Lands in | As | Note |
|---|---|---|---|
| **Foundation engine** (documents, registers R1–R18, fiscal numbering, status-layers, notifications, attachments, printing) | underlies all; explicitly re-verified in **L5** | KEEP (re-verify) | the correctness-critical "asset"; not rebuilt. NEW work = exposing `documents/api.py` (L5.2). |
| **All nomenclature REST + admin** (`@nomenclature` registry, parts catalog API) | **L1/L2/L4** | KEEP | already gives generic CRUD; layers are frontend-mostly. |
| **`parties` models** (Counterparty/Contract/Vehicle/OwnPJ) — NO API | **L2 (OwnPJ), L3 (Counterparty/Contract), L5 (Vehicle)** | NEW backend | the biggest master-data backend hole; build `parties/api.py` incrementally. |
| **inventory-procurement backend** (DONE + merged) | **L6** (re-verify) | KEEP + small REBUILD (order-to-stock 6.2) | engine done; needs the missing UI + lifecycle verbs + order-to-stock. |
| **inventory-procurement frontend** (create-only; some routes orphaned) | **L0** (mount) + **L6** (complete) | REBUILD | add edit/cancel/storno/history; align to new menu/UI-UX. |
| **`parts/PartsPicker`** (search-only) | **L4** | KEEP + extend | keep the picker; add the management catalog screen around it. |
| **`so_quotes`** (cont-quote-core branch; validated 75/75; **open contest #103/#104**; UI peeled) | **L7** | REBUILD (re-verify + land) backend; **NEW** editor UI | resolve the escalation first; re-verify against now-real deps; then build the peeled `cont-quote-editor-ui`. |
| **`treasury` foundations** (treasury-app-foundations branch; abstract Payment, currency resolver, conformity, ConformityTicket; backend-only) | **L8** | RE-SLOT | base money plumbing moved to its dependency-correct slot; re-verify on real money nomenclatures, then build producers + UI. |
| **menu/shell** (flat `appRoutes`, empty Home) | **L0** | REBUILD | becomes the living modular registry. |

**Stage tally (excluding the 10 founder-verification gates):** **36 stages** — **NEW 23**,
**KEEP (re-verify) 6**, **REBUILD 6**, **RE-SLOT 1**. Split **18 backend / 18 frontend** — intentionally
~even (the back/front-separation rule). Each frontend stage additionally carries the UI/UX spec-gate
(§2 questionnaire) + the founder visual gate. *(Was 33 in the DRAFT; ARH-01 added 3 via the PART-6 splits + the rate-endpoint correction.)*

---

## PART 5 — Open questions / judgment calls (architect + founder)

1. **[BLOCKER — founder/architect] Escalation #103/#104 on `so_quotes` (unresolved_contest).** L7 can't
   start until this is decided: **approved** / **rework:BUILD** / **rework:SPEC**. Evidence dossier:
   `/artifact/1200`. The build itself validated 75/75, so the contest is about a specific finding, not
   a broad failure — but it must be resolved before re-verify/land. *Recommend: architect reads the
   dossier and brings a recommendation; founder decides.*
2. **[founder] parties: REST API vs Django-admin-only.** The plan assumes Counterparty/Contract/Vehicle/OwnPJ
   get real in-app screens (gap-audit recommendation). ADR-0002 currently says config = Django admin.
   These are operational data clerks touch daily, so screens are recommended — but this needs a
   **conscious ratified decision** (it's an F5-dimension implication for who-can-edit). *Recommend: YES, build screens (L2/L3/L5).*
3. **[founder] Config/users/rights UI (Layer 9): build vs stay on Django admin for v1.** Recorded
   decision keeps it on admin (founder-only). Layer 9 is included for completeness and sequenced last;
   founder decides whether to build it now, defer, or keep admin + just be *told* admin is the cockpit.
4. **[architect] Visual reference apps (UI/UX §5.1 — "cea mai valoroasă intrare").** The UI/UX mechanism
   needs 1–2 reference apps the founder likes BEFORE the first frontend-heavy layer (L1's generic CRUD
   framework sets the visual tone for everything). *Blocks L1 FE polish; founder input needed early.*
5. **[architect] Generic CRUD framework scope (L1.2).** How much to invest in one config-driven catalog
   screen vs per-entity screens. Recommend heavy investment — it's reused across L1/L2/L4 and is the
   single biggest frontend leverage point; risk = over-generalizing before the visual reference is set.
6. **[architect] Where exactly does Vehicle intake (`act primire`) live?** L5 gives an interim Vehicle
   master-data form; the real intake (custody transition, mileage at intake, attachments) is a
   service-orders concern. Decide whether intake rides L7 (service-orders) or a dedicated later stage,
   and whether the interim form is throwaway.
7. **[architect] Edit/cancel/storno UI as a standing checklist item.** L5.2 builds the `documents` API
   and L5.4 the reusable controls; confirm every later operational FE stage (L6/L7/L8) has "expose
   edit + cancel + history" as a hard acceptance criterion so the create-only pattern never recurs.
8. **[architect] PaymentCategory.direction vs the Direction table.** Two different "direction" concepts
   exist (PaymentCategory's IN/OUT enum vs the body/service/general `Direction` table feeding
   ExpenseCategory/R5/R8 tags). Confirm the UI labels them distinctly so the founder doesn't conflate
   them in L1.
9. **[architect] Demo-seed vs founder-entered data for gates.** UI/UX #11 wants demo data; but the
   methodology's gates want the founder to CREATE real data. Decide per layer whether the gate uses
   seeded data, founder-entered data, or both (recommend: founder enters at least one of each entity
   per gate; seed the rest for volume).
10. **[architect] L1↔L2 money-nomenclature ordering edge cases.** `BankAccount` requires Bank+Currency+OwnPJ;
    `Contract`'s default money location needs CashDesk/BankAccount to pre-exist. Confirm the Layer-2
    "set default money location on a contract" is correctly deferred to L3 (Contract), not pulled into L2.

---

## PART 6 — ARH-01 architect review + independent code-verification (22-06-2026)

A fresh subagent (clean context, separate from the drafting agent — Doctrine §4) re-checked every
load-bearing claim of PART 1 / PART 4 against the **real** `erp-workspace` code, read-only. Full
evidence (file:line) is in the session record; summary below.

**Verified CONFIRMED (safe to build on):**
- `parties` (Counterparty/Contract/Vehicle/OwnPJ/CounterpartyRole) has **NO REST API** and is **not
  mounted** in `erp/urls.py` — admin-only. → L2/L3/L5 NEW backend stands.
- The `@nomenclature` registry gives generic GET/POST/PUT CRUD at `/api/nomenclature/<key>/` for **23
  keys**. → nomenclature layers are frontend-mostly, confirmed. **Caveat:** DELETE/PATCH return **405**
  — "delete" is soft (`active=false`).
- All load-bearing FK chains (Contract, CashDesk, BankAccount, Lot) match the code; **no FK forces a
  different layer ordering** than L0<…<L9.
- The layering rule is real + enforced: registers/procurement reference `service_orders` by
  **BigIntegerField** (not FK); `import-linter` `layers` + `forbidden` contracts in `pyproject.toml`.
- `documents/api.py` does **not** exist (L5.2 premise holds); the L0 issue/return route orphan is real.
- Branch assets confirmed: `so_quotes` = 16 modules on `cont-quote-core` (no editor FE — peeled);
  treasury abstract `Payment` / rate resolver / conformity / `ConformityTicket` on
  `treasury-app-foundations`, backend-only.

**Changes applied to the plan (architect-domain, mode 3):**
1. **L2.3 rate endpoint NEW, not KEEP** — `DefaultExchangeRate` has no REST on `main`; the only writer
   is on the unmerged treasury branch. Now its own small NEW backend stage (source the logic from the
   treasury branch; L8 reuses it).
2. **Split oversized stages** (small-stage mandate): **L1.2** → framework-skeleton-on-one-entity (1.2) +
   instantiate-the-rest (1.3); **L7.2** → editor header+lines (7.2) + discounts+lifecycle (7.3).
3. **Diapazon** (fiscal number ranges, FK→OwnPJ) was orphaned to L9 but is needed at L6 — added an
   explicit **data prerequisite** note to L6 (seed/admin-enter before L6; management screen stays L9).
4. **StatusLabel + TicketType** (otherwise-unhomed config nomenclatures) folded explicitly into L9.2.
5. **Soft-delete semantics** spelled out (deactivate, not row-disappears) so gates aren't mis-written.

**Flagged for spec-time sizing (NOT pre-split — last responsible moment, Doctrine §12):** L5.2
(documents lifecycle API — 5 verbs, consider read vs mutate split), L6.3 + L6.4 (multi-screen REBUILD
bundles — split per screen/pair). The hard rule: **each stage fits one builder pass; the spec agent
splits further if a stage exceeds it.**

**New tally:** 36 stages (was 33) — NEW 23 / KEEP 6 / REBUILD 6 / RE-SLOT 1; 18 BE / 18 FE.

**Open-question triage (PART 5):**
- *Architect-decided (mode 3 — applied or standing):* Q5 generic-CRUD investment (heavy, but split
  skeleton/fan-out — done); Q6 Vehicle intake (interim master-data form in L5; real `act primire` rides
  L7 service-orders; the interim form is NOT throwaway — it stays as the quick master-data path); Q7
  edit/cancel/storno standing checklist (= factory law: every operational FE stage MUST expose
  edit+cancel+history); Q8 PaymentCategory.direction vs the Direction table (UI labels them distinctly);
  Q9 demo-seed vs founder-entered (founder enters ≥1 of each entity per gate; seed the rest for volume);
  Q10 L1↔L2 ordering (default-money-location stays on Contract @ L3, not pulled into L2).
- *Founder-domain (mode 1 — pending; see the founder decision list):* reference apps (Q4, asked); ratify
  parties-get-real-screens vs Django-admin (Q2 — recommend YES); config/users/rights UI build vs
  Django-admin-for-v1 (Q3 — deferrable to L9, recommend Django-admin v1 + tell the founder admin is the
  cockpit). Q1 escalation #103/#104 is an L7 decision (parked decision #26) — decided when L7 is reached,
  NOT a plan-approval blocker.
