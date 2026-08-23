import { describe, expect, it, vi } from "vitest";

import { AdminApiError, createAdminClient } from "./client";

const ok = (body: unknown = {}) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

describe("createAdminClient", () => {
  it("sends credentials and csrf for mutations", async () => {
    const transport = vi.fn().mockResolvedValue(ok());
    const client = createAdminClient({ transport, csrfToken: () => "csrf-1" });

    await client.post("/orders/o1/refund", {});

    expect(transport).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({
        credentials: "include",
        headers: expect.objectContaining({ "X-CSRF-Token": "csrf-1" }),
      }),
    );
  });

  it("targets the admin api boundary and nothing else", async () => {
    const transport = vi.fn().mockResolvedValue(ok());
    const client = createAdminClient({ transport, csrfToken: () => "csrf-1" });

    await client.get("/orders");

    const [url] = transport.mock.calls[0];
    expect(url).toBe("/admin/api/v1/orders");
  });

  it("sends cookies on reads too, but no csrf header", async () => {
    // Reads are cookie-authenticated like everything else; CSRF only protects
    // state changes, and sending it on reads would imply it is a credential.
    const transport = vi.fn().mockResolvedValue(ok());
    const client = createAdminClient({ transport, csrfToken: () => "csrf-1" });

    await client.get("/orders");

    const [, init] = transport.mock.calls[0];
    expect(init.credentials).toBe("include");
    expect(init.headers["X-CSRF-Token"]).toBeUndefined();
  });

  it("reports a 401 so the caller can clear state and route to login", async () => {
    const transport = vi.fn().mockResolvedValue(new Response("", { status: 401 }));
    const onUnauthorized = vi.fn();
    const client = createAdminClient({
      transport,
      csrfToken: () => "csrf-1",
      onUnauthorized,
    });

    await expect(client.get("/orders")).rejects.toBeInstanceOf(AdminApiError);
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("treats a login 401 as wrong credentials, not an expired session", async () => {
    // At login there is no session to expire, so relaying the server's wording
    // matters: it is deliberately identical for an unknown username and a bad
    // password, and the UI must not replace it with "please log in again".
    const transport = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "用户名或密码不正确。" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const onUnauthorized = vi.fn();
    const client = createAdminClient({
      transport,
      csrfToken: () => null,
      onUnauthorized,
    });

    await expect(
      client.post("/auth/login", { username: "a", password: "b" }),
    ).rejects.toMatchObject({ status: 401, detail: "用户名或密码不正确。" });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("does not invoke the unauthorized hook for other failures", async () => {
    const transport = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "冲突" }), { status: 409 }),
    );
    const onUnauthorized = vi.fn();
    const client = createAdminClient({
      transport,
      csrfToken: () => "csrf-1",
      onUnauthorized,
    });

    await expect(client.post("/refunds/r1/approve", {})).rejects.toMatchObject({
      status: 409,
      detail: "冲突",
    });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("never places a credential in localStorage", async () => {
    const transport = vi.fn().mockResolvedValue(ok({ csrf_token: "csrf-9" }));
    const client = createAdminClient({ transport, csrfToken: () => "csrf-1" });

    await client.get("/auth/session");

    // The session rides on an HttpOnly cookie the SPA cannot read, so there is
    // nothing to persist. Storing anything here would create an XSS-readable
    // credential where none existed.
    expect(Object.keys(localStorage)).toHaveLength(0);
  });

  it("serialises the body as json for mutations", async () => {
    const transport = vi.fn().mockResolvedValue(ok());
    const client = createAdminClient({ transport, csrfToken: () => "csrf-1" });

    await client.post("/refunds/r1/reject", { reason: "证据不足" });

    const [, init] = transport.mock.calls[0];
    expect(init.method).toBe("POST");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(init.body)).toEqual({ reason: "证据不足" });
  });

  it("tolerates a 204 with no body", async () => {
    const transport = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    const client = createAdminClient({ transport, csrfToken: () => "csrf-1" });

    await expect(client.post("/auth/logout", {})).resolves.toBeNull();
  });

  it("passes query parameters through", async () => {
    const transport = vi.fn().mockResolvedValue(ok());
    const client = createAdminClient({ transport, csrfToken: () => "csrf-1" });

    await client.get("/orders", { state: "queued", cursor: "abc" });

    const [url] = transport.mock.calls[0];
    expect(url).toBe("/admin/api/v1/orders?state=queued&cursor=abc");
  });

  it("omits empty query parameters rather than sending blanks", async () => {
    const transport = vi.fn().mockResolvedValue(ok());
    const client = createAdminClient({ transport, csrfToken: () => "csrf-1" });

    await client.get("/orders", { state: "", cursor: undefined, query: "o-1" });

    const [url] = transport.mock.calls[0];
    expect(url).toBe("/admin/api/v1/orders?query=o-1");
  });
});
