# 94. Nomenclature WB SKU Reference

## Scope

`sheet_vitrina_v1_nomenclature_items` gains server-owned WB SKU reference fields for future FBW supply planning:

- `barcode`
- `barcodes_json`
- `barcode_source`
- `barcode_status`
- `barcode_synced_at`
- `barcode_updated_at`
- `barcode_evidence_json`
- `vendor_code`
- `wb_title`
- `wb_subject_name`
- `wb_updated_at`
- `wb_synced_at`
- `wb_sync_status`
- `wb_sync_evidence_json`
- `is_hidden`
- `hidden_at`
- `hidden_reason`

`sheet_vitrina_v1_sku_groups` is the server-owned dictionary for SKU group labels and vendorCode aliases/patterns. Seed groups are Clean, Anti-spy, Matte, No Frame Clean, No Frame Anti-spy, No Frame Matte, `extra` and `other`.

## Migration Safety

The runtime schema migration is non-destructive and uses nullable/default columns. Existing nomenclature rows survive unchanged and read back with empty WB reference fields, `barcode_source=missing`, `barcode_status=missing` and `is_hidden=false`.

Manual barcode overrides remain authoritative. Read-only WB Content sync reads `POST /content/v2/get/cards/list` with cursor pagination through canonical `WB_API_TOKEN`, matches local rows by `nm_id`, then barcode, then `vendor_code`, and includes hidden rows in the matching pool. Existing rows may update only WB-owned/reference fields and sync evidence; sync must not overwrite nomenclature name, SKU group, purchase price, match key, compatible models, operational `is_active`, hidden state or manual barcode override.

New WB cards create local rows with `nomenclature_name` from `vendorCode` when available, otherwise WB title. SKU group auto-detection uses server-owned group aliases over normalized vendorCode. Unknown vendorCode becomes `product_type=other`, `wb_sync_status=needs_review` and is visible for operator review instead of silently picking a fuzzy title match.

Hidden SKU rows are not physically deleted. They are hidden from the default visible list, remain available through `visibility=hidden|all`, stay eligible for future sync matching, and are not automatically restored when WB returns the card again.

Legacy `clear` / `anti_spy` / `matte` values survive. UI/export labels for the legacy product groups are Clean, Anti-spy and Matte; old Russian import labels remain accepted for compatibility.

## Out Of Scope

No WB mutations, no fuzzy auto-match by WB title, no FBW supply planning, no FBW/FBS supply creation, no Google Sheets/GAS, no localStorage truth.
