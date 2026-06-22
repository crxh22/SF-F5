# Session handoff — ARH-03 → ARH-04, 22-06-2026

**For the Main-Architect successor.** POINTER doc (Doctrine §9). ARH-03 finished ALL pre-start PREP;
your one job is the **structure-authoring** the founder reserved for a FRESH focused session. The durable
directive set is in memory **[[erp-rebuild-redesign-22-06]]** + **[[founder-model-effort-policy]]** — READ
BOTH FIRST.

> ## ⛔ ABSOLUTE RULES (carry forward)
> 1. **NEVER `pkill -f` / `pgrep -f`** (or any broad kill) with a pattern that can appear in a session's
>    launch prompt — it kills ALL architect sessions at once. Stop a task ONLY by EXACT PID
>    (`/proc/<pid>/cmdline` first) or EXACT tmux name. (memory [[never-prompt-matching-pkill]])
> 2. **NEVER kill/exit the PREDECESSOR session (ARH-03 / `arh-03`).** Founder 22-06: an architect kill
>    DROPS the predecessor's claude.ai/code dashboard history. Sole-writer is enforced by the MARKER, not by
>    killing. Leave ARH-03 attached + idle (readable); the FOUNDER retires it himself. (runbook
>    `session-launch-protocol.md §B`, commit 43419e9)

## NAMING — you are ARH-04
`ARH - 04` (phone RC label; tmux slug `arh-04`). Runbooks: `docs/runbooks/session-succession.md` +
`session-launch-protocol.md`.

## FIRST duties (in order)
1. **Claim the marker.** Your id = newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/`
   containing a UNIQUE phrase from YOUR launch prompt (NOT mtime — ARH-03's transcript also contains it;
   the LIVE one GROWS). Write it into `~/.claude/sf-architect-session`, replacing
   `802c89b5-4a9c-4f5f-9322-6db3491816c8`.
2. **Do NOT start the session monitor** — the factory is STOPPED (monitor loops on exit-12). Start it only
   when you restart the factory at re-seed.
3. **Verify RC.** Confirm `ARH - 04` on the founder's phone — BUT he is OFFLINE ~8h (see below), so do the
   MECHANICAL check (`tmux has-session -t arh-04` + a `--remote-control ARH - 04` process exists). ARH-03
   stays idle as the founder's fallback channel regardless.
4. **Do NOT kill ARH-03.** It stays idle/readable.

## 🏭 STATE (verify fresh)
Factory **STOPPED** (orchestrator down, watchdog disarmed, `drain.manual=true`, dashboard down). 0 open
escalations. Pending **decision #26** (cont-quote trade-off — leave). Parked branches: `cont-quote-core`
(AWAITING_HUMAN), `treasury-app-foundations` (BUILD) on erp-workspace. Monitor OFF.

**Capacity (founder's rule — memory [[founder-model-effort-policy]]):** USE the limit, never "wait for the
reset", never leave it idle — valorize it. Watch it ONLY to prioritize when parallel long runs compete. At
ARH-03 handoff: weekly **85%** (resets 25-06 03:00 UTC — 15% left, "destul de mult"), 5h **11%** (resets
~22:40 UTC). The dashboard's Limite-Claude block + `~/.claude/sf-limit.sh` show it live.

## ✅ ALL PREP DONE (ARH-03, on SF-F5 main)
- **Step 2** (`1db7bf3`): 2-D builder routing kind×risk — backend→codex, frontend→opus.
- **Step 6** (`63a5991`): Claude-limits dashboard poller (`deploy/sf-dash-limits-poller.*`, system timer
  installed+running).
- **Step 5** (`b8258ca`): integration safety net — small-stage size gate (WARN-first) + contract-first stage
  pattern; new nullable plan fields `acceptance_criteria`/`touched`/`role`; `db.deps_done` already enforces
  contract-first (no scheduler change); merge-gate loop-cap untouched.
- **Effort policy** (`db207e7`): NO reasoning downgrade on quality roles — codex xhigh (its ceiling; 'max'
  is Claude-only), opus xhigh, sonnet max. See [[founder-model-effort-policy]].
- **Step 4** (`7d7916c`): spec dual-audit — new `SPEC_AUDIT` state SPEC→SPEC_AUDIT→BUILD at ALL stages,
  BLOCKING, full-strength `spec_auditor_*` (opus xhigh + codex xhigh); loop-cap `spec_audit_max_rework=2`;
  DB migration 0006 (FK-safe). Founder chose block + full models.
- **Runbook fix** (`43419e9`): never-kill-predecessor (above).
- 965 tests green on main (ARH-03 re-verified each merge independently — diff + full serial suite).

## 🎯 YOUR MANDATE — author the new stage STRUCTURE (the founder reserved this for you)
Re-derive ALL *unfinished* ERP work FRESH, abstracting from the old 7 seeded phases (they have **NO**
structural value — founder directive). The deliverable is a stage structure ready for the founder's single
approval → re-seed → build.

**Inputs (raw material — re-examine, do NOT copy/assume-approved):**
- `docs/design/erp-rebuild-plan-DRAFT.md` (10 layers) — the basis, RE-DERIVED.
- ERP product docs: `/home/artur/projects/ERP-start` + `docs/projects/erp/PROJECT.md`.
- DONE + merged base (re-verify): foundation engine + inventory-procurement backend on `erp-workspace` main.
- Decision (A): reuse the 2 parked branches (treasury + cont-quote-core) as a STARTING POINT, **re-verify
  HARD** on new deps — NOT rebuild-from-zero.

**Output requirements (mechanically enforced now — honor them so the size gate stays quiet):**
- Phases→stages, **SHORT stage-ids** (the treasury loop was a long-stage-name AF_UNIX overflow — keep ids
  short; memory [[treasury-merge-gate-socket-loop]]).
- Each stage `kind: backend|frontend`. Back→codex, front→opus build routing is automatic from `kind`.
- **Small stages**: ≤7 `acceptance_criteria`, ≤6 `touched`, ≤6 dependency-degree (the WARN-first size gate
  flags violations). Emit `acceptance_criteria` (list), `touched` (files/components), `role`.
- **Contract-first**: a `role: contract` stage freezing shared seams BEFORE dependents fan out; every leaf
  must be a DAG descendant of a contract stage (else `read_phase_plan` rejects).
- Frontend stages get the UI/UX laws injected automatically (`work-protocols/ui-ux-laws.md`); design them to
  the founder's UX philosophy (`docs/design/ui-ux-concept.md`).
- Effort policy is config-level (already set) — just don't design anything implying cheap models.

**Method (founder-directed — do NOT author in one bloated session):** focused per-layer subagents draft each
layer's stages → YOU integrate → **DUAL-AUDIT (opus + codex)** the structure → **codex cross-verifies**
before you call it ready. Verify-before-merge throughout.

**Stop line:** prepare the re-seed (playbook `docs/runbooks/erp-rebuild-reseed-playbook.md`, Strategy A
archive+fresh; PRESERVE the 2 parked branches + foundation+inventory on erp-workspace main +
`runtime_settings`) but **STOP before the actual build** — the build needs the founder's approval and is the
expensive long run.

## THE LIVE CONVERSATION
The founder went **OFFLINE ~8 hours** (evening 22-06) and said: make the window maximally productive without
him; he **approved proceeding autonomously** ("aprob tot"). He returns to **APPROVE the structure**
(single DA → re-seed → build). Leave it complete, dual-audited, codex-verified, ready. Open thread he may
react to on return: 2 minor model rulings he tacitly approved (utility roles stay haiku; sonnet "mega-light"
routing deferred to YOU — decide if any stage is trivial enough to route to the sonnet/max builder).

## WORKING MODE / SUCCESSION
Romanian to founder, plain, his terms, DD-MM-YYYY, copy-paste, **NEVER `AskUserQuestion`**, no bare IDs.
Brutal honesty over validation (Doctrine §21). Mechanical guarantees ([[mechanical-guarantees-over-attention]]).
**Architect commits to main.** **VERIFY before merge** — dispatch focused subagents on a branch → review diff
+ re-run affected tests → ff-merge → delete branch. `ruff check` before commit. Founder delegation: he gives
chat decisions, you apply ([[founder-applies-approvals-via-architect]]). ntfy founder topic
`claude-artur-md-hello` (`[arhitect]` prefix). Resolution CLI: `.venv/bin/sf-factory resolve-escalation`.
When YOU hand off, follow `session-launch-protocol.md` verbatim (auto-launch successor; never kill predecessor).
