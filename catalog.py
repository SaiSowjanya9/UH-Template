"""The design catalog: UH Homes' clipper, server side.

Collections and their saved inspirations are workbook sheets (see CATALOG_SHEETS in
workspace_store.py), so they inherit validation, revision conflicts and backups like every
other record. Image bytes never enter the workbook: a clipped image is downloaded here —
never hotlinked — optimised with Pillow, stored beside the workspace under a hash-derived
name, and deduplicated by that hash.

Everything fetched from the outside world goes through safe_fetch, which refuses private
addresses (SSRF), re-checks every redirect, and caps the body size. The saved-designs
assistant is optional: with UH_AI_KEY set it asks Anthropic about the items' own text, and
without it falls back to plain word matching. Its answers are suggestions only, and no
image is ever sent anywhere for analysis.
"""
import datetime as dt
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import requests
from PIL import Image

from common import Sheet, clean, split_qty_unit
from workspace_store import (CATALOG_COLLECTION_SHEET, CATALOG_COMMENT_SHEET, CATALOG_ITEM_SHEET,
                             ValidationError, next_row, put, records, validate_values)

TIMEOUT = (5, 20)
MAX_HTML = 3_000_000
MAX_IMAGE = 15_000_000
MAX_REDIRECTS = 4
MAX_CANDIDATES = 40
MAX_PIXELS = 50_000_000          # a decompression bomb is refused before Pillow inflates it
IMAGE_LIMIT = 1920               # stored copies are resized down to this long side
THUMB_LIMIT = 480
MIN_SIDE = 80                    # anything smaller is an icon, not an inspiration
PALETTE_SIZE = 5
# A plain browser user agent: many retail sites answer 403 to anything announcing itself
# as a bot, and the clipper fetches one page a person pasted, throttled, not a crawl.
# (Heavier bot walls - Akamai, Cloudflare challenges - still refuse; the bookmarklet covers those.)
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

COLLECTION_SHEET = CATALOG_COLLECTION_SHEET
ITEM_SHEET = CATALOG_ITEM_SHEET
COMMENT_SHEET = CATALOG_COMMENT_SHEET

COLLECTION_FIELDS = ["Collection ID", "Name", "Description", "Cover Image",
                     "Owner", "Created", "Updated"]
ITEM_FIELDS = ["Item ID", "Collection ID", "Title", "Description", "Notes", "Category",
               "Source URL", "Source Site", "Image File", "Thumb File", "Image Files", "Thumb Files",
               "Image Hash", "Colors", "Favorite", "Approval", "Saved By", "Saved On", "Updated",
               "Price", "Brand", "Model #", "Finish / Color", "Size / Configuration", "Qty / Unit"]
COMMENT_FIELDS = ["Comment ID", "Item ID", "Author", "Comment", "Created"]
# The fields an editor may write directly; everything else is derived or stamped here.
ITEM_EDIT_FIELDS = ["Title", "Description", "Notes", "Category", "Source URL",
                    "Source Site", "Favorite", "Approval", "Collection ID",
                    "Price", "Brand", "Model #", "Finish / Color", "Size / Configuration", "Qty / Unit"]

CATEGORIES = ["Kitchen", "Bathroom", "Living Room", "Bedroom", "Flooring & Tiles",
              "Lighting & Fixtures", "Ceiling Designs", "Wall Treatments", "Exterior",
              "Landscaping", "Furniture & Decor", "Other"]
APPROVALS = ["", "Pending feedback", "Approved", "Needs changes"]

CATEGORY_KEYWORDS = {
    "Kitchen": ["kitchen", "backsplash", "range hood", "cooktop", "island", "pantry", "cabinet"],
    "Bathroom": ["bathroom", "bath ", "shower", "vanity", "toilet", "bathtub", "tub ", "powder room"],
    "Bedroom": ["bedroom", "headboard", "nightstand", "master suite"],
    "Living Room": ["living room", "sofa", "fireplace", "family room", "lounge", "sectional"],
    "Flooring & Tiles": ["flooring", "floor tile", "hardwood", "laminate", "carpet", "vinyl plank", "tile "],
    "Lighting & Fixtures": ["lighting", "chandelier", "pendant", "sconce", "lamp", "faucet", "fixture"],
    "Ceiling Designs": ["ceiling", "coffered", "tray ceiling", "beam"],
    "Wall Treatments": ["wall panel", "wainscot", "wallpaper", "shiplap", "molding", "accent wall"],
    "Exterior": ["exterior", "facade", "elevation", "siding", "roof", "porch", "stucco", "brick"],
    "Landscaping": ["landscap", "garden", "patio", "backyard", "pool", "outdoor", "deck", "pergola"],
    "Furniture & Decor": ["furniture", "armchair", "dining table", "decor", "rug", "mirror", "shelf"],
}


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def new_id(prefix, existing):
    taken = set(existing)
    while True:
        candidate = f"{prefix}-{secrets.token_hex(4)}"
        if candidate not in taken:
            return candidate


def stamp():
    return dt.datetime.now().isoformat(timespec="seconds")


def collection_records(wb):
    return records(wb, COLLECTION_SHEET)


def item_records(wb):
    return records(wb, ITEM_SHEET)


def comment_records(wb):
    return records(wb, COMMENT_SHEET)


def find(rows, key, value):
    return next((row for row in rows if row.get(key) == value), None)


# One product is one inspiration, however many pictures of it were clipped. The row names
# its cover in Image File / Thumb File and every picture, cover first, in the Image Files /
# Thumb Files lists; an item saved before several pictures were possible carries only the
# cover, so these two readers fall back to it rather than reading as having no picture.
def name_list(value):
    return [part.strip() for part in clean(value).split(",") if part.strip()]


def item_pictures(item):
    """Every picture of one inspiration, cover first, as (image file, thumb file) pairs."""
    images, thumbs = name_list(item.get("Image Files")), name_list(item.get("Thumb Files"))
    if not images:
        images, thumbs = [clean(item.get("Image File"))], [clean(item.get("Thumb File"))]
    return [(name, thumbs[n] if n < len(thumbs) else name)
            for n, name in enumerate(images) if name]


def item_files(item):
    """Every stored file an inspiration points at — both sizes of each of its pictures."""
    return list(dict.fromkeys(name for pair in item_pictures(item) for name in pair if name))


def picture_names(processed):
    """The two stored names of one processed picture. Both are derived from the image's
    own hash, so identical bytes are stored once and shared by every item holding them."""
    return f"cat-{processed['hash']}{processed['ext']}", f"cat-{processed['hash']}-thumb.jpg"


# ---------------------------------------------------------------------------
# One shared team catalog. Locally there are no accounts; hosted, every signed-in member
# sees and edits every collection. Owner only records who created it (comments keep an
# author-or-owner delete rule) — per-collection privacy and sharing were deliberately
# removed, and their old workbook columns are dropped by ensure_catalog_sheets.
# ---------------------------------------------------------------------------

def visible_catalog(wb, user, require_login):
    """Every collection, its items and those items' comments — the whole team's catalog."""
    collections = list(collection_records(wb))
    ids = {c["Collection ID"] for c in collections}
    items = [i for i in item_records(wb) if i["Collection ID"] in ids]
    item_ids = {i["Item ID"] for i in items}
    comments = [c for c in comment_records(wb) if c["Item ID"] in item_ids]
    return collections, items, comments


def editable_collection(wb, collection_id, user, require_login):
    collection = find(collection_records(wb), "Collection ID", clean(collection_id))
    if not collection:
        raise ValidationError("Choose one of your collections.")
    return collection


# ---------------------------------------------------------------------------
# Mutations: each takes the open workbook, edits sheets in place and returns a history note.
# The route holds store.lock and calls store.save with the returned note.
# ---------------------------------------------------------------------------

def save_collection(wb, data, user, require_login):
    values = validate_values(data.get("values"), COLLECTION_FIELDS)
    sheet = Sheet(wb[COLLECTION_SHEET])
    rows = collection_records(wb)
    existing = find(rows, "Collection ID", values.get("Collection ID")) if values.get("Collection ID") else None
    if data.get("delete"):
        if not existing:
            raise ValidationError("This collection no longer exists. Refresh the page.")
        owned = [i for i in item_records(wb) if i["Collection ID"] == existing["Collection ID"]]
        if owned and not data.get("cascade"):
            raise ValidationError(f"This collection holds {len(owned)} inspirations. Confirm deleting them with it.")
        removed_files = []
        # Deleting a row shifts every row under it, so each pass re-reads before deleting.
        while True:
            remaining = [i for i in item_records(wb) if i["Collection ID"] == existing["Collection ID"]]
            if not remaining:
                break
            removed_files += delete_item_row(wb, remaining[-1])
        from workspace_store import delete_record
        existing = find(collection_records(wb), "Collection ID", existing["Collection ID"])
        delete_record(wb, COLLECTION_SHEET, existing["_row"])
        return (f"catalog: delete collection {existing['Name']} (-{len(owned)} inspirations)", removed_files)
    name = clean(values.get("Name"))
    if not name or len(name) > 120:
        raise ValidationError("Give the collection a name of up to 120 characters.")
    if len(clean(values.get("Description"))) > 2000:
        raise ValidationError("A collection description can be up to 2,000 characters.")
    if existing:
        put(sheet, existing["_row"], {"Name": name, "Description": clean(values.get("Description")),
                                      "Cover Image": clean(values.get("Cover Image")) or existing.get("Cover Image", ""),
                                      "Updated": stamp()})
        return (f"catalog: collection {name}", [])
    if any(c["Name"].lower() == name.lower() for c in rows):
        raise ValidationError("A collection with that name already exists.")
    collection_id = new_id("col", (c["Collection ID"] for c in rows))
    row = next_row(sheet)
    put(sheet, row, {"Collection ID": collection_id, "Name": name,
                     "Description": clean(values.get("Description")),
                     "Owner": (user or ""), "Cover Image": "",
                     "Created": stamp(), "Updated": stamp()})
    return (f"catalog: new collection {name}", collection_id)


def touch_collection(wb, collection_id):
    collection = find(collection_records(wb), "Collection ID", collection_id)
    if collection:
        put(Sheet(wb[COLLECTION_SHEET]), collection["_row"], {"Updated": stamp()})


def delete_item_row(wb, item):
    """Remove one inspiration row; returns the image files nothing references any more."""
    from workspace_store import delete_record
    others = [i for i in item_records(wb) if i["_row"] != item["_row"]]
    held = {name for other in others for name in item_files(other)}
    orphaned = [name for name in item_files(item) if name not in held]
    for comment in sorted((c for c in comment_records(wb) if c["Item ID"] == item["Item ID"]),
                          key=lambda c: c["_row"], reverse=True):
        delete_record(wb, COMMENT_SHEET, comment["_row"])
    delete_record(wb, ITEM_SHEET, item["_row"])
    return orphaned


def save_item(wb, data, user, require_login):
    """Edit, move, favorite or delete one saved inspiration."""
    rows = item_records(wb)
    item = find(rows, "Item ID", clean(data.get("item")))
    if not item:
        raise ValidationError("This inspiration no longer exists. Refresh the page.")
    editable_collection(wb, item["Collection ID"], user, require_login)
    if data.get("delete"):
        orphans = delete_item_row(wb, item)
        touch_collection(wb, item["Collection ID"])
        return (f"catalog: delete inspiration {item['Title'] or item['Item ID']}", orphans)
    values = validate_values(data.get("values"), ITEM_EDIT_FIELDS)
    if "Category" in values and values["Category"] and values["Category"] not in CATEGORIES:
        raise ValidationError(f"Category must be one of: {', '.join(CATEGORIES)}.")
    if "Approval" in values and values["Approval"] not in APPROVALS:
        raise ValidationError("Approval must be Pending feedback, Approved or Needs changes.")
    if "Favorite" in values and values["Favorite"] not in {"", "Yes"}:
        raise ValidationError("Favorite must be Yes or empty.")
    if "Source URL" in values and values["Source URL"]:
        parts = urlsplit(values["Source URL"])
        if parts.scheme not in {"http", "https"}:
            raise ValidationError("The source must be a full http(s) address.")
    if values.get("Collection ID") and values["Collection ID"] != item["Collection ID"]:
        editable_collection(wb, values["Collection ID"], user, require_login)
        touch_collection(wb, values["Collection ID"])
    put(Sheet(wb[ITEM_SHEET]), item["_row"], {**values, "Updated": stamp()})
    touch_collection(wb, item["Collection ID"])
    return (f"catalog: update inspiration {values.get('Title') or item['Title'] or item['Item ID']}", None)


def add_item(wb, collection_id, pictures, meta, user, require_login):
    """One clip becomes one saved inspiration carrying every picture that was picked.

    A product page shows the same item from several angles, so the pictures belong on one
    record rather than on one record each. The first is the cover. Returns the new item's
    id, its pictures as (image file, thumb file) pairs, and the ones whose bytes are not
    stored yet — the caller writes those once the workbook has accepted the row.
    """
    collection = editable_collection(wb, collection_id, user, require_login)
    rows = item_records(wb)
    chosen, seen = [], []
    for one in pictures:
        image_name, thumb_name = picture_names(one)
        if image_name in seen:          # the same picture picked twice is still one picture
            continue
        seen.append(image_name)
        chosen.append((one, image_name, thumb_name))
    if not chosen:
        raise ValidationError("Clip an image: pick one from the page, paste an image address, or upload a file.")
    # Saving a product whose every picture is already filed here is the clip happening
    # twice; a clip that adds a picture to what is held is a different product shot.
    duplicate = next((i for i in rows if i["Collection ID"] == collection["Collection ID"]
                      and set(seen) <= set(item_files(i))), None)
    if duplicate:
        raise ValidationError(f"{'This image is' if len(seen) == 1 else 'These images are'} already in "
                              f"that collection as “{duplicate['Title'] or 'Untitled'}”.")
    held = {name for other in rows for name in item_files(other)}
    unstored = [one for one, image_name, _ in chosen if image_name not in held]
    processed, image_name, thumb_name = chosen[0]
    title = clean(meta.get("title"))[:200]
    category = clean(meta.get("category"))
    if category and category not in CATEGORIES:
        raise ValidationError(f"Category must be one of: {', '.join(CATEGORIES)}.")
    if not category:
        category = keyword_category(" ".join([title, clean(meta.get("description")),
                                              clean(meta.get("source_url"))]))
    item_id = new_id("ins", (i["Item ID"] for i in rows))
    sheet = Sheet(wb[ITEM_SHEET])
    put(sheet, next_row(sheet), {
        "Item ID": item_id, "Collection ID": collection["Collection ID"], "Title": title,
        "Description": clean(meta.get("description"))[:2000], "Notes": clean(meta.get("notes"))[:2000],
        "Category": category,
        "Source URL": clean(meta.get("source_url"))[:2000], "Source Site": clean(meta.get("source_site"))[:200],
        "Image File": image_name, "Thumb File": thumb_name, "Image Hash": processed["hash"],
        "Image Files": ", ".join(name for _, name, _ in chosen),
        "Thumb Files": ", ".join(name for _, _, name in chosen),
        "Colors": ", ".join(processed["colors"]),
        "Favorite": "", "Approval": "", "Saved By": (user or ""), "Saved On": stamp(), "Updated": stamp(),
        "Price": clean(meta.get("price"))[:50], "Brand": clean(meta.get("brand"))[:200],
        "Model #": clean(meta.get("model"))[:100], "Finish / Color": clean(meta.get("finish"))[:200],
        "Size / Configuration": clean(meta.get("size"))[:200], "Qty / Unit": clean(meta.get("qty_unit"))[:60],
    })
    touch_collection(wb, collection["Collection ID"])
    return item_id, [(image, thumb) for _, image, thumb in chosen], unstored


def viewable_item(wb, item_id, user, require_login):
    """An inspiration this person may see — enough to assign it to a project, since the
    assignment writes a Selections row and copies values rather than editing the catalog."""
    item = find(item_records(wb), "Item ID", clean(item_id))
    if not item:
        raise ValidationError("This inspiration no longer exists. Refresh the page.")
    if not find(collection_records(wb), "Collection ID", item["Collection ID"]):
        raise ValidationError("This inspiration no longer exists. Refresh the page.")
    return item


def selection_values_for(item, project_id, extra=None):
    """The Selections row one catalog item opens in a project: a copy of the clipped details
    plus the Catalog Item ID reference, so the master record is never duplicated or edited.
    Project-specific fields (quantity, room, notes, status) start their own life here."""
    extra = extra or {}
    price = price_number(item.get("Price"))
    pair = split_qty_unit(item.get("Qty / Unit"))
    return {
        "Project ID": project_id,
        "Item": clean(item.get("Title")) or f"Inspiration {item['Item ID']}",
        "Section": clean(extra.get("Section")) or clean(item.get("Category")),
        "Room / Area": clean(extra.get("Room / Area")),
        "Location": clean(extra.get("Location")),
        "Qty": extra.get("Qty") or pair["Qty"] or 1,
        "Unit": pair["Unit"],
        "Unit Price": price if price is not None else "",
        "Manufacturer": clean(item.get("Brand")) or clean(item.get("Source Site")),
        "Model #": clean(item.get("Model #")),
        "Finish / Color": clean(item.get("Finish / Color")),
        "Size / Configuration": clean(item.get("Size / Configuration")),
        "Product Name": clean(item.get("Title")),
        "Product URL": clean(item.get("Source URL")),
        "Client Notes": clean(extra.get("Client Notes")),
        "Catalog Item ID": item["Item ID"],
    }


def add_comment(wb, data, user, require_login):
    if data.get("delete"):
        comment = find(comment_records(wb), "Comment ID", clean(data.get("comment")))
        if not comment:
            raise ValidationError("This comment no longer exists. Refresh the page.")
        author = clean(comment.get("Author")).lower()
        if require_login and author and author != (user or "").lower():
            item = find(item_records(wb), "Item ID", comment["Item ID"])
            collection = find(collection_records(wb), "Collection ID", item["Collection ID"]) if item else None
            if not collection or clean(collection.get("Owner")).lower() != (user or "").lower():
                raise ValidationError("Only the comment's author or the collection's owner can remove it.")
        from workspace_store import delete_record
        delete_record(wb, COMMENT_SHEET, comment["_row"])
        return "catalog: delete comment"
    item = find(item_records(wb), "Item ID", clean(data.get("item")))
    if not item:
        raise ValidationError("This inspiration no longer exists. Refresh the page.")
    if not find(collection_records(wb), "Collection ID", item["Collection ID"]):
        raise ValidationError("Choose one of your collections.")
    text = clean(data.get("text"))
    if not text or len(text) > 2000:
        raise ValidationError("A comment needs 1–2,000 characters.")
    sheet = Sheet(wb[COMMENT_SHEET])
    put(sheet, next_row(sheet), {"Comment ID": new_id("com", (c["Comment ID"] for c in comment_records(wb))),
                                 "Item ID": item["Item ID"], "Author": (user or ""),
                                 "Comment": text, "Created": stamp()})
    return f"catalog: comment on {item['Title'] or item['Item ID']}"


# ---------------------------------------------------------------------------
# Fetching the outside world, safely.
# ---------------------------------------------------------------------------

def check_address(host):
    """Refuse anything that resolves inside a private network, so a pasted URL cannot be
    turned into a request against this machine or its neighbours (SSRF)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValidationError("That address could not be found. Check the URL.") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValidationError("That address points inside a private network, which the clipper will not fetch.")


def safe_fetch(url, limit, accept="*/*"):
    """GET an external URL: http(s) only, public addresses only, every redirect re-checked,
    and the body capped at `limit` bytes. Returns (body, content_type, final_url)."""
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValidationError("Enter a full public address starting with http:// or https://.")
        check_address(parts.hostname)
        try:
            response = requests.get(url, stream=True, timeout=TIMEOUT, allow_redirects=False,
                                    headers={"User-Agent": USER_AGENT, "Accept": accept,
                                             "Accept-Language": "en-US,en;q=0.9"})
        except requests.RequestException:
            raise ValidationError("That site could not be reached. Check the URL and try again.") from None
        with response:
            if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("Location"):
                url = urljoin(url, response.headers["Location"])
                continue
            if response.status_code >= 400:
                raise ValidationError("That site would not hand this over to the app.")
            body = io.BytesIO()
            for chunk in response.iter_content(65536):
                body.write(chunk)
                if body.tell() > limit:
                    raise ValidationError("That file is too large to clip.")
            return body.getvalue(), response.headers.get("Content-Type", ""), url
    raise ValidationError("That address redirects too many times.")


class PageParser(HTMLParser):
    """Just enough HTML reading for the clipper: meta tags, microdata, the title and every image."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.images, self._title, self._in_title = {}, [], [], False
        self.ld_scripts, self._ld = [], None
        self.micro = {}

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if a.get("itemprop") and a.get("content"):
            self.micro.setdefault(a["itemprop"].lower(), a["content"])
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
        elif tag == "img":
            src = a.get("src") or a.get("data-src") or a.get("data-original") or ""
            if not src and a.get("srcset"):
                src = a["srcset"].split(",")[-1].strip().split(" ")[0]
            if src:
                self.images.append({"url": src, "alt": a.get("alt", ""),
                                    "width": a.get("width", ""), "height": a.get("height", "")})
        elif tag == "title":
            self._in_title = True
        elif tag == "script" and a.get("type", "").lower().strip() == "application/ld+json":
            self._ld = []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._ld is not None:
            self.ld_scripts.append("".join(self._ld))
            self._ld = None

    def handle_data(self, data):
        if self._in_title:
            self._title.append(data)
        if self._ld is not None:
            self._ld.append(data)

    @property
    def title(self):
        return clean("".join(self._title))


def tiny(entry):
    """Images declaring themselves a few pixels wide are tracking beacons and sprites."""
    try:
        return (entry["width"] and int(re.sub(r"\D", "", entry["width"]) or 0) < 50) or \
               (entry["height"] and int(re.sub(r"\D", "", entry["height"]) or 0) < 50)
    except ValueError:
        return False


def ld_products(scripts):
    """Every JSON-LD Product object on the page, however the site nested it."""
    found = []

    def walk(node):
        if isinstance(node, list):
            for entry in node:
                walk(entry)
        elif isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(isinstance(k, str) and k.lower() == "product" for k in kinds):
                found.append(node)
            for value in node.values():
                walk(value)

    for script in scripts:
        try:
            walk(json.loads(script))
        except (ValueError, TypeError):
            continue
    return found


def product_info(meta, ld_scripts, micro=None):
    """The spec-sheet fields read as suggestions from the page's own structured data —
    JSON-LD products first, then Open Graph / product meta tags, then microdata — plus
    the product's name and its own photos, which are the cleanest candidates a page has."""
    micro = micro or {}

    def name_of(node):
        return clean(node.get("name") if isinstance(node, dict) else node)

    info = {"title": "", "price": "", "brand": "", "model": "", "finish": "", "size": ""}
    currency, images = "", []
    for product in ld_products(ld_scripts):
        offers = product.get("offers")
        offers = offers[0] if isinstance(offers, list) and offers else offers
        if isinstance(offers, dict) and not info["price"]:
            info["price"] = clean(offers.get("price") or offers.get("lowPrice"))
            currency = clean(offers.get("priceCurrency"))
        info["title"] = info["title"] or name_of(product.get("name"))
        info["brand"] = info["brand"] or name_of(product.get("brand"))
        info["model"] = info["model"] or clean(product.get("sku") or product.get("mpn")) or name_of(product.get("model"))
        info["finish"] = info["finish"] or name_of(product.get("color"))
        info["size"] = info["size"] or name_of(product.get("size"))
        image = product.get("image")
        for entry in (image if isinstance(image, list) else [image]):
            if isinstance(entry, dict):
                entry = entry.get("url") or entry.get("contentUrl")
            if isinstance(entry, str) and clean(entry):
                images.append(clean(entry))
    fallbacks = {"price": ("product:price:amount", "og:price:amount"),
                 "brand": ("product:brand", "og:brand"),
                 "model": ("product:retailer_part_no", "product:mfr_part_no"),
                 "finish": ("product:color",), "size": ("product:size",)}
    for key, names in fallbacks.items():
        info[key] = info[key] or clean(next((meta[n] for n in names if meta.get(n)), ""))
    # Microdata (itemprop) is the last resort, for pages with neither JSON-LD nor og tags.
    micro_keys = {"price": ("price", "lowprice"), "brand": ("brand",), "model": ("sku", "mpn", "model"),
                  "finish": ("color",), "size": ("size",)}
    for key, names in micro_keys.items():
        info[key] = info[key] or clean(next((micro[n] for n in names if micro.get(n)), ""))
    currency = currency or clean(meta.get("product:price:currency") or meta.get("og:price:currency")
                                 or micro.get("pricecurrency"))
    if info["price"] and currency and currency.upper() not in info["price"].upper():
        info["price"] = f"{info['price']} {currency.upper()}"
    limits = {"title": 200, "price": 50, "brand": 200, "model": 100, "finish": 200, "size": 200}
    return {**{key: value[:limits[key]] for key, value in info.items()}, "_images": images}


def price_number(text):
    """The number inside a clipped price ('$1,299.00 USD' -> 1299.0), or None."""
    match = re.search(r"\d[\d,]*(?:\.\d+)?", clean(text))
    if not match:
        return None
    try:
        value = float(match[0].replace(",", ""))
    except ValueError:
        return None
    return value if 0 <= value <= 100_000_000 else None


def parse_page(html, base_url):
    """A page's clip-relevant metadata and its candidate images, largest-intent first."""
    parser = PageParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    meta = parser.meta
    host = urlsplit(base_url).hostname or ""
    candidates, seen = [], set()

    def add(url, alt=""):
        url = urljoin(base_url, clean(url))
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or url in seen:
            return
        if parts.path.lower().endswith(".svg") or "sprite" in parts.path.lower():
            return
        seen.add(url)
        candidates.append({"url": url, "alt": clean(alt)[:200]})

    product = product_info(meta, parser.ld_scripts, parser.micro)
    # The product's own name over the page title, and its own photos right after og:image:
    # on a retail page they are the item and its picture, not the furniture around them.
    title = product.pop("title") or clean(meta.get("og:title") or parser.title)[:200]
    for key in ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"):
        if meta.get(key):
            add(meta[key], meta.get("og:title", ""))
    for url in product.pop("_images"):
        add(url, title)
    for entry in parser.images:
        if not tiny(entry):
            add(entry["url"], entry["alt"])
    return {"title": title,
            "description": clean(meta.get("og:description") or meta.get("description"))[:2000],
            "site": clean(meta.get("og:site_name") or host.removeprefix("www."))[:200],
            "source_url": base_url,
            **product,
            "images": candidates[:MAX_CANDIDATES]}


def inspect_page(url):
    html, content_type, final_url = safe_fetch(clean(url), MAX_HTML,
                                               accept="text/html,application/xhtml+xml")
    if "html" not in content_type.lower() and not html.lstrip()[:200].lower().startswith((b"<!doctype", b"<html")):
        raise ValidationError("That address is not a web page. Paste the image's address in the Image URL tab instead.")
    return parse_page(html.decode("utf-8", "replace"), final_url)


def fetch_image(url):
    raw, content_type, final_url = safe_fetch(clean(url), MAX_IMAGE, accept="image/*,*/*;q=0.5")
    main = content_type.split(";")[0].strip().lower()
    if main and not main.startswith("image/") and main not in {"application/octet-stream", ""}:
        raise ValidationError("That address did not return an image. Try another picture from the page.")
    return raw


# ---------------------------------------------------------------------------
# Image processing: validate, optimise, thumbnail, hash and read the palette.
# ---------------------------------------------------------------------------

def process_image(raw):
    if len(raw) > MAX_IMAGE:
        raise ValidationError("Choose an image smaller than 15 MB.")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > MAX_PIXELS:
                raise ValidationError("That image has too many pixels to process safely.")
            image = source.convert("RGBA") if source.mode in {"RGBA", "LA", "PA", "P"} else source.convert("RGB")
    except ValidationError:
        raise
    except Exception:
        raise ValidationError("That file is not a readable image. Choose a PNG, JPEG or WebP.") from None
    if image.width < MIN_SIDE or image.height < MIN_SIDE:
        raise ValidationError("That image is too small to keep as an inspiration.")
    image.thumbnail((IMAGE_LIMIT, IMAGE_LIMIT), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    if image.mode == "RGBA" and image.getchannel("A").getextrema()[0] < 255:
        image.save(buffer, "PNG")
        ext, mime = ".png", "image/png"
    else:
        image = image.convert("RGB")
        image.save(buffer, "JPEG", quality=85)
        ext, mime = ".jpg", "image/jpeg"
    flat = image.convert("RGB")
    thumb = flat.copy()
    thumb.thumbnail((THUMB_LIMIT, THUMB_LIMIT), Image.Resampling.LANCZOS)
    thumb_buffer = io.BytesIO()
    thumb.save(thumb_buffer, "JPEG", quality=80)
    return {"image": buffer.getvalue(), "ext": ext, "mime": mime, "thumb": thumb_buffer.getvalue(),
            "hash": hashlib.sha256(buffer.getvalue()).hexdigest()[:16],
            "width": image.width, "height": image.height, "colors": palette(flat)}


def palette(image):
    """The image's dominant colours as hex strings, most prominent first."""
    small = image.convert("RGB").copy()
    small.thumbnail((100, 100))
    quantized = small.quantize(colors=PALETTE_SIZE + 2, method=Image.Quantize.FASTOCTREE)
    counts = sorted(quantized.getcolors(maxcolors=256) or [], reverse=True)
    raw_palette = quantized.getpalette() or []
    out = []
    for _, index in counts[:PALETTE_SIZE]:
        r, g, b = raw_palette[index * 3: index * 3 + 3]
        out.append(f"#{r:02x}{g:02x}{b:02x}")
    return out


def store_processed(store, processed):
    image_name, thumb_name = picture_names(processed)
    store.store_catalog_image(image_name, processed["image"], processed["mime"])
    store.store_catalog_image(thumb_name, processed["thumb"], "image/jpeg")
    return image_name, thumb_name


# ---------------------------------------------------------------------------
# Local intelligence: keyword category, text search and similarity.
# These carry the feature without any key, and back the AI up when it is unsure.
# ---------------------------------------------------------------------------

def keyword_category(text):
    text = f" {clean(text).lower()} "
    best, score = "", 0
    for category, words in CATEGORY_KEYWORDS.items():
        hits = sum(1 for word in words if word in text)
        if hits > score:
            best, score = category, hits
    return best


def item_text(item):
    return " ".join(clean(item.get(field)) for field in
                    ("Title", "Description", "Notes", "Category", "Source Site")).lower()


def search_items(question, items):
    """Plain word matching over everything an item says about itself, best matches first."""
    words = [w for w in re.findall(r"[a-z0-9]+", clean(question).lower())
             if w not in {"the", "a", "an", "my", "all", "with", "show", "find", "me", "of", "and", "in", "designs", "design"}]
    scored = []
    for item in items:
        text = item_text(item)
        hits = sum(1 for word in words if word in text)
        if hits:
            scored.append((hits, item))
    scored.sort(key=lambda pair: (-pair[0], pair[1].get("Saved On", "")))
    return [item for _, item in scored]


def similar_items(item, items, limit=8):
    """Inspirations close to this one by category and palette, from the same saved catalog."""
    def color_bits(value):
        out = []
        for part in clean(value).split(","):
            part = part.strip().lstrip("#")
            if len(part) == 6:
                try:
                    out.append(tuple(int(part[i:i + 2], 16) for i in (0, 2, 4)))
                except ValueError:
                    pass
        return out

    base_colors = color_bits(item.get("Colors"))
    scored = []
    for other in items:
        if other["Item ID"] == item["Item ID"]:
            continue
        score = 0
        if other.get("Category") and other["Category"] == item.get("Category"):
            score += 3
        colors = color_bits(other.get("Colors"))
        if base_colors and colors:
            nearest = min(sum(abs(a - b) for a, b in zip(c1, c2)) for c1 in base_colors for c2 in colors)
            if nearest < 90:
                score += 2
            elif nearest < 180:
                score += 1
        if score:
            scored.append((score, other))
    scored.sort(key=lambda pair: -pair[0])
    return [other["Item ID"] for _, other in scored[:limit]]


# ---------------------------------------------------------------------------
# AI, through Anthropic's text API, for the assistant alone. Optional: everything above
# works without it, and no image is ever sent — there is deliberately no image analysis.
# The key stays server-side and never reaches a log, an error or the browser.
# ---------------------------------------------------------------------------

AI_URL = "https://api.anthropic.com/v1/messages"
AI_VERSION = "2023-06-01"
AI_TIMEOUT = (5, 60)


def ai_settings():
    key = (os.getenv("UH_AI_KEY") or "").strip()
    if not key:
        return None
    return {"key": key, "model": (os.getenv("UH_AI_MODEL") or "claude-sonnet-4-5").strip()}


def ai_request(config, messages, max_tokens=800):
    try:
        response = requests.post(AI_URL, json={"model": config["model"], "max_tokens": max_tokens,
                                               "messages": messages},
                                 headers={"x-api-key": config["key"], "anthropic-version": AI_VERSION,
                                          "Content-Type": "application/json"},
                                 timeout=AI_TIMEOUT)
    except requests.RequestException:
        raise ValidationError("The AI service could not be reached. Try again shortly.") from None
    if response.status_code >= 400:
        # The body can echo the request, so only the status reaches the caller.
        raise ValidationError(f"The AI service refused the request (HTTP {response.status_code}). "
                              "Check UH_AI_KEY and UH_AI_MODEL.")
    content = (response.json() or {}).get("content") or []
    return "".join(block.get("text", "") for block in content if block.get("type") == "text")


def parse_ai_json(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValidationError("The AI answer could not be read. Try again.")
    try:
        return json.loads(match.group(0))
    except ValueError:
        raise ValidationError("The AI answer could not be read. Try again.") from None


def assistant_answer(question, items, config):
    """Answer a question about the saved inspirations, naming the items it means."""
    question = clean(question)
    if not question or len(question) > 500:
        raise ValidationError("Ask a question of up to 500 characters.")
    if not config:
        matches = search_items(question, items)
        if not matches:
            return {"answer": "No saved inspirations match that. Try different words, or clip a few more designs first.",
                    "item_ids": [], "source": "local"}
        return {"answer": f"Found {len(matches)} saved inspiration{'s' if len(matches) != 1 else ''} matching your question.",
                "item_ids": [m["Item ID"] for m in matches[:24]], "source": "local"}
    lines = []
    for item in items[:300]:
        lines.append(json.dumps({"id": item["Item ID"], "title": item.get("Title", "")[:80],
                                 "category": item.get("Category", ""), "colors": item.get("Colors", ""),
                                 "collection": item.get("Collection ID", ""), "notes": item.get("Notes", "")[:120]},
                                ensure_ascii=False))
    prompt = ("Here is a home-design inspiration library, one JSON object per line:\n" + "\n".join(lines) +
              f"\n\nQuestion: {question}\n\nAnswer with a single JSON object, nothing else: "
              '{"answer": "<a short helpful answer>", "item_ids": ["<ids of the matching items>"]}')
    parsed = parse_ai_json(ai_request(config, [{"role": "user", "content": prompt}], max_tokens=1000))
    known = {item["Item ID"] for item in items}
    ids = [clean(i) for i in parsed.get("item_ids") or [] if clean(i) in known][:24]
    return {"answer": clean(parsed.get("answer"))[:2000] or "Here is what I found.",
            "item_ids": ids, "source": "ai"}
