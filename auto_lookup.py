import datetime as dt
import hashlib
import json
import os
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path

import common
import find_urls
from workbook_store import ConflictError, IDENTITY_FIELDS, ValidationError, http_url, put, records, validate_values, SELECTION_FIELDS


def digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def stamp(row):
    identity = [common.clean(row.get(key)) for key in ["Project ID", *IDENTITY_FIELDS]]
    return {"fingerprint": digest(json.dumps(identity)), "name": digest(row.get("Product Name", "")),
            "url": digest(row.get("Product URL", "")), "project": row["Project ID"]}


class AutoLookup:
    def __init__(self, store, provider_info, search=None):
        self.store, self.provider_info = store, provider_info
        self.search = search or find_urls.lookup
        self.state_path = store.path.parent / "lookup_state" / f"{digest(store.path.resolve())[:16]}.json"
        self.entries, self.pending = {}, {}
        self.initialized = False
        self.message = ""
        self._saved = ""
        self._stop = threading.Event()
        self._thread = None
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.entries, self.pending = data["entries"], data["pending"]
                self.initialized = True
                for job in self.pending.values():
                    if job["phase"] == "searching":
                        job["phase"] = "queued"
            except (ValueError, KeyError, TypeError):
                raise ValidationError("Automatic lookup history could not be read. Restore the lookup_state file before generating presentations.") from None

    def _persist(self):
        content = json.dumps({"entries": self.entries, "pending": self.pending}, sort_keys=True)
        if content == self._saved:
            return
        self.state_path.parent.mkdir(exist_ok=True)
        handle, name = tempfile.mkstemp(suffix=".json", dir=self.state_path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as output:
                output.write(content)
            os.replace(name, self.state_path)
            self._saved = content
        finally:
            Path(name).unlink(missing_ok=True)

    def observe(self, wb):
        with self.store.lock:
            current = {str(row["_row"]): row for row in records(wb, "Selections")}
            stamps = {key: stamp(row) for key, row in current.items()}
            buckets = defaultdict(list)
            for key, entry in self.entries.items():
                buckets[entry["fingerprint"]].append(key)
            matched = {key: key for key, entry in stamps.items()
                       if key in self.entries and self.entries[key]["fingerprint"] == entry["fingerprint"]}
            used = set(matched.values())
            for key, entry in stamps.items():
                if key not in matched:
                    old_key = next((candidate for candidate in buckets[entry["fingerprint"]] if candidate not in used), None)
                    if old_key is not None:
                        matched[key] = old_key
                        used.add(old_key)
            previous = {}
            pending = {}
            for key in current:
                old_key = matched.get(key, key if key not in used else None)
                previous[key] = self.entries.get(old_key)
                if old_key in self.pending:
                    pending[key] = self.pending[old_key]
            self.pending = pending
            for key, row in current.items():
                entry, old = stamps[key], previous[key]
                if self.initialized and (old is None or old["fingerprint"] != entry["fingerprint"]):
                    self.pending[key] = {"fingerprint": entry["fingerprint"], "project": row["Project ID"],
                                         "clear_name": bool(old and old["name"] == entry["name"]), "prepared": False,
                                         "phase": "queued", "attempts": 0, "retry_at": 0, "result": None}
                elif key in self.pending and old and old["url"] != entry["url"] and http_url(row["Product URL"]):
                    self.pending.pop(key)
                self.entries[key] = entry
            self.entries = {key: value for key, value in self.entries.items() if key in current}
            self.pending = {key: value for key, value in self.pending.items() if key in current}
            self.initialized = True
            self._persist()

    def acknowledge(self, wb, row):
        with self.store.lock:
            record = next((r for r in records(wb, "Selections") if r["_row"] == row), None)
            if record:
                self.entries[str(row)] = stamp(record)
            self.pending.pop(str(row), None)
            self._persist()

    def excel_open(self):
        return self.store.path.with_name("~$" + self.store.path.name).exists()

    def status(self):
        with self.store.lock:
            return {"enabled": True, "message": self.message,
                    "pending": [{"row": int(key), "project": job["project"], "phase": job["phase"]}
                                for key, job in self.pending.items()]}

    def display_rows(self, rows):
        with self.store.lock:
            out = []
            for row in rows:
                job = self.pending.get(str(row["_row"]))
                if job:
                    row = {**row, "Product URL": "", "Image URL": "", "Lookup Status": "Not run",
                           "_auto_phase": job["phase"]}
                    if job["clear_name"] and not job["prepared"]:
                        row["Product Name"] = ""
                out.append(row)
            return out

    def retry(self):
        with self.store.lock:
            for job in self.pending.values():
                if job["phase"] in {"failed", "retry", "waiting_key"}:
                    job.update(phase="queued", attempts=0, retry_at=0, result=None)
            self.message = ""
            self._persist()

    def _update(self, wb, revision, row, updates):
        updates = validate_values(updates, SELECTION_FIELDS)
        if any(common.clean(row.get(key)) != value for key, value in updates.items()):
            put(common.Sheet(wb["Selections"]), row["_row"], updates)
            self.store.save(wb, revision)
        row.update(updates)
        key = str(row["_row"])
        self.entries[key] = stamp(row)
        if key in self.pending:
            self.pending[key]["fingerprint"] = self.entries[key]["fingerprint"]
        return self.store.snapshot()

    def tick(self):
        with self.store.lock:
            wb, revision = self.store.snapshot()
            self.observe(wb)
            self.message = ""
            if not self.pending:
                return
            if self.excel_open():
                self.message = "Close the master workbook in Excel to let automatic lookup save updated links."
                for job in self.pending.values():
                    if job["phase"] not in {"retry", "failed"}:
                        job["phase"] = "waiting_excel"
                self._persist()
                return
            ready = [(key, job) for key, job in self.pending.items()
                     if job["phase"] != "failed" and job["retry_at"] <= time.time()]
            if not ready:
                return
            key, job = ready[0]
            row = next(r for r in records(wb, "Selections") if str(r["_row"]) == key)
            if not job["prepared"]:
                updates = {"Product URL": "", "Image URL": "", "Checked On": "", "Lookup Status": "Not run",
                           "Lookup Notes": "Product changed. Automatic lookup queued."}
                if job["clear_name"]:
                    updates["Product Name"] = ""
                wb, revision = self._update(wb, revision, row, updates)
                job["prepared"] = True
                self._persist()
            if not row["Manufacturer"] or not row["Model #"]:
                self._update(wb, revision, row, {"Lookup Status": "Not found", "Lookup Notes": "Manufacturer and Model # are both required. Complete them to start automatic lookup."})
                self.pending.pop(key)
                self._persist()
                return
            info = self.provider_info()
            if not info["ready"]:
                self.message = f"Automatic lookup is waiting for {info['key_name']} in the local .env file. Restart the app after configuring it."
                self._update(wb, revision, row, {"Lookup Status": "Search error", "Lookup Notes": self.message})
                job["phase"] = "waiting_key"
                job["retry_at"] = time.time() + 15
                self._persist()
                return
            fingerprint = job["fingerprint"]
            result = job.get("result")
            domain = common.manufacturer_domains(wb).get(row["Manufacturer"].lower(), "")
            job["phase"] = "searching" if result is None else "saving"
            self._persist()
        if result is None:
            try:
                result = self.search(find_urls.PROVIDERS[info["name"]], row["Manufacturer"], row["Model #"], domain,
                                     product_name=row["Product Name"] or row["Item"])
            except Exception:
                result = {"status": "Search error", "notes": "Search provider unavailable. Check credentials, quota, and connection."}
        with self.store.lock:
            wb, revision = self.store.snapshot()
            self.observe(wb)
            job = self.pending.get(key)
            if not job or job["fingerprint"] != fingerprint:
                return
            row = next(r for r in records(wb, "Selections") if str(r["_row"]) == key)
            job["result"] = result
            job["phase"] = "saving"
            self._persist()
            if self.excel_open():
                job["phase"] = "waiting_excel"
                self._persist()
                return
            if result.get("status") == "Search error":
                self._update(wb, revision, row, {"Lookup Status": "Search error", "Lookup Notes": "Automatic search failed. Check API credentials, quota, and connection. Retry from the app."})
                job.update(attempts=job["attempts"] + 1, result=None)
                job["phase"] = "failed" if job["attempts"] >= 3 else "retry"
                job["retry_at"] = time.time() + 60 * job["attempts"]
                self._persist()
                return
            url = result.get("url", "")
            if url and (not http_url(url) or len(url) > 4000):
                result = {"status": "Not found", "notes": "The returned product link was invalid. Review this selection manually."}
                url = ""
            updates = {"Product URL": url, "Lookup Status": result["status"],
                       "Lookup Notes": result.get("notes", ""), "Checked On": dt.date.today().isoformat()}
            if url:
                if not row["Product Name"]:
                    updates["Product Name"] = common.clean(result.get("name"))[:4000]
                image = common.clean(result.get("image"))
                updates["Image URL"] = image if http_url(image) and len(image) <= 4000 else ""
            self._update(wb, revision, row, updates)
            self.pending.pop(key)
            self._persist()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except PermissionError:
                self.message = "Excel is locking the workbook. Close it; pending updates will retry automatically."
            except ConflictError:
                self.message = "The workbook changed during lookup. Rescanning before applying any results."
            except ValidationError as error:
                self.message = f"Fix the workbook before lookup can continue: {error}"
            except Exception:
                self.message = "The workbook or lookup history is temporarily unavailable. Automatic lookup will retry."
            self._stop.wait(2)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="uh-auto-lookup", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
