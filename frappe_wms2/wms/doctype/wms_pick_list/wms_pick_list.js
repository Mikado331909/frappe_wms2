// WMS Pick List — processing screen.
// Everything stays a draft while the processor types: nothing mutates stock
// until submit. The reason/comment fields simply reveal themselves when a
// line differs or the batch-empty box is ticked.

frappe.ui.form.on("WMS Pick List", {
	refresh(frm) {
		if (frm.doc.docstatus === 0) {
			frm.add_custom_button(__("Add line from another batch"), () =>
				add_line_dialog(frm)
			);
		}
		if (frm.doc.docstatus === 1) {
			frm.add_custom_button(__("Cancel Pick List"), () => cancel_dialog(frm));
			frm.add_custom_button(__("Return Qty"), () => return_dialog(frm));
		}
		if (frm.doc.docstatus === 1 && frm.doc.stock_entry) {
			frm.add_custom_button(
				__("Pick Stock Entry"),
				() => frappe.set_route("Form", "Stock Entry", frm.doc.stock_entry),
				__("View")
			);
		}
		if (frm.doc.docstatus === 0) {
			frm.dashboard.add_comment(
				__("Nothing is booked until you submit. Reasons are required where the picked qty differs or a batch is flagged empty."),
				"blue",
				true
			);
		}
	},
});

frappe.ui.form.on("WMS Pick List Item", {
	picked_qty(frm, cdt, cdn) {
		refresh_totals(frm);
		frm.refresh_field("items");
	},
	batch_empty(frm, cdt, cdn) {
		frm.refresh_field("items");
	},
});

function refresh_totals(frm) {
	let picked = 0;
	(frm.doc.items || []).forEach((row) => {
		picked += flt(row.picked_qty);
	});
	frm.set_value("total_picked", picked);
}

function add_line_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Add a line (same customer only)"),
		fields: [
			{
				fieldname: "item_code",
				fieldtype: "Link",
				options: "Item",
				label: __("Item"),
				reqd: 1,
			},
			{ fieldname: "stock_html", fieldtype: "HTML" },
		],
		primary_action_label: __("Close"),
		primary_action() {
			dialog.hide();
		},
	});

	dialog.fields_dict.item_code.df.onchange = () => {
		const item_code = dialog.get_value("item_code");
		if (!item_code) return;
		frappe.call({
			method: "frappe_wms2.wms.doctype.wms_pick_list.wms_pick_list.get_pickable_stock",
			args: { pick_list: frm.doc.name, item_code },
			callback(r) {
				const rows = r.message || [];
				const $wrap = dialog.fields_dict.stock_html.$wrapper.empty();
				if (!rows.length) {
					$wrap.append(
						`<div class="text-muted">${__(
							"No stock of this item is available for this customer."
						)}</div>`
					);
					return;
				}
				const $table = $(`
					<table class="table table-bordered">
						<thead><tr>
							<th>${__("Batch")}</th><th>${__("Location")}</th>
							<th class="text-right">${__("Available")}</th><th></th>
						</tr></thead><tbody></tbody>
					</table>`);
				rows.forEach((row) => {
					const $tr = $(`<tr>
						<td>${row.batch_no}</td>
						<td>${row.storage_location || ""}</td>
						<td class="text-right">${format_number(row.qty)}</td>
						<td><button class="btn btn-xs btn-default">${__("Add")}</button></td>
					</tr>`);
					$tr.find("button").on("click", () => {
						const child = frm.add_child("items", {
							item_code: row.item_code,
							batch_no: row.batch_no,
							storage_location: row.storage_location,
							warehouse: row.warehouse,
							qty_available: row.qty,
							qty_to_pick: 0,
							picked_qty: 0,
							is_added: 1,
							material_request: (frm.doc.items || [])[0]
								? frm.doc.items[0].material_request
								: null,
						});
						frm.refresh_field("items");
						frappe.show_alert({
							message: __("Line added — enter the picked qty and a reason."),
							indicator: "green",
						});
						void child;
					});
					$table.find("tbody").append($tr);
				});
				$wrap.append($table);
			},
		});
	};

	dialog.show();
}


// ---------------------------------------------------------------- Phase 3b
// Cancel (full) and Return (partial). Both reverse exactly: the quantity goes
// back to the batch and location it was picked from. A reason is mandatory
// for both, and only reasons flagged "Applies to Cancel / Return" are offered.

function reason_field() {
	return {
		fieldname: "reason",
		fieldtype: "Link",
		options: "WMS Pick Reason",
		label: __("Reason"),
		reqd: 1,
		get_query: () => ({
			filters: { is_active: 1, applies_to_cancel_return: 1 },
		}),
	};
}

function cancel_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Cancel this pick list"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p>${__(
					"Everything still in WIP goes back to its original batch and location. This cannot be undone — redo it as a fresh pick."
				)}</p>`,
			},
			reason_field(),
			{ fieldname: "comment", fieldtype: "Small Text", label: __("Comment") },
		],
		primary_action_label: __("Cancel pick list"),
		primary_action(values) {
			d.hide();
			frm.call({
				doc: frm.doc,
				method: "cancel_pick",
				args: values,
				freeze: true,
				freeze_message: __("Returning stock from WIP..."),
			}).then(() => {
				frm.reload_doc();
				frappe.show_alert({
					message: __("Pick list cancelled and stock returned."),
					indicator: "green",
				});
			});
		},
	});
	d.show();
}

function return_dialog(frm) {
	const options = (frm.doc.items || [])
		.filter((row) => flt(row.picked_qty) - flt(row.returned_qty) > 0)
		.map((row) => ({
			label: `${row.idx}: ${row.item_code} — ${__("batch")} ${row.batch_no} @ ${
				row.storage_location
			} (${__("outstanding")} ${flt(row.picked_qty) - flt(row.returned_qty)})`,
			value: row.name,
		}));

	if (!options.length) {
		frappe.msgprint(__("Nothing left to return on this pick list."));
		return;
	}

	const d = new frappe.ui.Dialog({
		title: __("Return a quantity to stock"),
		fields: [
			{
				fieldname: "row_name",
				fieldtype: "Select",
				label: __("Line"),
				reqd: 1,
				options: options,
			},
			{ fieldname: "qty", fieldtype: "Float", label: __("Qty to return"), reqd: 1 },
			reason_field(),
			{ fieldname: "comment", fieldtype: "Small Text", label: __("Comment") },
		],
		primary_action_label: __("Return"),
		primary_action(values) {
			d.hide();
			frm.call({
				doc: frm.doc,
				method: "return_line",
				args: values,
				freeze: true,
				freeze_message: __("Returning stock from WIP..."),
			}).then(() => {
				frm.reload_doc();
				frappe.show_alert({
					message: __("Returned to the original batch and location."),
					indicator: "green",
				});
			});
		},
	});
	d.show();
}
