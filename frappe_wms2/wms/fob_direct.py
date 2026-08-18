# Part 2 — Type 3 "Purchased with customer" (FOB direct).
#
# The flow, end to end:
#   1. Intake at real cost into the customer's own warehouse (Part 0 routing).
#   2. In the SAME submit, a DRAFT Sales Invoice with Update Stock, sourced
#      from that warehouse and that exact storage location. Never submitted
#      automatically — the accountant reviews and submits it.
#   3. When the accountant submits it, the invoice takes the stock out (real
#      revenue and COGS), and this module immediately puts the same quantity
#      back at ZERO value, in the same warehouse and the same location.
#
# Net effect: quantity unchanged throughout, value drops from cost to zero
# exactly once, and the books carry a real sale. Nothing physically moves at
# any point — the material sits on its shelf the whole time.
#
# The batch's ownership stamp NEVER changes: it stays Type 3 + its customer
# forever. Whether it has been invoiced is derived from the WMS FOB Sale row
# (a submitted linked Sales Invoice), never stored as a second flag.

import frappe
from frappe import _
from frappe.utils import flt

from frappe_wms2.wms.fob import (
    get_existing_fob_sale,
    resolve_selling_rate,
)

OWNERSHIP_FIELD = "wms_ownership_type"
CUSTOMER_FIELD = "wms_customer"


def is_concept_invoice_type(ownership_type):
    """Type 3 is identified by its FLAG, never by its name."""
    if not ownership_type:
        return False
    return bool(
        frappe.db.get_value(
            "WMS Ownership Type", ownership_type, "create_concept_invoice_at_intake"
        )
    )


# ------------------------------------------------------------- intake hooks


def create_concept_invoices_purchase_receipt(doc, method=None):
    _handle_intake(doc, rows=doc.get("items") or [], warehouse_field="warehouse")


def create_concept_invoices_stock_entry(doc, method=None):
    if doc.purpose != "Material Receipt":
        return
    if doc.get("wms_fob_restock_for"):
        # This IS the restock of an earlier sale — never a new intake.
        return
    _handle_intake(
        doc,
        rows=[r for r in (doc.get("items") or []) if not r.get("s_warehouse")],
        warehouse_field="t_warehouse",
        location_field="to_storage_location",
    )


def _handle_intake(doc, rows, warehouse_field, location_field="storage_location"):
    """One draft Sales Invoice per Type 3 intake row.

    Two phases on purpose: every price is resolved BEFORE anything is
    created, so a missing Item Price cannot leave a half-finished trail of
    invoices and audit rows behind.
    """
    candidates = []
    for row in rows:
        ot_name = row.get(OWNERSHIP_FIELD)
        if not is_concept_invoice_type(ot_name):
            continue
        if get_existing_fob_sale(doc.doctype, doc.name, row.name):
            # Idempotent: this row was already processed (retry after a
            # transient failure). Skip quietly — never a second invoice.
            continue
        candidates.append(row)

    if not candidates:
        return

    # Phase 1 — resolve everything that can refuse.
    prepared = []
    for row in candidates:
        context = _("Row {0}").format(row.idx)
        customer = row.get(CUSTOMER_FIELD)
        warehouse = row.get(warehouse_field)
        location = row.get(location_field)

        if not location:
            frappe.throw(
                _(
                    "{0}: no Storage Location on this line, so the material "
                    "could not be put back in the same place after the sale. "
                    "A location is mandatory at intake."
                ).format(context),
                title=_("Storage Location missing"),
            )

        batch_no = _row_batch(row)
        if not batch_no:
            frappe.throw(
                _(
                    "{0}: Ownership Type {1} needs a batch — the sale and the "
                    "restock both act on one specific batch."
                ).format(context, frappe.bold(row.get(OWNERSHIP_FIELD))),
                title=_("Batch missing"),
            )

        rate, price_list, _currency = resolve_selling_rate(
            customer, row.item_code, company=doc.company, qty=row.qty
        )
        prepared.append(
            frappe._dict(
                row=row,
                customer=customer,
                warehouse=warehouse,
                location=location,
                batch_no=batch_no,
                rate=rate,
                price_list=price_list,
            )
        )

    # Phase 2 — create.
    for item in prepared:
        invoice = _make_concept_invoice(doc, item)
        frappe.get_doc(
            {
                "doctype": "WMS FOB Sale",
                "source_doctype": doc.doctype,
                "source_name": doc.name,
                "source_row": item.row.name,
                "customer": item.customer,
                "company": doc.company,
                "ownership_type": item.row.get(OWNERSHIP_FIELD),
                "item_code": item.row.item_code,
                "batch_no": item.batch_no,
                "qty": flt(item.row.qty),
                "rate": item.rate,
                "price_list": item.price_list,
                "warehouse": item.warehouse,
                "storage_location": item.location,
                "sales_invoice": invoice.name,
            }
        ).insert(ignore_permissions=True)

        frappe.msgprint(
            _(
                "Concept Sales Invoice {0} created for {1} — review and submit "
                "it to complete the sale; the material is then restocked at "
                "zero value."
            ).format(frappe.bold(invoice.name), frappe.bold(item.customer)),
            indicator="blue",
            alert=True,
        )


def _row_batch(row):
    if row.get("batch_no"):
        return row.batch_no
    bundle = row.get("serial_and_batch_bundle")
    if bundle:
        batches = frappe.get_all(
            "Serial and Batch Entry", filters={"parent": bundle}, pluck="batch_no"
        )
        batches = [b for b in set(batches) if b]
        if len(batches) == 1:
            return batches[0]
        if len(batches) > 1:
            frappe.throw(
                _(
                    "This line carries {0} batches. FOB-direct sells one batch "
                    "per line so the restock can put exactly that batch back."
                ).format(len(batches))
            )
    return None


def _make_concept_invoice(doc, item):
    """DRAFT Sales Invoice with Update Stock — no Delivery Note anywhere.

    Nothing physically ships: Update Stock is the mechanism that takes the
    material out of Crings' books when the accountant confirms the sale.
    """
    invoice = frappe.get_doc(
        {
            "doctype": "Sales Invoice",
            "customer": item.customer,
            "company": doc.company,
            "posting_date": doc.posting_date,
            "set_posting_time": 1,
            "currency": frappe.get_cached_value(
                "Price List", item.price_list, "currency"
            ),
            "conversion_rate": 1,
            "update_stock": 1,
            "selling_price_list": item.price_list,
            "wms_fob_source_doctype": doc.doctype,
            "wms_fob_source_name": doc.name,
            "wms_fob_source_row": item.row.name,
            "remarks": _(
                "FOB-direct sale of material received on {0}. Concept — the "
                "material is restocked at zero value when this invoice is "
                "submitted."
            ).format(doc.name),
            "items": [
                {
                    "item_code": item.row.item_code,
                    "qty": flt(item.row.qty),
                    "rate": item.rate,
                    "uom": item.row.get("stock_uom")
                    or frappe.get_cached_value("Item", item.row.item_code, "stock_uom"),
                    "conversion_factor": 1,
                    "warehouse": item.warehouse,
                    "storage_location": item.location,
                    "use_serial_batch_fields": 1,
                    "batch_no": item.batch_no,
                }
            ],
        }
    )
    invoice.flags.ignore_permissions = True
    invoice.insert(ignore_permissions=True)  # DRAFT — never auto-submitted
    return invoice


# ------------------------------------------------- invoice confirmation hook


def restock_on_invoice_submit(doc, method=None):
    """When the accountant submits a FOB-direct concept invoice, put the same
    quantity back at zero value — same warehouse, same location, same batch."""
    sales = frappe.get_all(
        "WMS FOB Sale",
        filters={"sales_invoice": doc.name},
        fields=[
            "name", "item_code", "batch_no", "qty", "customer", "ownership_type",
            "warehouse", "storage_location", "restock_stock_entry", "company",
        ],
    )
    if not sales:
        return

    for sale in sales:
        if sale.restock_stock_entry:
            continue  # already restocked — idempotent
        if not is_concept_invoice_type(sale.ownership_type):
            continue  # Type 4 rows live on the same doctype; they never restock
        if not sale.storage_location:
            frappe.throw(
                _(
                    "FOB sale {0} has no storage location, so the material "
                    "cannot be put back where it came from."
                ).format(sale.name)
            )

        entry = _make_restock_entry(doc, sale)
        frappe.db.set_value(
            "WMS FOB Sale", sale.name, "restock_stock_entry", entry.name
        )


def discard_concept_invoice(doc, method=None):
    """The accountant deleted or cancelled a concept invoice instead of
    submitting it.

    The audit row stays (it is evidence that a concept existed), but its link
    is cleared so the invoice can actually be deleted — a WMS FOB Sale
    pointing at it would otherwise block the deletion outright. The material
    simply remains at cost, un-invoiced, for as long as it takes.
    """
    sales = frappe.get_all(
        "WMS FOB Sale",
        filters={"sales_invoice": doc.name},
        fields=["name", "restock_stock_entry"],
    )
    for sale in sales:
        if sale.restock_stock_entry:
            # Already sold and restocked — that is not a discard, and
            # unwinding it is out of scope (see the report's caveats).
            frappe.throw(
                _(
                    "Sales Invoice {0} has already been completed with restock "
                    "{1}. Reversing a confirmed FOB sale is not supported here."
                ).format(frappe.bold(doc.name), frappe.bold(sale.restock_stock_entry)),
                title=_("Already completed"),
            )
        frappe.db.set_value(
            "WMS FOB Sale",
            sale.name,
            {"sales_invoice": None, "concept_discarded": 1},
        )


def _make_restock_entry(invoice, sale):
    se = frappe.get_doc(
        {
            "doctype": "Stock Entry",
            "stock_entry_type": "Material Receipt",
            "purpose": "Material Receipt",
            "company": sale.company or invoice.company,
            "posting_date": invoice.posting_date,
            "set_posting_time": 1,
            "wms_fob_restock_for": sale.name,
            "remarks": _(
                "Zero-valuation restock after FOB-direct sale {0}. The material "
                "never moved: same warehouse, same location, same batch."
            ).format(invoice.name),
            "items": [
                {
                    "item_code": sale.item_code,
                    "qty": flt(sale.qty),
                    "uom": frappe.get_cached_value("Item", sale.item_code, "stock_uom"),
                    "stock_uom": frappe.get_cached_value(
                        "Item", sale.item_code, "stock_uom"
                    ),
                    "conversion_factor": 1,
                    "t_warehouse": sale.warehouse,
                    "to_storage_location": sale.storage_location,
                    "basic_rate": 0,
                    "allow_zero_valuation_rate": 1,
                    "use_serial_batch_fields": 1,
                    "batch_no": sale.batch_no,
                    # The stamp does not change: same type, same customer.
                    OWNERSHIP_FIELD: sale.ownership_type,
                    CUSTOMER_FIELD: sale.customer,
                }
            ],
        }
    )
    se.flags.ignore_permissions = True
    se.insert(ignore_permissions=True)
    se.submit()
    return se
