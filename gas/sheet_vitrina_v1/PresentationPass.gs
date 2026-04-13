const DATA_VITRINA_WIDTHS = {
  label: 280,
  key: 220,
  date: 96,
};

const STATUS_WIDTHS = [190, 110, 110, 110, 110, 110, 110, 120, 120, 150, 260];

const PRESENTATION_HEADER_BACKGROUND = '#1f2937';
const PRESENTATION_HEADER_FONT_COLOR = '#ffffff';
const PRESENTATION_DATE_PATTERN = 'dd.mm.yyyy';
const PRESENTATION_PERCENT_PATTERN = '0.0%';
const PRESENTATION_RUBLE_PATTERN = '#,##0" ₽"';
const PRESENTATION_INTEGER_PATTERN = '#,##0';
const PRESENTATION_DECIMAL_PATTERN = '#,##0.00';

function applySheetVitrinaV1PresentationPass() {
  const spreadsheet = getPresentationSpreadsheet_();
  const dataSheet = requirePresentationSheet_(spreadsheet, 'DATA_VITRINA');
  const statusSheet = requirePresentationSheet_(spreadsheet, 'STATUS');

  const dataSummary = applyDataVitrinaPresentation_(dataSheet);
  const statusSummary = applyStatusPresentation_(statusSheet);
  SpreadsheetApp.flush();

  return JSON.stringify({
    ok: 'success',
    spreadsheet_id: spreadsheet.getId(),
    spreadsheet_name: spreadsheet.getName(),
    sheets: [dataSummary, statusSummary],
  });
}

function getSheetVitrinaV1PresentationSnapshot() {
  const spreadsheet = getPresentationSpreadsheet_();
  return JSON.stringify({
    spreadsheet_id: spreadsheet.getId(),
    spreadsheet_name: spreadsheet.getName(),
    sheets: [
      buildPresentationSnapshot_(spreadsheet, 'DATA_VITRINA'),
      buildPresentationSnapshot_(spreadsheet, 'STATUS'),
    ],
  });
}

function getPresentationSpreadsheet_() {
  const spreadsheet = SpreadsheetApp.openById(TARGET_SPREADSHEET_ID);
  if (spreadsheet.getId() !== TARGET_SPREADSHEET_ID) {
    throw new Error('unexpected spreadsheet id');
  }
  return spreadsheet;
}

function requirePresentationSheet_(spreadsheet, sheetName) {
  const sheet = spreadsheet.getSheetByName(sheetName);
  if (!sheet) {
    throw new Error(`missing required sheet: ${sheetName}`);
  }
  return sheet;
}

function applyDataVitrinaPresentation_(sheet) {
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow === 0 || lastColumn < 3) {
    throw new Error('DATA_VITRINA must contain header and date columns');
  }

  styleHeaderRow_(sheet, lastColumn);
  sheet.setFrozenColumns(2);
  sheet.setColumnWidth(1, DATA_VITRINA_WIDTHS.label);
  sheet.setColumnWidth(2, DATA_VITRINA_WIDTHS.key);
  if (lastColumn > 2) {
    sheet.setColumnWidths(3, lastColumn - 2, DATA_VITRINA_WIDTHS.date);
    sheet.getRange(1, 3, 1, lastColumn - 2)
      .setHorizontalAlignment('center')
      .setNumberFormat(PRESENTATION_DATE_PATTERN);
  }

  if (lastRow > 1) {
    sheet.getRange(2, 1, lastRow - 1, 1).setHorizontalAlignment('left');
    sheet.getRange(2, 2, lastRow - 1, 1).setHorizontalAlignment('left');
    if (lastColumn > 2) {
      const bodyRange = sheet.getRange(2, 3, lastRow - 1, lastColumn - 2);
      bodyRange.setHorizontalAlignment('right');
      applyDataBodyNumberFormats_(sheet, lastRow, lastColumn);
    }
  }

  return {
    sheet_name: 'DATA_VITRINA',
    frozen_rows: sheet.getFrozenRows(),
    frozen_columns: sheet.getFrozenColumns(),
    last_row: lastRow,
    last_column: lastColumn,
  };
}

function applyStatusPresentation_(sheet) {
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow === 0 || lastColumn < STATUS_WIDTHS.length) {
    throw new Error('STATUS must contain the expected V1 columns');
  }

  styleHeaderRow_(sheet, lastColumn);
  sheet.setFrozenRows(1);
  STATUS_WIDTHS.forEach((width, index) => sheet.setColumnWidth(index + 1, width));

  if (lastRow > 1) {
    const bodyRows = lastRow - 1;
    sheet.getRange(2, 1, bodyRows, 1).setHorizontalAlignment('left');
    sheet.getRange(2, 2, bodyRows, 1).setHorizontalAlignment('center').setFontWeight('bold');
    sheet.getRange(2, 3, bodyRows, 5)
      .setHorizontalAlignment('center')
      .setNumberFormat(PRESENTATION_DATE_PATTERN);
    sheet.getRange(2, 8, bodyRows, 2)
      .setHorizontalAlignment('center')
      .setNumberFormat(PRESENTATION_INTEGER_PATTERN);
    sheet.getRange(2, 10, bodyRows, 1).setHorizontalAlignment('left');
    sheet.getRange(2, 11, bodyRows, 1).setHorizontalAlignment('left');
  }

  return {
    sheet_name: 'STATUS',
    frozen_rows: sheet.getFrozenRows(),
    frozen_columns: sheet.getFrozenColumns(),
    last_row: lastRow,
    last_column: lastColumn,
  };
}

function styleHeaderRow_(sheet, lastColumn) {
  const header = sheet.getRange(1, 1, 1, lastColumn);
  header
    .setBackground(PRESENTATION_HEADER_BACKGROUND)
    .setFontColor(PRESENTATION_HEADER_FONT_COLOR)
    .setFontWeight('bold')
    .setVerticalAlignment('middle');
  sheet.getRange(1, 1, 1, Math.min(lastColumn, 2)).setHorizontalAlignment('left');
}

function applyDataBodyNumberFormats_(sheet, lastRow, lastColumn) {
  const keys = sheet.getRange(2, 2, lastRow - 1, 1).getDisplayValues();
  const values = sheet.getRange(2, 3, lastRow - 1, lastColumn - 2).getValues();
  for (let index = 0; index < keys.length; index += 1) {
    const key = String(keys[index][0] || '');
    const pattern = resolveDataPattern_(key, values[index]);
    sheet.getRange(index + 2, 3, 1, lastColumn - 2).setNumberFormat(pattern);
  }
}

function resolveDataPattern_(key, rowValues) {
  if (/\|(spp|ads_ctr|ctr|ctr_current)$/.test(key)) {
    return PRESENTATION_PERCENT_PATTERN;
  }
  if (/(price_seller_discounted|_rub)$/.test(key)) {
    return PRESENTATION_RUBLE_PATTERN;
  }
  for (let index = 0; index < rowValues.length; index += 1) {
    const value = rowValues[index];
    if (typeof value === 'number' && Math.floor(value) !== value) {
      return PRESENTATION_DECIMAL_PATTERN;
    }
  }
  return PRESENTATION_INTEGER_PATTERN;
}

function buildPresentationSnapshot_(spreadsheet, sheetName) {
  const sheet = requirePresentationSheet_(spreadsheet, sheetName);
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  const range = lastRow > 0 && lastColumn > 0 ? sheet.getRange(1, 1, lastRow, lastColumn) : null;

  return {
    sheet_name: sheetName,
    last_row: lastRow,
    last_column: lastColumn,
    frozen_rows: sheet.getFrozenRows(),
    frozen_columns: sheet.getFrozenColumns(),
    column_widths: collectColumnWidths_(sheet, lastColumn),
    header_style: lastColumn > 0 ? describeHeaderStyle_(sheet, lastColumn) : {},
    samples: sheetName === 'DATA_VITRINA' ? describeDataSamples_(sheet) : describeStatusSamples_(sheet),
    values: range ? normalizeMatrix_(range.getValues()) : [],
  };
}

function collectColumnWidths_(sheet, lastColumn) {
  const widths = {};
  for (let column = 1; column <= lastColumn; column += 1) {
    widths[columnName_(column)] = sheet.getColumnWidth(column);
  }
  return widths;
}

function describeHeaderStyle_(sheet, lastColumn) {
  return {
    background: sheet.getRange(1, 1).getBackground(),
    font_color: sheet.getRange(1, 1).getFontColor(),
    font_weight: sheet.getRange(1, 1).getFontWeight(),
    left_alignment: sheet.getRange(1, 1).getHorizontalAlignment(),
    date_alignment: lastColumn >= 3 ? sheet.getRange(1, 3).getHorizontalAlignment() : null,
    date_number_format: lastColumn >= 3 ? sheet.getRange(1, 3).getNumberFormat() : null,
  };
}

function describeDataSamples_(sheet) {
  return {
    percent: describeDataSampleByPredicate_(sheet, (key) => /\|(spp|ads_ctr|ctr|ctr_current)$/.test(key)),
    ruble: describeDataSampleByPredicate_(sheet, (key) => /(price_seller_discounted|_rub)$/.test(key)),
    integer: describeDataSampleByPredicate_(
      sheet,
      (key) => !/\|(spp|ads_ctr|ctr|ctr_current)$/.test(key) && !/(price_seller_discounted|_rub)$/.test(key)
    ),
  };
}

function describeDataSampleByPredicate_(sheet, predicate) {
  const lastRow = sheet.getLastRow();
  const keys = lastRow > 1 ? sheet.getRange(2, 2, lastRow - 1, 1).getDisplayValues() : [];
  for (let index = 0; index < keys.length; index += 1) {
    const key = String(keys[index][0] || '');
    if (predicate(key)) {
      const rowNumber = index + 2;
      return {
        key: key,
        row: rowNumber,
        number_format: sheet.getRange(rowNumber, 3).getNumberFormat(),
        alignment: sheet.getRange(rowNumber, 3).getHorizontalAlignment(),
      };
    }
  }
  return null;
}

function describeStatusSamples_(sheet) {
  return {
    kind: describeCell_(sheet, 2, 2),
    freshness: describeCell_(sheet, 2, 3),
    snapshot_date: describeCell_(sheet, 2, 4),
    requested_count: describeCell_(sheet, 2, 8),
    covered_count: describeCell_(sheet, 2, 9),
  };
}

function describeCell_(sheet, row, column) {
  if (sheet.getLastRow() < row || sheet.getLastColumn() < column) {
    return null;
  }
  const cell = sheet.getRange(row, column);
  return {
    display_value: normalizeScalar_(cell.getValue()),
    number_format: cell.getNumberFormat(),
    alignment: cell.getHorizontalAlignment(),
    font_weight: cell.getFontWeight(),
  };
}

function normalizeMatrix_(matrix) {
  return matrix.map((row) => row.map((value) => normalizeScalar_(value)));
}

function normalizeScalar_(value) {
  if (value instanceof Date) {
    return Utilities.formatDate(value, 'UTC', "yyyy-MM-dd'T'HH:mm:ss'Z'");
  }
  if (value === null || value === '') {
    return '';
  }
  if (typeof value === 'number' || typeof value === 'string' || typeof value === 'boolean') {
    return value;
  }
  return String(value);
}

function columnName_(index) {
  let current = index;
  let output = '';
  while (current > 0) {
    const remainder = (current - 1) % 26;
    output = String.fromCharCode(65 + remainder) + output;
    current = Math.floor((current - 1) / 26);
  }
  return output;
}
