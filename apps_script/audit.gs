/**
 * GRC tracker – audit trail for hand edits in the Master tab (DESIGN D8).
 *
 * Only Apps Script sees who edited a cell, so this installable edit trigger:
 *   1. writes one AuditLog row per edited cell (who, task ID, field, old value, new value);
 *   2. sets "Last updated" on every edited row;
 *   3. undoes Status = Done when the row has no "Submitted date" or no "Link" (proof).
 *
 * Columns are found by header name, never by position. Edits made by the service through the
 * Sheets API do not fire this trigger; the service writes its own AuditLog rows.
 *
 * Setup (once, by someone who can edit the protected AuditLog tab):
 *   Extensions → Apps Script → paste this file → run installAuditTrigger() → accept permissions.
 * Do NOT rename onMasterEdit to onEdit: a simple trigger cannot see the editor's email and
 * would run a second time next to the installable one.
 */

var MASTER = 'Master';
var AUDIT = 'AuditLog';
var MULTI_CELL_OLD = '(not available: multi-cell edit)';
var DONE_BLOCKED = 'Done needs Submitted date and Link';

function installAuditTrigger() {
  var spreadsheet = SpreadsheetApp.getActive();
  ScriptApp.getProjectTriggers().forEach(function (trigger) {
    if (trigger.getHandlerFunction() === 'onMasterEdit') ScriptApp.deleteTrigger(trigger);
  });
  ScriptApp.newTrigger('onMasterEdit').forSpreadsheet(spreadsheet).onEdit().create();
}

function onMasterEdit(e) {
  if (!e || !e.range) return;
  var range = e.range;
  var sheet = range.getSheet();
  if (sheet.getName() !== MASTER) return;

  var header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0].map(function (h) {
    return String(h).trim();
  });
  var col = function (name) {
    var i = header.indexOf(name);
    return i < 0 ? null : i + 1;
  };
  var idCol = col('ID');
  var statusCol = col('Status');
  var submittedCol = col('Submitted date');
  var linkCol = col('Link');
  var updatedCol = col('Last updated');

  var firstRow = Math.max(range.getRow(), 2); // never audit the header row
  var lastRow = range.getRow() + range.getNumRows() - 1;
  if (lastRow < firstRow) return;
  var firstCol = range.getColumn();
  var numCols = range.getNumColumns();
  var single = range.getNumRows() === 1 && numCols === 1;
  var user = editorEmail_(e);
  var now = new Date();
  var audit = [];

  for (var row = firstRow; row <= lastRow; row++) {
    var values = sheet.getRange(row, 1, 1, header.length).getValues()[0];
    var taskId = idCol ? String(values[idCol - 1]) : '';
    var touchedOnlyLastUpdated = true;

    for (var c = firstCol; c < firstCol + numCols; c++) {
      var field = header[c - 1] || 'Column ' + c;
      if (c === updatedCol) continue;
      touchedOnlyLastUpdated = false;
      var newValue = single && e.value !== undefined ? e.value : values[c - 1];
      var oldValue = single ? (e.oldValue === undefined ? '' : e.oldValue) : MULTI_CELL_OLD;
      audit.push([now, user, taskId, 'Edited', field, safe_(oldValue), safe_(newValue), '']);

      if (c === statusCol && String(newValue).trim() === 'Done') {
        var submitted = submittedCol ? values[submittedCol - 1] : '';
        var link = linkCol ? String(values[linkCol - 1]).trim() : '';
        if (submitted === '' || submitted === null || link === '') {
          var restore = single && e.oldValue !== undefined ? e.oldValue : 'In Progress';
          sheet.getRange(row, statusCol).setValue(restore);
          audit.push([now, user, taskId, 'Blocked', field, 'Done', safe_(restore), DONE_BLOCKED]);
          SpreadsheetApp.getActive().toast(
            (taskId || 'Row ' + row) + ': fill Submitted date and Link before setting Done.',
            'GRC tracker',
            8
          );
        }
      }
    }
    if (updatedCol && !touchedOnlyLastUpdated) {
      sheet.getRange(row, updatedCol).setValue(now);
    }
  }

  if (audit.length) {
    var log = SpreadsheetApp.getActive().getSheetByName(AUDIT);
    if (!log) throw new Error('GRC tracker: AuditLog tab is missing');
    log.getRange(log.getLastRow() + 1, 1, audit.length, audit[0].length).setValues(audit);
  }
}

function editorEmail_(e) {
  var email = '';
  try {
    if (e.user && typeof e.user.getEmail === 'function') email = e.user.getEmail();
  } catch (ignored) {}
  if (!email) {
    try {
      email = Session.getActiveUser().getEmail();
    } catch (ignored) {}
  }
  return email || 'unknown (outside the domain)';
}

/** Text that Sheets would read as a formula is stored as text. */
function safe_(value) {
  if (value instanceof Date || typeof value !== 'string') return value;
  return /^[=+\-@]/.test(value) ? "'" + value : value;
}

if (typeof module !== 'undefined') {
  module.exports = { onMasterEdit: onMasterEdit, installAuditTrigger: installAuditTrigger };
}
