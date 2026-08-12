app_name = "frappe_wms2"
app_title = "Frappe WMS2"
app_publisher = "WMS2"
app_description = (
    "Warehouse location tracking on native ERPNext Inventory Dimensions "
    "(Phase 0: Storage Location doctype, dimension registration, gate tests)"
)
app_email = "wms2@example.com"
app_license = "mit"

# ERPNext must be present: the whole design rides on Inventory Dimension.
required_apps = ["frappe/erpnext"]

# Reproducible registration of the "Storage Location" Inventory Dimension.
# - after_install: fresh installs of this app
# - patches.txt (see frappe_wms2/patches/...): existing sites, on migrate
after_install = "frappe_wms2.install.after_install"

# Version-controlled custom fields (Purchase Receipt Item, Batch,
# Stock Entry Detail) — synced on install/migrate.
fixtures = [
    {"dt": "Custom Field", "filters": [["name", "like", "%-wms_%"]]},
    # Phase 3a: the pick list print format (floor paper).
    {"dt": "Print Format", "filters": [["name", "in", ["WMS Pick List"]]]},
    # Self-managed masters are SEEDED (install + patch), not exported as
    # fixtures, so user edits are never overwritten on migrate.
]

# Phase 2a: mandatory ownership at intake + batch stamping + anti-backdoor.
doc_events = {
    "Purchase Receipt": {
        "before_validate": "frappe_wms2.wms.ownership.route_purchase_receipt",
        "validate": "frappe_wms2.wms.ownership.validate_purchase_receipt",
        "on_submit": "frappe_wms2.wms.ownership.stamp_batches_purchase_receipt",
    },
    "Stock Entry": {
        "before_validate": "frappe_wms2.wms.ownership.route_stock_entry",
        "validate": "frappe_wms2.wms.ownership.validate_stock_entry",
        "on_submit": "frappe_wms2.wms.ownership.stamp_batches_stock_entry",
    },
    "Purchase Invoice": {
        "before_validate": "frappe_wms2.wms.ownership.before_validate_purchase_invoice",
    },
}

# Phase 3a: pick list print format is the default for the doctype.
default_print_format_map = {"WMS Pick List": "WMS Pick List"}
