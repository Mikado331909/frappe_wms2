# Type 4 — batch per booking, per-Work-Order invoicing, traceability
**frappe_wms2 0.10.0**

**LIVE RESULT.** Full suite on a fresh disposable site (erpnext 16.26.2,
Python 3.14.6): **72 tests, OK** — 5 new gate tests for Parts A/B/C plus all 67
existing ones, so every earlier phase is regression-proven in the same run.

Part 0 was completed and reported earlier (double counting confirmed at 40 vs a
hand-counted 20, fixed by excluding `Material Transfer for Manufacture`); the
fix is live in this build and the Part 1 invoicing gates run against it.

---

## What was found empirically about `reference_name`

The document asked whether ERPNext links a Manufacture-created batch back to
its Work Order on its own. **It does — but the answer came with two surprises**,
both verified on a disposable 16.26.2 site rather than assumed:

**1. With `Make Serial No / Batch from Work Order` OFF (the default): yes.**
With our own hook disabled (verified disabled by listing the hooks frappe
actually loaded), a Manufacture booking created batch `PRB0-0002` from the
item's own series, and it came out referencing `Work Order / MFG-WO-2026-00007`
— not the Stock Entry. Note this contradicts what the source reads like:
`SerialBatchCreation.create_batch()` passes `voucher_type` / `voucher_no` as the
reference, which for a Stock Entry would give a Stock Entry reference. Some
path sets the Work Order instead. **I could not pin down which line does it**,
so I am reporting the observed behaviour and keeping our hook as a safety net
rather than claiming a mechanism I did not find.

Our `link_finished_good_batches_to_work_order()` therefore only writes when the
reference is missing or points elsewhere — a no-op in the default case, and
insurance if that behaviour differs on another site or version.

**2. With that setting ON: ERPNext itself crashes.** The Work Order pre-creates
empty batches, and the Manufacture entry then dies with
`MandatoryError: [Serial and Batch Bundle, …]: company` inside
`create_serial_and_batch_bundle()` — the bundle it builds has no company. This
is an ERPNext limitation, unrelated to this app; our old code masked it by
always supplying a batch. Since Part A stops doing that, I added a guard that
refuses such a booking with a readable explanation instead of that error.
**Practical consequence: keep that setting OFF** — which is also what the
per-booking traceability design wants.

---

## What was built

### Part A — one batch per Manufacture booking

`ensure_work_order_batch()` and the `batch_id = work_order` override are gone.
Each booking now gets whatever the finished-good item is configured to produce
(naming series / auto-create), exactly like any other batch-tracked item.

What remains is a narrow `on_submit` hook that ensures the produced batch
references its Work Order. It resolves batches through the shared
`_get_row_batches()` — the in-memory row is not refreshed by ERPNext's own
batch creation, the same trap that caused the earlier "Batch missing" bug.

`get_work_order_for_batch()` now treats the reference fields as the **normal**
path (batch names no longer coincide with Work Order names); the name match is
kept only for pre-Part-A batches. `get_work_order_batches()` is the new reverse
lookup.

### Part B — invoicing tracked per Work Order

`WMS FOB Invoicing Progress` is re-keyed from (finished-good batch, raw-material
batch) to **(Work Order, raw-material batch)**. Shipping any batch of a Work
Order draws down one shared allowance, so two batches on two Delivery Notes
cannot each claim the full consumption.

One behaviour worth flagging: the consumed quantity is now **refreshed on every
visit** rather than frozen at row creation. A later booking of the same Work
Order genuinely consumes more raw material, and the allowance has to grow with
it — freezing it would have under-billed multi-booking Work Orders.

### Part C — Work Order traceability overview

`get_work_order_traceability(work_order)` returns both lists: raw-material
batches (batch, item, qty consumed, ownership type, customer — stamps resolved
from the Batch, consumption from the same function invoicing uses, so the two
can never disagree) and finished-good batches (batch, qty, booking date,
Manufacture entry). Surfaced as a **"Batch traceability" button on the Work
Order form**.

---

## Gate tests (live)

| | |
|---|---|
| A1 | Two bookings → two distinct batches, neither named after the Work Order (no Batch with the WO's name exists), both from the item's own series; both resolve back to the same Work Order, and the reverse lookup returns exactly the two |
| B1 | 6 + 4 units of one Work Order shipped on two Delivery Notes invoice 12 + 8 = exactly the 20 consumed, never more; one progress row, fully drawn down |
| B2 | A single-booking Work Order invoices 3/unit × 5 = 15, unchanged by the batch splitting |
| C1 | A Work Order with a Type 4 batch (customer A) and a Type 2 batch (customer B), and two finished-good batches: all four listed individually with the right stamps and quantities — and only customer A's Type 4 material is invoiced |
| C2 | A Work Order with no booking yet returns empty lists without erroring |

---

## Caveats

1. **`Make Serial No / Batch from Work Order` must stay OFF** on this ERPNext
   version (see above). The guard explains it at the point of failure rather
   than letting ERPNext's mandatory-field error surface.
2. **The finished-good item must be configured for batches itself** —
   *Automatically Create New Batch* plus a series. Nothing overrides the name
   any more, so an item without that configuration will simply be refused by
   ERPNext ("Serial No / Batch No are mandatory"). The Part 1 fixtures were
   updated accordingly, which is the honest signal that real items need it too.
3. **Raw material is not attributed per booking**, as confirmed in the request:
   all batches of a Work Order share one undifferentiated raw-material pool for
   invoicing. The traceability overview reflects that — it shows what went in
   and what came out, not which input became which output.
4. **`finished_good_batch` remains on the progress doctype** as informational
   ("first batch this row was created from") but is no longer part of the key.
   Existing pilot rows keyed the old way are not migrated; with no production
   data that is by design, but a pilot site should be checked for stale rows.
5. **A cancelled Delivery Note still does not reverse** its concept invoice or
   the progress ledger — unchanged boundary from Part 1.
6. The observed `reference_name` behaviour (finding 1) is not something I could
   trace to a specific ERPNext line. If a future version changes it, our hook
   keeps the link working, and `get_work_order_for_batch()` would surface the
   problem loudly rather than silently mis-invoicing.
