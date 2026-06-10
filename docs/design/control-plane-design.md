# Control-Plane Design — SF-F5

**Status:** design, v1.2 — 2026-06-10, revised after adversarial review + contract amendment CCR-1 (see Review log). Binding spec: `_FRAMEWORK_MVP_DoD.md` (DoD v3); governed by `00 - DOCTRINA.md`; parameters: `factory.config.yaml`; constraints from decision log D-0002/D-0003/D-0007/D-0009.
**Harvest note (D-0002):** point mechanics consulted read-only in `~/projects/SF/factory-source/` — NDJSON line-tolerant parsing + terminate/kill grace pattern (`agents/transport.py`), idempotent worktree create + worktree-root/branch guards (`orchestrator/git_manager.py`), `VALID_TRANSITIONS: dict[State, set[State]]` table shape (`orchestrator/models.py`). All rewritten to this design; no architecture, stage sizing, or audit density inherited.
**Stack (D-0007):** Python 3.12, uv project (package `sf_factory`, src layout), pydantic v2, pytest, ruff. All tunables read from `factory.config.yaml` by key — zero hardcoded values (Doctrine §14). Timestamps: ISO 8601 UTC strings (`conventions.md`).

---

## 1. Module decomposition — `src/sf_factory/`

One responsibility per module, explicit boundaries (Doctrine §0). Agents never import any of this: they see files only (DoD §2.3).

| Module | Sole responsibility | May import |
|---|---|---|
| `models.py` | Domain vocabulary: enums, frozen dataclasses, `VALID_*_TRANSITIONS`, error taxonomy, `utc_now`/`new_id`. Zero I/O. | stdlib only |
| `config.py` | Load + pydantic-validate `factory.config.yaml` into `FactoryConfig`; cross-field checks. | models |
| `db.py` (+ `migrations/*.sql`) | SQLite WAL connection (sole writer), versioned migrations, transaction primitive, typed repository functions. No business rules. | models |
| `statemachine.py` | The only writer of unit `state` columns: transactional transition + event emission. | models, db |
| `runner.py` | Spawn `claude -p` / `codex exec` subprocesses (own process groups), canon injection (D-0009), session resume, NDJSON streaming + stderr capture, timeout/terminate/kill, process registry + token accounting, declared-failure detection. | models, config, db |
| `artifacts.py` | Artifact path conventions, sha256 hashing, registration (DB stores path+hash only, never content), sentinel + validation-sidecar + phase-plan contracts, integrity verification. No git operations. | models, db |
| `worktrees.py` | All git mechanics: worktree lifecycle, commit helper, git-state healing, Tier-1 merge gate (rebase + full test suite) + integration merge (serialized per target branch), diff primitives (digest / full / merged-unit). No agent judgment (DoD §5.1). | models, config |
| `thresholds.py` | DoD §8 mechanical triggers: counter recording + SQL evaluation. Decides nothing beyond firing. | models, config, db |
| `consultation.py` | CP framework: registry from config, schema-validated closed verdict sets, deterministic fallback, full call logging, breach detection. CP-1 is the only registered point. | models, config, db, runner, artifacts |
| `scheduler.py` | Level-agnostic DAG loop over stages AND phases (same code path, DoD §3.2); per-level `UnitExecutor`s; liveness refresh; crash recovery entry. | all above, notify |
| `notify.py` | ntfy HTTP publisher (title + deep link only, D-0004). | models, config |
| `watchdog.py` | External liveness check run from cron/systemd timer (separate process; OS scheduler is root of trust, DoD §9). | models, config, notify |
| `cli.py` | Operator entry: `init` / `run` / `status` / `resume` / `decide`. | all |

Deviations from the expected list (one-line justifications): error taxonomy lives inside `models` (it is shared vocabulary; avoids a 14th micro-module every module imports); `watchdog` added because DoD §9 mandates an external liveness checker and it needs a shippable entry point (`python -m sf_factory.watchdog`); crash recovery is owned by `scheduler.recover()` since it spans db+git+processes and gates the loop start; `decide` CLI subcommand added as the DoD §9 emergency-fallback plumbing. The dashboard is deliberately **not** designed here — DoD §16.3 defers its stack to the implementation session; its boundary is fixed: read views over `db` + artifacts served from the orchestrator's surface, **plus exactly one write path** — the decision-answer endpoint (validated option, or confirmed Decision Session outcome → `answer_decision` marshalled onto the orchestrator loop thread per §7's single-writer rule → transcript/decision artifact committed to git → dependent subtree unblocked) — because DoD §9/§12.A4 require decisions to be *answered from the dashboard*; `cli decide` stays the emergency fallback only. Orchestrator-spawned Decision Sessions belong to that dashboard design slice (OPEN-4).

Import DAG (acyclic, matching the May-import column exactly): leaf `models`; `config` and `db` import only it; `statemachine`/`artifacts`/`thresholds` add `db`; `worktrees`/`notify` add `config` and **never** `db`; `runner` = models+config+db; `consultation` builds on runner; `scheduler` on all of the above + notify; `watchdog` = models+config+notify; `cli` on all.

Artifact location rule (design decision): stage artifacts live **inside the stage worktree** under `_factory/stages/<stage_id>/` so spec/reports are co-versioned with the code and travel through the same merge gates; phase plans + frozen contracts live on the phase integration branch under `_factory/contracts/` and `_factory/phases/<phase_id>/` (read-only to units, DoD §5.1); macro artifacts stay in the factory repo (`docs/`). `artifact_refs.repo` records which repo each ref resolves in. Only **registered** artifact paths are canonical stage outputs; the Validator's derived tests never enter the stage worktree (§3.1 Validator isolation).

---

## 2. SQLite DDL (migration `0001_init.sql`)

WAL mode. Read/write paths, stated once (DoD §6): the orchestrator process is the **sole writer**, and exactly one instance runs (enforced by the §4 `cli` flock); agents see files only, never the DB; the watchdog reads the liveness **file** only — no DB dependency by design; the operator `cli status` and the dashboard backend are sanctioned read-only access (`mode=ro` connections or orchestrator-served views) — both are the orchestrator's own codebase, not agents. Every state transition = one transaction. States/levels carry CHECKs (code-owned enums); `risk_class` and role names carry **no** CHECK — they are config-defined sets validated at insert against `FactoryConfig` (Doctrine §14).

```sql
-- No PRAGMAs in migrations: journal_mode=WAL, synchronous=NORMAL, foreign_keys=ON, busy_timeout are set at
-- connect (db.open) — a journal_mode PRAGMA inside the per-migration transaction is a silent no-op.
CREATE TABLE schema_migrations (
  version     INTEGER PRIMARY KEY,
  description TEXT NOT NULL,
  applied_at  TEXT NOT NULL                                   -- ISO 8601 UTC, as all *_at below
);
CREATE TABLE phases (
  id               TEXT PRIMARY KEY,                          -- e.g. 'foundation'
  project          TEXT NOT NULL,                             -- key into config projects.*
  name             TEXT NOT NULL,
  state            TEXT NOT NULL CHECK (state IN ('PENDING','PLANNING','CONTRACTS_FROZEN','RUNNING',
                     'INTEGRATING','AWAITING_SIGNOFF','AWAITING_HUMAN','ESCALATED','DONE','FAILED','CANCELLED')),
  branch           TEXT,                                      -- integration branch, e.g. 'phase/foundation'
  plan_artifact_id INTEGER REFERENCES artifact_refs(id),
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE TABLE stages (
  id               TEXT PRIMARY KEY,
  phase_id         TEXT NOT NULL REFERENCES phases(id),
  name             TEXT NOT NULL,
  risk_class       TEXT NOT NULL,                             -- validated against config risk_classes keys
  state            TEXT NOT NULL CHECK (state IN ('PENDING','SPEC','BUILD','VALIDATE','AUDIT',
                     'AWAITING_HUMAN','MERGE_GATE','ESCALATED','DONE','FAILED','CANCELLED')),
  branch           TEXT,                                      -- 'stage/<id>'
  worktree_path    TEXT,
  spec_artifact_id INTEGER REFERENCES artifact_refs(id),
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE TABLE dag_edges (                                      -- level-agnostic DAG (DoD §3.2)
  level   TEXT NOT NULL CHECK (level IN ('phase','stage')),
  from_id TEXT NOT NULL,                                      -- prerequisite unit
  to_id   TEXT NOT NULL,                                      -- dependent unit
  PRIMARY KEY (level, from_id, to_id)
);
CREATE INDEX idx_dag_to ON dag_edges(level, to_id);            -- deps_done filters by dependent, not the PK prefix
CREATE TABLE events (                                         -- append-only; transitions and lifecycle facts
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,               -- monotonic, never reused
  unit_level TEXT NOT NULL CHECK (unit_level IN ('phase','stage','factory')),
  unit_id    TEXT,                                            -- NULL only for unit_level='factory'
  event_type TEXT NOT NULL,                                   -- 'transition','spawn','exit','timeout','context_reset',
                                                              -- 'declared_failure','contract_change_request','usage_missing',
                                                              -- 'cp_breach_attempt','decision_published','alert',...
  from_state TEXT,
  to_state   TEXT,
  actor      TEXT NOT NULL,                                   -- 'control_plane','founder', or config models.* role key
  payload_json TEXT NOT NULL DEFAULT '{}',                    -- small operational facts only, never artifact content
  created_at TEXT NOT NULL
);
CREATE INDEX idx_events_unit ON events(unit_level, unit_id, seq);
CREATE INDEX idx_events_type ON events(unit_level, unit_id, event_type, seq);  -- sentinel-trigger scans
CREATE TABLE fix_iterations (                                 -- one row per BUILD->VALIDATE loop (DoD §8)
  stage_id      TEXT NOT NULL REFERENCES stages(id),
  iteration     INTEGER NOT NULL,                             -- 1-based, assigned in-transaction
  failing_tests INTEGER NOT NULL,                             -- from validation-report.json sidecar
  report_artifact_id INTEGER REFERENCES artifact_refs(id),
  created_at    TEXT NOT NULL,
  PRIMARY KEY (stage_id, iteration)
);
CREATE TABLE churn (                                          -- patch-over-patch detector (DoD §8, Doctrine §11)
  stage_id   TEXT NOT NULL REFERENCES stages(id),
  file_path  TEXT NOT NULL,
  region     INTEGER NOT NULL,                                -- hunk start line // escalation.churn_region_lines
  edit_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (stage_id, file_path, region)
);
CREATE TABLE consultations (                                  -- one row per CP call (DoD §3.4: digest/verdict/model/latency/cost)
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  cp_id         TEXT NOT NULL,                                -- must exist in config consultation_points registry
  unit_level    TEXT NOT NULL, unit_id TEXT NOT NULL,
  input_digest  TEXT NOT NULL,                                -- sha256 of canonical input payload
  schema_valid  INTEGER NOT NULL CHECK (schema_valid IN (0,1)),
  fallback_used INTEGER NOT NULL CHECK (fallback_used IN (0,1)),
  verdict       TEXT NOT NULL,                                -- the executed verdict (fallback if invalid)
  rationale     TEXT,                                         -- cited rationale (bounded operational text)
  model         TEXT NOT NULL,
  latency_ms    INTEGER, cost_usd REAL, tokens_in INTEGER, tokens_out INTEGER,
  raw_log_path  TEXT NOT NULL,                                -- full request/response in process.ndjson_log_dir
  created_at    TEXT NOT NULL
);
CREATE TABLE escalations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  unit_level  TEXT NOT NULL, unit_id TEXT NOT NULL,
  trigger     TEXT NOT NULL,                                  -- models.Trigger values + 'cp1_verdict','unresolved_contest',
                                                              -- 'semantic_conflict','internal_error'
  target      TEXT NOT NULL CHECK (target IN ('phase_architect','main_architect','founder')),
  payload_artifact_id INTEGER REFERENCES artifact_refs(id),   -- payload = artifacts, not narrative (DoD §8)
  event_seq   INTEGER,                                        -- events.seq that fired this escalation: the dedup
                                                              -- cursor of the always-fire sentinel triggers below
  status      TEXT NOT NULL CHECK (status IN ('open','resolved')),
  resolution  TEXT,                                           -- e.g. 'rework:BUILD','respec','failed'
  created_at  TEXT NOT NULL, resolved_at TEXT
);
CREATE UNIQUE INDEX uq_open_escalation ON escalations(unit_level, unit_id, trigger) WHERE status='open';
CREATE TABLE audit_findings (                                 -- auditor AND integration-validator findings (DoD §7, §5.2)
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  stage_id      TEXT NOT NULL REFERENCES stages(id),
  auditor_role  TEXT NOT NULL,                                -- config models.* key
  finding_ref   TEXT NOT NULL,                                -- finding id within the report artifact
  severity      TEXT,
  report_artifact_id INTEGER NOT NULL REFERENCES artifact_refs(id),
  status        TEXT NOT NULL CHECK (status IN ('open','complied','contested','sustained','overruled','duplicate')),
  contest_artifact_id INTEGER REFERENCES artifact_refs(id),   -- executor's rationale; contests always logged
  resolved_by   TEXT,                                         -- 'executor' | 'phase_architect'
  created_at    TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX idx_findings_stage ON audit_findings(stage_id, status);
CREATE TABLE token_ledger (                                   -- feeds routing economics (DoD §6)
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  process_id INTEGER NOT NULL REFERENCES process_registry(id),
  unit_level TEXT NOT NULL, unit_id TEXT NOT NULL,
  role       TEXT NOT NULL, model TEXT NOT NULL,
  tokens_in  INTEGER, tokens_out INTEGER, cost_usd REAL,      -- NULL = CLI did not report (event 'usage_missing')
  estimated  INTEGER NOT NULL DEFAULT 0,                      -- 1 = filled by budgets.usage_missing_policy estimator
  recorded_at TEXT NOT NULL
);
CREATE INDEX idx_token_unit ON token_ledger(unit_level, unit_id);  -- context_budget evaluation per tick
CREATE TABLE process_registry (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  unit_level TEXT, unit_id TEXT,
  kind      TEXT NOT NULL CHECK (kind IN ('agent','consultation','tests')),
  role      TEXT NOT NULL,                                    -- config models.* key, or 'test_suite'
  cp_id     TEXT CHECK ((kind='consultation') = (cp_id IS NOT NULL)),  -- enforced, not just commented
  session_id TEXT,                                            -- CLI session id from the init/result NDJSON line
                                                              -- (continue_session resume support, DoD §3.4)
  pid       INTEGER,
  cmdline   TEXT NOT NULL,
  cwd       TEXT,
  state     TEXT NOT NULL CHECK (state IN ('spawned','running','exited','timed_out','killed','orphaned')),
  exit_code INTEGER,
  ndjson_log_path TEXT,
  spawned_at TEXT NOT NULL, heartbeat_at TEXT, ended_at TEXT
);
CREATE INDEX idx_proc_state ON process_registry(state);
CREATE TABLE artifact_refs (                                  -- path + hash only, NEVER content (DoD §2.8, §6)
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  unit_level TEXT NOT NULL, unit_id TEXT NOT NULL,
  kind       TEXT NOT NULL,                                   -- 'spec','build_notes','validation_report','validation_sidecar',
                                                              -- 'audit_report','contract','phase_plan','decision_request',
                                                              -- 'decision_answer','escalation_payload','contest_rationale','transcript'
  repo       TEXT NOT NULL CHECK (repo IN ('factory','workspace')),
  path       TEXT NOT NULL,                                   -- relative to the repo root
  sha256     TEXT NOT NULL,
  git_commit TEXT,                                            -- commit that captured this version
  created_at TEXT NOT NULL,
  UNIQUE (repo, path, sha256)
);
CREATE INDEX idx_artifacts_unit ON artifact_refs(unit_level, unit_id, kind, id);
CREATE TABLE decision_requests (                              -- human gates (DoD §9)
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  unit_level  TEXT NOT NULL, unit_id TEXT NOT NULL,
  gate_kind   TEXT NOT NULL,                                  -- 'critical_stage','business','phase_signoff','escalation_tradeoff'
  request_artifact_id INTEGER NOT NULL REFERENCES artifact_refs(id),
  status      TEXT NOT NULL CHECK (status IN ('pending','answered')),
  answer      TEXT,
  answer_artifact_id INTEGER REFERENCES artifact_refs(id),    -- transcript/decision artifact in git
  created_at  TEXT NOT NULL, alerted_at TEXT, answered_at TEXT
);
```

**§8 triggers — mechanical SQL** (evaluated by `thresholds.evaluate`; config values bound as parameters; dedup via `uq_open_escalation`; every events/token_ledger predicate includes `unit_level` so the indices apply):

- `max_fix_iterations`: fires when the last `:n` in-window iterations show no decrease and tests still fail. Two corrections over the naive form (both verified against sqlite3): the window is scoped to iterations recorded **after the last resolved** `max_fix_iterations` escalation for the stage, so the trigger re-arms cleanly after rework instead of firing at most once per stage lifetime; and `LAG` is computed **after** the `LIMIT` subset so `prev` is window-local — LAG-before-LIMIT makes every window row have a non-NULL predecessor once history exceeds `:n`, silencing full stagnation and firing on progress:
  `SELECT COUNT(*) FROM (SELECT failing_tests, LAG(failing_tests) OVER (ORDER BY iteration) AS prev FROM (SELECT iteration, failing_tests FROM fix_iterations WHERE stage_id=:s AND created_at > COALESCE((SELECT MAX(resolved_at) FROM escalations WHERE unit_level='stage' AND unit_id=:s AND trigger='max_fix_iterations' AND status='resolved'), '') ORDER BY iteration DESC LIMIT :n)) WHERE prev IS NOT NULL AND failing_tests >= prev` = `:n - 1` AND latest in-window `failing_tests > 0`, with at least `:n` in-window iterations.
- `churn_threshold`: `SELECT file_path, region, edit_count FROM churn WHERE stage_id=:s AND edit_count >= :threshold`.
- `contract_change_request` (always fires, DoD §8): sentinel event with no covering escalation; the cursor is `escalations.event_seq`, written at escalation insert — in full:
  `SELECT seq FROM events WHERE unit_level=:l AND unit_id=:s AND event_type='contract_change_request' AND seq > COALESCE((SELECT MAX(event_seq) FROM escalations WHERE unit_level=:l AND unit_id=:s AND trigger='contract_change_request'), 0)`.
- `agent_declared_failure`: same cursor pattern over `event_type='declared_failure'`; always fires, never blind-retried.
- `context_budget`: `SELECT COALESCE(SUM(tokens_in),0) + COALESCE(SUM(tokens_out),0) FROM token_ledger WHERE unit_level='stage' AND unit_id=:s` ≥ `budgets.per_stage[risk_class]` — per-aggregate COALESCE: `COALESCE(SUM(a)+SUM(b),0)` reads 0 whenever either column is all-NULL (verified), i.e. an unreported-usage stage would never reach its cap. Estimated rows count toward the total. Firings up to `escalation.max_context_resets` → state-preserving reset (event `context_reset`; the next iteration must NOT resume a session — that is what the reset resets); beyond that → escalation (no silent loop, Doctrine §7/§11).
- Missing usage is mechanically consequential (Doctrine §20 — never "someone remembering to look"): per `budgets.usage_missing_policy`, either `estimate` — conservative logged-stream-bytes/4 written into `token_ledger` with `estimated=1` — or `escalate_after` — more than `budgets.usage_missing_max_per_stage` `usage_missing` events in one stage → escalation. Either way a usage-blind stage still hits a budget.
- Consultation-creep breach scan (DoD §13): runner-only spawning guarantees calls are *logged*, not correctly *tagged* — so tagging is enforced at the spawn boundary (§4 `run_agent` raises `ConsultationBreachError` unless kind='consultation' ⇔ cp_id set ⇔ role ∈ registry consultation roles, and kind='agent' ⇒ role ∈ the config pipeline-role set) plus the DDL CHECK on `cp_id`. The scan must return empty: rows with `kind='consultation' AND cp_id NOT IN (:registry_ids)`, **plus** rows whose role is a consultation role but `kind≠'consultation'`, **plus** rows whose role is outside the config role set.
- Decision latency (DoD §13): `SELECT * FROM decision_requests WHERE status='pending' AND alerted_at IS NULL AND created_at < :now_minus_alert_h`.

---

## 3. State machines

All transitions executed solely by `statemachine.transition()` — validate against table, update row, append `events` row, run coupled writes, in **one transaction** (DoD §6). Guard conditions come from config (`risk_classes`), never hardcoded.

### 3.1 Stage flow (DoD §3.4 + AWAITING_HUMAN, ESCALATED, failure states)

| From | To (guard) |
|---|---|
| PENDING | SPEC (DAG deps DONE, dispatched); CANCELLED |
| SPEC | BUILD (spec artifact registered); ESCALATED; CANCELLED |
| BUILD | VALIDATE (build committed); ESCALATED; CANCELLED |
| VALIDATE | MERGE_GATE (pass ∧ `risk_classes[rc].audits == []`); AUDIT (pass ∧ audits non-empty); BUILD (fail → `continue_session`/`rebuild` via §8-then-CP-1); SPEC (`respec` verdict); ESCALATED; CANCELLED |
| AUDIT | MERGE_GATE (findings closed ∧ no `human_gate`); AWAITING_HUMAN (findings closed ∧ `human_gate`); BUILD (executor complies → rework); ESCALATED (unresolved contest); CANCELLED |
| AWAITING_HUMAN | MERGE_GATE (approved); BUILD / SPEC (changes requested); ESCALATED; CANCELLED |
| MERGE_GATE | DONE (Tier 1 + Tier 2 pass, merged); BUILD (Tier-1 conflict payload or Tier-2 finding routed back); ESCALATED (semantic conflict unresolved); CANCELLED |
| ESCALATED | SPEC / BUILD / VALIDATE (architect rework target); AWAITING_HUMAN (product trade-off, DoD §9.4); FAILED; CANCELLED |
| DONE, FAILED, CANCELLED | ∅ (terminal; rework after FAILED = a new stage re-derived whole, Doctrine §6) |

**Validator isolation (DoD §3.3 — the Builder never sees the Validator's test internals):** the orchestrator runs the Validator in its **own scratch worktree** of the stage branch (`worktrees.create(..., new_branch=False)`, `-validate`-suffixed path); the independently derived tests live and run only there; only `validation-report.md` + `validation-report.json` are copied into the stage worktree and registered. Before each BUILD step of a fix loop the executor asserts no unregistered validator files exist in the stage worktree — otherwise the Builder can code to the Validator's tests from iteration 2 onward, dissolving independent derivation (Doctrine §4) exactly in the loops `max_fix_iterations` governs. The same isolation applies to the Integration Validator at merge gates.

**Tier-2 invocation contract (DoD §5.2 Detect):** at each stage MERGE_GATE (after Tier 1 passes) and again at phase INTEGRATING, the orchestrator assembles for the Integration Validator: (a) the contracts in force (`_factory/contracts/`); (b) the phase plan; (c) **full diffs — bodies, not hunk headers —** of the gating unit (`worktrees.full_diff`, bounded by `process.tier2_max_diff_bytes_per_unit`) **and of every sibling unit merged into the integration branch since contract freeze** (`worktrees.merged_unit_diffs(target_branch, since_ref=contract-freeze commit)`). Sibling diffs are mandatory: already-merged diffs have vanished into the target branch, and without them the DoD §5.3 seeded scenario (two diffs jointly violating an invariant, Tier 1 green) is structurally uncatchable; hunk headers alone cannot check conformance "in substance". The header-only `diff_digest` is reserved for CP-1, whose DoD §3.4 input is a digest by definition. Tier-2 findings land in `audit_findings` and route per DoD §5.2 Resolve.

**CP-1 verdict execution (DoD §3.4 closed set — every verdict must be executable, never silently collapsed):** `continue_session` = re-spawn the Builder with `resume_session` = its last registered `session_id`; `rebuild` = fresh session; `respec` = transition to SPEC; `escalate` = escalation row. A `context_reset` event forbids `resume_session` on the stage's next iteration. If the Builder's route lacks verified resume support (codex — OPEN-3), `continue_session` executes as `rebuild` with an explicit `verdict_downgraded` event — recorded, never silent.

### 3.2 Phase flow

| From | To (guard) |
|---|---|
| PENDING | PLANNING (deps DONE); CANCELLED |
| PLANNING | CONTRACTS_FROZEN (plan + contracts registered & committed); ESCALATED; CANCELLED |
| CONTRACTS_FROZEN | RUNNING (phase-plan.json validated via `artifacts.read_phase_plan` — schema + acyclic DAG, §4 — then stage rows + stage DAG inserted); CANCELLED |
| RUNNING | INTEGRATING (no child stage outside TERMINAL_OK; CANCELLED children only with a registered replacement stage); ESCALATED (contract change request; **any child entered FAILED, or CANCELLED without replacement** — architect decides re-derive vs cancel; a failed child must never wedge the phase in RUNNING forever); AWAITING_HUMAN; CANCELLED |
| INTEGRATING | AWAITING_SIGNOFF (phase merge gates pass — DoD §9.3); RUNNING (gate findings → stage rework); ESCALATED; CANCELLED |
| AWAITING_SIGNOFF | DONE (founder sign-off); RUNNING (changes requested); CANCELLED |
| AWAITING_HUMAN | RUNNING; PLANNING; CANCELLED |
| ESCALATED | PLANNING; RUNNING; AWAITING_HUMAN; FAILED; CANCELLED |
| DONE, FAILED, CANCELLED | ∅ |

### 3.3 Scheduling categories (level-agnostic view, DoD §3.2)

`sched_category(level, state, deps_done)` → WAITING (PENDING, deps unmet) · RUNNABLE (PENDING, deps met) · RUNNING (SPEC/BUILD/VALIDATE/AUDIT/MERGE_GATE · PLANNING/CONTRACTS_FROZEN/RUNNING/INTEGRATING) · BLOCKED (AWAITING_HUMAN, AWAITING_SIGNOFF, ESCALATED) · TERMINAL_OK (DONE) · TERMINAL_FAIL (FAILED, CANCELLED). The scheduler operates **only** on categories + `dag_edges`; per-level step sequences live in `UnitExecutor`s — one fan-out/queue/gate code path for both levels.

---

## 4. Frozen public interfaces (contract-first, DoD §5.1/§5.2 Prevent)

These signatures are the contracts parallel builders code against. Changing one = a contract change request → STOP + escalation to the owning architect. Private helpers (`_`-prefixed) are free.

```python
# ---- models.py ----------------------------------------------------------------
class Level(StrEnum):        """Unit level: PHASE='phase', STAGE='stage'."""
class RiskClass(StrEnum):    """ROUTINE, STRUCTURAL, CRITICAL (config risk_classes keys)."""
class StageState(StrEnum):   """PENDING SPEC BUILD VALIDATE AUDIT AWAITING_HUMAN MERGE_GATE ESCALATED DONE FAILED CANCELLED."""
class PhaseState(StrEnum):   """PENDING PLANNING CONTRACTS_FROZEN RUNNING INTEGRATING AWAITING_SIGNOFF AWAITING_HUMAN ESCALATED DONE FAILED CANCELLED."""
class SchedCategory(StrEnum):"""WAITING RUNNABLE RUNNING BLOCKED TERMINAL_OK TERMINAL_FAIL."""
class Trigger(StrEnum):      """MAX_FIX_ITERATIONS CHURN_THRESHOLD CONTRACT_CHANGE_REQUEST AGENT_DECLARED_FAILURE CONTEXT_BUDGET."""
VALID_STAGE_TRANSITIONS: Mapping[StageState, frozenset[StageState]]   # §3.1 table
VALID_PHASE_TRANSITIONS: Mapping[PhaseState, frozenset[PhaseState]]   # §3.2 table
def sched_category(level: Level, state: str, deps_done: bool) -> SchedCategory:
    """Map a concrete unit state to its level-agnostic scheduling category."""
def utc_now() -> str:                       """ISO 8601 UTC timestamp 'YYYY-MM-DDTHH:MM:SSZ'."""
def new_id(prefix: str) -> str:             """'<prefix>-<12 hex chars>' unique id."""
@dataclass(frozen=True, slots=True)
class Phase:        """id, project, name, state: PhaseState, branch, plan_artifact_id, created_at, updated_at."""
class Stage:        """id, phase_id, name, risk_class, state: StageState, branch, worktree_path, spec_artifact_id, created_at, updated_at."""
class Event:        """seq, unit_level, unit_id, event_type, from_state, to_state, actor, payload: dict, created_at."""
class ArtifactRef:  """id, unit_level, unit_id, kind, repo, path, sha256, git_commit, created_at."""
class ProcessRecord:"""id, unit_level, unit_id, kind, role, cp_id, session_id, pid, cmdline, cwd, state, exit_code, ndjson_log_path, spawned_at, heartbeat_at, ended_at."""
class Escalation:   """id, unit_level, unit_id, trigger, target, payload_artifact_id, event_seq, status, resolution, created_at, resolved_at."""
class Finding:      """id, stage_id, auditor_role, finding_ref, severity, report_artifact_id, status, contest_artifact_id, resolved_by, created_at, updated_at."""
class DecisionRequest: """id, unit_level, unit_id, gate_kind, request_artifact_id, status, answer, answer_artifact_id, created_at, alerted_at, answered_at."""
class TriggerFiring:"""trigger: Trigger, unit_level, unit_id, evidence: dict (the SQL row(s) that fired)."""
class ValidationSummary: """failing: int, passing: int, total: int — parsed from validation-report.json."""
# error taxonomy (all subclass FactoryError(Exception); see §6):
class FactoryError(Exception): ...
class ConfigError(FactoryError): ...;        class MigrationError(FactoryError): ...
class TransitionError(FactoryError): ...;    class IntegrityError(FactoryError): ...
class GitError(FactoryError): ...;           class ProcessError(FactoryError): ...
class ArtifactContractError(FactoryError): ...; class ConsultationBreachError(FactoryError): ...
class NotifyError(FactoryError): ...

# ---- config.py ----------------------------------------------------------------
class ModelRoute(BaseModel):           """cli: Literal['claude','codex','stub']; model: str; mode: Literal['print','interactive']."""
class ConsultationPointCfg(BaseModel): """id, purpose, inputs: list[str], verdicts: list[str], fallback: str, role: str, max_input_bytes: int."""
class FactoryConfig(BaseModel):
    """Typed mirror of factory.config.yaml: factory, projects, models, budgets, escalation, risk_classes,
    economics, consultation_points, founder_channel, process, canon (D-0009). extra='forbid' everywhere."""
def load_config(path: Path) -> FactoryConfig:
    """Parse + validate YAML; cross-checks (fallback∈verdicts; risk_classes roles∈models; budgets.per_stage keys==risk_classes keys); raises ConfigError."""

# ---- db.py ---------------------------------------------------------------------
class Database:
    def __init__(self, path: Path, busy_timeout_ms: int) -> None: """Bind path; no I/O yet."""
    def open(self, *, read_only: bool = False) -> None:
        """Connect (mode=ro when read_only — `cli status`/dashboard reads); PRAGMA journal_mode=WAL,
        synchronous=NORMAL (WAL-safe tradeoff, stated: an OS crash may lose the last committed tx — acceptable
        because git+artifacts lead state and steps are re-runnable), foreign_keys=ON, busy_timeout."""
    def close(self) -> None:   """Close the connection."""
    def migrate(self, migrations_dir: Path) -> list[int]:
        """Apply pending NNNN_*.sql in order, each in its own tx, record in schema_migrations; raises MigrationError."""
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE; yield conn; commit, rollback on exception. THE composition primitive for atomic
        writes. Invariant (§7): the block is synchronous end-to-end — no await inside; raises if a transaction
        is already active on this connection (re-entrancy guard, enforced not assumed)."""
    def read(self) -> sqlite3.Connection:  """Connection for reads outside a write tx."""
# repository functions — pure SQL, no business rules; conn comes from Database.transaction()/read():
def insert_phase(conn, phase: Phase) -> None;          def get_phase(conn, phase_id: str) -> Phase | None
def insert_stage(conn, stage: Stage) -> None;          def get_stage(conn, stage_id: str) -> Stage | None
def list_units(conn, level: Level, states: Sequence[str] = ()) -> list[Phase | Stage]
def set_unit_state(conn, level: Level, unit_id: str, state: str) -> None    # called ONLY by statemachine
def set_stage_worktree(conn, stage_id: str, branch: str, worktree_path: str) -> None
def insert_event(conn, *, unit_level: str, unit_id: str | None, event_type: str, actor: str,
                 from_state: str | None = None, to_state: str | None = None, payload: dict | None = None) -> int
def insert_dag_edge(conn, level: Level, from_id: str, to_id: str) -> None
def deps_done(conn, level: Level, unit_id: str) -> bool
def insert_artifact_ref(conn, ref: ArtifactRef) -> int
def latest_artifact(conn, unit_level: str, unit_id: str, kind: str) -> ArtifactRef | None
def find_artifact_ref(conn, repo: str, path: str, sha256: str) -> ArtifactRef | None   # get-or-create probe for artifacts.register_artifact (CCR-1)
def iter_latest_artifact_refs(conn) -> Iterator[ArtifactRef]               # for integrity check
def insert_process(conn, rec: ProcessRecord) -> int;   def finalize_process(conn, process_id: int, *, state: str, exit_code: int | None, ended_at: str, session_id: str | None = None) -> None
def heartbeat_process(conn, process_id: int, at: str) -> None
def processes_in_state(conn, state: str) -> list[ProcessRecord]
def last_session_id(conn, *, unit_level: str, unit_id: str, role: str) -> str | None   # latest finalized session — continue_session support (CCR-1)
def insert_token_usage(conn, *, process_id: int, unit_level: str, unit_id: str, role: str, model: str,
                       tokens_in: int | None, tokens_out: int | None, cost_usd: float | None,
                       estimated: bool = False) -> None   # estimated=True for usage_missing_policy='estimate' rows (CCR-1)
def unit_token_total(conn, unit_level: str, unit_id: str) -> int
def insert_fix_iteration(conn, stage_id: str, failing_tests: int, report_artifact_id: int | None) -> int
def bump_churn(conn, stage_id: str, file_path: str, region: int) -> int
def insert_consultation(conn, row: Mapping[str, object]) -> int
def insert_escalation(conn, esc: Escalation) -> int;   def resolve_escalation(conn, esc_id: int, resolution: str) -> None
def open_escalation(conn, unit_level: str, unit_id: str, trigger: str) -> Escalation | None
def insert_finding(conn, f: Finding) -> int;           def set_finding_status(conn, finding_id: int, status: str, *, resolved_by: str | None = None, contest_artifact_id: int | None = None) -> None
def findings(conn, stage_id: str, statuses: Sequence[str] = ()) -> list[Finding]
def insert_decision_request(conn, dr: DecisionRequest) -> int
def answer_decision(conn, request_id: int, answer: str, answer_artifact_id: int | None) -> None
def mark_decision_alerted(conn, request_id: int, at: str) -> None   # set alerted_at after successful publish — latency alert must not re-fire every tick (CCR-1)
def pending_decisions(conn, *, unalerted_older_than_h: int | None = None) -> list[DecisionRequest]

# ---- statemachine.py ------------------------------------------------------------
class StateMachine:
    def __init__(self, db: Database) -> None: """Sole authority over unit state columns."""
    def transition(self, level: Level, unit_id: str, to_state: str, *, actor: str, reason: str,
                   payload: dict | None = None,
                   coupled: Callable[[sqlite3.Connection], None] | None = None) -> int:
        """Atomically (one tx, DoD §6): validate against VALID_*_TRANSITIONS, set state, append event,
        run coupled writes (e.g. fix-iteration insert). Returns event seq; raises TransitionError."""

# ---- runner.py -------------------------------------------------------------------
@dataclass(frozen=True)
class AgentResult: """process_id, exit_code, timed_out: bool, killed: bool, declared_failure: bool, result_text: str,
                   session_id: str|None (from the CLI init/result NDJSON line — continue_session support),
                   tokens_in: int|None, tokens_out: int|None, cost_usd: float|None, garbage_lines: int,
                   ndjson_log_path: str, stderr_path: str, duration_ms: int."""
class CliAdapter(Protocol):
    def build_cmd(self, route: ModelRoute, prompt: str, *, system_append: str | None = None,
                  resume_session: str | None = None) -> list[str]:
        """argv for a one-shot NDJSON-streaming run. system_append = canon bundle (D-0009: claude
        `--append-system-prompt`); resume_session = resume that CLI session (claude `--resume <id>`)."""
    def materialize_workspace(self, cwd: Path, system_append: str | None) -> None:
        """Hook for CLIs without a system-prompt flag (codex: write AGENTS.md into cwd before spawn, D-0009)."""
    def parse_line(self, obj: dict) -> StreamItem: """Classify one NDJSON object: init|text|result|usage|other."""
ADAPTERS: Mapping[str, CliAdapter]       # 'claude', 'codex', 'stub' — selected by config models.<role>.cli
class AgentRunner:
    def __init__(self, cfg: FactoryConfig, db: Database) -> None: """Only LLM spawn path in the factory."""
    async def run_agent(self, role: str, prompt: str, *, unit_level: str, unit_id: str, cwd: Path,
                        kind: str = "agent", cp_id: str | None = None, timeout_s: int | None = None,
                        resume_session: str | None = None) -> AgentResult:
        """Spawn per config models[role] in its OWN process group (start_new_session=True; Linux backstop
        PR_SET_PDEATHSIG=SIGKILL via preexec_fn — agent trees must die with the orchestrator, not run
        unsupervised until resume). Canon bundle resolved from cfg.canon by role class (D-0009) and passed to
        the adapter. Tagging enforced at this boundary (precondition of the §2 creep scan): kind='consultation'
        ⇔ cp_id set ⇔ role ∈ registry consultation roles; kind='agent' ⇒ role ∈ config pipeline roles — else
        ConsultationBreachError. Register process; stderr → <process_id>.stderr file (inherited fd, no drain
        task); stream NDJSON line-tolerantly to log file; heartbeat throttled to process.heartbeat_min_interval_s;
        capture session_id; enforce timeout (terminate->kill grace from config, signals to the process GROUP);
        finalize registry+token_ledger in one tx; detect declared-failure sentinel. Raises ProcessError only on
        spawn impossibility."""
    async def kill_running(self) -> int: """Kill (by process group) every process_registry row in 'spawned'/'running' whose pid is alive (recovery). Returns count."""

# ---- artifacts.py -----------------------------------------------------------------
STAGE_ARTIFACTS: Mapping[str, str]
"""kind -> filename under _factory/stages/<stage_id>/: spec='spec.md', build_notes='build-notes.md',
validation_report='validation-report.md', validation_sidecar='validation-report.json',
audit_report='audit-<role>.md', declared_failure='_DECLARED_FAILURE.md',
contract_change_request='_CONTRACT_CHANGE_REQUEST.md'. Layout is a frozen contract (referenced by role
prompts), not a tunable — changing it is a migration, not a config edit."""
def unit_artifact_dir(root: Path, level: Level, unit_id: str) -> Path:
    """_factory/stages/<id>/ or _factory/phases/<id>/ under the given repo/worktree root."""
PHASE_ARTIFACTS: Mapping[str, str]
"""kind -> filename under _factory/phases/<phase_id>/: phase_plan='phase-plan.md',
phase_plan_sidecar='phase-plan.json'; contracts live under _factory/contracts/. Same frozen-contract
status as STAGE_ARTIFACTS."""
def sha256_file(path: Path) -> str: """Streaming sha256 hex digest; raises IntegrityError if unreadable."""
def register_artifact(conn, *, unit_level: str, unit_id: str, kind: str, repo: str,
                      repo_root: Path, path: Path, git_commit: str | None) -> ArtifactRef:
    """Hash file and GET-OR-CREATE the artifact_refs row (path+hash only, never content) in the caller's tx:
    on (repo, path, sha256) conflict return the EXISTING ref — byte-identical re-registration is normal
    operation (unchanged sidecar across fix iterations, crash-replayed steps) and must never abort the
    enclosing transition; per-iteration linkage lives in fix_iterations.report_artifact_id."""
class PhasePlan(BaseModel): """Schema of phase-plan.json: stages[{id, name, risk_class, acceptance}],
    dag_edges[[from_id, to_id]]; extra='forbid'."""
def read_phase_plan(path: Path, risk_classes: Collection[str]) -> PhasePlan:
    """Validate the LLM-produced plan BEFORE any scheduler ingestion: unique stage ids, risk_class ∈
    risk_classes, every edge endpoint declared, DAG acyclic (toposort). Malformed or cyclic →
    ArtifactContractError → escalation — same contract as the validation sidecar; an unvalidated cyclic plan
    would leave all units WAITING forever with the watchdog green (Doctrine §20's silent slow death)."""
def read_validation_sidecar(path: Path) -> ValidationSummary:
    """Parse validator's machine-readable JSON sidecar; raises ArtifactContractError (no guessing, Doctrine §7)."""
def detect_sentinels(unit_dir: Path) -> list[str]:
    """Return present sentinel kinds ('declared_failure','contract_change_request') — mechanical detection;
    archived sentinels (`*.resolved-<id>.md`, §5.4) do not match."""
def verify_integrity(db: Database, repo_roots: Mapping[str, Path]) -> IntegrityReport:
    """DoD §12.A2: every latest artifact ref of a NON-TERMINAL unit resolves and sha256 matches; recorded
    git_commit exists. Resolution precedence: stage worktree_path if present → `git cat-file <commit>:<path>`
    → integration branch. Terminal units (FAILED/CANCELLED, merged DONE) downgrade mismatches to logged
    warnings — their worktrees/branches are legitimately gone. Returns report; never repairs silently."""

# ---- worktrees.py ------------------------------------------------------------------
@dataclass(frozen=True)
class Tier1Result: """passed: bool, rebase_conflict: bool, conflict_payload: str, tests_failed: bool, test_output_path: str | None."""
async def run_git(*args: str, cwd: Path | None = None) -> tuple[int, str, str]:
    """Run git, return (exit_code, stdout, stderr); never raises on nonzero exit."""
async def commit_paths(repo_root: Path, paths: Sequence[Path], message: str, *, trailers: Mapping[str, str]) -> str | None:
    """git add+commit with trailer block (moved here from artifacts.py — the git-exec primitive has exactly one
    home); refuses non-worktree-root cwd and branch mismatch; None if nothing to commit; GitError otherwise."""
class WorktreeManager:
    def __init__(self, cfg: FactoryConfig) -> None:
        """Owns worktree dirs per projects.<id>.worktrees_dir and one asyncio.Lock PER TARGET BRANCH: the whole
        rebase→test→merge sequence and worktree add/remove run under it — two stages gating concurrently would
        otherwise merge a state never rebased/tested against the post-sibling HEAD (silently voiding DoD §5.1)
        and contend on git index/ref locks."""
    async def create(self, repo_root: Path, unit_id: str, branch: str, base_branch: str, *, new_branch: bool = True) -> Path:
        """Idempotent `git worktree add` (`-b` when new_branch; else checkout of the existing branch — used for
        Validator scratch worktrees, §3.1). Runs `git worktree prune` first (crash-orphaned half-registrations
        are cleaned, not escalated); verifies an existing path is a registered worktree on the expected branch,
        else GitError (never mask inconsistent state)."""
    async def remove(self, repo_root: Path, worktree: Path) -> None: """`git worktree remove --force`; GitError on failure."""
    async def heal_git_state(self, worktree: Path) -> list[str]:
        """Mechanically abort in-progress rebase/merge/cherry-pick (`.git/rebase-merge`, `rebase-apply`,
        `MERGE_HEAD` present) left by a crash; verify expected branch; return actions taken. Run by
        Scheduler.recover() and as tier1_gate/integrate preamble — deterministic, no judgment: a SIGKILL
        mid-gate must resume mechanically, never degrade into a human escalation."""
    async def tier1_gate(self, worktree: Path, target_branch: str, test_cmd: list[str], timeout_s: int) -> Tier1Result:
        """DoD §5.1, purely mechanical, under the target-branch lock: heal_git_state; rebase onto target — on
        conflict abort rebase and return conflict payload; else run the full test suite. The CALLER
        (StageExecutor, which owns db access — worktrees never imports db) registers the suite as the
        kind='tests' process and, after a successful rebase, re-resolves the stage's artifact_refs.git_commit
        at the new branch head (rebase rewrote history; old shas survive only in reflog) — mechanical: same
        path + same sha256. No agent judgment."""
    async def integrate(self, repo_root: Path, branch: str, target_branch: str) -> str:
        """Fast-forward/no-ff merge of a gated branch into target, under the same target-branch lock, with a
        `Stage-Id:` trailer in the merge commit (keys merged_unit_diffs); asserts target HEAD unchanged since
        the gate's rebase (defense in depth — else caller loops back to rebase); returns merge commit sha;
        GitError on failure."""
    async def diff_digest(self, worktree: Path, target_branch: str, max_bytes: int) -> str:
        """Bounded digest (stat + hunk headers) — CP-1 input ONLY (DoD §3.4); never sufficient for Tier 2."""
    async def full_diff(self, worktree: Path, target_branch: str, max_bytes: int) -> str:
        """Full unified diff (bodies, size-bounded) of the gating unit vs target — Tier-2 input (§3.1)."""
    async def merged_unit_diffs(self, repo_root: Path, target_branch: str, since_ref: str, max_bytes_per_unit: int) -> Mapping[str, str]:
        """unit_id -> full diff for every unit merged into target_branch since since_ref (the contract-freeze
        commit), keyed by merge-commit Stage-Id trailers — Tier-2 sibling visibility (§3.1)."""

# ---- thresholds.py ----------------------------------------------------------------
class ThresholdEvaluator:
    def __init__(self, db: Database, cfg: FactoryConfig) -> None: """Binds §8 config values to SQL."""
    def record_validation(self, conn, stage_id: str, summary: ValidationSummary, report_artifact_id: int | None) -> int:
        """Insert next fix_iterations row inside the caller's tx (coupled with the VALIDATE transition); returns iteration."""
    def record_churn(self, conn, stage_id: str, diff_text: str) -> None:
        """Parse unified-diff hunk headers; bump churn per (file, start_line // churn_region_lines) bucket."""
    def evaluate(self, stage: Stage) -> list[TriggerFiring]:
        """Run the §2 trigger SQL set; return firings not yet covered by an open escalation. Pure reads."""

# ---- consultation.py ---------------------------------------------------------------
@dataclass(frozen=True)
class Verdict: """cp_id, value: str (∈ closed set), rationale: str, fallback_used: bool, consultation_id: int."""
class Consultor:
    def __init__(self, cfg: FactoryConfig, db: Database, runner: AgentRunner) -> None:
        """Registry = cfg.consultation_points; CP-1 is the only registered point in MVP."""
    async def consult(self, cp_id: str, *, unit_level: str, unit_id: str, inputs: Mapping[str, str]) -> Verdict:
        """Pure-function consultation (DoD §2.1): unknown cp_id -> log 'cp_breach_attempt' event then raise
        ConsultationBreachError; assemble bounded input (≤ max_input_bytes, input keys must equal the registry's
        declared inputs); call runner with the registry role; strict-parse exactly one JSON object against a
        pydantic model with Literal[verdicts]; invalid/ambiguous (≠1 object, unknown verdict, empty rationale)
        -> deterministic fallback verdict with fallback_used=True; always log consultations row + raw stream."""

# ---- scheduler.py ------------------------------------------------------------------
class UnitExecutor(Protocol):
    level: Level
    async def execute(self, unit_id: str) -> None:
        """Drive one unit from its current state until BLOCKED or terminal; every step: run agent, register
        artifacts, evaluate thresholds first, CP-1 only when thresholds do not decide, transition."""
class StageExecutor:   # implements UnitExecutor(level=STAGE): SPEC→…→MERGE_GATE conveyor (§3.1)
    def __init__(self, db: Database, sm: StateMachine, cfg: FactoryConfig, runner: AgentRunner,
                 wt: WorktreeManager, thresholds: ThresholdEvaluator, consultor: Consultor,
                 notify: NtfyPublisher) -> None: """Wires the stage conveyor; no policy outside config."""
class PhaseExecutor:   # implements UnitExecutor(level=PHASE): plan→freeze contracts→fan out stages→integrate (§3.2)
    def __init__(self, db: Database, sm: StateMachine, cfg: FactoryConfig, runner: AgentRunner,
                 wt: WorktreeManager, notify: NtfyPublisher) -> None:
        """Ingests phase-plan.json strictly via artifacts.read_phase_plan (schema + acyclicity validated BEFORE
        the CONTRACTS_FROZEN→RUNNING transition; failure = ArtifactContractError → escalation) into
        stages+dag_edges. An LLM-produced plan is never trusted unvalidated (Doctrine §7)."""
class Scheduler:
    def __init__(self, db: Database, sm: StateMachine, cfg: FactoryConfig,
                 executors: Mapping[Level, UnitExecutor], notify: NtfyPublisher) -> None:
        """Level-agnostic loop over sched categories + dag_edges; max process.max_parallel_agents concurrent units."""
    def recover(self) -> RecoveryReport:
        """Crash recovery (DoD §12.A2), only under the cli single-instance flock. Touches the liveness file at
        entry and periodically during the scan (a healthy restart must not page the watchdog). Steps:
        (a) orphan sweep — kill by PROCESS GROUP, mark 'orphaned' + event; (b) git healing —
        worktrees.heal_git_state on every known worktree + the integration checkout, `git worktree prune`;
        then worktree canonicalization: `git status --porcelain` per unit worktree — if dirty (an orphan kept
        writing until killed), save the dirty diff to ndjson_log_dir as evidence + event, hard-reset +
        `clean -fd` to the step's base commit (committed git state is the ONLY canonical step input — the
        idempotency precondition of §5.5d); (c) verify_integrity — abort start on a non-terminal-unit mismatch;
        BLOCKED/RUNNING units re-enter the queue and resume from SQLite state + on-disk artifacts."""
    async def run_forever(self) -> None:
        """Main loop, tick = process.loop_tick_s: refresh liveness file + pidfile, dispatch RUNNABLE units,
        reap finished tasks, fire decision-latency alerts, and run the STALL DETECTOR — non-terminal units
        exist, nothing RUNNABLE/RUNNING, and no open decision_request/escalation → 'alert' event + ntfy
        (a wedged factory must page, never idle green — Doctrine §20). One asyncio TaskGroup; ALL db writes
        happen on this loop thread; notification I/O only via the async NtfyPublisher (never blocks the loop)."""
    async def run_until_blocked(self) -> None: """Same loop; returns when nothing is RUNNABLE/RUNNING (tests, criterion runs)."""

# ---- notify.py ---------------------------------------------------------------------
class NtfyPublisher:
    def __init__(self, cfg: FactoryConfig) -> None: """Binds founder_channel.ntfy server/topic/priorities/timeout_s."""
    async def publish(self, title: str, *, link: str | None = None, priority: str = "default") -> None:
        """POST to ntfy: title + deep link only, never artifact content (D-0004). The blocking HTTP call runs
        off-loop (asyncio.to_thread) with founder_channel.ntfy.timeout_s — a hung ntfy connection must never
        stall the scheduler loop or the liveness heartbeat (§7); raises NotifyError on timeout/HTTP error."""
def dashboard_link(cfg: FactoryConfig, fragment: str) -> str: """Deep link into the dashboard for a unit/decision."""

# ---- watchdog.py -------------------------------------------------------------------
def check_once(cfg: FactoryConfig) -> bool:
    """Pid alive (pidfile + cmdline match) AND liveness file mtime fresher than staleness_threshold_s; a
    pidfile younger than staleness_threshold_s counts as startup/recovery grace (recover() also touches the
    liveness file, so a healthy restart never pages the founder). On failure publish max-priority ntfy;
    silence means healthy (DoD §9). Reads files only, never the DB."""
def main(argv: Sequence[str] | None = None) -> int: """Entry for cron/systemd timer: load config, check_once, exit 0/1."""

# ---- cli.py ------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    """Operator entry. init: validate config, create db + migrate, env sanity check. run / resume: FIRST
    acquire an exclusive flock on process.pid_file and hold it for the process lifetime — if held, or the
    recorded pid+cmdline is alive, abort with a clear message (a second instance would orphan-sweep the live
    instance's agents, double-write the DB behind busy_timeout, and mask watchdog death detection); only then
    recover() + run_forever() (resume: run_until_blocked()). status [--json] [--write]: render generated view
    from a READ-ONLY db connection (mode=ro — legitimately concurrent with a live orchestrator, §2) + git
    (non-canonical, Doctrine §9). decide <request_id> <option>: emergency-fallback answer path (DoD §9
    plumbing; the expected founder path is the dashboard answer endpoint, §1)."""
```

---

## 5. Process-runner lifecycle

1. **Spawn**: `run_agent` enforces kind/cp_id/role tagging (§2 creep-scan precondition), resolves `config.models[role]` + the role-class canon bundle (D-0009) → adapter builds argv (`claude --model <m> --output-format stream-json --verbose --append-system-prompt <canon> [--resume <session_id>] -p <prompt>`; codex equivalent per OPEN-3 with AGENTS.md materialized into cwd; `stub` for tests). The child starts in its **own process group** (`start_new_session=True`; Linux backstop `PR_SET_PDEATHSIG=SIGKILL` via preexec_fn) with stderr redirected at spawn to `<ndjson_log_dir>/<process_id>.stderr` (inherited fd — no drain task, crash-safe evidence; stderr is never PIPEd without a drain — a full 64KB pipe deadlocks the child into a spurious timeout — and never merged into stdout, which would corrupt the NDJSON stream). Insert `process_registry` row (`spawned`) and open `<ndjson_log_dir>/<process_id>.ndjson` **before** exec; flip to `running` with pid after exec. No subprocess without timeout + terminate/kill (harvested invariant).
2. **Stream**: read stdout line-by-line with `create_subprocess_exec(limit=process.ndjson_max_line_bytes)`; on overrun (asyncio's readline raises before the newline) consume to the next newline, append the truncated bytes to the log with a truncation marker, `garbage_lines += 1`, **continue** — an oversized line must never abort the stream and lose the result line. Every raw line appended to the log file first (crash-safe evidence), then parsed: `json.loads` failure → `garbage_lines += 1`, continue (line-tolerant, harvested); `heartbeat_at` updated at most once per `process.heartbeat_min_interval_s` (nothing consumes sub-second heartbeats — timeouts are deadline-based, the watchdog reads the liveness file — so per-line commits are pure write amplification); `init`/`result` lines yield `session_id`; `result` yields result text + usage/cost. Missing usage → `tokens_* = NULL` + event `usage_missing`, made mechanically consequential by `budgets.usage_missing_policy` (§2 — visible AND enforced, not guessed).
3. **Exit / timeout / kill**: normal exit → `exited` + exit_code; deadline (`timeout_s` or `process.agent_timeout_s`) → SIGTERM, wait `process.terminate_grace_s`, SIGKILL, wait `process.kill_grace_s` → `timed_out` — all signals to the **process group** (`os.killpg`): agent CLIs spawn their own subprocess trees (bash tools, test runners) which must die with them, or a "killed" agent's test command keeps mutating the worktree after the registry says killed. Finalize registry + `token_ledger` in one tx; nonzero-exit/timeout escalation payloads include the stderr tail.
4. **Post-conditions (mechanical)**: `artifacts.detect_sentinels(unit_dir)` — `_DECLARED_FAILURE.md` present (role-prompt contract: an agent that cannot proceed writes this artifact instead of guessing, Doctrine §7) → event `declared_failure` → §8 trigger `agent_declared_failure` → escalation, **never** retried blindly. `_CONTRACT_CHANGE_REQUEST.md` → immediate STOP + escalation to owning architect. Expected step artifact missing/unparseable → `ArtifactContractError` path (§6), not a retry. **Sentinel lifecycle**: the escalation row records the firing `events.seq` (`escalations.event_seq` — the §2 dedup cursor); at resolution the control plane archives the sentinel (rename to `<name>.resolved-<escalation_id>.md`, committed) **before** the resolving DB transaction — so post-rework re-detection of a stale sentinel cannot re-escalate indefinitely, while a *new* sentinel written after rework is a new event and fires again by design.
5. **Crash recovery (orchestrator killed mid-stage, DoD §12.A2)**: watchdog (cron, root of trust) detects stale liveness → max-priority ntfy within `check_interval_s`. On `run`/`resume` (under the cli single-instance flock — recovery against a *live* instance would sweep its agents as orphans), `Scheduler.recover()` touches the liveness file at entry and periodically while scanning, then:
   a. orphan sweep — every `process_registry` row in `spawned`/`running`: pid alive with matching cmdline → kill its **process group** (its supervisor is gone; steps are re-runnable from artifacts); mark `orphaned` + event;
   b. git healing — `worktrees.heal_git_state` on every known worktree and the integration checkout (a SIGKILL mid-rebase/mid-merge leaves `.git/rebase-merge`/`MERGE_HEAD`; aborting it is deterministic, never an escalation) + `git worktree prune`; then worktree canonicalization — dirty unit worktrees (orphans wrote during the death→resume window) → dirty diff saved to `ndjson_log_dir` as evidence + event, hard-reset + `clean -fd` to the step's base commit;
   c. integrity check — `artifacts.verify_integrity` (resolution precedence + terminal-unit scoping per §4): a non-terminal-unit mismatch → `IntegrityError`, start **aborted** with ntfy alert (no silent repair);
   d. resume — unit states are already consistent (every step's artifact file and git commit land **before** the synchronous DB transaction that records them, §7); units in RUNNING-category states re-enter the queue and their executor re-runs the current step from disk against the canonical worktree state restored in (b) — the explicit idempotency precondition of the at-least-once re-run model; AWAITING_* stay blocked. Sessions are disposable (resumable only when `continue_session` says so, §3.1); git+SQLite are the memory.

---

## 6. Failure model — fail-explicit everywhere (Doctrine §7)

No silent retries anywhere: every retry-shaped behavior is a counted, persisted loop (`fix_iterations`) governed by §8 thresholds. Every failure path persists, atomically: an `events` row (+ full traceback file under `ndjson_log_dir`, path in payload) and, where unit-scoped, an `escalations` row + payload artifact.

| Error | Meaning | Handling (what is persisted) |
|---|---|---|
| `ConfigError` / `MigrationError` | invalid config / schema | abort startup, nonzero exit (no factory without valid config) |
| `TransitionError` | illegal transition attempt = control-plane bug | caught at executor boundary → unit ESCALATED (`trigger='internal_error'`, traceback artifact); siblings keep running |
| `IntegrityError` | artifact ref unresolved / hash mismatch | abort start or stop dependent subtree; ntfy alert; event with mismatch list |
| `GitError` | worktree/commit/merge mechanics failed | event + unit ESCALATED with raw git output as payload artifact |
| `ProcessError` | spawn impossible (CLI missing, etc.) | event + unit ESCALATED; registry row finalized `killed` |
| `ArtifactContractError` | agent broke an artifact contract (missing sidecar, malformed) | event + escalation — treated as agent failure, never parsed "best effort" |
| `ConsultationBreachError` | LLM call outside registry attempted | `cp_breach_attempt` event (the DoD §13 governance scan), raise — caller bug |
| `NotifyError` | ntfy unreachable / timed out (`founder_channel.ntfy.timeout_s`) | event `alert_delivery_failed`; state unchanged (decisions remain pending; dashboard still shows them) |
| timeout / nonzero exit / garbage stream | agent process failure | `AgentResult` flags persisted in registry (+ stderr tail in any escalation payload); executor routes via thresholds→CP-1, never auto-rerun |
| scheduler stall (non-terminal units, nothing runnable/running, no open decision/escalation) | wedged factory — e.g. plan defect | `alert` event + ntfy from the §4 stall detector; never a silent green idle |
| agent declared failure | explicit inability | sentinel → escalation (DoD §8), routed up, never retried blindly |

Orchestrator-scoped fatal errors (DB corruption, `IntegrityError` at start) → clean shutdown + max-priority ntfy; unit-scoped errors are contained at the `UnitExecutor` boundary so parallel siblings continue (DoD §9: block only the dependent subtree).

---

## 7. Concurrency model

**asyncio, single process, single OS thread** (Python 3.12 `asyncio.TaskGroup` + `asyncio.subprocess`). Justification (Doctrine §16): asyncio is the standard Python answer to many concurrent streaming subprocesses — readline-loops over dozens of agent stdouts multiplex on one event loop with structured cancellation for timeout/kill, which threads only match with one reader thread per process plus shared-state locking. The single-SQLite-writer constraint (DoD §6) is satisfied by construction: all DB access happens on the loop thread through synchronous `sqlite3` with `BEGIN IMMEDIATE` — transactions are sub-millisecond at our row counts, so blocking the loop is cheaper than the thread-hop of `aiosqlite` (divergence from the old factory, justified here). sync+selectors would re-implement asyncio's subprocess transport by hand for no gain. CPU-bound work is absent (hashing the odd file is fine inline; agents burn their own processes). Parallelism is capped by `process.max_parallel_agents`, an economics knob (5-hour rolling subscription window, environment audit), not a technical limit.

Two invariants make the shared-connection model safe — stated here and **enforced**, not assumed:
1. **No await inside a transaction.** `Database.transaction()` blocks are synchronous end-to-end (the re-entrancy guard raises otherwise — a sibling task issuing `BEGIN IMMEDIATE` on the same connection at an interior await is a production-only flake). The fixed step sequence is: write artifact file → `await worktrees.commit_paths(...)` → **one synchronous tx** (register_artifact + coupled writes + transition).
2. **No blocking network I/O on the loop.** ntfy publishes via `asyncio.to_thread` with `founder_channel.ntfy.timeout_s` (§4 notify) — the "sub-millisecond transactions" claim covered SQLite only, never HTTP. It is kept honest on the SQLite side by `synchronous=NORMAL` + heartbeat throttling (§2/§5.2). Merge gates serialize on per-target-branch locks (§4 worktrees), which bounds gate concurrency without blocking the loop (the suite runs as a subprocess).

---

## 8. Test strategy

**Unit tests** (`tests/unit/test_<module>.py`, pytest, tmp-path DB + minimal `FactoryConfig` fixtures from `tests/conftest.py`): models — transition-table closure properties (terminals empty, all states reachable); config — golden load of the real `factory.config.yaml`, rejection of unknown/missing keys and bad cross-refs; db — migrations idempotent, monotonic event seq, partial-unique open-escalation index, consultation-tagging CHECK, transaction re-entrancy guard; statemachine — legal/illegal transitions, atomicity of coupled writes (fault injection mid-tx → rollback of both); thresholds — each §8 trigger as a pure SQL fixture, incl. the non-decreasing window at **n+1 consecutive non-decreasing iterations** (the naive LAG form fails exactly there), re-arm after a resolved escalation, churn buckets, and all-NULL-usage budgets; artifacts — hash/registration incl. byte-identical re-registration returning the existing ref (and crash-replay re-registration), sidecar contract rejection, phase-plan schema + cycle rejection, integrity mismatch detection + terminal-unit downgrade; worktrees — real temp git repos: idempotent create (+ prune of half-registered leftovers), wrong-branch refusal, rebase-conflict payload, failing test suite, heal_git_state on wedged rebase/merge, gate-lock serialization of two concurrent gates; consultation — valid verdict, invalid JSON → fallback, unknown cp_id → breach; runner — incl. oversized-line survival (stream continues, result line parsed), tagging enforcement, process-group kill; notify/watchdog — see below; scheduler — DAG ordering, parallel cap, level-agnosticism (same loop drives a fake phase + fake stages), stall detector, phase transition on a child stage entering FAILED; cli — second-instance flock refusal.

**Stub agent** (`tests/stub_agent.py`, executable; selected via test config `models.<role>.cli: stub` + `process.stub_agent_path`): emits scripted NDJSON per a scenario file/env var:
`success` (init+text+result with usage) · `persistent_failure` (as validator: writes validation-report.json with non-decreasing `failing`) · `declared_inability` (writes `_DECLARED_FAILURE.md`, clean exit) · `timeout` (sleeps past deadline) · `garbage` (non-JSON lines interleaved, oversized line — asserts the §5.2 truncate-and-continue semantics) · `invalid_verdict` (CP role: JSON outside the closed set) · `valid_verdict:<value>` (CP role: well-formed verdict from the closed set + rationale — drives CP-1 deterministically, e.g. B7's pre-threshold iterations) · `crash` (nonzero exit mid-stream).

**Integration tests** (`tests/integration/`) mapping to DoD §12:
- B7 escalation fires: stub `persistent_failure` ×`max_fix_iterations`, with CP-1 stubbed `valid_verdict:rebuild` at pre-threshold iterations (thresholds do not decide before iteration n, so the loop must pass CP-1 deterministically) → escalation row + ntfy stub called, no human input; plus the **n+1-iterations variant** (trigger must still fire — guards the corrected §2 SQL).
- B9 consultation contract: CP-1 happy path returns schema-valid verdict; `invalid_verdict` scenario → fallback `escalate` engaged + `fallback_used=1` logged.
- A2 restart integrity: run a scripted stage, SIGKILL the orchestrator between steps, re-run `resume` → orphans killed, integrity green, stage completes; corrupt one artifact byte → `IntegrityError` abort. Variants: SIGKILL **mid-step** while a long-running stub streams (assert: registry row → 'orphaned', process group actually dead, dirty-worktree reset evidence written, stage completes on resume) and SIGKILL **mid-MERGE_GATE** (during rebase) / **mid-integrate** (assert heal_git_state aborts the wedged state and the gate re-runs mechanically — no escalation).
- A5 failure honesty: `declared_inability` → escalation routed up, zero retries in `process_registry`.
- §5.3 / B8 semantic gate: harness-level test with a stub Integration Validator returning a seeded finding → resolution loop (comply path + contest path) completes — this proves **routing only**, never input sufficiency. **B8 is marked done only when the real seeded-conflict fixture** (two stages, Tier 1 green, shared invariant broken — built with real agents at criterion time, kept as a permanent regression fixture, DoD §5.3) **passes through the full §3.1 Tier-2 input contract, sibling merged-unit diffs included** — B8 is the hard gate blocking DoD §12.A6.
- Tier-1 gate: seeded textual conflict → conflict payload routed to owning unit; seeded failing suite → merge blocked.

---

## 9. Build plan — waves with file-level disjointness

Contract = §4 frozen interfaces; builders never edit files outside their row; every wave ends with non-executor verification in clean context (Doctrine §4, D-0008). `tests/conftest.py` is owned by wave 1 and **frozen with it** — a shared append-only file edited by concurrent builders is exactly the Tier-1 textual-conflict generator that file disjointness exists to prevent; later builders define any additional fixtures locally in their own test modules, keeping every wave's file set strictly disjoint.

| Wave | Builder | Owns files |
|---|---|---|
| 1 (foundations, single builder — everything depends on it) | F1 | `src/sf_factory/models.py`, `config.py`, `db.py`, `migrations/0001_init.sql`, `tests/conftest.py`, `tests/unit/test_models.py`, `test_config.py`, `test_db.py`, pyproject dep additions |
| 2 (parallel, against frozen wave-1) | B1 | `statemachine.py`, `thresholds.py`, `tests/unit/test_statemachine.py`, `test_thresholds.py` |
| 2 | B2 | `runner.py`, `tests/stub_agent.py`, `tests/unit/test_runner.py` |
| 2 | B3 | `artifacts.py`, `worktrees.py`, `tests/unit/test_artifacts.py`, `test_worktrees.py` |
| 2 | B4 | `notify.py`, `watchdog.py`, `tests/unit/test_notify.py`, `test_watchdog.py` |
| 3 (parallel, against frozen 1+2) | B5 | `consultation.py`, `tests/unit/test_consultation.py` |
| 3 | B6 | `scheduler.py` (incl. `StageExecutor`, `PhaseExecutor`, `recover`), `tests/unit/test_scheduler.py` |
| 3 | B7 | `cli.py`, `tests/unit/test_cli.py` |
| 4 (single builder — spans modules) | I1 | `tests/integration/*` (§8 scenarios incl. SIGKILL-resume), wiring fixes only via escalation, no interface edits |

Each wave's merge gate: full `uv run pytest` + ruff on the integration branch (the factory eats its own Tier-1 dog food). Interface change needed mid-build = contract change request → STOP + re-freeze by the architect (DoD §5.1). The dashboard module is scheduled after wave 4 in its own design slice (DoD §16.3).

---

## 10. Proposed config additions (require founder/D-entry approval — Doctrine §14)

All referenced by key in §2/§4/§5; none exist yet in `factory.config.yaml`. Proposed defaults:
`process.max_parallel_agents: 4` · `process.liveness_file: .factory/liveness` · `process.pid_file: .factory/orchestrator.pid` (also the single-instance flock target) · `process.db_busy_timeout_ms: 5000` · `process.terminate_grace_s: 10` · `process.kill_grace_s: 5` · `process.ndjson_max_line_bytes: 1048576` · `process.test_suite_timeout_s: 1800` · `process.loop_tick_s: 5` (scheduler tick / liveness-refresh interval; documented relation: `founder_channel.watchdog.staleness_threshold_s` ≥ 10× this — an arbitrary implementer tick must not silently set watchdog false-alarm behavior) · `process.heartbeat_min_interval_s: 1` · `process.tier2_max_diff_bytes_per_unit: 300000` · `escalation.churn_region_lines: 40` · `escalation.max_context_resets: 1` (consumed by the §2 context_budget trigger — was hardcoded prose) · `budgets.usage_missing_policy: estimate` (`estimate` = logged-stream-bytes/4 into the ledger with `estimated=1`; alternative `escalate_after`) · `budgets.usage_missing_max_per_stage: 3` · `founder_channel.ntfy.timeout_s: 10` · `consultation_points[].max_input_bytes: 200000` · `projects.erp.integration_branch: main` · `projects.erp.worktrees_dir: <workspace>/.worktrees` · `projects.erp.test_command: <OPEN-2>`.

## OPEN questions

- **OPEN-1 (config additions):** Do you approve the §10 new config keys with the proposed default values (one decision-log entry), or amend any value? Owner: founder; trigger: before wave 1 freezes `config.py`.
- **OPEN-2 (Tier-1 test command):** What is the canonical full-test-suite command for the ERP workspace merge gate (`projects.erp.test_command`)? Not derivable: the workspace is created only at Etapa 5 (Django+PostgreSQL stack suggests e.g. `uv run pytest`, unconfirmed). Owner: founder/Main Architect; trigger: before the first real BUILD stage; integration tests use a stub command meanwhile.
- **OPEN-3 (codex adapter):** Exact `codex exec` non-interactive JSON/NDJSON flagset, whether it reports token usage, and whether it supports session resume — the environment audit verified codex auth only, not its streaming format. Owner: wave-2 runner builder; trigger: smoke test before freezing the codex `CliAdapter`; until then cross-model roles are exercised via the stub. **Hard gates (added in review):** until codex usage reporting is verified, codex-routed roles run in budget-enforced stages only under `usage_missing_policy: estimate` (never budget-exempt); until codex resume is verified, `continue_session` on a codex-routed builder executes as `rebuild` with an explicit `verdict_downgraded` event (§3.1 — never silent).
- **OPEN-4 (interactive mode out of runner scope):** `models.main_architect.mode: interactive` — the runner implements `print` mode only; Main Architect/Intake PTY sessions remain operator-driven outside the orchestrator in MVP (DoD §3.3 marks them PTY/interactive; no DoD criterion requires orchestrated PTY). Confirm this scoping — **and confirm that orchestrator-spawned Decision Sessions (DoD §9) land in the dashboard design slice** (per the §1 boundary: read views + the single decision-answer endpoint), not in this runner. Owner: founder; trigger: this design's review.
- **OPEN-5 (validation sidecar contract):** `fix_iterations.failing_tests` requires a machine-readable count, so the Validator role prompt must mandate the `validation-report.json` sidecar (`{failing, passing, total}`); missing/malformed sidecar = `ArtifactContractError` → escalation. Confirm this addition to the Validator role-prompt contract. Owner: Main Architect (role-prompt author); trigger: before the first VALIDATE step.

---

## Review log

**CCR-1 (contract change request #1), 2026-06-10 — approved, v1.1→v1.2.** Wave-1 builder built strictly as-frozen and STOPped on four additive §4↔§2 freeze gaps (DoD §5.2 Prevent working as designed): `Escalation.event_seq` (sentinel dedup cursor must be writable/readable), `ProcessRecord.session_id` + `finalize_process(session_id=…)` + `db.last_session_id(…)` (continue_session must survive restarts), `insert_token_usage(estimated=…)` (usage_missing_policy='estimate' must be writable), `db.mark_decision_alerted(…)` (latency alert must not re-fire every tick). Plus: FactoryConfig docstring now enumerates `canon` (D-0009; golden load requires it under extra='forbid'); `db.find_artifact_ref(…)` added so artifacts.register_artifact's get-or-create keeps all SQL in db.py. Decision log D-0012.

Adversarial review, 2026-06-10 — two independent reviewers, both `approve_with_fixes`; 36 findings total (2 critical, 21 major, 13 minor). Disposition in v1.1: **all 36 applied, 0 rejected.** The empirically verifiable claims (LAG-over-full-set trigger miscount, missing `escalations.payload_json` column, NULL-propagating budget SUM, WAL-pragma no-op inside a migration tx, D-0009 as a ratified runner requirement, absent ntfy timeout key) were re-verified against the DDL, sqlite3 semantics, `docs/decision-log.md`, and `factory.config.yaml` before applying. Neither critical finding conflicted with the DoD itself — both (Tier-2 input contract, single-instance enforcement) were design defects relative to it, fixed in the DoD's direction; no DoD amendment required. Overlapping findings merged into single fixes: max_fix_iterations SQL, sentinel dedup cursor, crash-time git healing, continue_session executability, ntfy timeout, artifact get-or-create, read-path rule.
