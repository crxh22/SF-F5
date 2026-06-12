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

## Workspace bootstrap + phase seeding (Etapa 5 — phase-seeding design §3, D-0024)

One-time per project, operator-driven (Main Architect session — a DoD-sanctioned interactive role
outside the orchestrator, OPEN-4; DoD §12.A1's "zero manual file shuffling by the founder" binds
the conveyor, not one-time setup). `sf-factory seed-phases` verifies the result mechanically
(design §2.3.3) before any DB write — that is what Doctrine §20 actually demands; coding the
bootstrap itself would be a preventive mechanism without incident (Doctrine §8).

### One-time workspace bootstrap (per project)

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

`scripts/test.sh` initial content (proposed, OPEN-2 input): `uv run pytest -q`, with the no-tests
bootstrap window handled **self-retiringly**: on pytest exit code 5 ("no tests collected") the
script greens ONLY if the workspace has no committed test files
(`git ls-files -- 'tests/**/*.py' '**/test_*.py'` empty); once any test file exists, exit 5 =
FAILURE (it then means collection/deselection misconfig — e.g. a bad `testpaths`/`-k` — and must
never silently green the Tier-1 gate; Doctrine §20). Explicit in the script body, never hidden in
config.

### Seeding the macro plan (THE sanctioned path)

```bash
# orchestrator MUST be stopped — seed-phases refuses while the run/resume flock is held
# (and a claim-free seeder never touches the pidfile bytes/mtime, so the watchdog stays honest)
.venv/bin/sf-factory init                                                  # idempotent
.venv/bin/sf-factory seed-phases docs/projects/erp/macro-plan.json --dry-run   # validate first
.venv/bin/sf-factory seed-phases docs/projects/erp/macro-plan.json
.venv/bin/sf-factory run
```

The plan file must be **committed and clean** in the factory repo (tracked — a gitignored file
false-passes a porcelain-only check); the summary prints the **anchor commit sha** the
`macro_plan` ref is pinned to.

### Append-only anchors

Factory-repo commits that registered refs point at (seed anchors, decision answers) are history —
never amend/rebase the factory repo while seeded phases are non-terminal, or `verify_integrity`'s
recorded-commit check aborts every start once gc prunes the sha.

### Proving-ground PLANNING checkpoint (one-time)

For the first real phase (foundation), stop the orchestrator after PLANNING commits the phase
plan, review `phase-plan.json` + the intra-phase contracts (`_factory/contracts/phase-<id>/`),
then resume (the A2-validated path). Bounded validation of an untrusted first-use mechanism
(Doctrine §10) — not standing operator attention.

### OPEN-2 ratification note

Setting `projects.erp.test_command` deliberately amends the pinned config test —
`tests/unit/test_config.py` asserts it `None` today; the ratification commit that sets the
command amends that assertion in the same change. Do NOT amend the test before then.
