import { chromium } from "playwright";
import dns from "node:dns/promises";
import net from "node:net";

const url = process.argv[2];
if (!url) throw new Error("URL is required");

function isPublicIPv4(address) {
  const octets = address.split(".").map(Number);
  if (octets.length !== 4 || octets.some((item) => !Number.isInteger(item) || item < 0 || item > 255)) return false;
  const [a, b] = octets;
  if (a === 0 || a === 10 || a === 127 || a >= 224) return false;
  if (a === 169 && b === 254) return false;
  if (a === 172 && b >= 16 && b <= 31) return false;
  if (a === 192 && (b === 168 || b === 0)) return false;
  if (a === 100 && b >= 64 && b <= 127) return false;
  if (a === 198 && (b === 18 || b === 19 || b === 51)) return false;
  if (a === 203 && b === 0) return false;
  return true;
}

function isPublicAddress(address) {
  const kind = net.isIP(address);
  if (kind === 4) return isPublicIPv4(address);
  if (kind !== 6) return false;
  const lowered = address.toLowerCase();
  if (lowered.startsWith("::ffff:")) return isPublicIPv4(lowered.slice(7));
  return !(
    lowered === "::" || lowered === "::1" || lowered.startsWith("fc") || lowered.startsWith("fd") ||
    /^fe[89ab]/.test(lowered) || lowered.startsWith("ff") || lowered.startsWith("2001:db8") ||
    lowered.startsWith("2001:10")
  );
}

const checkedHosts = new Map();
async function requirePublicUrl(rawUrl) {
  const target = new URL(rawUrl);
  if (!["http:", "https:"].includes(target.protocol) || target.username || target.password) {
    throw new Error("network target denied by browser policy");
  }
  const hostname = target.hostname.toLowerCase().replace(/\.$/, "");
  if (hostname === "localhost" || hostname.endsWith(".localhost") || hostname.endsWith(".local") || hostname.endsWith(".internal")) {
    throw new Error("network target denied by browser policy");
  }
  if (!checkedHosts.has(hostname)) {
    checkedHosts.set(hostname, (async () => {
      const addresses = net.isIP(hostname) ? [{ address: hostname }] : await dns.lookup(hostname, { all: true, verbatim: true });
      if (!addresses.length || addresses.some(({ address }) => !isPublicAddress(address))) {
        throw new Error("network target denied by browser policy");
      }
      return true;
    })());
  }
  await checkedHosts.get(hostname);
}

await requirePublicUrl(url);
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  await page.route("**/*", async (route) => {
    const requestUrl = route.request().url();
    if (!requestUrl.startsWith("http://") && !requestUrl.startsWith("https://")) {
      await route.continue();
      return;
    }
    try {
      await requirePublicUrl(requestUrl);
      await route.continue();
    } catch {
      await route.abort("blockedbyclient");
    }
  });
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 });
  const result = {
    url: page.url(),
    title: await page.title(),
    text: (await page.locator("body").innerText()).slice(0, 12000),
  };
  console.log(JSON.stringify(result, null, 2));
} finally {
  await browser.close();
}
