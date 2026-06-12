# Session handoff — written 12-06-2026 ~14:30 EEST, mid–Etapa 5 (first production day)

**For the next Main-Architect session.** POINTER document (Doctrine §9) — authoritative history = `docs/decision-log.md` (read **D-0024 → D-0030 end to end**: the entire first production day lives there), the designs, git, and the ERP macro log. Auto-memory gives founder/infra/project profile; canon arrives via the launcher (now defaulting `--effort xhigh`, founder-approved 12-06).

## Where everything lives

| What | Where |
|---|---|
| Factory history (spine) | `docs/decision-log.md` D-0001…**D-0030** |
| ERP product decisions | `docs/projects/erp/decision-log.md` (D-ERP-0001 = intake ratification) + `docs/projects/erp/PROJECT.md` v1.0 RATIFIED |
| Designs | control-plane v1.8 (CCR-1..8), dashboard v1.2 (§10 UX slice), phase-seeding v1.1 |
| Contracts in force (ERP) | `erp-workspace/_factory/contracts/` (c1–c4 cross-phase, CANONICAL; + `phase-foundation/f1–f7`) |
| Live state | `sf-factory status` / dashboard `http://server-e9:8377` — never trust this file's snapshot |
| Runbook (ops ritual) | `docs/runbooks/first-live-run.md` — disarm-before-stop, seed-while-stopped, bootstrap, checkpoint |

## State at handoff (SNAPSHOT — re-check via status)

- **Factory LIVE in tmux `factory`, watchdog ARMED.** foundation RUNNING: `skeleton` DONE (merged, full gauntlet incl. Tier-2 codex on a ~300KB stdin prompt); `config-registry` in AUDIT (dual auditors), `document-engine` in VALIDATE; 11 stages PENDING behind them. Proving hold: post-foundation only `inventory-procurement` dispatches (D-ERP-0001 §5).
- **All five day-1 defect fixes are deployed** (budgets 30/75/150M; BUILD no-op acceptance; prompts via stdin; AGENTS.md scrub; UX dashboard incl. escalations strip + `resolve-escalation` CLI — used twice in production).
- Working tree clean at `ab0bb8f`; agent-worktree dirs under `.claude/` (gitignored).

## Immediate work items (next session, in order)

1. **D-0030 dispositions at the next deploy window**: builder-prompt spec-boundary line ("never modify spec.md or any `_factory` artifact except build-notes.md") — same pattern as the no-self-commit line; the mechanical registered-ref re-hash guard is design-with-care, trigger = second occurrence.
2. **Watch the conveyor**: escalations → triage via `sf-factory resolve-escalation <id> <verdict> --reason …` (works against the live orchestrator); decision cards are the founder's, not yours. A-criteria: A4 fires naturally at the first `critical` gate (`auth-access` next on the schema chain); A3 needs a CONTESTED audit finding (so far only comply); A1 settles on the first intervention-free stage.
3. **Watch items registered**: phase-PLANNING has no token cap (3.26M observed — config key if it repeats); lockfiles bloat Tier-2 diffs (exclusion globs on evidence); dashboard GET poll timeouts (cosmetic so far).

## Working-mode learnings of day 1 (keep these)

- **Factory-side builds happen in ISOLATED WORKTREES** (`Agent` tool isolation) — a dirty factory main tree false-pages the out-of-bounds detector at the next merge gate.
- **Deploy = next safe window** (no agents in flight), disarm → stop → start → re-arm; in-flight agents killed at stop re-run idempotently but cost real tokens.
- Operator DB surgery is RETIRED for escalations (CLI exists); anything else still needing surgery = a gap to register + close, same-day pattern.
- The proven loop: incident → root-cause (Doctrine §11) → micro-slice (design-in-prompt for bounded fixes, full design for surface changes) → adversarial review where judgment-heavy → builder (worktree) → non-executor verifier → merge → D-entry → deploy at window.
- Founder protocol unchanged: Romanian, glossed, options-with-recommendation; he reads the dashboard now with real tables — keep it true to UX-first law (ERP-start `technical-context.md` §UI/UX is binding for product AND channel).
