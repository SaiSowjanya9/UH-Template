"""
Build a client-ready PDF selections lookbook from the tracker workbook.

Usage:
    python build_lookbook.py --project UH-101        # one project
    python build_lookbook.py --all                   # every project on the Projects sheet
    python build_lookbook.py --all --verified-only   # only rows marked 'Verified'
    python build_lookbook.py --project UH-101 --draft  # internal review copy (watermark + status tags)

Output: output/<ProjectID>_<Project Name>_Lookbook.pdf
Brand colors, fonts, logo and contact line live in lookbook_config.json.
"""
import argparse
import datetime as dt
import hashlib
import io
import json
import math
import re
from collections import OrderedDict
from pathlib import Path

import requests
from PIL import Image
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from common import BASE_DIR, Sheet, clean, open_workbook, section_order
from remote import fetch
from branding import load_logo

PAGE_W, PAGE_H = landscape(letter)  # 792 x 612 pt
M = 42                               # page margin
CACHE = BASE_DIR / "image_cache"
OUT = BASE_DIR / "output"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36"}


# --------------------------------------------------------------------------
# Config / fonts
# --------------------------------------------------------------------------
CFG = json.loads((BASE_DIR / "lookbook_config.json").read_text())
C = {k: HexColor(v) for k, v in CFG["colors"].items()}
FONT_H, FONT_B, FONT_BB = "Times-Roman", "Helvetica", "Helvetica-Bold"
for key, attr, name in [("heading", "FONT_H", "BrandHeading"), ("body", "FONT_B", "BrandBody"),
                        ("body_bold", "FONT_BB", "BrandBodyBold")]:
    path = CFG.get("fonts", {}).get(key)
    if path and (BASE_DIR / path).exists():
        pdfmetrics.registerFont(TTFont(name, str(BASE_DIR / path)))
        globals()[attr] = name

PER_ROW = int(CFG.get("cards_per_row", 3))
ROWS = int(CFG.get("rows_per_page", 2))
PER_PAGE = PER_ROW * ROWS


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def fmt_date(v):
    if isinstance(v, (dt.date, dt.datetime)):
        return f"{v:%B} {v.day}, {v.year}"
    s = clean(v)
    try:
        d = dt.date.fromisoformat(s[:10])
        return f"{d:%B} {d.day}, {d.year}"
    except ValueError:
        return s


def load_data(verified_only):
    wb = open_workbook(data_only=True)
    order = section_order(wb)
    projects = OrderedDict()
    for _, d in Sheet(wb["Projects"]).rows():
        pid = clean(d.get("Project ID"))
        if pid:
            projects[pid] = {k: clean(v) if not isinstance(v, (dt.date, dt.datetime)) else v
                             for k, v in d.items()}
    items = {}
    for r, d in Sheet(wb["Selections"]).rows():
        pid = clean(d.get("Project ID"))
        if not pid or clean(d.get("Include in Lookbook")).lower() == "no":
            continue
        if not (clean(d.get("Item")) or clean(d.get("Manufacturer"))):
            continue
        status = clean(d.get("Lookup Status")) or "Not run"
        if verified_only and status != "Verified":
            continue
        rec = {k: clean(v) for k, v in d.items()}
        rec["Lookup Status"] = status
        rec["_row"] = r
        items.setdefault(pid, []).append(rec)

    def sort_key(x):
        sec = x.get("Section", "")
        return (order.index(sec) if sec in order else len(order), sec, x["_row"])

    for pid in items:
        items[pid].sort(key=sort_key)
    return projects, items


def group_sections(rows):
    groups = OrderedDict()
    for x in rows:
        groups.setdefault(x.get("Section") or "Other", []).append(x)
    return groups


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------
def get_image(src):
    """Return an ImageReader for a URL or local path, or None."""
    src = clean(src)
    if not src:
        return None
    try:
        if src.lower().startswith("http"):
            CACHE.mkdir(exist_ok=True)
            f = CACHE / (hashlib.sha1(src.encode()).hexdigest() + ".jpg")
            if not f.exists():
                content, _, _ = fetch(src, limit=12_000_000)
                img = Image.open(io.BytesIO(content))
                _save_rgb(img, f)
            return ImageReader(str(f))
        p = Path(src)
        p = p if p.is_absolute() else BASE_DIR / p
        if p.exists():
            CACHE.mkdir(exist_ok=True)
            f = CACHE / (hashlib.sha1(str(p).encode()).hexdigest() + ".jpg")
            if not f.exists() or f.stat().st_mtime < p.stat().st_mtime:
                _save_rgb(Image.open(p), f)
            return ImageReader(str(f))
    except Exception as e:
        print(f"   ! image skipped ({src[:60]}...): {e}")
    return None


def _save_rgb(img, dest):
    img.load()
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    img.thumbnail((1400, 1400))
    img.save(dest, "JPEG", quality=88)


# --------------------------------------------------------------------------
# Drawing helpers
# --------------------------------------------------------------------------
def lines(text, font, size, width, max_lines):
    out = simpleSplit(text or "", font, size, width)
    if len(out) > max_lines:
        out = out[:max_lines]
        last = out[-1]
        while last and pdfmetrics.stringWidth(last + "...", font, size) > width:
            last = last[:-1]
        out[-1] = last.rstrip() + "..."
    return out


def spaced(c, x, y, text, font, size, spacing=1.6, color=None, align="left"):
    """Letter-spaced caps label."""
    text = text.upper()
    w = pdfmetrics.stringWidth(text, font, size) + spacing * (len(text) - 1)
    if align == "right":
        x -= w
    elif align == "center":
        x -= w / 2
    t = c.beginText(x, y)
    t.setFont(font, size)
    t.setCharSpace(spacing)
    if color is not None:
        t.setFillColor(color)
    t.textOut(text)
    t.setCharSpace(0)  # text state persists in PDF - reset so later text isn't spaced
    c.drawText(t)
    return w


def footer(c, project, page_no, dark=False):
    col = C["paper"] if dark else C["muted"]
    c.setStrokeColor(C["rule"])
    c.setLineWidth(0.5)
    if not dark:
        c.line(M, 30, PAGE_W - M, 30)
    has_mark = logo(c, M, 14, 14, mark=True, max_width=17)
    spaced(c, M + 23 if has_mark else M, 18, CFG["company_name"], FONT_BB, 7, 1.4, col)
    spaced(c, PAGE_W / 2, 18, project.get("Project Name", ""), FONT_B, 7, 1.2, col, "center")
    spaced(c, PAGE_W - M, 18, f"{page_no:02d}", FONT_BB, 7, 1.2, col, "right")


def draft_mark(c):
    c.saveState()
    c.setFillColor(C["accent"])
    c.setFillAlpha(0.07)
    c.setFont(FONT_BB, 120)
    c.translate(PAGE_W / 2, PAGE_H / 2)
    c.rotate(28)
    c.drawCentredString(0, -40, "DRAFT")
    c.restoreState()


def logo(c, x, y, h, mark=False, max_width=None, centered=False):
    image = load_logo(CFG.get("logo_mark_path" if mark else "logo_path"))
    if image is None:
        return False
    iw, ih = image.size
    scale = min(h / ih, max_width / iw) if max_width else h / ih
    w, h = iw * scale, ih * scale
    c.drawImage(ImageReader(image), x - w / 2 if centered else x, y, w, h, mask="auto")
    return True


def image_box(c, img, x, y, w, h, fallback_label=""):
    c.setFillColor(white)
    c.rect(x, y, w, h, stroke=0, fill=1)
    if img:
        pad = 8
        c.drawImage(img, x + pad, y + pad, w - 2 * pad, h - 2 * pad,
                    preserveAspectRatio=True, anchor="c")
    else:
        c.setFillColor(C["paper"])
        c.rect(x + 6, y + 6, w - 12, h - 12, stroke=0, fill=1)
        spaced(c, x + w / 2, y + h / 2 + 2, "Image coming soon", FONT_B, 7, 1.2, C["muted"], "center")
        if fallback_label:
            c.setFont(FONT_H, 11)
            c.setFillColor(C["text"])
            c.drawCentredString(x + w / 2, y + h / 2 - 14, fallback_label[:40])


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
def page_cover(c, p, sections, draft):
    split = PAGE_W * 0.46
    c.setFillColor(C["dark"])
    c.rect(0, 0, split, PAGE_H, stroke=0, fill=1)

    if not logo(c, M, PAGE_H - M - 34, 34, max_width=split - 2 * M):
        spaced(c, M, PAGE_H - M - 12, CFG["company_name"], FONT_BB, 11, 3, white)
    c.setStrokeColor(C["accent"])
    c.setLineWidth(1.2)
    c.line(M, PAGE_H - M - 48, M + 40, PAGE_H - M - 48)

    spaced(c, M, 414, CFG.get("tagline", "Selections Lookbook"), FONT_BB, 8.5, 2.4, C["accent"])
    c.setFillColor(white)
    y = 375
    for ln in lines(p.get("Project Name", ""), FONT_H, 38, split - 2 * M, 3):
        c.setFont(FONT_H, 38)
        c.drawString(M, y, ln)
        y -= 42
    y -= 6
    details = [("Prepared for", p.get("Client Name")), ("Address", p.get("Address")),
               ("Plan", p.get("Plan / Elevation")), ("Date", fmt_date(p.get("Presentation Date")))]
    for label, val in details:
        if not val:
            continue
        spaced(c, M, y, label, FONT_B, 6.5, 1.4, C["muted"])
        c.setFont(FONT_B, 10)
        c.setFillColor(white)
        for ln in lines(val, FONT_B, 10, split - 2 * M - 80, 2):
            c.drawString(M + 80, y, ln)
            y -= 13
        y -= 5

    # Right side: cover image or contents
    x0 = split
    img = get_image(p.get("Cover Image"))
    if img:
        c.saveState()
        path = c.beginPath()
        path.rect(x0, 0, PAGE_W - x0, PAGE_H)
        c.clipPath(path, stroke=0, fill=0)
        iw, ih = img.getSize()
        scale = max((PAGE_W - x0) / iw, PAGE_H / ih)
        w, h = iw * scale, ih * scale
        c.drawImage(img, x0 + (PAGE_W - x0 - w) / 2, (PAGE_H - h) / 2, w, h)
        c.restoreState()
    else:
        c.setFillColor(C["paper"])
        c.rect(x0, 0, PAGE_W - x0, PAGE_H, stroke=0, fill=1)
        if not logo(c, x0 + (PAGE_W - x0) / 2, PAGE_H / 2 + 10, 144, mark=True, max_width=180, centered=True):
            c.setFont(FONT_H, 160)
            c.setFillColor(C["rule"])
            initials = "".join(w[0] for w in CFG["company_name"].split()[:2]).upper()
            c.drawCentredString(x0 + (PAGE_W - x0) / 2, PAGE_H / 2 + 10, initials)
        spaced(c, x0 + (PAGE_W - x0) / 2, PAGE_H / 2 - 30,
               f"{len(sections)} categories  |  {sum(len(v) for v in sections.values())} selections",
               FONT_B, 8, 1.8, C["muted"], "center")
    if draft:
        draft_mark(c)
    spaced(c, M, 18, CFG["company_name"], FONT_BB, 7, 1.4, C["paper"])
    c.showPage()


def page_overview(c, p, sections, page_map, schedule_page, draft, section_offset=0, page_no=2, show_schedule=True):
    c.setFillColor(C["paper"])
    c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
    spaced(c, M, PAGE_H - M - 6, "Overview", FONT_BB, 8, 2.4, C["accent"])
    c.setFont(FONT_H, 30)
    c.setFillColor(C["text"])
    c.drawString(M, PAGE_H - M - 44, "What's inside")

    # contents
    y = PAGE_H - M - 96
    colx = M
    for i, (sec, rows) in enumerate(sections.items(), start=section_offset + 1):
        c.setFont(FONT_H, 13)
        c.setFillColor(C["accent"])
        c.drawString(colx, y, f"{i:02d}")
        c.setFillColor(C["text"])
        c.drawString(colx + 34, y, (lines(sec, FONT_H, 13, 170, 1) or [""])[0])
        c.setFont(FONT_B, 8)
        c.setFillColor(C["muted"])
        c.drawRightString(colx + 300, y, f"{len(rows)} item{'s' if len(rows) != 1 else ''}   |   p. {page_map[sec]:02d}")
        c.setStrokeColor(C["rule"])
        c.setLineWidth(0.5)
        c.line(colx, y - 9, colx + 300, y - 9)
        c.linkRect("", f"sec{i}", (colx, y - 8, colx + 300, y + 14), relative=0)
        y -= 32
    if show_schedule:
        c.setFont(FONT_H, 13)
        c.setFillColor(C["accent"])
        c.drawString(colx, y, "--")
        c.setFillColor(C["text"])
        c.drawString(colx + 34, y, "Selections schedule")
        c.setFont(FONT_B, 8)
        c.setFillColor(C["muted"])
        c.drawRightString(colx + 300, y, f"p. {schedule_page:02d}")
        c.linkRect("", "schedule", (colx, y - 8, colx + 300, y + 14), relative=0)

    # project card on the right
    bx, bw = PAGE_W - M - 250, 250
    by, bh = 90, PAGE_H - M - 96 - 90 + 16
    c.setFillColor(white)
    c.rect(bx, by, bw, bh, stroke=0, fill=1)
    ty = by + bh - 28
    spaced(c, bx + 22, ty, "Project details", FONT_BB, 7.5, 2, C["accent"])
    ty -= 26
    for label, key in [("Project", "Project Name"), ("Project ID", "Project ID"), ("Client", "Client Name"),
                       ("Address", "Address"), ("Plan / Elevation", "Plan / Elevation"),
                       ("Designer", "Designer"), ("Presented", "Presentation Date")]:
        val = fmt_date(p.get(key)) if key == "Presentation Date" else p.get(key, "")
        if not val:
            continue
        spaced(c, bx + 22, ty, label, FONT_B, 6.5, 1.3, C["muted"])
        ty -= 13
        c.setFont(FONT_B, 10)
        c.setFillColor(C["text"])
        for ln in lines(val, FONT_B, 10, bw - 44, 2):
            c.drawString(bx + 22, ty, ln)
            ty -= 13
        ty -= 9
    if draft:
        draft_mark(c)
    footer(c, p, page_no)
    c.showPage()


def card(c, x, y, w, h, it, draft):
    img_h = h * 0.40
    c.setFillColor(white)
    c.rect(x, y, w, h, stroke=0, fill=1)
    image_box(c, get_image(it.get("Image URL")), x, y + h - img_h, w, img_h, it.get("Manufacturer", ""))
    c.setStrokeColor(C["rule"])
    c.setLineWidth(0.5)
    c.rect(x, y, w, h, stroke=1, fill=0)

    if draft:
        st = it.get("Lookup Status", "")
        ok = st == "Verified"
        tw = pdfmetrics.stringWidth(st.upper(), FONT_BB, 6) + 12
        c.setFillColor(HexColor("#3F7D58") if ok else HexColor("#B4532A"))
        c.roundRect(x + w - tw - 6, y + h - 18, tw, 12, 3, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont(FONT_BB, 6)
        c.drawCentredString(x + w - tw / 2 - 6, y + h - 14, st.upper())

    tx, tw_ = x + 12, w - 24
    ty = y + h - img_h - 16
    label = "  |  ".join(v for v in [it.get("Room / Area"), it.get("Item")] if v)
    spaced(c, tx, ty, lines(label, FONT_B, 6.5, tw_ - 20, 1)[0] if label else "", FONT_B, 6.5, 1.1, C["accent"])
    ty -= 15
    title = it.get("Product Name") or it.get("Item") or ""
    c.setFillColor(C["text"])
    c.setFont(FONT_H, 12.5)
    for ln in lines(title, FONT_H, 12.5, tw_, 2):
        c.drawString(tx, ty, ln)
        ty -= 14
    ty -= 2
    c.setFont(FONT_B, 8)
    c.setFillColor(C["muted"])
    mm = "  |  ".join(v for v in [it.get("Manufacturer"), f"Model {it['Model #']}" if it.get("Model #") else ""] if v)
    for ln in lines(mm, FONT_B, 8, tw_, 1):
        c.drawString(tx, ty, ln)
        ty -= 11
    fq = "  |  ".join(v for v in [f"Finish: {it['Finish / Color']}" if it.get("Finish / Color") else "",
                                   f"Qty: {it['Qty']}" if it.get("Qty") else ""] if v)
    if fq:
        for ln in lines(fq, FONT_B, 8, tw_, 1):
            c.drawString(tx, ty, ln)
            ty -= 11
    if it.get("Client Notes"):
        c.setFont("Helvetica-Oblique" if FONT_B == "Helvetica" else FONT_B, 7.5)
        for ln in lines(it["Client Notes"], FONT_B, 7.5, tw_, 1):
            c.drawString(tx, ty, ln)

    url = it.get("Product URL", "")
    if url.startswith("http"):
        lab = "VIEW PRODUCT  >"
        spaced(c, tx, y + 11, lab, FONT_BB, 6.5, 1.3, C["dark"])
        lw = pdfmetrics.stringWidth(lab, FONT_BB, 6.5) + 1.3 * (len(lab) - 1)
        c.setStrokeColor(C["accent"])
        c.line(tx, y + 8, tx + lw, y + 8)
        c.linkURL(url, (x, y, x + w, y + h), relative=0)


def layout_rows(sections):
    """Break sections into card rows and assign rows to pages.
    Returns (pages, first_page_of_section) where pages = [[(sec_idx, sec, items, is_first_row)], ...]."""
    slots = []
    for idx, (sec, rows) in enumerate(sections.items(), start=1):
        for k in range(0, len(rows), PER_ROW):
            slots.append((idx, sec, rows[k:k + PER_ROW], k == 0))
    pages = [slots[i:i + ROWS] for i in range(0, len(slots), ROWS)]
    first = {}
    for pn, pg in enumerate(pages):
        for idx, sec, _, is_first in pg:
            if is_first:
                first[sec] = pn
    return pages, first


def selection_pages(c, p, pages, sections, start_page, draft):
    gap = 16
    head_h = 46           # room for a section heading above each card row
    top = PAGE_H - M + 6
    bottom = 44
    slot_h = (top - bottom) / ROWS
    cw = (PAGE_W - 2 * M - gap * (PER_ROW - 1)) / PER_ROW
    ch = slot_h - head_h - gap / 2
    for pn, pg in enumerate(pages):
        c.setFillColor(C["paper"])
        c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
        prev_sec = None
        for si, (idx, sec, items, is_first) in enumerate(pg):
            slot_top = top - si * slot_h
            if sec != prev_sec:
                hy = slot_top - 26
                if is_first:
                    c.bookmarkHorizontal(f"sec{idx}", 0, slot_top + 10)
                    c.addOutlineEntry(sec, f"sec{idx}", level=0)
                c.setFont(FONT_H, 13)
                c.setFillColor(C["accent"])
                c.drawString(M, hy, f"{idx:02d}")
                c.setFont(FONT_H, 24)
                c.setFillColor(C["text"])
                c.drawString(M + 30, hy, sec + ("" if is_first else "  (cont.)"))
                if is_first:
                    rooms = sorted({r.get("Room / Area") for r in sections[sec] if r.get("Room / Area")})
                    if rooms:
                        txt = lines(", ".join(rooms), FONT_B, 7, 300, 1)[0]
                        spaced(c, PAGE_W - M, hy + 2, txt, FONT_B, 7, 1.1, C["muted"], "right")
                c.setStrokeColor(C["rule"])
                c.setLineWidth(0.6)
                c.line(M, hy - 12, PAGE_W - M, hy - 12)
                card_top = slot_top - head_h
            else:
                card_top = slot_top - 8   # same section continues: no heading, small gap
            for i, it in enumerate(items):
                card(c, M + i * (cw + gap), card_top - ch, cw, ch, it, draft)
            prev_sec = sec
        if draft:
            draft_mark(c)
        footer(c, p, start_page + pn)
        c.showPage()


SCHED_COLS = [("#", 22), ("Section", 90), ("Room / Area", 90), ("Item", 100), ("Manufacturer", 92),
              ("Model #", 88), ("Finish / Color", 104), ("Qty", 30), ("Link", 92)]


def schedule_pages(c, p, rows, start_page, draft, dry=False):
    """Draws the full selections table; returns number of pages used."""
    total_w = sum(w for _, w in SCHED_COLS)
    scale = (PAGE_W - 2 * M) / total_w
    cols = [(h, w * scale) for h, w in SCHED_COLS]
    page, i = 0, 0
    while i < len(rows) or (page == 0 and not rows):
        if not dry:
            c.setFillColor(white)
            c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
            if page == 0:
                c.bookmarkPage("schedule")
                c.addOutlineEntry("Selections schedule", "schedule", level=0)
            spaced(c, M, PAGE_H - M - 6, "Reference", FONT_BB, 8, 2.4, C["accent"])
            c.setFont(FONT_H, 24)
            c.setFillColor(C["text"])
            c.drawString(M, PAGE_H - M - 38, "Selections schedule" + ("  (cont.)" if page else ""))
        y = PAGE_H - M - 66
        if not dry:
            c.setFillColor(C["dark"])
            c.rect(M, y - 6, PAGE_W - 2 * M, 18, stroke=0, fill=1)
            x = M + 6
            for h, w in cols:
                spaced(c, x, y, h, FONT_BB, 6.5, 0.8, white)
                x += w
        y -= 22
        while i < len(rows):
            it = rows[i]
            vals = [str(i + 1), it.get("Section", ""), it.get("Room / Area", ""), it.get("Item", ""),
                    it.get("Manufacturer", ""), it.get("Model #", ""), it.get("Finish / Color", ""),
                    it.get("Qty", ""), "View product" if it.get("Product URL", "").startswith("http") else "-"]
            wrapped = [lines(v, FONT_B, 8, w - 10, 2) or [""] for v, (_, w) in zip(vals, cols)]
            rh = 11 * max(len(wl) for wl in wrapped) + 8
            if y - rh < 44:
                break
            if not dry:
                if i % 2:
                    c.setFillColor(C["paper"])
                    c.rect(M, y - rh + 9, PAGE_W - 2 * M, rh, stroke=0, fill=1)
                x = M + 6
                for ci, (wl, (_, w)) in enumerate(zip(wrapped, cols)):
                    is_link = ci == len(cols) - 1 and wl[0] == "View product"
                    c.setFont(FONT_BB if is_link else FONT_B, 8)
                    c.setFillColor(C["accent"] if is_link else C["text"])
                    for k, ln in enumerate(wl):
                        c.drawString(x, y - k * 11, ln)
                    if is_link:
                        c.linkURL(it["Product URL"], (x, y - 3, x + w - 10, y + 9), relative=0)
                    x += w
            y -= rh
            i += 1
        if not dry:
            if draft:
                draft_mark(c)
            footer(c, p, start_page + page)
            c.showPage()
        page += 1
        if not rows:
            break
    return page


def page_back(c, p, page_no, draft):
    c.setFillColor(C["dark"])
    c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
    if not logo(c, PAGE_W / 2, PAGE_H / 2 + 40, 40, max_width=300, centered=True):
        spaced(c, PAGE_W / 2, PAGE_H / 2 + 50, CFG["company_name"], FONT_BB, 16, 5, white, "center")
    c.setStrokeColor(C["accent"])
    c.line(PAGE_W / 2 - 20, PAGE_H / 2 + 30, PAGE_W / 2 + 20, PAGE_H / 2 + 30)
    c.setFont(FONT_H, 18)
    c.setFillColor(white)
    c.drawCentredString(PAGE_W / 2, PAGE_H / 2, "Thank you")
    if CFG.get("contact_line"):
        spaced(c, PAGE_W / 2, PAGE_H / 2 - 28, CFG["contact_line"], FONT_B, 7.5, 1.2, C["paper"], "center")
    c.setFont(FONT_B, 7)
    c.setFillColor(C["rule"])
    yy = 80
    for ln in simpleSplit(CFG.get("disclaimer", ""), FONT_B, 7, 460):
        c.drawCentredString(PAGE_W / 2, yy, ln)
        yy -= 10
    if draft:
        draft_mark(c)
    footer(c, p, page_no, dark=True)
    c.showPage()


# --------------------------------------------------------------------------
def build(pid, project, rows, draft, output_dir=None):
    sections = group_sections(rows)
    pages, first = layout_rows(sections)
    overview_count = max(1, math.ceil((len(sections) + 1) / 10))
    start_page = 2 + overview_count
    page_map = {sec: start_page + first[sec] for sec in sections}
    schedule_start = start_page + len(pages)

    destination = output_dir or OUT
    destination.mkdir(exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", project.get("Project Name", "")).strip("_")[:100]
    safe_pid = re.sub(r"[^A-Za-z0-9_-]", "_", pid)[:50]
    out = destination / f"{safe_pid}_{safe}_Lookbook{'_DRAFT' if draft else ''}.pdf"
    c = canvas.Canvas(str(out), pagesize=(PAGE_W, PAGE_H))
    c.setTitle(f"{project.get('Project Name', pid)} - {CFG.get('tagline', 'Selections Lookbook')}")
    c.setAuthor(CFG["company_name"])
    c.showOutline()

    page_cover(c, project, sections, draft)
    for index in range(overview_count):
        chunk = OrderedDict(list(sections.items())[index * 10:(index + 1) * 10])
        page_overview(c, project, chunk, page_map, schedule_start, draft,
                      section_offset=index * 10, page_no=2 + index, show_schedule=index == overview_count - 1)
    selection_pages(c, project, pages, sections, start_page, draft)
    n = schedule_pages(c, project, rows, schedule_start, draft)
    page_back(c, project, schedule_start + n, draft)
    c.save()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--project", help="Project ID, e.g. UH-101")
    g.add_argument("--all", action="store_true", help="Every project on the Projects sheet")
    ap.add_argument("--verified-only", action="store_true", help="Only include rows marked Verified")
    ap.add_argument("--draft", action="store_true", help="Internal review copy with status tags")
    args = ap.parse_args()

    projects, items = load_data(args.verified_only)
    ids = list(projects) if args.all else [args.project]
    for pid in ids:
        if pid not in projects:
            print(f"x {pid}: not on the Projects sheet")
            continue
        rows = items.get(pid, [])
        if not rows:
            print(f"- {pid}: no selections to include, skipped")
            continue
        pending = [r for r in rows if r["Lookup Status"] != "Verified"]
        out = build(pid, projects[pid], rows, args.draft)
        print(f"+ {pid}: {len(rows)} selections -> {out.relative_to(BASE_DIR)}")
        if pending and not args.draft:
            print(f"  ! {len(pending)} row(s) not marked Verified (sheet rows "
                  f"{', '.join(str(r['_row']) for r in pending[:12])}{'...' if len(pending) > 12 else ''}). "
                  "Review before sending, or use --verified-only.")


if __name__ == "__main__":
    main()
