"""
Editable PowerPoint twin of the client finish schedule.

Usage:
    python build_powerpoint.py --project UH-101
    python build_powerpoint.py --project UH-101 --draft
    python build_powerpoint.py --project UH-101 --title "Interior Selections" --prefix IN

Output: output/<ProjectID>_<Project Name>_<Schedule title>.pptx

Slides are letter portrait and share the PDF's measurements, so one point of the printed
schedule is one point here and both deliverables group and order rows identically (page
breaks can differ by a block at the very end). Every value is a plain text box, so the
schedule can be edited in PowerPoint without rebuilding it from the workbook.
"""
import argparse
import datetime as dt
import io
import re
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Pt
from reportlab.pdfbase import pdfmetrics
from reportlab.lib.utils import simpleSplit

from branding import load_logo
from build_lookbook import (BAND_GAP, BAND_H, BODY_BOTTOM, BODY_TOP, CFG, CODE_W, CONTENT_W, FIELD_GAP,
                            FIELD_W, FONT_B, FONT_BB, HEADING_GAP, HEADING_H, HEADING_OVERHANG, ITEM_W,
                            LOGO_H, M, MARK_W,
                            OUT, PAD_X, PAD_Y, PAGE_H, PAGE_W, SCHEDULE, SECTION_GAP, SIGN_H, SIGN_W,
                            SIGN_X, STATUS_W, band_values, clean, code_prefix, description, fit,
                            group_sections, load_data, numbered, presentation_details, schedule_title, spaced_caps)

COLORS = {key: value.lstrip("#").upper() for key, value in CFG["colors"].items()}
FAMILY = Path(CFG.get("fonts", {}).get("body") or "Lato").stem.split("-")[0] or "Lato"
DESC_W = CONTENT_W - CODE_W - ITEM_W
CODE, ITEM, ROOM, DESC, STATUS = (8, 12), (9, 11), (7, 9), (9, 11.5), (7, 9)   # (font size, leading)
NOTE = (7.2, 9.5)
ASCENT = 0.8          # cap height used to turn a PDF baseline into a text box top


def emu(points):
    return Emu(int(round(points * 12700)))


def top(y):
    """PDF coordinate (measured up from the page bottom) -> slide offset from the top edge."""
    return emu(PAGE_H - y)


def lines(text, size, width):
    return simpleSplit(clean(text), FONT_B, size, width) or [""]


def textbox(slide, x, y, width, height, text, size, color, bold=False, align=PP_ALIGN.LEFT, leading=None):
    """Text box whose top edge sits at PDF height y."""
    box = slide.shapes.add_textbox(emu(x), top(y), emu(width), emu(height))
    frame = box.text_frame
    frame.word_wrap = True
    frame.vertical_anchor = MSO_ANCHOR.TOP
    frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
    for index, line in enumerate(str(text or "").split("\n")):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.alignment = align
        paragraph.line_spacing = Pt(leading or size * 1.2)
        run = paragraph.add_run()
        run.text = line
        run.font.name, run.font.size, run.font.bold = FAMILY, Pt(size), bold
        run.font.color.rgb = RGBColor.from_string(color)
    return box


def label(slide, x, baseline, width, text, size, color, **kwargs):
    """Single line placed from its PDF baseline."""
    return textbox(slide, x, baseline + ASCENT * size, width, size * 1.4, text, size, color, **kwargs)


def rule(slide, x1, x2, y, color, width=0.5):
    line = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, emu(x1), top(y), emu(x2), top(y))
    line.line.color.rgb = RGBColor.from_string(color)
    line.line.width = emu(width)
    return line


def block(slide, x, y, width, height, color):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, emu(x), top(y), emu(width), emu(height))
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor.from_string(color)
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def picture(slide, source, x, baseline, height=None, width=None):
    """Logo placed from its bottom edge at the supplied aspect ratio; returns its width, or 0."""
    image = load_logo(source)
    if image is None:
        return 0
    stream = io.BytesIO()
    image.save(stream, "PNG")
    stream.seek(0)
    iw, ih = image.size
    height = height if height is not None else width * ih / iw
    width = width if width is not None else height * iw / ih
    slide.shapes.add_picture(stream, emu(x), top(baseline + height), emu(width), emu(height))
    return width


def cell(x, width, text, font, color, offset=0, align=PP_ALIGN.LEFT, bold=False):
    size, leading = font
    return {"x": x, "width": width, "text": clean(text), "size": size, "leading": leading,
            "color": color, "offset": offset, "align": align, "bold": bold}


def cell_height(item):
    return len(lines(item["text"], item["size"], item["width"] - 2 * PAD_X)) * item["leading"]


def row_cells(row, draft):
    width = DESC_W - (STATUS_W if draft else 0)
    item = cell(CODE_W, ITEM_W, row.get("Item"), ITEM, COLORS["dark"])
    cells = [cell(0, CODE_W, row.get("_code"), CODE, COLORS["muted"]), item,
             cell(CODE_W + ITEM_W, width, description(row), DESC, COLORS["body"])]
    if clean(row.get("Room / Area")):
        cells.append(cell(CODE_W, ITEM_W, row["Room / Area"], ROOM, COLORS["muted"], offset=cell_height(item)))
    if draft:
        cells.append(cell(CODE_W + ITEM_W + width, STATUS_W, clean(row.get("Lookup Status")) or "Not run",
                          STATUS, COLORS["muted"], align=PP_ALIGN.RIGHT))
    return cells


def detail_rows(project, rows):
    """(group heading, name, value) for project and selection custom fields."""
    return [(title if index == 0 else "", field["name"], field["value"] or "Not provided")
            for title, fields in presentation_details(project, rows)
            for index, field in enumerate(fields)]


class Deck:
    """Slides that repeat the schedule header and footer, with a running vertical cursor."""

    def __init__(self, title, draft):
        self.prs = Presentation()
        self.prs.slide_width, self.prs.slide_height = emu(PAGE_W), emu(PAGE_H)
        self.title, self.draft, self.slide, self.y = title, draft, None, BODY_BOTTOM

    def page(self):
        slide = self.prs.slides.add_slide(self.prs.slide_layouts[6])
        logo_width = picture(slide, CFG.get("logo_print_path") or CFG.get("logo_path"), M, 734.4, height=LOGO_H)
        if not logo_width:
            label(slide, M, 738, 200, CFG["company_name"], 13, COLORS["dark"], bold=True)
        eyebrow = clean(CFG.get("tagline", "Finish Schedule")).upper()
        label(slide, M, 752.4, CONTENT_W, f"{eyebrow}   \u00b7   DRAFT" if self.draft else eyebrow,
              7, COLORS["accent"], bold=True, align=PP_ALIGN.RIGHT)
        size, title = fit(self.title, FONT_B, 15, CONTENT_W - (logo_width or 160) - 12, minimum=9)
        label(slide, M, 736.56, CONTENT_W, title, size, COLORS["dark"], align=PP_ALIGN.RIGHT)
        rule(slide, M, PAGE_W - M, 720, COLORS["dark"], 0.8)
        rule(slide, M, PAGE_W - M, 44.64, COLORS["hairline"])
        has_mark = picture(slide, CFG.get("logo_mark_path"), M, 24.48, width=MARK_W)
        label(slide, 64.8 if has_mark else M, 28.8, 400, CFG.get("contact_line", ""), 7.5, COLORS["muted"])
        label(slide, PAGE_W - M - 100, 28.8, 100, f"Page {len(self.prs.slides)}", 7.5, COLORS["muted"],
              align=PP_ALIGN.RIGHT)
        if self.draft:
            mark = textbox(slide, PAGE_W / 2 - 200, PAGE_H / 2 + 60, 400, 140, "DRAFT", 120,
                           COLORS["band"], bold=True, align=PP_ALIGN.CENTER)
            mark.rotation = 332
        self.slide, self.y = slide, BODY_TOP
        return slide

    def room(self, height):
        """Start a new slide when the next block no longer fits above the footer."""
        if self.slide is None or self.y - height < BODY_BOTTOM:
            self.page()
        return self.slide

    def band(self, values):
        slide = self.room(BAND_H)
        bottom = self.y - BAND_H
        block(slide, M, self.y, CONTENT_W, BAND_H, COLORS["band"])
        for index, (field, value) in enumerate(values):
            x = M + 10.8 + index * (FIELD_W + FIELD_GAP)
            label(slide, x, bottom + 33.5, FIELD_W, field, 6.5, COLORS["muted"], bold=True)
            label(slide, x, bottom + 17, FIELD_W, value, 9, COLORS["text"])
            rule(slide, x, x + FIELD_W, bottom + 11, COLORS["field_rule"])
        self.y = bottom - BAND_GAP

    def heading(self, text, first=False):
        if not first:
            self.y -= SECTION_GAP
        slide = self.room(HEADING_H + 25)
        text = spaced_caps(text)
        label(slide, M + PAD_X, self.y - HEADING_H + 5, CONTENT_W, text, 8, COLORS["dark"], bold=True)
        rule(slide, M + PAD_X + pdfmetrics.stringWidth(text, FONT_BB, 8) + HEADING_GAP,
             PAGE_W - M + HEADING_OVERHANG, self.y - HEADING_H + 8, COLORS["heading_rule"])
        self.y -= HEADING_H

    def row(self, cells, hairline):
        height = max([CODE[1]] + [item["offset"] + cell_height(item) for item in cells]) + 2 * PAD_Y
        slide = self.room(height)
        for item in cells:
            textbox(slide, M + item["x"] + PAD_X, self.y - PAD_Y - item["offset"], item["width"] - 2 * PAD_X,
                    cell_height(item), item["text"], item["size"], item["color"], bold=item["bold"],
                    align=item["align"], leading=item["leading"])
        self.y -= height
        if hairline:
            rule(slide, M, PAGE_W - M, self.y, COLORS["hairline"], 0.4)

    def note_and_signatures(self):
        note = clean(SCHEDULE.get("note"))
        height = len(lines(note, NOTE[0], CONTENT_W - 2 * PAD_X)) * NOTE[1]
        self.y -= SECTION_GAP
        slide = self.room(height + 16 + SIGN_H)
        textbox(slide, M + PAD_X, self.y, CONTENT_W - 2 * PAD_X, height, note, NOTE[0], COLORS["muted"],
                leading=NOTE[1])
        self.y -= height + 16
        x = M + SIGN_X
        for text, width in zip(SCHEDULE.get("signatures") or [], SIGN_W):
            rule(slide, x, x + width, self.y - SIGN_H + 18, COLORS["dark"])
            label(slide, x, self.y - SIGN_H + 7.5, width, text, 7.5, COLORS["muted"])
            x += width
        self.y -= SIGN_H

    def save(self, pid, project, output_dir, rev=None):
        self.prs.core_properties.title = f"{clean(project.get('Project Name')) or pid} - {self.title}"
        self.prs.core_properties.author = CFG["company_name"]
        if rev:
            self.prs.core_properties.subject = f"Revision {rev} - exported {dt.date.today().isoformat()}"
        destination = Path(output_dir or OUT)
        destination.mkdir(exist_ok=True)
        parts = [re.sub(r"[^A-Za-z0-9_-]", "_", pid)[:50],
                 re.sub(r"[^A-Za-z0-9]+", "_", clean(project.get("Project Name"))).strip("_")[:100],
                 re.sub(r"[^A-Za-z0-9]+", "_", self.title).strip("_")[:60],
                 f"R{rev}" if rev else ""]
        output = destination / ("_".join(part for part in parts if part) + ("_DRAFT" if self.draft else "") + ".pptx")
        self.prs.save(output)
        return output


def build(pid, project, rows, draft=False, output_dir=None, title=None, prefix=None, rev=None):
    deck = Deck(schedule_title(title), draft)
    rows = numbered(rows, code_prefix(prefix))
    deck.page()
    deck.band(band_values(project))
    for index, (section, items) in enumerate(group_sections(rows).items()):
        deck.heading(section, first=index == 0)
        for position, row in enumerate(items):
            deck.row(row_cells(row, draft), position < len(items) - 1)
    details = detail_rows(project, rows)
    if details:
        deck.heading("Additional details")
        for index, (heading, name, value) in enumerate(details):
            if heading:
                deck.room(45)
                label(deck.slide, M + PAD_X, deck.y - 11, CONTENT_W, heading, 9, COLORS["dark"], bold=True)
                deck.y -= 15
            deck.row([cell(0, CODE_W + ITEM_W, name, ITEM, COLORS["dark"]),
                      cell(CODE_W + ITEM_W, DESC_W, value, DESC, COLORS["body"])],
                     index < len(details) - 1)
    deck.note_and_signatures()
    return deck.save(pid, project, output_dir, rev)


def main():
    parser = argparse.ArgumentParser(description="Generate the editable UH Homes finish schedule from the master workbook.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--verified-only", action="store_true")
    parser.add_argument("--title", help=f"Schedule title (default: {SCHEDULE.get('title', 'Selections')})")
    parser.add_argument("--prefix", help=f"Item code prefix (default: {SCHEDULE.get('code_prefix', 'EX')})")
    args = parser.parse_args()
    projects, items = load_data(args.verified_only)
    if args.project not in projects or not items.get(args.project):
        parser.error("Project not found or no selections match the export options.")
    rows = items[args.project]
    if not args.draft and any(row["Lookup Status"] != "Verified" for row in rows):
        parser.error("Review all selections first, or use --draft or --verified-only.")
    print(build(args.project, projects[args.project], rows, args.draft, title=args.title, prefix=args.prefix))


if __name__ == "__main__":
    main()
