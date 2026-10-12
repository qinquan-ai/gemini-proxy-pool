/**
 * GlassButton - 毛玻璃交互按钮
 * 对应 TransFlow 的 components/button/GlassButton
 */
export class GlassButton extends HTMLElement {
  static get observedAttributes() {
    return ["variant", "size", "loading", "disabled"];
  }

  connectedCallback() {
    this.render();
  }

  attributeChangedCallback() {
    if (this.isConnected) this.render();
  }

  render() {
    const variant = this.getAttribute("variant") || "glass";
    const size = this.getAttribute("size") || "md";
    const loading = this.hasAttribute("loading");
    const disabled = this.hasAttribute("disabled") || loading;

    const btn = this.querySelector("button") || document.createElement("button");
    btn.className = `gbtn is-${size} is-${variant}`;
    btn.disabled = disabled;
    
    // 清空内部并重构结构
    const labelText = this.getAttribute("label") || btn.textContent || "";
    btn.replaceChildren();

    const sheen = document.createElement("span");
    sheen.className = "gbtn__sheen";
    sheen.setAttribute("aria-hidden", "true");
    btn.append(sheen);

    if (loading) {
      const spinner = document.createElement("span");
      spinner.className = "gbtn__spinner";
      spinner.setAttribute("aria-hidden", "true");
      btn.append(spinner);
    }

    const label = document.createElement("span");
    label.className = "gbtn__label";
    label.textContent = labelText;
    btn.append(label);

    if (!this.contains(btn)) {
      this.replaceChildren(btn);
    }
  }
}

export function createGlassButton({ label = "", variant = "glass", size = "md", icon = "", onClick = null }) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `gbtn is-${size} is-${variant}`;

  const sheen = document.createElement("span");
  sheen.className = "gbtn__sheen";
  sheen.setAttribute("aria-hidden", "true");
  btn.append(sheen);

  const labelEl = document.createElement("span");
  labelEl.className = "gbtn__label";
  if (icon) {
    const iconEl = document.createElement("span");
    iconEl.className = "gbtn__icon";
    iconEl.textContent = icon;
    labelEl.append(iconEl);
  }
  const textEl = document.createElement("span");
  textEl.textContent = label;
  labelEl.append(textEl);
  btn.append(labelEl);

  if (onClick) {
    btn.addEventListener("click", onClick);
  }
  return btn;
}
