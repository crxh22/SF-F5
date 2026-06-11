# C2 — Document Engine (cross-phase contract)

**Status: DRAFT v0.1 — frozen (v1) at foundation exit; thereafter versioned only by Main Architect.**
**Normative sources (ERP-start, repo-root-relative):** `docs/Business/adr/0005-document-graph-and-dependency-cascade.md`, `docs/Business/adr/0006-posting-engine-rules.md`, `docs/Business/domain/actors-and-access.md` (edit/correction policy, audit, versioning), `docs/Business/blueprint/status-layer-model.md`, `docs/Business/blueprint/document-catalog.md` cross-cutting properties. Pin: ERP-start@51e32b0.

## Scope frozen by this contract

1. **Base document lifecycle:** Draft (no register effects) → Final (all effects atomic, incl. dependent-document creation — effect-matrix item 15); storno = new version (revert-to-old or zero-effect cancellation) under ONE engine (ADR-006 unified semantics); deletion does not exist.
2. **Versioning + audit:** author/last-editor/full edit log, complete visualizable version history, per-document-type audit table + central index (R11) — mandatory on every document type any phase adds.
3. **Dependency graph + cascade:** document types DECLARE upstream/downstream edges with typed semantics (spawns/generates/fulfilled_by/linked_to/consumes/feeds/defaults_from — ADR-005); edit pre-check → refuse-with-blockers or atomic cascade in dependency order, single tx; block vs warn per ADR-005; the only upstream cascade is supplier-fiscal-invoice → VAT attribute → sinecost.
4. **Status-layer framework:** stable internal keys + configurable display labels; per-status editable transition-date fields with backward-transition clearing semantics (status-layer-model.md §Status Transition Date Fields); layer definitions per entity (cont 1-2, ZN 3-5, vehicle 6-7) are domain-phase content, the FRAMEWORK (storage + clearing + audit) is frozen here.
5. **Dual dating:** every financially relevant document carries `managerial_date` + optional `official_date` (ADR-004 P1); reports declare their axis.
6. **Fiscal-mode inheritance:** a cont has ONE fiscal mode from its linked contract; relink to another contract (alb↔negru) is permitted only while the cont has NO linked payment AND NO fiscal invoice issued; after either exists, channel-vs-mode mismatches raise conformity tickets ONLY (both directions, no automatic mode flips) — `docs/Business/domain/management-accounting-and-reporting.md` §Cont Fiscal Mode Lifecycle + `docs/Business/domain/counterparties.md` §Cont-Contract Relink Rule.
7. **Notification framework:** per-document-type notification rules (actors-and-access.md §Edit Rules targets) + personal-cabinet inbox (payroll recalculation notices, price-difference warnings to management) — domain phases declare rules, the delivery mechanism is frozen here.

## Consumers

Every document type in every phase is an instance of this engine. A phase needing engine behavior the contract lacks = `_CONTRACT_CHANGE_REQUEST.md` + stop, never a local workaround.
