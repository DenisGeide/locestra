import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import http from "node:http";

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";

const ROOT = new URL("../", import.meta.url);
const readJson = async (relative) =>
  JSON.parse(await readFile(new URL(relative, ROOT), "utf8"));

const project = await readJson("package.json");
const lock = await readJson("package-lock.json");
const sdk = await readJson("node_modules/@modelcontextprotocol/sdk/package.json");
const hono = await readJson("node_modules/@hono/node-server/package.json");

assert.equal(project.overrides["@hono/node-server"], "2.0.11");
assert.equal(lock.packages["node_modules/@hono/node-server"].version, "2.0.11");
assert.equal(hono.version, "2.0.11");
assert.equal(sdk.version, "1.29.0");
assert.equal(sdk.dependencies["@hono/node-server"], "^1.19.9");
assert.ok(
  Number.parseInt(process.versions.node.split(".", 1)[0], 10) >= 20,
  "The audited Hono adapter requires Node.js 20 or newer.",
);

const transport = new StreamableHTTPServerTransport({
  sessionIdGenerator: undefined,
});
const mcp = new Server(
  { name: "locestra-mcp-compat", version: "1.0.0" },
  { capabilities: {} },
);
const server = http.createServer((request, response) => {
  transport.handleRequest(request, response).catch(() => {
    if (!response.headersSent) {
      response.statusCode = 500;
    }
    response.end();
  });
});

try {
  await mcp.connect(transport);
  await new Promise((resolve, reject) => {
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      5_000,
    );
    controller.signal.addEventListener(
      "abort",
      () => reject(new Error("Loopback MCP compatibility listener timed out.")),
      { once: true },
    );
    server.once("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    server.listen(
      { port: 0, host: "127.0.0.1", signal: controller.signal },
      () => {
        clearTimeout(timeout);
        resolve();
      },
    );
  });

  const address = server.address();
  assert.ok(address && typeof address === "object");
  const response = await fetch(`http://127.0.0.1:${address.port}/mcp`, {
    method: "POST",
    headers: {
      accept: "application/json, text/event-stream",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-03-26",
        capabilities: {},
        clientInfo: { name: "locestra-compat-check", version: "1.0.0" },
      },
    }),
    signal: AbortSignal.timeout(5_000),
  });
  const body = await response.text();
  assert.equal(response.status, 200);
  assert.match(body, /"name":"locestra-mcp-compat"/);
  assert.match(body, /"protocolVersion":"2025-03-26"/);
} finally {
  await mcp.close().catch(() => {});
  if (server.listening) {
    await new Promise((resolve) => server.close(resolve));
  }
}

console.log("MCP_NODE_COMPAT_OK");
