# Session handoff — ETAPA-5o → ETAPA-5p, 20-06-2026

**For ETAPA-5p (Main-Architect successor).** POINTER doc (Doctrine §9). Authoritative state:
- architect memory **`dashboard-mandate-20-06-2026`** — founder's refined dashboard reqs + the systemic test-OOM finding. **READ FIRST.**
- architect memory **`oom-incident-19-06-2026`** — OOM root cause + Scut/Lesă leash mechanics.
- **`docs/design-configurare-dashboard-20-06-2026.md`** — the Configurare design proposal (sent to founder, key decisions approved).
- **`docs/session-handoff-ETAPA-5o-20-06-2026.md`** — the PRIOR handoff: infra (monitor/marker/leash/sf-limit/sf-cap), DEPLOYED leash recipe, gotchas. **Still valid — read for infra.**
- `docs/decision-log.md` — history.
- this doc — the **delta**: the DRAIN strategy + the build plan + the live state.

Launch: opus, effort max, RC ON named ETAPA-5p, via `claude_canon.sh` (done by 5o).

## Lineage
5n (crashed in the 19-06 server OOM) → recovery "sf-f5" → **ETAPA-5o** (this session: claimed the guard, verified+stabilized production, resolved 2 escalations, verified Layer 2, delivered the dashboard design, aligned the founder on the build + the drain) → **you = ETAPA-5p** (build the dashboard).

## ⚠️ THE DRAIN STRATEGY (founder, 20-06 — CRITICAL, NEW, ACTIVE NOW)

The founder directed an **operational drain** for a clean factory restart — **no feature, behavioral**:
- **DO NOT resolve ANY architect escalation.** Leave them OPEN/queued until AFTER the restart. Resolving spawns new agent work (re-spec/re-build) that a restart would lose; postponing lets the factory wind down to quiescence.
- The monitor catches escalations → you **NOTE + summarize to the founder, but DO NOT resolve**. Expect the orchestrator stuck-detector to **bump** queued escalations after ~30 min (climbs phase_architect→main_architect→founder, pages `[arhitect]`) — **that is NORMAL**, just the queue growing; the founder knows. Don't be alarmed; don't resolve to silence it.
- It is **SAFE**: an escalated stage sits paused and ships NOTHING.
- Founder **human-gates** (`decision_requests`, AWAITING_HUMAN) stay the FOUNDER's — don't touch.
- **When the dashboard code is ready to deliver:** let in-flight agents work until a good restart moment, then **decide WITH the founder** ("ne orientăm atunci"): accept minor in-flight loss OR wait for a stage to advance to a safe point. Restart → **THEN resolve the whole escalation queue** with the new code.

Caveat: 5o resolved esc **#85** (stocktaking) and **#86** (stock-views) BEFORE the founder gave the drain directive — those are legitimately in-flight (see below). The drain applies to escalations from ~11:45Z 20-06 onward.

## THE DASHBOARD BUILD (the work) — founder-approved, build in this ORDER, ONE timed restart

Full reqs: memory `dashboard-mandate-20-06-2026` + the design doc. Dashboard = stdlib `http.server`, server-side rendered, ALL in `src/sf_factory/dashboard.py` (~3125 lines), deploys on factory restart. Order (safe→deep):

1. **Read-only panels first (lowest risk):**
   - (c) **Tokens in thousands, no decimals**: `12.547.709 → 12.548`. Pure display.
   - **Memory panel** (founder's #1 — "ne limităm la ce e simplu"): free server RAM (`/proc/meminfo` MemAvailable) + leash limits (orchestrator pidfile → cgroup `memory.max`/`memory.swap.max` via the parent-walk 5o used) + factory current use (`memory.current`). Per-agent RSS ONLY if simple (`process_registry.pid` → `/proc/<pid>/status` VmRSS).
   - **Per-agent start/finish/duration** (from `events` spawn+exit timestamps / `process_registry`).
   - **Per-stage "Detalii" button in "Acum în lucru"**: full stage history (reuse the Plan & istoric render) + the CURRENT agent + a clear result row after each agent + **audit findings details** (status accepted/contested + nature + content — render the report/contest artifact FILE content; paths in `audit_findings.report_artifact_id`/`contest_artifact_id` → `artifact_refs.path`).

2. **Spine — runtime-settings layer:** config is load-once (`config.py:489`); live-edit needs a DB-backed `runtime_settings` table (key→value+audit) the scheduler reads each tick and applies to its working `self._cfg`. Survives restart. Design doc §1. Required by items 3–5.

3. **Effective-token accounting + budget-on-EFFECTIVE (founder decision):** effective = total − tokens of agent runs that FAILED and delivered nothing. **`token_ledger` HAS `process_id`** (`db.py:647`) → join `process_registry` exit outcome → likely derivable WITHOUT a migration. Wire into BOTH (d) the display AND the **budget TRIGGER** (`thresholds.py:341 _check_context_budget` — change the token-sum SQL to effective). Founder: budget applies to EFFECTIVE, not total.

4. **Configurare tab (editable, uses the spine):** budgets (3 risk-class), `max_parallel_agents` (guard: NOT below currently-running count — read at `scheduler.py:5536`), governor thresholds, `agent_timeout_s`. Each shows in-UI "live? / when applies / guard". Budget live-edit applies to the running stage at its next check (`thresholds.py:351` reads per-tick).

5. **(e) manual DRAIN↔NORMAL switch + (f) "autodrenaj la limită" flag** (gate the proactive governor `scheduler.py _proactive_limit_tick` behind it). Both via the spine. This is the PERMANENT drain feature (distinct from the founder's current manual drain = you postponing escalations).

6. **ntfy decision-page retry fix (founder-approved):** `scheduler.py:2911 _publish_decision` is ONE-SHOT — a 429 loses the page until the 24h latency alert. **CAREFUL:** `alerted_at` doubles as the 24h-latency latch (`scheduler.py:5742 _check_decision_latency`) — do NOT naively reuse it for delivery-tracking or you kill the 24h nudge. Add a SEPARATE delivered-signal + a per-tick re-publish of pending-undelivered decisions (mirror the out_of_bounds `_published` streak pattern at `scheduler.py:760-885`, but DB-backed for restart-robustness). Design it; don't rush.

7. **Layer 2 (vitest self-sizing) — SEPARATE from the factory restart (ERP product code):** the cgroup-budget computation is **VERIFIED** by 5o under real memory scopes (uncapped→16, 4G→3, 1G→1, 10G→9, self-throttles when remaining shrinks). Drop the verified logic (below) into `/home/artur/projects/erp-workspace/frontend/vite.config.ts` `test:` block on ERP **`main`** (clean ATOMIC commit — never leave the tree dirty or the `_OutOfBoundsDetector` 429-bursts) + `_factory/stages/foundation.skeleton/spec.md` (regeneration durability). Protects FUTURE phases; current phase covered by the leash. **Systemic finding:** BACKEND pytest ALSO gets OOM-killed under concurrency (that was stock-views #86's root cause — exit 137) — Layer 2 only covers FRONTEND; backend needs its own memory-aware cap OR lower `max_parallel_agents`. Flag to founder; the memory panel surfaces it.

### Verified Layer 2 logic (drop into vite.config.ts `test:` — minWorkers/maxWorkers; env-overridable, Doctrine §14)
```js
// adaptive maxWorkers from the cgroup's REMAINING memory budget (the founder's
// "free resource ledger"); falls back to host MemAvailable when uncapped. Verified
// under real scopes by ETAPA-5o. Constants env-overridable.
import * as fs from "node:fs"; import * as os from "node:os";
function l2MaxWorkers() {
  const PER = parseInt(process.env.VITEST_BYTES_PER_WORKER||"")||512*1024*1024;
  const FRAC = parseFloat(process.env.VITEST_BUDGET_FRACTION||"")||0.5;
  const cpu = (os.cpus()||[]).length||4;
  const rd = (p)=>{try{const s=fs.readFileSync(p,"utf8").trim();return s==="max"?Infinity:parseInt(s,10);}catch{return NaN;}};
  let rel=null; try{const l=fs.readFileSync("/proc/self/cgroup","utf8").split("\n").find(x=>x.startsWith("0::"));if(l)rel=l.slice(3).trim();}catch{}
  let remaining=NaN; const root="/sys/fs/cgroup";
  if(rel){let d=root+(rel==="/"?"":rel);while(d.length>=root.length){const m=rd(d+"/memory.max");if(Number.isFinite(m)){const c=rd(d+"/memory.current");remaining=m-(Number.isFinite(c)?c:0);break;}if(d===root)break;d=d.slice(0,d.lastIndexOf("/"))||root;}}
  if(!Number.isFinite(remaining)){try{const m=fs.readFileSync("/proc/meminfo","utf8").match(/MemAvailable:\s+(\d+)\s+kB/);remaining=m?parseInt(m[1],10)*1024:os.freemem();}catch{remaining=os.freemem();}}
  return Math.max(1,Math.min(cpu,Math.floor((remaining*FRAC)/PER)));
}
// in defineConfig test: { ...existing, pool:"forks", maxWorkers:l2MaxWorkers(), minWorkers:1 }
```
Verify after landing: `systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=64M bash -c 'cd <fe>; echo 0>/proc/self/oom_score_adj; node_modules/.bin/vitest run'` and sample the scope's `memory.current` (a real suite stays under). NOTE: main's `frontend/node_modules` is ABSENT — `npm ci` first, or verify the computation standalone (5o's method: a node script of `l2MaxWorkers` under scopes).

## DEPLOYED / LIVE STATE (snapshot 20-06 ~11:46Z — VERIFY, don't trust stale)
- **Production RUNNING, LEASHED.** Orchestrator pid in `.factory/orchestrator.pid`; cgroup `memory.max=22G + swap 2G = 24G`; oom_kill was 0. Dashboard `http://100.69.221.108:8377/` HTTP 200. Launch recipe + verify-via-pidfile (NOT pgrep, never pkill-self): see 5o's handoff.
- **22/28 stages DONE.** In-flight: stocktaking [BUILD] (re-spec'd from #85 — building the storno money-bug fix + UI scope-out), stock-views [BUILD] (retry from #86), returns-supplier-client, phase-integration [PENDING], supplier-fiscal-invoice [DONE], stock-core [CANCELLED 16-06, intentional].
- **0 open escalations, 0 pending decisions** at handoff.
- **ntfy is 429-rate-limited** (founder pages failing — that's why the fix #6 matters; meanwhile the founder uses the dashboard + your monitor doesn't depend on ntfy).

## ESCALATIONS 5o RESOLVED (context for the queue)
- **#85** stocktaking `unresolved_contest` → **`rework:SPEC`**. ST-UI-DIRECT-LINE-001 (UI can't enter zero-record surplus) scoped-OUT to a sibling per spec §9 (founder confirmed "îl lăsăm așa"); ST-STORNO-TO-VERSION-001 = a REAL money bug (storno-to-version reposts a 90%-wrong R6 personal debt — re-decide OPEN-ST4 or F2 contract-change); ST-DRAFT-EDIT-ADDBACK-002 local fix. Now re-building.
- **#86** stock-views `agent_run_failed`/timeout (2nd time) → **`rework:BUILD`**. Root cause: BACKEND pytest OOM-KILLED (exit 137) under concurrent test load (the leash trimming) — NOT too-big/stuck. Retrying under light load. **If it times out a 3rd time → reduce concurrency** (`max_parallel_agents`), do not blind-retry (Doctrine §11).

## INFRA YOU INHERIT (details in 5o's handoff)
- **Context-guard hook** follows the marker `~/.claude/sf-architect-session` (bytes/4.1, 500k). **Your FIRST duty: write YOUR session id there.**
- **Monitor** `~/.claude/sf-architect-monitor.sh` — header says ETAPA-5o; update → ETAPA-5p, launch via Bash `run_in_background:true` (NEVER nohup). It exits-to-reinvoke-you on: open-escalation set change (10), pending-decision change (11), orchestrator death (12), 5h limit (13), routing/recurrence events (14). **Under the DRAIN it will fire on every postponed escalation — expected; note, don't resolve.**
- `~/.claude/sf-limit.sh` (manual limit), `deploy/sf-cap.sh` (the leash, 22G+2G).

## WORKING-MODE LEARNINGS
- **MECHANICAL guarantees, not "I'm careful."** 5o verified the leash + Layer 2 by RUNNING reality.
- **Verify via pidfile, never pgrep; never pkill-self.** Verify schema before DB queries (5o hit `status` vs `state`, `unit_id` vs `stage_id`, missing `process_id` col).
- **Brutal honesty (§21).** 5o corrected the founder's premise that a drain button existed (it doesn't yet — it's what the restart installs).
- **Resolution reasons carry the WHY** (architect-operations §2) into the re-entered agent's prompt — name file/line/contradiction.
- Romanian/plain to founder, his terms (cost/speed/risk), DD-MM-YYYY dates, long text → SendUserFile not a chat wall, concrete examples > theory, NEVER AskUserQuestion.

## YOUR SUCCESSION (later → ETAPA-5q)
Finish your work unit → write `docs/session-handoff-ETAPA-5q-DD-MM-YYYY.md` → launch ETAPA-5q via `claude_canon.sh` → **VERIFY 5q's RC on the founder's phone (claude.ai/code, green dot) BEFORE going silent** → hand the marker. Never two architects writing at once. Procedure: `docs/runbooks/session-succession.md`.
