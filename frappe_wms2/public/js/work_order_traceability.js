// Work Order traceability: what went in, what came out.
//
// Shown on the Work Order form itself — the natural place to look. Two short
// lists: the raw-material batches consumed (with the ownership stamp that
// decides who gets invoiced) and the finished-good batches produced, one per
// Manufacture booking.

frappe.ui.form.on("Work Order", {
	refresh(frm) {
		if (frm.doc.docstatus === 0) return;
		frm.add_custom_button(__("Batch traceability"), () => show_traceability(frm));
	},
});

function show_traceability(frm) {
	frappe.call({
		method: "frappe_wms2.wms.production.get_work_order_traceability",
		args: { work_order: frm.doc.name },
		freeze: true,
		callback(r) {
			const data = r.message || {};
			const dialog = new frappe.ui.Dialog({
				title: __("Batch traceability — {0}", [frm.doc.name]),
				size: "large",
				fields: [{ fieldtype: "HTML", fieldname: "body" }],
				primary_action_label: __("Close"),
				primary_action() {
					dialog.hide();
				},
			});
			dialog.fields_dict.body.$wrapper.html(render(data));
			dialog.show();
		},
	});
}

function render(data) {
	const raw = (data.raw_materials || [])
		.map(
			(r) => `<tr>
				<td>${frappe.utils.escape_html(r.batch_no || "")}</td>
				<td>${frappe.utils.escape_html(r.item_code || "")}</td>
				<td class="text-right">${format_number(r.qty)}</td>
				<td>${frappe.utils.escape_html(r.ownership_type || "")}</td>
				<td>${frappe.utils.escape_html(r.customer || __("—"))}</td>
			</tr>`
		)
		.join("");

	const finished = (data.finished_goods || [])
		.map(
			(r) => `<tr>
				<td>${frappe.utils.escape_html(r.batch_no || "")}</td>
				<td class="text-right">${format_number(r.qty)}</td>
				<td>${r.booking_date ? frappe.datetime.str_to_user(r.booking_date) : ""}</td>
				<td>${frappe.utils.escape_html(r.stock_entry || "")}</td>
			</tr>`
		)
		.join("");

	const empty = `<tr><td colspan="5" class="text-muted">${__(
		"Nothing booked yet."
	)}</td></tr>`;

	return `
		<h5>${__("Raw material batches used")}</h5>
		<table class="table table-bordered" style="font-size:12px">
			<thead><tr>
				<th>${__("Batch")}</th><th>${__("Item")}</th>
				<th class="text-right">${__("Qty consumed")}</th>
				<th>${__("Ownership Type")}</th><th>${__("Customer")}</th>
			</tr></thead>
			<tbody>${raw || empty}</tbody>
		</table>

		<h5 style="margin-top:14px">${__("Finished-good batches produced")}</h5>
		<table class="table table-bordered" style="font-size:12px">
			<thead><tr>
				<th>${__("Batch")}</th><th class="text-right">${__("Qty")}</th>
				<th>${__("Booking date")}</th><th>${__("Manufacture entry")}</th>
			</tr></thead>
			<tbody>${finished || empty}</tbody>
		</table>`;
}
