# ERP Rebuild — Factory Re-Seed Playbook (execution mechanics)

**Status:** Execution plan for re-seeding the SF-F5 factory from the current domain-category phases into
the 10 dependency-ordered layers of [`erp-rebuild-plan-DRAFT.md`](./erp-rebuild-plan-DRAFT.md). Authored
by ARH-01 (22-06-2026) from an independent **read-only** mapping of the factory seeding code. **The
mechanism is verified; PENDING founder final approval of the plan before execution.** The per-layer seed
JSON is authored at execution time (it does not exist yet — see §5.1).

> This doc is the HOW-to-execute (factory mechanics). The WHAT (layers/stages) lives in the rebuild
> plan. One responsibility per artifact (Doctrine §0).

## 0. Critical preconditions (verified against the code)
- **The orchestrator is LIVE (pid 506016). `seed-phases` REFUSES to run while it's alive** (flock on the
  run pidfile, `cli.py:732-738`). → re-seed requires a CLEAN STOP of the factory.
- **Drain ≠ stop.** `drain.manual=true` holds new agent SPAWNS (`scheduler.py:5629`), the process keeps
  running. Keep drain ON across the whole operation; lift it deliberately, layer by layer, after seeding.
- **Single-project guard:** all 10 layers MUST be `project: erp` (`cli.py:746`). Do not coin a new project.
- **`proving_phases` is a SECOND, independent gate** (`factory.config.yaml`, restart-only): a PENDING
  phase whose id is NOT in the list is held from dispatch regardless of DAG. Currently
  `[foundation, inventory-procurement]`. Must be updated to the new layer ids (or emptied) + restart, or
  nothing dispatches even with drain off.

## 1. The seeding model
- **Phases** are seeded OFFLINE by `sf-factory seed-phases <macro-plan.json>` — ONE transaction: inserts
  phases (state PENDING), phase-level `dag_edges`, one `macro_plan` artifact ref, one `phase_seeded`
  event each. Idempotent on replay; zero writes on any precondition failure.
- **Stages are NOT CLI-seeded.** They materialize at RUNTIME when the orchestrator drives a phase
  PLANNING→CONTRACTS_FROZEN→RUNNING, ingesting a `phase-plan.json` the Phase-Architect writes
  (`scheduler.py:4202-4237`). There is no `seed-stages` command.
- Schemas: macro-plan `{project, phases:[{id,name}], dag_edges:[[from,to]]}` (`artifacts.py:258`);
  phase-plan `{stages:[{id,name,risk_class,acceptance}], dag_edges}` (`artifacts.py:178`), validated
  acyclic (Kahn toposort) before the RUNNING transition. Ids: `^[A-Za-z0-9][A-Za-z0-9._-]*$`, unique, no
  `..`/trailing `.` (ids become branch names + dirs).
- A stage = a DB row (inserted at ingest) + a `spec.md` (written by the Spec Agent during the stage's own
  SPEC step, committed to the stage branch in the **workspace** repo, registered as an `artifact_ref`).

## 2. Strategy decision (architect — lean A; finalize when approval is near)
- **Strategy A — archive & fresh DB (RECOMMENDED).** Snapshot+archive `factory.db`, `sf-factory init` a
  fresh DB, seed all 10 layers cleanly. Matches the design's fresh-DB-per-project posture; cleanest DAG.
  COST: loses live DB history (decision #26, escalations, events). Mitigation: §3 carry-forward.
- **Strategy B — additive into the live DB.** Keep foundation+inventory-procurement DONE; CANCEL the
  stale domain phases (raw SQL — no bulk-cancel verb exists); seed L0–L9 alongside. Preserves #26 +
  history but is more fragile (raw SQL, id-collision risk on CANCELLED ids).
- **Lean A** + the #26 carry-forward below. The A-vs-B choice is technical (architect's, not the
  founder's); the only founder-facing cost is a brief factory stop+restart (a few copy-paste commands).

## 3. Preservation checklist (MUST hold across the operation)
- **Branches (NEVER delete):** `stage/service-orders.cont-quote-core` (so_quotes backend, 58 files →
  REBUILD into L7), `stage/treasury-payments.treasury-app-foundations` (Payment base/conformity, 34
  files → RE-SLOT into L8), and all other `stage/*` + `phase/*` branches. The factory creates NEW
  `stage/<layer>.<id>` branches — the preserved code must be **cherry-picked/grafted** into them at the
  relevant layer, NOT rebuilt from zero (else the asset + the #103/#104 contest substance is silently
  lost + token-expensive re-build).
- **Merged work:** `phase/foundation` + `phase/inventory-procurement` are in workspace `main` (15 backend
  apps) — the L4/L5/L6 KEEP base. Do NOT reset main.
- **`runtime_settings`:** under Strategy A (fresh DB), re-insert VERBATIM before restart:
  `drain.manual=true`, `max_parallel_agents=2`, `budget.critical=500000000`, `budget.routine=80000000`.
- **DB backup** (`.factory/factory.db` + `-wal` + `-shm`) before ANY mutation.
- **decision #26 / cont-quote-core contest (#103/#104, dossier `/artifact/1200`):** EXPORT the dossier +
  contested finding to a carry-forward doc BEFORE archiving the DB, and feed it as a documented INPUT to
  the L7 cont-quote-core rebuild spec (so the re-verify addresses it). Under Strategy A, record #26 as
  "superseded by L7 rebuild" before archiving.

## 4. Where the UI/UX law gets injected (becomes factory law)
1. **`work-protocols/architect-operations.md`** — the canon home, injected via `--append-system-prompt`
   to `[main_architect, phase_architect, spec_agent]` (`factory.config.yaml:230,240`). Put the NORMATIVE
   law here (mandatory for every spec/phase-architect run, durable across sessions): the UI/UX
   questionnaire (a–i from `ui-ux-concept.md §2`), the back/front-separation HARD rule, the 5 UI-quality
   mechanisms, the "every operational FE stage exposes edit/cancel/history" checklist. **Highest leverage.**
2. **`scheduler.py _planning_prompt` (~5033-5071)** — what the Phase-Architect produces. Inject the
   back/front-separation rule so phase plans STRUCTURALLY yield a separate FE stage per UI-bearing
   capability (a backend stage + a separate frontend stage).
3. **`scheduler.py _spec_prompt` (~3656-3692) + `_acceptance_text`** — what the Spec Agent gets per stage.
   Add a pointer: "apply the UI/UX gate from architect-operations.md to every frontend stage." Content
   source for the 5 mechanisms: `docs/design/ui-ux-concept.md`.

## 5. Re-seed step sequence (who runs what)
1. **ARH-01:** author `docs/design/erp-rebuild-macro-plan.json` (10 layer ids + the L0→…→L9 edge chain) +
   10 `phase-plan.json` (the 36 stages, each `[BE]` capability = one backend stage + a SEPARATE `[FE]`
   stage, with `risk_class` + `acceptance`). **Commit (macro-plan MUST be committed & clean** — the seeder
   anchors the ref to factory HEAD; uncommitted ⇒ abort). *This is the bulk of execution; it does not
   exist yet.*
2. **ARH-01:** inject the UI/UX law (§4) into the canon + prompts. Commit.
3. **Founder (copy-paste):** disarm watchdog (`sudo systemctl disable --now sf-factory-watchdog.timer`)
   FIRST, THEN clean-stop (`tmux send-keys -t factory C-c`). Confirm 0 agents + pid gone. (Disarm first or
   the stop pages him — runbook `first-live-run.md:34-38`.)
4. **ARH-01:** backup DB; (Strategy A) archive + `sf-factory init` fresh; re-insert `runtime_settings`.
5. **ARH-01:** `sf-factory seed-phases <macro-plan> --dry-run` → verify the 10 phases + edges + anchor →
   then seed for real.
6. **ARH-01:** graft the preserved branches (cont-quote-core → L7 backend stage; treasury-foundations →
   L8 RE-SLOT stage) into the new stage branches the factory creates.
7. **ARH-01:** update `proving_phases` to the new layer ids (or empty) in `factory.config.yaml`.
8. **Founder (copy-paste):** restart factory (`sf-factory run` in tmux `factory`, replicate PATH incl. nvm
   node), verify dashboard bound + recovery complete + liveness fresh, re-arm watchdog. **Keep drain ON.**
9. **ARH-01:** lift drain layer-by-layer as each layer becomes ready + founder-gated.

## 6. Gotchas
- Can't seed while the orchestrator runs (clean-stop mandatory; disarm watchdog FIRST to avoid paging).
- Pre-placing `phase-plan.json` vs letting the PLANNING agent author it is an UNVERIFIED path — the
  PLANNING step normally *writes* the plan. If pre-placing, TEST on a throwaway DB first.
- Branch grafting is manual + error-prone — the highest-risk manual step.
- `proving_phases` is easy to forget (restart-only); nothing dispatches without it.
- Only the surrounding manual steps (DB archive, branch graft, any raw SQL under Strategy B) are
  non-transactional; `seed-phases` itself is single-transaction + idempotent-on-replay (safe).

**Key files:** `src/sf_factory/cli.py:708-837,476-715` (seed/preconditions); `artifacts.py:178-330`
(schemas); `scheduler.py:4202-4237` (stage ingest), `:5033-5071` (planning prompt), `:3640-3692` (spec
prompt), `:5629` (drain gate), `:725-763` (proving gate); `factory.config.yaml:9-19,222-240`;
`docs/runbooks/first-live-run.md:34-38`; `work-protocols/architect-operations.md` (canon home).

## 7. Pre-re-seed factory fixes (incident 22-06 — treasury merge-gate loop)

The `treasury-payments.treasury-app-foundations` stage looped **12×** at the merge gate
(BUILD→VALIDATE→AUDIT→MERGE_GATE→BUILD) over ~8.5h, burning budget. Validation + audit passed EVERY time
(the code is fine — only 2 trivial findings total); Tier-1 failed every time on ONE infra bug. ARH-01
stopped the factory 22-06 ~12:37 UTC to halt the burn. These MUST land before re-seeding (stages run
again post-seed):

1. **Test-PG socket path overflow (HIGH).** The test Postgres unix socket lives INSIDE the worktree:
   `.../.worktrees/<stage_id>/.devpg/.s.PGSQL.5433`. For a long `stage_id` this exceeds the OS AF_UNIX
   limit (**107 bytes**); treasury's 42-char id → **109 bytes** → 1052 DB tests ERROR → merge gate fails
   → rework (`build_noop`, nothing to fix) → infinite loop. **Fix:** relocate the socket to a SHORT,
   name-length-independent dir (e.g. `/tmp/devpg-<shorthash>/` via `PGHOST`) in the workspace test-PG
   setup. (The unix socket itself came from the pg-in-agents AF_INET fix, propagated to erp-workspace
   `main`.) **Belt-and-suspenders:** keep layer stage-ids SHORT — budget
   `45 (fixed prefix) + len(stage_id) + 21 (/.devpg/.s.PGSQL.5433) ≤ 107` → `len(stage_id) ≤ ~40`.
2. **Merge-gate loop-cap (MEDIUM — Doctrine §8/§20).** The factory bounced the merge gate 12× with NO
   escalation and NO cap — a silent infinite loop that drain does NOT stop (drain holds new spawns, not
   in-flight rework). **Fix:** after N consecutive same-gate (Tier-1) failures with a `build_noop`
   rework, ESCALATE + halt the stage instead of re-looping. The counter + cap go in the scheduler's
   merge-gate path (`scheduler.py`, the `tier1_gate` / MERGE_GATE→BUILD logic).
3. **Short stage-ids constraint (carry into §5.1 authoring).** Until fix #1 lands, the per-layer
   stage-ids in the seed JSON MUST satisfy the ≤~40-char budget above.

---

## 8. ARH-04 EXECUTION UPDATE (23-06-2026) — supersedes the stale bits above

**§5.1 "author the plans" is DONE.** The structure is authored, dual-audited (opus+codex), reconciled,
and validated against the live factory code. Artifacts:
- `docs/projects/erp/rebuild/macro-plan.json` — 10 layer phases, linear DAG (committed `e1adf51`).
- `docs/projects/erp/rebuild/phase-plans/<l*>/phase-plan.{json,md}` — **40 stages** (20 BE / 20 FE, 14
  contract, 6 critical). Every plan passes `scripts/validate_phase_plan.py` (read_phase_plan + size gate).
- `docs/projects/erp/rebuild/STRUCTURE.md` — master view + the **SCOPE BOUNDARY** (deferred domains).

**§7 pre-re-seed fixes — STATUS:** (1) test-PG socket overflow = **DONE on erp-workspace main** (`bb95800`,
fixed-length `/tmp/sfpg-<hash>/` socket — short-ids NO LONGER load-bearing). (2) merge-gate loop-cap =
**DONE** (`merge_gate_max_tier1_failures: 3`). (3) short-ids = moot (socket fixed) but ids are short anyway.
PLUS a NEW fix ARH-04 found + landed: **builder kind×risk routing** (`00c857f`) — backend structural/critical
now correctly route to **codex** (were silently going to opus; completes Step-2's "backend→codex" intent).

### 8a. THE LANDING-MECHANISM DECISION (founder call — the #1 gate to USABILITY)

`scheduler._step_planning` ALWAYS spawns `phase_architect` to AUTHOR the phase-plan at runtime — there is
**no pre-place/ingest path**, so the dual-audited structure has no mechanical way to land today (the
playbook §6 "pre-placing is UNVERIFIED" gotcha is the real gap; the redesign's "phase_architect narrows to
contracts/spec, not stage-generation" was proposed, never built). Two options:

- **(A) Mechanical ingest-wiring — RECOMMENDED.** Add a config key `projects.<id>.prefrozen_phase_plans:
  Path|None`. When set, `_step_planning`: (1) copies the ratified `phase-plan.{json,md}` for `<phase_id>`
  into the worktree, (2) validates via `read_phase_plan`, (3) spawns `phase_architect` with a NARROWED
  prompt — "the stage plan is FROZEN; author ONLY the intra-phase `_factory/contracts/` seams these stages
  reference" — and (4) asserts the committed `phase-plan.json` byte-matches the ratified one (else escalate).
  Result: the dual-audited STAGES are mechanically what runs; the CONTRACTS (interface specs Tier-2
  validates against) are still LLM-authored per-phase (they need per-phase technical depth). Backward-compat:
  key unset → today's behavior. Bounded change (~config key + one `_step_planning` branch + tests),
  dual-audited. This is the **mechanical guarantee** the founder repeatedly demands.
- **(B) Prompt-adopt — no code.** Commit the phase-plans into the workspace; enrich `_planning_prompt` to
  "a ratified plan exists at <path> — adopt verbatim, author contracts." Relies on `read_phase_plan` + the
  proving-ground checkpoint (stop after PLANNING, review, resume). Lower friction; stage-adoption is
  TRUST-based (the agent could deviate) — NOT a mechanical guarantee.

**Recommendation: A.** It is built AFTER founder approval (it is part of re-seed EXECUTION, founder-gated) —
NOT pre-built here, because it is a pipeline-semantics change the founder should ratify (Doctrine §12/§13).

### 8b. Updated step sequence (replaces §5 where it differs)
1. **Founder** approves the structure (STRUCTURE.md) + the scope boundary + picks the landing option.
2. **[if A] ARH** builds + dual-audits + tests the ingest-wiring; merges. (Post-approval.)
3. **ARH** exports the **#104 (ASM-006)** contest dossier (`/artifact/1200`) + decision #26 to a
   carry-forward doc BEFORE archiving the DB; record #26 "superseded by L7 `cont-quote-land`".
4. **Founder (copy-paste)** disarm watchdog → clean-stop (already stopped; re-confirm 0 agents + pid gone).
5. **ARH** backup DB; (Strategy A) archive + `sf-factory init` fresh; re-insert `runtime_settings`
   VERBATIM (`drain.manual=true`, `max_parallel_agents=2`, `budget.critical=500000000`,
   `budget.routine=80000000`).
6. **ARH** `sf-factory seed-phases docs/projects/erp/rebuild/macro-plan.json --dry-run` → verify 10
   phases + 9 edges + anchor → seed for real. (macro-plan MUST be committed & clean — it is, `e1adf51`.)
7. **ARH** place the pre-authored phase-plans per the landing option (A: set the config key + commit them
   where it points; B: commit into the worktree path + enrich the prompt).
8. **ARH** graft the 2 parked branches into the factory-created stage branches: `cont-quote-core` →
   L7 `cont-quote-land`; `treasury-app-foundations` → L8 `treasury-found`. (Both 1 commit behind main,
   merge clean — low friction; highest-risk MANUAL step, verify diff.)
9. **ARH** seed **Diapazon** data — a fiscal range per issuing OwnPJ (registry nomenclature, generic CRUD)
   — BEFORE the L6 fiscal-invoice gate (its management screen is L9).
10. **ARH** set `proving_phases: [l0-shell]` in `factory.config.yaml` (prove the new pipeline on the
    smallest layer before fan-out) + restart-only, so the hold takes effect.
11. **Founder (copy-paste)** restart factory; verify dashboard bound + recovery + liveness; re-arm
    watchdog. **Start the session monitor** (factory is live again). **Keep drain ON.**
12. **ARH** lift drain layer-by-layer as each layer is built + founder-verifies the deployed layer.

### 8c. Preservation (unchanged from §3 — RE-CONFIRM before any mutation)
Never delete the 2 `stage/*` branches; do NOT reset workspace `main` (foundation+inventory); re-insert
`runtime_settings`; back up `factory.db`+wal+shm; export the #104 dossier + #26 first.
