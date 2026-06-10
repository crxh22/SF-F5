# 01 — FRAMEWORK MVP — Definition of Done & Kickoff Spec

**Status:** kickoff artifact, v3 — aligned to the final DOCTRINE numbering (root cause = §11; last responsible moment = §12). The hybrid control-plane/consultation design is a locked decision of this document, not a doctrine anchor. Supersedes all prior versions; no section inherits authority from them.
**Governed by:** `00 — DOCTRINE`. Where this document and the doctrine conflict, the doctrine wins.
**Language note:** English is the canonical version (instruction-text, Doctrine §5 exception). A Romanian translation exists as a review aid only — non-canonical (Doctrine §9).

---

## 1. Purpose

Build the minimal working version of the software-production pipeline ("the factory") that can take real ERP work from spec to validated, merged code with:
- a deterministic control plane with enumerated LLM consultation points,
- persistent memory at macro / phase / stage levels (git + SQLite),
- parallel execution with a two-tier conflict model (textual + semantic), recursive across levels,
- mechanical escalation,
- human intervention only at explicitly listed gates.

Validation discipline: **value criteria** are demonstrated on real Elita-9 ERP stages; **mechanism-correctness criteria** (detectors, gates, fallbacks) are demonstrated on designed synthetic scenarios with seeded faults — a detector cannot be validated by waiting for natural faults (Doctrine §10). Mechanisms beyond this document are added only after incidents demand them (Doctrine §8).

---

## 2. Locked decisions

1. **Control plane is deterministic code (Python), never an LLM.** The control plane owns: the state machine, DAG scheduling, process lifecycle, persistence, threshold evaluation, merge mechanics. At **enumerated decision points** it MAY consult an LLM as a pure function: bounded input (artifacts/diffs), structured output validated against a schema with a closed verdict set, every call logged, deterministic fallback on invalid or ambiguous output. The LLM never holds the control loop, never spawns or kills processes, never writes state directly. The registry of consultation points lives in config (§14); any LLM call from the orchestrator outside the registry is a governance breach, detectable mechanically from logs.
2. Agents communicate only through the orchestrator; never agent-to-agent directly. Native subagents spawned *inside* a single pipeline agent (e.g. Builder using research/exploration subagents within its stage) are an internal implementation detail — permitted, bounded by that stage's worktree, context, and budget; they are not pipeline agents and never cross stage boundaries.
3. Agent autonomy is bounded by written artifacts. Agents read and write **artifacts only**; they never touch the operational database.
4. Final validation by a non-executor agent, clean context (Doctrine §4); different model family where the risk class requires it.
5. Builder and cross-model Auditor use different model families (cognitive diversity).
6. Agents run as separate `claude -p` / equivalent processes with NDJSON streaming; PTY interactive mode reserved for heavy architectural stages (cost-routing decision stands).
7. Human = PO + arbiter; agents = translation + execution (Doctrine §15).
8. **Persistence split:** git is canonical for artifacts; SQLite is canonical for operational state and is owned exclusively by the orchestrator. No content is duplicated across the boundary (Doctrine §9): the DB references artifacts by path + hash, never stores their content.

---

## 3. Pipeline model

### 3.1 Levels and memory

| Level | Owner agent | Canonical artifacts (git) | Operational state (SQLite) |
|---|---|---|---|
| Macro (project) | Main Architect | `PROJECT.md`, macro decision log (append-only), **cross-phase contracts** | phase DAG, phase statuses |
| Phase | Phase Architect | phase plan: stages + acceptance criteria, **intra-phase contracts** | stage DAG, stage statuses, counters |
| Stage | Stage conveyor | stage folder: SPEC, build notes, validation report, audit findings | iteration counters, churn metrics, events |

All durable knowledge lives on disk (git), never only in an agent's context. Status views (founder dashboard, `STATUS.md`) are **generated** from DB + git — indexes pointing to sources, never canonical themselves (Doctrine §9).

### 3.2 Parallelism is recursive

The same fan-out pattern applies at both levels, executed by the same level-agnostic code path:

- **Stage level:** Phase Architect freezes intra-phase contracts → stages fan out into worktrees → merge gates (§5).
- **Phase level:** Main Architect freezes cross-phase contracts → phases fan out → same merge gates at phase integration.

ERP concretization: a **Foundation phase** (core schema: contragenți, nomenclature, document primitives, auth/access control) freezes the shared core; then inventory/procurement ∥ project/order management run in parallel; managerial accounting consumes frozen event/data contracts produced by both.

MVP scoping (explicit): the mechanism is demonstrated in **execution** at stage level and in **planning** at phase level (cross-phase contracts + a phase DAG marking ≥2 phases parallelizable after Foundation). The first parallel *execution* of phases is the first production use, on the same code path — not an MVP demonstration criterion.

### 3.3 Roles

**Intake Agent** — interviews the founder → business documentation. MVP: interactive session, not automated.

**Main Architect** — macro architecture, roadmap, phase split, cross-phase contracts, macro decision log. Heavy-context, PTY/interactive, strongest model.

**Phase Architect** — decomposes a phase into stages sized at the upper bound of one-pass confidence for the current builder model — split on evidence, not preemptively; stage size is a capability-dependent value, recalibrated per model generation (§14). Declares per-stage acceptance criteria and risk class in the phase plan; freezes intra-phase contracts before any fan-out; receives escalations; decides rework targets.

**Stage conveyor — 3 roles:**
1. **Spec Agent** — one artifact per stage, with depth scaled by risk class: `routine` → light spec (acceptance criteria + test list); `structural`/`critical` → full DoD + HLD + SPEC + test design. Test-first at every depth.
2. **Builder** — implements against SPEC; verifies its own work before handoff (Doctrine §4 — skipping self-verification is false economy). Does not see Validator's test internals.
3. **Validator** — clean context; derives tests independently from SPEC; runs them; produces the validation report. Different model family from Builder where the risk class requires it (§7).

**Integration Validator** — clean-context agent (cross-model preferred) invoked at merge gates (§5.2).

**Auditors (risk-routed, §7)** — same-model and/or cross-model; findings return to the stage executor, who may comply or contest with rationale; unresolved contests escalate to Phase Architect. All contests are logged.

### 3.4 Stage flow

```
SPEC → BUILD → VALIDATE → [AUDIT if risk class requires] → MERGE GATE → DONE
            ↑__________feedback loops__________|
```

Feedback-loop routing is decided in two layers:
1. **Deterministic thresholds first** (§8). When a threshold decides, no judgment is involved.
2. **LLM triage** — consultation point CP-1 — only when thresholds do not decide. Input: validation report + diff digest + SPEC. Output: one verdict from the closed set `{continue_session, rebuild, respec, escalate}` + cited rationale. Invalid or ambiguous output → deterministic fallback = `escalate`. Every call logged (input digest, verdict, model, latency, cost).

Every transition writes its artifact before the next step starts. Agents are constrained to fail explicitly rather than guess (Doctrine §7).

---

## 4. Orchestrator: control plane and consultation points

**Control plane (deterministic, exclusive):** state machine and transitions; DAG scheduling and queueing; process spawn/kill/timeout; SQLite persistence (sole writer); threshold evaluation; git/worktree/merge mechanics; budget enforcement; logging.

**Consultation point contract (each registered point defines):** `id`, purpose, bounded inputs, output JSON schema with closed verdict set, model routing, deterministic fallback, log destination. Adding a point = config change + macro decision-log entry.

**MVP registry:** a single point — **CP-1** (*consultation point #1*: feedback-loop triage, §3.4). No others. Candidate future points (not registered): merge-order hints, escalation-payload summarization.

---

## 5. Parallelism and the two-tier conflict model

### 5.1 Tier 1 — textual conflicts (mechanical, no AI)

Each parallel unit runs in its own git worktree/branch. Merge gate: rebase onto the integration branch + full test suite. Mechanical failure (rebase conflict, test failure) → orchestrator routes the conflict payload back to the owning unit. No agent judgment at this tier.

### 5.2 Tier 2 — semantic conflicts (logic divergence between parallel units)

Three mechanisms, in order:

**Prevent — contract-first fan-out.** Before parallelizing, the owning architect (Phase Architect at stage level; Main Architect at phase level) extracts the shared surface into versioned **contract artifacts**: data schemas, API signatures, named invariants. Parallel units may read contracts; none may modify them. A needed contract change = STOP on that unit + escalation to the owning architect, who versions the contract and re-syncs affected siblings (Doctrine §3).

**Detect — semantic merge gate.** After Tier 1 passes, the **Integration Validator** (clean context, cross-model preferred) receives: contracts in force, diffs of all merging units, the phase plan. It checks: contract conformance in substance (not just signature), cross-boundary invariant violations, duplicate/divergent implementations of the same concept (Doctrine §9), assumptions in one unit contradicted by another. Findings cite concrete locations (Doctrine §5).

**Resolve.** Findings return to unit executors — comply or contest with rationale. Unresolved → owning architect decides: which unit reworks, whether a contract version changes, or return to an earlier stage with supplementary specs. Decision + rationale enter the phase plan / decision log.

### 5.3 Mechanism validation — seeded conflicts (mandatory before trust)

The semantic gate is trusted only after passing a **designed synthetic scenario**: two stages constructed so their diffs merge cleanly and all tests pass (Tier 1 green) while jointly violating a named shared invariant. The Integration Validator must catch it and the resolution loop must complete. If it misses, the gate is redesigned before any real parallel work relies on it (Doctrine §10). The seeded scenario is kept in the repo as a permanent regression fixture for the factory itself.

---

## 6. Persistence

| Concern | Canonical store | Notes |
|---|---|---|
| Specs, contracts, reports, decision logs, role prompts, config | **git** | human-reviewable, diffable, audit trail (Doctrine §5) |
| Stage/phase status, DAG + queue, iteration & churn counters, escalation events, CP consultation log, audit-finding lifecycle, token/cost ledger, process registry, event stream | **SQLite** | WAL mode; every state transition is one transaction |

Rules:
- DB stores **references** (path + hash) to artifacts, never content (Doctrine §9).
- Orchestrator is the sole DB writer/reader; agents see only files; status needed by an agent is rendered into its context by the orchestrator.
- `STATUS.md` / founder dashboard = generated views.
- DB schema migrations are versioned scripts in git.
- The cost ledger feeds the existing routing economics (PTY vs `-p`, model per role).

---

## 7. Audit and validation routing by risk class

Risk class declared per stage by the Phase Architect in the phase plan. Defaults (configurable, §11):

| Risk class | Examples | Validation routing |
|---|---|---|
| `routine` | UI tweaks, isolated CRUD, refactors with full test cover | Validator only (any model) |
| `structural` | data model, shared contracts, cross-module logic | Validator + parallel dual audit (same-model + cross-model) |
| `critical` | money/tax flows, access control, external contracts (e-Factura), irreversible migrations | Validator + dual audit (same-model + cross-model) + human gate (§9) |

Executor's right to contest findings holds at every class; contests are logged.

**Audit-model principle:** the cross-model auditor is a complement for bias diversity, never a substitute for capability. Both auditors run in parallel (no wall-clock cost); the executor triages the **union** of findings, deduplicating overlaps. Dual audit at `structural` is justified by current subscription economics (the second auditor's marginal cost ≈ 0) — an economic parameter in config, dialed back if economics change. The table deliberately says *same-model / cross-model*, not vendor names (§14): the design does not depend on settling which family is currently stronger.

---

## 8. Escalation — mechanical triggers

Triggers fire from orchestrator-measured signals, never from agent self-assessment or anyone's attentiveness (Doctrine §20). Trigger values are config parameters (defaults in brackets):

- `max_fix_iterations` [3]: BUILD→VALIDATE loops without the failing-test count decreasing → escalate.
- `churn_threshold` [4 edits to the same file region within one stage]: patch-over-patch detector → force rebuild or escalate (Doctrine §11).
- `contract_change_request` [always]: immediate STOP + escalate to owning architect.
- `agent_declared_failure` [always]: explicit "I don't know / cannot proceed" routes up, never retried blindly (Doctrine §7).
- `context_budget` [per-stage token cap]: exceeding it forces a state-preserving reset — write everything to artifacts, fresh context resumes from disk.

Per Doctrine §20, "mechanical" qualifies the trigger; the *triage* of an ambiguous trigger may be CP-1, whose verdict the control plane validates and executes. Escalation target: Phase Architect first; macro-level causes go to Main Architect. The escalation payload is the artifacts, not a narrative summary.

---

## 9. Human gates — explicit list

The pipeline pauses and surfaces to the founder, in one place (Doctrine §18), only for:

1. Business/product decisions: features, priorities, scope cuts.
2. Choices with heavy irreversible impact: data-schema changes on live data, public/external API contracts, new major dependencies, anything touching money, taxes, or fiscal reporting (e-Factura).
3. Phase completion sign-off; re-prioritization of the phase DAG.
4. Unresolved escalations where agents propose options but the choice is a product trade-off.

**Surfacing mechanism (MVP):** no agent converses with the founder directly — the exceptions are Intake and orchestrator-spawned **Decision Sessions**, both bounded sessions mediated by the orchestrator and producing artifacts. When a gate fires, the orchestrator sets the unit to `awaiting_human`; the responsible architect (Phase or Main) prepares a **decision request artifact**: the question, links to the relevant artifacts, options with trade-offs, a recommendation. The orchestrator publishes it through the founder channel and blocks **only the dependent subtree** — independent work continues.

**Founder channel — ntfy push + one dashboard, never terminal mechanics.**
- **ntfy push** — fired only for situations requiring the founder's attention: decision requests, risk alerts, factory-down alerts from the watchdog. Content is minimal by design: title + deep link into the dashboard. Hosting is a config choice: self-hosted ntfy behind Tailscale, or ntfy.sh with a secret topic.
- **Watchdog — silent-death detection (Doctrine §20), push-on-failure only.** A minimal external check on the OS scheduler (systemd timer / cron — the root of trust, so the watchdog itself needs no monitor) periodically verifies orchestrator liveness: process alive + liveness timestamp fresh (the orchestrator refreshes it every loop). On failure → high-priority ntfy push. It watches **only the orchestrator**; everything below is the orchestrator's own job (process timeouts and §8 triggers cover stuck agents). No "all good" notifications — silence means healthy; the dashboard health strip shows the last liveness timestamp for on-demand confirmation. Whole-server death is out of MVP scope (visible as an unreachable dashboard; an external uptime ping is added only if incidents demand it, Doctrine §8). Check interval and staleness threshold in config.
- **Dashboard** — the single interaction surface (Tailscale-only; link saved on phone/laptop). The implementation stack is decided in the implementation session after the environment audit (§16); what binds now are the constraints: served from the factory server, minimal operational footprint — no new infrastructure class, no build pipeline required — a single page. Three sections over one data source:
  1. **Decisions awaited** — one card per pending decision; one click expands the full decision request artifact, rendered maximally clear: question, options with trade-offs, recommendation, links to artifacts. An inline input opens the **Decision Session** with the responsible architect right in the card — UX-direct, architecturally still orchestrator-mediated (the dashboard backend is the orchestrator's surface). Answering = tapping an option button, or conversing and then explicitly confirming an option; the transcript becomes an artifact; the validated answer goes to the decision log (git) and unblocks the dependent subtree.
  2. **Running now** — the health strip: phase progress, stage queue, budget burn, last incident, last orchestrator liveness.
  3. **Roadmap & backlog** — phases with progress and details; per phase, stages grouped done / in progress (with the pipeline step reached) / planned. A generated view over the phase plans (git) + statuses (DB), non-canonical (Doctrine §9).

CLI / decision-file editing remains as plumbing and emergency fallback, never the expected founder path.

**Question propagation:** a question is absorbed at the lowest level able to answer it technically, and travels up only insofar as it is a product question: Builder → Phase Architect → Main Architect → founder. Local + reversible ambiguity never stops work (Doctrine §13) — it is decided and logged as an assumption. A Builder-originated question that ends up requiring the founder is logged as an incident: a SPEC/contract defect signal (repeats produce a rule, Doctrine §8).

Everything else runs autonomously. The founder's single view: decisions awaited, what is running, where risk appeared, what was delivered, plan open to re-prioritization.

---

## 10. Artifact standards

- One clear responsibility, explicit boundaries per artifact (Doctrine §0).
- Facts cite sources; assumptions marked as such (Doctrine §5) — instruction-texts exempt per §5 exception.
- Artifacts are re-derived whole when accumulated changes have bent their shape (Doctrine §6), as this document was.
- Canonical content in exactly one place; everything else points to it (Doctrine §9).
- Change to a canonical artifact → dependents addressed or `deferred` + rationale (Doctrine §19).

---

## 11. Configuration

All numeric thresholds, model-routing tables, risk-class defaults, budgets, paths, and the **consultation-point registry** live in `factory.config.yaml` — never hardcoded in role prompts or conceptual docs (Doctrine §14). Role prompts reference config keys by name.

---

## 12. Definition of Done — MVP acceptance criteria

### A. Value criteria — on real ERP stages only

1. **End-to-end single stage:** one real ERP stage flows SPEC→BUILD→VALIDATE→DONE fully orchestrated, all artifacts on disk, zero manual file shuffling by the founder.
2. **Restart integrity:** kill the orchestrator mid-phase — the watchdog detects it and fires an ntfy push within its check interval; restart resumes from SQLite + git with no information loss; an integrity check confirms every DB artifact reference resolves and hashes match.
3. **Cross-model audit round-trip:** a `structural` stage receives cross-model findings; the Builder contests at least one; the contest is logged and resolved per §5.2/§7.
4. **Human gate:** a `critical` stage pauses, fires an ntfy push, and resumes on a decision answered from the dashboard on the phone — not from a terminal.
5. **Failure honesty:** at least one case where an agent reports explicit inability instead of guessing, routed correctly (Doctrine §7).
6. **Real parallel merge:** at least one pair of real ERP stages runs in parallel worktrees against frozen contracts and merges through Tier 1 + Tier 2 gates.

### B. Mechanism-correctness criteria — on seeded synthetic scenarios

7. **Escalation fires:** a seeded persistently-failing stage triggers `max_fix_iterations` escalation without human prompting.
8. **Semantic gate catches:** the seeded-conflict scenario of §5.3 (Tier 1 green, shared invariant broken) is caught by the Integration Validator and the resolution loop completes. Hard gate: failure here blocks criterion A6.
9. **Consultation contract:** CP-1 returns a schema-valid verdict on a real ambiguous case, AND the deterministic fallback engages on an injected invalid output.

### C. Planning criterion

10. **Phase-level parallel plan:** cross-phase contracts exist and the phase DAG marks ≥2 phases parallelizable after Foundation (execution of phase-level fan-out = first production use, same code path).

---

## 13. Falsifiability (Doctrine §10)

We will know the design is wrong if:

- **Conveyor compression wrong:** >30% of `routine` stages need respec after BUILD starts, within the first 10 stages → reintroduce a separate HLD step.
- **Contract-first too rigid:** contract change requests fire on >50% of fan-outs → contracts frozen too early or too broadly; freeze later or narrower.
- **Semantic gate adds no value:** zero actionable findings across the first 5 real parallel merges → demote Integration Validator to `structural`/`critical` merges only.
- **Escalation thresholds miscalibrated:** >2 silent patch-spirals not caught → lower `churn_threshold`; >2 false-positive escalations on healthy stages → raise it.
- **Factory overhead exceeds value:** orchestration+audit tokens >2× builder tokens on `routine` stages over a sample of 10 → simplify routing for `routine`.
- **Consultation creep:** any orchestrator LLM call outside the registry (mechanical log scan) → governance breach; stop and re-derive §4.
- **Consultation quality:** >30% of CP-1 verdicts overturned by the Phase Architect in the first 20 → revert CP-1 to deterministic `escalate`-always.
- **Dual audit at `structural` not paying:** over the first 10 `structural` audits, whichever auditor contributes ~zero unique findings (not duplicated by the other) is dropped from this class; if dual-finding triage noise costs more executor iterations than it saves, demote `structural` to single audit.
- **Founder channel failing:** the founder resorts to SSH/terminal to answer decisions more than rarely (>10% of decisions), or decision requests sit unanswered past [config `decision_latency_alert`, default 24h] because the channel is uncomfortable or unnoticed → redesign the channel before adding any pipeline feature.
- **Persistence split wrong:** >2 incidents of broken artifact references or content duplicated into the DB → revisit §6.
- **Recursion assumption false:** phase-level fan-out turns out to need mechanisms beyond the stage-level ones → the "level-agnostic" claim is falsified; redesign before scaling.

Review after the first completed phase; log outcomes in the macro decision log.

---

## 14. Out of scope for MVP

- Automated Intake Agent (founder interview stays interactive).
- Cross-cutting agents (Context Curator, Drift Sentinel, Retrospective Analyst, Clarification Broker) — added only on incident evidence (Doctrine §8).
- Parallel **execution** of phases (planning demonstrated per criterion C10; execution = first production use).
- Continuous per-stage contract-conformance checking (added if late detection proves costly — Doctrine §8).
- Any founder UI beyond the §9 channel: ntfy push + one dashboard. Deliberately NOT built: chat-platform bots (Telegram/WhatsApp), multi-user auth (Tailscale is the boundary), notification-preference UI (config file), websockets/real-time dashboard push (refresh/poll is enough), any session UI richer than the inline decision-card input.

---

## 15. Open parameters awaiting founder decision (Doctrine §12)

1. Initial model routing per role in `factory.config.yaml` (proposal: strongest model for Architects/Spec, cost-efficient model for Builder on `routine`, cross-model auditor = different family; CP-1 on a fast cheap model — its output is schema-validated anyway).
2. Token/cost budget caps per stage class.
3. Proving-ground phase (proposal: Foundation first by necessity, then inventory/procurement as the first full phase).
4. Whether any consultation point beyond CP-1 is wanted in MVP (proposal: none).
5. ntfy hosting (self-hosted behind Tailscale vs ntfy.sh with a secret topic); watchdog check interval and staleness threshold.

---

## 16. Implementation kickoff notes (for the executing session)

1. **First action before planning: environment audit.** Inventory what the session actually has — runtimes and versions, CLIs (git, agent CLIs, sqlite3, systemd/cron), network reach, credentials and subscription access, ntfy reachability, Tailscale state. Install/configure the missing essentials quickly, then move to implementation. No implementation step starts against an unverified environment.
2. **Build priorities for the factory project itself:** speed is the top priority; resources are deliberately unconstrained — maximum possible investment; quality scales with impact on the quality of the product the factory delivers: good-enough for internal conveniences and dashboard cosmetics, maximum for control-plane correctness, persistence integrity, merge gates, and validation paths.
3. **Technology choices not fixed by this document** (dashboard stack, libraries, supervision details) belong to the implementation session — decided after the environment audit, recorded in the decision log (Doctrine §12).
