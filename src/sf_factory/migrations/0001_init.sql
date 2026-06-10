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
