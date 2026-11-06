import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { createHmac } from "node:crypto";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { AdapterClient, PayloadTooLargeError } from "../index.js";
async function fixture(t, options = {}) {
  const dir = await mkdtemp(join(tmpdir(), "orb-sdk-"));
  const windows = process.platform === "win32";
  const socket = windows ? null : join(dir, "s");
  const file = join(dir, "api.json");
  const calls = [];
  const state = {
    token: "secret",
    owner: "owner",
    registered: false,
    highest: 0,
    ack: 0,
    badProof: false,
    apiMinor: 1,
  };
  const server = http.createServer(async (req, res) => {
    let raw = "";
    for await (const c of req) raw += c;
    const body = raw ? JSON.parse(raw) : {};
    calls.push({ path: req.url, body, headers: req.headers });
    assert.equal(req.headers.host, "localhost");

    assert.equal(req.headers["content-type"], "application/json");
    assert.equal(req.headers.origin, undefined);
    const reply = (status, data) => {
      res.writeHead(status, {
        "Content-Type": "application/json",
        Connection: "close",
      });
      res.end(data ? JSON.stringify(data) : undefined);
    };
    if (req.headers.authorization !== `Bearer ${state.token}`)
      return reply(401, {
        error: { code: "unauthorized", message: state.token },
      });
    if (req.url.endsWith("/info"))
      return reply(200, {
        api: 1,
        apiMinor: state.apiMinor,
        proof: createHmac("sha256", state.badProof ? "wrong" : state.token)
          .update(req.headers["x-orbtile-nonce"])
          .digest("hex"),
      });
    if (req.method === "PUT" && req.url === "/a/v1/adapters/dev.test") {
      if (state.registered)
        assert.equal(req.headers["x-orbtile-adapter-token"], state.owner);
      state.registered = true;
      return reply(201, {
        enabled: true,
        adapter: { id: "dev.test" },
        adapterToken: state.owner,
        warnings: state.registrationWarnings,
      });
    }
    if (!state.registered)
      return reply(404, {
        error: { code: "adapter_not_registered", message: "gone" },
      });
    assert.equal(req.headers["x-orbtile-adapter-token"], state.owner);
    if (req.url.endsWith("/events")) {
      assert.ok(
        body.ack >= state.ack &&
          body.ack <= body.after &&
          body.after <= state.highest,
      );
      state.ack = body.ack;
      const events = state.events ?? [
        { sequence: 2, type: "tap", sessionId: "s", host: "local" },
      ];
      state.highest = Math.max(state.highest, ...events.map((e) => e.sequence));
      return reply(200, {
        events: events.filter((e) => e.sequence > body.after),
        lastSequence: 8,
        acknowledged: body.ack,
      });
    }
    if (req.url.endsWith("/heartbeat")) return reply(204);
    return reply(200, { session: { uid: "s", visible: true } });
  });
  await new Promise((r) => windows ? server.listen(0, "127.0.0.1", r) : server.listen(socket, r));
  const writeDiscovery = (token) => {
    const port = windows ? server.address().port : null;
    return writeFile(file, JSON.stringify({
      api: 1,
      ...(windows ? { url: `http://127.0.0.1:${port}`, port } : { socketPath: socket }),
      basePath: "/a/v1",
      token,
    }));
  };
  await writeDiscovery("secret");
  const c = new AdapterClient({
    adapterId: "dev.test",
    registration: {
      name: "Test",
      palette: { baseHue: 150 },
      capabilities: ["tap", "questions", "approvals"],
    },
    apiFile: file,
    ...options,
  });
  t.after(async () => {
    c.close();
    await new Promise((r) => server.close(r));
    await rm(dir, { recursive: true });
  });
  await c.connect();
  return {
    c,
    calls,
    async deliver(event) {
      state.events = [event];
      await c.pollEvents(0);
      return event;
    },
    state,
    async rotate() {
      state.token = "replacement";
      state.owner = "new-owner";
      state.registered = false;
      state.highest = 0;
      state.ack = 0;
      await writeDiscovery(state.token);
    },
  };
}
test("resume replays before ack and never acknowledges lastSequence gaps", async (t) => {
  const { c, calls } = await fixture(t);
  let r = await c.pollEvents(0);
  assert.equal(r.events[0].sequence, 2);
  await c.pollEvents(0);
  assert.equal(calls.at(-1).body.ack, 0);
  c.ack(2);
  await c.pollEvents(0);
  assert.deepEqual(calls.at(-1).body, { after: 2, ack: 2, waitSeconds: 0 });
  assert.throws(() => c.ack(8));
});
test("heartbeat automatically renews idle leases", async (t) => {
  const { calls } = await fixture(t, { heartbeatIntervalMs: 20 });
  await new Promise((r) => setTimeout(r, 65));
  assert.ok(calls.some((x) => x.path.endsWith("/heartbeat")));
});
test("oversize UTF8 body rejected before transport", async (t) => {
  const { c, calls } = await fixture(t);
  const count = calls.length;
  await assert.rejects(
    c.upsertSession("s", {
      title: "S",
      activity: "streaming",
      message: { id: "m", text: "🙂".repeat(20000) },
    }),
    PayloadTooLargeError,
  );
  assert.equal(calls.length, count);
});
test("question commit validates picks and failure leaves cursor replayable", async (t) => {
  const { c, deliver } = await fixture(t);
  await c.askQuestion(
    "s",
    {
      id: "q",
      questions: [{ text: "Pick", options: [{ label: "A" }, { label: "B" }] }],
    },
    { title: "S" },
  );
  const e = {
    sequence: 2,
    type: "question",
    host: "local",
    sessionId: "s",
    requestId: "q",
    picks: [[0]],
    expiresAt: Date.now() / 1000 + 30,
  };
  await deliver(e);
  await assert.rejects(
    c.applyEvent(e, () => {
      throw new Error("commit");
    }),
  );
  assert.equal(c.cursor, 0);
  let applied = 0;
  await c.applyEvent(e, () => {
    applied++;
  });
  assert.equal(applied, 1);
  await c.applyEvent(e, () => {
    applied++;
  });
  assert.equal(applied, 1);
});
test("tap does not retire a pending interactive prompt; UI answer wins delayed decision", async (t) => {
  const { c, deliver } = await fixture(t);
  await c.requestApproval(
    "s",
    { id: "a", toolName: "Shell", commandSummary: "Show status" },
    { title: "S" },
  );
  await c.applyEvent(
    await deliver({ sequence: 1, type: "tap", sessionId: "s", host: "local" }),
    () => {},
  );
  assert.equal(c.pending.size, 1);
  let decisions = 0;
  await c.answerInUI("s", "local", () => {
    decisions++;
  });
  await c.applyEvent(
    await deliver({
      sequence: 2,
      type: "approval",
      requestId: "a",
      sessionId: "s",
      host: "local",
      decision: "allow",
      expiresAt: Date.now() / 1000 + 10,
    }),
    () => {
      decisions++;
    },
  );
  assert.equal(decisions, 1);
});
test("expired and invalid option events deliberately discarded without applying", async (t) => {
  const { c, deliver } = await fixture(t);
  await c.askQuestion(
    "s",
    {
      id: "q",
      questions: [{ text: "Pick", options: [{ label: "A" }, { label: "B" }] }],
    },
    { title: "S" },
  );
  let commits = 0;
  for (const [sequence, picks, expiresAt] of [
    [1, [[0]], 0],
    [2, [[0, 1]], Date.now() / 1000 + 30],
    [3, [[2]], Date.now() / 1000 + 30],
  ]) {
    await c.applyEvent(
      await deliver({
        sequence,
        type: "question",
        host: "local",
        sessionId: "s",
        requestId: "q",
        picks,
        expiresAt,
      }),
      () => {
        commits++;
      },
    );
  }
  assert.equal(commits, 0);
  assert.equal(c.cursor, 3);
  assert.equal(c.pending.size, 1);
});
test("remote removal and full batch replacement use exact routes", async (t) => {
  const { c, calls } = await fixture(t);
  await c.upsertSession("s", { host: "remote", title: "S", activity: "idle" });
  await c.removeSession("s", "remote");
  assert.equal(
    calls.at(-1).path,
    "/a/v1/adapters/dev.test/sessions/s?host=remote",
  );
  await c.replaceSessions([{ id: "batch", title: "B", activity: "done" }]);
  assert.deepEqual(calls.at(-1).body, {
    sessions: [{ id: "batch", title: "B", activity: "done" }],
  });
});
test("token rotation restores full display snapshots without replaying old question", async (t) => {
  const { c, calls, rotate } = await fixture(t);
  await c.upsertSession("s", {
    title: "Full title",
    activity: "thinking",
    host: "remote",
    model: "Model",
    project: "Customer jobs",
  });
  await c.askQuestion(
    "s",
    {
      id: "old",
      questions: [{ text: "Pick", options: [{ label: "A" }, { label: "B" }] }],
    },
    { host: "remote" },
  );
  await rotate();
  await assert.rejects(c.heartbeat(), (e) => e.code === "registration_lost");
  assert.equal(c.cursor, 0);
  assert.equal(c.pending.size, 0);
  const restored = calls.filter((x) => x.path.endsWith("/sessions/s")).at(-1);
  assert.equal(restored.body.title, "Full title");
  assert.equal(restored.body.host, "remote");
  assert.equal(restored.body.model, "Model");
  assert.equal(restored.body.project, "Customer jobs");
  assert.equal(restored.body.question, undefined);
  assert.equal(restored.headers["x-orbtile-adapter-token"], "new-owner");
  await c.askQuestion(
    "s",
    {
      id: "fresh",
      questions: [{ text: "New", options: [{ label: "A" }, { label: "B" }] }],
    },
    { host: "remote" },
  );
  assert.equal(c.pending.size, 1);
});
test("missing registration transparently restores full display state before merge", async (t) => {
  const { c, calls, state } = await fixture(t);
  await c.upsertSession("s", { title: "Preserved", activity: "thinking" });
  state.registered = false;
  state.owner = "next-owner";
  await c.upsertSession("s", { activity: "done" });
  assert.equal(calls.at(-1).body.title, "Preserved");
  assert.equal(calls.at(-1).body.activity, "done");
  assert.equal(calls.at(-1).headers["x-orbtile-adapter-token"], "next-owner");
});
test("proof mismatch refuses registration/session transmission", async (t) => {
  const { c, calls, state } = await fixture(t);
  state.badProof = true;
  c.close();
  const count = calls.length;
  await assert.rejects(c.connect(), (e) => e.code === "invalid_proof");
  assert.equal(calls.length, count + 1);
  assert.ok(calls.at(-1).path.endsWith("/info"));
});
test("async iterator delivery does not implicitly acknowledge", async (t) => {
  const { c, calls } = await fixture(t);
  const events = c.events({ waitSeconds: 0 });
  const first = await events.next();
  assert.equal(first.value.sequence, 2);
  assert.equal(c.cursor, 0);
  c.ack(first.value.sequence);
  await events.return();
  await c.pollEvents(0);
  assert.equal(calls.at(-1).body.ack, 2);
});
test("a delivered Deny cannot be changed into Allow before applying", async (t) => {
  const { c, deliver } = await fixture(t);
  await c.requestApproval(
    "s",
    { id: "a", toolName: "Shell", commandSummary: "Check" },
    { title: "S" },
  );
  const e = await deliver({
    sequence: 1,
    type: "approval",
    sessionId: "s",
    host: "local",
    requestId: "a",
    decision: "deny",
    expiresAt: Date.now() / 1000 + 30,
  });
  let applied = false;
  await assert.rejects(
    c.applyEvent({ ...e, decision: "allow" }, () => {
      applied = true;
    }),
    (e) => e.code === "invalid_cursor",
  );
  assert.equal(applied, false);
  assert.equal(c.cursor, 0);
});
test("own UI answer for replaced request cannot retire new prompt", async (t) => {
  const { c } = await fixture(t);
  const question = (id) => ({
    id,
    questions: [{ text: "Pick", options: [{ label: "A" }, { label: "B" }] }],
  });
  await c.askQuestion("s", question("q1"), { title: "S" });
  await c.askQuestion("s", question("q2"));
  let commits = 0;
  assert.equal(
    await c.answerInUI(
      "s",
      "local",
      () => {
        commits++;
      },
      { requestId: "q1" },
    ),
    false,
  );
  assert.equal(commits, 0);
  assert.equal(c.pending.get(JSON.stringify(["s", "local"])).request.id, "q2");
});
test("mutating registration descriptor still retires pending prompts on successful re-registration", async (t) => {
  const { c } = await fixture(t);
  await c.requestApproval(
    "s",
    { id: "a", toolName: "Shell", commandSummary: "Check" },
    { title: "S" },
  );
  c.registration.name = "Changed";
  await c.register();
  assert.equal(c.pending.size, 0);
  assert.equal(c.reconciliationRequired, true);
  assert.equal(c.history.has("a"), true);
});

test("v1.2 registration passes icon and palette.states through unchanged", async (t) => {
  const registration = {
    name: "Test",
    palette: {
      baseHue: 150,
      states: { thinking: "#A1b2C3", waitingPermission: "#d4E5f6" },
    },
    icon: { png: "aGVsbG8=", appBundleId: "com.example.agent" },
  };
  const { calls } = await fixture(t, { registration });
  assert.deepEqual(calls.at(-1).body, registration);
});

test("v1.2 registration sends explicit null icon and palette.states", async (t) => {
  const registration = {
    name: "Test",
    palette: { baseHue: 150, states: null },
    icon: null,
  };
  const { calls } = await fixture(t, { registration });
  assert.equal(calls.at(-1).body.icon, null);
  assert.equal(calls.at(-1).body.palette.states, null);
  assert.deepEqual(calls.at(-1).body, registration);
});

test("v1.2 fields register on API v1.1 and preserve warnings", async (t) => {
  const { c, calls, state } = await fixture(t);
  const warnings = [
    "unknown field 'icon' ignored",
    "unknown field 'palette.states' ignored",
  ];
  state.registrationWarnings = warnings;
  const registration = {
    ...c.registration,
    palette: {
      ...c.registration.palette,
      states: { thinking: "#a1b2c3" },
    },
    icon: { appBundleId: "com.example.agent" },
  };
  const reply = await c.register(registration);
  assert.equal(c.info.apiMinor, 1);
  assert.deepEqual(calls.at(-1).body, registration);
  assert.deepEqual(reply.warnings, warnings);
});

test("v1.2 icon-only descriptor change retires pending prompts", async (t) => {
  const { c } = await fixture(t);
  await c.requestApproval(
    "s",
    { id: "a", toolName: "Shell", commandSummary: "Check" },
    { title: "S" },
  );
  await c.register({
    ...c.registration,
    icon: { appBundleId: "com.example.agent" },
  });
  assert.equal(c.pending.size, 0);
  assert.equal(c.reconciliationRequired, true);
});

test("v1.2 palette.states-only descriptor change retires pending prompts", async (t) => {
  const { c } = await fixture(t);
  await c.requestApproval(
    "s",
    { id: "a", toolName: "Shell", commandSummary: "Check" },
    { title: "S" },
  );
  await c.register({
    ...c.registration,
    palette: {
      ...c.registration.palette,
      states: { waitingPermission: "#d4e5f6" },
    },
  });
  assert.equal(c.pending.size, 0);
  assert.equal(c.reconciliationRequired, true);
});

test("project merges, null clears and batches preserve the field", async (t) => {
  const { c, calls, rotate } = await fixture(t);
  await c.upsertSession("s", { title: "Job", activity: "thinking", project: "Customer jobs" });
  assert.equal(calls.at(-1).body.project, "Customer jobs");
  await c.upsertSession("s", { activity: "idle" });
  assert.deepEqual(calls.at(-1).body, { title: "Job", activity: "idle", project: "Customer jobs", host: "local" });
  await c.upsertSession("s", { project: null });
  assert.equal(calls.at(-1).body.project, null);
  await rotate();
  await c.heartbeat();
  const restored = calls.filter((x) => x.path.endsWith("/sessions/s")).at(-1);
  assert.equal(restored.body.project, null);
  await c.replaceSessions([{ id: "job", title: "Job", activity: "idle", project: "Batch jobs" }]);
  assert.equal(calls.at(-1).body.sessions[0].project, "Batch jobs");
});

async function discoveryFixture(t, platform) {
  const home = await mkdtemp(join(tmpdir(), "orbhome-"));
  const { default: os } = await import("node:os");
  const { syncBuiltinESMExports } = await import("node:module");
  const platformDescriptor = Object.getOwnPropertyDescriptor(process, "platform");
  const originalHomedir = os.homedir;
  const names = ["HOME", "USERPROFILE", "LOCALAPPDATA", "ORBTILE_API_FILE"];
  const saved = Object.fromEntries(names.map((name) => [name, process.env[name]]));
  Object.defineProperty(process, "platform", { value: platform });
  os.homedir = () => home;
  syncBuiltinESMExports();
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  process.env.LOCALAPPDATA = join(home, "AppData", "Local");
  delete process.env.ORBTILE_API_FILE;
  t.after(async () => {
    os.homedir = originalHomedir;
    Object.defineProperty(process, "platform", platformDescriptor);
    syncBuiltinESMExports();
    for (const name of names) {
      if (saved[name] === undefined) delete process.env[name];
      else process.env[name] = saved[name];
    }
    await rm(home, { recursive: true });
  });
  return {
    home,
    touch: async (file) => { await mkdir(dirname(file), { recursive: true }); await writeFile(file, "{}"); },
    chosen: (options = {}) =>
      new AdapterClient({ adapterId: "dev.test", registration: { name: "Test", palette: { baseHue: 150 } }, ...options }).apiFile,
  };
}

test("discovery: macOS keeps explicit file, ORBTILE_API_FILE, Standard then Enterprise fallback", async (t) => {
  const { home, touch, chosen } = await discoveryFixture(t, "darwin");
  const support = `${home}/Library/Application Support`;
  const standard = `${support}/Orbtile/adapter-api.json`;
  const enterprise = `${support}/Orbtile Enterprise/adapter-api.json`;
  assert.equal(chosen(), standard); // neither exists
  await touch(enterprise);
  assert.equal(chosen(), enterprise);
  const missing = join(home, "missing.json");
  assert.equal(chosen({ apiFile: missing }), missing);
  process.env.ORBTILE_API_FILE = missing;
  assert.equal(chosen(), missing);
  delete process.env.ORBTILE_API_FILE;
  await touch(standard);
  assert.equal(chosen(), standard);
});

test("discovery: Windows searches Store, Standard MSI, then Enterprise MSI", async (t) => {
  const { home, touch, chosen } = await discoveryFixture(t, "win32");
  const store = join(home, ".orbtile", "adapter-api.json");
  const standard = join(process.env.LOCALAPPDATA, "Orbtile", "Windows", "adapter-api.json");
  const enterprise = join(process.env.LOCALAPPDATA, "Orbtile Enterprise", "Windows", "adapter-api.json");
  assert.equal(chosen(), store); // Store is the default when no file exists
  await touch(enterprise);
  assert.equal(chosen(), enterprise);
  await touch(standard);
  assert.equal(chosen(), standard);
  await touch(store);
  assert.equal(chosen(), store);
  const missing = join(home, "missing.json");
  assert.equal(chosen({ apiFile: missing }), missing);
  process.env.ORBTILE_API_FILE = missing;
  assert.equal(chosen(), missing);
});
