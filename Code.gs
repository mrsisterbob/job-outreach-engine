/**
 * PRODUCTION CRM & JOB SEARCH BACKEND
 * 10-Column Schemas (incl. Column J UUID key) | Lock Synchronization | Bottom-Up Deletion
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

const ALL_TABS = Object.keys(TAB_MAP);
const UUID_COL = 10;    // Column J - the only field ever used to identify/move/delete a row
const NOTES_COL = 9;
const FOLLOWUP_COL = 7;
const LOCK_TIMEOUT_MS = 30000; // 30s concurrency lock, matches main.py's DB_WRITE_LOCK_TIMEOUT

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
    if (action === "add_row") {
      const targetTab = payload.target_code === "CW" ? "Carmen Warm" :
                        payload.target_code === "TC" ? "Tetiana Cold" : "Tetiana Cold";
      const sheet = getOrCreateSheet(ss, targetTab);
      const rowData = payload.row_data || [];
      sheet.appendRow(rowData);
      // Column J always holds the UUIDv4 row key, independent of row_data length/content
      sheet.getRange(sheet.getLastRow(), UUID_COL).setValue(payload.sheet_uuid || "");
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
      sheet.appendRow(rowData);
      sheet.getRange(sheet.getLastRow(), UUID_COL).setValue(payload.sheet_uuid || "");
      return respondJSON({ status: "success", message: "Contact created successfully", sheet_uuid: payload.sheet_uuid || "" });
    }

    // 3. Swipe-Reply Tab Move (/tw, /cw, /cc, /tc, /x -> build_crm_payload("update_status", ...))
    if (action === "update_status") {
      const sheetUuid = payload.sheet_uuid;
      const newTab = payload.new_tab;
      if (!sheetUuid || !newTab) {
        return respondJSON({ status: "error", message: "update_status requires sheet_uuid and new_tab" });
      }
      const found = findRecordBySheetUuid(ss, sheetUuid);
      if (!found) {
        return respondJSON({ status: "error", message: `No record found for sheet_uuid ${sheetUuid}` });
      }
      const rowValues = found.sheet.getRange(found.rowNum, 1, 1, found.sheet.getLastColumn()).getValues()[0];
      const targetSheet = getOrCreateSheet(ss, newTab);
      targetSheet.appendRow(rowValues);
      found.sheet.deleteRow(found.rowNum);
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
      // main.py already stamps the note with [YYYY-MM-DD] before sending - just append it
      const updatedNote = currentNote ? `${currentNote}\n${newNote}` : newNote;
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

function getOrCreateSheet(ss, name) {
  let sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    const schemaType = TAB_MAP[name] || "JOBS";
    sheet.appendRow(SCHEMAS[schemaType]);
    sheet.getRange(1, 1, 1, SCHEMAS[schemaType].length).setFontWeight("bold");
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