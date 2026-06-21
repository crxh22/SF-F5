# Session handoff — ETAPA-5u → ETAPA-5v, 21-06-2026

**For ETAPA-5v (Main-Architect successor).** POINTER doc (Doctrine §9). 5u hit the context guard (~573k).
Launch: opus, effort max, RC ON named ETAPA-5v, via `claude_canon.sh`.

**FIRST duties (in order):** (1) write your session_id into `~/.claude/sf-architect-session` (replace — holds 5u's `b99f4fec-8228-4932-a904-664648cd445e`). Find your id = the newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/` containing your launch prompt. (2) Update `~/.claude/sf-architect-monitor.sh` header (5u→5v) + relaunch via Bash `run_in_background:true`. (3) Stay lean.

## ⚠️ CRITICAL CORRECTION — the 04:25 incident was NOT an OOM
5u investigated it fully (founder JOB 1). **It was NOT memory/OOM** (overturns the original handoff's premise). Evidence: zero kernel OOM on 21-06 (journalctl -k — you're in group `adm`, no sudo needed); the 3 dead scopes deactivated NORMALLY (no `oom-kill` result, unlike the real 19-06 OOM); session 5t was 88s old / tiny when it died (a fresh process can't OOM). Conclusion: a **Claude-side / Remote-Control event terminated the 3 ACTIVELY-working sessions** at once; the 2 idle ones survived. Full report: `docs/incident-cadere-arhitecti-0425-21-06-2026.md` (sent to founder). **Do NOT re-investigate or fear OOM.** OPEN (founder NOT yet confirmed): the durable fix = (1) auto-restart architect sessions on death [primary], (2) finish the half-wired "Scut" [defense-in-depth]. Founder owes a "da, fă 1+2".

## ⚠️ IMMEDIATE JOB — execute the depozit foundation fix (founder CONFIRMED the direction)
**Why:** `stocktaking` loops forever at its merge gate on Tier-2 finding **INT-ST-DEPOZIT-KEY-PK-001** (LOW): it keys the `depozit` rights dimension on the Warehouse **PK**, violating the F5 slug convention. Root: `nomenclature.Warehouse` (created in `foundation.config-registry`) was the ONLY nomenclature catalog table used as a rights dimension that was given **no `code`** (its siblings Currency/Bank/etc. all have one). The factory has NO mechanism to accept a Tier-2 finding; the root must be fixed. Founder CONFIRMED the root fix (add the code), NOT a convention carve-out.

**The fix spec is WRITTEN + AUDITED:** `docs/spec-draft-depozit-code-21-06-2026.md` — audited by a clean-context subagent that verified against the actual code (root confirmed, cascade independently confirmed contained to Warehouse — all 12 dimensions checked, migration safe, no hidden consumers). One factual error + gaps were found and FIXED. The spec is sound. READ IT.

**Execution decision (5u's call): FOLD the fix into stocktaking's rework** (NOT a new stage — adding a stage mid-phase needs risky direct DB/replan surgery; the rework path is the factory's proven, safe mechanism, and stocktaking needs a keying-switch rework anyway). Exact steps:

1. **Raise the critical budget** (stocktaking is at ~456M; `budget.critical` runtime override is currently 500M; a rework cycle adds ~50-100M). Run:
   ```bash
   sqlite3 /home/artur/projects/SF-F5/.factory/factory.db "PRAGMA busy_timeout=8000; UPDATE runtime_settings SET value='650000000', updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'), updated_by='ETAPA-5v' WHERE key='budget.critical';"
   ```
   The orchestrator reads runtime_settings LIVE. **Revert to 500M after stocktaking is DONE** (founder's "pentru etapa asta" pattern, D-0058).
2. **Resolve escalation #99** (stocktaking, context_budget, currently target main_architect) with **`rework:BUILD`** and a COMPLETE, self-contained `--reason` (the builder works in the erp-workspace worktree and CANNOT read SF-F5/docs, so the authorization must be inline). The --reason must say: *AUTHORIZED foundation amendment for INT-ST-DEPOZIT-KEY-PK-001 — do NOT contest again. Do ALL of: (1) add `code = models.CharField(max_length=100, unique=True)` to nomenclature.Warehouse (catalog.py, mirror siblings); (2) migration nomenclature/0003: AddField code null=True → RunPython backfill (slugify(name); `-<pk>` on collision/empty; reversible) → AlterField null=False unique=True; (3) default a unique sequence-style code in the warehouse() test factory (registers/tests/factories.py); (4) add `"warehouse": {"code": {"unique": True}}` to nomenclature/tests/test_tables.py FIELD_SPECS; (5) switch THIS stage's depozit keying pk→code in inventory/api.py (`_depozit_key_from_query/_from_payload/_from_document` return Warehouse.code; `filter_by_rights(Warehouse, ("depozit","pk"))` → `("depozit","code")`) + update depozit-rights tests to grant on codes; add tests: depozit keyable on code + FKs to Warehouse still resolve; full scripts/test.sh green. Pure key-hygiene, no business logic. Full spec: docs/spec-draft-depozit-code-21-06-2026.md.*
3. **Monitor the build** (the stocktaking watcher script `/tmp/sf-stocktaking-watch.sh` exists; re-arm it via Bash run_in_background). Stocktaking: BUILD → VALIDATE → dual-AUDIT → critical human gate (AWAITING_HUMAN → **approve per delegation**, val+audit clean) → MERGE_GATE. Tier-2 should now find NO depozit-PK contradiction (stocktaking keys on code). **If Tier-2 flags the cross-phase nomenclature edit as a NEW finding → `settled` with the architect-authorization rationale** (it's sanctioned). If it LOOPS on the SAME contradiction → the keying switch didn't land; re-check the builder's diff, do NOT just re-settle (architect-operations §1).

## ⚠️ DOWNSTREAM (after stocktaking DONE) — the stock-views SPLIT (founder approved)
Founder approved: **split stock-views into 2 separate stages: backend + UI**, both **structural** (→ Opus builder + dual auditors), built FRESH on current main (its old worktree doom-looped on rebase conflicts, 88M tokens wasted; #97 parks it).
- **Boundary** (founder confirmed): backend = spec §5 (the 11 read endpoints `apps/inventory/stock_views.py` + `stock_rights.py` + procurement extensions + rights boundary + tests §7.1-7.4); UI = spec §6 (`frontend/src/features/stock/` «Stoc» + typed client + tests §7.5). UI depends on backend. Old spec: erp-workspace worktree `_factory/stages/inventory-procurement.stock-views/spec.md`.
- **CARRY FORWARD the own_pj business clarification** (founder's research request — 5u found it): the new backend spec MUST handle **la-negru (own_pj=NULL) vs official (own_pj set) stock as DISTINGUISHABLE** in the views (V1 + all balance views). The current stock-views code did NOT carry the `own_pj_isnull` selector. Founder leaning this is THE clarification (own_pj). Secondary: mixed-VAT returns rule (V6 should show VAT regime) — already handled in the returns stage.
- New stock-views keys `depozit` on **code** (the fix provides it).
- Old #97 (stock-views context_budget, target founder) is resolved by superseding it with the split (cancel/replace the old stock-views — see how D-0053 split stock-core: CANCEL + new PENDING stages; verify FK refs first).

## OPEN founder decisions (all presented, awaiting his "da"):
1. **Incident 04:25 durable fix** (auto-restart [1] + Scut completion [2]) — not confirmed.
2. **stock-views business clarification** — own_pj confirmed-leaning; bake into backend spec regardless.
3. **stocktaking fix** — CONFIRMED, you are executing it.
Founder gives decisions in CHAT, you APPLY them (`[[founder-applies-approvals-via-architect]]`); he auto-delegated approval of any val+audit-passed stage, ALL risk classes.

## STATE SNAPSHOT (verify, don't trust stale)
- Orchestrator pid in `.factory/orchestrator.pid`, under deploy/sf-cap.sh, schema 4. Factory IDLE/parked (nothing burning).
- Stages non-DONE: stocktaking ESCALATED (#99 — you resolve via rework above), stock-views ESCALATED (#97 — superseded by the split), phase-integration PENDING (waits on both).
- Monitor exit codes 10/11/12/13/14 (you'll see churn as you resolve things — relaunch after each). Budget mechanism = runtime_settings table (live). sf-limit.sh for the 5h limit.

## WORKING-MODE / SUCCESSION
- Romanian to founder, plain, his terms, DD-MM-YYYY, copy-paste commands, NEVER AskUserQuestion, no bare IDs. Verify schema before DB queries. `ruff check` (not format) before commit.
- Succession: `docs/runbooks/session-succession.md`. Verify the successor's RC on the founder's phone before going silent.
