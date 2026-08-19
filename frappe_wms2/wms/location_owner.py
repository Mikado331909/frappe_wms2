# One storage location, one owner.
#
# A physical shelf may only hold the stock of ONE owner at a time, so material
# of different owners cannot get mixed on the floor. "Owner" means the specific
# Customer for customer-owned batches (types 2/3/4), or — only when WMS
# Settings says own stock is not allowed to share — the company itself for its
# own stock (type 1).
#
# NOTHING IS STORED. Whether a location is occupied, and by whom, is computed
# from the stock ledger every time it is asked. The moment the last unit of an
# owner's stock leaves a location, the next check sees it as free again: no
# flag to reset, no cleanup step, nothing that can drift out of sync with the
# real stock — the same principle the whole app rests on.

import frappe
from frappe import _
from frappe.utils import flt

OWNERSHIP_FIELD = "wms_ownership_type"
CUSTOMER_FIELD = "wms_customer"

# Sentinel identity for the company's own (customer-less) stock.
OWN_STOCK = "__own_stock__"


def own_stock_may_share():
    """WMS Settings toggle: may the company's own stock share a location with
    a customer's stock?

    OFF (default): own stock is an owner like any other and conflicts with
    every customer. ON: own stock is skipped entirely — it neither claims a
    location nor is blocked by one. Customer-vs-customer conflicts are
    enforced either way; this toggle only decides whether type 1 takes part in
    the check at all.
    """
    return bool(
        frappe.db.get_single_value(
            "WMS Settings", "allow_own_stock_with_customer_stock"
        )
    )


def owner_of_batch(batch_no):
    """The owner identity of one batch, or None if it does not participate."""
    if not batch_no:
        return None
    stamp = frappe.db.get_value(
        "Batch", batch_no, [CUSTOMER_FIELD, OWNERSHIP_FIELD], as_dict=True
    )
    if not stamp:
        return None
    return owner_identity(stamp.get(CUSTOMER_FIELD))


def owner_identity(customer):
    """customer -> owner key. Own stock only counts when sharing is off."""
    if customer:
        return customer
    return None if own_stock_may_share() else OWN_STOCK


def owner_label(owner):
    return _("own stock") if owner == OWN_STOCK else owner


def get_location_stock(storage_location):
    """Every (item, batch, warehouse) with qty > 0 at this location.

    Built on `picking.get_stock_by_batch_location`, the same live ledger read
    the picking and reversal flows use — generalised from "one batch" to
    "everything here". Deliberately spans all warehouses: one location can
    legitimately sit under more than one warehouse in this architecture.
    """
    from frappe_wms2.wms.picking import get_stock_by_batch_location

    items = frappe.get_all(
        "Stock Ledger Entry",
        filters={"storage_location": storage_location, "is_cancelled": 0},
        pluck="item_code",
        distinct=True,
        limit_page_length=0,
    )
    if not items:
        return []

    return [
        row
        for row in get_stock_by_batch_location(list(set(items)))
        if row.storage_location == storage_location and flt(row.qty) > 0
    ]


def get_location_owners(storage_location):
    """All distinct owners currently holding stock at this location."""
    owners = {}
    for row in get_location_stock(storage_location):
        owner = owner_identity(row.get("batch_customer"))
        if not owner:
            continue  # own stock, and sharing is allowed: does not claim it
        owners.setdefault(owner, []).append(row)
    return owners


def resolve_location_owner(storage_location):
    """The single owner holding this location, or None when it is free.

    Refuses rather than guessing if more than one owner is present — which
    this rule makes impossible going forward, but which stock received before
    the rule existed may still show.
    """
    owners = get_location_owners(storage_location)
    if not owners:
        return None
    if len(owners) > 1:
        _throw_pre_existing_conflict(storage_location, owners)
    return next(iter(owners))


def assert_location_free_for(storage_location, customer, context=""):
    """The rule itself: refuse to put an owner's stock where another owner's
    stock already is. Hard block — no override, no reason field."""
    if not storage_location:
        return

    incoming = owner_identity(customer)
    if not incoming:
        # Own stock while sharing is allowed: it neither claims nor is blocked.
        return

    owners = get_location_owners(storage_location)
    if not owners:
        return
    if len(owners) > 1:
        _throw_pre_existing_conflict(storage_location, owners, context)

    current = next(iter(owners))
    if current == incoming:
        return  # same owner — several batches, items or ownership types are fine

    rows = owners[current]
    detail = ", ".join(
        sorted({f"{r.item_code} / {r.batch_no}" for r in rows})[:5]
    )
    frappe.throw(
        _(
            "{0}Storage Location {1} already holds stock of {2} ({3}), so "
            "{4} cannot be received there — one location holds one owner's "
            "stock at a time.<br><br>Use an empty location, or move that stock "
            "out first."
        ).format(
            context,
            frappe.bold(storage_location),
            frappe.bold(owner_label(current)),
            detail,
            frappe.bold(owner_label(incoming)),
        ),
        title=_("Location belongs to another owner"),
    )


def _throw_pre_existing_conflict(storage_location, owners, context=""):
    frappe.throw(
        _(
            "{0}Storage Location {1} already holds stock of MORE THAN ONE "
            "owner: {2}. That predates the one-owner-per-location rule, so it "
            "cannot be resolved automatically — move the stock apart first "
            "rather than have the system pick a side."
        ).format(
            context,
            frappe.bold(storage_location),
            ", ".join(frappe.bold(owner_label(o)) for o in sorted(owners)),
        ),
        title=_("Location holds several owners"),
    )
