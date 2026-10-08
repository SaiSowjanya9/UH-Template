"""One-shot: import the 901 Paris Dr spec sheet (Home Spec Builder.pdf) into UH-103
on the live site via the app's own CSV import and project-save endpoints.
Set UH_IMPORT_PASSWORD in the environment before running."""
import csv
import io
import os
import re
import sys

import requests

BASE = "https://uh-selections.onrender.com"
EMAIL = "saisowjanya218@gmail.com"
PASSWORD = os.environ["UH_IMPORT_PASSWORD"]
PROJECT = "UH-103"

# (Section, Room / Area, Item, Manufacturer, Model #, Finish / Color, Qty, Client Notes)
R = []

def row(section, room, item, mfr="", model="", finish="", qty="", notes=""):
    R.append({"Section": section, "Room / Area": room, "Item": item, "Manufacturer": mfr,
              "Model #": model, "Finish / Color": finish, "Qty": qty,
              "Client Notes": notes, "Include in Lookbook": "Yes"})

# ---------------- Whole-house specs (room = Whole House) ----------------
W = "Whole House"
row("Bulk Items", W, "Foundation", finish="Slab", qty="7571", notes="Slab / pier & beam · sq ft")
row("Bulk Items", W, "Framing", finish="2x6 exterior walls", notes="Lumber, sheathing, trusses")
row("Exterior", W, "Roofing", model="Class 4 shingles", finish="Driftwood", notes="Shingles / metal, underlayment · SQ")
row("Exterior", W, "Exterior siding", model="Stucco", finish="Smooth finish", notes="Brick / stone / siding · supplier: Sherwin-Williams")
row("Exterior", W, "Soffit, fascia & gutters", notes="LF")
row("Paint", W, "Exterior paint", mfr="Sherwin-Williams", model="Oyster White", finish="Oyster White SW 7637", notes="Siding / trim paint · gal")
row("Windows & Doors", W, "Window package", mfr="LUVINDOW", model="Custom", notes="Supplier: Sherwin-Williams · EA")
row("Windows & Doors", W, "Exterior doors", mfr="LUVINDOW", model="RAL 9005", notes="Front door, patio door · supplier: Sherwin-Williams · EA")
row("Garage", W, "Garage door + opener", model="18' x 8' / 10' x 8'", qty="2", notes="Door + opener · supplier: Sherwin-Williams · EA")
row("Bulk Items", W, "Insulation", finish="Walls R-21, attic R-38", notes="Batt insulation")
row("Bulk Items", W, "HVAC system", notes="System, tonnage, SEER · EA")
row("Bulk Items", W, "Thermostat", notes="EA")
row("Bulk Items", W, "Water heater", finish="Tankless", notes="EA")
row("Bulk Items", W, "Supply / drain piping")
row("Bulk Items", W, "Electrical panel", notes="Panel, amps · EA")
row("Bulk Items", W, "Smoke / CO detectors", notes="EA")
row("Bulk Items", W, "Low voltage, data & security")
row("Bulk Items", W, "Drywall", finish="Level 3", notes="Board, texture · sq ft")
row("Paint", W, "Interior paint", notes="Walls, ceilings, trim · gal")
row("Bulk Items", W, "Interior trim", notes="Base, casing, crown · LF")
row("Windows & Doors", W, "Interior doors", model="Lincoln Park 1-panel", notes="Door style, hardware · EA")
row("Outdoor Living", W, "Landscaping", notes="Sod, beds, trees, irrigation")
row("Outdoor Living", W, "Flatwork", notes="Driveway, walks, patio · sq ft")
row("Outdoor Living", W, "Fencing", notes="LF")

# ---------------- Entry / Foyer (8) ----------------
row("1st Floor", "Foyer", "Flooring", finish="Level 3", notes="sq ft")
row("1st Floor", "Foyer", "Wall paint", notes="gal")
row("1st Floor", "Foyer", "Trim", notes="Base / casing · LF")
row("1st Floor", "Foyer", "Lighting", finish="Level 3", notes="Pendant / chandelier · EA")
row("1st Floor", "Foyer", "Electrical", notes="Switches, outlets, doorbell · EA")
row("1st Floor", "Foyer", "Door hardware", notes="Front entry set · EA")
row("1st Floor", "Foyer", "Coat closet", notes="Shelf & rod · LF")
row("1st Floor", "Foyer", "Stairs", notes="Trim · lot")

# ---------------- Living / Family Room (8) ----------------
row("1st Floor", "Family Room", "Flooring", finish="Level 3", notes="sq ft")
row("1st Floor", "Family Room", "Wall paint", notes="gal")
row("1st Floor", "Family Room", "Trim", notes="Base / casing / crown · LF")
row("1st Floor", "Family Room", "Lighting", notes="Recessed cans · EA")
row("1st Floor", "Family Room", "Ceiling fan", notes="EA")
row("1st Floor", "Family Room", "Electrical", notes="Switches, outlets, TV / data · EA")
row("1st Floor", "Family Room", "Fireplace", model="60 in", notes="Unit, surround, mantel · EA")
row("1st Floor", "Family Room", "Ceiling", notes="Beams / treatment")

# ---------------- Kitchen (21) ----------------
row("Kitchen", "Kitchen", "Flooring", finish="Level 3", notes="sq ft · FS Builders")
row("Kitchen", "Kitchen", "Cabinets", mfr="Kentmore", model="Shaker", finish="Summer W", notes="Maple · LF · Kentmore")
row("Kitchen", "Kitchen", "Cabinet hardware", notes="Pulls / knobs · EA")
row("Kitchen", "Kitchen", "Countertops", finish="Level 3", notes="sq ft · FS Builders")
row("Kitchen", "Kitchen", "Backsplash", finish="Level 3", notes="Tile · sq ft · FS Builders")
row("Kitchen", "Kitchen", "Kitchen sink", mfr="Kohler", model="6489-0", finish="Whitehaven White", qty="1", notes="Enameled cast iron · EA · Facets")
row("Kitchen", "Kitchen", "Kitchen faucet", mfr="Kohler", model="28358-2MB", finish="Vibrant Brushed Moderne Brass", qty="1", notes="EA · Facets")
row("Kitchen", "Kitchen", "Disposal", mfr="ISE", model="80926A", qty="1", notes="EA")
row("Kitchen", "Kitchen", "Range / cooktop", mfr="ILVE", model="UPD40FNMPA", qty="1", notes="Professional range top · EA")
row("Kitchen", "Kitchen", "Microwave", mfr="Wolf", model="MDD3050PE/S/P", finish="Black glass with stainless", qty="1", notes="EA")
row("Kitchen", "Kitchen", "Vent hood", mfr="Vent-A-Hood", model="BH240SLD SS", finish="Stainless steel", qty="1", notes="EA")
row("Kitchen", "Kitchen", "Dishwasher", mfr="Cove", model="DW2451", finish="Panel ready 24 in", qty="1", notes="EA")
row("Kitchen", "Kitchen", "Refrigerator", mfr="Sub-Zero", model="DEC3050", finish="Panel ready 30 in", qty="1", notes="EA")
row("Kitchen", "Kitchen", "Lighting", finish="Level 3", notes="Pendants, cans, under-cabinet · EA")
row("Kitchen", "Kitchen", "Electrical", notes="Outlets (GFCI), switches · EA")
row("Kitchen", "Kitchen", "Wall paint", notes="gal")
row("Kitchen", "Kitchen", "Pantry shelving", notes="LF")
row("Kitchen", "Kitchen", "Freezer", mfr="Sub-Zero", model="DEC3050FI/L", finish="Panel ready 30 in", qty="1", notes="EA")
row("Kitchen", "Kitchen", "Pot filler", mfr="Kohler", model="28359-2MB", finish="Vibrant Brushed Moderne Brass", qty="1", notes="Edalyn by Studio · EA")
row("Kitchen", "Kitchen", "Disposal air switch", mfr="Kohler", model="K-35723-2MO", finish="Modern brushed", qty="1", notes="Garbage disposal air switch · EA")
row("Kitchen", "Kitchen", "Disposal stopper flange", mfr="Kohler", model="K-11352-2", finish="Vibrant brushed", qty="1", notes="Stopper flange w/ stopper · EA")

# ---------------- Dining Room (5) ----------------
row("1st Floor", "Dining Room", "Flooring", finish="Level 3", notes="sq ft")
row("1st Floor", "Dining Room", "Wall paint", notes="gal")
row("1st Floor", "Dining Room", "Trim", notes="Base / casing / crown · LF")
row("1st Floor", "Dining Room", "Lighting", finish="Level 3", notes="Chandelier · EA")
row("1st Floor", "Dining Room", "Electrical", notes="Switches, outlets · EA")

# ---------------- Primary Bedroom (7) ----------------
row("1st Floor", "Primary Bedroom", "Flooring", finish="Level 3", notes="sq ft")
row("1st Floor", "Primary Bedroom", "Wall paint", notes="gal")
row("1st Floor", "Primary Bedroom", "Trim", notes="Base / casing · LF")
row("1st Floor", "Primary Bedroom", "Ceiling fan", notes="EA")
row("1st Floor", "Primary Bedroom", "Lighting", finish="Level 3", notes="EA")
row("1st Floor", "Primary Bedroom", "Electrical", notes="Switches, outlets, TV / data · EA")
row("1st Floor", "Primary Bedroom", "Closet", notes="Shelving system · LF")

# ---------------- Primary Bathroom (16) ----------------
row("Bathrooms", "Primary Bath", "Flooring", notes="Tile · sq ft")
row("Bathrooms", "Primary Bath", "Vanity", notes="Cabinet · EA")
row("Bathrooms", "Primary Bath", "Countertop", notes="sq ft")
row("Bathrooms", "Primary Bath", "Sinks", notes="EA")
row("Bathrooms", "Primary Bath", "Faucets", notes="EA")
row("Bathrooms", "Primary Bath", "Toilet", notes="EA")
row("Bathrooms", "Primary Bath", "Shower", notes="Wall tile, floor tile, pan · sq ft")
row("Bathrooms", "Primary Bath", "Shower valve", notes="Valve, trim, head · EA")
row("Bathrooms", "Primary Bath", "Shower glass", notes="Door / panel · EA")
row("Bathrooms", "Primary Bath", "Tub", notes="Freestanding / drop-in + filler · EA")
row("Bathrooms", "Primary Bath", "Mirrors", notes="EA")
row("Bathrooms", "Primary Bath", "Lighting", notes="Vanity lights, cans · EA")
row("Bathrooms", "Primary Bath", "Exhaust fan", notes="EA")
row("Bathrooms", "Primary Bath", "Accessories", notes="Towel bars, rings, TP holder · EA")
row("Bathrooms", "Primary Bath", "Electrical", notes="GFCI outlets, switches · EA")
row("Bathrooms", "Primary Bath", "Wall paint", notes="gal")

# ---------------- Bedroom 2 (6) ----------------
row("2nd Floor", "Bedroom 2", "Flooring", notes="sq ft")
row("2nd Floor", "Bedroom 2", "Wall paint", notes="gal")
row("2nd Floor", "Bedroom 2", "Trim", notes="Base / casing · LF")
row("2nd Floor", "Bedroom 2", "Ceiling fan", notes="EA")
row("2nd Floor", "Bedroom 2", "Electrical", notes="Switches, outlets · EA")
row("2nd Floor", "Bedroom 2", "Closet", notes="Shelf & rod · LF")

# ---------------- Bathroom 2 (14) ----------------
row("Bathrooms", "Bathroom 2", "Flooring", notes="Tile · sq ft")
row("Bathrooms", "Bathroom 2", "Vanity", notes="Cabinet · EA")
row("Bathrooms", "Bathroom 2", "Countertop", notes="sq ft")
row("Bathrooms", "Bathroom 2", "Sink", notes="EA")
row("Bathrooms", "Bathroom 2", "Faucet", notes="EA")
row("Bathrooms", "Bathroom 2", "Toilet", notes="EA")
row("Bathrooms", "Bathroom 2", "Tub / shower", notes="Unit or tile surround")
row("Bathrooms", "Bathroom 2", "Shower valve", notes="Valve, trim, head · EA")
row("Bathrooms", "Bathroom 2", "Shower rod / glass", notes="EA")
row("Bathrooms", "Bathroom 2", "Mirror", notes="EA")
row("Bathrooms", "Bathroom 2", "Lighting", notes="Vanity light · EA")
row("Bathrooms", "Bathroom 2", "Exhaust fan", notes="EA")
row("Bathrooms", "Bathroom 2", "Accessories", notes="Towel bar, TP holder · EA")
row("Bathrooms", "Bathroom 2", "Wall paint", notes="gal")

# ---------------- Powder Room (9) ----------------
row("Bathrooms", "Powder Room", "Flooring", notes="sq ft")
row("Bathrooms", "Powder Room", "Vanity / pedestal sink", notes="EA")
row("Bathrooms", "Powder Room", "Faucet", notes="EA")
row("Bathrooms", "Powder Room", "Toilet", notes="EA")
row("Bathrooms", "Powder Room", "Mirror", notes="EA")
row("Bathrooms", "Powder Room", "Lighting", notes="EA")
row("Bathrooms", "Powder Room", "Exhaust fan", notes="EA")
row("Bathrooms", "Powder Room", "Accessories", notes="Towel ring, TP holder · EA")
row("Bathrooms", "Powder Room", "Wall paint", notes="gal")

# ---------------- Laundry / Utility (9) ----------------
row("1st Floor", "Laundry Room", "Flooring", notes="sq ft")
row("1st Floor", "Laundry Room", "Cabinets", notes="Upper / base · LF")
row("1st Floor", "Laundry Room", "Countertop", notes="sq ft")
row("1st Floor", "Laundry Room", "Utility sink + faucet", notes="EA")
row("1st Floor", "Laundry Room", "Washer box / dryer vent", notes="EA")
row("1st Floor", "Laundry Room", "Electrical", notes="240V dryer, GFCI outlets · EA")
row("1st Floor", "Laundry Room", "Lighting", notes="EA")
row("1st Floor", "Laundry Room", "Shelving", notes="LF")
row("1st Floor", "Laundry Room", "Wall paint", notes="gal")

# ---------------- Garage (8) ----------------
row("Garage", "Garage", "Floor", notes="Sealed concrete / epoxy · sq ft")
row("Garage", "Garage", "Drywall", notes="Taped / fire-rated · sq ft")
row("Garage", "Garage", "Wall paint", notes="gal")
row("Garage", "Garage", "Entry door to house", notes="Fire-rated door, hardware · EA")
row("Garage", "Garage", "Lighting", notes="EA")
row("Garage", "Garage", "Electrical", notes="Outlets, EV charger prep · EA")
row("Garage", "Garage", "Hose bib", notes="EA")
row("Garage", "Garage", "Storage", notes="Shelving / attic access")

# ---------------- Butler Pantry (13) ----------------
row("Kitchen", "Butler Pantry", "Flooring", finish="Level 3", notes="sq ft")
row("Kitchen", "Butler Pantry", "Wall paint", notes="gal")
row("Kitchen", "Butler Pantry", "Trim", notes="LF")
row("Kitchen", "Butler Pantry", "Lighting", finish="Level 3", notes="EA")
row("Kitchen", "Butler Pantry", "Electrical", notes="EA")
row("Kitchen", "Butler Pantry", "Range / cooktop", mfr="Wolf", model="DF36650/S/P", finish="Stainless steel", qty="1", notes="EA")
row("Kitchen", "Butler Pantry", "Vent hood", mfr="Vent-A-Hood", model="BH240SLD SS", finish="Stainless steel", qty="1", notes="EA")
row("Kitchen", "Butler Pantry", "Dishwasher", mfr="Cove", model="DW2451", finish="Panel ready 24 in", qty="1", notes="EA")
row("Kitchen", "Butler Pantry", "Refrigerator", mfr="Jenn-Air", model="JFFCC72EHL", finish="Stainless steel", qty="1", notes="EA")
row("Kitchen", "Butler Pantry", "Kitchen sink", mfr="Kohler", model="5832-5U-0", finish="Bakersfield White", qty="1", notes="Single basin undermount enameled cast iron · EA")
row("Kitchen", "Butler Pantry", "Kitchen faucet", mfr="Kohler", model="22972-2MB", finish="Vibrant Brushed Moderne Brass", qty="1", notes="Deck-mount single lever · EA")
row("Kitchen", "Butler Pantry", "Disposal", mfr="ISE", model="80926A", qty="1", notes="Disposer with cord · EA")
row("Kitchen", "Butler Pantry", "Disposal stopper flange", mfr="Kohler", model="K-11352-2I", finish="Vibrant Brushed Moderne Brass", qty="1", notes="EA")

FIELDS = ["Section", "Room / Area", "Item", "Manufacturer", "Model #", "Finish / Color",
          "Qty", "Client Notes", "Include in Lookbook"]


def login(s):
    page = s.get(BASE + "/login", timeout=60)
    token = re.search('name=.token. value=.([^"]+)', page.text).group(1)
    r = s.post(BASE + "/login", data={"token": token, "email": EMAIL, "password": PASSWORD, "next": "/"},
               timeout=60, allow_redirects=False)
    assert r.status_code == 302, f"login failed: {r.status_code}"
    home = s.get(BASE + "/", timeout=60).text
    return re.search('name="uh-token" content="([^"]+)', home).group(1)


def main():
    print(f"rows to import: {len(R)}")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\r\n")
    w.writeheader()
    w.writerows(R)
    payload = buf.getvalue().encode("utf-8-sig")

    s = requests.Session()
    csrf = login(s)
    headers = {"X-UH-Token": csrf}
    rev = s.get(BASE + "/api/state", timeout=60).json()["revision"]

    r = s.post(BASE + "/api/selections/csv", headers=headers,
               data={"project": PROJECT, "revision": rev},
               files={"file": ("uh103_spec.csv", payload, "text/csv")}, timeout=120)
    print("import:", r.status_code, r.json())

    # refresh revision, then update the project record itself
    rev = s.get(BASE + "/api/state", timeout=60).json()["revision"]
    r = s.post(BASE + "/api/projects", headers=headers, json={"revision": rev, "values": {
        "Project ID": PROJECT, "Project Name": "Velora", "Client Name": "Ravi Y",
        "Address": "901 Paris Drive, Prosper, TX 75078", "Plan / Elevation": "Velora / Elev. A",
        "Designer": "", "Presentation Date": "", "Cover Image": ""},
        "custom_fields": [
            {"name": "Community", "value": "Park Place"},
            {"name": "Lot / Block", "value": "4/G"},
            {"name": "Heated sq ft", "value": "5,870"},
            {"name": "Total sq ft (under roof)", "value": "7,571"},
            {"name": "Bedrooms / Bathrooms", "value": "5 / 5"},
            {"name": "Stories", "value": "2"},
            {"name": "Garage", "value": "2 bays"},
            {"name": "Foundation", "value": "Slab"},
            {"name": "Superintendent", "value": "Subbu"},
            {"name": "Start date", "value": "04/05/2026"},
        ]}, timeout=60)
    print("project update:", r.status_code, r.json())


if __name__ == "__main__":
    main()
