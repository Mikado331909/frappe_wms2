# FOB Parts 0 + 1 — Report (frappe_wms2 0.5.0)

**LIVE RESULT.** Full suite on a fresh disposable site (erpnext **16.26.2**,
frappe 16.31, Python 3.14.6): **40 tests, OK** — the 7 new Part 1 gate tests
(T1–T7) plus all 33 existing ones, so Phases 0 / 2a / 3a / 3b are
regression-proven in the same run.

Stopping here for your review before Part 2, as instructed.

---

## Part 0 — shared infrastructure

### 0.1 Pre-flight + runtime guard

Verified against the installed source (`Batch.set_batchwise_valuation`,
v16.26.2). The exact dangerous condition is:

```python
get_valuation_method(item) == "Moving Average"
    and Stock Settings.do_not_use_batchwise_valuation
```

Everything else is stamped `use_batchwise_valuation = 1`. Two consequences
the brief asked me to pin down:

- It is **per item, not global** — a FIFO item is safe even if the global
  flag is on, which is why the guard resolves the item's own method.
- `get_valuation_method()` is called **without a company**, so an item with
  no `valuation_method` falls back to **Stock Settings.valuation_method**
  (default FIFO), *not* to the Company default. `fob.uses_batchwise_valuation()`
  mirrors that exactly rather than re-deriving it.

`assert_batchwise_valuation()` runs at every Type 3/4 intake line and refuses
with a plain explanation if that item's batches would land on `0`. On the
site checked, the flag reads `0` (batch-wise valuation ON) and the default
method is FIFO, so nothing is blocked today — the guard exists so a later
settings change cannot silently corrupt a customer warehouse.

### 0.2 Customer warehouses + material classification

`WMS Settings` gained `fabrics_item_group` and `trimmings_item_group`
(Link → Item Group), plus an optional `customer_warehouse_parent`.
`fob.get_item_material()` walks an item's `parent_item_group` chain until it
reaches one of the two configured groups (the group itself or any descendant,
with a cycle guard), and **refuses** — naming the item and its group — if it
reaches neither, or if the settings are unconfigured.

`customer_warehouse.get_or_create_customer_warehouse(customer, material)` is
the single shared resolver, idempotent (name check plus a field-based check,
so a differently-suffixed existing warehouse is reused rather than
duplicated). Warehouses are named
`"{customer} - {configured group name} - {abbr}"` and hang under the
configured parent, else the company's root group warehouse.

**No business vocabulary is hardcoded anywhere.** The strings "Fabrics" and
"Trimmings" appear in the code only as internal dict keys (`FABRICS`,
`TRIMMINGS`) and in field *labels*; every visible name comes from the
company's own Item Groups. The Part 1 tests deliberately configure groups
named `WMS-STOFFEN-…` / `WMS-FOURNI-…` with a nested child group, so a
hardcoded English string would fail the gate.

### 0.3 Routing flag

`route_to_customer_warehouse` (Check) added to `WMS Ownership Type`, seeded
`1` on types 3 and 4 only. `ownership.py` gained one conditional branch in
`_route_rows` / `_validate_intake_row`; the `enforce_warehouse` path used by
Types 1 and 2 is byte-for-byte unchanged. A line that already carries a
different warehouse is refused rather than silently re-routed.

I also added a second flag, **`requires_bom`**, seeded `1` on Type 4 only —
the closing rule (1.1) is a property of the ownership type, and expressing it
as a flag keeps `production.py` from ever having to compare against the type's
*name*. `_is_type4()` identifies the type by its flags, not by the string
"Purchased for customer".

### 0.4 Pricing

`fob.resolve_selling_rate()` — one resolver used by both types. It calls
ERPNext's own `get_item_details` with the price list from
`party.get_default_price_list` (customer, else customer group, else Selling
Settings). No cost, no zero, no markup: if nothing resolves it throws, naming
customer, item and price list.

One fix worth flagging: I originally read the customer through
`frappe.get_cached_doc`, and the full-suite run exposed that a price list
assigned moments earlier was invisible behind a stale cache. It now reads the
party's price list straight from the database — a genuine bug, not a test
artefact.

### 0.5 Finished-good batch = Work Order

`production.name_finished_good_batch()` runs `before_validate` on a
Manufacture Stock Entry and names the produced batch after the Work Order.
Confirmed in source that this needs **no naming-series change**:
`Batch.autoname()` uses a supplied `batch_id` verbatim and only falls back to
`batch_number_series` / naming series / hash when it is empty. The batch is
created with `reference_doctype = Work Order`, and
`get_work_order_for_batch()` resolves the other way (identity first, batch
reference as fallback).

---

## Part 1 — Type 4 "Purchased for customer"

- **Intake** routes to the customer's material warehouse, at real cost, with
  the batch stamped — plus the BOM closing rule: the item must appear as a
  raw material in at least one **submitted, active** BOM
  (`BOM Item` joined to `BOM` on `is_active = 1 and docstatus = 1`), else the
  whole submit is refused naming the item.
- **Consumption** is untouched: `picking.py` was not modified, and nothing is
  pre-flagged at pick time.
- **Invoicing** hooks `Delivery Note.on_submit`. Per shipped batch it resolves
  the Work Order, reads that Work Order's own submitted Stock Entries
  (`Manufacture` / `Material Transfer for Manufacture`), reconstructs
  consumption from the **Serial and Batch Bundle** on the resulting SLEs
  (never `SLE.batch_no`, which is empty on v16), keeps only batches stamped
  Type 4 **for this Delivery Note's customer**, and invoices
  `BOM qty per unit × shipped qty`, capped by what is left un-invoiced.
  Everything lands on **one draft Sales Invoice per Delivery Note**
  (`docstatus 0`, `update_stock 0`, no Delivery Note of its own). A shipment
  whose production used none of this customer's Type 4 material creates no
  invoice at all, silently.
- **Split shipments**: `WMS FOB Invoicing Progress`, one row per
  (finished-good batch, raw-material batch), fixes total consumption at
  creation and accumulates invoiced qty. The cap is hard.
- **Audit**: `WMS FOB Sale` rows link source Delivery Note row → item, batch,
  qty, rate, price list, finished-good batch, Work Order, invoice. The
  doctype already carries `restock_stock_entry` for Part 2.

### Gate tests (live)

| | |
|---|---|
| T1 | Type 4 intake → customer's material warehouse, real cost (SLE value 30 for 10 × 3), batch stamped |
| T2 | Item in no active BOM refused; item outside both configured groups refused naming the group; nothing created |
| T3 | Pick → WIP → Manufacture produces a batch named after the Work Order; consumption reconstructs to 10 (2/unit × 5) |
| T4 | One DRAFT invoice, `update_stock 0`, exactly the Type 4 line at BOM qty × shipped qty and Price List rate; own-use material in the same BOM absent; audit + progress rows correct |
| T5 | 6 + 4 units invoice 12 + 8 = the full 20 consumed, never more; and a run that really consumed 14 against a BOM share of 20 is invoiced at 14 |
| T6 | Missing Item Price fails the whole Delivery Note naming item and customer; no invoice, no `WMS FOB Sale`, no progress row |
| T7 | Two batches of one item at cost 5 and 50 in the same customer warehouse keep their own incoming rates; both stamped `use_batchwise_valuation = 1` |

---

## Caveats

1. **Type 2 is untouched.** "Supplied by customer" still routes through its
   static `enforce_warehouse`, not per customer. It could adopt this resolver
   later for consistency — out of scope now, and no migration was performed.
2. **The concept invoice carries a storage location.** The Storage Location
   dimension is Mandatory on *every* stock-item line, including a Sales
   Invoice line that does not update stock (a documented Phase 2a consequence
   of applying the dimension to all doctypes). The draft invoice therefore
   sets the **WIP pot location** on its lines — where the material physically
   went when consumed. It has no stock effect (`update_stock = 0`). If you'd
   rather see a different value there, it is one function
   (`_invoice_line_location`).
3. **Types 3 and 4 are now active** (`is_active = 1`) via patch `v0_6`. Type 3
   intake will therefore be *accepted* from this deploy on, but Part 2's
   concept-invoice/restock logic is **not built yet** — a Type 3 receipt today
   routes to the customer warehouse at cost and nothing further happens. If
   you want Type 3 to stay closed until its pilot, say so and I will keep it
   inactive until Part 2 lands.
4. **`fg_completed_qty` is required** on a Manufacture Stock Entry
   (ERPNext-side); the tests set it explicitly. Nothing in the app depends on
   it, but a production entry built by other automation must set it too.
5. **Multiple Work Orders per finished batch** are not possible by
   construction — the batch *is* the Work Order — which is exactly the
   traceability property you asked for.
6. **A cancelled Delivery Note** does not reverse its concept invoice or the
   progress ledger. Same boundary Phase 3b drew: out of scope here, worth a
   decision before wide rollout.
7. `WMS Settings.fabrics_item_group` / `trimmings_item_group` are **not**
   field-level mandatory: making them `reqd` would block every existing
   deployment (including pick-flow-only ones) from saving WMS Settings at all.
   They are enforced where it matters — FOB intake refuses with a message
   pointing at WMS Settings. Flagging the deviation explicitly.

Nothing from Part 2 was built: no concept invoice at intake, no restock on
invoice submit, no `WMS FOB Sale` writing from the Type 3 path.
