# Bugfix — "Batch missing" on Type 3 intake with auto-created batches
**frappe_wms2 0.7.1**

**LIVE RESULT.** Full suite on a fresh disposable site (erpnext 16.26.2,
Python 3.14.6): **57 tests, OK** — the new regression test plus all 56
existing ones.

## What was wrong

`fob_direct.py` had its own `_row_batch(row)` that looked only at the
in-memory row (`row.batch_no`, `row.serial_and_batch_bundle`). When ERPNext
auto-creates the batch and its Serial and Batch Bundle during submit, that
in-memory row is not refreshed — so the check saw nothing and refused with
"Batch missing" even though the database already held the correct batch.

Your diagnosis was right down to the cause: this was solved correctly in
Phase 2a by `ownership._get_row_batches(doc, row)`, which treats the line's
**Stock Ledger Entries** (by `voucher_type` / `voucher_no` /
`voucher_detail_no`) as authoritative and uses the row's own fields only as a
supplement. Type 1/2 never showed the bug because they use that helper;
`fob_direct.py` reinvented the resolution instead of reusing it.

## The fix

`_row_batch()` is **deleted**, not patched. `fob_direct._handle_intake()` now
imports and calls `ownership._get_row_batches(doc, row)` — the same function
Type 1/2 stamping has relied on since Phase 2a. The multi-batch refusal moved
to the caller, where the result is consumed, and now names the batches it
found:

```python
batches = _get_row_batches(doc, row)
if not batches:        -> "Batch missing"
if len(batches) > 1:   -> "More than one batch" (names them)
batch_no = next(iter(batches))
```

Nothing else references `_row_batch`; there is no circular import
(`ownership.py` does not import `fob_direct`). Type 4 (`production.py`) is
untouched, as you noted — it reconstructs consumption from the Work Order's
own Stock Entries after intake and uses neither helper.

## Regression test — proven to catch the real bug

`test_auto_created_batch_is_found_by_the_shared_resolver` submits a Purchase
Receipt for an item with **Automatically Create New Batch** on, through the
real `on_submit` hook chain, and **supplies no `batch_no` and no bundle** —
pre-filling either would have masked exactly the failure being tested.

I verified the test is not vacuous by running it against the old
implementation first:

```
OLD (in-memory row only):  ERROR test_auto_created_batch_is_found_… -> "Batch missing"
NEW (shared resolver):     Ran 9 tests ... OK
```

The test asserts the whole chain on the auto-created batch: one batch resolved
from the ledger, the concept invoice created against that batch at the Price
List rate, the ownership stamp on it, and — after confirming the invoice — the
restock landing on the same batch and the same storage location.

## Caveats

1. **`_get_row_batches` is a private helper** shared across three modules now
   (`ownership`, `fob_direct`, plus the tests). It is stable and proven, but if
   it grows a third caller with different needs it deserves promotion to a
   public function in a shared module rather than more importing across the
   underscore.
2. **One batch per Type 3 line still stands** — unchanged rule, better message.
3. **Auto-batch items work for Type 3 as of this fix.** Items configured with
   *Automatically Create New Batch* were effectively blocked from Type 3 intake
   before; nothing needs migrating, but any receipts that failed during the
   pilot can simply be re-entered.
