# Recreate/repair the "Storage Location" Inventory Dimension on migrate.
# Idempotent: ensure_storage_location_inventory_dimension() is safe to re-run.
import frappe

from frappe_wms2.install import ensure_storage_location_inventory_dimension


def execute():
    # required_apps guarantees ERPNext, but guard anyway (patch may run in
    # odd migration orders).
    if not frappe.db.exists("DocType", "Inventory Dimension"):
        return

    # The Storage Location doctype itself is synced in the model-sync phase;
    # this patch is declared [post_model_sync] so it exists by now.
    ensure_storage_location_inventory_dimension()
