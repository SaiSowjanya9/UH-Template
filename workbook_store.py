import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import threading
import zipfile
from copy import copy
from pathlib import Path
from urllib.parse import urlsplit

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from common import Sheet, clean

PROJECT_FIELDS = ["Project ID", "Project Name", "Client Name", "Address", "Plan / Elevation", "Designer", "Presentation Date", "Cover Image"]
SELECTION_FIELDS = ["Project ID", "Section", "Room / Area", "Item", "Manufacturer", "Model #", "Finish / Color", "Qty", "Product URL", "Product Name", "Image URL", "Lookup Status", "Checked On", "Lookup Notes", "Client Notes", "Include in Lookbook"]
STATUSES = ["Not run", "Found - verify", "Found - retailer", "Multiple matches", "Not found", "Search error", "Verified"]
IDENTITY_FIELDS = ["Item", "Product Name", "Manufacturer", "Model #", "Finish / Color"]
BACKUP_KEEP = 30
STALE_DAYS = 180

# Client-approval lifecycle: separate from Lookup Status, which only tracks link verification.
CLIENT_STATUSES = ["Proposed", "Presented", "Approved", "Rejected", "Changed"]
OPTIONAL_SELECTION_FIELDS = ["Client Status"]

# Computed Projects columns (H/I/J/K in the template); backfilled for Excel-added rows.
PROJECT_FORMULAS = {
    "Total Items": '=IF(A{row}="","",COUNTIF(Selections!$A:$A,A{row}))',
    "Links Found": '=IF(A{row}="","",COUNTIFS(Selections!$A:$A,A{row},Selections!$I:$I,"?*"))',
    "Verified": '=IF(A{row}="","",COUNTIFS(Selections!$A:$A,A{row},Selections!$L:$L,"Verified"))',
    "Needs Review": '=IF(A{row}="","",H{row}-J{row})',
}

# Selections dropdowns Excel silently drops when it re-saves the workbook.
LIST_VALIDATIONS = {
    "Project ID": "Projects!$A$2:$A$51",
    "Section": "Lists!$A$2:$A$31",
    "Lookup Status": "Lists!$B$2:$B$8",
    "Include in Lookbook": "Lists!$C$2:$C$3",
    "Client Status": "Lists!$D$2:$D$6",
}

# Lists columns the app owns, so the Excel dropdowns always offer every valid value.
MANAGED_LISTS = {"B": ("Lookup Status", STATUSES), "C": ("Yes / No", ["Yes", "No"]),
                 "D": ("Client Status", CLIENT_STATUSES)}


class ValidationError(ValueError):
    pass


class ConflictError(ValueError):
    pass


def http_url(value):
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def validate_values(data, fields):
    if not isinstance(data, dict) or set(data) - set(fields):
        raise ValidationError("Unexpected fields in the submitted data.")
    result = {}
    for key, value in data.items():
        if not isinstance(value, (str, int, float, type(None))):
            raise ValidationError(f"Invalid value for {key}.")
        value = clean(value)
        if len(value) > 4000 or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
            raise ValidationError(f"{key} contains invalid or overly long text.")
        if key == "Product URL" and value and not http_url(value):
            raise ValidationError("Product URL must be a full HTTP or HTTPS link.")
        if key == "Qty" and value:
            try:
                qty = float(value)
                if not 0 <= qty <= 1_000_000:
                    raise ValueError()
            except ValueError:
                raise ValidationError("Quantity must be a number between 0 and 1,000,000.") from None
        result[key] = value
    return result


def put(sheet, row, data):
    for key, value in data.items():
        cell = sheet.ws.cell(row, sheet.cols[key])
        cell.value = value or None
        if isinstance(value, str) and value:
            cell.data_type = "s"
        if key == "Product URL":
            cell.hyperlink = value or None


def records(wb, name):
    return [{**{k: v.isoformat() if isinstance(v, (dt.datetime, dt.date)) else clean(v) for k, v in data.items()}, "_row": row}
            for row, data in Sheet(wb[name]).rows()]


def validate_custom_fields(fields, reserved=()):
    if not isinstance(fields, list) or len(fields) > 30:
        raise ValidationError("Custom fields must be a list of up to 30 name/value pairs.")
    result, names = [], set()
    protected = {name.casefold() for name in [*reserved, "Custom Fields"]}
    for field in fields:
        if not isinstance(field, dict) or set(field) != {"name", "value"}:
            raise ValidationError("Each custom field needs a name and value.")
        if not all(isinstance(field[key], str) for key in ("name", "value")):
            raise ValidationError("Custom field names and values must be text.")
        checked = validate_values(field, ["name", "value"])
        name, value = checked["name"], checked["value"]
        if not name or len(name) > 80 or "\n" in name or "\r" in name:
            raise ValidationError("Field names must contain 1–80 characters on one line.")
        if len(value) > 2000:
            raise ValidationError("A custom field value cannot exceed 2,000 characters.")
        if name.casefold() in names or name.casefold() in protected:
            raise ValidationError(f"Field name '{name}' is duplicated or already used by a standard field.")
        names.add(name.casefold())
        result.append({"name": name, "value": value})
    if len(json.dumps(result, ensure_ascii=False).encode("utf-16-le")) // 2 > 30000:
        raise ValidationError("The combined custom fields are too long for one workbook record.")
    return result


def read_custom_fields(value, reserved=()):
    if not clean(value):
        return []
    try:
        fields = json.loads(value)
    except (ValueError, TypeError):
        raise ValidationError("Invalid Custom Fields data. Edit these fields in the web app or import an unmodified export.") from None
    return validate_custom_fields(fields, reserved)


def detailed_records(wb, name):
    reserved = PROJECT_FIELDS if name == "Projects" else SELECTION_FIELDS + OPTIONAL_SELECTION_FIELDS
    return [{**row, "custom_fields": read_custom_fields(row.get("Custom Fields"), reserved)} for row in records(wb, name)]


def presentation_details(project, rows):
    groups = []
    fields = project.get("custom_fields", read_custom_fields(project.get("Custom Fields"), PROJECT_FIELDS))
    if fields:
        groups.append(("Project details", fields))
    for index, row in enumerate(rows, start=1):
        fields = row.get("custom_fields", read_custom_fields(row.get("Custom Fields"), SELECTION_FIELDS))
        if fields:
            heading = f"Selection {index}: {row.get('Item', '')}"
            context = " / ".join(filter(None, [row.get("Section"), row.get("Room / Area"), row.get("Model #")]))
            groups.append((heading + (f" — {context}" if context else ""), fields))
    return groups


def set_custom_fields(wb, name, row, fields):
    reserved = PROJECT_FIELDS if name == "Projects" else SELECTION_FIELDS
    fields = validate_custom_fields(fields, reserved)
    sheet = Sheet(wb[name])
    if not fields and "Custom Fields" not in sheet.cols:
        return
    if "Custom Fields" not in sheet.cols:
        column = sheet.ws.max_column + 1
        if column > 50:
            raise ValidationError("This worksheet has no space for the Custom Fields column.")
        cell = sheet.ws.cell(1, column, "Custom Fields")
        cell._style = copy(sheet.ws.cell(1, 1)._style)
        sheet.ws.column_dimensions[get_column_letter(column)].width = 50
        sheet = Sheet(sheet.ws)
    value = json.dumps(fields, ensure_ascii=False) if fields else ""
    put(sheet, row, {"Custom Fields": value})
    cell = sheet.ws.cell(row, sheet.cols["Custom Fields"])
    cell.alignment = copy(sheet.ws.cell(row, sheet.cols.get("Client Notes", 2)).alignment)
    sheet.ws.auto_filter.ref = f"A1:{get_column_letter(sheet.ws.max_column)}{sheet.ws.max_row}"


def next_row(sheet):
    used = {row for row, _ in sheet.rows()}
    row = 2
    while row in used:
        row += 1
    if row > 10001:
        raise ValidationError("This local workbook supports up to 10,000 rows per sheet.")
    if row > 2:
        for col in sheet.cols.values():
            sheet.ws.cell(row, col)._style = copy(sheet.ws.cell(2, col)._style)
    return row


def validate_workbook(wb):
    required = {"Projects": PROJECT_FIELDS, "Selections": SELECTION_FIELDS,
                "Manufacturers": ["Manufacturer", "Official Domain"], "Lists": ["Sections (lookbook order)"]}
    for name, fields in required.items():
        if name not in wb:
            raise ValidationError(f"Missing worksheet: {name}.")
        ws = wb[name]
        if ws.max_row > 10001 or ws.max_column > 50:
            raise ValidationError(f"Worksheet {name} is too large (maximum 10,000 rows and 50 columns).")
        headers = [clean(c.value) for c in ws[1] if clean(c.value)]
        if len(set(headers)) != len(headers):
            raise ValidationError(f"Duplicate column names in {name}.")
        missing = set(fields) - set(headers)
        if missing:
            raise ValidationError(f"Missing columns in {name}: {', '.join(sorted(missing))}.")
        sheet = Sheet(ws)
        for row, data in sheet.rows():
            if name in {"Projects", "Selections"} and "Custom Fields" in sheet.cols:
                if ws.cell(row, sheet.cols["Custom Fields"]).data_type == "f":
                    raise ValidationError(f"Formulas are not allowed in Custom Fields: {name}, row {row}.")
                read_custom_fields(data.get("Custom Fields"), fields)
            for field in fields:
                if ws.cell(row, sheet.cols[field]).data_type == "f":
                    raise ValidationError(f"Formulas are not allowed in input cells: {name}, row {row}, {field}.")
    projects = records(wb, "Projects")
    ids = [p["Project ID"] for p in projects]
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}", pid) for pid in ids):
        raise ValidationError("Project IDs must be 1–50 letters, numbers, hyphens, or underscores.")
    if len(ids) != len(set(ids)):
        raise ValidationError("Project IDs must be unique.")
    if any(not p["Project Name"] for p in projects):
        raise ValidationError("Every project needs a name.")
    for row in records(wb, "Selections"):
        if row["Project ID"] not in ids:
            raise ValidationError(f"Selections row {row['_row']} references an unknown Project ID.")
        validate_values({key: row[key] for key in SELECTION_FIELDS}, SELECTION_FIELDS)
        if row["Lookup Status"] and row["Lookup Status"] not in STATUSES:
            raise ValidationError(f"Unknown lookup status in row {row['_row']}.")
        if clean(row.get("Client Status")) and row["Client Status"] not in CLIENT_STATUSES:
            raise ValidationError(f"Unknown client status in row {row['_row']}.")
        if row["Lookup Status"] == "Verified" and not http_url(row["Product URL"]):
            raise ValidationError(f"Verified row {row['_row']} needs a product URL.")
        if row["Include in Lookbook"].lower() not in {"", "yes", "no"}:
            raise ValidationError(f"Include in Lookbook must be Yes or No in row {row['_row']}.")


def workbook_open_elsewhere(path):
    """True when another process (e.g. Excel) holds the file open for writing.

    Probing the file itself is more reliable than looking for a '~$' lock file,
    which Excel can leave behind after a crash and other editors never create.
    """
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True
    except FileNotFoundError:
        return False


def stale_check(row, today=None):
    """A Verified link stops being trustworthy once the product page may have moved on."""
    if clean(row.get("Lookup Status")) != "Verified":
        return False
    checked = clean(row.get("Checked On"))
    if not checked:
        return True
    try:
        return (today or dt.date.today()) - dt.date.fromisoformat(checked[:10]) > dt.timedelta(days=STALE_DAYS)
    except ValueError:
        return False


def ensure_selection_columns(wb):
    """Add the optional Client Status column when the workbook predates it. Returns a fresh Sheet."""
    ws = wb["Selections"]
    sheet = Sheet(ws)
    if "Client Status" in sheet.cols:
        return sheet
    column = ws.max_column + 1
    if column > 50:
        raise ValidationError("This worksheet has no space for the Client Status column.")
    cell = ws.cell(1, column, "Client Status")
    cell._style = copy(ws.cell(1, 1)._style)
    ws.column_dimensions[get_column_letter(column)].width = 16
    ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"
    return Sheet(ws)


def ensure_list_values(wb):
    """Keep the status columns on Lists complete, so Excel's dropdowns offer every valid value.

    Column A (sections) belongs to the user and is never rewritten.
    """
    ws = wb["Lists"]
    for letter, (header, values) in MANAGED_LISTS.items():
        column = ws[f"{letter}1"].column
        if clean(ws.cell(1, column).value) != header:
            cell = ws.cell(1, column, header)
            cell._style = copy(ws.cell(1, 1)._style)
            ws.column_dimensions[letter].width = max(ws.column_dimensions[letter].width or 0, 20)
        for index, value in enumerate(values, start=2):
            if clean(ws.cell(index, column).value) != value:
                ws.cell(index, column, value)
        for extra in range(len(values) + 2, ws.max_row + 1):
            if clean(ws.cell(extra, column).value):
                ws.cell(extra, column, None)


def ensure_validations(wb):
    """Re-add the Selections dropdowns Excel silently drops when it re-saves.

    Validations on the managed columns are replaced rather than appended, so a workbook
    saved by an older version cannot end up with two conflicting lists on one column.
    """
    ws = wb["Selections"]
    sheet = Sheet(ws)
    managed = {sheet.cols[header] for header in LIST_VALIDATIONS if header in sheet.cols}

    def owns(dv):
        ranges = list(dv.sqref.ranges) if dv.sqref else []
        return bool(ranges) and all(r.min_col == r.max_col and r.min_col in managed for r in ranges)

    for dv in [dv for dv in ws.data_validations.dataValidation if dv.type == "list" and owns(dv)]:
        ws.data_validations.dataValidation.remove(dv)
    last = max(501, ws.max_row)
    for header, formula in LIST_VALIDATIONS.items():
        column = sheet.cols.get(header)
        if column is None:
            continue
        letter = get_column_letter(column)
        dv = DataValidation(type="list", formula1=formula, allow_blank=True, showErrorMessage=True)
        dv.add(f"{letter}2:{letter}{last}")
        ws.add_data_validation(dv)


def ensure_project_formulas(wb):
    """Backfill the computed count columns for project rows added outside the app."""
    sheet = Sheet(wb["Projects"])
    targets = {field: sheet.cols[field] for field in PROJECT_FORMULAS if field in sheet.cols}
    for row, _ in sheet.rows():
        for field, column in targets.items():
            if not clean(sheet.ws.cell(row, column).value):
                sheet.ws.cell(row, column, PROJECT_FORMULAS[field].format(row=row))


def delete_record(wb, name, row):
    ws = wb[name]
    ws.delete_rows(row)
    ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"


class WorkbookStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def snapshot(self):
        with self.lock:
            raw = self.path.read_bytes()
            wb = load_workbook(io.BytesIO(raw), keep_links=False)
            validate_workbook(wb)
            return wb, hashlib.sha256(raw).hexdigest()

    def _history(self, backup_dir, note):
        entry = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "note": note or "save"}
        with (backup_dir / "history.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _prune_backups(self, backup_dir):
        copies = sorted(backup_dir.glob(f"{self.path.stem}_*.xlsx"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
        for old in copies[BACKUP_KEEP:]:
            old.unlink(missing_ok=True)

    def save(self, wb, revision, note=""):
        with self.lock:
            validate_workbook(wb)
            if hashlib.sha256(self.path.read_bytes()).hexdigest() != revision:
                raise ConflictError("The workbook changed. Refresh the page before saving again.")
            if workbook_open_elsewhere(self.path):
                raise PermissionError("The workbook is open in another program.")
            backup_dir = self.path.parent / "backups"
            backup_dir.mkdir(exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            shutil.copy2(self.path, backup_dir / f"{self.path.stem}_{stamp}.xlsx")
            ensure_selection_columns(wb)
            ensure_list_values(wb)
            ensure_validations(wb)
            ensure_project_formulas(wb)
            handle, temp = tempfile.mkstemp(suffix=".xlsx", dir=self.path.parent)
            os.close(handle)
            try:
                wb.save(temp)
                if hashlib.sha256(self.path.read_bytes()).hexdigest() != revision:
                    raise ConflictError("The workbook changed during the save. Refresh and retry.")
                os.replace(temp, self.path)
            finally:
                Path(temp).unlink(missing_ok=True)
            self._prune_backups(backup_dir)
            self._history(backup_dir, note)

    def log(self, note):
        """Append a history entry without touching the workbook (e.g. an export with no row changes)."""
        with self.lock:
            backup_dir = self.path.parent / "backups"
            backup_dir.mkdir(exist_ok=True)
            self._history(backup_dir, note)

    def count_notes(self, prefix):
        """How many history entries start with prefix - used to number export revisions."""
        path = self.path.parent / "backups" / "history.jsonl"
        if not path.exists():
            return 0
        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                if json.loads(line).get("note", "").startswith(prefix):
                    count += 1
            except ValueError:
                continue
        return count

    def import_bytes(self, raw, revision):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if sum(info.file_size for info in archive.infolist()) > 50_000_000:
                    raise ValidationError("The uncompressed workbook exceeds 50 MB.")
                if any(info.filename.lower().endswith("vbaproject.bin") for info in archive.infolist()):
                    raise ValidationError("Macro-enabled workbooks are not supported.")
            wb = load_workbook(io.BytesIO(raw), keep_links=False)
        except ValidationError:
            raise
        except Exception:
            raise ValidationError("This file could not be read as an Excel .xlsx workbook.") from None
        self.save(wb, revision, "workbook import")
