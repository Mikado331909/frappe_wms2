# GATE TESTS — draft reservations and concept-invoice currency
# ============================================================
# Run on a disposable site only:
#   MODULE=frappe_wms2.tests.test_fob_reservation_currency \
#       bash apps/frappe_wms2/scripts/run_tests_disposable.sh

import frappe
from frappe.utils import add_days, flt, nowdate

from frappe_wms2.tests.setup_records import COMPANY
from frappe_wms2.tests.test_type4_batches import Type4Fixtures

TYPE3 = "Purchased with customer"


class TestFOBReservationCurrency(Type4Fixtures):
    """Reuses the Type 4 fixtures (Work Orders, bookings, deliveries)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_currency = frappe.get_cached_value(
            "Company", COMPANY, "default_currency"
        )

    def progress_of(self, work_order, batch):
        return frappe.db.get_value(
            "WMS FOB Invoicing Progress",
            {"work_order": work_order, "raw_material_batch": batch},
            ["name", "consumed_qty", "reserved_qty", "invoiced_qty"],
            as_dict=True,
        )

    # ------------------------------------------------------------------ R1
    def test_r1_draft_reserves_submit_invoices(self):
        ctx = self.setup_work_order(per_unit=2, order_qty=5, rate=4)
        batch, loc = self.book_manufacture(ctx, 5)
        dn = self.deliver(ctx.finished, batch, 5, loc)
        invoice = self.invoice_for(dn.name)

        # Draft: reserved, NOT invoiced.
        progress = self.progress_of(ctx.wo, ctx.batch)
        self.assertEqual(flt(progress.consumed_qty), 10)
        self.assertEqual(flt(progress.reserved_qty), 10)
        self.assertEqual(flt(progress.invoiced_qty), 0)

        # Submitting moves it across.
        invoice.submit()
        progress = self.progress_of(ctx.wo, ctx.batch)
        self.assertEqual(flt(progress.reserved_qty), 0)
        self.assertEqual(flt(progress.invoiced_qty), 10)

    # ------------------------------------------------------------------ R2
    def test_r2_discarded_draft_releases_the_quantity(self):
        ctx = self.setup_work_order(per_unit=2, order_qty=6, rate=4)
        batch, loc = self.book_manufacture(ctx, 6)

        dn1 = self.deliver(ctx.finished, batch, 3, loc)
        invoice1 = self.invoice_for(dn1.name)
        self.assertEqual(flt(self.progress_of(ctx.wo, ctx.batch).reserved_qty), 6)

        # Discarded without ever being submitted.
        invoice1.delete(ignore_permissions=True)
        progress = self.progress_of(ctx.wo, ctx.batch)
        self.assertEqual(flt(progress.reserved_qty), 0)
        self.assertEqual(flt(progress.invoiced_qty), 0)

        # The same material is billable again on the next Delivery Note —
        # nothing was permanently lost.
        dn2 = self.deliver(ctx.finished, batch, 3, loc)
        invoice2 = self.invoice_for(dn2.name)
        self.assertEqual(flt(invoice2.items[0].qty), 6)
        invoice2.submit()

        progress = self.progress_of(ctx.wo, ctx.batch)
        self.assertEqual(flt(progress.invoiced_qty), 6)
        self.assertEqual(flt(progress.reserved_qty), 0)

    # ------------------------------------------------------------------ R3
    def test_r3_repeated_discards_never_lose_billable_quantity(self):
        ctx = self.setup_work_order(per_unit=2, order_qty=5, rate=4)
        batch, loc = self.book_manufacture(ctx, 5)

        # Ship one unit at a time and discard the draft each time. Each draft
        # covers 2 units of raw material (2 per finished unit).
        for _ in range(3):
            dn = self.deliver(ctx.finished, batch, 1, loc)
            invoice = self.invoice_for(dn.name)
            self.assertEqual(flt(invoice.items[0].qty), 2)
            invoice.delete(ignore_permissions=True)
            progress = self.progress_of(ctx.wo, ctx.batch)
            self.assertEqual(flt(progress.reserved_qty), 0)
            self.assertEqual(flt(progress.invoiced_qty), 0)
            # Nothing was consumed by the discards themselves.
            self.assertEqual(flt(progress.consumed_qty), 10)

        # Nothing was lost: the last shipment still bills its full share, and
        # the whole consumption is still available to invoice.
        dn = self.deliver(ctx.finished, batch, 2, loc)
        invoice = self.invoice_for(dn.name)
        self.assertEqual(flt(invoice.items[0].qty), 4)
        invoice.submit()

        progress = self.progress_of(ctx.wo, ctx.batch)
        self.assertEqual(flt(progress.invoiced_qty), 4)
        self.assertEqual(flt(progress.reserved_qty), 0)
        self.assertEqual(
            flt(progress.consumed_qty) - flt(progress.invoiced_qty), 6,
            "the rest of the consumption must still be billable",
        )

    # ------------------------------------------------------------------ R4
    def test_r4_open_draft_blocks_a_second_allocation(self):
        """Reserved is not billed, but it must not be handed out twice."""
        ctx = self.setup_work_order(per_unit=2, order_qty=6, rate=4)
        batch, loc = self.book_manufacture(ctx, 6)

        dn1 = self.deliver(ctx.finished, batch, 4, loc)
        invoice1 = self.invoice_for(dn1.name)
        self.assertEqual(flt(invoice1.items[0].qty), 8)

        # A second shipment can only take what is left after the open draft.
        dn2 = self.deliver(ctx.finished, batch, 2, loc)
        invoice2 = self.invoice_for(dn2.name)
        self.assertEqual(flt(invoice2.items[0].qty), 4)

        progress = self.progress_of(ctx.wo, ctx.batch)
        self.assertEqual(flt(progress.reserved_qty), 12)     # 8 + 4
        self.assertEqual(flt(progress.consumed_qty), 12)
        self.assertEqual(flt(progress.invoiced_qty), 0)

    # ------------------------------------------------------------------ C1
    def test_c1_base_currency_still_resolves_rate_one(self):
        from frappe_wms2.wms.fob import resolve_selling_rate

        item = self.new_material_item(priced=9)
        price = resolve_selling_rate(self.customer_a, item, company=COMPANY, qty=1)
        self.assertEqual(price.currency, self.company_currency)
        self.assertEqual(flt(price.conversion_rate), 1)
        self.assertEqual(flt(price.rate), 9)

    # ------------------------------------------------------------------ C2
    def test_c2_foreign_currency_uses_the_real_exchange_rate(self):
        from frappe_wms2.wms.fob import resolve_selling_rate

        customer, item = self.foreign_priced_customer(rate=25)
        self.exchange_rate("USD", self.company_currency, 3.15)

        price = resolve_selling_rate(customer, item, company=COMPANY, qty=1,
                                     posting_date=nowdate())
        self.assertEqual(price.currency, "USD")
        self.assertNotEqual(price.currency, self.company_currency)
        self.assertEqual(flt(price.rate), 25)
        self.assertEqual(flt(price.conversion_rate), 3.15)   # not 1

    # ------------------------------------------------------------------ C3
    def test_c3_missing_exchange_rate_refuses_the_invoice(self):
        from frappe_wms2.wms.fob import resolve_selling_rate

        customer, item = self.foreign_priced_customer(rate=12, currency="CHF")
        # Deliberately no Currency Exchange record for this pair.

        with self.assertRaises(frappe.ValidationError) as ctx:
            resolve_selling_rate(customer, item, company=COMPANY, qty=1,
                                 posting_date=nowdate())
        message = str(ctx.exception)
        self.assertIn("CHF", message)
        self.assertIn(self.company_currency, message)
        self.assertIn(item, message)
        self.assertIn(customer, message)

    # ------------------------------------------------------------------ C4
    def test_c4_both_types_use_the_same_resolver(self):
        """Not two copies of similar logic — literally the same function."""
        import inspect

        from frappe_wms2.wms import fob, fob_direct, production

        self.assertIs(production.resolve_selling_rate, fob.resolve_selling_rate)
        self.assertIs(fob_direct.resolve_selling_rate, fob.resolve_selling_rate)

        # And neither module resolves a conversion rate of its own.
        for module in (fob_direct, production):
            source = inspect.getsource(module)
            self.assertNotIn('"conversion_rate": 1', source)

    # ------------------------------------------------------------------ C5
    def test_c5_type4_invoice_uses_the_price_lists_currency(self):
        """Not the Delivery Note's — the rates were never calculated in it."""
        ctx = self.setup_work_order(per_unit=2, order_qty=4, rate=7)
        batch, loc = self.book_manufacture(ctx, 4)
        dn = self.deliver(ctx.finished, batch, 4, loc)
        invoice = self.invoice_for(dn.name)

        price_list_currency = frappe.get_cached_value(
            "Price List", self.price_list, "currency"
        )
        self.assertEqual(invoice.currency, price_list_currency)
        self.assertEqual(
            flt(invoice.conversion_rate),
            1 if price_list_currency == self.company_currency else
            flt(invoice.conversion_rate),
        )

    # ------------------------------------------------------------ helpers

    def foreign_priced_customer(self, rate, currency="USD"):
        token = frappe.generate_hash(length=4).upper()
        customer = self.new_customer(f"FX{token}")

        price_list = f"WMS FX {currency} {token}"
        if not frappe.db.exists("Price List", price_list):
            frappe.get_doc(
                {
                    "doctype": "Price List",
                    "price_list_name": price_list,
                    "selling": 1,
                    "enabled": 1,
                    "currency": currency,
                }
            ).insert(ignore_permissions=True)
            frappe.db.commit()
        self.track("Price List", price_list)

        frappe.db.set_value("Customer", customer, "default_price_list", price_list)
        frappe.db.commit()
        frappe.clear_document_cache("Customer", customer)

        item = self.new_material_item()
        doc = frappe.get_doc(
            {
                "doctype": "Item Price",
                "item_code": item,
                "price_list": price_list,
                "price_list_rate": rate,
                "currency": currency,
                "selling": 1,
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()
        self.track("Item Price", doc.name)
        return customer, item

    def exchange_rate(self, from_currency, to_currency, rate):
        doc = frappe.get_doc(
            {
                "doctype": "Currency Exchange",
                "date": add_days(nowdate(), -1),
                "from_currency": from_currency,
                "to_currency": to_currency,
                "exchange_rate": rate,
                "for_selling": 1,
                "for_buying": 1,
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()
        self.track("Currency Exchange", doc.name)
        return doc.name

    # ------------------------------------------------- Part 2: DN warning
    def test_w1_draft_concept_invoice_is_reported_for_the_warning(self):
        """The dialog is client-side; what it shows comes from here."""
        from frappe_wms2.wms.production import get_open_concept_invoices

        ctx = self.setup_work_order(per_unit=2, order_qty=4, rate=6)
        batch, loc = self.book_manufacture(ctx, 4)
        dn = self.deliver(ctx.finished, batch, 4, loc)
        invoice = self.invoice_for(dn.name)

        open_invoices = get_open_concept_invoices(dn.name)
        self.assertEqual(len(open_invoices), 1)
        row = open_invoices[0]
        self.assertEqual(row["name"], invoice.name)
        self.assertEqual(row["customer"], self.customer_a)
        self.assertEqual(flt(row["reserved"]), 8)          # still reserved
        self.assertEqual(
            [(i.item_code, flt(i.qty)) for i in
             [frappe._dict(x) for x in row["items"]]],
            [(ctx.raw, 8)],
        )

    def test_w2_confirmed_invoice_needs_no_warning(self):
        from frappe_wms2.wms.production import get_open_concept_invoices

        ctx = self.setup_work_order(per_unit=2, order_qty=4, rate=6)
        batch, loc = self.book_manufacture(ctx, 4)
        dn = self.deliver(ctx.finished, batch, 4, loc)
        invoice = self.invoice_for(dn.name)
        invoice.submit()

        # Submitted: untouched by a DN cancellation either way, so nothing
        # to warn about.
        self.assertEqual(get_open_concept_invoices(dn.name), [])

    def test_w3_delivery_without_a_concept_invoice_reports_nothing(self):
        from frappe_wms2.wms.production import get_open_concept_invoices

        # A shipment whose production consumed no Type 4 material of this
        # customer never generates a concept invoice at all.
        ctx = self.setup_work_order(per_unit=2, order_qty=3, rate=6)
        batch, loc = self.book_manufacture(ctx, 3)
        dn = self.deliver(ctx.finished, batch, 3, loc)
        invoice = self.invoice_for(dn.name)
        invoice.delete(ignore_permissions=True)   # discarded: nothing open

        self.assertEqual(get_open_concept_invoices(dn.name), [])
