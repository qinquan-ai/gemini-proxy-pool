import { createQuotaBar } from "../chart/QuotaBar.js";
import { createStatusPill } from "../tag/GlassTag.js";

/**
 * KeyDrawer - Key 多维详情与运维操作抽屉
 * 对应 TransFlow 的 components/dialog
 */
export class KeyDrawer extends HTMLElement {
  connectedCallback() {
    if (!this.querySelector(".drawer-backdrop")) {
      this.innerHTML = `
        <div class="drawer-backdrop"></div>
        <aside class="drawer-panel" role="dialog" aria-modal="true" aria-labelledby="drawer-key-name">
          <div class="drawer-header">
            <div>
              <div class="drawer-title-row">
                <h2 id="drawer-key-name" class="drawer-key-title">Key 详情</h2>
                <span id="drawer-status-pill"></span>
              </div>
              <div class="drawer-key-sub">
                <code id="drawer-key-prefix">AIza...</code>
                <button id="drawer-copy-key" class="icon-copy-btn mini" type="button" title="复制前缀">&#10697;</button>
              </div>
            </div>
            <button id="drawer-close-btn" class="icon-button" type="button" title="关闭" aria-label="关闭">&#10005;</button>
          </div>
          <div class="drawer-content">
            <!-- 运维操作区 -->
            <section class="drawer-section">
              <h3 class="drawer-section-title">运维操作</h3>
              <div class="drawer-actions-grid">
                <button id="action-ping-btn" class="drawer-btn" type="button">⚡ Ping 测速</button>
                <button id="action-unfreeze-btn" class="drawer-btn" type="button">❄️ 解冻</button>
                <button id="action-toggle-btn" class="drawer-btn" type="button">🚫 启停</button>
              </div>
              <div id="drawer-action-feedback" class="action-feedback"></div>
            </section>

            <!-- 模型配额 -->
            <section class="drawer-section">
              <div class="drawer-section-head">
                <h3 class="drawer-section-title">模型日配额 (RPD)</h3>
                <span id="drawer-quota-total" class="drawer-section-badge">0 / 0</span>
              </div>
              <div id="drawer-quotas-list" class="quotas-list"></div>
            </section>

            <!-- Token 细分资产 -->
            <section class="drawer-section">
              <h3 class="drawer-section-title">Token 细分统计</h3>
              <div id="drawer-tokens-grid" class="drawer-metrics-grid"></div>
            </section>

            <!-- 请求频次与并发统计 -->
            <section class="drawer-section">
              <h3 class="drawer-section-title">请求频次与并发指标 (今日 / 累计)</h3>
              <div id="drawer-traffic-grid" class="drawer-metrics-grid"></div>
            </section>

            <!-- 专属请求流水 -->
            <section class="drawer-section">
              <div class="drawer-section-head">
                <h3 class="drawer-section-title">最近命中流水 (专属)</h3>
                <span id="drawer-traces-count" class="drawer-section-badge">0 笔</span>
              </div>
              <div class="drawer-traces-container">
                <table class="drawer-traces-table">
                  <thead><tr><th>时间</th><th>端点/模型</th><th>状态码</th><th>耗时</th><th>结果</th></tr></thead>
                  <tbody id="drawer-traces-body"></tbody>
                </table>
              </div>
            </section>
          </div>
        </aside>
      `;
      this.setupEvents();
    }
  }

  setupEvents() {
    this.querySelector(".drawer-backdrop")?.addEventListener("click", () => this.close());
    this.querySelector("#drawer-close-btn")?.addEventListener("click", () => this.close());
    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this.classList.contains("open")) this.close();
    });
  }

  open(keyData, traces = [], callbacks = {}) {
    this.currentKey = keyData;
    this.callbacks = callbacks;
    this.classList.add("open");
    document.body.style.overflow = "hidden";
    this.renderData(keyData, traces);
  }

  close() {
    this.classList.remove("open");
    document.body.style.overflow = "";
  }

  renderData(key, allTraces = []) {
    const nameEl = this.querySelector("#drawer-key-name");
    if (nameEl) nameEl.textContent = key.name || "Unnamed";

    const pillContainer = this.querySelector("#drawer-status-pill");
    if (pillContainer) {
      pillContainer.replaceChildren(createStatusPill(String(key.status || "").toLowerCase(), key.status));
    }

    const prefixEl = this.querySelector("#drawer-key-prefix");
    if (prefixEl) prefixEl.textContent = key.key_prefix || "-";

    const copyBtn = this.querySelector("#drawer-copy-key");
    if (copyBtn) {
      copyBtn.onclick = async () => {
        await navigator.clipboard.writeText(key.key_prefix || "");
        this.showFeedback("已复制脱敏 Key 前缀", "info");
      };
    }

    const pingBtn = this.querySelector("#action-ping-btn");
    const unfreezeBtn = this.querySelector("#action-unfreeze-btn");
    const toggleBtn = this.querySelector("#action-toggle-btn");

    if (pingBtn) {
      pingBtn.onclick = async () => {
        pingBtn.disabled = true;
        this.showFeedback("正在测速与验证 Google 端点连通性...", "info");
        try {
          const res = await fetch(`/v1/keys/${encodeURIComponent(key.name)}/ping`, { method: "POST" });
          const json = await res.json();
          if (res.ok && json.status === "ok") {
            this.showFeedback(`连通性正常: 响应延时 ${json.latency_ms}ms`, "success");
          } else {
            this.showFeedback(`连通失败: ${json.message || "HTTP " + json.status_code}`, "danger");
          }
        } catch (e) {
          this.showFeedback(`测速异常: ${e.message}`, "danger");
        } finally {
          pingBtn.disabled = false;
        }
      };
    }

    if (unfreezeBtn) {
      unfreezeBtn.disabled = key.status !== "Cooling";
      unfreezeBtn.onclick = async () => {
        unfreezeBtn.disabled = true;
        try {
          const res = await fetch(`/v1/keys/${encodeURIComponent(key.name)}/unfreeze`, { method: "POST" });
          const json = await res.json();
          if (res.ok) {
            this.showFeedback(`解冻成功: ${json.message}`, "success");
            if (this.callbacks.onRefresh) this.callbacks.onRefresh();
          } else {
            this.showFeedback(`解冻失败: ${json.detail || "未知错误"}`, "danger");
          }
        } catch (e) {
          this.showFeedback(`请求失败: ${e.message}`, "danger");
        } finally {
          unfreezeBtn.disabled = false;
        }
      };
    }

    if (toggleBtn) {
      toggleBtn.textContent = key.disabled ? "✅ 恢复启用" : "🚫 停用此 Key";
      toggleBtn.onclick = async () => {
        toggleBtn.disabled = true;
        try {
          const res = await fetch(`/v1/keys/${encodeURIComponent(key.name)}/toggle`, { method: "POST" });
          const json = await res.json();
          if (res.ok) {
            this.showFeedback(json.message, "success");
            if (this.callbacks.onRefresh) this.callbacks.onRefresh();
          } else {
            this.showFeedback(`操作失败: ${json.detail || "未知错误"}`, "danger");
          }
        } catch (e) {
          this.showFeedback(`请求失败: ${e.message}`, "danger");
        } finally {
          toggleBtn.disabled = false;
        }
      };
    }

    const quotaList = this.querySelector("#drawer-quotas-list");
    const quotaTotal = this.querySelector("#drawer-quota-total");
    if (quotaList) {
      quotaList.replaceChildren();
      const models = key.models || {};
      const modelKeys = Object.keys(models);
      if (quotaTotal) {
        quotaTotal.textContent = `当日累计 ${key.used_today_total || 0} / ${key.limit_rpd_total || 0} RPD`;
      }
      if (!modelKeys.length) {
        quotaList.innerHTML = `<div class="empty-hint">暂无模型配额数据</div>`;
      } else {
        for (const m of modelKeys) {
          const mData = models[m];
          quotaList.append(createQuotaBar({
            model: m,
            used: mData.used_today,
            limit: mData.limit_rpd,
            limit_tpd: mData.limit_tpd,
            used_tokens: mData.used_tokens_today,
            status: mData.status,
            cooldown: mData.cooldown_remaining_sec,
          }));
        }
      }
    }

    const tokensGrid = this.querySelector("#drawer-tokens-grid");
    if (tokensGrid) {
      tokensGrid.replaceChildren(
        this.createTokenBox("今日 Token", key.today_total_tokens || 0, "accent"),
        this.createTokenBox("累计历史 Token", key.total_tokens || 0),
        this.createTokenBox("输入 Token", key.input_tokens || 0),
        this.createTokenBox("输出 Token", key.output_tokens || 0),
        this.createTokenBox("思考 Token", key.thought_tokens || 0, "warning"),
        this.createTokenBox("缓存 Token", key.cached_tokens || 0, "success"),
      );
    }

    const trafficGrid = this.querySelector("#drawer-traffic-grid");
    if (trafficGrid) {
      trafficGrid.replaceChildren(
        this.createTokenBox("今日请求", key.today_req_count || 0, "accent"),
        this.createTokenBox("历史累计请求", key.req_count || 0),
        this.createTokenBox("今日成功", key.today_success_count || 0, "success"),
        this.createTokenBox("历史累计成功", key.success_count || 0, "success"),
        this.createTokenBox("今日失败 (限流)", `${key.today_failure_count || 0} (${key.today_rate_limit_count || 0})`, (key.today_failure_count || 0) > 0 ? "danger" : ""),
        this.createTokenBox("历史失败 (限流)", `${key.failure_count || 0} (${key.rate_limit_count || 0})`, (key.failure_count || 0) > 0 ? "danger" : ""),
        this.createTokenBox("今日峰值 TPM", key.today_peak_tpm ? `${this.formatCompact(key.today_peak_tpm)}` : (key.current_tpm ? `${this.formatCompact(key.current_tpm)}` : "0"), "accent"),
        this.createTokenBox("当前瞬时 TPM", key.current_tpm ? `${this.formatCompact(key.current_tpm)}` : "0"),
      );
    }

    const tracesBody = this.querySelector("#drawer-traces-body");
    const tracesCount = this.querySelector("#drawer-traces-count");
    if (tracesBody) {
      tracesBody.replaceChildren();
      const matched = allTraces.filter(t => t.key_name === key.name);
      if (tracesCount) tracesCount.textContent = `${matched.length} 笔`;
      if (!matched.length) {
        tracesBody.innerHTML = `<tr><td colspan="5" class="table-message">最近尚无命中此 Key 的请求流水</td></tr>`;
      } else {
        for (const t of matched) {
          const tr = document.createElement("tr");
          const tdTime = document.createElement("td");
          tdTime.textContent = t.timestamp || "-";
          tr.append(tdTime);
          
          const endCell = document.createElement("td");
          const endCode = document.createElement("code");
          endCode.textContent = t.model || t.endpoint || "-";
          endCell.append(endCode);
          tr.append(endCell);

          const code = Number(t.status_code || 0);
          const pillClass = code === 200 ? "job-completed" : (code >= 500 ? "job-failed" : "job-running");
          const statusCell = document.createElement("td");
          statusCell.append(createStatusPill(pillClass, String(code || "-")));
          tr.append(statusCell);

          const tdLat = document.createElement("td");
          tdLat.className = "numeric";
          tdLat.textContent = `${Math.round(t.latency_ms || 0)}ms`;
          tr.append(tdLat);

          const errCell = document.createElement("td");
          errCell.className = t.error ? "error-text" : "success-text";
          errCell.textContent = t.error ? t.error.slice(0, 40) : "成功";
          errCell.title = t.error || "";
          tr.append(errCell);

          tracesBody.append(tr);
        }
      }
    }
  }

  createTokenBox(label, value, tone = "") {
    const box = document.createElement("div");
    box.className = `metric-box ${tone}`;
    const l = document.createElement("div");
    l.className = "metric-box-label";
    l.textContent = label;
    const v = document.createElement("div");
    v.className = "metric-box-val";
    v.textContent = typeof value === "number" ? new Intl.NumberFormat("zh-CN").format(value) : String(value ?? "-");
    box.append(l, v);
    return box;
  }

  formatCompact(val) {
    return new Intl.NumberFormat("zh-CN", {
      notation: "compact",
      maximumFractionDigits: 1,
    }).format(Number(val || 0));
  }

  showFeedback(message, type = "info") {
    const box = this.querySelector("#drawer-action-feedback");
    if (!box) return;
    box.textContent = message;
    box.className = `action-feedback ${type} visible`;
    setTimeout(() => {
      box.classList.remove("visible");
    }, 4000);
  }
}
