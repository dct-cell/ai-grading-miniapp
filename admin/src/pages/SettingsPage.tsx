import { useCallback, useEffect, useState } from "react";

import { AdminApiError } from "../api/client";
import { useSession } from "../api/session";
import { yuan } from "./OrdersPage";

interface SettingsBody {
  summary_cents_per_page: number;
  annotated_cents_per_page: number;
  max_pdf_pages: number;
  max_pdf_bytes: number;
  quote_ttl_seconds: number;
  acceptance_ttl_seconds: number;
  minutes_per_page: number;
  automatic_refund_max_amount_cents: number;
  automatic_refund_max_monthly_count: number;
}

/** Editable operational knobs. The price is versioned separately below. */
const FIELDS: Array<{ name: keyof SettingsBody; label: string; hint?: string }> = [
  { name: "max_pdf_pages", label: "PDF 页数上限" },
  { name: "max_pdf_bytes", label: "PDF 字节上限" },
  { name: "quote_ttl_seconds", label: "报价有效期（秒）" },
  { name: "acceptance_ttl_seconds", label: "验收期限（秒）", hint: "只影响此后交付的订单" },
  { name: "minutes_per_page", label: "每页预估分钟" },
  { name: "automatic_refund_max_amount_cents", label: "自动退款金额上限（分）" },
  { name: "automatic_refund_max_monthly_count", label: "自动退款月次数上限" },
];

export function SettingsPage() {
  const { client } = useSession();
  const [settings, setSettings] = useState<SettingsBody | null>(null);
  const [draft, setDraft] = useState<Partial<Record<string, string>>>({});
  const [prices, setPrices] = useState({ summary_report: "", annotated_review: "" });
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setSettings(await client.get<SettingsBody>("/settings"));
    } catch (caught) {
      setError(caught instanceof AdminApiError ? caught.detail : "加载失败。");
    }
  }, [client]);

  useEffect(() => {
    void load();
  }, [load]);

  async function save() {
    setNotice(null);
    setError(null);
    const changes: Record<string, number> = {};
    for (const [name, value] of Object.entries(draft)) {
      if (value !== undefined && value !== "") changes[name] = Number(value);
    }
    try {
      setSettings(await client.patch<SettingsBody>("/settings", changes));
      setDraft({});
      setNotice("配置已更新。已有报价与已交付订单保留原有快照。");
    } catch (caught) {
      setError(caught instanceof AdminApiError ? caught.detail : "保存失败。");
    }
  }

  async function publishPrice(serviceTier: "summary_report" | "annotated_review") {
    setNotice(null);
    setError(null);
    try {
      await client.post("/settings/price-rules", {
        service_tier: serviceTier,
        cents_per_page: Number(prices[serviceTier]),
      });
      setPrices({ ...prices, [serviceTier]: "" });
      setNotice("已发布新价格版本。已有报价金额不变。");
      await load();
    } catch (caught) {
      setError(caught instanceof AdminApiError ? caught.detail : "发布失败。");
    }
  }

  if (error !== null && settings === null) return <p role="alert">{error}</p>;
  if (settings === null) return <p role="status">加载中…</p>;

  return (
    <section>
      <h1>设置</h1>
      <p>
        这里<strong>不显示任何密钥</strong>：会话密钥、Worker 共享密钥、
        Admin 密钥与数据库地址都由环境变量管理，接口从不返回它们。
      </p>
      {error !== null && <p role="alert">{error}</p>}
      {notice !== null && <p role="status">{notice}</p>}

      <h2>定价</h2>
      <p>
        当前：简明评分 <strong>{yuan(settings.summary_cents_per_page)} / 页</strong>；
        逐页精批 <strong>{yuan(settings.annotated_cents_per_page)} / 页</strong>。
        调价会<strong>新建一个版本</strong>，已有报价与订单金额不受影响。
      </p>
      {([
        ["summary_report", "简明评分"],
        ["annotated_review", "逐页精批"],
      ] as const).map(([serviceTier, label]) => (
        <div className="filters" key={serviceTier}>
          <label htmlFor={`price-${serviceTier}`}>{label}新价格（分/页）</label>
          <input
            id={`price-${serviceTier}`}
            value={prices[serviceTier]}
            inputMode="numeric"
            onChange={(event) => setPrices({ ...prices, [serviceTier]: event.target.value })}
          />
          <button
            type="button"
            disabled={prices[serviceTier].trim() === ""}
            onClick={() => void publishPrice(serviceTier)}
          >
            发布{label}价格
          </button>
        </div>
      ))}

      <h2>运营参数</h2>
      <table>
        <thead>
          <tr>
            <th>参数</th>
            <th>当前值</th>
            <th>改为</th>
          </tr>
        </thead>
        <tbody>
          {FIELDS.map((field) => (
            <tr key={field.name}>
              <td>
                {field.label}
                {field.hint !== undefined && (
                  <small>（{field.hint}）</small>
                )}
              </td>
              <td>{settings[field.name]}</td>
              <td>
                <input
                  aria-label={field.label}
                  value={draft[field.name] ?? ""}
                  inputMode="numeric"
                  onChange={(event) =>
                    setDraft({ ...draft, [field.name]: event.target.value })
                  }
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button
        type="button"
        disabled={Object.values(draft).every((value) => !value)}
        onClick={() => void save()}
      >
        保存运营参数
      </button>
    </section>
  );
}
