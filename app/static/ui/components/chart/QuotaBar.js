/**
 * QuotaBar - 多模型日配额与官网每日支持 Token 数横向柱状图组件
 * 对应 TransFlow 的指标可视化组件
 */
export class QuotaBar extends HTMLElement {
  static get observedAttributes() {
    return [
      "model",
      "used",
      "limit",
      "limit-tpd",
      "used-tokens",
      "status",
      "cooldown",
      "in-flight",
      "concurrency-limit",
      "is-deprioritized",
    ];
  }

  connectedCallback() {
    this.render();
  }

  attributeChangedCallback() {
    if (this.isConnected) this.render();
  }

  formatCompact(value) {
    const val = Number(value || 0);
    if (!val) return "0";
    if (val >= 1_000_000) return `${(val / 1_000_000).toFixed(val % 1_000_000 === 0 ? 0 : 1)}M`;
    if (val >= 1_000) return `${(val / 1_000).toFixed(val % 1_000 === 0 ? 0 : 1)}k`;
    return String(val);
  }

  render() {
    this.replaceChildren();
    const model = this.getAttribute("model") || "-";
    const usedRpd = Number(this.getAttribute("used") || 0);
    const limitRpd = Number(this.getAttribute("limit") || 0);
    const limitTpd = Number(this.getAttribute("limit-tpd") || 0);
    const usedTokens = Number(this.getAttribute("used-tokens") || 0);
    const status = (this.getAttribute("status") || "Active").toLowerCase();
    const cooldown = Number(this.getAttribute("cooldown") || 0);
    const inFlight = Number(this.getAttribute("in-flight") || 0);
    const concurrencyLimit = Number(this.getAttribute("concurrency-limit") || 2);
    const isDeprioritized = this.getAttribute("is-deprioritized") === "true";

    const rpdPct = limitRpd > 0 ? Math.min(100, Math.round((usedRpd / limitRpd) * 100)) : 0;
    const tpdPct = limitTpd > 0 ? Math.min(100, Math.round((usedTokens / limitTpd) * 100)) : 0;
    const maxPct = Math.max(rpdPct, tpdPct);

    const row = document.createElement("div");
    row.className = "quota-bar-header";

    const titleEl = document.createElement("div");
    titleEl.className = "quota-bar-title";
    const modelTag = document.createElement("strong");
    modelTag.textContent = model;
    titleEl.append(modelTag);

    if (inFlight > 0) {
      const flightTag = document.createElement("span");
      flightTag.className = "model-inflight-tag";
      flightTag.innerHTML = `● 在途 [${inFlight}/${concurrencyLimit}]`;
      flightTag.title = `当前模型正在并发: ${inFlight}，上限: ${concurrencyLimit}`;
      titleEl.append(flightTag);
    }

    if (isDeprioritized) {
      const depTag = document.createElement("span");
      depTag.className = "deprioritized-tag";
      depTag.textContent = "后置 >60%";
      depTag.title = "今日配额消耗已超 60%，在回复优先模式下排序后置";
      titleEl.append(depTag);
    }

    if (cooldown > 0) {
      const cdTag = document.createElement("span");
      cdTag.className = "cooldown-tag";
      cdTag.textContent = `冷却 ${cooldown}s`;
      titleEl.append(cdTag);
    }

    const statEl = document.createElement("div");
    statEl.className = "quota-bar-stat";
    
    // 同时呈现 RPD 与官网每日支持 Token 上限 (TPD)
    const tpdText = limitTpd > 0 ? ` · Token ${this.formatCompact(usedTokens)}/${this.formatCompact(limitTpd)}` : "";
    statEl.textContent = `${usedRpd}/${limitRpd} RPD${tpdText} (${maxPct}%)`;
    statEl.title = `请求进度: ${usedRpd} / ${limitRpd} RPD (${rpdPct}%)\n每日 Token 进度: ${usedTokens.toLocaleString()} / ${limitTpd.toLocaleString()} TPD (${tpdPct}%)`;

    row.append(titleEl, statEl);

    const track = document.createElement("div");
    track.className = "quota-bar-track";
    track.title = statEl.title;

    const fill = document.createElement("div");
    fill.className = `quota-bar-fill ${status}`;
    fill.style.width = `${maxPct}%`;
    track.append(fill);

    this.append(row, track);
  }
}

export function createQuotaBar({
  model,
  used,
  limit,
  limit_tpd,
  used_tokens,
  status,
  cooldown,
  in_flight,
  concurrency_limit,
  is_deprioritized,
}) {
  const bar = document.createElement("quota-bar");
  bar.setAttribute("model", model);
  bar.setAttribute("used", String(used ?? 0));
  bar.setAttribute("limit", String(limit ?? 0));
  if (limit_tpd) bar.setAttribute("limit-tpd", String(limit_tpd));
  if (used_tokens) bar.setAttribute("used-tokens", String(used_tokens));
  bar.setAttribute("status", status || "Active");
  bar.setAttribute("cooldown", String(cooldown ?? 0));
  if (in_flight) bar.setAttribute("in-flight", String(in_flight));
  if (concurrency_limit) bar.setAttribute("concurrency-limit", String(concurrency_limit));
  if (is_deprioritized) bar.setAttribute("is-deprioritized", "true");
  return bar;
}
