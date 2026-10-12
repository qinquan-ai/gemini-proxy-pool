/**
 * GlassSpinner - 微光旋转指示组件
 * 对应 TransFlow 的 components/loading/GlassSpinner
 */
export class GlassSpinner extends HTMLElement {
  static get observedAttributes() {
    return ["size", "duration"];
  }

  connectedCallback() {
    this.render();
  }

  attributeChangedCallback() {
    if (this.isConnected) this.render();
  }

  render() {
    const size = Number(this.getAttribute("size") || 24);
    const duration = Number(this.getAttribute("duration") || 1.2);
    this.className = "glass-spinner-ring";
    this.style.width = `${size}px`;
    this.style.height = `${size}px`;
    this.style.animationDuration = `${duration}s`;
  }
}

export function createSpinner(size = 20) {
  const el = document.createElement("span");
  el.className = "glass-spinner-ring";
  el.style.width = `${size}px`;
  el.style.height = `${size}px`;
  return el;
}
