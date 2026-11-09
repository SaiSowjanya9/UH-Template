"""
Seed a project's line items from spec_template.json.

The template is a three-level outline - location (floor or zone), room, item - which maps
onto the Selections columns Location, Room / Area and Item; each item's Section category
is inferred from the same rules the location migration uses. Applying it adds every missing
line item in one workbook save and never touches rows that already exist, so it is safe to
re-run after the template grows or a room is added to a house.
"""
import json

from copy import copy

from common import BASE_DIR, Sheet, clean
from workbook_store import (MAX_LOCATIONS, ValidationError, ensure_location_list,
                            ensure_selection_columns, infer_category, location_list, put)

TEMPLATE_PATH = BASE_DIR / "spec_template.json"


def load(path=None):
    try:
        data = json.loads((path or TEMPLATE_PATH).read_text(encoding="utf-8"))
        sections = data["sections"]
    except (OSError, ValueError, KeyError, TypeError):
        raise ValidationError("The spec template file is missing or unreadable.") from None
    out = []
    for entry in sections:
        location = clean(entry.get("location") or entry.get("section"))
        rooms = [(clean(room.get("room")), [clean(item) for item in room.get("items", []) if clean(item)])
                 for room in entry.get("rooms", [])]
        if location:
            out.append((location, [(room, items) for room, items in rooms if items]))
    if not out:
        raise ValidationError("The spec template does not list any locations.")
    return data.get("name", "template"), out


def rows(template):
    """Flatten the outline into (location, room, item) in template order."""
    return [(location, room, item) for location, rooms in template for room, items in rooms for item in items]


def register_locations(wb, names):
    """Append new locations to the Lists sheet so they keep print order and stay in the dropdown."""
    column = ensure_location_list(wb)
    ws = wb["Lists"]
    _, existing = location_list(ws)
    added = [name for name in names if name not in existing]
    if not added:
        return []
    if len(existing) + len(added) > MAX_LOCATIONS:
        raise ValidationError(f"The Lists sheet holds up to {MAX_LOCATIONS} locations; remove unused ones first.")
    for offset, name in enumerate(added):
        ws.cell(len(existing) + 2 + offset, column, name)
    return added


def apply(wb, project_id, template):
    """Add the template's missing line items to one project. Returns (added, skipped)."""
    sheet = ensure_selection_columns(wb)
    present, used = {}, set()
    for row, data in sheet.rows():
        used.add(row)
        if clean(data.get("Project ID")) == project_id:
            key = (clean(data.get("Location")), clean(data.get("Room / Area")), clean(data.get("Item")))
            present[key] = present.get(key, 0) + 1
    # One pass over the free rows instead of rescanning the sheet for every insert.
    cursor = 2

    def take():
        nonlocal cursor
        while cursor in used:
            cursor += 1
        if cursor > 10001:
            raise ValidationError("This local workbook supports up to 10,000 rows per sheet.")
        used.add(cursor)
        if cursor > 2:
            for column in sheet.cols.values():
                sheet.ws.cell(cursor, column)._style = copy(sheet.ws.cell(2, column)._style)
        return cursor

    wanted = {}
    added = skipped = 0
    for location, room, item in rows(template):
        key = (location, room, item)
        wanted[key] = wanted.get(key, 0) + 1
        if wanted[key] <= present.get(key, 0):
            skipped += 1          # already specified for this project
            continue
        put(sheet, take(), {"Project ID": project_id, "Location": location,
                            "Section": infer_category(room, item), "Room / Area": room, "Item": item,
                            "Include in Lookbook": "Yes", "Client Status": "Proposed"})
        added += 1
    return added, skipped
