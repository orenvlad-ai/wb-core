# Evidence: sheet_vitrina_v1

- Источник входа: `artifacts/sheet_vitrina_v1/input/normal__template__delivery-bundle__fixture.json`
- Источник layout: `artifacts/sheet_vitrina_v1/layout/data_vitrina_sheet_layout.json`, `artifacts/sheet_vitrina_v1/layout/status_sheet_layout.json`
- Подтверждающий smoke: `python3 apps/sheet_vitrina_v1_smoke.py`
- Локальный bridge runner: `apps/sheet_vitrina_v1_live_write.py`
- Bound Apps Script bridge: `gas/sheet_vitrina_v1/WideVitrinaBridge.gs`, `.clasp.json`
- Ручной live smoke от `2026-04-12`: в bound Apps Script editor вручную запущена `debugWriteSheetVitrinaV1NormalFixture` для `script_id=1QalhdgdmpxekaTMbNEZM1ubLSPKkTYZ53SHacqBU9HRVJQgEKRdHkgSf`
- Target live sheet: `spreadsheet_id=1ltgE8GltN3Rk8qP1UiaT2NPEwQyPKZ-1tuIqV7EC1NE`
- Live result: в target Google Sheet подтверждено создание листов `DATA_VITRINA` и `STATUS`; `DATA_VITRINA` заполнен данными, `STATUS` создан как второй технический лист витрины
- Цель checkpoint: зафиксировать первый подтверждённый sheet-side live write bridge для bound Google Sheet витрины V1
