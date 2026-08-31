# One-time cleanup of the warehouses the removed per-customer mechanism
# created (see v0.7.1 `customer_warehouse.get_or_create_customer_warehouse`,
# dropped in v0.8.0).
#
# Those records look like any other Warehouse in a Link search, so it was easy
# to point WMS Settings at one by mistake. This disables and clearly marks the
# empty ones so they cannot be picked again, and deliberately leaves anything
# still holding stock alone for a human to judge.
#
# Nothing is deleted: an ERPNext Warehouse can carry accounting history even at
# zero stock, and a hard delete would break those references.

import frappe
from frappe.utils import flt

LEGACY_PREFIX = "[LEGACY-CUSTOMER-WH] "


def find_legacy_customer_warehouses():
    """Warehouses matching the old naming convention, exactly as it was built.

    The removed function named them:

        warehouse_name = f"{customer} - {material_label}"      # + " - {abbr}"

    where `material_label` was the *Item Group* configured for that material.
    So a legacy warehouse is one whose `warehouse_name` is
    "<an existing Customer> - <an existing Item Group>". Matching on both
    halves keeps a genuine warehouse that merely contains a dash — or one
    literally named after a material — out of the net.
    """
    customers = frappe.get_all("Customer", pluck="name")
    if not customers:
        return []
    item_groups = set(frappe.get_all("Item Group", pluck="name"))
    in_use = _warehouses_in_use()

    found = []
    for wh in frappe.get_all(
        "Warehouse",
        fields=["name", "warehouse_name", "company", "disabled"],
        limit_page_length=0,
    ):
        if wh.name in in_use:
            continue  # never touch a warehouse the settings point at
        if wh.warehouse_name.startswith(LEGACY_PREFIX):
            continue  # already cleaned up
        for customer in customers:
            prefix = f"{customer} - "
            if not wh.warehouse_name.startswith(prefix):
                continue
            remainder = wh.warehouse_name[len(prefix):]
            if remainder in item_groups:
                found.append(frappe._dict(wh, customer=customer, material=remainder))
            break
    return found


def _warehouses_in_use():
    """Warehouses the app is actively configured to use — off limits."""
    if not frappe.db.exists("DocType", "WMS Settings"):
        return set()
    settings = frappe.get_single("WMS Settings")
    return {
        w
        for w in (
            settings.get("fabrics_warehouse"),
            settings.get("trimmings_warehouse"),
            settings.get("wip_warehouse"),
        )
        if w
    }


def warehouse_has_stock(warehouse):
    """True if any item/batch still has a positive balance here."""
    qty = frappe.db.sql(
        """select sum(actual_qty) from `tabStock Ledger Entry`
           where warehouse = %s and is_cancelled = 0""",
        warehouse,
    )[0][0]
    if flt(qty) > 0:
        return True
    # Bin is the cheap cross-check ERPNext itself keeps per (item, warehouse).
    bin_qty = frappe.db.sql(
        """select sum(actual_qty) from `tabBin` where warehouse = %s""", warehouse
    )[0][0]
    return flt(bin_qty) > 0


def cleanup_legacy_customer_warehouses(verbose=True):
    """Disable and mark every EMPTY legacy customer warehouse.

    Idempotent: an already-marked warehouse is skipped, so a second run finds
    nothing left to do. Returns (cleaned, skipped_with_stock).
    """
    cleaned, skipped = [], []

    for wh in find_legacy_customer_warehouses():
        if warehouse_has_stock(wh.name):
            skipped.append(wh.name)
            continue

        new_label = f"{LEGACY_PREFIX}{wh.warehouse_name}"
        frappe.db.set_value(
            "Warehouse",
            wh.name,
            {"warehouse_name": new_label, "disabled": 1},
            update_modified=False,
        )
        frappe.clear_document_cache("Warehouse", wh.name)
        cleaned.append(wh.name)

    if verbose:
        if cleaned:
            print(
                f"frappe_wms2: disabled and marked {len(cleaned)} legacy "
                f"customer warehouse(s): " + ", ".join(cleaned)
            )
        if skipped:
            print(
                "frappe_wms2: the following legacy customer warehouse(s) STILL "
                "HOLD STOCK and were left untouched — decide what to do with "
                "them by hand: " + ", ".join(skipped)
            )
        if not cleaned and not skipped:
            print("frappe_wms2: no legacy customer warehouses found.")

    frappe.db.commit()
    return cleaned, skipped
