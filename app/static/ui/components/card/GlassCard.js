/**
 * GlassCard - 毛玻璃卡片基础组件
 * 对应 TransFlow 的 components/card/GlassCard
 */
export class GlassCard extends HTMLElement {
  connectedCallback() {
    this.classList.add("glass-card-container");
    if (!this.querySelector(".gcard")) {
      const isPad = this.getAttribute("padded") !== "false";
      const headerText = this.getAttribute("header");
      
      const wrapper = document.createElement("section");
      wrapper.className = `gcard ${isPad ? "is-pad" : ""}`;

      if (headerText) {
        const header = document.createElement("header");
        header.className = "gcard__header";
        header.textContent = headerText;
        wrapper.append(header);
      }

      const body = document.createElement("div");
      body.className = "gcard__body";
      while (this.firstChild) {
        body.append(this.firstChild);
      }
      wrapper.append(body);
      this.append(wrapper);
    }
  }
}

export function createGlassCard(options = {}) {
  const card = document.createElement("section");
  card.className = `gcard ${options.padded !== false ? "is-pad" : ""}`;
  if (options.header) {
    const h = document.createElement("header");
    h.className = "gcard__header";
    h.textContent = options.header;
    card.append(h);
  }
  return card;
}
