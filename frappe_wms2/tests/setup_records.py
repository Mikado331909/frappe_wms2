# Shared factories + assertion helpers for the Phase 0 gate tests.
#
# Everything is created on the fly on a clean test site. Records with stable
# names (company, warehouse, locations, supplier) are get-or-create; records
# that carry stock state (items, batches) get a unique hash suffix per test so
# tests never contaminate each other.

import functools

import frappe
from frappe.utils import flt, nowdate

from frappe_wms2.tests.site_safety import assert_disposable_site

COMPANY = "WMS2 Gate Company"
ABBR = "WGC"
WAREHOUSE_NAME = "WMS2 Gate WH"
WAREHOUSE = f"{WAREHOUSE_NAME} - {ABBR}"
SUPPLIER = "WMS2 Gate Supplier"
def throwaway_location_code(material="F"):
    """A Storage Location code that cannot collide with real shelves.

    The live site carries 622 real locations, all with real gang letters and
    small niveau numbers. Tests use gang X/Y/Z with a 3-digit niveau (>=700)
    — outside the real code space entirely — plus a random draw, so a
    populated site is never touched.

    No DB lookup on purpose: this is called at import time so the constants
    below are stable for `from ... import L1`. Re-using a code left by an
    earlier run in the same test warehouse is harmless — every test asserts
    on its own freshly created item.
    """
    import random

    return (
        f"{material}{random.choice('XYZ')}{random.randint(700, 999)}"
        f"-{random.randint(100, 999)}"
    )


# Fixed for the lifetime of one test process, random across runs.
L1 = throwaway_location_code()
L2 = throwaway_location_code()


def creates_test_data(func):
    """Every factory that writes to the database goes through this.

    The disposable-site guard used to live only in setup_gate_records(), so
    any test module that called a factory directly (as the Storage Location
    unit tests did) skipped it and silently created the test Company on a
    real site. Enforcing it here makes that impossible by construction: a
    new test file cannot create test data without passing the check, because
    there is no factory that isn't wrapped.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        assert_disposable_site()
        return func(*args, **kwargs)

    return wrapper


@creates_test_data
def setup_gate_records():
    """Create the fixed set of records the gate tests share.

    Guarded: this creates a Company and SUBMITTED stock documents, so it
    refuses to run on anything but an explicitly disposable site.
    """
    from frappe_wms2.install import ensure_storage_location_inventory_dimension

    ensure_storage_location_inventory_dimension()
    ensure_erpnext_masters()

    # Phase 2a: intake needs the ownership types (idempotent seed).
    from frappe_wms2.install import ensure_ownership_types

    ensure_ownership_types()

    _snapshot_global_singles()

    # Global negative stock must be OFF: V4 must be blocked by the DIMENSION
    # check even before the warehouse-level check could fire, and warehouse
    # totals in V4 are deliberately sufficient.
    frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 0)

    # v16: creating a Serial and Batch Bundle from the row-level batch_no
    # field (use_serial_batch_fields) is gated behind this Stock Settings
    # flag — without it V2/V3 fail with "Please check the 'Activate Serial
    # and Batch No for Item' checkbox in Stock Settings".
    frappe.db.set_single_value("Stock Settings", "enable_serial_and_batch_no_for_item", 1)
    frappe.db.set_single_value("Stock Settings", "use_serial_batch_fields", 1)

    # V3 receives the same item on two rows (same batch, two locations);
    # by default ERPNext rejects that with "Same item cannot be entered
    # multiple times."
    frappe.db.set_single_value("Buying Settings", "allow_multiple_items", 1)

    get_or_create_company()
    get_or_create_warehouse()

    # After the company and warehouse exist: since v0.9 every customer-owned
    # type is routed by material category, so the shared fixtures configure
    # one — two Item Groups and, by default, the gate warehouse for both, so
    # expectations written before material routing keep holding.
    ensure_material_config()

    for code in (L1, L2):
        get_or_create_location(code)
    get_or_create_supplier()


@creates_test_data
def ensure_erpnext_masters():
    """A sterile test site (setup wizard never run) has NONE of the standard
    masters a real site always has: Warehouse Type "Transit" (needed by
    Company default-warehouse creation), UOMs, Item/Supplier Group roots,
    Stock Entry Types, Fiscal Year. Seed them with ERPNext's OWN wizard
    fixture installer so the records are identical to a real site's.
    No-op on any site where they already exist."""
    if not (
        frappe.db.exists("Warehouse Type", "Transit")
        and frappe.db.exists("UOM", "Nos")
        and frappe.db.exists("Item Group", "All Item Groups")
        and frappe.db.exists("Supplier Group", "All Supplier Groups")
        and frappe.db.exists("Stock Entry Type", "Material Transfer")
    ):
        from erpnext.setup.setup_wizard.operations.install_fixtures import install

        install(country="Netherlands")

    # GL posting needs a Fiscal Year covering the posting date.
    from frappe.utils import getdate

    today = getdate()
    exists = frappe.db.exists(
        "Fiscal Year",
        {"year_start_date": ("<=", today), "year_end_date": (">=", today)},
    )
    if not exists:
        frappe.get_doc(
            {
                "doctype": "Fiscal Year",
                "year": str(today.year),
                "year_start_date": f"{today.year}-01-01",
                "year_end_date": f"{today.year}-12-31",
            }
        ).insert(ignore_permissions=True)


_SINGLE_SNAPSHOT = {}

_WATCHED_SINGLES = {
    "Stock Settings": (
        "allow_negative_stock",
        "enable_serial_and_batch_no_for_item",
        "use_serial_batch_fields",
        "auto_insert_price_list_rate_if_missing",
    ),
    "WMS Settings": ("wip_warehouse", "wip_storage_location",
                     "allow_customer_neutral_stock"),
}


def _snapshot_global_singles():
    """Remember global Single values so the suite can put them back.

    Singles are shared by the whole site and are NOT scoped per company, so
    a test that writes one would change live configuration. WMS Settings is
    never written at all (tests use frappe.flags.wms2_settings_override);
    this snapshot is the safety net for the Stock Settings flags the intake
    tests genuinely need, and a tripwire if anything else writes a Single.
    """
    if _SINGLE_SNAPSHOT:
        return
    for doctype, fields in _WATCHED_SINGLES.items():
        if not frappe.db.exists("DocType", doctype):
            continue
        _SINGLE_SNAPSHOT[doctype] = {
            field: frappe.db.get_single_value(doctype, field) for field in fields
        }


def restore_global_singles():
    """Put every watched Single back exactly as it was found."""
    for doctype, values in _SINGLE_SNAPSHOT.items():
        for field, value in values.items():
            if frappe.db.get_single_value(doctype, field) != value:
                frappe.db.set_single_value(doctype, field, value)
        frappe.clear_cache(doctype=doctype)


MATERIAL_GROUPS = {"fabrics": "WMS2 Gate Fabrics", "trimmings": "WMS2 Gate Trimmings"}


def material_group(kind="trimmings"):
    """The Item Group the test site treats as that material category."""
    return MATERIAL_GROUPS[kind]


def material_warehouse(kind="trimmings_warehouse"):
    """Whatever WMS Settings currently maps that material to."""
    return frappe.db.get_single_value("WMS Settings", kind)


@creates_test_data
def ensure_material_config(fabrics_warehouse=None, trimmings_warehouse=None):
    """Material Item Groups + the warehouses each maps to.

    Both default to the gate warehouse: tests written before material routing
    existed keep landing exactly where they did. Modules that care about the
    split (the FOB ones) point them at two different warehouses themselves.
    """
    root = (
        frappe.db.get_value("Item Group", {"is_group": 1, "parent_item_group": ""})
        or "All Item Groups"
    )
    for key, name in MATERIAL_GROUPS.items():
        if not frappe.db.exists("Item Group", name):
            frappe.get_doc(
                {
                    "doctype": "Item Group",
                    "item_group_name": name,
                    "parent_item_group": root,
                    "is_group": 1,
                }
            ).insert(ignore_permissions=True)
            frappe.db.commit()

    frappe.db.set_single_value(
        "WMS Settings",
        {
            "fabrics_item_group": MATERIAL_GROUPS["fabrics"],
            "trimmings_item_group": MATERIAL_GROUPS["trimmings"],
            "fabrics_warehouse": fabrics_warehouse or WAREHOUSE,
            "trimmings_warehouse": trimmings_warehouse or WAREHOUSE,
        },
    )
    frappe.clear_cache(doctype="WMS Settings")


@creates_test_data
def get_or_create_company():
    ensure_erpnext_masters()
    if frappe.db.exists("Company", COMPANY):
        return COMPANY

    frappe.get_doc(
        {
            "doctype": "Company",
            "company_name": COMPANY,
            "abbr": ABBR,
            "default_currency": "EUR",
            "country": "Netherlands",
            "create_chart_of_accounts_based_on": "Standard Template",
            "chart_of_accounts": "Standard",
            "enable_perpetual_inventory": 1,
        }
    ).insert(ignore_permissions=True)

    # Perpetual inventory needs a default inventory account. The standard
    # chart creates "Stock In Hand"; wire it up if company setup didn't.
    if not frappe.db.get_value("Company", COMPANY, "default_inventory_account"):
        stock_in_hand = frappe.db.get_value(
            "Account", {"company": COMPANY, "account_name": "Stock In Hand"}
        )
        if stock_in_hand:
            frappe.db.set_value(
                "Company", COMPANY, "default_inventory_account", stock_in_hand
            )
    return COMPANY


@creates_test_data
def get_or_create_warehouse():
    if frappe.db.exists("Warehouse", WAREHOUSE):
        return WAREHOUSE
    frappe.get_doc(
        {
            "doctype": "Warehouse",
            "warehouse_name": WAREHOUSE_NAME,
            "company": COMPANY,
        }
    ).insert(ignore_permissions=True)
    return WAREHOUSE


@creates_test_data
def get_or_create_location(code, warehouse=None):
    if frappe.db.exists("Storage Location", code):
        return code
    frappe.get_doc(
        {
            "doctype": "Storage Location",
            "location_code": code,
            "warehouse": warehouse or WAREHOUSE,
        }
    ).insert(ignore_permissions=True)
    return code


@creates_test_data
def get_or_create_supplier():
    if frappe.db.exists("Supplier", SUPPLIER):
        return SUPPLIER
    frappe.get_doc(
        {
            "doctype": "Supplier",
            "supplier_name": SUPPLIER,
            "supplier_group": "All Supplier Groups",
        }
    ).insert(ignore_permissions=True)
    return SUPPLIER


@creates_test_data
def get_or_create_customer(name):
    if frappe.db.exists("Customer", name):
        return name
    frappe.get_doc(
        {
            "doctype": "Customer",
            "customer_name": name,
            # Customer requires a NON-group Customer Group.
            "customer_group": _leaf_customer_group(),
            "territory": frappe.db.get_value(
                "Territory", {"is_group": 1, "parent_territory": ""}
            )
            or "All Territories",
        }
    ).insert(ignore_permissions=True)
    return name


@creates_test_data
def _leaf_customer_group():
    leaf = frappe.db.get_value("Customer Group", {"is_group": 0})
    if leaf:
        return leaf
    root = (
        frappe.db.get_value(
            "Customer Group", {"is_group": 1, "parent_customer_group": ""}
        )
        or "All Customer Groups"
    )
    doc = frappe.get_doc(
        {
            "doctype": "Customer Group",
            "customer_group_name": "WMS2 Gate Customers",
            "parent_customer_group": root,
            "is_group": 0,
        }
    ).insert(ignore_permissions=True)
    return doc.name


@creates_test_data
def make_item(has_batch_no=False, item_group=None):
    code = f"WMS2-GATE-{frappe.generate_hash(length=8).upper()}"
    frappe.get_doc(
        {
            "doctype": "Item",
            "item_code": code,
            "item_name": code,
            # Under a configured material group by default: types 2/3/4 are
            # all material-routed since v0.9 and refuse an unclassified item.
            "item_group": item_group or MATERIAL_GROUPS["trimmings"],
            "stock_uom": "Nos",
            "is_stock_item": 1,
            "has_batch_no": 1 if has_batch_no else 0,
            "create_new_batch": 0,
            # Explicitly NO auto opening stock: an opening-stock entry would
            # carry no storage_location and be blocked by the Mandatory rule.
            # All stock in these tests enters via vouchers that supply one.
            "opening_stock": 0,
            "valuation_method": "FIFO",
        }
    ).insert(ignore_permissions=True)
    return code


@creates_test_data
def make_batch(item_code):
    batch_id = f"WMS2B-{frappe.generate_hash(length=8).upper()}"
    frappe.get_doc(
        {"doctype": "Batch", "batch_id": batch_id, "item": item_code}
    ).insert(ignore_permissions=True)
    return batch_id


def company_accounts():
    cost_center, adjustment = frappe.db.get_value(
        "Company", COMPANY, ["cost_center", "stock_adjustment_account"]
    )
    if not adjustment:
        adjustment = frappe.db.get_value(
            "Account", {"company": COMPANY, "account_name": "Stock Adjustment"}
        )
    if not cost_center:
        cost_center = frappe.db.get_value(
            "Cost Center", {"company": COMPANY, "is_group": 0}
        )
    return cost_center, adjustment


@creates_test_data
def make_stock_entry(stock_entry_type, rows, do_not_submit=False):
    """rows: list of dicts; dimension fields (storage_location /
    to_storage_location) are passed through verbatim."""
    cost_center, adjustment = company_accounts()
    se = frappe.get_doc(
        {
            "doctype": "Stock Entry",
            "stock_entry_type": stock_entry_type,
            "purpose": stock_entry_type,
            "company": COMPANY,
            "posting_date": nowdate(),
        }
    )
    for row in rows:
        row.setdefault("uom", "Nos")
        row.setdefault("stock_uom", "Nos")
        row.setdefault("conversion_factor", 1)
        row.setdefault("expense_account", adjustment)
        row.setdefault("cost_center", cost_center)
        if stock_entry_type == "Material Receipt":
            # Phase 2a: intake requires an Ownership Type.
            row.setdefault("wms_ownership_type", "Own use")
        se.append("items", row)
    se.insert(ignore_permissions=True)
    if not do_not_submit:
        se.submit()
    return se


@creates_test_data
def zero_valuation_buying_price_list():
    """A dedicated EUR buying price list for the tests.

    Why this exists: on a populated site ERPNext auto-creates an Item Price
    from the first receipt of an item (get_item_details.insert_item_price)
    and then, because `use_serial_batch_fields` forces a rate refresh when a
    batch is set, RE-APPLIES that price to later lines
    (accounts_controller.set_missing_item_details). A customer-supplied line
    entered at 0 would silently come back at the earlier purchase rate. Real
    zero-valuation intake avoids this by not letting a price list feed the
    line; the factory mirrors that.
    """
    name = "WMS2 Test Buying (no prices)"
    if not frappe.db.exists("Price List", name):
        frappe.get_doc(
            {
                "doctype": "Price List",
                "price_list_name": name,
                "buying": 1,
                "enabled": 1,
                "currency": "EUR",
            }
        ).insert(ignore_permissions=True)
        # Committed: documents link to it during the same transaction.
        frappe.db.commit()
    return name


@creates_test_data
def make_purchase_receipt(rows, do_not_submit=False):
    cost_center, _adjustment = company_accounts()

    # Never let ERPNext invent a rate behind the test's back.
    if frappe.db.get_single_value("Stock Settings", "auto_insert_price_list_rate_if_missing"):
        frappe.db.set_single_value(
            "Stock Settings", "auto_insert_price_list_rate_if_missing", 0
        )
        frappe.clear_cache(doctype="Stock Settings")

    pr = frappe.get_doc(
        {
            "doctype": "Purchase Receipt",
            "supplier": SUPPLIER,
            "company": COMPANY,
            "posting_date": nowdate(),
            "currency": "EUR",
            "conversion_rate": 1,
            "buying_price_list": zero_valuation_buying_price_list(),
            "price_list_currency": "EUR",
            "plc_conversion_rate": 1,
            "ignore_pricing_rule": 1,
        }
    )
    for row in rows:
        row.setdefault("warehouse", WAREHOUSE)
        row.setdefault("uom", "Nos")
        row.setdefault("stock_uom", "Nos")
        row.setdefault("conversion_factor", 1)
        row.setdefault("cost_center", cost_center)
        # Phase 2a: intake requires an Ownership Type.
        row.setdefault("wms_ownership_type", "Own use")
        # A line meant to be free of value must stay free of value: pin the
        # price fields so nothing can be fetched into them.
        if not flt(row.get("rate")):
            row["rate"] = 0
            row["price_list_rate"] = 0
            row["discount_percentage"] = 0
            row["discount_amount"] = 0
            row["margin_rate_or_amount"] = 0
            row["last_purchase_rate"] = 0
            row.setdefault("allow_zero_valuation_rate", 1)
        pr.append("items", row)
    pr.insert(ignore_permissions=True)

    # Guard: if this version ever fetches a rate onto a zero line anyway, fail
    # loudly here rather than letting the app rule take the blame.
    for item_row, given in zip(pr.items, rows):
        if not flt(given.get("rate")) and flt(item_row.rate):
            frappe.throw(
                f"Test harness: rate {item_row.rate} was injected onto a "
                f"zero-valuation line for {item_row.item_code}"
            )

    if not do_not_submit:
        pr.submit()
    return pr


# ---------------------------------------------------------------------------
# Assertion helpers (all read ONLY from the native Stock Ledger / GL / Bin —
# there is no parallel stock table to read from, by design)
# ---------------------------------------------------------------------------


def get_sles(voucher):
    return frappe.get_all(
        "Stock Ledger Entry",
        filters={
            "voucher_type": voucher.doctype,
            "voucher_no": voucher.name,
            "is_cancelled": 0,
        },
        fields=[
            "name",
            "item_code",
            "warehouse",
            "actual_qty",
            "storage_location",
            "batch_no",
            "serial_and_batch_bundle",
        ],
        order_by="actual_qty asc",
    )


def sle_batch_qty_map(sle):
    """batch -> qty for one SLE, bundle-aware (v16 stores batches in the
    Serial and Batch Bundle linked from the SLE; SLE.batch_no is legacy)."""
    if sle.serial_and_batch_bundle:
        out = {}
        for row in frappe.get_all(
            "Serial and Batch Entry",
            filters={"parent": sle.serial_and_batch_bundle},
            fields=["batch_no", "qty"],
        ):
            out[row.batch_no] = out.get(row.batch_no, 0) + row.qty
        return out
    if sle.batch_no:
        return {sle.batch_no: sle.actual_qty}
    return {}


def location_balance(item_code, location, warehouse=None):
    """Qty of item in a storage location = sum over the native stock ledger.

    Note: v16 rejects SQL functions as strings in `fields`
    ("sum(actual_qty) as qty"), so fetch the rows and sum in Python.
    """
    qtys = frappe.get_all(
        "Stock Ledger Entry",
        filters={
            "item_code": item_code,
            "warehouse": warehouse or WAREHOUSE,
            "storage_location": location,
            "is_cancelled": 0,
        },
        pluck="actual_qty",
    )
    return flt(sum(qtys))


def batch_location_balance(item_code, batch_no, location, warehouse=None):
    """Qty of one batch in one location, resolved through SLE + bundles."""
    total = 0.0
    for sle in frappe.get_all(
        "Stock Ledger Entry",
        filters={
            "item_code": item_code,
            "warehouse": warehouse or WAREHOUSE,
            "storage_location": location,
            "is_cancelled": 0,
        },
        fields=["name", "actual_qty", "batch_no", "serial_and_batch_bundle"],
    ):
        total += sle_batch_qty_map(sle).get(batch_no, 0)
    return total


def gl_entries(voucher):
    return frappe.get_all(
        "GL Entry",
        filters={"voucher_no": voucher.name, "is_cancelled": 0},
        fields=["account", "debit", "credit"],
    )


def bin_qty(item_code, warehouse=None):
    return (
        frappe.db.get_value(
            "Bin",
            {"item_code": item_code, "warehouse": warehouse or WAREHOUSE},
            "actual_qty",
        )
        or 0
    )
