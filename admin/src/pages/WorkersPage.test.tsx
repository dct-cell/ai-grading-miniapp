import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { SessionProvider } from "../api/session";
import { AftersalesPage } from "./AftersalesPage";
import { WorkersPage } from "./WorkersPage";

const SESSION = { admin_id: "a-1", username: "ops", csrf_token: "csrf-1" };

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const BUSY_WORKER = {
  worker_id: "w-1111",
  device_name: "studio-mac",
  platform: "darwin",
  architecture: "arm64",
  worker_version: "3.0.0",
  codex_version: "1.2.3",
  tex_version: "2025",
  status: "online",
  current_job_id: "job-98765432",
  last_heartbeat_at: "2026-08-11T02:00:00Z",
  active_job_state: "running",
  lease_expires_at: "2026-08-11T02:02:00Z",
};

const PENDING_REFUND = {
  refund_id: "r-1",
  order_id: "11111111-2222-3333-4444-555555555555",
  owner_public_id: "u-abc",
  state: "pending",
  source: "user",
  amount_cents: 8000,
  order_state: "refund_pending",
  created_at: "2026-08-11T02:00:00Z",
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

function renderPage(ui: React.ReactElement, transport: typeof fetch) {
  return render(
    <MemoryRouter>
      <SessionProvider transport={transport}>{ui}</SessionProvider>
    </MemoryRouter>,
  );
}

describe("WorkersPage", () => {
  it("shows the operational facts for each worker", async () => {
    const { transport } = harness((url) =>
      url.includes("/workers") ? json({ items: [BUSY_WORKER] }) : undefined,
    );

    renderPage(<WorkersPage />, transport);

    expect(await screen.findByText("studio-mac")).toBeInTheDocument();
    expect(screen.getByText("darwin / arm64")).toBeInTheDocument();
    expect(screen.getByText(/job-9876/)).toBeInTheDocument();
  });

  it("states plainly that draining does not cancel running work", async () => {
    const { transport } = harness((url) =>
      url.includes("/workers") ? json({ items: [BUSY_WORKER] }) : undefined,
    );

    renderPage(<WorkersPage />, transport);

    // An operator must not have to guess whether they are about to kill a
    // paid-for grading run.
    expect(await screen.findByText(/不会取消正在执行的任务/)).toBeInTheDocument();
  });

  it("sends drain with a csrf token and no job field", async () => {
    const { transport, calls } = harness((url) =>
      url.includes("/workers") ? json({ items: [BUSY_WORKER] }) : undefined,
    );

    renderPage(<WorkersPage />, transport);
    await screen.findByText("studio-mac");
    await userEvent.click(screen.getByRole("button", { name: "停止接单" }));

    const drain = calls.find((call) => call.url.endsWith("/workers/w-1111/drain"));
    expect(drain).toBeDefined();
    expect(
      (drain?.init.headers as Record<string, string>)["X-CSRF-Token"],
    ).toBe(SESSION.csrf_token);
    // The UI must not be able to ask for a job to be cancelled.
    expect(JSON.parse(String(drain?.init.body))).toEqual({});
  });

  it("offers an empty state", async () => {
    const { transport } = harness((url) =>
      url.includes("/workers") ? json({ items: [] }) : undefined,
    );

    renderPage(<WorkersPage />, transport);

    expect(await screen.findByText("还没有 Worker 注册。")).toBeInTheDocument();
  });

  it("reports an error instead of an empty table", async () => {
    const { transport } = harness((url) =>
      url.includes("/workers") ? json({ detail: "读取失败。" }, 500) : undefined,
    );

    renderPage(<WorkersPage />, transport);

    expect(await screen.findByRole("alert")).toHaveTextContent("读取失败。");
  });

  it("never displays the worker shared key or installation id", async () => {
    const { transport } = harness((url) =>
      url.includes("/workers") ? json({ items: [BUSY_WORKER] }) : undefined,
    );

    renderPage(<WorkersPage />, transport);
    await screen.findByText("studio-mac");

    expect(document.body.innerHTML).not.toContain("installation_id");
  });
});

describe("AftersalesPage", () => {
  it("lists a pending user refund", async () => {
    const { transport } = harness((url) =>
      url.includes("/aftersales") ? json({ items: [PENDING_REFUND] }) : undefined,
    );

    renderPage(<AftersalesPage />, transport);

    expect(await screen.findByText("用户申请")).toBeInTheDocument();
    expect(screen.getByText("¥80.00")).toBeInTheDocument();
  });

  it("distinguishes the user queue from technical refunds", async () => {
    const { transport } = harness((url) =>
      url.includes("/aftersales") ? json({ items: [PENDING_REFUND] }) : undefined,
    );

    renderPage(<AftersalesPage />, transport);

    // The two entry points must not be confusable: only one counts against the
    // user's refund metrics.
    expect(await screen.findByText(/技术性退款/)).toBeInTheDocument();
  });

  it("approves without sending an amount", async () => {
    const { transport, calls } = harness((url) =>
      url.includes("/aftersales") ? json({ items: [PENDING_REFUND] }) : undefined,
    );

    renderPage(<AftersalesPage />, transport);
    await screen.findByText("用户申请");
    await userEvent.click(screen.getByRole("button", { name: "批准退款" }));

    const approve = calls.find((call) => call.url.includes("/refunds/r-1/approve"));
    expect(approve).toBeDefined();
    // The amount comes from the order server-side; offering one here would be a
    // route to refunding an arbitrary sum.
    expect(JSON.parse(String(approve?.init.body))).toEqual({});
  });

  it("requires a reason before a rejection can be sent", async () => {
    const { transport, calls } = harness((url) =>
      url.includes("/aftersales") ? json({ items: [PENDING_REFUND] }) : undefined,
    );

    renderPage(<AftersalesPage />, transport);
    await screen.findByText("用户申请");
    await userEvent.click(screen.getByRole("button", { name: "驳回…" }));

    const confirm = screen.getByRole("button", { name: "确认驳回" });
    expect(confirm).toBeDisabled();
    expect(calls.some((call) => call.url.includes("/reject"))).toBe(false);

    await userEvent.type(screen.getByLabelText("驳回原因"), "批改无误");
    await userEvent.click(confirm);

    const reject = calls.find((call) => call.url.includes("/refunds/r-1/reject"));
    expect(JSON.parse(String(reject?.init.body))).toEqual({ reason: "批改无误" });
  });

  it("offers a retry for a failed refund rather than a new one", async () => {
    const { transport } = harness((url) =>
      url.includes("/aftersales")
        ? json({ items: [{ ...PENDING_REFUND, state: "refund_failed" }] })
        : undefined,
    );

    renderPage(<AftersalesPage />, transport);

    // Retrying reuses the same refund row and external id; creating a second
    // would be a double payment.
    expect(await screen.findByRole("button", { name: "重试退款" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "批准退款" })).toBeNull();
  });

  it("filters by state through the api", async () => {
    const { transport, calls } = harness((url) =>
      url.includes("/aftersales") ? json({ items: [PENDING_REFUND] }) : undefined,
    );

    renderPage(<AftersalesPage />, transport);
    await screen.findByText("用户申请");
    await userEvent.selectOptions(screen.getByLabelText("状态"), "refunded");

    await waitFor(() =>
      expect(
        calls.some((call) => call.url.includes("state=refunded")),
      ).toBe(true),
    );
  });

  it("shows an empty state", async () => {
    const { transport } = harness((url) =>
      url.includes("/aftersales") ? json({ items: [] }) : undefined,
    );

    renderPage(<AftersalesPage />, transport);

    expect(await screen.findByText("没有待处理的售后。")).toBeInTheDocument();
  });
});
