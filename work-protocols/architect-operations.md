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
  (`rework:SPEC`). It MUST change. If the amendment is purely DOCUMENTARY (changes
  no code), resolve with **`rework:SPEC_DOC`** (D-0059): it amends the spec text
  then skips BUILD straight to VALIDATE, so the wasteful code re-generation is
  avoided while VALIDATE + AUDIT still verify the amended spec against the UNCHANGED
  code — a misclassified (actually-substantive) TIGHTENING is mechanically caught
  there and bounced back, never trusted on your word. (Caveat: LOOSENING/removing a
  requirement is NOT mechanically caught — the validator only tests what the spec
  says — exactly as for plain `rework:SPEC`; that direction stays your judgment +
  the §9 human gate.)

- **The finding is accurate but warrants no action** — code and spec are both
  fine; the observation is true but the behavior is accepted (e.g. a
  more-restrictive, self-healing edge; a deferred defense-in-depth idea). → Give
  it the **no-action disposition** (accurate · acknowledged · permanently closed),
  NOT a contest and NOT a spec rework. This closes it at the audit step and
  records it as settled, so later audits do not re-raise it — avoiding both the
  regeneration loop and an unnecessary rebuild.

The mechanical recurrence backstop (D-0059) is the **`finding_recurrence` event** —
emitted when an audit re-raises a `finding_ref` the SAME auditor already `settled`
or `overruled` on that stage, surfaced on the dashboard Puls line and your session
monitor's exit-14 grep. If it fires, the root was not actually fixed — return to the
generating artifact, do not overrule again. (It fires on `settled`/`overruled` only,
NOT `sustained`: a sustained finding is expected to be re-raisable while the fix
lands.)

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

## 4. Escalation routing ladder + the orchestrator's stuck-escalation detector (robustness UNIT 2, D-0042)

The orchestrator now consumes `escalations.target` as a **live routing signal** — the
durable, in-code replacement for the session-scoped bash monitor that previously was the
architect's only notification path (D-0041/D-0042). It is a **mechanical layer only**: it
reads, pages, and relabels `target`; it NEVER resolves an escalation, transitions a unit,
or spawns an agent (the founder's no-resolver-agent mandate). Resolution stays your
judgment — you still answer via `cli resolve-escalation` / the dashboard card.

**The routing ladder** (`models.ESCALATION_TARGET_LADDER`, the single source consumed by
the scheduler + glossed by the dashboard, == the `escalations.target` DDL CHECK set):

```
phase_architect  →  main_architect  →  founder
```

Creation sites write the first two by escalation nature (stage-conveyor →
`phase_architect`, cross-cutting → `main_architect`); the detector climbs UP toward
`founder` (the top product authority) and clamps there (no rung above founder). A bump is a
**label + page-recipient change only** — bumping to `founder` does NOT raise a decision card
or transition the unit (raising a card is judgment-adjacent; deferred, D-0042 Q3).

**The detector** (`Scheduler._stuck_escalation_detector`, on every tick after the
decision-latency alert) emits three distinct, machine-greppable events and pages via a
DISTINCT **`[arhitect]`** ntfy title prefix on the ONE shared topic (D-0004 — no second
topic; the title lets the founder relay correctly and a phone watcher disambiguate). Each
fires ONCE per episode/rung (latched — no alarm-fatigue thrash):

| event | when | action |
|---|---|---|
| `escalation_opened_notice` | an architect-targeted (`phase_architect`/`main_architect`) escalation is seen `open` and un-notified — **age 0, on the first tick, before any threshold** | one `[arhitect]` page → makes "the architect learns ≤5 min" (D-0042 HARD) a CODE law that survives a dead session monitor. `founder`-targeted escalations are NOT first-noticed here (they are the founder's domain via the trade-off-card path). |
| `escalation_bumped` | `open` whose `created_at` age says it belongs on a HIGHER rung than its current `target` — `expected_idx = min(age // escalation.stuck_escalation_threshold_min, len(ladder)-1)` (default threshold 30 min) | bump `target` straight to the age-derived rung `ladder[expected_idx]` + page that rung. STATELESS (no per-episode latch): the persisted `target` IS the latch, so in steady state it climbs at most one rung per threshold-interval (`phase_architect` → `main_architect` at one threshold, → `founder` at two) and clamps at `founder` — never cascades on a tick-storm. On restart an ALREADY-OLD escalation jumps straight to its age-derived rung in one bump (a hours-old `open` goes to `founder` directly, past `main_architect`). |
| `escalation_stuck_resolved` | `resolved` with `resolved_at` older than the threshold AND the unit is STILL `ESCALATED` (the resolution never got picked up — incident-[20] / auth-cap starvation class) | page the current `target`. The row is already resolved; the SILENCE is the bug. The detector does NOT re-resolve / re-create / transition — UNIT 1 fixes the pickup cause; this is the loud backstop. |

A delivery failure NEVER tears down the loop: it logs ONE `alert_delivery_failed` event
per failure streak (`kind` carries which signal) and retries next tick — the same contract
as the stall / decision-latency pages.

**Your session monitor MUST grep these three event types** (and recognize `[arhitect]`
ntfy titles) so a successor session learns of escalations within one poll of the threshold
crossing — see `docs/runbooks/session-succession.md` for the hand-down. The ntfy `[arhitect]`
push is the human backstop if the monitor is down.
