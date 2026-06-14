# Design — finding-regeneration slice 2: the `settled` (no-action) disposition + triage write-isolation

**Status:** DESIGNED (14-06-2026, ETAPA-5c), code-grounded via a Plan agent. NOT yet built. Build in 2 verified units (A then B), batch into the next consolidated deploy. Supersedes the loose "4th triage verb" framing in the D-0040 handoff — `architect-operations.md §1` (no-action is the architect's call) governs.

## Root being fixed
Accurate audit findings REGENERATE every round (CE-AUDIT-1 ×3, AA-A2 ×2+): the auditor runs in CLEAN context each round and `_audit_prompt` (scheduler.py ~3270) feeds it NO memory of prior adjudications, so it re-raises; the triage vocab {comply,contest,duplicate} has no "accurate·acknowledged·no-action·closed" disposition.

## Resolved decisions (decisive)
- **`settled` status**, applied by the **phase_architect** when resolving an `unresolved_contest` (NOT an executor triage verb — the executor must not self-close an accurate finding). The word matches `architect-operations.md:29`.
- **Do-not-re-raise set = `{settled, overruled}` ONLY.** NOT sustained/complied (those may be genuinely unfixed → must stay re-raisable — suppressing them would mask real bugs), NOT duplicate (round-local). This is the single most safety-critical line; **pin it by test**.
- **Auto-close backstop at triage = REUSE `duplicate`** (no 4th verb). If a clean-context auditor re-raises a settled/overruled observation despite the prompt memory, the executor answers `duplicate` → local close, no escalation (scheduler.py:2344-2347,2403-2406). Avoids a CHECK/contract/prompt change for a path that should rarely fire (Doctrine §8/§0).
- **`settled` routing wrinkle** (after settling, forward state is risk-dependent — MERGE_GATE non-critical / AWAITING_HUMAN critical — but `STAGE_ESCALATION_RESOLUTIONS` maps a token→ONE state): **chosen = `settled` is a first-class resolution token, SPECIAL-CASED in `_step_escalated` BEFORE the static-map lookup**, that marks the contested findings `settled` then delegates to the existing `_leave_clean_audit(stage, worktree)` for the risk-routed forward transition. Keep `settled` OUT of `STAGE_ESCALATION_RESOLUTIONS` (preserve the one-token-one-state pinned invariant); expose it via a `models.STAGE_NOACTION_RESOLUTION = "settled"` constant shared by the CLI + scheduler. The ESCALATED→MERGE_GATE and ESCALATED→AWAITING_HUMAN edges already exist (models.py:148-158) — **no transition-table change**.

## Per-file changes
- **`migrations/0002_settled_finding_disposition.sql` (NEW):** widen `audit_findings.status` CHECK to add `'settled'` via the SQLite table-rebuild idiom (CREATE _new with the widened CHECK byte-identical else to 0001:98-109 → `INSERT … SELECT *` → DROP old → RENAME → recreate `idx_findings_stage`). FK-safe by topology (nothing references audit_findings — grep-confirmed). DO NOT add `PRAGMA foreign_keys=OFF` (no-op inside the per-migration `BEGIN IMMEDIATE`, db.py:120; documented trap at 0001:1-2). One tx, whole-file rollback on failure (test_db.py:184).
- **`models.py`:** add `STAGE_NOACTION_RESOLUTION = "settled"` (NOT a `STAGE_ESCALATION_RESOLUTIONS` key). Pin: it is NOT a map key.
- **`scheduler.py` `_step_escalated` (2614-2755):** recognize `last.resolution == STAGE_NOACTION_RESOLUTION` BEFORE the static-map lookup (so it's not "unknown resolution → alert"); still archive sentinels (2641-2642); settle the open escalation's `("contested",)` findings → `set_finding_status(…, "settled", resolved_by="phase_architect")` (mirror 2685-2689); then `return await self._leave_clean_audit(stage, self._worktree(stage))`. Settle write committed before/with the routing; return early (skip the target-based block).
- **`scheduler.py` `_audit_prompt` (3270-3279):** query `fdb.findings(self._db.read(), stage.id, ("settled","overruled"))`; if non-empty append a "PREVIOUSLY ADJUDICATED — do NOT re-raise unless the implementation MATERIALLY CHANGED into a genuinely new defect" block listing `finding_ref`+severity+auditor_role (refs only — `Finding` has no summary; no schema change). Cap the list (~30 most recent by id; bounded like 1596).
- **`scheduler.py` `_respond_prompt` (3281-3296):** append (i) WRITE-BOUNDARY: "Respond ONLY by writing findings-response.json. Do NOT edit code/any other file — rework happens in the BUILD step after a comply. Never git commit." (the [20] fix half); (ii) "If a finding restates an observation already permanently closed in a prior round, answer `duplicate`."
- **`scheduler.py` `_step_audit` (2223-2406) — the [20] mechanical fix:** the response sidecar is already committed (2320-2322); **immediately after that commit, UNCONDITIONALLY call `await self._discard_uncommitted(worktree)`** (the stage worktree, 2227) to drop stray uncommitted source edits, recording the discarded entries into the transition payload (forensics, mirror 1523). Placement: after the response commit, before computing contested/complied (2323) — strays are corpse output in every triage outcome; discarding always (response already committed) is the simplest mechanical guarantee and matches the FAILED-path. Do NOT weaken `_assert_no_unregistered_files` (1921-1942) — it stays as the §3.1 belt-and-suspenders.
- **`cli.py` `cmd_resolve_escalation` (1254-1320):** accept `settled` for STAGE level only (`set(STAGE_ESCALATION_RESOLUTIONS) | {STAGE_NOACTION_RESOLUTION}`); list it in the error; reject for phase level. `resolve_escalation` (db.py:839) stores the string; `escalations.resolution` has no CHECK (0001:94 is a comment) — no escalation-table migration.

## Decomposition (2 independently-verifiable units, build A→B)
- **Unit A — `settled` disposition + audit memory** (root fix): migration 0002, models constant, `_step_escalated` settled branch, `_audit_prompt` memory, `_respond_prompt` duplicate clause, cli acceptance. Verifiable: `resolve-escalation <id> settled` → findings flip `settled`, stage routes MERGE_GATE/AWAITING_HUMAN by risk, next `_audit_prompt` carries the do-not-re-raise block.
- **Unit B — triage write-isolation [20] fix** (mechanical hygiene): `_respond_prompt` write-boundary, the unconditional `_discard_uncommitted` after the response commit, payload evidence. Verifiable: a triage agent scribbling stray source no longer wedges comply→BUILD; strays discarded with evidence; §3.1 still fires for genuine pre-existing leaks.
- **A first** (carries the migration/schema foundation; both touch `_respond_prompt` so A's content edit lands before B's hygiene edit — avoids re-touching, §6). Can be 2 commits on one branch; the split is for verification.

## Key tests
- 0002 widens CHECK (settled inserts ok, invalid still raises); preserves rows + index; whole-file rollback (generic).
- `_step_escalated`: `settled` on a routine stage → MERGE_GATE + findings `settled`/resolved_by phase_architect; on a critical stage → AWAITING_HUMAN + pending decision request.
- `_audit_prompt` lists settled+overruled ONLY (NOT sustained/complied/duplicate) — **the safety pin**.
- `_respond_prompt` forbids code edits (mutation: without the discard, a stray-writing comply scenario fails with §3.1 IntegrityError — proves Unit B).
- cli accepts `settled` for stage, rejects for phase.
- Regression: `test_build_asserts_validator_isolation`, `test_build_isolation_ignores_build_test_droppings` stay green (§3.1 NOT weakened).

## Risks / foot-guns
1. `_discard_uncommitted` MUST run AFTER the response commit (else it deletes findings-response.json) and ONLY in the stage worktree (it's reset --hard + clean -fd — destructive; D-0035 incident-7 invariant).
2. The suppress set `{settled, overruled}` is safety-critical — widening it to sustained/complied would silently mask unfixed bugs. Pin it.
3. Prompt memory is advisory (Doctrine §20) — the `duplicate` backstop + the slice-3 recurrence flag are the real guarantees; don't over-trust the prompt.
4. `settled` deliberately bypasses the static resolution map (a reviewer will be surprised) — the constant + comment + the "NOT a map key" pin document why (mirrors how rework:MERGE_GATE needed special handling, D-0042).
5. `settled` is stage-only (phase escalations have no contested findings).

## Critical files
`src/sf_factory/scheduler.py` (_step_audit 2223-2406, _step_escalated 2614-2755, _audit_prompt 3270, _respond_prompt 3281, _discard_uncommitted 1576, _assert_no_unregistered_files 1921); `src/sf_factory/migrations/0002_settled_finding_disposition.sql` (NEW); `src/sf_factory/models.py` (STAGE_NOACTION_RESOLUTION); `src/sf_factory/cli.py` (cmd_resolve_escalation 1254-1320); `work-protocols/architect-operations.md` §1 (the authoritative `settled` semantics).
