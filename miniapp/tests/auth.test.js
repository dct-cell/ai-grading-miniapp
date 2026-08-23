import test from "node:test";
import assert from "node:assert/strict";

import { createAuthService } from "../services/auth.js";
import { createMemoryStorage, createSessionStore, SESSION_KEY } from "../services/session.js";
import { ApiError } from "../services/api.js";

const STAGING = { name: "staging", baseUrl: "https://s.test", auth: "fake", payment: "simulate" };
const PRODUCTION = { name: "production", baseUrl: "https://p.test", auth: "wechat", payment: "wechat" };

function build({ storage, api, profile = STAGING, login, now = () => 1_000_000 }) {
  const sessions = createSessionStore({ storage, now });
  return {
    sessions,
    auth: createAuthService({ api, sessions, profile, login, now }),
  };
}

test("reuses a valid session before requesting a new staging identity", async () => {
  const storage = createMemoryStorage(
    new Map([[SESSION_KEY, { token: "t", expiresAt: 1_000_000 + 600_000 }]]),
  );
  let logins = 0;
  const { auth } = build({
    storage,
    api: {
      get: async path => {
        assert.equal(path, "/api/v1/me");
        return { id: "u1", public_id: "u-1" };
      },
      post: async () => {
        logins += 1;
        throw new Error("must not log in again");
      },
    },
  });

  const user = await auth.ensureLogin();
  assert.equal(user.id, "u1");
  assert.equal(logins, 0);
});

test("verifies the stored session against /api/v1/me, not /api/v1/auth/me", async () => {
  const storage = createMemoryStorage(
    new Map([[SESSION_KEY, { token: "t", expiresAt: 1_000_000 + 600_000 }]]),
  );
  const paths = [];
  const { auth } = build({
    storage,
    api: {
      get: async path => {
        paths.push(path);
        return { id: "u1" };
      },
      post: async () => ({}),
    },
  });

  await auth.ensureLogin();
  assert.deepEqual(paths, ["/api/v1/me"]);
});

test("logs in again when the stored session is rejected by the server", async () => {
  const storage = createMemoryStorage(
    new Map([[SESSION_KEY, { token: "revoked", expiresAt: 1_000_000 + 600_000 }]]),
  );
  const posts = [];
  const { auth, sessions } = build({
    storage,
    api: {
      get: async () => {
        throw new ApiError(401, "会话已过期。");
      },
      post: async (path, body) => {
        posts.push({ path, body });
        return {
          access_token: "fresh",
          expires_in: 2_591_999,
          user: { id: "u2", public_id: "u-2" },
        };
      },
    },
  });

  const user = await auth.ensureLogin();
  assert.equal(user.id, "u2");
  assert.equal(posts[0].path, "/api/v1/auth/login");
  assert.equal(sessions.getToken(), "fresh");
});

test("skips the /me probe when no usable token is stored", async () => {
  const storage = createMemoryStorage();
  let probes = 0;
  const { auth } = build({
    storage,
    api: {
      get: async () => {
        probes += 1;
        return { id: "x" };
      },
      post: async () => ({
        access_token: "new",
        expires_in: 100,
        user: { id: "u3" },
      }),
    },
  });

  const user = await auth.ensureLogin();
  assert.equal(user.id, "u3");
  assert.equal(probes, 0);
});

test("treats an expired stored token as absent without calling the server", async () => {
  const storage = createMemoryStorage(
    // Already past: stored expiry is behind `now`.
    new Map([[SESSION_KEY, { token: "old", expiresAt: 900_000 }]]),
  );
  let probes = 0;
  const { auth } = build({
    storage,
    api: {
      get: async () => {
        probes += 1;
        return {};
      },
      post: async () => ({ access_token: "n", expires_in: 100, user: { id: "u4" } }),
    },
  });

  const user = await auth.ensureLogin();
  assert.equal(user.id, "u4");
  assert.equal(probes, 0);
});

test("sends a test- prefixed code that the fake provider accepts", async () => {
  const storage = createMemoryStorage();
  let sentCode = null;
  const { auth } = build({
    storage,
    api: {
      get: async () => ({}),
      post: async (path, body) => {
        sentCode = body.code;
        return { access_token: "t", expires_in: 100, user: { id: "u5" } };
      },
    },
  });

  await auth.ensureLogin();

  // FakeAuthProvider rejects anything not starting with "test-" (400).
  assert.ok(sentCode.startsWith("test-"), `code was ${sentCode}`);
  assert.ok(sentCode.length > "test-".length);
});

test("reuses one device identity across cold starts", async () => {
  const storage = createMemoryStorage();
  const codes = [];
  const api = {
    get: async () => {
      throw new ApiError(401, "expired");
    },
    post: async (path, body) => {
      codes.push(body.code);
      return { access_token: "t", expires_in: 100, user: { id: "u6" } };
    },
  };

  await build({ storage, api }).auth.ensureLogin();
  // A second cold start shares the same storage.
  await build({ storage, api }).auth.ensureLogin();

  assert.equal(codes.length, 2);
  // A fresh identity per launch would create a new server-side user every
  // time, so the order history would appear to vanish.
  assert.equal(codes[0], codes[1]);
});

test("converts expires_in seconds into an absolute expiry", async () => {
  const storage = createMemoryStorage();
  const { auth, sessions } = build({
    storage,
    api: {
      get: async () => ({}),
      post: async () => ({
        access_token: "t",
        expires_in: 2_591_999,
        user: { id: "u7" },
      }),
    },
    now: () => 5_000_000,
  });

  await auth.ensureLogin();

  assert.equal(sessions.read().expiresAt, 5_000_000 + 2_591_999 * 1000);
});

test("production login forwards the wx.login code unchanged", async () => {
  const storage = createMemoryStorage();
  let sentCode = null;
  const { auth } = build({
    storage,
    profile: PRODUCTION,
    login: async () => ({ code: "081Abc-real-wx-code" }),
    api: {
      get: async () => ({}),
      post: async (path, body) => {
        sentCode = body.code;
        return { access_token: "t", expires_in: 100, user: { id: "u8" } };
      },
    },
  });

  await auth.ensureLogin();

  // The real provider must receive exactly what WeChat issued: no "test-"
  // prefix, no rewriting.
  assert.equal(sentCode, "081Abc-real-wx-code");
});

test("logout clears the stored session", async () => {
  const storage = createMemoryStorage(
    new Map([[SESSION_KEY, { token: "t", expiresAt: 1_000_000 + 600_000 }]]),
  );
  const { auth, sessions } = build({
    storage,
    api: { get: async () => ({ id: "u9" }), post: async () => ({}) },
  });

  await auth.ensureLogin();
  auth.logout();

  assert.equal(sessions.getToken(), null);
});
