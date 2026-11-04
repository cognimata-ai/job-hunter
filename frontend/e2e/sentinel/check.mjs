// `npm --prefix frontend run e2e:sentinel` (spec §16 L5486, Ruling F4-a): no retained or uploaded E2E artefact
// holds a credential. It serves a login page and a stub of the session API on 127.0.0.1, invents a password, a
// session cookie the login sets and one in a saved session, runs `artefacts.sentinel.ts` (which signs in with
// them and fails on purpose) under the E2E configuration with `CI=1`, as e2e.yml runs it, and scans what the run
// left in its test output and HTML report, the directories e2e.yml uploads after a failure: every file, every
// zip entry (a trace) and the HTML report's embedded data. It exits 1 naming each file that holds a sentinel
// (never the value), and also when the scan cannot be trusted: no failure screenshot, or a harmless canary the
// spec put in a step title and an attachment that the scan does not find. The run's directory is removed when
// the check passes and kept, holding invented values only, when it fails. No browser installs, no stack.
import { Buffer } from "node:buffer";
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import process from "node:process";
import { URL, fileURLToPath } from "node:url";
import { inflateRawSync } from "node:zlib";
import en from "../../src/i18n/en.json" with { type: "json" };

const FRONTEND = fileURLToPath(new URL("../..", import.meta.url));
const CLI = fileURLToPath(new URL("../../node_modules/@playwright/test/cli.js", import.meta.url));
const LOGIN_COOKIE = "jh_sentinel_session";
const STATE_COOKIE = "jh_sentinel_saved";

const invented = () => randomBytes(16).toString("hex");
const SECRETS = { password: invented(), "login cookie": invented(), "saved session cookie": invented() };
const CANARY = `canary-${invented()}`;

const escapeHtml = (text) => text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const LOGIN_PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Sentinel</title></head><body>
<form id="login"><label for="u">${escapeHtml(en["login.username"])}</label><input id="u" name="username" autocomplete="username">
<label for="p">${escapeHtml(en["login.password"])}</label><input id="p" name="password" type="password" autocomplete="current-password">
<button type="submit">${escapeHtml(en["login.submit"])}</button></form>
<p>${CANARY}</p>
<script>
document.getElementById("login").addEventListener("submit", (event) => {
  event.preventDefault();
  const body = JSON.stringify({ username: document.getElementById("u").value, password: document.getElementById("p").value });
  void fetch("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json", "X-JH-Request": "1" }, body });
});
</script></body></html>`;

/** The stub: the login page, a login that checks the password and sets the session cookie, and the session. */
function answer(request, response, body) {
  const path = new URL(request.url ?? "/", "http://127.0.0.1").pathname;
  if (request.method === "GET" && path === "/login") {
    response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" }).end(LOGIN_PAGE);
  } else if (request.method === "POST" && path === "/api/auth/login") {
    let sent = {};
    try {
      sent = JSON.parse(body);
    } catch {
      // not JSON: refused below
    }
    if (sent.password === SECRETS.password) {
      const cookie = `${LOGIN_COOKIE}=${SECRETS["login cookie"]}; Path=/; HttpOnly; SameSite=Strict`;
      response.writeHead(204, { "Set-Cookie": cookie }).end();
    } else {
      response.writeHead(401).end();
    }
  } else if (request.method === "GET" && path === "/api/auth/session") {
    const signedIn = (request.headers.cookie ?? "").includes(`${LOGIN_COOKIE}=${SECRETS["login cookie"]}`);
    response.writeHead(signedIn ? 200 : 401).end();
  } else {
    response.writeHead(404).end();
  }
}

const server = createServer((request, response) => {
  const chunks = [];
  request.on("data", (chunk) => chunks.push(chunk));
  request.on("end", () => { answer(request, response, Buffer.concat(chunks).toString("utf8")); });
});
await new Promise((resolve) => { server.listen(0, "127.0.0.1", resolve); });
const origin = `http://127.0.0.1:${String(server.address().port)}`;

const out = mkdtempSync(join(tmpdir(), "jh-e2e-sentinel-"));
const results = join(out, "test-results");
const report = join(out, "playwright-report");
const state = join(out, "state.json");
writeFileSync(state, JSON.stringify({
  cookies: [{ name: STATE_COOKIE, value: SECRETS["saved session cookie"], domain: "127.0.0.1", path: "/", expires: -1, httpOnly: true, secure: false, sameSite: "Strict" }],
  origins: [],
}));

const ran = await new Promise((resolve) => {
  const child = spawn(process.execPath, [CLI, "test", "-c", "e2e/sentinel/sentinel.config.ts", "--output", results], {
    cwd: FRONTEND,
    env: {
      ...process.env,
      CI: "1",
      PLAYWRIGHT_HTML_OUTPUT_DIR: report,
      JH_SENTINEL_ORIGIN: origin,
      JH_SENTINEL_PASSWORD: SECRETS.password,
      JH_SENTINEL_STATE: state,
      JH_SENTINEL_CANARY: CANARY,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  child.stdout.on("data", (chunk) => { output += chunk; });
  child.stderr.on("data", (chunk) => { output += chunk; });
  child.on("close", (code) => { resolve({ code, output }); });
});
server.close();

/** The entries of a zip archive (stored or deflated), by its central directory. */
function zipEntries(buffer) {
  const end = buffer.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
  if (end < 0) {
    return [];
  }
  const count = buffer.readUInt16LE(end + 10);
  let at = buffer.readUInt32LE(end + 16);
  const entries = [];
  for (let i = 0; i < count && buffer.readUInt32LE(at) === 0x02014b50; i++) {
    const method = buffer.readUInt16LE(at + 10);
    const size = buffer.readUInt32LE(at + 20);
    const nameLength = buffer.readUInt16LE(at + 28);
    const skip = nameLength + buffer.readUInt16LE(at + 30) + buffer.readUInt16LE(at + 32);
    const local = buffer.readUInt32LE(at + 42);
    const name = buffer.toString("utf8", at + 46, at + 46 + nameLength);
    const start = local + 30 + buffer.readUInt16LE(local + 26) + buffer.readUInt16LE(local + 28);
    const raw = buffer.subarray(start, start + size);
    entries.push({ name, data: method === 8 ? inflateRawSync(raw) : raw });
    at += 46 + skip;
  }
  return entries;
}

const EMBEDDED = /data:application\/zip;base64,([A-Za-z0-9+/=]+)/g;
const needles = Object.entries(SECRETS).flatMap(([what, value]) => [
  [what, Buffer.from(value)],
  [what, Buffer.from(Buffer.from(value).toString("base64").replace(/=+$/, ""))],
]);
const found = new Set();
let canaryInReport = false;
let canaryInResults = false;

/** Every sentinel in `data`, the bytes of `where`, and in every archive inside it. */
function scan(where, data, top) {
  for (const [what, needle] of needles) {
    if (data.includes(needle)) {
      found.add(`${where}: the ${what}`);
    }
  }
  if (data.includes(CANARY)) {
    canaryInReport ||= top === report && where.includes("!");
    canaryInResults ||= top === results;
  }
  if (data.subarray(0, 4).equals(Buffer.from([0x50, 0x4b, 0x03, 0x04]))) {
    for (const entry of zipEntries(data)) {
      scan(`${where}!${entry.name}`, entry.data, top);
    }
  }
  if (where.endsWith(".html")) {
    for (const [, base64] of data.toString("latin1").matchAll(EMBEDDED)) {
      scan(`${where}!embedded`, Buffer.from(base64, "base64"), top);
    }
  }
}

function files(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

const problems = [];
if (ran.code !== 1) {
  problems.push(`the sentinel run exited ${String(ran.code)}, not 1 (its spec fails on purpose):\n${ran.output.slice(-2000)}`);
} else if (!ran.output.includes("the sentinel spec fails on purpose")) {
  problems.push(`the sentinel spec failed before its deliberate failure:\n${ran.output.slice(-2000)}`);
}
const scanned = [results, report].flatMap((top) => {
  try {
    return files(top).map((path) => ({ top, path }));
  } catch {
    problems.push(`no ${relative(out, top)} directory`);
    return [];
  }
});
for (const { top, path } of scanned) {
  scan(relative(out, path), readFileSync(path), top);
}
if (!scanned.some(({ path }) => path.endsWith(".png"))) {
  problems.push("no failure screenshot: the run kept no failure artefact to scan");
}
if (!canaryInReport || !canaryInResults) {
  problems.push(`the canary was not found (report ${String(canaryInReport)}, test output ${String(canaryInResults)}): the scan reaches too little`);
}
for (const hit of [...found].sort()) {
  problems.push(`a credential survives in ${hit}`);
}
const kinds = [...new Set(scanned.map(({ path }) => path.split(".").pop()))].sort();
if (problems.length > 0) {
  process.stderr.write(`${problems.join("\n")}\ne2e:sentinel: FAILED; the run's files (invented values only) are kept in ${out}\n`);
  process.exit(1);
}
rmSync(out, { recursive: true, force: true });
process.stdout.write(`e2e:sentinel: ${String(scanned.length)} artefact files (${kinds.join(", ")}) hold no credential\n`);
