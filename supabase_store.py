"""The master workbook kept in Supabase Storage, with its revision held in Postgres.

Each save uploads a new, immutable object and then advances a single pointer row by
compare-and-swap, so two app processes can never both win a save. Earlier objects are
the saved copies the app already offers to restore, which means hosting needs no disk.

Only the service key reaches this module, and it never appears in responses or logs.
"""
import datetime as dt
import hashlib
import io
import json
import os
import re
import time

import requests

from workbook_store import BACKUP_KEEP, ConflictError, ValidationError, history_entry

TIMEOUT = (5, 30)
STATE = "uh_workbook_state"
VERSIONS = "uh_workbook_versions"
HISTORY = "uh_history"
LOOKUP_STATE = "uh_lookup_state"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ConfigError(RuntimeError):
    pass


def settings():
    """Read the Supabase configuration, or None when the app is running on a local file."""
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_KEY") or ""
    if not url and not key:
        return None
    if not url or not key:
        raise ConfigError("Set both SUPABASE_URL and SUPABASE_SERVICE_KEY, or neither.")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+", url):
        raise ConfigError("SUPABASE_URL must look like https://<project>.supabase.co.")
    return {"url": url, "key": key,
            "bucket": os.getenv("SUPABASE_BUCKET", "workbooks"),
            "name": os.getenv("UH_WORKBOOK_NAME", "UH_Homes_Selections_Tracker.xlsx")}


class Supabase:
    """Just enough of the Storage and PostgREST APIs, over the requests already in use."""

    def __init__(self, config):
        self.url, self.bucket = config["url"], config["bucket"]
        self.session = requests.Session()
        self.session.headers.update({"apikey": config["key"], "Authorization": f"Bearer {config['key']}",
                                     "User-Agent": "UH-Homes-Selections/1.0"})

    def _check(self, response, action):
        if response.status_code >= 400:
            # The body can echo the request, so only the status reaches the caller.
            raise RuntimeError(f"Supabase could not {action} (HTTP {response.status_code}).")
        return response

    def download(self, path):
        response = self.session.get(f"{self.url}/storage/v1/object/{self.bucket}/{path}", timeout=TIMEOUT)
        return self._check(response, "read the stored workbook").content

    def upload(self, path, raw):
        response = self.session.post(f"{self.url}/storage/v1/object/{self.bucket}/{path}", data=raw,
                                     headers={"Content-Type": XLSX_MIME, "x-upsert": "true"}, timeout=TIMEOUT)
        self._check(response, "store the workbook")

    def remove(self, path):
        self.session.delete(f"{self.url}/storage/v1/object/{self.bucket}/{path}", timeout=TIMEOUT)

    def bucket_info(self):
        response = self.session.get(f"{self.url}/storage/v1/bucket/{self.bucket}", timeout=TIMEOUT)
        return self._check(response, "read the storage bucket").json()

    def select(self, table, params, count=False):
        headers = {"Prefer": "count=exact"} if count else {}
        response = self.session.get(f"{self.url}/rest/v1/{table}", params=params, headers=headers, timeout=TIMEOUT)
        self._check(response, f"read {table}")
        if count:
            return int((response.headers.get("Content-Range", "/0").rsplit("/", 1)[-1]) or 0)
        return response.json()

    def insert(self, table, row, upsert=False):
        prefer = "return=representation" + (",resolution=merge-duplicates" if upsert else "")
        response = self.session.post(f"{self.url}/rest/v1/{table}", json=row,
                                     headers={"Prefer": prefer, "Content-Type": "application/json"}, timeout=TIMEOUT)
        return self._check(response, f"write {table}").json()

    def update(self, table, params, row):
        """Returns the rows actually changed, so a filtered update doubles as compare-and-swap."""
        response = self.session.patch(f"{self.url}/rest/v1/{table}", params=params, json=row,
                                      headers={"Prefer": "return=representation",
                                               "Content-Type": "application/json"}, timeout=TIMEOUT)
        return self._check(response, f"update {table}").json()


class RemoteState:
    """The automatic lookup history as one Postgres row."""

    def __init__(self, api, key):
        self.api, self.key = api, key

    def read(self):
        rows = self.api.select(LOOKUP_STATE, {"key": f"eq.{self.key}", "select": "content"})
        return rows[0]["content"] if rows else None

    def write(self, content):
        self.api.insert(LOOKUP_STATE, {"key": self.key, "content": content,
                                       "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}, upsert=True)

    def set_aside(self):
        self.api.insert(LOOKUP_STATE, {"key": f"{self.key}-corrupt-{dt.datetime.now():%Y%m%d%H%M%S}",
                                       "content": self.read() or ""})
        self.api.update(LOOKUP_STATE, {"key": f"eq.{self.key}"},
                        {"content": json.dumps({"entries": {}, "pending": {}})})


class SupabaseBackend:
    """Workbook bytes in Storage, authoritative revision in Postgres."""

    remote = True

    def __init__(self, config=None, api=None):
        self.config = config or settings()
        if not self.config:
            raise ConfigError("Supabase is not configured.")
        self.api = api or Supabase(self.config)
        self.stem = os.path.splitext(self.config["name"])[0]
        self._cache = (None, None)      # (revision, bytes); versions are immutable, so this cannot go stale
        # The interface polls every few seconds and automatic lookup ticks alongside it, so the
        # pointer row is held briefly to collapse those bursts into one request. A stale read can
        # only ever produce a rejected save, never a lost one: the compare-and-swap decides.
        self._ttl = float(os.getenv("UH_REVISION_TTL", "1"))
        self._pointer = (0.0, None)

    @property
    def name(self):
        return self.config["name"]

    @property
    def path(self):
        raise AttributeError("The hosted workbook has no local path. Download it from the app instead.")

    def _state(self, fresh=False):
        cached_at, cached = self._pointer
        if cached and not fresh and time.monotonic() - cached_at < self._ttl:
            return cached
        rows = self.api.select(STATE, {"id": "eq.1", "select": "revision,object_path"})
        if not rows:
            raise ValidationError("No workbook has been uploaded yet. Run 'python supabase_store.py push' once to seed it.")
        self._pointer = (time.monotonic(), rows[0])
        return rows[0]

    def revision(self):
        return self._state()["revision"]

    def read(self):
        state = self._state()
        revision, path = state["revision"], state["object_path"]
        if self._cache[0] != revision:
            self._cache = (revision, self.api.download(path))
        return self._cache[1], revision

    def locked_elsewhere(self):
        return False        # nothing but this app can hold the hosted workbook open

    def backup(self):
        return None         # every save keeps its predecessor, so there is nothing to copy

    def write(self, wb, revision):
        buffer = io.BytesIO()
        wb.save(buffer)
        raw = buffer.getvalue()
        # Every save is a new revision: openpyxl stamps docProps/core.xml with the time of
        # writing, so even an exactly reverted edit produces different bytes.
        new_revision = hashlib.sha256(raw).hexdigest()
        name = f"{self.stem}_{dt.datetime.now():%Y%m%d_%H%M%S_%f}.xlsx"
        path = f"versions/{name}"
        self.api.upload(path, raw)
        try:
            self.api.insert(VERSIONS, {"revision": new_revision, "name": name,
                                       "object_path": path, "size": len(raw)}, upsert=True)
            # Only one writer can match the expected revision, so this settles concurrent saves.
            if not self.api.update(STATE, {"id": "eq.1", "revision": f"eq.{revision}"},
                                   {"revision": new_revision, "object_path": path,
                                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}):
                raise ConflictError("Someone else saved first. Refresh the page and reapply your change.")
        except Exception:
            # Leave neither an object nor a version row the pointer never reached.
            self.api.remove(path)
            self.api.update(VERSIONS, {"revision": f"eq.{new_revision}"}, {"object_path": "", "size": 0})
            raise
        self._cache = (new_revision, raw)
        self._pointer = (time.monotonic(), {"revision": new_revision, "object_path": path})
        self._prune()

    def _prune(self):
        """Drop the objects behind the oldest versions, keeping their history rows."""
        old = self.api.select(VERSIONS, {"select": "revision,object_path", "order": "created_at.desc",
                                         "offset": BACKUP_KEEP, "limit": 50, "object_path": "neq."})
        for row in old:
            self.api.remove(row["object_path"])
            self.api.update(VERSIONS, {"revision": f"eq.{row['revision']}"}, {"object_path": "", "size": 0})

    def log(self, note):
        self.api.insert(HISTORY, history_entry(note))

    def count_notes(self, prefix):
        # PostgREST passes like patterns to Postgres, where \ % and _ are special.
        escaped = prefix.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
        return self.api.select(HISTORY, {"select": "id", "note": f"like.{escaped}*"}, count=True)

    def backups(self):
        """Earlier versions only, matching the local backups folder, which never holds the live file."""
        live = self._state()["revision"]
        rows = self.api.select(VERSIONS, {"select": "revision,name,created_at,size,object_path",
                                          "order": "created_at.desc", "limit": BACKUP_KEEP + 1})
        return [{"name": row["name"], "when": row["created_at"][:19], "size": row["size"] or 0}
                for row in rows if row["object_path"] and row["revision"] != live][:BACKUP_KEEP]

    def read_backup(self, name):
        rows = self.api.select(VERSIONS, {"select": "object_path", "name": f"eq.{name}"})
        if not rows or not rows[0]["object_path"]:
            raise ValidationError("That saved copy is no longer available. Refresh and try again.")
        return self.api.download(rows[0]["object_path"])

    def state(self):
        key = hashlib.sha256(f"{self.config['bucket']}/{self.name}".encode("utf-8")).hexdigest()[:16]
        return RemoteState(self.api, key)


def push(source):
    """Seed the hosted workbook from a local file, replacing whatever revision is current."""
    from openpyxl import load_workbook
    from workbook_store import validate_workbook
    raw = open(source, "rb").read()
    validate_workbook(load_workbook(io.BytesIO(raw), keep_links=False))
    backend = SupabaseBackend()
    revision = hashlib.sha256(raw).hexdigest()
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = f"versions/{backend.stem}_{stamp}.xlsx"
    backend.api.upload(path, raw)
    backend.api.insert(VERSIONS, {"revision": revision, "name": os.path.basename(path),
                                  "object_path": path, "size": len(raw), "note": "seeded"}, upsert=True)
    backend.api.insert(STATE, {"id": 1, "revision": revision, "object_path": path,
                               "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}, upsert=True)
    backend.api.insert(HISTORY, history_entry(f"seeded from {os.path.basename(source)}"))
    return revision


def pull(destination):
    """Download the hosted workbook, for a local copy or an offline check."""
    raw, revision = SupabaseBackend().read()
    with open(destination, "wb") as out:
        out.write(raw)
    return revision


def check():
    """Report exactly what is and is not ready for hosting. Never prints a secret."""
    results = []

    def note(ok, message):
        results.append((ok, message))

    try:
        config = settings()
    except ConfigError as error:
        return [(False, str(error))]
    if not config:
        return [(False, "Supabase is not configured. Add SUPABASE_URL and SUPABASE_SERVICE_KEY to .env.")]
    note(True, f"Configured for {config['url']} (bucket '{config['bucket']}').")

    api = Supabase(config)
    tables_ok = True
    for table in (STATE, VERSIONS, HISTORY, LOOKUP_STATE):
        try:
            api.select(table, {"select": "*", "limit": 1})
            note(True, f"Table {table} exists.")
        except Exception:
            tables_ok = False
            note(False, f"Table {table} is missing. Apply supabase/migrations/ (SQL Editor works too).")

    try:
        info = api.bucket_info()
        note(not info.get("public", False),
             f"Bucket '{config['bucket']}' exists and is private." if not info.get("public")
             else f"Bucket '{config['bucket']}' is PUBLIC. Make it private: anyone could download the workbook.")
    except Exception:
        note(False, f"Bucket '{config['bucket']}' is missing. Create it in Storage, private, "
                    f"or re-run the migration.")

    if tables_ok:
        try:
            raw, revision = SupabaseBackend(config, api=api).read()
            note(True, f"Workbook seeded: revision {revision[:12]}, {len(raw):,} bytes.")
        except ValidationError as error:
            note(False, str(error))
        except Exception:
            note(False, "The seeded workbook could not be downloaded. Check the bucket contents.")

    import auth
    if auth.settings():
        note(True, "Sign-in configured (SUPABASE_ANON_KEY present).")
    else:
        note(False, "SUPABASE_ANON_KEY is missing, so the hosted app cannot accept sign-ins.")
    for name, value in [("UH_SECRET_KEY", os.getenv("UH_SECRET_KEY")),
                        ("UH_TRUSTED_HOSTS", os.getenv("UH_TRUSTED_HOSTS"))]:
        note(bool(value), f"{name} is set." if value else
             f"{name} is not set. Required on the host (not locally).")
    return results


if __name__ == "__main__":
    import argparse
    import common

    parser = argparse.ArgumentParser(description="Move the master workbook between this machine and Supabase.")
    parser.add_argument("action", choices=["push", "pull", "check"])
    parser.add_argument("--file", default=str(common.WORKBOOK))
    args = parser.parse_args()
    if args.action == "check":
        failures = 0
        for ok, message in check():
            print(f"  {'OK  ' if ok else 'TODO'}  {message}")
            failures += not ok
        print("\nReady to host." if not failures else f"\n{failures} item(s) still to do.")
        raise SystemExit(1 if failures else 0)
    if args.action == "push":
        print(f"Uploaded {args.file} as revision {push(args.file)[:12]}.")
    else:
        print(f"Downloaded revision {pull(args.file)[:12]} to {args.file}.")
