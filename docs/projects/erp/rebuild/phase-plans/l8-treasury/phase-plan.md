# L8 — Treasury & payments OPERATIONS (phase-plan)

**Phase id:** `l8-treasury` · **Macro position:** after `l7-service-orders`, before `l9-config`
(linear macro DAG). Money OPERATIONS — deliberately LATE per the methodology (money nomenclatures
sit at the BASE in L1/L2; money operations ride the top).

**Source material:** DRAFT LAYER 8 + the in-flight backend branch
`stage/treasury-payments.treasury-app-foundations` (`treasury` app — 15 files, backend-only, NO
`api.py`), RE-SLOTTED here. Branch verified read-only: abstract `Payment(DocumentBacked)` base,
`currency.py` (current_rate / set_exchange_rate / mdl_equivalent), `conformity.py`
(channel_fiscal_side + raise_conformity_ticket_if_mismatch), `ConformityTicket` ledger. Allocation
seam verified against `stage/service-orders.cont-quote-core:so_quotes/payment_state.py`.

---

## Stages

| id | kind | role | risk | one-line |
|---|---|---|---|---|
| `treasury-found` | backend | **contract** | structural | RE-SLOT: land the treasury skeleton + abstract `Payment` base + currency resolver + conformity helper + `ConformityTicket`; freeze the Payment seam; re-verify on real L1/L2 nomenclatures; inherit L2's single canonical rate writer. |
| `payment-producers` | backend | leaf | **critical** | NEW: four concrete payment types on the base; R5/R6/R8 postings (producers only — allocation is the sibling stage). |
| `payment-allocation` | backend | leaf | **critical** | NEW: allocate/advance vs ACCEPTED Conts posting NEGATIVE R6 rows (`cont_reference`=Cont.id) → the existing `payment_state.py` seam derives state; conformity ticket at recording. Descends from `payment-producers`. |
| `payment-fe` | frontend | leaf | structural | NEW: take-a-payment (channel→currency derived, rate prefill+override, live MDL equivalent), allocate-to-cont (shows derived state change), conformity surfacing, L5 lifecycle controls. Founder visual gate = L8 capstone. |

## Contract stage + why

`treasury-found` is `role:contract`. The frozen seam is the **abstract `Payment` DocumentBacked base**
(money-location XOR; amount+rate stored; currency/direction/own_pj/fiscal_side/amount_mdl derived) plus
the currency resolver + conformity helper. The three leaves all descend from it: `payment-producers`
subclasses the base; `payment-allocation` allocates those documents (and reuses the resolver/conformity);
`payment-fe` renders what they expose. It is a backend stage that establishes the structural seam (not an
endpoint — backend-only, no `api.py` this stage), exactly the contract-first pattern. Floor-exempt as a
contract, but it carries full re-verify acceptance (7 AC / 6 touched).

## Intra-layer DAG (linear chain — contract is the root)

```
treasury-found ──▶ payment-producers ──▶ payment-allocation ──▶ payment-fe
```

Contract-first reachability satisfied: every leaf is a DAG descendant of the contract
(`treasury-found`). Degrees: treasury-found out=1 (deg 1); payment-producers in=1/out=1 (deg 2);
payment-allocation in=1/out=1 (deg 2); payment-fe in=1 (deg 1). All ≤6. The chain is necessarily linear —
producers cannot post until the abstract base is frozen; allocation cannot post NEGATIVE R6 rows until the
producers exist; the FE cannot render until allocation drives the Cont-state seam.

**Producers/allocation split (at planning — the only split point):** the bundled `payment-producers`
stage carried BOTH the four doc types (+ their R5/R6/R8 postings) AND the allocation/advance path (+
conformity), exceeding one builder pass. It is peeled into two **critical** BE leaves: `payment-producers`
(the four types + postings) and `payment-allocation` (allocate/advance vs ACCEPTED Conts, NEGATIVE R6,
conformity at recording), the latter a DAG descendant of the former. Both stay `critical` (founder
human_gate) — the money-posting and the Cont-paid-state seam are each a correctness point.

## Founder gate (mechanical, on `payment-fe`)

Record a payment in a foreign-currency channel (MDL equivalent shown) → allocate it against an
ACCEPTED Cont → see the Cont's DERIVED payment state change → trigger an alb/negru mismatch and watch
the conformity ticket appear. Realized as the founder visual review on `payment-fe` (the L8 capstone
frontend stage), reinforced by the `payment-producers` + `payment-allocation` **critical** human_gates
(the architect lifts drain after the founder tests the deployed layer per playbook §5.9).

## Key code anchors (verified, file:concept)

- `treasury/models.py:Payment` — abstract base, derived properties (currency/direction/own_pj/
  fiscal_side/amount_mdl), `cash_desk` XOR `bank_account` check-constraint. **Frozen seam.**
- `treasury/currency.py` — `current_rate` (latest-in-force / MDL=1 / `ExchangeRateMissing` on absence),
  `mdl_equivalent` (amount×rate, 2-dp ROUND_HALF_UP). `set_exchange_rate` is the **duplicate writer**
  reconciled away at L2.
- `treasury/conformity.py` — `channel_fiscal_side` (keyed on `CashDeskType.requires_pj`, not type
  names), `raise_conformity_ticket_if_mismatch` (one non-blocking accounting `Ticket`, idempotent per
  `(payment_document, cont)`).
- `so_quotes/payment_state.py` (L7) — the **allocation seam**: reads R6 payment-type rows
  (`settlement_type` payment/advance_settlement) by loose integer `cont_reference==Cont.id` (NO FK);
  payments post NEGATIVE; `paid` = magnitude clamped ≥0; partitions over `Cont.StatusCommercial.ACCEPTED`.
  → L8.2 must post matching R6 rows; NO new `so_quotes` code needed.

## Deviations from the DRAFT

- **DRAFT 8.2 split into two critical BE leaves** (the only structural deviation, at the planning split
  point). DRAFT 8.1/8.3 map 1:1 to `treasury-found` / `payment-fe`; DRAFT 8.2 (payment producers +
  allocation) is peeled into `payment-producers` (the four doc types + R5/R6/R8 postings) +
  `payment-allocation` (allocate/advance vs ACCEPTED Conts, NEGATIVE R6, conformity at recording) because
  the bundle exceeded one builder pass. `payment-allocation` descends from `payment-producers`; `payment-fe`
  now descends from `payment-allocation`. Short kebab ids replace the `8.x` numbering (factory namespaces
  them to `l8-treasury.<id>`).
- **Made the rate-writer reconciliation a hard, INHERITED constraint** (not a fresh dedup): L2's
  `rate-rest` already collapsed the two writers into the single canonical `set_default_rate`. L8
  must NOT re-introduce a second writer — it inherits the one path and keeps only the read helpers.
- **Both 8.2 halves typed `critical`** (DRAFT implied NEW backend without a class): money posting
  (`payment-producers`) + allocation/Cont-paid-state seam (`payment-allocation`) + irreversible engine
  paths ⇒ critical per authoring-notes §4 (founder human_gate) on each. 8.1 `structural`
  (land+re-verify+freeze, no founder-gated money write yet). 8.3 `structural` (operational FE with the
  founder visual gate; not money-correctness-irreversible itself — it drives the gated 8.2 backend).

## Open questions

1. **Rate-writer reconciliation ownership (cross-layer, L2↔L8).** L2's `rate-rest` is the stage that
   actually collapses `treasury/currency.py:set_exchange_rate` into `nomenclature/exchange_rates.py:
   set_default_rate`. L8's `treasury-found` ASSUMES that reconciliation has landed (L2 DONE precedes L8
   in the macro DAG). If the founder/architect chooses to keep treasury's `set_exchange_rate` as a thin
   delegator vs delete it outright, that decision is made AT L2 and L8 simply inherits it. Flag: confirm
   L2 ships the single-writer outcome before L8 builds, else `treasury-found` must do the dedup itself
   (it is written to tolerate either: "delegates to (or is removed in favour of) it").
2. **Advance vs allocation modelling.** `payment_state.py` already distinguishes `settlement_type`
   `payment` vs `advance_settlement` in R6. L8.2 must post the right type; confirm at spec-time whether
   an advance (no specific Cont yet) posts to a holding key or is always cont-targeted — the seam reads
   per-cont, so an unallocated advance likely posts only the R5/cash side until allocated.
3. **`expense_direction_tag` source for `plata_directa_cheltuieli`.** The base carries the optional
   `Direction` FK (body/service/general). Confirm the FE labels it distinctly from `PaymentCategory.
   direction` (the two-direction caveat, DRAFT Q8) so the founder never conflates them.
