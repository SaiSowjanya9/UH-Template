import csv
import datetime as dt
import io
import json
import os
import re
import secrets
import tempfile
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

import build_lookbook
import common
import find_urls
import spec_template
from auto_lookup import AutoLookup
from workbook_store import (CLIENT_STATUSES, ConflictError, IDENTITY_FIELDS, OPTIONAL_SELECTION_FIELDS,
                            PROJECT_FIELDS, PROJECT_FORMULAS, SELECTION_FIELDS, STATUSES, ValidationError,
                            WorkbookStore, delete_record, ensure_selection_columns, http_url, next_row,
                            put, records, stale_check, validate_values, detailed_records, set_custom_fields)

BASE = common.BASE_DIR
app = Flask(__name__, template_folder=str(BASE), static_folder=None)
app.config.update(MAX_CONTENT_LENGTH=8 * 1024 * 1024, TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]"])
store = WorkbookStore(common.WORKBOOK)
TOKEN = secrets.token_urlsafe(32)
EXPORT_LOCK = threading.Lock()
automation = None


@app.before_request
def guard():
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if not secrets.compare_digest(request.headers.get("X-UH-Token", ""), TOKEN):
            return jsonify(error="This page has expired. Refresh and try again."), 403


@app.after_request
def headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: http: data:; connect-src 'self'; frame-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


@app.errorhandler(Exception)
def error_response(error):
    if isinstance(error, HTTPException):
        return jsonify(error="The request or file is too large." if error.code == 413 else error.description), error.code
    if isinstance(error, ConflictError):
        return jsonify(error=str(error)), 409
    if isinstance(error, ValidationError):
        return jsonify(error=str(error)), 400
    if isinstance(error, PermissionError):
        return jsonify(error="Close the master workbook in Excel, then retry. Your existing workbook has not been replaced."), 409
    if isinstance(error, FileNotFoundError):
        return jsonify(error="A required file is missing. Check the workbook and template configuration."), 404
    app.logger.error("Request failed: %s", type(error).__name__)
    return jsonify(error="The operation could not be completed. Your source workbook is preserved; check the terminal and retry."), 500


@app.get("/")
def index():
    return render_template("ui.html", token=TOKEN)


@app.get("/favicon.ico")
def favicon():
    return send_file(BASE / "brand_assets" / "uh-icon.png", mimetype="image/png")


@app.get("/brand/<name>.png")
def brand_asset(name):
    sources = {"wordmark": build_lookbook.CFG.get("logo_path"),
               "wordmark-print": build_lookbook.CFG.get("logo_print_path"),
               "symbol": build_lookbook.CFG.get("logo_mark_path"),
               "icon": "brand_assets/uh-icon.png"}
    source = sources.get(name)
    if not source:
        return jsonify(error="Brand asset not found."), 404
    path = Path(source)
    return send_file(path if path.is_absolute() else BASE / path)


@app.get("/assets/<name>")
def asset(name):
    if name not in {"ui.js", "ui.css"}:
        return jsonify(error="Asset not found."), 404
    return send_file(BASE / name)


def provider_info():
    provider = os.getenv("SEARCH_PROVIDER", "brave").lower()
    keys = {"brave": ["BRAVE_API_KEY"], "serpapi": ["SERPAPI_KEY", "SERPAPI_API_KEY"],
            "claude": ["ANTHROPIC_API_KEY"]}.get(provider, [])
    return {"name": provider, "ready": any(os.getenv(key) for key in keys),
            "key_name": keys[0] if keys else "SEARCH_PROVIDER"}


@app.get("/api/state")
def state():
    with store.lock:
        wb, revision = store.snapshot()
        rows = detailed_records(wb, "Selections")
        if automation:
            automation.observe(wb)
            rows = automation.display_rows(rows)
        rows = [{**row, "_stale": stale_check(row)} for row in rows]
        return jsonify(projects=detailed_records(wb, "Projects"), selections=rows,
                       manufacturers=records(wb, "Manufacturers"), sections=common.section_order(wb),
                       statuses=STATUSES, client_statuses=CLIENT_STATUSES, stale_days=180,
                       revision=revision, provider=provider_info(),
                       automation=automation.status() if automation else {"enabled": False, "pending": [], "message": ""},
                       workbook=store.path.name, branding=json.loads((BASE / "lookbook_config.json").read_text()))


@app.post("/api/automation/retry")
def retry_automation():
    current(payload())
    if automation:
        automation.retry()
    return jsonify(ok=True)


def payload():
    data = request.get_json()
    if not isinstance(data, dict) or not isinstance(data.get("revision"), str):
        raise ValidationError("A workbook revision is required. Refresh this page.")
    return data


def current(data):
    wb, revision = store.snapshot()
    if data["revision"] != revision:
        raise ConflictError("The workbook changed. Refresh before saving your changes.")
    if automation:
        automation.observe(wb)
    return wb, revision


@app.post("/api/projects")
def save_project():
    data = payload()
    values = validate_values(data.get("values"), PROJECT_FIELDS)
    with store.lock:
        wb, revision = current(data)
        sheet = common.Sheet(wb["Projects"])
        existing = next((p for p in records(wb, "Projects") if p["Project ID"] == values.get("Project ID")), None)
        if data.get("delete"):
            if not existing:
                raise ConflictError("This project no longer exists. Refresh the page.")
            owned = [r["_row"] for r in records(wb, "Selections") if r["Project ID"] == existing["Project ID"]]
            if owned and not data.get("cascade"):
                raise ValidationError(f"This project has {len(owned)} line items. Confirm deleting them with the project.")
            for row in sorted(owned, reverse=True):     # bottom up so the remaining rows keep their numbers
                delete_record(wb, "Selections", row)
            delete_record(wb, "Projects", existing["_row"])
            store.save(wb, revision, f"delete project {existing['Project ID']} (-{len(owned)} line items)")
            if automation and owned:
                automation.observe(wb)
            return jsonify(ok=True, deleted=True, removed=len(owned))
        if data.get("create") and existing:
            raise ValidationError("That Project ID already exists. Choose a unique ID.")
        row = existing["_row"] if existing else next_row(sheet)
        put(sheet, row, values)
        for field, formula in PROJECT_FORMULAS.items():
            if field in sheet.cols:
                sheet.ws.cell(row, sheet.cols[field], formula.format(row=row))
        if "custom_fields" in data:
            set_custom_fields(wb, "Projects", row, data["custom_fields"])
        seeded = 0
        if not existing:
            # A new home starts from the standard room-by-room specification.
            _, template = spec_template.load()
            spec_template.register_sections(wb, [section for section, _ in template])
            seeded, _ = spec_template.apply(wb, values["Project ID"], template)
            from openpyxl.utils import get_column_letter
            selections = wb["Selections"]
            selections.auto_filter.ref = f"A1:{get_column_letter(selections.max_column)}{selections.max_row}"
        store.save(wb, revision, f"project {values.get('Project ID')}" + (f" (+{seeded} line items)" if seeded else ""))
        if automation and seeded:
            automation.observe(wb)
    return jsonify(ok=True, project_id=values.get("Project ID"), seeded=seeded)


@app.post("/api/selections")
def save_selection():
    data = payload()
    values = validate_values(data.get("values"), SELECTION_FIELDS + OPTIONAL_SELECTION_FIELDS)
    with store.lock:
        wb, revision = current(data)
        sheet = common.Sheet(wb["Selections"])
        if data.get("delete"):
            old = next((r for r in records(wb, "Selections") if r["_row"] == data.get("row")), None)
            if not old:
                raise ConflictError("This selection no longer exists. Refresh the page.")
            delete_record(wb, "Selections", old["_row"])
            store.save(wb, revision, f"delete selection row {old['_row']} ({old['Item']})")
            if automation:
                automation.observe(wb)
            return jsonify(ok=True, deleted=True)
        old = None
        if data.get("row") is not None:
            old = next((r for r in records(wb, "Selections") if r["_row"] == data["row"]), None)
            if not old:
                raise ConflictError("This selection no longer exists. Refresh the page.")
        row = old["_row"] if old else next_row(sheet)
        merged = {k: old.get(k, "") if old else "" for k in SELECTION_FIELDS + OPTIONAL_SELECTION_FIELDS}
        merged.update(values)
        if not merged["Project ID"] or not merged["Item"]:
            raise ValidationError("Project and item name are required.")
        if automation and str(row) in automation.pending and values.get("Lookup Status") == "Verified":
            raise ValidationError("Automatic lookup is pending for this changed product. Wait for the result or save a new manual link before verifying.")
        merged["Client Status"] = merged.get("Client Status") or "Proposed"
        if old and any(merged[k] != old[k] for k in IDENTITY_FIELDS):
            if merged["Client Status"] in {"Presented", "Approved"}:
                merged["Client Status"] = "Changed"
            elif merged["Client Status"] == "Rejected":
                merged["Client Status"] = "Proposed"
            new_url = merged["Product URL"] if merged["Product URL"] != old["Product URL"] else ""
            merged.update({k: "" for k in ["Product URL", "Image URL", "Checked On", "Lookup Notes"]})
            if merged["Product Name"] == old["Product Name"]:
                merged["Product Name"] = ""
            merged["Product URL"] = new_url
            merged["Lookup Status"] = "Found - verify" if new_url else "Not run"
        elif old and merged["Product URL"] != old["Product URL"]:
            merged["Lookup Status"] = "Found - verify" if merged["Product URL"] else "Not run"
            merged["Checked On"] = ""
            merged["Lookup Notes"] = "Manually updated link. Review before verifying."
        elif not old and (merged["Lookup Status"] == "Verified" or merged["Product URL"]):
            merged["Lookup Status"] = "Found - verify"
        merged["Lookup Status"] = merged["Lookup Status"] or "Not run"
        merged["Include in Lookbook"] = merged["Include in Lookbook"] or "Yes"
        if merged["Lookup Status"] == "Verified" and (not old or old["Lookup Status"] != "Verified"):
            if not data.get("confirm_verified"):
                raise ValidationError("Confirm that you checked the manufacturer, model, and finish on the product page.")
            merged["Checked On"] = dt.date.today().isoformat()
        sheet = ensure_selection_columns(wb)
        put(sheet, row, merged)
        from openpyxl.utils import get_column_letter
        sheet.ws.auto_filter.ref = f"A1:{get_column_letter(sheet.ws.max_column)}{max(sheet.ws.max_row, row)}"
        if "custom_fields" in data:
            set_custom_fields(wb, "Selections", row, data["custom_fields"])
        store.save(wb, revision, f"selection row {row}")
        if automation:
            automation.observe(wb)
        queued = bool(automation and str(row) in automation.pending)
    return jsonify(ok=True, row=row, queued=queued)


@app.post("/api/manufacturers")
def save_manufacturer():
    data = payload()
    values = validate_values(data.get("values"), ["Manufacturer", "Official Domain", "Notes"])
    name = values.get("Manufacturer", "")
    with store.lock:
        wb, revision = current(data)
        sheet = common.Sheet(wb["Manufacturers"])
        old = next((r for r in records(wb, "Manufacturers") if r["Manufacturer"].lower() == name.lower()), None)
        if data.get("delete"):
            if not old:
                raise ConflictError("This manufacturer no longer exists. Refresh the page.")
            delete_record(wb, "Manufacturers", old["_row"])
            store.save(wb, revision, f"delete manufacturer {name}")
            return jsonify(ok=True, deleted=True)
        domain = values.get("Official Domain", "").lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")
        if not name or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", domain):
            raise ValidationError("Enter a manufacturer name and a domain such as brand.com, without a page path.")
        values["Official Domain"] = domain
        put(sheet, old["_row"] if old else next_row(sheet), values)
        store.save(wb, revision, f"manufacturer {name}")
    return jsonify(ok=True)


@app.post("/api/selections/<int:row>/lookup")
def lookup_selection(row):
    data = payload()
    info = provider_info()
    if not info["ready"]:
        raise ValidationError(f"Product search is not configured. Add {info['key_name']} to your local .env file and restart the app. Do not paste keys into the workbook or chat.")
    wb, revision = current(data)
    record = next((r for r in records(wb, "Selections") if r["_row"] == row), None)
    if not record:
        raise ValidationError("Selection not found.")
    if automation and str(row) in automation.pending:
        raise ConflictError("Automatic lookup is already queued for this product. Check its progress or use Retry automatic lookup.")
    if record["Lookup Status"] == "Verified":
        return jsonify(ok=True, skipped=True)
    if not record["Manufacturer"] or not record["Model #"]:
        raise ValidationError("Add both a manufacturer and model number before searching.")
    domain = common.manufacturer_domains(wb).get(record["Manufacturer"].lower(), "")
    result = find_urls.lookup(find_urls.PROVIDERS[info["name"]], record["Manufacturer"], record["Model #"], domain,
                              product_name=record["Product Name"] or record["Item"])
    if result["status"] == "Search error":
        return jsonify(error=result["notes"]), 502
    if len(result.get("url", "")) > 4000:
        raise ValidationError("The returned product URL is too long. Review this product manually.")
    updates = {"Lookup Status": result["status"], "Lookup Notes": result.get("notes", ""), "Checked On": dt.date.today().isoformat()}
    if result.get("url"):
        updates["Product URL"] = result["url"]
        updates["Product Name"] = result.get("name", "")
        if not record["Image URL"] or record["Image URL"].startswith("http"):
            updates["Image URL"] = result.get("image", "")
    elif record["Product URL"]:
        updates["Lookup Notes"] += " Existing link retained for manual review; it was not confirmed."
    with store.lock:
        put(common.Sheet(wb["Selections"]), row, updates)
        store.save(wb, revision, f"lookup row {row}")
        if automation:
            automation.acknowledge(wb, row)
    return jsonify(ok=True, result=result)


@app.post("/api/selections/parse-link")
def parse_link():
    data = payload()
    current(data)
    url = common.clean(data.get("url"))
    if not http_url(url) or len(url) > 4000:
        raise ValidationError("Enter a full HTTP or HTTPS product link.")
    details = find_urls.describe_page(url)
    if not details or not details.get("Product Name"):
        raise ValidationError("That page could not be read as a product page. Enter the details manually.")
    return jsonify(ok=True, details=details)


# The band's project/lot field combines two columns, so it is read-only on the form.
BAND_TO_PROJECT = {"client": "Client Name", "address": "Address", "date": "Presentation Date"}


def normalise_date(value):
    """Accept an ISO date or the printed form, and store ISO."""
    text = common.clean(value)
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValidationError(f"'{text}' is not a date the schedule understands. Use YYYY-MM-DD.")


@app.post("/api/schedule/form")
def import_schedule_form():
    """Apply a filled fillable schedule: client details, line descriptions and removals."""
    import build_form
    upload = request.files.get("file")
    if not upload or not upload.filename.lower().endswith(".pdf"):
        raise ValidationError("Choose the fillable schedule PDF you downloaded and filled in.")
    project_id = common.clean(request.form.get("project"))
    try:
        details, lines = build_form.read(io.BytesIO(upload.read()))
    except Exception:
        raise ValidationError("That PDF could not be read as a fillable schedule. Download a fresh copy and fill that in.") from None
    if not lines and not details:
        raise ValidationError("That PDF has no schedule fields. Use the fillable copy, not the client PDF.")

    with store.lock:
        wb, revision = current({"revision": request.form.get("revision", "")})
        project = next((p for p in records(wb, "Projects") if p["Project ID"] == project_id), None)
        if not project:
            raise ValidationError("Choose a valid project.")
        sheet = ensure_selection_columns(wb)
        owned = {r["_row"]: r for r in records(wb, "Selections") if r["Project ID"] == project_id}
        foreign = [row for row in lines if row not in owned]
        if foreign:
            raise ValidationError("That form belongs to a different project or an older version of it. Download a fresh copy.")

        updates = {}
        for key, field in BAND_TO_PROJECT.items():
            value, stored = details.get(key, ""), common.clean(project.get(field, ""))
            if field == "Presentation Date":
                value, stored = normalise_date(value), stored[:10]
            if value and value != stored:
                updates[field] = value
        if updates:
            updates = validate_values(updates, PROJECT_FIELDS)
            put(common.Sheet(wb["Projects"]), project["_row"], updates)

        removed = described = 0
        for row, entry in lines.items():
            record = owned[row]
            if entry.get("remove"):
                removed += 1
                continue
            typed = entry.get("description", "")
            # Unedited fields still read back transliterated, so compare like for like.
            generated = build_form.form_text(build_lookbook.description({**record, "Description Override": ""}))
            override = "" if typed in {"", generated} else typed
            if override != record.get("Description Override", ""):
                put(sheet, row, validate_values({"Description Override": override},
                                                SELECTION_FIELDS + OPTIONAL_SELECTION_FIELDS))
                described += 1
        for row in sorted((row for row, entry in lines.items() if entry.get("remove")), reverse=True):
            delete_record(wb, "Selections", row)
        if removed or described or updates:
            store.save(wb, revision, f"schedule form {project_id} (-{removed}/~{described})")
            if automation:
                automation.observe(wb)
    return jsonify(ok=True, removed=removed, described=described, details=sorted(updates))


SPEC_CSV_FIELDS = ["Section", "Room / Area", "Item", "Manufacturer", "Model #", "Finish / Color",
                   "Qty", "Unit Price", "Markup %", "Client Notes", "Client Status", "Include in Lookbook"]
CSV_ROW_LIMIT = 5000


def spec_key(row):
    return (common.clean(row.get("Section")), common.clean(row.get("Room / Area")), common.clean(row.get("Item")))


@app.get("/api/selections/csv")
def export_spec_csv():
    project_id = common.clean(request.args.get("project"))
    with store.lock:
        wb, _ = store.snapshot()
        project = next((p for p in records(wb, "Projects") if p["Project ID"] == project_id), None)
        if not project:
            raise ValidationError("Choose a valid project.")
        order = common.section_order(wb)
        rows = [r for r in records(wb, "Selections") if r["Project ID"] == project_id]
    rows.sort(key=lambda r: (order.index(r["Section"]) if r["Section"] in order else len(order), r["Section"], r["_row"]))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=SPEC_CSV_FIELDS, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    writer.writerows({field: row.get(field, "") for field in SPEC_CSV_FIELDS} for row in rows)
    name = re.sub(r"[^A-Za-z0-9_-]", "_", project_id)[:50]
    return send_file(io.BytesIO(buffer.getvalue().encode("utf-8-sig")), as_attachment=True,
                     download_name=f"{name}_spec_sheet.csv", mimetype="text/csv")


@app.post("/api/selections/csv")
def import_spec_csv():
    project_id = common.clean(request.form.get("project"))
    upload = request.files.get("file")
    if not upload or not upload.filename.lower().endswith(".csv"):
        raise ValidationError("Choose a .csv file exported from this spec sheet.")
    try:
        text = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationError("Save the file as UTF-8 CSV and try again.") from None
    reader = csv.DictReader(io.StringIO(text))
    headers = [common.clean(name) for name in (reader.fieldnames or [])]
    if "Item" not in headers:
        raise ValidationError("The file needs at least an Item column. Export the spec sheet first to see the format.")
    unknown = [name for name in headers if name and name not in SPEC_CSV_FIELDS]
    if unknown:
        raise ValidationError(f"Unexpected column(s): {', '.join(unknown[:4])}. Export the spec sheet to see the format.")
    incoming = []
    for entry in reader:
        values = {field: common.clean(entry.get(field)) for field in SPEC_CSV_FIELDS if field in headers}
        if any(values.values()):
            incoming.append(values)
        if len(incoming) > CSV_ROW_LIMIT:
            raise ValidationError(f"This import is limited to {CSV_ROW_LIMIT} rows.")
    if not incoming:
        raise ValidationError("That file has no line items.")

    with store.lock:
        wb, revision = current({"revision": request.form.get("revision", "")})
        project = next((p for p in records(wb, "Projects") if p["Project ID"] == project_id), None)
        if not project:
            raise ValidationError("Choose a valid project.")
        sheet = ensure_selection_columns(wb)
        existing = {}
        for record in records(wb, "Selections"):
            if record["Project ID"] == project_id:
                existing.setdefault(spec_key(record), []).append(record)
        added = updated = 0
        for values in incoming:
            if not values.get("Item"):
                raise ValidationError("Every imported line needs an item name.")
            values = validate_values(values, SELECTION_FIELDS + OPTIONAL_SELECTION_FIELDS)
            if values.get("Client Status") and values["Client Status"] not in CLIENT_STATUSES:
                raise ValidationError(f"Unknown client status '{values['Client Status']}'.")
            matches = existing.get(spec_key(values))
            old = matches.pop(0) if matches else None
            if old:
                merged = {**{k: old.get(k, "") for k in SELECTION_FIELDS + OPTIONAL_SELECTION_FIELDS}, **values}
                if any(merged[key] != old[key] for key in IDENTITY_FIELDS):
                    # the product itself changed, so its old link and verification no longer apply
                    merged.update({key: "" for key in ["Product URL", "Image URL", "Checked On", "Lookup Notes"]})
                    merged["Lookup Status"] = "Not run"
                    if merged["Client Status"] in {"Presented", "Approved"}:
                        merged["Client Status"] = "Changed"
                put(sheet, old["_row"], merged)
                updated += 1
            else:
                row = next_row(sheet)
                put(sheet, row, {**{k: "" for k in SELECTION_FIELDS + OPTIONAL_SELECTION_FIELDS}, **values,
                                 "Project ID": project_id, "Lookup Status": "Not run",
                                 "Include in Lookbook": values.get("Include in Lookbook") or "Yes",
                                 "Client Status": values.get("Client Status") or "Proposed"})
                added += 1
        from openpyxl.utils import get_column_letter
        sheet.ws.auto_filter.ref = f"A1:{get_column_letter(sheet.ws.max_column)}{sheet.ws.max_row}"
        store.save(wb, revision, f"spec csv import {project_id} (+{added}/~{updated})")
        if automation:
            automation.observe(wb)
    return jsonify(ok=True, added=added, updated=updated)


@app.get("/api/workbook")
def export_workbook():
    with store.lock:
        raw = store.path.read_bytes()
    return send_file(io.BytesIO(raw), as_attachment=True, download_name=store.path.name,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/api/workbook/import")
def import_workbook():
    if request.form.get("confirm") != "replace-with-backup":
        raise ValidationError("Confirm replacement of the master workbook first.")
    upload = request.files.get("file")
    if not upload or not upload.filename.lower().endswith(".xlsx"):
        raise ValidationError("Choose an .xlsx workbook exported from this app or using the same template.")
    with store.lock:
        store.import_bytes(upload.read(), request.form.get("revision", ""))
        if automation:
            automation.observe(store.snapshot()[0])
    return jsonify(ok=True)


BAND_INPUTS = {"client": "Client Name", "project_lot": "Project / Lot", "address": "Address", "date": "Date"}


def band_overrides(data):
    """Client details typed for this download, replacing the project's own for the band."""
    details = data.get("details")
    if details is None:
        return {}
    if not isinstance(details, dict) or set(details) - set(BAND_INPUTS):
        raise ValidationError("Unexpected client detail fields.")
    out = {}
    for key, label in BAND_INPUTS.items():
        value = common.clean(details.get(key))
        if any(ord(character) < 32 for character in value) or len(value) > 200:
            raise ValidationError(f"Enter {label} as a single line of up to 200 characters.")
        if key == "date" and value:
            value = normalise_date(value)
        out[key] = value
    return out


def with_details(project, overrides):
    if not overrides:
        return project
    shown = dict(project)
    if overrides.get("client"):
        shown["Client Name"] = overrides["client"]
    if overrides.get("project_lot"):
        # the band composes name and plan, so a typed value replaces the pair outright
        shown["Project Name"], shown["Plan / Elevation"] = overrides["project_lot"], ""
    if overrides.get("address"):
        shown["Address"] = overrides["address"]
    if overrides.get("date"):
        shown["Presentation Date"] = overrides["date"]
    return shown


def schedule_options(data):
    """Per-export schedule title and item code prefix, falling back to the branding defaults."""
    title, prefix = common.clean(data.get("title")), common.clean(data.get("prefix"))
    if len(title) > 80 or any(ord(character) < 32 for character in title):
        raise ValidationError("Enter a schedule title of up to 80 characters on one line.")
    if prefix and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,5}", prefix):
        raise ValidationError("The item code prefix must be 1-6 letters, numbers, or hyphens, such as EX.")
    return title or None, prefix or None


@app.post("/api/presentation")
def presentation():
    data = payload()
    wb, revision = current(data)
    project = next((p for p in detailed_records(wb, "Projects") if p["Project ID"] == data.get("project")), None)
    if not project:
        raise ValidationError("Choose a valid project.")
    mode, kind = data.get("mode", "draft"), data.get("format", "pdf")
    if mode not in {"draft", "verified", "final"} or kind not in {"pdf", "pptx", "form"}:
        raise ValidationError("Invalid presentation options.")
    title, prefix = schedule_options(data)
    overrides = band_overrides(data)
    rows = [r for r in detailed_records(wb, "Selections") if r["Project ID"] == project["Project ID"] and r["Include in Lookbook"].lower() != "no"]
    if automation:
        with store.lock:
            pending_rows = set(automation.pending)
        if mode == "verified":
            rows = [r for r in rows if str(r["_row"]) not in pending_rows]
        elif any(str(r["_row"]) in pending_rows for r in rows):
            raise ConflictError("This project has changed products awaiting automatic lookup. Close Excel and wait for the results, save manual links, or export verified selections only.")
    if mode == "final" and any(r["Lookup Status"] != "Verified" for r in rows):
        raise ValidationError("Some included selections are not verified. Review them, export a draft, or choose verified selections only.")
    if mode == "verified":
        rows = [r for r in rows if r["Lookup Status"] == "Verified"]
    if not rows:
        raise ValidationError("No selections match these export options.")
    order = common.section_order(wb)
    rows.sort(key=lambda r: (order.index(r["Section"]) if r["Section"] in order else len(order), r["Section"], r["_row"]))
    preview = bool(data.get("preview"))
    note = f"export {project['Project ID']} {kind} {mode}"
    with store.lock:
        marked, saved = [], []
        if not preview:
            if data.get("save_details") and overrides:
                # only the unambiguous columns; the band's project/lot merges two of them
                changed = {}
                for key, field in {"client": "Client Name", "address": "Address", "date": "Presentation Date"}.items():
                    value, stored = overrides.get(key, ""), common.clean(project.get(field, ""))
                    if field == "Presentation Date":
                        stored = stored[:10]
                    if value and value != stored:
                        changed[field] = value
                saved = validate_values(changed, PROJECT_FIELDS)
                if saved:
                    put(common.Sheet(wb["Projects"]), project["_row"], saved)
            if mode != "draft":
                sheet = ensure_selection_columns(wb)
                marked = [r for r in rows if common.clean(r.get("Client Status")) in {"", "Proposed"}]
                for r in marked:
                    put(sheet, r["_row"], {"Client Status": "Presented"})
            if marked or saved:
                store.save(wb, revision, note)
            else:
                store.log(note)
        rev = store.count_notes(f"export {project['Project ID']} ")
    shown = with_details(project, overrides)
    with EXPORT_LOCK, tempfile.TemporaryDirectory(prefix="uh_presentation_") as folder:
        if kind == "form":
            import build_form
            output = build_form.build(project["Project ID"], shown, rows,
                                      output_dir=Path(folder), title=title, prefix=prefix)
            mime = "application/pdf"
        else:
            if kind == "pdf":
                build_schedule, mime = build_lookbook.build, "application/pdf"
            else:
                from build_powerpoint import build as build_schedule
                mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            output = build_schedule(project["Project ID"], shown, rows, mode == "draft",
                                    output_dir=Path(folder), title=title, prefix=prefix,
                                    rev=None if preview else rev)
        raw = output.read_bytes()
    return send_file(io.BytesIO(raw), as_attachment=not preview, download_name=output.name, mimetype=mime)


if __name__ == "__main__":
    import argparse
    import atexit
    parser = argparse.ArgumentParser(description="Run the local UH Homes selection tracker.")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535.")
    automation = AutoLookup(store, provider_info)
    automation.observe(store.snapshot()[0])
    automation.start()
    atexit.register(automation.stop)
    print("Automatic lookup watches saved app and Excel edits while this process is running.")
    print(f"UH Homes Selections: http://127.0.0.1:{args.port}")
    print("Local access only. Close the master workbook in Excel before saving changes.")
    app.run(host="127.0.0.1", port=args.port, debug=False)
