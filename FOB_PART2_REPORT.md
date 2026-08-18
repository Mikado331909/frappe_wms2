# FOB Part 2 — Type 3 "Purchased with customer" (FOB direct) — pilot report

**frappe_wms2 0.6.0.** Full suite on a fresh disposable site (erpnext
**16.26.2**, Python 3.14.6): **48 tests, OK** — the 8 new Part 2 gate tests
(S1–S8) plus Part 1's 7 and all 33 from the earlier phases, so everything
before this is regression-proven in the same run.

---

## What was built

**Flag-driven, as with Type 4.** A new Check on `WMS Ownership Type`,
`create_concept_invoice_at_intake`, seeded `1` on the Type 3 row only. No code
anywhere compares against the string "Purchased with customer".

**Intake (2.1/2.2).** On submit of a Purchase Receipt or Stock Entry Material
Receipt, each Type 3 row produces one **draft** Sales Invoice: customer, item,
qty and batch from the row, `update_stock = 1`, sourced from the customer
warehouse and the **exact storage location** the line was received into, rate
from the shared Part 0 Price List resolver. No Delivery Note anywhere. It is
never submitted automatically.

The work happens in **two phases on purpose**: every price is resolved before
anything is created, so a missing Item Price cannot leave a half-finished
trail behind — the intake refuses whole, naming customer and item.

**Restock (2.3/2.4).** `Sales Invoice.on_submit` finds the `WMS FOB Sale` row
for that invoice and posts one Material Receipt: same qty, same batch, **same
warehouse, same storage location**, `basic_rate = 0` with
`allow_zero_valuation_rate`. Nothing moves; only the valuation drops. It is
idempotent (a row that already has a restock is skipped) and it ignores Type 4
rows, which share the same audit doctype but never restock. A missing location
refuses rather than guesses.

**The batch stamp never changes.** It stays Type 3 + its customer forever;
`_assert_batch_compatible` is untouched and still passes for a top-up of the
same batch (S5). Whether a batch has been sold is derived from the
`WMS FOB Sale` row, not stored on the batch.

**Guard rails (2.6).** Re-running the intake hook creates no second invoice —
`get_existing_fob_sale()` is checked per source row and skips quietly (S2).
The restock Stock Entry carries a `wms_fob_restock_for` marker so it can never
be mistaken for a new intake and start a second sale.

### Verified at ledger level (independent of the tests)

A kept test site, inspected directly:

```
Purchase Receipt  MAT-PRE-…06   qty  +5   value +100   -> balance  5 @ FX844-294
Sales Invoice     ACC-SINV-…05  qty  -5   value -100   -> balance  0 @ FX844-294
Stock Entry       MAT-STE-…02   qty  +5   value    0   -> balance  5 @ FX844-294

GL of the invoice:  Cost of Goods Sold  dr 100 | Stock In Hand  cr 100
                    Debtors             dr  55 | Sales          cr  55
Batch stamp after the whole flow: ('Purchased with customer', '… Cust A')
```

Quantity unchanged (5 → 0 → 5, same location), value from cost to zero exactly
once, real revenue and COGS on the books, ownership stamp intact.

### Gate tests (live)

| | |
|---|---|
| S1 | Intake at full cost (SLE value 40 for 10 × 4) into the customer's material warehouse; batch stamped Type 3 + customer |
| S2 | Same submit creates exactly one DRAFT invoice, `update_stock = 1`, batch/warehouse/location from the row, rate from the Price List (11, not the cost 4); no Delivery Note exists; re-running the hook creates no second invoice |
| S3 | Before confirmation: stock still present and still at cost; no restock entry exists |
| S4 | Submitting the invoice creates exactly one restock: same qty, batch, warehouse and location, zero valuation, no outgoing leg. Net qty unchanged; restock SLE value 0; the invoice's GL balances and carries the 10 × 11 sale |
| S5 | The batch's (Ownership Type, Customer) stamp is identical before and after, and still passes the anti-mixing check |
| S6 | A customer with no Item Price fails the ENTIRE intake naming item and customer; no audit row, no orphan invoice, no stock ledger entry for that item |
| S7 | Discarding the concept invoice leaves the material at cost, un-invoiced, indefinitely — no restock, no error, stamp untouched |
| S8 | A zero-valued (sold and restocked) batch and a still-at-cost batch of the SAME item in the SAME customer warehouse keep their own rates — the at-cost batch still carries 5 × 30 = 150 |

---

## One design decision I had to make (please confirm)

**A concept invoice could not be deleted at all.** The `WMS FOB Sale` audit row
links it, and frappe refuses to delete a document that is linked — so S7's
"the accountant discards it instead" was impossible in practice.

Rather than dropping the audit row (losing the evidence that a concept existed)
I added a `Sales Invoice` `on_trash` / `on_cancel` hook: the audit row releases
its invoice link and sets a new **`Concept Discarded`** flag. The invoice can
then be deleted, the row remains as evidence, and the material stays parked.

Two consequences worth your explicit agreement:

1. **A discarded concept is not re-created automatically.** The idempotency
   check still finds that source row, so re-submitting the intake will not
   produce a second invoice. If the accountant discards one by mistake, the
   sale has to be invoiced manually. Say the word if you would rather a
   discarded row be re-invoiceable.
2. **Cancelling a SUBMITTED invoice that already restocked is refused**, with a
   message naming the restock entry. Unwinding a confirmed FOB sale is exactly
   the "no reversal" boundary you agreed for Part 1 — but note this now shows
   up as an explicit refusal rather than silence.

---

## Caveats

1. **No reversal of a confirmed Type 3 invoice.** Once submitted and
   restocked, the sale stands. Correct it with a credit note and a manual
   stock correction, outside this feature.
2. **Cancelling the intake document after a concept invoice exists is out of
   scope** — the same boundary Phase 3b drew for consumed WIP. The concept
   invoice would be left pointing at a cancelled receipt; nothing cleans that
   up automatically.
3. **No partial-quantity concept invoices.** The whole received line becomes
   one invoice line. Splitting an intake line across sales is out of scope.
4. **One batch per Type 3 line.** The sale and the restock act on one specific
   batch, so a line whose bundle carries several batches is refused with a
   clear message.
5. **Type 2 remains untouched** — still routed by its static
   `enforce_warehouse`, not per customer. It could adopt the Part 0 resolver
   later; no migration was performed.
6. **The customer warehouse mixes zero-valued and at-cost batches by design**
   (S8), which is exactly why the Part 0.1 batch-wise valuation guard exists.
   If Stock Settings' `do_not_use_batchwise_valuation` is ever switched on for
   a Moving-Average item, Type 3 intake for that item is refused rather than
   silently corrupting both batches.
7. **Pilot scope.** As you flagged, this is the hardest of the four types.
   Everything above is proven on a disposable site with synthetic data; I would
   run one real receipt through it on a test copy of the production site before
   wide rollout.
