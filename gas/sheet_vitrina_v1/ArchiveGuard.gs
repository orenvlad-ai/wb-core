const LEGACY_GOOGLE_SHEETS_CONTOUR_ARCHIVED = true;
const LEGACY_GOOGLE_SHEETS_ARCHIVE_STATUS = 'ARCHIVED / DO NOT USE';
const LEGACY_GOOGLE_SHEETS_ARCHIVE_MESSAGE =
  'Legacy Google Sheets contour is archived. Use the website/operator web-vitrina surface instead.';

function assertLegacyGoogleSheetsContourActive_() {
  if (LEGACY_GOOGLE_SHEETS_CONTOUR_ARCHIVED) {
    throw new Error(LEGACY_GOOGLE_SHEETS_ARCHIVE_MESSAGE);
  }
}

function getLegacyGoogleSheetsArchiveStatus() {
  return JSON.stringify({
    status: LEGACY_GOOGLE_SHEETS_ARCHIVE_STATUS,
    active: false,
    write_enabled: false,
    load_enabled: false,
    verification_target: false,
    message: LEGACY_GOOGLE_SHEETS_ARCHIVE_MESSAGE,
  });
}

function showLegacyGoogleSheetsArchiveNotice() {
  const message = LEGACY_GOOGLE_SHEETS_ARCHIVE_MESSAGE;
  try {
    SpreadsheetApp.getActiveSpreadsheet().toast(message, LEGACY_GOOGLE_SHEETS_ARCHIVE_STATUS, 8);
  } catch (error) {
    // The archive notice must not reopen legacy write/read flows.
  }
  try {
    SpreadsheetApp.getUi().alert(message);
  } catch (error) {
    // UI is unavailable for Execution API runs.
  }
  return getLegacyGoogleSheetsArchiveStatus();
}
