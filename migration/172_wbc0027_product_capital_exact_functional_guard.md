# Migration 172 — WBC0027 exact-functional product-capital guard

## Scope

- account-level CNY documents without an exact shipment identity become a
  durable warehouse no-op;
- mutable own-capital events become replay signals only;
- exact functional publication is the sole post-cutover product-capital
  writer, with date/version/digest/publication provenance;
- both product-capital readers reject unbound or mismatched projection rows;
- warehouse projection health exhaustively reconciles the 42-key allowlist;
- one production-mutation manifest prepares two separate T1 recovery
  operations: product capital, then qualified cost/Proxy.

## Production qualification frozen by the manifest

- 17–29 August: 936 rows / 19,656 cells; 7,655 mismatches, including
  7,639 event-path cells and 16 separately qualified exact-zero cells for SKU
  `497413772` on 21 August;
- 13, 14 and 16 August: 216 rows and 1,791 mismatches;
- 15 August: `EVIDENCE_BLOCKED`, because no immutable same-date functional
  version exists;
- 30 August and later: hard non-target, exact at qualification;
- cost/Proxy: 298 logical / 472 persisted source-proven repairs on 26 and
  29 August; twelve 26-August protected/dependent cells remain explicit
  evidence-blocked rather than guessed.

The Release Runner stops at `awaiting_apply`. Only the trusted Apply Runner may
execute the exact reviewed manifest after deployed-SHA and OWNER/MEMBER comment
authorization validation.
