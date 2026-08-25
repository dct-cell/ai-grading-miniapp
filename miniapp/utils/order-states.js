/**
 * Order state vocabulary, mirroring the server's enum.
 *
 * This module maps server states to Chinese labels and to the three list
 * filters. It is a *presentation* mapping only: the server decides the state
 * and decides which actions are allowed. Nothing here computes eligibility.
 */

export const ORDER_STATES = Object.freeze({
  AWAITING_PAYMENT: "awaiting_payment",
  V1_QUEUED: "v1_queued",
  V1_RUNNING: "v1_running",
  V1_DELIVERED: "v1_delivered",
  V2_QUEUED: "v2_queued",
  V2_RUNNING: "v2_running",
  V2_DELIVERED: "v2_delivered",
  REFUND_PENDING: "refund_pending",
  REFUNDED: "refunded",
  ACCEPTED: "accepted",
});

/** Server filter values accepted by GET /api/v1/orders?category= */
export const FILTERS = Object.freeze({
  ALL: "all",
  GRADING: "grading",
  ACCEPTANCE: "acceptance",
});

const GRADING_STATES = Object.freeze([
  ORDER_STATES.V1_QUEUED,
  ORDER_STATES.V1_RUNNING,
  ORDER_STATES.V2_QUEUED,
  ORDER_STATES.V2_RUNNING,
]);

const ACCEPTANCE_STATES = Object.freeze([
  ORDER_STATES.V1_DELIVERED,
  ORDER_STATES.V2_DELIVERED,
  ORDER_STATES.REFUND_PENDING,
]);

const STATE_LABELS = Object.freeze({
  [ORDER_STATES.AWAITING_PAYMENT]: "待支付",
  [ORDER_STATES.V1_QUEUED]: "排队中",
  [ORDER_STATES.V1_RUNNING]: "批改中",
  [ORDER_STATES.V1_DELIVERED]: "待验收",
  [ORDER_STATES.V2_QUEUED]: "复核排队中",
  [ORDER_STATES.V2_RUNNING]: "复核批改中",
  [ORDER_STATES.V2_DELIVERED]: "复核待验收",
  [ORDER_STATES.REFUND_PENDING]: "退款处理中",
  [ORDER_STATES.REFUNDED]: "已退款",
  [ORDER_STATES.ACCEPTED]: "已完成",
});

const STATE_TONES = Object.freeze({
  [ORDER_STATES.AWAITING_PAYMENT]: "warn",
  [ORDER_STATES.V1_QUEUED]: "busy",
  [ORDER_STATES.V1_RUNNING]: "busy",
  [ORDER_STATES.V2_QUEUED]: "busy",
  [ORDER_STATES.V2_RUNNING]: "busy",
  [ORDER_STATES.V1_DELIVERED]: "ready",
  [ORDER_STATES.V2_DELIVERED]: "ready",
  [ORDER_STATES.REFUND_PENDING]: "warn",
  [ORDER_STATES.REFUNDED]: "done",
  [ORDER_STATES.ACCEPTED]: "done",
});

/**
 * Which list filter an order state belongs to.
 *
 * Mirrors server/services/orders.py category_of, so the tab counts agree with
 * what `?category=` returns.
 */
export function filterForState(state) {
  if (GRADING_STATES.includes(state)) {
    return FILTERS.GRADING;
  }
  if (ACCEPTANCE_STATES.includes(state)) {
    return FILTERS.ACCEPTANCE;
  }
  return FILTERS.ALL;
}

export function stateLabel(state) {
  return STATE_LABELS[state] || state || "—";
}

export function stateTone(state) {
  return STATE_TONES[state] || "muted";
}

/** True when the order still has work that a Worker will progress. */
export function isActive(state) {
  return GRADING_STATES.includes(state) || state === ORDER_STATES.AWAITING_PAYMENT;
}

export function isGradingState(state) {
  return GRADING_STATES.includes(state);
}

const PROGRESS_LABELS = Object.freeze({
  queued: Object.freeze({ short: "排队中", full: "排队中" }),
  assigned: Object.freeze({ short: "准备批改", full: "正在准备批改" }),
  preparing: Object.freeze({ short: "读取答卷", full: "正在读取答卷" }),
  understanding: Object.freeze({ short: "理解题目", full: "正在理解题目与作答" }),
  rubric: Object.freeze({ short: "整理评分点", full: "正在整理评分要点" }),
  decomposing: Object.freeze({ short: "梳理解答", full: "正在梳理解答步骤" }),
  verifying: Object.freeze({ short: "核验推理", full: "正在核验关键推理" }),
  scoring: Object.freeze({ short: "计算得分", full: "正在计算得分" }),
  auditing: Object.freeze({ short: "复核判分", full: "正在复核判分" }),
  reporting: Object.freeze({ short: "生成报告", full: "正在生成批改报告" }),
  validating: Object.freeze({ short: "检查报告", full: "正在检查批改报告" }),
  uploading: Object.freeze({ short: "上传结果", full: "正在上传批改结果" }),
  system_processing: Object.freeze({ short: "系统处理中", full: "系统处理中" }),
});

const PULSING_PROGRESS_STAGES = Object.freeze([
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
]);

export function progressLabel(stage, { full = false } = {}) {
  const labels = PROGRESS_LABELS[stage];
  return labels ? labels[full ? "full" : "short"] : "";
}

export function isPulsingProgress(stage) {
  return PULSING_PROGRESS_STAGES.includes(stage);
}

/**
 * A round whose job hit worker_exception is shown as "系统处理中".
 *
 * `worker_exception` is a *job* state, not an order state: the order stays in
 * v1_running/v2_running while staff investigate. Surfacing the raw job state
 * would suggest the user must act, which they must not.
 */
export function hasSystemException(rounds) {
  return Array.isArray(rounds) && rounds.some(round => round.state === "worker_exception");
}

/**
 * The label for a card, accounting for a stalled job.
 *
 * Called with the order's rounds so a grading order whose job failed reads
 * "系统处理中" instead of a plain "批改中".
 */
export function displayLabel(state, rounds, progressStage, { full = false } = {}) {
  const progress = progressLabel(progressStage, { full });
  if (progress) {
    return progress;
  }
  if (filterForState(state) === FILTERS.GRADING && hasSystemException(rounds)) {
    return "系统处理中";
  }
  return stateLabel(state);
}

/** The delivered rounds a user may download, newest first. */
export function deliveredRounds(rounds) {
  if (!Array.isArray(rounds)) {
    return [];
  }
  return rounds
    .filter(round => Boolean(round.delivered_at))
    .slice()
    .sort((left, right) => right.round_number - left.round_number);
}

/**
 * A user-facing label for one round's job state.
 *
 * The server exposes raw job states here (`queued`, `leased`, `running`,
 * `uploading`, `succeeded`, `worker_exception`, `delivered`). Rendering those
 * verbatim would put internal vocabulary in front of users, and
 * `worker_exception` in particular would look like something they must fix.
 */
const ROUND_STATE_LABELS = Object.freeze({
  queued: "排队中",
  leased: "已分配",
  running: "批改中",
  uploading: "生成结果中",
  succeeded: "已完成",
  delivered: "已完成",
  worker_exception: "系统处理中",
  cancelled: "已取消",
});

export function roundStateLabel(state) {
  return ROUND_STATE_LABELS[state] || "处理中";
}
