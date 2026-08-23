import { useCallback, useEffect, useState } from "react";

import { AdminApiError } from "../api/client";
import { useSession } from "../api/session";

interface AuditEntry {
  id: string;
  actor_type: string;
  actor_id: string;
  action: string;
  target_type: string;
  target_id: string;
  details: Record<string, unknown>;
  created_at: string;
}

export function AuditPage() {
  const { client } = useSession();
  const [filters, setFilters] = useState({
    actor_id: "",
    action: "",
    target_type: "",
  });
  const [items, setItems] = useState<AuditEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const body = await client.get<{ items: AuditEntry[] }>("/audit", filters);
      setItems(body.items);
    } catch (caught) {
      setError(caught instanceof AdminApiError ? caught.detail : "加载失败。");
    }
  }, [client, filters]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error !== null && items === null) return <p role="alert">{error}</p>;
  if (items === null) return <p role="status">加载中…</p>;

  return (
    <section>
      <h1>审计</h1>
      {/* Append-only by design: no edit or delete control exists here, because
          the log is the evidence that an action happened. */}
      <p>
        审计记录<strong>只增不改不删</strong>，管理台没有编辑或删除入口。
      </p>
      {error !== null && <p role="alert">{error}</p>}

      <form
        className="filters"
        onSubmit={(event) => {
          event.preventDefault();
          void load();
        }}
      >
        <label htmlFor="action">动作</label>
        <input
          id="action"
          value={filters.action}
          onChange={(event) => setFilters({ ...filters, action: event.target.value })}
        />
        <label htmlFor="target-type">目标类型</label>
        <input
          id="target-type"
          value={filters.target_type}
          onChange={(event) =>
            setFilters({ ...filters, target_type: event.target.value })
          }
        />
        <label htmlFor="actor">操作者 ID</label>
        <input
          id="actor"
          value={filters.actor_id}
          onChange={(event) =>
            setFilters({ ...filters, actor_id: event.target.value })
          }
        />
      </form>

      {items.length === 0 ? (
        <p>没有符合条件的审计记录。</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>操作者</th>
              <th>动作</th>
              <th>目标</th>
              <th>详情</th>
            </tr>
          </thead>
          <tbody>
            {items.map((entry) => (
              <tr key={entry.id}>
                <td>{new Date(entry.created_at).toLocaleString("zh-CN")}</td>
                <td>
                  {entry.actor_type}:{entry.actor_id.slice(0, 8)}
                </td>
                <td>{entry.action}</td>
                <td>
                  {entry.target_type}:{entry.target_id.slice(0, 8)}
                </td>
                <td>{JSON.stringify(entry.details)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
