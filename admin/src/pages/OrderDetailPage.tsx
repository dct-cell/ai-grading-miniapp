import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { AdminApiError } from "../api/client";
import { useSession } from "../api/session";
import { yuan } from "./OrdersPage";

interface OrderDetail {
  id: string;
  state: string;
  owner_public_id: string;
  paid_amount_cents: number;
  page_count: number;
  grading_standard: string;
  note: string;
  current_round_number: number;
  acceptance_deadline: string | null;
  downloads_revoked_at: string | null;
  created_at: string;
  payment: {
    id: string;
    state: string;
    amount_cents: number;
    external_transaction_id: string | null;
  } | null;
  refunds: Array<{
    id: string;
    state: string;
    source: string;
    amount_cents: number;
    created_at: string;
  }>;
  rounds: Array<{
    round_number: number;
    delivered_at: string | null;
    has_result_pdf: boolean;
    has_result_json: boolean;
    job: {
      id: string;
      state: string;
      worker_id: string | null;
      attempt_count: number;
      lease_version: number;
      lease_expires_at: string | null;
    } | null;
  }>;
  files: Array<{ kind: string; size_bytes: number }>;
  timeline: Array<{ event: string; at: string }>;
  available_admin_actions: string[];
}

export function OrderDetailPage() {
  const { orderId } = useParams<{ orderId: string }>();
  const { client } = useSession();
  const [detail, setDetail] = useState<OrderDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      setDetail(await client.get<OrderDetail>(`/orders/${orderId}`));
    } catch (caught) {
      setError(caught instanceof AdminApiError ? caught.detail : "加载失败。");
    }
  }, [client, orderId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function issueTechnicalRefund() {
    setNotice(null);
    setError(null);
    try {
      await client.post("/refunds/technical", { order_id: orderId, reason });
      setNotice("技术性退款已提交。");
      setConfirming(false);
      setReason("");
      await load();
    } catch (caught) {
      setError(caught instanceof AdminApiError ? caught.detail : "操作失败。");
    }
  }

  if (error !== null && detail === null) return <p role="alert">{error}</p>;
  if (detail === null) return <p role="status">加载中…</p>;

  return (
    <section>
      <h1>订单详情</h1>
      {error !== null && <p role="alert">{error}</p>}
      {notice !== null && <p role="status">{notice}</p>}

      <dl className="detail">
        <dt>订单号</dt>
        <dd>{detail.id}</dd>
        <dt>状态</dt>
        <dd>{detail.state}</dd>
        <dt>用户</dt>
        <dd>{detail.owner_public_id}</dd>
        <dt>已付金额</dt>
        <dd>{yuan(detail.paid_amount_cents)}</dd>
        <dt>页数</dt>
        <dd>{detail.page_count}</dd>
        <dt>下载权</dt>
        <dd>{detail.downloads_revoked_at === null ? "有效" : "已撤销"}</dd>
      </dl>

      <h2>轮次</h2>
      <table>
        <thead>
          <tr>
            <th>轮次</th>
            <th>任务状态</th>
            <th>Worker</th>
            <th>尝试次数</th>
            <th>交付时间</th>
            <th>产物</th>
          </tr>
        </thead>
        <tbody>
          {detail.rounds.map((round) => (
            <tr key={round.round_number}>
              <td>V{round.round_number}</td>
              <td>{round.job?.state ?? "—"}</td>
              <td>{round.job?.worker_id ?? "—"}</td>
              <td>{round.job?.attempt_count ?? 0}</td>
              <td>
                {round.delivered_at === null
                  ? "—"
                  : new Date(round.delivered_at).toLocaleString("zh-CN")}
              </td>
              <td>
                {round.has_result_pdf ? "PDF " : ""}
                {round.has_result_json ? "JSON" : ""}
                {!round.has_result_pdf && !round.has_result_json ? "—" : ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2>附件</h2>
      <ul>
        {detail.files.map((file) => (
          <li key={file.kind}>
            {file.kind}（{Math.round(file.size_bytes / 1024)} KB）
          </li>
        ))}
      </ul>

      <h2>时间线</h2>
      <ol>
        {detail.timeline.map((event, index) => (
          <li key={`${event.event}-${index}`}>
            {new Date(event.at).toLocaleString("zh-CN")} · {event.event}
          </li>
        ))}
      </ol>

      <h2>退款记录</h2>
      {detail.refunds.length === 0 ? (
        <p>暂无退款。</p>
      ) : (
        <ul>
          {detail.refunds.map((refund) => (
            <li key={refund.id}>
              {refund.state} · {refund.source} · {yuan(refund.amount_cents)}
            </li>
          ))}
        </ul>
      )}

      {detail.available_admin_actions.includes("technical_refund") && (
        <section className="danger-zone">
          <h2>技术性退款</h2>
          <p>
            用于「我们的原因导致批改失败」。全额退回原支付渠道，
            <strong>不计入该用户的退款次数与占比</strong>，并立即撤销下载权。
          </p>
          {!confirming ? (
            <button type="button" onClick={() => setConfirming(true)}>
              发起技术性退款…
            </button>
          ) : (
            <div>
              <label htmlFor="reason">原因（必填，会写入审计）</label>
              <input
                id="reason"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
              {/* Explicit confirmation: this moves real money. */}
              <button
                type="button"
                disabled={reason.trim() === ""}
                onClick={() => void issueTechnicalRefund()}
              >
                确认退款 {yuan(detail.paid_amount_cents)}
              </button>
              <button type="button" onClick={() => setConfirming(false)}>
                取消
              </button>
            </div>
          )}
        </section>
      )}
    </section>
  );
}
