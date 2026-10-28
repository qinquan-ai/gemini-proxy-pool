/**
 * QuotaBar - 单 Key 单模型的 RPD 进度。P1 起补充令牌桶状态与最近耗时。
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
      "tokens",
      "burst",
      "last-cost",
      "rate-limited",
      "timeout",
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
    const tokens = Number(this.getAttribute("tokens") || 0);
    const burst = Number(this.getAttribute("burst") || 0);
    const lastCost = Number(this.getAttribute("last-cost") || 0);
    const rateLimited = Number(this.getAttribute("rate-limited") || 0);
    const timeout = Number(this.getAttribute("timeout") || 0);

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
      flightTag.textContent = `在途 ${inFlight}`;
      flightTag.title = `当前在飞请求数：${inFlight}。并发不是闸门，只作观测。`;
      titleEl.append(flightTag);
    }

    if (burst > 0) {
      const tokenTag = document.createElement("span");
      tokenTag.className = "model-inflight-tag";
      tokenTag.textContent = `令牌 ${tokens.toFixed(1)}/${burst}`;
      tokenTag.title = "令牌桶余量。低于 1 时下一发需要等待补充。";
      titleEl.append(tokenTag);
    }

    if (cooldown > 0) {
      const cdTag = document.createElement("span");
      cdTag.className = "cooldown-tag";
      cdTag.textContent = `冷却 ${cooldown}s`;
      titleEl.append(cdTag);
    }

    if (lastCost > 0) {
      const costTag = document.createElement("span");
      costTag.className = "model-inflight-tag";
      costTag.textContent = `上次 ${Math.round(lastCost)}ms`;
      titleEl.append(costTag);
    }

    const statEl = document.createElement("div");
    statEl.className = "quota-bar-stat";

    const tpdText = limitTpd > 0 ? ` · Token ${this.formatCompact(usedTokens)}/${this.formatCompact(limitTpd)}` : "";
    statEl.textContent = `${usedRpd}/${limitRpd} RPD${tpdText} (${maxPct}%)`;
    statEl.title =
      `请求进度: ${usedRpd} / ${limitRpd} RPD (${rpdPct}%)\n` +
      `每日 Token: ${usedTokens.toLocaleString()} / ${limitTpd.toLocaleString()} (${tpdPct}%)` +
      (rateLimited ? `\n今日被上游 429：${rateLimited} 次` : "") +
      (timeout ? `\n今日超时：${timeout} 次` : "");

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
  tokens,
  burst,
  last_cost_ms,
  rate_limited,
  timeout,
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
  if (tokens !== undefined) bar.setAttribute("tokens", String(tokens));
  if (burst) bar.setAttribute("burst", String(burst));
  if (last_cost_ms) bar.setAttribute("last-cost", String(last_cost_ms));
  if (rate_limited) bar.setAttribute("rate-limited", String(rate_limited));
  if (timeout) bar.setAttribute("timeout", String(timeout));
  return bar;
}
