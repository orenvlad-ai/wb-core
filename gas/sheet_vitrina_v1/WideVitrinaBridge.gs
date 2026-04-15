const TARGET_SPREADSHEET_ID = '1ltgE8GltN3Rk8qP1UiaT2NPEwQyPKZ-1tuIqV7EC1NE';
const TARGET_SPREADSHEET_NAME = 'WB Core Vitrina V1';
const EXPECTED_SHEET_NAMES = ['DATA_VITRINA', 'STATUS'];
const DATA_VITRINA_MATRIX_HEADER = ['дата', 'key'];

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
  const presentation = applySheetVitrinaV1PresentationPass_ ? applySheetVitrinaV1PresentationPass_(spreadsheet) : null;
  return JSON.stringify({
    ok: 'success',
    spreadsheet_id: spreadsheet.getId(),
    spreadsheet_name: spreadsheet.getName(),
    snapshot_id: plan.snapshot_id,
    as_of_date: plan.as_of_date,
    sheets: sheets,
    presentation: presentation,
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
  if (target.sheet_name === 'DATA_VITRINA') {
    return writeDataVitrinaMatrixTarget_(sheet, target);
  }
  return writeFullOverwriteTarget_(sheet, target);
}

function writeFullOverwriteTarget_(sheet, target) {
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

function enrichDataVitrinaWriteSummary_(summary, target) {
  const header = Array.isArray(target.header) ? target.header : [];
  const metricKeys = collectDataVitrinaMetricKeys_(target.rows || []);
  summary.source_row_count = Array.isArray(target.rows) ? target.rows.length : 0;
  summary.source_metric_key_count = metricKeys.length;
  summary.displayed_metric_count = metricKeys.length;
  summary.metric_keys = metricKeys;
  if (isDataVitrinaMatrixHeader_(header)) {
    summary.layout_mode = 'date_matrix';
    summary.date_columns = header.slice(2);
    summary.rendered_data_row_count = summary.row_count;
    return summary;
  }
  if (isDataVitrinaFlatHeader_(header)) {
    summary.layout_mode = 'flat_rows';
    summary.date_columns = header.slice(2);
    summary.rendered_data_row_count = summary.row_count;
  }
  return summary;
}

function collectDataVitrinaMetricKeys_(rows) {
  const metricKeys = [];
  (Array.isArray(rows) ? rows : []).forEach((row) => {
    const key = normalizeDataVitrinaMetricKey_(row[1]);
    if (!key || isDataVitrinaBlockKey_(row[1])) {
      return;
    }
    pushUnique_(metricKeys, key);
  });
  return metricKeys;
}

function normalizeDataVitrinaMetricKey_(key) {
  const normalized = String(key || '').trim();
  if (!normalized) {
    return '';
  }
  if (normalized.indexOf('|') >= 0) {
    return normalized.slice(normalized.lastIndexOf('|') + 1).trim();
  }
  return normalized;
}

function writeDataVitrinaMatrixTarget_(sheet, target) {
  const existingState = readExistingDataVitrinaMatrixState_(sheet);
  const incomingState = buildIncomingDataVitrinaMatrixState_(target.header, target.rows || []);
  if (!incomingState.block_order.length || !incomingState.dates.length) {
    return enrichDataVitrinaWriteSummary_(writeFullOverwriteTarget_(sheet, target), target);
  }

  const mergedState = mergeDataVitrinaMatrixStates_(existingState, incomingState);
  const matrix = buildDataVitrinaMatrixSheet_(mergedState);
  sheet.getRange(target.clear_range).clearContent();
  const writeRange = sheet.getRange(target.write_start_cell).offset(0, 0, matrix.length, matrix[0].length);
  writeRange.setValues(matrix);
  return {
    sheet_name: target.sheet_name,
    write_rect: writeRange.getA1Notation(),
    row_count: matrix.length - 1,
    column_count: matrix[0].length,
    layout_mode: 'date_matrix',
    date_columns: mergedState.dates.slice(),
    block_key_count: mergedState.block_order.length,
    block_keys: mergedState.block_order.slice(),
    displayed_metric_count: incomingState.metric_keys.length,
    metric_keys: incomingState.metric_keys.slice(),
    source_row_count: incomingState.source_row_count,
    source_metric_key_count: incomingState.metric_keys.length,
    rendered_block_count: mergedState.block_order.length,
    rendered_date_column_count: mergedState.dates.length,
    rendered_metric_row_count: countDataVitrinaMatrixMetricRows_(mergedState),
    rendered_data_row_count: matrix.length - 1,
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

function applySheetVitrinaV1PresentationPass_(spreadsheet) {
  const summaries = [];
  if (typeof applyDataVitrinaPresentation_ === 'function') {
    const dataSheet = spreadsheet.getSheetByName('DATA_VITRINA');
    if (dataSheet && dataSheet.getLastRow() > 0 && dataSheet.getLastColumn() > 0) {
      summaries.push(applyDataVitrinaPresentation_(dataSheet));
    }
  }
  if (typeof applyStatusPresentation_ === 'function') {
    const statusSheet = spreadsheet.getSheetByName('STATUS');
    if (statusSheet && statusSheet.getLastRow() > 0 && statusSheet.getLastColumn() > 0) {
      summaries.push(applyStatusPresentation_(statusSheet));
    }
  }
  return {
    ok: 'success',
    sheets: summaries,
  };
}

function createEmptyDataVitrinaMatrixState_() {
  return {
    dates: [],
    block_order: [],
    block_titles: {},
    block_scope_order: {
      TOTAL: [],
      GROUP: [],
      SKU: [],
      OTHER: [],
    },
    block_metric_order: {},
    metric_titles: {},
    metric_keys: [],
    source_row_count: 0,
    values: {},
  };
}

function readExistingDataVitrinaMatrixState_(sheet) {
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow <= 0 || lastColumn <= 0) {
    return createEmptyDataVitrinaMatrixState_();
  }
  const values = sheet.getRange(1, 1, lastRow, lastColumn).getValues();
  const header = values[0];
  if (isDataVitrinaMatrixHeader_(header)) {
    return parseMatrixDataVitrinaState_(values, resolveSpreadsheetTimeZone_(sheet.getParent()));
  }
  if (isDataVitrinaFlatHeader_(header)) {
    return buildIncomingDataVitrinaMatrixState_(header, values.slice(1));
  }
  return createEmptyDataVitrinaMatrixState_();
}

function buildIncomingDataVitrinaMatrixState_(header, rows) {
  const dateColumns = normalizeDataVitrinaDateColumns_(header);
  const dates = uniqueDataVitrinaDates_(dateColumns);
  const state = createEmptyDataVitrinaMatrixState_();
  state.dates = dates.slice();
  (Array.isArray(rows) ? rows : []).forEach((row) => {
    const key = String(row[1] || '').trim();
    if (!key) {
      return;
    }
    const parsed = parseFlatDataVitrinaKey_(key);
    if (!parsed) {
      return;
    }
    const scope = scopeFromBlockKey_(parsed.block_key);
    const logicalKey = `${parsed.block_key}|${parsed.metric_key}`;
    const derivedTitles = deriveFlatRowTitles_(parsed.block_key, String(row[0] || ''), parsed.metric_key);
    const defaultTitle = buildDefaultBlockTitle_(parsed.block_key);
    const currentTitle = state.block_titles[parsed.block_key] || '';
    if (
      !currentTitle ||
      (currentTitle === defaultTitle && derivedTitles.block_title !== defaultTitle)
    ) {
      state.block_titles[parsed.block_key] = derivedTitles.block_title;
    }
    if (!state.block_metric_order[parsed.block_key]) {
      state.block_metric_order[parsed.block_key] = [];
    }
    pushUnique_(state.block_scope_order[scope] || state.block_scope_order.OTHER, parsed.block_key);
    pushUnique_(state.block_metric_order[parsed.block_key], parsed.metric_key);
    pushUnique_(state.metric_keys, parsed.metric_key);
    state.metric_titles[logicalKey] = derivedTitles.metric_title;
    state.values[logicalKey] = mapRowValuesToDates_(dateColumns, row.slice(2));
    state.source_row_count += 1;
  });
  state.block_order = composeDataVitrinaBlockOrder_(state.block_scope_order);
  return state;
}

function parseMatrixDataVitrinaState_(values, timeZone) {
  const state = createEmptyDataVitrinaMatrixState_();
  const dateColumns = normalizeDataVitrinaDateColumns_(values[0], timeZone);
  state.dates = uniqueDataVitrinaDates_(dateColumns);
  let currentBlockKey = '';
  values.slice(1).forEach((row) => {
    const label = String(row[0] || '').trim();
    const key = String(row[1] || '').trim();
    if (!label && !key) {
      currentBlockKey = '';
      return;
    }
    if (isDataVitrinaBlockKey_(key)) {
      currentBlockKey = key;
      pushUnique_(state.block_scope_order[scopeFromBlockKey_(key)] || state.block_scope_order.OTHER, key);
      state.block_titles[key] = label || buildDefaultBlockTitle_(key);
      if (!state.block_metric_order[key]) {
        state.block_metric_order[key] = [];
      }
      return;
    }
    if (!currentBlockKey || !key) {
      return;
    }
    const logicalKey = `${currentBlockKey}|${key}`;
    pushUnique_(state.block_metric_order[currentBlockKey], key);
    pushUnique_(state.metric_keys, key);
    state.metric_titles[logicalKey] = label || key;
    state.values[logicalKey] = mapRowValuesToDates_(dateColumns, row.slice(2));
    state.source_row_count += 1;
  });
  state.block_order = composeDataVitrinaBlockOrder_(state.block_scope_order);
  return state;
}

function mergeDataVitrinaMatrixStates_(existingState, incomingState) {
  const merged = createEmptyDataVitrinaMatrixState_();
  merged.dates = existingState.dates.slice();
  incomingState.dates.forEach((date) => {
    if (merged.dates.indexOf(date) === -1) {
      merged.dates.push(date);
    }
  });
  merged.block_scope_order = {
    TOTAL: (incomingState.block_scope_order.TOTAL || []).slice(),
    GROUP: (incomingState.block_scope_order.GROUP || []).slice(),
    SKU: (incomingState.block_scope_order.SKU || []).slice(),
    OTHER: (incomingState.block_scope_order.OTHER || []).slice(),
  };
  merged.block_order = composeDataVitrinaBlockOrder_(merged.block_scope_order);
  merged.metric_keys = incomingState.metric_keys.slice();
  merged.source_row_count = incomingState.source_row_count;
  merged.block_order.forEach((blockKey) => {
    merged.block_titles[blockKey] =
      incomingState.block_titles[blockKey] ||
      existingState.block_titles[blockKey] ||
      buildDefaultBlockTitle_(blockKey);
    merged.block_metric_order[blockKey] = (incomingState.block_metric_order[blockKey] || []).slice();
  });
  incomingState.block_order.forEach((blockKey) => {
    (incomingState.block_metric_order[blockKey] || []).forEach((metricKey) => {
      const logicalKey = `${blockKey}|${metricKey}`;
      const existingValues = existingState.values[logicalKey] || {};
      const incomingValues = incomingState.values[logicalKey] || {};
      merged.metric_titles[logicalKey] =
        incomingState.metric_titles[logicalKey] ||
        existingState.metric_titles[logicalKey] ||
        metricKey;
      merged.values[logicalKey] = {};
      merged.dates.forEach((date) => {
        if (Object.prototype.hasOwnProperty.call(incomingValues, date)) {
          const value = incomingValues[date];
          if (value !== '' && value !== null) {
            merged.values[logicalKey][date] = value;
          }
          return;
        }
        if (Object.prototype.hasOwnProperty.call(existingValues, date)) {
          const value = existingValues[date];
          if (value !== '' && value !== null) {
            merged.values[logicalKey][date] = value;
          }
        }
      });
    });
  });
  return merged;
}

function buildDataVitrinaMatrixSheet_(state) {
  const header = DATA_VITRINA_MATRIX_HEADER.concat(state.dates);
  const rows = [];
  state.block_order.forEach((blockKey, index) => {
    rows.push([state.block_titles[blockKey] || buildDefaultBlockTitle_(blockKey), blockKey].concat(buildBlankCells_(state.dates.length)));
    (state.block_metric_order[blockKey] || []).forEach((metricKey) => {
      const logicalKey = `${blockKey}|${metricKey}`;
      const dateValues = state.values[logicalKey] || {};
      rows.push(
        [(state.metric_titles[logicalKey] || metricKey), metricKey].concat(
          state.dates.map((date) => (Object.prototype.hasOwnProperty.call(dateValues, date) ? dateValues[date] : ''))
        )
      );
    });
    if (index < state.block_order.length - 1) {
      rows.push(buildBlankCells_(header.length));
    }
  });
  return [header].concat(rows);
}

function countDataVitrinaMatrixMetricRows_(state) {
  return state.block_order.reduce((total, blockKey) => total + ((state.block_metric_order[blockKey] || []).length), 0);
}

function normalizeDataVitrinaDateHeaders_(header, timeZone) {
  return uniqueDataVitrinaDates_(normalizeDataVitrinaDateColumns_(header, timeZone));
}

function normalizeDataVitrinaDateColumns_(header, timeZone) {
  return (Array.isArray(header) ? header.slice(2) : [])
    .map((item) => normalizeDataVitrinaDateHeader_(item, timeZone))
    .filter((item) => item);
}

function uniqueDataVitrinaDates_(dates) {
  const out = [];
  (Array.isArray(dates) ? dates : []).forEach((item) => {
    if (out.indexOf(item) === -1) {
      out.push(item);
    }
  });
  return out;
}

function normalizeDataVitrinaDateHeader_(value, timeZone) {
  const targetTimeZone = String(timeZone || 'UTC');
  if (value instanceof Date && !Number.isNaN(value.getTime())) {
    return Utilities.formatDate(value, targetTimeZone, 'yyyy-MM-dd');
  }
  const normalized = String(value || '').trim();
  if (!normalized) {
    return '';
  }
  if (/^\d{4}-\d{2}-\d{2}$/.test(normalized)) {
    return normalized;
  }
  const parsed = new Date(normalized);
  if (!Number.isNaN(parsed.getTime())) {
    return Utilities.formatDate(parsed, targetTimeZone, 'yyyy-MM-dd');
  }
  return normalized;
}

function resolveSpreadsheetTimeZone_(spreadsheet) {
  if (spreadsheet && typeof spreadsheet.getSpreadsheetTimeZone === 'function') {
    const value = String(spreadsheet.getSpreadsheetTimeZone() || '').trim();
    if (value) {
      return value;
    }
  }
  if (typeof Session !== 'undefined' && Session && typeof Session.getScriptTimeZone === 'function') {
    const value = String(Session.getScriptTimeZone() || '').trim();
    if (value) {
      return value;
    }
  }
  return 'UTC';
}

function parseFlatDataVitrinaKey_(key) {
  const parts = String(key || '').split('|');
  if (parts.length !== 2) {
    return null;
  }
  const blockKey = String(parts[0] || '').trim();
  const metricKey = String(parts[1] || '').trim();
  if (!blockKey || !metricKey) {
    return null;
  }
  if (!/^TOTAL$|^GROUP:[^|]+$|^SKU:[^|]+$/.test(blockKey)) {
    return null;
  }
  return {
    block_key: blockKey,
    metric_key: metricKey,
  };
}

function deriveFlatRowTitles_(blockKey, label, metricKey) {
  const defaultBlockTitle = buildDefaultBlockTitle_(blockKey);
  const defaultMetricTitle = metricKey;
  const normalizedLabel = String(label || '').trim();
  if (blockKey === 'TOTAL') {
    if (normalizedLabel.startsWith('Итого: ')) {
      return {
        block_title: 'ИТОГО',
        metric_title: normalizedLabel.slice('Итого: '.length).trim() || defaultMetricTitle,
      };
    }
    return {
      block_title: 'ИТОГО',
      metric_title: normalizedLabel || defaultMetricTitle,
    };
  }
  const separatorIndex = normalizedLabel.lastIndexOf(': ');
  if (separatorIndex >= 0) {
    const entityTitle = normalizedLabel.slice(0, separatorIndex).trim();
    const metricTitle = normalizedLabel.slice(separatorIndex + 2).trim() || defaultMetricTitle;
    if (blockKey.startsWith('GROUP:') && entityTitle.startsWith('Группа ')) {
      return {
        block_title: `ГРУППА: ${entityTitle.slice('Группа '.length).trim()}`,
        metric_title: metricTitle,
      };
    }
    return {
      block_title: entityTitle || defaultBlockTitle,
      metric_title: metricTitle,
    };
  }
  return {
    block_title: defaultBlockTitle,
    metric_title: normalizedLabel || defaultMetricTitle,
  };
}

function buildDefaultBlockTitle_(blockKey) {
  if (blockKey === 'TOTAL') {
    return 'ИТОГО';
  }
  if (blockKey.startsWith('GROUP:')) {
    return `ГРУППА: ${blockKey.slice('GROUP:'.length)}`;
  }
  return blockKey;
}

function composeDataVitrinaBlockOrder_(blockScopeOrder) {
  return []
    .concat(blockScopeOrder.TOTAL || [])
    .concat(blockScopeOrder.GROUP || [])
    .concat(blockScopeOrder.SKU || [])
    .concat(blockScopeOrder.OTHER || []);
}

function mapRowValuesToDates_(dates, rowValues) {
  const mapped = {};
  dates.forEach((date, index) => {
    const value = rowValues[index];
    if (value !== '' && value !== null) {
      mapped[date] = value;
    }
  });
  return mapped;
}

function cloneDateValueMap_(values) {
  const out = {};
  Object.keys(values || {}).forEach((date) => {
    out[date] = values[date];
  });
  return out;
}

function buildBlankCells_(count) {
  const values = [];
  for (let index = 0; index < count; index += 1) {
    values.push('');
  }
  return values;
}

function pushUnique_(items, value) {
  if (items.indexOf(value) === -1) {
    items.push(value);
  }
}

function isDataVitrinaBlockKey_(key) {
  return /^TOTAL$|^GROUP:[^|]+$|^SKU:[^|]+$/.test(String(key || '').trim());
}

function scopeFromBlockKey_(blockKey) {
  if (blockKey === 'TOTAL') {
    return 'TOTAL';
  }
  if (String(blockKey).startsWith('GROUP:')) {
    return 'GROUP';
  }
  if (String(blockKey).startsWith('SKU:')) {
    return 'SKU';
  }
  return 'OTHER';
}

function isDataVitrinaMatrixHeader_(header) {
  return Array.isArray(header) && String(header[0] || '').trim() === 'дата' && String(header[1] || '').trim() === 'key';
}

function isDataVitrinaFlatHeader_(header) {
  return Array.isArray(header) && String(header[0] || '').trim() === 'label' && String(header[1] || '').trim() === 'key';
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
    if (isDataVitrinaMatrixHeader_(header)) {
      const normalizedDateHeaders = normalizeDataVitrinaDateHeaders_(header, resolveSpreadsheetTimeZone_(spreadsheet));
      state.header = DATA_VITRINA_MATRIX_HEADER.concat(normalizedDateHeaders);
      const dataRows = values.slice(1);
      const metricKeys = [];
      const blockKeys = [];
      const scopeBlockCounts = {
        TOTAL: 0,
        GROUP: 0,
        SKU: 0,
        OTHER: 0,
      };
      let separatorRowCount = 0;
      let sectionRowCount = 0;
      let metricRowCount = 0;
      let nonEmptyMetricRowCount = 0;
      dataRows.forEach((row) => {
        const key = String(row[1] || '').trim();
        if (!String(row[0] || '').trim() && !key) {
          separatorRowCount += 1;
          return;
        }
        if (isDataVitrinaBlockKey_(key)) {
          sectionRowCount += 1;
          blockKeys.push(key);
          scopeBlockCounts[scopeFromBlockKey_(key)] = (scopeBlockCounts[scopeFromBlockKey_(key)] || 0) + 1;
          return;
        }
        if (!key) {
          return;
        }
        metricKeys.push(key);
        metricRowCount += 1;
        if (row.slice(2).some((cell) => cell !== '' && cell !== null)) {
          nonEmptyMetricRowCount += 1;
        }
      });
      const uniqueMetricKeys = Array.from(new Set(metricKeys));
      state.layout_mode = 'date_matrix';
      state.data_row_count = dataRows.length;
      state.date_column_count = normalizedDateHeaders.length;
      state.date_headers = normalizedDateHeaders;
      state.metric_key_count = uniqueMetricKeys.length;
      state.metric_keys = uniqueMetricKeys;
      state.block_key_count = blockKeys.length;
      state.block_keys = blockKeys;
      state.scope_block_counts = scopeBlockCounts;
      state.section_row_count = sectionRowCount;
      state.metric_row_count = metricRowCount;
      state.separator_row_count = separatorRowCount;
      state.non_empty_metric_row_count = nonEmptyMetricRowCount;
      state.rendered_block_count = blockKeys.length;
      state.rendered_date_column_count = normalizedDateHeaders.length;
      state.rendered_data_row_count = dataRows.length;
      state.rendered_metric_row_count = metricRowCount;
    } else {
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
      state.layout_mode = 'flat_rows';
      state.data_row_count = dataRows.length;
      state.metric_key_count = uniqueMetricKeys.length;
      state.metric_keys = uniqueMetricKeys;
      state.scope_row_counts = scopeRowCounts;
      state.non_empty_value_row_count = nonEmptyValueRowCount;
    }
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
