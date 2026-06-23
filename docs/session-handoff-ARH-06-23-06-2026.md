# Session handoff — ARH-06 → ARH-07, 23-06-2026

**For the Main-Architect successor.** POINTER doc (Doctrine §9). ARH-06 deployed **4 fixes** (factory
RESTARTED, new code live), diagnosed a builder self-kill, and is **AWAITING a founder A/B budget
decision**. Durable memory: **[[erp-rebuild-structure-authored-23-06]]** + **[[factory-weekly-governor-usable-runway]]**
+ **[[never-prompt-matching-pkill]]** + **[[mechanical-guarantees-over-attention]]** + **[[founder-model-effort-policy]]**.

> ## ⛔ ABSOLUTE RULES (carry forward)
> 1. **NEVER `pkill -f` / `pgrep -f`** a pattern that can appear in a session's prompt — kills ALL
>    architect sessions at once. Stop a task ONLY by EXACT PID or EXACT tmux name.
> 2. **NEVER kill/exit a PREDECESSOR** (arh-03, arh-04, arh-05, arh-06). Leave attached + idle; the
>    FOUNDER retires them. (An architect kill drops the dashboard history.)

## NAMING — you are ARH-07
`ARH - 07` (phone RC label; tmux slug `arh-07`). Runbooks: `session-succession.md` + `session-launch-protocol.md`.

## FIRST duties (in order)
1. **Claim the marker.** Your id = newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/`
   containing a UNIQUE phrase from YOUR launch prompt (NOT mtime — predecessor transcripts also contain
   it; the LIVE one GROWS as a top-level USER turn; the scratchpad path also encodes your id). Write it
   into `~/.claude/sf-architect-session`, REPLACING `138f08d7-a857-42bd-9b08-807119ddbaa6`.
2. **Verify RC** (`ARH - 07` on the founder's phone). He is ONLINE + mid-decision — do NOT go silent.
3. **START the monitor** (factory is RUNNING — orchestrator present). `bash ~/.claude/sf-architect-monitor.sh`
   via Bash tool `run_in_background:true`. Bump its header `ARH-07`. Grep escalation events.
4. **Do NOT kill arh-03/04/05/06.**

## ⚠️ PENDING — founder A/B decision (handle FIRST, await his answer)
ARH-06 asked: **retry stage 2's builder NOW vs wait for the reset.** The builder self-kill is FIXED
(`c9aaa4e`), so a retry will NOT self-kill. Await his answer:
- **A (ARH-06 recommended):** raise `governor.seven_day_threshold_pct` to ~99 (dashboard `/configurare`
  or runtime_settings) so the governor permits the spawn (weekly 97% < 99%), THEN lift drain
  (`curl -s -X POST http://100.69.221.108:8377/configurare --data 'drain_manual=normal'`). Stage 2 (at
  BUILD, held) retries (~1.5% of weekly → ~98.5%). **WATCH live** (`~/.claude/sf-limit.sh` is now
  reliable) and **RE-DRAIN instantly** (`--data 'drain_manual=drenaj'`) if weekly nears 100%.
- **B:** wait for the 25-06 reset (zero hard-wall risk).

## 🏭 STATE (verify fresh)
- Factory **RUNNING** (orchestrator pid was 946867 in tmux `factory`; RESTARTED this session with new
  code) + **DRAINED** (`drain.manual=true`). Dashboard `http://100.69.221.108:8377`. Watchdog ARMED
  (SYSTEM systemd timer). `max_parallel_agents=1` (founder set). `proving_phases=[l0-shell]` → only L0
  runs; the 38-stage build is GATED.
- **`l0-shell.menu-registry` = DONE** (L0 screen 1 merged — Tier-2 clean). **`l0-shell.mount-orphans-home`
  = BUILD, held** (escalation #4 `agent_run_failed` RESOLVED `rework:BUILD` — retry queued behind drain+governor).

## 🚨 CAPACITY — the HARD WALL (read [[factory-weekly-governor-usable-runway]])
- Weekly **97%** (== governor threshold; founder raised 95→97). **`extra_usage` = 100% used / DISABLED
  (`org_level_disabled_until`) → NO overflow buffer → 100% weekly is a HARD STOP that cuts off YOUR
  architect session too** (founder phone-only — he CANNOT restart you). Reset **25-06 ~03:00 UTC**. 5h ~13%.
  BE LEAN. Never approach 100%.

## ✅ DEPLOYED THIS SESSION — commits on SF-F5 main (all live at the restart)
- `035bc16` + `4285545`: ruff-format `scripts/test_pg_socket_dir_matches.py` on **main + phase/l0-shell**
  — the merge-gate format landmine (`test_quality.py::test_ruff_format` checks the WHOLE repo; one
  unformatted file bounced EVERY stage's Tier-1, `tests_failed=true`+`rebase_conflict=false`). **The merge
  gate rebases onto `phase/<id>`, NOT main** — fix the phase branch (ARH-06's first fix wrongly went to main only).
- `3b10695`: **agent-level manual drain** (the founder's requested correction). Was stage-level
  wind-down (`_dispatch` only); now each `execute()` loop holds at the AGENT spawn boundary — in-flight
  agents finish, NO new spawn; `_dispatch` `drain_lifted` re-dispatches on lift. 177 scheduler tests.
- `303f7f5`: **shared serve-stale cache for the OAuth usage query** (`sf_factory/usage.py`) — collapses
  the governor+monitor+poller onto ONE cache file; serves the last value on failure (no more 429-blind
  drain). `~/.claude/sf-limit.sh` ALSO replaced (out-of-repo, uncommittable) to import it. 10 tests.
- `c9aaa4e`: **forbid `pkill -f` in the §4 visual-capture cleanup** — a frontend builder ran
  `pkill -f "vite"`, self-matched its own cmdline, SIGTERM'd itself (exit 143), discarded the whole stage
  (incident 23-06, mount-orphans-home BUILD). `ui-ux-laws.md §4` now mandates exact-PID/PGID kill;
  injected into every frontend agent.
- ARH-05's `730fcd2` grounding fix is ALSO live now — **VALIDATED**: stage 2's spec converged in ~2
  spec-audit rounds vs stage 1's 3 rounds + cap. The grounding works.

## RESTART procedure (verified this session)
Disarm watchdog `sudo systemctl disable --now sf-factory-watchdog.timer` (sudo IS passwordless) → graceful
stop `tmux send-keys -t factory C-c` (the tmux `factory` session CLOSES when its cmd exits — recreate it)
→ `tmux new-session -d -s factory -c /home/artur/projects/SF-F5` + `tmux send-keys -t factory 'export
PATH="/home/artur/.local/bin:/home/artur/.nvm/versions/node/v24.16.0/bin:$PATH"; .venv/bin/sf-factory run
2>&1 | tee -a .factory/run-live.log' Enter` → re-arm `sudo systemctl enable --now sf-factory-watchdog.timer`.
Verify: dashboard bound + recovery complete + new pid + state preserved. Keep drain ON across a restart.

## OPEN / NEXT
- The founder A/B (above) is the immediate item.
- After the 25-06 reset: retry stage 2 → finish L0 → founder tests the 2 screens → then the 38-stage build
  (widen/empty `proving_phases` after L0 sign-off; per-layer founder gate = drain-lift).
- Escalation CLI: `.venv/bin/sf-factory resolve-escalation <id> <token> --reason "<why>"`
  (tokens: rework:SPEC | SPEC_DOC | BUILD | VALIDATE | MERGE_GATE | awaiting_human | cancelled | failed | respec).

## WORKING MODE / SUCCESSION
Romanian to founder, plain, DD-MM-YYYY, copy-paste, **NEVER `AskUserQuestion`**, no bare IDs, brutal
honesty over validation. **Architect commits to main**, `ruff check` before commit, VERIFY (diff+tests)
before merge. When YOU hand off, follow `session-launch-protocol.md` verbatim (auto-launch `ARH - 08`;
never kill predecessors).
