import {
  defineUiComponents,
  createCell,
  createStatusPill,
  createKeyCard,
} from "/assets/ui/index.js";
import { initializeTheme } from "/assets/ui/theme.js";

// 注册所有 UI Web Components 并初始化主题
defineUiComponents();
initializeTheme();

const REFRESH_INTERVAL = 3000;
let timerId = null;
let pendingRequest = null;
let autoRefreshEnabled = true;
let toastTimer = null;
let latestData = null;
let activeKeyName = null;
const expandedKeys = new Set();

function formatCompactNumber(value) {
  return new Intl.NumberFormat("zh-CN", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(Number(value || 0));
}

function showToast(message) {
  const toast = document.getElementById("toast");
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add("visible");
  if (toastTimer) window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 1800);
}

function openDrawerForKey(key) {
  activeKeyName = key.name;
  const drawer = document.getElementById("key-detail-drawer");
  if (!drawer) return;
  drawer.open(key, latestData?.recent_traces || [], {
    onRefresh: updateStats,
  });
}

function openVerificationModal(keyName = null) {
  const modal = document.getElementById("verification-modal");
  if (!modal) return;
  if (latestData) {
    modal.setPoolData(latestData.pool || [], latestData.models || []);
  }
  modal.open(keyName);
}

function renderKeyCards(data) {
  const container = document.getElementById("cards-container");
  if (!container) return;
  const pool = data.pool || [];

  // 保存当前用户展开的折叠面板状态，防止 3 秒轮询重绘时自动闭合
  container.querySelectorAll("key-card").forEach((card) => {
    const details = card.querySelector("details.model-collapse-box");
    if (details && card.keyData?.name) {
      if (details.open) expandedKeys.add(card.keyData.name);
      else expandedKeys.delete(card.keyData.name);
    }
  });

  container.replaceChildren();

  if (!pool.length) {
    const empty = document.createElement("div");
    empty.className = "table-message";
    empty.style.gridColumn = "1 / -1";
    empty.textContent = "当前号池未载入任何有效 API Key";
    container.append(empty);
    return;
  }

  const resetText = data.quota_reset_in_hours ? `${data.quota_reset_in_hours}h 后 (16:00 刷新)` : "";

  for (const key of pool) {
    const isExpanded = expandedKeys.has(key.name);
    const cardEl = createKeyCard(
      key,
      resetText,
      {
        onRefresh: updateStats,
        onOpenDetail: (k) => openDrawerForKey(k),
        onOpenVerification: (kName) => openVerificationModal(kName),
        onToggleExpand: (name, open) => {
          if (open) expandedKeys.add(name);
          else expandedKeys.delete(name);
        },
      },
      isExpanded
    );
    container.append(cardEl);
  }
}

function renderModelQuotaStrip(data) {
  const container = document.getElementById("model-quota-strip");
  if (!container) return;
  const items = (data.model_summary || []).filter((item) => item.primary);
  container.replaceChildren();
  if (!items.length) {
    container.style.display = "none";
    return;
  }
  container.style.display = "";
  for (const item of items) {
    const card = document.createElement("div");
    card.className = `model-chip ${item.state || "available"}`;
    card.title =
      `${item.model}\n今日已用 ${item.used} / 上限 ${item.limit}\n` +
      `可用 Key ${item.available_keys} 个，冷却 ${item.cooling_keys} 个\n` +
      `在途 ${item.in_flight}，今日 429 ${item.rate_limited_today} 次，超时 ${item.timeout_today} 次`;

    const head = document.createElement("div");
    head.className = "model-chip-head";
    const name = document.createElement("strong");
    name.textContent = item.model.replace(/^gemini-/, "");
    const left = document.createElement("span");
    left.textContent = `剩 ${item.remaining}`;
    head.append(name, left);

    const meta = document.createElement("div");
    meta.className = "model-chip-meta";
    meta.textContent = `${item.used}/${item.limit} RPD · Key ${item.available_keys} 可用`;

    const track = document.createElement("div");
    track.className = "model-chip-track";
    const fill = document.createElement("div");
    fill.className = "model-chip-fill";
    fill.style.width = `${item.limit > 0 ? Math.min(100, Math.round((item.used / item.limit) * 100)) : 0}%`;
    track.append(fill);

    card.append(head, meta, track);
    container.append(card);
  }
}

function renderChatPanel(data) {
  const chatPanel = document.getElementById("prompt-chat-panel");
  if (!chatPanel) return;
  chatPanel.setPoolKeys(data.pool || [], data.models || ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.1-flash-lite"]);
  chatPanel.onCompleted = () => {
    // 聊天发送完毕后立即触发一次刷新，让指标与请求流水实时更新
    setTimeout(updateStats, 300);
  };
}

function renderTraces(data) {
  const body = document.getElementById("traces-body");
  const traces = data.recent_traces || [];

  let nativeCount = 0;
  let openaiCount = 0;
  for (const t of traces) {
    if (t.protocol === "Google Native") nativeCount++;
    else openaiCount++;
  }

  document.getElementById("traces-summary").textContent = `实时追踪最近 ${traces.length} 笔调用 · 网关历史累计 ${data.total_requests || 0} 次`;

  const badgesContainer = document.getElementById("trace-badges");
  badgesContainer.replaceChildren();
  const b1 = document.createElement("span");
  b1.textContent = `Native: ${nativeCount}`;
  const b2 = document.createElement("span");
  b2.textContent = `OpenAI: ${openaiCount}`;
  badgesContainer.append(b1, b2);

  body.replaceChildren();
  if (!traces.length) {
    const row = document.createElement("tr");
    const cell = createCell("尚无调用审计流水", "table-message");
    cell.colSpan = 9;
    row.append(cell);
    body.append(row);
    return;
  }

  for (const trace of traces) {
    const row = document.createElement("tr");
    row.append(createCell(trace.timestamp || "-"));

    const protoCell = document.createElement("td");
    const isNative = trace.protocol === "Google Native";
    protoCell.append(createStatusPill(isNative ? "native" : "openai", trace.protocol));
    row.append(protoCell);

    const modelCell = document.createElement("td");
    const modelTag = document.createElement("strong");
    modelTag.className = "job-title";
    modelTag.textContent = trace.model || "-";
    const epTag = document.createElement("code");
    epTag.className = "job-id";
    epTag.textContent = trace.endpoint || "-";
    modelCell.append(modelTag, epTag);
    row.append(modelCell);

    row.append(createCell(trace.key_name || "-"));

    const statusCell = document.createElement("td");
    const code = Number(trace.status_code || 0);
    const pillClass = code === 200 ? "job-completed" : code >= 500 ? "job-failed" : "job-running";
    statusCell.append(createStatusPill(pillClass, String(code || "-")));
    row.append(statusCell);

    row.append(createCell(`${Math.round(trace.latency_ms || 0)}ms`, "numeric"));

    const totalTok = trace.tokens?.totalTokenCount || trace.tokens?.total_tokens;
    row.append(createCell(totalTok ? formatCompactNumber(totalTok) : "-", "numeric"));

    const modeMap = {
      single: "单模型",
      family: "数组降级",
      "lock-key": "锁 Key",
      "lock-key+switch": "锁 Key + 数组",
      "fail-fast": "Fail-Fast",
    };
    const modeLabel = modeMap[trace.route_mode] || trace.route_mode || "-";
    row.append(createCell(modeLabel));

    const errorCell = createCell(trace.error || "正常", trace.error ? "error-text" : "success-text");
    errorCell.title = trace.error || "";
    row.append(errorCell);

    body.append(row);
  }
}

function renderIntegration(data) {
  const origin = window.location.origin;
  const openAiBase = `${origin}/v1`;
  document.getElementById("api-base-url").textContent = openAiBase;
  document.getElementById("gemini-base-url").textContent = origin;
  document.getElementById("traces-url").textContent = `${origin}/v1/traces`;
  const availModelsEl = document.getElementById("guide-available-models");
  if (availModelsEl) {
    const listStr = (data.models || []).join(", ");
    availModelsEl.textContent = listStr;
    availModelsEl.title = `支持模型 (${(data.models || []).length} 个):\n${listStr}\n(鼠标悬浮完整查看)`;
  }
  document.getElementById("client-key-guide").textContent = data.auth_enabled ? "填写已配置的代理 Token" : "仅本机时可填任意字符";
  document.getElementById("powershell-command").textContent = `Invoke-RestMethod -Uri ${openAiBase}/chat/completions -Method Post -ContentType "application/json" -Body '{"model":"${data.default_model}","messages":[{"role":"user","content":"只回复：连接成功"}]}'`;
}

function setConnectionState(state, text) {
  const dot = document.getElementById("health-dot");
  const txt = document.getElementById("health-text");
  if (dot) dot.className = `health-dot ${state}`;
  if (txt) txt.textContent = text;
}

async function updateStats() {
  if (pendingRequest) return pendingRequest;
  const button = document.getElementById("refresh-button");
  if (button) button.setAttribute("aria-busy", "true");

  pendingRequest = (async () => {
    try {
      const response = await fetch("/v1/status", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      latestData = data;

      renderKeyCards(data);
      renderModelQuotaStrip(data);
      renderChatPanel(data);
      renderTraces(data);
      renderIntegration(data);

      const drawer = document.getElementById("key-detail-drawer");
      if (drawer && drawer.classList.contains("open") && activeKeyName) {
        const currentKey = (data.pool || []).find((k) => k.name === activeKeyName);
        if (currentKey) {
          drawer.renderData(currentKey, data.recent_traces || []);
        }
      }

      const verifyModal = document.getElementById("verification-modal");
      if (verifyModal) {
        verifyModal.setPoolData(data.pool || [], data.models || []);
      }

      const modelInfo = document.getElementById("model-info");
      if (modelInfo) {
        const limitMode = { off: "限速关", shadow: "限速观察", enforce: "限速生效" }[data.rate_limit_mode] || data.rate_limit_mode;
      modelInfo.textContent = `默认 ${data.default_model} · ${data.available_keys}/${data.total_keys} Key 可用 · ${limitMode} · ${data.auth_enabled ? "Token 鉴权" : "仅本机免密"}`;
      }
      const updatedAt = document.getElementById("updated-at");
      if (updatedAt) {
        updatedAt.textContent = `最后更新 ${new Date().toLocaleTimeString()}`;
      }
      setConnectionState(data.available_keys > 0 ? "ok" : "error", data.available_keys > 0 ? "正常" : "无可用 Key");
    } catch (error) {
      setConnectionState("error", "连接失败");
      const cardsContainer = document.getElementById("cards-container");
      if (cardsContainer) {
        cardsContainer.innerHTML = `<div class="table-message" style="grid-column: 1 / -1; color: var(--danger);">${error instanceof Error ? error.message : "无法连接网关"}</div>`;
      }
      const tracesBody = document.getElementById("traces-body");
      if (tracesBody) {
        const row = document.createElement("tr");
        const cell = createCell(error instanceof Error ? error.message : "未知错误", "table-message");
        cell.colSpan = 9;
        row.append(cell);
        tracesBody.replaceChildren(row);
      }
    } finally {
      if (button) button.removeAttribute("aria-busy");
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

document.getElementById("verify-modal-btn")?.addEventListener("click", () => openVerificationModal());
document.getElementById("refresh-button")?.addEventListener("click", updateStats);
document.getElementById("auto-refresh")?.addEventListener("change", (event) => {
  autoRefreshEnabled = event.currentTarget.checked;
  scheduleNextRefresh();
});
document.querySelectorAll("[data-copy-target]").forEach((button) =>
  button.addEventListener("click", async () => {
    const target = document.getElementById(button.dataset.copyTarget);
    if (!target) return;
    await navigator.clipboard.writeText(target.textContent.trim());
    showToast("已复制");
  })
);
document.addEventListener("visibilitychange", () => {
  scheduleNextRefresh();
  if (!document.hidden && autoRefreshEnabled) updateStats();
});

// 立即载入
updateStats();
