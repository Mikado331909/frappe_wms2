# Disable and mark the warehouses the removed per-customer mechanism created.
# Idempotent: safe to re-run, and a no-op on a site that never had them.
import frappe

from frappe_wms2.wms.legacy_cleanup import cleanup_legacy_customer_warehouses


def execute():
    if not frappe.db.exists("DocType", "Warehouse"):
        return
    cleanup_legacy_customer_warehouses()
