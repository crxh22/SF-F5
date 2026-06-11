# Phase-Seeding & Etapa-5 Readiness — design slice

**Status:** design, v1.1 — 2026-06-12, revised after adversarial review (R1: control-plane conformance, R2: failure modes/ops — both approve_with_fixes; all findings dispositioned, none rejected; see Review log). Governed by `00 - DOCTRINA.md`; binding spec `_FRAMEWORK_MVP_DoD.md`; amends `docs/design/control-plane-design.md` v1.4 (additive — CCR-5 list in §6).
**Trigger:** decision log D-0021 needs_architect item (1) — "no operator command to create a phase — manual DB insert is the only entry; trigger: Etapa 5 intake defines the sanctioned path" — plus two Etapa-5 readiness gaps found while preparing the intake package (§4, §5: no real `claude` pipeline agent has ever run — B8 exercised real codex only; the Phase Architect prompt carries zero project context) and one config key that exists but changes no behavior (§5b: `proving_phases`, a Doctrine §17 violation as-is).

---

## 1. Problem

1. **No sanctioned phase-creation path.** Phases enter the DB only by manual SQL (how the demo and B8 were seeded). The intake interview produces the first real macro plan; it must land in the orchestrator through a validated, transactional, operator-visible command (Doctrine §20; DoD §2.1).
2. **The Phase Architect prompt has no project context.** `_planning_prompt` points at no business documentation, no PROJECT.md, no contracts. On the real ERP the agent would plan from the phase *name*.
3. **`claude` print-mode agents cannot write (or read outside cwd).** The claude adapter passes no permission flag; in `-p` mode the default mode denies writes — every real claude Builder/Validator fails at its first artifact (same defect class as the codex sandbox fix `470f4ad`; invisible until now because B8's builders were stubs).
4. **Unconstrained first fan-out.** `proving_phases` is config-declared and consumed by nothing; with a full macro plan seeded, foundation sign-off would silently dispatch three parallel phase PLANNINGs as the factory's *first* production fan-out, contradicting the DoD §15.3 proving order.

## 2. The sanctioned path — `cli seed-phases`

### 2.1 Operator flow (end-to-end)

```
(interview ratifies macro plan)
→ Main Architect commits docs/projects/<project>/PROJECT.md + macro-plan.json    [factory repo]
→ cli init                                                                       [db + migrate, idempotent]
→ operator bootstraps the workspace once (runbook section, §3)                   [one-time, per project]
→ cli seed-phases docs/projects/<project>/macro-plan.json                        [THE sanctioned path]
→ cli run                                                                        [phases dispatch normally]
```

### 2.2 `MacroPlan` schema + validation (`artifacts.py`)

Mirrors the `PhasePlan`/`read_phase_plan` contract style:

```python
class MacroPhase(BaseModel):   """id: str, name: str; extra='forbid'."""
class MacroPlan(BaseModel):    """project: str, phases: list[MacroPhase], dag_edges: list[tuple[str, str]]; extra='forbid'."""
def read_macro_plan(path: Path, *, projects: Collection[str]) -> MacroPlan:
    """Strict-validate BEFORE any DB write: project ∈ projects; phase ids unique, non-empty,
    and matching the same id grammar as plan-local stage ids (_PLAN_ID_RE — ids feed branch
    names, artifact dirs, and stage namespacing; a malformed id must die here, not at
    dispatch); dag edges cycle-checked over the subgraph induced by the plan's OWN phases —
    edge endpoints NOT declared in the plan are tolerated here (they may resolve to
    existing DB phases; the CALLER owns that resolution and the combined-graph re-check,
    because artifacts.py's file-contract validators stay DB-free even though the module
    may import db — placement rationale, not an import-rule claim).
    Malformed → ArtifactContractError (fail-explicit, Doctrine §7)."""
```

### 2.3 Command semantics

`cli seed-phases <plan.json> [--dry-run]`:

1. **Exclusive-instance guard:** acquire `_InstanceLock` (cli.py) on `process.pid_file` in a new **`claim=False`** mode: take the exclusive flock on the SAME file (mutual exclusion with `run`/`resume` needs the same inode) but SKIP the pidfile truncate/write/fsync — a short-lived seeder must never (a) reset the pidfile mtime, which grants the watchdog's freshness grace and can silence an actively-paging watchdog for up to `staleness_threshold_s` while the orchestrator is down, nor (b) record itself as "the orchestrator" for `cli status` and the next `run`'s pid-liveness refusal. If the flock is held → abort `"orchestrator running — stop it first (runbook: seed only while stopped)"` (the runbook rule, mechanical — Doctrine §20; provenance: D-0021 ops learnings, incident-born).
2. **Plan validation:** `read_macro_plan` (§2.2); then against the DB (read paths: `db.list_units(level=phase)` + new `db.list_dag_edges(level)`):
   - **single-project guard:** if existing phases reference a DIFFERENT project → abort naming the fresh-DB-per-project MVP posture (D-0022 item 3) and the archived-DB convention (D-0023). Seeding a second project into a live DB would make every subsequent `recover()` abort at `_repo_roots` — refusal here is the only sanctioned outcome until multi-project mapping lands.
   - every plan phase id NEW (any existing id → see replay rule below);
   - every edge endpoint ∈ plan ∪ DB; endpoints resolving to DB phases must not be in a dead state (`FAILED`/`CANCELLED` → abort naming the dead prerequisite — `deps_done` requires DONE, so a dead prerequisite seeds a permanently-WAITING unit);
   - edge not already present in the DB (named abort, not a raw PK IntegrityError);
   - combined graph (existing edges ∪ plan edges) acyclic.
   - **Idempotent replay:** if EVERY plan phase id already exists AND the registered `macro_plan` ref matches this file's (path, sha256, git_commit) → exit 0 `"already seeded at <event ts> — nothing to do"` (a crash after commit but before output must not present as a collision); any divergence → nonzero naming the differing ids.
3. **Workspace precondition (fail-early, not at first gate):** `projects.<project>.workspace` exists, is a git repo, has `integration_branch`; `projects.<project>.test_command` is non-null (a null command otherwise dies as ConfigError at the first MERGE_GATE — after the full SPEC/BUILD/VALIDATE token spend); when the command references a workspace-relative script, that file exists and is committed; `_factory/contracts/` on the integration branch is non-empty (an empty contracts dir would make every Tier-2 gate validate against nothing, silently voiding the mechanism B8 proved — D-0022). Each failure aborts pointing at the bootstrap runbook.
4. **Committed-plan precondition:** the plan file is **tracked** (`git ls-files --error-unmatch` — porcelain-empty alone false-passes a gitignored file, whose blob would resolve at no commit and poison the next `recover()`), unmodified (`git status --porcelain -- <path>` empty), in the factory repo; anchor = `git rev-parse HEAD`.
5. **One transaction:** insert `phases` rows (state `PENDING`, `branch=NULL` — dispatch derives `phase/<id>`; `plan_artifact_id=NULL` — consistent with `_step_planning`, which registers refs without touching the column) + `dag_edges(level='phase')` + **exactly ONE** `register_artifact(kind='macro_plan', repo='factory', unit_level='factory', unit_id=<project>, git_commit=<anchor>)` — per-phase refs are impossible against `UNIQUE (repo, path, sha256)` + get-or-create (N calls collapse into the first row), and a factory-level ref is *stricter* under `verify_integrity` (`_unit_status` checks unknown unit levels forever, never downgrading — correct for a plan that outlives any single phase; `artifact_refs.unit_level` carries no CHECK; the `'factory'` repo root is always passed by `_repo_roots`) — plus one `events` row per phase (`event_type='phase_seeded'`, `actor='main_architect'`, payload: plan path, anchor sha, macro_plan ref id).
6. `--dry-run`: run 2-4, print the would-be inserts, write nothing.

Output: summary (phases, edges, ref id, **anchor commit sha** — the operator must know which factory commit is pinned). Exit nonzero on any precondition failure; zero writes on failure (single tx).

### 2.4 What seed-phases is NOT

- It does NOT create stages — stage decomposition is the Phase Architect's job in PLANNING (DoD §3.3).
- It does NOT start the orchestrator, arm the watchdog, or touch worktrees.
- It does NOT edit `factory.config.yaml` (config is founder-governed, Doctrine §14).
- It does NOT support seeding a second project alongside a live one (§2.3.2 guard; D-0022 posture).

## 3. Workspace bootstrap — operator runbook, not code

One-time per project, operator-driven (Main Architect session — a DoD-sanctioned interactive role outside the orchestrator, OPEN-4; DoD §12.A1's "zero manual file shuffling by the founder" binds the conveyor, not one-time setup), appended to `docs/runbooks/first-live-run.md`:

```bash
git init -b main /home/artur/projects/erp-workspace
cd /home/artur/projects/erp-workspace
# .gitignore: .worktrees/, __pycache__/, *.pyc, .pytest_cache/, .ruff_cache/, node_modules/, .venv/
# scripts/test.sh — the stable Tier-1 indirection (config: projects.erp.test_command = "bash scripts/test.sh")
# _factory/contracts/ — the ratified cross-phase contracts: MOVED here from docs/projects/erp/contracts/
#   (this directory becomes the CANONICAL home from this commit on — Doctrine §9; the factory-repo dir
#    is replaced by a one-line pointer in the same ratification commit; gates read ONLY the workspace)
git add -A && git commit -m "erp workspace bootstrap: contracts v0 + test indirection"
```

Rationale for *not* coding this: one-time setup whose every artifact is ratified content, not mechanical derivation; `seed-phases` verifies the result mechanically (§2.3.3), which is what Doctrine §20 actually demands; coding it would be a preventive mechanism without incident (Doctrine §8).

Runbook notes added in the same section:
- **Append-only anchors:** factory-repo commits that registered refs point at (seed anchors, decision answers) are history — never amend/rebase the factory repo while seeded phases are non-terminal, or `verify_integrity`'s recorded-commit check aborts every start once gc prunes the sha.
- **Proving-ground PLANNING checkpoint (one-time):** for the first real phase (foundation), stop the orchestrator after PLANNING commits the phase plan, review `phase-plan.json` + intra-phase contracts, then resume (the A2-validated path). Bounded validation of an untrusted first-use mechanism (Doctrine §10) — not standing operator attention.
- **OPEN-2 ratification note:** setting `projects.erp.test_command` deliberately amends the pinned config test (`tests/unit/test_config.py` asserts it `None` today).

`scripts/test.sh` initial content (proposed, OPEN-2 input): `uv run pytest -q`, with the no-tests bootstrap window handled **self-retiringly**: on pytest exit code 5 ("no tests collected") the script greens ONLY if the workspace has no committed test files (`git ls-files -- 'tests/**/*.py' '**/test_*.py'` empty); once any test file exists, exit 5 = FAILURE (it then means collection/deselection misconfig — e.g. a bad `testpaths`/`-k` — and must never silently green the Tier-1 gate; Doctrine §20). Explicit in the script body, never hidden in config.

## 4. Phase Architect project context (`scheduler.py` delta)

`_planning_prompt` gains a project-context block, config-driven, when the keys are present:

```
Project context (read before planning):
- Business documentation (canonical source of truth): <projects.<p>.docs_repo — absolute path>
- Macro plan & project brief: <factory.home>/<projects.<p>.project_md> (PROJECT.md; the macro
  decision log sits next to it)
- Cross-phase contracts already in force: _factory/contracts/*.md (READ-ONLY — a needed change is
  a _CONTRACT_CHANGE_REQUEST.md + stop)
- Write YOUR intra-phase contracts under _factory/contracts/phase-<id>/ (namespace convention:
  cross-phase files at the root are never edited by a phase; both Tier-2 collection sites rglob
  recursively, so namespaced contracts are picked up unchanged)
```

New config key: `projects.<id>.project_md: Path | None = None` (factory-repo-relative; `None` ⇒ block omitted — synthetic/b8 projects unaffected). The existing prompt line "freeze the intra-phase contracts … under `_factory/contracts/`" is amended to the namespaced path. Spec/Build prompts deliberately unchanged (stage agents work from spec + acceptance criteria; evidence may later justify more — Doctrine §8).

## 5. Claude print-mode permissions (`runner.py` delta) + out-of-bounds detector

`ClaudeAdapter.build_cmd`: when `route.tools != "none"`, append `--permission-mode bypassPermissions` (verified against the installed CLI). Tools-off Decision Sessions unchanged.

**Honest posture statement (corrected in review):** this is NOT symmetric with codex — `--sandbox workspace-write` is OS-enforced and workspace-scoped; `bypassPermissions` removes claude's own gating entirely, and print-mode default-deny was, incidentally, the only mechanical enforcement of DoD §2.3 ("agents never touch the operational database"). A print-mode agent has no human to answer prompts — a denied write is a wedged stage — so the flag is necessary; the lost guardrail is replaced, not waved away:

**Out-of-bounds detector (mechanical, Doctrine §20):** at every stage MERGE_GATE entry and during `Scheduler.recover()`, the control plane runs `git status --porcelain` on (a) `factory.home` and (b) the project workspace integration checkout, filtered through `process.isolation_ignore_globs` (precedent: D-0022/c50bf37). Unexpected dirt → `alert` event (`payload`: repo, paths) + ntfy alert, deduplicated per streak — never a silent pass. This makes the §10 falsifiability trigger *observable*: "agent damaged state outside its worktree" is detected by the machine at the next gate, not by someone noticing. Unit worktrees are already covered (recovery canonicalization + dirty-diff evidence). **Residual risk, accepted explicitly:** a foreign write to the SQLite DB or gitignored operational files remains undetected (the D-0015 incident class — agent writing the decision log — IS covered, via (a)); a DB-tamper tripwire (e.g. `PRAGMA data_version` correlation, complicated by the sanctioned `cli decide` second writer) is deferred to first incident (Doctrine §8).

**OPEN-S2 disposition:** blanket-for-tools-on, pinned in the adapter argv (consistent with the §5.1 frozen-argv pattern and the codex `--sandbox` precedent), WITH the detector above; the revisit path is pre-registered as a config key shape — `models.<role>.permission_mode` — so narrowing later is a config addition, not a contract change.

## 5b. `proving_phases` dispatch hold (`scheduler.py` delta)

The key exists in config and changes no behavior — a standing Doctrine §17 violation that becomes operationally dangerous the moment a full macro plan is seeded (the factory's FIRST phase-level fan-out would fire automatically at foundation sign-off). Semantics given to the existing key:

> A phase-level unit whose id is NOT in `projects.<p>.proving_phases` is dispatch-held (stays PENDING even when deps are DONE) while ANY phase listed in `proving_phases` is non-DONE. Once every proving phase is DONE, the hold dissolves and the DAG governs alone. Empty/absent list = no hold.

One filter in the scheduler's RUNNABLE selection, phase-level only, config-driven (Doctrine §14). The hold's effect is visible: held units render as PENDING with a `held: proving` marker in `cli status`. This preserves DoD §15.3 (foundation, then inventory-procurement as the first full phase) mechanically, while the committed macro plan + DB DAG satisfy C10's "≥2 phases parallelizable after Foundation" from seed time.

## 6. Frozen-surface amendments (CCR-5, all additive)

| Surface | Amendment |
|---|---|
| `artifacts.py` §4 | + `MacroPhase`, `MacroPlan`, `read_macro_plan(path, *, projects)` |
| `db.py` §4 | + `list_dag_edges(conn, level: Level) -> list[tuple[str, str]]` (read path for §2.3.2; also used by the duplicate-edge named abort) |
| `cli.py` §4 docstring | subcommand list += `seed-phases`; `_InstanceLock.acquire(claim: bool = True)` (private class — recorded for the §4 cli docstring's flock narrative, not a frozen signature) |
| `config.ProjectCfg` | + `project_md: Path | None = None` |
| `artifact_refs` vocabulary (§2 comment) | + kind `'macro_plan'`; unit_level `'factory'` usage documented |
| `events.event_type` vocabulary (§2 comment) | + `'phase_seeded'`, out-of-bounds `'alert'` payload shape |
| `ClaudeAdapter` argv (§5.1 literal) | + `--permission-mode bypassPermissions` when tools enabled |
| `Scheduler` dispatch (§4 prose) | proving-phases hold (§5b) + out-of-bounds detector at gate/recover (§5) |

Ratification updates `control-plane-design.md` annotations + this file's status, per Doctrine §19.

## 7. Failure modes (fail-explicit, Doctrine §7)

| Failure | Behavior |
|---|---|
| orchestrator running | abort before any read/write (flock test, claim-free) |
| plan malformed / bad id grammar / cyclic / unknown project | `ArtifactContractError` → nonzero exit, nothing written |
| second project into a live DB | abort naming fresh-DB-per-project posture (D-0022/D-0023) |
| phase id exists, same plan ref | exit 0 "already seeded" (crash-replay) |
| phase id exists, divergent plan | abort naming differing ids |
| edge endpoint unknown (plan ∪ DB) | abort, names the endpoint |
| edge prerequisite FAILED/CANCELLED | abort, names the dead prerequisite |
| edge already in DB | abort, names the edge |
| combined DAG cyclic | abort, names the cycle |
| workspace missing / wrong branch / null test_command / missing committed test script / empty contracts dir | abort, points at bootstrap runbook |
| plan file untracked / gitignored / dirty | abort, names the file and the factory repo |
| tx failure mid-insert | rollback — zero rows |
| agent writes outside its worktree (post-build) | out-of-bounds detector: alert event + ntfy at next gate / recover |

## 8. Tests

- `read_macro_plan`: happy; unknown project; dup/empty/malformed ids (grammar); plan-local cycle; foreign endpoint tolerated; extra keys rejected.
- `cli seed-phases`: happy path (phase rows + edges + ONE factory-level ref + per-phase events, all-or-nothing); duplicate-phase divergent abort; idempotent replay exit 0; flock-held abort; claim-free lock leaves pidfile bytes + mtime untouched; multi-project abort; missing-workspace / missing-branch / null-test_command / missing-script / empty-contracts aborts; untracked + gitignored + dirty plan aborts; edge-to-existing-DONE-phase accepted; edge-to-FAILED abort; duplicate-edge abort; combined-cycle abort; `--dry-run` writes nothing.
- `ClaudeAdapter.build_cmd`: bypass flag present iff tools enabled — **amends the two existing golden-argv tests by name** (`test_claude_build_cmd_full_argv_order`, `test_claude_build_cmd_minimal`); tools-off golden untouched.
- `_planning_prompt`: context block present iff `project_md` set; intra-phase namespace line present; block absent for b8-style projects.
- Scheduler: proving-hold (held while proving non-DONE; releases on DONE; empty list = no hold; `cli status` marker); out-of-bounds detector (dirty factory repo → alert event + ntfy stub; ignore-globs filtered; clean = no event).
- Integration smoke: temp factory+workspace pair → seed 2-phase plan → `run_until_blocked` with stub routes → first phase reaches PLANNING consuming the seeded row.

## 9. Build lanes (single builder, enumerated files)

`src/sf_factory/artifacts.py`, `src/sf_factory/db.py` (new read function only), `src/sf_factory/cli.py`, `src/sf_factory/scheduler.py` (planning prompt + dispatch hold + gate/recover detector), `src/sf_factory/runner.py` (ClaudeAdapter only), `src/sf_factory/config.py` (ProjectCfg only), `factory.config.yaml` (projects.erp.project_md only), `tests/unit/test_artifacts.py`, `test_db.py`, `test_cli.py`, `test_runner.py` (incl. the two NAMED golden-argv amendments — the only permitted edits to existing test functions), `test_scheduler.py`, `tests/integration/test_seed_phases.py` (new), `docs/runbooks/first-live-run.md` (bootstrap + checkpoint + append-only + OPEN-2 notes). Builder forbidden from: decision-log writes, staging unrelated files, interface edits beyond §6 (CCR discipline, D-0015 rule).

## Resolved review OPENs

- **OPEN-S1 → resolved:** ONE factory-level ref (unit_level='factory', unit_id=project) — per-phase refs impossible against the DDL; factory-level is stricter under verify_integrity (R1-1/R2-1).
- **OPEN-S2 → resolved:** blanket bypass + mechanical out-of-bounds detector + pre-registered narrowing key shape (R2-2/R2-10).
- **OPEN-S3 → kept:** incremental seeding stands, with the §2.3.2 dead-prerequisite state check (R2-9).

## Review log

Adversarial review, 2026-06-12 — two independent reviewers (R1 conformance/feasibility, R2 failure-modes/ops), both `approve_with_fixes`; 19 findings (1 critical, 9 major, 9 minor); **all applied, 0 rejected** in v1.1: one-ref registration (R1-1/R2-1), claim-free flock (R1-2), `db.list_dag_edges` (R1-3), named golden-argv amendments (R1-4), placement-rationale fix (R1-5), id grammar + endpoint tolerance (R1-6), trackedness probe (R1-7), OPEN-2 test note (R1-8), single-project guard (R1-9/R2-4), detector + honest posture + key-shape pre-registration (R2-2/R2-10), deep workspace preconditions (R2-3), self-retiring exit-5 shim (R2-5), idempotent replay (R2-6), canonical contracts home (R2-7, with the ERP package), append-only anchor note (R2-8), dead-prerequisite check (R2-9). §5b (proving hold) entered v1.1 from the package review (R4-1) — same ratification.
