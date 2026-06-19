# Session handoff — ETAPA-5m → ETAPA-5n, 19-06-2026 (D-0037 context-guard succession @ ~514k)

**For ETAPA-5n (Main-Architect successor).** POINTER doc. **Authoritative state = `docs/decision-log.md` D-0059 (read FIRST, it is large + complete) + `docs/factory-management-inventory-19-06-2026.md`.** Launch: opus, effort max, RC ON named ETAPA-5n, architect-operations canon layer (all via `claude_canon.sh`).

## ⚠️ YOUR MANDATE (founder, 19-06 evening): PREPARE + EXECUTE the factory PRODUCTION STARTUP
The founder is **ready to move to production** — the whole robustness program he asked for is DONE (proactive limit mgmt, architect auto-resume, the inventory, both canon-ghost tools). He handed the production startup to YOU (5m hit context limit). **He has effectively confirmed "nothing else before production."** Your job: execute the production-resume plan below, keeping him in the loop for the actual start + the escalation resolutions (Romanian, `protocol_interactiune_founder`, NEVER `AskUserQuestion`). Do NOT silently re-decide settled things — if something looks off, ASK; do not invent (his words: "să nu inventeze ceva, să nu inducă drift").

## DEPLOYED-ON-RESTART (editable install — ALL go live the moment the orchestrator restarts)
Nothing below is live yet (factory paused). One `sf-factory run` restart delivers them together:
- **P1 proactive limit governor** (`0e4bb4a`): OAuth-polls usage every 5min; HOLDS new claude spawns at **5h≥80% OR weekly≥90%**, running agents finish, auto-resumes when back under (after reset). **Do NOT be alarmed when the factory pauses itself near a limit — that is the new mechanism working.** Dashboard shows "pauză proactivă de limită". Reactive governor (on signature) still backstops.
- **Tool 1 — documentary path `rework:SPEC_DOC`** (`8c9c943`): when YOU resolve a contest and the spec fix is TEXT-ONLY, use `rework:SPEC_DOC` (not `rework:SPEC`) — it skips BUILD (saves a full rebuild) but KEEPS VALIDATE+AUDIT as mechanical guards. Caveat: catches a wrong TIGHTENING, NOT a loosening (same as rework:SPEC). See architect-operations §1.
- **Tool 2 — recurrence backstop `finding_recurrence`** (`8c9c943`): fires mechanically when an audit re-raises a finding the SAME auditor already settled/overruled → dashboard Puls line + your monitor's **exit 14**. If it fires: return to the generating artifact, do NOT overrule again (architect-operations §1).
- **`ce03942`** merge-gate auto-recovery fix (rework:MERGE_GATE in `_CAPACITY_RESOLUTIONS`) — needed for [73].
- **`e0c31d8`** critical budget 280M (ONE-TIME for warehouse-issue; **REVERT to 250M after [73] DONE**).
- **`e888600`** stocktaking own_pj spec/IP2 (on its branch) — the [68] rework:BUILD re-runs at restart.

## THE PRODUCTION-RESUME PLAN (D-0058 §MORNING + D-0059) — execute in order
1. **Restart orchestrator** (delivers everything above; recovery re-drives the drain-killed stages — stocktaking own_pj build, supplier-fiscal-invoice):
   ```bash
   tmux new-session -d -s factory -c /home/artur/projects/SF-F5 '.venv/bin/sf-factory run 2>&1 | tee -a .factory/run-live.log'
   ```
   Verify it comes up (liveness fresh, dashboard live).
2. **Re-arm watchdog:** `sudo systemctl enable --now sf-factory-watchdog.timer` (currently `inactive`/disarmed — disarming was part of the clean pause).
3. **Start YOUR monitor** — FIRST edit its header `ETAPA-5m` → `ETAPA-5n` (line 2 of `~/.claude/sf-architect-monitor.sh`), THEN launch via the **Bash tool with `run_in_background:true`** (NEVER `nohup &` inside a Bash call). Kill any leftover monitor by EXACT cmdline (`ps -eo pid,comm,args` filtered `comm=bash` matching `sf-architect-monitor.sh`, then `kill` the pids — NOT `pkill -f`, NOT `grep -F` on the path). The monitor already greps the routing events + `finding_recurrence` (exit 14) + is pid-anchored for orchestrator-death (exit 12) — all done by 5m.
4. **Resolve [73] warehouse-issue** (target=founder, was bumped): `cli resolve-escalation 73 rework:MERGE_GATE --reason "..."` — hand-fix `2353b1e` + 280M already in place → reaches DONE. THEN **revert critical 280M→250M** in `factory.config.yaml` + commit (deploys next restart). His "pentru etapa asta" — cap+split-discipline stand for all other stages.
5. **Handle [78] returns-supplier-client** `unresolved_contest` (target=phase_architect, D-0049 pattern): read BOTH audit reports + the findings-response + the contest, classify per architect-operations §1, resolve.
6. **Verify stocktaking** own_pj_isnull rebuilt correctly (re-run from `e888600`) → validate → dual-audit → §9 → DONE. (It "cannot converge until own_pj verified+resolved" — founder-approved Option A, D-0058.)
7. **Carry-forwards** (product, not blocking): overdue-worklist → phase-integration; lot-substrate Option-A watch (service-orders/stock-views, D-0055/OPEN-WI4); ordering URL-split → phase-integration; reception OPEN-RCP5; rights-seam repoint before service-orders; UI-A3. Full list in the inventory doc §C/§D.

## NEW INFRA YOU INHERIT (know it exists — mostly nothing to do)
- **P2 architect auto-resume** (`sf-architect-resume.timer`, INSTALLED + active): an external systemd timer that WAKES you if you freeze on the limit during production (orchestrator running + your transcript stale >20min + an architect-targeted open escalation → send-keys to your tmux; founder page if your session died). NO-OP while the factory is paused. It reads `~/.claude/sf-architect-session` (your session_id) + `~/.claude/sf-architect-tmux` (your tmux name, written mechanically by `claude_canon.sh` at launch). You inherit it — nothing to configure. Cooldown 30min. Script: `deploy/sf-architect-resume.sh` (dry-run by default; `--act` is what the timer runs). First REAL wake under systemd is still unconfirmed (only fires in production when you're actually stuck).
- **`~/.claude/sf-limit.sh`** (direct OAuth limit query, exit 2 ≥threshold) — your manual limit check.

## STATE SNAPSHOT (19-06-2026 ~13:00Z)
- **Factory PAUSED**: orchestrator dead (stale pidfile `1590847`, liveness ~12h stale), watchdog `inactive`, `factory` tmux GONE. **5h limit FRESH ~1%** (resets ~13:09Z), weekly 36%.
- **Open escalations**: **[73]** warehouse-issue `context_budget` (target founder, prepped → rework:MERGE_GATE), **[78]** returns-supplier-client `unresolved_contest` (target phase_architect, untouched).
- **My commits this session**: `0e4bb4a` P1, `5afee6e` P2, `635525b` inventory+headers, `f946ee6` RO-translation+P2-install, `8c9c943` Tool1+Tool2. All on `main`, NOT pushed (factory runs from the working tree).
- **Out-of-repo artifacts** (deploy-env, NOT in git): `~/.claude/sf-architect-monitor.sh` (header at 5m — YOU update to 5n), `~/.claude/sf-limit.sh`, `~/.claude/sf-architect-session` (your marker), `~/.claude/sf-architect-tmux`.

## WORKING-MODE LEARNINGS (do NOT re-learn the hard way)
- **MECHANICAL guarantees, NOT attention** (founder, 19-06, memory [[mechanical-guarantees-over-attention]]): never defend a gap with "I'll be careful" (probabilistic/subjective) or "no incident yet" (empty — validation is only PARTIAL until run in reality). If a risk can be closed by a gate/audit/assertion the SYSTEM verifies, build that or state it's unguarded. He holds you to the doctrine.
- **Brutal honesty (§21)**: he wants the real risk/effort, not validation. He rejected my soft framing twice and was right.
- **Romanian, plain, his terms** (cost/speed/risk/impact); no context-stripped IDs; concrete examples beat theory; founder-facing files = DD-MM-YYYY; long reference text → a file (SendUserFile), not a wall of chat.
- **Factory infra is YOUR work** (the conveyor doesn't build its own orchestrator) — build it yourself + non-executor verify (Doctrine §4). Both Tool 1/2 were adversarially verified by a fresh agent; do the same for core changes.

## YOUR SUCCESSION (later): finish your unit, write the handoff, launch ETAPA-5o, **VERIFY 5o's RC on the founder's phone BEFORE going silent** (claude.ai/code green dot), hand the marker. Never two architects writing at once.
