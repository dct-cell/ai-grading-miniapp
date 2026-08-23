import test from "node:test";
import assert from "node:assert/strict";

import {
  DETAIL_ACTION_KINDS,
  resolveDetailActions,
} from "../services/detail-actions.js";
import { createOrderNavigationIntent } from "../services/order-navigation.js";

test("post-payment navigation intent is consumed exactly once", () => {
  const intent = createOrderNavigationIntent();
  intent.set({ category: "grading", orderId: "order-1" });

  assert.deepEqual(intent.consume(), { category: "grading", orderId: "order-1" });
  assert.equal(intent.consume(), null);
});

test("navigation intent rejects an unknown task category", () => {
  const intent = createOrderNavigationIntent();
  intent.set({ category: "not-a-server-filter", orderId: "order-2" });

  assert.deepEqual(intent.consume(), { category: "all", orderId: "order-2" });
});

test("grading detail prioritizes wayfinding and hides refund", () => {
  const footer = resolveDetailActions({
    state: "v1_running",
    actions: ["refund"],
  });

  assert.deepEqual(footer, {
    primaryLabel: "继续提交",
    primaryKind: DETAIL_ACTION_KINDS.CREATE,
    secondaryLabel: "返回首页",
    secondaryKind: DETAIL_ACTION_KINDS.HOME,
    roundNumber: 0,
    showHistoryDownloads: false,
  });
});

test("a review being graded keeps its previous report in history", () => {
  const footer = resolveDetailActions({
    state: "v2_running",
    actions: ["refund"],
    newestRound: { round_number: 1 },
  });

  assert.equal(footer.primaryKind, DETAIL_ACTION_KINDS.CREATE);
  assert.equal(footer.secondaryKind, DETAIL_ACTION_KINDS.HOME);
  assert.equal(footer.showHistoryDownloads, true);
});

test("a delivered task keeps report and server-owned order actions", () => {
  const footer = resolveDetailActions({
    state: "v1_delivered",
    actions: ["accept", "review", "refund"],
    newestRound: { round_number: 1 },
  });

  assert.deepEqual(footer, {
    primaryLabel: "打开批改报告",
    primaryKind: DETAIL_ACTION_KINDS.DOWNLOAD,
    secondaryLabel: "订单操作",
    secondaryKind: DETAIL_ACTION_KINDS.ORDER_ACTIONS,
    roundNumber: 1,
    showHistoryDownloads: false,
  });
});
