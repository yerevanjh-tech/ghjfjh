const SHEET_NAME = 'Книги';
const READERS_SHEET_NAME = 'Читатели';
const HEADERS = [
  'inventory_id', 'title', 'author', 'year', 'isbn', 'status', 'updated_at',
  'issue_date', 'due_date', 'return_date'
];
const READER_HEADERS = ['id', 'name', 'phone', 'updated_at'];

function getSheet_() {
  const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = spreadsheet.getSheetByName(SHEET_NAME);
  if (!sheet) throw new Error('Создайте лист с названием: ' + SHEET_NAME);
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]);
  }
  return sheet;
}

function getReadersSheet_() {
  const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = spreadsheet.getSheetByName(READERS_SHEET_NAME);
  if (!sheet) sheet = spreadsheet.insertSheet(READERS_SHEET_NAME);
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, READER_HEADERS.length).setValues([READER_HEADERS]);
  }
  return sheet;
}

function doGet(event) {
  try {
    authorize_(getParameterToken_(event));
    const sheet = getSheet_();
    const values = sheet.getDataRange().getValues();
    const headers = HEADERS;
    const inventoryColumn = headers.indexOf('inventory_id');
    if (inventoryColumn < 0) throw new Error('В таблице отсутствует столбец inventory_id.');
    const records = values.length <= 1 ? [] : values.slice(1).filter(row => row[inventoryColumn] !== '')
      .map(row => {
        const item = {};
        headers.forEach((header, index) => item[header] = row[index]);
        return item;
      });
    const readerSheet = getReadersSheet_();
    const readerValues = readerSheet.getDataRange().getValues();
    const readers = readerValues.length <= 1 ? [] : readerValues.slice(1).filter(row => row[0] !== '')
      .map(row => ({
        id: row[0] || '',
        name: row[1] || '',
        phone: row[2] || '',
        updated_at: row[3] || ''
      }));
    return json_({ ok: true, records: records, readers: readers });
  } catch (error) {
    return json_({ ok: false, error: String(error) });
  }
}

function doPost(event) {
  try {
    const body = JSON.parse(event.postData.contents || '{}');
    authorize_(body.token || '');
    if (body.action === 'readers_upsert' && Array.isArray(body.records)) {
      const sheet = getReadersSheet_();
      const readerRows = body.records.map(record => READER_HEADERS.map(header => record[header] || ''));
      sheet.clearContents();
      sheet.getRange(1, 1, 1, READER_HEADERS.length).setValues([READER_HEADERS]);
      if (readerRows.length) sheet.getRange(2, 1, readerRows.length, READER_HEADERS.length).setValues(readerRows);
      return json_({ ok: true, saved: body.records.length });
    }
    if (body.action !== 'upsert' || !Array.isArray(body.records)) {
      throw new Error('Ожидался action=upsert и массив records.');
    }
    const sheet = getSheet_();
    const values = sheet.getDataRange().getValues();
    const headers = HEADERS;
    const idColumn = headers.indexOf('inventory_id');
    if (idColumn < 0) throw new Error('В таблице отсутствует столбец inventory_id.');
    if (headers.indexOf('updated_at') < 0) throw new Error('В таблице отсутствует столбец updated_at.');
    const rows = body.records.map(record => {
      const id = String(record.inventory_id || '');
      if (!id) throw new Error('У записи отсутствует inventory_id.');
      return headers.map(header => record[header] === undefined ? '' : record[header]);
    });
    sheet.getRange(1, 1, Math.max(sheet.getMaxRows(), 1), headers.length).clearContent();
    const extraColumns = sheet.getMaxColumns() - headers.length;
    if (extraColumns > 0) {
      sheet.deleteColumns(headers.length + 1, extraColumns);
    }
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    if (rows.length) sheet.getRange(2, 1, rows.length, headers.length).setValues(rows);
    return json_({ ok: true, saved: body.records.length });
  } catch (error) {
    return json_({ ok: false, error: String(error) });
  }
}

function getParameterToken_(event) {
  return event && event.parameter ? event.parameter.token || '' : '';
}

function authorize_(token) {
  const expected = PropertiesService.getScriptProperties().getProperty('SYNC_TOKEN') || '';
  if (!expected || token !== expected) throw new Error('Доступ запрещён. Неверный токен.');
}

function json_(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}
