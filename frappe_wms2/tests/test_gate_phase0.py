# PHASE 0 GATE TESTS
# ==================
# These tests prove (or disprove) the native ERPNext behaviours the whole
# architecture rests on. If any of V1, V3 or V4 fails on the installed
# version, that is a STOP-AND-RETHINK signal for the design, not something to
# patch around.
#
# Run on a clean test site:
#   bench --site <test_site> run-tests --app frappe_wms2 \
#       --module frappe_wms2.tests.test_gate_phase0
#
# All assertions read exclusively from native tables (Stock Ledger Entry,
# Serial and Batch Bundle, Bin, GL Entry). There is no app-owned stock table.

import frappe

try:
    from frappe.tests import IntegrationTestCase as TestBase
except ImportError:  # older frappe
    from frappe.tests.utils import FrappeTestCase as TestBase

try:
    from erpnext.stock.doctype.stock_ledger_entry.stock_ledger_entry import (
        InventoryDimensionNegativeStockError,
    )
except ImportError:  # fall back: any validation error still blocks the doc
    InventoryDimensionNegativeStockError = frappe.ValidationError

from frappe_wms2.tests.setup_records import (
    L1,
    L2,
    WAREHOUSE,
    batch_location_balance,
    bin_qty,
    get_sles,
    gl_entries,
    location_balance,
    make_batch,
    make_item,
    make_purchase_receipt,
    make_stock_entry,
    setup_gate_records,
    sle_batch_qty_map,
)

# NOTE on test-record bootstrapping: this module is not doctype-bound, so
# frappe does not auto-create test records for it (and forbids
# IGNORE_TEST_RECORD_DEPENDENCIES outside doctype folders). The doctype-bound
# module (test_storage_location.py) DOES carry that guard: ERPNext's own
# Warehouse -> Company -> Item test records set opening_stock, and the auto
# opening-stock entry has no storage_location — our Mandatory rule (correctly)
# blocks it. All records these tests need are created explicitly in
# setup_records.py, always with a location and never via opening stock.


class TestGatePhase0(TestBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_gate_records()

    # ------------------------------------------------------------------ V0
    def test_v0_dimension_is_registered_and_mandatory(self):
        """Foundation check: dimension registered, fields exist, Mandatory
        actually blocks a stock-affecting line without a location."""
        dim = frappe.get_doc("Inventory Dimension", "Storage Location")
        self.assertEqual(dim.reference_document, "Storage Location")
        self.assertEqual(dim.reqd, 1)
        self.assertEqual(dim.validate_negative_stock, 1)
        self.assertEqual(dim.apply_to_all_doctypes, 1)
        self.assertEqual(dim.target_fieldname, "storage_location")

        for doctype in (
            "Stock Entry Detail",
            "Purchase Receipt Item",
            "Delivery Note Item",
            "Stock Reconciliation Item",
        ):
            self.assertTrue(
                frappe.get_meta(doctype).has_field("storage_location"),
                f"{doctype} is missing the storage_location field",
            )
        self.assertTrue(
            frappe.get_meta("Stock Entry Detail").has_field("to_storage_location")
        )
        self.assertTrue(
            frappe.get_meta("Stock Ledger Entry").has_field("storage_location")
        )

        # Mandatory: a Material Receipt line without a target location must
        # be rejected already at save (server-side enforcement in v16).
        item = make_item()
        with self.assertRaises(frappe.ValidationError):
            make_stock_entry(
                "Material Receipt",
                [{"item_code": item, "qty": 5, "t_warehouse": WAREHOUSE,
                  "basic_rate": 10}],  # no to_storage_location
            )

    # ------------------------------------------------------------------ V1
    def test_v1_same_warehouse_location_transfer_is_gl_neutral(self):
        """Move qty L1 -> L2 inside ONE warehouse: stock ledger records the
        move per location, and NO GL / valuation entries are created."""
        item = make_item()

        receipt = make_stock_entry(
            "Material Receipt",
            [{"item_code": item, "qty": 10, "t_warehouse": WAREHOUSE,
              "to_storage_location": L1, "basic_rate": 10}],
        )
        # Perpetual inventory must be live, otherwise "no GL on transfer"
        # would be a vacuous assertion.
        self.assertTrue(
            gl_entries(receipt),
            "Receipt produced no GL entries — perpetual inventory is not "
            "active on the test company; V1's GL-neutrality check would be "
            "meaningless.",
        )
        self.assertEqual(location_balance(item, L1), 10)

        transfer = make_stock_entry(
            "Material Transfer",
            [{"item_code": item, "qty": 4,
              "s_warehouse": WAREHOUSE, "t_warehouse": WAREHOUSE,
              "storage_location": L1, "to_storage_location": L2}],
        )

        # Stock Ledger records the move: one -4 out of L1, one +4 into L2,
        # both in the same warehouse.
        sles = get_sles(transfer)
        self.assertEqual(len(sles), 2)
        out_sle, in_sle = sles[0], sles[1]
        self.assertEqual(out_sle.actual_qty, -4)
        self.assertEqual(out_sle.storage_location, L1)
        self.assertEqual(in_sle.actual_qty, 4)
        self.assertEqual(in_sle.storage_location, L2)
        self.assertEqual(out_sle.warehouse, WAREHOUSE)
        self.assertEqual(in_sle.warehouse, WAREHOUSE)

        # Stock balance per location moved.
        self.assertEqual(location_balance(item, L1), 6)
        self.assertEqual(location_balance(item, L2), 4)
        # Warehouse total unchanged.
        self.assertEqual(bin_qty(item), 10)

        # THE assertion: no GL entries for a same-warehouse dimension move.
        self.assertEqual(
            gl_entries(transfer),
            [],
            "Same-warehouse location transfer created GL entries — "
            "V1 assumption broken: STOP AND RETHINK.",
        )

    # ------------------------------------------------------------------ V2
    def test_v2_receipt_carries_batch_and_location_together(self):
        """One Purchase Receipt line with batch + location: the resulting
        stock ledger row carries BOTH, and balances filter by both."""
        item = make_item(has_batch_no=True)
        batch = make_batch(item)

        pr = make_purchase_receipt(
            [{"item_code": item, "qty": 10, "rate": 5,
              "use_serial_batch_fields": 1, "batch_no": batch,
              "storage_location": L1}],
        )

        sles = get_sles(pr)
        self.assertEqual(len(sles), 1)
        sle = sles[0]
        self.assertEqual(sle.storage_location, L1)
        # v16 keeps the batch in a Serial and Batch Bundle linked from the
        # SLE (SLE.batch_no itself stays empty on submit) — the helper
        # resolves either representation.
        self.assertEqual(sle_batch_qty_map(sle), {batch: 10})

        # "Stock Balance filters by both": qty for (item, batch, location).
        self.assertEqual(batch_location_balance(item, batch, L1), 10)
        self.assertEqual(batch_location_balance(item, batch, L2), 0)
        self.assertEqual(location_balance(item, L1), 10)

    # ------------------------------------------------------------------ V3
    def test_v3_one_batch_split_across_two_locations(self):
        """Receive ONE batch, part into L1 and part into L2: two stock rows
        of the same batch, each with its own qty; total matches the Bin."""
        item = make_item(has_batch_no=True)
        batch = make_batch(item)

        pr = make_purchase_receipt(
            [
                {"item_code": item, "qty": 6, "rate": 5,
                 "use_serial_batch_fields": 1, "batch_no": batch,
                 "storage_location": L1},
                {"item_code": item, "qty": 4, "rate": 5,
                 "use_serial_batch_fields": 1, "batch_no": batch,
                 "storage_location": L2},
            ],
        )

        sles = get_sles(pr)
        self.assertEqual(
            len(sles), 2,
            "Expected two stock ledger rows (one per location) — "
            "V3 assumption broken: STOP AND RETHINK.",
        )
        by_location = {sle.storage_location: sle for sle in sles}
        self.assertEqual(set(by_location), {L1, L2})
        self.assertEqual(sle_batch_qty_map(by_location[L1]), {batch: 6})
        self.assertEqual(sle_batch_qty_map(by_location[L2]), {batch: 4})

        # Same batch, two locations, per-location batch balances correct.
        self.assertEqual(batch_location_balance(item, batch, L1), 6)
        self.assertEqual(batch_location_balance(item, batch, L2), 4)

        # Sum of locations == warehouse Bin (native total; no drift possible
        # since both numbers come from the same ledger).
        self.assertEqual(bin_qty(item), 10)
        self.assertEqual(
            location_balance(item, L1) + location_balance(item, L2), bin_qty(item)
        )

    # ------------------------------------------------------------------ V4
    def test_v4_cannot_drive_a_location_negative(self):
        """Warehouse holds enough (10) but L1 holds only 6: issuing 8 from L1
        must be blocked by the per-dimension negative stock validation."""
        item = make_item()
        make_stock_entry(
            "Material Receipt",
            [
                {"item_code": item, "qty": 6, "t_warehouse": WAREHOUSE,
                 "to_storage_location": L1, "basic_rate": 10},
                {"item_code": item, "qty": 4, "t_warehouse": WAREHOUSE,
                 "to_storage_location": L2, "basic_rate": 10},
            ],
        )
        self.assertEqual(bin_qty(item), 10)

        # Warehouse-level stock (10) covers the issue of 8 — only the
        # per-LOCATION check can stop this.
        with self.assertRaises(
            InventoryDimensionNegativeStockError,
            msg="Issuing more than the location holds was NOT blocked — "
                "V4 assumption broken: STOP AND RETHINK.",
        ):
            make_stock_entry(
                "Material Issue",
                [{"item_code": item, "qty": 8, "s_warehouse": WAREHOUSE,
                  "storage_location": L1}],
            )

        # Balances untouched by the blocked attempt.
        self.assertEqual(location_balance(item, L1), 6)
        self.assertEqual(location_balance(item, L2), 4)

        # Positive control: the check must not over-block. Issuing exactly
        # what L1 holds succeeds and empties it...
        make_stock_entry(
            "Material Issue",
            [{"item_code": item, "qty": 6, "s_warehouse": WAREHOUSE,
              "storage_location": L1}],
        )
        self.assertEqual(location_balance(item, L1), 0)
        self.assertEqual(bin_qty(item), 4)

        # ...and picking from the now-empty location is blocked again.
        with self.assertRaises(InventoryDimensionNegativeStockError):
            make_stock_entry(
                "Material Issue",
                [{"item_code": item, "qty": 1, "s_warehouse": WAREHOUSE,
                  "storage_location": L1}],
            )
