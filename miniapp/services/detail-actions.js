import { ACTION_LABELS } from "./aftersales.js";
import { FILTERS, filterForState } from "../utils/order-states.js";

export const DETAIL_ACTION_KINDS = Object.freeze({
  CREATE: "create",
  DOWNLOAD: "download",
  HOME: "home",
  ORDER_ACTIONS: "order_actions",
});

/**
 * Resolve the sticky footer from server-owned order state and actions.
 *
 * While a job is running, wayfinding takes priority: the user can start another
 * submission or leave for home. Server-provided aftersales actions are not
 * promoted during this state. Once a report is delivered, the existing report
 * and order-action affordances take over again.
 */
export function resolveDetailActions({ state, actions = [], newestRound = null, serviceTier = "annotated_review" } = {}) {
  const grading = filterForState(state) === FILTERS.GRADING;

  if (grading) {
    return {
      primaryLabel: "继续提交",
      primaryKind: DETAIL_ACTION_KINDS.CREATE,
      secondaryLabel: "返回首页",
      secondaryKind: DETAIL_ACTION_KINDS.HOME,
      roundNumber: 0,
      showHistoryDownloads: Boolean(newestRound),
    };
  }

  if (newestRound) {
    return {
      primaryLabel: serviceTier === "summary_report" ? "打开评分报告" : "打开批改报告",
      primaryKind: DETAIL_ACTION_KINDS.DOWNLOAD,
      secondaryLabel: actions.length > 0 ? "订单操作" : "",
      secondaryKind: actions.length > 0 ? DETAIL_ACTION_KINDS.ORDER_ACTIONS : "",
      roundNumber: newestRound.round_number,
      showHistoryDownloads: false,
    };
  }

  const action = actions[0] || "";
  return {
    primaryLabel: action ? ACTION_LABELS[action] : "",
    primaryKind: action,
    secondaryLabel: "",
    secondaryKind: "",
    roundNumber: 0,
    showHistoryDownloads: false,
  };
}
