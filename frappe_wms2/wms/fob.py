# Shared infrastructure for the FOB ownership types (3 and 4).
#
# Nothing in this module hardcodes a company, a category name, an item group
# name or any other business vocabulary: the deploying company points WMS
# Settings at its own Item Groups, and everything here reads those.

import frappe
from frappe import _
from frappe.utils import flt

FABRICS = "fabrics"
TRIMMINGS = "trimmings"

MATERIAL_SETTING = {
    FABRICS: "fabrics_item_group",
    TRIMMINGS: "trimmings_item_group",
}


# ------------------------------------------------------- 0.1 valuation guard


def assert_batchwise_valuation(item_code, context=""):
    """Refuse FOB intake for an item whose batches would NOT get batch-wise
    valuation.

    Why this is per-item and not a single global check: mirrored exactly from
    `erpnext.stock.doctype.batch.batch.Batch.set_batchwise_valuation()` in
    v16.26.2, a batch is stamped `use_batchwise_valuation = 0` only when

        get_valuation_method(item) == "Moving Average"
        AND Stock Settings.do_not_use_batchwise_valuation

    Everything else gets 1. So a FIFO item is safe regardless of the global
    flag, and only that exact combination is dangerous — a customer warehouse
    holding a cost-valued and a zero-valued batch of the same item would
    otherwise share one moving-average rate and silently corrupt both.

    Note the fallback chain: ERPNext calls get_valuation_method() WITHOUT a
    company here, so an item with no valuation_method falls back to
    Stock Settings.valuation_method (default "FIFO"), NOT to the Company
    default. This function mirrors that, deliberately.
    """
    if not uses_batchwise_valuation(item_code):
        frappe.throw(
            _(
                "{0}Item {1} is valued <b>Moving Average</b> while Stock Settings "
                "has <b>Do not use Batch-wise Valuation</b> enabled. FOB stock "
                "cannot be received for this item: a customer warehouse holds "
                "cost-valued and zero-valued batches of the same item side by "
                "side, and without batch-wise valuation they would corrupt each "
                "other's rate.<br><br>Fix the item's valuation method or the "
                "Stock Settings flag before using this ownership type."
            ).format(context, frappe.bold(item_code)),
            title=_("Batch-wise valuation required"),
        )


def uses_batchwise_valuation(item_code):
    """True when a NEW batch of this item would get use_batchwise_valuation=1."""
    from erpnext.stock.utils import get_valuation_method

    if get_valuation_method(item_code) != "Moving Average":
        return True
    return not frappe.db.get_single_value(
        "Stock Settings", "do_not_use_batchwise_valuation"
    )


# --------------------------------------------- 0.2 material classification


def get_material_item_groups():
    """The two Item Groups the deploying company designated, from WMS Settings.

    Returns {"fabrics": <group>, "trimmings": <group>}. Both must be set —
    FOB intake refuses rather than guessing a side.
    """
    settings = frappe.get_cached_doc("WMS Settings")
    groups = {}
    missing = []
    for material, fieldname in MATERIAL_SETTING.items():
        value = settings.get(fieldname)
        if not value:
            missing.append(frappe.get_meta("WMS Settings").get_label(fieldname))
        groups[material] = value

    if missing:
        frappe.throw(
            _(
                "Set {0} in <b>WMS Settings</b> first. FOB ownership types need "
                "to know which Item Group represents each material category in "
                "your own item catalogue."
            ).format(", ".join(f"<b>{m}</b>" for m in missing)),
            title=_("WMS Settings incomplete"),
        )
    return groups


def get_item_material(item_code, context=""):
    """Which material category an item belongs to, by walking its Item Group
    parent chain up to one of the two configured groups.

    Matches the configured group itself or ANY descendant of it, so the
    company can structure its catalogue as deep as it likes. An item whose
    chain reaches neither configured group is refused — never defaulted.
    """
    groups = get_material_item_groups()
    targets = {groups[FABRICS]: FABRICS, groups[TRIMMINGS]: TRIMMINGS}

    item_group = frappe.get_cached_value("Item", item_code, "item_group")
    if not item_group:
        frappe.throw(
            _("{0}Item {1} has no Item Group.").format(context, frappe.bold(item_code))
        )

    seen = set()
    node = item_group
    while node and node not in seen:
        if node in targets:
            return targets[node], groups[targets[node]]
        seen.add(node)
        node = frappe.get_cached_value("Item Group", node, "parent_item_group")

    frappe.throw(
        _(
            "{0}Item {1} (Item Group {2}) does not fall under either material "
            "group configured in WMS Settings ({3} / {4}), so it cannot be "
            "received under a FOB ownership type. Move the item into the right "
            "part of your Item Group tree, or use a different ownership type."
        ).format(
            context,
            frappe.bold(item_code),
            frappe.bold(item_group),
            frappe.bold(groups[FABRICS]),
            frappe.bold(groups[TRIMMINGS]),
        ),
        title=_("Item is not a FOB material"),
    )


# ----------------------------------------------------------- 0.4 pricing


def resolve_selling_rate(customer, item_code, company=None, qty=1, uom=None):
    """The selling rate for (customer, item) from ERPNext's OWN Price List
    mechanism. Shared by Type 3 and Type 4 concept invoices.

    No cost+margin, no fallback to cost, no fallback to zero: if ERPNext
    resolves no price, the caller must refuse its whole action.
    """
    from erpnext.accounts.party import get_default_price_list
    from erpnext.stock.get_item_details import get_item_details

    # Read the party's price list from the DATABASE, not from the document
    # cache: a Price List assigned to a customer moments earlier (or by
    # another process) must be picked up immediately — a stale cached doc
    # would silently price against the wrong list, or refuse a valid sale.
    party = frappe._dict(
        doctype="Customer",
        name=customer,
        default_price_list=frappe.db.get_value(
            "Customer", customer, "default_price_list"
        ),
        customer_group=frappe.db.get_value("Customer", customer, "customer_group"),
    )
    price_list = get_default_price_list(party) or frappe.db.get_single_value(
        "Selling Settings", "selling_price_list"
    )
    if not price_list:
        frappe.throw(
            _(
                "No selling Price List is configured for customer {0} (and no "
                "default in Selling Settings), so no price can be resolved for "
                "{1}."
            ).format(frappe.bold(customer), frappe.bold(item_code)),
            title=_("No Price List"),
        )

    company = company or frappe.defaults.get_global_default("company")
    currency = frappe.get_cached_value("Price List", price_list, "currency")

    ctx = frappe._dict(
        {
            "doctype": "Sales Invoice",
            "company": company,
            "customer": customer,
            "item_code": item_code,
            "qty": flt(qty) or 1,
            "uom": uom or frappe.get_cached_value("Item", item_code, "stock_uom"),
            "selling_price_list": price_list,
            "price_list": price_list,
            "price_list_currency": currency,
            "currency": currency,
            "conversion_rate": 1,
            "plc_conversion_rate": 1,
            "transaction_date": frappe.utils.nowdate(),
            "ignore_pricing_rule": 0,
            "is_pos": 0,
        }
    )

    details = get_item_details(ctx)
    rate = flt(details.get("price_list_rate")) or flt(details.get("rate"))

    if not rate:
        frappe.throw(
            _(
                "No price could be resolved for item {0} and customer {1} in "
                "Price List {2}. Add an Item Price before this can be invoiced — "
                "FOB invoicing never falls back to cost or to zero."
            ).format(
                frappe.bold(item_code), frappe.bold(customer), frappe.bold(price_list)
            ),
            title=_("No price for this customer and item"),
        )

    return rate, price_list, currency


# ------------------------------------------------------- WMS FOB Sale rows


def get_existing_fob_sale(source_doctype, source_name, source_row):
    """Idempotency check: has this exact source row already been processed?"""
    return frappe.db.get_value(
        "WMS FOB Sale",
        {
            "source_doctype": source_doctype,
            "source_name": source_name,
            "source_row": source_row,
            "docstatus": ("<", 2),
        },
        "name",
    )


def record_fob_sale(**kwargs):
    doc = frappe.get_doc(dict(kwargs, doctype="WMS FOB Sale"))
    doc.insert(ignore_permissions=True)
    return doc
