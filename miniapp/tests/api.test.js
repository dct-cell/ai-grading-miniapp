import test from "node:test";
import assert from "node:assert/strict";

import { ApiError, createApiClient } from "../services/api.js";

/**
 * The API client is the single place where the mini-program talks to the
 * server, so it is also the single place where the authentication domain
 * boundary and the error contract are enforced. `request` is injected so
 * these tests run under plain Node with no `wx` global in sight.
 */

function stubRequest(responses) {
  const calls = [];
  const queue = Array.isArray(responses) ? [...responses] : [responses];
  const request = async options => {
    calls.push(options);
    const next = queue.length > 1 ? queue.shift() : queue[0];
    if (next instanceof Error) {
      throw next;
    }
    return next;
  };
  return { calls, request };
}

test("adds bearer token and rejects non-2xx responses", async () => {
  const { calls, request } = stubRequest({
    statusCode: 401,
    data: { detail: "expired" },
  });
  const client = createApiClient({
    baseUrl: "https://staging.example.test",
    getToken: () => "token-1",
    request,
  });

  await assert.rejects(() => client.get("/api/v1/orders"), /expired/);
  assert.equal(calls[0].header.Authorization, "Bearer token-1");
});

test("omits the Authorization header entirely when no token is held", async () => {
  const { calls, request } = stubRequest({ statusCode: 200, data: {} });
  const client = createApiClient({
    baseUrl: "https://staging.example.test",
    getToken: () => null,
    request,
  });

  await client.post("/api/v1/auth/login", { code: "test-device-abc" });

  // Sending `Authorization: Bearer null` would be a bug: the server would
  // reject a call that is supposed to be anonymous.
  assert.equal("Authorization" in calls[0].header, false);
});

test("builds absolute urls from the configured base", async () => {
  const { calls, request } = stubRequest({ statusCode: 200, data: {} });
  const client = createApiClient({
    baseUrl: "https://staging.example.test",
    getToken: () => "t",
    request,
  });

  await client.get("/api/v1/me");

  assert.equal(calls[0].url, "https://staging.example.test/api/v1/me");
});

test("surfaces the server detail and status on ApiError", async () => {
  const { request } = stubRequest({
    statusCode: 409,
    data: { detail: "订单状态已变更。" },
  });
  const client = createApiClient({
    baseUrl: "https://staging.example.test",
    getToken: () => "t",
    request,
  });

  // The UI shows server-authored messages, so the detail has to survive
  // transport verbatim rather than being replaced by a client-side guess.
  const error = await client.get("/api/v1/orders/o1").catch(caught => caught);
  assert.ok(error instanceof ApiError);
  assert.equal(error.status, 409);
  assert.equal(error.detail, "订单状态已变更。");
});

test("calls onUnauthorized exactly once per 401", async () => {
  const { request } = stubRequest({
    statusCode: 401,
    data: { detail: "会话已过期。" },
  });
  let unauthorized = 0;
  const client = createApiClient({
    baseUrl: "https://staging.example.test",
    getToken: () => "stale",
    request,
    onUnauthorized: () => {
      unauthorized += 1;
    },
  });

  await assert.rejects(() => client.get("/api/v1/me"));
  assert.equal(unauthorized, 1);
});

test("does not treat 403 or 410 as a session problem", async () => {
  const { request } = stubRequest({
    statusCode: 410,
    data: { detail: "下载权限已被撤销。" },
  });
  let unauthorized = 0;
  const client = createApiClient({
    baseUrl: "https://staging.example.test",
    getToken: () => "t",
    request,
    onUnauthorized: () => {
      unauthorized += 1;
    },
  });

  await assert.rejects(() => client.get("/api/v1/orders/o1/rounds/1/result/result_pdf"));
  // A revoked download must not log the user out.
  assert.equal(unauthorized, 0);
});

test("applies a 30 second timeout to every request", async () => {
  const { calls, request } = stubRequest({ statusCode: 200, data: {} });
  const client = createApiClient({
    baseUrl: "https://staging.example.test",
    getToken: () => "t",
    request,
  });

  await client.get("/api/v1/me");

  assert.equal(calls[0].timeout, 30_000);
});

test("normalizes transport failures into ApiError with status 0", async () => {
  const { request } = stubRequest(new Error("network down"));
  const client = createApiClient({
    baseUrl: "https://staging.example.test",
    getToken: () => "t",
    request,
  });

  const error = await client.get("/api/v1/me").catch(caught => caught);
  assert.ok(error instanceof ApiError);
  assert.equal(error.status, 0);
  assert.match(error.detail, /网络/);
});

test("parses the string body returned by wx.uploadFile", async () => {
  const calls = [];
  const client = createApiClient({
    baseUrl: "https://staging.example.test",
    getToken: () => "token-1",
    request: async () => ({ statusCode: 200, data: {} }),
    upload: async options => {
      calls.push(options);
      // wx.uploadFile hands back a *string*, unlike wx.request.
      return { statusCode: 201, data: JSON.stringify({ id: "q1", page_count: 3 }) };
    },
  });

  const quote = await client.uploadPdf({
    path: "/api/v1/quotes",
    filePath: "wxfile://tmp/source.pdf",
    name: "source_pdf",
    formData: { grading_standard: "imo", note: "" },
  });

  assert.equal(quote.id, "q1");
  assert.equal(quote.page_count, 3);
  assert.equal(calls[0].header.Authorization, "Bearer token-1");
  assert.equal(calls[0].name, "source_pdf");
});

test("rejects an upload whose body is not the JSON the server promises", async () => {
  const client = createApiClient({
    baseUrl: "https://staging.example.test",
    getToken: () => "t",
    request: async () => ({ statusCode: 200, data: {} }),
    upload: async () => ({ statusCode: 200, data: "<html>gateway error</html>" }),
  });

  await assert.rejects(
    () =>
      client.uploadPdf({
        path: "/api/v1/quotes",
        filePath: "wxfile://tmp/source.pdf",
        name: "source_pdf",
        formData: {},
      }),
    error => error instanceof ApiError,
  );
});

test("reports the server detail when an upload is rejected", async () => {
  const client = createApiClient({
    baseUrl: "https://staging.example.test",
    getToken: () => "t",
    request: async () => ({ statusCode: 200, data: {} }),
    upload: async () => ({
      statusCode: 400,
      data: JSON.stringify({ detail: "PDF 页数超出上限。" }),
    }),
  });

  const error = await client
    .uploadPdf({
      path: "/api/v1/quotes",
      filePath: "wxfile://tmp/source.pdf",
      name: "source_pdf",
      formData: {},
    })
    .catch(caught => caught);
  assert.equal(error.status, 400);
  assert.equal(error.detail, "PDF 页数超出上限。");
});

test("uploadPdf preserves the optional UploadTask progress callback", async () => {
  const progress = () => {};
  let receivedProgress = null;
  const client = createApiClient({
    baseUrl: "https://staging.example.test",
    getToken: () => "t",
    request: async () => ({ statusCode: 200, data: {} }),
    upload: async (options, onProgress) => {
      receivedProgress = onProgress;
      return { statusCode: 201, data: JSON.stringify({ id: "q2" }) };
    },
  });

  await client.uploadPdf({
    path: "/api/v1/quotes",
    filePath: "wxfile://source.pdf",
    name: "source_pdf",
    formData: {},
    onProgress: progress,
  });

  assert.equal(receivedProgress, progress);
});
