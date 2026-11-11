// [un]scripted dashboard - vanilla JS, no build step.
// The access code lives only in sessionStorage (cleared when the tab closes).
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
let TOKEN = null, ROLE = null, STATUS = null, CONTEXT = null, textHistory = [];

function store(k, v) { try { v === null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch (_) {} }
function load(k) { try { return sessionStorage.getItem(k); } catch (_) { return null; } }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

async function api(path, opts = {}) {
  const headers = { Authorization: `Bearer ${TOKEN}`, ...(opts.headers || {}) };
  let body = opts.body;
  if (body && !(body instanceof FormData) && typeof body !== "string") { body = JSON.stringify(body); headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, { ...opts, headers, body });
  if (r.status === 401) { logout(); throw new Error("Signed out"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.blob();
  if (!r.ok) { const d = data.detail || data.error || {}; throw new Error(d.message || `HTTP ${r.status}`); }
  return data;
}

function flash(el, msg, ok = true) { el.textContent = msg; el.className = ok ? "ok" : "err"; setTimeout(() => { if (el.textContent === msg) { el.textContent = ""; el.className = "muted"; } }, 5000); }

// ------------------------------------------------------------------ auth --
async function login(token) {
  TOKEN = token;
  const w = await api("/api/v1/admin/whoami");
  ROLE = w.role; store("tok", token);
  $("#login").hidden = true; $("#app").hidden = false;
  $("#whoami").textContent = `Signed in as ${ROLE}`;
  $$("[data-role=researcher]").forEach(b => b.hidden = ROLE !== "researcher");
  await refreshStatus(); await refreshContext();
  setInterval(refreshStatus, 2000);
}
function logout() { store("tok", null); location.reload(); }
$("#loginForm").addEventListener("submit", async e => {
  e.preventDefault();
  try { await login($("#tokenInput").value.trim()); } catch (err) { $("#loginErr").textContent = err.message; TOKEN = null; }
});
$("#logout").addEventListener("click", logout);

$$(".tabs [data-tab]").forEach(b => b.addEventListener("click", () => {
  $$(".tabs [data-tab]").forEach(x => x.classList.toggle("active", x === b));
  $$("[data-panel]").forEach(p => p.hidden = p.dataset.panel !== b.dataset.tab);
  if (b.dataset.tab === "data") { refreshMetrics(); refreshEvents(); }
}));

// ---------------------------------------------------------------- status --
async function refreshStatus() {
  try { STATUS = await api("/api/v1/admin/status"); } catch (_) { return; }
  const s = STATUS, b = [];
  if (s.ai.mode === "MOCK_PROVIDER") b.push("<b>MOCK_PROVIDER test mode.</b> Replies are canned test output, not real AI. Do not use with participants.");
  else if (s.ai.mode === "AI_NOT_CONFIGURED") b.push("<b>AI not configured.</b> OPENAI_API_KEY is not set in backend/.env; the device will say it cannot connect.");
  if (s.research && s.research.loopback_test) b.push("<b>Loopback test is ON.</b> The device plays back what it heard; no AI is used.");
  $("#banners").innerHTML = b.map(x => `<div class="banner">${x}</div>`).join("");
  renderDevices(); renderConfig();
}

function renderDevices() {
  const s = STATUS;
  if (!s.devices.length) { $("#devices").innerHTML = '<p class="muted">No device provisioned yet. Run <code>scripts/configure_device.sh</code>.</p>'; return; }
  $("#devices").innerHTML = s.devices.map(d => {
    const dg = d.diagnostics || {};
    const kind = d.client_kind === "device" ? "REAL_HARDWARE" : (d.client_kind ? "SOFTWARE_SIMULATOR" : "-");
    return `<div class="dev">
      <div><b>Device</b>${esc(d.device_id)} ${d.label ? "· " + esc(d.label) : ""}</div>
      <div><b>Connection</b><span class="pill ${d.connected ? "on" : "off"}">${d.connected ? "Connected" : "Not connected"}</span>
        ${d.seconds_since_seen != null ? `<span class="muted"> ${d.seconds_since_seen}s ago</span>` : ""}</div>
      <div><b>Kind</b>${kind}</div>
      <div><b>State</b>${esc(d.state || "-")}</div>
      <div><b>Wi-Fi signal</b>${d.rssi != null ? d.rssi + " dBm" : "-"}</div>
      <div><b>Firmware</b>${esc(d.firmware || "-")}</div>
      <div><b>Settings applied</b>v${d.applied_version ?? "-"} of v${s.device_config.version}</div>
      <div><b>Mic / speaker self-test</b>${dg.mic_ok == null ? "-" : (dg.mic_ok ? "mic ok" : "mic FAIL")} · ${dg.speaker_ok == null ? "-" : (dg.speaker_ok ? "spk ok" : "spk FAIL")}</div>
      <div><b>AI</b>${esc(s.ai.mode)} ${s.ai.ready ? "" : "(not ready)"}</div>
      <div><b>Last conversation</b>${esc(d.last_interaction_at || "-")}</div>
    </div>`;
  }).join("<hr>");
}

let configDirty = false;
function renderConfig() {
  const c = STATUS.device_config;
  if (!configDirty) {
    $("#volume").value = c.volume; $("#volOut").textContent = c.volume + "%";
    $("#max_brightness_pct").value = c.max_brightness_pct; $("#brOut").textContent = c.max_brightness_pct + "%";
    $("#reduced_motion").checked = c.reduced_motion; $("#lights_off").checked = c.lights_off;
    for (const k of ["led_offset", "afterglow_ms", "silence_endpoint_ms", "vad_abs_dbfs", "vad_snr_db"]) if (document.activeElement !== $("#" + k)) $("#" + k).value = c[k];
    $("#led_reverse").checked = c.led_reverse; $("#led_grb").checked = c.led_grb;
  }
  $$("#condSeg button").forEach(b => b.classList.toggle("active", b.dataset.c === c.condition));
  if (ROLE === "researcher" && STATUS.research && !researchLoaded) loadResearch();
}

async function putConfig(changes, msgEl) {
  try {
    const r = await api("/api/v1/admin/device-config", { method: "PUT", body: { changes } });
    flash(msgEl, Object.keys(r.applied).length ? `Saved (settings v${r.device_config.version})` : "No change");
    configDirty = false; refreshStatus();
  } catch (e) { flash(msgEl, e.message, false); }
}
for (const id of ["volume", "max_brightness_pct"]) {
  $("#" + id).addEventListener("input", e => { configDirty = true; (id === "volume" ? $("#volOut") : $("#brOut")).textContent = e.target.value + "%"; });
  $("#" + id).addEventListener("change", e => putConfig({ [id]: +e.target.value }, $("#comfortMsg")));
}
for (const id of ["reduced_motion", "lights_off"]) $("#" + id).addEventListener("change", e => putConfig({ [id]: e.target.checked }, $("#comfortMsg")));

$("#resetSession").addEventListener("click", async () => {
  try { await api("/api/v1/admin/session/reset", { method: "POST" }); flash($("#resetMsg"), "New conversation started"); }
  catch (e) { flash($("#resetMsg"), e.message, false); }
});

// ----------------------------------------------------------- condition --
$$("#condSeg button").forEach(b => b.addEventListener("click", async () => {
  const t0 = performance.now(), before = STATUS.devices.map(d => d.applied_version);
  try {
    const r = await api("/api/v1/admin/condition", { method: "POST", body: { condition: b.dataset.c } });
    const v = r.device_config.version;
    $("#condMsg").textContent = `Condition ${b.dataset.c} saved (v${v}). Waiting for the device to confirm...`;
    refreshStatus();
    const poll = setInterval(async () => {
      await refreshStatus();
      const d = STATUS.devices.find(d => (d.applied_version || 0) >= v);
      const el = Math.round(performance.now() - t0);
      if (d) { clearInterval(poll); $("#condMsg").textContent = `Device confirmed ${b.dataset.c} after ~${el} ms (dashboard view; exact value is in the event log).`; }
      else if (el > 10000) { clearInterval(poll); $("#condMsg").textContent = "No device confirmation within 10 s. Is the device connected?"; }
    }, 250);
  } catch (e) { $("#condMsg").textContent = e.message; }
}));

// ------------------------------------------------------------ research --
let researchLoaded = false;
async function loadResearch() {
  researchLoaded = true;
  const rc = await api("/api/v1/admin/research-config");
  $("#participant_code").value = rc.participant_code || ""; $("#session_number").value = rc.session_number ?? "";
  for (const k of ["retain_transcripts", "retain_audio", "loopback_test"]) $("#" + k).checked = !!rc[k];
}
$("#saveResearch").addEventListener("click", async () => {
  const sn = $("#session_number").value;
  const changes = { participant_code: $("#participant_code").value.trim().toUpperCase(), session_number: sn ? +sn : null,
    retain_transcripts: $("#retain_transcripts").checked, retain_audio: $("#retain_audio").checked, loopback_test: $("#loopback_test").checked };
  try { await api("/api/v1/admin/research-config", { method: "PUT", body: { changes, consent_recorded: $("#consent_recorded").checked } }); flash($("#researchMsg"), "Saved"); refreshStatus(); }
  catch (e) { flash($("#researchMsg"), e.message, false); loadResearch(); }
});
$$("#probeRow [data-p]").forEach(b => b.addEventListener("click", () =>
  api("/api/v1/admin/probe", { method: "POST", body: { status: b.dataset.p, duration_ms: Math.round(+$("#probeDur").value * 1000) } }).catch(e => alert(e.message))));
$$("[data-d]").forEach(b => b.addEventListener("click", () => api("/api/v1/admin/led-diag", { method: "POST", body: { pattern: b.dataset.d } }).catch(e => alert(e.message))));
$("#saveCal").addEventListener("click", () => putConfig({
  led_offset: +$("#led_offset").value, led_reverse: $("#led_reverse").checked, led_grb: $("#led_grb").checked,
  afterglow_ms: +$("#afterglow_ms").value, silence_endpoint_ms: +$("#silence_endpoint_ms").value,
  vad_abs_dbfs: +$("#vad_abs_dbfs").value, vad_snr_db: +$("#vad_snr_db").value }, $("#calMsg")));

$("#textForm").addEventListener("submit", async e => {
  e.preventDefault();
  const text = $("#textIn").value.trim(); if (!text) return;
  $("#textIn").value = "";
  const div = document.createElement("div"); div.className = "bubble"; div.innerHTML = `<b>You:</b> ${esc(text)}<div class="meta">waiting...</div>`;
  $("#textLog").prepend(div);
  try {
    const r = await api("/api/v1/admin/simulate/text-turn", { method: "POST", body: { text, history: textHistory } });
    const rep = r.reply, v = r.validation;
    textHistory.push({ user: text, assistant: rep.reply_text }); textHistory = textHistory.slice(-6);
    div.innerHTML = `<b>You:</b> ${esc(text)}<br><b>Companion:</b> ${esc(rep.reply_text)}
      <div class="meta">${r.labels.map(esc).join(" · ")} · topic ${rep.topic} (${rep.topic_confidence}) · safety ${rep.safety_flag}
      · context ${rep.uses_verified_context} · ${Math.round(rep.latency_ms.llm)} ms · ${esc(r.model || "")}
      ${v.replaced ? ` · <span class="err">replaced: ${esc(v.rules.join(","))}</span>` : ""}${v.trimmed_questions || v.trimmed_sentences ? " · trimmed" : ""}</div>`;
  } catch (err) { div.querySelector(".meta").textContent = "Error: " + err.message; div.querySelector(".meta").className = "meta err"; }
});
$("#textClear").addEventListener("click", () => { textHistory = []; $("#textLog").innerHTML = ""; });

// -------------------------------------------------------- personal context --
async function refreshContext() {
  CONTEXT = await api("/api/v1/admin/context");
  const p = CONTEXT.profile || {};
  $("#preferred_name").value = p.preferred_name || ""; $("#communication_style").value = p.communication_style || "";
  const statusLabel = { living: "Living", deceased_verified: "Has died (verified)", unknown: "Not specified" };
  renderTable("people", CONTEXT.people, r => `<b>${esc(r.name)}</b> · ${esc(r.relationship)} · ${statusLabel[r.status]}${r.notes ? " · " + esc(r.notes) : ""}`);
  renderTable("routines", CONTEXT.routines, r => `${r.time_label ? "<b>" + esc(r.time_label) + ":</b> " : ""}${esc(r.description)}`);
  renderTable("activities", CONTEXT.activities, r => esc(r.description));
  $("#voiceTable").innerHTML = CONTEXT.voice_messages.map(v => `<tr><td><b>${esc(v.label)}</b> ${v.description ? "· " + esc(v.description) : ""}
     <span class="muted">${v.duration_s}s</span></td>
     <td><button class="link" data-play="${v.id}">Play</button></td>
     <td><label class="check"><input type="checkbox" data-vapprove="${v.id}" ${v.approved ? "checked" : ""}> Approved</label></td>
     <td><button class="link" data-vdel="${v.id}">Delete</button></td></tr>`).join("") || '<tr><td class="muted">None yet</td></tr>';
  $("#promptPreview").textContent = CONTEXT.prompt_preview;
}
function renderTable(table, rows, fmt) {
  $(`#${table}Table`).innerHTML = rows.map(r => `<tr><td>${fmt(r)}</td>
    <td><label class="check"><input type="checkbox" data-approve="${table}:${r.id}" ${r.approved ? "checked" : ""}> Approved</label></td>
    <td><button class="link" data-del="${table}:${r.id}">Delete</button></td></tr>`).join("") || '<tr><td class="muted">None yet</td></tr>';
}
document.addEventListener("change", async e => {
  const a = e.target.dataset.approve, va = e.target.dataset.vapprove;
  try {
    if (a) {
      const [table, id] = a.split(":"); const row = CONTEXT[table].find(r => r.id == id);
      const { id: _i, updated_at: _u, ...body } = row; body.approved = e.target.checked;
      await api(`/api/v1/admin/context/${table}/${id}`, { method: "PUT", body });
      refreshContext();
    } else if (va) { await api(`/api/v1/admin/voice-messages/${va}`, { method: "PATCH", body: { approved: e.target.checked } }); refreshContext(); }
  } catch (err) { alert(err.message); refreshContext(); }
});
document.addEventListener("click", async e => {
  const d = e.target.dataset;
  try {
    if (d.del && confirm("Delete this item?")) { const [t, id] = d.del.split(":"); await api(`/api/v1/admin/context/${t}/${id}`, { method: "DELETE" }); refreshContext(); }
    if (d.vdel && confirm("Delete this recording?")) { await api(`/api/v1/admin/voice-messages/${d.vdel}`, { method: "DELETE" }); refreshContext(); }
    if (d.play) { const blob = await api(`/api/v1/admin/voice-messages/${d.play}/audio`); new Audio(URL.createObjectURL(blob)).play(); }
  } catch (err) { alert(err.message); }
});
function formHandler(formId, table) {
  $(formId).addEventListener("submit", async e => {
    e.preventDefault();
    const f = e.target, body = Object.fromEntries(new FormData(f)); body.approved = f.approved.checked;
    try { await api(`/api/v1/admin/context/${table}`, { method: "POST", body }); f.reset(); refreshContext(); } catch (err) { alert(err.message); }
  });
}
formHandler("#personForm", "people"); formHandler("#routineForm", "routines"); formHandler("#activityForm", "activities");
$("#saveProfile").addEventListener("click", async () => {
  try { await api("/api/v1/admin/context/profile", { method: "PUT", body: { preferred_name: $("#preferred_name").value, communication_style: $("#communication_style").value } });
    flash($("#profileMsg"), "Saved"); refreshContext(); } catch (e) { flash($("#profileMsg"), e.message, false); }
});
$("#deleteAll").addEventListener("click", async () => {
  if (prompt('Type DELETE to remove all personal information and recordings') !== "DELETE") return;
  try { const r = await api("/api/v1/admin/context?confirm=DELETE", { method: "DELETE" }); alert(`Deleted ${r.deleted_items} items.`); refreshContext(); } catch (e) { alert(e.message); }
});

// --------------------------------------------- voice recording -> WAV --
let recorder = null, recChunks = [], recBlob = null, recTimer = null;
$("#recBtn").addEventListener("click", async () => {
  if (recorder && recorder.state === "recording") { recorder.stop(); return; }
  if (!navigator.mediaDevices || !window.MediaRecorder) { flash($("#vmMsg"), "Recording needs a secure page (open the dashboard at http://localhost:8000) - or upload a WAV file instead.", false); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    recChunks = []; recorder = new MediaRecorder(stream);
    recorder.ondataavailable = e => recChunks.push(e.data);
    recorder.onstop = async () => {
      stream.getTracks().forEach(t => t.stop()); clearInterval(recTimer);
      $("#recBtn").textContent = "● Record";
      try { recBlob = await toWav(new Blob(recChunks, { type: recorder.mimeType })); $("#recState").textContent = "Recorded. Add a label, then Upload."; }
      catch (e) { $("#recState").textContent = "Could not convert the recording: " + e.message; }
    };
    recorder.start(); const t0 = Date.now();
    $("#recBtn").textContent = "■ Stop";
    recTimer = setInterval(() => { const s = Math.round((Date.now() - t0) / 1000); $("#recState").textContent = `Recording ${s}s`; if (s >= 60) recorder.stop(); }, 250);
  } catch (e) { flash($("#vmMsg"), "Microphone unavailable: " + e.message, false); }
});
async function toWav(blob) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const buf = await ctx.decodeAudioData(await blob.arrayBuffer()); ctx.close();
  const n = buf.length, ch = buf.numberOfChannels, mono = new Float32Array(n);
  for (let c = 0; c < ch; c++) { const d = buf.getChannelData(c); for (let i = 0; i < n; i++) mono[i] += d[i] / ch; }
  const out = new DataView(new ArrayBuffer(44 + n * 2));
  const w = (o, s) => [...s].forEach((c, i) => out.setUint8(o + i, c.charCodeAt(0)));
  w(0, "RIFF"); out.setUint32(4, 36 + n * 2, true); w(8, "WAVE"); w(12, "fmt "); out.setUint32(16, 16, true);
  out.setUint16(20, 1, true); out.setUint16(22, 1, true); out.setUint32(24, buf.sampleRate, true); out.setUint32(28, buf.sampleRate * 2, true);
  out.setUint16(32, 2, true); out.setUint16(34, 16, true); w(36, "data"); out.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) out.setInt16(44 + i * 2, Math.max(-1, Math.min(1, mono[i])) * 32767, true);
  return new Blob([out], { type: "audio/wav" });
}
$("#vmUpload").addEventListener("click", async () => {
  const file = $("#vmFile").files[0] || recBlob; const label = $("#vmLabel").value.trim();
  if (!file) return flash($("#vmMsg"), "Record or choose a WAV file first", false);
  if (!label) return flash($("#vmMsg"), "Add a label", false);
  const fd = new FormData(); fd.append("file", file, "message.wav");
  const q = new URLSearchParams({ label, description: $("#vmDesc").value.trim(), approved: $("#vmApproved").checked });
  try { await api(`/api/v1/admin/voice-messages?${q}`, { method: "POST", body: fd }); flash($("#vmMsg"), "Uploaded");
    recBlob = null; $("#vmFile").value = ""; $("#vmLabel").value = ""; $("#vmDesc").value = ""; $("#recState").textContent = ""; refreshContext();
  } catch (e) { flash($("#vmMsg"), e.message, false); }
});

// ------------------------------------------------------- data & metrics --
const fmt = m => m && m.n ? `${m.median} / ${m.p95} / n=${m.n}` : "-";
async function refreshMetrics() {
  const m = await api("/api/v1/admin/metrics");
  if (!m.groups.length) { $("#metrics").innerHTML = '<p class="muted">No events yet.</p>'; return; }
  const rows = [["Button → listening state (ms) · target ≤150", g => fmt(g.button_to_listening_state_ms)],
    ["Release → first reply audio (ms) · target median ≤3000, p95 ≤6000", g => fmt(g.release_to_first_audio_ms)],
    ["Speech-to-text (ms)", g => fmt(g.stt_ms)], ["Language model (ms)", g => fmt(g.llm_ms)],
    ["TTS first audio (ms)", g => fmt(g.tts_first_audio_ms)], ["Condition change → device ack (ms) · target ≤2000", g => fmt(g.condition_update_ms)],
    ["Wi-Fi loss → offline (ms) · target ≤5000", g => fmt(g.wifi_loss_to_offline_ms)],
    ["LED late frames · target ≤1%", g => g.led_frames ? `${g.led_dropped_frame_pct}% of ${g.led_frames}` : "-"],
    ["Turns / failure rate", g => `${g.turns} / ${g.turn_failure_rate ?? "-"}`], ["Safety flags / replies replaced", g => `${g.safety_flags} / ${g.replies_replaced}`],
    ["Errors", g => esc(JSON.stringify(g.error_counts))]];
  $("#metrics").innerHTML = `<div class="scroll"><table class="list small"><tr><th></th>${m.groups.map(g => `<th>${g.run_mode}<br>${g.ai_mode}</th>`).join("")}</tr>
    ${rows.map(([l, f]) => `<tr><td>${l}</td>${m.groups.map(g => `<td>${f(g)}</td>`).join("")}</tr>`).join("")}</table></div>`;
}
async function refreshEvents() {
  const ev = await api("/api/v1/admin/events?limit=300");
  const cols = ["id", "received_at", "run_mode", "ai_mode", "source", "name", "condition", "state", "turn_id", "value", "duration_ms", "code", "detail"];
  $("#eventsTable").innerHTML = `<tr>${cols.map(c => `<th>${c}</th>`).join("")}</tr>` + ev.map(e => `<tr>${cols.map(c => `<td>${esc(e[c])}</td>`).join("")}</tr>`).join("");
}
$("#refreshMetrics").addEventListener("click", () => { refreshMetrics(); refreshEvents(); });
async function download(fmtName) {
  const blob = await api(`/api/v1/admin/export?fmt=${fmtName}`);
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob instanceof Blob ? blob : new Blob([JSON.stringify(blob)]));
  a.download = `unscripted_events.${fmtName}`; a.click();
}
$("#expCsv").addEventListener("click", () => download("csv").catch(e => alert(e.message)));
$("#expJson").addEventListener("click", () => download("json").catch(e => alert(e.message)));
$("#deleteEvents").addEventListener("click", async () => {
  if (prompt("Type DELETE to remove all research events and transcripts") !== "DELETE") return;
  try { const r = await api("/api/v1/admin/events?confirm=DELETE", { method: "DELETE" }); alert(`Deleted ${r.events_deleted} events.`); refreshEvents(); } catch (e) { alert(e.message); }
});

// ----------------------------------------------------------------- boot --
const saved = load("tok");
if (saved) login(saved).catch(() => store("tok", null));
