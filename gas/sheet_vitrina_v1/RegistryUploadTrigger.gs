const REGISTRY_UPLOAD_TARGET_SPREADSHEET_ID = '1ltgE8GltN3Rk8qP1UiaT2NPEwQyPKZ-1tuIqV7EC1NE';
const REGISTRY_UPLOAD_TARGET_SPREADSHEET_NAME = 'WB Core Vitrina V1';

const REGISTRY_UPLOAD_MENU_ROOT = 'WB Core';
const REGISTRY_UPLOAD_MENU_PREPARE_LABEL = 'Подготовить листы CONFIG / METRICS / FORMULAS';
const REGISTRY_UPLOAD_MENU_UPLOAD_LABEL = 'Отправить реестры на сервер';

const REGISTRY_UPLOAD_SHEET_LAYOUTS = {
  CONFIG: {
    headers: ['nm_id', 'enabled', 'display_name', 'group', 'display_order'],
    widths: [140, 100, 240, 160, 140],
  },
  METRICS: {
    headers: [
      'metric_key',
      'enabled',
      'scope',
      'label_ru',
      'calc_type',
      'calc_ref',
      'show_in_data',
      'format',
      'display_order',
      'section',
    ],
    widths: [180, 100, 100, 220, 110, 180, 120, 120, 140, 180],
  },
  FORMULAS: {
    headers: ['formula_id', 'expression', 'description'],
    widths: [180, 280, 320],
  },
};

const REGISTRY_UPLOAD_CONTROL_HEADERS = ['key', 'value'];
const REGISTRY_UPLOAD_CONTROL_ROWS = [
  'endpoint_url',
  'last_bundle_version',
  'last_status',
  'last_activated_at',
  'last_http_status',
  'last_validation_errors',
];

const REGISTRY_UPLOAD_CONTROL_START_COLUMN = 8;
const REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN = REGISTRY_UPLOAD_CONTROL_START_COLUMN + 1;
const REGISTRY_UPLOAD_ENDPOINT_ROW = 2;
const REGISTRY_UPLOAD_STATUS_FIRST_ROW = 3;
const REGISTRY_UPLOAD_STATUS_LAST_ROW = 7;
const REGISTRY_UPLOAD_CONTROL_NOTE =
  'Укажите полный URL HTTP entrypoint вида https://host/v1/registry-upload/bundle';

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu(REGISTRY_UPLOAD_MENU_ROOT)
    .addItem(REGISTRY_UPLOAD_MENU_PREPARE_LABEL, 'prepareRegistryUploadOperatorSheets')
    .addItem(REGISTRY_UPLOAD_MENU_UPLOAD_LABEL, 'uploadRegistryUploadBundle')
    .addToUi();
}

function prepareRegistryUploadOperatorSheets() {
  const spreadsheet = getRegistryUploadSpreadsheet_();
  const createdMap = _ensureRegistryUploadSheets_(spreadsheet);
  const seededCounts = _applyRegistryUploadCompactV3Seed_(spreadsheet);
  const summaries = _summarizeRegistryUploadSheets_(spreadsheet, createdMap);
  const summary = {
    ok: 'success',
    spreadsheet_id: spreadsheet.getId(),
    spreadsheet_name: spreadsheet.getName(),
    seed_version: 'compact_v3_runtime_compatible',
    sheet_names: summaries.map((item) => item.sheet_name),
    seeded_counts: seededCounts,
    sheets: summaries,
  };
  const message = `Подготовлены листы: ${summary.sheet_names.join(', ')} · seed ${seededCounts.config_v2}/${seededCounts.metrics_v2}/${seededCounts.formulas_v2}`;
  _notifyRegistryUploadOperator_(message, 'success');
  return JSON.stringify(summary);
}

function uploadRegistryUploadBundle() {
  const spreadsheet = getRegistryUploadSpreadsheet_();
  spreadsheet.toast('Собираю bundle и отправляю реестры...', REGISTRY_UPLOAD_MENU_ROOT, 5);
  try {
    const response = _uploadRegistryUploadBundleFromSheets_({});
    const result = response.upload_result;
    if (result) {
      const summaryLines = [
        `Статус: ${result.status}`,
        `Bundle: ${result.bundle_version}`,
        `HTTP: ${response.http_status}`,
      ];
      if (result.activated_at) {
        summaryLines.push(`Активировано: ${result.activated_at}`);
      }
      if (result.validation_errors.length) {
        summaryLines.push(`Ошибки: ${result.validation_errors.join('; ')}`);
      }
      _notifyRegistryUploadOperator_(summaryLines.join('\n'), result.status === 'accepted' ? 'success' : 'warning');
    } else {
      _notifyRegistryUploadOperator_(
        `HTTP ${response.http_status}: ${response.error || 'unexpected runtime error'}`,
        'error'
      );
    }
    return JSON.stringify(response);
  } catch (error) {
    _recordRegistryUploadTransportError_(String(error));
    _notifyRegistryUploadOperator_(String(error), 'error');
    throw error;
  }
}

function ensureRegistryUploadOperatorSheets() {
  const spreadsheet = getRegistryUploadSpreadsheet_();
  const createdMap = _ensureRegistryUploadSheets_(spreadsheet);
  const summaries = _summarizeRegistryUploadSheets_(spreadsheet, createdMap);

  return JSON.stringify({
    ok: 'success',
    spreadsheet_id: spreadsheet.getId(),
    spreadsheet_name: spreadsheet.getName(),
    sheet_names: summaries.map((summary) => summary.sheet_name),
    sheets: summaries,
  });
}

function _ensureRegistryUploadSheets_(spreadsheet) {
  const createdMap = {};
  Object.keys(REGISTRY_UPLOAD_SHEET_LAYOUTS).forEach((sheetName) => {
    createdMap[sheetName] = _ensureRegistryUploadSheet_(spreadsheet, sheetName);
  });
  return createdMap;
}

function _summarizeRegistryUploadSheets_(spreadsheet, createdMap) {
  return Object.keys(REGISTRY_UPLOAD_SHEET_LAYOUTS).map((sheetName) =>
    _summarizeRegistryUploadSheet_(spreadsheet, sheetName, Boolean(createdMap[sheetName]))
  );
}

function debugWriteRegistryUploadBundleToSheets(bundleJson, endpointUrl) {
  const bundle = _requireObject_(JSON.parse(String(bundleJson || '')), 'bundle');
  const spreadsheet = getRegistryUploadSpreadsheet_();
  ensureRegistryUploadOperatorSheets();
  _writeRegistryUploadBundleToSheets_(spreadsheet, bundle, String(endpointUrl || ''));
  return JSON.stringify({
    ok: 'success',
    spreadsheet_id: spreadsheet.getId(),
    spreadsheet_name: spreadsheet.getName(),
    sheet_names: Object.keys(REGISTRY_UPLOAD_SHEET_LAYOUTS),
    bundle_version: String(bundle.bundle_version || ''),
  });
}

function debugBuildRegistryUploadBundleFromSheets(bundleVersion, uploadedAt) {
  return JSON.stringify(
    _buildRegistryUploadBundleFromSheets_({
      bundleVersion: String(bundleVersion || '').trim(),
      uploadedAt: String(uploadedAt || '').trim(),
    })
  );
}

function debugUploadRegistryUploadBundleFromSheets(endpointUrl, bundleVersion, uploadedAt) {
  return JSON.stringify(
    _uploadRegistryUploadBundleFromSheets_({
      endpointUrl: String(endpointUrl || '').trim(),
      bundleVersion: String(bundleVersion || '').trim(),
      uploadedAt: String(uploadedAt || '').trim(),
    })
  );
}

function debugResetRegistryUploadOperatorSheets() {
  const spreadsheet = getRegistryUploadSpreadsheet_();
  ensureRegistryUploadOperatorSheets();
  Object.keys(REGISTRY_UPLOAD_SHEET_LAYOUTS).forEach((sheetName) => {
    const sheet = requireRegistryUploadSheet_(spreadsheet, sheetName);
    _clearRegistryUploadSheetData_(sheet, sheetName);
  });
  _clearRegistryUploadStatusBlock_(requireRegistryUploadSheet_(spreadsheet, 'CONFIG'));
  return JSON.stringify({
    ok: 'success',
    spreadsheet_id: spreadsheet.getId(),
    spreadsheet_name: spreadsheet.getName(),
    sheet_names: Object.keys(REGISTRY_UPLOAD_SHEET_LAYOUTS),
  });
}

function _uploadRegistryUploadBundleFromSheets_(options) {
  const spreadsheet = getRegistryUploadSpreadsheet_();
  const bundle = _buildRegistryUploadBundleFromSheets_({
    bundleVersion: String(options.bundleVersion || '').trim(),
    uploadedAt: String(options.uploadedAt || '').trim(),
  });
  const endpointUrl = _resolveRegistryUploadEndpointUrl_(spreadsheet, String(options.endpointUrl || '').trim());
  const response = _postRegistryUploadBundle_(bundle, endpointUrl);
  _writeRegistryUploadStatus_(requireRegistryUploadSheet_(spreadsheet, 'CONFIG'), endpointUrl, response);
  return response;
}

function _buildRegistryUploadBundleFromSheets_(options) {
  const spreadsheet = getRegistryUploadSpreadsheet_();
  const generatedUploadedAt = _formatRegistryUploadTimestamp_(new Date());
  const uploadedAt = String(options.uploadedAt || '').trim() || generatedUploadedAt;
  const bundleVersion =
    String(options.bundleVersion || '').trim() || `sheet_vitrina_v1_registry_upload__${uploadedAt}`;

  return {
    bundle_version: bundleVersion,
    uploaded_at: uploadedAt,
    config_v2: _readConfigRegistryItems_(requireRegistryUploadSheet_(spreadsheet, 'CONFIG')),
    metrics_v2: _readMetricRegistryItems_(requireRegistryUploadSheet_(spreadsheet, 'METRICS')),
    formulas_v2: _readFormulaRegistryItems_(requireRegistryUploadSheet_(spreadsheet, 'FORMULAS')),
  };
}

function _readConfigRegistryItems_(sheet) {
  const rows = _readRegistryUploadRows_(sheet, REGISTRY_UPLOAD_SHEET_LAYOUTS.CONFIG.headers);
  return rows.map((row, index) => ({
    nm_id: _requireIntLike_(row.nm_id, `CONFIG row ${index + 2}: nm_id`),
    enabled: _requireBooleanLike_(row.enabled, `CONFIG row ${index + 2}: enabled`),
    display_name: _requireNonEmptyString_(row.display_name, `CONFIG row ${index + 2}: display_name`),
    group: _requireNonEmptyString_(row.group, `CONFIG row ${index + 2}: group`),
    display_order: _requireIntLike_(row.display_order, `CONFIG row ${index + 2}: display_order`),
  }));
}

function _readMetricRegistryItems_(sheet) {
  const rows = _readRegistryUploadRows_(sheet, REGISTRY_UPLOAD_SHEET_LAYOUTS.METRICS.headers);
  return rows.map((row, index) => ({
    metric_key: _requireNonEmptyString_(row.metric_key, `METRICS row ${index + 2}: metric_key`),
    enabled: _requireBooleanLike_(row.enabled, `METRICS row ${index + 2}: enabled`),
    scope: _requireNonEmptyString_(row.scope, `METRICS row ${index + 2}: scope`),
    label_ru: _requireNonEmptyString_(row.label_ru, `METRICS row ${index + 2}: label_ru`),
    calc_type: _requireNonEmptyString_(row.calc_type, `METRICS row ${index + 2}: calc_type`),
    calc_ref: _requireNonEmptyString_(row.calc_ref, `METRICS row ${index + 2}: calc_ref`),
    show_in_data: _requireBooleanLike_(row.show_in_data, `METRICS row ${index + 2}: show_in_data`),
    format: _requireNonEmptyString_(row.format, `METRICS row ${index + 2}: format`),
    display_order: _requireIntLike_(row.display_order, `METRICS row ${index + 2}: display_order`),
    section: _requireNonEmptyString_(row.section, `METRICS row ${index + 2}: section`),
  }));
}

function _readFormulaRegistryItems_(sheet) {
  const rows = _readRegistryUploadRows_(sheet, REGISTRY_UPLOAD_SHEET_LAYOUTS.FORMULAS.headers);
  return rows.map((row, index) => ({
    formula_id: _requireNonEmptyString_(row.formula_id, `FORMULAS row ${index + 2}: formula_id`),
    expression: _requireNonEmptyString_(row.expression, `FORMULAS row ${index + 2}: expression`),
    description: _requireNonEmptyString_(row.description, `FORMULAS row ${index + 2}: description`),
  }));
}

function _readRegistryUploadRows_(sheet, headers) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    return [];
  }
  const values = sheet.getRange(2, 1, lastRow - 1, headers.length).getValues();
  return values
    .filter((row) => row.some((cell) => !_isRegistryUploadEmptyCell_(cell)))
    .map((row) => {
      const out = {};
      headers.forEach((header, index) => {
        out[header] = row[index];
      });
      return out;
    });
}

function _writeRegistryUploadBundleToSheets_(spreadsheet, bundle, endpointUrl) {
  _writeRegistrySheetRows_(spreadsheet, 'CONFIG', bundle.config_v2 || []);
  _writeRegistrySheetRows_(spreadsheet, 'METRICS', bundle.metrics_v2 || []);
  _writeRegistrySheetRows_(spreadsheet, 'FORMULAS', bundle.formulas_v2 || []);
  const configSheet = requireRegistryUploadSheet_(spreadsheet, 'CONFIG');
  if (endpointUrl) {
    _setRegistryUploadEndpointUrl_(configSheet, endpointUrl);
  }
  _clearRegistryUploadStatusBlock_(configSheet);
}

function _writeRegistrySheetRows_(spreadsheet, sheetName, rows) {
  const sheet = requireRegistryUploadSheet_(spreadsheet, sheetName);
  const headers = REGISTRY_UPLOAD_SHEET_LAYOUTS[sheetName].headers;
  _clearRegistryUploadSheetData_(sheet, sheetName);
  if (!Array.isArray(rows) || rows.length === 0) {
    return;
  }

  const values = rows.map((item) => headers.map((header) => item[header]));
  sheet.getRange(2, 1, values.length, headers.length).setValues(values);
}

function _applyRegistryUploadCompactV3Seed_(spreadsheet) {
  const seed = getRegistryUploadCompactV3Seed();
  _writeRegistrySheetRows_(spreadsheet, 'CONFIG', seed.config_v2 || []);
  _writeRegistrySheetRows_(spreadsheet, 'METRICS', seed.metrics_v2 || []);
  _writeRegistrySheetRows_(spreadsheet, 'FORMULAS', seed.formulas_v2 || []);
  return {
    config_v2: Array.isArray(seed.config_v2) ? seed.config_v2.length : 0,
    metrics_v2: Array.isArray(seed.metrics_v2) ? seed.metrics_v2.length : 0,
    formulas_v2: Array.isArray(seed.formulas_v2) ? seed.formulas_v2.length : 0,
  };
}

function _clearRegistryUploadSheetData_(sheet, sheetName) {
  const headers = REGISTRY_UPLOAD_SHEET_LAYOUTS[sheetName].headers;
  const startRow = 2;
  const rowCount = Math.max(sheet.getMaxRows() - 1, 1);
  sheet.getRange(startRow, 1, rowCount, headers.length).clearContent();
}

function _postRegistryUploadBundle_(bundle, endpointUrl) {
  const response = UrlFetchApp.fetch(endpointUrl, {
    method: 'post',
    contentType: 'application/json; charset=utf-8',
    muteHttpExceptions: true,
    payload: JSON.stringify(bundle),
  });

  const httpStatus = response.getResponseCode();
  const bodyText = response.getContentText();
  let bodyPayload = null;
  try {
    bodyPayload = JSON.parse(bodyText);
  } catch (error) {
    return {
      ok: 'error',
      endpoint_url: endpointUrl,
      http_status: httpStatus,
      error: `endpoint returned non-JSON response: ${bodyText}`,
    };
  }

  if (_isCanonicalRegistryUploadResult_(bodyPayload)) {
    return {
      ok: 'success',
      endpoint_url: endpointUrl,
      http_status: httpStatus,
      upload_result: bodyPayload,
    };
  }

  return {
    ok: 'error',
    endpoint_url: endpointUrl,
    http_status: httpStatus,
    error: String(bodyPayload.error || 'unexpected upload response'),
    response_body: bodyPayload,
  };
}

function _isCanonicalRegistryUploadResult_(payload) {
  return (
    payload &&
    typeof payload === 'object' &&
    typeof payload.status === 'string' &&
    typeof payload.bundle_version === 'string' &&
    payload.accepted_counts &&
    typeof payload.accepted_counts === 'object' &&
    Array.isArray(payload.validation_errors)
  );
}

function _ensureRegistryUploadSheet_(spreadsheet, sheetName) {
  const layout = REGISTRY_UPLOAD_SHEET_LAYOUTS[sheetName];
  let sheet = spreadsheet.getSheetByName(sheetName);
  const created = !sheet;
  if (!sheet) {
    sheet = spreadsheet.insertSheet(sheetName);
  }

  sheet.getRange(1, 1, 1, layout.headers.length).setValues([layout.headers]);
  sheet.getRange(1, 1, 1, layout.headers.length).setFontWeight('bold');
  sheet.setFrozenRows(1);
  layout.widths.forEach((width, index) => sheet.setColumnWidth(index + 1, width));

  if (sheetName === 'CONFIG') {
    _ensureRegistryUploadControlBlock_(sheet);
  }

  return created;
}

function _summarizeRegistryUploadSheet_(spreadsheet, sheetName, created) {
  const layout = REGISTRY_UPLOAD_SHEET_LAYOUTS[sheetName];
  const sheet = requireRegistryUploadSheet_(spreadsheet, sheetName);
  const summary = {
    sheet_name: sheetName,
    created: created,
    header: layout.headers,
    data_row_count: _readRegistryUploadRows_(sheet, layout.headers).length,
    last_row: sheet.getLastRow(),
    last_column: sheet.getLastColumn(),
  };
  if (sheetName === 'CONFIG') {
    summary.control_header = REGISTRY_UPLOAD_CONTROL_HEADERS;
    summary.control_rows = REGISTRY_UPLOAD_CONTROL_ROWS;
  }
  return {
    sheet_name: summary.sheet_name,
    created: summary.created,
    header: summary.header,
    data_row_count: summary.data_row_count,
    last_row: summary.last_row,
    last_column: summary.last_column,
    control_header: summary.control_header,
    control_rows: summary.control_rows,
  };
}

function _ensureRegistryUploadControlBlock_(sheet) {
  const existingValues = _readRegistryUploadControlValues_(sheet);
  sheet
    .getRange(1, REGISTRY_UPLOAD_CONTROL_START_COLUMN, 1, REGISTRY_UPLOAD_CONTROL_HEADERS.length)
    .setValues([REGISTRY_UPLOAD_CONTROL_HEADERS])
    .setFontWeight('bold');
  sheet
    .getRange(2, REGISTRY_UPLOAD_CONTROL_START_COLUMN, REGISTRY_UPLOAD_CONTROL_ROWS.length, 1)
    .setValues(REGISTRY_UPLOAD_CONTROL_ROWS.map((label) => [label]));
  sheet
    .getRange(2, REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN, REGISTRY_UPLOAD_CONTROL_ROWS.length, 1)
    .clearContent();
  sheet.setColumnWidth(REGISTRY_UPLOAD_CONTROL_START_COLUMN, 180);
  sheet.setColumnWidth(REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN, 320);
  sheet
    .getRange(2, REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN, REGISTRY_UPLOAD_CONTROL_ROWS.length, 1)
    .setValues(REGISTRY_UPLOAD_CONTROL_ROWS.map((key) => [existingValues[key]]));
  sheet.getRange(REGISTRY_UPLOAD_ENDPOINT_ROW, REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN).setNote(REGISTRY_UPLOAD_CONTROL_NOTE);
}

function _readRegistryUploadControlValues_(sheet) {
  const values = sheet
    .getRange(2, REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN, REGISTRY_UPLOAD_CONTROL_ROWS.length, 1)
    .getValues();
  const out = {};
  REGISTRY_UPLOAD_CONTROL_ROWS.forEach((key, index) => {
    out[key] = values[index][0];
  });
  return out;
}

function _writeRegistryUploadStatus_(configSheet, endpointUrl, response) {
  _setRegistryUploadEndpointUrl_(configSheet, endpointUrl);
  const bundleVersion = response.upload_result ? response.upload_result.bundle_version : '';
  const statusValue = response.upload_result ? response.upload_result.status : 'transport_error';
  const activatedAt = response.upload_result ? String(response.upload_result.activated_at || '') : '';
  const errors = response.upload_result
    ? response.upload_result.validation_errors.join('; ')
    : String(response.error || '');

  configSheet.getRange(3, REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN).setValue(bundleVersion);
  configSheet.getRange(4, REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN).setValue(statusValue);
  configSheet.getRange(5, REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN).setValue(activatedAt);
  configSheet.getRange(6, REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN).setValue(String(response.http_status || ''));
  configSheet.getRange(7, REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN).setValue(errors);
}

function _recordRegistryUploadTransportError_(message) {
  const configSheet = requireRegistryUploadSheet_(getRegistryUploadSpreadsheet_(), 'CONFIG');
  configSheet.getRange(3, REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN).setValue('');
  configSheet.getRange(4, REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN).setValue('transport_error');
  configSheet.getRange(5, REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN).setValue('');
  configSheet.getRange(6, REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN).setValue('');
  configSheet.getRange(7, REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN).setValue(message);
}

function _clearRegistryUploadStatusBlock_(configSheet) {
  configSheet.getRange(REGISTRY_UPLOAD_STATUS_FIRST_ROW, REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN, 5, 1).clearContent();
}

function _resolveRegistryUploadEndpointUrl_(spreadsheet, endpointUrlOverride) {
  if (endpointUrlOverride) {
    return _validateRegistryUploadEndpointUrl_(endpointUrlOverride);
  }
  const configSheet = requireRegistryUploadSheet_(spreadsheet, 'CONFIG');
  const endpointUrl = String(configSheet.getRange(REGISTRY_UPLOAD_ENDPOINT_ROW, REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN).getValue() || '').trim();
  if (!endpointUrl) {
    throw new Error('CONFIG!I2 должен содержать URL registry upload endpoint');
  }
  return _validateRegistryUploadEndpointUrl_(endpointUrl);
}

function _setRegistryUploadEndpointUrl_(configSheet, endpointUrl) {
  if (!endpointUrl) {
    return;
  }
  configSheet.getRange(REGISTRY_UPLOAD_ENDPOINT_ROW, REGISTRY_UPLOAD_CONTROL_VALUE_COLUMN).setValue(endpointUrl);
}

function _validateRegistryUploadEndpointUrl_(endpointUrl) {
  if (!/^https?:\/\//i.test(endpointUrl)) {
    throw new Error(`endpoint URL must start with http:// or https://, got ${endpointUrl}`);
  }
  return endpointUrl;
}

function _formatRegistryUploadTimestamp_(date) {
  return date.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function getRegistryUploadSpreadsheet_() {
  const spreadsheet = SpreadsheetApp.openById(REGISTRY_UPLOAD_TARGET_SPREADSHEET_ID);
  if (spreadsheet.getId() !== REGISTRY_UPLOAD_TARGET_SPREADSHEET_ID) {
    throw new Error('unexpected registry upload spreadsheet id');
  }
  return spreadsheet;
}

function requireRegistryUploadSheet_(spreadsheet, sheetName) {
  const sheet = spreadsheet.getSheetByName(sheetName);
  if (!sheet) {
    throw new Error(`missing required registry upload sheet: ${sheetName}`);
  }
  return sheet;
}

function _notifyRegistryUploadOperator_(message, level) {
  const spreadsheet = getRegistryUploadSpreadsheet_();
  spreadsheet.toast(message, REGISTRY_UPLOAD_MENU_ROOT, 8);
  try {
    SpreadsheetApp.getUi().alert(message);
  } catch (error) {
    Logger.log(`registry upload operator notice [${level}]: ${message}`);
  }
}

function _requireObject_(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} must be a JSON object`);
  }
  return value;
}

function _requireNonEmptyString_(value, label) {
  const normalized = String(value === null || value === undefined ? '' : value).trim();
  if (!normalized) {
    throw new Error(`${label} must be a non-empty string`);
  }
  return normalized;
}

function _requireIntLike_(value, label) {
  if (value === '' || value === null || value === undefined) {
    throw new Error(`${label} must be an integer`);
  }
  const numberValue = typeof value === 'number' ? value : Number(String(value).trim());
  if (!Number.isFinite(numberValue) || Math.floor(numberValue) !== numberValue) {
    throw new Error(`${label} must be an integer`);
  }
  return numberValue;
}

function _requireBooleanLike_(value, label) {
  if (value === true || value === false) {
    return value;
  }
  const normalized = String(value === null || value === undefined ? '' : value).trim().toLowerCase();
  if (['true', '1', 'yes', 'y', 'да'].indexOf(normalized) >= 0) {
    return true;
  }
  if (['false', '0', 'no', 'n', 'нет'].indexOf(normalized) >= 0) {
    return false;
  }
  throw new Error(`${label} must be a boolean`);
}

function _isRegistryUploadEmptyCell_(value) {
  return value === '' || value === null;
}
