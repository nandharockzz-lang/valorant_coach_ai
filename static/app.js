const els = {
  recordingDir: document.querySelector("#recordingDir"),
  videoPath: document.querySelector("#videoPath"),
  status: document.querySelector("#status"),
  versionBadge: document.querySelector("#versionBadge"),
  matchesList: document.querySelector("#matchesList"),
  trendsView: document.querySelector("#trendsView"),
  capabilitiesView: document.querySelector("#capabilitiesView"),
  calibrationView: document.querySelector("#calibrationView"),
  automationView: document.querySelector("#automationView"),
  coachView: document.querySelector("#coachView"),
  reportView: document.querySelector("#reportView"),
  saveSettingsBtn: document.querySelector("#saveSettingsBtn"),
  saveCalibrationBtn: document.querySelector("#saveCalibrationBtn"),
  scanBtn: document.querySelector("#scanBtn"),
  importBtn: document.querySelector("#importBtn"),
  refreshBtn: document.querySelector("#refreshBtn"),
};

let currentMatchId = null;
let latestJobs = [];
let activeCoachJobId = null;
let jobPollTimer = null;
const completedJobIds = new Set();
let currentCalibration = {};
const CALIBRATION_REGIONS = [
  ["hud_top", "Top HUD"],
  ["hud_bottom", "Bottom HUD"],
  ["killfeed", "Killfeed"],
  ["minimap", "Minimap"],
  ["crosshair", "Crosshair"],
  ["combat_report", "Combat Report"],
];
let selectedCalibrationRegion = "minimap";
let dragCalibration = null;
const LABEL_PRESETS = [
  "dry peek",
  "crosshair too low/wide",
  "exposed to multiple angles",
  "poor reposition after contact",
  "isolated from team",
  "repeated same-angle fight",
  "late rotation / bad timing",
  "utility unused before taking space",
];

function setStatus(message) {
  els.status.textContent = message;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || payload.message || `Request failed: ${response.status}`);
  }
  return payload;
}

async function loadSettings() {
  const payload = await api("/api/settings");
  els.recordingDir.value = payload.recording_dir || "";
}

async function loadVersionBadge() {
  const version = await api("/api/version");
  const commit = version.git || {};
  const build = commit.commit_count ? `build ${commit.commit_count}` : version.build || "local";
  const hash = commit.short_hash ? ` · ${commit.short_hash}` : "";
  const dirty = commit.dirty ? " · dirty" : "";
  els.versionBadge.textContent = `${version.version} · ${build}${hash}${dirty}`;
  els.versionBadge.title = `Version ${version.version}\nBuild ${build}${hash}${dirty}\nBranch ${commit.branch || "unknown"}\nCommit date ${commit.commit_date || "unknown"}`;
}

async function loadAutomation() {
  const [settings, jobs, watcher, storage, analytics, logs, tools, backups, schema, version, providers, privacy, corrections, playbookPayload, diagnostics, evaluation, plugins, localAi, setup, prompts, tuning, modelAudit, sessionReport] = await Promise.all([
    api("/api/settings"),
    api("/api/jobs"),
    api("/api/watcher"),
    api("/api/storage"),
    api("/api/analytics"),
    api("/api/logs"),
    api("/api/tools"),
    api("/api/backups"),
    api("/api/schema"),
    api("/api/version"),
    api("/api/providers"),
    api("/api/privacy"),
    api("/api/corrections"),
    api("/api/playbooks"),
    api("/api/diagnostics"),
    api("/api/evaluation"),
    api("/api/plugins"),
    api("/api/local-ai"),
    api("/api/setup"),
    api("/api/prompts"),
    api("/api/detector/tuning"),
    api("/api/privacy/model-audit"),
    api("/api/sessions/report"),
  ]);
  renderAutomation(
    settings,
    jobs.jobs || [],
    watcher.watcher || {},
    storage.storage || {},
    analytics,
    logs.logs || [],
    tools,
    backups.backups || [],
    schema,
    version,
    providers,
    privacy,
    corrections.corrections || [],
    playbookPayload.playbooks || {},
    diagnostics,
    evaluation,
    plugins,
    localAi,
    setup,
    prompts,
    tuning,
    modelAudit,
    sessionReport
  );
  latestJobs = jobs.jobs || [];
  renderJobProgressPanel();
}

function renderAutomation(settings, jobs, watcher, storage, analytics, logs, tools, backups, schema, version, providers, privacy, corrections, playbooks, diagnostics, evaluation, plugins, localAi, setup, prompts, tuning, modelAudit, sessionReport) {
  const jobRows = jobs.slice(0, 8).map((job) => `
    <li>
      <strong>#${job.id} ${escapeHtml(job.name)}</strong>
      <span>${escapeHtml(job.status)} · ${Number(job.progress || 0)}%</span>
      <p class="muted">${escapeHtml(job.message || "")}</p>
      <details>
        <summary>Details</summary>
        <pre class="json-small">${escapeHtml(JSON.stringify(job.result || job.error || {}, null, 2))}</pre>
      </details>
      ${["queued", "running"].includes(job.status) ? `<button class="danger" data-action="cancel-job" data-id="${job.id}">Cancel</button>` : ""}
    </li>
  `).join("");
  const bytes = (value) => `${Math.round(Number(value || 0) / 1024 / 1024)} MB`;
  const toolRows = Object.entries(tools).map(([name, item]) => `
    <li><strong>${escapeHtml(name)}</strong>: ${item.available ? "ready" : "missing"} <span class="muted">${escapeHtml(item.path || item.install || "")}</span></li>
  `).join("");
  const logRows = logs.slice(0, 6).map((item) => `<li><strong>${escapeHtml(item.level)}</strong> ${escapeHtml(item.source)}: ${escapeHtml(item.message)}</li>`).join("");
  const backupOptions = backups.map((item) => `<option value="${escapeAttr(item.path)}">${escapeHtml(fileName(item.path))}</option>`).join("");
  const providerRows = renderProviderRows(providers);
  const pluginRows = (plugins.plugins || []).map((item) => `
    <li><strong>${escapeHtml(item.name)}</strong>: ${item.enabled ? "enabled" : "disabled"} <span class="muted">${escapeHtml(item.privacy || "")}</span></li>
  `).join("");
  const diagnosticRows = (diagnostics.checks || []).slice(0, 10).map((item) => `
    <li><strong>${escapeHtml(item.name)}</strong>: ${item.ok ? "ok" : "check"}</li>
  `).join("");
  const correctionRows = corrections.slice(0, 8).map((item) => `
    <li>
      <strong>#${item.id} ${escapeHtml(item.subject_type)} ${escapeHtml(item.subject_id)}</strong>
      <span>${escapeHtml(item.analysis_type)}</span>
      <button class="secondary" data-action="apply-correction" data-id="${item.id}">Apply</button>
      <pre class="json-small">${escapeHtml(JSON.stringify(item.payload || {}, null, 2))}</pre>
    </li>
  `).join("");
  const playbookOptions = Object.keys(playbooks).sort().map((key) => `<option value="${escapeAttr(key)}">${escapeHtml(key)}</option>`).join("");
  const chartBars = renderAnalyticsBars(analytics);
  const changelog = (version.changelog || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const setupRows = (setup.steps || []).map((item) => `<li><strong>${escapeHtml(item.label)}</strong>: ${item.ok ? "ready" : item.optional ? "optional" : "needed"}</li>`).join("");
  const promptOptions = Object.keys(prompts.templates || {}).sort().map((key) => `<option value="${escapeAttr(key)}" ${prompts.active === key ? "selected" : ""}>${escapeHtml(key)}</option>`).join("");
  els.automationView.innerHTML = `
    <div class="automation-block">
      <h3>Setup Wizard</h3>
      <p>${setup.ready ? "Ready for review workflow." : "Finish required setup before relying on automation."}</p>
      <ul class="compact-list">${setupRows}</ul>
      <label>Recording folder <input id="setupRecordingDir" type="text" value="${escapeAttr(settings.recording_dir || "")}" /></label>
      <div class="row">
        <button data-action="save-setup">Save Setup</button>
        <button class="secondary" data-action="refresh-diagnostics">Refresh Checks</button>
      </div>
    </div>
    <div class="automation-block">
      <h3>Release</h3>
      <p><strong>${escapeHtml(version.version || "local")}</strong> · ${escapeHtml(version.build || "dev")}</p>
      <p class="muted">Schema ${(schema.items || []).map((item) => `${item.key}=${item.value}`).join(", ")}</p>
      <ul class="compact-list">${changelog}</ul>
    </div>
    <div class="automation-controls">
      <label>Auto import
        <select id="autoImport">
          <option value="false" ${settings.auto_import !== "true" ? "selected" : ""}>Off</option>
          <option value="true" ${settings.auto_import === "true" ? "selected" : ""}>On</option>
        </select>
      </label>
      <label>Auto analysis
        <select id="autoAnalysis">
          <option value="false" ${settings.auto_analysis !== "true" ? "selected" : ""}>Off</option>
          <option value="true" ${settings.auto_analysis === "true" ? "selected" : ""}>On</option>
        </select>
      </label>
      <label>Detector sensitivity
        <select id="detectorSensitivity">
          ${["low", "normal", "high"].map((item) => `<option value="${item}" ${settings.detector_sensitivity === item ? "selected" : ""}>${item}</option>`).join("")}
        </select>
      </label>
      <label>Frame sample rate
        <select id="frameSampleRate">
          ${["light", "standard", "dense"].map((item) => `<option value="${item}" ${settings.frame_sample_rate === item ? "selected" : ""}>${item}</option>`).join("")}
        </select>
      </label>
      <label>Skip completed
        <select id="skipCompletedAnalysis">
          <option value="true" ${settings.skip_completed_analysis !== "false" ? "selected" : ""}>On</option>
          <option value="false" ${settings.skip_completed_analysis === "false" ? "selected" : ""}>Off</option>
        </select>
      </label>
      <label>Max jobs <input id="maxConcurrentJobs" type="number" min="1" max="4" value="${escapeAttr(settings.max_concurrent_jobs || "1")}" /></label>
      <button data-action="save-automation">Save Settings</button>
      <button class="secondary" data-action="start-watcher">${watcher.running ? "Watcher Running" : "Start Watcher"}</button>
      <button class="secondary" data-action="stop-watcher">Stop Watcher</button>
    </div>
    <div class="automation-block">
      <h3>Jobs</h3>
      <ul class="compact-list">${jobRows || "<li>No jobs yet.</li>"}</ul>
    </div>
    <div class="automation-block">
      <h3>Storage</h3>
      <p class="muted">clips ${bytes(storage.clips)} · reports ${bytes(storage.reports)} · frames ${bytes((storage.vision || 0) + (storage.deep || 0))}</p>
      <div class="row">
        <button class="danger" data-action="cleanup-frames">Delete Frames</button>
        <button class="danger" data-action="cleanup-clips">Delete Clips</button>
        <button class="danger" data-action="retention">Apply Retention</button>
      </div>
    </div>
    <div class="automation-block">
      <h3>Tools</h3>
      <ul class="compact-list">${toolRows}</ul>
    </div>
    <div class="automation-block">
      <h3>Providers</h3>
      <ul class="compact-list">${providerRows}</ul>
    </div>
    <div class="automation-block">
      <h3>Plugins</h3>
      <ul class="compact-list">${pluginRows}</ul>
      <label>Provider
        <select id="localAiProvider">
          ${(localAi.providers || []).map((item) => `<option value="${escapeAttr(item.id)}" ${localAi.provider === item.id ? "selected" : ""}>${escapeHtml(item.label)}</option>`).join("")}
        </select>
      </label>
      <label>Model <input id="localAiModel" type="text" value="${escapeAttr(localAi.model || "")}" placeholder="llava" /></label>
      <label>Base URL <input id="localAiBaseUrl" type="text" value="${escapeAttr(localAi.base_url || "")}" placeholder="http://127.0.0.1:11434" /></label>
      <label>Custom command <input id="localAiCommand" type="text" value="${escapeAttr(localAi.command || "")}" placeholder="python C:\\path\\review_clip.py" /></label>
      <div class="row">
        <button class="secondary" data-action="use-lmstudio-defaults">Use LM Studio Defaults</button>
        <button class="secondary" data-action="test-local-ai">Test Local AI</button>
        <button class="secondary" data-action="save-local-ai">Save Local AI</button>
      </div>
      <div id="localAiTestResult" class="muted"></div>
      <p class="muted">${escapeHtml(localAi.expected_protocol || "")}</p>
    </div>
    <div class="automation-block">
      <h3>Prompt System</h3>
      <label>Template <select id="promptTemplateSelect">${promptOptions}</select></label>
      <label>Key <input id="promptKey" type="text" placeholder="duelist-custom" /></label>
      <label>Name <input id="promptName" type="text" placeholder="My review prompt" /></label>
      <label>Role <input id="promptRole" type="text" placeholder="duelist" /></label>
      <label>Prompt <textarea id="promptText" rows="5" placeholder="Use {round}, {timestamp}, {labels}, {notes}"></textarea></label>
      <div class="row">
        <button class="secondary" data-action="load-prompt">Load</button>
        <button data-action="save-prompt">Save Prompt</button>
      </div>
    </div>
    <div class="automation-block">
      <h3>Diagnostics</h3>
      <p>${escapeHtml(diagnostics.summary || "")}</p>
      <ul class="compact-list">${diagnosticRows}</ul>
      <button class="secondary" data-action="refresh-diagnostics">Run Diagnostics</button>
    </div>
    <div class="automation-block">
      <h3>Benchmark</h3>
      <p>${escapeHtml(evaluation.summary || "")}</p>
      <p class="muted">Measured precision ${escapeHtml((evaluation.metrics || {}).measured_precision ?? "n/a")} · measured recall ${escapeHtml((evaluation.metrics || {}).measured_recall ?? "n/a")} · proxy ${escapeHtml((evaluation.metrics || {}).precision_proxy ?? "n/a")}</p>
      <button class="secondary" data-action="refresh-evaluation">Run Benchmark</button>
    </div>
    <div class="automation-block">
      <h3>Detector Tuning</h3>
      <p>${escapeHtml(tuning.summary || "")}</p>
      <p class="muted">Current ${escapeHtml(tuning.current || "normal")} · recommended ${escapeHtml(tuning.recommended || "normal")}</p>
      <button class="secondary" data-action="apply-detector-tuning">Apply Tuning</button>
    </div>
    <div class="automation-block">
      <h3>Backups</h3>
      <button class="secondary" data-action="backup-db">Backup DB</button>
      <label>Restore <select id="backupPath">${backupOptions || "<option value=''>No backups</option>"}</select></label>
      <button class="danger" data-action="restore-db">Restore DB</button>
    </div>
    <div class="automation-block">
      <h3>Analytics</h3>
      <p>${escapeHtml((analytics.summary || {}).matches || 0)} match(es), top issue: ${escapeHtml((analytics.summary || {}).top_mistake || "none")}</p>
      <p class="muted">Detector accepted ${escapeHtml((analytics.detector || {}).accepted || 0)}, rejected ${escapeHtml((analytics.detector || {}).rejected || 0)}</p>
      <div class="bar-chart">${chartBars}</div>
    </div>
    <div class="automation-block">
      <h3>Advanced Search</h3>
      <label>Text <input id="searchText" type="text" placeholder="notes, map, agent, label" /></label>
      <label>Label <input id="searchLabel" type="text" placeholder="dry peek" /></label>
      <label>Phase <input id="searchPhase" type="text" placeholder="late round" /></label>
      <label>Min confidence <input id="searchConfidence" type="number" min="0" max="1" step="0.05" value="0" /></label>
      <label>Clip
        <select id="searchClip">
          <option value="">Any</option>
          <option value="with_clip">With clip</option>
          <option value="without_clip">Without clip</option>
        </select>
      </label>
      <button class="secondary" data-action="search-deaths">Search Deaths</button>
      <div id="searchResults" class="search-results muted"></div>
    </div>
    <div class="automation-block">
      <h3>Playbook Editor</h3>
      <label>Existing
        <select id="playbookSelect">
          <option value="">New playbook</option>
          ${playbookOptions}
        </select>
      </label>
      <label>Key <input id="playbookKey" type="text" placeholder="Ascent:Jett" /></label>
      <label>Summary <input id="playbookSummary" type="text" placeholder="Default fighting rules for this map/agent" /></label>
      <label>Rules <textarea id="playbookRules" rows="4" placeholder="One rule per line"></textarea></label>
      <label>Drills <textarea id="playbookDrills" rows="3" placeholder="One drill per line"></textarea></label>
      <div class="row">
        <button class="secondary" data-action="load-playbook-editor">Load</button>
        <button data-action="save-playbook">Save Playbook</button>
        <button class="danger" data-action="delete-playbook">Delete Playbook</button>
      </div>
    </div>
    <div class="automation-block">
      <h3>Correction Queue</h3>
      <ul class="compact-list">${correctionRows || "<li>No corrections queued.</li>"}</ul>
    </div>
    <div class="automation-block">
      <h3>Privacy Audit</h3>
      <p>Mode ${escapeHtml(privacy.privacy_mode || "local-only")} · uploads ${escapeHtml(privacy.network_uploads || "disabled")}</p>
      <p class="muted">${escapeHtml((privacy.data_categories || []).join(", "))}</p>
      <div class="row">
        <button class="secondary" data-action="privacy-export">Export Data</button>
        <button class="secondary" data-action="debug-bundle">Debug Bundle</button>
        <button class="danger" data-action="privacy-wipe-frames">Wipe Frames</button>
        <button class="danger" data-action="privacy-wipe-clips">Wipe Clips</button>
      </div>
      <p class="muted">${escapeHtml((modelAudit || {}).summary || "")}</p>
    </div>
    <div class="automation-block">
      <h3>Session Report</h3>
      <p>${escapeHtml(((sessionReport || {}).report || {}).summary || "No session report yet.")}</p>
      <ul class="compact-list">${((((sessionReport || {}).report || {}).next_drills || []).slice(0, 3).map((item) => `<li>${escapeHtml(item)}</li>`).join(""))}</ul>
      <button class="secondary" data-action="session-report">Refresh Session Report</button>
    </div>
    <div class="automation-block">
      <h3>Memory</h3>
      <button class="secondary" data-action="export-memory">Export</button>
      <label>Import JSON <input id="memoryImport" type="file" accept="application/json" /></label>
      <button class="secondary" data-action="import-memory">Import</button>
    </div>
    <div class="automation-block">
      <h3>Exports / Imports</h3>
      <button class="secondary" data-action="export-report-json">Export Report JSON</button>
      <button class="secondary" data-action="export-report-html">Export Report HTML</button>
      <label>Stats file path <input id="statsImportPath" type="text" placeholder="C:\\path\\tracker_stats.json" /></label>
      <button class="secondary" data-action="import-stats">Import Stats</button>
    </div>
    <div class="automation-block">
      <h3>Logs</h3>
      <ul class="compact-list">${logRows || "<li>No logs yet.</li>"}</ul>
    </div>
  `;
  window.__coachPlaybooks = playbooks;
  window.__coachPrompts = prompts.templates || {};
}

function renderProviderRows(providers) {
  return Object.entries(providers || {}).flatMap(([category, items]) =>
    (items || []).map((item) => `
      <li>
        <strong>${escapeHtml(category)} / ${escapeHtml(item.id)}</strong>:
        ${escapeHtml(item.status || "unknown")}
        <span class="muted">${escapeHtml(item.privacy || "")}</span>
      </li>
    `)
  ).join("");
}

function renderAnalyticsBars(analytics) {
  const labelCounts = (analytics.trends || {}).labels || analytics.labels || analytics.mistakes || {};
  const rows = (Array.isArray(labelCounts) ? labelCounts : Object.entries(labelCounts).map(([label, count]) => ({ label, count }))).slice(0, 6);
  const max = Math.max(1, ...rows.map((item) => Number(item.count || item.value || 0)));
  return rows.map((item) => {
    const label = item.label || item.name || "unknown";
    const count = Number(item.count || item.value || 0);
    const width = Math.max(4, Math.round((count / max) * 100));
    return `
      <div class="bar-row">
        <span>${escapeHtml(label)}</span>
        <i style="width:${width}%"></i>
        <b>${escapeHtml(count)}</b>
      </div>
    `;
  }).join("") || '<p class="muted">No trend bars yet.</p>';
}

async function saveAutomationSettings() {
  await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({
      recording_dir: els.recordingDir.value,
      auto_import: document.querySelector("#autoImport").value,
      auto_analysis: document.querySelector("#autoAnalysis").value,
      detector_sensitivity: document.querySelector("#detectorSensitivity").value,
      frame_sample_rate: document.querySelector("#frameSampleRate").value,
      skip_completed_analysis: document.querySelector("#skipCompletedAnalysis").value,
      max_concurrent_jobs: document.querySelector("#maxConcurrentJobs").value,
      privacy_mode: "local-only",
      ocr_engine: "tesseract",
    }),
  });
  setStatus("Automation settings saved.");
  await loadAutomation();
}

async function cancelJob(id) {
  await api(`/api/jobs/${id}/cancel`, { method: "POST" });
  setStatus(`Cancel requested for job #${id}.`);
  await loadAutomation();
}

async function applyRetention() {
  await api("/api/storage/retention", { method: "POST" });
  setStatus("Retention policy applied.");
  await loadAutomation();
}

async function backupDb() {
  const payload = await api("/api/backups/create", { method: "POST" });
  setStatus(payload.ok ? `Backup created: ${payload.path}` : payload.message);
  await loadAutomation();
}

async function restoreDb() {
  const path = document.querySelector("#backupPath").value;
  if (!path) {
    setStatus("Choose a backup first.");
    return;
  }
  const payload = await api("/api/backups/restore", { method: "POST", body: JSON.stringify({ path }) });
  setStatus(payload.message || "Database restored.");
}

async function searchDeaths() {
  const payload = await api("/api/search/advanced", {
    method: "POST",
    body: JSON.stringify({
      text: document.querySelector("#searchText").value,
      label: document.querySelector("#searchLabel").value,
      phase: document.querySelector("#searchPhase").value,
      confidence_min: document.querySelector("#searchConfidence").value,
      clip: document.querySelector("#searchClip").value,
    }),
  });
  const target = document.querySelector("#searchResults");
  const rows = (payload.results || []).slice(0, 20).map((item) => {
    const death = item.death || {};
    const match = item.match || {};
    return `
      <tr>
        <td>#${escapeHtml(death.id || "")}</td>
        <td>${escapeHtml(match.map || "unknown")}</td>
        <td>${escapeHtml(match.agent || "unknown")}</td>
        <td>${escapeHtml(formatTs(death.timestamp))}</td>
        <td>${escapeHtml((death.mistake_labels || []).join(", "))}</td>
        <td>${escapeHtml(Math.round(Number(death.confidence || 0) * 100))}%</td>
      </tr>
    `;
  }).join("");
  target.innerHTML = `
    <p>${payload.count} result(s).</p>
    <table class="mini-table">
      <thead><tr><th>Death</th><th>Map</th><th>Agent</th><th>Time</th><th>Labels</th><th>Confidence</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6">No matches.</td></tr>'}</tbody>
    </table>
  `;
}

function loadPlaybookEditor() {
  const key = document.querySelector("#playbookSelect").value;
  const playbook = (window.__coachPlaybooks || {})[key] || {};
  document.querySelector("#playbookKey").value = key;
  document.querySelector("#playbookSummary").value = playbook.summary || "";
  document.querySelector("#playbookRules").value = (playbook.rules || []).join("\n");
  document.querySelector("#playbookDrills").value = (playbook.drills || []).join("\n");
}

async function savePlaybook() {
  const payload = await api("/api/playbooks", {
    method: "POST",
    body: JSON.stringify({
      key: document.querySelector("#playbookKey").value,
      summary: document.querySelector("#playbookSummary").value,
      rules: document.querySelector("#playbookRules").value,
      drills: document.querySelector("#playbookDrills").value,
    }),
  });
  setStatus(payload.ok ? `Playbook saved: ${payload.key}` : payload.message);
  await loadAutomation();
}

async function deletePlaybookFromEditor() {
  const key = document.querySelector("#playbookKey").value || document.querySelector("#playbookSelect").value;
  if (!key) {
    setStatus("Choose a playbook key first.");
    return;
  }
  await api(`/api/playbooks/${encodeURIComponent(key)}`, { method: "DELETE" });
  setStatus(`Playbook deleted: ${key}`);
  await loadAutomation();
}

async function applyCorrection(id) {
  const payload = await api(`/api/corrections/${id}/apply`, { method: "POST" });
  setStatus(payload.ok ? `Correction #${id} applied.` : payload.message);
  await Promise.all([loadAutomation(), currentMatchId ? loadReport(currentMatchId) : Promise.resolve()]);
}

async function saveLocalAiConfig() {
  const payload = {
    provider: document.querySelector("#localAiProvider").value,
    model: document.querySelector("#localAiModel").value,
    base_url: document.querySelector("#localAiBaseUrl").value,
    command: document.querySelector("#localAiCommand").value,
  };
  await api("/api/local-ai/config", { method: "POST", body: JSON.stringify(payload) });
  setStatus("Local AI settings saved.");
  await loadAutomation();
}

function useLmStudioDefaults() {
  const provider = document.querySelector("#localAiProvider");
  const model = document.querySelector("#localAiModel");
  const baseUrl = document.querySelector("#localAiBaseUrl");
  const command = document.querySelector("#localAiCommand");
  if (provider) provider.value = "lmstudio";
  if (baseUrl) baseUrl.value = "http://127.0.0.1:1234/v1";
  if (model && !model.value) model.value = "local-model";
  if (command) command.value = "";
  setStatus("LM Studio defaults filled. If LM Studio shows a specific model ID, paste it into Model before saving.");
}

async function testLocalAiConfig() {
  const payload = {
    provider: document.querySelector("#localAiProvider").value,
    model: document.querySelector("#localAiModel").value,
    base_url: document.querySelector("#localAiBaseUrl").value,
    command: document.querySelector("#localAiCommand").value,
  };
  const response = await fetch("/api/local-ai/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  const target = document.querySelector("#localAiTestResult");
  const models = (result.models || []).slice(0, 8);
  if (target) {
    target.innerHTML = `
      <p>${escapeHtml(result.message || "")}</p>
      ${models.length ? `<p>Models: ${models.map(escapeHtml).join(", ")}</p>` : ""}
    `;
  }
  if (result.ok && !payload.model && models.length) {
    document.querySelector("#localAiModel").value = models[0];
  }
  setStatus(result.message || "Local AI test complete.");
}

async function saveSetupWizard() {
  await api("/api/setup", {
    method: "POST",
    body: JSON.stringify({
      recording_dir: document.querySelector("#setupRecordingDir").value,
      auto_import: document.querySelector("#autoImport")?.value || "false",
      auto_analysis: document.querySelector("#autoAnalysis")?.value || "false",
      frame_sample_rate: document.querySelector("#frameSampleRate")?.value || "standard",
      detector_sensitivity: document.querySelector("#detectorSensitivity")?.value || "normal",
      local_ai_provider: document.querySelector("#localAiProvider")?.value || "custom-command",
      local_ai_model: document.querySelector("#localAiModel")?.value || "",
      local_ai_base_url: document.querySelector("#localAiBaseUrl")?.value || "",
      local_ai_command: document.querySelector("#localAiCommand")?.value || "",
    }),
  });
  els.recordingDir.value = document.querySelector("#setupRecordingDir").value;
  setStatus("Setup saved.");
  await loadAutomation();
}

function loadPromptEditor() {
  const key = document.querySelector("#promptTemplateSelect").value;
  const template = (window.__coachPrompts || {})[key] || {};
  document.querySelector("#promptKey").value = key;
  document.querySelector("#promptName").value = template.name || "";
  document.querySelector("#promptRole").value = template.role || "";
  document.querySelector("#promptText").value = template.prompt || "";
}

async function savePromptTemplate() {
  const payload = {
    key: document.querySelector("#promptKey").value,
    name: document.querySelector("#promptName").value,
    role: document.querySelector("#promptRole").value,
    prompt: document.querySelector("#promptText").value,
    active: true,
  };
  await api("/api/prompts", { method: "POST", body: JSON.stringify(payload) });
  setStatus(`Prompt saved: ${payload.key}`);
  await loadAutomation();
}

async function applyDetectorTuning() {
  const payload = await api("/api/detector/tuning/apply", { method: "POST" });
  setStatus(`Detector sensitivity set to ${payload.tuning.recommended}.`);
  await loadAutomation();
}

async function privacyExport() {
  const payload = await api("/api/privacy/export", { method: "POST" });
  setStatus(payload.message || `Privacy export written: ${payload.path}`);
}

async function debugBundle() {
  const payload = await api("/api/privacy/debug-bundle", { method: "POST" });
  setStatus(payload.message || `Debug bundle written: ${payload.path}`);
}

async function privacyWipe(targets) {
  await api("/api/privacy/wipe", { method: "POST", body: JSON.stringify({ targets }) });
  setStatus(`Privacy wipe complete: ${targets.join(", ")}.`);
  await loadAutomation();
}

async function refreshSessionReport() {
  const payload = await api("/api/sessions/report", { method: "POST", body: JSON.stringify({}) });
  setStatus((payload.report || {}).summary || "Session report refreshed.");
  await loadAutomation();
}

async function exportCurrentReport(format) {
  if (!currentMatchId) {
    setStatus("Select a match first.");
    return;
  }
  const payload = await api(`/api/matches/${currentMatchId}/report/export`, {
    method: "POST",
    body: JSON.stringify({ format }),
  });
  const blob = new Blob([payload.content], { type: format === "html" ? "text/html" : "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `match-${currentMatchId}.${format}`;
  link.click();
  URL.revokeObjectURL(url);
}

async function importStats() {
  const path = document.querySelector("#statsImportPath").value;
  const payload = await api("/api/stats/import", { method: "POST", body: JSON.stringify({ path }) });
  setStatus(payload.message || `Imported ${payload.imported} stat match(es).`);
  await Promise.all([loadMatches(), loadAutomation()]);
}

async function startWatcher() {
  await api("/api/watcher/start", { method: "POST" });
  setStatus("Watcher started.");
  await loadAutomation();
}

async function stopWatcher() {
  await api("/api/watcher/stop", { method: "POST" });
  setStatus("Watcher stopped.");
  await loadAutomation();
}

async function cleanupStorage(targets) {
  await api("/api/storage/cleanup", {
    method: "POST",
    body: JSON.stringify({ targets }),
  });
  setStatus("Storage cleanup complete.");
  await loadAutomation();
}

async function exportMemory() {
  const payload = await api("/api/memory/export");
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "valorant-coach-memory.json";
  link.click();
  URL.revokeObjectURL(url);
}

async function importMemory() {
  const input = document.querySelector("#memoryImport");
  const file = input?.files?.[0];
  if (!file) {
    setStatus("Choose a memory JSON file first.");
    return;
  }
  const payload = JSON.parse(await file.text());
  const result = await api("/api/memory/import", { method: "POST", body: JSON.stringify(payload) });
  setStatus(result.message);
  await Promise.all([loadCoach(), loadAutomation()]);
}

async function loadCapabilities() {
  const payload = await api("/api/capabilities");
  const item = (name, ready, path) => `
    <div class="capability ${ready ? "ready" : "missing"}">
      <strong>${escapeHtml(name)}</strong>
      <span>${ready ? "ready" : "missing"}</span>
      <small>${escapeHtml(path || "")}</small>
    </div>
  `;
  els.capabilitiesView.innerHTML = [
    item("ffmpeg", payload.ffmpeg, payload.ffmpeg_path),
    item("Tesseract OCR", payload.tesseract, payload.tesseract_path),
    item("PyInstaller", payload.pyinstaller, payload.pyinstaller_path),
  ].join("");
}

async function loadCalibration() {
  const payload = await api("/api/calibration");
  currentCalibration = payload.regions || {};
  renderCalibration(currentCalibration);
}

function renderCalibration(regions) {
  els.calibrationView.innerHTML = CALIBRATION_REGIONS.map(([key, label]) => {
    const region = regions[key] || { x: 0, y: 0, w: 0.1, h: 0.1 };
    return `
      <fieldset class="region-editor" data-region="${escapeAttr(key)}">
        <legend>${escapeHtml(label)}</legend>
        <label>x <input data-field="x" type="number" min="0" max="1" step="0.001" value="${escapeAttr(region.x ?? 0)}" /></label>
        <label>y <input data-field="y" type="number" min="0" max="1" step="0.001" value="${escapeAttr(region.y ?? 0)}" /></label>
        <label>w <input data-field="w" type="number" min="0.001" max="1" step="0.001" value="${escapeAttr(region.w ?? 0.1)}" /></label>
        <label>h <input data-field="h" type="number" min="0.001" max="1" step="0.001" value="${escapeAttr(region.h ?? 0.1)}" /></label>
      </fieldset>
    `;
  }).join("");
}

async function saveCalibration() {
  const regions = {};
  for (const editor of els.calibrationView.querySelectorAll(".region-editor")) {
    const name = editor.dataset.region;
    regions[name] = {};
    for (const input of editor.querySelectorAll("input")) {
      regions[name][input.dataset.field] = Number(input.value);
    }
  }
  const payload = await api("/api/calibration", {
    method: "POST",
    body: JSON.stringify({ regions }),
  });
  currentCalibration = payload.regions || regions;
  renderCalibration(currentCalibration);
  setStatus("Calibration saved.");
}

async function saveSettings() {
  await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({ recording_dir: els.recordingDir.value }),
  });
  setStatus("Recording folder saved.");
}

async function scanFolder() {
  setStatus("Scanning recording folder...");
  const payload = await api("/api/scan", { method: "POST" });
  setStatus(`Found ${payload.found} video(s).`);
  await loadMatches();
}

async function importVideo() {
  setStatus("Importing video...");
  const payload = await api("/api/videos/import", {
    method: "POST",
    body: JSON.stringify({ path: els.videoPath.value }),
  });
  setStatus(`Imported match #${payload.match_id}.`);
  await loadMatches();
}

async function analyzeMatch(id) {
  setStatus(`Analyzing match #${id}...`);
  const payload = await api(`/api/matches/${id}/analyze`, { method: "POST" });
  setStatus(`Analysis complete. ${payload.clips.message}`);
  await loadMatches();
  await loadReport(id);
  await loadTrends();
}

async function suggestDeaths(id) {
  setStatus(`Scanning match #${id} for death candidates...`);
  const payload = await api(`/api/matches/${id}/suggest-deaths`, { method: "POST" });
  setStatus(payload.message);
  await loadReport(id);
}

async function extractClips(id) {
  setStatus(`Extracting clips for match #${id}...`);
  const payload = await api(`/api/matches/${id}/clips`, { method: "POST" });
  setStatus(payload.clips.message);
  await loadReport(id);
}

async function analyzeDeathVision(deathId) {
  setStatus(`Analyzing clip frames for death #${deathId}...`);
  const payload = await api(`/api/deaths/${deathId}/vision`, { method: "POST" });
  setStatus(payload.message);
  if (currentMatchId) await loadReport(currentMatchId);
}

async function generateMatchReview(id) {
  setStatus(`Generating coach review for match #${id}...`);
  const payload = await api(`/api/matches/${id}/coach-review`, { method: "POST" });
  setStatus(`Coach review ready. Score: ${payload.review.score}/100`);
  await loadReport(id);
  await loadCoach();
}

async function runGuidedCoach(id) {
  setStatus(`Coach is reading match #${id}...`);
  const payload = await api(`/api/matches/${id}/guided-coach`, { method: "POST" });
  const guided = payload.guided_coach || {};
  setStatus(guided.summary || "Guided coach plan ready.");
  await loadReport(id);
  await loadCoach();
}

async function runMatchAnalysis(id, type) {
  setStatus(`Running ${type} analysis for match #${id}...`);
  const payload = await api(`/api/matches/${id}/${type}`, { method: "POST" });
  setStatus(payload.message || `${type} analysis complete.`);
  await loadReport(id);
}

async function startPipeline(id) {
  const payload = await api(`/api/matches/${id}/pipeline`, { method: "POST" });
  setStatus(`Pipeline queued as job #${payload.job_id}.`);
  await loadAutomation();
}

async function startAutoCoach(id) {
  currentMatchId = id;
  activeCoachJobId = null;
  setStatus(`Auto Coach queued for match #${id}...`);
  const payload = await api(`/api/matches/${id}/auto-coach`, { method: "POST" });
  activeCoachJobId = payload.job_id;
  completedJobIds.delete(Number(payload.job_id));
  setStatus(`Auto Coach running as job #${payload.job_id}.`);
  await Promise.all([loadReport(id), pollJobs()]);
  ensureJobPolling();
}

async function startFullVodCoach(id) {
  currentMatchId = id;
  activeCoachJobId = null;
  setStatus(`Full VOD Coach queued for match #${id}...`);
  const payload = await api(`/api/matches/${id}/full-vod-coach`, { method: "POST" });
  activeCoachJobId = payload.job_id;
  completedJobIds.delete(Number(payload.job_id));
  setStatus(`Full VOD Coach running as job #${payload.job_id}.`);
  await Promise.all([loadReport(id), pollJobs()]);
  ensureJobPolling();
}

async function startDeathBatch(id) {
  const payload = await api(`/api/matches/${id}/batch-deaths`, { method: "POST" });
  setStatus(`Death batch queued as job #${payload.job_id}.`);
  await loadAutomation();
}

function ensureJobPolling() {
  if (jobPollTimer) return;
  jobPollTimer = window.setInterval(() => {
    pollJobs().catch((err) => setStatus(err.message));
  }, 1500);
}

async function pollJobs() {
  const payload = await api("/api/jobs");
  latestJobs = payload.jobs || [];
  renderJobProgressPanel();
  const running = latestJobs.some((job) => ["queued", "running"].includes(job.status));
  const activeJob = activeCoachJobId ? latestJobs.find((job) => Number(job.id) === Number(activeCoachJobId)) : null;
  if (activeJob && ["complete", "failed", "cancelled"].includes(activeJob.status) && !completedJobIds.has(Number(activeJob.id))) {
    completedJobIds.add(Number(activeJob.id));
    setStatus(activeJob.status === "complete" ? "Coach job complete. Review markers and advice are refreshed." : `Coach job ${activeJob.status}: ${activeJob.message || ""}`);
    if (currentMatchId) {
      await Promise.all([loadMatches(), loadReport(currentMatchId), loadTrends(), loadCoach()]);
    }
  }
  if (!running && jobPollTimer) {
    window.clearInterval(jobPollTimer);
    jobPollTimer = null;
  }
}

async function loadPlaybook(id) {
  const payload = await api(`/api/matches/${id}/playbook`, { method: "POST" });
  const playbook = payload.playbook || {};
  setStatus(`Playbook: ${playbook.summary || "generic plan loaded."}`);
  await loadReport(id);
}

async function getAdvice(deathId) {
  setStatus(`Generating advice for death #${deathId}...`);
  const payload = await api(`/api/deaths/${deathId}/advice`, { method: "POST" });
  setStatus(`Advice generated: ${payload.advice.primary_mistake}`);
  await loadReport(currentMatchId);
  await loadCoach();
}

async function analyzeGameplay(deathId) {
  setStatus(`Generating gameplay hypotheses for death #${deathId}...`);
  const payload = await api(`/api/deaths/${deathId}/gameplay`, { method: "POST" });
  setStatus(payload.message);
  if (currentMatchId) await loadReport(currentMatchId);
}

async function understandClip(deathId) {
  setStatus(`Building clip understanding for death #${deathId}...`);
  const payload = await api(`/api/deaths/${deathId}/understand`, { method: "POST" });
  setStatus(payload.message);
  if (currentMatchId) await loadReport(currentMatchId);
}

async function extractKeyframes(deathId) {
  setStatus(`Selecting keyframes for death #${deathId}...`);
  const payload = await api(`/api/deaths/${deathId}/keyframes`, { method: "POST" });
  setStatus(payload.message);
  if (currentMatchId) await loadReport(currentMatchId);
}

async function aiReview(deathId) {
  const payload = await api(`/api/deaths/${deathId}/ai-review`, { method: "POST" });
  setStatus(payload.message);
}

async function localAiReview(deathId) {
  const payload = await api(`/api/deaths/${deathId}/local-ai-review`, { method: "POST" });
  setStatus(payload.message);
  if (currentMatchId) await loadReport(currentMatchId);
}

async function saveClipAnnotation(button) {
  const card = button.closest(".death-card");
  const read = (field) => card.querySelector(`[data-field="${field}"]`)?.value || "";
  const payload = await api(`/api/deaths/${button.dataset.id}/annotations`, {
    method: "POST",
    body: JSON.stringify({
      mistake_start: read("annotation_mistake_start"),
      first_contact: read("annotation_first_contact"),
      death_moment: read("annotation_death_moment"),
      better_decision: read("annotation_better_decision"),
      labels: read("annotation_labels"),
      notes: read("annotation_notes"),
    }),
  });
  setStatus(payload.ok ? "Clip annotation saved." : payload.message);
  if (currentMatchId) await loadReport(currentMatchId);
  await loadAutomation();
}

async function saveCoachProfile() {
  const payload = {
    rank: document.querySelector("#coachRank").value,
    main_agents: document.querySelector("#coachAgents").value,
    target_style: document.querySelector("#coachStyle").value,
    notes: document.querySelector("#coachNotes").value,
  };
  await api("/api/coach/profile", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  setStatus("Coach profile saved.");
  await loadCoach();
}

async function startPlaySession() {
  const focus = document.querySelector("#suggestedFocus")?.dataset.focus || "";
  await api("/api/sessions/start", {
    method: "POST",
    body: JSON.stringify({
      name: document.querySelector("#sessionName").value || "VALORANT Session",
      focus_label: focus,
      notes: document.querySelector("#sessionNotes").value || "",
    }),
  });
  setStatus("Session started.");
  await loadCoach();
}

async function endPlaySession(sessionId) {
  await api("/api/sessions/end", {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId }),
  });
  setStatus("Session ended.");
  await loadCoach();
}

async function startSuggestedGoal() {
  const focus = document.querySelector("#suggestedFocus").dataset.focus;
  const description = document.querySelector("#suggestedSummary").textContent;
  await api("/api/coach/goals", {
    method: "POST",
    body: JSON.stringify({ focus_label: focus, description, target_matches: 2 }),
  });
  setStatus(`Started focus: ${focus}`);
  await loadCoach();
}

async function completeGoal(goalId) {
  await api(`/api/coach/goals/${goalId}/complete`, { method: "POST" });
  setStatus("Session focus completed.");
  await loadCoach();
}

async function saveAdviceFeedback(adviceId, verdict) {
  await api(`/api/advice/${adviceId}/feedback`, {
    method: "POST",
    body: JSON.stringify({ verdict }),
  });
  setStatus(`Advice ${verdict}.`);
  await loadCoach();
  if (currentMatchId) await loadReport(currentMatchId);
}

async function writeReport(id) {
  const payload = await api(`/api/matches/${id}/report/write`, { method: "POST" });
  setStatus(`Report written: ${payload.path}`);
}

async function saveMatchMetadata(button) {
  const card = button.closest(".match-item");
  const id = button.dataset.id;
  await api(`/api/matches/${id}/metadata`, {
    method: "POST",
    body: JSON.stringify({
      map: card.querySelector('[data-field="match_map"]').value,
      agent: card.querySelector('[data-field="match_agent"]').value,
      status: card.querySelector('[data-field="match_status"]').value,
    }),
  });
  setStatus(`Match #${id} metadata saved.`);
  await Promise.all([loadMatches(), currentMatchId == id ? loadReport(id) : Promise.resolve()]);
}

async function saveBenchmarkLabel(payload) {
  const result = await api("/api/evaluation/labels", { method: "POST", body: JSON.stringify(payload) });
  setStatus(`Benchmark label saved: ${result.label.label_type}.`);
  await loadAutomation();
}

async function loadMatches() {
  const payload = await api("/api/matches");
  els.matchesList.innerHTML = "";
  if (payload.matches.length === 0) {
    els.matchesList.innerHTML = '<p class="muted">No matches imported yet.</p>';
    return;
  }
  for (const match of payload.matches) {
    const item = document.createElement("article");
    item.className = "match-item";
    item.innerHTML = `
      <div class="match-title">${escapeHtml(fileName(match.video_path))}</div>
      <div class="match-meta">
        ${escapeHtml(match.map || "unknown map")} · ${escapeHtml(match.agent || "unknown agent")} ·
        ${escapeHtml(match.status)} · ${match.death_count} death(s)
      </div>
      <details class="metadata-editor">
        <summary>Edit metadata</summary>
        <label>Map <input data-field="match_map" type="text" value="${escapeAttr(match.map || "")}" /></label>
        <label>Agent <input data-field="match_agent" type="text" value="${escapeAttr(match.agent || "")}" /></label>
        <label>Status <input data-field="match_status" type="text" value="${escapeAttr(match.status || "")}" /></label>
        <button class="secondary" data-action="save-match-metadata" data-id="${match.id}">Save Metadata</button>
      </details>
      <div class="match-actions">
        <div class="match-primary-actions">
          <button data-action="view" data-id="${match.id}">Review</button>
          <button data-action="auto-coach" data-id="${match.id}">Auto Coach</button>
          <button class="secondary" data-action="full-vod-coach" data-id="${match.id}">Full VOD Coach</button>
          <button class="secondary" data-action="analyze" data-id="${match.id}">Analyze</button>
          <button class="secondary" data-action="suggest" data-id="${match.id}">Find Deaths</button>
          <button class="ghost" data-action="guided-coach" data-id="${match.id}">Coach Me</button>
        </div>
        <details class="advanced-actions">
          <summary>Advanced tools</summary>
          <div class="row">
            <button data-action="pipeline" data-id="${match.id}">Full Pipeline</button>
            <button data-action="batch-deaths" data-id="${match.id}">Batch Deaths</button>
            <button data-action="events-v2" data-id="${match.id}">Death Detector</button>
            <button data-action="rounds" data-id="${match.id}">Rounds</button>
            <button data-action="scoreboard-rounds" data-id="${match.id}">Scoreboard Rounds</button>
            <button data-action="hud" data-id="${match.id}">HUD</button>
            <button data-action="minimap" data-id="${match.id}">Minimap</button>
            <button data-action="crosshair" data-id="${match.id}">Crosshair</button>
            <button data-action="ocr" data-id="${match.id}">OCR</button>
            <button data-action="review-queue" data-id="${match.id}">Queue</button>
            <button data-action="review-queue-v2" data-id="${match.id}">Smart Queue</button>
            <button data-action="story" data-id="${match.id}">Story</button>
            <button data-action="playbook" data-id="${match.id}">Playbook</button>
            <button data-action="clips" data-id="${match.id}">Extract Clips</button>
            <button data-action="review" data-id="${match.id}">Basic Coach Review</button>
            <button data-action="write" data-id="${match.id}">Write Report</button>
          </div>
        </details>
      </div>
    `;
    els.matchesList.appendChild(item);
  }
}

async function loadTrends() {
  const trends = await api("/api/trends");
  const labels = Object.entries(trends.labels)
    .slice(0, 8)
    .map(([label, count]) => `<li><span class="tag">${escapeHtml(label)}</span> ${count}</li>`)
    .join("");
  const maps = Object.entries(trends.by_map)
    .slice(0, 5)
    .map(([map, count]) => `<li>${escapeHtml(map)}: ${count}</li>`)
    .join("");
  const recent = trends.matches
    .slice(0, 5)
    .map((match) => `<li>#${match.match_id} ${escapeHtml(match.map)} / ${escapeHtml(match.agent)}: ${match.death_count} deaths</li>`)
    .join("");

  els.trendsView.innerHTML = `
    <h3>Top Mistakes</h3>
    <ul class="compact-list">${labels || "<li>No labeled trends yet.</li>"}</ul>
    <h3>Death Load By Map</h3>
    <ul class="compact-list">${maps || "<li>No map data yet.</li>"}</ul>
    <h3>Recent Matches</h3>
    <ul class="compact-list">${recent || "<li>No matches yet.</li>"}</ul>
  `;
}

async function loadCoach() {
  const coach = await api("/api/coach/v2");
  renderCoach(coach);
}

function renderCoach(coach) {
  const profile = coach.profile || {};
  const plan = coach.plan || {};
  const goal = coach.active_goal;
  const sessions = coach.sessions || {};
  const learning = coach.suggestion_learning || {};
  const memory = coach.memory || {};
  const outcomes = coach.outcomes || {};
  const coachV2 = coach.coach_v2 || {};
  const weekly = coachV2.weekly_focus || {};
  const progress = plan.progress || {};
  const agents = (profile.main_agents || []).join(", ");
  const goalBlock = goal
    ? `
      <div class="coach-goal">
        <strong>Active focus</strong>
        <p>${escapeHtml(goal.focus_label)}</p>
        <p class="muted">${escapeHtml(goal.description || "")}</p>
        <button class="secondary" data-action="complete-goal" data-id="${goal.id}">Complete Focus</button>
      </div>
    `
    : `
      <button class="secondary" data-action="start-goal">Start Suggested Focus</button>
    `;

  els.coachView.innerHTML = `
    <div class="coach-plan">
      <h3>Session</h3>
      ${renderSessionBlock(sessions)}
    </div>
    <div class="coach-profile">
      <label>Rank <input id="coachRank" type="text" value="${escapeAttr(profile.rank || "")}" placeholder="Gold 2" /></label>
      <label>Main agents <input id="coachAgents" type="text" value="${escapeAttr(agents)}" placeholder="Jett, Omen" /></label>
      <label>Target style <input id="coachStyle" type="text" value="${escapeAttr(profile.target_style || "")}" placeholder="More disciplined entry fights" /></label>
      <label>Coach notes <input id="coachNotes" type="text" value="${escapeAttr(profile.notes || "")}" placeholder="What should the coach remember?" /></label>
      <button data-action="save-profile">Save Profile</button>
    </div>
    <div class="coach-plan">
      <h3 id="suggestedFocus" data-focus="${escapeAttr(plan.focus_label || "")}">${escapeHtml(plan.summary || "No plan yet")}</h3>
      <p id="suggestedSummary">${escapeHtml(plan.why || "")}</p>
      <p class="muted">${escapeHtml(plan.profile_context || "")}</p>
      <ul class="compact-list">
        <li><strong>In game:</strong> ${escapeHtml(plan.in_game_rule || "")}</li>
        <li><strong>Review:</strong> ${escapeHtml(plan.review_rule || "")}</li>
        <li><strong>Drill:</strong> ${escapeHtml(plan.drill || "")}</li>
        <li><strong>Target:</strong> ${escapeHtml(plan.target || "")}</li>
      </ul>
      <div class="coach-progress">
        <span>${progress.recent_focus_deaths || 0} focus deaths</span>
        <span>${progress.recent_matches || 0} recent matches</span>
        <span>${progress.accepted_advice || 0} accepted</span>
        <span>${progress.rejected_advice || 0} rejected</span>
        <span>${learning.accepted || 0} suggestions accepted</span>
        <span>${learning.rejected || 0} suggestions rejected</span>
      </div>
      ${goalBlock}
    </div>
    <div class="coach-plan">
      <h3>Personal Coach v2</h3>
      <p>${escapeHtml(weekly.target || "No weekly target yet.")}</p>
      <div class="bar-chart">${renderSkillBars(coachV2.skill_scores || {})}</div>
      <ul class="compact-list">
        ${(weekly.drills || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}
      </ul>
      <p class="muted">Memory strength ${escapeHtml(coachV2.memory_strength || 0)} · primary focus ${escapeHtml(weekly.primary_focus || "none")}</p>
    </div>
    <div class="coach-plan">
      <h3>Weighted Patterns</h3>
      <ul class="compact-list">
        ${(coachV2.weighted_profile || []).slice(0, 6).map((item) => `<li>${escapeHtml(item.label)}: ${escapeHtml(item.weight)}</li>`).join("") || "<li>No weighted patterns yet.</li>"}
      </ul>
    </div>
    <div class="coach-plan">
      <h3>Personal Coach Memory</h3>
      <p>${escapeHtml(memory.summary || "No learned memory yet.")}</p>
      <ul class="compact-list">
        ${(memory.priorities || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}
      </ul>
      <p class="muted">${Number(memory.analysis_count || 0)} saved analysis read(s), ${Number(memory.recent_clip_reads || 0)} recent clip read(s).</p>
    </div>
    <div class="coach-plan">
      <h3>Measured Outcomes</h3>
      <p>${escapeHtml(outcomes.summary || "No measured outcomes yet.")}</p>
      <div class="coach-progress">
        <span>Focus: ${escapeHtml(outcomes.focus_label || "none")}</span>
        <span>Crosshair avg: ${escapeHtml(outcomes.crosshair_average ?? "n/a")}</span>
        <span>Detector accepted: ${escapeHtml((outcomes.detector_feedback || {}).accepted || 0)}</span>
        <span>Detector rejected: ${escapeHtml((outcomes.detector_feedback || {}).rejected || 0)}</span>
      </div>
    </div>
  `;
}

function renderSkillBars(scores) {
  const entries = Object.entries(scores);
  if (!entries.length) return '<p class="muted">No skill ratings yet.</p>';
  return entries.map(([skill, score]) => `
    <div class="bar-row">
      <span>${escapeHtml(skill.replaceAll("_", " "))}</span>
      <i style="width:${Math.max(4, Number(score || 0))}%"></i>
      <b>${escapeHtml(score)}</b>
    </div>
  `).join("");
}

function renderSessionBlock(sessions) {
  const active = sessions.active;
  if (active) {
    return `
      <p><strong>${escapeHtml(active.name)}</strong></p>
      <p class="muted">Focus: ${escapeHtml(active.focus_label || "none")}</p>
      <button class="secondary" data-action="end-session" data-id="${active.id}">End Session</button>
    `;
  }
  return `
    <label>Name <input id="sessionName" type="text" value="VALORANT Session" /></label>
    <label>Notes <input id="sessionNotes" type="text" placeholder="Goal for this play block" /></label>
    <button class="secondary" data-action="start-session">Start Session</button>
  `;
}

async function loadReport(id) {
  currentMatchId = id;
  const report = await api(`/api/matches/${id}/report`);
  renderReport(report);
}

function renderReport(report) {
  const match = report.match;
  const deaths = report.deaths.map(renderDeathCard).join("");
  const review = renderMatchReview(report.review);
  const guidedCoach = renderGuidedCoach(report.guided_coach, match.id);
  const suggestions = renderSuggestions(report.suggestions || []);
  const coachMoments = mergeCoachMomentFeedback(
    (((report.match_analyses || {}).full_vod_coach || {}).payload || {}).moments || [],
    report.coach_moment_feedback || []
  );
  const timeline = renderVideoTimeline(report.deaths || [], report.suggestions || [], coachMoments);
  const coachMomentsView = renderCoachMoments(coachMoments);
  const matchAnalyses = renderMatchAnalyses(report.match_analyses || {});
  const jobPanel = renderJobProgress(report.match.id);
  const priorities = renderCoachPriorities(report, coachMoments);
  const manualMarkerForm = renderManualMarkerForm(match.id);

  els.reportView.innerHTML = `
    <div id="jobProgressMount">${jobPanel}</div>
    <section class="player-wrap">
      <div class="review-head">
        <h3>${escapeHtml(match.map || "Unknown map")} / ${escapeHtml(match.agent || "Unknown agent")}</h3>
        <span class="tag">${report.deaths.length} marked death(s)</span>
      </div>
      <div class="calibration-stage">
        <video id="vodPlayer" controls preload="metadata" src="/api/matches/${match.id}/video"></video>
        <div id="calibrationOverlay" class="calibration-overlay hidden"></div>
      </div>
      ${timeline}
    </section>
    ${priorities}
    ${guidedCoach}
    ${suggestions}
    ${coachMomentsView}
    <section>
      <div class="review-head">
        <h3>Death Review</h3>
        <span class="muted">Open the first 3, not every card.</span>
      </div>
      <div>${deaths || '<p class="muted">No deaths marked yet.</p>'}</div>
    </section>
    <details class="advanced-actions">
      <summary>Manual tools and advanced analysis</summary>
      ${manualMarkerForm}
      ${review}
      <section>
        <h3>Detector Benchmarking</h3>
        <div class="add-death">
          <label>Missed death sec <input id="benchmarkMissedTs" type="number" min="0" step="0.1" placeholder="120.5" /></label>
          <label class="wide">Note <input id="benchmarkNote" type="text" placeholder="Why the detector missed this death" /></label>
          <button class="secondary" data-action="benchmark-missed" data-id="${match.id}">Mark Missed Death</button>
        </div>
      </section>
      <section>
        <h3>Calibration Overlay</h3>
        <div class="overlay-tools">
          <label>Region
            <select id="overlayRegion">${CALIBRATION_REGIONS.map(([key, label]) => `<option value="${escapeAttr(key)}" ${key === selectedCalibrationRegion ? "selected" : ""}>${escapeHtml(label)}</option>`).join("")}</select>
          </label>
          <button class="secondary" data-action="toggle-calibration-overlay">Show / Hide Overlay</button>
          <button class="secondary" data-action="save-overlay-calibration">Save Overlay</button>
        </div>
      </section>
      ${matchAnalyses}
    </details>
  `;
  attachVideoTimelineSync();
}

function renderManualMarkerForm(matchId) {
  return `
    <section>
      <h3>Add Death Marker</h3>
      <div class="add-death">
        <label>Round <input id="newRound" type="number" min="1" placeholder="1" /></label>
        <label>Time sec <input id="newTimestamp" type="number" min="0" step="0.1" placeholder="82.4" /></label>
        <label>Labels <input id="newLabels" type="text" placeholder="dry peek, exposed to multiple angles" /></label>
        <div class="preset-row wide" data-preset-target="newLabels">${renderPresetButtons()}</div>
        <label class="wide">Notes <input id="newNotes" type="text" placeholder="What happened before the death?" /></label>
        <button data-action="add-death" data-id="${matchId}">Add Death</button>
      </div>
    </section>
  `;
}

function renderCoachPriorities(report, coachMoments) {
  const labelItems = Object.entries(report.label_counts || {})
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, 3);
  const deathWithAdvice = (report.deaths || []).find((death) => death.advice);
  const firstMoment = (coachMoments || [])[0];
  const focusItems = (report.focus || []).slice(0, 3);
  const cards = [];

  if (labelItems.length) {
    const [label, count] = labelItems[0];
    cards.push(`
      <article class="priority-card">
        <span>Pattern</span>
        <strong>${escapeHtml(label)}</strong>
        <p>Appears in ${escapeHtml(count)} marked death(s). Review only the first few clips and look for the decision before contact.</p>
      </article>
    `);
  }
  if (deathWithAdvice) {
    cards.push(`
      <article class="priority-card">
        <span>Best Starting Clip</span>
        <strong>${escapeHtml(formatDeathTime(deathWithAdvice))}</strong>
        <p>${escapeHtml(deathWithAdvice.advice.better_play || deathWithAdvice.advice.what_happened || "Open this marker first.")}</p>
        <button class="secondary" data-action="jump" data-ts="${escapeAttr(deathWithAdvice.timestamp || 0)}">Review Clip</button>
      </article>
    `);
  } else if ((report.deaths || []).length) {
    const firstDeath = report.deaths[0];
    cards.push(`
      <article class="priority-card">
        <span>Start Here</span>
        <strong>${escapeHtml(formatDeathTime(firstDeath))}</strong>
        <p>Generate advice for this marker, then accept or reject it so the coach learns what is useful.</p>
        <button class="secondary" data-action="advice" data-id="${firstDeath.id}">Generate Advice</button>
      </article>
    `);
  }
  if (firstMoment) {
    cards.push(`
      <article class="priority-card">
        <span>Non-Death Habit</span>
        <strong>${escapeHtml(firstMoment.title || firstMoment.label || "Coach moment")}</strong>
        <p>${escapeHtml((firstMoment.ai_review || {}).better_play || firstMoment.better_play || firstMoment.reason || "Review this whole-VOD moment.")}</p>
        <button class="secondary" data-action="jump" data-ts="${escapeAttr(firstMoment.timestamp || 0)}">Jump</button>
      </article>
    `);
  }
  if (!cards.length) {
    cards.push(`
      <article class="priority-card">
        <span>Next Step</span>
        <strong>Build clean review data</strong>
        <p>Run Auto Coach or Find Deaths, accept real deaths, reject noise, then generate advice on the first confirmed marker.</p>
      </article>
    `);
  }
  const focus = focusItems.length
    ? `<ul class="compact-list">${focusItems.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
    : "";
  return `
    <section class="coach-priorities">
      <div class="review-head">
        <h3>Coach Priorities</h3>
        <span class="muted">The next things to review.</span>
      </div>
      <div class="priority-grid">${cards.join("")}</div>
      ${focus ? `<details><summary>More focus notes</summary>${focus}</details>` : ""}
    </section>
  `;
}

function renderJobProgress(matchId) {
  const job = findVisibleCoachJob(matchId);
  if (!job) return "";
  const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
  const result = job.result || {};
  const summary = (result.summary || {}).summary || result.message || "";
  const details = (result.summary || {}).next_action || "";
  const statusClass = ["complete", "failed", "cancelled"].includes(job.status) ? job.status : "running";
  return `
    <section class="job-progress ${statusClass}">
      <div class="job-progress-head">
        <div>
          <h3>Auto Coach Progress</h3>
          <p>${escapeHtml(job.name)} · ${escapeHtml(job.status)}</p>
        </div>
        <strong>${progress}%</strong>
      </div>
      <div class="progress-track" aria-label="Auto Coach progress">
        <span class="progress-fill" style="width:${progress}%"></span>
      </div>
      <p class="job-message">${escapeHtml(job.message || "Queued.")}</p>
      ${summary ? `<p class="muted">${escapeHtml(summary)}</p>` : ""}
      ${details ? `<p class="muted">${escapeHtml(details)}</p>` : ""}
      ${["queued", "running"].includes(job.status) ? `<button class="danger" data-action="cancel-job" data-id="${job.id}">Cancel</button>` : ""}
    </section>
  `;
}

function renderJobProgressPanel() {
  const mount = document.querySelector("#jobProgressMount");
  if (!mount || !currentMatchId) return;
  mount.innerHTML = renderJobProgress(currentMatchId);
}

function findVisibleCoachJob(matchId) {
  const targetNames = [`Auto coach match #${matchId}`, `Full VOD coach match #${matchId}`];
  if (activeCoachJobId) {
    const active = latestJobs.find((job) => Number(job.id) === Number(activeCoachJobId));
    if (active) return active;
  }
  return latestJobs.find((job) => targetNames.some((name) => String(job.name || "").toLowerCase() === name.toLowerCase())) || null;
}

function renderMatchAnalyses(analyses) {
  const entries = Object.entries(analyses).filter(([type, row]) => type !== "guided_coach" && row && row.payload);
  if (!entries.length) {
    return `
      <section>
        <h3>Visual And OCR Analysis</h3>
        <p class="muted">No match-level HUD, minimap, or OCR analysis has been saved yet.</p>
      </section>
    `;
  }
  const cards = entries.map(([type, row]) => {
    const payload = row.payload || {};
    const reads = (payload.reads || payload.candidates || payload.rounds || payload.items || payload.moments || payload.timeline_events || [])
      .slice(0, 6)
      .map((item) => `<li>${escapeHtml(formatAnalysisItem(item))}</li>`)
      .join("");
    const interpretation = (payload.interpretation || [])
      .slice(0, 4)
      .map((item) => `<li>${escapeHtml(item)}</li>`)
      .join("");
    const observations = (payload.observations || [])
      .slice(0, 4)
      .map((item) => `<li>${escapeHtml(JSON.stringify(item))}</li>`)
      .join("");
    return `
      <article class="analysis-card">
        <div class="analysis-head">
          <strong>${escapeHtml(type.toUpperCase())}</strong>
          <span>${Math.round(Number(payload.confidence || 0) * 100)}%</span>
        </div>
        <p>${escapeHtml(payload.summary || "Analysis captured.")}</p>
        ${interpretation ? `<ul class="compact-list">${interpretation}</ul>` : ""}
        ${reads ? `<ul class="compact-list">${reads}</ul>` : ""}
        ${!reads && observations ? `<ul class="compact-list">${observations}</ul>` : ""}
      </article>
    `;
  }).join("");
  return `
    <section>
      <h3>Visual And OCR Analysis</h3>
      <div class="analysis-list">${cards}</div>
    </section>
  `;
}

function formatAnalysisItem(item) {
  if (item.text) return `${item.region}: ${item.text}`;
  if (item.title) return `${item.title}: ${item.reason || ""}`;
  if (item.kind && item.frame !== undefined) return `${item.kind} frame ${item.frame}: ${item.text || ""}`;
  if (item.timestamp !== undefined) return `@ ${formatTs(item.timestamp)} ${item.reason || ""}`.trim();
  if (item.round_number !== undefined) return `R${item.round_number}: ${formatTs(item.start_ts)}-${formatTs(item.end_ts)}`;
  return JSON.stringify(item);
}

function renderVideoTimeline(deaths, suggestions, coachMoments = []) {
  const points = [
    ...deaths
      .filter((item) => item.timestamp !== null && item.timestamp !== undefined)
      .map((item) => ({
        kind: "death",
        timestamp: Number(item.timestamp),
        label: `Marked death ${formatDeathTime(item)}`,
      })),
    ...suggestions
      .filter((item) => item.timestamp !== null && item.timestamp !== undefined)
      .map((item) => ({
        kind: "suggestion",
        timestamp: Number(item.timestamp),
        label: `Suggested death @ ${formatTs(item.timestamp)} (${Math.round(Number(item.confidence || 0) * 100)}%)`,
      })),
    ...coachMoments
      .filter((item) => item.timestamp !== null && item.timestamp !== undefined)
      .map((item) => ({
        kind: "coach",
        timestamp: Number(item.timestamp),
        label: `${item.title || "Coach moment"} @ ${formatTs(item.timestamp)} (${Math.round(Number(item.confidence || 0) * 100)}%)`,
      })),
  ].filter((item) => Number.isFinite(item.timestamp));
  if (!points.length) {
    return '<p class="muted">No video markers yet. Run Auto Coach, Full VOD Coach, Find Deaths, or add a death marker.</p>';
  }
  const maxTs = Math.max(60, ...points.map((item) => item.timestamp));
  const markers = points
    .sort((a, b) => a.timestamp - b.timestamp)
    .map((item) => {
      const left = Math.max(0, Math.min(100, (item.timestamp / maxTs) * 100));
      return `
        <button
          class="timeline-marker ${item.kind}"
          style="left:${left}%"
          title="${escapeAttr(item.label)}"
          data-action="jump"
          data-ts="${escapeAttr(item.timestamp)}"
          aria-label="${escapeAttr(item.label)}">
        </button>
      `;
    }).join("");
  return `
    <div class="video-timeline">
      <div class="timeline-track">${markers}</div>
      <div class="timeline-legend">
        <span><i class="death"></i>Marked death</span>
        <span><i class="suggestion"></i>Suggested death</span>
        <span><i class="coach"></i>Coach moment</span>
        <span>${points.length} marker(s)</span>
      </div>
    </div>
  `;
}

function renderCoachMoments(moments) {
  if (!moments.length) {
    return `
      <section class="coach-moments-section empty">
        <h3>Coach Moments</h3>
        <p class="muted">Run Full VOD Coach to find mechanics and decision moments outside obvious deaths.</p>
      </section>
    `;
  }
  const cards = moments.slice(0, 10).map((item, index) => {
    const ai = item.ai_review || {};
    const labels = renderTags([item.personal_label || item.label].concat(item.secondary_labels || []).filter(Boolean));
    const feedback = item.feedback || {};
    const feedbackText = feedback.verdict ? `<span class="tag">${escapeHtml(feedback.verdict)}</span>` : "";
    return `
      <article class="coach-moment-card" data-moment-id="${escapeAttr(item.moment_id || "")}">
        <div class="coach-moment-head">
          <button class="ghost" data-action="jump" data-ts="${escapeAttr(item.timestamp || 0)}">${formatTs(item.timestamp)}</button>
          <div>
            <strong>${index + 1}. ${escapeHtml(item.title || "Coach moment")}</strong>
            <p class="muted">Priority ${escapeHtml(item.priority || 0)}</p>
          </div>
        </div>
        <p>${escapeHtml(ai.summary || item.reason || "Review this timestamp for a possible habit leak.")}</p>
        <p class="coach-action"><strong>Do this:</strong> ${escapeHtml(ai.better_play || item.better_play || "Replay the moment and identify the safer decision before contact.")}</p>
        ${ai.drill ? `<p><strong>Drill:</strong> ${escapeHtml(ai.drill)}</p>` : ""}
        <div>${labels}</div>
        <div class="coach-moment-feedback">
          ${feedbackText}
          <input data-field="coach_moment_note" type="text" value="${escapeAttr(feedback.note || "")}" placeholder="Optional note for this tip" />
          <button class="secondary" data-action="coach-moment-feedback" data-verdict="accepted" data-match="${currentMatchId}" data-moment-id="${escapeAttr(item.moment_id || "")}" data-ts="${escapeAttr(item.timestamp || 0)}" data-label="${escapeAttr(item.label || "")}" data-title="${escapeAttr(item.title || "")}">Useful</button>
          <button class="danger" data-action="coach-moment-feedback" data-verdict="rejected" data-match="${currentMatchId}" data-moment-id="${escapeAttr(item.moment_id || "")}" data-ts="${escapeAttr(item.timestamp || 0)}" data-label="${escapeAttr(item.label || "")}" data-title="${escapeAttr(item.title || "")}">Not Useful</button>
        </div>
      </article>
    `;
  }).join("");
  return `
    <section class="coach-moments-section">
      <div class="review-head">
        <h3>Whole-VOD Coach Moments</h3>
        <span class="muted">Top ${Math.min(10, moments.length)} only</span>
      </div>
      <div class="coach-moment-list">${cards}</div>
    </section>
  `;
}

function mergeCoachMomentFeedback(moments, feedbackRows) {
  const feedbackById = {};
  for (const row of feedbackRows || []) {
    const payload = row.payload || {};
    if (payload.moment_id) feedbackById[payload.moment_id] = payload;
  }
  return (moments || []).map((moment) => ({
    ...moment,
    feedback: feedbackById[moment.moment_id] || moment.feedback,
  }));
}

async function saveCoachMomentFeedback(button) {
  const card = button.closest(".coach-moment-card");
  const note = card?.querySelector('[data-field="coach_moment_note"]')?.value || "";
  const matchId = button.dataset.match || currentMatchId;
  await api(`/api/matches/${matchId}/coach-moment-feedback`, {
    method: "POST",
    body: JSON.stringify({
      moment_id: button.dataset.momentId,
      timestamp: button.dataset.ts,
      label: button.dataset.label,
      title: button.dataset.title,
      verdict: button.dataset.verdict,
      note,
    }),
  });
  setStatus(`Coach moment marked ${button.dataset.verdict}.`);
  await Promise.all([loadReport(matchId), loadCoach()]);
}

function attachVideoTimelineSync() {
  const player = document.querySelector("#vodPlayer");
  if (!player) return;
  const sync = () => {
    const duration = Number(player.duration || 0);
    if (!Number.isFinite(duration) || duration <= 0) return;
    for (const marker of document.querySelectorAll(".timeline-marker[data-ts]")) {
      const ts = Number(marker.dataset.ts || 0);
      const left = Math.max(0, Math.min(100, (ts / duration) * 100));
      marker.style.left = `${left}%`;
    }
  };
  player.addEventListener("loadedmetadata", sync, { once: true });
  sync();
}

function renderSuggestions(suggestions) {
  if (!suggestions.length) {
    return `
      <section class="suggestion-section empty">
        <h3>Suggested Deaths</h3>
        <p class="muted">No pending death candidates. Confirmed markers are shown in Death Review.</p>
      </section>
    `;
  }
  const cards = suggestions.map((item) => {
    const frameId = fileName(item.frame_path || "").replace(/\.jpg$/i, "");
    const frame = item.frame_path ? `<img class="suggestion-frame" src="/api/vision/frame/${frameId}" alt="suggested death frame" />` : "";
    return `
      <article class="suggestion-card" data-suggestion-id="${item.id}">
        ${frame}
        <div>
          <strong>Possible death @ ${formatTs(item.timestamp)}</strong>
          <span class="tag">${Math.round(Number(item.confidence || 0) * 100)}%</span>
          <p>${escapeHtml(shortenText(item.reason, 150))}</p>
          <details class="advanced-actions">
            <summary>Edit labels before accepting</summary>
            <label>Labels <input data-field="suggestion_labels" type="text" value="needs manual review" /></label>
            <div class="preset-row" data-preset-field="suggestion_labels">${renderPresetButtons()}</div>
            <label>Notes <input data-field="suggestion_notes" type="text" value="${escapeAttr(item.reason)}" /></label>
          </details>
          <div class="suggestion-actions">
            <button data-action="accept-suggestion" data-id="${item.id}" data-ts="${item.timestamp}">Accept</button>
            <button class="danger" data-action="reject-suggestion" data-id="${item.id}">Reject</button>
            <button class="secondary" data-action="jump" data-ts="${item.timestamp}">Jump</button>
            <button class="ghost" data-action="benchmark-false-positive" data-id="${item.id}" data-match="${currentMatchId}" data-ts="${item.timestamp}">False Positive</button>
          </div>
        </div>
      </article>
    `;
  }).join("");
  return `
    <section class="suggestion-section">
      <div class="review-head">
        <h3>Pending Death Candidates</h3>
        <span class="muted">${suggestions.length} to verify</span>
      </div>
      <div class="suggestion-list">${cards}</div>
    </section>
  `;
}

function renderGuidedCoach(row, matchId) {
  const coach = (row && row.payload) || row || null;
  if (!coach) {
    return `
      <section class="guided-coach empty">
        <div>
          <h3>Coach Mode</h3>
          <p class="muted">Generate a short review order after markers are confirmed.</p>
        </div>
        <button data-action="guided-coach" data-id="${matchId}">Coach This Match</button>
      </section>
    `;
  }
  const items = (coach.review_order || []).map((item) => `
    <li>
      <button class="ghost" data-action="jump" data-ts="${escapeAttr(item.timestamp || 0)}">${item.timestamp !== undefined ? formatTs(item.timestamp) : "Open"}</button>
      <div>
        <strong>${escapeHtml(item.title || `Step ${item.rank}`)}</strong>
        <p>${escapeHtml(item.reason || "")}</p>
        ${item.pause_question ? `<p><strong>Check:</strong> ${escapeHtml(item.pause_question)}</p>` : ""}
        ${item.coach_action ? `<p><strong>Do:</strong> ${escapeHtml(item.coach_action)}</p>` : ""}
      </div>
    </li>
  `).join("");
  const homework = (coach.homework || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  return `
    <section class="guided-coach">
      <div class="review-head">
        <h3>Coach Mode</h3>
        <strong>${Math.round(Number(coach.confidence || 0) * 100)}%</strong>
      </div>
      <p>${escapeHtml(coach.summary || "")}</p>
      ${coach.coach_read ? `<p><strong>Read:</strong> ${escapeHtml(coach.coach_read)}</p>` : ""}
      ${coach.between_round_rule ? `<p><strong>Round rule:</strong> ${escapeHtml(coach.between_round_rule)}</p>` : ""}
      <ol class="coach-steps">${items}</ol>
      <details class="advanced-actions">
        <summary>Practice plan</summary>
        <ul class="compact-list">${homework}</ul>
      </details>
      <button class="secondary" data-action="guided-coach" data-id="${matchId}">Refresh Coach Read</button>
    </section>
  `;
}

function renderMatchReview(review) {
  if (!review) {
    return `
      <section>
        <h3>Coach Review</h3>
        <p class="muted">No match-level review yet. Use Coach Review from the match card after labeling deaths.</p>
      </section>
    `;
  }
  const labels = Object.entries(review.label_counts || {})
    .map(([label, count]) => `<span class="tag">${escapeHtml(label)} ${count}</span>`)
    .join(" ");
  return `
    <section class="match-review">
      <div class="review-head">
        <h3>Coach Review</h3>
        <strong>${Number(review.score || 0)}/100</strong>
      </div>
      <p>${escapeHtml(review.summary)}</p>
      <p><strong>Next action:</strong> ${escapeHtml(review.next_action)}</p>
      <p><strong>Drill:</strong> ${escapeHtml(review.drill)}</p>
      <p><strong>Coach note:</strong> ${escapeHtml(review.coach_note)}</p>
      <div>${labels}</div>
    </section>
  `;
}

function renderDeathCard(death) {
  const labels = (death.mistake_labels || []).join(", ");
  const clip = death.clip_path
    ? `<a href="/api/deaths/${death.id}/clip" target="_blank">Open clip</a>`
    : `<span class="muted">No clip yet</span>`;
  const advice = renderAdvice(death.advice);
  const vision = renderVision(death.vision);
  const understanding = renderUnderstanding(death.understanding);
  const keyframes = renderKeyframes(death.keyframes);
  const localAi = renderLocalAiReview(death.local_ai_review);
  const annotations = renderAnnotations(death.annotations || []);
  return `
    <article class="death-card" data-death-id="${death.id}">
      <div class="death-card-header">
        <div class="death-card-title">
          <strong>${escapeHtml(formatDeathTime(death))}</strong>
          ${renderTags(death.mistake_labels || [])}
        </div>
        <button class="secondary" data-action="jump" data-ts="${death.timestamp || 0}">Jump</button>
        <button data-action="advice" data-id="${death.id}">${death.advice ? "Refresh Advice" : "Get Advice"}</button>
      </div>
      ${death.notes ? `<p class="death-note">${escapeHtml(shortenText(death.notes, 180))}</p>` : ""}
      ${advice}
      <details class="advanced-actions">
        <summary>Clip, AI, and edit tools</summary>
        <div class="row">
          <button class="secondary" data-action="local-ai-review" data-id="${death.id}">Local AI</button>
          <button class="secondary" data-action="vision" data-id="${death.id}">Analyze Clip</button>
          <button class="secondary" data-action="keyframes" data-id="${death.id}">Keyframes</button>
          <button class="secondary" data-action="understand" data-id="${death.id}">Understand</button>
          <button class="secondary" data-action="gameplay" data-id="${death.id}">Gameplay</button>
          <button class="secondary" data-action="ai-review" data-id="${death.id}">AI Review</button>
          <button class="secondary" data-action="benchmark-true-positive" data-id="${death.id}" data-match="${death.match_id}" data-ts="${death.timestamp || 0}">True Positive</button>
          ${clip}
        </div>
        ${vision}
        ${keyframes}
        ${understanding}
        ${localAi}
        ${annotations}
        <div class="death-editor">
          <label>Round <input data-field="round_number" type="number" min="1" value="${death.round_number || ""}" /></label>
          <label>Time sec <input data-field="timestamp" type="number" min="0" step="0.1" value="${death.timestamp ?? ""}" /></label>
          <label>Labels <input data-field="mistake_labels" type="text" value="${escapeAttr(labels)}" /></label>
          <div class="preset-row wide" data-preset-field="mistake_labels">${renderPresetButtons()}</div>
          <label>Confidence <input data-field="confidence" type="number" min="0" max="1" step="0.01" value="${death.confidence || 0}" /></label>
          <label class="wide">Notes <input data-field="notes" type="text" value="${escapeAttr(death.notes || "")}" /></label>
          <label>Phase <input data-field="round_phase_correction" type="text" value="${escapeAttr(death.round_phase || "")}" /></label>
          <label class="wide">Correction <input data-field="correction_note" type="text" placeholder="Correct OCR, phase, event type, or keyframe read" /></label>
          <label>Mistake start <input data-field="annotation_mistake_start" type="number" min="0" step="0.1" placeholder="clip sec" /></label>
          <label>First contact <input data-field="annotation_first_contact" type="number" min="0" step="0.1" placeholder="clip sec" /></label>
          <label>Death moment <input data-field="annotation_death_moment" type="number" min="0" step="0.1" placeholder="clip sec" /></label>
          <div class="wide timeline-actions" data-death-ts="${escapeAttr(death.timestamp || 0)}">
            <button type="button" class="secondary" data-action="set-annotation-time" data-field="annotation_mistake_start">Set Mistake</button>
            <button type="button" class="secondary" data-action="set-annotation-time" data-field="annotation_first_contact">Set Contact</button>
            <button type="button" class="secondary" data-action="set-annotation-time" data-field="annotation_death_moment">Set Death</button>
            <button type="button" class="secondary" data-action="loop-death" data-ts="${death.timestamp || 0}">Loop 12s</button>
          </div>
          <label class="wide">Better decision <input data-field="annotation_better_decision" type="text" placeholder="What should you have done instead?" /></label>
          <label>Annotation labels <input data-field="annotation_labels" type="text" placeholder="timing, utility, spacing" /></label>
          <label class="wide">Annotation notes <input data-field="annotation_notes" type="text" placeholder="What should the personal coach learn?" /></label>
          <button data-action="save-death" data-id="${death.id}">Save</button>
          <button class="secondary" data-action="save-correction" data-id="${death.id}">Save Correction</button>
          <button class="secondary" data-action="save-annotation" data-id="${death.id}">Save Annotation</button>
          <button class="danger" data-action="delete-death" data-id="${death.id}">Delete</button>
        </div>
      </details>
    </article>
  `;
}

function renderKeyframes(row) {
  if (!row || !row.payload || !(row.payload.frames || []).length) {
    return "";
  }
  const frames = row.payload.frames.map((item) => `
    <figure class="keyframe">
      <img src="/api/vision/frame/${escapeAttr(item.frame_id)}" alt="${escapeAttr(item.role)} keyframe" />
      <figcaption>
        <strong>${escapeHtml(item.role)}</strong>
        <span>${formatTs(item.timestamp)} · ${escapeHtml(item.reason || "")}</span>
      </figcaption>
    </figure>
  `).join("");
  return `<div class="keyframe-gallery">${frames}</div>`;
}

function renderUnderstanding(row) {
  if (!row || !row.payload) {
    return "";
  }
  const payload = row.payload;
  const labels = (payload.suggested_labels || []).map((label) => `<span class="tag">${escapeHtml(label)}</span>`).join(" ");
  return `
    <div class="analysis-card">
      <div class="analysis-head">
        <strong>Clip Understanding</strong>
        <span>${Math.round(Number(payload.confidence || 0) * 100)}%</span>
      </div>
      <p>${escapeHtml(payload.summary || "")}</p>
      <p><strong>Minimap:</strong> ${escapeHtml(payload.minimap_read || "")}</p>
      <p><strong>Crosshair:</strong> ${escapeHtml(payload.crosshair_read || "")}</p>
      <div>${labels}</div>
    </div>
  `;
}

function renderLocalAiReview(row) {
  if (!row || !row.payload) {
    return "";
  }
  const payload = row.payload;
  const labels = (payload.labels || []).map((label) => `<span class="tag">${escapeHtml(label)}</span>`).join(" ");
  return `
    <div class="analysis-card">
      <div class="analysis-head">
        <strong>Local AI Review</strong>
        <span>${escapeHtml(payload.status || "captured")} · ${Math.round(Number(payload.confidence || 0) * 100)}%</span>
      </div>
      <p>${escapeHtml(payload.summary || "")}</p>
      ${payload.better_play ? `<p><strong>Better play:</strong> ${escapeHtml(payload.better_play)}</p>` : ""}
      <div>${labels}</div>
    </div>
  `;
}

function renderAnnotations(rows) {
  if (!rows.length) {
    return "";
  }
  const items = rows.slice(0, 4).map((row) => {
    const payload = row.payload || {};
    return `
      <li>
        <strong>${escapeHtml(payload.better_decision || "Clip annotation")}</strong>
        <span class="muted">mistake ${escapeHtml(payload.mistake_start ?? "n/a")} · contact ${escapeHtml(payload.first_contact ?? "n/a")} · death ${escapeHtml(payload.death_moment ?? "n/a")}</span>
        <p class="muted">${escapeHtml(payload.notes || "")}</p>
      </li>
    `;
  }).join("");
  return `
    <div class="analysis-card">
      <div class="analysis-head"><strong>Annotations</strong><span>${rows.length}</span></div>
      <ul class="compact-list">${items}</ul>
    </div>
  `;
}

function renderVision(vision) {
  if (!vision) {
    return '<div class="vision-empty">No visual clip analysis yet.</div>';
  }
  const observations = (vision.observations || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const labels = (vision.suggested_labels || []).map((label) => `<span class="tag">${escapeHtml(label)}</span>`).join(" ");
  const metrics = vision.metrics || {};
  return `
    <div class="vision-box">
      <div class="vision-head">
        <strong>Visual Read</strong>
        <span>${Math.round(Number(vision.confidence || 0) * 100)}%</span>
      </div>
      <p>${escapeHtml(vision.summary || "")}</p>
      <ul class="compact-list">${observations}</ul>
      <div>${labels}</div>
      <p class="muted">Peak death UI: ${escapeHtml(metrics.peak_death_ui_score ?? "n/a")} · peak motion: ${escapeHtml(metrics.peak_motion ?? "n/a")} · crosshair activity: ${escapeHtml(metrics.average_crosshair_activity ?? "n/a")}</p>
    </div>
  `;
}

function renderPresetButtons() {
  return LABEL_PRESETS.map(
    (label) => `<button type="button" class="preset-button" data-action="preset-label" data-label="${escapeAttr(label)}">${escapeHtml(label)}</button>`
  ).join("");
}

function renderAdvice(advice) {
  if (!advice) {
    return '<div class="advice-empty">No coach read yet. Click Get Advice after confirming this marker.</div>';
  }
  const secondary = (advice.secondary_mistakes || []).length
    ? `<p class="muted">Also check: ${escapeHtml(advice.secondary_mistakes.join(", "))}</p>`
    : "";
  return `
    <div class="advice-box">
      <div class="advice-head">
        <strong>${escapeHtml(titleCase(advice.primary_mistake))}</strong>
        <span>${Math.round(Number(advice.confidence || 0) * 100)}%</span>
      </div>
      ${secondary}
      <p class="coach-read">${escapeHtml(advice.what_happened)}</p>
      <p class="coach-action"><strong>Do this:</strong> ${escapeHtml(advice.better_play)}</p>
      <p><strong>Practice:</strong> ${escapeHtml(advice.drill)}</p>
      <div class="advice-actions">
        <button class="secondary" data-action="advice-feedback" data-id="${advice.id}" data-verdict="accepted">Accept</button>
        <button class="danger" data-action="advice-feedback" data-id="${advice.id}" data-verdict="rejected">Reject</button>
        ${advice.feedback ? `<span class="muted">Marked ${escapeHtml(advice.feedback.verdict)}</span>` : ""}
      </div>
    </div>
  `;
}

function shortenText(value, maxLength = 160) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (text.length <= maxLength) return text;
  return `${text.slice(0, Math.max(0, maxLength - 1)).trim()}...`;
}

function titleCase(value) {
  return String(value || "")
    .split(" ")
    .map((part) => part ? part[0].toUpperCase() + part.slice(1) : part)
    .join(" ");
}

function renderTags(labels) {
  return labels.map((label) => `<span class="tag">${escapeHtml(label)}</span>`).join(" ");
}

function applyPreset(button) {
  const label = button.dataset.label;
  const targetId = button.parentElement.dataset.presetTarget;
  const field = button.parentElement.dataset.presetField;
  const input = targetId
    ? document.querySelector(`#${targetId}`)
    : button.closest(".death-editor, .suggestion-card").querySelector(`[data-field="${field}"]`);
  if (!input) return;
  const labels = input.value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  if (!labels.includes(label)) {
    labels.push(label);
  }
  input.value = labels.join(", ");
}

async function addDeath(matchId) {
  await api(`/api/matches/${matchId}/deaths`, {
    method: "POST",
    body: JSON.stringify({
      round_number: document.querySelector("#newRound").value,
      timestamp: document.querySelector("#newTimestamp").value,
      mistake_labels: document.querySelector("#newLabels").value,
      notes: document.querySelector("#newNotes").value,
      confidence: 1,
    }),
  });
  setStatus("Death marker added.");
  await loadReport(matchId);
  await loadTrends();
}

async function acceptSuggestion(button) {
  const card = button.closest(".suggestion-card");
  const id = button.dataset.id;
  const labels = card.querySelector('[data-field="suggestion_labels"]').value;
  const notes = card.querySelector('[data-field="suggestion_notes"]').value;
  await api(`/api/suggestions/${id}`, {
    method: "POST",
    body: JSON.stringify({
      action: "accept",
      timestamp: button.dataset.ts,
      mistake_labels: labels,
      notes,
      confidence: 0.6,
    }),
  });
  setStatus("Suggested death accepted.");
  await loadReport(currentMatchId);
  await loadTrends();
}

async function rejectSuggestion(id) {
  await api(`/api/suggestions/${id}`, {
    method: "POST",
    body: JSON.stringify({ action: "reject" }),
  });
  setStatus("Suggested death rejected.");
  await loadReport(currentMatchId);
}

async function saveDeath(button) {
  const card = button.closest(".death-card");
  const id = button.dataset.id;
  const read = (field) => card.querySelector(`[data-field="${field}"]`).value;
  await api(`/api/deaths/${id}`, {
    method: "POST",
    body: JSON.stringify({
      round_number: read("round_number"),
      timestamp: read("timestamp"),
      mistake_labels: read("mistake_labels"),
      notes: read("notes"),
      confidence: read("confidence"),
    }),
  });
  setStatus("Death marker saved.");
  await loadReport(currentMatchId);
  await loadTrends();
}

async function saveDeathCorrection(button) {
  const card = button.closest(".death-card");
  await api("/api/corrections", {
    method: "POST",
    body: JSON.stringify({
      subject_type: "death",
      subject_id: button.dataset.id,
      correction_type: "round_phase",
      data: {
        phase: card.querySelector('[data-field="round_phase_correction"]').value,
        note: card.querySelector('[data-field="correction_note"]').value,
      },
    }),
  });
  setStatus("Manual correction saved.");
}

async function deleteDeath(id) {
  await api(`/api/deaths/${id}`, { method: "DELETE" });
  setStatus("Death marker deleted.");
  await loadReport(currentMatchId);
  await loadTrends();
}

function jumpTo(ts) {
  const player = document.querySelector("#vodPlayer");
  if (!player) return;
  player.currentTime = Math.max(0, Number(ts) - 5);
  player.play().catch(() => {});
}

function setAnnotationTime(button) {
  const player = document.querySelector("#vodPlayer");
  const card = button.closest(".death-card");
  if (!player || !card) return;
  const base = Number(button.closest(".timeline-actions")?.dataset.deathTs || 0);
  const relative = Math.max(0, player.currentTime - Math.max(0, base - 8));
  const input = card.querySelector(`[data-field="${button.dataset.field}"]`);
  if (input) input.value = relative.toFixed(1);
}

function loopDeath(ts) {
  const player = document.querySelector("#vodPlayer");
  if (!player) return;
  const start = Math.max(0, Number(ts) - 8);
  const end = Math.max(start + 1, Number(ts) + 4);
  player.currentTime = start;
  player.play().catch(() => {});
  const onTime = () => {
    if (player.currentTime >= end) player.currentTime = start;
  };
  player.removeEventListener("timeupdate", window.__deathLoopHandler || (() => {}));
  window.__deathLoopHandler = onTime;
  player.addEventListener("timeupdate", onTime);
}

function renderCalibrationOverlay() {
  const overlay = document.querySelector("#calibrationOverlay");
  if (!overlay) return;
  overlay.innerHTML = "";
  for (const [key, label] of CALIBRATION_REGIONS) {
    const region = currentCalibration[key];
    if (!region) continue;
    const box = document.createElement("div");
    box.className = `overlay-box ${key === selectedCalibrationRegion ? "selected" : ""}`;
    box.dataset.region = key;
    box.style.left = `${region.x * 100}%`;
    box.style.top = `${region.y * 100}%`;
    box.style.width = `${region.w * 100}%`;
    box.style.height = `${region.h * 100}%`;
    box.innerHTML = `<span>${escapeHtml(label)}</span><i></i>`;
    overlay.appendChild(box);
  }
}

function startCalibrationDrag(event) {
  const box = event.target.closest(".overlay-box");
  if (!box) return;
  event.preventDefault();
  selectedCalibrationRegion = box.dataset.region;
  const overlay = box.closest(".calibration-overlay");
  const rect = overlay.getBoundingClientRect();
  const region = currentCalibration[selectedCalibrationRegion];
  dragCalibration = {
    mode: event.target.tagName === "I" ? "resize" : "move",
    rect,
    startX: event.clientX,
    startY: event.clientY,
    region: { ...region },
  };
  renderCalibration(currentCalibration);
  renderCalibrationOverlay();
}

function moveCalibrationDrag(event) {
  if (!dragCalibration) return;
  const dx = (event.clientX - dragCalibration.startX) / dragCalibration.rect.width;
  const dy = (event.clientY - dragCalibration.startY) / dragCalibration.rect.height;
  const next = { ...dragCalibration.region };
  if (dragCalibration.mode === "resize") {
    next.w = clamp(next.w + dx, 0.02, 1 - next.x);
    next.h = clamp(next.h + dy, 0.02, 1 - next.y);
  } else {
    next.x = clamp(next.x + dx, 0, 1 - next.w);
    next.y = clamp(next.y + dy, 0, 1 - next.h);
  }
  currentCalibration[selectedCalibrationRegion] = next;
  renderCalibration(currentCalibration);
  renderCalibrationOverlay();
}

function stopCalibrationDrag() {
  dragCalibration = null;
}

function toggleCalibrationOverlay() {
  const overlay = document.querySelector("#calibrationOverlay");
  if (!overlay) return;
  if (!overlay.children.length) {
    renderCalibrationOverlay();
  }
  overlay.classList.toggle("hidden");
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function fileName(path) {
  return path.split(/[\\/]/).pop();
}

function formatTs(value) {
  if (value === null || value === undefined || value === "") return "unknown";
  const seconds = Math.floor(Number(value));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function formatDeathTime(death) {
  const timestamp = formatTs(death.timestamp);
  if (death.round_number) {
    return `Round ${death.round_number} · ${timestamp}`;
  }
  return `${timestamp} · Round unknown`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}

els.saveSettingsBtn.addEventListener("click", () => saveSettings().catch((err) => setStatus(err.message)));
els.saveCalibrationBtn.addEventListener("click", () => saveCalibration().catch((err) => setStatus(err.message)));
els.scanBtn.addEventListener("click", () => scanFolder().catch((err) => setStatus(err.message)));
els.importBtn.addEventListener("click", () => importVideo().catch((err) => setStatus(err.message)));
els.refreshBtn.addEventListener("click", () => Promise.all([loadVersionBadge(), loadMatches(), loadTrends(), loadCapabilities(), loadAutomation()]).catch((err) => setStatus(err.message)));
els.coachView.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "save-profile") saveCoachProfile().catch((err) => setStatus(err.message));
  if (action === "start-session") startPlaySession().catch((err) => setStatus(err.message));
  if (action === "end-session") endPlaySession(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "start-goal") startSuggestedGoal().catch((err) => setStatus(err.message));
  if (action === "complete-goal") completeGoal(button.dataset.id).catch((err) => setStatus(err.message));
});
els.automationView.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "save-automation") saveAutomationSettings().catch((err) => setStatus(err.message));
  if (action === "save-setup") saveSetupWizard().catch((err) => setStatus(err.message));
  if (action === "start-watcher") startWatcher().catch((err) => setStatus(err.message));
  if (action === "stop-watcher") stopWatcher().catch((err) => setStatus(err.message));
  if (action === "cancel-job") cancelJob(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "cleanup-frames") cleanupStorage(["vision", "deep"]).catch((err) => setStatus(err.message));
  if (action === "cleanup-clips") cleanupStorage(["clips"]).catch((err) => setStatus(err.message));
  if (action === "retention") applyRetention().catch((err) => setStatus(err.message));
  if (action === "backup-db") backupDb().catch((err) => setStatus(err.message));
  if (action === "restore-db") restoreDb().catch((err) => setStatus(err.message));
  if (action === "search-deaths") searchDeaths().catch((err) => setStatus(err.message));
  if (action === "load-playbook-editor") loadPlaybookEditor();
  if (action === "save-playbook") savePlaybook().catch((err) => setStatus(err.message));
  if (action === "delete-playbook") deletePlaybookFromEditor().catch((err) => setStatus(err.message));
  if (action === "apply-correction") applyCorrection(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "use-lmstudio-defaults") useLmStudioDefaults();
  if (action === "test-local-ai") testLocalAiConfig().catch((err) => setStatus(err.message));
  if (action === "save-local-ai") saveLocalAiConfig().catch((err) => setStatus(err.message));
  if (action === "load-prompt") loadPromptEditor();
  if (action === "save-prompt") savePromptTemplate().catch((err) => setStatus(err.message));
  if (action === "apply-detector-tuning") applyDetectorTuning().catch((err) => setStatus(err.message));
  if (action === "refresh-diagnostics") loadAutomation().then(() => setStatus("Diagnostics refreshed.")).catch((err) => setStatus(err.message));
  if (action === "refresh-evaluation") loadAutomation().then(() => setStatus("Benchmark refreshed.")).catch((err) => setStatus(err.message));
  if (action === "privacy-export") privacyExport().catch((err) => setStatus(err.message));
  if (action === "debug-bundle") debugBundle().catch((err) => setStatus(err.message));
  if (action === "privacy-wipe-frames") privacyWipe(["vision", "deep"]).catch((err) => setStatus(err.message));
  if (action === "privacy-wipe-clips") privacyWipe(["clips"]).catch((err) => setStatus(err.message));
  if (action === "session-report") refreshSessionReport().catch((err) => setStatus(err.message));
  if (action === "export-memory") exportMemory().catch((err) => setStatus(err.message));
  if (action === "import-memory") importMemory().catch((err) => setStatus(err.message));
  if (action === "export-report-json") exportCurrentReport("json").catch((err) => setStatus(err.message));
  if (action === "export-report-html") exportCurrentReport("html").catch((err) => setStatus(err.message));
  if (action === "import-stats") importStats().catch((err) => setStatus(err.message));
});
els.matchesList.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const id = button.dataset.id;
  const action = button.dataset.action;
  if (action === "view") loadReport(id).catch((err) => setStatus(err.message));
  if (action === "save-match-metadata") saveMatchMetadata(button).catch((err) => setStatus(err.message));
  if (action === "auto-coach") startAutoCoach(id).catch((err) => setStatus(err.message));
  if (action === "full-vod-coach") startFullVodCoach(id).catch((err) => setStatus(err.message));
  if (action === "guided-coach") runGuidedCoach(id).catch((err) => setStatus(err.message));
  if (action === "pipeline") startPipeline(id).catch((err) => setStatus(err.message));
  if (action === "batch-deaths") startDeathBatch(id).catch((err) => setStatus(err.message));
  if (action === "analyze") analyzeMatch(id).catch((err) => setStatus(err.message));
  if (action === "suggest") suggestDeaths(id).catch((err) => setStatus(err.message));
  if (action === "hud") runMatchAnalysis(id, "hud").catch((err) => setStatus(err.message));
  if (action === "minimap") runMatchAnalysis(id, "minimap").catch((err) => setStatus(err.message));
  if (action === "ocr") runMatchAnalysis(id, "ocr").catch((err) => setStatus(err.message));
  if (action === "events-v2") runMatchAnalysis(id, "events-v2").catch((err) => setStatus(err.message));
  if (action === "rounds") runMatchAnalysis(id, "rounds/reconstruct").catch((err) => setStatus(err.message));
  if (action === "scoreboard-rounds") runMatchAnalysis(id, "scoreboard-rounds").catch((err) => setStatus(err.message));
  if (action === "crosshair") runMatchAnalysis(id, "crosshair").catch((err) => setStatus(err.message));
  if (action === "review-queue") runMatchAnalysis(id, "review-queue").catch((err) => setStatus(err.message));
  if (action === "review-queue-v2") runMatchAnalysis(id, "review-queue-v2").catch((err) => setStatus(err.message));
  if (action === "story") runMatchAnalysis(id, "story").catch((err) => setStatus(err.message));
  if (action === "playbook") loadPlaybook(id).catch((err) => setStatus(err.message));
  if (action === "clips") extractClips(id).catch((err) => setStatus(err.message));
  if (action === "review") generateMatchReview(id).catch((err) => setStatus(err.message));
  if (action === "write") writeReport(id).catch((err) => setStatus(err.message));
});
els.reportView.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "cancel-job") cancelJob(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "jump") jumpTo(button.dataset.ts);
  if (action === "guided-coach") runGuidedCoach(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "preset-label") applyPreset(button);
  if (action === "accept-suggestion") acceptSuggestion(button).catch((err) => setStatus(err.message));
  if (action === "reject-suggestion") rejectSuggestion(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "coach-moment-feedback") saveCoachMomentFeedback(button).catch((err) => setStatus(err.message));
  if (action === "benchmark-false-positive") saveBenchmarkLabel({ match_id: button.dataset.match, suggestion_id: button.dataset.id, timestamp: button.dataset.ts, label_type: "false_positive", note: "Marked from suggestion card" }).catch((err) => setStatus(err.message));
  if (action === "benchmark-true-positive") saveBenchmarkLabel({ match_id: button.dataset.match, death_id: button.dataset.id, timestamp: button.dataset.ts, label_type: "true_positive", note: "Marked from death card" }).catch((err) => setStatus(err.message));
  if (action === "benchmark-missed") saveBenchmarkLabel({ match_id: button.dataset.id, timestamp: document.querySelector("#benchmarkMissedTs").value, label_type: "missed_death", note: document.querySelector("#benchmarkNote").value }).catch((err) => setStatus(err.message));
  if (action === "add-death") addDeath(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "advice") getAdvice(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "vision") analyzeDeathVision(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "keyframes") extractKeyframes(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "understand") understandClip(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "gameplay") analyzeGameplay(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "ai-review") aiReview(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "local-ai-review") localAiReview(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "advice-feedback") saveAdviceFeedback(button.dataset.id, button.dataset.verdict).catch((err) => setStatus(err.message));
  if (action === "save-death") saveDeath(button).catch((err) => setStatus(err.message));
  if (action === "save-correction") saveDeathCorrection(button).catch((err) => setStatus(err.message));
  if (action === "save-annotation") saveClipAnnotation(button).catch((err) => setStatus(err.message));
  if (action === "set-annotation-time") setAnnotationTime(button);
  if (action === "loop-death") loopDeath(button.dataset.ts);
  if (action === "delete-death") deleteDeath(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "toggle-calibration-overlay") toggleCalibrationOverlay();
  if (action === "save-overlay-calibration") saveCalibration().catch((err) => setStatus(err.message));
});

els.reportView.addEventListener("change", (event) => {
  if (event.target.id !== "overlayRegion") return;
  selectedCalibrationRegion = event.target.value;
  renderCalibrationOverlay();
});
els.reportView.addEventListener("pointerdown", startCalibrationDrag);
window.addEventListener("pointermove", moveCalibrationDrag);
window.addEventListener("pointerup", stopCalibrationDrag);

loadSettings()
  .then(() => Promise.all([loadVersionBadge(), loadMatches(), loadTrends(), loadCoach(), loadCapabilities(), loadCalibration(), loadAutomation()]))
  .then(() => {
    if (latestJobs.some((job) => ["queued", "running"].includes(job.status))) ensureJobPolling();
  })
  .catch((err) => setStatus(err.message));
