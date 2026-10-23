"""Reproduce spec-sheet drag-and-drop in a real browser against a temp workbook."""
import shutil
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from werkzeug.serving import make_server

import app
from workbook_store import WorkbookStore
from playwright.sync_api import sync_playwright

app.REQUIRE_LOGIN = False

folder = tempfile.mkdtemp()
workbook = Path(folder) / "t.xlsx"
shutil.copy2(app.common.WORKBOOK, workbook)
with patch.object(app, "store", WorkbookStore(workbook)), patch.object(app, "automation", None):
    server = make_server("127.0.0.1", 0, app.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port
    print("serving on", port)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.on("pageerror", lambda e: print("PAGEERROR:", e))
        page.on("console", lambda m: print("CONSOLE:", m.type, m.text) if m.type in ("error", "warning") else None)
        requests = []
        page.on("request", lambda r: requests.append((r.method, r.url)) if "order" in r.url else None)
        responses = []
        page.on("response", lambda r: responses.append((r.status, r.url)) if "order" in r.url else None)
        page.goto(f"http://127.0.0.1:{port}")
        page.wait_for_selector("#selection-rows tr, .project-choice", timeout=30000)
        page.wait_for_timeout(500)
        page.click('[data-view="spec"]')
        page.wait_for_selector("#spec-view:not([hidden])")
        page.wait_for_selector(".spec-table tr[data-row]", state="attached")
        rows = page.locator(".spec-table tr[data-row]")
        print("row count:", rows.count())

        def item_of(row):
            return row.locator("td").nth(1).locator("input").input_value()

        print("before:", [item_of(rows.nth(i)) for i in range(min(4, rows.count()))])

        src, dst = rows.nth(0), rows.nth(2)
        src_name = item_of(src)
        # a real pointer drag: press on the grip, move over target's lower half, release
        grip = src.locator(".drag-handle")
        box_s = grip.bounding_box()
        box_d = dst.bounding_box()
        page.mouse.move(box_s["x"] + box_s["width"] / 2, box_s["y"] + box_s["height"] / 2)
        page.mouse.down()
        page.mouse.move(box_d["x"] + box_d["width"] / 2, box_d["y"] + box_d["height"] * 0.75, steps=12)
        page.wait_for_timeout(300)
        page.mouse.up()
        page.wait_for_timeout(2500)
        rows = page.locator(".spec-table tr[data-row]")
        print("after :", [item_of(rows.nth(i)) for i in range(min(4, rows.count()))])
        print("order requests:", requests)
        print("order responses:", responses)
        toast = page.locator(".toast, #toast")
        if toast.count():
            for i in range(toast.count()):
                if toast.nth(i).is_visible():
                    print("toast:", toast.nth(i).inner_text())
        browser.close()
    server.shutdown()
print("done")
