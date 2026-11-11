import io
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

import common
import build_powerpoint


def docx_text(raw):
    """Every run of text in a Word document: body, tables and the repeating furniture."""
    from docx import Document
    document = Document(io.BytesIO(raw) if isinstance(raw, bytes) else raw)
    parts = [paragraph.text for paragraph in document.paragraphs]
    parts += [cell.text for table in document.tables for row in table.rows for cell in row.cells]
    parts += [paragraph.text for section in document.sections
              for story in (section.header, section.footer) for paragraph in story.paragraphs]
    return "\n".join(parts)


class AppTests(unittest.TestCase):
    def setUp(self):
        import app
        from workspace_store import WorkspaceStore
        self.module = app
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "master.xlsx"
        shutil.copy2(common.WORKSPACE, self.path)
        self.store = WorkspaceStore(self.path)
        self.patcher = patch.object(app, "store", self.store)
        self.patcher.start()
        # These tests exercise the local-mode contract (the per-start TOKEN);
        # REQUIRE_LOGIN depends on the ambient .env, so pin it off.
        self.login_patcher = patch.object(app, "REQUIRE_LOGIN", False)
        self.login_patcher.start()
        self.client = app.app.test_client()
        self.headers = {"X-UH-Token": app.TOKEN}

    def tearDown(self):
        self.patcher.stop()
        self.login_patcher.stop()
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

    def test_excel_edits_flow_into_the_interface_and_the_schedule(self):
        """Excel stays the source of truth: sheet edits appear in state and on the deliverable."""
        from workspace_store import ensure_location_list, location_list
        wb, revision = self.store.snapshot()
        column = ensure_location_list(wb)
        lists = wb["Lists"]
        _, existing = location_list(lists)
        lists.cell(len(existing) + 2, column, "Specialty Finishes")   # a new location, last in print order
        project = wb["Projects"].cell(2, 1).value
        sheet = common.Sheet(wb["Selections"])
        row = sheet.ws.max_row + 1
        for header, value in [("Project ID", project), ("Section", "Specialty Finishes"), ("Item", "Pergola"),
                              ("Manufacturer", "Test Brand"), ("Finish / Color", "Matte Black"),
                              ("Include in Lookbook", "Yes")]:
            sheet.set(row, header, value)
        self.store.save(wb, revision)

        state = self.state()
        self.assertEqual(state["locations"][-1], "Specialty Finishes")
        # a location name entered as a Section migrates onto Location, with the category re-inferred
        record = next(r for r in state["selections"] if r["Item"] == "Pergola")
        self.assertEqual(record["Location"], "Specialty Finishes")
        self.assertEqual(record["Section"], "Other")
        import build_lookbook
        from workspace_store import detailed_records
        wb, _ = self.store.snapshot()
        rows = [r for r in detailed_records(wb, "Selections") if r["Project ID"] == project]
        common.schedule_sort(rows, wb)
        project_record = next(p for p in detailed_records(wb, "Projects") if p["Project ID"] == project)
        output = build_lookbook.build(project, project_record, rows, True, Path(self.folder.name))
        try:
            from pypdf import PdfReader
        except ImportError:
            return
        text = "\n".join(page.extract_text() for page in PdfReader(str(output)).pages)
        self.assertIn("S P E C I A L T Y   F I N I S H E S", text)
        self.assertIn("Test Brand Matte Black", text)
        # headings follow the order of the Lists sheet's Locations, whatever the project uses
        spaced = {location: "   ".join(" ".join(word) for word in location.upper().split()) for location in state["locations"]}
        found = [(text.index(heading), location) for location, heading in spaced.items() if heading in text]
        self.assertEqual([location for _, location in sorted(found)],
                         [location for location in state["locations"] if spaced[location] in text])
        self.assertEqual(sorted(found)[-1][1], "Specialty Finishes")

    def test_create_project_and_selection_preserve_text_models(self):
        response = self.post("/api/projects", {"create": True, "values": {"Project ID": "TEST-1", "Project Name": "Test House"}})
        self.assertEqual(response.status_code, 200, response.json)
        response = self.post("/api/selections", {"values": {"Project ID": "TEST-1", "Item": "Faucet", "Manufacturer": "Brand", "Model #": "00123"}})
        self.assertEqual(response.status_code, 200, response.json)
        record = next(r for r in self.state()["selections"] if r["Project ID"] == "TEST-1" and r["Item"] == "Faucet")
        self.assertEqual(record["Model #"], "00123")
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
        response = self.post("/api/selections", {"row": row["_row"], "values": {"Model #": "NEW-MODEL"}})
        self.assertEqual(response.status_code, 200, response.json)
        current = next(r for r in self.state()["selections"] if r["_row"] == row["_row"])
        for key in ["Product URL", "Product Name", "Image URL"]:
            self.assertEqual(current[key], "")

    def test_retired_lookup_fields_are_refused(self):
        """The columns product lookup owned are gone, so a client cannot write them back."""
        from workspace_store import RETIRED_SELECTION_FIELDS
        row = self.state()["selections"][0]
        for field in RETIRED_SELECTION_FIELDS:
            response = self.post("/api/selections", {"row": row["_row"], "values": {field: "Verified"}})
            self.assertEqual(response.status_code, 400, field)

    def test_retired_lookup_columns_are_dropped_on_save(self):
        """A workspace written before product lookup was removed loses those columns, keeping its data."""
        from workspace_store import RETIRED_PROJECT_FIELDS, RETIRED_SELECTION_FIELDS, records
        before = self.store.snapshot()[0]
        self.assertTrue({c.value for c in before["Selections"][1]} & set(RETIRED_SELECTION_FIELDS),
                        "fixture workspace no longer carries the retired columns")
        kept = [(r["_row"], r["Item"], r["Client Notes"]) for r in records(before, "Selections")]
        wb, revision = self.store.snapshot()
        self.store.save(wb, revision)
        after, _ = self.store.snapshot()
        for sheet, retired in (("Selections", RETIRED_SELECTION_FIELDS), ("Projects", RETIRED_PROJECT_FIELDS)):
            headers = {c.value for c in after[sheet][1]}
            self.assertFalse(headers & set(retired), sheet)
        self.assertEqual([(r["_row"], r["Item"], r["Client Notes"]) for r in records(after, "Selections")], kept)
        self.assertIsNone(after["Lists"]["B1"].value)
        # The dropdowns that outlived lookup are rebuilt against the shifted columns.
        sheet = common.Sheet(after["Selections"])
        for dv in after["Selections"].data_validations.dataValidation:
            column = {r.min_col for r in dv.sqref.ranges}
            self.assertTrue(column <= set(sheet.cols.values()), dv.formula1)

    def test_product_link_column_mirrors_the_saved_url(self):
        """Excel shows the words 'Product Link', hyperlinked, instead of a raw address."""
        row = self.state()["selections"][0]
        self.post("/api/selections", {"row": row["_row"], "values": {"Product URL": "https://example.com/p"}})

        def link_cell():
            sheet = common.Sheet(load_workbook(self.path)["Selections"])
            return sheet.ws.cell(row["_row"], sheet.cols["Product Link"])

        self.assertEqual(link_cell().value, "Product Link")
        self.assertEqual(link_cell().hyperlink.target, "https://example.com/p")
        # the column is generated, so it is not offered back as an editable field
        self.assertNotIn("Product Link", self.state()["selections"][0])
        self.post("/api/selections", {"row": row["_row"], "values": {"Product URL": ""}})
        self.assertIsNone(link_cell().value)
        self.assertIsNone(link_cell().hyperlink)

    def test_formula_like_input_is_written_as_literal_text(self):
        row = self.state()["selections"][0]["_row"]
        response = self.post("/api/selections", {"row": row, "values": {"Client Notes": "=1+1"}})
        self.assertEqual(response.status_code, 200, response.json)
        wb, _ = self.store.snapshot()
        sheet = common.Sheet(wb["Selections"])
        self.assertEqual(sheet.ws.cell(row, sheet.cols["Client Notes"]).data_type, "s")

    def test_hidden_columns_are_left_out_of_every_export(self):
        """Columns chosen before an export are absent from the spec sheet downloads."""
        import csv as csvlib
        from pypdf import PdfReader
        row = next(r for r in self.state()["selections"] if r["Manufacturer"] and r["Finish / Color"])
        self.post("/api/selections", {"row": row["_row"], "values": {"Qty": "4", "Unit Price": "1234.5", "Location": "1st Floor"}})
        row = next(r for r in self.state()["selections"] if r["_row"] == row["_row"])
        project = row["Project ID"]
        hidden = ["code", "Section", "Location", "Manufacturer", "Qty", "Unit Price"]

        word_sheet = self.client.get(f"/api/selections/docx?project={project}&hide={','.join(hidden)}")
        self.assertEqual(word_sheet.status_code, 200)
        text = docx_text(word_sheet.data)
        self.assertNotIn("MANUFACTURER", text)
        self.assertNotIn("UNIT PRICE", text)
        self.assertNotIn("1234.5", text)
        self.assertIn("ITEM", text)
        self.assertEqual(self.client.get(f"/api/selections/docx?project={project}&hide=Nonsense").status_code, 400)

        pdf_sheet = self.client.get(f"/api/selections/pdf?project={project}&hide={','.join(hidden)}")
        self.assertEqual(pdf_sheet.status_code, 200)
        text = " ".join(" ".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf_sheet.data)).pages).split())
        self.assertNotIn("MANUFACTURER", text)
        self.assertNotIn("UNIT PRICE", text)
        self.assertNotIn("1234.5", text)
        self.assertIn("ITEM", text)
        self.assertEqual(self.client.get(f"/api/selections/pdf?project={project}&hide=Nonsense").status_code, 400)

        sheet = self.client.get(f"/api/selections/csv?project={project}&hide={','.join(hidden)}")
        self.assertEqual(sheet.status_code, 200)
        lines = list(csvlib.DictReader(io.StringIO(sheet.data.decode("utf-8-sig"))))
        self.assertNotIn("Manufacturer", lines[0])
        self.assertNotIn("Unit Price", lines[0])
        self.assertNotIn("Section", lines[0])
        self.assertIn("Item", lines[0])
        self.assertNotIn(b"1234.5", sheet.data)

        # hiding nothing is the default, and an unknown column is refused rather than ignored
        everything = self.client.get(f"/api/selections/csv?project={project}")
        self.assertIn("Manufacturer", csvlib.DictReader(io.StringIO(everything.data.decode("utf-8-sig"))).fieldnames)
        self.assertEqual(self.client.get(f"/api/selections/csv?project={project}&hide=Nonsense").status_code, 400)

    def test_the_download_chooser_offers_exactly_what_an_export_can_drop(self):
        """The toggles in the Import & export dialog are build_lookbook's one registry, so the
        app cannot offer a column the exports would then refuse to leave out."""
        import csv as csvlib
        from urllib.parse import quote
        from build_lookbook import HIDEABLE_COLUMNS
        offered = self.state()["export_columns"]
        self.assertEqual([column["key"] for column in offered],
                         [key for key, _, families in HIDEABLE_COLUMNS if "sheet" in families])
        self.assertTrue(all(column["label"] for column in offered))
        # the three a returning CSV is matched on are flagged, so the dialog can say so
        self.assertEqual({column["key"] for column in offered if column["matches"]},
                         set(self.module.SPEC_MATCH_FIELDS))

        project = self.state()["selections"][0]["Project ID"]
        everything = ",".join(column["key"] for column in offered)
        # Encoded, as the page sends them: 'Model #' and 'Markup %' are a fragment and an
        # escape to a URL, and arrive as unknown columns if they ever go out raw.
        for column in offered:
            response = self.client.get(f"/api/selections/csv?project={project}&hide={quote(column['key'])}")
            self.assertEqual(response.status_code, 200, column["key"])
        for name in ["csv", "docx", "pdf"]:   # and hiding every column at once is a legitimate ask
            response = self.client.get(f"/api/selections/{name}?project={project}&hide={quote(everything)}")
            self.assertEqual(response.status_code, 200, name)
        bare = self.client.get(f"/api/selections/csv?project={project}&hide={quote(everything)}")
        self.assertEqual(next(csvlib.reader(io.StringIO(bare.data.decode("utf-8-sig")))), [])

    def test_invalid_project_and_missing_fields_are_rejected(self):
        self.assertEqual(self.post("/api/projects", {"values": {"Project ID": "../outside", "Project Name": "Invalid"}}).status_code, 400)
        self.assertEqual(self.post("/api/selections", {"values": {"Project ID": "UNKNOWN", "Item": "Item"}}).status_code, 400)
        self.assertEqual(self.post("/api/selections", {"values": {"Project ID": "UH-101", "Item": "Item", "Qty": "nan"}}).status_code, 400)

    def test_locked_workspace_keeps_original(self):
        before = self.path.read_bytes()
        with patch("workspace_store.os.replace", side_effect=PermissionError()):
            response = self.post("/api/projects", {"values": {"Project ID": "TEST-1", "Project Name": "Test House"}})
        self.assertEqual(response.status_code, 409)
        self.assertIn("Close", response.json["error"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_save_refused_while_excel_holds_the_workspace(self):
        before = self.path.read_bytes()
        with patch("workspace_store.workspace_open_elsewhere", return_value=True):
            response = self.post("/api/projects", {"values": {"Project ID": "TEST-1", "Project Name": "Test House"}})
        self.assertEqual(response.status_code, 409)
        self.assertIn("Close", response.json["error"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_save_restores_dropdowns_excel_dropped(self):
        from openpyxl.utils import get_column_letter
        from workspace_store import LIST_VALIDATIONS, MAX_UNITS, unit_list
        wb, revision = self.store.snapshot()
        wb["Selections"].data_validations.dataValidation = []
        self.store.save(wb, revision)
        restored, _ = self.store.snapshot()
        lists = [dv for dv in restored["Selections"].data_validations.dataValidation if dv.type == "list"]
        # Units was appended to Lists instead of being given a letter, so its range follows
        # the column the list landed in rather than being named in LIST_VALIDATIONS.
        letter = get_column_letter(unit_list(restored["Lists"])[0])
        expected = set(LIST_VALIDATIONS.values()) | {f"Lists!${letter}$2:${letter}${MAX_UNITS + 1}"}
        self.assertEqual({dv.formula1 for dv in lists}, expected)
        self.assertEqual(len(lists), len(expected))   # one dropdown per column, no duplicates

    def test_excel_dropdowns_offer_every_status(self):
        from workspace_store import CLIENT_STATUSES
        wb, revision = self.store.snapshot()
        wb["Lists"]["B7"] = "Stale value"
        self.store.save(wb, revision)
        lists = self.store.snapshot()[0]["Lists"]
        # Column B held the retired Lookup Status list: emptied, never reused, so the
        # Yes / No, Client Status and Locations ranges still name columns C, D and E.
        self.assertEqual([lists.cell(row, 2).value for row in range(1, 9)], [None] * 8)
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

    def test_delete_endpoints_and_guards(self):
        row = self.state()["selections"][0]
        response = self.post("/api/selections", {"row": row["_row"], "values": {"Item": row["Item"]}, "delete": True})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(any(r["Item"] == row["Item"] for r in self.state()["selections"]))
        project = self.state()["projects"][0]
        response = self.post("/api/projects", {"values": {"Project ID": project["Project ID"]}, "delete": True})
        self.assertEqual(response.status_code, 400)
        self.assertIn("line items", response.json["error"])   # never silently discards a project's work
        self.post("/api/projects", {"create": True, "values": {"Project ID": "TEST-9", "Project Name": "Temp"}})
        seeded = len([r for r in self.state()["selections"] if r["Project ID"] == "TEST-9"])
        response = self.post("/api/projects", {"values": {"Project ID": "TEST-9"}, "delete": True, "cascade": True})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["removed"], seeded)
        state = self.state()
        self.assertFalse(any(p["Project ID"] == "TEST-9" for p in state["projects"]))
        self.assertFalse(any(r["Project ID"] == "TEST-9" for r in state["selections"]))
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

    def test_renumbering_a_project_takes_its_line_items_with_it(self):
        """A Project ID is the only thing tying a selection to its project, so renaming one
        has to move every line item in the same save - and must refuse an ID already in use
        rather than merge two homes into one."""
        state = self.state()
        project = state["projects"][0]
        old_id = project["Project ID"]
        owned = [r for r in state["selections"] if r["Project ID"] == old_id]
        self.assertTrue(owned, "the first project needs line items for this test to mean anything")
        other = next((p["Project ID"] for p in state["projects"][1:]), None)

        # an ID another project already holds is refused, and nothing moves
        if other:
            clash = self.post("/api/projects", {"values": {"Project ID": old_id}, "rename": other})
            self.assertEqual(clash.status_code, 400, clash.json)
            self.assertIn("already exists", clash.json["error"])
            self.assertEqual(len([r for r in self.state()["selections"] if r["Project ID"] == old_id]),
                             len(owned))

        response = self.post("/api/projects", {"values": {"Project ID": old_id}, "rename": "UH-001"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["project_id"], "UH-001")
        self.assertEqual(response.json["moved"], len(owned))

        state = self.state()
        ids = {p["Project ID"] for p in state["projects"]}
        self.assertIn("UH-001", ids)
        self.assertNotIn(old_id, ids)
        self.assertEqual([r["Project ID"] for r in state["selections"] if r["Project ID"] == old_id], [])
        moved = [r for r in state["selections"] if r["Project ID"] == "UH-001"]
        self.assertEqual(len(moved), len(owned))
        # the line items are the same records, not blanked or reordered by the move
        self.assertEqual([r["Item"] for r in moved], [r["Item"] for r in owned])
        renamed = next(p for p in state["projects"] if p["Project ID"] == "UH-001")
        self.assertEqual(renamed["Project Name"], project["Project Name"])
        self.assertEqual(renamed["_row"], project["_row"])   # renumbered in place

        # a rename to the ID it already has is a no-op rather than an error
        same = self.post("/api/projects", {"values": {"Project ID": "UH-001"}, "rename": "UH-001"})
        self.assertEqual(same.status_code, 200, same.json)
        self.assertEqual(same.json["moved"], 0)
        # and a project that is no longer there conflicts instead of creating one
        gone = self.post("/api/projects", {"values": {"Project ID": old_id}, "rename": "UH-009"})
        self.assertEqual(gone.status_code, 409, gone.json)
        self.assertNotIn("UH-009", {p["Project ID"] for p in self.state()["projects"]})

    def test_a_renamed_project_still_exports(self):
        """The spec sheet is fetched by Project ID, so a renumbered project must answer to its
        new one - the check that the cascade really landed in the workspace."""
        old_id = self.state()["projects"][0]["Project ID"]
        self.assertEqual(self.post("/api/projects", {"values": {"Project ID": old_id},
                                                     "rename": "UH-001"}).status_code, 200)
        stale = self.client.get(f"/api/selections/pdf?project={old_id}")
        self.assertEqual(stale.status_code, 400, "the old ID must no longer name a project")
        fresh = self.client.get("/api/selections/pdf?project=UH-001")
        self.assertEqual(fresh.status_code, 200, fresh.data[:200])
        self.assertTrue(fresh.data.startswith(b"%PDF"))

    def test_new_project_starts_from_the_spec_template(self):
        import spec_template
        _, template = spec_template.load()
        expected = spec_template.rows(template)
        before = len(self.state()["selections"])
        response = self.post("/api/projects", {"create": True, "values": {"Project ID": "TEST-T", "Project Name": "Template home"}})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["seeded"], len(expected))
        state = self.state()
        self.assertEqual(state["locations"][-5:], [location for location, _ in template])
        self.assertEqual(len(state["selections"]), before + len(expected))
        rows = [r for r in state["selections"] if r["Project ID"] == "TEST-T"]
        for location, room, item in expected[:: max(1, len(expected) // 20)]:
            self.assertTrue(any(r["Location"] == location and r["Room / Area"] == room and r["Item"] == item for r in rows),
                            f"{location} / {room} / {item} missing")
        seeded = next(r for r in rows if r["Room / Area"] == "Master Bathroom")
        self.assertEqual((seeded["Include in Lookbook"], seeded["Client Status"]), ("Yes", "Proposed"))
        # editing the project later must not seed it again
        edit = self.post("/api/projects", {"values": {"Project ID": "TEST-T", "Client Name": "Someone"}})
        self.assertEqual(edit.json["seeded"], 0)
        self.assertEqual(len(self.state()["selections"]), before + len(expected))

    def send_spec_csv(self, project, rows):
        """Upload line items the way a spreadsheet saves them back."""
        import csv as csvlib
        buffer = io.StringIO()
        writer = csvlib.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        return self.client.post("/api/selections/csv", data={
            "file": (io.BytesIO(buffer.getvalue().encode("utf-8")), "spec.csv"),
            "project": project, "revision": self.state()["revision"]}, headers=self.headers)

    def exported_spec_csv(self, project):
        import csv as csvlib
        export = self.client.get(f"/api/selections/csv?project={project}")
        self.assertEqual(export.status_code, 200)
        return export, list(csvlib.DictReader(io.StringIO(export.data.decode("utf-8-sig"))))

    def test_spec_csv_product_link_survives_a_spreadsheet_round_trip(self):
        """The CSV carries a HYPERLINK formula, and Excel's flattened text never clears a link."""
        project = self.state()["projects"][0]["Project ID"]
        row = next(r for r in self.state()["selections"] if r["Project ID"] == project)
        self.post("/api/selections", {"row": row["_row"], "values": {"Product URL": "https://example.com/p"}})
        _, lines = self.exported_spec_csv(project)
        target = next(index for index, line in enumerate(lines)
                      if (line["Location"], line["Room / Area"], line["Item"]) == (row["Location"], row["Room / Area"], row["Item"]))
        self.assertEqual(lines[target]["Product Link"], '=HYPERLINK("https://example.com/p","Product Link")')
        self.assertTrue(all(line["Product Link"] == "" for index, line in enumerate(lines) if index != target))

        def current():
            return next(r for r in self.state()["selections"] if r["_row"] == row["_row"])

        # Excel writes the formula's displayed text when it re-saves a CSV
        flattened = [dict(line) for line in lines]
        flattened[target]["Product Link"] = "Product Link"
        self.assertEqual(self.send_spec_csv(project, flattened).status_code, 200)
        self.assertEqual(current()["Product URL"], "https://example.com/p")

        # a link typed into the spreadsheet is imported
        typed = [dict(line) for line in lines]
        typed[target]["Product Link"] = "https://example.com/replacement"
        self.assertEqual(self.send_spec_csv(project, typed).status_code, 200)
        self.assertEqual(current()["Product URL"], "https://example.com/replacement")

    def test_spec_csv_round_trip_updates_without_losing_data(self):
        project = self.state()["projects"][0]["Project ID"]
        row = next(r for r in self.state()["selections"] if r["Project ID"] == project)
        self.post("/api/selections", {"row": row["_row"], "values": {"Product URL": "https://example.com/p"}})

        export, lines = self.exported_spec_csv(project)
        self.assertIn("attachment", export.headers["Content-Disposition"])
        self.assertEqual(len(lines), len([r for r in self.state()["selections"] if r["Project ID"] == project]))

        # re-importing the untouched export changes nothing and keeps the link
        def send(rows):
            return self.send_spec_csv(project, rows)

        response = send(lines)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual((response.json["added"], response.json["updated"]), (0, len(lines)))
        current = next(r for r in self.state()["selections"] if r["_row"] == row["_row"])
        self.assertEqual(current["Product URL"], "https://example.com/p")

        # rows are matched on identity rather than position, so find the linked line by its own
        def index_of(record):
            return next(i for i, line in enumerate(lines)
                        if (line["Location"], line["Room / Area"], line["Item"]) == (record["Location"], record["Room / Area"], record["Item"]))
        target = index_of(row)

        # a price edit plus a brand-new line
        edited = [dict(line) for line in lines]
        edited[target]["Unit Price"] = "99.5"
        edited.append({**{field: "" for field in lines[0]}, "Section": "Garage", "Room / Area": "3-car",
                       "Item": "Imported shelving", "Qty / Unit": "2 EA"})
        response = send(edited)
        self.assertEqual((response.json["added"], response.json["updated"]), (1, len(lines)))
        state = self.state()
        self.assertTrue(any(r["Item"] == "Imported shelving" and (r["Qty"], r["Unit"]) == ("2", "EA")
                            for r in state["selections"]))
        self.assertEqual(next(r for r in state["selections"] if r["_row"] == row["_row"])["Unit Price"], "99.5")

        # changing the product identity clears the link that described the old one
        changed = [dict(line) for line in lines]
        changed[target]["Model #"] = "DIFFERENT-MODEL"
        response = send(changed)
        self.assertEqual(response.status_code, 200, response.json)
        current = next(r for r in self.state()["selections"] if r["_row"] == row["_row"])
        self.assertEqual(current["Product URL"], "")

        # malformed input is refused outright
        bad = self.client.post("/api/selections/csv", data={
            "file": (io.BytesIO(b"Nonsense,Columns\n1,2\n"), "spec.csv"),
            "project": project, "revision": self.state()["revision"]}, headers=self.headers)
        self.assertEqual(bad.status_code, 400)

    def test_word_exports(self):
        """The spec sheet downloads carry every column header and line item."""
        data = self.state()
        project = data["projects"][0]
        pid = project["Project ID"]

        sheet = self.client.get(f"/api/selections/docx?project={pid}")
        self.assertEqual(sheet.status_code, 200)
        self.assertIn("Spec_Sheet", sheet.headers["Content-Disposition"])
        self.assertIn("MANUFACTURER", docx_text(sheet.data))
        self.assertEqual(self.client.get("/api/selections/docx?project=nope").status_code, 400)

        sheet_pdf = self.client.get(f"/api/selections/pdf?project={pid}")
        self.assertEqual(sheet_pdf.status_code, 200)
        self.assertIn("Spec_Sheet", sheet_pdf.headers["Content-Disposition"])
        from pypdf import PdfReader
        # narrow column headings wrap, so check the words against whitespace-normalised text
        text = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(sheet_pdf.data)).pages)
        text = " ".join(text.split())
        self.assertIn("MANUFACTURER", text)
        self.assertIn("UNIT PRICE", text)
        self.assertEqual(self.client.get("/api/selections/pdf?project=nope").status_code, 400)

    def test_spec_exports_carry_every_on_screen_column_and_line_item(self):
        """The download mirrors the spec-sheet view: its columns, its headings, every row."""
        from pypdf import PdfReader
        pid = self.state()["projects"][0]["Project ID"]
        rows = [r for r in self.state()["selections"] if r["Project ID"] == pid]
        shown = ["ITEM", "MANUFACTURER", "MODEL #", "FINISH / COLOR", "PRODUCT LINK",
                 "QTY / UNIT", "UNIT PRICE", "MARKUP %"]
        absent = ["CLIENT STATUS", "CLIENT NOTES", "INCLUDE IN LOOKBOOK", "PRODUCT NAME"]

        pdf = self.client.get(f"/api/selections/pdf?project={pid}")
        self.assertEqual(pdf.status_code, 200)
        text = " ".join(" ".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.data)).pages).split())
        for column in shown:
            self.assertIn(column, text, column)
        for column in absent:
            self.assertNotIn(column, text, column)
        for row in rows:
            self.assertIn(row["Item"], text, row["Item"])

        word = self.client.get(f"/api/selections/docx?project={pid}")
        self.assertEqual(word.status_code, 200)
        text = " ".join(docx_text(word.data).split())
        for column in shown:
            self.assertIn(column, text, column)
        for column in absent:
            self.assertNotIn(column, text, column)
        for row in rows:
            self.assertIn(row["Item"], text, row["Item"])

    def test_spec_exports_group_the_way_the_view_does(self):
        from pypdf import PdfReader
        row = next(r for r in self.state()["selections"] if r["Section"] and r["Room / Area"])
        pid = row["Project ID"]

        def headings(group):
            response = self.client.get(f"/api/selections/pdf?project={pid}&group={group}")
            self.assertEqual(response.status_code, 200, group)
            text = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(response.data)).pages)
            return " ".join(text.split())

        # the heading is letter-spaced on the page, exactly as the schedule's are
        spaced = lambda value: " ".join(" ".join(word) for word in value.upper().split())
        self.assertIn(spaced(row["Section"]), headings("Section"))
        self.assertIn(spaced(row["Room / Area"]), headings("Room / Area"))
        self.assertIn(spaced(row["Room / Area"]), headings(""))   # the chip the view opens on
        # the view offers by room and by category only, so Location is as unknown as any other name
        for group in ("Location", "Nonsense"):
            self.assertEqual(self.client.get(f"/api/selections/pdf?project={pid}&group={group}").status_code, 400)
            self.assertEqual(self.client.get(f"/api/selections/docx?project={pid}&group={group}").status_code, 400)

    def test_the_project_group_prints_as_a_details_box_not_a_table(self):
        """Rows under a heading called PROJECT are the home's own figures, not line items:
        the spec sheet draws them like the client band above - labelled values on cream -
        rather than as a ruled table of columns, in the PDF and the Word copy alike."""
        from pypdf import PdfReader
        pid = self.state()["projects"][0]["Project ID"]
        rows = [r for r in self.state()["selections"] if r["Project ID"] == pid]
        wb, revision = self.store.snapshot()
        sheet = common.Sheet(wb["Selections"])
        for row, data in sheet.rows():
            if common.clean(data.get("Project ID")) == pid:
                sheet.set(row, "Room / Area", "PROJECT")
        self.store.save(wb, revision)

        pdf = self.client.get(f"/api/selections/pdf?project={pid}")
        text = " ".join(" ".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.data)).pages).split())
        spaced = lambda value: " ".join(" ".join(word) for word in value.upper().split())
        self.assertIn(spaced("PROJECT"), text)              # the heading still prints
        self.assertNotIn("MANUFACTURER", text)              # but no line-item table follows
        for row in rows[:3]:
            self.assertIn(row["Item"].upper(), text, row["Item"])
        # a figure's other filled fields follow its value, like the band's own fields
        sample = next((r["Size / Configuration"] for r in rows if r.get("Size / Configuration")), None)
        if sample:
            self.assertIn(sample, text)

        word = " ".join(docx_text(self.client.get(f"/api/selections/docx?project={pid}").data).split())
        self.assertIn("PROJECT", word)
        self.assertNotIn("MANUFACTURER", word)
        for row in rows[:3]:
            self.assertIn(row["Item"].upper(), word, row["Item"])

    def test_spec_exports_keep_the_order_the_view_shows(self):
        """A download must read top to bottom like the screen, which is workspace order.

        Sorting the rows by Location instead moved every heading: a row dragged into place on
        the spec sheet landed somewhere else in its own PDF.
        """
        from pypdf import PdfReader
        pid = self.state()["projects"][0]["Project ID"]
        mine = lambda: [r for r in self.state()["selections"] if r["Project ID"] == pid]
        first, last = mine()[0], mine()[-1]
        # Locations the list puts last and first, so a location-ordered export would swap these
        # two rows end for end; distinctive room names make their headings unmistakable.
        self.post("/api/selections", {"row": first["_row"],
                                      "values": {"Room / Area": "Zulu Suite", "Location": "Bulk Items"}})
        self.post("/api/selections", {"row": last["_row"],
                                      "values": {"Room / Area": "Alpha Nook", "Location": "1st Floor"}})
        rows = mine()
        self.assertEqual([rows[0]["_row"], rows[-1]["_row"]], [first["_row"], last["_row"]])

        _, lines = self.exported_spec_csv(pid)
        self.assertEqual([line["Item"] for line in lines], [r["Item"] for r in rows])

        pdf = self.client.get(f"/api/selections/pdf?project={pid}")
        self.assertEqual(pdf.status_code, 200)
        text = " ".join(" ".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.data)).pages).split())
        spaced = lambda value: " ".join(" ".join(word) for word in value.upper().split())
        self.assertLess(text.index(spaced("Zulu Suite")), text.index(spaced("Alpha Nook")))

        word = " ".join(docx_text(self.client.get(f"/api/selections/docx?project={pid}").data).split())
        self.assertLess(word.index(spaced("Zulu Suite")), word.index(spaced("Alpha Nook")))

    def test_spec_exports_print_the_orientation_the_download_asks_for(self):
        """Landscape as the dialog opens, portrait when it is asked for: the same sheet, turned.

        Eight columns want a letter page on its side, but a copy filed with the client
        schedule has to stand upright, so the choice travels with the download - and the
        file says which way round it is, so both can sit in one folder.
        """
        from docx import Document
        from pypdf import PdfReader
        pid = self.state()["projects"][0]["Project ID"]
        # a long quantity and a five-figure price: portrait's columns are three quarters of
        # the width, and at the landscape padding a number of this length came down the
        # column a digit at a time
        priced = next(r for r in self.state()["selections"] if r["Project ID"] == pid)
        self.post("/api/selections", {"row": priced["_row"],
                                      "values": {"Qty": "1250", "Unit": "sq ft",
                                                 "Unit Price": "12345.67"}})
        items = [r["Item"] for r in self.state()["selections"] if r["Project ID"] == pid]

        def pdf_page(query):
            response = self.client.get(f"/api/selections/pdf?project={pid}{query}")
            self.assertEqual(response.status_code, 200, query)
            reader = PdfReader(io.BytesIO(response.data))
            box = reader.pages[0].mediabox
            return (response, round(float(box.width)), round(float(box.height)),
                    " ".join(" ".join(page.extract_text() for page in reader.pages).split()))

        def word_page(query):
            response = self.client.get(f"/api/selections/docx?project={pid}{query}")
            self.assertEqual(response.status_code, 200, query)
            section = Document(io.BytesIO(response.data)).sections[0]
            return (response, round(section.page_width.pt), round(section.page_height.pt),
                    " ".join(docx_text(response.data).split()))

        for name, read_page in (("PDF", pdf_page), ("Word", word_page)):
            for query in ("", "&orientation=landscape"):
                response, width, height, _ = read_page(query)
                self.assertEqual((width, height), (792, 612), f"{name} {query or 'by default'}")
                self.assertIn("Landscape", response.headers["Content-Disposition"], name)
            response, width, height, text = read_page("&orientation=portrait")
            self.assertEqual((width, height), (612, 792), name)
            self.assertIn("Portrait", response.headers["Content-Disposition"], name)
            # turning the page is all that changes: every line item is still on it, and the
            # narrower columns still hold a value rather than wrapping it mid-number
            for item in items:
                self.assertIn(item, text, f"{name} {item}")
            for value in ("1250 sq ft", "12345.67"):
                self.assertIn(value, text, f"{name} {value}")

        # an orientation the builders do not know is refused, never quietly printed landscape
        for route in ("pdf", "docx"):
            stale = self.client.get(f"/api/selections/{route}?project={pid}&orientation=sideways")
            self.assertEqual(stale.status_code, 400, route)

    def test_unit_is_a_selections_column_the_spec_sheet_carries(self):
        """Qty is a bare number without it: 7571 means nothing until it says sq ft.

        The two read as one Qty / Unit column everywhere they are shown, and come back
        apart into the columns they are stored in - Qty stays the number the Price
        Schedule multiplies.
        """
        import csv as csvlib
        from pypdf import PdfReader
        from workspace_store import UNITS, unit_list
        for text, apart in (("7571 sq ft", ("7571", "sq ft")), ("4", ("4", "")), ("EA", ("", "EA")),
                            ("1,250 LF", ("1250", "LF")), ("2.5gal", ("2.5", "gal")), ("", ("", ""))):
            self.assertEqual(common.split_qty_unit(text), dict(zip(("Qty", "Unit"), apart)), text)
        self.assertEqual(common.qty_unit({"Qty": "7571", "Unit": "sq ft"}), "7571 sq ft")
        self.assertEqual((common.qty_unit({"Qty": "4"}), common.qty_unit({})), ("4", ""))

        row = self.state()["selections"][0]
        pid = row["Project ID"]
        saved = self.post("/api/selections", {"row": row["_row"], "values": {"Qty": "7571", "Unit": "sq ft"}})
        self.assertEqual(saved.status_code, 200, saved.json)
        current = next(r for r in self.state()["selections"] if r["_row"] == row["_row"])
        self.assertEqual((current["Qty"], current["Unit"]), ("7571", "sq ft"))

        # the seeded list fills Excel's own dropdown
        self.assertEqual(self.state()["units"], UNITS)
        wb, _ = self.store.snapshot()
        column, names = unit_list(wb["Lists"])
        self.assertEqual(names, UNITS)
        letter = __import__("openpyxl").utils.get_column_letter(column)
        sheet = common.Sheet(wb["Selections"])
        unit_letter = __import__("openpyxl").utils.get_column_letter(sheet.cols["Unit"])
        lists = [dv for dv in wb["Selections"].data_validations.dataValidation
                 if dv.type == "list" and f"Lists!${letter}$2" in (dv.formula1 or "")]
        self.assertEqual(len(lists), 1, "the Unit column needs exactly one dropdown")
        self.assertIn(f"{unit_letter}2", str(lists[0].sqref))

        for name, text in (("PDF", " ".join(" ".join(p.extract_text() for p in PdfReader(
                                io.BytesIO(self.client.get(f"/api/selections/pdf?project={pid}").data)).pages).split())),
                           ("Word", " ".join(docx_text(self.client.get(f"/api/selections/docx?project={pid}").data).split()))):
            self.assertIn("QTY / UNIT", text, name)
            self.assertIn("7571 sq ft", text, name)

        # the CSV carries one column too, and splits it back into the two on the way in
        export, lines = self.exported_spec_csv(pid)
        self.assertIn("Qty / Unit", lines[0])
        self.assertNotIn("Qty", lines[0])
        mine = next(l for l in lines if l["Qty / Unit"] == "7571 sq ft")
        again = self.send_spec_csv(pid, [{**l, "Qty / Unit": "1,250 SQ"} if l is mine else l for l in lines])
        self.assertEqual(again.status_code, 200, again.json)
        current = next(r for r in self.state()["selections"] if r["_row"] == row["_row"])
        self.assertEqual((current["Qty"], current["Unit"]), ("1250", "SQ"))

        # either half can be left out of a download, which narrows the column to the other
        hidden = self.client.get(f"/api/selections/csv?project={pid}&hide=Unit")
        self.assertEqual(hidden.status_code, 200)
        narrowed = list(csvlib.DictReader(io.StringIO(hidden.data.decode("utf-8-sig"))))[0]
        self.assertIn("Qty", narrowed)
        for gone in ("Unit", "Qty / Unit"):
            self.assertNotIn(gone, narrowed)
        # and a download that kept only one half still imports under that name
        back = self.send_spec_csv(pid, list(csvlib.DictReader(io.StringIO(hidden.data.decode("utf-8-sig")))))
        self.assertEqual(back.status_code, 200, back.json)
        kept = next(r for r in self.state()["selections"] if r["_row"] == row["_row"])
        self.assertEqual((kept["Qty"], kept["Unit"]), ("1250", "SQ"))

    def test_unit_column_is_added_to_a_workspace_that_predates_it(self):
        """A workspace saved before the column existed gains it, and its list, on the next save."""
        from workspace_store import UNITS, UNITS_HEADER, named_list
        wb, revision = self.store.snapshot()
        sheet = common.Sheet(wb["Selections"])
        wb["Selections"].delete_cols(sheet.cols["Unit"])
        lists = wb["Lists"]
        unit_column, _ = named_list(lists, UNITS_HEADER)
        lists.delete_cols(unit_column)
        # The save that writes the stripped workspace repairs it on the way through.
        self.store.save(wb, revision, "drop the Unit column")

        wb, _ = self.store.snapshot()
        self.assertIn("Unit", common.Sheet(wb["Selections"]).cols)
        self.assertEqual(named_list(wb["Lists"], UNITS_HEADER)[1], UNITS)
        state = self.state()
        self.assertTrue(all(r["Unit"] == "" for r in state["selections"]))
        row = state["selections"][0]
        self.assertEqual(self.post("/api/selections",
                                   {"row": row["_row"], "values": {"Unit": "LF"}}).status_code, 200)
        self.assertEqual(next(r for r in self.state()["selections"] if r["_row"] == row["_row"])["Unit"], "LF")

    def test_spec_exports_show_pricing(self):
        """The spec sheet is the internal document, so its downloads carry prices."""
        from pypdf import PdfReader
        row = self.state()["selections"][0]
        self.post("/api/selections", {"row": row["_row"], "values": {"Unit Price": "4321.99"}})
        sheet = self.client.get(f"/api/selections/docx?project={row['Project ID']}")
        self.assertIn("4321.99", docx_text(sheet.data))
        sheet_pdf = self.client.get(f"/api/selections/pdf?project={row['Project ID']}")
        sheet_text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(sheet_pdf.data)).pages)
        self.assertIn("4321.99", sheet_text)

    def test_backups_are_listed_and_restore_earlier_data(self):
        row = self.state()["selections"][0]
        original = row["Item"]
        self.post("/api/selections", {"row": row["_row"], "values": {"Item": "Before the mistake"}})
        self.post("/api/selections", {"row": row["_row"], "values": {"Item": "The mistake"}})
        listed = self.store.backups()
        self.assertGreaterEqual(len(listed), 2)
        self.assertEqual(listed, sorted(listed, key=lambda b: b["when"], reverse=True))   # newest first
        self.assertTrue(all(b["size"] > 0 and b["name"].endswith(".xlsx") for b in listed))

        # the newest copy was taken just before the latest save, so it still holds the earlier name
        _, revision = self.store.snapshot()
        self.store.import_bytes(self.store.read_backup(listed[0]["name"]), revision)
        self.assertEqual(next(r for r in self.state()["selections"] if r["_row"] == row["_row"])["Item"],
                         "Before the mistake")
        # restoring kept a copy of what it replaced, so the mistake is still recoverable
        self.assertTrue(any(b["name"] not in {entry["name"] for entry in listed}
                            for b in self.store.backups()))
        self.assertNotEqual(original, "The mistake")

    def test_backups_are_pruned_and_logged(self):
        with patch("workspace_store.BACKUP_KEEP", 2):
            for index in range(4):
                response = self.post("/api/projects", {"create": True, "values": {"Project ID": f"TEST-{index}", "Project Name": "T"}})
                self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(len(list((self.path.parent / "backups").glob("*.xlsx"))), 2)
        history = (self.path.parent / "backups" / "history.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(history), 4)

    def test_copies_saved_in_the_same_second_are_ordered_precisely(self):
        """Several saves can land in one second, so microseconds must decide the order."""
        folder = self.path.parent / "backups"
        folder.mkdir(exist_ok=True)
        for micro in ["9", "100000", "20000"]:
            (folder / f"{self.path.stem}_20250101_120000_{micro}.xlsx").write_bytes(b"copy")
        listed = [entry["name"] for entry in self.store.backups()]
        self.assertEqual(listed, [f"{self.path.stem}_20250101_120000_{micro}.xlsx"
                                  for micro in ["100000", "20000", "9"]])

    def test_stray_lookup_artifacts_do_not_orphan_a_row(self):
        """A row holding only machine output (e.g. a Product URL left behind by a
        delete) carries no user data — it must not trip the unknown-project check."""
        import openpyxl
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Selections"]
        url_col = next(c.column for c in ws[1] if c.value == "Product URL")
        ws.cell(row=ws.max_row + 2, column=url_col, value="https://example.com/stray")
        wb.save(self.path)
        state = self.state()
        self.assertTrue(all(r["Item"] for r in state["selections"]))
        self.assertNotIn("example.com/stray", str(state["selections"]))

    def test_reorder_permutes_rows_without_losing_data(self):
        state = self.state()
        project = state["projects"][0]["Project ID"]
        rows = [r for r in state["selections"] if r["Project ID"] == project]
        first, last = rows[0], rows[-1]
        order = [last["_row"], *[r["_row"] for r in rows[1:-1]], first["_row"]]
        response = self.post("/api/selections/order", {"project": project, "order": order,
                                                       "updates": {str(last["_row"]): {"Section": "Moved Section"}}})
        self.assertEqual(response.status_code, 200, response.json)
        after = [r for r in self.state()["selections"] if r["Project ID"] == project]
        self.assertEqual([r["Item"] for r in after],
                         [last["Item"], *[r["Item"] for r in rows[1:-1]], first["Item"]])
        moved = after[0]
        self.assertEqual(moved["Item"], last["Item"])
        self.assertEqual(moved["Section"], "Moved Section")
        for field in ["Manufacturer", "Model #", "Finish / Color", "Qty", "Product URL",
                      "Client Status", "Include in Lookbook"]:
            self.assertEqual(moved[field], last[field], field)

    def test_reorder_rejects_partial_or_foreign_orders(self):
        state = self.state()
        project = state["projects"][0]["Project ID"]
        rows = [r for r in state["selections"] if r["Project ID"] == project]
        self.assertEqual(self.post("/api/selections/order",
                                   {"project": project, "order": [r["_row"] for r in rows[1:]]}).status_code, 400)
        foreign = next(r["_row"] for r in state["selections"] if r["Project ID"] != project)
        self.assertEqual(self.post("/api/selections/order",
                                   {"project": project, "order": [r["_row"] for r in rows[:-1]] + [foreign]}).status_code, 400)

    def test_batch_save_applies_every_row_in_one_revision(self):
        """Manual-save mode flushes many edited rows at once; one stale revision would
        reject the rest, so the whole batch must share a single workspace save."""
        state = self.state()
        rows = [r for r in state["selections"] if r["Project ID"] == state["projects"][0]["Project ID"]][:3]
        batch = {str(r["_row"]): {"Item": f"Batched {i}", "Client Notes": f"note {i}"}
                 for i, r in enumerate(rows)}
        response = self.post("/api/selections", {"batch": batch})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(sorted(response.json["applied"]), sorted(r["_row"] for r in rows))
        after = {r["_row"]: r for r in self.state()["selections"]}
        for i, row in enumerate(rows):
            self.assertEqual(after[row["_row"]]["Item"], f"Batched {i}")
            self.assertEqual(after[row["_row"]]["Client Notes"], f"note {i}")
            self.assertEqual(after[row["_row"]]["Manufacturer"], row["Manufacturer"])

    def test_batch_save_is_all_or_nothing(self):
        state = self.state()
        rows = [r for r in state["selections"] if r["Project ID"] == state["projects"][0]["Project ID"]]
        good = str(rows[0]["_row"])
        for batch, code in [({good: {"Item": "Kept"}, "9999": {"Item": "Ghost"}}, 409),
                            ({good: {"Nope": "x"}}, 400),
                            ({good: {"Item": ""}}, 400),
                            ({"not-a-row": {"Item": "x"}}, 400)]:
            response = self.post("/api/selections", {"batch": batch})
            self.assertEqual(response.status_code, code, response.json)
        unchanged = {r["_row"]: r for r in self.state()["selections"]}
        self.assertEqual(unchanged[rows[0]["_row"]]["Item"], rows[0]["Item"])

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
        from workspace_store import SELECTION_FIELDS
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


def pdf_ink(path):
    """What the first page is painted in: the fill colours used, and every rule by colour.

    reportlab writes colours as their own operators ('1 1 1 rg' to fill, '... RG' to stroke)
    and each rule as one line op, so a count per stroke colour says which rules are drawn
    in what - the page, the client band and the rules can be told apart.
    """
    from pypdf import PdfReader
    stream = PdfReader(str(path)).pages[0].get_contents().get_data().decode("latin-1")
    fills, rules, stroke = set(), {}, None
    for line in stream.splitlines():
        if match := re.fullmatch(r"([\d.]+) ([\d.]+) ([\d.]+) (rg|RG)", line):
            color = tuple(round(float(match[index]), 3) for index in (1, 2, 3))
            if match[4] == "rg":
                fills.add(color)
            else:
                stroke = color
        elif line.endswith(" S"):
            rules[stroke] = rules.get(stroke, 0) + 1
    return fills, rules


def rule_segments(path):
    """Every stroked rule on each page as (width, x1, y1, x2, y2), in page coordinates.

    A table's rules are drawn inside a translated canvas, so the `cm` offsets have to be
    followed before a vertical's ends can be compared with the horizontals it lands on.
    A Product Link underline is stroked without setting a width at all, which is the PDF's
    own default of 1pt - not an unknown - so it is reported as that rather than as None.
    """
    from pypdf import PdfReader
    pages = []
    for page in PdfReader(str(path)).pages:
        stream = page.get_contents().get_data().decode("latin-1")
        segments, width, stack, x, y = [], 1.0, [], 0.0, 0.0
        for line in stream.splitlines():
            if line == "q":
                stack.append((x, y, width))
            elif line == "Q" and stack:
                x, y, width = stack.pop()
            elif match := re.fullmatch(r"1 0 0 1 (-?[\d.]+) (-?[\d.]+) cm", line):
                x, y = x + float(match[1]), y + float(match[2])
            elif match := re.fullmatch(r"([\d.]+) w", line):
                width = float(match[1])
            elif match := re.fullmatch(r"n (-?[\d.]+) (-?[\d.]+) m (-?[\d.]+) (-?[\d.]+) l S", line):
                segments.append((width, round(x + float(match[1]), 1), round(y + float(match[2]), 1),
                                 round(x + float(match[3]), 1), round(y + float(match[4]), 1)))
        pages.append(segments)
    return pages


def rgb(value):
    """'#AA8960' as the fractions a PDF operator carries."""
    return tuple(round(int(value.lstrip("#")[index:index + 2], 16) / 255, 3) for index in (0, 2, 4))


def cell_borders(document):
    """Every colour the table cell rules in a Word document are drawn in."""
    from docx.oxml.ns import qn
    return {edge.get(qn("w:color")) for table in document.tables for row in table.rows
            for cell in row.cells for borders in cell._tc.iter(qn("w:tcBorders")) for edge in borders}


class ScheduleTests(unittest.TestCase):
    """The client deliverable follows the approved finish schedule template."""

    def test_layout_matches_the_approved_template(self):
        import build_lookbook
        from workspace_store import SELECTION_FIELDS
        template = Path("brand_assets/finish_schedule_template.pdf")
        if not template.exists():
            self.skipTest("Reference template PDF is not available.")
        rows = [{**{key: "" for key in SELECTION_FIELDS}, "Section": section, "Location": section,
                 "Item": item, "Client Notes": note} for section, item, note in TEMPLATE_ROWS]
        with tempfile.TemporaryDirectory() as folder:
            pdf = build_lookbook.build("UH-000", {"Project Name": ""}, rows, False, Path(folder),
                                       title="Exterior Selections", prefix="EX")
            self.assertEqual(page_geometry(pdf), page_geometry(template))

    def rows(self, count=1, **values):
        from workspace_store import SELECTION_FIELDS
        return [{**{key: "" for key in SELECTION_FIELDS}, "Section": "Roofing", "Item": f"Item {index}",
                 **values} for index in range(1, count + 1)]

    def text(self, path):
        from pypdf import PdfReader
        return "\n".join(page.extract_text() for page in PdfReader(str(path)).pages)

    def test_deliverables_use_arial_for_selection_items(self):
        import build_lookbook
        import build_powerpoint
        from pypdf import PdfReader
        from pptx import Presentation
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            pdf = build_lookbook.build("SCHED", {"Project Name": "T"}, self.rows(2), False, folder,
                                       title="Exterior Selections", prefix="EX")
            resources = PdfReader(str(pdf)).pages[0]["/Resources"].get_object()
            names = {str(font.get_object()["/BaseFont"]) for font in resources["/Font"].values()}
            self.assertTrue(any("LiberationSans" in name for name in names))
            self.assertFalse(any(name.lstrip("/").startswith("Lato") for name in names))
            deck = build_powerpoint.build("SCHED", {"Project Name": "T"}, self.rows(2), False, folder,
                                          title="Exterior Selections", prefix="EX")
            runs = [run for slide in Presentation(str(deck)).slides for shape in slide.shapes
                    if shape.has_text_frame for para in shape.text_frame.paragraphs
                    for run in para.runs if run.text.strip()]
            self.assertTrue(runs)
            self.assertEqual({run.font.name for run in runs}, {"Arial"})
            for style_, size in [(build_lookbook.CODE_STYLE, 9), (build_lookbook.ITEM_STYLE, 10),
                                 (build_lookbook.DESC_STYLE, 10)]:
                self.assertEqual(style_.fontSize, size)
            for spec, size in [(build_powerpoint.CODE, 9), (build_powerpoint.ITEM, 10),
                               (build_powerpoint.DESC, 10)]:
                self.assertEqual(spec[0], size)

    def test_every_spec_sheet_heading_prints_on_one_line(self):
        """A heading that wraps reads as two columns - PRODUCT over LINK - so each share is
        wide enough for its own heading, and PRODUCT LINK for the words in its cells too.

        Portrait is what decides them: the same columns in three quarters of the width, with
        tighter cell padding. Hiding a column only ever widens the rest, so the full set is
        the tight case, and Word lays out the same shares in the same font metrics.
        """
        import build_lookbook
        from reportlab.pdfbase import pdfmetrics
        columns = build_lookbook.spec_columns()
        total = sum(share for _, share in columns)
        head, cell = build_lookbook.SPEC_HEAD_STYLE, build_lookbook.SPEC_CELL_STYLE
        for name in build_lookbook.SPEC_ORIENTATIONS:
            page = build_lookbook.spec_page(name)
            for column, share in columns:
                room = share / total * page.content_w - 2 * page.pad_x
                printed = pdfmetrics.stringWidth(build_lookbook.spec_heading(column),
                                                 build_lookbook.FONT_BB, head.fontSize)
                if column == build_lookbook.PRODUCT_LINK:
                    printed = max(printed, pdfmetrics.stringWidth(column, build_lookbook.FONT_B, cell.fontSize))
                self.assertLess(printed, room, f"{column} wraps on the {name} sheet")

    def test_the_spec_sheets_column_gaps_are_light_and_close_on_a_row_rule(self):
        """The gaps between columns are secondary structure, so they are the lightest rule on
        the sheet - half a row rule - and both ends close on a horizontal rule instead of
        stopping in mid-air above the headings or below the last row.

        Long enough to split, so the repeated heading on each later page is measured too.
        """
        import build_lookbook
        rows = self.rows(60, **{"Room / Area": "Whole House"})
        with tempfile.TemporaryDirectory() as folder:
            pages = rule_segments(build_lookbook.build_spec("UH-1", {"Project Name": "T"}, rows,
                                                            Path(folder), orientation="portrait"))
        self.assertGreater(len(pages), 1, "the sheet needs to split to check a repeated heading")
        for number, segments in enumerate(pages, 1):
            across = {y1 for _, _, y1, _, y2 in segments if y1 == y2}
            down = [(width, min(y1, y2), max(y1, y2)) for width, x1, y1, x2, y2 in segments if x1 == x2]
            self.assertTrue(down, f"page {number} has no column gaps")
            for width, foot, head in down:
                self.assertLess(width, min(w for w, _, y1, _, y2 in segments if y1 == y2),
                                f"page {number}: a column gap is not lighter than every row rule")
                self.assertIn(foot, across, f"page {number}: a column gap stops in mid-air at its foot")
                self.assertIn(head, across, f"page {number}: a column gap stops in mid-air at its head")

    def test_the_spec_sheet_prints_on_white_and_is_ruled_in_black(self):
        """The internal sheet is the one document on plain paper: a white page ruled in black
        at the studio weights, with the client details box left exactly as it was - cream,
        with its four gold field underlines. The client finish schedule keeps the cream sheet
        and its gold rules, so the two are told apart at a glance.
        """
        import build_lookbook
        import build_word
        from docx import Document
        from docx.oxml.ns import qn
        colors = build_lookbook.CFG["colors"]
        band, gold, cream = (rgb(colors[key]) for key in ("band", "field_rule", "hairline"))
        project = {"Project Name": "T", "Client Name": "C"}
        rows = self.rows(3, **{"Room / Area": "Whole House"})
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            fills, rules = pdf_ink(build_lookbook.build_spec("UH-1", project, rows, folder))
            self.assertIn(rgb("#FFFFFF"), fills)        # the page under everything
            self.assertIn(band, fills)                  # the client details box, untouched
            self.assertEqual(rules.get(gold), 4)        # and only its own four underlines
            self.assertEqual(set(rules) - {gold}, {rgb("#000000")})

            # the client schedule is left on cream paper with its gold and hairline rules
            fills, rules = pdf_ink(build_lookbook.build("UH-1", project, rows, False, folder))
            self.assertIn(rgb(colors["paper"]), fills)
            self.assertNotIn(rgb("#FFFFFF"), fills)
            self.assertIn(cream, rules)

            # the Word copies of each say the same thing
            sheet = Document(str(build_word.build_spec("UH-1", project, rows, folder)))
            self.assertEqual(sheet.element.find(qn("w:background")).get(qn("w:color")), "FFFFFF")
            self.assertEqual(cell_borders(sheet), {"000000"})
            schedule = Document(str(build_word.build("UH-1", project, rows, False, folder)))
            self.assertEqual(schedule.element.find(qn("w:background")).get(qn("w:color")),
                             colors["paper"].lstrip("#").upper())
            self.assertEqual(cell_borders(schedule), {colors["hairline"].lstrip("#").upper()})

    def test_descriptions_are_assembled_from_workspace_columns(self):
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

    def test_descriptions_leave_out_hidden_columns(self):
        from build_lookbook import description
        row = {"Manufacturer": "Delta", "Product Name": "Trinsic vanity faucet", "Model #": "559LF-PP",
               "Finish / Color": "Chrome", "Qty": "2", "Client Notes": "left basin"}
        full = "Delta Trinsic vanity faucet (Model 559LF-PP) \u2013 Chrome \u2013 Qty 2 \u2013 left basin"
        self.assertEqual(description(row), full)
        cases = [
            ({"Manufacturer"}, "Trinsic vanity faucet (Model 559LF-PP) \u2013 Chrome \u2013 Qty 2 \u2013 left basin"),
            ({"Model #"}, "Delta Trinsic vanity faucet \u2013 Chrome \u2013 Qty 2 \u2013 left basin"),
            ({"Qty", "Client Notes"}, "Delta Trinsic vanity faucet (Model 559LF-PP) \u2013 Chrome"),
            ({"Product Name"}, "Delta 559LF-PP \u2013 Chrome \u2013 Qty 2 \u2013 left basin"),
            (set(), full),
            (set(row), ""),            # hiding every column leaves an empty line, not an error
        ]
        for hidden, expected in cases:
            self.assertEqual(description(row, hidden=hidden), expected, hidden)
        # a description typed on the fillable schedule is literal text, so hiding cannot rewrite it
        self.assertEqual(description({**row, "Description Override": "As selected"}, hidden={"Manufacturer"}),
                         "As selected")

    def test_schedule_page_keeps_the_template_furniture_and_links_each_product(self):
        import build_lookbook
        from pypdf import PdfReader
        project = {"Project ID": "SCHED", "Project Name": "Template home", "Client Name": "Test client",
                   "Address": "1 Test Way", "Plan / Elevation": "Plan 1", "Presentation Date": "2026-10-01"}
        rows = self.rows(2, Manufacturer="Test", Location="Roofing",
                         **{"Product URL": "https://example.com/product",
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
            # every row carries its product page as a clickable "Product Link"
            self.assertEqual(text.count("Product Link"), 2)
            links = [annotation.get_object()["/A"]["/URI"] for annotation in reader.pages[0]["/Annots"]]
            self.assertEqual(links, ["https://example.com/product"] * 2)
            # product images stay an internal review aid
            self.assertNotIn(b"https://example.com/image.jpg", pdf.read_bytes())
            self.assertEqual(sorted(image.image.size for image in reader.pages[0].images), [(379, 344), (2364, 346)])

    def test_powerpoint_rows_link_to_the_product_page(self):
        import build_powerpoint
        from pptx import Presentation
        rows = self.rows(2, Manufacturer="Test", **{"Product URL": "https://example.com/product"})
        rows[1]["Product URL"] = ""
        with tempfile.TemporaryDirectory() as folder:
            deck = build_powerpoint.build("SCHED", {"Project Name": "T"}, rows, False, Path(folder))
            runs = [run for slide in Presentation(str(deck)).slides for shape in slide.shapes
                    if shape.has_text_frame for para in shape.text_frame.paragraphs for run in para.runs]
            linked = [run for run in runs if run.hyperlink.address]
            self.assertEqual([run.text for run in linked], [build_powerpoint.LINK_SUFFIX])
            self.assertEqual(linked[0].hyperlink.address, "https://example.com/product")
            self.assertEqual(linked[0].font.name, "Arial")

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
