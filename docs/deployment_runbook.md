# Deployment runbook — installing frappe_wms2 for a customer

**The customer install NEVER runs the test suite.** The three commands below
are the whole procedure. `bench run-tests` is not part of it and must never be
pointed at a customer's site.

## New customer install

```bash
# 1. get the app onto the bench
bench get-app <repo-url>            # or: bench get-app /path/to/frappe_wms2

# 2. install it on the customer site (creates doctypes, custom fields,
#    fixtures; registers the Storage Location inventory dimension; seeds the
#    ownership types and pick reasons)
bench --site <customer-site> install-app frappe_wms2

# 3. apply schema/patches (also run after every app update)
bench --site <customer-site> migrate
```

That is all. Nothing in these three steps creates a Company, a warehouse, a
stock document or writes to WMS Settings' WIP fields.

## Post-install configuration (in the UI, by the key user)

1. **WMS Settings** → set *Company*, *WIP Warehouse* and *WIP Location (pot)*.
   The pickers only offer records of the selected company; a pot from another
   company is refused server-side.
2. **Storage Location** → create the physical locations (or import them).
3. **WMS Ownership Type** / **WMS Pick Reason** → review the seeded rows; both
   are self-managed masters and seeding never overwrites edits.

## Updating an existing customer

```bash
bench --site <customer-site> backup            # always first
git -C apps/frappe_wms2 pull                   # or: bench update --apps frappe_wms2
bench --site <customer-site> migrate
```

## Running the tests — development only

The suite is **regression coverage and stays part of the app**; it is simply
never executed against a customer site. It creates a test Company, warehouses,
items and submitted stock documents, so it refuses to run unless the site is
explicitly marked disposable:

```bash
# from the bench directory: creates a throwaway site, runs, drops it again
bash apps/frappe_wms2/scripts/run_tests_disposable.sh
```

Run this before shipping a change and as the smoke test for a new customer
rollout — on a development bench, on its own throwaway site.

If a site was polluted by an earlier run (test Company, its warehouses and
stock documents, a test WIP pot in WMS Settings):

```bash
bench --site <site> execute frappe_wms2.tests.site_safety.purge
```

## Quick check that a site is clean

```bash
bench --site <site> console
>>> frappe.get_all("Company", pluck="name")            # only the customer's own
>>> frappe.get_doc("WMS Settings").as_dict()           # company + WIP pot correct
```
