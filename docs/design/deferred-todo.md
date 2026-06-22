# Deferred TODO — post-replan (founder-directed)

Items the founder explicitly deferred to AFTER the ERP replanning is settled. **Do NOT fix now** —
this is the parking lot so nothing is lost. Authored by ARH-01.

## From 22-06 (founder, in chat)

1. **Drain semantics fix.** The founder's EXPECTED behavior: drain ON = let the currently-ACTIVE agent
   finish, then start/resume **NO new agents** (a soft-stop after the current unit). Today's behavior:
   drain holds new STAGE starts but lets IN-FLIGHT stages keep spawning rework agents
   (build/validate/audit/merge each spawn) — which is why drain did NOT stop the treasury merge-gate
   loop. The founder's semantics WOULD have stopped it. Related to (but distinct from) the merge-gate
   loop-cap in `erp-rebuild-reseed-playbook.md` §7 — same goal (stop runaway in-flight work), different
   mechanism. Scope: scheduler dispatch/drain logic (`scheduler.py`, the `eff.drain_manual` gate at
   `_dispatch`). *Founder said: don't fix now, after the replan.*

2. **Dashboard — first block = Claude account limits.** A SHORT block at the TOP of the dashboard: live
   5h + weekly Claude usage consumed + reset time + countdown, refetched fresh on each query. The
   founder re-confirmed this 22-06 ("cred că am spus și mai înainte" — yes he did). Already recorded in
   memory `dashboard-usage-limits-block` and part of the broader dashboard mandate
   (`dashboard-mandate-20-06`). Build with the rest of the dashboard work.

## Already tracked elsewhere (pointers, not duplicates — Doctrine §9)

- **3 pre-re-seed factory fixes** (test-PG socket path overflow, merge-gate loop-cap, short stage-ids)
  → `erp-rebuild-reseed-playbook.md` §7. These are PRE-re-seed (must land before stages run again),
  not post-replan — listed here only for visibility.
- **Dashboard mandate** (memory panel, per-stage "Detalii" button, per-agent timing, budget-on-EFFECTIVE,
  manual drain) → memory `dashboard-mandate-20-06`.
