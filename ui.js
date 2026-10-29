"use strict";

const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="uh-token"]').content;
const state = { data: null, project: "", view: "selections", page: 0, busy: false, cancel: false, editor: null,
                specGroup: "Section", specCollapsed: new Set(), focusSelector: null, hidden: new Set(),
                specPending: new Map(),
                specLinkEdit: new Set() };
const names = { selections: "Selection tracker", spec: "Spec sheet", presentation: "Client presentations", manufacturers: "Manufacturers" };
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const isUrl = (value) => { try { return ["http:", "https:"].includes(new URL(value).protocol); } catch { return false; } };
const selectedProject = () => state.data?.projects.find((p) => p["Project ID"] === state.project);
const projectRows = () => state.data?.selections.filter((r) => r["Project ID"] === state.project) || [];
let toastTimer;

function toast(message, error = false) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").classList.toggle("error", error);
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 11000 : 5000);
}

async function api(path, body, binary = false) {
  const options = body === undefined ? {} : { method: "POST", headers: { "X-UH-Token": token }, body };
  if (body !== undefined && !(body instanceof FormData)) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify({ revision: state.data?.revision, ...body });
  }
  let response;
  try { response = await fetch(path, options); } catch { throw new Error("Cannot reach the app. Make sure it is running, then retry."); }
  if (response.status === 401) {
    // The session ended on the hosted workspace, so collect a new one rather than losing the edit.
    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    throw new Error("Your session has ended. Redirecting you to sign in again.");
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    const error = new Error(detail.error || `Request failed (${response.status}).`);
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

function setBusy(value, message = "") {
  state.busy = value;
  $("busy-banner").hidden = !value;
  $("busy-message").textContent = message;
  document.querySelectorAll("button:not(.close-dialog):not(.sidebar-toggle)").forEach((button) => { button.disabled = value; });
  $("cancel-lookup").disabled = false;
  if (!value && state.data) render();
}

async function action(fn) {
  if (state.busy) return;
  try { await fn(); } catch (error) { toast(error.message, true); }
}

function badge(status, phase) {
  if (phase) return `<span class="status review">${esc({ queued: "Auto queued", searching: "Searching…", saving: "Saving link…", waiting_excel: "Close Excel", waiting_key: "API key needed", retry: "Retry scheduled", failed: "Search paused" }[phase] || "Auto queued")}</span>`;
  const kind = status === "Verified" ? "verified" : ["Not found", "Search error"].includes(status) ? "failed" : status && status !== "Not run" ? "review" : "";
  return `<span class="status ${kind}">${esc(status || "Not run")}</span>`;
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

const PREVIEW_ROWS = 7;

function scheduleDefaults() {
  return state.data?.branding?.schedule || {};
}

function scheduleOptions() {
  const defaults = scheduleDefaults();
  return { title: $("export-title").value.trim() || defaults.title || "Selections",
           prefix: ($("export-prefix").value.trim() || defaults.code_prefix || "EX").toUpperCase(),
           hidden: [...state.hidden] };
}

const isHidden = (column) => state.hidden.has(column);
// Hiding one of these leaves a spec sheet that cannot be matched back to its lines on import.
const CSV_KEY_COLUMNS = ["Section", "Room / Area", "Item"];

function renderHideGrid(id, scope) {
  const grid = $(id);
  const columns = (state.data?.hideable || []).filter((column) => !scope || column.documents.includes(scope));
  if (grid.childElementCount !== columns.length) {
    // built once, so clicking a box never pulls the focus out from under the pointer
    grid.innerHTML = columns.map((column) => `<label><input type="checkbox" data-hide="${esc(column.key)}">${esc(column.label)}${column.documents.includes("schedule") ? "" : " <small>· spec sheet</small>"}</label>`).join("");
  }
  grid.querySelectorAll("[data-hide]").forEach((box) => { box.checked = isHidden(box.dataset.hide); });
  const count = columns.filter((column) => isHidden(column.key)).length;
  $(`${id.replace("-grid", "")}-count`).textContent = count ? `${count} hidden` : "Showing everything";
  const keys = CSV_KEY_COLUMNS.filter(isHidden);
  $(`${id.replace("-grid", "")}-note`).textContent = "Hidden columns are left out of the PDF, the Word and PowerPoint copies, the fillable copies and the spec sheet."
    + (keys.length ? ` A spec sheet without ${keys.join(" or ")} cannot be matched back to these line items when you import it again.` : "");
}

function toggleHidden(key, off) {
  if (off) state.hidden.delete(key); else state.hidden.add(key);
  render();
}

function rowDescription(row) {
  if (row["Description Override"]) return row["Description Override"];
  const shown = (column) => (isHidden(column) ? "" : row[column] || "");
  const product = shown("Product Name") || shown("Model #");
  let identity = [shown("Manufacturer"), product].filter(Boolean).join(" ");
  let finish = shown("Finish / Color");
  if (!product && finish) { identity = [identity, finish].filter(Boolean).join(" "); finish = ""; }
  const model = shown("Model #");
  if (model && product !== model && !identity.toLowerCase().includes(model.toLowerCase())) identity = `${identity} (Model ${model})`.trim();
  const qty = isHidden("Qty") || Number(row.Qty) === 1 || !row.Qty ? "" : `Qty ${row.Qty}`;
  return [identity, finish, qty, shown("Client Notes")].filter(Boolean).join(" \u2013 ");
}

function renderPreview(project, included) {
  const branding = state.data.branding || {}, defaults = scheduleDefaults();
  const { title, prefix } = scheduleOptions();
  $("preview-eyebrow").textContent = (branding.tagline || "Finish Schedule").toUpperCase();
  $("preview-title").textContent = title;
  $("preview-contact").textContent = branding.contact_line || "";
  $("preview-note").textContent = defaults.note || "";
  $("preview-sign").innerHTML = (defaults.signatures || []).map((label) => `<span>${esc(label)}</span>`).join("");
  const plan = [project?.["Project Name"], project?.["Plan / Elevation"]].filter(Boolean).join(" / ");
  $("preview-band").innerHTML = [["CLIENT", project?.["Client Name"]], ["PROJECT / LOT", plan],
                                 ["ADDRESS", project?.Address], ["DATE", project?.["Presentation Date"]?.slice(0, 10)]]
    .map(([label, value]) => `<div><dt>${label}</dt><dd>${esc(value || "")}</dd></div>`).join("");
  const mode = $("export-mode").value;
  const rows = (mode === "verified" ? included.filter((r) => r["Lookup Status"] === "Verified") : included);
  const itemColumn = !(isHidden("Item") && isHidden("Room / Area"));
  const columns = `${isHidden("code") ? " no-code" : ""}${itemColumn ? "" : " no-item"}`;
  let markup = "", section = "";
  rows.slice(0, PREVIEW_ROWS).forEach((row, index) => {
    if (!isHidden("Section") && row.Section !== section) { section = row.Section; markup += `<div class="sheet-section">${esc(section || "Other")}</div>`; }
    // the schedule prints the link after the description, so the paper preview shows it there too
    const link = isUrl(row["Product URL"]) && !isHidden("Product URL") ? ` – <a class="product-link" href="${esc(row["Product URL"])}" target="_blank" rel="noopener noreferrer">Product Link</a>` : "";
    const item = [isHidden("Item") ? "" : esc(row.Item), isHidden("Room / Area") ? "" : esc(row["Room / Area"] || "")].filter(Boolean).join("<br>");
    markup += `<div class="sheet-row${columns}">${isHidden("code") ? "" : `<span>${esc(prefix)}-${String(index + 1).padStart(2, "0")}</span>`}${itemColumn ? `<strong>${item}</strong>` : ""}<em>${esc(rowDescription(row))}${link}</em></div>`;
  });
  if (rows.length > PREVIEW_ROWS) markup += `<div class="sheet-more">+ ${rows.length - PREVIEW_ROWS} more selection${rows.length - PREVIEW_ROWS === 1 ? "" : "s"} in the download</div>`;
  $("preview-rows").innerHTML = markup || '<div class="sheet-more">Add selections to this project to see them here.</div>';
}

function render() {
  if (specDrag) { specRenderPending = true; return; }   // never tear down rows mid-drag
  const data = state.data;
  if (!data) return;
  const project = selectedProject(), rows = projectRows();
  const verified = rows.filter((r) => r["Lookup Status"] === "Verified").length;
  const stale = rows.filter((r) => r._stale).length;
  const linked = rows.filter((r) => isUrl(r["Product URL"])).length;
  const included = rows.filter((r) => r["Include in Lookbook"].toLowerCase() !== "no");
  const bare = state.view === "spec";   // the spec sheet stands on its own
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
    const entry = (label, control, isCustom) => `<div class="detail-entry ${isCustom ? "custom-detail" : ""}">${label}<dd>${control}</dd></div>`;
    $("project-detail-values").innerHTML = standard.map((name) => {
      const date = name === "Presentation Date";
      const value = date ? (project[name] || "").slice(0, 10) : project[name] || "";
      return entry(`<dt>${esc(name)}</dt>`,
        `<input class="cell" data-project-field="${esc(name)}" type="${date ? "date" : "text"}" value="${esc(value)}" maxlength="4000" placeholder="Not provided">`);
    }).join("") + custom.map((field, index) => entry(
      `<dt><input class="cell cell-label" data-custom-index="${index}" data-custom-key="name" value="${esc(field.name)}" maxlength="80" aria-label="Field name"></dt>`,
      `<input class="cell" data-custom-index="${index}" data-custom-key="value" value="${esc(field.value)}" maxlength="2000" placeholder="Not provided" aria-label="${esc(field.name)}">`, true)).join("");
    $("project-custom-empty").hidden = custom.length > 0;
  }
  if (project) {
    $("project-id").textContent = project["Project ID"];
    $("project-name").textContent = project["Project Name"];
    $("project-address").textContent = project["Address"] || "Add an address in project details";
    $("project-date").textContent = project["Presentation Date"] ? `Presentation · ${project["Presentation Date"].slice(0, 10)}` : "Presentation date not set";
    $("project-client").textContent = [project["Client Name"] ? `Prepared for ${project["Client Name"]}` : "", project["Plan / Elevation"]].filter(Boolean).join("   /   ");
  }
  const priced = rows.filter((r) => Number(r["Unit Price"]) > 0);
  const budget = priced.reduce((sum, r) => sum + lineTotal(r), 0);
  $("stats").innerHTML = [["Total selections", rows.length, `${new Set(rows.map((r) => r.Section || "Other")).size} categories`], ["Product links", linked, "found or added"], ["Needs review", rows.length - verified, "before presenting"], ["Verified selections", verified, `${rows.length ? Math.round(verified / rows.length * 100) : 0}% complete${stale ? ` · ${stale} stale` : ""}`], ["Priced total", budget.toLocaleString(undefined, { maximumFractionDigits: 0 }), `${priced.length} of ${rows.length} priced`]].map(([label, value, note]) => `<div class="stat"><div class="stat-label">${label}</div><div class="stat-value"><strong>${value}</strong><span>${note}</span></div></div>`).join("");
  $("page-title").textContent = names[state.view];
  // the spec sheet is the standard specification, so it is not labelled with one project
  $("breadcrumb-name").textContent = project && !["manufacturers", "spec"].includes(state.view)
    ? `${project["Project Name"]} / ${names[state.view]}` : names[state.view];
  $("page-subtitle").textContent = { selections: "Every material. Every finish. All in one place.", spec: "The full specification, grouped the way you work.", presentation: "From your workbook to the client’s finish schedule.", manufacturers: "Keep your trusted brands and official websites together." }[state.view];
  // the spec sheet edits the same rows the schedule prints, so name the home it belongs to
  if (state.view === "spec" && project) $("page-subtitle").textContent = `Specifying ${project["Project Name"]} — these line items are what the client schedule prints.`;
  document.querySelectorAll("[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === state.view));
  document.querySelectorAll(".view").forEach((view) => { view.hidden = view.id !== `${state.view}-view`; });
  $("add-selection").disabled = !project || state.busy;
  $("find-links").disabled = !project || state.busy;
  const oldSection = $("section-filter").value;
  $("section-filter").innerHTML = '<option value="">All categories</option>' + [...new Set([...data.sections, ...rows.map((r) => r.Section).filter(Boolean)])].map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  $("section-filter").value = oldSection;
  $("selection-count").textContent = rows.length;
  renderTable();
  renderSpec();
  $("export-title").placeholder = scheduleDefaults().title || "Selections";
  $("export-prefix").placeholder = scheduleDefaults().code_prefix || "EX";
  renderHideGrid("export-hide-grid");
  renderHideGrid("schedule-hide-grid", "schedule");
  renderPreview(project, included);
  const mode = $("export-mode").value;
  const count = mode === "verified" ? included.filter((r) => r["Lookup Status"] === "Verified").length : included.length;
  const pending = included.filter((r) => r["Lookup Status"] !== "Verified").length;
  $("export-summary").textContent = `${count} selections will be included. ${rows.length - included.length} hidden from presentations.` + (mode === "verified" ? ` ${pending} unverified selections will be omitted.` : pending ? ` ${pending} still need review${mode === "final" ? " — final export is blocked until verified" : "; draft labels will be shown"}.` : " All included links are verified.");
  const automaticPending = included.some((r) => r._auto_phase);
  if (automaticPending && mode !== "verified") $("export-summary").textContent += " Export waits for automatic lookup so stale product links cannot be included.";
  if (stale) $("export-summary").textContent += ` ${stale} verified link${stale === 1 ? " is" : "s are"} over 6 months old — worth a re-check before presenting.`;
  $("export-pdf").disabled = !project || !count || state.busy || (mode === "final" && pending > 0) || (mode !== "verified" && automaticPending);
  $("export-more").disabled = !project || state.busy;
  $("manufacturer-grid").innerHTML = data.manufacturers.map((m, index) => `<article class="manufacturer-card"><h3>${esc(m.Manufacturer)}</h3><p>${esc(m["Official Domain"])}</p><p>${esc(m.Notes || "Official product source")}</p><button class="text-button" data-manufacturer="${index}">Edit manufacturer ↗</button></article>`).join("") || '<div class="empty-state"><h3>Add your first manufacturer</h3><p>Enter the brand and its official website domain.</p></div>';
  $("workbook-name").textContent = data.workbook;
  $("provider-status").textContent = data.provider.ready ? `${data.provider.name} is configured. Live requests may use paid API quota.` : `Search is not configured. Required: ${data.provider.key_name}. You can still manage selections, enter links manually, and export presentations.`;
  attachImageErrors();
  restoreFocus();
}

function renderTable() {
  const query = $("search").value.trim().toLowerCase(), category = $("section-filter").value, status = $("status-filter").value;
  const rows = projectRows().filter((r) => (!category || r.Section === category) && (!query || [r.Item, r.Manufacturer, r["Model #"], r["Room / Area"], r["Finish / Color"], ...(r.custom_fields || []).flatMap((field) => [field.name, field.value])].join(" ").toLowerCase().includes(query)) && (!status || (status === "review" ? r["Lookup Status"] !== "Verified" : status === "missing" ? !isUrl(r["Product URL"]) : r["Lookup Status"] === status)));
  state.page = Math.min(state.page, Math.max(0, Math.ceil(rows.length / 50) - 1));
  const start = state.page * 50;
  $("selection-rows").innerHTML = rows.slice(start, start + 50).map((r) => `<tr><td><div class="item-cell">${thumb(r)}<div><strong>${esc(r.Item || "Untitled selection")}</strong><small>${esc([r.Section, r["Room / Area"]].filter(Boolean).join(" / "))}${r["Include in Lookbook"].toLowerCase() === "no" ? " · Hidden" : ""}</small>${selectionDetails(r)}</div></div></td><td><strong>${esc(r.Manufacturer || "Manufacturer pending")}</strong><small>${esc(r["Model #"] || "Model pending")}</small></td><td><strong>${esc(r["Finish / Color"] || "—")}</strong><small>${r.Qty ? `Qty: ${esc(r.Qty)}` : "Quantity not set"}</small></td><td>${isUrl(r["Product URL"]) ? `<a class="product-link" href="${esc(r["Product URL"])}" target="_blank" rel="noopener noreferrer" title="${esc(r["Product URL"])}">Product Link ↗</a>` : '<span class="muted">Not linked</span>'}</td><td>${badge(r["Lookup Status"], r._auto_phase)}${r._stale ? ' <span class="status review">Re-check</span>' : ""}${r["Client Status"] && r["Client Status"] !== "Proposed" ? ` <small class="muted">Client: ${esc(r["Client Status"])}</small>` : ""}</td><td><button class="row-action" data-edit="${r._row}" aria-label="Edit ${esc(r.Item)}">Edit ↗</button></td></tr>`).join("");
  $("table-empty").hidden = rows.length > 0;
  $("table-summary").textContent = rows.length ? `Showing ${start + 1}–${Math.min(start + 50, rows.length)} of ${rows.length} selections` : "0 selections";
  $("previous-page").disabled = !state.page || state.busy;
  $("next-page").disabled = start + 50 >= rows.length || state.busy;
  attachImageErrors();
}

const SPEC_COLUMNS = [
  { field: "Item", label: "ITEM", placeholder: "Name the selection" },
  { field: "Manufacturer", label: "BRAND", placeholder: "Brand" },
  { field: "Model #", label: "MODEL", placeholder: "Model / SKU" },
  { field: "Finish / Color", label: "FINISH / COLOR", placeholder: "Finish" },
  // the product URL is stored as it always was; this column is where it is pasted and read
  { field: "Product URL", label: "PRODUCT LINK", placeholder: "Paste product URL", link: true },
  { field: "Qty", label: "QTY", placeholder: "1", number: true },
  { field: "Unit Price", label: "UNIT PRICE", placeholder: "—", number: true },
  { field: "Markup %", label: "MARKUP %", placeholder: "0", number: true },
];

const money = (value) => value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const lineTotal = (row) => (Number(row["Unit Price"]) || 0) * (Number(row.Qty) || 1) * (1 + (Number(row["Markup %"]) || 0) / 100);

function specGroups(rows) {
  const key = state.specGroup;
  const groups = new Map();
  for (const row of rows) groups.set(row[key] || (key === "Section" ? "Other" : "Unassigned"), []);
  for (const row of rows) groups.get(row[key] || (key === "Section" ? "Other" : "Unassigned")).push(row);
  if (key === "Section") {
    const order = state.data.sections;
    return [...groups].sort((a, b) => (order.indexOf(a[0]) + 1 || 99) - (order.indexOf(b[0]) + 1 || 99));
  }
  return [...groups];
}

function renderSpec() {
  if (specDrag) { specRenderPending = true; return; }   // never tear down rows mid-drag
  document.querySelectorAll("#spec-view .chip").forEach((chip) => chip.classList.toggle("active", chip.dataset.group === state.specGroup));
  const query = $("spec-filter").value.trim().toLowerCase();
  const all = projectRows();
  const rows = all.filter((r) => !query || [r.Item, r.Manufacturer, r["Model #"], r["Finish / Color"], r["Room / Area"], r.Section, r["Client Notes"]].join(" ").toLowerCase().includes(query));
  const groups = specGroups(rows);
  $("spec-summary").textContent = all.length
    ? `${groups.length} ${state.specGroup === "Section" ? "categories" : "rooms"} · ${rows.length} line item${rows.length === 1 ? "" : "s"}${rows.length !== all.length ? ` of ${all.length}` : ""}`
    : "";
  $("spec-summary").hidden = state.view !== "spec" || !all.length;
  $("spec-add").disabled = !selectedProject() || state.busy;
  if (!all.length) {
    $("spec-groups").innerHTML = '<div class="empty-state"><div class="empty-icon">▤</div><h3>Nothing specified yet</h3><p>Add your first line item to start building this home’s specification.</p></div>';
    $("spec-total").textContent = "—";
    specSaveUI();
    return;
  }
  $("spec-groups").innerHTML = groups.map(([name, items]) => {
    const total = items.reduce((sum, r) => sum + lineTotal(r), 0);
    const collapsed = state.specCollapsed.has(name);
    const body = items.map((r) => `<tr data-row="${r._row}" draggable="true"><td class="drag-col"><span class="drag-handle" title="Drag row to reorder" aria-hidden="true">⠿</span></td>${SPEC_COLUMNS.map((c) => {
      const pending = state.specPending.get(r._row) || {};   // unsaved edits outlive every re-render
      const edited = c.field in pending;
      const value = String(edited ? pending[c.field] : r[c.field] ?? "");
      // a saved address shows the words "Product Link", never the raw URL; the pencil reopens the input
      if (c.link && isUrl(value) && !state.specLinkEdit.has(r._row)) {
        return `<td class="link-col"><div class="link-cell${edited ? " dirty" : ""}"><a class="product-link" href="${esc(value)}" target="_blank" rel="noopener noreferrer" title="${esc(value)}">Product Link ↗</a><button type="button" class="icon-button link-edit" data-link-edit="${r._row}" aria-label="View or change the product link for ${esc(r.Item)}" title="View or change the address"${r._auto_phase ? " disabled" : ""}>✎</button></div></td>`;
      }
      return `<td${c.number ? ' class="num"' : c.link ? ' class="link-col"' : ""}><input class="cell${c.number ? " num" : ""}${edited ? " dirty" : ""}" data-row="${r._row}" data-field="${esc(c.field)}" value="${esc(value)}" placeholder="${esc(c.placeholder)}"${c.number ? ' type="number" min="0" step="any"' : ` maxlength="4000"`}${r._auto_phase ? " disabled" : ""}></td>`;
    }).join("")}<td><div class="spec-actions"><button class="text-button" data-edit="${r._row}" aria-label="Open ${esc(r.Item)}">Open</button><button class="icon-button" data-spec-remove="${r._row}" aria-label="Remove ${esc(r.Item)}">×</button></div></td></tr>`).join("");
    return `<article class="spec-card${collapsed ? " collapsed" : ""}" data-spec-name="${esc(name)}">
      <button type="button" class="spec-card-head" data-spec-toggle="${esc(name)}" aria-expanded="${!collapsed}"><span class="twist" aria-hidden="true">▾</span><h3>${esc(name)}</h3><span class="count">${items.length} item${items.length === 1 ? "" : "s"}</span><span class="group-total">${total ? money(total) : ""}</span></button>
      <div class="spec-body"><table class="spec-table"><thead><tr><th class="drag-col"><span class="sr-only">Reorder</span></th>${SPEC_COLUMNS.map((c) => `<th${c.number ? ' class="num"' : ""}>${c.label}</th>`).join("")}<th><span class="sr-only">Actions</span></th></tr></thead><tbody>${body}</tbody></table></div>
      <div class="spec-foot"><button class="text-button" data-spec-add="${esc(name)}">+ Add to ${esc(name)}</button><span class="muted">${items.filter((r) => r["Lookup Status"] === "Verified").length} of ${items.length} verified</span></div>
    </article>`;
  }).join("");
  const grand = rows.reduce((sum, r) => sum + lineTotal(r), 0);
  $("spec-total").textContent = grand ? money(grand) : "—";
  specSaveUI();
}

let specDrag = null, specPress = null, specRenderPending = false;

function clearDropMarks() {
  document.querySelectorAll(".drop-before,.drop-after,.drop-end,.dragging").forEach((el) => el.classList.remove("drop-before", "drop-after", "drop-end", "dragging"));
}

function specDragEvents() {
  const zone = $("spec-groups");
  zone.addEventListener("mousedown", (event) => { specPress = event.target; }, true);
  zone.addEventListener("dragstart", (event) => {
    const tr = event.target;
    if (!tr.matches || !tr.matches("tr[data-row]")) return;   // child drag such as selected input text
    if (state.busy || (specPress && specPress.closest("input,textarea,select,button,a"))) {
      event.preventDefault();   // presses on cells/buttons keep their normal behavior
      return;
    }
    specDrag = { row: Number(tr.dataset.row), group: tr.closest(".spec-card")?.dataset.specName };
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(specDrag.row));
    requestAnimationFrame(() => tr.classList.add("dragging"));
  });
  zone.addEventListener("dragover", (event) => {
    if (!specDrag) return;
    const card = event.target.closest(".spec-card");
    if (!card) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    clearDropMarks();
    const tr = event.target.closest("tr[data-row]");
    if (tr && Number(tr.dataset.row) !== specDrag.row) {
      const rect = tr.getBoundingClientRect();
      tr.classList.add(event.clientY < rect.top + rect.height / 2 ? "drop-before" : "drop-after");
    } else if (!tr) {
      card.classList.add("drop-end");
    }
  });
  zone.addEventListener("drop", (event) => {
    if (!specDrag) return;
    const card = event.target.closest(".spec-card");
    if (card) event.preventDefault();
    const tr = event.target.closest("tr[data-row]");
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
    const last = card?.querySelector("tbody tr[data-row]:last-of-type");
    index = last ? order.indexOf(Number(last.dataset.row)) + 1 : order.length;
  } else {
    index = order.indexOf(target) + (after ? 1 : 0);
    if (index < 0) index = order.length;
  }
  order.splice(index, 0, from);
  const updates = {};
  const targetGroup = card?.dataset.specName;
  if (targetGroup && state.data) {
    const dragged = projectRows().find((r) => r._row === from);
    const current = dragged?.[state.specGroup] || (state.specGroup === "Section" ? "Other" : "Unassigned");
    if (targetGroup !== current) {
      updates[from] = { [state.specGroup]: ["Other", "Unassigned"].includes(targetGroup) ? "" : targetGroup };
    }
  }
  if (!Object.keys(updates).length && order.every((row, i) => row === all[i])) return;
  action(async () => {
    const payload = { project: state.project, order, updates };
    try {
      await api("/api/selections/order", payload);
    } catch (error) {
      if (error.status !== 409) throw error;
      // AutoLookup may have saved between the last poll and this drop; the same
      // swap is still valid against the new revision, so refresh and retry once.
      await refresh();
      await api("/api/selections/order", payload);
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
  await api("/api/projects", { values: { "Project ID": project["Project ID"], [field]: value } });
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
  await api("/api/projects", { values: { "Project ID": project["Project ID"] }, custom_fields: fields });
  await refresh();
  toast("Saved.");
}

function dropHeldSpecEdit(row, field) {
  const held = state.specPending.get(row);
  if (!held) return;
  delete held[field];   // a confirmed save covers whatever this row and field held
  if (!Object.keys(held).length) state.specPending.delete(row);
  specSaveUI();
}

async function saveCell(input) {
  const row = Number(input.dataset.row), field = input.dataset.field;
  const record = projectRows().find((r) => r._row === row);
  if (!record) return;
  const value = input.value.trim();
  if (String(record[field] ?? "") === value) { dropHeldSpecEdit(row, field); return; }
  if (field === "Item" && !value) { toast("An item needs a name.", true); renderSpec(); return; }
  input.classList.add("dirty");
  state.focusSelector = `#spec-groups .cell[data-row="${row}"][data-field="${CSS.escape(field)}"]`;
  try {
    const result = await api("/api/selections", { row, values: { [field]: value } });
    dropHeldSpecEdit(row, field);
    await refresh();
    toast(result.queued ? "Saved. Automatic product lookup is queued for this change." : "Saved.");
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
  if (options.choices) control = `<select ${attrs}>${options.choices.map((choice) => `<option value="${esc(choice)}" ${choice === value ? "selected" : ""}>${esc(choice)}</option>`).join("")}</select>`;
  else if (options.textarea) control = `<textarea ${attrs} aria-label="${esc(name)}" maxlength="4000">${esc(value)}</textarea>`;
  else control = `<input ${attrs} type="${options.type || "text"}" value="${esc(value)}" maxlength="${options.max || 4000}" ${options.type === "number" ? 'min="0" max="1000000" step="any"' : ""}>`;
  return `<label class="field ${options.wide ? "wide" : ""}">${esc(name)}${options.required ? " *" : ""}${control}</label>`;
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
    fields = field("Project ID", r["Project ID"], { required: true, readonly: !!record, max: 50 }) + field("Project Name", r["Project Name"], { required: true, max: 200 }) + field("Client Name", r["Client Name"]) + field("Presentation Date", r["Presentation Date"]?.slice(0, 10), { type: "date" }) + field("Address", r.Address, { wide: true }) + field("Plan / Elevation", r["Plan / Elevation"]) + field("Designer", r.Designer) + field("Cover Image", r["Cover Image"], { wide: true });
    note = record ? "Cover Image accepts a public image URL or a local file path. Each project gets its own presentation."
      : "Use a unique Project ID, such as UH-104. A new project starts with the standard room-by-room line items, ready for you to specify. Cover Image accepts a public image URL or a local file path.";
  } else if (kind === "selection") {
    fields = field("Item", r.Item, { required: true }) + field("Section", r.Section || state.data.sections[0] || "Other", { choices: [...new Set([...state.data.sections, r.Section || "Other"])] }) + field("Room / Area", r["Room / Area"]) + field("Qty", r.Qty, { type: "number" }) + field("Unit Price", r["Unit Price"], { type: "number" }) + field("Markup %", r["Markup %"], { type: "number" }) + field("Manufacturer", r.Manufacturer) + field("Model #", r["Model #"]) + field("Finish / Color", r["Finish / Color"]) + (state.view === "spec" ? "" : field("Include in Lookbook", r["Include in Lookbook"] || "Yes", { choices: ["Yes", "No"] })) + field("Client Status", r["Client Status"] || "Proposed", { choices: state.data.client_statuses || ["Proposed"] }) + field("Product URL", r["Product URL"], { wide: true, type: "url" }) + field("Product Name", r["Product Name"], { wide: true }) + field("Image URL", r["Image URL"], { wide: true }) + field("Client Notes", r["Client Notes"], { wide: true, textarea: true });
    if (r["Lookup Status"] === "Verified") fields += field("Lookup Status", "Verified", { choices: ["Verified", "Found - verify"] });
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
  $("editor-verify").hidden = !(kind === "selection" && record && isUrl(record["Product URL"]) && record["Lookup Status"] !== "Verified");
  $("editor").showModal();
}

async function deleteEditorRecord() {
  const { kind, record } = state.editor || {};
  if (!record || state.busy) return;
  const label = { selection: `selection “${record.Item}”`, project: `project “${record["Project Name"]}”`, manufacturer: `manufacturer “${record.Manufacturer}”` }[kind];
  const owned = kind === "project" ? state.data.selections.filter((r) => r["Project ID"] === record["Project ID"]).length : 0;
  const extra = owned ? ` Its ${owned} line item${owned === 1 ? "" : "s"} will be deleted too.`
    : kind === "manufacturer" ? " Selections using this brand lose their official-domain search boost." : "";
  if (!confirm(`Delete ${label}? The row is removed from the workbook; a backup is kept.${extra}`)) return;
  $("editor-error").hidden = true;
  setBusy(true, "Deleting…");
  try {
    const values = Object.fromEntries(new FormData($("editor-form")));
    if (kind === "selection") values["Project ID"] = state.project;
    await api(`/api/${{ project: "projects", selection: "selections", manufacturer: "manufacturers" }[kind]}`,
              { values, row: record._row, delete: true, cascade: owned > 0, revision: state.editor.revision });
    if (kind === "project") state.project = "";
    $("editor").close();
    await refresh();
    toast("Deleted from the workbook. A backup was preserved.");
  } catch (error) {
    $("editor-error").textContent = error.message;
    $("editor-error").hidden = false;
  } finally { setBusy(false); }
}

async function saveEditor(event) {
  event.preventDefault();
  if (state.busy) return;
  const { kind, record } = state.editor;
  const values = Object.fromEntries(new FormData($("editor-form")));
  if (kind === "selection") values["Project ID"] = state.project;
  const custom = kind === "manufacturer" ? {} : { custom_fields: Array.from($("custom-field-rows").children, (row) => ({ name: row.querySelector(".custom-name").value, value: row.querySelector(".custom-value").value })) };
  setBusy(true, "Saving project details…");
  $("editor-save").disabled = true;
  try {
    const result = await api(`/api/${{ project: "projects", selection: "selections", manufacturer: "manufacturers" }[kind]}`, { values, row: record?._row, create: !record, revision: state.editor.revision, ...custom });
    if (kind === "project") state.project = result.project_id;
    $("editor").close();
    await refresh();
    toast(result.seeded ? `Project created with ${result.seeded} standard line items ready to specify.`
      : result.queued ? "Selection saved. Automatic product lookup is queued; results will appear when ready."
      : "Project details saved. A backup was preserved.");
  } catch (error) {
    $("editor-error").textContent = error.message;
    $("editor-error").hidden = false;
  } finally { setBusy(false); }
}

async function verify(row) {
  const r = projectRows().find((r) => r._row === row);
  if (!r || !confirm(`Have you opened the product page and confirmed all of these?\n\nManufacturer: ${r.Manufacturer}\nModel: ${r["Model #"]}\nFinish: ${r["Finish / Color"] || "Not specified"}\n\nMark this selection verified?`)) return;
  setBusy(true, "Saving verification…");
  try {
    await api("/api/selections", { row, values: { "Lookup Status": "Verified" }, confirm_verified: true });
    await refresh();
    toast("Selection verified. Automatic lookup will not overwrite it.");
  } finally { setBusy(false); }
}

async function lookup(row) {
  if (!state.data.provider.ready) { $("setup-dialog").showModal(); return; }
  const items = row ? projectRows().filter((r) => r._row === row) : projectRows().filter((r) => !r._auto_phase && !r["Product URL"] && r.Manufacturer && r["Model #"] && r["Lookup Status"] !== "Verified" && r["Include in Lookbook"].toLowerCase() !== "no");
  if (!items.length) { toast("No eligible selections. Add a manufacturer and model, or review existing links."); return; }
  if (!confirm(`Search ${items.length} selection${items.length === 1 ? "" : "s"} using ${state.data.provider.name}?\n\nManufacturer and model numbers will be sent to the search provider. API charges may apply.${row && items[0]["Product URL"] ? " A new match may replace the current unverified link." : ""}`)) return;
  state.cancel = false;
  $("cancel-lookup").textContent = "Stop after this item";
  setBusy(true, "Starting product search…");
  $("cancel-lookup").hidden = false;
  let completed = 0;
  try {
    for (const r of items) {
      if (state.cancel) break;
      $("busy-message").textContent = `Finding product ${completed + 1} of ${items.length}: ${r.Manufacturer} ${r["Model #"]}…`;
      await api(`/api/selections/${r._row}/lookup`, {});
      completed++;
      await refresh();
      if (!state.cancel && completed < items.length) await new Promise((resolve) => setTimeout(resolve, 1100));
    }
    toast(`${completed} selection${completed === 1 ? "" : "s"} checked. Review the matches before sharing.`);
  } finally {
    $("cancel-lookup").hidden = true;
    setBusy(false);
  }
}

async function addFromLink() {
  const url = prompt("Paste a product page link to prefill a new selection:");
  if (!url || !url.trim()) return;
  setBusy(true, "Reading the product page…");
  let details;
  try {
    details = (await api("/api/selections/parse-link", { url: url.trim() })).details;
  } finally {
    setBusy(false);
  }
  if (details) openEditor("selection", null, details);
}

function scheduleDetails() {
  // Client details live in the spec-sheet header, saved to the project itself.
  const values = Object.fromEntries(new FormData($("schedule-form")));
  const defaults = scheduleDefaults();
  return {
    title: values.title.trim() || defaults.title || "Selections",
    prefix: (values.prefix.trim() || defaults.code_prefix || "EX").toUpperCase(),
    hidden: [...state.hidden],
  };
}

function openScheduleDialog(format) {
  const project = selectedProject();
  if (!project) { toast("Choose a project first.", true); return; }
  const defaults = scheduleDefaults();
  state.scheduleFormat = format;
  $("schedule-dialog-title").textContent = { pdf: "Download finish schedule", form: "Download fillable schedule" }[format];
  const form = $("schedule-form");
  form.title.value = $("export-title").value.trim() || defaults.title || "";
  form.prefix.value = $("export-prefix").value.trim() || defaults.code_prefix || "";
  renderHideGrid("schedule-hide-grid", "schedule");
  $("schedule-error").hidden = true;
  $("schedule-frame").removeAttribute("src");
  $("schedule-dialog").showModal();
  refreshSchedulePreview();
}

let previewUrl = null;

async function refreshSchedulePreview() {
  const hint = $("schedule-hint");
  hint.textContent = "Building the preview…";
  hint.hidden = false;
  $("schedule-error").hidden = true;
  try {
    const response = await api("/api/presentation", {
      project: state.project, mode: $("export-mode").value, format: state.scheduleFormat,
      preview: true, ...scheduleDetails() }, true);
    const blob = await response.blob();
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(blob);
    $("schedule-frame").src = previewUrl;
    hint.textContent = "Every page of the document, exactly as it will download.";
  } catch (error) {
    $("schedule-frame").removeAttribute("src");
    hint.hidden = true;
    $("schedule-error").textContent = error.message;
    $("schedule-error").hidden = false;
  }
}

async function downloadSchedule(event) {
  event.preventDefault();
  if (state.busy) return;
  $("schedule-error").hidden = true;
  setBusy(true, "Building your finish schedule…");
  try {
    await exportPresentation(state.scheduleFormat, { ...scheduleDetails() });
    $("schedule-dialog").close();
  } catch (error) {
    $("schedule-error").textContent = error.message;
    $("schedule-error").hidden = false;
  } finally { setBusy(false); }
}

const FORMAT_LABELS = { pdf: "PDF", docx: "Word", pptx: "PowerPoint", form: "fillable", "form-docx": "fillable Word" };
const FORMAT_EXTENSIONS = { pdf: "pdf", docx: "docx", pptx: "pptx", form: "pdf", "form-docx": "docx" };

async function exportPresentation(format, extra = {}) {
  const { title, prefix, hidden: columns } = scheduleOptions();
  const label = FORMAT_LABELS[format];
  const quiet = Object.keys(extra).length > 0;   // the dialog shows its own progress
  if (!quiet) setBusy(true, `Building your ${label} finish schedule…`);
  try {
    const response = await api("/api/presentation", { project: state.project, mode: $("export-mode").value, format, title, prefix, hidden: columns, ...extra }, true);
    const blob = await response.blob(), url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const filename = (response.headers.get("Content-Disposition") || "").match(/filename="?([^";]+)/);
    link.href = url;
    link.download = filename ? filename[1] : `${state.project}_${title.replace(/[^A-Za-z0-9]+/g, "_")}.${FORMAT_EXTENSIONS[format] || "pdf"}`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    await refresh();
    toast("Finish schedule downloaded. Review the layout and branding before sharing with your client.");
  } finally { if (!quiet) setBusy(false); }
}

async function importScheduleForm(file) {
  if (!file) return;
  const project = selectedProject();
  if (!project) { toast("Choose a project first.", true); return; }
  if (file.size > 8 * 1024 * 1024) throw new Error("Choose a file smaller than 8 MB.");
  if (!confirm(`Apply “${file.name}” to “${project["Project Name"]}”?\n\nClient details are updated, edited descriptions are saved for the client schedule, and any line you ticked Remove is deleted. A backup is kept.`)) return;
  const data = new FormData();
  data.append("file", file);
  data.append("project", state.project);
  data.append("revision", state.data.revision);
  setBusy(true, "Reading the filled schedule…");
  try {
    const result = await api("/api/schedule/form", data);
    await refresh();
    const parts = [];
    if (result.details.length) parts.push(`${result.details.length} client detail${result.details.length === 1 ? "" : "s"} updated`);
    if (result.described) parts.push(`${result.described} description${result.described === 1 ? "" : "s"} saved`);
    if (result.removed) parts.push(`${result.removed} line${result.removed === 1 ? "" : "s"} removed`);
    toast(parts.length ? `${parts.join(", ")}.` : "Nothing had changed in that form.");
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

async function importWorkbook(file) {
  if (!file) return;
  if (file.size > 8 * 1024 * 1024) throw new Error("Choose a workbook smaller than 8 MB.");
  if (!confirm(`Replace the current master workbook with “${file.name}”?\n\nThis replaces ALL projects and selections, not just the selected project. A timestamped backup will be kept. Close Excel before continuing.`)) return;
  const data = new FormData();
  data.append("file", file);
  data.append("revision", state.data.revision);
  data.append("confirm", "replace-with-backup");
  setBusy(true, "Validating and importing the workbook…");
  try { await api("/api/workbook/import", data); await refresh(); toast("Workbook imported. The previous version is in the backups folder."); }
  finally { setBusy(false); }
}

let polling = false;
setInterval(async () => {
  if (polling || state.busy || specDrag || $("editor").open || !state.data?.automation?.enabled || document.hidden) return;
  if (document.activeElement?.classList?.contains("cell")) return;   // never redraw a cell being edited
  polling = true;
  try {
    const data = await api("/api/state");
    if (!state.busy && !$("editor").open && (data.revision !== state.data.revision || JSON.stringify(data.automation) !== JSON.stringify(state.data.automation))) {
      state.data = data;
      if (!selectedProject()) state.project = data.projects[0]?.["Project ID"] || "";
      $("load-error").hidden = true;
      render();
    }
  } catch (error) {
    $("load-error").textContent = error.message;
    $("load-error").hidden = false;
  } finally { polling = false; }
}, 2500);

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

$("retry-automation").addEventListener("click", () => action(async () => {
  await api("/api/automation/retry", {});
  await refresh();
  toast("Automatic lookup will retry eligible selections.");
}));
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
$("editor-form").addEventListener("submit", saveEditor);
$("editor-delete").addEventListener("click", deleteEditorRecord);
$("editor-verify").addEventListener("click", () => { const { record } = state.editor || {}; if (record) { $("editor").close(); action(() => verify(record._row)); } });
$("new-project").addEventListener("click", () => openEditor("project"));
$("edit-project").addEventListener("click", () => openEditor("project", selectedProject()));
$("add-selection").addEventListener("click", () => openEditor("selection"));
$("add-from-link").addEventListener("click", () => action(addFromLink));
$("spec-filter").addEventListener("input", renderSpec);
// Word downloads skip the details dialog: its preview is an iframe, which cannot show a .docx.
const specQuery = () => `?project=${encodeURIComponent(state.project)}&hide=${encodeURIComponent([...state.hidden].join(","))}`;
const FILE_ACTIONS = {
  pdf: () => openScheduleDialog("pdf"),
  docx: () => action(() => exportPresentation("docx")),
  form: () => openScheduleDialog("form"),
  "form-docx": () => action(() => exportPresentation("form-docx")),
  pptx: () => action(() => exportPresentation("pptx")),
  csv: () => { window.location.href = `/api/selections/csv${specQuery()}`; },
  "spec-docx": () => { window.location.href = `/api/selections/docx${specQuery()}`; },
  workbook: () => { window.location.href = "/api/workbook"; },
  "import-csv": () => $("spec-import-file").click(),
  "import-form": () => $("form-import-file").click(),
  "import-workbook": () => $("import-file").click(),
};
const NEEDS_PROJECT = new Set(["pdf", "docx", "form", "form-docx", "pptx", "csv", "spec-docx",
                               "import-csv", "import-form"]);

$("files-button").addEventListener("click", () => { if (state.data && !state.busy) $("files-dialog").showModal(); });
$("files-dialog").addEventListener("click", (event) => {
  const choice = event.target.closest(".file-choice");
  if (!choice || state.busy) return;
  const key = choice.dataset.file;
  if (NEEDS_PROJECT.has(key) && !selectedProject()) { toast("Choose a project first.", true); return; }
  $("files-dialog").close();
  FILE_ACTIONS[key]();
});
$("spec-import-file").addEventListener("change", (event) => {
  const file = event.target.files[0];
  event.target.value = "";
  action(() => importSpecCsv(file));
});
$("spec-add").addEventListener("click", () => openEditor("selection"));

$("spec-expand").addEventListener("click", () => { state.specCollapsed.clear(); renderSpec(); });
$("spec-collapse").addEventListener("click", () => {
  specGroups(projectRows()).forEach(([name]) => state.specCollapsed.add(name));
  renderSpec();
});
const pendingSpecCount = () => [...state.specPending.values()].reduce((sum, values) => sum + Object.keys(values).length, 0);

function specSaveUI() {
  const count = pendingSpecCount();
  $("spec-save").textContent = count ? `Save changes (${count})` : "Save";
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
    await api("/api/selections", { batch });
    state.specPending.clear();   // only once the workbook has them
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
  if (record && String(record[field] ?? "") === input.value.trim()) delete pending[field];   // back to the saved value
  else pending[field] = input.value.trim();
  if (Object.keys(pending).length) state.specPending.set(row, pending);
  else state.specPending.delete(row);
  input.classList.toggle("dirty", field in pending);
  specSaveUI();
}

let specSaving = 0;   // an autosave still writing re-renders itself; an early redraw would flash stale values
$("spec-groups").addEventListener("change", (event) => {
  if (!event.target.classList.contains("cell")) return;
  if (state.busy) { holdSpecEdit(event.target); return; }   // a save is running — hold this edit for Save
  specSaving++;
  action(() => saveCell(event.target)).finally(() => specSaving--);
});
$("spec-groups").addEventListener("focusout", (event) => {
  const input = event.target;
  if (!input.classList.contains("cell")) return;
  const column = SPEC_COLUMNS.find((c) => c.field === input.dataset.field);
  if (!column?.link) return;
  const wasEditing = state.specLinkEdit.delete(Number(input.dataset.row));
  // the link editor folds back into the "Product Link" anchor once it loses focus
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
$("add-manufacturer").addEventListener("click", () => openEditor("manufacturer"));
function whenText(iso) {
  const when = new Date(iso);
  const today = new Date(new Date().toDateString());
  const day = new Date(new Date(when).toDateString());
  const days = Math.round((today - day) / 86400000);
  const time = when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (days === 0) return `Today at ${time}`;
  if (days === 1) return `Yesterday at ${time}`;
  return `${when.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" })} at ${time}`;
}

async function renderBackups() {
  const list = $("backup-list");
  try {
    const { backups } = await api("/api/backups");
    list.innerHTML = backups.length
      ? backups.map((backup) => `<div class="backup-row"><time datetime="${esc(backup.when)}">${esc(whenText(backup.when))}</time><small>${Math.round(backup.size / 1024)} KB</small><button class="text-button" data-restore="${esc(backup.name)}">Restore this</button></div>`).join("")
      : '<p class="muted">No earlier versions yet. One is saved each time something changes.</p>';
  } catch (error) {
    list.innerHTML = `<p class="muted">${esc(error.message)}</p>`;
  }
}

$("setup-button").addEventListener("click", () => { $("setup-dialog").showModal(); renderBackups(); });
$("restore-file").addEventListener("click", () => { if (state.data && !state.busy) $("import-file").click(); });
$("backup-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-restore]");
  if (!button || state.busy) return;
  const when = button.closest(".backup-row").querySelector("time").textContent;
  action(async () => {
    if (!confirm(`Go back to the version saved ${when}?\n\nEverything in every project returns to how it was then. The current version is saved first, so you can come back.`)) return;
    setBusy(true, "Restoring that version…");
    try {
      await api("/api/backups/restore", { name: button.dataset.restore });
      await refresh();
      await renderBackups();
      toast(`Restored the version from ${when}. The version you replaced was saved too.`);
    } finally { setBusy(false); }
  });
});
$("refresh").addEventListener("click", () => action(async () => { await refresh(); toast("Workbook refreshed."); }));
// Only rendered on the hosted workspace, where there is a session to end.
$("sign-out")?.addEventListener("click", () => action(async () => {
  await api("/api/logout", {});
  window.location.assign("/login");
}));
$("find-links").addEventListener("click", () => action(() => lookup()));
$("cancel-lookup").addEventListener("click", () => { state.cancel = true; $("cancel-lookup").textContent = "Stopping after this item…"; });
document.querySelectorAll(".hide-grid").forEach((grid) => grid.addEventListener("change", (event) => {
  const box = event.target.closest("[data-hide]");
  if (box) toggleHidden(box.dataset.hide, !box.checked);
}));
$("export-hide-clear").addEventListener("click", () => { state.hidden.clear(); render(); });
$("schedule-hide-clear").addEventListener("click", () => { state.hidden.clear(); render(); });
$("export-pdf").addEventListener("click", () => openScheduleDialog("pdf"));
$("export-more").addEventListener("click", () => { if (state.data && !state.busy) $("files-dialog").showModal(); });
$("schedule-form").addEventListener("submit", downloadSchedule);
$("schedule-refresh").addEventListener("click", () => action(refreshSchedulePreview));
$("schedule-dialog").addEventListener("close", () => {
  if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = null; }
  $("schedule-frame").removeAttribute("src");
});

$("form-import-file").addEventListener("change", (event) => {
  const file = event.target.files[0];
  event.target.value = "";
  action(() => importScheduleForm(file));
});
$("export-mode").addEventListener("change", render);
$("export-title").addEventListener("input", render);
$("export-prefix").addEventListener("input", render);

$("import-file").addEventListener("change", (event) => { const file = event.target.files[0]; event.target.value = ""; action(() => importWorkbook(file)); });
$("previous-page").addEventListener("click", () => { state.page--; renderTable(); });
$("next-page").addEventListener("click", () => { state.page++; renderTable(); });
for (const id of ["search", "section-filter", "status-filter"]) $(id).addEventListener(id === "search" ? "input" : "change", () => { state.page = 0; renderTable(); });
document.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.classList.contains("close-dialog")) { button.closest("dialog").close(); return; }
  if (state.busy || !state.data) return;
  if (button.dataset.view) { state.view = button.dataset.view; render(); }
  if (button.dataset.project) { state.project = button.dataset.project; state.page = 0; state.specLinkEdit.clear(); $("search").value = ""; $("section-filter").value = ""; render(); }
  if (button.dataset.group) { state.specGroup = button.dataset.group; state.specCollapsed.clear(); renderSpec(); }
  if (button.dataset.specToggle !== undefined) {
    const name = button.dataset.specToggle;
    state.specCollapsed.has(name) ? state.specCollapsed.delete(name) : state.specCollapsed.add(name);
    renderSpec();
  }
  if (button.dataset.specAdd !== undefined) {
    const name = button.dataset.specAdd;   // placeholder group names are not real values
    const prefill = ["Other", "Unassigned"].includes(name) ? {}
      : state.specGroup === "Section" ? { Section: name } : { "Room / Area": name };
    openEditor("selection", null, prefill);
  }
  if (button.dataset.specRemove) action(() => removeSpecRow(Number(button.dataset.specRemove)));
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

refresh().catch((error) => {
  $("load-error").textContent = error.message;
  $("load-error").hidden = false;
  $("project-list").textContent = "Workbook unavailable";
});
