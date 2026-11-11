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

from flask import Flask, Request, jsonify, redirect, render_template, request, send_file, session, url_for
from PIL import Image
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

import auth
import build_lookbook
import common
import spec_template
import supabase_store
from workspace_store import (CLIENT_STATUSES, COVER_PREFIX, ConflictError, IDENTITY_FIELDS, OPTIONAL_SELECTION_FIELDS,
                            PRODUCT_LINK, PROJECT_FIELDS, PROJECT_FORMULAS, SELECTION_FIELDS,
                            ValidationError, WorkspaceStore, delete_record, ensure_selection_columns,
                            hyperlink_formula, next_row, parse_hyperlink, put, records,
                            validate_values, detailed_records, set_custom_fields, unit_order)

BASE = common.BASE_DIR
LOCAL_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
HOST = os.getenv("UH_HOST", "127.0.0.1")
# Anything but a loopback bind is reachable by others, so it must carry sign-in.
LOCAL_ONLY = HOST in {"127.0.0.1", "localhost", "::1"}
AUTH = auth.settings()
REQUIRE_LOGIN = bool(AUTH) and os.getenv("UH_REQUIRE_LOGIN", "1") != "0"


def open_store():
    """The workspace in Supabase Storage when configured, otherwise the local file."""
    config = supabase_store.settings()
    if config:
        return WorkspaceStore(supabase_store.SupabaseBackend(config))
    return WorkspaceStore(common.WORKSPACE)


def trusted_hosts():
    extra = [name.strip() for name in (os.getenv("UH_TRUSTED_HOSTS") or "").split(",") if name.strip()]
    return LOCAL_HOSTS + extra


class AppRequest(Request):
    """/healthz must answer whatever Host the platform's internal probe sends."""
    _trusted_hosts = None

    @property
    def trusted_hosts(self):
        return None if self.path == "/healthz" else self._trusted_hosts

    @trusted_hosts.setter
    def trusted_hosts(self, value):
        self._trusted_hosts = value


app = Flask(__name__, template_folder=str(BASE), static_folder=None)
app.request_class = AppRequest
app.config.update(MAX_CONTENT_LENGTH=8 * 1024 * 1024, TRUSTED_HOSTS=trusted_hosts(),
                  SECRET_KEY=os.getenv("UH_SECRET_KEY") or secrets.token_hex(32),
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=not LOCAL_ONLY,
                  PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=12))
if not LOCAL_ONLY:
    # Behind a hosting proxy, so trust its forwarded scheme and host for secure cookies.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
store = open_store()
TOKEN = secrets.token_urlsafe(32)
EXPORT_LOCK = threading.Lock()
THROTTLE = auth.Throttle()
# Reachable without a session: the sign-in page and the assets and health check it needs.
OPEN_ENDPOINTS = {"login", "logout", "asset", "brand_asset", "favicon", "healthz"}


def csrf_token():
    """Per-session when signed in, per-process when running locally without login."""
    if not REQUIRE_LOGIN:
        return TOKEN
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@app.before_request
def guard():
    if request.routing_exception is not None:
        return None                 # let Flask raise the stored 400/404 (e.g. an untrusted Host)
    if request.endpoint == "login":
        return None                 # the sign-in form carries its own token as a field
    if REQUIRE_LOGIN and not session.get("user") and request.endpoint not in OPEN_ENDPOINTS:
        if request.path.startswith("/api/"):
            return jsonify(error="Your session has ended. Sign in again to continue."), 401
        return redirect(url_for("login", next=request.full_path if request.method == "GET" else None))
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        expected = session.get("csrf") if REQUIRE_LOGIN else TOKEN
        if not expected or not secrets.compare_digest(request.headers.get("X-UH-Token", ""), expected):
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
        return jsonify(error="Close the master workspace in Excel, then retry. Your existing workspace has not been replaced."), 409
    if isinstance(error, FileNotFoundError):
        return jsonify(error="A required file is missing. Check the workspace and template configuration."), 404
    app.logger.error("Request failed: %s", type(error).__name__)
    return jsonify(error="The operation could not be completed. Nothing was changed; check the terminal and retry."), 500


@app.get("/")
def index():
    user = session.get("user") or {}
    return render_template("ui.html", token=csrf_token(), workspace="HOSTED" if store.remote else "LOCAL",
                           user=user.get("email", ""), can_sign_out=REQUIRE_LOGIN)


@app.get("/healthz")
def healthz():
    """A liveness check the host can poll without a session."""
    return jsonify(ok=True)


def safe_next(target):
    """Only ever redirect back into this app."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("index")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not REQUIRE_LOGIN:
        return redirect(url_for("index"))
    target = safe_next(request.values.get("next"))
    if session.get("user"):
        return redirect(target)
    if request.method == "GET":
        return render_template("login.html", token=csrf_token(), next=target, error="")
    expected = session.get("csrf", "")
    if not expected or not secrets.compare_digest(request.form.get("token", ""), expected):
        return render_template("login.html", token=csrf_token(), next=target,
                               error="That sign-in form expired. Try again."), 400
    source = request.remote_addr or "unknown"
    try:
        THROTTLE.check(source)
        user = auth.sign_in(request.form.get("email"), request.form.get("password"), AUTH)
    except auth.AuthError as error:
        THROTTLE.record(source)
        return render_template("login.html", token=csrf_token(), next=target, error=str(error)), 401
    THROTTLE.clear(source)
    session.clear()              # a fresh session id and token, so a pre-login cookie cannot be reused
    session["user"] = user
    session.permanent = True
    csrf_token()
    return redirect(target)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/favicon.ico")
def favicon():
    return send_file(BASE / "brand_assets" / "uh-icon.png", mimetype="image/png")


@app.get("/brand/<name>.png")
@app.get("/brand/<name>.jpg")
def brand_asset(name):
    sources = {"wordmark": build_lookbook.CFG.get("logo_path"),
               "wordmark-print": build_lookbook.CFG.get("logo_print_path"),
               "symbol": build_lookbook.CFG.get("logo_mark_path"),
               "icon": "brand_assets/uh-icon.png",
               "login-background": "brand_assets/login-background.jpg"}
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


# The three columns an exported spec sheet is matched back onto its line items by on import.
# Leaving one out of a download is the sender's own call, so the chooser offers them like any
# other column — with a warning, and never switched off to begin with.
SPEC_MATCH_FIELDS = ("Location", "Room / Area", "Item")
# What the download chooser offers: the spec-sheet family of build_lookbook's one registry, in
# its order. Reading the list from there is what stops the app offering a column the exports
# do not know how to leave out, which `hidden_columns` would then refuse with a 400.
EXPORT_COLUMNS = [{"key": key, "label": label, "matches": key in SPEC_MATCH_FIELDS}
                  for key, label, families in build_lookbook.HIDEABLE_COLUMNS if "sheet" in families]


@app.get("/api/state")
def state():
    with store.lock:
        wb, revision = store.snapshot()
        rows = detailed_records(wb, "Selections")
        return jsonify(projects=detailed_records(wb, "Projects"), selections=rows,
                       manufacturers=records(wb, "Manufacturers"), sections=common.section_order(wb),
                       locations=common.location_order(wb),
                       units=unit_order(wb),
                       client_statuses=CLIENT_STATUSES,
                       export_columns=EXPORT_COLUMNS,
                       revision=revision,
                       workspace=store.name, branding=json.loads((BASE / "lookbook_config.json").read_text()))


def payload():
    data = request.get_json()
    if not isinstance(data, dict) or not isinstance(data.get("revision"), str):
        raise ValidationError("This request is missing its page revision. Refresh this page.")
    return data


def current(data):
    wb, revision = store.snapshot()
    if data["revision"] != revision:
        raise ConflictError("This page is out of date \u2014 refresh it, then make the change again.")
    return wb, revision


def rename_project(wb, project, new_id):
    """Move a project and every line item that names it onto a new Project ID.

    The ID is the only thing tying a selection to its project, so this has to happen inside one
    save: a workspace where the project has been renumbered and its line items have not is a
    project with nothing in it. Returns how many line items moved.

    The cover image is stored under a file named after the old ID. The cell would go on
    resolving to it, but the next upload would write the new name and leave the sheet pointing
    at the old one, so the file is copied across now; a cover that cannot be read is not worth
    failing a rename for.
    """
    old_id = project["Project ID"]
    projects, selections = common.Sheet(wb["Projects"]), common.Sheet(wb["Selections"])
    owned = [r["_row"] for r in records(wb, "Selections") if r["Project ID"] == old_id]
    put(projects, project["_row"], {"Project ID": new_id})
    for row in owned:
        put(selections, row, {"Project ID": new_id})
    cover = common.clean(project.get("Cover Image"))
    if cover.startswith(COVER_PREFIX):
        name = cover[len(COVER_PREFIX):]
        suffix = Path(name).suffix.lower()
        renamed = f"cover-{re.sub(r'[^A-Za-z0-9_-]', '_', new_id)[:40]}{suffix}"
        if renamed != name:
            try:
                store.store_cover(renamed, store.read_cover(name), COVER_MIME.get(suffix, "image/png"))
                put(projects, project["_row"], {"Cover Image": f"{COVER_PREFIX}{renamed}"})
            except Exception:
                pass
    return len(owned)


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
            return jsonify(ok=True, deleted=True, removed=len(owned))
        if data.get("create") and existing:
            raise ValidationError("That Project ID already exists. Choose a unique ID.")
        old_id, moved = values.get("Project ID"), 0
        if data.get("rename") is not None:
            if not existing:
                raise ConflictError("This project no longer exists. Refresh the page.")
            new_id = validate_values({"Project ID": data["rename"]}, PROJECT_FIELDS)["Project ID"]
            if not new_id:
                raise ValidationError("Enter the new Project ID.")
            if len(new_id) > 50:
                raise ValidationError("A Project ID can be up to 50 characters.")
            if new_id != old_id:
                if any(p["Project ID"] == new_id for p in records(wb, "Projects")):
                    raise ValidationError("That Project ID already exists. Choose a unique ID.")
                moved = rename_project(wb, existing, new_id)
                values["Project ID"] = new_id   # the rest of this save writes against the new ID
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
            spec_template.register_locations(wb, [location for location, _ in template])
            seeded, _ = spec_template.apply(wb, values["Project ID"], template)
            from openpyxl.utils import get_column_letter
            selections = wb["Selections"]
            selections.auto_filter.ref = f"A1:{get_column_letter(selections.max_column)}{selections.max_row}"
        detail = (f" (renamed from {old_id}, {moved} line items moved)" if moved
                  else f" (+{seeded} line items)" if seeded else "")
        store.save(wb, revision, f"project {values.get('Project ID')}{detail}")
    return jsonify(ok=True, project_id=values.get("Project ID"), seeded=seeded, moved=moved)


COVER_MIME = {".png": "image/png", ".jpg": "image/jpeg"}
COVER_PIXEL_LIMIT = 2400


@app.post("/api/projects/<pid>/cover")
def upload_cover(pid):
    """Store a cover image outside the workspace and point the project's Cover Image at it."""
    upload = request.files.get("file")
    if not upload:
        raise ValidationError("Choose a PNG or JPEG image.")
    try:
        with Image.open(upload.stream) as source:
            image = source.convert("RGBA") if source.mode in {"RGBA", "LA", "PA", "P"} else source.convert("RGB")
        image.thumbnail((COVER_PIXEL_LIMIT, COVER_PIXEL_LIMIT), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        if image.mode == "RGBA" and image.getchannel("A").getextrema()[0] < 255:
            image.save(buffer, "PNG")
            ext, mime = ".png", "image/png"
        else:
            image.convert("RGB").save(buffer, "JPEG", quality=88)
            ext, mime = ".jpg", "image/jpeg"
    except Exception:
        raise ValidationError("That file is not a readable image. Choose a PNG or JPEG.") from None
    name = f"cover-{re.sub(r'[^A-Za-z0-9_-]', '_', pid)[:40]}{ext}"
    with store.lock:
        wb, revision = current({"revision": request.form.get("revision", "")})
        project = next((p for p in records(wb, "Projects") if p["Project ID"] == pid), None)
        if not project:
            raise ValidationError("Choose a valid project.")
        store.store_cover(name, buffer.getvalue(), mime)
        put(common.Sheet(wb["Projects"]), project["_row"], {"Cover Image": f"{COVER_PREFIX}{name}"})
        store.save(wb, revision, f"cover image {pid}")
    return jsonify(ok=True, value=f"{COVER_PREFIX}{name}")


@app.get("/api/covers/<name>")
def project_cover(name):
    """A stored cover image, for the thumbnail beside the Cover Image field. The name comes
    from a cover: cell value, so no workspace read is needed to serve it."""
    return send_file(io.BytesIO(store.read_cover(name)),
                     mimetype=COVER_MIME.get(Path(name).suffix, "image/png"))


@app.post("/api/selections/order")
def reorder_selections():
    data = payload()
    project = data.get("project")
    order, updates = data.get("order"), data.get("updates") or {}
    with store.lock:
        wb, revision = current(data)
        sheet = common.Sheet(wb["Selections"])
        positions = sorted(r["_row"] for r in records(wb, "Selections") if r["Project ID"] == project)
        try:
            order = [int(row) for row in order or []]
        except (TypeError, ValueError):
            order = []
        if sorted(order) != positions:
            raise ValidationError("The new order must include every line item in this project.")
        try:
            updates = {int(row): values for row, values in updates.items()}
        except (TypeError, ValueError):
            raise ValidationError("Reordered field edits must name a line item row.")
        if any(row not in positions for row in updates):
            raise ValidationError("Reordered field edits must belong to this project.")
        for row, values in updates.items():
            put(sheet, row, validate_values(values, SELECTION_FIELDS + OPTIONAL_SELECTION_FIELDS))
        captured = {row: {h: (cell.value, cell.hyperlink.target if cell.hyperlink else None)
                          for h, col in sheet.cols.items()
                          for cell in [sheet.ws.cell(row, col)]}
                    for row in positions}
        for pos, src in zip(positions, order):
            for h, (value, link) in captured[src].items():
                cell = sheet.ws.cell(pos, sheet.cols[h])
                cell.value, cell.hyperlink = value, link
        store.save(wb, revision, f"reorder selections in {project}")
    return jsonify(ok=True)


def merge_selection(old, values, row=None):
    """Merge validated cell values into one selection record; shared by single and batch saves."""
    merged = {k: old.get(k, "") if old else "" for k in SELECTION_FIELDS + OPTIONAL_SELECTION_FIELDS}
    merged.update(values)
    if not merged["Project ID"] or not merged["Item"]:
        raise ValidationError("Project and item name are required.")
    merged["Client Status"] = merged.get("Client Status") or "Proposed"
    if old and any(merged[k] != old[k] for k in IDENTITY_FIELDS):
        if merged["Client Status"] in {"Presented", "Approved"}:
            merged["Client Status"] = "Changed"
        elif merged["Client Status"] == "Rejected":
            merged["Client Status"] = "Proposed"
        # The product itself changed, so a link and image found for the old one no longer describe it.
        new_url = merged["Product URL"] if merged["Product URL"] != old["Product URL"] else ""
        merged.update({k: "" for k in ["Product URL", "Image URL"]})
        if merged["Product Name"] == old["Product Name"]:
            merged["Product Name"] = ""
        merged["Product URL"] = new_url
    merged["Include in Lookbook"] = merged["Include in Lookbook"] or "Yes"
    return merged


@app.post("/api/selections")
def save_selection():
    data = payload()
    values = {} if data.get("batch") else validate_values(data.get("values"), SELECTION_FIELDS + OPTIONAL_SELECTION_FIELDS)
    with store.lock:
        wb, revision = current(data)
        sheet = common.Sheet(wb["Selections"])
        if data.get("delete"):
            old = next((r for r in records(wb, "Selections") if r["_row"] == data.get("row")), None)
            if not old:
                raise ConflictError("This selection no longer exists. Refresh the page.")
            delete_record(wb, "Selections", old["_row"])
            store.save(wb, revision, f"delete selection row {old['_row']} ({old['Item']})")
            return jsonify(ok=True, deleted=True)
        if data.get("batch"):
            sheet = ensure_selection_columns(wb)
            applied = []
            for key, raw in data["batch"].items():
                try:
                    row = int(key)
                except (TypeError, ValueError):
                    raise ValidationError("Batch edits must name selection rows.")
                old = next((r for r in records(wb, "Selections") if r["_row"] == row), None)
                if not old:
                    raise ConflictError(f"Selection row {row} no longer exists. Refresh the page.")
                merged = merge_selection(old, validate_values(raw, SELECTION_FIELDS + OPTIONAL_SELECTION_FIELDS), row)
                put(sheet, row, merged)
                applied.append(row)
            store.save(wb, revision, f"batch edit {len(applied)} selection rows")
            return jsonify(ok=True, applied=applied)
        old = None
        if data.get("row") is not None:
            old = next((r for r in records(wb, "Selections") if r["_row"] == data["row"]), None)
            if not old:
                raise ConflictError("This selection no longer exists. Refresh the page.")
        row = old["_row"] if old else next_row(sheet)
        merged = merge_selection(old, values, row)
        sheet = ensure_selection_columns(wb)
        put(sheet, row, merged)
        from openpyxl.utils import get_column_letter
        sheet.ws.auto_filter.ref = f"A1:{get_column_letter(sheet.ws.max_column)}{max(sheet.ws.max_row, row)}"
        if "custom_fields" in data:
            set_custom_fields(wb, "Selections", row, data["custom_fields"])
        store.save(wb, revision, f"selection row {row}")
    return jsonify(ok=True, row=row)


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


PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

SPEC_CSV_FIELDS = ["Section", "Location", "Room / Area", "Item", "Manufacturer", "Model #", "Finish / Color",
                   "Size / Configuration", common.QTY_UNIT, "Unit Price", "Markup %", PRODUCT_LINK,
                   "Client Notes", "Client Status", "Include in Lookbook"]
CSV_ROW_LIMIT = 5000
# Columns the sheet shows under a name of their own: the product URL as Product Link, and the
# quantity with what it counts as one Qty / Unit column, which is what each is hidden by.
CSV_HIDE_KEYS = {PRODUCT_LINK: ("Product URL",), common.QTY_UNIT: common.QTY_UNIT_KEYS}
# Every header an export of this sheet can carry, so a download that hid one half of
# Qty / Unit - leaving the other behind under its own name - still imports.
CSV_HEADERS = SPEC_CSV_FIELDS + list(common.QTY_UNIT_KEYS)


def hidden_option(value):
    """The columns this export leaves out, as named by the hide-columns chooser."""
    try:
        return build_lookbook.hidden_columns(value)
    except ValueError as error:
        raise ValidationError(str(error)) from None


def csv_fields(hidden):
    """The spec sheet's columns, minus the hidden ones."""
    headers = (build_lookbook.column_header(field, CSV_HIDE_KEYS.get(field, (field,)), hidden)
               for field in SPEC_CSV_FIELDS)
    return [header for header in headers if header]


def spec_key(row):
    return tuple(common.clean(row.get(field)) for field in SPEC_MATCH_FIELDS)


def csv_values(entry, headers):
    """One CSV line as selection fields; Qty / Unit splits in two and Product Link carries the URL."""
    values = {field: common.clean(entry.get(field)) for field in CSV_HEADERS if field in headers}
    quantity = values.pop(common.QTY_UNIT, None)
    if quantity is not None:
        # One column over two workspace fields. A half-hidden export carries the other half
        # under its own name, so a column naming a field outright wins over the split.
        values = {**common.split_qty_unit(quantity), **values}
    link = values.pop(PRODUCT_LINK, "")
    if link:
        # Excel writes a formula's displayed text ("Product Link") when it re-saves a CSV, so a
        # cell we cannot read an address out of means "unchanged", never "drop the saved link".
        values["Product URL"] = parse_hyperlink(link)
        if not values["Product URL"]:
            del values["Product URL"]
    return values


@app.get("/api/selections/csv")
def export_spec_csv():
    project_id = common.clean(request.args.get("project"))
    hidden = hidden_option(request.args.get("hide", ""))
    with store.lock:
        wb, _ = store.snapshot()
        project = next((p for p in records(wb, "Projects") if p["Project ID"] == project_id), None)
        if not project:
            raise ValidationError("Choose a valid project.")
        rows = [r for r in records(wb, "Selections") if r["Project ID"] == project_id]
    # Workspace order, so the file reads like the view; the import matches on spec_key, not position.
    fields = csv_fields(hidden)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    # Product Link is generated from the URL, never copied from the workspace's own link column.
    writer.writerows({**{field: build_lookbook.spec_value(row, field) for field in fields},
                      PRODUCT_LINK: hyperlink_formula(row.get("Product URL"))} for row in rows)
    name = re.sub(r"[^A-Za-z0-9_-]", "_", project_id)[:50]
    return send_file(io.BytesIO(buffer.getvalue().encode("utf-8-sig")), as_attachment=True,
                     download_name=f"{name}_spec_sheet.csv", mimetype="text/csv")


def group_option(value):
    """The heading the spec sheet is grouped by, as chosen by the view's group-by chips."""
    group = common.clean(value) or "Room / Area"
    if group not in build_lookbook.SPEC_GROUPS:
        raise ValidationError(f"Group the spec sheet by one of: {', '.join(build_lookbook.SPEC_GROUPS)}.")
    return group


def orientation_option(value):
    """The page a spec-sheet document prints on, as chosen in the export dialog."""
    try:
        return build_lookbook.spec_orientation(value)
    except ValueError as error:
        raise ValidationError(str(error)) from None


def spec_export_rows():
    """The requested project's line items in spec-sheet order for a document export.

    Workspace order, which is the order the view shows and the order dragging a row
    writes back. Never `schedule_sort`: that is the finish schedule's Location order,
    and applying it here reshuffles a download away from the screen it came from.
    """
    project_id = common.clean(request.args.get("project"))
    with store.lock:
        wb, _ = store.snapshot()
        project = next((p for p in records(wb, "Projects") if p["Project ID"] == project_id), None)
        if not project:
            raise ValidationError("Choose a valid project.")
        rows = [r for r in records(wb, "Selections") if r["Project ID"] == project_id]
    if not rows:
        raise ValidationError("This project has no line items to export.")
    return project_id, project, rows


@app.get("/api/selections/docx")
def export_spec_docx():
    """The same line items as the CSV, as a Word document to read, print and mark up."""
    import build_word
    hidden = hidden_option(request.args.get("hide", ""))
    group = group_option(request.args.get("group"))
    orientation = orientation_option(request.args.get("orientation"))
    project_id, project, rows = spec_export_rows()
    with EXPORT_LOCK, tempfile.TemporaryDirectory(prefix="uh_spec_") as folder:
        output = build_word.build_spec(project_id, project, rows, output_dir=Path(folder),
                                       hidden=hidden, group=group, orientation=orientation)
        raw = output.read_bytes()
    return send_file(io.BytesIO(raw), as_attachment=True, download_name=output.name, mimetype=DOCX_MIME)


@app.get("/api/selections/pdf")
def export_spec_pdf():
    """The spec sheet as a printable PDF: the same columns as the Word copy."""
    hidden = hidden_option(request.args.get("hide", ""))
    group = group_option(request.args.get("group"))
    orientation = orientation_option(request.args.get("orientation"))
    project_id, project, rows = spec_export_rows()
    with EXPORT_LOCK, tempfile.TemporaryDirectory(prefix="uh_spec_") as folder:
        output = build_lookbook.build_spec(project_id, project, rows, output_dir=Path(folder),
                                           hidden=hidden, group=group, orientation=orientation)
        raw = output.read_bytes()
    return send_file(io.BytesIO(raw), as_attachment=True, download_name=output.name, mimetype=PDF_MIME)


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
    unknown = [name for name in headers if name and name not in CSV_HEADERS]
    if unknown:
        raise ValidationError(f"Unexpected column(s): {', '.join(unknown[:4])}. Export the spec sheet to see the format.")
    incoming = []
    for entry in reader:
        values = csv_values(entry, headers)
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
                link = merged["Product URL"] if merged["Product URL"] != old["Product URL"] else ""
                if any(merged[key] != old[key] for key in IDENTITY_FIELDS):
                    # the product itself changed, so its old link and image no longer apply
                    merged.update({key: "" for key in ["Product URL", "Image URL"]})
                    if merged["Client Status"] in {"Presented", "Approved"}:
                        merged["Client Status"] = "Changed"
                if link:
                    merged["Product URL"] = link
                put(sheet, old["_row"], merged)
                updated += 1
            else:
                row = next_row(sheet)
                put(sheet, row, {**{k: "" for k in SELECTION_FIELDS + OPTIONAL_SELECTION_FIELDS}, **values,
                                 "Project ID": project_id,
                                 "Include in Lookbook": values.get("Include in Lookbook") or "Yes",
                                 "Client Status": values.get("Client Status") or "Proposed"})
                added += 1
        from openpyxl.utils import get_column_letter
        sheet.ws.auto_filter.ref = f"A1:{get_column_letter(sheet.ws.max_column)}{sheet.ws.max_row}"
        store.save(wb, revision, f"spec csv import {project_id} (+{added}/~{updated})")
    return jsonify(ok=True, added=added, updated=updated)


def check_exposure():
    """Refuse to serve beyond this machine without sign-in, so a misconfigured host cannot leak client data."""
    if LOCAL_ONLY:
        return
    if not REQUIRE_LOGIN:
        raise SystemExit("Refusing to start: UH_HOST is not local but sign-in is not configured. "
                         "Set SUPABASE_URL and SUPABASE_ANON_KEY, and leave UH_REQUIRE_LOGIN unset.")
    if not os.getenv("UH_SECRET_KEY"):
        raise SystemExit("Refusing to start: set UH_SECRET_KEY to a long random value so sessions "
                         "survive restarts and cannot be forged.")
    if not os.getenv("UH_TRUSTED_HOSTS"):
        raise SystemExit("Refusing to start: set UH_TRUSTED_HOSTS to this deployment's hostname, "
                         "for example uh-selections.onrender.com.")
    if not store.remote:
        # A hosted filesystem is temporary, so edits would vanish on the next deploy.
        raise SystemExit("Refusing to start: UH_HOST is not local but the workspace is a local file, "
                         "which a redeploy would discard. Set SUPABASE_URL and SUPABASE_SERVICE_KEY.")


check_exposure()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run the UH Homes selection tracker.")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "5000")))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535.")
    print(f"UH Homes Selections: http://{HOST}:{args.port}")
    if store.remote:
        print("Workspace stored in Supabase. Sign-in required." if REQUIRE_LOGIN else "Workspace stored in Supabase.")
    else:
        print("Local access only. Close the master workspace in Excel before saving changes.")
    app.run(host=HOST, port=args.port, debug=False)
