"""
Build the client finish schedule PDF from the tracker workspace.

Usage:
    python build_lookbook.py --project UH-101                              # one project
    python build_lookbook.py --all                                         # every project
    python build_lookbook.py --project UH-101 --draft                      # internal copy (watermarked)
    python build_lookbook.py --project UH-101 --title "Interior Selections" --prefix IN
    python build_lookbook.py --project UH-101 --spec                       # internal priced spec sheet
    python build_lookbook.py --project UH-101 --spec --orientation portrait  # ... turned upright
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
from collections import OrderedDict, namedtuple
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import requests
from PIL import Image
from reportlab.lib.colors import Color, HexColor
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Flowable, Frame,
                                KeepTogether, NextPageTemplate, PageBreak, PageTemplate,
                                Paragraph, Spacer, Table, TableStyle)

from branding import load_logo
from common import (BASE_DIR, WORKSPACE, QTY_UNIT, QTY_UNIT_KEYS, Sheet, clean, open_workspace,
                    location_order, qty_unit)
from workspace_store import COVER_PREFIX, PRODUCT_LINK, http_url, fix_product_url, presentation_details

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
BAND_INSET = 10.8                    # left and right padding inside a band field
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
# The spec sheet is the internal working copy, so by request it prints on plain white and is
# ruled in a light grey at the same weights, while the client finish schedule keeps the studio's
# cream paper and gold rules. The client details band is deliberately left out of it:
# its own ruled strip and gold field underlines as before. No text, link or logo colour
# changes either.
SHEET_COLORS = {"paper": "#FFFFFF", "rule": "#BFBFBF", **CFG.get("sheet_colors", {})}
SHEET = {key: HexColor(value) for key, value in SHEET_COLORS.items()}
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
# The sheet is read and marked up, so its line items are set at 9/13 - a point under the
# client schedule's 10/13 descriptions, by request, so a dense sheet fits its page. The
# column labels stay smaller than the data they head - 7pt against 9 - because the longest
# of them (MANUFACTURER) is what sets a column's minimum width, and at 8pt it took the room
# an item name needs. build_word reads all three off these styles, so one change moves both
# documents. Column heads go darker than the data they head: at the muted gold they read as
# just another small value, and a header nobody can pick out is no header at all.
SPEC_HEAD_STYLE = style("spec_head", 7, 9.5, C["dark"], font=FONT_BB)
# The details box's field labels keep the client band's own muted gold instead, and the
# figure they label reads at the line items' size, bold and set against the right edge of
# its pair - so a column of areas lines up as figures do, whatever the labels beside them do.
DETAIL_HEAD_STYLE = style("detail_head", 7, 9.5, C["muted"], font=FONT_BB)
DETAIL_FIGURE_STYLE = style("detail_figure", 9, 13, C["body"], font=FONT_BB, align=TA_RIGHT)
# A label and its figure read as one line, so they sit on one baseline. reportlab puts a
# paragraph's first baseline exactly its font size below the top of its box, so the smaller
# label starts that much lower than the figure beside it; Word hangs an exactly spaced line
# from the bottom of its line box instead, so build_word drops it by the leading difference.
# Both are read off these styles, so neither document can drift from a change of size.
DETAIL_BASELINE = DETAIL_FIGURE_STYLE.fontSize - DETAIL_HEAD_STYLE.fontSize
SPEC_CELL_STYLE = style("spec_cell", 9, 13, C["body"])
SPEC_NOTE_STYLE = style("spec_note", 8, 10.5, C["muted"])

SPEC_TITLE = "Spec Sheet"
# The printed spec sheet mirrors the app's spec-sheet table, column for column, so the
# download carries exactly the line items and headings on screen. The grouping field becomes
# the heading; the row's Section and the lookbook flag print as notes under the item name,
# matching the cell-sub line the same table shows on screen. (column, width share, the hideable
# key - or keys, where one column stands over two fields - that drop it); shares are rescaled
# to what is shown, and build_word.build_spec lays the same registry out in Word.
# Every share is wide enough for its own heading on one line, and PRODUCT LINK for the words
# in its cells as well. A heading that wraps reads as two columns - PRODUCT over LINK - and
# portrait decides the numbers: three quarters of the landscape width, with tighter padding.
SPEC_COLUMNS = [("Item", 88, "Item"), ("Manufacturer", 67, "Manufacturer"),
                ("Model #", 48, "Model #"), ("Finish / Color", 70, "Finish / Color"),
                ("Size / Configuration", 62.5, "Size / Configuration"),
                (PRODUCT_LINK, 50, "Product URL"), (QTY_UNIT, 44, QTY_UNIT_KEYS),
                ("Unit Price", 52, "Unit Price"), ("Markup %", 46, "Markup %")]
# Printed headings shorter than their column names, where a column earns it: the long
# 'Size / Configuration' needs 'SIZE / CONFIG' to fit at all, and the link column prints
# 'LINK' over a one-word 'Link' anchor so it stays only as wide as what it carries -
# the freed share goes to the columns whose text actually wraps.
SPEC_LABELS = {"Size / Configuration": "Size / Config", PRODUCT_LINK: "Link"}
# A group headed PROJECT carries the home's own figures - areas, garage size, porch counts -
# not a set of line items, so the spec sheet, its Word copy and the portal draw it as a short
# list on open paper rather than a ruled table: pairs of label and figure, a hairline under
# each, between the client band's own two hairlines.
DETAILS_GROUP = "PROJECT"
# Two pairs across, as the portal's own box reads. Tiled as fields - a name over its figure on
# an underline, four across and generously padded - eight areas took a third of the sheet and
# nearly all of it white space. Two pairs rather than three because a label has to print on one
# line to read as a label: measured against a home's own eight figures, three across wrapped
# five of them in landscape and every one of them in portrait, while two wrap none and one.
DETAIL_PAIRS = 2
DETAIL_GUTTER = 2 * BAND_INSET      # a real gap between the pairs, so no rule crosses it
DETAIL_PAD_Y = 3                    # rows, not tiles: a figure takes its own line and no more
# The figure column is as wide as the widest figure on the sheet and no wider, so the labels
# keep the rest of their pair; a value long enough to want half the pair wraps instead of
# squeezing every label beside it.
DETAIL_FIGURE_MAX = 0.5
# The figure itself is the Size / Configuration cell the portal types into, and whatever else
# the row still shows follows as small print under the label.
DETAIL_FIGURE = "Size / Configuration"
# The spec sheet's section headings track their letters instead of spacing between them:
# the same look the schedule's letter-spaced headings have, at about half the air.
SPEC_HEADING_TRACK = 1.2

# Editable spec-sheet form fields: PDF form fields accept only the standard 14 fonts,
# so typed text uses Helvetica while every label and heading keeps the brand font.
SPEC_FORM_FONT = "Helvetica"
# A field is somewhere to type, never ink. The sheet is printed, marked up and handed over
# as a spec sheet, so no box may survive the printer: every one of them is the paper's own
# white, border included, and what shows there is something to click into is the highlight
# a reader paints over its own form fields - on screen, never on paper. White rather than
# no colour at all, because a field prints what somebody typed into it while the page
# underneath still carries the value as exported: with nothing behind it the two print one
# across the other. Left to itself reportlab fills a field in its own default light blue,
# and forceBorder painted that into the page as well as into the field - a blue box behind
# every value and over it, so a printer that left the fields out printed the boxes and not
# one value.
SPEC_FIELD_BOX = Color(1, 1, 1)
SPEC_FIELD_H = 15                    # a box holding one line
SPEC_FIELD_BW = 0.5                  # the hairline it is laid out to, white on white
# A reader lays a field's text out inside that border rather than against it: it starts
# the text four border widths in and clips at two, so that much of the box never holds a
# character. The box is drawn that much wider than its cell instead, half either side, so
# what a field can hold is exactly the room spec_widths measured the column for - rather
# than every column on the page paying for the border its cells are drawn with.
SPEC_FIELD_INSET = 6 * SPEC_FIELD_BW
SPEC_FIELD_LEADING = 1.2             # the leading a reader gives a field's own lines
# Typographic punctuation → WinAnsi-safe equivalents for form field values.
SPEC_SAFE_CHARS = {0x2013: "-", 0x2014: "-", 0x2018: "'", 0x2019: "'", 0x201C: '"',
                   0x201D: '"', 0x2026: "...", 0x00A0: " "}


def spec_form_text(value):
    """A value a spec-sheet PDF form field can hold."""
    return clean(value).translate(SPEC_SAFE_CHARS).encode("latin-1", "replace").decode("latin-1")


def spec_field_lines(value, room, size=9):
    """A value broken into the lines a field with that much room holds, wrapped at spaces.

    A field is a box, not a column: a reader clips whatever reaches past its edge and
    the copy drawn under it spills across the cell beside it, so a finish name reads
    half inside its box and half over the one next door. Wrapping here instead lets
    the box grow and keeps every character where it belongs. A single word wider than
    the whole box is cut where it stops fitting, which the column widths make a last
    resort rather than a habit.
    """
    fits = lambda text: pdfmetrics.stringWidth(text, SPEC_FORM_FONT, size) <= room
    words = value.split()
    if not words or room < size:      # no value, or no room to lay one out in
        return [" ".join(words)] if words else []
    lines = []
    for word in words:
        while word and not fits(word):
            cut = next((i for i in range(1, len(word)) if not fits(word[:i + 1])), len(word))
            lines.append(word[:cut])
            word = word[cut:]
        if not word:
            continue
        if lines and fits(f"{lines[-1]} {word}"):
            lines[-1] = f"{lines[-1]} {word}"
        else:
            lines.append(word)
    return lines


class SpecFieldCell(Flowable):
    """One cell's value, with a field over it to type into on the editable copy.

    The value is wrapped to the box and the box is as tall as the lines it holds, so
    nothing printed on the sheet is cut off at a field's edge or spilled past it. The
    same lines are drawn on the page as well as carried by the field, at the positions
    a reader lays its own text out in, so the two coincide instead of reading as one
    blurred double - and the sheet still prints and extracts its values wherever form
    fields are not rendered at all.

    Neither copy draws a box on the page - a hard copy of either reads as a spec sheet
    rather than as a blank form - so the locked one is simply the same page with no field
    added: the two are the same document to read and only one of them can be typed into.
    """

    def __init__(self, name, value, room, size=9, fillable=True):
        super().__init__()
        self.name, self.width, self.size, self.fillable = name, room, size, fillable
        self.lines = spec_field_lines(spec_form_text(value), room, size)
        self.value = "\n".join(self.lines)
        self.height = SPEC_FIELD_H + (max(len(self.lines), 1) - 1) * SPEC_FIELD_LEADING * size

    def wrap(self, *_):
        return self.width, self.height

    def draw(self):
        # The box reaches half the border's own inset either side of the cell's text, so
        # the first character sits where a printed one would and the last one still fits.
        left = -SPEC_FIELD_INSET / 2
        if self.lines:
            self.canv.saveState()
            self.canv.setFont(SPEC_FORM_FONT, self.size)
            self.canv.setFillColor(C["body"])
            top = self.height - self.size - 2 * SPEC_FIELD_BW
            for index, line in enumerate(self.lines):
                self.canv.drawString(left + 4 * SPEC_FIELD_BW,
                                     top - index * SPEC_FIELD_LEADING * self.size, line)
            self.canv.restoreState()
        if not self.fillable:
            # The locked copy: the value and nothing else. Nothing a reader can click into,
            # so what was exported is what the sheet keeps saying.
            return
        # The editable field over it, white on white paper and bordered in the same, so a
        # hard copy carries the value and no box around it. Every field is multiline, not
        # only the ones that opened wrapped: it is what lays a printed value's lines back
        # out as they were, and what wraps a longer one somebody types in rather than
        # running it off the side.
        self.canv.acroForm.textfield(
            name=self.name, value=self.value, x=left, y=0,
            width=self.width + SPEC_FIELD_INSET, height=self.height,
            fontName=SPEC_FORM_FONT, fontSize=self.size,
            textColor=C["body"], fillColor=SPEC_FIELD_BOX,
            borderColor=SPEC_FIELD_BOX, borderWidth=SPEC_FIELD_BW, relative=True,
            fieldFlags="doNotSpellCheck multiline")


def spec_heading(column):
    """The label a spec column prints over itself, uppercased."""
    return SPEC_LABELS.get(column, column).upper()


def link_word():
    """The word a spec-sheet link anchor prints - the column's own short label, so the
    cell text and the heading it sits under can never drift apart."""
    return SPEC_LABELS.get(PRODUCT_LINK, PRODUCT_LINK)


# Landscape spec sheet: a letter page turned on its side, so the wide column set fits.
LAND_W, LAND_H = PAGE_H, PAGE_W           # 792 x 612
LAND_CONTENT_W = LAND_W - 2 * M           # 698.4
LAND_BODY_TOP = LAND_H - (PAGE_H - BODY_TOP)   # the same 96pt from the top edge
# The spec sheet prints either way round, chosen per download: landscape as it always has,
# because this many columns want the width, or portrait to file beside the client schedule.
# Only the page changes - the same furniture, the same 96pt from the top edge to the first
# row, and column widths that are shares of whatever content width is left, each grown to
# hold the longest unbreakable word its cells print.
SPEC_ORIENTATIONS = ("landscape", "portrait")
# Portrait fits the same columns into three quarters of the width, so its cells are padded
# tighter: with nine columns to a narrower page, the narrow numeric ones have barely room
# for their own heading even at 3pt, and a quantity of 1,250 comes down the Qty column a
# digit at a time at the full 6.
SPEC_PAD_X = {"landscape": PAD_X, "portrait": 3}
SpecPage = namedtuple("SpecPage", "name landscape width height content_w body_top pad_x")


def spec_orientation(value):
    """The orientation a spec sheet was asked for; nothing asked for means landscape.

    An unknown name is refused rather than quietly printed landscape: a download that
    ignores the choice is worse than one that says it cannot honour it.
    """
    name = clean(value).lower() or SPEC_ORIENTATIONS[0]
    if name not in SPEC_ORIENTATIONS:
        raise ValueError(f"Print the spec sheet in one of: {', '.join(SPEC_ORIENTATIONS)}.")
    return name


# The PDF goes out as one of two copies of the same sheet: the locked one, which nothing a
# reader offers will change, or the editable one, whose cells are form fields to type into.
# A plain download is the locked copy, because a reader paints its own pale blue highlight
# over every form field it finds - nothing inside the file can turn that off, so an editable
# sheet reads as a page of blue boxes however the fields themselves are coloured, and a sheet
# to read should read as a sheet. Word and the CSV have no such choice - Word is a document
# to mark up by its nature, and a CSV is text - so this rides with the PDF alone.
SPEC_COPIES = ("locked", "editable")


def spec_copy(value):
    """Which copy of the spec sheet to print; nothing asked for means the locked one.

    Like the orientation, an unknown name is refused rather than quietly answered with
    the other copy: handing somebody a typeable sheet when they asked for a locked one
    is the mistake this option exists to prevent.
    """
    name = clean(value).lower() or SPEC_COPIES[0]
    if name not in SPEC_COPIES:
        raise ValueError(f"Export the spec sheet as one of: {', '.join(SPEC_COPIES)}.")
    return name


def spec_page(orientation="landscape"):
    """The page that orientation prints on, as the PDF and the Word copy both measure it."""
    name = spec_orientation(orientation)
    width, height = (LAND_W, LAND_H) if name == "landscape" else (PAGE_W, PAGE_H)
    return SpecPage(name, name == "landscape", width, height, width - 2 * M,
                    height - (PAGE_H - BODY_TOP), SPEC_PAD_X[name])


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
    # a column on the spec sheet, the section headings on the schedule; one name covers both
    ("Location", "Location", ("schedule", "sheet")),
    ("Section", "Category", ("sheet",)),
    ("Item", "Item name", ("schedule", "sheet")),
    ("Room / Area", "Room / area", ("schedule", "sheet")),
    ("Manufacturer", "Manufacturer", ("schedule", "sheet")),
    ("Product Name", "Product name", ("schedule",)),
    ("Model #", "Model number", ("schedule", "sheet")),
    ("Finish / Color", "Finish / color", ("schedule", "sheet")),
    # the product's dimensions or configuration, shown as its own spec column and
    # folded into the client schedule's description sentence
    ("Size / Configuration", "Size / configuration", ("schedule", "sheet")),
    ("Qty", "Quantity", ("schedule", "sheet")),
    ("Client Notes", "Client notes", ("schedule", "sheet")),
    ("Product URL", "Product link", ("schedule", "sheet")),
    ("details", "Additional details", ("schedule",)),
    # Unit sits beside Qty on the spec sheet only: the client documents describe a quantity
    # in words through `description`, which the fillable copies compare against on import.
    ("Unit", "Unit", ("sheet",)),
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


def is_details_row(row):
    """A row carrying the home's own figures rather than a line item.

    The figures answer to the project, so a `PROJECT` heading in either grouping column
    marks them: areas and porch counts are no more a category than they are a room, and
    grouping the sheet by category used to file them away as ordinary line items under
    whatever category they happened to carry.
    """
    return any(is_details_group(row.get(field)) for field in SPEC_GROUPS)


def spec_groups(rows, group="Room / Area", hidden=(), room_order=()):
    """Rows under the heading the spec sheet is grouped by on screen.

    The home's own figures lead the sheet as their own `PROJECT` group whichever way it
    is grouped, and never appear among the line items.  When *room_order* is given and
    the grouping is by room, the headings print in that order.  Unknown rooms sort after
    the listed ones, keeping their line-item order among themselves.  The line items
    inside each room stay in workspace order.
    """
    if group not in SPEC_GROUPS:
        raise ValueError(f"Group the spec sheet by one of: {', '.join(SPEC_GROUPS)}.")
    details = [row for row in rows if is_details_row(row)]
    lines = [row for row in rows if not is_details_row(row)]
    groups = OrderedDict()
    if details:
        groups[DETAILS_GROUP] = details
    if group in hidden:
        groups[""] = lines
        return groups
    placeholder = "Other" if group == "Section" else "Unassigned"
    for row in lines:
        groups.setdefault(clean(row.get(group)) or placeholder, []).append(row)
    if room_order and group == "Room / Area":
        order = list(room_order)
        # PROJECT (details band) always prints first; unknown rooms sort after listed ones.
        def sort_key(pair):
            name = pair[0]
            if is_details_group(name):
                return (-1, 0)
            if name in order:
                return (0, order.index(name))
            return (1, 0)
        groups = OrderedDict(sorted(groups.items(), key=sort_key))
    return groups


def column_header(column, keys, hidden):
    """A column's header once its hidden fields are taken out, or '' when none is left.

    Only Qty / Unit stands over two workspace fields: hiding one half narrows the column to
    the other and hiding both drops it, exactly as two separate columns behaved.
    """
    shown = [key for key in keys if key not in hidden]
    if not shown:
        return ""
    return column if len(shown) == len(keys) else " / ".join(shown)


def spec_columns(hidden=()):
    """(header, width share) for every spec-sheet column still shown."""
    shown = [(column_header(column, key if isinstance(key, tuple) else (key,), hidden), share)
             for column, share, key in SPEC_COLUMNS]
    return [(header, share) for header, share in shown if header] or [("Item", 1)]


def spec_value(row, column):
    """The text a spec-sheet column shows for a row; Qty / Unit joins the two fields it covers."""
    return qty_unit(row) if column == QTY_UNIT else clean(row.get(column))


def is_details_group(name):
    """The heading that marks a project's own figures rather than a set of line items."""
    return clean(name).upper() == DETAILS_GROUP


def detail_parts(row, hidden):
    """A project figure as the portal shows it: the figure itself, and its small print.

    The figure is the Size / Configuration cell, which is what the portal's own box types
    into; whatever else the row still shows reads as small print under the label. A row
    keeping its figure somewhere else - a Qty and a Unit, say - would print an empty field
    and leave the figure in the small print, so the remainder stands in when that cell is
    empty.
    """
    figure = "" if DETAIL_FIGURE in hidden else spec_value(row, DETAIL_FIGURE)
    rest = detail_value(row, hidden, skip=(DETAIL_FIGURE,))
    return (figure, rest) if figure else (rest, "")


def detail_value(row, hidden, skip=()):
    """The words a details field carries: every spec column still shown except the item
    name it is labelled with, the link, and whatever the caller prints itself."""
    bits = []
    for column, _, keys in SPEC_COLUMNS:
        if column in ("Item", PRODUCT_LINK) or column in skip:
            continue
        keys = keys if isinstance(keys, tuple) else (keys,)
        shown = [key for key in keys if key not in hidden]
        if not shown:
            continue
        value = spec_value(row, column) if len(shown) == len(keys) else clean(row.get(shown[0]))
        if value:
            bits.append(value)
    return " · ".join(bits)


# Columns whose value is one identifier rather than prose never tear mid-word: a model
# number split anywhere - even at its own space - reads as a typo, and a quantity split
# from its unit reads as two fields. Each value holds whole while it fits inside
# SPEC_ATOMIC_MAX of the page; a value claiming more than that is prose wearing an
# identifier's clothes and wraps at its spaces like prose, still never mid-word.
SPEC_ATOMIC = {"Model #", QTY_UNIT, "Unit Price", "Markup %"}
SPEC_ATOMIC_MAX = 0.2


def spec_measure(column, items, hidden, text_cap=None):
    """(unbreakable, whole-line) text widths a spec column's contents ask for.

    The first is what the column cannot go below without losing data: its heading on
    one line, and per cell the whole value while it fits inside text_cap - or its
    longest word once it does not, because a value that long wraps at its spaces
    instead of claiming the page. The second is what prints the longest cell whole."""
    head = pdfmetrics.stringWidth(spec_heading(column), SPEC_HEAD_STYLE.fontName,
                                  SPEC_HEAD_STYLE.fontSize)
    word = whole = head
    for row in items:
        if column == PRODUCT_LINK:
            text = link_word() if product_link(row, hidden) else ""
        else:
            text = spec_value(row, column)
        text_w = pdfmetrics.stringWidth(text, SPEC_CELL_STYLE.fontName,
                                        SPEC_CELL_STYLE.fontSize)
        token_w = max((pdfmetrics.stringWidth(token, SPEC_CELL_STYLE.fontName,
                                              SPEC_CELL_STYLE.fontSize)
                       for token in text.split()), default=0.0)
        whole = max(whole, text_w)
        word = max(word, token_w if text_cap is None or text_w > text_cap else text_w)
    return word, whole


def spec_widths(columns, content_w, items=(), pad_x=PAD_X, hidden=()):
    """What each spec-sheet column gets of the page.

    First every column is guaranteed its hard floor - the width below which printed
    data is torn, so no value loses characters anywhere on the sheet; an atomic value
    counts whole while it fits inside SPEC_ATOMIC_MAX of the page, by its longest word
    once it does not. The shares then divide the space as they always did, and finally
    any room a column holds past what its longest whole value needs moves to columns
    still short of theirs, so a value wraps only when the page genuinely cannot keep
    every value on one line. Both builders measure through here so they cannot drift.
    """
    pad = 2 * pad_x + 0.5
    cap = content_w * SPEC_ATOMIC_MAX
    hards, wants = [], []
    for column, _ in columns:
        if column in SPEC_ATOMIC:
            word_w, whole_w = spec_measure(column, items, hidden, cap - pad)
            hards.append(word_w + pad)
            wants.append(min(whole_w + pad, cap))
        else:
            word_w, whole_w = spec_measure(column, items, hidden)
            hards.append(word_w + pad)
            wants.append(whole_w + pad)
    if sum(hards) >= content_w:                       # nothing to divide: scale the floors
        return [w * content_w / sum(hards) for w in hards]
    shares = [share for _, share in columns]
    widths, pool, free = list(hards), content_w, list(range(len(columns)))
    while free:
        rest = sum(shares[i] for i in free)
        grown = {i: pool * shares[i] / rest for i in free}
        tight = [i for i in free if grown[i] < hards[i]]
        if not tight:
            for i in free:
                widths[i] = grown[i]
            break
        for i in tight:
            free.remove(i)
            pool -= hards[i]
    else:                                             # every column pinned: fill by shares
        extra = (content_w - sum(widths)) / sum(shares)
        widths = [w + extra * share for w, share in zip(widths, shares)]
    spare = [max(0.0, w - max(hard, want)) for w, hard, want in zip(widths, hards, wants)]
    short = [max(0.0, want - w) for w, want in zip(widths, wants)]
    give, need = sum(spare), sum(short)
    move = min(give, need)
    if move:
        widths = [w - s * move / give + f * move / need
                  for w, s, f in zip(widths, spare, short)]
    return widths


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
    segments = [identity, finish, shown("Size / Configuration"), quantity_segment, shown("Client Notes")]
    return " \u2013 ".join(segment for segment in segments if segment)


def product_link(row, hidden=()):
    """The product page a schedule row links to, or '' when there is nothing to open."""
    url = "" if "Product URL" in hidden else fix_product_url(clean(row.get("Product URL")))
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


def band_shares(values, font, size):
    """Each band field's share of the row, proportional to the text it must hold.

    Four equal fields left the longest value - always the address - shrunk to unreadable
    beside a short client name. Measuring each field's need first gives the wide ones the
    room they print in; fit() still guards a value longer than even that share.
    """
    needs = [max(pdfmetrics.stringWidth(label, FONT_BB, 6.5),
                 pdfmetrics.stringWidth(value, font, size)) + 18
             for label, value in values]
    total = sum(needs) or 1
    return [need / total for need in needs]


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


def place(canvas, x, y, text, font, size, color, align="left", track=0):
    canvas.setFont(font, size)
    canvas.setFillColor(color)
    (canvas.drawRightString if align == "right" else
     canvas.drawString)(x, y, clean(text), charSpace=track)


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


def paper(canvas, page_w, page_h, color=None):
    """The studio's cream sheet, laid under everything else; reportlab leaves paper white."""
    canvas.setFillColor(color or C["paper"])
    canvas.rect(0, 0, page_w, page_h, stroke=0, fill=1)


def furniture(canvas, doc, title, draft, landscape=False, sheet=False):
    """Header and footer, repeated on every page. Positions hang off the page edges so the
    same furniture fits the landscape spec sheet, and `sheet` prints it on the spec sheet's
    white page with light grey rules."""
    page_w, page_h = (LAND_W, LAND_H) if landscape else (PAGE_W, PAGE_H)
    content_w = page_w - 2 * M
    canvas.saveState()
    paper(canvas, page_w, page_h, SHEET["paper"] if sheet else None)
    logo_width = brand_image(canvas, CFG.get("logo_print_path") or CFG.get("logo_path"), M,
                             page_h - 57.6, height=LOGO_H)
    if not logo_width:
        place(canvas, M, page_h - 54, CFG["company_name"], FONT_BB, 13, C["dark"])
        logo_width = pdfmetrics.stringWidth(CFG["company_name"], FONT_BB, 13)
    eyebrow = clean(CFG.get("tagline", "Finish Schedule")).upper()
    place(canvas, page_w - M, page_h - 39.6, eyebrow, FONT_BB, 7, C["accent"], "right")
    size, shown = fit(title, FONT_H, 15, content_w - logo_width - 12, minimum=9)   # keep clear of the wordmark
    place(canvas, page_w - M, page_h - 55.44, shown, FONT_H, size, C["dark"], "right")
    canvas.setStrokeColor(SHEET["rule"] if sheet else C["dark"])
    canvas.setLineWidth(0.8)
    canvas.line(M, page_h - 72, page_w - M, page_h - 72)
    canvas.setStrokeColor(SHEET["rule"] if sheet else C["hairline"])
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
    """Client / project / address / date fields under the header, drawn as a ledger strip:
    hairlines above and below rather than a filled box, each field's share measured from
    what it prints, and the field's own gold underline doing the talking."""

    def __init__(self, values, width=CONTENT_W, value_size=9, value_font=FONT_B):
        super().__init__()
        # The spec sheet asks for its own type in the box - a bigger, bolder value - so the
        # client details read as their own element rather than more line-item text.
        self.values, self.width = values, width
        self.value_size, self.value_font = value_size, value_font

    def wrap(self, *_):
        return self.width, BAND_H

    def draw(self):
        canvas = self.canv
        canvas.setFillColor(C["band"])
        canvas.rect(0, 0, self.width, BAND_H, stroke=0, fill=1)
        canvas.setStrokeColor(C["hairline"])
        canvas.setLineWidth(0.5)
        canvas.line(0, BAND_H, self.width, BAND_H)
        canvas.line(0, 0, self.width, 0)
        pool = self.width - 2 * BAND_INSET - (len(self.values) - 1) * FIELD_GAP
        widths = [share * pool for share in band_shares(self.values, self.value_font, self.value_size)]
        x = BAND_INSET
        for (label, value), field_w in zip(self.values, widths):
            place(canvas, x, 33.5, label, FONT_BB, 6.5, C["muted"])
            size, shown = fit(value, self.value_font, self.value_size, field_w)
            place(canvas, x, 17, shown, self.value_font, size, C["text"])
            canvas.setStrokeColor(C["field_rule"])
            canvas.setLineWidth(0.5)
            canvas.line(x, 11, x + field_w, 11)
            x += field_w + FIELD_GAP


class Heading(Flowable):
    """Letter-spaced section heading with a rule running past the right margin.

    `track` draws plain uppercase letters with a small space after each in place of a full
    space between them - the tighter spacing the spec sheet's own headings use.
    """

    def __init__(self, text, width=CONTENT_W, keep=True, rule=None, track=0):
        super().__init__()
        self.text = clean(text).upper() if track else spaced_caps(text)
        self.width, self.track = width, track
        self.keepWithNext = 1 if keep else 0
        self.rule = rule or C["heading_rule"]

    def wrap(self, *_):
        return self.width, HEADING_H

    def draw(self):
        canvas = self.canv
        place(canvas, PAD_X, 5, self.text, FONT_BB, 10, C["dark"], track=self.track)
        text_w = (pdfmetrics.stringWidth(self.text, FONT_BB, 10)
                  + self.track * max(0, len(self.text) - 1))
        canvas.setStrokeColor(self.rule)
        canvas.setLineWidth(0.5)
        canvas.line(PAD_X + text_w + HEADING_GAP, 8, self.width + HEADING_OVERHANG, 8)


def section_block(text, table, width=CONTENT_W, frame_h=BODY_TOP - BODY_BOTTOM, rule=None,
                  track=0):
    """A section heading with its rows: the rows may split across pages, but the heading is
    never left stranded at the foot of one.

    reportlab's own keepWithNext pairs the heading with the *whole* table, so a section longer
    than a page moved itself - and left the page it was on empty below the client band. A
    maxHeight of the frame tells KeepTogether to split the rows in place instead - but it
    hands the frame the heading and the table separately, so when what is left of a page fits
    the heading yet not the table's first row, the table still moves over whole and strands
    the heading. A CondPageBreak therefore goes first, asking for the heading plus the least
    the table can open with - its repeated header and a data row, one row for an unheaded
    strip - so a section only ever starts where that much fits. The ask is capped at the
    frame height: a first row taller than a page is left to fail the way it always has,
    rather than break every page.

    Returns a list, not one flowable - the break has to reach the frame ahead of the block,
    so callers extend the story rather than append.
    """
    table.wrap(width, frame_h)                       # fixes the row heights the break asks for
    first = sum(table._rowHeights[:table.repeatRows + 1]) if table._rowHeights else 0
    return [CondPageBreak(min(HEADING_H + first, frame_h)),
            KeepTogether([Heading(text, width, keep=False, rule=rule, track=track), table],
                         maxHeight=frame_h)]


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
    frame_h = BODY_TOP - BODY_BOTTOM
    story = [Spacer(0, SECTION_GAP)]
    for index, (title, fields) in enumerate(groups):
        title_p = Paragraph(escape(title), GROUP_STYLE)
        table = Table([[Paragraph(escape(field["name"]), ITEM_STYLE),
                        Paragraph(escape(field["value"] or "Not provided").replace("\n", "<br/>"), DESC_STYLE)]
                       for field in fields],
                      colWidths=[CODE_W + ITEM_W, CONTENT_W - CODE_W - ITEM_W], style=TableStyle([
                          ("VALIGN", (0, 0), (-1, -1), "TOP"),
                          ("LEFTPADDING", (0, 0), (-1, -1), PAD_X),
                          ("RIGHTPADDING", (0, 0), (-1, -1), PAD_X),
                          ("TOPPADDING", (0, 0), (-1, -1), PAD_Y),
                          ("BOTTOMPADDING", (0, 0), (-1, -1), PAD_Y),
                          ("LINEBELOW", (0, 0), (-1, -2), 0.4, C["hairline"]),
                      ]))
        # the same rule section_block sets: a heading, or a group's own title, opens only
        # where its first row fits beneath it - never alone at the foot of a page.
        _, title_h = title_p.wrap(CONTENT_W, frame_h)
        table.wrap(CONTENT_W, frame_h)
        first = table._rowHeights[0] if table._rowHeights else 0
        need = title_p.getSpaceBefore() + title_h + first + (HEADING_H if index == 0 else 0)
        story.append(CondPageBreak(min(need, frame_h)))
        if index == 0:
            story.append(Heading("Additional details"))
        story += [title_p, table]
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
        story += section_block(section, table) if section else [table]
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


def _spec_field_name(row_index, column):
    """Unique form-field name for one spec-sheet cell, e.g. 'r3_item' or 'r3_model'."""
    slug = re.sub(r"[^a-z0-9]+", "_", column.lower()).strip("_")
    return f"r{row_index}_{slug}"


def spec_cell(row, column, hidden, row_index=0, room=0, fillable=True):
    """One spec-sheet cell as an editable form field, or as the printed value alone.

    Product Link stays a clickable link rather than a field, because it is a URL
    and the user is not expected to type one in by hand.  Everything else becomes
    an editable text field pre-filled with the current value.
    """
    if column == PRODUCT_LINK:
        url = product_link(row, hidden)
        if not url:
            return ""
        return Paragraph(f'<link href="{escape(url, {chr(34): "&quot;"})}" '
                         f'color="{CFG["colors"]["accent"]}"><u>{link_word()}</u></link>',
                         SPEC_CELL_STYLE)
    return SpecFieldCell(_spec_field_name(row_index, column),
                         spec_value(row, column), room, fillable=fillable)


def spec_table(items, columns, widths, hidden, pad_x=PAD_X, show_section=True,
               section_tag="", fillable=True):
    """A repeated-header table of every priced line item, one row per selection.

    Every data cell is an editable PDF form field so customers can type directly into the
    downloaded PDF.  A single Notes row at the end of the section provides space for
    additional entries.

    Hairlines run down the column gaps as well as under the rows: eight narrow columns of
    short values, many of them empty, read as one drifting line of text without them. They
    are the sheet's light grey at the studio weights.

    The gaps are secondary structure and read that way: half the weight of a row rule, and
    drawn only between the horizontal ones, from the heading rule down to the rule that
    closes the last row. That closing rule repeats the heading's weight, so the table ends
    the way it opened and nothing stops in mid-air; an open foot read as a row still
    waiting for its line. There is still no rule above the headings - a section's own
    heading rule, or the band above an unsectioned sheet, does that job.

    The row's Section prints under its item name in the same small note face as the
    lookbook flag, so the category a line item was filed under is readable in a
    room-grouped sheet. It is skipped when the sheet is grouped by Section - the
    heading already says it - and when the Category column was left out of the export.
    """
    # The room a field has for text: the column less its padding, which is the width
    # every value on the sheet was measured against.
    field_rooms = [max(w - 2 * pad_x, 10) for w in widths]
    data = [[Paragraph(escape(spec_heading(column)), SPEC_HEAD_STYLE) for column, _ in columns]]
    for row_idx, row in enumerate(items):
        cells = [spec_cell(row, column, hidden, row_index=row.get("_row", row_idx),
                           room=field_rooms[col_idx], fillable=fillable)
                 for col_idx, (column, _) in enumerate(columns)]
        notes = []
        if show_section and clean(row.get("Section")):
            notes.append(clean(row["Section"]))
        if "Include in Lookbook" not in hidden and clean(row.get("Include in Lookbook")).lower() == "no":
            notes.append("Not in lookbook")
        if notes:
            cells[0] = ([cells[0]] if cells[0] != "" else []) + \
                       [Paragraph(escape(note), SPEC_NOTE_STYLE) for note in notes]
        data.append(cells)
    # A single Notes row at the end of the section for the customer to add data.
    safe_tag = re.sub(r"[^a-z0-9]+", "_", (section_tag or "all").lower()).strip("_")
    notes_row = [SpecFieldCell(f"notes_{safe_tag}_{re.sub(r'[^a-z0-9]+', '_', col.lower()).strip('_')}",
                               "", room, fillable=fillable)
                 for (col, _), room in zip(columns, field_rooms)]
    # Label the first cell of the notes row so the customer knows it is for notes: a line
    # to type into on screen, and on a hard copy the open row the section's last rule closes.
    first_col = columns[0][0] if columns else "Item"
    notes_row[0] = SpecFieldCell(f"notes_{safe_tag}_{re.sub(r'[^a-z0-9]+', '_', first_col.lower()).strip('_')}",
                                 "Notes", field_rooms[0], fillable=fillable)
    data.append(notes_row)
    return Table(data, colWidths=widths, repeatRows=1, style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("VALIGN", (0, 0), (-1, 0), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), pad_x),
        ("RIGHTPADDING", (0, 0), (-1, -1), pad_x),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, SHEET["rule"]),
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, SHEET["rule"]),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, SHEET["rule"]),
        ("LINEAFTER", (0, 1), (-2, -1), 0.2, SHEET["rule"]),
    ]))


def spec_details(items, width, hidden):
    """A project's own figures read as a short list on open paper, never a boxed grid: pairs
    of label and figure across the page, each figure closing on its own gold hairline between
    the client band's two, which is how the portal's own box reads.

    The figure holds the right edge of its pair, so a column of areas lines up whether the
    label beside it takes one line or two, and the row's other filled columns follow as small
    print under the label. Tiled as band fields instead - a name over its figure, four across,
    padded for an underline - the same eight figures took a third of the sheet in white space.
    """
    figures = [detail_parts(row, hidden) for row in items]
    pair_w = (width - DETAIL_GUTTER * (DETAIL_PAIRS - 1)) / DETAIL_PAIRS
    # As wide as the widest figure, its own padding and the least gap a long label may leave.
    figure_w = min(max([pdfmetrics.stringWidth(figure, FONT_BB, DETAIL_FIGURE_STYLE.fontSize)
                        for figure, _ in figures] + [0]) + BAND_INSET + PAD_X,
                   pair_w * DETAIL_FIGURE_MAX)
    cells = []
    for row, (figure, rest) in zip(items, figures):
        small = escape(rest)
        url = product_link(row, hidden)
        if url:
            mark = (f'<link href="{escape(url, {chr(34): "&quot;"})}" '
                    f'color="{CFG["colors"]["accent"]}"><u>{link_word()}</u></link>')
            small = f"{small} · {mark}" if small else mark
        label = [Paragraph(escape(clean(row.get("Item")).upper()), DETAIL_HEAD_STYLE)]
        if small:
            label.append(Paragraph(small, SPEC_NOTE_STYLE))
        cells.append([label, Paragraph(escape(figure), DETAIL_FIGURE_STYLE)])
    for _ in range(-len(cells) % DETAIL_PAIRS):      # a short last row simply stays open
        cells.append(["", ""])
    data, widths = [], []
    for index in range(0, len(cells), DETAIL_PAIRS):
        line = []
        for pair in cells[index:index + DETAIL_PAIRS]:
            line += ([""] if line else []) + pair    # the gutter column between the pairs
        data.append(line)
    for index in range(DETAIL_PAIRS):
        widths += ([DETAIL_GUTTER] if widths else []) + [pair_w - figure_w, figure_w]
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("LEFTPADDING", (0, 0), (-1, -1), 0),
             ("RIGHTPADDING", (0, 0), (-1, -1), 0),
             ("TOPPADDING", (0, 0), (-1, -1), DETAIL_PAD_Y),
             ("BOTTOMPADDING", (0, 0), (-1, -1), DETAIL_PAD_Y),
             ("LINEABOVE", (0, 0), (-1, 0), 0.5, C["hairline"]),
             ("LINEBELOW", (0, -1), (-1, -1), 0.5, C["hairline"])]
    for index in range(DETAIL_PAIRS):
        label_col = 3 * index
        # The label keeps the band's own inset, the figure its padding and a gap from the
        # label, and the hairline under a figure stops at its pair rather than running on
        # across the gutter into the next one.
        style += [("LEFTPADDING", (label_col, 0), (label_col, -1), BAND_INSET),
                  ("TOPPADDING", (label_col, 0), (label_col, -1), DETAIL_PAD_Y + DETAIL_BASELINE),
                  ("LEFTPADDING", (label_col + 1, 0), (label_col + 1, -1), PAD_X),
                  ("RIGHTPADDING", (label_col + 1, 0), (label_col + 1, -1), BAND_INSET),
                  ("LINEBELOW", (label_col, 0), (label_col + 1, -2), 0.4, C["field_rule"])]
    return Table(data, colWidths=widths, style=TableStyle(style))


def build_spec(pid, project, rows, output_dir=None, hidden=(), group="Room / Area",
               orientation="landscape", room_order=(), copy=SPEC_COPIES[0]):
    """The internal spec sheet: the app's line items and columns, under the same headings,
    on the page orientation the download asked for, as the editable or the locked copy."""
    page = spec_page(orientation)
    fillable = spec_copy(copy) == "editable"
    columns = spec_columns(hidden)
    groups = spec_groups(rows, group, hidden, room_order=room_order)
    tabled = [row for section, items in groups.items() if not is_details_group(section)
              for row in items]
    widths = spec_widths(columns, page.content_w, tabled, page.pad_x, hidden)
    story = [Band(band_values(project), page.content_w, value_size=11, value_font=FONT_BB),
             Spacer(0, BAND_GAP)]
    for index, (section, items) in enumerate(groups.items()):
        if index:
            story.append(Spacer(0, SECTION_GAP))
        block = (spec_details(items, page.content_w, hidden)
                 if is_details_group(section)
                 else spec_table(items, columns, widths, hidden, page.pad_x,
                                 show_section=group != "Section" and "Section" not in hidden,
                                 section_tag=section, fillable=fillable))
        story += (section_block(section, block, page.content_w, page.body_top - BODY_BOTTOM,
                                rule=SHEET["rule"], track=SPEC_HEADING_TRACK)
                  if section else [block])

    destination = Path(output_dir or OUT)
    destination.mkdir(exist_ok=True)
    # The orientation is part of the name: both copies are downloaded to compare, and two
    # files called the same thing leave the browser to tell them apart with a "(1)". The
    # editable copy says so too, because on paper the two are the same page - the fields
    # only show in a reader - and the working copy mistaken for the one to send is the risk.
    parts = [re.sub(r"[^A-Za-z0-9_-]", "_", pid)[:50],
             re.sub(r"[^A-Za-z0-9]+", "_", clean(project.get("Project Name"))).strip("_")[:100],
             re.sub(r"[^A-Za-z0-9]+", "_", SPEC_TITLE).strip("_")[:60], page.name.title()]
    if fillable:
        parts.append("Editable")
    out = destination / ("_".join(part for part in parts if part) + ".pdf")
    doc = BaseDocTemplate(str(out), pagesize=(page.width, page.height), leftMargin=M, rightMargin=M,
                          topMargin=page.height - page.body_top, bottomMargin=BODY_BOTTOM,
                          title=f"{clean(project.get('Project Name')) or pid} - {SPEC_TITLE}",
                          author=CFG["company_name"], subject=f"Internal spec sheet for {pid}")
    frame = Frame(M, BODY_BOTTOM, page.content_w, page.body_top - BODY_BOTTOM, 0, 0, 0, 0, id="spec")
    doc.addPageTemplates([PageTemplate("spec", [frame],
                                       onPage=lambda canvas, document: furniture(canvas, document,
                                                                                 SPEC_TITLE, False,
                                                                                 landscape=page.landscape,
                                                                                 sheet=True))])
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
    parser.add_argument("--spec", action="store_true", help="Internal priced spec sheet")
    parser.add_argument("--orientation", default=SPEC_ORIENTATIONS[0], choices=SPEC_ORIENTATIONS,
                        help="Spec sheet page orientation (default: landscape)")
    parser.add_argument("--copy", default=SPEC_COPIES[0], choices=SPEC_COPIES,
                        help="Spec sheet copy: locked, or editable cells (default: locked)")
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
            out = build_spec(pid, projects[pid], spec_rows(wb, pid), hidden=hidden,
                             orientation=args.orientation, copy=args.copy)
            print(f"+ {pid}: {out.relative_to(BASE_DIR)}")
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
