/**
 * VerificationModal - 账号与模型批量测活控制台
 * 支持多账号筛选、多模型列表多选（全选开关）、Health 0配额探针与真实请求探针、实时矩阵测活与明细审计
 */
export class VerificationModal extends HTMLElement {
  constructor() {
    super();
    this.availableKeys = [];
    this.availableModels = [
      "gemini-3.8-flash",
      "gemini-3.7-flash",
      "gemini-3.6-flash",
      "gemini-3.5-flash",
      "gemini-3.5-flash-lite",
      "gemini-3.1-flash-lite",
      "gemini-3-flash-preview",
      "gemini-2.5-flash",
      "gemini-2.5-flash-lite",
      "gemma-4-31b-it",
    ];
    this.selectedKeys = new Set();
    this.selectedModels = new Set(this.availableModels);
    this.probeType = "ping"; // 默认使用不扣额度的 health 探针
    this.keySearchQuery = "";
    this.modelSearchQuery = "";
    this.running = false;
    this.lastRecords = [];
    this.resultFilter = "all";
  }

  connectedCallback() {
    this.render();
    this.setupEvents();
  }

  setPoolData(keys = [], models = []) {
    if (keys && keys.length) {
      this.availableKeys = keys;
      if (!this.selectedKeys.size) {
        this.selectedKeys = new Set(keys.map((k) => k.name));
      }
    }
    if (models && models.length) {
      this.availableModels = Array.from(new Set([...models, ...this.availableModels]));
      if (!this.selectedModels.size) {
        this.selectedModels = new Set(this.availableModels);
      }
    }
    this.renderKeysChecklist();
    this.renderModelsChecklist();
    this.updateCounters();
  }

  open(preselectedKeyName = null) {
    this.classList.add("is-open");
    document.body.style.overflow = "hidden";
    if (preselectedKeyName) {
      this.selectedKeys = new Set([preselectedKeyName]);
    } else if (!this.selectedKeys.size && this.availableKeys.length) {
      this.selectedKeys = new Set(this.availableKeys.map((k) => k.name));
    }
    this.updateCounters();
    this.renderKeysChecklist();
    this.renderModelsChecklist();
  }

  close() {
    if (this.running) return;
    this.classList.remove("is-open");
    document.body.style.overflow = "";
  }

  render() {
    this.innerHTML = `
      <div class="verification-backdrop"></div>
      <div class="verification-dialog direct-verify-dialog" role="dialog" aria-modal="true" aria-labelledby="verify-dialog-title">
        <!-- 弹窗顶栏 -->
        <div class="verification-dialog-header">
          <div class="header-brand-wrap">
            <span class="verify-shield-icon">🛡️</span>
            <div>
              <h2 id="verify-dialog-title" class="verify-dialog-title">批量测活</h2>
              <p class="verify-dialog-sub">支持按账号与模型筛选验证，提供健康探针与真实生成探测</p>
            </div>
          </div>
          <button id="modal-close-btn" class="icon-button" type="button" title="关闭" aria-label="关闭">&#10005;</button>
        </div>

        <div class="verify-console-body">
          <!-- 区域 1: 账号筛选与选择 -->
          <div class="verify-section">
            <div class="verify-section-header">
              <label class="section-title-label">
                <span>账号</span>
                <span class="count-tag" id="keys-count-tag">(已选 0 / 0)</span>
              </label>
              <div class="header-tools">
                <input type="text" id="key-search-input" class="search-input-sm" placeholder="搜索账号..." />
                <label class="custom-checkbox-label">
                  <input type="checkbox" id="check-all-keys" checked />
                  <span>全选</span>
                </label>
              </div>
            </div>
            <div id="keys-checklist" class="keys-checklist-grid"></div>
          </div>

          <!-- 区域 2: 目标模型列表多选 (非下拉框，带全选开关) -->
          <div class="verify-section">
            <div class="verify-section-header">
              <label class="section-title-label">
                <span>模型</span>
                <span class="count-tag" id="models-count-tag">(已选 0 / 0)</span>
              </label>
              <div class="header-tools">
                <label class="custom-checkbox-label">
                  <input type="checkbox" id="check-all-models" checked />
                  <span>全选</span>
                </label>
              </div>
            </div>
            <div id="models-checklist" class="models-checklist-grid"></div>
          </div>

          <!-- 区域 3: 探测方式选择 -->
          <div class="verify-section probe-section">
            <div class="section-title-label" style="margin-bottom: 0.5rem;">探测方式</div>
            <div class="probe-options-group">
              <label class="probe-option-card is-recommended">
                <input type="radio" name="probe-type-radio" value="ping" checked />
                <div class="probe-option-text">
                  <div class="probe-option-title">
                    <strong>Health 探针</strong>
                    <span class="badge-free">0 配额消耗</span>
                  </div>
                  <div class="probe-option-desc">
                    通过 GET /v1beta/models/{model} 查询模型权限与状态，不扣除 RPD 或 Token 额度。
                  </div>
                </div>
              </label>

              <label class="probe-option-card">
                <input type="radio" name="probe-type-radio" value="generate" />
                <div class="probe-option-text">
                  <div class="probe-option-title">
                    <strong>Generate 探针</strong>
                    <span class="badge-consume">消耗 1 RPD</span>
                  </div>
                  <div class="probe-option-desc">
                    发送微量 Prompt 验证真实端到端生成响应，消耗 1 次 RPD 额度。
                  </div>
                </div>
              </label>
            </div>
          </div>

          <!-- 区域 4: 操作栏 -->
          <div class="verify-actions-bar">
            <div class="actions-meta" id="verify-meta-summary">
              预计执行 0 个组合探测
            </div>
            <div class="actions-buttons">
              <button id="btn-clear-results" class="gbtn gbtn-sm" type="button" disabled>清空结果</button>
              <button id="btn-start-verify" class="gbtn gbtn-sm gbtn-primary" type="button">
                <span id="start-btn-icon">⚡</span>
                <span id="start-btn-text">开始测活</span>
              </button>
            </div>
          </div>

          <!-- 区域 5: 测活结果展示 -->
          <div class="verify-section results-section">
            <div class="results-header">
              <div class="section-title-label">测活结果</div>
              <div class="detail-filters-row" id="results-filter-row">
                <button type="button" class="detail-filter-pill is-active" data-filter="all">全部 (0)</button>
                <button type="button" class="detail-filter-pill" data-filter="success">成功 (0)</button>
                <button type="button" class="detail-filter-pill" data-filter="cooling">限流 (0)</button>
                <button type="button" class="detail-filter-pill" data-filter="failed">失败 (0)</button>
              </div>
            </div>

            <div class="results-table-wrap">
              <table class="verification-table">
                <thead>
                  <tr>
                    <th>账号</th>
                    <th>Key 前缀</th>
                    <th>模型</th>
                    <th>方式</th>
                    <th>状态</th>
                    <th>耗时 (ms)</th>
                    <th>HTTP</th>
                    <th>明细</th>
                  </tr>
                </thead>
                <tbody id="verify-results-tbody">
                  <tr>
                    <td colspan="8" class="verification-empty-cell">
                      勾选账号与模型后，点击【开始测活】执行检测
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    `;
  }

  setupEvents() {
    this.querySelector(".verification-backdrop").onclick = () => this.close();
    this.querySelector("#modal-close-btn").onclick = () => this.close();

    // 账号全选切换
    const checkAllKeys = this.querySelector("#check-all-keys");
    checkAllKeys.onchange = (e) => {
      const filtered = this.getFilteredKeys();
      if (e.target.checked) {
        filtered.forEach((k) => this.selectedKeys.add(k.name));
      } else {
        filtered.forEach((k) => this.selectedKeys.delete(k.name));
      }
      this.updateCounters();
      this.renderKeysChecklist();
    };

    // 模型全选切换
    const checkAllModels = this.querySelector("#check-all-models");
    checkAllModels.onchange = (e) => {
      if (e.target.checked) {
        this.selectedModels = new Set(this.availableModels);
      } else {
        this.selectedModels.clear();
      }
      this.updateCounters();
      this.renderModelsChecklist();
    };

    // 账号搜索
    const keySearch = this.querySelector("#key-search-input");
    keySearch.oninput = (e) => {
      this.keySearchQuery = e.target.value.trim().toLowerCase();
      this.renderKeysChecklist();
    };

    // 探测方式单选
    this.querySelectorAll("input[name='probe-type-radio']").forEach((radio) => {
      radio.onchange = (e) => {
        this.probeType = e.target.value;
      };
    });

    // 开始测活
    this.querySelector("#btn-start-verify").onclick = () => this.executeVerification();

    // 清空结果
    this.querySelector("#btn-clear-results").onclick = () => {
      this.lastRecords = [];
      this.renderResultsTable();
      this.querySelector("#btn-clear-results").disabled = true;
    };

    // 结果筛选 Pills
    const filterPills = this.querySelectorAll("#results-filter-row .detail-filter-pill");
    filterPills.forEach((pill) => {
      pill.onclick = () => {
        filterPills.forEach((p) => p.classList.remove("is-active"));
        pill.classList.add("is-active");
        this.resultFilter = pill.dataset.filter;
        this.renderResultsTable();
      };
    });
  }

  getFilteredKeys() {
    if (!this.keySearchQuery) return this.availableKeys;
    return this.availableKeys.filter((k) =>
      k.name.toLowerCase().includes(this.keySearchQuery) ||
      (k.key_prefix && k.key_prefix.toLowerCase().includes(this.keySearchQuery))
    );
  }

  updateCounters() {
    const kTag = this.querySelector("#keys-count-tag");
    const mTag = this.querySelector("#models-count-tag");
    const metaSummary = this.querySelector("#verify-meta-summary");

    if (kTag) kTag.textContent = `(已选 ${this.selectedKeys.size} / ${this.availableKeys.length})`;
    if (mTag) mTag.textContent = `(已选 ${this.selectedModels.size} / ${this.availableModels.length})`;

    const totalCombinations = this.selectedKeys.size * this.selectedModels.size;
    if (metaSummary) {
      metaSummary.textContent = `准备检测: ${this.selectedKeys.size} 个账号 × ${this.selectedModels.size} 款模型 = ${totalCombinations} 个组合`;
    }

    const checkAllKeys = this.querySelector("#check-all-keys");
    if (checkAllKeys) {
      checkAllKeys.checked = this.availableKeys.length > 0 && this.selectedKeys.size === this.availableKeys.length;
    }

    const checkAllModels = this.querySelector("#check-all-models");
    if (checkAllModels) {
      checkAllModels.checked = this.availableModels.length > 0 && this.selectedModels.size === this.availableModels.length;
    }
  }

  renderKeysChecklist() {
    const container = this.querySelector("#keys-checklist");
    if (!container) return;
    const filtered = this.getFilteredKeys();

    if (!filtered.length) {
      container.innerHTML = `<div class="keys-empty-hint">暂无匹配的账号</div>`;
      return;
    }

    container.innerHTML = filtered
      .map((k) => {
        const isChecked = this.selectedKeys.has(k.name);
        const statusClass = k.status === "Active" ? "tag-active" : k.status === "Cooling" ? "tag-cooling" : "tag-disabled";
        return `
          <div class="key-check-item ${isChecked ? "is-selected" : ""}" data-name="${k.name}">
            <input type="checkbox" class="key-checkbox-input" data-name="${k.name}" ${isChecked ? "checked" : ""} />
            <div class="key-check-info">
              <span class="key-check-name">${k.name}</span>
              <span class="key-check-prefix">${k.key_prefix || "AIzaSy..."}</span>
            </div>
            <span class="key-check-badge ${statusClass}">${k.status || "Active"}</span>
          </div>
        `;
      })
      .join("");

    container.querySelectorAll(".key-check-item").forEach((item) => {
      item.onclick = (e) => {
        if (e.target.tagName === "INPUT") return;
        const name = item.dataset.name;
        if (this.selectedKeys.has(name)) this.selectedKeys.delete(name);
        else this.selectedKeys.add(name);
        this.updateCounters();
        this.renderKeysChecklist();
      };
    });

    container.querySelectorAll(".key-checkbox-input").forEach((input) => {
      input.onchange = (e) => {
        const name = input.dataset.name;
        if (e.target.checked) this.selectedKeys.add(name);
        else this.selectedKeys.delete(name);
        this.updateCounters();
        this.renderKeysChecklist();
      };
    });
  }

  renderModelsChecklist() {
    const container = this.querySelector("#models-checklist");
    if (!container) return;

    container.innerHTML = this.availableModels
      .map((m) => {
        const isChecked = this.selectedModels.has(m);
        const isFlash = m.includes("flash");
        const isPro = m.includes("pro");
        return `
          <div class="model-check-item ${isChecked ? "is-selected" : ""}" data-model="${m}">
            <input type="checkbox" class="model-checkbox-input" data-model="${m}" ${isChecked ? "checked" : ""} />
            <span class="model-check-name" title="${m}">${m}</span>
            ${isPro ? '<span class="model-type-pill pro">PRO</span>' : isFlash ? '<span class="model-type-pill flash">FLASH</span>' : ""}
          </div>
        `;
      })
      .join("");

    container.querySelectorAll(".model-check-item").forEach((item) => {
      item.onclick = (e) => {
        if (e.target.tagName === "INPUT") return;
        const m = item.dataset.model;
        if (this.selectedModels.has(m)) this.selectedModels.delete(m);
        else this.selectedModels.add(m);
        this.updateCounters();
        this.renderModelsChecklist();
      };
    });

    container.querySelectorAll(".model-checkbox-input").forEach((input) => {
      input.onchange = (e) => {
        const m = input.dataset.model;
        if (e.target.checked) this.selectedModels.add(m);
        else this.selectedModels.delete(m);
        this.updateCounters();
        this.renderModelsChecklist();
      };
    });
  }

  async executeVerification() {
    if (this.running) return;
    if (!this.selectedKeys.size) {
      alert("请至少勾选一个参与测活的账号");
      return;
    }
    if (!this.selectedModels.size) {
      alert("请至少勾选一个目标测试模型");
      return;
    }

    const keys = Array.from(this.selectedKeys);
    const models = Array.from(this.selectedModels);
    const probeType = this.probeType;

    const startBtn = this.querySelector("#btn-start-verify");
    const startText = this.querySelector("#start-btn-text");
    const startIcon = this.querySelector("#start-btn-icon");

    this.running = true;
    startBtn.disabled = true;
    startIcon.textContent = "⏳";
    startText.textContent = `探测中 (${keys.length * models.length} 项)...`;

    const tbody = this.querySelector("#verify-results-tbody");
    if (tbody) {
      tbody.innerHTML = `
        <tr>
          <td colspan="8">
            <div class="verify-loading-box">
              <span class="verify-loading-spinner"></span>
              <span>正在探测 ${keys.length} 个账号与 ${models.length} 个模型 (共 ${keys.length * models.length} 项)...</span>
            </div>
          </td>
        </tr>
      `;
    }

    try {
      const res = await fetch("/v1/keys/verify-batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          keys,
          models,
          probe_type: probeType,
        }),
      });

      const data = await res.json();
      if (res.ok && data.records) {
        this.lastRecords = data.records;
        this.renderResultsTable();
        this.querySelector("#btn-clear-results").disabled = false;
      } else {
        alert(`测活失败: ${data.detail || "服务错误"}`);
      }
    } catch (err) {
      alert(`网络或服务连接异常: ${err.message}`);
    } finally {
      this.running = false;
      startBtn.disabled = false;
      startIcon.textContent = "⚡";
      startText.textContent = "开始测活";
    }
  }

  renderResultsTable() {
    const tbody = this.querySelector("#verify-results-tbody");
    if (!tbody) return;

    const records = this.lastRecords || [];
    const successCount = records.filter((r) => r.status === "Success").length;
    const coolingCount = records.filter((r) => r.status === "HighDemand" || r.status === "RateLimited").length;
    const failedCount = records.filter((r) => r.status === "Failed" || r.status === "AuthError").length;

    // 更新筛选按钮数值
    const pills = this.querySelectorAll("#results-filter-row .detail-filter-pill");
    pills.forEach((p) => {
      const f = p.dataset.filter;
      if (f === "all") p.textContent = `全部 (${records.length})`;
      else if (f === "success") p.textContent = `成功 (${successCount})`;
      else if (f === "cooling") p.textContent = `限流/高负载 (${coolingCount})`;
      else if (f === "failed") p.textContent = `失败 (${failedCount})`;
    });

    let displayRecords = records;
    if (this.resultFilter === "success") {
      displayRecords = records.filter((r) => r.status === "Success");
    } else if (this.resultFilter === "cooling") {
      displayRecords = records.filter((r) => r.status === "HighDemand" || r.status === "RateLimited");
    } else if (this.resultFilter === "failed") {
      displayRecords = records.filter((r) => r.status === "Failed" || r.status === "AuthError");
    }

    if (!displayRecords.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="verification-empty-cell">${records.length ? "当前筛选分类无记录" : "勾选上方账号与模型后，点击【开始测活】执行矩阵检测"}</td></tr>`;
      return;
    }

    tbody.innerHTML = displayRecords
      .map((r) => {
        const isSuccess = r.status === "Success";
        const isCooling = r.status === "HighDemand" || r.status === "RateLimited";
        const statusBadgeClass = isSuccess ? "vcount-badge is-success" : isCooling ? "vcount-badge is-warning" : "vcount-badge is-failed";
        const statusLabel = isSuccess ? "可用" : isCooling ? "限流/高负载" : "不可用";
        const probeStr = r.probe_type === "generate" ? "生成探针" : "Health 探针";

        return `
          <tr>
            <td><strong>${r.key_name}</strong></td>
            <td><code>${r.key_prefix}</code></td>
            <td><strong>${r.model || "-"}</strong></td>
            <td><span class="probe-type-tag">${probeStr}</span></td>
            <td><span class="${statusBadgeClass}">${statusLabel}</span></td>
            <td>${r.latency_ms ? `${r.latency_ms}ms` : "-"}</td>
            <td><span class="http-code-tag ${r.status_code === 200 ? "c200" : "cErr"}">${r.status_code || "-"}</span></td>
            <td class="cell-message" title="${r.message || ""}">${r.message || "-"}</td>
          </tr>
        `;
      })
      .join("");
  }
}
customElements.define("verification-modal", VerificationModal);
