# L5 — Vehicle + engine re-verify + documents-lifecycle API (phase `l5-engine-docs`) — the KEYSTONE

**Shape:** the documents-lifecycle REST API is the contract L6/L7/L8 plug into. 4 backend (3 NEW + 1 KEEP
re-verify) + 1 NEW frontend. Validates clean (read_phase_plan OK, size-gate clean).

## Stages
| id | kind | role | risk | one-line |
|---|---|---|---|---|
| `vehicle-rest` | backend | **contract** | structural | [NEW] Vehicle slice of `parties/api.py` — owner Counterparty, make/model/reg/mileage; custody/follow_up READ-ONLY (intake = L7). Adds a nullable `active` column + reversible migration (deactivate = PUT active=false). |
| `docs-lifecycle-api` | backend | **contract** | **critical** | [NEW] build `documents/api.py` over engine.py — the **MUTATE** verbs finalize/edit/cancel-storno; the standing keystone contract every later doc-type plugs into; founder human_gate. (Reads split out → `docs-history-read`.) |
| `docs-history-read` | backend | leaf | structural | [NEW] the **READ** half of `documents/api.py` — version-history + dependency-cascade-PREVIEW GETs over the engine; descends from the lifecycle contract |
| `engine-reverify` | backend | leaf | routine | [KEEP / **VERIFY-ONLY**] re-verify posting/register/snapshot/period + fiscal-numbering (R15A/B + Diapazon) THROUGH the new API; prove idempotency + write-guards; NO rebuild. Engine defect requiring a code change → ESCALATE for a separate structural fix stage, never patch the engine in place. |
| `vehicle-fe` | frontend | leaf | structural | [NEW] Vehicle screen + the REUSABLE document-actions controls (cancel/storno/edit/history) L6/L7/L8 mount — the create-only pattern stops here |

## Contracts (two roots)
- `docs-lifecycle-api` — the keystone. Freezes `documents/api.py` as the lifecycle seam every later operational
  layer plugs into. **critical** (money/posting/irreversible engine paths + founder personally gates). Scope is
  now the **MUTATE verbs only** (finalize/edit/cancel-storno) — the version-history + cascade-preview READ
  endpoints split out into the sibling `docs-history-read` leaf (descends from this contract) per the dual-audit
  reconciliation; the contract stays the single keystone root. The codex per-type-adapter concern is resolved in
  the acceptance: `edit` is ONE generic verb wrapping the engine's reverse-and-repost around each producer's
  EXISTING create serializer (per-producer {base,data} payload) — no per-type edit adapter is written.
- `vehicle-rest` — the second contract. Freezes the Vehicle `parties/api.py` seam the Vehicle FE builds on.
  Promoted from leaf→contract so reachability holds WITHOUT a false edge: Vehicle REST has no code dependency
  on the documents API, so making it a leaf would force an artificial edge to satisfy contract-first. As a
  contract root it stands honestly on its own. Adds a nullable `active` column + reversible migration so the
  standing soft-delete law (deactivate = PUT active=false, never DELETE) has a target (Vehicle has none in real code).

## Intra-layer DAG
```
docs-lifecycle-api ──┬── docs-history-read ──┐
                     ├── engine-reverify     │   (re-verify exercises the engine THROUGH the exposed API)
                     └── vehicle-fe ◄────────┘
                              ▲
                         vehicle-rest
```
Edges: `docs-lifecycle-api→docs-history-read`, `docs-lifecycle-api→engine-reverify`,
`vehicle-rest→vehicle-fe`, `docs-lifecycle-api→vehicle-fe`, `docs-history-read→vehicle-fe`.
Degrees: vehicle-rest 1, docs-lifecycle-api 3, docs-history-read 2, engine-reverify 1, vehicle-fe 3 (all ≤6).
`vehicle-fe` consumes BOTH document seams (mutate from `docs-lifecycle-api`, history/cascade-preview from
`docs-history-read`). All three non-contract stages descend from a contract → contract-first satisfied.

## Deviations
- **engine-reverify edge direction:** modelled as a descendant of `docs-lifecycle-api` (the API → re-verify),
  i.e. the engine is re-verified *through the newly exposed lifecycle API on the test ERP*, not just as
  callable-Python. This is both the honest dependency (re-verify consumes the API) AND the stronger proof
  (exercises the real seam). It also resolves reachability for the routine re-verify stage cleanly.
- Vehicle's custody/follow_up + date_* columns are exposed READ-ONLY (F4 frozen skeleton owned by L7 intake);
  the interim Vehicle form is the quick master-data path, not throwaway (per DRAFT Q6).

## Open questions
1. **Documents-API read-vs-mutate split — RESOLVED (split now, at planning).** The dual-audit reconciliation
   takes the escape hatch at the only legitimate split point (planning): `docs-lifecycle-api` keeps the MUTATE
   verbs (finalize/edit/cancel-storno) and stays the keystone `role:contract`; the version-history +
   dependency-cascade-PREVIEW read GETs become the sibling `docs-history-read` (backend · structural · leaf),
   a DAG descendant of the contract. Both stages are independently builder-sized (lifecycle 7 AC / 5 touched;
   read 6 AC / 4 touched). The codex per-type-adapter concern is resolved on the mutate stage: `edit` wraps the
   engine's reverse-and-repost around each producer's EXISTING create serializer (generic verb + per-producer
   payload) — no new per-type edit adapter. (Was: kept as one stage with an escape hatch; now split.)
2. **Vehicle contract-vs-leaf (resolved → contract).** Chosen `role:contract` (see Contracts) to avoid a false
   reachability edge; flagged so the integrator can collapse it to a leaf IF a future restructure gives it a
   natural contract parent.
3. **Integrator/Diapazon prerequisite (carried).** Diapazon (fiscal ranges, FK → OwnPJ) data must be
   seeded/admin-entered for each issuing OwnPJ before the L6 fiscal-invoice flow (and before `engine-reverify`
   can exercise the R15A/B path). Its management screen is L9 — so this is a seed/admin-entry prerequisite,
   noted in `engine-reverify` acceptance and for L6.
