# WB Finance daily management report and weekly SPP

## Source and isolation

`Отчёты → Финотчёт ВБ` has independent `weekly` and `daily` report periods. Both
use the official `POST /api/finance/v1/sales-reports/detailed` endpoint through
the shared seller-account rate gate. Daily acquisition requests one closed
Moscow date with `period=daily`; terminal HTTP 204 is required after all pages.
An initial 204 without rows is unknown, not a zero report. The operator read
endpoint never calls Wildberries.

Daily raw snapshots are immutable and keyed by seller, date and batch. An
authoritative daily pointer selects one fully fetched batch. Operational sync
and aggregate rows carry its batch identity, source digest and row count. A
failure after raw commit can be resumed from that batch; a mismatched or
unprojected batch is not published. Daily rows do not enter weekly raw/current
views, weekly sync, Partner or Proxy consumers. `reportId` and `rrdId` remain
strings at the JSON/browser boundary, including values beyond JavaScript's
safe integer range.

The initial daily history is the 14 closed Moscow days before the first schema
initialization. This lower bound is persisted, so a delayed day cannot fall
out of a moving window. The background worker processes a bounded number of
due days per run, then follows new closed dates and retains retry state. The
`bootstrap` CLI processes up to 14 due days in one bounded invocation; it and
the timer take the same process lock, while the shared Finance API gate keeps
seller requests serialized. The canonical HTTP startup creates the daily
schema before the first GET.
The screen and read admission cover the latest 14 days; older stored history is
retained but is not presented as freshly checked. The latest days remain
preliminary until a second unchanged fetch. No synthetic daily report is made
by slicing a weekly report or using operational sales.

## Calculation and presentation

Daily and weekly periods share the Finance row classifier, monetary formulas,
canonical channel-aware COGS resolver and operator table definitions. Daily
capitalization applies exact, capped supply-layer allocations over daily raw
history only; the weekly allocator continues over weekly raw history only.
Its dependency hash covers every authoritative daily pointer, including a
later report containing earlier operations. A separate exact canonical cost
source fingerprint and economic `cost_state_hash` fail closed after cost
changes. The background tick reprojects stale days from stored raw; read HTTP
checks dependencies without recalculating COGS.
The daily screen uses the same row order, expenses, margins, coverage meanings
and colors as the weekly screen. Missing source or cost remains missing.

The two new weekly disclosure rows are `СПП FBO, %` and `СПП FBS, %`, each the
quantity-weighted mean of valid `spp` percentages on positive gross sale rows
in Russia (`docTypeName` and `sellerOperName` both `Продажа`). A valid zero
participates in the denominator. Returns, service rows, missing/invalid SPP,
non-Russian rows and unknown/conflicting channels do not. Explicit
`deliveryMethod` FBO/FBW/FBS is used when unambiguous and DBS is excluded;
the only accepted office aliases are `Склад WB` and `Склад поставщика - везу на
склад WB`. Unknown channels remain unknown. Coverage publishes candidate,
classified, unknown and valid row/quantity counts. The projection is bounded
to the latest ten stored weeks and depends on the acknowledged weekly raw
content hash; it never triggers a weekly COGS rebuild. Older or unsupported
weeks display unavailable, not zero. SPP is informational and does not change
Finance revenue, commission, profit or COGS formulas.

Daily raw tables belong to the canonical `finance_raw` SQLite store. Normal
split-store snapshots and restores copy that complete database. The older
monolith-to-split migration handles weekly raw only and explicitly rejects a
monolith with nonempty daily raw, rather than misrouting or dropping it.
