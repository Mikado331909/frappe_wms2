# Work Order threaded through the pick chain (frappe_wms2 0.11.0)

**LIVE RESULT.** Full suite on a fresh disposable site (erpnext 16.26.2,
Python 3.14.6): **79 tests, OK** — 7 new gate tests plus all 72 existing ones,
so every earlier phase is regression-proven in the same run.

Two findings below need a decision from you: item 5 came back **negative**, and
one gate test as written would have re-broken the Part 0 fix.

---

## Item 5 — verification result: the premise does NOT hold on v16.26.2

Checked directly against the installed source, not assumed:

| | |
|---|---|
| `Material Request Item.work_order` | **does not exist** — the field list has `sales_order`, `sales_order_item`, `production_plan`, `material_request_plan_item`, and no work-order column at all |
| `Material Request.work_order` (header) | **exists** (Link → Work Order, read-only) |
| Native Work Order → Material Request action | **does not exist**; the native direction is Material Request → Work Order (`raise_work_orders`), and the Work Order carries `material_request` / `material_request_item` / `sales_order` |
| Anything in ERPNext that fills `Material Request.work_order` | **nothing** — a grep across the whole app finds no writer. The Job Card → Material Request mapper sets `job_card`, not `work_order` |

So the request's assumption — "`work_order` off Material Request Item, already
present, no new field needed" — is not true on this version, in two ways: the
field is on the **header**, not the row, and **nothing populates it**.

**What I built instead**, as item 5 anticipated ("a small, separate addition"):

- The chain reads `work_order` from the **Material Request header**, which is
  where v16 actually keeps it. No new field on ERPNext doctypes.
- `make_material_request_for_work_order(work_order)` — a whitelisted helper
  that raises the MR for a Work Order's raw material, setting `work_order` on
  the header and `sales_order` on every row (from the Work Order's own Sales
  Order). Without it there is no way to produce such an MR at all, since
  ERPNext offers no such action and the field is read-only in the UI.
- A Work Order with no Sales Order is refused, naming it, rather than guessed
  at.

---

## The second finding: gate test 2 conflicts with the Part 0 fix

The gate list asks to "confirm `get_work_order_consumption()` now correctly
includes this picked quantity". Implementing that literally would have
reintroduced exactly the bug fixed in Part 0.

The material moves twice: the pick takes it **bulk → WIP**, and the Manufacture
booking later takes the **same units WIP → finished good**. Counting both legs
is what produced 40 against a hand-counted 20 in the Part 0 probe. Now that the
pick's Stock Entry carries `work_order`, counting picks as consumption would
double-count again — via a different purpose, but identically.

**So the pick is attributed, not counted.** `work_order` on the pick entry
makes the movement findable per Work Order (traceability, and it is what lets
production draw the right pool from WIP); consumption stays counted once, where
the material is actually embodied in the product. The test asserts this
explicitly rather than hiding it:

```
after the pick        : consumption = 0     (attributed, not consumed)
after the Manufacture : consumption = 10    (counted exactly once)
```

If you *do* want picked-to-WIP material invoiced before it is manufactured,
that is a different design — say so and I will work it through, but it cannot
simply be added on top without re-breaking Part 0.

---

## What was built

1. **`work_order` threaded through**: Material Request header → `WMS Pick Batch`
   MR row (new read-only field, visible in the bundle) → demand rows →
   `WMS Pick List Item.work_order` (new field) → the Stock Entry.
2. **Stock Entries split per Work Order on submit.** The picker still gets ONE
   consolidated list; on submit the lines are grouped by `work_order` and one
   Material Transfer is created per group, each carrying its own Work Order and
   only its own lines. Lines without a Work Order form their own group, exactly
   as before. `stock_entry` still points at the first entry (unchanged for
   plain pick lists); the new `stock_entries` field lists them all.
3. **Customer consistency**: unchanged and still enforced. The existing
   per-line `assert_batch_allowed` check runs at save/submit, so a manual
   override to another customer's batch is refused naming both customers —
   tested specifically as an override, not just via the auto-suggestion.
4. **FIFO suggestion**: unchanged logic, reused as-is. A Work-Order request is
   allocated exactly like a customer-shipment request — same oldest-batch-first
   walk within that customer's stock, same printable list, no special-casing
   visible to the picker.

## Gate tests (live)

| | |
|---|---|
| W1 | A Work-Order request bundles and allocates like any other: same FIFO batch/location, one list, `work_order` on the line |
| W2 | Submitting creates a Stock Entry carrying the Work Order; consumption is 0 from the pick alone and exactly 10 after the Manufacture booking (see the finding above) |
| W3 | Two Work Orders of one customer, one list, one submit → two Stock Entries, each with only its own lines; 10 + 12 = the list's 22 |
| W4 | A mixed list splits into the Work-Order entry and a plain entry with no `work_order`, each with the right lines |
| W5 | Overriding the suggestion to another customer's batch is refused, naming both customers |
| W6 | A Work Order with no Sales Order is refused with a clear message, not guessed at |
| W7 | Regression: a plain pick list still produces exactly one Stock Entry, no `work_order`, same shape as before |

## Caveats

1. **`Material Request.work_order` has to be set by our helper** (or by other
   code) — ERPNext will never fill it, and the field is read-only in the UI. A
   Material Request created any other way carries no Work Order and its lines
   pick as ordinary customer-shipment lines, silently. Worth a UI entry point
   on the Work Order form if the owner will raise these by hand; not built here.
2. **The consumption question above** is the one open decision.
3. **One Work Order per Material Request.** The field is on the header, so a
   single MR cannot span two Work Orders. Two Work Orders = two MRs, bundled
   into one pick list — which is exactly what W3 exercises.
4. **Splitting happens only at submit.** A draft pick list shows all lines
   together, as the picker sees them; nothing indicates the future split beyond
   the `work_order` column on each line.
5. **Cancelling a pick list** is still refused (Phase 3b boundary), and would
   now involve several Stock Entries — worth revisiting if reversal is ever
   extended to Work-Order picks.
