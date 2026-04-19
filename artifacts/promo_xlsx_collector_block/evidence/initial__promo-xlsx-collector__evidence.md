# Promo XLSX collector: initial bounded evidence

Repo-owned bounded collector block фиксирует уже доказанный внешний contour:

- canonical hydration entry:
  - direct open `https://seller.wildberries.ru/dp-promo-calendar`
  - дождаться `Принимаю`
  - кликнуть `Принимаю`
  - дождаться hydrated DOM `Акции WB` + `timeline-action > 0`
  - при наличии modal закрыть её через known close control
- canonical drawer reset:
  - `#Portal-drawer [data-testid="pages/main-page/promo-action-wizard/drawer-close-button-button-ghost"]`
  - ждать исчезновения `#Portal-drawer [data-testid="pages/main-page/promo-action-wizard/drawer-drawer-overlay"]`
  - только после этого кликать следующий promo block
- metadata sidecar обязателен для всех promo, потому что workbook не несёт promo-level title/period/status
- workbook kinds, доказанные внешним bounded run:
  - `exclude_list_template`
  - `eligible_items_report`
- cross-year short labels `декабрь -> январь` не должны invent-ить exact dates:
  - `promo_start_at = null`
  - `promo_end_at = null`
  - `period_parse_confidence = low`

Этот evidence file нужен только как compact bounded reminder для module doc и smoke contract, не как full operational runbook.
