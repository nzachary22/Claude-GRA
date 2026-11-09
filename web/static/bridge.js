// Grant Reporting Assistant, browser version: connects the page to the assistant running in this tab.
// The page (app.js, the same as the desktop app) asks for /api/... as if a local server were there.
// This script answers those requests from the background worker (worker.js) instead, so nothing the
// person adds is uploaded to this website.
(() => {
  "use strict";
  const ROOT = new URL("../", document.currentScript.src).href;
  const realFetch = window.fetch.bind(window);
  const waiting = new Map();
  let worker = null;
  let ready = null;
  let seq = 0;
  let busyRun = null;          // the run whose report or correction is being worked on now
  const latest = {};           // run id -> its latest progress, pushed by the worker while it is busy
  const stopped = {};          // run id -> its last state, for runs stopped with Cancel
  const activity = {};         // run id -> the AI record's lines, as written while the worker is busy

  function startWorker() {
    worker = new Worker(ROOT + "worker.js");
    busyRun = null;
    ready = new Promise((resolve, reject) => {
      worker.onmessage = (e) => {
        const m = e.data;
        if (m.type === "ready") { booted(m.version); resolve(); }
        else if (m.type === "failed") { bootFailed(m.message); reject(new Error(m.message)); }
        else if (m.type === "poll") latest[m.run] = m.data;
        else if (m.type === "activity") (activity[m.run] = activity[m.run] || []).push(m.line);
        else if (m.type === "busy") busyRun = m.run;
        else if (m.type === "idle") busyRun = null;
        else if (m.type === "response") {
          if (m.busy) busyRun = m.busy;
          const w = waiting.get(m.id);
          if (w) { waiting.delete(m.id); w(m); }
        }
      };
      worker.onerror = (e) => { console.error(e); };
    });
  }

  function booted() {
    const b = document.getElementById("booting");
    if (b) b.remove();
    const main = document.querySelector("main");
    if (main) main.hidden = false;
  }
  function bootFailed(message) {
    const b = document.getElementById("booting");
    if (b) b.innerHTML = `<strong>The assistant could not start.</strong> <span class="muted">Reload the page to try again. This browser may be too old or may block part of the page (${String(message).replace(/[<&>]/g, "")}).</span>`;
  }

  function json(status, body) {
    return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  }

  async function encodeBody(opts) {
    const files = [];
    let body = "";
    if (opts.body instanceof FormData) {
      const fields = {};
      for (const [field, value] of opts.body.entries()) {
        if (value instanceof Blob) files.push({ field, name: value.name || "file", buffer: await value.arrayBuffer() });
        else fields[field] = value;
      }
      body = JSON.stringify(fields);
    } else if (typeof opts.body === "string") body = opts.body;
    return { files, body };
  }

  async function send(method, url, opts = {}) {
    await ready;
    const { files, body } = await encodeBody(opts);
    const id = ++seq;
    const reply = new Promise((resolve) => waiting.set(id, resolve));
    worker.postMessage({ type: "request", id, method, url, body, files }, files.map((f) => f.buffer));
    const m = await reply;
    if (m.file) {
      return new Response(new Blob([m.file.data], { type: m.file.media }), {
        status: m.status,
        headers: { "content-type": m.file.media, "content-disposition": `attachment; filename="${m.file.filename}"` },
      });
    }
    return json(m.status, m.json);
  }

  function withSince(reply, url) {
    const since = Number(new URL(url, location.href).searchParams.get("since") || 0);
    return { ...reply, events: (reply.events || []).slice(since) };
  }

  async function api(method, url, opts) {
    const path = url.split("?")[0];
    const runMatch = path.match(/^\/api\/runs\/([^/]+)(\/.*)?$/);
    const runId = runMatch && runMatch[1];
    const tail = runMatch && runMatch[2];

    if (runId && stopped[runId]) {
      if (method === "GET" && !tail) return json(200, withSince(stopped[runId], url));
      if (method === "DELETE") delete stopped[runId];
      else if (!(method === "GET" && /^\/activity-(report|log)$/.test(tail || ""))) {
        return json(409, { detail: "This report was stopped. Start it again to get tables and downloads." });
      }
    }
    if (method === "DELETE" && path === "/api/runs") for (const k of Object.keys(stopped)) delete stopped[k];

    // While a report is being prepared the worker is busy, so progress comes from what it last pushed.
    if (busyRun && runId === busyRun && method === "GET" && !tail && latest[runId]) {
      return json(200, withSince(latest[runId], url));
    }
    // Cancel stops the worker at once (a request to the AI can't be interrupted otherwise) and starts a fresh one.
    if (busyRun && runId === busyRun && method === "POST" && tail === "/cancel") {
      const last = latest[runId] || { id: runId, events: [], tables: [], notes: [], report: {} };
      const events = [...(last.events || []), { at: Date.now() / 1000, kind: "cancelled", message: "Stopped at your request" }];
      stopped[runId] = { ...last, status: "cancelled", events, event_count: events.length };
      worker.terminate();
      for (const [id, w] of waiting) w({ type: "response", id, status: 409, json: { detail: "Stopped." } });
      waiting.clear();
      startWorker();
      // Give the fresh assistant the AI record written so far, so the AI activity report stays complete.
      const lines = activity[runId] || [];
      if (lines.length) send("POST", `/api/web/restore-activity/${runId}`, { body: JSON.stringify({ lines }) }).catch(() => {});
      return json(200, { ok: true });
    }
    return send(method, url, opts);
  }

  window.fetch = (input, opts = {}) => {
    const url = typeof input === "string" ? input : input.url;
    const u = new URL(url, location.href);
    if (u.origin === location.origin && u.pathname.startsWith("/api/")) {
      return api((opts.method || "GET").toUpperCase(), u.pathname + u.search, opts);
    }
    return realFetch(input, opts);
  };

  // Download links (Word, Excel, AI activity report, the sample report) are answered here too.
  function toast(text) {
    let t = document.getElementById("bridge-toast");
    if (!t) { t = document.createElement("div"); t.id = "bridge-toast"; t.className = "toast"; t.setAttribute("role", "alert"); document.body.appendChild(t); }
    t.textContent = text; t.hidden = false;
    clearTimeout(t._timer); t._timer = setTimeout(() => { t.hidden = true; }, 6000);
  }
  document.addEventListener("click", async (e) => {
    const a = e.target.closest && e.target.closest("a[href]");
    if (!a) return;
    const u = new URL(a.getAttribute("href"), location.href);
    if (u.origin !== location.origin || !u.pathname.startsWith("/api/")) return;
    e.preventDefault();
    const res = await window.fetch(u.pathname + u.search);
    if (!res.ok) {
      let detail = "That download is not available.";
      try { detail = (await res.json()).detail || detail; } catch {}
      toast(detail);
      return;
    }
    const blob = await res.blob();
    const name = (res.headers.get("content-disposition") || "").match(/filename="([^"]+)"/)?.[1] || "download";
    const href = URL.createObjectURL(blob);
    if (a.target === "_blank") window.open(href, "_blank", "noopener");
    else { const d = document.createElement("a"); d.href = href; d.download = name; document.body.appendChild(d); d.click(); d.remove(); }
    setTimeout(() => URL.revokeObjectURL(href), 60000);
  }, true);

  startWorker();
})();
