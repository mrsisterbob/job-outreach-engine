/**
 * PRODUCTION CRM & JOB SEARCH BACKEND
 * 10-Column Schemas (incl. Column J UUID key) | Lock Synchronization | Bottom-Up Deletion
 * Auto-Formatting Engine: frozen/styled headers, zebra striping, hidden UUID column, conditional formatting
 */

const SCHEMAS = {
  JOBS: ["Date Added", "Company", "Role", "Contact Email", "Fit Score", "Status", "Next Followup Date", "Job Link", "Notes", "Sheet UUID"],
  PEOPLE: ["Last Contact Date", "Contact Name", "Company / Org", "Contact Email", "Context / Priority", "Status", "Next Followup Date", "LinkedIn / Source", "Notes", "Sheet UUID"]
};

const TAB_MAP = {
  "Tetiana Cold": "JOBS",
  "Tetiana Warm": "JOBS",
  "Died": "JOBS",
  "Carmen Cold": "PEOPLE",
  "Carmen Warm": "PEOPLE",
  "Killed": "PEOPLE"
};

// Named-object field aliasing for add_row (Column order = SCHEMAS[type], minus Sheet UUID)
const FIELD_ALIASES = {
  JOBS: {
    "Date Added": ["date_added", "first_contact", "last_contact"],
    "Company": ["company", "employer_name"],
    "Role": ["role", "job_title", "title"],
    "Contact Email": ["contact_email", "email", "target_email"],
    "Fit Score": ["fit_score", "score"],
    "Status": ["status"],
    "Next Followup Date": ["next_followup", "next_followup_date"],
    "Job Link": ["job_link", "job_apply_link", "link"],
    "Notes": ["notes", "note", "reason"]
  },
  PEOPLE: {
    "Last Contact Date": ["last_contact", "date_added", "first_contact"],
    "Contact Name": ["name", "contact_name"],
    "Company / Org": ["company", "company_org"],
    "Contact Email": ["email", "contact_email"],
    "Context / Priority": ["priority", "context"],
    "Status": ["status"],
    "Next Followup Date": ["next_followup", "next_followup_date"],
    "LinkedIn / Source": ["source", "linkedin"],
    "Notes": ["note", "notes"]
  }
};

const ALL_TABS = Object.keys(TAB_MAP);
const UUID_COL = 10;    // Column J - the only field ever used to identify/move/delete a row
const NOTES_COL = 9;
const FOLLOWUP_COL = 7;
const LOCK_TIMEOUT_MS = 30000; // 30s concurrency lock, matches main.py's DB_WRITE_LOCK_TIMEOUT

// Formatting constants
const HEADER_BG = "#1B2A4A";
const HEADER_FG = "#FFFFFF";
const OVERDUE_BG = "#FCE8E6";
const HIGH_FIT_BG = "#E6F4EA";
const HEADER_ROW_HEIGHT = 35;
const FORMAT_BUFFER_ROWS = 1000; // ensures banding/conditional formatting cover future appended rows

function doPost(e) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(LOCK_TIMEOUT_MS)) {
    return respondJSON({ status: "error", message: "Lock timeout - server busy" });
  }

  try {
    const payload = JSON.parse(e.postData.contents);
    const action = payload.action;
    const ss = SpreadsheetApp.getActiveSpreadsheet();

    // 1. Job Pipeline Row Log (main.py: build_crm_payload("add_row", ...))
    // Accepts payload.row_data as either a structured array or a named object
    if (action === "add_row") {
      const targetTab = payload.target_code === "CW" ? "Carmen Warm" :
                        payload.target_code === "TC" ? "Tetiana Cold" : "Tetiana Cold";
      const sheet = getOrCreateSheet(ss, targetTab);
      const schemaType = TAB_MAP[targetTab] || "JOBS";
      const rowData = normalizeRowData(payload.row_data, schemaType);
      const newRow = sheet.getLastRow() + 1;
      sheet.getRange(newRow, 1, 1, rowData.length).setValues([rowData]); // Columns A-I only
      sheet.getRange(newRow, UUID_COL).setValue(payload.sheet_uuid || ""); // Column J reserved for UUID
      formatSheet(sheet);
      return respondJSON({ status: "success", message: "Row appended successfully", sheet_uuid: payload.sheet_uuid || "" });
    }

    // 2. /quick Contact Creation (main.py: build_crm_payload("quick_add", ...))
    if (action === "quick_add") {
      const targetTab = payload.target_code === "TC" ? "Tetiana Cold" :
                        payload.target_code === "CW" ? "Carmen Warm" : "Carmen Cold";
      const sheet = getOrCreateSheet(ss, targetTab);
      const rowData = [
        payload.last_contact || getTodayStr(),
        payload.name || "N/A",
        payload.company || "N/A",
        payload.email || "",
        payload.priority ? `Priority ${payload.priority}` : "Priority 5",
        "Cold Lead",
        payload.next_followup || getFollowupStr(14),
        payload.source || "Telegram /quick",
        payload.note || ""
      ];
      const newRow = sheet.getLastRow() + 1;
      sheet.getRange(newRow, 1, 1, rowData.length).setValues([rowData]); // Columns A-I only
      sheet.getRange(newRow, UUID_COL).setValue(payload.sheet_uuid || ""); // Column J reserved for UUID
      formatSheet(sheet);
      return respondJSON({ status: "success", message: "Contact created successfully", sheet_uuid: payload.sheet_uuid || "" });
    }

    // 3. Swipe-Reply Tab Move (/tw, /cw, /cc, /tc, /x -> build_crm_payload("update_status", ...))
    if (action === "update_status") {
      const sheetUuid = payload.sheet_uuid;
      let newTab = payload.new_tab;
      if (!sheetUuid || !newTab) {
        return respondJSON({ status: "error", message: "update_status requires sheet_uuid and new_tab" });
      }
      const found = findRecordBySheetUuid(ss, sheetUuid);
      if (!found) {
        return respondJSON({ status: "error", message: `No record found for sheet_uuid ${sheetUuid}` });
      }
      // Resolve ambiguous "kill" targets to the schema-correct archive tab: Died (JOBS) vs Killed (PEOPLE)
      if (newTab === "Died / Killed" || newTab === "Died/Killed" || newTab === "Dead") {
        const sourceSchemaType = TAB_MAP[found.sheet.getName()] || "JOBS";
        newTab = sourceSchemaType === "PEOPLE" ? "Killed" : "Died";
      }
      const rowValues = found.sheet.getRange(found.rowNum, 1, 1, found.sheet.getLastColumn()).getValues()[0];
      const targetSheet = getOrCreateSheet(ss, newTab);
      targetSheet.appendRow(rowValues);
      found.sheet.deleteRow(found.rowNum);
      formatSheet(targetSheet);
      return respondJSON({ status: "success", message: `Moved record ${sheetUuid} to ${newTab}` });
    }

    // 4. Snooze Follow-up (/f -> build_crm_payload("update_snooze", ...))
    if (action === "update_snooze") {
      const sheetUuid = payload.sheet_uuid;
      const nextFollowup = payload.next_followup;
      if (!sheetUuid || !nextFollowup) {
        return respondJSON({ status: "error", message: "update_snooze requires sheet_uuid and next_followup" });
      }
      const found = findRecordBySheetUuid(ss, sheetUuid);
      if (!found) {
        return respondJSON({ status: "error", message: `No record found for sheet_uuid ${sheetUuid}` });
      }
      found.sheet.getRange(found.rowNum, FOLLOWUP_COL).setValue(nextFollowup);
      return respondJSON({ status: "success", message: `Follow-up snoozed to ${nextFollowup}` });
    }

    // 5. Append Timestamped Note (/n -> build_crm_payload("append_note", ...))
    if (action === "append_note") {
      const sheetUuid = payload.sheet_uuid;
      const newNote = payload.note;
      if (!sheetUuid || !newNote) {
        return respondJSON({ status: "error", message: "append_note requires sheet_uuid and note" });
      }
      const found = findRecordBySheetUuid(ss, sheetUuid);
      if (!found) {
        return respondJSON({ status: "error", message: `No record found for sheet_uuid ${sheetUuid}` });
      }
      const currentNote = found.sheet.getRange(found.rowNum, NOTES_COL).getValue();
      const stampedNote = ensureTimestampedNote(newNote);
      const updatedNote = currentNote ? `${currentNote}\n${stampedNote}` : stampedNote;
      found.sheet.getRange(found.rowNum, NOTES_COL).setValue(updatedNote);
      return respondJSON({ status: "success", message: "Note appended" });
    }

    // 6. Networking Card Pull (/c, /cw, /cc -> {"action": "get_followups", "tab": target_code})
    if (action === "get_followups") {
      const tabName = payload.tab === "CW" ? "Carmen Warm" : payload.tab === "TC" ? "Tetiana Cold" : null;
      if (!tabName) {
        return respondJSON({ status: "error", message: `Unknown target_code: ${payload.tab}` });
      }
      const sheet = ss.getSheetByName(tabName);
      if (!sheet) {
        return respondJSON({ status: "success", followups: [] });
      }
      const data = sheet.getDataRange().getValues();
      const results = [];
      for (let i = data.length - 1; i >= 1; i--) {
        const row = data[i];
        const contextPriority = (row[4] || "").toString();
        const prioMatch = contextPriority.match(/\d+/);
        const rowPriority = prioMatch ? parseInt(prioMatch[0], 10) : 5;
        results.push({
          sheet_uuid: row[9] || "",
          name: row[1],
          company: row[2],
          priority: rowPriority,
          note: row[8],
          next_followup: formatDate(row[6])
        });
      }
      // Overdue Follow-Up Sort: Next Followup Date ASC, Priority DESC
      results.sort((a, b) => {
        const dateDiff = new Date(a.next_followup) - new Date(b.next_followup);
        return dateDiff !== 0 ? dateDiff : (b.priority - a.priority);
      });
      return respondJSON({ status: "success", followups: results });
    }

    // 7. Dynamic Filter Mirroring (main.py set_filter dual-write)
    if (action === "update_system_config") {
      const sheet = getSystemConfigSheet(ss);
      const key = payload.key;
      const value = JSON.stringify(payload.value);
      const data = sheet.getDataRange().getValues();
      let found = false;
      for (let i = data.length - 1; i >= 1; i--) {
        if ((data[i][0] || "").toString() === key) {
          sheet.getRange(i + 1, 2).setValue(value);
          found = true;
          break;
        }
      }
      if (!found) {
        sheet.appendRow([key, value]);
      }
      return respondJSON({ status: "success", message: `System_Config updated: ${key}` });
    }

    return respondJSON({ status: "error", message: "Invalid action type" });

  } catch (err) {
    return respondJSON({ status: "error", message: err.toString() });
  } finally {
    lock.releaseLock();
  }
}

function doGet(e) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(LOCK_TIMEOUT_MS)) {
    return respondJSON({ status: "error", message: "Lock timeout - server busy" });
  }

  try {
    const action = e.parameter.action;
    const ss = SpreadsheetApp.getActiveSpreadsheet();

    // 1. Fetch Priority Contacts sorted strictly by Overdue Next Followup Date
    if (action === "get_priority") {
      const priorityLevel = parseInt(e.parameter.level || "5", 10);
      const results = [];
      const tabs = ["Carmen Cold", "Carmen Warm"];

      tabs.forEach(tabName => {
        const sheet = ss.getSheetByName(tabName);
        if (!sheet) return;

        const data = sheet.getDataRange().getValues();
        for (let i = 1; i < data.length; i++) {
          const row = data[i];
          const contextPriority = (row[4] || "").toString();
          const prioMatch = contextPriority.match(/\d+/);
          const rowPriority = prioMatch ? parseInt(prioMatch[0], 10) : 5;

          if (rowPriority === priorityLevel) {
            results.push({
              sheet_uuid: row[9] || "",
              last_contact: formatDate(row[0]),
              name: row[1],
              company: row[2],
              email: row[3],
              priority: rowPriority,
              status: row[5],
              next_followup: formatDate(row[6]),
              source: row[7],
              latest_note: row[8]
            });
          }
        }
      });

      // Sort strictly by most overdue Next Followup Date ascending
      results.sort((a, b) => new Date(a.next_followup) - new Date(b.next_followup));
      return respondJSON({ status: "success", contacts: results });
    }

    // 2. Load Mirrored System_Config on Startup
    if (action === "load_system_config") {
      const sheet = ss.getSheetByName("System_Config");
      if (!sheet) return respondJSON({ status: "success", filters: {} });

      const data = sheet.getDataRange().getValues();
      const filters = {};
      for (let i = 1; i < data.length; i++) {
        if (data[i][0]) {
          try {
            filters[data[i][0]] = JSON.parse(data[i][1]);
          } catch (err) {
            filters[data[i][0]] = data[i][1];
          }
        }
      }
      return respondJSON({ status: "success", filters: filters });
    }

    return respondJSON({ status: "error", message: "Unsupported GET request" });

  } catch (err) {
    return respondJSON({ status: "error", message: err.toString() });
  } finally {
    lock.releaseLock();
  }
}

// Helper Utilities

// Bottom-to-top scan (maxRows down to 2) so in-flight matches survive row deletions elsewhere
function findRowBySheetUuid(sheet, sheetUuid) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return -1;
  const uuidValues = sheet.getRange(2, UUID_COL, lastRow - 1, 1).getValues();
  for (let i = uuidValues.length - 1; i >= 0; i--) {
    if ((uuidValues[i][0] || "").toString() === sheetUuid) {
      return i + 2; // convert back to 1-based sheet row number
    }
  }
  return -1;
}

function findRecordBySheetUuid(ss, sheetUuid) {
  for (const tabName of ALL_TABS) {
    const sheet = ss.getSheetByName(tabName);
    if (!sheet) continue;
    const rowNum = findRowBySheetUuid(sheet, sheetUuid);
    if (rowNum !== -1) {
      return { sheet: sheet, rowNum: rowNum };
    }
  }
  return null;
}

// Normalizes add_row's payload.row_data (array OR named object) into a fixed-length
// Column A-I array; Column J (Sheet UUID) is always set separately by the caller.
function normalizeRowData(rowInput, schemaType) {
  const schema = SCHEMAS[schemaType];
  const fieldCount = schema.length - 1;

  if (Array.isArray(rowInput)) {
    const out = rowInput.slice(0, fieldCount);
    while (out.length < fieldCount) out.push("");
    return out;
  }

  if (rowInput && typeof rowInput === "object") {
    const aliasMap = FIELD_ALIASES[schemaType];
    return schema.slice(0, fieldCount).map(colName => {
      const aliases = aliasMap[colName] || [];
      for (const key of aliases) {
        if (rowInput[key] !== undefined && rowInput[key] !== null && rowInput[key] !== "") {
          return rowInput[key];
        }
      }
      return "";
    });
  }

  return new Array(fieldCount).fill("");
}

function ensureTimestampedNote(note) {
  return /^\[\d{4}-\d{2}-\d{2}\]/.test(note) ? note : `[${getTodayStr()}] ${note}`;
}

function getOrCreateSheet(ss, name) {
  let sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    const schemaType = TAB_MAP[name] || "JOBS";
    sheet.appendRow(SCHEMAS[schemaType]);
    formatSheet(sheet);
  }
  return sheet;
}

function getSystemConfigSheet(ss) {
  let sheet = ss.getSheetByName("System_Config");
  if (!sheet) {
    sheet = ss.insertSheet("System_Config");
    sheet.appendRow(["Key", "Value JSON"]);
    sheet.getRange(1, 1, 1, 2).setFontWeight("bold");
  }
  return sheet;
}

// Beautification & Formatting Engine

// Re-applies full styling to every CRM tab; safe to run repeatedly (bindings/rules are reset, not stacked)
function formatAllSheets() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  ALL_TABS.forEach(tabName => {
    const sheet = ss.getSheetByName(tabName);
    if (sheet) formatSheet(sheet);
  });
}

// Applies frozen/styled header, body font, zebra striping, hidden UUID column, and conditional formatting
function formatSheet(sheet) {
  const schemaType = TAB_MAP[sheet.getName()];
  if (!schemaType) return; // skip non-CRM tabs (e.g. System_Config)
  const numCols = SCHEMAS[schemaType].length;
  const maxRows = Math.max(sheet.getMaxRows(), FORMAT_BUFFER_ROWS);

  const headerRange = sheet.getRange(1, 1, 1, numCols);
  headerRange.setBackground(HEADER_BG)
    .setFontColor(HEADER_FG)
    .setFontWeight("bold")
    .setHorizontalAlignment("center")
    .setVerticalAlignment("middle");
  sheet.setRowHeight(1, HEADER_ROW_HEIGHT);
  sheet.setFrozenRows(1);

  sheet.getRange(2, 1, maxRows - 1, numCols)
    .setFontFamily("Arial")
    .setFontSize(9)
    .setVerticalAlignment("middle");

  sheet.hideColumns(UUID_COL);

  // Zebra striping (Light Grey) - clear existing bandings first to avoid duplicate-banding errors
  sheet.getBandings().forEach(b => b.remove());
  sheet.getRange(2, 1, maxRows - 1, numCols)
    .applyRowBanding(SpreadsheetApp.BandingTheme.LIGHT_GREY, false, false);

  applyConditionalFormatting(sheet, maxRows, numCols);
}

// Soft Red for overdue Next Followup Date (Col G), Soft Green for Fit Score >= 80 (Col E)
function applyConditionalFormatting(sheet, maxRows, numCols) {
  const range = sheet.getRange(2, 1, maxRows - 1, numCols);
  const overdueRule = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=AND(ISDATE(G2), G2<TODAY(), G2<>"")')
    .setBackground(OVERDUE_BG)
    .setRanges([range])
    .build();
  const highFitRule = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=AND(ISNUMBER(E2), E2>=80)')
    .setBackground(HIGH_FIT_BG)
    .setRanges([range])
    .build();
  sheet.setConditionalFormatRules([overdueRule, highFitRule]);
}

function respondJSON(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function getTodayStr() {
  return Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "yyyy-MM-dd");
}

function getFollowupStr(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return Utilities.formatDate(d, Session.getScriptTimeZone(), "yyyy-MM-dd");
}

function formatDate(val) {
  if (val instanceof Date) {
    return Utilities.formatDate(val, Session.getScriptTimeZone(), "yyyy-MM-dd");
  }
  return val || getTodayStr();
}