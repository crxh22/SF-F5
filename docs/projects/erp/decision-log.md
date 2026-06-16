# ERP Macro Decision Log

Append-only (DoD §3.1, macro level — project ERP). Newest entry last. Format: `D-ERP-NNNN — date — owner — decision`. Factory-level decisions stay in `docs/decision-log.md`; this log holds ERP-the-product decisions made through the pipeline (intake ratifications, phase sign-offs, contract version bumps, human-gate outcomes of `business`/`phase_signoff` kind).

---

## D-ERP-0001 — 2026-06-12 — founder — Intake interview: macro plan ratified; ERP production start approved

Interactive intake interview (12-06-2026, founder + Main Architect — the DoD §3.3 intake act). Founder ratified the intake package (drafted + adversarially reviewed under D-0024) with the following decisions:

1. **Phase map = variant A**: 7 phases — foundation → {inventory-procurement ∥ service-orders ∥ treasury-payments} → payroll → reporting → migration-1c; 3 phases parallelizable after foundation (DoD §12.C10 planning artifact). `docs/projects/erp/macro-plan.json` is the machine form; PROJECT.md v0.1→**v1.0 RATIFIED**.
2. **Foundation scope as drafted**, including the post-review additions (cont/ZN/ZN-line skeleton entities, print/PDF framework, media/attachment subsystem, notification framework) and the intake UI/UX additions: design system + ADR-0002 abstraction layer + per-user view-preferences mechanism (concrete preference set deferred by founder — architecture must keep adding it cheap).
3. **`projects.erp.test_command` = `bash scripts/test.sh`** (OPEN-2 closed; self-retiring exit-5 shim + pre-skeleton pyproject guard in the script body).
4. **M1/M2 placement confirmed**: Method-1 documents in treasury-payments (confidential scope), `revanzare_tva` cont tip in service-orders, contract-level monitoring in reporting; M1 ≠ M2 access scopes (re-confirms 2026-06-10).
5. **Fan-out timing**: full plan seeded up front (C10 in the DB DAG); `proving_phases: [foundation, inventory-procurement]` dispatch hold confirmed — first 3-way parallel execution only after inventory-procurement completes.
6. **One-time PLANNING checkpoint approved**: operator review of foundation's phase plan between PLANNING and execution (stop → review → resume).
7. **Start approved**: bootstrap workspace → seed → run, immediately after the interview.

**UI/UX requirements** (founder statements, recorded in ERP-start `docs/Business/domain/technical-context.md` §UI/UX Requirements, commit `c225fbc`): modular+configurable UI; one shared design system; per-user view preferences (set deferred, mechanism from the start); UX-first process — problem → flow → scenario simulation BEFORE UI design — binding for every UI-bearing stage's acceptance criteria.

**Iteration-1 posture** (founder framing, architect concurrence with nuance): iteration 1 also serves as the framework's proving run; expected to surface framework improvements/bugs. NO pre-commitment to discard iteration-1 ERP code — the pipeline's quality mechanisms target production-grade output from the start. Registered OPEN option: "restart ERP code generation on the stabilized framework", owner = founder, deciding trigger = **foundation sign-off retrospective** (the DoD §13 review after the first completed phase), judged on evidence (framework defects that REACHED code vs were caught). Mitigating fact: docs, plans, contracts, and lessons survive any restart — only generated code would be redone, on validated plans.

**Effort routing + capacity policy**: per factory log D-0025 (CCR-6) — per-role effort in config (xhigh/high), capacity events page the founder, pause-and-wait is the default posture, model downshift only by founder decision preferably at phase boundaries.

---

## D-ERP-0002 — 2026-06-16 — founder + Main Architect (ETAPA-5i) — foundation phase signed off (approved); C1–C4 frozen v1; E6 15A refinement settled

**Foundation phase sign-off — APPROVED (founder, 2026-06-16T01:04Z, dashboard).** The `phase_signoff` human gate [8]; all 14 stages DONE, phase Tier-1 (full combined suite green on the integration branch) + Tier-2 (cross-unit integration_validator findings `[]`) passed. The Main Architect (ETAPA-5i) reviewed the as-built report before recommending approval (not just the mechanical "gates passed"): the C1–C4↔code map is honest and complete, divergences benign, deferrals by-design. Foundation → **DONE**.

**C1–C4 cross-phase contracts FROZEN v1** (workspace commit `5cf7e2a`, by the Main Architect at sign-off, from the as-built state `_factory/phases/foundation/as-built-report.md` per PROJECT.md §3). Status `DRAFT v0.1 → FROZEN v1` on all four. **No clause-text changes beyond the status line + the E6 refinement**: the as-built found no contract clause that contradicts the code (the 6 divergences are source-doc / phase-plan / by-design-data — e.g. `cont_curent` desk type, composed treasury seed names, unified `Attachment`, R11 audit-index in `documents`, superuser-default rights backends — all recorded in the as-built report, the §9 divergence index; C1 does not assert R11 is a movement register, C3 asserts no cash-desk count, so nothing lies about the code → architect-operations §1 no-amend).

**E6 15A refinement — SETTLED at freeze (the one reserved Main-Architect decision):** R15A (Supplier Fiscal Invoice Register / `fiscal.SupplierFiscalInvoice`)'s linked-document set is the **GENERIC `documents.Document` M2M** (`linked_documents`, as-built `apps/fiscal/models.py:44`) — it already extends to treasury's M1 `service_acquisition`, which links by the IDENTICAL call as inventory's `document_de_achizitie`. Foundation built the linkage generic deliberately, anticipating this. **Outcome: 15A's set is FINAL as-built; no extension work, no producer-build contract change.** Producers are domain-phase: inventory builds the stock-purchase linkage, treasury the M1 service-acquisition linkage, both reusing the same R15A. C4 E6 invariant text updated from the pending "refine at freeze" instruction to the settled decision.

**Registered founder option now TRIGGERED (not yet decided):** D-ERP-0001's "restart ERP code generation on the stabilized framework" — deciding trigger = foundation sign-off retrospective (DoD §13). Architect's read on the evidence: the framework's defects this iteration (integration_validator 1M overflow D-0047, finding-regeneration loop D-0048) were CAUGHT and fixed BEFORE reaching ERP code, and the audit chain caught real ERP bugs (FN-CROSS-001 double-storno, UIF token-bypass) — i.e. the quality mechanisms worked, favoring KEEP over restart. Surfaced to the founder for the §13 retrospective; not urgent, conveyor unblocked.
