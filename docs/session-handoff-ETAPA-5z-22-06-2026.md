# Session handoff — ETAPA-5z → ARH - 01, 22-06-2026

**For the Main-Architect successor (first under the new `ARH - NN` naming).** POINTER doc
(Doctrine §9), but the founder asked for MAXIMAL detail this time — so it is long on purpose.
ETAPA-5z hit ~400k+ context mid-replanning; the founder directed succession.

> ## ⛔ ABSOLUTE RULE (carry forward — it killed 5u/5v/5w; memory [[never-prompt-matching-pkill]])
> NEVER `pkill -f` / `pgrep -f` (or any broad kill) with a pattern that can appear in a session's
> launch prompt (`sf-architect-monitor.sh`, `sf-factory`, `sf-cap`, `orchestrator`, `stock-views`,
> any factory/stage word, `erp-backend`/`erp-frontend`/`runserver`/`vite`). It matches the FULL
> cmdline and kills ALL active sessions at once. Stop a background task by EXACT PID
> (verify `/proc/<pid>/cmdline`) or let it die with the session.

## NAMING CHANGE (founder, 22-06) — you are the first `ARH - NN`
The `ETAPA-5{letter}` lineage ENDED at 5z (me). Successors are now **`ARH - NN`** (NN zero-padded,
from `01`). You are **`ARH - 01`**. Runbook updated (`docs/runbooks/session-succession.md`, commit
93354fa): launch uses `SFF5_TMUX_SESSION=arh-01 SFF5_RC_NAME="ARH - 01"`. Your phone RC label =
`ARH - 01`.

## FIRST duties (in order)
1. Write your session_id into `~/.claude/sf-architect-session` (replace `22f2e978-8b2c-476b-a07d-fbb24176ce10`).
   Your id = newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/` containing your launch
   prompt — verify with a UNIQUE phrase from it (NOT just mtime; my transcript also contains your prompt
   because I authored it). The live one is the one actively GROWING.
2. Update `~/.claude/sf-architect-monitor.sh` header (5z→ARH-01) + relaunch via Bash `run_in_background:true`
   (my monitor dies with my session). Logic is sound; only the header comment needs the bump. Its watch-set:
   open-escalations, pending-decisions, orchestrator liveness, 5h-limit (exit 13), routing events (exit 14:
   `escalation_opened_notice|escalation_bumped|escalation_stuck_resolved|finding_recurrence`).
3. Verify your RC shows on the founder's phone as `ARH - 01` (he tests from his Android `galaxy-s24-ultra`,
   100.101.93.98). Do NOT go silent until confirmed.
4. Do NOT pkill anything.

## ⭐ THE BIG PICTURE — what this session became
It started as routine factory-ops succession (resolve escalation #103) but the founder tested the ERP
and it turned into a **fundamental re-planning of the whole ERP build**. The factory is now DRAINED and
your main job is: **review/refine the rebuild plan draft, get the founder's reference apps, get final
approval, then EXECUTE the replan (re-seed the factory's phases into the new layered structure).**

## 🧭 ALL FOUNDER INPUTS / DECISIONS THIS SESSION (the maximal-detail part — do NOT re-litigate)
1. **Factory on DRAIN + a feedback round** — "until we advance, I want a feedback round; we must adjust the
   UI and re-examine the whole concept of how UI is generated." Drain = `drain.manual=true` (he set it via
   the dashboard). Drain holds NEW stage starts; in-flight stages still run to their next checkpoint.
2. **Naming → `ARH - NN`** (above).
3. **ERP UI testing feedback (his 11 points + clarifications):**
   - Master-data MANAGEMENT is missing (can't add/edit/delete contragenți/contracte/marfă). He reacted
     STRONGLY: an ERP must manage its own data; the "static data mass" assumption is "one of the strongest
     ABERRATIONS" of an AI; he feared the plan is full of holes / "good only for the trash."
   - Every entity-selector should allow "add new" inline (#3).
   - Colors (font/background) not done with taste (#4).
   - Menu organization must be modular / easy to reorganize later (#5).
   - Back/front must be SEPARATED into different stages — never a builder doing both in one (#6).
   - UI is "tasteless"; he wants research into AI-assisted UI/UX tools + methodologies (#7), and into the
     TYPICAL problems of AI-built frontends + how to counter them (added angle).
   - Everything UI must be parametrizable/easy to change — what was done? (#8).
   - Before building a UI module, the spec agent must answer a UX-discovery questionnaire (a–g) (#9), PLUS
     (his addition) "which UI control fits each input/output, minimizing clicks — e.g. a 2-option choice =
     checkbox, not a dropdown."
   - Compile all UX changes + guarantee they happen at execution level (#10).
   - Populate the DB with demo data so he can test (#11).
4. **DECISIONS (apply, don't re-litigate):**
   - Master-data CRUD is MANDATORY, first-class. (It was never a real question.)
   - **Back/front separation = HARD rule.** Every UI-bearing stage = a backend stage + a SEPARATE frontend
     stage. Smaller stages.
   - **5 UI-quality mechanisms ADOPTED** (canonical: `docs/design/ui-ux-concept.md` §4): (1) an agent
     UI-rules file, (2) mechanical fences (build FAILS on violation), (3) the UX spec-gate questionnaire
     (#9), (4) a visual feedback loop (agent screenshots + self-corrects), (5) a FOUNDER visual-review gate
     before UI sign-off. Skip v0/Lovable/Figma/paid visual tools (don't fit).
   - Django admin for **config/users/rights = the LAST module built** (not permanent admin; proper screens,
     just last).
5. **NEW PLANNING METHODOLOGY (founder-approved — "go"):** build in **dependency-ordered, founder-TESTABLE
   layers**, NOT by domain category. Master-data catalogs/nomenclatures FIRST in dependency order
   (no-deps → deps), EACH with its management UI. **Money NOMENCLATURES (bank accounts, cash desks,
   currencies) at the BASE** (he corrected me — only money OPERATIONS go late). Each layer is
   founder-VERIFIED before the next (his core point: "the core is solid" is MY hypothesis — UNVERIFIED by
   him — and right now he CAN'T verify it because there's no data/UI; building catalogs first is what makes
   the foundation testable). **Navigation/menu (Layer 0) is a LIVING modular surface**, updated as each
   layer adds screens. **Keep/re-verify solid backend; re-slot base plumbing early; any reused code is a
   STARTING POINT to be re-verified + corrected/REBUILT — never assumed good** (he insisted: replanning WILL
   need partial rebuilds + corrections to fit the new reality).
6. **⚠️ FOUNDER OWES: 1–2 reference apps whose UI he likes.** He said remind him / let the successor remind
   him. This is now ON THE CRITICAL PATH — Layer 1 (the first build) sets the visual tone, so the reference
   must come BEFORE building L1. Without it the AI averages to "blandness." **REMIND HIM.**

## 📦 THE PLAN (your starting point — REVIEW + REFINE + get approval + execute)
- Draft committed (`df9854c`): **`docs/design/erp-rebuild-plan-DRAFT.md`** — a subagent derived the REAL
  entity dependency graph (FK relationships across all 17 apps) and laid out **10 dependency-ordered
  layers**. The founder confirmed I should save the agent's plan and have you REVIEW it (his words).
- **Layers:** L0 Navigation shell · L1 Root money/classification nomenclatures (+ a reusable generic-CRUD
  framework) · L2 Own entities + money locations (OwnPJ, CashDesk, BankAccount, exchange rate) · L3
  Counterparties + Contracts (the gap-audit #1 alarm) · L4 Parts catalog + production noms · L5 Vehicle +
  engine re-verify + `documents` lifecycle API (edit/cancel/storno/history) · L6 Inventory/procurement
  operations UI (finish create-only screens + order-to-stock) · L7 Service-orders core (land `so_quotes`,
  build peeled quote editor UI, ZN) · L8 Treasury operations (re-slot treasury foundations + payments) ·
  L9 Config/users/rights UI (last).
- **~33 stages** (+ gates): NEW 20 / KEEP-re-verify 6 / REBUILD 6 / RE-SLOT 1; 17 backend / 16 frontend.
- **Open questions to resolve with the founder** (from the draft): (1) reference apps (critical path, above);
  (2) ratify parties-get-REST-screens vs Django-admin (ADR-0002) — I've taken his thrust as "screens, yes";
  (3) generic-CRUD-framework investment level; (4) Vehicle `act primire` intake location; (5) so_quotes
  #103/#104 disposition (it's L7, REBUILD there — NOT an immediate blocker; see state below).
- **EXECUTION after approval** = re-seed the factory's phases/stages into this layered structure. The current
  domain-category phases (service-orders, treasury-payments) get dissolved/re-derived; foundation +
  inventory-procurement backend KEPT (+ management UI added); the DAG re-drawn dependency-first. This is a
  big operational step — confirm the founder approves the refined plan BEFORE re-seeding.

## 🏭 CURRENT FACTORY STATE (verify fresh)
- Orchestrator pid `.factory/orchestrator.pid` = **506016**, ALIVE (`sf-factory run`). Dashboard
  `http://100.69.221.108:8377/`.
- **DRAIN ON** (`drain.manual=true`). Keep it until the replan is approved + execution begins.
- **cont-quote-core (service-orders) = AWAITING_HUMAN, PARKED.** History: I resolved escalation #103 with
  `rework:BUILD` (8 real backend defects, all verified — see the resolution reason in the event log). It
  reworked, then raised a 2nd contest **#104**, which I PARKED with `awaiting_human` (to stop the bump
  cascade before it paged the founder). That created a pending **decision #26** (`escalation_tradeoff`,
  options approved/rework:BUILD/rework:SPEC) — leave it; it resolves when L7 is reached (so_quotes is REBUILD
  there). The code asset = the `so_quotes` Django app (16 modules) on git branch
  `stage/service-orders.cont-quote-core` (commit `ee5e9f1`) — survives worktree cleanup. DO NOT advance/merge
  it during the pause.
- **treasury-app-foundations = AUDIT, still cycling** (in-flight stages keep running under drain). It is
  base money plumbing (skeleton/layering/currency-rate-resolver/payment-base/conformity-helper), backend-only
  → conforms to back/front separation. Founder wants it KEPT + re-slotted early (L8). When it reaches its
  next checkpoint (contest or signoff), HOLD it (don't advance) per the pause — like cont-quote-core.
- 0 open escalations, decision #26 pending. Monitor (mine) died with me; START YOURS.
- **Deferred from earlier (still open):** (a) IP-INTEG-001 urlconf dual-mount split — settled, do for the
  next `erp/urls.py`-touching stage. (b) **cont-quote-editor-ui** sibling stage (the §12 peel from #103) —
  now folded into L7 of the new plan (the "peeled quote editor UI"); the old standalone tracking is
  superseded by the rebuild plan.

## 🖥️ ERP TEST INSTANCE — RUNNING (founder is testing)
- tmux `erp-backend` (Django runserver 127.0.0.1:8000, settings `erp.settings.dev`), `erp-frontend` (Vite
  0.0.0.0:5173 → `http://100.69.221.108:5173`), `erp-deviceapprover` (auto-approves devices). Login
  `artur` / `parola-fondator-2026!`. Memory [[erp-local-test-instance]].
- **⚠️ 2 UNCOMMITTED dev edits in the erp-workspace MAIN checkout** (the non-secure-cookie-over-HTTP fix so
  the founder's phone can log in): `backend/erp/settings/dev.py` (+SESSION/CSRF_COOKIE_SECURE=False) and
  `backend/apps/accounts/api.py` (device cookie `secure=settings.SESSION_COOKIE_SECURE`). **REVERT** them
  (`git -C ~/projects/erp-workspace checkout backend/erp/settings/dev.py backend/apps/accounts/api.py`) +
  stop the `erp-*` tmux sessions WHEN THE FOUNDER IS DONE TESTING. They trip the `_OutOfBoundsDetector`
  (latched, one ntfy) but do NOT leak into worktrees.
- **Demo data seeded** for testing: `scripts/seed_demo.py` (committed 6493326) — 7 counterparties, 6
  contracts, 10 parts, 1 warehouse, all "Demo "-prefixed, idempotent. STOCK is empty (lots need a reception
  through the document engine — the founder can create one via UI: Achiziții → Comandă furnizor → Recepție).
  Re-run: `cd erp-workspace/backend && DJANGO_SETTINGS_MODULE=erp.settings.dev PYTHONPATH=. ../.venv/bin/python ../../SF-F5/scripts/seed_demo.py`.

## 📚 ARTIFACTS PRODUCED THIS SESSION (all committed on SF-F5 main)
- `docs/design/ui-ux-concept.md` — the canonical UI/UX concept + 5 mechanisms + the #9 questionnaire (incl.
  control-selection 9-g) + open decisions. (commits f0cb320, 4d18a58)
- `docs/research/ui-ux-ai-assisted-research-22-06-2026.md` — AI-assisted UI/UX tooling + methodologies.
- `docs/research/ai-frontend-pitfalls-22-06-2026.md` — typical AI-frontend failures + counters.
- `docs/research/erp-gap-audit-22-06-2026.md` — the gap audit (entity×management matrix; ~7 forgotten vs ~4
  deferred gaps; verdict: expensive core solid, management/ground-floor layer missing).
- `docs/design/erp-rebuild-plan-DRAFT.md` — the 10-layer plan (your starting point).
- `scripts/seed_demo.py` — demo data.
- Memory: `[[erp-ui-planning-reset-22-06]]` + MEMORY.md pointer.

## ✅ YOUR IMMEDIATE NEXT STEPS
1. Bootstrap (marker, monitor, RC).
2. **REMIND the founder for the 1–2 reference apps** (critical path before L1). When he answers, feed them
   into the UI rules-file + the visual tone.
3. **Review + refine the rebuild plan** (`erp-rebuild-plan-DRAFT.md`): sanity-check the dependency graph,
   layer boundaries, stage sizing (small!), keep/reslot/rebuild marks. Resolve the open questions with the
   founder. Present the refined plan for FINAL approval.
4. On approval: design + execute the factory re-seed into the 10-layer structure (the operational replan).
   Build in the 5 UI mechanisms + back/front separation + the UX spec-gate questionnaire as factory law.
5. Keep the factory DRAINED until execution begins. Hold the parked stages (#26/#104, treasury-foundations).
6. When the founder finishes testing: revert the 2 dev-cookie edits + stop the `erp-*` tmux.

## WORKING MODE / SUCCESSION
- Romanian to founder, plain, his terms, DD-MM-YYYY, copy-paste commands, NEVER `AskUserQuestion`, no bare
  IDs/acronyms. `ruff check` (not format) before commit. Verify DB schema before queries (escalations use
  `trigger`/`target`, NO `kind`; decision_requests use `gate_kind`; events use `payload_json` not `payload`).
- **Founder delegation:** he gives chat decisions, you apply them; auto-approve any val+audit-passed
  stage/phase_signoff, ALL risk classes, without waiting ([[founder-applies-approvals-via-architect]]) —
  BUT we are in a deliberate PAUSE now, so HOLD stages at checkpoints until the replan executes.
- He values brutal honesty over validation (Doctrine §21) and demands mechanical guarantees, not "I'm
  careful" ([[mechanical-guarantees-over-attention]]). He caught a real systemic gap this session; match that
  honesty.
- Resolution CLI: `.venv/bin/sf-factory resolve-escalation <id> <token> --reason ...`;
  `.venv/bin/sf-factory decide <req_id> <option>`. ntfy founder channel: topic `claude-artur-md-hello`
  (`[arhitect]` prefix for architect pages, D-0004). Succession + monitor watch-set:
  `docs/runbooks/session-succession.md` + the architect-operations canon.
