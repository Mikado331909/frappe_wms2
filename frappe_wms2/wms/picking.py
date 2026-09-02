# Phase 3a — picking engine.
#
# Reads stock EXCLUSIVELY from ERPNext's own ledger (no parallel stock table):
# quantity per (item, batch, warehouse, storage_location) is derived from
# Stock Ledger Entries plus their Serial and Batch Bundles. On v16 the batch
# lives in the bundle — SLE.batch_no stays empty on submit — so every batch
# read here goes through the bundle, with SLE.batch_no only as a fallback for
# legacy rows.
#
# Customer separation is hard: a pick for customer X may only touch batches
# stamped for customer X, plus (optionally, see WMS Settings) customer-neutral
# "Own use" stock, which carries no customer and therefore cannot mix
# customers.

import frappe
from frappe import _
from frappe.utils import flt

OWNERSHIP_FIELD = "wms_ownership_type"
CUSTOMER_FIELD = "wms_customer"


# --------------------------------------------------------------- settings


def get_settings():
    """WMS Settings, honouring an in-memory override.

    WMS Settings is a global Single shared by the whole site. Tests must
    never write to it (a test run once overwrote a live WIP pot), so they
    set `frappe.flags.wms2_settings_override` instead: the values live only
    in the current process and nothing is persisted.
    """
    override = getattr(frappe.local, "flags", {}).get("wms2_settings_override")
    if override:
        doc = frappe.get_cached_doc("WMS Settings")
        # Never mutate the cached Single: work on a detached copy.
        values = doc.as_dict()
        values.update(override)
        return frappe._dict(values)
    return frappe.get_cached_doc("WMS Settings")


def get_wip_target(company=None):
    """(warehouse, storage_location) of the WIP pot. Both are required: the
    Storage Location dimension is mandatory on stock lines (Phase 0), so even
    a 'we don't track location in WIP' pot needs one sentinel location.

    When a company is given, the pot must belong to it. That check is the
    enforcement behind the company-scoped pickers on WMS Settings: a filtered
    dropdown is only UX, this is what actually stops stock being transferred
    into another company's warehouse.
    """
    s = get_settings()
    if not s.wip_warehouse or not s.wip_storage_location:
        frappe.throw(
            _(
                "Set the WIP Warehouse and WIP Location (pot) in "
                "<b>WMS Settings</b> before submitting a pick list."
            ),
            title=_("WIP pot not configured"),
        )

    if company:
        pot_company = frappe.db.get_value("Warehouse", s.wip_warehouse, "company")
        if pot_company != company:
            frappe.throw(
                _(
                    "The WIP pot in WMS Settings ({0}) belongs to {1}, but this "
                    "document is for {2}. Configure a WIP warehouse of {2}."
                ).format(
                    frappe.bold(s.wip_warehouse),
                    frappe.bold(pot_company or _("no company")),
                    frappe.bold(company),
                ),
                title=_("Wrong company"),
            )

    return s.wip_warehouse, s.wip_storage_location


def excluded_warehouses():
    """Never pick from the WIP pot itself."""
    s = get_settings()
    return [w for w in [s.wip_warehouse] if w]


# --------------------------------------------------------------- balances


def get_stock_by_batch_location(item_codes, customer=None, warehouse=None):
    """Live balance per (item, batch, warehouse, storage_location).

    Returns rows sorted FIFO (oldest batch first) with keys:
    item_code, batch_no, warehouse, storage_location, qty, batch_creation,
    batch_customer, batch_ownership.
    Only batches the given customer may touch are returned.
    """
    item_codes = [i for i in set(item_codes or []) if i]
    if not item_codes:
        return []

    filters = {
        "item_code": ("in", item_codes),
        "is_cancelled": 0,
    }
    if warehouse:
        filters["warehouse"] = warehouse
    excluded = excluded_warehouses()
    if excluded and not warehouse:
        filters["warehouse"] = ("not in", excluded)

    sles = frappe.get_all(
        "Stock Ledger Entry",
        filters=filters,
        fields=[
            "name",
            "item_code",
            "warehouse",
            "storage_location",
            "actual_qty",
            "batch_no",
            "serial_and_batch_bundle",
        ],
        limit_page_length=0,
    )
    if not sles:
        return []

    # Resolve batches through the bundles (authoritative on v16).
    bundle_names = [s.serial_and_batch_bundle for s in sles if s.serial_and_batch_bundle]
    bundle_map = {}
    if bundle_names:
        for row in frappe.get_all(
            "Serial and Batch Entry",
            filters={"parent": ("in", bundle_names)},
            fields=["parent", "batch_no", "qty"],
            limit_page_length=0,
        ):
            if not row.batch_no:
                continue
            bundle_map.setdefault(row.parent, {})
            bundle_map[row.parent][row.batch_no] = (
                bundle_map[row.parent].get(row.batch_no, 0) + flt(row.qty)
            )

    balances = {}
    for sle in sles:
        if sle.serial_and_batch_bundle:
            batch_qty = bundle_map.get(sle.serial_and_batch_bundle, {})
        elif sle.batch_no:
            batch_qty = {sle.batch_no: flt(sle.actual_qty)}
        else:
            # Non-batched stock is out of scope for Phase 3a picking:
            # ownership/customer separation rides on the batch.
            continue

        for batch_no, qty in batch_qty.items():
            key = (sle.item_code, batch_no, sle.warehouse, sle.storage_location)
            balances[key] = balances.get(key, 0) + flt(qty)

    if not balances:
        return []

    batch_names = {k[1] for k in balances}
    batch_info = {
        b.name: b
        for b in frappe.get_all(
            "Batch",
            filters={"name": ("in", list(batch_names))},
            fields=[
                "name",
                "creation",
                "manufacturing_date",
                "disabled",
                CUSTOMER_FIELD,
                OWNERSHIP_FIELD,
            ],
            limit_page_length=0,
        )
    }

    rows = []
    for (item_code, batch_no, wh, location), qty in balances.items():
        if flt(qty) <= 0:
            continue
        info = batch_info.get(batch_no)
        if not info or info.disabled:
            continue
        if not is_batch_allowed(info, customer):
            continue
        rows.append(
            frappe._dict(
                item_code=item_code,
                batch_no=batch_no,
                warehouse=wh,
                storage_location=location,
                qty=flt(qty),
                batch_date=info.manufacturing_date,
                batch_creation=info.creation,
                batch_customer=info.get(CUSTOMER_FIELD),
                batch_ownership=info.get(OWNERSHIP_FIELD),
            )
        )

    # FIFO: oldest batch first. manufacturing_date is only a DATE, so many
    # batches share one value; the batch's creation timestamp is the
    # tiebreaker, which keeps the order deterministic and truly first-in.
    rows.sort(
        key=lambda r: (
            str(r.batch_date or ""),
            str(r.batch_creation or ""),
            r.batch_no,
            r.storage_location or "",
        )
    )
    return rows


def get_batch_location_qty(item_code, batch_no, warehouse, storage_location):
    """Balance of exactly one (item, batch, warehouse, location).

    Deliberately unfiltered by customer: this is a stock READ. Whether the
    batch may be picked for this customer is a separate, explicit check
    (assert_batch_allowed).
    """
    for row in get_stock_by_batch_location([item_code], warehouse=warehouse):
        if (
            row.batch_no == batch_no
            and row.storage_location == storage_location
        ):
            return row.qty
    return 0.0


# ------------------------------------------------------ customer separation


def is_batch_allowed(batch_info, customer):
    """Hard customer separation. A batch may be picked for `customer` when it
    is stamped for that customer, or when it is customer-neutral (Own use)
    and WMS Settings allows neutral stock.

    `customer=None` means "no customer filter" (plain balance lookups), NOT
    "only neutral batches" — a balance read must see what is physically
    there regardless of who owns it."""
    batch_customer = batch_info.get(CUSTOMER_FIELD)
    if not customer:
        return True
    if batch_customer == customer:
        return True
    if batch_customer:
        return False  # belongs to a different customer — never
    return bool(get_settings().allow_customer_neutral_stock)


def assert_batch_allowed(batch_no, customer, context=""):
    info = frappe.db.get_value(
        "Batch", batch_no, ["name", CUSTOMER_FIELD, OWNERSHIP_FIELD], as_dict=True
    )
    if not info:
        frappe.throw(_("{0}Batch {1} does not exist.").format(context, batch_no))
    if not customer:
        frappe.throw(_("{0}No customer to check the batch against.").format(context))
    if not is_batch_allowed(info, customer):
        frappe.throw(
            _(
                "{0}Batch {1} belongs to {2} and cannot be picked for {3}. "
                "Customers are never mixed in one pick."
            ).format(
                context,
                frappe.bold(batch_no),
                frappe.bold(info.get(CUSTOMER_FIELD) or _("own use / no customer")),
                frappe.bold(customer),
            ),
            title=_("Customer separation"),
        )
    return info


# ------------------------------------------------------------- allocation


def allocate_fifo(demand_rows, customer):
    """FIFO-allocate open demand across batches.

    demand_rows: list of dicts with material_request, sales_order, item_code,
    item_name, item_group, stock_uom, qty (open demand for that order line).

    Walks batches oldest-first; a batch that is not fully consumed keeps its
    rest. Returns (lines, shortages) where shortages lists demand that no
    stock could cover — the pick list is still generated for what IS there.
    """
    item_codes = [d["item_code"] for d in demand_rows]
    stock = get_stock_by_batch_location(item_codes, customer=customer)

    remaining = {}
    for row in stock:
        key = (row.item_code, row.batch_no, row.warehouse, row.storage_location)
        remaining[key] = row.qty

    lines, shortages = [], []
    for demand in demand_rows:
        needed = flt(demand["qty"])
        if needed <= 0:
            continue
        for row in stock:
            if needed <= 0:
                break
            if row.item_code != demand["item_code"]:
                continue
            key = (row.item_code, row.batch_no, row.warehouse, row.storage_location)
            avail = flt(remaining.get(key))
            if avail <= 0:
                continue
            take = min(needed, avail)
            remaining[key] = avail - take
            needed -= take
            lines.append(
                {
                    "material_request": demand.get("material_request"),
                    "sales_order": demand.get("sales_order"),
                    # Carried so the submit can group the Stock Entries by it.
                    "work_order": demand.get("work_order"),
                    "item_code": row.item_code,
                    "item_name": demand.get("item_name"),
                    "item_group": demand.get("item_group"),
                    "stock_uom": demand.get("stock_uom"),
                    "qty_needed": flt(demand["qty"]),
                    "warehouse": row.warehouse,
                    "storage_location": row.storage_location,
                    "batch_no": row.batch_no,
                    # Balance of this (item, batch, location) at generation
                    # time — NOT reduced by earlier lines of the same list.
                    "qty_available": row.qty,
                    "qty_to_pick": take,
                    "picked_qty": take,
                    "batch_customer": row.batch_customer,
                }
            )
        if needed > 0:
            shortages.append(
                {
                    "material_request": demand.get("material_request"),
                    "item_code": demand["item_code"],
                    "qty_short": needed,
                }
            )

    return lines, shortages
