# Phase 2a Report — Ownership at Intake (frappe_wms2 0.2.0)

**LIVE RESULT.** Full suite executed on the live bench (frappe 16.25.0 /
erpnext 16.26.2, Python 3.14.6, MariaDB 10.11), on **two** sites — an
established one and a sterile fresh one (migrate → run-tests):

```
bench --site <site> run-tests --app frappe_wms2
Ran 16 tests ... OK      # 5 Phase-0 gates + 5 Storage Location + 6 Phase-2a
```

The Phase 0 gates keep passing alongside Phase 2a (regression-checked in the
same run). Tests are company-agnostic: nothing binds to CRINGS/CB account
names — accounts resolve from company and warehouse defaults, so the same
suite runs unchanged against Crings B.V.

## What was built (and only this)

1. **WMS Ownership Type** — self-managed master (Stock Manager can edit in
   the UI, no developer). Behaviour comes from flags, not hardcoded names:
   `requires_customer`, `zero_valuation_receipt`, `enforce_warehouse`
   (optional routing), `is_active`. Seeded with the four business types;
   **"Own use"** and **"Supplied by customer"** active, the two FOB types
   seeded **inactive** and rejected if used (T4e proves it). Seeding is
   reproducible (after_install + `[post_model_sync]` patch) and never
   overwrites user edits — it only inserts missing rows.

2. **Purchase Receipt, per ITEM LINE** (custom fields via version-controlled
   fixtures, `wms_` prefixed to avoid collisions): `wms_ownership_type`
   (Link, **reqd**) and `wms_customer` (Link Customer). Per-line was chosen
   deliberately: one receipt can mix customers and ownership types. The
   authoritative rule is server-side (runs on save **and** submit, since
   submit re-validates): ownership mandatory on every stock-item line;
   customer mandatory when the type's flag says so; customer must stay
   **empty** for customer-independent types (deliberate strictness — an
   "Own use" batch stamped with a customer would be a contradiction).

3. **Submit behaviour by type** —
   - *Own use*: untouched standard ERPNext receipt. T1 proves at live: SLE
     `stock_value_difference` = qty×rate and the GL debits the warehouse's
     inventory account at cost.
   - *Supplied by customer*: **zero valuation**. The rule is explicit, not
     silent: a non-zero rate on such a line is **rejected** (no hidden
     mutation of what the user typed); the line is received with
     `allow_zero_valuation_rate`. T2 proves at live: qty 8 on the ledger,
     `stock_value_difference` = 0, `valuation_rate` = 0, **zero GL entries**
     — own stock value untouched — and the quantity is tracked per storage
     location through the existing dimension (location balance = 8 on the
     receiving location).
   - Warehouse routing: if `enforce_warehouse` is set on the type, an empty
     line warehouse is auto-filled (in `before_validate`, i.e. before the
     controller's own checks) and a *different* warehouse is rejected. T2
     receives with no warehouse on the line and lands in the consignment
     warehouse.

4. **Batch stamping** — on submit, `Customer` + `Ownership Type` are written
   onto every Batch the line produced. Batches are resolved through the
   **Serial and Batch Bundle** on the line's Stock Ledger Entries (v16:
   `SLE.batch_no` stays empty on submit; the bundle is authoritative), with
   the row-level `batch_no` as fallback. A batch can never carry two owners:
   a receipt of an already-stamped batch for a different customer *or* a
   different ownership type is blocked (pre-submit when the batch is known,
   and again at stamping time); a top-up for the **same** owner is allowed
   (all proven in T5). This is what keeps a physically identical zipper
   separated per customer across receipts.

5. **Storage locations stay customer-neutral** — nothing binds a location to
   a customer; a hygiene test asserts the Storage Location doctype has no
   customer field/link, so this stays true structurally.

6. **Anti-backdoor** (T6, live):
   - Stock Entry **Material Receipt**: same fields (fixtures on Stock Entry
     Detail) + same server rule on rows that receive without a source
     warehouse; batches stamped the same way; zero-valuation enforced.
   - Purchase Invoice with **Update Stock is blocked outright** — intake
     must go through a Purchase Receipt so ownership is captured. (Business
     rule; if standalone stock-updating PIs are ever needed, this hook is
     where the fields would move instead.)

## Paths NOT covered in Phase 2a (documented, not hidden)

- **Stock Reconciliation**: a positive count correction can introduce
  quantity without ownership fields. Locations are still mandatory (Phase 0
  dimension), but ownership is not asked. Treat reconciliations as
  controlled corrections; extend in a later phase if needed.
- **Manufacture / Repack / Subcontracting receipts**: inward legs are not
  ownership-validated yet — deferred to the production phases (types 3/4).
- **Purchase returns** (`is_return` receipts) carry the fields copied from
  the original and are not separately re-validated.
- **Batch stamps survive cancellation** of the receipt that created them
  (deliberate: prevents silent owner reuse; a Stock Manager can clear the
  fields on the Batch manually if a receipt was entered in error — the
  fields are editable for exactly this correction case).

## Version caveats (v16.26.2, verified live)

- `allow_zero_valuation_rate` exists on both Purchase Receipt Item and
  Stock Entry Detail and behaves as expected (zero-value SLE, no GL).
- The client-side `mandatory_depends_on` on the PR customer field references
  the "Own use" name for UX only; renaming is disabled on the master
  (`allow_rename: 0`) and the authoritative rule is flag-driven server-side.
- Field-level `reqd` on `wms_ownership_type` (PR Item) blocks drafts too;
  the server rule independently blocks save/submit, so the protection does
  not depend on the fixture being synced.
- `patches.txt` keeps the explicit empty `[pre_model_sync]` section
  (Python 3.14 strict configparser) — re-verified by the sterile-site
  migrate in this run.

Nothing from later phases was built: no FOB behaviours (types 3/4 are seeded
but inactive and unusable), no pick document, no FIFO, no per-production
invoicing.
