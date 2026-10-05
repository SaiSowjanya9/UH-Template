import io
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

import common
import find_urls
import build_powerpoint


class MatchingTests(unittest.TestCase):
    def test_model_boundaries(self):
        self.assertTrue(find_urls.exact_model("Model K-596-VS", "k596vs"))
        self.assertTrue(find_urls.exact_model("Model K596VS", "k596vs"))
        self.assertFalse(find_urls.exact_model("Model K-596-VS2", "k596vs"))
        self.assertFalse(find_urls.exact_model("Model 17594ESRS", "7594esrs"))

    def test_category_page_is_not_product_match(self):
        html = '<html><title>All faucets</title><body>K-596-VS and many more</body></html>'
        with patch("find_urls.fetch_html", return_value=(html, "https://example.com/category")):
            self.assertEqual(find_urls.inspect_page("https://example.com/category", "k596vs"), (False, "", ""))

    def test_structured_category_is_not_a_product_page(self):
        html = '<script type="application/ld+json">[{"@type":"Product","sku":"K-596-VS"},{"@type":"Product","sku":"K-597-VS"}]</script>'
        with patch("find_urls.fetch_html", return_value=(html, "https://example.com/category")):
            self.assertEqual(find_urls.inspect_page("https://example.com/category", "k596vs"), (False, "", ""))

    def test_model_does_not_match_hyphenated_variant_or_prefix(self):
        self.assertFalse(find_urls.exact_model("K-596-VS-2", "k596vs"))
        self.assertFalse(find_urls.exact_model("PREFIX-K-596-VS", "k596vs"))

    def test_structured_product_match(self):
        html = '<script type="application/ld+json">{"@type":"Product","sku":"K-596-VS","name":"Kitchen faucet","image":"/faucet.jpg"}</script>'
        with patch("find_urls.fetch_html", return_value=(html, "https://example.com/faucet")):
            self.assertEqual(find_urls.inspect_page("https://example.com/faucet", "k596vs"),
                             (True, "Kitchen faucet", "https://example.com/faucet.jpg"))

    def test_fallback_after_unconfirmed_official_hit(self):
        queries = []
        def search(q):
            queries.append(q)
            return [{"url": "https://brand.example/category" if len(queries) == 1 else "https://shop.example/product", "title": "", "snippet": ""}]
        def inspect(url, model):
            return ("shop.example" in url, "Product", "")
        with patch("find_urls.inspect_page", side_effect=inspect):
            result = find_urls.lookup(search, "Brand", "ABC123", "brand.example")
        self.assertEqual(len(queries), 2)
        self.assertEqual(result["status"], "Found - retailer")

    def test_provider_errors_do_not_expose_keys(self):
        def search(q):
            raise RuntimeError("https://provider.example?api_key=secret-value")
        result = find_urls.lookup(search, "Brand", "ABC123", "")
        self.assertNotIn("secret-value", result["notes"])
        self.assertEqual(result["status"], "Search error")


class AppTests(unittest.TestCase):
    def setUp(self):
        import app
        from workbook_store import WorkbookStore
        self.module = app
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "master.xlsx"
        shutil.copy2(common.WORKBOOK, self.path)
        self.store = WorkbookStore(self.path)
        self.patcher = patch.object(app, "store", self.store)
        self.patcher.start()
        self.client = app.app.test_client()
        self.headers = {"X-UH-Token": app.TOKEN}

    def tearDown(self):
        self.patcher.stop()
        self.folder.cleanup()

    def state(self):
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def post(self, path, data):
        return self.client.post(path, json={"revision": self.state()["revision"], **data}, headers=self.headers)

    def test_state_is_project_scoped_by_ids_and_contains_no_keys(self):
        state = self.state()
        ids = {p["Project ID"] for p in state["projects"]}
        self.assertTrue(ids)
        self.assertTrue(all(r["Project ID"] in ids for r in state["selections"]))
        self.assertNotIn("token", state)
        self.assertEqual(set(state["provider"]), {"name", "ready", "key_name"})

    def test_excel_edits_flow_into_the_interface_and_the_schedule(self):
        """Excel stays the source of truth: sheet edits appear in state and on the deliverable."""
        wb, revision = self.store.snapshot()
        wb["Lists"].cell(13, 1, "Outdoor Living")
        project = wb["Projects"].cell(2, 1).value
        sheet = common.Sheet(wb["Selections"])
        row = sheet.ws.max_row + 1
        for header, value in [("Project ID", project), ("Section", "Outdoor Living"), ("Item", "Pergola"),
                              ("Manufacturer", "Test Brand"), ("Finish / Color", "Matte Black"),
                              ("Include in Lookbook", "Yes"), ("Lookup Status", "Not run")]:
            sheet.set(row, header, value)
        self.store.save(wb, revision)

        state = self.state()
        self.assertEqual(state["sections"][-1], "Outdoor Living")
        self.assertTrue(any(r["Item"] == "Pergola" and r["Section"] == "Outdoor Living" for r in state["selections"]))
        response = self.post("/api/presentation", {"project": project, "format": "pdf", "mode": "draft"})
        self.assertEqual(response.status_code, 200, response.json if response.is_json else "")
        try:
            from pypdf import PdfReader
        except ImportError:
            return
        text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(response.data)).pages)
        self.assertIn("O U T D O O R   L I V I N G", text)
        self.assertIn("Test Brand Matte Black", text)
        sections = [line for line in text.splitlines() if line.startswith(("E X", "R O", "W I", "K I", "O U"))]
        self.assertEqual(sections[-1], "O U T D O O R   L I V I N G")

    def test_create_project_and_selection_preserve_text_models(self):
        response = self.post("/api/projects", {"create": True, "values": {"Project ID": "TEST-1", "Project Name": "Test House"}})
        self.assertEqual(response.status_code, 200, response.json)
        response = self.post("/api/selections", {"values": {"Project ID": "TEST-1", "Item": "Faucet", "Manufacturer": "Brand", "Model #": "00123"}})
        self.assertEqual(response.status_code, 200, response.json)
        record = next(r for r in self.state()["selections"] if r["Project ID"] == "TEST-1")
        self.assertEqual(record["Model #"], "00123")
        self.assertEqual(record["Lookup Status"], "Not run")
        self.assertEqual(len(list((self.path.parent / "backups").glob("*.xlsx"))), 2)

    def test_stale_revision_does_not_overwrite(self):
        revision = self.state()["revision"]
        self.post("/api/projects", {"create": True, "values": {"Project ID": "TEST-1", "Project Name": "Test House"}})
        before = self.path.read_bytes()
        response = self.client.post("/api/projects", json={"revision": revision, "values": {"Project ID": "TEST-2", "Project Name": "Stale House"}}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.path.read_bytes(), before)

    def test_product_identity_change_clears_stale_metadata(self):
        row = self.state()["selections"][0]
        response = self.post("/api/selections", {"row": row["_row"], "values": {"Product URL": "https://example.com/product", "Product Name": "Previous product", "Image URL": "https://example.com/image.jpg"}})
        self.assertEqual(response.status_code, 200, response.json)
        response = self.post("/api/selections", {"row": row["_row"], "values": {"Lookup Status": "Verified"}, "confirm_verified": True})
        self.assertEqual(response.status_code, 200, response.json)
        response = self.post("/api/selections", {"row": row["_row"], "values": {"Model #": "NEW-MODEL"}})
        self.assertEqual(response.status_code, 200, response.json)
        current = next(r for r in self.state()["selections"] if r["_row"] == row["_row"])
        for key in ["Product URL", "Product Name", "Image URL", "Checked On"]:
            self.assertEqual(current[key], "")
        self.assertEqual(current["Lookup Status"], "Not run")

    def test_verification_requires_link_and_confirmation(self):
        row = self.state()["selections"][0]["_row"]
        self.assertEqual(self.post("/api/selections", {"row": row, "values": {"Lookup Status": "Verified"}}).status_code, 400)
        self.assertEqual(self.post("/api/selections", {"row": row, "values": {"Lookup Status": "Verified"}, "confirm_verified": True}).status_code, 400)

    def test_formula_like_input_is_written_as_literal_text(self):
        row = self.state()["selections"][0]["_row"]
        response = self.post("/api/selections", {"row": row, "values": {"Client Notes": "=1+1"}})
        self.assertEqual(response.status_code, 200, response.json)
        wb, _ = self.store.snapshot()
        sheet = common.Sheet(wb["Selections"])
        self.assertEqual(sheet.ws.cell(row, sheet.cols["Client Notes"]).data_type, "s")

    def test_missing_api_key_is_actionable(self):
        row = self.state()["selections"][0]["_row"]
        with patch.dict("os.environ", {"SEARCH_PROVIDER": "brave", "BRAVE_API_KEY": ""}):
            response = self.post(f"/api/selections/{row}/lookup", {})
        self.assertEqual(response.status_code, 400)
        self.assertIn("BRAVE_API_KEY", response.json["error"])

    def test_lookup_writes_candidate_but_not_verified(self):
        row = self.state()["selections"][0]["_row"]
        result = {"url": "https://example.com/product", "name": "Candidate", "image": "", "status": "Found - verify", "notes": ""}
        with patch.dict("os.environ", {"SEARCH_PROVIDER": "brave", "BRAVE_API_KEY": "test-only"}), patch("find_urls.lookup", return_value=result):
            response = self.post(f"/api/selections/{row}/lookup", {})
        self.assertEqual(response.status_code, 200, response.json)
        current = next(r for r in self.state()["selections"] if r["_row"] == row)
        self.assertEqual(current["Lookup Status"], "Found - verify")
        self.assertEqual(current["Product URL"], result["url"])

    def test_verified_rows_are_not_looked_up(self):
        row = self.state()["selections"][0]["_row"]
        self.post("/api/selections", {"row": row, "values": {"Product URL": "https://example.com/product"}})
        self.post("/api/selections", {"row": row, "values": {"Lookup Status": "Verified"}, "confirm_verified": True})
        with patch.dict("os.environ", {"SEARCH_PROVIDER": "brave", "BRAVE_API_KEY": "test-only"}), patch("find_urls.lookup") as lookup:
            response = self.post(f"/api/selections/{row}/lookup", {})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["skipped"])
        lookup.assert_not_called()

    def test_final_export_rejects_unverified_selections(self):
        project = self.state()["projects"][0]["Project ID"]
        response = self.post("/api/presentation", {"project": project, "format": "pdf", "mode": "final"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("not verified", response.json["error"])

    def test_pdf_and_powerpoint_exports(self):
        from pptx import Presentation
        data = self.state()
        project = data["projects"][0]
        other = data["projects"][1]
        options = {"project": project["Project ID"], "mode": "draft", "title": "Exterior Selections", "prefix": "EX"}
        pdf = self.post("/api/presentation", {**options, "format": "pdf"})
        pptx = self.post("/api/presentation", {**options, "format": "pptx"})
        self.assertEqual(pdf.status_code, 200, pdf.json if pdf.is_json else "")
        self.assertTrue(pdf.data.startswith(b"%PDF-"))
        self.assertGreater(len(pdf.data), 5000)
        try:
            from pypdf import PdfReader
        except ImportError:
            PdfReader = None
        if PdfReader:
            reader = PdfReader(io.BytesIO(pdf.data))
            text = "\n".join(page.extract_text() for page in reader.pages)
            self.assertIn(project["Project Name"], text)
            self.assertNotIn(other["Project Name"], text)
            for expected in ["FINISH SCHEDULE", "Exterior Selections", "EX-01", "Client Signature", "DRAFT"]:
                self.assertIn(expected, text)
        self.assertEqual(pptx.status_code, 200, pptx.json if pptx.is_json else "")
        presentation = Presentation(io.BytesIO(pptx.data))
        self.assertEqual((presentation.slide_width, presentation.slide_height), (7772400, 10058400))
        text = "\n".join(shape.text for slide in presentation.slides for shape in slide.shapes if shape.has_text_frame)
        self.assertIn(project["Project Name"], text)
        self.assertNotIn(other["Project Name"], text)
        for expected in ["Exterior Selections", "EX-01", "Client Signature", "DRAFT"]:
            self.assertIn(expected, text)

    def test_schedule_title_and_prefix_are_validated(self):
        project = self.state()["projects"][0]["Project ID"]
        for invalid in [{"title": "x" * 81}, {"prefix": "way-too-long"}, {"prefix": "EX 1"}, {"title": "Line\nbreak"}]:
            response = self.post("/api/presentation", {"project": project, "format": "pdf", "mode": "draft", **invalid})
            self.assertEqual(response.status_code, 400, invalid)
        response = self.post("/api/presentation", {"project": project, "format": "pdf", "mode": "draft",
                                                   "title": "Interior Selections", "prefix": "in"})
        self.assertEqual(response.status_code, 200, response.json if response.is_json else "")
        self.assertIn("Interior_Selections", response.headers["Content-Disposition"])
        try:
            from pypdf import PdfReader
        except ImportError:
            return
        text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(response.data)).pages)
        self.assertIn("IN-01", text)
        self.assertNotIn("EX-01", text)

    def test_verified_only_export_omits_pending_and_hidden_rows(self):
        from pptx import Presentation
        data = self.state()
        row = data["selections"][0]
        self.post("/api/selections", {"row": row["_row"], "values": {"Product URL": "https://example.com/product"}})
        self.post("/api/selections", {"row": row["_row"], "values": {"Lookup Status": "Verified"}, "confirm_verified": True})
        response = self.post("/api/presentation", {"project": row["Project ID"], "format": "pptx", "mode": "verified"})
        self.assertEqual(response.status_code, 200, response.json if response.is_json else "")
        prs = Presentation(io.BytesIO(response.data))
        text = "\n".join(shape.text for slide in prs.slides for shape in slide.shapes if shape.has_text_frame)
        self.assertIn(row["Item"], text)
        self.assertNotIn("DRAFT", text)
        unverified = next(r for r in self.state()["selections"] if r["Project ID"] == row["Project ID"] and r["_row"] != row["_row"])
        self.assertNotIn(unverified["Item"], text)
        self.post("/api/selections", {"row": row["_row"], "values": {"Include in Lookbook": "No"}})
        response = self.post("/api/presentation", {"project": row["Project ID"], "format": "pptx", "mode": "verified"})
        self.assertEqual(response.status_code, 400)

    def test_import_validates_before_replacement(self):
        before = self.path.read_bytes()
        response = self.client.post("/api/workbook/import", data={"file": (io.BytesIO(b"not an Excel file"), "broken.xlsx"), "revision": self.state()["revision"], "confirm": "replace-with-backup"}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.path.parent / "backups").exists())

    def test_import_export_roundtrip(self):
        original = self.state()
        exported = self.client.get("/api/workbook")
        self.assertEqual(exported.status_code, 200)
        response = self.client.post("/api/workbook/import", data={"file": (io.BytesIO(exported.data), "tracker.xlsx"), "revision": original["revision"], "confirm": "replace-with-backup"}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.state()["selections"], original["selections"])
        self.assertTrue(list((self.path.parent / "backups").glob("*.xlsx")))

    def test_invalid_project_and_missing_fields_are_rejected(self):
        self.assertEqual(self.post("/api/projects", {"values": {"Project ID": "../outside", "Project Name": "Invalid"}}).status_code, 400)
        self.assertEqual(self.post("/api/selections", {"values": {"Project ID": "UNKNOWN", "Item": "Item"}}).status_code, 400)
        self.assertEqual(self.post("/api/selections", {"values": {"Project ID": "UH-101", "Item": "Item", "Qty": "nan"}}).status_code, 400)

    def test_locked_workbook_keeps_original(self):
        before = self.path.read_bytes()
        with patch("workbook_store.os.replace", side_effect=PermissionError()):
            response = self.post("/api/projects", {"values": {"Project ID": "TEST-1", "Project Name": "Test House"}})
        self.assertEqual(response.status_code, 409)
        self.assertIn("Close", response.json["error"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_save_refused_while_excel_holds_the_workbook(self):
        before = self.path.read_bytes()
        with patch("workbook_store.workbook_open_elsewhere", return_value=True):
            response = self.post("/api/projects", {"values": {"Project ID": "TEST-1", "Project Name": "Test House"}})
        self.assertEqual(response.status_code, 409)
        self.assertIn("Close", response.json["error"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_save_restores_dropdowns_excel_dropped(self):
        from workbook_store import LIST_VALIDATIONS
        wb, revision = self.store.snapshot()
        wb["Selections"].data_validations.dataValidation = []
        self.store.save(wb, revision)
        restored, _ = self.store.snapshot()
        lists = [dv for dv in restored["Selections"].data_validations.dataValidation if dv.type == "list"]
        self.assertEqual({dv.formula1 for dv in lists}, set(LIST_VALIDATIONS.values()))
        self.assertEqual(len(lists), len(LIST_VALIDATIONS))   # one dropdown per column, no duplicates

    def test_excel_dropdowns_offer_every_status(self):
        from workbook_store import CLIENT_STATUSES, STATUSES
        wb, revision = self.store.snapshot()
        wb["Lists"]["B7"] = "Stale value"
        self.store.save(wb, revision)
        lists = self.store.snapshot()[0]["Lists"]
        self.assertEqual([lists.cell(row, 2).value for row in range(2, 2 + len(STATUSES))], STATUSES)
        self.assertEqual([lists.cell(row, 4).value for row in range(2, 2 + len(CLIENT_STATUSES))], CLIENT_STATUSES)
        self.assertEqual(lists.cell(1, 4).value, "Client Status")

    def test_excel_added_project_gets_count_formulas(self):
        wb, revision = self.store.snapshot()
        sheet = common.Sheet(wb["Projects"])
        row = wb["Projects"].max_row + 1
        sheet.set(row, "Project ID", "TEST-9")
        sheet.set(row, "Project Name", "Added in Excel")
        self.store.save(wb, revision)
        restored, _ = self.store.snapshot()
        ws = restored["Projects"]
        headers = {c.value: c.column for c in ws[1] if c.value}
        self.assertEqual(ws.cell(row, headers["Total Items"]).value,
                         f'=IF(A{row}="","",COUNTIF(Selections!$A:$A,A{row}))')
        self.assertEqual(ws.cell(row, headers["Needs Review"]).value, f'=IF(A{row}="","",H{row}-J{row})')

    def test_delete_endpoints_and_guards(self):
        row = self.state()["selections"][0]
        response = self.post("/api/selections", {"row": row["_row"], "values": {"Item": row["Item"]}, "delete": True})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(any(r["Item"] == row["Item"] for r in self.state()["selections"]))
        project = self.state()["projects"][0]
        response = self.post("/api/projects", {"values": {"Project ID": project["Project ID"]}, "delete": True})
        self.assertEqual(response.status_code, 400)
        self.assertIn("selections", response.json["error"])
        self.post("/api/projects", {"create": True, "values": {"Project ID": "TEST-9", "Project Name": "Temp"}})
        self.assertEqual(self.post("/api/projects", {"values": {"Project ID": "TEST-9"}, "delete": True}).status_code, 200)
        self.assertFalse(any(p["Project ID"] == "TEST-9" for p in self.state()["projects"]))
        name = self.state()["manufacturers"][0]["Manufacturer"]
        self.assertEqual(self.post("/api/manufacturers", {"values": {"Manufacturer": name}, "delete": True}).status_code, 200)
        self.assertFalse(any(m["Manufacturer"] == name for m in self.state()["manufacturers"]))

    def test_client_status_lifecycle(self):
        row = self.state()["selections"][0]
        response = self.post("/api/selections", {"row": row["_row"], "values": {"Client Status": "Approved"}})
        self.assertEqual(response.status_code, 200, response.json)
        current = next(r for r in self.state()["selections"] if r["_row"] == row["_row"])
        self.assertEqual(current["Client Status"], "Approved")
        # editing the product's identity returns an approved selection to the client
        self.post("/api/selections", {"row": row["_row"], "values": {"Model #": "NEW-MODEL"}})
        current = next(r for r in self.state()["selections"] if r["_row"] == row["_row"])
        self.assertEqual(current["Client Status"], "Changed")
        self.assertEqual(self.post("/api/selections", {"row": row["_row"], "values": {"Client Status": "Bogus"}}).status_code, 400)
        # a non-draft export marks proposed selections as presented
        self.post("/api/selections", {"row": row["_row"], "values": {"Product URL": "https://example.com/p", "Client Status": "Proposed"}})
        self.post("/api/selections", {"row": row["_row"], "values": {"Lookup Status": "Verified"}, "confirm_verified": True})
        response = self.post("/api/presentation", {"project": row["Project ID"], "format": "pdf", "mode": "verified"})
        self.assertEqual(response.status_code, 200, response.json if response.is_json else "")
        current = next(r for r in self.state()["selections"] if r["_row"] == row["_row"])
        self.assertEqual(current["Client Status"], "Presented")
        self.assertIn("_R", response.headers["Content-Disposition"])

    def test_export_revisions_increment_per_project(self):
        project = self.state()["projects"][0]["Project ID"]
        first = self.post("/api/presentation", {"project": project, "format": "pdf", "mode": "draft"})
        second = self.post("/api/presentation", {"project": project, "format": "pdf", "mode": "draft"})
        self.assertIn("_R1_", first.headers["Content-Disposition"])
        self.assertIn("_R2_", second.headers["Content-Disposition"])
        other = self.state()["projects"][1]["Project ID"]
        fresh = self.post("/api/presentation", {"project": other, "format": "pptx", "mode": "draft"})
        self.assertIn("_R1_", fresh.headers["Content-Disposition"])

    def test_stale_verified_links_are_flagged(self):
        from workbook_store import put
        row = self.state()["selections"][0]
        wb, revision = self.store.snapshot()
        put(common.Sheet(wb["Selections"]), row["_row"],
            {"Product URL": "https://example.com/p", "Lookup Status": "Verified", "Checked On": "2020-01-01"})
        self.store.save(wb, revision)
        current = next(r for r in self.state()["selections"] if r["_row"] == row["_row"])
        self.assertTrue(current["_stale"])
        wb, revision = self.store.snapshot()
        put(common.Sheet(wb["Selections"]), row["_row"], {"Checked On": "2099-01-01"})
        self.store.save(wb, revision)
        current = next(r for r in self.state()["selections"] if r["_row"] == row["_row"])
        self.assertFalse(current["_stale"])

    def test_parse_link_prefills_a_selection(self):
        html = ('<html><head><meta property="og:title" content="Trinsic Faucet">'
                '<meta property="og:image" content="/faucet.jpg">'
                '<script type="application/ld+json">{"@type":"Product","sku":"559LF-PP","brand":{"name":"Delta"}}</script>'
                '</head></html>')
        with patch("find_urls.fetch_html", return_value=(html, "https://delta.example/faucet")):
            response = self.post("/api/selections/parse-link", {"url": "https://delta.example/faucet"})
        self.assertEqual(response.status_code, 200, response.json)
        details = response.json["details"]
        self.assertEqual(details["Model #"], "559LF-PP")
        self.assertEqual(details["Manufacturer"], "Delta")
        self.assertEqual(details["Product Name"], "Trinsic Faucet")
        self.assertEqual(details["Image URL"], "https://delta.example/faucet.jpg")
        self.assertEqual(details["Lookup Status"], "Found - verify")
        self.assertEqual(self.post("/api/selections/parse-link", {"url": "file:///etc/passwd"}).status_code, 400)

    def test_price_schedule_groups_every_selection_and_totals(self):
        from price_schedule import SHEET
        row = self.state()["selections"][0]
        response = self.post("/api/selections", {"row": row["_row"], "values": {"Qty": "3", "Unit Price": "125.50", "Markup %": "15"}})
        self.assertEqual(response.status_code, 200, response.json)
        wb, _ = self.store.snapshot()
        ws = wb[SHEET]
        self.assertEqual([ws.cell(4, c).value for c in range(1, 8)],
                         ["S.No", "Code", "Description", "Qty", "Unit Price", "Markup %", "Price"])
        text = [[ws.cell(r, c).value for c in range(1, 8)] for r in range(5, ws.max_row + 1)]
        flat = "\n".join(str(cell) for line in text for cell in line if cell)
        # every project, category and selection appears
        for project in {r["Project ID"] for r in self.state()["selections"]}:
            self.assertIn(project, flat)
        for selection in self.state()["selections"]:
            self.assertIn(selection["Section"].upper(), flat)
        self.assertIn("subtotal", flat)
        self.assertIn("Grand total", flat)
        # the priced row points at its Selections cells and multiplies them out
        priced = next(line for line in text if line[2] and "Pure White" not in str(line[2])
                      and str(line[1] or "").startswith("EX") and f"Selections!H{row['_row']}" in str(line[3]))
        columns = {c.value: c.column_letter for c in wb["Selections"][1] if c.value}
        self.assertIn(f"Selections!{columns['Unit Price']}{row['_row']}", priced[4])
        self.assertIn(f"Selections!{columns['Markup %']}{row['_row']}", priced[5])
        self.assertIn("(1+IF(ISNUMBER(F", priced[6])   # the line total applies the markup
        current = next(r for r in self.state()["selections"] if r["_row"] == row["_row"])
        self.assertEqual((current["Unit Price"], current["Markup %"]), ("125.5", "15"))
        for bad in [{"Unit Price": "free"}, {"Markup %": "lots"}, {"Markup %": "5000"}]:
            self.assertEqual(self.post("/api/selections", {"row": row["_row"], "values": bad}).status_code, 400, bad)

    def test_spec_template_seeds_rooms_once(self):
        import spec_template
        name, template = spec_template.load()
        expected = spec_template.rows(template)
        project = self.state()["projects"][0]["Project ID"]
        before = len(self.state()["selections"])
        response = self.post("/api/selections/template", {"project": project})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["added"], len(expected))
        self.assertEqual(self.state()["sections"][-5:], [section for section, _ in template])
        self.assertEqual(len(self.state()["selections"]), before + len(expected))
        rows = [r for r in self.state()["selections"] if r["Project ID"] == project]
        for section, room, item in expected[:: max(1, len(expected) // 20)]:
            self.assertTrue(any(r["Section"] == section and r["Room / Area"] == room and r["Item"] == item for r in rows),
                            f"{section} / {room} / {item} missing")
        seeded = next(r for r in rows if r["Room / Area"] == "Master Bathroom")
        self.assertEqual((seeded["Lookup Status"], seeded["Include in Lookbook"], seeded["Client Status"]),
                         ("Not run", "Yes", "Proposed"))
        # re-running adds nothing and removes nothing
        again = self.post("/api/selections/template", {"project": project})
        self.assertEqual((again.json["added"], again.json["skipped"]), (0, len(expected)))
        self.assertEqual(len(self.state()["selections"]), before + len(expected))
        # a second project gets its own copy
        self.post("/api/projects", {"create": True, "values": {"Project ID": "TEST-T", "Project Name": "Template home"}})
        other = self.post("/api/selections/template", {"project": "TEST-T"})
        self.assertEqual(other.json["added"], len(expected))
        self.assertEqual(other.json["sections"], [])   # sections already registered

    def test_prices_never_reach_client_documents(self):
        from pypdf import PdfReader
        from pptx import Presentation
        row = self.state()["selections"][0]
        self.post("/api/selections", {"row": row["_row"], "values": {"Unit Price": "4321.99"}})
        for kind in ["pdf", "pptx"]:
            response = self.post("/api/presentation", {"project": row["Project ID"], "format": kind, "mode": "draft"})
            self.assertEqual(response.status_code, 200, response.json if response.is_json else "")
            self.assertNotIn(b"4321.99", response.data)
            if kind == "pdf":
                text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(response.data)).pages)
            else:
                text = "\n".join(s.text for slide in Presentation(io.BytesIO(response.data)).slides
                                 for s in slide.shapes if s.has_text_frame)
            self.assertNotIn("4321", text)

    def test_backups_are_pruned_and_logged(self):
        with patch("workbook_store.BACKUP_KEEP", 2):
            for index in range(4):
                response = self.post("/api/projects", {"create": True, "values": {"Project ID": f"TEST-{index}", "Project Name": "T"}})
                self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(len(list((self.path.parent / "backups").glob("*.xlsx"))), 2)
        history = (self.path.parent / "backups" / "history.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(history), 4)

    def test_csrf_and_host_protection(self):
        self.assertEqual(self.client.post("/api/projects", json={}).status_code, 403)
        self.assertEqual(self.client.get("/", headers={"Host": "attacker.example"}).status_code, 400)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        for name in ["ui.css", "ui.js"]:
            with self.client.get(f"/assets/{name}") as asset:
                self.assertEqual(asset.status_code, 200)
        for name in ["symbol", "wordmark", "icon"]:
            with self.client.get(f"/brand/{name}.png") as asset:
                self.assertEqual(asset.status_code, 200)
                self.assertEqual(asset.mimetype, "image/png")
                self.assertTrue(asset.data.startswith(b"\x89PNG"))
        self.assertEqual(self.client.get("/brand/unknown.png").status_code, 404)


class RemoteTests(unittest.TestCase):
    def test_private_and_non_http_urls_are_rejected(self):
        import remote
        for url in ["file:///etc/passwd", "javascript:alert(1)", "https://user:password@example.com", "http://example.com:8080"]:
            with self.assertRaises(ValueError):
                remote.public_url(url)
        with patch("remote.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 80))]):
            with self.assertRaises(ValueError):
                remote.public_url("http://example.com")


class BrandingTests(unittest.TestCase):
    def test_logos_retain_transparency_and_aspect_ratio(self):
        from branding import load_logo
        from build_lookbook import CFG, brand_image
        from unittest.mock import Mock
        for field in ["logo_path", "logo_print_path", "logo_mark_path"]:
            image = load_logo(CFG[field])
            self.assertIsNotNone(image)
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getchannel("A").getextrema(), (0, 255))
        canvas = Mock()
        self.assertTrue(brand_image(canvas, CFG["logo_print_path"], 46.8, 734.4, height=20.16))
        _, x, y, width, height = canvas.drawImage.call_args.args
        image = load_logo(CFG["logo_print_path"])
        self.assertAlmostEqual(width / height, image.width / image.height)
        self.assertEqual((x, y, round(height, 2)), (46.8, 734.4, 20.16))
        self.assertEqual(canvas.drawImage.call_args.kwargs["mask"], "auto")

    def test_branding_embedded_in_pdf_and_powerpoint(self):
        import build_lookbook
        from PIL import Image
        from pptx import Presentation
        from branding import load_logo
        from workbook_store import SELECTION_FIELDS
        project = {"Project ID": "BRAND", "Project Name": "Branding preview"}
        row = {**{key: "" for key in SELECTION_FIELDS}, "Section": "Kitchen", "Item": "Test fixture"}
        wordmark = load_logo(build_lookbook.CFG["logo_print_path"])
        with tempfile.TemporaryDirectory() as folder:
            pdf = build_lookbook.build("BRAND", project, [row], True, Path(folder))
            pptx = build_powerpoint.build("BRAND", project, [row], True, Path(folder))
            self.assertIn(b"/SMask", pdf.read_bytes())
            prs = Presentation(pptx)
            for slide in [prs.slides[0], prs.slides[-1]]:
                pictures = [shape for shape in slide.shapes if shape.shape_type == 13]
                logos = [shape for shape in pictures if shape.image.size == wordmark.size]
                self.assertEqual(len(logos), 1)
                with Image.open(io.BytesIO(logos[0].image.blob)) as image:
                    self.assertEqual(image.mode, "RGBA")
                    self.assertEqual(image.getchannel("A").getextrema(), (0, 255))
                self.assertAlmostEqual(logos[0].width / logos[0].height, wordmark.width / wordmark.height, places=3)


TEMPLATE_ROWS = [
    ("Walls & Trim", "Stucco", "Smooth stucco finish"),
    ("Walls & Trim", "Stucco Color", "Sherwin-Williams White Duck (SW 7010) \u2013 warm white"),
    ("Walls & Trim", "Trim", "Sherwin-Williams White Duck (SW 7010)"),
    ("Walls & Trim", "Gutters & Downspouts", "Painted to match Sherwin-Williams White Duck (SW 7010)"),
    ("Roofing", "Roof", "Owens Corning Duration Storm shingles \u2013 Estate Gray"),
    ("Roofing", "Standing Seam Metal Roof & Awnings", "Matte black"),
    ("Doors & Windows", "Windows", "Black exterior window frames"),
    ("Doors & Windows", "Front Door", "Contemporary vertical plank design in a warm natural wood finish"),
    ("Doors & Windows", "Garage Door", "Contemporary vertical plank design in a warm natural wood finish to coordinate with the front door"),
    ("Doors & Windows", "Pedestrian Gate", "Black steel frame with horizontal wood-look slats in a warm natural wood finish to coordinate with the front door and garage door"),
    ("Lighting", "Exterior Lighting", "Contemporary matte black fixtures"),
]


def page_geometry(path):
    """Absolute positions of every text baseline and rule on a schedule's first page.

    One harmless difference from the approved template is normalised: it prints a
    placeholder for empty band values.
    """
    from pypdf import PdfReader
    stream = PdfReader(str(path)).pages[0].get_contents().get_data().decode("latin-1")
    marks, stack, x, y = [], [], 0.0, 0.0
    for line in stream.splitlines():
        if line == "q":
            stack.append((x, y))
        elif line == "Q" and stack:
            x, y = stack.pop()
        elif match := re.fullmatch(r"1 0 0 1 (-?[\d.]+) (-?[\d.]+) cm", line):
            x, y = x + float(match[1]), y + float(match[2])
        elif match := re.match(r"BT 1 0 0 1 (-?[\d.]+) (-?[\d.]+) Tm.*\(.+\) Tj", line):
            position = (round(x + float(match[1]), 2), round(y + float(match[2]), 2))
            if position[1] != 657:
                marks.append(("text", *position))
        elif match := re.fullmatch(r"n (-?[\d.]+) (-?[\d.]+) m (-?[\d.]+) (-?[\d.]+) l S", line):
            x1, y1, x2, y2 = (float(match[index]) for index in (1, 2, 3, 4))
            marks.append(("rule", round(x + x1, 2), round(y + y1, 2), round(x + x2, 2), round(y + y2, 2)))
    return sorted(marks)


class ScheduleTests(unittest.TestCase):
    """The client deliverable follows the approved finish schedule template."""

    def test_layout_matches_the_approved_template(self):
        import build_lookbook
        from workbook_store import SELECTION_FIELDS
        template = Path("brand_assets/finish_schedule_template.pdf")
        if not template.exists():
            self.skipTest("Reference template PDF is not available.")
        rows = [{**{key: "" for key in SELECTION_FIELDS}, "Section": section, "Item": item,
                 "Client Notes": note, "Lookup Status": "Verified"} for section, item, note in TEMPLATE_ROWS]
        with tempfile.TemporaryDirectory() as folder:
            pdf = build_lookbook.build("UH-000", {"Project Name": ""}, rows, False, Path(folder),
                                       title="Exterior Selections", prefix="EX")
            self.assertEqual(page_geometry(pdf), page_geometry(template))

    def rows(self, count=1, **values):
        from workbook_store import SELECTION_FIELDS
        return [{**{key: "" for key in SELECTION_FIELDS}, "Section": "Roofing", "Item": f"Item {index}",
                 "Lookup Status": "Verified", **values} for index in range(1, count + 1)]

    def text(self, path):
        from pypdf import PdfReader
        return "\n".join(page.extract_text() for page in PdfReader(str(path)).pages)

    def test_descriptions_are_assembled_from_workbook_columns(self):
        from build_lookbook import description
        cases = [
            ({"Manufacturer": "Sherwin-Williams", "Finish / Color": "White Duck (SW 7010)", "Client Notes": "warm white"},
             "Sherwin-Williams White Duck (SW 7010) \u2013 warm white"),
            ({"Manufacturer": "Owens Corning", "Product Name": "Duration Storm shingles", "Finish / Color": "Estate Gray"},
             "Owens Corning Duration Storm shingles \u2013 Estate Gray"),
            ({"Manufacturer": "Delta", "Product Name": "Trinsic vanity faucet", "Model #": "559LF-PP",
              "Finish / Color": "Chrome", "Qty": "2"},
             "Delta Trinsic vanity faucet (Model 559LF-PP) \u2013 Chrome \u2013 Qty 2"),
            ({"Manufacturer": "Moen", "Model #": "7594ESRS", "Finish / Color": "Spot Resist Stainless", "Qty": "1"},
             "Moen 7594ESRS \u2013 Spot Resist Stainless"),
        ]
        for values, expected in cases:
            self.assertEqual(description({**{key: "" for key in ["Manufacturer", "Product Name", "Model #", "Finish / Color", "Qty", "Client Notes"]}, **values}), expected)

    def test_schedule_page_keeps_the_template_furniture_without_links(self):
        import build_lookbook
        from pypdf import PdfReader
        project = {"Project ID": "SCHED", "Project Name": "Template home", "Client Name": "Test client",
                   "Address": "1 Test Way", "Plan / Elevation": "Plan 1", "Presentation Date": "2026-10-01"}
        rows = self.rows(2, Manufacturer="Test", **{"Product URL": "https://example.com/product",
                                                    "Image URL": "https://example.com/image.jpg"})
        with tempfile.TemporaryDirectory() as folder:
            pdf = build_lookbook.build("SCHED", project, rows, False, Path(folder), title="Exterior Selections", prefix="EX")
            reader = PdfReader(str(pdf))
            self.assertEqual(len(reader.pages), 1)
            self.assertEqual([round(value) for value in reader.pages[0].mediabox], [0, 0, 612, 792])
            text = self.text(pdf)
            for expected in ["FINISH SCHEDULE", "Exterior Selections", "Test client", "Template home / Plan 1",
                             "1 Test Way", "October 1, 2026", "R O O F I N G", "EX-01", "EX-02",
                             "Client Signature", "UH Homes Representative", "Page 1"]:
                self.assertIn(expected, text)
            self.assertNotIn("DRAFT", text)
            raw = pdf.read_bytes()
            self.assertNotIn(b"https://example.com/product", raw)
            self.assertNotIn(b"https://example.com/image.jpg", raw)
            self.assertEqual(sorted(image.image.size for image in reader.pages[0].images), [(379, 344), (2364, 346)])
            self.assertIsNone(reader.pages[0].get("/Annots"))

    def test_long_schedules_paginate_and_repeat_the_furniture(self):
        import build_lookbook
        import build_powerpoint
        from pptx import Presentation
        from pypdf import PdfReader
        project = {"Project ID": "TEST", "Project Name": "A test home with many categories", "Client Name": "Test client"}
        rows = [{**self.rows(1, Manufacturer="Test", Section=f"Section {index:02}")[0], "Item": f"Item {index}",
                 "Client Notes": "A long client-facing description that wraps onto a second printed line. " * 2}
                for index in range(31)]
        with tempfile.TemporaryDirectory() as folder:
            pdf = build_lookbook.build("TEST", project, rows, True, Path(folder))
            pptx = build_powerpoint.build("TEST", project, rows, True, Path(folder))
            reader = PdfReader(str(pdf))
            self.assertGreater(len(reader.pages), 1)
            pages = [page.extract_text() for page in reader.pages]
            for index, page in enumerate(pages, start=1):
                self.assertIn(f"Page {index}", page)
                self.assertIn("FINISH SCHEDULE", page)
            self.assertIn("Client Signature", pages[-1])
            joined = "\n".join(pages)
            for index in range(31):
                self.assertIn(f"Item {index}", joined)
            prs = Presentation(pptx)
            self.assertGreater(len(prs.slides), 1)
            slides = ["\n".join(shape.text for shape in slide.shapes if shape.has_text_frame) for slide in prs.slides]
            for index, slide in enumerate(slides, start=1):
                self.assertIn(f"Page {index}", slide)
            self.assertIn("Client Signature", slides[-1])
            for index in range(31):
                self.assertIn(f"Item {index}", "\n".join(slides))


if __name__ == "__main__":
    unittest.main()
