# Reproducible registration of the "Storage Location" Inventory Dimension.
#
# Why code and not a fixture: Inventory Dimension is a "smart" doctype whose
# on_update() creates the actual Custom Fields on the stock child tables and on
# Stock Ledger Entry / Stock Closing Balance. Inserting it through the ORM (as
# done here) guarantees those side effects run on every site, every time, in
# the same way. A plain fixture JSON would also work in most cases, but this
# path is explicit, idempotent, and easy to assert in tests.
#
# Configuration (ERPNext v16 semantics, verified against v16.26.2 source):
# - apply_to_all_doctypes = 1
#     ERPNext applies the dimension to every stock-carrying child table it
#     knows about. This is a superset of the four tables Phase 0 requires
#     (Stock Entry Detail, Purchase Receipt Item, Delivery Note Item,
#     Stock Reconciliation Item). There is no native way to apply one
#     dimension to an arbitrary subset of doctypes (apply_to_all_doctypes=0
#     binds a dimension to exactly ONE doctype), so "all" is the correct,
#     supported configuration. Extra fields on e.g. Sales Invoice Item are
#     needed anyway the moment an invoice updates stock.
# - reqd = 1 ("Mandatory")
#     In v16 this is enforced SERVER-SIDE by
#     StockController.validate_inventory_dimension_mandatory(): required on
#     stock-item rows, skipped for service items and on cancel. On Stock Entry
#     Detail the source location is required only when s_warehouse is set and
#     the target location only when t_warehouse is set.
# - validate_negative_stock = 1
#     Enforced in StockLedgerEntry.validate() for every outgoing SLE that
#     carries the dimension: available qty for (item, warehouse, storage
#     location) before the posting datetime must cover the outgoing qty,
#     independent of the global "Allow Negative Stock" setting.

import frappe

DIMENSION_NAME = "Storage Location"
REFERENCE_DOCTYPE = "Storage Location"

# The four child tables Phase 0 cares about. apply_to_all_doctypes covers a
# superset; verify_dimension_fields() asserts these four explicitly.
REQUIRED_CHILD_DOCTYPES = [
    "Stock Entry Detail",
    "Purchase Receipt Item",
    "Delivery Note Item",
    "Stock Reconciliation Item",
]

SOURCE_FIELDNAME = "storage_location"  # scrub(DIMENSION_NAME)
TARGET_FIELDNAME = "storage_location"  # field on Stock Ledger Entry


def after_install():
    ensure_storage_location_inventory_dimension()
    ensure_ownership_types()
    ensure_pick_reasons()
    ensure_wms_settings_company()


# Phase 3a: shared, self-managed reason list (shortage AND surplus).
# Seeded, not fixture-exported: user edits must survive migrate.
PICK_REASON_SEED = [
    {"reason": "Batch empty earlier than expected", "applies_to_shortage": 1,
     "applies_to_surplus": 0,
     "description": "Physical batch ran out before the proposed quantity was reached."},
    {"reason": "Damaged / unusable", "applies_to_shortage": 1,
     "applies_to_surplus": 0,
     "description": "Material present but not usable."},
    {"reason": "Not found on location", "applies_to_shortage": 1,
     "applies_to_surplus": 0,
     "description": "Nothing (or less) found on the stated location."},
    {"reason": "More present than administered", "applies_to_shortage": 0,
     "applies_to_surplus": 1,
     "description": "Physically more available; extra picked."},
    {"reason": "Extra needed for production", "applies_to_shortage": 0,
     "applies_to_surplus": 1,
     "description": "More picked on purpose for the order."},
    {"reason": "Counting difference", "applies_to_shortage": 1,
     "applies_to_surplus": 1,
     "description": "Difference between administration and physical count."},
    # Phase 3b: cancel / return of a submitted pick list.
    {"reason": "Order cancelled", "applies_to_cancel_return": 1,
     "description": "The customer order behind the pick was cancelled."},
    {"reason": "Wrong material picked", "applies_to_cancel_return": 1,
     "description": "Picked material does not match the order; put back."},
    {"reason": "Too much picked", "applies_to_cancel_return": 1,
     "description": "Surplus goes back to its original batch and location."},
    {"reason": "Production not started", "applies_to_cancel_return": 1,
     "description": "Material returned to stock before production used it."},
    {"reason": "Pick list entered in error", "applies_to_cancel_return": 1,
     "description": "The pick list itself was wrong and is reversed in full."},
    # Extra material beyond the BOM, requested deliberately for a Work Order.
    {"reason": "Rework", "applies_to_extra_consumption": 1,
     "description": "Production had to be redone, consuming extra material."},
    {"reason": "Spoilage during production", "applies_to_extra_consumption": 1,
     "description": "Material damaged or lost while producing."},
    {"reason": "Correction mid-run", "applies_to_extra_consumption": 1,
     "description": "A correction during the run needed more material than planned."},
    {"reason": "Extra material agreed with customer", "applies_to_extra_consumption": 1,
     "description": "Additional consumption agreed with the customer up front."},
]


def ensure_wms_settings_company():
    """Fill the company scope on WMS Settings if it is still empty.

    WMS Settings is a global Single; its WIP fields are filtered by this
    company. Resolved dynamically (global default, or the only company on
    the site) — never by name, so the app installs unchanged anywhere.
    """
    if not frappe.db.exists("DocType", "WMS Settings"):
        return
    if frappe.db.get_single_value("WMS Settings", "company"):
        return

    from frappe_wms2.wms.doctype.wms_settings.wms_settings import (
        resolve_default_company,
    )

    company = resolve_default_company()
    if company:
        frappe.db.set_single_value("WMS Settings", "company", company)
        frappe.clear_cache(doctype="WMS Settings")


def ensure_pick_reasons():
    """Insert missing WMS Pick Reason rows. Idempotent; existing rows
    (possibly edited in the UI) are left untouched."""
    if not frappe.db.exists("DocType", "WMS Pick Reason"):
        return
    for seed in PICK_REASON_SEED:
        if frappe.db.exists("WMS Pick Reason", seed["reason"]):
            continue
        row = dict(seed, doctype="WMS Pick Reason", is_active=1)
        # Flags default to 0 unless the seed says otherwise, so a cancel
        # reason never shows up in the shortage picker and vice versa.
        for flag in ("applies_to_shortage", "applies_to_surplus",
                     "applies_to_cancel_return", "applies_to_extra_consumption"):
            row.setdefault(flag, 0)
        frappe.get_doc(row).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# Phase 2a: WMS Ownership Type seed data
# ---------------------------------------------------------------------------
# Four business types; only the first two are active in Phase 2a. Behaviour
# comes from the flags, so the user can adjust them in the UI without a
# developer. Existing records are NEVER overwritten (user edits win) — the
# seed only inserts what is missing.

OWNERSHIP_TYPE_SEED = [
    {
        "ownership_type": "Own use",
        "is_active": 1,
        "requires_customer": 0,
        "zero_valuation_receipt": 0,
        "notes": "Type 1: Crings buys for its own use. Normal receipt at "
        "cost into own warehouse and own inventory account.",
    },
    {
        "ownership_type": "Supplied by customer",
        "is_active": 1,
        "requires_customer": 1,
        "zero_valuation_receipt": 1,
        # Since v0.9: routed to the company's own material warehouse like
        # every other type, instead of one static warehouse for both
        # materials. Valuation and stamping are unchanged.
        "route_to_customer_warehouse": 1,
        "notes": "Type 2 (CMT): the customer sends material. Quantity is "
        "tracked (incl. per storage location) at ZERO value so it never "
        "inflates own stock value.",
    },
    {
        "ownership_type": "Purchased with customer",
        "is_active": 1,
        "requires_customer": 1,
        "zero_valuation_receipt": 0,
        "route_to_customer_warehouse": 1,
        "create_concept_invoice_at_intake": 1,
        "notes": "Type 3 (FOB direct): received at cost into the customer's own "
        "warehouse; a concept Sales Invoice is created at intake and the "
        "material is restocked at zero value when that invoice is submitted.",
    },
    {
        "ownership_type": "Purchased for customer",
        "is_active": 1,
        "requires_customer": 1,
        "zero_valuation_receipt": 0,
        "route_to_customer_warehouse": 1,
        "requires_bom": 1,
        "notes": "Type 4 (FOB per production): reserved for a later phase "
        "(per-production invoicing). Not active in Phase 2a.",
    },
]


def ensure_ownership_types():
    """Insert missing WMS Ownership Type seed rows. Idempotent; existing
    rows (possibly edited by the user in the UI) are left untouched."""
    if not frappe.db.exists("DocType", "WMS Ownership Type"):
        frappe.throw("WMS Ownership Type doctype not synced yet — run migrate.")

    for seed in OWNERSHIP_TYPE_SEED:
        if frappe.db.exists("WMS Ownership Type", seed["ownership_type"]):
            # Self-managed master: a row the user already has is never
            # overwritten. The FOB flags below are the exception ONLY while
            # they are still unset on a row that predates them — a fresh
            # field on an existing row is not a user edit.
            _backfill_new_flags(seed)
            continue
        doc = frappe.get_doc(dict(seed, doctype="WMS Ownership Type"))
        doc.insert(ignore_permissions=True)


# Flags introduced after the row itself existed. Backfilled only where the
# stored value is still the field default (0) AND the seed wants 1.
_BACKFILL_FLAGS = (
    "route_to_customer_warehouse",
    "requires_bom",
    "create_concept_invoice_at_intake",
)


def _backfill_new_flags(seed):
    name = seed["ownership_type"]
    updates = {}
    for flag in _BACKFILL_FLAGS:
        if seed.get(flag) and not frappe.db.get_value("WMS Ownership Type", name, flag):
            updates[flag] = 1
    if updates:
        frappe.db.set_value("WMS Ownership Type", name, updates)
        frappe.clear_document_cache("WMS Ownership Type", name)


def ensure_storage_location_inventory_dimension():
    """Create or update the Inventory Dimension. Safe to run repeatedly."""
    if not frappe.db.exists("DocType", "Inventory Dimension"):
        frappe.throw(
            "ERPNext (Inventory Dimension doctype) is not installed on this site. "
            "Install ERPNext before frappe_wms2."
        )

    # During `install-app` the Storage Location DocType row may not be
    # visible yet when after_install runs (observed live on frappe 16.25 /
    # ERPNext 16.26.2). Reload it explicitly so the Link validation on
    # Inventory Dimension.reference_document can resolve.
    if not frappe.db.exists("DocType", REFERENCE_DOCTYPE):
        frappe.reload_doc("wms", "doctype", "storage_location")
    if not frappe.db.exists("DocType", REFERENCE_DOCTYPE):
        frappe.throw(
            "Storage Location DocType could not be synced; cannot register "
            "the Inventory Dimension."
        )

    name = frappe.db.get_value(
        "Inventory Dimension", {"dimension_name": DIMENSION_NAME}, "name"
    )

    if name:
        dim = frappe.get_doc("Inventory Dimension", name)
        # NOTE: once Stock Ledger Entries exist against the dimension, ERPNext
        # blocks changes to everything except fetch_from_parent,
        # type_of_transaction, condition and validate_negative_stock
        # (DoNotChangeError). We therefore only touch flags that are off.
        changed = False
        for field, value in (
            ("reqd", 1),
            ("validate_negative_stock", 1),
            ("apply_to_all_doctypes", 1),
        ):
            if not dim.get(field) == value:
                dim.set(field, value)
                changed = True
        if changed:
            dim.save(ignore_permissions=True)
        else:
            # Re-run field creation in case custom fields were deleted by hand.
            dim.run_method("on_update")
    else:
        dim = frappe.get_doc(
            {
                "doctype": "Inventory Dimension",
                "dimension_name": DIMENSION_NAME,
                "reference_document": REFERENCE_DOCTYPE,
                "apply_to_all_doctypes": 1,
                "reqd": 1,
                "validate_negative_stock": 1,
            }
        )
        dim.insert(ignore_permissions=True)

    verify_dimension_fields()
    frappe.clear_cache()  # get_inventory_dimensions() is request-cached


def verify_dimension_fields():
    """Assert the custom fields Phase 0 depends on actually exist."""
    missing = []

    for doctype in REQUIRED_CHILD_DOCTYPES:
        if not _field_exists(doctype, SOURCE_FIELDNAME):
            missing.append(f"{doctype}.{SOURCE_FIELDNAME}")

    # Stock Entry gets a second, target-side field (like s_/t_warehouse).
    if not _field_exists("Stock Entry Detail", f"to_{SOURCE_FIELDNAME}"):
        missing.append(f"Stock Entry Detail.to_{SOURCE_FIELDNAME}")

    # The stock ledger itself must carry the dimension.
    if not _field_exists("Stock Ledger Entry", TARGET_FIELDNAME):
        missing.append(f"Stock Ledger Entry.{TARGET_FIELDNAME}")

    if missing:
        frappe.throw(
            "Storage Location Inventory Dimension registration incomplete, "
            "missing fields: " + ", ".join(missing)
        )


def _field_exists(doctype, fieldname) -> bool:
    return bool(
        frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": fieldname})
        or frappe.db.get_value("DocField", {"parent": doctype, "fieldname": fieldname})
    )
