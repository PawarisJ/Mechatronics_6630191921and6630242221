// Paste this into your Google Sheet: Extensions > Apps Script, replace everything, then
// Deploy > Manage deployments > (edit pencil) > Version: New version > Deploy  (keeps the same URL).
//
// The Raspberry Pi POSTs one event per access attempt by a registered person. Each becomes a row
// in the "Access Log" tab (created automatically as the first tab):
//   Date | Time | Employee ID | Name | Face match (%) | Blink check | Password | Result | Status
// Status is "On time" / "Late" for a granted check-in (cutoff set on the Pi's admin page), "-" otherwise.
// Other event types (old thumbs-up / attendance) are ignored.

var HEADER = ["Date", "Time", "Employee ID", "Name", "Face match (%)", "Blink check", "Password", "Result", "Status"];
var RESULT_COL = 8, STATUS_COL = 9;

function doPost(e) {
  var data = JSON.parse(e.postData.contents);
  if (data.type !== "access") {
    return reply({ ok: true, version: 4, ignored: true });
  }
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName("Access Log") || ss.insertSheet("Access Log", 0);
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(HEADER);
    sheet.getRange(1, 1, 1, HEADER.length).setFontWeight("bold").setBackground("#e8eaed");
    sheet.setFrozenRows(1);
  } else if (sheet.getRange(1, STATUS_COL).getValue() !== "Status") {
    // sheet made by the older script: add the new column heading, old rows are left as they are
    sheet.getRange(1, STATUS_COL).setValue("Status").setFontWeight("bold").setBackground("#e8eaed");
  }
  // leading ' keeps IDs like 001 as text instead of turning them into the number 1
  sheet.appendRow([data.date, data.time, "'" + data.employee_id, data.name, data.face_match,
                   data.blink, data.password, data.result, data.status || "-"]);

  // colour the Result cell: green = granted, orange = wrong code but can retry, red = failed
  var row = sheet.getLastRow();
  var colour = data.result === "Access granted" ? "#c8f0d4"
             : data.result.indexOf("Wrong code") === 0 ? "#ffe2b8" : "#f8c9c6";
  sheet.getRange(row, RESULT_COL).setBackground(colour);
  sheet.getRange(row, STATUS_COL).setBackground(data.status === "On time" ? "#c8f0d4"
                                                : data.status === "Late" ? "#ffe2b8" : null);
  sheet.getRange(row, 7).setBackground(data.password === "Correct" ? "#c8f0d4"
                                       : data.password === "Incorrect" ? "#f8c9c6" : null);
  return reply({ ok: true, version: 4 });
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
