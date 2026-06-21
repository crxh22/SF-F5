# Stage spec — inventory-procurement.warehouse-code (DRAFT for audit, 21-06-2026)

**Risk class: structural** (foundation amendment → Opus builder + dual auditors). Author: ETAPA-5u (architect-defined fix). 
**Depends on (integrated):** the inventory-procurement integration branch as-is (foundation merged).
**Dependents (must rebuild AFTER this merges):** `inventory-procurement.stocktaking` (rework:BUILD — switch depozit keying pk→code), `inventory-procurement.stock-views` (new split — keys depozit on code from the start).
**Contracts touched (architect-sanctioned foundation amendment):** F7 nomenclature (`nomenclature.Warehouse` gains a `code`); F5 rights convention (`accounts/rights.py:10-11`) becomes satisfiable for `depozit` (no convention edit — the code makes the existing convention applicable). This is the root resolution of **INT-ST-DEPOZIT-KEY-PK-001 / OPEN-AA5**.

---

> **⚠️ EXECUTION CORRECTION (ETAPA-5v, 21-06-2026) — this DRAFT was superseded at execution; the authoritative instruction is the `rework:BUILD` `--reason` on escalation #99, `/tmp/sf-rework-99-reason.txt`.** 5v verified every pointer against the live stocktaking worktree before firing and found two stale items in the draft below — corrected here:
> 1. **Migration is `0004_warehouse_code`, NOT `0003`** — `0003_specificpart_enrichment` already exists in the worktree, so the new migration is **0004** and **depends on `("nomenclature","0003_specificpart_enrichment")`** (the current leaf), NOT `0002_party_links`. §3.2 / §6 below are stale on this point. (Cause: the draft was audited against a branch without specificpart_enrichment.)
> 2. **Factory code prefix must NOT be `dep-`** — `department()` in the same `factories.py` already uses `dep-`; use a distinct prefix (e.g. `wh-`). §3.3's `dep-{n}` example is wrong.
> 3. **Execution shape:** the fix was **folded into `stocktaking`'s `rework:BUILD`** (escalation #99), NOT built as a standalone `warehouse-code` stage (adding a stage mid-phase needs risky DB/replan surgery; the rework path is proven and stocktaking needs the keying switch anyway). So §5/§6/§7's "this stage merges then stocktaking reworks" two-step is collapsed into one rework: the SAME builder adds `Warehouse.code` AND switches the keying (§5 of the `--reason`). The keying resolvers receive a depozit **pk** from the request and must **resolve pk→`Warehouse.code`** for the rights key (the draft's "return Warehouse.code" elides this); in-file precedent: `filter_by_rights(OwnPJ, ("own_pj","code"))` at `api.py:~418`.
> Everything else in this draft (scope boundary, backfill design, acceptance tests §4, falsifiability §5) is sound and was carried into the `--reason`.

---

## 1. Objective

Give the `depozit` nomenclature table (`nomenclature.Warehouse`) a **stable opaque `code`**, exactly like its sibling nomenclature tables (Currency, Bank, ExpenseCategory, Direction, PaymentCategory), so that the `depozit` **rights dimension** can be keyed on a stable slug — as the F5 convention requires (`accounts/rights.py:10-11`: "Dimension keys are opaque stable codes/slugs, never auto-increment PKs ... OPEN-AA5 pins per-dimension formats at first consumption").

**Root-cause context (why this stage exists):** `Warehouse` was created in `foundation.config-registry` WITHOUT a `code` (the base `NomenclatureBase` deliberately omits it — each concrete model adds it where needed; Warehouse was missed). It is the ONLY nomenclature catalog table used as a rights dimension that lacks a code. The first per-key consumer (`stocktaking`) was forced to key on the Warehouse PK-as-string, which the integration validator correctly flagged as a convention violation (INT-ST-DEPOZIT-KEY-PK-001). The factory has no mechanism to accept a Tier-2 finding; the root must be fixed. This stage fixes the root: add the missing `code`.

## 2. Scope boundary

**In scope:**
- Add `code = models.CharField(max_length=100, unique=True)` to `nomenclature.Warehouse`.
- A Django migration that adds the column AND backfills a stable, unique code for any pre-existing warehouse rows (the live dev DB may hold a few; warehouses are NOT seeded, see §4.2).
- Update the warehouse test factory (`backend/apps/registers/tests/factories.py:~104`) to set a `code`.
- Add `"warehouse": {"code": {"unique": True}}` to the declarative table spec in `backend/apps/nomenclature/tests/test_tables.py` (the existing per-table code/uniqueness fence).
- A test proving the `depozit` rights dimension can be keyed on `Warehouse.code` (a `Right` row with `dimension_type="depozit"`, `dimension_key=<warehouse.code>` resolves via `has_right`).

**Out of scope (explicit — other owners / other stages):**
- **No consumer keying change here.** `stocktaking` switches its `depozit` keying (pk→code) in its OWN rework:BUILD; `stock-views` keys on code from the start. This stage ONLY makes the code exist.
- **No edit to `accounts/rights.py`** — the convention is already correct; adding the code makes it satisfiable. (We are fixing the root, NOT carving a PK exception.)
- **No change to any other nomenclature table, and no other dimension is affected.** Containment reasoning (audit-corrected): `Right.dimension_key` is a **free-form `CharField(max_length=150)` with NO foreign key** to any table (`accounts/models.py:130`) — the "opaque slug, never PK" convention is a **discipline each consumer applies at key time**, not a table relationship. Of the 12 `DIMENSION_TYPES`, `depozit` is the only one whose natural backing table (Warehouse) is slug-less AND has a real per-row consumer that must key on actual rows. The other slug-less catalog tables (PriceLevel/GenericPart/SpecificPart) back NO dimension; the other dimensions are either backed by tables that have a code (direction→Direction, own_pj→OwnPJ) or are free-slug/wildcard-only (money_location, report, tx_category, … keyed by literal slugs in code, never PKs). So Warehouse is the unique fix; nothing else is forced onto a PK.
- **Fences NOT to edit (so the builder does not over-reach):** `nomenclature/tests/test_framework.py` uses `BASE_FIELDS <= field_names` (subset check — unaffected by adding a column); `registers/tests/test_party_link_seams.py` pins only `responsible` (unaffected). Only the exact-column fence in `test_tables.py` must change (§4.1).
- No new business value / F7 param key; no F5 dimension added.

**Reused as-built (read-only):** `NomenclatureBase` (`name`, `active`, `sort_order`, timestamps, `updated_by`); the `@nomenclature("warehouse")` registry decorator; the generic nomenclature serializer (`fields="__all__"`, `apps/nomenclature/api.py`).

## 3. Design

### 3.1 Model change (`backend/apps/nomenclature/models/catalog.py`)
Add to `class Warehouse(NomenclatureBase)`:
```python
code = models.CharField(max_length=100, unique=True)
```
Place it as the first field (mirroring siblings). Keep the existing `responsible` FK unchanged. `max_length=100` matches Bank/ExpenseCategory/Direction/PaymentCategory (Currency's 3 is currency-specific).

### 3.2 Migration (`backend/apps/nomenclature/migrations/0003_warehouse_code.py`)
Three operations in ONE migration (the standard safe add-unique-column pattern — existing rows must not violate the constraint):
1. `AddField` `code` as **nullable** (`null=True`) temporarily.
2. A `RunPython` backfill (with a reverse no-op): for every existing `Warehouse`, set `code = _slug(name)`, where `_slug` lowercases, ASCII-folds, replaces non-alphanumerics with `-`, trims, and **guarantees uniqueness** by appending `-<pk>` on the first collision (or when the slug is empty). Deterministic, idempotent.
3. `AlterField` `code` to `null=False, unique=True`.
Depends on `0002_party_links`. Reversible (the reverse drops the column).

### 3.3 Test factory (`backend/apps/registers/tests/factories.py:~104`)
The `warehouse()` factory's `Warehouse.objects.create(**kwargs)` must default a unique `code` when the caller does not pass one, using the **sequence style already used in this file** for siblings (e.g. `f"dep-{n}"` via the file's counter — NOT a uuid), so existing tests that create warehouses without a code keep passing under the new `unique=True, null=False` constraint and stay consistent with the file's conventions.

### 3.4-key-width note
`Warehouse.code` is `max_length=100`; `Right.dimension_key` is `CharField(max_length=150)` — a 100-char code always fits the dimension-key column. No width conflict.

### 3.4 No consumer changes (boundary reminder)
This stage compiles and tests GREEN on its own. The `depozit` rights keying in `apps/inventory/api.py` (stocktaking) is NOT touched here — it is switched in stocktaking's rework. The integration validator on THIS stage sees only an additive foundation column (no convention contradiction introduced).

## 4. Acceptance tests (write FIRST; stage done when all committed and green via the full suite)

### 4.1 Model + uniqueness
- `backend/apps/nomenclature/tests/test_tables.py`: add `"warehouse": {"code": {"unique": True}}` to the declarative spec; the existing framework asserts the field exists, is unique, and `max_length` if given.
- A test that two warehouses with the same `code` raise `IntegrityError`.

### 4.2 Migration + backfill
- A migration test (this is the FIRST `RunPython` data-migration in the nomenclature app — no in-repo backfill precedent to mirror; write it fresh and carefully): create warehouse rows at the pre-`0003` state, run the migration, assert every warehouse has a non-null, unique `code`. It MUST cover all three non-trivial paths explicitly: (a) normal name → slugified code; (b) **two same-named warehouses** → distinct codes via the `-<pk>` suffix; (c) **an empty / all-punctuation name** (slugs to nothing) → a deterministic fallback code (e.g. `depozit-<pk>`), never empty/duplicate. NOTE: warehouses are NOT seeded anywhere (grep-confirmed: only `registers/tests/`); on a fresh DB the backfill is a no-op, so these synthetic rows are the only way to exercise it.

### 4.3 FKs unaffected
- Assert that existing FK relations to Warehouse still resolve (registers movements `depozit`, `Warehouse.responsible`, etc.) — adding a `code` column does not alter the PK or any FK. A smoke test that an R2/R3/R4 movement referencing a warehouse still saves/reads.

### 4.4 Rights dimension keyable on code (the WHOLE point)
- A test: create a warehouse with `code="dep-01"`, grant a user `Right(right_type="view_balances", dimension_type="depozit", dimension_key="dep-01")`, assert `has_right(user, "view_balances", "depozit", "dep-01")` is True and for a different code is False. Proves the convention is now satisfiable WITHOUT a PK.

### 4.5 Generic nomenclature API — additive read, now-required write (audit-corrected)
The generic nomenclature serializer uses `fields="__all__"` (`apps/nomenclature/api.py:38`), so `code` is picked up **additively** with no serializer edit. (NOTE: the `default_code="unknown_nomenclature"` constant at `api.py:23` is the DRF error code of the `UnknownNomenclature` **exception** for an unknown nomenclature *key* — it is NOT a per-field fallback; do not touch it.)
- Assert the GET warehouse-nomenclature payload now **includes `code`**; the existing list/GET generic-API test stays green.
- **Intended contract change to state, not a regression:** because `code` is `null=False, unique=True` with no blank default, a POST to create a warehouse via the generic nomenclature endpoint now **requires `code`** (previously creatable with `name` alone). No existing API test POSTs a warehouse (the generic POST test uses `currency`, which always sends `code`), so nothing breaks — but the builder must treat the now-required field as intended hygiene.

### 4.6 Full suite green
- `bash scripts/test.sh` green (backend + frontend + quality: ruff/format/mypy/import-linter — the import-linter contract is unchanged; no new app, no new cross-layer import).

## 5. Falsifiability / escalation triggers (STOP, do not improvise)
- **The backfill cannot guarantee uniqueness** (e.g. data with duplicate names AND duplicate pks — impossible, pk is unique) → the `-<pk>` suffix is the guaranteed disambiguator; if a different uniqueness rule is needed, STOP and escalate.
- **A consumer outside scope appears to require editing** to compile (something already keys depozit and breaks) → STOP and escalate (it means a consumer is merged that this plan didn't account for; the architect re-sequences).
- **The migration is not cleanly reversible** or risks existing data → STOP; a foundation migration must be safe + reversible.
- **The convention (`accounts/rights.py`) seems to need editing** → it does NOT; if the auditor thinks so, that is the carve-out approach we explicitly rejected — STOP and escalate.

## 6. Deliverables
- `backend/apps/nomenclature/models/catalog.py` (Warehouse.code).
- `backend/apps/nomenclature/migrations/0003_warehouse_code.py` (add + backfill + alter).
- `backend/apps/registers/tests/factories.py` (factory defaults code).
- `backend/apps/nomenclature/tests/test_tables.py` (+ warehouse code spec) + new tests (§4.1–4.5) in the appropriate nomenclature/accounts test modules.
- This `spec.md`; build/validation/audit/integration reports per the frozen layout.
- **OPEN-AA5 resolution note:** the `depozit` dimension format is now PINNED to `Warehouse.code` (a stable slug), resolving the first-consumption pin correctly (no PK).

No new app/register/param/F5-dimension. No business-logic change — pure foundation key-hygiene.

## 7. Sequencing & necessary-not-sufficient note (architect tracking)
This stage is **necessary but not sufficient** to clear INT-ST-DEPOZIT-KEY-PK-001. The finding fires on `stocktaking` keying `depozit` on `Warehouse.pk`; it clears ONLY when stocktaking's **dependent rework** switches its keying pk→code (using the `Warehouse.code` this stage creates). Required order, enforced by the DAG:
1. **this stage merges** (Warehouse.code exists on the integration branch).
2. **stocktaking** rework:BUILD → switch `_depozit_key_from_query/_from_payload/_from_document` + `filter_by_rights(Warehouse, ("depozit","pk"))` to `("depozit","code")`, update its depozit-rights tests, re-validate, re-audit; its Tier-2 now finds no contradiction → DONE. (Needs its budget raised — it is at the 450M critical cap.)
3. **stock-views** (new backend/UI split) → keys `depozit` on code from the start.
Do not assume this stage alone fixes the finding; the architect must sequence + land step 2.
