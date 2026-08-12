import frappe
from frappe import _
from frappe.model.document import Document


class WMSOwnershipType(Document):
    def validate(self):
        # A zero-valuation intake without a customer would be stock that
        # belongs to nobody — almost certainly a configuration mistake.
        if self.zero_valuation_receipt and not self.requires_customer:
            frappe.msgprint(
                _(
                    "Warning: '{0}' receives at zero valuation but does not "
                    "require a Customer. The goods will carry no owner."
                ).format(self.ownership_type),
                indicator="orange",
            )

    def on_trash(self):
        # Batches / receipt lines link here; frappe's link check blocks
        # deletion when referenced. Nothing extra needed, but keep types
        # recoverable by preferring deactivation.
        pass
