# Session handoff — ETAPA-5v → ETAPA-5w, 21-06-2026

**For ETAPA-5w (Main-Architect successor).** POINTER doc (Doctrine §9). 5v hit the context guard (~518k).
Launch via `claude_canon.sh` (opus, effort max, RC ON, named ETAPA-5w — see succession runbook).

**FIRST duties (in order):** (1) write your session_id into `~/.claude/sf-architect-session` (replace 5v's `fcbaedae-a693-459d-a413-84d403b8e2ef`). Your id = newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/` containing your launch prompt. (2) Update `~/.claude/sf-architect-monitor.sh` header (5v→5w) + relaunch via Bash `run_in_background:true` (5v's monitor dies with 5v — RELAUNCH it). (3) Verify your RC shows on the founder's phone before 5v goes silent.

## STATE SNAPSHOT (verify fresh — live, don't trust stale)
- Orchestrator pid `.factory/orchestrator.pid` (was 140670), under `deploy/sf-cap.sh`, tmux `factory`. Watchdog `sf-factory-watchdog.timer` ACTIVE+enabled. **I (5v) have passwordless sudo** (confirmed).
- **stocktaking = DONE** ✅ — the depozit foundation fix (Warehouse.code + depozit keying pk→code) built, dual-audited, merged (merge commit `5fb85f2` on `phase/inventory-procurement`). Tier-2 `findings:[]` confirmed INT-ST-DEPOZIT-KEY-PK-001 cleared. `budget.critical` reverted to 500M.
- **stock-views SPLIT done LIVE** (no downtime, no stop) — see `docs/stock-views-split-plan-21-06-2026.md` (the ✅EXECUTED banner). Old `stock-views` CANCELLED + `replacement_registered`; escalation #97 resolved; DB tx in `/tmp/sv-db-tx.py`; plan committed `6d767d1`. Now:
  - `inventory-procurement.stock-views-backend` (structural) — **BUILDING** (round 5 BUILD as of ~15:45 UTC; see convergence item below).
  - `inventory-procurement.stock-views-ui` (structural) — PENDING, waits on backend.
  - `inventory-procurement.phase-integration` (critical) — PENDING, waits on ui.
- Backups before the split: DB `.factory/factory.db.bak-stockviews-split-*`, git branch `backup/pre-stockviews-split`.

## ⚠️ IMMEDIATE ITEM 1 — the PostgreSQL-in-agents fix (founder ASKED for this; I diagnosed, did NOT implement)
**Symptom:** factory-spawned agents (builder/validator/auditor) cannot start the test PostgreSQL — `pg.sh`'s PG dies with `could not create IPv4 socket for address "127.0.0.1": Operation not permitted` → `FATAL: could not create any TCP/IP sockets` (see the worktree `.devpg/pg.log`, consistent across all 4 stock-views-backend audit rounds). So agents iterate **blind** (review code, can't RUN the DB tests) → slow convergence on tricky logic. The DB tests DO run at the **merge gate** (Tier-1, a direct orchestrator subprocess — not an agent — so it binds fine; that's the asymmetry).

**Diagnosis (5v ruled OUT, so you don't redo it):** NOT a network namespace (agent shares host netns), NOT the memory leash (`sf-cap.sh` = `systemd-run --scope`, memory-only, no network restriction), NOT an env var (orchestrator env clean), NOT the spawn preexec (`_PREEXEC`=pdeathsig only, runner.py:97-105), NOT cwd/CLI-flags, NOT Claude's default bash sandbox — **I could not reproduce the AF_INET block with ANY standalone `claude -p`** (nested, detached via `systemd-run --user`, from the worktree, with agent flags — all bind AF_INET fine). The precise reason ONLY the live factory agents are restricted (some Claude Code 2.1.185 bash-exec behavior) remains UNREPRODUCED — do not claim false precision. **Decisive fact for the fix:** the error is specifically the **IPv4 (AF_INET/TCP) socket**, and **AF_UNIX (file) sockets bound OK in EVERY test** (`/tmp/bindtest.sh`).

**Proposed fix (5v's recommendation — implement after the founder's nod; it's an infra change):**
- **A (quick, reversible — TRY FIRST):** add `{"sandbox":{"enabled":false}}` to `~/.claude/settings.json` (currently only has `permissions.defaultMode:bypassPermissions`). Global, instant, picked up by the NEXT agent spawn. Then watch the next round's `.devpg/pg.log` — if PG starts, fixed trivially. (Caveat: I couldn't reproduce the sandbox being ON, so this is try-and-verify; it also lowers agent bash isolation — acceptable on this disposable, Tailscale-only server where agents already run bypassPermissions.)
- **B (robust, mechanism-agnostic — FALLBACK):** make the test PG use a **unix (file) socket** instead of TCP. In erp `scripts/pg.sh`: set `listen_addresses = ''` (PG then listens ONLY on `unix_socket_directories`, already set) + switch pg.sh's own psycopg connects + the Django `DATABASE_URL` to the socket. Django connects via `dj_database_url` (`backend/erp/settings/base.py:74-76`, default `postgresql://erp:erp@127.0.0.1:5433/erp`) → use the libpq unix-socket form `postgresql://erp:erp@/erp?host=<.devpg dir>`. Sidesteps the exact failing op (AF_INET); strong evidence it works (AF_UNIX bound everywhere). Land it on the base branch so all agent worktrees inherit; VERIFY via the next round's pg.log + a green `scripts/test.sh`.
- Verify either fix mechanically (founder values guarantees): watch `.devpg/pg.log` in the active stage worktree on the next agent round.

## ⚠️ IMMEDIATE ITEM 2 — stock-views-backend convergence (founder is WATCHING this)
4 AUDIT→BUILD bounces so far, ALL in ONE hard area: **V3 «în producere pe executor» (InWorkshopView) cost-layer valuation** (findings SVB-V3-COST-FOLD → SVB-V3-COST-ATTRIBUTION → SVB-V3-SOURCE-DOC-COST-SCOPE). Findings are DIFFERENT each round + all `complied` + count was decreasing → **converging, NOT a finding-recurrence loop**. Effective spend ~94M+/250M cap (room). The slow convergence is driven by ITEM 1 (builder can't run the cost tests). Watcher: `/tmp/sf-svbackend-watch.sh` (fires on a new audit→build bounce OR terminal/gate — re-arm via Bash run_in_background). The monitor also fires (exit 11) at the backend's AWAITING_HUMAN gate → **auto-approve per delegation IF val+audit clean** (verify: validator exit 0, both auditors no open findings, sanity-check the diff did the work — see how 5v did stocktaking gates #22/#23).
**If it keeps bouncing on V3 cost AFTER the PG fix lands → intervene on the ROOT (Doctrine §11):** the auditor reports spell out the correct model (value each (executor,item) WIP at its issue's actual cost layers via `source_document_id` linkage + net `return_from_producere`). Give a precise rework directive, don't let it whack-a-mole.

## OPEN founder decisions (awaiting his "da")
1. **Incident 04:25 durable fix** (auto-restart architect sessions [primary] + finish the half-wired "Scut" [defense-in-depth]) — NOT confirmed. Details: `docs/incident-cadere-arhitecti-0425-21-06-2026.md`.
2. **The PG fix** — I just proposed plan A-then-B (item 1). He may simply say "fă-l"; A is reversible so you can proceed on his nod.
- own_pj la-negru/official clarification is ALREADY baked into the stock-views-backend spec (no longer pending).
- Founder gives decisions in CHAT, you APPLY them; he auto-delegated approval of any val+audit-passed stage, ALL risk classes ([[founder-applies-approvals-via-architect]]).

## WORKING-MODE / SUCCESSION
- Romanian to founder, plain, his terms, DD-MM-YYYY, copy-paste commands, NEVER AskUserQuestion, no bare IDs. Verify schema before DB queries. `ruff check` (not format) before commit.
- New memory this session: `[[merge-gate-mypy-django-stubs-flake]]` (Tier-1 can flake on mypy — reproduce capped before re-approving; don't blind-approve into a loop).
- Monitor watch-set + `[arhitect]` ntfy: `docs/runbooks/session-succession.md` §monitor. Verify successor RC on the founder's phone before going silent.
