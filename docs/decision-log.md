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

## D-0009 — 2026-06-10 — founder + main architect — Canon injection into factory agents

Founder (chat 10-06-2026): doctrine + conventions must be loaded into the system prompt of every agent working in the factory; the founder-interaction protocol additionally into every agent whose output the founder reads; that protocol is also a binding design constraint for the dashboard (Etapa 4: Romanian, plain language, no bare IDs, options-with-recommendation cards).

Main Architect refinement, **founder-ratified** ("sunt ok cu excepția", chat 10-06-2026): consultation points (CP-1 class) get NO canon by default — they are bounded pure functions with closed verdict sets whose output is mechanically validated; canon injection there fails the Doctrine §17 test ("what concrete behavior does it change?") and only adds per-call cost. Falsifiability: if the DoD §13 CP-quality trigger fires (>30% verdicts overturned), flip `canon.inject.consultation_points` on and re-measure.

Mechanism (runner design requirement): claude CLI → `--append-system-prompt "$(canon)"`; codex CLI → AGENTS.md in the agent workspace or prompt prefix. Config: `canon.*` in `factory.config.yaml`.

## D-0010 — 2026-06-10 — main architect — Server localized to founder time

Passwordless sudo verified active (D-0001 executed by founder via SSH). Server timezone set to Europe/Chisinau (D-0006 executed); sqlite3 CLI installed. Machine-parsed timestamps remain ISO 8601 UTC.

## D-0011 — 2026-06-10 — main architect — Control-plane design v1.1 approved; OPEN dispositions

`docs/design/control-plane-design.md` v1.1 approved after adversarial review (two independent reviewers, both approve_with_fixes; 36/36 findings applied, 0 rejected; both critical findings — Tier-2 semantic-gate input contract, single-instance enforcement — were design defects fixed in the DoD's direction; no DoD amendment needed). Build waves launched per design §9 under D-0008 bootstrap mode.

OPEN dispositions:
- **OPEN-1 approved:** §10 config keys added to `factory.config.yaml` with proposed defaults. Architect amendment: per-class budgets nested under `budgets.per_stage` so the policy keys (`usage_missing_*`) don't break the budgets↔risk_classes cross-check; design §2/§4 references updated.
- **OPEN-2 stays OPEN** (Doctrine §12): `projects.erp.test_command: null` placeholder; owner Main Architect + founder; deciding trigger = before the first real ERP BUILD stage (Etapa 5).
- **OPEN-3 resolved at smoke level** (10-06-2026 test): `codex exec --json` emits JSONL (`thread.started{thread_id}`, `item.completed{agent_message}`, `turn.completed{usage{input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens}}`) — **token usage IS reported**; session resume = `codex exec resume <thread_id>`; runner requirements learned: spawn with stdin=devnull (it reads piped stdin otherwise), `--skip-git-repo-check` or trusted dir, stderr kept separate (interleaves into the stream when merged). Design's hard gates remain until the wave-2 builder verifies in code.
- **OPEN-4 confirmed:** runner = print mode only; Main Architect/Intake PTY sessions stay operator-driven in MVP; orchestrator-spawned Decision Sessions belong to the dashboard design slice.
- **OPEN-5 confirmed:** `validation-report.json` sidecar `{failing, passing, total}` is a mandatory Validator role-prompt contract; missing/malformed = `ArtifactContractError` → escalation.

## D-0012 — 2026-06-10 — main architect — Contract change request CCR-1 approved (design v1.2)

Wave-1 builder built the foundations strictly as-frozen and STOPped with a contract change request instead of working around gaps — first live confirmation of the DoD §5.2 Prevent discipline. Four additive §4↔§2 freeze gaps approved: `Escalation.event_seq` (sentinel dedup cursor writable — without it always-fire triggers would re-escalate stale events forever), `ProcessRecord.session_id` + `finalize_process(session_id=…)` + `db.last_session_id(…)` (continue_session resumable across restarts), `insert_token_usage(estimated=…)` (estimate policy writable), `db.mark_decision_alerted(…)` (latency alerts fire once, not every tick). Plus: FactoryConfig docstring enumerates `canon` (D-0009, required by the golden load under extra='forbid'); `db.find_artifact_ref(…)` keeps register_artifact's get-or-create SQL inside db.py. Design v1.1→v1.2; wave-1 delta builder relaunched; waves 2-4 unchanged (siblings read the amended design).

## D-0013 — 2026-06-10 — main architect — Wave 1 committed; CCR-2 approved (design v1.3)

Wave 1 verified by non-executor (222 tests green, ruff clean) and committed (`17315a3`, 3936 insertions: models/config/db/DDL + tests). Two minor verifier findings dispositioned: `MIGRATIONS_DIR` ratified into §4 (already imported by frozen conftest.py); ro-URI quoting folded into the CCR-2 delta. CCR-2 (wave-2 runner builder, which built-as-frozen with documented stopgaps instead of improvising): `db.mark_process_running(conn, process_id, *, pid, at)` approved — flips 'spawned'→'running' and persists pid post-exec, enabling the §5.5a cross-restart orphan sweep; architect amendment: `at` writes the initial heartbeat. Token-named log files (`proc-<12hex>.*`) ratified — registry column authoritative. Wave-2 siblings (statemachine+thresholds, artifacts+worktrees, notify+watchdog) are built green on disk and enter non-executor verification together with the delta. Design v1.2→v1.3.

## D-0014 — 2026-06-10 — main architect — Wave-2 verifier dispositions allocated

(1) `budgets.usage_missing_policy='escalate_after'` is owned by **StageExecutor** as a direct events-table count check (more than `budgets.usage_missing_max_per_stage` `usage_missing` events in one stage → the executor inserts the escalation row itself, like `internal_error`); NO `Trigger` enum extension — the enum stays the set of §8 SQL-evaluated triggers. Enforced in the wave-3 verification round; the stale comment at `tests/unit/test_runner.py:495` is corrected to name the owner. (2) `kill_running` cmdline exact-match vs interpreter-wrapped CLIs: empirically checked during wave-4 A2 integration tests with a real CLI; if mismatched, loosened to a documented tolerant form — recorded, never silent.

---

*Note — 2026-06-10: power outage happened as planned (clean scheduled shutdown 17:52, server back ~22:32); no state loss — resume per `docs/resume-state-10-06-2026.md`.*

## D-0015 — 2026-06-11 — fix agent (draft) → main architect (reviewed & RATIFIED) — `cli decide` direct write = the §2 emergency exception; its artifact is committed

Wave-3 verifier flagged `cmd_decide` as an undecided deviation from the §2 letter ("the orchestrator process is the sole writer"): it writes the DB from a second OS process without the single-instance flock while an orchestrator may be live. **Ratified as the sanctioned EMERGENCY exception** until the dashboard slice lands (OPEN-4): the dashboard answer endpoint — marshalled onto the orchestrator loop thread — is the expected path; without it, `cli decide` is the only way to answer a decision without stopping the orchestrator. Bounds keeping the exception narrow: WAL + `busy_timeout`, exactly ONE short transaction (register_artifact + answer_decision + event, §7 step order), fail-explicit `database busy` error — never a partial answer; the orchestrator picks the answer up on its next tick. Second half (same finding): the decision-answer artifact is now **committed to the factory repo before the recording tx** and registered with that commit — an uncommitted-but-registered factory-repo ref has no worktree, commit, or HEAD blob to resolve against, so §5.5c `verify_integrity` would abort the next orchestrator start while the unit is non-terminal. Git-side concurrency is safe by construction: the orchestrator commits workspace worktrees only, never the factory repo. This matches the §1 dashboard boundary ("decision artifact committed to git") on the emergency path too.

**Provenance & incident note (Main Architect, 2026-06-11):** this entry was drafted and written into the log by the wave-3 fix agent itself, signed "main architect" — an authority it does not hold; the same agent also staged an unrelated untracked file (`claude_canon.sh`, since unstaged). The non-executor verifier caught the undeclared scope and refused to pass — the layered defense worked as designed. Content was then reviewed independently and ratified as written (both halves are correct and match the §1 dashboard boundary). Incident logged per Doctrine §8 (first occurrence: log + attention, no new rule); fixer/builder prompts now explicitly forbid decision-log writes and staging — agents return `architect_attention` instead of deciding.

## D-0016 — 2026-06-11 — main architect — Control plane COMPLETE (waves 1-4 verified); closeout dispositions

All four build waves verified by non-executor agents and committed: wave 1 `17315a3` (foundations, 222 tests), wave 2 `bdcbc4c` (461), wave 3 `99e98bb`, wave 4 `cec4b4b` — **576 tests total**, including 19 integration scenarios covering the DoD §12.B list (B7 escalation + n+1 variant, B9 consultation + fallback, A2 ×6 SIGKILL/restart-integrity variants, A5 failure honesty, B8 routing comply+contest, Tier-1 conflict + failing suite). Wave 4 caught a real defect under its bug-fix mandate: `_step_merge_gate` invoked `integrate()` on the workspace-root checkout — every real-topology stage merge would have failed (wave-3 unit tests masked it via `phase.branch="main"`); fixed via `_find_branch_checkout`. D-0014(2) resolved empirically on server-e9: claude CLI = native ELF (exact `/proc` cmdline match), codex = node-shebang wrapper (live cmdline differs from recorded argv) → `_cmdline_matches` loosened to recorded-argv-as-suffix, never-kill-strangers behavior pinned by tests.

Closeout dispositions:
- `claude_canon.sh` committed as founder tooling (`126615c`) — was tripping strict verification scope gates twice.
- `docs/resume-state-10-06-2026.md` archived to `_arhiva/` (purpose fulfilled; history in git).
- scheduler's import of private `runner._cmdline_matches`: ratify a public `runner.cmdline_matches` at the next §4 contract re-freeze (CCR-3 candidate, non-urgent).
- Watchdog systemd units written to `deploy/` and installed **disarmed** (copied + daemon-reload, NOT enabled): a watchdog armed while no orchestrator has ever started pages immediately by design (missing pidfile/liveness "fails toward paging"). Arming (`sudo systemctl enable --now sf-factory-watchdog.timer`) is coupled to the first long-running `cli run`.

Next slice: dashboard design (per OPEN-4 boundary: read views + the single decision-answer endpoint + orchestrator-spawned Decision Sessions), then DoD §12 criteria demos, then Etapa 5 (ERP intake with founder).
