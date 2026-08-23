# Phase 06 Native Mini-Program Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the native WeChat mini-program that runs the complete staging flow from onboarding and PDF upload through order delivery, review and refund.

**Architecture:** Use native WXML/WXSS/JavaScript with a small API client and page-local state. The backend remains authoritative for identity, price, actions, order state and download permission; the mini-program never computes refund eligibility or trusts payment UI success.

**Tech Stack:** WeChat Mini Program, JavaScript, WXML, WXSS, Node test runner, WeChat Developer Tools test account

---

### Task 1: Scaffold the native project and API client

**Files:**
- Create: miniapp/app.js
- Create: miniapp/app.json
- Create: miniapp/app.wxss
- Create: miniapp/project.config.json
- Create: miniapp/services/api.js
- Create: miniapp/services/session.js
- Create: miniapp/tests/api.test.js
- Create: miniapp/package.json

- [ ] **Step 1: Write the failing API client test**

    import test from "node:test";
    import assert from "node:assert/strict";
    import { createApiClient } from "../services/api.js";

    test("adds bearer token and rejects non-2xx responses", async () => {
      const calls = [];
      const client = createApiClient({
        baseUrl: "https://staging.example.test",
        getToken: () => "token-1",
        request: async options => {
          calls.push(options);
          return { statusCode: 401, data: { detail: "expired" } };
        },
      });
      await assert.rejects(() => client.get("/api/v1/orders"), /expired/);
      assert.equal(calls[0].header.Authorization, "Bearer token-1");
    });

- [ ] **Step 2: Confirm failure**

    cd miniapp && npm test

Expected: services/api.js is missing.

- [ ] **Step 3: Implement request and upload wrappers**

createApiClient must expose get, post and uploadPdf. It adds Authorization only when a token exists, applies a 30-second request timeout, normalizes errors to ApiError(status, detail), and calls an onUnauthorized hook on 401. uploadPdf uses wx.uploadFile and parses JSON from response.data.

- [ ] **Step 4: Add navigation**

Configure three tabs:
- pages/home/index
- pages/create/upload
- pages/orders/index

Use Chinese labels 首页, 批改, 订单. Do not add a fourth profile tab; avatar opens pages/account/index.

- [ ] **Step 5: Run and commit**

    cd miniapp && npm test
    git add miniapp
    git commit -m "feat: scaffold native miniapp"

### Task 2: Implement staging login and home

**Files:**
- Create: miniapp/pages/home/index.js
- Create: miniapp/pages/home/index.json
- Create: miniapp/pages/home/index.wxml
- Create: miniapp/pages/home/index.wxss
- Create: miniapp/pages/account/index.js
- Create: miniapp/pages/account/index.json
- Create: miniapp/pages/account/index.wxml
- Create: miniapp/pages/account/index.wxss
- Create: miniapp/services/auth.js
- Test: miniapp/tests/auth.test.js

- [ ] **Step 1: Write the session reuse test**

    test("reuses a valid session before requesting a new staging identity", async () => {
      const storage = new Map([["session", { token: "t", expiresAt: Date.now() + 60_000 }]]);
      const auth = createAuthService({ storage, api: { me: async () => ({ id: "u1" }) } });
      assert.equal((await auth.ensureLogin()).id, "u1");
    });

- [ ] **Step 2: Implement ensureLogin**

For staging fake auth, create one random device identity and persist it locally, then send code test-device-<identity>. For the later WeChat adapter, call wx.login and send the returned code unchanged. Store token and expiry; always verify /api/v1/auth/me at app launch.

- [ ] **Step 3: Implement dynamic home**

New users see welcome, three-step guide, CNY 10/page and the three scoring standards. Returning users see active order, create button, recent result and an operations banner. All text comes from backend configuration where it can change operationally.

- [ ] **Step 4: Run and commit**

    cd miniapp && npm test
    git add miniapp/pages/home miniapp/pages/account miniapp/services/auth.js miniapp/tests/auth.test.js
    git commit -m "feat: add miniapp login and home"

### Task 3: Implement the three-step creation wizard

**Files:**
- Create: miniapp/pages/create/upload.js
- Create: miniapp/pages/create/upload.json
- Create: miniapp/pages/create/upload.wxml
- Create: miniapp/pages/create/upload.wxss
- Create: miniapp/pages/create/options.js
- Create: miniapp/pages/create/options.json
- Create: miniapp/pages/create/options.wxml
- Create: miniapp/pages/create/options.wxss
- Create: miniapp/pages/create/payment.js
- Create: miniapp/pages/create/payment.json
- Create: miniapp/pages/create/payment.wxml
- Create: miniapp/pages/create/payment.wxss
- Create: miniapp/components/pdf-picker/index.js
- Create: miniapp/components/pdf-picker/index.json
- Create: miniapp/components/pdf-picker/index.wxml
- Create: miniapp/components/pdf-picker/index.wxss
- Create: miniapp/components/price-summary/index.js
- Create: miniapp/components/price-summary/index.json
- Create: miniapp/components/price-summary/index.wxml
- Create: miniapp/components/price-summary/index.wxss
- Test: miniapp/tests/create-flow.test.js

- [ ] **Step 1: Write wizard-state tests**

    test("reference PDF is optional and does not affect quoted pages", () => {
      const state = reduceCreateState(initialState, {
        type: "QUOTE_RECEIVED",
        quote: { id: "q1", page_count: 8, amount_cents: 8000 },
      });
      assert.equal(state.quote.amount_cents, 8000);
      assert.equal(state.referencePdf, null);
    });

- [ ] **Step 2: Implement upload page**

Require one source PDF and label it 必须同时包含题目和学生作答. Allow one optional standard-answer/rubric PDF. Display filename, size, upload progress and validation errors. Prevent leaving while an upload is active.

- [ ] **Step 3: Implement options page**

Render league second round, CMO and IMO as native selectable cards. Provide a 4000-character optional note. Create the quote only after both file selection and standard are valid.

- [ ] **Step 4: Implement payment page**

Display filenames, source page count, price version, unit price and total. In staging, call the authenticated simulate-success endpoint that is enabled only outside production. In production, call wx.requestPayment and then poll order status; never mark paid solely from the wx.requestPayment success callback.

- [ ] **Step 5: Run and commit**

    cd miniapp && npm test
    git add miniapp/pages/create miniapp/components miniapp/tests/create-flow.test.js
    git commit -m "feat: add native grading wizard"

### Task 4: Implement order tabs and detail

**Files:**
- Create: miniapp/pages/orders/index.js
- Create: miniapp/pages/orders/index.json
- Create: miniapp/pages/orders/index.wxml
- Create: miniapp/pages/orders/index.wxss
- Create: miniapp/pages/orders/detail.js
- Create: miniapp/pages/orders/detail.json
- Create: miniapp/pages/orders/detail.wxml
- Create: miniapp/pages/orders/detail.wxss
- Create: miniapp/components/order-card/index.js
- Create: miniapp/components/order-card/index.json
- Create: miniapp/components/order-card/index.wxml
- Create: miniapp/components/order-card/index.wxss
- Create: miniapp/components/status-pill/index.js
- Create: miniapp/components/status-pill/index.json
- Create: miniapp/components/status-pill/index.wxml
- Create: miniapp/components/status-pill/index.wxss
- Test: miniapp/tests/orders.test.js

- [ ] **Step 1: Write status grouping tests**

    test("groups server states into three order filters", () => {
      assert.equal(filterForState("v1_running"), "grading");
      assert.equal(filterForState("v2_queued"), "grading");
      assert.equal(filterForState("v1_delivered"), "acceptance");
      assert.equal(filterForState("accepted"), "all");
    });

- [ ] **Step 2: Implement filters**

Use 全部, 批改中 and 待验收. 批改中 includes V1/V2 queued/running and system-processing worker exceptions. 待验收 includes V1/V2 delivered and refund pending. Use cursor pagination and pull-to-refresh.

- [ ] **Step 3: Implement detail polling**

Poll active orders every 15 seconds while visible, stop on hide/unload, and display the server ETA range rather than a local countdown. Show original quote, standard, note, round history, appeal text, result summary and available actions.

- [ ] **Step 4: Implement secure download**

Request a short-lived download token, call wx.downloadFile, then wx.openDocument or wx.saveFile. On 401/403/410 refresh the order and show the server message. Never store a permanent file URL.

- [ ] **Step 5: Run and commit**

    cd miniapp && npm test
    git add miniapp/pages/orders miniapp/components miniapp/tests/orders.test.js
    git commit -m "feat: add miniapp order history and results"

### Task 5: Implement accept, review and refund UX

**Files:**
- Create: miniapp/pages/aftersales/review.js
- Create: miniapp/pages/aftersales/review.json
- Create: miniapp/pages/aftersales/review.wxml
- Create: miniapp/pages/aftersales/review.wxss
- Create: miniapp/pages/aftersales/refund.js
- Create: miniapp/pages/aftersales/refund.json
- Create: miniapp/pages/aftersales/refund.wxml
- Create: miniapp/pages/aftersales/refund.wxss
- Test: miniapp/tests/aftersales.test.js

- [ ] **Step 1: Write action tests**

    test("v1 renders three actions and v2 renders two", () => {
      assert.deepEqual(actionsFor(["accept", "review", "refund"]), ["accept", "review", "refund"]);
      assert.deepEqual(actionsFor(["accept", "refund"]), ["accept", "refund"]);
    });

- [ ] **Step 2: Implement V1 review**

Show the immutable original PDF identity and prohibit file selection. Require a non-empty explanation, show remaining deadline, submit once, and replace the page with V2 queued state after 202.

- [ ] **Step 3: Implement refunds**

Display full refund amount only; do not allow amount editing. Offer reason choices including uploaded_wrong_pdf and grading_disputed plus optional details. Show automatic refund, manual review or completed status returned by the server.

- [ ] **Step 4: Implement confirmation safeguards**

Disable buttons after first tap, include an idempotency key per action, and refresh order state after network errors before offering retry.

- [ ] **Step 5: Run automated and real-device gate**

    cd miniapp && npm test

Then in WeChat Developer Tools use a test account, enable development-domain bypass, and verify on a real phone:
1. login;
2. upload source and optional reference;
3. quote and fake pay;
4. observe Worker result;
5. V1 review and V2 refund;
6. result download before refund and denial after refund.

- [ ] **Step 6: Commit**

    git add miniapp/pages/aftersales miniapp/tests/aftersales.test.js
    git commit -m "feat: complete miniapp aftersales flow"
