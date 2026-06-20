# Session handoff — ETAPA-5q → ETAPA-5r, 20-06-2026

**For ETAPA-5r (Main-Architect successor).** POINTER doc (Doctrine §9). Authoritative state:
- **`docs/session-handoff-ETAPA-5q-20-06-2026.md`** — the PRIOR handoff: dashboard build plan,
  the wiring points, infra (monitor/marker/leash/sf-limit/sf-cap), the founder protocol. Most of
  it is now DONE (see below), but the infra + working-mode + founder-protocol parts STILL apply.
- `docs/decision-log.md` — history. `work-protocols/architect-operations.md` — §1 governs the
  queue resolution that is YOUR first job.
- THIS doc — the **delta**: 5q DEPLOYED the whole dashboard batch + coordinated the restart; the
  drained escalation queue is now UNBLOCKED and is yours to RESOLVE.

Launch: opus, effort max, RC ON named ETAPA-5r, via `claude_canon.sh` (done by 5q).
**FIRST duty: write your session_id into `~/.claude/sf-architect-session` (replace).** Then update
the monitor header (`~/.claude/sf-architect-monitor.sh`: ETAPA-5q → 5r) and launch it via Bash
`run_in_background:true` (5q killed its own on succession — none is running).

## Lineage
5p (built dashboard items 1+2+3) → **5q** (this session: claimed the guard, ran the monitor,
RESPECTED the founder's drain, BUILT items 4+5+6, DEPLOYED item 7 to ERP main separately,
then — on the founder's explicit "go" — MERGED `dashboard-build`→`main`, RESTARTED the factory,
VERIFIED the deploy, and handed off WITHOUT resolving the queue because the context guard fired
at ~645k mid-restart: 3 contest/budget adjudications at that context risk the degraded resolution
architect-operations §1 warns against) → **you = 5r** (RESOLVE the queue; flag the backend-OOM).

## ✅ THE DRAIN IS LIFTED — the factory is LIVE again (restart done 20-06 ~15:35Z)
The founder's operational drain is OVER. 5q restarted the factory ON the founder's go-ahead. The
escalated stages are NO LONGER "leave them queued" — they are PAUSED work you must RESOLVE.

## YOUR IMMEDIATE WORK (in order)

### 1. RESOLVE the escalation queue (architect-operations §1 — fix the generating artifact)
Three escalations are `open`, all `phase_architect`-targeted, on the `inventory-procurement` phase:
- **#87 `inventory-procurement.stocktaking`** — `unresolved_contest`. 90+ min old (created 12:41Z).
- **#88 `inventory-procurement.returns-supplier-client`** — `unresolved_contest` (created 13:35Z).
- **#89 `inventory-procurement.stock-views`** — `context_budget` (created 14:12Z).

Resolve each per **architect-operations §1**: classify FIRST, then act in the SAME resolution.
- `unresolved_contest` (#87/#88): read the contested audit finding + the executor's contest
  artifact (use the dashboard per-stage Detalii page — 5p built it, it renders findings WITH
  inline report/contest content — or `cli`/db). Then: artifact genuinely wrong → `rework:SPEC`
  (or `rework:SPEC_DOC` if purely documentary, D-0059); finding accurate-but-no-action → the
  no-action disposition (settle), which closes it at audit without a rebuild. Carry the WHY into
  `--reason` (architect-operations §2 — it reaches the re-entered agent's prompt).
- `context_budget` (#89): the stage hit `budgets.per_stage[risk]`. The EFFECTIVE-tokens feature
  (5p/item 3) shows whether the spend was real or wasted on OOM-killed runs — check the Detalii
  page's effective-vs-total. This is the **#86/#89 backend-OOM root** (see item 2). Resolve per §2
  reset-vs-escalate (bounded by `escalation.max_context_resets`).
- The orchestrator now consumes `escalations.target` as a live routing signal (D-0042) and the
  `finding_recurrence` event fires if a settled/overruled finding reappears — your monitor greps
  it (exit 14). If it fires after you resolve, the root was NOT fixed; return to the artifact.

### 2. FLAG the backend-pytest-OOM systemic finding to the founder
Item 7 (Layer 2) self-sizes only the **FRONTEND** vitest pool. The **BACKEND** pytest ALSO gets
OOM-killed under the factory's concurrent agent load on the shared leash — the root of the
stock-views #86/#89 budget burn. Two levers, present them to the founder (mode 1, his terms):
- **Immediate, zero-code:** lower `max agenți simultan` LIVE from the new ⚙ Configurare tab
  (now deployed) — fewer concurrent agents = less peak memory. Reversible in one click.
- **Durable:** a backend pytest worker cap mirroring Layer 2 (a conftest/pytest-xdist `-n` sized
  to the cgroup budget), deployed to ERP main like item 7. Bigger, separate deploy.
The memory panel (5p) surfaces the pressure. Don't fix silently — it's the founder's cost/risk call.

### 3. New live levers you can USE (the ⚙ Configurare tab is now live in production)
You no longer need a restart to change: `max_parallel_agents`, the manual DRAIN switch, per-class
budgets, autodrenaj on/off, the 5h/7d thresholds, agent_timeout. Edit them at
`http://100.69.221.108:8377/configurare` (or via `db.set_runtime_setting`) and they apply within
one tick. E.g. for item 2 you can drop `max_parallel_agents` live instead of restarting.

## DEPLOYED / LIVE STATE (snapshot 20-06 ~15:36Z — VERIFY, don't trust stale)
- **Production RUNNING, LEASHED.** Orchestrator pid **140670** (in `.factory/orchestrator.pid`),
  on **`main`** (5q merged `dashboard-build`→main, fast-forward — `git log` HEAD = item-6 commit
  986d361). tmux session `factory`, under `deploy/sf-cap.sh` (cgroup `memory.max=22G`); oom_kill=0;
  ~41M used (just started). Verify via PIDFILE (not pgrep).
- **Schema version → 4.** Migrations **0003** (runtime_settings) + **0004** (decision published_at)
  applied to the LIVE db at restart (`run` migrates pending on startup, cli.py:441). VERIFIED:
  runtime_settings table present, decision_requests.published_at present.
- **Dashboard `http://100.69.221.108:8377/` HTTP 200; `/configurare` HTTP 200** (the new tab is
  live + verified rendering the real effective config). 0 pending decisions.
- **23 DONE, 3 ESCALATED (#87/#88/#89 — YOUR queue), 1 PENDING (phase-integration), 1 CANCELLED.**
- Launch recipe (if you must restart): `tmux new-session -d -s factory -c /home/artur/projects/SF-F5
  'deploy/sf-cap.sh .venv/bin/sf-factory run 2>&1 | tee -a .factory/run-live.log'`.

## WHAT 5q SHIPPED (commits on `main`; `git log` since 7355434)
- `1afa624` items 4+5 backend: `EffectiveConfig` wired into scheduler cap + the 5e manual-DRAIN
  gate, the 5f autodrenaj flag + live thresholds (proactive tick), runner agent_timeout, thresholds
  budget. Empty overrides == byte-identical (the existing suites pass unchanged). +7 override tests.
- `14e8fc1` items 4+5 UI: the ⚙ Configurare tab (GET/POST /configurare) + `DashboardServer.update_settings`
  (all-or-nothing, guards: max_parallel ≥ running count, budget ≥ a running stage's consumed spend,
  is_writable_key allow-list). +8 dashboard tests; live-rendered against a migrated copy of the live db.
- `986d361` item 6: migration 0004 `published_at` + `_publish_pending_decisions` per-tick re-publish
  backstop (a transient ntfy 429 no longer loses a decision page). +4 scheduler tests.
- **ERP main `9a701b1`** (SEPARATE repo, already live): Layer 2 memory-adaptive vitest pool. Verified
  under a 4G scope: 3 workers, peak 1378M, oom_kill 0. The skeleton spec records it for regeneration.

## INFRA YOU INHERIT (details in 5q's / 5p's handoff)
- **Context-guard hook** follows the marker `~/.claude/sf-architect-session` (bytes/5, 500k). Fired
  for 5q at ~645k. **Your FIRST duty: write YOUR session id there.**
- **Monitor** `~/.claude/sf-architect-monitor.sh` — header says ETAPA-5q; update → 5r; launch via Bash
  `run_in_background:true` (NEVER nohup). Exit codes: 10 open-esc set, 11 pending-dec, 12 orchestrator
  death, 13 5h limit, 14 routing/recurrence (`escalation_opened_notice|bumped|stuck_resolved|
  finding_recurrence`). **NOTE:** the orchestrator NOW HAS the D-0042 routing code (it restarted on
  `main` which carries it) — so exit-14 `[arhitect]` bump pages WILL fire now (unlike under 5p/5q where
  the old orchestrator lacked it). Grep all three event types + recognize `[arhitect]` titles.
- `~/.claude/sf-limit.sh` (manual limit), `deploy/sf-cap.sh` (leash 22G+swap).

## WORKING-MODE LEARNINGS (carry forward)
- **MECHANICAL guarantees, not "I'm careful."** 5q verified every commit (suites green), migrations
  against a copy of the live db, the Configurare render against the live db, Layer 2 under real memory
  scopes, and the restart end-to-end (schema 4, routes live, queue preserved, leash on, oom 0).
- **Tight edit→verify→commit.** Run the affected pytest files after each change; ruff before commit.
  NOTE: this repo is NOT `ruff format`-clean (line-length 100 but hand-wrapped tighter) — run
  `ruff check` (the CI gate), do NOT run `ruff format` (it reformats unrelated code).
- **Verify via pidfile, never pgrep; never pkill-self.** Verify schema before DB queries.
- **Founder protocol:** Romanian, plain, his terms (cost/speed/risk), DD-MM-YYYY, long text →
  SendUserFile, concrete examples, **NEVER AskUserQuestion**, no context-stripped IDs.
- **Brutal honesty (§21).** Resolution reasons carry the WHY (architect-operations §2).

## YOUR SUCCESSION (later → ETAPA-5s)
Finish your work unit → write `docs/session-handoff-ETAPA-5s-DD-MM-YYYY.md` → launch ETAPA-5s via
`claude_canon.sh` → **VERIFY 5s's RC on the founder's phone (claude.ai/code, green dot) BEFORE going
silent** → hand the marker. Never two architects writing at once. Procedure:
`docs/runbooks/session-succession.md`.
