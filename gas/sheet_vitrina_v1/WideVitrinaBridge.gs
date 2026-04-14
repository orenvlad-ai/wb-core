const TARGET_SPREADSHEET_ID = '1ltgE8GltN3Rk8qP1UiaT2NPEwQyPKZ-1tuIqV7EC1NE';
const TARGET_SPREADSHEET_NAME = 'WB Core Vitrina V1';
const EXPECTED_SHEET_NAMES = ['DATA_VITRINA', 'STATUS'];

function getSheetVitrinaBridgeTargetInfo() {
  const spreadsheet = getTargetSpreadsheet_();
  return JSON.stringify({
    spreadsheet_id: spreadsheet.getId(),
    spreadsheet_name: spreadsheet.getName(),
    sheet_names: spreadsheet.getSheets().map((sheet) => sheet.getName()),
  });
}

function writeSheetVitrinaV1Plan(planJson) {
  const plan = parsePlan_(planJson);
  const spreadsheet = getTargetSpreadsheet_();
  const sheets = plan.sheets.map((target) => writeSheetTarget_(spreadsheet, target));
  return JSON.stringify({
    ok: 'success',
    spreadsheet_id: spreadsheet.getId(),
    spreadsheet_name: spreadsheet.getName(),
    snapshot_id: plan.snapshot_id,
    as_of_date: plan.as_of_date,
    sheets: sheets,
  });
}

function getSheetVitrinaV1State() {
  const spreadsheet = getTargetSpreadsheet_();
  return JSON.stringify({
    spreadsheet_id: spreadsheet.getId(),
    spreadsheet_name: spreadsheet.getName(),
    sheets: EXPECTED_SHEET_NAMES.map((sheetName) => describeSheetState_(spreadsheet, sheetName)),
  });
}

function debugWriteSheetVitrinaV1NormalFixture() {
  return writeSheetVitrinaV1Plan(String.raw`{
  "plan_version": "wide_matrix_delivery_v1__sheet_scaffold_v1",
  "snapshot_id": "2026-04-05__wide_matrix_delivery_v1__normal",
  "as_of_date": "2026-04-05",
  "sheets": [
    {
      "sheet_name": "DATA_VITRINA",
      "write_start_cell": "A1",
      "write_rect": "A1:E19",
      "clear_range": "A:ZZ",
      "write_mode": "full_overwrite",
      "partial_update_allowed": false,
      "header": [
        "label",
        "key",
        "2026-04-03",
        "2026-04-04",
        "2026-04-05"
      ],
      "rows": [
        [
          "Итого: Остаток, шт",
          "TOTAL|stock_total",
          14630.0,
          14660.0,
          14692.0
        ],
        [
          "Итого: Показы рекламы",
          "TOTAL|ads_views",
          2160.0,
          2230.0,
          2300.0
        ],
        [
          "Группа hoodie: Остаток, шт",
          "GROUP:hoodie|stock_total",
          9050.0,
          9140.0,
          9230.0
        ],
        [
          "Группа hoodie: Показы рекламы",
          "GROUP:hoodie|ads_views",
          1400.0,
          1450.0,
          1500.0
        ],
        [
          "Группа tshirt: Остаток, шт",
          "GROUP:tshirt|stock_total",
          5580.0,
          5520.0,
          5462.0
        ],
        [
          "Группа tshirt: Показы рекламы",
          "GROUP:tshirt|ads_views",
          760.0,
          780.0,
          800.0
        ],
        [
          "Худи basic: Остаток, шт",
          "SKU:210183919|stock_total",
          9050.0,
          9140.0,
          9230.0
        ],
        [
          "Худи basic: Цена со скидкой",
          "SKU:210183919|price_seller_discounted",
          1019.0,
          1009.0,
          999.0
        ],
        [
          "Худи basic: СПП",
          "SKU:210183919|spp",
          0.248,
          0.247,
          0.24621052631578932
        ],
        [
          "Худи basic: Показы рекламы",
          "SKU:210183919|ads_views",
          1400.0,
          1450.0,
          1500.0
        ],
        [
          "Худи basic: CTR рекламы",
          "SKU:210183919|ads_ctr",
          0.04857142857142857,
          0.0496551724137931,
          0.05
        ],
        [
          "Худи basic: Прокси-прибыль, руб",
          "SKU:210183919|proxy_profit_rub",
          790.0,
          812.0,
          835.0
        ],
        [
          "Футболка base: Остаток, шт",
          "SKU:210184534|stock_total",
          5580.0,
          5520.0,
          5462.0
        ],
        [
          "Футболка base: Цена со скидкой",
          "SKU:210184534|price_seller_discounted",
          1410.0,
          1400.0,
          1390.0
        ],
        [
          "Футболка base: СПП",
          "SKU:210184534|spp",
          0.2445,
          0.243,
          0.24250000000000005
        ],
        [
          "Футболка base: Показы рекламы",
          "SKU:210184534|ads_views",
          760.0,
          780.0,
          800.0
        ],
        [
          "Футболка base: CTR рекламы",
          "SKU:210184534|ads_ctr",
          0.038157894736842106,
          0.03974358974358974,
          0.04
        ],
        [
          "Футболка base: Прокси-прибыль, руб",
          "SKU:210184534|proxy_profit_rub",
          469.0,
          488.0,
          507.0
        ]
      ],
      "row_count": 18,
      "column_count": 5
    },
    {
      "sheet_name": "STATUS",
      "write_start_cell": "A1",
      "write_rect": "A1:K12",
      "clear_range": "A:K",
      "write_mode": "full_overwrite",
      "partial_update_allowed": false,
      "header": [
        "source_key",
        "kind",
        "freshness",
        "snapshot_date",
        "date",
        "date_from",
        "date_to",
        "requested_count",
        "covered_count",
        "missing_nm_ids",
        "note"
      ],
      "rows": [
        [
          "sku_display_bundle",
          "success",
          "",
          "",
          "",
          "",
          "",
          3,
          3,
          "",
          ""
        ],
        [
          "web_source_snapshot",
          "success",
          "2026-04-04",
          "",
          "",
          "2026-04-04",
          "2026-04-04",
          3,
          2,
          "210185771",
          ""
        ],
        [
          "seller_funnel_snapshot",
          "success",
          "2026-04-04",
          "",
          "2026-04-04",
          "",
          "",
          3,
          2,
          "210185771",
          ""
        ],
        [
          "prices_snapshot",
          "success",
          "2026-04-05",
          "2026-04-05",
          "",
          "",
          "",
          3,
          2,
          "210185771",
          ""
        ],
        [
          "sf_period",
          "success",
          "2026-04-05",
          "2026-04-05",
          "",
          "",
          "",
          3,
          2,
          "210185771",
          ""
        ],
        [
          "spp",
          "success",
          "2026-04-04",
          "2026-04-04",
          "",
          "",
          "",
          3,
          2,
          "210185771",
          ""
        ],
        [
          "ads_bids",
          "success",
          "2026-04-05",
          "2026-04-05",
          "",
          "",
          "",
          3,
          2,
          "210185771",
          ""
        ],
        [
          "stocks",
          "success",
          "2026-04-05",
          "2026-04-05",
          "",
          "",
          "",
          3,
          2,
          "210185771",
          ""
        ],
        [
          "ads_compact",
          "success",
          "2026-04-05",
          "2026-04-05",
          "",
          "",
          "",
          3,
          2,
          "210185771",
          ""
        ],
        [
          "fin_report_daily",
          "success",
          "2026-04-05",
          "2026-04-05",
          "",
          "",
          "",
          3,
          2,
          "210185771",
          "fin_storage_fee_total=75.0"
        ],
        [
          "sales_funnel_history",
          "success",
          "2026-04-05",
          "",
          "",
          "2026-03-30",
          "2026-04-05",
          3,
          2,
          "210185771",
          ""
        ]
      ],
      "row_count": 11,
      "column_count": 11
    }
  ]
}`);
}

function parsePlan_(planJson) {
  const plan = JSON.parse(planJson);
  if (!plan || typeof plan !== 'object') {
    throw new Error('sheet write plan must be an object');
  }
  if (!Array.isArray(plan.sheets) || plan.sheets.length !== 2) {
    throw new Error('sheet write plan must contain exactly two sheets');
  }
  return plan;
}

function getTargetSpreadsheet_() {
  const spreadsheet = SpreadsheetApp.openById(TARGET_SPREADSHEET_ID);
  if (spreadsheet.getId() !== TARGET_SPREADSHEET_ID) {
    throw new Error('unexpected spreadsheet id');
  }
  if (spreadsheet.getName() !== TARGET_SPREADSHEET_NAME) {
    throw new Error(`unexpected spreadsheet name: ${spreadsheet.getName()}`);
  }
  return spreadsheet;
}

function writeSheetTarget_(spreadsheet, target) {
  validateTarget_(target);
  const sheet = spreadsheet.getSheetByName(target.sheet_name) || spreadsheet.insertSheet(target.sheet_name);
  const header = target.header;
  const rows = target.rows || [];
  const matrix = [header].concat(rows);
  sheet.getRange(target.clear_range).clearContent();
  const writeRange = sheet.getRange(target.write_start_cell).offset(0, 0, matrix.length, header.length);
  if (writeRange.getA1Notation() !== target.write_rect) {
    throw new Error(`unexpected write rect for ${target.sheet_name}: ${writeRange.getA1Notation()} != ${target.write_rect}`);
  }
  writeRange.setValues(matrix);
  return {
    sheet_name: target.sheet_name,
    write_rect: writeRange.getA1Notation(),
    row_count: rows.length,
    column_count: header.length,
  };
}

function validateTarget_(target) {
  if (!target || typeof target !== 'object') {
    throw new Error('sheet target must be an object');
  }
  if (EXPECTED_SHEET_NAMES.indexOf(target.sheet_name) === -1) {
    throw new Error(`unexpected sheet target: ${target.sheet_name}`);
  }
  if (target.write_mode !== 'full_overwrite') {
    throw new Error(`unsupported write mode for ${target.sheet_name}: ${target.write_mode}`);
  }
  if (target.partial_update_allowed !== false) {
    throw new Error(`partial update is forbidden for ${target.sheet_name}`);
  }
  if (!Array.isArray(target.header) || target.header.length === 0) {
    throw new Error(`missing header for ${target.sheet_name}`);
  }
  if (!Array.isArray(target.rows)) {
    throw new Error(`rows must be an array for ${target.sheet_name}`);
  }
}

function describeSheetState_(spreadsheet, sheetName) {
  const sheet = spreadsheet.getSheetByName(sheetName);
  if (!sheet) {
    return {
      sheet_name: sheetName,
      present: false,
      last_row: 0,
      last_column: 0,
      header: [],
    };
  }
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  const header = lastRow > 0 && lastColumn > 0 ? sheet.getRange(1, 1, 1, lastColumn).getValues()[0] : [];
  const state = {
    sheet_name: sheetName,
    present: true,
    last_row: lastRow,
    last_column: lastColumn,
    header: header,
  };
  if (lastRow <= 1 || lastColumn <= 0) {
    return state;
  }

  const values = sheet.getRange(1, 1, lastRow, lastColumn).getValues();
  if (sheetName === 'DATA_VITRINA') {
    const dataRows = values.slice(1);
    const metricKeys = [];
    const scopeRowCounts = {
      TOTAL: 0,
      GROUP: 0,
      SKU: 0,
      OTHER: 0,
    };
    let nonEmptyValueRowCount = 0;
    dataRows.forEach((row) => {
      const key = String(row[1] || '').trim();
      if (!key) {
        return;
      }
      const pipeIndex = key.lastIndexOf('|');
      metricKeys.push(pipeIndex >= 0 ? key.slice(pipeIndex + 1) : key);
      if (key.startsWith('TOTAL|')) {
        scopeRowCounts.TOTAL += 1;
      } else if (key.startsWith('GROUP:')) {
        scopeRowCounts.GROUP += 1;
      } else if (key.startsWith('SKU:')) {
        scopeRowCounts.SKU += 1;
      } else {
        scopeRowCounts.OTHER += 1;
      }
      if (row.slice(2).some((cell) => cell !== '' && cell !== null)) {
        nonEmptyValueRowCount += 1;
      }
    });
    const uniqueMetricKeys = Array.from(new Set(metricKeys)).sort();
    state.data_row_count = dataRows.length;
    state.metric_key_count = uniqueMetricKeys.length;
    state.metric_keys = uniqueMetricKeys;
    state.scope_row_counts = scopeRowCounts;
    state.non_empty_value_row_count = nonEmptyValueRowCount;
  }

  if (sheetName === 'STATUS') {
    state.status_row_count = Math.max(lastRow - 1, 0);
    state.source_keys = values
      .slice(1)
      .map((row) => String(row[0] || '').trim())
      .filter((value) => value);
  }
  return state;
}
