# Phase 3a Report — Core Picking Flow (frappe_wms2 0.3.0)

**LIVE RESULT.** Full suite executed on a live bench pinned to your exact
build — **erpnext 16.26.2**, frappe 16.31, **Python 3.14.6**, MariaDB 10.11 —
on two sites (an established one and a sterile fresh one: new-site → install
erpnext → install frappe_wms2 → migrate → run-tests):

```
bench --site <site> run-tests --app frappe_wms2
Ran 26 tests ... OK
```

That covers 10 Phase 3a tests, the 6 Phase 2a ownership tests (types 1/2) and
the 5 Phase 0 gates + 5 Storage Location unit tests — so the earlier phases
are regression-proven in the same run.

## Collision safety (required, and it found a real bug)

The suite is built for a site that already holds 622 real locations and live
stock:

- Every location code a test creates uses **gang X/Y/Z with niveau ≥ 700**
  plus a random draw — outside the real code space entirely.
- Items, batches, customers, item groups, warehouses, MRs and the WIP pot all
  carry a random per-run token; every balance assertion is scoped to a
  freshly created throwaway item, so pre-existing stock can never move a
  result.
- `tearDownClass` deletes the drafts and masters it created; submitted stock
  documents are deliberately left (they are ledger history, and the items
  they touch are throwaway).

Applying this to the **older** phases exposed a genuine hazard: the Phase 0
and Phase 2a tests used hardcoded codes `FA1-1`, `FA1-2`, `TA9-1`, `TA9-2`.
On your populated site those are **real shelves** — the old suite would have
posted test stock into them. They now use throwaway codes too. This was a
latent bug in the delivered Phase 0/2a tests, not a Phase 3a issue.

## What was built

**Bundling — `WMS Pick Batch`.** Customer is derived from each MR's Sales
Order; bundling MRs of two customers **throws** (T1), it is not a warning.
Demand is summed per item across the bundled MRs (T2). The demand table shows
demand / on-pick-lists / open per item.

**Reservation.** "On pick lists" is not a stored counter — it is computed
from the actual pick list lines (drafts included, cancelled excluded), so it
cannot drift. Generation only ever allocates the **open** remainder; when
everything is listed, a second generation for the same group finds nothing
and says so (T3).

**Item-group selection.** `get_item_groups` returns the groups present with
their open qty; a dialog lets the user tick which to pick now and generates
**one pick list per ticked group** — fabrics first, trimmings later, with the
untouched group still fully open (T4).

**FIFO.** Oldest batch first, walking batches until the order demand is met;
a partly consumed batch keeps its rest (T3 asserts 3 + 4 + 2 across three
batches). "Qty available on batch" is the live balance of that
(item, batch, location) read from the ledger via the bundle.

**Print format** (`WMS Pick List`, shipped as a fixture): customer at the
top, the exact columns Order | Item | Qty needed | Qty available on batch |
Qty to pick from batch | Location | Batch | Picked qty | Batch empty?, one
**sum row per item**, blank boxes for the floor, and **no value/price
anywhere** — all asserted by rendering the format in T10.

**Processing and submit.** Saving a draft never touches stock (T5 asserts the
balance is unchanged after save). On submit: a Material Transfer books the
picked quantities out of their batch+location into the WIP pot, and a
separate Material Issue writes flagged-empty (batch, location) balances to 0
with the reason in the remarks (T6). Surplus posts once a surplus-valid
reason is given (T7); a missing reason blocks the save/submit; picking more
than the location holds is refused with a clear message before the dimension
rule would fire. Processor-added lines are allowed only from **same-customer**
batches — the other customer's batch is refused and never even offered by the
allocator (T8). Reasons come from one shared, self-managed master with
shortage/surplus flags; a reason a user creates in the UI works immediately
(T9).

**WIP provenance.** WIP is a simple pot: one warehouse, one sentinel
location, no tracking inside it. Provenance is preserved and readable via
`get_wip_provenance()`: pick list, customer, Material Request, Sales Order,
item, batch, batch ownership type, batch customer, qty and the Stock Entry
(T5 asserts all of it).

## Follow-up fix: T5 on a populated site (harness, not behaviour)

On the live site `test_t5_batch_cannot_mix_owners` failed because a
customer-supplied line entered at rate 0 came back as rate 1.0, and the
zero-valuation rule then correctly refused it. Root cause, traced in the
v16.26.2 source and reproduced locally:

1. `get_item_details.insert_item_price()` auto-creates an **Item Price** from
   the first receipt of an item when Stock Settings has
   *Auto Insert Price List Rate If Missing* on (the default) and a default
   buying price list exists — which is the case on a populated site but was
   not on the bare test site, hence "passes here, fails there".
2. On the next receipt of that item, `accounts_controller
   .set_missing_item_details()` **force-refreshes the rate** whenever
   `use_serial_batch_fields` is set and a `batch_no` is present:
   `if fieldname == "batch_no" ... if ret.get("rate"): item.set("rate", ...)`.
   So the stored price silently overwrote the 0 the test asked for.

The zero-valuation enforcement was doing exactly its job and is **unchanged**.
The test factory now enters a zero-valuation receipt the way a real one is
entered: a dedicated empty buying price list, `ignore_pricing_rule = 1`,
`auto_insert_price_list_rate_if_missing` off, and `rate`/`price_list_rate`/
discount/margin/last-purchase pinned to 0 on any line meant to carry no
value. It also asserts, right after insert, that no rate was injected — so a
future regression of this kind is reported as a harness fault instead of
being blamed on the app rule.

Verified by reproducing the production condition on the test site (default
buying price list + an Item Price of 1.0 for the item): the **old** factory
reproduces your exact failure, the **new** one keeps the line at rate 0 and
the SLE at value 0. The full suite then ran on a **populated** site (772
storage locations, 622 of them with real-looking codes, auto-price-insert
enabled): **26/26 OK**, and a check confirms zero real-code locations were
touched by test stock.

## Caveats — flagged honestly

1. **The WIP pot needs a sentinel location.** The Phase 0 dimension is
   mandatory on stock lines, so material cannot enter *any* warehouse without
   a Storage Location — including WIP. I added an `is_special` flag to
   Storage Location: such a location skips the `FA1-2` code format and
   carries no parsed components. It is a mechanical necessity, not a model
   change; the alternative (relaxing the mandatory dimension) would be worse.
   Set the pot in **WMS Settings** before the first submit.
2. **Own-use stock is pickable for a customer order by default.** Own-use
   batches carry no customer, so using them cannot *mix* customers. This is a
   judgement call, exposed as a switch in WMS Settings
   (`allow_customer_neutral_stock`) — untick it for strictly customer-owned
   picking. Another customer's batch is **never** allowed, switch or not.
3. **Reason rule, precise semantics.** A reason is required when picked ≠
   to-pick, or when a batch is flagged empty **while stock is still
   administered** (i.e. a correction is being booked). If a pick empties the
   balance exactly, the flag is set automatically and no reason is demanded —
   nothing is being corrected. Drift on a non-empty batch is deliberately not
   corrected (cycle count, later phase).
4. **Cancelling a submitted pick list is refused** with an explicit message.
   Cancellation/return is a later phase; a half-implemented reversal would be
   worse than a clear "not yet".
5. **Non-batched items are out of scope** for picking: ownership and customer
   separation ride on the batch, so an item without batches cannot be
   allocated. Worth confirming that all pickable materials are batched.
6. **Bundle-level warehouse scope**: allocation scans all warehouses except
   the WIP pot. If Crings ever holds stock in a warehouse that must not be
   picked from, that needs an exclusion — say so and it is a one-line filter.
7. **The test bench was built with `--skip-assets`** (frappe 16 now wants
   Node ≥ 24 for the asset build). This affects only frappe's print *wrapper*
   CSS, not the format itself — T10 renders the format body and asserts its
   content. On your real site with built assets the print view works
   normally.

Nothing beyond Phase 3a was built: no cancellation/return, no BOM
calculation, no cycle count, no invoicing.
