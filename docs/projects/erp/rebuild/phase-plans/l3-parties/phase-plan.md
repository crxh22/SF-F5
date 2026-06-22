# l3-parties — phase-plan companion

**Layer:** L3 — Commercial parties: Counterparty (+roles) + Contract. The biggest NEW backend (the gap-audit #1 alarm; clerks touch these daily, today admin-only). Contract is a deep money/legal node depending on L1/L2.

## Contract seams
Two `role:contract` stages (both are NEW backend slices of `parties/api.py` that downstream FE freezes onto):
- `counterparty-rest` — the primary seam: Counterparty + CounterpartyRole CRUD (commercial rows only). It is the add-new-inline target for **every** counterparty picker app-wide, so it is frozen as a contract.
- `contract-rest` — Contract CRUD carrying the full money/legal `clean()`; frozen as a contract because the Contract FE depends on its exact field-level validation surface. It depends on `counterparty-rest` (Contract FK→Counterparty).

Both contracts present ⇒ the reachability rule requires every leaf to descend from a contract: `counterparty-fe` descends from `counterparty-rest`; `contract-fe` descends from both `contract-rest` and `counterparty-rest` (the latter for inline Counterparty add).

## Stages
| id | kind | role | risk | one-line |
|---|---|---|---|---|
| `counterparty-rest` | backend | contract | structural | NEW. Counterparty + CounterpartyRole CRUD, COMMERCIAL rows only via `.commercial()` (system rows guarded); role add/remove; supplier/client/employee/outsourcer filters. Adds a nullable `active` column + reversible migration (deactivate = PUT active=false). |
| `contract-rest` | backend | contract | **critical** | NEW. Contract CRUD; full clean() surfaced as 400s (alb⇒own_pj req / negru⇒forbidden; ≤1 money location; pay-cat direction match). Founder human_gate. |
| `counterparty-fe` | frontend | leaf | structural | NEW. List+create/edit, multi-role chips; the reusable add-new-inline counterparty picker app-wide. |
| `contract-fe` | frontend | leaf | structural | NEW. Conditional fields by alb/negru + money-location XOR + direction-filtered pay-cat pickers; inline add-new Counterparty/PriceLevel/PaymentCategory. |

## Intra-layer DAG
```
counterparty-rest ──► contract-rest ──► contract-fe
        │                                   ▲
        ├──► counterparty-fe                │
        └───────────────────────────────────┘
```
Linear backbone counterparty→contract; each FE leaf hangs off its backend contract. `contract-fe` also depends on `counterparty-rest` (inline Counterparty add). Max degree 3 (≤6).

## DRAFT founder gate (per the DRAFT L3 gate)
Founder creates a commercial Counterparty with two roles, then a Contract (alb) against it + an OwnPJ, watches the alb/negru validation block bad combos, sets default money location (cash_desk XOR bank_account) + price level, and confirms the contract is now selectable where a quote will need it.

## Deviations / decisions vs the DRAFT
- **`contract-rest` is risk_class `critical`** (DRAFT left it open / "NEW backend"). Per authoring-notes §4: it carries money/legal correctness the founder must personally gate → critical (founder human_gate is the per-stage verification), where `counterparty-rest` stays `structural`. This is the natural founder-verification capstone for L3 on the backend side; the FE gate rides the visual review on `contract-fe`.
- **System-counterparty guard is load-bearing on the API**, not just the queryset: `counterparty-rest` must prevent a commercial create/edit from acquiring `system_kind`/`system_own_pj`/the SYSTEM_COUNTERPARTY role (the `.commercial()` exclusion only hides them from reads).
- **clean() must be surfaced as API 400s** on `contract-rest` (the DB CheckConstraints alone are not enough for a usable form): all five branches — alb/negru ⇒ own_pj, money-location XOR, in/out direction match — fire through the serializer's full_clean.

## Open questions
1. **Soft-delete on Counterparty — RESOLVED (a):** the model has no `active` field on `Counterparty` (it is on `Contract`), and the standing soft-delete=PUT active=false law needs one. Disposition: option (a) — `counterparty-rest` adds a nullable `active` column + a reversible migration (`apps/parties/models.py` + `migrations/0003_counterparty_active.py`) so deactivate = PUT active=false (never DELETE), and the `.commercial()` queryset/serializer expose it. (Was: flagged for a founder/architect call; now baked into the stage per the dual-audit reconciliation.)
2. **Module path convention** (`frontend/src/modules/parties/…`) is assumed from the foundation shell; if L0's menu registry fixes a different path, the `touched` FE paths re-home (cosmetic, not structural).
