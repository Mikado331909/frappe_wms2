# Phase 2a — ownership at intake.
#
# Rules (driven by WMS Ownership Type flags, not hardcoded names):
# - Every stock-item intake line must carry an ACTIVE Ownership Type.
# - requires_customer  -> Customer mandatory on the line;
#   otherwise           -> Customer must stay EMPTY (no ambiguous stamping).
# - zero_valuation_receipt -> line rate must be 0 and the line is received
#   with allow_zero_valuation_rate, so quantity is tracked (incl. per
#   storage location) while stock value stays 0.
# - enforce_warehouse (optional) -> the line is routed to that warehouse
#   (auto-filled when empty, rejected when different).
# - On submit, Ownership Type + Customer are stamped onto every Batch the
#   line produced. Batches are resolved through the Serial and Batch Bundle
#   (v16: SLE.batch_no is legacy and stays empty on submit). A batch can
#   never be stamped with two different owners: mixing is blocked.
#
# Anti-backdoor: the same rule holds for Stock Entry "Material Receipt"
# lines, and "Update Stock" on Purchase Invoice is blocked (stock must enter
# through a Purchase Receipt). Paths NOT covered in Phase 2a are documented
# in PHASE2A_REPORT.md (Stock Reconciliation count corrections, returns,
# manufacturing/subcontracting receipts).

import frappe
from frappe import _
from frappe.utils import flt

OWNERSHIP_FIELD = "wms_ownership_type"
CUSTOMER_FIELD = "wms_customer"


def _is_stock_item(item_code) -> bool:
    return bool(item_code) and bool(
        frappe.get_cached_value("Item", item_code, "is_stock_item")
    )


def _get_ownership_type(name, context):
    try:
        return frappe.get_cached_doc("WMS Ownership Type", name)
    except frappe.DoesNotExistError:
        frappe.throw(
            _("{0}: Ownership Type {1} does not exist.").format(
                context, frappe.bold(name)
            )
        )


def _validate_intake_row(row, warehouse_field, rate_field, context,
                         location_field="storage_location"):
    """Shared rule for one stock-inward line. Returns the ownership doc."""
    if not _is_stock_item(row.item_code):
        return None

    ot_name = row.get(OWNERSHIP_FIELD)
    if not ot_name:
        frappe.throw(
            _("{0}: Ownership Type is mandatory for stock item {1}.").format(
                context, frappe.bold(row.item_code)
            ),
            title=_("Ownership Type missing"),
        )

    ot = _get_ownership_type(ot_name, context)

    if not ot.is_active:
        frappe.throw(
            _(
                "{0}: Ownership Type {1} is not active (reserved for a later "
                "phase). Choose an active type."
            ).format(context, frappe.bold(ot_name))
        )

    customer = row.get(CUSTOMER_FIELD)
    if ot.requires_customer and not customer:
        frappe.throw(
            _("{0}: Customer is mandatory for Ownership Type {1}.").format(
                context, frappe.bold(ot_name)
            ),
            title=_("Customer missing"),
        )
    if not ot.requires_customer and customer:
        frappe.throw(
            _(
                "{0}: leave Customer empty for Ownership Type {1} — it is "
                "customer-independent."
            ).format(context, frappe.bold(ot_name))
        )

    if ot.zero_valuation_receipt:
        # Quantity in, value 0. The line may not carry a purchase value.
        if flt(row.get(rate_field)):
            frappe.throw(
                _(
                    "{0}: Ownership Type {1} is received at ZERO valuation — "
                    "set the line rate to 0 (found {2})."
                ).format(context, frappe.bold(ot_name), row.get(rate_field))
            )
        row.allow_zero_valuation_rate = 1

    # One location, one owner — applies to EVERY ownership type, so it lives
    # in the shared path rather than in the FOB branch below.
    _assert_location_owner(row, customer, context, location_field)

    if ot.get("route_to_customer_warehouse"):
        # FOB (types 3/4): per-customer, per-material warehouse.
        _validate_fob_row(row, ot, customer, warehouse_field, context)

    elif ot.enforce_warehouse:
        current = row.get(warehouse_field)
        if not current:
            row.set(warehouse_field, ot.enforce_warehouse)
        elif current != ot.enforce_warehouse:
            frappe.throw(
                _(
                    "{0}: Ownership Type {1} routes receipts to warehouse "
                    "{2}, not {3}."
                ).format(
                    context,
                    frappe.bold(ot_name),
                    frappe.bold(ot.enforce_warehouse),
                    frappe.bold(current),
                )
            )

    # Early anti-mixing check when the batch is known before submit.
    if row.get("batch_no"):
        _assert_batch_compatible(row.batch_no, ot_name, customer, context)

    return ot


def _assert_location_owner(row, customer, context, location_field):
    """A storage location may only hold one owner's stock at a time."""
    from frappe_wms2.wms.location_owner import assert_location_free_for

    location = row.get(location_field)
    if not location:
        return  # the mandatory dimension reports a missing location itself
    assert_location_free_for(location, customer, context=f"{context}: ")


def _validate_fob_row(row, ot, customer, warehouse_field, context):
    """Extra rules that only apply to the FOB ownership types.

    Deliberately additive: nothing here runs for Type 1/2, whose code path is
    the unchanged `enforce_warehouse` branch.
    """
    from frappe_wms2.wms.customer_warehouse import get_warehouse_for_item
    from frappe_wms2.wms.fob import assert_batchwise_valuation

    prefix = f"{context}: "

    # 0.1 — a customer warehouse mixes cost-valued and zero-valued batches of
    # the same item, so batch-wise valuation must actually apply to it.
    assert_batchwise_valuation(row.item_code, context=prefix)

    # 1.1 — closing rule: no BOM, no Type 4 (see requires_bom flag).
    if ot.get("requires_bom"):
        _assert_item_in_active_bom(row.item_code, ot.name, prefix)

    # 0.2/0.3 — resolve (and create on first use) the customer warehouse, and
    # always route the line there.
    #
    # This ALWAYS overwrites, never throws. Two reasons:
    #   * There is no legitimate destination for a routed line other than the
    #     resolved customer warehouse, so correcting is as safe as filling in
    #     a blank — which this already did.
    #   * Throwing here was actively harmful: ERPNext's own UI pre-fills a
    #     default warehouse on a new row, so the mismatch branch fired
    #     routinely, and because the throw and the just-created Warehouse share
    #     one transaction, frappe rolled the Warehouse back with it. The error
    #     then named a warehouse that no longer existed and could not be
    #     selected anywhere.
    row.set(
        warehouse_field,
        get_warehouse_for_item(customer, row.item_code, context=prefix),
    )


def _assert_item_in_active_bom(item_code, ot_name, prefix):
    """The Type 4 closing rule: the item must be a raw material in at least
    one active, submitted BOM. Without a BOM there is no way to translate a
    shipped finished-good quantity into consumed raw material, so the type is
    structurally impossible for that item."""
    exists = frappe.db.sql(
        """
        select 1
        from `tabBOM Item` bi
        inner join `tabBOM` bom on bom.name = bi.parent
        where bi.item_code = %s and bom.is_active = 1 and bom.docstatus = 1
        limit 1
        """,
        item_code,
    )
    if not exists:
        frappe.throw(
            _(
                "{0}Item {1} does not appear as a raw material in any active BOM, "
                "so it cannot be received under Ownership Type {2}: there would "
                "be no way to translate a shipped finished-good quantity into "
                "consumed raw material at invoicing time."
            ).format(prefix, frappe.bold(item_code), frappe.bold(ot_name)),
            title=_("No active BOM for this item"),
        )


def _assert_batch_compatible(batch_no, ot_name, customer, context):
    existing = frappe.db.get_value(
        "Batch", batch_no, [OWNERSHIP_FIELD, CUSTOMER_FIELD], as_dict=True
    )
    if not existing:
        return
    stamped_ot = existing.get(OWNERSHIP_FIELD)
    stamped_customer = existing.get(CUSTOMER_FIELD) or None
    if stamped_ot and (stamped_ot != ot_name or stamped_customer != (customer or None)):
        frappe.throw(
            _(
                "{0}: Batch {1} already belongs to Ownership Type {2} / "
                "Customer {3} — one batch cannot mix owners. Use a "
                "different batch."
            ).format(
                context,
                frappe.bold(batch_no),
                frappe.bold(stamped_ot),
                frappe.bold(stamped_customer or _("(none)")),
            ),
            title=_("Batch ownership conflict"),
        )


# ----------------------------------------------------------- before_validate


def route_purchase_receipt(doc, method=None):
    """Autofill the enforced warehouse BEFORE the controller's own validate
    runs (hooked `validate` functions run after it — too late to satisfy
    warehouse-mandatory checks). Mismatches still throw in validate."""
    _route_rows(doc.get("items") or [], "warehouse")


def route_stock_entry(doc, method=None):
    if doc.purpose != "Material Receipt":
        return
    rows = [
        d
        for d in (doc.get("items") or [])
        if not d.get("s_warehouse")
    ]
    _route_rows(rows, "t_warehouse")


def _route_rows(rows, warehouse_field):
    for row in rows:
        ot_name = row.get(OWNERSHIP_FIELD)
        if not ot_name or row.get(warehouse_field):
            continue
        if not frappe.db.exists("WMS Ownership Type", ot_name):
            continue  # validate() reports this properly

        ot = frappe.get_cached_doc("WMS Ownership Type", ot_name)

        # FOB types (3/4): the warehouse is per CUSTOMER and per material,
        # created on first use. Type 1/2 keep the static enforce_warehouse
        # path below, untouched.
        if ot.get("route_to_customer_warehouse"):
            customer = row.get(CUSTOMER_FIELD)
            if not customer or not row.get("item_code"):
                continue  # validate() reports the missing customer properly
            row.set(
                warehouse_field,
                _resolve_customer_warehouse(row.item_code, customer),
            )
            continue

        if ot.enforce_warehouse:
            row.set(warehouse_field, ot.enforce_warehouse)


def _resolve_customer_warehouse(item_code, customer, context=""):
    from frappe_wms2.wms.customer_warehouse import get_warehouse_for_item

    return get_warehouse_for_item(customer, item_code, context=context)


# ---------------------------------------------------------------- validate


def validate_purchase_receipt(doc, method=None):
    for row in doc.get("items") or []:
        _validate_intake_row(
            row,
            warehouse_field="warehouse",
            rate_field="rate",
            context=_("Row {0}").format(row.idx),
        )


def validate_stock_entry(doc, method=None):
    # Only pure receipts bring NEW stock in from outside; transfers move
    # existing (already-stamped) stock and manufacture entries are a later
    # phase. Guard rows that receive without a source.
    if doc.purpose != "Material Receipt":
        return
    for row in doc.get("items") or []:
        if row.get("s_warehouse") or not row.get("t_warehouse"):
            continue
        _validate_intake_row(
            row,
            warehouse_field="t_warehouse",
            rate_field="basic_rate",
            location_field="to_storage_location",
            context=_("Row {0}").format(row.idx),
        )


def before_validate_purchase_invoice(doc, method=None):
    # Anti-backdoor: a Purchase Invoice with Update Stock would receive
    # stock without the intake rule. Force intake through Purchase Receipt.
    if doc.get("update_stock"):
        frappe.throw(
            _(
                "Stock intake must go through a Purchase Receipt so that "
                "Ownership Type and Customer are captured. 'Update Stock' "
                "on Purchase Invoice is blocked by frappe_wms2 (Phase 2a)."
            ),
            title=_("Receive via Purchase Receipt"),
        )


# ---------------------------------------------------------------- on_submit


def stamp_batches_purchase_receipt(doc, method=None):
    _stamp_batches(doc)


def stamp_batches_stock_entry(doc, method=None):
    if doc.purpose == "Material Receipt":
        _stamp_batches(doc)


def _stamp_batches(doc):
    for row in doc.get("items") or []:
        ot_name = row.get(OWNERSHIP_FIELD)
        if not ot_name or not _is_stock_item(row.item_code):
            continue
        customer = row.get(CUSTOMER_FIELD)
        context = _("Row {0}").format(row.idx)
        for batch_no in _get_row_batches(doc, row):
            _assert_batch_compatible(batch_no, ot_name, customer, context)
            frappe.db.set_value(
                "Batch",
                batch_no,
                {OWNERSHIP_FIELD: ot_name, CUSTOMER_FIELD: customer},
                update_modified=False,
            )


def _get_row_batches(doc, row):
    """All batches this line put into stock — resolved via the Serial and
    Batch Bundle on the line's Stock Ledger Entries (authoritative in v16;
    SLE.batch_no is legacy), plus the row-level batch_no as fallback."""
    batches, bundles = set(), set()

    if row.get("batch_no"):
        batches.add(row.batch_no)
    if row.get("serial_and_batch_bundle"):
        bundles.add(row.serial_and_batch_bundle)

    for sle in frappe.get_all(
        "Stock Ledger Entry",
        filters={
            "voucher_type": doc.doctype,
            "voucher_no": doc.name,
            "voucher_detail_no": row.name,
            "is_cancelled": 0,
            "actual_qty": (">", 0),
        },
        fields=["batch_no", "serial_and_batch_bundle"],
    ):
        if sle.batch_no:
            batches.add(sle.batch_no)
        if sle.serial_and_batch_bundle:
            bundles.add(sle.serial_and_batch_bundle)

    for bundle in bundles:
        for batch_no in frappe.get_all(
            "Serial and Batch Entry", {"parent": bundle}, pluck="batch_no"
        ):
            if batch_no:
                batches.add(batch_no)

    return batches
