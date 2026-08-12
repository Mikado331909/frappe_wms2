# Phase 2a: seed WMS Ownership Type rows on migrate (idempotent; user edits
# to existing rows are never overwritten). Custom fields arrive via fixture
# sync, which frappe runs after post_model_sync patches.
import frappe

from frappe_wms2.install import ensure_ownership_types


def execute():
    if not frappe.db.exists("DocType", "WMS Ownership Type"):
        # DocType is synced in the model-sync phase before this patch runs;
        # guard anyway for odd migration orders.
        return
    ensure_ownership_types()
