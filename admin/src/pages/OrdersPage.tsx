import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { AdminApiError } from "../api/client";
import { useSession } from "../api/session";

/** Filters live in the URL so a view can be bookmarked and shared. */
interface OrderRow {
  id: string;
  state: string;
  owner_public_id: string;
  paid_amount_cents: number;
  page_count: number;
  current_round_number: number;
  created_at: string;
}

interface OrderList {
  items: OrderRow[];
  next_cursor: string | null;
}

const STATES = [
  "",
  "awaiting_payment",
  "v1_queued",
  "v1_running",
  "v1_delivered",
  "v2_queued",
  "v2_running",
  "v2_delivered",
  "refund_pending",
  "refunded",
  "accepted",
] as const;

export function yuan(cents: number): string {
  return `¥${(cents / 100).toFixed(2)}`;
}

export function OrdersPage() {
  const { client } = useSession();
  const [params, setParams] = useSearchParams();
  const [page, setPage] = useState<OrderList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const query = params.get("query") ?? "";
  const state = params.get("state") ?? "";

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setPage(await client.get<OrderList>("/orders", { query, state }));
    } catch (caught) {
      setError(caught instanceof AdminApiError ? caught.detail : "加载失败。");
    } finally {
      setLoading(false);
    }
  }, [client, query, state]);

  useEffect(() => {
    void load();
  }, [load]);

  function updateFilter(name: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(name, value);
    else next.delete(name);
    setParams(next);
  }

  return (
    <section>
      <h1>订单</h1>
      <form
        className="filters"
        onSubmit={(event) => {
          event.preventDefault();
          void load();
        }}
      >
        <label htmlFor="query">精确查找</label>
        <input
          id="query"
          value={query}
          placeholder="订单号 / 用户公开 ID / 支付交易号"
          onChange={(event) => updateFilter("query", event.target.value)}
        />
        <label htmlFor="state">状态</label>
        <select
          id="state"
          value={state}
          onChange={(event) => updateFilter("state", event.target.value)}
        >
          {STATES.map((option) => (
            <option key={option} value={option}>
              {option === "" ? "全部" : option}
            </option>
          ))}
        </select>
      </form>

      {loading && <p role="status">加载中…</p>}
      {error !== null && <p role="alert">{error}</p>}
      {!loading && error === null && page !== null && page.items.length === 0 && (
        <p>没有符合条件的订单。</p>
      )}
      {!loading && error === null && page !== null && page.items.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>订单</th>
              <th>用户</th>
              <th>状态</th>
              <th>页数</th>
              <th>金额</th>
              <th>创建时间</th>
            </tr>
          </thead>
          <tbody>
            {page.items.map((row) => (
              <tr key={row.id}>
                <td>
                  <Link to={`/orders/${row.id}`}>{row.id.slice(0, 8)}</Link>
                </td>
                <td>{row.owner_public_id}</td>
                <td>{row.state}</td>
                <td>{row.page_count}</td>
                <td>{yuan(row.paid_amount_cents)}</td>
                <td>{new Date(row.created_at).toLocaleString("zh-CN")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
