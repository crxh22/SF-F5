-- Widen stages.state to admit 'SPEC_AUDIT' (the new spec dual-audit step that
-- runs AFTER SPEC and BEFORE BUILD at every stage — models.StageState.SPEC_AUDIT).
-- SQLite cannot ALTER a CHECK, so the table is rebuilt (the 0002_settled
-- precedent). Unlike audit_findings (childless), `stages` is a PARENT: three
-- tables FK-reference it (fix_iterations, churn, audit_findings). With
-- foreign_keys=ON — and PRAGMA foreign_keys is a SILENT no-op inside the
-- per-migration BEGIN IMMEDIATE (db.py:120), the same trap documented at
-- 0001_init.sql:1-2, so it CANNOT be turned off here — a bare DROP TABLE stages
-- fails the FK constraint while ANY table's schema still references it (verified:
-- it fails even with zero offending rows, and even if the children are renamed,
-- because their schema text still names `stages`). The robust rebuild therefore
-- (1) snapshots each child into an FK-LESS temp table, (2) drops the children so
-- nothing references `stages`, (3) rebuilds `stages` with the widened CHECK,
-- (4) recreates the children (FKs restored, now pointing at the rebuilt parent),
-- recopies their rows, and drops the temps. All in the one wrapping transaction;
-- any failure rolls the whole migration back. Using REAL DDL (not writable_schema)
-- means the migrating connection — which db.py reuses live for the scheduler in the
-- `run` path (cli.py:440-442) — sees the new CHECK immediately, with no stale
-- schema-cache cookie games. The child table bodies below are byte-faithful to
-- their current effective schema (0001_init.sql + 0002 rebuild of audit_findings).

-- 1. FK-less snapshots of every child of stages (CREATE ... AS SELECT copies data,
--    NOT constraints — these temps have no REFERENCES, so step 2's DROPs are legal).
CREATE TABLE _spec_audit_tmp_fix_iterations AS SELECT * FROM fix_iterations;
CREATE TABLE _spec_audit_tmp_churn          AS SELECT * FROM churn;
CREATE TABLE _spec_audit_tmp_audit_findings AS SELECT * FROM audit_findings;

-- 2. Drop the children so `stages` has no remaining referrers.
DROP TABLE fix_iterations;
DROP TABLE churn;
DROP TABLE audit_findings;

-- 3. Rebuild stages with SPEC_AUDIT added to the state CHECK. Byte-identical to the
--    current effective schema (0001_init.sql stages + 0005's `kind TEXT`) except the
--    single widened CHECK line.
CREATE TABLE stages_new (
  id               TEXT PRIMARY KEY,
  phase_id         TEXT NOT NULL REFERENCES phases(id),
  name             TEXT NOT NULL,
  risk_class       TEXT NOT NULL,                             -- validated against config risk_classes keys
  state            TEXT NOT NULL CHECK (state IN ('PENDING','SPEC','SPEC_AUDIT','BUILD','VALIDATE','AUDIT',
                     'AWAITING_HUMAN','MERGE_GATE','ESCALATED','DONE','FAILED','CANCELLED')),
  branch           TEXT,                                      -- 'stage/<id>'
  worktree_path    TEXT,
  spec_artifact_id INTEGER REFERENCES artifact_refs(id),
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  kind             TEXT                                       -- 0005: backend/frontend dimension (nullable)
);
INSERT INTO stages_new SELECT * FROM stages;
DROP TABLE stages;
ALTER TABLE stages_new RENAME TO stages;

-- 4. Recreate the children (FKs restored, now referencing the rebuilt stages),
--    recopy their rows, drop the temps, restore the audit_findings index.
CREATE TABLE fix_iterations (                                 -- one row per BUILD->VALIDATE loop (DoD §8)
  stage_id      TEXT NOT NULL REFERENCES stages(id),
  iteration     INTEGER NOT NULL,                             -- 1-based, assigned in-transaction
  failing_tests INTEGER NOT NULL,                             -- from validation-report.json sidecar
  report_artifact_id INTEGER REFERENCES artifact_refs(id),
  created_at    TEXT NOT NULL,
  PRIMARY KEY (stage_id, iteration)
);
INSERT INTO fix_iterations SELECT * FROM _spec_audit_tmp_fix_iterations;
DROP TABLE _spec_audit_tmp_fix_iterations;

CREATE TABLE churn (                                          -- patch-over-patch detector (DoD §8, Doctrine §11)
  stage_id   TEXT NOT NULL REFERENCES stages(id),
  file_path  TEXT NOT NULL,
  region     INTEGER NOT NULL,                                -- hunk start line // escalation.churn_region_lines
  edit_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (stage_id, file_path, region)
);
INSERT INTO churn SELECT * FROM _spec_audit_tmp_churn;
DROP TABLE _spec_audit_tmp_churn;

CREATE TABLE audit_findings (                                 -- auditor AND integration-validator findings (DoD §7, §5.2)
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  stage_id      TEXT NOT NULL REFERENCES stages(id),
  auditor_role  TEXT NOT NULL,                                -- config models.* key
  finding_ref   TEXT NOT NULL,                                -- finding id within the report artifact
  severity      TEXT,
  report_artifact_id INTEGER NOT NULL REFERENCES artifact_refs(id),
  status        TEXT NOT NULL CHECK (status IN ('open','complied','contested','sustained','overruled','duplicate','settled')),
  contest_artifact_id INTEGER REFERENCES artifact_refs(id),   -- executor's rationale; contests always logged
  resolved_by   TEXT,                                         -- 'executor' | 'phase_architect'
  created_at    TEXT NOT NULL, updated_at TEXT NOT NULL
);
INSERT INTO audit_findings SELECT * FROM _spec_audit_tmp_audit_findings;
DROP TABLE _spec_audit_tmp_audit_findings;
CREATE INDEX idx_findings_stage ON audit_findings(stage_id, status);
