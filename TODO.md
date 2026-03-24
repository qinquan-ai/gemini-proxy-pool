# AI Proxy Gateway Development Roadmap

## Phase 1: Standard Gemini API Pool (Current Priority)
**Goal:** Build a robust, load-balancing gateway for multiple standard Gemini API keys to bypass standard rate limits.
- [ ] **Infrastructure Setup:** Ensure FastAPI server is running securely and accessible by OpenClaw.
- [ ] **OpenAI Compatibility:** Perfect the translation layer between OpenAI request formats and Gemini API formats (handling streaming, tool calls, and content variations).
- [ ] **Multi-Key Rotation:** 
  - Read multiple API keys from `.env` (e.g., `GEMINI_API_KEY_1`, `GEMINI_API_KEY_2`).
  - Implement a Round-Robin or Random selection algorithm.
- [ ] **Error Handling & Fallback:** Catch `429 Too Many Requests` or quota exceeded errors and automatically retry with the next healthy key in the pool.
- [ ] **Testing:** Verify integration with OpenClaw using the `Custom Provider` setting.

## Phase 2: Antigravity OAuth Integration (Advanced)
**Goal:** Reverse-engineer the Google Cloud Code Assist OAuth flow to tap into the high-tier, unlimited Antigravity agentic infrastructure using 3 Pro accounts.
- [ ] **OAuth Authentication Script:** Write a standalone Python script to perform the initial PKCE OAuth flow and extract the `refresh_token` for each of the 3 accounts.
- [ ] **Token Management System:** 
  - Securely store the 3 `refresh_token`s in `.env`.
  - Implement a background worker in `main.py` that automatically acquires a fresh `access_token` every ~50 minutes for each account.
- [ ] **Protocol Translation (The Hard Part):** 
  - Analyze the `v1internal:loadCodeAssist` and internal streaming endpoints.
  - Write a new provider module in Python that translates standard OpenAI chat completion requests into Google's proprietary internal nested JSON/gRPC format.
- [ ] **Hybrid Routing (The Ultimate Gateway):** 
  - Route standard, lightweight requests to the Phase 1 Gemini API pool.
  - Route heavy, complex reasoning tasks to the Phase 2 Antigravity pool.
