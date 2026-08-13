# Phase 3a: seed the shared pick reason list on migrate (idempotent; user
# edits to existing rows are never overwritten).
import frappe

from frappe_wms2.install import ensure_pick_reasons, ensure_wms_settings_company


def execute():
    if not frappe.db.exists("DocType", "WMS Pick Reason"):
        return
    ensure_pick_reasons()
    # Backfill the company scope on the WMS Settings Single (added later than
    # the WIP fields, so existing sites have it empty).
    ensure_wms_settings_company()
