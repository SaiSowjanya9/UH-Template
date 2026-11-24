"use strict";

// The deep page reader the bookmarklet carries: /api/catalog/bookmarklet serves this file
// verbatim inside the hand-off wrapper. It looks wherever vendors actually put
// their product details — structured data first, then meta tags and microdata, then the
// visible spec tables, definition lists and labelled rows most stores hide the specifics
// in, and finally the page's own text for a price — so a page with no structured data at
// all still hands over its brand, SKU, dimensions and price.
window.__uhCollect = () => {
  const clean = (text) => String(text || "").replace(/\s+/g, " ").trim();
  const meta = (name) => clean(document.querySelector(`meta[property="${name}"], meta[name="${name}"]`)?.content);
  const LIMITS = { price: 50, brand: 200, model: 100, finish: 200, size: 200, description: 2000 };
  const product = {};
  const found = (field, value) => {
    value = clean(value);
    if (value && !product[field]) product[field] = value.slice(0, LIMITS[field]);
  };

  // 1. Structured data: a JSON-LD Product node is the closest a page comes to a spec sheet.
  const nameOf = (node) => clean(node && typeof node === "object" ? node.name : node);
  const walk = (node) => {
    if (Array.isArray(node)) { node.forEach(walk); return; }
    if (!node || typeof node !== "object") return;
    if ([].concat(node["@type"] || []).some((k) => typeof k === "string" && k.toLowerCase() === "product")) {
      const offers = Array.isArray(node.offers) ? node.offers[0] : node.offers;
      if (offers && typeof offers === "object" && (offers.price || offers.lowPrice)) {
        const amount = String(offers.price || offers.lowPrice);
        const currency = clean(offers.priceCurrency).toUpperCase();
        found("price", currency && !amount.toUpperCase().includes(currency) ? `${amount} ${currency}` : amount);
      }
      found("brand", nameOf(node.brand));
      found("model", clean(node.sku || node.mpn) || nameOf(node.model));
      found("finish", nameOf(node.color));
      found("size", nameOf(node.size));
      found("description", typeof node.description === "string" ? node.description : "");
    }
    Object.values(node).forEach(walk);
  };
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { walk(JSON.parse(script.textContent)); } catch { /* unreadable structured data is skipped */ }
  }

  // 2. Meta tags and microdata. A microdata value is its content attribute or its text.
  const micro = (name) => {
    const el = document.querySelector(`[itemprop="${name}" i]`);
    return clean(el?.getAttribute("content") || el?.textContent).slice(0, 300);
  };
  found("price", meta("product:price:amount") || meta("og:price:amount") || micro("price"));
  found("brand", meta("product:brand") || meta("og:brand") || micro("brand"));
  found("model", micro("sku") || micro("mpn") || micro("model"));
  found("finish", meta("product:color") || micro("color"));
  found("size", meta("product:size") || micro("size"));

  // 3. Labelled rows: spec tables, definition lists, "Label: value" list entries and
  // Twitter's label/data meta pairs. Hidden rows count too — specs live behind tabs.
  const fieldFor = (label) => {
    if (/^(brand|manufacturer|vendor|made by|designed by)$/.test(label)) return "brand";
    if (/^(sku|mpn|upc|model|(model|item|part|style|article)\s*(#|no\.?|number))$/.test(label)) return "model";
    if (/^(colou?r|finish|finish\s*\/\s*colou?r|colou?r\s*\/\s*finish)$/.test(label)) return "finish";
    if (/^(dimensions?|measurements|size|(overall|assembled|product)\s*(size|dimensions?))$/.test(label)) return "size";
    if (/^((our|sale|your|list|regular|current)\s+)?price$/.test(label)) return "price";
    return "";
  };
  const dims = {};
  let pairs = 0;
  const pair = (label, value) => {
    if (pairs > 600) return;
    pairs += 1;
    label = clean(label).toLowerCase().replace(/\s*[:：]$/, "");
    value = clean(value);
    if (!label || label.length > 40 || !value || value.length > 300) return;
    const dim = label.match(/^(width|height|depth|length|diameter)$/);
    if (dim) { dims[dim[1]] = dims[dim[1]] || value; return; }
    const field = fieldFor(label);
    if (field) found(field, value);
  };
  for (let i = 1; i <= 6; i += 1) pair(meta(`twitter:label${i}`), meta(`twitter:data${i}`));
  for (const row of document.querySelectorAll("table tr")) {
    const cells = row.querySelectorAll("th, td");
    if (cells.length === 2) pair(cells[0].textContent, cells[1].textContent);
  }
  for (const term of document.querySelectorAll("dl dt")) {
    const value = term.nextElementSibling;
    if (value?.tagName === "DD") pair(term.textContent, value.textContent);
  }
  for (const line of document.querySelectorAll("li, [class*='spec' i] p, [class*='spec' i] div")) {
    if (line.childElementCount > 2) continue;
    const text = clean(line.textContent);
    const split = text.length <= 160 && text.match(/^([^:：]{1,40})[:：]\s*(\S.*)$/);
    if (split) pair(split[1], split[2]);
  }
  if (!product.size) {
    const SHORT = { width: "W", depth: "D", height: "H", length: "L", diameter: "Dia" };
    found("size", Object.keys(SHORT).filter((k) => dims[k]).map((k) => `${dims[k]} ${SHORT[k]}`).join(" x "));
  }

  // 4. A price printed on the page, when nothing structured named one. Only a visible
  // element counts: the hidden price is usually the crossed-out old one.
  if (!product.price) {
    const MONEY = /(?:[$€£]\s?\d[\d,]*(?:\.\d{1,2})?)|(?:\d[\d,]*(?:\.\d{1,2})?\s?(?:USD|CAD|EUR|GBP))/;
    for (const el of document.querySelectorAll('[itemprop="price" i], [class*="price" i], [id*="price" i]')) {
      if (!el.getClientRects().length) continue;
      const match = clean(el.textContent).slice(0, 400).match(MONEY);
      if (match) { found("price", match[0]); break; }
    }
  }

  // 5. The description: the page's own summary first, then what the product node or a
  // description block offers.
  const block = document.querySelector('[class*="product-desc" i] p, [id*="description" i] p, [class*="description" i] p');
  const description = (meta("og:description") || meta("description") || product.description
                       || clean(block?.textContent)).slice(0, LIMITS.description);

  // 6. Every usable image, largest first; og/twitter images lead because they are the
  // page's own choice of hero shot.
  const seen = new Set();
  const images = [];
  const add = (url, alt, area) => {
    if (!url || seen.has(url) || !/^https?:/.test(url)) return;
    if (/\.svg(\?|$)/i.test(url)) return;
    seen.add(url);
    images.push({ url, alt: (alt || "").slice(0, 200), area });
  };
  for (const key of ["og:image", "twitter:image"]) {
    if (meta(key)) {
      try { add(new URL(meta(key), location.href).href, meta("og:title"), 1e9); } catch { /* a malformed address is skipped */ }
    }
  }
  for (const img of document.images) {
    const area = (img.naturalWidth || img.width) * (img.naturalHeight || img.height);
    if (area && area < 10000) continue;        // icons and trackers
    add(img.currentSrc || img.src, img.alt, area || 0);
  }
  images.sort((a, b) => b.area - a.area);

  return { url: location.href, title: meta("og:title") || document.title || "",
           site: meta("og:site_name") || location.hostname.replace(/^www\./, ""),
           description,
           price: product.price || "", brand: product.brand || "", model: product.model || "",
           finish: product.finish || "", size: product.size || "",
           images: images.slice(0, 60) };
};
