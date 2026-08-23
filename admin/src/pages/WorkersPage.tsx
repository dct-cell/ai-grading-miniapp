import { useCallback, useEffect, useState } from "react";

import { AdminApiError } from "../api/client";
import { useSession } from "../api/session";

interface WorkerRow {
  worker_id: string;
  device_name: string;
  platform: string;
  architecture: string;
  worker_version: string;
  codex_version: string | null;
  tex_version: string | null;
  status: string;
  current_job_id: string | null;
  last_heartbeat_at: string;
  active_job_state: string | null;
  lease_expires_at: string | null;
}

const CONTROLS = [
  { action: "drain", label: "停止接单" },
  { action: "disable", label: "停用" },
  { action: "enable", label: "恢复" },
] as const;

export function WorkersPage() {
  const { client } = useSession();
  const [items, setItems] = useState<WorkerRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const body = await client.get<{ items: WorkerRow[] }>("/workers");
      setItems(body.items);
    } catch (caught) {
      setError(caught instanceof AdminApiError ? caught.detail : "加载失败。");
    }
  }, [client]);

  useEffect(() => {
    void load();
  }, [load]);

  async function control(workerId: string, action: string) {
    setNotice(null);
    setError(null);
    try {
      await client.post(`/workers/${workerId}/${action}`, {});
      setNotice("已更新 Worker 状态。正在执行的任务不受影响。");
      await load();
    } catch (caught) {
      setError(caught instanceof AdminApiError ? caught.detail : "操作失败。");
    }
  }

  if (error !== null && items === null) return <p role="alert">{error}</p>;
  if (items === null) return <p role="status">加载中…</p>;

  return (
    <section>
      <h1>Worker</h1>
      <p>
        「停止接单」与「停用」<strong>都不会取消正在执行的任务</strong>：
        它们只阻止分配新任务，当前任务会继续跑完并交付。
      </p>
      {error !== null && <p role="alert">{error}</p>}
      {notice !== null && <p role="status">{notice}</p>}

      {items.length === 0 ? (
        <p>还没有 Worker 注册。</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>设备</th>
              <th>平台</th>
              <th>版本</th>
              <th>状态</th>
              <th>当前任务</th>
              <th>租约到期</th>
              <th>最近心跳</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((worker) => (
              <tr key={worker.worker_id}>
                <td>{worker.device_name}</td>
                <td>
                  {worker.platform} / {worker.architecture}
                </td>
                <td>
                  {worker.worker_version}
                  {worker.codex_version !== null && ` · codex ${worker.codex_version}`}
                  {worker.tex_version !== null && ` · tex ${worker.tex_version}`}
                </td>
                <td>{worker.status}</td>
                <td>
                  {worker.current_job_id === null
                    ? "—"
                    : `${worker.current_job_id.slice(0, 8)} (${worker.active_job_state})`}
                </td>
                <td>
                  {worker.lease_expires_at === null
                    ? "—"
                    : new Date(worker.lease_expires_at).toLocaleString("zh-CN")}
                </td>
                <td>{new Date(worker.last_heartbeat_at).toLocaleString("zh-CN")}</td>
                <td>
                  {CONTROLS.map((item) => (
                    <button
                      key={item.action}
                      type="button"
                      onClick={() => void control(worker.worker_id, item.action)}
                    >
                      {item.label}
                    </button>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
