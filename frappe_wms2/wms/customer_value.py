# What is Crings holding, and for whom, and what is it worth.
#
# Dropping per-customer warehouses removed the one thing they gave for free:
# reading a customer's exposure off a warehouse balance. This replaces it, and
# does so more honestly — it reports the REAL current valuation of every batch
# stamped for a customer, grouped by ownership type, with no special-casing.
#
# Zeros are meaningful, not noise: customer-supplied stock and already-invoiced
# FOB-direct stock are genuinely zero-valued in the ledger, while FOB-per-
# production stock and not-yet-invoiced FOB-direct stock carry real cost. The
# report shows exactly that.
#
# Valuation comes from the stock ledger the same way everything else in this
# app reads stock: the latest Stock Ledger Entry per (item, warehouse, batch)
# carries `stock_value_difference`, and summing those per batch gives its
# current value — no separate valuation method is invented here.

import frappe
from frappe import _
from frappe.utils import flt

OWNERSHIP_FIELD = "wms_ownership_type"
CUSTOMER_FIELD = "wms_customer"


@frappe.whitelist()
def get_customer_stock_value(customer=None, company=None, item_code=None):
    """Current stock value held per customer, grouped by ownership type.

    customer: one customer, or None for every customer that holds stock.
    Returns rows: customer, ownership_type, item_code, batch_no, qty, value.
    """
    frappe.has_permission("Batch", throw=True)

    batch_filters = {CUSTOMER_FIELD: ("is", "set")}
    if customer:
        batch_filters[CUSTOMER_FIELD] = customer
    if item_code:
        batch_filters["item"] = item_code

    batches = frappe.get_all(
        "Batch",
        filters=batch_filters,
        fields=["name", "item", CUSTOMER_FIELD, OWNERSHIP_FIELD],
        limit_page_length=0,
    )
    if not batches:
        return []

    stamp = {b.name: b for b in batches}
    balances = _batch_balances(list(stamp), company=company)

    rows = []
    for (batch_no, item, warehouse), figures in balances.items():
        if flt(figures["qty"]) <= 0:
            continue
        info = stamp.get(batch_no)
        rows.append(
            {
                "customer": info.get(CUSTOMER_FIELD),
                "ownership_type": info.get(OWNERSHIP_FIELD),
                "item_code": item,
                "batch_no": batch_no,
                "warehouse": warehouse,
                "qty": flt(figures["qty"]),
                "value": flt(figures["value"]),
            }
        )

    rows.sort(key=lambda r: (r["customer"] or "", r["ownership_type"] or "",
                             r["item_code"], r["batch_no"]))
    return rows


@frappe.whitelist()
def get_customer_stock_summary(customer=None, company=None):
    """The same figures rolled up: per customer, per ownership type."""
    summary = {}
    for row in get_customer_stock_value(customer=customer, company=company):
        key = (row["customer"], row["ownership_type"])
        entry = summary.setdefault(
            key,
            {
                "customer": row["customer"],
                "ownership_type": row["ownership_type"],
                "qty": 0.0,
                "value": 0.0,
                "batches": 0,
            },
        )
        entry["qty"] += flt(row["qty"])
        entry["value"] += flt(row["value"])
        entry["batches"] += 1

    rows = sorted(
        summary.values(), key=lambda r: (r["customer"] or "", r["ownership_type"] or "")
    )

    totals = {}
    for row in rows:
        total = totals.setdefault(
            row["customer"], {"customer": row["customer"], "qty": 0.0, "value": 0.0}
        )
        total["qty"] += row["value"] and row["qty"] or row["qty"]
        total["value"] += row["value"]

    return {"rows": rows, "totals": sorted(totals.values(),
                                           key=lambda t: t["customer"] or "")}


def _batch_balances(batch_names, company=None):
    """qty and value per (batch, item, warehouse), from the stock ledger.

    Batch data lives in the Serial and Batch Bundle on this version, so the
    per-batch quantity is read there; the value comes from the SLE the bundle
    belongs to, apportioned by that batch's share of the entry.
    """
    conditions = ""
    params = {"batches": batch_names}
    if company:
        conditions = " and sle.company = %(company)s"
        params["company"] = company

    rows = frappe.db.sql(
        f"""
        select
            sbe.batch_no      as batch_no,
            sle.item_code     as item_code,
            sle.warehouse     as warehouse,
            sum(sbe.qty)      as qty,
            sum(sbe.stock_value_difference) as value
        from `tabSerial and Batch Entry` sbe
        inner join `tabSerial and Batch Bundle` sbb on sbb.name = sbe.parent
        inner join `tabStock Ledger Entry` sle
            on sle.serial_and_batch_bundle = sbb.name and sle.is_cancelled = 0
        where sbe.batch_no in %(batches)s and sbb.docstatus = 1 {conditions}
        group by sbe.batch_no, sle.item_code, sle.warehouse
        """,
        params,
        as_dict=True,
    )

    balances = {}
    for row in rows:
        balances[(row.batch_no, row.item_code, row.warehouse)] = {
            "qty": flt(row.qty),
            "value": flt(row.value),
        }

    # Legacy rows: batches recorded straight on the SLE without a bundle.
    legacy = frappe.db.sql(
        f"""
        select sle.batch_no, sle.item_code, sle.warehouse,
               sum(sle.actual_qty) as qty,
               sum(sle.stock_value_difference) as value
        from `tabStock Ledger Entry` sle
        where sle.batch_no in %(batches)s and sle.is_cancelled = 0
            and (sle.serial_and_batch_bundle is null
                 or sle.serial_and_batch_bundle = '') {conditions}
        group by sle.batch_no, sle.item_code, sle.warehouse
        """,
        params,
        as_dict=True,
    )
    for row in legacy:
        key = (row.batch_no, row.item_code, row.warehouse)
        entry = balances.setdefault(key, {"qty": 0.0, "value": 0.0})
        entry["qty"] += flt(row.qty)
        entry["value"] += flt(row.value)

    return balances
