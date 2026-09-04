# Extra-consumption reasons for supplementary Material Requests.
# Idempotent; never overwrites a reason a user has edited.
import frappe

from frappe_wms2.install import ensure_pick_reasons


def execute():
    if not frappe.db.exists("DocType", "WMS Pick Reason"):
        return
    ensure_pick_reasons()
