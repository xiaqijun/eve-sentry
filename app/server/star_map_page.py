"""HTML page served by the local intel server."""

INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>EVE Sentry Intel Map</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b0d10;
      --panel: #15191f;
      --panel-2: #1d232b;
      --line: #39424d;
      --text: #eef3f8;
      --muted: #9aa7b5;
      --accent: #39d6c3;
      --danger: #ff4d5e;
      --warn: #ffd166;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 "Segoe UI", "Microsoft YaHei", sans-serif;
      overflow: hidden;
    }
    .shell {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 360px;
      height: 100vh;
    }
    .map-area {
      position: relative;
      min-width: 0;
      background:
        radial-gradient(circle at 35% 20%, rgba(57, 214, 195, 0.12), transparent 32%),
        linear-gradient(135deg, #0b0d10 0%, #11151a 60%, #100d12 100%);
    }
    canvas {
      display: block;
      width: 100%;
      height: 100%;
    }
    header {
      position: absolute;
      inset: 18px auto auto 20px;
      display: flex;
      align-items: center;
      gap: 14px;
      pointer-events: none;
    }
    h1 {
      margin: 0;
      font-size: 20px;
      font-weight: 650;
      letter-spacing: 0;
    }
    .summary {
      display: flex;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
    }
    .pill {
      padding: 4px 8px;
      border: 1px solid rgba(255,255,255,0.12);
      background: rgba(18, 22, 27, 0.72);
      border-radius: 4px;
    }
    aside {
      min-width: 0;
      border-left: 1px solid #252d36;
      background: var(--panel);
      display: grid;
      grid-template-rows: auto auto auto auto auto auto minmax(0, 1fr);
    }
    .toolbar {
      padding: 16px;
      border-bottom: 1px solid #252d36;
      display: grid;
      gap: 10px;
    }
    .toolbar label {
      color: var(--muted);
      font-size: 12px;
    }
    input,
    textarea {
      width: 100%;
      color: var(--text);
      background: #0f1318;
      border: 1px solid #303946;
      border-radius: 4px;
      outline: none;
    }
    input {
      height: 34px;
      padding: 0 10px;
    }
    textarea {
      min-height: 58px;
      resize: vertical;
      padding: 8px 10px;
      font: inherit;
    }
    input:focus,
    textarea:focus { border-color: var(--accent); }
    .selected {
      padding: 14px 16px;
      border-bottom: 1px solid #252d36;
      background: var(--panel-2);
    }
    .selected h2 {
      margin: 0 0 8px;
      font-size: 17px;
      font-weight: 650;
    }
    .ingest-panel,
    .esi-panel,
    .config-panel {
      padding: 12px 16px;
      border-bottom: 1px solid #252d36;
      background: #12171d;
      display: grid;
      gap: 10px;
    }
    .ingest-panel {
      background: #10151b;
    }
    .esi-panel {
      background: #111820;
    }
    .ingest-panel h2,
    .esi-panel h2,
    .config-panel h2 {
      margin: 0;
      font-size: 15px;
      font-weight: 650;
    }
    .status-grid {
      display: grid;
      gap: 5px;
    }
    .status-row {
      min-width: 0;
      display: flex;
      justify-content: space-between;
      gap: 10px;
      color: var(--muted);
      font-size: 12px;
    }
    .status-row strong {
      color: var(--text);
      font-weight: 650;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .config-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }
    .field {
      display: grid;
      gap: 5px;
    }
    .field label {
      color: var(--muted);
      font-size: 12px;
    }
    .field-wide {
      grid-column: 1 / -1;
    }
    .actions {
      display: flex;
      gap: 8px;
      align-items: center;
    }
    button {
      height: 32px;
      color: var(--text);
      background: #202832;
      border: 1px solid #3a4655;
      border-radius: 4px;
      padding: 0 10px;
      cursor: pointer;
    }
    button.primary {
      color: #041512;
      background: var(--accent);
      border-color: var(--accent);
      font-weight: 650;
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.58;
    }
    .form-status,
    .esi-status,
    .config-status {
      min-width: 0;
      color: var(--muted);
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .list-tabs {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      padding: 10px 16px;
      border-bottom: 1px solid #252d36;
      background: #11161c;
    }
    .tab {
      height: 30px;
      color: var(--muted);
      background: #151b22;
      border: 1px solid #303946;
    }
    .tab.active {
      color: var(--text);
      border-color: var(--accent);
      background: #19302f;
    }
    .meta {
      color: var(--muted);
      font-size: 12px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .intel-list {
      min-height: 0;
      overflow: auto;
      padding: 10px;
    }
    .report {
      padding: 10px;
      margin-bottom: 8px;
      border: 1px solid #2c3540;
      border-radius: 6px;
      background: #101419;
    }
    .report.hot { border-color: rgba(255, 77, 94, 0.55); }
    .report.critical { border-color: rgba(255, 77, 94, 0.72); }
    .report.high { border-color: rgba(255, 209, 102, 0.68); }
    .report.medium { border-color: rgba(57, 214, 195, 0.54); }
    .report-title {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      font-weight: 650;
      margin-bottom: 6px;
    }
    .time {
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .names {
      color: var(--danger);
      word-break: break-word;
    }
    .note {
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
    }
    .scoreline {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
    }
    .level {
      color: var(--text);
      border: 1px solid #44505e;
      border-radius: 4px;
      padding: 1px 6px;
      text-transform: uppercase;
    }
    .evidence {
      display: grid;
      gap: 4px;
      margin-top: 8px;
    }
    .evidence-item {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      word-break: break-word;
    }
    .detail-action {
      margin-top: 8px;
      height: 28px;
      font-size: 12px;
    }
    .alert-detail {
      margin-top: 10px;
      padding-top: 8px;
      border-top: 1px solid #2c3540;
      display: grid;
      gap: 8px;
    }
    .detail-section {
      display: grid;
      gap: 5px;
    }
    .detail-title {
      color: var(--text);
      font-size: 12px;
      font-weight: 650;
    }
    .detail-row {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      word-break: break-word;
    }
    .detail-row strong {
      color: var(--text);
      font-weight: 650;
    }
    .empty {
      color: var(--muted);
      padding: 18px 8px;
      text-align: center;
    }
    @media (max-width: 860px) {
      body { overflow: auto; }
      .shell {
        grid-template-columns: 1fr;
        grid-template-rows: 60vh auto;
        height: auto;
        min-height: 100vh;
      }
      aside { border-left: 0; border-top: 1px solid #252d36; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="map-area">
      <canvas id="map"></canvas>
      <header>
        <h1>EVE Sentry Intel Map</h1>
        <div class="summary">
          <span class="pill" id="systems-pill">星系 0</span>
          <span class="pill" id="hostiles-pill">敌对 0</span>
          <span class="pill" id="reports-pill">情报 0</span>
        </div>
      </header>
    </section>
    <aside>
      <section class="toolbar">
        <label for="filter">筛选星系或角色</label>
        <input id="filter" autocomplete="off" placeholder="输入 Jita、Tama 或角色名">
      </section>
      <section class="selected" id="selected"></section>
      <section class="esi-panel" aria-label="ESI Session">
        <h2>ESI Session</h2>
        <div class="status-grid" id="esi-status"></div>
        <div class="actions">
          <button id="esi-refresh" type="button">Refresh</button>
          <button id="esi-use-system" type="button" disabled>Use system</button>
          <span class="esi-status" id="esi-message">Not loaded</span>
        </div>
      </section>
      <section class="ingest-panel" aria-label="Manual Intel">
        <h2>Manual Intel</h2>
        <div class="config-grid">
          <div class="field">
            <label for="obs-system">System</label>
            <input id="obs-system" autocomplete="off" placeholder="Tama">
          </div>
          <div class="field">
            <label for="obs-source">Source</label>
            <input id="obs-source" autocomplete="off" value="manual" placeholder="manual">
          </div>
          <div class="field field-wide">
            <label for="obs-names">Pilots</label>
            <textarea id="obs-names" autocomplete="off" placeholder="One pilot per line"></textarea>
          </div>
          <div class="field field-wide">
            <label for="obs-raw">Raw note</label>
            <textarea id="obs-raw" autocomplete="off" placeholder="Raw intel text"></textarea>
          </div>
        </div>
        <div class="actions">
          <button class="primary" id="obs-submit" type="button">Submit</button>
          <button id="obs-clear" type="button">Clear</button>
          <span class="form-status" id="obs-status">Ready</span>
        </div>
      </section>
      <section class="config-panel" aria-label="Scoring Config">
        <h2>Scoring Config</h2>
        <div class="config-grid">
          <div class="field field-wide">
            <label for="cfg-whitelist">Whitelist pilots</label>
            <textarea id="cfg-whitelist" autocomplete="off" placeholder="One pilot per line"></textarea>
          </div>
          <div class="field field-wide">
            <label for="cfg-blacklist">Blacklist pilots</label>
            <textarea id="cfg-blacklist" autocomplete="off" placeholder="One pilot per line"></textarea>
          </div>
          <div class="field">
            <label for="cfg-corps">Hostile corps</label>
            <input id="cfg-corps" inputmode="numeric" autocomplete="off" placeholder="98000001, 98000002">
          </div>
          <div class="field">
            <label for="cfg-alliances">Hostile alliances</label>
            <input id="cfg-alliances" inputmode="numeric" autocomplete="off" placeholder="99000001, 99000002">
          </div>
          <div class="field">
            <label for="cfg-standing">Standing threshold</label>
            <input id="cfg-standing" type="number" step="0.1" placeholder="-5">
          </div>
          <div class="field">
            <label for="cfg-cooldown">Cooldown seconds</label>
            <input id="cfg-cooldown" type="number" min="0" step="1" placeholder="60">
          </div>
        </div>
        <div class="actions">
          <button class="primary" id="cfg-save" type="button">Save</button>
          <button id="cfg-reload" type="button">Reload</button>
          <span class="config-status" id="cfg-status">Not loaded</span>
        </div>
      </section>
      <section class="list-tabs" aria-label="Intel list mode">
        <button class="tab active" id="tab-reports" type="button">Reports</button>
        <button class="tab" id="tab-alerts" type="button">Alerts</button>
      </section>
      <section class="intel-list" id="intel"></section>
    </aside>
  </main>
  <script>
    const canvas = document.getElementById("map");
    const ctx = canvas.getContext("2d");
    const intelEl = document.getElementById("intel");
    const selectedEl = document.getElementById("selected");
    const esiStatusEl = document.getElementById("esi-status");
    const esiMessageEl = document.getElementById("esi-message");
    const esiRefreshButton = document.getElementById("esi-refresh");
    const esiUseSystemButton = document.getElementById("esi-use-system");
    const filterEl = document.getElementById("filter");
    const reportTabButton = document.getElementById("tab-reports");
    const alertTabButton = document.getElementById("tab-alerts");
    const obsStatusEl = document.getElementById("obs-status");
    const obsFields = {
      system_name: document.getElementById("obs-system"),
      names: document.getElementById("obs-names"),
      source: document.getElementById("obs-source"),
      raw_text: document.getElementById("obs-raw")
    };
    const submitIntelButton = document.getElementById("obs-submit");
    const clearIntelButton = document.getElementById("obs-clear");
    const configStatusEl = document.getElementById("cfg-status");
    const configFields = {
      whitelist: document.getElementById("cfg-whitelist"),
      blacklist: document.getElementById("cfg-blacklist"),
      hostile_corporation_ids: document.getElementById("cfg-corps"),
      hostile_alliance_ids: document.getElementById("cfg-alliances"),
      hostile_standing_threshold: document.getElementById("cfg-standing"),
      cooldown_seconds: document.getElementById("cfg-cooldown")
    };
    const saveConfigButton = document.getElementById("cfg-save");
    const reloadConfigButton = document.getElementById("cfg-reload");
    let snapshot = { systems: [], links: [], reports: [], characters: [], summary: {} };
    let esiSession = { loading: true, enabled: false, authenticated: false, error: "", location: null, scopes: [] };
    let selectedSystem = null;
    let listMode = "reports";
    let manualSystemId = 0;
    const alertDetails = new Map();

    function resize() {
      const rect = canvas.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(rect.width * ratio));
      canvas.height = Math.max(1, Math.floor(rect.height * ratio));
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      draw();
    }

    function scale(system) {
      const rect = canvas.getBoundingClientRect();
      return {
        x: system.x / 1000 * rect.width,
        y: system.y / 700 * rect.height
      };
    }

    function draw() {
      const rect = canvas.getBoundingClientRect();
      ctx.clearRect(0, 0, rect.width, rect.height);
      ctx.fillStyle = "#0b0d10";
      ctx.fillRect(0, 0, rect.width, rect.height);

      const systemsByName = new Map(snapshot.systems.map(system => [system.name, system]));
      ctx.lineWidth = 1;
      for (const link of snapshot.links) {
        const a = systemsByName.get(link.from);
        const b = systemsByName.get(link.to);
        if (!a || !b) continue;
        const pa = scale(a);
        const pb = scale(b);
        ctx.strokeStyle = "rgba(120, 140, 160, 0.28)";
        ctx.beginPath();
        ctx.moveTo(pa.x, pa.y);
        ctx.lineTo(pb.x, pb.y);
        ctx.stroke();
      }

      for (const system of snapshot.systems) {
        const p = scale(system);
        const hot = system.hostile_count > 0;
        const selected = selectedSystem === system.name;
        const radius = hot ? 7 + Math.min(system.hostile_count, 8) : 4;

        if (hot) {
          ctx.beginPath();
          ctx.arc(p.x, p.y, radius + 10, 0, Math.PI * 2);
          ctx.fillStyle = "rgba(255, 77, 94, 0.16)";
          ctx.fill();
        }
        ctx.beginPath();
        ctx.arc(p.x, p.y, selected ? radius + 4 : radius, 0, Math.PI * 2);
        ctx.fillStyle = hot ? "#ff4d5e" : "#8da0b3";
        ctx.fill();
        ctx.strokeStyle = selected ? "#ffd166" : "rgba(255,255,255,0.35)";
        ctx.lineWidth = selected ? 2 : 1;
        ctx.stroke();

        ctx.fillStyle = hot ? "#fff2f3" : "#b5c1cd";
        ctx.font = hot ? "650 12px Segoe UI" : "12px Segoe UI";
        ctx.fillText(system.name, p.x + 10, p.y + 4);
      }
    }

    function render() {
      const summary = snapshot.summary || {};
      document.getElementById("systems-pill").textContent = `星系 ${summary.system_count || 0}`;
      document.getElementById("hostiles-pill").textContent = `敌对 ${summary.hostile_count || 0}`;
      document.getElementById("reports-pill").textContent = `情报 ${summary.report_count || 0}`;

      if (!selectedSystem && snapshot.systems.length) {
        const hot = snapshot.systems.find(system => system.hostile_count > 0);
        selectedSystem = hot ? hot.name : snapshot.systems[0].name;
      }
      renderSelected();
      renderIntelList();
      draw();
    }

    function renderSelected() {
      const system = snapshot.systems.find(item => item.name === selectedSystem);
      if (!system) {
        selectedEl.innerHTML = `<h2>未选择星系</h2><div class="meta">点击星图上的节点查看详情</div>`;
        return;
      }
      const names = system.hostiles && system.hostiles.length
        ? system.hostiles.join("、")
        : "暂无敌对";
      selectedEl.innerHTML = `
        <h2>${escapeHtml(system.name)}</h2>
        <div class="meta">
          <span>${escapeHtml(system.region || "未知区域")}</span>
          <span>安全 ${system.security ?? "?"}</span>
          <span>敌对 ${system.hostile_count || 0}</span>
          <span>最近 ${formatTime(system.latest_seen)}</span>
        </div>
        <div class="note names">${escapeHtml(names)}</div>
      `;
    }

    function renderIntelList() {
      reportTabButton.classList.toggle("active", listMode === "reports");
      alertTabButton.classList.toggle("active", listMode === "alerts");
      if (listMode === "alerts") {
        renderAlerts();
        return;
      }
      renderReports();
    }

    function renderReports() {
      const query = filterEl.value.trim().toLowerCase();
      const reports = snapshot.reports.filter(report => {
        if (!query) return true;
        return report.system.toLowerCase().includes(query)
          || report.names.some(name => name.toLowerCase().includes(query))
          || (report.note || "").toLowerCase().includes(query);
      }).slice(0, 80);

      if (!reports.length) {
        intelEl.innerHTML = `<div class="empty">暂无匹配情报</div>`;
        return;
      }
      intelEl.innerHTML = reports.map(report => `
        <article class="report hot">
          <div class="report-title">
            <span>${escapeHtml(report.system)}</span>
            <span class="time">${formatTime(report.seen_at)}</span>
          </div>
          <div class="names">${escapeHtml(report.names.join("、"))}</div>
          <div class="note">来源: ${escapeHtml(report.source || "ocr")}${report.note ? " | " + escapeHtml(report.note) : ""}</div>
        </article>
      `).join("");
    }

    function renderAlerts() {
      const query = filterEl.value.trim().toLowerCase();
      const alerts = (snapshot.alerts || []).filter(alert => {
        if (!query) return true;
        const system = String(alert.system_name || alert.system || "").toLowerCase();
        const names = Array.isArray(alert.names) ? alert.names : [];
        const evidence = Array.isArray(alert.evidence) ? alert.evidence : [];
        return system.includes(query)
          || names.some(name => String(name).toLowerCase().includes(query))
          || evidence.some(item => String(item.summary || item.type || "").toLowerCase().includes(query));
      }).slice(0, 80);

      if (!alerts.length) {
        intelEl.innerHTML = `<div class="empty">No matching alerts</div>`;
        return;
      }
      intelEl.innerHTML = alerts.map(alert => {
        const evidence = Array.isArray(alert.evidence) ? alert.evidence.slice(0, 4) : [];
        const level = String(alert.level || "low").toLowerCase();
        const system = alert.system_name || alert.system || "Unknown";
        const names = Array.isArray(alert.names) ? alert.names.join(", ") : "Unknown target";
        const alertId = alert.id || alert.source_observation_id || `${system}:${names}`;
        const detailsOpen = alertDetails.has(alertId);
        return `
          <article class="report ${escapeHtml(level)}">
            <div class="report-title">
              <span>${escapeHtml(system)}</span>
              <span class="time">${formatTime(alert.created_at || alert.seen_at)}</span>
            </div>
            <div class="scoreline">
              <span class="level">${escapeHtml(level)}</span>
              <span>Score ${Number(alert.score || 0)}</span>
            </div>
            <div class="names">${escapeHtml(names)}</div>
            <div class="evidence">
              ${evidence.map(item => `
                <div class="evidence-item">
                  ${escapeHtml(item.summary || item.type || "Evidence")}
                  ${item.weight === undefined ? "" : ` (${Number(item.weight)})`}
                </div>
              `).join("")}
            </div>
            <button class="detail-action" type="button" data-alert-details="${escapeHtml(alertId)}">
              ${detailsOpen ? "Hide details" : "Details"}
            </button>
            ${renderAlertDetails(alertId)}
          </article>
        `;
      }).join("");
    }

    function renderAlertDetails(alertId) {
      const detail = alertDetails.get(alertId);
      if (!detail) return "";
      if (detail.status === "loading") {
        return `<div class="alert-detail"><div class="detail-row">Loading details...</div></div>`;
      }
      if (detail.status === "error") {
        return `<div class="alert-detail"><div class="detail-row">${escapeHtml(detail.error || "Details unavailable")}</div></div>`;
      }

      const characters = Array.isArray(detail.characters) ? detail.characters : [];
      const characterRows = characters.length
        ? characters.map(renderCharacterDetail).join("")
        : `<div class="detail-row">No character identifiers available</div>`;
      return `
        <div class="alert-detail">
          <div class="detail-section">
            <div class="detail-title">Characters</div>
            ${characterRows}
          </div>
          <div class="detail-section">
            <div class="detail-title">System</div>
            ${renderSystemDetail(detail)}
          </div>
        </div>
      `;
    }

    function renderCharacterDetail(item) {
      const profile = item.profile || {};
      const activity = item.activity || {};
      const label = profile.name || item.label || profile.character_id || activity.character_id || "Unknown";
      const profileParts = [
        profile.character_id ? `ID ${profile.character_id}` : "",
        profile.corporation_id ? `Corp ${profile.corporation_id}` : "",
        profile.alliance_id ? `Alliance ${profile.alliance_id}` : "",
        profile.security_status === undefined ? "" : `Security ${Number(profile.security_status).toFixed(1)}`
      ].filter(Boolean);
      const activityParts = [
        activity.kills === undefined ? "" : `Kills ${Number(activity.kills)}`,
        activity.losses === undefined ? "" : `Losses ${Number(activity.losses)}`,
        activity.latest_kill_at ? `Latest ${formatTime(activity.latest_kill_at)}` : ""
      ].filter(Boolean);
      const profileText = profileParts.length ? profileParts.join(" | ") : (item.profileError || "Profile unavailable");
      const activityText = activityParts.length ? activityParts.join(" | ") : (item.activityError || "Kill activity unavailable");
      return `
        <div class="detail-row">
          <strong>${escapeHtml(label)}</strong><br>
          ${escapeHtml(profileText)}<br>
          ${escapeHtml(activityText)}
        </div>
      `;
    }

    function renderSystemDetail(detail) {
      const profile = detail.system || {};
      const activity = detail.systemActivity || {};
      const label = profile.name || detail.systemName || profile.system_id || activity.system_id || "Unknown";
      const profileParts = [
        profile.system_id ? `ID ${profile.system_id}` : "",
        profile.security_status === undefined ? "" : `Security ${Number(profile.security_status).toFixed(1)}`,
        profile.constellation_id ? `Constellation ${profile.constellation_id}` : ""
      ].filter(Boolean);
      const activityParts = [
        activity.kills === undefined ? "" : `Kills ${Number(activity.kills)}`,
        Array.isArray(activity.character_ids) && activity.character_ids.length ? `Pilots ${activity.character_ids.slice(0, 5).join(", ")}` : "",
        activity.latest_kill_at ? `Latest ${formatTime(activity.latest_kill_at)}` : ""
      ].filter(Boolean);
      const profileText = profileParts.length ? profileParts.join(" | ") : (detail.systemError || "System profile unavailable");
      const activityText = activityParts.length ? activityParts.join(" | ") : (detail.systemActivityError || "System activity unavailable");
      return `
        <div class="detail-row">
          <strong>${escapeHtml(label)}</strong><br>
          ${escapeHtml(profileText)}<br>
          ${escapeHtml(activityText)}
        </div>
      `;
    }

    async function toggleAlertDetails(alertId) {
      if (alertDetails.has(alertId)) {
        alertDetails.delete(alertId);
        renderIntelList();
        return;
      }
      alertDetails.set(alertId, { status: "loading" });
      renderIntelList();
      try {
        const alert = findAlert(alertId);
        if (!alert) {
          throw new Error("Alert not found");
        }
        alertDetails.set(alertId, await loadAlertDetails(alert));
      } catch (error) {
        alertDetails.set(alertId, {
          status: "error",
          error: error.message || "Details unavailable"
        });
      }
      renderIntelList();
    }

    function findAlert(alertId) {
      return (snapshot.alerts || []).find(alert => {
        const system = alert.system_name || alert.system || "Unknown";
        const names = Array.isArray(alert.names) ? alert.names.join(", ") : "Unknown target";
        const candidate = alert.id || alert.source_observation_id || `${system}:${names}`;
        return candidate === alertId;
      });
    }

    async function loadAlertDetails(alert) {
      const characterIds = uniquePositiveInts(alert.character_ids);
      const names = Array.isArray(alert.names) ? alert.names.filter(Boolean) : [];
      const characters = characterIds.length
        ? await Promise.all(characterIds.slice(0, 5).map(id => loadCharacterDetail({ id })))
        : await Promise.all(names.slice(0, 5).map(name => loadCharacterDetail({ name })));
      const systemName = alert.system_name || alert.system || "";
      const systemLookup = systemName
        ? await fetchOptional(`/api/systems/by-name/${encodeURIComponent(systemName)}`, "system")
        : { error: "System name unavailable" };
      const system = systemLookup.data || null;
      const systemId = positiveInt(alert.system_id) || positiveInt(system && system.system_id);
      const systemActivityLookup = systemId
        ? await fetchOptional(`/api/kill-activity/system/${systemId}`, "activity")
        : { error: "System id unavailable" };
      return {
        status: "loaded",
        characters,
        systemName,
        system,
        systemError: systemLookup.error,
        systemActivity: systemActivityLookup.data || null,
        systemActivityError: systemActivityLookup.error
      };
    }

    async function loadCharacterDetail(query) {
      const label = query.name || String(query.id);
      const profileLookup = query.id
        ? await fetchOptional(`/api/characters/${query.id}`, "character")
        : await fetchOptional(`/api/characters/by-name/${encodeURIComponent(query.name)}`, "character");
      const profile = profileLookup.data || null;
      const characterId = positiveInt(query.id) || positiveInt(profile && profile.character_id);
      const activityLookup = characterId
        ? await fetchOptional(`/api/kill-activity/character/${characterId}`, "activity")
        : { error: "Character id unavailable" };
      return {
        label,
        profile,
        profileError: profileLookup.error,
        activity: activityLookup.data || null,
        activityError: activityLookup.error
      };
    }

    async function fetchOptional(path, key) {
      const response = await fetch(path, { cache: "no-store" });
      let payload = {};
      try {
        payload = await response.json();
      } catch (_error) {
        payload = {};
      }
      if (response.status === 404) {
        return { data: null, error: payload.error || "Unavailable" };
      }
      if (!response.ok) {
        throw new Error(payload.error || "Lookup failed");
      }
      return { data: payload[key] || null, error: "" };
    }

    function uniquePositiveInts(values) {
      const seen = new Set();
      const result = [];
      for (const value of Array.isArray(values) ? values : []) {
        const number = positiveInt(value);
        if (number && !seen.has(number)) {
          seen.add(number);
          result.push(number);
        }
      }
      return result;
    }

    function positiveInt(value) {
      const number = Number.parseInt(value, 10);
      return Number.isInteger(number) && number > 0 ? number : 0;
    }

    function formatTime(value) {
      if (!value) return "无";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString("zh-CN", { hour12: false });
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function setConfigStatus(text, isError = false) {
      configStatusEl.textContent = text;
      configStatusEl.style.color = isError ? "var(--danger)" : "var(--muted)";
    }

    function listToTextarea(values) {
      return Array.isArray(values) ? values.join("\\n") : "";
    }

    function textareaToList(value) {
      return value
        .split(/\\n|,/)
        .map(item => item.trim())
        .filter(Boolean);
    }

    function setObservationStatus(text, isError = false) {
      obsStatusEl.textContent = text;
      obsStatusEl.style.color = isError ? "var(--danger)" : "var(--muted)";
    }

    function setEsiMessage(text, isError = false) {
      esiMessageEl.textContent = text;
      esiMessageEl.style.color = isError ? "var(--danger)" : "var(--muted)";
    }

    function esiCurrentSystem() {
      const location = esiSession && esiSession.location && typeof esiSession.location === "object"
        ? esiSession.location
        : {};
      const embedded = location.solar_system && typeof location.solar_system === "object"
        ? location.solar_system
        : {};
      const systemId = positiveInt(location.solar_system_id || embedded.system_id);
      const name = String(location.solar_system_name || embedded.name || "").trim();
      return { systemId, name };
    }

    function renderEsiStatus() {
      const current = esiCurrentSystem();
      esiUseSystemButton.disabled = !current.name;
      if (esiSession.loading) {
        esiStatusEl.innerHTML = `<div class="status-row"><span>Status</span><strong>Loading</strong></div>`;
        setEsiMessage("Loading...");
        return;
      }

      const statusText = esiSession.enabled
        ? (esiSession.authenticated ? "Authenticated" : "Enabled")
        : "Disabled";
      const rows = [
        ["Status", statusText],
        ["Character", esiSession.character_id || "Unknown"],
        ["Current system", current.name ? `${current.name}${current.systemId ? ` (${current.systemId})` : ""}` : "Unavailable"],
        ["Scopes", Array.isArray(esiSession.scopes) ? esiSession.scopes.length : 0]
      ];
      esiStatusEl.innerHTML = rows.map(([label, value]) => `
        <div class="status-row">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </div>
      `).join("");
      setEsiMessage(esiSession.error || "Ready", Boolean(esiSession.error));
    }

    async function loadEsiStatus() {
      esiRefreshButton.disabled = true;
      esiSession = { ...esiSession, loading: true, error: "" };
      renderEsiStatus();
      try {
        const statusResponse = await fetch("/api/esi/status", { cache: "no-store" });
        const statusPayload = await statusResponse.json();
        if (!statusResponse.ok) {
          throw new Error(statusPayload.error || "ESI status unavailable");
        }
        esiSession = {
          loading: false,
          enabled: Boolean(statusPayload.enabled),
          authenticated: Boolean(statusPayload.authenticated),
          character_id: statusPayload.character_id || "",
          scopes: Array.isArray(statusPayload.scopes) ? statusPayload.scopes : [],
          error: statusPayload.error || "",
          location: null
        };

        if (esiSession.enabled && esiSession.authenticated) {
          const sessionResponse = await fetch("/api/esi/session?location=true&contacts=false", { cache: "no-store" });
          const sessionPayload = await sessionResponse.json();
          if (!sessionResponse.ok) {
            throw new Error(sessionPayload.error || "ESI session unavailable");
          }
          const session = sessionPayload.snapshot || {};
          esiSession.character_id = session.character_id || esiSession.character_id;
          esiSession.scopes = Array.isArray(session.scopes) ? session.scopes : esiSession.scopes;
          esiSession.location = session.location || null;
        }
      } catch (error) {
        esiSession = {
          ...esiSession,
          loading: false,
          error: error.message || "ESI unavailable"
        };
      } finally {
        esiRefreshButton.disabled = false;
        renderEsiStatus();
      }
    }

    function useEsiSystem() {
      const current = esiCurrentSystem();
      if (!current.name) {
        setEsiMessage("Current system unavailable", true);
        return;
      }
      obsFields.system_name.value = current.name;
      manualSystemId = current.systemId;
      setObservationStatus(`System ${current.name}`);
    }

    function readObservationForm() {
      const systemName = obsFields.system_name.value.trim();
      const current = esiCurrentSystem();
      const payload = {
        system_name: systemName,
        names: textareaToList(obsFields.names.value),
        source: obsFields.source.value.trim() || "manual",
        raw_text: obsFields.raw_text.value.trim()
      };
      if (manualSystemId && systemName === current.name) {
        payload.system_id = manualSystemId;
      }
      return payload;
    }

    function clearObservationForm({ keepSystem = false } = {}) {
      if (!keepSystem) {
        obsFields.system_name.value = "";
        manualSystemId = 0;
      }
      obsFields.names.value = "";
      obsFields.raw_text.value = "";
      obsFields.source.value = obsFields.source.value.trim() || "manual";
      setObservationStatus("Ready");
    }

    async function submitObservation() {
      const payload = readObservationForm();
      if (!payload.system_name) {
        setObservationStatus("System required", true);
        obsFields.system_name.focus();
        return;
      }
      if (!payload.names.length && !payload.raw_text) {
        setObservationStatus("Pilot or raw note required", true);
        obsFields.names.focus();
        return;
      }

      submitIntelButton.disabled = true;
      clearIntelButton.disabled = true;
      setObservationStatus("Submitting...");
      try {
        const response = await fetch("/api/observations", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const result = await response.json();
        if (!response.ok) {
          throw new Error(result.error || "Submit failed");
        }
        const alert = result.alert || null;
        clearObservationForm({ keepSystem: true });
        setObservationStatus(alert ? `Alert ${alert.level || "created"}` : "Observation saved");
        if (alert) {
          listMode = "alerts";
        }
        await refresh();
      } catch (error) {
        setObservationStatus(error.message || "Submit failed", true);
      } finally {
        submitIntelButton.disabled = false;
        clearIntelButton.disabled = false;
      }
    }

    function idListFromInput(value) {
      return value
        .split(/\\n|,/)
        .map(item => Number.parseInt(item.trim(), 10))
        .filter(item => Number.isInteger(item) && item > 0);
    }

    function renderConfig(config) {
      configFields.whitelist.value = listToTextarea(config.whitelist);
      configFields.blacklist.value = listToTextarea(config.blacklist);
      configFields.hostile_corporation_ids.value = (config.hostile_corporation_ids || []).join(", ");
      configFields.hostile_alliance_ids.value = (config.hostile_alliance_ids || []).join(", ");
      configFields.hostile_standing_threshold.value = config.hostile_standing_threshold ?? "";
      configFields.cooldown_seconds.value = config.cooldown_seconds ?? 60;
    }

    function readConfigForm() {
      const standingText = configFields.hostile_standing_threshold.value.trim();
      return {
        whitelist: textareaToList(configFields.whitelist.value),
        blacklist: textareaToList(configFields.blacklist.value),
        hostile_corporation_ids: idListFromInput(configFields.hostile_corporation_ids.value),
        hostile_alliance_ids: idListFromInput(configFields.hostile_alliance_ids.value),
        hostile_standing_threshold: standingText ? Number.parseFloat(standingText) : null,
        cooldown_seconds: Number.parseFloat(configFields.cooldown_seconds.value || "0")
      };
    }

    async function loadConfig() {
      setConfigStatus("Loading...");
      const response = await fetch("/api/config", { cache: "no-store" });
      if (!response.ok) {
        setConfigStatus("Config unavailable", true);
        return;
      }
      const payload = await response.json();
      renderConfig(payload.config || {});
      setConfigStatus("Loaded");
    }

    async function saveConfig() {
      saveConfigButton.disabled = true;
      reloadConfigButton.disabled = true;
      setConfigStatus("Saving...");
      try {
        const response = await fetch("/api/config", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(readConfigForm())
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Save failed");
        }
        renderConfig(payload.config || {});
        setConfigStatus("Saved");
        await refresh();
      } catch (error) {
        setConfigStatus(error.message || "Save failed", true);
      } finally {
        saveConfigButton.disabled = false;
        reloadConfigButton.disabled = false;
      }
    }

    canvas.addEventListener("click", event => {
      const rect = canvas.getBoundingClientRect();
      const point = { x: event.clientX - rect.left, y: event.clientY - rect.top };
      let nearest = null;
      let nearestDistance = Infinity;
      for (const system of snapshot.systems) {
        const p = scale(system);
        const distance = Math.hypot(point.x - p.x, point.y - p.y);
        if (distance < nearestDistance) {
          nearest = system;
          nearestDistance = distance;
        }
      }
      if (nearest && nearestDistance < 28) {
        selectedSystem = nearest.name;
        render();
      }
    });

    filterEl.addEventListener("input", renderIntelList);
    intelEl.addEventListener("click", event => {
      const button = event.target.closest("[data-alert-details]");
      if (!button) return;
      toggleAlertDetails(button.dataset.alertDetails).catch(console.error);
    });
    reportTabButton.addEventListener("click", () => {
      listMode = "reports";
      renderIntelList();
    });
    alertTabButton.addEventListener("click", () => {
      listMode = "alerts";
      renderIntelList();
    });
    saveConfigButton.addEventListener("click", () => saveConfig().catch(console.error));
    reloadConfigButton.addEventListener("click", () => loadConfig().catch(console.error));
    esiRefreshButton.addEventListener("click", () => loadEsiStatus().catch(console.error));
    esiUseSystemButton.addEventListener("click", () => useEsiSystem());
    obsFields.system_name.addEventListener("input", () => { manualSystemId = 0; });
    submitIntelButton.addEventListener("click", () => submitObservation().catch(console.error));
    clearIntelButton.addEventListener("click", () => clearObservationForm());
    window.addEventListener("resize", resize);

    async function refresh() {
      const response = await fetch("/api/intel", { cache: "no-store" });
      snapshot = await response.json();
      render();
    }

    resize();
    refresh().catch(console.error);
    loadConfig().catch(console.error);
    loadEsiStatus().catch(console.error);
    setInterval(() => refresh().catch(console.error), 2000);
    setInterval(() => loadEsiStatus().catch(console.error), 30000);
  </script>
</body>
</html>
"""
