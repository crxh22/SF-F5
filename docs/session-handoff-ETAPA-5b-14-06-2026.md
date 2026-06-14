# Session handoff — ETAPA-5b → ETAPA-5c, written 14-06-2026 ~13:30 EEST (succession per D-0037)

**For ETAPA-5c (Main-Architect successor).** POINTER document (Doctrine §9) — authoritative history = `docs/decision-log.md`: read **D-0038 → D-0041 end to end** (ETAPA-5b's shift; D-0031→D-0037 was 5a's, covered by the archived 13-06 handoff). You launch on **opus @ effort max** with the **architect-operations** canon rule already in your system prompt (D-0040), and with **Remote Control ON, named ETAPA-5c** (D-0041) — the founder drives you from his phone.

## Where everything lives

| What | Where |
|---|---|
| Factory history (spine) | `docs/decision-log.md` D-0001…**D-0041** |
| Designs | control-plane **v1.11**, dashboard **v1.3** (/costuri), phase-seeding v1.1 |
| Runbooks | `first-live-run.md` (deploy ritual: disarm→stop→start→re-arm) + **`session-succession.md`** (you exist by it — hand the marker forward at YOUR succession; RC is now auto per D-0041) |
| Architect rule | `work-protocols/architect-operations.md` — in YOUR canon (and phase_architect/spec_agent's), not the shared one |
| Live state | `uv run sf-factory status` / `http://server-e9:8377` (+ /costuri) — never trust this file's snapshot |
| Context guard | hook `~/.claude/hooks/sf-architect-context-guard.sh`; marker `~/.claude/sf-architect-session` (YOUR first duty: write your session id there) |

## State at handoff (SNAPSHOT — re-check via status)

- Factory LIVE on **opus** (heavy claude roles) + **codex gpt-5.5/xhigh** (cross-model audit). Budgets: structural **120M** (D-0039), critical 150M. Watchdog armed, capacity governor enabled.
- foundation: skeleton/config-registry/core-entities **DONE**. **document-engine** at the MERGE GATE, **blocked on escalation [19]** (integration_validator overflowed gpt-5.5's context — see below). **auth-access** (critical, human-gate) re-doing SPEC after [18] resolved rework:SPEC; will eventually surface the **A4 founder decision card** — watch for it. Rest PENDING behind the DAG. Proving hold post-foundation.
- Day ledger climbing (document-engine alone ~82M genuine tokens / ~$250 — it was over-scoped; decomposition is the registered Opus-era lesson).

## Immediate work items (in order)

1. **DEPLOY the pending config + resolve [19].** `integration_validator` is rerouted codex→**opus** in `factory.config.yaml` (D-0041, founder-approved) but NOT deployed. At a clean window (0/young agents): deploy ritual → then resolve [19] `agent_run_failed`. Note: rework:VALIDATE re-runs validate+audit+integration (~+20M → may re-trip 120M; bump cap or find a lighter path — this is exactly the "re-run only the failed gate" gap below). The integration validator on opus (1M) will then FIT the merge payload.
2. **Watch the conveyor; triage escalations — but you now have a MONITOR.** I left a background bash monitor (polls escalations/decisions/orchestrator-liveness, alerts you) — **it dies with MY session; set up your OWN at startup** (or better, build the stuck-detector in item 4). You get NO ntfy for escalations (those go to the founder); without a monitor you only learn by polling.
3. **BUILD the finding-regeneration program (founder approved option c + documentary path, D-0039/D-0040).** Three slices, each design→build→non-executor-verify→merge, then one consolidated deploy:
   (a) **No-action finding disposition** (root) — a 4th triage verb "accurate·acknowledged·closed". Design subtlety: how a later clean-context audit learns a finding was settled — feed settled findings into the audit prompt + auto-close matches at executor triage. Touches scheduler audit-triage, the findings-response schema, the audit prompt, db.
   (b) **Recurrence flag** (symptom backstop) — db query for an overruled/settled finding that reappears + dashboard surface.
   (c) **Documentary spec-amendment path** — SPEC→AUDIT skip when the architect asserts the amendment is code-neutral (skips needless rebuild+revalidate). Touches `VALID_STAGE_TRANSITIONS` (core contract — heaviest blast radius, build LAST with the fullest verification + adversarial review).
4. **BUILD the escalation-routing mechanism (D-0041, founder "in principiu ok"; CONFIRM the 30-min threshold + auto-resolver yes/no first).** (i) **stuck-escalation detector** — open > T_architect (~30min) without resolution → auto-bump UP a level + ntfy (the Doctrine §20 guarantee that nothing sits silently); product-level escalations ntfy immediately; existing `decision_latency_alert: 24h` stays for founder product decisions. (ii) **routing by resolver competence** — stage-rework→phase, config/infra→main, product→founder (today everything is flatly `phase_architect` with no actor). (iii) **richer resolution actions** (re-run only the failed gate — see item 1). NOT automating escalation JUDGMENT — reliable notification + stuck-detector + routing, human arbitration kept.
5. **Watch-item registry (no action without trigger):** finer stage DECOMPOSITION on Opus (document-engine proved structural stages can be too big — DoD §13 trigger if escalation frequency climbs); integration-validator diff-scoping when many siblings merge (1M could be tight late in a phase); auth-access audit-contest churn (will subside once 3(a) lands); contaminated document-engine fix_iterations; codex availability (founder may change his codex subscription — treat as possibly-unavailable until he confirms; a codex outage fails closed like the Fable one).

## Pending founder confirmations
- **30-min stuck-escalation threshold** + whether to also build the **auto-resolver agent** (item 4). He was mid-confirming when he pivoted to the RC blocker.
- A4 (auth-access) human-gate card semantics when it lands.

## Working-mode learnings (keep)
- The proven loop: incident → root-cause (§11) → micro-slice → **adversarial review where judgment-heavy** → builder (worktree, Agent tool) → **non-executor verifier (Doctrine §4 — never skip it)** → merge → D-entry → deploy at window. The verifier APPROVE on the layered-canon slice this session was clean; trust the process.
- **Sequencing law:** recalibrate → deploy → resolve (level-check triggers re-fire against the pinned live config otherwise).
- **The finding-regeneration root (the session's big find):** the triage vocab {comply,contest,duplicate} has no "accurate-no-action" → such findings loop every audit (CE-AUDIT-1 ×3, AA-A2 ×2+). The architect rule (now in your canon) + the no-action disposition (item 3a) fix it. When you resolve a prevailed contest: fix the GENERATING artifact (spec text), never defer as "editorial debt".
- **Brutal honesty includes self-correction:** I asserted document-engine's 82M was mostly infra-waste; it was <1% (verify before asserting — the founder catches it).
- Founder protocol: Romanian, glossed (NO bare IDs/acronyms), options-with-recommendation, tables, he reads /costuri and drives by phone (RC). UX-first; he tests product reality only at inventory-procurement end.
- **Your succession:** at the context-guard note, finish the work unit, write the handoff, launch ETAPA-5d (the launcher now does RC + name + opus + max automatically — `SFF5_TMUX_SESSION=etapa-5d SFF5_RC_NAME=ETAPA-5d ./claude_canon.sh "<prompt>"`), VERIFY the successor's RC on the founder's phone before going silent, hand the marker, go silent.
