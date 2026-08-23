import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { SessionProvider } from "../api/session";
import { OrderDetailPage } from "./OrderDetailPage";
import { OrdersPage } from "./OrdersPage";
import { OverviewPage } from "./OverviewPage";

const SESSION = { admin_id: "a-1", username: "ops", csrf_token: "csrf-1" };

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const ORDER_ROW = {
  id: "11111111-2222-3333-4444-555555555555",
  state: "v1_queued",
  owner_public_id: "u-abc123",
  paid_amount_cents: 2000,
  page_count: 2,
  current_round_number: 1,
  created_at: "2026-08-11T02:00:00Z",
};

const ORDER_DETAIL = {
  ...ORDER_ROW,
  grading_standard: "league-second-round",
  note: "",
  acceptance_deadline: null,
  downloads_revoked_at: null,
  payment: {
    id: "p-1",
    state: "succeeded",
    amount_cents: 2000,
    external_transaction_id: "wx-tx-1",
  },
  refunds: [],
  rounds: [
    {
      round_number: 1,
      delivered_at: null,
      has_result_pdf: false,
      has_result_json: false,
      job: {
        id: "j-1",
        state: "queued",
        worker_id: null,
        attempt_count: 0,
        lease_version: 0,
        lease_expires_at: null,
      },
    },
  ],
  files: [{ kind: "source_pdf", size_bytes: 51200 }],
  timeline: [{ event: "order_created", at: "2026-08-11T02:00:00Z" }],
  available_admin_actions: ["technical_refund"],
};

function harness(
  responder: (url: string, init: RequestInit) => Response | undefined,
) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const transport = vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init });
    if (url.includes("/auth/session")) return json(SESSION);
    return responder(url, init) ?? json({});
  });
  return { transport: transport as unknown as typeof fetch, calls };
}

function renderWithSession(
  ui: React.ReactElement,
  transport: typeof fetch,
  route = "/",
) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <SessionProvider transport={transport}>{ui}</SessionProvider>
    </MemoryRouter>,
  );
}

describe("OrdersPage", () => {
  it("shows a loading state before the first response", async () => {
    const { transport } = harness(() => json({ items: [], next_cursor: null }));

    renderWithSession(<OrdersPage />, transport);

    expect(screen.getByRole("status")).toHaveTextContent("加载中");
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
  });

  it("shows an empty state when nothing matches", async () => {
    const { transport } = harness((url) =>
      url.includes("/orders") ? json({ items: [], next_cursor: null }) : undefined,
    );

    renderWithSession(<OrdersPage />, transport);

    expect(await screen.findByText("没有符合条件的订单。")).toBeInTheDocument();
  });

  it("shows an error state when the request fails", async () => {
    const { transport } = harness((url) =>
      url.includes("/orders") ? json({ detail: "服务暂时不可用。" }, 500) : undefined,
    );

    renderWithSession(<OrdersPage />, transport);

    expect(await screen.findByRole("alert")).toHaveTextContent("服务暂时不可用。");
  });

  it("renders orders once populated", async () => {
    const { transport } = harness((url) =>
      url.includes("/orders")
        ? json({ items: [ORDER_ROW], next_cursor: null })
        : undefined,
    );

    renderWithSession(<OrdersPage />, transport);

    expect(await screen.findByText("u-abc123")).toBeInTheDocument();
    expect(screen.getByText("¥20.00")).toBeInTheDocument();
  });

  it("serialises filters into the url and the request", async () => {
    const { transport, calls } = harness((url) =>
      url.includes("/orders")
        ? json({ items: [ORDER_ROW], next_cursor: null })
        : undefined,
    );

    renderWithSession(<OrdersPage />, transport, "/?state=v1_queued");

    await screen.findByText("u-abc123");
    const request = calls.find((call) => call.url.includes("/orders"));
    expect(request?.url).toContain("state=v1_queued");
  });

  it("never displays a server file path", async () => {
    const { transport } = harness((url) =>
      url.includes("/orders")
        ? json({ items: [ORDER_ROW], next_cursor: null })
        : undefined,
    );

    renderWithSession(<OrdersPage />, transport);

    await screen.findByText("u-abc123");
    expect(document.body.innerHTML).not.toContain("relative_path");
  });
});

describe("OverviewPage", () => {
  const OVERVIEW = {
    orders: { v1_delivered: 3, v2_delivered: 1 },
    jobs: { queued: 2, running: 1, worker_exception: 0 },
    workers: { online: 2, suspected_offline: 0, draining: 1, disabled: 1 },
    refunds: { pending_manual: 4, failed: 0 },
    storage: { used_percent: 42.5, latest_backup_age_seconds: null },
  };

  it("renders every headline metric", async () => {
    const { transport } = harness((url) =>
      url.includes("/overview") ? json(OVERVIEW) : undefined,
    );

    renderWithSession(<OverviewPage />, transport);

    expect(await screen.findByText("42.5%")).toBeInTheDocument();
    expect(screen.getByText("待人工审批").nextSibling).toHaveTextContent("4");
  });

  it("does not claim a backup exists when none is configured", async () => {
    const { transport } = harness((url) =>
      url.includes("/overview") ? json(OVERVIEW) : undefined,
    );

    renderWithSession(<OverviewPage />, transport);

    // Phase 09 owns real backups; implying otherwise would misstate the
    // recovery point an operator can rely on.
    expect(await screen.findByText(/尚未启用/)).toBeInTheDocument();
  });

  it("shows drained workers rather than dropping them from the counts", async () => {
    const { transport } = harness((url) =>
      url.includes("/overview") ? json(OVERVIEW) : undefined,
    );

    renderWithSession(<OverviewPage />, transport);

    // A drained Worker still exists and still holds its current job, so
    // omitting it would read as lost capacity.
    expect(await screen.findByText("停止接单")).toBeInTheDocument();
    expect(screen.getByText("停止接单").nextSibling).toHaveTextContent("1");
  });

  it("reports an error rather than rendering zeros", async () => {
    const { transport } = harness((url) =>
      url.includes("/overview") ? json({ detail: "读取失败。" }, 500) : undefined,
    );

    renderWithSession(<OverviewPage />, transport);

    expect(await screen.findByRole("alert")).toHaveTextContent("读取失败。");
  });
});

describe("OrderDetailPage", () => {
  function renderDetail(transport: typeof fetch) {
    return render(
      <MemoryRouter initialEntries={[`/orders/${ORDER_ROW.id}`]}>
        <SessionProvider transport={transport}>
          <Routes>
            <Route path="/orders/:orderId" element={<OrderDetailPage />} />
          </Routes>
        </SessionProvider>
      </MemoryRouter>,
    );
  }

  it("shows the order, its rounds and its attachments", async () => {
    const { transport } = harness((url) =>
      url.includes(`/orders/${ORDER_ROW.id}`) ? json(ORDER_DETAIL) : undefined,
    );

    renderDetail(transport);

    expect(await screen.findByText(ORDER_ROW.id)).toBeInTheDocument();
    expect(screen.getByText(/source_pdf/)).toBeInTheDocument();
    expect(screen.getByText(/order_created/)).toBeInTheDocument();
  });

  it("requires an explicit confirmation before a technical refund", async () => {
    const { transport, calls } = harness((url) =>
      url.includes(`/orders/${ORDER_ROW.id}`) ? json(ORDER_DETAIL) : undefined,
    );

    renderDetail(transport);
    await screen.findByText(ORDER_ROW.id);

    // The destructive call must not be reachable in one click.
    await userEvent.click(screen.getByRole("button", { name: /发起技术性退款/ }));
    const confirm = screen.getByRole("button", { name: /确认退款/ });
    expect(confirm).toBeDisabled();

    await userEvent.type(screen.getByLabelText(/原因/), "批改运行时崩溃");
    expect(confirm).toBeEnabled();
    expect(calls.some((call) => call.url.includes("/refunds/technical"))).toBe(false);

    await userEvent.click(confirm);
    const refund = calls.find((call) => call.url.includes("/refunds/technical"));
    expect(refund).toBeDefined();
    expect(
      (refund?.init.headers as Record<string, string>)["X-CSRF-Token"],
    ).toBe(SESSION.csrf_token);
    expect(JSON.parse(String(refund?.init.body))).toMatchObject({
      order_id: ORDER_ROW.id,
      reason: "批改运行时崩溃",
    });
  });

  it("never sends an amount with a technical refund", async () => {
    const { transport, calls } = harness((url) =>
      url.includes(`/orders/${ORDER_ROW.id}`) ? json(ORDER_DETAIL) : undefined,
    );

    renderDetail(transport);
    await screen.findByText(ORDER_ROW.id);
    await userEvent.click(screen.getByRole("button", { name: /发起技术性退款/ }));
    await userEvent.type(screen.getByLabelText(/原因/), "运行时失败");
    await userEvent.click(screen.getByRole("button", { name: /确认退款/ }));

    const refund = calls.find((call) => call.url.includes("/refunds/technical"));
    // The server refuses an amount outright; the UI must not offer one either.
    expect(Object.keys(JSON.parse(String(refund?.init.body)))).toEqual([
      "order_id",
      "reason",
    ]);
  });

  it("hides the refund control when the server does not offer it", async () => {
    const { transport } = harness((url) =>
      url.includes(`/orders/${ORDER_ROW.id}`)
        ? json({ ...ORDER_DETAIL, available_admin_actions: [] })
        : undefined,
    );

    renderDetail(transport);
    await screen.findByText(ORDER_ROW.id);

    expect(screen.queryByRole("button", { name: /发起技术性退款/ })).toBeNull();
  });

  it("shows a revoked download state", async () => {
    const { transport } = harness((url) =>
      url.includes(`/orders/${ORDER_ROW.id}`)
        ? json({ ...ORDER_DETAIL, downloads_revoked_at: "2026-08-11T03:00:00Z" })
        : undefined,
    );

    renderDetail(transport);

    expect(await screen.findByText("已撤销")).toBeInTheDocument();
  });
});
