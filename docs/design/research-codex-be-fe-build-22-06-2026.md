# Research — Routing [BE]→codex / [FE]→opus in the SF-F5 factory

**Produced by:** an ARH-02 research subagent, 22-06-2026, on the founder's idea (point 4 of the 22-06
pipeline-review thread, **NOT yet a decision**): give BACKEND build stages to the codex CLI (gpt-5.5)
and FRONTEND build stages to opus. Read-only investigation. Founder-facing synthesis is in the chat thread.

## PART 1 — Codex CLI cheatsheet (installed `codex-cli 0.139.0`, ChatGPT-subscription auth)
- **Non-interactive:** `codex exec [PROMPT]` (prompt positional or piped stdin). The factory already uses
  this for `auditor_cross_model`.
- **Model:** `-m gpt-5.5` (current default/strongest agentic coder). Also gpt-5.4 / 5.4-mini / 5.3-codex-spark.
- **Effort:** `-c model_reasoning_effort="xhigh"` (config override, not a flag). Levels none/low/medium/high/xhigh.
  **`xhigh` is codex's ceiling** (`max` is Claude-only) — already enforced by config.py:435-456.
- **Writes (unattended):** `--sandbox workspace-write` (worktree-scoped; excludes .git/outside-root/network);
  `danger-full-access` only in an isolated container. `--ask-for-approval never` for no-human runs. `--add-dir`
  to widen write scope.
- **Resume:** `codex exec resume <SESSION_ID>` / `--last` EXISTS at 0.139.0.
- **Output:** `--json` (JSONL events incl. file-changes + `turn.completed.usage`); `--output-schema FILE`
  (structured final message — NOT used by the factory); `-o FILE` (final message to file).
- **Cost:** codex reports **tokens only, no `cost_usd`**. gpt-5.5 API list = **input $5 / output $30 per Mtok**
  (+ long-context ×2/×1.5 above 272K input). Context 1.05M API / ~400K on the subscription product.
- **Auth:** docs prefer API keys for automation; this box uses ChatGPT-account auth (the rate-limited path),
  and **codex sits OUTSIDE the factory's Claude capacity-governor drain** (factory.config.yaml:119) — a codex
  rate-limit is handled differently than a Claude one.

## PART 2 — Cardinal factory changes (each cited)
**Core finding:** builder selection keys on **risk class only** (`scheduler.py:592-605` `_builder_role`),
and the `[BE]`/`[FE]` markers live ONLY in the human draft `erp-rebuild-plan-DRAFT.md` (line 89 notation) —
**not** in the machine `phase-plan.json`. The factory has no path from that markdown to a routing decision.

1. **Thread a BE/FE field end-to-end** (REQUIRED, moderate): new migration `0005` (ALTER stages),
   `PhasePlanStage` field (it is `extra="forbid"` — artifacts.py:178-196 — so plans reject unknown keys
   today), `Stage` dataclass (models.py:394), insert/read paths (db.py:277-294, 240-252; ingest scheduler.py:4274),
   AND the plan-authoring agent must EMIT the field.
2. **2-D routing** (REQUIRED, small): new roles `builder_backend`(codex)/`builder_frontend`(opus); rewrite
   `_builder_role` to a BE/FE × risk matrix (the axes are orthogonal). Call sites already pass `stage`.
3. **Codex builder writes** (mostly done): the codex path already writes via `--sandbox workspace-write`
   (runner.py:310-351, added because read-only "refused the report writes"). Residual: `tools:none`
   unsupported for codex (must be all); no `--add-dir`; runner doesn't read back files codex writes.
4. **Artifact capture** (low-moderate): runner captures only codex's final stdout message (runner.py:379-384),
   not files it writes — a builder owing a structured sidecar may need `-o`/`--output-schema` wiring.
5. **Ledger/cost** (low): codex = tokens only, no cost; config gpt-5.5 price ($1.25/$10) is an estimate
   "pending invoice" and **well under list ($5/$30)** — reconcile before trusting codex cost figures.
6. **RESUME — codex is NO-RESUME today** (HIGH significance): `RESUME_VERIFIED_CLIS={claude,stub}`
   (scheduler.py:119-125, OPEN-3). CP-1 `continue_session` downgrades to a full **cold rebuild** for codex
   (scheduler.py:2495-2549). The runner CAN emit `codex exec resume` (runner.py:329-330) — only the policy
   gate blocks it. So every backend fix-loop iteration restarts cold across `max_fix_iterations:3`, on exactly
   the heaviest (backend/NEW-code) stages. Lifting = verify `codex exec resume` in code + add `"codex"` to the frozenset.
7. **MODEL-DIVERSITY-AT-AUDIT COLLAPSE** (HIGH significance, design hazard): `auditor_cross_model` is ALREADY
   codex/gpt-5.5 (factory.config.yaml:49). If BE stages are BUILT by codex AND cross-audited by codex, the
   "cross-model" auditor becomes the SAME family as the builder — the diversity the framework was designed
   around (D-0003) collapses on every BE structural/critical stage. **Fix:** re-pair BE-stage audits (make
   opus the independent/cross auditor for codex-built stages).

## PART 3 — Feasibility
**FEASIBLE but NOT config-only** — a small feature (schema field + 2-D routing) + two genuine design hazards.
Recommend a **guarded pilot, not a blanket rollout**. The real motivation is **capacity** (offload backend
onto the ChatGPT/codex limit window so the Claude 5h/weekly window isn't exhausted — more parallelism), NOT
cost (gpt-5.5 ≈ opus, even slightly above, at real prices).

**Biggest risks (priority):** (1) model-diversity-at-audit collapse (Change 7) — must re-pair BE audits;
(2) unattended file-write reliability at builder scale (Changes 3-4) — pilot first; (3) no warm resume →
cold rebuild every BE fix loop (Change 6).

**Suggested sequencing IF the founder proceeds:** (a) re-pair BE-stage audits to keep opus independent;
(b) add BE/FE field + 2-D routing; (c) verify `codex exec resume` + add to RESUME_VERIFIED_CLIS; (d) pilot
on 2-3 real BE stages watching write-reliability, fix-loop burn, escalation frequency — before any blanket switch.

**Key files:** scheduler.py (592, 119-125, 2495-2549, 4274-4329), runner.py (301-396), models.py (394-408),
db.py (240-252, 277-294), artifacts.py (178-196), config.py (435-456), migrations/0001_init.sql (19-31),
factory.config.yaml (48-49, 108-111, 146-150), erp-rebuild-plan-DRAFT.md (89 + BE/FE rows), decision-log.md (19-21, OPEN-3 at 62).
