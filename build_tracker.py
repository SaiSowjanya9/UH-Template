"""Creates the UH Homes Selections Tracker workspace (run once)."""
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.comments import Comment

F = "Arial"
HDR_FILL = PatternFill("solid", start_color="2F3437")
INPUT_FILL = PatternFill("solid", start_color="FFF6D5")   # yellow-ish = you type here
AUTO_FILL = PatternFill("solid", start_color="EEF1F3")    # grey = script fills
thin = Side(style="thin", color="D0D4D8")
BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)

SECTIONS = ["Exterior", "Roofing", "Windows & Doors", "Kitchen", "Appliances",
            "Bathrooms", "Flooring", "Lighting", "Paint", "Interior Doors & Trim",
            "Hardware", "Other"]
LOCATIONS = ["1st Floor", "2nd Floor", "Outdoor Living", "Garage", "Bulk Items"]

wb = Workbook()

# ---------- Instructions ----------
ins = wb.active
ins.title = "Instructions"
lines = [
    ("UH Homes - Selections Tracker", True, 16),
    ("", False, 10),
    ("How this workspace works", True, 12),
    ("1. Add each project once on the Projects sheet (one row per project, unique Project ID).", False, 10),
    ("2. Add every selection on the Selections sheet. Yellow columns are yours to fill; grey columns hold the product-link details.", False, 10),
    ("3. Paste each product page address into Product URL, with Product Name and Image URL as needed.", False, 10),
    ("4. Run:  python build_lookbook.py --project UH-101   (or --all) - creates the client finish schedule PDF in /output.", False, 10),
    ("", False, 10),
    ("Legend", True, 12),
    ("Yellow cell = input you type", False, 10),
    ("Grey cell = the product-link details, which you may edit by hand", False, 10),
    ("", False, 10),
    ("Rules", True, 12),
    ("- Section is the trade category (Kitchen, Flooring...); Location is where in the home it goes (1st Floor...).", False, 10),
    ("- The schedule groups by Location; headings appear in the order on the Lists sheet's Locations column.", False, 10),
    ("- Add each brand's official website on the Manufacturers sheet, so every product link comes from the source.", False, 10),
    ("- Image URL may be a web link or a local file path (e.g. images/front-door.jpg) to override the found image.", False, 10),
    ("- Include in Lookbook = No hides a row from the client PDF without deleting it.", False, 10),
    ("- The rows on Projects/Selections are EXAMPLES - replace them with real data.", False, 10),
]
for i, (t, b, s) in enumerate(lines, start=1):
    c = ins.cell(row=i, column=1, value=t)
    c.font = Font(name=F, bold=b, size=s)
ins["A11"].fill = INPUT_FILL
ins["A12"].fill = AUTO_FILL
ins.column_dimensions["A"].width = 120

def header(ws, cols, widths):
    for i, (h, w) in enumerate(zip(cols, widths), start=1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = Font(name=F, bold=True, color="FFFFFF")
        c.fill = HDR_FILL
        c.alignment = Alignment(vertical="center", wrap_text=True)
        c.border = BORDER
        ws.column_dimensions[c.column_letter].width = w
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"

def style_block(ws, rows, ncols, input_cols, auto_cols, formula_cols=()):
    for r in range(2, rows + 2):
        for col in range(1, ncols + 1):
            c = ws.cell(row=r, column=col)
            c.font = Font(name=F, size=10)
            c.border = BORDER
            c.alignment = Alignment(vertical="top", wrap_text=True)
            if col in input_cols:
                c.fill = INPUT_FILL
            elif col in auto_cols:
                c.fill = AUTO_FILL

# ---------- Projects ----------
pj = wb.create_sheet("Projects")
pcols = ["Project ID", "Project Name", "Client Name", "Address", "Plan / Elevation",
         "Designer", "Presentation Date", "Total Items", "Cover Image"]
header(pj, pcols, [12, 26, 22, 34, 18, 18, 16, 11, 30])
projects = [
    ["UH-101", "The Magnolia Residence", "Example Client A", "123 Example Ln, Frisco, TX", "Plan 2840 / Elev. B", "Designer Name", "2026-10-01"],
    ["UH-102", "Cedar Ridge Custom Build", "Example Client B", "456 Sample Dr, Prosper, TX", "Plan 3310 / Elev. A", "Designer Name", "2026-10-15"],
]
NP = 50
for r, row in enumerate(projects, start=2):
    for c, v in enumerate(row, start=1):
        pj.cell(row=r, column=c, value=v)
for r in range(2, NP + 2):
    pj[f"H{r}"] = f'=IF(A{r}="","",COUNTIF(Selections!$A:$A,A{r}))'
style_block(pj, NP, len(pcols), input_cols=[1, 2, 3, 4, 5, 6, 7, 9], auto_cols=[8])
pj["I1"].comment = Comment("Optional: web link or local path to a rendering/photo for the lookbook cover.", "Tracker")
pj["H1"].comment = Comment("Formula - counts rows on Selections for this Project ID.", "Tracker")

# ---------- Selections ----------
sl = wb.create_sheet("Selections")
scols = ["Project ID", "Section", "Room / Area", "Item", "Manufacturer", "Model #",
         "Finish / Color", "Qty", "Product URL", "Product Name", "Image URL",
         "Client Notes", "Include in Lookbook", "Location"]
header(sl, scols, [11, 18, 16, 22, 18, 18, 18, 6, 40, 30, 30, 26, 11, 16])
examples = [
    ["UH-101", "Exterior", "Front Elevation", "Siding", "James Hardie", "HardiePlank Lap", "Arctic White", "", "", "", "", "", "Yes", ""],
    ["UH-101", "Windows & Doors", "Front Entry", "Entry Door", "Therma-Tru", "S4108", "Black", 1, "", "", "", "", "Yes", "1st Floor"],
    ["UH-101", "Kitchen", "Kitchen", "Kitchen Faucet", "Moen", "7594ESRS", "Spot Resist Stainless", 1, "", "", "", "", "Yes", "1st Floor"],
    ["UH-101", "Bathrooms", "Primary Bath", "Vanity Faucet", "Delta", "559LF-PP", "Chrome", 2, "", "", "", "", "Yes", "2nd Floor"],
    ["UH-101", "Paint", "Whole House", "Wall Paint", "Sherwin-Williams", "SW 7005", "Pure White", "", "", "", "", "", "Yes", ""],
    ["UH-102", "Kitchen", "Kitchen", "Kitchen Sink", "Kohler", "K-5540", "White", 1, "", "", "", "", "Yes", "1st Floor"],
    ["UH-102", "Lighting", "Dining Room", "Chandelier", "Kichler", "", "Olde Bronze", 1, "", "", "", "Model TBD", "Yes", "1st Floor"],
]
NS = 500
for r, row in enumerate(examples, start=2):
    for c, v in enumerate(row, start=1):
        sl.cell(row=r, column=c, value=v)
style_block(sl, NS, len(scols), input_cols=[1, 2, 3, 4, 5, 6, 7, 8, 12, 13, 14], auto_cols=[9, 10, 11])
sl["F1"].comment = Comment("Type the model / SKU exactly as the manufacturer lists it.", "Tracker")
sl.auto_filter.ref = f"A1:N{NS + 1}"

# ---------- Manufacturers ----------
mf = wb.create_sheet("Manufacturers")
mcols = ["Manufacturer", "Official Domain", "Notes"]
header(mf, mcols, [24, 30, 50])
brands = [
    ("James Hardie", "jameshardie.com"), ("Therma-Tru", "thermatru.com"), ("Pella", "pella.com"),
    ("Andersen", "andersenwindows.com"), ("Moen", "moen.com"), ("Delta", "deltafaucet.com"),
    ("Kohler", "kohler.com"), ("Sherwin-Williams", "sherwin-williams.com"), ("Behr", "behr.com"),
    ("Kichler", "kichler.com"), ("Daltile", "daltile.com"), ("Shaw", "shawfloors.com"),
    ("GE Appliances", "geappliances.com"), ("Whirlpool", "whirlpool.com"),
    ("GAF", "gaf.com"), ("Owens Corning", "owenscorning.com"), ("Kwikset", "kwikset.com"),
    ("Schlage", "schlage.com"), ("Emtek", "emtek.com"), ("Trex", "trex.com"),
]
for r, (n, d) in enumerate(brands, start=2):
    mf.cell(row=r, column=1, value=n)
    mf.cell(row=r, column=2, value=d)
mf.cell(row=2, column=3, value="Starter list - double-check each domain and add the brands UH Homes uses.")
style_block(mf, 100, 3, input_cols=[1, 2, 3], auto_cols=[])

# ---------- Lists ----------
ls = wb.create_sheet("Lists")
# Column B is left empty on purpose: it held the retired Lookup Status list, and the
# Yes / No, Client Status and Locations dropdowns name columns C, D and E by letter.
header(ls, ["Sections (lookbook order)", "", "Yes / No", "", "Locations"], [26, 20, 10, 4, 18])
for i, s in enumerate(SECTIONS, start=2):
    ls.cell(row=i, column=1, value=s)
ls["C2"], ls["C3"] = "Yes", "No"
for i, s in enumerate(LOCATIONS, start=2):
    ls.cell(row=i, column=5, value=s)
style_block(ls, 30, 5, input_cols=[1, 5], auto_cols=[])

# ---------- Validations ----------
def dv(ws, formula, rng):
    v = DataValidation(type="list", formula1=formula, allow_blank=True)
    v.error = "Pick a value from the list (edit the Lists sheet to add options)."
    ws.add_data_validation(v)
    v.add(rng)
dv(sl, "=Lists!$A$2:$A$31", f"B2:B{NS + 1}")
dv(sl, "=Lists!$E$2:$E$31", f"Q2:Q{NS + 1}")
dv(sl, "=Lists!$B$2:$B$7", f"L2:L{NS + 1}")
dv(sl, "=Lists!$C$2:$C$3", f"P2:P{NS + 1}")
dv(sl, f"=Projects!$A$2:$A${NP + 1}", f"A2:A{NS + 1}")


wb.save("UH_Homes_Selections_Tracker.xlsx")
print("saved")
