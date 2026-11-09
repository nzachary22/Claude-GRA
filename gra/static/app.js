// Grant Reporting Assistant front end: Upload -> Prepare -> Review.
(() => {
  const $ = (id) => document.getElementById(id);
  const state = { reportFiles: [], dataFiles: [], runId: null, seen: 0, timer: null, last: null };

  // ------------------------------------------------------------ helpers
  const sizeText = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (cell) => cell.display ?? "";
  const kindLabel = (k, key) => (key ? ({ assumption: "Consideration", gap: "Data check", warning: "Data check" }[k] || "Note") : { assumption: "Assumption", gap: "Data gap", warning: "Warning" }[k] || k);
  // A note about one table sits beside it; anything broader is shown once, above the tables.
  const noteScope = (r) => {
    const ids = new Set(r.tables.map((t) => t.id));
    const single = (n) => n.tables.length === 1 && ids.has(n.tables[0]);
    return { single: (n) => !n.key && single(n), key: r.notes.filter((n) => n.key), shared: r.notes.filter((n) => !n.key && !single(n)) };
  };
  const affects = (n, r) => {
    const named = r.tables.filter((t) => n.tables.includes(t.id)).map((t) => t.title);
    return named.length && named.length < r.tables.length ? named.join("; ") : "";
  };
  async function api(path, opts = {}) {
    const res = await fetch(path, opts);
    const body = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
    if (!res.ok) throw new Error(body?.detail || `Request failed (${res.status})`);
    return body;
  }
  function show(step) {
    for (const id of ["upload", "prepare", "review"]) $(id).hidden = id !== step;
    const order = ["upload", "prepare", "review"];
    document.querySelectorAll(".steps span").forEach((el) => {
      const i = order.indexOf(el.dataset.step), cur = order.indexOf(step);
      el.className = i === cur ? "active" : i < cur ? "done" : "";
    });
  }
  function msg(id, text, kind = "") { const el = $(id); el.textContent = text; el.className = `msg ${kind}`; }

  // ------------------------------------------------------------ setup
  const KEY_HELP = {
    anthropic: { placeholder: "sk-ant-...", help: "Create a key at console.anthropic.com under API keys." },
    openai: { placeholder: "sk-...", help: "Create a key at platform.openai.com under API keys." },
  };
  const chosen = () => document.querySelector('input[name="provider"]:checked').value;
  function showProvider(s) {
    const p = chosen();
    $("key-input").placeholder = KEY_HELP[p].placeholder;
    const saved = s?.keys?.[p];
    $("key-help").textContent = saved
      ? `${KEY_HELP[p].help} A key is already saved; paste a new one only to replace it.`
      : KEY_HELP[p].help;
    $("key-input").required = !saved;
    $("forget-key").hidden = !saved;
    document.querySelectorAll(".key-state").forEach((el) => {
      const k = el.dataset.for;
      el.textContent = s?.keys?.[k] ? (k === s.provider ? "· key saved, in use" : "· key saved") : "";
    });
  }
  async function checkStatus() {
    const s = await api("/api/status");
    state.status = s;
    if (s.version) document.querySelector(".ver").textContent = "V." + s.version.replace(/\.0$/, "");
    // No key is needed for the demo; one is asked for only when someone prepares a report of their own.
    $("setup-title").textContent = s.has_key ? "Settings" : "One-time setup";
    $("setup-close").hidden = false;
    const radio = document.querySelector(`input[name="provider"][value="${s.provider}"]`);
    if (radio && !state.providerTouched) radio.checked = true;
    showProvider(s);
    if (typeof showEstimate === "function") showEstimate();
    return s;
  }
  const closeSetup = () => { $("setup").hidden = true; $("whats-new").hidden = true; };
  $("settings-btn").onclick = () => { if ($("setup").hidden) $("setup").hidden = false; else closeSetup(); };
  $("setup-close").onclick = closeSetup;
  $("demo-settings").onclick = () => { $("setup").hidden = false; window.scrollTo({ top: 0, behavior: "smooth" }); };
  $("providers").onchange = async () => {
    state.providerTouched = true;
    showProvider(state.status);
    const p = chosen();
    msg("key-msg", "");
    if (!state.status?.keys?.[p] || state.status.provider === p) return;
    // A service with a saved key is switched to at once, with no key to re-enter.
    try {
      await api("/api/provider", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ provider: p }) });
      state.providerTouched = false;
      await checkStatus();
      msg("key-msg", `New reports will use ${p === "openai" ? "OpenAI" : "Anthropic Claude"}.`, "ok");
    } catch (err) { msg("key-msg", err.message, "err"); }
  };
  $("key-form").onsubmit = async (e) => {
    e.preventDefault();
    const p = chosen();
    if (!$("key-input").value.trim()) { msg("key-msg", "Paste an API key.", "err"); return; }
    msg("key-msg", "Checking the key…");
    try {
      await api("/api/key", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key: $("key-input").value, provider: p }) });
      $("key-input").value = "";
      state.providerTouched = false;
      await checkStatus();
      $("setup").hidden = false;
      msg("key-msg", `Saved. New reports will use ${p === "openai" ? "OpenAI" : "Anthropic Claude"}.`, "ok");
      setTimeout(closeSetup, 1200);
    } catch (err) { msg("key-msg", err.message, "err"); }
  };

  $("forget-key").onclick = async () => {
    const p = chosen(), label = p === "openai" ? "OpenAI" : "Anthropic Claude";
    if (!confirm(`Forget the saved ${label} key? You can paste it again at any time.`)) return;
    try {
      await api(`/api/key/${p}`, { method: "DELETE" });
      await checkStatus();
      msg("key-msg", `The ${label} key was removed.`, "ok");
    } catch (err) { msg("key-msg", err.message, "err"); }
  };

  // ------------------------------------------------------------ upload
  const lastYear = new Date().getFullYear() - 1;
  $("period-start").value = `${lastYear}-01-01`;
  $("period-end").value = `${lastYear}-12-31`;

  const setters = {};
  // A rough guide before anything is sent, from earlier runs: most reports take a few minutes and about $1.
  function showEstimate() {
    const p = state.status?.provider === "openai" ? "OpenAI" : "Anthropic";
    const big = state.reportFiles.reduce((n, f) => n + f.size, 0) > 3 * 1048576 || state.reportFiles.length > 2;
    $("estimate").textContent = state.reportFiles.length && state.dataFiles.length
      ? `Estimate: usually ${big ? "5 to 15" : "3 to 10"} minutes and ${big ? "about $1 to $3" : "about $1 or less"} in AI charges, billed by ${p} to your account. Reports with many tables, and each correction you request, take longer and cost more.`
      : "";
  }
  function bindDrop(zoneId, inputId, listId, key) {
    const zone = $(zoneId), input = $(inputId);
    const render = () => {
      const list = state[key];
      $(listId).innerHTML = list.map((f, i) => `<li><span>${esc(f.name)} <span class="size">${sizeText(f.size)}</span></span><button type="button" class="remove" data-i="${i}" aria-label="Remove ${esc(f.name)}" title="Remove">×</button></li>`).join("") +
        (list.length > 1 ? `<li class="clear"><button type="button" class="link clear-all">Clear all</button></li>` : "");
      $("start").disabled = !(state.reportFiles.length && state.dataFiles.length);
      $("preview").disabled = !state.dataFiles.length;
      $("preview-body").hidden = true;  // a preview of other files would mislead
      state.previewToken = (state.previewToken || 0) + 1;  // and one still loading is discarded
      msg("upload-msg", "");
      if (state.demoReady && !state.loadingSample) demoMode(false);
      showEstimate();
      if (typeof suggestSaved === "function") suggestSaved();
    };
    const set = (files) => {
      // A file chosen again replaces the earlier copy of the same name, so a corrected export is never ignored.
      const names = new Set(files.map((f) => f.name));
      state[key] = [...state[key].filter((f) => !names.has(f.name)), ...files];
      render();
    };
    setters[key] = set;
    // The list sits inside the clickable box, so its buttons must not open the file picker.
    $(listId).onclick = (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      e.preventDefault(); e.stopPropagation();
      if (btn.classList.contains("clear-all")) state[key] = [];
      else state[key].splice(Number(btn.dataset.i), 1);
      render();
    };
    input.onchange = () => { set([...input.files]); input.value = ""; };
    zone.ondragover = (e) => { e.preventDefault(); zone.classList.add("over"); };
    zone.ondragleave = () => zone.classList.remove("over");
    zone.ondrop = (e) => { e.preventDefault(); zone.classList.remove("over"); set([...e.dataTransfer.files]); };
  }
  bindDrop("drop-report", "report-files", "report-list", "reportFiles");
  bindDrop("drop-data", "data-files", "data-list", "dataFiles");

  // The fictional sample, put into the two upload boxes as if someone had added the files.
  async function loadSample() {
    const s = await api("/api/sample");
    const load = async (names) => Promise.all(names.map(async (n) => {
      const res = await fetch(`/api/sample/${encodeURIComponent(n)}`);
      if (!res.ok) throw new Error("The sample files could not be loaded.");
      return new File([await res.blob()], n, { type: res.headers.get("content-type") || "" });
    }));
    const [report, data] = await Promise.all([load(s.report), load(s.data)]);
    state.reportFiles = []; state.dataFiles = [];
    state.loadingSample = true;
    try { setters.reportFiles(report); setters.dataFiles(data); } finally { state.loadingSample = false; }
    $("period-start").value = s.period[0]; $("period-end").value = s.period[1];
    return s;
  }
  const toUpload = () => { history.replaceState(null, "", location.pathname); show("upload"); $("upload").scrollIntoView({ behavior: "smooth", block: "start" }); };
  function demoMode(on) {
    state.demoReady = on;
    $("demo-ready").hidden = !on;
    $("upload").classList.toggle("demo-step", on);
  }

  // The demo first shows the sample files in the upload boxes, then runs when the person is ready.
  $("demo").onclick = async () => {
    $("demo").disabled = true;
    closeSetup(); toUpload();
    try {
      await loadSample();
      demoMode(true);
      $("demo-go").focus({ preventScroll: true });
    } catch (err) { msg("upload-msg", err.message, "err"); }
    finally { $("demo").disabled = false; }
  };
  $("demo-go").onclick = async () => {
    $("demo-go").disabled = true;
    try {
      const { id } = await api("/api/demo", { method: "POST" });
      demoMode(false);
      msg("upload-msg", "");
      startPolling(id);
    } catch (err) { msg("upload-msg", err.message, "err"); }
    finally { $("demo-go").disabled = false; }
  };
  $("demo-cancel").onclick = () => {
    demoMode(false);
    state.reportFiles = []; setters.reportFiles([]);
    state.dataFiles = []; setters.dataFiles([]);
  };

  $("save-choices").onclick = async () => {
    try {
      const res = await api(`/api/runs/${state.runId}/save-choices`, { method: "POST" });
      $("save-choices").textContent = `Saved ${res.count} choice(s). They'll be offered when you next prepare this report.`;
      $("save-choices").disabled = true;
      loadSavedChoices();
    } catch (err) { msg("review-msg", err.message, "err"); }
  };
  async function loadSavedChoices() {
    let list = [];
    try { list = await api("/api/saved-choices"); } catch { list = []; }
    state.saved = list;
    const sel = $("saved-choices"), keep = sel.value;
    sel.innerHTML = `<option value="">None</option>` + list.map((c) => `<option value="${esc(c.id)}">${esc(c.name)} (${c.count} choice${c.count === 1 ? "" : "s"}, saved ${esc(c.saved_at)})</option>`).join("");
    sel.value = list.some((c) => c.id === keep) ? keep : "";
    $("saved-line").hidden = !list.length;
    suggestSaved();
  }
  // Offer the saved choices of the same report automatically, matched by its file name; the user can switch to None.
  function suggestSaved() {
    const names = new Set(state.reportFiles.map((f) => f.name));
    const hit = (state.saved || []).find((c) => (c.report_files || []).some((n) => names.has(n)));
    if (hit && !$("saved-choices").dataset.touched) $("saved-choices").value = hit.id;
    $("forget-choices").hidden = !$("saved-choices").value;
  }
  $("saved-choices").onchange = () => { $("saved-choices").dataset.touched = "1"; $("forget-choices").hidden = !$("saved-choices").value; };
  $("forget-choices").onclick = async () => {
    const id = $("saved-choices").value;
    if (!id || !confirm("Forget these saved choices?")) return;
    await api(`/api/saved-choices/${id}`, { method: "DELETE" }).catch(() => {});
    loadSavedChoices();
  };
  loadSavedChoices();

  $("start").onclick = async () => {
    $("start").disabled = true;
    msg("upload-msg", "Reading the files…");
    const s = await checkStatus();
    if (!s.has_key) {
      $("setup").hidden = false;
      msg("upload-msg", "To prepare a report from these files, add an AI key in the setup panel above. To look around first, watch the demo; it needs no key.", "err");
      $("setup").scrollIntoView({ behavior: "smooth" });
      $("start").disabled = false; return;
    }
    closeSetup();
    const form = new FormData();
    state.reportFiles.forEach((f) => form.append("report_files", f));
    state.dataFiles.forEach((f) => form.append("data_files", f));
    form.append("period_start", $("period-start").value);
    form.append("period_end", $("period-end").value);
    if ($("saved-choices").value) form.append("saved_choices", $("saved-choices").value);
    try {
      const { id } = await api("/api/runs", { method: "POST", body: form });
      msg("upload-msg", "");
      startPolling(id);
    } catch (err) { msg("upload-msg", err.message, "err"); $("start").disabled = false; }
  };

  // Shows, on this computer, what the AI would get from the data files; nothing is sent.
  function previewHtml(p) {
    const many = p.files.length > 1;
    const where = (c) => (many ? ` <span class="from">${esc(c.file)}</span>` : "");
    const chips = (items, extra = () => "") => items.length
      ? `<ul class="chips">${items.map((c) => `<li><b>${esc(c.header)}</b>${extra(c)}${where(c)}</li>`).join("")}</ul>`
      : `<p class="muted">None.</p>`;
    const records = p.files.reduce((n, f) => n + f.rows, 0);
    const kept = `<div class="pv-box pv-kept"><h4>Stays on this computer</h4><p>The AI never sees these values.</p>
      ${chips(p.kept, (c) => ` <span class="why">${esc(c.why)}</span>`)}</div>`;
    const listed = `<div class="pv-box pv-check"><h4>Check these: the AI sees the possible answers</h4><p>Only a summary of the different answers found in each column, never the full column or which client gave which answer. Make sure no list contains a name, ID or address.</p>
      ${p.listed.length ? `<ul class="lists">${p.listed.map((c) => `<li><b>${esc(c.header)}</b>${where(c)}<span class="vals"><span class="why">${c.count} possible answer${c.count === 1 ? "" : "s"}:</span> ${c.examples.map(esc).join(", ")}${c.count > c.examples.length ? `, and ${c.count - c.examples.length} more` : ""}</span></li>`).join("")}</ul>` : `<p class="muted">None.</p>`}
      ${p.code_lists.length ? `<p class="code-lists">Code lists shared in full (they define codes and hold no client records): ${p.code_lists.map((c) => `<b>${esc(c.file)}</b> (${c.rows} rows: ${c.columns.map(esc).join(", ")})`).join("; ")}</p>` : ""}</div>`;
    const totals = `<div class="pv-box pv-totals"><h4>Totals only</h4><p>The AI can count, add up or group these, but never sees a single record's value.</p>${chips(p.totals)}</div>`;
    return `<p class="pv-head"><b>Nothing has been sent.</b> This is what the AI would get from ${p.files.length} file(s), ${records.toLocaleString()} records.</p>
      <div class="pv-grid">${kept}${listed}${totals}</div>
      <details><summary>Exact text the AI would receive about the data</summary><pre>${esc(p.schema)}</pre></details>`;
  }
  $("preview").onclick = async () => {
    $("preview").disabled = true;
    const token = state.previewToken = (state.previewToken || 0) + 1;
    msg("upload-msg", "Reading your data files on this computer…");
    const form = new FormData();
    state.dataFiles.forEach((f) => form.append("data_files", f));
    try {
      const p = await api("/api/preview", { method: "POST", body: form });
      if (token !== state.previewToken) return;  // the files changed while it loaded
      msg("upload-msg", "");
      $("preview-body").innerHTML = previewHtml(p);
      $("preview-body").hidden = false;
    } catch (err) { if (token === state.previewToken) msg("upload-msg", err.message, "err"); }
    finally { $("preview").disabled = !state.dataFiles.length; }
  };

  // ------------------------------------------------------------ prepare
  function startPolling(id) {
    state.runId = id; state.seen = 0;
    history.replaceState(null, "", `#run=${id}`);
    $("events").innerHTML = ""; $("now").textContent = "Starting…"; $("cancel").hidden = false;
    $("bar").style.display = "";
    show("prepare");
    clearInterval(state.timer);
    state.timer = setInterval(poll, 1000);
    poll();
  }
  async function poll() {
    if (state.polling) return;  // never overlap requests, or events would repeat
    state.polling = true;
    let r;
    try { r = await api(`/api/runs/${state.runId}?since=${state.seen}`); } catch { return; } finally { state.polling = false; }
    state.seen = r.event_count;
    for (const e of r.events) {
      const li = document.createElement("li");
      li.className = `ev-${e.kind}`;
      li.textContent = e.message;
      $("events").appendChild(li);
      if (e.kind !== "refused") $("now").textContent = e.message;
    }
    $("events").scrollTop = $("events").scrollHeight;
    if (r.status === "done") { clearInterval(state.timer); renderReview(r); }
    else if (r.status === "failed" || r.status === "cancelled") {
      clearInterval(state.timer);
      $("bar").style.display = "none";
      $("cancel").hidden = true;  // nothing left to stop; pressing it would hide the way back
      $("now").textContent = r.status === "failed" ? `Stopped: ${r.error || "the report could not be prepared."}` : "Cancelled.";
      if (r.tables.length) { const b = document.createElement("button"); b.className = "secondary"; b.textContent = "Review last draft"; b.onclick = () => renderReview(r); $("now").appendChild(document.createTextNode(" ")); $("now").appendChild(b); }
      const log = document.createElement("a"); log.href = `/api/runs/${state.runId}/activity-report`; log.textContent = "AI activity report";
      $("now").appendChild(document.createTextNode(" ")); $("now").appendChild(log);
      const back = document.createElement("button"); back.className = "link"; back.textContent = "Back to upload";
      back.onclick = () => { show("upload"); $("start").disabled = false; };
      $("now").appendChild(back);
    }
  }
  $("cancel").onclick = async () => {
    if (!state.runId) return;
    $("now").textContent = "Stopping…";
    await api(`/api/runs/${state.runId}/cancel`, { method: "POST" });
  };

  // ------------------------------------------------------------ review
  function renderReview(r) {
    state.last = r;
    show("review");
    $("demo-banner").hidden = !r.demo;
    $("review").classList.toggle("demo", !!r.demo);
    $("save-line").hidden = !!r.demo || !(r.corrections || r.notes.some((n) => n.key && n.kind === "assumption"));
    $("save-choices").disabled = false; $("save-choices").textContent = "Save this run's choices for next time";
    $("correct-form").closest(".card").hidden = !!r.demo;
    const gaps = r.tables.reduce((n, t) => n + t.rows.reduce((m, row) => m + row.cells.filter((c) => c.gap).length, 0), 0);
    $("review-title").textContent = r.report.organization || r.report.title || "Draft tables";
    const reportLine = [r.report.organization ? r.report.title : "", r.report.subtitle].filter(Boolean).join(" · ");
    $("review-sub").textContent = (reportLine ? `${reportLine} · ` : "") + `Draft · Reporting period ${r.period[0]} to ${r.period[1]} · ${r.tables.length} table(s) · ${r.notes.length} note(s)` + (gaps ? ` · ${gaps} unavailable figure(s)` : "");
    $("dl-word").href = `/api/runs/${r.id}/export.docx`;
    $("dl-excel").href = `/api/runs/${r.id}/export.xlsx`;
    $("dl-log").href = `/api/runs/${r.id}/activity-report`;
    $("dl-quality").href = `/api/runs/${r.id}/data-quality`;
    $("quality-item").hidden = !r.data_gaps;
    const scope = noteScope(r);
    $("considerations").innerHTML = considerationsHtml(scope.key, r);
    $("shared-notes").innerHTML = scope.shared.length ? `<div class="card"><h3>Assumptions and notes for more than one table</h3><div class="notes">${scope.shared.map((n) => noteHtml(n, affects(n, r))).join("")}</div></div>` : "";
    $("tables").innerHTML = r.tables.map((t) => {
      const notes = r.notes.filter((n) => scope.single(n) && n.tables[0] === t.id);
      return `<div class="card table-block${t.headers.length > 6 ? " wide" : ""}"><div><h3>${esc(t.title)}</h3>${t.subtitle ? `<p class="table-sub">${esc(t.subtitle)}</p>` : ""}<div class="table-wrap">${tableHtml(t)}</div>${t.note ? `<p class="caption">${esc(t.note)}</p>` : ""}</div>
        <aside class="notes">${notes.length ? notes.map((n) => noteHtml(n)).join("") : '<p class="muted">No notes specific to this table.</p>'}</aside></div>`;
    }).join("");
    const defs = r.report.definitions || [];
    $("definitions").innerHTML = defs.length ? `<div class="card defs"><h3>Reporting definitions</h3>${defs.map((d) => `${d.heading ? `<h4>${esc(d.heading)}</h4>` : ""}${d.text.split("\n").filter((x) => x.trim()).map((x) => `<p>${esc(x)}</p>`).join("")}`).join("")}</div>` : "";
    document.querySelectorAll("#review [data-change]").forEach((b) => (b.onclick = () => openChange(b)));
    bindConsiderations(r);
    $("tech").open = false;
    $("tech-body").textContent = "Loading…";
  }
  function tableHtml(t) {
    const head = `<thead><tr><th>${esc(t.row_header)}</th>${t.headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead>`;
    const body = t.rows.map((row) => {
      if (row.kind === "heading") return `<tr class="heading"><td colspan="${t.headers.length + 1}">${esc(row.label)}</td></tr>`;
      const pad = row.indent ? ` style="padding-left:${9 + row.indent * 16}px"` : "";
      return `<tr class="${row.kind}"><td${pad}>${esc(row.label)}</td>${row.cells.map((c, ci) => {
        const how = c.gap || c.how ? ` data-how="${esc(t.id)}|${t.rows.indexOf(row)}|${ci}" tabindex="0"` : "";
        return `<td class="num${c.gap ? " gap" : ""}${c.how ? " explainable" : ""}"${how}>${esc(fmt(c))}</td>`;
      }).join("")}</tr>`;
    }).join("");
    return `<table class="report">${head}<tbody>${body}</tbody></table>`;
  }
  // "How was this counted?": a plain description of where a figure came from. No records are shown.
  const human = (k) => String(k).replace(/_/g, " ");
  function explainText(r, t, row, c) {
    if (c.gap) return `<b>Not available.</b> ${esc(c.gap)}`;
    const h = c.how || {};
    if (h.sum_of) return `<b>${esc(fmt(c))}</b> is the total of: ${h.sum_of.map((x) => esc(x || "(unnamed)")).join(", ")}.`;
    if (!h.query) return "";
    const purpose = (r.purposes || {})[h.query] || "A calculation on your data";
    const where = Object.entries(h.match || {}).map(([k, v]) => `${esc(human(k))} is “${esc(v)}”`).join(" and ");
    return `<b>${esc(fmt(c))}</b> · ${esc(row.label)}${t.headers.length > 1 ? ` · ${esc(t.headers[row.cells.indexOf(c)] || "")}` : ""}<br>
      Counted on this computer by: ${esc(purpose)}.${where ? ` This figure is the result where ${where}.` : ""}
      ${h.none_found ? " No records matched, so it is 0." : ""}<br><span class="muted">Calculation ${esc(h.query)}; the exact query is under Technical details.</span>`;
  }
  function showExplain(td) {
    const [tid, ri, ci] = td.dataset.how.split("|");
    const r = state.last, t = r.tables.find((x) => x.id === tid), row = t.rows[Number(ri)], c = row.cells[Number(ci)];
    const box = $("explain");
    box.innerHTML = `<button type="button" class="close" aria-label="Close">×</button><div>${explainText(r, t, row, c)}</div>`;
    box.hidden = false;
    const rect = td.getBoundingClientRect();
    const left = Math.min(window.scrollX + rect.left, window.scrollX + document.documentElement.clientWidth - box.offsetWidth - 12);
    box.style.left = `${Math.max(8, left)}px`;
    box.style.top = `${window.scrollY + rect.bottom + 6}px`;
    box.querySelector(".close").onclick = () => { box.hidden = true; };
  }
  document.addEventListener("click", (e) => {
    const td = e.target.closest("td[data-how]");
    if (td) { showExplain(td); return; }
    if (!e.target.closest("#explain")) $("explain").hidden = true;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $("explain").hidden = true;
    if (e.key === "Enter" && e.target.matches?.("td[data-how]")) showExplain(e.target);
  });

  // The few choices and data checks worth a look. The person picks an answer for any of them,
  // and one Recalculate at the end sends every answer together.
  function considerationsHtml(key, r) {
    if (!key.length) return "";
    const counted = key.filter((n) => n.kind === "assumption"), data = key.filter((n) => n.kind !== "assumption");
    const item = (n) => {
      const tag = n.question ? ["q", "Question"] : n.kind === "assumption" ? ["a", "Choice"] : ["w", "Data check"];
      const a = affects(n, r);
      const opts = n.options || [];
      const keeps = opts.length && /^keep\b/i.test(opts[0].label);
      // A card whose options don't start with "Keep:" still gets a clear way to leave it as it is.
      const leave = keeps ? "" : `<button type="button" class="secondary chosen" data-pick="keep" aria-pressed="true">Leave as it is <span class="now">✓ Used in the tables now</span></button>`;
      const buttons = leave + opts.map((o, i) => i === 0 && keeps
        ? `<button type="button" class="secondary chosen" data-pick="0" aria-pressed="true">${esc(o.label)} <span class="now">✓ Used in the tables now</span></button>`
        : `<button type="button" class="secondary" data-pick="${i}" aria-pressed="false">${esc(o.label)}</button>`).join("");
      return `<div class="item" data-note="${esc(n.id)}"><span class="tag ${tag[0]}">${tag[1]}</span><div>
        <p>${esc(n.text)}</p>${n.question ? `<p class="small">${esc(n.question)}</p>` : ""}${a ? `<p class="small muted">Affects: ${esc(a)}</p>` : ""}
        <div class="opts">${buttons}<button type="button" class="link" data-pick="other" aria-pressed="false">${opts.length ? "Something else…" : "Change"}</button></div>
        <textarea class="other" rows="2" maxlength="1000" placeholder="What should be done instead?" hidden></textarea></div></div>`;
    };
    const section = (title, list) => (list.length ? `<h4>${title}</h4>${list.map(item).join("")}` : "");
    return `<div class="card considerations"><h3>Some considerations to check</h3>
      <p class="muted lead">Optional refinements. The tables are ready as they are: the option marked "Used in the tables now" is what each table already does, so a card you leave alone stays as it is. To change any of them, pick a different option (or "Something else…"), then press Recalculate once at the end; all your changes are applied together.</p>
      ${section("Who was counted", counted)}${section("Things noticed in the data", data)}
      <div class="recalc"><button type="button" class="primary" id="recalc-all" disabled>Recalculate</button><span class="muted" id="recalc-count">The tables already use the selected options. Choose a different option on any card to recalculate.</span></div></div>`;
  }
  function bindConsiderations(r) {
    const box = document.querySelector("#considerations .considerations");
    if (!box) return;
    const picks = {};  // note id -> option index or "other"; unpicked notes keep the current approach
    const changes = () => [...box.querySelectorAll(".item")].map((el) => {
      const n = r.notes.find((x) => x.id === el.dataset.note), p = picks[n.id];
      const opts = n.options || [], keeps = opts.length && /^keep\b/i.test(opts[0].label);
      if (p === undefined || p === "keep" || (p === 0 && keeps)) return null;
      if (p === "other") {
        const t = el.querySelector("textarea").value.trim();
        return t ? { n, text: t } : null;
      }
      return { n, label: opts[p].label, instruction: opts[p].instruction || "" };
    }).filter(Boolean);
    // "Something else…" with an empty box is unfinished, not a quiet "keep".
    const unfinished = () => [...box.querySelectorAll(".item")].filter((el) => picks[el.dataset.note] === "other" && !el.querySelector("textarea").value.trim()).length;
    const refresh = () => {
      const c = changes(), open = unfinished();
      $("recalc-all").disabled = !c.length || open > 0;
      $("recalc-count").textContent = open ? `Describe what you'd like in the "Something else" box${open > 1 ? "es" : ""}, or pick another option.`
        : c.length ? `${c.length} change${c.length > 1 ? "s" : ""} will be applied together. Cards you left alone stay as they are.` : "The tables already use the selected options. Choose a different option on any card to recalculate.";
    };
    box.querySelectorAll(".item").forEach((el) => {
      const id = el.dataset.note, ta = el.querySelector("textarea");
      el.querySelectorAll("[data-pick]").forEach((b) => (b.onclick = () => {
        const v = ["other", "keep"].includes(b.dataset.pick) ? b.dataset.pick : Number(b.dataset.pick);
        picks[id] = v;
        el.querySelectorAll("[data-pick]").forEach((x) => { const on = x === b; x.classList.toggle("chosen", on && x.dataset.pick !== "other"); x.setAttribute("aria-pressed", on); });
        ta.hidden = v !== "other";
        if (v === "other") ta.focus();
        refresh();
      }));
      ta.oninput = refresh;
    });
    $("recalc-all").onclick = () => {
      const c = changes();
      if (!c.length || unfinished()) return;
      const short = (t) => (t.length > 220 ? t.slice(0, 217) + "..." : t);
      const lines = c.map((x, i) => `${i + 1}. Note ${x.n.id} ("${short(x.n.text)}"): ` +
        (x.text ? `the user wants: ${x.text}` : `the user chose "${x.label}". ${x.instruction}`.trim()));
      const kept = r.notes.filter((n) => n.key && !c.some((x) => x.n.id === n.id)).map((n) => n.id);
      submitCorrection(`The user reviewed "Some considerations to check" and made these choices together. Apply all of them in this one recalculation:\n${lines.join("\n")}` +
        (kept.length ? `\nKeep the current approach for the other considerations (${kept.join(", ")}).` : ""));
    };
  }
  function noteHtml(n, affected = "") {
    return `<div class="note ${esc(n.kind)}"><div class="kind">${esc(kindLabel(n.kind, n.key))}</div><div>${esc(n.text)}</div>
      ${affected ? `<div class="muted">Affects: ${esc(affected)}</div>` : ""}
      ${n.how_to_change ? `<div class="muted">${esc(n.how_to_change)}</div>` : ""}
      <div class="change"><button class="link" data-change="${esc(n.id)}" data-text="${esc(n.text)}">Change</button></div></div>`;
  }
  function openChange(btn) {
    const box = btn.closest(".note, .item > div");
    if (box.querySelector("form")) return;
    const form = document.createElement("form");
    form.innerHTML = `<textarea rows="3" required placeholder="What should be done instead?"></textarea><button class="primary" type="submit">Recalculate</button>`;
    form.onsubmit = (e) => { e.preventDefault(); submitCorrection(`Regarding note ${btn.dataset.change} ("${btn.dataset.text}"): ${form.querySelector("textarea").value}`); };
    box.appendChild(form);
    form.querySelector("textarea").focus();
  }
  async function submitCorrection(text) {
    try {
      await api(`/api/runs/${state.runId}/correct`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
      $("correct-text").value = "";
      startPollingCorrection();
    } catch (err) { msg("review-msg", err.message, "err"); }
  }
  function startPollingCorrection() {
    $("events").innerHTML = ""; $("bar").style.display = ""; $("now").textContent = "Applying your correction…"; $("cancel").hidden = false;
    show("prepare");
    clearInterval(state.timer);
    state.timer = setInterval(poll, 1000);
  }
  $("correct-form").onsubmit = (e) => { e.preventDefault(); submitCorrection($("correct-text").value); };
  $("delete-run").onclick = async () => {
    if (!state.runId || !confirm("Delete this run's uploads, database and AI record from this computer? Download the tables and AI activity report first if you need them.")) return;
    try {
      await api(`/api/runs/${state.runId}`, { method: "DELETE" });
      state.runId = null; state.last = null;
      history.replaceState(null, "", location.pathname);
      show("upload");
      msg("upload-msg", "That run's files were deleted from this computer.", "ok");
    } catch (err) { msg("review-msg", err.message, "err"); }
  };
  $("delete-all").onclick = async () => {
    if (!confirm("Delete the files of every saved report run from this computer?")) return;
    try {
      const r = await api("/api/runs", { method: "DELETE" });
      msg("delete-all-msg", `Deleted ${r.deleted} saved run(s).`, "ok");
    } catch (err) { msg("delete-all-msg", err.message, "err"); }
  };
  $("new-report").onclick = () => { history.replaceState(null, "", location.pathname); show("upload"); $("start").disabled = !(state.reportFiles.length && state.dataFiles.length); };

  $("copy").onclick = async () => {
    const r = state.last; if (!r) return;
    const scope = noteScope(r);
    const defs = r.report.definitions || [];
    const heading = [r.report.organization, r.report.title, r.report.subtitle].filter(Boolean);
    const noteLine = (n) => { const a = affects(n, r); return `${kindLabel(n.kind, n.key)}: ${n.text}${a ? ` [Affects: ${a}]` : ""}`; };
    const htmlHead = heading.map((x, i) => (i ? `<p>${esc(x)}</p>` : `<h2>${esc(x)}</h2>`)).join("") +
      scope.key.map((n) => `<p><b>${esc(noteLine(n))}</b></p>`).join("") +
      scope.shared.map((n) => `<p><small>${esc(noteLine(n))}</small></p>`).join("");
    const html = htmlHead + r.tables.map((t) => `<h3>${esc(t.title)}</h3>${t.subtitle ? `<p><i>${esc(t.subtitle)}</i></p>` : ""}${tableHtml(t).replace('class="report"', 'border="1" cellpadding="4" style="border-collapse:collapse"')}${t.note ? `<p><i>${esc(t.note)}</i></p>` : ""}` +
      r.notes.filter((n) => scope.single(n) && n.tables[0] === t.id).map((n) => `<p><small>${esc(noteLine(n))}</small></p>`).join("")).join("<br>") +
      (defs.length ? `<h3>Reporting definitions</h3>` + defs.map((d) => `${d.heading ? `<h4>${esc(d.heading)}</h4>` : ""}${d.text.split("\n").filter((x) => x.trim()).map((x) => `<p>${esc(x)}</p>`).join("")}`).join("") : "");
    const text = [...heading, ...scope.shared.map(noteLine), ...r.tables.map((t) => [t.title, ...(t.subtitle ? [t.subtitle] : []), [t.row_header, ...t.headers].join("\t"),
      ...t.rows.map((row) => [" ".repeat(row.indent * 2) + row.label, ...(row.kind === "heading" ? [] : row.cells.map(fmt))].join("\t")),
      ...(t.note ? [t.note] : []), ...r.notes.filter((n) => scope.single(n) && n.tables[0] === t.id).map(noteLine)].join("\n")),
      ...(defs.length ? [["Reporting definitions", ...defs.map((d) => [d.heading, d.text].filter(Boolean).join("\n"))].join("\n\n")] : [])].join("\n\n");
    try {
      if (window.ClipboardItem) await navigator.clipboard.write([new ClipboardItem({ "text/html": new Blob([html], { type: "text/html" }), "text/plain": new Blob([text], { type: "text/plain" }) })]);
      else await navigator.clipboard.writeText(text);
      msg("review-msg", "All tables copied. Paste into Word or Excel.", "ok");
    } catch { msg("review-msg", "Copy was blocked by the browser; use the downloads instead.", "err"); }
  };

  $("tech").ontoggle = async () => {
    if (!$("tech").open || !state.runId) return;
    try {
      const d = await api(`/api/runs/${state.runId}/details`);
      $("tech-body").innerHTML = `<p><a href="/api/runs/${state.runId}/activity-log">Download the full word-for-word AI record</a></p><h4>Data description shared with the AI</h4><pre>${esc(d.schema)}</pre><h4>Calculations run (${d.queries.length})</h4>` +
        d.queries.map((q) => `<p><b>${esc(q.id)}</b> ${esc(q.purpose)} <span class="muted">(${q.row_count} rows, ${q.ms} ms)</span></p><pre>${esc(q.sql)}</pre>` +
          `<table><tr>${q.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>${q.rows.slice(0, 50).map((row) => `<tr>${row.map((v) => `<td>${esc(v)}</td>`).join("")}</tr>`).join("")}</table>`).join("");
    } catch (err) { $("tech-body").textContent = err.message; }
  };

  // Reopening or refreshing the page returns to the same report run.
  const m = location.hash.match(/run=([a-f0-9]+)/);
  checkStatus().then((s) => {
    if (m) return;  // never over a report in progress
    // Settings opens by itself while no key is saved (it holds the demo and the key field),
    // and once after an update, so people who already have a key see what's new.
    if (!s.has_key) { $("setup").hidden = false; return; }
    if (!s.show_settings) return;
    $("setup").hidden = false;
    $("whats-new").hidden = false;
    api("/api/settings-seen", { method: "POST" }).catch(() => {});
  }).catch(() => {});
  if (m) startPolling(m[1]);
})();
