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

## D-0017 — 2026-06-11 — main architect — Dashboard design v1.1 approved (CCR-3); OPEN dispositions; build launched

`docs/design/dashboard-design.md` v1.1 approved after adversarial review (two reviewers, 18/18 findings applied, 0 rejected). Key catches: the control-plane decision-request templates were English machine text with bare tokens — re-authored in Romanian **within this build**, so the first card the founder ever sees obeys the founder protocol; the pinned CSP would have silently killed the session poll script; the dashboard supervisor now contractually contains every exception (incl. its own ntfy publish failures) with paging dedup.

Ratification rider recorded:
1. **DoD §14 deviation, accepted:** the Decision-Session page is the rendering of the card's inline input (JS-free reload page = §14 baseline; the poll script is a convenience inside §14's "refresh/poll is enough" allowance) — not a richer session UI.
2. **CCR-3** (control-plane §4 additive amendments, design v1.3→v1.4 — annotated there per Doctrine §19): `models.GATE_ANSWERS` (deliberate alias drop: `changes_requested`), `Scheduler.__init__(dashboard=None)` + contained `_dashboard_supervisor`, `config.ModelRoute.tools` (tools-off Decision Sessions — structural no-write enforcement, not prompt-level), `thresholds` `context_budget` excludes `role='decision_session'` (OPEN-D4 resolved mechanically, Doctrine §20), `runner.cmdline_matches` promoted public (closes the D-0016 disposition).
3. **OPEN-D1:** the `Recomandare: <option-token>` marker contract is ratified now and exercised by the re-authored control-plane templates; architect role prompts adopt it at Etapa 5.
4. **OPEN-D2 resolved — abort on bind failure stands:** the dashboard is tailnet-only; a degraded localhost bind would serve a page the founder cannot reach anyway. Abort is the honest failure.
5. **OPEN-D3 resolved:** `decision_session` routes to fable (rare, turn-bounded, tools-off; founder-facing trade-off reasoning warrants the strongest model).
6. **Config keys ratified as proposed** (design §6 list); the values land in `factory.config.yaml` inside the D1 build delta, atomically with `config.py` (golden test stays green at every commit).

Build: single builder D1 with enumerated deltas only + non-executor verification including a live founder-protocol conformance pass, per D-0008.

## D-0018 — 2026-06-11 — main architect — CCR-4: dashboard build lane extension approved

D1 built the full dashboard slice (uncommitted, 631 tests green incl. live Romanian-conformance render; claude tools-off flagset verified against the installed CLI: `--tools ""`) and STOPped exactly at its lane boundary instead of touching unowned files. Approved extensions: (1) `tests/unit/test_cli.py` — FakeScheduler gains the CCR-3 `dashboard=` kwarg and the test env binds `127.0.0.1`/ephemeral (unit tests must never bind a real tailnet socket) — unblocking the ~10-line `cli.py` wiring (eager `dashboard.start()` before recover/run_forever; resume passes None); (2) `tests/integration/test_d0014_cmdline.py` — imports migrate to the public `runner.cmdline_matches`, the temporary private alias in runner.py is deleted; (3) ratified: the integration test file is `test_dashboard_integration.py` (pytest prepend-import mode refuses duplicate basenames). Finisher + full non-executor verification + commit follow.

## D-0019 — 2026-06-11 — main architect — Transcript-race disposition (dashboard slice verifier, reproduced)

The slice verifier **reproduced** a race on the single write path: a Decision-Session agent turn appending to the transcript file inside `answer()`'s `await commit_paths()` window makes the registered sha256 match post-append bytes while `git_commit` pins the pre-append blob — `verify_integrity` then fails the non-terminal ref and **aborts the next orchestrator start** after a founder-normal action (tapping an option while the agent is composing). Disposition: **quiesce-the-session** (design §3.1a/§4 amendments) chosen over register-by-committed-blob — the answer semantically ends the session, and hashing the blob would leave the live file permanently diverged from its registered ref. Required pin test: answer-during-busy-turn → registered transcript resolves at its recorded commit, `verify_integrity` green, `post_message` refused while answering, cancelled-turn notice present in the committed transcript. Plus RO typo fix (`"nu a putut fi citit"`). One scripted fix round was exhausted → dedicated remediation run.

## D-0020 — 2026-06-11 — main architect — Dashboard slice committed; Etapa 4 (founder channel) complete

Slice committed `c051c01` (15 files, +5583/−129; `dashboard.py` 2156 lines + ~2245 test lines): single Romanian page, decision cards rendering the re-authored control-plane request templates, single write path with the D-0019 quiesce (pin tests validated against a neutralized fix — all fail pre-fix), tools-off Decision Sessions, contained supervisor, eager tailnet bind. Full suite **639 green**, ruff clean. Verifier verdict: pass; one minor **deferred** per Doctrine §8: the stage escalation→AWAITING_HUMAN wrapper path does not recreate an absent worktree (fail-explicit, contained at the executor boundary; normal dispatch always has one; operator recovery = restore the worktree; mirror the phase path's recreate only if it ever bites).

Founder channel now functionally complete: ntfy live (topic tested), dashboard committed, watchdog module + disarmed systemd units. Next: first live run (arms the watchdog per D-0016 and stages the DoD §12.A4 phone demo on a real Romanian decision card), real B8 seeded-conflict fixture with live agents, Etapa 5 preparation.

## D-0021 — 2026-06-11 — main architect — First live run COMPLETE (founder-channel mechanism demo)

Synthetic critical-gate demo on the live orchestrator (stub agent routes via `tests/integration/agent_driver.py`; orchestrator, dashboard, ntfy, git, DB, watchdog all REAL). Demonstrated live, twice end-to-end: clean start + recovery; dashboard on the tailnet (founder phone `galaxy-s24-ultra`); critical stage → AWAITING_HUMAN → ntfy push → **founder answered from the phone** (decisions #1–#4, all `approved`, `via=dashboard`); Tier-1 + Tier-2 merge gates; phase sign-off → DONE; answer artifacts + session transcripts auto-committed (`0ee78c5`…`c4e7239`); watchdog armed via systemd timer, healthy, zero false pages across a disarm→stop→restart→re-arm cycle.

**Incident, handled by design:** the founder's first Decision-Session turn landed exactly in an Anthropic subscription-billing lapse — claude returned 403 "organization has disabled subscription access"; the turn failed explicitly (RO failed-turn notice in the transcript, no blind retry, answer buttons unaffected, transcript-with-failure committed with the answer). After the founder paid, round 2 succeeded: real fable turn, canon-injected, tools-off, founder-protocol Romanian — the session agent itself articulated the mechanical-`Recomandare` semantics and its own no-write boundary.

**Scope note:** this proves the §12.A4 *mechanism*; the formal A-criteria run on real ERP stages (Etapa 5). Ops learnings in `docs/runbooks/first-live-run.md` (tmux-dies-with-command; disarm-before-stop; seed-only-while-stopped).

**needs_architect backlog** (from the live-run investigation; deferred per Doctrine §8, each with its deciding trigger): (1) no operator command to create a phase — manual DB insert is the only entry (trigger: Etapa 5 intake defines the sanctioned path); (2) `notify.dashboard_link` hardcodes `gethostname()` (trigger: first dead-link report — the phone resolved `server-e9` fine); (3) no SIGTERM graceful shutdown in `cli run` (trigger: before a systemd-managed orchestrator); (4) `cli resume` runs dashboard-less (trigger: first production resume); (5) watchdog unit hardcodes the config path (trigger: configs multiply); (6) OPEN-2 production `test_command` still null (existing trigger: before the first real ERP BUILD stage).

## D-0022 — 2026-06-11 — main architect — Criterion B8 PASSED live; three real defects flushed by the run

The §5.3 seeded-conflict scenario ran on the live pipeline with the REAL cross-model Integration Validator (codex). Stage `catalog-a` merged clean — the conditional invariant was vacuously satisfied and the validator correctly stayed silent. Stage `totals-b` (built on the pre-A assumption; own tests green; Tier-1 green) was **caught at its merge gate**: finding `SUM-1`, severity blocker, citing concrete locations and naming the sibling merge ("suma … este 1500 după merge-ul catalog-a") — possible only through the §3.1 sibling-diff Tier-2 input contract (the design-review critical fix earning its keep). Resolution loop completed: executor complied → rework BUILD → fix → clean re-gate → merged. Fixture preserved per DoD §5.3: `tests/fixtures/b8-seeded-conflict/`.

The run flushed three real defects before succeeding:
1. **codex adapter had no write access** (default read-only sandbox) → `--sandbox workspace-write` (`470f4ad`); the validator had analyzed correctly and documented the write refusal honestly — model behavior exemplary, adapter at fault.
2. **Validator-isolation assertion tripped on the factory's own test-run bytecode droppings** (`__pycache__/`) → `process.isolation_ignore_globs` (`c50bf37`).
3. **Multi-project DBs unsupported**: recovery cannot map `repo='workspace'` refs when phases span projects → backlog, trigger = before two projects share one DB (fresh-DB-per-project is the MVP posture).

Scenario re-derivation (Doctrine §6/§11): fixture v1's violation was not joint-only (stage B violated alone) and a kind-keyed playbook derails on unplanned reworks caused by legitimate real-validator variance — v2 uses a conditional invariant (vacuous at A, broken only by the pair) and a minimal-surface stage A. Validator-quality bonus: on v1, codex unprompted flagged Python banker's rounding in `to_bani` as monetarily nondeterministic — a correct finding, adopted.

DoD §12.B status: **B7 ✓** (integration suite), **B8 ✓ live**, **B9** fallback ✓ (integration suite); the "real ambiguous case" half lands naturally at the first real CP-1 consultation in Etapa 5. A-criteria + C10: Etapa 5 (real ERP stages).

## D-0023 — 2026-06-11 — main architect — Bonus phase-level catch; B8 demo retired

After the stage-level B8 pass, the **phase-level** Tier-2 gate (same level-agnostic code path, DoD §3.2) produced a second, independent catch: `PLAN-ASSUMPTION-1` (medium) — the phase plan narrated the seeded trap ("B adds totals.json on the old assumption…") while the delivered state was already compliant; a genuine plan-vs-delivered divergence, correctly escalated to `main_architect` per §5.2 Resolve. Architect disposition: the finding is meta-to-the-fixture (the plan should never narrate seeding mechanics) — fixture `plan_md` rewritten in-world; lesson recorded in the fixture README. Demo environment retired: orchestrator stopped cleanly, watchdog disarmed (re-armed at the next long-running `cli run`), demo DBs archived under `.factory/archive/`. The escalated demo phase is intentionally left unresolved in the archived DB — its purpose is fulfilled.

## D-0024 — 2026-06-12 — main architect — Phase-seeding design v1.1 ratified (CCR-5); ERP intake package drafted; Etapa-5 readiness build launched

The sanctioned phase-creation path (D-0021 backlog item 1, trigger fired at Etapa-5 intake preparation) is designed, adversarially reviewed, and ratified: `docs/design/phase-seeding-design.md` v1.1 — `cli seed-phases` (validated `MacroPlan` → phases + phase DAG + factory-level `macro_plan` artifact ref + `phase_seeded` events, one transaction, claim-free flock guard, deep workspace/committed-plan preconditions), workspace bootstrap as a runbook procedure verified mechanically at seed time, Phase-Architect project-context prompt block (+ intra-phase contract namespace `_factory/contracts/phase-<id>/`), claude print-mode `--permission-mode bypassPermissions` for tools-on agents WITH a mechanical out-of-bounds detector (gate/recover `git status` on factory repo + workspace, alert + ntfy), and the `proving_phases` dispatch hold (the config key finally changes behavior — Doctrine §17). Review: two independent reviewers (conformance/feasibility; failure-modes/ops), both approve_with_fixes, 19 findings (1 critical — per-phase artifact refs impossible against `UNIQUE(repo,path,sha256)`, re-derived to one factory-level ref), all applied, 0 rejected.

**CCR-5** (control-plane §4 additive amendments, design v1.4→v1.5 annotation): `artifacts.MacroPhase/MacroPlan/read_macro_plan`, `db.list_dag_edges`, cli subcommand `seed-phases` + `_InstanceLock.acquire(claim=False)`, `config.ProjectCfg.project_md`, artifact kind `macro_plan` (unit_level `factory`), event `phase_seeded`, ClaudeAdapter bypass flag, scheduler proving-hold + out-of-bounds detector. Full contract: phase-seeding design §6.

**ERP intake package drafted** under `docs/projects/erp/` (PROJECT.md, macro-plan.json — 7 phases, 3 parallelizable after foundation; cross-phase contract drafts c1–c4; empty D-ERP macro decision log): reviewed by two further adversarial reviewers (business-docs conformance against ERP-start@51e32b0; pipeline fitness against DoD §12), both approve_with_fixes, 20 findings, all applied — notably: cont/ZN/ZN-line skeleton interface added to C3 (the missing seam all three parallel phases read), ZN-line split single-owned by service-orders with reservation-redistribution as event E10, painter-debt E9, outsource-confirmation E12, readiness auto-update E11, income-tax provision placed in treasury-payments, print/PDF + media + notification subsystems moved into foundation, contracts canonical home = workspace `_factory/contracts/` from bootstrap (factory-repo dir becomes a pointer), foundation-exit contract freeze executed by Main Architect at sign-off (never by the phase). **Package status: DRAFT — binding only at the intake interview (D-ERP-0001).** Build of the code deltas launched per design §9 (single builder, enumerated files, non-executor verification).
