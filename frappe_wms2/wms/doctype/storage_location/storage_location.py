# Storage Location: reference document for the "Storage Location" Inventory
# Dimension.
#
# Deliberately holds NO stock quantities. Quantity per (item, batch, warehouse,
# storage location) lives exclusively in ERPNext's Stock Ledger — that is the
# core architectural decision of this app (no parallel stock table, no drift).

import re

import frappe
from frappe import _
from frappe.model.document import Document

# [F|T][gang letter][niveau]-[plaats], e.g. FA1-2
LOCATION_CODE_PATTERN = re.compile(
    r"^(?P<material>[FT])(?P<gang>[A-Z])(?P<niveau>\d+)-(?P<plaats>\d+)$"
)

MATERIAL_MAP = {"F": "Fabrics", "T": "Trimmings"}


class StorageLocation(Document):
    def before_naming(self):
        # autoname (field:location_code) runs BEFORE validate — normalise
        # here so the document name itself is the canonical uppercase code.
        if self.location_code:
            self.location_code = self.location_code.strip().upper()

    def validate(self):
        self.parse_location_code()

    @property
    def is_physical(self):
        return not self.is_special

    def parse_location_code(self):
        code = (self.location_code or "").strip().upper()
        self.location_code = code

        if self.is_special:
            # Sentinel location (e.g. the WIP pot). It exists only because
            # the Storage Location dimension is mandatory on stock lines;
            # it is not a shelf, so it carries no parsed components.
            if not code:
                frappe.throw(_("Location Code is required."))
            self.material = None
            self.gang = None
            self.niveau = 0
            self.plaats = 0
            return

        match = LOCATION_CODE_PATTERN.match(code)
        if not match:
            frappe.throw(
                _(
                    "Invalid Location Code {0}. Expected format: "
                    "[F|T][gang letter][niveau]-[plaats], e.g. FA1-2 "
                    "(F=Fabrics/T=Trimmings, gang A, niveau 1, plaats 2)."
                ).format(frappe.bold(code or _("(empty)"))),
                title=_("Invalid Location Code"),
            )

        self.material = MATERIAL_MAP[match.group("material")]
        self.gang = match.group("gang")
        self.niveau = int(match.group("niveau"))
        self.plaats = int(match.group("plaats"))
