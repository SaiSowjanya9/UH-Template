"use strict";

const $ = (id) => document.getElementById(id);
let settings = null;            // { appUrl, token, direct }
let page = null;                // what the scanner read from the active tab
let selected = null;            // the chosen image {url, alt}

// The details panel: each input is prefilled with what the scanner found and stays
// editable, so the person clipping chooses exactly what is saved — clearing a box
// drops that detail, retyping it corrects the page's own wording.
const DETAIL_FIELDS = ["price", "brand", "model", "finish", "size", "materials", "description"];

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

// The page is read by scan.js (shared with hover save): injected first, then asked for
// everything it found — images plus the spec-sheet details, wherever the vendor put them.
async function readPage(tabId) {
  await chrome.scripting.executeScript({ target: { tabId }, files: ["scan.js"] });
  const [{ result }] = await chrome.scripting.executeScript({ target: { tabId }, func: () => window.__uhCollect() });
  return result;
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
    page = await readPage(tab.id);
  } catch {
    $("page-title").textContent = "This page cannot be read by extensions.";
    $("no-images").hidden = false;
    return;
  }
  $("page-title").textContent = `${page.site} — ${page.title}`;
  $("clip-title").value = page.title.slice(0, 200);
  for (const field of DETAIL_FIELDS) $(`clip-${field}`).value = page[field] || "";
  const count = DETAIL_FIELDS.filter((field) => page[field]).length;
  $("details-note").textContent = count ? `${count} detail${count === 1 ? "" : "s"} found on the page — edit or clear anything before saving.`
                                        : "Nothing found on this page — fill in what you know.";
  $("no-images").hidden = page.images.length > 0;
  $("images").innerHTML = "";
  page.images.forEach((image) => {
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
    const headers = { "X-UH-Clipper": settings.token };
    const [collections, projects] = await Promise.all([
      fetch(`${settings.appUrl}/api/catalog/clipper/collections`, { headers }),
      fetch(`${settings.appUrl}/api/catalog/clipper/projects`, { headers }),
    ]);
    if (collections.status === 401 || collections.status === 403) { showSetup(); setStatus("setup-status", "The token has expired. Generate a new one in the app.", true); return; }
    const data = await collections.json();
    $("clip-collection").innerHTML = (data.collections || []).map((c) => `<option value="${c.id}">${c.name} (${c.count})</option>`).join("")
      + '<option value="__new">— New collection —</option>';
    const listed = projects.ok ? (await projects.json()).projects || [] : [];
    $("clip-project").innerHTML = '<option value="">Library only</option>'
      + listed.map((p) => `<option value="${p.id}">${p.id} — ${p.name}</option>`).join("");
    chrome.storage.local.get(["lastCollection", "lastProject"], ({ lastCollection, lastProject }) => {
      if (lastCollection && [...$("clip-collection").options].some((o) => o.value === lastCollection)) $("clip-collection").value = lastCollection;
      $("new-collection-row").hidden = $("clip-collection").value !== "__new";
      if (lastProject && [...$("clip-project").options].some((o) => o.value === lastProject)) $("clip-project").value = lastProject;
      $("clip-room-row").hidden = !$("clip-project").value;
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
  const project = $("clip-project").value;
  $("clip-save").disabled = true;
  setStatus("clip-status", "Saving to UH Homes…");
  const body = { image_url: selected.url, source_url: page.url, source_site: page.site,
                 title: $("clip-title").value.trim(), collection, new_collection: newName,
                 projects: project ? [project] : [], room: project ? $("clip-room").value.trim() : "" };
  for (const field of DETAIL_FIELDS) body[field] = $(`clip-${field}`).value.trim();
  const response = await chrome.runtime.sendMessage({ type: "uh-clip", settings, body });
  if (response?.ok) {
    chrome.storage.local.set({ lastCollection: response.collection, lastProject: project });
    setStatus("clip-status", project ? `Saved — in your library and on ${project}'s spec sheet.`
                                     : "Saved. Find it under Inspirations in UH Homes.");
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
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["scan.js"] });
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
$("clip-project").addEventListener("change", () => { $("clip-room-row").hidden = !$("clip-project").value; });

getSettings().then((stored) => {
  settings = stored.appUrl && stored.token ? { appUrl: stored.appUrl, token: stored.token, direct: !!stored.direct } : null;
  if (settings) startClipper(); else showSetup();
});
