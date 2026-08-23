import test from "node:test";
import assert from "node:assert/strict";

import { formatCents, formatEta, formatDeadline, standardLabel } from "../utils/format.js";

test("formats server cent amounts without doing arithmetic on price", () => {
  assert.equal(formatCents(3000), "¥30.00");
  assert.equal(formatCents(0), "¥0.00");
  assert.equal(formatCents(1), "¥0.01");
  // Missing amounts render as a placeholder rather than "¥NaN".
  assert.equal(formatCents(undefined), "—");
});

test("renders the server eta range and never a local countdown", () => {
  assert.equal(
    formatEta({ earliest_minutes: 20, latest_minutes: 40 }),
    "预计还需 20 分钟 ~ 40 分钟",
  );
  assert.equal(formatEta({ earliest_minutes: 90, latest_minutes: 90 }), "预计还需 1 小时 30 分钟");
});

test("shows nothing when the server supplies no eta", () => {
  // A null eta means no pending work or no ready Worker; inventing a countdown
  // would promise a turnaround nobody is working towards.
  assert.equal(formatEta(null), "");
  assert.equal(formatEta(undefined), "");
  assert.equal(formatEta({}), "");
});

test("derives the acceptance window from the server deadline", () => {
  const now = Date.parse("2026-08-10T00:00:00Z");
  assert.equal(formatDeadline("2026-08-12T12:00:00Z", now), "剩余 2 天 12 小时");
  assert.equal(formatDeadline("2026-08-10T05:00:00Z", now), "剩余 5 小时");
  assert.equal(formatDeadline("2026-08-09T00:00:00Z", now), "已超过处理期限");
  assert.equal(formatDeadline(null, now), "");
});

test("labels the three grading standards", () => {
  assert.match(standardLabel("league_second_round"), /联赛二试/);
  assert.match(standardLabel("cmo"), /CMO/);
  assert.match(standardLabel("imo"), /IMO/);
  // An unknown value falls back to itself rather than crashing the page.
  assert.equal(standardLabel("unknown"), "unknown");
});
