# Migration 149: isolated FBS payment-order parser

## Scope

This repo-only change introduces a deterministic text-layer parser for Russian
payment orders in form `0401060`. It recognizes the supplied WB Bank and VTB
layouts through separate versioned adapters and emits one common normalized
contract. The existing repository-owned financial PDF text extractor is reused:
`pdftotext -layout` when available, then `pypdf`. OCR is not used.

The result contains source bank and adapter, payer-bank BIC, payment-order
number, document/debit/execution dates, execution status and timestamp, amount
and RUB currency, normalized payer/beneficiary identities and bank details,
payment purpose, explicit invoice reference, explicit VAT evidence, parser and
parse status, deterministic warnings/errors, exact file SHA-256 and a versioned
content fingerprint.

## Fail-closed contract

- unreadable, damaged, unsupported or ambiguous documents return `parse_error`
  or `needs_review` and are never `posting_eligible`;
- only a structurally complete document with an explicit executed stamp is
  eligible; an unclear or negative execution marker remains non-postable;
- an absent optional invoice date produces a stable warning without preventing
  an otherwise complete executed document from parsing;
- VAT status, rate and amount are populated only from explicit purpose text;
- the payment fingerprint is derived from normalized business identity fields,
  not filename or file hash, so equivalent regenerated layouts deduplicate while
  different payments remain distinct;
- normalized output and fingerprint material do not retain the source filename,
  raw text, signature certificate or electronic-signature metadata.

VTB adapter v2 first strips only recognized right-side form-control labels
(`Вид оп.`, `Срок плат.` and their bounded value cells) from a beneficiary
line, then parses the remaining left-side candidate. A beneficiary sharing the
same physical line with those controls is therefore supported without treating
control text as the name. Multiple or structurally ambiguous candidates remain
`needs_review`; the cleanup is not a heuristic recipient guess.

## Boundaries and evidence

This parser creates no posting, expense allocation, FBS warehouse assignment,
database persistence, HTTP route, UI, runtime/deploy wiring or production data
mutation. The recipient is not interpreted as a warehouse.

Migration 151 consumes this unchanged parser contract from the canonical
facility/pool `pool_overhead` document workflow. That downstream integration
persists normalized evidence and applies its own execution/RUB/dedup/posting
gates; no allocation or facility/category inference has moved into the parser.

`apps/russian_payment_orders_smoke.py` renders only synthetic/anonymized PDF
fixtures and proves both adapters, both invoice-reference phrases, VAT 5%,
non-taxable VAT, optional-date warning, stable content fingerprint across PDF
filename/layout changes, distinct-payment separation and fail-closed handling
for unsupported, damaged and non-executed documents. The inline-control VTB
regression is fully synthetic. The two supplied real PDFs
are local read-only acceptance evidence only and are never stored in git.
