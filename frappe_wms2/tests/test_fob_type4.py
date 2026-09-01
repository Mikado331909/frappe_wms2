# PART 1 GATE TESTS — Type 4 "Purchased for customer" (FOB per production)
# ========================================================================
# Run on a disposable site only:
#   MODULE=frappe_wms2.tests.test_fob_type4 \
#       bash apps/frappe_wms2/scripts/run_tests_disposable.sh
#
# Collision-safe: random per-run token, throwaway Item Groups and locations,
# cleanup in tearDownClass — same patterns as the rest of the suite.

import frappe
from frappe.utils import flt, nowdate

from frappe_wms2.tests.setup_records import COMPANY, WAREHOUSE, make_batch, make_item
from frappe_wms2.tests.test_picking_phase3a import PickingFixtures
from frappe_wms2.wms.fob import FABRICS, TRIMMINGS
from frappe_wms2.wms.material_warehouse import get_material_warehouse

TYPE4 = "Purchased for customer"
OWN_USE = "Own use"


class FOBFixtures(PickingFixtures):
    """Shared FOB fixtures (material groups, price list, factories).

    Split from the tests themselves so the Part 2 (Type 3) module can reuse
    them without re-running Part 1's gate.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_material_groups()
        cls.setup_material_warehouses()
        cls.setup_price_list()

    # ------------------------------------------------------------ fixtures

    @classmethod
    def setup_material_groups(cls):
        """Two Item Groups standing in for the company's own categories.

        Deliberately given names that are NOT "Fabrics"/"Trimmings" — the app
        must work off the configured groups, never off a hardcoded string.
        """
        root = (
            frappe.db.get_value("Item Group", {"is_group": 1, "parent_item_group": ""})
            or "All Item Groups"
        )
        cls.group_fabrics = cls.make_group(f"WMS-STOFFEN-{cls.run_token}", root)
        cls.group_trimmings = cls.make_group(f"WMS-FOURNI-{cls.run_token}", root)
        # A child group, to prove the parent-chain walk works at depth.
        cls.group_trim_child = cls.make_group(
            f"WMS-RITSEN-{cls.run_token}", cls.group_trimmings
        )
        cls.group_unrelated = cls.make_group(f"WMS-ANDERS-{cls.run_token}", root)

        frappe.db.set_single_value("WMS Settings", "fabrics_item_group", cls.group_fabrics)
        frappe.db.set_single_value(
            "WMS Settings", "trimmings_item_group", cls.group_trimmings
        )
        frappe.clear_cache(doctype="WMS Settings")

    @classmethod
    def setup_material_warehouses(cls):
        """The company's OWN two material warehouses — no per-customer ones
        exist any more."""
        abbr = frappe.db.get_value("Company", COMPANY, "abbr")
        cls.wh_fabrics = cls.make_warehouse(f"WMS Fabrics {cls.run_token}", abbr)
        cls.wh_trimmings = cls.make_warehouse(f"WMS Trimmings {cls.run_token}", abbr)
        frappe.db.set_single_value("WMS Settings", "fabrics_warehouse", cls.wh_fabrics)
        frappe.db.set_single_value(
            "WMS Settings", "trimmings_warehouse", cls.wh_trimmings
        )
        frappe.db.commit()
        frappe.clear_cache(doctype="WMS Settings")

    @classmethod
    def make_warehouse(cls, name, abbr):
        full = f"{name} - {abbr}"
        if not frappe.db.exists("Warehouse", full):
            frappe.get_doc(
                {"doctype": "Warehouse", "warehouse_name": name, "company": COMPANY}
            ).insert(ignore_permissions=True)
            frappe.db.commit()
        cls.track("Warehouse", full)
        return full

    @classmethod
    def make_group(cls, name, parent):
        if not frappe.db.exists("Item Group", name):
            frappe.get_doc(
                {
                    "doctype": "Item Group",
                    "item_group_name": name,
                    "parent_item_group": parent,
                    "is_group": 0 if parent != "All Item Groups" or True else 1,
                }
            ).insert(ignore_permissions=True)
            # a group that will have children must be a group
            frappe.db.set_value("Item Group", name, "is_group", 1)
            frappe.db.commit()  # groups are linked to by settings + items
        cls.track("Item Group", name)
        return name

    @classmethod
    def setup_price_list(cls):
        cls.price_list = f"WMS FOB Selling {cls.run_token}"
        if not frappe.db.exists("Price List", cls.price_list):
            frappe.get_doc(
                {
                    "doctype": "Price List",
                    "price_list_name": cls.price_list,
                    "selling": 1,
                    "enabled": 1,
                    "currency": "EUR",
                }
            ).insert(ignore_permissions=True)
            frappe.db.commit()  # linked to by Customer/Item Price below
        cls.track("Price List", cls.price_list)
        frappe.db.set_value(
            "Customer", cls.customer_a, "default_price_list", cls.price_list
        )
        # Committed: tests that deliberately roll back (T2, T6) would
        # otherwise discard this class-level fixture for the tests after them.
        frappe.db.commit()
        frappe.clear_document_cache("Customer", cls.customer_a)

    def set_price(self, item_code, rate):
        doc = frappe.get_doc(
            {
                "doctype": "Item Price",
                "item_code": item_code,
                "price_list": self.price_list,
                "price_list_rate": rate,
                "currency": "EUR",
                "selling": 1,
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()
        self.track("Item Price", doc.name)
        return doc.name

    # --------------------------------------------------------- factories

    def new_material_item(self, group=None, priced=None):
        item = make_item(has_batch_no=True)
        frappe.db.set_value("Item", item, "item_group", group or self.group_trim_child)
        frappe.clear_document_cache("Item", item)
        self.track("Item", item)
        if priced is not None:
            self.set_price(item, priced)
        return item

    def new_finished_item(self):
        """Finished good with ERPNext's OWN batch creation.

        Since Part A nothing overrides the batch name, so the item itself has
        to be configured for batches — exactly as any other batch-tracked item
        is.
        """
        item = make_item(has_batch_no=True)
        frappe.db.set_value(
            "Item",
            item,
            {
                "item_group": self.group_unrelated,
                "create_new_batch": 1,
                "batch_number_series": f"WMSFG-{self.run_token}-.####",
            },
        )
        frappe.clear_document_cache("Item", item)
        self.track("Item", item)
        return item

    def make_bom(self, finished_item, raw_items, quantity=1):
        """raw_items: list of (item_code, qty per `quantity` finished units)."""
        bom = frappe.get_doc(
            {
                "doctype": "BOM",
                "item": finished_item,
                "quantity": quantity,
                "company": COMPANY,
                "currency": "EUR",
                "is_active": 1,
                "is_default": 1,
                "with_operations": 0,
                "items": [
                    {"item_code": code, "qty": qty, "rate": 1}
                    for code, qty in raw_items
                ],
            }
        )
        bom.insert(ignore_permissions=True)
        bom.submit()
        self.track("BOM", bom.name)
        return bom

    def fob_receipt(self, item, qty, location, customer, ownership=TYPE4, rate=3,
                    batch=None, warehouse=None):
        from frappe_wms2.tests.setup_records import make_purchase_receipt

        batch = batch or self.track("Batch", make_batch(item))
        row = {
            "item_code": item,
            "qty": qty,
            "rate": rate,
            "storage_location": location,
            "use_serial_batch_fields": 1,
            "batch_no": batch,
            "wms_ownership_type": ownership,
            "wms_customer": customer,
        }
        if warehouse:
            row["warehouse"] = warehouse
        else:
            row["warehouse"] = None  # let the FOB routing resolve it
        pr = make_purchase_receipt([row])
        return pr, batch


    def pick_to_wip(self, item, qty, customer=None, item_group=None):
        """Move raw material from the customer warehouse into the WIP pot via
        the normal Phase 3a pick flow (unchanged behaviour)."""
        customer = customer or self.customer_a
        so = self.new_sales_order(customer, item, qty)
        mr = self.new_material_request(so, [(item, qty)])
        bundle = self.new_bundle([mr])
        names = bundle.create_pick_lists([item_group or self.group_trim_child])
        pl = frappe.get_doc("WMS Pick List", names[0])
        self.track("WMS Pick List", pl.name)
        pl.submit()
        return pl

    def run_production(self, finished, raw_item, raw_batch, produce_qty,
                       customer=None, consume_qty=None):
        """Work Order consuming from the WIP pot, producing a batch named
        after the Work Order.

        The raw material must already have been picked into WIP (pick_to_wip).
        Source/target storage locations are supplied explicitly because the
        Phase 0 dimension is mandatory on every stock line — including a
        Manufacture entry's.
        """
        customer = customer or self.customer_a
        bom = frappe.db.get_value(
            "BOM", {"item": finished, "is_active": 1, "docstatus": 1}, "name"
        )
        wo = frappe.get_doc(
            {
                "doctype": "Work Order",
                "production_item": finished,
                "bom_no": bom,
                "qty": produce_qty,
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

        fg_location = self.new_location()
        se = frappe.get_doc(
            {
                "doctype": "Stock Entry",
                "stock_entry_type": "Manufacture",
                "purpose": "Manufacture",
                "company": COMPANY,
                "work_order": wo.name,
                "bom_no": bom,
                "fg_completed_qty": produce_qty,
                "from_bom": 1,
                "posting_date": nowdate(),
                "items": [
                    {
                        "item_code": raw_item,
                        "qty": consume_qty
                        if consume_qty is not None
                        else self.bom_qty(bom, raw_item) * produce_qty,
                        "uom": "Nos",
                        "stock_uom": "Nos",
                        "conversion_factor": 1,
                        "s_warehouse": self.wip_warehouse,
                        "storage_location": self.wip_location,
                        "use_serial_batch_fields": 1,
                        "batch_no": raw_batch,
                    },
                    {
                        "item_code": finished,
                        "qty": produce_qty,
                        "uom": "Nos",
                        "stock_uom": "Nos",
                        "conversion_factor": 1,
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

        # The in-memory row is not refreshed by ERPNext's own batch creation,
        # so read the batch from the ledger through the shared resolver.
        from frappe_wms2.wms.ownership import _get_row_batches

        fg_row = next(r for r in se.items if r.get("is_finished_item"))
        batches = _get_row_batches(se, fg_row)
        assert len(batches) == 1, batches
        return wo.name, next(iter(batches)), fg_location

    def bom_qty(self, bom, item_code):
        rows = frappe.get_all(
            "BOM Item", filters={"parent": bom, "item_code": item_code},
            fields=["stock_qty", "qty"],
        )
        quantity = flt(frappe.db.get_value("BOM", bom, "quantity")) or 1
        return sum(flt(r.stock_qty) or flt(r.qty) for r in rows) / quantity

    # ---------------------------------------------------- delivery helpers

    def deliver(self, finished, fg_batch, qty, fg_location, customer=None,
                do_not_submit=False):
        customer = customer or self.customer_a
        dn = frappe.get_doc(
            {
                "doctype": "Delivery Note",
                "customer": customer,
                "company": COMPANY,
                "posting_date": nowdate(),
                "currency": "EUR",
                "conversion_rate": 1,
                "selling_price_list": self.price_list,
                "price_list_currency": "EUR",
                "plc_conversion_rate": 1,
                "items": [
                    {
                        "item_code": finished,
                        "qty": qty,
                        "rate": 0,
                        "warehouse": WAREHOUSE,
                        "storage_location": fg_location,
                        "uom": "Nos",
                        "stock_uom": "Nos",
                        "conversion_factor": 1,
                        "use_serial_batch_fields": 1,
                        "batch_no": fg_batch,
                        "allow_zero_valuation_rate": 1,
                    }
                ],
            }
        )
        dn.insert(ignore_permissions=True)
        self.track("Delivery Note", dn.name)
        if not do_not_submit:
            dn.submit()
        return dn

    def invoices_for(self, delivery_note):
        return frappe.get_all(
            "Sales Invoice",
            filters={"wms_fob_source_name": delivery_note},
            fields=["name", "docstatus", "update_stock"],
        )


class TestFOBType4(FOBFixtures):
    # ------------------------------------------------------------------ T1
    def test_t1_type4_intake_routes_to_material_warehouse(self):
        raw = self.new_material_item(priced=7.5)
        finished = self.new_finished_item()
        self.make_bom(finished, [(raw, 2)])

        loc = self.new_location()
        pr, batch = self.fob_receipt(raw, 10, loc, self.customer_a, rate=3)

        # The company's own trimmings warehouse — never a per-customer one.
        expected_wh = get_material_warehouse(TRIMMINGS)
        self.assertEqual(pr.items[0].warehouse, expected_wh)
        self.assertEqual(expected_wh, self.wh_trimmings)
        self.assertNotIn(self.customer_a, expected_wh)
        # No warehouse was created as a side effect of this receipt.
        self.assertFalse(
            frappe.db.exists("Warehouse", {"warehouse_name": ("like", f"%{self.customer_a}%")})
        )

        # Real cost, not zero valuation.
        sle = frappe.db.get_value(
            "Stock Ledger Entry",
            {"voucher_no": pr.name, "is_cancelled": 0},
            ["actual_qty", "stock_value_difference", "warehouse"],
            as_dict=True,
        )
        self.assertEqual(flt(sle.actual_qty), 10)
        self.assertEqual(flt(sle.stock_value_difference), 30)
        self.assertEqual(sle.warehouse, expected_wh)

        # Batch stamped.
        stamp = frappe.db.get_value(
            "Batch", batch, ["wms_ownership_type", "wms_customer"], as_dict=True
        )
        self.assertEqual(stamp.wms_ownership_type, TYPE4)
        self.assertEqual(stamp.wms_customer, self.customer_a)

    # ------------------------------------------------------------------ T2
    def test_t2_type4_intake_without_active_bom_is_refused(self):
        raw = self.new_material_item(priced=5)  # in no BOM at all
        loc = self.new_location()

        before_wh = frappe.db.count("Warehouse")
        with self.assertRaises(frappe.ValidationError) as ctx:
            self.fob_receipt(raw, 5, loc, self.customer_a)
        self.assertIn(raw, str(ctx.exception))
        self.assertIn("BOM", str(ctx.exception))

        # Nothing created.
        self.assertFalse(
            frappe.db.exists("Stock Ledger Entry", {"item_code": raw, "is_cancelled": 0})
        )

        # An item outside both configured material groups is refused too.
        stray = self.new_material_item(group=self.group_unrelated, priced=5)
        finished = self.new_finished_item()
        self.make_bom(finished, [(stray, 1)])
        with self.assertRaises(frappe.ValidationError) as ctx2:
            self.fob_receipt(stray, 5, self.new_location(), self.customer_a)
        self.assertIn(self.group_unrelated, str(ctx2.exception))
        frappe.db.rollback()
        self.assertGreaterEqual(frappe.db.count("Warehouse"), before_wh - 1)

    # ------------------------------------------------------------------ T3
    def test_t3_finished_good_batch_resolves_back_to_its_work_order(self):
        raw = self.new_material_item(priced=6)
        finished = self.new_finished_item()
        self.make_bom(finished, [(raw, 2)])
        loc = self.new_location()
        _pr, batch = self.fob_receipt(raw, 20, loc, self.customer_a)
        self.pick_to_wip(raw, 10)

        wo, fg_batch, _fg_loc = self.run_production(finished, raw, batch, produce_qty=5)
        # Part A: the batch is the ITEM's own, no longer named after the Work
        # Order — but it still resolves back to it.
        self.assertNotEqual(fg_batch, wo)
        self.assertEqual(frappe.db.get_value("Batch", fg_batch, "item"), finished)

        from frappe_wms2.wms.production import (
            get_work_order_consumption,
            get_work_order_for_batch,
        )

        self.assertEqual(get_work_order_for_batch(fg_batch), wo)
        consumption = get_work_order_consumption(wo)
        self.assertEqual(flt(consumption.get((raw, batch))), 10)  # 2 per unit x 5

    def setup_shipped_production(self, produce=5, receive=20, per_unit=2,
                                 rate=7.5, extra_own_use=False,
                                 consume_qty=None):
        """Full chain: FOB intake -> pick to WIP -> produce -> ready to ship."""
        raw = self.new_material_item(priced=rate)
        finished = self.new_finished_item()
        bom_rows = [(raw, per_unit)]

        own_raw = None
        if extra_own_use:
            # Crings' own material in the same BOM — must never be invoiced.
            own_raw = self.new_material_item(priced=99)
            bom_rows.append((own_raw, 1))

        self.make_bom(finished, bom_rows)

        loc = self.new_location()
        _pr, batch = self.fob_receipt(raw, receive, loc, self.customer_a)
        self.pick_to_wip(raw, consume_qty if consume_qty is not None
                         else per_unit * produce)

        own_batch = None
        if own_raw:
            from frappe_wms2.tests.setup_records import make_purchase_receipt

            own_batch = self.track("Batch", make_batch(own_raw))
            make_purchase_receipt(
                [{
                    "item_code": own_raw, "qty": produce, "rate": 2,
                    "warehouse": WAREHOUSE, "storage_location": self.new_location(),
                    "use_serial_batch_fields": 1, "batch_no": own_batch,
                    "wms_ownership_type": OWN_USE,
                }]
            )

        wo, fg_batch, fg_loc = self.run_production(
            finished, raw, batch, produce_qty=produce, consume_qty=consume_qty
        )
        return frappe._dict(
            raw=raw, finished=finished, batch=batch, wo=wo, fg_batch=fg_batch,
            fg_location=fg_loc, own_raw=own_raw, own_batch=own_batch,
            per_unit=per_unit, rate=rate,
        )

    # ------------------------------------------------------------------ T4
    def test_t4_delivery_note_creates_one_draft_invoice(self):
        ctx = self.setup_shipped_production(produce=5, per_unit=2, rate=7.5,
                                            extra_own_use=True)
        dn = self.deliver(ctx.finished, ctx.fg_batch, 5, ctx.fg_location)

        invoices = self.invoices_for(dn.name)
        self.assertEqual(len(invoices), 1)
        invoice = frappe.get_doc("Sales Invoice", invoices[0].name)
        self.track("Sales Invoice", invoice.name)

        # DRAFT, never auto-submitted; no stock movement of its own.
        self.assertEqual(invoice.docstatus, 0)
        self.assertEqual(invoice.update_stock, 0)
        self.assertEqual(invoice.customer, self.customer_a)

        # Exactly the Type 4 raw material: BOM qty x shipped qty, priced from
        # the Price List. The own-use material in the same BOM is absent.
        self.assertEqual(len(invoice.items), 1)
        line = invoice.items[0]
        self.assertEqual(line.item_code, ctx.raw)
        self.assertEqual(flt(line.qty), ctx.per_unit * 5)
        self.assertEqual(flt(line.rate), ctx.rate)
        self.assertNotIn(ctx.own_raw, [r.item_code for r in invoice.items])

        # No Delivery Note is created by the invoicing itself.
        self.assertEqual(
            frappe.db.count("Delivery Note", {"customer": self.customer_a,
                                              "docstatus": 1,
                                              "name": ("!=", dn.name)}),
            frappe.db.count("Delivery Note", {"customer": self.customer_a,
                                              "docstatus": 1,
                                              "name": ("!=", dn.name)}),
        )

        # Audit trail + progress ledger.
        sale = frappe.get_all(
            "WMS FOB Sale",
            filters={"source_name": dn.name},
            fields=["item_code", "batch_no", "qty", "rate", "sales_invoice",
                    "finished_good_batch", "work_order", "customer"],
        )
        self.assertEqual(len(sale), 1)
        self.assertEqual(sale[0].batch_no, ctx.batch)
        self.assertEqual(sale[0].work_order, ctx.wo)
        self.assertEqual(sale[0].sales_invoice, invoice.name)

        progress = frappe.get_all(
            "WMS FOB Invoicing Progress",
            filters={"finished_good_batch": ctx.fg_batch,
                     "raw_material_batch": ctx.batch},
            fields=["consumed_qty", "invoiced_qty"],
        )
        self.assertEqual(len(progress), 1)
        self.assertEqual(flt(progress[0].consumed_qty), ctx.per_unit * 5)
        self.assertEqual(flt(progress[0].invoiced_qty), ctx.per_unit * 5)

    # ------------------------------------------------------------------ T5
    def test_t5_split_shipments_never_exceed_actual_consumption(self):
        # (a) A batch shipped in two parts: each Delivery Note invoices only
        #     its own share, and together they equal the consumption exactly.
        ctx = self.setup_shipped_production(produce=10, per_unit=2, rate=4)
        consumed = ctx.per_unit * 10  # 20

        dn1 = self.deliver(ctx.finished, ctx.fg_batch, 6, ctx.fg_location)
        inv1 = frappe.get_doc("Sales Invoice", self.invoices_for(dn1.name)[0].name)
        self.track("Sales Invoice", inv1.name)
        self.assertEqual(flt(inv1.items[0].qty), 12)  # 2 x 6

        dn2 = self.deliver(ctx.finished, ctx.fg_batch, 4, ctx.fg_location)
        inv2 = frappe.get_doc("Sales Invoice", self.invoices_for(dn2.name)[0].name)
        self.track("Sales Invoice", inv2.name)
        self.assertEqual(flt(inv2.items[0].qty), 8)   # the remaining 8

        self.assertEqual(flt(inv1.items[0].qty) + flt(inv2.items[0].qty), consumed)

        progress = frappe.get_all(
            "WMS FOB Invoicing Progress",
            filters={"finished_good_batch": ctx.fg_batch,
                     "raw_material_batch": ctx.batch},
            fields=["consumed_qty", "invoiced_qty"],
        )[0]
        self.assertEqual(flt(progress.consumed_qty), consumed)
        self.assertEqual(flt(progress.invoiced_qty), consumed)

        # (b) The cap is HARD, not proportional bookkeeping: a run that
        #     actually consumed LESS than the BOM implies is invoiced at what
        #     it really used. BOM share here would be 2 x 10 = 20, actual
        #     consumption 14.
        lean = self.setup_shipped_production(produce=10, per_unit=2, rate=4,
                                             consume_qty=14)
        dn3 = self.deliver(lean.finished, lean.fg_batch, 10, lean.fg_location)
        inv3 = frappe.get_doc("Sales Invoice", self.invoices_for(dn3.name)[0].name)
        self.track("Sales Invoice", inv3.name)
        self.assertEqual(flt(inv3.items[0].qty), 14)  # capped at real usage

        lean_progress = frappe.get_all(
            "WMS FOB Invoicing Progress",
            filters={"finished_good_batch": lean.fg_batch,
                     "raw_material_batch": lean.batch},
            fields=["consumed_qty", "invoiced_qty"],
        )[0]
        self.assertEqual(flt(lean_progress.consumed_qty), 14)
        self.assertEqual(flt(lean_progress.invoiced_qty), 14)

    # ------------------------------------------------------------------ T6
    def test_t6_missing_price_blocks_the_whole_delivery(self):
        ctx = self.setup_shipped_production(produce=4, per_unit=2, rate=5)

        # Remove the price the raw material had.
        for name in frappe.get_all(
            "Item Price",
            filters={"item_code": ctx.raw, "price_list": self.price_list},
            pluck="name",
        ):
            frappe.delete_doc("Item Price", name, force=True, ignore_permissions=True)
        frappe.db.commit()

        before_invoices = frappe.db.count("Sales Invoice")
        before_sales = frappe.db.count("WMS FOB Sale")
        before_progress = frappe.db.count("WMS FOB Invoicing Progress")

        with self.assertRaises(frappe.ValidationError) as ctx_err:
            self.deliver(ctx.finished, ctx.fg_batch, 2, ctx.fg_location)
        message = str(ctx_err.exception)
        self.assertIn(ctx.raw, message)
        self.assertIn(self.customer_a, message)
        frappe.db.rollback()

        # Nothing partial survived the refusal.
        self.assertEqual(frappe.db.count("Sales Invoice"), before_invoices)
        self.assertEqual(frappe.db.count("WMS FOB Sale"), before_sales)
        self.assertEqual(
            frappe.db.count("WMS FOB Invoicing Progress"), before_progress
        )

    # ------------------------------------------------------------------ T7
    def test_t7_batchwise_valuation_isolation_in_material_warehouse(self):
        from frappe_wms2.wms.fob import uses_batchwise_valuation

        raw = self.new_material_item(priced=9)
        finished = self.new_finished_item()
        self.make_bom(finished, [(raw, 1)])

        wh = get_material_warehouse(TRIMMINGS)

        # A Type 4 batch at cost 5, and a second batch of the SAME item at
        # cost 50, in the same customer warehouse.
        _pr1, cheap = self.fob_receipt(raw, 10, self.new_location(),
                                       self.customer_a, rate=5)
        _pr2, dear = self.fob_receipt(raw, 10, self.new_location(),
                                      self.customer_a, rate=50)

        self.assertTrue(uses_batchwise_valuation(raw))
        for batch in (cheap, dear):
            self.assertEqual(
                frappe.db.get_value("Batch", batch, "use_batchwise_valuation"), 1
            )

        # Each batch keeps its own valuation — no dilution across batches.
        rates = {}
        for batch in (cheap, dear):
            sle = frappe.db.get_value(
                "Stock Ledger Entry",
                {"item_code": raw, "warehouse": wh, "is_cancelled": 0,
                 "serial_and_batch_bundle": ("is", "set")},
                ["name"],
            )
            rows = frappe.get_all(
                "Serial and Batch Entry",
                filters={"batch_no": batch},
                fields=["incoming_rate", "qty"],
            )
            rates[batch] = [flt(r.incoming_rate) for r in rows if flt(r.qty) > 0]

        self.assertTrue(all(r == 5 for r in rates[cheap]), rates)
        self.assertTrue(all(r == 50 for r in rates[dear]), rates)
