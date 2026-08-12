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
		},
	});
}
