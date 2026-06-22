# L4 — Parts catalog + production nomenclatures + Warehouse (phase `l4-catalog`)

**Shape:** mostly management UIs around EXISTING nomenclature REST + the search-only `PartsPicker`.
2 backend re-verify (KEEP) + 2 NEW frontend. Validates clean (read_phase_plan OK, size-gate clean).

## Stages
| id | kind | role | risk | one-line |
|---|---|---|---|---|
| `parts-prod-rest` | backend | **contract** | routine | [KEEP / **VERIFY-ONLY**] re-verify + FREEZE the parts (`generic_parts_catalog`/`specific_parts_catalog`) + production (`stages`/`stage_state`/`departments`/`works`/`executor`) nomenclature seam: real keys, M2M, original_code race-safety constraint. Defect requiring a code change → ESCALATE for a separate structural fix stage, never patch in place. |
| `warehouse-rest` | backend | leaf | routine | [KEEP / **VERIFY-ONLY**] re-verify `warehouse` CRUD — nullable `responsible` Counterparty FK through the API. Defect requiring a code change → ESCALATE, never patch in place. |
| `parts-catalog-fe` | frontend | leaf | structural | [NEW] two-layer Generic→Specific catalog manager (OEM/aftermarket codes); EXTENDS not replaces PartsPicker |
| `prod-warehouse-fe` | frontend | leaf | structural | [NEW] Stage/StageState/Department/Work/Executor/Warehouse screens; Dept↔Stage + Work↔Stage M2M editors; Executor→counterparty+department+percent |

## Contract
`parts-prod-rest` is the sole `role:contract` — it freezes the catalog+production REST surface every L4 FE
leaf builds on (real registry keys, the SpecificPart enrichment columns + the partial-unique `original_code`
constraint that makes `procurement/catalog.py:resolve_or_create_specific` race-safe, and the production M2M
editors). KEEP/routine because it re-verifies an existing endpoint (no new code expected); contract stages
are floor-exempt but this one still carries 7 AC / 4 touched.

## Intra-layer DAG
```
parts-prod-rest ──┬── warehouse-rest ──┐
                  ├── parts-catalog-fe │
                  └── prod-warehouse-fe ◄┘   (prod-warehouse-fe also depends on warehouse-rest)
```
Degrees: parts-prod-rest 3, warehouse-rest 2, parts-catalog-fe 1, prod-warehouse-fe 2 (all ≤6). Every
leaf is a descendant of the contract → contract-first satisfied.

## Deviations
- `resolve_or_create_specific` is a **procurement-app callable** (`apps/procurement/catalog.py`), NOT a
  nomenclature endpoint (the DRAFT/brief phrasing could read as a REST verb). The contract re-verifies the
  **constraint** it relies on; resolve is not re-exposed here.
- `procent_executor` is the real model field (DecimalField fraction) — the brief's "percent".

## Open questions
None blocking. The founder L4 gate (add Generic+Specific with OEM code, Stage/Department/Executor/Warehouse,
all selectable in pickers) is met jointly by `parts-catalog-fe` + `prod-warehouse-fe` (structural, with the
frontend UI/UX gate) — no mechanical `critical` human_gate added (the layer's verification is the architect's
per-layer drain-lift after founder testing).
