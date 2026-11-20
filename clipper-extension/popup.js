"use strict";

const $ = (id) => document.getElementById(id);
let settings = null;            // { appUrl, token, direct }
let page = null;                // what collect() read from the active tab
let selected = null;            // the chosen image {url, alt}

const getSettings = () => new Promise((done) => chrome.storage.local.get(["appUrl", "token", "direct"], done));
const setStatus = (id, message, error = false) => { const el = $(id); el.textContent = message; el.style.color = error ? "#984b3c" : ""; };

function showSetup() {
  $("setup").hidden = false;
  $("clipper").hidden = true;
  $("setting-url").value = settings?.appUrl || "";
  $("setting-token").value = settings?.token || "";
  $("setting-direct").checked = !!settings?.direct;
}

async function saveSettings() {
  let appUrl;
  try {
    appUrl = new URL($("setting-url").value.trim()).origin;
  } catch {
    setStatus("setup-status", "Enter the app's full address, starting with http(s)://.", true);
    return;
  }
  const token = $("setting-token").value.trim();
  if (!token) { setStatus("setup-status", "Paste a clipper token from the app (Inspirations → Get the clipper).", true); return; }
  const origins = [`${appUrl}/*`];
  if ($("setting-direct").checked) origins.push("http://*/*", "https://*/*");
  const granted = await chrome.permissions.request({ origins });
  if (!granted) { setStatus("setup-status", "Permission to reach that address was declined.", true); return; }
  setStatus("setup-status", "Checking the connection…");
  try {
    const response = await fetch(`${appUrl}/api/catalog/clipper/collections`, { headers: { "X-UH-Clipper": token } });
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || `The app answered ${response.status}. Check the address and token.`);
  } catch (error) {
    setStatus("setup-status", error.message || "The app could not be reached.", true);
    return;
  }
  settings = { appUrl, token, direct: $("setting-direct").checked };
  await chrome.storage.local.set(settings);
  setStatus("setup-status", "Connected.");
  startClipper();
}

// Runs inside the page: every usable image plus the page's own metadata, including the
// spec-sheet fields (brand, model, finish, size, price, materials) its structured data offers.
function collect() {
  const meta = (name) => document.querySelector(`meta[property="${name}"], meta[name="${name}"]`)?.content || "";
  const micro = (name) => document.querySelector(`[itemprop="${name}" i][content]`)?.getAttribute("content") || "";
  const product = {};
  const nameOf = (node) => String((node && typeof node === "object" ? node.name : node) || "").trim();
  const walk = (node) => {
    if (Array.isArray(node)) { node.forEach(walk); return; }
    if (!node || typeof node !== "object") return;
    if ([].concat(node["@type"] || []).some((k) => typeof k === "string" && k.toLowerCase() === "product")) {
      const offers = Array.isArray(node.offers) ? node.offers[0] : node.offers;
      if (!product.price && offers && typeof offers === "object" && (offers.price || offers.lowPrice)) {
        const amount = String(offers.price || offers.lowPrice);
        const currency = String(offers.priceCurrency || "");
        product.price = currency && !amount.toUpperCase().includes(currency.toUpperCase()) ? `${amount} ${currency.toUpperCase()}` : amount;
      }
      product.brand = product.brand || nameOf(node.brand);
      product.model = product.model || String(node.sku || node.mpn || "").trim() || nameOf(node.model);
      product.finish = product.finish || nameOf(node.color);
      product.size = product.size || nameOf(node.size);
      product.materials = product.materials || [].concat(node.material || []).map(nameOf).filter(Boolean).join(", ");
    }
    Object.values(node).forEach(walk);
  };
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { walk(JSON.parse(script.textContent)); } catch { /* unreadable structured data is skipped */ }
  }
  const seen = new Set();
  const images = [];
  const add = (url, alt, area) => {
    if (!url || seen.has(url) || !/^https?:/.test(url)) return;
    if (/\.svg(\?|$)/i.test(url)) return;
    seen.add(url);
    images.push({ url, alt: (alt || "").slice(0, 200), area });
  };
  for (const key of ["og:image", "twitter:image"]) if (meta(key)) add(new URL(meta(key), location.href).href, meta("og:title"), 1e9);
  for (const img of document.images) {
    const area = (img.naturalWidth || img.width) * (img.naturalHeight || img.height);
    if (area && area < 10000) continue;        // icons and trackers
    add(img.currentSrc || img.src, img.alt, area || 0);
  }
  images.sort((a, b) => b.area - a.area);
  return { url: location.href, title: meta("og:title") || document.title || "",
           site: meta("og:site_name") || location.hostname.replace(/^www\./, ""),
           description: meta("og:description") || meta("description") || "",
           price: (product.price || meta("product:price:amount") || micro("price") || "").slice(0, 50),
           brand: (product.brand || meta("product:brand") || meta("og:brand") || micro("brand") || "").slice(0, 200),
           model: (product.model || micro("sku") || micro("mpn") || "").slice(0, 100),
           finish: (product.finish || meta("product:color") || micro("color") || "").slice(0, 200),
           size: (product.size || meta("product:size") || micro("size") || "").slice(0, 200),
           materials: (product.materials || meta("product:material") || micro("material") || "").slice(0, 500),
           images: images.slice(0, 60) };
}

async function startClipper() {
  $("setup").hidden = true;
  $("clipper").hidden = false;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !/^https?:/.test(tab.url || "")) {
    $("page-title").textContent = "Open a web page to clip from it.";
    $("no-images").hidden = false;
    return;
  }
  try {
    const [{ result }] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: collect });
    page = result;
  } catch {
    $("page-title").textContent = "This page cannot be read by extensions.";
    $("no-images").hidden = false;
    return;
  }
  $("page-title").textContent = `${page.site} — ${page.title}`;
  $("clip-title").value = page.title.slice(0, 200);
  $("no-images").hidden = page.images.length > 0;
  $("images").innerHTML = "";
  page.images.forEach((image, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.title = image.alt || image.url;
    const img = document.createElement("img");
    img.src = image.url;
    img.alt = image.alt || "";
    img.addEventListener("error", () => button.remove());
    button.appendChild(img);
    button.addEventListener("click", () => {
      selected = image;
      if (image.alt && !$("clip-title").value) $("clip-title").value = image.alt;
      document.querySelectorAll("#images button").forEach((b) => b.classList.toggle("selected", b === button));
      $("clip-save").disabled = false;
    });
    $("images").appendChild(button);
  });
  try {
    const response = await fetch(`${settings.appUrl}/api/catalog/clipper/collections`, { headers: { "X-UH-Clipper": settings.token } });
    if (response.status === 401 || response.status === 403) { showSetup(); setStatus("setup-status", "The token has expired. Generate a new one in the app.", true); return; }
    const data = await response.json();
    $("clip-collection").innerHTML = (data.collections || []).map((c) => `<option value="${c.id}">${c.name} (${c.count})</option>`).join("")
      + '<option value="__new">— New collection —</option>';
    chrome.storage.local.get(["lastCollection"], ({ lastCollection }) => {
      if (lastCollection && [...$("clip-collection").options].some((o) => o.value === lastCollection)) $("clip-collection").value = lastCollection;
      $("new-collection-row").hidden = $("clip-collection").value !== "__new";
    });
  } catch {
    setStatus("clip-status", "The app could not be reached for your collections.", true);
  }
}

async function clip() {
  if (!selected) return;
  const collection = $("clip-collection").value === "__new" ? "" : $("clip-collection").value;
  const newName = $("clip-collection").value === "__new" ? $("clip-new-collection").value.trim() : "";
  if (!collection && !newName) { setStatus("clip-status", "Choose a collection or name a new one.", true); return; }
  $("clip-save").disabled = true;
  setStatus("clip-status", "Saving to UH Homes…");
  const body = { image_url: selected.url, source_url: page.url, source_site: page.site,
                 title: $("clip-title").value.trim(), description: page.description,
                 price: page.price || "", brand: page.brand || "", model: page.model || "",
                 finish: page.finish || "", size: page.size || "", materials: page.materials || "",
                 collection, new_collection: newName };
  const response = await chrome.runtime.sendMessage({ type: "uh-clip", settings, body });
  if (response?.ok) {
    chrome.storage.local.set({ lastCollection: response.collection });
    setStatus("clip-status", "Saved. Find it under Inspirations in UH Homes.");
  } else {
    setStatus("clip-status", response?.error || "The inspiration could not be saved.", true);
    $("clip-save").disabled = false;
  }
}

async function enableHoverMode() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return;
  const collection = $("clip-collection").value === "__new" ? "" : $("clip-collection").value;
  if (!collection) { setStatus("clip-status", "Pick an existing collection for hover saves first.", true); return; }
  await chrome.storage.local.set({ hoverCollection: collection });
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
    window.close();
  } catch {
    setStatus("clip-status", "Hover save cannot run on this page.", true);
  }
}

$("save-settings").addEventListener("click", saveSettings);
$("open-settings").addEventListener("click", showSetup);
$("clip-save").addEventListener("click", clip);
$("hover-mode").addEventListener("click", enableHoverMode);
$("clip-collection").addEventListener("change", () => { $("new-collection-row").hidden = $("clip-collection").value !== "__new"; });

getSettings().then((stored) => {
  settings = stored.appUrl && stored.token ? { appUrl: stored.appUrl, token: stored.token, direct: !!stored.direct } : null;
  if (settings) startClipper(); else showSetup();
});
