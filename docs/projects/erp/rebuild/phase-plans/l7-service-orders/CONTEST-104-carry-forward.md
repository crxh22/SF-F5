# Carry-forward — contest #104 (ASM-006) + decision #26 · cont-quote-core → L7 `cont-quote-land`

**Provenance.** Exported by ARH-05 on **23-06-2026** from the LIVE `factory.db` **before** the Strategy-A
re-seed archive (reseed-playbook §3 / §8b step 3; L7 phase-plan.md "Open questions" §Contest #104). The
live-DB locators (`/artifact/1200`, decision-request #26) do **not** survive the archive — this doc
preserves the substance + the surviving **git** locators so the L7 `cont-quote-land` spec actually has it.

> **Status of the decision: SUPERSEDED by L7 `cont-quote-land`.** Decision-request #26 (a pending
> `escalation_tradeoff` on `service-orders.cont-quote-core`, options `{approved | rework:BUILD | rework:SPEC}`)
> was **NEVER answered** (created 2026-06-22T09:36:10Z, status `pending`; handoffs ARH-01..04 all "leave").
> It is moot: the stage is archived and **re-slotted into L7 `cont-quote-land`** (REBUILD = graft the branch
> + re-verify against the now-real L1–L5 deps + resolve the contest, then merge). The `{approved | rework}`
> call is re-made AT L7-time by the architect (reads this dossier, brings a recommendation) + founder.

## Surviving git locators (the archive does NOT delete these branches)

- **Branch:** `stage/service-orders.cont-quote-core` (workspace repo `/home/artur/projects/erp-workspace`;
  the L7 graft source — 37 files, 1 commit behind `main`, merges 0-conflict per merge-tree).
- **Dossier** (`/artifact/1200`, kind `contest_rationale`): `_factory/stages/service-orders.cont-quote-core/findings-response.json`
  @ commit `5419b5a73af64632853471ad6ebba0928ba2a560` · sha256 `0c19a3b5d7f63e36e91808f9cf2b4f5392a565ec8a489d6441e68058dbb4dda0` (verified on export).
- **Decision request** (`/artifact/1204`): `_factory/stages/service-orders.cont-quote-core/decision-request-escalation.md`
  @ commit `ee5e9f1e1f45f40de1fc7693db7969ac0028fb38` · sha256 `ad50fcb437a7ea4032092d0c6988baeb092ff440f67b781104096a5f7db52e57` (verified on export).

Re-read the dossier byte-exactly with:
`git -C /home/artur/projects/erp-workspace show 5419b5a7:_factory/stages/service-orders.cont-quote-core/findings-response.json`

## The dossier — three findings, the builder's adjudicated responses

The stage halted **AWAITING_HUMAN** with the contest pending, so the two `comply` fixes below were
**not necessarily applied on the branch** — the L7 re-verify must re-examine **all three** against the
now-real L1–L5 deps and record the disposition per `architect-operations.md §1` BEFORE merge.

### 1. `CQ-AUD-004` — action: **comply** (un-enveloped raw 500s on malformed input)
Reachable client inputs bypass the F1 `{error:{code,message,details}}` envelope (spec §11 inherits it):
(1) non-numeric `contract_id` → `Contract.objects.get` int-coercion `ValueError`, not caught (api.py:245-255);
(2) `{'lines':[null]}` → `_normalize_line` `raw.get()` `AttributeError` BEFORE the try (api.py:234,113-114,126);
(3) inside `apply()`, `qty='not-a-decimal'` → `Decimal` `InvalidOperation` and `promised_term='not-a-date'`
→ `date.fromisoformat` `ValueError` (documents.py:48-58), neither caught; (4) non-list `supplier_options`
persisted unchanged, violating the spec's typed `list` contract (spec §125-145). **Builder's planned fix
(BUILD, no contract change):** one payload-shape/type validator used by BOTH create + edit before the
service call, mapped to the existing envelope (reuse `validation`/`invalid_field_value` codes).

### 2. `ASM-005` — action: **comply** (spec §7 helper shadowed by an inline re-implementation)
`accepted_conts_by_payment_state(state)` (payment_state.py:66) is spec'd (§7) as backing the list API (§11)
but is referenced only by its test; `QuotesCollectionView.get` re-implements the partition inline
(api.py:262-266) and the inline path ships. No behavioural gap (both delegate to the single `payment_state()`
source), severity `info` — but a parallel-copy / index→source drift (Doctrine §9). **Builder's planned fix:**
make one path canonical — either `get()` calls the named helper (restoring §7's stated wiring) OR delete the
unused-in-prod helper + its test. Cheap, behaviour-preserving, no contract change.

### 3. `ASM-006` — action: **CONTEST** ← *this is contest #104, the unresolved one*
**Finding (factual):** `QuotesCollectionView.get` materialises all conts via `list(qs)` with no pagination
(api.py:264) and `_summary` calls `payment_state` per row (api.py:207), each issuing two `registers.balance`
queries (payment_state.py:54-63) → **O(2n) register queries on an unfiltered list (N+1)**.
**Builder's contest grounds (and the auditor concurred it is "not a current conformance defect"):**
1. **No spec/contract imposes any list-performance/pagination budget** — there is no conformance obligation.
2. **Pagination is the consumer's contract** — the cont-quote-editor list UI (now L7 `quote-editor-lines` /
   `quote-editor-flow`) owns the `Page<T>` shape; binding it now, before the consumer exists, is premature
   binding (Doctrine §12). Derive the list contract once, WITH the UI stage.
3. **The N+1 cannot be cheaply removed in scope** — `payment_state` reads R6 per cont through the *frozen*
   `registers.balance` single-cont seam; a batched balance read does not exist on that frozen seam, so
   removing the N+1 needs a NEW foundation seam — out of scope and itself premature.
4. The accepted-cont set is small → the latent characteristic carries no current risk (Doctrine §13: local +
   bounded, not a risk that must stop us).
**Builder's recommendation:** carry the observation forward to the editor-UI stage where pagination lands
with the list shape — i.e. resolve it WITH **`quote-editor-lines` / `quote-editor-flow`**, not in the backend.

## What the L7 `cont-quote-land` spec/architect must do (per its acceptance + architect-operations §1)

1. **Feed this doc** as a documented INPUT to the `cont-quote-land` spec.
2. **Re-verify** the grafted `/api/quotes/` API + discount Σ≡total + lifecycle + 62-day auto-refuse + R6
   payment-state + relink guard against the **now-real** L1–L5 deps (the deps were skeletons when the contest
   was raised).
3. **Classify contest #104 / ASM-006** per `architect-operations.md §1`: comply → fix in the graft ·
   accurate-but-no-action → **`settled`** (D-0062 accept-path: accurate, harmless, permanently closed) ·
   spec-lie → correct the spec (`rework:SPEC_DOC` if documentary). On the dossier's own evidence + the
   auditor's concurrence, ASM-006 reads as the **accurate-but-no-action / `settled`** class (a bounded latent
   N+1 with no spec/contract obligation), with the pagination decision deferred to the editor-UI stages —
   **but the architect + founder make the final call at L7-time after re-reading the dossier.**
4. **Re-examine** `CQ-AUD-004` + `ASM-005` (the two `comply` items) — apply or confirm-already-applied.
5. **Record the disposition** before merge.
