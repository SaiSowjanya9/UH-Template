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
                    open_workbook, WORKBOOK)

from remote import fetch_html

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
    key = os.environ["SERPAPI_KEY"]
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


def inspect_page(url, model_n):
    """Download the page; return (model_found, product_name, image_url)."""
    try:
        html, final_url = fetch_html(url)
        if not html or host(url) != host(final_url):
            return False, "", ""
    except (requests.RequestException, OSError, ValueError):
        return False, "", ""
    soup = BeautifulSoup(html, "html.parser")

    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""

    name = meta("og:title", "twitter:title") or (soup.title.get_text(strip=True) if soup.title else "")
    image = meta("og:image", "og:image:secure_url", "twitter:image")
    page_products = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.get_text())
        except (ValueError, TypeError):
            continue
        page_products.extend(products(data))
    if len(page_products) > 1:
        return False, "", ""
    for product in page_products:
        ids = [product.get(k) for k in ("sku", "mpn", "model", "productID")]
        if any(norm_model(v) == model_n for v in ids if isinstance(v, (str, int))):
            img = product.get("image") or image
            if isinstance(img, list):
                img = img[0] if img else ""
            if isinstance(img, dict):
                img = img.get("url", "")
            return True, clean(product.get("name") or name)[:200], urljoin(final_url, img) if isinstance(img, str) and img else ""
        if any(ids):
            return False, "", ""
    heading = soup.find("h1")
    product_type = meta("og:type").lower()
    prominent = exact_model(name, model_n) or (heading is not None and exact_model(heading.get_text(" "), model_n))
    if prominent and (product_type in {"product", "og:product"} or soup.select_one('[itemtype*="schema.org/Product"]')):
        return True, name[:200], urljoin(final_url, image) if image else ""
    return False, "", ""


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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help="Only this Project ID")
    ap.add_argument("--force", action="store_true", help="Re-check rows that already have a URL")
    ap.add_argument("--dry-run", action="store_true", help="Don't save the workbook")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds between lookups")
    args = ap.parse_args()

    provider = os.getenv("SEARCH_PROVIDER", "brave").lower()
    if provider not in PROVIDERS:
        sys.exit(f"SEARCH_PROVIDER must be one of {list(PROVIDERS)}")
    search = PROVIDERS[provider]

    wb = open_workbook()
    domains = manufacturer_domains(wb)
    sel = Sheet(wb["Selections"])
    today = dt.date.today().isoformat()
    done = 0

    for r, d in sel.rows():
        pid, mfr, model = clean(d["Project ID"]), clean(d["Manufacturer"]), clean(d["Model #"])
        status = clean(d["Lookup Status"])
        if args.project and pid != args.project:
            continue
        if status == "Verified":
            continue
        if clean(d["Product URL"]) and not args.force:
            continue
        if not mfr or not model:
            sel.set(r, "Lookup Status", "Not found")
            sel.set(r, "Lookup Notes", "Manufacturer and Model # are both required.")
            continue

        domain = domains.get(mfr.lower(), "")
        print(f"[{pid}] {mfr} {model} ... ", end="", flush=True)
        res = lookup(search, mfr, model, domain)
        print(res["status"], res.get("url", ""))

        sel.set(r, "Lookup Status", res["status"])
        sel.set(r, "Checked On", today)
        sel.set(r, "Lookup Notes", res.get("notes", "") + ("" if domain else " (Brand missing from Manufacturers sheet.)"))
        if res.get("url"):
            sel.set(r, "Product URL", res["url"])
            sel.ws.cell(row=r, column=sel.cols["Product URL"]).hyperlink = res["url"]
            if not clean(d["Product Name"]) or args.force:
                sel.set(r, "Product Name", res["name"])
            img = clean(d["Image URL"])
            if (not img or img.startswith("http")) and res.get("image"):
                sel.set(r, "Image URL", res["image"])
        done += 1
        time.sleep(args.delay)

    if args.dry_run:
        print(f"Dry run - {done} row(s) checked, nothing saved.")
        return
    try:
        wb.save(WORKBOOK)
    except PermissionError:
        sys.exit("Could not save - close the workbook in Excel and run again.")
    print(f"Done - {done} row(s) checked. Review the 'Lookup Status' column, then mark good rows 'Verified'.")


if __name__ == "__main__":
    main()
