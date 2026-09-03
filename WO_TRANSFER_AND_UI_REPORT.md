# Native Work Order transfer bookkeeping + two UI fixes (frappe_wms2 0.12.0)

**LIVE RESULT.** Full suite on a fresh disposable site (erpnext 16.26.2,
Python 3.14.6): **81 tests, OK** — all three items plus every earlier phase in
the same run.

---

## 1. Pick's WIP leg now uses `Material Transfer for Manufacture`

### Verified first, as asked — and there is more to it than the purpose

Read from the v16.26.2 source, not assumed. Two different mechanisms feed the
Work Order form, with **different** requirements:

**Per-item `transferred_qty`** — `Work Order.update_transferred_qty_for_required_items()`
sums Stock Entry Details where:

| condition | |
|---|---|
| `Stock Entry.docstatus` | `= 1` |
| `Stock Entry.work_order` | matches the Work Order |
| `Stock Entry.purpose` | **exactly** `"Material Transfer for Manufacture"` |
| `Stock Entry.is_return` | `= 0` |

No BOM field is required — I checked specifically, since the request asked. But
there **is** a second precondition the request did not mention: the method
returns immediately if **`Work Order.skip_transfer` is set**. A Work Order
created with skip-transfer will never show a transferred quantity no matter
what the pick does. Our own test fixtures had `skip_transfer = 1`, so they were
changed to 0 — which is also the flow this whole item is about.

**Header `material_transferred_for_manufacturing`** — derived separately, and
here v16.26.2 behaves *better* than the source first suggests. The older path
sums `fg_completed_qty` across those entries (which a pick does not set, and
deliberately should not: a pick knows how much material it moved, not how many
finished units that covers). But `recompute_material_transferred_for_manufacturing()`
recomputes it from the **actual item-level transfers**. Measured live: after
picking 10 units of a 2-per-unit requirement, the header field reads **5** —
five finished units' worth — with no `fg_completed_qty` anywhere. So both the
per-item and the header figures come out right without the pick inventing a
finished-goods quantity.

### The change

In `WMS Pick List.make_transfer_to_wip()`, the Work-Order group's Stock Entry
is created with purpose `"Material Transfer for Manufacture"`. Lines with no
Work Order keep the generic `"Material Transfer"`, untouched. Safe precisely
because `CONSUMPTION_PURPOSES` is now `("Manufacture",)`: neither transfer
purpose is counted as consumption, so no double-counting risk exists.

### Gate tests

- W2 now asserts the entry's purpose **and** that ERPNext's own bookkeeping
  moved: `required_items.transferred_qty = 10` and
  `material_transferred_for_manufacturing = 5`, read back off the reloaded
  Work Order — through the native mechanism, no reconciliation by us.
- W7 (regression) unchanged and passing: a plain pick list still produces one
  generic `"Material Transfer"` with no `work_order`.
- `test_p0_only_the_manufacture_leg_counts_as_consumption` **re-ran unmodified
  and passes** — consumption counting is untouched, which was the condition for
  making this change at all.

## 2. Customer now appears on selection, not after save

A whitelisted `get_material_request_context()` exposes the same derivation the
server already runs in `validate` (Sales Order → customer, plus the Work
Order). Choosing a Material Request fills `sales_order` / `customer` /
`work_order` on the row and the header customer immediately, and a mismatch
against an already-set customer raises an alert at that moment rather than at
save. The server-side hard rule is unchanged — this only shows the answer
earlier.

## 3. Duplicate checkbox removed from the item-group dialog

The grid's built-in row-selector column is hidden on that dialog, leaving only
the intentional "Pick now" column. There is no dialog-grid flag for it, so it
is removed after render; scoped to this dialog, every other grid keeps its
selector.

---

## Caveats

1. **`skip_transfer` must be off** on Work Orders that are fed by a pick list,
   or ERPNext will not track the transfer at all — its own guard, not ours.
   Worth checking on the owner's live Work Orders.
2. **The pick's target must be the Work Order's WIP warehouse.** The WMS WIP
   pot (WMS Settings) and `Work Order.wip_warehouse` need to be the same
   warehouse for the native bookkeeping and the later Manufacture booking to
   line up. They are in the tests; verify it on the live site.
3. **`fg_completed_qty` is deliberately not set** by the pick. If a future flow
   ever needs the older `fg_completed_qty`-based progress path, that is a
   separate decision — setting it wrongly would corrupt Work Order progress and
   trigger ERPNext's excess-transfer validation.
4. Items 2 and 3 are client-side only; both were verified by syntax check and
   by the full suite still passing, not by driving a browser.
