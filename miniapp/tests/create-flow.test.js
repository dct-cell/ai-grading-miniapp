import test from "node:test";
import assert from "node:assert/strict";

import {
  MAX_NOTE_CHARS,
  canRequestQuote,
  initialState,
  noteError,
  reduceCreateState,
} from "../services/create-flow.js";
import { createPaymentFlow, PaymentUnconfirmed } from "../services/payments.js";
import { ApiError } from "../services/api.js";

const STAGING = { name: "staging", baseUrl: "https://s.test", auth: "fake", payment: "simulate" };
const PRODUCTION = { name: "production", baseUrl: "https://p.test", auth: "wechat", payment: "wechat" };

/* ---------------------------------------------------------------- wizard state */

test("reference PDF is optional and does not affect quoted pages", () => {
  const state = reduceCreateState(initialState, {
    type: "QUOTE_RECEIVED",
    quote: { id: "q1", page_count: 8, amount_cents: 8000 },
  });
  assert.equal(state.quote.amount_cents, 8000);
  assert.equal(state.referencePdf, null);
});

test("a quote requires a source pdf, service tier and grading standard", () => {
  let state = initialState;
  assert.equal(canRequestQuote(state), false);

  state = reduceCreateState(state, {
    type: "SOURCE_SELECTED",
    file: { path: "wxfile://s.pdf", name: "s.pdf", size: 1024 },
  });
  assert.equal(canRequestQuote(state), false, "service tier and standard still missing");

  state = reduceCreateState(state, { type: "STANDARD_SELECTED", standard: "imo" });
  assert.equal(canRequestQuote(state), false, "service tier still missing");

  state = reduceCreateState(state, {
    type: "SERVICE_TIER_SELECTED",
    serviceTier: "summary_report",
  });
  assert.equal(canRequestQuote(state), true);
});

test("a reference pdf alone never satisfies the source requirement", () => {
  const state = reduceCreateState(initialState, {
    type: "REFERENCE_SELECTED",
    file: { path: "wxfile://r.pdf", name: "r.pdf", size: 512 },
  });
  assert.equal(state.sourcePdf, null);
  assert.equal(canRequestQuote(state), false);
});

test("the note limit matches the server's2000 characters", () => {
  // The server declares MAX_NOTE_CHARS = 2000; a client limit of 4000 would
  // let the user type text that is rejected with a 422 on submit.
  assert.equal(MAX_NOTE_CHARS, 2000);
  assert.equal(noteError("a".repeat(2000)), "");
  assert.match(noteError("a".repeat(2001)), /2000/);
});

test("upload progress is tracked and cleared when the quote arrives", () => {
  let state = reduceCreateState(initialState, {
    type: "SOURCE_SELECTED",
    file: { path: "wxfile://s.pdf", name: "s.pdf", size: 2048 },
  });
  state = reduceCreateState(state, { type: "UPLOAD_STARTED" });
  assert.equal(state.uploading, true);

  state = reduceCreateState(state, { type: "UPLOAD_PROGRESS", progress: 42 });
  assert.equal(state.progress, 42);

  state = reduceCreateState(state, {
    type: "QUOTE_RECEIVED",
    quote: { id: "q9", page_count: 2, amount_cents: 2000 },
  });
  assert.equal(state.uploading, false);
  assert.equal(state.progress, 100);
  assert.equal(state.error, "");
});

test("a failed upload keeps the server message and stops the upload", () => {
  let state = reduceCreateState(initialState, { type: "UPLOAD_STARTED" });
  state = reduceCreateState(state, { type: "UPLOAD_FAILED", error: "PDF 已加密，无法解析。" });
  assert.equal(state.uploading, false);
  assert.equal(state.error, "PDF 已加密，无法解析。");
  assert.equal(state.quote, null);
});

test("changing a file invalidates a quote that priced the previous file", () => {
  let state = reduceCreateState(initialState, {
    type: "SOURCE_SELECTED",
    file: { path: "wxfile://a.pdf", name: "a.pdf", size: 10 },
  });
  state = reduceCreateState(state, { type: "STANDARD_SELECTED", standard: "cmo" });
  state = reduceCreateState(state, {
    type: "SERVICE_TIER_SELECTED",
    serviceTier: "annotated_review",
  });
  state = reduceCreateState(state, {
    type: "QUOTE_RECEIVED",
    quote: { id: "q1", page_count: 3, amount_cents: 3000 },
  });
  assert.ok(state.quote);

  state = reduceCreateState(state, {
    type: "SOURCE_SELECTED",
    file: { path: "wxfile://b.pdf", name: "b.pdf", size: 20 },
  });
  // Keeping the old quote would show a price computed for a different file.
  assert.equal(state.quote, null);
});

test("changing the service tier invalidates the previous quote", () => {
  let state = {
    ...initialState,
    sourcePdf: { path: "wxfile://a.pdf", name: "a.pdf", size: 10 },
    standard: "imo",
    serviceTier: "summary_report",
    quote: { id: "q-summary", amount_cents: 500 },
  };

  state = reduceCreateState(state, {
    type: "SERVICE_TIER_SELECTED",
    serviceTier: "annotated_review",
  });

  assert.equal(state.serviceTier, "annotated_review");
  assert.equal(state.quote, null);
});

/* ------------------------------------------------------------------- payment */

function orderListStub(pages) {
  const queue = [...pages];
  return async () => (queue.length > 1 ? queue.shift() : queue[0]);
}

test("staging pay confirms the order through the server, not the payment ui", async () => {
  const posts = [];
  const flow = createPaymentFlow({
    api: {
      post: async (path, body) => {
        posts.push(path);
        if (path === "/api/v1/payments/prepay") {
          return {
            payment_id: "p1",
            prepay_id: "fake-1",
            amount_cents: 3000,
            client_payload: { fake_prepay_id: "fake-1" },
          };
        }
        return {};
      },
      get: orderListStub([
        { items: [{ id: "o-new", state: "v1_queued" }], next_cursor: null },
      ]),
    },
    profile: STAGING,
    wait: async () => {},
  });

  const order = await flow.payAndConfirm({ quoteId: "q1", knownOrderIds: [] });

  assert.equal(order.id, "o-new");
  assert.deepEqual(posts, [
    "/api/v1/payments/prepay",
    "/api/v1/payments/p1/simulate-success",
  ]);
});

test("production pay calls wx.requestPayment and still waits for the server", async () => {
  const posts = [];
  let requested = 0;
  const flow = createPaymentFlow({
    api: {
      post: async (path) => {
        posts.push(path);
        return {
          payment_id: "p2",
          prepay_id: "wx-1",
          amount_cents: 3000,
          client_payload: { timeStamp: "1", nonceStr: "n", package: "prepay_id=x", paySign: "s" },
        };
      },
      get: orderListStub([
        { items: [], next_cursor: null },
        { items: [{ id: "o-2", state: "v1_queued" }], next_cursor: null },
      ]),
    },
    profile: PRODUCTION,
    requestPayment: async () => {
      requested += 1;
      return { errMsg: "requestPayment:ok" };
    },
    wait: async () => {},
  });

  const order = await flow.payAndConfirm({ quoteId: "q1", knownOrderIds: [] });

  assert.equal(requested, 1);
  assert.equal(order.id, "o-2");
  // The fake simulate endpoint must never be called in production: it is not
  // even registered there.
  assert.deepEqual(posts, ["/api/v1/payments/prepay"]);
});

test("a successful wx.requestPayment callback alone is NOT treated as paid", async () => {
  const flow = createPaymentFlow({
    api: {
      post: async () => ({
        payment_id: "p3",
        prepay_id: "wx-2",
        amount_cents: 3000,
        client_payload: {},
      }),
      // The server never reports a new order: no verified callback arrived.
      get: orderListStub([{ items: [], next_cursor: null }]),
    },
    profile: PRODUCTION,
    requestPayment: async () => ({ errMsg: "requestPayment:ok" }),
    wait: async () => {},
    maxAttempts: 3,
  });

  // This is the invariant: only a server-verified callback creates an order,
  // so an unconfirmed payment must surface as unconfirmed rather than success.
  await assert.rejects(
    () => flow.payAndConfirm({ quoteId: "q1", knownOrderIds: [] }),
    error => error instanceof PaymentUnconfirmed,
  );
});

test("only an order absent before payment counts as this payment's order", async () => {
  const flow = createPaymentFlow({
    api: {
      post: async () => ({
        payment_id: "p4",
        prepay_id: "fake-4",
        amount_cents: 1000,
        client_payload: {},
      }),
      get: orderListStub([
        // The user's pre-existing order must not be mistaken for the new one.
        { items: [{ id: "o-old", state: "v1_delivered" }], next_cursor: null },
        {
          items: [
            { id: "o-fresh", state: "v1_queued" },
            { id: "o-old", state: "v1_delivered" },
          ],
          next_cursor: null,
        },
      ]),
    },
    profile: STAGING,
    wait: async () => {},
  });

  const order = await flow.payAndConfirm({ quoteId: "q1", knownOrderIds: ["o-old"] });
  assert.equal(order.id, "o-fresh");
});

test("a cancelled payment reports cancellation without polling", async () => {
  let polls = 0;
  const flow = createPaymentFlow({
    api: {
      post: async () => ({
        payment_id: "p5",
        prepay_id: "wx-5",
        amount_cents: 1000,
        client_payload: {},
      }),
      get: async () => {
        polls += 1;
        return { items: [], next_cursor: null };
      },
    },
    profile: PRODUCTION,
    requestPayment: async () => {
      throw { errMsg: "requestPayment:fail cancel" };
    },
    wait: async () => {},
  });

  await assert.rejects(() => flow.payAndConfirm({ quoteId: "q1", knownOrderIds: [] }), /取消/);
  assert.equal(polls, 0);
});

test("an expired quote surfaces the server's own message", async () => {
  const flow = createPaymentFlow({
    api: {
      post: async () => {
        throw new ApiError(404, "报价不存在或已失效。");
      },
      get: async () => ({ items: [], next_cursor: null }),
    },
    profile: STAGING,
    wait: async () => {},
  });

  await assert.rejects(
    () => flow.payAndConfirm({ quoteId: "q-expired", knownOrderIds: [] }),
    /报价不存在或已失效/,
  );
});
