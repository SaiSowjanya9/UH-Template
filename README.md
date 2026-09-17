# UH Homes – Selections Tracker & Lookbook Generator

One Excel workbook holds every project's exterior and interior selections. The local web app manages projects, finds candidate product links, and generates separate PDF and editable PowerPoint presentations for each project.

## Start the web app (Windows)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

Open **http://127.0.0.1:5000**. The app runs only on this computer; it does not include shared hosting or user accounts.

1. Add a project, then add its selections (manufacturer, model, finish, room, and quantity).
2. Save a new selection or edit its item/product name, manufacturer, model, or finish. **Automatic lookup** queues that selection and writes its candidate URL back to Excel. Live lookup needs a search-provider API key; see **Setup & workbook**. The manual **Find missing product links** button is still available for unchanged existing rows.
3. Use **Review matches** to open candidate pages and verify the exact manufacturer, model, and finish.
4. Open **Client presentations** and download a PDF or editable PowerPoint. Draft exports show review labels. Final exports require verified selections; the verified-only option explicitly omits pending items.
5. **Export Excel** downloads the entire master workbook. **Import Excel** validates and replaces it only after confirmation, with a timestamped backup in `backups/`.

Close Excel before saving through the app. Every workbook write creates a backup and checks for conflicting changes. While `app.py` is running, a background watcher detects edits saved directly in Excel, invalidates the changed product's old links and verification, and queues a lookup. Close Excel to let the watcher write the results back. The browser refreshes lookup progress automatically without overwriting an open edit form.

Automatic lookup applies to item/product name, manufacturer, model number, and finish changes, not quantity or client notes. It uses manufacturer + model, with the product/item name as supporting context. A newly typed product name is retained. Existing unchanged rows are not searched on the first launch; subsequent changes and pending work survive restarts through `lookup_state/`. Keep that folder with this workspace. Row insertions and reordering do not recheck unchanged selections.

Changed selections cannot contribute old links to app-generated presentations: drafts and full final exports wait for pending lookups; verified-only exports omit pending rows. Other projects remain available. Missing matches leave the URL blank for manual review. Missing API keys leave the queue waiting; provider failures retry up to three attempts, then pause until **Retry automatic lookup** is clicked. After saving product changes, you may save a new manual URL to replace a pending lookup, then verify it.

Provider requests can incur charges after saved product edits. Keep the app running for Excel monitoring, and generate a new PDF/PowerPoint after the lookup completes; previously downloaded files are not rewritten automatically.

The supplied projects and contact details are examples. Replace the branding/contact placeholders in `lookbook_config.json` and restart the app before preparing client deliverables. PowerPoint uses editable text boxes, tables, and separately embedded images; its widescreen layout is distinct from the landscape-letter PDF.

## Optional command-line workflow

The original scripts are also available. Use the web app for the automatic workflow and its pending-lookup export safeguards; these standalone scripts do not run the watcher. Do not run the standalone URL writer concurrently with the app's automatic lookup:

| Step | What you do | Command |
|---|---|---|
| 1 | Add the project on **Projects**, then its items on **Selections** (Manufacturer + Model # are enough) | — |
| 2 | Find product links, names and images automatically | `python find_urls.py` |
| 3 | Review the **Lookup Status** column; set correct rows to **Verified** | — |
| 4 | Generate the client PDF | `python build_lookbook.py --project UH-101` |

## Setup (once)

```bash
pip install -r requirements.txt
cp .env.example .env        # then add one search API key
```

Close the workbook in Excel before running `find_urls.py` (Excel locks the file).

## The workbook

- **Projects** – one row per project. Total / Links Found / Verified / Needs Review are formulas.
  *Cover Image* (optional) = a rendering or photo for the cover (web link or local path).
- **Selections** – one row per item. Yellow columns are typed; grey columns are filled by the lookup.
  *Include in Lookbook = No* hides a row from the PDF without deleting it.
- **Manufacturers** – brand name → official website. The lookup searches that site first, so
  keep this list complete (a starter list is included – double-check the domains).
- **Lists** – dropdown values. The order of **Sections** is the order they appear in the lookbook.

The example rows are placeholders – replace them with real projects.

## How the URL lookup decides

1. Searches `site:<brand domain> "<model>"`, then a broader query if no manufacturer page can be confirmed.
2. Checks product-specific data for an exact model/SKU, or a model in the page title/heading accompanied by product markup. Formatting differences are tolerated, but longer model variants and pages listing several structured products are rejected.
3. Prefers the manufacturer's own page over retailers, and pulls the page's product title and image. Pages without sufficient evidence, including some JavaScript-heavy or blocked sites, need manual lookup. This is candidate discovery, not a guarantee of an exact finish or product match.
4. Writes a status:

| Status | Meaning |
|---|---|
| Found - verify | One confirmed page on the manufacturer's site – quick check, then mark Verified |
| Multiple matches | Several manufacturer pages matched (often finish variants) – alternatives are in Lookup Notes |
| Found - retailer | Only a retailer page confirmed it – confirm, or add/fix the brand domain |
| Not found | Nothing confirmed – Lookup Notes has a ready-made search link |
| Search error | Provider unavailable, missing credentials, or quota/network issue; fix setup and retry |
| Verified | Set by you. **Never overwritten** by the script |

Options: `--project UH-101`, `--force` (re-check rows that already have a URL), `--dry-run`.

To override an image, put a local file path in *Image URL* (e.g. `images/front-door.jpg`).

## The lookbook

`python build_lookbook.py --project UH-101` → `output/UH-101_<Name>_Lookbook.pdf`

- Cover → overview with clickable contents → product cards grouped by section →
  full selections schedule → closing page. Every card and schedule row links to the product page.
- `--all` builds every project; `--verified-only` leaves out unverified rows;
  `--draft` adds a DRAFT watermark and status tags for internal review.
- The script warns you if any included row isn't Verified yet.

### Branding – `lookbook_config.json`

Company name, tagline, contact line, logo path, colors, cards per row/page, and the
disclaimer. For brand fonts, drop the `.ttf` files in the folder and set
`fonts.heading`, `fonts.body`, `fonts.body_bold`.

## Files

```
UH_Homes_Selections_Tracker.xlsx   the workbook
find_urls.py                       product link lookup
build_lookbook.py                  PDF generator
lookbook_config.json               brand settings
common.py                          shared helpers
build_tracker.py                   recreates a blank workbook (only if needed)
```
