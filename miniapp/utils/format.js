/**
 * Display helpers.
 *
 * All money crossing the API is an integer number of *cents*; the server is the
 * only authority on amounts. These helpers format server-supplied values and
 * never compute a price, a total or a refund amount.
 */

export const GRADING_STANDARDS = Object.freeze([
  Object.freeze({
    value: "league_second_round",
    label: "全国高中数学联赛二试",
    hint: "加试三题或四题，按联赛二试评分标准批改",
  }),
  Object.freeze({
    value: "cmo",
    label: "中国数学奥林匹克 CMO",
    hint: "按CMO 评分细则批改",
  }),
  Object.freeze({
    value: "imo",
    label: "国际数学奥林匹克 IMO",
    hint: "按 IMO 评分细则批改",
  }),
]);

export const SERVICE_TIERS = Object.freeze([
  Object.freeze({
    value: "summary_report",
    label: "简明评分",
    hint: "总分、每题得分、主要问题与建议",
    delivery: "A4 评分报告",
  }),
  Object.freeze({
    value: "annotated_review",
    label: "逐页精批",
    hint: "在答卷关键位置标注，并逐页说明",
    delivery: "逐页批改报告",
  }),
]);

const STANDARD_LABELS = Object.freeze(
  GRADING_STANDARDS.reduce((all, item) => ({ ...all, [item.value]: item.label }), {}),
);

const SERVICE_TIER_LABELS = Object.freeze(
  SERVICE_TIERS.reduce((all, item) => ({ ...all, [item.value]: item.label }), {}),
);

/** Format an integer cent amount as ¥x.xx. */
export function formatCents(cents) {
  if (typeof cents !== "number" || Number.isNaN(cents)) {
    return "—";
  }
  return `¥${(cents / 100).toFixed(2)}`;
}

export function standardLabel(value) {
  return STANDARD_LABELS[value] || value || "—";
}

export function serviceTierLabel(value) {
  return SERVICE_TIER_LABELS[value] || value || "—";
}

export function serviceTierDelivery(value) {
  const match = SERVICE_TIERS.find(item => item.value === value);
  return match ? match.delivery : "批改报告";
}

export function formatBytes(bytes) {
  if (typeof bytes !== "number" || bytes <= 0) {
    return "";
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(0)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Render the server's ETA range as text.
 *
 * The server returns the window; the client displays it and never runs a local
 * countdown. A null eta means the server has nothing honest to say (no pending
 * work, or no Worker ready), so nothing is shown.
 */
export function formatEta(eta) {
  if (!eta) {
    return "";
  }
  const { earliest_minutes: earliest, latest_minutes: latest } = eta;
  if (typeof earliest !== "number" || typeof latest !== "number") {
    return "";
  }
  const render = minutes => {
    if (minutes < 60) {
      return `${minutes} 分钟`;
    }
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    return rest === 0 ? `${hours} 小时` : `${hours} 小时 ${rest} 分钟`;
  };
  if (earliest === latest) {
    return `预计还需 ${render(earliest)}`;
  }
  return `预计还需 ${render(earliest)} ~ ${render(latest)}`;
}

/** Remaining acceptance window, derived from the server's deadline. */
export function formatDeadline(deadline, now = Date.now()) {
  if (!deadline) {
    return "";
  }
  const remaining = new Date(deadline).getTime() - now;
  if (Number.isNaN(remaining)) {
    return "";
  }
  if (remaining <= 0) {
    return "已超过处理期限";
  }
  const hours = Math.floor(remaining / 3_600_000);
  if (hours >= 24) {
    return `剩余 ${Math.floor(hours / 24)} 天 ${hours % 24} 小时`;
  }
  if (hours >= 1) {
    return `剩余 ${hours} 小时`;
  }
  return `剩余 ${Math.max(1, Math.floor(remaining / 60_000))} 分钟`;
}

export function formatDateTime(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const pad = number => String(number).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}
