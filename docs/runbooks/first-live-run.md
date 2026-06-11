# Runbook — live factory run (first run: 11-06-2026, synthetic critical-gate demo)

Distilled from the first live run (decision log D-0021). The synthetic demo drives the REAL
orchestrator/dashboard/ntfy/git/DB/watchdog with deterministic stub agents
(`tests/integration/agent_driver.py`) — zero spend; sanctioned for mechanism criteria (DoD §1).

## Prerequisites

- Workspace repo exists with an initial commit and **repo-local git identity** (no global identity on e9).
- Config: copy of `factory.config.yaml` changing ONLY `projects.*` (workspace paths + a real
  `test_command`) and `models.*` → `{cli: stub, model: stub, mode: print}` (keep `decision_session`
  on claude). `process.*` paths MUST stay identical — the watchdog unit reads the production config
  and watches those paths. Live copy used: `.factory/factory.synthetic.yaml` + `.factory/playbook-synth.json`.
- Founder phone on the tailnet (dashboard is tailnet-only). Push links use hostname
  `http://server-e9:8377` (MagicDNS); IP fallback `http://100.69.221.108:8377/`.

## Sequence

```bash
.venv/bin/sf-factory -c <config> init          # fresh DB + migrations
# seed phase rows ONLY while the orchestrator is stopped (sole-writer rule):
.venv/bin/python - <<'EOF'
# load_config -> Database.open -> insert_phase(Phase(id=..., project=..., state=PENDING, ...))
EOF
tmux new-session -d -s factory -c /home/artur/projects/SF-F5 \
  'export PATH="$HOME/.local/bin:$PATH"; export SF_DRIVER_PLAYBOOK=<playbook>; \
   .venv/bin/sf-factory -c <config> run 2>&1 | tee -a .factory/run-live.log'
sleep 10  # verify: pidfile, liveness mtime, curl dashboard -> 200, log "entering scheduler loop"
sudo systemctl enable --now sf-factory-watchdog.timer   # ARM only while an orchestrator runs (D-0016)
```

## Stop / restart (the order matters)

1. `sudo systemctl disable --now sf-factory-watchdog.timer` — disarm FIRST or the stop pages the founder.
2. `tmux send-keys -t factory C-c` — clean shutdown (agents die via PDEATHSIG).
3. **The tmux session dies with the command** (it was created WITH a command, no shell underneath):
   restart = `tmux new-session -d -s factory '...'` again, never `send-keys` into the dead session.
4. Re-arm the watchdog after the new instance is up.

## Verified behaviors (first run)

- Conveyor SPEC→BUILD→VALIDATE→dual-AUDIT→AWAITING_HUMAN in seconds; ntfy decision push (priority
  high, RO title) delivered; founder answered all four decisions from the phone; Tier-1 (rebase +
  test suite) + Tier-2 (integration validator) gates passed; phase sign-off; answer artifacts +
  session transcripts auto-committed to the factory repo.
- Watchdog: healthy check exit 0, zero false pages across a disarm→stop→start→re-arm cycle.
- Failure honesty under a real outage: a Decision-Session turn hit the Anthropic billing window
  (claude exit 1, `api_error_status: 403`, "organization has disabled subscription access") → RO
  failed-turn notice appended to the transcript, no retry, buttons unaffected, transcript committed
  with the answer. After payment: round 2 turn exit 0, canon-injected, tools-off.

## Billing-outage signature (for future triage)

`process_registry` row `role='decision_session'` (or any claude route) with `exit_code=1` and the
ndjson result line carrying `"api_error_status": 403 ... "disabled Claude subscription access"` →
subscription payment lapse; fix is on the Anthropic billing page, nothing in the factory.
