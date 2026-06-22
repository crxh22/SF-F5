# Session handoff — ARH-02 → ARH-03, 22-06-2026

**For the Main-Architect successor.** POINTER doc (Doctrine §9). ARH-02 hit ~512k context mid-prep.
The durable directive set lives in memory **[[erp-rebuild-redesign-22-06]]** — READ IT FIRST; this doc
is the immediate-work map + the things memory doesn't carry.

> ## ⛔ ABSOLUTE RULE (carry forward — it killed 5u/5v/5w; memory [[never-prompt-matching-pkill]])
> NEVER `pkill -f` / `pgrep -f` (or any broad kill) with a pattern that can appear in a session's launch
> prompt. It matches the FULL cmdline and kills ALL active architect sessions at once. Stop a task by
> EXACT PID (verify `/proc/<pid>/cmdline`) or EXACT tmux session name (`tmux kill-session -t <name>`).

## NAMING — you are ARH-03
ARH-02 → **`ARH - 03`** (phone RC label; tmux slug `arh-03`). Runbook: `docs/runbooks/session-succession.md`.

## FIRST duties (in order)
1. Write your session_id into `~/.claude/sf-architect-session` (replace `34d4c0c2-0fe6-4b2f-97d8-a0c4027bbdb7`).
   Your id = newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/` containing your launch
   prompt — verify with a UNIQUE phrase (NOT mtime; my transcript also contains it). The live one GROWS.
2. **Do NOT start the session monitor.** The factory is STOPPED — it would fire exit-12 on a loop. Start it
   (header ARH-02→ARH-03) only when you RESTART the factory at re-seed. Monitor = `~/.claude/sf-architect-monitor.sh`.
3. Verify your RC shows on the founder's phone as `ARH - 03`. Do NOT go silent until confirmed.
4. Do NOT pkill anything.

## ⭐ BIG PICTURE — the founder reframed the work (22-06)
We are in **PRE-START PREP** for the ERP rebuild. The founder's directive: **the 7 phases seeded in the
factory have NO value as a structure → re-derive ALL *unfinished* work fresh, abstracting from the old
phases, the new way.** Sequence: **do the PREP (factory changes) → a FRESH focused successor authors the new
stage structure (dual-audited opus+codex, codex-verified) → founder approves → re-seed → build.** Full
directive set + rationale = memory [[erp-rebuild-redesign-22-06]] (read it).

## ✅ DONE THIS SESSION (ARH-02) — SF-F5 main, + erp-workspace main for the socket fix
- **Pipeline review** (`588bf8e`): `docs/design/pipeline-review-22-06-2026.md` + `pipeline-map-22-06-2026.dot`
  (png gitignored) — the factory's agents + real-vs-dormant map (all 12 defined agents have run; cp1_triage/
  decision_session conditional). The founder asked for this; delivered.
- **2 research docs** (`9574bd1`): `research-integration-vs-parallelism-22-06-2026.md` (small stages cut
  integration cost **58%**; ranked ideas: small-stage gate, loop-cap, contract-first, scope Tier-2) +
  `research-codex-be-fe-build-22-06-2026.md` (codex BE/FE feasibility + the cardinal-changes list).
- **Step 1 — stage `kind` dimension** (`8352aab`): nullable `backend`/`frontend` on stages, threaded
  end-to-end (migration 0005, PhasePlanStage, Stage, db, ingest). No behavior change. 915 tests green.
- **Step 3 — front-gated UI/UX-laws injection** (`fa99bf5`): laws moved to `work-protocols/ui-ux-laws.md`
  (single source; `ui-ux-concept.md` §2/3/4/7 → pointers, §0/1/5/6 kept). `canon.inject.frontend=[ui_ux_laws]`
  composed in `runner._canon_text` ONLY when `stage_kind=='frontend'` → reaches builder/validator/auditor
  **incl. codex auditors via AGENTS.md**; architect gets a POINTER (architect-operations.md §5). Survives
  future edits (pipeline reads canon fresh per spawn).
- **Founder §5 UX decisions closed** (`080641d`): screens-in-app; adopt §4 + front/back split; visual
  capture **2 widths (desktop+phone, NO tablet)**; founder visual-gate **only first few UI iterations then
  re-evaluate**; cadence yes; AA default yes; palette later (modern default now). §4 mods are in the
  INJECTED laws file.
- **Socket-path fix** (erp-workspace `bb95800`): test-PG unix socket → `/tmp/sfpg-<12hex>` (hash of worktree
  root; pg.sh + Django proven identical). **Mechanically verified** (reproduced the 152-byte overflow, then
  confirmed 36-byte socket + Django connect + migrate). **The treasury merge-gate root cause is FIXED.**
- **2 KEY FINDINGS:** (a) **AGENTS.md canon parity is ALREADY mechanical** — `_canon_text` is the single
  source, claude gets it via `--append-system-prompt`, codex via `materialize_workspace`→AGENTS.md (same
  bundle); no parity work needed. (b) **codex resume WORKS** — verified empirically (`codex exec resume <id>`
  recalled context across a fresh process; codex CLI 0.139.0).

## 🎯 FOUNDER DECISIONS to inherit (detail in memory [[erp-rebuild-redesign-22-06]])
- **(A)** reuse the 2 parked branches (so_quotes/cont-quote + treasury) as a STARTING POINT, re-verify HARD on
  new deps — NOT rebuild-from-zero.
- **Spec dual-audit at ALL stages** ("toate"); routine spec-audit on a cheap model.
- **The architect (a FRESH focused successor, AFTER all prep, with NOTHING else on its plate) authors the
  stage STRUCTURE**; phase_architect narrows to contracts; the structure is **dual-audited (opus+codex)**
  before applying. (Founder-directed context hygiene.)
- **Layered-context / autonomous phase_architect: DEFERRED** (needs careful design).
- **codex-back: COMMITTED (NOT a pilot)** — Claude limits insufficient, codex reserves free → **capacity**.
  front→opus, back→codex.
- **codex gets same canon via AGENTS.md** (DONE) · **UI/UX laws reach all front roles incl. codex auditors** (DONE).
- **Keep the integration safety net** (small-stage gate + contract-first + loop-cap); do NOT adopt the 2
  aggressive cuts (scope Tier-2 payload / scope suite). · **Codex cross-verifies the new plan before "ready".**

## ⏳ REMAINING PREP (in order; do before the structure-authoring)
1. **Step 2 — codex-back ROUTING. ✅ FOUNDER APPROVED 22-06** (routine-FRONT → opus OK'd; back→codex, front→opus).
   Work: add `builder_backend`(codex)/`builder_frontend`(opus)
   + 2-D routing (kind×risk); `_builder_role` keys on RISK today (`scheduler.py ~592`); `kind` is on stages now.
   Lift resume gate: add `"codex"` to `RESUME_VERIFIED_CLIS` (`scheduler.py ~119`) AFTER in-flow verify.
   Reconcile codex pricing (gpt-5.5 list $5/$30; config has $1.25/$10). See research-codex-be-fe-build doc.
2. **Step 4 — spec DUAL-AUDIT at ALL stages.** Needs conveyor design: an audit step after SPEC (auditor_same_model
   + auditor_cross_model review the SPEC) before BUILD; findings triage (rework spec / escalate / proceed).
3. **Step 5 — integration safety net:** make small-stage a MECHANICAL planning gate (needs a measurable predicate
   at plan time — plan-time has no token estimate; design it) + contract-first stage pattern. See the research doc.
4. **Step 6 — Limite Claude block:** wire a poller writing `/tmp/sf-dash-limits.json` from `~/.claude/sf-limit.sh`
   every ~5 min (shape `{checked_at,five_h_pct,weekly_pct,five_h_reset,weekly_reset}`). Dashboard already renders it.
   (Socket fix = DONE.)

## 📦 THEN — structure-authoring (fresh successor) → re-seed
A FRESH focused successor authors the new structure from `docs/design/erp-rebuild-plan-DRAFT.md` (raw material,
re-examined, NOT assumed approved; abstract from old phases; SHORT stage-ids; mark each stage
`kind=backend/frontend`; small stages) → dual-audit (opus+codex) + codex-verify before "ready" + founder approval
→ re-seed (`erp-rebuild-reseed-playbook.md`, Strategy A archive+fresh; PRESERVE the 2 parked branches +
foundation+inventory on erp-workspace main + `runtime_settings`) → build.

## 🏭 FACTORY STATE (verify fresh)
STOPPED (orchestrator down, `sf-factory-watchdog.timer` disarmed, `drain.manual=true`, dashboard down). 0 open
escalations. Pending **decision #26** (cont-quote trade-off — leave). Parked: `cont-quote-core` (AWAITING_HUMAN),
`treasury-app-foundations` (BUILD). Monitor OFF (start at re-seed restart). passwordless sudo works.

## WORKING MODE / SUCCESSION
Romanian to founder, plain, his terms, DD-MM-YYYY, copy-paste, **NEVER `AskUserQuestion`**, no bare IDs. Brutal
honesty over validation (Doctrine §21). Mechanical guarantees, not "I'm careful" ([[mechanical-guarantees-over-attention]]).
**Architect commits to main** (the norm). **VERIFY before merge** — the pattern that worked all session:
dispatch a focused implementation subagent on a branch → review the diff + re-run the affected tests → ff-merge →
delete branch. Use focused subagents for implementation (keeps your context lean — the founder's own principle).
`ruff check` before commit. Founder delegation: he gives chat decisions, you apply ([[founder-applies-approvals-via-architect]]).
ntfy founder topic `claude-artur-md-hello` (`[arhitect]` prefix). Resolution CLI: `.venv/bin/sf-factory resolve-escalation`.

## THE LIVE CONVERSATION
The founder is actively engaged (mode-2 design). The one open thread you inherit: **his nod on Step 2**
(codex-back routing / the routine-front-opus consequence). Pick up the conversation in Romanian.
