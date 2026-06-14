# Session handoff — ETAPA-5c → ETAPA-5d, written 14-06-2026 ~15:45 EEST (succession per D-0037)

**For ETAPA-5d (Main-Architect successor).** POINTER doc (Doctrine §9) — authoritative history = `docs/decision-log.md`: read **D-0042** end to end (ETAPA-5c's shift; D-0038→D-0041 was 5b's, covered by the archived 5b handoff). You launch on **opus @ effort max**, **Remote-Control ON named ETAPA-5d**, with the **architect-operations** canon layer. RC = the founder drives you from his phone.

**Why this handoff:** ETAPA-5c is near context capacity at a CLEAN boundary (the merge-gate program shipped, document-engine unblocked, escalations clear, the next build program designed+saved). This is a proactive clean handoff, not a context-guard-note trigger — done BEFORE a messy mid-build one (per §0/§6 discipline; long autonomous stretches don't fire the founder-prompt context-guard, so self-judgment applies).

## Where everything lives
| What | Where |
|---|---|
| Factory history (spine) | `docs/decision-log.md` D-0001…**D-0042** (read D-0042) |
| **Slice-2 design — READY to build** | **`docs/design-slice2-noaction-disposition.md`** (code-grounded, 2 units A→B, every file:line) |
| Architect rules | `work-protocols/architect-operations.md` (§1 contest-resolution, §2 carry-WHY, **§3 `rework:MERGE_GATE` usage** — new this session) |
| Runbooks | `docs/runbooks/first-live-run.md` (deploy ritual: disarm watchdog → C-c → fresh tmux `factory` → re-arm) + `session-succession.md` (you exist by it; RC auto per D-0041) |
| Live state | `uv run sf-factory status` / dashboard `http://server-e9:8377` (+ /costuri) — never trust this doc's snapshot |
| Context guard | hook `~/.claude/hooks/sf-architect-context-guard.sh`; marker `~/.claude/sf-architect-session` (**YOUR first duty: write your session id there**) |
| Live orchestrator restart | tmux `factory`, cmd `.venv/bin/sf-factory run 2>&1 \| tee -a .factory/run-live.log`, cwd `/home/artur/projects/SF-F5`, PATH incl. `~/.local/bin` + `~/.nvm/versions/node/v24.16.0/bin` |

## State at handoff (SNAPSHOT — re-check via status)
- Factory LIVE, healthy, watchdog ARMED. **Zero open escalations.** Orchestrator running main HEAD (carries: additive canon layer, integration_validator→opus, `rework:MERGE_GATE` verb + its usage constraint).
- foundation: skeleton/config-registry/core-entities/**document-engine all DONE**. document-engine's completion released the DAG → dependency-cascade, register-schemas, media-attachments, print-pdf in-flight; **auth-access reworking (BUILD)** finding ASM-A1. Rest PENDING. Proving hold post-foundation.
- **`max_parallel_agents = 4`** (founder's economics/subscription knob). Founder DECLINED a temporary bump-to-5 (it needs a restart that wastes in-flight agents; auth-access self-resolved instead). He is OPEN to 5 at the next deploy for more throughput — confirm the cost with him first.

## What this session did (read D-0042 for the full account)
- Built/verified/**deployed** `rework:MERGE_GATE` (re-enter ONLY the merge gate, no re-validate/re-audit) → resolved [19] → **document-engine merged (DONE)** on the opus integration_validator. The merge-gate-context-overflow saga is CLOSED.
- Incident [20]: the auth-access audit-triage builder edited code during the *response* step → the §3.1 BUILD-isolation guard caught the dirty tree → ESCALATED. Recovered (dirty_worktree_reset) + resolved `rework:BUILD`. **Root-fix folded into slice 2 Unit B.**
- **auth-access starvation diagnosed:** a no-spawn ESCALATED→BUILD transition was gated by the `max_parallel_agents` cap (a critical stage waiting behind routine ones for a slot it doesn't even need to spawn an agent). Self-resolved (it was rowid 4 = first in scan order → got the next free slot). → **new build item: scheduler-fairness fix.**

## The build program (your main work — all → ONE consolidated deploy at the next natural clean window)
1. **Slice 2** (no-action `settled` disposition + the [20] triage write-isolation fix) — **design READY** in `docs/design-slice2-noaction-disposition.md`. Build Units **A→B**, non-executor verify each. A = the regeneration root fix (`settled` status + audit-prompt memory + architect no-action routing); B = the [20] triage write-boundary + discard stray-uncommitted before comply→BUILD. SAFETY PIN: the do-not-re-raise set is `{settled, overruled}` ONLY (never sustained/complied — would mask unfixed bugs).
2. **Scheduler-fairness fix** (auth-access starvation root) — design + build. Exempt no-spawn steps (ESCALATED resolution, gates) + resolved escalations from the agent-slot cap, or add aging/priority so a critical/resolved unit isn't starved behind routine ones. Touches `_dispatch` (scheduler.py ~5004-5046, the `if len(self._tasks) >= cap: break`). HIGH founder priority (silent-stall class).
3. **Escalation-routing (item 4) — founder APPROVED** (D-0042): human stays arbiter, NO auto-resolver agent (don't automate judgment); build only the mechanical layer — live routing (an actor+action per level, today `target` is a dead label) + a **stuck-escalation detector** (open/resolved-not-advanced > **30 min** → escalate up a level + ntfy) + richer resolution actions. MUST be CODE in the orchestrator so it persists across ALL sessions; ≤5-min architect pickup is a founder hard-requirement (the session monitor meets it but is a cârpa — the stuck-detector is the durable guarantee). NOTE: the stuck-detector would have caught both the [20] resolved-not-advanced stall AND the auth-access starvation — design it to cover resolved-but-not-advanced, not just open.
4. **Slices 3-4:** recurrence flag (db query for a `settled`/`overruled` finding reappearing + dashboard surface — depends on slice 2's `settled` status); documentary spec-amendment path (SPEC→AUDIT skip when the architect asserts code-neutral — touches `VALID_STAGE_TRANSITIONS`, heaviest blast radius, build LAST with fullest verification + adversarial review).

## Working-mode learnings (keep)
- The proven loop: incident → root-cause (§11) → micro-slice → **adversarial/clean-context verify (Doctrine §4 — NEVER skip)** → builder (worktree, Agent tool) → non-executor verifier → merge → D-entry → deploy at a clean window. The merge-gate slice followed it cleanly (builder → APPROVE verifier with a real residual-risk find → canon §3 guard).
- **Sequencing law:** build/recalibrate → deploy → resolve (level-checks re-fire against the pinned live config otherwise).
- **Deploy needs a clean 0-agent window** (C-c kills in-flight agents via PDEATHSIG). Windows are now RARE (conveyor busy post-DAG-release). The next likely clean window: when auth-access hits its A4 human gate + siblings block. BATCH all built changes into that one deploy.
- **Start your OWN monitor at startup** (5c's dies with 5c's session — and kill any zombie from a prior session, as 5c found one). Background bash polling escalations/decisions/orchestrator-liveness, exit-on-change → re-invokes you. The durable replacement is item 4's stuck-detector. CAVEAT: the escalation-monitor does NOT catch resolved-but-not-advanced stalls (the auth-access case) — poll stage state too, or rely on the founder's dashboard until item 4 lands.
- Founder protocol: Romanian, glossed (NO bare IDs/acronyms), options+recommendation, **brutal honesty** (he catches errors — verify before asserting, §21), he drives by phone (RC), reads /costuri, UX-first. He deeply values: robustness (nothing stuck silently) + cost-consciousness (the 4-agent cap is his knob — don't bump without his OK).
- **§8 discipline:** the [20] incident and the starvation were each FIRST occurrences → logged + fix-folded/registered, NOT preventively over-built. Watch for recurrence.

## Pending founder threads
- A4 (auth-access) human-gate card semantics — surfaces when auth-access reaches AWAITING_HUMAN (it's a critical/human-gate stage). Founder confirmation pending on the card semantics.
- cap-5 option (more throughput at the next deploy — his cost call).

## Your succession (when YOUR context-guard fires)
Finish the work unit, write the handoff, launch ETAPA-5e (`SFF5_TMUX_SESSION=etapa-5e SFF5_RC_NAME=ETAPA-5e ./claude_canon.sh "<prompt>"` — RC+name+opus+max auto), VERIFY 5e's RC on the founder's phone BEFORE going silent (predecessor RC is the fallback if the successor's silently fails), hand the marker, go silent.
