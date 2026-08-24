import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { SessionProvider } from "../api/session";
import { AuditPage } from "./AuditPage";
import { FundsPage } from "./FundsPage";
import { SettingsPage } from "./SettingsPage";
import { UsersPage } from "./UsersPage";

const SESSION = { admin_id: "a-1", username: "ops", csrf_token: "csrf-1" };

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const SETTINGS = {
  summary_cents_per_page: 100,
  annotated_cents_per_page: 1000,
  max_pdf_pages: 30,
  max_pdf_bytes: 26214400,
  quote_ttl_seconds: 86400,
  acceptance_ttl_seconds: 259200,
  minutes_per_page: 10,
  automatic_refund_max_amount_cents: 5000,
  automatic_refund_max_monthly_count: 4,
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

describe("SettingsPage", () => {
  it("shows the operational values in force", async () => {
    const { transport } = harness((url) =>
      url.includes("/settings") ? json(SETTINGS) : undefined,
    );

    renderPage(<SettingsPage />, transport);

    expect(await screen.findByText("¥10.00 / 页")).toBeInTheDocument();
    expect(screen.getByText("30")).toBeInTheDocument();
  });

  it("never renders a secret field", async () => {
    const { transport } = harness((url) =>
      url.includes("/settings") ? json(SETTINGS) : undefined,
    );

    renderPage(<SettingsPage />, transport);
    await screen.findByText("¥10.00 / 页");

    const html = document.body.innerHTML;
    for (const forbidden of [
      "session_secret",
      "worker_shared_key",
      "admin_shared_key",
      "database_url",
    ]) {
      expect(html).not.toContain(forbidden);
    }
  });

  it("says plainly that repricing does not change existing quotes", async () => {
    const { transport } = harness((url) =>
      url.includes("/settings") ? json(SETTINGS) : undefined,
    );

    renderPage(<SettingsPage />, transport);

    expect(await screen.findByText(/新建一个版本/)).toBeInTheDocument();
  });

  it("publishes a price as a new version", async () => {
    const { transport, calls } = harness((url) => {
      if (url.includes("/settings/price-rules")) {
        return json({ id: "pr-1", cents_per_page: 1200, effective_from: "2026-08-11T00:00:00Z" }, 201);
      }
      return url.includes("/settings") ? json(SETTINGS) : undefined;
    });

    renderPage(<SettingsPage />, transport);
    await screen.findByText("¥10.00 / 页");
    await userEvent.type(
      screen.getByLabelText("逐页精批新价格（分/页）"),
      "1200",
    );
    await userEvent.click(
      screen.getByRole("button", { name: "发布逐页精批价格" }),
    );

    const published = calls.find((call) => call.url.includes("/price-rules"));
    expect(JSON.parse(String(published?.init.body))).toEqual({
      service_tier: "annotated_review",
      cents_per_page: 1200,
    });
    expect(
      (published?.init.headers as Record<string, string>)["X-CSRF-Token"],
    ).toBe(SESSION.csrf_token);
  });

  it("sends only the fields the operator edited", async () => {
    const { transport, calls } = harness((url) =>
      url.includes("/settings") ? json(SETTINGS) : undefined,
    );

    renderPage(<SettingsPage />, transport);
    await screen.findByText("¥10.00 / 页");
    await userEvent.type(screen.getByLabelText("PDF 页数上限"), "20");
    await userEvent.click(screen.getByRole("button", { name: "保存运营参数" }));

    const patch = calls.find((call) => call.init.method === "PATCH");
    expect(JSON.parse(String(patch?.init.body))).toEqual({ max_pdf_pages: 20 });
  });

  it("reports a rejected value rather than pretending it saved", async () => {
    const { transport } = harness((url, init) => {
      if (init.method === "PATCH") {
        return json({ detail: "配置项无效：max_pdf_pages" }, 422);
      }
      return url.includes("/settings") ? json(SETTINGS) : undefined;
    });

    renderPage(<SettingsPage />, transport);
    await screen.findByText("¥10.00 / 页");
    await userEvent.type(screen.getByLabelText("PDF 页数上限"), "9999");
    await userEvent.click(screen.getByRole("button", { name: "保存运营参数" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("配置项无效");
  });
});

describe("UsersPage", () => {
  const USER = {
    public_id: "u-abc123",
    created_at: "2026-08-01T00:00:00Z",
    order_count: 3,
    lifetime_paid_cents: 6000,
    lifetime_user_refunded_cents: 2000,
    technical_refunded_cents: 1000,
    monthly_user_refund_count: 1,
    lifetime_refund_ratio: 0.3333,
  };

  it("separates user refunds from technical refunds", async () => {
    const { transport } = harness((url) =>
      url.includes("/users/") ? json(USER) : undefined,
    );

    renderPage(<UsersPage />, transport);
    await userEvent.type(screen.getByLabelText("公开 ID"), "u-abc123");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));

    expect(await screen.findByText("¥20.00")).toBeInTheDocument();
    // Technical refunds are our fault and must be labelled as not counting.
    expect(screen.getByText(/不计入该用户指标/)).toBeInTheDocument();
    expect(screen.getByText("33.33%")).toBeInTheDocument();
  });

  it("never displays an openid even if the server sends one", async () => {
    // Assert on the *value*, not the word: the page copy legitimately mentions
    // openid to explain that it is not shown. This also proves the page renders
    // named fields rather than dumping whatever the server returned.
    const leaked = "wx-openid-should-never-render";
    const { transport } = harness((url) =>
      url.includes("/users/") ? json({ ...USER, openid: leaked }) : undefined,
    );

    renderPage(<UsersPage />, transport);
    await userEvent.type(screen.getByLabelText("公开 ID"), "u-abc123");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));

    await screen.findByText("¥20.00");
    expect(document.body.innerHTML).not.toContain(leaked);
  });

  it("reports an unknown user", async () => {
    const { transport } = harness((url) =>
      url.includes("/users/") ? json({ detail: "用户不存在。" }, 404) : undefined,
    );

    renderPage(<UsersPage />, transport);
    await userEvent.type(screen.getByLabelText("公开 ID"), "u-nope");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("用户不存在。");
  });
});

describe("FundsPage", () => {
  const FUNDS = {
    payments: { succeeded_cents: 10000, succeeded_count: 5 },
    refunds: {
      refunded_cents: 2000,
      technical_refunded_cents: 1000,
      failed_count: 0,
      pending_count: 1,
    },
    reconciliation: { source: "none", settled_to_bank_cents: null },
  };

  it("does not claim a bank settlement occurred", async () => {
    const { transport } = harness((url) =>
      url.includes("/funds") ? json(FUNDS) : undefined,
    );

    renderPage(<FundsPage />, transport);

    // Claiming money reached the bank without a statement to prove it would
    // misstate the accounts.
    expect(await screen.findByText(/不声称任何款项已到账/)).toBeInTheDocument();
  });

  it("summarises payments and refunds", async () => {
    const { transport } = harness((url) =>
      url.includes("/funds") ? json(FUNDS) : undefined,
    );

    renderPage(<FundsPage />, transport);

    expect(await screen.findByText(/¥100.00/)).toBeInTheDocument();
    expect(screen.getByText("¥20.00")).toBeInTheDocument();
  });
});

describe("AuditPage", () => {
  const ENTRY = {
    id: "al-1",
    actor_type: "admin",
    actor_id: "a-11111111",
    action: "worker.drain",
    target_type: "worker",
    target_id: "w-22222222",
    details: { status: "draining" },
    created_at: "2026-08-11T02:00:00Z",
  };

  it("lists entries and offers no edit or delete control", async () => {
    const { transport } = harness((url) =>
      url.includes("/audit") ? json({ items: [ENTRY] }) : undefined,
    );

    renderPage(<AuditPage />, transport);

    expect(await screen.findByText("worker.drain")).toBeInTheDocument();
    // The log is evidence, so the UI must not offer a way to alter it.
    expect(screen.queryByRole("button", { name: /删除/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /编辑/ })).toBeNull();
    expect(screen.getByText(/只增不改不删/)).toBeInTheDocument();
  });

  it("filters through the api", async () => {
    const { transport, calls } = harness((url) =>
      url.includes("/audit") ? json({ items: [ENTRY] }) : undefined,
    );

    renderPage(<AuditPage />, transport);
    await screen.findByText("worker.drain");
    await userEvent.type(screen.getByLabelText("动作"), "worker.drain");

    expect(
      calls.some((call) => call.url.includes("action=worker.drain")),
    ).toBe(true);
  });

  it("shows an empty state", async () => {
    const { transport } = harness((url) =>
      url.includes("/audit") ? json({ items: [] }) : undefined,
    );

    renderPage(<AuditPage />, transport);

    expect(await screen.findByText("没有符合条件的审计记录。")).toBeInTheDocument();
  });
});
