/**
 * Result downloads.
 *
 * `wx.downloadFile` sends request headers, so the session token authenticates
 * the download exactly like any other API call. There is no download token to
 * mint, cache or persist, and no permanent URL is ever stored: the server
 * re-checks ownership and `downloads_revoked_at` on every request, so a refund
 * stops the download immediately.
 *
 * 401/403/410 all mean "ask the server again": the caller refreshes the order
 * and shows the server's own message.
 */

export class DownloadRefused extends Error {
  constructor(status, detail) {
    super(detail);
    this.name = "DownloadRefused";
    this.status = status;
    this.detail = detail;
    /** The order state may have changed; the caller should refresh it. */
    this.shouldRefresh = status === 401 || status === 403 || status === 410;
  }
}

const REFUSAL_DETAIL = {
  401: "登录状态已失效，请重新进入小程序。",
  403: "没有下载权限。",
  410: "该订单已退款，下载权限已被撤销。",
  404: "批改结果不存在。",
};

export function createDownloadService({
  api,
  baseUrl,
  getToken,
  downloadFile,
  openDocument,
}) {
  const trimmedBase = String(baseUrl || "").replace(/\/+$/, "");

  function resultPath(orderId, roundNumber, kind) {
    return `/api/v1/orders/${orderId}/rounds/${roundNumber}/result/${kind}`;
  }

  return {
    /**
     * Download the graded PDF and hand it to the system viewer.
     *
     * The temp file path is used immediately and never persisted: it is only
     * valid for this session, and caching it would outlive the permission that
     * produced it.
     */
    async openResultPdf({ orderId, roundNumber }) {
      const token = getToken();
      if (!token) {
        throw new DownloadRefused(401, REFUSAL_DETAIL[401]);
      }

      let result;
      try {
        result = await downloadFile({
          url: `${trimmedBase}${resultPath(orderId, roundNumber, "result_pdf")}`,
          header: { Authorization: `Bearer ${token}` },
        });
      } catch (error) {
        throw new DownloadRefused(0, "网络连接失败，请稍后重试。");
      }

      if (result.statusCode !== 200) {
        throw new DownloadRefused(
          result.statusCode,
          REFUSAL_DETAIL[result.statusCode] || "下载失败，请稍后重试。",
        );
      }

      await openDocument({ filePath: result.tempFilePath, fileType: "pdf" });
      return result.tempFilePath;
    },

    /**
     * Fetch the result JSON to render the score summary.
     *
     * The order detail endpoint does not carry the score, so the summary is
     * read from the delivered artefact instead of adding a server-side parse to
     * a 15-second polling path.
     */
    async fetchResultSummary({ orderId, roundNumber }) {
      try {
        const body = await api.get(resultPath(orderId, roundNumber, "result_json"));
        return normalizeSummary(body);
      } catch (error) {
        const status = error && error.status;
        if (status === 410 || status === 403 || status === 401) {
          throw new DownloadRefused(status, error.detail || REFUSAL_DETAIL[status]);
        }
        // A missing or unreadable summary must not break the detail page; the
        // download button is what actually matters.
        return null;
      }
    },
  };
}

/** Pick only the fields shown to the user. */
export function normalizeSummary(body) {
  if (!body || typeof body !== "object") {
    return null;
  }
  const problems = Array.isArray(body.problems) ? body.problems : [];
  const normalizedProblems = problems.map(problem => {
    const label = String(problem.label || problem.index || "");
    return {
      label,
      labelText: /^\d+$/.test(label) ? `第 ${label} 题` : label,
      score: typeof problem.score === "number" ? problem.score : null,
      maxScore: typeof problem.max_score === "number" ? problem.max_score : null,
    };
  });
  const hasCompleteMaximum = normalizedProblems.length > 0 && normalizedProblems.every(problem => problem.maxScore !== null);
  return {
    title: typeof body.title === "string" ? body.title : "",
    totalScore: typeof body.total_score === "number" ? body.total_score : null,
    maxScore: hasCompleteMaximum
      ? normalizedProblems.reduce((sum, problem) => sum + problem.maxScore, 0)
      : null,
    overallSummary: typeof body.overall_summary === "string" ? body.overall_summary : "",
    problems: normalizedProblems,
  };
}
