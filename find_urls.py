"""
Fill Product URL / Product Name / Image URL for every selection that has a
Manufacturer + Model # and hasn't been verified yet.

Usage:
    python find_urls.py                    # all projects, rows without a URL
    python find_urls.py --project UH-101   # one project
    python find_urls.py --force            # re-check rows that already have a URL (never 'Verified' rows)
    python find_urls.py --dry-run          # show results, don't save

Search provider is chosen with SEARCH_PROVIDER in .env: brave | serpapi | claude
Close the workbook in Excel before running (Excel locks the file).
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from urllib.parse import urlparse, quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from common import (Sheet, clean, load_env, manufacturer_domains, norm_model,
                    WORKBOOK)

from remote import fetch_html
from workbook_store import ConflictError, IDENTITY_FIELDS, ValidationError, WorkbookStore

load_env()
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT = 15
MAX_PAGES_TO_CHECK = 4


# --------------------------------------------------------------------------
# Search providers - each returns a list of {"url", "title", "snippet"}
# --------------------------------------------------------------------------
def search_brave(query):
    key = os.environ["BRAVE_API_KEY"]
    r = requests.get("https://api.search.brave.com/res/v1/web/search",
                     headers={"X-Subscription-Token": key, "Accept": "application/json"},
                     params={"q": query, "count": 10}, timeout=TIMEOUT)
    r.raise_for_status()
    return [{"url": x.get("url", ""), "title": x.get("title", ""), "snippet": x.get("description", "")}
            for x in r.json().get("web", {}).get("results", [])]


def search_serpapi(query):
    key = os.environ.get("SERPAPI_KEY") or os.environ["SERPAPI_API_KEY"]
    r = requests.get("https://serpapi.com/search.json",
                     params={"engine": "google", "q": query, "num": 10, "api_key": key},
                     timeout=TIMEOUT)
    r.raise_for_status()
    return [{"url": x.get("link", ""), "title": x.get("title", ""), "snippet": x.get("snippet", "")}
            for x in r.json().get("organic_results", [])]


def search_claude(query):
    """Uses the Claude API with its web search tool and asks for candidate URLs as JSON."""
    key = os.environ["ANTHROPIC_API_KEY"]
    prompt = (
        f"Find the official product page(s) for this building product. Search query: {query}\n"
        "Return ONLY a JSON array (no prose, no code fences) of up to 5 objects with keys "
        '"url", "title", "snippet". Prefer the manufacturer\'s own website and the exact model page.'
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={
            "model": os.getenv("CLAUDE_MODEL", "claude-sonnet-5"),
            "max_tokens": 1500,
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    text = text.replace("```json", "").replace("```", "").strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1:
        return []
    try:
        return [x for x in json.loads(text[start:end + 1]) if isinstance(x, dict) and x.get("url")]
    except json.JSONDecodeError:
        return []


PROVIDERS = {"brave": search_brave, "serpapi": search_serpapi, "claude": search_claude}


# --------------------------------------------------------------------------
# Page checking
# --------------------------------------------------------------------------
def host(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


def on_domain(url, domain):
    h = host(url)
    return bool(domain) and (h == domain or h.endswith("." + domain))


def exact_model(text, model_n):
    if not model_n:
        return False
    pattern = r"(?<![a-z0-9_./-])" + r"[\s\-_/\.]*".join(re.escape(c) for c in model_n) + r"(?![a-z0-9]|[-_/.][a-z0-9])"
    return bool(re.search(pattern, clean(text), re.IGNORECASE))


def products(value):
    if isinstance(value, list):
        for item in value:
            yield from products(item)
    elif isinstance(value, dict):
        kind = value.get("@type", [])
        if kind == "Product" or isinstance(kind, list) and "Product" in kind:
            yield value
        for key in ("@graph", "mainEntity"):
            if key in value:
                yield from products(value[key])


def meta_value(soup, *names):
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def product_cards(soup):
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.get_text())
        except (ValueError, TypeError):
            continue
        out.extend(products(data))
    return out


def product_image(product, fallback):
    img = product.get("image") or fallback
    if isinstance(img, list):
        img = img[0] if img else ""
    if isinstance(img, dict):
        img = img.get("url", "")
    return img if isinstance(img, str) else ""


def inspect_page(url, model_n):
    """Download the page; return (model_found, product_name, image_url)."""
    try:
        html, final_url = fetch_html(url)
        if not html or host(url) != host(final_url):
            return False, "", ""
    except (requests.RequestException, OSError, ValueError):
        return False, "", ""
    soup = BeautifulSoup(html, "html.parser")

    name = meta_value(soup, "og:title", "twitter:title") or (soup.title.get_text(strip=True) if soup.title else "")
    image = meta_value(soup, "og:image", "og:image:secure_url", "twitter:image")
    page_products = product_cards(soup)
    if len(page_products) > 1:
        return False, "", ""
    for product in page_products:
        ids = [product.get(k) for k in ("sku", "mpn", "model", "productID")]
        if any(norm_model(v) == model_n for v in ids if isinstance(v, (str, int))):
            img = product_image(product, image)
            return True, clean(product.get("name") or name)[:200], urljoin(final_url, img) if img else ""
        if any(ids):
            return False, "", ""
    heading = soup.find("h1")
    product_type = meta_value(soup, "og:type").lower()
    prominent = exact_model(name, model_n) or (heading is not None and exact_model(heading.get_text(" "), model_n))
    if prominent and (product_type in {"product", "og:product"} or soup.select_one('[itemtype*="schema.org/Product"]')):
        return True, name[:200], urljoin(final_url, image) if image else ""
    return False, "", ""


def describe_page(url):
    """Best-effort selection fields from a product link, for the add-from-link flow."""
    try:
        html, final_url = fetch_html(url)
    except (requests.RequestException, OSError, ValueError):
        return {}
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    name = meta_value(soup, "og:title", "twitter:title") or (soup.title.get_text(strip=True) if soup.title else "")
    image = meta_value(soup, "og:image", "og:image:secure_url", "twitter:image")
    model, brand = "", ""
    cards = product_cards(soup)
    if cards:
        product = cards[0]
        name = clean(product.get("name")) or name
        image = product_image(product, image)
        for key in ("sku", "mpn", "model", "productID"):
            if isinstance(product.get(key), (str, int)) and clean(product[key]):
                model = clean(product[key])
                break
        maker = product.get("brand") or product.get("manufacturer")
        if isinstance(maker, dict):
            maker = maker.get("name")
        brand = clean(maker) if isinstance(maker, (str, int)) else ""
    image = urljoin(final_url, image) if image else ""
    return {"Product URL": final_url, "Item": clean(name)[:200], "Product Name": clean(name)[:200],
            "Image URL": image, "Model #": model, "Manufacturer": brand, "Lookup Status": "Found - verify"}


def score(c, domain, model_n):
    s = 0
    if on_domain(c["url"], domain):
        s += 5
    if model_n and model_n in norm_model(c["url"]):
        s += 3
    if model_n and model_n in norm_model(c.get("title", "") + c.get("snippet", "")):
        s += 2
    return s


def fallback_link(domain, manufacturer, model):
    q = f"site:{domain} {model}" if domain else f"{manufacturer} {model}"
    return "https://www.google.com/search?q=" + quote_plus(q)


def lookup(search, manufacturer, model, domain, product_name=""):
    """Returns dict with url, name, image, status, notes."""
    model_n = norm_model(model)
    broad = f'"{manufacturer}" "{model}"'
    queries = [f'site:{domain} "{model}"'] if domain else []
    if clean(product_name):
        queries.append(f'{broad} {clean(product_name)[:150]}')
    queries.append(broad)
    seen, verified = set(), []
    for q in queries:
        candidates = []
        try:
            for c in search(q):
                u = clean(c.get("url")).split("#")[0]
                if urlparse(u).scheme in {"http", "https"} and u not in seen:
                    seen.add(u)
                    candidates.append({**c, "url": u})
        except Exception as e:  # provider/network error - record and continue
            return {"status": "Search error", "notes": "Search provider unavailable. Check your API key, quota, and internet connection, then retry."}
        candidates.sort(key=lambda c: score(c, domain, model_n), reverse=True)
        for c in candidates[:MAX_PAGES_TO_CHECK]:
            ok, name, image = inspect_page(c["url"], model_n)
            if ok:
                verified.append({**c, "name": name or c.get("title", ""), "image": image,
                                 "official": on_domain(c["url"], domain)})
        if any(c["official"] for c in verified):
            break  # official-site hits found; skip the broad query

    if not verified:
        return {"status": "Not found",
                "notes": f"No page confirmed the model number. Try: {fallback_link(domain, manufacturer, model)}"}

    official = [v for v in verified if v["official"]]
    pool = official or verified
    best = pool[0]
    others = [v["url"] for v in pool[1:]]
    if official:
        status = "Multiple matches" if others else "Found - verify"
    else:
        status = "Found - retailer"
    notes = ""
    if others:
        notes = "Other matching pages: " + " | ".join(others[:3])
    if not official:
        notes = (notes + " " if notes else "") + "Not on the manufacturer's site - confirm or add the brand domain."
    return {"url": best["url"], "name": best["name"], "image": best["image"],
            "status": status, "notes": notes[:500]}


def identity(data):
    """What a lookup searched for; results are only applied while this still matches."""
    return tuple(clean(data.get(key)) for key in ["Project ID", *IDENTITY_FIELDS])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help="Only this Project ID")
    ap.add_argument("--force", action="store_true", help="Re-check rows that already have a URL")
    ap.add_argument("--dry-run", action="store_true", help="Don't save the workbook")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds between lookups")
    ap.add_argument("--limit", type=int, default=50, help="Maximum paid searches this run may spend (0 = no cap)")
    args = ap.parse_args()

    provider = os.getenv("SEARCH_PROVIDER", "brave").lower()
    if provider not in PROVIDERS:
        sys.exit(f"SEARCH_PROVIDER must be one of {list(PROVIDERS)}")
    search = PROVIDERS[provider]

    store = WorkbookStore(WORKBOOK)
    try:
        wb, _ = store.snapshot()
    except ValidationError as error:
        sys.exit(f"Workbook problem: {error}")
    domains = manufacturer_domains(wb)
    today = dt.date.today().isoformat()

    # Phase 1 - collect jobs and search. Nothing is written to the workbook yet.
    jobs = []
    for r, d in Sheet(wb["Selections"]).rows():
        if args.project and clean(d["Project ID"]) != args.project:
            continue
        if clean(d["Lookup Status"]) == "Verified":
            continue
        if clean(d["Product URL"]) and not args.force:
            continue
        jobs.append({"row": r, "identity": identity(d), "mfr": clean(d["Manufacturer"]),
                     "model": clean(d["Model #"]), "name": clean(d["Product Name"]) or clean(d["Item"]),
                     "has_url": bool(clean(d["Product URL"])), "result": None})

    searches = [job for job in jobs if job["mfr"] and job["model"]]
    if args.limit and len(searches) > args.limit:
        sys.exit(f"{len(searches)} selections need a search, above --limit {args.limit}. "
                 f"Narrow with --project or raise --limit.")
    if args.dry_run:
        print(f"Dry run - {len(jobs)} row(s) eligible ({len(searches)} searches), nothing checked or saved.")
        return

    for job in jobs:
        if not job["mfr"] or not job["model"]:
            job["result"] = {"status": "Not found", "notes": "Manufacturer and Model # are both required."}
            continue
        domain = domains.get(job["mfr"].lower(), "")
        print(f"[{job['identity'][0]}] {job['mfr']} {job['model']} ... ", end="", flush=True)
        job["result"] = lookup(search, job["mfr"], job["model"], domain, product_name=job["name"])
        job["domain"] = domain
        print(job["result"]["status"], job["result"].get("url", ""))
        time.sleep(args.delay)

    if not jobs:
        print("Nothing to do.")
        return

    # Phase 2 - apply results to a fresh snapshot in one validated, backed-up save.
    # Rows whose identity changed since phase 1 are skipped rather than overwritten.
    wb, revision = store.snapshot()
    sel = Sheet(wb["Selections"])
    current = {r: d for r, d in sel.rows()}
    by_identity = {}
    for r, d in current.items():
        by_identity.setdefault(identity(d), r)
    applied = skipped = 0
    for job in jobs:
        res = job["result"]
        row = job["row"]
        data = current.get(row)
        if data is None or identity(data) != job["identity"]:
            row = by_identity.get(job["identity"])  # row may have moved in Excel
            data = current.get(row) if row else None
        if data is None or clean(data["Lookup Status"]) == "Verified":
            skipped += 1  # deleted, changed, or verified while the search ran
            continue
        if clean(data["Product URL"]) and (not job["has_url"] or not args.force):
            skipped += 1  # a link was added by hand after the search - keep it
            continue
        sel.set(row, "Lookup Status", res["status"])
        notes = res.get("notes", "")
        if job["mfr"] and job["model"] and not job.get("domain"):
            notes += " (Brand missing from Manufacturers sheet.)"
        sel.set(row, "Lookup Notes", notes)
        if job["mfr"] and job["model"]:
            sel.set(row, "Checked On", today)
        if res.get("url"):
            sel.set(row, "Product URL", res["url"])
            sel.ws.cell(row=row, column=sel.cols["Product URL"]).hyperlink = res["url"]
            if not clean(data["Product Name"]) or args.force:
                sel.set(row, "Product Name", res.get("name", ""))
            img = clean(data["Image URL"])
            if (not img or img.startswith("http")) and res.get("image"):
                sel.set(row, "Image URL", res["image"])
        applied += 1

    try:
        store.save(wb, revision, f"find_urls ({applied} rows)")
    except PermissionError:
        sys.exit("Could not save - close the workbook in Excel and run again.")
    except ConflictError:
        sys.exit("The workbook changed while saving - close other editors and run again.")
    print(f"Done - {applied} row(s) updated, {skipped} skipped. Review the 'Lookup Status' column, then mark good rows 'Verified'.")


if __name__ == "__main__":
    main()
