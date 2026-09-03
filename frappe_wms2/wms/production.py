# 0.5 + Part 1 — production batch identity and Type 4 (FOB per production)
# invoicing.
#
# 0.5: a Work Order's produced batch IS the Work Order. No parallel ID, no
# extra linking table — a Delivery Note that shipped two production runs shows
# batch WO-0001 and batch WO-0002, and that is the whole traceability chain.
#
# Part 1: shipping the finished good is what triggers invoicing of the
# customer's Type 4 raw material. The consumption is reconstructed from the
# Work Order's own Stock Entries at that moment; picking.py is untouched and
# needs no pre-flagging.

import frappe
from frappe import _
from frappe.utils import flt

from frappe_wms2.wms.fob import resolve_selling_rate

OWNERSHIP_FIELD = "wms_ownership_type"
CUSTOMER_FIELD = "wms_customer"

# The ONE purpose whose Stock Entries genuinely consume raw material for a
# Work Order — the point where the material is embodied in the finished good.
# Every unit is counted here exactly once.
#
# Two purposes are deliberately absent:
#
# "Material Transfer for Manufacture" — the same physical material at an
# earlier stage: it leaves the source warehouse for Work-In-Progress and later
# leaves Work-In-Progress via "Manufacture". Counting both negative legs
# counted every unit twice. Verified empirically on 16.26.2 with a
# hand-counted Work Order: 20 units used, both legs summed to 40, the
# Manufacture leg alone to 20.
#
# "Material Consumption for Manufacture" — belongs to Job Card / operations
# based manufacturing, which is not in use here. The real handoff between
# departments (cutting -> sewing) is physical and produces no stock document
# at all; if it is ever booked, it will be a plain "Material Transfer", which
# is not a consumption purpose either way. Left out rather than kept
# speculatively: if Job Cards are ever introduced, this purpose gets added
# back deliberately and re-tested then.
#
# The WMS pick list's own bulk -> WIP movement is a plain "Material Transfer"
# and carries `work_order` for attribution only — never counted here (see the
# Work Order picking report).
CONSUMPTION_PURPOSES = ("Manufacture",)


# ------------------------------------------------ 0.5 finished-good batches


def guard_work_order_batch_setting(doc, method=None):
    """Refuse a Manufacture booking that ERPNext itself cannot complete.

    Found empirically on 16.26.2: with Manufacturing Settings ->
    "Make Serial No / Batch from Work Order" ON, a Manufacture entry whose
    finished-good row has no batch makes ERPNext build its own Serial and
    Batch Bundle WITHOUT a company, and the insert dies on a mandatory-field
    error deep inside `create_serial_and_batch_bundle`. That is an ERPNext
    limitation, not something this app can fix — but the raw error is
    unreadable, so it is caught here and explained.
    """
    if doc.purpose != "Manufacture" or not doc.get("work_order"):
        return
    if not frappe.db.get_single_value(
        "Manufacturing Settings", "make_serial_no_batch_from_work_order"
    ):
        return

    for row in doc.get("items") or []:
        if not row.get("is_finished_item") or row.get("s_warehouse"):
            continue
        if not frappe.get_cached_value("Item", row.item_code, "has_batch_no"):
            continue
        if row.get("batch_no") or row.get("serial_and_batch_bundle"):
            continue

        frappe.throw(
            _(
                "Manufacturing Settings has <b>Make Serial No / Batch from Work "
                "Order</b> switched on. On this ERPNext version that path fails "
                "with a mandatory-company error when the finished-good row has "
                "no batch of its own.<br><br>Either switch that setting off (so "
                "each Manufacture booking creates its own batch from the item's "
                "series, which is what the traceability setup expects), or pick "
                "the Work Order's batch on row {0} by hand."
            ).format(row.idx),
            title=_("Incompatible Manufacturing Setting"),
        )


def link_finished_good_batches_to_work_order(doc, method=None):
    """Point every batch a Manufacture booking produced at its Work Order.

    Part A: the batch is NO LONGER named after the Work Order. Each booking
    gets its own batch from ERPNext's own resolution (the item's naming series
    / auto-create settings), so two bookings on two days are two distinct
    batches — that is the traceability the owner asked for.

    What still has to hold is the LINK back to the Work Order, because the
    whole invoicing chain resolves consumption through it. Verified empirically
    on 16.26.2 (see the report): with Manufacturing Settings ->
    "Make Serial No / Batch from Work Order" ON, ERPNext pre-creates batches
    already referencing the Work Order; with it OFF (the default) a batch
    created during submit references the STOCK ENTRY instead. So the link
    cannot be assumed — this hook sets it where it is missing.

    Runs on submit, when the batches actually exist: the in-memory row is not
    refreshed by ERPNext's own batch creation, so batches are resolved from
    the ledger through the shared `_get_row_batches` helper.
    """
    if doc.purpose != "Manufacture" or not doc.get("work_order"):
        return

    from frappe_wms2.wms.ownership import _get_row_batches

    for row in doc.get("items") or []:
        if not row.get("is_finished_item") or row.get("s_warehouse"):
            continue

        for batch_no in _get_row_batches(doc, row):
            reference = frappe.db.get_value(
                "Batch", batch_no, ["reference_doctype", "reference_name"],
                as_dict=True,
            )
            if (
                reference
                and reference.reference_doctype == "Work Order"
                and reference.reference_name == doc.work_order
            ):
                continue  # ERPNext already linked it; leave it alone

            frappe.db.set_value(
                "Batch",
                batch_no,
                {
                    "reference_doctype": "Work Order",
                    "reference_name": doc.work_order,
                },
                update_modified=False,
            )
            frappe.clear_document_cache("Batch", batch_no)


def get_work_order_batches(work_order):
    """Every finished-good batch a Work Order produced — the reverse of
    `get_work_order_for_batch`. One entry per Manufacture booking."""
    return frappe.get_all(
        "Batch",
        filters={"reference_doctype": "Work Order", "reference_name": work_order},
        pluck="name",
    )


def get_work_order_for_batch(batch_no):
    """The Work Order behind a finished-good batch.

    Since Part A the reference fields are the NORMAL path, not a fallback:
    batch names no longer coincide with Work Order names. The name check is
    kept only for batches created before that change.
    """
    if not batch_no:
        return None

    ref_doctype, ref_name = frappe.db.get_value(
        "Batch", batch_no, ["reference_doctype", "reference_name"]
    ) or (None, None)
    if ref_doctype == "Work Order" and ref_name:
        return ref_name

    # Legacy: batches named after their Work Order (pre-Part-A).
    if frappe.db.exists("Work Order", batch_no):
        return batch_no
    return None


# ------------------------------------------- consumption reconstruction


def get_work_order_consumption(work_order):
    """Raw material actually consumed by a Work Order, per (item, batch).

    Read from the real Stock Ledger Entries of the Work Order's own Stock
    Entries. Batch data on v16 lives in the Serial and Batch Bundle, so it is
    resolved through the bundle — SLE.batch_no is only a fallback for legacy
    rows.
    """
    entries = frappe.get_all(
        "Stock Entry",
        filters={
            "work_order": work_order,
            "docstatus": 1,
            "purpose": ("in", CONSUMPTION_PURPOSES),
        },
        pluck="name",
    )
    if not entries:
        return {}

    sles = frappe.get_all(
        "Stock Ledger Entry",
        filters={
            "voucher_type": "Stock Entry",
            "voucher_no": ("in", entries),
            "is_cancelled": 0,
            "actual_qty": ("<", 0),
        },
        fields=["item_code", "actual_qty", "batch_no", "serial_and_batch_bundle",
                "warehouse"],
        limit_page_length=0,
    )

    consumption = {}
    for sle in sles:
        for batch_no, qty in _batches_of(sle).items():
            if not batch_no:
                continue
            key = (sle.item_code, batch_no)
            consumption[key] = consumption.get(key, 0) + abs(flt(qty))
    return consumption


def _batches_of(sle):
    if sle.serial_and_batch_bundle:
        out = {}
        for row in frappe.get_all(
            "Serial and Batch Entry",
            filters={"parent": sle.serial_and_batch_bundle},
            fields=["batch_no", "qty"],
        ):
            out[row.batch_no] = out.get(row.batch_no, 0) + flt(row.qty)
        return out
    if sle.batch_no:
        return {sle.batch_no: flt(sle.actual_qty)}
    return {}


def get_bom_qty_per_unit(work_order, item_code):
    """Raw material quantity per ONE finished-good unit, from the Work Order's
    BOM."""
    bom_no, wo_qty = frappe.db.get_value(
        "Work Order", work_order, ["bom_no", "qty"]
    ) or (None, None)
    if not bom_no:
        return None

    bom_quantity = flt(frappe.db.get_value("BOM", bom_no, "quantity")) or 1
    rows = frappe.get_all(
        "BOM Item",
        filters={"parent": bom_no, "item_code": item_code},
        fields=["stock_qty", "qty"],
    )
    if not rows:
        return None
    total = sum(flt(r.stock_qty) or flt(r.qty) for r in rows)
    return total / bom_quantity


# ---------------------------------------------- Part 1: Delivery Note hook


def invoice_fob_material_on_delivery(doc, method=None):
    """On submit of a Delivery Note: create ONE draft Sales Invoice for the
    customer's Type 4 raw material consumed by the shipped production runs.

    Nothing is auto-submitted: the accountant reviews and submits.
    """
    if doc.get("is_return"):
        return

    lines, progress_updates, audit = [], [], []
    currencies = set()

    for dn_row in doc.get("items") or []:
        for fg_batch, shipped_qty in _shipped_batches(dn_row).items():
            work_order = get_work_order_for_batch(fg_batch)
            if not work_order:
                continue

            for (item_code, rm_batch), consumed in get_work_order_consumption(
                work_order
            ).items():
                stamp = frappe.db.get_value(
                    "Batch", rm_batch, [OWNERSHIP_FIELD, CUSTOMER_FIELD], as_dict=True
                )
                if not stamp:
                    continue
                # Only THIS customer's Type 4 material. Type 1/2/3 consumed by
                # the same Work Order is never invoiced here.
                if not _is_type4(stamp.get(OWNERSHIP_FIELD)):
                    continue
                if stamp.get(CUSTOMER_FIELD) != doc.customer:
                    continue

                per_unit = get_bom_qty_per_unit(work_order, item_code)
                if per_unit is None:
                    continue

                share = flt(per_unit) * flt(shipped_qty)
                progress = _get_progress(
                    work_order, rm_batch, fg_batch, item_code, consumed, doc, stamp
                )
                # Reserved = on a draft that has not been submitted. Counted
                # here so the same material cannot be put on a second concept
                # invoice, but NOT counted as billed.
                remaining = (
                    flt(progress.consumed_qty)
                    - flt(progress.invoiced_qty)
                    - flt(progress.reserved_qty)
                )
                qty = min(share, remaining)
                if qty <= 0:
                    continue

                price = resolve_selling_rate(
                    doc.customer, item_code, company=doc.company, qty=qty,
                    posting_date=doc.posting_date,
                )

                lines.append(
                    {
                        "item_code": item_code,
                        "qty": qty,
                        "rate": price.rate,
                        "uom": frappe.get_cached_value("Item", item_code, "stock_uom"),
                        "conversion_factor": 1,
                    }
                )
                progress_updates.append((progress.name, qty))
                currencies.add((price.currency, price.conversion_rate))
                audit.append(
                    {
                        "item_code": item_code,
                        "batch_no": rm_batch,
                        "qty": qty,
                        "rate": price.rate,
                        "price_list": price.price_list,
                        "finished_good_batch": fg_batch,
                        "work_order": work_order,
                        "ownership_type": stamp.get(OWNERSHIP_FIELD),
                        "source_row": dn_row.name,
                    }
                )

    if not lines:
        # Nothing of this customer's Type 4 material was consumed — silent, by
        # design; a zero-line invoice is simply not created.
        return

    invoice = _make_draft_invoice(doc, lines, currencies)

    # RESERVE, do not invoice: the invoice is a draft the accountant may still
    # discard. Booking it as invoiced here permanently understated what is owed
    # if the draft never got submitted — a silent underbilling.
    for progress_name, qty in progress_updates:
        frappe.db.set_value(
            "WMS FOB Invoicing Progress",
            progress_name,
            "reserved_qty",
            flt(frappe.db.get_value(
                "WMS FOB Invoicing Progress", progress_name, "reserved_qty"
            )) + flt(qty),
        )

    for row in audit:
        frappe.get_doc(
            dict(
                row,
                doctype="WMS FOB Sale",
                source_doctype="Delivery Note",
                source_name=doc.name,
                customer=doc.customer,
                company=doc.company,
                sales_invoice=invoice.name,
            )
        ).insert(ignore_permissions=True)

    frappe.msgprint(
        _("Concept Sales Invoice {0} created for FOB material — review and submit it.")
        .format(frappe.bold(invoice.name)),
        indicator="blue",
        alert=True,
    )


def _is_type4(ownership_type):
    """Type 4 = the ownership type configured to require a BOM and a customer
    warehouse. Identified by its FLAGS, never by its name."""
    if not ownership_type:
        return False
    row = frappe.db.get_value(
        "WMS Ownership Type",
        ownership_type,
        ["requires_bom", "route_to_customer_warehouse", "zero_valuation_receipt"],
        as_dict=True,
    )
    return bool(row and row.requires_bom and row.route_to_customer_warehouse)


def _shipped_batches(dn_row):
    """batch -> qty for one Delivery Note line (bundle first, field fallback)."""
    if dn_row.get("serial_and_batch_bundle"):
        out = {}
        for row in frappe.get_all(
            "Serial and Batch Entry",
            filters={"parent": dn_row.serial_and_batch_bundle},
            fields=["batch_no", "qty"],
        ):
            out[row.batch_no] = out.get(row.batch_no, 0) + abs(flt(row.qty))
        return out
    if dn_row.get("batch_no"):
        return {dn_row.batch_no: flt(dn_row.qty)}
    return {}


def _get_progress(work_order, rm_batch, fg_batch, item_code, consumed, doc, stamp):
    """The (WORK ORDER, raw-material batch) ledger row.

    Part B: keyed on the Work Order, not on a finished-good batch. Since each
    Manufacture booking now produces its own batch, two batches of one Work
    Order shipped on two Delivery Notes must draw down ONE shared allowance —
    the Work Order's actual total consumption — or the same material would be
    invoiced once per batch.

    The consumed quantity is refreshed on every visit rather than frozen at
    creation: a later booking of the same Work Order consumes more raw
    material, and the allowance has to grow with it.
    """
    name = frappe.db.get_value(
        "WMS FOB Invoicing Progress",
        {"work_order": work_order, "raw_material_batch": rm_batch},
        "name",
    )
    if name:
        progress = frappe.get_doc("WMS FOB Invoicing Progress", name)
        if flt(progress.consumed_qty) != flt(consumed):
            progress.db_set("consumed_qty", flt(consumed))
        return progress

    return frappe.get_doc(
        {
            "doctype": "WMS FOB Invoicing Progress",
            "finished_good_batch": fg_batch,
            "raw_material_batch": rm_batch,
            "work_order": work_order,
            "item_code": item_code,
            "customer": stamp.get(CUSTOMER_FIELD),
            "ownership_type": stamp.get(OWNERSHIP_FIELD),
            "company": doc.company,
            "consumed_qty": consumed,
            "invoiced_qty": 0,
        }
    ).insert(ignore_permissions=True)


def _invoice_line_location(company):
    """The Storage Location dimension is Mandatory on EVERY stock-item line —
    including a Sales Invoice line that does not update stock (a documented
    Phase 2a caveat of applying the dimension to all doctypes). The concept
    invoice therefore carries the WIP pot location: that is where this
    material physically went when it was consumed. It has no stock effect,
    the invoice does not move anything.
    """
    from frappe_wms2.wms.picking import get_wip_target

    try:
        _warehouse, location = get_wip_target(company)
        return location
    except frappe.ValidationError:
        return None


def _make_draft_invoice(doc, lines, currencies):
    """One DRAFT invoice per Delivery Note. No stock movement of its own:
    Type 4's raw material left stock at pick time.

    The invoice is denominated in the currency of the Price List the rates
    were actually resolved from — NOT the Delivery Note's currency, which may
    be a different one entirely and would make the invoice claim a currency
    its own line rates were never calculated in.
    """
    if len(currencies) > 1:
        frappe.throw(
            _(
                "The lines for this Delivery Note resolved to more than one "
                "currency ({0}). One invoice cannot carry two currencies."
            ).format(", ".join(sorted(c for c, _r in currencies))),
            title=_("Mixed currencies"),
        )
    currency, conversion_rate = next(iter(currencies))
    location = _invoice_line_location(doc.company)
    if location:
        for line in lines:
            line.setdefault("storage_location", location)

    invoice = frappe.get_doc(
        {
            "doctype": "Sales Invoice",
            "customer": doc.customer,
            "company": doc.company,
            "posting_date": doc.posting_date,
            "currency": currency,
            "conversion_rate": conversion_rate,
            "update_stock": 0,
            "wms_fob_source_doctype": "Delivery Note",
            "wms_fob_source_name": doc.name,
            "remarks": _(
                "FOB material consumed for the production shipped on Delivery "
                "Note {0}. Concept — review before submitting."
            ).format(doc.name),
            "items": lines,
        }
    )
    invoice.flags.ignore_permissions = True
    invoice.insert(ignore_permissions=True)  # DRAFT — never auto-submitted
    return invoice


# ------------------------------------------------- Part C: traceability


@frappe.whitelist()
def get_work_order_traceability(work_order):
    """What went into a Work Order, and what came out of it.

    Raw material batches are read from the Work Order's own consumption (the
    same function invoicing uses, so the two can never disagree), with each
    batch's ownership stamp resolved from the Batch itself. Finished-good
    batches are the reverse lookup of `get_work_order_for_batch`: one entry per
    Manufacture booking since Part A.

    A Work Order with nothing booked yet returns empty lists, not an error —
    that is a normal mid-production state.
    """
    if not frappe.db.exists("Work Order", work_order):
        frappe.throw(_("Work Order {0} does not exist.").format(work_order))
    frappe.has_permission("Work Order", doc=work_order, throw=True)

    raw_materials = []
    for (item_code, batch_no), qty in sorted(
        get_work_order_consumption(work_order).items()
    ):
        stamp = (
            frappe.db.get_value(
                "Batch", batch_no, [OWNERSHIP_FIELD, CUSTOMER_FIELD], as_dict=True
            )
            or {}
        )
        raw_materials.append(
            {
                "batch_no": batch_no,
                "item_code": item_code,
                "item_name": frappe.get_cached_value("Item", item_code, "item_name"),
                "qty": flt(qty),
                "ownership_type": stamp.get(OWNERSHIP_FIELD),
                "customer": stamp.get(CUSTOMER_FIELD),
            }
        )

    finished_goods = []
    for batch_no in get_work_order_batches(work_order):
        finished_goods.append(
            dict(_finished_batch_figures(batch_no), batch_no=batch_no)
        )
    finished_goods.sort(key=lambda r: (r.get("booking_date") or "", r["batch_no"]))

    return {
        "work_order": work_order,
        "production_item": frappe.db.get_value(
            "Work Order", work_order, "production_item"
        ),
        "raw_materials": raw_materials,
        "finished_goods": finished_goods,
    }


def _finished_batch_figures(batch_no):
    """Produced qty and booking date of one finished-good batch, from the
    Manufacture entry that created it."""
    rows = frappe.db.sql(
        """
        select sle.posting_date, sle.voucher_no, sum(sbe.qty) as qty
        from `tabSerial and Batch Entry` sbe
        inner join `tabSerial and Batch Bundle` sbb on sbb.name = sbe.parent
        inner join `tabStock Ledger Entry` sle
            on sle.serial_and_batch_bundle = sbb.name and sle.is_cancelled = 0
        where sbe.batch_no = %s and sbb.docstatus = 1 and sle.actual_qty > 0
        group by sle.posting_date, sle.voucher_no
        order by sle.posting_date asc
        """,
        batch_no,
        as_dict=True,
    )
    if not rows:
        legacy = frappe.db.sql(
            """select posting_date, voucher_no, sum(actual_qty) as qty
               from `tabStock Ledger Entry`
               where batch_no = %s and is_cancelled = 0 and actual_qty > 0
               group by posting_date, voucher_no""",
            batch_no,
            as_dict=True,
        )
        rows = legacy

    qty = sum(flt(r.qty) for r in rows)
    first = rows[0] if rows else None
    return {
        "qty": qty,
        "booking_date": first.posting_date if first else None,
        "stock_entry": first.voucher_no if first else None,
        "item_code": frappe.db.get_value("Batch", batch_no, "item"),
    }


# ------------------------------------- reserved -> invoiced, and releases
#
# A concept invoice reserves the quantity when it is created and only bills it
# when it is actually submitted. Discarding the draft releases the reservation,
# so the material becomes billable again on the next Delivery Note. Mirrors the
# Type 3 discard path in fob_direct.py rather than inventing a second pattern.


def settle_fob_reservations_on_submit(doc, method=None):
    """Move this invoice's reserved quantity into invoiced."""
    for sale in _fob_sale_rows(doc):
        progress = _progress_for(sale)
        if not progress or sale.invoice_settled:
            continue

        qty = flt(sale.qty)
        frappe.db.set_value(
            "WMS FOB Invoicing Progress",
            progress,
            {
                "reserved_qty": max(
                    0.0,
                    flt(frappe.db.get_value(
                        "WMS FOB Invoicing Progress", progress, "reserved_qty"
                    )) - qty,
                ),
                "invoiced_qty": flt(frappe.db.get_value(
                    "WMS FOB Invoicing Progress", progress, "invoiced_qty"
                )) + qty,
            },
        )
        frappe.db.set_value("WMS FOB Sale", sale.name, "invoice_settled", 1)


def release_fob_reservations(doc, method=None):
    """Give the quantity back when a concept invoice is discarded.

    Handles both states: a draft that was never submitted releases its
    reservation; a submitted invoice that is cancelled gives back what it had
    already billed. Either way the material becomes billable again instead of
    silently disappearing from the remaining-to-invoice figure.
    """
    for sale in _fob_sale_rows(doc):
        progress = _progress_for(sale)
        if not progress:
            continue

        field = "invoiced_qty" if sale.invoice_settled else "reserved_qty"
        frappe.db.set_value(
            "WMS FOB Invoicing Progress",
            progress,
            field,
            max(
                0.0,
                flt(frappe.db.get_value(
                    "WMS FOB Invoicing Progress", progress, field
                )) - flt(sale.qty),
            ),
        )
        frappe.db.set_value(
            "WMS FOB Sale",
            sale.name,
            {"invoice_settled": 0, "concept_discarded": 1, "sales_invoice": None},
        )


def _fob_sale_rows(invoice):
    """The Type 4 audit rows behind a Sales Invoice (Type 3 rows have no Work
    Order and are handled by fob_direct)."""
    return frappe.get_all(
        "WMS FOB Sale",
        filters={"sales_invoice": invoice.name, "work_order": ("is", "set")},
        fields=["name", "qty", "work_order", "batch_no", "invoice_settled"],
    )


def _progress_for(sale):
    return frappe.db.get_value(
        "WMS FOB Invoicing Progress",
        {"work_order": sale.work_order, "raw_material_batch": sale.batch_no},
        "name",
    )
