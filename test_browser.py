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


@unittest.skipIf(sync_playwright is None, "Install requirements-dev.txt to run browser tests.")
class BrowserTests(unittest.TestCase):
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
                        expect(page.locator("#automation-banner")).to_be_visible()
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
                        with page.expect_download(timeout=30000) as download_info:
                            page.locator("#export-pdf").click()
                        path = Path(folder) / "automatic.pdf"
                        download_info.value.save_as(path)
                        self.assertIn(b"https://example.com/products/AUTO-TWO", path.read_bytes())
                        self.assertNotIn(b"https://example.com/products/AUTO-ONE", path.read_bytes())
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
                        expect(page.locator(".preview-top img")).to_be_visible()
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
                        editor.get_by_label("Item", exact=False).fill("Test Kitchen Faucet")
                        editor.get_by_label("Manufacturer", exact=True).fill("Test Manufacturer")
                        editor.get_by_label("Model #", exact=True).fill("00123-VS")
                        editor.get_by_label("Finish / Color").fill("Stainless")
                        editor.get_by_label("Qty", exact=True).fill("2")
                        editor.get_by_role("button", name="Save changes").click()
                        expect(editor).not_to_be_visible()
                        expect(page.locator("#selection-rows")).to_contain_text("00123-VS")
                        expect(page.locator("#selection-rows tr")).to_have_count(1)
                        page.locator('[data-view="review"]').click()
                        page.get_by_role("button", name="Find product", exact=True).click()
                        expect(page.locator("#setup-dialog")).to_be_visible()
                        expect(page.locator("#provider-status")).to_contain_text("not configured")
                        page.locator("#setup-dialog").get_by_role("button", name="Close dialog").click()
                        page.get_by_role("button", name="Edit details").click()
                        editor.get_by_label("Product URL", exact=True).fill("https://example.com/product")
                        editor.get_by_role("button", name="Save changes").click()
                        expect(editor).not_to_be_visible()
                        page.get_by_role("button", name="Mark verified").click()
                        expect(page.locator("#review-grid")).to_contain_text("Everything checked")
                        page.locator('[data-view="presentation"]').click()
                        page.locator("#export-mode").select_option("final")
                        for button, suffix in [("#export-pdf", ".pdf"), ("#export-pptx", ".pptx")]:
                            with page.expect_download(timeout=30000) as download_info:
                                page.locator(button).click()
                            download = download_info.value
                            self.assertTrue(download.suggested_filename.endswith(suffix))
                            download.save_as(Path(folder) / f"download{suffix}")
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
