/**
 * Persistent session storage.
 *
 * The server returns the raw session token exactly once and stores only its
 * SHA-256, so the mini-program is the only place the raw token exists. It is
 * kept in mini-program storage together with an absolute expiry, and it is
 * never written to a log.
 *
 * `storage` is injected (a Map in tests, a wx-storage shim in the runtime) so
 * this module can be exercised without the WeChat runtime.
 */

export const SESSION_KEY = "grader.session";
export const DEVICE_KEY = "grader.device";

//Refresh slightly before the real deadline so a request cannot be sent with a
// token that expires in flight.
const EXPIRY_SKEW_MS = 60_000;

export function createSessionStore({ storage, now = () => Date.now() }) {
  if (!storage || typeof storage.get !== "function") {
    throw new TypeError("createSessionStore requires a storage with get/set");
  }

  return {
    /** The raw token, or null when absent or (nearly) expired. */
    getToken() {
      const session = storage.get(SESSION_KEY);
      if (!session || !session.token) {
        return null;
      }
      if (typeof session.expiresAt === "number" && session.expiresAt - EXPIRY_SKEW_MS <= now()) {
        return null;
      }
      return session.token;
    },

    read() {
      return storage.get(SESSION_KEY) || null;
    },

    /**
     * Persist a freshly issued session.
     *
     * `expiresIn` is the server's value in *seconds*; it is converted to an
     * absolute timestamp so a device clock change or a long suspend cannot
     * make an expired token look valid forever.
     */
    save({ token, expiresIn, user }) {
      const session = {
        token,
        expiresAt: now() + Number(expiresIn) * 1000,
        user: user || null,
      };
      storage.set(SESSION_KEY, session);
      return session;
    },

    clear() {
      storage.remove(SESSION_KEY);
    },

    /**
     * A stable per-install identity for the staging fake auth provider.
     *
     * It is generated once and reused, so repeated cold starts map to the same
     * server-side user instead of littering the database with new accounts.
     */
    getOrCreateDeviceId(generate) {
      const existing = storage.get(DEVICE_KEY);
      if (existing) {
        return existing;
      }
      const created = generate();
      storage.set(DEVICE_KEY, created);
      return created;
    },
  };
}

/** Storage backed by the mini-program's synchronous storage API. */
export function createWxStorage(wxApi) {
  return {
    get(key) {
      try {
        const value = wxApi.getStorageSync(key);
        return value === "" ? null : value;
      } catch (error) {
        return null;
      }
    },
    set(key, value) {
      wxApi.setStorageSync(key, value);
    },
    remove(key) {
      try {
        wxApi.removeStorageSync(key);
      } catch (error) {
        /* Storage removal failures are not actionable for the user. */
      }
    },
  };
}

/** A Map-backed storage, used by tests. */
export function createMemoryStorage(initial) {
  const map = initial instanceof Map ? initial : new Map();
  return {
    get(key) {
      return map.has(key) ? map.get(key) : null;
    },
    set(key, value) {
      map.set(key, value);
    },
    remove(key) {
      map.delete(key);
    },
  };
}
