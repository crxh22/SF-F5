# Resume state — written 10-06-2026 17:45 EEST, before planned power outage (shutdown 17:52)

**For the post-restart session. Read this first; full context in `claude --continue`.**

## Where the build stands

| Wave | Content | State |
|---|---|---|
| 1 | models, config, db + DDL | **verified + committed** `17315a3` (222 tests) |
| 2 | statemachine, thresholds, runner+stub, artifacts, worktrees, notify, watchdog + CCR-2 delta | **verified + committed** `bdcbc4c` (461 tests, ruff clean) |
| 3 | consultation.py, scheduler.py, cli.py + their unit tests | builders self-reported green; **non-executor verification ABORTED** (monthly spend limit hit minutes before its reset) → committed **UNVERIFIED** in the commit after this file |
| 4 | tests/integration/* (DoD §12 scenarios) | **not started** |

## First actions after restart (in order)

1. `export PATH="$HOME/.local/bin:$PATH" && uv run --no-sync pytest -q && uv run --no-sync ruff check src tests` — establish baseline.
2. Run wave-3 **non-executor verification** (the spend-limited step), then one fix round if needed, per the existing workflow script:
   `~/.claude/projects/-home-artur-projects-SF-F5/411a135a-1603-46aa-98fa-c240035771b9/workflows/scripts/build-control-plane-wf_744e84de-565.js`
   — edit it to keep only: verify-wave-3 (+fix) and wave 4 + verify-wave-4. Wave-3 commit exists; amend nothing, add fix commits.
3. Then wave 4 (integration suite) unchanged from the script.

## Architect dispositions pending formal D-entry (record as D-0014 after restart)

- **usage_missing `escalate_after` policy owner** (wave-2 verifier, minor): allocate to **StageExecutor as a direct events-table count check** (like `internal_error` — executor inserts the escalation row itself; NO new Trigger enum member). Add to wave-3 fix round; also fix the misleading comment at `tests/unit/test_runner.py:495` to name StageExecutor as owner.
- **kill_running cmdline exact-match too strict for interpreter-wrapped CLIs** (wave-2 verifier, minor): verify `/proc/<pid>/cmdline` of a real spawned claude process during wave-4 A2 tests; if it differs from recorded argv, loosen to a documented tolerant match via CCR note — never silently.

## Standing context

- Power outage was confirmed; shutdown scheduled 17:52 EEST 10-06-2026; founder powers the server back ~1h later. Tailscale + ssh.socket are boot-enabled — access self-restores.
- Spend limit: founder confirmed the monthly limit was hit minutes before its reset — after restart, capacity should be available again; if an agent dies with a quota error, wait for the window/limit reset and relaunch (state is all on disk).
- Workflow run IDs (journals, same session): design `wf_2b24129b-ec0`; build runs `wf_744e84de-565` → `wf_3d6c7c67-22b` → `wf_e941d2b3-73d`.
- Next after wave 4: Etapa 2 demo scenarios already covered by integration tests; then founder channel (dashboard slice design per OPEN-4) + watchdog systemd timer install; then Etapa 5 (ERP intake with founder).
