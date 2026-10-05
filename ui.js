"use strict";

const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="uh-token"]').content;
const state = { data: null, project: "", view: "selections", page: 0, reviewLimit: 24, busy: false, cancel: false, editor: null,
                specGroup: "Section", specCollapsed: new Set(), focusSelector: null };
const names = { selections: "Selection tracker", spec: "Spec sheet", review: "Review matches", presentation: "Client presentations", manufacturers: "Manufacturers" };
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
  try { response = await fetch(path, options); } catch { throw new Error("Cannot reach the local app. Make sure it is running, then retry."); }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.error || `Request failed (${response.status}).`);
  }
  return binary ? response : response.json();
}

async function refresh() {
  const data = await api("/api/state");
  state.data = data;
  if (!data.projects.some((p) => p["Project ID"] === state.project)) state.project = data.projects[0]?.["Project ID"] || "";
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

function thumb(row, review = false) {
  if (isUrl(row["Image URL"])) return `<img ${review ? "" : 'class="item-thumb"'} src="${esc(row["Image URL"])}" alt="${esc(row["Item"])}" loading="lazy" referrerpolicy="no-referrer">`;
  return review ? "<span>PRODUCT IMAGE PENDING</span>" : '<span class="item-thumb item-placeholder" aria-hidden="true">◇</span>';
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
           prefix: ($("export-prefix").value.trim() || defaults.code_prefix || "EX").toUpperCase() };
}

function rowDescription(row) {
  if (row["Description Override"]) return row["Description Override"];
  const product = row["Product Name"] || row["Model #"];
  let identity = [row.Manufacturer, product].filter(Boolean).join(" ");
  let finish = row["Finish / Color"] || "";
  if (!product && finish) { identity = [identity, finish].filter(Boolean).join(" "); finish = ""; }
  const model = row["Model #"];
  if (model && product !== model && !identity.toLowerCase().includes(model.toLowerCase())) identity = `${identity} (Model ${model})`.trim();
  const qty = Number(row.Qty) === 1 || !row.Qty ? "" : `Qty ${row.Qty}`;
  return [identity, finish, qty, row["Client Notes"]].filter(Boolean).join(" \u2013 ");
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
  let markup = "", section = "";
  rows.slice(0, PREVIEW_ROWS).forEach((row, index) => {
    if (row.Section !== section) { section = row.Section; markup += `<div class="sheet-section">${esc(section || "Other")}</div>`; }
    markup += `<div class="sheet-row"><span>${esc(prefix)}-${String(index + 1).padStart(2, "0")}</span><strong>${esc(row.Item)}</strong><em>${esc(rowDescription(row))}</em></div>`;
  });
  if (rows.length > PREVIEW_ROWS) markup += `<div class="sheet-more">+ ${rows.length - PREVIEW_ROWS} more selection${rows.length - PREVIEW_ROWS === 1 ? "" : "s"} in the download</div>`;
  $("preview-rows").innerHTML = markup || '<div class="sheet-more">Add selections to this project to see them here.</div>';
}

function render() {
  const data = state.data;
  if (!data) return;
  const project = selectedProject(), rows = projectRows();
  const verified = rows.filter((r) => r["Lookup Status"] === "Verified").length;
  const stale = rows.filter((r) => r._stale).length;
  const linked = rows.filter((r) => isUrl(r["Product URL"])).length;
  const included = rows.filter((r) => r["Include in Lookbook"].toLowerCase() !== "no");
  const auto = data.automation || { enabled: false, pending: [], message: "" };
  const projectJobs = auto.pending.filter((job) => job.project === state.project);
  const bare = state.view === "spec";   // the spec sheet stands on its own
  $("automation-banner").hidden = !auto.enabled || bare;
  $("automation-message").textContent = auto.message || (auto.pending.length ? `${projectJobs.length} automatic lookup(s) pending in this project; ${auto.pending.length} across the workbook. Saved Excel edits are detected automatically. Close Excel to allow link updates.` : `Automatic lookup is watching saved app and Excel edits.${data.provider.ready ? "" : " Configure your search API key in Setup & workbook to enable searches."}`);
  $("retry-automation").hidden = !auto.pending.some((job) => ["failed", "retry", "waiting_key"].includes(job.phase));
  $("project-list").innerHTML = data.projects.length ? data.projects.map((p) => `<button class="project-choice ${p["Project ID"] === state.project ? "selected" : ""}" data-project="${esc(p["Project ID"])}"><span><strong>${esc(p["Project Name"])}</strong><small>${esc(p["Project ID"])}</small></span></button>`).join("") : '<p class="muted">Add a project to get started.</p>';
  $("project-summary").hidden = !project || state.view === "manufacturers" || bare;
  $("stats").hidden = !project || state.view === "manufacturers" || bare;
  $("project-details-panel").hidden = !project || state.view !== "selections";
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
      `<dt><input class="cell cell-label" data-custom-index="${index}" data-custom-key="name" value="${esc(field.name)}" maxlength="80" aria-label="Field name"><span class="custom-tag">CUSTOM</span></dt>`,
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
  $("review-count").textContent = rows.length - verified;
  $("page-title").textContent = names[state.view];
  // the spec sheet is the standard specification, so it is not labelled with one project
  $("breadcrumb-name").textContent = project && !["manufacturers", "spec"].includes(state.view)
    ? `${project["Project Name"]} / ${names[state.view]}` : names[state.view];
  $("page-subtitle").textContent = { selections: "Every material. Every finish. All in one place.", spec: "The full specification, grouped the way you work.", review: "The right product, down to the last detail.", presentation: "From your workbook to the client’s finish schedule.", manufacturers: "Keep your trusted brands and official websites together." }[state.view];
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
  renderReview();
  $("export-title").placeholder = scheduleDefaults().title || "Selections";
  $("export-prefix").placeholder = scheduleDefaults().code_prefix || "EX";
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
  $("selection-rows").innerHTML = rows.slice(start, start + 50).map((r) => `<tr><td><div class="item-cell">${thumb(r)}<div><strong>${esc(r.Item || "Untitled selection")}</strong><small>${esc([r.Section, r["Room / Area"]].filter(Boolean).join(" / "))}${r["Include in Lookbook"].toLowerCase() === "no" ? " · Hidden" : ""}</small>${selectionDetails(r)}</div></div></td><td><strong>${esc(r.Manufacturer || "Manufacturer pending")}</strong><small>${esc(r["Model #"] || "Model pending")}</small></td><td><strong>${esc(r["Finish / Color"] || "—")}</strong><small>${r.Qty ? `Qty: ${esc(r.Qty)}` : "Quantity not set"}</small></td><td>${isUrl(r["Product URL"]) ? `<a class="product-link" href="${esc(r["Product URL"])}" target="_blank" rel="noopener noreferrer">View product ↗</a>` : '<span class="muted">Not linked</span>'}</td><td>${badge(r["Lookup Status"], r._auto_phase)}${r._stale ? ' <span class="status review">Re-check</span>' : ""}${r["Client Status"] && r["Client Status"] !== "Proposed" ? ` <small class="muted">Client: ${esc(r["Client Status"])}</small>` : ""}</td><td><button class="row-action" data-edit="${r._row}" aria-label="Edit ${esc(r.Item)}">Edit ↗</button></td></tr>`).join("");
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
    return;
  }
  $("spec-groups").innerHTML = groups.map(([name, items]) => {
    const total = items.reduce((sum, r) => sum + lineTotal(r), 0);
    const collapsed = state.specCollapsed.has(name);
    const body = items.map((r) => `<tr>${SPEC_COLUMNS.map((c) => `<td${c.number ? ' class="num"' : ""}><input class="cell${c.number ? " num" : ""}" data-row="${r._row}" data-field="${esc(c.field)}" value="${esc(r[c.field] ?? "")}" placeholder="${esc(c.placeholder)}"${c.number ? ' type="number" min="0" step="any"' : ` maxlength="4000"`}${r._auto_phase ? " disabled" : ""}></td>`).join("")}<td><div class="spec-actions"><button class="text-button" data-edit="${r._row}" aria-label="Open ${esc(r.Item)}">Open</button><button class="icon-button" data-spec-remove="${r._row}" aria-label="Remove ${esc(r.Item)}">×</button></div></td></tr>`).join("");
    return `<article class="spec-card${collapsed ? " collapsed" : ""}">
      <button type="button" class="spec-card-head" data-spec-toggle="${esc(name)}" aria-expanded="${!collapsed}"><span class="twist" aria-hidden="true">▾</span><h3>${esc(name)}</h3><span class="count">${items.length} item${items.length === 1 ? "" : "s"}</span><span class="group-total">${total ? money(total) : ""}</span></button>
      <div class="spec-body"><table class="spec-table"><thead><tr>${SPEC_COLUMNS.map((c) => `<th${c.number ? ' class="num"' : ""}>${c.label}</th>`).join("")}<th><span class="sr-only">Actions</span></th></tr></thead><tbody>${body}</tbody></table></div>
      <div class="spec-foot"><button class="text-button" data-spec-add="${esc(name)}">+ Add to ${esc(name)}</button><span class="muted">${items.filter((r) => r["Lookup Status"] === "Verified").length} of ${items.length} verified</span></div>
    </article>`;
  }).join("");
  const grand = rows.reduce((sum, r) => sum + lineTotal(r), 0);
  $("spec-total").textContent = grand ? money(grand) : "—";
}

function restoreFocus() {
  if (!state.focusSelector) return;
  const cell = document.querySelector(state.focusSelector);
  state.focusSelector = null;
  if (!cell) return;
  cell.focus();
  if (cell.type !== "date") cell.setSelectionRange?.(cell.value.length, cell.value.length);
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
}

async function saveCell(input) {
  const row = Number(input.dataset.row), field = input.dataset.field;
  const record = projectRows().find((r) => r._row === row);
  if (!record || String(record[field] ?? "") === input.value.trim()) return;
  if (field === "Item" && !input.value.trim()) { toast("An item needs a name.", true); renderSpec(); return; }
  input.classList.add("dirty");
  state.focusSelector = `#spec-groups .cell[data-row="${row}"][data-field="${CSS.escape(field)}"]`;
  try {
    const result = await api("/api/selections", { row, values: { [field]: input.value.trim() } });
    await refresh();
    if (result.queued) toast("Saved. Automatic product lookup is queued for this change.");
  } catch (error) {
    state.focusSelector = null;
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

function renderReview() {
  const rows = projectRows().filter((r) => r["Lookup Status"] !== "Verified" || r._stale);
  $("review-grid").innerHTML = rows.slice(0, state.reviewLimit).map((r) => `<article class="review-card"><div class="review-image">${thumb(r, true)}</div><div class="review-body">${badge(r["Lookup Status"], r._auto_phase)}${r._stale ? ' <span class="status review">Re-check</span>' : ""}${r["Client Status"] && r["Client Status"] !== "Proposed" ? ` <span class="status">${esc(r["Client Status"])}</span>` : ""}<h3>${esc(r.Item)}</h3><p>${esc(r.Manufacturer)} · <strong>${esc(r["Model #"] || "Model pending")}</strong></p><p>${esc(r["Finish / Color"] || "Finish not specified")} / ${esc(r["Room / Area"] || r.Section)}</p>${r["Lookup Notes"] ? `<div class="review-notes">${esc(r["Lookup Notes"])}</div>` : ""}${isUrl(r["Product URL"]) ? `<a class="product-link" href="${esc(r["Product URL"])}" target="_blank" rel="noopener noreferrer">Open product page ↗</a>` : '<p class="muted">No candidate link yet.</p>'}<div class="review-actions">${isUrl(r["Product URL"]) ? `<button class="button primary" data-verify="${r._row}">Mark verified</button>` : `<button class="button primary" data-lookup="${r._row}" ${r._auto_phase || !r.Manufacturer || !r["Model #"] ? "disabled" : ""}>Find product</button>`}<button class="button secondary" data-edit="${r._row}">Edit details</button>${isUrl(r["Product URL"]) ? `<button class="text-button" data-lookup="${r._row}">Search again</button>` : ""}</div></div></article>`).join("") || `<div class="empty-state"><div class="empty-icon">◇</div><h3>${projectRows().length ? "Everything checked. Beautifully done." : "Nothing to review yet."}</h3><p>${projectRows().length ? "All selections in this project are verified." : "Add selections to begin finding and reviewing product links."}</p></div>`;
  $("more-reviews").hidden = rows.length <= state.reviewLimit;
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
  const values = Object.fromEntries(new FormData($("schedule-form")));
  const defaults = scheduleDefaults();
  return {
    details: { client: values.client.trim(), project_lot: values.project_lot.trim(),
               address: values.address.trim(), date: values.date },
    title: values.title.trim() || defaults.title || "Selections",
    prefix: (values.prefix.trim() || defaults.code_prefix || "EX").toUpperCase(),
  };
}

function openScheduleDialog(format) {
  const project = selectedProject();
  if (!project) { toast("Choose a project first.", true); return; }
  const defaults = scheduleDefaults();
  state.scheduleFormat = format;
  $("schedule-dialog-title").textContent = { pdf: "Download finish schedule", form: "Download fillable schedule" }[format];
  const form = $("schedule-form");
  form.client.value = project["Client Name"] || "";
  form.project_lot.value = [project["Project Name"], project["Plan / Elevation"]].filter(Boolean).join(" / ");
  form.address.value = project.Address || "";
  form.date.value = (project["Presentation Date"] || "").slice(0, 10);
  form.title.value = $("export-title").value.trim() || defaults.title || "";
  form.prefix.value = $("export-prefix").value.trim() || defaults.code_prefix || "";
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
    await exportPresentation(state.scheduleFormat, { ...scheduleDetails(), save_details: $("schedule-save-details").checked });
    $("schedule-dialog").close();
  } catch (error) {
    $("schedule-error").textContent = error.message;
    $("schedule-error").hidden = false;
  } finally { setBusy(false); }
}

async function exportPresentation(format, extra = {}) {
  const { title, prefix } = scheduleOptions();
  const label = { pdf: "PDF", pptx: "PowerPoint", form: "fillable" }[format];
  const quiet = Object.keys(extra).length > 0;   // the dialog shows its own progress
  if (!quiet) setBusy(true, `Building your ${label} finish schedule…`);
  try {
    const response = await api("/api/presentation", { project: state.project, mode: $("export-mode").value, format, title, prefix, ...extra }, true);
    const blob = await response.blob(), url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const filename = (response.headers.get("Content-Disposition") || "").match(/filename="?([^";]+)/);
    link.href = url;
    link.download = filename ? filename[1] : `${state.project}_${title.replace(/[^A-Za-z0-9]+/g, "_")}.${format === "pptx" ? "pptx" : "pdf"}`;
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
  if (polling || state.busy || $("editor").open || !state.data?.automation?.enabled || document.hidden) return;
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
$("editor-form").addEventListener("submit", saveEditor);
$("editor-delete").addEventListener("click", deleteEditorRecord);
$("new-project").addEventListener("click", () => openEditor("project"));
$("edit-project").addEventListener("click", () => openEditor("project", selectedProject()));
$("add-selection").addEventListener("click", () => openEditor("selection"));
$("add-from-link").addEventListener("click", () => action(addFromLink));
$("spec-filter").addEventListener("input", renderSpec);
const FILE_ACTIONS = {
  pdf: () => openScheduleDialog("pdf"),
  form: () => openScheduleDialog("form"),
  pptx: () => action(() => exportPresentation("pptx")),
  csv: () => { window.location.href = `/api/selections/csv?project=${encodeURIComponent(state.project)}`; },
  workbook: () => { window.location.href = "/api/workbook"; },
  "import-csv": () => $("spec-import-file").click(),
  "import-form": () => $("form-import-file").click(),
  "import-workbook": () => $("import-file").click(),
};
const NEEDS_PROJECT = new Set(["pdf", "form", "pptx", "csv", "import-csv", "import-form"]);

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
$("spec-groups").addEventListener("change", (event) => {
  if (event.target.classList.contains("cell")) action(() => saveCell(event.target));
});
$("project-detail-values").addEventListener("change", (event) => {
  const input = event.target;
  if (!input.classList.contains("cell")) return;
  action(() => (input.dataset.projectField ? saveProjectField(input) : saveProjectCustom(input)));
});
$("add-manufacturer").addEventListener("click", () => openEditor("manufacturer"));
$("setup-button").addEventListener("click", () => $("setup-dialog").showModal());
$("refresh").addEventListener("click", () => action(async () => { await refresh(); toast("Workbook refreshed."); }));
$("find-links").addEventListener("click", () => action(() => lookup()));
$("cancel-lookup").addEventListener("click", () => { state.cancel = true; $("cancel-lookup").textContent = "Stopping after this item…"; });
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
$("more-reviews").addEventListener("click", () => { state.reviewLimit += 24; renderReview(); attachImageErrors(); });
for (const id of ["search", "section-filter", "status-filter"]) $(id).addEventListener(id === "search" ? "input" : "change", () => { state.page = 0; renderTable(); });
document.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.classList.contains("close-dialog")) { button.closest("dialog").close(); return; }
  if (state.busy || !state.data) return;
  if (button.dataset.view) { state.view = button.dataset.view; render(); }
  if (button.dataset.project) { state.project = button.dataset.project; state.page = 0; state.reviewLimit = 24; $("search").value = ""; $("section-filter").value = ""; render(); }
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
  if (button.dataset.edit) openEditor("selection", projectRows().find((r) => r._row === Number(button.dataset.edit)));
  if (button.dataset.verify) action(() => verify(Number(button.dataset.verify)));
  if (button.dataset.lookup) action(() => lookup(Number(button.dataset.lookup)));
  if (button.dataset.manufacturer !== undefined) openEditor("manufacturer", state.data.manufacturers[Number(button.dataset.manufacturer)]);
});

refresh().catch((error) => {
  $("load-error").textContent = error.message;
  $("load-error").hidden = false;
  $("project-list").textContent = "Workbook unavailable";
});
