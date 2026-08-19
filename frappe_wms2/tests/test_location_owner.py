# GATE TESTS — one storage location, one owner (+ Item 1 regression)
# ==================================================================
# Run on a disposable site only:
#   MODULE=frappe_wms2.tests.test_location_owner \
#       bash apps/frappe_wms2/scripts/run_tests_disposable.sh

import frappe
from frappe.utils import flt

from frappe_wms2.tests.setup_records import (
    COMPANY,
    WAREHOUSE,
    company_accounts,
    make_batch,
    make_purchase_receipt,
)
from frappe_wms2.tests.test_fob_type4 import FOBFixtures
from frappe_wms2.wms.customer_warehouse import get_or_create_customer_warehouse
from frappe_wms2.wms.fob import TRIMMINGS
from frappe_wms2.wms.location_owner import (
    OWN_STOCK,
    resolve_location_owner,
)

OWN_USE = "Own use"
TYPE2 = "Supplied by customer"
TYPE3 = "Purchased with customer"
TYPE4 = "Purchased for customer"


class TestLocationOwner(FOBFixtures):
    def setUp(self):
        super().setUp()
        # Default for these tests: exclusive locations.
        self.set_sharing(False)

    def set_sharing(self, allowed):
        frappe.db.set_single_value(
            "WMS Settings", "allow_own_stock_with_customer_stock", 1 if allowed else 0
        )
        frappe.clear_cache(doctype="WMS Settings")

    # ---------------------------------------------------------- factories

    def receive_own(self, location, qty=5, item=None, batch=None,
                    warehouse=WAREHOUSE):
        item = item or self.new_material_item()
        batch = batch or self.track("Batch", make_batch(item))
        make_purchase_receipt(
            [{
                "item_code": item, "qty": qty, "rate": 2,
                "warehouse": warehouse, "storage_location": location,
                "use_serial_batch_fields": 1, "batch_no": batch,
                "wms_ownership_type": OWN_USE,
            }]
        )
        return item, batch

    def receive_type2(self, location, customer, qty=5, item=None, batch=None,
                      warehouse=WAREHOUSE):
        item = item or self.new_material_item()
        batch = batch or self.track("Batch", make_batch(item))
        make_purchase_receipt(
            [{
                "item_code": item, "qty": qty, "rate": 0,
                "warehouse": warehouse, "storage_location": location,
                "use_serial_batch_fields": 1, "batch_no": batch,
                "wms_ownership_type": TYPE2, "wms_customer": customer,
            }]
        )
        return item, batch

    def receive_type4(self, location, customer, qty=5):
        item = self.new_material_item(priced=8)
        finished = self.new_finished_item()
        self.make_bom(finished, [(item, 1)])
        _pr, batch = self.fob_receipt(item, qty, location, customer,
                                      ownership=TYPE4, rate=3)
        return item, batch

    def issue_all(self, item, batch, location, qty, warehouse=WAREHOUSE):
        """Empty a (batch, location) again — a Material Issue is not intake,
        so the rule does not apply to it."""
        cost_center, adjustment = company_accounts()
        se = frappe.get_doc(
            {
                "doctype": "Stock Entry",
                "stock_entry_type": "Material Issue",
                "purpose": "Material Issue",
                "company": COMPANY,
                "items": [{
                    "item_code": item, "qty": qty, "uom": "Nos",
                    "stock_uom": "Nos", "conversion_factor": 1,
                    "s_warehouse": warehouse, "storage_location": location,
                    "use_serial_batch_fields": 1, "batch_no": batch,
                    "expense_account": adjustment, "cost_center": cost_center,
                    "allow_zero_valuation_rate": 1,
                }],
            }
        )
        se.insert(ignore_permissions=True)
        se.submit()
        self.track("Stock Entry", se.name)
        return se

    def relocate(self, item, batch, qty, from_location, to_location,
                 warehouse=WAREHOUSE):
        """Move stock between locations. Material Transfer is NOT covered by
        the intake rule — this is the documented backdoor, used here to build
        a pre-existing conflict on purpose."""
        se = frappe.get_doc(
            {
                "doctype": "Stock Entry",
                "stock_entry_type": "Material Transfer",
                "purpose": "Material Transfer",
                "company": COMPANY,
                "items": [{
                    "item_code": item, "qty": qty, "uom": "Nos",
                    "stock_uom": "Nos", "conversion_factor": 1,
                    "s_warehouse": warehouse, "storage_location": from_location,
                    "t_warehouse": warehouse, "to_storage_location": to_location,
                    "use_serial_batch_fields": 1, "batch_no": batch,
                }],
            }
        )
        se.insert(ignore_permissions=True)
        se.submit()
        self.track("Stock Entry", se.name)
        return se

    # ------------------------------------------------------------------ L1
    def test_l1_own_stock_into_empty_location(self):
        loc = self.new_location()
        self.assertIsNone(resolve_location_owner(loc))

        item, batch = self.receive_own(loc, qty=5)
        self.assertEqual(resolve_location_owner(loc), OWN_STOCK)

        # L2 — once it is empty again, a customer may use the same location.
        self.issue_all(item, batch, loc, 5)
        self.assertIsNone(resolve_location_owner(loc))

        self.receive_type2(loc, self.customer_a)
        self.assertEqual(resolve_location_owner(loc), self.customer_a)

    # ------------------------------------------------------------------ L3
    def test_l3_own_and_customer_conflict_when_sharing_is_off(self):
        # customer first, then own stock
        loc_a = self.new_location()
        self.receive_type2(loc_a, self.customer_a)
        with self.assertRaises(frappe.ValidationError) as ctx:
            self.receive_own(loc_a)
        self.assertIn(self.customer_a, str(ctx.exception))
        frappe.db.rollback()

        # own stock first, then a customer
        loc_b = self.new_location()
        self.receive_own(loc_b)
        with self.assertRaises(frappe.ValidationError) as ctx2:
            self.receive_type2(loc_b, self.customer_a)
        self.assertIn("own stock", str(ctx2.exception))
        frappe.db.rollback()

    # ------------------------------------------------------------------ L4
    def test_l4_sharing_on_exempts_own_stock_only(self):
        self.set_sharing(True)

        # Own stock does not claim the location...
        loc = self.new_location()
        self.receive_own(loc)
        self.assertIsNone(resolve_location_owner(loc))

        # ...so a customer may still receive there, and vice versa.
        self.receive_type2(loc, self.customer_a)
        self.assertEqual(resolve_location_owner(loc), self.customer_a)
        self.receive_own(loc)  # own stock is not blocked either
        self.assertEqual(resolve_location_owner(loc), self.customer_a)

        # A SECOND customer is refused regardless of the setting.
        with self.assertRaises(frappe.ValidationError) as ctx:
            self.receive_type2(loc, self.customer_b)
        self.assertIn(self.customer_a, str(ctx.exception))
        frappe.db.rollback()

    # ------------------------------------------------------------------ L5
    def test_l5_two_customers_never_share_a_location(self):
        for sharing in (False, True):
            self.set_sharing(sharing)
            loc = self.new_location()
            self.receive_type2(loc, self.customer_a)

            with self.assertRaises(frappe.ValidationError) as ctx:
                self.receive_type2(loc, self.customer_b)
            message = str(ctx.exception)
            self.assertIn(self.customer_a, message)   # names who holds it
            self.assertIn(self.customer_b, message)
            frappe.db.rollback()

    # ------------------------------------------------------------------ L6
    def test_l6_same_customer_may_mix_ownership_types(self):
        loc = self.new_location()
        self.receive_type2(loc, self.customer_a)

        # Type 4 for the SAME customer: one owner, different ownership type.
        item4, batch4 = self.receive_type4(loc, self.customer_a)
        self.assertEqual(resolve_location_owner(loc), self.customer_a)

        # It really landed there (in the customer's own warehouse).
        wh = get_or_create_customer_warehouse(self.customer_a, TRIMMINGS)
        qty = frappe.db.sql(
            """select sum(sle.actual_qty) from `tabStock Ledger Entry` sle
               where sle.item_code=%s and sle.warehouse=%s
                 and sle.storage_location=%s and sle.is_cancelled=0""",
            (item4, wh, loc),
        )[0][0]
        self.assertEqual(flt(qty), 5)

        # And a Type 3 receipt for the same customer is fine too.
        item3 = self.new_material_item(priced=9)
        self.fob_receipt(item3, 3, loc, self.customer_a, ownership=TYPE3, rate=4)
        self.assertEqual(resolve_location_owner(loc), self.customer_a)

    # ------------------------------------------------------------------ L7
    def test_l7_location_frees_itself_when_stock_leaves(self):
        loc = self.new_location()
        item, batch = self.receive_type2(loc, self.customer_a, qty=4)
        self.assertEqual(resolve_location_owner(loc), self.customer_a)
        # Committed: the expected refusal below is followed by a rollback,
        # which would otherwise discard this setup too.
        frappe.db.commit()

        # Blocked while A's stock is there...
        with self.assertRaises(frappe.ValidationError):
            self.receive_type2(loc, self.customer_b)
        frappe.db.rollback()

        # ...consumed to zero, no manual "free up" step anywhere...
        self.issue_all(item, batch, loc, 4)
        self.assertIsNone(resolve_location_owner(loc))

        # ...and customer B may now use it.
        self.receive_type2(loc, self.customer_b)
        self.assertEqual(resolve_location_owner(loc), self.customer_b)

    # ------------------------------------------------------------------ L8
    def test_l8_pre_existing_two_owner_location_refuses_without_guessing(self):
        """Built through a Material Transfer, which this phase does NOT cover
        — the same way stock received before the rule existed could look."""
        loc = self.new_location()
        other = self.new_location()

        item_a, batch_a = self.receive_type2(loc, self.customer_a, qty=3)
        item_b, batch_b = self.receive_type2(other, self.customer_b, qty=3)
        self.relocate(item_b, batch_b, 3, other, loc)  # backdoor, on purpose
        frappe.db.commit()  # survives the rollback after the refusal below

        # Both owners are now present at one location.
        from frappe_wms2.wms.location_owner import get_location_owners

        self.assertEqual(
            set(get_location_owners(loc)), {self.customer_a, self.customer_b}
        )

        # Resolving the owner refuses rather than guessing which one wins.
        with self.assertRaises(frappe.ValidationError):
            resolve_location_owner(loc)

        # Any new receipt there refuses and NAMES BOTH — never picks a side,
        # not even for an owner that is genuinely already present.
        with self.assertRaises(frappe.ValidationError) as ctx:
            self.receive_type2(loc, self.customer_a)
        message = str(ctx.exception)
        self.assertIn(self.customer_a, message)
        self.assertIn(self.customer_b, message)
        self.assertIn("MORE THAN ONE", message)
        frappe.db.rollback()

    # ------------------------------------------------ Item 1 regression
    def test_item1_prefilled_wrong_warehouse_is_corrected_not_refused(self):
        """ERPNext's UI pre-fills a default warehouse on a new row. That used
        to hit a throw which rolled back the customer warehouse created in the
        same transaction — the error then named a warehouse that no longer
        existed."""
        item = self.new_material_item(priced=6)
        finished = self.new_finished_item()
        self.make_bom(finished, [(item, 1)])
        loc = self.new_location()
        batch = self.track("Batch", make_batch(item))

        expected = get_or_create_customer_warehouse(self.customer_b, TRIMMINGS)
        # Make sure the resolver would have to CREATE it, as on a first receipt.
        frappe.delete_doc("Warehouse", expected, force=True, ignore_permissions=True)
        frappe.db.commit()
        self.assertFalse(frappe.db.exists("Warehouse", expected))

        # Row arrives pre-filled with the WRONG warehouse (ERPNext's default).
        pr = make_purchase_receipt(
            [{
                "item_code": item, "qty": 4, "rate": 3,
                "warehouse": WAREHOUSE,          # deliberately wrong
                "storage_location": loc,
                "use_serial_batch_fields": 1, "batch_no": batch,
                "wms_ownership_type": TYPE4, "wms_customer": self.customer_b,
            }]
        )

        # Corrected silently, no error.
        self.assertEqual(pr.items[0].warehouse, expected)

        # And the warehouse really persisted — it was not rolled back.
        frappe.db.commit()
        self.assertTrue(frappe.db.exists("Warehouse", expected))
        self.track("Warehouse", expected)

        sle = frappe.db.get_value(
            "Stock Ledger Entry",
            {"voucher_no": pr.name, "is_cancelled": 0},
            ["warehouse", "actual_qty"],
            as_dict=True,
        )
        self.assertEqual(sle.warehouse, expected)
        self.assertEqual(flt(sle.actual_qty), 4)
