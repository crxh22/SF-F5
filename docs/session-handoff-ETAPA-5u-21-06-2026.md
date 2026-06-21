# Session handoff — ETAPA-5q → ETAPA-5u, 21-06-2026

**For ETAPA-5u (Main-Architect successor).** POINTER doc (Doctrine §9).
**FOUNDER DIRECTIVE (21-06, explicit):** your FIRST job is to investigate the 04:25 incident
**fully and in detail** — root cause, why it happened, why the prior OOM fix didn't hold. Only
AFTER that, unblock the factory. Do NOT unblock first.

Launch: opus, effort max, RC ON named ETAPA-5u, via `claude_canon.sh` (done by 5q). The chain
5q→5r→5s→5t all died (see below); 5u is the next free letter.
**FIRST duties (in order):** (1) write your session_id into `~/.claude/sf-architect-session`
(replace — the marker currently holds 5q's id, reclaimed manually). (2) Update the monitor header
(`~/.claude/sf-architect-monitor.sh`: 5q → 5u) and relaunch it via Bash `run_in_background:true`
(5q has one running under its id; yours replaces it once you take the marker). (3) **Protect
yourself — see "YOUR OWN SURVIVAL" — before doing ANY memory-heavy work.**

## Lineage (and the incident)
5q (built+deployed the dashboard batch + Layer 2, restarted the factory) → **5r** (0396fe67:
took over, RESOLVED the original queue #87/#88/#89, drove the factory to nearly finish inventory,
EXTENDED item 7 to a backend-pytest memory-admission gate in the ERP skeleton spec) → **5s**
(109b3105) → **5t** (launched, never took the marker). **5r + 5s + 5t ALL DIED at 04:25:14 on
21-06** — a memory/OOM cascade. 5p and 5q (idle, low footprint) survived. The founder woke up,
found no architect on his phone (all the active ones were dead), reached the still-alive 5q, and
directed this incident-first succession → **you = 5u**.

## ⚠️ JOB 1 — INVESTIGATE THE 04:25 INCIDENT (founder priority, do FIRST)

### What 5q already established (your scaffolding — verify, don't re-derive)
- **Simultaneous death at 04:25:14.** 5r (0396fe67) and 5s (109b3105) transcripts both stop at
  EXACTLY `2026-06-21 04:25:14`. 5t (recorded in `~/.claude/sf-architect-tmux` as `etapa-5t`) is
  also gone. Simultaneity ⇒ a single system-level event, not three independent failures.
- **The factory leash did NOT OOM.** The orchestrator's cgroup (under `deploy/sf-cap.sh`, 22G)
  shows `oom_kill 0` / `oom_group_kill 0`. So the kill came from OUTSIDE the factory leash — the
  user/system level (the architect tmux sessions are NOT under the factory leash). This matches the
  prior incident pattern (memory `oom-incident-19-06-2026`: a 29GB run in a lingering session →
  user-manager OOM-killed → cgroup cascade killed ALL architect tmux sessions at once).
- **Survival correlates with being IDLE, NOT with Scut.** The survivors 5p (pid 55902) and 5q
  (pid 108637) have `oom_score_adj = 0` — they are NOT Scut-protected (-1000). They lived because
  they were idle (small RSS → low oom_score). The dead ones (5r/5s/5t) were ACTIVE — running the
  monitor, resolving escalations, very likely spawning subagents and/or running tests (the backend
  pytest gate they were building OOMs by design under concurrency — Layer 2's whole point). An
  active architect + its children = large RSS = first picked by the OOM killer.
- **`uid=1000` cannot set a negative `oom_score_adj`.** Writing -1000 needs root/CAP_SYS_RESOURCE.
  So the documented "Scut" fix (oom_score -1000) was **never actually applied** to these sessions
  by a non-root launcher — a prime suspect for why the protection didn't hold. CONFIRM this.

### What YOU must pin down (the founder wants it "foarte clar și detaliat")
1. **The kernel OOM record.** `dmesg`/`journalctl` need sudo (5q at uid 1000 got nothing). Ask the
   founder to run, in this session, `! sudo dmesg -T | grep -iE 'killed process|out of memory'`
   (or `! sudo journalctl --since '2026-06-21 04:20' --until '04:30' | grep -i oom`). Identify the
   EXACT process(es) the kernel killed at 04:25 and the memory state then (`Killed process … total-vm…`).
2. **What SPIKED the memory.** Which process drove the box to the wall? Candidates: an architect
   subagent; a backend `pytest` run (the OOM-prone one — was the pytest memory-admission gate the
   5r/5s session was building actually IN FORCE yet, or did an uncapped run slip through?); a vitest
   run; the factory; an unrelated lingering process. Cross-reference the kill time with the factory
   `run-live.log`, the ERP test activity, and any subagent transcripts around 04:2x.
3. **Why the prior fix (Scut + Lesă) didn't protect the architects.** Leså (sf-cap.sh) only caps the
   FACTORY, not the architect sessions or their subagents. Scut (-1000) needs root and the survivors
   show oom_score_adj=0 — so was Scut ever wired into the launch path? Read `claude_canon.sh` (no
   oom_score logic in it) + check for any sudo/systemd drop-in. Conclusion likely: **architect
   sessions + their subagents have NO memory protection at all** — the Lesã/Scut from 19-06 covered
   the factory and possibly a one-off, not the recurring architect-session exposure.
4. **The durable fix (propose to the founder, mode 1 — his cost/risk call).** Options to weigh:
   (a) run the whole architect tmux session under a `systemd-run --user --scope -p MemoryMax=…` leash
   like the factory (bounds the session + its subagents; a hit kills only that session, not a cascade);
   (b) a real Scut via a tiny sudo helper or a systemd `OOMScoreAdjust=` drop-in (needs the founder's
   one-time sudo); (c) cap the memory-heavy CHILDREN (any test run) under a scope, never bare. Don't
   fix silently — present options + a recommendation.

### YOUR OWN SURVIVAL (do this BEFORE any heavy work — you are the next to die otherwise)
- Stay LEAN until the fix is in. Do NOT run bare `pytest`/`vitest`/`npm ci` or spawn many subagents
  on the host — that is exactly what killed 5r/5s/5t. Run ANY test/build under a scope, e.g.
  `systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=64M <cmd>` (5q used this for the Layer 2
  proof — it held). You cannot Scut yourself (uid 1000). Prefer reasoning + targeted reads over running
  the heavy suites until JOB-1's durable fix lands.

## ⚠️ JOB 2 — only AFTER Job 1: unblock the factory (small backlog, parked since 04:25)
- **Decision #20** — `stage` `inventory-procurement.stocktaking`, gate_kind `critical_stage`, pending
  since 02:50, published (item 6 works). Per the founder's 21-06 delegation (memory
  `founder-applies-approvals-via-architect`: the architect AUTO-APPROVES any val+audit-passed stage,
  ALL risk classes, without waiting for him) — if stocktaking passed validation+audit, approve it
  (`cli decide` / dashboard). Verify the val+audit-passed precondition first.
- **Escalation #97** — `inventory-procurement.stock-views`, `context_budget`, open since 01:48Z,
  already `escalation_bumped` (the orchestrator now HAS the D-0042 routing code — your monitor's
  exit-14 fires for real now). Resolve per architect-operations §2 (reset-vs-escalate, bounded by
  `escalation.max_context_resets`); the EFFECTIVE-tokens view (Detalii page) shows whether the burn
  was real or wasted on OOM-killed runs — directly relevant to Job 1.
- The factory (pid 140670) is otherwise healthy: 24 DONE, 1 AWAITING_HUMAN (the #20 stage), 1
  ESCALATED (#97), 1 PENDING, 1 CANCELLED. 0 other pending decisions.

## DEPLOYED / LIVE STATE (snapshot 21-06 ~05:40Z — VERIFY, don't trust stale)
- **Production RUNNING, LEASHED.** Orchestrator pid **140670** on `main`, tmux `factory`, under
  `deploy/sf-cap.sh` (22G), oom_kill=0, ~1.8G used. Schema version **4** (migrations 0003 runtime_settings
  + 0004 decision published_at applied at 5q's 20-06 restart). Dashboard + `/configurare` HTTP 200.
- **The dashboard batch is LIVE** (5q's three commits on main: live-config `EffectiveConfig` wiring,
  the ⚙ Configurare tab + write path, the decision-publish retry backstop). **Item 7** (Layer 2 vitest
  self-sizing) is on ERP main `9a701b1`; 5r extended it to a **backend pytest memory-admission gate**
  in `erp-workspace/_factory/stages/foundation.skeleton/spec.md` §3.5 (directly relevant to Job 1 —
  read it; it may or may not be IN FORCE as running code yet).
- **New live levers** (⚙ Configurare, no restart needed): `max_parallel_agents`, manual DRAIN, budgets,
  autodrenaj, 5h/7d thresholds, agent_timeout — `http://100.69.221.108:8377/configurare`. Lowering
  `max_parallel_agents` is the quick lever to reduce concurrent-agent memory pressure (relevant to Job 1).

## INFRA YOU INHERIT
- **Context-guard hook** follows the marker (bytes/5, 500k). Fired for 5q at ~753k. **First duty: write
  YOUR id there.**
- **Monitor** `~/.claude/sf-architect-monitor.sh` (header says ETAPA-5q reclaimed; update → 5u). Exit
  codes 10/11/12/13/14 (the D-0042 routing events fire for real now — the live orchestrator carries them).
- `~/.claude/sf-limit.sh`, `deploy/sf-cap.sh`. **`~/.claude/sf-architect-tmux`** records the last-launched
  session name for the auto-resume path — set it to your own name if you relaunch anything.

## WORKING-MODE LEARNINGS (carry forward)
- **MECHANICAL guarantees, not "I'm careful."** The founder rejects "no incident yet" — and 04:25 IS the
  incident. Cap heavy work under scopes; verify the OOM fix by REPRODUCING the pressure under the cap.
- **Verify via pidfile, never pgrep; never pkill-self.** Verify schema before DB queries. `ruff check`
  before commit (NOT `ruff format` — repo isn't format-clean).
- **Founder protocol:** Romanian, plain, his terms (cost/speed/risk), DD-MM-YYYY, copy-paste commands,
  long text → SendUserFile, concrete examples, **NEVER AskUserQuestion**, no context-stripped IDs.
- **Succession reality (this incident's lesson):** verify a successor SURVIVES, not just that it launched
  + took the marker. 5q verified 5r booted + took the marker + RC — but 5r died hours later. A launch
  check is not a survival guarantee; Job 1's fix is what makes the next handoff durable.

## YOUR SUCCESSION (later → ETAPA-5v)
Finish your work unit → write `docs/session-handoff-ETAPA-5v-DD-MM-YYYY.md` → launch via `claude_canon.sh`
→ **VERIFY the successor's RC on the founder's phone (green dot) AND that the OOM fix is in place so it
will SURVIVE** → hand the marker. Procedure: `docs/runbooks/session-succession.md`.
