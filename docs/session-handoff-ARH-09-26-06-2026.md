# Session handoff — ARH-09 → ARH-10, 26-06-2026

**For the Main-Architect successor.** POINTER doc (Doctrine §9). ARH-09 resolved the crud escalation
chain through to **L1 DONE**, fixed a 60-min agent-timeout wall, pushed both repos to GitHub, and stood
up the **L1 live test instance** for the founder. The one live gate now: the **L1 phase_signoff (decision #2)**,
which the founder is actively testing before answering. Durable memory: **[[applayout-phone-chrome-overlap]]**
(CLOSED — crud visual gate confirmed) + **[[factory-drain-first-fix-on-running-phase]]** +
**[[sf-f5-github-remote-manual-push]]** (NEW) + **[[founder-applies-approvals-via-architect]]** +
**[[evidence-over-guessing-and-budget-headroom]]** + **[[mechanical-guarantees-over-attention]]** +
**[[founder-model-effort-policy]]** + **[[erp-local-test-instance]]**.

> ## ⛔ ABSOLUTE RULES (carry forward)
> 1. **NEVER `pkill -f` / `pgrep -f`** a prompt-matchable pattern — kills ALL architect sessions. Stop a
>    task ONLY by EXACT PID or EXACT tmux name. (Test-instance sessions `erp-be/erp-fe/erp-approver` you MAY
>    `tmux kill-session -t <exact-name>` — they are not architect sessions.)
> 2. **NEVER kill/exit a PREDECESSOR architect session** (arh-03 … arh-09). Leave attached + idle; the FOUNDER retires them.

## NAMING — you are ARH-10
`ARH - 10` (phone RC label; tmux slug `arh-10`). Runbooks: `session-succession.md` + `session-launch-protocol.md`.

## FIRST duties (in order)
1. **Claim the marker.** Your id = newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/`
   containing a UNIQUE phrase from YOUR launch prompt (NOT mtime — the live one GROWS as a top-level USER
   turn; the scratchpad path also encodes your id). Write it into `~/.claude/sf-architect-session`,
   REPLACING `97f68df8-b2f7-421f-a0a7-f64f3205cc5b`.
2. **Verify RC** (`ARH - 10` on the founder's phone). Confirm via `/proc/<pane_pid>/cmdline` if `ps|grep`
   misses it (long prompts don't render in `ps -o args`). He is ENGAGED + testing L1 — do NOT go silent.
3. **START the monitor** (factory RUNNING): `bash ~/.claude/sf-architect-monitor.sh` via Bash tool
   `run_in_background:true`. Bump header `ARH-10`. Exits: 10=escalation set changed, 11=decision set changed,
   12=orchestrator dead, 13=5h-limit, 14=routing event, 0=6h heartbeat. Restart it EACH exit (it re-baselines).
4. **Do NOT kill arh-03 … arh-09.**

## 🔴 OPEN FOUNDER DECISION — L1 phase_signoff (decision #2)
**The whole L1-nomencl phase is DONE and `AWAITING_SIGNOFF`.** Decision **#2** (`gate_kind=phase_signoff`,
unit=phase `l1-nomencl`, pending since 2026-06-25T16:51:50Z) is the founder's per-phase gate (NOT the delegated
stage-integration gate — that one I auto-approve; the PHASE signoff is his). I presented it + sent him the phone
captures; **he chose to test it LIVE first** (instance up — see below). When he answers:
- **`aprob` / approve:** `.venv/bin/sf-factory decide 2 approved` → L1 closes, L2 (money-base) dispatches.
- **`schimbări` / changes:** `.venv/bin/sf-factory decide 2 changes` → phase reopens (carry his WHY into the rework).
Evidence already verified: phase integration report **PASS, 0 findings** (source-read); both stage visual gates
clean (eyeballed — currency + the 9 catalogs render clean on phone, hamburger no overlap, no overflow/console
errors). **Honest minor note I gave him:** on phone, wide catalog lists (e.g. cash_desk_type, 5 cols) need a small
horizontal scroll inside the table to reach "Editează" — standard antd, page does NOT overflow, framework-level
(consistent), NOT a blocker. **My recommendation to him: approve.** After approval, YOU author the L2 stages
(rebuild design: architect authors, dual-audited opus+codex before apply — [[erp-rebuild-redesign-22-06]]).

## 🖥️ L1 LIVE TEST INSTANCE — UP (founder testing now)
- **`http://100.69.221.108:5173`**, login `artur` / `test1234`. His Android device is already approved (auto-approver `erp-approver` running).
- **What I did:** switched `tmux erp-fe` to run vite from the **L1 phase worktree** `/home/artur/projects/erp-workspace/.worktrees/l1-nomencl/frontend` (`npm run dev -- --host 0.0.0.0`). **Backend `erp-be` UNCHANGED** — still the `l0-shell` worktree backend (`:8000`): L1 added NO backend production code (nomencl-rest-verify was verify-only), so the foundation nomenclature API + the DB + approved devices + the uncommitted dev-cookie fix all carry over. Proxy `:5173/api → :8000` verified working.
- Catalogs start EMPTY (no nomenclature seed) — the founder tests by ADDING entries. Verified: app serves (HTTP 200), proxy reaches backend (403 unauth = healthy).
- **Revert when he's DONE testing L1:** `tmux kill-session -t erp-fe` (or repoint to l0-shell). The 2 UNCOMMITTED dev-cookie files live in `.worktrees/l0-shell` (`backend/erp/settings/dev.py` + `apps/accounts/api.py`) — `git checkout` them only when he's done with the BACKEND too. See [[erp-local-test-instance]].

## 🏭 STATE (verify fresh)
- Factory **RUNNING** (orchestrator pid 946867; `sf-factory run` in tmux `factory`). Dashboard `http://100.69.221.108:8377`.
- `drain.manual=false`, `max_parallel_agents=1`, `governor.seven_day_threshold_pct=97`.
- **L0 DONE/merged.** **L1 `AWAITING_SIGNOFF`** — all 3 stages DONE: `crud-framework-skeleton` (DONE; persistent draft autosave per founder DECISION A landed + visual gate confirmed), `instantiate-catalogs` (DONE; 9 catalogs under the `nomenclatoare` menu), `nomencl-rest-verify` (DONE). **L2–L9 PENDING** (no stages planned — you author L2 after signoff).

## ⚙️ agent_timeout_s = 5400 (90 min) — runtime override I set 25-06
crud's heavy frontend build (live visual-gate server + full tsc/eslint/vitest = wall-clock-heavy, NOT token-heavy
at ~64M) hit the **60-min per-agent wall-clock timeout** (`agent_run_failed`: timeout) at 60m01s WHILE FINALIZING;
the worktree rolled back (work lost). Founder DECISION A: raised `agent_timeout_s` 3600→5400 (live runtime
override via `db.set_runtime_setting`, applies to next agent, NO restart). Fresh rebuild then completed in ~48 min.
**OPEN micro-decision (mine/yours):** keep 90 min permanently (likely — L2/L4 frontend stages are similar) vs lower
to 60 after — incident-driven (Doctrine §8). It is reversible (`db.set_runtime_setting(conn,'agent_timeout_s',N,...)`
or the dashboard config input). Event `runtime_setting_changed` seq 401 records it.

## 📤 GitHub — both repos pushed 25-06; MANUAL push (no auto-sync)
`crxh22/SF-F5` (PUBLIC) was 226 commits behind for 15 days until I pushed (`25daf51..9324625`); `crxh22/ERP-start`
(PUBLIC, ssh `github.com-erp`) was 1 commit behind (pushed `c225fbc`). The factory code lives at `src/sf_factory/`,
tracked, secret-safe (`.factory/`/`.claude/`/`.venv/` gitignored). **No auto-push hook** — push BOTH before an
external agent reads GitHub. Offered the founder a post-commit/periodic auto-push (existing `deploy/` timers) — AWAITING
his call. THIS handoff commit is local-only unless you push. See [[sf-f5-github-remote-manual-push]].

## 👻 Ghost decisions #14 / #15 — NO ACTION
The founder saw "Decizia #14 (Retur furnizor & retur de la client)" + "#15 (Stock visibility views)" as "awaiting"
on his phone. They are STALE claude.ai/code cards from the SUPERSEDED `inventory-procurement` run — he ALREADY
answered both 20-06 (via CLI, which never cleared the phone card), and the 23-06 rebuild (fresh DB) replaced that
whole structure. The LIVE DB has 0 pending decisions besides #2. Their substance → future **L6-stock-ops** (re-derived
fresh). Told him: ignore/dismiss; they clear when he retires the old sessions.

## 📋 CLI / PRECEDENT
- `.venv/bin/sf-factory resolve-escalation <id> <token> --reason "<why>"` (tokens: rework:SPEC|SPEC_DOC|BUILD|VALIDATE|MERGE_GATE|settled|approved|changes). Carry the WHY (architect-operations §2 — it reaches the re-entered agent).
- `.venv/bin/sf-factory decide <request_id> <option>` (decisions, e.g. phase_signoff: `decide 2 approved`).
- Runtime settings (no CLI): `db.set_runtime_setting(conn, key, value, updated_by=, at=)` + an `insert_event(... 'runtime_setting_changed' ...)`, in one tx, `conn.row_factory = sqlite3.Row`. Writable keys: `drain.manual`, `max_parallel_agents`, `agent_timeout_s`, `governor.*`, `budget.<rc>`.
- **Read the agent's OWN evidence before resolving** (worktree `_factory/stages/<id>/`: spec.md, audit-*.{md,json}, findings-response.json, build-notes.md, visual-gate/*.png + capture-report.json; transcripts `.factory/logs/proc-*.ndjson`; tracebacks `error-*.traceback.txt`). [[evidence-over-guessing-and-budget-headroom]]

## WORKING MODE / SUCCESSION
Romanian to founder, plain, DD-MM-YYYY, copy-paste, **NEVER `AskUserQuestion`**, no bare IDs, brutal honesty over
validation. **Architect commits to main**, `ruff`/project gate before commit, VERIFY (diff+tests) before merge.
**DRAIN FIRST** before landing any fix on a RUNNING phase branch. When YOU hand off, follow
`session-launch-protocol.md` verbatim (auto-launch `ARH - 11`; never kill predecessors).
