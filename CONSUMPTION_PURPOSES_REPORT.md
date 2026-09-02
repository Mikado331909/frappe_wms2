# `CONSUMPTION_PURPOSES` reduced to `("Manufacture",)` — confirmed
**frappe_wms2 0.11.1**

**LIVE RESULT.** Full suite on a fresh disposable site (erpnext 16.26.2,
Python 3.14.6): **80 tests, OK** — one new pinning test plus all 79 existing
ones.

## The change

```python
CONSUMPTION_PURPOSES = ("Manufacture",)
```

You were right that this was missed: the earlier Part 0 fix removed
`"Material Transfer for Manufacture"` (the double-counting culprit) but left
`"Material Consumption for Manufacture"` in speculatively. It is now gone, and
the comment in `production.py` records **why each excluded purpose is
excluded**, so the reasoning survives without needing the reports:

- `Material Transfer for Manufacture` — the same physical units at an earlier
  stage; counting both legs gave 40 against a hand-counted 20.
- `Material Consumption for Manufacture` — Job Card / operations-based
  manufacturing, not in use here. The real cutting → sewing handoff is physical
  and produces no stock document; if it is ever booked it will be a plain
  `Material Transfer`, which is not a consumption purpose either way. If Job
  Cards are introduced later, this purpose gets added back deliberately and
  re-tested then, rather than sitting there untested today.
- The WMS pick list's own bulk → WIP movement is a plain `Material Transfer`
  carrying `work_order` for attribution only — never counted, per the design
  you approved in point 1.

## New regression test

`test_p0_only_the_manufacture_leg_counts_as_consumption` pins all of it on a
live site, so the value cannot drift back unnoticed:

- asserts `CONSUMPTION_PURPOSES == ("Manufacture",)` exactly;
- picks 12 units of Type 4 material to WIP through the real pick flow, then
  asserts consumption is **0** — the pick is attributed, not counted;
- books the Manufacture and asserts consumption is exactly **12** — the
  hand-counted quantity, counted once;
- posts an additional explicit `Material Transfer for Manufacture` of 4 more
  units into WIP and asserts consumption is **still 12** — a transfer into WIP
  can never inflate the invoicing basis.

That last step is the hand-counted Part 0 check, now living inside the suite
rather than in a throwaway script.

## Verification note

I re-ran the standalone Part 0 probe script as well, but it needs the WIP-pot
configuration that only the test fixtures set up, and rebuilding that outside
the suite was not worth the churn — the test above exercises the identical
path (transfer leg + manufacture leg, hand-counted quantities) on a disposable
site, and passes. Saying so plainly rather than implying a second independent
run.

## Unchanged

Point 1 stands exactly as approved: the pick's Stock Entry still carries
`work_order` for traceability and for splitting entries per Work Order;
consumption is counted once, at the Manufacture booking. Nothing else in the
Work Order picking work changed.

## Caveat

If Job Cards are ever adopted, raw material consumed through
`Material Consumption for Manufacture` will be **invisible** to Type 4
invoicing until that purpose is added back — deliberately, per your decision.
The pinning test will fail the moment someone changes the tuple, which is the
intended prompt to re-verify rather than a nuisance.
