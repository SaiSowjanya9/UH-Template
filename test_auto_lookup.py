import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import common
from workbook_store import WorkbookStore, put, records
from auto_lookup import AutoLookup


class AutoLookupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "master.xlsx"
        shutil.copy2(common.WORKBOOK, self.path)
        self.store = WorkbookStore(self.path)
        self.info = {"name": "brave", "ready": True, "key_name": "BRAVE_API_KEY"}
        self.search = Mock(return_value={"url": "https://example.com/new-product", "name": "Matched product", "image": "", "status": "Found - verify", "notes": ""})
        self.engine = AutoLookup(self.store, lambda: self.info, search=self.search)
        self.engine.observe(self.store.snapshot()[0])

    def tearDown(self):
        self.folder.cleanup()

    def rows(self):
        return records(self.store.snapshot()[0], "Selections")

    def change(self, row, values):
        wb, revision = self.store.snapshot()
        put(common.Sheet(wb["Selections"]), row, values)
        self.store.save(wb, revision)

    def test_initial_workbook_does_not_trigger_paid_searches(self):
        self.engine.tick()
        self.search.assert_not_called()
        self.assertFalse(self.engine.pending)

    def test_model_edit_searches_only_changed_row(self):
        before = self.rows()
        row = before[0]
        self.change(row["_row"], {"Model #": "NEW-MODEL"})
        self.engine.tick()
        self.search.assert_called_once()
        after = self.rows()
        self.assertEqual(after[0]["Product URL"], "https://example.com/new-product")
        self.assertEqual(after[1:], before[1:])
        self.assertEqual(after[0]["Lookup Status"], "Found - verify")
        self.engine.tick()
        self.assertEqual(self.search.call_count, 1)

    def test_name_only_edit_uses_new_name_and_avoids_loop(self):
        row = self.rows()[0]
        self.change(row["_row"], {"Product Name": "New product name"})
        self.engine.tick()
        self.assertEqual(self.search.call_args.kwargs["product_name"], "New product name")
        self.assertEqual(self.rows()[0]["Product Name"], "New product name")
        self.engine.tick()
        self.assertEqual(self.search.call_count, 1)

    def test_notes_and_quantity_do_not_trigger_search(self):
        self.change(self.rows()[0]["_row"], {"Client Notes": "Updated note", "Qty": "4"})
        self.engine.tick()
        self.search.assert_not_called()

    def test_verified_old_product_is_invalidated_on_excel_edit(self):
        row = self.rows()[0]["_row"]
        self.change(row, {"Product URL": "https://example.com/old-product", "Lookup Status": "Verified"})
        self.engine.observe(self.store.snapshot()[0])
        self.change(row, {"Model #": "NEW-MODEL"})
        self.engine.tick()
        self.assertEqual(self.rows()[0]["Product URL"], "https://example.com/new-product")
        self.assertNotEqual(self.rows()[0]["Lookup Status"], "Verified")

    def test_locked_excel_waits_without_searching(self):
        self.change(self.rows()[0]["_row"], {"Model #": "NEW-MODEL"})
        with patch.object(self.engine, "excel_open", return_value=True):
            self.engine.tick()
            self.assertTrue(self.engine.pending)
            self.search.assert_not_called()
        self.engine.tick()
        self.search.assert_called_once()

    def test_changes_during_search_discard_old_result(self):
        row = self.rows()[0]["_row"]
        self.change(row, {"Model #": "FIRST"})
        def search(*args, **kwargs):
            self.change(row, {"Model #": "SECOND"})
            return {"url": "https://example.com/first", "name": "First", "status": "Found - verify", "notes": ""}
        self.search.side_effect = search
        self.engine.tick()
        self.assertNotEqual(self.rows()[0]["Product URL"], "https://example.com/first")
        self.assertTrue(self.engine.pending)

    def test_manual_link_cancels_pending_lookup(self):
        row = self.rows()[0]["_row"]
        self.change(row, {"Model #": "NEW-MODEL"})
        self.engine.observe(self.store.snapshot()[0])
        self.change(row, {"Product URL": "https://example.com/manual"})
        self.engine.tick()
        self.search.assert_not_called()
        self.assertEqual(self.rows()[0]["Product URL"], "https://example.com/manual")
        self.assertFalse(self.engine.pending)

    def test_missing_key_clears_old_link_and_retains_queue(self):
        row = self.rows()[0]["_row"]
        self.change(row, {"Product URL": "https://example.com/old-product", "Lookup Status": "Verified"})
        self.engine.observe(self.store.snapshot()[0])
        self.info["ready"] = False
        self.change(row, {"Model #": "NEW-MODEL"})
        self.engine.tick()
        self.assertEqual(self.rows()[0]["Product URL"], "")
        self.assertTrue(self.engine.pending)
        self.assertIn("BRAVE_API_KEY", self.rows()[0]["Lookup Notes"])
        self.search.assert_not_called()

    def test_pending_work_survives_restart(self):
        self.change(self.rows()[0]["_row"], {"Model #": "NEW-MODEL"})
        self.engine.observe(self.store.snapshot()[0])
        restarted = AutoLookup(self.store, lambda: self.info, search=self.search)
        restarted.tick()
        self.assertEqual(self.rows()[0]["Product URL"], "https://example.com/new-product")
        self.search.assert_called_once()

    def test_inserting_blank_excel_row_does_not_recheck_unchanged_products(self):
        wb, revision = self.store.snapshot()
        wb["Selections"].insert_rows(2)
        self.store.save(wb, revision)
        self.engine.tick()
        self.search.assert_not_called()
        self.assertFalse(self.engine.pending)

    def test_result_waits_for_excel_without_searching_again(self):
        self.change(self.rows()[0]["_row"], {"Model #": "NEW-MODEL"})
        with patch.object(self.engine, "excel_open", side_effect=[False, True]):
            self.engine.tick()
        self.assertTrue(self.engine.pending)
        self.search.assert_called_once()
        self.engine.tick()
        self.search.assert_called_once()
        self.assertFalse(self.engine.pending)

    def test_provider_errors_are_retried_then_paused(self):
        self.change(self.rows()[0]["_row"], {"Model #": "NEW-MODEL"})
        self.search.return_value = {"status": "Search error", "notes": "Unavailable"}
        for _ in range(3):
            for job in self.engine.pending.values():
                job["retry_at"] = 0
            self.engine.tick()
        self.assertEqual(self.search.call_count, 3)
        self.engine.tick()
        self.assertEqual(self.search.call_count, 3)
        self.assertEqual(next(iter(self.engine.pending.values()))["phase"], "failed")
        self.engine.retry()
        self.engine.tick()
        self.assertEqual(self.search.call_count, 4)

    def test_app_save_queues_and_exports_use_new_link(self):
        import io
        import zipfile
        import app
        row = self.rows()[0]
        with patch.object(app, "store", self.store), patch.object(app, "automation", self.engine):
            client = app.app.test_client()
            headers = {"X-UH-Token": app.TOKEN}
            def post(path, data):
                revision = client.get("/api/state").json["revision"]
                return client.post(path, json={"revision": revision, **data}, headers=headers)
            saved = post("/api/selections", {"row": row["_row"], "values": {"Model #": "NEW-MODEL"}})
            self.assertEqual(saved.status_code, 200, saved.json)
            self.assertTrue(saved.json["queued"])
            blocked = post("/api/presentation", {"project": row["Project ID"], "format": "pdf", "mode": "draft"})
            self.assertEqual(blocked.status_code, 409)
            self.assertIn("awaiting automatic lookup", blocked.json["error"])
            other = next(r["Project ID"] for r in self.rows() if r["Project ID"] != row["Project ID"])
            self.assertEqual(post("/api/presentation", {"project": other, "format": "pdf", "mode": "draft"}).status_code, 200)
            self.engine.tick()
            pdf = post("/api/presentation", {"project": row["Project ID"], "format": "pdf", "mode": "draft"})
            self.assertEqual(pdf.status_code, 200)
            self.assertIn(b"https://example.com/new-product", pdf.data)
            pptx = post("/api/presentation", {"project": row["Project ID"], "format": "pptx", "mode": "draft"})
            self.assertEqual(pptx.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(pptx.data)) as archive:
                links = b"".join(archive.read(name) for name in archive.namelist() if name.endswith(".rels"))
            self.assertIn(b"https://example.com/new-product", links)

    def test_direct_excel_edit_is_hidden_before_worker_runs(self):
        import app
        row = self.rows()[0]
        self.change(row["_row"], {"Product URL": "https://example.com/old-product", "Lookup Status": "Verified"})
        self.engine.observe(self.store.snapshot()[0])
        self.change(row["_row"], {"Product Name": "Different fixture"})
        with patch.object(app, "store", self.store), patch.object(app, "automation", self.engine):
            data = app.app.test_client().get("/api/state").json
        displayed = data["selections"][0]
        self.assertEqual(displayed["Product URL"], "")
        self.assertNotEqual(displayed["Lookup Status"], "Verified")
        self.assertEqual(displayed["Product Name"], "Different fixture")
        self.assertEqual(displayed["_auto_phase"], "queued")

    def test_no_match_does_not_retain_old_link(self):
        row = self.rows()[0]["_row"]
        self.change(row, {"Product URL": "https://example.com/old-product"})
        self.engine.observe(self.store.snapshot()[0])
        self.change(row, {"Model #": "NEW-MODEL"})
        self.search.return_value = {"status": "Not found", "notes": "No confirmed match."}
        self.engine.tick()
        self.assertEqual(self.rows()[0]["Product URL"], "")
        self.assertEqual(self.rows()[0]["Lookup Status"], "Not found")
        self.assertFalse(self.engine.pending)


if __name__ == "__main__":
    unittest.main()
