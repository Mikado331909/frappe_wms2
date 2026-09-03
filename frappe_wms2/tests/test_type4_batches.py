# GATE TESTS — Type 4: batch per booking, per-Work-Order invoicing, traceability
# ============================================================================
# Run on a disposable site only:
#   MODULE=frappe_wms2.tests.test_type4_batches \
#       bash apps/frappe_wms2/scripts/run_tests_disposable.sh

import frappe
from frappe.utils import flt, nowdate

from frappe_wms2.tests.setup_records import COMPANY, WAREHOUSE, make_batch, make_purchase_receipt
from frappe_wms2.tests.test_fob_type4 import FOBFixtures
from frappe_wms2.wms.production import (
    get_work_order_batches,
    get_work_order_consumption,
    get_work_order_for_batch,
    get_work_order_traceability,
)

TYPE2 = "Supplied by customer"
TYPE4 = "Purchased for customer"


class Type4Fixtures(FOBFixtures):
    """Work Order / Manufacture-booking helpers, shared with the reservation
    and currency tests."""

    # --------------------------------------------------------- helpers

    def auto_batch_finished_item(self):
        """Finished good with ERPNext's own batch creation — no naming
        override from us any more."""
        from frappe_wms2.tests.setup_records import make_item

        item = make_item(has_batch_no=True)
        frappe.db.set_value(
            "Item",
            item,
            {
                "item_group": self.group_unrelated,
                "create_new_batch": 1,
                "batch_number_series": f"WMS4FG-{self.run_token}-.####",
            },
        )
        frappe.clear_document_cache("Item", item)
        self.track("Item", item)
        return item

    def setup_work_order(self, per_unit=2, order_qty=10, rate=5, receive=60):
        raw = self.new_material_item(priced=rate)
        finished = self.auto_batch_finished_item()
        self.make_bom(finished, [(raw, per_unit)])

        loc = self.new_location()
        _pr, batch = self.fob_receipt(raw, receive, loc, self.customer_a, rate=3)
        self.pick_to_wip(raw, per_unit * order_qty)

        bom = frappe.db.get_value(
            "BOM", {"item": finished, "is_active": 1, "docstatus": 1}, "name"
        )
        wo = frappe.get_doc(
            {
                "doctype": "Work Order",
                "production_item": finished,
                "bom_no": bom,
                "qty": order_qty,
                "company": COMPANY,
                "wip_warehouse": self.wip_warehouse,
                "fg_warehouse": WAREHOUSE,
                "source_warehouse": self.wip_warehouse,
                "skip_transfer": 1,
            }
        )
        wo.insert(ignore_permissions=True)
        wo.submit()
        self.track("Work Order", wo.name)
        return frappe._dict(
            raw=raw, finished=finished, batch=batch, wo=wo.name, bom=bom,
            per_unit=per_unit, rate=rate,
        )

    def book_manufacture(self, ctx, qty, raw_batch=None, raw_item=None):
        """One Manufacture booking — its own finished-good batch."""
        fg_location = self.new_location()
        rows = [
            {
                "item_code": raw_item or ctx.raw,
                "qty": ctx.per_unit * qty,
                "uom": "Nos", "stock_uom": "Nos", "conversion_factor": 1,
                "s_warehouse": self.wip_warehouse,
                "storage_location": self.wip_location,
                "use_serial_batch_fields": 1,
                "batch_no": raw_batch or ctx.batch,
            },
            {
                "item_code": ctx.finished,
                "qty": qty,
                "uom": "Nos", "stock_uom": "Nos", "conversion_factor": 1,
                "t_warehouse": WAREHOUSE,
                "to_storage_location": fg_location,
                "is_finished_item": 1,
            },
        ]
        se = frappe.get_doc(
            {
                "doctype": "Stock Entry",
                "stock_entry_type": "Manufacture",
                "purpose": "Manufacture",
                "company": COMPANY,
                "work_order": ctx.wo,
                "bom_no": ctx.bom,
                "fg_completed_qty": qty,
                "from_bom": 1,
                "posting_date": nowdate(),
                "items": rows,
            }
        )
        se.insert(ignore_permissions=True)
        se.submit()
        self.track("Stock Entry", se.name)

        from frappe_wms2.wms.ownership import _get_row_batches

        fg_row = next(r for r in se.items if r.get("is_finished_item"))
        batches = _get_row_batches(se, fg_row)
        self.assertEqual(len(batches), 1)
        return next(iter(batches)), fg_location

    def invoice_for(self, delivery_note):
        rows = frappe.get_all(
            "Sales Invoice", filters={"wms_fob_source_name": delivery_note},
            pluck="name",
        )
        if not rows:
            return None
        invoice = frappe.get_doc("Sales Invoice", rows[0])
        self.track("Sales Invoice", invoice.name)
        return invoice


class TestType4Batches(Type4Fixtures):
    """Part A/B/C: one batch per Manufacture booking, invoicing capped per
    Work Order, and the traceability overview."""

    # ------------------------------------------------------------------ A1
    def test_a1_each_booking_gets_its_own_batch_not_named_after_the_wo(self):
        ctx = self.setup_work_order(per_unit=2, order_qty=10)

        batch1, _loc1 = self.book_manufacture(ctx, 6)
        batch2, _loc2 = self.book_manufacture(ctx, 4)

        # Two distinct batches, neither named after the Work Order.
        self.assertNotEqual(batch1, batch2)
        self.assertNotEqual(batch1, ctx.wo)
        self.assertNotEqual(batch2, ctx.wo)
        self.assertFalse(frappe.db.exists("Batch", ctx.wo))
        for batch in (batch1, batch2):
            self.assertTrue(batch.startswith(f"WMS4FG-{self.run_token}"))

        # A2 — both still resolve back to the same Work Order.
        self.assertEqual(get_work_order_for_batch(batch1), ctx.wo)
        self.assertEqual(get_work_order_for_batch(batch2), ctx.wo)
        self.assertEqual(set(get_work_order_batches(ctx.wo)), {batch1, batch2})

    # ------------------------------------------------------------------ B1
    def test_b1_two_batches_of_one_work_order_never_over_invoice(self):
        """The core regression risk of this change."""
        ctx = self.setup_work_order(per_unit=2, order_qty=10, rate=5)
        consumed_total = ctx.per_unit * 10  # 20

        batch1, loc1 = self.book_manufacture(ctx, 6)   # consumes 12
        batch2, loc2 = self.book_manufacture(ctx, 4)   # consumes 8
        self.assertEqual(
            flt(get_work_order_consumption(ctx.wo).get((ctx.raw, ctx.batch))),
            consumed_total,
        )

        # First batch shipped -> its proportional share.
        dn1 = self.deliver(ctx.finished, batch1, 6, loc1)
        inv1 = self.invoice_for(dn1.name)
        self.assertEqual(flt(inv1.items[0].qty), 12)
        self.assertEqual(flt(inv1.items[0].rate), ctx.rate)

        # Second batch, separate Delivery Note -> only the remainder.
        dn2 = self.deliver(ctx.finished, batch2, 4, loc2)
        inv2 = self.invoice_for(dn2.name)
        self.assertEqual(flt(inv2.items[0].qty), 8)

        # Together: exactly the Work Order's real consumption, never more.
        self.assertEqual(
            flt(inv1.items[0].qty) + flt(inv2.items[0].qty), consumed_total
        )

        # ONE progress row for the Work Order + raw batch, fully drawn down.
        progress = frappe.get_all(
            "WMS FOB Invoicing Progress",
            filters={"work_order": ctx.wo, "raw_material_batch": ctx.batch},
            fields=["consumed_qty", "reserved_qty", "invoiced_qty"],
        )
        self.assertEqual(len(progress), 1)
        self.assertEqual(flt(progress[0].consumed_qty), consumed_total)
        # Drafts: reserved (so neither shipment can claim it twice), not billed.
        self.assertEqual(flt(progress[0].reserved_qty), consumed_total)
        self.assertEqual(flt(progress[0].invoiced_qty), 0)

    # ------------------------------------------------------------------ B2
    def test_b2_single_booking_invoices_the_same_as_before(self):
        """The common case must be untouched by the batch splitting."""
        ctx = self.setup_work_order(per_unit=3, order_qty=5, rate=4)
        batch, loc = self.book_manufacture(ctx, 5)

        dn = self.deliver(ctx.finished, batch, 5, loc)
        invoice = self.invoice_for(dn.name)

        self.assertEqual(len(invoice.items), 1)
        self.assertEqual(flt(invoice.items[0].qty), 15)  # 3/unit x 5 units
        self.assertEqual(flt(invoice.items[0].rate), 4)
        self.assertEqual(invoice.docstatus, 0)

        progress = frappe.get_all(
            "WMS FOB Invoicing Progress",
            filters={"work_order": ctx.wo},
            fields=["consumed_qty", "reserved_qty", "invoiced_qty"],
        )
        self.assertEqual(len(progress), 1)
        self.assertEqual(flt(progress[0].reserved_qty), 15)
        self.assertEqual(flt(progress[0].invoiced_qty), 0)

    # ------------------------------------------------------------------ C1
    def test_c1_traceability_lists_everything_individually(self):
        ctx = self.setup_work_order(per_unit=1, order_qty=8, rate=6)

        # A second raw material for the same Work Order: Type 2, another
        # customer — it must appear in the overview but never be invoiced.
        raw2 = self.new_material_item()
        batch2_raw = self.track("Batch", make_batch(raw2))
        make_purchase_receipt(
            [{
                "item_code": raw2, "qty": 20, "rate": 0,
                "warehouse": self.wh_trimmings,
                "storage_location": self.new_location(),
                "use_serial_batch_fields": 1, "batch_no": batch2_raw,
                "wms_ownership_type": TYPE2, "wms_customer": self.customer_b,
            }]
        )
        # Picked for ITS OWN customer: customer separation means B's material
        # can only be picked on B's demand.
        self.pick_to_wip(raw2, 8, customer=self.customer_b)

        fg1, loc1 = self.book_manufacture(ctx, 5)
        fg2, _loc2 = self.book_manufacture(ctx, 3, raw_batch=batch2_raw,
                                           raw_item=raw2)

        overview = get_work_order_traceability(ctx.wo)

        # Two raw material batches, each individually attributed.
        raws = {r["batch_no"]: r for r in overview["raw_materials"]}
        self.assertEqual(set(raws), {ctx.batch, batch2_raw})
        self.assertEqual(raws[ctx.batch]["ownership_type"], TYPE4)
        self.assertEqual(raws[ctx.batch]["customer"], self.customer_a)
        self.assertEqual(flt(raws[ctx.batch]["qty"]), 5)
        self.assertEqual(raws[batch2_raw]["ownership_type"], TYPE2)
        self.assertEqual(raws[batch2_raw]["customer"], self.customer_b)
        self.assertEqual(flt(raws[batch2_raw]["qty"]), 3)

        # Two finished-good batches, with their booked quantity and date.
        fgs = {r["batch_no"]: r for r in overview["finished_goods"]}
        self.assertEqual(set(fgs), {fg1, fg2})
        self.assertEqual(flt(fgs[fg1]["qty"]), 5)
        self.assertEqual(flt(fgs[fg2]["qty"]), 3)
        for row in fgs.values():
            self.assertTrue(row["booking_date"])
            self.assertTrue(row["stock_entry"])

        # And only the Type 4 material of THIS customer is invoiced.
        dn = self.deliver(ctx.finished, fg1, 5, loc1)
        invoice = self.invoice_for(dn.name)
        self.assertEqual([r.item_code for r in invoice.items], [ctx.raw])

    # ------------------------------------------------------------------ C2
    def test_c2_work_order_without_a_booking_is_empty_not_an_error(self):
        ctx = self.setup_work_order(per_unit=2, order_qty=4)

        overview = get_work_order_traceability(ctx.wo)
        self.assertEqual(overview["work_order"], ctx.wo)
        self.assertEqual(overview["finished_goods"], [])
        self.assertEqual(overview["raw_materials"], [])
        self.assertEqual(get_work_order_batches(ctx.wo), [])

    def location_of(self, item_code, batch_no):
        """Where the remaining stock of this batch actually sits."""
        from frappe_wms2.wms.picking import get_stock_by_batch_location

        for row in get_stock_by_batch_location([item_code],
                                               warehouse=self.wh_trimmings):
            if row.batch_no == batch_no and flt(row.qty) > 0:
                return row.storage_location
        raise AssertionError(f"no stock left of {batch_no}")

    # ------------------------------------------------------------------ P0
    def test_p0_only_the_manufacture_leg_counts_as_consumption(self):
        """Consumption is counted at exactly one stage.

        Guards the Part 0 finding against regression, and pins the agreed
        contents of CONSUMPTION_PURPOSES: neither the transfer INTO
        Work-In-Progress nor a pick's own bulk -> WIP movement may be counted,
        or every unit would be billed twice.
        """
        from frappe_wms2.wms.production import CONSUMPTION_PURPOSES

        self.assertEqual(CONSUMPTION_PURPOSES, ("Manufacture",))

        ctx = self.setup_work_order(per_unit=2, order_qty=6, rate=5)
        hand_counted = ctx.per_unit * 6  # 12

        # The pick that put this material into WIP carries the Work Order for
        # attribution — it must not add to consumption.
        picks = frappe.get_all(
            "Stock Entry",
            filters={"purpose": "Material Transfer", "docstatus": 1},
            fields=["name", "work_order"],
        )
        self.assertTrue(picks, "the fixture should have picked material to WIP")

        self.assertEqual(get_work_order_consumption(ctx.wo), {})

        # Only after the Manufacture booking, and then exactly once.
        self.book_manufacture(ctx, 6)
        consumption = get_work_order_consumption(ctx.wo)
        self.assertEqual(flt(consumption.get((ctx.raw, ctx.batch))), hand_counted)

        # An explicit "Material Transfer for Manufacture" of the same units
        # into WIP still does not inflate it.
        se = frappe.get_doc(
            {
                "doctype": "Stock Entry",
                "stock_entry_type": "Material Transfer for Manufacture",
                "purpose": "Material Transfer for Manufacture",
                "company": COMPANY,
                "work_order": ctx.wo,
                "bom_no": ctx.bom,
                "fg_completed_qty": 0,
                "posting_date": nowdate(),
                "items": [
                    {
                        "item_code": ctx.raw, "qty": 4,
                        "uom": "Nos", "stock_uom": "Nos", "conversion_factor": 1,
                        "s_warehouse": self.wh_trimmings,
                        "storage_location": self.location_of(ctx.raw, ctx.batch),
                        "t_warehouse": self.wip_warehouse,
                        "to_storage_location": self.wip_location,
                        "use_serial_batch_fields": 1, "batch_no": ctx.batch,
                    }
                ],
            }
        )
        se.insert(ignore_permissions=True)
        se.submit()
        self.track("Stock Entry", se.name)

        self.assertEqual(
            flt(get_work_order_consumption(ctx.wo).get((ctx.raw, ctx.batch))),
            hand_counted,
            "a transfer into WIP must never be counted as consumption",
        )
