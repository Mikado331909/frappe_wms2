# 0.2 — per-customer, per-material warehouses, auto-provisioned on first use.
#
# One resolver shared by Type 3 and Type 4; never forked per type.
# Idempotent: check-then-create, never duplicates.
#
# Naming: "{customer name} - {material} - {company abbr}", where {material}
# is the NAME OF THE ITEM GROUP the deploying company configured in WMS
# Settings — not a hardcoded "Fabrics"/"Trimmings". A company running this
# app in another language or another catalogue structure gets warehouses
# named after its own groups.

import frappe
from frappe import _

from frappe_wms2.wms.fob import get_item_material


def get_or_create_customer_warehouse(customer, material, company=None, item_code=None):
    """Return the warehouse for (customer, material), creating it on first use.

    `material` is one of the keys from fob.MATERIAL_SETTING ("fabrics" /
    "trimmings") — internal identifiers only; the visible name comes from the
    company's own configured Item Group.
    """
    from frappe_wms2.wms.fob import get_material_item_groups

    groups = get_material_item_groups()
    if material not in groups:
        frappe.throw(_("Unknown material category {0}.").format(material))

    company = company or resolve_company()
    abbr = frappe.get_cached_value("Company", company, "abbr")
    material_label = groups[material]

    warehouse_name = f"{customer} - {material_label}"
    full_name = f"{warehouse_name} - {abbr}"

    if frappe.db.exists("Warehouse", full_name):
        return full_name

    # Someone may have created it with a different suffix handling; match on
    # the fields rather than trusting the name alone.
    existing = frappe.db.get_value(
        "Warehouse",
        {"warehouse_name": warehouse_name, "company": company},
        "name",
    )
    if existing:
        return existing

    doc = frappe.get_doc(
        {
            "doctype": "Warehouse",
            "warehouse_name": warehouse_name,
            "company": company,
            "parent_warehouse": get_parent_warehouse(company),
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return doc.name


def get_warehouse_for_item(customer, item_code, company=None, context=""):
    """Convenience: classify the item, then resolve its customer warehouse."""
    material, _label = get_item_material(item_code, context=context)
    return get_or_create_customer_warehouse(
        customer, material, company=company, item_code=item_code
    )


def get_parent_warehouse(company):
    """Where customer warehouses hang in the tree.

    WMS Settings.customer_warehouse_parent when the company set one (so the
    customer warehouses sit next to its own material warehouses); otherwise
    the company's root group warehouse, which is ERPNext's own default
    parent. Never a hardcoded name.
    """
    settings = frappe.get_cached_doc("WMS Settings")
    parent = settings.get("customer_warehouse_parent")
    if parent and frappe.db.get_value("Warehouse", parent, "company") == company:
        return parent

    root = frappe.db.get_value(
        "Warehouse",
        {"company": company, "is_group": 1, "parent_warehouse": ("in", ["", None])},
        "name",
    )
    return root


def resolve_company():
    from frappe_wms2.wms.doctype.wms_settings.wms_settings import (
        resolve_default_company,
    )

    company = frappe.get_cached_value("WMS Settings", "WMS Settings", "company")
    return company or resolve_default_company()
