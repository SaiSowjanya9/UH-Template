# UH Homes – Selections Tracker & Spec Sheet Generator

One Excel workbook holds every project's exterior and interior selections. The local web app manages projects and their line items, and downloads each project's **spec sheet** — every column header and every line item exactly as you see them on screen — as a PDF, a Word document or a CSV.

## Start the web app (Windows)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

Open **http://127.0.0.1:5000**. By default the app runs only on this computer, with no user accounts. See **Hosting it for your team** below to put it online with Supabase storage and sign-in.

If another service occupies port 5000, run `.venv\Scripts\python.exe app.py --port 5001` and open **http://127.0.0.1:5001** instead. The app remains bound to loopback on either port.

1. Add a project, then add its selections (manufacturer, model, finish, room, and quantity).
2. Open **Spec sheet** to fill in the line items: brand, model, finish, the product link, quantity, unit price and markup. Paste each product page address into the **PRODUCT LINK** column; the cell then shows the words "Product Link" and the pencil reopens the address. Edits save themselves, and **Save** retries anything a dropped connection held back.
3. Enter the client details at the top of the Spec sheet — client, plan / lot, address and date. They are saved to the project and print in the band at the top of every download.
4. Group the sheet **by location**, **by room** or **by category**, then use **Import / Export** to download the spec sheet as a **PDF**, a **Word** document or a **CSV**. Each one carries the same columns, the same group headings and every line item shown on screen.
5. Bring a priced-up **CSV** back in from the same dialog: matching line items are updated, new ones are added, and nothing is removed.

Close Excel before saving through the app. Every workbook write creates a backup in `backups/` and checks for conflicting changes. Edits you save directly in Excel appear in the app, and changing a product's item name, manufacturer, model or finish clears that row's old link and verification, because they no longer describe the product.

Product links are typed or pasted by you — there is no automated search. **Lookup Status** is your own record of whether a link has been checked: set it to **Verified** once you have opened the page and confirmed the exact manufacturer, model and finish. Verified links older than six months are flagged **Re-check**.

The supplied projects and contact details are examples. Replace the branding/contact placeholders in `lookbook_config.json` and restart the app before preparing client deliverables.

## Project and selection custom fields

The Selection tracker shows the selected project's complete standard details. Use **Project details → Add field** to enter an additional field name and value, then save. Use **Edit details** to rename, update, or remove existing custom fields. Fields belong only to that project, not to every project.

For an individual selection, choose **Edit → Additional fields → Add field**. Saved fields can be expanded under that item in the selections schedule and are included in selection searches. Custom-field-only changes never reset a row's verification.

Each record supports up to 30 custom fields, with unique names of up to 80 characters and text values of up to 2,000 characters (subject to the combined workbook-cell limit).

Custom data is preserved in an optional `Custom Fields` JSON column on each relevant sheet. Keep this column with the rest of the row when sorting or importing. Use the web forms to edit custom fields rather than manually editing their JSON. Existing workbooks without this column remain compatible.

## Hosting it for your team

The app runs in one of two modes, decided entirely by environment variables. With none set it
behaves exactly as described above: one workbook file on your computer, no accounts, loopback
only. Set the Supabase variables and it keeps the workbook in Supabase Storage instead, and
requires every visitor to sign in.

Supabase supplies the database, file storage and accounts; it cannot run the Python process, so
the app itself needs a host such as Render (`render.yaml` is included).

**One-time setup**

1. Apply the database migration. Push `supabase/migrations/` to the GitHub branch wired to your
   Supabase project, or run `supabase db push`. This creates the four tables and the private
   `workbooks` bucket.
2. Copy `.env.example` to `.env` and fill in `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` and
   `SUPABASE_ANON_KEY` from **Project settings → API**. The service key bypasses row level
   security: keep it on the server, never in a browser or a commit.
3. Upload your current workbook once: `.venv\Scripts\python.exe supabase_store.py push`.
   `supabase_store.py pull` brings a copy back down.
4. Create the team's accounts:
   `.venv\Scripts\python.exe auth.py alice@uh.example bob@uh.example`.
   Each account is created already confirmed and prints a generated password once — share it
   privately and have the holder change it. (The dashboard's **Authentication → Users → Add
   user** with *Auto Confirm User* does the same thing by hand.) Leave public sign-ups disabled
   in **Authentication → Sign In / Providers**: there is no self-service sign-up or password
   reset by design, so no email delivery is needed.
5. Confirm everything is in place: `.venv\Scripts\python.exe supabase_store.py check`. It
   verifies the four tables, that the bucket exists and is private, that the workbook is seeded,
   and which environment variables are still missing — without printing any secret.
6. Deploy. Either run `.venv\Scripts\python.exe deploy_render.py` after adding `RENDER_API_KEY`
   to `.env`, which creates the service, copies the hosted settings across and sets
   `UH_TRUSTED_HOSTS` to the hostname Render assigns; or use `render.yaml` from the dashboard
   (**New → Blueprint**) and set that hostname and the `sync: false` secrets by hand.
   `deploy_render.py` is safe to re-run: it reuses an existing service and just pushes the
   current `.env` values. Use `--plan starter` to avoid the free plan's sleeping, and
   `--region` to match your Supabase project.

**It must run as exactly one process.** The workbook is saved whole, so a second worker would
race the first's saves. `wsgi.py` is the entry point and `waitress-serve --threads=8` is the
start command; scale with threads, never with instances.

**Running it free.** Both Render's free web service and Supabase's free plan work, with two
behaviours to know about. Render sleeps the service after 15 minutes idle and takes about a
minute to wake; nothing is lost, because the workbook lives in Supabase. Supabase pauses a
free project after a week with no database activity — it is restorable from the dashboard, but
if the team will go quiet for longer than that, open the app (or the Supabase dashboard) once a
week. Upgrading the Render service to Starter removes the sleeping; upgrading Supabase to Pro
removes the pausing. Storage is a non-issue either way: 31 kept versions of this workbook are
about 2 MB against a 1 GB allowance.

**Sign-in is not optional once the app is reachable.** If `UH_HOST` is anything other than
`127.0.0.1`, the app refuses to start unless sign-in, `UH_SECRET_KEY` and `UH_TRUSTED_HOSTS` are
all configured — a misconfigured deployment fails loudly instead of publishing client selections
and pricing.

**How the hosted workbook is stored.** Each save uploads a new immutable `.xlsx` object and then
advances a single pointer row in Postgres by compare-and-swap, so two processes can never both
win a save, and earlier objects are the saved copies the app offers to restore. That means no
disk is attached to the host, and a redeploy cannot lose data. The last 30 versions are kept.

Two limitations worth knowing. Excel cannot open the hosted workbook directly — use
`supabase_store.py pull` and `push`, which also means direct Excel editing only applies to the
local mode. And because the whole workbook is written on
every save, two people editing the same project at the same time will see
*"Someone else saved first"*; the app is built for taking turns, and moving selections into
Postgres rows is what would lift that.

## Optional command-line workflow

The generators also run as scripts, against the workbook directly. The client finish-schedule,
fillable and PowerPoint builders are command-line only — the app itself downloads the spec sheet:

| Step | What you do | Command |
|---|---|---|
| 1 | Add the project on **Projects**, then its items on **Selections** | — |
| 2 | Paste each product page address into **Product URL** | — |
| 3 | Review each link, then set correct rows to **Verified** | — |
| 4 | Generate the internal spec sheet (PDF, or `build_word.py --spec` for Word) | `python build_lookbook.py --project UH-101 --spec` |
| 5 | Generate the client finish schedule | `python build_lookbook.py --project UH-101` |
| 6 | Or the editable Word version | `python build_word.py --project UH-101` |
| 7 | Or the editable PowerPoint version | `python build_powerpoint.py --project UH-101` |

## Setup (once)

```bash
pip install -r requirements.txt
```

Close the workbook in Excel before running any script that writes to it.

## The workbook

- **Projects** – one row per project. Total / Links Found / Verified / Needs Review are formulas.
  *Cover Image* (optional) = a rendering or photo for the cover (web link or local path).
- **Selections** – one row per item. Yellow columns are typed; grey columns hold the product-link details.
  *Include in Lookbook = No* hides a row from the schedule without deleting it.
  The client description of each row is built from *Manufacturer*, *Product Name* or *Model #*,
  *Finish / Color*, *Qty* (when it is not 1) and *Client Notes*; *Room / Area* prints under the item name.
  *Product Link* is generated on every save: it reads “Product Link” and opens whatever is in
  *Product URL*, so edit the URL column (or the app's PRODUCT LINK column) and never this one.
- **Manufacturers** – brand name → official website, kept together for reference (a starter
  list is included – double-check the domains).
- **Lists** – dropdown values. **Section** is the trade category (Kitchen, Flooring…) and **Location** is where in the home it goes (1st Floor…). The order of the **Locations** column is the order the group headings appear on the schedule.

The example rows are placeholders – replace them with real projects.

## Lookup Status

**Lookup Status** records how far a product link has been checked. You set it yourself; nothing
in the app changes it behind your back, and editing a row's item name, manufacturer, model or
finish clears it back to *Not run* because the old link no longer describes the product.

| Status | Meaning |
|---|---|
| Not run | No link yet, or the product changed and needs a fresh one |
| Found - verify | A link is saved but has not been confirmed against the page |
| Multiple matches | Several candidate pages – note the alternatives in Lookup Notes |
| Found - retailer | Only a retailer page, not the manufacturer's own |
| Not found | No product page could be found |
| Search error | Left over from an earlier automated lookup; treat it as *Not run* |
| Verified | You opened the page and confirmed manufacturer, model and finish |

Verified links older than six months are flagged **Re-check** in the app.

Product images are review aids for your team and are never printed, so a missing image never
blocks a deliverable. The product page itself is printed, as a clickable “Product Link”.

## The internal spec sheet

The app's **Import / Export** dialog downloads the selected project's spec sheet. It is a
landscape page carrying the client details band, the group headings the view is grouped by, and
one row per line item under the same eight column headers as the screen: ITEM, MANUFACTURER,
MODEL #, FINISH / COLOR, PRODUCT LINK, QTY, UNIT PRICE and MARKUP %. Pricing is internal, so
treat the spec sheet as an internal document.

- **Spec sheet · PDF** – `build_lookbook.build_spec`, for reading and printing.
- **Spec sheet · Word** – `build_word.build_spec`, the same page as editable paragraphs and tables.
- **Spec sheet · CSV** – the line items for Excel or Sheets, with *Product Link* as a
  `=HYPERLINK(...)` formula. Price it up, save it as UTF-8 CSV and bring it back in: rows are
  matched on location, room and item, matches are updated, new lines are added and nothing is
  removed. A link you type into the spreadsheet imports as *Found - verify*, never as verified.

From the command line, `--spec` writes the same sheet (`build_lookbook.py --spec`,
`build_word.py --spec`) and `--hide "Qty,Unit Price"` leaves columns out.

## The client finish schedule (command line only)

`python build_lookbook.py --project UH-101` → `output/UH-101_<Name>_<Schedule title>.pdf`

The client-facing schedule is no longer generated from the app; it remains available as a
script. One letter-portrait page per ~20 selections, in the approved layout:

- Wordmark, gold `FINISH SCHEDULE` eyebrow and the schedule title over a ruled header.
- A client / project-lot / address / date band on the first page.
- Letter-spaced, ruled location headings in the **Lists** sheet's Locations order.
- One row per selection: item code (`EX-01`), item name with its room beneath, and the
  description assembled from the workbook columns, ending in a clickable “Product Link”
  when the row has a product URL. No product images and no pricing.
- **Additional details** rows for project and selection custom fields.
- The client note and the signature block, then a footer with your contact line and page number.

Options: `--project UH-101` or `--all`; `--verified-only` leaves out unverified rows;
`--draft` adds the DRAFT mark and a status column for internal review;
`--title "Interior Selections"` and `--prefix IN` override the schedule title and item codes.
The script warns you if any included row isn't Verified yet. `build_powerpoint.py` takes the
same options and writes the editable `.pptx` version of the same page, and `build_word.py`
writes the `.docx` version (add `--fillable` for the working copy, `--spec` for the internal
spec sheet, `--hide "code,Qty"` to leave columns out).

### Branding – `lookbook_config.json`

Company name, the `tagline` used as the eyebrow, contact line, logos, colors, fonts, and the
`schedule` defaults (`title`, `code_prefix`, `note`, `signatures`). `logo_path` is the
white/gold wordmark for dark backgrounds (app sidebar); `logo_print_path` is the dark wordmark
used on the printed schedule. Lato is included under `brand_assets/fonts/` (SIL OFL, license
alongside the files); to use different brand fonts, drop the `.ttf` files in and set
`fonts.heading`, `fonts.body`, `fonts.body_bold`.

## Files

```
UH_Homes_Selections_Tracker.xlsx   the workbook
build_lookbook.py                  finish schedule PDF and spec sheet generators
build_word.py                      editable Word version, fillable copy and spec sheet
build_powerpoint.py                editable PowerPoint version
lookbook_config.json               brand settings
common.py                          shared helpers
build_tracker.py                   recreates a blank workbook (only if needed)
workbook_store.py                  validation, backups and the local file backend
supabase_store.py                  the hosted backend, plus push/pull for the workbook
auth.py                            team sign-in against Supabase Auth
wsgi.py                            hosted entry point (single process)
render.yaml                        Render deployment blueprint
supabase/migrations/               database schema for the hosted mode
```
