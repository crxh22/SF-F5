# C1 — Registers and Posting (cross-phase contract)

**Status: DRAFT v0.1 — frozen (v1) at foundation exit; thereafter versioned only by Main Architect.**
**Normative sources (ERP-start, repo-root-relative):** `docs/Business/blueprint/register-model.md`, `docs/Business/blueprint/document-effect-matrix.md` (ADR-006 P0: on divergence the matrix + producer lists win), `docs/Business/adr/0003-movement-register-model.md`, `docs/Business/adr/0006-posting-engine-rules.md`. Pin: ERP-start@51e32b0.

## Scope frozen by this contract

1. **Movement-register schemas** — one table per register family R1–R18 with EXACTLY the dimensions register-model.md declares (e.g. R2 stock: item, depozit, own_pj, quantity, cost_basis, vat_attribute, zn_line_reference, source_document, movement_type; R6: the 5 settlement types; R9A/B granularity = one row per source document). Foundation concretizes column types/indexes; dimension SETS are this contract.
2. **Posting API** (the only write path to registers): `post(document_event) → movements`, edit = reverse-old + post-new in one tx (ADR-006 P5), idempotency key `(document_id, document_version, register_type, line_sequence)` (P4), every movement carries `managerial_date` + nullable `official_date` (ADR-004), document reference mandatory + immutable (P1).
3. **Global TVA precondition:** R9A/R9B movements only `pe alb`; `la negru` = zero TVA rows — enforced in the engine, not per producer.
4. **Stock semantics:** lot model (separate cost layers per VAT attribute), statuses `on_hand → in_producere → consumed` (+ `direct_stock_release` shortcut), reservation register at ZN-line granularity (auto-on-receipt / manual / reassign / remove as recorded movements), picking order (reserved → FIFO same-VAT → fail-explicit), `incomplete_delivery_negative` as the ONLY permitted negative.
5. **Effect-matrix conformance rule:** every Y/P cell of the matrix is a test obligation of the phase that owns the producing document; a producer posting outside its row = contract violation (Tier-2 gate input).
6. **Period snapshots:** the STORAGE SHAPE (snapshot keying per register + balance-from-snapshot query form, ADR-003 amendment) is frozen here; the snapshot tables themselves and the close/reopen workflow are reporting-phase scope — nothing earlier may assume their existence.

## Consumers

All phases post ONLY through the posting API. No phase writes register tables directly; no phase adds movement types outside register-model.md without a contract version bump.
