"""Shared helpers: config, workspace access by column name."""
import os
import re
from pathlib import Path

from openpyxl import load_workbook

BASE_DIR = Path(__file__).resolve().parent
WORKSPACE = BASE_DIR / "UH_Homes_Selections_Tracker.xlsx"


def load_env(path=BASE_DIR / ".env"):
    """Minimal .env loader (KEY=value lines) so no extra package is needed."""
    if not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
WORKSPACE = Path(os.getenv("UH_WORKSPACE", str(WORKSPACE)))
if not WORKSPACE.is_absolute():
    WORKSPACE = BASE_DIR / WORKSPACE


def clean(v):
    if v is None:
        return ""
    return str(v).strip()


def norm_model(s):
    """'K-596-VS' -> 'k596vs' so formatting differences don't break matching."""
    return re.sub(r"[^a-z0-9]", "", clean(s).lower())


class Sheet:
    """Access a sheet's rows as dicts keyed by header, and write back by header."""

    def __init__(self, ws):
        self.ws = ws
        self.cols = {clean(c.value): c.column for c in ws[1] if clean(c.value)}

    def rows(self):
        for r in range(2, self.ws.max_row + 1):
            data = {h: self.ws.cell(row=r, column=c).value for h, c in self.cols.items()}
            if any(clean(v) for k, v in data.items() if k not in AUTO_OR_FORMULA):
                yield r, data

    def set(self, row, header, value):
        self.ws.cell(row=row, column=self.cols[header], value=value)


# Columns ignored when deciding whether a row is "empty": computed totals, generated columns
# and the product-link metadata. A row holding only that (e.g. an orphan Product URL left
# behind by a row delete) carries no user data and must not count.
# The retired lookup columns stay listed on purpose. A workspace written before they were
# removed can still hold a stray "Not run", and that must not make an empty row look
# occupied in the window before the next save drops the column.
AUTO_OR_FORMULA = {"Total Items", "Links Found", "Verified", "Needs Review",
                   "Lookup Status", "Include in Lookbook", "Client Status",
                   "Checked On", "Lookup Notes", "Product URL", "Product Link",
                   "Image URL", "Product Name"}


def open_workspace(data_only=False):
    if not WORKSPACE.exists():
        raise SystemExit(f"Workspace not found: {WORKSPACE}")
    return load_workbook(WORKSPACE, data_only=data_only)


def manufacturer_domains(wb):
    out = {}
    for _, d in Sheet(wb["Manufacturers"]).rows():
        name, dom = clean(d.get("Manufacturer")), clean(d.get("Official Domain"))
        if name and dom:
            dom = re.sub(r"^https?://", "", dom).split("/")[0].removeprefix("www.")
            out[name.lower()] = dom.lower()
    return out


def section_order(wb):
    return [clean(r[0].value) for r in wb["Lists"].iter_rows(min_row=2, max_col=1) if clean(r[0].value)]


def location_order(wb):
    """The Locations list on Lists, in order - the schedule's grouping order."""
    ws = wb["Lists"]
    column = next((c.column for c in ws[1] if clean(c.value) == "Locations"), None)
    if column is None:
        return []
    return [clean(row[0]) for row in ws.iter_rows(min_row=2, min_col=column, max_col=column, values_only=True)
            if clean(row[0])]


def schedule_sort(rows, wb):
    """In-place finish-schedule order: the Locations list order, then entry order within a location.

    This belongs to the finish schedule alone, because that document prints Location headings.
    The spec sheet has none and is shown in workspace order, so sorting its rows here only
    reshuffles a download away from the screen it was exported from.
    """
    order = location_order(wb)
    rows.sort(key=lambda r: (order.index(clean(r.get("Location"))) if clean(r.get("Location")) in order
                             else len(order), clean(r.get("Location")), r["_row"]))
