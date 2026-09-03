# Draft reservations + concept-invoice currency (frappe_wms2 0.13.0)

**LIVE RESULT.** Full suite on a fresh disposable site (erpnext 16.26.2,
Python 3.14.6): **90 tests, OK** — 9 new gate tests plus all 81 existing ones,
including both full Type 3 and Type 4 suites re-run against the currency change
as the request required.

---

## 1. `invoiced_qty` no longer moves at draft creation

### What changed

`WMS FOB Invoicing Progress` gained **`reserved_qty`**, and the three states
are now distinct:

- **draft created** → `reserved_qty` increases; `invoiced_qty` untouched;
- **invoice submitted** → the amount moves from `reserved_qty` to
  `invoiced_qty`;
- **draft discarded / invoice cancelled** → the amount is released.

`remaining = consumed_qty − invoiced_qty − reserved_qty`, so an open draft
still blocks the same material from landing on a second concept invoice — the
reason the old code incremented `invoiced_qty` in the first place — without
counting as billed.

### On the gap you asked about: Type 4 had **no** discard path

Confirmed — there was none. Only Type 3's `discard_concept_invoice` existed,
and the `Sales Invoice` `on_trash` / `on_cancel` hooks pointed at it alone. A
discarded Type 4 draft left `invoiced_qty` permanently inflated with nothing to
undo it. Closed by mirroring that pattern rather than inventing another:
`production.release_fob_reservations` is registered on the same two hooks.

It handles both states, using a new `invoice_settled` flag on `WMS FOB Sale` to
tell them apart: an unsubmitted draft releases its **reservation**, and a
cancelled *submitted* invoice gives back what it had already **billed**. The
second case is slightly beyond the letter of the request, but leaving it out
would have reproduced exactly the same class of silent underbilling one step
later.

**One bug found while building this:** both handlers run on the same hook, and
Type 3's cleared `sales_invoice` on *every* row of that invoice — including
Type 4 rows — before the Type 4 handler could see them, so nothing was
released. Each handler is now scoped to its own rows (`work_order` set or not),
which the R2/R3 tests would otherwise have caught in production instead.

---

## 2. Currency and conversion rate

### What was wrong, and what the shared resolver now does

`fob.resolve_selling_rate()` is the single implementation for both types and
now returns `rate`, `price_list`, `currency` **and** `conversion_rate`:

- **Currency** always comes from the Price List the rate was resolved from.
  Type 3 was already correct; **Type 4 was taking the Delivery Note's
  currency** while pricing from a possibly different Price List — the invoice
  claimed a currency its own rates were never calculated in. It now uses the
  Price List's, and refuses outright if one Delivery Note's lines somehow
  resolve to two currencies.
- **Conversion rate** is 1 only when the Price List currency genuinely equals
  the company's default currency. Otherwise it comes from
  `erpnext.setup.utils.get_exchange_rate(from, to, posting_date)` — signature
  verified in v16.26.2, where it returns 1 for an identical pair and **0.0**
  when it cannot resolve one. A `0.0` is refused with an error naming the
  customer, the item and the currency pair; there is no fallback to 1.

### A second currency bug the tests exposed

Writing the foreign-currency test turned up something not in the report: a
25 USD price list was resolving to **7.94**. `get_item_details()` returns the
rate already converted into the document's currency using the exchange rate it
resolves for the party — so on a foreign-currency list it hands back the rate
in *company* currency. Booking that as a USD invoice line would have
under-billed by the exchange rate itself.

The resolver now takes the raw list rate through ERPNext's own
`get_price_list_rate_for()` (falling back to `get_item_details` when it returns
nothing), so a USD price list invoices 25 USD with a conversion rate of 3.15,
not 7.94 of anything.

---

## Gate tests (live)

| | |
|---|---|
| R1 | Draft creation sets `reserved_qty = 10`, `invoiced_qty = 0`; submitting moves it across |
| R2 | Deleting the draft releases the reservation, and the same material bills in full on the next Delivery Note |
| R3 | Three create-and-discard cycles in a row lose nothing: consumption stays 10, the next shipment still bills its full share, the rest remains billable |
| R4 | An open draft still blocks double-allocation — a second shipment can only take what is left (8 + 4 reserved of 12 consumed, 0 invoiced) |
| C1 | A base-currency Price List still resolves `conversion_rate = 1` — no regression for the common case |
| C2 | A USD Price List resolves rate 25 and the real rate 3.15, not 1 and not 7.94 |
| C3 | No exchange rate for the pair refuses cleanly, naming customer, item and both currencies |
| C4 | Type 3 and Type 4 call the *same function object* (asserted with `assertIs`), and neither module contains a hardcoded `"conversion_rate": 1` |
| C5 | A Type 4 invoice carries the Price List's currency, not the Delivery Note's |

Four existing Type 4 assertions were updated: they asserted `invoiced_qty`
immediately after draft creation — i.e. they encoded the bug. They now assert
`reserved_qty` with `invoiced_qty = 0`, which is the corrected meaning.

## Caveats

1. **No backfill**, as agreed — existing progress rows carry `reserved_qty = 0`
   and whatever `invoiced_qty` the old code wrote. With no production data this
   is clean; on a pilot site any pre-existing rows are worth deleting rather
   than reasoning about.
2. **`get_exchange_rate` may call an external API** if a Currency Exchange
   record is missing and Currency Exchange Settings are enabled. In an offline
   or misconfigured environment that path fails and returns 0.0 — which we
   refuse on, so the outcome is a clear error rather than a wrong rate.
3. **Cancelling a submitted Type 4 invoice releases the billed quantity** (see
   above). If the intent is ever that a cancelled invoice should *not* make the
   material billable again, that is a one-line change in
   `release_fob_reservations`.
4. **Reserved quantity is released on `on_trash`/`on_cancel` only.** A draft
   left open forever holds its reservation indefinitely — visible in the
   `Reserved qty` column, and by design: it is genuinely allocated until
   someone decides otherwise.
5. Nothing from the broader architectural review was built.
