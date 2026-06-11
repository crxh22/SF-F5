# B8 seeded-conflict fixture (DoD §5.3 / §12.B8) — permanent regression fixture

First passed LIVE on 11-06-2026 (decision log D-0022) with the REAL cross-model Integration
Validator (codex): Tier-1 green on both stages, the joint SUM-1 violation caught at stage B's
merge gate via sibling-diff visibility, resolution loop completed, fix merged.

## The scenario (joint-only violation)

- **Contract `SUM-1`** (frozen by the Phase Architect before fan-out): *IF* `totals.json` exists,
  its `total_bani` equals the exact sum of `prices.json` values.
- **Seed** (workspace `main`): `prices.json = {"sku-1": 1000}`, no `totals.json` + a
  `__pycache__`-style `.gitignore`.
- **Stage A `catalog-a`**: adds `sku-2: 500` to `prices.json` (+ its test). The invariant is
  *vacuously satisfied* — A is clean alone AND jointly; the validator must stay silent.
- **Stage B `totals-b`** (DAG: after A): adds `totals.json = {"total_bani": 1000}` — correct
  against the PRE-A assumption, own tests green, Tier-1 green. Jointly with A: violation.
  The validator must catch it at B's gate (it only can via the sibling diffs of §3.1's Tier-2
  input contract). Rework call fixes totals to 1500 + a sum-checking test.

## Running it

Per `docs/runbooks/first-live-run.md`, with: a parallel config whose `models.*` are `stub`
EXCEPT `integration_validator: {cli: codex, model: default}`; `process.stub_agent_path:
tests/integration/agent_driver.py`; a `b8` project entry pointing at a freshly seeded workspace
(`test_command: python3 -m unittest discover -s tests -q`); `SF_DRIVER_PLAYBOOK` → a COPY of
`playbook.json` from this directory (the driver consumes the copy destructively); fresh DB;
seed one PENDING phase row (project `b8`). Expected: A → DONE clean; B → gate finding `SUM-1`
(blocker, names the sibling merge) → complied → rework → clean re-gate → DONE.

## Lessons encoded here (from the live runs that failed first)

- v1 of the scenario (a stage directly violating a money-format contract) was NOT joint-only —
  the validator rightly caught stage B without needing the sibling, and its legitimate extra
  findings on stage A derailed the kind-keyed playbook. A correct B8 scenario needs a
  *conditional* invariant: vacuous at A, broken only by the pair.
- Real-validator variance is part of the test: keep stage A's diff surface minimal.
- The run also flushed two factory defects, both fixed: codex sandbox write access
  (`--sandbox workspace-write`, commit 470f4ad) and bytecode droppings tripping the
  Validator-isolation assertion (`process.isolation_ignore_globs`, commit c50bf37).
- Keep `plan_md` IN-WORLD (describe the intended deliverable, never the seeding mechanics):
  the live run's phase-level gate correctly flagged a plan-vs-delivered divergence
  (`PLAN-ASSUMPTION-1`) because v2's plan narrated the trap — a second, bonus catch at the
  recursive level, and a fixture-authoring lesson (fixed in this playbook).
