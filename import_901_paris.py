"""One-shot: rebuild the 901 Paris Drive project in the portal from
901_Paris_Drive_Spec_Sheet.xlsx (Room Specs + Bulk Items tabs).

The sheet's "Whole House" scope is the portal's "Entire Home" location, which is
registered on the Lists sheet if missing. The sheet's own Category names land on
the portal's existing Section list (kitchen-area rooms under Kitchen, bath rooms
under Bathrooms, the rest by trade), so the category dropdown stays one clean
vocabulary. Every existing line item on the project is replaced: the Reference
Schedule tab is the old data and is not re-imported.

Reads .env for Supabase, like the app, and writes through WorkspaceStore, so the
save is validated, backed up as a version, and atomic. --dry-run prints the
mapping and the row count without touching the workspace.
"""
import argparse
import re
import sys
from pathlib import Path

from openpyxl import load_workbook

import common
import spec_template
import supabase_store
from common import clean
from workspace_store import (OPTIONAL_SELECTION_FIELDS, SELECTION_FIELDS, WorkspaceStore,
                             delete_record, ensure_selection_columns, next_row, put, records,
                             validate_values)

DEFAULT_PROJECT = "UH-001"  # Velora Plan, 901 Paris Drive, Prosper TX
SOURCE = Path.home() / "Downloads" / "901_Paris_Drive_Spec_Sheet.xlsx"

# The sheet's Floor / Scope column is the portal's Location; "whole house" there
# means "entire home" here. Floors pass through under their own names.
def location(floor):
    return {"Whole House": "Entire Home"}.get(floor, floor)


# A room value of "Whole House" names the same scope: the whole home.
def room(value):
    return {"Whole House": "Entire Home"}.get(value, value)


def is_bath(room_name):
    name = room_name.lower()
    return "bath" in name or "powder" in name


def is_kitchen(room_name):
    return any(word in room_name.lower() for word in ("kitchen", "pantry", "coffee", "dry bar"))


# What the sheet calls a Category, filed under the portal's Section list. The two
# room-based rules mirror the schedule the project already followed: everything
# in a bathroom files under Bathrooms, everything in a kitchen-area room under
# Kitchen; everywhere else the category decides the trade.
TRADE_MAP = {
    "Area": "Other",
    "Appliance": "Appliances",
    "Backsplash": "Other",
    "Cabinet": "Interior Doors & Trim",
    "Cabinet accessories": "Hardware",
    "Cabinet appliance coordination": "Kitchen",
    "Cabinet coordination": "Interior Doors & Trim",
    "Countertop": "Other",
    "Doors & trim": "Interior Doors & Trim",
    "Exterior": "Exterior",
    "Exterior paint": "Exterior",
    "Exterior siding": "Exterior",
    "Fireplace": "Other",
    "Floor tile": "Flooring",
    "Flooring": "Flooring",
    "Lighting": "Lighting",
    "Paint": "Paint",
    "Plumbing": "Bathrooms",
    "Room scope": "Other",
    "Shower pan mosaic": "Flooring",
    "Stair package": "Other",
    "Wall accent tile": "Flooring",
    "Wall tile": "Flooring",
    "Wall tile accent": "Flooring",
    "Window coverings": "Windows & Doors",
}

# The Bulk Items tab has no room column; its whole-house scope lands on
# Entire Home and its Category column files under the same Section list.
BULK_MAP = {
    "Framing": "Other",
    "Foundation": "Other",
    "Insulation": "Other",
    "HVAC system": "Other",
    "Water heater": "Other",
    "Thermostat": "Other",
    "Life safety": "Other",
    "Low voltage": "Other",
    "Drywall": "Other",
    "Interior trim": "Interior Doors & Trim",
    "Exterior": "Exterior",
    "Roofing": "Roofing",
    "Windows": "Windows & Doors",
    "Exterior doors": "Windows & Doors",
    "Interior doors": "Interior Doors & Trim",
    "Garage": "Other",
    "Landscaping": "Exterior",
    "Flatwork": "Other",
    "Exterior paint": "Exterior",
    "Exterior siding": "Exterior",
}


def section(category, room_name):
    if is_bath(room_name):
        return "Bathrooms"
    if is_kitchen(room_name):
        return "Kitchen"
    return TRADE_MAP.get(category, "Other")


def quantity(value):
    """The sheet's Qty as a number string the workspace can store, or ''."""
    text = clean(value).replace(",", "")
    if not text:
        return ""
    try:
        float(text)
    except ValueError:
        return ""
    return text


def read_rows(path, project):
    wb = load_workbook(path, data_only=True)
    rows, unmapped = [], set()

    def add(loc, rm, cat, item, mfr="", model="", finish="", size="", qty="", notes="",
            bulk=False):
        if not clean(item):
            return
        sec = BULK_MAP.get(cat) if bulk else (section(cat, rm))
        if sec is None or (cat and cat not in TRADE_MAP and not is_bath(rm)
                           and not is_kitchen(rm) and not bulk):
            unmapped.add(cat)
        rows.append(validate_values({
            "Project ID": project, "Section": sec or "Other", "Location": loc,
            "Room / Area": rm, "Item": item, "Manufacturer": clean(mfr),
            "Model #": clean(model), "Finish / Color": clean(finish),
            "Size / Configuration": clean(size), "Qty": quantity(qty),
            "Client Notes": clean(notes), "Client Status": "Proposed",
            "Include in Lookbook": "Yes"},
            SELECTION_FIELDS + OPTIONAL_SELECTION_FIELDS))

    specs = wb["Room Specs"]
    for cells in specs.iter_rows(min_row=4, values_only=True):
        floor, rm, cat, item, mfr, model, finish, size, qty, notes = (list(cells) + [None] * 10)[:10]
        if not any(clean(c) for c in (floor, rm, cat, item)):
            continue
        add(location(clean(floor)), room(clean(rm)), clean(cat), clean(item),
            mfr, model, finish, size, qty, notes)

    bulk = wb["Bulk Items"]
    for cells in bulk.iter_rows(min_row=5, values_only=True):
        scope, cat, item, notes = (list(cells) + [None] * 4)[:4]
        if not any(clean(c) for c in (scope, cat, item, notes)):
            continue
        add(location(clean(scope)), "Entire Home", clean(cat), clean(item), notes=notes, bulk=True)

    return rows, unmapped


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", default=str(SOURCE), help="the spec sheet to import")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="the portal project to rebuild")
    parser.add_argument("--dry-run", action="store_true", help="print the mapping and stop")
    args = parser.parse_args()
    project = args.project

    rows, unmapped = read_rows(Path(args.file), project)
    if unmapped:
        print("categories with no section mapping (filed under Other):", ", ".join(sorted(unmapped)))
    print(f"rows to write: {len(rows)}")
    from collections import Counter
    print("by location:", dict(Counter(r["Location"] or "(blank)" for r in rows)))
    print("by section:", dict(Counter(r["Section"] for r in rows)))
    keys = Counter((r["Location"], r["Room / Area"], r["Item"]) for r in rows)
    dupes = {key: n for key, n in keys.items() if n > 1}
    if dupes:
        print("duplicate (location, room, item) keys:", dupes)
    if args.dry_run:
        for r in rows:
            print(f"  {r['Location']:12s} | {r['Room / Area'][:28]:28s} | {r['Section']:22s} | {r['Item'][:44]}")
        return

    config = supabase_store.settings()
    if not config:
        sys.exit("Supabase is not configured; check .env.")
    store = WorkspaceStore(supabase_store.SupabaseBackend(config))
    with store.lock:
        wb, revision = store.snapshot()
        if not any(p["Project ID"] == project for p in records(wb, "Projects")):
            sys.exit(f"No project {project} in the workspace.")
        spec_template.register_locations(wb, ["Entire Home"])
        sheet = ensure_selection_columns(wb)
        existing = sorted((r["_row"] for r in records(wb, "Selections")
                           if r["Project ID"] == project), reverse=True)
        for row in existing:
            delete_record(wb, "Selections", row)
        for values in rows:
            put(sheet, next_row(sheet), values)
        store.save(wb, revision,
                   f"rebuild {project} from 901 Paris Drive spec sheet "
                   f"(-{len(existing)}/+{len(rows)} line items)")
    print(f"saved: {len(existing)} removed, {len(rows)} written; a backup version was kept.")


if __name__ == "__main__":
    main()
