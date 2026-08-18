# FOB Ownership Types 3 & 4 — final report (Parts 0–2)

**frappe_wms2 0.6.0** — all four ownership types now live.
Full suite on a fresh disposable site (erpnext **16.26.2**, frappe 16.31,
Python 3.14.6, MariaDB 10.11): **48 tests, OK**.

| Phase | Tests |
|---|---|
| Phase 0 gates (dimension foundation) + Storage Location units | 10 |
| Phase 2a — ownership at intake (types 1 & 2) | 6 |
| Phase 3a — picking flow | 11 |
| Phase 3b — cancel / partial return | 6 |
| **Part 1 — Type 4 (FOB per production)** | **7** |
| **Part 2 — Type 3 (FOB direct)** | **8** |
| **Total** | **48** |

Every earlier phase is regression-proven in the same run. Tests only ever run
on a throwaway site via `scripts/run_tests_disposable.sh`; the suite refuses to
start anywhere else.

---

## The four types, as they now behave

| | Intake value | Warehouse | Invoice | Restock |
|---|---|---|---|---|
| **1 Own use** | cost | own | — | — |
| **2 Supplied by customer** | **zero** | static consignment warehouse | — | — |
| **3 Purchased with customer** | cost | **customer's own, per material** | draft **at intake**, Update Stock | zero-value, on invoice confirmation |
| **4 Purchased for customer** | cost | **customer's own, per material** | draft **at delivery** of the finished good | — (material left at pick time) |

Types 1 and 2 were not touched. Their code path — including Type 2's static
`enforce_warehouse` routing — is byte-for-byte unchanged.

---

## Part 0 — shared infrastructure

**Batch-wise valuation guard.** Verified against the installed
`Batch.set_batchwise_valuation()`: a batch is only stamped
`use_batchwise_valuation = 0` when `get_valuation_method(item) == "Moving
Average"` **and** `Stock Settings.do_not_use_batchwise_valuation`. The check is
therefore per item, and the fallback for an item with no method is Stock
Settings (default FIFO), **not** the Company default. Type 3/4 intake refuses
for any item whose batches would land on `0` — a customer warehouse deliberately
holds cost-valued and zero-valued batches of the same item side by side, and
without batch-wise valuation they would corrupt each other silently.

**Customer warehouses.** `customer_warehouse.get_or_create_customer_warehouse()`
— one resolver shared by both types, idempotent, auto-provisioning on first use,
named `"{customer} - {configured group} - {abbr}"` under a configurable parent.

**Material classification is configuration, not code.** `WMS Settings` carries
`fabrics_item_group` and `trimmings_item_group`; an item's material is found by
walking its Item Group parent chain until it reaches one of them (the group or
any descendant). An item that reaches neither is refused, naming the item and
its group. **No category name, item group name or company name appears anywhere
in the code** — the gate tests deliberately configure groups named
`WMS-STOFFEN-…` / `WMS-FOURNI-…` with a nested child, so a hardcoded English
string would fail the suite.

**Flag-driven behaviour.** Everything type-specific is a Check on
`WMS Ownership Type` — `route_to_customer_warehouse`, `requires_bom`,
`create_concept_invoice_at_intake` — seeded idempotently and never overwriting a
user's edits. No code compares against a type's *name*.

**Pricing.** One resolver, `fob.resolve_selling_rate()`, using ERPNext's own
`get_item_details` and the customer's (or customer group's) default Price List.
No cost, no zero, no markup: if nothing resolves, the whole action refuses,
naming customer, item and price list.

**Finished-good batch = Work Order.** Confirmed no naming-series change is
needed: `Batch.autoname()` uses a supplied `batch_id` verbatim. A Sales Order
filled by two production runs is therefore traceable per run on the Delivery
Note, with no linking table.

---

## Part 1 — Type 4 "Purchased for customer"

Intake at cost into the customer's warehouse, plus the **closing rule**: the
item must be a raw material in at least one submitted, active BOM, else the
submit is refused — an item with no BOM could never be translated from shipped
finished goods into consumed raw material.

Shipping the finished good triggers invoicing. Per shipped batch the Work Order
is resolved from the batch identity, its own Stock Entries are read, consumption
is reconstructed **through the Serial and Batch Bundle** (never `SLE.batch_no`,
which is empty on v16), filtered to this customer's Type 4 batches, and invoiced
at `BOM qty × shipped qty` — capped by what is left un-invoiced. One draft
invoice per Delivery Note; a shipment that used none of this customer's Type 4
material creates nothing, silently.

`WMS FOB Invoicing Progress` fixes total consumption at creation and accumulates
invoiced qty, so split shipments can never double-bill. The cap is hard: a run
that really consumed 14 against a BOM share of 20 is invoiced at 14.

---

## Part 2 — Type 3 "Purchased with customer"

Intake at cost into the customer's warehouse and, in the same submit, a **draft**
Sales Invoice with `update_stock = 1` from that warehouse and that exact storage
location. Prices are resolved for every row *before* anything is created, so a
missing Item Price refuses the intake whole rather than leaving a half-finished
trail.

Submitting that invoice is what triggers the restock: same qty, same batch,
**same warehouse, same location**, zero valuation. Verified at ledger level:

```
Purchase Receipt   qty +5   value +100   -> balance 5 @ FX844-294
Sales Invoice      qty -5   value -100   -> balance 0 @ FX844-294
Stock Entry        qty +5   value    0   -> balance 5 @ FX844-294

GL: Cost of Goods Sold dr 100 | Stock In Hand cr 100
    Debtors            dr  55 | Sales         cr  55
Batch stamp afterwards: ('Purchased with customer', '… Cust A')
```

The batch's ownership stamp never changes; whether it has been sold is derived
from the `WMS FOB Sale` row, never stored as a second flag.

---

## Final addition — confirmation dialogs

Both outcomes of a concept invoice now state plainly which one they are before
anything happens (`public/js/fob_sales_invoice.js`, wired via `doctype_js`):

- **Submit** → *"You are about to **CONFIRM** this sale"* — explains that stock
  leaves at cost, revenue and COGS post, and the same quantity is restocked at
  zero value; and that it cannot be undone from there.
- **Cancel** → *"You are about to **CANCEL** this sale — the opposite of
  confirming it"* — explains that nothing is invoiced, the material stays at
  cost, and that a discarded concept is **not** created again automatically.

Anything other than an explicit yes — "Go back", Escape, the X — stops the
action. Only FOB-generated invoices are affected; a normal Sales Invoice keeps
ERPNext's standard behaviour. (Deleting a draft still goes through frappe's own
"permanently delete" confirmation, which we do not intercept.)

---

## Caveats carried forward

1. **No reversal of a confirmed Type 3 sale.** Once submitted and restocked it
   stands; cancelling such an invoice is refused, naming the restock entry.
   Credit note plus manual stock correction is the path.
2. **A discarded concept is not re-invoiced automatically** — accepted as built.
   The audit row survives with a `Concept Discarded` flag.
3. **Cancelling an intake document after a concept invoice exists** is out of
   scope, same boundary Phase 3b drew for consumed WIP.
4. **No partial-quantity concept invoices**; one batch per Type 3 line.
5. **A cancelled Delivery Note does not reverse** its Type 4 concept invoice or
   the progress ledger — agreed boundary.
6. **Type 2 keeps its static routing.** It could adopt the per-customer resolver
   later for consistency; no migration was performed.
7. **The concept invoice line carries the WIP pot location** (Type 4), because
   the Storage Location dimension is Mandatory on every stock-item line — agreed.
8. **Two bugs found and fixed during this build**, both real rather than test
   artefacts: the price resolver read the customer through a stale document
   cache, and a `WMS FOB Sale` row made its own concept invoice undeletable.
9. **Pilot advice stands for Type 3.** Everything is proven on disposable sites
   with synthetic data; run one real receipt through a copy of production before
   wide rollout.

## Deployment

Unchanged, and still never runs tests:

```bash
bench get-app <repo>
bench --site <site> install-app frappe_wms2
bench --site <site> migrate
```

Then, once per site: **WMS Settings** → Company, WIP pot, and the two material
Item Groups (required before any FOB intake). Patches `v0_6` and `v0_7` activate
types 3 and 4 and seed their flags on existing sites.
