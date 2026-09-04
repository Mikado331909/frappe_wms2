// Cancelling a Delivery Note leaves a linked, still-open concept invoice —
// and its reservation — exactly where it was. That is the agreed behaviour,
// not a bug, and this does not change it. It only says so at the moment it
// matters, instead of leaving it as documentation someone has to remember.
//
// Purely informational: the cancel proceeds afterwards either way. Same dialog
// style as the concept-invoice confirm/reject actions.

frappe.ui.form.on("Delivery Note", {
	before_cancel(frm) {
		return new Promise((resolve) => {
			frappe.call({
				method: "frappe_wms2.wms.production.get_open_concept_invoices",
				args: { delivery_note: frm.doc.name },
				callback(r) {
					const invoices = r.message || [];
					// No concept invoice, or it is already confirmed: nothing
					// to say. A confirmed invoice is untouched by this cancel
					// either way.
					if (!invoices.length) {
						resolve();
						return;
					}
					show_warning(frm, invoices, resolve);
				},
				error() {
					// Never let a warning lookup block an ordinary cancel.
					resolve();
				},
			});
		});
	},
});

function show_warning(frm, invoices, resolve) {
	const dialog = new frappe.ui.Dialog({
		title: __("Open concept invoice for this delivery"),
		indicator: "orange",
		fields: [{ fieldtype: "HTML", fieldname: "body" }],
		primary_action_label: __("Cancel the delivery anyway"),
		primary_action() {
			dialog.hide();
		},
		secondary_action_label: __("Go back"),
		secondary_action() {
			dialog.hide();
		},
	});

	// Whichever way it closes, the cancel proceeds — this is a warning, not a
	// second confirmation step. "Go back" simply lets the person read it and
	// leave; frappe's own cancel confirmation still stands behind it.
	dialog.onhide = () => resolve();

	dialog.fields_dict.body.$wrapper.html(render(invoices));
	dialog.show();
}

function render(invoices) {
	const rows = invoices
		.map((inv) => {
			const items = (inv.items || [])
				.map(
					(i) =>
						`${frappe.utils.escape_html(i.item_code)} — ${format_number(
							i.qty
						)} × ${format_currency(i.rate, inv.currency)}`
				)
				.join("<br>");

			return `
				<tr>
					<td><a href="/app/sales-invoice/${encodeURIComponent(
						inv.name
					)}" target="_blank">${frappe.utils.escape_html(inv.name)}</a></td>
					<td>${frappe.utils.escape_html(inv.customer || "")}</td>
					<td>${items}</td>
					<td class="text-right">${format_currency(inv.grand_total, inv.currency)}</td>
					<td class="text-right">${format_number(inv.reserved || 0)}</td>
				</tr>`;
		})
		.join("");

	return `
		<p>${__(
			"This Delivery Note has a concept invoice that has <b>not been confirmed</b>:"
		)}</p>
		<table class="table table-bordered" style="font-size:12px">
			<thead><tr>
				<th>${__("Concept invoice")}</th>
				<th>${__("Customer")}</th>
				<th>${__("Lines")}</th>
				<th class="text-right">${__("Amount")}</th>
				<th class="text-right">${__("Reserved qty")}</th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table>

		<p style="margin-top:10px">${__(
			"Cancelling this Delivery Note will <b>not</b> discard that concept invoice, and will <b>not</b> release its reserved quantity."
		)}</p>
		<ul>
			<li>${__(
				"If you do not intend to bill for what was shipped, open the concept invoice above and discard it yourself — that releases the reservation."
			)}</li>
			<li>${__(
				"If you do still intend to bill for it despite cancelling the delivery, nothing further is needed. That is a perfectly valid choice; the invoice simply stays as it is."
			)}</li>
		</ul>`;
}
