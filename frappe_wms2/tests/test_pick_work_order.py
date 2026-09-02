# GATE TESTS — Work Order threaded through the pick chain
# =======================================================
# Run on a disposable site only:
#   MODULE=frappe_wms2.tests.test_pick_work_order \
#       bash apps/frappe_wms2/scripts/run_tests_disposable.sh

import frappe
from frappe.utils import flt, nowdate

from frappe_wms2.tests.setup_records import COMPANY, WAREHOUSE
from frappe_wms2.tests.test_fob_type4 import FOBFixtures
from frappe_wms2.wms.doctype.wms_pick_batch.wms_pick_batch import (
    make_material_request_for_work_order,
)
from frappe_wms2.wms.production import get_work_order_consumption

TYPE4 = "Purchased for customer"


class TestPickWorkOrder(FOBFixtures):
    # ---------------------------------------------------------- helpers

    def work_order_for(self, raw, per_unit=2, qty=5, customer=None,
                       with_sales_order=True):
        """A Work Order, optionally with the Sales Order that carries the
        customer."""
        finished = self.auto_batch_item()
        self.make_bom(finished, [(raw, per_unit)])
        bom = frappe.db.get_value(
            "BOM", {"item": finished, "is_active": 1, "docstatus": 1}, "name"
        )

        wo_dict = {
            "doctype": "Work Order",
            "production_item": finished,
            "bom_no": bom,
            "qty": qty,
            "company": COMPANY,
            "wip_warehouse": self.wip_warehouse,
            "fg_warehouse": WAREHOUSE,
            "source_warehouse": self.wh_trimmings,
            "skip_transfer": 1,
        }
        if with_sales_order:
            so = self.new_sales_order(customer or self.customer_a, finished, qty)
            wo_dict["sales_order"] = so.name

        wo = frappe.get_doc(wo_dict)
        wo.insert(ignore_permissions=True)
        wo.submit()
        self.track("Work Order", wo.name)
        return wo

    def auto_batch_item(self):
        from frappe_wms2.tests.setup_records import make_item

        item = make_item(has_batch_no=True)
        frappe.db.set_value(
            "Item",
            item,
            {
                "item_group": self.group_unrelated,
                "create_new_batch": 1,
                "batch_number_series": f"WOFG-{self.run_token}-.####",
            },
        )
        frappe.clear_document_cache("Item", item)
        self.track("Item", item)
        return item

    def material_for_work_order(self, per_unit=2, wo_qty=5, stock=40,
                                customer=None, with_sales_order=True):
        """An item, a Work Order that needs it, and stock of it.

        In this order on purpose: Type 4 intake refuses an item that is in no
        active BOM, so the BOM (created with the Work Order) has to exist
        before the material can be received.
        """
        item = self.new_material_item(priced=6)
        wo = self.work_order_for(item, per_unit=per_unit, qty=wo_qty,
                                 customer=customer,
                                 with_sales_order=with_sales_order)
        batch, loc = self.receive_material(item, stock, customer)
        return item, batch, loc, wo

    def receive_material(self, item, qty, customer=None):
        loc = self.new_location()
        _pr, batch = self.fob_receipt(
            item, qty, loc, customer or self.customer_a, ownership=TYPE4, rate=3
        )
        return batch, loc

    def mr_for_work_order(self, wo):
        mr = make_material_request_for_work_order(wo.name)
        mr.submit()
        self.track("Material Request", mr.name)
        return mr

    def pick(self, material_requests, submit=True):
        bundle = self.new_bundle(material_requests)
        names = bundle.create_pick_lists([self.group_trim_child])
        self.assertEqual(len(names), 1)   # ONE consolidated list for the picker
        pl = frappe.get_doc("WMS Pick List", names[0])
        self.track("WMS Pick List", pl.name)
        if submit:
            pl.submit()
            pl.reload()
        return bundle, pl

    def book_manufacture(self, wo, raw, raw_batch, produce, per_unit):
        """Consume the picked material out of WIP into the finished good."""
        fg_location = self.new_location()
        se = frappe.get_doc(
            {
                "doctype": "Stock Entry",
                "stock_entry_type": "Manufacture",
                "purpose": "Manufacture",
                "company": COMPANY,
                "work_order": wo.name,
                "bom_no": wo.bom_no,
                "fg_completed_qty": produce,
                "from_bom": 1,
                "posting_date": nowdate(),
                "items": [
                    {
                        "item_code": raw, "qty": per_unit * produce,
                        "uom": "Nos", "stock_uom": "Nos", "conversion_factor": 1,
                        "s_warehouse": self.wip_warehouse,
                        "storage_location": self.wip_location,
                        "use_serial_batch_fields": 1, "batch_no": raw_batch,
                    },
                    {
                        "item_code": wo.production_item, "qty": produce,
                        "uom": "Nos", "stock_uom": "Nos", "conversion_factor": 1,
                        "t_warehouse": WAREHOUSE,
                        "to_storage_location": fg_location,
                        "is_finished_item": 1,
                    },
                ],
            }
        )
        se.insert(ignore_permissions=True)
        se.submit()
        self.track("Stock Entry", se.name)
        return se

    def transfers_of(self, pick_list):
        return frappe.get_all(
            "Stock Entry",
            filters={"name": ("in", [n.strip() for n in
                                     (pick_list.stock_entries or "").split(",") if n.strip()])},
            fields=["name", "work_order", "purpose"],
        )

    # ------------------------------------------------------------------ W1
    def test_w1_work_order_request_picks_like_any_other(self):
        """Same bundling, same FIFO suggestion, same single printed list."""
        raw, batch, loc, wo = self.material_for_work_order(per_unit=2, wo_qty=5)
        mr = self.mr_for_work_order(wo)

        # The Work Order is on the MR header (where v16 keeps it).
        self.assertEqual(frappe.db.get_value("Material Request", mr.name, "work_order"),
                         wo.name)

        bundle, pl = self.pick([mr], submit=False)
        self.assertEqual(bundle.customer, self.customer_a)
        self.assertEqual(bundle.material_requests[0].work_order, wo.name)

        # FIFO suggested the customer's own oldest batch — nothing special.
        self.assertEqual(len(pl.items), 1)
        line = pl.items[0]
        self.assertEqual(line.batch_no, batch)
        self.assertEqual(line.storage_location, loc)
        self.assertEqual(line.work_order, wo.name)
        self.assertEqual(flt(line.qty_to_pick), 10)  # 2/unit x 5

    # ------------------------------------------------------------------ W2
    def test_w2_submitted_pick_feeds_work_order_consumption(self):
        raw, batch, _loc, wo = self.material_for_work_order(per_unit=2, wo_qty=5)
        mr = self.mr_for_work_order(wo)
        _bundle, pl = self.pick([mr])

        entries = self.transfers_of(pl)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].work_order, wo.name)

        # The picked material is now ATTRIBUTED to the Work Order: the Stock
        # Entry carries it, so the movement is findable per Work Order.
        picked = frappe.get_all(
            "Stock Entry",
            filters={"work_order": wo.name, "docstatus": 1},
            fields=["name", "purpose"],
        )
        self.assertIn(entries[0].name, [p.name for p in picked])

        # It is NOT counted as consumption yet, and deliberately so: the pick
        # moves material bulk -> WIP, and the Manufacture booking later moves
        # the SAME units WIP -> finished good. Counting both legs is exactly
        # the double counting found and fixed in Part 0 (40 vs a hand-counted
        # 20). Consumption is counted once, where the material is actually
        # embodied in the product. See the report.
        self.assertEqual(flt(get_work_order_consumption(wo.name).get((raw, batch))), 0)

        # After the Manufacture booking it counts — exactly once.
        self.book_manufacture(wo, raw, batch, produce=5, per_unit=2)
        self.assertEqual(flt(get_work_order_consumption(wo.name).get((raw, batch))), 10)

    # ------------------------------------------------------------------ W3
    def test_w3_two_work_orders_one_list_two_stock_entries(self):
        raw_a, batch_a, _la, wo1 = self.material_for_work_order(
            per_unit=2, wo_qty=5)                              # needs 10
        raw_b, batch_b, _lb, wo2 = self.material_for_work_order(
            per_unit=3, wo_qty=4)                              # needs 12
        mr1, mr2 = self.mr_for_work_order(wo1), self.mr_for_work_order(wo2)

        _bundle, pl = self.pick([mr1, mr2])

        # One list for the picker...
        self.assertEqual(flt(pl.total_picked), 22)
        # ...two Stock Entries in the books, one per Work Order.
        entries = {e.work_order: e for e in self.transfers_of(pl)}
        self.assertEqual(set(entries), {wo1.name, wo2.name})

        totals = {}
        for work_order, entry in entries.items():
            se = frappe.get_doc("Stock Entry", entry.name)
            totals[work_order] = sum(flt(r.qty) for r in se.items)
            # each entry carries ONLY its own lines
            for row in se.items:
                self.assertEqual(
                    row.item_code, raw_a if work_order == wo1.name else raw_b
                )
        self.assertEqual(totals[wo1.name], 10)
        self.assertEqual(totals[wo2.name], 12)
        self.assertEqual(sum(totals.values()), flt(pl.total_picked))

        # Each Work Order's material is findable through its own entry; the
        # picks themselves are not counted as consumption (see W2).
        for work_order, item, qty in ((wo1.name, raw_a, 10), (wo2.name, raw_b, 12)):
            moved = frappe.db.sql(
                """select sum(sed.qty) from `tabStock Entry Detail` sed
                   inner join `tabStock Entry` se on se.name = sed.parent
                   where se.work_order = %s and se.docstatus = 1
                     and sed.item_code = %s""",
                (work_order, item),
            )[0][0]
            self.assertEqual(flt(moved), qty)

    # ------------------------------------------------------------------ W4
    def test_w4_mixed_work_order_and_plain_lines_split_correctly(self):
        raw_wo, _bw, _lw, wo = self.material_for_work_order(
            per_unit=2, wo_qty=4)                              # needs 8
        raw_plain, _bp, _lp, _wo_unused = self.material_for_work_order(
            per_unit=1, wo_qty=1)   # only to give the item a BOM + stock
        mr_wo = self.mr_for_work_order(wo)

        # An ordinary customer-shipment request for the same customer.
        so = self.new_sales_order(self.customer_a, raw_plain, 6)
        mr_plain = self.new_material_request(so, [(raw_plain, 6)])

        _bundle, pl = self.pick([mr_wo, mr_plain])

        entries = self.transfers_of(pl)
        self.assertEqual(len(entries), 2)
        by_wo = {e.work_order: e for e in entries}
        self.assertEqual(set(by_wo), {wo.name, None})

        plain = frappe.get_doc("Stock Entry", by_wo[None].name)
        self.assertEqual([r.item_code for r in plain.items], [raw_plain])
        self.assertEqual(sum(flt(r.qty) for r in plain.items), 6)

        wo_entry = frappe.get_doc("Stock Entry", by_wo[wo.name].name)
        self.assertEqual([r.item_code for r in wo_entry.items], [raw_wo])
        self.assertEqual(sum(flt(r.qty) for r in wo_entry.items), 8)

    # ------------------------------------------------------------------ W5
    def test_w5_override_to_another_customers_batch_is_refused(self):
        """Free to change batch or location; never free to cross customers."""
        raw, _batch_a, _loc_a, wo = self.material_for_work_order(
            per_unit=2, wo_qty=3, customer=self.customer_a)
        # The SAME item, stocked for a DIFFERENT customer.
        batch_b, loc_b = self.receive_material(raw, 20, self.customer_b)
        mr = self.mr_for_work_order(wo)
        _bundle, pl = self.pick([mr], submit=False)

        # FIFO never offered B's batch in the first place.
        self.assertNotIn(batch_b, [r.batch_no for r in pl.items])

        # The picker overrides the suggestion to customer B's batch.
        pl.items[0].batch_no = batch_b
        pl.items[0].storage_location = loc_b
        with self.assertRaises(frappe.ValidationError) as ctx:
            pl.save()
        message = str(ctx.exception)
        self.assertIn(self.customer_b, message)   # who the batch belongs to
        self.assertIn(self.customer_a, message)   # who the pick is for
        frappe.db.rollback()

    # ------------------------------------------------------------------ W6
    def test_w6_work_order_without_a_customer_is_refused(self):
        _raw, _batch, _loc, wo = self.material_for_work_order(
            per_unit=2, wo_qty=3, with_sales_order=False)

        with self.assertRaises(frappe.ValidationError) as ctx:
            make_material_request_for_work_order(wo.name)
        message = str(ctx.exception)
        self.assertIn(wo.name, message)
        self.assertIn("Sales Order", message)
        frappe.db.rollback()

    # ------------------------------------------------------------------ W7
    def test_w7_plain_pick_lists_are_completely_unchanged(self):
        """Regression: no Work Order anywhere -> exactly one Stock Entry with
        no work_order, as before this change."""
        item, batch, _loc, _wo = self.material_for_work_order(
            per_unit=1, wo_qty=1, stock=15)
        so = self.new_sales_order(self.customer_a, item, 7)
        mr = self.new_material_request(so, [(item, 7)])

        _bundle, pl = self.pick([mr])

        self.assertTrue(pl.stock_entry)
        entries = self.transfers_of(pl)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, pl.stock_entry)
        self.assertFalse(entries[0].work_order)

        se = frappe.get_doc("Stock Entry", pl.stock_entry)
        self.assertEqual(se.purpose, "Material Transfer")
        self.assertEqual(sum(flt(r.qty) for r in se.items), 7)
        self.assertEqual(se.items[0].batch_no, batch)
        for line in pl.items:
            self.assertFalse(line.work_order)
