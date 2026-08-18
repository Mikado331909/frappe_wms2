// FOB concept invoices — explicit confirmation before either action.
//
// A concept invoice has two very different outcomes and one toolbar: submitting
// it completes the sale (stock leaves at cost, revenue and COGS post, the
// material is restocked at zero value), while cancelling or deleting it parks
// the material at cost, un-invoiced, with no way back to an automatic invoice.
// Clicking the wrong one is easy and expensive, so each says plainly which one
// it is before anything happens.
//
// Only FOB-generated invoices are affected: a normal Sales Invoice keeps
// ERPNext's standard behaviour.

frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		if (!is_fob_concept(frm)) return;

		frm.dashboard.add_comment(
			__(
				"Concept invoice created by the warehouse flow. Submitting it confirms the sale; cancelling or deleting it leaves the material at cost, un-invoiced."
			),
			"blue",
			true
		);
	},

	before_submit(frm) {
		if (!is_fob_concept(frm)) return;

		return confirm_action(frm, {
			title: __("Confirm this sale?"),
			indicator: "green",
			primary_label: __("Yes, CONFIRM the sale"),
			message: __(
				"You are about to <b>CONFIRM</b> this sale.<br><br>" +
					"The material leaves stock at cost, revenue and cost of goods sold are posted, " +
					"and the same quantity is immediately restocked at <b>zero value</b> in the same " +
					"warehouse and the same location.<br><br>" +
					"This cannot be undone from here: a confirmed sale is corrected with a credit " +
					"note and a manual stock correction."
			),
		});
	},

	before_cancel(frm) {
		if (!is_fob_concept(frm)) return;

		return confirm_action(frm, {
			title: __("Cancel this sale?"),
			indicator: "red",
			primary_label: __("Yes, CANCEL the sale"),
			message: __(
				"You are about to <b>CANCEL</b> this sale — the opposite of confirming it." +
					"<br><br>Nothing is invoiced. The material stays where it is, at cost, marked as " +
					"this customer's, for as long as it takes.<br><br>" +
					"A discarded concept is <b>not created again automatically</b>: if it still has " +
					"to be sold, the invoice has to be made by hand."
			),
		});
	},
});

function is_fob_concept(frm) {
	return Boolean(frm.doc.wms_fob_source_name);
}

function confirm_action(frm, opts) {
	// frappe awaits the promise a form trigger returns, and treats
	// `frappe.validated = false` as "stop". Resolving without validating is
	// how the action is aborted when the person says no.
	return new Promise((resolve) => {
		let confirmed = false;

		const dialog = new frappe.ui.Dialog({
			title: opts.title,
			indicator: opts.indicator,
			fields: [{ fieldtype: "HTML", options: `<p>${opts.message}</p>` }],
			primary_action_label: opts.primary_label,
			primary_action() {
				confirmed = true;
				dialog.hide();
			},
			secondary_action_label: __("Go back"),
			secondary_action() {
				dialog.hide();
			},
		});

		// One exit path for every way the dialog can close (primary action,
		// "Go back", Escape, the X): anything that is not an explicit yes
		// stops the action.
		dialog.onhide = () => {
			if (!confirmed) {
				frappe.validated = false;
			}
			resolve();
		};

		dialog.show();
	});
}
