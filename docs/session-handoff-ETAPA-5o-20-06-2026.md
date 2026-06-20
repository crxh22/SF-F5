# Session handoff — recovery session "sf-f5" → ETAPA-5o, 20-06-2026

**For ETAPA-5o (Main-Architect successor).** POINTER doc. Authoritative state =
`docs/decision-log.md` + the architect memory **`oom-incident-19-06-2026`** (READ IT FIRST —
it carries the OOM root cause + the protection mechanics) + this doc. Launch: opus, effort
max, RC ON named ETAPA-5o, via `claude_canon.sh`.

## Lineage (so the name makes sense)
5m → 5n (session `5b1f2b31`, crashed in the 19-06 server-wide OOM mid-production-start, never
wrote a handoff) → **this recovery session "sf-f5"** (session `2ad1e6fc`, launched post-crash;
did the OOM root-cause analysis + built & verified the protection + restarted production
leashed) → **you = ETAPA-5o** (ETAPA convention restored).

## ⚠️ YOUR MANDATE (founder, 20-06)
Production is LIVE and protected. **While you MONITOR it, do the founder's DASHBOARD work
(below) + the Layer-2 fast-follow.** Romanian + plain language to the founder, NEVER
`AskUserQuestion`, mechanical guarantees over attention, brutal honesty.

## DEPLOYED STATE (live snapshot 20-06 ~13:35Z — VERIFY, don't trust stale)
- **Production RUNNING, LEASHED.** Launch recipe:
  `tmux new-session -d -s factory -c /home/artur/projects/SF-F5 'deploy/sf-cap.sh .venv/bin/sf-factory run 2>&1 | tee -a .factory/run-live.log'`
  The orchestrator + ALL its agents + their tests run in ONE cgroup scope capped **22G RAM +
  2G swap = 24G ceiling** (founder-set). Dashboard: `http://100.69.221.108:8377/`.
  Snapshot: 22 stages DONE, ~4 active (VALIDATE/BUILD/AUDIT), 1 PENDING, **0 open escalations**,
  oom_kill=0, host 28G free.
- **SCUT (anti-OOM-cascade):** the user-session manager is protected `OOMScoreAdjust=-1000`
  via `/etc/systemd/system/user@.service.d/oom.conf` (survives reboot — proven across a clean
  reboot AND a power outage). This is what keeps YOUR architect session alive — architect
  sessions die from the systemd cgroup-teardown cascade, NOT from RAM; protecting the manager
  blocks the cascade. (oom_score_adj does NOT stop systemd's teardown — only manager-protection does.)
- **Watchdog + arch-resume:** both systemd timers `active`. Watchdog only ALERTS (ntfy), does
  NOT restart the orchestrator.
- **VERIFY the leash via the PIDFILE** (`.factory/orchestrator.pid`, the orchestrator
  self-reports), THEN check its cgroup `memory.max`. Do NOT use `pgrep -f sf-factory` — it
  catches transient recovery subprocesses in the tmux-spawn scope and gives FALSE "unleashed"
  alarms (cost me needless restarts). NEVER `pkill -f <pattern>` where <pattern> appears in
  your own command — it self-kills your shell.

## PENDING WORK

### 1. Layer 2 — adaptive self-sizing of vitest workers (fast-follow, founder-approved)
Each frontend validator runs `vitest run` at default ~15 workers (one per CPU on 16 cores).
Measured ~467 MB/worker (typical suite ~7G; the incident's 3-4G/worker was thrash-inflated).
Layer 1 (the 24G leash) is the hard backstop; Layer 2 keeps suites UNDER it so the cap rarely
trims (which would flake tests).
**Design (founder chose this over a static cap = wasteful, and over a full central
resource-ledger = premature):** each suite **self-sizes** `maxWorkers` to its FAIR SHARE of the
cgroup's REMAINING budget at launch — the cgroup itself is a free, leak-proof "resource ledger"
(the founder's idea, realized with no central coordinator + no reservation/leak problem). In
`vite.config.ts`: read `/proc/self/cgroup` → `memory.max − memory.current` → `maxWorkers =
budget_share / ~per_worker`; clamp to CPU count; fall back to host avail if uncapped.
**Where it STICKS:** the committed `frontend/vite.config.ts` `test:` block (shared across all
git worktrees) + `_factory/stages/foundation.skeleton/spec.md` (so factory regeneration keeps
it). Verify with a real suite run under a scope (method: `systemd-run --user --scope -p
MemoryMax=.. -p MemorySwapMax=.. bash -c 'cd <fe>; echo 0>/proc/self/oom_score_adj; vitest
run --maxWorkers=N'`, sample the scope's `memory.current`). The full resource-ledger stays the
NORTH STAR — revisit with real data if the simple version proves too crude.

### 2. Founder's DASHBOARD work (do while monitoring)
Dashboard is served by the orchestrator; changes deploy on a factory restart (editable install).
For the config tab the founder said **design the UI first** (propose → approve → code).
- **(a)** Per-stage token budget **editable live from the dashboard**. Define + show in-UI WHEN
  it applies (next agent only? the running one too?).
- **(b)** **"Configurare" tab**: every editable factory param, with GUARDS — e.g. cannot lower
  `max_parallel_agents` below the count currently running. For EACH param show in-UI text:
  editable live? when does the change take effect? what guard? **Design the UI first.**
- **(c)** Tokens shown **in thousands, no decimals**: `12.547.709 → 12.548`.
- **(d)** Separate **total** vs **effective** consumption: effective = total − tokens of agents
  that FAILED and delivered nothing (needs the DB to mark failed-no-delivery runs).
- **(e) NEW: manual DRAIN↔NORMAL switch** — a UI toggle to put the factory into drain regime
  (stop starting new agents; let the running ones finish) or back to normal, by hand.
- **(f) NEW: gate the auto-limit governor behind a flag `autodrenaj la limita`** — the existing
  proactive drain-near-limits mechanism (the P1 limit governor, commit `0e4bb4a`: holds new
  claude spawns at 5h≥80% OR weekly≥90%) must act ONLY when this flag is ON; toggle the flag
  from the dashboard. (So: (e) = manual override; (f) = the automatic behavior, now opt-in.)

## INFRA YOU INHERIT
- **Context-guard hook** `~/.claude/hooks/sf-architect-context-guard.sh` (`UserPromptSubmit`):
  injects a succession note past 500k est. **RECALIBRATED 20-06 to bytes/4.1** (was bytes/5,
  which under-read: 464k est vs 560k real → it stayed silent, the founder had to flag it).
  It follows the marker `~/.claude/sf-architect-session` (must equal YOUR session id —
  **your FIRST duty is to write your session id there**).
- **`~/.claude/sf-architect-monitor.sh`** — header still says ETAPA-5n. Update header → ETAPA-5o,
  then launch via the **Bash tool with `run_in_background:true`** (NEVER `nohup &`). Must grep the
  escalation-routing events (architect-operations §4) + `finding_recurrence` (exit 14) +
  orchestrator-death (exit 12). Kill any leftover monitor by EXACT cmdline, not `pkill -f`.
- **`~/.claude/sf-limit.sh`** — manual 5h/weekly OAuth limit check.
- **`deploy/sf-cap.sh`** — the leash wrapper (default MemoryMax=22G + MemorySwapMax=2G).
- **session-20 leftovers** (from the crash, harmless): an orphaned ERP dev postgres
  (`erp-workspace/.worktrees/inventory-procurement.stock-core/.devpg`) + a stuck pytest-poller
  bash loop, in `session-20.scope` (state "closing"). Cleanup pending founder OK.

## WORKING-MODE LEARNINGS (don't re-learn the hard way)
- **MECHANICAL guarantees, NOT "I'm careful"/"no incident yet."** Prove by running reality —
  I stress-tested the leash (it OOM-kills a runaway, exit 137, host safe); verify Layer 2 the same.
- **Brutal honesty (§21).** The founder rejects validation; give real risk/effort. I had to
  correct myself TWICE this session (both were me guessing instead of checking): "manual run, not
  the factory" → was the factory; "wrapping sf-factory run won't cap the agents" → it does
  (agents are bare-subprocess children → inherit the cgroup). Verify before asserting.
- **Verify via pidfile, never pgrep; never pkill-self.** (See DEPLOYED STATE.)
- Romanian, plain, his terms (cost/speed/risk/impact); founder-facing dates DD-MM-YYYY; long
  reference text → a file (SendUserFile), not a chat wall; concrete examples beat theory.

## YOUR SUCCESSION (later)
Finish your work unit → write `docs/session-handoff-ETAPA-5p-DD-MM-YYYY.md` → launch ETAPA-5p via
`claude_canon.sh` → **VERIFY 5p's RC on the founder's phone (claude.ai/code, green dot) BEFORE
going silent** → hand the marker. Never two architects writing at once.
