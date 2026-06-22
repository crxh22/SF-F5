# L6 — l6-stock-ops · phase-plan companion

**Layer:** Inventory & procurement OPERATIONS surface (UI + lifecycle verbs).
**Phase dep (macro DAG):** `l5-engine-docs → l6-stock-ops` (needs the merged inventory-procurement
backend DONE + the L5 reusable document-lifecycle controls + the L0 menu registry).
**Founder gate:** run a full stock cycle on real data — order-to-stock → receive → issue → return →
stocktake — editing and cancelling at least one finalized document, and confirm stock + money
registers stay consistent.

## Stages

| id | kind | role | risk | one-line |
|---|---|---|---|---|
| `procurement-reverify` | backend | **contract** | routine | [KEEP / **VERIFY-ONLY**] Re-verify the merged ordering/reception/supplier-invoice/issue/return/stocktaking/reservation producers + R2/R3/R6/R8 postings on real data; FREEZE the already-mounted operational seam. Defect requiring a code change → ESCALATE for a separate structural fix stage, never patch in place. |
| `order-to-stock` | backend | leaf | structural | [REBUILD] Extend ordering+reception so a purchase can target OWN STOCK not only a ZnLine (unblocks vanzare_piese/revanzare_tva; corrects the ZN-only assumption). |
| `ordering-reception-fe` | frontend | leaf | structural | [REBUILD] Ordering + reception screens + the order-to-stock entry point + lifecycle (edit/cancel/storno/history via L5 controls). Also descends from `order-to-stock`. |
| `supplier-invoice-fe` | frontend | leaf | structural | [REBUILD] Supplier-fiscal-invoice screen + lifecycle (edit/cancel/storno/history via L5 controls; link-to-purchase-docs). |
| `issue-fe` | frontend | leaf | structural | [REBUILD] Warehouse-issue + painter-material-issue screens + lifecycle (edit/cancel/storno/history via L5 controls). |
| `returns-stocktake-fe` | frontend | leaf | structural | [REBUILD] Return-from-executor/to-supplier/from-client + stocktaking + reservation screens + lifecycle. |

## Contract stage + why

`procurement-reverify` is `role:contract`. The inventory-procurement backend is DONE + merged, and its
HTTP surface is already mounted on `main` (`/api/procurement/`, `/api/inventory/`, `/api/stocktaking/`).
This stage proves those producers + postings fire on real data and FREEZES that already-live endpoint
list as the operational seam every other L6 stage builds on (the order-to-stock extension and all four FE
stages). It is `routine` per doc-1 §4 (a re-verify-only KEEP stage = little/no new code; no post-build
code audit) — **VERIFY-ONLY**: a defect requiring a code change is ESCALATED for a separate structural fix
stage, never patched in place (an in-place fix would ship un-audited). It still carries the spec dual-audit
(all classes) and is contract-first floor-exempt.

## Intra-layer DAG

```
procurement-reverify ──┬──► order-to-stock ──► ordering-reception-fe
                       │                       ▲
                       ├───────────────────────┘  (ordering-reception-fe also depends directly on the re-verify)
                       ├──► supplier-invoice-fe
                       ├──► issue-fe
                       └──► returns-stocktake-fe
```

Edges: `procurement-reverify→order-to-stock`, `procurement-reverify→ordering-reception-fe`,
`procurement-reverify→supplier-invoice-fe`, `procurement-reverify→issue-fe`,
`procurement-reverify→returns-stocktake-fe`, `order-to-stock→ordering-reception-fe`.
`ordering-reception-fe` depends on `order-to-stock` because it mounts the standalone order-to-stock entry
point; the other three FE stages need only the verified operational seam. `procurement-reverify`
out-degree is 5 (≤6); all stage degrees ≤5.

**FE split (at planning — the only split point):** the old two FE stages were each too large for one
builder pass, so they were peeled into four leaves, all still descending from `procurement-reverify`:
- `ordering-fe` → `ordering-reception-fe` (ordering + reception + order-to-stock entry) + `supplier-invoice-fe`
  (supplier-fiscal-invoice screen).
- `issue-return-fe` → `issue-fe` (warehouse-issue + painter-material-issue) + `returns-stocktake-fe`
  (returns + stocktaking + reservations).
Each new leaf carries the operational-FE law (L5 lifecycle controls: edit/cancel-storno/history) +
add-new-inline pickers, and is independently builder-sized (≤6 AC / ≤6 touched).

## Deviations from doc-1 (RAW material → authored)

- **No procurement-decoupling stage** (doc-1 §1.2 CORRECTION): procurement imports `service_orders`
  DIRECTLY (lazy `from apps.service_orders.models import ZnLine/Zn`); only registers + fiscal use the
  BigInteger-not-FK pattern. So `order-to-stock` keeps the lazy import and adds NO upward FK — it only
  makes the ZN linkage optional. No structural decoupling work exists.
- **The L6 backend endpoints are ALREADY MOUNTED on main** — verified in `backend/erp/urls.py`
  (`api/procurement/`, `api/inventory/`, `api/stocktaking/`). Hence `procurement-reverify` is a
  re-verify+freeze, and both FE stages are REBUILD on existing `frontend/src/features/*` screens
  (ordering/reception/supplierInvoices/returns/stocktaking/warehouse-issue all exist create-only today),
  not NEW.
- **order-to-stock premise verified in code:** `SupplierOrder` line carries `zn_line_reference` and
  `PurchaseReception` payload requires `zn_id` — purchasing is structurally ZN-only today. The rebuild
  makes both nullable + routes a no-ZN reception into a plain own-stock-in lot (R3).
- **Diapazon prerequisite** (doc-1 L6 note) folded into the `procurement-reverify` acceptance: a fiscal
  number range must exist per issuing OwnPJ before the fiscal-invoice legs are exercised (its management
  screen stays L9; the data is admin/seed-entered before L6).

## Open questions

- **Founder-gate realization:** all four FE stages are `structural` (mechanical human_gate is on `critical`
  only). Per doc-1 §4 the L6 founder verification is realized by the architect lifting drain per-layer
  after the founder tests the deployed layer (playbook §5.9), since there is no single natural capstone
  screen (the cycle now spans four FE stages). If a mechanical gate is preferred, `returns-stocktake-fe`
  (the stocktake terminus of the cycle) is the candidate to mark `critical` — flagged for the integrator.
- **order-to-stock spec-time scope:** whether a single document may MIX ZN-targeted and stock-targeted
  lines, or stock orders are a separate document mode, is left to the stage spec (acceptance says
  "handled per spec"). Either satisfies the unblock of vanzare_piese/revanzare_tva.
- **`touched` paths for new backend files** (e.g. the order-to-stock test) are best-estimate canonical
  paths; the builder may place a test alongside existing `procurement/tests/`.
