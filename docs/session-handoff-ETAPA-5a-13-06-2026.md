# Session handoff — ETAPA-5a → ETAPA-5b, written 13-06-2026 ~00:15 EEST (succession per D-0037)

**For ETAPA-5b (Main-Architect successor).** POINTER document (Doctrine §9) — authoritative history = `docs/decision-log.md`: read **D-0031 → D-0037 end to end** (ETAPA-5a's shift; D-0024→D-0030 was the prior shift, covered by the archived 12-06 handoff). Designs, git log, and the ERP macro log carry the rest. Auto-memory gives founder/infra/project profile; canon arrives via the launcher.

## Where everything lives

| What | Where |
|---|---|
| Factory history (spine) | `docs/decision-log.md` D-0001…**D-0037** |
| Designs | control-plane **v1.11** (CCR-1..11), dashboard **v1.3** (§11 /costuri), phase-seeding v1.1 |
| Runbooks | `first-live-run.md` (deploy ritual: disarm→stop→start→re-arm, kill-cheap windows) + **`session-succession.md`** (you exist because of it — hand the marker forward at YOUR succession) |
| Live state | `sf-factory status` / `http://server-e9:8377` (+ **/costuri** = founder's cost page) — never trust this file's snapshot |
| Context guard | hook `~/.claude/hooks/sf-architect-context-guard.sh`; marker `~/.claude/sf-architect-session` (YOUR first duty: write your session id there) |

## State at handoff (SNAPSHOT — re-check via status)

- Factory LIVE (pid 513777, just deployed **CCR-11 capacity governor** — auto-drain on usage limits, haiku probe, auto-resume of limit-marked failures; `enabled: true` in golden config), watchdog ARMED.
- foundation: skeleton + config-registry + **core-entities DONE-or-MERGE_GATE** (check), **auth-access in BUILD — FIRST CRITICAL stage: expect the A4 human-gate decision card for the founder at AWAITING_HUMAN**, document-engine in VALIDATE (post §3.4-varargs rework). Rest PENDING behind DAG. Proving hold: post-foundation only inventory-procurement dispatches.
- Day totals (ledger): ~$300+ API-equivalent; clean structural stage ≈ $58; ERP projection $3.5-6k (D-0035). Founder's decision gate = end of inventory-procurement (~$500-800 paid post-15-06).

## Immediate work items (in order)

1. **Watch the conveyor; triage architect-lane escalations** with evidence (DB mode=ro) — precedents D-0035/D-0036; resolutions carry your `--reason` into the re-entered agent's prompt (CCR-9). Limit-class `agent_run_failed` ones the governor now handles ALONE — don't touch them during a hold.
2. **A4 (auth-access critical gate):** the founder gets his first real product decision card — the channel's purpose; watch it land and that ntfy/dashboard render correctly.
3. **Pending founder answer (D-0036/F4, mode 1):** cost-display semantics (CLI-exact vs config-formula). Asked twice in chat, unanswered — re-surface ONCE at a natural moment, then record in the log either way.
4. **Deploy discipline:** changes ride safe windows (no/young agents). Factory-side builds in ISOLATED WORKTREES (Agent tool); sequential builders when files overlap (GLOSS/dashboard.py is a collision hotspot).
5. **Watch-item registry (no action without trigger):** CCR-11 residuals (forever-failing probe = silent drain, re-page candidate; `--until-blocked` exits during hold; probe spend per-unit invisible); phase spawns ungated by incident-7 (D-0036); killed-run spend unledgered (F9); codex model-attribution `default`; per-project model routing CCR (waits for the founder's parallel weak-profile project); lockfiles in Tier-2 diffs; iteration-window re-trip risk on document-engine (contaminated rows 2-4 remain — if max_fix_iterations fires there, resolve referencing D-0035).

## Working-mode learnings (keep)

- The proven loop: incident → root-cause (§11) → micro-slice (design-in-prompt bounded / full design for founder surfaces) → adversarial review where judgment-heavy → builder (worktree) → non-executor verifier → merge → D-entry → deploy at window. Two-round gauntlets caught critical defects TWICE (CCR-9 reason-merge; §11 refresh-collapse) — never skip the verifier.
- **Capacity:** D-0025 posture is now MECHANIZED (governor). Manual drain procedure proven 12-06 ~19:40Z (let in-flight finish, stop before next spawns). The 5h window + monthly spend cap are DIFFERENT limits; new billing starts 15-06 (founder's info — projection uses API list prices, founder-tunable in `pricing.*`).
- **Sequencing law:** recalibrate → deploy → resolve (level-check triggers re-fire while the old config is pinned in the live instance).
- Founder protocol unchanged: Romanian, glossed, options-with-recommendation, tables; he reads /costuri now; UX-first law binding; he tests product reality only at inventory-procurement end — keep him pointed there.
- Your succession: when the context-guard note appears, finish the work unit, write the handoff, launch ETAPA-5c per the runbook, hand the marker, go silent.
