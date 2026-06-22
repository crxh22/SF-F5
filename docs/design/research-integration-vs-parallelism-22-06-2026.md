# Research — Integration cost vs parallelism (SF-F5 factory, own history)

**Produced by:** an ARH-02 research subagent, 22-06-2026, on the founder's question (point 3 of the
22-06 pipeline-review thread): *"vreau paralelizare la maxim, dar dacă integrarea nu e gândită bine,
fură tot timpul economisit și per total cheltuim mai mult — pune un agent să facă research în istorie."*
Read-only analysis of `.factory/factory.db` + `docs/decision-log.md` + `docs/design/control-plane-design.md`.
Verbatim agent report below; the founder-facing synthesis lives in the 22-06 chat thread. Feeds the
pipeline redesign (small-stage gate, contract-first stages) and the re-seed plan.

---

## 1. QUANTIFIED FINDINGS

### 1.1 The integration payload grows monotonically with merge order — the core mechanism, measured
Tier-2 merge-gate payload = the gating unit's full diff + **every sibling diff merged since contract
freeze** (`control-plane-design.md:240`, `:482`). In `foundation`, sibling count per `tier2_gate`
climbs strictly with merge order:

| merge # | stage | siblings in payload | integration_validator tokens_in |
|---|---|---|---|
| 1 | skeleton | 0 | 1.28M |
| 4 | document-engine | 3 | (gpt-5.5 **overflowed**, retry on opus) 5.43M |
| 7 (2nd try) | media-attachments | 5 | **13.33M** |
| 8 | dependency-cascade | 6 | 10.20M |
| 9 | auth-access | 7 | 12.0M |
| 10 | register-schemas | 8 | 12.69M |
| 14 | integration-seed | 13 | 1.62M* |

Early codex/default mergers (#1–3) averaged **1.48M tokens_in**; opus mergers (#4+) averaged **4.70M,
peaking at 13.33M** — ~3.2× average, ~9× peak inflation purely from accumulated siblings. (*Late
foundation stages drop because the hunk-header elision fix landed mid-phase.)

### 1.2 Two documented hard failures where integration cost broke the pipeline
- **4th merger overflowed the cross-model validator window (D-0041).** document-engine passed
  validate+dual-audit+Tier-1, then codex/gpt-5.5 integration_validator ran out of context on the
  gate payload (gating diff + 3 sibling diffs) → permanent reroute **codex→opus** (1M window),
  driven solely by integration-payload size.
- **10th merger overflowed even opus's 1M window (D-0046/D-0047).** posting-engine's Tier-2 prompt =
  **2,361,705 bytes, of which 2,060,628 (87%) were sibling diffs** (9 units) → HTTP 400 "prompt too
  long." status-notifications hit the identical overflow the same day. Fix: `process.tier2_max_total_bytes
  = 1_500_000`; siblings collapse to `@@` hunk headers above it (D-0047, `c97f8db`).

### 1.3 The smaller-stage directive (D-0050) bounded integration cost — the A/B result
| phase | stages | IV runs | IV tokens_in total | avg/run | peak run |
|---|---|---|---|---|---|
| foundation (coarse) | 14 | 22 | **88.1M** | 4.20M | **13.33M** |
| inventory-procurement (finer) | 12+ | 23 | **36.5M** | 1.59M | 3.35M |

Inventory-procurement reviewed a **larger** scope with **58% less** integration-validator token volume
and a **4× lower peak** — the cleanest proof that integration cost is governed by stage granularity.

### 1.4 Where integration cost ate parallelism savings — the magnitudes
Directly-costed integration_validator ≈ 9% of foundation / 6.4% of IP spend — but this **understates**
the true tax (cross-model runs report NULL cost), hidden in:
1. **Re-burns at the gate:** 13 escalations resolved `rework:MERGE_GATE` (8 stages) + 22 `rework:VALIDATE`
   (11 stages). Each re-runs Tier-1 (full suite) + Tier-2 (full sibling payload again) / structural
   validation + dual audit (~45M tokens per the D-0043 estimate).
2. **Full-suite-per-merge:** 82 Tier-1 gate fires across 32 stages → many re-ran the entire suite
   repeatedly. Wall-clock: **263 min total, avg 193s, max 602s**.
3. **The treasury catastrophe:** `treasury-payments.treasury-app-foundations` fired `tier1_gate` **13×,
   every one tests_failed, never passed** → looped ~8h45m, never merged. Wasted **$176.42** on one
   stage. Root = long stage-id overflowing the AF_UNIX 107-byte socket path; the mechanism that turned
   a bug into $176 = an **uncapped merge-gate retry loop**. Motivated the loop-cap (c066103, ARH-01).

**Verdict:** savings eaten by (i) sibling-diff growth, (ii) rework re-burns, (iii) full-suite-per-merge,
(iv) uncapped gate loops.

## 2. ROOT MECHANISMS
1. **Sibling-diff payload grows O(n) with merge order** — Tier-2 must see every sibling merged since
   freeze as full bodies (already-merged diffs vanished into target; hunk headers can't check substance —
   `control-plane-design.md:240,:482`). Intrinsic to the joint-invariant guarantee (DoD §5.3).
2. **Stage size is the multiplier on (1)** — a big stage's diff is re-read by every later sibling.
   foundation's oversized stages produced the 2.06MB payload that overflowed opus.
3. **Late conflict discovery → rebase + full-suite re-runs** (`control-plane-design.md:466,:452`);
   ~2.6 full-suite runs/stage avg; status-notifications 8×, stocktaking 6×.
4. **Contract drift discovered at the gate, not at freeze** — seams not nailed in the frozen contract
   surface late (SN-INT-001 rights-seam 9 merges late D-0047/48; circular device-fixture dep D-0043;
   URL dual-mount D-0062).
5. **Integration-finding regeneration loops** — clean-context re-derivation re-flags settled findings
   without a do-not-re-raise memory (fixed D-0048; same class D-0056, treasury 13×).
6. **Uncapped merge-gate retry = a bug becomes a budget hole** (treasury, until c066103).
7. **Phase INTEGRATING re-reviews everything once more** (foundation phase-gate IV 3.69M, IP 2.84M).

## 3. RANKED IDEAS (impact ÷ cost/risk)
1. **Make smaller-stage a MECHANICAL planning gate** (size predicate at `CONTRACTS_FROZEN→RUNNING`),
   not a prose directive. Highest leverage (the 58%/4× win); also parallelizes better. Low risk (add a
   lower size floor to avoid over-split overhead). [§1.1,§1.3, D-0050/53/55]
2. **Cap the merge-gate retry loop** (shipped c066103) — verify + generalize to all gate re-entries.
   Very low risk; converts worst-case from "$176 sink" to "N laps then human." [§1.4, D-0056/48]
3. **Dependency-aware wave scheduling + explicit contract-first stage** — a thin stage freezing shared
   seams before fan-out, then waves of file-disjoint leaf stages (`control-plane-design.md:643,:657`).
   Attacks contract drift (#4); enables wider parallelism. Medium. [D-0043,47,48,62, decision-log:161]
4. **Scope the Tier-2 sibling payload to the contract surface** (full bodies only for sibling hunks
   touching contract files/symbols + any file two siblings both touched; header-elide the rest). Breaks
   O(n)-bodies growth. **Medium-HIGH risk — trades a slice of the DoD §5.3 joint-invariant guarantee
   (off-contract interactions go invisible); needs an explicit ruling.** [§1.1,1.2, D-0041 names it]
5. **Parallel-safe merge ordering** (smallest-diff / most-depended-on first) — lowers area under the
   sibling-growth curve + rebase conflicts. Low risk (age priority to avoid starvation). [§1.1, D-0049]
6. **Scoped Tier-1 suite per rework lap** (impact-scoped subset on intermediate laps; full suite only at
   the final pre-merge run + phase INTEGRATING). Cuts the 263-min full-suite tax + OOM risk (D-0061).
   **Medium-HIGH risk — a scoped run can miss a cross-module regression; needs strong mitigation.**

### One-line synthesis
Integration cost grew ~9× first→peak merger in foundation and twice broke the validator window — but
the factory's own mid-flight fix (smaller stages, D-0050) cut IV token volume **58% on a bigger phase**.
Recipe: **(1) small stages as a mechanical gate, (2) cap the merge-gate loop [done — verify], (3)
contract-first seam-freezing before fan-out, (4) scope the Tier-2 payload.** (1)–(3) low-risk; (4) and
the scoped-suite (#6) trade a slice of the integration guarantee and need an explicit founder ruling.
