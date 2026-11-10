import re
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.serving import make_server

import app
from workspace_store import WorkspaceStore

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    sync_playwright = None


def settle_layout(page):
    """The shell's margin-left transitions for 180ms after a viewport change, so an
    immediate measurement still reports the desktop layout."""
    expect(page.locator(".app-shell")).to_have_css("margin-left", "0px")


@unittest.skipIf(sync_playwright is None, "Install requirements-dev.txt to run browser tests.")
class BrowserTests(unittest.TestCase):
    def setUp(self):
        # REQUIRE_LOGIN depends on the ambient .env; these tests drive the local, signed-out app.
        patcher = patch.object(app, "REQUIRE_LOGIN", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_custom_project_and_selection_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "custom.xlsx"
            shutil.copy2(app.common.WORKSPACE, workspace)
            with patch.object(app, "store", WorkspaceStore(workspace)):
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1440, "height": 1000})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on("dialog", lambda dialog: dialog.accept())
                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        expect(page.locator("#project-details-panel")).to_be_visible()
                        page.locator("#project-add-field").click()
                        editor = page.locator("#editor")
                        editor.get_by_label("Field name", exact=True).fill("Lot number")
                        editor.get_by_label("Field value", exact=True).fill("Lot 24")
                        editor.get_by_role("button", name="Save changes").click()
                        expect(editor).not_to_be_visible()
                        expect(page.locator('#project-detail-values input[data-custom-key="value"]')).to_have_value("Lot 24")
                        page.locator('[data-project="UH-102"]').click()
                        expect(page.locator('#project-detail-values input[data-custom-key="value"]')).to_have_count(0)
                        page.locator('[data-project="UH-101"]').click()
                        page.get_by_role("button", name="Edit Siding", exact=True).click()
                        editor.locator("#add-custom-field").click()
                        editor.get_by_label("Field name", exact=True).fill("Supplier")
                        editor.get_by_label("Field value", exact=True).fill("Approved supplier")
                        editor.get_by_role("button", name="Save changes").click()
                        expect(editor).not_to_be_visible()
                        page.locator(".selection-details summary").first.click()
                        expect(page.locator("#selection-rows")).to_contain_text("Approved supplier")
                        page.reload()
                        expect(page.locator('#project-detail-values input[data-custom-key="value"]')).to_have_value("Lot 24")
                        # one field, not a duplicate per reload, and edited in place rather than in a dialog
                        value = page.locator('#project-detail-values input[data-custom-key="value"]')
                        expect(value).to_have_count(1)
                        value.fill("Lot 25")
                        page.locator("#project-save-details").click()
                        page.reload()
                        expect(page.locator('#project-detail-values input[data-custom-key="value"]')).to_have_value("Lot 25")
                        page.locator("#search").fill("Approved supplier")
                        expect(page.locator("#selection-rows tr")).to_have_count(1)
                        page.locator("#search").fill("")
                        output = app.BASE / "output"
                        output.mkdir(exist_ok=True)
                        page.screenshot(path=str(output / "custom-fields-desktop.png"), full_page=True)
                        page.set_viewport_size({"width": 390, "height": 844})
                        settle_layout(page)
                        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                        page.screenshot(path=str(output / "custom-fields-mobile.png"), full_page=True)
                        page.set_viewport_size({"width": 1440, "height": 1000})
                        page.locator("#project-add-field").click()   # the only route to the field editor
                        expect(editor).to_be_visible()
                        while editor.locator(".remove-custom-field").count():
                            editor.locator(".remove-custom-field").first.click()
                        editor.get_by_role("button", name="Save changes").click()
                        expect(editor).not_to_be_visible()
                        expect(page.locator('#project-detail-values input[data-custom-key="value"]')).to_have_count(0)
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_spec_sheet_autosaves_and_manual_save_retries(self):
        """Every committed cell saves itself and confirms with a toast. The Save button
        stays as a manual fallback: a save the network drops is held for it to retry."""
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "spec.xlsx"
            shutil.copy2(app.common.WORKSPACE, workspace)
            with patch.object(app, "store", WorkspaceStore(workspace)):
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1500, "height": 1100})
                        errors, saves = [], []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on("request", lambda r: saves.append(r.url) if r.url.endswith("/api/selections") else None)
                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        expect(page.locator("#project-details-panel")).to_be_visible()
                        page.locator('[data-view="spec"]').click()
                        expect(page.locator("#spec-view")).to_be_visible()
                        rows = page.locator(".spec-table tr[data-row]")
                        expect(rows.first).to_be_attached()
                        expect(page.locator("#spec-save")).to_have_text("Save")

                        moved = rows.first.get_attribute("data-row")
                        page.locator(f'tr[data-row="{moved}"] input.cell').first.fill("Autosaved edit")
                        page.locator(f'tr[data-row="{moved}"] input.cell').first.blur()
                        expect(page.locator("#toast")).to_have_text("Saved.", timeout=15000)
                        self.assertEqual(len(saves), 1, saves)   # the edit wrote itself

                        page.locator("#spec-save").click()
                        expect(page.locator("#toast")).to_have_text("Everything is already saved.")

                        # Qty / Unit is one box over two workspace columns: 7571 means nothing
                        # until it says sq ft, but the Price Schedule still needs the bare number.
                        quantity = page.locator(f'tr[data-row="{moved}"] input.cell[data-field="Qty / Unit"]')
                        quantity.fill("7571 sq ft")
                        quantity.blur()
                        expect(page.locator("#toast")).to_have_text("Saved.", timeout=15000)
                        self.assertEqual(page.evaluate(
                            "(row) => { const r = state.data.selections.find((s) => s._row === row);"
                            " return [r.Qty, r.Unit]; }", int(moved)), ["7571", "sq ft"])
                        expect(quantity).to_have_value("7571 sq ft")

                        page.route("**/api/selections", lambda route: route.abort())
                        page.locator(f'tr[data-row="{moved}"] input.cell').nth(1).fill("Retry me")
                        page.locator(f'tr[data-row="{moved}"] input.cell').nth(1).blur()
                        expect(page.locator("#spec-save")).to_have_text("Save changes (1)", timeout=15000)
                        expect(page.locator("#spec-groups .cell.dirty")).to_have_value("Retry me")
                        page.unroute("**/api/selections")
                        page.locator("#spec-save").click()
                        expect(page.locator("#toast")).to_have_text("Saved 1 change.")
                        expect(page.locator("#spec-save")).to_have_text("Save")
                        expect(page.locator("#spec-groups .cell.dirty")).to_have_count(0)

                        target = rows.nth(2).get_attribute("data-row")
                        page.drag_and_drop(f'tr[data-row="{moved}"] .drag-handle',
                                           f'tr[data-row="{target}"]',
                                           target_position={"x": 60, "y": 20})
                        expect(page.locator("#toast")).to_have_text("Line item moved.", timeout=15000)

                        page.evaluate("refresh()")   # what polling does after any background save
                        expect(page.locator('#spec-groups input.cell[value="Autosaved edit"]')).to_have_count(1)
                        expect(page.locator('#spec-groups input.cell[value="Retry me"]')).to_have_count(1)

                        page.reload()
                        page.locator('[data-view="spec"]').click()
                        expect(page.locator('#spec-groups input.cell[value="Autosaved edit"]')).to_have_count(1)
                        expect(page.locator('#spec-groups input.cell[value="Retry me"]')).to_have_count(1)
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_a_column_width_set_from_its_header_is_kept(self):
        """Long brand, model and finish names are what the sheet is for, so every header
        carries a grip that sets its column's width. A width set once holds: the same in
        every group card, through a re-render, and after a reload."""
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "widths.xlsx"
            shutil.copy2(app.common.WORKSPACE, workspace)
            with patch.object(app, "store", WorkspaceStore(workspace)):
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1500, "height": 1100})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        expect(page.locator("#project-details-panel")).to_be_visible()

                        def open_sheet():
                            page.locator('[data-view="spec"]').click()
                            expect(page.locator("#spec-view")).to_be_visible()
                            expect(page.locator(".spec-table tr[data-row]").first).to_be_attached()

                        def model_width():
                            return page.locator('.spec-table th:has(.col-grip[data-grip="Model #"])').first.bounding_box()["width"]

                        open_sheet()
                        grip = page.locator('.spec-table .col-grip[data-grip="Model #"]').first
                        opened = model_width()
                        box = grip.bounding_box()
                        middle = box["y"] + box["height"] / 2
                        page.mouse.move(box["x"] + box["width"] / 2, middle)
                        page.mouse.down()
                        page.mouse.move(box["x"] + box["width"] / 2 + 90, middle, steps=8)
                        page.mouse.up()
                        widened = model_width()
                        self.assertAlmostEqual(widened, opened + 90, delta=3)
                        # one grid, not one per card: every group card reads the same width
                        widths = page.evaluate("""[...document.querySelectorAll('.spec-table col[data-col="Model #"]')].map((c) => c.style.width)""")
                        self.assertGreater(len(widths), 0)
                        self.assertEqual(set(widths), {f"{round(widened)}px"})

                        # a re-render must not put the column back where it started
                        page.locator("#spec-filter").fill("a")
                        page.locator("#spec-filter").fill("")
                        expect(page.locator(".spec-table tr[data-row]").first).to_be_attached()
                        self.assertAlmostEqual(model_width(), widened, delta=1)

                        page.reload()
                        expect(page.locator("#project-details-panel")).to_be_visible()
                        open_sheet()
                        self.assertAlmostEqual(model_width(), widened, delta=1)
                        self.assertEqual(page.evaluate("JSON.parse(localStorage.getItem('uh-spec-widths'))['Model #']"),
                                         round(widened))

                        # the grip's own double-click is the only way back to the opening width
                        page.locator('.spec-table .col-grip[data-grip="Model #"]').first.dblclick()
                        self.assertAlmostEqual(model_width(), opened, delta=1)
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_spec_sheet_groups_by_room_or_category(self):
        """The toolbar offers the two groupings the view has: by room, which it opens on, and
        by category. Grouping by location was removed by request, and the chosen heading is
        what the downloads print under."""
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "groups.xlsx"
            shutil.copy2(app.common.WORKSPACE, workspace)
            with patch.object(app, "store", WorkspaceStore(workspace)):
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1500, "height": 1100})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        expect(page.locator("#project-details-panel")).to_be_visible()
                        page.locator('[data-view="spec"]').click()
                        expect(page.locator("#spec-view")).to_be_visible()
                        chips = page.locator("#spec-view .chip")
                        expect(chips).to_have_text(["By room", "By category"])
                        headings = page.locator(".spec-card-head h3")

                        expect(chips.nth(0)).to_have_class("chip active")
                        rooms = list(dict.fromkeys(page.evaluate("projectRows().map((r) => r['Room / Area'] || 'Unassigned')")))
                        expect(headings).to_have_text(rooms)   # rooms keep the order of the line items
                        expect(page.locator("#spec-summary")).to_contain_text(f"{len(rooms)} room")
                        self.assertIn("group=Room%20%2F%20Area", page.evaluate("specQuery()"))

                        page.get_by_role("button", name="By category").click()
                        expect(chips.nth(1)).to_have_class("chip active")
                        expect(chips.nth(0)).to_have_class("chip")
                        order = page.evaluate("state.data.sections")
                        sections = list(dict.fromkeys(page.evaluate("projectRows().map((r) => r.Section || 'Other')")))
                        # categories print in the order the Lists sheet names them
                        expect(headings).to_have_text(sorted(sections, key=lambda s: order.index(s) + 1 if s in order else 99))
                        expect(page.locator("#spec-summary")).to_contain_text(f"{len(sections)} categor")
                        self.assertIn("group=Section", page.evaluate("specQuery()"))
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_a_column_switched_off_is_left_out_of_every_download(self):
        """Not every sheet is sent out for the same reason, so the Import & export dialog can
        switch any column off first. Nothing is hidden to begin with, the choice is this
        browser's and holds across a reload, and losing a column the import matches a
        returning sheet on says so rather than being refused."""
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "columns.xlsx"
            shutil.copy2(app.common.WORKSPACE, workspace)
            with patch.object(app, "store", WorkspaceStore(workspace)):
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1500, "height": 1100})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        expect(page.locator("#project-details-panel")).to_be_visible()
                        warning = page.locator("#export-columns-warning")

                        def open_files():
                            page.locator("#files-button").click()
                            expect(page.locator("#files-dialog")).to_be_visible()

                        def csv_header():
                            """The columns of a CSV downloaded from the open dialog, which closes it."""
                            with page.expect_download(timeout=30000) as downloading:
                                page.locator('#files-dialog [data-file="csv"]').click()
                            return Path(downloading.value.path()).read_text(encoding="utf-8-sig").splitlines()[0].split(",")

                        open_files()
                        expect(page.locator("#export-column-list input[data-column]")).to_have_count(len(app.EXPORT_COLUMNS))
                        expect(page.locator("#export-column-list input:not(:checked)")).to_have_count(0)
                        expect(page.locator("#export-columns-summary")).to_contain_text("Every column")
                        expect(warning).to_be_hidden()
                        self.assertIn("Unit Price", csv_header())

                        # 'Model #' is a fragment to a URL and 'Markup %' an escape: both have to
                        # reach the server as the names of real columns, or it refuses the lot.
                        open_files()
                        for column in ["Model #", "Markup %", "Unit Price"]:
                            page.locator(f'#export-column-list input[data-column="{column}"]').uncheck()
                        expect(page.locator("#export-columns-summary")).to_contain_text("3 left out")
                        expect(warning).to_be_hidden()
                        fields = csv_header()
                        self.assertNotIn("Model #", fields)
                        self.assertNotIn("Markup %", fields)
                        self.assertNotIn("Unit Price", fields)
                        self.assertIn("Item", fields)

                        open_files()
                        page.locator('#export-column-list input[data-column="Item"]').uncheck()
                        expect(warning).to_contain_text("Item name")
                        page.locator('#export-column-list input[data-column="Item"]').check()
                        expect(warning).to_be_hidden()
                        page.keyboard.press("Escape")

                        page.reload()
                        expect(page.locator("#project-details-panel")).to_be_visible()
                        open_files()
                        expect(page.locator('#export-column-list input[data-column="Model #"]')).not_to_be_checked()
                        self.assertIn("hide=Model%20%23%2CUnit%20Price%2CMarkup%20%25", page.evaluate("specQuery()"))

                        page.get_by_role("button", name="Switch them all back on").click()
                        expect(page.locator("#export-column-list input:not(:checked)")).to_have_count(0)
                        self.assertNotIn("hide=", page.evaluate("specQuery()"))
                        self.assertIn("Model #", csv_header())
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_the_orientation_chosen_in_the_dialog_is_the_page_that_is_printed(self):
        """The Word and PDF copies turn on request: landscape as the dialog opens, portrait
        for a sheet filed with the client schedule. The choice is this browser's and holds
        across a reload, the file says which way round it is, and a CSV never carries it."""
        active = re.compile(r"\bactive\b")
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "orientation.xlsx"
            shutil.copy2(app.common.WORKSPACE, workspace)
            with patch.object(app, "store", WorkspaceStore(workspace)):
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1500, "height": 1100})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        expect(page.locator("#project-details-panel")).to_be_visible()
                        landscape = page.locator('#files-dialog [data-orientation="landscape"]')
                        portrait = page.locator('#files-dialog [data-orientation="portrait"]')

                        def open_files():
                            page.locator("#files-button").click()
                            expect(page.locator("#files-dialog")).to_be_visible()

                        def downloaded(choice):
                            """The name of a file downloaded from the open dialog, which closes it."""
                            with page.expect_download(timeout=30000) as downloading:
                                page.locator(f'#files-dialog [data-file="{choice}"]').click()
                            return downloading.value.suggested_filename

                        open_files()
                        expect(landscape).to_have_class(active)
                        self.assertTrue(downloaded("spec-pdf").endswith("_Landscape.pdf"))

                        open_files()
                        portrait.click()
                        expect(portrait).to_have_class(active)
                        expect(landscape).not_to_have_class(active)
                        expect(page.locator("#files-dialog")).to_be_visible()   # a chip is not a download
                        self.assertTrue(downloaded("spec-pdf").endswith("_Portrait.pdf"))

                        page.reload()
                        expect(page.locator("#project-details-panel")).to_be_visible()
                        open_files()
                        expect(portrait).to_have_class(active)
                        self.assertIn("orientation=portrait", page.evaluate("docQuery()"))
                        self.assertNotIn("orientation", page.evaluate("specQuery()"))
                        self.assertTrue(downloaded("spec-docx").endswith("_Portrait.docx"))
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_spec_product_link_shows_link_text(self):
        """A pasted address renders as a "Product Link" anchor; the pencil button
        reopens the input so the stored URL can be viewed or changed."""
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "link.xlsx"
            shutil.copy2(app.common.WORKSPACE, workspace)
            with patch.object(app, "store", WorkspaceStore(workspace)):
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1500, "height": 1100})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        expect(page.locator("#project-details-panel")).to_be_visible()
                        page.locator('[data-view="spec"]').click()
                        expect(page.locator("#spec-view")).to_be_visible()

                        url = "https://example.com/catalog/faucet"
                        row = page.locator(".spec-table tr[data-row]").first.get_attribute("data-row")
                        cell = page.locator(f'tr[data-row="{row}"] .link-col')
                        if cell.locator(".link-edit").count():
                            cell.locator(".link-edit").click()   # the row already links somewhere
                        cell.locator("input.cell").fill(url)
                        cell.locator("input.cell").blur()
                        link = page.locator(f'tr[data-row="{row}"] a.product-link')
                        expect(link).to_have_text("Product Link ↗")
                        expect(link).to_have_attribute("href", url)
                        expect(link).to_have_attribute("title", url)
                        expect(cell.locator("input.cell")).to_have_count(0)

                        cell.locator(".link-edit").click()
                        expect(cell.locator("input.cell")).to_have_value(url)
                        cell.locator("input.cell").blur()
                        expect(link).to_have_text("Product Link ↗")

                        page.reload()
                        page.locator('[data-view="spec"]').click()
                        expect(link).to_have_text("Product Link ↗")
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_project_selection_review_and_download_flow(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "test.xlsx"
            shutil.copy2(app.common.WORKSPACE, workspace)
            with patch.object(app, "store", WorkspaceStore(workspace)):
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on("dialog", lambda dialog: dialog.accept())
                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        expect(page.locator("#project-name")).to_contain_text("Magnolia")
                        output = app.BASE / "output"
                        output.mkdir(exist_ok=True)
                        page.wait_for_function("Array.from(document.querySelectorAll('img[src^=\"/brand/\"]')).every(img => img.complete && img.naturalWidth > 0)")
                        expect(page.locator(".brand-wordmark")).to_be_visible()
                        ratio = page.locator(".brand-wordmark").evaluate("img => (img.getBoundingClientRect().width / img.getBoundingClientRect().height) / (img.naturalWidth / img.naturalHeight)")
                        self.assertAlmostEqual(ratio, 1, places=2)
                        page.screenshot(path=str(output / "studio-desktop.png"), full_page=True)
                        page.get_by_role("button", name="Add project", exact=True).click()
                        editor = page.locator("#editor")
                        editor.get_by_label("Project ID").fill("BROWSER-TEST")
                        editor.get_by_label("Project Name").fill("Browser Test Residence")
                        editor.get_by_label("Client Name").fill("Test Client")
                        editor.get_by_label("Address").fill("Test address")
                        editor.get_by_role("button", name="Save changes").click()
                        expect(editor).not_to_be_visible()
                        expect(page.locator("#project-name")).to_have_text("Browser Test Residence")
                        page.get_by_role("button", name="Add selection", exact=False).click()
                        editor.locator('input[name="Item"]').fill("Test Kitchen Faucet")
                        editor.get_by_label("Manufacturer", exact=True).fill("Test Manufacturer")
                        editor.get_by_label("Model #", exact=True).fill("00123-VS")
                        editor.get_by_label("Finish / Color").fill("Stainless")
                        editor.get_by_label("Qty / Unit", exact=True).fill("2 EA")
                        editor.get_by_role("button", name="Save changes").click()
                        expect(editor).not_to_be_visible()
                        # a new project is seeded with the standard template, so filter to the new row
                        page.locator("#search").fill("Test Kitchen Faucet")
                        expect(page.locator("#selection-rows tr")).to_have_count(1)
                        expect(page.locator("#selection-rows")).to_contain_text("00123-VS")
                        page.locator("#setup-button").click()   # the review view is gone; setup opens from the sidebar
                        expect(page.locator("#setup-dialog")).to_be_visible()
                        page.locator("#setup-dialog").get_by_role("button", name="Close dialog").click()
                        page.get_by_role("button", name="Edit Test Kitchen Faucet").click()
                        editor.get_by_label("Product URL", exact=True).fill("https://example.com/product")
                        editor.get_by_role("button", name="Save changes").click()
                        expect(editor).not_to_be_visible()
                        expect(page.locator("#selection-rows")).to_contain_text("Product Link")
                        # the spec sheet is the deliverable now, downloaded from the files dialog
                        page.locator("#files-button").click()
                        with page.expect_download(timeout=30000) as download_info:
                            page.locator('#files-dialog [data-file="spec-pdf"]').click()
                        download = download_info.value
                        self.assertTrue(download.suggested_filename.endswith(".pdf"))
                        self.assertIn("Spec_Sheet", download.suggested_filename)
                        download.save_as(Path(folder) / "download.pdf")
                        page.locator("#files-button").click()
                        with page.expect_download(timeout=30000) as download_info:
                            page.locator('#files-dialog [data-file="csv"]').click()
                        download = download_info.value
                        self.assertTrue(download.suggested_filename.endswith(".csv"))
                        download.save_as(Path(folder) / "download.csv")
                        expect(page.locator("#busy-banner")).not_to_be_visible()
                        page.get_by_role("button", name="Edit Test Kitchen Faucet").click()
                        editor.get_by_label("Model #", exact=True).fill("NEW-00123")
                        editor.get_by_role("button", name="Save changes").click()
                        expect(editor).not_to_be_visible()
                        # changing the model clears the link that described the old product
                        expect(page.locator("#selection-rows")).to_contain_text("Not linked")
                        page.locator('[data-project="UH-101"]').click()
                        expect(page.locator("#selection-rows")).not_to_contain_text("Test Kitchen Faucet")
                        page.locator("#search").fill("7594ESRS")
                        expect(page.locator("#selection-rows tr")).to_have_count(1)
                        page.locator("#search").fill("")
                        page.set_viewport_size({"width": 390, "height": 844})
                        page.screenshot(path=str(output / "studio-mobile.png"), full_page=True)
                        self.assertTrue(page.get_by_role("button", name="Add project", exact=True).is_visible())
                        overflow = page.evaluate("Array.from(document.querySelectorAll('body *')).filter(e => e.getBoundingClientRect().right > 391).map(e => ({tag:e.tagName, id:e.id, cls:e.className, position:getComputedStyle(e).position, right:e.getBoundingClientRect().right})).filter(e => e.position === 'absolute' || e.position === 'fixed')")
                        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390, overflow)
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_a_save_that_lands_second_retries_rather_than_blaming_the_user(self):
        """Two quick edits make the second carry the revision the first just replaced. The
        person did not cause that race, so the save takes the new revision and goes again
        instead of reporting a conflict."""
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "conflict.xlsx"
            shutil.copy2(app.common.WORKSPACE, workspace)
            with patch.object(app, "store", WorkspaceStore(workspace)):
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1500, "height": 1100})
                        errors, refused = [], []
                        page.on("pageerror", lambda error: errors.append(str(error)))

                        def conflict_once(route):
                            if route.request.method == "POST" and not refused:
                                refused.append(route.request.url)
                                route.fulfill(status=409, content_type="application/json",
                                              body='{"error": "This page is out of date."}')
                            else:
                                route.continue_()

                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        expect(page.locator("#project-details-panel")).to_be_visible()
                        page.locator('[data-view="spec"]').click()
                        expect(page.locator("#spec-view")).to_be_visible()
                        page.route("**/api/selections", conflict_once)
                        cell = page.locator("#spec-groups input.cell").first
                        cell.fill("Retried finish")
                        cell.blur()
                        expect(page.locator("#toast")).to_have_text("Saved.", timeout=20000)
                        expect(page.locator("#spec-groups .cell.dirty")).to_have_count(0)
                        self.assertEqual(len(refused), 1, "the first save should have been refused once")
                        page.unroute("**/api/selections")
                        page.reload()
                        expect(page.locator("#spec-groups input.cell").first).to_have_value("Retried finish")
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_a_failed_first_load_recovers_on_the_next_poll(self):
        """A workspace must not stay empty because one request landed on a bad moment: the
        first /api/state fails here, and the poll two and a half seconds later fills it in."""
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "recover.xlsx"
            shutil.copy2(app.common.WORKSPACE, workspace)
            with patch.object(app, "store", WorkspaceStore(workspace)):
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1440, "height": 1000})
                        errors, calls = [], []
                        page.on("pageerror", lambda error: errors.append(str(error)))

                        def fail_once(route):
                            calls.append(route.request.url)
                            if len(calls) == 1:
                                route.fulfill(status=500, content_type="application/json",
                                              body='{"error": "The operation could not be completed."}')
                            else:
                                route.continue_()

                        page.route("**/api/state", fail_once)
                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        expect(page.locator("#load-error")).to_be_visible()
                        expect(page.locator(".project-choice").first).to_be_visible(timeout=20000)
                        expect(page.locator("#load-error")).not_to_be_visible()
                        expect(page.locator("#project-name")).not_to_be_empty()
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_a_cleared_value_is_not_refilled_by_a_read_that_straddled_a_save(self):
        """A read answered just before a save describes the workspace as it used to be, so
        drawing it puts a value somebody cleared back on the screen. A read that straddles a
        write is discarded instead."""
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "cleared.xlsx"
            shutil.copy2(app.common.WORKSPACE, workspace)
            with patch.object(app, "store", WorkspaceStore(workspace)):
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1500, "height": 1100})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        expect(page.locator("#project-details-panel")).to_be_visible()
                        page.locator('[data-view="spec"]').click()
                        expect(page.locator("#spec-view")).to_be_visible()
                        row = int(page.locator(".spec-table tr[data-row]").first.get_attribute("data-row"))
                        held = page.evaluate(
                            """async (row) => {
                                 await api('/api/selections', { row, values: { 'Unit Price': '50' } });
                                 await refresh();
                                 const stale = await api('/api/state');   // a body that still carries the 50
                                 await api('/api/selections', { row, values: { 'Unit Price': '' } });
                                 await refresh();                         // the page now holds the cleared row
                                 const real = window.fetch;
                                 // the poll's read is answered slowly, and with that older body
                                 window.fetch = (path, options) => String(path).includes('/api/state')
                                   ? new Promise((done) => setTimeout(() => done(new Response(
                                       JSON.stringify(stale),
                                       { status: 200, headers: { 'Content-Type': 'application/json' } })), 100))
                                   : real(path, options);
                                 const poll = pollOnce();                 // the read goes out ...
                                 const write = api('/api/selections', { row, values: { 'Markup %': '1' } });
                                 await poll;                              // ... and answers after the save started
                                 const seen = state.data.selections.find((r) => r._row === row)['Unit Price'];
                                 await write.catch(() => {});
                                 window.fetch = real;
                                 return seen;
                               }""", row)
                        self.assertEqual(held, "", "a read older than the save refilled the cleared price")
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_other_hands_a_dropdown_over_to_a_typed_value(self):
        """Section and Location are lists that cannot name every home: choosing "Other" gives
        way to an empty box, what is typed there is saved on the line item, and a value the
        list does not hold comes back in that box. An empty box keeps the stored value."""
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "other.xlsx"
            shutil.copy2(app.common.WORKSPACE, workspace)
            with patch.object(app, "store", WorkspaceStore(workspace)):
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1440, "height": 1000})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        expect(page.locator("#project-details-panel")).to_be_visible()
                        page.get_by_role("button", name="Add selection", exact=False).click()
                        editor = page.locator("#editor")
                        section, location = 'input[name="Section"]', 'input[name="Location"]'
                        editor.locator('input[name="Item"]').fill("Media Rack")
                        expect(editor.locator(section)).to_be_hidden()
                        editor.locator('select[name="Section"]').select_option("Other")
                        expect(editor.locator(section)).to_be_visible()
                        expect(editor.locator(section)).to_have_value("")
                        expect(editor.locator('select[name="Section"]')).to_be_hidden()
                        editor.locator(section).fill("Audio / Visual")
                        editor.locator('select[name="Location"]').select_option("Other")
                        editor.locator(location).fill("Media Room")
                        output = app.BASE / "output"
                        output.mkdir(exist_ok=True)
                        page.screenshot(path=str(output / "other-typed-value.png"))
                        editor.get_by_role("button", name="Save changes").click()
                        expect(editor).not_to_be_visible()
                        page.locator("#search").fill("Media Rack")
                        expect(page.locator("#selection-rows tr")).to_have_count(1)
                        expect(page.locator("#selection-rows")).to_contain_text("Audio / Visual")
                        expect(page.locator("#selection-rows")).to_contain_text("Media Room")
                        # the typed values come back for editing, not as a dropdown that lost them
                        page.get_by_role("button", name="Edit Media Rack").click()
                        expect(editor.locator(section)).to_have_value("Audio / Visual")
                        expect(editor.locator(location)).to_have_value("Media Room")
                        editor.locator(section).fill("")
                        editor.get_by_role("button", name="Save changes").click()
                        expect(editor).not_to_be_visible()
                        expect(page.locator("#selection-rows")).to_contain_text("Audio / Visual")
                        # and the list is one click away again
                        page.get_by_role("button", name="Edit Media Rack").click()
                        editor.get_by_role("button", name="Choose from the list").first.click()
                        expect(editor.locator('select[name="Section"]')).to_be_visible()
                        expect(editor.locator(section)).to_be_hidden()
                        page.locator("#editor").get_by_role("button", name="Cancel").click()
                        expect(editor).not_to_be_visible()
                        # a custom value joins the filters, so the row stays reachable
                        page.locator("#search").fill("")
                        page.locator("#section-filter").select_option("Audio / Visual")
                        expect(page.locator("#selection-rows tr")).to_have_count(1)
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_a_group_heading_is_renamed_from_its_pencil(self):
        """A card's heading is the grouped field's own value, so renaming it rewrites the room
        on every line item under it. Escape abandons the edit, and clicking away leaves the
        heading as it was rather than saving a half-typed name."""
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "heading.xlsx"
            shutil.copy2(app.common.WORKSPACE, workspace)
            with patch.object(app, "store", WorkspaceStore(workspace)):
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1500, "height": 1100})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on("dialog", lambda dialog: dialog.accept())
                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        expect(page.locator("#project-details-panel")).to_be_visible()
                        page.locator('[data-view="spec"]').click()
                        expect(page.locator("#spec-view")).to_be_visible()

                        card = page.locator(".spec-card").first
                        # textContent, not innerText: the heading is uppercased by CSS, and the
                        # stored room is "Whole House" rather than "WHOLE HOUSE".
                        before = card.locator(".spec-card-head h3").text_content().strip()
                        rooms = page.evaluate(
                            """(name) => projectRows().filter((r) => (r["Room / Area"] || "Unassigned") === name).length""",
                            before)
                        self.assertTrue(rooms, "the first card needs line items to rename")

                        # Escape abandons the edit
                        card.locator(".spec-rename").click()
                        box = card.locator(".group-rename")
                        expect(box).to_be_focused()
                        box.fill("Thrown away")
                        page.keyboard.press("Escape")
                        expect(card.locator(".spec-card-head h3")).to_have_text(before, ignore_case=True)

                        # and a committed name moves every line item under it
                        card.locator(".spec-rename").click()
                        page.locator(".group-rename").fill("Whole Home")
                        page.keyboard.press("Enter")
                        expect(page.locator("#toast")).to_contain_text(f"{rooms} line item")
                        expect(page.locator(".spec-card-head h3").first).to_have_text("Whole Home", ignore_case=True)
                        self.assertEqual(page.evaluate(
                            """() => projectRows().filter((r) => r["Room / Area"] === "Whole Home").length"""), rooms)
                        self.assertEqual(page.evaluate(
                            """(name) => projectRows().filter((r) => r["Room / Area"] === name).length""",
                            before), 0)
                        # it is a real workspace edit, not only on screen
                        page.reload()
                        page.locator('[data-view="spec"]').click()
                        expect(page.locator(".spec-card-head h3").first).to_have_text("Whole Home", ignore_case=True)
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_a_project_is_renumbered_from_its_editor_and_the_next_one_follows(self):
        """Homes are numbered in one sequence: editing a Project ID renumbers the project and
        takes its line items with it, and adding a project opens on the next free number
        instead of an empty box."""
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "renumber.xlsx"
            shutil.copy2(app.common.WORKSPACE, workspace)
            with patch.object(app, "store", WorkspaceStore(workspace)):
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1440, "height": 1000})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on("dialog", lambda dialog: dialog.accept())
                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        expect(page.locator("#project-details-panel")).to_be_visible()

                        before = page.evaluate("state.project")
                        owned = page.evaluate("projectRows().length")
                        self.assertTrue(owned, "the open project needs line items to move")
                        editor = page.locator("#editor")
                        page.locator("#edit-project").click()
                        expect(editor).to_be_visible()
                        field = editor.locator('input[name="Project ID"]')
                        expect(field).to_have_value(before)
                        expect(field).not_to_have_attribute("readonly", "")   # renaming is allowed now
                        field.fill("UH-001")
                        editor.get_by_role("button", name="Save changes").click()
                        expect(editor).not_to_be_visible()
                        expect(page.locator("#toast")).to_contain_text("Renumbered to UH-001")
                        expect(page.locator("#project-list")).to_contain_text("UH-001")
                        expect(page.locator("#project-list")).not_to_contain_text(before)
                        self.assertEqual(page.evaluate("state.project"), "UH-001")
                        # the line items came with it, rather than being orphaned by the rename
                        self.assertEqual(page.evaluate("projectRows().length"), owned)
                        self.assertEqual(page.evaluate(
                            """(pid) => state.data.selections.filter((r) => r["Project ID"] === pid).length""",
                            before), 0, "no line item may be left behind under the old ID")

                        # a new project opens on the next number after the highest one left
                        ids = page.evaluate("""state.data.projects.map((p) => p["Project ID"])""")
                        numbers = [int(match.group(1)) for pid in ids
                                   if (match := re.fullmatch(r"UH-0*(\d+)", pid or ""))]
                        page.locator("#new-project").click()
                        expect(editor).to_be_visible()
                        expect(editor.locator('input[name="Project ID"]')).to_have_value(
                            f"UH-{max(numbers) + 1:03d}")
                        page.keyboard.press("Escape")
                        expect(editor).not_to_be_visible()
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_a_restarting_host_is_waited_out_rather_than_reported(self):
        """The hosted workspace is one process, so a deploy leaves a few seconds where the
        router answers 502 and nothing is behind the address. A read is asked again, so the
        poll and every refresh ride it out. A write is not retried - a delete names a row by
        number - so it says what happened and the edit stays held for Save."""
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "gateway.xlsx"
            shutil.copy2(app.common.WORKSPACE, workspace)
            with patch.object(app, "store", WorkspaceStore(workspace)):
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1500, "height": 1100})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        expect(page.locator("#project-details-panel")).to_be_visible()
                        page.locator('[data-view="spec"]').click()
                        expect(page.locator("#spec-view")).to_be_visible()
                        cell = page.locator(".spec-table tr[data-row] input.cell").first
                        expect(cell).to_be_attached()
                        cell.click()   # the poll leaves a cell being edited alone, so reads are ours

                        reads = []

                        def restarting(route):
                            reads.append(route.request.url)
                            if len(reads) <= 2:   # the gap while the new instance comes up
                                route.fulfill(status=502, content_type="text/html",
                                              body="<html><body>Bad Gateway</body></html>")
                            else:
                                route.continue_()

                        page.route("**/api/state", restarting)
                        page.evaluate("refresh()")        # resolves only because it is retried
                        self.assertGreaterEqual(len(reads), 3)
                        expect(page.locator("#load-error")).to_be_hidden()
                        expect(page.locator("#toast")).to_be_hidden()
                        page.unroute("**/api/state")

                        # a write is not sent twice; it explains itself and stays in hand
                        writes = []

                        def refuse(route):
                            writes.append(route.request.url)
                            route.fulfill(status=502, content_type="text/html",
                                          body="<html><body>Bad Gateway</body></html>")

                        page.route("**/api/selections", refuse)
                        row = page.locator(".spec-table tr[data-row]").first.get_attribute("data-row")
                        target = page.locator(f'tr[data-row="{row}"] input.cell').nth(1)
                        target.fill("Mid deploy")
                        target.blur()
                        expect(page.locator("#toast")).to_contain_text("restarting", timeout=15000)
                        self.assertEqual(len(writes), 1, "a write must not be retried by itself")
                        expect(page.locator("#spec-save")).to_have_text("Save changes (1)")
                        expect(page.locator("#spec-groups .cell.dirty")).to_have_value("Mid deploy")
                        page.unroute("**/api/selections")
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_clicking_away_from_a_dialog_closes_it(self):
        """Clicking outside a modal dismisses it, the way Escape does, for every dialog the
        app opens. A click on the dialog's own padding is not "outside", and neither is a
        drag that began in a field and finished past the edge: a half-filled form must not be
        thrown away by selecting text."""
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "dismiss.xlsx"
            shutil.copy2(app.common.WORKSPACE, workspace)
            with patch.object(app, "store", WorkspaceStore(workspace)):
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1440, "height": 1000})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        expect(page.locator("#project-details-panel")).to_be_visible()

                        def click_away(dialog):
                            box = dialog.bounding_box()   # the backdrop, half way up the left margin
                            page.mouse.click(box["x"] / 2, box["y"] + box["height"] / 2)

                        for opener, selector in [("#files-button", "#files-dialog"),
                                                 ("#setup-button", "#setup-dialog"),
                                                 ("#add-selection", "#editor")]:
                            dialog = page.locator(selector)
                            page.locator(opener).click()
                            expect(dialog).to_be_visible()
                            click_away(dialog)
                            expect(dialog).not_to_be_visible()

                        editor = page.locator("#editor")
                        page.locator("#add-selection").click()
                        expect(editor).to_be_visible()
                        box = editor.bounding_box()
                        page.mouse.click(box["x"] + 4, box["y"] + box["height"] / 2)   # its own padding
                        expect(editor).to_be_visible()

                        item = editor.locator('input[name="Item"]')
                        item.fill("Half typed")
                        field = item.bounding_box()
                        page.mouse.move(field["x"] + 10, field["y"] + field["height"] / 2)
                        page.mouse.down()
                        page.mouse.move(box["x"] / 2, field["y"], steps=6)   # a selection released past the edge
                        page.mouse.up()
                        expect(editor).to_be_visible()
                        expect(item).to_have_value("Half typed")

                        click_away(editor)
                        expect(editor).not_to_be_visible()
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()


if __name__ == "__main__":
    unittest.main()
