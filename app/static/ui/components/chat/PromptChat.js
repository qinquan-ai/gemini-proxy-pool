/**
 * PromptChat - 针对 Key 选定与 Prompt 聊天调试的毛玻璃组件
 * 对应 TransFlow 的 components/chat
 */
export class PromptChat extends HTMLElement {
  connectedCallback() {
    if (!this.querySelector(".chat-panel")) {
      this.render();
    }
  }

  setPoolKeys(keys = [], models = []) {
    this.poolKeys = keys;
    this.models = models;
    this.updateSelects();
  }

  render() {
    this.innerHTML = `
      <section class="gcard is-pad chat-panel">
        <header class="chat-header">
          <div class="chat-title-group">
            <h2 class="chat-title">Prompt 调试</h2>
          </div>
          <div class="chat-controls">
            <div class="control-item">
              <label for="chat-key-select">目标 Key</label>
              <select id="chat-key-select" class="glass-select">
                <option value="">自动号池均衡 (默认)</option>
              </select>
            </div>
            <div class="control-item">
              <label for="chat-model-select">执行模型</label>
              <select id="chat-model-select" class="glass-select">
                <option value="gemini-3.5-flash-lite">gemini-3.5-flash-lite</option>
                <option value="gemini-3.8-flash">gemini-3.8-flash</option>
                <option value="gemini-3.1-flash-lite">gemini-3.1-flash-lite</option>
                <option value="gemini-2.5-flash">gemini-2.5-flash</option>
              </select>
            </div>
            <div class="control-item">
              <label for="chat-protocol-select">调用协议</label>
              <select id="chat-protocol-select" class="glass-select">
                <option value="openai">OpenAI Compat (/v1)</option>
                <option value="native">Google Native (/v1beta)</option>
              </select>
            </div>
            <div class="control-item">
              <label for="chat-mode-select">调度策略</label>
              <select id="chat-mode-select" class="glass-select">
                <option value="01">01: 定模多Key并发 (默认)</option>
                <option value="11">11: 定Key定模重试</option>
                <option value="10">10: 定Key模型降级</option>
                <option value="00">00: 动态并发</option>
              </select>
            </div>
          </div>
        </header>

        <div class="chat-body">
          <div id="chat-output" class="chat-output">
            <div class="chat-empty-hint">在下方输入 Prompt 并发送，验证调度策略与模型推理连通性</div>
          </div>
        </div>

        <footer class="chat-footer">
          <div class="chat-input-wrapper">
            <textarea id="chat-prompt-input" class="glass-input" rows="2" placeholder="输入测试 Prompt (Enter 发送，Shift+Enter 换行)..."></textarea>
            <div class="chat-actions">
              <span id="chat-status-indicator" class="chat-status">空闲</span>
              <button id="chat-abort-btn" type="button" class="gbtn is-md is-danger" style="display: none; margin-right: 0.5rem;">
                <span class="gbtn__label">中止请求</span>
              </button>
              <button id="chat-send-btn" type="button" class="gbtn is-md is-solid">
                <span class="gbtn__sheen" aria-hidden="true"></span>
                <span class="gbtn__label">发送请求</span>
              </button>
            </div>
          </div>
        </footer>
      </section>
    `;

    this.setupEvents();
    this.updateSelects();
  }

  updateSelects() {
    const keySelect = this.querySelector("#chat-key-select");
    const modelSelect = this.querySelector("#chat-model-select");
    if (!keySelect) return;

    const currentKeyVal = keySelect.value;
    keySelect.innerHTML = `<option value="">自动号池均衡 (默认)</option>`;
    if (this.poolKeys && this.poolKeys.length) {
      for (const k of this.poolKeys) {
        const opt = document.createElement("option");
        opt.value = k.name;
        opt.textContent = `${k.name} (${k.status === "Active" ? "可用" : "冷却/禁用"})`;
        keySelect.append(opt);
      }
    }
    if (currentKeyVal) keySelect.value = currentKeyVal;

    if (modelSelect && this.models && this.models.length) {
      const currentModelVal = modelSelect.value;
      modelSelect.replaceChildren();
      for (const m of this.models) {
        const opt = document.createElement("option");
        opt.value = m;
        opt.textContent = m;
        modelSelect.append(opt);
      }
      if (currentModelVal && this.models.includes(currentModelVal)) {
        modelSelect.value = currentModelVal;
      }
    }
  }

  setupEvents() {
    const input = this.querySelector("#chat-prompt-input");
    const sendBtn = this.querySelector("#chat-send-btn");
    const abortBtn = this.querySelector("#chat-abort-btn");
    const keySelect = this.querySelector("#chat-key-select");
    const modeSelect = this.querySelector("#chat-mode-select");

    if (keySelect && modeSelect) {
      keySelect.onchange = () => {
        if (keySelect.value && modeSelect.value === "01") {
          modeSelect.value = "11";
        } else if (!keySelect.value && (modeSelect.value === "11" || modeSelect.value === "10")) {
          modeSelect.value = "01";
        }
      };
    }

    if (abortBtn) {
      abortBtn.onclick = () => this.abortPrompt();
    }

    if (sendBtn && input) {
      sendBtn.onclick = () => this.sendPrompt();
      input.onkeydown = (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          this.sendPrompt();
        }
      };
    }
  }

  abortPrompt() {
    if (this.abortController) {
      this.abortController.abort();
      this.abortController = null;
      const statusEl = this.querySelector("#chat-status-indicator");
      if (statusEl) statusEl.textContent = "已主动中止";
    }
  }

  async sendPrompt() {
    const input = this.querySelector("#chat-prompt-input");
    const sendBtn = this.querySelector("#chat-send-btn");
    const abortBtn = this.querySelector("#chat-abort-btn");
    const statusEl = this.querySelector("#chat-status-indicator");
    const outputEl = this.querySelector("#chat-output");
    const keySelect = this.querySelector("#chat-key-select");
    const modelSelect = this.querySelector("#chat-model-select");
    const modeSelect = this.querySelector("#chat-mode-select");

    const promptText = input.value.trim();
    if (!promptText) return;

    const selectedKey = keySelect.value;
    const selectedModel = modelSelect.value;
    const selectedMode = modeSelect ? modeSelect.value : "01";

    const protocolSelect = this.querySelector("#chat-protocol-select");
    const selectedProtocol = protocolSelect ? protocolSelect.value : "openai";

    this.abortController = new AbortController();

    sendBtn.style.display = "none";
    if (abortBtn) abortBtn.style.display = "inline-flex";
    input.disabled = true;
    statusEl.textContent = `[${selectedProtocol.toUpperCase()}] 模式 [${selectedMode}] 调度中...`;

    // 创建用户输入展示气泡
    const userBubble = document.createElement("div");
    userBubble.className = "chat-bubble user";
    userBubble.innerHTML = `<div class="bubble-header"><span>用户 Prompt (${selectedProtocol === "native" ? "Google Native" : "OpenAI Compat"})</span><small>${new Date().toLocaleTimeString()}</small></div><div class="bubble-content">${this.escapeHtml(promptText)}</div>`;
    
    // 如果是第一次发送，清空占位提示
    if (outputEl.querySelector(".chat-empty-hint")) {
      outputEl.replaceChildren();
    }
    outputEl.append(userBubble);

    // 创建模型回复气泡
    const botBubble = document.createElement("div");
    botBubble.className = "chat-bubble bot loading";
    botBubble.innerHTML = `<div class="bubble-header"><span>${selectedModel}</span><div class="bubble-meta"><span class="route-tag">模式 ${selectedMode}</span><span class="meta-tag key">${selectedKey ? "指定: " + selectedKey : "自动号池"}</span><span class="meta-tag">${selectedProtocol === "native" ? "Native" : "OpenAI"}</span></div></div><div class="bubble-content">正在等待响应...</div>`;
    outputEl.append(botBubble);
    outputEl.scrollTop = outputEl.scrollHeight;

    const startTime = performance.now();

    try {
      const headers = {
        "Content-Type": "application/json",
        "X-Route-Mode": selectedMode,
      };
      if (selectedKey) {
        headers["X-Target-Key"] = encodeURIComponent(selectedKey);
      }

      let res;
      if (selectedProtocol === "native") {
        res = await fetch(`/v1beta/models/${encodeURIComponent(selectedModel)}:generateContent`, {
          method: "POST",
          headers,
          signal: this.abortController.signal,
          body: JSON.stringify({
            contents: [{ role: "user", parts: [{ text: promptText }] }],
          }),
        });
      } else {
        res = await fetch("/v1/chat/completions", {
          method: "POST",
          headers,
          signal: this.abortController.signal,
          body: JSON.stringify({
            model: selectedModel,
            messages: [{ role: "user", content: promptText }],
          }),
        });
      }

      const elapsed = Math.round(performance.now() - startTime);
      const json = await res.json();

      botBubble.classList.remove("loading");
      if (res.ok) {
        let replyText = "(无返回内容)";
        let totalTokens = 0;
        let hitModel = selectedModel;

        if (selectedProtocol === "native") {
          const parts = json.candidates?.[0]?.content?.parts || [];
          replyText = parts.map((p) => p.text || "").join("") || "(无返回内容)";
          totalTokens = json.usageMetadata?.totalTokenCount || 0;
          hitModel = `${selectedModel} (Native)`;
        } else if (json.choices && json.choices.length) {
          replyText = json.choices[0].message?.content || "(无返回内容)";
          totalTokens = json.usage?.total_tokens || 0;
          hitModel = json.model || selectedModel;
        }

        const hitKey = json.system_fingerprint || selectedKey || "Gateway Pool";
        botBubble.innerHTML = `
          <div class="bubble-header">
            <span>${hitModel}</span>
            <div class="bubble-meta">
              <span class="route-tag">模式 ${selectedMode}</span>
              <span class="meta-tag key">${hitKey}</span>
              <span class="meta-tag">${elapsed}ms</span>
              <span class="meta-tag">${totalTokens} tokens</span>
            </div>
          </div>
          <div class="bubble-content">${this.escapeHtml(replyText)}</div>
        `;
        statusEl.textContent = `完成 (${elapsed}ms)`;
      } else {
        const errMsg = json.error?.message || json.detail || `HTTP ${res.status}`;
        botBubble.classList.add("error");
        botBubble.innerHTML = `
          <div class="bubble-header">
            <span>调用失败</span>
            <span class="meta-tag danger">HTTP ${res.status}</span>
          </div>
          <div class="bubble-content error-text">${this.escapeHtml(errMsg)}</div>
        `;
        statusEl.textContent = `请求失败: HTTP ${res.status}`;
      }
    } catch (e) {
      botBubble.classList.remove("loading");
      botBubble.classList.add("error");
      const isAbort = e.name === "AbortError";
      const errText = isAbort ? "请求已由用户主动中止 (客户端连接已掐断)" : e.message;
      botBubble.innerHTML = `
        <div class="bubble-header"><span>${isAbort ? "已中止" : "连接异常"}</span></div>
        <div class="bubble-content error-text">${this.escapeHtml(errText)}</div>
      `;
      statusEl.textContent = isAbort ? "已主动中止" : `异常: ${e.message}`;
    } finally {
      this.abortController = null;
      if (abortBtn) abortBtn.style.display = "none";
      sendBtn.style.display = "inline-flex";
      sendBtn.disabled = false;
      input.disabled = false;
      input.value = "";
      input.focus();
      outputEl.scrollTop = outputEl.scrollHeight;
      if (this.onCompleted) this.onCompleted();
    }
  }

  escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }
}

export function createPromptChat() {
  return document.createElement("prompt-chat");
}
