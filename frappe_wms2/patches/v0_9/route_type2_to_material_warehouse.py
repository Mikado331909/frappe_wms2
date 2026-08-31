# Type 2 ("Supplied by customer") moves off the static enforce_warehouse and
# onto the same material-aware routing types 1/3/4 use.
#
# Only the flag is set here; the routing itself is the shared code path.
# Idempotent, and it never overwrites a flag a user has already set.
import frappe

from frappe_wms2.install import ensure_ownership_types


def execute():
    if not frappe.db.exists("DocType", "WMS Ownership Type"):
        return
    ensure_ownership_types()
