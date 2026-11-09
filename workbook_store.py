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
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from common import Sheet, clean

PROJECT_FIELDS = ["Project ID", "Project Name", "Client Name", "Address", "Plan / Elevation", "Designer", "Presentation Date", "Cover Image"]
SELECTION_FIELDS = ["Project ID", "Section", "Room / Area", "Item", "Manufacturer", "Model #", "Finish / Color", "Qty", "Product URL", "Product Name", "Image URL", "Lookup Status", "Checked On", "Lookup Notes", "Client Notes", "Include in Lookbook"]
STATUSES = ["Not run", "Found - verify", "Found - retailer", "Multiple matches", "Not found", "Search error", "Verified"]
IDENTITY_FIELDS = ["Item", "Product Name", "Manufacturer", "Model #", "Finish / Color"]
BACKUP_KEEP = 30
STALE_DAYS = 180
# Saved copies are named <workbook stem>_<date>_<time>_<microseconds>.xlsx
BACKUP_PATTERN = re.compile(r"^[A-Za-z0-9_-]+_(\d{8})_(\d{6})_(\d+)\.xlsx$")

# Client-approval lifecycle: separate from Lookup Status, which only tracks link verification.
CLIENT_STATUSES = ["Proposed", "Presented", "Approved", "Rejected", "Changed"]
OPTIONAL_SELECTION_FIELDS = ["Client Status", "Unit Price", "Markup %", "Description Override", "Location"]
# Where in the home a selection belongs - the schedule's grouping. Kept out of Section so
# Section stays a pure trade category; the seeded list lives on Lists under "Locations".
LOCATIONS = ["1st Floor", "2nd Floor", "Outdoor Living", "Garage", "Bulk Items"]
LOCATIONS_HEADER = "Locations"
MAX_LOCATIONS = 30          # the Location dropdown covers Lists!E2:E31
# Generated Selections column: the clickable face of Product URL, rebuilt on every save.
# The header and the words inside the cell are deliberately the same, so a reader
# clicks "Product Link" under "Product Link" instead of reading a raw address.
PRODUCT_LINK = "Product Link"
GENERATED_SELECTION_FIELDS = [PRODUCT_LINK]

# A Cover Image cell holding "cover:<name>" points at a file the app stored: ./covers beside the
# local workbook, objects under covers/ in the hosted bucket. Plain URLs and file paths still work.
COVER_PREFIX = "cover:"
COVER_NAME = re.compile(r"^cover-[A-Za-z0-9_-]{1,50}\.(png|jpg)$")
LINK_COLOR = "0563C1"           # Excel's own hyperlink blue
# Numeric input columns: (maximum, message)
NUMERIC_FIELDS = {"Qty": (1_000_000, "Quantity must be a number between 0 and 1,000,000."),
                  "Unit Price": (100_000_000, "Unit Price must be a number between 0 and 100,000,000."),
                  "Markup %": (1_000, "Markup % must be a number between 0 and 1,000.")}
PRICE_SHEET = "Price Schedule"

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
    "Location": "Lists!$E$2:$E$31",
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


def hyperlink_formula(url):
    """A product URL as a spreadsheet formula that reads 'Product Link' and opens the page.

    CSV carries no hyperlinks of its own, so the exported spec sheet hands Excel and Sheets
    a formula instead. Only a validated http(s) address is ever written, so the leading '='
    is always ours and never text somebody typed into a cell.
    """
    url = clean(url)
    if not http_url(url):
        return ""
    return f'=HYPERLINK("{url.replace(chr(34), chr(34) * 2)}","{PRODUCT_LINK}")'


def parse_hyperlink(value):
    """The address inside a HYPERLINK formula, or the cell's own text when it is a URL."""
    value = clean(value)
    match = re.match(r'^=HYPERLINK\(\s*"(.*?)"\s*(?:[,;].*)?\)$', value, re.IGNORECASE | re.DOTALL)
    if match:
        value = match[1].replace('""', '"')
    return value if http_url(value) else ""


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
        if key in NUMERIC_FIELDS and value:
            limit, message = NUMERIC_FIELDS[key]
            try:
                if not 0 <= float(value) <= limit:
                    raise ValueError()
            except ValueError:
                raise ValidationError(message) from None
        result[key] = value
    return result


def put(sheet, row, data):
    for key, value in data.items():
        cell = sheet.ws.cell(row, sheet.cols[key])
        if key in NUMERIC_FIELDS and clean(value):
            # Stored as a real number so Excel's SUM and ISNUMBER work on the Price Schedule.
            number = float(value)
            cell.value = int(number) if number.is_integer() else number
            if key in {"Unit Price", "Markup %"}:
                cell.number_format = '#,##0.00' if key == "Unit Price" else '0.##'
            continue
        cell.value = value or None
        if isinstance(value, str) and value:
            cell.data_type = "s"
        if key == "Product URL":
            cell.hyperlink = value or None


def records(wb, name):
    # Generated columns are left out: they only restate a value the record already carries,
    # so reading one back would make a workbook saved before they existed look different.
    return [{**{k: v.isoformat() if isinstance(v, (dt.datetime, dt.date)) else clean(v)
                for k, v in data.items() if k not in GENERATED_SELECTION_FIELDS}, "_row": row}
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
    reserved = PROJECT_FIELDS if name == "Projects" else SELECTION_FIELDS + OPTIONAL_SELECTION_FIELDS + GENERATED_SELECTION_FIELDS
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
            context = " / ".join(filter(None, [row.get("Location"), row.get("Room / Area"), row.get("Model #")]))
            groups.append((heading + (f" — {context}" if context else ""), fields))
    return groups


def set_custom_fields(wb, name, row, fields):
    reserved = PROJECT_FIELDS if name == "Projects" else SELECTION_FIELDS + GENERATED_SELECTION_FIELDS
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
    """Add the optional Selections columns when the workbook predates them. Returns a fresh Sheet."""
    ws = wb["Selections"]
    sheet = Sheet(ws)
    added = False
    for header in OPTIONAL_SELECTION_FIELDS:
        if header in sheet.cols:
            continue
        column = ws.max_column + 1
        if column > 50:
            raise ValidationError(f"This worksheet has no space for the {header} column.")
        cell = ws.cell(1, column, header)
        cell._style = copy(ws.cell(1, 1)._style)
        ws.column_dimensions[get_column_letter(column)].width = 16
        if header in {"Unit Price", "Markup %"}:
            fmt = '#,##0.00' if header == "Unit Price" else '0.##'
            for row in range(2, max(ws.max_row, 2) + 1):
                ws.cell(row, column).number_format = fmt
        sheet, added = Sheet(ws), True
    if added:
        ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"
    return sheet


def ensure_product_links(wb):
    """Rewrite the generated 'Product Link' column from each row's Product URL.

    Product URL stays the column anything writes to - the app, the CSV import, automatic
    lookup - and this one is only its readable face, so it is rebuilt from scratch on every
    save and anything typed into it by hand is discarded.
    """
    ws = wb["Selections"]
    sheet = Sheet(ws)
    if PRODUCT_LINK not in sheet.cols:
        column = ws.max_column + 1
        if column > 50:
            raise ValidationError(f"This worksheet has no space for the {PRODUCT_LINK} column.")
        cell = ws.cell(1, column, PRODUCT_LINK)
        cell._style = copy(ws.cell(1, 1)._style)
        ws.column_dimensions[get_column_letter(column)].width = 16
        sheet = Sheet(ws)
        ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"
    target, source = sheet.cols[PRODUCT_LINK], sheet.cols["Product URL"]
    for row in range(2, ws.max_row + 1):
        origin = ws.cell(row, source)
        url = clean(origin.value)
        cell = ws.cell(row, target)
        cell._style = copy(origin._style)
        if not http_url(url):
            cell.value, cell.hyperlink = None, None
            continue
        cell.value = PRODUCT_LINK
        cell.hyperlink = url
        cell.font = Font(name=origin.font.name, size=origin.font.size, color=LINK_COLOR, underline="single")


def infer_category(room, item):
    """Best-effort trade category for a row that was filed under a floor or zone.

    Used when migrating location-style Sections onto the Location field and when the spec
    template seeds line items, so both paths land on the same category names. Anything the
    rules cannot place falls back to "Other".
    """
    text = clean(item).lower()

    def has(*words):
        return any(word in text for word in words)

    if has("flooring"):
        return "Flooring"
    if has("paint"):
        return "Paint"
    if has("light", "chandelier", "pendant", "sconce"):
        return "Lighting"
    if has("roof"):
        return "Roofing"
    if has("landscap", "masonry", "garage door", "balcony", "siding", "porch column"):
        return "Exterior"
    if has("window", "exterior door", "front door", "door - exterior", "doors - exterior"):
        return "Windows & Doors"
    if has("stopper", "knob", "hinge", "hardware") or ("pull" in text and "pullout" not in text and "pull-out" not in text):
        return "Hardware"
    if has("door", "staircase", "baseboard", "trim", "bookcase", "mud bench", "millwork", "wine cellar"):
        return "Interior Doors & Trim"
    if has("cooktop", "range", "oven", "microwave", "dishwasher", "venthood", "vent hood",
           "refrigerator", "exhaust fan", "washer", "dryer", "freezer"):
        return "Appliances"
    if has("kitchen", "coffee bar", "trash pullout"):
        return "Kitchen"
    if has("toilet", "towel", "vanity", "shower", "tub", "bath"):
        return "Bathrooms"
    # Ambiguous fixtures - sinks, faucets, countertops, backsplash, cabinets - follow the room.
    area = clean(room).lower()
    if any(word in area for word in ("kitchen", "pantry", "coffee")):
        return "Kitchen"
    if any(word in area for word in ("bath", "powder", "laundry")):
        return "Bathrooms"
    return "Other"


def location_list(ws):
    """The Lists column headed 'Locations', as (column, names in order)."""
    column = next((cell.column for cell in ws[1] if clean(cell.value) == LOCATIONS_HEADER), None)
    if column is None:
        return None, []
    names = [clean(row[0]) for row in ws.iter_rows(min_row=2, min_col=column, max_col=column, values_only=True)]
    return column, [name for name in names if name]


def ensure_location_list(wb):
    """Seed the 'Locations' column on Lists so the Location dropdown has values.

    Like the sections column, the list belongs to the user afterwards: existing entries are
    never rewritten or removed here.
    """
    ws = wb["Lists"]
    column, names = location_list(ws)
    if column is not None and names:
        return column
    if column is None:
        column = ws.max_column + 1
        if column > 50:
            raise ValidationError("The Lists sheet has no space for the Locations column.")
        cell = ws.cell(1, column, LOCATIONS_HEADER)
        cell._style = copy(ws.cell(1, 1)._style)
        ws.column_dimensions[get_column_letter(column)].width = max(
            ws.column_dimensions[get_column_letter(column)].width or 0, 20)
    for index, name in enumerate(LOCATIONS, start=2):
        if clean(ws.cell(index, column).value) != name:
            ws.cell(index, column, name)
    return column


def ensure_locations(wb):
    """Move floor/zone Sections onto the Location column and re-categorise those rows.

    A Section that names a Locations entry is a location, not a trade category: the row's
    Location takes the value (unless it already has one) and its Section is re-inferred from
    the room and item. Location names are also stripped from the sections list so the
    Section dropdown stops offering them. Runs on every save, so workbooks edited directly
    in Excel migrate the same way.
    """
    ensure_location_list(wb)
    ws = wb["Lists"]
    _, locations = location_list(ws)
    names = set(locations) | set(LOCATIONS)

    # Repack the sections column without the location names, preserving user order.
    sections = [clean(row[0]) for row in ws.iter_rows(min_row=2, max_col=1, values_only=True)]
    kept = [name for name in sections if name and name not in names]
    if len(kept) != len([name for name in sections if name]):
        for index, name in enumerate(kept, start=2):
            ws.cell(index, 1, name)
        for index in range(len(kept) + 2, len(sections) + 2):
            ws.cell(index, 1).value = None   # ws.cell(..., None) does not write

    sheet = ensure_selection_columns(wb)
    if "Location" not in sheet.cols:
        return
    section_col, location_col = sheet.cols["Section"], sheet.cols["Location"]
    room_col, item_col = sheet.cols["Room / Area"], sheet.cols["Item"]
    for row in range(2, sheet.ws.max_row + 1):
        section = clean(sheet.ws.cell(row, section_col).value)
        if section not in names:
            continue
        if not clean(sheet.ws.cell(row, location_col).value):
            sheet.ws.cell(row, location_col, section)
        sheet.ws.cell(row, section_col, infer_category(
            sheet.ws.cell(row, room_col).value, sheet.ws.cell(row, item_col).value))


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


def history_entry(note):
    return {"ts": dt.datetime.now().isoformat(timespec="seconds"), "note": note or "save"}


class LocalBackend:
    """The master workbook as a file beside the app: Excel can open it, backups sit in ./backups.

    This is the single-user arrangement the tracker was built for. Concurrency rests on the
    revision hash alone, so only one process may write at a time.
    """

    remote = False

    def __init__(self, path):
        self.path = Path(path)

    @property
    def name(self):
        return self.path.name

    @property
    def backup_dir(self):
        return self.path.parent / "backups"

    def read(self):
        raw = self.path.read_bytes()
        return raw, hashlib.sha256(raw).hexdigest()

    def revision(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def locked_elsewhere(self):
        return workbook_open_elsewhere(self.path)

    def backup(self):
        self.backup_dir.mkdir(exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        shutil.copy2(self.path, self.backup_dir / f"{self.path.stem}_{stamp}.xlsx")

    def _copies(self):
        """Saved copies newest first, ordered by the microseconds in their names.

        Several saves can land in the same second, so neither the second-precision
        timestamp nor the filesystem's modification time can order them reliably.
        """
        found = []
        for path in self.backup_dir.glob(f"{self.path.stem}_*.xlsx"):
            match = BACKUP_PATTERN.match(path.name)
            if match:
                found.append(((match[1], match[2], int(match[3])), path, match))
        found.sort(key=lambda entry: entry[0], reverse=True)
        return found

    def write(self, wb, revision):
        handle, temp = tempfile.mkstemp(suffix=".xlsx", dir=self.path.parent)
        os.close(handle)
        try:
            wb.save(temp)
            if self.revision() != revision:
                raise ConflictError("The workbook changed during the save. Refresh and retry.")
            os.replace(temp, self.path)
        finally:
            Path(temp).unlink(missing_ok=True)
        for _, old, _ in self._copies()[BACKUP_KEEP:]:
            old.unlink(missing_ok=True)

    def log(self, note):
        self.backup_dir.mkdir(exist_ok=True)
        with (self.backup_dir / "history.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(history_entry(note), ensure_ascii=False) + "\n")

    def count_notes(self, prefix):
        path = self.backup_dir / "history.jsonl"
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

    def backups(self):
        out = []
        for _, path, match in self._copies():
            try:
                when = dt.datetime.strptime(match[1] + match[2], "%Y%m%d%H%M%S")
            except ValueError:
                continue
            out.append({"name": path.name, "when": when.isoformat(timespec="seconds"), "size": path.stat().st_size})
        return out

    def read_backup(self, name):
        path = self.backup_dir / name
        if path.parent.resolve() != self.backup_dir.resolve() or not path.is_file():
            raise ValidationError("That saved copy is no longer available. Refresh and try again.")
        return path.read_bytes()

    @property
    def cover_dir(self):
        return self.path.parent / "covers"

    def store_cover(self, name, raw, content_type="image/png"):
        self.cover_dir.mkdir(exist_ok=True)
        (self.cover_dir / name).write_bytes(raw)

    def read_cover(self, name):
        path = self.cover_dir / name
        if path.parent.resolve() != self.cover_dir.resolve() or not path.is_file():
            raise ValidationError("That cover image is no longer on disk. Upload it again.")
        return path.read_bytes()


class WorkbookStore:
    """Validation, repair and compare-and-swap around whichever backend holds the workbook."""

    def __init__(self, backend):
        self.backend = LocalBackend(backend) if isinstance(backend, (str, os.PathLike)) else backend
        self.lock = threading.RLock()

    @property
    def path(self):
        """The workbook file, for the local backend only (Excel integration, tests)."""
        return self.backend.path

    @property
    def name(self):
        return self.backend.name

    @property
    def remote(self):
        return self.backend.remote

    def read_bytes(self):
        with self.lock:
            return self.backend.read()[0]

    def locked_elsewhere(self):
        return self.backend.locked_elsewhere()

    def snapshot(self):
        with self.lock:
            raw, revision = self.backend.read()
            wb = load_workbook(io.BytesIO(raw), keep_links=False)
            validate_workbook(wb)
            # Views and exports see location data even before the next save migrates the file.
            ensure_locations(wb)
            return wb, revision

    def save(self, wb, revision, note=""):
        with self.lock:
            validate_workbook(wb)
            if self.backend.revision() != revision:
                raise ConflictError("The workbook changed. Refresh the page before saving again.")
            if self.backend.locked_elsewhere():
                raise PermissionError("The workbook is open in another program.")
            self.backend.backup()
            ensure_selection_columns(wb)
            ensure_locations(wb)
            ensure_product_links(wb)
            ensure_list_values(wb)
            ensure_validations(wb)
            ensure_project_formulas(wb)
            from price_schedule import rebuild   # local import: price_schedule reads build_lookbook
            rebuild(wb)
            self.backend.write(wb, revision)
            self.backend.log(note)

    def log(self, note):
        """Append a history entry without touching the workbook (e.g. an export with no row changes)."""
        with self.lock:
            self.backend.log(note)

    def count_notes(self, prefix):
        """How many history entries start with prefix - used to number export revisions."""
        return self.backend.count_notes(prefix)

    def backups(self):
        """Earlier copies kept automatically before each save, newest first."""
        return self.backend.backups()

    def read_backup(self, name):
        if not BACKUP_PATTERN.match(name):
            raise ValidationError("Choose one of the saved copies listed here.")
        return self.backend.read_backup(name)

    def store_cover(self, name, raw, content_type="image/png"):
        """A cover image lives outside the workbook, so it survives unchanged across saves."""
        if not COVER_NAME.match(name):
            raise ValidationError("Invalid cover image name.")
        self.backend.store_cover(name, raw, content_type)

    def read_cover(self, name):
        if not COVER_NAME.match(name):
            raise ValidationError("Invalid cover image name.")
        return self.backend.read_cover(name)

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
