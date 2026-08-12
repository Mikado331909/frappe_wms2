# Unit tests for the Storage Location doctype (schema + code parsing).
# Run: bench --site <site> run-tests --app frappe_wms2 --module frappe_wms2.wms.doctype.storage_location.test_storage_location

import frappe

try:
    from frappe.tests import IntegrationTestCase as TestBase
except ImportError:  # older frappe
    from frappe.tests.utils import FrappeTestCase as TestBase

from frappe_wms2.tests.setup_records import (
    WAREHOUSE,
    get_or_create_company,
    get_or_create_warehouse,
    throwaway_location_code,
)

# Do NOT let frappe bootstrap ERPNext's own test records for linked doctypes:
# that chain (Warehouse -> Company -> Item) creates items with opening_stock,
# whose auto opening-stock entry carries no storage_location and is therefore
# (correctly) blocked by our mandatory dimension. We create everything we
# need ourselves, always with a location.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Warehouse", "UOM", "Company"]

# Opt out of frappe's automatic test-record generation for linked doctypes:
# walking Warehouse -> Company -> ... imports ERPNext test modules that create
# opening stock WITHOUT a storage location — which our own Mandatory dimension
# (correctly) blocks. We provision company + warehouse ourselves instead,
# always supplying locations where stock is involved.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Warehouse", "UOM"]


class TestStorageLocation(TestBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        get_or_create_company()
        get_or_create_warehouse()

    def _new(self, code):
        return frappe.get_doc(
            {
                "doctype": "Storage Location",
                "location_code": code,
                "warehouse": WAREHOUSE,
            }
        )

    def test_code_is_parsed_into_components(self):
        # Throwaway code (the live site has 622 real ones); lowercase on
        # purpose: it must be normalised.
        code = throwaway_location_code()
        doc = self._new(code.lower())
        doc.insert(ignore_permissions=True)
        material, gang, niveau, plaats = (
            code[0], code[1], int(code[2:].split("-")[0]), int(code.split("-")[1])
        )
        self.assertEqual(doc.name, code)
        self.assertEqual(doc.location_code, code)
        self.assertEqual(doc.material, "Fabrics" if material == "F" else "Trimmings")
        self.assertEqual(doc.gang, gang)
        self.assertEqual(doc.niveau, niveau)
        self.assertEqual(doc.plaats, plaats)
        doc.delete()

    def test_trimmings_code(self):
        doc = self._new(throwaway_location_code("T"))
        doc.insert(ignore_permissions=True)
        self.assertEqual(doc.material, "Trimmings")
        self.assertIn(doc.gang, ("X", "Y", "Z"))
        self.assertTrue(doc.niveau >= 700)
        self.assertTrue(doc.plaats >= 100)
        doc.delete()

    def test_invalid_codes_are_rejected(self):
        for bad in ("XA1-2", "F11-2", "FA1", "FA-2", "FA1-2-3", "", "FAB1-2"):
            with self.assertRaises(frappe.ValidationError, msg=f"code={bad!r}"):
                self._new(bad).insert(ignore_permissions=True)

    def test_location_code_is_unique(self):
        code = throwaway_location_code()
        self._new(code).insert(ignore_permissions=True)
        with self.assertRaises(frappe.exceptions.DuplicateEntryError):
            self._new(code).insert(ignore_permissions=True)
        frappe.delete_doc("Storage Location", code, force=True)

    def test_doctype_holds_no_stock_quantities(self):
        """Architectural guard: the location must never grow quantity fields.

        Stock per location lives ONLY in the Stock Ledger. Numeric fields on
        this doctype are limited to the parsed code components and capacity.
        """
        allowed_numeric = {"niveau", "plaats", "capacity"}
        meta = frappe.get_meta("Storage Location")
        for df in meta.fields:
            if df.fieldtype in ("Int", "Float", "Currency"):
                self.assertIn(
                    df.fieldname,
                    allowed_numeric,
                    f"Unexpected numeric field '{df.fieldname}' on Storage "
                    "Location — locations must not hold stock numbers.",
                )
            self.assertNotRegex(
                df.fieldname,
                r"qty|stock_value|balance",
                f"Field '{df.fieldname}' looks like a stock quantity — "
                "not allowed on Storage Location.",
            )
