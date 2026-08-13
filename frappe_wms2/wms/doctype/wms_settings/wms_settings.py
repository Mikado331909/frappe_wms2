# WMS Settings — global (Single) configuration for the picking flow.
#
# The WIP fields are company-scoped. The link_filters in the JSON keep the
# dropdowns clean, but a filter is only UX: the rules below are what actually
# prevents wiring the WIP pot to another company's warehouse.
#
# Nothing here matches on a company NAME or abbreviation. The company is
# resolved per site (global default, or the only company that exists), so the
# app deploys unchanged for any customer.

import frappe
from frappe import _
from frappe.model.document import Document


class WMSSettings(Document):
    def onload(self):
        self.set_default_company()

    def before_validate(self):
        self.set_default_company()

    def validate(self):
        self.validate_company_scope()

    # ------------------------------------------------------------ company

    def set_default_company(self):
        if not self.company:
            self.company = resolve_default_company()

    def validate_company_scope(self):
        if not self.company:
            return

        if self.wip_warehouse:
            warehouse_company = frappe.db.get_value(
                "Warehouse", self.wip_warehouse, "company"
            )
            if warehouse_company != self.company:
                frappe.throw(
                    _(
                        "WIP Warehouse {0} belongs to {1}, not to {2}. "
                        "Pick a warehouse of the selected company."
                    ).format(
                        frappe.bold(self.wip_warehouse),
                        frappe.bold(warehouse_company or _("no company")),
                        frappe.bold(self.company),
                    ),
                    title=_("Wrong company"),
                )

        if self.wip_storage_location:
            if not self.wip_warehouse:
                frappe.throw(_("Select the WIP Warehouse first."))
            location_warehouse = frappe.db.get_value(
                "Storage Location", self.wip_storage_location, "warehouse"
            )
            if location_warehouse != self.wip_warehouse:
                frappe.throw(
                    _(
                        "WIP Location {0} sits in warehouse {1}, not in the WIP "
                        "Warehouse {2}."
                    ).format(
                        frappe.bold(self.wip_storage_location),
                        frappe.bold(location_warehouse or _("none")),
                        frappe.bold(self.wip_warehouse),
                    ),
                    title=_("Wrong warehouse"),
                )


def resolve_default_company():
    """The site's company, resolved dynamically — never by name.

    Order: the global default company, then the only company on the site (a
    single-company site needs no configuration at all), else nothing.
    """
    company = frappe.defaults.get_global_default("company")
    if company and frappe.db.exists("Company", company):
        return company

    companies = frappe.get_all("Company", pluck="name", limit=2)
    if len(companies) == 1:
        return companies[0]
    return None


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def storage_location_query(doctype, txt, searchfield, start, page_len, filters):
    """Storage Locations of one company, resolved through their warehouse.

    Storage Location has no company field of its own — it links to a
    Warehouse, and the Warehouse carries the company. A plain link_filter
    cannot hop that relation, so this query does it. Used while no WIP
    warehouse has been chosen yet; afterwards the location list is simply
    filtered to that warehouse.
    """
    company = (filters or {}).get("company") or resolve_default_company()

    return frappe.db.sql(
        """
        select sl.name, sl.warehouse
        from `tabStorage Location` sl
        inner join `tabWarehouse` wh on wh.name = sl.warehouse
        where wh.company = %(company)s
            and sl.name like %(txt)s
        order by sl.name asc
        limit %(start)s, %(page_len)s
        """,
        {
            "company": company,
            "txt": f"%{txt or ''}%",
            "start": start or 0,
            "page_len": page_len or 20,
        },
    )
