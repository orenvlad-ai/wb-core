# Parity: minimal reference vs target

- Reference minimal-case содержит только empty `sku_display_bundle`.
- Target возвращает `kind = "empty"`, `count = 0` и не пытается строить synthetic projection rows.
- Для minimal-case сохраняется только честный source status по `sku_display_bundle`.
