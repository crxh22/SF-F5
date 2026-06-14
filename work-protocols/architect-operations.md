# architect-operations.md — operating rules for the architect roles

**Regime:** loaded into the system prompt of the **architect-class roles only**
(main_architect, phase_architect, spec_agent) via the canon's architect layer —
NOT the shared canon. These rules govern how an architect RESOLVES and AMENDS;
they do not restate what the control plane mechanically enforces.

## 1. Contest resolution — fix the generating artifact, never defer it as "editorial debt"

An audit re-derives its findings from the SPEC and the contracts every round. So
an unfixed mismatch in those written artifacts **regenerates the same finding at
the next audit** — an unbounded contest→escalate→overrule→re-raise loop. "Note it
for later" is attention-based and fails (Doctrine §20). Observed twice before this
rule existed: the core-entities §7 migration-graph line (3 rounds) and the
auth-access idle-timeout clause (2 rounds).

When you resolve a prevailed contest, first classify it, then act in the SAME
resolution — never overrule-and-defer:

- **The artifact is genuinely wrong** — the SPEC/contract asserts something the
  code correctly does NOT do (the text lies about the code). → Amend the text now
  (`rework:SPEC`). It MUST change. If the amendment is purely documentary (it
  changes no code), route it down the documentary path so it does not force a
  needless rebuild.

- **The finding is accurate but warrants no action** — code and spec are both
  fine; the observation is true but the behavior is accepted (e.g. a
  more-restrictive, self-healing edge; a deferred defense-in-depth idea). → Give
  it the **no-action disposition** (accurate · acknowledged · permanently closed),
  NOT a contest and NOT a spec rework. This closes it at the audit step and
  records it as settled, so later audits do not re-raise it — avoiding both the
  regeneration loop and an unnecessary rebuild.

The mechanical recurrence flag on the dashboard is the backstop: if a finding you
settled or overruled reappears, that is the signal the root was not actually
fixed — return to the generating artifact, do not overrule again.

## 2. Carry the WHY into the re-entered role

Every rework re-entry you author (escalation resolution, respec, rebuild) must
carry your rationale in the resolution `--reason`: it reaches the re-entered
agent's prompt (rework_context). A fresh-context Spec/Build agent cannot fix what
it cannot see — name the exact artifact, line, and the contradiction.

## 3. `rework:MERGE_GATE` — only for a merge-gate failure, never to skip the gates before it

`rework:MERGE_GATE` re-enters ONLY the merge gate (Tier-1 rebase+suite + Tier-2
integration_validator) — no re-validate, no re-audit, no §9 human gate. It is the
correct, cheap resolution for a stage that failed AT the merge gate with
`agent_run_failed` (e.g. the integration_validator overflowed its context window):
the structural validation and dual audit already passed and must not be re-run, and
re-validating needlessly re-spends the (already large) stage budget — which is what
forced this token into existence (D-0041, document-engine at 107M against the 120M
structural cap).

NEVER apply it to:
- an **`unresolved_contest`** escalation — the gate only closes `open`
  integration_validator findings, so the contested structural findings would be
  left `contested` forever and the stage could merge to DONE with a dangling,
  never-settled contest. Use `rework:VALIDATE` / `rework:BUILD`.
- a stage that has **not yet passed AUDIT** (escalated from SPEC/BUILD) — it would
  jump to the gate with zero structural validation and, on a critical stage,
  bypass the founder §9 human gate. Re-enter the step that actually failed.

There is deliberately **no machine guard** (Doctrine §8 — no preventive mechanism
without an incident); this rule is the guard. A misapplication is the incident that
would justify a code-level precondition.
