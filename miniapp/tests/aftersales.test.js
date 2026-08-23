import test from "node:test";
import assert from "node:assert/strict";

import {
  ACTION_LABELS,
  REFUND_REASONS,
  actionsFor,
  createAftersalesService,
  describeRefundOutcome,
  reviewTextError,
} from "../services/aftersales.js";
import { ApiError } from "../services/api.js";

/* -------------------------------------------------------------------- actions */

test("v1 renders three actions and v2 renders two", () => {
  assert.deepEqual(actionsFor(["accept", "review", "refund"]), ["accept", "review", "refund"]);
  assert.deepEqual(actionsFor(["accept", "refund"]), ["accept", "refund"]);
});

test("only server-supplied actions are rendered", () => {
  // The mini-program never adds an action the server did not offer: the server
  // owns refund eligibility and the single-review rule.
  assert.deepEqual(actionsFor([]), []);
  assert.deepEqual(actionsFor(undefined), []);
  assert.deepEqual(actionsFor(null), []);
});

test("a v2 order never gets a review button even if something injects one", () => {
  // V2 has no review endpoint (the server answers 409), so an unknown or
  // stale action must be dropped rather than rendered as a dead button.
  assert.deepEqual(actionsFor(["accept", "refund", "review_again"]), ["accept", "refund"]);
});

test("actions keep a stable display order regardless of server ordering", () => {
  assert.deepEqual(actionsFor(["refund", "accept", "review"]), ["accept", "review", "refund"]);
});

test("every renderable action has a label", () => {
  for (const action of actionsFor(["accept", "review", "refund"])) {
    assert.ok(ACTION_LABELS[action], action);
  }
});

/* --------------------------------------------------------------------- review */

test("a review explanation must not be blank", () => {
  // The server rejects whitespace-only text with a 422.
  assert.match(reviewTextError(""), /填写/);
  assert.match(reviewTextError("   "), /填写/);
  assert.equal(reviewTextError("第二题的构造被判为不严谨，请复核。"), "");
});

test("the review explanation is capped at the server's 2000 characters", () => {
  assert.equal(reviewTextError("a".repeat(2000)), "");
  assert.match(reviewTextError("a".repeat(2001)), /2000/);
});

/* -------------------------------------------------------------- idempotency */

test("each action sends an idempotency key to the server", async () => {
  const calls = [];
  const service = createAftersalesService({
    api: {
      post: async (path, body, options) => {
        calls.push({ path, body, options });
        return { order_id: "o1", state: "accepted" };
      },
    },
  });

  await service.accept("o1");

  assert.equal(calls[0].path, "/api/v1/orders/o1/accept");
  const key = calls[0].options.header["Idempotency-Key"];
  assert.ok(key && key.length > 8, "an idempotency key must be sent");
});

test("a retry of the same action reuses its idempotency key", async () => {
  const keys = [];
  const service = createAftersalesService({
    api: {
      post: async (path, body, options) => {
        keys.push(options.header["Idempotency-Key"]);
        throw new ApiError(0, "网络连接失败");
      },
    },
  });

  const attempt = () => service.refund("o1", { reason: "grading_disputed" }).catch(() => {});
  await attempt();
  await service.retryLast().catch(() => {});

  // Reusing the key keeps the retry labelled as the same attempt. Note the
  // server does not de-duplicate on this header today: a double refund is
  // actually prevented by its conditional state UPDATE (the loser gets 409)
  // and the one-live-refund-per-payment rule.
  assert.equal(keys.length, 2);
  assert.equal(keys[0], keys[1]);
});

test("a different action gets a different key", async () => {
  const keys = [];
  const service = createAftersalesService({
    api: {
      post: async (path, body, options) => {
        keys.push(options.header["Idempotency-Key"]);
        return { order_id: "o1", state: "accepted" };
      },
    },
  });

  await service.accept("o1");
  await service.refund("o1", { reason: "too_slow" });

  assert.notEqual(keys[0], keys[1]);
});

test("the refund request never sends an amount", async () => {
  const bodies = [];
  const service = createAftersalesService({
    api: {
      post: async (path, body) => {
        bodies.push(body);
        return { order_id: "o1", state: "refunded", amount_cents: 3000 };
      },
    },
  });

  await service.refund("o1", { reason: "uploaded_wrong_pdf" });

  // The refund is always the full paid amount, decided by the server.
  assert.equal("amount_cents" in bodies[0], false);
  assert.equal("amount" in bodies[0], false);
  assert.deepEqual(Object.keys(bodies[0]), ["reason"]);
});

test("the review request sends only the explanation text", async () => {
  const bodies = [];
  const service = createAftersalesService({
    api: {
      post: async (path, body) => {
        bodies.push(body);
        return { order_id: "o1", state: "v2_queued" };
      },
    },
  });

  await service.review("o1", { text: "请复核第三题" });

  // No file may be attached: a review re-grades the same immutable PDF.
  assert.deepEqual(Object.keys(bodies[0]), ["text"]);
});

test("ownership is never sent by the client", async () => {
  const bodies = [];
  const service = createAftersalesService({
    api: {
      post: async (path, body) => {
        bodies.push(body || {});
        return { order_id: "o1", state: "accepted" };
      },
    },
  });

  await service.accept("o1");
  await service.review("o1", { text: "t" });
  await service.refund("o1", { reason: "other" });

  for (const body of bodies) {
    assert.equal("user_id" in body, false);
    assert.equal("owner_user_id" in body, false);
  }
});

/* ------------------------------------------------------------ refund outcome */

test("an automatic refund and a pending refund read differently", () => {
  const automatic = describeRefundOutcome({ state: "refunded", amount_cents: 3000 });
  const pending = describeRefundOutcome({ state: "refund_pending", amount_cents: 3000 });

  assert.match(automatic.title, /已退款|退款成功/);
  assert.equal(automatic.completed, true);
  assert.match(pending.title, /审核|处理/);
  assert.equal(pending.completed, false);
});

test("the refund outcome never exposes internal policy metrics", () => {
  const outcome = describeRefundOutcome({
    state: "refund_pending",
    amount_cents: 3000,
    // Even if the server ever returned these, they must not be shown.
    monthly_refund_count: 3,
    refund_ratio: 0.42,
  });

  const rendered = JSON.stringify(outcome);
  assert.equal(rendered.includes("0.42"), false);
  assert.equal(rendered.includes("monthly"), false);
});

test("the four server refund reasons are offered", () => {
  assert.deepEqual(
    REFUND_REASONS.map(reason => reason.value),
    ["uploaded_wrong_pdf", "grading_disputed", "too_slow", "other"],
  );
});
