/**
 * Environment configuration.
 *
 * Two independent switches are modelled here, and they must stay independent:
 *
 * - `auth`: "fake" uses the staging test-account login; "wechat" uses wx.login.
 * - `payment`: "simulate" calls the staging simulate-success endpoint;
 *   "wechat" calls wx.requestPayment.
 *
 * The fake login, fake payment and fake callback routes are NOT registered in
 * the server's production environment (server/main.py FAKE_ADAPTER_ENVIRONMENTS),
 * so pointing a "fake" profile at production would simply 404. Both code paths
 * exist here on purpose: production must never fall back to the fake one.
 *
 * No secret belongs in this file. The mini-program only ever holds a user
 * session token — never the Worker shared key, never the Admin key. `appid`
 * lives in project.config.json, which is supplied per developer.
 */

export const PROFILES = Object.freeze({
  staging: Object.freeze({
    name: "staging",
    // Overridden at build/dev time; the developer tool needs "skip domain
    // checks" enabled for a localhost base URL.
    baseUrl: "http://127.0.0.1:8000",
    auth: "fake",
    payment: "simulate",
  }),
  production: Object.freeze({
    name: "production",
    baseUrl: "https://api.skyedumath.com",
    auth: "wechat",
    payment: "wechat",
  }),
});

export const DEFAULT_PROFILE = "staging";

const DEVICE_DEBUG_PROFILE = "device-debug";
const HTTPS_ORIGIN =
  /^https:\/\/(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}(?::\d{1,5})?$/i;

export function resolveProfile(name) {
  const profile = PROFILES[name || DEFAULT_PROFILE];
  if (!profile) {
    throw new Error(`unknown environment profile: ${name}`);
  }
  return profile;
}

function decodeLaunchValue(value) {
  const text = String(value || "").trim();
  try {
    return decodeURIComponent(text);
  } catch (_error) {
    throw new Error("invalid device debug URL encoding");
  }
}

/**
 * Resolve an HTTPS-only profile used by an explicit true-device compile mode.
 *
 * Normal compilation has no launch query and therefore continues to use the
 * fixed staging localhost URL. A temporary tunnel URL is accepted only when
 * the caller explicitly selects `profile=device-debug`; it can never override
 * the production profile or be persisted in the default configuration.
 */
export function resolveLaunchProfile(launchOptions = {}, runtime = {}) {
  const query = launchOptions && launchOptions.query ? launchOptions.query : {};
  const requested =
    query.profile || (runtime.envVersion === "release" ? "production" : DEFAULT_PROFILE);
  const suppliedDebugUrl = query.deviceApiBaseUrl;

  if (requested !== DEVICE_DEBUG_PROFILE) {
    if (suppliedDebugUrl) {
      throw new Error("device debug URL requires profile=device-debug");
    }
    return resolveProfile(requested);
  }

  const baseUrl = decodeLaunchValue(suppliedDebugUrl).replace(/\/$/, "");
  if (!HTTPS_ORIGIN.test(baseUrl)) {
    throw new Error("device debug URL must be a plain HTTPS origin");
  }

  const portMatch = baseUrl.match(/:(\d{1,5})$/);
  if (portMatch && Number(portMatch[1]) > 65_535) {
    throw new Error("device debug URL contains an invalid port");
  }

  return Object.freeze({
    ...PROFILES.staging,
    name: DEVICE_DEBUG_PROFILE,
    baseUrl,
  });
}

export function usesFakeAuth(profile) {
  return profile.auth === "fake";
}

export function usesSimulatedPayment(profile) {
  return profile.payment === "simulate";
}
