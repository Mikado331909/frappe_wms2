# frappe_wms2 — Phase 0 + Phase 2a + Phase 3a

Warehouse **location** tracking built on native ERPNext v16 mechanisms:

- Physical location = an ERPNext **Inventory Dimension** ("Storage Location").
- Quantity per (item, batch, warehouse, storage location) lives **only** in
  ERPNext's own Stock Ledger. There is **no parallel stock table** — no drift
  by construction.
- Accounting stays on the **warehouse** (Fabrics stock / Trimmings stock);
  the location is a tag underneath with no ledger account of its own.

Phase 0 scope (nothing more): the `Storage Location` doctype, reproducible
dimension registration, and gate tests V1–V4 that prove the native behaviours
the architecture rests on.

## Contents

| Path | What |
|---|---|
| `frappe_wms2/wms/doctype/storage_location/` | Storage Location doctype (code `FA1-2` parsed into material/gang/niveau/plaats; capacity; **no quantity fields**) |
| `frappe_wms2/install.py` | Idempotent registration of the Inventory Dimension (Mandatory + Validate Negative Stock, applied to all stock doctypes incl. Stock Entry Detail, Purchase Receipt Item, Delivery Note Item, Stock Reconciliation Item) |
| `frappe_wms2/patches/v0_1/…` | Same registration on `bench migrate` for existing sites |
| `frappe_wms2/tests/test_gate_phase0.py` | Automated gate tests V0–V4 |
| `docs/gate_test_script.md` | Exact manual (UI) gate script for a clean test site |
| `GATE_REPORT.md` | Phase 0 per-gate verdict for ERPNext v16 (+ version caveats) |
| `frappe_wms2/wms/doctype/wms_ownership_type/` | Self-managed ownership master (Phase 2a) |
| `frappe_wms2/wms/ownership.py` + `fixtures/custom_field.json` | Mandatory intake fields, zero-valuation receipt, batch stamping, anti-backdoor |
| `frappe_wms2/tests/test_ownership_phase2a.py` | Phase 2a live tests |
| `PHASE2A_REPORT.md` | Phase 2a live results + uncovered paths |
| `frappe_wms2/wms/doctype/wms_pick_batch/` | Bundle MRs of ONE customer, summed demand, pick list generation (Phase 3a) |
| `frappe_wms2/wms/doctype/wms_pick_list/` | Pick list: FIFO lines, reasons, submit-only posting to WIP |
| `frappe_wms2/wms/doctype/wms_pick_reason/` | Shared self-managed reason master (shortage + surplus) |
| `frappe_wms2/wms/doctype/wms_settings/` | WIP pot + picking switches |
| `frappe_wms2/wms/picking.py` | Balances per (item, batch, location), customer separation, FIFO |
| `frappe_wms2/fixtures/print_format.json` | Pick list print format (floor paper, no values) |
| `frappe_wms2/tests/test_picking_phase3a.py` | Phase 3a live, collision-safe tests |
| `PHASE3A_REPORT.md` | Phase 3a live results + caveats |

## Install

```bash
bench get-app <this repo>
bench --site <site> install-app frappe_wms2   # registers the dimension
bench --site <site> migrate
```

## Run the tests — on a DISPOSABLE site only

The suite creates a test Company, warehouses, items and submitted stock
documents. It refuses to run unless the site is explicitly marked disposable.

```bash
# from the bench directory: creates a fresh site, runs, drops it again
bash apps/frappe_wms2/scripts/run_tests_disposable.sh
```

Never run it against a production site. If a site was polluted by an earlier
run, clean it with:

```bash
bench --site <site> execute frappe_wms2.tests.site_safety.purge
```

The gate must pass on the actual target ERPNext build before any Phase 1
work starts. If V1, V3 or V4 fails: stop and rethink — do not work around.
