import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { SessionProvider } from "./api/session";

const SESSION = {
  admin_id: "a-1",
  username: "ops-zhang",
  csrf_token: "csrf-token-1",
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * Minimal payloads so each page can render past its loading state. The shell
 * tests are about routing and credentials, not about page content, but a page
 * that never resolves would leave every assertion looking at a spinner.
 */
const PAGE_DATA: Record<string, unknown> = {
  "/overview": {
    orders: {},
    jobs: {},
    workers: {},
    refunds: {},
    storage: { used_percent: 10, latest_backup_age_seconds: null },
  },
  "/orders": { items: [], next_cursor: null },
  "/workers": { items: [] },
  "/aftersales": { items: [] },
  "/audit": { items: [] },
  "/funds": {
    payments: { succeeded_cents: 0, succeeded_count: 0 },
    refunds: {
      refunded_cents: 0,
      technical_refunded_cents: 0,
      failed_count: 0,
      pending_count: 0,
    },
    reconciliation: { source: "none", settled_to_bank_cents: null },
  },
  "/settings": {
    cents_per_page: 1000,
    max_pdf_pages: 30,
    max_pdf_bytes: 26214400,
    quote_ttl_seconds: 86400,
    acceptance_ttl_seconds: 259200,
    minutes_per_page: 10,
    automatic_refund_max_amount_cents: 5000,
    automatic_refund_max_monthly_count: 4,
  },
};

/** A transport that answers /auth/session and records every call. */
function transportFor(
  responses: Array<(url: string, init: RequestInit) => Response | undefined>,
) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const transport = vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init });
    for (const responder of responses) {
      const response = responder(url, init);
      if (response !== undefined) return response;
    }
    for (const [path, body] of Object.entries(PAGE_DATA)) {
      if (url.startsWith(`/admin/api/v1${path}`)) return json(body);
    }
    return json({}, 200);
  });
  return { transport: transport as unknown as typeof fetch, calls };
}

function renderApp(
  transport: typeof fetch,
  { route = "/overview" }: { route?: string } = {},
) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <SessionProvider transport={transport}>
        <App />
      </SessionProvider>
    </MemoryRouter>,
  );
}

describe("App shell", () => {
  it("routes an unauthenticated visitor to the login form", async () => {
    const { transport } = transportFor([
      (url) => (url.includes("/auth/session") ? json({}, 401) : undefined),
    ]);

    renderApp(transport);

    expect(
      await screen.findByRole("heading", { name: "管理台登录" }),
    ).toBeInTheDocument();
  });

  it("shows the requested page once the session resolves", async () => {
    const { transport } = transportFor([
      (url) => (url.includes("/auth/session") ? json(SESSION) : undefined),
    ]);

    renderApp(transport);

    expect(await screen.findByRole("heading", { name: "总览" })).toBeInTheDocument();
    expect(screen.getByText("ops-zhang")).toBeInTheDocument();
  });

  it("never writes a credential to web storage", async () => {
    const { transport } = transportFor([
      (url) => (url.includes("/auth/session") ? json(SESSION) : undefined),
    ]);

    renderApp(transport);
    await screen.findByRole("heading", { name: "总览" });

    // The session rides on an HttpOnly cookie, so a persisted copy would be a
    // new, script-readable credential.
    expect(Object.keys(localStorage)).toHaveLength(0);
    expect(Object.keys(sessionStorage)).toHaveLength(0);
    expect(document.body.innerHTML).not.toContain(SESSION.csrf_token);
  });

  it("logs in and then loads the console", async () => {
    let authenticated = false;
    const { transport, calls } = transportFor([
      (url, init) => {
        if (url.includes("/auth/login") && init.method === "POST") {
          authenticated = true;
          return new Response(null, { status: 204 });
        }
        if (url.includes("/auth/session")) {
          return authenticated ? json(SESSION) : json({}, 401);
        }
        return undefined;
      },
    ]);

    renderApp(transport, { route: "/orders" });
    await screen.findByRole("heading", { name: "管理台登录" });

    await userEvent.type(screen.getByLabelText("用户名"), "ops-zhang");
    await userEvent.type(screen.getByLabelText("密码"), "correct horse");
    await userEvent.click(screen.getByRole("button", { name: "登录" }));

    // It returns to the page that was originally requested.
    expect(await screen.findByRole("heading", { name: "订单" })).toBeInTheDocument();
    const login = calls.find((call) => call.url.includes("/auth/login"));
    expect(login?.init.credentials).toBe("include");
  });

  it("reports a failed login without inventing detail", async () => {
    const { transport } = transportFor([
      (url, init) => {
        if (url.includes("/auth/login") && init.method === "POST") {
          return json({ detail: "用户名或密码不正确。" }, 401);
        }
        if (url.includes("/auth/session")) return json({}, 401);
        return undefined;
      },
    ]);

    renderApp(transport);
    await screen.findByRole("heading", { name: "管理台登录" });

    await userEvent.type(screen.getByLabelText("用户名"), "ops-zhang");
    await userEvent.type(screen.getByLabelText("密码"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "用户名或密码不正确。",
    );
  });

  it("surfaces a rate-limit refusal to the operator", async () => {
    const { transport } = transportFor([
      (url, init) => {
        if (url.includes("/auth/login") && init.method === "POST") {
          return json({ detail: "登录尝试过于频繁，请稍后再试。" }, 429);
        }
        if (url.includes("/auth/session")) return json({}, 401);
        return undefined;
      },
    ]);

    renderApp(transport);
    await screen.findByRole("heading", { name: "管理台登录" });

    await userEvent.type(screen.getByLabelText("用户名"), "ops-zhang");
    await userEvent.type(screen.getByLabelText("密码"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("过于频繁");
  });

  it("does not keep the password in the form after submitting", async () => {
    const { transport } = transportFor([
      (url, init) => {
        if (url.includes("/auth/login") && init.method === "POST") {
          return json({ detail: "用户名或密码不正确。" }, 401);
        }
        if (url.includes("/auth/session")) return json({}, 401);
        return undefined;
      },
    ]);

    renderApp(transport);
    await screen.findByRole("heading", { name: "管理台登录" });
    await userEvent.type(screen.getByLabelText("用户名"), "ops-zhang");
    await userEvent.type(screen.getByLabelText("密码"), "secret-value");
    await userEvent.click(screen.getByRole("button", { name: "登录" }));

    await screen.findByRole("alert");
    expect(screen.getByLabelText("密码")).toHaveValue("");
  });

  it("sends the csrf token when logging out", async () => {
    const { transport, calls } = transportFor([
      (url) => (url.includes("/auth/session") ? json(SESSION) : undefined),
      (url) =>
        url.includes("/auth/logout") ? new Response(null, { status: 204 }) : undefined,
    ]);

    renderApp(transport);
    await screen.findByRole("heading", { name: "总览" });
    await userEvent.click(screen.getByRole("button", { name: "退出" }));

    const logout = calls.find((call) => call.url.includes("/auth/logout"));
    expect(
      (logout?.init.headers as Record<string, string>)["X-CSRF-Token"],
    ).toBe(SESSION.csrf_token);
    expect(
      await screen.findByRole("heading", { name: "管理台登录" }),
    ).toBeInTheDocument();
  });

  it("returns to the login form when any call reports a 401", async () => {
    let sessionValid = true;
    const { transport } = transportFor([
      (url) => {
        if (url.includes("/auth/session")) {
          return sessionValid ? json(SESSION) : json({}, 401);
        }
        if (url.includes("/auth/logout")) {
          sessionValid = false;
          return json({}, 401);
        }
        return undefined;
      },
    ]);

    renderApp(transport);
    await screen.findByRole("heading", { name: "总览" });
    sessionValid = false;
    await userEvent.click(screen.getByRole("button", { name: "退出" }));

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "管理台登录" })).toBeInTheDocument(),
    );
  });

  it("exposes every planned route", async () => {
    const routes = [
      ["/overview", "总览"],
      ["/orders", "订单"],
      ["/aftersales", "售后"],
      ["/workers", "Worker"],
      ["/users", "用户"],
      ["/funds", "资金"],
      ["/settings", "设置"],
      ["/audit", "审计"],
    ] as const;

    for (const [route, heading] of routes) {
      const { transport } = transportFor([
        (url) => (url.includes("/auth/session") ? json(SESSION) : undefined),
      ]);
      const view = renderApp(transport, { route });
      expect(
        await screen.findByRole("heading", { name: heading }),
      ).toBeInTheDocument();
      view.unmount();
    }
  });

  it("only ever calls the admin api boundary", async () => {
    const { transport, calls } = transportFor([
      (url) => (url.includes("/auth/session") ? json(SESSION) : undefined),
    ]);

    renderApp(transport);
    await screen.findByRole("heading", { name: "总览" });

    expect(calls).not.toHaveLength(0);
    for (const call of calls) {
      expect(call.url.startsWith("/admin/api/v1")).toBe(true);
    }
  });
});
