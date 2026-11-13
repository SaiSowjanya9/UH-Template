"""One-shot: rebuild the 901 Paris Drive project in the portal from
901_Paris_Drive_Spec_Sheet.xlsx (Room Specs + Bulk Items tabs).

The sheet's "Whole House" scope is the portal's "Entire Home" location, which is
registered on the Lists sheet if missing. The sheet's Category names land on
Section verbatim, so the portal and the exports read the sheet's own categories;
any name not already on Lists is appended there. One name cannot be carried over
directly: "Garage" is a location in the workspace, and a Section with a location's
name is migrated onto Location on save, so those rows file under "Garage (bulk)".
Room names pass through ROOM_MAP, which keeps the consolidations the project
settled on (Mud Room 1/2 under Laundry / Mud Room, the kitchen zones under Butler
Pantry / Prep Kitchen, and so on) so a re-import cannot bring duplicate room
headings back; a merged item that would read identically to a sibling keeps its
zone in parentheses. Every existing line item on the project is replaced: the
Reference Schedule tab is the old data and is not re-imported.

Reads .env for Supabase, like the app, and writes through WorkspaceStore, so the
save is validated, backed up as a version, and atomic. --dry-run prints the
mapping and the row count without touching the workspace.
"""
import argparse
import re
import sys
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook

import common
import spec_template
import supabase_store
from common import clean
from workspace_store import (LOCATIONS, OPTIONAL_SELECTION_FIELDS, SELECTION_FIELDS,
                             WorkspaceStore, delete_record, ensure_selection_columns,
                             next_row, put, records, validate_values)

DEFAULT_PROJECT = "UH-001"  # Velora Plan, 901 Paris Drive, Prosper TX
SOURCE = Path.home() / "Downloads" / "901_Paris_Drive_Spec_Sheet.xlsx"

# The sheet's Floor / Scope column is the portal's Location; "whole house" there
# means "entire home" here. Floors pass through under their own names.
def location(floor):
    return {"Whole House": "Entire Home"}.get(floor, floor)


# A room value of "Whole House" names the same scope: the whole home, and the
# zone names the sheet splits one room into file under the room they belong to -
# the consolidations the project settled on, so a re-import cannot bring the
# duplicate headings back. Items whose zone still matters keep it in their name
# (Cabinetry (Island), Wood floor (Pantry)); closet and bath sub-rooms stay
# separate because they name different spaces.
ROOM_MAP = {
    "Whole House": "Entire Home",
    "Utility - Upstairs": "Utility Upstairs",
    "Bed 1 Up with Closet": "Bedroom 1 Upstairs",
    "Bed 2 Up with Closet": "Bedroom 2 Upstairs",
    "Bed 3 Up with Closet": "Bedroom 3 Upstairs",
    "Kitchen - Most Cabinets": "Kitchen",
    "Powder": "Powder Bath",
    "Dry Bar by Family": "Dry Bar",
    "Prep Kitchen": "Butler Pantry / Prep Kitchen",
    "Butler's Pantry": "Butler Pantry / Prep Kitchen",
    "Pantry": "Butler Pantry / Prep Kitchen",
    "Coffee Bar": "Butler Pantry / Prep Kitchen",
    "Island": "Butler Pantry / Prep Kitchen",
    "Mud Room": "Laundry / Mud Room",
    "Mud Room 1": "Laundry / Mud Room",
    "Mud Room 2": "Laundry / Mud Room",
}


def room(value):
    return ROOM_MAP.get(value, value)


def section(category):
    """The sheet's Category, kept as the portal's Section unless it names a location.

    A Section called after a location would be read as a floor/zone on save and
    migrated onto Location with the category re-inferred, so those file under a
    " (bulk)" suffix that no location list can contain.
    """
    if category in LOCATIONS:
        return f"{category} (bulk)"
    return category or "Other"


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
    rows = []

    def add(loc, src_rm, cat, item, mfr="", model="", finish="", size="", qty="", notes=""):
        if not clean(item):
            return
        values = validate_values({
            "Project ID": project, "Section": section(clean(cat)), "Location": loc,
            "Room / Area": room(src_rm), "Item": item, "Manufacturer": clean(mfr),
            "Model #": clean(model), "Finish / Color": clean(finish),
            "Size / Configuration": clean(size), "Qty": quantity(qty),
            "Client Notes": clean(notes), "Client Status": "Proposed",
            "Include in Lookbook": "Yes"},
            SELECTION_FIELDS + OPTIONAL_SELECTION_FIELDS)
        values["_src_room"] = src_rm
        rows.append(values)

    specs = wb["Room Specs"]
    for cells in specs.iter_rows(min_row=4, values_only=True):
        floor, rm, cat, item, mfr, model, finish, size, qty, notes = (list(cells) + [None] * 10)[:10]
        if not any(clean(c) for c in (floor, rm, cat, item)):
            continue
        add(location(clean(floor)), clean(rm), clean(cat), clean(item),
            mfr, model, finish, size, qty, notes)

    bulk = wb["Bulk Items"]
    for cells in bulk.iter_rows(min_row=5, values_only=True):
        scope, cat, item, notes = (list(cells) + [None] * 4)[:4]
        if not any(clean(c) for c in (scope, cat, item, notes)):
            continue
        add(location(clean(scope)), "Entire Home", clean(cat), clean(item), notes=notes)

    # a merge that lands two like-named items in one room keeps the zone in the item
    counts = Counter((r["Room / Area"], r["Item"]) for r in rows)
    for values in rows:
        src_rm = values.pop("_src_room")
        if counts[(values["Room / Area"], values["Item"])] > 1 and src_rm != values["Room / Area"]:
            values["Item"] = f"{values['Item']} ({src_rm})"

    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", default=str(SOURCE), help="the spec sheet to import")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="the portal project to rebuild")
    parser.add_argument("--dry-run", action="store_true", help="print the mapping and stop")
    args = parser.parse_args()
    project = args.project

    rows = read_rows(Path(args.file), project)
    print(f"rows to write: {len(rows)}")
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
        # the sheet's category names go on the Sections list, after the entries already there
        lists = wb["Lists"]
        section_col = next((c.column for c in lists[1]
                            if clean(c.value) == "Sections (lookbook order)"), 1)
        have = {clean(lists.cell(r, section_col).value) for r in range(2, lists.max_row + 1)}
        for name in dict.fromkeys(r["Section"] for r in rows):
            if name and name not in have:
                lists.cell(lists.max_row + 1, section_col, name)
                have.add(name)
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
