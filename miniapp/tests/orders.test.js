import test from "node:test";
import assert from "node:assert/strict";

import {
  FILTERS,
  deliveredRounds,
  displayLabel,
  filterForState,
  hasSystemException,
  isActive,
  isGradingState,
  isPulsingProgress,
  progressLabel,
  roundStateLabel,
  stateLabel,
} from "../utils/order-states.js";
import {
  createOrderPoller,
  createOrderProgressPoller,
  createOrderService,
} from "../services/orders.js";
import { decorateSummary } from "../utils/decorate.js";
import { DownloadRefused, createDownloadService, normalizeSummary } from "../services/downloads.js";
import { ApiError } from "../services/api.js";

/* ------------------------------------------------------------------ grouping */

test("groups server states into three order filters", () => {
  assert.equal(filterForState("v1_running"), "grading");
  assert.equal(filterForState("v2_queued"), "grading");
  assert.equal(filterForState("v1_delivered"), "acceptance");
  assert.equal(filterForState("accepted"), "all");
});

test("mirrors the server's category mapping for every order state", () => {
  // Must agree with server/services/orders.py, or a tab would show orders the
  // server's ?category= filter excludes.
  const expected = {
    awaiting_payment: FILTERS.ALL,
    v1_queued: FILTERS.GRADING,
    v1_running: FILTERS.GRADING,
    v2_queued: FILTERS.GRADING,
    v2_running: FILTERS.GRADING,
    v1_delivered: FILTERS.ACCEPTANCE,
    v2_delivered: FILTERS.ACCEPTANCE,
    refund_pending: FILTERS.ACCEPTANCE,
    refunded: FILTERS.ALL,
    accepted: FILTERS.ALL,
  };
  for (const [state, filter] of Object.entries(expected)) {
    assert.equal(filterForState(state), filter, state);
  }
});

test("a stalled job reads as 系统处理中 rather than a job state", () => {
  // worker_exception is a *job* state; the order stays v1_running. Showing the
  // raw state would imply the user has to act.
  const rounds = [{ round_number: 1, state: "worker_exception", delivered_at: null }];
  assert.equal(hasSystemException(rounds), true);
  assert.equal(displayLabel("v1_running", rounds), "系统处理中");
  assert.equal(displayLabel("v1_running", [{ round_number: 1, state: "running" }]), "批改中");
});

test("system exception only applies to orders still being graded", () => {
  const rounds = [{ round_number: 1, state: "worker_exception" }];
  // A delivered order is awaiting the user, whatever an old job row says.
  assert.equal(displayLabel("v1_delivered", rounds), stateLabel("v1_delivered"));
});

test("only orders with outstanding work are polled", () => {
  assert.equal(isActive("v1_running"), true);
  assert.equal(isActive("v2_queued"), true);
  assert.equal(isActive("awaiting_payment"), true);
  assert.equal(isActive("v1_delivered"), false);
  assert.equal(isActive("accepted"), false);
  assert.equal(isActive("refunded"), false);
  assert.equal(isGradingState("v1_running"), true);
  assert.equal(isGradingState("awaiting_payment"), false);
});

test("progress stages use short list labels and complete detail labels", () => {
  const labels = {
    queued: ["排队中", "排队中"],
    assigned: ["准备批改", "正在准备批改"],
    preparing: ["读取答卷", "正在读取答卷"],
    understanding: ["理解题目", "正在理解题目与作答"],
    rubric: ["整理评分点", "正在整理评分要点"],
    decomposing: ["梳理解答", "正在梳理解答步骤"],
    verifying: ["核验推理", "正在核验关键推理"],
    scoring: ["计算得分", "正在计算得分"],
    auditing: ["复核判分", "正在复核判分"],
    reporting: ["生成报告", "正在生成批改报告"],
    validating: ["检查报告", "正在检查批改报告"],
    uploading: ["上传结果", "正在上传批改结果"],
    system_processing: ["系统处理中", "系统处理中"],
  };
  for (const [stage, [short, full]] of Object.entries(labels)) {
    assert.equal(progressLabel(stage), short, stage);
    assert.equal(progressLabel(stage, { full: true }), full, stage);
  }
  assert.equal(progressLabel("unknown"), "");
});

test("only actual grading and upload stages pulse", () => {
  for (const stage of [
    "preparing",
    "understanding",
    "rubric",
    "decomposing",
    "verifying",
    "scoring",
    "auditing",
    "reporting",
    "validating",
    "uploading",
  ]) {
    assert.equal(isPulsingProgress(stage), true, stage);
  }
  for (const stage of ["queued", "assigned", "system_processing", undefined]) {
    assert.equal(isPulsingProgress(stage), false, String(stage));
  }
});

test("delivered rounds are listed newest first", () => {
  const rounds = [
    { round_number: 1, delivered_at: "2026-08-01T00:00:00Z" },
    { round_number: 2, delivered_at: "2026-08-02T00:00:00Z" },
    { round_number: 3, delivered_at: null },
  ];
  assert.deepEqual(deliveredRounds(rounds).map(r => r.round_number), [2, 1]);
});

test("internal job vocabulary never reaches the user", () => {
  // The server exposes raw job states on rounds[].state. Showing
  // "worker_exception" would read like something the user must fix.
  assert.equal(roundStateLabel("worker_exception"), "系统处理中");
  assert.equal(roundStateLabel("uploading"), "生成结果中");
  assert.equal(roundStateLabel("running"), "批改中");
  assert.equal(roundStateLabel("succeeded"), "已完成");
  // An unrecognised state still renders as human text, never as a raw token.
  assert.equal(roundStateLabel("some_new_state"), "处理中");
  assert.equal(roundStateLabel(undefined), "处理中");
});

/* ---------------------------------------------------------------- pagination */

test("a list item renders even though the list omits eta and rounds", () => {
  // GET /api/v1/orders returns OrderSummaryView, which has no `eta` and no
  // `rounds` — those exist only on the detail response. The card must degrade
  // (hide the ETA) rather than throw on the missing fields.
  const listItem = {
    id: "o1",
    state: "v1_running",
    category: "grading",
    grading_standard: "imo",
    page_count: 2,
    paid_amount_cents: 2000,
    current_round_number: 1,
    created_at: "2026-08-10T00:00:00Z",
  };

  const view = decorateSummary(listItem);

  assert.equal(view.stateText, "批改中");
  assert.equal(view.etaText, "", "no eta on list items, so nothing is shown");
  assert.equal(view.amountText, "¥20.00");
});

test("summary decoration chooses short or full progress without changing state", () => {
  const order = {
    id: "o-progress",
    state: "v1_running",
    progress_stage: "verifying",
    grading_standard: "imo",
    page_count: 2,
    paid_amount_cents: 2000,
    created_at: "2026-08-10T00:00:00Z",
  };

  assert.equal(decorateSummary(order).stateText, "核验推理");
  assert.equal(
    decorateSummary(order, { fullProgress: true }).stateText,
    "正在核验关键推理",
  );
  assert.equal(decorateSummary(order).progressPulsing, true);
});
test("passes the category to the server and reports the end of the list", async () => {
  const urls = [];
  const service = createOrderService({
    api: {
      get: async url => {
        urls.push(url);
        return { items: [{ id: "o1" }], next_cursor: null };
      },
    },
  });

  const page = await service.list({ category: FILTERS.GRADING });

  assert.match(urls[0], /category=grading/);
  // A null cursor is the signal to stop paging.
  assert.equal(page.nextCursor, null);
  assert.equal(page.items.length, 1);
});

test("forwards an encoded cursor on later pages", async () => {
  const urls = [];
  const service = createOrderService({
    api: {
      get: async url => {
        urls.push(url);
        return { items: [], next_cursor: null };
      },
    },
  });

  await service.list({ cursor: "abc+def/==" });

  assert.match(urls[0], /cursor=abc%2Bdef%2F%3D%3D/);
});

test("batch progress deduplicates and caps visible order ids", async () => {
  const urls = [];
  const service = createOrderService({
    api: {
      get: async url => {
        urls.push(url);
        return { items: [] };
      },
    },
  });
  const ids = ["first", "first", ...Array.from({ length: 60 }, (_, i) => `o-${i}`)];

  await service.progress(ids);

  assert.match(urls[0], /^\/api\/v1\/orders\/progress\?/);
  assert.equal((urls[0].match(/order_ids=/g) || []).length, 50);
  assert.equal((urls[0].match(/order_ids=first/g) || []).length, 1);
});

/* ------------------------------------------------------------------ polling */

function fakeClock() {
  let nextId = 1;
  const timers = new Map();
  return {
    setTimer(fn, ms) {
      const id = nextId++;
      timers.set(id, { fn, ms });
      return id;
    },
    clearTimer(id) {
      timers.delete(id);
    },
    pending: () => timers.size,
    async fire() {
      const entries = [...timers.entries()];
      timers.clear();
      for (const [, timer] of entries) {
        await timer.fn();
      }
    },
  };
}

test("polling stops and clears its timer, not just a flag", async () => {
  const clock = fakeClock();
  const poller = createOrderPoller({
    fetchOrder: async () => ({ id: "o1", state: "v1_running" }),
    onUpdate: () => {},
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  poller.start();
  assert.equal(clock.pending(), 1);

  poller.stop();
  // The armed timer must actually be cleared; a lingering timer would keep
  // hitting the server after onHide/onUnload.
  assert.equal(clock.pending(), 0);
  assert.equal(poller.isRunning(), false);
  assert.equal(poller.hasPendingTimer(), false);
});

test("a stopped poller makes no further requests", async () => {
  const clock = fakeClock();
  let fetches = 0;
  const poller = createOrderPoller({
    fetchOrder: async () => {
      fetches += 1;
      return { id: "o1", state: "v1_running" };
    },
    onUpdate: () => {},
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  poller.start();
  await clock.fire();
  assert.equal(fetches, 1);

  poller.stop();
  await clock.fire();
  assert.equal(fetches, 1, "no request after stop");
});

test("polling ends on its own once the order is no longer active", async () => {
  const clock = fakeClock();
  const states = ["v1_running", "v1_delivered"];
  let index = 0;
  const seen = [];
  const poller = createOrderPoller({
    fetchOrder: async () => ({ id: "o1", state: states[index++] }),
    onUpdate: order => seen.push(order.state),
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  poller.start();
  await clock.fire();
  await clock.fire();

  assert.deepEqual(seen, ["v1_running", "v1_delivered"]);
  assert.equal(poller.isRunning(), false);
  assert.equal(clock.pending(), 0);
});

test("an update is not delivered after the page stopped the poller", async () => {
  const clock = fakeClock();
  let release;
  const updates = [];
  const poller = createOrderPoller({
    fetchOrder: () =>
      new Promise(resolve => {
        release = () => resolve({ id: "o1", state: "v1_running" });
      }),
    onUpdate: order => updates.push(order),
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  poller.start();
  const inFlight = clock.fire();
  // The page unloads while the request is still in flight.
  poller.stop();
  release();
  await inFlight;

  // setData on an unloaded page is a real crash source in the mini-program.
  assert.deepEqual(updates, []);
});

test("a polling error keeps polling instead of giving up", async () => {
  const clock = fakeClock();
  const errors = [];
  let calls = 0;
  const poller = createOrderPoller({
    fetchOrder: async () => {
      calls += 1;
      if (calls === 1) {
        throw new ApiError(0, "网络连接失败");
      }
      return { id: "o1", state: "v1_running" };
    },
    onUpdate: () => {},
    onError: error => errors.push(error),
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  poller.start();
  await clock.fire();
  assert.equal(errors.length, 1);
  assert.equal(clock.pending(), 1, "a transient failure must not end polling");

  await clock.fire();
  assert.equal(calls, 2);
  poller.stop();
});

test("list progress polling is non-overlapping and stops cleanly", async () => {
  const clock = fakeClock();
  let release;
  let fetches = 0;
  const updates = [];
  const poller = createOrderProgressPoller({
    fetchProgress: () => {
      fetches += 1;
      return new Promise(resolve => {
        release = () => resolve({ items: [{ id: "o1", progress_stage: "scoring" }] });
      });
    },
    onUpdate: update => updates.push(update),
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  poller.start();
  const inFlight = clock.fire();
  assert.equal(fetches, 1);
  assert.equal(clock.pending(), 0, "no second timer while a request is in flight");
  poller.stop();
  release();
  await inFlight;

  assert.deepEqual(updates, [], "an unloaded list must not receive an update");
  assert.equal(clock.pending(), 0);
  assert.equal(poller.isRunning(), false);
});

/* ---------------------------------------------------------------- downloads */

test("the download sends the session token and no persisted url", async () => {
  const calls = [];
  const service = createDownloadService({
    api: { get: async () => ({}) },
    baseUrl: "https://s.test",
    getToken: () => "tok",
    downloadFile: async options => {
      calls.push(options);
      return { statusCode: 200, tempFilePath: "wxfile://tmp/a.pdf" };
    },
    openDocument: async () => ({}),
  });

  const path = await service.openResultPdf({ orderId: "o1", roundNumber: 1 });

  assert.equal(calls[0].header.Authorization, "Bearer tok");
  assert.equal(
    calls[0].url,
    "https://s.test/api/v1/orders/o1/rounds/1/result/result_pdf",
  );
  // A temp path, valid for this session only — never stored.
  assert.equal(path, "wxfile://tmp/a.pdf");
});

test("a revoked download reports 410 and asks for a refresh", async () => {
  const service = createDownloadService({
    api: { get: async () => ({}) },
    baseUrl: "https://s.test",
    getToken: () => "tok",
    downloadFile: async () => ({ statusCode: 410 }),
    openDocument: async () => ({}),
  });

  const error = await service
    .openResultPdf({ orderId: "o1", roundNumber: 1 })
    .catch(caught => caught);

  assert.ok(error instanceof DownloadRefused);
  assert.equal(error.status, 410);
  assert.equal(error.shouldRefresh, true);
});

test("the document is not opened when the download was refused", async () => {
  let opened = 0;
  const service = createDownloadService({
    api: { get: async () => ({}) },
    baseUrl: "https://s.test",
    getToken: () => "tok",
    downloadFile: async () => ({ statusCode: 403 }),
    openDocument: async () => {
      opened += 1;
    },
  });

  await assert.rejects(() => service.openResultPdf({ orderId: "o1", roundNumber: 1 }));
  assert.equal(opened, 0);
});

test("reads the score summary from the delivered result json", async () => {
  const service = createDownloadService({
    api: {
      get: async path => {
        assert.equal(path, "/api/v1/orders/o1/rounds/1/result/result_json");
        return {
          title: "IMO 2026 第一题",
          total_score: 21,
          overall_summary: "整体思路正确。",
          problems: [{ label: "1", score: 7, max_score: 7 }],
        };
      },
    },
    baseUrl: "https://s.test",
    getToken: () => "tok",
    downloadFile: async () => ({}),
    openDocument: async () => ({}),
  });

  const summary = await service.fetchResultSummary({ orderId: "o1", roundNumber: 1 });

  assert.equal(summary.totalScore, 21);
  assert.equal(summary.problems[0].score, 7);
});

test("a revoked summary surfaces as a refusal, not as an empty page", async () => {
  const service = createDownloadService({
    api: {
      get: async () => {
        throw new ApiError(410, "该订单已退款，下载权限已被撤销。");
      },
    },
    baseUrl: "https://s.test",
    getToken: () => "tok",
    downloadFile: async () => ({}),
    openDocument: async () => ({}),
  });

  await assert.rejects(
    () => service.fetchResultSummary({ orderId: "o1", roundNumber: 1 }),
    error => error instanceof DownloadRefused && error.status === 410,
  );
});

test("an unreadable summary degrades instead of breaking the page", async () => {
  const service = createDownloadService({
    api: {
      get: async () => {
        throw new ApiError(404, "批改结果不存在。");
      },
    },
    baseUrl: "https://s.test",
    getToken: () => "tok",
    downloadFile: async () => ({}),
    openDocument: async () => ({}),
  });

  assert.equal(await service.fetchResultSummary({ orderId: "o1", roundNumber: 1 }), null);
});

test("the summary exposes only user-facing fields", () => {
  const summary = normalizeSummary({
    title: "t",
    total_score: 18,
    overall_summary: "s",
    problems: [{ label: "2", score: 4, max_score: 7 }],
    // Internal grader working data must not be surfaced even if present.
    internal: { proof_map: "secret" },
  });

  assert.equal("internal" in summary, false);
  assert.equal(summary.totalScore, 18);
});

test("a download without a session never reaches the network", async () => {
  let attempted = 0;
  const service = createDownloadService({
    api: { get: async () => ({}) },
    baseUrl: "https://s.test",
    getToken: () => null,
    downloadFile: async () => {
      attempted += 1;
      return { statusCode: 200 };
    },
    openDocument: async () => ({}),
  });

  await assert.rejects(() => service.openResultPdf({ orderId: "o1", roundNumber: 1 }));
  assert.equal(attempted, 0);
});
