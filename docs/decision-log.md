# Macro Decision Log

Append-only (DoD §3.1). Newest entry last. Format: `D-NNNN — date — owner — decision`, then rationale/source.

---

## D-0001 — 2026-06-10 — founder — Full rights on dev server

Passwordless sudo for user `artur` on server-e9, approved by founder ("ți-aș da maxim drepturi și credențiale (până și acces sudo)", chat 10-06-2026). Rationale: non-production, disposable, tailnet-isolated server; removes founder-availability blockage. Execution: founder runs the sudoers one-liner (see `docs/decision-request-kickoff-10-06-2026.md`, Decizia 1). **Status: pending execution.**

## D-0002 — 2026-06-10 — founder — Why SF was reset to SF-F5 (calibration doctrine)

Founder, verbatim (chat 10-06-2026): "vechiul pipeline era calibrat defensiv pe slăbiciunile modelelor din era Opus 4.x — etape mărunte, 6 agenți pe conveier, audit dens peste tot — iar cu Fable 5 acea postură devine overhead pur, care costă timp și tokeni fără să mai cumpere calitate. Varianta nouă păstrează doar invarianții independenți de model (control plane determinist, validare de non-executor, contract-first, gate-uri umane) și mută tot ce e dependent de capabilitate în config și falsificabilitate — deci pipeline-ul se recalibrează pe dovezi la fiecare generație de model, în loc să fie reproiectat."

Implications binding on implementation:
- Model-independent invariants live in architecture; capability-dependent calibration lives ONLY in `factory.config.yaml` + DoD §13 falsifiability triggers.
- `~/projects/SF` is reference-only harvest: point mechanics (worktree management, NDJSON parsing, transition-table shape) may be consulted read-only and rewritten to the new design; its architecture, stage sizing, and audit density are explicitly NOT inherited.

## D-0003 — 2026-06-10 — founder — Cross-model auditor = codex CLI

codex CLI (authenticated via founder's ChatGPT subscription, verified in environment audit) is the different-model-family auditor / integration validator (DoD §2.5, §5.2, §7). Zero marginal cost. Founder: "ok".

## D-0004 — 2026-06-10 — founder — Founder channel = ntfy.sh, topic `claude-artur-md-hello`

Public ntfy.sh instance, founder-chosen topic, app already installed on his phone. Founder explicitly accepts the topic being non-secret: "nu îmi pare nimic sensibil ce va fi transmis prin el și nu văd risc de daună prin asta". Constraint kept from DoD §9 regardless: payloads stay minimal — title + deep link, never artifact content. Self-hosted ntfy deferred until incidents demand it (Doctrine §8).

## D-0005 — 2026-06-10 — founder — DoD §15 proposals confirmed; starter config values

Model routing per role; per-stage token budgets routine 300k / structural 1M / critical 2M; proving ground = Foundation, then inventory/procurement; consultation registry = CP-1 only. Founder: "da, trebuie doar să stabilim valorile din config" → starter values set in `factory.config.yaml` by Main Architect; all recalibrable on DoD §13 evidence.

## D-0006 — 2026-06-10 — founder — Founder timezone = Chișinău, Moldova

Server clock moves to `Europe/Chisinau` once sudo lands. Founder-facing times rendered in Europe/Chisinau; machine-parsed timestamps remain ISO 8601 UTC (conventions.md).

## D-0007 — 2026-06-10 — main architect — Control-plane stack

Python 3.12 + uv project (package `sf_factory`), pydantic v2 for config/verdict schema validation, pytest, ruff. Per DoD §16.3 (implementation-session technology choices). Concurrency model is decided inside the control-plane design doc after adversarial review, not here (Doctrine §12).

## D-0008 — 2026-06-10 — main architect — Build mode for the factory itself (bootstrap scaffolding)

The factory is built by the Main Architect session orchestrating parallel subagent teams, with verification always by a non-executor agent in clean context (Doctrine §4) — founder-endorsed mode ("echipă de subagenți, poate ultracode"). All durable state lives on disk in git; sessions are disposable. This scaffolding mode ends when the deterministic control plane takes over routine coordination.

---

*Note — 2026-06-10: tentative power outage at 15:00 UTC (18:00 Chișinău); confirmation pending, shutdown decision with founder ~14:40 UTC.*
