# L7 — l7-service-orders · phase-plan companion

**Layer:** Service-orders spine — Cont de plata (quote) editor + ZN.
**Phase dep (macro DAG):** `l6-stock-ops → l7-service-orders` (needs the IP4 parts picker + the real
L1–L5 master data + the L0 menu + the L5 lifecycle controls).
**Founder gate:** create a Cont de plata against a real Contract + Vehicle, add work/part/material
lines, apply each discount scope/method (Σ ≡ total to the cent), send + accept, then open the resulting
ZN and walk its statuses.

## Stages

| id | kind | role | risk | one-line |
|---|---|---|---|---|
| `cont-quote-land` | backend | **contract** | structural | [REBUILD] Graft the cont-quote-core branch; re-verify the EXISTING `/api/quotes/` API + discount Σ≡total + lifecycle + 62-day auto-refuse + R6 payment-state + relink guard against the now-real L1–L5 deps; resolve contest #104 (contested finding ASM-006); merge. FREEZE `/api/quotes/`. |
| `quote-editor-lines` | frontend | leaf | structural | [NEW] Cont/quote editor part 1 — header + lines grid; writes quote lines via `/api/quotes/` (ContLine/QuoteLine); the ZnLine anchor is created server-side by the quotes API (no direct `zn-core` dependency). |
| `quote-editor-flow` | frontend | leaf | structural | [NEW] Cont/quote editor part 2 — discount scope/method controls (Σ lines ≡ total) + send-for-coordination/accept/refuse + lifecycle/history. |
| `zn-core` | backend | **contract** | structural | [NEW] ZN+ZnLine behaviour over the frozen L1 skeletons (verification/procurement/production status layers, restant chain, cont→ZN line mapping). FREEZE the ZN API. |
| `zn-screen` | frontend | leaf | structural | [NEW] ZN board — status layers, lines, links back to its Cont; add-new-inline for parts/executors. |

## Contract stages + why

Two `role:contract` stages, each freezing a distinct seam:

- **`cont-quote-land`** freezes `/api/quotes/` — the cont editor (both FE parts) builds on it. It is a
  REBUILD (graft + re-verify + land the EXISTING API), not a green-field build.
- **`zn-core`** freezes the ZN REST surface — `zn-screen` builds on it. ZN is genuinely NEW.

`zn-core` is itself a DAG descendant of `cont-quote-land` (ZN is created from an accepted cont), so the
two-contract plan still satisfies contract-first reachability: every leaf has a contract ancestor.

## Intra-layer DAG

```
cont-quote-land ──► quote-editor-lines ──► quote-editor-flow
       │                                 ▲
       ├─────────────────────────────────┘  (quote-editor-flow also depends directly on the contract)
       └──► zn-core ──► zn-screen
```

Edges: `cont-quote-land→quote-editor-lines`, `cont-quote-land→quote-editor-flow`,
`cont-quote-land→zn-core`, `quote-editor-lines→quote-editor-flow`, `zn-core→zn-screen`.
The editor is split into two FE stages (header+lines, then discounts+lifecycle) per the doc-1 over-split
correction; part 2 depends on part 1 (same editor). All degrees ≤3.

## Deviations from doc-1 (the cont-quote API correction — VERIFIED in branch)

- **CORRECTION (doc-1 §1.1), confirmed by direct `git show` of the branch:** the
  `stage/service-orders.cont-quote-core` branch ALREADY ships the backend REST:
  - `backend/apps/so_quotes/api.py` (~400 lines): `QuotesCollectionView` (POST create / GET list),
    `QuoteDetailView` (GET / PUT edit), `SendForCoordinationView`, `AcceptView`, `RefuseView`,
    `RelinkView` — full `{"error":{code,message,details}}` envelope, F5 `doc_type_scope` rights, the
    Method-2 `revanzare_tva` gate, create/edit data-merge that preserves the snapshot on partial PUT.
  - `backend/apps/so_quotes/urls.py` mounting `/api/quotes/` (Collection/Detail/SendForCoordination/
    Accept/Refuse/Relink) + the `erp/urls.py` patch.
  - So `cont-quote-land` = **RE-VERIFY + LAND the existing API**, NOT build-from-scratch. "Peeled" =
    only the React editor is absent (→ `quote-editor-lines` + `quote-editor-flow`). The app is 37 files,
    1 commit behind main, merges 0-conflict (merge-tree).
- **The other so_quotes building blocks all exist on the branch** (verified): `discount.py` (the pure
  (method × scope) math core with the Σ effective_value ≡ Cont.total to-the-cent invariant + the
  largest-in-scope-line rounding residual), `lifecycle.py` (draft→awaiting→accepted/refused + the
  re-coordination revert through `status_layers.transition`), `auto_refuse.py` (62-day, param
  `quote_auto_refuse_timeout`, skips released conts), `payment_state.py` (R6-derived), `relink.py`
  (guard → ConformityTicket on ZN-mapped lines). The re-verify exercises each on the now-real deps.
- **ZN is NEW backend** built over the byte-frozen L1 `Zn`/`ZnLine` skeletons whose status columns are
  already declared on the models (`status_verification`/`status_procurement`/`status_production`,
  `Origin {ordinary,restant}`, the restant chain ref, `ZnLine.cont_line` PROTECT FK). `zn-core`
  implements the three status LAYERS + the restant chain + the cont→ZN mapping and freezes a ZN API.
- **Layering preserved:** procurement already references the ZN by BigInteger (lazy import); `zn-core`
  adds no upward FK.

## Open questions

- **[BLOCKER, resolved AT L7-time, not a plan-authoring blocker] Contest #104 (contested finding ASM-006)** —
  `unresolved_contest` on `stage/service-orders.cont-quote-core`, dossier `/artifact/1200`, tracked as
  decision #26. It was NEVER resolved (handoffs ARH-01..03 all say "decision #26 — leave"; the branch is
  AWAITING_HUMAN). The reseed-playbook §3 mandates EXPORT the dossier + the contested finding and feed
  it as a documented INPUT to the L7 cont-quote-core rebuild spec. The `cont-quote-land` acceptance
  encodes exactly that: feed the dossier, classify the finding per architect-operations §1
  (comply → fix in the graft / accurate-no-action → settled / spec-lie → correct the spec), record the
  disposition before merge. **The {approved | rework:BUILD | rework:SPEC} call is made when L7 is reached
  (architect reads the dossier, brings a recommendation, founder decides) — it does NOT block authoring
  or seeding this structure.** Integrator action: ensure the dossier is exported to a carry-forward doc
  before the DB is archived (reseed-playbook §3), so the spec actually has it.
- **Vehicle intake (`act primire`)** rides L7 service-orders per doc-1 Q6 (the L5 Vehicle form is the
  interim master-data path, NOT throwaway). This plan does not add a separate intake stage; if the
  founder wants a dedicated intake surface on the ZN/cont flow, it is a spec-time addition to
  `quote-editor-lines` or a follow-on — flagged.
- **`touched` paths for the NEW ZN + editor files** (`service_orders/zn.py`, `service_orders/api.py`,
  `frontend/src/features/quotes/*`, `frontend/src/features/zn/*`) are canonical best-estimates; the
  builder may co-locate per existing app conventions.
- **Founder-gate capstone** is `zn-screen` (open the ZN from an accepted cont + walk its statuses) — a
  natural terminal screen. Left `structural`; if a mechanical human_gate is wanted, `zn-screen` is the
  candidate to mark `critical`. Flagged for the integrator.
