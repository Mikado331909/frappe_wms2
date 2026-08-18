# FOB Part 2: backfill the "Concept Invoice at Intake" flag on the Type 3 row.
# Idempotent; only fills the flag where it is still unset.
import frappe

from frappe_wms2.install import ensure_ownership_types


def execute():
    if not frappe.db.exists("DocType", "WMS Ownership Type"):
        return
    ensure_ownership_types()
