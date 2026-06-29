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
      grid-template-rows: auto auto minmax(0, 1fr);
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
    input {
      width: 100%;
      height: 34px;
      color: var(--text);
      background: #0f1318;
      border: 1px solid #303946;
      border-radius: 4px;
      padding: 0 10px;
      outline: none;
    }
    input:focus { border-color: var(--accent); }
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
      <section class="intel-list" id="intel"></section>
    </aside>
  </main>
  <script>
    const canvas = document.getElementById("map");
    const ctx = canvas.getContext("2d");
    const intelEl = document.getElementById("intel");
    const selectedEl = document.getElementById("selected");
    const filterEl = document.getElementById("filter");
    let snapshot = { systems: [], links: [], reports: [], characters: [], summary: {} };
    let selectedSystem = null;

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
      renderReports();
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

    filterEl.addEventListener("input", renderReports);
    window.addEventListener("resize", resize);

    async function refresh() {
      const response = await fetch("/api/intel", { cache: "no-store" });
      snapshot = await response.json();
      render();
    }

    resize();
    refresh().catch(console.error);
    setInterval(() => refresh().catch(console.error), 2000);
  </script>
</body>
</html>
"""
