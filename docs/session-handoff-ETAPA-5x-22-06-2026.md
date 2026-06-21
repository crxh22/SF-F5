# Session handoff — ETAPA-5x → ETAPA-5y, 22-06-2026

**For the Main-Architect successor.** POINTER doc (Doctrine §9). 5x hit the context guard (~508k).
Launch via `claude_canon.sh` (opus, effort max, RC ON — see `docs/runbooks/session-succession.md`).

> ## ⛔ ABSOLUTE RULE (carry forward — it killed 5u/5v/5w; now also in memory [[never-prompt-matching-pkill]])
> NEVER `pkill -f` / `pgrep -f` (or any broad kill) with a pattern that can appear in a session's
> launch prompt (`sf-architect-monitor.sh`, `sf-factory`, `sf-cap`, `orchestrator`, `stock-views`,
> any factory/stage word). It matches the FULL cmdline and kills ALL active architect sessions at
> once. Stop a background task by EXACT PID (verify `/proc/<pid>/cmdline`) or let it die with the
> session. The session monitor uses PID-anchored liveness — keep it that way.

## FIRST duties (in order)
1. Write your session_id into `~/.claude/sf-architect-session` (replace `3f565f75-429c-4d12-92e1-60365e2e5863`). Your id = newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/` containing your launch prompt.
2. Update `~/.claude/sf-architect-monitor.sh` header (5x→5y) + relaunch via Bash `run_in_background:true` (5x's monitor dies with 5x — RELAUNCH it). Logic is sound; only the header comment needs the bump.
3. Verify your RC shows on the founder's phone (`ETAPA-5y`) before 5x goes silent.
4. Do NOT pkill anything (rule above).

## STATE SNAPSHOT (verify fresh)
- Orchestrator pid `.factory/orchestrator.pid` (140670), alive, under `deploy/sf-cap.sh`, tmux `factory`. Watchdog timer active. Passwordless sudo available.
- **inventory-procurement phase = INTEGRATING** (RUNNING→INTEGRATING @ 02:20Z 22-06). ALL stages DONE — including `phase-integration` (last stage), merged `716b2ba`. **A phase-level `phase_signoff` decision_request is likely imminent** → auto-approve per delegation IF clean (verify first; the monitor fires exit 11 on the pending decision). 0 open escalations, 0 pending decisions at handoff.
- Watch what the phase does after INTEGRATING: does it merge phase→main (relevant to the PG-fix main-propagation item below), and does a NEXT phase start?

## WHAT 5x DID
1. **PG-in-agents fix (founder's ITEM 1) — DONE + VERIFIED.** Root: factory agents get EPERM creating an AF_INET listen socket; AF_UNIX works everywhere. Switched the test PG to a unix socket. Commit `76232e2` on `phase/inventory-procurement` (`scripts/pg.sh` + `backend/erp/settings/base.py`). Verified live: a real phase-integration BUILD agent ran 1244 DB tests over the socket. Details + the "don't re-investigate the unreproducible root" note: memory [[pg-in-agents-unix-socket-fix]]. **CAVEAT:** on the phase branch only; `main` (7207394) has DIVERGED → see open item #1.
2. **Resolved 2 escalations on `phase-integration` (critical), then approved its §9 gate (#24 → approved):**
   - #100 (`unresolved_contest`): upheld the builder's AUD-SM-2 contest (R9B vacuous in Spine E's no-FF chain; auditor itself said "not a defect"); `rework:BUILD` for 5 comply findings (as-built honesty + build-ref + 2 test strengthenings).
   - #101 (`unresolved_contest`, AUD-CM-001): builder contested the `fixture_doc` test-setup stand-in. I VERIFIED in `backend/apps/procurement/reception.py` that the real reception UNCONDITIONALLY auto-reserves (R3, lines 194-202) + binds `zn.cont.own_pj` (line 399) — so it structurally cannot emit the loose/unreserved/own_pj=None lots those legs need. Upheld the contest as an accepted, disclosed divergence (`settled`, mirrors foundation FixtureDoc precedent). AUD-CM-002/SM-5 (stale validated-ref) non-blocking — merge-gate Tier-1 re-validates the final code. **If a re-audit ever re-raises AUD-SM-2 or AUD-CM-001 → `finding_recurrence` (monitor exit 14); they are settled, do NOT reopen — see the resolution reasons in the events.**
3. **Corrected `docs/incident-cadere-arhitecti-0425-21-06-2026.md`** — the 04:25 deaths were the prompt-matching pkill, NOT a Claude/RC event (commit `8f8b85c` on SF-F5 main). New memories: [[never-prompt-matching-pkill]], [[pg-in-agents-unix-socket-fix]].

## OPEN ITEMS
1. **Propagate the PG fix to `main` for FUTURE phases** (task tracker #4, DEFERRED — last responsible moment). `main` diverged from the phase branch (not an ancestor), so a phase branched from `main` would lack the unix-socket fix. Confirm whether the phase→main merge at phase completion carries `76232e2`; if not, re-apply the same two edits to `main` when the next phase is set up. The two edits are small (see `76232e2`).
2. **Minor PG stale-cluster observation** (NOT blocking — Doctrine §8, only act if it recurs): the phase-integration cross-model auditor once hit "shared memory block still in use" restarting the per-worktree cluster (the BUILD agent's postmaster was transiently unrestartable). The BUILD agent itself ran fine (1244 tests). If this starts blocking agents, add stale-postmaster/shmem cleanup to `pg.sh cmd_start`.
3. **Founder decision still nominally open:** the 04:25 "durable fix" — now ANSWERED in substance (the durable fix is the pkill rule, already enforced; auto-restart is at most a secondary safety net). Confirm with the founder if he wants the secondary net.

## WORKING MODE / SUCCESSION
- Romanian to founder, plain, his terms, DD-MM-YYYY, copy-paste commands, NEVER AskUserQuestion, no bare IDs. `ruff check` (not format) before commit. Verify DB schema before queries (stages use `id`/`state`, NOT `name`/`status`; events use `unit_id`; escalations have no `title`).
- **Founder delegation:** auto-approve any val+audit-passed stage, ALL risk classes, without waiting ([[founder-applies-approvals-via-architect]]). Verify validator passed + auditors have no OPEN blocking findings + sanity-check the diff before approving.
- Monitor watch-set + `[arhitect]` ntfy + the resolve-escalation/decide CLI (`.venv/bin/sf-factory resolve-escalation <id> <token> --reason ...`, `... decide <req_id> <option>`): see `docs/runbooks/session-succession.md` + architect-operations canon. Resolution tokens: `rework:BUILD|SPEC|SPEC_DOC|VALIDATE|MERGE_GATE`, `settled` (no-action), `failed`, `cancelled`.
