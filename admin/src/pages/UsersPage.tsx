import { useState } from "react";

import { AdminApiError } from "../api/client";
import { useSession } from "../api/session";
import { yuan } from "./OrdersPage";

interface UserDetail {
  public_id: string;
  created_at: string;
  order_count: number;
  lifetime_paid_cents: number;
  lifetime_user_refunded_cents: number;
  technical_refunded_cents: number;
  monthly_user_refund_count: number;
  lifetime_refund_ratio: number;
}

export function UsersPage() {
  const { client } = useSession();
  const [publicId, setPublicId] = useState("");
  const [detail, setDetail] = useState<UserDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function lookup() {
    setError(null);
    setDetail(null);
    try {
      setDetail(await client.get<UserDetail>(`/users/${publicId.trim()}`));
    } catch (caught) {
      setError(caught instanceof AdminApiError ? caught.detail : "查询失败。");
    }
  }

  return (
    <section>
      <h1>用户</h1>
      <p>按公开 ID 查询。用户的微信 openid 不在管理台展示。</p>
      <form
        className="filters"
        onSubmit={(event) => {
          event.preventDefault();
          void lookup();
        }}
      >
        <label htmlFor="public-id">公开 ID</label>
        <input
          id="public-id"
          value={publicId}
          placeholder="u-xxxxxxxx"
          onChange={(event) => setPublicId(event.target.value)}
        />
        <button type="submit" disabled={publicId.trim() === ""}>
          查询
        </button>
      </form>

      {error !== null && <p role="alert">{error}</p>}
      {detail !== null && (
        <dl className="detail">
          <dt>公开 ID</dt>
          <dd>{detail.public_id}</dd>
          <dt>注册时间</dt>
          <dd>{new Date(detail.created_at).toLocaleString("zh-CN")}</dd>
          <dt>订单数</dt>
          <dd>{detail.order_count}</dd>
          <dt>累计支付</dt>
          <dd>{yuan(detail.lifetime_paid_cents)}</dd>
          <dt>用户退款</dt>
          <dd>{yuan(detail.lifetime_user_refunded_cents)}</dd>
          <dt>技术性退款</dt>
          <dd>
            {yuan(detail.technical_refunded_cents)}
            <small>（我们的原因，不计入该用户指标）</small>
          </dd>
          <dt>本月退款次数</dt>
          <dd>{detail.monthly_user_refund_count}</dd>
          <dt>累计退款占比</dt>
          <dd>{(detail.lifetime_refund_ratio * 100).toFixed(2)}%</dd>
        </dl>
      )}
    </section>
  );
}
