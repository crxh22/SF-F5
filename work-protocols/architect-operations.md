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
