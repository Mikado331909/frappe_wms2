# PHASE 2a LIVE TESTS — ownership at intake
# ==========================================
# Run: bench --site <site> run-tests --app frappe_wms2 \
#          --module frappe_wms2.tests.test_ownership_phase2a
#
# Verifies on the installed ERPNext v16:
#   T1  Own-use receipt posts at cost to the own warehouse/inventory account.
#   T2  Supplied-by-customer receipt posts at ZERO value, quantity present,
#       tracked per storage location, routed to the configured warehouse.
#   T3  Customer + Ownership Type land on the Batch (via the Serial and
#       Batch Bundle) and are readable.
#   T4  Missing/invalid intake fields are blocked (ownership missing,
#       customer missing, customer on Own use, non-zero rate on zero-val
#       type, inactive FOB types).
#   T5  One batch can never mix owners (across receipts).
#   T6  Anti-backdoor: Stock Entry Material Receipt enforces the same rule;
#       Purchase Invoice with Update Stock is blocked.
#
# Company-agnostic on purpose: nothing binds to Crings account names —
# accounts resolve from company/warehouse defaults, so the same tests run
# against Crings B.V. (CB/CRINGS) unchanged.

import frappe
from frappe.utils import flt

try:
    from frappe.tests import IntegrationTestCase as TestBase
except ImportError:  # older frappe
    from frappe.tests.utils import FrappeTestCase as TestBase

from frappe_wms2.install import ensure_ownership_types
from frappe_wms2.tests.setup_records import (
    COMPANY,
    L1,
    WAREHOUSE,
    get_or_create_customer,
    get_or_create_location,
    get_sles,
    gl_entries,
    location_balance,
    make_batch,
    make_item,
    make_purchase_receipt,
    make_stock_entry,
    setup_gate_records,
    sle_batch_qty_map,
    throwaway_location_code,
)

OWN_USE = "Own use"
SUPPLIED = "Supplied by customer"
CUSTOMER_A = "WMS2 Customer A"
CUSTOMER_B = "WMS2 Customer B"
CONSIGNMENT_WH_NAME = "Customer Supplied Stock"
# Storage locations inside the consignment warehouse — throwaway codes, so
# the suite is safe on a site full of real locations.
CL1 = throwaway_location_code("T")
CL2 = throwaway_location_code("T")


def batch_stamp(batch_no):
    return frappe.db.get_value(
        "Batch", batch_no, ["wms_ownership_type", "wms_customer"], as_dict=True
    )


class TestOwnershipPhase2a(TestBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_gate_records()
        ensure_ownership_types()
        for name in (CUSTOMER_A, CUSTOMER_B):
            get_or_create_customer(name)

        cls.consignment_wh = cls.get_or_create_consignment_warehouse()
        for code in (CL1, CL2):
            get_or_create_location(code, cls.consignment_wh)

        # Self-managed config: route customer-supplied stock to its own
        # warehouse (exactly what a key user would set in the UI).
        frappe.db.set_value(
            "WMS Ownership Type",
            SUPPLIED,
            "enforce_warehouse",
            cls.consignment_wh,
            update_modified=False,
        )
        frappe.clear_document_cache("WMS Ownership Type", SUPPLIED)

    @classmethod
    def get_or_create_consignment_warehouse(cls):
        abbr = frappe.db.get_value("Company", COMPANY, "abbr")
        name = f"{CONSIGNMENT_WH_NAME} - {abbr}"
        if not frappe.db.exists("Warehouse", name):
            frappe.get_doc(
                {
                    "doctype": "Warehouse",
                    "warehouse_name": CONSIGNMENT_WH_NAME,
                    "company": COMPANY,
                }
            ).insert(ignore_permissions=True)
        return name

    # ------------------------------------------------------------------ T1
    def test_t1_own_use_receipt_posts_at_cost(self):
        item = make_item(has_batch_no=True)
        batch = make_batch(item)

        pr = make_purchase_receipt(
            [{
                "item_code": item, "qty": 10, "rate": 5,
                "warehouse": WAREHOUSE, "storage_location": L1,
                "use_serial_batch_fields": 1, "batch_no": batch,
                "wms_ownership_type": OWN_USE,
            }]
        )

        # Stock ledger carries full value: 10 pcs * 5 = 50.
        sles = get_sles(pr)
        self.assertEqual(len(sles), 1)
        value = frappe.db.get_value(
            "Stock Ledger Entry", sles[0].name, "stock_value_difference"
        )
        self.assertEqual(flt(value), 50)

        # GL: inventory (own warehouse account) debited at cost.
        inventory_account = frappe.db.get_value(
            "Warehouse", WAREHOUSE, "account"
        ) or frappe.db.get_value("Company", COMPANY, "default_inventory_account")
        gles = gl_entries(pr)
        self.assertTrue(gles, "Own-use receipt must post GL entries")
        debit_rows = [g for g in gles if g.account == inventory_account]
        self.assertEqual(sum(flt(g.debit) - flt(g.credit) for g in debit_rows), 50)

        # Batch stamped: Own use, no customer.
        stamp = batch_stamp(batch)
        self.assertEqual(stamp.wms_ownership_type, OWN_USE)
        self.assertFalse(stamp.wms_customer)

    # ------------------------------------------------------------------ T2
    def test_t2_supplied_by_customer_zero_value_per_location(self):
        item = make_item(has_batch_no=True)
        batch = make_batch(item)

        pr = make_purchase_receipt(
            [{
                "item_code": item, "qty": 8, "rate": 0,
                # warehouse intentionally ABSENT (None defeats the factory
                # default): routing must fill the enforced warehouse.
                "warehouse": None,
                "storage_location": CL1,
                "use_serial_batch_fields": 1, "batch_no": batch,
                "wms_ownership_type": SUPPLIED,
                "wms_customer": CUSTOMER_A,
            }]
        )

        # Routed to the consignment warehouse.
        self.assertEqual(pr.items[0].warehouse, self.consignment_wh)

        # Quantity present, value ZERO, on the stock ledger.
        sles = get_sles(pr)
        self.assertEqual(len(sles), 1)
        sle = frappe.db.get_value(
            "Stock Ledger Entry",
            sles[0].name,
            ["actual_qty", "stock_value_difference", "valuation_rate",
             "storage_location"],
            as_dict=True,
        )
        self.assertEqual(flt(sle.actual_qty), 8)
        self.assertEqual(flt(sle.stock_value_difference), 0)
        self.assertEqual(flt(sle.valuation_rate), 0)

        # Tracked per storage location via the existing dimension.
        self.assertEqual(sle.storage_location, CL1)
        self.assertEqual(
            location_balance(item, CL1, warehouse=self.consignment_wh), 8
        )

        # ZERO accounting impact: no GL entries at all.
        self.assertEqual(
            gl_entries(pr),
            [],
            "Customer-supplied stock must never touch the GL / own stock value",
        )

        # T3 (part): batch carries customer + ownership, readable.
        stamp = batch_stamp(batch)
        self.assertEqual(stamp.wms_ownership_type, SUPPLIED)
        self.assertEqual(stamp.wms_customer, CUSTOMER_A)

        # Bundle really is the batch carrier on this version.
        self.assertEqual(sle_batch_qty_map(sles[0]), {batch: 8})

    # ------------------------------------------------------------------ T4
    def test_t4_missing_or_invalid_intake_fields_are_blocked(self):
        item = make_item(has_batch_no=True)

        # (a) No ownership type -> blocked (reqd field and/or server rule).
        # Explicit None defeats the factory's "Own use" default.
        with self.assertRaises(frappe.ValidationError):
            make_purchase_receipt(
                [{"item_code": item, "qty": 1, "rate": 1,
                  "warehouse": WAREHOUSE, "storage_location": L1,
                  "use_serial_batch_fields": 1, "batch_no": make_batch(item),
                  "wms_ownership_type": None}]
            )

        # (b) Supplied by customer without customer -> blocked.
        with self.assertRaises(frappe.ValidationError):
            make_purchase_receipt(
                [{"item_code": item, "qty": 1, "rate": 0,
                  "warehouse": self.consignment_wh, "storage_location": CL1,
                  "use_serial_batch_fields": 1, "batch_no": make_batch(item),
                  "wms_ownership_type": SUPPLIED}]
            )

        # (c) Own use WITH a customer -> blocked (no ambiguous stamping).
        with self.assertRaises(frappe.ValidationError):
            make_purchase_receipt(
                [{"item_code": item, "qty": 1, "rate": 1,
                  "warehouse": WAREHOUSE, "storage_location": L1,
                  "use_serial_batch_fields": 1, "batch_no": make_batch(item),
                  "wms_ownership_type": OWN_USE, "wms_customer": CUSTOMER_A}]
            )

        # (d) Zero-valuation type with a non-zero rate -> blocked.
        with self.assertRaises(frappe.ValidationError):
            make_purchase_receipt(
                [{"item_code": item, "qty": 1, "rate": 7,
                  "warehouse": self.consignment_wh, "storage_location": CL1,
                  "use_serial_batch_fields": 1, "batch_no": make_batch(item),
                  "wms_ownership_type": SUPPLIED, "wms_customer": CUSTOMER_A}]
            )

        # (e) Seeded FOB types exist but are INACTIVE in Phase 2a.
        for fob in ("Purchased with customer", "Purchased for customer"):
            self.assertTrue(frappe.db.exists("WMS Ownership Type", fob))
            with self.assertRaises(frappe.ValidationError):
                make_purchase_receipt(
                    [{"item_code": item, "qty": 1, "rate": 1,
                      "warehouse": WAREHOUSE, "storage_location": L1,
                      "use_serial_batch_fields": 1,
                      "batch_no": make_batch(item),
                      "wms_ownership_type": fob, "wms_customer": CUSTOMER_A}]
                )

    # ------------------------------------------------------------------ T5
    def test_t5_batch_cannot_mix_owners(self):
        item = make_item(has_batch_no=True)
        batch = make_batch(item)

        make_purchase_receipt(
            [{"item_code": item, "qty": 5, "rate": 0,
              "warehouse": self.consignment_wh, "storage_location": CL1,
              "use_serial_batch_fields": 1, "batch_no": batch,
              "wms_ownership_type": SUPPLIED, "wms_customer": CUSTOMER_A}]
        )

        # Same batch, different customer -> blocked.
        with self.assertRaises(frappe.ValidationError):
            make_purchase_receipt(
                [{"item_code": item, "qty": 5, "rate": 0,
                  "warehouse": self.consignment_wh, "storage_location": CL2,
                  "use_serial_batch_fields": 1, "batch_no": batch,
                  "wms_ownership_type": SUPPLIED, "wms_customer": CUSTOMER_B}]
            )

        # Same batch, different ownership type -> blocked.
        with self.assertRaises(frappe.ValidationError):
            make_purchase_receipt(
                [{"item_code": item, "qty": 5, "rate": 1,
                  "warehouse": WAREHOUSE, "storage_location": L1,
                  "use_serial_batch_fields": 1, "batch_no": batch,
                  "wms_ownership_type": OWN_USE}]
            )

        # Same batch, SAME owner -> allowed (top-up receipt).
        pr = make_purchase_receipt(
            [{"item_code": item, "qty": 3, "rate": 0,
              "warehouse": self.consignment_wh, "storage_location": CL2,
              "use_serial_batch_fields": 1, "batch_no": batch,
              "wms_ownership_type": SUPPLIED, "wms_customer": CUSTOMER_A}]
        )
        self.assertTrue(pr.docstatus == 1)

    # ------------------------------------------------------------------ T6
    def test_t6_stock_entry_backdoor_is_closed(self):
        item = make_item(has_batch_no=True)

        # (a) Material Receipt without ownership -> blocked by server rule
        # (field is not reqd on Stock Entry Detail; the hook must catch it).
        # Explicit None defeats the factory's "Own use" default.
        with self.assertRaises(frappe.ValidationError):
            make_stock_entry(
                "Material Receipt",
                [{"item_code": item, "qty": 2, "t_warehouse": WAREHOUSE,
                  "to_storage_location": L1, "basic_rate": 10,
                  "use_serial_batch_fields": 1, "batch_no": make_batch(item),
                  "wms_ownership_type": None}],
            )

        # (b) Customer-supplied via Stock Entry: allowed with the full info,
        # received at zero value, batch stamped.
        batch = make_batch(item)
        se = make_stock_entry(
            "Material Receipt",
            [{"item_code": item, "qty": 4,
              "t_warehouse": self.consignment_wh,
              "to_storage_location": CL1, "basic_rate": 0,
              "use_serial_batch_fields": 1, "batch_no": batch,
              "wms_ownership_type": SUPPLIED, "wms_customer": CUSTOMER_A}],
        )
        sles = get_sles(se)
        self.assertEqual(len(sles), 1)
        self.assertEqual(
            flt(frappe.db.get_value(
                "Stock Ledger Entry", sles[0].name, "stock_value_difference"
            )),
            0,
        )
        self.assertEqual(gl_entries(se), [])
        stamp = batch_stamp(batch)
        self.assertEqual(
            (stamp.wms_ownership_type, stamp.wms_customer),
            (SUPPLIED, CUSTOMER_A),
        )

        # (c) Purchase Invoice with Update Stock -> blocked outright.
        supplier = frappe.db.get_value("Supplier", {}, "name")
        pi = frappe.get_doc(
            {
                "doctype": "Purchase Invoice",
                "supplier": supplier,
                "company": COMPANY,
                "currency": "EUR",
                "conversion_rate": 1,
                "update_stock": 1,
                "items": [{
                    "item_code": item, "qty": 1, "rate": 1,
                    "warehouse": WAREHOUSE,
                }],
            }
        )
        with self.assertRaises(frappe.ValidationError):
            pi.insert(ignore_permissions=True)

    # ------------------------------------------------------------ hygiene
    def test_storage_locations_stay_customer_neutral(self):
        """Design guard: Storage Location must not grow a customer binding —
        ownership travels on the batch, never on the location."""
        meta = frappe.get_meta("Storage Location")
        for df in meta.fields:
            self.assertNotIn(
                "customer",
                (df.fieldname or "").lower(),
                "Storage Location must stay customer-neutral (Phase 2a rule 5)",
            )
            self.assertNotEqual(
                (df.options or ""),
                "Customer",
                "Storage Location must not link to Customer",
            )
