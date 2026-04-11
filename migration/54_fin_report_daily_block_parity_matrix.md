# Parity Matrix Блока Fin Report Daily

## Success-Case

| Legacy semantics | Target field | Status |
| --- | --- | --- |
| `snapshot_date` | `result.snapshot_date` | required |
| `nmId` | `result.items[].nm_id` | required |
| `fin_delivery_rub` | `result.items[].fin_delivery_rub` | required |
| `fin_storage_fee` | `result.items[].fin_storage_fee` | required |
| `fin_deduction` | `result.items[].fin_deduction` | required |
| `fin_commission` | `result.items[].fin_commission` | required |
| `fin_penalty` | `result.items[].fin_penalty` | required |
| `fin_additional_payment` | `result.items[].fin_additional_payment` | required |
| `fin_buyout_rub` | `result.items[].fin_buyout_rub` | required |
| `fin_commission_wb_portal` | `result.items[].fin_commission_wb_portal` | required |
| `fin_acquiring_fee` | `result.items[].fin_acquiring_fee` | required |
| `fin_loyalty_rub` | `result.items[].fin_loyalty_rub` | required |

## Special Total Row

| Legacy semantics | Target field | Status |
| --- | --- | --- |
| `nmId = 0` | `result.storage_total.nm_id` | required |
| total `fin_storage_fee` | `result.storage_total.fin_storage_fee_total` | required |

## Checkpoint Scope

На первом checkpoint проверяется:
- success-case на bootstrap sample set;
- special total row `nmId = 0`;
- bounded `rrdid` pagination semantics;
- sale/return normalization для `fin_buyout_rub` и `fin_commission_wb_portal`.
