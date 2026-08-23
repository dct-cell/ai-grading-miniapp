import test from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_PROFILE,
  PROFILES,
  resolveLaunchProfile,
  resolveProfile,
} from "../config.js";

test("normal compilation keeps the existing localhost staging profile", () => {
  assert.equal(DEFAULT_PROFILE, "staging");
  assert.equal(resolveLaunchProfile(), PROFILES.staging);
  assert.equal(resolveProfile("staging").baseUrl, "http://127.0.0.1:8000");
});

test("an explicit device-debug launch accepts an encoded HTTPS origin", () => {
  const profile = resolveLaunchProfile({
    query: {
      profile: "device-debug",
      deviceApiBaseUrl: encodeURIComponent("https://quiet-proof.trycloudflare.com"),
    },
  });

  assert.equal(profile.name, "device-debug");
  assert.equal(profile.baseUrl, "https://quiet-proof.trycloudflare.com");
  assert.equal(profile.auth, "fake");
  assert.equal(profile.payment, "simulate");
});

test("device-debug strips one harmless trailing slash", () => {
  const profile = resolveLaunchProfile({
    query: {
      profile: "device-debug",
      deviceApiBaseUrl: "https://quiet-proof.trycloudflare.com/",
    },
  });

  assert.equal(profile.baseUrl, "https://quiet-proof.trycloudflare.com");
});

test("device URL cannot override staging or production implicitly", () => {
  for (const profile of ["staging", "production"]) {
    assert.throws(
      () =>
        resolveLaunchProfile({
          query: { profile, deviceApiBaseUrl: "https://quiet-proof.trycloudflare.com" },
        }),
      /requires profile=device-debug/,
    );
  }
});

test("device-debug rejects HTTP, paths, queries, credentials and invalid ports", () => {
  const invalid = [
    "http://quiet-proof.trycloudflare.com",
    "https://quiet-proof.trycloudflare.com/api",
    "https://quiet-proof.trycloudflare.com?x=1",
    "https://user:pass@quiet-proof.trycloudflare.com",
    "https://quiet-proof.trycloudflare.com:70000",
    "not-a-url",
    "",
  ];

  for (const deviceApiBaseUrl of invalid) {
    assert.throws(
      () => resolveLaunchProfile({ query: { profile: "device-debug", deviceApiBaseUrl } }),
      /device debug URL/,
      deviceApiBaseUrl,
    );
  }
});

test("unknown ordinary profiles still fail closed", () => {
  assert.throws(
    () => resolveLaunchProfile({ query: { profile: "preview" } }),
    /unknown environment profile/,
  );
});
