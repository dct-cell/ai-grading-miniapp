/**
 * The only way this SPA talks to the server.
 *
 * Authentication is entirely the HttpOnly session cookie: `credentials:
 * "include"` sends it, and the SPA never reads, writes or stores a token. That
 * is deliberate — a token in `localStorage` would be readable by any injected
 * script, whereas the cookie is not, and there is nothing for the SPA to leak.
 *
 * The transport is injected so tests can assert on exactly what would go over
 * the wire without a live server or a `fetch` stub reaching the network.
 */

export const ADMIN_API_PREFIX = "/admin/api/v1";

/** The one path where a 401 means "wrong credentials", not "session expired". */
const LOGIN_PATH = "/auth/login";

export type QueryValue = string | number | boolean | undefined | null;

export interface AdminClientOptions {
  /** Injected for tests; defaults to the global `fetch`. */
  transport?: typeof fetch;
  /** Returns the CSRF token for the current session, or null before login. */
  csrfToken: () => string | null;
  /** Called on a 401 so the shell can clear UI state and route to /login. */
  onUnauthorized?: () => void;
}

/** A failed Admin API call, carrying the server's status and message. */
export class AdminApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail || `请求失败（${status}）`);
    this.name = "AdminApiError";
    this.status = status;
    this.detail = detail;
  }
}

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const url = `${ADMIN_API_PREFIX}${path}`;
  if (!query) return url;

  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    // An empty filter means "no filter", so it must not become `state=`, which
    // the server would read as a request for the empty state.
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const serialised = params.toString();
  return serialised ? `${url}?${serialised}` : url;
}

async function readBody(response: Response): Promise<unknown> {
  if (response.status === 204) return null;
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function detailFrom(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return `请求失败（${status}）`;
}

export interface AdminClient {
  get<T = unknown>(path: string, query?: Record<string, QueryValue>): Promise<T>;
  post<T = unknown>(path: string, body?: unknown): Promise<T>;
  patch<T = unknown>(path: string, body?: unknown): Promise<T>;
}

export function createAdminClient(options: AdminClientOptions): AdminClient {
  const transport = options.transport ?? fetch;

  async function request<T>(
    method: string,
    path: string,
    { body, query }: { body?: unknown; query?: Record<string, QueryValue> } = {},
  ): Promise<T> {
    const headers: Record<string, string> = {};
    const mutating = method !== "GET";

    if (mutating) {
      headers["Content-Type"] = "application/json";
      const csrf = options.csrfToken();
      // Only mutations carry the CSRF token. Sending it on reads would suggest
      // it authenticates the request, which it does not — the cookie does.
      if (csrf) headers["X-CSRF-Token"] = csrf;
    }

    const response = await transport(buildUrl(path, query), {
      method,
      credentials: "include",
      headers,
      ...(mutating ? { body: JSON.stringify(body ?? {}) } : {}),
    });

    const payload = await readBody(response);

    if (response.status === 401) {
      // The login endpoint is the one place a 401 is an answer rather than an
      // expiry: it means the credentials were wrong. Rewriting it to "session
      // expired" would both mislead the operator and wrongly clear UI state
      // for a session that never existed.
      if (path === LOGIN_PATH) {
        throw new AdminApiError(401, detailFrom(payload, 401));
      }
      options.onUnauthorized?.();
      throw new AdminApiError(401, "登录已过期，请重新登录。");
    }
    if (!response.ok) {
      throw new AdminApiError(response.status, detailFrom(payload, response.status));
    }
    return payload as T;
  }

  return {
    get: (path, query) => request("GET", path, { query }),
    post: (path, body) => request("POST", path, { body }),
    patch: (path, body) => request("PATCH", path, { body }),
  };
}
