import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.serving import make_server

import app
from workbook_store import WorkbookStore

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
            workbook = Path(folder) / "custom.xlsx"
            shutil.copy2(app.common.WORKBOOK, workbook)
            with patch.object(app, "store", WorkbookStore(workbook)), patch.object(app, "automation", None):
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

    def test_spec_sheet_manual_save_survives_reorder(self):
        """Manual mode holds edits in page state, so neither a drag (which renumbers
        workbook rows) nor a background refresh may lose them or move them to another row."""
        with tempfile.TemporaryDirectory() as folder:
            workbook = Path(folder) / "spec.xlsx"
            shutil.copy2(app.common.WORKBOOK, workbook)
            with patch.object(app, "store", WorkbookStore(workbook)), patch.object(app, "automation", None):
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

                        page.locator("#spec-autosave").click()
                        expect(page.locator("#spec-autosave")).to_have_text("Auto-save off")
                        expect(page.locator("#spec-save")).to_be_hidden()

                        moved = rows.first.get_attribute("data-row")
                        page.locator(f'tr[data-row="{moved}"] input.cell').first.fill("Held edit")
                        page.locator(f'tr[data-row="{moved}"] input.cell').first.blur()
                        expect(page.locator("#spec-save")).to_have_text("Save changes (1)")
                        self.assertEqual(saves, [])   # manual mode writes nothing yet

                        target = rows.nth(2).get_attribute("data-row")
                        page.drag_and_drop(f'tr[data-row="{moved}"] .drag-handle',
                                           f'tr[data-row="{target}"]',
                                           target_position={"x": 60, "y": 20})
                        expect(page.locator("#toast")).to_have_text("Line item moved.", timeout=15000)
                        expect(page.locator("#spec-save")).to_have_text("Save changes (1)")
                        held = page.locator("#spec-groups .cell.dirty")
                        expect(held).to_have_count(1)
                        expect(held).to_have_value("Held edit")

                        page.evaluate("refresh()")   # what polling does after any background save
                        expect(page.locator("#spec-groups .cell.dirty")).to_have_value("Held edit")

                        page.locator("#spec-save").click()
                        expect(page.locator("#spec-save")).to_be_hidden()
                        expect(page.locator("#spec-groups .cell.dirty")).to_have_count(0)
                        self.assertEqual(len(saves), 1, saves)   # one atomic batch, not one call per row
                        page.reload()
                        page.locator('[data-view="spec"]').click()
                        expect(page.locator('#spec-groups input.cell[value="Held edit"]')).to_have_count(1)
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
            workbook = Path(folder) / "link.xlsx"
            shutil.copy2(app.common.WORKBOOK, workbook)
            with patch.object(app, "store", WorkbookStore(workbook)), patch.object(app, "automation", None):
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

    def test_automatic_app_and_excel_edit_workflow(self):
        from auto_lookup import AutoLookup
        from unittest.mock import Mock
        from workbook_store import put
        with tempfile.TemporaryDirectory() as folder:
            workbook = Path(folder) / "test.xlsx"
            shutil.copy2(app.common.WORKBOOK, workbook)
            store = WorkbookStore(workbook)
            info = {"name": "brave", "ready": True, "key_name": "BRAVE_API_KEY"}
            search = Mock(side_effect=lambda provider, manufacturer, model, domain, **kwargs: {
                "url": f"https://example.com/products/{model}", "name": "Matched fixture", "image": "", "status": "Found - verify", "notes": ""})
            engine = AutoLookup(store, lambda: info, search=search)
            engine.observe(store.snapshot()[0])
            with patch.object(app, "store", store), patch.object(app, "automation", engine), patch("app.provider_info", return_value=info):
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                engine.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1440, "height": 1000})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        expect(page.locator("#selection-rows tr").first).to_be_attached()
                        page.get_by_role("button", name="Edit Siding", exact=True).click()
                        editor = page.locator("#editor")
                        editor.get_by_label("Model #", exact=True).fill("AUTO-ONE")
                        editor.get_by_label("Product Name", exact=True).fill("Automatic fixture")
                        editor.get_by_role("button", name="Save changes").click()
                        expect(editor).not_to_be_visible()
                        expect(page.locator('#selection-rows a[href="https://example.com/products/AUTO-ONE"]')).to_be_visible(timeout=20000)
                        self.assertEqual(search.call_count, 1)
                        self.assertEqual(search.call_args.kwargs["product_name"], "Automatic fixture")
                        with patch.object(engine, "excel_open", return_value=True):
                            with store.lock:
                                wb, revision = store.snapshot()
                                put(app.common.Sheet(wb["Selections"]), 2, {"Model #": "AUTO-TWO"})
                                store.save(wb, revision)
                            expect(page.locator("#selection-rows")).to_contain_text("Close Excel", timeout=20000)
                            expect(page.locator('#selection-rows a[href="https://example.com/products/AUTO-ONE"]')).to_have_count(0)
                            page.locator('[data-view="presentation"]').click()
                            expect(page.locator("#export-pdf")).to_be_disabled()
                            self.assertEqual(search.call_count, 1)
                        expect(page.locator("#export-pdf")).to_be_enabled(timeout=20000)
                        page.locator("#export-pdf").click()   # the PDF now goes through the details dialog
                        expect(page.locator("#schedule-dialog")).to_be_visible()
                        with page.expect_download(timeout=30000) as download_info:
                            page.locator("#schedule-download").click()
                        path = Path(folder) / "automatic.pdf"
                        download_info.value.save_as(path)
                        from pypdf import PdfReader
                        schedule = "\n".join(page.extract_text() for page in PdfReader(str(path)).pages)
                        self.assertIn("AUTO-TWO", schedule)
                        self.assertNotIn("AUTO-ONE", schedule)
                        self.assertEqual(search.call_count, 2)
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    engine.stop()
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_project_selection_review_and_download_flow(self):
        with tempfile.TemporaryDirectory() as folder:
            workbook = Path(folder) / "test.xlsx"
            shutil.copy2(app.common.WORKBOOK, workbook)
            with patch.object(app, "store", WorkbookStore(workbook)), patch("app.provider_info", return_value={"name": "brave", "ready": False, "key_name": "BRAVE_API_KEY"}):
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
                        page.locator('[data-view="presentation"]').click()
                        expect(page.locator(".sheet-head img")).to_be_visible()
                        expect(page.locator("#preview-rows")).to_contain_text("EX-01")
                        page.locator("#export-title").fill("Interior Selections")
                        page.locator("#export-prefix").fill("IN")
                        expect(page.locator("#preview-title")).to_have_text("Interior Selections")
                        expect(page.locator("#preview-rows")).to_contain_text("IN-01")
                        page.screenshot(path=str(output / "studio-presentation.png"), full_page=True)
                        page.locator('[data-view="selections"]').click()
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
                        editor.get_by_label("Qty", exact=True).fill("2")
                        editor.get_by_role("button", name="Save changes").click()
                        expect(editor).not_to_be_visible()
                        # a new project is seeded with the standard template, so filter to the new row
                        page.locator("#search").fill("Test Kitchen Faucet")
                        expect(page.locator("#selection-rows tr")).to_have_count(1)
                        expect(page.locator("#selection-rows")).to_contain_text("00123-VS")
                        page.locator("#setup-button").click()   # the review view is gone; setup opens from the sidebar
                        expect(page.locator("#setup-dialog")).to_be_visible()
                        expect(page.locator("#provider-status")).to_contain_text("not configured")
                        page.locator("#setup-dialog").get_by_role("button", name="Close dialog").click()
                        page.get_by_role("button", name="Edit Test Kitchen Faucet").click()
                        editor.get_by_label("Product URL", exact=True).fill("https://example.com/product")
                        editor.get_by_role("button", name="Save changes").click()
                        expect(editor).not_to_be_visible()
                        # verification moved into the row editor when the review grid was removed
                        page.get_by_role("button", name="Edit Test Kitchen Faucet").click()
                        page.locator("#editor-verify").click()
                        expect(page.locator("#selection-rows")).to_contain_text("Verified")
                        page.locator('[data-view="presentation"]').click()
                        # "final" would demand every seeded template row be verified too
                        page.locator("#export-mode").select_option("verified")
                        page.locator("#export-pdf").click()
                        expect(page.locator("#schedule-dialog")).to_be_visible()
                        with page.expect_download(timeout=30000) as download_info:
                            page.locator("#schedule-download").click()
                        download = download_info.value
                        self.assertTrue(download.suggested_filename.endswith(".pdf"))
                        self.assertIn("Interior_Selections", download.suggested_filename)
                        download.save_as(Path(folder) / "download.pdf")
                        expect(page.locator("#schedule-dialog")).not_to_be_visible()   # the dialog closes once the file lands
                        page.locator("#export-more").click()   # PowerPoint moved into the files dialog
                        with page.expect_download(timeout=30000) as download_info:
                            page.locator('#files-dialog [data-file="pptx"]').click()
                        download = download_info.value
                        self.assertTrue(download.suggested_filename.endswith(".pptx"))
                        self.assertIn("Interior_Selections", download.suggested_filename)
                        download.save_as(Path(folder) / "download.pptx")
                        expect(page.locator("#busy-banner")).not_to_be_visible()
                        page.locator('[data-view="selections"]').click()
                        page.get_by_role("button", name="Edit Test Kitchen Faucet").click()
                        editor.get_by_label("Model #", exact=True).fill("NEW-00123")
                        editor.get_by_role("button", name="Save changes").click()
                        expect(editor).not_to_be_visible()
                        expect(page.locator("#selection-rows")).to_contain_text("Not linked")
                        expect(page.locator("#selection-rows")).to_contain_text("Not run")
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


if __name__ == "__main__":
    unittest.main()
