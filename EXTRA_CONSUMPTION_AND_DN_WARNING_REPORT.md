# Extra consumption beyond the BOM + Delivery Note cancel warning
**frappe_wms2 0.14.0**

**LIVE RESULT.** Full suite on a fresh disposable site (erpnext 16.26.2,
Python 3.14.6): **101 tests, OK** — 11 new gate tests (8 for Part 1, 3 for
Part 2) plus all 90 existing ones.

Reported together but built independently, as the two documents are.

---

# Part 1 — extra material beyond the BOM

## What was built

**A supplementary Material Request against an existing Work Order**, on top of
the normal one, carrying a mandatory reason:

- `make_supplementary_material_request(work_order, items, reason, comment)` —
  marks the request with `wms_supplementary_for_work_order`, its reason and an
  optional comment (custom fields on Material Request, shipped as fixtures).
- **Reason** reuses the existing `WMS Pick Reason` master, extended with a new
  `Applies to Extra Consumption` category alongside shortage / surplus /
  cancel-return, exactly as Phase 3b's cancel reasons were added. Four reasons
  are seeded (Rework, Spoilage during production, Correction mid-run, Extra
  material agreed with customer); a reason from another category is refused.
- **The guard needs BOTH conditions.** A request is refused only when the Work
  Order is finished (`Completed` / `Closed` / `Stopped` / cancelled) **and**
  everything it consumed is already invoiced with nothing reserved. Either
  condition alone still allows it — there is a later invoicing event to ride
  along with. The refusal names the Work Order and says plainly that there is
  no future invoice event left to attach the material to.

**Invoicing** happens inside the existing `invoice_fob_material_on_delivery()`,
as a second calculation *added alongside* the untouched BOM-ratio one. For each
Work Order shipped on that Delivery Note, supplementary material that was
genuinely consumed (it appears in `get_work_order_consumption()`) and is not
yet invoiced becomes its own line on the same concept invoice, described as
*"Extra material beyond the BOM — MAT-MR-… (reason: Rework, …comment)"* — never
indistinguishable from the standard line.

**Same protection as everything else.** The supplementary amount gets its own
`WMS FOB Invoicing Progress` row (keyed by Work Order + raw batch + the
supplementary request, so it stays identifiable), and rides the v0.13.0
reserved → invoiced → released path unchanged. Discarding a concept invoice
releases the supplementary portion exactly like the standard one. Two details
worth naming: the standard progress lookup now explicitly excludes
supplementary rows, and `WMS FOB Sale` carries the request so a release finds
the right row — without those, the two portions would have drawn down each
other's allowance.

## Gate tests

| | |
|---|---|
| E1 | No supplementary request anywhere → invoicing identical to today (one line, 10, one progress row) |
| E2 | Supplementary request with a reason against an open Work Order succeeds, alongside the normal request |
| E3 | No reason → refused; a reason from another category → refused |
| E4 | Closed **and** fully invoiced (nothing reserved) → refused, naming the Work Order |
| E5 | Closed but with an open reservation → allowed; fully invoiced but still open → allowed. Both conditions really are required together |
| E6 | Consumed extra material appears as a second, labelled line naming the request, its reason and comment, next to the unchanged BOM-ratio line; tracked in its own reserved progress row |
| E7 | Discarding the invoice releases both portions; both are billable again on the next shipment and both settle on submit |
| E8 | Consumed but never shipped again → stays reserved and visible, matching the accepted v0.13.0 behaviour rather than disappearing |

---

# Part 2 — warning when cancelling a Delivery Note

`get_open_concept_invoices(delivery_note)` returns **draft** concept invoices
generated from that Delivery Note, with customer, lines, amount and the
quantity still reserved on their account. A submitted invoice is deliberately
not returned — it is untouched by the cancellation either way.

The client script (`delivery_note_concept_warning.js`, registered via
`doctype_js`) shows this in `before_cancel` using the same dialog style as the
concept-invoice confirm/reject actions. It states what is open, that cancelling
will **not** discard the invoice or release its reservation, that discarding it
yourself is the way to release it (with a direct link), and that continuing to
bill despite the cancellation is a perfectly valid choice.

**It is a warning, not a block.** However the dialog closes, the cancel
proceeds; frappe's own cancel confirmation still stands behind it. No behaviour
changed — the reservation state after cancelling is identical with or without
the dialog.

| | |
|---|---|
| W1 | A draft concept invoice is reported with customer, line (item + qty) and the 8 still reserved |
| W2 | A submitted concept invoice reports nothing — no warning |
| W3 | A Delivery Note with no open concept invoice reports nothing |

---

## Caveats

1. **The supplementary request has no UI entry point yet.** It is a whitelisted
   method; a button on the Work Order form would be the natural place, and is
   a small follow-up if the owner will raise these by hand.
2. **Supplementary quantity is billed against actual consumption**, matched per
   raw-material batch of that Work Order. If the extra material was requested
   but never actually consumed, nothing is billed — the request alone is not
   an invoice trigger.
3. **The guard reads the Work Order's status field**, so a Work Order that is
   effectively finished but still shows an open status will accept a
   supplementary request. That is deliberate (the permissive side of the two
   conditions) but worth knowing.
4. **Part 2 is client-side**, verified by syntax check and by the server-side
   lookup's own tests; no browser was driven.
5. Nothing from the broader architectural review was built, and neither part
   changed the BOM-ratio calculation, the invoicing trigger, or the
   cancel-a-Delivery-Note behaviour.
