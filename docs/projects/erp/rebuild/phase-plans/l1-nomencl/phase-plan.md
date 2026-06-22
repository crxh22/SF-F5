# l1-nomencl — Root money + classification nomenclatures (L1)

BE re-verify + FE. The generic-CRUD FE framework is the keystone reusable contract and the visual
tone-setter for the whole app (full UI/UX gate). Backend REST already exists (the `@nomenclature`
registry) — so this layer is frontend-mostly. Intra-layer DAG:
`nomencl-rest-verify` (contract) → `crud-framework-skeleton` (contract) → `instantiate-catalogs` (leaf).

Nine real Layer-1 keys: `currency`, `bank`, `direction`, `expense_category`, `payment_category`,
`cash_desk_type`, `bank_account_type`, `price_level`, `contract_type`.

**Layer founder-verification gate (DRAFT L1):** the founder creates a Currency, a Bank, two
PaymentCategories (one IN, one OUT), an ExpenseCategory, a CashDeskType (one `requires_pj=true`), edits
one, soft-deletes one — and sees them persist, sort, and reappear in pickers.

---

## nomencl-rest-verify — [KEEP] re-verify the generic nomenclature REST seam (backend · contract · routine)

**Scope:** Prove in reality (running test ERP, authenticated) that the existing generic CRUD seam at
`/api/nomenclature/<key>/` already serves all nine L1 keys with the contract every FE stage builds on.
KEEP / **VERIFY-ONLY** (routine = no post-build code audit) — prove the existing behavior on the new
deps; ship no production code fix here. If a missing key or wrong 405 behaviour is found — a defect
requiring a CODE change — ESCALATE for a separate structural fix stage, never patch in place. Extend
`test_api.py` coverage across the nine keys (per-key PATCH/DELETE→405 + a PUT active=false round-trip).

**Contract seam (frozen here — root of the layer):** endpoint shape `GET/POST` on
`/api/nomenclature/<key>/`, `GET/PUT` (full-object) on `/api/nomenclature/<key>/<pk>/`; **soft-delete =
PUT `active=false`**; **DELETE and PATCH → 405**; per-key writable field set (e.g. `payment_category.direction`
IN/OUT, `expense_category.direction` FK→Direction, `cash_desk_type.requires_pj`, `price_level.algorithm_key`,
`*.code`). No field-metadata/OPTIONS endpoint exists — field/column definitions are delegated to FE
config-per-entity. This is the backend seam the FE framework depends on (so it is itself `role:contract`,
per the contract-first law).

**Founder-gate contribution:** guarantees the persistence/soft-delete behaviour the L1 gate relies on.

---

## crud-framework-skeleton — [NEW] config-driven CRUD catalog framework on ONE entity (frontend · contract · structural)

**Scope:** Build the ONE reusable, config-driven catalog screen every nomenclature instantiates, proven
end-to-end on a single entity (Currency). Generic `nomenclature.ts` API client (full-object PUT,
soft-delete = PUT active=false; never PATCH/DELETE) + a `CatalogScreen` driven by an `EntityConfig`
descriptor: dense exception-first list (AppTable) + create/edit drawer (AppDrawer + AppForm, optimistic,
validation from the API error envelope) + deactivate/reactivate + add-new. The visual tone-setter — runs
the full UI/UX gate against the founder reference apps; flagged for founder visual review.

**Contract seam (frozen here — second root of the layer):** the `EntityConfig` / `FieldDef` (text/number/
select/switch/entity-picker, chosen for minimum clicks) / `ColumnDef` descriptor types. Minimal +
additive. The fan-out stage adds ONLY config objects against this seam — no new framework code. Depends
on `nomencl-rest-verify` (builds against that backend seam).

**Founder-gate contribution:** delivers the create/edit/soft-delete behaviour for Currency (the first
entity in the gate) and sets the visual bar.

---

## instantiate-catalogs — [NEW] instantiate the remaining 8 catalogs (frontend · leaf · structural)

**Scope:** Drive the framework for the other eight keys by adding config objects only — no new framework
code, no new visual primitives. Per-entity control fit: `payment_category.direction` = IN/OUT switch
(binary, min clicks), `cash_desk_type.requires_pj` = switch, `expense_category.direction` = entity-picker
→Direction with add-new-inline; `price_level.algorithm_key`. The two "direction" concepts (PaymentCategory
IN/OUT vs the Direction tag table) are labelled distinctly so the founder never conflates them. Each
catalog registers in the L0 menu registry under `nomenclatoare`. Standing law (edit/soft-delete/add-new)
inherited from the framework; asserted per entity, not re-implemented.

**Contract seam (consumes):** depends on `crud-framework-skeleton` — config objects against the frozen
`EntityConfig` seam + registration against the L0 menu-registry seam. Adds no new seam.

**Founder-gate contribution:** this is the stage the L1 gate exercises end-to-end — create/edit/soft-delete
a currency, two payment categories (IN+OUT), an expense category, and a cash-desk type with
`requires_pj=true`, all persisting and reappearing in pickers.
