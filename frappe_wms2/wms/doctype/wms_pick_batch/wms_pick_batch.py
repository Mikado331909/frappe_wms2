# WMS Pick Batch — bundle Material Requests of ONE customer for picking.
#
# The customer is DERIVED from the Sales Order behind each Material Request;
# bundling MRs of different customers is impossible (hard throw, not a
# warning). Demand is summed per item across the bundled MRs. Whatever goes
# onto a generated pick list is reserved and never offered again.

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class WMSPickBatch(Document):
    def validate(self):
        self.resolve_customers()
        self.build_demand()

    def on_submit(self):
        self.status = "Open"

    def on_cancel(self):
        open_lists = frappe.get_all(
            "WMS Pick List",
            filters={"pick_batch": self.name, "docstatus": ("<", 2)},
            pluck="name",
        )
        if open_lists:
            frappe.throw(
                _("Cancel the pick lists first: {0}").format(", ".join(open_lists))
            )
        self.status = "Cancelled"

    # ------------------------------------------------------------ customer

    def resolve_customers(self):
        if not self.material_requests:
            frappe.throw(_("Select at least one Material Request."))

        seen = {}
        for row in self.material_requests:
            sales_order, customer, work_order = get_mr_order_customer(
                row.material_request
            )
            row.sales_order = sales_order
            row.customer = customer
            row.work_order = work_order
            seen.setdefault(customer, []).append(row.material_request)

        if len(seen) > 1:
            details = "<br>".join(
                f"{cust}: {', '.join(mrs)}" for cust, mrs in seen.items()
            )
            frappe.throw(
                _(
                    "A pick batch can only bundle Material Requests of ONE "
                    "customer. Found:<br>{0}"
                ).format(details),
                title=_("Customers cannot be mixed"),
            )

        names = [r.material_request for r in self.material_requests]
        if len(names) != len(set(names)):
            frappe.throw(_("The same Material Request is listed twice."))

        self.customer = next(iter(seen))

    # -------------------------------------------------------------- demand

    def build_demand(self):
        demand = get_bundle_demand(self.material_requests)
        listed = get_listed_qty(self.name)

        self.set("demand_items", [])
        total_demand = total_open = 0.0
        for key in sorted(demand, key=lambda k: (demand[k]["item_group"] or "", k)):
            row = demand[key]
            qty_listed = flt(listed.get(key))
            qty_open = flt(row["qty"]) - qty_listed
            self.append(
                "demand_items",
                {
                    "item_code": key,
                    "item_name": row["item_name"],
                    "item_group": row["item_group"],
                    "stock_uom": row["stock_uom"],
                    "qty_demand": flt(row["qty"]),
                    "qty_listed": qty_listed,
                    "qty_open": qty_open,
                },
            )
            total_demand += flt(row["qty"])
            total_open += qty_open

        self.total_demand = total_demand
        self.total_open = total_open
        if self.docstatus == 1:
            self.status = "Completed" if total_open <= 0 else "Open"

    # ------------------------------------------------------------ pick list

    @frappe.whitelist()
    def create_pick_lists(self, item_groups):
        """Generate ONE pick list per selected item group, for the open
        (not yet listed) demand only."""
        if isinstance(item_groups, str):
            item_groups = frappe.parse_json(item_groups)
        item_groups = [g for g in (item_groups or []) if g]
        if not item_groups:
            frappe.throw(_("Select at least one Item Group."))

        if self.docstatus != 1:
            frappe.throw(_("Submit the pick batch before generating pick lists."))

        created = []
        for group in item_groups:
            name = self._create_pick_list_for_group(group)
            if name:
                created.append(name)

        if not created:
            frappe.throw(
                _("Nothing open to pick for: {0}").format(", ".join(item_groups))
            )

        self.reload()
        self.build_demand()
        self.db_update_all()
        return created

    def _create_pick_list_for_group(self, item_group):
        from frappe_wms2.wms.picking import allocate_fifo, get_settings

        open_map = get_open_demand_per_order(self)
        demand_rows = [
            row for row in open_map if row["item_group"] == item_group and row["qty"] > 0
        ]
        if not demand_rows:
            return None

        lines, shortages = allocate_fifo(demand_rows, self.customer)
        if not lines:
            return None

        settings = get_settings()
        doc = frappe.get_doc(
            {
                "doctype": "WMS Pick List",
                "pick_batch": self.name,
                "customer": self.customer,
                "company": self.company,
                "item_group": item_group,
                "wip_warehouse": settings.wip_warehouse,
                "wip_storage_location": settings.wip_storage_location,
                "items": lines,
            }
        )
        doc.insert(ignore_permissions=True)

        if shortages:
            frappe.msgprint(
                _("Not enough stock for: {0}").format(
                    ", ".join(
                        f"{s['item_code']} ({s['qty_short']})" for s in shortages
                    )
                ),
                indicator="orange",
                title=_("Partly allocated"),
            )
        return doc.name


# --------------------------------------------------------------- helpers


def get_mr_order_customer(material_request):
    """(sales_order, customer, work_order) behind a Material Request.

    The customer still comes from the Sales Order on the MR's own rows. The
    Work Order comes from the MR **header**: verified against v16.26.2, that
    is where ERPNext keeps the field — `Material Request Item` has no
    `work_order` column at all (see the report).
    """
    mr = frappe.db.get_value(
        "Material Request",
        material_request,
        ["name", "docstatus", "material_request_type", "work_order"],
        as_dict=True,
    )
    if not mr:
        frappe.throw(_("Material Request {0} does not exist.").format(material_request))
    if mr.docstatus != 1:
        frappe.throw(
            _("Material Request {0} is not submitted.").format(material_request)
        )

    sales_orders = [
        so
        for so in frappe.get_all(
            "Material Request Item",
            filters={"parent": material_request},
            pluck="sales_order",
        )
        if so
    ]
    sales_orders = list(dict.fromkeys(sales_orders))
    if not sales_orders:
        if mr.work_order:
            frappe.throw(
                _(
                    "Material Request {0} is for Work Order {1}, but no Sales "
                    "Order is linked to its lines, so the customer cannot be "
                    "determined. A pick is always for one customer — link the "
                    "Sales Order behind that Work Order before picking for it."
                ).format(material_request, frappe.bold(mr.work_order)),
                title=_("No customer for this Work Order"),
            )
        frappe.throw(
            _(
                "Material Request {0} is not linked to a Sales Order, so no "
                "customer can be derived. Link the order line to its Sales "
                "Order."
            ).format(material_request),
            title=_("No Sales Order"),
        )
    if len(sales_orders) > 1:
        customers = {
            frappe.db.get_value("Sales Order", so, "customer") for so in sales_orders
        }
        if len(customers) > 1:
            frappe.throw(
                _(
                    "Material Request {0} points at Sales Orders of different "
                    "customers ({1}) — it cannot be picked."
                ).format(material_request, ", ".join(sorted(customers)))
            )

    customer = frappe.db.get_value("Sales Order", sales_orders[0], "customer")
    if not customer:
        frappe.throw(
            _("Sales Order {0} has no customer.").format(sales_orders[0])
        )
    return sales_orders[0], customer, mr.work_order


def get_bundle_demand(mr_rows):
    """Summed demand per item across the bundled MRs."""
    names = [r.material_request for r in mr_rows if r.material_request]
    demand = {}
    if not names:
        return demand
    for row in frappe.get_all(
        "Material Request Item",
        filters={"parent": ("in", names)},
        fields=["item_code", "item_name", "item_group", "stock_uom", "stock_qty", "qty"],
        limit_page_length=0,
    ):
        qty = flt(row.stock_qty) or flt(row.qty)
        entry = demand.setdefault(
            row.item_code,
            {
                "item_name": row.item_name,
                "item_group": row.item_group
                or frappe.get_cached_value("Item", row.item_code, "item_group"),
                "stock_uom": row.stock_uom,
                "qty": 0.0,
            },
        )
        entry["qty"] += qty
    return demand


def get_listed_qty(pick_batch, per_order=False):
    """Qty already reserved on pick lists of this batch (drafts count;
    cancelled lists do not)."""
    filters = {"docstatus": ("<", 2)}
    lists = frappe.get_all(
        "WMS Pick List", filters=dict(filters, pick_batch=pick_batch), pluck="name"
    )
    out = {}
    if not lists:
        return out
    for row in frappe.get_all(
        "WMS Pick List Item",
        filters={"parent": ("in", lists)},
        fields=["item_code", "material_request", "qty_to_pick", "returned_qty"],
        limit_page_length=0,
    ):
        key = (row.material_request, row.item_code) if per_order else row.item_code
        # Phase 3b: a returned quantity is no longer reserved — it went back
        # to stock, so the demand behind it opens up again. (A cancelled pick
        # list drops out entirely: docstatus 2 is excluded above.)
        reserved = flt(row.qty_to_pick) - flt(row.returned_qty)
        out[key] = flt(out.get(key)) + max(0.0, reserved)
    return out


def get_open_demand_per_order(bundle):
    """Open demand per (Material Request, item) — demand minus what is
    already on a pick list. This is the reservation mechanism: reserved
    quantity is never offered to a second pick list."""
    listed = get_listed_qty(bundle.name, per_order=True)
    names = [r.material_request for r in bundle.material_requests]
    so_map = {r.material_request: r.sales_order for r in bundle.material_requests}
    wo_map = {r.material_request: r.work_order for r in bundle.material_requests}

    rows = []
    for item in frappe.get_all(
        "Material Request Item",
        filters={"parent": ("in", names)},
        fields=[
            "parent",
            "item_code",
            "item_name",
            "item_group",
            "stock_uom",
            "stock_qty",
            "qty",
        ],
        order_by="parent asc, idx asc",
        limit_page_length=0,
    ):
        demand_qty = flt(item.stock_qty) or flt(item.qty)
        already = flt(listed.get((item.parent, item.item_code)))
        open_qty = demand_qty - already
        rows.append(
            {
                "material_request": item.parent,
                "sales_order": so_map.get(item.parent),
                "work_order": wo_map.get(item.parent),
                "item_code": item.item_code,
                "item_name": item.item_name,
                "item_group": item.item_group
                or frappe.get_cached_value("Item", item.item_code, "item_group"),
                "stock_uom": item.stock_uom,
                "qty": open_qty,
            }
        )
    return rows


@frappe.whitelist()
def get_item_groups(pick_batch):
    """Item Groups occurring in the bundle, with open qty per group."""
    doc = frappe.get_doc("WMS Pick Batch", pick_batch)
    doc.check_permission("read")
    groups = {}
    for row in doc.demand_items:
        groups.setdefault(row.item_group, {"item_group": row.item_group, "qty_open": 0})
        groups[row.item_group]["qty_open"] += flt(row.qty_open)
    return sorted(groups.values(), key=lambda g: g["item_group"] or "")


@frappe.whitelist()
def make_material_request_for_work_order(work_order, qty_multiplier=1):
    """Raise a Material Request for a Work Order's raw material.

    This exists because ERPNext v16.26.2 has no Work Order -> Material Request
    path of its own (verified: the native direction is Material Request ->
    Work Order, and `Material Request Item` has no `work_order` field at all).
    The MR header field `work_order` DOES exist but nothing in ERPNext ever
    fills it, so without this the picking chain has nothing to read.

    What it sets, and why:
      * `work_order` on the header — what the pick list groups its Stock
        Entries by, and what `get_work_order_consumption()` looks for.
      * `sales_order` on every row — how the customer is derived, exactly as
        for an ordinary customer-shipment request. A Work Order with no Sales
        Order behind it is refused rather than guessed at.
    """
    wo = frappe.get_doc("Work Order", work_order)
    wo.check_permission("read")

    if not wo.sales_order:
        frappe.throw(
            _(
                "Work Order {0} has no Sales Order, so its customer cannot be "
                "determined and its material cannot be picked. Link the Sales "
                "Order first — a pick is always for exactly one customer."
            ).format(frappe.bold(work_order)),
            title=_("No customer for this Work Order"),
        )

    rows = []
    for item in wo.required_items:
        qty = flt(item.required_qty) * flt(qty_multiplier or 1)
        if qty <= 0:
            continue
        rows.append(
            {
                "item_code": item.item_code,
                "qty": qty,
                "warehouse": item.source_warehouse or wo.source_warehouse,
                "schedule_date": wo.planned_start_date or frappe.utils.nowdate(),
                "sales_order": wo.sales_order,
            }
        )

    if not rows:
        frappe.throw(
            _("Work Order {0} has no raw material to request.").format(work_order)
        )

    mr = frappe.get_doc(
        {
            "doctype": "Material Request",
            "material_request_type": "Material Transfer",
            "company": wo.company,
            "transaction_date": frappe.utils.nowdate(),
            "schedule_date": wo.planned_start_date or frappe.utils.nowdate(),
            "work_order": work_order,
            "items": rows,
        }
    )
    mr.insert(ignore_permissions=True)
    return mr


@frappe.whitelist()
def get_material_request_context(material_request):
    """Sales Order / customer / Work Order behind a Material Request.

    Item 2: the bundle form calls this the moment a Material Request is
    picked, so the row fills in immediately instead of only on save. It is
    the SAME derivation the server uses on save (`get_mr_order_customer`),
    not a second implementation that could drift — the save-time check stays
    authoritative, this only shows the answer earlier.
    """
    frappe.has_permission("Material Request", doc=material_request, throw=True)
    sales_order, customer, work_order = get_mr_order_customer(material_request)
    return {
        "sales_order": sales_order,
        "customer": customer,
        "work_order": work_order,
    }
