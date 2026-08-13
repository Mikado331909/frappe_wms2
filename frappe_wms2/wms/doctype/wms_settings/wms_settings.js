// WMS Settings — keep the WIP pickers inside the configured company.
//
// The declarative `link_filters` in the doctype JSON already scope both
// fields. These get_query handlers are the belt-and-braces version: they also
// cover the case where no WIP warehouse has been chosen yet (then locations
// are limited to the company's warehouses instead of falling back to every
// location on the site).
//
// The company is never hardcoded — it comes from the doc, and from frappe's
// session defaults when the field is still empty.

frappe.ui.form.on("WMS Settings", {
	onload(frm) {
		if (!frm.doc.company) {
			frm.set_value("company", frappe.defaults.get_default("company"));
		}
	},

	refresh(frm) {
		frm.set_query("wip_warehouse", () => {
			return {
				filters: {
					company: frm.doc.company || frappe.defaults.get_default("company"),
					is_group: 0,
				},
			};
		});

		frm.set_query("wip_storage_location", () => {
			if (frm.doc.wip_warehouse) {
				return { filters: { warehouse: frm.doc.wip_warehouse } };
			}
			// No warehouse chosen yet: still never show another company's
			// locations.
			return {
				query: "frappe_wms2.wms.doctype.wms_settings.wms_settings.storage_location_query",
				filters: {
					company: frm.doc.company || frappe.defaults.get_default("company"),
				},
			};
		});
	},

	company(frm) {
		// Changing the company invalidates a pot from the previous one.
		if (frm.doc.wip_warehouse) {
			frappe.db
				.get_value("Warehouse", frm.doc.wip_warehouse, "company")
				.then((r) => {
					if (r.message && r.message.company !== frm.doc.company) {
						frm.set_value("wip_warehouse", null);
						frm.set_value("wip_storage_location", null);
						frappe.show_alert({
							message: __("WIP pot cleared: it belonged to another company."),
							indicator: "orange",
						});
					}
				});
		}
	},

	wip_warehouse(frm) {
		if (!frm.doc.wip_warehouse) {
			frm.set_value("wip_storage_location", null);
			return;
		}
		if (frm.doc.wip_storage_location) {
			frappe.db
				.get_value("Storage Location", frm.doc.wip_storage_location, "warehouse")
				.then((r) => {
					if (r.message && r.message.warehouse !== frm.doc.wip_warehouse) {
						frm.set_value("wip_storage_location", null);
					}
				});
		}
	},
});
