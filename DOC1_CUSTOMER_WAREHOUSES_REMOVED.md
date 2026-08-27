# Customer warehouses removed + value-per-customer report (frappe_wms2 0.8.0)

**LIVE RESULT.** Full suite on a fresh disposable site (erpnext 16.26.2,
Python 3.14.6): **58 tests, OK** — every earlier phase regression-proven in the
same run, including the whole Type 3 intake → concept invoice → confirm →
restock chain against the simplified routing.

## What was removed

`wms/customer_warehouse.py` is **deleted**, not deprecated:
`get_or_create_customer_warehouse()` and `get_warehouse_for_item(customer, …)`
are gone, and nothing creates a Warehouse as a side effect of a receipt any
more. `WMS Settings.customer_warehouse_parent` is removed with it — there is
nothing left to parent.

## What replaces it

`wms/material_warehouse.py`, a straight lookup:

- **`WMS Settings.fabrics_warehouse` / `trimmings_warehouse`** (Link →
  Warehouse, company-filtered like every other WMS Settings link) — the
  company's own two existing warehouses.
- `get_warehouse_for_item(item_code)` classifies the item with the **unchanged**
  `fabrics_item_group` / `trimmings_item_group` parent-chain walk from the
  earlier FOB work, then returns the matching own warehouse. Only the
  destination changed; the classification logic is reused as-is.
- It refuses plainly if a warehouse is unconfigured, or belongs to another
  company.

The `route_to_customer_warehouse` flag on `WMS Ownership Type` keeps its
fieldname (so no data migration is needed) but is relabelled **"Route to
Material Warehouse"** with a description matching what it now does. Types 3 and
4 carry it; types 1 and 2 are untouched.

Batch stamping is unchanged — same Ownership Type + Customer, same code path.
Type 3's concept-invoice-then-restock is unchanged too: the restock still
targets the same warehouse and the same `storage_location` as the intake row,
which was never customer-warehouse specific.

## The new value-per-customer report

`wms/customer_value.py`:

- `get_customer_stock_value(customer=None, company=None)` — one row per
  (customer, ownership type, item, batch, warehouse) with **current qty and
  current value**.
- `get_customer_stock_summary()` — the same figures rolled up per customer and
  ownership type, plus a per-customer total.

Valuation comes from the ledger the same way the rest of the app reads stock:
per-batch quantity and `stock_value_difference` from the **Serial and Batch
Entry** rows of submitted bundles (verified present on this version), with a
fallback for legacy rows that carry `batch_no` straight on the SLE. No new
valuation method.

Zeros are reported, not filtered: customer-supplied and already-invoiced
FOB-direct stock genuinely *are* zero-valued, and that is the honest picture.

## Gate tests (live)

- **Routing** — a Type 4 receipt lands in the company's own trimmings
  warehouse, asserted to equal the configured warehouse, asserted *not* to
  contain the customer name, and asserted that **no warehouse was created** as
  a side effect. Same for Type 3 (S1).
- **Location exclusivity still holds** — the regression you flagged as most
  important, now that everything funnels through shared warehouses. All eight
  location-owner tests pass unchanged: two customers still cannot share a
  location, own-vs-customer still respects the toggle, and a location still
  frees itself when its stock leaves.
- **Type 3 end to end** — the full Part 2 gate (S1–S8) re-run against the
  simplified routing, including the batch-stamp identity check and the
  cost→zero valuation transition.
- **Value per customer** — a customer with one not-yet-invoiced Type 3 batch
  (5 × 6 = **30**) and one Type 2 batch (8 units, **0**) reports exactly that,
  attributed to the right ownership types, with a customer total of 30; another
  customer's stock does not leak in; and confirming the Type 3 invoice drops
  that batch to **0** with nothing to reset by hand.

## Caveats

1. **Configuration is now required before any FOB intake**: both material Item
   Groups *and* both material warehouses in WMS Settings. Each refuses with a
   message pointing at the setting rather than guessing.
2. **Existing pilot data** created under the old mechanism (customer
   warehouses, and any Storage Locations under them) can simply be discarded,
   as you said — nothing migrates it, by design.
3. **The location-exclusivity rule now carries more of the separation weight.**
   It still only covers *intake*; Material Transfer and Stock Reconciliation
   remain the open backdoors flagged earlier. That gap matters more than it did
   when warehouses also separated customers — worth closing next.
4. **Type 2 still routes via its own static `enforce_warehouse`**, untouched by
   this change. It could now point at the same material warehouses for
   consistency; out of scope here.
5. The value report reads submitted bundles only; a draft bundle contributes
   nothing, which is correct but means figures move at submit time, not before.
