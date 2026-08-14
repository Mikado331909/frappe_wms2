# Phase 3b Report — Pick List Cancellation and Partial Return (frappe_wms2 0.4.0)

**LIVE RESULT.** Full suite on a fresh disposable site (erpnext 16.26.2,
Python 3.14.6): **33 tests, OK** — 6 new Phase 3b tests plus the 27 existing
ones, so Phases 0/2a/3a are regression-proven in the same run. The site is
created, used and dropped by `scripts/run_tests_disposable.sh`; nothing runs
against a customer site.

## What was built

**Two whitelisted methods on `WMS Pick List`, each with a toolbar button:**

- `cancel_pick(reason, comment)` — full, exact reversal. Every line's
  outstanding picked quantity moves from the WIP pot back into **its own**
  batch and storage location, taken from the line itself (never chosen). The
  document is then marked cancelled.
- `return_line(row_name, qty, reason, comment)` — the same exact reversal for
  a partial quantity of one line, repeatable until the line is fully returned.

Both post a **new** Material Transfer (WIP → origin), tagged `[WMS-REVERSAL]`
in the remarks, rather than cancelling the original Stock Entry: partial
return has no cancel equivalent, the original entry may already be partly
returned, and both movements stay visible in the ledger. Ownership type and
customer need no handling — it is the same batch, so the stamp travels along.

**The plain Cancel button still refuses**, now in `before_cancel` rather than
`on_cancel`. That matters: `before_cancel` runs *before* the docstatus is
written, so a refused cancel cannot leave a cancelled row behind if a caller
swallows the exception without rolling back. `cancel_pick()` sets an internal
flag that the guard honours, so only the audited path can cancel.

**The hard boundary is derived, never stored.** At the moment an action is
requested, the reversible quantity is
`min(picked − already returned, WIP balance for that item+batch − what other
pick lists still hold there)`. That subtraction matters because WIP is a
shared pot: several pick lists can park the same batch in it, and one must
not be able to reverse another's stock (test R6). If the request exceeds what
is available, it is refused and the refusal **names the consuming
document(s)** — `Material Issue STE-xxxx (2 on 14-08-2026)` — with the
explicit note that a consumed pick is a manual stock correction. Our own
reversal entries are excluded from that search via the `[WMS-REVERSAL]` tag,
so a return never looks like a consumer.

The only new stored field is `returned_qty` per line — bookkeeping for
repeatable returns and audit, not the state that decides whether a reversal
is allowed.

**Reasons: a new `Applies to Cancel / Return` flag** on the shared
`WMS Pick Reason` master, as agreed. A reason is mandatory for both actions,
must be active, and must carry that flag — a shortage reason like "Batch
empty earlier than expected" is refused, and vice versa. Five reasons are
seeded (Order cancelled, Wrong material picked, Too much picked, Production
not started, Pick list entered in error); the master stays self-managed and
seeding never overwrites edits. Patch `v0_5` adds them to existing sites.

**Reservation follows reality.** A cancelled pick list drops out of the
reservation entirely (docstatus 2), and a returned quantity is subtracted
from its line's reservation — so the demand behind it opens up again on the
pick batch and can be picked afresh. This is the small decision I flagged
before starting; R1 asserts it.

## Test coverage (all live)

| Test | What it proves |
|---|---|
| R1 | Full cancel restores the exact origin batch+location balance, zeroes this pick list's WIP quantity, records reason/audit, sets docstatus 2, reopens the demand |
| R2 | Partial return moves only the specified qty, leaves the rest in WIP, is repeatable, and refuses more than was picked |
| R3 | After a downstream Material Issue consumes part of the WIP qty: cancel refused **naming the consuming voucher**, over-sized return refused, the still-untouched remainder can be returned, nothing beyond it |
| R4 | Both actions refuse without a reason, and refuse a reason from the wrong category; nothing moves during a refusal |
| R5 | The plain Cancel button still refuses and leaves the document submitted with its stock in WIP |
| R6 | Two pick lists sharing one batch in the pot cannot reverse each other's quantity |

## Caveats

1. **Attribution in a shared pot is quantity-based, not lot-tracked.** ERPNext
   does not track which pick list's units are which inside the WIP warehouse.
   If two pick lists hold the same batch and something consumes part of it,
   the consumption is attributed to whichever list tries to reverse first —
   it will be refused, while the other may still succeed. That is the honest
   behaviour of a simple pot; a stricter model would need WIP lot tracking,
   which is deliberately out of scope.
2. **Backdated postings.** Reversals post at today's date. A reversal of a
   backdated pick therefore lands on today, not on the original date.
3. **No un-cancel**, as agreed: redo it as a fresh pick.
4. **Fully consumed picks are untouchable here** by design — manual stock
   correction.
5. `on_cancel` also carries the guard, so even a direct `docstatus` change
   through an unusual code path hits it.

Out of scope and not built: reversing anything that left the WIP pot,
choosing a different batch or location on return, un-cancelling.
