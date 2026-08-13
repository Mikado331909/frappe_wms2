# Backfill the company scope on the WMS Settings Single.
#
# The company field was added after the WIP fields, so sites installed
# earlier have it empty — and the WIP link filters depend on it. Resolved
# dynamically (global default company, or the only company on the site);
# never by name, so this works for every customer.
import frappe

from frappe_wms2.install import ensure_wms_settings_company


def execute():
    if not frappe.db.exists("DocType", "WMS Settings"):
        return
    ensure_wms_settings_company()
