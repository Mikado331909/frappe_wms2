# Warehouse-mismatch bugfix + one-owner-per-location (frappe_wms2 0.7.0)

**LIVE RESULT.** Full suite on a fresh disposable site (erpnext **16.26.2**,
Python 3.14.6): **56 tests, OK** — 8 new tests here plus all 48 existing ones,
so every earlier phase is regression-proven in the same run. `bench migrate`
on an existing site verified separately: the new setting appears with default
`0`.

---

## Item 1 — customer-warehouse mismatch no longer throws

`ownership.py._validate_fob_row()` now always routes the line:

```python
row.set(warehouse_field,
        get_warehouse_for_item(customer, row.item_code, context=prefix))
```

The `current != expected` branch is gone. Your diagnosis was exactly right,
and the failure mode is worth recording because it will recur elsewhere: the
Warehouse is created with `doc.insert()` inside the same request, so when
`frappe.throw()` fired the transaction rolled back **the new Warehouse along
with it** — leaving an error message naming a record that no longer existed
and could not be selected anywhere. Correcting instead of refusing is safe:
there is no legitimate destination for a routed line other than the resolved
customer warehouse, which is precisely why the blank case already did this.

**Regression test** (`test_item1_prefilled_wrong_warehouse_is_corrected_not_refused`)
reproduces the real trigger: it deletes the customer warehouse so the resolver
must create it, submits a Purchase Receipt whose row arrives pre-filled with a
different warehouse (as ERPNext's UI does), then asserts the row was corrected
with no error, that the Stock Ledger Entry landed in the right warehouse — and,
after a commit, that **the warehouse still exists**, i.e. was not rolled back.

---

## Item 2 — one storage location, one owner

### Mechanism

`wms/location_owner.py`, computed live, nothing stored:

- `get_location_stock(location)` — every (item, batch, warehouse) with qty > 0
  at a location, built on `picking.get_stock_by_batch_location`, the same
  ledger read the picking and reversal flows already use, generalised from
  "one batch" to "everything here". It spans **all** warehouses, since one
  location can legitimately sit under more than one.
- `resolve_location_owner(location)` — the single owner, or `None` when free.
- `assert_location_free_for(location, customer, context)` — the rule itself.

Because occupancy is derived, a location frees itself the moment its last unit
leaves. There is no flag to reset and nothing that can drift out of step with
actual stock (test L7 proves it: A's batch consumed to zero, B receives there
immediately, no intervention).

### The rule as built

- **Owner** = the specific Customer for types 2/3/4; the company itself
  (sentinel `__own_stock__`) for type 1 — the latter only when the new toggle
  is OFF.
- **`WMS Settings.allow_own_stock_with_customer_stock`**, default **0**:
  own stock is an owner like any other and conflicts with every customer.
  Set to 1, own stock is skipped entirely — it neither claims a location nor
  is blocked by one.
- **Customer vs. different customer is always refused**, whatever the toggle
  says.
- **Same owner, several batches / items / ownership types is fine** — this is
  the reading you flagged for confirmation, and I built it as written: a Type 2
  and a Type 4 batch of the SAME customer share a location happily (test L6,
  which also adds a Type 3 receipt on top). "One owner", not "one ownership
  type".
- **Hard refuse**: no override, no reason field.
- **A location that already holds two owners** (only possible from before this
  rule) refuses every new receipt with both owners named, and
  `resolve_location_owner()` refuses too rather than picking a side.

### Where it runs

In the **shared** intake path — `_validate_intake_row()` in `ownership.py`,
before the type-specific branches — so it covers all four types including the
already-live Types 1 and 2, on `validate` of Purchase Receipt and of Stock
Entry Material Receipt (using `to_storage_location` there).

### Gate tests (live, all passing)

| | |
|---|---|
| L1 | Type 1 into an empty location succeeds; once emptied, Type 2 for A succeeds there |
| L3 | Toggle OFF: customer-then-own and own-then-customer both refused, each naming who holds it |
| L4 | Toggle ON: own stock does not claim the location, customer and own stock coexist — and a second customer is still refused |
| L5 | Customer B into A's location refused, error names A — identical with the toggle ON and OFF |
| L6 | Type 4 (and then Type 3) for the SAME customer join a Type 2 location; stock verified in the customer warehouse |
| L7 | Blocked while A is there; A consumed to zero; B receives with no manual free-up step |
| L8 | Pre-existing two-owner location (built through a Material Transfer) refuses any new receipt, naming both owners, and refuses to resolve |

---

## Caveats

1. **Intake only.** Material Transfer (relocating already-received stock) and
   Stock Reconciliation are **not** covered — the same backdoors the roadmap
   already lists. A relocation can still create a conflict this check will not
   catch; test L8 uses exactly that route on purpose to build its scenario.
   Worth closing next if the rule matters on the floor.
2. **"Same owner across ownership types" is how I read your point 2** and how
   it is built — flagging it once more since you asked to be told if the
   reading was wrong.
3. **The WIP pot legitimately holds many owners.** It is fed by transfers, not
   receipts, so the rule never fires on it. If anyone ever *receives* directly
   into the WIP location it would be refused — correctly, but the message would
   talk about ownership rather than about WIP.
4. **The Type 3 restock passes by construction** — same batch, same customer,
   same location, so it resolves to the same owner.
5. **Pre-existing conflicts must be resolved by hand.** By design: the system
   names both owners and stops rather than choosing.
6. Existing sites get the toggle via patch `v0_8` at its default (exclusive).
   No data migration is performed and none is needed — occupancy is computed,
   never stored.
