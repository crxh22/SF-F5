# Session handoff — ETAPA-5p → ETAPA-5q, 20-06-2026

**For ETAPA-5q (Main-Architect successor).** POINTER doc (Doctrine §9). Authoritative state:
- **`docs/session-handoff-ETAPA-5p-20-06-2026.md`** — the PRIOR handoff: the DRAIN strategy, the
  dashboard build plan/order, infra (monitor/marker/leash/sf-limit/sf-cap), launch recipe,
  gotchas. **STILL VALID for the drain + infra + the build reqs — read it.**
- architect memory **`dashboard-mandate-20-06-2026`** — founder's dashboard reqs + decisions.
- **`docs/design-configurare-dashboard-20-06-2026.md`** — the Configurare tab UI design (approved).
- `docs/decision-log.md` — history.
- THIS doc — the **delta**: what 5p BUILT (items 1+2+3, committed on a branch) + the precise
  wiring points for the REMAINING items (4,5,6,7).

Launch: opus, effort max, RC ON named ETAPA-5q, via `claude_canon.sh` (done by 5p).
**FIRST duty: write your session_id into `~/.claude/sf-architect-session` (replace).**

## Lineage
5o → **5p** (this session: claimed the guard, verified+stabilized production, ran the monitor,
RESPECTED the founder's drain — postponed escalations #87/#88/#89, never resolved — and BUILT
items 1+2+3 of the dashboard batch on branch `dashboard-build`, 6 commits, each verified) →
**you = 5q** (build items 4,5,6,7; then coordinate the ONE restart with the founder).

## ⚠️ THE DRAIN IS STILL ACTIVE (founder, 20-06 — CRITICAL)
- **DO NOT resolve ANY architect escalation.** Queue is now **[#87 stocktaking (unresolved_contest),
  #88 returns-supplier-client (unresolved_contest), #89 stock-views (context_budget)]** — all
  drained, none resolved. Leave them queued until AFTER the coordinated restart.
- It is **SAFE**: an escalated stage sits paused, ships NOTHING. The factory is now essentially
  fully wound down (3 ESCALATED, 1 PENDING phase-integration, the rest DONE/CANCELLED).
- The monitor fires exit 10 on each new escalation (expected — note, don't resolve). **NOTE:**
  the orchestrator stuck-detector (D-0042 bump events) is **NOT emitting** — `routing_evt_seq`
  stuck at 3219 though #87 is 90+ min old → the RUNNING orchestrator (on `main`, started 13:33)
  lacks that code. So the monitor's **exit-10 (open-escalation set change) is the working
  notification path**, not exit-14. Don't expect `[arhitect]` bump pages from THIS orchestrator.
- **When the build is ready:** decide the restart window WITH the founder → restart → **THEN
  resolve the whole queue** (architect-operations §1 — fix the generating artifact, don't defer).
- Founder human-gates (decision_requests) stay the founder's. 0 pending now.

## THE BUILD — state on branch `dashboard-build` (6 commits; `git log main..dashboard-build`)
**factory.home is checked out on `dashboard-build`. The RUNNING orchestrator is on `main`
in-memory (started 13:33) — UNAFFECTED by the branch.** The build deploys at the ONE restart
(merge `dashboard-build` → main, or restart from the branch). An unplanned restart before then
would deploy the verified partial batch + migration 0003 (additive) — degraded-but-working, not
broken (every commit imports + passes tests).

### DONE + committed + verified (items 1, 2, 3)
- **Item 1 — read-only panels** (all in `dashboard.py`, pure-render + `_open_ro` data layer):
  - (c) **tokens in thousands** — `_fmt_ktok` (12_547_709 → "12.548"), `(mii)` labels; `$` untouched.
  - **Memory panel** — `_build_memory`/`_render_memory`: host RAM (/proc/meminfo) + cgroup leash
    (`/proc/self` parent-walk to memory.max/swap.max/current) + per-agent RSS. **Verified vs the
    live 22G/2G scope.**
  - **Per-agent start/finish/duration** — `/costuri` Pornit+Durată columns; `list_token_ledger`
    now LEFT JOINs `process_registry` (spawned_at/ended_at/state/exit_code on `AgentCostRow`).
  - **Per-stage Detalii** — `GET /stage/<id>` (`build_stage_detail`/`render_stage_page`): history
    (transitions) + agents-with-results + audit findings WITH inline report/contest content
    (esc'd `<pre>`, 8000-char cap + `/artifact/<id>` link). Linked from "Acum în lucru" rows.
    **Verified live** on stocktaking (28 findings) + returns-supplier-client (35).
- **Item 3 — effective tokens** (founder: budget on EFFECTIVE, not total):
  - `db.effective_token_sum` + **`_FAILED_RUN_SQL`** = THE single predicate (Doctrine §9):
    failed = `state IN ('timed_out','killed','orphaned') OR (state='exited' AND exit_code<>0)`.
    Running/spawned + clean exit-0 COUNT. exit-0 declared-failure treated as delivered (rare, not
    DB-distinguishable, visible per-run in /costuri).
  - `thresholds._check_context_budget` triggers on EFFECTIVE; evidence carries effective + total.
  - Dashboard budget table: **Efectiv | Total | Plafon | %** (% on effective) + a note when waste>0.
  - **Live finding:** returns-supplier-client wasted **8.8% (28M tok)** on failed runs (now excluded).
- **Item 2 — runtime_settings SPINE** (the foundation; NO production wiring yet):
  - Migration **`0003_runtime_settings.sql`** (key/value/updated_at/updated_by; value = JSON scalar).
    **Verified: applies cleanly to a COPY of the LIVE db** (table created, schema version → 3).
  - `db.get_runtime_settings` / `db.set_runtime_setting` (upsert).
  - **`runtime_settings.py`**: the SINGLE source of override keys + `EffectiveConfig` — a pure
    overlay laying DB overrides over the load-once `FactoryConfig`. Properties: `max_parallel_agents`,
    `agent_timeout_s`, `budget(rc)`, `gov_five_hour_pct`, `gov_seven_day_pct`, `autodrenaj`,
    `drain_manual`. Plus `is_writable_key()` allow-list (structural params can NEVER be set live).

### REMAINING — items 4, 5, 6, 7 (build order: 4+5 together, then 6, then 7 separately)

**Items 4 (Configurare tab) + 5 (drain switch + autodrenaj) — the live-control feature.** Wire
`EffectiveConfig` into the consumers, then build the editable UI. **KEY SAFETY: with EMPTY
overrides, `EffectiveConfig` returns the cfg values byte-identically — the existing scheduler/
thresholds tests MUST still pass unchanged. Test the default path == current behavior, then each
override.** Build `EffectiveConfig(fdb.get_runtime_settings(conn), self._cfg)` once per tick.

Verified wiring points (line numbers may drift — re-grep):
- `scheduler.py:5536` `cap = self._cfg.process.max_parallel_agents` → `eff.max_parallel_agents`.
  Cap enforced at `self._spawning_count() >= cap` (5566); `_spawning_count` at 5610.
- **Drain gate (5e):** in the dispatch loop, before a SPAWNING drive, add `if spawns and
  eff.drain_manual: continue` (hold new spawns, let running finish) — mirror the economics-cap
  `if spawns and self._spawning_count() >= cap: continue`.
- `_proactive_limit_tick:1218` gate `if not (self.enabled and cg.proactive_enabled): return` →
  add `and eff.autodrenaj` (5f). Thresholds `cg.five_hour_threshold_pct`/`seven_day` (1228-1230)
  → `eff.gov_five_hour_pct`/`eff.gov_seven_day_pct`. NOTE: `eff.autodrenaj` defaults to
  `cfg.capacity_governor.proactive_enabled` (False) → default behavior unchanged.
- `thresholds._check_context_budget` `budget = self._cfg.budgets.per_stage.get(rc)` →
  `EffectiveConfig(get_runtime_settings(conn), self._cfg).budget(rc)` (it has `self._db`).
- `agent_timeout_s` (4): `run_agent` already takes a `timeout_s` param — have the scheduler read
  `eff.agent_timeout_s` and PASS it at the call site (grep `run_agent(` in scheduler.py; verify).

**The UI (item 4)** — `docs/design-configurare-dashboard-20-06-2026.md §2` mockup. New tab
"⚙ Configurare" + a POST route (mirror the decision-answer POST: `_read_form` / `_marshal`, the
`_DECISION_ANSWER_RE` handler). Write via `db.set_runtime_setting` + insert a
`runtime_setting_changed` event (audit; surfaces in the Detalii/decision-log). **Per-field show
live?/when-applies/guard** (founder req b). **Guard:** reject `max_parallel_agents` below the
live running count — the dashboard has no scheduler instance, so approximate via
`SELECT COUNT(*) FROM process_registry WHERE state IN ('running','spawned') AND kind='agent'`
(the scheduler re-checks per-tick anyway; document the approximation). Budget live-edit applies to
the running stage at its next budget check (`thresholds` reads per-tick). **The UI is read-only-ish
rendering + a POST handler — a good candidate to DELEGATE to a subagent (like 5p did the Detalii
view), then review + verify yourself.**

**Item 6 — ntfy decision-page retry fix.** `scheduler.py:2911 _publish_decision` is ONE-SHOT (a
429 loses the page until the 24h latency alert). Add a SEPARATE delivered-signal — a new column
on `decision_requests` (migration **0004**, e.g. `published_at`) — + a per-tick re-publish of
pending-undelivered decisions (mirror the `_OutOfBoundsDetector._published` streak at
`scheduler.py:761-884`, but DB-backed for restart-robustness). **CAREFUL: do NOT reuse
`alerted_at`** — it is the 24h-latency latch in `_decision_latency_alerts:5705`. Each tick: query
pending where `published_at IS NULL`, publish, on success set `published_at`, on failure log
`alert_delivery_failed` + retry next tick. The 24h `alerted_at` nudge stays independent.

**Item 7 — Layer 2 (vitest self-sizing) — SEPARATE deploy, ERP product code, NOT the factory
restart.** Drop the VERIFIED `l2MaxWorkers` (in 5p's handoff `## Verified Layer 2 logic` block,
unchanged) into `erp-workspace/frontend/vite.config.ts` `test:` block on ERP **`main`** (clean
ATOMIC commit — never leave the ERP tree dirty or the `_OutOfBoundsDetector` 429-bursts) +
`_factory/stages/foundation.skeleton/spec.md` (regeneration durability). main's `frontend/
node_modules` is ABSENT — `npm ci` first or verify standalone. **Systemic finding:** BACKEND
pytest ALSO gets OOM-killed under concurrency (stock-views #86/#89 root) — Layer 2 only covers
FRONTEND; backend needs its own cap OR lower `max_parallel_agents` (now live-editable via item 4).
Flag to founder; the memory panel surfaces it.

## DEPLOYED / LIVE STATE (snapshot 20-06 ~14:12Z — VERIFY, don't trust stale)
- **Production RUNNING, LEASHED.** Orchestrator pid `6071` (in `.factory/orchestrator.pid`), on
  `main` in-memory; cgroup `memory.max=22G + swap 2G = 24G`; **oom_kill=0**; ~2.1G used (wound
  down). Dashboard `http://100.69.221.108:8377/` HTTP 200. Verify via PIDFILE (not pgrep).
- **23 DONE, 3 ESCALATED (#87/#88/#89 — drained), 1 PENDING (phase-integration), 1 CANCELLED
  (stock-core).** 0 open decisions. ntfy 429-rate-limited (founder uses the dashboard).
- Launch recipe (for the restart): `tmux new-session -d -s factory -c /home/artur/projects/SF-F5
  'deploy/sf-cap.sh .venv/bin/sf-factory run 2>&1 | tee -a .factory/run-live.log'`.

## INFRA YOU INHERIT (details in 5p's / 5o's handoff)
- **Context-guard hook** follows the marker `~/.claude/sf-architect-session` (bytes/4.1, 500k).
  Fired for 5p at ~571k. **Your FIRST duty: write YOUR session id there.**
- **Monitor** `~/.claude/sf-architect-monitor.sh` — header says ETAPA-5p; **update → 5q**, launch
  via Bash `run_in_background:true` (NEVER nohup). **No monitor is currently running** (5p's exited
  on the #89 change and was not relaunched — clean; nothing to kill). Exit codes: 10 open-esc set,
  11 pending-dec, 12 orchestrator death, 13 5h limit, 14 routing/recurrence events. **Under the
  drain it fires exit 10 on every new escalation — expected; note, don't resolve.**
- `~/.claude/sf-limit.sh` (manual limit), `deploy/sf-cap.sh` (leash 22G+2G).

## WORKING-MODE LEARNINGS (carry forward)
- **MECHANICAL guarantees, not "I'm careful."** 5p verified the memory panel against the live
  scope, effective-tokens against live data, and migration 0003 against a copy of the live db.
- **Tight edit→verify→commit on the branch.** The OOB detector scans the factory root ONLY at
  `merge_gate`/`recover` (NOT continuously) — under the drain (no merges) a clean committed tree
  is safe. Keep dirty windows short. Run the affected pytest files after each change.
- **Verify via pidfile, never pgrep; never pkill-self.** Verify schema before DB queries
  (`stages.state` not `status`; `audit_findings.stage_id` not `unit_id`; `process_registry.state`).
- **Founder protocol:** Romanian, plain, his terms (cost/speed/risk), DD-MM-YYYY dates, long text
  → SendUserFile not a chat wall, concrete examples > theory, **NEVER AskUserQuestion**. Don't spam
  a message per drained escalation — batch the count into checkpoints.
- **Brutal honesty (§21).** Resolution reasons carry the WHY (architect-operations §2).

## YOUR SUCCESSION (later → ETAPA-5r)
Finish your work unit → write `docs/session-handoff-ETAPA-5r-DD-MM-YYYY.md` → launch ETAPA-5r via
`claude_canon.sh` → **VERIFY 5r's RC on the founder's phone (claude.ai/code, green dot) BEFORE
going silent** → hand the marker. Never two architects writing at once. Procedure:
`docs/runbooks/session-succession.md`.
