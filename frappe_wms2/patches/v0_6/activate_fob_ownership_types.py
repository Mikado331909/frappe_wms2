# FOB phase: activate ownership types 3 and 4 and backfill their new flags.
#
# Phase 2a deliberately seeded them inactive ("reserved for a later phase").
# This is that phase, so the one-time flip is intended — but only from the
# seeded 0, never over a value a user chose since.
import frappe

from frappe_wms2.install import ensure_ownership_types

FOB_TYPES = ("Purchased with customer", "Purchased for customer")


def execute():
    if not frappe.db.exists("DocType", "WMS Ownership Type"):
        return

    # Seeds + backfills route_to_customer_warehouse / requires_bom.
    ensure_ownership_types()

    for name in FOB_TYPES:
        if not frappe.db.exists("WMS Ownership Type", name):
            continue
        if frappe.db.get_value("WMS Ownership Type", name, "is_active"):
            continue
        frappe.db.set_value("WMS Ownership Type", name, "is_active", 1)
        frappe.clear_document_cache("WMS Ownership Type", name)
