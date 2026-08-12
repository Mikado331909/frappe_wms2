# Phase 0 Gate — Manual Test Script (clean test site, UI)

The automated tests (`frappe_wms2/tests/test_gate_phase0.py`) cover V1–V4
end-to-end. Use this script when you want to see the behaviour with your own
eyes in the UI, or if the automated run is not possible in your environment.

Every "Verify" step reads only native ERPNext screens/reports — there is no
app-owned stock table anywhere.

## 0. Setup (once)

1. Clean site with ERPNext v16 installed, then:
   ```
   bench get-app <repo-url-or-path>/frappe_wms2
   bench --site <site> install-app frappe_wms2
   bench --site <site> migrate
   ```
2. Verify registration: open **Inventory Dimension → Storage Location**.
   Expected: Reference Document = Storage Location, *Apply to All Inventory
   Documents*, *Mandatory* and *Validate Negative Stock* all checked.
3. Company: any company with **Enable Perpetual Inventory** ON (needed so V1's
   "no GL entries" is a meaningful check). Note its warehouse, e.g.
   `Stores - XX` (call it **WH**).
4. Stock Settings: **Allow Negative Stock = OFF** (default).
5. Create Storage Locations (Stock → Storage Location → New):
   - `FA1-1`, Warehouse = WH  → saves as name FA1-1; Material/Gang/Niveau/
     Plaats auto-fill to Fabrics / A / 1 / 1.
   - `FA1-2`, Warehouse = WH.
   Also try saving code `XA1-2` → expected: rejected, "Invalid Location Code".
6. Items:
   - `GATE-PLAIN`: stock item, UOM Nos, no batch.
   - `GATE-BATCH`: stock item, UOM Nos, **Has Batch No** ON, *Automatically
     Create New Batch* OFF.
7. Batch: new Batch `GATE-B-001` for item `GATE-BATCH`.
8. A supplier (any).

## V0 — Mandatory location

1. Stock Entry, type **Material Receipt**: item `GATE-PLAIN`, qty 10, target
   warehouse WH, **leave Target Storage Location empty**. Save.
   - **Expected: blocked** — "Target Storage Location is mandatory for the
     Inventory Dimension Storage Location".
2. Set Target Storage Location = FA1-1, Save + Submit. **Expected: succeeds.**

## V1 — Same-warehouse location move is GL-neutral

Precondition: 10 pcs `GATE-PLAIN` in WH @ FA1-1 (from V0 step 2).

1. Stock Entry, type **Material Transfer**, one row:
   - Item `GATE-PLAIN`, qty 4
   - Source Warehouse = WH, Target Warehouse = **WH (same!)**
   - Source Storage Location = FA1-1, Target Storage Location = FA1-2
   - Save + Submit. **Expected: submits** (v16 explicitly allows same
     source/target warehouse for Material Transfer).
2. Verify stock: report **Stock Balance**, filter Item = GATE-PLAIN, add the
   *Storage Location* filter (the report picks the dimension up
   automatically): FA1-1 → 6, FA1-2 → 4. Warehouse row total 10.
3. Verify ledger: report **Stock Ledger**, filter by the Stock Entry number:
   two rows, −4 @ FA1-1 and +4 @ FA1-2, same warehouse, Storage Location
   column filled.
4. Verify **no accounting impact**: on the submitted Stock Entry open
   *View → Accounting Ledger* (or report **General Ledger** filtered by the
   voucher no).
   - **Expected: zero GL Entries.** Any GL entry here = V1 FAILS → stop.

## V2 — Batch + location on one receipt line

1. Purchase Receipt: supplier, one row: item `GATE-BATCH`, qty 10, rate 5,
   warehouse WH, Batch No = GATE-B-001 (tick *Use Serial No / Batch Fields*
   if the batch field is hidden), **Target Storage Location = FA1-1**.
   Submit.
2. Verify: **Stock Ledger** report for this voucher → one row, +10, Batch
   GATE-B-001, Storage Location FA1-1. (In v16 the batch is stored via a
   Serial and Batch Bundle linked on the ledger row; the report resolves it.)
3. Verify: **Stock Balance** with Batch No = GATE-B-001 AND Storage Location
   = FA1-1 → 10. Same filters with FA1-2 → nothing.

## V3 — One batch split over two locations

1. Purchase Receipt: supplier, TWO rows, both `GATE-BATCH`,
   both Batch No = **GATE-B-001**:
   - row 1: qty 6, warehouse WH, Storage Location FA1-1
   - row 2: qty 4, warehouse WH, Storage Location FA1-2
   Submit. **Expected: submits** — same batch in two locations is fine.
2. Verify: Stock Ledger for the voucher → two rows, +6 @ FA1-1 and +4 @
   FA1-2, both batch GATE-B-001.
3. Verify totals match the Bin: Stock Balance without location filter →
   GATE-BATCH in WH = previous 10 (V2) + 10 = 20; with location filter:
   FA1-1 = 16, FA1-2 = 4. Sum of locations == warehouse total, by
   construction (single ledger).

## V4 — Cannot drive a location negative

Precondition: `GATE-PLAIN` has 6 in FA1-1 and 4 in FA1-2 (after V1).
Warehouse total = 10, so only the per-location check can block the following.

1. Stock Entry, type **Material Issue**: item `GATE-PLAIN`, qty **8**,
   source warehouse WH, Source Storage Location = FA1-1. Submit.
   - **Expected: blocked** with title "Inventory Dimension Negative Stock":
     "2.0 units of GATE-PLAIN are required in WH with the inventory
     dimension: storage_location: FA1-1 …". If this submits, V4 FAILS → stop.
2. Positive control: same entry with qty **6** → submits; FA1-1 now 0.
3. Issue qty 1 from FA1-1 (now empty) → **Expected: blocked** again
   ("cannot pick from an empty location").

## Result recording

| Gate | Expected | Pass/Fail | Notes |
|------|----------|-----------|-------|
| V0   | location mandatory on stock lines | | |
| V1   | per-location move, ZERO GL entries | | |
| V2   | one SLE carries batch + location | | |
| V3   | one batch, two locations, totals == Bin | | |
| V4   | over-issue from location blocked | | |
