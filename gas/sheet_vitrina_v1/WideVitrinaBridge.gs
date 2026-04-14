const TARGET_SPREADSHEET_ID = '1ltgE8GltN3Rk8qP1UiaT2NPEwQyPKZ-1tuIqV7EC1NE';
const TARGET_SPREADSHEET_NAME = 'WB Core Vitrina V1';
const EXPECTED_SHEET_NAMES = ['DATA_VITRINA', 'STATUS'];
const DATA_VITRINA_MATRIX_HEADER = ['дата', 'key'];
const DATA_VITRINA_MATRIX_METRICS = [
  { key: 'view_count', title: 'Показы в воронке' },
  { key: 'ctr', title: 'CTR открытия карточки' },
  { key: 'open_card_count', title: 'Открытия карточки' },
  { key: 'views_current', title: 'Показы в поиске' },
  { key: 'ctr_current', title: 'CTR в поиске' },
  { key: 'orders_current', title: 'Заказы в поиске' },
  { key: 'position_avg', title: 'Средняя позиция в поиске' },
];
const DATA_VITRINA_METRIC_TITLE_BY_KEY = DATA_VITRINA_MATRIX_METRICS.reduce((out, item) => {
  out[item.key] = item.title;
  return out;
}, {});
const DATA_VITRINA_TOTAL_SOURCE_KEYS = {
  view_count: 'total_view_count',
  ctr: 'ctr',
  open_card_count: 'total_open_card_count',
  views_current: 'total_views_current',
  ctr_current: 'avg_ctr_current',
  orders_current: 'total_orders_current',
  position_avg: 'avg_position_avg',
};
const DATA_VITRINA_TOTAL_RATIO_FALLBACKS = {
  ctr: {
    numerator: 'total_open_card_count',
    denominator: 'total_view_count',
  },
};

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
  const summary = writeFullOverwriteTarget_(sheet, target);
  if (target.sheet_name === 'DATA_VITRINA') {
    return enrichDataVitrinaWriteSummary_(summary, target);
  }
  return summary;
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
  summary.displayed_metric_count = metricKeys.length;
  summary.metric_keys = metricKeys;
  if (isDataVitrinaMatrixHeader_(header)) {
    summary.layout_mode = 'date_matrix';
    summary.date_columns = header.slice(2);
    return summary;
  }
  if (isDataVitrinaFlatHeader_(header)) {
    summary.layout_mode = 'flat_rows';
    summary.date_columns = header.slice(2);
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
    return writeFullOverwriteTarget_(sheet, target);
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
    displayed_metric_count: DATA_VITRINA_MATRIX_METRICS.length,
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
    return parseMatrixDataVitrinaState_(values);
  }
  if (isDataVitrinaFlatHeader_(header)) {
    return buildIncomingDataVitrinaMatrixState_(header, values.slice(1));
  }
  return createEmptyDataVitrinaMatrixState_();
}

function buildIncomingDataVitrinaMatrixState_(header, rows) {
  const dates = normalizeDataVitrinaDateHeaders_(header);
  const state = createEmptyDataVitrinaMatrixState_();
  state.dates = dates.slice();
  const rawBlocks = {};
  rows.forEach((row) => {
    const key = String(row[1] || '').trim();
    if (!key) {
      return;
    }
    const parsed = parseFlatDataVitrinaKey_(key);
    if (!parsed) {
      return;
    }
    if (!rawBlocks[parsed.block_key]) {
      rawBlocks[parsed.block_key] = {
        raw_metrics: {},
        title: '',
      };
    }
    const derivedTitle = deriveFlatBlockTitle_(parsed.block_key, String(row[0] || ''), parsed.metric_key);
    const defaultTitle = buildDefaultBlockTitle_(parsed.block_key);
    if (
      !rawBlocks[parsed.block_key].title ||
      (rawBlocks[parsed.block_key].title === defaultTitle && derivedTitle !== defaultTitle)
    ) {
      rawBlocks[parsed.block_key].title = derivedTitle;
    }
    rawBlocks[parsed.block_key].raw_metrics[parsed.metric_key] = mapRowValuesToDates_(dates, row.slice(2));
  });

  const blockScopeOrder = {
    TOTAL: [],
    GROUP: [],
    SKU: [],
    OTHER: [],
  };
  Object.keys(rawBlocks).forEach((blockKey) => {
    if (!blockHasSupportedMetricRows_(blockKey, rawBlocks[blockKey].raw_metrics)) {
      return;
    }
    pushUnique_(blockScopeOrder[scopeFromBlockKey_(blockKey)] || blockScopeOrder.OTHER, blockKey);
    state.block_titles[blockKey] = rawBlocks[blockKey].title || buildDefaultBlockTitle_(blockKey);
    DATA_VITRINA_MATRIX_METRICS.forEach((metric) => {
      state.values[`${blockKey}|${metric.key}`] = resolveDisplayedMetricValues_(
        blockKey,
        metric.key,
        rawBlocks[blockKey].raw_metrics,
        dates
      );
    });
  });

  state.block_scope_order = blockScopeOrder;
  state.block_order = composeDataVitrinaBlockOrder_(blockScopeOrder);
  return state;
}

function parseMatrixDataVitrinaState_(values) {
  const state = createEmptyDataVitrinaMatrixState_();
  state.dates = normalizeDataVitrinaDateHeaders_(values[0]);
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
      return;
    }
    if (!currentBlockKey || !DATA_VITRINA_METRIC_TITLE_BY_KEY[key]) {
      return;
    }
    state.values[`${currentBlockKey}|${key}`] = mapRowValuesToDates_(state.dates, row.slice(2));
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
    TOTAL: mergeScopeOrder_(
      incomingState.block_scope_order.TOTAL || [],
      existingState.block_scope_order.TOTAL || []
    ),
    GROUP: mergeScopeOrder_(
      incomingState.block_scope_order.GROUP || [],
      existingState.block_scope_order.GROUP || []
    ),
    SKU: mergeScopeOrder_(
      incomingState.block_scope_order.SKU || [],
      existingState.block_scope_order.SKU || []
    ),
    OTHER: mergeScopeOrder_(
      incomingState.block_scope_order.OTHER || [],
      existingState.block_scope_order.OTHER || []
    ),
  };
  merged.block_order = composeDataVitrinaBlockOrder_(merged.block_scope_order);
  merged.block_order.forEach((blockKey) => {
    merged.block_titles[blockKey] =
      incomingState.block_titles[blockKey] ||
      existingState.block_titles[blockKey] ||
      buildDefaultBlockTitle_(blockKey);
  });

  Object.keys(existingState.values).forEach((logicalKey) => {
    merged.values[logicalKey] = cloneDateValueMap_(existingState.values[logicalKey]);
  });
  incomingState.dates.forEach((date) => {
    Object.keys(merged.values).forEach((logicalKey) => {
      delete merged.values[logicalKey][date];
    });
  });
  incomingState.block_order.forEach((blockKey) => {
    DATA_VITRINA_MATRIX_METRICS.forEach((metric) => {
      const logicalKey = `${blockKey}|${metric.key}`;
      if (!merged.values[logicalKey]) {
        merged.values[logicalKey] = {};
      }
      const incomingValues = incomingState.values[logicalKey] || {};
      incomingState.dates.forEach((date) => {
        if (Object.prototype.hasOwnProperty.call(incomingValues, date)) {
          const value = incomingValues[date];
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
    DATA_VITRINA_MATRIX_METRICS.forEach((metric) => {
      const logicalKey = `${blockKey}|${metric.key}`;
      const dateValues = state.values[logicalKey] || {};
      rows.push(
        [metric.title, metric.key].concat(
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

function normalizeDataVitrinaDateHeaders_(header) {
  return (Array.isArray(header) ? header.slice(2) : [])
    .map((item) => String(item || '').trim())
    .filter((item) => item);
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

function blockHasSupportedMetricRows_(blockKey, rawMetrics) {
  return DATA_VITRINA_MATRIX_METRICS.some((metric) => {
    if (blockKey === 'TOTAL') {
      const directKey = DATA_VITRINA_TOTAL_SOURCE_KEYS[metric.key];
      const ratio = DATA_VITRINA_TOTAL_RATIO_FALLBACKS[metric.key];
      return Boolean(
        rawMetrics[directKey] ||
        (ratio && rawMetrics[ratio.numerator] && rawMetrics[ratio.denominator]) ||
        rawMetrics[metric.key]
      );
    }
    return Boolean(rawMetrics[metric.key]);
  });
}

function resolveDisplayedMetricValues_(blockKey, metricKey, rawMetrics, dates) {
  if (blockKey === 'TOTAL') {
    return resolveTotalDisplayedMetricValues_(metricKey, rawMetrics, dates);
  }
  return cloneDateValueMap_(rawMetrics[metricKey] || {});
}

function resolveTotalDisplayedMetricValues_(metricKey, rawMetrics, dates) {
  const directKey = DATA_VITRINA_TOTAL_SOURCE_KEYS[metricKey];
  if (directKey && rawMetrics[directKey]) {
    return cloneDateValueMap_(rawMetrics[directKey]);
  }
  if (rawMetrics[metricKey]) {
    return cloneDateValueMap_(rawMetrics[metricKey]);
  }
  const ratio = DATA_VITRINA_TOTAL_RATIO_FALLBACKS[metricKey];
  if (!ratio || !rawMetrics[ratio.numerator] || !rawMetrics[ratio.denominator]) {
    return {};
  }
  const result = {};
  dates.forEach((date) => {
    const numerator = rawMetrics[ratio.numerator][date];
    const denominator = rawMetrics[ratio.denominator][date];
    if (typeof numerator === 'number' && typeof denominator === 'number' && denominator !== 0) {
      result[date] = numerator / denominator;
    }
  });
  return result;
}

function deriveFlatBlockTitle_(blockKey, label, metricKey) {
  if (blockKey === 'TOTAL') {
    return 'ИТОГО';
  }
  const metricTitle = DATA_VITRINA_METRIC_TITLE_BY_KEY[metricKey];
  if (!metricTitle) {
    return buildDefaultBlockTitle_(blockKey);
  }
  const suffix = `: ${metricTitle}`;
  if (label.endsWith(suffix)) {
    const entityTitle = label.slice(0, label.length - suffix.length).trim();
    if (blockKey.startsWith('GROUP:') && entityTitle.startsWith('Группа ')) {
      return `ГРУППА: ${entityTitle.slice('Группа '.length).trim()}`;
    }
    return entityTitle || buildDefaultBlockTitle_(blockKey);
  }
  return buildDefaultBlockTitle_(blockKey);
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

function mergeScopeOrder_(primary, secondary) {
  const merged = [];
  (primary || []).forEach((item) => pushUnique_(merged, item));
  (secondary || []).forEach((item) => pushUnique_(merged, item));
  return merged;
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
        if (DATA_VITRINA_METRIC_TITLE_BY_KEY[key]) {
          metricKeys.push(key);
          metricRowCount += 1;
          if (row.slice(2).some((cell) => cell !== '' && cell !== null)) {
            nonEmptyMetricRowCount += 1;
          }
        }
      });
      state.layout_mode = 'date_matrix';
      state.data_row_count = dataRows.length;
      state.date_column_count = Math.max(header.length - 2, 0);
      state.date_headers = header.slice(2);
      state.metric_key_count = Array.from(new Set(metricKeys)).length;
      state.metric_keys = Array.from(new Set(metricKeys));
      state.block_key_count = blockKeys.length;
      state.block_keys = blockKeys;
      state.scope_block_counts = scopeBlockCounts;
      state.section_row_count = sectionRowCount;
      state.metric_row_count = metricRowCount;
      state.separator_row_count = separatorRowCount;
      state.non_empty_metric_row_count = nonEmptyMetricRowCount;
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
