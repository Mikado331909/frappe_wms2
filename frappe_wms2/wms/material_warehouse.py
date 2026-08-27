# Where a routed intake line lands.
#
# Replaces the former per-customer warehouse mechanism entirely. All stock —
# every ownership type — is received into the company's OWN material
# warehouses. Ownership is separated by two things that already exist and do
# the job on their own:
#
#   * the batch stamp (Ownership Type + Customer on every batch), and
#   * the one-owner-per-location rule.
#
# A warehouse per customer added nothing on top of those, had to be created
# and maintained, and was the root cause of the "wrong warehouse" bug.
#
# The material classification itself is unchanged: the same configured Item
# Groups and the same parent-chain walk as before. Only the destination
# differs.

import frappe
from frappe import _

from frappe_wms2.wms.fob import FABRICS, TRIMMINGS, get_item_material

MATERIAL_WAREHOUSE_SETTING = {
    FABRICS: "fabrics_warehouse",
    TRIMMINGS: "trimmings_warehouse",
}


def get_material_warehouse(material, company=None):
    """The company's own warehouse for a material category."""
    fieldname = MATERIAL_WAREHOUSE_SETTING.get(material)
    if not fieldname:
        frappe.throw(_("Unknown material category {0}.").format(material))

    settings = frappe.get_cached_doc("WMS Settings")
    warehouse = settings.get(fieldname)
    if not warehouse:
        frappe.throw(
            _(
                "Set the <b>{0}</b> in <b>WMS Settings</b> — stock of this "
                "material category has nowhere to be received."
            ).format(frappe.get_meta("WMS Settings").get_label(fieldname)),
            title=_("WMS Settings incomplete"),
        )

    company = company or settings.get("company")
    if company:
        warehouse_company = frappe.db.get_value("Warehouse", warehouse, "company")
        if warehouse_company != company:
            frappe.throw(
                _(
                    "Warehouse {0} in WMS Settings belongs to {1}, not to {2}."
                ).format(
                    frappe.bold(warehouse),
                    frappe.bold(warehouse_company or _("no company")),
                    frappe.bold(company),
                ),
                title=_("Wrong company"),
            )
    return warehouse


def get_warehouse_for_item(item_code, company=None, context=""):
    """Classify the item, then return the own warehouse for that material."""
    material, _label = get_item_material(item_code, context=context)
    return get_material_warehouse(material, company=company)
