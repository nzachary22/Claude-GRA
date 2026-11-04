// Grant Reporting Assistant front end: Upload -> Prepare -> Review.
(() => {
  const $ = (id) => document.getElementById(id);
  const state = { reportFiles: [], dataFiles: [], runId: null, seen: 0, timer: null, last: null };

  // ------------------------------------------------------------ helpers
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (cell) => cell.display ?? "";
  const kindLabel = (k) => ({ assumption: "Assumption", gap: "Data gap", warning: "Warning" }[k] || k);
  // A note about one table sits beside it; anything broader is shown once, above the tables.
  const noteScope = (r) => {
    const ids = new Set(r.tables.map((t) => t.id));
    const single = (n) => n.tables.length === 1 && ids.has(n.tables[0]);
    return { single, shared: r.notes.filter((n) => !single(n)) };
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
  async function checkStatus() {
    const s = await api("/api/status");
    $("setup").hidden = s.has_key;
    return s;
  }
  $("settings-btn").onclick = () => { $("setup").hidden = !$("setup").hidden; };
  $("key-form").onsubmit = async (e) => {
    e.preventDefault();
    msg("key-msg", "Checking the key…");
    try {
      await api("/api/key", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key: $("key-input").value }) });
      $("key-input").value = "";
      msg("key-msg", "Saved.", "ok");
      setTimeout(() => { $("setup").hidden = true; }, 900);
    } catch (err) { msg("key-msg", err.message, "err"); }
  };

  // ------------------------------------------------------------ upload
  const lastYear = new Date().getFullYear() - 1;
  $("period-start").value = `${lastYear}-01-01`;
  $("period-end").value = `${lastYear}-12-31`;

  function bindDrop(zoneId, inputId, listId, key) {
    const zone = $(zoneId), input = $(inputId);
    const set = (files) => {
      state[key] = [...state[key], ...files].filter((f, i, a) => a.findIndex((g) => g.name === f.name && g.size === f.size) === i);
      $(listId).innerHTML = state[key].map((f) => `<li>${esc(f.name)}</li>`).join("");
      $("start").disabled = !(state.reportFiles.length && state.dataFiles.length);
    };
    input.onchange = () => { set([...input.files]); input.value = ""; };
    zone.ondragover = (e) => { e.preventDefault(); zone.classList.add("over"); };
    zone.ondragleave = () => zone.classList.remove("over");
    zone.ondrop = (e) => { e.preventDefault(); zone.classList.remove("over"); set([...e.dataTransfer.files]); };
  }
  bindDrop("drop-report", "report-files", "report-list", "reportFiles");
  bindDrop("drop-data", "data-files", "data-list", "dataFiles");

  $("start").onclick = async () => {
    $("start").disabled = true;
    msg("upload-msg", "Uploading…");
    const s = await checkStatus();
    if (!s.has_key) { msg("upload-msg", "Add your API key above first.", "err"); $("start").disabled = false; return; }
    const form = new FormData();
    state.reportFiles.forEach((f) => form.append("report_files", f));
    state.dataFiles.forEach((f) => form.append("data_files", f));
    form.append("period_start", $("period-start").value);
    form.append("period_end", $("period-end").value);
    try {
      const { id } = await api("/api/runs", { method: "POST", body: form });
      msg("upload-msg", "");
      startPolling(id);
    } catch (err) { msg("upload-msg", err.message, "err"); $("start").disabled = false; }
  };

  // ------------------------------------------------------------ prepare
  function startPolling(id) {
    state.runId = id; state.seen = 0;
    history.replaceState(null, "", `#run=${id}`);
    $("events").innerHTML = ""; $("now").textContent = "Starting…";
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
      $("now").textContent = r.status === "failed" ? `Stopped: ${r.error || "the report could not be prepared."}` : "Cancelled.";
      if (r.tables.length) { const b = document.createElement("button"); b.className = "secondary"; b.textContent = "Review last draft"; b.onclick = () => renderReview(r); $("now").appendChild(document.createTextNode(" ")); $("now").appendChild(b); }
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
    const gaps = r.tables.reduce((n, t) => n + t.rows.reduce((m, row) => m + row.cells.filter((c) => c.gap).length, 0), 0);
    $("review-title").textContent = r.report.organization || r.report.title || "Draft tables";
    const reportLine = [r.report.organization ? r.report.title : "", r.report.subtitle].filter(Boolean).join(" · ");
    $("review-sub").textContent = (reportLine ? `${reportLine} · ` : "") + `Draft · Reporting period ${r.period[0]} to ${r.period[1]} · ${r.tables.length} table(s) · ${r.notes.length} note(s)` + (gaps ? ` · ${gaps} unavailable figure(s)` : "");
    $("dl-word").href = `/api/runs/${r.id}/export.docx`;
    $("dl-excel").href = `/api/runs/${r.id}/export.xlsx`;
    const scope = noteScope(r);
    $("general-notes").innerHTML = scope.shared.length ? `<div class="card"><h3>Assumptions and notes for more than one table</h3><div class="notes">${scope.shared.map((n) => noteHtml(n, affects(n, r))).join("")}</div></div>` : "";
    $("tables").innerHTML = r.tables.map((t) => {
      const notes = r.notes.filter((n) => scope.single(n) && n.tables[0] === t.id);
      return `<div class="card table-block${t.headers.length > 6 ? " wide" : ""}"><div><h3>${esc(t.title)}</h3>${t.subtitle ? `<p class="table-sub">${esc(t.subtitle)}</p>` : ""}<div class="table-wrap">${tableHtml(t)}</div>${t.note ? `<p class="caption">${esc(t.note)}</p>` : ""}</div>
        <aside class="notes">${notes.length ? notes.map((n) => noteHtml(n)).join("") : '<p class="muted">No notes specific to this table.</p>'}</aside></div>`;
    }).join("");
    document.querySelectorAll("[data-change]").forEach((b) => (b.onclick = () => openChange(b)));
    $("tech").open = false;
    $("tech-body").textContent = "Loading…";
  }
  function tableHtml(t) {
    const head = `<thead><tr><th>${esc(t.row_header)}</th>${t.headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead>`;
    const body = t.rows.map((row) => {
      if (row.kind === "heading") return `<tr class="heading"><td colspan="${t.headers.length + 1}">${esc(row.label)}</td></tr>`;
      const pad = row.indent ? ` style="padding-left:${9 + row.indent * 16}px"` : "";
      return `<tr class="${row.kind}"><td${pad}>${esc(row.label)}</td>${row.cells.map((c) => {
        const tip = c.gap ? c.gap : c.source ? `From ${c.source}` : "";
        return `<td class="num${c.gap ? " gap" : ""}"${tip ? ` title="${esc(tip)}"` : ""}>${esc(fmt(c))}</td>`;
      }).join("")}</tr>`;
    }).join("");
    return `<table class="report">${head}<tbody>${body}</tbody></table>`;
  }
  function noteHtml(n, affected = "") {
    return `<div class="note ${esc(n.kind)}"><div class="kind">${esc(kindLabel(n.kind))}</div><div>${esc(n.text)}</div>
      ${affected ? `<div class="muted">Affects: ${esc(affected)}</div>` : ""}
      ${n.how_to_change ? `<div class="muted">${esc(n.how_to_change)}</div>` : ""}
      <div class="change"><button class="link" data-change="${esc(n.id)}" data-text="${esc(n.text)}">Change</button></div></div>`;
  }
  function openChange(btn) {
    const box = btn.closest(".note");
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
    $("events").innerHTML = ""; $("bar").style.display = ""; $("now").textContent = "Applying your correction…";
    show("prepare");
    clearInterval(state.timer);
    state.timer = setInterval(poll, 1000);
  }
  $("correct-form").onsubmit = (e) => { e.preventDefault(); submitCorrection($("correct-text").value); };
  $("new-report").onclick = () => { history.replaceState(null, "", location.pathname); show("upload"); $("start").disabled = !(state.reportFiles.length && state.dataFiles.length); };

  $("copy").onclick = async () => {
    const r = state.last; if (!r) return;
    const scope = noteScope(r);
    const heading = [r.report.organization, r.report.title, r.report.subtitle].filter(Boolean);
    const noteLine = (n) => { const a = affects(n, r); return `${kindLabel(n.kind)}: ${n.text}${a ? ` [Affects: ${a}]` : ""}`; };
    const htmlHead = heading.map((x, i) => (i ? `<p>${esc(x)}</p>` : `<h2>${esc(x)}</h2>`)).join("") +
      scope.shared.map((n) => `<p><small>${esc(noteLine(n))}</small></p>`).join("");
    const html = htmlHead + r.tables.map((t) => `<h3>${esc(t.title)}</h3>${t.subtitle ? `<p><i>${esc(t.subtitle)}</i></p>` : ""}${tableHtml(t).replace('class="report"', 'border="1" cellpadding="4" style="border-collapse:collapse"')}${t.note ? `<p><i>${esc(t.note)}</i></p>` : ""}` +
      r.notes.filter((n) => scope.single(n) && n.tables[0] === t.id).map((n) => `<p><small>${esc(noteLine(n))}</small></p>`).join("")).join("<br>");
    const text = [...heading, ...scope.shared.map(noteLine), ...r.tables.map((t) => [t.title, ...(t.subtitle ? [t.subtitle] : []), [t.row_header, ...t.headers].join("\t"),
      ...t.rows.map((row) => [" ".repeat(row.indent * 2) + row.label, ...(row.kind === "heading" ? [] : row.cells.map(fmt))].join("\t")),
      ...(t.note ? [t.note] : []), ...r.notes.filter((n) => scope.single(n) && n.tables[0] === t.id).map(noteLine)].join("\n"))].join("\n\n");
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
      $("tech-body").innerHTML = `<h4>Data description shared with the AI</h4><pre>${esc(d.schema)}</pre><h4>Calculations run (${d.queries.length})</h4>` +
        d.queries.map((q) => `<p><b>${esc(q.id)}</b> ${esc(q.purpose)} <span class="muted">(${q.row_count} rows, ${q.ms} ms)</span></p><pre>${esc(q.sql)}</pre>` +
          `<table><tr>${q.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>${q.rows.slice(0, 50).map((row) => `<tr>${row.map((v) => `<td>${esc(v)}</td>`).join("")}</tr>`).join("")}</table>`).join("");
    } catch (err) { $("tech-body").textContent = err.message; }
  };

  checkStatus().catch(() => {});
  // Reopening or refreshing the page returns to the same report run.
  const m = location.hash.match(/run=([a-f0-9]+)/);
  if (m) startPolling(m[1]);
})();
