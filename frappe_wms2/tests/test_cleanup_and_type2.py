# GATE TESTS — legacy customer-warehouse cleanup + Type 2 material routing
# ========================================================================
# Run on a disposable site only:
#   MODULE=frappe_wms2.tests.test_cleanup_and_type2 \
#       bash apps/frappe_wms2/scripts/run_tests_disposable.sh

import frappe
from frappe.utils import flt

from frappe_wms2.tests.setup_records import (
    COMPANY,
    WAREHOUSE,
    make_batch,
    make_purchase_receipt,
)
from frappe_wms2.tests.test_fob_type4 import FOBFixtures
from frappe_wms2.wms.fob import FABRICS, TRIMMINGS
from frappe_wms2.wms.legacy_cleanup import (
    LEGACY_PREFIX,
    cleanup_legacy_customer_warehouses,
    find_legacy_customer_warehouses,
)
from frappe_wms2.wms.material_warehouse import get_material_warehouse

TYPE2 = "Supplied by customer"


class TestCleanupAndType2(FOBFixtures):
    # ------------------------------------------------------------ helpers

    def make_legacy_warehouse(self, customer, item_group):
        """A warehouse named exactly as the removed mechanism named them:
        "{customer} - {material item group}" (+ the company abbr suffix)."""
        name = f"{customer} - {item_group}"
        doc = frappe.get_doc(
            {"doctype": "Warehouse", "warehouse_name": name, "company": COMPANY}
        ).insert(ignore_permissions=True)
        frappe.db.commit()
        self.track("Warehouse", doc.name)
        return doc.name

    def receive_type2(self, item, qty, location, customer, batch=None,
                      warehouse=None):
        batch = batch or self.track("Batch", make_batch(item))
        row = {
            "item_code": item, "qty": qty, "rate": 0,
            "storage_location": location,
            "use_serial_batch_fields": 1, "batch_no": batch,
            "wms_ownership_type": TYPE2, "wms_customer": customer,
        }
        row["warehouse"] = warehouse  # None lets the routing resolve it
        pr = make_purchase_receipt([row])
        return pr, batch

    # =============================================== Part 1 — cleanup

    def test_c1_empty_legacy_warehouse_is_disabled_and_marked(self):
        legacy = self.make_legacy_warehouse(self.customer_a, self.group_trimmings)
        self.assertIn(legacy, [w.name for w in find_legacy_customer_warehouses()])

        cleaned, skipped = cleanup_legacy_customer_warehouses(verbose=False)
        self.assertIn(legacy, cleaned)
        self.assertEqual(skipped, [])

        wh = frappe.db.get_value(
            "Warehouse", legacy, ["disabled", "warehouse_name"], as_dict=True
        )
        self.assertEqual(wh.disabled, 1)
        self.assertTrue(wh.warehouse_name.startswith(LEGACY_PREFIX))

        # The settings pickers exclude disabled warehouses, so it can never be
        # selected again — this is what the link_filters enforce in the UI.
        meta = frappe.get_meta("WMS Settings")
        for fieldname in ("fabrics_warehouse", "trimmings_warehouse",
                          "wip_warehouse"):
            self.assertIn("disabled", meta.get_field(fieldname).link_filters or "")

        offered = frappe.get_all(
            "Warehouse",
            filters={"company": COMPANY, "is_group": 0, "disabled": 0},
            pluck="name",
        )
        self.assertNotIn(legacy, offered)

    def test_c2_legacy_warehouse_with_stock_is_left_alone_and_named(self):
        legacy = self.make_legacy_warehouse(self.customer_b, self.group_fabrics)

        item = self.new_material_item()
        location = self.new_location()
        batch = self.track("Batch", make_batch(item))
        make_purchase_receipt(
            [{
                "item_code": item, "qty": 5, "rate": 2,
                "warehouse": legacy, "storage_location": location,
                "use_serial_batch_fields": 1, "batch_no": batch,
                "wms_ownership_type": "Own use",
            }]
        )
        frappe.db.commit()

        cleaned, skipped = cleanup_legacy_customer_warehouses(verbose=False)
        self.assertNotIn(legacy, cleaned)
        self.assertIn(legacy, skipped)   # named for the owner to investigate

        wh = frappe.db.get_value(
            "Warehouse", legacy, ["disabled", "warehouse_name"], as_dict=True
        )
        self.assertEqual(wh.disabled, 0)                       # untouched
        self.assertFalse(wh.warehouse_name.startswith(LEGACY_PREFIX))

    def test_c3_no_legacy_warehouses_is_a_clean_no_op(self):
        # Nothing matching the convention exists beyond what other tests make;
        # run twice and confirm the second run has nothing left to do.
        cleanup_legacy_customer_warehouses(verbose=False)
        cleaned, skipped = cleanup_legacy_customer_warehouses(verbose=False)
        self.assertEqual(cleaned, [])

    def test_c4_cleanup_is_idempotent(self):
        legacy = self.make_legacy_warehouse(self.customer_a, self.group_fabrics)

        first_cleaned, _ = cleanup_legacy_customer_warehouses(verbose=False)
        self.assertIn(legacy, first_cleaned)
        state_after_first = frappe.db.get_value(
            "Warehouse", legacy, ["disabled", "warehouse_name"], as_dict=True
        )

        second_cleaned, _ = cleanup_legacy_customer_warehouses(verbose=False)
        self.assertNotIn(legacy, second_cleaned)   # nothing left to rename
        state_after_second = frappe.db.get_value(
            "Warehouse", legacy, ["disabled", "warehouse_name"], as_dict=True
        )
        self.assertEqual(dict(state_after_first), dict(state_after_second))
        # And no double prefix.
        self.assertEqual(
            state_after_second.warehouse_name.count(LEGACY_PREFIX), 1
        )

    def test_c5_configured_and_unrelated_warehouses_are_never_touched(self):
        """The company's real warehouses must survive, including one whose
        name happens to contain a dash."""
        decoy = frappe.get_doc(
            {
                "doctype": "Warehouse",
                "warehouse_name": f"Central - Overflow {self.run_token}",
                "company": COMPANY,
            }
        ).insert(ignore_permissions=True)
        self.track("Warehouse", decoy.name)

        # A warehouse matching the convention BUT configured in WMS Settings
        # is off limits — protects against a mis-selection already made.
        # A distinct customer/group pair, so this cannot collide with the
        # warehouses the other tests in this class create.
        configured = self.make_legacy_warehouse(
            self.customer_b, self.group_trim_child
        )
        frappe.db.set_single_value("WMS Settings", "trimmings_warehouse", configured)
        frappe.clear_cache(doctype="WMS Settings")

        cleaned, _ = cleanup_legacy_customer_warehouses(verbose=False)
        self.assertNotIn(decoy.name, cleaned)
        self.assertNotIn(configured, cleaned)
        self.assertEqual(frappe.db.get_value("Warehouse", decoy.name, "disabled"), 0)

        # restore the real setting for the rest of the class
        frappe.db.set_single_value(
            "WMS Settings", "trimmings_warehouse", self.wh_trimmings
        )
        frappe.clear_cache(doctype="WMS Settings")

    # =========================================== Part 2 — Type 2 routing

    def test_t2r1_type2_routes_per_material(self):
        """Fabrics and trimmings no longer share one static warehouse."""
        trim_item = self.new_material_item(group=self.group_trim_child)
        fab_item = self.new_material_item(group=self.group_fabrics)

        pr_trim, _b1 = self.receive_type2(
            trim_item, 5, self.new_location(), self.customer_a
        )
        pr_fab, _b2 = self.receive_type2(
            fab_item, 5, self.new_location(), self.customer_a
        )

        self.assertEqual(pr_trim.items[0].warehouse, get_material_warehouse(TRIMMINGS))
        self.assertEqual(pr_fab.items[0].warehouse, get_material_warehouse(FABRICS))
        self.assertNotEqual(
            pr_trim.items[0].warehouse, pr_fab.items[0].warehouse
        )
        # And neither is a per-customer warehouse.
        for pr in (pr_trim, pr_fab):
            self.assertNotIn(self.customer_a, pr.items[0].warehouse)

    def test_t2r2_zero_valuation_and_stamping_unchanged(self):
        """The routing change must not have touched what already worked."""
        item = self.new_material_item(group=self.group_trim_child)
        location = self.new_location()
        pr, batch = self.receive_type2(item, 8, location, self.customer_a)

        sle = frappe.db.get_value(
            "Stock Ledger Entry",
            {"voucher_no": pr.name, "is_cancelled": 0},
            ["actual_qty", "stock_value_difference", "valuation_rate",
             "storage_location", "warehouse"],
            as_dict=True,
        )
        self.assertEqual(flt(sle.actual_qty), 8)
        self.assertEqual(flt(sle.stock_value_difference), 0)   # still zero-valued
        self.assertEqual(flt(sle.valuation_rate), 0)
        self.assertEqual(sle.storage_location, location)
        self.assertEqual(sle.warehouse, get_material_warehouse(TRIMMINGS))

        # No GL impact, exactly as before.
        self.assertEqual(
            frappe.get_all("GL Entry", filters={"voucher_no": pr.name,
                                                "is_cancelled": 0}),
            [],
        )

        # Stamp unchanged.
        stamp = frappe.db.get_value(
            "Batch", batch, ["wms_ownership_type", "wms_customer"], as_dict=True
        )
        self.assertEqual(stamp.wms_ownership_type, TYPE2)
        self.assertEqual(stamp.wms_customer, self.customer_a)

        # A non-zero rate is still refused for a zero-valuation type.
        with self.assertRaises(frappe.ValidationError):
            make_purchase_receipt(
                [{
                    "item_code": item, "qty": 1, "rate": 5,
                    "warehouse": None, "storage_location": self.new_location(),
                    "use_serial_batch_fields": 1,
                    "batch_no": self.track("Batch", make_batch(item)),
                    "wms_ownership_type": TYPE2, "wms_customer": self.customer_a,
                }]
            )
        frappe.db.rollback()

    def test_t2r3_unclassified_item_is_refused_for_type2_too(self):
        stray = self.new_material_item(group=self.group_unrelated)
        with self.assertRaises(frappe.ValidationError) as ctx:
            self.receive_type2(stray, 3, self.new_location(), self.customer_a)
        message = str(ctx.exception)
        self.assertIn(stray, message)
        self.assertIn(self.group_unrelated, message)
        frappe.db.rollback()

    def test_t2r4_wrong_prefilled_warehouse_is_corrected(self):
        """As for the FOB types: a pre-filled default is corrected, not
        refused."""
        item = self.new_material_item(group=self.group_trim_child)
        pr, _batch = self.receive_type2(
            item, 4, self.new_location(), self.customer_b, warehouse=WAREHOUSE
        )
        self.assertEqual(pr.items[0].warehouse, get_material_warehouse(TRIMMINGS))
