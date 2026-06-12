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
