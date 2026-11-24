"use strict";

const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="uh-token"]').content;
const state = { data: null, project: "", view: "selections", page: 0, busy: false, editor: null,
                specGroup: "Room / Area", specCollapsed: new Set(), focusSelector: null,
                specPending: new Map(),
                specRename: null,
                specHeld: [], specNaming: false,
                specLinkEdit: new Set() };
// The spec sheet's group-by chips: by room, as it opens, or by category. A line item with nothing
// in that column sits under a placeholder heading, which is a label rather than a stored value.
const specPlaceholder = () => (state.specGroup === "Section" ? "Other" : "Unassigned");
const groupLabel = () => (state.specGroup === "Section" ? "category" : "room");
// A group headed PROJECT carries the home's own figures - areas, garage size, porch counts -
// not line items, so it is drawn like the client details box above the cards instead of as
// a table, matching the spec-sheet exports (DETAILS_GROUP in build_lookbook.py).
const isDetailsGroup = (name) => String(name || "").trim().toUpperCase() === "PROJECT";
const isBulkGroup = (name) => { const n = String(name || "").trim(); return n === "Entire Home" || n === "Whole House"; };
// A room is not a record of its own: it is on the sheet because line items name it. So a room
// added from the toolbar is held here - the project and field it belongs to, with the heading
// itself - and drawn as an empty card until its first line item puts it in the workspace.
// state.specNaming is the one card whose heading is still being typed.
const heldGroups = () => state.specHeld.filter((g) => g.field === state.specGroup && g.project === state.project);
const names = { selections: "Selection tracker", spec: "Spec sheet", manufacturers: "Manufacturers", catalog: "Design inspirations" };
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const isUrl = (value) => { try { return ["http:", "https:"].includes(new URL(value).protocol); } catch { return false; } };
const fixUrl = (url) => { const m = String(url || "").match(/^https?:\/\/misc\.hajoca\.com\/reports\/[^/]+\/s(\d+\.pdf)/); return m ? `https://www.hajoca.com/Misc/DisplayPDF.aspx?s=${m[1]}` : url; };
// A Cover Image cell -> something an <img> can show: stored upload or web address. Local file paths
// reach only the export builders, never the browser.
const coverSource = (value) => (value || "").startsWith("cover:") ? `/api/covers/${encodeURIComponent(value.slice(6))}` : isUrl(value) ? value : "";
const selectedProject = () => state.data?.projects.find((p) => p["Project ID"] === state.project);
const projectRows = () => state.data?.selections.filter((r) => r["Project ID"] === state.project) || [];
let toastTimer;
let EMBLEM_SVG;

function toast(message, error = false) {
  clearTimeout(toastTimer);
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", error);
  el.hidden = false;
  // An open modal dialog paints in the top layer, above any z-index, so a toast used to show
  // dimmed behind its backdrop — "no notification" to the person waiting on a save. As a
  // popover the toast joins the top layer; closing and reopening moves it back above a dialog
  // that has opened since the last message, because the top layer stacks by recency.
  if (el.showPopover) {
    if (el.matches(":popover-open")) el.hidePopover();
    el.showPopover();
  }
  toastTimer = setTimeout(() => {
    if (el.hidePopover && el.matches(":popover-open")) el.hidePopover();
    el.hidden = true;
  }, error ? 11000 : 5000);
}

// A request that never answers - a sleeping hosted instance, a dropped connection - used to
// leave the save counter above zero, so the toolbar read "Saving…" for ever while the edit sat
// in hand. Every request now has a deadline, which is generous enough for a cold start on the
// free hosting plan and still guarantees the promise settles.
const REQUEST_DEADLINE = 90000;
// A read answered just before a save landed describes the workspace as it used to be, so
// rendering it puts the old value back on screen - a cell someone cleared filling itself in
// again. Every write counts, and a read that straddled one is discarded instead of drawn.
let writes = 0, writing = 0;
// 502/503/504 are the host's router answering, not the app: the hosted workspace is deliberately
// one process, so a deploy or a restart leaves a few seconds with nothing behind the address.
// A read can simply be asked again - it changes nothing - and that covers the 2.5s poll and
// every refresh, which are what a restart usually interrupts. A write is deliberately not
// retried: a delete names a row by its number and the editor creates rows, so a second attempt
// could remove the wrong line item or add a duplicate. Instead it says what happened, and the
// edit stays in hand for the Save button to send again.
const GATEWAY = [502, 503, 504];
const GATEWAY_WAITS = [700, 2000, 4500];

async function api(path, body, binary = false) {
  const write = body !== undefined;
  const options = write ? { method: "POST", headers: { "X-UH-Token": token }, body } : {};
  if (write && !(body instanceof FormData)) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify({ revision: state.data?.revision, ...body });
  }
  // Counted as it starts: a read already in flight cannot describe what this write is about to
  // do, and a write that times out may still have reached the workspace.
  if (write) { writing++; writes++; }
  let response;
  try {
    for (let attempt = 0; ; attempt++) {
      try {
        // A fresh deadline per attempt: an AbortSignal that has fired stays fired.
        response = await fetch(path, { ...options, signal: AbortSignal.timeout(REQUEST_DEADLINE) });
      } catch (error) {
        throw new Error(error?.name === "TimeoutError"
          ? "The workspace did not answer in time. Your change is still here \u2014 press Save to send it again."
          : "Cannot reach the app. Make sure it is running, then retry.");
      }
      if (write || attempt >= GATEWAY_WAITS.length || !GATEWAY.includes(response.status)) break;
      await new Promise((done) => setTimeout(done, GATEWAY_WAITS[attempt]));
    }
  } finally {
    if (write) writing--;
  }
  if (response.status === 401) {
    // The session ended on the hosted workspace, so collect a new one rather than losing the edit.
    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    throw new Error("Your session has ended. Redirecting you to sign in again.");
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    const error = new Error(detail.error || (GATEWAY.includes(response.status)
      ? "The app is restarting, so that did not reach the workspace. Your change is still here \u2014 press Save to send it again."
      : `Request failed (${response.status}).`));
    error.status = response.status;
    throw error;
  }
  return binary ? response : response.json();
}

async function refresh() {
  const data = await api("/api/state");
  state.data = data;
  if (!data.projects.some((p) => p["Project ID"] === state.project)) state.project = data.projects[0]?.["Project ID"] || "";
  if (state.specPending.size || state.specLinkEdit.size) {   // never hold an edit for a line item that no longer exists
    const live = new Set(data.selections.map((r) => r._row));
    for (const row of [...state.specPending.keys()]) if (!live.has(row)) state.specPending.delete(row);
    for (const row of [...state.specLinkEdit]) if (!live.has(row)) state.specLinkEdit.delete(row);
  }
  $("load-error").hidden = true;
  render();
}

// Every write carries the revision the page last saw, so the save that landed a moment ago -
// usually the previous cell or field this person edited - makes the next one conflict. That
// edit is still exactly what they asked for, so take the current revision and send it once
// more rather than showing them a race they did not cause. The check runs after the refresh:
// an edit whose row has since gone must not be reapplied blind.
async function persist(path, body, stillThere) {
  try {
    return await api(path, body);
  } catch (error) {
    if (error.status !== 409) throw error;
    await refresh();
    if (stillThere && !stillThere()) throw error;
    return await api(path, body);
  }
}

function setBusy(value, message = "") {
  state.busy = value;
  $("busy-banner").hidden = !value;
  $("busy-message").textContent = message;
  document.querySelectorAll("button:not(.close-dialog):not(.sidebar-toggle)").forEach((button) => { button.disabled = value; });
  specSaveUI();
  if (!value && state.data) render();
}

async function action(fn) {
  if (state.busy) return;
  try { await fn(); } catch (error) { toast(error.message, true); }
}

function thumb(row) {
  if (isUrl(row["Image URL"])) return `<img class="item-thumb" src="${esc(row["Image URL"])}" alt="${esc(row["Item"])}" loading="lazy" referrerpolicy="no-referrer">`;
  return '<span class="item-thumb item-placeholder" aria-hidden="true">◇</span>';
}

function detailValue(value) {
  if (!value) return '<span class="muted">Not provided</span>';
  return isUrl(value) ? `<a href="${esc(value)}" target="_blank" rel="noopener noreferrer">${esc(value)}</a>` : esc(value);
}

function selectionDetails(row) {
  const fields = row.custom_fields || [];
  if (!fields.length) return "";
  return `<details class="selection-details"><summary>${fields.length} additional field${fields.length === 1 ? "" : "s"}</summary><dl>${fields.map((field) => `<div><dt>${esc(field.name)}</dt><dd>${detailValue(field.value)}</dd></div>`).join("")}</dl></details>`;
}

function render() {
  if (specDrag || specResize) { specRenderPending = true; return; }   // never tear down rows mid-drag
  const data = state.data;
  if (!data) return;
  const project = selectedProject(), rows = projectRows();
  const linked = rows.filter((r) => isUrl(r["Product URL"])).length;
  const bare = state.view === "spec" || state.view === "catalog";   // these views stand on their own, apart from any one project
  $("project-list").innerHTML = data.projects.length ? data.projects.map((p) => `<button class="project-choice ${p["Project ID"] === state.project ? "selected" : ""}" data-project="${esc(p["Project ID"])}"><span><strong>${esc(p["Project Name"])}</strong><small>${esc(p["Project ID"])}</small></span></button>`).join("") : '<p class="muted">Add a project to get started.</p>';
  $("project-summary").hidden = !project || state.view === "manufacturers" || bare;
  $("stats").hidden = !project || state.view === "manufacturers" || bare;
  $("project-details-panel").hidden = !project || state.view !== "selections";
  $("spec-client-panel").hidden = !project || state.view !== "spec";
  if (project) {
    document.querySelectorAll("#spec-client-panel [data-project-field]").forEach((input) => {
      if (document.activeElement === input) return;   // never clobber a keystroke in progress
      const value = project[input.dataset.projectField] || "";
      input.value = input.dataset.projectField === "Presentation Date" ? value.slice(0, 10) : value;
    });
  }
  if (project) {
    const standard = ["Client Name", "Address", "Plan / Elevation", "Designer", "Presentation Date", "Cover Image"];
    const custom = project.custom_fields || [];
    // Rebuilding the inputs on every poll would steal focus and reload the cover thumbnail, so only
    // repaint when the underlying values actually changed.
    const detailsSig = JSON.stringify([project["Project ID"], standard.map((n) => project[n] || ""), custom]);
    if (detailsSig !== state.detailsSig) {
      state.detailsSig = detailsSig;
      const entry = (label, control, isCustom) => `<div class="detail-entry ${isCustom ? "custom-detail" : ""}">${label}<dd>${control}</dd></div>`;
      $("project-detail-values").innerHTML = standard.map((name) => {
      const date = name === "Presentation Date";
      const value = date ? (project[name] || "").slice(0, 10) : project[name] || "";
      if (name === "Cover Image") {
        const source = coverSource(value);
        return entry(`<dt>${esc(name)}</dt>`,
          `<div class="cover-field"><input class="cell" data-project-field="${esc(name)}" type="text" value="${esc(value)}" maxlength="4000" placeholder="Image URL, or upload a file"><button type="button" class="text-button" data-cover-upload>Upload…</button></div>` +
          (source ? `<img class="cover-thumb" src="${esc(source)}" alt="Cover image preview" loading="lazy" referrerpolicy="no-referrer">` : ""));
      }
      return entry(`<dt>${esc(name)}</dt>`,
        `<input class="cell" data-project-field="${esc(name)}" type="${date ? "date" : "text"}" value="${esc(value)}" maxlength="4000" placeholder="Not provided">`);
    }).join("") + custom.map((field, index) => entry(
      `<dt><input class="cell cell-label" data-custom-index="${index}" data-custom-key="name" value="${esc(field.name)}" maxlength="80" aria-label="Field name"></dt>`,
      `<input class="cell" data-custom-index="${index}" data-custom-key="value" value="${esc(field.value)}" maxlength="2000" placeholder="Not provided" aria-label="${esc(field.name)}">`, true)).join("");
    }
    $("project-custom-empty").hidden = custom.length > 0;
  } else {
    state.detailsSig = null;
  }
  const emblem = $("project-emblem");
  const emblemSrc = coverSource(project?.["Cover Image"]);
  EMBLEM_SVG ||= emblem.innerHTML;   // the house line-art, kept for projects with no usable image
  emblem.classList.toggle("has-cover", !!emblemSrc);
  emblem.innerHTML = emblemSrc ? `<img src="${esc(emblemSrc)}" alt="Cover image">` : EMBLEM_SVG;
  if (project) {
    $("project-id").textContent = project["Project ID"];
    $("project-name").textContent = project["Project Name"];
    $("project-address").textContent = project["Address"] || "Add an address in project details";
    $("project-date").textContent = project["Presentation Date"] ? `Presentation · ${project["Presentation Date"].slice(0, 10)}` : "Presentation date not set";
    $("project-client").textContent = [project["Client Name"] ? `Prepared for ${project["Client Name"]}` : "", project["Plan / Elevation"]].filter(Boolean).join("   /   ");
  }
  const priced = rows.filter((r) => Number(r["Unit Price"]) > 0);
  const budget = priced.reduce((sum, r) => sum + lineTotal(r), 0);
  $("stats").innerHTML = [["Total selections", rows.length, `${new Set(rows.map((r) => r.Section || "Other")).size} categories`], ["Product links", linked, `of ${rows.length} with a product page`], ["Priced total", budget.toLocaleString(undefined, { maximumFractionDigits: 0 }), `${priced.length} of ${rows.length} priced`]].map(([label, value, note]) => `<div class="stat"><div class="stat-label">${label}</div><div class="stat-value"><strong>${value}</strong><span>${note}</span></div></div>`).join("");
  $("page-title").textContent = names[state.view];
  // the spec sheet is the standard specification, so it is not labelled with one project
  $("breadcrumb-name").textContent = project && !["manufacturers", "spec", "catalog"].includes(state.view)
    ? `${project["Project Name"]} / ${names[state.view]}` : names[state.view];
  $("page-subtitle").textContent = { selections: "Every material. Every finish. All in one place.", spec: "The full specification, grouped the way you work.", manufacturers: "Keep your trusted brands and official websites together.", catalog: "Everything you have clipped, beautifully organized." }[state.view];
  // the spec sheet edits the same rows the exports print, so name the home it belongs to
  if (state.view === "spec" && project) $("page-subtitle").textContent = `Specifying ${project["Project Name"]}`;
  // the catalog section carries its own heading, so the page banner would say it twice
  document.querySelector(".page-heading").hidden = state.view === "catalog";
  document.querySelectorAll("[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === state.view));
  document.querySelectorAll(".view").forEach((view) => { view.hidden = view.id !== `${state.view}-view`; });
  $("add-selection").disabled = !project || state.busy;
  const oldSection = $("section-filter").value, oldLocation = $("location-filter").value;
  $("section-filter").innerHTML = '<option value="">All categories</option>' + [...new Set([...data.sections, ...rows.map((r) => r.Section).filter(Boolean)])].map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  $("section-filter").value = oldSection;
  $("location-filter").innerHTML = '<option value="">All locations</option>' + [...new Set([...(data.locations || []), ...rows.map((r) => r.Location).filter(Boolean)])].map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  $("location-filter").value = oldLocation;
  renderTable();
  renderSpec();
  renderCatalog();
  $("manufacturer-grid").innerHTML = data.manufacturers.map((m, index) => `<article class="manufacturer-card"><h3>${esc(m.Manufacturer)}</h3><p>${esc(m["Official Domain"])}</p><p>${esc(m.Notes || "Official product source")}</p><button class="text-button" data-manufacturer="${index}">Edit manufacturer ↗</button></article>`).join("") || '<div class="empty-state"><h3>Add your first manufacturer</h3><p>Enter the brand and its official website domain.</p></div>';
  attachImageErrors();
  restoreFocus();
}

function renderTable() {
  const query = $("search").value.trim().toLowerCase(), category = $("section-filter").value, location = $("location-filter").value;
  const rows = projectRows().filter((r) => (!category || r.Section === category) && (!location || r.Location === location) && (!query || [r.Item, r.Manufacturer, r["Model #"], r["Room / Area"], r["Finish / Color"], r.Location, ...(r.custom_fields || []).flatMap((field) => [field.name, field.value])].join(" ").toLowerCase().includes(query)));
  state.page = Math.min(state.page, Math.max(0, Math.ceil(rows.length / 50) - 1));
  const start = state.page * 50;
  $("selection-rows").innerHTML = rows.slice(start, start + 50).map((r) => `<tr><td><div class="item-cell">${thumb(r)}<div><strong>${esc(r.Item || "Untitled selection")}</strong><small class="item-meta">${r.Location ? `<span class="meta-chip meta-location">${esc(r.Location)}</span>` : ""}${r.Section ? `<span class="meta-chip meta-section">${esc(r.Section)}</span>` : ""}${r["Room / Area"] ? `<span class="meta-room">${esc(r["Room / Area"])}</span>` : ""}${r["Include in Lookbook"].toLowerCase() === "no" ? '<span class="meta-room">· Hidden</span>' : ""}</small>${selectionDetails(r)}</div></div></td><td><strong>${esc(r.Manufacturer || "Manufacturer pending")}</strong><small>${esc(r["Model #"] || "Model pending")}</small></td><td><strong>${esc(r["Finish / Color"] || "—")}</strong><small>${qtyUnit(r) ? `Qty: ${esc(qtyUnit(r))}` : "Quantity not set"}</small></td><td>${isUrl(r["Product URL"]) ? `<a class="product-link" href="${esc(fixUrl(r["Product URL"]))}" target="_blank" rel="noopener noreferrer" title="${esc(fixUrl(r["Product URL"]))}">Link</a>` : '<span class="muted">Not linked</span>'}</td><td>${r["Client Status"] ? `<span class="status">${esc(r["Client Status"])}</span>` : ""}</td><td><button class="row-action" data-edit="${r._row}" aria-label="Edit ${esc(r.Item)}">Edit ↗</button></td></tr>`).join("");
  $("table-empty").hidden = rows.length > 0;
  $("table-summary").textContent = rows.length ? `Showing ${start + 1}–${Math.min(start + 50, rows.length)} of ${rows.length} selections` : "0 selections";
  $("previous-page").disabled = !state.page || state.busy;
  $("next-page").disabled = start + 50 >= rows.length || state.busy;
  attachImageErrors();
}

// A quantity means nothing without what it counts, so the spec sheet, its downloads and the
// editor show the two as one column. They stay two workspace columns underneath — Qty is the
// number the Price Schedule multiplies — so the box is split on its leading number when saved.
// common.py holds the same pair of helpers for the exports and the CSV import.
const QTY_UNIT = "Qty / Unit";
const QTY_UNIT_KEYS = ["Qty", "Unit"];
const QTY_UNIT_RE = /^(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d*\.?\d+)?\s*([\s\S]*)$/;
const qtyUnit = (row) => QTY_UNIT_KEYS.map((key) => String(row[key] ?? "").trim()).filter(Boolean).join(" ");
const splitQtyUnit = (text) => {
  const [, number = "", unit = ""] = QTY_UNIT_RE.exec(String(text).trim());
  return { Qty: number.replace(/,/g, ""), Unit: unit.trim() };
};
// The workspace fields a spec-sheet cell covers, and what it saves to each of them.
const cellFields = (field) => (field === QTY_UNIT ? QTY_UNIT_KEYS : [field]);
const cellValues = (field, text) => (field === QTY_UNIT ? splitQtyUnit(text) : { [field]: text });

const SPEC_COLUMNS = [
  { field: "Item", label: "ITEM", placeholder: "Name the selection", width: 200 },
  { field: "Manufacturer", label: "BRAND", placeholder: "Brand", width: 125 },
  { field: "Model #", label: "MODEL", placeholder: "Model / SKU", width: 115 },
  { field: "Finish / Color", label: "FINISH / COLOR", placeholder: "Finish", width: 140 },
  // the product's dimensions or configuration, between the finish and its link as on the sheet
  { field: "Size / Configuration", label: "SIZE / CONFIG", placeholder: "Size / config", width: 130 },
  // the product URL is stored as it always was; this column is where it is pasted and read,
  // headed LINK over a one-word Link anchor just like the printed sheet
  { field: "Product URL", label: "LINK", placeholder: "Paste product URL", link: true, width: 120 },
  // the quantity and what it counts, in one box: 7571 means nothing until it says sq ft.
  // They stay two workspace columns, so the cell splits on its leading number when it is saved.
  { field: QTY_UNIT, label: "QTY / UNIT", placeholder: "1 EA", pair: QTY_UNIT_KEYS, width: 118 },
  { field: "Unit Price", label: "UNIT PRICE", placeholder: "—", number: true, width: 100 },
  { field: "Markup %", label: "MARKUP %", placeholder: "0", number: true, width: 100 },
];

// A brand, model or finish is often longer than the column it lands in, so every header carries
// a grip that sets that column's width: dragged once and then kept, the same in every group card,
// through every re-render and after a reload. The width is how one person reads the sheet rather
// than anything about the home, so it is remembered in the browser - like the sidebar - and never
// written to the workspace. The table is laid out fixed, so a width that has been set is the
// width that is used; whatever is left over past the last column goes to the actions cell.
const SPEC_WIDTH_KEY = "uh-spec-widths";
const SPEC_WIDTH_MIN = 56, SPEC_WIDTH_MAX = 680, SPEC_WIDTH_STEP = 12;
const SPEC_DRAG_W = 26, SPEC_ACTIONS_W = 104;   // the reorder handle and the actions are not resizable
const specClamp = (value) => Math.min(SPEC_WIDTH_MAX, Math.max(SPEC_WIDTH_MIN, Math.round(value)));
const specDefault = (field) => SPEC_COLUMNS.find((c) => c.field === field)?.width || SPEC_WIDTH_MIN;

function loadSpecWidths() {
  let stored = {};
  try { stored = JSON.parse(localStorage.getItem(SPEC_WIDTH_KEY)) || {}; } catch {}
  return new Map(SPEC_COLUMNS.map((c) => [c.field, Number(stored[c.field]) ? specClamp(stored[c.field]) : c.width]));
}

const specWidths = loadSpecWidths();
const specWidth = (field) => specWidths.get(field) || specDefault(field);
const specTableWidth = () => SPEC_DRAG_W + SPEC_ACTIONS_W + SPEC_COLUMNS.reduce((sum, c) => sum + specWidth(c.field), 0);
// The columns are declared bare and sized afterwards: the app serves no inline styles (its
// Content-Security-Policy allows none), and the CSSOM is also what a drag needs - it writes the
// new width onto the tables already on screen rather than tearing down the rows being sized.
const specColgroup = () => `<colgroup><col class="drag-col">${SPEC_COLUMNS.map((c) =>
  `<col data-col="${esc(c.field)}">`).join("")}<col></colgroup>`;

function applySpecWidths() {
  const total = `${specTableWidth()}px`;
  document.querySelectorAll(".spec-table").forEach((table) => {
    table.style.minWidth = total;
    table.querySelectorAll("col[data-col]").forEach((col) => { col.style.width = `${specWidth(col.dataset.col)}px`; });
  });
}

function setSpecWidth(field, width) {
  specWidths.set(field, specClamp(width));
  applySpecWidths();
  try { localStorage.setItem(SPEC_WIDTH_KEY, JSON.stringify(Object.fromEntries(specWidths))); } catch {}
}

const money = (value) => value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const lineTotal = (row) => (Number(row["Unit Price"]) || 0) * (Number(row.Qty) || 1) * (1 + (Number(row["Markup %"]) || 0) / 100);

function specGroups(rows) {
  const key = state.specGroup, placeholder = specPlaceholder();
  const groups = new Map();
  for (const row of rows) groups.set(row[key] || placeholder, []);
  for (const row of rows) groups.get(row[key] || placeholder).push(row);
  // A heading held in the browser has no line items yet, so it joins the sheet as an empty group;
  // once a line item names it, the workspace is what puts it on screen. A card somebody dragged
  // into place remembers the heading it follows - null says it leads the sheet - and a held
  // heading with no memory lands where it always did, after the last card.
  const keys = [...groups.keys()];
  const slots = new Map();
  for (const held of heldGroups()) {
    if (groups.has(held.name)) continue;
    let at;
    if (held.after === null) at = slots.get(null) ?? 0;
    else if (held.after === undefined) at = keys.length;
    else {
      const anchor = keys.indexOf(held.after);
      at = anchor < 0 ? keys.length : (slots.get(held.after) ?? anchor + 1);
    }
    keys.splice(at, 0, held.name);
    slots.set(held.after, at + 1);
    groups.set(held.name, []);
  }
  const ordered = keys.map((name) => [name, groups.get(name)]);
  // Categories print in the order the Lists sheet names them; rooms print in the floor-plan
  // order the Lists sheet's Rooms column names them. Unknown rooms sort after the listed ones,
  // keeping their line-item order among themselves.
  const order = key === "Section" ? (state.data.sections || []) : (state.data.rooms || []);
  if (!order.length) return ordered;
  // PROJECT (details band) always first; listed rooms in floor-plan order; unknown rooms last.
  const rank = (name) => isDetailsGroup(name) ? -1 : order.indexOf(name) >= 0 ? order.indexOf(name) : 9999;
  return ordered.sort((a, b) => rank(a[0]) - rank(b[0]));
}

// A room being named: the card is already on the sheet, with a box where its heading will be, so
// naming one reads exactly like renaming one. The box keeps the `cell` class, which is what holds
// the 2.5s poll off an input that has focus.
const namingCard = () => `<article class="spec-card held naming">
  <div class="spec-card-head"><input class="cell group-rename group-new" data-group-new="" value="" maxlength="200" autocomplete="off" placeholder="Name this ${groupLabel()}" aria-label="Name the new ${groupLabel()}"><span class="count">New</span></div>
  <div class="spec-body"><p class="spec-card-note">Type a name and press Enter. Escape leaves it out.</p></div>
</article>`;

// A heading has to work as the field's own value, so three names are refused rather than added:
// a blank one, the placeholder heading (a label for line items with nothing in that column) and
// a heading already on this sheet - nothing would be added in any of those cases.
function groupName(name, except) {
  const label = groupLabel();
  if (!name) return "";
  if (name === specPlaceholder()) {
    toast(`“${name}” is where line items with no ${label} sit, so it cannot name one.`, true);
    return "";
  }
  if (specGroups(projectRows()).some(([heading]) => heading === name && heading !== except)) {
    toast(`“${name}” is already on this sheet.`, true);
    return "";
  }
  return name;
}

function addSpecGroup(name) {
  state.specNaming = false;   // closed before anything re-renders, so a blur cannot reopen it
  const accepted = groupName(name);
  if (accepted) state.specHeld.push({ field: state.specGroup, project: state.project, name: accepted });
  renderSpec();
  if (accepted) toast(`“${accepted}” added at the end — drag its card to place it. Add a line item to keep this ${groupLabel()}.`);
}

// One figure in the project box: the item is the field's label and Size / Configuration its
// value, like the client details fields above; every other filled spec column follows as the
// small print - the same words the PDF and Word copies put in their box. Pending edits show
// in place like any spec cell, and the whole figure can still be dragged like a line item.
// The label is a one-row textarea rather than an input, so a long figure name wraps inside
// its field like the printed sheet's do instead of clipping at the column edge; the box
// stays one line tall until fitDetailName grows it to whatever the words need.
function specDetail(row) {
  const pending = state.specPending.get(row._row) || {};
  const merged = { ...row, ...pending };
  const dirty = (field) => cellFields(field).some((key) => key in pending);
  const extras = SPEC_COLUMNS.filter((c) => c.field !== "Item" && c.field !== "Size / Configuration")
    .map((c) => {
      if (c.link) return isUrl(merged[c.field]) ? `<a class="product-link" href="${esc(fixUrl(merged[c.field]))}" target="_blank" rel="noopener noreferrer" title="${esc(fixUrl(merged[c.field]))}">Link</a>` : "";
      const value = c.pair ? qtyUnit(merged) : String(merged[c.field] ?? "").trim();
      return value ? esc(value) : "";
    }).filter(Boolean).join(" · ");
  return `<div class="spec-detail" data-row="${row._row}" draggable="true">
    <div class="detail-head"><textarea class="cell detail-name${dirty("Item") ? " dirty" : ""}" data-row="${row._row}" data-field="Item" rows="1" maxlength="4000" aria-label="Item name" placeholder="Name the detail">${esc(merged.Item)}</textarea><span class="detail-actions"><button type="button" class="text-button" data-edit="${row._row}" aria-label="Open ${esc(row.Item)}">Open</button><button type="button" class="icon-button" data-spec-remove="${row._row}" aria-label="Remove ${esc(row.Item)}">×</button></span></div>
    <input class="cell detail-value${dirty("Size / Configuration") ? " dirty" : ""}" data-row="${row._row}" data-field="Size / Configuration" value="${esc(merged["Size / Configuration"])}" maxlength="4000" aria-label="Value for ${esc(merged.Item) || "the detail"}" placeholder="Value, e.g. 3,819 sq ft">
    ${extras ? `<div class="detail-extra">${extras}</div>` : ""}
  </div>`;
}

// A detail name's box is one row tall until its words need more. field-sizing does that
// itself where it is supported; elsewhere measure the wrapped text and write the height
// through the CSSOM, the same way applySpecWidths writes column widths.
function fitDetailName(name) {
  if (CSS.supports("field-sizing", "content")) return;
  name.style.height = "auto";
  name.style.height = `${name.scrollHeight}px`;
}

// If the focused element is a spec cell being typed into, capture its current value into
// specPending so a re-render does not lose keystrokes.  Returns a selector that restoreFocus
// can use to put the cursor back where it was.
function captureActiveSpecEdit() {
  const active = document.activeElement;
  if (!active?.classList?.contains("cell") || !active.closest("#spec-groups")) return null;
  const row = active.dataset.row, field = active.dataset.field;
  if (!row || !field) return null;
  holdSpecEdit(active);
  return `#spec-groups .cell[data-row="${row}"][data-field="${CSS.escape(field)}"]`;
}

function renderSpec() {
  if (specDrag || specResize) { specRenderPending = true; return; }   // never tear down rows mid-drag
  // Capture any in-progress edit before tearing down the inputs — a save that just finished
  // calls refresh → render → renderSpec, and the cell the person moved to is still mid-keystroke.
  // The active cell takes priority over the selector a completed save left behind: the person
  // already moved on, and putting focus back on the finished cell would steal it.
  const activeSel = captureActiveSpecEdit();
  if (activeSel) state.focusSelector = activeSel;
  document.querySelectorAll("#spec-view .chip").forEach((chip) => chip.classList.toggle("active", chip.dataset.group === state.specGroup));
  const query = $("spec-filter").value.trim().toLowerCase();
  const all = projectRows();
  // A held heading that line items now name belongs to the workspace, so stop holding it.
  const named = new Set(all.map((r) => r[state.specGroup] || specPlaceholder()));
  state.specHeld = state.specHeld.filter((held) =>
    !(held.field === state.specGroup && held.project === state.project && named.has(held.name)));
  const rows = all.filter((r) => !query || [r.Item, r.Manufacturer, r["Model #"], r["Finish / Color"], r["Size / Configuration"], r["Room / Area"], r.Section, r.Location, r["Client Notes"]].join(" ").toLowerCase().includes(query));
  const groups = specGroups(rows);
  const heading = state.specGroup === "Section"
    ? (groups.length === 1 ? "category" : "categories")
    : `room${groups.length === 1 ? "" : "s"}`;
  $("spec-summary").textContent = all.length
    ? `${groups.length} ${heading} · ${rows.length} line item${rows.length === 1 ? "" : "s"}${rows.length !== all.length ? ` of ${all.length}` : ""}`
    : "";
  $("spec-summary").hidden = state.view !== "spec" || !all.length;
  $("spec-add").disabled = !selectedProject() || state.busy;
  // The same button adds a room or a category, whichever the sheet is grouped by.
  $("spec-add-group").textContent = `+ Add ${groupLabel()}`;
  $("spec-add-group").disabled = !selectedProject() || state.busy;
  if (!all.length && !heldGroups().length && !state.specNaming) {
    $("spec-groups").innerHTML = '<div class="empty-state"><div class="empty-icon">▤</div><h3>Nothing specified yet</h3><p>Add your first line item to start building this home’s specification.</p></div>';
    $("spec-total").textContent = "—";
    specSaveUI();
    return;
  }
  $("spec-groups").innerHTML = groups.map(([name, items]) => {
    const total = items.reduce((sum, r) => sum + lineTotal(r), 0);
    const collapsed = state.specCollapsed.has(name);
    const details = items.length > 0 && isDetailsGroup(name);
    const body = details ? "" : items.map((r) => `<tr data-row="${r._row}" draggable="true"><td class="drag-col"><span class="drag-handle" title="Drag row to reorder" aria-hidden="true">⠿</span></td>${SPEC_COLUMNS.map((c) => {
      const pending = state.specPending.get(r._row) || {};   // unsaved edits outlive every re-render
      const edited = cellFields(c.field).some((f) => f in pending);
      const value = c.pair ? qtyUnit({ ...r, ...pending }) : String(edited ? pending[c.field] : r[c.field] ?? "");
      // a saved address shows the word "Link", never the raw URL; the pencil reopens the input
      if (c.link && isUrl(value) && !state.specLinkEdit.has(r._row)) {
        return `<td class="link-col"><div class="link-cell${edited ? " dirty" : ""}"><a class="product-link" href="${esc(value)}" target="_blank" rel="noopener noreferrer" title="${esc(value)}">Link</a><button type="button" class="icon-button link-edit" data-link-edit="${r._row}" aria-label="View or change the product link for ${esc(r.Item)}" title="View or change the address">✎</button></div></td>`;
      }
      // the category a line item was filed under reads under its name, like the spec sheet;
      // when the view is already grouped by it, the card's heading says it instead.
      // For bulk/Entire Home groups, the category shows beside the item (side-by-side)
      // instead of below, matching the Excel Bulk Items layout.
      const section = c.field === "Item" && state.specGroup !== "Section" && String(r.Section || "").trim();
      const bulk = section && isBulkGroup(name);
      return `<td${c.number ? ' class="num"' : c.link ? ' class="link-col"' : ""}>${bulk ? `<span class="bulk-cat">${esc(section)}</span>` : ""}<input class="cell${c.number ? " num" : ""}${edited ? " dirty" : ""}" data-row="${r._row}" data-field="${esc(c.field)}" value="${esc(value)}" placeholder="${esc(c.placeholder)}"${c.number ? ' type="number" min="0" step="any"' : ` maxlength="4000"`}>${section && !bulk ? `<div class="cell-sub">${esc(section)}</div>` : ""}</td>`;
    }).join("")}<td><div class="spec-actions"><button class="text-button" data-edit="${r._row}" aria-label="Open ${esc(r.Item)}">Open</button><button class="icon-button" data-spec-remove="${r._row}" aria-label="Remove ${esc(r.Item)}">×</button></div></td></tr>`).join("");
    // The heading is the grouped field's own value, so editing it renames the room or category
    // on every line item under it. While it is being edited the toggle gives way to the box:
    // an input cannot live inside a button, and collapsing is not what this click means.
    const renaming = state.specRename === name;
    const label = groupLabel();
    // Room order is line-item order, so a room card can be dragged into place; categories
    // follow the Lists sheet and their order cannot be moved here.
    const grip = state.specGroup === "Section" ? ""
      : `<span class="drag-handle card-grip" draggable="true" title="Drag to reorder this ${label}" aria-hidden="true">⠿</span>`;
    const head = renaming
      ? `<input class="cell group-rename" data-group-rename="${esc(name)}" value="${esc(name)}" maxlength="200" aria-label="Rename the ${label} ${esc(name)}">`
      : `<button type="button" class="spec-card-toggle" data-spec-toggle="${esc(name)}" aria-expanded="${!collapsed}"><span class="twist" aria-hidden="true">▾</span><h3>${esc(name)}</h3></button>`;
    // Only a heading added from the toolbar has no line items, so an empty card is one still
    // held in this browser: it says so in place of the table, and its × stops holding it.
    const held = !items.length;
    return `<article class="spec-card${collapsed ? " collapsed" : ""}${held ? " held" : ""}${details ? " details" : ""}" data-spec-name="${esc(name)}">
      <div class="spec-card-head">${grip}${head}<span class="count">${items.length} item${items.length === 1 ? "" : "s"}</span><button type="button" class="icon-button spec-rename" data-spec-rename="${esc(name)}" aria-label="Rename the ${label} ${esc(name)}" title="Rename this ${label}">✎</button>${held ? `<button type="button" class="icon-button spec-discard" data-spec-discard="${esc(name)}" aria-label="Remove the empty ${label} ${esc(name)}" title="Remove this empty ${label}">×</button>` : ""}<span class="group-total">${total ? money(total) : ""}</span></div>
      <div class="spec-body">${held
        ? `<p class="spec-card-note">Nothing in this ${label} yet, so it is only on this screen. Drag the card by its grip to place it, then add a line item below — or drag one in from another card — and the workspace keeps it there.</p>`
        : details
          ? `<div class="spec-details-box">${items.map(specDetail).join("")}</div>`
          : `<table class="spec-table">${specColgroup()}<thead><tr><th class="drag-col"><span class="sr-only">Reorder</span></th>${SPEC_COLUMNS.map((c) => `<th${c.number ? ' class="num"' : ""}><span class="th-label" title="${esc(c.label)}">${c.label}</span><button type="button" class="col-grip" data-grip="${esc(c.field)}" title="Drag to set the width of this column, double-click to reset it" aria-label="Set the width of the ${esc(c.label)} column"></button></th>`).join("")}<th><span class="sr-only">Actions</span></th></tr></thead><tbody>${body}</tbody></table>`}</div>
      <div class="spec-foot"><button class="text-button" data-spec-add="${esc(name)}">+ Add to ${esc(name)}</button></div>
    </article>`;
  }).join("") + (state.specNaming ? namingCard() : "");
  applySpecWidths();
  document.querySelectorAll("#spec-groups .detail-name").forEach(fitDetailName);
  const grand = rows.reduce((sum, r) => sum + lineTotal(r), 0);
  $("spec-total").textContent = grand ? money(grand) : "—";
  specSaveUI();
  // Put the cursor back on the cell the person was typing in, so a save that just finished
  // does not steal focus from the next cell.
  if (activeSel) restoreFocus();
}

let specDrag = null, specPress = null, specRenderPending = false, specResize = null;

function specResizeEvents() {
  const zone = $("spec-groups");
  zone.addEventListener("pointerdown", (event) => {
    const grip = event.target.closest(".col-grip");
    if (!grip || event.button) return;
    event.preventDefault();   // no text selection, and no row drag starting underneath the grip
    grip.focus({ preventScroll: true });   // so the arrow keys can finish what the pointer started
    const field = grip.dataset.grip, fromX = event.clientX, fromW = specWidth(field);
    specResize = field;
    grip.setPointerCapture(event.pointerId);
    grip.classList.add("resizing");
    document.body.classList.add("resizing-column");
    const drag = (moved) => setSpecWidth(field, fromW + moved.clientX - fromX);
    const release = () => {
      grip.removeEventListener("pointermove", drag);
      grip.classList.remove("resizing");
      document.body.classList.remove("resizing-column");
      specResize = null;
      if (specRenderPending) { specRenderPending = false; render(); }
    };
    grip.addEventListener("pointermove", drag);
    grip.addEventListener("pointerup", release, { once: true });
    grip.addEventListener("pointercancel", release, { once: true });
  });
  // The grip's own double-click is the only way back to the width the column opened with.
  zone.addEventListener("dblclick", (event) => {
    const grip = event.target.closest(".col-grip");
    if (grip) setSpecWidth(grip.dataset.grip, specDefault(grip.dataset.grip));
  });
  zone.addEventListener("keydown", (event) => {
    const grip = event.target.closest?.(".col-grip");
    if (!grip || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const step = SPEC_WIDTH_STEP * (event.key === "ArrowRight" ? 1 : -1);
    setSpecWidth(grip.dataset.grip, specWidth(grip.dataset.grip) + step);
  });
}

function clearDropMarks() {
  document.querySelectorAll(".drop-before,.drop-after,.drop-end,.dragging").forEach((el) => el.classList.remove("drop-before", "drop-after", "drop-end", "dragging"));
}

function specDragEvents() {
  const zone = $("spec-groups");
  zone.addEventListener("mousedown", (event) => { specPress = event.target; }, true);
  zone.addEventListener("dragstart", (event) => {
    const grip = event.target.closest ? event.target.closest(".card-grip") : null;
    if (grip) {   // a card's grip moves the whole room, not a line item
      const card = grip.closest(".spec-card");
      if (state.busy || !card) { event.preventDefault(); return; }
      specDrag = { card: card.dataset.specName };
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", specDrag.card);
      requestAnimationFrame(() => card.classList.add("dragging"));
      return;
    }
    // Text selected in a cell must never become a drag: the browser would carry it into
    // whichever cell it was let go over and splice it into that value — "30 in" dropped on
    // became "30 iStainless steel, transitionaln" — and the cell saves what it is left with.
    if (event.target.closest?.(".cell")) { event.preventDefault(); return; }
    const rowEl = event.target;
    if (!rowEl.matches || !rowEl.matches("tr[data-row],.spec-detail[data-row]")) return;   // any other child drag
    if (state.busy || (specPress && specPress.closest("input,textarea,select,button,a"))) {
      event.preventDefault();   // presses on cells/buttons keep their normal behavior
      return;
    }
    specDrag = { row: Number(rowEl.dataset.row), group: rowEl.closest(".spec-card")?.dataset.specName };
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(specDrag.row));
    requestAnimationFrame(() => rowEl.classList.add("dragging"));
  });
  zone.addEventListener("dragover", (event) => {
    if (!specDrag) return;
    if (specDrag.card !== undefined) {
      const over = event.target.closest(".spec-card[data-spec-name]");
      if (!over || over.dataset.specName === specDrag.card) { clearDropMarks(); return; }
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      clearDropMarks();
      const rect = over.getBoundingClientRect();
      over.classList.add(event.clientY < rect.top + rect.height / 2 ? "drop-before" : "drop-after");
      return;
    }
    const card = event.target.closest(".spec-card");
    if (!card) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    clearDropMarks();
    const tr = event.target.closest("tr[data-row],.spec-detail[data-row]");
    if (tr && Number(tr.dataset.row) !== specDrag.row) {
      const rect = tr.getBoundingClientRect();
      tr.classList.add(event.clientY < rect.top + rect.height / 2 ? "drop-before" : "drop-after");
    } else if (!tr) {
      card.classList.add("drop-end");
    }
  });
  zone.addEventListener("drop", (event) => {
    if (!specDrag) return;
    if (specDrag.card !== undefined) {
      const over = event.target.closest(".spec-card[data-spec-name]");
      if (over) event.preventDefault();
      const after = over ? event.clientY >= over.getBoundingClientRect().top + over.getBoundingClientRect().height / 2 : false;
      finishGroupDrop(over ? over.dataset.specName : null, after);
      return;
    }
    const card = event.target.closest(".spec-card");
    if (card) event.preventDefault();
    const tr = event.target.closest("tr[data-row],.spec-detail[data-row]");
    if (tr && Number(tr.dataset.row) === specDrag.row) { specDrag = null; clearDropMarks(); return; }
    const after = tr ? event.clientY >= tr.getBoundingClientRect().top + tr.getBoundingClientRect().height / 2 : false;
    finishSpecDrop(tr ? Number(tr.dataset.row) : null, after, card);
  });
  zone.addEventListener("dragend", () => {
    specDrag = null;
    specPress = null;
    clearDropMarks();
    if (specRenderPending) { specRenderPending = false; render(); }
  });
}

function finishSpecDrop(target, after, card) {
  const from = specDrag?.row;
  specDrag = null;
  clearDropMarks();
  if (!from) return;
  if (state.busy) { toast("Wait for the current save to finish, then drag again.", true); return; }
  const all = projectRows().map((r) => r._row);
  const order = all.filter((r) => r !== from);
  let index;
  if (target === null) {   // dropped on the group card: land after its last visible item
    const rows = card?.querySelectorAll("tr[data-row],.spec-detail[data-row]");
    const last = rows?.length ? rows[rows.length - 1] : null;
    if (last) index = order.indexOf(Number(last.dataset.row)) + 1;
    else {
      // An empty card still has a place: the row lands just past the heading it follows.
      const held = heldGroups().find((h) => h.name === card?.dataset.specName);
      index = held ? dropIndexFor(held, order) : order.length;
    }
  } else {
    index = order.indexOf(target) + (after ? 1 : 0);
    if (index < 0) index = order.length;
  }
  order.splice(index, 0, from);
  const updates = {};
  const targetGroup = card?.dataset.specName;
  if (targetGroup && state.data) {
    const dragged = projectRows().find((r) => r._row === from);
    const current = dragged?.[state.specGroup] || specPlaceholder();
    if (targetGroup !== current) {
      updates[from] = { [state.specGroup]: targetGroup === specPlaceholder() ? "" : targetGroup };
    }
    // Dropping onto an empty card makes it real where it sits, so the held headings that
    // were keeping its place follow the group the row just made.
    const held = heldGroups().find((h) => h.name === targetGroup);
    if (held) reanchorHeld(held);
  }
  if (!Object.keys(updates).length && order.every((row, i) => row === all[i])) return;
  // Land the row now: the server round-trip on a hosted workspace takes a moment,
  // and nobody wants to watch the row hang while it saves.
  const byRow = new Map(projectRows().map((r) => [r._row, r]));
  const positions = all.slice().sort((a, b) => a - b);
  const next = new Map(state.data.selections.map((r) => [r._row, r]));
  positions.forEach((pos, i) => next.set(pos, byRow.get(order[i])));
  state.data = { ...state.data, selections: [...next.values()] };
  for (const [row, values] of Object.entries(updates)) {
    const record = state.data.selections.find((r) => r._row === Number(row));
    if (record) Object.assign(record, values);
  }
  renderSpec();
  action(async () => {
    const payload = { project: state.project, order, updates };
    try {
      // The same swap stays valid against a newer revision, so persist retries it for us.
      await persist("/api/selections/order", payload);
    } catch (error) {
      await refresh().catch(() => {});
      throw error;
    }
    remapPendingSpec(order, all);
    await refresh();
    toast("Line item moved.");
  });
}

function remapPendingSpec(order, all) {
  // The server moves each row's contents to sorted position i of order[i], so unsaved
  // edits must follow their selection instead of staying on a row number someone else now owns.
  if (!state.specPending.size && !state.specLinkEdit.size) return;
  const positions = [...all].sort((a, b) => a - b);
  const moved = new Map();
  for (const [row, values] of state.specPending) {
    const index = order.indexOf(row);
    moved.set(index < 0 ? row : positions[index], values);
  }
  state.specPending = moved;
  state.specLinkEdit = new Set([...state.specLinkEdit].map((row) => {
    const index = order.indexOf(row);
    return index < 0 ? row : positions[index];
  }));
}

// The row position a held heading's place works out to: just past the last line item of the
// heading it follows, the top of the sheet when it leads, and the end when it was never placed.
function dropIndexFor(held, order) {
  if (held.after === undefined) return order.length;
  if (held.after === null) return 0;
  const rows = new Map(projectRows().map((r) => [r._row, r]));
  const anchorLast = [...order].reverse().find((rn) =>
    (rows.get(rn)?.[held.field] || (held.field === "Section" ? "Other" : "Unassigned")) === held.after);
  return anchorLast === undefined ? order.length : order.indexOf(anchorLast) + 1;
}

// A held heading becomes real the moment a line item names it: the held headings that
// followed it in the same place now follow the group itself, or the order jumps.
function reanchorHeld(held) {
  const run = state.specHeld.filter((h) =>
    h.field === held.field && h.project === held.project && h.after === held.after);
  for (const h of run.slice(run.indexOf(held) + 1)) h.after = held.name;
}

function finishGroupDrop(targetName, after) {
  const name = specDrag?.card;
  specDrag = null;
  clearDropMarks();
  if (!name) return;
  if (state.busy) { toast("Wait for the current save to finish, then drag again.", true); return; }
  const cards = specGroups(projectRows()).map(([groupName]) => groupName);
  const order = cards.filter((n) => n !== name);
  let index = targetName ? order.indexOf(targetName) + (after ? 1 : 0) : order.length;
  if (index < 0) index = order.length;
  order.splice(index, 0, name);
  if (order.every((n, i) => n === cards[i])) return;
  // A real card moves as a block of its line items; a held card has none, so it remembers
  // the heading it now follows - null leads the sheet - and the held run keeps the same
  // left-to-right reading it was dropped in.
  const held = new Map(heldGroups().map((h) => [h.name, h]));
  const real = new Set(projectRows().map((r) => r[state.specGroup] || specPlaceholder()));
  const sequence = [], groupOrder = [];
  let lastReal = null;
  for (const n of order) {
    const h = real.has(n) ? null : held.get(n);
    if (h) { h.after = lastReal; sequence.push(h); }
    else { lastReal = n; groupOrder.push(n); }
  }
  state.specHeld = state.specHeld
    .filter((h) => !(h.field === state.specGroup && h.project === state.project))
    .concat(sequence);
  const all = projectRows().map((r) => r._row);
  const block = new Map(groupOrder.map((g) => [g, []]));
  for (const r of projectRows()) {
    const g = r[state.specGroup] || specPlaceholder();
    if (block.has(g)) block.get(g).push(r._row);
  }
  const moved = groupOrder.flatMap((g) => block.get(g));
  if (all.some((r) => !moved.includes(r))) { renderSpec(); return; }
  if (moved.every((r, i) => r === all[i])) { renderSpec(); return; }   // only held cards moved
  // Land the cards now, like a dragged line item: the workspace round-trip takes a moment.
  const byRow = new Map(projectRows().map((r) => [r._row, r]));
  const positions = all.slice().sort((a, b) => a - b);
  const next = new Map(state.data.selections.map((r) => [r._row, r]));
  positions.forEach((pos, i) => next.set(pos, byRow.get(moved[i])));
  state.data = { ...state.data, selections: [...next.values()] };
  renderSpec();
  const label = groupLabel();
  action(async () => {
    try {
      // The same swap stays valid against a newer revision, so persist retries it for us.
      await persist("/api/selections/order", { project: state.project, order: moved });
    } catch (error) {
      await refresh().catch(() => {});
      throw error;
    }
    remapPendingSpec(moved, all);
    await refresh();
    toast(`${label[0].toUpperCase()}${label.slice(1)} moved.`);
  });
}

// The editor appends a new line item at the end of the sheet, so a room that was dragged
// into place while it was empty would jump to the foot the moment its first line item
// landed. The new row moves to the place the empty card was keeping instead.
async function placeNewSpecRow(row, held) {
  if (held.after === undefined) return;
  const all = projectRows().map((r) => r._row);
  const order = all.filter((r) => r !== row);
  const index = dropIndexFor(held, order);
  if (index === order.length) return;   // it already landed where the card was keeping its place
  order.splice(index, 0, row);
  await persist("/api/selections/order", { project: state.project, order });
}

async function renameSpecGroup(from, to) {
  const field = state.specGroup;
  const belongs = (row) => (row[field] || specPlaceholder()) === from;
  const rows = projectRows().filter(belongs);
  state.specRename = null;   // closed before anything awaits, so a blur cannot reopen it
  // A card with no line items is still held in this browser, so its heading is renamed here
  // instead of on rows there are none of.
  const held = !rows.length && heldGroups().find((g) => g.name === from);
  if (held) {
    const accepted = to === from ? "" : groupName(to, from);
    if (accepted) held.name = accepted;
    renderSpec();
    return;
  }
  if (!to || to === from || !rows.length) { renderSpec(); return; }
  // Typing an existing heading moves these line items into that group, which is the same thing
  // dragging a row between cards does - but it is not obvious from a renamed heading, so ask.
  if (projectRows().some((row) => (row[field] || specPlaceholder()) === to)) {
    const ask = `“${to}” already exists. Move ${rows.length} line item${rows.length === 1 ? "" : "s"} into it?`;
    if (!confirm(ask)) { renderSpec(); return; }
  }
  // The placeholder heading is a label for rows with nothing in that column, never a value.
  const value = to === specPlaceholder() ? "" : to;
  // Held cards anchored to this heading follow it to its new name.
  state.specHeld.forEach((h) => { if (h.field === field && h.project === state.project && h.after === from) h.after = to; });
  const batch = Object.fromEntries(rows.map((row) => [row._row, { [field]: value }]));
  setBusy(true, `Renaming ${from}…`);
  try {
    await persist("/api/selections", { batch },
                  () => rows.every((row) => projectRows().some((live) => live._row === row._row)));
    await refresh();
    toast(`${rows.length} line item${rows.length === 1 ? "" : "s"} now under “${to}”.`);
  } finally { setBusy(false); }
}

function restoreFocus() {
  if (!state.focusSelector) return;
  const cell = document.querySelector(state.focusSelector);
  state.focusSelector = null;
  if (!cell) return;
  cell.focus();
  if (["text", "search", "url", "tel", "password", "textarea"].includes(cell.type)) {
    cell.setSelectionRange(cell.value.length, cell.value.length);
  }
}

async function saveProjectField(input) {
  const project = selectedProject();
  if (!project) return;
  const field = input.dataset.projectField, value = input.value.trim();
  const before = field === "Presentation Date" ? (project[field] || "").slice(0, 10) : project[field] || "";
  if (before === value) return;
  state.focusSelector = `[data-project-field="${CSS.escape(field)}"]`;
  // the Project ID travels with every edit so the server updates this row instead of adding one
  await persist("/api/projects", { values: { "Project ID": project["Project ID"], [field]: value } },
                () => !!selectedProject());
  await refresh();
  toast("Saved.");
}

async function saveProjectCustom(input) {
  const project = selectedProject();
  if (!project) return;
  const index = Number(input.dataset.customIndex), key = input.dataset.customKey;
  const fields = (project.custom_fields || []).map((field) => ({ ...field }));
  if (!fields[index] || fields[index][key] === input.value.trim()) return;
  fields[index][key] = input.value.trim();
  state.focusSelector = `[data-custom-index="${index}"][data-custom-key="${key}"]`;
  await persist("/api/projects", { values: { "Project ID": project["Project ID"] }, custom_fields: fields },
                () => !!selectedProject());
  await refresh();
  toast("Saved.");
}

function dropHeldSpecEdit(row, field) {
  const held = state.specPending.get(row);
  if (!held) return;
  for (const key of cellFields(field)) delete held[key];   // a confirmed save covers whatever this cell held
  if (!Object.keys(held).length) state.specPending.delete(row);
  specSaveUI();
}

async function saveCell(input) {
  const row = Number(input.dataset.row), field = input.dataset.field;
  const record = projectRows().find((r) => r._row === row);
  if (!record) return;
  const value = input.value.trim(), values = cellValues(field, value);
  if (cellFields(field).every((key) => String(record[key] ?? "") === values[key])) { dropHeldSpecEdit(row, field); return; }
  if (field === "Item" && !value) { toast("An item needs a name.", true); renderSpec(); return; }
  input.classList.add("dirty");
  state.focusSelector = `#spec-groups .cell[data-row="${row}"][data-field="${CSS.escape(field)}"]`;
  try {
    await persist("/api/selections", { row, values }, () => projectRows().some((r) => r._row === row));
    dropHeldSpecEdit(row, field);
    await refresh();
    toast("Saved.");
  } catch (error) {
    state.focusSelector = null;
    holdSpecEdit(input);   // a failed save stays held so Save can retry it
    toast(error.message, true);
    await refresh().catch(() => {});
  }
}

async function removeSpecRow(row) {
  const record = projectRows().find((r) => r._row === row);
  if (!record || !confirm(`Remove “${record.Item || "this line item"}” from the specification? A backup is kept.`)) return;
  setBusy(true, "Removing line item…");
  try {
    await api("/api/selections", { row, values: {}, delete: true });
    await refresh();
    toast("Line item removed.");
  } finally { setBusy(false); }
}

function attachImageErrors() {
  document.querySelectorAll("img").forEach((img) => {
    img.onerror = () => {
      const placeholder = document.createElement("span");
      placeholder.className = img.classList.contains("item-thumb") ? "item-thumb item-placeholder" : "muted";
      placeholder.textContent = img.classList.contains("item-thumb") ? "◇" : "Image unavailable";
      img.replaceWith(placeholder);
    };
  });
}

function field(name, value = "", options = {}) {
  const required = options.required ? " required" : "";
  const readonly = options.readonly ? " readonly" : "";
  const attrs = `name="${esc(name)}"${required}${readonly}`;
  let control;
  if (options.choices) control = options.custom ? choiceControl(value, options, attrs) : menu(value, options.choices, attrs);
  else if (options.textarea) control = `<textarea ${attrs} aria-label="${esc(name)}" maxlength="4000">${esc(value)}</textarea>`;
  else control = `<input ${attrs} type="${options.type || "text"}" value="${esc(value)}" maxlength="${options.max || 4000}" placeholder="${esc(options.placeholder || "")}" ${options.type === "number" ? 'min="0" max="1000000" step="any"' : ""}>`;
  return `<label class="field ${options.wide ? "wide" : ""}">${esc(name)}${options.required ? " *" : ""}${control}</label>`;
}

const menu = (value, choices, attrs, off = false) => `<select ${attrs}${off ? " hidden disabled" : ""}>${choices.map((choice) => `<option value="${esc(choice)}" ${choice === value ? "selected" : ""}>${esc(choice)}</option>`).join("")}</select>`;

// Section and Location come from lists that cannot name every home, so each dropdown ends in
// "Other". "Other" is not a value in itself - it means "not on this list" - so choosing it hands
// over an empty box, and what is typed there is saved on the line item. A stored value the list
// does not hold reopens in that box, ready to edit; the inactive control is disabled so only the
// one on screen is submitted.
const OTHER_CHOICE = "Other";
const CHOICE_LABELS = { list: "Enter another value", custom: "Choose from the list" };

function choiceControl(value, options, attrs) {
  const choices = options.choices.includes(OTHER_CHOICE) ? options.choices : [...options.choices, OTHER_CHOICE];
  const typed = value === OTHER_CHOICE ? "" : choices.includes(value) ? null : value;
  const custom = typed !== null;
  return `<div class="choice" data-original="${esc(value)}">${menu(custom ? OTHER_CHOICE : value, choices, attrs, custom)}<input ${attrs} type="text" value="${esc(typed ?? "")}" maxlength="${options.max || 200}" placeholder="${esc(options.placeholder || "Type a value")}"${custom ? "" : " hidden disabled"}><button type="button" class="text-button choice-toggle">${CHOICE_LABELS[custom ? "custom" : "list"]}</button></div>`;
}

function choiceMode(wrapper, custom, blank = false) {
  const select = wrapper.querySelector("select"), box = wrapper.querySelector("input");
  select.hidden = select.disabled = custom;
  box.hidden = box.disabled = !custom;
  if (blank) box.value = "";
  wrapper.querySelector(".choice-toggle").textContent = CHOICE_LABELS[custom ? "custom" : "list"];
  (custom ? box : select).focus();
}

function editorValues() {
  const values = Object.fromEntries(new FormData($("editor-form")));
  // One box over the two columns a quantity is stored in.
  if (QTY_UNIT in values) {
    Object.assign(values, splitQtyUnit(values[QTY_UNIT]));
    delete values[QTY_UNIT];
  }
  // An "Other" box left empty names nothing new, so the record keeps the value it had.
  for (const wrapper of $("editor-fields").querySelectorAll(".choice")) {
    const box = wrapper.querySelector("input");
    if (!box.disabled && !box.value.trim()) values[box.name] = wrapper.dataset.original;
  }
  return values;
}

function addCustomField(field = { name: "", value: "" }, focus = true) {
  const container = $("custom-field-rows");
  if (container.children.length >= 30) { toast("A record can have up to 30 additional fields.", true); return; }
  const row = document.createElement("div");
  row.className = "custom-field-row";
  row.innerHTML = `<label class="field">Field name<input class="custom-name" type="text" maxlength="80" required value="${esc(field.name)}" placeholder="e.g. Lot number"></label><label class="field">Field value<textarea class="custom-value" aria-label="Field value" maxlength="2000" placeholder="Enter a value">${esc(field.value)}</textarea></label><button type="button" class="remove-custom-field icon-button" aria-label="Remove field">×</button>`;
  container.append(row);
  if (focus) row.querySelector("input").focus();
}

function openEditor(kind, record, prefill = {}) {
  if (state.busy || !state.data) return;
  state.editor = { kind, record, revision: state.data.revision };
  $("editor-error").hidden = true;
  $("editor-title").textContent = `${record ? "Edit" : "New"} ${kind}`;
  $("editor-eyebrow").textContent = kind === "selection" ? "MATERIALS & FINISHES" : "WORKSPACE DETAILS";
  let fields = "", note = "";
  const r = record || prefill;
  if (kind === "project") {
    fields = field("Project ID", r["Project ID"], { required: true, max: 50 }) + field("Project Name", r["Project Name"], { required: true, max: 200 }) + field("Client Name", r["Client Name"]) + field("Presentation Date", r["Presentation Date"]?.slice(0, 10), { type: "date" }) + field("Address", r.Address, { wide: true }) + field("Plan / Elevation", r["Plan / Elevation"]) + field("Designer", r.Designer) + field("Cover Image", r["Cover Image"], { wide: true });
    note = record ? "Changing the Project ID renumbers this project and moves its line items with it. Cover Image accepts an uploaded image, a public image URL, or a local file path, and shows beside this project."
      : `The next number in the sequence is filled in for you — change it if this home needs a different one. A new project starts with the standard room-by-room line items, ready for you to specify. Cover Image accepts an uploaded image, a public URL, or a local file path.`;
  } else if (kind === "selection") {
    fields = field("Item", r.Item, { required: true }) + field("Section", r.Section || state.data.sections[0] || OTHER_CHOICE, { choices: state.data.sections, custom: true, placeholder: "Name the section" }) + field("Location", r.Location, { choices: ["", ...(state.data.locations || [])], custom: true, placeholder: "Name the location" }) + field("Room / Area", r["Room / Area"]) + field(QTY_UNIT, qtyUnit(r), { placeholder: "7571 sq ft", max: 60 }) + field("Unit Price", r["Unit Price"], { type: "number" }) + field("Markup %", r["Markup %"], { type: "number" }) + field("Manufacturer", r.Manufacturer) + field("Model #", r["Model #"]) + field("Finish / Color", r["Finish / Color"]) + field("Size / Configuration", r["Size / Configuration"]) + (state.view === "spec" ? "" : field("Include in Lookbook", r["Include in Lookbook"] || "Yes", { choices: ["Yes", "No"] })) + field("Client Status", r["Client Status"] || "Proposed", { choices: state.data.client_statuses || ["Proposed"] }) + field("Product URL", r["Product URL"], { wide: true, type: "url" }) + field("Product Name", r["Product Name"], { wide: true }) + field("Image URL", r["Image URL"], { wide: true }) + field("Client Notes", r["Client Notes"], { wide: true, textarea: true });
  } else {
    fields = field("Manufacturer", r.Manufacturer, { required: true, readonly: !!record }) + field("Official Domain", r["Official Domain"], { required: true }) + field("Notes", r.Notes, { wide: true, textarea: true });
    note = "Use the official domain only (for example, brand.com), not a retailer or search page.";
  }
  $("editor-fields").innerHTML = fields;
  $("custom-fields-editor").hidden = kind === "manufacturer";
  $("custom-field-rows").replaceChildren();
  if (kind !== "manufacturer") (r.custom_fields || []).forEach((field) => addCustomField(field, false));
  $("editor-note").textContent = note;
  $("editor-note").hidden = !note;
  $("editor-delete").hidden = !record;
  if (record) $("editor-delete").textContent = `Delete ${kind}`;
  $("editor").showModal();
}

async function deleteEditorRecord() {
  const { kind, record } = state.editor || {};
  if (!record || state.busy) return;
  const label = { selection: `selection “${record.Item}”`, project: `project “${record["Project Name"]}”`, manufacturer: `manufacturer “${record.Manufacturer}”` }[kind];
  const owned = kind === "project" ? state.data.selections.filter((r) => r["Project ID"] === record["Project ID"]).length : 0;
  const extra = owned ? ` Its ${owned} line item${owned === 1 ? "" : "s"} will be deleted too.` : "";
  if (!confirm(`Delete ${label}? The row is removed from this project; a backup is kept.${extra}`)) return;
  $("editor-error").hidden = true;
  setBusy(true, "Deleting…");
  try {
    const values = editorValues();
    if (kind === "selection") values["Project ID"] = state.project;
    await api(`/api/${{ project: "projects", selection: "selections", manufacturer: "manufacturers" }[kind]}`,
              { values, row: record._row, delete: true, cascade: owned > 0, revision: state.editor.revision });
    if (kind === "project") state.project = "";
    $("editor").close();
    await refresh();
    toast("Deleted. A backup was preserved.");
  } catch (error) {
    $("editor-error").textContent = error.message;
    $("editor-error").hidden = false;
  } finally { setBusy(false); }
}

async function saveEditor(event) {
  event.preventDefault();
  if (state.busy) return;
  const { kind, record } = state.editor;
  const values = editorValues();
  if (kind === "selection") values["Project ID"] = state.project;
  // A renumbered project is still found by the ID it has now, so the new one travels separately.
  let rename;
  if (kind === "project" && record && values["Project ID"] !== record["Project ID"]) {
    const owned = state.data.selections.filter((r) => r["Project ID"] === record["Project ID"]).length;
    const moving = owned ? `, moving its ${owned} line item${owned === 1 ? "" : "s"} with it` : "";
    if (!confirm(`Renumber “${record["Project Name"]}” from ${record["Project ID"]} to ${values["Project ID"]}${moving}?\n\nA backup is kept.`)) return;
    rename = values["Project ID"];
    values["Project ID"] = record["Project ID"];
  }
  const custom = kind === "manufacturer" ? {} : { custom_fields: Array.from($("custom-field-rows").children, (row) => ({ name: row.querySelector(".custom-name").value, value: row.querySelector(".custom-value").value })) };
  // If this new line item names a heading still held on screen, its card was keeping a place.
  const targetHeld = kind === "selection" && !record
    ? state.specHeld.find((h) => h.project === state.project
        && h.name === (values[h.field] || (h.field === "Section" ? "Other" : "Unassigned")))
    : null;
  setBusy(true, rename ? "Renumbering the project…" : "Saving project details…");
  $("editor-save").disabled = true;
  try {
    const result = await api(`/api/${{ project: "projects", selection: "selections", manufacturer: "manufacturers" }[kind]}`, { values, row: record?._row, create: !record, revision: state.editor.revision, ...(rename ? { rename } : {}), ...custom });
    if (kind === "project") state.project = result.project_id;
    $("editor").close();
    if (targetHeld) {
      reanchorHeld(targetHeld);
      // Saved is saved: if placing it misses, the line item simply sits at the end.
      try { await placeNewSpecRow(result.row, targetHeld); } catch {}
    }
    await refresh();
    toast(result.seeded ? `Project created with ${result.seeded} standard line items ready to specify.`
      : result.moved ? `Renumbered to ${result.project_id}, with ${result.moved} line item${result.moved === 1 ? "" : "s"} moved.`
      : "Project details saved. A backup was preserved.");
  } catch (error) {
    $("editor-error").textContent = error.message;
    $("editor-error").hidden = false;
  } finally { setBusy(false); }
}

async function importSpecCsv(file) {
  if (!file) return;
  const project = selectedProject();
  if (!project) { toast("Choose a project first.", true); return; }
  if (file.size > 8 * 1024 * 1024) throw new Error("Choose a file smaller than 8 MB.");
  if (!confirm(`Import “${file.name}” into “${project["Project Name"]}”?\n\nLines are matched by category, room and item: matches are updated, new lines are added, and nothing else is removed. A backup is kept.`)) return;
  const data = new FormData();
  data.append("file", file);
  data.append("project", state.project);
  data.append("revision", state.data.revision);
  setBusy(true, "Importing the spec sheet…");
  try {
    const result = await api("/api/selections/csv", data);
    await refresh();
    toast(`${result.added} line item${result.added === 1 ? "" : "s"} added, ${result.updated} updated.`);
  } finally { setBusy(false); }
}

let polling = false;
async function pollOnce() {
  if (polling || state.busy || writing || specDrag || $("editor").open || document.hidden) return;
  if (document.activeElement?.classList?.contains("cell")) return;   // never redraw a cell being edited
  polling = true;
  const sent = writes;
  try {
    const data = await api("/api/state");
    if (writes !== sent) return;   // a save landed while this read was in flight, so it is already out of date
    // state.data is null when the very first load failed, so this poll is also the recovery:
    // a workspace must not stay empty because one request happened to land on a bad moment.
    if (!state.busy && !$("editor").open && data.revision !== state.data?.revision) {
      state.data = data;
      if (!selectedProject()) state.project = data.projects[0]?.["Project ID"] || "";
      $("load-error").hidden = true;
      render();
    }
  } catch (error) {
    $("load-error").textContent = error.message;
    $("load-error").hidden = false;
  } finally { polling = false; }
}
setInterval(pollOnce, 2500);

function setSidebar(collapsed) {
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  const button = $("sidebar-toggle");
  button.setAttribute("aria-expanded", String(!collapsed));
  const label = collapsed ? "Expand navigation" : "Collapse navigation";
  button.title = label;
  button.setAttribute("aria-label", label);
  try { localStorage.setItem("uh-sidebar", collapsed ? "collapsed" : "open"); } catch {}
}

try { if (localStorage.getItem("uh-sidebar") === "collapsed") setSidebar(true); } catch {}
$("sidebar-toggle").addEventListener("click", () => setSidebar(!document.body.classList.contains("sidebar-collapsed")));

$("add-custom-field").addEventListener("click", () => { if (!state.busy) addCustomField(); });
$("project-save-details").addEventListener("click", () => action(async () => {
  const project = selectedProject();
  if (!project) return;
  // Commit anything typed but not yet blurred, then confirm the record is stored.
  const pending = Array.from(document.querySelectorAll("#project-detail-values .cell"))
    .filter((input) => input.dataset.projectField
      && (input.dataset.projectField === "Presentation Date"
        ? (project[input.dataset.projectField] || "").slice(0, 10) : project[input.dataset.projectField] || "") !== input.value.trim());
  for (const input of pending) await saveProjectField(input);
  if (!pending.length) await refresh();
  toast("Project details saved. A backup was preserved.");
}));
$("project-delete").addEventListener("click", () => {
  const project = selectedProject();
  if (project) { openEditor("project", project); deleteEditorRecord(); }
});
$("project-add-field").addEventListener("click", () => {
  if (state.busy || !selectedProject()) return;
  openEditor("project", selectedProject());
  addCustomField();
});
$("custom-field-rows").addEventListener("click", (event) => {
  const button = event.target.closest(".remove-custom-field");
  if (!button || state.busy) return;
  const row = button.closest(".custom-field-row");
  const name = row.querySelector(".custom-name").value;
  if (name && !confirm(`Remove the field “${name}”? This takes effect when you save changes.`)) return;
  row.remove();
});
specDragEvents();
specResizeEvents();
$("editor-form").addEventListener("submit", saveEditor);
$("editor-delete").addEventListener("click", deleteEditorRecord);
// Picking "Other" asks for a value the list does not hold, so the dropdown gives way to a box.
$("editor-fields").addEventListener("change", (event) => {
  const wrapper = event.target.closest(".choice");
  if (wrapper && event.target.tagName === "SELECT" && event.target.value === OTHER_CHOICE) choiceMode(wrapper, true, true);
});
$("editor-fields").addEventListener("click", (event) => {
  const toggle = event.target.closest(".choice-toggle");
  if (toggle) choiceMode(toggle.closest(".choice"), toggle.closest(".choice").querySelector("input").disabled);
});
// Homes are numbered in one sequence, so a new project opens on the next free number rather
// than a blank box: the highest UH-### in the workspace plus one, zero padded to three digits.
// It is prefilled, not fixed - a home that needs a different number can still have one.
const nextProjectId = () => {
  const used = (state.data?.projects || [])
    .map((p) => /^UH-0*(\d+)$/.exec(p["Project ID"] || "")?.[1])
    .filter(Boolean).map(Number);
  return `UH-${String(used.length ? Math.max(...used) + 1 : 1).padStart(3, "0")}`;
};
$("new-project").addEventListener("click", () => openEditor("project", null, { "Project ID": nextProjectId() }));
$("edit-project").addEventListener("click", () => openEditor("project", selectedProject()));
$("add-selection").addEventListener("click", () => openEditor("selection"));
$("spec-filter").addEventListener("input", renderSpec);

// Not every sheet is sent for the same reason, so any column can be switched off before a
// download and the CSV, Word and PDF copies all leave it out. Which columns those are says how
// one person sends this sheet rather than anything about the home, so - like the column widths -
// it is remembered in this browser and never written to the workspace. The list itself is the
// server's one registry (`build_lookbook.HIDEABLE_COLUMNS`), so the chooser cannot offer a
// column the exports do not know how to leave out.
const EXPORT_HIDE_KEY = "uh-export-hidden";
const exportHidden = new Set((() => {
  try { return JSON.parse(localStorage.getItem(EXPORT_HIDE_KEY)) || []; } catch { return []; }
})());
const exportColumns = () => state.data?.export_columns || [];
// Filtered against that registry on the way out: a name left over from an older version is
// refused outright, and a download is a plain navigation, so the refusal would land as a page.
const hiddenColumns = () => exportColumns().filter((c) => exportHidden.has(c.key)).map((c) => c.key);
const joinWords = (words) => (words.length < 2 ? words.join("") : `${words.slice(0, -1).join(", ")} and ${words.at(-1)}`);

function renderExportColumns() {
  const columns = exportColumns(), hidden = hiddenColumns();
  $("export-column-list").innerHTML = columns.map((c) => {
    const off = exportHidden.has(c.key);
    return `<label class="column-toggle${off ? " off" : ""}"><input type="checkbox" data-column="${esc(c.key)}"${off ? "" : " checked"}><span>${esc(c.label)}</span></label>`;
  }).join("");
  $("export-columns-summary").textContent = hidden.length
    ? `${columns.length - hidden.length} of ${columns.length} · ${hidden.length} left out`
    : "Every column. Switch one off to leave it out of the files above.";
  $("export-columns-all").hidden = !hidden.length;
  // The CSV is the one file that comes back in, and it is matched to its line items by these
  // three columns: a sheet sent back without them reads as new items rather than as edits.
  const matched = columns.filter((c) => c.matches && exportHidden.has(c.key)).map((c) => c.label);
  $("export-columns-warning").hidden = !matched.length;
  $("export-columns-warning").textContent = matched.length
    ? `Without ${joinWords(matched)}, a CSV sent back cannot be matched to these line items: importing it would add new ones instead of updating them.`
    : "";
}

$("export-column-list").addEventListener("change", (event) => {
  const box = event.target.closest("input[data-column]");
  if (!box) return;
  if (box.checked) exportHidden.delete(box.dataset.column); else exportHidden.add(box.dataset.column);
  try { localStorage.setItem(EXPORT_HIDE_KEY, JSON.stringify([...exportHidden])); } catch {}
  renderExportColumns();
});
$("export-columns-all").addEventListener("click", () => {
  exportHidden.clear();
  try { localStorage.removeItem(EXPORT_HIDE_KEY); } catch {}
  renderExportColumns();
});

// Landscape gives nine columns the width they want; portrait files beside the client schedule
// and prints on a sheet already in the binder. Which one is wanted is how this sheet is sent
// rather than anything about the home, so - like the hidden columns - it is remembered in this
// browser. A CSV has no page, so the choice rides only with the Word and PDF copies.
const ORIENTATION_KEY = "uh-spec-orientation";
const ORIENTATIONS = ["landscape", "portrait"];
const loadOrientation = () => {
  try { const stored = localStorage.getItem(ORIENTATION_KEY); if (ORIENTATIONS.includes(stored)) return stored; } catch {}
  return ORIENTATIONS[0];
};
state.specOrientation = loadOrientation();

function renderExportOrientation() {
  document.querySelectorAll("#files-dialog [data-orientation]").forEach((chip) =>
    chip.classList.toggle("active", chip.dataset.orientation === state.specOrientation));
}

function setExportOrientation(value) {
  if (!ORIENTATIONS.includes(value)) return;
  state.specOrientation = value;
  try { localStorage.setItem(ORIENTATION_KEY, value); } catch {}
  renderExportOrientation();
}

// The download carries the spec sheet as it is on screen, under the same group headings and
// without the columns switched off above.
const specQuery = () => {
  const hidden = hiddenColumns();
  return `?project=${encodeURIComponent(state.project)}&group=${encodeURIComponent(state.specGroup)}`
    + (hidden.length ? `&hide=${encodeURIComponent(hidden.join(","))}` : "");
};
const docQuery = () => `${specQuery()}&orientation=${encodeURIComponent(state.specOrientation)}`;
// A download is fetched, never navigated to: an attachment reply leaves the page alone either
// way, but an error reply - a 502 while the hosted instance restarts - committed the navigation,
// and the tab sat on the API address where every refresh, restore and retry downloaded the file
// again. One click once arrived as dozens of copies that way. Going through `api` also picks up
// the read-side gateway waits, so a restart mid-download is waited out rather than reported.
const download = async (path) => {
  setBusy(true, "Preparing the download…");
  try {
    const response = await api(path, undefined, true);
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const name = decodeURIComponent(
      (disposition.match(/filename\*=UTF-8''([^;]+)/) || disposition.match(/filename="?([^";]+)/) || [])[1]
      || "download");
    const anchor = Object.assign(document.createElement("a"),
      { href: URL.createObjectURL(blob), download: name, hidden: true });
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(anchor.href);
  } finally { setBusy(false); }
};
const FILE_ACTIONS = {
  csv: () => download(`/api/selections/csv${specQuery()}`),
  "spec-docx": () => download(`/api/selections/docx${docQuery()}`),
  "spec-pdf": () => download(`/api/selections/pdf${docQuery()}`),
  "import-csv": () => $("spec-import-file").click(),
};
const NEEDS_PROJECT = new Set(["csv", "spec-docx", "spec-pdf", "import-csv"]);
const FILE_NAMES = {
  csv: "Spec sheet · CSV", "spec-docx": "Spec sheet · Word", "spec-pdf": "Spec sheet · PDF",
  "import-csv": "Spec sheet · CSV, brought in",
};
// A file is chosen first and sent on OK, rather than downloading the moment it is clicked:
// the orientation and the columns below are part of the same decision, and picking the file
// first used to mean closing the dialog with settings still to make. Cleared each time the
// dialog opens, so OK can never repeat the last download by accident.
let fileChoice = null;

function renderFileChoice() {
  document.querySelectorAll("#files-dialog .file-choice").forEach((card) => {
    const chosen = card.dataset.file === fileChoice;
    card.classList.toggle("selected", chosen);
    card.setAttribute("aria-pressed", String(chosen));
  });
  $("files-chosen").textContent = fileChoice ? FILE_NAMES[fileChoice] : "Choose a file above.";
  $("files-ok").disabled = !fileChoice;
}

// Drawn as the dialog opens rather than from `render`: the list only changes when someone
// clicks it, and rebuilding it under the 2.5s poll would take the focus off a box mid-choice.
$("files-button").addEventListener("click", () => {
  if (!state.data || state.busy) return;
  fileChoice = null;
  renderExportColumns();
  renderExportOrientation();
  renderFileChoice();
  $("files-dialog").showModal();
});
$("files-dialog").addEventListener("click", (event) => {
  const orientation = event.target.closest("[data-orientation]");
  if (orientation) { setExportOrientation(orientation.dataset.orientation); return; }
  const choice = event.target.closest(".file-choice");
  if (!choice || state.busy) return;
  fileChoice = choice.dataset.file;
  renderFileChoice();
});
$("files-ok").addEventListener("click", () => {
  if (!fileChoice || state.busy) return;
  if (NEEDS_PROJECT.has(fileChoice) && !selectedProject()) { toast("Choose a project first.", true); return; }
  $("files-dialog").close();
  action(() => FILE_ACTIONS[fileChoice]());
});
$("spec-import-file").addEventListener("change", (event) => {
  const file = event.target.files[0];
  event.target.value = "";
  action(() => importSpecCsv(file));
});
$("spec-add").addEventListener("click", () => openEditor("selection"));
// A room is added to the sheet before it is added to the workspace: the card appears at once with
// its heading in a box, and the first line item named under it is what makes it real.
$("spec-add-group").addEventListener("click", () => {
  if (state.busy || !selectedProject()) return;
  state.specNaming = true;
  state.focusSelector = "#spec-groups .group-new";
  renderSpec();
  restoreFocus();
});

$("spec-expand").addEventListener("click", () => { state.specCollapsed.clear(); renderSpec(); });
$("spec-collapse").addEventListener("click", () => {
  specGroups(projectRows()).forEach(([name]) => state.specCollapsed.add(name));
  renderSpec();
});
const pendingSpecCount = () => [...state.specPending.values()].reduce((sum, values) => sum + Object.keys(values).length, 0);

let specSaving = 0;   // an autosave still writing re-renders itself; an early redraw would flash stale values

// The save pill is a notification at the top right, not a fixture: "Saving…" and the unsaved
// count hold while they are true, and "All changes saved" flashes for a few seconds on the
// way out of a save, then fades away. A quiet sheet shows nothing, and the 2.5s poll landing
// on one must not keep bringing the pill back.
let saveMode = "hidden", saveFlashTimer;

function specSaveUI() {
  const count = pendingSpecCount(), saving = state.busy || specSaving > 0;
  $("spec-save").textContent = count ? `Save changes (${count})` : "Save";
  const status = $("spec-save-status");
  const mode = saving ? "saving" : count ? "pending" : projectRows().length ? "saved" : "hidden";
  if (mode === "saved" && saveMode === "saved") return;   // the flash is up - let it fade
  const was = saveMode;
  saveMode = mode;
  clearTimeout(saveFlashTimer);
  // While a whole-workspace operation runs, the busy banner carries the message; the pill
  // stays out of its way but still tracks the transition so "saved" flashes afterwards.
  if (state.busy) { status.hidden = true; return; }
  // "saved" only announces itself after a save actually ran; landing on an already-quiet
  // sheet (load, a project switch, the poll) shows nothing.
  if (mode === "hidden" || (mode === "saved" && was !== "pending" && was !== "saving")) {
    status.hidden = true;
    return;
  }
  status.hidden = false;
  status.className = `save-status${mode === "saving" ? " saving" : mode === "pending" ? " pending" : ""}`;
  status.textContent = mode === "saving" ? "Saving…"
    : mode === "pending" ? `${count} unsaved change${count === 1 ? "" : "s"}`
    : "All changes saved";
  if (mode === "saved") {
    saveFlashTimer = setTimeout(() => {
      status.classList.add("fading");
      saveFlashTimer = setTimeout(() => { status.hidden = true; status.classList.remove("fading"); }, 450);
    }, 3200);
  }
}

async function specSaveAll() {
  const count = pendingSpecCount();
  if (!count) return;
  if ([...state.specPending.values()].some((values) => "Item" in values && !values.Item)) {
    toast("An item needs a name.", true);
    return;
  }
  const batch = Object.fromEntries(state.specPending);
  setBusy(true, "Saving changes…");
  try {
    await persist("/api/selections", { batch },
                 () => Object.keys(batch).every((row) => projectRows().some((r) => r._row === Number(row))));
    state.specPending.clear();   // only once the save has them
    await refresh();
    toast(`Saved ${count} change${count === 1 ? "" : "s"}.`);
  } finally { setBusy(false); }
}

// Manual save stays available even though edits autosave: it commits the cell being
// typed and retries anything a busy moment or failed save left held.
$("spec-save").addEventListener("click", () => action(async () => {
  const editing = document.activeElement;
  if (editing?.classList?.contains("cell")) editing.blur();
  if (pendingSpecCount()) await specSaveAll();
  else if (!specSaving) toast("Everything is already saved.");
}));

function holdSpecEdit(input) {
  const row = Number(input.dataset.row), field = input.dataset.field;
  const record = projectRows().find((r) => r._row === row);
  const pending = state.specPending.get(row) || {};
  for (const [key, value] of Object.entries(cellValues(field, input.value.trim()))) {
    if (record && String(record[key] ?? "") === value) delete pending[key];   // back to the saved value
    else pending[key] = value;
  }
  if (Object.keys(pending).length) state.specPending.set(row, pending);
  else state.specPending.delete(row);
  input.classList.toggle("dirty", cellFields(field).some((key) => key in pending));
  specSaveUI();
}

$("spec-groups").addEventListener("change", (event) => {
  if (event.target.dataset.groupNew !== undefined) {
    addSpecGroup(event.target.value.trim());
    return;
  }
  if (event.target.dataset.groupRename !== undefined) {
    action(() => renameSpecGroup(event.target.dataset.groupRename, event.target.value.trim()));
    return;
  }
  if (!event.target.classList.contains("cell")) return;
  if (state.busy) { holdSpecEdit(event.target); return; }   // a save is running — hold this edit for Save
  specSaving++;
  specSaveUI();
  action(() => saveCell(event.target)).finally(() => { specSaving--; specSaveUI(); });
});
$("spec-groups").addEventListener("input", (event) => {
  // A wrapped detail name grows its box line by line as a long heading is typed; renderSpec
  // refits every box again once the edit saves.
  if (event.target.classList.contains("detail-name")) fitDetailName(event.target);
});
$("spec-groups").addEventListener("keydown", (event) => {
  const naming = event.target.dataset.groupNew !== undefined;
  if (!naming && event.target.dataset.groupRename === undefined) {
    // A detail's name still holds one line of text even though its box wraps it: Enter
    // commits through change like any other spec cell rather than typing a newline.
    if (event.key === "Enter" && event.target.classList.contains("detail-name")) {
      event.preventDefault();
      event.target.blur();
    }
    return;
  }
  if (event.key === "Escape") {
    // Taking the box off the page blurs it, and a blurred input whose value has changed fires
    // change on the way out - which would save the very edit Escape is abandoning. Put the
    // heading back first - a card being named never had one - so there is nothing left to commit.
    event.target.value = naming ? "" : event.target.dataset.groupRename;
    if (naming) state.specNaming = false;
    else state.specRename = null;
    renderSpec();
  }
  // Enter commits through change when the name differs, and otherwise just closes the box.
  if (event.key === "Enter") { event.preventDefault(); event.target.blur(); }
});
$("spec-groups").addEventListener("focusout", (event) => {
  const input = event.target;
  if (input.dataset.groupNew !== undefined) {
    // Still open means nothing was typed, so clicking away leaves the card out, as Escape does.
    if (state.specNaming) { state.specNaming = false; renderSpec(); }
    return;
  }
  if (input.dataset.groupRename !== undefined) {
    // Still open means the name was left as it was: clicking away abandons it rather than saving.
    if (state.specRename !== null) { state.specRename = null; renderSpec(); }
    return;
  }
  if (!input.classList.contains("cell")) return;
  const column = SPEC_COLUMNS.find((c) => c.field === input.dataset.field);
  if (!column?.link) return;
  const wasEditing = state.specLinkEdit.delete(Number(input.dataset.row));
  // the link editor folds back into the "Link" anchor once it loses focus
  if (!specSaving && (wasEditing || isUrl(input.value.trim()))) renderSpec();
});
$("spec-client-panel").addEventListener("change", (event) => {
  if (event.target.dataset.projectField) action(() => saveProjectField(event.target));
});
$("project-detail-values").addEventListener("change", (event) => {
  const input = event.target;
  if (!input.classList.contains("cell")) return;
  action(() => (input.dataset.projectField ? saveProjectField(input) : saveProjectCustom(input)));
});
$("project-detail-values").addEventListener("click", (event) => {
  const thumb = event.target.closest(".cover-thumb");
  if (thumb) {
    $("cover-lightbox-img").src = thumb.src;
    $("cover-lightbox").showModal();
    return;
  }
  if (event.target.closest("[data-cover-upload]") && !state.busy && selectedProject()) $("cover-file").click();
});
$("cover-lightbox").addEventListener("click", () => $("cover-lightbox").close());
$("project-emblem").addEventListener("click", () => {
  const img = $("project-emblem").querySelector("img");
  if (!img) return;
  $("cover-lightbox-img").src = img.src;
  $("cover-lightbox").showModal();
});
async function uploadCover(file) {
  const project = selectedProject();
  if (!project || !file) return;
  const data = new FormData();
  data.append("file", file);
  data.append("revision", state.data.revision);
  setBusy(true, "Saving the cover image…");
  try {
    await api(`/api/projects/${encodeURIComponent(project["Project ID"])}/cover`, data);
    await refresh();
    toast("Cover image saved. It opens this project’s next exported schedule.");
  } finally { setBusy(false); }
}
$("cover-file").addEventListener("change", (event) => {
  const file = event.target.files[0];
  event.target.value = "";
  action(() => uploadCover(file));
});
$("add-manufacturer").addEventListener("click", () => openEditor("manufacturer"));

$("refresh").addEventListener("click", () => action(async () => { await refresh(); toast("Workspace refreshed."); }));
// Only rendered on the hosted workspace, where there is a session to end.
$("sign-out")?.addEventListener("click", () => action(async () => {
  await api("/api/logout", {});
  window.location.assign("/login");
}));
$("previous-page").addEventListener("click", () => { state.page--; renderTable(); });
$("next-page").addEventListener("click", () => { state.page++; renderTable(); });
for (const id of ["search", "section-filter", "location-filter"]) $(id).addEventListener(id === "search" ? "input" : "change", () => { state.page = 0; renderTable(); });
// Every modal closes on Escape, so clicking away from one closes it too — the editor, the
// settings dialog, the import/export dialog and the cover lightbox alike. A dialog's own
// padding reports the dialog as the click target, so the pointer is measured against its box
// instead of trusting the target, and the press has to have started outside as well: dragging
// to select text in a field and releasing past the edge must not discard a half-filled form.
const backdropOf = (event) => {
  const dialog = event.target;
  if (!(dialog instanceof HTMLDialogElement)) return null;
  const box = dialog.getBoundingClientRect();
  const outside = event.clientX < box.left || event.clientX > box.right
    || event.clientY < box.top || event.clientY > box.bottom;
  return outside ? dialog : null;
};
let pressedBackdrop = null;
document.addEventListener("pointerdown", (event) => { pressedBackdrop = backdropOf(event); });
document.addEventListener("click", (event) => {
  const dialog = backdropOf(event);
  if (dialog && dialog === pressedBackdrop) dialog.close();
  pressedBackdrop = null;
});
document.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.classList.contains("close-dialog")) { button.closest("dialog").close(); return; }
  if (state.busy || !state.data) return;
  if (button.dataset.view) { state.view = button.dataset.view; render(); }
  if (button.dataset.project) { state.project = button.dataset.project; state.page = 0; state.specLinkEdit.clear(); state.specNaming = false; $("search").value = ""; $("section-filter").value = ""; $("location-filter").value = ""; render(); }
  if (button.dataset.group) { state.specGroup = button.dataset.group; state.specCollapsed.clear(); state.specNaming = false; renderSpec(); }
  if (button.dataset.specToggle !== undefined) {
    const name = button.dataset.specToggle;
    state.specCollapsed.has(name) ? state.specCollapsed.delete(name) : state.specCollapsed.add(name);
    renderSpec();
  }
  if (button.dataset.specAdd !== undefined) {
    const name = button.dataset.specAdd;   // placeholder group names are not real values
    const prefill = name === specPlaceholder() ? {} : { [state.specGroup]: name };
    openEditor("selection", null, prefill);
  }
  if (button.dataset.specRemove) action(() => removeSpecRow(Number(button.dataset.specRemove)));
  // An empty card was never in the workspace, so letting it go is a change of mind, not a delete.
  if (button.dataset.specDiscard !== undefined) {
    const name = button.dataset.specDiscard;
    state.specHeld = state.specHeld.filter((held) =>
      !(held.field === state.specGroup && held.project === state.project && held.name === name));
    renderSpec();
  }
  if (button.dataset.specRename !== undefined) {
    state.specRename = button.dataset.specRename;
    state.focusSelector = "#spec-groups .group-rename";
    renderSpec();
    restoreFocus();
  }
  if (button.dataset.linkEdit !== undefined) {
    const row = Number(button.dataset.linkEdit);
    state.specLinkEdit.add(row);
    state.focusSelector = `#spec-groups .cell[data-row="${row}"][data-field="Product URL"]`;
    renderSpec();
    restoreFocus();
  }
  if (button.dataset.edit) openEditor("selection", projectRows().find((r) => r._row === Number(button.dataset.edit)));
  if (button.dataset.manufacturer !== undefined) openEditor("manufacturer", state.data.manufacturers[Number(button.dataset.manufacturer)]);
});

// ---------------------------------------------------------------------------
// Design inspirations: collections of images clipped from the web (see catalog.py).
// Collections and items arrive with /api/state, so the 2.5s poll keeps them fresh;
// image bytes are stored by the app and served from /api/catalog/images, never hotlinked.
// ---------------------------------------------------------------------------
const cat = () => state.data?.catalog || { collections: [], items: [], comments: [], categories: [], approvals: [], ai: false, user: "", shared_accounts: false };
let catCollectionId = "";          // "" = all inspirations
let catFavsOnly = false;
// A page is rarely worth one picture only, so the capture step keeps a list: every image
// clicked is saved as its own inspiration, sharing the details reviewed in step 3.
let clipTab = "page", clipPicks = [], clipFile = null, clipInspected = null, clipInspectFailed = false;

const catImage = (name) => `/api/catalog/images/${encodeURIComponent(name || "")}`;
const catItem = (id) => cat().items.find((i) => i["Item ID"] === id);
const catCollection = (id) => cat().collections.find((c) => c["Collection ID"] === id);
const catCollections = () => [...cat().collections].sort((a, b) => String(b.Updated || "").localeCompare(String(a.Updated || "")));
const catItems = () => [...cat().items].sort((a, b) => String(b["Saved On"] || "").localeCompare(String(a["Saved On"] || "")));
const itemThumb = (i) => catImage(i["Thumb File"] || i["Image File"]);
const collectionItems = (id) => catItems().filter((i) => i["Collection ID"] === id);
const coverFor = (c) => {
  if (c["Cover Image"]) return catImage(c["Cover Image"]);
  const first = collectionItems(c["Collection ID"])[0];
  return first ? itemThumb(first) : "";
};

function renderCatalog() {
  const data = cat();
  if (!$("catalog-view") || state.view !== "catalog") return;
  const oldCategory = $("catalog-category").value;
  $("catalog-category").innerHTML = '<option value="">All categories</option>' + data.categories.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  $("catalog-category").value = oldCategory;
  if (catCollectionId && !catCollection(catCollectionId)) catCollectionId = "";
  const strip = [`<button class="collection-card all-card ${catCollectionId === "" ? "selected" : ""}" data-collection="">
      <span class="collection-cover" aria-hidden="true">❖</span>
      <span class="collection-body"><strong>All inspirations</strong><small>${data.items.length} saved</small></span></button>`];
  for (const c of catCollections()) {
    const count = collectionItems(c["Collection ID"]).length;
    const cover = coverFor(c);
    strip.push(`<button class="collection-card ${catCollectionId === c["Collection ID"] ? "selected" : ""}" data-collection="${esc(c["Collection ID"])}" title="${esc(c.Description || c.Name)}">
      <span class="collection-cover">${cover ? `<img src="${esc(cover)}" alt="" loading="lazy">` : "❖"}</span>
      <span class="collection-body"><strong>${esc(c.Name)}</strong><small>${count} inspiration${count === 1 ? "" : "s"}${c.Updated ? ` · ${esc(String(c.Updated).slice(0, 10))}` : ""}</small></span></button>`);
  }
  $("collection-strip").innerHTML = strip.join("");
  const query = $("catalog-search").value.trim().toLowerCase(), category = $("catalog-category").value;
  const rows = catItems().filter((i) => (!catCollectionId || i["Collection ID"] === catCollectionId)
    && (!category || i.Category === category)
    && (!catFavsOnly || i.Favorite === "Yes")
    && (!query || [i.Title, i.Description, i.Notes, i.Tags, i.Category, i.Materials, i["Source Site"]].join(" ").toLowerCase().includes(query)));
  $("catalog-gallery").innerHTML = rows.map((i) => `<article class="insp-card" data-insp="${esc(i["Item ID"])}" tabindex="0" role="button" aria-label="Open ${esc(i.Title || "inspiration")}">
      <img src="${esc(itemThumb(i))}" alt="${esc(i.Title || "Saved inspiration")}" loading="lazy">
      <button class="insp-fav ${i.Favorite === "Yes" ? "active" : ""}" data-fav="${esc(i["Item ID"])}" aria-label="${i.Favorite === "Yes" ? "Remove from favorites" : "Add to favorites"}" aria-pressed="${i.Favorite === "Yes"}">♥</button>
      <div class="insp-body"><strong>${esc(i.Title || "Untitled inspiration")}</strong><small>${[i.Category, i["Source Site"]].filter(Boolean).map(esc).join(" · ") || "&nbsp;"}</small></div>
    </article>`).join("");
  $("catalog-empty").hidden = rows.length > 0;
  $("catalog-gallery").classList.toggle("empty", !rows.length);
}

// --- Collection editor ------------------------------------------------------
let editingCollection = null;

function openCollectionEditor(collection) {
  editingCollection = collection || null;
  $("collection-title").textContent = collection ? collection.Name : "New collection";
  $("collection-name").value = collection?.Name || "";
  $("collection-description").value = collection?.Description || "";
  $("collection-delete").hidden = !collection;
  $("collection-error").hidden = true;
  $("collection-dialog").showModal();
}

async function saveCollection(event) {
  event.preventDefault();
  await action(async () => {
    const values = { "Collection ID": editingCollection?.["Collection ID"] || "",
                     Name: $("collection-name").value.trim(),
                     Description: $("collection-description").value.trim(),
                     "Cover Image": editingCollection?.["Cover Image"] || "" };
    try {
      const result = await persist("/api/catalog/collections", { values });
      $("collection-dialog").close();
      if (!editingCollection && result.collection) catCollectionId = result.collection;
      await refresh();
      toast(editingCollection ? "Collection updated." : "Collection created. Clip something beautiful into it.");
    } catch (error) {
      $("collection-error").textContent = error.message;
      $("collection-error").hidden = false;
      throw error;
    }
  });
}

async function deleteCollection() {
  const collection = editingCollection;
  if (!collection) return;
  const count = collectionItems(collection["Collection ID"]).length;
  if (!confirm(`Delete “${collection.Name}”${count ? ` and its ${count} saved inspiration${count === 1 ? "" : "s"}` : ""}? This cannot be undone.`)) return;
  await action(async () => {
    await persist("/api/catalog/collections", { values: { "Collection ID": collection["Collection ID"], Name: collection.Name }, delete: true, cascade: true });
    $("collection-dialog").close();
    if (catCollectionId === collection["Collection ID"]) catCollectionId = "";
    await refresh();
    toast("Collection deleted.");
  });
}

// --- Clip dialog: from a page, an image address, or an upload ---------------
function setClipTab(tab) {
  clipTab = tab;
  document.querySelectorAll("[data-clip-tab]").forEach((b) => b.classList.toggle("active", b.dataset.clipTab === tab));
  $("clip-page-panel").hidden = tab !== "page";
  $("clip-image-panel").hidden = tab !== "image";
  $("clip-upload-panel").hidden = tab !== "upload";
  updateClipChosen();
}

const clipCount = () => (clipTab === "page" ? clipPicks.length : 1);
const pictures = (n) => `${n} picture${n === 1 ? "" : "s"}`;

function updateClipChosen() {
  const picked = clipPicks.length;
  $("clip-chosen").textContent = clipStep === 1 ? "Step 1 of 3 — choose where it files."
    : clipStep === 3 ? (clipCount() === 1 ? "Step 3 of 3 — review the details, then save."
      : `Step 3 of 3 — these details go on all ${clipCount()} inspirations.`)
    : clipTab === "page" ? (picked ? `${pictures(picked)} selected — click more, or Continue.` : "Find images, then click every picture you want.")
    : clipTab === "image" ? "The image at that address will be saved."
    : (clipFile ? `Ready to upload ${clipFile.name}.` : "Choose an image file to upload.");
  $("clip-save").textContent = clipCount() > 1 ? `Save ${clipCount()} inspirations` : "Save inspiration";
}

// The clip dialog walks three steps: collection first, then capture, then review and save.
let clipStep = 1;

function setClipStep(step) {
  clipStep = step;
  for (const n of [1, 2, 3]) $(`clip-step-${n}`).hidden = n !== step;
  document.querySelectorAll("[data-step-dot]").forEach((dot) => {
    const at = Number(dot.dataset.stepDot);
    dot.classList.toggle("current", at === step);
    dot.classList.toggle("done", at < step);
  });
  $("clip-back").hidden = step === 1;
  $("clip-next").hidden = step === 3;
  $("clip-save").hidden = step !== 3;
  $("clip-error").hidden = true;
  updateClipChosen();
}

function clipStepForward() {
  const fail = (message) => { $("clip-error").textContent = message; $("clip-error").hidden = false; };
  if (clipStep === 1) {
    if ($("clip-collection").value === "__new" && !$("clip-new-collection").value.trim()) { fail("Name the new collection first."); return; }
    setClipStep(2);
  } else if (clipStep === 2) {
    if (clipTab === "page" && !clipPicks.length) {
      fail(clipInspectFailed
        ? "That page could not be read, so there are no images to pick here. Switch to the Image address or Upload tab, or clip it with the Clip to UH Homes bookmark."
        : "Click the pictures you want to save — one, or as many as you like.");
      return;
    }
    if (clipTab === "image" && !$("clip-image-url").value.trim()) { fail("Paste the image's address first."); return; }
    if (clipTab === "upload" && !clipFile) { fail("Choose an image file to upload."); return; }
    setClipStep(3);
  }
}

function openClip(pageUrl) {
  clipPicks = []; clipFile = null; clipInspected = null; clipInspectFailed = false;
  $("clip-page-note").classList.remove("form-error");
  for (const id of ["clip-page-url", "clip-image-url", "clip-title", "clip-tags", "clip-notes", "clip-description", "clip-new-collection", "clip-price", "clip-brand", "clip-model", "clip-finish", "clip-size", "clip-link", "clip-qty"]) $(id).value = "";
  $("clip-file-name").textContent = "";
  $("clip-candidates").hidden = true;
  $("clip-candidates").innerHTML = "";
  $("clip-page-note").hidden = true;
  $("clip-error").hidden = true;
  $("clip-category").innerHTML = '<option value="">Choose automatically</option>' + cat().categories.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  const options = catCollections().map((c) => `<option value="${esc(c["Collection ID"])}" ${c["Collection ID"] === catCollectionId ? "selected" : ""}>${esc(c.Name)}</option>`).join("");
  $("clip-collection").innerHTML = options + '<option value="__new">— New collection —</option>';
  if (!catCollections().length) $("clip-collection").value = "__new";
  $("clip-new-collection-field").hidden = $("clip-collection").value !== "__new";
  setClipTab("page");
  setClipStep(1);
  $("clip-dialog").showModal();
  if (pageUrl) { $("clip-page-url").value = pageUrl; inspectPage(); }
}

async function inspectPage() {
  const url = $("clip-page-url").value.trim();
  if (!url) { toast("Paste the address of the page first.", true); return; }
  await action(async () => {
    $("clip-inspect").disabled = true;
    try {
      let result;
      try {
        result = await api("/api/catalog/inspect", { url });
      } catch {
        // The site refused or could not be read. No error codes here — just the flow
        // that works for a page like this, inside the dialog where the person is looking.
        clipInspected = null; clipPicks = []; clipInspectFailed = true;
        $("clip-candidates").hidden = true;
        $("clip-candidates").innerHTML = "";
        $("clip-page-note").textContent = "This page prefers to be clipped with your Clip to UH Homes bookmark: "
          + "1) Open the product page in another tab.  2) Click the bookmark in your bookmarks bar "
          + "(set it up once under Get the clipper).  3) The images and details arrive here by themselves, "
          + "ready to review and save. You can also upload a screenshot, or paste the picture's own address in the Image address tab.";
        $("clip-page-note").classList.add("form-error");
        $("clip-page-note").hidden = false;
        updateClipChosen();
        return;
      }
      showInspected(result);
    } finally { $("clip-inspect").disabled = false; }
  });
}

// What a read page looks like in the dialog, wherever the reading happened: the server's
// inspect or the bookmarklet's in-browser scan both land here.
function showInspected(result, how = "found on") {
  clipInspected = result; clipPicks = []; clipInspectFailed = false;
  $("clip-page-note").classList.remove("form-error");
  // Fill every default field the page offers, leaving anything already typed alone.
  for (const [id, key] of [["clip-title", "title"], ["clip-description", "description"], ["clip-price", "price"],
                           ["clip-brand", "brand"], ["clip-model", "model"], ["clip-finish", "finish"],
                           ["clip-size", "size"], ["clip-link", "source_url"]]) {
    if (!$(id).value) $(id).value = result[key] || "";
  }
  $("clip-page-note").textContent = result.images.length
    ? `${result.images.length} image${result.images.length === 1 ? "" : "s"} ${how} ${result.site || "the page"} — click every picture you want to keep (one or several), × away the rest, ⤢ to see one large.`
    : "No images could be read from that page. Try the Image address tab with a direct link to the picture.";
  $("clip-page-note").hidden = false;
  renderClipCandidates();
  updateClipChosen();
}

// The candidate grid. Clicking a tile keeps that picture — as many as the page is worth,
// each saved as its own inspiration — ⤢ opens it large, and × drops it from the choices,
// since read pages bring banners and related products along. Both the keeping and the
// removing live in this dialog only: nothing is written until Save.
function renderClipCandidates() {
  const images = clipInspected?.images || [];
  $("clip-candidates").hidden = !images.length;
  $("clip-candidates").innerHTML = images.map((img, index) => {
    const at = clipPicks.indexOf(img);
    return `<div class="clip-candidate-box">
      <button type="button" class="clip-candidate ${at < 0 ? "" : "selected"}" data-candidate="${index}" aria-pressed="${at >= 0}" title="${esc(img.alt || img.url)}"><img src="${esc(img.url)}" alt="${esc(img.alt || "Candidate image")}" loading="lazy" referrerpolicy="no-referrer"><span class="clip-candidate-tick" aria-hidden="true">${at < 0 ? "" : at + 1}</span></button>
      <button type="button" class="clip-candidate-zoom" data-preview="${index}" title="See this image large" aria-label="See this image large">⤢</button>
      <button type="button" class="clip-candidate-x" data-dismiss="${index}" title="Remove this image from the choices" aria-label="Remove this image from the choices">×</button>
    </div>`;
  }).join("");
  $("clip-candidates").querySelectorAll("img").forEach((img) => img.addEventListener("error", () => { img.closest(".clip-candidate-box").hidden = true; }));
}

// Keep or drop one picture, by the object itself: the grid is re-rendered around it.
function toggleClipPick(image) {
  if (!image) return;
  const at = clipPicks.indexOf(image);
  if (at < 0) clipPicks.push(image); else clipPicks.splice(at, 1);
  if (image.alt && !$("clip-title").value && clipPicks.length === 1) $("clip-title").value = image.alt;
  renderClipCandidates();
  updateClipChosen();
}

// A page handed over by the bookmarklet: everything its in-browser scan read, validated
// down to plain capped strings before any of it reaches the dialog.
function openClipFromPage(page) {
  const text = (value, max) => (typeof value === "string" ? value : "").slice(0, max).trim();
  const result = { title: text(page.title, 200), site: text(page.site, 200),
                   description: text(page.description, 2000), price: text(page.price, 50),
                   brand: text(page.brand, 200), model: text(page.model, 100),
                   finish: text(page.finish, 200), size: text(page.size, 200),
                   materials: text(page.materials, 500), source_url: text(page.url, 2000),
                   images: (Array.isArray(page.images) ? page.images : []).slice(0, 60)
                     .map((img) => ({ url: text(img && img.url, 2000), alt: text(img && img.alt, 200) }))
                     .filter((img) => /^https?:/.test(img.url)) };
  state.view = "catalog";
  render();
  openClip();
  $("clip-page-url").value = result.source_url;
  showInspected(result, "read from your browser on");
}

async function saveClip() {
  const fail = (message) => { $("clip-error").textContent = message; $("clip-error").hidden = false; };
  const collectionChoice = $("clip-collection").value;
  const newName = collectionChoice === "__new" ? $("clip-new-collection").value.trim() : "";
  const collection = collectionChoice === "__new" ? "" : collectionChoice;
  $("clip-error").hidden = true;
  if (!collection && !newName) { fail("Choose a collection, or name a new one."); return; }
  const meta = { title: $("clip-title").value.trim(), description: $("clip-description").value.trim(),
                 notes: $("clip-notes").value.trim(), tags: $("clip-tags").value.trim(),
                 category: $("clip-category").value, materials: clipInspected?.materials || "",
                 price: $("clip-price").value.trim(), brand: $("clip-brand").value.trim(),
                 model: $("clip-model").value.trim(), finish: $("clip-finish").value.trim(),
                 size: $("clip-size").value.trim(), qty_unit: $("clip-qty").value.trim(),
                 source_url: $("clip-link").value.trim() };
  let request;
  if (clipTab === "upload") {
    if (!clipFile) { fail("Choose an image file to upload."); return; }
    if (clipFile.size > 8 * 1024 * 1024) { fail("Choose a file smaller than 8 MB."); return; }
    const data = new FormData();
    data.append("file", clipFile);
    for (const [key, value] of Object.entries(meta)) data.append(key, value);
    data.append("collection", collection);
    data.append("new_collection", newName);
    request = api("/api/catalog/items/upload", data);
  } else {
    const body = { ...meta, collection, new_collection: newName };
    if (clipTab === "page") {
      if (!clipPicks.length) { fail("Click the pictures you want to save — one, or as many as you like."); return; }
      body.image_urls = clipPicks.map((image) => image.url);
      body.source_url = body.source_url || $("clip-page-url").value.trim();
      body.source_site = clipInspected?.site || "";
    } else {
      if (!$("clip-image-url").value.trim()) { fail("Paste the image's address first."); return; }
      body.image_url = $("clip-image-url").value.trim();
    }
    request = api("/api/catalog/clip", body);
  }
  setBusy(true, clipCount() > 1 ? `Saving ${clipCount()} inspirations…` : "Saving the inspiration…");
  let result;
  try { result = await request; }
  catch (error) {
    // The save itself was refused — usually the site blocking the image download too.
    // Keep the dialog open and say where to go instead of losing the work to a toast.
    fail(`${error.message} If the site is blocking the app, clip it with the Clip to UH Homes bookmark (Get the clipper), `
       + "or save a screenshot of the picture and use the Upload tab.");
    return;
  } finally { setBusy(false); }
  $("clip-dialog").close();
  state.view = "catalog";
  // Some sites hand over one picture and withhold another; say so rather than quietly saving fewer.
  const saved = Number(result?.saved) || 1, missed = Number(result?.refused) || 0;
  const said = saved === 1 ? "Inspiration saved to your collection." : `${saved} inspirations saved to your collection.`;
  await action(async () => { await refresh(); toast(missed ? `${said} ${pictures(missed)} could not be downloaded.` : said); });
}

// --- Inspiration detail ------------------------------------------------------
let inspOpen = null;

function inspirationHTML(item) {
  const collection = catCollection(item["Collection ID"]);
  const editable = !!collection;        // one shared catalog: whoever can see it can edit it
  const off = editable ? "" : "disabled";
  const colors = String(item.Colors || "").split(",").map((c) => c.trim()).filter(Boolean);
  const comments = cat().comments.filter((c) => c["Item ID"] === item["Item ID"]);
  const user = (cat().user || "").toLowerCase();
  const owner = (collection?.Owner || "").toLowerCase();
  const approvals = cat().approvals;
  const source = item["Source URL"];
  const usedIn = [...new Set((state.data?.selections || []).filter((r) => r["Catalog Item ID"] === item["Item ID"]).map((r) => r["Project ID"]))];
  const usedNames = usedIn.map((id) => state.data.projects.find((p) => p["Project ID"] === id)?.["Project Name"] || id);
  return `<div class="dialog-heading"><div><div class="eyebrow">${esc(collection?.Name || "Inspiration")}</div><h2>${esc(item.Title || "Untitled inspiration")}</h2></div>
      <div class="insp-head-actions"><button type="button" class="insp-fav standalone ${item.Favorite === "Yes" ? "active" : ""}" data-insp-fav aria-pressed="${item.Favorite === "Yes"}" aria-label="Favorite">♥</button><button type="button" class="icon-button close-dialog" aria-label="Close dialog">×</button></div></div>
    <div class="insp-layout">
      <div class="insp-media">
        <a href="${esc(catImage(item["Image File"]))}" target="_blank" rel="noopener"><img src="${esc(catImage(item["Image File"]))}" alt="${esc(item.Title || "Saved inspiration")}"></a>
        ${colors.length ? `<div class="insp-colors" aria-label="Color palette">${colors.map((c) => `<span class="color-dot" style="background:${esc(c)}" title="${esc(c)}"></span>`).join("")}</div>` : ""}
        <p class="insp-meta">${[item["Source Site"], item["Saved By"] ? `saved by ${item["Saved By"]}` : "", item["Saved On"] ? `on ${String(item["Saved On"]).slice(0, 10)}` : ""].filter(Boolean).map(esc).join(" · ") || "Saved to your inspirations"}</p>
        <div class="insp-links">${source ? `<a class="button secondary" href="${esc(source)}" target="_blank" rel="noopener noreferrer">Open original ↗</a>` : ""}<a class="button secondary" href="${esc(catImage(item["Image File"]))}?download=1">Download</a>${editable ? '<button type="button" class="button secondary" data-insp-cover>Use as collection cover</button>' : ""}<button type="button" class="button secondary" data-insp-assign>+ Add to projects</button></div>
        <p class="insp-meta insp-projects">${usedIn.length ? `In ${usedIn.length} project${usedIn.length === 1 ? "" : "s"}: ${usedNames.map(esc).join(", ")}` : "Not in any project yet — add it to one to make it a line item."}</p>
      </div>
      <div class="insp-side">
        <div class="form-grid insp-fields">
          <label class="field form-span">Item<input id="insp-title" type="text" maxlength="200" value="${esc(item.Title || "")}" ${off}></label>
          <label class="field">Brand<input id="insp-brand" type="text" maxlength="200" value="${esc(item.Brand || "")}" ${off}></label>
          <label class="field">Model<input id="insp-model" type="text" maxlength="100" value="${esc(item["Model #"] || "")}" ${off}></label>
          <label class="field">Finish / Color<input id="insp-finish" type="text" maxlength="200" value="${esc(item["Finish / Color"] || "")}" ${off}></label>
          <label class="field">Size / Config<input id="insp-size" type="text" maxlength="200" value="${esc(item["Size / Configuration"] || "")}" ${off}></label>
          <label class="field form-span">Link (product page)<input id="insp-link" type="url" maxlength="2000" value="${esc(item["Source URL"] || "")}" ${off}></label>
          <label class="field">Qty / Unit<input id="insp-qty" type="text" maxlength="60" value="${esc(item["Qty / Unit"] || "")}" ${off}></label>
          <label class="field">Unit Price (as clipped)<input id="insp-price" type="text" maxlength="50" value="${esc(item.Price || "")}" ${off}></label>
          <label class="field">Category<select id="insp-category" ${off}>${["", ...cat().categories].map((c) => `<option value="${esc(c)}" ${item.Category === c ? "selected" : ""}>${esc(c || "—")}</option>`).join("")}</select></label>
          <label class="field">Collection<select id="insp-collection" ${off}>${catCollections().map((c) => `<option value="${esc(c["Collection ID"])}" ${c["Collection ID"] === item["Collection ID"] ? "selected" : ""}>${esc(c.Name)}</option>`).join("")}</select></label>
          <label class="field">Feedback<select id="insp-approval" ${off}>${approvals.map((a) => `<option value="${esc(a)}" ${item.Approval === a ? "selected" : ""}>${esc(a || "—")}</option>`).join("")}</select></label>
          <label class="field">Tags<input id="insp-tags" type="text" maxlength="500" value="${esc(item.Tags || "")}" ${off}></label>
          <label class="field form-span">Description<textarea id="insp-description" maxlength="2000" ${off}>${esc(item.Description || "")}</textarea></label>
          <label class="field form-span">My notes<textarea id="insp-notes" maxlength="2000" ${off}>${esc(item.Notes || "")}</textarea></label>
          <label class="field form-span">Materials<input id="insp-materials" type="text" maxlength="500" value="${esc(item.Materials || "")}" ${off}></label>
        </div>
        <div class="insp-ai">
          <button type="button" class="button secondary" data-insp-analyze>✶ ${cat().ai ? "Analyze with AI" : "Suggest category & materials"}</button>
          <div id="insp-ai-result" class="insp-ai-result" hidden></div>
        </div>
        <section class="insp-similar-wrap"><h3>Similar saved designs</h3><div id="insp-similar" class="insp-similar"><span class="muted">Looking…</span></div></section>
        <section class="insp-comments-wrap"><h3>Comments</h3>
          <div id="insp-comments">${comments.map((c) => `<div class="insp-comment"><div><strong>${esc(c.Author || "You")}</strong><small> · ${esc(String(c.Created).slice(0, 16).replace("T", " "))}</small><p>${esc(c.Comment)}</p></div>${(!cat().shared_accounts || (c.Author || "").toLowerCase() === user || owner === user) ? `<button type="button" class="text-button" data-comment-delete="${esc(c["Comment ID"])}">Remove</button>` : ""}</div>`).join("") || '<p class="muted">No comments yet.</p>'}</div>
          <div class="clip-url-row"><input id="insp-comment-text" type="text" maxlength="2000" placeholder="Add a comment for the team…" aria-label="Comment"><button type="button" class="button secondary" data-comment-add>Post</button></div>
        </section>
        <div class="dialog-actions">${editable ? '<button type="button" class="text-button" data-insp-delete>Delete inspiration</button>' : ""}<button type="button" class="button secondary close-dialog">Close</button>${editable ? '<button type="button" class="button primary" data-insp-save>Save changes</button>' : ""}</div>
      </div>
    </div>`;
}

async function openInspiration(id) {
  const item = catItem(id);
  if (!item) return;
  inspOpen = id;
  $("inspiration-body").innerHTML = inspirationHTML(item);
  $("inspiration-dialog").showModal();
  try {
    const result = await api(`/api/catalog/similar?item=${encodeURIComponent(id)}`);
    const similar = result.item_ids.map(catItem).filter(Boolean);
    $("insp-similar").innerHTML = similar.length
      ? similar.map((s) => `<button type="button" class="insp-similar-card" data-insp-open="${esc(s["Item ID"])}" title="${esc(s.Title || "")}"><img src="${esc(itemThumb(s))}" alt="${esc(s.Title || "Similar design")}" loading="lazy"></button>`).join("")
      : '<span class="muted">Nothing similar saved yet — clip more designs and they will appear here.</span>';
  } catch { $("insp-similar").innerHTML = '<span class="muted">Similar designs are unavailable right now.</span>'; }
}

async function saveInspiration() {
  const item = catItem(inspOpen);
  if (!item) return;
  await action(async () => {
    const values = { Title: $("insp-title").value.trim(), Category: $("insp-category").value,
                     "Collection ID": $("insp-collection").value, Approval: $("insp-approval").value,
                     Tags: $("insp-tags").value.trim(), Description: $("insp-description").value.trim(),
                     Notes: $("insp-notes").value.trim(), Materials: $("insp-materials").value.trim(),
                     Price: $("insp-price").value.trim(), Brand: $("insp-brand").value.trim(),
                     "Model #": $("insp-model").value.trim(), "Finish / Color": $("insp-finish").value.trim(),
                     "Size / Configuration": $("insp-size").value.trim(), "Qty / Unit": $("insp-qty").value.trim(),
                     "Source URL": $("insp-link").value.trim() };
    await persist("/api/catalog/items", { item: item["Item ID"], values }, () => !!catItem(item["Item ID"]));
    $("inspiration-dialog").close();
    await refresh();
    toast("Inspiration updated.");
  });
}

async function toggleFavorite(id, fromDialog) {
  const item = catItem(id);
  if (!item) return;
  await action(async () => {
    await persist("/api/catalog/items", { item: id, values: { Favorite: item.Favorite === "Yes" ? "" : "Yes" } }, () => !!catItem(id));
    await refresh();
    if (fromDialog && catItem(id)) { $("inspiration-body").innerHTML = inspirationHTML(catItem(id)); }
  });
}

async function analyzeInspiration() {
  const item = catItem(inspOpen);
  if (!item) return;
  await action(async () => {
    const box = $("insp-ai-result");
    box.hidden = false;
    box.innerHTML = '<span class="muted">Analyzing the image…</span>';
    try {
      const result = await api(`/api/catalog/items/${encodeURIComponent(item["Item ID"])}/analyze`, {});
      const parts = [];
      if (result.category) parts.push(`<div><dt>Category</dt><dd>${esc(result.category)}</dd></div>`);
      if (result.style) parts.push(`<div><dt>Style</dt><dd>${esc(result.style)}</dd></div>`);
      if (result.description) parts.push(`<div><dt>Description</dt><dd>${esc(result.description)}</dd></div>`);
      if (result.materials?.length) parts.push(`<div><dt>Materials</dt><dd>${esc(result.materials.join(", "))}</dd></div>`);
      if (result.tags?.length) parts.push(`<div><dt>Tags</dt><dd>${esc(result.tags.join(", "))}</dd></div>`);
      box.innerHTML = parts.length
        ? `<p class="muted">${result.source === "ai" ? "AI suggestions — review before applying." : "Suggested from your saved details (connect an AI key for image analysis)."}</p><dl class="insp-ai-grid">${parts.join("")}</dl><button type="button" class="button secondary" data-insp-apply>Apply suggestions</button>`
        : '<p class="muted">Nothing could be suggested for this image yet.</p>';
      box.dataset.result = JSON.stringify(result);
    } catch (error) {
      box.innerHTML = `<p class="muted">${esc(error.message)}</p>`;
      throw error;
    }
  });
}

function applyAiSuggestions() {
  const box = $("insp-ai-result");
  let result;
  try { result = JSON.parse(box.dataset.result || "{}"); } catch { return; }
  if (result.category) $("insp-category").value = result.category;
  if (result.description && !$("insp-description").value.trim()) $("insp-description").value = result.description;
  if (result.materials?.length) $("insp-materials").value = [...new Set([...$("insp-materials").value.split(",").map((m) => m.trim()).filter(Boolean), ...result.materials])].join(", ");
  const extra = [...(result.tags || []), ...(result.style ? [result.style] : [])];
  if (extra.length) $("insp-tags").value = [...new Set([...$("insp-tags").value.split(",").map((t) => t.trim()).filter(Boolean), ...extra])].join(", ");
  toast("Suggestions applied — review the fields, then press Save changes.");
}

async function deleteInspiration() {
  const item = catItem(inspOpen);
  if (!item) return;
  if (!confirm(`Delete “${item.Title || "this inspiration"}”? This cannot be undone.`)) return;
  await action(async () => {
    await persist("/api/catalog/items", { item: item["Item ID"], delete: true }, () => !!catItem(item["Item ID"]));
    $("inspiration-dialog").close();
    await refresh();
    toast("Inspiration deleted.");
  });
}

async function setCollectionCover() {
  const item = catItem(inspOpen);
  const collection = item && catCollection(item["Collection ID"]);
  if (!collection) return;
  await action(async () => {
    await persist("/api/catalog/collections", { values: { "Collection ID": collection["Collection ID"], Name: collection.Name,
      Description: collection.Description || "", "Cover Image": item["Thumb File"] || item["Image File"] } });
    await refresh();
    toast(`“${item.Title || "This image"}” is now the cover of ${collection.Name}.`);
  });
}

// --- Catalog → project assignment --------------------------------------------
// One catalog item, many projects: each assignment writes that project its own Selections
// row referencing the Catalog Item ID. The master record is reused, never duplicated, and
// removing a line item later never touches the catalog.
let assignItemId = null;

function openAssign(id) {
  const item = catItem(id);
  if (!item || !state.data) return;
  if (!state.data.projects.length) { toast("Add a project first, then assign catalog items to it.", true); return; }
  assignItemId = id;
  const used = new Set(state.data.selections.filter((r) => r["Catalog Item ID"] === id).map((r) => r["Project ID"]));
  $("assign-title").textContent = `Add “${item.Title || "Untitled"}” to projects`;
  $("assign-projects").innerHTML = state.data.projects.map((p) => `<label class="assign-project"><input type="checkbox" value="${esc(p["Project ID"])}"><span>${esc(p["Project ID"])} — ${esc(p["Project Name"] || "")}</span>${used.has(p["Project ID"]) ? '<small class="muted">already a line item there</small>' : ""}</label>`).join("");
  $("assign-room").value = "";
  $("assign-qty").value = "1";
  $("assign-error").hidden = true;
  $("assign-dialog").showModal();
}

async function saveAssign() {
  if (!catItem(assignItemId)) return;
  const chosen = Array.from($("assign-projects").querySelectorAll("input:checked"), (box) => box.value);
  await action(async () => {
    $("assign-error").hidden = true;
    if (!chosen.length) {
      $("assign-error").textContent = "Tick at least one project.";
      $("assign-error").hidden = false;
      return;
    }
    const values = { "Room / Area": $("assign-room").value.trim(), Qty: $("assign-qty").value.trim() || "1" };
    await persist(`/api/catalog/items/${encodeURIComponent(assignItemId)}/assign`,
                  { projects: chosen, values }, () => !!catItem(assignItemId));
    $("assign-dialog").close();
    await refresh();
    if ($("inspiration-dialog").open && catItem(inspOpen)) $("inspiration-body").innerHTML = inspirationHTML(catItem(inspOpen));
    toast(`Added to ${chosen.length} project${chosen.length === 1 ? "" : "s"} — each has its own line item now.`);
  });
}

// --- The tracker's catalog picker ---------------------------------------------
let pickerSelected = new Set();

function renderPicker() {
  const term = $("picker-search").value.trim().toLowerCase();
  const items = catItems().filter((i) => !term || ["Title", "Tags", "Materials", "Brand", "Category", "Description"]
    .some((f) => String(i[f] || "").toLowerCase().includes(term)));
  const used = new Set(state.data.selections.filter((r) => r["Project ID"] === state.project && r["Catalog Item ID"]).map((r) => r["Catalog Item ID"]));
  $("picker-grid").innerHTML = items.map((i) => `<button type="button" class="picker-card ${pickerSelected.has(i["Item ID"]) ? "selected" : ""}" data-pick="${esc(i["Item ID"])}"><img src="${esc(itemThumb(i))}" alt="${esc(i.Title || "Saved item")}" loading="lazy"><span class="picker-name">${esc(i.Title || "Untitled")}</span><span class="picker-sub">${[i.Brand, i.Price].filter(Boolean).map(esc).join(" · ")}</span>${used.has(i["Item ID"]) ? '<span class="picker-used">In this project</span>' : ""}</button>`).join("")
    || '<p class="muted">Nothing matches that search.</p>';
  $("picker-count").textContent = pickerSelected.size ? `${pickerSelected.size} selected` : "";
  $("picker-add").disabled = !pickerSelected.size || state.busy;
}

function openCatalogPicker() {
  if (state.busy || !state.data) return;
  if (!state.project) { toast("Choose a project first.", true); return; }
  if (!cat().items.length) { toast("Your catalog is empty — clip or upload an inspiration first.", true); return; }
  pickerSelected = new Set();
  $("picker-search").value = "";
  $("picker-error").hidden = true;
  const project = state.data.projects.find((p) => p["Project ID"] === state.project);
  $("picker-title").textContent = `Add from catalog to ${project?.["Project Name"] || state.project}`;
  renderPicker();
  $("catalog-picker-dialog").showModal();
}

async function addPickedToProject() {
  const chosen = [...pickerSelected].filter(catItem);
  if (!chosen.length || !state.project) return;
  await action(async () => {
    setBusy(true, "Adding catalog items to the project…");
    try {
      for (const id of chosen) {
        await persist(`/api/catalog/items/${encodeURIComponent(id)}/assign`,
                      { projects: [state.project] }, () => !!catItem(id));
      }
      $("catalog-picker-dialog").close();
      await refresh();
      toast(`${chosen.length} catalog item${chosen.length === 1 ? "" : "s"} added to ${state.project}.`);
    } finally { setBusy(false); }
  });
}

async function addComment() {
  const item = catItem(inspOpen);
  const text = $("insp-comment-text").value.trim();
  if (!item || !text) return;
  await action(async () => {
    await persist("/api/catalog/comments", { item: item["Item ID"], text }, () => !!catItem(item["Item ID"]));
    await refresh();
    if (catItem(inspOpen)) $("inspiration-body").innerHTML = inspirationHTML(catItem(inspOpen));
  });
}

// --- AI assistant ------------------------------------------------------------
async function askAssistant() {
  const question = $("assistant-question").value.trim();
  if (!question) return;
  await action(async () => {
    $("assistant-ask").disabled = true;
    try {
      const result = await api("/api/catalog/assistant", { question });
      $("assistant-answer").textContent = result.answer;
      $("assistant-answer").hidden = false;
      const items = result.item_ids.map(catItem).filter(Boolean);
      $("assistant-results").innerHTML = items.map((i) => `<button type="button" class="insp-similar-card" data-insp-open="${esc(i["Item ID"])}" title="${esc(i.Title || "")}"><img src="${esc(itemThumb(i))}" alt="${esc(i.Title || "Saved design")}" loading="lazy"><small>${esc(i.Title || "Untitled")}</small></button>`).join("");
    } finally { $("assistant-ask").disabled = false; }
  });
}

// --- Wiring ------------------------------------------------------------------
$("new-collection").addEventListener("click", () => openCollectionEditor(null));
$("collection-form").addEventListener("submit", saveCollection);
$("collection-delete").addEventListener("click", () => action(deleteCollection));
$("catalog-add").addEventListener("click", () => openClip());
$("catalog-empty-add").addEventListener("click", () => openClip());
$("catalog-search").addEventListener("input", renderCatalog);
$("catalog-category").addEventListener("change", renderCatalog);
$("catalog-favs").addEventListener("click", () => {
  catFavsOnly = !catFavsOnly;
  $("catalog-favs").classList.toggle("active", catFavsOnly);
  $("catalog-favs").setAttribute("aria-pressed", String(catFavsOnly));
  renderCatalog();
});
$("collection-strip").addEventListener("click", (event) => {
  const card = event.target.closest("[data-collection]");
  if (!card || state.busy) return;
  if (catCollectionId === card.dataset.collection && card.dataset.collection) {
    const collection = catCollection(card.dataset.collection);
    if (collection) { openCollectionEditor(collection); return; }
  }
  catCollectionId = card.dataset.collection;
  renderCatalog();
});
$("collection-strip").addEventListener("dblclick", (event) => {
  const card = event.target.closest("[data-collection]");
  const collection = card && catCollection(card.dataset.collection);
  if (collection) openCollectionEditor(collection);
});
$("catalog-gallery").addEventListener("click", (event) => {
  const fav = event.target.closest("[data-fav]");
  if (fav) { toggleFavorite(fav.dataset.fav, false); return; }
  const card = event.target.closest("[data-insp]");
  if (card) openInspiration(card.dataset.insp);
});
$("catalog-gallery").addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  const card = event.target.closest("[data-insp]");
  if (card) openInspiration(card.dataset.insp);
});
document.querySelectorAll("[data-clip-tab]").forEach((button) => button.addEventListener("click", () => setClipTab(button.dataset.clipTab)));
$("clip-inspect").addEventListener("click", inspectPage);
$("clip-page-url").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); inspectPage(); } });
// A tile's ⤢ opens the lightbox; "Keep this image" adds that picture to the save list.
let clipPreview = -1;

function showClipPreview(index) {
  const images = clipInspected?.images || [];
  if (!images.length) return;
  clipPreview = ((index % images.length) + images.length) % images.length;
  const image = images[clipPreview];
  $("clip-lightbox-caption").textContent = image.alt || image.url;
  $("clip-lightbox-image").alt = image.alt || "Image preview";
  $("clip-lightbox-image").src = image.url;
  $("clip-lightbox-count").textContent = `${clipPreview + 1} of ${images.length}`;
  if (!$("clip-lightbox").open) $("clip-lightbox").showModal();
}

$("clip-candidates").addEventListener("click", (event) => {
  const cross = event.target.closest("[data-dismiss]");
  if (cross && clipInspected) {
    const [removed] = clipInspected.images.splice(Number(cross.dataset.dismiss), 1);
    const kept = clipPicks.indexOf(removed);
    if (kept >= 0) clipPicks.splice(kept, 1);     // a dropped picture cannot stay on the save list
    renderClipCandidates();
    updateClipChosen();
    if (!clipInspected.images.length) {
      $("clip-page-note").textContent = "Every image was removed — Find images (or the bookmarklet) brings them back.";
      $("clip-page-note").hidden = false;
    }
    return;
  }
  const zoom = event.target.closest("[data-preview]");
  if (zoom) { showClipPreview(Number(zoom.dataset.preview)); return; }
  const button = event.target.closest("[data-candidate]");
  if (button) toggleClipPick(clipInspected?.images[Number(button.dataset.candidate)]);
});
$("clip-lightbox-use").addEventListener("click", () => {
  const image = clipInspected?.images[clipPreview];
  if (image && clipPicks.indexOf(image) < 0) toggleClipPick(image);
  $("clip-lightbox").close();
});
$("clip-lightbox-prev").addEventListener("click", () => showClipPreview(clipPreview - 1));
$("clip-lightbox-next").addEventListener("click", () => showClipPreview(clipPreview + 1));
$("clip-lightbox-close").addEventListener("click", () => $("clip-lightbox").close());
$("clip-lightbox").addEventListener("keydown", (event) => {
  if (event.key === "ArrowLeft") { event.preventDefault(); showClipPreview(clipPreview - 1); }
  if (event.key === "ArrowRight") { event.preventDefault(); showClipPreview(clipPreview + 1); }
});
$("clip-lightbox-image").addEventListener("error", () => {
  if ($("clip-lightbox").open) $("clip-lightbox-caption").textContent = "This image cannot be previewed here — it may still save, or pick another.";
});
$("clip-collection").addEventListener("change", () => { $("clip-new-collection-field").hidden = $("clip-collection").value !== "__new"; });
$("clip-next").addEventListener("click", clipStepForward);
$("clip-back").addEventListener("click", () => setClipStep(Math.max(1, clipStep - 1)));
$("clip-choose-file").addEventListener("click", () => $("catalog-file").click());
$("catalog-file").addEventListener("change", (event) => {
  clipFile = event.target.files[0] || null;
  event.target.value = "";
  $("clip-file-name").textContent = clipFile ? `Selected: ${clipFile.name}` : "";
  updateClipChosen();
});
$("clip-save").addEventListener("click", saveClip);
$("inspiration-dialog").addEventListener("click", (event) => {
  const open = event.target.closest("[data-insp-open]");
  if (open) { openInspiration(open.dataset.inspOpen); return; }
  if (event.target.closest("[data-insp-fav]")) toggleFavorite(inspOpen, true);
  else if (event.target.closest("[data-insp-save]")) saveInspiration();
  else if (event.target.closest("[data-insp-delete]")) deleteInspiration();
  else if (event.target.closest("[data-insp-analyze]")) analyzeInspiration();
  else if (event.target.closest("[data-insp-apply]")) applyAiSuggestions();
  else if (event.target.closest("[data-insp-cover]")) setCollectionCover();
  else if (event.target.closest("[data-insp-assign]")) openAssign(inspOpen);
  else if (event.target.closest("[data-comment-add]")) addComment();
  else if (event.target.closest("[data-comment-delete]")) {
    const id = event.target.closest("[data-comment-delete]").dataset.commentDelete;
    action(async () => {
      await persist("/api/catalog/comments", { comment: id, delete: true });
      await refresh();
      if (catItem(inspOpen)) $("inspiration-body").innerHTML = inspirationHTML(catItem(inspOpen));
    });
  }
});
$("assign-save").addEventListener("click", saveAssign);
$("add-from-catalog").addEventListener("click", openCatalogPicker);
$("picker-search").addEventListener("input", renderPicker);
$("picker-grid").addEventListener("click", (event) => {
  const card = event.target.closest("[data-pick]");
  if (!card || state.busy) return;
  const id = card.dataset.pick;
  if (pickerSelected.has(id)) pickerSelected.delete(id); else pickerSelected.add(id);
  renderPicker();
});
$("picker-add").addEventListener("click", addPickedToProject);
$("catalog-assistant-open").addEventListener("click", () => { $("assistant-answer").hidden = true; $("assistant-results").innerHTML = ""; $("assistant-question").value = ""; $("assistant-dialog").showModal(); $("assistant-question").focus(); });
$("assistant-ask").addEventListener("click", askAssistant);
$("assistant-question").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); askAssistant(); } });
$("assistant-dialog").addEventListener("click", (event) => {
  const open = event.target.closest("[data-insp-open]");
  if (open) { $("assistant-dialog").close(); state.view = "catalog"; render(); openInspiration(open.dataset.inspOpen); }
});
// The clipper is the one downloadable bookmark file: it carries the page scanner and the
// UH icon, which a bookmark only keeps when it is imported rather than dragged. It reads
// the page from inside the browser and posts the result here, so a site whose bot wall
// 403s the server's fetch clips exactly like any other.
$("catalog-clipper-open").addEventListener("click", () => $("clipper-dialog").showModal());
// Dragging the download onto the bar bookmarks this page instead of the clipper, so the
// link refuses to be dragged and says what to do with the file it just saved.
$("clipper-download").addEventListener("dragstart", (event) => {
  event.preventDefault();
  toast("Click this to download the file, then import it — step 1 below.", true);
});
$("clipper-download").addEventListener("click", () => toast("Downloaded. Now import it: Ctrl+Shift+O → ⋮ menu → Import bookmarks."));

refresh().then(() => {
  // An old bookmarklet opens /?clip=<page>: land on the catalog with the server reading that page.
  const params = new URLSearchParams(location.search);
  const clipUrl = params.get("clip");
  if (clipUrl) {
    history.replaceState(null, "", "/");
    state.view = "catalog";
    render();
    openClip(clipUrl);
  }
  // The current bookmarklet opens /?clipwait=1 and posts over what it already read in the
  // browser — no server fetch, so a page behind a bot wall arrives all the same. The
  // payload is validated field by field in openClipFromPage before any of it is shown.
  if (params.has("clipwait")) {
    history.replaceState(null, "", "/");
    window.addEventListener("message", (event) => {
      const page = event.data && event.data.type === "uh-clip-page" && event.data.page;
      if (page && typeof page === "object") openClipFromPage(page);
    });
    window.opener?.postMessage("uh-clip-ready", "*");
  }
}).catch((error) => {
  $("load-error").textContent = error.message;
  $("load-error").hidden = false;
  $("project-list").textContent = "Workspace unavailable";
});
