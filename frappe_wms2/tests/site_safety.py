# Test-site safety: a guard so the suite cannot run against a real site, and
# a purge that removes everything the suite creates.
#
# Background (a real incident): running the suite against the production site
# left behind a test Company with its warehouses, submitted Purchase Receipts
# with stock ledger entries, and — worst — a test WIP pot written into the
# global WMS Settings Single, which had to be spotted and corrected by hand.
# Nothing here is theoretical.

import frappe

TEST_COMPANY = "WMS2 Gate Company"
TEST_ABBR = "WGC"

# Every record the suite creates carries one of these markers.
NAME_MARKERS = ("WMS2 ", "WMS2-", "WMS3A ", "WMS3A-", "WMS2B-", "WIP-POT-")

OPT_IN_KEY = "wms2_disposable_test_site"


_CHECKED = {}


def assert_disposable_site():
    """Refuse to run on anything that is not an explicitly disposable site.

    Called by EVERY test-data factory (see setup_records.creates_test_data),
    so no entry point can skip it. The result is cached per site per process
    — this runs on every factory call.

    A site opts in with:
        bench --site <site> set-config wms2_disposable_test_site 1
    which `scripts/run_tests_disposable.sh` does automatically for the fresh
    throwaway site it creates. Production sites simply never carry the flag.
    """
    site = getattr(frappe.local, "site", None)
    if _CHECKED.get(site):
        return

    if frappe.conf.get(OPT_IN_KEY):
        _CHECKED[site] = True
        return

    other_companies = [
        c
        for c in frappe.get_all("Company", pluck="name")
        if c != TEST_COMPANY
    ]
    frappe.throw(
        "REFUSING TO RUN TESTS ON SITE '{site}'.\n\n"
        "This suite creates a test Company, warehouses, items and SUBMITTED "
        "stock documents. It must run on a disposable site.\n\n"
        "Use:  bash apps/frappe_wms2/scripts/run_tests_disposable.sh\n\n"
        "If this site really is disposable, mark it once:\n"
        "  bench --site {site} set-config {key} 1\n\n"
        "Companies found on this site: {companies}".format(
            site=frappe.local.site,
            key=OPT_IN_KEY,
            companies=", ".join(other_companies) or "(none)",
        ),
        title="Not a disposable test site",
    )


# ------------------------------------------------------------------ purge


def purge_test_data(delete_company=True, passes=3):  # noqa: C901
    """Remove everything the suite created. Safe to run repeatedly.

    Only touches records carrying a test marker or belonging to the test
    company — never anything else on the site.

    Runs several passes: documents are linked to each other (pick list ->
    stock entry -> batch -> item -> warehouse), and a link that blocks a
    delete in pass 1 is usually gone by pass 2.
    """
    # Deleting documents enqueues background work (global search, linked
    # file cleanup). Without a running worker that queue fills up and frappe
    # starts refusing with QueueOverloaded, which silently stalls the purge.
    # Running the jobs inline keeps a purge self-contained.
    previous_in_test = frappe.flags.in_test
    frappe.flags.in_test = True

    total = {}
    for _ in range(passes):
        removed = _purge_once(delete_company)
        for key, value in removed.items():
            total[key] = total.get(key, 0) + value
        if not removed:
            break

    frappe.flags.in_test = previous_in_test
    frappe.db.commit()
    return {k: v for k, v in total.items() if v}


def _purge_once(delete_company=True):
    removed = {}

    # 1. WMS documents. A submitted pick list refuses to cancel by design
    #    (that guard is for users, not for a purge), so drop the docstatus
    #    first and delete the row outright.
    for doctype in ("WMS Pick List", "WMS Pick Batch"):
        if not frappe.db.exists("DocType", doctype):
            continue
        names = frappe.get_all(doctype, pluck="name")
        if names:
            # Direct SQL: frappe.db.set_value refuses to touch docstatus.
            frappe.db.sql(
                f"update `tab{doctype}` set docstatus = 2 where docstatus = 1"
            )
            # Commit immediately: a later failed delete rolls back the open
            # transaction, and this must not be undone with it.
            frappe.db.commit()
        removed[doctype] = _delete_all(names, doctype)

    # 2. Stock documents of the test company, newest first (cancel + delete).
    for doctype in (
        "Stock Entry",
        "Purchase Receipt",
        "Purchase Invoice",
        "Material Request",
        "Sales Order",
        "Stock Reconciliation",
    ):
        if not frappe.db.exists("DocType", doctype):
            continue
        names = frappe.get_all(
            doctype,
            filters={"company": TEST_COMPANY},
            pluck="name",
            order_by="creation desc",
        )
        removed[doctype] = _delete_all(names, doctype, cancel_first=True)

    # 3. Serial and Batch Bundles left behind by those documents.
    if frappe.db.exists("DocType", "Serial and Batch Bundle"):
        names = frappe.get_all(
            "Serial and Batch Bundle",
            filters={"company": TEST_COMPANY},
            pluck="name",
        )
        removed["Serial and Batch Bundle"] = _delete_all(
            names, "Serial and Batch Bundle", cancel_first=True
        )

    # 3b. Storage Locations created with a THROWAWAY CODE. These carry no
    #     name marker (the code space is the marker: gang X/Y/Z with a
    #     3-digit niveau, deliberately outside any real shelf coding), so the
    #     marker sweep below would miss them.
    if frappe.db.exists("DocType", "Storage Location"):
        throwaway = frappe.get_all(
            "Storage Location",
            filters={"gang": ("in", ["X", "Y", "Z"]), "niveau": (">=", 700)},
            pluck="name",
        )
        removed["Storage Location (throwaway codes)"] = _delete_all(
            throwaway, "Storage Location"
        )

    # 4. Masters created by the suite, identified by marker.
    for doctype, field in (
        ("Batch", "name"),
        ("Item", "name"),
        ("Storage Location", "name"),
        ("Warehouse", "name"),
        ("Customer", "name"),
        ("Supplier", "name"),
        ("Item Group", "name"),
        ("Customer Group", "name"),
        ("Price List", "name"),
    ):
        if not frappe.db.exists("DocType", doctype):
            continue
        names = [
            n
            for n in frappe.get_all(doctype, pluck=field)
            if _is_test_name(n)
        ]
        removed[doctype] = _delete_all(names, doctype)

    # 5. Leftover ledger rows of the test company, then its warehouses
    #    (incl. the ones ERPNext auto-creates: Stores / Work In Progress /
    #    Finished Goods / Goods In Transit / All Warehouses) and the Company.
    if delete_company:
        test_warehouses = frappe.get_all(
            "Warehouse", filters={"company": TEST_COMPANY}, pluck="name",
            order_by="lft desc",
        )
        if test_warehouses:
            for doctype in ("Stock Ledger Entry", "Bin",
                            "Serial and Batch Bundle", "GL Entry"):
                if not frappe.db.exists("DocType", doctype):
                    continue
                field = "warehouse" if doctype != "GL Entry" else "company"
                value = (
                    ("in", test_warehouses) if field == "warehouse" else TEST_COMPANY
                )
                try:
                    count = frappe.db.count(doctype, {field: value})
                    if count:
                        frappe.db.delete(doctype, {field: value})
                        removed[doctype] = removed.get(doctype, 0) + count
                except Exception:
                    frappe.db.rollback()

            removed["Company warehouses"] = _delete_all(
                test_warehouses, "Warehouse"
            )

        if frappe.db.exists("Company", TEST_COMPANY):
            removed["Company"] = _delete_company()

    frappe.db.commit()
    return {k: v for k, v in removed.items() if v}


def _is_test_name(name):
    return any(marker in (name or "") for marker in NAME_MARKERS)


def _delete_all(names, doctype, cancel_first=False):
    """Delete one by one, committing each success.

    Per-record commits matter: some deletes legitimately fail on links, and
    the rollback that cleans up after a failure must not throw away the
    records already removed in this pass.
    """
    count = 0
    for name in names:
        try:
            if cancel_first:
                doc = frappe.get_doc(doctype, name)
                if doc.docstatus == 1:
                    doc.flags.ignore_links = True
                    doc.cancel()
            frappe.delete_doc(
                doctype,
                name,
                force=True,
                ignore_permissions=True,
                ignore_missing=True,
                delete_permanently=True,
            )
            frappe.db.commit()
            count += 1
        except Exception:
            frappe.db.rollback()
            continue
    return count


def _delete_company():
    try:
        from erpnext.setup.doctype.company.delete_company_transactions import (
            delete_company_transactions,
        )

        delete_company_transactions(TEST_COMPANY)
    except Exception:
        frappe.db.rollback()
    try:
        frappe.delete_doc(
            "Company",
            TEST_COMPANY,
            force=True,
            ignore_permissions=True,
            ignore_missing=True,
        )
        return 1
    except Exception:
        frappe.db.rollback()
        return 0


@frappe.whitelist()
def purge():
    """bench --site <site> execute frappe_wms2.tests.site_safety.purge"""
    frappe.only_for("System Manager")
    result = purge_test_data()
    print("Purged:", result or "nothing found")
    return result
