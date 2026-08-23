import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

/**
 * Structural checks over the mini-program source.
 *
 * Page files are WeChat runtime code (they call Page()/getApp()), so they
 * cannot be imported under plain Node. These tests therefore assert on the
 * source text — enough to catch the specific regressions that would break a
 * security or product invariant, without pretending to execute the page.
 */

const root = path.resolve(import.meta.dirname, "..");

function read(relative) {
  return fs.readFileSync(path.join(root, relative), "utf8");
}

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(path.join(root, dir), { withFileTypes: true })) {
    if (entry.name === "node_modules") continue;
    const relative = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      walk(relative, out);
    } else {
      out.push(relative);
    }
  }
  return out;
}

const allFiles = walk(".");
const sourceFiles = allFiles.filter(f => /\.(js|json|wxml|wxss)$/.test(f) && !f.startsWith("tests"));

test("the review page offers no way to pick a file", () => {
  const source = read("pages/aftersales/review.js") + read("pages/aftersales/review.wxml");
  // A review re-grades the same immutable PDF. Any picker here would let a
  // different document be graded than the one that was paid for.
  for (const api of ["chooseMessageFile", "chooseMedia", "chooseImage", "uploadFile", "pdf-picker"]) {
    assert.equal(source.includes(api), false, `review must not use ${api}`);
  }
});

test("the refund page has no amount input", () => {
  const wxml = read("pages/aftersales/refund.wxml");
  // Only the reason is chosen; the amount is display-only.
  assert.equal(/<input/.test(wxml), false, "refund must not accept a typed amount");
  assert.match(wxml, /amountText/);
});

test("no page computes a price or an amount", () => {
  // Amounts arrive from the server in cents and are only formatted. Any
  // arithmetic on cents_per_page would risk showing a total the server will
  // not charge.
  for (const file of sourceFiles.filter(f => f.endsWith(".js"))) {
    const source = read(file);
    assert.equal(
      /cents_per_page\s*\*|\*\s*cents_per_page|page_count\s*\*\s*|amount_cents\s*[+\-*/]\s*\d/.test(source),
      false,
      `${file} appears to compute an amount`,
    );
  }
});

test("no source file contains a credential or a hardcoded appid", () => {
  const forbidden = [
    "worker_shared_key",
    "WORKER_SHARED_KEY",
    "admin_shared_key",
    "ADMIN_SHARED_KEY",
    "session_secret",
    "SESSION_SECRET",
    "X-Worker-ID",
    "X-Admin-ID",
    "wx1234567890",
  ];
  for (const file of sourceFiles) {
    const source = read(file);
    for (const needle of forbidden) {
      assert.equal(source.includes(needle), false, `${file} must not contain ${needle}`);
    }
  }
});

test("the mini-program never calls the worker or admin credential domains", () => {
  for (const file of sourceFiles) {
    const source = read(file);
    assert.equal(source.includes("/worker/v1/"), false, `${file} must not call /worker/v1/*`);
    assert.equal(source.includes("/admin/api/"), false, `${file} must not call /admin/api/*`);
  }
});

test("the access token is never logged", () => {
  for (const file of sourceFiles.filter(f => f.endsWith(".js"))) {
    const source = read(file);
    const logsToken = /console\.(log|info|warn|error)\([^)]*(token|access_token|Authorization)/i.test(
      source,
    );
    assert.equal(logsToken, false, `${file} must not log a token`);
  }
});

test("both payment paths exist and production never uses the fake one", () => {
  const payments = read("services/payments.js");
  // Staging drives the verified callback service; production uses the real
  // gateway. Neither may be the only implementation.
  assert.match(payments, /simulate-success/);
  assert.match(payments, /requestPayment/);
  assert.match(payments, /usesSimulatedPayment/);

  const config = read("config.js");
  assert.match(config, /production/);
  assert.match(config, /payment: "wechat"/);
  assert.match(config, /payment: "simulate"/);
});

test("the detail page stops polling in both onHide and onUnload", () => {
  const detail = read("pages/orders/detail.js");
  // onHide alone leaks a timer when the page is destroyed; onUnload alone keeps
  // polling while the page sits in the background.
  assert.match(detail, /onHide\(\)\s*\{[^}]*stopPolling/);
  assert.match(detail, /onUnload\(\)\s*\{[^}]*stopPolling/);
});

test("confirmed payment builds the task-list-to-detail navigation stack", () => {
  const payment = read("pages/create/payment.js");
  const orders = read("pages/orders/index.js");

  assert.match(payment, /orderNavigationIntent\.set/);
  assert.match(payment, /switchTab\(\{ url: "\/pages\/orders\/index" \}\)/);
  assert.equal(payment.includes("redirectTo({ url: `/pages/orders/detail"), false);
  assert.match(orders, /orderNavigationIntent\.consume/);
  assert.match(orders, /navigateTo\(\{ url: `\/pages\/orders\/detail\?id=/);
});

test("grading detail offers navigation without promoting refund", () => {
  const detail = read("services/detail-actions.js");

  assert.match(detail, /primaryLabel: "继续提交"/);
  assert.match(detail, /secondaryLabel: "返回首页"/);
  assert.match(detail, /if \(grading\)/);
  assert.ok(detail.indexOf("if (grading)") < detail.indexOf("if (newestRound)"));
});

test("the upload guard is applied and released around the upload", () => {
  const options = read("pages/create/options.js");
  assert.match(options, /applyGuard\(\)/);
  assert.match(options, /releaseGuard\(\)/);
  assert.match(options, /enableAlertBeforeUnload/);
  // Released in a finally block, or a failed upload would trap the user.
  assert.match(options, /finally\s*\{[\s\S]*releaseGuard/);
});

test("every page declared in app.json exists with all its files", () => {
  const app = JSON.parse(read("app.json"));
  for (const page of app.pages) {
    for (const extension of ["js", "wxml", "json"]) {
      assert.ok(
        fs.existsSync(path.join(root, `${page}.${extension}`)),
        `missing ${page}.${extension}`,
      );
    }
  }
  for (const tab of app.tabBar.list) {
    assert.ok(app.pages.includes(tab.pagePath), `${tab.pagePath} missing from pages`);
  }
});

test("every component referenced by a page exists", () => {
  for (const file of sourceFiles.filter(f => f.endsWith(".json"))) {
    let config;
    try {
      config = JSON.parse(read(file));
    } catch (error) {
      assert.fail(`${file} is not valid JSON: ${error.message}`);
    }
    for (const [name, target] of Object.entries(config.usingComponents || {})) {
      const resolved = path.resolve(path.dirname(path.join(root, file)), `${target}.js`);
      assert.ok(fs.existsSync(resolved), `${file}: component ${name} -> ${target} missing`);
    }
  }
});

test("node_modules and private config are ignored", () => {
  const ignore = read(".gitignore");
  assert.match(ignore, /node_modules/);
  assert.match(ignore, /project\.private\.config\.json/);
});

test("no PDF, database or env file is committed under miniapp", () => {
  for (const file of allFiles) {
    assert.equal(/\.(pdf|sqlite3|db|env|key|pem)$/i.test(file), false, `unexpected file ${file}`);
  }
});

test("creation pages share a side-effect-free draft store", () => {
  const store = read("services/create-draft.js");
  assert.equal(/\bPage\s*\(/.test(store), false);
  assert.equal(/\bgetApp\s*\(/.test(store), false);
  assert.equal(/\bwx\./.test(store), false);

  for (const file of ["pages/create/options.js", "pages/create/payment.js"]) {
    const source = read(file);
    assert.match(source, /services\/create-draft\.js/);
    assert.equal(source.includes('from "./upload.js"'), false);
  }
});

test("user-facing pages use the product brand and hide technical vocabulary", () => {
  const pages = sourceFiles.filter(file => file.endsWith(".wxml") && file.startsWith("pages"));
  for (const file of pages) {
    const source = read(file);
    for (const forbidden of ["AI 批改", "Codex", "Mac", "staging", "production", "运行环境"]) {
      assert.equal(source.includes(forbidden), false, `${file} exposes ${forbidden}`);
    }
  }
  assert.match(read("pages/home/index.wxml"), /数学竞赛题批改/);
});

test("Soft Geometry assets stay below the 100KB visual budget", () => {
  const files = walk("assets");
  const bytes = files.reduce((sum, file) => sum + fs.statSync(path.join(root, file)).size, 0);
  assert.ok(bytes <= 100 * 1024, `visual assets use ${bytes} bytes`);
});

test("the geometry proof artwork is an optimized transparent 640px PNG", () => {
  const file = path.join(root, "assets/geometry-proof.png");
  const png = fs.readFileSync(file);
  const signature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

  assert.equal(png.subarray(0, 8).equals(signature), true);
  assert.equal(png.readUInt32BE(16), 640);
  assert.equal(png.readUInt32BE(20), 640);
  assert.ok(png.length <= 45 * 1024, `geometry proof artwork uses ${png.length} bytes`);
  assert.ok(png.includes(Buffer.from("tRNS")) || [4, 6].includes(png[25]), "geometry proof artwork must retain transparency");
});

test("all interactive shared components enforce disabled state in their handlers", () => {
  const sticky = read("components/sticky-action-bar/index.js");
  const picker = read("components/pdf-picker/index.js");
  const standard = read("components/standard-selector/index.js");
  assert.match(sticky, /this\.data\.disabled\s*\|\|\s*this\.data\.busy/);
  assert.match(picker, /this\.data\.disabled\s*\|\|\s*this\.data\.uploading/);
  assert.match(standard, /if \(this\.data\.disabled\)/);
});

test("custom tap targets use the shared scale feedback and never trigger haptics", () => {
  for (const file of sourceFiles) {
    const source = read(file);
    assert.equal(source.includes("vibrateShort"), false, `${file} must not trigger device vibration`);
  }

  for (const file of sourceFiles.filter(file => file.endsWith(".wxml"))) {
    const source = read(file);
    const interactiveViews = source.match(/<view\b[^>]*(?:bindtap|catchtap)=[^>]*>/gs) || [];
    for (const tag of interactiveViews) {
      assert.match(tag, /tap-feedback/, `${file} tap target is missing the shared feedback class`);
      assert.match(tag, /hover-class=/, `${file} tap target is missing a hover state`);
      assert.match(tag, /hover-start-time="0"/, `${file} tap target must react immediately`);
      assert.match(tag, /hover-stay-time="100"/, `${file} tap target must recover quickly`);
    }
  }

  const styles = read("app.wxss");
  assert.match(styles, /--motion-fast:\s*160ms/);
  assert.match(styles, /\.tap-feedback--active\s*\{[^}]*scale\(0\.985\)/s);
  assert.match(styles, /\.btn\.tap-feedback--active\s*\{[^}]*scale\(0\.98\)/s);
});

test("disabled shared controls neither dispatch nor animate", () => {
  const stickyMarkup = read("components/sticky-action-bar/index.wxml");
  const pickerMarkup = read("components/pdf-picker/index.wxml");
  const navStyles = read("components/navigation-bar/index.wxss");
  const orderStyles = read("components/order-row/index.wxss");

  assert.match(stickyMarkup, /\{\{busy \? 'action-bar__button--disabled' : 'tap-feedback'\}\}/);
  assert.match(stickyMarkup, /hover-class="\{\{busy \? 'none' : 'tap-feedback--active'\}\}"/);
  assert.match(stickyMarkup, /\{\{disabled \|\| busy \? 'action-bar__button--disabled' : 'tap-feedback'\}\}/);
  assert.match(pickerMarkup, /hover-class="\{\{disabled \|\| uploading \? 'none' : 'tap-feedback--active'\}\}"/);
  assert.equal(navStyles.includes("scale(0.96)"), false);
  assert.match(navStyles, /\.nav__account\.tap-feedback--active\s*\{[^}]*scale\(0\.985\)/s);
  assert.match(orderStyles, /\.order-row\.tap-feedback--active\s*\{[^}]*scale\(0\.985\)/s);
});

test("structured content uses one rounded group surface and content inset", () => {
  const styles = read("app.wxss");
  assert.match(styles, /\.list-surface\s*\{[^}]*border-radius:\s*var\(--radius-group\)/s);
  assert.match(styles, /\.list-surface\s*\{[^}]*padding:\s*0 var\(--space-group\)/s);

  const expectedGroups = {
    "pages/home/index.wxml": ["process list-surface", "standard-strip list-surface", "recent list-surface"],
    "pages/create/options.wxml": ["files-brief list-surface"],
    "pages/create/payment.wxml": ["confirm-list list-surface"],
    "pages/orders/index.wxml": ["orders-list list-surface"],
    "pages/orders/detail.wxml": ["timeline list-surface", "detail-info list-surface", "downloads list-surface"],
    "pages/account/index.wxml": ["settings list-surface"],
  };
  for (const [file, classNames] of Object.entries(expectedGroups)) {
    const source = read(file);
    for (const className of classNames) {
      assert.ok(source.includes(className), `${file} must use ${className}`);
    }
  }

  const homeStyles = read("pages/home/index.wxss");
  assert.equal(/\.recent\s*\{[^}]*padding:\s*20rpx 0/s.test(homeStyles), false);
});

test("custom navigation respects the status bar and capsule content row", () => {
  const markup = read("components/navigation-bar/index.wxml");
  const styles = read("components/navigation-bar/index.wxss");

  assert.match(markup, /height:\{\{totalHeight\}\}px/);
  assert.equal(markup.includes("height:{{navigationHeight}}px"), false);
  assert.match(styles, /\.nav__title\s*\{[^}]*flex:\s*1/s);
  assert.match(styles, /\.nav__account\s*\{[^}]*margin-left:\s*auto/s);
  assert.equal(/\.nav__account\s*\{[^}]*(?:position:\s*absolute|bottom:)/s.test(styles), false);
});

test("screen rhythm and the refined home statement stay intentional", () => {
  const globalStyles = read("app.wxss");
  const steps = read("components/step-header/index.wxss");
  const home = read("pages/home/index.wxml");
  const homeStyles = read("pages/home/index.wxss");

  assert.match(globalStyles, /--space-screen-top:\s*28rpx/);
  assert.match(globalStyles, /--space-section:\s*48rpx/);
  assert.match(steps, /padding:\s*32rpx 8rpx 52rpx/);
  assert.match(home, /intro__title">欢迎回来<\/view>/);
  assert.match(home, /intro__sub">提交答卷，查看报告<\/view>/);
  assert.match(home, /intro__promise">逐页细看，评得有据<\/view>/);
  assert.match(homeStyles, /\.intro__copy\s*\{[^}]*flex:\s*0 0 44%/s);
  assert.match(homeStyles, /\.intro__visual\s*\{[^}]*flex:\s*0 0 56%/s);
  for (const className of ["intro__title", "intro__sub", "intro__promise"]) {
    assert.match(homeStyles, new RegExp(`\\.${className}\\s*\\{[^}]*white-space:\\s*nowrap`, "s"));
  }
  assert.match(homeStyles, /\.intro__geometry\s*\{[^}]*width:\s*360rpx[^}]*height:\s*360rpx[^}]*opacity:\s*1/s);
  assert.match(homeStyles, /\.current__geometry\s*\{[^}]*opacity:\s*0\.24/s);
  assert.match(read("pages/orders/index.wxss"), /\.orders-empty__geometry image\s*\{[^}]*opacity:\s*0\.7/s);
});

test("the home guidance remains visible after a user has order history", () => {
  const home = read("pages/home/index.wxml");

  assert.equal(home.includes('wx:if="{{isNewUser}}"'), false);
  assert.match(home, /wx:if="\{\{!isNewUser\}\}"/);
  assert.equal((home.match(/一份答卷，三步完成/g) || []).length, 1);
  assert.equal((home.match(/class="process list-surface"/g) || []).length, 1);
  assert.equal((home.match(/class="standard-strip list-surface"/g) || []).length, 1);
  assert.equal((home.match(/bindtap="goCreate"/g) || []).length, 1);
});

test("the home hero owns the single accessible account entry", () => {
  const home = read("pages/home/index.wxml");

  assert.match(home, /<navigation-bar title="数学竞赛题批改"\s*\/>/);
  assert.equal(home.includes("showAccount"), false);
  assert.equal(home.includes("home-heading"), false);
  assert.equal((home.match(/bindtap="goAccount"/g) || []).length, 1);
  assert.match(home, /intro__account tap-feedback/);
  assert.match(home, /role="button"/);
  assert.match(home, /aria-label="查看账户"/);
  assert.match(home, /\/assets\/geometry-proof\.png/);
});
