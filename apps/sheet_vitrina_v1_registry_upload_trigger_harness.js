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
  const requiredKeys = ['scriptPath', 'endpointUrl', 'bundleVersion', 'uploadedAt'];
  if (options.mode === 'bundle_upload') {
    requiredKeys.push('fixturePath');
  }
  if (options.mode === 'mvp_end_to_end') {
    requiredKeys.push('asOfDate');
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

function runMvpEndToEndMode({ context, spreadsheet, options }) {
  const prepareResult = parseJsonString(context.prepareRegistryUploadOperatorSheets());
  const builtBundle = parseJsonString(
    context.debugBuildRegistryUploadBundleFromSheets(options.bundleVersion, options.uploadedAt)
  );
  const acceptedResponse = parseJsonString(
    context.debugUploadRegistryUploadBundleFromSheets(options.endpointUrl, options.bundleVersion, options.uploadedAt)
  );
  const loadResult = parseJsonString(
    context.debugLoadSheetVitrinaTable(options.endpointUrl, options.asOfDate)
  );
  const configSheet = spreadsheet.getSheetByName('CONFIG');
  const statusBlock = readStatusBlock(configSheet);
  return {
    prepare_result: prepareResult,
    built_bundle: builtBundle,
    accepted_response: acceptedResponse,
    load_result: loadResult,
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
    this.maxRows = 200;
    this.maxColumns = 702;
    this.frozenRows = 0;
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
  }

  setColumnWidth(column, width) {
    this.columnWidths.set(column, width);
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
    for (let rowOffset = 0; rowOffset < this.numRows; rowOffset += 1) {
      for (let columnOffset = 0; columnOffset < this.numColumns; columnOffset += 1) {
        this.sheet.values.set(`${this.row + rowOffset}:${this.column + columnOffset}`, '');
      }
    }
    return this;
  }

  setFontWeight() {
    return this;
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
