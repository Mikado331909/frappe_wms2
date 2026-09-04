# GATE TESTS — extra material beyond the BOM (supplementary requests)
# ===================================================================
# Run on a disposable site only:
#   MODULE=frappe_wms2.tests.test_extra_consumption \
#       bash apps/frappe_wms2/scripts/run_tests_disposable.sh

import frappe
from frappe.utils import flt

from frappe_wms2.tests.setup_records import COMPANY
from frappe_wms2.tests.test_type4_batches import Type4Fixtures
from frappe_wms2.wms.doctype.wms_pick_batch.wms_pick_batch import (
    make_supplementary_material_request,
)

REASON = "Rework"
SHORTAGE_REASON = "Batch empty earlier than expected"


class TestExtraConsumption(Type4Fixtures):
    # ---------------------------------------------------------- helpers

    def progress_rows(self, work_order):
        return frappe.get_all(
            "WMS FOB Invoicing Progress",
            filters={"work_order": work_order},
            fields=["name", "raw_material_batch", "consumed_qty", "reserved_qty",
                    "invoiced_qty", "supplementary_material_request",
                    "extra_consumption_reason"],
        )

    def ensure_sales_order(self, ctx):
        """The Type 4 fixtures build Work Orders without a Sales Order; the
        supplementary request needs one, since the customer is derived from
        it (same rule as the ordinary request helper)."""
        if frappe.db.get_value("Work Order", ctx.wo, "sales_order"):
            return
        so = self.new_sales_order(self.customer_a, ctx.finished, 1)
        frappe.db.set_value("Work Order", ctx.wo, "sales_order", so.name)
        frappe.db.commit()
        frappe.clear_document_cache("Work Order", ctx.wo)

    def supplementary(self, ctx, qty, reason=REASON, submit=True, comment=None):
        self.ensure_sales_order(ctx)
        mr = make_supplementary_material_request(
            ctx.wo,
            items=[{"item_code": ctx.raw, "qty": qty}],
            reason=reason,
            comment=comment,
        )
        self.track("Material Request", mr.name)
        if submit:
            mr.submit()
        return mr

    # ------------------------------------------------------------------ E1
    def test_e1_unused_feature_changes_nothing(self):
        """No supplementary request anywhere: invoicing as before."""
        ctx = self.setup_work_order(per_unit=2, order_qty=5, rate=4)
        batch, loc = self.book_manufacture(ctx, 5)
        dn = self.deliver(ctx.finished, batch, 5, loc)
        invoice = self.invoice_for(dn.name)

        self.assertEqual(len(invoice.items), 1)
        self.assertEqual(flt(invoice.items[0].qty), 10)      # 2/unit x 5
        rows = self.progress_rows(ctx.wo)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].supplementary_material_request)

    # ------------------------------------------------------------------ E2
    def test_e2_supplementary_request_with_a_reason_succeeds(self):
        ctx = self.setup_work_order(per_unit=2, order_qty=5, rate=4)
        self.book_manufacture(ctx, 2)   # work in progress, Work Order open

        mr = self.supplementary(ctx, 6, comment="second run needed more")
        self.assertEqual(mr.work_order, ctx.wo)
        self.assertTrue(mr.wms_supplementary_for_work_order)
        self.assertEqual(mr.wms_extra_consumption_reason, REASON)
        self.assertEqual(flt(mr.items[0].qty), 6)
        # It sits alongside the Work Order's normal request, not instead of it.
        self.assertEqual(
            frappe.db.count(
                "Material Request",
                {"work_order": ctx.wo, "wms_supplementary_for_work_order": 1},
            ),
            1,
        )

    # ------------------------------------------------------------------ E3
    def test_e3_no_reason_is_refused(self):
        ctx = self.setup_work_order(per_unit=2, order_qty=5, rate=4)

        self.ensure_sales_order(ctx)
        for bad in (None, ""):
            with self.assertRaises(frappe.ValidationError) as err:
                make_supplementary_material_request(
                    ctx.wo, items=[{"item_code": ctx.raw, "qty": 3}], reason=bad
                )
            self.assertIn("reason", str(err.exception).lower())

        # A reason from another category is refused too.
        with self.assertRaises(frappe.ValidationError):
            make_supplementary_material_request(
                ctx.wo,
                items=[{"item_code": ctx.raw, "qty": 3}],
                reason=SHORTAGE_REASON,
            )
        frappe.db.rollback()

    # ------------------------------------------------------------------ E4
    def test_e4_closed_and_fully_invoiced_is_refused(self):
        ctx = self.setup_work_order(per_unit=2, order_qty=4, rate=4)
        batch, loc = self.book_manufacture(ctx, 4)
        dn = self.deliver(ctx.finished, batch, 4, loc)
        invoice = self.invoice_for(dn.name)
        invoice.submit()   # nothing reserved, everything billed

        rows = self.progress_rows(ctx.wo)
        self.assertEqual(flt(rows[0].reserved_qty), 0)
        self.assertEqual(flt(rows[0].invoiced_qty), flt(rows[0].consumed_qty))

        frappe.db.set_value("Work Order", ctx.wo, "status", "Completed")
        frappe.db.commit()

        with self.assertRaises(frappe.ValidationError) as err:
            self.supplementary(ctx, 4)
        message = str(err.exception)
        self.assertIn(ctx.wo, message)
        self.assertIn("no future invoice event", message)
        frappe.db.rollback()

    # ------------------------------------------------------------------ E5
    def test_e5_both_conditions_are_required_together(self):
        """Closed but NOT fully invoiced still accepts a supplementary
        request — and open but fully invoiced does too."""
        # (a) closed, with an open reservation -> allowed
        ctx = self.setup_work_order(per_unit=2, order_qty=4, rate=4)
        batch, loc = self.book_manufacture(ctx, 4)
        dn = self.deliver(ctx.finished, batch, 4, loc)
        self.invoice_for(dn.name)          # left as a DRAFT: reserved > 0
        frappe.db.set_value("Work Order", ctx.wo, "status", "Completed")
        frappe.db.commit()

        rows = self.progress_rows(ctx.wo)
        self.assertGreater(flt(rows[0].reserved_qty), 0)
        mr = self.supplementary(ctx, 3, submit=False)
        self.assertTrue(mr.name)

        # (b) fully invoiced, but the Work Order is still open -> allowed
        ctx2 = self.setup_work_order(per_unit=2, order_qty=4, rate=4)
        batch2, loc2 = self.book_manufacture(ctx2, 4)
        dn2 = self.deliver(ctx2.finished, batch2, 4, loc2)
        invoice2 = self.invoice_for(dn2.name)
        invoice2.submit()

        # ERPNext marks a Work Order Completed as soon as everything is
        # produced, so the "still open" half is set explicitly — the point
        # being tested is the guard's logic, not ERPNext's status machine.
        frappe.db.set_value("Work Order", ctx2.wo, "status", "In Process")
        frappe.db.commit()
        frappe.clear_document_cache("Work Order", ctx2.wo)
        self.assertNotIn(
            frappe.db.get_value("Work Order", ctx2.wo, "status"),
            ("Completed", "Closed", "Stopped"),
        )
        mr2 = self.supplementary(ctx2, 3, submit=False)
        self.assertTrue(mr2.name)

    # ------------------------------------------------------------------ E6
    def test_e6_consumed_extra_material_is_invoiced_separately(self):
        ctx = self.setup_work_order(per_unit=2, order_qty=5, rate=4, receive=60)

        # Extra material requested with a reason, then genuinely consumed by a
        # second Manufacture booking of the same Work Order.
        self.supplementary(ctx, 6, comment="rework of the first run")
        batch, loc = self.book_manufacture(ctx, 5)

        dn = self.deliver(ctx.finished, batch, 5, loc)
        invoice = self.invoice_for(dn.name)

        # Two lines: the untouched BOM-ratio line, and the extra one.
        self.assertEqual(len(invoice.items), 2)
        by_desc = sorted(invoice.items, key=lambda r: bool(
            r.description and "beyond the BOM" in r.description))
        standard, extra = by_desc[0], by_desc[1]

        self.assertEqual(flt(standard.qty), 10)          # 2/unit x 5, unchanged
        self.assertNotIn("beyond the BOM", standard.description or "")

        self.assertEqual(flt(extra.qty), 6)
        self.assertIn("beyond the BOM", extra.description)
        self.assertIn(REASON, extra.description)         # the reason is named
        self.assertIn("rework of the first run", extra.description)

        # Tracked in its own progress row, reserved like everything else.
        rows = {r.supplementary_material_request or "standard": r
                for r in self.progress_rows(ctx.wo)}
        self.assertEqual(len(rows), 2)
        supplementary = rows[[k for k in rows if k != "standard"][0]]
        self.assertEqual(flt(supplementary.reserved_qty), 6)
        self.assertEqual(flt(supplementary.invoiced_qty), 0)
        self.assertEqual(supplementary.extra_consumption_reason, REASON)

    # ------------------------------------------------------------------ E7
    def test_e7_discarding_releases_the_supplementary_portion_too(self):
        ctx = self.setup_work_order(per_unit=2, order_qty=10, rate=4, receive=60)
        self.supplementary(ctx, 6)
        # Ten units produced so the same batch can ship twice.
        batch, loc = self.book_manufacture(ctx, 10)

        dn = self.deliver(ctx.finished, batch, 5, loc)
        invoice = self.invoice_for(dn.name)
        self.assertEqual(len(invoice.items), 2)

        invoice.delete(ignore_permissions=True)

        for row in self.progress_rows(ctx.wo):
            self.assertEqual(flt(row.reserved_qty), 0, row)
            self.assertEqual(flt(row.invoiced_qty), 0, row)

        # Both portions are billable again on the next shipment.
        dn2 = self.deliver(ctx.finished, batch, 5, loc)
        invoice2 = self.invoice_for(dn2.name)
        self.assertEqual(
            sorted(flt(r.qty) for r in invoice2.items), [6, 10]
        )
        invoice2.submit()
        for row in self.progress_rows(ctx.wo):
            self.assertEqual(flt(row.reserved_qty), 0)
            self.assertGreater(flt(row.invoiced_qty), 0)

    # ------------------------------------------------------------------ E8
    def test_e8_never_shipped_again_stays_visibly_reserved(self):
        """Matches the accepted behaviour for unresolved reservations — not a
        new silent loss."""
        ctx = self.setup_work_order(per_unit=2, order_qty=5, rate=4, receive=60)
        self.supplementary(ctx, 4)
        batch, loc = self.book_manufacture(ctx, 5)

        dn = self.deliver(ctx.finished, batch, 5, loc)
        self.invoice_for(dn.name)   # left open forever

        rows = {r.supplementary_material_request or "standard": r
                for r in self.progress_rows(ctx.wo)}
        supplementary = rows[[k for k in rows if k != "standard"][0]]

        # Reserved and visible, never invoiced, never quietly dropped.
        self.assertEqual(flt(supplementary.reserved_qty), 4)
        self.assertEqual(flt(supplementary.invoiced_qty), 0)
        self.assertEqual(flt(supplementary.consumed_qty), 4)
