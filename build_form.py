"""
The fillable twin of the client finish schedule.

Same page as the deliverable - wordmark, ruled header, client band, letter-spaced section
headings, coded rows, note and sign-off - but the client details and each line's
description are real PDF form fields, and every line carries a small remove box.

It is a working document: fill it in any PDF reader, then import it back to update the
workbook. Field names carry the workbook row, so a filled form maps back exactly even
after rows move. The client-facing PDF produced by build_lookbook.py is unchanged.
"""
import re
from pathlib import Path

from reportlab.lib.colors import Color
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, KeepTogether, PageTemplate,
                                Paragraph, Spacer, Table, TableStyle)
from xml.sax.saxutils import escape

from build_lookbook import (BAND_FIELDS, BAND_GAP, BAND_H, BODY_BOTTOM, BODY_TOP, C, CFG, CODE_STYLE,
                            CODE_W, CONTENT_W, DESC_STYLE, FIELD_GAP, FIELD_W, FONT_B, FONT_BB, Heading,
                            ITEM_STYLE, ITEM_W, M, NOTE_STYLE, OUT, PAD_X, PAD_Y, PAGE_H, PAGE_W,
                            ROOM_STYLE, SCHEDULE, SECTION_GAP, SignOff, band_values, clean, code_prefix,
                            description, furniture, group_sections, numbered, place, schedule_title)

REMOVE_W = 42           # the remove column only exists on the fillable copy
FIELD_H = 15
BORDER = Color(0.78, 0.74, 0.65)
# PDF form fields accept only the standard 14 fonts, so typed text uses Helvetica
# while every label, item and heading on the page keeps the brand font.
FORM_FONT = "Helvetica"
# Those fonts are WinAnsi only, so typographic punctuation is transliterated.
SAFE_CHARACTERS = {0x2013: "-", 0x2014: "-", 0x2018: "'", 0x2019: "'", 0x201C: '"',
                   0x201D: '"', 0x2026: "...", 0x00A0: " "}


def form_text(value):
    """A value a PDF form field can hold."""
    return clean(value).translate(SAFE_CHARACTERS).encode("latin-1", "replace").decode("latin-1")


def field_name(row, suffix):
    return f"row{int(row)}_{suffix}"


class FieldCell(Flowable):
    """A single-line text field sized to its table cell."""

    def __init__(self, name, value, width, height=FIELD_H, size=9, align="left"):
        super().__init__()
        self.name, self.value, self.width, self.height = name, form_text(value), width, height
        self.size, self.align = size, align

    def wrap(self, *_):
        return self.width, self.height

    def draw(self):
        self.canv.acroForm.textfield(
            name=self.name, value=self.value, x=0, y=0, width=self.width, height=self.height,
            fontName=FORM_FONT, fontSize=self.size, textColor=C["body"], fillColor=None,
            borderColor=BORDER, borderWidth=0.5, forceBorder=True, relative=True,
            fieldFlags="doNotSpellCheck")


class RemoveBox(Flowable):
    """Tick to delete this line on import."""

    def __init__(self, name):
        super().__init__()
        self.name = name

    def wrap(self, *_):
        return REMOVE_W, FIELD_H

    def draw(self):
        self.canv.acroForm.checkbox(name=self.name, x=REMOVE_W / 2 - 5, y=1.5, size=10,
                                    checked=False, borderColor=BORDER, fillColor=None,
                                    borderWidth=0.5, relative=True)


class FormBand(Flowable):
    """The client band, with each value as a form field."""

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
            name = BAND_FIELDS[index].lower().replace(" / ", "_").replace(" ", "_")
            # project_lot prints two columns at once, so it is shown but not editable
            flags = "doNotSpellCheck readOnly" if name == "project_lot" else "doNotSpellCheck"
            canvas.acroForm.textfield(
                name=name, value=form_text(value), x=x, y=11, width=FIELD_W, height=FIELD_H,
                fontName=FORM_FONT, fontSize=9, textColor=C["text"], fillColor=None,
                borderColor=BORDER, borderWidth=0.5, forceBorder=True, relative=True, fieldFlags=flags)


def rows_table(rows):
    description_width = CONTENT_W - CODE_W - ITEM_W - REMOVE_W
    data = []
    for row in rows:
        item = [Paragraph(escape(clean(row.get("Item"))), ITEM_STYLE)]
        if clean(row.get("Room / Area")):
            item.append(Paragraph(escape(clean(row["Room / Area"])), ROOM_STYLE))
        data.append([Paragraph(escape(row.get("_code", "")), CODE_STYLE), item,
                     FieldCell(field_name(row["_row"], "desc"), description(row), description_width - 2 * PAD_X),
                     RemoveBox(field_name(row["_row"], "remove"))])
    return Table(data, colWidths=[CODE_W, ITEM_W, description_width, REMOVE_W], style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), PAD_X),
        ("RIGHTPADDING", (0, 0), (-1, -1), PAD_X),
        ("TOPPADDING", (0, 0), (-1, -1), PAD_Y),
        ("BOTTOMPADDING", (0, 0), (-1, -1), PAD_Y),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, C["hairline"]),
    ]))


def column_note(canvas, doc, title):
    furniture(canvas, doc, title, False)
    canvas.saveState()
    place(canvas, PAGE_W - M - REMOVE_W + 2, BODY_TOP + 6, "REMOVE", FONT_BB, 6, C["muted"])
    canvas.restoreState()


def form_band_values(project):
    """Band values for the working copy: the date stays ISO so it round-trips exactly."""
    values = band_values(project)
    return [(label, clean(project.get("Presentation Date"))[:10] if label == "DATE" else value)
            for label, value in values]


def build(pid, project, rows, output_dir=None, title=None, prefix=None):
    title, rows = schedule_title(title), numbered(rows, code_prefix(prefix))
    story = [FormBand(form_band_values(project)), Spacer(0, BAND_GAP)]
    for index, (section, items) in enumerate(group_sections(rows).items()):
        if index:
            story.append(Spacer(0, SECTION_GAP))
        story += [Heading(section), rows_table(items)]
    story.append(Spacer(0, SECTION_GAP))
    story.append(KeepTogether([Paragraph(escape(clean(SCHEDULE.get("note"))), NOTE_STYLE), Spacer(0, 16),
                               SignOff(SCHEDULE.get("signatures") or [])]))

    destination = Path(output_dir or OUT)
    destination.mkdir(exist_ok=True)
    parts = [re.sub(r"[^A-Za-z0-9_-]", "_", pid)[:50],
             re.sub(r"[^A-Za-z0-9]+", "_", clean(project.get("Project Name"))).strip("_")[:100],
             re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:60], "FILLABLE"]
    out = destination / ("_".join(part for part in parts if part) + ".pdf")
    doc = BaseDocTemplate(str(out), pagesize=(PAGE_W, PAGE_H), leftMargin=M, rightMargin=M,
                          topMargin=PAGE_H - BODY_TOP, bottomMargin=BODY_BOTTOM,
                          title=f"{clean(project.get('Project Name')) or pid} - {title} (fillable)",
                          author=CFG["company_name"], subject=f"Fillable working copy for {pid}")
    frame = Frame(M, BODY_BOTTOM, CONTENT_W, BODY_TOP - BODY_BOTTOM, 0, 0, 0, 0, id="schedule")
    doc.addPageTemplates([PageTemplate("schedule", [frame],
                                       onPage=lambda canvas, document: column_note(canvas, document, title))])
    doc.build(story)
    return out


def read(path_or_stream):
    """Field values from a filled form: client details, per-row text and remove ticks."""
    from pypdf import PdfReader

    fields = PdfReader(path_or_stream).get_fields() or {}
    details, lines = {}, {}
    for name, field in fields.items():
        value = field.get("/V")
        value = "" if value is None else str(value)
        match = re.fullmatch(r"row(\d+)_(desc|remove)", str(name))
        if match:
            row = int(match[1])
            entry = lines.setdefault(row, {})
            if match[2] == "desc":
                entry["description"] = clean(value)
            else:
                entry["remove"] = value.strip("/").lower() in {"yes", "on", "1", "true"}
        elif str(name) in {"client", "project_lot", "address", "date"}:
            details[str(name)] = clean(value)
    return details, lines
