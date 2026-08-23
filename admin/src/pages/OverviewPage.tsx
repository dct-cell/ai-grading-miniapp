import { useEffect, useState } from "react";

import { AdminApiError } from "../api/client";
import { useSession } from "../api/session";

interface Overview {
  orders: Record<string, number>;
  jobs: Record<string, number>;
  workers: Record<string, number>;
  refunds: Record<string, number>;
  storage: {
    used_percent: number | null;
    latest_backup_age_seconds: number | null;
  };
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="metric">
      <span className="metric__label">{label}</span>
      <strong className="metric__value">{value}</strong>
    </div>
  );
}

export function OverviewPage() {
  const { client } = useSession();
  const [overview, setOverview] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const body = await client.get<Overview>("/overview");
        if (!cancelled) setOverview(body);
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
  if (overview === null) return <p role="status">加载中…</p>;

  const queued = overview.jobs.queued ?? 0;
  const running = overview.jobs.running ?? 0;
  const failed = overview.jobs.worker_exception ?? 0;

  return (
    <section>
      <h1>总览</h1>

      <h2>批改队列</h2>
      <div className="metrics">
        <Metric label="排队中" value={queued} />
        <Metric label="批改中" value={running} />
        <Metric label="Worker 异常" value={failed} />
      </div>

      <h2>待验收</h2>
      <div className="metrics">
        <Metric label="V1 已交付" value={overview.orders.v1_delivered ?? 0} />
        <Metric label="V2 已交付" value={overview.orders.v2_delivered ?? 0} />
      </div>

      <h2>退款</h2>
      <div className="metrics">
        <Metric label="待人工审批" value={overview.refunds.pending_manual ?? 0} />
        <Metric label="退款失败" value={overview.refunds.failed ?? 0} />
      </div>

      <h2>Worker</h2>
      <div className="metrics">
        <Metric label="在线" value={overview.workers.online ?? 0} />
        <Metric label="疑似离线" value={overview.workers.suspected_offline ?? 0} />
        {/* Drained Workers still exist and still hold their current job, so
            omitting them here would read as lost capacity. */}
        <Metric label="停止接单" value={overview.workers.draining ?? 0} />
        <Metric label="已停用" value={overview.workers.disabled ?? 0} />
      </div>

      <h2>存储</h2>
      <div className="metrics">
        <Metric
          label="磁盘占用"
          value={
            overview.storage.used_percent === null
              ? "未知"
              : `${overview.storage.used_percent}%`
          }
        />
        <Metric
          label="最近备份"
          value={
            overview.storage.latest_backup_age_seconds === null
              ? "尚未启用（Phase 09）"
              : `${overview.storage.latest_backup_age_seconds} 秒前`
          }
        />
      </div>
    </section>
  );
}
