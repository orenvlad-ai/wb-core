# 94. Nomenclature WB Barcode Reference

## Scope

`sheet_vitrina_v1_nomenclature_items` gains server-owned WB barcode reference fields for future FBW supply planning:

- `barcode`
- `barcodes_json`
- `barcode_source`
- `barcode_status`
- `barcode_synced_at`
- `barcode_updated_at`
- `barcode_evidence_json`

## Migration Safety

The runtime schema migration is non-destructive and uses nullable/default columns. Existing nomenclature rows survive unchanged and read back with empty `barcode`, empty `barcodes`, `barcode_source=missing` and `barcode_status=missing`.

Manual barcode overrides remain authoritative. Read-only WB Content sync may fill missing non-manual rows from `POST /content/v2/get/cards/list`, but token/API failures become diagnostics and do not reject nomenclature saves.

## Out Of Scope

No WB mutations, no FBW supply planning, no FBW/FBS supply creation, no Google Sheets/GAS, no localStorage truth.
