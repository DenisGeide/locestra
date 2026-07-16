import { chromium } from "playwright";

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
await page.setContent("<title>PLAYWRIGHT_OK</title><h1>Browser module</h1>");
const title = await page.title();
await browser.close();
if (title !== "PLAYWRIGHT_OK") process.exit(1);
console.log("PLAYWRIGHT_OK");
