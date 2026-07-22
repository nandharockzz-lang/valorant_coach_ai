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
let latestCoachDashboard = null;
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
let detectorBoxDrag = null;
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

function setStatus(message, options = {}) {
  const state = options.state || inferStatusState(message);
  const progress = normalizeProgress(options.progress);
  els.status.className = `status ${state}`;
  els.status.setAttribute("aria-busy", state === "busy" ? "true" : "false");
  els.status.innerHTML = `
    ${state === "busy" ? '<span class="status-spinner" aria-hidden="true"></span>' : '<span class="status-dot" aria-hidden="true"></span>'}
    <span class="status-text">${escapeHtml(message || "Ready.")}</span>
    ${progress !== null ? `<span class="status-progress">${progress}%</span>` : ""}
  `;
}

function inferStatusState(message) {
  const text = String(message || "").toLowerCase();
  if (text.endsWith("...") || text.includes(" queued") || text.includes(" running") || text.includes("scanning") || text.includes("analyzing") || text.includes("generating") || text.includes("extracting") || text.includes("importing")) {
    return "busy";
  }
  if (text.includes("failed") || text.includes("error") || text.includes("not found")) {
    return "error";
  }
  return "idle";
}

function normalizeProgress(value) {
  if (value === undefined || value === null || value === "") {
    return null;
  }
  const number = Math.round(Number(value));
  if (!Number.isFinite(number)) {
    return null;
  }
  return Math.max(0, Math.min(100, number));
}

function activateDashboardTab(targetId) {
  document.querySelectorAll(".side-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tabTarget === targetId);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === targetId);
  });
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
  const detectorCandidatePath = currentMatchId
    ? `/api/detector/candidates?match_id=${encodeURIComponent(currentMatchId)}`
    : "/api/detector/candidates";
  const [settings, jobs, watcher, storage, analytics, logs, tools, backups, schema, version, providers, privacy, corrections, playbookPayload, diagnostics, evaluation, plugins, localAi, knowledge, setup, prompts, tuning, modelAudit, sessionReport, detectorStatus, detectorDashboard, detectorCandidates, signals] = await Promise.all([
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
    api("/api/knowledge/status"),
    api("/api/setup"),
    api("/api/prompts"),
    api("/api/detector/tuning"),
    api("/api/privacy/model-audit"),
    api("/api/sessions/report"),
    api("/api/detector/status"),
    api("/api/detector/dashboard"),
    api(detectorCandidatePath),
    api("/api/signals"),
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
    knowledge,
    setup,
    prompts,
    tuning,
    modelAudit,
    sessionReport,
    detectorStatus,
    detectorDashboard,
    detectorCandidates,
    signals
  );
  latestJobs = jobs.jobs || [];
  renderJobProgressPanel();
}

function renderAutomation(settings, jobs, watcher, storage, analytics, logs, tools, backups, schema, version, providers, privacy, corrections, playbooks, diagnostics, evaluation, plugins, localAi, knowledge, setup, prompts, tuning, modelAudit, sessionReport, detectorStatus, detectorDashboard, detectorCandidates, signals) {
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
  const tesseractReady = Boolean((tools.tesseract || {}).available);
  const detectorReadiness = tesseractReady
    ? `Primary death detector is active for ${settings.player_name || "SicaJR"}: killfeed OCR plus combat report.`
    : "Primary death detector needs Tesseract OCR. Until then, Find Deaths uses only fallback visual signals.";
  const promptOptions = Object.keys(prompts.templates || {}).sort().map((key) => `<option value="${escapeAttr(key)}" ${prompts.active === key ? "selected" : ""}>${escapeHtml(key)}</option>`).join("");
  const knowledgeCounts = Object.entries((knowledge || {}).counts || {}).map(([key, value]) => `<span class="tag">${escapeHtml(key)} ${escapeHtml(value)}</span>`).join("");
  els.automationView.innerHTML = `
    <div class="automation-block">
      <h3>Setup Wizard</h3>
      <p>${setup.ready ? "Ready for review workflow." : "Finish required setup before relying on automation."}</p>
      <p class="${tesseractReady ? "detector-ready" : "detector-warning"}">${escapeHtml(detectorReadiness)}</p>
      <ul class="compact-list">${setupRows}</ul>
      <label>Recording folder <input id="setupRecordingDir" type="text" value="${escapeAttr(settings.recording_dir || "")}" /></label>
      <label>In-game name <input id="playerName" type="text" value="${escapeAttr(settings.player_name || "SicaJR")}" placeholder="SicaJR" /></label>
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
      <label>Enemy detector command <input id="enemyDetectorCommand" type="text" value="${escapeAttr(settings.enemy_detector_command || "")}" placeholder="optional local detector {image}" /></label>
      <label>Enemy detector model <input id="enemyDetectorModelPath" type="text" value="${escapeAttr(settings.enemy_detector_model_path || "")}" placeholder="optional best.pt path" /></label>
      <label>Frame sample rate
        <select id="frameSampleRate">
          ${["light", "standard", "dense"].map((item) => `<option value="${item}" ${settings.frame_sample_rate === item ? "selected" : ""}>${item}</option>`).join("")}
        </select>
      </label>
      <label>Find Deaths OCR cap <input id="deathScanMaxOcrFrames" type="number" min="30" max="600" value="${escapeAttr(settings.death_scan_max_ocr_frames || "180")}" /></label>
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
      <label>Purpose
        <select id="localAiPurpose">
          <option value="coach" ${localAi.purpose === "coach" ? "selected" : ""}>Gameplay coach</option>
          <option value="ocr" ${localAi.purpose === "ocr" ? "selected" : ""}>OCR / HUD reader</option>
        </select>
      </label>
      <label>Review mode
        <select id="localAiReviewMode">
          <option value="contact" ${localAi.review_mode === "contact" ? "selected" : ""}>Contact: final 5s at 5 FPS</option>
          <option value="burst" ${localAi.review_mode === "burst" ? "selected" : ""}>Burst: final 5s at 10 FPS, batched</option>
          <option value="hybrid" ${localAi.review_mode === "hybrid" ? "selected" : ""}>Hybrid: context + contact</option>
          <option value="context" ${localAi.review_mode === "context" ? "selected" : ""}>Context: final 10s at 2 FPS</option>
          <option value="adaptive" ${localAi.review_mode === "adaptive" ? "selected" : ""}>Adaptive: configurable setup + dense contact</option>
        </select>
      </label>
      <label>Review window seconds
        <input id="localAiReviewWindow" type="number" min="5" max="20" step="1" value="${escapeAttr(localAi.review_window_seconds || "10")}" />
      </label>
      <label>FPS override
        <input id="localAiReviewFps" type="number" min="1" max="20" step="1" value="${escapeAttr(localAi.review_fps || "")}" placeholder="mode default" />
      </label>
      <label>Context limit
        <input id="localAiContextLimit" type="number" min="4096" max="131072" step="512" value="${escapeAttr(localAi.context_limit || "8192")}" />
      </label>
      <label>Image token estimate
        <input id="localAiImageTokenEstimate" type="number" min="256" max="4096" step="64" value="${escapeAttr(localAi.image_token_estimate || "900")}" />
      </label>
      <div class="row compact-actions">
        <button class="secondary" data-action="set-local-ai-window" data-window="5">5s</button>
        <button class="secondary" data-action="set-local-ai-window" data-window="10">10s</button>
        <button class="secondary" data-action="set-local-ai-window" data-window="15">15s</button>
        <button class="secondary" data-action="set-local-ai-fps" data-fps="">Mode Default</button>
        <button class="secondary" data-action="set-local-ai-fps" data-fps="8">8 FPS</button>
        <button class="secondary" data-action="set-local-ai-fps" data-fps="12">12 FPS</button>
        <button class="secondary" data-action="set-local-ai-fps" data-fps="15">15 FPS</button>
      </div>
      <p class="muted">Active sequence: ${escapeHtml(localAi.review_mode_label || "")} · frame cap ${escapeHtml(localAi.review_frame_limit || "default")} · request budget ${escapeHtml(localAi.context_limit || "8192")} tokens</p>
      <label>Base URL <input id="localAiBaseUrl" type="text" value="${escapeAttr(localAi.base_url || "")}" placeholder="http://127.0.0.1:11434" /></label>
      <label>Custom command <input id="localAiCommand" type="text" value="${escapeAttr(localAi.command || "")}" placeholder="python C:\\path\\review_clip.py" /></label>
      <div class="row">
        <button class="secondary" data-action="use-lmstudio-defaults">Use LM Studio Defaults</button>
        <button class="secondary" data-action="use-olmocr-defaults">Use olmOCR Defaults</button>
        <button class="secondary" data-action="test-local-ai">Test Local AI</button>
        <button class="secondary" data-action="save-local-ai">Save Local AI</button>
      </div>
      <div id="localAiTestResult" class="muted"></div>
      <p class="muted">${escapeHtml(localAi.expected_protocol || "")}</p>
    </div>
    <div class="automation-block">
      <h3>Knowledge Base</h3>
      <p>${knowledge.ready ? escapeHtml(knowledge.summary || "Knowledge base ready.") : "Build the local VALORANT knowledge base before relying on game-specific model context."}</p>
      <p class="muted">Last built ${escapeHtml(knowledge.last_built_at || "never")} · ${escapeHtml(knowledge.snippet_count || 0)} snippet(s)</p>
      <div class="tag-row">${knowledgeCounts || '<span class="tag">not built</span>'}</div>
      <label>Search <input id="knowledgeSearchText" type="text" placeholder="Ascent Jett dry peek crosshair" /></label>
      <div class="row">
        <button class="secondary" data-action="rebuild-knowledge">Rebuild Knowledge</button>
        <button class="secondary" data-action="search-knowledge">Search Knowledge</button>
      </div>
      <div id="knowledgeResults" class="knowledge-results muted">Retrieved snippets are injected into Local AI prompts, capped to stay context-safe.</div>
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
    ${renderTrainedDetectorPanel(detectorStatus || {}, detectorDashboard || {}, detectorCandidates || {}, signals || {})}
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

function renderTrainedDetectorPanel(detector, dashboard, candidates, signals) {
  const annotations = detector.annotations || {};
  const model = dashboard.model || {};
  const nextAction = dashboard.next_action || {};
  const latestEval = dashboard.latest_evaluation || null;
  const latestJob = dashboard.latest_training_job || null;
  const readiness = Math.max(0, Math.min(100, Number(dashboard.readiness_percent || 0)));
  const candidateRows = (candidates.candidates || []).slice(0, 8).map((item) => `
    <li>
      <button class="ghost" data-action="jump" data-ts="${escapeAttr(item.timestamp || 0)}">${escapeHtml(formatTs(item.timestamp || 0))}</button>
      <strong>${escapeHtml(item.role || "frame")}</strong>
      <span>${escapeHtml(item.status || "needs_label")} · priority ${escapeHtml(item.priority || 0)} · death #${escapeHtml(item.death_id || "")}</span>
    </li>
  `).join("");
  const metricCards = [
    ["Boxes", annotations.box_count || 0, "YOLO labels"],
    ["Frames", annotations.frame_count || 0, "unique images"],
    ["Negatives", annotations.negative_count || 0, "no_enemy labels"],
    ["Queue", (dashboard.candidates || {}).needs_label || candidates.count || 0, "needs review"],
  ].map(([label, value, detail]) => `
    <div class="detector-metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <p>${escapeHtml(detail)}</p>
    </div>
  `).join("");
  const milestoneRows = (dashboard.milestones || []).map((item) => `
    <div class="detector-progress-row">
      <div><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.current_boxes || 0)} / ${escapeHtml(item.target_boxes || 0)} boxes</span></div>
      ${renderMiniProgress(item.percent || 0)}
      <p class="muted">${item.complete ? "Complete" : `${escapeHtml(item.remaining_boxes || 0)} more box(es)`} · ${escapeHtml(item.description || "")}</p>
    </div>
  `).join("");
  const classRows = (dashboard.class_progress || []).map((item) => `
    <div class="detector-progress-row compact">
      <div><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.current || 0)} / ${escapeHtml(item.target || 0)}</span></div>
      ${renderMiniProgress(item.percent || 0)}
    </div>
  `).join("");
  const gapRows = (dashboard.gaps || []).slice(0, 5).map((item) => `
    <li><strong>${escapeHtml(item.label || "Gap")}</strong> <span class="muted">${escapeHtml(item.detail || "")}</span></li>
  `).join("");
  const evalBlock = latestEval ? `
    <div class="detector-quality">
      <div><span>Precision</span><strong>${escapeHtml(latestEval.precision ?? "n/a")}</strong></div>
      <div><span>Recall</span><strong>${escapeHtml(latestEval.recall ?? "n/a")}</strong></div>
      <div><span>Frames</span><strong>${escapeHtml(latestEval.frames ?? "n/a")}</strong></div>
    </div>
    <p class="muted">${escapeHtml(latestEval.summary || "")}</p>
  ` : `<p class="muted">No detector evaluation yet. Train a model, then run Evaluate Detector to get precision and recall.</p>`;
  const jobBlock = latestJob ? `
    <div class="detector-job">
      <div><strong>#${escapeHtml(latestJob.id)} ${escapeHtml(latestJob.status || "unknown")}</strong><span>${escapeHtml(latestJob.progress || 0)}%</span></div>
      ${renderMiniProgress(latestJob.progress || 0)}
      <p class="muted">${escapeHtml(latestJob.message || "")}</p>
    </div>
  ` : `<p class="muted">No detector training job has run yet.</p>`;
  return `
    <div class="automation-block detector-dashboard">
      <div class="detector-dashboard-head">
        <div>
          <h3>Detector Model Dashboard</h3>
          <p>${escapeHtml(dashboard.summary || detector.summary || "Detector status unavailable.")}</p>
        </div>
        <div class="detector-readiness" aria-label="Detector dataset readiness">
          <strong>${escapeHtml(readiness)}%</strong>
          <span>${escapeHtml(dashboard.stage_label || "Needs labels")}</span>
        </div>
      </div>
      <div class="detector-readiness-bar">${renderMiniProgress(readiness)}</div>
      <div class="detector-next-action">
        <span>Recommended next step</span>
        <strong>${escapeHtml(nextAction.label || "Build label queue")}</strong>
        <p>${escapeHtml(nextAction.detail || "Create a training queue, label frames, then train locally.")}</p>
      </div>
      <div class="detector-metric-grid">${metricCards}</div>
      <p class="muted">Model ${model.model_exists ? "ready" : "missing"} · Ultralytics ${model.ultralytics_available ? "installed" : "not installed"} · Signal contracts ${escapeHtml((signals.signals || []).length || 0)}</p>
      ${detector.suggested_command ? `<label>Suggested command <input id="detectorSuggestedCommand" type="text" value="${escapeAttr(detector.suggested_command)}" readonly /></label>` : ""}
      <div class="detector-train-controls">
        <label>Epochs <input id="detectorTrainEpochs" type="number" min="1" max="300" value="40" /></label>
        <label>Image size <input id="detectorTrainImgsz" type="number" min="320" max="1280" step="32" value="640" /></label>
        <label>Base model <input id="detectorTrainBaseModel" type="text" value="yolo11n.pt" /></label>
      </div>
      <div class="row">
        <button class="secondary" data-action="build-detector-candidates">Build Label Queue</button>
        <button class="secondary" data-action="prelabel-detector-candidates">Pre-label Queue</button>
        <button class="secondary" data-action="evaluate-detector">Evaluate Detector</button>
        <button class="secondary" data-action="export-detector-dataset">Export YOLO Dataset</button>
        <button data-action="train-detector">Train Detector</button>
        ${detector.suggested_command ? '<button class="secondary" data-action="use-detector-command">Use Suggested Command</button>' : ""}
      </div>
      <div class="detector-dashboard-grid">
        <section>
          <h4>Training Milestones</h4>
          ${milestoneRows || '<p class="muted">No milestones available.</p>'}
        </section>
        <section>
          <h4>Class Coverage</h4>
          ${classRows || '<p class="muted">No class labels yet.</p>'}
        </section>
        <section>
          <h4>Model Quality</h4>
          ${evalBlock}
        </section>
        <section>
          <h4>Training Progress</h4>
          ${jobBlock}
        </section>
      </div>
      <details class="advanced-actions">
        <summary>Remaining gaps</summary>
        <ul class="compact-list">${gapRows || "<li>No major detector training gaps currently reported.</li>"}</ul>
      </details>
      <details class="advanced-actions">
        <summary>Active-learning queue</summary>
        <ul class="compact-list">${candidateRows || "<li>Build a label queue after extracting keyframes or running Clip Coach.</li>"}</ul>
      </details>
      <p class="muted">${escapeHtml(dashboard.note || "Confirmed enemies come only from trained detector boxes, VLM evidence, or your labels. Red/HUD heuristics remain contact proxies.")}</p>
    </div>
  `;
}

async function saveAutomationSettings() {
  await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({
      recording_dir: els.recordingDir.value,
      player_name: document.querySelector("#playerName")?.value || "SicaJR",
      auto_import: document.querySelector("#autoImport").value,
      auto_analysis: document.querySelector("#autoAnalysis").value,
      detector_sensitivity: document.querySelector("#detectorSensitivity").value,
      enemy_detector_command: document.querySelector("#enemyDetectorCommand")?.value || "",
      enemy_detector_model_path: document.querySelector("#enemyDetectorModelPath")?.value || "",
      frame_sample_rate: document.querySelector("#frameSampleRate").value,
      death_scan_max_ocr_frames: document.querySelector("#deathScanMaxOcrFrames").value,
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
    purpose: document.querySelector("#localAiPurpose").value,
    review_mode: document.querySelector("#localAiReviewMode").value,
    review_fps: document.querySelector("#localAiReviewFps").value,
    review_window_seconds: document.querySelector("#localAiReviewWindow").value,
    context_limit: document.querySelector("#localAiContextLimit").value,
    image_token_estimate: document.querySelector("#localAiImageTokenEstimate").value,
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
  const purpose = document.querySelector("#localAiPurpose");
  const reviewMode = document.querySelector("#localAiReviewMode");
  const reviewFps = document.querySelector("#localAiReviewFps");
  const reviewWindow = document.querySelector("#localAiReviewWindow");
  const contextLimit = document.querySelector("#localAiContextLimit");
  const imageTokenEstimate = document.querySelector("#localAiImageTokenEstimate");
  if (provider) provider.value = "lmstudio";
  if (baseUrl) baseUrl.value = "http://127.0.0.1:1234/v1";
  if (purpose) purpose.value = "coach";
  if (reviewMode) reviewMode.value = "contact";
  if (reviewFps) reviewFps.value = "";
  if (reviewWindow) reviewWindow.value = "10";
  if (contextLimit) contextLimit.value = "8192";
  if (imageTokenEstimate) imageTokenEstimate.value = "900";
  if (model && !model.value) model.value = "local-model";
  if (command) command.value = "";
  setStatus("LM Studio defaults filled. If LM Studio shows a specific model ID, paste it into Model before saving.");
}

function useOlmocrDefaults() {
  const provider = document.querySelector("#localAiProvider");
  const model = document.querySelector("#localAiModel");
  const baseUrl = document.querySelector("#localAiBaseUrl");
  const command = document.querySelector("#localAiCommand");
  const purpose = document.querySelector("#localAiPurpose");
  const reviewMode = document.querySelector("#localAiReviewMode");
  const reviewFps = document.querySelector("#localAiReviewFps");
  const reviewWindow = document.querySelector("#localAiReviewWindow");
  const contextLimit = document.querySelector("#localAiContextLimit");
  const imageTokenEstimate = document.querySelector("#localAiImageTokenEstimate");
  if (provider) provider.value = "lmstudio";
  if (baseUrl) baseUrl.value = "http://127.0.0.1:1234/v1";
  if (purpose) purpose.value = "ocr";
  if (reviewMode) reviewMode.value = "context";
  if (reviewFps) reviewFps.value = "";
  if (reviewWindow) reviewWindow.value = "10";
  if (contextLimit) contextLimit.value = "8192";
  if (imageTokenEstimate) imageTokenEstimate.value = "900";
  if (model) model.value = model.value || "olmocr";
  if (command) command.value = "";
  setStatus("olmOCR defaults filled. Paste the exact LM Studio model id if it differs, then Test Local AI and Save.");
}

function setLocalAiFps(value) {
  const input = document.querySelector("#localAiReviewFps");
  if (!input) return;
  input.value = value || "";
  setStatus(value ? `Local AI FPS override set to ${value}. Save Local AI to apply.` : "Local AI FPS override cleared. Save Local AI to apply.");
}

function setLocalAiWindow(value) {
  const input = document.querySelector("#localAiReviewWindow");
  if (!input) return;
  input.value = value || "10";
  const mode = document.querySelector("#localAiReviewMode");
  if (mode) mode.value = "adaptive";
  setStatus(`Local AI adaptive window set to ${input.value}s. Save Local AI to apply.`);
}

async function testLocalAiConfig() {
  const payload = {
    provider: document.querySelector("#localAiProvider").value,
    purpose: document.querySelector("#localAiPurpose").value,
    review_mode: document.querySelector("#localAiReviewMode").value,
    review_fps: document.querySelector("#localAiReviewFps").value,
    review_window_seconds: document.querySelector("#localAiReviewWindow").value,
    context_limit: document.querySelector("#localAiContextLimit").value,
    image_token_estimate: document.querySelector("#localAiImageTokenEstimate").value,
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
    payload.model = models[0];
    document.querySelector("#localAiModel").value = payload.model;
  }
  if (result.ok) {
    await api("/api/local-ai/config", { method: "POST", body: JSON.stringify(payload) });
    setStatus(`${result.message || "Local AI test complete."} Settings saved for Clip Coach.`);
    await loadAutomation();
  } else {
    setStatus(result.message || "Local AI test failed.", { state: "error" });
  }
}

async function rebuildKnowledgeBase() {
  setStatus("Rebuilding VALORANT knowledge base from local notes and structured game data...", { state: "busy" });
  const payload = await api("/api/knowledge/rebuild", {
    method: "POST",
    body: JSON.stringify({ fetch_remote: true }),
  });
  const index = payload.index || {};
  setStatus(`Knowledge base rebuilt: ${index.snippet_count || 0} snippets.`);
  await loadAutomation();
}

async function searchKnowledgeBase() {
  const query = document.querySelector("#knowledgeSearchText")?.value || "";
  const target = document.querySelector("#knowledgeResults");
  setStatus("Searching local VALORANT knowledge...", { state: "busy" });
  const payload = await api(`/api/knowledge/search?q=${encodeURIComponent(query)}`);
  const rows = (payload.items || []).map((item) => `
    <li>
      <strong>${escapeHtml(item.title || "")}</strong>
      <span>${escapeHtml(item.topic || "")} · score ${escapeHtml(item.score || "")}</span>
      <p>${escapeHtml(item.text || "")}</p>
    </li>
  `).join("");
  if (target) {
    target.innerHTML = `
      <p>${escapeHtml(payload.count || 0)} retrieved snippet(s).</p>
      <ul class="compact-list">${rows || "<li>No relevant snippets found.</li>"}</ul>
      <details>
        <summary>Prompt Context Preview</summary>
        <pre class="json-small">${escapeHtml(payload.prompt_context || "")}</pre>
      </details>
    `;
  }
  setStatus(`Knowledge search returned ${payload.count || 0} snippet(s).`);
}

async function saveSetupWizard() {
  await api("/api/setup", {
    method: "POST",
    body: JSON.stringify({
      recording_dir: document.querySelector("#setupRecordingDir").value,
      player_name: document.querySelector("#playerName")?.value || "SicaJR",
      auto_import: document.querySelector("#autoImport")?.value || "false",
      auto_analysis: document.querySelector("#autoAnalysis")?.value || "false",
      frame_sample_rate: document.querySelector("#frameSampleRate")?.value || "standard",
      detector_sensitivity: document.querySelector("#detectorSensitivity")?.value || "normal",
      enemy_detector_command: document.querySelector("#enemyDetectorCommand")?.value || "",
      local_ai_provider: document.querySelector("#localAiProvider")?.value || "custom-command",
      local_ai_model: document.querySelector("#localAiModel")?.value || "",
      local_ai_base_url: document.querySelector("#localAiBaseUrl")?.value || "",
      local_ai_command: document.querySelector("#localAiCommand")?.value || "",
      local_ai_review_mode: document.querySelector("#localAiReviewMode")?.value || "contact",
      local_ai_review_fps: document.querySelector("#localAiReviewFps")?.value || "",
      local_ai_review_window_seconds: document.querySelector("#localAiReviewWindow")?.value || "10",
      local_ai_context_limit: document.querySelector("#localAiContextLimit")?.value || "8192",
      local_ai_image_token_estimate: document.querySelector("#localAiImageTokenEstimate")?.value || "900",
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

async function exportDetectorDataset() {
  const payload = await api("/api/detector/export", { method: "POST", body: JSON.stringify({}) });
  setStatus(payload.message || "Detector dataset exported.");
  await loadAutomation();
}

async function trainDetector() {
  const epochs = Number(document.querySelector("#detectorTrainEpochs")?.value || 40);
  const imgsz = Number(document.querySelector("#detectorTrainImgsz")?.value || 640);
  const baseModel = document.querySelector("#detectorTrainBaseModel")?.value || "yolo11n.pt";
  const payload = await api("/api/detector/train", {
    method: "POST",
    body: JSON.stringify({ epochs, imgsz, base_model: baseModel }),
  });
  setStatus(`Detector training queued as job #${payload.job_id}.`, { state: "busy", progress: 1 });
  ensureJobPolling();
  await loadAutomation();
}

async function buildDetectorCandidates() {
  const payload = await api("/api/detector/candidates", {
    method: "POST",
    body: JSON.stringify({ match_id: currentMatchId || "", limit: 160 }),
  });
  setStatus(payload.summary || "Detector label queue built.");
  await loadAutomation();
}

async function prelabelDetectorCandidates() {
  const payload = await api("/api/detector/prelabel", {
    method: "POST",
    body: JSON.stringify({ match_id: currentMatchId || "", limit: 80 }),
  });
  setStatus(payload.message || `Pre-labeled ${payload.count || 0} frame(s).`);
  await loadAutomation();
}

async function evaluateDetector() {
  const payload = await api("/api/detector/evaluate", {
    method: "POST",
    body: JSON.stringify({ limit: 120 }),
  });
  setStatus(payload.summary || "Detector evaluation complete.");
  await loadAutomation();
}

function useDetectorCommand() {
  const suggested = document.querySelector("#detectorSuggestedCommand")?.value || "";
  const command = document.querySelector("#enemyDetectorCommand");
  if (command && suggested) {
    command.value = suggested;
    setStatus("Detector command filled. Save settings to apply it.");
  }
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

async function suggestDeaths(id, options = {}) {
  currentMatchId = id;
  activeCoachJobId = null;
  const hasRange = (options.start_seconds !== null && options.start_seconds !== undefined) || (options.end_seconds !== null && options.end_seconds !== undefined);
  const rangeLabel = hasRange
    ? ` ${formatScanRange(options.start_seconds, options.end_seconds)}`
    : "";
  setStatus(`Find Deaths${rangeLabel} queued for match #${id}...`, { state: "busy" });
  const payload = await api(`/api/matches/${id}/suggest-deaths`, { method: "POST", body: JSON.stringify(options) });
  if (payload.job_id) {
    activeCoachJobId = payload.job_id;
    completedJobIds.delete(Number(payload.job_id));
    setStatus(`Find Deaths${rangeLabel} running as job #${payload.job_id}.`, { state: "busy", progress: 1 });
    await Promise.all([loadReport(id), pollJobs()]);
    ensureJobPolling();
    return;
  }
  const detector = payload.detector || {};
  setStatus(payload.message, { state: detector.warning ? "error" : "idle" });
  await loadReport(id);
}

async function suggestDeathsRange(button) {
  const card = button.closest(".match-item");
  const id = button.dataset.id;
  const start = parseTimeInput(card?.querySelector(`[data-field="scan_start_${id}"]`)?.value || "");
  const end = parseTimeInput(card?.querySelector(`[data-field="scan_end_${id}"]`)?.value || "");
  const limitValue = Number(card?.querySelector(`[data-field="scan_limit_${id}"]`)?.value || 0);
  if (start === null && end === null) {
    throw new Error("Enter a start or end time for range scan.");
  }
  if (start !== null && end !== null && end <= start) {
    throw new Error("End time must be after start time.");
  }
  const options = {
    start_seconds: start,
    end_seconds: end,
    limit: Number.isFinite(limitValue) && limitValue > 0 ? Math.round(limitValue) : undefined,
  };
  await suggestDeaths(id, options);
}

function parseTimeInput(value) {
  const text = String(value || "").trim();
  if (!text) return null;
  if (/^\d+(?:\.\d+)?$/.test(text)) {
    return Number(text);
  }
  const parts = text.split(":").map((part) => Number(part));
  if (parts.some((part) => !Number.isFinite(part) || part < 0) || parts.length > 3) {
    throw new Error(`Invalid time: ${text}`);
  }
  if (parts.length === 2) {
    return parts[0] * 60 + parts[1];
  }
  if (parts.length === 3) {
    return parts[0] * 3600 + parts[1] * 60 + parts[2];
  }
  throw new Error(`Invalid time: ${text}`);
}

function formatScanRange(start, end) {
  if (start !== null && start !== undefined && end !== null && end !== undefined) return `from ${formatTs(start)} to ${formatTs(end)}`;
  if (start !== null && start !== undefined) return `from ${formatTs(start)}`;
  return `until ${formatTs(end)}`;
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
  const visibleJob = activeJob || latestJobs.find((job) => ["queued", "running"].includes(job.status));
  if (visibleJob && ["queued", "running"].includes(visibleJob.status)) {
    const label = visibleJob.name || `Job #${visibleJob.id}`;
    const message = visibleJob.message || visibleJob.status || "Running.";
    setStatus(`${label}: ${message}`, { state: "busy", progress: visibleJob.progress });
  }
  if (activeJob && ["complete", "failed", "cancelled"].includes(activeJob.status) && !completedJobIds.has(Number(activeJob.id))) {
    completedJobIds.add(Number(activeJob.id));
    const label = activeJob.name || `Job #${activeJob.id}`;
    const completeMessage = String(label).toLowerCase().includes("find deaths")
      ? "Find Deaths complete. Review suggested markers below the video."
      : `${label} complete. Review markers and advice are refreshed.`;
    setStatus(activeJob.status === "complete" ? completeMessage : `${label} ${activeJob.status}: ${activeJob.message || ""}`, { state: activeJob.status === "complete" ? "idle" : "error" });
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

async function getAdvice(deathId, options = {}) {
  setStatus(`Generating advice for death #${deathId}...`);
  const payload = await api(`/api/deaths/${deathId}/advice`, { method: "POST" });
  setStatus(`Advice generated: ${payload.advice.primary_mistake}`);
  if (options.reload !== false) {
    await loadReport(currentMatchId);
    await loadCoach();
  }
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

async function localAiReview(deathId, options = {}) {
  setStatus(`Clip Coach death #${deathId}: extracting frames, reading HUD/context with the VALORANT KB, then asking the local model...`, { state: "busy", progress: 35 });
  const payload = await api(`/api/deaths/${deathId}/local-ai-review`, { method: "POST" });
  setStatus(payload.message || `Clip Coach review ready for death #${deathId}.`);
  if (options.reload !== false && currentMatchId) await loadReport(currentMatchId);
  return payload;
}

async function coachClip(deathId) {
  setStatus(`Coach death #${deathId}: local context extraction and clip review running...`, { state: "busy", progress: 25 });
  let localError = "";
  try {
    await localAiReview(deathId, { reload: false });
  } catch (err) {
    localError = err.message;
  }
  await getAdvice(deathId, { reload: false });
  await Promise.all([currentMatchId ? loadReport(currentMatchId) : Promise.resolve(), loadCoach(), loadTrends()]);
  if (localError) {
    setStatus(`Normal coach advice saved. Clip Coach failed: ${localError}`, { state: "error" });
  } else {
    setStatus(`Coach review ready for death #${deathId}.`);
  }
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

async function saveClipReviewFeedback(button) {
  const card = button.closest(".death-card");
  const note = card?.querySelector('[data-field="clip_review_feedback_note"]')?.value || "";
  const deathId = button.dataset.id;
  await api(`/api/deaths/${deathId}/review-feedback`, {
    method: "POST",
    body: JSON.stringify({ verdict: button.dataset.verdict, note }),
  });
  setStatus(`Clip Coach feedback saved: ${button.dataset.verdict}.`);
  await loadCoach();
  if (currentMatchId) await loadReport(currentMatchId);
}

async function saveClipTrainingLabel(button) {
  const card = button.closest(".death-card");
  const deathId = button.dataset.id;
  const read = (field) => card?.querySelector(`[data-field="${field}"]`)?.value || "";
  await api(`/api/deaths/${deathId}/training-label`, {
    method: "POST",
    body: JSON.stringify({
      enemy_visible_frame: read("training_enemy_visible_frame"),
      first_contact_frame: read("training_first_contact_frame"),
      death_frame: read("training_death_frame"),
      crosshair_issue: read("training_crosshair_issue"),
      correct_mistake_label: read("training_correct_mistake_label"),
      notes: read("training_notes"),
    }),
  });
  setStatus("Coach training label saved. Future review queue ranking and prompts will use it.");
  await Promise.all([loadCoach(), loadAutomation()]);
  if (currentMatchId) await loadReport(currentMatchId);
}

async function saveDetectorAnnotation(button) {
  const card = button.closest(".death-card");
  const deathId = button.dataset.id;
  const read = (field) => card?.querySelector(`[data-field="${field}"]`)?.value || "";
  const frameSelect = card?.querySelector('[data-field="detector_frame_id"]');
  const selected = frameSelect?.selectedOptions?.[0];
  await api(`/api/deaths/${deathId}/detector-annotations`, {
    method: "POST",
    body: JSON.stringify({
      frame_id: read("detector_frame_id"),
      frame_number: selected?.dataset.frame || "",
      relative_second: selected?.dataset.rel || "",
      label: read("detector_label") || "enemy_body",
      bbox_norm: {
        x: read("detector_bbox_x"),
        y: read("detector_bbox_y"),
        w: read("detector_bbox_w"),
        h: read("detector_bbox_h"),
      },
      notes: read("detector_notes"),
    }),
  });
  setStatus("Detector training box saved locally.");
  await Promise.all([loadAutomation(), currentMatchId ? loadReport(currentMatchId) : Promise.resolve()]);
}

function renderDetectorFrameCanvas(select) {
  const card = select.closest(".death-card");
  const canvas = card?.querySelector("[data-detector-canvas]");
  const frameId = select.value || "";
  if (!canvas) return;
  if (!frameId) {
    canvas.innerHTML = '<span class="muted">Choose a keyframe to draw a box.</span>';
    return;
  }
  canvas.innerHTML = `
    <img src="/api/vision/frame/${escapeAttr(frameId)}" alt="detector annotation frame" draggable="false" />
    <div class="detector-drawn-box" hidden></div>
  `;
}

function startDetectorBoxDrag(event) {
  const canvas = event.target.closest("[data-detector-canvas]");
  if (!canvas || !canvas.querySelector("img")) return;
  const card = canvas.closest(".death-card");
  const label = card?.querySelector('[data-field="detector_label"]')?.value || "";
  if (label === "no_enemy") return;
  event.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const startX = clamp((event.clientX - rect.left) / rect.width, 0, 1);
  const startY = clamp((event.clientY - rect.top) / rect.height, 0, 1);
  const box = canvas.querySelector(".detector-drawn-box");
  box.hidden = false;
  detectorBoxDrag = { canvas, card, box, startX, startY };
  updateDetectorBoxDrag(event);
}

function updateDetectorBoxDrag(event) {
  if (!detectorBoxDrag) return;
  const { canvas, card, box, startX, startY } = detectorBoxDrag;
  const rect = canvas.getBoundingClientRect();
  const endX = clamp((event.clientX - rect.left) / rect.width, 0, 1);
  const endY = clamp((event.clientY - rect.top) / rect.height, 0, 1);
  const x = Math.min(startX, endX);
  const y = Math.min(startY, endY);
  const w = Math.abs(endX - startX);
  const h = Math.abs(endY - startY);
  box.style.left = `${x * 100}%`;
  box.style.top = `${y * 100}%`;
  box.style.width = `${w * 100}%`;
  box.style.height = `${h * 100}%`;
  setDetectorBoxInputs(card, x, y, w, h);
}

function stopDetectorBoxDrag() {
  detectorBoxDrag = null;
}

function setDetectorBoxInputs(card, x, y, w, h) {
  const set = (field, value) => {
    const input = card?.querySelector(`[data-field="${field}"]`);
    if (input) input.value = value > 0 ? value.toFixed(4) : "";
  };
  set("detector_bbox_x", x);
  set("detector_bbox_y", y);
  set("detector_bbox_w", w);
  set("detector_bbox_h", h);
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
          <div class="row compact-range">
            <label>Scan start <input data-field="scan_start_${match.id}" type="text" placeholder="10:00" /></label>
            <label>Scan end <input data-field="scan_end_${match.id}" type="text" placeholder="13:00" /></label>
            <label>Limit <input data-field="scan_limit_${match.id}" type="number" min="1" max="100" value="5" /></label>
            <button class="secondary" data-action="suggest-range" data-id="${match.id}">Find Range</button>
          </div>
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
  const matches = trends.matches || [];
  const matchCount = matches.length;
  const deathCount = matches.reduce((sum, match) => sum + Number(match.death_count || 0), 0);
  const topMistake = topCount(trends.labels || {});
  const topMap = topCount(trends.by_map || {});
  const avgDeaths = matchCount ? (deathCount / matchCount).toFixed(1) : "0";
  const recent = matches
    .slice(0, 6)
    .map((match) => {
      const top = topCount(match.labels || {});
      return `<li>#${match.match_id} ${escapeHtml(match.map)} / ${escapeHtml(match.agent)}: ${match.death_count} deaths${top ? `, ${escapeHtml(top[0])}` : ""}</li>`;
    })
    .join("");

  els.trendsView.innerHTML = `
    <section class="player-status-report compact-status-report">
      <details class="fold-panel" open>
        <summary>Overview</summary>
        <div class="status-metric-grid">
          <article class="status-metric">
            <span>Matches Parsed</span>
            <strong>${matchCount}</strong>
            <p>${deathCount} marked death(s)</p>
          </article>
          <article class="status-metric">
            <span>Avg Death Load</span>
            <strong>${avgDeaths}</strong>
            <p>marked deaths per match</p>
          </article>
          <article class="status-metric">
            <span>Top Mistake</span>
            <strong>${escapeHtml(titleCase(topMistake?.[0] || "none"))}</strong>
            <p>${topMistake ? `${topMistake[1]} occurrence(s)` : "Save labels to build this."}</p>
          </article>
          <article class="status-metric">
            <span>Worst Map</span>
            <strong>${escapeHtml(topMap?.[0] || "none")}</strong>
            <p>${topMap ? `${topMap[1]} death marker(s)` : "No map data yet."}</p>
          </article>
        </div>
      </details>
      <div class="player-graph-grid">
        ${renderFoldableStatusPanel("Mistakes Across All Matches", trends.labels || {}, Math.max(1, deathCount), "No labeled mistakes yet.", true)}
        ${renderImprovementTrendPanel(matches)}
        ${renderFoldableStatusPanel("Deaths By Map", trends.by_map || {}, Math.max(1, deathCount), "No map data yet.")}
        ${renderFoldableStatusPanel("Deaths By Agent", trends.by_agent || {}, Math.max(1, deathCount), "No agent data yet.")}
        <details class="status-panel fold-panel">
          <summary>
            <span>Recent Match Reads</span>
            <span>${matches.slice(0, 6).length}</span>
          </summary>
          <ul class="compact-list">${recent || "<li>No matches yet.</li>"}</ul>
        </details>
      </div>
    </section>
  `;
}

function renderImprovementTrendPanel(matches) {
  const recent = (matches || []).slice(0, 6);
  if (recent.length < 2) {
    return `
      <details class="status-panel fold-panel">
        <summary><span>Improvement Trend</span><strong>new</strong></summary>
        <p class="muted">Add at least two reviewed matches to compare whether repeated mistakes are improving.</p>
      </details>
    `;
  }
  const latest = recent[0];
  const previous = recent.slice(1);
  const topLabels = Object.keys(latest.labels || {}).length ? Object.keys(latest.labels || {}) : Object.keys(previous[0]?.labels || {});
  const rows = topLabels.slice(0, 5).map((label) => {
    const latestRate = ratePerDeath(Number((latest.labels || {})[label] || 0), Number(latest.death_count || 0));
    const previousRate = previous.reduce((sum, match) => sum + ratePerDeath(Number((match.labels || {})[label] || 0), Number(match.death_count || 0)), 0) / Math.max(1, previous.length);
    const delta = latestRate - previousRate;
    const direction = delta < -0.05 ? "improving" : delta > 0.05 ? "worse" : "flat";
    return `
      <div class="status-bar-row">
        <div>
          <span>${escapeHtml(titleCase(label))}</span>
          <strong>${direction} ${delta >= 0 ? "+" : ""}${Math.round(delta * 100)}%</strong>
        </div>
        <i style="width:${Math.max(6, Math.min(100, Math.round(Math.abs(delta) * 100)))}%"></i>
      </div>
    `;
  }).join("");
  return `
    <details class="status-panel fold-panel" open>
      <summary><span>Improvement Trend</span><strong>${recent.length}</strong></summary>
      <p class="muted">Latest match compared with the previous ${previous.length} reviewed match(es), normalized by death count.</p>
      <div class="status-bars">${rows || '<p class="muted">No repeated labels yet.</p>'}</div>
    </details>
  `;
}

function ratePerDeath(count, deaths) {
  return deaths > 0 ? count / deaths : 0;
}

async function loadCoach() {
  const coach = await api("/api/coach/v2");
  renderCoach(coach);
}

function renderCoach(coach) {
  latestCoachDashboard = coach;
  const profile = coach.profile || {};
  const plan = coach.plan || {};
  const goal = coach.active_goal;
  const sessions = coach.sessions || {};
  const learning = coach.suggestion_learning || {};
  const memory = coach.memory || {};
  const persistentMemory = memory.persistent || {};
  const outcomes = coach.outcomes || {};
  const coachV2 = coach.coach_v2 || {};
  const weekly = coachV2.weekly_focus || {};
  const training = coachV2.training_labels || {};
  const detector = coachV2.detector_profile || {};
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
    <details class="coach-plan fold-panel" open>
      <summary>Session</summary>
      ${renderSessionBlock(sessions)}
    </details>
    <details class="coach-profile fold-panel">
      <summary>Profile</summary>
      <label>Rank <input id="coachRank" type="text" value="${escapeAttr(profile.rank || "")}" placeholder="Gold 2" /></label>
      <label>Main agents <input id="coachAgents" type="text" value="${escapeAttr(agents)}" placeholder="Jett, Omen" /></label>
      <label>Target style <input id="coachStyle" type="text" value="${escapeAttr(profile.target_style || "")}" placeholder="More disciplined entry fights" /></label>
      <label>Coach notes <input id="coachNotes" type="text" value="${escapeAttr(profile.notes || "")}" placeholder="What should the coach remember?" /></label>
      <button data-action="save-profile">Save Profile</button>
    </details>
    <details class="coach-plan fold-panel" open>
      <summary id="suggestedFocus" data-focus="${escapeAttr(plan.focus_label || "")}">${escapeHtml(plan.summary || "Suggested Focus")}</summary>
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
    </details>
    <details class="coach-plan fold-panel">
      <summary>Personal Coach v2</summary>
      <p>${escapeHtml(weekly.target || "No weekly target yet.")}</p>
      <div class="bar-chart">${renderSkillBars(coachV2.skill_scores || {})}</div>
      <ul class="compact-list">
        ${(weekly.drills || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}
      </ul>
      <p class="muted">Memory strength ${escapeHtml(coachV2.memory_strength || 0)} · primary focus ${escapeHtml(weekly.primary_focus || "none")}</p>
    </details>
    <details class="coach-plan fold-panel" open>
      <summary>Training Dashboard</summary>
      <div class="coach-progress">
        <span>Training clips: ${Number(training.count || 0)}</span>
        <span>Frame-labeled: ${Number(training.frame_labeled || 0)}</span>
        <span>Crosshair confirmed: ${Number(training.crosshair_issue_yes || 0)}</span>
        <span>Detector: ${escapeHtml(detector.learning_state || "warming_up")}</span>
      </div>
      <p class="muted">${escapeHtml(detector.summary || "Save training labels from Clip Coach reviews to personalize visual detection.")}</p>
      <ul class="compact-list">
        ${(training.top_labels || []).slice(0, 6).map((item) => `<li>${escapeHtml(Array.isArray(item) ? item[0] : item.label)} x${escapeHtml(Array.isArray(item) ? item[1] : item.count)}</li>`).join("") || "<li>No corrected mistake labels yet.</li>"}
      </ul>
    </details>
    <details class="coach-plan fold-panel">
      <summary>Weighted Patterns</summary>
      <ul class="compact-list">
        ${(coachV2.weighted_profile || []).slice(0, 6).map((item) => `<li>${escapeHtml(item.label)}: ${escapeHtml(item.weight)}</li>`).join("") || "<li>No weighted patterns yet.</li>"}
      </ul>
    </details>
    <details class="coach-plan fold-panel">
      <summary>Personal Coach Memory</summary>
      <p>${escapeHtml(memory.summary || "No learned memory yet.")}</p>
      <div class="coach-progress">
        <span>Learned reviews: ${Number(persistentMemory.persistent_review_count || 0)}</span>
        <span>Focus: ${escapeHtml(persistentMemory.current_focus || memory.top_label || "learning")}</span>
        <span>Updated: ${escapeHtml(persistentMemory.persistent_updated_at || "not yet")}</span>
      </div>
      ${renderMemoryPatterns(persistentMemory.top_patterns || [])}
      ${renderMemoryPatterns(persistentMemory.correction_patterns || [], "Corrections")}
      ${renderMemoryPatterns(persistentMemory.perception_patterns || [], "Perception")}
      <ul class="compact-list">
        ${(memory.priorities || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}
      </ul>
      ${(persistentMemory.recent_lessons || []).length ? `
        <p class="muted">Recent learned notes</p>
        <ul class="compact-list">
          ${(persistentMemory.recent_lessons || []).slice(0, 4).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}
        </ul>
      ` : ""}
      <p class="muted">${Number(memory.analysis_count || 0)} saved analysis read(s), ${Number(memory.recent_clip_reads || 0)} recent clip read(s).</p>
    </details>
    <details class="coach-plan fold-panel">
      <summary>Measured Outcomes</summary>
      <p>${escapeHtml(outcomes.summary || "No measured outcomes yet.")}</p>
      <div class="coach-progress">
        <span>Focus: ${escapeHtml(outcomes.focus_label || "none")}</span>
        <span>Crosshair avg: ${escapeHtml(outcomes.crosshair_average ?? "n/a")}</span>
        <span>Detector accepted: ${escapeHtml((outcomes.detector_feedback || {}).accepted || 0)}</span>
        <span>Detector rejected: ${escapeHtml((outcomes.detector_feedback || {}).rejected || 0)}</span>
      </div>
    </details>
  `;
}

function renderMemoryPatterns(patterns, title = "") {
  if (!patterns.length) return "";
  return `
    ${title ? `<p class="muted">${escapeHtml(title)}</p>` : ""}
    <div class="coach-progress">
      ${patterns.slice(0, 4).map((item) => `<span>${escapeHtml(item.label)} x${Number(item.count || 0)}</span>`).join("")}
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
  const memoryStrip = renderCoachMemoryStrip(latestCoachDashboard);
  const playerStatus = renderPlayerStatusReport(report, coachMoments);
  const matchThemes = renderMatchThemes(report.match_themes || {});
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
    ${memoryStrip}
    ${playerStatus}
    ${matchThemes}
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

function renderCoachMemoryStrip(coach) {
  if (!coach) {
    return "";
  }
  const plan = coach.plan || {};
  const coachV2 = coach.coach_v2 || {};
  const weekly = coachV2.weekly_focus || {};
  const memory = coach.memory || {};
  const focus = weekly.primary_focus || plan.focus_label || "review discipline";
  const target = weekly.target || plan.in_game_rule || memory.summary || "Build memory by reviewing deaths with Clip Coach.";
  const weighted = (coachV2.weighted_profile || []).slice(0, 3).map((item) => `<span class="tag">${escapeHtml(item.label)}</span>`).join("");
  return `
    <details class="coach-memory-strip">
      <summary>
        <span>
          <span class="muted">Coach Memory</span>
          <strong>${escapeHtml(focus)}</strong>
        </span>
        <span class="memory-tags">${weighted || '<span class="tag">learning</span>'}</span>
      </summary>
      <div class="coach-memory-body">
        <p>${escapeHtml(target)}</p>
        <p class="muted">Used as context for draft reviews. Confirmed markers are changed only when you save them.</p>
      </div>
    </details>
  `;
}

function renderPlayerStatusReport(report, coachMoments = []) {
  const deaths = report.deaths || [];
  const suggestions = report.suggestions || [];
  const deathCount = deaths.length;
  const reviewedCount = deaths.filter((death) => death.advice || death.local_ai_review).length;
  const localAiCount = deaths.filter((death) => death.local_ai_review).length;
  const roundKnownCount = deaths.filter((death) => death.round_number).length;
  const roundDisplayCount = deaths.filter((death) => death.round_number || death.display_round_number).length;
  const causeCounts = collectDeathCauseCounts(deaths);
  const perceptionCounts = collectPerceptionCounts(deaths);
  const coachingIssueCounts = collectCoachingIssueCounts(deaths);
  const contextCounts = collectContextCounts(deaths);
  const reviewQualityCounts = collectReviewQualityCounts(deaths);
  const labelCounts = collectStoredLabelCounts(report);
  const phaseCounts = collectPhaseCounts(deaths);
  const roundCounts = collectRoundCounts(deaths);
  const topCause = topCount(causeCounts);
  const topStored = topCount(labelCounts);
  const reviewPct = percent(reviewedCount, deathCount);
  const roundPct = percent(roundDisplayCount, deathCount);
  const confirmedRoundText = `${roundKnownCount}/${deathCount}`;
  const roundHelp = roundKnownCount === deathCount
    ? "All death markers have confirmed round numbers."
    : `${roundDisplayCount - roundKnownCount} marker(s) are using timeline/spacing estimates until OCR or manual save confirms them.`;

  return `
    <section class="player-status-report">
      <div class="review-head">
        <h3>Player Status</h3>
        <span class="muted">Built from parsed markers, advice, Clip Coach reads, and VOD moments.</span>
      </div>
      <div class="status-metric-grid">
        <article class="status-metric">
          <span>Deaths Marked</span>
          <strong>${deathCount}</strong>
          <p>${suggestions.length} pending suggestion(s)</p>
        </article>
        <article class="status-metric">
          <span>Review Coverage</span>
          <strong>${reviewPct}%</strong>
          <p>${reviewedCount}/${deathCount || 0} have advice or Clip Coach</p>
          ${renderMiniProgress(reviewPct)}
        </article>
        <article class="status-metric">
          <span>Primary Cause</span>
          <strong>${escapeHtml(titleCase(topCause?.[0] || topStored?.[0] || "not enough data"))}</strong>
          <p>${topCause ? `${topCause[1]} supporting read(s)` : "Run Clip Coach or save labels."}</p>
        </article>
        <article class="status-metric">
          <span>Round Coverage</span>
          <strong>${roundPct}%</strong>
          <p>${confirmedRoundText} confirmed. ${roundHelp}</p>
          ${renderMiniProgress(roundPct)}
        </article>
      </div>
      <div class="player-graph-grid">
        ${renderStatusPanel("Mistakes From Saved Markers", labelCounts, deathCount, "Save corrected labels to improve this chart.")}
        ${renderStatusPanel("Death Causes From Coach Reads", causeCounts, Math.max(1, reviewedCount + localAiCount), "Generate advice or Clip Coach reviews for clearer causes.")}
        ${renderStatusPanel("Clip Perception Reads", perceptionCounts, Math.max(1, localAiCount), "Run Clip Coach to track enemy visibility, crosshair level, and peek type.")}
        ${renderStatusPanel("Coaching Issue Types", coachingIssueCounts, Math.max(1, localAiCount), "Structured Clip Coach reviews will separate utility, crosshair, positioning, and mechanics.")}
        ${renderStatusPanel("Map / Agent / Weapon Context", contextCounts, deathCount, "Run Clip Coach or save context to identify repeated map, agent, and weapon patterns.")}
        ${renderStatusPanel("Review Evidence Quality", reviewQualityCounts, Math.max(1, localAiCount), "Clip Coach reviews will be scored by visible evidence and segment coverage.")}
        ${renderStatusPanel("Deaths By Round Phase", phaseCounts, deathCount, "Round phase uses reconstructed round timing when available.")}
        ${renderStatusPanel("Deaths By Round", roundCounts, deathCount, "Estimated rounds are marked until scoreboard OCR/manual save confirms them.")}
      </div>
      ${coachMoments.length ? `<p class="muted">${coachMoments.length} whole-VOD coach moment(s) found outside death markers.</p>` : ""}
    </section>
  `;
}

function renderMatchThemes(themes) {
  if (!themes || !Object.keys(themes).length) return "";
  const chips = (themes.top_mistakes || []).map((item) => `<span class="tag">${escapeHtml(item.label)} ${escapeHtml(item.count)}</span>`).join("");
  const dimensions = (themes.top_dimensions || []).map((item) => `<span class="tag">${escapeHtml(item.label)} ${escapeHtml(item.count)}</span>`).join("");
  const context = (themes.context_patterns || []).slice(0, 4).map((item) => `<li>${escapeHtml(item.label)} · ${escapeHtml(item.count)}</li>`).join("");
  const rounds = (themes.round_patterns || []).slice(0, 4).map((item) => `<li>${escapeHtml(item.label)} · ${escapeHtml(item.count)}</li>`).join("");
  const practice = (themes.practice_plan || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const evidence = (themes.evidence_examples || []).slice(0, 4).map((item) => `
    <li>
      ${item.timestamp !== undefined && item.timestamp !== null ? `<button class="timeline-jump" data-action="jump" data-ts="${escapeAttr(item.timestamp)}">${escapeHtml(formatTs(item.timestamp))}</button>` : ""}
      <strong>${escapeHtml(item.event || "evidence")}</strong>
      <span>${escapeHtml(shortenText(item.evidence || "", 180))}</span>
    </li>
  `).join("");
  return `
    <section class="match-themes-panel">
      <div class="review-head">
        <h3>Match Themes</h3>
        <span class="muted">${Math.round(Number(themes.confidence || 0) * 100)}%</span>
      </div>
      <p>${escapeHtml(themes.summary || "")}</p>
      <div class="tag-row">${chips || '<span class="tag">learning</span>'} ${dimensions}</div>
      <div class="player-graph-grid">
        <article class="status-panel">
          <div class="analysis-head"><strong>Context Pattern</strong><span>${(themes.context_patterns || []).length}</span></div>
          <ul class="compact-list">${context || "<li>Run Clip Coach or save context for map/agent/weapon patterns.</li>"}</ul>
        </article>
        <article class="status-panel">
          <div class="analysis-head"><strong>Round Pattern</strong><span>${(themes.round_patterns || []).length}</span></div>
          <ul class="compact-list">${rounds || "<li>Round pattern needs OCR/manual round data.</li>"}</ul>
        </article>
      </div>
      <details class="advanced-actions" open>
        <summary>Next Practice Plan</summary>
        <ul class="compact-list">${practice || "<li>Review more clips to build a practice plan.</li>"}</ul>
      </details>
      ${evidence ? `<details class="advanced-actions"><summary>Evidence Examples</summary><ul class="compact-list">${evidence}</ul></details>` : ""}
    </section>
  `;
}

function collectContextCounts(deaths) {
  const counts = {};
  for (const death of deaths || []) {
    const fields = death.match_context?.fields || {};
    if (fields.map?.value) addCount(counts, `map ${fields.map.value}`, 1);
    if (fields.agent?.value) addCount(counts, `agent ${fields.agent.value}`, 1);
    if (fields.weapon?.value) addCount(counts, `weapon ${fields.weapon.value}`, 1);
  }
  return counts;
}

function collectReviewQualityCounts(deaths) {
  const counts = {};
  for (const death of deaths || []) {
    const quality = death.local_ai_review?.payload?.review_quality || {};
    if (!death.local_ai_review) continue;
    addCount(counts, quality.summary || "unscored", 1);
  }
  return counts;
}

function collectPerceptionCounts(deaths) {
  const counts = {};
  for (const death of deaths || []) {
    const perception = death.local_ai_review?.payload?.perception || {};
    if (perception.enemy_seen && perception.enemy_seen !== "unknown") addCount(counts, `enemy ${perception.enemy_seen}`, 1);
    if (perception.crosshair_level && perception.crosshair_level !== "unknown") addCount(counts, `crosshair ${perception.crosshair_level}`, 1);
    if (perception.peek_type && perception.peek_type !== "unknown") addCount(counts, `peek ${perception.peek_type}`, 1);
    if (perception.utility_seen && perception.utility_seen !== "unknown") addCount(counts, `utility ${perception.utility_seen}`, 1);
  }
  return counts;
}

function collectCoachingIssueCounts(deaths) {
  const counts = {};
  for (const death of deaths || []) {
    const payload = death.local_ai_review?.payload || {};
    const coaching = payload.coaching || {};
    for (const [key, label] of [
      ["utility_issue", "utility"],
      ["crosshair_issue", "crosshair"],
      ["positioning_issue", "positioning"],
      ["mechanical_issue", "mechanics"],
    ]) {
      const value = String(coaching[key] || payload[key] || "").toLowerCase();
      if (value && !value.includes("no") && !value.includes("none") && !value.includes("insufficient")) addCount(counts, label, 1);
    }
  }
  return counts;
}

function collectStoredLabelCounts(report) {
  const counts = {};
  for (const [label, count] of Object.entries(report.label_counts || {})) {
    addCount(counts, label, Number(count || 0));
  }
  return counts;
}

function collectDeathCauseCounts(deaths) {
  const counts = {};
  for (const death of deaths || []) {
    const seen = new Set();
    for (const label of death.mistake_labels || []) {
      if (label && label !== "needs manual review") seen.add(label);
    }
    if (death.advice?.primary_mistake) seen.add(death.advice.primary_mistake);
    const localPayload = death.local_ai_review?.payload || {};
    for (const label of localPayload.labels || []) {
      if (label) seen.add(label);
    }
    for (const label of seen) addCount(counts, label, 1);
  }
  return counts;
}

function collectPhaseCounts(deaths) {
  const counts = {};
  for (const death of deaths || []) {
    addCount(counts, death.round_phase || "unknown", 1);
  }
  return counts;
}

function collectRoundCounts(deaths) {
  const counts = {};
  for (const death of deaths || []) {
    const number = death.round_number || death.display_round_number;
    const source = death.round_number ? "" : death.display_round_number ? " est." : "";
    addCount(counts, number ? `R${number}${source}` : "unknown", 1);
  }
  return counts;
}

function renderStatusPanel(title, counts, total, emptyText) {
  const bars = renderStatusBars(counts, total);
  return `
    <article class="status-panel">
      <div class="analysis-head">
        <strong>${escapeHtml(title)}</strong>
        <span>${Object.keys(counts || {}).length}</span>
      </div>
      ${bars || `<p class="muted">${escapeHtml(emptyText)}</p>`}
    </article>
  `;
}

function renderFoldableStatusPanel(title, counts, total, emptyText, open = false) {
  const bars = renderStatusBars(counts, total);
  return `
    <details class="status-panel fold-panel" ${open ? "open" : ""}>
      <summary>
        <span>${escapeHtml(title)}</span>
        <strong>${Object.keys(counts || {}).length}</strong>
      </summary>
      ${bars || `<p class="muted">${escapeHtml(emptyText)}</p>`}
    </details>
  `;
}

function renderStatusBars(counts, total, limit = 6) {
  const entries = Object.entries(counts || {})
    .filter(([, count]) => Number(count) > 0)
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, limit);
  if (!entries.length) return "";
  const divisor = Math.max(1, Number(total || 0), ...entries.map(([, count]) => Number(count || 0)));
  return `
    <div class="status-bars">
      ${entries.map(([label, count]) => {
        const width = Math.max(6, Math.round((Number(count || 0) / divisor) * 100));
        return `
          <div class="status-bar-row">
            <div>
              <span>${escapeHtml(titleCase(label))}</span>
              <strong>${escapeHtml(count)}</strong>
            </div>
            <i style="width:${width}%"></i>
          </div>
        `;
      }).join("")}
    </div>
  `;
}

function renderMiniProgress(value) {
  const width = Math.max(0, Math.min(100, Number(value || 0)));
  return `<div class="mini-progress"><span style="width:${width}%"></span></div>`;
}

function addCount(counts, key, amount = 1) {
  const label = String(key || "").trim().toLowerCase();
  if (!label) return;
  counts[label] = (counts[label] || 0) + Number(amount || 0);
}

function topCount(counts) {
  return Object.entries(counts || {}).sort((a, b) => Number(b[1]) - Number(a[1]))[0] || null;
}

function percent(part, total) {
  if (!total) return 0;
  return Math.round((Number(part || 0) / Number(total)) * 100);
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
    <section>
      <h3>OCR Health Check</h3>
      <div class="add-death">
        <label>Video time <input id="ocrHealthTimestamp" type="text" placeholder="blank = current player time" /></label>
        <label>Regions <input id="ocrHealthRegions" type="text" value="killfeed,combat_report,hud_top,hud_bottom,round_score" /></label>
        <button class="secondary" data-action="ocr-health" data-id="${matchId}">Run OCR Health</button>
      </div>
      <div id="ocrHealthResult" class="ocr-health-result"></div>
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
          <h3>Job Progress</h3>
          <p>${escapeHtml(job.name)} · ${escapeHtml(job.status)}</p>
        </div>
        <strong>${progress}%</strong>
      </div>
      <div class="progress-track" aria-label="Job progress">
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
  const targetNames = [`Auto coach match #${matchId}`, `Full VOD coach match #${matchId}`, `Find Deaths match #${matchId}`];
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
        <button class="secondary" data-action="clear-pending-suggestions" data-id="${currentMatchId}">Clear Unreviewed</button>
      </div>
      <div class="suggestion-list">${cards}</div>
    </section>
  `;
}

function renderGuidedCoach(row, matchId) {
  const coach = (row && row.payload) || row || null;
  if (!coach) {
    return `
      <details class="guided-coach empty fold-panel">
        <summary>Coach Mode</summary>
        <div>
          <p class="muted">Generate a short review order after markers are confirmed.</p>
        </div>
        <button data-action="guided-coach" data-id="${matchId}">Coach This Match</button>
      </details>
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
    <details class="guided-coach fold-panel">
      <summary>
        <span>Coach Mode</span>
        <strong>${Math.round(Number(coach.confidence || 0) * 100)}%</strong>
      </summary>
      <p>${escapeHtml(coach.summary || "")}</p>
      ${coach.coach_read ? `<p><strong>Read:</strong> ${escapeHtml(coach.coach_read)}</p>` : ""}
      ${coach.between_round_rule ? `<p><strong>Round rule:</strong> ${escapeHtml(coach.between_round_rule)}</p>` : ""}
      <ol class="coach-steps">${items}</ol>
      <details class="advanced-actions">
        <summary>Practice plan</summary>
        <ul class="compact-list">${homework}</ul>
      </details>
      <button class="secondary" data-action="guided-coach" data-id="${matchId}">Refresh Coach Read</button>
    </details>
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

function renderMatchContextPanel(context, death) {
  const fields = context.fields || {};
  const field = (key) => fields[key] || { value: "", known: false, source: "unknown", confidence: 0 };
  const chips = ["map", "agent", "round", "side", "weapon", "location", "spike_state", "team_counts"].map((key) => {
    const item = field(key);
    const label = key.replaceAll("_", " ");
    const value = item.known ? item.value : "unknown";
    const state = item.known ? "known" : "unknown";
    return `
      <span class="context-chip ${state}" title="${escapeAttr(item.source || "unknown")} · ${Math.round(Number(item.confidence || 0) * 100)}%">
        <b>${escapeHtml(label)}</b>
        ${escapeHtml(value)}
      </span>
    `;
  }).join("");
  const manual = context.manual_correction || {};
  const extraction = renderContextExtraction(context.context_extraction || {});
  return `
    <details class="match-context-panel ${context.ready_for_knowledge ? "ready" : "needs-work"}" ${context.ready_for_knowledge ? "" : "open"}>
      <summary>
        <span>Match Context</span>
        <strong>${escapeHtml(context.known_count || 0)}/${escapeHtml(context.total_count || 0)} known</strong>
      </summary>
      <p class="muted">${escapeHtml(context.summary || "Correct map, agent, and round to make knowledge retrieval specific.")}</p>
      <div class="context-chip-row">${chips}</div>
      ${extraction}
      <details class="context-editor-panel" ${context.ready_for_knowledge ? "" : "open"}>
        <summary>Correct context</summary>
        <div class="context-editor">
          <label>Map <input data-field="context_map" type="text" value="${escapeAttr(field("map").value || manual.map || "")}" placeholder="Ascent" /></label>
          <label>Agent <input data-field="context_agent" type="text" value="${escapeAttr(field("agent").value || manual.agent || "")}" placeholder="Jett" /></label>
          <label>Round <input data-field="context_round_number" type="number" min="1" max="30" value="${escapeAttr(manual.round_number || death.round_number || death.display_round_number || "")}" /></label>
          <label>Side <input data-field="context_side" type="text" value="${escapeAttr(field("side").value || manual.side || "")}" placeholder="attack / defense" /></label>
          <label>Weapon <input data-field="context_weapon" type="text" value="${escapeAttr(field("weapon").value || manual.weapon || "")}" placeholder="Vandal" /></label>
          <label>Location <input data-field="context_location" type="text" value="${escapeAttr(field("location").value || manual.location || "")}" placeholder="A Main, Mid, B Site" /></label>
          <label>Spike <input data-field="context_spike_state" type="text" value="${escapeAttr(field("spike_state").value || manual.spike_state || "")}" placeholder="pre-plant / planted / retake" /></label>
          <label>Alive <input data-field="context_team_counts" type="text" value="${escapeAttr(field("team_counts").value || manual.team_counts || "")}" placeholder="3v4" /></label>
          <label class="wide">Context note <input data-field="context_notes" type="text" value="${escapeAttr(manual.notes || "")}" placeholder="Any correction the coach should trust" /></label>
          <button class="secondary" data-action="save-context" data-id="${death.id}">Save Context</button>
        </div>
      </details>
    </details>
  `;
}

function renderContextExtraction(extraction) {
  if (!extraction || !Object.keys(extraction).length) return "";
  const resolved = extraction.resolved || {};
  const auto = extraction.auto_corrections || {};
  const rows = ["map", "agent", "round_number", "side", "weapon", "location", "spike_state", "team_counts"].map((key) => {
    const item = resolved[key] || {};
    const value = item.value || "";
    if (!value) return "";
    const applied = Object.prototype.hasOwnProperty.call(auto, key) ? "applied" : item.blocked_by_manual ? "manual kept" : item.status || "candidate";
    return `<li><strong>${escapeHtml(key.replaceAll("_", " "))}:</strong> ${escapeHtml(value)} · ${Math.round(Number(item.confidence || 0) * 100)}% · ${escapeHtml(applied)}${item.evidence ? ` <span class="muted">(${escapeHtml(shortenText(item.evidence, 120))})</span>` : ""}</li>`;
  }).filter(Boolean).join("");
  const visible = (extraction.visible_text || []).slice(0, 5).map((item) => `<li>${escapeHtml(shortenText(item, 140))}</li>`).join("");
  if (!rows && !visible) return "";
  return `
    <details class="context-extraction">
      <summary>
        <span>Auto extraction</span>
        <strong>${escapeHtml(extraction.status || "captured")}</strong>
      </summary>
      <p class="muted">${escapeHtml(extraction.summary || "KB-constrained context pass completed.")}</p>
      ${rows ? `<ul class="compact-list">${rows}</ul>` : ""}
      ${visible ? `<p class="muted">Visible text</p><ul class="compact-list">${visible}</ul>` : ""}
    </details>
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
  const localAi = renderLocalAiReview(death.local_ai_review, death);
  const annotations = renderAnnotations(death.annotations || []);
  const contextPanel = renderMatchContextPanel(death.match_context || {}, death);
  const lifecycle = renderMarkerLifecycle(death.marker_lifecycle || {});
  return `
    <article class="death-card" data-death-id="${death.id}">
      <div class="death-card-header">
        <div class="death-card-title">
          <strong>${escapeHtml(formatDeathTime(death))}</strong>
          ${renderTags(death.mistake_labels || [])}
          ${lifecycle}
        </div>
        <button class="secondary" data-action="jump" data-ts="${death.timestamp || 0}">Jump</button>
        <button data-action="coach-clip" data-id="${death.id}">${death.advice || death.local_ai_review ? "Refresh Coach" : "Coach This Clip"}</button>
      </div>
      ${death.notes ? `<p class="death-note">${escapeHtml(shortenText(death.notes, 180))}</p>` : ""}
      <details class="evidence-panel">
        <summary>Evidence and gaps</summary>
        <div class="evidence-body" data-evidence-target="${death.id}">
          <p class="muted">${escapeHtml((death.marker_lifecycle || {}).source_detail || "Load evidence receipts for this marker.")}</p>
          <button class="secondary" data-action="load-evidence" data-id="${death.id}">Load Evidence</button>
        </div>
      </details>
      ${contextPanel}
      ${advice}
      <details class="advanced-actions">
        <summary>Edit marker and advanced tools</summary>
        <div class="row">
          <button class="secondary" data-action="advice" data-id="${death.id}">Normal Advice Only</button>
          <button class="secondary" data-action="local-ai-review" data-id="${death.id}">Clip Coach Only</button>
          ${clip}
        </div>
        <details class="advanced-actions">
          <summary>Legacy diagnostics</summary>
          <div class="row">
            <button class="secondary" data-action="vision" data-id="${death.id}">Analyze Clip</button>
            <button class="secondary" data-action="keyframes" data-id="${death.id}">Keyframes</button>
            <button class="secondary" data-action="understand" data-id="${death.id}">Understand</button>
            <button class="secondary" data-action="gameplay" data-id="${death.id}">Gameplay</button>
            <button class="secondary" data-action="ai-review" data-id="${death.id}">AI Review</button>
            <button class="secondary" data-action="benchmark-true-positive" data-id="${death.id}" data-match="${death.match_id}" data-ts="${death.timestamp || 0}">True Positive</button>
          </div>
        </details>
        ${vision}
        ${keyframes}
        ${understanding}
        ${localAi}
        ${renderDetectorAnnotationForm(death)}
        ${annotations}
        <div class="row">
          <button class="secondary" data-action="loop-death" data-ts="${death.timestamp || 0}">Loop Clip</button>
        </div>
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

function renderMarkerLifecycle(lifecycle) {
  if (!lifecycle || !Object.keys(lifecycle).length) {
    return "";
  }
  const trained = lifecycle.trained ? "trained" : "not trained";
  const source = lifecycle.source || "manual";
  return `<span class="marker-badge" title="${escapeAttr(lifecycle.source_detail || "")}">${escapeHtml(source)} · ${escapeHtml(trained)}</span>`;
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

function renderDetectorAnnotationForm(death) {
  const frameOptions = (((death.keyframes || {}).payload || {}).frames || []).map((item) => (
    `<option value="${escapeAttr(item.frame_id || "")}" data-frame="${escapeAttr(item.sequence_index || item.index || "")}" data-rel="${escapeAttr(item.relative_second ?? "")}">${escapeHtml(`${item.sequence_index || item.index || "frame"} ${item.role || ""}`)}</option>`
  )).join("");
  return `
    <details class="training-label-card">
      <summary>
        <span>Enemy Detector Training Box</span>
        <strong>YOLO</strong>
      </summary>
      <p class="muted">Save boxes only when the frame visibly contains the object. Coordinates are normalized 0-1 across the full frame.</p>
      <div class="training-label-grid">
        <label class="wide">Frame
          <select data-field="detector_frame_id">
            <option value="">choose keyframe</option>
            ${frameOptions}
          </select>
        </label>
        <label>Label
          <select data-field="detector_label">
            ${["enemy_body", "enemy_head", "teammate", "weapon", "ability_effect", "no_enemy"].map((item) => `<option value="${item}">${item}</option>`).join("")}
          </select>
        </label>
        <label>X <input data-field="detector_bbox_x" type="number" min="0" max="1" step="0.001" placeholder="0.45" /></label>
        <label>Y <input data-field="detector_bbox_y" type="number" min="0" max="1" step="0.001" placeholder="0.30" /></label>
        <label>W <input data-field="detector_bbox_w" type="number" min="0" max="1" step="0.001" placeholder="0.08" /></label>
        <label>H <input data-field="detector_bbox_h" type="number" min="0" max="1" step="0.001" placeholder="0.16" /></label>
        <label class="wide">Notes <input data-field="detector_notes" type="text" placeholder="enemy shoulder visible, head box, false red UI, etc." /></label>
        <div class="detector-box-editor wide">
          <div class="detector-frame-canvas" data-detector-canvas>
            <span class="muted">Choose a keyframe to draw a box.</span>
          </div>
          <p class="muted">Drag from top-left to bottom-right. For no_enemy, leave the box empty.</p>
        </div>
        <button class="secondary" data-action="save-detector-annotation" data-id="${death.id || ""}">Save Detector Box</button>
      </div>
    </details>
  `;
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

function renderLocalAiReview(row, death = {}) {
  if (!row || !row.payload) {
    return "";
  }
  const payload = row.payload;
  const feedback = death.clip_review_feedback?.payload || {};
  const trainingLabel = death.clip_training_label?.payload || {};
  const perception = payload.perception || {};
  const coaching = payload.coaching || {};
  const quality = payload.review_quality || {};
  const labels = (payload.labels || []).map((label) => `<span class="tag">${escapeHtml(label)}</span>`).join(" ");
  const evidence = (payload.visible_evidence || payload.evidence || [])
    .filter(Boolean)
    .map((item) => `<li>${escapeHtml(item)}</li>`)
    .join("");
  const memoryFocus = latestCoachDashboard?.coach_v2?.weekly_focus?.primary_focus || latestCoachDashboard?.plan?.focus_label || "";
  const draftLabels = Array.isArray(payload.labels) ? payload.labels.join(", ") : "";
  const draftNote = [
    payload.summary ? `Clip Coach: ${payload.summary}` : "",
    payload.better_play ? `Better play: ${payload.better_play}` : "",
    memoryFocus ? `Coach memory focus: ${memoryFocus}` : "",
  ].filter(Boolean).join(" | ");
  return `
    <div class="analysis-card">
      <div class="analysis-head">
        <strong>Clip Coach Review</strong>
        <span>${escapeHtml(quality.summary || payload.status || "captured")} · ${Math.round(Number(payload.confidence || 0) * 100)}%</span>
      </div>
      <p>${escapeHtml(payload.summary || "")}</p>
      ${memoryFocus ? `<p><strong>Coach memory context:</strong> ${escapeHtml(memoryFocus)}</p>` : ""}
      ${renderCoachingRead(coaching, payload)}
      ${payload.better_play ? `<p><strong>Better play:</strong> ${escapeHtml(payload.better_play)}</p>` : ""}
      ${payload.drill ? `<p><strong>Drill:</strong> ${escapeHtml(payload.drill)}</p>` : ""}
      <div>${labels}</div>
      ${renderEvidenceTimeline(payload.evidence_timeline || [])}
      ${renderClaimConfidence(payload.claim_confidence || {})}
      ${renderMultiPassReview(payload.multi_pass || {}, payload.multi_pass_reviews || [])}
      <details class="advanced-actions">
        <summary>Segment reads and raw evidence</summary>
        ${renderReviewDiagnostics(payload.review_diagnostics || {}, payload.fallback_support || {})}
        ${renderReviewPipeline(payload.review_pipeline || {})}
        ${renderDeterministicSignals(payload.deterministic_signals || {})}
        ${renderSegmentReviews(payload.segment_reviews || [])}
        ${renderPerceptionRead(perception)}
        ${evidence ? `<p><strong>Visible evidence</strong></p><ul class="compact-list">${evidence}</ul>` : ""}
        ${payload.extracted_text ? `<p><strong>Extracted text:</strong> ${escapeHtml(payload.extracted_text)}</p>` : ""}
        ${payload.scoreboard ? `<p><strong>Scoreboard:</strong> ${escapeHtml(JSON.stringify(payload.scoreboard))}</p>` : ""}
      </details>
      <div class="row local-ai-actions">
        <button class="secondary" data-action="fill-review-draft" data-id="${death.id || ""}" data-labels="${escapeAttr(draftLabels)}" data-note="${escapeAttr(draftNote)}">Fill Review Draft</button>
        <input data-field="clip_review_feedback_note" type="text" value="${escapeAttr(feedback.note || "")}" placeholder="Optional coach feedback" />
        <button class="secondary" data-action="clip-review-feedback" data-id="${death.id || ""}" data-verdict="useful">Useful</button>
        <button class="secondary" data-action="clip-review-feedback" data-id="${death.id || ""}" data-verdict="accurate">Accurate</button>
        <button class="danger" data-action="clip-review-feedback" data-id="${death.id || ""}" data-verdict="wrong">Wrong</button>
        ${feedback.verdict ? `<span class="muted">Marked ${escapeHtml(feedback.verdict)}</span>` : ""}
      </div>
      ${renderClipTrainingLabel(death, trainingLabel)}
    </div>
  `;
}

function renderReviewDiagnostics(diagnostics, support) {
  if (!Object.keys(diagnostics || {}).length && !Object.keys(support || {}).length) return "";
  const warnings = (diagnostics.warnings || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const supportEvidence = (support.visible_evidence || []).slice(0, 5).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  return `
    <details class="multi-pass-review">
      <summary>
        <span>Model Diagnostics</span>
        <strong>${diagnostics.model_weak ? "weak" : "ok"}</strong>
      </summary>
      <ul class="compact-list">
        <li><strong>Frames:</strong> prepared ${escapeHtml(diagnostics.prepared_frames ?? "")}, sent ${escapeHtml(diagnostics.sent_frames ?? "")}</li>
        <li><strong>Sent range:</strong> ${escapeHtml(diagnostics.sent_frame_range || "unknown")}</li>
        ${diagnostics.trimmed ? "<li>Prompt or frames were trimmed for context budget.</li>" : ""}
        ${warnings}
      </ul>
      ${support.summary ? `<p><strong>Local detector support:</strong> ${escapeHtml(support.summary)}</p>` : ""}
      ${supportEvidence ? `<ul class="compact-list">${supportEvidence}</ul>` : ""}
    </details>
  `;
}

function renderMultiPassReview(meta, reviews) {
  if (!reviews.length && !meta.enabled) return "";
  const rows = (reviews || []).map((review) => `
    <li>
      <strong>${escapeHtml(review.pass_label || review.pass_id || "vision pass")}</strong>
      <span class="muted">${escapeHtml(review.frame_range || "")} · ${Math.round(Number(review.confidence || 0) * 100)}%</span>
      <p>${escapeHtml(shortenText(review.summary || "", 220))}</p>
    </li>
  `).join("");
  return `
    <details class="multi-pass-review">
      <summary>
        <span>Local Vision Multi-Pass</span>
        <strong>${escapeHtml(meta.pass_count || reviews.length || 0)} pass(es)</strong>
      </summary>
      <ul class="compact-list">${rows || "<li>Pass metadata saved, no per-pass text returned.</li>"}</ul>
    </details>
  `;
}

function renderClipTrainingLabel(death, label) {
  const suggestedContact = suggestedContactFrame(death.local_ai_review?.payload || {});
  return `
    <details class="training-label-card">
      <summary>
        <span>Teach Coach From This Clip</span>
        <strong>${label.created_at ? "saved" : "optional"}</strong>
      </summary>
      <p class="muted">Use this when the model missed the enemy, first contact, death frame, or mistake type. It stays local in the training-label history.</p>
      <div class="training-label-grid">
        <label>Enemy frame <input data-field="training_enemy_visible_frame" type="number" min="0" step="1" value="${escapeAttr(label.enemy_visible_frame ?? "")}" placeholder="frame #" /></label>
        <label>Contact frame <input data-field="training_first_contact_frame" type="number" min="0" step="1" value="${escapeAttr(label.first_contact_frame ?? suggestedContact ?? "")}" placeholder="frame #" /></label>
        <label>Death frame <input data-field="training_death_frame" type="number" min="0" step="1" value="${escapeAttr(label.death_frame ?? "")}" placeholder="frame #" /></label>
        <label>Crosshair issue
          <select data-field="training_crosshair_issue">
            <option value="" ${label.crosshair_issue === undefined || label.crosshair_issue === null ? "selected" : ""}>unknown</option>
            <option value="true" ${label.crosshair_issue === true ? "selected" : ""}>yes</option>
            <option value="false" ${label.crosshair_issue === false ? "selected" : ""}>no</option>
          </select>
        </label>
        <label class="wide">Correct mistake <input data-field="training_correct_mistake_label" type="text" value="${escapeAttr(label.correct_mistake_label || "")}" placeholder="crosshair too low, dry swing, late trade..." /></label>
        <label class="wide">What should the coach learn? <input data-field="training_notes" type="text" value="${escapeAttr(label.notes || "")}" placeholder="Specific correction for this clip" /></label>
        <button class="secondary" data-action="save-training-label" data-id="${death.id || ""}">Save Training Label</button>
        ${label.created_at ? `<span class="muted">Last saved ${escapeHtml(label.created_at)}</span>` : ""}
      </div>
    </details>
  `;
}

function suggestedContactFrame(payload) {
  const timeline = payload.deterministic_signals?.visual?.timeline || [];
  const contact = timeline.find((item) => String(item.class || "").includes("contact"));
  return contact?.frame ?? "";
}

function renderDeterministicSignals(signals) {
  const visual = signals.visual || {};
  const ocr = signals.ocr || {};
  if (!Object.keys(visual).length && !Object.keys(ocr).length) return "";
  const classifier = visual.frame_classifier || {};
  const contact = visual.crosshair_to_contact || {};
  const detector = visual.detector_profile || {};
  const structuredOcr = ocr.structured || {};
  const parsedHud = structuredOcr.parsed || structuredOcr.parsed_hud || structuredOcr.hints || {};
  const rows = [
    visual.summary ? `<li><strong>Visual:</strong> ${escapeHtml(visual.summary)}</li>` : "",
    detector.summary ? `<li><strong>Adaptive detector:</strong> ${escapeHtml(detector.summary)}</li>` : "",
    classifier.summary ? `<li><strong>Frame classes:</strong> ${escapeHtml(classifier.summary)}${renderClassCounts(classifier.counts || {})}</li>` : "",
    contact.summary ? `<li><strong>Aim vs contact:</strong> ${escapeHtml(contact.summary)}</li>` : "",
    visual.crosshair_score?.summary ? `<li><strong>Crosshair:</strong> ${escapeHtml(visual.crosshair_score.summary)}</li>` : "",
    visual.movement_read?.summary ? `<li><strong>Movement:</strong> ${escapeHtml(visual.movement_read.summary)}</li>` : "",
    visual.minimap_read?.summary ? `<li><strong>Minimap:</strong> ${escapeHtml(visual.minimap_read.summary)}</li>` : "",
    ocr.summary ? `<li><strong>OCR:</strong> ${escapeHtml(ocr.summary)}</li>` : "",
    renderParsedHudRow(parsedHud),
  ].filter(Boolean).join("");
  return rows ? `<p><strong>Local detectors</strong></p><ul class="compact-list">${rows}</ul>${renderClipSignalTimeline(visual.timeline || [])}` : "";
}

function renderClipSignalTimeline(timeline) {
  const rows = (timeline || []).slice(0, 90);
  if (!rows.length) return "";
  const firstTs = rows.find((item) => item.timestamp !== undefined)?.timestamp || 0;
  const lastTs = rows.slice().reverse().find((item) => item.timestamp !== undefined)?.timestamp || firstTs + 1;
  const span = Math.max(1, Number(lastTs) - Number(firstTs));
  const markers = rows.map((item) => {
    const left = Math.max(0, Math.min(100, ((Number(item.timestamp || firstTs) - Number(firstTs)) / span) * 100));
    const cls = frameClassCss(item.class);
    const title = `${item.class || "frame"} · frame ${item.frame ?? ""} · contact ${item.contact_score ?? ""} · death ${item.death_score ?? ""}`;
    return `<button class="clip-signal-marker ${cls}" style="left:${left}%" title="${escapeAttr(title)}" data-action="jump" data-ts="${escapeAttr(item.timestamp || firstTs)}"></button>`;
  }).join("");
  const contactRows = rows
    .filter((item) => String(item.class || "").includes("contact") || Number(item.death_score || 0) >= 0.45)
    .slice(0, 8)
    .map((item) => `
      <li>
        <button class="timeline-jump" data-action="jump" data-ts="${escapeAttr(item.timestamp || 0)}">${escapeHtml(formatTs(item.timestamp || 0))}</button>
        <strong>${escapeHtml((item.class || "frame").replaceAll("_", " "))}</strong>
        <span>frame ${escapeHtml(item.frame ?? "")} · contact ${escapeHtml(item.contact_score ?? "0")} · death ${escapeHtml(item.death_score ?? "0")}</span>
      </li>
    `).join("");
  return `
    <details class="clip-signal-timeline">
      <summary>Frame Signal Timeline</summary>
      <div class="clip-signal-track">${markers}</div>
      <div class="timeline-legend">
        <span><i class="signal-contact"></i>contact</span>
        <span><i class="signal-damage"></i>damage/death</span>
        <span><i class="signal-empty"></i>low signal</span>
      </div>
      ${contactRows ? `<ul class="compact-list">${contactRows}</ul>` : `<p class="muted">No contact/death frames crossed the display threshold.</p>`}
    </details>
  `;
}

function frameClassCss(value) {
  const text = String(value || "");
  if (text.includes("death") || text.includes("damage")) return "damage";
  if (text.includes("contact")) return "contact";
  return "empty";
}

function renderClassCounts(counts) {
  const rows = Object.entries(counts || {})
    .filter(([, value]) => Number(value) > 0)
    .map(([key, value]) => `${key.replaceAll("_", " ")} ${value}`)
    .join(", ");
  return rows ? ` <span class="muted">(${escapeHtml(rows)})</span>` : "";
}

function renderParsedHudRow(parsedHud) {
  const items = [
    ["Score", parsedHud.score],
    ["Round", parsedHud.round_number_from_score || parsedHud.round_number],
    ["Timer", parsedHud.round_timer],
    ["HP", parsedHud.health],
    ["Ammo", parsedHud.ammo],
    ["Weapon", parsedHud.weapon],
    ["Spike", parsedHud.spike_state],
  ].filter(([, value]) => value !== undefined && value !== null && String(value).trim());
  if (!items.length) return "";
  return `<li><strong>HUD parsed:</strong> ${items.map(([key, value]) => `${escapeHtml(key)} ${escapeHtml(formatPerceptionValue(value))}`).join(" · ")}</li>`;
}

function renderReviewPipeline(pipeline) {
  const steps = pipeline.steps || [];
  if (!steps.length) return "";
  const rows = steps.map((step) => `
    <li>
      <strong>${escapeHtml(step.label || step.id || "")}</strong>
      <span class="muted">${escapeHtml(step.status || "")}${step.count !== undefined ? ` · ${escapeHtml(step.count)}` : ""}</span>
      ${step.summary ? `<p>${escapeHtml(shortenText(step.summary, 160))}</p>` : ""}
    </li>
  `).join("");
  return `<p><strong>Pipeline</strong></p><ul class="compact-list">${rows}</ul>`;
}

function renderEvidenceTimeline(items) {
  const rows = (items || []).slice(0, 8).map((item) => {
    const ts = item.video_timestamp !== undefined && item.video_timestamp !== null && item.video_timestamp !== ""
      ? `<button class="timeline-jump" data-action="jump" data-ts="${escapeAttr(item.video_timestamp)}">${escapeHtml(formatTs(item.video_timestamp))}</button>`
      : `<span class="muted">${escapeHtml(item.time || item.frame || "")}</span>`;
    return `
      <li>
        ${ts}
        <strong>${escapeHtml(titleCase(item.event || item.segment_id || "evidence"))}</strong>
        <span>${escapeHtml(item.evidence || "")}</span>
        <em>${Math.round(Number(item.claim_confidence || 0) * 100)}%</em>
      </li>
    `;
  }).join("");
  if (!rows) return "";
  return `
    <div class="evidence-timeline">
      <strong>Evidence Timeline</strong>
      <ol>${rows}</ol>
    </div>
  `;
}

function renderSegmentReviews(items) {
  const rows = (items || []).map((item) => `
    <li>
      <strong>${escapeHtml(item.label || item.segment_id || "segment")}</strong>
      <span class="muted">${escapeHtml(item.frame_range || "")} · ${Math.round(Number(item.confidence || 0) * 100)}%</span>
      <p>${escapeHtml(item.summary || "")}</p>
      ${item.mistake ? `<p><strong>Issue:</strong> ${escapeHtml(item.mistake)}</p>` : ""}
    </li>
  `).join("");
  return rows ? `<ul class="compact-list segment-review-list">${rows}</ul>` : `<p class="muted">No segment reads returned.</p>`;
}

function renderClaimConfidence(claims) {
  const rows = Object.entries(claims || {})
    .filter(([, value]) => Number(value) > 0)
    .map(([key, value]) => `<span title="${escapeAttr(key)}">${escapeHtml(key.replaceAll("_", " "))} ${Math.round(Number(value || 0) * 100)}%</span>`)
    .join("");
  return rows ? `<div class="coach-progress claim-confidence">${rows}</div>` : "";
}

function renderPerceptionRead(perception) {
  if (!perception || !Object.keys(perception).length) return "";
  const rows = [
    ["Enemy", perception.enemy_seen],
    ["Contact", perception.first_contact_time],
    ["TTD", perception.time_to_death],
    ["Crosshair", perception.crosshair_level],
    ["Alignment", perception.crosshair_alignment],
    ["Peek", perception.peek_type],
    ["Utility", perception.utility_seen],
    ["Weapon", perception.weapon_seen],
    ["HP", perception.hp_seen],
    ["Score", perception.score_seen],
    ["Spike", perception.spike_state_seen],
  ].filter(([, value]) => value !== undefined && value !== null && String(value).trim() && String(value).trim() !== "unknown");
  if (!rows.length) return "";
  return `
    <div class="coach-progress perception-grid">
      ${rows.slice(0, 10).map(([label, value]) => `<span>${escapeHtml(label)}: ${escapeHtml(formatPerceptionValue(value))}</span>`).join("")}
    </div>
  `;
}

function renderCoachingRead(coaching, payload) {
  const items = [
    ["First mistake", coaching.first_mistake || payload.first_mistake],
    ["Utility", coaching.utility_issue || payload.utility_issue],
    ["Crosshair", coaching.crosshair_issue || payload.crosshair_issue],
    ["Positioning", coaching.positioning_issue || payload.positioning_issue],
    ["Mechanics", coaching.mechanical_issue || payload.mechanical_issue],
  ].filter(([, value]) => value && String(value).trim());
  if (!items.length) return "";
  return `
    <ul class="compact-list">
      ${items.map(([label, value]) => `<li><strong>${escapeHtml(label)}:</strong> ${escapeHtml(value)}</li>`).join("")}
    </ul>
  `;
}

function formatPerceptionValue(value) {
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return value;
}

function fillReviewDraft(button) {
  const card = button.closest(".death-card");
  if (!card) return;
  const labels = button.dataset.labels || "";
  const note = button.dataset.note || "";
  const labelsInput = card.querySelector('[data-field="mistake_labels"]');
  const notesInput = card.querySelector('[data-field="notes"]');
  if (labelsInput && labels && !labelsInput.value.trim()) {
    labelsInput.value = labels;
  }
  if (notesInput && note) {
    notesInput.value = notesInput.value.trim() ? `${notesInput.value.trim()} | ${note}` : note;
  }
  setStatus("Review draft filled from Clip Coach. Save the marker to confirm it.");
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
    return '<div class="advice-empty">No coach read yet. Click Coach This Clip after confirming this marker.</div>';
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

async function clearPendingSuggestions(matchId) {
  const payload = await api(`/api/matches/${matchId}/suggestions/clear-pending`, { method: "POST" });
  setStatus(`Cleared ${payload.cleared || 0} unreviewed suggestion(s).`);
  await loadReport(matchId);
}

async function loadDeathEvidence(button) {
  const target = document.querySelector(`[data-evidence-target="${button.dataset.id}"]`);
  if (!target) return;
  target.innerHTML = '<p class="muted">Loading evidence receipts...</p>';
  const payload = await api(`/api/deaths/${button.dataset.id}/evidence`);
  target.innerHTML = renderEvidenceResult(payload);
  setStatus("Evidence loaded.");
}

function renderEvidenceResult(payload) {
  const marker = payload.marker || {};
  const detector = payload.detector || {};
  const localAi = payload.local_ai || {};
  const ocr = payload.ocr || {};
  const frames = payload.frames || {};
  const gaps = (payload.gaps || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const suggestions = (payload.suggestions || []).map((item) => `
    <li>${escapeHtml(item.status || "unknown")} @ ${formatTs(item.timestamp)} · ${Math.round(Number(item.confidence || 0) * 100)}% · ${escapeHtml(shortenText(item.reason || "", 120))}</li>
  `).join("");
  return `
    <div class="evidence-grid">
      <article>
        <span>Marker</span>
        <strong>${escapeHtml(marker.source || "manual")}</strong>
        <p>${formatTs(marker.timestamp)} · ${escapeHtml(marker.labels || [])}</p>
      </article>
      <article>
        <span>Frames</span>
        <strong>${Number(frames.keyframe_count || 0)}</strong>
        <p>Saved keyframe receipt(s)</p>
      </article>
      <article>
        <span>OCR</span>
        <strong>${escapeHtml(ocr.status || "missing")}</strong>
        <p>${Number(ocr.read_count || 0)} read(s)</p>
      </article>
      <article>
        <span>Detector</span>
        <strong>${Number(detector.annotation_count || 0)}</strong>
        <p>${Number(detector.prelabel_count || 0)} prelabel(s)</p>
      </article>
      <article>
        <span>Local AI</span>
        <strong>${escapeHtml(localAi.status || "missing")}</strong>
        <p>${escapeHtml(shortenText(localAi.summary || "", 130))}</p>
      </article>
    </div>
    <p>${escapeHtml(payload.summary || "")}</p>
    ${suggestions ? `<details class="advanced-actions"><summary>Nearby suggestion receipts</summary><ul class="compact-list">${suggestions}</ul></details>` : ""}
    ${gaps ? `<details class="advanced-actions" open><summary>Evidence gaps</summary><ul class="compact-list">${gaps}</ul></details>` : ""}
  `;
}

async function runOcrHealthCheck(matchId) {
  const player = document.querySelector("#vodPlayer");
  const timeInput = document.querySelector("#ocrHealthTimestamp");
  const regionInput = document.querySelector("#ocrHealthRegions");
  const rawTime = (timeInput?.value || "").trim();
  const timestamp = rawTime ? parseTimeInput(rawTime) : (player ? player.currentTime : null);
  const regions = (regionInput?.value || "").split(",").map((item) => item.trim()).filter(Boolean);
  const mount = document.querySelector("#ocrHealthResult");
  if (mount) mount.innerHTML = '<p class="muted">Extracting frame and checking OCR crops...</p>';
  setStatus("Running OCR health check...", { state: "busy" });
  const payload = await api(`/api/matches/${matchId}/ocr-health`, {
    method: "POST",
    body: JSON.stringify({ timestamp, regions }),
  });
  if (mount) mount.innerHTML = renderOcrHealthResult(payload.analysis || {});
  setStatus(payload.message || "OCR health check complete.");
}

function renderOcrHealthResult(analysis) {
  const cards = (analysis.regions || []).map((item) => `
    <article class="ocr-health-card ${escapeAttr(item.status || "")}">
      ${item.frame_id ? `<img src="/api/vision/frame/${escapeAttr(item.frame_id)}" alt="${escapeAttr(item.region)} OCR crop" />` : ""}
      <div>
        <strong>${escapeHtml(item.region || "region")}</strong>
        <span class="tag">${escapeHtml(item.status || "unknown")}</span>
        <p>${escapeHtml(item.message || "")}</p>
        ${item.text ? `<pre>${escapeHtml(shortenText(item.text, 300))}</pre>` : ""}
      </div>
    </article>
  `).join("");
  return `
    <div class="review-head">
      <strong>${escapeHtml(analysis.summary || "OCR health result")}</strong>
      <span class="muted">${formatTs(analysis.timestamp)}</span>
    </div>
    <div class="ocr-health-grid">${cards || '<p class="muted">No OCR regions returned.</p>'}</div>
  `;
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

async function saveDeathContext(button) {
  const card = button.closest(".death-card");
  const id = button.dataset.id;
  const read = (field) => card.querySelector(`[data-field="${field}"]`)?.value || "";
  await api(`/api/deaths/${id}/context`, {
    method: "POST",
    body: JSON.stringify({
      map: read("context_map"),
      agent: read("context_agent"),
      round_number: read("context_round_number"),
      side: read("context_side"),
      weapon: read("context_weapon"),
      location: read("context_location"),
      spike_state: read("context_spike_state"),
      team_counts: read("context_team_counts"),
      notes: read("context_notes"),
      confidence: 1,
    }),
  });
  setStatus("Match context saved. Knowledge retrieval will use this correction.");
  await Promise.all([loadReport(currentMatchId), loadMatches(), loadAutomation()]);
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
  player.scrollIntoView({ behavior: "smooth", block: "center" });
  player.focus({ preventScroll: true });
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
  if (death.display_round_number) {
    const source = death.round_source === "timeline" ? "timeline" : "est.";
    return `Round ${death.display_round_number} (${source}) · ${timestamp}`;
  }
  return `${timestamp} · ${death.round_unknown_reason || "Round unknown"}`;
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
document.querySelectorAll(".side-tab").forEach((button) => {
  button.addEventListener("click", () => activateDashboardTab(button.dataset.tabTarget));
});
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
  if (action === "jump") jumpTo(button.dataset.ts);
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
  if (action === "use-olmocr-defaults") useOlmocrDefaults();
  if (action === "set-local-ai-fps") setLocalAiFps(button.dataset.fps);
  if (action === "set-local-ai-window") setLocalAiWindow(button.dataset.window);
  if (action === "test-local-ai") testLocalAiConfig().catch((err) => setStatus(err.message));
  if (action === "save-local-ai") saveLocalAiConfig().catch((err) => setStatus(err.message));
  if (action === "rebuild-knowledge") rebuildKnowledgeBase().catch((err) => setStatus(err.message));
  if (action === "search-knowledge") searchKnowledgeBase().catch((err) => setStatus(err.message));
  if (action === "load-prompt") loadPromptEditor();
  if (action === "save-prompt") savePromptTemplate().catch((err) => setStatus(err.message));
  if (action === "apply-detector-tuning") applyDetectorTuning().catch((err) => setStatus(err.message));
  if (action === "build-detector-candidates") buildDetectorCandidates().catch((err) => setStatus(err.message));
  if (action === "prelabel-detector-candidates") prelabelDetectorCandidates().catch((err) => setStatus(err.message));
  if (action === "evaluate-detector") evaluateDetector().catch((err) => setStatus(err.message));
  if (action === "export-detector-dataset") exportDetectorDataset().catch((err) => setStatus(err.message));
  if (action === "train-detector") trainDetector().catch((err) => setStatus(err.message));
  if (action === "use-detector-command") useDetectorCommand();
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
  if (action === "suggest-range") suggestDeathsRange(button).catch((err) => setStatus(err.message));
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
  if (action === "clear-pending-suggestions") clearPendingSuggestions(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "load-evidence") loadDeathEvidence(button).catch((err) => setStatus(err.message));
  if (action === "ocr-health") runOcrHealthCheck(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "coach-moment-feedback") saveCoachMomentFeedback(button).catch((err) => setStatus(err.message));
  if (action === "benchmark-false-positive") saveBenchmarkLabel({ match_id: button.dataset.match, suggestion_id: button.dataset.id, timestamp: button.dataset.ts, label_type: "false_positive", note: "Marked from suggestion card" }).catch((err) => setStatus(err.message));
  if (action === "benchmark-true-positive") saveBenchmarkLabel({ match_id: button.dataset.match, death_id: button.dataset.id, timestamp: button.dataset.ts, label_type: "true_positive", note: "Marked from death card" }).catch((err) => setStatus(err.message));
  if (action === "benchmark-missed") saveBenchmarkLabel({ match_id: button.dataset.id, timestamp: document.querySelector("#benchmarkMissedTs").value, label_type: "missed_death", note: document.querySelector("#benchmarkNote").value }).catch((err) => setStatus(err.message));
  if (action === "add-death") addDeath(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "coach-clip") coachClip(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "advice") getAdvice(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "vision") analyzeDeathVision(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "keyframes") extractKeyframes(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "understand") understandClip(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "gameplay") analyzeGameplay(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "ai-review") aiReview(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "local-ai-review") localAiReview(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "fill-review-draft") fillReviewDraft(button);
  if (action === "clip-review-feedback") saveClipReviewFeedback(button).catch((err) => setStatus(err.message));
  if (action === "save-training-label") saveClipTrainingLabel(button).catch((err) => setStatus(err.message));
  if (action === "save-detector-annotation") saveDetectorAnnotation(button).catch((err) => setStatus(err.message));
  if (action === "advice-feedback") saveAdviceFeedback(button.dataset.id, button.dataset.verdict).catch((err) => setStatus(err.message));
  if (action === "save-death") saveDeath(button).catch((err) => setStatus(err.message));
  if (action === "save-context") saveDeathContext(button).catch((err) => setStatus(err.message));
  if (action === "save-correction") saveDeathCorrection(button).catch((err) => setStatus(err.message));
  if (action === "save-annotation") saveClipAnnotation(button).catch((err) => setStatus(err.message));
  if (action === "set-annotation-time") setAnnotationTime(button);
  if (action === "loop-death") loopDeath(button.dataset.ts);
  if (action === "delete-death") deleteDeath(button.dataset.id).catch((err) => setStatus(err.message));
  if (action === "toggle-calibration-overlay") toggleCalibrationOverlay();
  if (action === "save-overlay-calibration") saveCalibration().catch((err) => setStatus(err.message));
});

els.reportView.addEventListener("change", (event) => {
  if (event.target.id === "overlayRegion") {
    selectedCalibrationRegion = event.target.value;
    renderCalibrationOverlay();
  }
  if (event.target.matches('[data-field="detector_frame_id"]')) {
    renderDetectorFrameCanvas(event.target);
  }
  if (event.target.matches('[data-field="detector_label"]') && event.target.value === "no_enemy") {
    const card = event.target.closest(".death-card");
    setDetectorBoxInputs(card, 0, 0, 0, 0);
    const box = card?.querySelector(".detector-drawn-box");
    if (box) box.hidden = true;
  }
});
els.reportView.addEventListener("pointerdown", startCalibrationDrag);
els.reportView.addEventListener("pointerdown", startDetectorBoxDrag);
window.addEventListener("pointermove", moveCalibrationDrag);
window.addEventListener("pointermove", updateDetectorBoxDrag);
window.addEventListener("pointerup", stopCalibrationDrag);
window.addEventListener("pointerup", stopDetectorBoxDrag);

setStatus("Loading app...", { state: "busy" });
loadSettings()
  .then(() => Promise.all([loadVersionBadge(), loadMatches(), loadTrends(), loadCoach(), loadCapabilities(), loadCalibration(), loadAutomation()]))
  .then(() => {
    if (latestJobs.some((job) => ["queued", "running"].includes(job.status))) {
      ensureJobPolling();
    } else {
      setStatus("Ready.", { state: "idle" });
    }
  })
  .catch((err) => setStatus(err.message));
