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
  const source = fs.readFileSync(scriptPath, 'utf8');
  vm.runInNewContext(source, context, { filename: scriptPath });

  const ensureResult = parseJsonString(context.ensureRegistryUploadOperatorSheets());
  const bundleFixture = JSON.parse(fs.readFileSync(path.resolve(options.fixturePath), 'utf8'));
  parseJsonString(
    context.debugWriteRegistryUploadBundleToSheets(
      JSON.stringify(bundleFixture),
      ''
    )
  );
  const builtBundle = parseJsonString(
    context.debugBuildRegistryUploadBundleFromSheets(
      options.bundleVersion,
      options.uploadedAt
    )
  );
  const acceptedResponse = parseJsonString(
    context.debugUploadRegistryUploadBundleFromSheets(
      options.endpointUrl,
      options.bundleVersion,
      options.uploadedAt
    )
  );
  const duplicateResponse = parseJsonString(
    context.debugUploadRegistryUploadBundleFromSheets(
      options.endpointUrl,
      options.bundleVersion,
      options.uploadedAt
    )
  );

  const configSheet = spreadsheet.getSheetByName('CONFIG');
  const statusBlock = {
    endpoint_url: String(configSheet.getCellValue(2, 9) || ''),
    last_bundle_version: String(configSheet.getCellValue(3, 9) || ''),
    last_status: String(configSheet.getCellValue(4, 9) || ''),
    last_activated_at: String(configSheet.getCellValue(5, 9) || ''),
    last_http_status: String(configSheet.getCellValue(6, 9) || ''),
    last_validation_errors: String(configSheet.getCellValue(7, 9) || ''),
  };

  parseJsonString(context.debugResetRegistryUploadOperatorSheets());

  const result = {
    ensure_result: ensureResult,
    built_bundle: builtBundle,
    accepted_response: acceptedResponse,
    duplicate_response: duplicateResponse,
    status_block: statusBlock,
  };
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
  for (const required of ['scriptPath', 'fixturePath', 'endpointUrl', 'bundleVersion', 'uploadedAt']) {
    if (!options[required]) {
      throw new Error(`missing required argument --${required}`);
    }
  }
  return options;
}

function parseJsonString(value) {
  return typeof value === 'string' ? JSON.parse(value) : value;
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
    this.maxColumns = 26;
    this.frozenRows = 0;
    this.columnWidths = new Map();
  }

  getName() {
    return this.name;
  }

  getRange(row, column, numRows = 1, numColumns = 1) {
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
}

main();
