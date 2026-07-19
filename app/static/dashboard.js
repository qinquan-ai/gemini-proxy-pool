import { createCell, createMetric, createStatusPill, defineUiComponents } from "/assets/ui/components.js";
import { initializeTheme } from "/assets/ui/theme.js";

defineUiComponents();
initializeTheme();

const REFRESH_INTERVAL = 3000;
const statusLabels = { Active: "可用", Cooling: "冷却", Disabled: "禁用" };
const jobStatusLabels = { queued: "排队", running: "运行中", completed: "完成", failed: "失败", cancelled: "已取消" };
const stageLabels = { queued: "等待", downloading: "下载", uploading: "上传", processing: "处理", analyzing: "分析", completed: "完成", failed: "失败", cancelled: "取消" };
const sourceLabels = { youtube: "YouTube", douyin: "抖音", bilibili: "哔哩哔哩", web: "网页", local: "本地", gemini: "Gemini 文件" };

let timerId = null;
let pendingRequest = null;
let autoRefreshEnabled = true;
let toastTimer = null;

function formatCooldown(seconds) {
  const value = Number(seconds || 0);
  if (value <= 0) return "-";
  if (value < 60) return `${value}s`;
  return `${Math.floor(value / 60)}m ${value % 60}s`;
}

function formatDuration(seconds) {
  const value = Number(seconds || 0);
  if (!value) return "-";
  const minutes = Math.floor(value / 60);
  const remainder = Math.round(value % 60);
  return minutes ? `${minutes}m ${remainder}s` : `${remainder}s`;
}

function formatElapsed(start, end) {
  if (!start || !end) return "-";
  return formatDuration(Math.max(0, Number(end) - Number(start)));
}

function formatCompactNumber(value) {
  return new Intl.NumberFormat("zh-CN", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(Number(value || 0));
}

function showToast(message) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.add("visible");
  if (toastTimer) window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 1800);
}

function renderSummary(data) {
  const jobs = data.video_jobs || {};
  const definitions = [
    { label: "总 Key", value: data.total_keys },
    { label: "健康 Key", value: data.available_keys, tone: "success" },
    { label: "总请求", value: data.total_requests },
    { label: "成功率", value: data.success_rate, suffix: "%", tone: "accent" },
    { label: "累计 Token", value: formatCompactNumber(data.total_tokens) },
    { label: "视频任务", value: jobs.total || 0 },
    { label: "分析中", value: jobs.running || 0, tone: "warning" },
    { label: "任务失败", value: jobs.failed || 0, tone: "danger" },
  ];
  document.getElementById("summary").replaceChildren(...definitions.map(createMetric));
}

function renderPool(pool) {
  const body = document.getElementById("pool-body");
  body.replaceChildren();
  if (!pool.length) {
    const row = document.createElement("tr");
    const cell = createCell("没有载入任何 Key", "table-message");
    cell.colSpan = 11;
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
    const tokenCell = createCell(formatCompactNumber(key.total_tokens), "numeric token-count");
    tokenCell.title = `输入 ${key.input_tokens || 0} · 输出 ${key.output_tokens || 0} · 思考 ${key.thought_tokens || 0} · 缓存 ${key.cached_tokens || 0}`;
    row.append(tokenCell);
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

function renderJobs(data) {
  const body = document.getElementById("jobs-body");
  const jobs = data.recent_video_jobs || [];
  const counts = data.video_jobs || {};
  document.getElementById("video-job-summary").textContent = `共 ${counts.total || 0} 个 · 运行 ${counts.running || 0} · 完成 ${counts.completed || 0} · 失败 ${counts.failed || 0}`;
  document.getElementById("source-badges").replaceChildren(...(data.supported_video_sources || []).map((source) => {
    const badge = document.createElement("span");
    badge.textContent = source;
    return badge;
  }));
  body.replaceChildren();
  if (!jobs.length) {
    const row = document.createElement("tr");
    const cell = createCell("尚无视频分析任务", "table-message");
    cell.colSpan = 8;
    row.append(cell);
    body.append(row);
    return;
  }

  for (const job of jobs) {
    const row = document.createElement("tr");
    const taskCell = document.createElement("td");
    const taskTitle = document.createElement("strong");
    taskTitle.className = "job-title";
    taskTitle.textContent = job.title || job.id;
    taskTitle.title = job.title || job.id;
    const taskId = document.createElement("code");
    taskId.className = "job-id";
    taskId.textContent = job.id;
    taskCell.append(taskTitle, taskId);
    row.append(taskCell);
    row.append(createCell(sourceLabels[job.source_type] || job.source_type));
    row.append(createCell(job.analysis_type));
    const statusCell = document.createElement("td");
    statusCell.append(createStatusPill(`job-${job.status}`, jobStatusLabels[job.status] || job.status));
    row.append(statusCell);
    const progressCell = document.createElement("td");
    const progress = Math.round(Number(job.progress || 0) * 100);
    const progressLabel = document.createElement("div");
    progressLabel.className = "progress-label";
    const stage = document.createElement("span");
    stage.textContent = stageLabels[job.stage] || job.stage;
    const percentage = document.createElement("span");
    percentage.textContent = `${progress}%`;
    progressLabel.append(stage, percentage);
    const progressTrack = document.createElement("div");
    progressTrack.className = "progress-track";
    const progressFill = document.createElement("span");
    progressFill.style.width = `${Math.max(0, Math.min(100, progress))}%`;
    progressTrack.append(progressFill);
    progressCell.append(progressLabel, progressTrack);
    row.append(progressCell);
    const modelCell = document.createElement("td");
    const model = document.createElement("code");
    model.textContent = job.model || "-";
    const key = document.createElement("small");
    key.textContent = job.key_name || "尚未分配 Key";
    modelCell.append(model, key);
    row.append(modelCell);
    row.append(createCell(formatElapsed(job.created_at, job.updated_at)));
    const errorCell = createCell(job.error || "-", job.error ? "error-text" : "");
    errorCell.title = job.error || "";
    row.append(errorCell);
    body.append(row);
  }
}

function renderIntegration(data) {
  const origin = window.location.origin;
  const openAiBase = `${origin}/v1`;
  document.getElementById("api-base-url").textContent = openAiBase;
  document.getElementById("gemini-base-url").textContent = origin;
  document.getElementById("mcp-url").textContent = `${origin}/mcp/`;
  document.getElementById("guide-default-model").textContent = data.default_model;
  document.getElementById("guide-available-models").textContent = (data.models || []).join(", ");
  document.getElementById("client-key-guide").textContent = data.auth_enabled ? "填写已配置的代理 Token" : "仅本机时可填任意字符";
  document.getElementById("powershell-command").textContent = `Invoke-RestMethod -Uri ${openAiBase}/chat/completions -Method Post -ContentType "application/json" -Body '{"model":"${data.default_model}","messages":[{"role":"user","content":"只回复：连接成功"}]}'`;
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
      renderJobs(data);
      renderIntegration(data);
      document.getElementById("model-info").textContent = `默认 ${data.default_model} · ${data.available_keys}/${data.total_keys} Key 可用 · ${data.auth_enabled ? "Token 鉴权" : "仅本机"}`;
      document.getElementById("updated-at").textContent = `最后更新 ${new Date().toLocaleTimeString()}`;
      setConnectionState(data.available_keys > 0 ? "ok" : "error", data.available_keys > 0 ? "网关运行正常" : "没有健康 Key");
    } catch (error) {
      setConnectionState("error", "无法连接网关");
      for (const [bodyId, columns] of [["pool-body", 11], ["jobs-body", 8]]) {
        const row = document.createElement("tr");
        const cell = createCell(error instanceof Error ? error.message : "未知错误", "table-message");
        cell.colSpan = columns;
        row.append(cell);
        document.getElementById(bodyId).replaceChildren(row);
      }
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
  timerId = autoRefreshEnabled && !document.hidden ? window.setTimeout(updateStats, REFRESH_INTERVAL) : null;
}

document.getElementById("refresh-button").addEventListener("click", updateStats);
document.getElementById("auto-refresh").addEventListener("change", (event) => {
  autoRefreshEnabled = event.currentTarget.checked;
  scheduleNextRefresh();
});
document.querySelectorAll("[data-copy-target]").forEach((button) => button.addEventListener("click", async () => {
  const target = document.getElementById(button.dataset.copyTarget);
  if (!target) return;
  await navigator.clipboard.writeText(target.textContent.trim());
  showToast("已复制");
}));
document.addEventListener("visibilitychange", () => {
  scheduleNextRefresh();
  if (!document.hidden && autoRefreshEnabled) updateStats();
});

updateStats();
