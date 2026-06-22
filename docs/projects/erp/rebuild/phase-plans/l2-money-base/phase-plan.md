# l2-money-base — phase-plan companion

**Layer:** L2 — Own legal entities + money locations + exchange rate (the money base).
**Scope:** OwnPJ (+ its auto-spawned system tax counterparties), CashDesk, BankAccount, DefaultExchangeRate (append-only rate journal). First slice of the NEW `parties/api.py`; the exchange-rate REST + writer reconciliation. CashDesk/BankAccount/rate `clean()` rules mirrored as UI validation.

## Contract seam
`own-pj-rest` (`role:contract`) freezes the first slice of `parties/api.py` (`api/parties/` mount, OwnPJ create/edit/list, soft-delete via PUT, F1 envelope, rights-checked). It is the seam every L2 leaf builds on and the foundation L3/L5 extend. Every other L2 stage is a DAG descendant of it. **Soft-delete target:** OwnPJ has no `active` column in real code, so this stage adds a nullable `active` + reversible migration (deactivate = PUT active=false, never DELETE) to give the standing soft-delete law a target.

## Stages
| id | kind | role | risk | one-line |
|---|---|---|---|---|
| `own-pj-rest` | backend | contract | structural | NEW. parties/api.py slice 1 — OwnPJ CRUD; save() spawns/re-syncs system TVA + Impozit counterparties idempotently; expose them read-only. Adds a nullable `active` column + reversible migration (deactivate = PUT active=false). |
| `money-loc-rest` | backend | leaf | routine | KEEP / **VERIFY-ONLY**. Re-verify `cash_desk`/`bank_account` generic nomenclature CRUD; prove requires_pj/forbids_pj/one-currency clean() fires through the API (not just admin). Defect requiring a code change → ESCALATE for a separate structural fix stage, never patch in place. |
| `rate-rest` | backend | leaf | structural | RECONCILE/EXPOSE (not green-field). Expose append-only create + history-list REST over the EXISTING `set_default_rate`; dedup the treasury duplicate writer to one canonical path for L8. |
| `money-fe` | frontend | leaf | structural | NEW. OwnPJ (read-only spawned counterparties) + CashDesk + BankAccount + set-exchange-rate screens; clean() rules mirrored as UI validation; add-new-inline pickers. |

## Intra-layer DAG
```
own-pj-rest ──► money-loc-rest ──► money-fe
     │                                 ▲
     ├────────► rate-rest ─────────────┤
     └────────────────────────────────┘
```
All three leaves descend from the contract `own-pj-rest`; `money-fe` also gathers `money-loc-rest` + `rate-rest`. Max degree 3 (≤6). The two backend leaves can build in parallel after the contract freezes.

## DRAFT founder gate (per the DRAFT L2 gate)
Founder creates an OwnPJ (vat_payer on) and confirms its `TVA …` / `Impozit pe venit …` counterparties auto-appear (read-only); creates a CashDesk (blocked correctly when the PJ rule is violated); creates a BankAccount; sets an EUR exchange rate and sees it in the rate history.

## Key deviation from the DRAFT
**`rate-rest` (DRAFT 2.3):** the DRAFT said the only rate writer is on the unmerged treasury branch. WRONG — corrected per authoring-notes §1.3: `nomenclature/exchange_rates.py:set_default_rate` already exists on **main** (+ a Django-admin add-path); the treasury branch adds a **second, duplicate** writer `treasury/currency.py:set_exchange_rate` (same `DefaultExchangeRate` table; stricter — float-rejection + MDL-base guard). So this stage = expose REST **over the existing `set_default_rate`** AND **reconcile/dedup** the two writers to one canonical path (fold treasury's guards in; `set_exchange_rate` delegates or is removed) so L8 reuses one. Not green-field. `DefaultExchangeRate` is intentionally NOT a NomenclatureBase → it gets its own route, never the generic `/api/nomenclature/` CRUD.
