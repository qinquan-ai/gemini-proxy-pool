import { createCell, createMetric, createStatusPill, defineUiComponents } from "/assets/ui/components.js";
import { initializeTheme } from "/assets/ui/theme.js";

defineUiComponents();
initializeTheme();

const REFRESH_INTERVAL = 2000;
const statusLabels = { Active: "可用", Cooling: "冷却", Disabled: "禁用" };
const summaryDefinitions = [
  { label: "总 Key", key: "total_keys" },
  { label: "可用", key: "available_keys", tone: "success" },
  { label: "冷却", key: "cooling_keys", tone: "warning" },
  { label: "禁用", key: "disabled_keys", tone: "danger" },
  { label: "总请求", key: "total_requests" },
  { label: "成功数", key: "total_successes", tone: "success" },
  { label: "在途请求", key: "in_flight" },
  { label: "成功率", key: "success_rate", suffix: "%", tone: "accent" },
];

let timerId = null;
let pendingRequest = null;
let autoRefreshEnabled = true;

function formatCooldown(seconds) {
  const value = Number(seconds || 0);
  if (value <= 0) return "-";
  if (value < 60) return `${value}s`;
  return `${Math.floor(value / 60)}m ${value % 60}s`;
}

function renderSummary(data) {
  const summary = document.getElementById("summary");
  summary.replaceChildren(...summaryDefinitions.map((item) => createMetric({ ...item, value: data[item.key] })));
}

function renderPool(pool) {
  const body = document.getElementById("pool-body");
  body.replaceChildren();
  if (!pool.length) {
    const row = document.createElement("tr");
    const cell = createCell("没有载入任何 Key", "table-message");
    cell.colSpan = 10;
    row.append(cell);
    body.append(row);
    return;
  }

  for (const key of pool) {
    const row = document.createElement("tr");
    row.append(createCell(key.name, "key-name"));
    const statusCell = document.createElement("td");
    statusCell.append(createStatusPill(String(key.status || "").toLowerCase(), statusLabels[key.status] || key.status));
    row.append(statusCell);
    const prefixCell = createCell(key.key_prefix, "key-prefix");
    const code = document.createElement("code");
    code.textContent = prefixCell.textContent;
    prefixCell.replaceChildren(code);
    row.append(prefixCell);
    row.append(createCell(key.req_count, "numeric"));
    row.append(createCell(key.success_count, "numeric"));
    row.append(createCell(key.failure_count, "numeric"));
    row.append(createCell(key.rate_limit_count, "numeric"));
    const inFlightCell = document.createElement("td");
    inFlightCell.className = "numeric";
    inFlightCell.append(createStatusPill("in-flight", key.in_flight));
    row.append(inFlightCell);
    row.append(createCell(formatCooldown(key.cooldown_remaining_sec), "numeric"));
    const errorCell = createCell(key.last_error || "-", key.last_error ? "error-text" : "");
    errorCell.title = key.last_error || "";
    row.append(errorCell);
    body.append(row);
  }
}

function setConnectionState(state, text) {
  document.getElementById("health-dot").className = `health-dot ${state}`;
  document.getElementById("health-text").textContent = text;
}

async function updateStats() {
  if (pendingRequest) return pendingRequest;
  const button = document.getElementById("refresh-button");
  button.setAttribute("aria-busy", "true");
  pendingRequest = (async () => {
    try {
      const response = await fetch("/v1/status", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      renderSummary(data);
      renderPool(data.pool || []);
      document.getElementById("model-info").textContent = `默认：${data.default_model} · 可用：${(data.models || []).join(", ")} · 鉴权：${data.auth_enabled ? "已启用" : "仅本机"}`;
      const guideDefault = document.getElementById("guide-default-model");
      if (guideDefault) guideDefault.textContent = data.default_model;
      const guideModels = document.getElementById("guide-available-models");
      if (guideModels) guideModels.textContent = (data.models || []).join(", ");
      document.getElementById("updated-at").textContent = `最后更新 ${new Date().toLocaleTimeString()}`;
      setConnectionState(data.available_keys > 0 ? "ok" : "error", data.available_keys > 0 ? "代理运行正常" : "没有健康 Key");
    } catch (error) {
      setConnectionState("error", "无法连接代理");
      const body = document.getElementById("pool-body");
      const row = document.createElement("tr");
      const cell = createCell(error instanceof Error ? error.message : "未知错误", "table-message");
      cell.colSpan = 10;
      row.append(cell);
      body.replaceChildren(row);
    } finally {
      button.removeAttribute("aria-busy");
      pendingRequest = null;
      scheduleNextRefresh();
    }
  })();
  return pendingRequest;
}

function scheduleNextRefresh() {
  if (timerId) window.clearTimeout(timerId);
  timerId = autoRefreshEnabled && !document.hidden
    ? window.setTimeout(updateStats, REFRESH_INTERVAL)
    : null;
}

function syncAutoRefresh(enabled) {
  autoRefreshEnabled = enabled;
  scheduleNextRefresh();
}

document.getElementById("refresh-button").addEventListener("click", updateStats);
document.getElementById("auto-refresh").addEventListener("change", (event) => syncAutoRefresh(event.currentTarget.checked));
document.addEventListener("visibilitychange", () => {
  scheduleNextRefresh();
  if (!document.hidden && autoRefreshEnabled) updateStats();
});

syncAutoRefresh(true);
updateStats();
