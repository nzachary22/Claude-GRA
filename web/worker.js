// Grant Reporting Assistant, browser version: the background worker.
// It runs the app's own Python code (the same as the desktop app) with Pyodide, entirely inside this
// browser tab. Uploaded files, the database and the AI record live in this browser's storage for the
// app, on this computer. The only addresses it contacts are this site (for its own files) and, when a
// report is prepared, the AI service chosen in Settings.
"use strict";

const BASE = new URL("./", self.location.href).href;
let py = null;
let webApi = null;

const DATA_DIR = "/appdata";

async function syncToStorage() {
  // Save what changed in the app's folder to this browser's storage (IndexedDB).
  await new Promise((resolve) => py.FS.syncfs(false, () => resolve()));
}

async function boot() {
  const manifest = await (await fetch(BASE + "manifest.json", { cache: "no-cache" })).json();
  const engine = BASE + manifest.engine + "/";
  importScripts(engine + "pyodide.js");
  py = await loadPyodide({ indexURL: engine, env: { GRA_DATA_DIR: DATA_DIR, HOME: "/home/pyodide" } });
  await py.loadPackage(manifest.packages, { messageCallback: () => {}, errorCallback: (m) => console.warn(m) });
  for (const name of manifest.libraries) {
    const res = await fetch(engine + "lib/" + name);
    if (!res.ok) throw new Error(`Could not load ${name}`);
    py.unpackArchive(await res.arrayBuffer(), "wheel");
  }
  const app = await fetch(BASE + manifest.app);
  if (!app.ok) throw new Error("Could not load the app");
  py.unpackArchive(await app.arrayBuffer(), "zip", { extractDir: "/app" });

  py.FS.mkdirTree(DATA_DIR);
  py.FS.mount(py.FS.filesystems.IDBFS, {}, DATA_DIR);
  await new Promise((resolve) => py.FS.syncfs(true, () => resolve()));

  py.runPython(`
import sys, logging
sys.path.insert(0, "/app")
logging.basicConfig(level=logging.WARNING)
from gra import web_api
`);
  webApi = py.pyimport("gra.web_api");
  webApi.set_publisher((runId, reply) => {
    const data = reply.toJs({ dict_converter: Object.fromEntries, create_proxies: false });
    reply.destroy();
    self.postMessage({ type: "poll", run: runId, data });
  });
  webApi.set_activity_hook((runId, line) => self.postMessage({ type: "activity", run: runId, line }));
  self.postMessage({ type: "ready", version: manifest.version });
}

function toJs(value) {
  if (value && typeof value.toJs === "function") {
    const out = value.toJs({ dict_converter: Object.fromEntries, create_proxies: false });
    value.destroy();
    return out;
  }
  return value;
}

async function handle(msg) {
  const files = msg.files.map((f) => [f.field, f.name, new Uint8Array(f.buffer)]);
  const pyFiles = py.toPy(files);
  let reply;
  try {
    reply = toJs(webApi.handle(msg.method, msg.url, msg.body || "", pyFiles));
  } finally {
    pyFiles.destroy();
  }
  // A report or correction to work on next: the page is told before the reply, so it never waits on the worker.
  const jobRun = webApi.has_jobs() ? webApi.job_run_id() : null;
  if (jobRun) webApi.publish(true);
  const out = { type: "response", id: msg.id, status: reply.status, busy: jobRun };
  const transfer = [];
  if (reply.file) {
    const [data, media, filename] = reply.file;
    const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
    out.file = { data: bytes.buffer, media, filename };
    transfer.push(bytes.buffer);
  } else {
    out.json = reply.json;
  }
  self.postMessage(out, transfer);

  // Long work (preparing a report, a correction) runs now, one step at a time; progress is pushed while it
  // runs, and what changed is saved to the browser's storage after each step.
  if (jobRun) {
    try {
      while (webApi.run_job()) await syncToStorage();
    } catch (e) {
      console.error(e);
    }
    self.postMessage({ type: "idle", run: jobRun });
  }
  await syncToStorage();
}

// Requests are handled one at a time, in order.
let queue = Promise.resolve();
const booted = boot().catch((e) => {
  self.postMessage({ type: "failed", message: String(e && e.message || e) });
  throw e;
});

self.onmessage = (event) => {
  const msg = event.data;
  if (msg.type !== "request") return;
  queue = queue.then(async () => {
    try {
      await booted;
      await handle(msg);
    } catch (e) {
      console.error(e);
      self.postMessage({ type: "response", id: msg.id, status: 500, json: { detail: "The assistant stopped unexpectedly. Reload the page and try again." } });
    }
  });
};
