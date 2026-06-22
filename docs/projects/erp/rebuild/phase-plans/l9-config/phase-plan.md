# L9 — Config / users / rights management UI (phase-plan)

**Phase id:** `l9-config` · **Macro position:** LAST module, after `l8-treasury` (linear macro DAG).
The curated in-app surface for what is currently Django-admin-only and founder-only
(parameters / rights / users / devices / residual config nomenclatures).

**FOUNDER DECISION (DRAFT PART-5 Q3 + doc-1):** build-vs-keep-Django-admin-for-v1 is a FOUNDER call.
These three stages are authored **fully and build-ready** so the sequence is complete — but the whole
layer (or individual stages) may be **deferred or reduced to Django-admin v1**. Recorded recommendation
(ARH-01, DRAFT PART-6): Django-admin v1 + tell the founder admin is the cockpit, deferrable to L9. This
plan does not presume that decision; it presents the full build for the founder to choose.

**Source material:** DRAFT LAYER 9. Code verified read-only on `erp-workspace` main:
`nomenclature/parameters.py:set_param` (history-preserving writer, admin-only, no REST);
`accounts/api.py` (Device list/approve/revoke already REST + non-destructive; rights meta-admin is
superuser-only and the only template op `_overwrite_rights` is **destructive by contract**).

---

## Stages

| id | kind | role | risk | one-line |
|---|---|---|---|---|
| `config-rights-rest` | backend | **contract** | **critical** | NEW: Parameter history-preserving `set_param` REST + CURATED **non-destructive** rights endpoints (single-row grant/revoke + merge-apply); re-verify existing Device endpoints; freeze the config seam. **Critical** — the rights-mutation backend is the security fault line (founder human_gate); stays role:contract. |
| `config-params-fe` | frontend | leaf | structural | NEW: parameters editor (typed values + history) + exchange-rate/Diapazon admin + print-template overrides + residual nomenclatures StatusLabel/TicketType. |
| `users-rights-fe` | frontend | leaf | **critical** | NEW: users create/edit + device approval queue + per-dimension rights editor (additive grant/revoke, **non-destructive** merge-apply). Founder human_gate (sensitive). |

## Contract stage + why

`config-rights-rest` is `role:contract`. It freezes TWO seams both FE leaves build on: the Parameter
`set_param` REST surface (`config-params-fe`) and the curated non-destructive rights/device surface
(`users-rights-fe`). Backend-first, thin interface freeze — the contract-first pattern. Floor-exempt as
a contract but carries full acceptance (7 AC / 6 touched). It is risk_class **critical** (the rights-mutation
backend is the security fault line — a wrong grant/revoke or a leaked meta-admin plane is an auth-correctness
defect), so the founder human_gate is its per-stage verification. (Note: `own-pj-rest` and `rate-rest`
deliberately stay `structural` — they are not the rights fault line; only the rights-mutation backend is
promoted.)

## Intra-layer DAG (fan-out from the contract)

```
config-rights-rest ──▶ config-params-fe
                  └──▶ users-rights-fe
```

Contract-first reachability satisfied: both leaves are DAG descendants of the contract. Degrees:
config-rights-rest out=2 (deg 2); config-params-fe in=1 (deg 1); users-rights-fe in=1 (deg 1). All ≤6.
The two FE stages are independent of each other (parameters vs users/rights) — they fan out from the
shared backend contract and can build in parallel once it is frozen.

## Founder gate (mechanical, split across the two FE stages)

Edit a business parameter (history kept) — on `config-params-fe`. Create a second user with scoped
rights + approve a device — on `users-rights-fe` (typed **critical** → mechanical human_gate). All
WITHOUT Django admin. The two FE stages together realize the DRAFT L9 gate. The backend
`config-rights-rest` is ALSO **critical** (the rights-mutation seam), so the founder additionally gates the
rights/parameter backend itself before either FE stage builds — two founder human_gates in L9
(`config-rights-rest` backend + `users-rights-fe` frontend).

## Key code anchors (verified, file:concept)

- `nomenclature/parameters.py:set_param(key, value, *, actor)` — **history-preserving** writer (appends
  `ParameterHistory`); `to_stored` typing (decimal as string, **float rejected**; int/str/bool strict;
  json/pointer as-is); `ParameterMissing`/`ParameterUnset`/`ParameterTypeError`. Editable ONLY via
  `ParameterAdmin`/`ParameterHistoryAdmin` today — **no REST** → L9.1 exposes it.
- `accounts/api.py` — `DeviceListView`/`DeviceApproveView`/`DeviceRevokeView` (`CanManageDevices`)
  **already REST, non-destructive** → re-verify, the L9.3 device queue reuses them. `MeView`,
  login/logout/reauth, `ViewPreference` exist.
- `accounts/api.py:_overwrite_rights` — the only template op, **"destructive by contract"** (deletes
  ALL of a user's `Right` rows then bulk-creates). `IsSuperuserMetaAdmin` gates rights meta-admin;
  `is_superuser` and data-rights are **separate planes** (spec §4.8). → L9.1 adds a CURATED
  **non-destructive** surface (single-row grant/revoke + MERGE apply/copy); the destructive overwrite is
  NOT exposed to the FE.

## Deviations from the DRAFT

- **None structural.** DRAFT 9.1/9.2/9.3 map 1:1 to `config-rights-rest` / `config-params-fe` /
  `users-rights-fe`. Short kebab ids replace the `9.x` numbering.
- **Sharpened "rights endpoints" to "CURATED NON-DESTRUCTIVE"** against the verified reality: the
  existing rights template op is destructive (full overwrite); device endpoints already exist
  non-destructively. So L9.1 is precisely: add Parameter REST (genuinely new) + add a non-destructive
  rights surface (single-row grant/revoke + merge), and re-verify the device endpoints (already there).
- **`config-rights-rest` promoted `structural` → `critical`** (dual-audit reconciliation): the
  rights-mutation backend is the security fault line — grant/revoke + the meta-admin plane are
  auth-correctness, so the founder personally gates it (mechanical human_gate) per authoring-notes §4. It
  stays `role:contract` (the seam both FE leaves freeze onto). `own-pj-rest`/`rate-rest` deliberately stay
  `structural` (not the rights fault line).
- **`users-rights-fe` typed `critical`** (the mandate offered "structural or critical — your call"):
  it is the sensitive auth/rights plane (create users, approve devices, grant rights) — the founder
  must personally gate it, so `critical` (mechanical human_gate) per authoring-notes §4. `config-params-fe`
  stays `structural` (config editing, founder visual gate, not auth-sensitive).

## Open questions

1. **[FOUNDER — the layer-level decision] Build this in-app surface vs keep Django admin for v1.**
   DRAFT PART-5 Q3 / doc-1. All three stages are authored build-ready, but the founder may defer/reduce
   the whole layer (or per-stage) to Django-admin v1. Recommendation on record: Django-admin v1 + tell
   the founder admin is the cockpit (deferrable). Sub-options if partially built: ship `config-params-fe`
   (parameters are edited more often) but keep users/rights on admin; or ship all three. **Needs a
   conscious ratified founder decision before L9 dispatches.**
2. **Rights-delegation model (OPEN-AA3).** Today rights meta-administration is superuser-only
   (`IsSuperuserMetaAdmin`); the code comments flag delegation as an open question. The L9.3 rights
   editor assumes the operator is a superuser/meta-admin. If the founder wants delegated (non-superuser)
   rights management, that is a backend policy change beyond this layer's curated-endpoints scope — flag,
   do not silently widen.
3. **Diapazon management home.** The DRAFT puts Diapazon's management screen in L9.2 but its DATA must
   exist by L6 (seed/admin-entered before L6 fiscal invoices). Confirm L9.2 owns the full management UI
   while L6 only required seeded data — no conflict, but the founder should know the L9 screen is the
   permanent editor for ranges that were bootstrapped earlier.
