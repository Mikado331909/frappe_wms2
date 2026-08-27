# Type 4 Part 0 — mandatory consumption check: **DOUBLE COUNTING CONFIRMED**

Reporting here as instructed before touching Parts A/B/C.

## What I ran

One ordinary Work Order, end to end, with a hand-counted quantity — no split
bookings, no shortcuts: 10 finished units from a BOM of **2 raw units each**,
so **20 units** of Type 4 raw material genuinely used. Both stages posted:
`Material Transfer for Manufacture` (source → WIP), then `Manufacture`
(WIP → finished good).

## The evidence, from the stock ledger

```
HAND-COUNTED raw material genuinely used: 20  (2/unit x 10 units)

NEGATIVE stock ledger entries for the raw material:
  MAT-STE-2026-00001  PROBE TRIM 33C32 - WGC   -20.0   <-- transfer leg
  MAT-STE-2026-00001  PROBE WIP  33C32 - WGC   +20.0
  MAT-STE-2026-00002  PROBE WIP  33C32 - WGC   -20.0   <-- manufacture leg

SUM of negative legs (what the code added up):  40.0
get_work_order_consumption() reported:          40.0

VERDICT: DOUBLE COUNTED (reported 40 vs actual 20)
```

Your reading was exactly right: the two purposes are two stages of the *same*
physical units, and both legs are negative — one out of the source warehouse,
one out of WIP. Every ordinary Type 4 invoice using a transfer step would have
billed **twice** the material actually used. Not an edge case; blocking, as you
said.

## The fix

`production.CONSUMPTION_PURPOSES` now counts only the stage where material is
genuinely embodied in the finished good:

```python
CONSUMPTION_PURPOSES = ("Manufacture", "Material Consumption for Manufacture")
```

`Material Transfer for Manufacture` is excluded — it only relocates. Re-running
the identical probe against the fixed code:

```
get_work_order_consumption() reported: 20.0
VERDICT: CORRECT (reported 20 vs actual 20)
```

Work Orders posted with `skip_transfer` (no transfer leg at all, which is how
the existing Part 1 gate tests run) are unaffected: they only ever had the
Manufacture leg, which is why T4/T5 passed and never surfaced this. That is
also the honest reason the bug survived Part 1 review — the tests exercised the
simpler of the two production flows.

## The Phase 3a pick-list interaction you asked about

**No overlap.** The pick list's WIP movement is a plain `Material Transfer`
with **no `work_order` set** (`wms_pick_list._new_stock_entry`), and
`get_work_order_consumption()` filters on both `work_order` and purpose — so a
pick can never be swept into a Work Order's consumption. Worth noting the
practical consequence: material picked into the WIP pot is *already* out of the
bulk before any Work Order entry exists, and only the `Manufacture` booking now
counts it, which is exactly once.

## Full suite

**58 tests, OK** — including the Part 1 invoicing gates (T4/T5) re-run against
the corrected consumption figure, and everything from Document 1 below.

## Caveat

`Material Consumption for Manufacture` is included as a genuine consumption
purpose, but a flow that mixes it *with* a later `Manufacture` booking was not
exercised — ERPNext reduces the Manufacture entry's raw materials by what was
already consumed, so it should not double count, but I have not proven that
empirically. If Crings uses that purpose, say so and I will probe it the same
way before it matters.

**Stopping here for your review, as instructed. Parts A/B/C are not started.**
