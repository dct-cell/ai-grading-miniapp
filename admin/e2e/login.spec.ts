import { expect, test } from "@playwright/test";

/**
 * These tests need a real browser, a running backend on localhost:8000 and an
 * admin account. They are not part of `npm test`; see admin/README.md for the
 * setup and for how to run them.
 *
 * What they prove that the Vitest suite cannot: a genuine cookie jar accepts an
 * HttpOnly, SameSite=Strict, Path=/admin cookie through the Vite proxy and
 * returns it on the next request. That is the integration most likely to break
 * from a hostname or cookie-attribute mistake.
 */

const USERNAME = process.env.ADMIN_E2E_USERNAME ?? "ops-admin";
const PASSWORD = process.env.ADMIN_E2E_PASSWORD ?? "";

test.skip(
  PASSWORD === "",
  "Set ADMIN_E2E_USERNAME and ADMIN_E2E_PASSWORD to run the end-to-end suite.",
);

test("an admin can sign in and reach the overview", async ({ page }) => {
  await page.goto("/admin/login");

  await page.getByLabel("用户名").fill(USERNAME);
  await page.getByLabel("密码").fill(PASSWORD);
  await page.getByRole("button", { name: "登录" }).click();

  await expect(page.getByRole("heading", { name: "总览" })).toBeVisible();
});

test("the session cookie is HttpOnly and scoped to /admin", async ({ page }) => {
  await page.goto("/admin/login");
  await page.getByLabel("用户名").fill(USERNAME);
  await page.getByLabel("密码").fill(PASSWORD);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("heading", { name: "总览" })).toBeVisible();

  const cookies = await page.context().cookies();
  const session = cookies.find((cookie) => cookie.name === "grader_admin_session");
  expect(session).toBeDefined();
  expect(session?.httpOnly).toBe(true);
  expect(session?.path).toBe("/admin");
  expect(session?.sameSite).toBe("Strict");

  // Script must not be able to read it, which is the whole point of HttpOnly.
  const readable = await page.evaluate(() => document.cookie);
  expect(readable).not.toContain("grader_admin_session");
});

test("a reload keeps the operator signed in without any stored token", async ({
  page,
}) => {
  await page.goto("/admin/login");
  await page.getByLabel("用户名").fill(USERNAME);
  await page.getByLabel("密码").fill(PASSWORD);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("heading", { name: "总览" })).toBeVisible();

  await page.reload();

  await expect(page.getByRole("heading", { name: "总览" })).toBeVisible();
  const stored = await page.evaluate(() => ({
    local: Object.keys(localStorage),
    session: Object.keys(sessionStorage),
  }));
  expect(stored.local).toHaveLength(0);
  expect(stored.session).toHaveLength(0);
});

test("wrong credentials are refused with the server's wording", async ({ page }) => {
  await page.goto("/admin/login");

  await page.getByLabel("用户名").fill(USERNAME);
  await page.getByLabel("密码").fill("definitely-not-the-password");
  await page.getByRole("button", { name: "登录" }).click();

  await expect(page.getByRole("alert")).toContainText("不正确");
  await expect(page.getByRole("heading", { name: "管理台登录" })).toBeVisible();
});
