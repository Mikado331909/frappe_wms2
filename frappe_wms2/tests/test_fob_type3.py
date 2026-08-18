# PART 2 GATE TESTS — Type 3 "Purchased with customer" (FOB direct)
# =================================================================
# Pilot scope. Run on a disposable site only:
#   MODULE=frappe_wms2.tests.test_fob_type3 \
#       bash apps/frappe_wms2/scripts/run_tests_disposable.sh

import frappe
from frappe.utils import flt

from frappe_wms2.tests.setup_records import COMPANY, WAREHOUSE, make_batch
from frappe_wms2.tests.test_fob_type4 import FOBFixtures
from frappe_wms2.wms.customer_warehouse import get_or_create_customer_warehouse
from frappe_wms2.wms.fob import TRIMMINGS

TYPE3 = "Purchased with customer"
TYPE2 = "Supplied by customer"


class TestFOBType3(FOBFixtures):
    """Reuses the Part 1 fixtures (material groups, price list, customer
    warehouses, factories); only the Type 3 flow is exercised here."""

    # ---------------------------------------------------------- helpers

    def type3_receipt(self, item, qty, location, rate=4, customer=None, batch=None):
        return self.fob_receipt(
            item, qty, location, customer or self.customer_a,
            ownership=TYPE3, rate=rate, batch=batch,
        )

    def fob_sales_for(self, source_name):
        return frappe.get_all(
            "WMS FOB Sale",
            filters={"source_name": source_name},
            fields=["name", "item_code", "batch_no", "qty", "rate", "customer",
                    "ownership_type", "warehouse", "storage_location",
                    "sales_invoice", "restock_stock_entry"],
        )

    def batch_qty(self, item, batch, warehouse):
        from frappe_wms2.wms.picking import get_stock_by_batch_location

        return sum(
            flt(r.qty)
            for r in get_stock_by_batch_location([item], warehouse=warehouse)
            if r.batch_no == batch
        )

    def batch_stamp(self, batch):
        return frappe.db.get_value(
            "Batch", batch, ["wms_ownership_type", "wms_customer"], as_dict=True
        )

    # ------------------------------------------------------------------ S1
    def test_s1_type3_intake_at_cost_in_customer_warehouse(self):
        item = self.new_material_item(priced=11)
        loc = self.new_location()
        pr, batch = self.type3_receipt(item, 10, loc, rate=4)

        expected_wh = get_or_create_customer_warehouse(self.customer_a, TRIMMINGS)
        self.assertEqual(pr.items[0].warehouse, expected_wh)

        sle = frappe.db.get_value(
            "Stock Ledger Entry",
            {"voucher_no": pr.name, "is_cancelled": 0},
            ["actual_qty", "stock_value_difference", "storage_location"],
            as_dict=True,
        )
        self.assertEqual(flt(sle.actual_qty), 10)
        self.assertEqual(flt(sle.stock_value_difference), 40)  # full cost
        self.assertEqual(sle.storage_location, loc)

        stamp = self.batch_stamp(batch)
        self.assertEqual(stamp.wms_ownership_type, TYPE3)
        self.assertEqual(stamp.wms_customer, self.customer_a)

    # ------------------------------------------------------------------ S2
    def test_s2_intake_creates_one_draft_update_stock_invoice(self):
        item = self.new_material_item(priced=11)
        loc = self.new_location()
        pr, batch = self.type3_receipt(item, 6, loc, rate=4)

        sales = self.fob_sales_for(pr.name)
        self.assertEqual(len(sales), 1)
        sale = sales[0]
        self.assertEqual(sale.batch_no, batch)
        self.assertEqual(sale.storage_location, loc)
        self.assertEqual(flt(sale.rate), 11)  # Price List, not cost
        self.assertFalse(sale.restock_stock_entry)

        invoice = frappe.get_doc("Sales Invoice", sale.sales_invoice)
        self.track("Sales Invoice", invoice.name)
        self.assertEqual(invoice.docstatus, 0)          # DRAFT
        self.assertEqual(invoice.update_stock, 1)       # no Delivery Note
        self.assertEqual(invoice.customer, self.customer_a)
        self.assertEqual(len(invoice.items), 1)

        line = invoice.items[0]
        self.assertEqual(line.item_code, item)
        self.assertEqual(flt(line.qty), 6)
        self.assertEqual(flt(line.rate), 11)
        self.assertEqual(line.batch_no, batch)
        self.assertEqual(line.warehouse, pr.items[0].warehouse)
        self.assertEqual(line.storage_location, loc)

        # No Delivery Note anywhere in this flow.
        self.assertFalse(
            frappe.db.exists(
                "Delivery Note Item", {"item_code": item, "docstatus": ("<", 2)}
            )
        )

        # Idempotency: re-running the intake hook creates no second invoice.
        from frappe_wms2.wms.fob_direct import (
            create_concept_invoices_purchase_receipt,
        )

        create_concept_invoices_purchase_receipt(frappe.get_doc(pr.doctype, pr.name))
        self.assertEqual(len(self.fob_sales_for(pr.name)), 1)

    # ------------------------------------------------------------------ S3
    def test_s3_before_confirmation_stock_stays_at_cost(self):
        item = self.new_material_item(priced=11)
        loc = self.new_location()
        pr, batch = self.type3_receipt(item, 8, loc, rate=4)
        wh = pr.items[0].warehouse

        # Still there, still at cost.
        self.assertEqual(self.batch_qty(item, batch, wh), 8)
        value = frappe.db.get_value(
            "Stock Ledger Entry",
            {"voucher_no": pr.name, "is_cancelled": 0},
            "stock_value_difference",
        )
        self.assertEqual(flt(value), 32)

        # No restock exists yet.
        sale = self.fob_sales_for(pr.name)[0]
        self.assertFalse(sale.restock_stock_entry)
        self.assertFalse(
            frappe.db.exists("Stock Entry", {"wms_fob_restock_for": sale.name})
        )

    # ------------------------------------------------------------------ S4
    def test_s4_confirming_invoice_restocks_at_zero_value(self):
        item = self.new_material_item(priced=11)
        loc = self.new_location()
        pr, batch = self.type3_receipt(item, 10, loc, rate=4)
        wh = pr.items[0].warehouse
        sale = self.fob_sales_for(pr.name)[0]

        qty_before = self.batch_qty(item, batch, wh)
        self.assertEqual(qty_before, 10)

        invoice = frappe.get_doc("Sales Invoice", sale.sales_invoice)
        self.track("Sales Invoice", invoice.name)
        invoice.submit()

        # Exactly one restock entry, and it is the same everything.
        sale_after = self.fob_sales_for(pr.name)[0]
        self.assertTrue(sale_after.restock_stock_entry)
        entries = frappe.get_all(
            "Stock Entry",
            filters={"wms_fob_restock_for": sale.name, "docstatus": 1},
            pluck="name",
        )
        self.assertEqual(len(entries), 1)

        se = frappe.get_doc("Stock Entry", entries[0])
        self.track("Stock Entry", se.name)
        row = se.items[0]
        self.assertEqual(row.t_warehouse, wh)               # same warehouse
        self.assertEqual(row.to_storage_location, loc)      # same location
        self.assertEqual(row.batch_no, batch)               # same batch
        self.assertEqual(flt(row.qty), 10)                  # same qty
        self.assertEqual(flt(row.basic_rate), 0)            # zero valuation
        self.assertFalse(row.get("s_warehouse"))            # nothing moves out

        # Net quantity unchanged across intake -> sale -> restock.
        self.assertEqual(self.batch_qty(item, batch, wh), qty_before)

        # The restock itself carries no value...
        restock_value = frappe.db.get_value(
            "Stock Ledger Entry",
            {"voucher_no": se.name, "is_cancelled": 0},
            "stock_value_difference",
        )
        self.assertEqual(flt(restock_value), 0)

        # ...while the invoice posted real revenue and COGS.
        gl = frappe.get_all(
            "GL Entry",
            filters={"voucher_no": invoice.name, "is_cancelled": 0},
            fields=["account", "debit", "credit"],
        )
        self.assertTrue(gl, "the confirmed sale must hit the general ledger")
        self.assertEqual(
            round(sum(flt(g.debit) - flt(g.credit) for g in gl), 6), 0.0
        )
        self.assertTrue(
            any(flt(g.credit) == 110 or flt(g.debit) == 110 for g in gl),
            f"expected the 10 x 11 sale in the GL, got {gl}",
        )

    # ------------------------------------------------------------------ S5
    def test_s5_batch_stamp_is_identical_before_and_after(self):
        item = self.new_material_item(priced=11)
        loc = self.new_location()
        pr, batch = self.type3_receipt(item, 5, loc, rate=4)

        before = self.batch_stamp(batch)
        invoice = frappe.get_doc(
            "Sales Invoice", self.fob_sales_for(pr.name)[0].sales_invoice
        )
        self.track("Sales Invoice", invoice.name)
        invoice.submit()

        after = self.batch_stamp(batch)
        self.assertEqual(dict(before), dict(after))
        self.assertEqual(after.wms_ownership_type, TYPE3)  # never re-labelled
        self.assertEqual(after.wms_customer, self.customer_a)

        # And the anti-mixing rule still accepts a top-up of the same batch.
        from frappe_wms2.wms.ownership import _assert_batch_compatible

        _assert_batch_compatible(batch, TYPE3, self.customer_a, "check")

    # ------------------------------------------------------------------ S6
    def test_s6_missing_price_fails_the_whole_intake(self):
        item = self.new_material_item()  # deliberately no Item Price
        loc = self.new_location()

        with self.assertRaises(frappe.ValidationError) as ctx:
            self.type3_receipt(item, 4, loc, rate=4)
        message = str(ctx.exception)
        self.assertIn(item, message)
        self.assertIn(self.customer_a, message)
        frappe.db.rollback()

        # Scoped to THIS item — a global count would also be affected by the
        # rollback discarding earlier tests' uncommitted work.
        self.assertFalse(
            frappe.db.exists("WMS FOB Sale", {"item_code": item}),
            "no audit row may survive a refused intake",
        )
        self.assertFalse(
            frappe.db.exists("Sales Invoice Item", {"item_code": item}),
            "no orphan concept invoice may survive a refused intake",
        )
        # Nothing of the receipt survived either.
        self.assertFalse(
            frappe.db.exists(
                "Stock Ledger Entry", {"item_code": item, "is_cancelled": 0}
            )
        )

    # ------------------------------------------------------------------ S7
    def test_s7_cancelled_concept_invoice_parks_the_material(self):
        item = self.new_material_item(priced=11)
        loc = self.new_location()
        pr, batch = self.type3_receipt(item, 7, loc, rate=4)
        wh = pr.items[0].warehouse
        sale = self.fob_sales_for(pr.name)[0]

        invoice = frappe.get_doc("Sales Invoice", sale.sales_invoice)
        invoice.delete(ignore_permissions=True)  # accountant discards it

        # Valid parked state: still there, still at cost, no restock, no error.
        self.assertEqual(self.batch_qty(item, batch, wh), 7)
        self.assertEqual(
            flt(frappe.db.get_value(
                "Stock Ledger Entry",
                {"voucher_no": pr.name, "is_cancelled": 0},
                "stock_value_difference",
            )),
            28,
        )
        self.assertFalse(
            frappe.db.get_value("WMS FOB Sale", sale.name, "restock_stock_entry")
        )
        self.assertFalse(
            frappe.db.exists("Stock Entry", {"wms_fob_restock_for": sale.name})
        )
        # The stamp is untouched — it stays this customer's material.
        self.assertEqual(self.batch_stamp(batch).wms_ownership_type, TYPE3)

    # ------------------------------------------------------------------ S8
    def test_s8_cost_and_zero_valued_batches_coexist(self):
        """A still-at-cost Type 3 batch and an already-zero-valued batch of
        the SAME item in the SAME customer warehouse keep their own rates."""
        item = self.new_material_item(priced=11)
        wh = get_or_create_customer_warehouse(self.customer_a, TRIMMINGS)

        # Batch A: Type 3, sold and restocked -> ends up at zero value.
        loc_a = self.new_location()
        pr_a, batch_a = self.type3_receipt(item, 5, loc_a, rate=20)
        invoice = frappe.get_doc(
            "Sales Invoice", self.fob_sales_for(pr_a.name)[0].sales_invoice
        )
        self.track("Sales Invoice", invoice.name)
        invoice.submit()

        # Batch B: Type 3, still at cost (its invoice left as a concept).
        loc_b = self.new_location()
        pr_b, batch_b = self.type3_receipt(item, 5, loc_b, rate=30)

        # Both batches valued independently.
        self.assertEqual(
            frappe.db.get_value("Batch", batch_a, "use_batchwise_valuation"), 1
        )
        self.assertEqual(
            frappe.db.get_value("Batch", batch_b, "use_batchwise_valuation"), 1
        )

        # Batch B still carries its own cost of 30 — untouched by A's sale
        # and zero-valued restock in the same warehouse.
        intake_b = frappe.db.get_value(
            "Stock Ledger Entry",
            {"voucher_no": pr_b.name, "is_cancelled": 0},
            ["stock_value_difference", "actual_qty"],
            as_dict=True,
        )
        self.assertEqual(flt(intake_b.stock_value_difference), 150)  # 5 x 30

        rates_b = [
            flt(r.incoming_rate)
            for r in frappe.get_all(
                "Serial and Batch Entry",
                filters={"batch_no": batch_b},
                fields=["incoming_rate", "qty"],
            )
            if flt(r.qty) > 0
        ]
        self.assertTrue(all(r == 30 for r in rates_b), rates_b)

        # Quantities are both present in the same warehouse.
        self.assertEqual(self.batch_qty(item, batch_a, wh), 5)
        self.assertEqual(self.batch_qty(item, batch_b, wh), 5)
