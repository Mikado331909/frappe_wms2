# PHASE 3a LIVE TESTS — core picking flow
# =======================================
# Run: bench --site <site> run-tests --app frappe_wms2 \
#          --module frappe_wms2.tests.test_picking_phase3a
#
# COLLISION-SAFE BY CONSTRUCTION. The real site already carries 622 storage
# locations and live stock, so this suite:
#   * generates a fresh random token per RUN and per TEST, used in every
#     record name it creates (locations, items, batches, customers, MRs...);
#   * uses gang letters far outside the real code space and a random niveau,
#     and re-rolls if a code somehow exists;
#   * never touches, reads-for-assertions, or deletes pre-existing records;
#     every balance assertion is scoped to its own throwaway item;
#   * cleans up its own drafts/masters in tearDownClass (submitted stock
#     documents are deliberately left: they are ledger history, and the test
#     items/locations they touch are throwaway anyway).

import random
import string

import frappe
from frappe.utils import flt, nowdate

try:
    from frappe.tests import IntegrationTestCase as TestBase
except ImportError:  # older frappe
    from frappe.tests.utils import FrappeTestCase as TestBase

from frappe_wms2.install import ensure_ownership_types, ensure_pick_reasons
from frappe_wms2.tests.setup_records import (
    COMPANY,
    WAREHOUSE,
    get_or_create_company,
    get_or_create_customer,
    get_or_create_location,
    get_or_create_supplier,
    get_or_create_warehouse,
    make_batch,
    make_item,
    make_purchase_receipt,
    setup_gate_records,
)
from frappe_wms2.wms.doctype.wms_pick_list.wms_pick_list import get_wip_provenance
from frappe_wms2.wms.picking import get_stock_by_batch_location

SUPPLIED = "Supplied by customer"
OWN_USE = "Own use"


def _price_list():
    """A selling price list in EUR; created once if the site has none."""
    name = frappe.db.get_value(
        "Price List", {"selling": 1, "currency": "EUR", "enabled": 1}
    )
    if name:
        return name
    name = "WMS3A Selling EUR"
    if not frappe.db.exists("Price List", name):
        frappe.get_doc(
            {
                "doctype": "Price List",
                "price_list_name": name,
                "selling": 1,
                "enabled": 1,
                "currency": "EUR",
            }
        ).insert(ignore_permissions=True)
    return name


def token(n=6):
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


class TestPickingPhase3a(TestBase):
    # ------------------------------------------------------------- fixtures

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.run_token = token()
        cls.created = []  # (doctype, name) — cleaned up in reverse

        setup_gate_records()
        ensure_ownership_types()
        ensure_pick_reasons()
        get_or_create_company()
        get_or_create_warehouse()
        get_or_create_supplier()

        cls.customer_a = cls.new_customer("A")
        cls.customer_b = cls.new_customer("B")
        cls.item_group = cls.new_item_group()
        cls.other_group = cls.new_item_group()

        cls.setup_wip_pot()

        cls.reason_short = "Batch empty earlier than expected"
        cls.reason_surplus = "More present than administered"

    @classmethod
    def tearDownClass(cls):
        frappe.flags.wms2_settings_override = None
        # Reverse order: children before parents.
        for doctype, name in reversed(cls.created):
            try:
                doc = frappe.get_doc(doctype, name)
                if doc.meta.is_submittable and doc.docstatus == 1:
                    continue  # ledger history — leave it
                doc.delete(ignore_permissions=True)
            except Exception:
                pass  # never let cleanup break the run
        frappe.db.commit()
        super().tearDownClass()

    @classmethod
    def track(cls, doctype, name):
        cls.created.append((doctype, name))
        return name

    # --------------------------------------------------------- factories

    @classmethod
    def new_customer(cls, suffix):
        name = f"WMS3A {cls.run_token} Cust {suffix}"
        get_or_create_customer(name)
        return cls.track("Customer", name)

    @classmethod
    def new_item_group(cls):
        name = f"WMS3A-{cls.run_token}-{token(4)}"
        parent = frappe.db.get_value(
            "Item Group", {"is_group": 1, "parent_item_group": ""}
        ) or "All Item Groups"
        frappe.get_doc(
            {
                "doctype": "Item Group",
                "item_group_name": name,
                "parent_item_group": parent,
                "is_group": 0,
            }
        ).insert(ignore_permissions=True)
        return cls.track("Item Group", name)

    @classmethod
    def new_location(cls):
        """Throwaway physical location with a code that cannot collide with
        the 622 real ones: gang letters X/Y/Z + random 3-digit niveau."""
        for _ in range(50):
            code = (
                f"F{random.choice('XYZ')}{random.randint(700, 999)}"
                f"-{random.randint(100, 999)}"
            )
            if not frappe.db.exists("Storage Location", code):
                get_or_create_location(code)
                return cls.track("Storage Location", code)
        raise RuntimeError("could not find a free throwaway location code")

    @classmethod
    def setup_wip_pot(cls):
        abbr = frappe.db.get_value("Company", COMPANY, "abbr")
        wh_name = f"WMS3A WIP {cls.run_token}"
        wh = f"{wh_name} - {abbr}"
        if not frappe.db.exists("Warehouse", wh):
            frappe.get_doc(
                {"doctype": "Warehouse", "warehouse_name": wh_name, "company": COMPANY}
            ).insert(ignore_permissions=True)
        cls.track("Warehouse", wh)

        pot = f"WIP-POT-{cls.run_token}"
        if not frappe.db.exists("Storage Location", pot):
            frappe.get_doc(
                {
                    "doctype": "Storage Location",
                    "location_code": pot,
                    "warehouse": wh,
                    "is_special": 1,
                }
            ).insert(ignore_permissions=True)
        cls.track("Storage Location", pot)

        # WMS Settings is a GLOBAL Single, shared by the whole site and not
        # scoped per company — a test once overwrote a live WIP pot there.
        # So we never write it: the values are injected in-memory for this
        # process only and disappear when it ends.
        frappe.flags.wms2_settings_override = {
            "company": COMPANY,
            "wip_warehouse": wh,
            "wip_storage_location": pot,
            "allow_customer_neutral_stock": 1,
        }
        cls.wip_warehouse = wh
        cls.wip_location = pot

    def new_item(self, item_group=None):
        code = make_item(has_batch_no=True)
        frappe.db.set_value(
            "Item", code, "item_group", item_group or self.item_group
        )
        self.track("Item", code)
        return code

    def receive(self, item_code, qty, location, customer, batch=None, rate=0):
        """Bring stock in through the Phase 2a intake path (real flow)."""
        batch = batch or self.track("Batch", make_batch(item_code))
        row = {
            "item_code": item_code,
            "qty": qty,
            "rate": rate,
            "warehouse": WAREHOUSE,
            "storage_location": location,
            "use_serial_batch_fields": 1,
            "batch_no": batch,
        }
        if customer:
            row["wms_ownership_type"] = SUPPLIED
            row["wms_customer"] = customer
        else:
            row["wms_ownership_type"] = OWN_USE
        make_purchase_receipt([row])
        return batch

    def new_sales_order(self, customer, item_code, qty):
        so = frappe.get_doc(
            {
                "doctype": "Sales Order",
                "customer": customer,
                "company": COMPANY,
                "currency": "EUR",
                "conversion_rate": 1,
                # A sterile/test site may have no default price list.
                "selling_price_list": _price_list(),
                "price_list_currency": "EUR",
                "plc_conversion_rate": 1,
                "transaction_date": nowdate(),
                "delivery_date": nowdate(),
                "items": [
                    {
                        "item_code": item_code,
                        "qty": qty,
                        "rate": 1,
                        "delivery_date": nowdate(),
                        "warehouse": WAREHOUSE,
                    }
                ],
            }
        )
        so.insert(ignore_permissions=True)
        so.submit()
        self.track("Sales Order", so.name)
        return so

    def new_material_request(self, sales_order, rows):
        """rows: list of (item_code, qty)."""
        mr = frappe.get_doc(
            {
                "doctype": "Material Request",
                "material_request_type": "Material Transfer",
                "company": COMPANY,
                "transaction_date": nowdate(),
                "schedule_date": nowdate(),
                "items": [
                    {
                        "item_code": item_code,
                        "qty": qty,
                        "schedule_date": nowdate(),
                        "warehouse": WAREHOUSE,
                        "sales_order": sales_order.name,
                    }
                    for item_code, qty in rows
                ],
            }
        )
        mr.insert(ignore_permissions=True)
        mr.submit()
        self.track("Material Request", mr.name)
        return mr

    def new_bundle(self, material_requests, submit=True):
        doc = frappe.get_doc(
            {
                "doctype": "WMS Pick Batch",
                "company": COMPANY,
                "material_requests": [
                    {"material_request": mr.name} for mr in material_requests
                ],
            }
        )
        doc.insert(ignore_permissions=True)
        self.track("WMS Pick Batch", doc.name)
        if submit:
            doc.submit()
        return doc

    def item_balance_by_location(self, item_code):
        out = {}
        for row in get_stock_by_batch_location([item_code]):
            out[(row.batch_no, row.storage_location)] = row.qty
        return out

    # ------------------------------------------------------------------ T1
    def test_t1_cannot_bundle_two_customers(self):
        item = self.new_item()
        so_a = self.new_sales_order(self.customer_a, item, 5)
        so_b = self.new_sales_order(self.customer_b, item, 5)
        mr_a = self.new_material_request(so_a, [(item, 5)])
        mr_b = self.new_material_request(so_b, [(item, 5)])

        with self.assertRaises(frappe.ValidationError):
            self.new_bundle([mr_a, mr_b], submit=False)

        # Single-customer bundle is fine, and the customer is DERIVED.
        bundle = self.new_bundle([mr_a], submit=False)
        self.assertEqual(bundle.customer, self.customer_a)
        self.assertEqual(bundle.material_requests[0].sales_order, so_a.name)

    # ------------------------------------------------------------------ T2
    def test_t2_demand_summed_across_material_requests(self):
        item1 = self.new_item()
        item2 = self.new_item()
        so = self.new_sales_order(self.customer_a, item1, 10)
        mr1 = self.new_material_request(so, [(item1, 6), (item2, 4)])
        mr2 = self.new_material_request(so, [(item1, 9)])

        bundle = self.new_bundle([mr1, mr2], submit=False)
        demand = {r.item_code: r for r in bundle.demand_items}
        self.assertEqual(flt(demand[item1].qty_demand), 15)  # 6 + 9
        self.assertEqual(flt(demand[item2].qty_demand), 4)
        self.assertEqual(flt(demand[item1].qty_open), 15)  # nothing listed yet
        self.assertEqual(flt(bundle.total_demand), 19)

    # ------------------------------------------------------------------ T3
    def test_t3_fifo_allocation_across_batches(self):
        item = self.new_item()
        loc1, loc2, loc3 = self.new_location(), self.new_location(), self.new_location()

        # Oldest first: b1 (3), then b2 (4), then b3 (10).
        b1 = self.receive(item, 3, loc1, self.customer_a)
        b2 = self.receive(item, 4, loc2, self.customer_a)
        b3 = self.receive(item, 10, loc3, self.customer_a)

        so = self.new_sales_order(self.customer_a, item, 9)
        mr = self.new_material_request(so, [(item, 9)])
        bundle = self.new_bundle([mr])

        names = bundle.create_pick_lists([self.item_group])
        self.assertEqual(len(names), 1)
        pl = frappe.get_doc("WMS Pick List", names[0])
        self.track("WMS Pick List", pl.name)

        # FIFO: 3 from b1, 4 from b2, 2 from b3 = 9; b3 keeps its rest.
        allocation = [(r.batch_no, flt(r.qty_to_pick)) for r in pl.items]
        self.assertEqual(allocation, [(b1, 3), (b2, 4), (b3, 2)])
        self.assertEqual(flt(pl.total_to_pick), 9)

        # "Qty available on batch" is the real balance of that batch+location.
        avail = {r.batch_no: flt(r.qty_available) for r in pl.items}
        self.assertEqual(avail, {b1: 3, b2: 4, b3: 10})

        # Customer header, no values on the document.
        self.assertEqual(pl.customer, self.customer_a)
        self.assertFalse(pl.meta.has_field("rate"))
        self.assertFalse(pl.meta.get_field("items").options == "Sales Order Item")

        # RESERVATION: the bundle now shows 9 listed, 0 open — a second
        # pick list for the same group finds nothing left.
        bundle.reload()
        bundle.build_demand()
        demand = {r.item_code: r for r in bundle.demand_items}
        self.assertEqual(flt(demand[item].qty_listed), 9)
        self.assertEqual(flt(demand[item].qty_open), 0)
        with self.assertRaises(frappe.ValidationError):
            bundle.create_pick_lists([self.item_group])

    # ------------------------------------------------------------------ T4
    def test_t4_item_group_selection_generates_one_list_per_group(self):
        item_fab = self.new_item(self.item_group)
        item_trim = self.new_item(self.other_group)
        loc = self.new_location()
        self.receive(item_fab, 5, loc, self.customer_a)
        self.receive(item_trim, 5, loc, self.customer_a)

        so = self.new_sales_order(self.customer_a, item_fab, 5)
        mr = self.new_material_request(so, [(item_fab, 5), (item_trim, 5)])
        bundle = self.new_bundle([mr])

        # Only fabrics first.
        names = bundle.create_pick_lists([self.item_group])
        self.assertEqual(len(names), 1)
        pl1 = frappe.get_doc("WMS Pick List", names[0])
        self.track("WMS Pick List", pl1.name)
        self.assertEqual(pl1.item_group, self.item_group)
        self.assertEqual({r.item_code for r in pl1.items}, {item_fab})

        # The other group is still open.
        bundle.reload()
        bundle.build_demand()
        open_map = {r.item_code: flt(r.qty_open) for r in bundle.demand_items}
        self.assertEqual(open_map[item_fab], 0)
        self.assertEqual(open_map[item_trim], 5)

        names2 = bundle.create_pick_lists([self.other_group])
        pl2 = frappe.get_doc("WMS Pick List", names2[0])
        self.track("WMS Pick List", pl2.name)
        self.assertEqual({r.item_code for r in pl2.items}, {item_trim})

    # ------------------------------------------------------------------ T5
    def test_t5_full_pick_posts_to_wip_with_provenance(self):
        item = self.new_item()
        loc = self.new_location()
        batch = self.receive(item, 12, loc, self.customer_a)

        so = self.new_sales_order(self.customer_a, item, 7)
        mr = self.new_material_request(so, [(item, 7)])
        bundle = self.new_bundle([mr])
        pl = frappe.get_doc(
            "WMS Pick List", bundle.create_pick_lists([self.item_group])[0]
        )
        self.track("WMS Pick List", pl.name)

        # DRAFT: nothing has moved yet.
        before = self.item_balance_by_location(item)
        self.assertEqual(before[(batch, loc)], 12)

        pl.reload()
        self.assertEqual(flt(pl.items[0].qty_to_pick), 7)
        pl.items[0].picked_qty = 7  # picked == to pick, no reason needed
        pl.save()
        self.assertEqual(
            self.item_balance_by_location(item)[(batch, loc)],
            12,
            "Saving a draft must never move stock",
        )

        pl.submit()
        pl.reload()

        # Bulk decreased by exactly the picked qty; batch keeps its rest.
        after = self.item_balance_by_location(item)
        self.assertEqual(after[(batch, loc)], 5)

        # Material is in the WIP pot (simple pot: one location, provenance
        # stays on the batch/document).
        self.assertTrue(pl.stock_entry)
        se = frappe.get_doc("Stock Entry", pl.stock_entry)
        self.assertEqual(se.docstatus, 1)
        self.assertEqual(se.purpose, "Material Transfer")
        self.assertEqual(se.items[0].t_warehouse, self.wip_warehouse)
        self.assertEqual(se.items[0].to_storage_location, self.wip_location)

        wip_qty = sum(
            flt(r.actual_qty)
            for r in frappe.get_all(
                "Stock Ledger Entry",
                filters={
                    "item_code": item,
                    "warehouse": self.wip_warehouse,
                    "is_cancelled": 0,
                },
                fields=["actual_qty"],
            )
        )
        self.assertEqual(wip_qty, 7)

        # Provenance intact: order, customer, batch + its ownership.
        prov = get_wip_provenance(pl.name)
        self.assertEqual(len(prov), 1)
        p = prov[0]
        self.assertEqual(p["customer"], self.customer_a)
        self.assertEqual(p["material_request"], mr.name)
        self.assertEqual(p["sales_order"], so.name)
        self.assertEqual(p["batch_no"], batch)
        self.assertEqual(p["batch_customer"], self.customer_a)
        self.assertEqual(p["batch_ownership_type"], SUPPLIED)
        self.assertEqual(flt(p["qty"]), 7)

        # No correction entry when nothing was flagged empty.
        self.assertFalse(pl.correction_stock_entry)

    # ------------------------------------------------------------------ T6
    def test_t6_short_pick_with_batch_empty_sets_balance_to_zero(self):
        item = self.new_item()
        loc = self.new_location()
        batch = self.receive(item, 10, loc, self.customer_a)

        so = self.new_sales_order(self.customer_a, item, 10)
        mr = self.new_material_request(so, [(item, 10)])
        bundle = self.new_bundle([mr])
        pl = frappe.get_doc(
            "WMS Pick List", bundle.create_pick_lists([self.item_group])[0]
        )
        self.track("WMS Pick List", pl.name)

        pl.items[0].picked_qty = 6      # 4 short
        pl.items[0].batch_empty = 1     # ...and the batch turned out empty

        # Missing reason blocks the submit.
        with self.assertRaises(frappe.ValidationError):
            pl.save()
        pl.reload()

        pl.items[0].picked_qty = 6
        pl.items[0].batch_empty = 1
        pl.items[0].reason = self.reason_short
        pl.items[0].comment = "roll was shorter than administered"
        pl.save()
        pl.submit()
        pl.reload()

        # Picked 6 to WIP, remaining 4 written off to 0 with the reason.
        balances = self.item_balance_by_location(item)
        self.assertEqual(flt(balances.get((batch, loc), 0)), 0)

        self.assertTrue(pl.correction_stock_entry)
        corr = frappe.get_doc("Stock Entry", pl.correction_stock_entry)
        self.assertEqual(corr.purpose, "Material Issue")
        self.assertEqual(flt(corr.items[0].qty), 4)
        self.assertEqual(corr.items[0].storage_location, loc)
        self.assertIn(self.reason_short, corr.remarks)
        self.assertEqual(flt(pl.items[0].correction_qty), 4)

    # ------------------------------------------------------------------ T7
    def test_t7_surplus_pick_requires_reason_and_posts(self):
        item = self.new_item()
        loc = self.new_location()
        batch = self.receive(item, 10, loc, self.customer_a)

        so = self.new_sales_order(self.customer_a, item, 4)
        mr = self.new_material_request(so, [(item, 4)])
        bundle = self.new_bundle([mr])
        pl = frappe.get_doc(
            "WMS Pick List", bundle.create_pick_lists([self.item_group])[0]
        )
        self.track("WMS Pick List", pl.name)

        pl.items[0].picked_qty = 6  # more than the proposed 4
        with self.assertRaises(frappe.ValidationError):
            pl.save()
        pl.reload()

        pl.items[0].picked_qty = 6
        pl.items[0].reason = self.reason_surplus
        pl.save()
        pl.submit()

        self.assertEqual(self.item_balance_by_location(item)[(batch, loc)], 4)

        # Cannot pick more than the location holds — the Phase 0 dimension
        # rule still governs, and we fail with a clear message first.
        item2 = self.new_item()
        loc2 = self.new_location()
        self.receive(item2, 2, loc2, self.customer_a)
        so2 = self.new_sales_order(self.customer_a, item2, 2)
        mr2 = self.new_material_request(so2, [(item2, 2)])
        b2 = self.new_bundle([mr2])
        pl2 = frappe.get_doc(
            "WMS Pick List", b2.create_pick_lists([self.item_group])[0]
        )
        self.track("WMS Pick List", pl2.name)
        pl2.items[0].picked_qty = 5   # location holds only 2
        pl2.items[0].reason = self.reason_surplus
        with self.assertRaises(frappe.ValidationError):
            pl2.save()
        pl2.reload()

    # ------------------------------------------------------------------ T8
    def test_t8_added_line_only_allows_same_customer_batches(self):
        item = self.new_item()
        loc_a, loc_b = self.new_location(), self.new_location()
        batch_a = self.receive(item, 5, loc_a, self.customer_a)
        batch_b = self.receive(item, 5, loc_b, self.customer_b)   # other customer
        batch_own = self.receive(item, 5, self.new_location(), None)  # own use

        so = self.new_sales_order(self.customer_a, item, 5)
        mr = self.new_material_request(so, [(item, 5)])
        bundle = self.new_bundle([mr])
        pl = frappe.get_doc(
            "WMS Pick List", bundle.create_pick_lists([self.item_group])[0]
        )
        self.track("WMS Pick List", pl.name)

        # Allocation itself never offers the other customer's batch.
        self.assertNotIn(batch_b, {r.batch_no for r in pl.items})

        # Processor adds a line from customer B's batch -> blocked.
        pl.append(
            "items",
            {
                "material_request": mr.name,
                "item_code": item,
                "qty_needed": 0,
                "warehouse": WAREHOUSE,
                "storage_location": loc_b,
                "batch_no": batch_b,
                "qty_to_pick": 0,
                "picked_qty": 2,
                "reason": self.reason_surplus,
                "is_added": 1,
                "stock_uom": "Nos",
            },
        )
        with self.assertRaises(frappe.ValidationError):
            pl.save()
        pl.reload()

        # Same-customer batch on an added line IS accepted.
        pl.append(
            "items",
            {
                "material_request": mr.name,
                "item_code": item,
                "qty_needed": 0,
                "warehouse": WAREHOUSE,
                "storage_location": loc_a,
                "batch_no": batch_a,
                "qty_to_pick": 0,
                "picked_qty": 2,
                "reason": self.reason_surplus,
                "is_added": 1,
                "stock_uom": "Nos",
            },
        )
        pl.save()
        self.assertEqual(len(pl.items), 2)

    # ----------------------------------------------------------------- T10
    def test_t10_print_format_has_columns_sum_rows_and_no_values(self):
        """The floor paper: customer header, the required columns, one sum
        row per item, blank Picked qty / Batch empty? boxes, no values."""
        item1 = self.new_item()
        item2 = self.new_item()
        loc1, loc2 = self.new_location(), self.new_location()
        self.receive(item1, 4, loc1, self.customer_a)
        self.receive(item1, 4, loc2, self.customer_a)   # 2 lines for item1
        self.receive(item2, 5, loc1, self.customer_a)

        so = self.new_sales_order(self.customer_a, item1, 7)
        mr = self.new_material_request(so, [(item1, 7), (item2, 5)])
        bundle = self.new_bundle([mr])
        pl = frappe.get_doc(
            "WMS Pick List", bundle.create_pick_lists([self.item_group])[0]
        )
        self.track("WMS Pick List", pl.name)

        self.assertGreater(len(pl.items), 2)
        self.assertEqual(len({r.item_code for r in pl.items}), 2)

        pf = frappe.get_doc("Print Format", "WMS Pick List")
        html = frappe.render_template(
            pf.html, {"doc": pl, "frappe": frappe, "_": frappe._}
        )
        import re

        text = re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", html))

        for column in (
            "Order", "Item", "Qty needed", "Qty available on batch",
            "Qty to pick from batch", "Location", "Batch", "Picked qty",
            "Batch empty?",
        ):
            self.assertIn(column, text, f"missing column: {column}")

        # Customer at the top, and NO value/price anywhere.
        self.assertIn(self.customer_a, text)
        for money in ("rate", "amount", "valuation", "price", "\u20ac"):
            self.assertNotIn(money, text.lower())

        # One sum row per item.
        self.assertEqual(html.count('class="sum"'), 2)

        # Picked qty / Batch empty? are left blank for the picker.
        self.assertGreaterEqual(html.count('class="fill"'), 2 * len(pl.items))

    # ----------------------------------------------------------------- T11
    def test_t11_wip_pot_is_company_scoped(self):
        """WMS Settings is a global Single, so its WIP fields must be scoped
        to a company — by configuration, not by name matching."""
        from frappe_wms2.wms.doctype.wms_settings.wms_settings import (
            resolve_default_company,
            storage_location_query,
        )
        from frappe_wms2.wms.picking import get_wip_target

        meta = frappe.get_meta("WMS Settings")
        self.assertTrue(meta.has_field("company"))

        # Both WIP fields carry company-scoping link filters.
        wh_filters = meta.get_field("wip_warehouse").link_filters or ""
        self.assertIn("company", wh_filters)
        self.assertIn("eval:doc.company", wh_filters)
        loc_filters = meta.get_field("wip_storage_location").link_filters or ""
        self.assertIn("eval:doc.wip_warehouse", loc_filters)

        # A second, differently named company must not leak into the picker.
        other_name = f"WMS2 Other {self.run_token}"
        if not frappe.db.exists("Company", other_name):
            frappe.get_doc(
                {
                    "doctype": "Company",
                    "company_name": other_name,
                    "abbr": f"O{self.run_token[:3]}",
                    "default_currency": "EUR",
                    "country": "Netherlands",
                }
            ).insert(ignore_permissions=True)
        self.track("Company", other_name)

        other_wh_name = f"WMS2 Other WH {self.run_token}"
        other_abbr = frappe.db.get_value("Company", other_name, "abbr")
        other_wh = f"{other_wh_name} - {other_abbr}"
        if not frappe.db.exists("Warehouse", other_wh):
            frappe.get_doc(
                {
                    "doctype": "Warehouse",
                    "warehouse_name": other_wh_name,
                    "company": other_name,
                }
            ).insert(ignore_permissions=True)
        self.track("Warehouse", other_wh)

        other_loc = f"WMS2-OTHER-{self.run_token}"
        if not frappe.db.exists("Storage Location", other_loc):
            frappe.get_doc(
                {
                    "doctype": "Storage Location",
                    "location_code": other_loc,
                    "warehouse": other_wh,
                    "is_special": 1,
                }
            ).insert(ignore_permissions=True)
        self.track("Storage Location", other_loc)

        # The location search returns only the queried company's locations —
        # for BOTH companies, so this cannot be name matching.
        ours = [row[0] for row in storage_location_query(
            "Storage Location", "", "name", 0, 100, {"company": COMPANY}
        )]
        theirs = [row[0] for row in storage_location_query(
            "Storage Location", "", "name", 0, 100, {"company": other_name}
        )]
        self.assertIn(self.wip_location, ours)
        self.assertNotIn(other_loc, ours)
        self.assertIn(other_loc, theirs)
        self.assertNotIn(self.wip_location, theirs)

        # Company is resolved dynamically, never hardcoded.
        self.assertIn(
            resolve_default_company(),
            frappe.get_all("Company", pluck="name") + [None],
        )

        # Server-side enforcement: a pot from another company is refused.
        settings = frappe.get_doc("WMS Settings")
        settings.company = COMPANY
        settings.wip_warehouse = other_wh
        with self.assertRaises(frappe.ValidationError):
            settings.validate_company_scope()

        # ...and posting refuses it too, whatever the dropdown allowed.
        frappe.flags.wms2_settings_override = {
            "company": other_name,
            "wip_warehouse": other_wh,
            "wip_storage_location": other_loc,
        }
        with self.assertRaises(frappe.ValidationError):
            get_wip_target(COMPANY)

        # Restore the override the other tests rely on.
        frappe.flags.wms2_settings_override = {
            "company": COMPANY,
            "wip_warehouse": self.wip_warehouse,
            "wip_storage_location": self.wip_location,
            "allow_customer_neutral_stock": 1,
        }

    # ------------------------------------------------------------------ T9
    def test_t9_reason_master_is_self_managed_and_shared(self):
        """One shared list covering shortage AND surplus, editable in the
        UI (a plain master, no code needed)."""
        meta = frappe.get_meta("WMS Pick Reason")
        self.assertTrue(meta.has_field("applies_to_shortage"))
        self.assertTrue(meta.has_field("applies_to_surplus"))
        self.assertTrue(frappe.db.exists("WMS Pick Reason", self.reason_short))

        # A user-created reason works immediately.
        custom = f"WMS3A reason {self.run_token}"
        frappe.get_doc(
            {
                "doctype": "WMS Pick Reason",
                "reason": custom,
                "is_active": 1,
                "applies_to_shortage": 1,
                "applies_to_surplus": 1,
            }
        ).insert(ignore_permissions=True)
        self.track("WMS Pick Reason", custom)

        item = self.new_item()
        loc = self.new_location()
        self.receive(item, 5, loc, self.customer_a)
        so = self.new_sales_order(self.customer_a, item, 5)
        mr = self.new_material_request(so, [(item, 5)])
        bundle = self.new_bundle([mr])
        pl = frappe.get_doc(
            "WMS Pick List", bundle.create_pick_lists([self.item_group])[0]
        )
        self.track("WMS Pick List", pl.name)
        pl.items[0].picked_qty = 4
        pl.items[0].reason = custom
        pl.save()  # accepted for a shortage
        self.assertEqual(pl.items[0].reason, custom)
