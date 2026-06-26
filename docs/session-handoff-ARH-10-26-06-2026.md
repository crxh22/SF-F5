# Session handoff — ARH-10 → ARH-11, 26-06-2026

**For the Main-Architect successor.** POINTER doc (Doctrine §9). ARH-10 diagnosed+fixed the L1 "can't save"
bug (3 CSRF layers), signed L1 on the founder's explicit mandate (→ DONE, merged `main` cf03bce), recolored
the UI to a warm "Claude" palette (LIVE PREVIEW, uncommitted, awaiting the founder's eye), and fixed the
test instance after the L1 merge cleaned its worktree. L2 auto-planned + started; **one OPEN escalation
(#12, own-pj-rest internal_error) awaits you.** Durable memory: **[[erp-returning-session-csrf-write-403]]**
(NEW) + **[[erp-claude-style-palette]]** (NEW) + **[[erp-deferred-safe-delete-nomenclature]]** (NEW) +
**[[factory-drain-first-fix-on-running-phase]]** + **[[sf-f5-github-remote-manual-push]]** +
**[[founder-applies-approvals-via-architect]]** + **[[evidence-over-guessing-and-budget-headroom]]** +
**[[mechanical-guarantees-over-attention]]** + **[[erp-local-test-instance]]**.

> ## ⛔ ABSOLUTE RULES (carry forward)
> 1. **NEVER `pkill -f` / `pgrep -f`** a prompt-matchable pattern — kills ALL architect sessions. Stop a
>    task ONLY by EXACT PID or EXACT tmux name. (Test sessions `erp-be/erp-fe/erp-approver` you MAY
>    `tmux kill-session -t <exact-name>`.)
> 2. **NEVER kill/exit a PREDECESSOR architect session** (arh-03 … arh-10). Leave attached + idle; the FOUNDER retires them.

## NAMING — you are ARH-11
`ARH - 11` (phone RC label; tmux slug `arh-11`). Runbooks: `session-succession.md` + `session-launch-protocol.md`.

## FIRST duties (in order)
1. **Claim the marker.** Your id = newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/` that
   contains a UNIQUE phrase from YOUR launch prompt (NOT mtime — the live one GROWS as a top-level USER turn;
   the scratchpad path also encodes your id). Write it into `~/.claude/sf-architect-session`, REPLACING
   `4e5997be-91ec-425f-92cd-96a5c272604c`.
2. **Verify RC** (`ARH - 11` on the founder's phone). Confirm via `/proc/<pane_pid>/cmdline` if `ps|grep` misses it.
3. **START the monitor** (factory RUNNING): `bash ~/.claude/sf-architect-monitor.sh` via Bash `run_in_background:true`.
   Bump header `ARH-11`. Exits: 10=escalation set changed, 11=decision set, 12=orch dead, 13=5h-limit, 14=routing.
   Restart it EACH exit (re-baselines). It WILL exit 10 immediately on the open escalation #12 — restart + handle.
4. **Do NOT kill arh-03 … arh-10.**

## 🔴 OPEN ESCALATION #12 — l2-money-base.own-pj-rest (internal_error) — YOUR FIRST JOB
`stage/l2-money-base.own-pj-rest — trigger=internal_error — target=phase_architect — status=open`, created
2026-06-26T13:24:55Z. The stage burned **13.8M tokens** then died with an internal_error (agent_run_failed
class — could be context overflow, a crash, or a tooling error). **READ THE AGENT'S OWN EVIDENCE BEFORE
RESOLVING** ([[evidence-over-guessing-and-budget-headroom]]): newest transcripts
`.factory/logs/proc-e884ce07de1e.ndjson` + `proc-44b93265a2f9.ndjson` (grep for the terminal error / is_error /
subtype); worktree `/home/artur/projects/erp-workspace/.worktrees/l2-money-base.own-pj-rest/_factory/stages/`
(error-*.traceback.txt, build-notes, spec). Decide: transient (re-enter `rework:BUILD`/`rework:VALIDATE` via
`.venv/bin/sf-factory resolve-escalation 12 <token> --reason "<why>"`, carry the WHY — architect-operations §2)
vs a real defect (fix the generating artifact). 13.8M tokens is a LOT — if it overflowed context, the stage may
be too big and need a respec/split, not a blind re-run. I did NOT resolve it (over my context budget; fresh
context is the right place — Doctrine §4).

## 🎨 CLAUDE-STYLE PALETTE — LIVE PREVIEW, UNCOMMITTED on `main` working tree
Founder asked to recolor the ERP to "stil Claude" (warm neutrals + terracotta). Applied as a LIVE preview the
founder is eyeballing on the test instance. **THREE uncommitted files on `/home/artur/projects/erp-workspace`
(main working tree):** `frontend/src/ui/theme/tokens.ts` (colors: bg `#FAF9F5`, surface `#F0EEE6`, text `#2B2A26`,
primary terracotta `#C96442`, warm success/warning/error/border), `frontend/src/ui/theme/ConfigProvider.tsx`
(added `colorLink`=primary + `components.Layout` headerBg/siderBg=surface/bodyBg=background/headerColor=text —
antd's Layout defaults to dark-navy #001529 + links default blue, NOT controlled by the global seed; that's why
the FIRST attempt only got ~30% — content warmed but the dark sidebar + blue links remained), and
`frontend/src/shell/AppShell.tsx` (NavLink `color`: charcoal inactive / terracotta active on a lighter tile).
`tsc` clean; vite HMR live; NO playwright here to screenshot (founder's eye is the final check).
- **If founder says keep/merge:** run the frontend gate `cd .../frontend && npm run check` (tsc+eslint+vitest),
  commit the 3 files to `main`, and UPDATE `docs/design/ui-ux-concept.md` §7 palette to match (governance —
  the concept is the canonical visual source). Architect hand-edit, not a factory stage ([[erp-claude-style-palette]]).
- **If tweaks:** adjust `tokens.ts` only (single styling source — "a visual change is a change HERE, nowhere else").
  Likely nits: too much terracotta → make active-nav charcoal+bold instead; coral too light on buttons → deepen primary.

## 🖥️ TEST INSTANCE — repointed, LIVE (`http://100.69.221.108:5173`, artur/test1234)
The L1 merge de-registered the `l1-nomencl` worktree (orphan dir remains) and broke vite (404). I **repointed
`tmux erp-fe` to serve from `main`'s frontend** (`/home/artur/projects/erp-workspace/frontend`, stable, has
merged L1 + node_modules) — it carries the uncommitted palette preview. `erp-be` UNCHANGED (l0-shell orphan
backend, :8000, with the uncommitted dev hacks: SESSION/CSRF_COOKIE_SECURE=False + `CSRF_TRUSTED_ORIGINS=
["http://100.69.221.108:5173"]` in dev.py — DEV-ONLY, revert when founder done testing the backend). Proxy
:5173/api→:8000 healthy. Bank catalog has the founder's real rows MAIB+EXIM (my repro test-rows cleaned).

## 🏭 STATE (verify fresh)
- Factory **RUNNING** (orchestrator in tmux `factory`). Dashboard `http://100.69.221.108:8377`.
  `drain.manual=false`, `max_parallel_agents=1`, `governor.seven_day_threshold_pct=97` (HARD wall, watch budget).
- **L0 DONE/merged. L1 DONE/merged** (`cf03bce integrate phase/l1-nomencl into main`; signed on founder's chat
  mandate via decisions #2+#3 both `approved`). **L2-money-base RUNNING** — phase_architect AUTO-planned 3 stages
  (this is the correct layered-context flow — DON'T hand-author): `money-fe` PENDING(structural),
  `money-loc-rest` PENDING(routine), `own-pj-rest` ESCALATED (see #12). **L3–L9 PENDING.**

## ✅ L1 CSRF FIXES (shipped today — for context)
Founder live-test hit "Save does nothing" = 3 stacked CSRF layers, each reproduced with evidence:
(1) returning session never re-primed csrftoken → `@ensure_csrf_cookie` on MeView (**main `45100ed`** + test);
(2) failed Save was SILENT → never-silent banner fallback (merged via L1, test SA-21);
(3) Origin check rejected cross-port dev → `CSRF_TRUSTED_ORIGINS` (dev.py scaffolding, uncommitted).
[[erp-returning-session-csrf-write-403]]. Note the StaleGate re-gate: committing 45100ed to main WHILE L1 was
AWAITING_SIGNOFF moved the base → approving #2 re-gated (clean) → #3 → merge. Not a loop (verified in
`_step_awaiting_signoff`). Land foundation fixes on main BEFORE a phase hits signoff to avoid the re-gate.

## 📤 GitHub — push pending
`crxh22/SF-F5` + `crxh22/ERP-start` are MANUAL push (no auto-sync). My new commits (erp `45100ed`, `cf03bce`,
+ this handoff on SF-F5) are LOCAL — push `origin main` (both repos) before any external agent reads GitHub
([[sf-f5-github-remote-manual-push]]).

## 📋 CLI / PRECEDENT
- `.venv/bin/sf-factory resolve-escalation <id> <token> --reason "<why>"` (tokens: rework:SPEC|SPEC_DOC|BUILD|
  VALIDATE|MERGE_GATE|settled|approved|changes). Carry the WHY (reaches the re-entered agent).
- `.venv/bin/sf-factory decide <id> <option>`; `.venv/bin/sf-factory status`.
- Read the agent's OWN evidence before resolving (worktree `_factory/stages/<id>/` + `.factory/logs/proc-*.ndjson`).
- Ghost decisions #14/#15 = stale superseded cards, ZERO action. agent_timeout_s=5400 (90min) kept.

## WORKING MODE / SUCCESSION
Romanian to founder, plain, DD-MM-YYYY, copy-paste, **NEVER `AskUserQuestion`**, no bare IDs, brutal honesty.
Architect commits to main, `ruff`/project gate before commit, VERIFY (diff+tests) before merge. **DRAIN FIRST**
before a fix on a RUNNING phase branch. Reproduce bugs as the FULL browser (cookie+token+Origin) — a partial
repro made me over-claim "fixed" twice today. When YOU hand off, follow `session-launch-protocol.md` verbatim
(auto-launch `ARH - 12`; never kill predecessors).
