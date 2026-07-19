class PoolMetric extends HTMLElement {
  static get observedAttributes() { return ["label", "value", "suffix"]; }
  connectedCallback() { this.render(); }
  attributeChangedCallback() { if (this.isConnected) this.render(); }
  render() {
    this.replaceChildren();
    const label = document.createElement("div");
    label.className = "metric-label";
    label.textContent = this.getAttribute("label") || "";
    const value = document.createElement("div");
    value.className = "metric-value";
    value.textContent = `${this.getAttribute("value") || "0"}${this.getAttribute("suffix") || ""}`;
    this.append(label, value);
  }
}

class StatusPill extends HTMLElement {
  static get observedAttributes() { return ["label"]; }
  connectedCallback() { this.render(); }
  attributeChangedCallback() { if (this.isConnected) this.render(); }
  render() { this.textContent = this.getAttribute("label") || ""; }
}

export function defineUiComponents() {
  if (!customElements.get("pool-metric")) customElements.define("pool-metric", PoolMetric);
  if (!customElements.get("status-pill")) customElements.define("status-pill", StatusPill);
}

export function createMetric({ label, value, suffix = "", tone = "default" }) {
  const metric = document.createElement("pool-metric");
  metric.setAttribute("label", label);
  metric.setAttribute("value", String(value ?? 0));
  metric.setAttribute("suffix", suffix);
  metric.setAttribute("tone", tone);
  return metric;
}

export function createStatusPill(status, label) {
  const pill = document.createElement("status-pill");
  pill.setAttribute("status", status);
  pill.setAttribute("label", label);
  return pill;
}

export function createCell(value, className = "") {
  const cell = document.createElement("td");
  cell.className = className;
  cell.textContent = String(value ?? "-");
  return cell;
}
