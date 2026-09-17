# UH Homes Selections

- This is a local-only Flask app backed by one master `.xlsx` workbook. Bind to `127.0.0.1`; shared hosting and authentication are not implemented.
- On Windows, run `.venv/Scripts/python.exe app.py`. Install runtime dependencies with `.venv/Scripts/python.exe -m pip install -r requirements.txt`.
- Core verification: `.venv/Scripts/python.exe -m unittest test_workflow test_auto_lookup -v`, `node --check ui.js`, and `.venv/Scripts/python.exe -m pip check`.
- Browser verification: install `requirements-dev.txt`, then run `.venv/Scripts/python.exe -m unittest test_browser -v`. The browser test uses installed Microsoft Edge through Playwright, runs against a temporary workbook, and saves screenshots under `output/`.
- Do not run `build_tracker.py` against the working master workbook; it overwrites it with example data. Tests must use temporary copies of the workbook.
- Web edits go through `WorkbookStore`, which validates headers, detects stale revisions, backs up the existing workbook, and atomically replaces it. Close Excel before saving through the app.
- Never overwrite unchanged Verified products through automated lookup. Item/product name, manufacturer, model, or finish changes invalidate prior metadata and verification, including changes saved directly in Excel.
- `AutoLookup` runs from `app.py`, persists hashed input history and pending results under `lookup_state/`, and shares the workbook lock with API edits. Detect row relocations without rechecking unchanged products; acknowledge auto-filled product names so they do not trigger loops. Network calls must run outside the workbook lock, and their results must be revalidated against current product identity before saving.
- App exports must observe changes and block pending rows (or omit them in verified-only mode), even before the worker has cleared old URLs on disk. Test watcher behavior with temporary workbooks and mocked search providers.
- Live search uses a locally configured API key in `.env`; tests mock provider calls. Do not log provider exception URLs or expose keys in API responses.
- PDF and PowerPoint generators share branding from `lookbook_config.json`. Both accept an explicit output directory for isolated tests and downloads.
- Supplied logos live in `brand_assets/`. Use `branding.load_logo` to retain PNG transparency, not the product-image loader that flattens to white. Use the white/gold wordmark on dark backgrounds, the gold symbol for compact branding, and always preserve aspect ratio.
