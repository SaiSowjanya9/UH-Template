"""One-shot: strip estimating unit abbreviations (EA, sq ft, LF, gal, SQ, lot)
from Client Notes on the live site, keeping the descriptive parts of each note.

Dry run:   python clean_unit_notes.py
Apply:     python clean_unit_notes.py --apply

Credentials are read from .env (Email:/Password: lines)."""
import os
import re
import sys

import requests

BASE = "https://uh-selections.onrender.com"

UNITS = {"ea", "each", "sq ft", "sqft", "sf", "sq", "lf", "ln ft", "lin ft",
         "gal", "gallon", "gallons", "lot"}
SEPARATORS = re.compile(r"\s*[·•]\s*")


def credentials():
    email = password = None
    for line in open(".env", encoding="utf-8"):
        key, _, value = line.partition(":")
        if key.strip().lower() == "email":
            email = value.strip()
        elif key.strip().lower() == "password":
            password = value.strip()
    if not email or not password:
        sys.exit("Email:/Password: not found in .env")
    return email, password


def clean_note(note):
    """Drop segments that are only a unit abbreviation; keep everything else."""
    kept = [piece for piece in (p.strip().rstrip(".") for p in SEPARATORS.split(note))
            if piece and piece.lower() not in UNITS]
    return " · ".join(kept)


def login(s, email, password):
    page = s.get(BASE + "/login", timeout=60)
    token = re.search('name=.token. value=.([^"]+)', page.text).group(1)
    r = s.post(BASE + "/login", data={"token": token, "email": email,
                                      "password": password, "next": "/"},
               timeout=60, allow_redirects=False)
    assert r.status_code == 302, f"login failed: {r.status_code}"
    home = s.get(BASE + "/", timeout=60).text
    return re.search('name="uh-token" content="([^"]+)', home).group(1)


def main():
    email, password = credentials()
    s = requests.Session()
    csrf = login(s, email, password)

    state = s.get(BASE + "/api/state", timeout=60).json()
    changes = {}
    for row in state["selections"]:
        note = row.get("Client Notes") or ""
        cleaned = clean_note(note)
        if cleaned != note:
            changes[str(row["_row"])] = {"Client Notes": cleaned}
            print(f'row {row["_row"]:>4} [{row.get("Project ID")}] {row.get("Item")}')
            print(f'    - {note!r}')
            print(f'    + {cleaned!r}')

    print(f"\n{len(changes)} rows to update")
    if not changes or "--apply" not in sys.argv:
        print("dry run only — re-run with --apply to save")
        return

    r = s.post(BASE + "/api/selections", headers={"X-UH-Token": csrf},
               json={"revision": state["revision"], "batch": changes}, timeout=120)
    print("save:", r.status_code, r.json())


if __name__ == "__main__":
    main()
