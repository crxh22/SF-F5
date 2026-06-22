# ERP Gap Audit — Elita-9 (22-06-2026)

**Scope:** Audit the planned + partially-built ERP for MISSING fundamental capabilities — especially user-facing MANAGEMENT (create/edit/delete) of master data and configuration. Trigger: founder discovered there is **no way to add/edit/delete counterparties, contracts, parts/goods, or nomenclatures anywhere in the app UI**, and feared more such holes.

**State audited:** `erp-workspace` **`main` branch** (the integration branch). Verified that the two "in-progress" phases are NOT built yet: `git diff main..phase/service-orders` and `..phase/treasury-payments` each contain **only `phase-plan.json/md`** (planning docs, 8 files) — zero models, zero APIs, zero screens. So the entire shipped codebase today = **foundation + inventory-procurement**.

Plan/spec source of truth: `/home/artur/projects/ERP-start/docs/Business/` (35-doc catalog, ratified, pinned commit `51e32b0`) and `/home/artur/projects/SF-F5/docs/projects/erp/PROJECT.md`.

---

## 1. Executive verdict

**How holey is it? — The engine is sound; the cockpit barely exists.**

The ERP is **not "full of holes" in its core**; it is **missing almost its entire user-facing surface** because the two phases that build operational screens (service-orders, treasury-payments) **have not produced any code yet** — only plans. What shipped (foundation + inventory-procurement) is a **deep, correct back-of-house engine** with a **thin, transaction-only frontend** bolted to one corner of it (warehouse/procurement).

**What is SOLID (genuinely well-built, do not rewrite):**
- **Document engine** (`apps/documents`): base draft/final model, version history, **storno = reverse-and-repost / zero-effect cancellation** (ADR-006), per-type audit + central R11 index, document dependency graph + atomic cascade (ADR-005).
- **Posting + register engine** (`apps/registers`): all movement journals **R1–R10, R18** + trackers **R12/R13** + per-register **period snapshots** + period open/close state, idempotent posting, engine-context write-guard. This is the expensive, correctness-critical heart and it is present and structured to spec.
- **Fiscal numbering** (`apps/fiscal`: R15A/R15B, diapazon + e-factura lifecycles), **status-layer framework**, **notifications framework**, **media/attachments**, **print/PDF override** framework, **nomenclature/parameter config registry** (configurability-is-law honoured).
- **Inventory-procurement operational logic**: ordering, reception, supplier fiscal invoice linkage, warehouse + painter issue, returns (executor / supplier / client) + their fiscal forms, stocktaking with reconciliation, reservation flows, negative-stock guard. Both backend AND a working React UI exist for these.

**The verdict: the core IS salvageable — in fact it is the asset.** The holes are almost entirely **"surface not built yet"**, not "engine wrong." The founder's specific alarm (no master-data CRUD) is **real and correct**, but it splits into two very different categories (below): a deliberately-deferred-to-Django-admin slice, and a genuinely-unowned slice.

---

## 2. Entity × management-capability matrix

Legend: **(a)** frontend screen · **(b)** REST API, no screen · **(c)** Django admin only · **(d)** seed/import only · **(e)** nothing.
Sources: backend models per `apps/*/models.py`; admin per `apps/*/admin.py` + `apps/nomenclature/apps.py:9-18` (auto-registers all nomenclature into admin); APIs per root `backend/erp/urls.py:7-17` + each `apps/*/api.py`; frontend per `frontend/src/shell/routes.tsx` + `features/*`.

### Operational / commercial master data

| Entity | Model file | Mgmt path | Notes |
|---|---|---|---|
| **Counterparty** (client/supplier/employee) | `apps/parties/models.py` | **(c) admin only** | NO REST surface at all — `parties` is not mounted in `backend/erp/urls.py`. Read-only autocomplete in procurement forms (`procurement/api.py:177-180,293`). **Founder's #1 gap.** |
| **Contract** (service/angajat/rate/TVA/outsource) | `apps/parties/models.py` | **(c) admin only** | Same. No API, no screen. Every `Cont` requires a `contract` FK (`core-entities/spec.md:88`) — so you cannot make a quote without a contract that only Django admin can create. |
| **OwnPJ** (own legal entity + VAT flag) | `apps/parties/models.py` | **(c) admin only** | Read-only selector in stocktaking. No setup UI. |
| **Vehicle** | `apps/parties/models.py` | **(c) admin only** | No REST, no screen. **No intake/`act primire` flow built** (that lives in unbuilt service-orders) → today a vehicle can ONLY be born in Django admin. |
| **CounterpartyRole** | `apps/parties/models.py` | **(c)** admin inline | child of Counterparty. |

### Nomenclature / configuration (all `apps/nomenclature`, auto-registered to admin + generic REST)

| Entity group | Mgmt path | Notes |
|---|---|---|
| GenericPart / SpecificPart (the **two-layer parts catalog**) | **(b)+(c)** | `POST`/`PUT` via `/api/nomenclature/<key>/` (`nomenclature/api.py:83-93`), **but DELETE → 405** (soft-delete `active=False` only). Admin-registered. **No frontend catalog screen** — founder's "no way to add parts/goods" is correct at the UI level; API+admin exist. |
| Warehouse/Depozit, CashDeskType, BankAccountType, **CashDesk, BankAccount** | (b)+(c) | API create/update + admin. No screen. (Cash desk/bank account *seeding* is also a planned treasury concern.) |
| ExpenseCategory, PaymentCategory, Direction, Currency, Bank, PriceLevel, Diapazon | (b)+(c) | same. |
| Stage, StageState, Department, Executor, Work | (b)+(c) | same — the MVP stage-tracking nomenclature. |
| StatusLabel, RightsTemplate, ContractType, TicketType | (b)+(c) | StatusLabel = configurable status display labels. |
| **Parameter** (business-parameter registry) + ParameterHistory | (c) | hand-written admin routes edits through `set_param` so history is kept; **no API, no screen**. |
| DefaultExchangeRate | (c) add-only | append-only; admin add-only. |
| PrintTemplateOverride | (c) | `apps/printing`. |

### Users / access / config

| Entity | Mgmt path | Notes |
|---|---|---|
| **User** | **(c) admin only** | No user-management UI. Login screen exists (`frontend/src/auth/screens/LoginScreen.tsx`); no create-user/edit-user screen. |
| **Right** (per-domain view/edit rights) | (c) + **(b) partial/destructive** | Admin tabular inline (`auth-access/spec.md:116`). API = only `save-as-template` / `apply-template` / `copy-from` (`accounts/api.py:240-278`), **superuser-only, destructive (overwrite-all)**. No granular rights UI. |
| **Device** + DeviceSession/DeviceAudit | (b) state-change + (c) | bootstrap + approve/revoke endpoints (`accounts/api.py`); admin read-only with approve/revoke actions. |
| ViewPreference | (b) | `/api/me/preferences/<key>/`; per-user view prefs plumbing (no settings screen, by design — concrete set deferred). |

### Documents (transactional) — built vs not, and can a user edit/cancel/delete?

| Document | Backend model | API write | Screen | Edit / cancel / **storno** exposed? |
|---|---|---|---|---|
| Cont de plata (#2), ZN (#3), ZnLine | `apps/service_orders/models.py` | **(e) none** | **(e) none** | skeleton entities only — behaviour/flows belong to **unbuilt** service-orders. |
| Supplier order (#5), Purchase reception (#6), Supplier fiscal invoice (#7) | `apps/procurement` | (b) create/finalize | **(a)** ordering / reception / supplierInvoices screens | **create only** — no edit/cancel/storno path in API or UI. |
| Warehouse issue (#8), Painter issue (#9), Return-from-executor (#26) | `apps/inventory` | (b) create | **(a)** issue/return counters (note: `IssueCounter`/`ReturnCounter` are **components not mounted on a route** — see Gap-7) | create only. |
| Retur furnizor (#27/#28), Retur client (#29/#30) | `apps/inventory` | (b) create + fiscal-invoice | **(a)** returns screens | create only. |
| Stocktaking (#24) | `apps/inventory` | (b) create/finalize | **(a)** stocktaking screen | create/finalize only. |
| Sale doc (#10), incomplete-delivery (#11), expense-closure (#12), payments (#13–16), sales fiscal invoice (#17), supplementation (#31), settlement acts (#32), service acq/sale (#33/#34), opening balances (#35), defect assessment (#1), custody acts (#22a/b) | mostly **not built** | (e) | (e) | owned by unbuilt service-orders / treasury / migration phases. |
| **Generic document storno/edit/cancel/cascade** | engine in `apps/documents` | **(e) NO `apps/documents/api.py` exists** | (e) | **The whole edit-by-reverse-and-cancel engine has NO REST surface and NO UI.** It is callable Python only. |

### Registers / movements / snapshots
All R1–R18 movement tables, trackers, per-register snapshots, period-state: **(d)/(e)** — written only by the posting engine, read by (the future) reporting. Correct that they have no CRUD. Not registered in admin (correct).

---

## 3. Fundamental capability gaps beyond CRUD — present/absent

| # | Capability | Status | Detail |
|---|---|---|---|
| G1 | **Add/edit/delete operational master data in-app** (counterparty, contract, vehicle, PJ) | **ABSENT in UI; admin-only; NO REST** | parties app has no `api.py`, not mounted. |
| G2 | **Manage the parts/goods catalog in-app** | **ABSENT in UI** (API+admin exist) | no catalog screen; PartsPicker is search-only. |
| G3 | **Manage nomenclatures/config in-app** (categories, warehouses, cash desks, bank accounts, stages, departments, executors, works, parameters, exchange rates, print templates) | **ABSENT in UI** (API+admin exist) | by recorded decision = Django admin in v1 (see §4 / OPEN-CR1). |
| G4 | **Standalone "order parts to stock"** (not against a ZN) | **ABSENT** | `orderable-lines` is sourced exclusively from `ZnLine` where kind∈{part,material} (`procurement/api.py:188-215`); reception is "one per ZN" (catalog #6). No purchase-to-own-stock path → blocks `vanzare_piese` / `revanzare_tva` resale stock and any speculative stocking. |
| G5 | **Edit / cancel / storno / delete a finalized document** in-app | **ABSENT** | engine exists (`apps/documents` storno + cascade) but no `documents/api.py`, no UI verb. Even procurement/inventory screens are create-only. |
| G6 | **Navigation to reach management** | **ABSENT for master data** | flat sidebar (`frontend/src/shell/AppShell.tsx`) = 9 transaction/analytics links only; no Settings/Admin/Nomenclatoare section; HomePage is an empty stub. |
| G7 | **Reach the issue/return screens at all** | **ABSENT (orphaned)** | `warehouse-issue/IssueCounter.tsx` + `ReturnCounter.tsx` are prop-driven components **not mounted on any route** → unreachable in the running app. |
| G8 | **Global search** | **ABSENT** | no global search component/route; only local typeaheads in forms. |
| G9 | **User & rights management UI** | **ABSENT (admin-only)** | needed per spec (actors-and-access.md §Access "configurable user management … similar to 1C"); only destructive superuser template API + admin inline exist. |
| G10 | **Company / PJ & configuration setup UI** | **ABSENT (admin-only)** | no company-setup or settings screen; OwnPJ admin-only. |
| G11 | **Create a Cont/quote / ZN / take a payment / release a vehicle** | **ABSENT (unbuilt phase)** | the entire operational spine + money spine = service-orders + treasury, which produced plans only. This is the bulk of "the ERP" as a user sees it. |

---

## 4. Planned vs forgotten

The decisive distinction. There is **one explicit recorded decision** that reframes part of the founder's alarm as *intentional*:

> **OPEN-CR1 RESOLVED** (`_factory/stages/foundation.auth-access/spec.md:29`): "nomenclature/parameter/user/rights administration is **meta-administration, founder-only in v1** ('The founder will manually add rights and create templates'; **ADR-0002: Django admin = config management**). Delegating config administration to non-founder users later requires an F5 dimension bump."

So **G3, G9, G10 (nomenclature/parameter/user/rights/config) are DEFERRED-AS-PLANNED to Django admin for v1** — a conscious choice, not a crack. The hole is that *the founder was not aware Django admin is the intended cockpit for these*, and Django admin is not a translated/curated UX.

Everything else is **genuinely unowned or fell through**:

| Gap | Planned anywhere? | Verdict |
|---|---|---|
| G1 Counterparty/Contract/Vehicle/PJ **operational** management | Master data is "config" in the catalog, but PROJECT.md never names a screen; intake/`act primire` (vehicle birth) is scoped to **service-orders** (catalog #22a, PROJECT.md §service-orders) — **unbuilt, but owned** | **Vehicle creation: deferred-as-planned (service-orders intake).** Counterparty/Contract/PJ create-edit: **FORGOTTEN** — no phase owns a counterparty/contract management UI; ADR-0002 implies admin, but they are operational data clerks touch daily, not founder-only config. Migration-1c seeds *initial* master data, not ongoing management. |
| G2 Parts catalog UI | inventory-procurement built the *tables* + API; "code search" is mentioned (PROJECT.md) but no **management** screen was specced | **FORGOTTEN at UI level** (engine/API done). |
| G4 Standalone order-to-stock | catalog/PROJECT.md frame ordering as ZN-driven; `vanzare_piese`/`revanzare_tva` conturs assume stock already exists | **FORGOTTEN / unowned** — no phase provides purchase-to-own-stock. |
| G5 Document edit/cancel/storno UI | engine is foundation; exposing it is implicitly each domain phase's job per document type | **PARTIALLY FORGOTTEN** — no phase plan explicitly lists "edit/cancel existing document" screens; high risk of repeating the create-only pattern. |
| G6 Navigation / app shell for management | UI-foundation built the shell; no phase owns a master-data nav section | **FORGOTTEN** (shell exists, sections don't). |
| G7 Orphaned issue/return routes | inventory-procurement built the components; mounting them was missed | **FELL THROUGH THE CRACKS** (build defect, smallest/cheapest fix). |
| G8 Global search | not in any plan reviewed | **FORGOTTEN / never scoped** (arguably nice-to-have). |
| G11 Operational + money spine | **fully planned** — service-orders + treasury-payments phases exist with detailed phase-plans; just not executed | **DEFERRED-AS-PLANNED** (in progress). |

**Count: ~7 FORGOTTEN/unowned (G1-counterparty/contract, G2, G4, G5, G6, G7, G8) vs ~4 DEFERRED-AS-PLANNED (G1-vehicle-intake, G3, G9/G10, G11).**

---

## 5. Prioritized GAP LIST

| Sev | Gap | Entities/screens affected | Planned? | Which phase should own |
|---|---|---|---|---|
| **BLOCKS BASIC ERP USE** | G1 Counterparty + Contract management UI | every quote/payment/order needs a contract+counterparty; clerks need daily add/edit | forgotten (admin-only by default) | **NEW: a "master-data / nomenclatoare" UI stage** — or fold into foundation follow-up / service-orders intake |
| **BLOCKS BASIC ERP USE** | G11 Operational + money spine (Cont, ZN, sale, payments, fiscal invoice, release) | the actual ERP workflows | deferred-as-planned | service-orders + treasury-payments (execute them) |
| **BLOCKS BASIC ERP USE** | G5 Edit/cancel/storno a document in-app | every document type | partly forgotten | each domain phase MUST add edit/cancel verbs over the existing `apps/documents` engine; expose `documents/api.py` |
| **BLOCKS TESTING** | G6 + G7 Navigation + mount orphaned routes | issue/return screens unreachable; no master-data nav | fell through cracks | quick fix now (UI-foundation follow-up) |
| **BLOCKS TESTING** | G1 Vehicle creation / intake | can't start a real job without a vehicle | deferred-as-planned (intake) | service-orders (`act primire`); interim: a minimal vehicle form |
| **BLOCKS TESTING** | G2 Parts catalog management screen | can't add a new part/good as a user | forgotten (API exists) | master-data UI stage (cheap — API already there) |
| **BLOCKS basic resale flows** | G4 Standalone order-to-stock | `vanzare_piese`/`revanzare_tva`, speculative stocking | forgotten/unowned | inventory-procurement follow-up (extend ordering to non-ZN) |
| NICE-TO-HAVE (but expected) | G3/G9/G10 in-app nomenclature/user/rights/company-config UI | config clerks; rights delegation | deferred-as-planned (Django admin v1) | later F5-dimension bump; or a curated config UI stage |
| NICE-TO-HAVE | G8 Global search | cross-entity lookup | never scoped | reporting/UX phase |

---

## 6. What the replan must add

1. **A first-class "Master Data & Nomenclatoare" UI surface** (the founder's core ask). Cheapest high-value work because the **backend + REST already exist** for the whole nomenclature set and the parts catalog — this is mostly frontend. It must cover at minimum: counterparties, contracts, vehicles, own-PJ, parts catalog (generic+specific), warehouses, cash desks, bank accounts, expense/payment categories, stages/departments/executors/works, parameters, exchange rates. **Note the one missing backend piece: parties (Counterparty/Contract/Vehicle/OwnPJ) have NO REST API** — those four need serializers+viewsets built (or a deliberate ratified decision that they stay Django-admin-only, which the founder should make consciously, not by accident).
2. **A real app shell / navigation** with a management section, and **mount the orphaned** `IssueCounter`/`ReturnCounter` routes (trivial, do immediately).
3. **Document lifecycle verbs in every domain phase's acceptance criteria**: not just "create document" but **edit (reverse-and-repost), cancel (zero-effect), view version history** — expose the foundation engine through `documents/api.py` + per-type UI. Make this a standing checklist item so the create-only pattern stops repeating.
4. **A standalone purchase-to-stock path** (order + reception not tied to a ZN) to support parts resale and stocking.
5. **A conscious decision on the config/user/rights UI**: keep Django admin for v1 (cheap, already works) **but tell the founder explicitly** that `…/admin/` is the cockpit for those, OR schedule a curated config UI. Either is fine; the silent assumption is the bug.
6. **Defer cleanly (already planned, leave alone):** global search, full user/rights delegation UI, reporting — these are genuinely later.

**Bottom line for the replan:** the expensive, correctness-critical core (documents + posting + registers + fiscal) is **built and worth keeping**. The replan is overwhelmingly about **building the missing user surface** — most urgently a master-data management UI and the in-app edit/cancel/storno verbs — plus **executing the already-planned service-orders/treasury phases**. Only ~7 gaps genuinely fell through the cracks; the rest is "planned but not yet built."
