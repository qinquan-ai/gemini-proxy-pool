import { createQuotaBar } from "../chart/QuotaBar.js";
import { createStatusPill } from "../tag/GlassTag.js";

/**
 * KeyCard - Cockpit Tools 风格 Key 账号大卡片
 * 包含：
 * 1. key 名及脱敏前缀
 * 2. 各模型日配额横向柱状图
 * 3. 运行中高亮圈 / 微光旋转环 (in_flight)
 * 4. 冻结 / 冷却状态与快捷解冻
 * 5. 最后一次检查额度时间
 * 6. 距离下次额度刷新时间
 * 7. 请求次数
 * 8. 成功次数
 * 9. 失败 / 限流次数
 * 10. 正在并发数
 * 11. 最大并发数
 * 12. TPM 与累计 Token
 */
export class KeyCard extends HTMLElement {
  setKeyData(key, resetText = "", callbacks = {}, isExpanded = false) {
    this.keyData = key;
    this.resetText = resetText;
    this.callbacks = callbacks;
    this.isExpanded = Boolean(isExpanded);
    this.render();
  }

  connectedCallback() {
    if (this.keyData) this.render();
  }

  render() {
    this.replaceChildren();
    const key = this.keyData;
    if (!key) return;

    const card = document.createElement("article");
    const inFlightCount = Number(key.in_flight || 0);
    const isRunning = inFlightCount > 0;
    const isCooling = key.status === "Cooling";
    const isDisabled = Boolean(key.disabled);

    let cardClasses = ["gcard", "key-cockpit-card"];
    if (isRunning) cardClasses.push("is-running");
    if (isCooling) cardClasses.push("is-cooling");
    if (isDisabled) cardClasses.push("is-disabled");
    card.className = cardClasses.join(" ");

    // 1. 卡片顶栏：Key 标识、状态指示与微光旋转圈
    const header = document.createElement("div");
    header.className = "card-header";

    const titleGroup = document.createElement("div");
    titleGroup.className = "card-title-group";

    const statusRing = document.createElement("div");
    statusRing.className = `status-indicator ${isRunning ? "running" : isCooling ? "cooling" : isDisabled ? "disabled" : "active"}`;
    if (isRunning) {
      statusRing.innerHTML = `<span class="spin-ring"></span><span class="active-dot"></span>`;
    } else {
      statusRing.innerHTML = `<span class="active-dot"></span>`;
    }

    const titleText = document.createElement("strong");
    titleText.className = "card-name";
    titleText.textContent = key.name || "未命名 Key";

    const prefixBadge = document.createElement("code");
    prefixBadge.className = "key-prefix-badge";
    prefixBadge.textContent = key.key_prefix || "-";

    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "icon-copy-btn";
    copyBtn.title = "复制脱敏 Key 前缀";
    copyBtn.innerHTML = "&#10697;";
    copyBtn.onclick = async (e) => {
      e.stopPropagation();
      await navigator.clipboard.writeText(key.key_prefix || "");
      this.showFeedback("已复制脱敏 Key", "info");
    };

    titleGroup.append(statusRing, titleText, prefixBadge, copyBtn);

    const badgesGroup = document.createElement("div");
    badgesGroup.className = "card-badges-group";

    if (isRunning) {
      const flightBadge = document.createElement("span");
      flightBadge.className = "in-flight-tag";
      flightBadge.innerHTML = `<span class="pulse-ring"></span>在途并发 ${inFlightCount}`;
      badgesGroup.append(flightBadge);
    }

    if (key.active_models && key.active_models.length) {
      for (const act of key.active_models) {
        const actTag = document.createElement("span");
        actTag.className = "active-model-tag";
        actTag.innerHTML = `● ${act.model} [${act.in_flight}/${act.limit}]`;
        actTag.title = `运行中模型: ${act.model}，当前在途: ${act.in_flight}，并发限制: ${act.limit}`;
        badgesGroup.append(actTag);
      }
    }

    const statusPill = createStatusPill(
      String(key.status || "").toLowerCase(),
      key.status === "Active" ? "正常" : key.status === "Cooling" ? "冷却" : "禁用"
    );
    badgesGroup.append(statusPill);

    header.append(titleGroup, badgesGroup);
    card.append(header);

    // 2. 额度刷新倒计时横幅（下次刷新时间）
    const banner = document.createElement("div");
    banner.className = "card-banner";
    const resetDisplay = this.resetText || "每日 16:00 自动刷新 (PT 午夜)";
    banner.innerHTML = `<span class="clock-icon">🕒</span><span>配额重置: ${resetDisplay}</span>`;
    card.append(banner);

    // 3. 模型日配额横向柱状图 (分组折叠设计：核心模型外显，扩展模型折叠)
    const quotasSection = document.createElement("div");
    quotasSection.className = "card-quotas-section";

    const quotasHead = document.createElement("div");
    quotasHead.className = "section-subtitle";
    quotasHead.innerHTML = `<span>模型配额 (RPD · TPD)</span><small>${key.used_today_total || 0} / ${key.limit_rpd_total || 0} RPD</small>`;
    quotasSection.append(quotasHead);

    const models = key.models || {};
    const modelKeys = Object.keys(models);
    if (!modelKeys.length) {
      const empty = document.createElement("div");
      empty.className = "empty-hint mini";
      empty.textContent = "暂无模型配额分配";
      quotasSection.append(empty);
    } else {
      const coreKeys = [];
      const extKeys = [];
      for (const m of modelKeys) {
        if (models[m].is_core || (models[m].last_used_at && models[m].last_used_at > 0)) {
          coreKeys.push(m);
        } else {
          extKeys.push(m);
        }
      }

      // 3.1 核心推荐模型 (所有 Key 统一等高外显)
      const coreList = document.createElement("div");
      coreList.className = "core-models-list";
      for (const m of coreKeys) {
        const mData = models[m];
        coreList.append(
          createQuotaBar({
            model: m,
            used: mData.used_today,
            limit: mData.limit_rpd,
            limit_tpd: mData.limit_tpd,
            used_tokens: mData.used_tokens_today,
            status: mData.status,
            cooldown: mData.cooldown_remaining_sec,
            in_flight: mData.in_flight,
            tokens: mData.tokens,
            burst: mData.burst,
            last_cost_ms: mData.last_cost_ms,
            rate_limited: mData.rate_limited_today,
            timeout: mData.timeout_today,
          })
        );
      }
      quotasSection.append(coreList);

      // 3.2 扩展模型 (支持折叠与展开)
      if (extKeys.length) {
        const collapseDetails = document.createElement("details");
        collapseDetails.className = "model-collapse-box";
        if (this.isExpanded) {
          collapseDetails.open = true;
        }
        collapseDetails.addEventListener("toggle", () => {
          this.isExpanded = collapseDetails.open;
          if (this.callbacks.onToggleExpand) {
            this.callbacks.onToggleExpand(key.name, collapseDetails.open);
          }
        });
        collapseDetails.onclick = (e) => e.stopPropagation();

        const summary = document.createElement("summary");
        summary.className = "model-collapse-trigger";
        summary.innerHTML = `<span>更多模型配额 (${extKeys.length} 个)</span><span class="chevron">▼</span>`;
        collapseDetails.append(summary);

        const extList = document.createElement("div");
        extList.className = "ext-models-list";
        for (const m of extKeys) {
          const mData = models[m];
          extList.append(
            createQuotaBar({
              model: m,
              used: mData.used_today,
              limit: mData.limit_rpd,
              limit_tpd: mData.limit_tpd,
              used_tokens: mData.used_tokens_today,
              status: mData.status,
              cooldown: mData.cooldown_remaining_sec,
              in_flight: mData.in_flight,
              tokens: mData.tokens,
              burst: mData.burst,
              last_cost_ms: mData.last_cost_ms,
              rate_limited: mData.rate_limited_today,
              timeout: mData.timeout_today,
            })
          );
        }
        collapseDetails.append(extList);
        quotasSection.append(collapseDetails);
      }
    }
    card.append(quotasSection);

    // 4. 运行指标微网格 (展示今日与累计指标)
    const statsGrid = document.createElement("div");
    statsGrid.className = "card-stats-grid";

    const maxConcurrency = key.max_concurrency || 5;
    const lastCheckTime = key.last_check_time ? this.formatTime(key.last_check_time) : this.formatTime(Date.now() / 1000);
    const todayTokensCompact = this.formatCompact(key.today_total_tokens || 0);
    const totalTokensCompact = this.formatCompact(key.total_tokens || 0);

    const statItems = [
      {
        label: "今日请求",
        value: key.today_req_count || 0,
        title: `今日请求: ${key.today_req_count || 0} 次\n累计历史: ${key.req_count || 0} 次`,
      },
      {
        label: "今日成功",
        value: key.today_success_count || 0,
        tone: "success",
        title: `今日成功: ${key.today_success_count || 0} 次\n累计历史: ${key.success_count || 0} 次`,
      },
      {
        label: "今日失败/限流",
        value: `${key.today_failure_count || 0} (${key.today_rate_limit_count || 0})`,
        tone: (key.today_failure_count || 0) > 0 ? "danger" : "default",
        title: `今日失败: ${key.today_failure_count || 0} 次 (限流 ${key.today_rate_limit_count || 0} 次)\n累计失败: ${key.failure_count || 0} 次 (限流 ${key.rate_limit_count || 0} 次)`,
      },
      {
        label: "正在并发",
        value: `${inFlightCount} / ${maxConcurrency}`,
        tone: isRunning ? "accent" : "default",
        title: `当前在途并发: ${inFlightCount}\n最大允许并发: ${maxConcurrency}`,
      },
      {
        label: "并发上限",
        value: `${maxConcurrency} 线程`,
        tone: "default",
      },
      {
        label: "今日峰值 TPM",
        value: key.today_peak_tpm ? `${this.formatCompact(key.today_peak_tpm)} TPM` : (key.current_tpm ? `${this.formatCompact(key.current_tpm)} TPM` : "0 TPM"),
        tone: key.today_peak_tpm ? "accent" : "info",
        title: `今日峰值: ${(key.today_peak_tpm || 0).toLocaleString()} TPM\n当前瞬时: ${(key.current_tpm || 0).toLocaleString()} TPM\n官方参考上限: 1,000,000 TPM`,
      },
      {
        label: "今日 Token",
        value: todayTokensCompact,
        tone: key.today_total_tokens ? "accent" : "default",
        title: `今日已用: ${(key.today_total_tokens || 0).toLocaleString()} Token\n累计历史: ${(key.total_tokens || 0).toLocaleString()} Token`,
      },
      {
        label: "最近调用",
        value: this.formatTime(key.last_used_at),
        title: `最近调用: ${this.formatTime(key.last_used_at)}\n累计历史: ${totalTokensCompact} Token (详情中可查看明细)`,
      },
      {
        label: "核对时间",
        value: lastCheckTime,
        title: `最后状态检查时间: ${lastCheckTime}`,
      },
    ];

    for (const item of statItems) {
      const tile = document.createElement("div");
      tile.className = `stat-tile ${item.tone || ""}`;
      if (item.title) tile.title = item.title;
      const l = document.createElement("span");
      l.className = "stat-tile-label";
      l.textContent = item.label;
      const v = document.createElement("strong");
      v.className = "stat-tile-val";
      v.textContent = String(item.value);
      tile.append(l, v);
      statsGrid.append(tile);
    }
    card.append(statsGrid);

    // 5. 提示反馈区
    const feedbackBox = document.createElement("div");
    feedbackBox.className = "card-action-feedback";
    card.append(feedbackBox);

    // 6. 卡片底部运维操作栏
    const footer = document.createElement("div");
    footer.className = "card-footer";

    const statusTip = document.createElement("div");
    statusTip.className = "card-footer-tip";
    if (key.last_error && (isDisabled || isCooling)) {
      statusTip.className = "card-footer-tip error";
      statusTip.textContent = `异常: ${key.last_error.slice(0, 26)}...`;
      statusTip.title = key.last_error;
    } else {
      statusTip.textContent = isDisabled ? "已停用" : isCooling ? `冷却退避 (${key.cooldown_remaining_sec}s)` : "可用";
      if (key.last_error && !isDisabled && !isCooling) {
        statusTip.title = `最近历史提示: ${key.last_error}`;
      }
    }
    footer.append(statusTip);

    const actionsBar = document.createElement("div");
    actionsBar.className = "card-actions-bar";

    // [⚡ 测活] - 点击唤起测活弹窗 (直达该 Key)
    const pingBtn = document.createElement("button");
    pingBtn.type = "button";
    pingBtn.className = "card-btn-action";
    pingBtn.title = `针对 ${key.name} 发起模型测活`;
    pingBtn.innerHTML = "<span>⚡</span><small>测活</small>";
    pingBtn.onclick = (e) => {
      e.stopPropagation();
      const modal = document.getElementById("verification-modal");
      if (modal && modal.open) {
        modal.open(key.name);
      }
    };

    // [❄️ 解冻]
    const unfreezeBtn = document.createElement("button");
    unfreezeBtn.type = "button";
    unfreezeBtn.className = "card-btn-action";
    unfreezeBtn.title = "手动清除冷却状态";
    unfreezeBtn.innerHTML = "<span>❄️</span><small>解冻</small>";
    unfreezeBtn.disabled = key.status !== "Cooling";
    unfreezeBtn.onclick = async (e) => {
      e.stopPropagation();
      unfreezeBtn.disabled = true;
      try {
        const res = await fetch(`/v1/keys/${encodeURIComponent(key.name)}/unfreeze`, { method: "POST" });
        const json = await res.json();
        if (res.ok) {
          this.showFeedback("已解冻恢复", "success");
          if (this.callbacks.onRefresh) this.callbacks.onRefresh();
        } else {
          this.showFeedback(`解冻失败: ${json.detail}`, "danger");
        }
      } catch (err) {
        this.showFeedback(`请求失败: ${err.message}`, "danger");
      } finally {
        unfreezeBtn.disabled = false;
      }
    };

    // [🚫 启停]
    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "card-btn-action";
    toggleBtn.title = isDisabled ? "恢复启用" : "临时停用";
    toggleBtn.innerHTML = isDisabled ? "<span>✅</span><small>启用</small>" : "<span>🚫</span><small>停用</small>";
    toggleBtn.onclick = async (e) => {
      e.stopPropagation();
      toggleBtn.disabled = true;
      try {
        const res = await fetch(`/v1/keys/${encodeURIComponent(key.name)}/toggle`, { method: "POST" });
        const json = await res.json();
        if (res.ok) {
          this.showFeedback(json.message, "success");
          if (this.callbacks.onRefresh) this.callbacks.onRefresh();
        } else {
          this.showFeedback(`操作失败: ${json.detail}`, "danger");
        }
      } catch (err) {
        this.showFeedback(`请求失败: ${err.message}`, "danger");
      } finally {
        toggleBtn.disabled = false;
      }
    };

    // [🔍 详情]
    const detailBtn = document.createElement("button");
    detailBtn.type = "button";
    detailBtn.className = "card-btn-action primary";
    detailBtn.title = "查看专属审计流水与配额明细";
    detailBtn.innerHTML = "<span>🔍</span><small>详情</small>";
    detailBtn.onclick = (e) => {
      e.stopPropagation();
      if (this.callbacks.onOpenDetail) this.callbacks.onOpenDetail(key);
    };

    actionsBar.append(pingBtn, unfreezeBtn, toggleBtn, detailBtn);
    footer.append(actionsBar);
    card.append(footer);

    // 卡片整体点击查看详情
    card.onclick = () => {
      if (this.callbacks.onOpenDetail) this.callbacks.onOpenDetail(key);
    };

    this.append(card);
  }

  showFeedback(msg, type = "info") {
    const box = this.querySelector(".card-action-feedback");
    if (!box) return;
    box.textContent = msg;
    box.className = `card-action-feedback ${type} visible`;
    setTimeout(() => {
      box.classList.remove("visible");
    }, 3500);
  }

  formatCompact(val) {
    return new Intl.NumberFormat("zh-CN", {
      notation: "compact",
      maximumFractionDigits: 1,
    }).format(Number(val || 0));
  }

  formatTime(ts) {
    if (!ts) return "-";
    const d = new Date(Number(ts) * 1000);
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
  }
}

export function createKeyCard(key, resetText = "", callbacks = {}, isExpanded = false) {
  const card = document.createElement("key-card");
  card.setKeyData(key, resetText, callbacks, isExpanded);
  return card;
}
