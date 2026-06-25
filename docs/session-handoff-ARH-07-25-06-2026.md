# Session handoff — ARH-07 → ARH-08, 25-06-2026

**For the Main-Architect successor.** POINTER doc (Doctrine §9). ARH-07 (post-25-06 reset): resumed
the factory, finished **L0** (founder signed + merged to main), resolved two adjudications (the L0 visual
contest + the L1 N1 contract-change-request), and stood up the **L0 founder test instance**. Durable memory:
**[[applayout-phone-chrome-overlap]]** (← PRIORITY #1) + **[[erp-local-test-instance]]** +
**[[erp-rebuild-structure-authored-23-06]]** + **[[founder-applies-approvals-via-architect]]** +
**[[mechanical-guarantees-over-attention]]** + **[[evidence-over-guessing-and-budget-headroom]]**.

> ## ⛔ ABSOLUTE RULES (carry forward)
> 1. **NEVER `pkill -f` / `pgrep -f`** a prompt-matchable pattern — kills ALL architect sessions. Stop a
>    task ONLY by EXACT PID or EXACT tmux name.
> 2. **NEVER kill/exit a PREDECESSOR** (arh-03…arh-07). Leave attached + idle; the FOUNDER retires them.

## NAMING — you are ARH-08
`ARH - 08` (phone RC label; tmux slug `arh-08`). Runbooks: `session-succession.md` + `session-launch-protocol.md`.

## FIRST duties (in order)
1. **Claim the marker.** Your id = newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/`
   containing a UNIQUE phrase from YOUR launch prompt (NOT mtime — the live one GROWS as a top-level USER
   turn; the scratchpad path also encodes your id). Write it into `~/.claude/sf-architect-session`,
   REPLACING `8875f6e8-675e-45c7-9bbb-28bb45dcdd2b`.
2. **Verify RC** (`ARH - 08` on the founder's phone). He is ONLINE + engaged — do NOT go silent.
3. **START the monitor** (factory is RUNNING). `bash ~/.claude/sf-architect-monitor.sh` via Bash tool
   `run_in_background:true`. Bump its header `ARH-08`. Grep escalation events.
4. **Do NOT kill arh-03…arh-07.**

## 🔴 PRIORITY #1 — founder-directed: fix the AppLayout phone-chrome overlap at the FOUNDATION
**The founder explicitly flagged (25-06):** if we don't fix the phone overlap at the foundation it ECHOES
on every UI stage → contest → settle → repeat = wasted time + tokens. He is RIGHT (architect-operations §1
regeneration trap). See **[[applayout-phone-chrome-overlap]]**.
- **Root cause:** `frontend/src/ui/AppLayout.tsx:31` `<Sider width={220} breakpoint="lg" collapsedWidth={0}>`
  — at the phone (`lg`) breakpoint the collapsed-sidebar trigger (hamburger ≡) overlaps the first line of
  page content on EVERY route (verified visually in the L0 captures). F6/foundation defect, pre-existing.
- **Fix:** give the collapsed trigger a content offset / reposition it so it never overlaps. Land it ONCE.
- **Mechanism (your call):** preferred = a SMALL dedicated factory fix-stage so the visual gate mechanically
  verifies no overlap on all routes ([[mechanical-guarantees-over-attention]]). The L1–L9 plans are
  **prefrozen/founder-ratified** (`docs/projects/erp/rebuild/phase-plans`, `factory.config.yaml`
  `prefrozen_phase_plans`) so the fix is NOT in them — work out the cleanest housing (a tiny foundation/hotfix
  phase, or fold into the earliest FE stage with the founder's ok). A tight hand-edit + re-run the visual
  capture to verify is acceptable for this low-risk chrome change IF you verify visually — but the founder
  wants mechanical guarantees, so prefer the factory path.
- **Timing:** land it BEFORE `l1-nomencl.crud-framework-skeleton`'s visual gate (the first L1 FE stage that
  captures phone screens). The CURRENT L1 stage (`nomencl-rest-verify`) is VERIFY-ONLY (no UI) — safe. With
  `max_parallel_agents=1` the FE stages are still a while out; you have a window. Drain if it gets tight.

## 🏭 STATE (verify fresh)
- Factory **RUNNING** (orchestrator in tmux `factory`; pid was 946867). Dashboard `http://100.69.221.108:8377`.
  `drain.manual=false`, `max_parallel_agents=1`, `governor.seven_day_threshold_pct=97`.
- **L0 = DONE, merged to main** (`erp-workspace` main `70aa946 integrate phase/l0-shell into main`). Both
  screens shipped (menu-registry + mount-orphans-home). Founder signed `approved` (decision 1) 05:51.
- **L1 = RUNNING.** `l1-nomencl.nomencl-rest-verify` re-running at **SPEC** after I resolved its CCR
  (esc 6, `rework:SPEC_DOC`) — I reconciled the frozen **N1 contract** (`NO-INTROSPECTION` was internally
  contradictory + false vs as-built DRF OPTIONS metadata; reworded §1/§6/§8, documentary, committed
  `37ae9fa` on the stage branch). It will re-derive spec → VALIDATE → AUDIT → merge. Next L1 stages
  (prefrozen): `crud-framework-skeleton` (FE), `instantiate-catalogs` (FE).
- **Gating:** `proving_phases=[l0-shell]` in `factory.config.yaml` — now INERT (the hold dissolves once every
  proving phase is DONE; L0 is DONE). The DAG governs; phases flow L1→…→L9, one at a time. **Each phase pauses
  at its own `phase_signoff` (AWAITING_SIGNOFF) for the FOUNDER** — verified universal (`_enter_signoff` is
  unconditional). So per-layer founder control is preserved without action from you.

## 🖥️ L0 FOUNDER TEST INSTANCE — UP (leave running while he tests; revert when done)
Founder is testing L0 live. See **[[erp-local-test-instance]]**. Running in tmux:
- `erp-be` (Django `127.0.0.1:8000`), `erp-fe` (Vite `0.0.0.0:5173`), `erp-approver` (auto-approves devices,
  `/tmp/erp_approver.py`), PG via `pg.sh` (socket `/tmp/sfpg-21e9b5b08006`, in `.worktrees/l0-shell`).
- Founder URL **`http://100.69.221.108:5173`**, login **`artur` / `test1234`** (verified end-to-end).
- **UNCOMMITTED dev tweaks** in `.worktrees/l0-shell` (the Secure-cookie-over-HTTP fix): `backend/erp/settings/dev.py`
  (+`SESSION_COOKIE_SECURE=False`/`CSRF_COOKIE_SECURE=False`) + `backend/apps/accounts/api.py:78`
  (`secure=settings.SESSION_COOKIE_SECURE`). **REVERT** (`git checkout` those 2 files) when the founder is done
  testing — main already has the clean L0 merge, so these never touched main; they trip `_OutOfBoundsDetector`
  once (latched, harmless).

## 🚦 CAPACITY — relaxed (post-reset)
Weekly reset 25-06 ~03:00; now ~2–3% used, resets **02-07 ~03:00 UTC**. 5h ~13%. The hard-wall pressure ARH-06
faced is GONE — operate normally, just not wasteful (the founder's token-waste concern drives PRIORITY #1).

## 📋 ADJUDICATION PRECEDENT (architect-operations §1 — fix the generating artifact, don't defer)
- **esc 5** (L0 visual contest, `settled`): the mount stage's contest was UPHELD — the overlap is the F6
  AppLayout defect, not the stage's. → PRIORITY #1 above is the real root fix.
- **esc 6** (L1 N1 CCR, `rework:SPEC_DOC`): reconciled the frozen N1 contract (I'm the contract owner). Verify
  facts from the agent's OWN evidence ([[evidence-over-guessing-and-budget-headroom]]) — I verified api.py +
  base.py before resolving.
- CLI: `.venv/bin/sf-factory resolve-escalation <id> <token> --reason "<why>"` (tokens: rework:SPEC | SPEC_DOC |
  BUILD | VALIDATE | MERGE_GATE | settled | awaiting_human | cancelled | failed | respec). Carry the WHY in
  `--reason` (architect-operations §2 — it reaches the re-entered agent).

## WORKING MODE / SUCCESSION
Romanian to founder, plain, DD-MM-YYYY, copy-paste, **NEVER `AskUserQuestion`**, no bare IDs, brutal honesty
over validation. **Architect commits to main**, `ruff check` before commit, VERIFY (diff+tests) before merge.
When YOU hand off, follow `session-launch-protocol.md` verbatim (auto-launch `ARH - 09`; never kill predecessors).
