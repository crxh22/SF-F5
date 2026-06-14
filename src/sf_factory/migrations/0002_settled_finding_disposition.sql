-- Widen audit_findings.status to admit 'settled' (the no-action disposition: an
-- accurate finding the phase_architect acknowledges and permanently closes —
-- architect-operations.md §1). SQLite cannot ALTER a CHECK, so rebuild the table.
-- NO PRAGMA foreign_keys=OFF here: it is a silent no-op inside the per-migration
-- BEGIN IMMEDIATE (db.py:120), the same trap documented at 0001_init.sql:1-2. The
-- whole file runs in one transaction (the runner wraps it) — any failure rolls
-- the entire migration back. FK-safe by topology: nothing references
-- audit_findings, so DROP + RENAME orphans no child rows.
-- audit_findings_new is byte-identical to 0001_init.sql's audit_findings except
-- for the single widened CHECK line below.
CREATE TABLE audit_findings_new (                             -- auditor AND integration-validator findings (DoD §7, §5.2)
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
INSERT INTO audit_findings_new SELECT * FROM audit_findings;
DROP TABLE audit_findings;
ALTER TABLE audit_findings_new RENAME TO audit_findings;
CREATE INDEX idx_findings_stage ON audit_findings(stage_id, status);
