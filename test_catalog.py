"""The design catalog (the clipper): collections, clipping, permissions, AI fallbacks."""
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import catalog
import common
from workspace_store import ValidationError


def picture(color=(180, 40, 30), size=(640, 420), kind="JPEG"):
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, kind)
    return buffer.getvalue()


PAGE_HTML = """<html><head><title>Fallback Title</title>
<meta property="og:title" content="Dream Kitchen">
<meta property="og:site_name" content="Design Site">
<meta property="og:description" content="A kitchen with a marble island">
<meta property="og:image" content="/images/hero.jpg">
</head><body>
<img src="/images/photo1.jpg" alt="Kitchen view" width="800" height="600">
<img src="/pixels/tiny.gif" width="1" height="1">
<img src="/icons/sprite.svg">
<img data-src="https://cdn.example.com/lazy.jpg" alt="Lazy loaded">
</body></html>"""


class AppTestCase(unittest.TestCase):
    """A test app over a temporary copy of the workspace, no login required."""

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
        self.login_patcher = patch.object(app, "REQUIRE_LOGIN", False)
        self.login_patcher.start()
        self.key_patcher = patch.dict("os.environ", {"UH_AI_KEY": ""})
        self.key_patcher.start()
        app.CLIP_THROTTLE.attempts.clear()
        self.client = app.app.test_client()
        self.headers = {"X-UH-Token": app.TOKEN}

    def tearDown(self):
        self.patcher.stop()
        self.login_patcher.stop()
        self.key_patcher.stop()
        self.folder.cleanup()

    def state(self):
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def post(self, path, data):
        return self.client.post(path, json={"revision": self.state()["revision"], **data}, headers=self.headers)

    def make_collection(self, name="Kitchen Inspirations", **extra):
        response = self.post("/api/catalog/collections", {"values": {"Name": name, **extra}})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json["collection"]

    def upload(self, collection, raw=None, **fields):
        data = {"file": (io.BytesIO(raw or picture()), "photo.jpg"), "collection": collection, **fields}
        return self.client.post("/api/catalog/items/upload", data=data, headers=self.headers,
                                content_type="multipart/form-data")


class CatalogAppTests(AppTestCase):
    def test_a_workspace_written_before_the_catalog_reads_back_with_empty_sheets(self):
        state = self.state()
        self.assertEqual(state["catalog"]["collections"], [])
        self.assertEqual(state["catalog"]["items"], [])
        self.assertIn("Kitchen", state["catalog"]["categories"])
        self.assertFalse(state["catalog"]["shared_accounts"])

    def test_collection_lifecycle(self):
        cid = self.make_collection("Bathroom Designs", Description="Tile and stone ideas")
        found = next(c for c in self.state()["catalog"]["collections"] if c["Collection ID"] == cid)
        self.assertEqual(found["Description"], "Tile and stone ideas")
        # a duplicate name is refused
        response = self.post("/api/catalog/collections", {"values": {"Name": "bathroom designs"}})
        self.assertEqual(response.status_code, 400)
        # an update keeps the id and bumps Updated
        response = self.post("/api/catalog/collections",
                             {"values": {"Collection ID": cid, "Name": "Bathroom Designs",
                                         "Description": "Now renamed"}})
        self.assertEqual(response.status_code, 200, response.json)
        found = next(c for c in self.state()["catalog"]["collections"] if c["Collection ID"] == cid)
        self.assertEqual(found["Description"], "Now renamed")
        response = self.post("/api/catalog/collections",
                             {"values": {"Collection ID": cid, "Name": "Bathroom Designs"}, "delete": True})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.state()["catalog"]["collections"], [])

    def test_an_upload_becomes_an_inspiration_with_palette_thumbnail_and_category(self):
        cid = self.make_collection()
        response = self.upload(cid, title="Marble kitchen island")
        self.assertEqual(response.status_code, 200, response.json)
        item = next(i for i in self.state()["catalog"]["items"] if i["Item ID"] == response.json["item"])
        self.assertEqual(item["Title"], "Marble kitchen island")
        self.assertEqual(item["Category"], "Kitchen")          # inferred from the title
        self.assertTrue(item["Colors"].startswith("#"))
        self.assertTrue(item["Image File"].startswith("cat-"))
        for name in (item["Image File"], item["Thumb File"]):
            served = self.client.get(f"/api/catalog/images/{name}")
            self.assertEqual(served.status_code, 200)
            self.assertTrue(served.data[:3] in (b"\xff\xd8\xff", b"\x89PN"))
        download = self.client.get(f"/api/catalog/images/{item['Image File']}?download=1")
        self.assertIn("attachment", download.headers.get("Content-Disposition", ""))

    def test_the_same_image_twice_in_one_collection_is_refused_and_reused_across_collections(self):
        cid = self.make_collection()
        other = self.make_collection("Living Room Ideas")
        raw = picture((20, 90, 160))
        first = self.upload(cid, raw=raw, title="Blue wall")
        self.assertEqual(first.status_code, 200, first.json)
        again = self.upload(cid, raw=raw, title="Blue wall again")
        self.assertEqual(again.status_code, 400)
        self.assertIn("already", again.json["error"])
        elsewhere = self.upload(other, raw=raw, title="Blue wall elsewhere")
        self.assertEqual(elsewhere.status_code, 200, elsewhere.json)
        items = self.state()["catalog"]["items"]
        hashes = {i["Image Hash"] for i in items}
        self.assertEqual(len(hashes), 1)       # one stored file serves both items

    def test_clipping_an_image_url_downloads_and_stores_rather_than_hotlinking(self):
        cid = self.make_collection()
        raw = picture((90, 140, 60))
        with patch.object(catalog, "safe_fetch", return_value=(raw, "image/jpeg", "https://example.com/pic.jpg")):
            response = self.post("/api/catalog/clip",
                                 {"collection": cid, "image_url": "https://example.com/pic.jpg",
                                  "source_url": "https://example.com/post", "title": "Garden patio",
                                  "brand": "Belgard", "model": "CAT-450", "finish": "Slate Gray",
                                  "size": "24x24 in", "qty_unit": "120 sq ft"})
        self.assertEqual(response.status_code, 200, response.json)
        item = next(i for i in self.state()["catalog"]["items"] if i["Item ID"] == response.json["item"])
        self.assertEqual(item["Source URL"], "https://example.com/post")
        self.assertEqual(item["Category"], "Landscaping")
        self.assertEqual(item["Brand"], "Belgard")
        self.assertEqual(item["Model #"], "CAT-450")
        self.assertEqual(item["Finish / Color"], "Slate Gray")
        self.assertEqual(item["Size / Configuration"], "24x24 in")
        self.assertEqual(item["Qty / Unit"], "120 sq ft")
        self.assertEqual(self.client.get(f"/api/catalog/images/{item['Image File']}").status_code, 200)

    def test_several_pictures_from_one_page_become_one_inspiration(self):
        """A product page shows the same item from several angles, so every picture picked
        lands on one inspiration — the first its cover — not an inspiration each."""
        cid = self.make_collection()
        shots = [picture((30, 60, 90)), picture((120, 30, 40)), picture((200, 190, 170))]
        fetched = [(raw, "image/jpeg", f"https://example.com/{n}.jpg") for n, raw in enumerate(shots)]
        with patch.object(catalog, "safe_fetch", side_effect=fetched):
            response = self.post("/api/catalog/clip",
                                 {"collection": cid, "title": "Daltile floor",
                                  "image_urls": [f"https://example.com/{n}.jpg" for n in range(3)],
                                  "source_url": "https://example.com/tile"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(len(response.json["images"]), 3)
        self.assertEqual(response.json["refused"], 0)
        items = [i for i in self.state()["catalog"]["items"] if i["Collection ID"] == cid]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["Title"], "Daltile floor")
        images, thumbs = catalog.name_list(item["Image Files"]), catalog.name_list(item["Thumb Files"])
        self.assertEqual(len(set(images)), 3)                                  # three distinct pictures
        self.assertEqual((item["Image File"], item["Thumb File"]), (images[0], thumbs[0]))
        self.assertEqual([entry["image"] for entry in response.json["images"]], images)
        for name in images + thumbs:
            self.assertEqual(self.client.get(f"/api/catalog/images/{name}").status_code, 200)

    def test_the_same_picture_picked_twice_is_saved_once(self):
        """Two tiles can carry the same file. The inspiration keeps one of it rather than
        listing the picture twice under its own cover."""
        cid = self.make_collection()
        raw = picture((70, 110, 95))
        with patch.object(catalog, "safe_fetch", side_effect=lambda url, *a, **k: (raw, "image/jpeg", url)):
            response = self.post("/api/catalog/clip",
                                 {"collection": cid, "title": "Same shot twice",
                                  "image_urls": ["https://example.com/a.jpg", "https://example.com/b.jpg"]})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(len(response.json["images"]), 1)
        item = next(i for i in self.state()["catalog"]["items"] if i["Collection ID"] == cid)
        self.assertEqual(len(catalog.name_list(item["Image Files"])), 1)

    def test_a_picture_a_site_withholds_does_not_lose_the_others(self):
        """Sites hand over some images and refuse others. The ones that came through are
        saved and the count says what was missed, rather than the whole clip failing."""
        cid = self.make_collection()
        def answer(url, *args, **kwargs):
            if url.endswith("2.jpg"):
                raise ValidationError("That site would not hand this over to the app.")
            return (picture((10, 20, 30) if url.endswith("1.jpg") else (90, 90, 90)), "image/jpeg", url)
        with patch.object(catalog, "safe_fetch", side_effect=answer):
            response = self.post("/api/catalog/clip",
                                 {"collection": cid, "title": "Mixed page",
                                  "image_urls": ["https://example.com/1.jpg", "https://example.com/2.jpg",
                                                 "https://example.com/3.jpg"]})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual((len(response.json["images"]), response.json["refused"]), (2, 1))
        items = [i for i in self.state()["catalog"]["items"] if i["Collection ID"] == cid]
        self.assertEqual(len(items), 1)
        self.assertEqual(len(catalog.name_list(items[0]["Image Files"])), 2)

    def test_several_pictures_of_one_product_write_one_line_item(self):
        """Clipping three pictures of one product onto a project's spec sheet writes that
        project a single referencing row — the pictures are angles, not three products."""
        cid = self.make_collection()
        shots = [(picture((n * 40, 20, 60)), "image/jpeg", f"https://example.com/{n}.jpg") for n in range(3)]
        with patch.object(catalog, "safe_fetch", side_effect=shots):
            response = self.post("/api/catalog/clip",
                                 {"collection": cid, "title": "Green tile",
                                  "image_urls": [f"https://example.com/{n}.jpg" for n in range(3)],
                                  "projects": ["UH-101"], "room": "Powder Room"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(list(response.json["rows"]), ["UH-101"])
        rows = [r for r in self.state()["selections"] if r.get("Catalog Item ID") == response.json["item"]]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Room / Area"], "Powder Room")
        self.assertEqual(rows[0]["Item"], "Green tile")

    def test_more_pictures_than_one_clip_allows_are_refused(self):
        """A clip is a page's worth of pictures, not a crawl: the list is capped and the
        refusal names the limit instead of quietly saving the first few."""
        cid = self.make_collection()
        response = self.post("/api/catalog/clip",
                             {"collection": cid, "title": "Too many",
                              "image_urls": [f"https://example.com/{n}.jpg" for n in range(40)]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("24", response.json["error"])
        self.assertEqual(self.state()["catalog"]["items"], [])

    def test_clipping_can_create_the_collection_in_the_same_save(self):
        with patch.object(catalog, "safe_fetch", return_value=(picture(), "image/jpeg", "https://example.com/p.jpg")):
            response = self.post("/api/catalog/clip",
                                 {"new_collection": "Exterior Designs", "image_url": "https://example.com/p.jpg",
                                  "title": "Stone facade"})
        self.assertEqual(response.status_code, 200, response.json)
        state = self.state()["catalog"]
        self.assertEqual(state["collections"][0]["Name"], "Exterior Designs")
        self.assertEqual(state["items"][0]["Collection ID"], response.json["collection"])

    def test_a_clip_can_land_straight_on_a_projects_spec_sheet(self):
        """A clip's 'Also add to project': one clip saves the library item and writes
        that project its own referencing line item in the same save."""
        cid = self.make_collection()
        with patch.object(catalog, "safe_fetch", return_value=(picture((40, 70, 110)), "image/jpeg", "https://example.com/p.jpg")):
            response = self.post("/api/catalog/clip",
                                 {"collection": cid, "image_url": "https://example.com/p.jpg",
                                  "source_url": "https://example.com/pendant", "title": "Brass pendant",
                                  "brand": "Visual Comfort", "model": "TOB5003", "price": "$429.00",
                                  "projects": ["UH-101"], "room": "Kitchen"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(list(response.json["rows"]), ["UH-101"])
        row = next(r for r in self.state()["selections"] if r.get("Catalog Item ID") == response.json["item"])
        self.assertEqual(row["Project ID"], "UH-101")
        self.assertEqual(row["Room / Area"], "Kitchen")
        self.assertEqual(row["Item"], "Brass pendant")
        self.assertEqual(row["Manufacturer"], "Visual Comfort")
        self.assertEqual(row["Model #"], "TOB5003")
        self.assertEqual(float(row["Unit Price"]), 429.0)       # parsed from the clipped price
        self.assertEqual(row["Product URL"], "https://example.com/pendant")
        # the library kept the master record too
        self.assertTrue(any(i["Item ID"] == response.json["item"] for i in self.state()["catalog"]["items"]))

    def test_a_clip_into_an_unknown_project_is_refused_whole(self):
        """A bad project refuses the entire clip — nothing half saved in the library."""
        cid = self.make_collection()
        with patch.object(catalog, "safe_fetch", return_value=(picture((5, 5, 5)), "image/jpeg", "https://example.com/q.jpg")):
            response = self.post("/api/catalog/clip",
                                 {"collection": cid, "image_url": "https://example.com/q.jpg",
                                  "title": "Ghost sconce", "projects": ["UH-999"]})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Ghost sconce", [i["Title"] for i in self.state()["catalog"]["items"]])
        self.assertFalse(any(r.get("Item") == "Ghost sconce" for r in self.state()["selections"]))

    def test_inspect_reads_metadata_and_candidate_images(self):
        with patch.object(catalog, "safe_fetch", return_value=(PAGE_HTML.encode(), "text/html", "https://example.com/post")):
            response = self.post("/api/catalog/inspect", {"url": "https://example.com/post"})
        self.assertEqual(response.status_code, 200, response.json)
        data = response.json
        self.assertEqual(data["title"], "Dream Kitchen")
        self.assertEqual(data["site"], "Design Site")
        urls = [img["url"] for img in data["images"]]
        self.assertEqual(urls[0], "https://example.com/images/hero.jpg")     # og:image first
        self.assertIn("https://example.com/images/photo1.jpg", urls)
        self.assertIn("https://cdn.example.com/lazy.jpg", urls)              # lazy-loaded images count
        self.assertFalse(any("tiny.gif" in u for u in urls))                 # 1x1 beacons do not
        self.assertFalse(any(u.endswith(".svg") for u in urls))              # icons do not

    def test_private_addresses_and_odd_schemes_are_refused(self):
        for url in ("http://127.0.0.1/admin", "http://localhost:8080/x", "http://169.254.169.254/meta",
                    "http://10.0.0.5/internal", "ftp://example.com/file", "file:///etc/passwd",
                    "https://user:pass@example.com/"):
            with self.assertRaises(ValidationError, msg=url):
                catalog.safe_fetch(url, 1000)

    def test_a_redirect_into_a_private_network_is_refused(self):
        class Hop:
            status_code = 302
            headers = {"Location": "http://127.0.0.1/steal"}
            def __enter__(self): return self
            def __exit__(self, *a): return False
        with patch.object(catalog.requests, "get", return_value=Hop()):
            with self.assertRaises(ValidationError):
                catalog.safe_fetch("https://example.com/start", 1000)

    def test_editing_moving_favoriting_and_deleting_an_inspiration(self):
        cid = self.make_collection()
        other = self.make_collection("Flooring & Tiles")
        item_id = self.upload(cid, title="Herringbone floor").json["item"]
        response = self.post("/api/catalog/items", {"item": item_id, "values": {
            "Title": "Herringbone oak floor", "Category": "Flooring & Tiles",
            "Collection ID": other, "Favorite": "Yes"}})
        self.assertEqual(response.status_code, 200, response.json)
        item = next(i for i in self.state()["catalog"]["items"] if i["Item ID"] == item_id)
        self.assertEqual(item["Collection ID"], other)
        self.assertEqual(item["Favorite"], "Yes")
        files = [item["Image File"], item["Thumb File"]]
        response = self.post("/api/catalog/items", {"item": item_id, "delete": True})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.state()["catalog"]["items"], [])
        for name in files:                                     # orphaned files are removed with the row
            self.assertEqual(self.client.get(f"/api/catalog/images/{name}").status_code, 400)

    def test_deleting_an_inspiration_removes_every_picture_it_held(self):
        """An inspiration owns all of its pictures, so deleting it takes each of them —
        not only the cover — with it."""
        cid = self.make_collection()
        shots = [(picture((n * 60 + 10, 40, 80)), "image/jpeg", f"https://example.com/{n}.jpg") for n in range(3)]
        with patch.object(catalog, "safe_fetch", side_effect=shots):
            created = self.post("/api/catalog/clip",
                                {"collection": cid, "title": "Three angles",
                                 "image_urls": [f"https://example.com/{n}.jpg" for n in range(3)]}).json
        files = [name for entry in created["images"] for name in (entry["image"], entry["thumb"])]
        response = self.post("/api/catalog/items", {"item": created["item"], "delete": True})
        self.assertEqual(response.status_code, 200, response.json)
        for name in files:
            self.assertEqual(self.client.get(f"/api/catalog/images/{name}").status_code, 400)

    def test_clipping_the_same_product_again_is_refused_but_a_new_angle_is_not(self):
        """Every picture already filed here means the clip happened twice; one the
        collection has not seen means this clip shows the product differently."""
        cid = self.make_collection()
        shots = {f"https://example.com/{n}.jpg": picture((n * 50 + 20, 90, 120)) for n in range(3)}
        def answer(url, *args, **kwargs):
            return (shots[url], "image/jpeg", url)
        with patch.object(catalog, "safe_fetch", side_effect=answer):
            first = self.post("/api/catalog/clip", {"collection": cid, "title": "Bridge faucet",
                                                    "image_urls": list(shots)[:2]})
            self.assertEqual(first.status_code, 200, first.json)
            again = self.post("/api/catalog/clip", {"collection": cid, "title": "Bridge faucet again",
                                                    "image_urls": list(shots)[:2]})
            self.assertEqual(again.status_code, 400)
            self.assertIn("already", again.json["error"])
            wider = self.post("/api/catalog/clip", {"collection": cid, "title": "Bridge faucet, wider",
                                                    "image_urls": list(shots)})
            self.assertEqual(wider.status_code, 200, wider.json)
        self.assertEqual(len([i for i in self.state()["catalog"]["items"] if i["Collection ID"] == cid]), 2)

    def test_an_unknown_category_is_refused(self):
        cid = self.make_collection()
        item_id = self.upload(cid, title="Something").json["item"]
        response = self.post("/api/catalog/items", {"item": item_id, "values": {"Category": "Bananas"}})
        self.assertEqual(response.status_code, 400)

    def test_comments_are_added_and_removed(self):
        cid = self.make_collection()
        item_id = self.upload(cid, title="Pendant lights").json["item"]
        response = self.post("/api/catalog/comments", {"item": item_id, "text": "Love the brass finish."})
        self.assertEqual(response.status_code, 200, response.json)
        comment = self.state()["catalog"]["comments"][0]
        self.assertEqual(comment["Comment"], "Love the brass finish.")
        response = self.post("/api/catalog/comments", {"comment": comment["Comment ID"], "delete": True})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.state()["catalog"]["comments"], [])

    def test_deleting_a_collection_removes_its_items_and_their_files(self):
        cid = self.make_collection()
        kept = self.make_collection("Keep Me")
        names = []
        for color in ((200, 10, 10), (10, 200, 10), (10, 10, 200)):
            created = self.upload(cid, raw=picture(color)).json
            names += [created["image"], created["thumb"]]
        kept_item = self.upload(kept, raw=picture((123, 231, 132))).json
        refused = self.post("/api/catalog/collections", {"values": {"Collection ID": cid, "Name": "Kitchen Inspirations"},
                                                         "delete": True})
        self.assertEqual(refused.status_code, 400)             # cascade must be confirmed
        response = self.post("/api/catalog/collections", {"values": {"Collection ID": cid, "Name": "Kitchen Inspirations"},
                                                          "delete": True, "cascade": True})
        self.assertEqual(response.status_code, 200, response.json)
        state = self.state()["catalog"]
        self.assertEqual([c["Collection ID"] for c in state["collections"]], [kept])
        self.assertEqual(len(state["items"]), 1)
        for name in names:
            self.assertEqual(self.client.get(f"/api/catalog/images/{name}").status_code, 400)
        self.assertEqual(self.client.get(f"/api/catalog/images/{kept_item['image']}").status_code, 200)

    def test_the_bookmarklet_serves_the_shared_scanner_with_the_handoff(self):
        """A bot-walled page (403 to the server) is read in the browser instead: the
        bookmarklet carries clip_scan.js itself — the one deep reader — plus the postMessage
        hand-off that delivers what it found to the app's /?clipwait=1 window."""
        response = self.client.get("/api/catalog/bookmarklet")
        self.assertEqual(response.status_code, 200)
        code = response.get_data(as_text=True)
        scanner = (Path(__file__).parent / "clip_scan.js").read_text(encoding="utf-8")
        self.assertIn(scanner.strip(), code)         # the scanner itself, never a drifting copy
        self.assertIn("uh-clip-ready", code)         # the app window says it is listening
        self.assertIn("uh-clip-page", code)          # the page rides over in the answer
        self.assertIn("__UH_APP__", code)            # ui.js fills in its own origin
        self.assertIn("clipwait=1", code)

    def test_the_importable_bookmark_file_stays_removed(self):
        """The clipper is the bookmarklet dragged out of the dialog, by request. The
        downloadable bookmarks file that carried the UH icon through Chrome's importer is
        gone: an import that lands in a folder somewhere reads as a clipper that did not
        install, and the dragged ticket is one step that always works."""
        self.assertEqual(self.client.get("/clip-bookmark.html").status_code, 404)

    def test_the_retired_browser_extension_stays_removed(self):
        """The extension was retired for the bookmarklet: its download, its picker routes
        and its token are gone, and the old token header no longer stands in for CSRF."""
        self.assertEqual(self.client.get("/clipper-extension.zip").status_code, 404)
        self.assertEqual(self.client.get("/api/catalog/clipper/collections").status_code, 404)
        self.assertEqual(self.client.get("/api/catalog/clipper/projects").status_code, 404)
        self.assertEqual(self.client.post("/api/catalog/clipper-token", json={}).status_code, 404)
        response = self.client.post("/api/catalog/clip", json={"image_url": "https://example.com/p.jpg"},
                                    headers={"X-UH-Clipper": "anything"})   # no X-UH-Token at all
        self.assertEqual(response.status_code, 403)            # the CSRF rule decides alone

    def test_the_assistant_answers_locally_when_no_ai_key_is_configured(self):
        cid = self.make_collection()
        self.upload(cid, title="Marble kitchen island")
        self.upload(cid, raw=picture((99, 99, 99)), title="Cedar pergola")
        response = self.post("/api/catalog/assistant", {"question": "find my marble kitchen ideas"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["source"], "local")
        items = self.state()["catalog"]["items"]
        marble = next(i for i in items if "Marble" in i["Title"])
        self.assertIn(marble["Item ID"], response.json["item_ids"])

    def test_the_retired_image_analysis_stays_removed(self):
        """Nothing analyses a picture any more: the suggestion button, its route and the
        vision call are gone, so an image is never sent anywhere to be described."""
        cid = self.make_collection()
        item_id = self.upload(cid, title="Quartz bathroom vanity").json["item"]
        response = self.client.post(f"/api/catalog/items/{item_id}/analyze", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(hasattr(catalog, "analyze_image"))
        self.assertFalse(hasattr(catalog, "local_analysis"))
        self.assertNotIn("ai", self.state()["catalog"])

    def test_the_retired_tag_and_material_fields_are_refused_and_dropped(self):
        """Tags and Materials were removed from the inspiration: a client cannot write
        them back, and a workspace that still carries the columns loses them on load."""
        cid = self.make_collection()
        item_id = self.upload(cid, title="Herringbone floor").json["item"]
        for field in ("Tags", "Materials"):
            response = self.post("/api/catalog/items", {"item": item_id, "values": {field: "oak"}})
            self.assertEqual(response.status_code, 400, field)
        wb, revision = self.store.snapshot()
        sheet = wb["Catalog Items"]
        column = sheet.max_column + 1
        sheet.cell(1, column, "Tags")
        sheet.cell(2, column, "left over from an older save")
        self.store.save(wb, revision)
        after, _ = self.store.snapshot()
        self.assertNotIn("Tags", {c.value for c in after["Catalog Items"][1]})
        item = next(i for i in self.state()["catalog"]["items"] if i["Item ID"] == item_id)
        self.assertNotIn("Tags", item)
        self.assertNotIn("Materials", item)

    def test_similar_designs_rank_shared_category_and_palette_first(self):
        cid = self.make_collection()
        a = self.upload(cid, raw=picture((200, 50, 50)), title="Kitchen one").json["item"]
        b = self.upload(cid, raw=picture((210, 60, 60)), title="Kitchen two").json["item"]
        c = self.upload(cid, raw=picture((5, 5, 5)), title="Dark cedar pergola garden").json["item"]
        response = self.client.get(f"/api/catalog/similar?item={a}")
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["item_ids"][0], b)
        self.assertNotIn(c, response.json["item_ids"][:1])

    def test_existing_selection_workflow_is_untouched_by_the_catalog_sheets(self):
        state = self.state()
        self.assertTrue(state["projects"])
        project = state["projects"][0]["Project ID"]
        response = self.post("/api/selections", {"values": {"Project ID": project, "Item": "Catalog Coexistence Check"}})
        self.assertEqual(response.status_code, 200, response.json)
        wb, _ = self.store.snapshot()
        self.assertIn("Catalog Collections", wb.sheetnames)
        self.assertTrue(any(r["Item"] == "Catalog Coexistence Check"
                            for r in self.state()["selections"]))


class HostedCatalogTests(unittest.TestCase):
    """Hosted mode: one shared team catalog behind sign-in."""

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
        self.login_patcher = patch.object(app, "REQUIRE_LOGIN", True)
        self.login_patcher.start()
        app.CLIP_THROTTLE.attempts.clear()
        self.client = app.app.test_client()

    def tearDown(self):
        self.patcher.stop()
        self.login_patcher.stop()
        self.folder.cleanup()

    def sign_in(self, email):
        with self.client.session_transaction() as session:
            session["user"] = {"id": email, "email": email}
            session["csrf"] = f"csrf-{email}"
        return {"X-UH-Token": f"csrf-{email}"}

    def state(self):
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_every_signed_in_member_sees_and_edits_every_collection(self):
        """Privacy and sharing were removed: the catalog is one shared team workspace."""
        owner = self.sign_in("owner@uh.test")
        response = self.client.post("/api/catalog/collections", headers=owner,
                                    json={"revision": self.state()["revision"],
                                          "values": {"Name": "Studio Board"}})
        self.assertEqual(response.status_code, 200, response.json)
        cid = response.json["collection"]

        other = self.sign_in("other@uh.test")
        collections = self.state()["catalog"]["collections"]
        self.assertEqual(len(collections), 1)                  # visible to everyone signed in
        self.assertNotIn("Privacy", collections[0])            # the retired columns are gone
        self.assertNotIn("Shared With", collections[0])
        response = self.client.post("/api/catalog/collections", headers=other,
                                    json={"revision": self.state()["revision"],
                                          "values": {"Collection ID": cid, "Name": "Studio Board",
                                                     "Description": "Edited by a teammate"}})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.state()["catalog"]["collections"][0]["Description"], "Edited by a teammate")
        # the removed fields are refused rather than silently stored
        response = self.client.post("/api/catalog/collections", headers=other,
                                    json={"revision": self.state()["revision"],
                                          "values": {"Collection ID": cid, "Name": "Studio Board",
                                                     "Privacy": "Private"}})
        self.assertEqual(response.status_code, 400)

    def test_a_clip_needs_a_signed_in_session_and_the_old_token_header_is_ignored(self):
        """The browser extension was retired: hosted, a clip reaches the workspace only
        through a signed-in session, never through the old X-UH-Clipper header."""
        fresh = self.module.app.test_client()                  # no session at all
        response = fresh.post("/api/catalog/clip", json={"image_url": "https://example.com/p.jpg"},
                              headers={"X-UH-Clipper": "anything"})
        self.assertEqual(response.status_code, 401)            # no session, no way in
        self.assertEqual(fresh.get("/clipper-extension.zip").status_code, 404)


class AssignmentTests(AppTestCase):
    """One catalog item -> many projects: the catalog stays the single master record and each
    project gets its own referencing line item."""

    def saved_item(self, title="Brass kitchen faucet", **fields):
        cid = self.make_collection()
        response = self.upload(cid, title=title, **fields)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json["item"]

    def assigned_rows(self, item_id):
        return [r for r in self.state()["selections"] if r.get("Catalog Item ID") == item_id]

    def test_one_catalog_item_becomes_line_items_in_many_projects_without_duplication(self):
        item_id = self.saved_item(price="$1,299.00 USD", brand="Brizo", category="Kitchen",
                                  model="63075-BL", finish="Matte Black", size="Single handle",
                                  qty_unit="3 EA")
        before = len(self.state()["catalog"]["items"])
        response = self.post(f"/api/catalog/items/{item_id}/assign",
                             {"projects": ["UH-101", "UH-103"], "values": {"Room / Area": "Kitchen", "Qty": "2"}})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(set(response.json["rows"]), {"UH-101", "UH-103"})
        rows = self.assigned_rows(item_id)
        self.assertEqual({r["Project ID"] for r in rows}, {"UH-101", "UH-103"})
        for row in rows:
            self.assertEqual(row["Item"], "Brass kitchen faucet")
            self.assertEqual(row["Section"], "Kitchen")
            self.assertEqual(row["Room / Area"], "Kitchen")
            self.assertEqual(float(row["Qty"]), 2.0)
            self.assertEqual(float(row["Unit Price"]), 1299.0)   # parsed from the clipped price
            self.assertEqual(row["Manufacturer"], "Brizo")
            self.assertEqual(row["Model #"], "63075-BL")         # the spec fields ride along
            self.assertEqual(row["Finish / Color"], "Matte Black")
            self.assertEqual(row["Size / Configuration"], "Single handle")
            self.assertEqual(row["Unit"], "EA")                  # the clipped '3 EA', split
            self.assertEqual(row["Client Status"], "Proposed")
        # the master record was referenced, never copied
        self.assertEqual(len(self.state()["catalog"]["items"]), before)
        # the same item goes into a third project later, still without a new catalog entry
        again = self.post(f"/api/catalog/items/{item_id}/assign", {"projects": ["UH-102"]})
        self.assertEqual(again.status_code, 200, again.json)
        self.assertEqual({r["Project ID"] for r in self.assigned_rows(item_id)}, {"UH-101", "UH-102", "UH-103"})
        # without an override, the quantity comes out of the clipped 'Qty / Unit'
        third = next(r for r in self.assigned_rows(item_id) if r["Project ID"] == "UH-102")
        self.assertEqual(float(third["Qty"]), 3.0)
        self.assertEqual(third["Unit"], "EA")
        self.assertEqual(len(self.state()["catalog"]["items"]), before)

    def test_an_unknown_project_and_an_empty_choice_are_refused(self):
        item_id = self.saved_item()
        response = self.post(f"/api/catalog/items/{item_id}/assign", {"projects": ["UH-999"]})
        self.assertEqual(response.status_code, 400)
        response = self.post(f"/api/catalog/items/{item_id}/assign", {"projects": []})
        self.assertEqual(response.status_code, 400)
        response = self.post("/api/catalog/items/ins-nope/assign", {"projects": ["UH-101"]})
        self.assertEqual(response.status_code, 400)

    def test_removing_a_line_item_never_deletes_the_catalog_record_and_vice_versa(self):
        item_id = self.saved_item(title="Travertine floor tile")
        self.assertEqual(self.post(f"/api/catalog/items/{item_id}/assign",
                                   {"projects": ["UH-101", "UH-102"]}).status_code, 200)
        row = self.assigned_rows(item_id)[0]
        response = self.post("/api/selections", {"row": row["_row"], "values": {}, "delete": True})
        self.assertEqual(response.status_code, 200, response.json)
        # the catalog still holds the master record, and the other project keeps its line item
        self.assertTrue(any(i["Item ID"] == item_id for i in self.state()["catalog"]["items"]))
        self.assertEqual(len(self.assigned_rows(item_id)), 1)
        # deleting the catalog item leaves the remaining project row untouched
        survivor = self.assigned_rows(item_id)[0]
        self.assertEqual(self.post("/api/catalog/items", {"item": item_id, "delete": True}).status_code, 200)
        kept = next(r for r in self.state()["selections"] if r["_row"] == survivor["_row"])
        self.assertEqual(kept["Item"], "Travertine floor tile")

    def test_editing_the_master_record_never_rewrites_a_project_row(self):
        item_id = self.saved_item(title="Walnut vanity", price="$900")
        self.assertEqual(self.post(f"/api/catalog/items/{item_id}/assign", {"projects": ["UH-103"]}).status_code, 200)
        response = self.post("/api/catalog/items", {"item": item_id, "values": {
            "Title": "Walnut double vanity", "Price": "$1,500"}})
        self.assertEqual(response.status_code, 200, response.json)
        row = self.assigned_rows(item_id)[0]
        self.assertEqual(row["Item"], "Walnut vanity")             # the project's copy stands
        self.assertEqual(float(row["Unit Price"]), 900.0)
        item = next(i for i in self.state()["catalog"]["items"] if i["Item ID"] == item_id)
        self.assertEqual(item["Title"], "Walnut double vanity")    # the master took the edit

    def test_a_selection_edit_keeps_its_catalog_reference(self):
        item_id = self.saved_item(title="Pendant light")
        self.assertEqual(self.post(f"/api/catalog/items/{item_id}/assign", {"projects": ["UH-101"]}).status_code, 200)
        row = self.assigned_rows(item_id)[0]
        response = self.post("/api/selections", {"row": row["_row"], "values": {
            "Project ID": "UH-101", "Item": "Pendant light", "Qty": 3, "Client Notes": "Over the island"}})
        self.assertEqual(response.status_code, 200, response.json)
        kept = next(r for r in self.state()["selections"] if r["_row"] == row["_row"])
        self.assertEqual(kept["Catalog Item ID"], item_id)
        self.assertEqual(float(kept["Qty"]), 3.0)


PRODUCT_HTML = """<html><head><title>Faucet</title>
<meta property="og:title" content="Odin Pull-Down Faucet">
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[{"@type":"Product","name":"Odin Pull-Down Faucet",
"brand":{"@type":"Brand","name":"Brizo"},"sku":"63075-BL","color":"Matte Black",
"size":"Single handle, 16 in","material":["brass","stainless steel"],
"image":["https://cdn.example.com/odin-main.jpg",{"@type":"ImageObject","url":"https://cdn.example.com/odin-side.jpg"}],
"offers":[{"@type":"Offer","price":"1299.00","priceCurrency":"USD"}]}]}
</script></head><body><img src="/f.jpg" width="900" height="900"></body></html>"""

MICRO_HTML = """<html><head><title>Shaker Wall Cabinet | Example Store</title></head>
<body itemscope itemtype="https://schema.org/Product">
<meta itemprop="brand" content="Diamond NOW">
<meta itemprop="sku" content="G10-W3030">
<meta itemprop="color" content="Painted White">
<meta itemprop="size" content="30 x 30 in">
<meta itemprop="material" content="plywood">
<meta itemprop="price" content="149.00">
<meta itemprop="priceCurrency" content="USD">
<img src="/cabinet.jpg" width="700" height="700">
</body></html>"""

META_PRICE_HTML = """<html><head>
<meta property="og:title" content="Cement Tile">
<meta property="product:price:amount" content="12.50">
<meta property="product:price:currency" content="USD">
<meta property="og:brand" content="Clay Imports">
<meta property="product:retailer_part_no" content="CT-8x8-OLE">
<meta property="product:color" content="Terracotta">
<meta property="product:size" content="8x8 in">
<meta property="product:material" content="cement">
</head><body></body></html>"""


class ParserTests(unittest.TestCase):
    def test_the_spec_fields_come_from_json_ld_product_data(self):
        page = catalog.parse_page(PRODUCT_HTML, "https://example.com/faucet")
        self.assertEqual(page["title"], "Odin Pull-Down Faucet")   # the product's own name
        self.assertEqual(page["price"], "1299.00 USD")
        self.assertEqual(page["brand"], "Brizo")
        self.assertEqual(page["model"], "63075-BL")
        self.assertEqual(page["finish"], "Matte Black")
        self.assertEqual(page["size"], "Single handle, 16 in")
        self.assertNotIn("materials", page)        # the page's material is read by nothing now
        urls = [img["url"] for img in page["images"]]
        # the product's own photos are offered as candidates, however the page nested them
        self.assertIn("https://cdn.example.com/odin-main.jpg", urls)
        self.assertIn("https://cdn.example.com/odin-side.jpg", urls)

    def test_the_spec_fields_fall_back_to_microdata(self):
        page = catalog.parse_page(MICRO_HTML, "https://example.com/cabinet")
        self.assertEqual(page["price"], "149.00 USD")
        self.assertEqual(page["brand"], "Diamond NOW")
        self.assertEqual(page["model"], "G10-W3030")
        self.assertEqual(page["finish"], "Painted White")
        self.assertEqual(page["size"], "30 x 30 in")

    def test_the_spec_fields_fall_back_to_meta_tags(self):
        page = catalog.parse_page(META_PRICE_HTML, "https://example.com/tile")
        self.assertEqual(page["price"], "12.50 USD")
        self.assertEqual(page["brand"], "Clay Imports")
        self.assertEqual(page["model"], "CT-8x8-OLE")
        self.assertEqual(page["finish"], "Terracotta")
        self.assertEqual(page["size"], "8x8 in")

    def test_price_number_reads_the_amount_out_of_clipped_text(self):
        self.assertEqual(catalog.price_number("$1,299.00 USD"), 1299.0)
        self.assertEqual(catalog.price_number("12.50"), 12.5)
        self.assertIsNone(catalog.price_number("Call for pricing"))
        self.assertIsNone(catalog.price_number(""))

    def test_keyword_category(self):
        self.assertEqual(catalog.keyword_category("A waterfall kitchen island with brass pulls"), "Kitchen")
        self.assertEqual(catalog.keyword_category("stucco facade with a standing-seam roof"), "Exterior")
        self.assertEqual(catalog.keyword_category("nothing relevant"), "")



    def test_palette_reads_dominant_colors(self):
        image = Image.new("RGB", (120, 120), (250, 10, 10))
        colors = catalog.palette(image)
        self.assertTrue(colors)
        red = tuple(int(colors[0][i:i + 2], 16) for i in (1, 3, 5))
        self.assertGreater(red[0], 200)
        self.assertLess(red[1], 60)


if __name__ == "__main__":
    unittest.main()
