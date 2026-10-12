/**
 * UI 设计系统根导出 (Glass Kit)
 * 严格对齐 Tiktok_TransFlow 的 webui/src/ui 架构
 */

import { GlassButton } from "./components/button/GlassButton.js";
import { GlassCard } from "./components/card/GlassCard.js";
import { KeyCard } from "./components/card/KeyCard.js";
import { GlassTag } from "./components/tag/GlassTag.js";
import { GlassSpinner } from "./components/loading/GlassSpinner.js";
import { QuotaBar } from "./components/chart/QuotaBar.js";
import { KeyDrawer } from "./components/dialog/KeyDrawer.js";
import { VerificationModal } from "./components/dialog/VerificationModal.js";
import { PromptChat } from "./components/chat/PromptChat.js";

export * from "./components/button/index.js";
export * from "./components/card/index.js";
export * from "./components/tag/index.js";
export * from "./components/loading/index.js";
export * from "./components/chart/index.js";
export * from "./components/dialog/index.js";
export * from "./components/chat/index.js";

/**
 * 注册所有自定义元素
 */
export function defineUiComponents() {
  if (!customElements.get("glass-card")) customElements.define("glass-card", GlassCard);
  if (!customElements.get("key-card")) customElements.define("key-card", KeyCard);
  if (!customElements.get("glass-button")) customElements.define("glass-button", GlassButton);
  if (!customElements.get("glass-tag")) customElements.define("glass-tag", GlassTag);
  if (!customElements.get("glass-spinner")) customElements.define("glass-spinner", GlassSpinner);
  if (!customElements.get("quota-bar")) customElements.define("quota-bar", QuotaBar);
  if (!customElements.get("key-drawer")) customElements.define("key-drawer", KeyDrawer);
  if (!customElements.get("verification-modal")) customElements.define("verification-modal", VerificationModal);
  if (!customElements.get("prompt-chat")) customElements.define("prompt-chat", PromptChat);
}

export function createCell(value, className = "") {
  const cell = document.createElement("td");
  cell.className = className;
  cell.textContent = String(value ?? "-");
  return cell;
}
