#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { execFileSync } = require('child_process');

function main() {
  const options = parseArgs(process.argv.slice(2));
  const spreadsheet = new MockSpreadsheet(
    '1ltgE8GltN3Rk8qP1UiaT2NPEwQyPKZ-1tuIqV7EC1NE',
    'WB Core Vitrina V1'
  );
  const context = buildContext({ spreadsheet });
  const scriptPath = path.resolve(options.scriptPath);
  const scriptDir = path.dirname(scriptPath);
  const scriptFiles = fs
    .readdirSync(scriptDir)
    .filter((name) => name.endsWith('.gs'))
    .sort();
  for (const fileName of scriptFiles) {
    const filePath = path.join(scriptDir, fileName);
    const source = fs.readFileSync(filePath, 'utf8');
    vm.runInNewContext(source, context, { filename: filePath });
  }

  const result =
    options.mode === 'seed_bootstrap'
      ? runSeedBootstrapMode({ context, spreadsheet, options })
      : options.mode === 'mvp_end_to_end'
        ? runMvpEndToEndMode({ context, spreadsheet, options })
      : options.mode === 'cost_price_upload'
        ? runCostPriceUploadMode({ context, spreadsheet, options })
      : options.mode === 'load_only'
        ? runLoadOnlyMode({ context, spreadsheet, options })
      : options.mode === 'server_driven_materialization'
        ? runServerDrivenMaterializationMode({ context, spreadsheet })
      : runBundleUploadMode({ context, spreadsheet, options });
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}

function parseArgs(argv) {
  const options = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key || !key.startsWith('--') || value === undefined) {
      throw new Error(`unexpected arguments: ${argv.join(' ')}`);
    }
    options[key.slice(2)] = value;
  }
  options.mode = options.mode || 'bundle_upload';
  const requiredKeys = ['scriptPath'];
  if (!['server_driven_materialization', 'load_only', 'cost_price_upload'].includes(options.mode)) {
    requiredKeys.push('endpointUrl', 'bundleVersion', 'uploadedAt');
  }
  if (options.mode === 'bundle_upload') {
    requiredKeys.push('fixturePath');
  }
  if (options.mode === 'cost_price_upload') {
    requiredKeys.push('fixturePath', 'endpointUrl', 'datasetVersion', 'uploadedAt');
  }
  if (options.mode === 'mvp_end_to_end') {
    requiredKeys.push('asOfDate', 'refreshUrl');
  }
  if (options.mode === 'load_only') {
    requiredKeys.push('endpointUrl');
  }
  for (const required of requiredKeys) {
    if (!options[required]) {
      throw new Error(`missing required argument --${required}`);
    }
  }
  return options;
}

function runBundleUploadMode({ context, spreadsheet, options }) {
  const ensureResult = parseJsonString(context.ensureRegistryUploadOperatorSheets());
  const bundleFixture = JSON.parse(fs.readFileSync(path.resolve(options.fixturePath), 'utf8'));
  parseJsonString(context.debugWriteRegistryUploadBundleToSheets(JSON.stringify(bundleFixture), ''));
  const builtBundle = parseJsonString(
    context.debugBuildRegistryUploadBundleFromSheets(options.bundleVersion, options.uploadedAt)
  );
  const acceptedResponse = parseJsonString(
    context.debugUploadRegistryUploadBundleFromSheets(options.endpointUrl, options.bundleVersion, options.uploadedAt)
  );
  const duplicateResponse = parseJsonString(
    context.debugUploadRegistryUploadBundleFromSheets(options.endpointUrl, options.bundleVersion, options.uploadedAt)
  );

  const configSheet = spreadsheet.getSheetByName('CONFIG');
  const statusBlock = readStatusBlock(configSheet);

  parseJsonString(context.debugResetRegistryUploadOperatorSheets());

  return {
    ensure_result: ensureResult,
    built_bundle: builtBundle,
    accepted_response: acceptedResponse,
    duplicate_response: duplicateResponse,
    status_block: statusBlock,
  };
}

function runSeedBootstrapMode({ context, spreadsheet, options }) {
  const prepareResult = parseJsonString(context.prepareRegistryUploadOperatorSheets());
  const configSheet = spreadsheet.getSheetByName('CONFIG');
  writeStatusBlock(configSheet, {
    endpoint_url: options.endpointUrl,
    last_bundle_version: 'preserved_bundle_version',
    last_status: 'accepted',
    last_activated_at: '2026-04-13T12:09:59Z',
    last_http_status: '200',
    last_validation_errors: '',
  });
  const prepareResultAfterReprepare = parseJsonString(context.prepareRegistryUploadOperatorSheets());
  const preservedControlBlock = readStatusBlock(configSheet);
  const builtBundle = parseJsonString(
    context.debugBuildRegistryUploadBundleFromSheets(options.bundleVersion, options.uploadedAt)
  );
  const acceptedResponse = parseJsonString(
    context.debugUploadRegistryUploadBundleFromSheets('', options.bundleVersion, options.uploadedAt)
  );
  const statusBlock = readStatusBlock(configSheet);

  parseJsonString(context.debugResetRegistryUploadOperatorSheets());

  return {
    prepare_result: prepareResult,
    prepare_result_after_reprepare: prepareResultAfterReprepare,
    preserved_control_block: preservedControlBlock,
    built_bundle: builtBundle,
    accepted_response: acceptedResponse,
    status_block: statusBlock,
  };
}

function runCostPriceUploadMode({ context, spreadsheet, options }) {
  const prepareResult = parseJsonString(context.prepareCostPriceSheet());
  const fixture = JSON.parse(fs.readFileSync(path.resolve(options.fixturePath), 'utf8'));
  parseJsonString(context.debugWriteCostPriceRowsToSheet(JSON.stringify(fixture), options.endpointUrl));
  const builtPayload = parseJsonString(
    context.debugBuildCostPriceUploadFromSheet(options.datasetVersion, options.uploadedAt)
  );
  const acceptedResponse = parseJsonString(
    context.debugUploadCostPriceSheet(options.endpointUrl, options.datasetVersion, options.uploadedAt)
  );
  const duplicateResponse = parseJsonString(
    context.debugUploadCostPriceSheet(options.endpointUrl, options.datasetVersion, options.uploadedAt)
  );

  const costPriceSheet = spreadsheet.getSheetByName('COST_PRICE');
  const statusBlock = readCostPriceStatusBlock(costPriceSheet);

  parseJsonString(context.debugResetCostPriceSheet());

  return {
    prepare_result: prepareResult,
    built_payload: builtPayload,
    accepted_response: acceptedResponse,
    duplicate_response: duplicateResponse,
    status_block: statusBlock,
    sheet: snapshotSheet(costPriceSheet),
  };
}

function runMvpEndToEndMode({ context, spreadsheet, options }) {
  const prepareResult = parseJsonString(context.prepareRegistryUploadOperatorSheets());
  const builtBundle = parseJsonString(
    context.debugBuildRegistryUploadBundleFromSheets(options.bundleVersion, options.uploadedAt)
  );
  const acceptedResponse = parseJsonString(
    context.debugUploadRegistryUploadBundleFromSheets(options.endpointUrl, options.bundleVersion, options.uploadedAt)
  );
  const refreshResponse = fetchJson(options.refreshUrl, {
    method: 'POST',
    payload: JSON.stringify({ as_of_date: options.asOfDate }),
  });
  const loadResult = parseJsonString(
    context.debugLoadSheetVitrinaTable(options.endpointUrl, options.asOfDate)
  );
  const sheetState = parseJsonString(context.getSheetVitrinaV1State());
  const configSheet = spreadsheet.getSheetByName('CONFIG');
  const statusBlock = readStatusBlock(configSheet);
  return {
    prepare_result: prepareResult,
    built_bundle: builtBundle,
    accepted_response: acceptedResponse,
    refresh_response: refreshResponse,
    load_result: loadResult,
    sheet_state: sheetState,
    presentation_snapshot: parseJsonString(context.getSheetVitrinaV1PresentationSnapshot()),
    status_block: statusBlock,
    sheets: {
      CONFIG: snapshotSheet(spreadsheet.getSheetByName('CONFIG')),
      METRICS: snapshotSheet(spreadsheet.getSheetByName('METRICS')),
      FORMULAS: snapshotSheet(spreadsheet.getSheetByName('FORMULAS')),
      DATA_VITRINA: snapshotSheet(spreadsheet.getSheetByName('DATA_VITRINA')),
      STATUS: snapshotSheet(spreadsheet.getSheetByName('STATUS')),
    },
  };
}

function runLoadOnlyMode({ context, spreadsheet, options }) {
  const ensureResult = parseJsonString(context.ensureRegistryUploadOperatorSheets());
  const configSheet = spreadsheet.getSheetByName('CONFIG');
  writeStatusBlock(configSheet, {
    endpoint_url: options.endpointUrl,
    last_bundle_version: '',
    last_status: '',
    last_activated_at: '',
    last_http_status: '',
    last_validation_errors: '',
  });

  let loadResult = null;
  let loadError = '';
  try {
    loadResult = parseJsonString(context.debugLoadSheetVitrinaTable('', options.asOfDate || ''));
  } catch (error) {
    loadError = String(error);
  }

  return {
    ensure_result: ensureResult,
    load_result: loadResult,
    load_error: loadError,
    status_block: readStatusBlock(configSheet),
    sheets: {
      DATA_VITRINA: snapshotSheet(spreadsheet.getSheetByName('DATA_VITRINA')),
      STATUS: snapshotSheet(spreadsheet.getSheetByName('STATUS')),
    },
  };
}

function runServerDrivenMaterializationMode({ context, spreadsheet }) {
  const dayOnePlan = buildSyntheticSheetVitrinaPlan('2026-04-12', 0);
  const dayOneOverwritePlan = buildSyntheticSheetVitrinaPlan('2026-04-12', 500);
  const dayTwoPlan = buildSyntheticSheetVitrinaPlan('2026-04-13', 1000);

  const firstLoad = parseJsonString(context.writeSheetVitrinaV1Plan(JSON.stringify(dayOnePlan)));
  const firstState = parseJsonString(context.getSheetVitrinaV1State());
  const firstPresentation = parseJsonString(context.getSheetVitrinaV1PresentationSnapshot());
  const firstSnapshot = snapshotSheet(spreadsheet.getSheetByName('DATA_VITRINA'));

  const sameDayOverwrite = parseJsonString(context.writeSheetVitrinaV1Plan(JSON.stringify(dayOneOverwritePlan)));
  const sameDayState = parseJsonString(context.getSheetVitrinaV1State());
  const sameDayPresentation = parseJsonString(context.getSheetVitrinaV1PresentationSnapshot());
  const sameDaySnapshot = snapshotSheet(spreadsheet.getSheetByName('DATA_VITRINA'));

  const nextDayOverwrite = parseJsonString(context.writeSheetVitrinaV1Plan(JSON.stringify(dayTwoPlan)));
  const nextDayState = parseJsonString(context.getSheetVitrinaV1State());
  const nextDayPresentation = parseJsonString(context.getSheetVitrinaV1PresentationSnapshot());
  const nextDaySnapshot = snapshotSheet(spreadsheet.getSheetByName('DATA_VITRINA'));

  return {
    first_load: firstLoad,
    same_day_overwrite: sameDayOverwrite,
    next_day_overwrite: nextDayOverwrite,
    snapshots: {
      after_first_load: firstSnapshot,
      after_same_day_overwrite: sameDaySnapshot,
      after_next_day_overwrite: nextDaySnapshot,
    },
    states: {
      first_load: firstState,
      same_day_overwrite: sameDayState,
      next_day_overwrite: nextDayState,
    },
    presentations: {
      first_load: firstPresentation,
      same_day_overwrite: sameDayPresentation,
      next_day_overwrite: nextDayPresentation,
    },
  };
}

function parseJsonString(value) {
  return typeof value === 'string' ? JSON.parse(value) : value;
}

function readStatusBlock(configSheet) {
  return {
    endpoint_url: String(configSheet.getCellValue(2, 9) || ''),
    last_bundle_version: String(configSheet.getCellValue(3, 9) || ''),
    last_status: String(configSheet.getCellValue(4, 9) || ''),
    last_activated_at: String(configSheet.getCellValue(5, 9) || ''),
    last_http_status: String(configSheet.getCellValue(6, 9) || ''),
    last_validation_errors: String(configSheet.getCellValue(7, 9) || ''),
  };
}

function writeStatusBlock(configSheet, values) {
  configSheet.getRange(2, 9).setValue(values.endpoint_url || '');
  configSheet.getRange(3, 9).setValue(values.last_bundle_version || '');
  configSheet.getRange(4, 9).setValue(values.last_status || '');
  configSheet.getRange(5, 9).setValue(values.last_activated_at || '');
  configSheet.getRange(6, 9).setValue(values.last_http_status || '');
  configSheet.getRange(7, 9).setValue(values.last_validation_errors || '');
}

function readCostPriceStatusBlock(costPriceSheet) {
  return {
    endpoint_url: String(costPriceSheet.getCellValue(2, 6) || ''),
    last_dataset_version: String(costPriceSheet.getCellValue(3, 6) || ''),
    last_status: String(costPriceSheet.getCellValue(4, 6) || ''),
    last_activated_at: String(costPriceSheet.getCellValue(5, 6) || ''),
    last_http_status: String(costPriceSheet.getCellValue(6, 6) || ''),
    last_validation_errors: String(costPriceSheet.getCellValue(7, 6) || ''),
  };
}

function writePlanDirectlyToSheet(spreadsheet, target) {
  const sheet = spreadsheet.getSheetByName(target.sheet_name) || spreadsheet.insertSheet(target.sheet_name);
  const matrix = [target.header].concat(target.rows || []);
  sheet.getRange(target.clear_range).clearContent();
  sheet.getRange(target.write_start_cell).offset(0, 0, matrix.length, matrix[0].length).setValues(matrix);
}

function buildSyntheticSheetVitrinaPlan(asOfDate, offset) {
  const supportedMetrics = [
    ['view_count', 'Показы в воронке'],
    ['ctr', 'CTR открытия карточки'],
    ['open_card_count', 'Открытия карточки'],
    ['views_current', 'Показы в поиске'],
    ['ctr_current', 'CTR в поиске'],
    ['orders_current', 'Заказы в поиске'],
    ['position_avg', 'Средняя позиция в поиске'],
    ['proxy_profit_rub', 'Прокси-прибыль, ₽'],
    ['inventory_value_retail_rub', 'Розничная стоимость остатков, ₽'],
    ['localization_percent', 'Локализация, %'],
  ];
  const totalRows = [
    ['Итого: Маржинальность прокси, %', 'TOTAL|proxy_margin_pct_total', Number((0.2 + offset / 10000).toFixed(4))],
    ['Итого: Прокси-прибыль, ₽', 'TOTAL|total_proxy_profit_rub', 10000 + offset],
    ['Итого: Показы в воронке', 'TOTAL|total_view_count', 1000 + offset],
    ['Итого: Открытия карточки', 'TOTAL|total_open_card_count', 250 + offset],
    ['Итого: Показы в поиске всего', 'TOTAL|total_views_current', 850 + offset],
    ['Итого: CTR в поиске средний', 'TOTAL|avg_ctr_current', 0.23 + offset / 10000],
    ['Итого: Заказы в поиске всего', 'TOTAL|total_orders_current', 75 + offset],
    ['Итого: Средняя позиция в поиске средняя', 'TOTAL|avg_position_avg', 4.25 + offset / 1000],
    ['Итого: Розничная стоимость остатков, ₽', 'TOTAL|inventory_value_retail_rub_total', 15000 + offset],
  ];
  const groups = [
    { key: 'GROUP:Clean', label: 'Clean', base: 200 + offset },
    { key: 'GROUP:Anti-Spy', label: 'Anti-Spy', base: 120 + offset },
  ];
  const skus = [
    { key: 'SKU:210183919', label: 'clean iPhone 14', base: 110 + offset },
    { key: 'SKU:210183920', label: 'clean iPhone 15', base: 80 + offset },
  ];

  const rows = totalRows.slice();
  groups.forEach((group) => {
    supportedMetrics.forEach(([metricKey, title], index) => {
        rows.push([
          `Группа ${group.label}: ${title}`,
          `${group.key}|${metricKey}`,
          syntheticMetricValue(metricKey, group.base + index),
        ]);
    });
  });
  skus.forEach((sku) => {
    supportedMetrics.forEach(([metricKey, title], index) => {
      rows.push([
        `${sku.label}: ${title}`,
        `${sku.key}|${metricKey}`,
        syntheticMetricValue(metricKey, sku.base + index),
      ]);
    });
    rows.push([`${sku.label}: Остаток`, `${sku.key}|stock_total`, 500 + sku.base]);
  });

  return {
    plan_version: 'sheet_vitrina_v1_compact_live_v2__sheet_scaffold_v1',
    snapshot_id: `${asOfDate}__sheet_vitrina_v1_compact_live_v2__synthetic`,
    as_of_date: asOfDate,
    sheets: [
      {
        sheet_name: 'DATA_VITRINA',
        write_start_cell: 'A1',
        write_rect: `A1:C${rows.length + 1}`,
        clear_range: 'A:ZZ',
        write_mode: 'full_overwrite',
        partial_update_allowed: false,
        header: ['label', 'key', asOfDate],
        rows: rows,
        row_count: rows.length,
        column_count: 3,
      },
      {
        sheet_name: 'STATUS',
        write_start_cell: 'A1',
        write_rect: 'A1:K3',
        clear_range: 'A:K',
        write_mode: 'full_overwrite',
        partial_update_allowed: false,
        header: [
          'source_key',
          'kind',
          'freshness',
          'snapshot_date',
          'date',
          'date_from',
          'date_to',
          'requested_count',
          'covered_count',
          'missing_nm_ids',
          'note',
        ],
        rows: [
          ['registry_upload_current_state', 'success', asOfDate, asOfDate, '', '', '', 2, 2, '', 'synthetic'],
          ['sheet_vitrina_v1_compact_live_v2', 'success', asOfDate, asOfDate, '', '', '', supportedMetrics.length + 2, supportedMetrics.length + 2, '', 'synthetic'],
        ],
        row_count: 2,
        column_count: 11,
      },
    ],
  };
}

function syntheticMetricValue(metricKey, base) {
  if (metricKey === 'ctr' || metricKey === 'ctr_current' || metricKey === 'localization_percent') {
    return Number((0.1 + base / 1000).toFixed(4));
  }
  if (metricKey === 'position_avg') {
    return Number((2 + base / 100).toFixed(2));
  }
  if (metricKey.endsWith('_rub')) {
    return Number((100 + base / 10).toFixed(2));
  }
  return Math.round(base);
}

function buildContext({ spreadsheet }) {
  const menu = {
    addItem() {
      return this;
    },
    addToUi() {
      return this;
    },
  };
  const ui = {
    createMenu() {
      return menu;
    },
    alert() {
      return null;
    },
  };

  return {
    console,
    JSON,
    String,
    Number,
    Boolean,
    Date,
    URL,
    Math,
    RegExp,
    Array,
    Object,
    Logger: {
      log() {
        return null;
      },
    },
    SpreadsheetApp: {
      openById(id) {
        if (id !== spreadsheet.getId()) {
          throw new Error(`unexpected spreadsheet id: ${id}`);
        }
        return spreadsheet;
      },
      flush() {
        return null;
      },
      getUi() {
        return ui;
      },
    },
    UrlFetchApp: {
      fetch(url, options) {
        return curlFetch(url, options);
      },
    },
  };
}

function curlFetch(url, options) {
  const args = [
    '-sS',
    '-X',
    String(options.method || 'get').toUpperCase(),
    '-H',
    `Content-Type: ${options.contentType || 'application/json'}`,
    '--data',
    options.payload || '',
    '-w',
    '\n%{http_code}',
    url,
  ];
  let output;
  try {
    output = execFileSync('curl', args, { encoding: 'utf8' });
  } catch (error) {
    throw new Error(error.stderr || error.message);
  }
  const lines = output.trimEnd().split('\n');
  const status = Number(lines.pop());
  const body = lines.join('\n');
  return {
    getResponseCode() {
      return status;
    },
    getContentText() {
      return body;
    },
  };
}

function fetchJson(url, options) {
  const response = curlFetch(url, options || {});
  const bodyText = response.getContentText();
  let payload;
  try {
    payload = JSON.parse(bodyText);
  } catch (error) {
    throw new Error(`endpoint returned non-JSON response: ${bodyText}`);
  }
  if (response.getResponseCode() >= 400) {
    throw new Error(payload.error || `endpoint failed with HTTP ${response.getResponseCode()}`);
  }
  return payload;
}

class MockSpreadsheet {
  constructor(id, name) {
    this.id = id;
    this.name = name;
    this.toastLog = [];
    this.sheets = new Map();
  }

  getId() {
    return this.id;
  }

  getName() {
    return this.name;
  }

  getSheetByName(name) {
    return this.sheets.get(name) || null;
  }

  getSheets() {
    return Array.from(this.sheets.values());
  }

  insertSheet(name) {
    const sheet = new MockSheet(name);
    this.sheets.set(name, sheet);
    return sheet;
  }

  toast(message, title, timeoutSeconds) {
    this.toastLog.push({ message, title, timeoutSeconds });
  }
}

class MockSheet {
  constructor(name) {
    this.name = name;
    this.values = new Map();
    this.notes = new Map();
    this.backgrounds = new Map();
    this.fontColors = new Map();
    this.fontWeights = new Map();
    this.horizontalAlignments = new Map();
    this.verticalAlignments = new Map();
    this.numberFormats = new Map();
    this.maxRows = 5000;
    this.maxColumns = 702;
    this.frozenRows = 0;
    this.frozenColumns = 0;
    this.columnWidths = new Map();
  }

  getName() {
    return this.name;
  }

  getRange(row, column, numRows = 1, numColumns = 1) {
    if (typeof row === 'string') {
      const range = parseA1Range(row, this.maxRows, this.maxColumns);
      return new MockRange(this, range.row, range.column, range.numRows, range.numColumns);
    }
    return new MockRange(this, row, column, numRows, numColumns);
  }

  getLastRow() {
    let max = 0;
    for (const key of this.values.keys()) {
      const [row] = key.split(':').map(Number);
      const value = this.values.get(key);
      if (value !== '' && value !== null && value !== undefined) {
        max = Math.max(max, row);
      }
    }
    return max;
  }

  getLastColumn() {
    let max = 0;
    for (const key of this.values.keys()) {
      const [, column] = key.split(':').map(Number);
      const value = this.values.get(key);
      if (value !== '' && value !== null && value !== undefined) {
        max = Math.max(max, column);
      }
    }
    return max;
  }

  getMaxRows() {
    return this.maxRows;
  }

  setFrozenRows(count) {
    this.frozenRows = count;
    return this;
  }

  getFrozenRows() {
    return this.frozenRows;
  }

  setFrozenColumns(count) {
    this.frozenColumns = count;
    return this;
  }

  getFrozenColumns() {
    return this.frozenColumns;
  }

  setColumnWidth(column, width) {
    this.columnWidths.set(column, width);
    return this;
  }

  setColumnWidths(startColumn, numColumns, width) {
    for (let offset = 0; offset < numColumns; offset += 1) {
      this.columnWidths.set(startColumn + offset, width);
    }
    return this;
  }

  getColumnWidth(column) {
    return this.columnWidths.get(column) || 100;
  }

  showRows() {
    return this;
  }

  getCellValue(row, column) {
    const key = `${row}:${column}`;
    return this.values.has(key) ? this.values.get(key) : '';
  }
}

class MockRange {
  constructor(sheet, row, column, numRows, numColumns) {
    this.sheet = sheet;
    this.row = row;
    this.column = column;
    this.numRows = numRows;
    this.numColumns = numColumns;
  }

  setValues(values) {
    for (let rowOffset = 0; rowOffset < this.numRows; rowOffset += 1) {
      for (let columnOffset = 0; columnOffset < this.numColumns; columnOffset += 1) {
        this.sheet.values.set(
          `${this.row + rowOffset}:${this.column + columnOffset}`,
          values[rowOffset][columnOffset]
        );
      }
    }
    return this;
  }

  getValues() {
    const out = [];
    for (let rowOffset = 0; rowOffset < this.numRows; rowOffset += 1) {
      const row = [];
      for (let columnOffset = 0; columnOffset < this.numColumns; columnOffset += 1) {
        const key = `${this.row + rowOffset}:${this.column + columnOffset}`;
        row.push(this.sheet.values.has(key) ? this.sheet.values.get(key) : '');
      }
      out.push(row);
    }
    return out;
  }

  getValue() {
    const key = `${this.row}:${this.column}`;
    return this.sheet.values.has(key) ? this.sheet.values.get(key) : '';
  }

  setValue(value) {
    this.sheet.values.set(`${this.row}:${this.column}`, value);
    return this;
  }

  clearContent() {
    for (const key of Array.from(this.sheet.values.keys())) {
      const [row, column] = key.split(':').map(Number);
      if (
        row >= this.row &&
        row < this.row + this.numRows &&
        column >= this.column &&
        column < this.column + this.numColumns
      ) {
        this.sheet.values.set(key, '');
      }
    }
    return this;
  }

  setFontWeight(value) {
    return this._setStyle(this.sheet.fontWeights, value);
  }

  setBackground(value) {
    return this._setStyle(this.sheet.backgrounds, value);
  }

  setFontColor(value) {
    return this._setStyle(this.sheet.fontColors, value);
  }

  setHorizontalAlignment(value) {
    return this._setStyle(this.sheet.horizontalAlignments, value);
  }

  setVerticalAlignment(value) {
    return this._setStyle(this.sheet.verticalAlignments, value);
  }

  setNumberFormat(value) {
    return this._setStyle(this.sheet.numberFormats, value);
  }

  getDisplayValues() {
    return this.getValues().map((row) =>
      row.map((value) =>
        value === null || value === undefined ? '' : typeof value === 'string' ? value : String(value)
      )
    );
  }

  getBackground() {
    return this._getStyle(this.sheet.backgrounds, '#ffffff');
  }

  getFontColor() {
    return this._getStyle(this.sheet.fontColors, '#000000');
  }

  getFontWeight() {
    return this._getStyle(this.sheet.fontWeights, 'normal');
  }

  getHorizontalAlignment() {
    return this._getStyle(this.sheet.horizontalAlignments, '');
  }

  getVerticalAlignment() {
    return this._getStyle(this.sheet.verticalAlignments, '');
  }

  getNumberFormat() {
    return this._getStyle(this.sheet.numberFormats, '');
  }

  getNote() {
    return this.sheet.notes.get(`${this.row}:${this.column}`) || '';
  }

  _setStyle(store, value) {
    for (let rowOffset = 0; rowOffset < this.numRows; rowOffset += 1) {
      for (let columnOffset = 0; columnOffset < this.numColumns; columnOffset += 1) {
        store.set(`${this.row + rowOffset}:${this.column + columnOffset}`, value);
      }
    }
    return this;
  }

  _getStyle(store, fallback) {
    const key = `${this.row}:${this.column}`;
    return store.has(key) ? store.get(key) : fallback;
  }

  setNote(note) {
    this.sheet.notes.set(`${this.row}:${this.column}`, note);
    return this;
  }

  offset(rowOffset, columnOffset, numRows = this.numRows, numColumns = this.numColumns) {
    return new MockRange(
      this.sheet,
      this.row + rowOffset,
      this.column + columnOffset,
      numRows,
      numColumns
    );
  }

  getA1Notation() {
    const start = `${columnName(this.column)}${this.row}`;
    const end = `${columnName(this.column + this.numColumns - 1)}${this.row + this.numRows - 1}`;
    return start === end ? start : `${start}:${end}`;
  }
}

function snapshotSheet(sheet) {
  if (!sheet) {
    return null;
  }
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  return {
    name: sheet.getName(),
    last_row: lastRow,
    last_column: lastColumn,
    values: lastRow > 0 && lastColumn > 0 ? sheet.getRange(1, 1, lastRow, lastColumn).getValues() : [],
  };
}

function parseA1Range(value, maxRows, maxColumns) {
  const normalized = String(value).trim().toUpperCase();
  const parts = normalized.split(':');
  if (parts.length === 1) {
    const single = parseA1Cell(parts[0]);
    return { row: single.row, column: single.column, numRows: 1, numColumns: 1 };
  }
  if (parts.length !== 2) {
    throw new Error(`unsupported A1 notation: ${value}`);
  }
  const left = parts[0];
  const right = parts[1];
  if (/^[A-Z]+$/.test(left) && /^[A-Z]+$/.test(right)) {
    const startColumn = columnNumber(left);
    const endColumn = columnNumber(right);
    return {
      row: 1,
      column: startColumn,
      numRows: maxRows,
      numColumns: Math.min(maxColumns, endColumn) - startColumn + 1,
    };
  }
  const start = parseA1Cell(left);
  const end = parseA1Cell(right);
  return {
    row: start.row,
    column: start.column,
    numRows: end.row - start.row + 1,
    numColumns: end.column - start.column + 1,
  };
}

function parseA1Cell(value) {
  const match = /^([A-Z]+)(\d+)$/.exec(String(value).trim().toUpperCase());
  if (!match) {
    throw new Error(`unsupported A1 cell: ${value}`);
  }
  return { column: columnNumber(match[1]), row: Number(match[2]) };
}

function columnNumber(label) {
  let current = 0;
  for (const ch of String(label)) {
    current = current * 26 + (ch.charCodeAt(0) - 64);
  }
  return current;
}

function columnName(index) {
  let out = '';
  let current = index;
  while (current > 0) {
    const remainder = (current - 1) % 26;
    out = String.fromCharCode(65 + remainder) + out;
    current = Math.floor((current - 1) / 26);
  }
  return out;
}

main();
