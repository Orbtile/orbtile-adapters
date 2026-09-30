// Orbtile Adapter Protocol v1 — reference adapter in Node (>= 18, no dependencies).
// Transport: HTTP/1.1 over the Unix-domain socket named in the discovery file (http.request socketPath).
// A poller-style adapter: every 5 s it lists its jobs and sends the full set with
// PUT /sessions (full replace). Jobs that disappear are ended by Orbtile.
import { readFile } from "node:fs/promises";
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { request } from "node:http";
import { homedir } from "node:os";

const API_FILE = process.env.ORBTILE_API_FILE
  ?? `${homedir()}/Library/Application Support/Orbtile/adapter-api.json`;
const ID = "dev.example.node";
let api = null;

async function load() { api = JSON.parse(await readFile(API_FILE, "utf8")); }

// One HTTP exchange over the socket. No proxy is involved, so the token cannot leak to one.
function send(method, path, body, headers) {
  return new Promise((resolve, reject) => {
    const data = method === "GET" ? undefined : JSON.stringify(body ?? {});
    const req = request({
      socketPath: api.socketPath,
      path: api.basePath + path,
      method,
      headers: {
        host: "localhost",
        authorization: `Bearer ${api.token}`,
        "content-type": "application/json",
        ...(data === undefined ? {} : { "content-length": Buffer.byteLength(data) }),
        ...headers,
      },
      timeout: 2000,
    }, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, text: Buffer.concat(chunks).toString("utf8") }));
    });
    req.on("timeout", () => req.destroy(new Error("timeout")));
    req.on("error", reject);
    req.end(data);
  });
}

// `retry` lists the recoveries still allowed. After an Orbtile relaunch one call needs both: 401 (new token), then
// 404 adapter_not_registered. A full replace carries every session, so nothing else has to be resent.
async function call(method, path, body, headers = {}, retry = ["401", "404"]) {
  const r = await send(method, path, body, headers);
  let json = {};
  try { json = r.text ? JSON.parse(r.text) : {}; } catch { json = {}; }
  if (r.status === 401 && retry.includes("401")) {
    await load();
    return call(method, path, body, headers, retry.filter((x) => x !== "401"));
  }
  if (json.error?.code === "adapter_not_registered" && retry.includes("404")) {
    await register();
    return call(method, path, body, headers, []);
  }
  if (r.status === 429) await new Promise((s) => setTimeout(s, 1000 * Number(r.headers["retry-after"] ?? 1)));
  return { status: r.status, json };
}

async function verify() {
  const nonce = randomBytes(16).toString("hex");
  const { status, json } = await call("GET", "/info", undefined, { "x-orbtile-nonce": nonce });
  const want = createHmac("sha256", api.token).update(nonce).digest();
  const got = Buffer.from(json.proof ?? "", "hex");
  if (status !== 200 || got.length !== want.length || !timingSafeEqual(got, want)) throw new Error("not Orbtile");
}

// Magenta working orb with an amber alarm colour (see protocol/PALETTE.md).
const register = () => call("PUT", `/adapters/${ID}`, {
  name: "Node jobs", shortName: "Jobs", palette: { baseHue: 320, attentionHue: 75 },
  glyph: { symbol: "gearshape.2", monogram: "Nj" }, version: "1.0.0",
}, {}, ["401"]);

// Replace with the real job source.
function listJobs() {
  return [{ id: "job-42", title: "nightly export", activity: "tool", toolLabel: "Uploading  3/7", ttlSeconds: 30 }];
}

await load(); await verify(); await register();
setInterval(() => call("PUT", `/adapters/${ID}/sessions`, { sessions: listJobs() }).catch(() => {}), 5000);
