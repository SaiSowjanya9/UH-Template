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
            with patch.object(app, "store", WorkbookStore(workbook)):
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
            workbook = Path(folder) / "spec.xlsx"
            shutil.copy2(app.common.WORKBOOK, workbook)
            with patch.object(app, "store", WorkbookStore(workbook)):
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

    def test_spec_sheet_groups_by_room_or_category(self):
        """The toolbar offers the two groupings the view has: by room, which it opens on, and
        by category. Grouping by location was removed by request, and the chosen heading is
        what the downloads print under."""
        with tempfile.TemporaryDirectory() as folder:
            workbook = Path(folder) / "groups.xlsx"
            shutil.copy2(app.common.WORKBOOK, workbook)
            with patch.object(app, "store", WorkbookStore(workbook)):
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

    def test_spec_product_link_shows_link_text(self):
        """A pasted address renders as a "Product Link" anchor; the pencil button
        reopens the input so the stored URL can be viewed or changed."""
        with tempfile.TemporaryDirectory() as folder:
            workbook = Path(folder) / "link.xlsx"
            shutil.copy2(app.common.WORKBOOK, workbook)
            with patch.object(app, "store", WorkbookStore(workbook)):
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
            workbook = Path(folder) / "test.xlsx"
            shutil.copy2(app.common.WORKBOOK, workbook)
            with patch.object(app, "store", WorkbookStore(workbook)):
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
                        editor.get_by_label("Qty", exact=True).fill("2")
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

    def test_other_hands_a_dropdown_over_to_a_typed_value(self):
        """Section and Location are lists that cannot name every home: choosing "Other" gives
        way to an empty box, what is typed there is saved on the line item, and a value the
        list does not hold comes back in that box. An empty box keeps the stored value."""
        with tempfile.TemporaryDirectory() as folder:
            workbook = Path(folder) / "other.xlsx"
            shutil.copy2(app.common.WORKBOOK, workbook)
            with patch.object(app, "store", WorkbookStore(workbook)):
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


if __name__ == "__main__":
    unittest.main()
