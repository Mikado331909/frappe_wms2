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
        "on_submit": [
            "frappe_wms2.wms.ownership.stamp_batches_purchase_receipt",
            # FOB Part 2: Type 3 gets its concept invoice in the same submit.
            "frappe_wms2.wms.fob_direct.create_concept_invoices_purchase_receipt",
        ],
    },
    "Stock Entry": {
        "before_validate": [
            "frappe_wms2.wms.ownership.route_stock_entry",
            "frappe_wms2.wms.production.guard_work_order_batch_setting",
        ],
        "validate": "frappe_wms2.wms.ownership.validate_stock_entry",
        "on_submit": [
            "frappe_wms2.wms.ownership.stamp_batches_stock_entry",
            "frappe_wms2.wms.fob_direct.create_concept_invoices_stock_entry",
            # Part A: each Manufacture booking keeps its own batch; this only
            # makes sure that batch points back at its Work Order.
            "frappe_wms2.wms.production.link_finished_good_batches_to_work_order",
        ],
    },
    # Phase FOB (Part 1): shipping the finished good invoices the customer's
    # own Type 4 raw material consumed by that production run.
    "Delivery Note": {
        "on_submit": "frappe_wms2.wms.production.invoice_fob_material_on_delivery",
    },
    # FOB Part 2: confirming the concept invoice is what triggers the
    # zero-valuation restock — never the intake itself.
    "Sales Invoice": {
        "on_submit": [
            "frappe_wms2.wms.fob_direct.restock_on_invoice_submit",
            # Type 4: reserved -> invoiced only when the invoice is real.
            "frappe_wms2.wms.production.settle_fob_reservations_on_submit",
        ],
        # A discarded concept invoice must remain deletable: the audit row
        # releases its link and records the discard.
        "on_trash": [
            "frappe_wms2.wms.fob_direct.discard_concept_invoice",
            "frappe_wms2.wms.production.release_fob_reservations",
        ],
        "on_cancel": [
            "frappe_wms2.wms.fob_direct.discard_concept_invoice",
            "frappe_wms2.wms.production.release_fob_reservations",
        ],
    },
    "Purchase Invoice": {
        "before_validate": "frappe_wms2.wms.ownership.before_validate_purchase_invoice",
    },
}

# FOB: explicit "you are about to CONFIRM / CANCEL this sale" dialogs on
# concept invoices, so the two opposite actions cannot be confused.
doctype_js = {
    "Sales Invoice": "public/js/fob_sales_invoice.js",
    # Part C: batch traceability button on the Work Order form.
    "Work Order": "public/js/work_order_traceability.js",
    # Warning when cancelling a delivery that still has an open concept
    # invoice — informational only, the behaviour itself is unchanged.
    "Delivery Note": "public/js/delivery_note_concept_warning.js",
}

# Phase 3a: pick list print format is the default for the doctype.
default_print_format_map = {"WMS Pick List": "WMS Pick List"}
