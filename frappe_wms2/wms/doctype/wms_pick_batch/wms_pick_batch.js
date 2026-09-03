// WMS Pick Batch — bundle screen.
// Shows which Item Groups occur in the bundle with their still-open qty,
// lets the user tick the group(s) to make a pick list for now
// (e.g. fabrics first, then trimmings) and generates one list per group.

frappe.ui.form.on("WMS Pick Batch", {
	refresh(frm) {
		if (frm.doc.docstatus === 1 && frm.doc.status !== "Cancelled") {
			frm.add_custom_button(__("Generate Pick Lists"), () =>
				select_groups_and_generate(frm)
			);
		}
		if (frm.doc.docstatus !== 0) {
			frm.add_custom_button(
				__("Pick Lists"),
				() =>
					frappe.set_route("List", "WMS Pick List", {
						pick_batch: frm.doc.name,
					}),
				__("View")
			);
		}
	},
});

// Item 2: the customer used to appear only after saving, because the
// derivation is server-side. This pulls the same answer the moment a
// Material Request is chosen, so the row and the header fill in straight
// away. The server still derives and enforces it on save — this only shows
// it earlier.
frappe.ui.form.on("WMS Pick Batch MR", {
	material_request(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.material_request) {
			frappe.model.set_value(cdt, cdn, {
				sales_order: null,
				customer: null,
				work_order: null,
			});
			return;
		}

		frappe.call({
			method: "frappe_wms2.wms.doctype.wms_pick_batch.wms_pick_batch.get_material_request_context",
			args: { material_request: row.material_request },
			callback(r) {
				if (!r.message) return;
				frappe.model.set_value(cdt, cdn, {
					sales_order: r.message.sales_order,
					customer: r.message.customer,
					work_order: r.message.work_order,
				});
				if (!frm.doc.customer) {
					frm.set_value("customer", r.message.customer);
				} else if (frm.doc.customer !== r.message.customer) {
					// The hard one-customer rule still throws on save; this is
					// just the earliest possible warning.
					frappe.show_alert({
						message: __(
							"{0} belongs to {1}, but this batch is for {2} — a pick batch holds one customer only.",
							[row.material_request, r.message.customer, frm.doc.customer]
						),
						indicator: "red",
					});
				}
			},
		});
	},
});

function select_groups_and_generate(frm) {
	frappe.call({
		method: "frappe_wms2.wms.doctype.wms_pick_batch.wms_pick_batch.get_item_groups",
		args: { pick_batch: frm.doc.name },
		callback(r) {
			const groups = (r.message || []).filter((g) => g.qty_open > 0);
			if (!groups.length) {
				frappe.msgprint(__("Nothing open to pick — everything is already on a pick list."));
				return;
			}

			const dialog = new frappe.ui.Dialog({
				title: __("Which item group(s) to pick now?"),
				fields: [
					{
						fieldname: "groups",
						fieldtype: "Table",
						cannot_add_rows: true,
						cannot_delete_rows: true,
						in_place_edit: true,
						// Item 3: hide the grid's own row-selector column, so
						// the only checkbox on screen is the "Pick now" one
						// this dialog actually reads.
						check_all_rows: false,
						allow_bulk_edit: false,
						data: groups.map((g) => ({
							select: 0,
							item_group: g.item_group,
							qty_open: g.qty_open,
						})),
						get_data: () => dialog.fields_dict.groups.grid.data,
						fields: [
							{
								fieldname: "select",
								fieldtype: "Check",
								label: __("Pick now"),
								in_list_view: 1,
								columns: 2,
							},
							{
								fieldname: "item_group",
								fieldtype: "Data",
								label: __("Item Group"),
								read_only: 1,
								in_list_view: 1,
								columns: 6,
							},
							{
								fieldname: "qty_open",
								fieldtype: "Float",
								label: __("Open"),
								read_only: 1,
								in_list_view: 1,
								columns: 2,
							},
						],
					},
				],
				primary_action_label: __("Create Pick List(s)"),
				primary_action() {
					const chosen = (dialog.fields_dict.groups.grid.data || [])
						.filter((row) => row.select)
						.map((row) => row.item_group);
					if (!chosen.length) {
						frappe.msgprint(__("Tick at least one item group."));
						return;
					}
					dialog.hide();
					frm.call({
						doc: frm.doc,
						method: "create_pick_lists",
						args: { item_groups: chosen },
						freeze: true,
						freeze_message: __("Allocating stock (FIFO)..."),
					}).then((r) => {
						frm.reload_doc();
						const names = r.message || [];
						if (names.length === 1) {
							frappe.set_route("Form", "WMS Pick List", names[0]);
						} else if (names.length) {
							frappe.msgprint(
								__("Created: {0}", [names.join(", ")])
							);
						}
					});
				},
			});
			dialog.show();

			// The row-selector checkbox column is rendered by the grid
			// itself; there is no flag for it on a dialog grid, so it is
			// removed after render. Two identical-looking checkboxes side by
			// side read as a double selection.
			const grid = dialog.fields_dict.groups.grid;
			grid.wrapper.find(".grid-row-check").addClass("hidden");
			grid.wrapper.find(".grid-heading-row .grid-row-check").addClass("hidden");
		},
	});
}

