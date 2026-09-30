"use strict";

// Isolated local browser regression: no Telegram calls, no external requests.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const root = path.resolve(__dirname, "..");
const checks = [];
function check(value, label) { assert(value, label); checks.push(label); }

function fixture(index, extra = {}) {
  return { id: String(index), protocol: "vless", name: `Server ${index}`,
    address: "8.8.8.8", port: 443, status: "online", valid: true, secure: true,
    country_name: "United States", transport: "tcp", latency: index,
    raw: `vless://id-${index}@8.8.8.8:443?security=tls#label`, ...extra };
}

async function main() {
  const server = http.createServer(async (request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    const relative = decodeURIComponent(pathname === "/" ? "index.html" : pathname.slice(1));
    const filename = path.resolve(root, relative);
    if (!filename.startsWith(root + path.sep)) { response.writeHead(403).end(); return; }
    try {
      const content = await fs.readFile(filename);
      const mime = { ".html": "text/html", ".css": "text/css", ".js": "application/javascript",
        ".json": "application/json", ".png": "image/png" }[path.extname(filename)] || "text/plain";
      response.writeHead(200, { "Content-Type": mime }); response.end(content);
    } catch { response.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({ headless: true,
      ...(process.env.BROUTE_BROWSER_EXECUTABLE ? { executablePath: process.env.BROUTE_BROWSER_EXECUTABLE } : {}) });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.route("https://**/*", route => route.abort());
    const fixtures = Array.from({ length: 450 }, (_, i) => fixture(i));
    fixtures[0].name = '<img src=x onerror="window.injected=true">';
    fixtures.push(fixture("unknown", { status: "unknown", latency: null }),
      fixture("invalid", { valid: false }), fixture("removed", { should_remove: true }),
      fixture("unavailable", { source_unavailable: true }));
    await context.route("**/data/servers.json", route => route.fulfill({ json: fixtures }));
    await context.addInitScript(() => {
      Object.defineProperty(navigator, "clipboard", { configurable: true,
        value: { writeText: async value => { window.copiedText = value; } } });
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => document.querySelector("#filter-protocol").options.length > 1);
    check(await page.locator(".server-card").count() === 0, "initial results remain hidden until a filter/search");
    await page.selectOption("#filter-status", "online");
    check(await page.locator(".server-card").count() === 200, "online results initially show 200 cards");
    check((await page.locator("#server-results-count").textContent()).includes("450"), "removed/invalid/unavailable records excluded");
    check(await page.locator("#server-list img").count() === 0, "server names escape HTML");
    check((await page.locator(".server-card").first().textContent()).includes("0ms"), "zero latency is visible");
    await page.locator(".server-card [data-copy]").first().click();
    check(await page.evaluate(() => window.copiedText) === fixtures[0].raw, "copied config preserves exact bytes");
    await page.click("#load-more-btn");
    check(await page.locator(".server-card").count() === 400, "load more reaches 400 results");
    await page.click("#load-more-btn");
    check(await page.locator(".server-card").count() === 450, "all results can be reached beyond old 200 limit");
    check(await page.locator("#load-more-btn").isHidden(), "load more hides after the final page");
    await page.selectOption("#filter-status", "unknown");
    check(await page.locator(".status-unknown").count() === 1, "unknown status has a neutral marker");
    await page.selectOption("#filter-status", "");
    check(await page.locator(".server-card").count() === 0, "clearing all filters restores the original empty state");
    await page.click("#copy-sub-btn");
    check((await page.evaluate(() => window.copiedText)).endsWith("/data/sub.txt"), "subscription copy works");
    await page.evaluate(() => {
      Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
      document.execCommand = action => {
        window.fallbackCopy = action === "copy" && document.querySelector("textarea").value;
        return true;
      };
    });
    await page.click("#copy-sub-btn");
    check((await page.evaluate(() => window.fallbackCopy)).endsWith("/data/sub.txt"), "missing clipboard API uses the fallback");
    check(await page.locator("textarea").count() === 0, "clipboard fallback cleans up its input");
    await page.click("#show-qr-btn");
    check((await page.locator("#toast").textContent()).includes("QR Code"), "unavailable QR dependency fails gracefully");
    check(await page.locator("#toast").count() === 1, "toast ID is unique");
    await page.click('[data-platform="android"]');
    check(await page.locator("#platform-apps-modal").isVisible(), "platform selection opens the apps modal");
    await page.locator(".platform-app-item").first().click();
    check(await page.locator("#app-detail-modal").isVisible(), "app details open");
    await page.keyboard.press("Escape");
    check(await page.locator("#app-detail-modal").isHidden(), "Escape closes dialogs");
    check(await page.locator('[data-platform="android"]').evaluate(el => el === document.activeElement), "dialog close restores focus");
    await page.click('[data-platform="android"]');
    await page.keyboard.press("Shift+Tab");
    check(await page.locator(".platform-app-item").last().evaluate(el => el === document.activeElement), "dialog focus remains trapped for Shift+Tab");
    await page.keyboard.press("Escape");
    check(await page.locator(".hero-title-box").evaluate(el => {
      const box = el.getBoundingClientRect(), text = el.querySelector("#typing-text").getBoundingClientRect();
      return text.left >= box.left - 1 && text.right <= box.right + 1;
    }), "desktop heading stays inside its box");
    const screenshotDir = process.env.BROUTE_SCREENSHOT_DIR;
    if (screenshotDir) {
      await fs.mkdir(screenshotDir, { recursive: true });
      await page.screenshot({ path: path.join(screenshotDir, "desktop.png") });
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.selectOption("#filter-status", "online");
    check(await page.locator(".server-card").count() === 200, "mobile filtering works");
    if (screenshotDir) await page.screenshot({ path: path.join(screenshotDir, "mobile.png") });
    const overflow = await page.evaluate(() => ({ width: innerWidth,
      html: document.documentElement.scrollWidth, body: document.body.scrollWidth, x: scrollX,
      bodyWidth: document.body.getBoundingClientRect().width,
      elements: [...document.querySelectorAll("body *")]
        .filter(el => { const box = el.getBoundingClientRect(); return !el.matches('.bg-blob, .stat-bg') && box.width && (box.left < -1 || box.right > innerWidth + 1); })
        .slice(0, 8).map(el => ({ tag: el.tagName, class: el.className,
          left: Math.round(el.getBoundingClientRect().left), right: Math.round(el.getBoundingClientRect().right) })) }));
    check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
      `mobile viewport has no horizontal overflow: ${JSON.stringify(overflow)}`);
    for (const width of [320, 768]) {
      await page.setViewportSize({ width, height: 844 });
      check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
        `layout has no horizontal overflow at ${width}px`);
    }
    check(errors.length === 0, `no browser JavaScript exceptions: ${errors.join(", ")}`);
    console.log(JSON.stringify({ status: "passed", browser_checks: checks.length, checks }, null, 2));
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
