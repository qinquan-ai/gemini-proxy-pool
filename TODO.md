# AI Proxy Gateway Development Roadmap

## Phase 1: Standard Gemini API Pool (Current Priority)
**Goal:** Build a reliable gateway for legitimate multi-project Gemini capacity and failover.
- [x] **Infrastructure Setup:** FastAPI server, local-only binding, optional bearer auth, configurable CORS.
- [x] **OpenAI Compatibility:** Text, streaming, function calls, generation settings, inline image/audio content.
- [x] **Responses API Adapter:** Stateless `/v1/responses` support for Responses-wire clients.
- [x] **Multi-Key Rotation:** Thread-safe round-robin with health state and in-flight counters.
- [x] **Error Handling & Fallback:** 429 retry delay, auth handling, transient cooldown, and correct 4xx behavior.
- [x] **Offline Testing:** Key manager, translator, API helpers, and dashboard endpoints.
- [ ] **Client Integration:** Verify with AGY/OpenClaw through the custom provider settings.
- [ ] **Video Jobs:** Files API upload, processing poll, job affinity, and structured analysis output.

## Phase 2: Antigravity OAuth Integration (Advanced)
**Goal:** Evaluate supported OAuth integrations separately from the Gemini Developer API key pool.
- [ ] **OAuth Authentication Script:** Write a standalone Python script to perform the initial PKCE OAuth flow and extract the `refresh_token` for each of the 3 accounts.
- [ ] **Token Management System:** 
  - Securely store the 3 `refresh_token`s in `.env`.
  - Implement a background worker in `main.py` that automatically acquires a fresh `access_token` every ~50 minutes for each account.
- [ ] **Protocol Translation (The Hard Part):** 
  - Prefer documented provider APIs. Do not depend on private internal endpoints for production routing.
- [ ] **Hybrid Routing (The Ultimate Gateway):** 
  - Route standard, lightweight requests to the Phase 1 Gemini API pool.
  - Route heavy, complex reasoning tasks to the Phase 2 Antigravity pool.
