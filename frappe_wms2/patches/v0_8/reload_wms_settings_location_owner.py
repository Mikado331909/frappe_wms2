# One-owner-per-location: make sure the new WMS Settings toggle exists on
# sites that installed before it. Nothing to backfill — the default (0,
# exclusive locations) is what the owner asked for.
import frappe


def execute():
    if not frappe.db.exists("DocType", "WMS Settings"):
        return
    frappe.reload_doc("wms", "doctype", "wms_settings")
