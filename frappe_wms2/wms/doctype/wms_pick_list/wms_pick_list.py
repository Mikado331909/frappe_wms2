# WMS Pick List — the picking document.
#
# Nothing mutates stock while editing: the whole document is a draft until
# submit. On submit it books, in this order:
#   1. the picked quantities out of the bulk (their batch + storage location,
#      via the Phase 0 dimension) into the WIP pot — a Material Transfer;
#   2. the batch-empty corrections: whatever the admin balance still holds in
#      a (batch, location) flagged empty is written off to 0 with its reason
#      — a Material Issue.
# "Picked = consumed": there is no separate picked-vs-consumed tracking.
#
# Drift on a NON-empty batch is deliberately NOT corrected here — that is a
# cycle count, a later phase.

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from frappe_wms2.wms.picking import (
    assert_batch_allowed,
    get_batch_location_qty,
    get_wip_target,
)


class WMSPickList(Document):
    def validate(self):
        if not self.items:
            frappe.throw(_("A pick list needs at least one line."))
        self.validate_lines()
        self.set_totals()

    def before_submit(self):
        # Re-check against the live balance at submit time, not at generation.
        self.validate_lines(at_submit=True)

    def on_submit(self):
        self.post_stock()

    def before_cancel(self):
        # Deliberately before_cancel, not on_cancel: this hook runs BEFORE the
        # docstatus is written, so a refused cancel leaves the row untouched
        # even if the caller swallows the exception without rolling back.
        self.guard_cancel()

    def on_cancel(self):
        self.guard_cancel()

    def guard_cancel(self):
        # The standard Cancel button must never post an unaudited reversal:
        # it would leave the picked stock sitting in WIP with nothing to say
        # where it went. Cancellation goes through cancel_pick(), which
        # posts the reverse transfer, records the reason, and only then sets
        # this flag.
        if not self.flags.wms2_reversal:
            frappe.throw(
                _(
                    "Use the <b>Cancel Pick List</b> button instead. It moves the "
                    "picked quantities back from WIP to their original batch and "
                    "location, and records the mandatory reason."
                ),
                title=_("Cancel via the button"),
            )

    # ------------------------------------------------------- Phase 3b: cancel

    @frappe.whitelist()
    def cancel_pick(self, reason, comment=None):
        """Full, exact reversal of a submitted pick list.

        Every line's picked quantity goes back from the WIP pot into its own
        batch and storage location. No partial cancel — use return_line() for
        a partial quantity.
        """
        from frappe_wms2.wms.reversal import (
            assert_still_in_wip,
            make_reversal_entry,
            validate_reason,
        )

        self.check_permission("cancel")
        if self.docstatus != 1:
            frappe.throw(_("Only a submitted pick list can be cancelled."))

        validate_reason(reason, _("cancel"))

        lines = []
        for row in self.items:
            outstanding = flt(row.picked_qty) - flt(row.returned_qty)
            if outstanding <= 0:
                continue
            # Hard boundary: refuse unless it is ALL still untouched in WIP.
            assert_still_in_wip(self, row, outstanding)
            lines.append((row, outstanding))

        entry = make_reversal_entry(
            self, lines, reason, comment, action="Cancellation"
        ) if lines else None

        for row, qty in lines:
            row.db_set("returned_qty", flt(row.returned_qty) + flt(qty))

        self.db_set(
            {
                "cancel_reason": reason,
                "cancel_comment": comment,
                "total_returned": sum(
                    flt(r.returned_qty) for r in self.items
                ),
            }
        )

        # Now mark the document itself cancelled.
        self.flags.wms2_reversal = True
        self.cancel()

        self.refresh_bundle()

        return {
            "stock_entry": entry.name if entry else None,
            "reversed_qty": sum(flt(q) for _row, q in lines),
        }

    # ------------------------------------------------------- Phase 3b: return

    @frappe.whitelist()
    def return_line(self, row_name, qty, reason, comment=None):
        """Return a specific quantity of ONE line back to stock.

        Exactly the same reversal as a cancel, only for part of a line and
        repeatable: same batch, same storage location, never a different one.
        """
        from frappe_wms2.wms.reversal import (
            assert_still_in_wip,
            make_reversal_entry,
            validate_reason,
        )

        self.check_permission("submit")
        if self.docstatus != 1:
            frappe.throw(_("Only a submitted pick list can be returned from."))

        qty = flt(qty)
        if qty <= 0:
            frappe.throw(_("Enter a quantity greater than zero."))

        validate_reason(reason, _("return a quantity from"))

        row = next((r for r in self.items if r.name == row_name), None)
        if not row:
            frappe.throw(_("Line {0} is not part of this pick list.").format(row_name))

        outstanding = flt(row.picked_qty) - flt(row.returned_qty)
        if qty > outstanding + 0.0000001:
            frappe.throw(
                _(
                    "Row {0}: cannot return {1}; only {2} of the picked {3} is "
                    "still outstanding."
                ).format(row.idx, qty, outstanding, flt(row.picked_qty)),
                title=_("More than was picked"),
            )

        # Hard boundary: this quantity must still be untouched in WIP.
        assert_still_in_wip(self, row, qty)

        entry = make_reversal_entry(
            self, [(row, qty)], reason, comment, action="Return"
        )

        row.db_set("returned_qty", flt(row.returned_qty) + qty)
        # The line keeps its own reason trail; the last return wins on the
        # header counter.
        self.db_set(
            "total_returned", sum(flt(r.returned_qty) for r in self.items)
        )

        self.refresh_bundle()

        return {"stock_entry": entry.name, "returned_qty": qty}

    def refresh_bundle(self):
        """Reservation follows reality: cancelled/returned quantity becomes
        open demand again on the pick batch."""
        if not self.pick_batch or not frappe.db.exists(
            "WMS Pick Batch", self.pick_batch
        ):
            return
        bundle = frappe.get_doc("WMS Pick Batch", self.pick_batch)
        bundle.build_demand()
        bundle.db_update_all()

    # ----------------------------------------------------------- validation

    def validate_lines(self, at_submit=False):
        for row in self.items:
            context = _("Row {0}: ").format(row.idx)

            # Hard customer separation — also for processor-added lines.
            info = assert_batch_allowed(row.batch_no, self.customer, context)
            row.batch_customer = info.get("wms_customer")

            balance = get_batch_location_qty(
                row.item_code, row.batch_no, row.warehouse, row.storage_location
            )
            if at_submit:
                row.qty_available = balance

            picked = flt(row.picked_qty)
            if picked < 0:
                frappe.throw(_("{0}Picked qty cannot be negative.").format(context))

            if picked > flt(balance) + 0.0000001:
                frappe.throw(
                    _(
                        "{0}Picked qty {1} exceeds the balance {2} of batch {3} "
                        "in {4}. A location can never go negative."
                    ).format(
                        context, picked, flt(balance), row.batch_no,
                        row.storage_location,
                    ),
                    title=_("Not enough stock"),
                )

            remaining = flt(balance) - picked

            # A reason is required when a CORRECTION is being booked:
            # the batch is flagged empty while the administration still
            # holds a rest. If the pick empties the balance exactly, the
            # flag is simply set for information — nothing is corrected,
            # so no reason is demanded for an otherwise perfect pick.
            flag_mismatch = bool(row.batch_empty) and remaining > 0
            if remaining <= 0 and picked > 0 and not row.batch_empty:
                row.batch_empty = 1

            differs = abs(picked - flt(row.qty_to_pick)) > 0.0000001

            if (differs or flag_mismatch) and not row.reason:
                frappe.throw(
                    _(
                        "{0}A reason is mandatory: picked {1} against {2} to "
                        "pick{3}."
                    ).format(
                        context,
                        picked,
                        flt(row.qty_to_pick),
                        _(", and the batch is flagged empty while {0} is "
                          "still administered").format(remaining)
                        if flag_mismatch else "",
                    ),
                    title=_("Reason required"),
                )

            if row.reason:
                reason = frappe.get_cached_doc("WMS Pick Reason", row.reason)
                if not reason.is_active:
                    frappe.throw(
                        _("{0}Reason {1} is not active.").format(context, row.reason)
                    )
                surplus = picked > flt(row.qty_to_pick)
                if surplus and not reason.applies_to_surplus:
                    frappe.throw(
                        _("{0}Reason {1} cannot be used for a surplus.").format(
                            context, row.reason
                        )
                    )
                if not surplus and not reason.applies_to_shortage:
                    frappe.throw(
                        _("{0}Reason {1} cannot be used for a shortage.").format(
                            context, row.reason
                        )
                    )

            # Correction booked on submit when the batch is flagged empty.
            row.correction_qty = remaining if row.batch_empty else 0

    def set_totals(self):
        self.total_to_pick = sum(flt(r.qty_to_pick) for r in self.items)
        self.total_picked = sum(flt(r.picked_qty) for r in self.items)

    # -------------------------------------------------------------- posting

    def post_stock(self):
        wip_warehouse, wip_location = get_wip_target(self.company)

        transfer = self.make_transfer_to_wip(wip_warehouse, wip_location)
        correction = self.make_empty_corrections()

        self.db_set("stock_entry", transfer.name if transfer else None)
        self.db_set(
            "correction_stock_entry", correction.name if correction else None
        )

        # Refresh the bundle's reservation/status view.
        bundle = frappe.get_doc("WMS Pick Batch", self.pick_batch)
        bundle.build_demand()
        bundle.db_update_all()

    def _new_stock_entry(self, purpose):
        return frappe.get_doc(
            {
                "doctype": "Stock Entry",
                "stock_entry_type": purpose,
                "purpose": purpose,
                "company": self.company,
                "posting_date": self.posting_date,
                "set_posting_time": 1,
                "remarks": _("Pick list {0} — customer {1}").format(
                    self.name, self.customer
                ),
            }
        )

    def make_transfer_to_wip(self, wip_warehouse, wip_location):
        rows = [r for r in self.items if flt(r.picked_qty) > 0]
        if not rows:
            return None

        se = self._new_stock_entry("Material Transfer")
        for row in rows:
            se.append(
                "items",
                {
                    "item_code": row.item_code,
                    "qty": flt(row.picked_qty),
                    "uom": row.stock_uom,
                    "stock_uom": row.stock_uom,
                    "conversion_factor": 1,
                    "s_warehouse": row.warehouse,
                    "storage_location": row.storage_location,
                    "t_warehouse": wip_warehouse,
                    "to_storage_location": wip_location,
                    "use_serial_batch_fields": 1,
                    "batch_no": row.batch_no,
                },
            )
        se.insert(ignore_permissions=True)
        se.submit()
        return se

    def make_empty_corrections(self):
        rows = [
            r
            for r in self.items
            if r.batch_empty and flt(r.correction_qty) > 0
        ]
        if not rows:
            return None

        se = self._new_stock_entry("Material Issue")
        expense_account, cost_center = get_correction_accounts(self.company)
        remarks = []
        for row in rows:
            se.append(
                "items",
                {
                    "item_code": row.item_code,
                    "qty": flt(row.correction_qty),
                    "uom": row.stock_uom,
                    "stock_uom": row.stock_uom,
                    "conversion_factor": 1,
                    "s_warehouse": row.warehouse,
                    "storage_location": row.storage_location,
                    "use_serial_batch_fields": 1,
                    "batch_no": row.batch_no,
                    "expense_account": expense_account,
                    "cost_center": cost_center,
                    "allow_zero_valuation_rate": 1,
                },
            )
            remarks.append(
                f"{row.item_code} / {row.batch_no} @ {row.storage_location}: "
                f"{flt(row.correction_qty)} — {row.reason}"
                + (f" ({row.comment})" if row.comment else "")
            )
        se.remarks = _("Batch-empty corrections for pick list {0}: ").format(
            self.name
        ) + "; ".join(remarks)
        se.insert(ignore_permissions=True)
        se.submit()
        return se


def get_correction_accounts(company):
    expense = frappe.db.get_value("Company", company, "stock_adjustment_account")
    if not expense:
        expense = frappe.db.get_value(
            "Account", {"company": company, "account_name": "Stock Adjustment"}
        )
    cost_center = frappe.db.get_value("Company", company, "cost_center")
    if not cost_center:
        cost_center = frappe.db.get_value(
            "Cost Center", {"company": company, "is_group": 0}
        )
    return expense, cost_center


# --------------------------------------------------------------- API used
# by the processor when adding a line for a batch/location that was not
# proposed. Customer separation is enforced here too.


@frappe.whitelist()
def get_pickable_stock(pick_list, item_code):
    doc = frappe.get_doc("WMS Pick List", pick_list)
    doc.check_permission("read")
    from frappe_wms2.wms.picking import get_stock_by_batch_location

    return get_stock_by_batch_location([item_code], customer=doc.customer)


@frappe.whitelist()
def get_wip_provenance(pick_list):
    """What is in the WIP pot for this pick list, and where it came from.
    WIP itself holds no location/quantity tracking — provenance lives here:
    order, customer, batch and the batch's ownership."""
    doc = frappe.get_doc("WMS Pick List", pick_list)
    doc.check_permission("read")

    out = []
    for row in doc.items:
        if flt(row.picked_qty) <= 0:
            continue
        batch = frappe.db.get_value(
            "Batch",
            row.batch_no,
            ["wms_ownership_type", "wms_customer"],
            as_dict=True,
        ) or {}
        out.append(
            {
                "pick_list": doc.name,
                "customer": doc.customer,
                "material_request": row.material_request,
                "sales_order": row.sales_order,
                "item_code": row.item_code,
                "batch_no": row.batch_no,
                "qty": flt(row.picked_qty),
                "from_warehouse": row.warehouse,
                "from_storage_location": row.storage_location,
                "batch_ownership_type": batch.get("wms_ownership_type"),
                "batch_customer": batch.get("wms_customer"),
                "stock_entry": doc.stock_entry,
            }
        )
    return out
