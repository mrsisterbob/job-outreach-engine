/**
 * PRODUCTION CRM & JOB SEARCH BACKEND
 * Dual 9-Column Schemas | Lock Synchronization | Bottom-Up Deletion
 */

const SCHEMAS = {
  JOBS: ["Date Added", "Company", "Role", "Contact Email", "Fit Score", "Status", "Next Followup Date", "Job Link", "Notes"],
  PEOPLE: ["Last Contact Date", "Contact Name", "Company / Org", "Contact Email", "Context / Priority", "Status", "Next Followup Date", "LinkedIn / Source", "Notes"]
};

const TAB_MAP = {
  "Tetiana Cold": "JOBS",
  "Tetiana Warm": "JOBS",
  "Died": "JOBS",
  "Carmen Cold": "PEOPLE",
  "Carmen Warm": "PEOPLE",
  "Killed": "PEOPLE"
};

function doPost(e) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) {
    return respondJSON({ status: "error", message: "Lock timeout - server busy" });
  }

  try {
    const payload = JSON.parse(e.postData.contents);
    const action = payload.action;
    const ss = SpreadsheetApp.getActiveSpreadsheet();

    // 1. Log or Append New Row
    if (action === "add_row" || action === "quick_add") {
      const targetTab = payload.target_code === "TC" ? "Tetiana Cold" : 
                        payload.target_code === "CW" ? "Carmen Warm" : "Carmen Cold";
      const sheet = getOrCreateSheet(ss, targetTab);
      
      let rowData = [];
      if (action === "quick_add") {
        // People Schema Mapping
        rowData = [
          payload.last_contact || getTodayStr(),
          payload.name || "N/A",
          payload.company || "N/A",
          payload.email || "N/A",
          payload.priority ? `Priority ${payload.priority}` : "Priority 5",
          "Cold Lead",
          payload.next_followup || getFollowupStr(14),
          payload.source || "Telegram /quick",
          payload.note || ""
        ];
      } else {
        rowData = payload.row_data;
      }

      sheet.appendRow(rowData);
      return respondJSON({ status: "success", message: "Row appended successfully" });
    }

    // 2. Move Row (Swipe Action Router)
    if (action === "move_row") {
      const sourceTab = payload.source_tab;
      const targetTab = payload.target_tab;
      const matchEmail = (payload.email || "").toLowerCase().trim();
      const matchCompany = (payload.company || "").toLowerCase().trim();

      const sourceSheet = ss.getSheetByName(sourceTab);
      const targetSheet = getOrCreateSheet(ss, targetTab);

      if (!sourceSheet) return respondJSON({ status: "error", message: "Source tab missing" });

      const data = sourceSheet.getDataRange().getValues();
      // Bottom-up iteration to prevent index shifting on row deletion
      for (let i = data.length - 1; i >= 1; i--) {
        const row = data[i];
        const rowEmail = (row[3] || "").toString().toLowerCase().trim();
        const rowCompany = (row[1] || row[2] || "").toString().toLowerCase().trim();

        if ((matchEmail && rowEmail === matchEmail) || (matchCompany && rowCompany === matchCompany)) {
          // Append to destination sheet
          targetSheet.appendRow(row);
          // Delete from source sheet
          sourceSheet.deleteRow(i + 1);
          return respondJSON({ status: "success", message: `Moved row from ${sourceTab} to ${targetTab}` });
        }
      }
      return respondJSON({ status: "error", message: "Matching record not found for move operation" });
    }

    // 3. Append Note & Recalculate Follow-up Date
    if (action === "append_note") {
      const tabName = payload.tab;
      const matchIdentifier = (payload.identifier || "").toLowerCase().trim();
      const newNote = payload.note;
      const daysToAdd = payload.followup_days || 7;

      const sheet = ss.getSheetByName(tabName);
      if (!sheet) return respondJSON({ status: "error", message: "Sheet not found" });

      const data = sheet.getDataRange().getValues();
      for (let i = data.length - 1; i >= 1; i--) {
        const row = data[i];
        const email = (row[3] || "").toString().toLowerCase().trim();
        const nameOrComp = (row[1] || "").toString().toLowerCase().trim();

        if (email === matchIdentifier || nameOrComp === matchIdentifier) {
          const rowNum = i + 1;
          const currentNote = sheet.getRange(rowNum, 9).getValue();
          const timestamp = getTodayStr();
          const updatedNote = currentNote ? `${currentNote}\n[${timestamp}] ${newNote}` : `[${timestamp}] ${newNote}`;
          
          sheet.getRange(rowNum, 9).setValue(updatedNote);
          sheet.getRange(rowNum, 7).setValue(getFollowupStr(daysToAdd)); // Col G: Next Followup Date
          return respondJSON({ status: "success", message: "Note appended and follow-up updated" });
        }
      }
      return respondJSON({ status: "error", message: "Record not found for note append" });
    }

    // 4. Filter State Mirroring
    if (action === "save_filters") {
      const filterSheet = getOrCreateSheet(ss, "Filters");
      filterSheet.clear();
      filterSheet.appendRow(["Key", "Value JSON"]);
      
      const filters = payload.filters;
      for (let key in filters) {
        filterSheet.appendRow([key, JSON.stringify(filters[key])]);
      }
      return respondJSON({ status: "success", message: "Filters mirrored to Sheets" });
    }

    return respondJSON({ status: "error", message: "Invalid action type" });

  } catch (err) {
    return respondJSON({ status: "error", message: err.toString() });
  } finally {
    lock.releaseLock();
  }
}

function doGet(e) {
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

  // 2. Load Mirrored Filters on Container Startup
  if (action === "load_filters") {
    const sheet = ss.getSheetByName("Filters");
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
}

// Helper Utilities
function getOrCreateSheet(ss, name) {
  let sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    const schemaType = TAB_MAP[name] || "JOBS";
    sheet.appendRow(SCHEMAS[schemaType]);
    sheet.getRange(1, 1, 1, 9).setFontWeight("bold");
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