import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { request } from "node:http";
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { homedir } from "node:os";
import { join } from "node:path";
import { inspect, isDeepStrictEqual } from "node:util";
const copy = (value) => structuredClone(value);
function defaultApiFile() {
  if (process.platform === "win32") {
    const store = join(homedir(), ".orbtile", "adapter-api.json");
    const local = process.env.LOCALAPPDATA ?? join(homedir(), "AppData", "Local");
    const standard = join(local, "Orbtile", "Windows", "adapter-api.json");
    const enterprise = join(local, "Orbtile Enterprise", "Windows", "adapter-api.json");
    return [store, standard, enterprise].find((file) => existsSync(file)) ?? store;
  }
  const support = `${homedir()}/Library/Application Support`;
  const standard = `${support}/Orbtile/adapter-api.json`;
  const enterprise = `${support}/Orbtile Enterprise/adapter-api.json`;
  return !existsSync(standard) && existsSync(enterprise) ? enterprise : standard;
}
const key = (id, host = "local") => JSON.stringify([id, host]);
export class AdapterError extends Error {
  constructor(code, message = code, { status = 0, field, retryAfter } = {}) {
    super(message);
    this.name = this.constructor.name;
    this.code = code;
    this.status = status;
    this.field = field;
    this.retryAfter = retryAfter;
  }
}
export class PayloadTooLargeError extends AdapterError {
  constructor() {
    super("payload_too_large", "JSON body exceeds 65536 bytes", {
      status: 413,
    });
  }
}
export class TransportError extends AdapterError {
  constructor() {
    super("transport_error", "Local Orbtile connection failed");
  }
}
export class RegistrationLostError extends AdapterError {
  constructor() {
    super(
      "registration_lost",
      "Registration ended; reconcile your UI and publish fresh request ids",
    );
  }
}
export class AdapterClient {
  #api;
  #owner;
  #timer;
  #lock = Promise.resolve();
  #recovering;
  #polling = false;
  #closed = false;
  #received = new Map();
  #registeredDescriptor;
  constructor({
    adapterId,
    registration,
    apiFile = process.env.ORBTILE_API_FILE ?? defaultApiFile(),
    heartbeatIntervalMs = 10000,
    onLeaseError = () => {},
  }) {
    if (
      !Number.isFinite(heartbeatIntervalMs) ||
      heartbeatIntervalMs <= 0 ||
      heartbeatIntervalMs > 10000
    )
      throw new AdapterError(
        "invalid_field",
        "heartbeatIntervalMs must be >0 and <=10000",
      );
    this.adapterId = adapterId;
    this.registration = copy(registration);
    this.apiFile = apiFile;
    this.heartbeatIntervalMs = heartbeatIntervalMs;
    this.onLeaseError = onLeaseError;
    this.cursor = 0;
    this.leaseGeneration = 0;
    this.reconciliationRequired = false;
    this.delivered = new Set();
    this.sessions = new Map();
    this.pending = new Map();
    this.history = new Set();
  }
  [inspect.custom]() {
    return `AdapterClient(${this.adapterId})`;
  }
  get path() {
    return `/adapters/${encodeURIComponent(this.adapterId)}`;
  }
  async #load() {
    let api;
    try {
      api = JSON.parse(await readFile(this.apiFile, "utf8"));
    } catch {
      throw new AdapterError(
        "discovery_unavailable",
        "Orbtile discovery file unavailable",
      );
    }
    const windowsTcp = process.platform === "win32" &&
      Number.isInteger(api.port) && api.port > 0 && api.port <= 65535 &&
      api.url === `http://127.0.0.1:${api.port}`;
    if (
      api.api !== 1 ||
      (typeof api.socketPath !== "string" && !windowsTcp) ||
      typeof api.token !== "string" ||
      !api.token ||
      /[\r\n]/.test(api.token) ||
      api.basePath !== "/a/v1"
    )
      throw new AdapterError("invalid_discovery");
    this.#api = api;
  }
  async #send(method, path, body, headers = {}) {
    if (!this.#api)
      throw new AdapterError("not_connected", "Call connect() first");
    const data = body === undefined ? undefined : JSON.stringify(body);
    if (data !== undefined && Buffer.byteLength(data) > 65536)
      throw new PayloadTooLargeError();
    return new Promise((resolve, reject) => {
      const req = request(
        {
          ...(typeof this.#api.socketPath === "string"
            ? { socketPath: this.#api.socketPath }
            : { host: "127.0.0.1", port: this.#api.port }),
          path: this.#api.basePath + path,
          method,
          agent: false,
          headers: {
            host: "localhost",
            "content-type": "application/json",
            authorization: `Bearer ${this.#api.token}`,
            "content-length": data === undefined ? 0 : Buffer.byteLength(data),
            ...(this.#owner && path.startsWith("/adapters/")
              ? { "x-orbtile-adapter-token": this.#owner }
              : {}),
            ...headers,
          },
        },
        (res) => {
          const chunks = [];
          res.on("data", (c) => chunks.push(c));
          res.on("error", () => reject(new TransportError()));
          res.on("end", () => {
            let reply;
            try {
              reply = chunks.length
                ? JSON.parse(Buffer.concat(chunks).toString())
                : {};
            } catch {
              return reject(new AdapterError("invalid_response"));
            }
            if (res.statusCode >= 400)
              return reject(
                new AdapterError(
                  reply.error?.code ?? "http_error",
                  `HTTP request rejected (${res.statusCode})`,
                  {
                    status: res.statusCode,
                    field: reply.error?.field,
                    retryAfter: Number(res.headers["retry-after"] ?? 1),
                  },
                ),
              );
            resolve(reply);
          });
        },
      );
      req.setTimeout(5000, () => req.destroy());
      req.on("error", () => reject(new TransportError()));
      req.end(data);
    });
  }
  async #verify() {
    const nonce = randomBytes(16).toString("hex");
    const info = await this.#send("GET", "/info", undefined, {
      "x-orbtile-nonce": nonce,
    });
    const expected = createHmac("sha256", this.#api.token)
      .update(nonce)
      .digest();
    const actual = Buffer.from(info.proof ?? "", "hex");
    if (actual.length !== expected.length || !timingSafeEqual(actual, expected))
      throw new AdapterError("invalid_proof", "Listener proof did not match");
    if (this.registration.capabilities?.length && !(info.apiMinor >= 1))
      throw new AdapterError("events_not_supported");
    this.info = info;
    return info;
  }
  async connect() {
    await this.#load();
    await this.#verify();
    const reply = await this.register();
    this.#closed = false;
    this.#timer ??= setInterval(() => {
      if (!this.#closed) this.heartbeat().catch((e) => this.onLeaseError(e));
    }, this.heartbeatIntervalMs);
    this.#timer.unref();
    return reply;
  }
  register(registration = this.registration) {
    return this.#serial(() => this.#register(registration));
  }
  async #register(registration = this.registration) {
    const descriptor = (r) =>
      JSON.stringify(
        [
          "name",
          "shortName",
          "palette",
          "urlSchemes",
          "homepage",
          "capabilities",
          "tapBehavior",
        ].map((k) => r[k]),
      );
    const nextDescriptor = descriptor(registration);
    const changed =
      this.#registeredDescriptor !== undefined &&
      this.#registeredDescriptor !== nextDescriptor;
    this.registration = copy(registration);
    const previous = this.#owner;
    const reply = await this.#send("PUT", this.path, this.registration);
    if (this.registration.capabilities?.length && !reply.adapterToken)
      throw new AdapterError("events_not_supported");
    if (previous && reply.adapterToken && reply.adapterToken !== previous)
      this.#reset();
    if (
      reply.adapterToken &&
      (typeof reply.adapterToken !== "string" ||
        /[\r\n]/.test(reply.adapterToken))
    )
      throw new AdapterError("invalid_response");
    if (reply.adapterToken) this.#owner = reply.adapterToken;
    this.#registeredDescriptor = nextDescriptor;
    if (changed) {
      if (this.pending.size) this.reconciliationRequired = true;
      this.pending.clear();
      for (const session of this.sessions.values()) {
        delete session.approval;
        delete session.question;
      }
    }
    return reply;
  }
  #reset() {
    this.leaseGeneration++;
    if (this.pending.size) this.reconciliationRequired = true;
    this.#owner = undefined;
    this.#registeredDescriptor = undefined;
    this.cursor = 0;
    this.delivered.clear();
    this.#received.clear();
    this.pending.clear();
    this.history.clear();
    for (const s of this.sessions.values()) {
      delete s.approval;
      delete s.question;
    }
  }
  async #recover() {
    if (!this.#recovering)
      this.#recovering = (async () => {
        this.#reset();
        await this.#load();
        await this.#verify();
        await this.#register();
        for (const [identity, session] of this.sessions) {
          const [id] = JSON.parse(identity);
          await this.#send(
            "POST",
            `${this.path}/sessions/${encodeURIComponent(id)}`,
            session,
          );
        }
      })().finally(() => {
        this.#recovering = undefined;
      });
    return this.#recovering;
  }
  async #call(method, path, body) {
    try {
      return await this.#send(method, path, body);
    } catch (e) {
      if (e.status === 401 || e.code === "adapter_not_registered") {
        if (e.status === 401) {
          const previousToken = this.#api.token;
          await this.#load();
          await this.#verify();
          if (previousToken === this.#api.token) {
            try {
              return await this.#send(method, path, body);
            } catch (retryError) {
              if (retryError.code !== "adapter_not_registered")
                throw retryError;
            }
          }
        }
        const hadPending = this.pending.size > 0;
        await this.#recover();
        if (
          hadPending ||
          body?.approval ||
          body?.question ||
          path.endsWith("/events")
        )
          throw new RegistrationLostError();
        return this.#send(method, path, body);
      }
      throw e;
    }
  }
  #serial(fn) {
    const p = this.#lock.then(fn);
    this.#lock = p.catch(() => {});
    return p;
  }
  async heartbeat() {
    return this.#serial(() => this.#call("POST", `${this.path}/heartbeat`));
  }
  async upsertSession(id, fields) {
    return this.#serial(async () => {
      const host = fields.host ?? "local";
      const identity = key(id, host);
      const full = { ...this.sessions.get(identity), ...copy(fields), host };
      // Validate request identity before publication; commit local ledger only after success.
      let entry;
      for (const kind of ["approval", "question"])
        if (full[kind]) {
          if (entry)
            throw new AdapterError(
              "invalid_field",
              "Only one request may be pending",
            );
          const req = full[kind];
          const prior = this.pending.get(identity);
          if (prior?.request.id === req.id) {
            if (
              prior.kind !== kind ||
              JSON.stringify(prior.request) !== JSON.stringify(req)
            )
              throw new AdapterError("stale_request");
            entry = prior;
          } else {
            if (this.history.has(req.id) || this.history.size >= 4096)
              throw new AdapterError("stale_request");
            entry = {
              kind,
              request: copy(req),
              expiresAt: Date.now() / 1000 + (kind === "approval" ? 120 : 600),
            };
          }
        }
      const reply = await this.#call(
        "POST",
        `${this.path}/sessions/${encodeURIComponent(id)}`,
        full,
      );
      this.sessions.set(identity, full);
      if (entry) {
        this.pending.set(identity, entry);
        this.history.add(entry.request.id);
      } else this.pending.delete(identity);
      return reply;
    });
  }
  async removeSession(id, host = "local") {
    return this.#serial(async () => {
      const reply = await this.#call(
        "DELETE",
        `${this.path}/sessions/${encodeURIComponent(id)}?host=${encodeURIComponent(host)}`,
      );
      this.sessions.delete(key(id, host));
      this.pending.delete(key(id, host));
      return reply;
    });
  }
  async replaceSessions(sessions) {
    return this.#serial(async () => {
      if (sessions.some((s) => s.approval || s.question))
        throw new AdapterError(
          "invalid_field",
          "Use request helpers for interactive sessions",
        );
      const reply = await this.#call("PUT", `${this.path}/sessions`, {
        sessions,
      });
      this.sessions.clear();
      this.pending.clear();
      for (const { id, ...s } of sessions)
        this.sessions.set(key(id, s.host), copy(s));
      return reply;
    });
  }
  async requestApproval(id, approval, fields = {}) {
    return this.upsertSession(id, {
      ...fields,
      activity: "waitingPermission",
      question: null,
      approval,
    });
  }
  async askQuestion(id, question, fields = {}) {
    return this.upsertSession(id, {
      ...fields,
      activity: "waitingQuestion",
      approval: null,
      question,
    });
  }
  async answerInUI(id, host, commit, { requestId } = {}) {
    return this.#serial(async () => {
      const identity = key(id, host);
      const pending = this.pending.get(identity);
      if (
        !pending ||
        (requestId !== undefined && requestId !== pending.request.id)
      )
        return false;
      await commit(copy(pending.request));
      this.pending.delete(identity);
      const s = this.sessions.get(identity);
      if (s) {
        s[pending.kind] = null;
        await this.#call(
          "POST",
          `${this.path}/sessions/${encodeURIComponent(id)}`,
          { host, [pending.kind]: null },
        );
      }
      return true;
    });
  }
  async pollEvents(waitSeconds = 2) {
    if (!Number.isInteger(waitSeconds) || waitSeconds < 0 || waitSeconds > 2)
      throw new AdapterError("invalid_field");
    if (this.#polling) throw new AdapterError("poll_pending");
    this.#polling = true;
    try {
      const reply = await this.#serial(() =>
        this.#call("POST", `${this.path}/events`, {
          after: this.cursor,
          ack: this.cursor,
          waitSeconds,
        }),
      );
      for (const e of reply.events) {
        if (!Number.isSafeInteger(e.sequence) || e.sequence < 1)
          throw new AdapterError("invalid_response");
        this.delivered.add(e.sequence);
        this.#received.set(e.sequence, copy(e));
      }
      return reply;
    } finally {
      this.#polling = false;
    }
  }
  ack(sequence) {
    if (!this.delivered.has(sequence) || sequence < this.cursor)
      throw new AdapterError("invalid_cursor");
    this.cursor = sequence;
    for (const n of this.delivered) if (n <= sequence) this.delivered.delete(n);
  }
  async *events({ signal, waitSeconds = 2 } = {}) {
    while (!this.#closed && !signal?.aborted) {
      let reply;
      try {
        reply = await this.pollEvents(waitSeconds);
      } catch (e) {
        if (
          e.code === "transport_error" ||
          e.code === "poll_limit" ||
          e.code === "rate_limited" ||
          e.code === "limit_exceeded"
        ) {
          await new Promise((r) => setTimeout(r, 1000 * (e.retryAfter ?? 1)));
          continue;
        }
        throw e;
      }
      if (!reply.events.length) await new Promise((r) => setTimeout(r, 50));
      for (const e of reply.events) {
        if (signal?.aborted || this.#closed) return;
        if (e.sequence > this.cursor) yield e;
      }
    }
  }
  async applyEvent(event, commit) {
    return this.#serial(async () => {
      if (event.sequence <= this.cursor) return false;
      if (
        !this.delivered.has(event.sequence) ||
        !isDeepStrictEqual(this.#received.get(event.sequence), event)
      )
        throw new AdapterError("invalid_cursor");
      const identity = key(event.sessionId, event.host);
      const p = this.pending.get(identity);
      let valid = event.type === "tap";
      if (event.type === "approval" || event.type === "question") {
        valid =
          !!p &&
          p.kind === event.type &&
          p.request.id === event.requestId &&
          Number.isFinite(event.expiresAt) &&
          Date.now() / 1000 < Math.min(event.expiresAt, p.expiresAt);
        if (valid)
          valid =
            event.type === "approval"
              ? ["allow", "deny"].includes(event.decision)
              : validPicks(p.request.questions, event.picks);
      }
      if (valid) {
        await commit(copy(event), p ? copy(p.request) : undefined);
        if (p && event.type !== "tap") {
          this.pending.delete(identity);
          const s = this.sessions.get(identity);
          if (s) s[p.kind] = null;
        }
      }
      this.ack(event.sequence);
      return valid;
    });
  }
  reconcile() {
    this.reconciliationRequired = false;
  }
  close() {
    this.#closed = true;
    clearInterval(this.#timer);
    this.#timer = undefined;
  }
  async unregister() {
    this.close();
    return this.#serial(async () => {
      await this.#call("DELETE", this.path);
      this.#reset();
      this.sessions.clear();
    });
  }
}
export function validPicks(questions, picks) {
  return (
    Array.isArray(picks) &&
    picks.length === questions.length &&
    questions.every(
      (q, n) =>
        Array.isArray(picks[n]) &&
        picks[n].length > 0 &&
        (q.multiSelect || picks[n].length === 1) &&
        new Set(picks[n]).size === picks[n].length &&
        picks[n].every(
          (i) => Number.isInteger(i) && i >= 0 && i < q.options.length,
        ),
    )
  );
}
