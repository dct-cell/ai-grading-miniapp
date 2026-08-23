/**
 * Aftersales actions: accept, review, refund.
 *
 * Two rules shape this module:
 *
 * 1. **The server decides what is allowed.** Buttons are rendered from the
 *    order's `available_actions`; this file never computes refund eligibility,
 *    never counts reviews and never inspects refund policy. `available_actions`
 *    is advisory — each endpoint re-checks its own conditions in a transaction —
 *    so a rejected action is reported, not prevented locally.
 *
 * 2. **A retry must not double-charge.** Each attempt carries an
 *    `Idempotency-Key`, and a retry of the *same* attempt reuses it.
 *
 *    Be precise about what this buys today: the server does **not** yet
 *    de-duplicate on this header. What actually prevents a double refund is the
 *    server's conditional `UPDATE ... WHERE state = :observed` — the second
 *    request loses the state race and gets a 409 — plus the rule that one
 *    payment may have only one live refund row. The header is sent so a future
 *    server-side de-duplication has a stable key to work with, and so a retry
 *    is already correctly labelled. It is not the reason a retry is safe.
 *
 * Status codes differ per action and must not be collapsed: accept returns 200,
 * review and refund return 202.
 */

/** Matches the server's review text limit. */
export const MAX_REVIEW_CHARS = 2000;

/** Rendered in this order regardless of how the server lists them. */
const ACTION_ORDER = ["accept", "review", "refund"];

export const ACTION_LABELS = Object.freeze({
  accept: "验收",
  review: "申请复核",
  refund: "申请退款",
});

export const REFUND_REASONS = Object.freeze([
  Object.freeze({ value: "uploaded_wrong_pdf", label: "上传了错误的文件" }),
  Object.freeze({ value: "grading_disputed", label: "对批改结果有异议" }),
  Object.freeze({ value: "too_slow", label: "批改太慢" }),
  Object.freeze({ value: "other", label: "其他原因" }),
]);

/**
 * The actions to render, filtered to the ones this client understands.
 *
 * Unknown values are dropped rather than rendered as dead buttons.
 */
export function actionsFor(availableActions) {
  if (!Array.isArray(availableActions)) {
    return [];
  }
  return ACTION_ORDER.filter(action => availableActions.includes(action));
}

export function reviewTextError(text) {
  const trimmed = (text || "").trim();
  if (trimmed === "") {
    return "请填写需要复核的具体问题。";
  }
  if ((text || "").length > MAX_REVIEW_CHARS) {
    return `复核说明最多 ${MAX_REVIEW_CHARS} 字。`;
  }
  return "";
}

/**
 * How to present a refund the server has just routed.
 *
 * `refunded` means the gateway already returned the money; `refund_pending`
 * means an Admin has to approve it. Only the amount and the state are shown —
 * never the internal policy metrics that decided the routing.
 */
export function describeRefundOutcome(outcome) {
  const completed = outcome && outcome.state === "refunded";
  return {
    completed: Boolean(completed),
    state: (outcome && outcome.state) || "",
    amountCents: (outcome && outcome.amount_cents) || 0,
    title: completed ? "退款成功" : "退款申请已提交，等待人工审核",
    detail: completed
      ? "款项将退回原支付渠道，到账时间取决于支付渠道。批改结果的下载权限已关闭。"
      : "我们会尽快处理这笔退款申请。在此期间批改结果仍可下载。",
  };
}

function newIdempotencyKey() {
  const random = Math.random().toString(36).slice(2);
  return `${Date.now().toString(36)}-${random}`;
}

export function createAftersalesService({ api, keyFactory = newIdempotencyKey }) {
  /** The most recent attempt, so a retry can reuse its key. */
  let lastAttempt = null;

  function send({ path, body }) {
    const attempt = { path, body, key: keyFactory() };
    lastAttempt = attempt;
    return dispatch(attempt);
  }

  function dispatch(attempt) {
    return api.post(attempt.path, attempt.body, {
      header: { "Idempotency-Key": attempt.key },
    });
  }

  return {
    /** 200 on success. */
    accept(orderId) {
      return send({ path: `/api/v1/orders/${orderId}/accept`, body: {} });
    },

    /** 202: a Worker grades the second round later. */
    review(orderId, { text }) {
      return send({ path: `/api/v1/orders/${orderId}/review`, body: { text } });
    },

    /**
     * 202. The amount is deliberately absent: the server always refunds the
     * full paid amount and does not accept a client-supplied figure.
     */
    refund(orderId, { reason }) {
      return send({ path: `/api/v1/orders/${orderId}/refund`, body: { reason } });
    },

    /** Retry the last attempt with its original key. */
    retryLast() {
      if (!lastAttempt) {
        return Promise.reject(new Error("no action to retry"));
      }
      return dispatch(lastAttempt);
    },
  };
}
