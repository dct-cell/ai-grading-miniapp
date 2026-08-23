/**
 * Login and session lifecycle.
 *
 * `ensureLogin` deliberately prefers an existing session:
 *
 *   stored token? -> verify with GET /api/v1/me -> reuse
 *                -> 401 => log in again
 *   no token      -> log in (no pointless /me round trip)
 *
 * Verifying on launch matters because the server can revoke or expire a
 * session independently of this client. Reusing matters because minting a new
 * staging identity on every cold start would create a new server-side user and
 * make the user's own order history disappear.
 *
 * The endpoint is GET /api/v1/me. (There is no /api/v1/auth/me.)
 */
import { ApiError } from "./api.js";
import { usesFakeAuth } from "../config.js";

const ME_PATH = "/api/v1/me";
const LOGIN_PATH = "/api/v1/auth/login";

/**
 * A random per-install identity for the staging fake provider.
 *
 * The `test-` prefix is required: FakeAuthProvider rejects any other code and
 * the login returns 400.
 */
function generateDeviceIdentity() {
  const random = Math.random().toString(36).slice(2, 10);
  const stamp = Date.now().toString(36);
  return `${stamp}${random}`;
}

export function createAuthService({ api, sessions, profile, login, now = () => Date.now() }) {
  async function authenticate() {
    let code;
    if (usesFakeAuth(profile)) {
      const identity = sessions.getOrCreateDeviceId(generateDeviceIdentity);
      code = `test-device-${identity}`;
    } else {
      if (typeof login !== "function") {
        throw new Error("wx.login is required outside staging");
      }
      const result = await login({});
      if (!result || !result.code) {
        throw new ApiError(0, "微信登录失败，请重试。");
      }
      // Forwarded verbatim: the server exchanges this with WeChat.
      code = result.code;
    }

    const issued = await api.post(LOGIN_PATH, { code });
    sessions.save({
      token: issued.access_token,
      expiresIn: issued.expires_in,
      user: issued.user,
    });
    return issued.user;
  }

  return {
    async ensureLogin() {
      if (sessions.getToken()) {
        try {
          return await api.get(ME_PATH);
        } catch (error) {
          // Only an authentication failure justifies re-login. A network blip
          // or a 500 must propagate, otherwise a transient outage would
          // silently strand the user on a brand-new identity.
          if (!(error instanceof ApiError) || error.status !== 401) {
            throw error;
          }
          sessions.clear();
        }
      }
      return authenticate();
    },

    /** Re-read the current user; used after actions that may change state. */
    refreshUser() {
      return api.get(ME_PATH);
    },

    logout() {
      sessions.clear();
    },
  };
}
