"""
Build the client finish schedule PDF from the tracker workspace.

Usage:
    python build_lookbook.py --project UH-101                              # one project
    python build_lookbook.py --all                                         # every project
    python build_lookbook.py --project UH-101 --draft                      # internal copy (watermarked)
    python build_lookbook.py --project UH-101 --title "Interior Selections" --prefix IN
    python build_lookbook.py --project UH-101 --spec                       # internal priced spec sheet
    python build_lookbook.py --project UH-101 --hide "Manufacturer,Qty"      # leave columns out

Output: output/<ProjectID>_<Project Name>_<Schedule title>.pdf

The layout follows the approved UH Homes finish schedule: letter portrait, wordmark and
schedule title over a ruled header, a client/project band, letter-spaced ruled section
headings, one row per selection (code / item / description), the client note and a
sign-off block. Brand colors, fonts, logo, contact line and the schedule defaults
(title, code prefix, note, signature labels) live in lookbook_config.json.
"""
import argparse
import datetime as dt
import io
import ipaddress
import json
import re
import socket
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import requests
from PIL import Image
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, KeepTogether, NextPageTemplate,
                                PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle)

from branding import load_logo
from common import BASE_DIR, WORKSPACE, Sheet, clean, open_workspace, location_order
from workspace_store import COVER_PREFIX, PRODUCT_LINK, http_url, presentation_details

PAGE_W, PAGE_H = letter          # 612 x 792 pt, portrait
M = 46.8                         # side margins
CONTENT_W = PAGE_W - 2 * M       # 518.4
BODY_TOP, BODY_BOTTOM = 696, 56  # flowing content area between the header and footer rules
HEAD_RULE_Y, FOOT_RULE_Y = 720, 44.64
LOGO_H, MARK_W = 20.16, 11.52
FOOT_SIZE, FOOT_BASE = 7.5, 28.8     # footer type size, and the baseline it is set on
MARK_GAP = 6.48                      # gap between the footer symbol and the contact line
CONTACT_X = M + MARK_W + MARK_GAP    # where the contact line starts, clear of the symbol
CODE_W, ITEM_W = 50.4, 144
PAD_X, PAD_Y = 6, 6.5            # table cell padding
BAND_H, FIELD_W, FIELD_GAP = 56, 113.4, 14.4
BAND_GAP, SECTION_GAP = 14, 10   # space below the band / above a section heading
HEADING_H, HEADING_GAP = 15.84, 10   # heading block height, gap between its text and its rule
HEADING_OVERHANG = 6                 # section-heading rules run past the right margin
SIGN_W = [172.8, 79.2, 172.8, 79.2]
SIGN_H, SIGN_X = 42.48, 7.2
BAND_FIELDS = ["CLIENT", "PROJECT / LOT", "ADDRESS", "DATE"]
OUT = BASE_DIR / "output"


# --------------------------------------------------------------------------
# Config / fonts
# --------------------------------------------------------------------------
CFG = json.loads((BASE_DIR / "lookbook_config.json").read_text(encoding="utf-8"))
C = {key: HexColor(value) for key, value in CFG["colors"].items()}
SCHEDULE = CFG.get("schedule", {})
FONT_H, FONT_B, FONT_BB = "Helvetica", "Helvetica", "Helvetica-Bold"
for key, attr, name in [("heading", "FONT_H", "BrandHeading"), ("body", "FONT_B", "BrandBody"),
                        ("body_bold", "FONT_BB", "BrandBodyBold")]:
    path = CFG.get("fonts", {}).get(key)
    if path and (BASE_DIR / path).exists():
        pdfmetrics.registerFont(TTFont(name, str(BASE_DIR / path)))
        globals()[attr] = name


def style(name, size, leading, color, font=None, align=None, **kwargs):
    return ParagraphStyle(name, fontName=font or FONT_B, fontSize=size, leading=leading,
                          textColor=color, alignment=align or 0, **kwargs)


CODE_STYLE = style("code", 9, 13, C["muted"])
ITEM_STYLE = style("item", 10, 13, C["dark"])
ROOM_STYLE = style("room", 8, 10.5, C["muted"])
DESC_STYLE = style("description", 10, 13, C["body"])
GROUP_STYLE = style("group", 10, 13, C["dark"], font=FONT_BB, spaceBefore=8, spaceAfter=3, leftIndent=PAD_X)
NOTE_STYLE = style("note", 8, 10.5, C["muted"], leftIndent=PAD_X, rightIndent=PAD_X)
SPEC_HEAD_STYLE = style("spec_head", 6.5, 9, C["muted"], font=FONT_BB)
SPEC_CELL_STYLE = style("spec_cell", 8, 10.5, C["body"])
SPEC_NOTE_STYLE = style("spec_note", 6.5, 9, C["muted"])

SPEC_TITLE = "Spec Sheet"
# The printed spec sheet mirrors the app's spec-sheet table, column for column, so the
# download carries exactly the line items and headings on screen. The grouping field becomes
# the heading and the lookbook flag a note under the item. (column, width share, the hideable
# key that drops it); shares are rescaled to what is shown, and build_word.build_spec lays
# the same registry out in Word.
SPEC_COLUMNS = [("Item", 120, "Item"), ("Manufacturer", 90, "Manufacturer"),
                ("Model #", 84, "Model #"), ("Finish / Color", 92, "Finish / Color"),
                (PRODUCT_LINK, 60, "Product URL"), ("Qty", 30, "Qty"),
                ("Unit Price", 50, "Unit Price"), ("Markup %", 46, "Markup %")]
# Landscape spec sheet: a letter page turned on its side, so the wide column set fits.
LAND_W, LAND_H = PAGE_H, PAGE_W           # 792 x 612
LAND_CONTENT_W = LAND_W - 2 * M           # 698.4
LAND_BODY_TOP = LAND_H - (PAGE_H - BODY_TOP)   # the same 96pt from the top edge


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def fmt_date(value):
    if isinstance(value, (dt.date, dt.datetime)):
        return f"{value:%B} {value.day}, {value.year}"
    text = clean(value)
    try:
        parsed = dt.date.fromisoformat(text[:10])
        return f"{parsed:%B} {parsed.day}, {parsed.year}"
    except ValueError:
        return text


def load_data():
    wb = open_workspace(data_only=True)
    order = location_order(wb)
    projects = OrderedDict()
    for _, data in Sheet(wb["Projects"]).rows():
        pid = clean(data.get("Project ID"))
        if pid:
            projects[pid] = {k: v if isinstance(v, (dt.date, dt.datetime)) else clean(v) for k, v in data.items()}
    items = {}
    for row, data in Sheet(wb["Selections"]).rows():
        pid = clean(data.get("Project ID"))
        if not pid or clean(data.get("Include in Lookbook")).lower() == "no":
            continue
        if not (clean(data.get("Item")) or clean(data.get("Manufacturer"))):
            continue
        record = {k: clean(v) for k, v in data.items()}
        record["_row"] = row
        items.setdefault(pid, []).append(record)

    def sort_key(record):
        location = record.get("Location", "")
        return (order.index(location) if location in order else len(order), location, record["_row"])

    for pid in items:
        items[pid].sort(key=sort_key)
    return projects, items


# --------------------------------------------------------------------------
# Hidden columns
# --------------------------------------------------------------------------
# What an export may leave out, chosen per download. Keys are the workspace columns, except
# 'code' and 'details', which name generated blocks rather than a column. 'schedule' covers
# the PDF, the PowerPoint twin and the fillable copy; 'sheet' is the spec sheet CSV.
HIDEABLE_COLUMNS = [
    ("code", "Item code", ("schedule",)),
    ("Location", "Location headings", ("schedule", "sheet")),
    ("Section", "Category", ("sheet",)),
    ("Item", "Item name", ("schedule", "sheet")),
    ("Room / Area", "Room / area", ("schedule", "sheet")),
    ("Manufacturer", "Manufacturer", ("schedule", "sheet")),
    ("Product Name", "Product name", ("schedule",)),
    ("Model #", "Model number", ("schedule", "sheet")),
    ("Finish / Color", "Finish / color", ("schedule", "sheet")),
    ("Qty", "Quantity", ("schedule", "sheet")),
    ("Client Notes", "Client notes", ("schedule", "sheet")),
    ("Product URL", "Product link", ("schedule", "sheet")),
    ("details", "Additional details", ("schedule",)),
    ("Unit Price", "Unit price", ("sheet",)),
    ("Markup %", "Markup %", ("sheet",)),
    ("Client Status", "Client status", ("sheet",)),
    ("Include in Lookbook", "Include in lookbook", ("sheet",)),
]
HIDEABLE = OrderedDict((key, label) for key, label, _ in HIDEABLE_COLUMNS)


def hidden_columns(value):
    """The columns an export leaves out, from a list or a comma-separated string.

    Hiding nothing, one column or every column is all legitimate; an unknown name is not,
    because it would silently print a field the user believes they have hidden.
    """
    if value is None:
        value = []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError("Hidden columns must be given as a list of column names.")
    names = {clean(name) for name in value} - {""}
    unknown = names - set(HIDEABLE)
    if unknown:
        raise ValueError(f"Unknown column(s) to hide: {', '.join(sorted(unknown))}.")
    return names


def group_sections(rows, hidden=()):
    """Rows under their printed location heading, or all together under '' when headings are hidden."""
    if "Location" in hidden:
        return OrderedDict([("", list(rows))])
    groups = OrderedDict()
    for row in rows:
        groups.setdefault(clean(row.get("Location")) or "Other", []).append(row)
    return groups


# The spec sheet's group-by chips, as the app offers them: by room, as the view opens, or by
# category. Grouping by Location was removed from the view, so an export cannot ask for it either.
SPEC_GROUPS = ("Room / Area", "Section")


def spec_groups(rows, group="Room / Area", hidden=()):
    """Rows under the heading the spec sheet is grouped by on screen."""
    if group not in SPEC_GROUPS:
        raise ValueError(f"Group the spec sheet by one of: {', '.join(SPEC_GROUPS)}.")
    if group in hidden:
        return OrderedDict([("", list(rows))])
    placeholder = "Other" if group == "Section" else "Unassigned"
    groups = OrderedDict()
    for row in rows:
        groups.setdefault(clean(row.get(group)) or placeholder, []).append(row)
    return groups


def row_widths(hidden, reserved=0):
    """Code / item / description widths; the space a hidden column frees goes to the description."""
    code = 0 if "code" in hidden else CODE_W
    item = 0 if {"Item", "Room / Area"} <= set(hidden) else ITEM_W
    return code, item, CONTENT_W - code - item - reserved


def numbered(rows, prefix):
    """Give every selection its schedule code (EX-01, EX-02 ...) in document order."""
    digits = max(2, len(str(len(rows))))
    return [dict(row, _code=f"{prefix}-{index:0{digits}d}") for index, row in enumerate(rows, start=1)]


def description(row, include_quantity=True, hidden=()):
    """One plain-English line per selection, assembled from the workspace columns.

    A Description Override, typed on the fillable schedule, replaces the assembled text and is
    printed as typed: it is literal text, not columns, so hiding a column cannot edit it.
    Tables that show quantity in their own column pass include_quantity=False.
    """
    if clean(row.get("Description Override")):
        return clean(row["Description Override"])
    def shown(column):
        return "" if column in hidden else clean(row.get(column))

    product = shown("Product Name") or shown("Model #")
    finish = shown("Finish / Color")
    identity = " ".join(filter(None, [shown("Manufacturer"), product]))
    if not product and finish:
        identity, finish = " ".join(filter(None, [identity, finish])), ""
    model = shown("Model #")
    if model and product != model and model.lower() not in identity.lower():
        identity = f"{identity} (Model {model})".strip()
    quantity_segment = quantity(row) if include_quantity and "Qty" not in hidden else ""
    segments = [identity, finish, quantity_segment, shown("Client Notes")]
    return " \u2013 ".join(segment for segment in segments if segment)


def product_link(row, hidden=()):
    """The product page a schedule row links to, or '' when there is nothing to open."""
    url = "" if "Product URL" in hidden else clean(row.get("Product URL"))
    return url if http_url(url) else ""


def link_markup(row, hidden=()):
    """The row's product page as a clickable 'Product Link' after its description."""
    url = product_link(row, hidden)
    if not url:
        return ""
    return (f' \u2013 <link href="{escape(url, {chr(34): "&quot;"})}" color="{CFG["colors"]["accent"]}">'
            f"<u>{PRODUCT_LINK}</u></link>")


def quantity(row):
    value = clean(row.get("Qty"))
    try:
        if not value or float(value) == 1:
            return ""
    except ValueError:
        return f"Qty {value}"
    return f"Qty {value.rstrip('0').rstrip('.') if '.' in value else value}"


def band_pair(primary, secondary):
    """Keep the optional second detail only while the pair still fits on one field rule."""
    primary, secondary = clean(primary), clean(secondary)
    if secondary.casefold() == primary.casefold():
        secondary = ""
    combined = " / ".join(filter(None, [primary, secondary]))
    if secondary and pdfmetrics.stringWidth(combined, FONT_B, 9) > FIELD_W:
        return primary
    return combined


def public_url(value):
    """An http(s) address that cannot reach back into a private network or this machine.

    The hosted app fetches whatever a Cover Image cell holds, so loopback, link-local and
    private ranges (including cloud metadata endpoints) are refused before connecting.
    """
    if not http_url(value):
        return False
    try:
        infos = socket.getaddrinfo(urlsplit(value).hostname, None)
        return bool(infos) and all(ipaddress.ip_address(info[4][0]).is_global for info in infos)
    except (OSError, ValueError):
        return False


def resolve_cover(value):
    """Cover Image cell -> image bytes: an app upload (cover:name), a web address, or a file path.

    Uploads land in ./covers beside the workspace; the hosted app resolves the same reference
    through its storage backend and passes the bytes in. A source that cannot be read simply
    produces no cover rather than failing the schedule.
    """
    value = clean(value)
    if not value:
        return None
    if value.startswith(COVER_PREFIX):
        folder = WORKSPACE.parent / "covers"
        path = folder / value[len(COVER_PREFIX):]
        if path.parent.resolve() != folder.resolve() or not path.is_file():
            return None
        return path.read_bytes()
    if http_url(value):
        if not public_url(value):
            return None
        try:
            response = requests.get(value, timeout=15, headers={"User-Agent": "UH-Homes-Selections/1.0"})
            if response.ok and len(response.content) <= 8_000_000:
                return response.content
            return None
        except requests.RequestException:
            return None
    path = Path(value)
    path = path if path.is_absolute() else WORKSPACE.parent / path
    try:
        return path.read_bytes()
    except OSError:
        return None


def open_cover(source):
    """Cover bytes -> a PIL image, or None when the source is not a usable image."""
    try:
        with Image.open(io.BytesIO(source)) as image:
            return image.convert("RGB")
    except Exception:
        return None


def band_values(project):
    return list(zip(BAND_FIELDS, [clean(project.get("Client Name")),
                                  band_pair(project.get("Project Name"), project.get("Plan / Elevation")),
                                  clean(project.get("Address")), fmt_date(project.get("Presentation Date"))]))


def schedule_title(value):
    return clean(value) or clean(SCHEDULE.get("title")) or "Selections"


def code_prefix(value):
    return (clean(value) or clean(SCHEDULE.get("code_prefix")) or "EX").upper()


# --------------------------------------------------------------------------
# Drawing helpers
# --------------------------------------------------------------------------
def spaced_caps(text):
    """'WALLS & TRIM' -> 'W A L L S   &   T R I M'"""
    return "   ".join(" ".join(word) for word in clean(text).upper().split())


def place(canvas, x, y, text, font, size, color, align="left"):
    canvas.setFont(font, size)
    canvas.setFillColor(color)
    (canvas.drawRightString if align == "right" else canvas.drawString)(x, y, clean(text))


def fit(text, font, size, width, minimum=6.5):
    """Shrink, then trim, so a value stays on one line inside the project band."""
    text = clean(text)
    while size > minimum and pdfmetrics.stringWidth(text, font, size) > width:
        size -= 0.25
    if pdfmetrics.stringWidth(text, font, size) > width:
        while text and pdfmetrics.stringWidth(text + "\u2026", font, size) > width:
            text = text[:-1]
        text = text.rstrip() + "\u2026"
    return size, text


def brand_image(canvas, source, x, y, height=None, width=None):
    """Draw a logo at its supplied aspect ratio; returns the drawn width, or 0 when missing."""
    image = load_logo(source)
    if image is None:
        return 0
    iw, ih = image.size
    height = height if height is not None else width * ih / iw
    width = width if width is not None else height * iw / ih
    canvas.drawImage(ImageReader(image), x, y, width, height, mask="auto")
    return width


def mark_bottom(width=MARK_W):
    """Bottom edge that centres the footer symbol on the contact line beside it.

    A line of type is centred on its cap middle, not on its baseline, so standing the
    symbol on the baseline - or on the same bottom edge as the words - leaves it hanging
    below them. Word and PowerPoint set their footer symbol from this too.
    """
    cap = FOOT_SIZE * pdfmetrics.getFont(FONT_B).face.capHeight / 1000
    image = load_logo(CFG.get("logo_mark_path"))
    height = width * image.size[1] / image.size[0] if image is not None else 0
    return FOOT_BASE + (cap - height) / 2


def paper(canvas, page_w, page_h):
    """The studio's cream sheet, laid under everything else; reportlab leaves paper white."""
    canvas.setFillColor(C["paper"])
    canvas.rect(0, 0, page_w, page_h, stroke=0, fill=1)


def furniture(canvas, doc, title, draft, landscape=False):
    """Header and footer, repeated on every page. Positions hang off the page edges so the
    same furniture fits the landscape spec sheet."""
    page_w, page_h = (LAND_W, LAND_H) if landscape else (PAGE_W, PAGE_H)
    content_w = page_w - 2 * M
    canvas.saveState()
    paper(canvas, page_w, page_h)
    logo_width = brand_image(canvas, CFG.get("logo_print_path") or CFG.get("logo_path"), M,
                             page_h - 57.6, height=LOGO_H)
    if not logo_width:
        place(canvas, M, page_h - 54, CFG["company_name"], FONT_BB, 13, C["dark"])
        logo_width = pdfmetrics.stringWidth(CFG["company_name"], FONT_BB, 13)
    eyebrow = clean(CFG.get("tagline", "Finish Schedule")).upper()
    place(canvas, page_w - M, page_h - 39.6, eyebrow, FONT_BB, 7, C["accent"], "right")
    size, shown = fit(title, FONT_H, 15, content_w - logo_width - 12, minimum=9)   # keep clear of the wordmark
    place(canvas, page_w - M, page_h - 55.44, shown, FONT_H, size, C["dark"], "right")
    canvas.setStrokeColor(C["dark"])
    canvas.setLineWidth(0.8)
    canvas.line(M, page_h - 72, page_w - M, page_h - 72)
    canvas.setStrokeColor(C["hairline"])
    canvas.setLineWidth(0.5)
    canvas.line(M, FOOT_RULE_Y, page_w - M, FOOT_RULE_Y)
    has_mark = brand_image(canvas, CFG.get("logo_mark_path"), M, mark_bottom(), width=MARK_W)
    place(canvas, CONTACT_X if has_mark else M, FOOT_BASE, CFG.get("contact_line", ""), FONT_B,
          FOOT_SIZE, C["muted"])
    place(canvas, page_w - M, FOOT_BASE, f"Page {doc.page}", FONT_B, FOOT_SIZE, C["muted"], "right")
    if draft:
        canvas.setFillColor(C["accent"])
        canvas.setFillAlpha(0.07)
        canvas.setFont(FONT_BB, 120)
        canvas.translate(page_w / 2, page_h / 2)
        canvas.rotate(28)
        canvas.drawCentredString(0, -40, "DRAFT")
    canvas.restoreState()


def cover_page(canvas, doc, image, project, title, draft):
    """Opening page when a project has a cover image: brand header, the home's rendering
    aspect-fit inside a hairline frame, then the project and client centred underneath."""
    canvas.saveState()
    paper(canvas, PAGE_W, PAGE_H)
    logo_width = brand_image(canvas, CFG.get("logo_print_path") or CFG.get("logo_path"), M, 734.4, height=LOGO_H)
    if not logo_width:
        place(canvas, M, 738, CFG["company_name"], FONT_BB, 13, C["dark"])
    eyebrow = clean(CFG.get("tagline", "Finish Schedule")).upper()
    place(canvas, PAGE_W - M, 752.4, eyebrow, FONT_BB, 7, C["accent"], "right")
    canvas.setStrokeColor(C["dark"])
    canvas.setLineWidth(0.8)
    canvas.line(M, HEAD_RULE_Y, PAGE_W - M, HEAD_RULE_Y)

    box_y, box_h = 285, 396
    iw, ih = image.size
    scale = min(CONTENT_W / iw, box_h / ih)
    w, h = iw * scale, ih * scale
    canvas.setStrokeColor(C["hairline"])
    canvas.setLineWidth(0.5)
    canvas.rect(M, box_y, CONTENT_W, box_h, stroke=1, fill=0)
    canvas.drawImage(ImageReader(image), M + (CONTENT_W - w) / 2, box_y + (box_h - h) / 2, w, h, mask="auto")

    y = box_y - 42
    name = clean(project.get("Project Name")) or clean(project.get("Project ID"))
    size, shown = fit(name, FONT_H, 19, CONTENT_W - 40, minimum=11)
    canvas.setFont(FONT_H, size)
    canvas.setFillColor(C["dark"])
    canvas.drawCentredString(PAGE_W / 2, y, shown)
    plan = clean(project.get("Plan / Elevation"))
    if plan and plan.casefold() != name.casefold():
        y -= 17
        size, shown = fit(plan, FONT_B, 11, CONTENT_W - 40, minimum=7)
        canvas.setFont(FONT_B, size)
        canvas.setFillColor(C["muted"])
        canvas.drawCentredString(PAGE_W / 2, y, shown)
    y -= 15
    canvas.setStrokeColor(C["accent"])
    canvas.setLineWidth(1.2)
    canvas.line(PAGE_W / 2 - 24, y, PAGE_W / 2 + 24, y)
    y -= 16
    seen = {name.casefold(), plan.casefold()}
    parts = []
    for part in [clean(project.get("Client Name")), clean(project.get("Address")),
                 fmt_date(project.get("Presentation Date"))]:
        if part and part.casefold() not in seen:
            seen.add(part.casefold())
            parts.append(part)
    details = "   \u00b7   ".join(parts)
    size, shown = fit(details, FONT_B, 9.5, CONTENT_W - 30, minimum=6.5)
    canvas.setFont(FONT_B, size)
    canvas.setFillColor(C["body"])
    canvas.drawCentredString(PAGE_W / 2, y, shown)
    y -= 15
    size, shown = fit(title, FONT_BB, 8, CONTENT_W - 30, minimum=6)
    canvas.setFont(FONT_BB, size)
    canvas.setFillColor(C["accent"])
    canvas.drawCentredString(PAGE_W / 2, y, shown.upper())

    canvas.setStrokeColor(C["hairline"])
    canvas.setLineWidth(0.5)
    canvas.line(M, FOOT_RULE_Y, PAGE_W - M, FOOT_RULE_Y)
    canvas.setFont(FONT_B, FOOT_SIZE)
    canvas.setFillColor(C["muted"])
    canvas.drawCentredString(PAGE_W / 2, FOOT_BASE, CFG.get("contact_line", ""))
    if draft:
        canvas.setFillColor(C["accent"])
        canvas.setFillAlpha(0.07)
        canvas.setFont(FONT_BB, 120)
        canvas.translate(PAGE_W / 2, PAGE_H / 2)
        canvas.rotate(28)
        canvas.drawCentredString(0, -40, "DRAFT")
    canvas.restoreState()


class Band(Flowable):
    """Client / project / address / date fields under the header."""

    def __init__(self, values, width=CONTENT_W):
        super().__init__()
        self.values, self.width = values, width

    def wrap(self, *_):
        return self.width, BAND_H

    def draw(self):
        canvas = self.canv
        canvas.setFillColor(C["band"])
        canvas.rect(0, 0, self.width, BAND_H, stroke=0, fill=1)
        field_w = (self.width - 2 * 10.8 - (len(self.values) - 1) * FIELD_GAP) / len(self.values)
        for index, (label, value) in enumerate(self.values):
            x = 10.8 + index * (field_w + FIELD_GAP)
            place(canvas, x, 33.5, label, FONT_BB, 6.5, C["muted"])
            size, shown = fit(value, FONT_B, 9, field_w)
            place(canvas, x, 17, shown, FONT_B, size, C["text"])
            canvas.setStrokeColor(C["field_rule"])
            canvas.setLineWidth(0.5)
            canvas.line(x, 11, x + field_w, 11)


class Heading(Flowable):
    """Letter-spaced section heading with a rule running past the right margin."""

    def __init__(self, text, width=CONTENT_W, keep=True):
        super().__init__()
        self.text, self.width = spaced_caps(text), width
        self.keepWithNext = 1 if keep else 0

    def wrap(self, *_):
        return self.width, HEADING_H

    def draw(self):
        canvas = self.canv
        place(canvas, PAD_X, 5, self.text, FONT_BB, 10, C["dark"])
        canvas.setStrokeColor(C["heading_rule"])
        canvas.setLineWidth(0.5)
        canvas.line(PAD_X + pdfmetrics.stringWidth(self.text, FONT_BB, 10) + HEADING_GAP, 8,
                    self.width + HEADING_OVERHANG, 8)


def section_block(text, table, width=CONTENT_W, frame_h=BODY_TOP - BODY_BOTTOM):
    """A section heading with its rows: the rows may split across pages, but the heading is
    never left stranded at the foot of one.

    reportlab's own keepWithNext pairs the heading with the *whole* table, so a section longer
    than a page moved itself - and left the page it was on empty below the client band. A
    maxHeight of the frame tells KeepTogether to split the rows in place instead, and only
    break the page when the heading itself no longer fits.
    """
    return KeepTogether([Heading(text, width, keep=False), table], maxHeight=frame_h)


class SignOff(Flowable):
    """Signature and date rules for the client and the company representative."""

    def __init__(self, labels):
        super().__init__()
        self.labels = labels

    def wrap(self, *_):
        return CONTENT_W, SIGN_H

    def draw(self):
        canvas = self.canv
        x = SIGN_X
        for label, width in zip(self.labels, SIGN_W):
            canvas.setStrokeColor(C["dark"])
            canvas.setLineWidth(0.5)
            canvas.line(x, 18, x + width, 18)
            place(canvas, x, 7.5, label, FONT_B, 7.5, C["muted"])
            x += width


def item_cell(row, hidden=()):
    """The item column: the item name, with its room beneath, minus whatever is hidden."""
    parts = [] if "Item" in hidden else [Paragraph(escape(clean(row.get("Item"))), ITEM_STYLE)]
    if "Room / Area" not in hidden and clean(row.get("Room / Area")):
        parts.append(Paragraph(escape(clean(row["Room / Area"])), ROOM_STYLE))
    return parts or ""


def rows_table(rows, draft, hidden=()):
    code_w, item_w, desc_w = row_widths(hidden, 0)
    widths = [width for width in (code_w, item_w) if width] + [desc_w]
    data = []
    for row in rows:
        cells = [Paragraph(escape(row.get("_code", "")), CODE_STYLE)] if code_w else []
        if item_w:
            cells.append(item_cell(row, hidden))
        cells.append(Paragraph(escape(description(row, hidden=hidden)) + link_markup(row, hidden), DESC_STYLE))
        data.append(cells)
    return Table(data, colWidths=widths, style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), PAD_X),
        ("RIGHTPADDING", (0, 0), (-1, -1), PAD_X),
        ("TOPPADDING", (0, 0), (-1, -1), PAD_Y),
        ("BOTTOMPADDING", (0, 0), (-1, -1), PAD_Y),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, C["hairline"]),
    ]))


def detail_flowables(project, rows, hidden=()):
    """Project and selection custom fields, in the same two-column rhythm."""
    groups = [] if "details" in hidden else presentation_details(project, rows)
    if not groups:
        return []
    story = [Spacer(0, SECTION_GAP), Heading("Additional details")]
    for title, fields in groups:
        story.append(Paragraph(escape(title), GROUP_STYLE))
        story.append(Table([[Paragraph(escape(field["name"]), ITEM_STYLE),
                             Paragraph(escape(field["value"] or "Not provided").replace("\n", "<br/>"), DESC_STYLE)]
                            for field in fields],
                           colWidths=[CODE_W + ITEM_W, CONTENT_W - CODE_W - ITEM_W], style=TableStyle([
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), PAD_X),
                               ("RIGHTPADDING", (0, 0), (-1, -1), PAD_X),
                               ("TOPPADDING", (0, 0), (-1, -1), PAD_Y),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), PAD_Y),
                               ("LINEBELOW", (0, 0), (-1, -2), 0.4, C["hairline"]),
                           ])))
    return story


# --------------------------------------------------------------------------
def build(pid, project, rows, draft=False, output_dir=None, title=None, prefix=None, rev=None, hidden=(), cover=None):
    title, rows = schedule_title(title), numbered(rows, code_prefix(prefix))
    cover_image = open_cover(cover) if cover else None
    story = [Band(band_values(project)), Spacer(0, BAND_GAP)]
    for index, (section, items) in enumerate(group_sections(rows, hidden).items()):
        if index:
            story.append(Spacer(0, SECTION_GAP))
        table = rows_table(items, draft, hidden)
        story.append(section_block(section, table) if section else table)
    story += detail_flowables(project, rows, hidden)
    story.append(Spacer(0, SECTION_GAP))
    story.append(KeepTogether([Paragraph(escape(clean(SCHEDULE.get("note"))), NOTE_STYLE), Spacer(0, 16),
                               SignOff(SCHEDULE.get("signatures") or [])]))

    destination = Path(output_dir or OUT)
    destination.mkdir(exist_ok=True)
    parts = [re.sub(r"[^A-Za-z0-9_-]", "_", pid)[:50],
             re.sub(r"[^A-Za-z0-9]+", "_", clean(project.get("Project Name"))).strip("_")[:100],
             re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:60],
             f"R{rev}" if rev else ""]
    out = destination / ("_".join(part for part in parts if part) + ("_DRAFT" if draft else "") + ".pdf")
    doc = BaseDocTemplate(str(out), pagesize=(PAGE_W, PAGE_H), leftMargin=M, rightMargin=M,
                          topMargin=PAGE_H - BODY_TOP, bottomMargin=BODY_BOTTOM,
                          title=f"{clean(project.get('Project Name')) or pid} - {title}",
                          author=CFG["company_name"],
                          subject=f"Revision {rev} - exported {dt.date.today().isoformat()}" if rev else "")
    frame = Frame(M, BODY_BOTTOM, CONTENT_W, BODY_TOP - BODY_BOTTOM, 0, 0, 0, 0, id="schedule")
    templates = []
    if cover_image:
        templates.append(PageTemplate("cover", [Frame(M, BODY_BOTTOM, CONTENT_W, BODY_TOP - BODY_BOTTOM, 0, 0, 0, 0, id="cover")],
                                      onPage=lambda canvas, document: cover_page(canvas, document, cover_image, project, title, draft)))
    templates.append(PageTemplate("schedule", [frame],
                                  onPage=lambda canvas, document: furniture(canvas, document, title, draft)))
    doc.addPageTemplates(templates)
    if cover_image:
        story = [Spacer(0, 0), NextPageTemplate("schedule"), PageBreak()] + story
    doc.build(story)
    return out


def spec_cell(row, column, hidden):
    """One spec-sheet cell: the product page as a clickable link, everything else as text."""
    if column == PRODUCT_LINK:
        url = product_link(row, hidden)
        if not url:
            return ""
        return Paragraph(f'<link href="{escape(url, {chr(34): "&quot;"})}" '
                         f'color="{CFG["colors"]["accent"]}"><u>{PRODUCT_LINK}</u></link>',
                         SPEC_CELL_STYLE)
    return Paragraph(escape(clean(row.get(column))), SPEC_CELL_STYLE)


def spec_table(items, columns, widths, hidden):
    """A repeated-header table of every priced line item, one row per selection."""
    data = [[Paragraph(escape(column.upper()), SPEC_HEAD_STYLE) for column, _ in columns]]
    for row in items:
        cells = [spec_cell(row, column, hidden) for column, _ in columns]
        if "Include in Lookbook" not in hidden and clean(row.get("Include in Lookbook")).lower() == "no":
            cells[0] = [cells[0]] if cells[0] != "" else []
            cells[0].append(Paragraph(escape("Not in lookbook"), SPEC_NOTE_STYLE))
        data.append(cells)
    return Table(data, colWidths=widths, repeatRows=1, style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("VALIGN", (0, 0), (-1, 0), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), PAD_X),
        ("RIGHTPADDING", (0, 0), (-1, -1), PAD_X),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, C["heading_rule"]),
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, C["hairline"]),
    ]))


def build_spec(pid, project, rows, output_dir=None, hidden=(), group="Room / Area"):
    """The internal spec sheet: the app's line items and columns, landscape, under the same headings."""
    columns = [(column, share) for column, share, key in SPEC_COLUMNS if key not in hidden]
    if not columns:
        columns = [("Item", 1)]
    widths = [LAND_CONTENT_W * share / sum(share for _, share in columns) for _, share in columns]
    story = [Band(band_values(project), LAND_CONTENT_W), Spacer(0, BAND_GAP)]
    for index, (section, items) in enumerate(spec_groups(rows, group, hidden).items()):
        if index:
            story.append(Spacer(0, SECTION_GAP))
        table = spec_table(items, columns, widths, hidden)
        story.append(section_block(section, table, LAND_CONTENT_W, LAND_BODY_TOP - BODY_BOTTOM)
                     if section else table)

    destination = Path(output_dir or OUT)
    destination.mkdir(exist_ok=True)
    parts = [re.sub(r"[^A-Za-z0-9_-]", "_", pid)[:50],
             re.sub(r"[^A-Za-z0-9]+", "_", clean(project.get("Project Name"))).strip("_")[:100],
             re.sub(r"[^A-Za-z0-9]+", "_", SPEC_TITLE).strip("_")[:60]]
    out = destination / ("_".join(part for part in parts if part) + ".pdf")
    doc = BaseDocTemplate(str(out), pagesize=(LAND_W, LAND_H), leftMargin=M, rightMargin=M,
                          topMargin=LAND_H - LAND_BODY_TOP, bottomMargin=BODY_BOTTOM,
                          title=f"{clean(project.get('Project Name')) or pid} - {SPEC_TITLE}",
                          author=CFG["company_name"], subject=f"Internal spec sheet for {pid}")
    frame = Frame(M, BODY_BOTTOM, LAND_CONTENT_W, LAND_BODY_TOP - BODY_BOTTOM, 0, 0, 0, 0, id="spec")
    doc.addPageTemplates([PageTemplate("spec", [frame],
                                       onPage=lambda canvas, document: furniture(canvas, document,
                                                                                 SPEC_TITLE, False,
                                                                                 landscape=True))])
    doc.build(story)
    return out


def spec_rows(wb, pid):
    """Every line item of a project in workspace order, which is the order the view shows.

    The lookbook flag is a note, not a filter, and the rows are deliberately left unsorted:
    `schedule_sort` is the finish schedule's Location order and would reshuffle the sheet.
    """
    return [{**{k: clean(v) for k, v in data.items()}, "_row": row}
            for row, data in Sheet(wb["Selections"]).rows()
            if clean(data.get("Project ID")) == pid]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--project", help="Project ID, e.g. UH-101")
    group.add_argument("--all", action="store_true", help="Every project on the Projects sheet")
    parser.add_argument("--spec", action="store_true", help="Internal priced spec sheet, landscape")
    parser.add_argument("--draft", action="store_true", help="Internal review copy (watermarked)")
    parser.add_argument("--title", help=f"Schedule title (default: {SCHEDULE.get('title', 'Selections')})")
    parser.add_argument("--prefix", help=f"Item code prefix (default: {SCHEDULE.get('code_prefix', 'EX')})")
    parser.add_argument("--hide", default="",
                        help="Columns to leave out, comma separated: " + ", ".join(HIDEABLE).replace("%", "%%"))
    args = parser.parse_args()

    try:
        hidden = hidden_columns(args.hide)
    except ValueError as error:
        parser.error(str(error))
    projects, items = load_data()
    if args.spec:
        wb = open_workspace(data_only=True)
        for pid in (list(projects) if args.all else [args.project]):
            if pid not in projects or not spec_rows(wb, pid):
                print(f"- {pid}: no line items to export, skipped")
                continue
            print(f"+ {pid}: {build_spec(pid, projects[pid], spec_rows(wb, pid), hidden=hidden).relative_to(BASE_DIR)}")
        return
    for pid in (list(projects) if args.all else [args.project]):
        if pid not in projects:
            print(f"x {pid}: not on the Projects sheet")
            continue
        rows = items.get(pid, [])
        if not rows:
            print(f"- {pid}: no selections to include, skipped")
            continue
        out = build(pid, projects[pid], rows, args.draft, title=args.title, prefix=args.prefix, hidden=hidden,
                    cover=resolve_cover(projects[pid].get("Cover Image")))
        print(f"+ {pid}: {len(rows)} selections -> {out.relative_to(BASE_DIR)}")


if __name__ == "__main__":
    main()
