# Session handoff — ARH-08 → ARH-09, 25-06-2026

**For the Main-Architect successor.** POINTER doc (Doctrine §9). ARH-08: landed **PRIORITY #1**
(the AppLayout phone-chrome fix) at the foundation, re-cut the FE stage that branched off the unfixed
base, and resolved three crud escalations — the third is an **OPEN FOUNDER DECISION** (below). Durable
memory: **[[applayout-phone-chrome-overlap]]** (RESOLVED) + **[[factory-drain-first-fix-on-running-phase]]**
(new — the re-cut lesson) + **[[founder-applies-approvals-via-architect]]** +
**[[evidence-over-guessing-and-budget-headroom]]** + **[[mechanical-guarantees-over-attention]]**.

> ## ⛔ ABSOLUTE RULES (carry forward)
> 1. **NEVER `pkill -f` / `pgrep -f`** a prompt-matchable pattern — kills ALL architect sessions. Stop a
>    task ONLY by EXACT PID or EXACT tmux name.
> 2. **NEVER kill/exit a PREDECESSOR** (arh-03 … arh-08). Leave attached + idle; the FOUNDER retires them.

## NAMING — you are ARH-09
`ARH - 09` (phone RC label; tmux slug `arh-09`). Runbooks: `session-succession.md` + `session-launch-protocol.md`.

## FIRST duties (in order)
1. **Claim the marker.** Your id = newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/`
   containing a UNIQUE phrase from YOUR launch prompt (NOT mtime — the live one GROWS as a top-level USER
   turn; the scratchpad path also encodes your id). Write it into `~/.claude/sf-architect-session`,
   REPLACING `94b3c3a6-416d-4d0c-804e-80acbda5f4c1`.
2. **Verify RC** (`ARH - 09` on the founder's phone). He is ONLINE + engaged — do NOT go silent.
3. **START the monitor** (factory RUNNING): `bash ~/.claude/sf-architect-monitor.sh` via Bash tool
   `run_in_background:true`. Bump its header `ARH-09`. It exits 10/11/14 on escalation/decision/routing
   changes — restart it each time (it re-baselines). It exited often under me because I was resolving.
4. **Do NOT kill arh-03 … arh-08.**

## 🔴 OPEN FOUNDER DECISION — crud draft-autosave vs the UX law (resolve esc 9)
`l1-nomencl.crud-framework-skeleton` is **ESCALATED** on escalation **9** (`spec_audit_loop`). The blocking
issue (cross-model auditor, HIGH finding AUD-001): the spec explicitly **defers persistent draft autosave**,
which **conflicts with the injected UI law** "zero pierdere de date la închiderea tab-ului / auto-save de
draft mereu" (ui-ux-laws §7). The auditor's own resolution menu: **either implement durable autosave in this
stage, OR the founder grants a formal exception to the law.** I surfaced the A/B to the founder (his UI-law
call — do NOT decide it unilaterally). When he answers, resolve esc 9:
- **Founder picks A (implement now):** `.venv/bin/sf-factory resolve-escalation 9 rework:SPEC --reason
  "Founder DECISION A: implement persistent draft autosave/restoration per the UI law — localStorage keyed by
  (catalog,mode,row), restore on open/reload/tab-close, clear on submit/cancel, with acceptance tests. This is
  the foundation framework; every catalog inherits it. Keep all prior reworks; honor frozen N1/N2/L0-1/F6."`
- **Founder picks B (formal exception):** `… resolve-escalation 9 rework:SPEC --reason "Founder GRANTED a
  formal exception to the draft-autosave UI law for this skeleton stage (decision 25-06): in-session draft
  preservation stays + tested; persistent autosave-restore DEFERRED to a dedicated follow-up. Document the
  founder-approved exception explicitly in the spec, citing the decision. Keep it tight."`  Then if the
  cross-model auditor STILL re-loops on it, the exception is founder-authorized → resolve the next loop with
  **`rework:BUILD`** (non-blocking by founder fiat).
- **Caveat — the audit asymptote:** this is a COMPLEX framework spec; both auditors confirm it is REALIZABLE +
  CONTRACT-CONFORMANT with no contract change, but the dual audit keeps surfacing a fresh LOW/MEDIUM tail each
  round (cap `spec_audit_max_rework=2`). esc 7 (internal_error: spec agent omitted an explicit findings-response
  entry for SA-06 it had subsumed under CAS-003 → `rework:SPEC`), esc 8 (`spec_audit_loop` → targeted
  `rework:SPEC`), esc 9 (loop again, now the HIGH autosave/UI-law conflict). Once the autosave decision is
  settled, if it loops once more on a pure low/medium tail, **`rework:BUILD`** is the right call — the build +
  Tier-1 + build-audit are the next quality gates. Read the latest audit MD/JSON in the stage worktree
  (`_factory/stages/l1-nomencl.crud-framework-skeleton/audit-spec_auditor_{cross,same}_model.{md,json}`) before
  each resolution ([[evidence-over-guessing-and-budget-headroom]]).

## 🟢 PRIORITY #1 — AppLayout phone-chrome — DONE (verify crud's visual gate confirms it)
Fixed at the foundation: `main` `ab5b1c8` + `phase/l1-nomencl` `7680417` (reposition the antd zero-width Sider
trigger into the content-free header band + mobile-only header gutter via `Grid.useBreakpoint`). Verified by an
isolated playwright render (390×844 no overlap, 1440×900 no regression) + `tsc/eslint/vitest 246/246`. Founder
gave the visual gust sign-off (before/after). **The one thing still PENDING: positively confirm zero-echo at
crud's visual gate** — a `crud-gate-watch` background task fires when crud reaches VALIDATE; then READ
`.worktrees/l1-nomencl.crud-framework-skeleton/_factory/stages/.../visual-gate/*.phone.png` and confirm the
hamburger no longer overlaps content. Full record + the **re-cut recovery procedure** (drain → reset stage to
PENDING + delete branch/worktree → lift drain; safe because `execute()` re-reads state from DB) is in
[[factory-drain-first-fix-on-running-phase]]. Harmless footprint: an orphan `spec` artifact_ref (132, deleted
branch commit) — `_reresolve_artifact_commits` skips it; NO cleanup needed.

## 🏭 STATE (verify fresh)
- Factory **RUNNING** (orchestrator pid was 946867; `sf-factory run` in tmux `factory`). Dashboard
  `http://100.69.221.108:8377` (its usage-limits block times out a lot — known parked issue, not urgent).
- `drain.manual=false`, `max_parallel_agents=1`, `governor.seven_day_threshold_pct=97`. Set drain via
  `db.set_runtime_setting(conn,'drain.manual',True/False,...)` (no CLI) — used for the re-cut, now OFF.
- **L0 = DONE/merged/approved.** **L1 RUNNING:** `nomencl-rest-verify` DONE; `crud-framework-skeleton`
  ESCALATED (esc 9, see above); `instantiate-catalogs` PENDING (will branch off the FIXED phase → inherits the
  AppLayout fix; no echo). L2–L9 PENDING. Each phase pauses at its own `phase_signoff` for the founder.

## 🖥️ L0 FOUNDER TEST INSTANCE — UP (leave running; revert when he's done)
tmux `erp-be`/`erp-fe`/`erp-approver`; `http://100.69.221.108:5173`, `artur`/`test1234`. UNCOMMITTED dev cookie
tweaks in `.worktrees/l0-shell` (`backend/erp/settings/dev.py` + `backend/apps/accounts/api.py:78`) — `git
checkout` those 2 files when the founder says he's done testing L0 (never touched main). See [[erp-local-test-instance]].

## 🚦 CAPACITY — relaxed (weekly reset 25-06; ~few % used; resets 02-07). Operate normally, not wasteful.

## 📋 CLI / PRECEDENT
- `.venv/bin/sf-factory resolve-escalation <id> <token> --reason "<why>"` (stage tokens: rework:SPEC | SPEC_DOC
  | BUILD | VALIDATE | MERGE_GATE | settled | … ). `rework:SPEC`→SPEC, `rework:BUILD`→BUILD. Carry the WHY in
  `--reason` (architect-operations §2 — it reaches the re-entered agent). A SPEC_AUDIT-stage failure has no
  rework:SPEC_AUDIT token — it maps to `rework:SPEC`.
- Read the agent's OWN evidence before resolving (transcripts `.factory/logs/proc-*.ndjson`, tracebacks
  `.factory/logs/error-*.traceback.txt`, stage artifacts in the worktree).

## WORKING MODE / SUCCESSION
Romanian to founder, plain, DD-MM-YYYY, copy-paste, **NEVER `AskUserQuestion`**, no bare IDs, brutal honesty
over validation. **Architect commits to main**, `ruff`/the project gate before commit, VERIFY (diff+tests)
before merge. **DRAIN FIRST** before landing any fix on a RUNNING phase branch (I lost a 3s race not doing this
— [[factory-drain-first-fix-on-running-phase]]). When YOU hand off, follow `session-launch-protocol.md` verbatim
(auto-launch `ARH - 10`; never kill predecessors).
