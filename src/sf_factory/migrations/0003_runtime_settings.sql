-- Live-editable factory settings (founder dashboard, 20-06-2026). A small
-- key->value overlay the dashboard Configurare tab writes and the scheduler
-- reads each tick to layer over the load-once YAML config; SURVIVES restart
-- (the founder's live edits persist). Structural params (models, prices, ports,
-- risk classes) are NOT here — they stay in YAML and change only on restart.
-- The override key registry + the override-vs-default precedence live ONCE in
-- runtime_settings.py (Doctrine §9). value is a JSON-encoded scalar so a number,
-- bool or string round-trips through one TEXT column.
CREATE TABLE runtime_settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,                 -- JSON-encoded scalar
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL                  -- 'founder' (dashboard) | 'control_plane'
);
