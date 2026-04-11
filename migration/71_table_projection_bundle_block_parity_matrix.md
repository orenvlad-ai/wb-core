# Parity Matrix Для Table Projection Bundle

| Upstream/reference source | Target field | Status |
| --- | --- | --- |
| `sku_display_bundle_block.result.items[]` | `result.items[].nm_id/display_name/group/enabled/display_order` | required |
| `web_source_snapshot_block.result` | `result.items[].web_source.search_analytics` + `result.source_statuses[]` | required |
| `seller_funnel_snapshot_block.result` | `result.items[].web_source.seller_funnel_daily` + `result.source_statuses[]` | required |
| `prices_snapshot_block.result` | `result.items[].official_api.prices` + `result.source_statuses[]` | required |
| `sf_period_block.result` | `result.items[].official_api.sf_period` + `result.source_statuses[]` | required |
| `spp_block.result` | `result.items[].official_api.spp` + `result.source_statuses[]` | required |
| `ads_bids_block.result` | `result.items[].official_api.ads_bids` + `result.source_statuses[]` | required |
| `stocks_block.result` | `result.items[].official_api.stocks` + `result.source_statuses[]` | required |
| `ads_compact_block.result` | `result.items[].official_api.ads_compact` + `result.source_statuses[]` | required |
| `fin_report_daily_block.result` | `result.items[].official_api.fin_report_daily` + `result.source_statuses[]` | required |
| `sales_funnel_history_block.result` | `result.items[].history_summary` + `result.source_statuses[]` | required |
| empty sku bundle | `result.kind = "empty"` | required |

## Комментарии

- Для этого модуля используется `reference`, а не классический `legacy`, потому что upstream source уже живёт в merged module outputs `wb-core`.
- Full history inline не является parity-обязательством этого checkpoint; обязательна только честная `history_summary`/linked representation.
