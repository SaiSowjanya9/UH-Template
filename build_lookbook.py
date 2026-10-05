"""
Build the client finish schedule PDF from the tracker workbook.

Usage:
    python build_lookbook.py --project UH-101                              # one project
    python build_lookbook.py --all                                         # every project
    python build_lookbook.py --all --verified-only                         # only rows marked 'Verified'
    python build_lookbook.py --project UH-101 --draft                      # internal copy (watermark + status)
    python build_lookbook.py --project UH-101 --title "Interior Selections" --prefix IN

Output: output/<ProjectID>_<Project Name>_<Schedule title>.pdf

The layout follows the approved UH Homes finish schedule: letter portrait, wordmark and
schedule title over a ruled header, a client/project band, letter-spaced ruled section
headings, one row per selection (code / item / description), the client note and a
sign-off block. Brand colors, fonts, logo, contact line and the schedule defaults
(title, code prefix, note, signature labels) live in lookbook_config.json.
"""
import argparse
import datetime as dt
import json
import re
from collections import OrderedDict
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, KeepTogether, PageTemplate,
                                Paragraph, Spacer, Table, TableStyle)

from branding import load_logo
from common import BASE_DIR, Sheet, clean, open_workbook, section_order
from workbook_store import presentation_details

PAGE_W, PAGE_H = letter          # 612 x 792 pt, portrait
M = 46.8                         # side margins
CONTENT_W = PAGE_W - 2 * M       # 518.4
BODY_TOP, BODY_BOTTOM = 696, 56  # flowing content area between the header and footer rules
HEAD_RULE_Y, FOOT_RULE_Y = 720, 44.64
LOGO_H, MARK_W = 20.16, 11.52
CODE_W, ITEM_W, STATUS_W = 50.4, 144, 70   # STATUS_W is used by draft copies only
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


CODE_STYLE = style("code", 8, 12, C["muted"])
ITEM_STYLE = style("item", 9, 11, C["dark"])
ROOM_STYLE = style("room", 7, 9, C["muted"])
DESC_STYLE = style("description", 9, 11.5, C["body"])
STATUS_STYLE = style("status", 7, 9, C["muted"], align=TA_RIGHT)
GROUP_STYLE = style("group", 9, 12, C["dark"], font=FONT_BB, spaceBefore=8, spaceAfter=3, leftIndent=PAD_X)
NOTE_STYLE = style("note", 7.2, 9.5, C["muted"], leftIndent=PAD_X, rightIndent=PAD_X)


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


def load_data(verified_only):
    wb = open_workbook(data_only=True)
    order = section_order(wb)
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
        status = clean(data.get("Lookup Status")) or "Not run"
        if verified_only and status != "Verified":
            continue
        record = {k: clean(v) for k, v in data.items()}
        record.update({"Lookup Status": status, "_row": row})
        items.setdefault(pid, []).append(record)

    def sort_key(record):
        section = record.get("Section", "")
        return (order.index(section) if section in order else len(order), section, record["_row"])

    for pid in items:
        items[pid].sort(key=sort_key)
    return projects, items


def group_sections(rows):
    groups = OrderedDict()
    for row in rows:
        groups.setdefault(clean(row.get("Section")) or "Other", []).append(row)
    return groups


def numbered(rows, prefix):
    """Give every selection its schedule code (EX-01, EX-02 ...) in document order."""
    digits = max(2, len(str(len(rows))))
    return [dict(row, _code=f"{prefix}-{index:0{digits}d}") for index, row in enumerate(rows, start=1)]


def description(row, include_quantity=True):
    """One plain-English line per selection, assembled from the workbook columns.

    Tables that show quantity in their own column pass include_quantity=False.
    """
    product = clean(row.get("Product Name")) or clean(row.get("Model #"))
    finish = clean(row.get("Finish / Color"))
    identity = " ".join(filter(None, [clean(row.get("Manufacturer")), product]))
    if not product and finish:
        identity, finish = " ".join(filter(None, [identity, finish])), ""
    model = clean(row.get("Model #"))
    if model and product != model and model.lower() not in identity.lower():
        identity = f"{identity} (Model {model})".strip()
    segments = [identity, finish, quantity(row) if include_quantity else "", clean(row.get("Client Notes"))]
    return " \u2013 ".join(segment for segment in segments if segment)


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
    combined = " / ".join(filter(None, [primary, secondary]))
    if secondary and pdfmetrics.stringWidth(combined, FONT_B, 9) > FIELD_W:
        return primary
    return combined


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


def furniture(canvas, doc, title, draft):
    """Header and footer, repeated on every page."""
    canvas.saveState()
    logo_width = brand_image(canvas, CFG.get("logo_print_path") or CFG.get("logo_path"), M, 734.4, height=LOGO_H)
    if not logo_width:
        place(canvas, M, 738, CFG["company_name"], FONT_BB, 13, C["dark"])
        logo_width = pdfmetrics.stringWidth(CFG["company_name"], FONT_BB, 13)
    eyebrow = clean(CFG.get("tagline", "Finish Schedule")).upper()
    place(canvas, PAGE_W - M, 752.4, f"{eyebrow}   \u00b7   DRAFT" if draft else eyebrow, FONT_BB, 7, C["accent"], "right")
    size, shown = fit(title, FONT_H, 15, CONTENT_W - logo_width - 12, minimum=9)   # keep clear of the wordmark
    place(canvas, PAGE_W - M, 736.56, shown, FONT_H, size, C["dark"], "right")
    canvas.setStrokeColor(C["dark"])
    canvas.setLineWidth(0.8)
    canvas.line(M, HEAD_RULE_Y, PAGE_W - M, HEAD_RULE_Y)
    canvas.setStrokeColor(C["hairline"])
    canvas.setLineWidth(0.5)
    canvas.line(M, FOOT_RULE_Y, PAGE_W - M, FOOT_RULE_Y)
    has_mark = brand_image(canvas, CFG.get("logo_mark_path"), M, 24.48, width=MARK_W)
    place(canvas, 64.8 if has_mark else M, 28.8, CFG.get("contact_line", ""), FONT_B, 7.5, C["muted"])
    place(canvas, PAGE_W - M, 28.8, f"Page {doc.page}", FONT_B, 7.5, C["muted"], "right")
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

    def __init__(self, values):
        super().__init__()
        self.values = values

    def wrap(self, *_):
        return CONTENT_W, BAND_H

    def draw(self):
        canvas = self.canv
        canvas.setFillColor(C["band"])
        canvas.rect(0, 0, CONTENT_W, BAND_H, stroke=0, fill=1)
        for index, (label, value) in enumerate(self.values):
            x = 10.8 + index * (FIELD_W + FIELD_GAP)
            place(canvas, x, 33.5, label, FONT_BB, 6.5, C["muted"])
            size, shown = fit(value, FONT_B, 9, FIELD_W)
            place(canvas, x, 17, shown, FONT_B, size, C["text"])
            canvas.setStrokeColor(C["field_rule"])
            canvas.setLineWidth(0.5)
            canvas.line(x, 11, x + FIELD_W, 11)


class Heading(Flowable):
    """Letter-spaced section heading with a rule running past the right margin."""

    def __init__(self, text):
        super().__init__()
        self.text = spaced_caps(text)
        self.keepWithNext = 1

    def wrap(self, *_):
        return CONTENT_W, HEADING_H

    def draw(self):
        canvas = self.canv
        place(canvas, PAD_X, 5, self.text, FONT_BB, 8, C["dark"])
        canvas.setStrokeColor(C["heading_rule"])
        canvas.setLineWidth(0.5)
        canvas.line(PAD_X + pdfmetrics.stringWidth(self.text, FONT_BB, 8) + HEADING_GAP, 8,
                    CONTENT_W + HEADING_OVERHANG, 8)


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


def rows_table(rows, draft):
    widths = [CODE_W, ITEM_W, CONTENT_W - CODE_W - ITEM_W - (STATUS_W if draft else 0)]
    data = []
    for row in rows:
        item = [Paragraph(escape(clean(row.get("Item"))), ITEM_STYLE)]
        if clean(row.get("Room / Area")):
            item.append(Paragraph(escape(clean(row["Room / Area"])), ROOM_STYLE))
        cells = [Paragraph(escape(row.get("_code", "")), CODE_STYLE), item,
                 Paragraph(escape(description(row)), DESC_STYLE)]
        if draft:
            cells.append(Paragraph(escape(clean(row.get("Lookup Status")) or "Not run"), STATUS_STYLE))
        data.append(cells)
    return Table(data, colWidths=widths + ([STATUS_W] if draft else []), style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), PAD_X),
        ("RIGHTPADDING", (0, 0), (-1, -1), PAD_X),
        ("TOPPADDING", (0, 0), (-1, -1), PAD_Y),
        ("BOTTOMPADDING", (0, 0), (-1, -1), PAD_Y),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, C["hairline"]),
    ]))


def detail_flowables(project, rows):
    """Project and selection custom fields, in the same two-column rhythm."""
    groups = presentation_details(project, rows)
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
def build(pid, project, rows, draft=False, output_dir=None, title=None, prefix=None, rev=None):
    title, rows = schedule_title(title), numbered(rows, code_prefix(prefix))
    story = [Band(band_values(project)), Spacer(0, BAND_GAP)]
    for index, (section, items) in enumerate(group_sections(rows).items()):
        if index:
            story.append(Spacer(0, SECTION_GAP))
        story += [Heading(section), rows_table(items, draft)]
    story += detail_flowables(project, rows)
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
    doc.addPageTemplates([PageTemplate("schedule", [frame],
                                       onPage=lambda canvas, document: furniture(canvas, document, title, draft))])
    doc.build(story)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--project", help="Project ID, e.g. UH-101")
    group.add_argument("--all", action="store_true", help="Every project on the Projects sheet")
    parser.add_argument("--verified-only", action="store_true", help="Only include rows marked Verified")
    parser.add_argument("--draft", action="store_true", help="Internal review copy with status labels")
    parser.add_argument("--title", help=f"Schedule title (default: {SCHEDULE.get('title', 'Selections')})")
    parser.add_argument("--prefix", help=f"Item code prefix (default: {SCHEDULE.get('code_prefix', 'EX')})")
    args = parser.parse_args()

    projects, items = load_data(args.verified_only)
    for pid in (list(projects) if args.all else [args.project]):
        if pid not in projects:
            print(f"x {pid}: not on the Projects sheet")
            continue
        rows = items.get(pid, [])
        if not rows:
            print(f"- {pid}: no selections to include, skipped")
            continue
        out = build(pid, projects[pid], rows, args.draft, title=args.title, prefix=args.prefix)
        print(f"+ {pid}: {len(rows)} selections -> {out.relative_to(BASE_DIR)}")
        pending = [row for row in rows if row["Lookup Status"] != "Verified"]
        if pending and not args.draft:
            print(f"  ! {len(pending)} row(s) not marked Verified (sheet rows "
                  f"{', '.join(str(row['_row']) for row in pending[:12])}{'...' if len(pending) > 12 else ''}). "
                  "Review before sending, or use --verified-only.")


if __name__ == "__main__":
    main()
