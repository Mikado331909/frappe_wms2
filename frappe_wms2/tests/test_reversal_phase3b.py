# PHASE 3b LIVE TESTS — cancellation and partial return
# =====================================================
# Run (never against a real site — the suite refuses):
#   bash apps/frappe_wms2/scripts/run_tests_disposable.sh
#   MODULE=frappe_wms2.tests.test_reversal_phase3b bash .../run_tests_disposable.sh
#
# Collision-safe like the rest of the suite: random per-run token, throwaway
# location codes outside the real code space, cleanup in tearDownClass.

import frappe
from frappe.utils import flt

try:
    from frappe.tests import IntegrationTestCase as TestBase
except ImportError:  # older frappe
    from frappe.tests.utils import FrappeTestCase as TestBase

from frappe_wms2.tests.setup_records import WAREHOUSE
from frappe_wms2.tests.test_picking_phase3a import PickingFixtures

CANCEL_REASON = "Order cancelled"
RETURN_REASON = "Production not started"
SHORTAGE_REASON = "Batch empty earlier than expected"


class TestReversalPhase3b(PickingFixtures):
    """Reuses the Phase 3a fixtures (customers, WIP pot, factories) so the
    reversal tests start from a real, submitted pick list."""

    # ------------------------------------------------------------- helpers

    def make_submitted_pick(self, qty_available, qty_ordered, picked=None):
        """Receive stock, order it, pick it, submit. Returns (pick list, item,
        batch, location)."""
        item = self.new_item()
        loc = self.new_location()
        batch = self.receive(item, qty_available, loc, self.customer_a)

        so = self.new_sales_order(self.customer_a, item, qty_ordered)
        mr = self.new_material_request(so, [(item, qty_ordered)])
        bundle = self.new_bundle([mr])
        pl = frappe.get_doc(
            "WMS Pick List", bundle.create_pick_lists([self.item_group])[0]
        )
        self.track("WMS Pick List", pl.name)
        if picked is not None:
            pl.items[0].picked_qty = picked
            pl.save()
        pl.submit()
        pl.reload()
        return pl, item, batch, loc

    def wip_qty(self, item_code, batch_no=None):
        from frappe_wms2.wms.reversal import get_wip_balance

        if batch_no:
            return get_wip_balance(
                item_code, batch_no, self.wip_warehouse, self.wip_location
            )
        return sum(
            flt(r.actual_qty)
            for r in frappe.get_all(
                "Stock Ledger Entry",
                filters={
                    "item_code": item_code,
                    "warehouse": self.wip_warehouse,
                    "is_cancelled": 0,
                },
                fields=["actual_qty"],
            )
        )

    def consume_from_wip(self, item_code, batch_no, qty):
        """A downstream document eats part of the WIP pot (production)."""
        from frappe_wms2.tests.setup_records import company_accounts

        cost_center, adjustment = company_accounts()
        se = frappe.get_doc(
            {
                "doctype": "Stock Entry",
                "stock_entry_type": "Material Issue",
                "purpose": "Material Issue",
                "company": self.pick_company(),
                "items": [
                    {
                        "item_code": item_code,
                        "qty": qty,
                        "uom": "Nos",
                        "stock_uom": "Nos",
                        "conversion_factor": 1,
                        "s_warehouse": self.wip_warehouse,
                        "storage_location": self.wip_location,
                        "use_serial_batch_fields": 1,
                        "batch_no": batch_no,
                        "expense_account": adjustment,
                        "cost_center": cost_center,
                        "allow_zero_valuation_rate": 1,
                    }
                ],
            }
        )
        se.insert(ignore_permissions=True)
        se.submit()
        self.track("Stock Entry", se.name)
        return se

    def pick_company(self):
        from frappe_wms2.tests.setup_records import COMPANY

        return COMPANY

    # ------------------------------------------------------------------ R1
    def test_r1_full_cancel_restores_origin_and_empties_wip(self):
        pl, item, batch, loc = self.make_submitted_pick(12, 7)

        # After the pick: 5 left on the shelf, 7 in the WIP pot.
        self.assertEqual(self.item_balance_by_location(item)[(batch, loc)], 5)
        self.assertEqual(self.wip_qty(item, batch), 7)

        result = pl.cancel_pick(reason=CANCEL_REASON, comment="customer pulled the order")
        pl.reload()

        # Exact reversal: the origin batch+location is whole again...
        self.assertEqual(self.item_balance_by_location(item)[(batch, loc)], 12)
        # ...and this pick list's quantity is gone from WIP.
        self.assertEqual(self.wip_qty(item, batch), 0)

        # Document state + audit trail.
        self.assertEqual(pl.docstatus, 2)
        self.assertEqual(pl.cancel_reason, CANCEL_REASON)
        self.assertEqual(flt(pl.items[0].returned_qty), 7)
        self.assertEqual(flt(pl.total_returned), 7)

        se = frappe.get_doc("Stock Entry", result["stock_entry"])
        self.assertEqual(se.docstatus, 1)
        self.assertEqual(se.items[0].s_warehouse, self.wip_warehouse)
        self.assertEqual(se.items[0].t_warehouse, WAREHOUSE)
        self.assertEqual(se.items[0].to_storage_location, loc)
        self.assertIn("[WMS-REVERSAL]", se.remarks)

        # Ownership travels with the batch — untouched by the reversal.
        stamp = frappe.db.get_value(
            "Batch", batch, ["wms_customer", "wms_ownership_type"], as_dict=True
        )
        self.assertEqual(stamp.wms_customer, self.customer_a)

        # Reservation follows reality: the demand is open again.
        bundle = frappe.get_doc("WMS Pick Batch", pl.pick_batch)
        bundle.build_demand()
        self.assertEqual(flt(bundle.demand_items[0].qty_open), 7)

    # ------------------------------------------------------------------ R2
    def test_r2_partial_return_moves_only_that_qty(self):
        pl, item, batch, loc = self.make_submitted_pick(10, 8)
        row = pl.items[0]
        self.assertEqual(flt(row.picked_qty), 8)
        self.assertEqual(self.wip_qty(item, batch), 8)

        pl.return_line(row_name=row.name, qty=3, reason=RETURN_REASON)
        pl.reload()

        # Only 3 went back; the rest stays in WIP.
        self.assertEqual(self.item_balance_by_location(item)[(batch, loc)], 5)
        self.assertEqual(self.wip_qty(item, batch), 5)
        self.assertEqual(flt(pl.items[0].returned_qty), 3)
        self.assertEqual(pl.docstatus, 1)  # still a live pick list

        # Repeatable: a second return of the remainder.
        pl.return_line(row_name=row.name, qty=5, reason=RETURN_REASON)
        pl.reload()
        self.assertEqual(self.item_balance_by_location(item)[(batch, loc)], 10)
        self.assertEqual(self.wip_qty(item, batch), 0)
        self.assertEqual(flt(pl.items[0].returned_qty), 8)

        # ...and nothing beyond what was picked.
        with self.assertRaises(frappe.ValidationError):
            pl.return_line(row_name=pl.items[0].name, qty=1, reason=RETURN_REASON)

    # ------------------------------------------------------------------ R3
    def test_r3_refuses_once_wip_qty_consumed_downstream(self):
        pl, item, batch, loc = self.make_submitted_pick(10, 6)
        self.assertEqual(self.wip_qty(item, batch), 6)

        # Production takes 2 out of the WIP pot.
        consumer = self.consume_from_wip(item, batch, 2)
        self.assertEqual(self.wip_qty(item, batch), 4)

        # Full cancel is refused, naming the consuming document.
        with self.assertRaises(frappe.ValidationError) as ctx:
            pl.cancel_pick(reason=CANCEL_REASON)
        message = str(ctx.exception)
        self.assertIn(consumer.name, message)
        self.assertIn("manual stock correction", message)

        # Returning more than is left is refused too...
        pl.reload()
        with self.assertRaises(frappe.ValidationError):
            pl.return_line(row_name=pl.items[0].name, qty=5, reason=RETURN_REASON)

        # ...while what IS still untouched can be returned.
        pl.reload()
        pl.return_line(row_name=pl.items[0].name, qty=4, reason=RETURN_REASON)
        pl.reload()
        self.assertEqual(flt(pl.items[0].returned_qty), 4)
        self.assertEqual(self.wip_qty(item, batch), 0)
        self.assertEqual(self.item_balance_by_location(item)[(batch, loc)], 8)

        # Nothing further: the consumed 2 are a manual correction.
        with self.assertRaises(frappe.ValidationError):
            pl.return_line(row_name=pl.items[0].name, qty=1, reason=RETURN_REASON)

    # ------------------------------------------------------------------ R4
    def test_r4_both_actions_require_a_cancel_return_reason(self):
        pl, item, batch, loc = self.make_submitted_pick(6, 4)
        before = self.item_balance_by_location(item)[(batch, loc)]

        # No reason at all.
        for call in (
            lambda: pl.cancel_pick(reason=None),
            lambda: pl.return_line(row_name=pl.items[0].name, qty=1, reason=None),
            lambda: pl.return_line(row_name=pl.items[0].name, qty=1, reason=""),
        ):
            with self.assertRaises(frappe.ValidationError):
                call()

        # A reason from the wrong category is refused as well.
        self.assertTrue(frappe.db.exists("WMS Pick Reason", SHORTAGE_REASON))
        self.assertFalse(
            frappe.db.get_value(
                "WMS Pick Reason", SHORTAGE_REASON, "applies_to_cancel_return"
            )
        )
        with self.assertRaises(frappe.ValidationError):
            pl.cancel_pick(reason=SHORTAGE_REASON)
        with self.assertRaises(frappe.ValidationError):
            pl.return_line(
                row_name=pl.items[0].name, qty=1, reason=SHORTAGE_REASON
            )

        # Nothing moved during any of those refusals.
        self.assertEqual(self.item_balance_by_location(item)[(batch, loc)], before)
        self.assertEqual(self.wip_qty(item, batch), 4)

        # Cancel reasons exist and are flagged for this use.
        self.assertTrue(
            frappe.db.get_value(
                "WMS Pick Reason", CANCEL_REASON, "applies_to_cancel_return"
            )
        )

    # ------------------------------------------------------------------ R5
    def test_r5_plain_cancel_button_still_refuses(self):
        """The standard Cancel must not post an unaudited reversal."""
        pl, item, batch, _loc = self.make_submitted_pick(5, 3)

        with self.assertRaises(frappe.ValidationError) as ctx:
            pl.cancel()
        self.assertIn("Cancel Pick List", str(ctx.exception))

        # Untouched: still submitted, still in WIP.
        pl.reload()
        self.assertEqual(pl.docstatus, 1)
        self.assertEqual(self.wip_qty(item, batch), 3)

    # ------------------------------------------------------------------ R6
    def test_r6_shared_wip_pot_attributes_qty_per_pick_list(self):
        """Two pick lists parking the SAME batch in the pot must not be able
        to reverse each other's quantity."""
        item = self.new_item()
        loc = self.new_location()
        batch = self.receive(item, 20, loc, self.customer_a)

        picks = []
        for qty in (5, 6):
            so = self.new_sales_order(self.customer_a, item, qty)
            mr = self.new_material_request(so, [(item, qty)])
            bundle = self.new_bundle([mr])
            pl = frappe.get_doc(
                "WMS Pick List", bundle.create_pick_lists([self.item_group])[0]
            )
            self.track("WMS Pick List", pl.name)
            pl.submit()
            pl.reload()
            picks.append(pl)

        self.assertEqual(self.wip_qty(item, batch), 11)

        # Each list may only reverse its own quantity.
        with self.assertRaises(frappe.ValidationError):
            picks[0].return_line(
                row_name=picks[0].items[0].name, qty=7, reason=RETURN_REASON
            )

        picks[0].reload()
        picks[0].return_line(
            row_name=picks[0].items[0].name, qty=5, reason=RETURN_REASON
        )
        self.assertEqual(self.wip_qty(item, batch), 6)   # the other list's stock
        self.assertEqual(self.item_balance_by_location(item)[(batch, loc)], 14)

        # The second list can still cancel its own 6 in full.
        picks[1].reload()
        picks[1].cancel_pick(reason=CANCEL_REASON)
        self.assertEqual(self.wip_qty(item, batch), 0)
        self.assertEqual(self.item_balance_by_location(item)[(batch, loc)], 20)
