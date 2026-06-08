---
title: "Модуль: spp_proxy_block"
doc_id: "WB-CORE-MODULE-35-SPP-PROXY-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать bounded server-owned source и application contract для новой метрики `SPP-прокси`."
scope: "Current-only anonymous public WB card buyer-price source, формула `spp_proxy`, STATUS/loading integration and ready-snapshot materialization. Existing `spp` is not replaced or redefined."
source_basis:
  - "packages/contracts/spp_proxy_block.py"
  - "packages/adapters/spp_proxy_block.py"
  - "packages/application/spp_proxy_block.py"
  - "apps/spp_proxy_source_smoke.py"
  - "apps/sheet_vitrina_v1_spp_proxy_integration_smoke.py"
related_modules:
  - "packages/contracts/spp_proxy_block.py"
  - "packages/adapters/spp_proxy_block.py"
  - "packages/application/spp_proxy_block.py"
  - "packages/application/sheet_vitrina_v1_live_plan.py"
related_tables: []
related_endpoints:
  - "GET https://www.wildberries.ru/catalog/{nmId}/detail.aspx [anonymous current-only]"
  - "GET https://card.wb.ru/cards/v4/detail?...&nm={nmId} [anonymous current-only fallback]"
  - "public WB card API fallback [anonymous current-only]"
related_runners:
  - "apps/spp_proxy_source_smoke.py"
  - "apps/sheet_vitrina_v1_spp_proxy_integration_smoke.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "05_MODULE__SPP_BLOCK.md"
  - "31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Добавлен отдельный current-only public-card source для `SPP-прокси`; текущий `spp` сохраняет прежний смысл."
---

# 1. Идентификатор и статус

- `module_id`: `spp_proxy_block`
- `source_key`: `spp_proxy`
- `metric_key`: `spp_proxy`
- `label_ru`: `SPP-прокси`
- `family`: `public-web`
- `status_main`: active in repo

# 2. Business Semantics

- `SPP-прокси` не заменяет и не переименовывает текущий `SPP` / `spp`.
- Формула:
  - `spp_proxy = (price_seller_discounted - public_buyer_price) / price_seller_discounted`
  - значение хранится как доля: `0.23` means `23%`.
- `price_seller_discounted` берётся из existing server-owned `prices_snapshot` lookup.
- `public_buyer_price` берётся из anonymous public WB card contour, without Seller Portal auth/session/storage state.
- Если seller discounted price отсутствует/нулевой, public buyer price не получена или public buyer price выше seller discounted price, result stays blank/`None` and STATUS diagnostics explain the reason. Fake zero is not produced.

# 3. Source Semantics

- Source is current-only: public card/API gives current anonymous buyer price, not historical truth.
- `today_current` can accept a valid exact business-date snapshot into `accepted_current_snapshot`.
- `yesterday_closed` materializes only from a prior accepted current snapshot for the same date; current values are not backfilled into closed-day history.
- Later failed/blank public-card attempts preserve already accepted same-day `SPP-прокси` values and expose the latest attempt reason in STATUS.
- Public fetch is anonymous. The adapter does not print cookies, headers with secrets or Seller Portal state.
- Parser priority:
  - hydrated/script JSON and public card API payloads first;
  - WB `cards/v4/detail` is the primary API fallback when the public detail HTML returns an anti-bot/challenge page;
  - bounded HTML/meta/DOM price fallback only when JSON/API price is unavailable.
- Live WB can return anti-bot/challenge pages. That state is a controlled missing/error diagnostic, not a false green source outcome.

# 4. Contract Shape

- Request:
  - `snapshot_date`
  - `nm_ids`
  - `price_seller_discounted_by_nm_id`
- Success/incomplete item:
  - `nm_id`
  - `spp_proxy`
  - `price_seller_discounted`
  - `public_buyer_price`
  - `spp_proxy_rub`
- Diagnostics include per-`nmId` reason plus source/parser context for missing/invalid rows.

# 5. Web-Vitrina Integration

- `sheet_vitrina_v1_live_plan` loads `prices_snapshot` before `spp_proxy`.
- A selected/group refresh of `spp_proxy` expands source dependencies with `prices_snapshot`, so the formula has server-owned seller price truth without a manual UI action.
- Loading/source-status group: `WB public card / бот`.
- Metric registry row:
  - scope `SKU`
  - section `Цены`
  - format `percent`
  - display order directly after existing `spp`
- Full registry upload bundle also exposes `avg_spp_proxy` as the existing TOTAL `avg_*` convention for SPP-like percent rows; it is an arithmetic mean over available SKU `spp_proxy` values, not a replacement for `avg_spp`.
- `spp_proxy` participates in accepted-current preservation separately from `spp`.

# 6. Verification

- `apps/spp_proxy_source_smoke.py`
  - fixture HTML/JSON public-card price extraction;
  - formula and ratio normalization;
  - missing/zero seller price and missing public price stay blank;
  - public price above seller price stays blank with diagnostic;
  - historical current-only fetch does not hit live public card.
  - public detail anti-bot response falls through to `cards/v4/detail` and normalizes WB minor-unit `sizes.price.product` values.
- `apps/sheet_vitrina_v1_spp_proxy_integration_smoke.py`
  - live-plan materializes `SPP-прокси`;
  - web-vitrina contract exposes the row;
  - source-status exposes `spp_proxy` in `WB public card / бот`;
  - failed later attempts preserve accepted current value;
  - existing `spp` row remains unchanged;
  - group refresh updates only `spp_proxy` selected-date cells.
