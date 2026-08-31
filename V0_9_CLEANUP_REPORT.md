# Legacy warehouse cleanup + Type 2 material routing (frappe_wms2 0.9.0)

**LIVE RESULT.** Full suite on a fresh disposable site (erpnext 16.26.2,
Python 3.14.6): **67 tests, OK** — 9 new tests for these two parts plus all 58
existing ones, so every earlier phase is regression-proven in the same run.
Both parts were additionally verified end to end on a purpose-built
"pilot-like" site, described below.

---

## Part 1 — legacy customer-warehouse cleanup

### Identification is taken from the removed code, not guessed

I recovered the exact convention from the deleted
`customer_warehouse.get_or_create_customer_warehouse()` (v0.7.1 package):

```python
warehouse_name = f"{customer} - {material_label}"     # + " - {abbr}" by ERPNext
```

where `material_label` was the **Item Group** configured for that material at
the time. So a legacy warehouse is one whose `warehouse_name` reads
"*&lt;an existing Customer&gt; - &lt;an existing Item Group&gt;*". Matching on
**both** halves against real records is what keeps a genuine warehouse that
merely contains a dash — or one literally named after a material — out of the
net. Matching against *any* Item Group rather than only the two currently
configured ones is deliberate: the configuration may well have changed since
those warehouses were created.

### What the patch does

`patches/v0_9/cleanup_legacy_customer_warehouses.py` →
`wms/legacy_cleanup.py`:

- **Empty** legacy warehouse → `disabled = 1` and renamed with the
  `[LEGACY-CUSTOMER-WH] ` prefix. **Never deleted** — an ERPNext Warehouse can
  carry accounting history at zero stock.
- **Still holding stock** → untouched, and named explicitly in the patch's log
  output for a human to judge.
- **Nothing found** → says so and exits.
- Emptiness is checked from the Stock Ledger *and* cross-checked against `Bin`.

One guard I added beyond the brief: a warehouse currently referenced by WMS
Settings (`fabrics_warehouse`, `trimmings_warehouse`, `wip_warehouse`) is
**never** touched, even if it matches the pattern. If the owner has already
pointed a setting at a leftover warehouse — the exact mistake this request is
about — disabling it underneath the configuration would break the app rather
than fix it. It stays visible and configured until re-pointed by hand.

### Recurrence guard

All three warehouse Link fields on WMS Settings now filter
`disabled = 0`, so a marked legacy warehouse cannot be selected again — the
same pattern ERPNext uses elsewhere.

### Verified on a pilot-like site

A site with one empty leftover ("test klant - … - WGC") and one still holding
7 units:

```
BEFORE detected as legacy: ['andere klant - … - WGC', 'test klant - … - WGC']
disabled and marked 1 legacy customer warehouse(s): test klant - … - WGC
STILL HOLD STOCK and were left untouched: andere klant - … - WGC

  test klant - … - WGC     disabled=1  name='[LEGACY-CUSTOMER-WH] test klant - …'
  andere klant - … - WGC   disabled=0  name='andere klant - …'

SECOND RUN (idempotency): cleaned again: []          <- nothing left to do
disabled legacy warehouse offered in Link search?  False
```

`bench migrate` runs it cleanly, and running it again afterwards changes
nothing.

---

## Part 2 — Type 2 routes by material

`route_to_customer_warehouse` (label since v0.8: *Route to Material
Warehouse*) is now seeded on **Type 2** as well, and the shared branch in
`ownership.py` — renamed `_validate_fob_row` → `_validate_routed_row`, since
it is no longer FOB-only — resolves the warehouse from the item's material
category via the unchanged `get_warehouse_for_item`.

Patch `v0_9/route_type2_to_material_warehouse.py` backfills the flag on
existing sites through the same idempotent seeding that never overwrites a
user's own edits.

**Only routing changed.** Zero valuation and batch stamping run in the caller,
untouched, and are asserted explicitly in the gate tests below.
`enforce_warehouse` remains on the doctype for user-defined types — no seeded
type uses it any more — and is hidden when the routing flag is on.

### Gate tests (live)

| | |
|---|---|
| C1 | Empty legacy warehouse → disabled and prefixed |
| C2 | Legacy warehouse with stock → untouched and named in the output |
| C3 | Site with none → clean no-op |
| C4 | Second run changes nothing (idempotent) |
| C5 | Configured and unrelated warehouses are never touched |
| T2R1 | A Type 2 Fabrics item lands in the Fabrics warehouse, a Type 2 Trimmings item in the Trimmings warehouse — no longer one shared warehouse |
| T2R2 | Type 2 intake is still **zero-valued** and still stamps Ownership Type + Customer, exactly as before |
| T2R3 | A Type 2 item classified under neither material group is refused, like types 3/4 |
| T2R4 | A pre-filled wrong warehouse on a Type 2 line is corrected silently |

---

## Caveats

1. **Type 2's warehouse changes on existing sites.** Stock already received
   into the old static consignment warehouse stays there; only new receipts
   route by material. If the pilot site has Type 2 stock in the old warehouse,
   it needs moving by hand (or leaving — nothing breaks).
2. **Type 2 now requires material classification.** An item under neither
   configured group can no longer be received as Type 2 at all. That is the
   intended consistency, but it is stricter than before and will surface any
   items that were never classified.
3. **The cleanup is name-based**, because the removed mechanism left no marker
   on the records. A customer renamed since a warehouse was created will not
   match — the warehouse simply stays as it is, visible, rather than being
   touched on a guess.
4. **Warehouses referenced by WMS Settings are skipped** (see above). If the
   owner already configured a leftover, re-point the setting first and run
   `bench --site <site> execute
   frappe_wms2.wms.legacy_cleanup.cleanup_legacy_customer_warehouses`.
5. **Nothing is deleted, ever** — disabled and renamed only, so the action is
   reversible by clearing the flag and the prefix.
6. Shared test fixtures now configure material groups/warehouses for the whole
   suite (defaulting both materials to the existing gate warehouse, so older
   expectations are unchanged); the FOB modules still point them at two
   distinct warehouses themselves.
