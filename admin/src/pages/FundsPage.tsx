import { useEffect, useState } from "react";

import { AdminApiError } from "../api/client";
import { useSession } from "../api/session";
import { yuan } from "./OrdersPage";

interface Funds {
  payments: { succeeded_cents: number; succeeded_count: number };
  refunds: {
    refunded_cents: number;
    technical_refunded_cents: number;
    failed_count: number;
    pending_count: number;
  };
  reconciliation: {
    source: string;
    settled_to_bank_cents: number | null;
  };
}

export function FundsPage() {
  const { client } = useSession();
  const [funds, setFunds] = useState<Funds | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const body = await client.get<Funds>("/funds");
        if (!cancelled) setFunds(body);
      } catch (caught) {
        if (!cancelled) {
          setError(caught instanceof AdminApiError ? caught.detail : "加载失败。");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client]);

  if (error !== null) return <p role="alert">{error}</p>;
  if (funds === null) return <p role="status">加载中…</p>;

  return (
    <section>
      <h1>资金</h1>
      <h2>收款</h2>
      <dl className="detail">
        <dt>成功收款</dt>
        <dd>
          {yuan(funds.payments.succeeded_cents)}（{funds.payments.succeeded_count} 笔）
        </dd>
      </dl>

      <h2>退款</h2>
      <dl className="detail">
        <dt>已退金额</dt>
        <dd>{yuan(funds.refunds.refunded_cents)}</dd>
        <dt>其中技术性</dt>
        <dd>{yuan(funds.refunds.technical_refunded_cents)}</dd>
        <dt>待处理</dt>
        <dd>{funds.refunds.pending_count}</dd>
        <dt>退款失败</dt>
        <dd>{funds.refunds.failed_count}</dd>
      </dl>

      <h2>对账</h2>
      {funds.reconciliation.settled_to_bank_cents === null ? (
        <p>
          <strong>尚未接入银行对账单</strong>，因此这里
          <strong>不声称任何款项已到账</strong>。
          上面的数字只表示我们向支付渠道发起并被确认的金额。
          真实结算对账是 Phase 09。
        </p>
      ) : (
        <p>
          已结算至银行：{yuan(funds.reconciliation.settled_to_bank_cents)}
          （来源：{funds.reconciliation.source}）
        </p>
      )}
    </section>
  );
}
