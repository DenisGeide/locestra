import { chromium } from "playwright";
import crypto from "node:crypto";
import fs from "node:fs";

const [rawUrl, selector, expectedText, screenshotPath] = process.argv.slice(2);
if (!rawUrl || !selector || expectedText === undefined || !screenshotPath) {
  throw new Error("url, selector, expected text, and screenshot path are required");
}

function allowedTarget(raw) {
  const target = new URL(raw);
  const host = target.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (!["http:", "https:"].includes(target.protocol) || target.username || target.password) return false;
  if (!["127.0.0.1", "::1", "localhost"].includes(host)) return false;
  const port = Number(target.port || (target.protocol === "https:" ? 443 : 80));
  return Number.isInteger(port) && port >= 1024 && port <= 65535;
}

if (!allowedTarget(rawUrl)) throw new Error("coding UI verification permits loopback high ports only");
const origin = new URL(rawUrl).origin;
const websocketOrigin = (() => {
  const target = new URL(origin);
  target.protocol = target.protocol === "https:" ? "wss:" : "ws:";
  return target.origin;
})();

function allowedWebSocketTarget(raw) {
  try {
    const target = new URL(raw);
    return (
      ["ws:", "wss:"].includes(target.protocol) &&
      !target.username &&
      !target.password &&
      target.origin === websocketOrigin
    );
  } catch {
    return false;
  }
}

const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ serviceWorkers: "block" });
  const page = await context.newPage();
  await page.route("**/*", async (route) => {
    const requestUrl = route.request().url();
    if (requestUrl.startsWith("data:") || requestUrl.startsWith("blob:")) {
      await route.continue();
      return;
    }
    try {
      const target = new URL(requestUrl);
      if (allowedTarget(requestUrl) && target.origin === origin) await route.continue();
      else await route.abort("blockedbyclient");
    } catch {
      await route.abort("blockedbyclient");
    }
  });
  await page.routeWebSocket("**/*", async (route) => {
    if (allowedWebSocketTarget(route.url())) {
      route.connectToServer();
      return;
    }
    await route.close({ code: 1008, reason: "Blocked by coding UI policy" });
  });
  const response = await page.goto(rawUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
  if (!response || !response.ok()) throw new Error("UI target returned a non-success response");
  const locator = page.locator(selector).first();
  await locator.waitFor({ state: "visible", timeout: 30000 });
  const actualText = (await locator.innerText()).trim();
  if (!actualText.includes(expectedText)) throw new Error("UI expected text was not found");
  await page.screenshot({ path: screenshotPath, fullPage: true });
  const visibleUrl = new URL(page.url());
  const digest = (value) => crypto.createHash("sha256").update(value, "utf8").digest("hex");
  const title = await page.title();
  const result = {
    url: `${visibleUrl.origin}${visibleUrl.pathname}`,
    titleHash: digest(title),
    titleLength: title.length,
    selectorHash: digest(selector),
    expectedTextHash: digest(expectedText),
    actualTextHash: digest(actualText),
    actualTextLength: actualText.length,
    matched: true,
    screenshotBytes: fs.statSync(screenshotPath).size,
  };
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
