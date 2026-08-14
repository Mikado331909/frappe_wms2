# Phase 3b — cancellation and partial return of a submitted pick list.
#
# Both actions are exact reversals: quantity goes from the WIP pot back to the
# batch and storage location it was picked from (never a different one), and
# the ownership/customer stamp travels along automatically because it is the
# same batch.
#
# THE HARD BOUNDARY. A reversal is only allowed while the picked quantity is
# still sitting untouched in the WIP pot. Whether that is the case is DERIVED
# from the stock ledger at the moment the action is requested — there is no
# "is it still there" flag to drift. If anything has already moved on, the
# action is refused and the consuming document is named: a consumed pick is a
# manual stock correction, outside this feature.

import frappe
from frappe import _
from frappe.utils import flt

from frappe_wms2.wms.picking import get_wip_target

# Marks the Stock Entries this module creates, so the consumption check can
# tell "someone used it in production" from "we put it back ourselves".
REVERSAL_PURPOSES = ("cancel", "return")


def get_wip_balance(item_code, batch_no, wip_warehouse, wip_location):
    """Live balance of one (item, batch) in the WIP pot, from the ledger."""
    from frappe_wms2.wms.picking import get_stock_by_batch_location

    total = 0.0
    for row in get_stock_by_batch_location([item_code], warehouse=wip_warehouse):
        if row.batch_no == batch_no and row.storage_location == wip_location:
            total += flt(row.qty)
    return total


def get_outstanding_in_wip(item_code, batch_no, exclude_pick_list=None):
    """How much of this (item, batch) other, still-unreversed pick lists have
    parked in the WIP pot.

    WIP is a shared pot: several pick lists can hold the same batch there. To
    judge whether THIS pick list's quantity is untouched, the quantities that
    belong to other pick lists have to be set aside first.
    """
    rows = frappe.get_all(
        "WMS Pick List Item",
        filters={
            "item_code": item_code,
            "batch_no": batch_no,
            "docstatus": 1,
        },
        fields=["parent", "picked_qty", "returned_qty"],
        limit_page_length=0,
    )
    total = 0.0
    for row in rows:
        if exclude_pick_list and row.parent == exclude_pick_list:
            continue
        total += flt(row.picked_qty) - flt(row.returned_qty)
    return total


def get_reversible_qty(pick_list, row):
    """How much of this line may still be reversed, and why not more.

    Returns (reversible_qty, outstanding_qty, wip_balance, available_for_us).
    """
    wip_warehouse, wip_location = get_wip_target(pick_list.company)

    outstanding = flt(row.picked_qty) - flt(row.returned_qty)
    wip_balance = get_wip_balance(
        row.item_code, row.batch_no, wip_warehouse, wip_location
    )
    others = get_outstanding_in_wip(
        row.item_code, row.batch_no, exclude_pick_list=pick_list.name
    )
    # What is physically left in the pot that can be attributed to us.
    available_for_us = max(0.0, wip_balance - others)
    reversible = min(outstanding, available_for_us)

    return reversible, outstanding, wip_balance, available_for_us


def assert_still_in_wip(pick_list, row, qty):
    """Refuse unless `qty` of this line is still untouched in the WIP pot."""
    reversible, outstanding, wip_balance, available = get_reversible_qty(
        pick_list, row
    )

    if flt(qty) <= flt(reversible) + 0.0000001:
        return reversible

    consumed_by = describe_wip_consumption(
        pick_list, row.item_code, row.batch_no
    )
    frappe.throw(
        _(
            "Row {0}: {1} of {2} (batch {3}) is no longer untouched in the WIP "
            "pot, so it cannot be reversed here.<br><br>"
            "Picked and not yet returned: <b>{4}</b><br>"
            "Still available in WIP for this pick list: <b>{5}</b><br>"
            "{6}<br><br>"
            "Once WIP stock has moved on, putting it back is a manual stock "
            "correction — outside cancel/return."
        ).format(
            row.idx,
            flt(qty),
            row.item_code,
            row.batch_no,
            flt(outstanding),
            flt(available),
            consumed_by,
        ),
        title=_("Already consumed from WIP"),
    )


def describe_wip_consumption(pick_list, item_code, batch_no):
    """Name what took the stock out of WIP, for the refusal message."""
    wip_warehouse, wip_location = get_wip_target(pick_list.company)

    sles = frappe.get_all(
        "Stock Ledger Entry",
        filters={
            "item_code": item_code,
            "warehouse": wip_warehouse,
            "storage_location": wip_location,
            "actual_qty": ("<", 0),
            "is_cancelled": 0,
        },
        fields=["voucher_type", "voucher_no", "actual_qty", "posting_date",
                "serial_and_batch_bundle", "batch_no"],
        order_by="posting_date desc, creation desc",
        limit_page_length=0,
    )

    culprits = []
    for sle in sles:
        qty = _sle_qty_for_batch(sle, batch_no)
        if not qty:
            continue
        if is_reversal_voucher(sle.voucher_no):
            continue  # our own cancel/return, not a consumer
        culprits.append(
            f"{sle.voucher_type} {sle.voucher_no} "
            f"({abs(flt(qty))} on {frappe.utils.formatdate(sle.posting_date)})"
        )
        if len(culprits) >= 5:
            break

    if not culprits:
        return _("No consuming document was found — the WIP balance is simply "
                 "lower than expected; check the stock ledger for this batch.")
    return _("Taken out of WIP by: {0}").format(", ".join(culprits))


def _sle_qty_for_batch(sle, batch_no):
    if sle.serial_and_batch_bundle:
        rows = frappe.get_all(
            "Serial and Batch Entry",
            filters={"parent": sle.serial_and_batch_bundle, "batch_no": batch_no},
            fields=["qty"],
        )
        return sum(flt(r.qty) for r in rows)
    if sle.batch_no == batch_no:
        return flt(sle.actual_qty)
    return 0.0


def is_reversal_voucher(voucher_no):
    """True for Stock Entries created by cancel/return themselves."""
    remarks = frappe.db.get_value("Stock Entry", voucher_no, "remarks") or ""
    return "[WMS-REVERSAL]" in remarks


# ---------------------------------------------------------------- reasons


def validate_reason(reason, action):
    """A reason is mandatory for BOTH actions, always."""
    if not reason:
        frappe.throw(
            _("A reason is required to {0} a pick list.").format(action),
            title=_("Reason required"),
        )
    doc = frappe.get_cached_doc("WMS Pick Reason", reason)
    if not doc.is_active:
        frappe.throw(_("Reason {0} is not active.").format(reason))
    if not doc.applies_to_cancel_return:
        frappe.throw(
            _(
                "Reason {0} is not meant for cancellations or returns. Pick a "
                "reason flagged <b>Applies to Cancel / Return</b>."
            ).format(frappe.bold(reason)),
            title=_("Wrong kind of reason"),
        )
    return doc


# ------------------------------------------------------------- movements


def make_reversal_entry(pick_list, lines, reason, comment, action):
    """One Material Transfer: WIP pot -> the original batch + location.

    `lines` is a list of (row, qty). Nothing else is ever chosen: the target
    warehouse and storage location come from the line itself, so the material
    goes back exactly where it came from.
    """
    wip_warehouse, wip_location = get_wip_target(pick_list.company)

    se = frappe.get_doc(
        {
            "doctype": "Stock Entry",
            "stock_entry_type": "Material Transfer",
            "purpose": "Material Transfer",
            "company": pick_list.company,
            "posting_date": frappe.utils.nowdate(),
            "set_posting_time": 1,
            "remarks": "[WMS-REVERSAL] {0} of pick list {1} — customer {2} — "
                       "reason: {3}{4}".format(
                           action,
                           pick_list.name,
                           pick_list.customer,
                           reason,
                           f" ({comment})" if comment else "",
                       ),
        }
    )

    for row, qty in lines:
        se.append(
            "items",
            {
                "item_code": row.item_code,
                "qty": flt(qty),
                "uom": row.stock_uom,
                "stock_uom": row.stock_uom,
                "conversion_factor": 1,
                # out of the WIP pot...
                "s_warehouse": wip_warehouse,
                "storage_location": wip_location,
                # ...back into the exact origin.
                "t_warehouse": row.warehouse,
                "to_storage_location": row.storage_location,
                "use_serial_batch_fields": 1,
                "batch_no": row.batch_no,
            },
        )

    se.insert(ignore_permissions=True)
    se.submit()
    return se
