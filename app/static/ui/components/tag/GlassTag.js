/**
 * GlassTag - 毛玻璃标签与状态胶囊组件
 * 对应 TransFlow 的 components/tag/GlassTag
 */
export class GlassTag extends HTMLElement {
  static get observedAttributes() {
    return ["variant", "label"];
  }

  connectedCallback() {
    this.render();
  }

  attributeChangedCallback() {
    if (this.isConnected) this.render();
  }

  render() {
    const variant = this.getAttribute("variant") || "default";
    const label = this.getAttribute("label") || this.textContent || "";
    this.className = `gtag is-${variant}`;
    this.textContent = label;
  }
}

export function createGlassTag(label, variant = "default") {
  const tag = document.createElement("span");
  tag.className = `gtag is-${variant}`;
  tag.textContent = label;
  return tag;
}

export function createStatusPill(status, label) {
  const pill = document.createElement("span");
  const s = String(status || "").toLowerCase();
  pill.className = `status-pill ${s}`;
  pill.textContent = label || status;
  return pill;
}
