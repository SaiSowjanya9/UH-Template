# UH Homes – Selections Tracker & Finish Schedule Generator

One Excel workbook holds every project's exterior and interior selections. The local web app manages projects, finds candidate product links for your own review, and generates the approved UH Homes **finish schedule** for each project as a PDF and as an editable PowerPoint.

## Start the web app (Windows)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

Open **http://127.0.0.1:5000**. By default the app runs only on this computer, with no user accounts. See **Hosting it for your team** below to put it online with Supabase storage and sign-in.

If another service occupies port 5000, run `.venv\Scripts\python.exe app.py --port 5001` and open **http://127.0.0.1:5001** instead. The app remains bound to loopback on either port.

1. Add a project, then add its selections (manufacturer, model, finish, room, and quantity).
2. Save a new selection or edit its item/product name, manufacturer, model, or finish. **Automatic lookup** queues that selection and writes its candidate URL back to Excel. Live lookup needs a search-provider API key; see **Setup & workbook**. The manual **Find missing product links** button is still available for unchanged existing rows.
3. Use **Review matches** to open candidate pages and verify the exact manufacturer, model, and finish.
4. Open **Client presentations**, check the live preview, set the **Schedule title** (for example `Exterior Selections`) and **Item code prefix** (`EX` → EX-01, EX-02 …), then download the PDF or the editable PowerPoint. Draft exports add a DRAFT mark and a status column. Final exports require verified selections; the verified-only option explicitly omits pending items.
5. **Export Excel** downloads the entire master workbook. **Import Excel** validates and replaces it only after confirmation, with a timestamped backup in `backups/`.

Close Excel before saving through the app. Every workbook write creates a backup and checks for conflicting changes. While `app.py` is running, a background watcher detects edits saved directly in Excel, invalidates the changed product's old links and verification, and queues a lookup. Close Excel to let the watcher write the results back. The browser refreshes lookup progress automatically without overwriting an open edit form.

Automatic lookup applies to item/product name, manufacturer, model number, and finish changes, not quantity or client notes. It uses manufacturer + model, with the product/item name as supporting context. A newly typed product name is retained. Existing unchanged rows are not searched on the first launch; subsequent changes and pending work survive restarts through `lookup_state/`. Keep that folder with this workspace. Row insertions and reordering do not recheck unchanged selections.

Changed selections cannot contribute stale product details to app-generated schedules: drafts and full final exports wait for pending lookups; verified-only exports omit pending rows. Other projects remain available. Missing matches leave the URL blank for manual review. Missing API keys leave the queue waiting; provider failures retry up to three attempts, then pause until **Retry automatic lookup** is clicked. After saving product changes, you may save a new manual URL to replace a pending lookup, then verify it.

Provider requests can incur charges after saved product edits. Keep the app running for Excel monitoring, and generate a new PDF/PowerPoint after the lookup completes; previously downloaded files are not rewritten automatically.

The supplied projects and contact details are examples. Replace the branding/contact placeholders in `lookbook_config.json` and restart the app before preparing client deliverables. Both files use the same letter-portrait schedule layout; PowerPoint keeps every value in an editable text box so you can adjust wording without rebuilding from the workbook.

## Project and selection custom fields

The Selection tracker shows the selected project's complete standard details. Use **Project details → Add field** to enter an additional field name and value, then save. Use **Edit details** to rename, update, or remove existing custom fields. Fields belong only to that project, not to every project.

For an individual selection, choose **Edit → Additional fields → Add field**. Saved fields can be expanded under that item in the selections schedule and are included in selection searches. Custom-field-only changes do not start a product search or reset verification.

Each record supports up to 30 custom fields, with unique names of up to 80 characters and text values of up to 2,000 characters (subject to the combined workbook-cell limit). Custom fields appear on the schedule's **Additional details** rows, after the categories and before the sign-off block; do not put internal-only information there if you do not intend to share it.

Excel import/export is now under **Data tools**. Custom data is preserved in an optional `Custom Fields` JSON column on each relevant sheet. Keep this column with the rest of the row when sorting or importing. Use the web forms to edit custom fields rather than manually editing their JSON. Existing workbooks without this column remain compatible.

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
6. Deploy with `render.yaml`, setting `UH_TRUSTED_HOSTS` to the service's real hostname and the
   secrets marked `sync: false` in the dashboard.

**It must run as exactly one process.** Automatic lookup is a background thread and the workbook
is saved whole, so a second worker would run a second lookup thread and race the first's saves.
`wsgi.py` is the entry point and `waitress-serve --threads=8` is the start command; scale with
threads, never with instances.

**Running it free.** Both Render's free web service and Supabase's free plan work, with two
behaviours to know about. Render sleeps the service after 15 minutes idle and takes about a
minute to wake; nothing is lost, because the workbook *and* the pending-lookup queue live in
Supabase, but automatic lookup only progresses while someone has the app open. Supabase pauses a
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
**Export Excel** / **Import Excel**, or `supabase_store.py pull`, which also means the
Excel-edit watcher only applies to the local mode. And because the whole workbook is written on
every save, two people editing the same project at the same time will see
*"Someone else saved first"*; the app is built for taking turns, and moving selections into
Postgres rows is what would lift that.

## Optional command-line workflow

The original scripts are also available. Use the web app for the automatic workflow and its pending-lookup export safeguards; these standalone scripts do not run the watcher. Do not run the standalone URL writer concurrently with the app's automatic lookup:

| Step | What you do | Command |
|---|---|---|
| 1 | Add the project on **Projects**, then its items on **Selections** (Manufacturer + Model # are enough) | — |
| 2 | Find product links, names and images automatically | `python find_urls.py` |
| 3 | Review the **Lookup Status** column; set correct rows to **Verified** | — |
| 4 | Generate the client finish schedule | `python build_lookbook.py --project UH-101` |
| 5 | Or the editable PowerPoint version | `python build_powerpoint.py --project UH-101` |

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
  *Include in Lookbook = No* hides a row from the schedule without deleting it.
  The client description of each row is built from *Manufacturer*, *Product Name* or *Model #*,
  *Finish / Color*, *Qty* (when it is not 1) and *Client Notes*; *Room / Area* prints under the item name.
- **Manufacturers** – brand name → official website. The lookup searches that site first, so
  keep this list complete (a starter list is included – double-check the domains).
- **Lists** – dropdown values. The order of **Sections** is the order the headings appear on the schedule.

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

Product links and images are review aids for your team. They are not printed on the client
schedule, so a missing image never blocks a deliverable.

## The client finish schedule

`python build_lookbook.py --project UH-101` → `output/UH-101_<Name>_<Schedule title>.pdf`

One letter-portrait page per ~20 selections, in the approved layout:

- Wordmark, gold `FINISH SCHEDULE` eyebrow and the schedule title over a ruled header.
- A client / project-lot / address / date band on the first page.
- Letter-spaced, ruled category headings in the **Lists** sheet order.
- One row per selection: item code (`EX-01`), item name with its room beneath, and the
  description assembled from the workbook columns. No product images or links.
- **Additional details** rows for project and selection custom fields.
- The client note and the signature block, then a footer with your contact line and page number.

Options: `--project UH-101` or `--all`; `--verified-only` leaves out unverified rows;
`--draft` adds the DRAFT mark and a lookup-status column for internal review;
`--title "Interior Selections"` and `--prefix IN` override the schedule title and item codes.
The script warns you if any included row isn't Verified yet. `build_powerpoint.py` takes the
same options and writes the editable `.pptx` version of the same page.

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
find_urls.py                       product link lookup
build_lookbook.py                  finish schedule PDF generator
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
