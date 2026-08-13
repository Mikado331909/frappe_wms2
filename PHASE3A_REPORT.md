# Phase 3a Report — Core Picking Flow (frappe_wms2 0.3.0)

**LIVE RESULT.** Full suite executed on a live bench pinned to your exact
build — **erpnext 16.26.2**, frappe 16.31, **Python 3.14.6**, MariaDB 10.11 —
on two sites (an established one and a sterile fresh one: new-site → install
erpnext → install frappe_wms2 → migrate → run-tests):

```
bench --site <site> run-tests --app frappe_wms2
Ran 26 tests ... OK
```

That covers 10 Phase 3a tests, the 6 Phase 2a ownership tests (types 1/2) and
the 5 Phase 0 gates + 5 Storage Location unit tests — so the earlier phases
are regression-proven in the same run.

## Collision safety (required, and it found a real bug)

The suite is built for a site that already holds 622 real locations and live
stock:

- Every location code a test creates uses **gang X/Y/Z with niveau ≥ 700**
  plus a random draw — outside the real code space entirely.
- Items, batches, customers, item groups, warehouses, MRs and the WIP pot all
  carry a random per-run token; every balance assertion is scoped to a
  freshly created throwaway item, so pre-existing stock can never move a
  result.
- `tearDownClass` deletes the drafts and masters it created; submitted stock
  documents are deliberately left (they are ledger history, and the items
  they touch are throwaway).

Applying this to the **older** phases exposed a genuine hazard: the Phase 0
and Phase 2a tests used hardcoded codes `FA1-1`, `FA1-2`, `TA9-1`, `TA9-2`.
On your populated site those are **real shelves** — the old suite would have
posted test stock into them. They now use throwaway codes too. This was a
latent bug in the delivered Phase 0/2a tests, not a Phase 3a issue.

## What was built

**Bundling — `WMS Pick Batch`.** Customer is derived from each MR's Sales
Order; bundling MRs of two customers **throws** (T1), it is not a warning.
Demand is summed per item across the bundled MRs (T2). The demand table shows
demand / on-pick-lists / open per item.

**Reservation.** "On pick lists" is not a stored counter — it is computed
from the actual pick list lines (drafts included, cancelled excluded), so it
cannot drift. Generation only ever allocates the **open** remainder; when
everything is listed, a second generation for the same group finds nothing
and says so (T3).

**Item-group selection.** `get_item_groups` returns the groups present with
their open qty; a dialog lets the user tick which to pick now and generates
**one pick list per ticked group** — fabrics first, trimmings later, with the
untouched group still fully open (T4).

**FIFO.** Oldest batch first, walking batches until the order demand is met;
a partly consumed batch keeps its rest (T3 asserts 3 + 4 + 2 across three
batches). "Qty available on batch" is the live balance of that
(item, batch, location) read from the ledger via the bundle.

**Print format** (`WMS Pick List`, shipped as a fixture): customer at the
top, the exact columns Order | Item | Qty needed | Qty available on batch |
Qty to pick from batch | Location | Batch | Picked qty | Batch empty?, one
**sum row per item**, blank boxes for the floor, and **no value/price
anywhere** — all asserted by rendering the format in T10.

**Processing and submit.** Saving a draft never touches stock (T5 asserts the
balance is unchanged after save). On submit: a Material Transfer books the
picked quantities out of their batch+location into the WIP pot, and a
separate Material Issue writes flagged-empty (batch, location) balances to 0
with the reason in the remarks (T6). Surplus posts once a surplus-valid
reason is given (T7); a missing reason blocks the save/submit; picking more
than the location holds is refused with a clear message before the dimension
rule would fire. Processor-added lines are allowed only from **same-customer**
batches — the other customer's batch is refused and never even offered by the
allocator (T8). Reasons come from one shared, self-managed master with
shortage/surplus flags; a reason a user creates in the UI works immediately
(T9).

**WIP provenance.** WIP is a simple pot: one warehouse, one sentinel
location, no tracking inside it. Provenance is preserved and readable via
`get_wip_provenance()`: pick list, customer, Material Request, Sales Order,
item, batch, batch ownership type, batch customer, qty and the Stock Entry
(T5 asserts all of it).

## Follow-up fix: T5 on a populated site (harness, not behaviour)

On the live site `test_t5_batch_cannot_mix_owners` failed because a
customer-supplied line entered at rate 0 came back as rate 1.0, and the
zero-valuation rule then correctly refused it. Root cause, traced in the
v16.26.2 source and reproduced locally:

1. `get_item_details.insert_item_price()` auto-creates an **Item Price** from
   the first receipt of an item when Stock Settings has
   *Auto Insert Price List Rate If Missing* on (the default) and a default
   buying price list exists — which is the case on a populated site but was
   not on the bare test site, hence "passes here, fails there".
2. On the next receipt of that item, `accounts_controller
   .set_missing_item_details()` **force-refreshes the rate** whenever
   `use_serial_batch_fields` is set and a `batch_no` is present:
   `if fieldname == "batch_no" ... if ret.get("rate"): item.set("rate", ...)`.
   So the stored price silently overwrote the 0 the test asked for.

The zero-valuation enforcement was doing exactly its job and is **unchanged**.
The test factory now enters a zero-valuation receipt the way a real one is
entered: a dedicated empty buying price list, `ignore_pricing_rule = 1`,
`auto_insert_price_list_rate_if_missing` off, and `rate`/`price_list_rate`/
discount/margin/last-purchase pinned to 0 on any line meant to carry no
value. It also asserts, right after insert, that no rate was injected — so a
future regression of this kind is reported as a harness fault instead of
being blamed on the app rule.

Verified by reproducing the production condition on the test site (default
buying price list + an Item Price of 1.0 for the item): the **old** factory
reproduces your exact failure, the **new** one keeps the line at rate 0 and
the SLE at value 0. The full suite then ran on a **populated** site (772
storage locations, 622 of them with real-looking codes, auto-price-insert
enabled): **26/26 OK**, and a check confirms zero real-code locations were
touched by test stock.

## Caveats — flagged honestly

1. **The WIP pot needs a sentinel location.** The Phase 0 dimension is
   mandatory on stock lines, so material cannot enter *any* warehouse without
   a Storage Location — including WIP. I added an `is_special` flag to
   Storage Location: such a location skips the `FA1-2` code format and
   carries no parsed components. It is a mechanical necessity, not a model
   change; the alternative (relaxing the mandatory dimension) would be worse.
   Set the pot in **WMS Settings** before the first submit.
2. **Own-use stock is pickable for a customer order by default.** Own-use
   batches carry no customer, so using them cannot *mix* customers. This is a
   judgement call, exposed as a switch in WMS Settings
   (`allow_customer_neutral_stock`) — untick it for strictly customer-owned
   picking. Another customer's batch is **never** allowed, switch or not.
3. **Reason rule, precise semantics.** A reason is required when picked ≠
   to-pick, or when a batch is flagged empty **while stock is still
   administered** (i.e. a correction is being booked). If a pick empties the
   balance exactly, the flag is set automatically and no reason is demanded —
   nothing is being corrected. Drift on a non-empty batch is deliberately not
   corrected (cycle count, later phase).
4. **Cancelling a submitted pick list is refused** with an explicit message.
   Cancellation/return is a later phase; a half-implemented reversal would be
   worse than a clear "not yet".
5. **Non-batched items are out of scope** for picking: ownership and customer
   separation ride on the batch, so an item without batches cannot be
   allocated. Worth confirming that all pickable materials are batched.
6. **Bundle-level warehouse scope**: allocation scans all warehouses except
   the WIP pot. If Crings ever holds stock in a warehouse that must not be
   picked from, that needs an exclusion — say so and it is a one-line filter.
7. **The test bench was built with `--skip-assets`** (frappe 16 now wants
   Node ≥ 24 for the asset build). This affects only frappe's print *wrapper*
   CSS, not the format itself — T10 renders the format body and asserts its
   content. On your real site with built assets the print view works
   normally.

Nothing beyond Phase 3a was built: no cancellation/return, no BOM
calculation, no cycle count, no invoicing.

---

# Follow-up: test isolation (test suite must never touch a real site)

Running the suite against the live site left real artifacts behind — a test
Company with warehouses, submitted Purchase Receipts with stock ledger
entries, and a test WIP pot written into the **global WMS Settings Single**,
which had to be spotted and corrected by hand. That last one is the dangerous
part: a Single is site-wide and not scoped per company. Fixed at the cause:

**1. The suite refuses to run on a non-disposable site.**
`tests/site_safety.assert_disposable_site()` runs before any record is
created and throws unless the site carries `wms2_disposable_test_site` in
site_config. The error names the site, the companies it found and how to
proceed. Production sites simply never carry the flag.

**2. A disposable-site runner.** `scripts/run_tests_disposable.sh` creates a
fresh site, installs erpnext + frappe_wms2, sets the disposable flag, runs
the tests and **drops the site again** (pass or fail; `--keep` to inspect).
This is now the documented way to run the suite.

**3. Global Singles are never written.** WMS Settings is injected in-memory
via `frappe.flags.wms2_settings_override`, honoured by `picking.get_settings()`
— the values exist for the test process only and nothing is persisted. The
Stock Settings flags the intake tests genuinely need are snapshotted before
and restored after (`setup_records.restore_global_singles()`).

**4. A purge for sites that already got polluted.**
`bench --site <site> execute frappe_wms2.tests.site_safety.purge` removes
everything the suite creates — WMS documents (including submitted pick lists,
whose user-facing cancel guard it bypasses deliberately), stock documents of
the test company, their ledger rows, marked masters, the auto-created company
warehouses and finally the Company itself. It only touches records carrying a
test marker or belonging to `WMS2 Gate Company`, runs several passes because
of inter-document links, commits per record so one failure cannot undo the
rest, and runs enqueued jobs inline (without this, frappe starts refusing
with QueueOverloaded and the purge stalls silently).

## Verified

Reproduced the situation on a site holding a real `Crings B.V.` with a
configured WIP pot (`WIP - CB` / `WIP-CRINGS`) plus the leftover clutter:

- the guard **refused** the run on that site, naming Crings B.V.;
- the purge removed the lot — 22 pick lists, 73 pick batches, 135 purchase
  receipts, 186 stock ledger entries, the WGC warehouses and the Company;
- `run_tests_disposable.sh` then ran the **full suite: 26 tests, OK**, on a
  throwaway site that was dropped afterwards.

Post-run state of the production-like site:

```
Companies                 : ['Crings B.V.']
WMS Settings WIP warehouse: WIP - CB
WMS Settings WIP location : WIP-CRINGS
Warehouses not under Crings: none
WMS Pick Lists / Batches  : 0 / 0
Stock Ledger Entries      : 0
Test-marked locations     : none
```

Caveat, stated plainly: the purge deletes ledger rows of the test company
directly where cancellation is blocked by links. That is acceptable for
throwaway data and is scoped to `WMS2 Gate Company`, but it is the reason
the disposable-site runner — not the purge — is the recommended path.

---

# Follow-up: company scoping of the WMS Settings WIP fields

`wip_warehouse` and `wip_storage_location` carried no filters, so their
dropdowns listed Warehouses and Storage Locations of **every** company on the
site. A field-config gap, not a data bug — but one that lets someone wire the
WIP pot to another company's warehouse. Fixed on three levels, none of them
name-based:

1. **A `Company` field on WMS Settings**, defaulted dynamically:
   `resolve_default_company()` takes the site's global default company, or —
   on a single-company site — the only company that exists. No name or
   abbreviation appears anywhere in the code, so the app deploys unchanged
   for any customer.
2. **Declarative `link_filters`** on both fields:
   `wip_warehouse` → `[["Warehouse","company","=","eval:doc.company"], ["Warehouse","is_group","=",0]]`
   `wip_storage_location` → `[["Storage Location","warehouse","=","eval:doc.wip_warehouse"]]`.
   The location filter goes through the warehouse deliberately: Storage
   Location has no company field of its own (and the underlying doctypes were
   not changed), and the pot belongs inside the WIP warehouse anyway — so
   filtering on the warehouse is both stricter and inherently
   company-correct. A client script adds `get_query` fallbacks, including a
   company-scoped Storage Location search
   (`wms_settings.storage_location_query`, joining Warehouse for the company)
   for when no WIP warehouse has been picked yet.
3. **Server-side enforcement**, because a filtered dropdown is only UX:
   `WMSSettings.validate_company_scope()` refuses a warehouse of another
   company and a location outside the WIP warehouse, and
   `picking.get_wip_target(company)` refuses to post a pick list into a pot
   belonging to a different company.

A `v0_4` patch backfills the company on sites installed before the field
existed.

## Verified

On a site carrying two deliberately differently-named companies
(`Crings B.V.` / `CB` and `Zephyr Textiles N.V.` / `ZTX`), each with its own
warehouses and WIP location:

```
[Crings B.V.]            WIP Warehouse: WIP - CB, ...stock - CB, ... (CB only)
                         WIP Location : WIP-CRINGS
[Zephyr Textiles N.V.]   WIP Warehouse: Zephyr WIP - ZTX, ...            (ZTX only)
                         WIP Location : WIP-ZEPHYR
```

Neither company's records appear in the other's dropdown — proving the scope
follows the configured company, not a name match. Saving a Crings settings
document with a Zephyr warehouse is refused server-side, and `get_wip_target`
refuses the cross-company pot at posting time. New test
`test_t11_wip_pot_is_company_scoped` covers all of this; full suite:
**27 tests, OK** on a disposable site.

# Follow-up: test isolation — process confirmation

Confirmed in writing, and documented in `docs/deployment_runbook.md`:

**The customer install runbook is exactly `bench get-app` → `bench --site
<customer-site> install-app frappe_wms2` → `bench --site <customer-site>
migrate`. It never calls `bench run-tests`.** None of those three steps
creates a Company, warehouse or stock document, or writes the WIP fields of
WMS Settings.

**The test suite stays in the app** as regression coverage for future changes
and new customer rollouts. It is only ever run on a throwaway development
site via `scripts/run_tests_disposable.sh`, which creates a fresh site, runs,
and drops it again — and the suite refuses to start on any site not flagged
`wms2_disposable_test_site`.

State of the production-like site after the latest full test run:

```
Companies                 : ['Crings B.V.']
WMS Settings company      : 'Crings B.V.'
WMS Settings WIP warehouse: WIP - CB
WMS Settings WIP location : WIP-CRINGS
Warehouses not under Crings: none
WMS Pick Lists / Batches  : 0 / 0   |   Stock Ledger Entries: 0
Test-marked locations     : none
```

---

# Follow-up: the disposable-site guard is now enforced by construction

The guard was called from `setup_gate_records()` only, so
`test_storage_location.py` — which calls `get_or_create_company()` and
`get_or_create_warehouse()` directly — walked straight past it and would
recreate the test Company on a real site. Exactly the failure mode the guard
was meant to close, at an entry point it didn't cover.

**Fix — one choke point, not a rule to remember.** `setup_records.py` now
defines a `@creates_test_data` decorator that calls `assert_disposable_site()`,
and **every** factory that writes to the database carries it: `setup_gate_records`,
`ensure_erpnext_masters`, `get_or_create_company`, `get_or_create_warehouse`,
`get_or_create_location`, `get_or_create_supplier`, `get_or_create_customer`,
`_leaf_customer_group`, `make_item`, `make_batch`, `make_stock_entry`,
`zero_valuation_buying_price_list`, `make_purchase_receipt` (13 in total).
A future test file cannot create the test Company or its warehouse without
passing the check, because there is no unwrapped factory to reach it through.
The check result is cached per site per process, so calling it on every
factory costs nothing.

The purge also learned about **throwaway-coded Storage Locations** (gang
X/Y/Z, niveau >= 700). Those carry no name marker — the code space *is* the
marker — so earlier purges left them behind; 138 residual ones from pre-guard
runs have now been removed.

## Verified

Each module run on its own against the non-disposable site:

```
storage_location.test_storage_location -> guard fired: YES   <- previously bypassed
tests.test_gate_phase0                 -> guard fired: YES
tests.test_ownership_phase2a           -> guard fired: YES
tests.test_picking_phase3a             -> guard fired: YES
```

The refused run created nothing: no `WMS2 Gate Company`, no warehouse (checked
immediately afterwards). Full disposable-site run still green: **27 tests, OK**,
site dropped afterwards. Production-like site after all of it:

```
Companies                 : ['Crings B.V.']
WMS Settings WIP warehouse: WIP - CB      WIP location: WIP-CRINGS
Warehouses not under Crings: none
Pick Lists / Batches: 0 / 0   Stock Ledger Entries: 0   Test-coded locations: 0
```
