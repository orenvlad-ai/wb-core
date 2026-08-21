# Migration 151: facility-pool FBS/FBO overhead expenses

## Scope

This live/runtime migration extends the existing `pool_overhead` facility-pool
document; it does not add another ledger or allocation algorithm. The protected
operator form accepts an explicitly selected active FF facility, explicit
`FBS`, `FBO` or `both`, one stable expense category, a positive RUB amount and
an optional comment. Category `other` requires an explanatory comment. The
server derives the current business date from the selected facility timezone;
historical payment and execution dates are evidence only and never backdate a
posting.

Stable categories are:

- `fbs_order_processing` — Обработка FBS-заказов;
- `inbound_logistics_to_ff` — Логистика до склада FF;
- `storage` — Хранение;
- `internal_warehouse_movement` — Перемещение внутри склада;
- `receiving` — Приёмка;
- `returns_processing` — Обработка возвратов/невыкупов;
- `packaging_labeling_consumables` — Упаковка, маркировка и расходные материалы;
- `other` — Прочие.

One document has exactly one category. Multi-category splitting, OCR,
automatic facility/pool/category inference and retrospective profitability
recalculation remain out of scope.

## Manual and payment-order evidence

Manual mode works without a file and persists the actor plus an explicit
manual-source marker. PDF mode reuses the versioned Russian payment-order
parser from migration 149. Only parsed, explicitly executed, posting-eligible
RUB evidence may be confirmed. Parsed amount is authoritative while attached;
a different amount requires removing the file and using manual mode.

The canonical request stores the original source bytes behind the existing
authenticated document-file path, filename/content type/file SHA-256,
normalized parser result, parser and fingerprint versions and the content
payment fingerprint. A unique append-only fingerprint binding aliases renamed
or regenerated equivalent evidence and new client request IDs to the existing
canonical request/document. Unsupported, damaged, OCR-only, ambiguous,
needs-review and non-executed PDFs persist as blocked evidence and cannot create
or post an overhead document.

## Posting and fail-closed invariants

The existing pool allocator distributes exact kopecks by current positive
physical quantity in the selected facility/pool scope. Reservations are not in
the denominator. Every positive physical SKU must have positive known capital;
if any does not, the entire preview blocks with the missing rows and no amount
is redistributed over a subset. No positive denominator also blocks.

Preview freezes facility, scope, category/comment/source, exact amount,
quantity and capital basis, payment evidence/dedup state and feature epoch.
Confirm rebuilds and compares that plan before T1 and again inside the writer
transaction. Posting has `quantity_delta=0`, increases capital by the exact
input total and recalculates the facility-local WAC. Other facilities/pools are
unchanged. Storno reverses the exact original capital effect and retains the
category/payment evidence link.

The legacy aggregate overhead records and reversal APIs remain historical
compatibility. Their operator creation form is retired with a link to
`Документы фулфилмента → Накладные расходы FBS/FBO`, making `pool_overhead` the
single new-posting path.

## Verification and deployment boundary

Focused domain, surface, HTTP and browser smokes use only synthetic fixtures.
They cover all category codes, explicit selections, facility-timezone date,
manual and both supported bank layouts, fail-closed evidence, equivalent-PDF
dedup, amount mismatch, stale preview, missing capital basis, exact cent and
quantity conservation, other-facility invariance, storno, registry/source-file
readback and action-switch payload clearing.

Deployment is schema-additive and does not create a facility, overhead request,
document, movement or production-data mutation. Production UI verification is
read-only and must not submit a business document.
