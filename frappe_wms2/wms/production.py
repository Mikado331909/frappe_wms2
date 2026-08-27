# 0.5 + Part 1 — production batch identity and Type 4 (FOB per production)
# invoicing.
#
# 0.5: a Work Order's produced batch IS the Work Order. No parallel ID, no
# extra linking table — a Delivery Note that shipped two production runs shows
# batch WO-0001 and batch WO-0002, and that is the whole traceability chain.
#
# Part 1: shipping the finished good is what triggers invoicing of the
# customer's Type 4 raw material. The consumption is reconstructed from the
# Work Order's own Stock Entries at that moment; picking.py is untouched and
# needs no pre-flagging.

import frappe
from frappe import _
from frappe.utils import flt

from frappe_wms2.wms.fob import resolve_selling_rate

OWNERSHIP_FIELD = "wms_ownership_type"
CUSTOMER_FIELD = "wms_customer"

# Purposes whose Stock Entries genuinely CONSUME raw material for a Work
# Order — i.e. the material is embodied in the finished good.
#
# "Material Transfer for Manufacture" is deliberately NOT in this list. It is
# the same physical material at an earlier stage: it leaves the source
# warehouse for Work-In-Progress, and later leaves Work-In-Progress via
# "Manufacture". Counting both negative legs counted every unit twice.
# Verified empirically on 16.26.2 with a hand-counted Work Order: 20 units
# used, both legs summed to 40, the Manufacture leg alone to 20.
CONSUMPTION_PURPOSES = ("Manufacture", "Material Consumption for Manufacture")


# ------------------------------------------------ 0.5 finished-good batches


def name_finished_good_batch(doc, method=None):
    """Name a Work Order's produced batch after the Work Order itself.

    Runs before validation of a Manufacture Stock Entry: for each finished
    item row of a batch-tracked item with no batch chosen yet, the batch is
    the Work Order's name. `Batch.autoname()` uses a supplied `batch_id`
    verbatim (v16.26.2), so no naming-series change is needed.
    """
    if doc.purpose != "Manufacture" or not doc.get("work_order"):
        return

    for row in doc.get("items") or []:
        if not row.get("is_finished_item") or row.get("s_warehouse"):
            continue
        if not frappe.get_cached_value("Item", row.item_code, "has_batch_no"):
            continue
        if row.get("batch_no") or row.get("serial_and_batch_bundle"):
            continue

        row.batch_no = ensure_work_order_batch(doc.work_order, row.item_code)
        row.use_serial_batch_fields = 1


def ensure_work_order_batch(work_order, item_code):
    """The Batch named after the Work Order, created once."""
    if frappe.db.exists("Batch", work_order):
        existing_item = frappe.db.get_value("Batch", work_order, "item")
        if existing_item != item_code:
            frappe.throw(
                _(
                    "Batch {0} already exists for item {1}, so it cannot also "
                    "identify the output of Work Order {0} ({2})."
                ).format(
                    frappe.bold(work_order), frappe.bold(existing_item),
                    frappe.bold(item_code),
                )
            )
        return work_order

    batch = frappe.get_doc(
        {
            "doctype": "Batch",
            "batch_id": work_order,
            "item": item_code,
            "reference_doctype": "Work Order",
            "reference_name": work_order,
        }
    )
    batch.insert(ignore_permissions=True)
    return batch.name


def get_work_order_for_batch(batch_no):
    """The Work Order behind a finished-good batch (0.5: identity, not a link
    table). Falls back to the Batch's reference fields for batches created
    some other way."""
    if not batch_no:
        return None
    if frappe.db.exists("Work Order", batch_no):
        return batch_no
    ref_doctype, ref_name = frappe.db.get_value(
        "Batch", batch_no, ["reference_doctype", "reference_name"]
    ) or (None, None)
    if ref_doctype == "Work Order" and ref_name:
        return ref_name
    return None


# ------------------------------------------- consumption reconstruction


def get_work_order_consumption(work_order):
    """Raw material actually consumed by a Work Order, per (item, batch).

    Read from the real Stock Ledger Entries of the Work Order's own Stock
    Entries. Batch data on v16 lives in the Serial and Batch Bundle, so it is
    resolved through the bundle — SLE.batch_no is only a fallback for legacy
    rows.
    """
    entries = frappe.get_all(
        "Stock Entry",
        filters={
            "work_order": work_order,
            "docstatus": 1,
            "purpose": ("in", CONSUMPTION_PURPOSES),
        },
        pluck="name",
    )
    if not entries:
        return {}

    sles = frappe.get_all(
        "Stock Ledger Entry",
        filters={
            "voucher_type": "Stock Entry",
            "voucher_no": ("in", entries),
            "is_cancelled": 0,
            "actual_qty": ("<", 0),
        },
        fields=["item_code", "actual_qty", "batch_no", "serial_and_batch_bundle",
                "warehouse"],
        limit_page_length=0,
    )

    consumption = {}
    for sle in sles:
        for batch_no, qty in _batches_of(sle).items():
            if not batch_no:
                continue
            key = (sle.item_code, batch_no)
            consumption[key] = consumption.get(key, 0) + abs(flt(qty))
    return consumption


def _batches_of(sle):
    if sle.serial_and_batch_bundle:
        out = {}
        for row in frappe.get_all(
            "Serial and Batch Entry",
            filters={"parent": sle.serial_and_batch_bundle},
            fields=["batch_no", "qty"],
        ):
            out[row.batch_no] = out.get(row.batch_no, 0) + flt(row.qty)
        return out
    if sle.batch_no:
        return {sle.batch_no: flt(sle.actual_qty)}
    return {}


def get_bom_qty_per_unit(work_order, item_code):
    """Raw material quantity per ONE finished-good unit, from the Work Order's
    BOM."""
    bom_no, wo_qty = frappe.db.get_value(
        "Work Order", work_order, ["bom_no", "qty"]
    ) or (None, None)
    if not bom_no:
        return None

    bom_quantity = flt(frappe.db.get_value("BOM", bom_no, "quantity")) or 1
    rows = frappe.get_all(
        "BOM Item",
        filters={"parent": bom_no, "item_code": item_code},
        fields=["stock_qty", "qty"],
    )
    if not rows:
        return None
    total = sum(flt(r.stock_qty) or flt(r.qty) for r in rows)
    return total / bom_quantity


# ---------------------------------------------- Part 1: Delivery Note hook


def invoice_fob_material_on_delivery(doc, method=None):
    """On submit of a Delivery Note: create ONE draft Sales Invoice for the
    customer's Type 4 raw material consumed by the shipped production runs.

    Nothing is auto-submitted: the accountant reviews and submits.
    """
    if doc.get("is_return"):
        return

    lines, progress_updates, audit = [], [], []

    for dn_row in doc.get("items") or []:
        for fg_batch, shipped_qty in _shipped_batches(dn_row).items():
            work_order = get_work_order_for_batch(fg_batch)
            if not work_order:
                continue

            for (item_code, rm_batch), consumed in get_work_order_consumption(
                work_order
            ).items():
                stamp = frappe.db.get_value(
                    "Batch", rm_batch, [OWNERSHIP_FIELD, CUSTOMER_FIELD], as_dict=True
                )
                if not stamp:
                    continue
                # Only THIS customer's Type 4 material. Type 1/2/3 consumed by
                # the same Work Order is never invoiced here.
                if not _is_type4(stamp.get(OWNERSHIP_FIELD)):
                    continue
                if stamp.get(CUSTOMER_FIELD) != doc.customer:
                    continue

                per_unit = get_bom_qty_per_unit(work_order, item_code)
                if per_unit is None:
                    continue

                share = flt(per_unit) * flt(shipped_qty)
                progress = _get_progress(
                    fg_batch, rm_batch, work_order, item_code, consumed, doc, stamp
                )
                remaining = flt(progress.consumed_qty) - flt(progress.invoiced_qty)
                qty = min(share, remaining)
                if qty <= 0:
                    continue

                rate, price_list, _currency = resolve_selling_rate(
                    doc.customer, item_code, company=doc.company, qty=qty
                )

                lines.append(
                    {
                        "item_code": item_code,
                        "qty": qty,
                        "rate": rate,
                        "uom": frappe.get_cached_value("Item", item_code, "stock_uom"),
                        "conversion_factor": 1,
                    }
                )
                progress_updates.append((progress.name, qty))
                audit.append(
                    {
                        "item_code": item_code,
                        "batch_no": rm_batch,
                        "qty": qty,
                        "rate": rate,
                        "price_list": price_list,
                        "finished_good_batch": fg_batch,
                        "work_order": work_order,
                        "ownership_type": stamp.get(OWNERSHIP_FIELD),
                        "source_row": dn_row.name,
                    }
                )

    if not lines:
        # Nothing of this customer's Type 4 material was consumed — silent, by
        # design; a zero-line invoice is simply not created.
        return

    invoice = _make_draft_invoice(doc, lines)

    for progress_name, qty in progress_updates:
        frappe.db.set_value(
            "WMS FOB Invoicing Progress",
            progress_name,
            "invoiced_qty",
            flt(frappe.db.get_value(
                "WMS FOB Invoicing Progress", progress_name, "invoiced_qty"
            )) + flt(qty),
        )

    for row in audit:
        frappe.get_doc(
            dict(
                row,
                doctype="WMS FOB Sale",
                source_doctype="Delivery Note",
                source_name=doc.name,
                customer=doc.customer,
                company=doc.company,
                sales_invoice=invoice.name,
            )
        ).insert(ignore_permissions=True)

    frappe.msgprint(
        _("Concept Sales Invoice {0} created for FOB material — review and submit it.")
        .format(frappe.bold(invoice.name)),
        indicator="blue",
        alert=True,
    )


def _is_type4(ownership_type):
    """Type 4 = the ownership type configured to require a BOM and a customer
    warehouse. Identified by its FLAGS, never by its name."""
    if not ownership_type:
        return False
    row = frappe.db.get_value(
        "WMS Ownership Type",
        ownership_type,
        ["requires_bom", "route_to_customer_warehouse", "zero_valuation_receipt"],
        as_dict=True,
    )
    return bool(row and row.requires_bom and row.route_to_customer_warehouse)


def _shipped_batches(dn_row):
    """batch -> qty for one Delivery Note line (bundle first, field fallback)."""
    if dn_row.get("serial_and_batch_bundle"):
        out = {}
        for row in frappe.get_all(
            "Serial and Batch Entry",
            filters={"parent": dn_row.serial_and_batch_bundle},
            fields=["batch_no", "qty"],
        ):
            out[row.batch_no] = out.get(row.batch_no, 0) + abs(flt(row.qty))
        return out
    if dn_row.get("batch_no"):
        return {dn_row.batch_no: flt(dn_row.qty)}
    return {}


def _get_progress(fg_batch, rm_batch, work_order, item_code, consumed, doc, stamp):
    """The (finished-good batch, raw-material batch) ledger row — created once,
    with the total consumption fixed at creation."""
    name = frappe.db.get_value(
        "WMS FOB Invoicing Progress",
        {"finished_good_batch": fg_batch, "raw_material_batch": rm_batch},
        "name",
    )
    if name:
        return frappe.get_doc("WMS FOB Invoicing Progress", name)

    return frappe.get_doc(
        {
            "doctype": "WMS FOB Invoicing Progress",
            "finished_good_batch": fg_batch,
            "raw_material_batch": rm_batch,
            "work_order": work_order,
            "item_code": item_code,
            "customer": stamp.get(CUSTOMER_FIELD),
            "ownership_type": stamp.get(OWNERSHIP_FIELD),
            "company": doc.company,
            "consumed_qty": consumed,
            "invoiced_qty": 0,
        }
    ).insert(ignore_permissions=True)


def _invoice_line_location(company):
    """The Storage Location dimension is Mandatory on EVERY stock-item line —
    including a Sales Invoice line that does not update stock (a documented
    Phase 2a caveat of applying the dimension to all doctypes). The concept
    invoice therefore carries the WIP pot location: that is where this
    material physically went when it was consumed. It has no stock effect,
    the invoice does not move anything.
    """
    from frappe_wms2.wms.picking import get_wip_target

    try:
        _warehouse, location = get_wip_target(company)
        return location
    except frappe.ValidationError:
        return None


def _make_draft_invoice(doc, lines):
    """One DRAFT invoice per Delivery Note. No stock movement of its own:
    Type 4's raw material left stock at pick time."""
    location = _invoice_line_location(doc.company)
    if location:
        for line in lines:
            line.setdefault("storage_location", location)

    invoice = frappe.get_doc(
        {
            "doctype": "Sales Invoice",
            "customer": doc.customer,
            "company": doc.company,
            "posting_date": doc.posting_date,
            "currency": doc.currency,
            "conversion_rate": doc.conversion_rate or 1,
            "update_stock": 0,
            "wms_fob_source_doctype": "Delivery Note",
            "wms_fob_source_name": doc.name,
            "remarks": _(
                "FOB material consumed for the production shipped on Delivery "
                "Note {0}. Concept — review before submitting."
            ).format(doc.name),
            "items": lines,
        }
    )
    invoice.flags.ignore_permissions = True
    invoice.insert(ignore_permissions=True)  # DRAFT — never auto-submitted
    return invoice
