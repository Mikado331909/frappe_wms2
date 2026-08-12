# Phase 0 Gate Report — frappe_wms2 on ERPNext v16

**Verification basis — LIVE RESULT.** The gate suite was executed on a live
bench: **frappe 16.25.0 / erpnext 16.26.2 (version-16), Python 3.14.6,
MariaDB 10.11** — the same ERPNext build and Python line as the target site.

```
bench --site <site> run-tests --app frappe_wms2
Ran 10 tests ... OK        # V0–V4 gate tests + 5 Storage Location unit tests
```

Verified on **two** sites: an established site (`frontend`-equivalent) and a
**completely sterile, freshly created site** (new-site → install-app erpnext →
install-app frappe_wms2 → run-tests), proving the whole chain — app install,
dimension registration, `patches.txt` parsing under Python 3.14's strict
configparser, and V0–V4 — works from scratch. Every mechanism was additionally
verified against the erpnext v16.26.2 source; file/line references below.

**Status legend:** ✅ PASS = executed live and green.

## Test-harness fixes applied (no change to the dimension design)

1. **Aggregate reads** — `frappe.get_all(fields=["sum(actual_qty) as qty"])`
   is rejected on this version ("SQL functions are not allowed as strings in
   SELECT"). `location_balance()` now plucks `actual_qty` rows and sums in
   Python.
2. **Batch bundle gate** — v16 refuses to build a Serial and Batch Bundle
   from the row-level `batch_no` unless Stock Settings →
   *"Activate Serial and Batch No for Item"*
   (`enable_serial_and_batch_no_for_item`) is on. Test setup now enables it
   (plus `use_serial_batch_fields`) before receiving batched stock.
3. **Mandatory vs. auto opening stock** — ERPNext's own test fixtures create
   Items with `opening_stock`; the auto opening-stock entry carries no
   `storage_location` and is **correctly** blocked by our Mandatory rule
   (this is the rule working, not a bug). The suite now (a) sets
   `IGNORE_TEST_RECORD_DEPENDENCIES` in the doctype test module so frappe
   never bootstraps that fixture chain, (b) creates every item explicitly
   with `opening_stock: 0`, and (c) all stock enters via vouchers that always
   supply a location.
4. **Sterile-site seeding** — on a site where the setup wizard never ran,
   the standard masters (Warehouse Type "Transit", UOMs, Item/Supplier Group
   roots, Stock Entry Types, Fiscal Year) don't exist. The suite seeds them
   using **ERPNext's own wizard fixture installer** (no-op on real sites).
5. **`patches.txt`** now carries an explicit empty `[pre_model_sync]`
   section so Python 3.14's stricter configparser accepts it on clean
   installs (proven by the sterile-site install + `bench migrate`).

---

## Registration: "Mandatory" and "Validate Negative Stock" on this version

Both checkboxes exist and are wired, but **both behave differently than the
label suggests on older versions** — worth stating explicitly:

**Mandatory (`reqd`)** — in v16 this is *not* a `reqd` flag on the custom
fields. Enforcement is server-side in
`StockController.validate_inventory_dimension_mandatory()`
(`controllers/stock_controller.py` ~L1182), driven by
`get_mandatory_dimension_fields()` (`inventory_dimension.py` ~L399):

- Enforced on save/submit; **skipped on cancel** and **skipped for
  non-stock (service) item rows**. Good.
- Stock Entry Detail: source location required only when `s_warehouse` is
  set; `to_storage_location` required only when `t_warehouse` is set. So a
  pure receipt needs only the target location, a pure issue only the source.
- Purchase Receipt Item / Delivery Note Item / Stock Reconciliation Item:
  `storage_location` unconditionally required on stock-item rows. PR
  additionally requires `rejected_storage_location` when `rejected_qty > 0`.
- **Caveat 1:** with `apply_to_all_doctypes` (the only native way to cover
  our four child tables with one dimension — the alternative binds a
  dimension to exactly ONE doctype), the field also lands on Sales Invoice
  Item / Purchase Invoice Item / POS Invoice Item etc., and the mandatory
  check fires there **even when the invoice does not update stock**. If
  invoices are always made from DN/PR the value maps across and this is
  invisible; standalone invoices of stock items will demand a location.
  Acceptable for Phase 0; flagged for review.
- **Caveat 2:** cosmetic — display-only fields also get created on a few
  batch/serial-carrying doctypes outside stock flow (e.g. Putaway Rule).
  Harmless.

**Validate Negative Stock** — enforced in
`StockLedgerEntry.validate() → validate_inventory_dimension_negative_stock()`
(`stock_ledger_entry.py` ~L107): for every **outgoing** SLE that carries the
dimension, available qty for `(item, warehouse, storage_location)` from all
prior (by posting datetime, non-cancelled) SLEs must cover the issue.

- Runs **independently of** Stock Settings → Allow Negative Stock. Good.
- **Caveat 3:** it validates only when the outgoing row *has* a location.
  An outgoing SLE with an empty location skips the per-location check.
  Mandatory=ON closes this hole on the applied doctypes — the two checkboxes
  are a package deal; never relax Mandatory without rethinking V4.
- **Caveat 4 (back-dating):** the check compares against the balance *at the
  posting datetime*. A back-dated issue that turns a **later** per-location
  balance negative is not re-validated at dimension level (warehouse-level
  future-SLE validation still applies). With normal, current-dated postings
  this never occurs; treat back-dated stock entries as a controlled
  exception.

Registration is reproducible: `after_install` hook + `[post_model_sync]`
patch both call the same idempotent `ensure_storage_location_inventory_dimension()`,
which also **asserts** the resulting fields on the four required child tables
+ Stock Ledger Entry and fails loudly if anything is missing. Note: once
SLEs exist against the dimension, ERPNext freezes its configuration except
`validate_negative_stock` / `condition` (`DoNotChangeError`) — configure
before go-live.

---

## V1 — Same-warehouse L1→L2 transfer: ledger moves, zero GL — ✅ PASS (live)

- Same source/target warehouse is **explicitly allowed** for purpose
  "Material Transfer" (`stock_entry.py` ~L974: the same-warehouse throw
  exempts Material Transfer / Material Transfer for Manufacture). There is
  even an optional Stock Settings flag *Validate Material Transfer
  warehouses* whose documented behaviour is: same warehouse allowed **iff at
  least one inventory dimension differs** — i.e. our exact use case is the
  intended one (`stock_entry.py` ~L1328).
- SLE mapping: outgoing leg takes `storage_location`, incoming leg takes
  `to_storage_location` (`stock_controller.update_inventory_dimensions`
  ~L1226). Two SLEs, one −qty @L1, one +qty @L2, same warehouse.
- GL: both legs post identical value against the same warehouse account;
  the GL map nets to zero and no GL Entries are written. The automated test
  asserts `GL Entry == []` for the transfer **and** asserts the preceding
  receipt *did* create GL entries (perpetual inventory active), so the
  neutrality check cannot pass vacuously.

## V2 — One line carries batch + location — ✅ PASS (live)

- The SLE carries `storage_location` directly (custom target field on Stock
  Ledger Entry).
- **Version caveat, important:** in v16 the batch does **not** sit in
  `SLE.batch_no` on submit — it lives in a **Serial and Batch Bundle**
  linked from the SLE (`stock_ledger_entry.on_submit → SerialBatchBundle`;
  `SLE.batch_no` is only populated on the deprecated cancellation path).
  Functionally the gate holds: one ledger row carries both batch (via
  bundle) and location, and Stock Balance filters by both (the report joins
  bundles and auto-adds dimension filters —
  `stock_balance.py` L16/L70/L184). But **any later phase that reads
  `SLE.batch_no` directly is wrong on v16** — always resolve via the bundle
  (the test helpers in `tests/setup_records.py` show how).

## V3 — One batch split across L1/L2 — ✅ PASS (live)

- Two receipt rows, same batch, different locations → two SLEs, each with
  its own bundle referencing the same Batch, each with its own
  `storage_location`. Nothing in v16 restricts a batch to one dimension
  value.
- Totals cannot drift by construction: the per-location figures and the Bin
  are both derived from the same single ledger. The test still asserts
  `sum(locations) == Bin.actual_qty`.

## V4 — Cannot over-issue / pick from empty location — ✅ PASS (live)

- The sharp version is tested: warehouse holds 10 (6 in L1 + 4 in L2),
  issue 8 from L1. Warehouse-level stock is sufficient, global negative
  stock is OFF but wouldn't matter — only the per-dimension check
  (`InventoryDimensionNegativeStockError`) can and must block it.
- Positive control included: issuing exactly 6 from L1 succeeds; issuing 1
  more from the now-empty L1 is blocked again (no over-blocking, no
  pick-from-empty).
- Caveats 3 and 4 above apply (empty-location rows; back-dating).

---

## Verdict

**GATE PASSED — live, on ERPNext v16.26.2 / Python 3.14.** All four
architectural assumptions hold as executed behaviour, not just as source
reading: same-warehouse dimension transfers move stock per location with zero
GL entries (with perpetual inventory proven active in the same test); a single
ledger row carries location + batch (batch via Serial and Batch Bundle — any
later phase must resolve batches through the bundle, never `SLE.batch_no`);
one batch splits cleanly across locations with per-location totals equal to
the Bin; and issuing more than a location holds is blocked by
`InventoryDimensionNegativeStockError` even when warehouse stock suffices,
with the positive control confirming exact-quantity issues still pass and an
empty location cannot be picked from.

Additional live results: the Mandatory rule blocks a receipt line without a
target location at save time (V0), and it also blocked ERPNext's own
opening-stock test fixture — evidence the rule catches location-less stock
movements from *any* code path, which is exactly what Phase 1+ will rely on.

Phase 1 may proceed. Nothing beyond Phase 0 was built: no pick document, no
FIFO, no one-customer-per-location rule, no reason list, no traceability
report, no auto-batch.
