import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { AdminApiError } from "../api/client";
import { useSession } from "../api/session";
import { yuan } from "./OrdersPage";

interface AftersalesRow {
  refund_id: string;
  order_id: string;
  owner_public_id: string;
  state: string;
  source: string;
  amount_cents: number;
  order_state: string;
  created_at: string;
}

const STATES = ["", "pending", "refunded", "refund_failed", "rejected"] as const;

export function AftersalesPage() {
  const { client } = useSession();
  const [state, setState] = useState("");
  const [items, setItems] = useState<AftersalesRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [rejecting, setRejecting] = useState<string | null>(null);
  const [reason, setReason] = useState("");

  const load = useCallback(async () => {
    setError(null);
    try {
      const body = await client.get<{ items: AftersalesRow[] }>("/aftersales", {
        state,
      });
      setItems(body.items);
    } catch (caught) {
      setError(caught instanceof AdminApiError ? caught.detail : "加载失败。");
    }
  }, [client, state]);

  useEffect(() => {
    void load();
  }, [load]);

  async function approve(refundId: string) {
    setNotice(null);
    setError(null);
    try {
      await client.post(`/refunds/${refundId}/approve`, {});
      setNotice("退款已提交，金额与收款方由服务端从订单推导。");
      await load();
    } catch (caught) {
      setError(caught instanceof AdminApiError ? caught.detail : "操作失败。");
    }
  }

  async function reject(refundId: string) {
    setNotice(null);
    setError(null);
    try {
      await client.post(`/refunds/${refundId}/reject`, { reason });
      setNotice("已驳回，用户保留下载权。");
      setRejecting(null);
      setReason("");
      await load();
    } catch (caught) {
      setError(caught instanceof AdminApiError ? caught.detail : "操作失败。");
    }
  }

  if (error !== null && items === null) return <p role="alert">{error}</p>;
  if (items === null) return <p role="status">加载中…</p>;

  return (
    <section>
      <h1>售后</h1>
      <p>
        这里只处理<strong>用户提出的退款</strong>。
        「我们的原因导致失败」应在订单详情页发起技术性退款——它不计入用户指标。
      </p>
      {error !== null && <p role="alert">{error}</p>}
      {notice !== null && <p role="status">{notice}</p>}

      <div className="filters">
        <label htmlFor="state">状态</label>
        <select
          id="state"
          value={state}
          onChange={(event) => setState(event.target.value)}
        >
          {STATES.map((option) => (
            <option key={option} value={option}>
              {option === "" ? "全部" : option}
            </option>
          ))}
        </select>
      </div>

      {items.length === 0 ? (
        <p>没有待处理的售后。</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>订单</th>
              <th>用户</th>
              <th>来源</th>
              <th>金额</th>
              <th>退款状态</th>
              <th>订单状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <tr key={row.refund_id}>
                <td>
                  <Link to={`/orders/${row.order_id}`}>
                    {row.order_id.slice(0, 8)}
                  </Link>
                </td>
                <td>{row.owner_public_id}</td>
                <td>{row.source === "user" ? "用户申请" : "技术性"}</td>
                <td>{yuan(row.amount_cents)}</td>
                <td>{row.state}</td>
                <td>{row.order_state}</td>
                <td>
                  {row.state === "pending" && (
                    <>
                      <button
                        type="button"
                        onClick={() => void approve(row.refund_id)}
                      >
                        批准退款
                      </button>
                      <button
                        type="button"
                        onClick={() => setRejecting(row.refund_id)}
                      >
                        驳回…
                      </button>
                    </>
                  )}
                  {row.state === "refund_failed" && (
                    <button type="button" onClick={() => void approve(row.refund_id)}>
                      重试退款
                    </button>
                  )}
                  {rejecting === row.refund_id && (
                    <span>
                      <label htmlFor={`reason-${row.refund_id}`}>驳回原因</label>
                      <input
                        id={`reason-${row.refund_id}`}
                        value={reason}
                        onChange={(event) => setReason(event.target.value)}
                      />
                      <button
                        type="button"
                        disabled={reason.trim() === ""}
                        onClick={() => void reject(row.refund_id)}
                      >
                        确认驳回
                      </button>
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
