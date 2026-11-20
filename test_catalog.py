"""The design catalog (the clipper): collections, clipping, permissions, AI fallbacks."""
import base64
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
        response = self.upload(cid, title="Marble kitchen island", tags="modern, brass")
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

    def test_clipping_can_create_the_collection_in_the_same_save(self):
        with patch.object(catalog, "safe_fetch", return_value=(picture(), "image/jpeg", "https://example.com/p.jpg")):
            response = self.post("/api/catalog/clip",
                                 {"new_collection": "Exterior Designs", "image_url": "https://example.com/p.jpg",
                                  "title": "Stone facade"})
        self.assertEqual(response.status_code, 200, response.json)
        state = self.state()["catalog"]
        self.assertEqual(state["collections"][0]["Name"], "Exterior Designs")
        self.assertEqual(state["items"][0]["Collection ID"], response.json["collection"])

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
            "Collection ID": other, "Favorite": "Yes", "Tags": "herringbone, oak"}})
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

    def test_the_clipper_extension_downloads_as_a_zip_for_hosted_users(self):
        import zipfile
        response = self.client.get("/clipper-extension.zip")
        self.assertEqual(response.status_code, 200)
        names = zipfile.ZipFile(io.BytesIO(response.data)).namelist()
        self.assertIn("clipper-extension/manifest.json", names)
        self.assertIn("clipper-extension/popup.js", names)
        self.assertTrue(all(name.startswith("clipper-extension/") for name in names))

    def test_the_clipper_token_authenticates_without_a_session_and_a_forged_one_does_not(self):
        cid = self.make_collection()
        token = self.post("/api/catalog/clipper-token", {}).json["token"]
        raw = base64.b64encode(picture((60, 60, 200))).decode()
        response = self.client.post("/api/catalog/clip",                      # no X-UH-Token at all
                                    json={"collection": cid, "image_data": raw, "title": "Clipped remotely"},
                                    headers={"X-UH-Clipper": token})
        self.assertEqual(response.status_code, 200, response.json)
        listing = self.client.get("/api/catalog/clipper/collections", headers={"X-UH-Clipper": token})
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json["collections"][0]["count"], 1)
        forged = self.client.post("/api/catalog/clip", json={"collection": cid, "image_data": raw},
                                  headers={"X-UH-Clipper": token[:-4] + "zzzz"})
        self.assertEqual(forged.status_code, 403)              # falls through to the CSRF rule

    def test_the_assistant_answers_locally_when_no_ai_key_is_configured(self):
        cid = self.make_collection()
        self.upload(cid, title="Marble kitchen island", tags="marble, modern")
        self.upload(cid, raw=picture((99, 99, 99)), title="Cedar pergola", tags="outdoor")
        response = self.post("/api/catalog/assistant", {"question": "find my marble kitchen ideas"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["source"], "local")
        items = self.state()["catalog"]["items"]
        marble = next(i for i in items if "Marble" in i["Title"])
        self.assertIn(marble["Item ID"], response.json["item_ids"])

    def test_analyze_without_a_key_suggests_from_keywords_and_is_labelled_local(self):
        cid = self.make_collection()
        item_id = self.upload(cid, title="Quartz bathroom vanity").json["item"]
        response = self.client.post(f"/api/catalog/items/{item_id}/analyze", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["source"], "local")
        self.assertEqual(response.json["category"], "Bathroom")
        self.assertIn("quartz", response.json["materials"])

    def test_similar_designs_rank_shared_category_and_tags_first(self):
        cid = self.make_collection()
        a = self.upload(cid, raw=picture((200, 50, 50)), title="Kitchen one", tags="brass, modern").json["item"]
        b = self.upload(cid, raw=picture((210, 60, 60)), title="Kitchen two", tags="brass").json["item"]
        c = self.upload(cid, raw=picture((5, 5, 5)), title="Dark cedar pergola garden", tags="outdoor").json["item"]
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
    """Hosted mode: one shared team catalog, and the clipper token's identity."""

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

    def test_the_clipper_extension_downloads_without_signing_in(self):
        """The zip is static public code; a teammate setting up should not need a session first."""
        import zipfile
        response = self.client.get("/clipper-extension.zip")        # no sign_in()
        self.assertEqual(response.status_code, 200)
        self.assertIn("clipper-extension/manifest.json", zipfile.ZipFile(io.BytesIO(response.data)).namelist())

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

    def test_the_clipper_token_carries_its_owner_into_hosted_saves(self):
        owner = self.sign_in("owner@uh.test")
        response = self.client.post("/api/catalog/collections", headers=owner,
                                    json={"revision": self.state()["revision"], "values": {"Name": "Clips"}})
        cid = response.json["collection"]
        token = self.client.post("/api/catalog/clipper-token", headers=owner, json={}).json["token"]

        fresh = self.module.app.test_client()                  # no session at all
        raw = base64.b64encode(picture((10, 120, 220))).decode()
        response = fresh.post("/api/catalog/clip", json={"collection": cid, "image_data": raw, "title": "From the road"},
                              headers={"X-UH-Clipper": token})
        self.assertEqual(response.status_code, 200, response.json)
        self.sign_in("owner@uh.test")
        item = self.state()["catalog"]["items"][0]
        self.assertEqual(item["Saved By"], "owner@uh.test")
        no_token = fresh.post("/api/catalog/clip", json={"collection": cid, "image_data": raw})
        self.assertEqual(no_token.status_code, 401)            # no session and no token


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
        self.assertEqual(page["materials"], "brass, stainless steel")
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
        self.assertEqual(page["materials"], "plywood")

    def test_the_spec_fields_fall_back_to_meta_tags(self):
        page = catalog.parse_page(META_PRICE_HTML, "https://example.com/tile")
        self.assertEqual(page["price"], "12.50 USD")
        self.assertEqual(page["brand"], "Clay Imports")
        self.assertEqual(page["model"], "CT-8x8-OLE")
        self.assertEqual(page["finish"], "Terracotta")
        self.assertEqual(page["size"], "8x8 in")
        self.assertEqual(page["materials"], "cement")

    def test_price_number_reads_the_amount_out_of_clipped_text(self):
        self.assertEqual(catalog.price_number("$1,299.00 USD"), 1299.0)
        self.assertEqual(catalog.price_number("12.50"), 12.5)
        self.assertIsNone(catalog.price_number("Call for pricing"))
        self.assertIsNone(catalog.price_number(""))

    def test_keyword_category_and_materials(self):
        self.assertEqual(catalog.keyword_category("A waterfall kitchen island with brass pulls"), "Kitchen")
        self.assertEqual(catalog.keyword_category("stucco facade with a standing-seam roof"), "Exterior")
        self.assertEqual(catalog.keyword_category("nothing relevant"), "")
        self.assertEqual(catalog.keyword_materials("Honed marble over white oak"), ["marble", "oak"])



    def test_palette_reads_dominant_colors(self):
        image = Image.new("RGB", (120, 120), (250, 10, 10))
        colors = catalog.palette(image)
        self.assertTrue(colors)
        red = tuple(int(colors[0][i:i + 2], 16) for i in (1, 3, 5))
        self.assertGreater(red[0], 200)
        self.assertLess(red[1], 60)


if __name__ == "__main__":
    unittest.main()
