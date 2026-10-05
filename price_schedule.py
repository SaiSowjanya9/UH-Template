"""
The 'Price Schedule' sheet: every selection grouped by project and category, priced.

The sheet is a view, rebuilt from the Selections sheet whenever the workbook is saved, so
it is never edited directly. Qty, Unit Price and the line total are live formulas pointing
at Selections, which means prices typed on Selections (or in the web app) flow straight
through, together with per-category subtotals, a project total and a workbook grand total.

Enter prices in the Unit Price column on Selections. Price = Qty x Unit Price.
"""
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from common import Sheet, clean, section_order

SHEET = "Price Schedule"
HEADERS = ["S.No", "Code", "Description", "Qty", "Unit Price", "Markup %", "Price"]
WIDTHS = [7, 10, 78, 7, 13, 11, 15]
MONEY = '#,##0.00'
TOTAL_COLUMN = len(HEADERS)          # the Price column carries every total
TOTAL_LETTER = get_column_letter(TOTAL_COLUMN)

DARK, ACCENT, MUTED = "FF2F3337", "FFC9A24A", "FF7A7E82"
BAND, HAIRLINE = "FFF5F5F3", "FFE3E3E1"
TITLE_FONT = Font(name="Calibri", size=15, bold=True, color=DARK)
NOTE_FONT = Font(name="Calibri", size=9, italic=True, color=MUTED)
HEAD_FONT = Font(name="Calibri", size=10, bold=True, color="FFFFFFFF")
PROJECT_FONT = Font(name="Calibri", size=11, bold=True, color=DARK)
SECTION_FONT = Font(name="Calibri", size=10, bold=True, color=DARK)
BODY_FONT = Font(name="Calibri", size=10, color=DARK)
TOTAL_FONT = Font(name="Calibri", size=10, bold=True, color=DARK)
GRAND_FONT = Font(name="Calibri", size=11, bold=True, color=DARK)
HEAD_FILL = PatternFill("solid", fgColor=DARK)
PROJECT_FILL = PatternFill("solid", fgColor=ACCENT)
SECTION_FILL = PatternFill("solid", fgColor=BAND)
UNDERLINE = Border(bottom=Side(style="thin", color=HAIRLINE))
TOP_RULE = Border(top=Side(style="thin", color=DARK))


def code_prefix(default="EX"):
    try:
        import json
        from common import BASE_DIR
        config = json.loads((BASE_DIR / "lookbook_config.json").read_text(encoding="utf-8"))
        return clean(config.get("schedule", {}).get("code_prefix")) or default
    except (OSError, ValueError):
        return default


def selection_rows(wb):
    """Selections grouped as {project id: {section: [(sheet row, data), ...]}} in print order."""
    order = section_order(wb)
    sheet = Sheet(wb["Selections"])
    projects = [clean(data.get("Project ID")) for _, data in Sheet(wb["Projects"]).rows()]
    grouped = {}
    for row, data in sheet.rows():
        pid = clean(data.get("Project ID"))
        if not pid or not (clean(data.get("Item")) or clean(data.get("Manufacturer"))):
            continue
        grouped.setdefault(pid, []).append((row, data))
    for pid in grouped:
        grouped[pid].sort(key=lambda entry: (
            order.index(clean(entry[1].get("Section"))) if clean(entry[1].get("Section")) in order else len(order),
            clean(entry[1].get("Section")), entry[0]))
    return [(pid, grouped[pid]) for pid in projects if pid in grouped] + \
           [(pid, rows) for pid, rows in grouped.items() if pid not in projects]


def rebuild(wb):
    """Recreate the Price Schedule sheet from the current Selections rows."""
    from build_lookbook import description   # local import: build_lookbook imports workbook_store

    if SHEET in wb.sheetnames:
        wb.remove(wb[SHEET])
    ws = wb.create_sheet(SHEET)
    ws.sheet_view.showGridLines = False
    source = Sheet(wb["Selections"])
    columns = {header: get_column_letter(index) for header, index in source.cols.items()}
    projects = {clean(data.get("Project ID")): data for _, data in Sheet(wb["Projects"]).rows()}
    prefix = code_prefix()

    for index, width in enumerate(WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws["A1"], ws["A1"].font = "Price Schedule", TITLE_FONT
    ws["A2"] = ("Rebuilt automatically from the Selections sheet - do not edit here. Enter costs in the "
                "Unit Price and Markup % columns on Selections; Price = Qty x Unit Price x (1 + Markup %).")
    ws["A2"].font = NOTE_FONT
    ws.merge_cells("A2:F2")
    for index, header in enumerate(HEADERS, start=1):
        cell = ws.cell(4, index, header)
        cell.font, cell.fill = HEAD_FONT, HEAD_FILL
        cell.alignment = Alignment(horizontal="right" if index >= 4 else "left", vertical="center")
    ws.freeze_panes = "A5"

    def money(cell, bold=False):
        cell.number_format = MONEY
        cell.alignment = Alignment(horizontal="right")
        cell.font = TOTAL_FONT if bold else BODY_FONT

    line, project_totals = 5, []
    for pid, entries in selection_rows(wb):
        name = clean(projects.get(pid, {}).get("Project Name"))
        cell = ws.cell(line, 1, f"{pid}   {name}".strip())
        cell.font, cell.alignment = PROJECT_FONT, Alignment(vertical="center")
        for column in range(1, len(HEADERS) + 1):
            ws.cell(line, column).fill = PROJECT_FILL
        ws.merge_cells(start_row=line, start_column=1, end_row=line, end_column=len(HEADERS))
        line += 1
        section_totals, number = [], 0
        current = None
        for position, (source_row, data) in enumerate(entries):
            section = clean(data.get("Section")) or "Other"
            if section != current:
                if current is not None:
                    section_totals.append(subtotal(ws, line, start, line - 1, current))
                    line += 1
                current, start = section, line + 1
                cell = ws.cell(line, 1, section.upper())
                cell.font = SECTION_FONT
                for column in range(1, len(HEADERS) + 1):
                    ws.cell(line, column).fill = SECTION_FILL
                ws.merge_cells(start_row=line, start_column=1, end_row=line, end_column=len(HEADERS))
                line += 1
            number += 1
            digits = max(2, len(str(len(entries))))
            ws.cell(line, 1, number).font = BODY_FONT
            ws.cell(line, 1).alignment = Alignment(horizontal="left")
            ws.cell(line, 2, f"{prefix}-{position + 1:0{digits}d}").font = BODY_FONT
            text = ws.cell(line, 3, description(data, include_quantity=False) or clean(data.get("Item")))
            text.font, text.alignment = BODY_FONT, Alignment(wrap_text=True, vertical="top")
            qty = f"Selections!{columns['Qty']}{source_row}"
            ws.cell(line, 4, f'=IF({qty}="",1,{qty})').font = BODY_FONT
            ws.cell(line, 4).alignment = Alignment(horizontal="right")
            markup = ws.cell(line, 6)
            markup.font, markup.alignment = BODY_FONT, Alignment(horizontal="right")
            markup.number_format = '0.##'
            if "Markup %" in columns:
                cell_ref = f"Selections!{columns['Markup %']}{source_row}"
                markup.value = f'=IF({cell_ref}="","",{cell_ref})'
            if "Unit Price" in columns:
                unit = f"Selections!{columns['Unit Price']}{source_row}"
                money(ws.cell(line, 5, f'=IF({unit}="","",{unit})'))
                money(ws.cell(line, TOTAL_COLUMN,
                              f'=IF(ISNUMBER(E{line}),D{line}*E{line}*(1+IF(ISNUMBER(F{line}),F{line},0)/100),"")'))
            else:
                money(ws.cell(line, 5))
                money(ws.cell(line, TOTAL_COLUMN))
            for column in range(1, len(HEADERS) + 1):
                ws.cell(line, column).border = UNDERLINE
            line += 1
        if current is not None:
            section_totals.append(subtotal(ws, line, start, line - 1, current))
            line += 1
        cell = ws.cell(line, 3, f"{pid} total")
        cell.font, cell.alignment = TOTAL_FONT, Alignment(horizontal="right")
        total = ws.cell(line, TOTAL_COLUMN, f"={'+'.join(section_totals)}" if section_totals else "")
        money(total, bold=True)
        for column in range(1, len(HEADERS) + 1):
            ws.cell(line, column).border = TOP_RULE
        project_totals.append(f"{TOTAL_LETTER}{line}")
        line += 2

    cell = ws.cell(line, 3, "Grand total")
    cell.font, cell.alignment = GRAND_FONT, Alignment(horizontal="right")
    grand = ws.cell(line, TOTAL_COLUMN, f"={'+'.join(project_totals)}" if project_totals else "")
    money(grand, bold=True)
    grand.font = GRAND_FONT
    for column in range(1, len(HEADERS) + 1):
        ws.cell(line, column).border = TOP_RULE
    return ws


def subtotal(ws, line, start, end, section):
    cell = ws.cell(line, 3, f"{section} subtotal")
    cell.font, cell.alignment = TOTAL_FONT, Alignment(horizontal="right")
    total = ws.cell(line, TOTAL_COLUMN,
                    f"=SUM({TOTAL_LETTER}{start}:{TOTAL_LETTER}{end})" if end >= start else "")
    total.number_format, total.font = MONEY, TOTAL_FONT
    total.alignment = Alignment(horizontal="right")
    return f"{TOTAL_LETTER}{line}"
