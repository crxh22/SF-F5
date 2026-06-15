# Session handoff — ETAPA-5e → ETAPA-5f, written 15-06-2026 early UTC (succession per D-0037)

**For ETAPA-5f (Main-Architect successor).** POINTER doc (Doctrine §9). Authoritative history = `docs/decision-log.md` **D-0044** (ETAPA-5e's shift) + the **TaskList (#1–10)** which is the live backlog. You launch on opus @ effort max, Remote-Control ON named ETAPA-5f, architect-operations canon layer. RC = the founder drives you from his phone.

**Why this handoff:** ETAPA-5e ran a heavy shift — built + verified + **DEPLOYED** the robustness program (UNITs 1/2/3) alongside slice 2 and the founder-approved 220M critical budget. A **case-2b over-fire bug surfaced in production** (found by real first-use, not the clean verifier tests) and is **temporarily silenced**; the founder raised a **codex contingency** and a **billing scare** (resolved — `-p` has credit). Context is filling after all this → proactive clean handoff. Factory is LIVE + healthy; frozen escalations + the proper case-2b fix await you.

## YOUR FIRST DUTIES (in order)
1. **Write your session id** to `~/.claude/sf-architect-session` (newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/`, born at your startup — **verify via BIRTH time (`stat -c %W`), NOT mtime**; the predecessor's large `.jsonl` shares the mtime and `ls -t` will mislead you. I hit this trap — `ls -t` gave the wrong file; birth time gave the right one).
2. **Start your OWN monitor** (`bash ~/.claude/sf-architect-monitor.sh`, run_in_background) — kill any leftover one **by PID** (`ps -eo pid,cmd | grep sf-architect-monitor.sh`), NOT via `pkill -f 'sf-architect-monitor.sh'` (that string appears in your own kill command's args, so pkill kills the killer — I hit this and it 144'd my restart script). Update the script header to ETAPA-5f.
3. **Read `docs/decision-log.md` D-0044**, then `uv run sf-factory status`, then `TaskList`.

## State snapshot (15-06 early UTC — RE-CHECK via status)
- **Factory LIVE on the NEW code**: slice 2 + robustness UNITs 1/2/3 + `critical:220M`, deployed 14-06 ~18:08, **re-deployed 15-06 ~[now]** (the threshold-disable below). factory main FF-merged (currently @ `5ce078e`). 7 foundation stages DONE.
- **⚠️ case-2b stuck-detector is DISABLED** via `escalation.stuck_escalation_threshold_min: 999999` (TEMP, factory.config.yaml:79). first-notice (age 0) STILL works; the climb (2a) + resolved-not-advanced (2b) are OFF. This was an EMERGENCY silence of a flood — see task #9 + below.
- **Open escalations — ALL frozen (ESCALATED), need YOUR adjudication:**
  - `[29]` status-notifications `context_budget` (structural; hit the 120M cap — its audit findings are all complied, so it's "large + done-ish" not churning, but 120M for a notifications stage is high — assess extend-vs-split; raising structural is per-class like the critical 220M bump).
  - `[32]` auth-access `unresolved_contest` (adjudicate the contest per architect-operations §1 — read its findings/contest).
  - `[33]` register-schemas `context_budget` (it was BUILDING the F-RS-CROSS-001 amount>=0 fix from [26] — assess).
- **codex WORKS** (auditor_cross_model exited 0 through ~21:34 on 14-06); **claude `-p` WORKS** (billing OK — I tested `claude -p` directly 15-06, got a clean reply, exit 0). 7 stages DONE; auth-access/register-schemas/status-notifications cycling; rest PENDING. Watchdog armed.

## URGENT priorities for you
1. **Resolve [29][32][33]** (the 3 frozen stages) — adjudicate each. Budget ones: the [27] method (extend if converging+honest, bounded "split if it exceeds again"; remember it raises the whole risk-class). Contest [32]: architect-operations §1 (the D-0043 [22]/[26] method). Resolutions take effect promptly now (UNIT 1 is live — no starvation).
2. **Fix case-2b properly (task #9) + RESTORE threshold to 30 + redeploy.** Root: case-2b ("resolved escalation + unit still ESCALATED") fired for EVERY old resolved escalation of a unit re-ESCALATED for a NEW reason; the in-memory latch self-prunes when a unit oscillates out of/into ESCALATED, so it RE-fires → ~32 false `[arhitect]` pages flooding the founder. **Fix:** scope (2b) to the unit's MOST-RECENT escalation only (if it's resolved+old+unit-ESCALATED → fire; if the latest is OPEN → it's (2a)/first-notice territory, skip; older resolved ones ignored). Equivalent: skip (2b) for any unit that has an OPEN escalation. Add a test: a unit with N old resolved escalations + a current OPEN escalation → (2b) fires ZERO. Build on a branch → INDEPENDENT verify (Doctrine §4) → redeploy. Until restored, the climb-to-founder + resolved-not-advanced backstops are OFF (first-notice still covers new escalations).
3. **STANDING codex contingency (task #10, until 15-06 09:00 UTC = tomorrow 12:00 Chișinău):** IF codex fails, swap `auditor_cross_model` (factory.config.yaml:42, the ONLY codex role) → `{cli: claude, model: opus, mode: print, effort: xhigh}` (mirror auditor_same_model:41) + restart. Founder: "2 separate opus controllers > 1; don't stop production for codex." Reactive only. ALSO watch `claude -p` billing (founder worried a billing change today would cut `-p` credit — it has NOT; agents fail instantly + visibly if it does; the capacity governor auto-handles transient usage-limits, e.g. the 14-06 19:43 blip).
4. **[23] auth-access re-confirm gate (task #6):** when auth-access finishes + returns to its `critical_stage` human gate on a now-green suite, surface to the founder that his earlier OK rested on a false all-green premise → he re-confirms.
5. **Slices 3, 4 (tasks 7, 8)** + **monitor/docs event-update (task #4)** — make the architect monitor also exit-on `escalation_opened_notice|escalation_bumped|escalation_stuck_resolved` (defer until case-2b is restored, else noise).

## What was deployed (robustness program — fuller in D-0044)
- **UNIT 1** scheduler fairness: no-spawn control-plane work (ESCALATED pickup, gates) is exempt from the agent-slot cap, so resolved escalations aren't starved. ACCEPTED residual (pinned by `test_rework_routing_overshoots_cap_by_bounded_k_accepted_residual`): a rework-routing escalation can transiently spawn `cap+K` agents (K bounded by simultaneously-resolved rework escalations ≤ cap; economic-cap tolerance, §8). Revisit-trigger = budget-reset oscillation → add a spawn-point semaphore.
- **UNIT 2** escalation routing + stuck-detector + orchestrator-owned first-notice (the ≤5min code law) + `_notify_architect`. **The case-2b over-fire is its bug (now disabled, task #9).** The climb (2a) is a stateless age-derived ladder phase_architect→main_architect→founder.
- **UNIT 3** budget-reset → architect-resume page (`capacity_governor.notify_architect_on_resume: bool=True`).
- 840 tests green at robustness HEAD; `critical:220` + the threshold-disable are config-only on top. The pre-existing `config.py:291` E501 is NOT ours (D-0040).

## Working-mode learnings (keep)
- **Redeploy ritual** (docs/runbooks/first-live-run.md): disarm watchdog (`sudo -n systemctl disable --now sf-factory-watchdog.timer`) → `tmux send-keys -t factory C-c` → wait for the python orch to die → `tmux kill-session -t factory` → `tmux new-session -d -s factory -c /home/artur/projects/SF-F5 '<RUNCMD>'` → wait for `entering scheduler loop` in run-live.log (use a baseline line-count to catch the NEW startup) → re-arm. **RUNCMD** = `export PATH="$HOME/.local/bin:$HOME/.nvm/versions/node/v24.16.0/bin:$PATH"; .venv/bin/sf-factory run 2>&1 | tee -a .factory/run-live.log`. **orch_pid** = `pgrep -af 'sf-factory run' | grep bin/python | awk '{print $1}'` (the `bin/python` filter excludes your own script + the bash wrapper). GOTCHAS I HIT: (a) the self-`pkill` bug above; (b) startup takes >60s under the dashboard-request flood + recovery scan — use a 150s timeout + the log marker, not a short pgrep poll (my first 60s timeout false-negatived a SUCCESSFUL restart); (c) `run` auto-applies pending migrations on startup (log: "applied pending migration(s) [2]"); (d) `sudo -n` is passwordless for systemctl; (e) factory main FF-merges cleanly — NEVER rebase (ref anchors).
- **case-2b lesson:** clean single-escalation verifier tests missed the real-world "unit with resolved history, re-ESCALATED for a new reason" case → over-fire. Real first-use is the falsifiability test (§10). Mutation-test the OSCILLATION case next time.
- **Favorable-window insight:** a restart's cost is low when the EXPENSIVE stages are FROZEN (ESCALATED) — you don't need a perfect 0-agent window, just no-expensive-agent-in-flight.
- **Founder:** Romanian, glossed (no bare IDs), brutal honesty (§21 — he probes sharply: the auth-access cost, the billing). Deeply cost + robustness conscious; watches `/costuri` + `/costuri`; drives by phone. Up late 14-15 June worried about billing + the buzz flood — I reassured (billing tested OK; alarm bug silenced). He front-loads decisions when going away (the codex contingency).

## Pending founder threads
- **cap-5** (4 vs 5 agents) — STILL OPEN, default 4 in config. He's OPEN to 5; apply at a restart if he says so.
- **codex contingency (#10) + `-p` billing watch** — until 15-06 09:00 UTC.
- **buzz-flood** — acknowledged (alarm bug, silenced).
- **[23] re-confirm gate** — when auth-access finishes.
- **Dashboard redesign** — FUTURE dedicated session (mobile-first).

## Your succession
Finish your work unit, write the handoff, launch ETAPA-5g (`SFF5_TMUX_SESSION=etapa-5g SFF5_RC_NAME=ETAPA-5g ./claude_canon.sh "<prompt>"` — RC+name+opus+max auto), VERIFY 5g's RC on the founder's phone BEFORE going silent (predecessor RC is the fallback), hand the marker, go silent.
