# Session handoff — ETAPA-5r → ETAPA-5s, 20-06-2026

**For ETAPA-5s (Main-Architect successor).** POINTER doc (Doctrine §9). Authoritative state:
- **`docs/decision-log.md` → D-0061** — THE full record of the 5r session: the queue resolution
  (#87/#88/#89), the mid-session follow-ons (#90/#91/#92), the founder's 20-06-evening rulings +
  what was applied, the backend-OOM proof, the ntfy outage, the open carry-forwards. READ IT FIRST.
- `docs/brief-fondator-20-06-2026.md` — the founder-facing brief (backend-OOM + the levers).
- `work-protocols/architect-operations.md` — §1 (contest resolution) + §3 (rework:MERGE_GATE) +
  §4 (escalation routing) governed every resolution this session.
- `docs/runbooks/session-succession.md` — this procedure.

**FIRST duties** (your launch prompt says it): write your session_id into `~/.claude/sf-architect-session`
(replace — claim the context guard), then update the monitor header (`~/.claude/sf-architect-monitor.sh`:
ETAPA-5r → 5s) and launch it via Bash `run_in_background:true`. The 5r monitor dies with this session.

## Lineage
5q (deployed dashboard + restarted factory LIVE) → **5r** (this session: RESOLVED the queue
#87/#88/#89 per architect-operations §1; drove stocktaking to convergence through #90/#91/#92;
PROVED the backend-pytest-OOM; flagged the founder; got + APPLIED the founder's 20-06-evening
rulings) → **you = 5s** (do the founder-scheduled backend fix; clear stock-views; finish the merges).

## ✅ FACTORY IS LIVE + HEALTHY (verify, don't trust this stale snapshot — 20-06 ~18:35Z)
- Orchestrator pid **140670** (`.factory/orchestrator.pid`), on `main`, tmux `factory`, leash 22G
  (`deploy/sf-cap.sh`). Verify via PIDFILE + `/proc/<pid>/cmdline` (NOT pgrep — the path substring
  false-matches sessions whose prompt embeds it, incl. yours).
- **Live settings (runtime_settings, applied this session):** `max_parallel_agents=2` (founder OK'd),
  `budget.critical=450000000` (founder "cu rezerva"). ⚠️ **REVERT `budget.critical`→364M** after the
  critical stages are DONE (it's a finish-line bump, not permanent — weakens the runaway detector).
- **Stages:** stocktaking + returns-supplier-client are MERGING (both approved by the founder via me;
  the merge-gate target-branch lock SERIALIZES them — no concurrent-backend OOM). stock-views is
  PARKED (card #15 pending). ~23 DONE.
- **1 founder card pending: #15 (stock-views, budget)** — intentionally HELD until the backend fix.

## YOUR IMMEDIATE WORK (in order)

### 1. Backend-tests durable fix — FOUNDER-SCHEDULED "imediat după asta" (your first task)
The backend pytest OOM-kills under concurrent agent load on the shared 22G leash (PROVEN: `exit 137`
×4 + ~292 pytest invocations in `.factory/logs/proc-69c2bf08debd.ndjson`). A SINGLE pytest run fits
22G (stocktaking's builder: 928 passed solo); the OOM is concurrent runs summing >22G. Build a backend
pytest **worker cap** (a conftest / `pytest-xdist -n` sized to the cgroup budget), mirroring the
already-deployed FRONTEND Layer-2 vitest cap (ERP main `9a701b1`, "memory-adaptive vitest pool").
Deploy to **ERP main** (separate repo `/home/artur/projects/erp-workspace`, docs_repo
`/home/artur/projects/ERP-start`). The skeleton/spec records it for regeneration (as Layer-2 did).
Verify under a real memory scope (Layer-2 was verified under a 4G scope: 3 workers, peak 1378M, oom 0).
**Sequencing:** do NOT run the full backend pytest yourself WHILE stocktaking's/returns' merge suite
is running (concurrent → OOM). Let the in-flight merges finish first, OR validate the cap under an
isolated small scope.

### 2. Clear card #15 (stock-views) — AFTER the backend fix
stock-views (routine) burned 44.3M vs the 30M cap (lifetime sum; a context_reset refreshes the agent
window, NOT the ledger), so `rework:BUILD` alone re-escalates. After the backend fix: bump
`budget.routine` live (e.g. 30M→80M to clear the 44M + a clean ~6M re-run — TEMPORARY, revert after
DONE) + `decide 15 rework:BUILD` (or resolve via the card). It re-runs cleanly (single stage, no OOM).
**Apply it yourself** — the founder does NOT use the dashboard ([[founder-applies-approvals-via-architect]]).

### 3. Watch the in-flight merges (stocktaking, returns) converge to DONE
The monitor does NOT wake on a stage→DONE (only escalations/decisions/orchestrator). Poll occasionally.
A **#92-class dirty-worktree merge error** can recur (audit-then-escalate leaves uncommitted, UNREGISTERED
reports → tier1_gate's rebase refuses the dirty tree, then mis-aborts a non-started rebase → GitError).
RECOVERY (proven this session): `git -C <worktree> checkout -- <the uncommitted _factory/.../audit-*.md/json>`
(VERIFY they're unregistered: their sha256 ≠ the artifact_refs', which match HEAD), then
`resolve-escalation <id> rework:MERGE_GATE`. returns' worktree was CLEAN at hand-off (checked).

### 4. Founder decisions still open (chat → you apply; he won't touch the dashboard)
- **ntfy founder-page channel is DOWN** (ntfy.sh egress-blocked: IPv4 159.203.148.75 times out, no IPv6;
  general internet works). All autonomous pages fail; the founder's PRIMARY channel is the claude.ai/code
  SESSION + your SendUserFile. NOT fixable from the session — needs an egress/firewall fix (his infra).
  Flag it again; relay via SendUserFile meanwhile. Plus the §20 gap: nothing surfaces `alert_delivery_failed`
  (the monitor greps escalations/decisions/orchestrator/limit, NOT delivery failures).

## FOLLOW-UPS (lower priority — don't drop)
- **REVERT `budget.critical`→364M** after the critical stages are DONE (HARD reminder).
- **Document the single-regime fiscal rule** in the returns spec: the founder ruled a supplier return is
  ONE fiscal regime (mixed with/without-VAT CANNOT exist → 2 documents), so RSC-SM-105's §4.8-vs-§5.3
  divergence is about an impossible case (cosmetic; the code is correct). A small rework:SPEC_DOC or a
  note — non-blocking.
- **Factory robustness gap** (Doctrine §8 — 2nd incident would justify a code fix): tier1_gate
  (`worktrees.py:413-416`) treats ANY `git rebase` failure as a conflict and aborts; a non-started rebase
  (dirty tree) → GitError. And the audit step leaves reports uncommitted when the round escalates. Both
  caused #92. A proper fix: tolerate "no rebase in progress" + commit audit reports before AWAITING_HUMAN.

## INFRA YOU INHERIT
- **Context-guard hook** follows `~/.claude/sf-architect-session` (bytes/5, 500k). Fired for 5r at ~559k.
  Your FIRST duty: write YOUR id there.
- **Monitor** `~/.claude/sf-architect-monitor.sh` (header 5r→5s; launch run_in_background). Exit codes:
  10 open-esc set, 11 pending-dec set, 12 orchestrator death, 13 5h-limit, 14 routing/recurrence event.
  NOTE: it greps `escalation_opened_notice|bumped|stuck_resolved|finding_recurrence` BUT the orchestrator
  CANNOT page (ntfy egress down) — the events fire, the ntfy push fails (`alert_delivery_failed`).
- `~/.claude/sf-limit.sh` (5h limit), `deploy/sf-cap.sh` (22G+swap leash). DB `.factory/factory.db`
  schema 4; columns: events.payload_json, escalations.trigger/unit_id, stages.state, audit_findings.

## WORKING-MODE LEARNINGS (carry forward)
- **VERIFY at SOURCE, not the executor's self-citation** (Doctrine §4/§5): every contest this session
  was checked against the actual spec.md lines + the auditor report, not the findings-response paraphrase
  (it flipped #87 from "settle" to rework:SPEC_DOC, and confirmed #88's contradiction).
- **MECHANICAL guarantees, not "probably fine"** ([[mechanical-guarantees-over-attention]]): the backend-OOM
  was PROVEN (exit 137 in logs), not asserted; the budget question answered from the code; the artifact_ref
  alignment computed (sha256) before the #92 git surgery.
- **Founder protocol:** Romanian, plain, his terms (cost/speed/risk), DD-MM-YYYY, long → SendUserFile,
  concrete, **NEVER AskUserQuestion**, no naked IDs. He gives decisions in CHAT; you APPLY them
  (`sf-factory decide <id> <token>` / `/configurare` POST) — he won't use the dashboard.
- Map his plain ruling to the gate token ("o aprob"→`approved`). `decide <id> approved|rework:BUILD|rework:SPEC`.
- **Verify pidfile via /proc, never pgrep; never pkill-self.** Verify schema before DB queries.

## YOUR SUCCESSION (later → ETAPA-5t)
Finish your unit → write `docs/session-handoff-ETAPA-5t-DD-MM-YYYY.md` → launch 5t via the runbook →
VERIFY 5t's RC on the founder's phone BEFORE going silent → hand the marker. Never two architects writing.
