#!/usr/bin/env python3
"""Orbtile Adapter Protocol v1.2 — reference adapter in Python (stdlib only).

Transport: HTTP/1.1 over the Unix-domain socket named in the discovery file (AF_UNIX).
Verifies that the listener is the real Orbtile (HMAC proof), registers, then mirrors
a toy agent loop. Re-reads the discovery file on 401 (token regenerated) and re-registers
on 404 adapter_not_registered; after an Orbtile relaunch both happen on one call. Honors
429 Retry-After.

Updates are merges, and a relaunched Orbtile has no sessions: the client keeps the merged
state of each session and sends all of it after registering again (display-only v1). With
--events, wait for the user's Allow before publishing anything, and reconcile the own UI and
issue fresh request ids after a lost registration.
"""
import argparse, hashlib, hmac, http.client, json, math, os, secrets, socket, threading, time

def discovery_file():
    """ORBTILE_API_FILE when set; else Orbtile's file, or Orbtile Enterprise's when only that one exists."""
    if "ORBTILE_API_FILE" in os.environ:
        return os.environ["ORBTILE_API_FILE"]
    support = os.path.expanduser("~/Library/Application Support")
    standard = os.path.join(support, "Orbtile", "adapter-api.json")
    enterprise = os.path.join(support, "Orbtile Enterprise", "adapter-api.json")
    return enterprise if not os.path.exists(standard) and os.path.exists(enterprise) else standard


API_FILE = discovery_file()
ADAPTER = "dev.example.python"
# Cyan base palette with four optional v1.2 state colours and an installed macOS app icon.
REG = {"name": "PyAgent",
       "palette": {"baseHue": 200, "attentionHue": 18,
                   "states": {"thinking": "#eab308", "waitingPermission": "#ef4444",
                              "done": "#22c55e", "idle": "#6b7280"}},
       "glyph": {"symbol": "sparkles", "monogram": "Py"},
       "icon": {"appBundleId": "com.apple.Terminal"},
       "version": "1.0.0", "urlSchemes": ["https"]}


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects to a Unix-domain socket. Host header: localhost."""

    def __init__(self, path, timeout=2):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.socket_path)
        self.sock = s


class EventConsumer:
    """Serialize this with the authoritative UI: re-check, apply once, then acknowledge.

    The callback must commit the decision in the agent UI atomically, not launch arbitrary
    commands from event text. A raising callback must leave no side effects (or use an
    idempotent own-UI ledger). An on-screen answer calls clear() under the same lock. State
    is memory-only; keep this object over transport reconnects, reset on new registration.
    """
    def __init__(self, clock=time.time):
        self.clock = clock
        self.ack = 0
        self.requests = {}
        self.history = set()
        self.lock = threading.RLock()

    def pending(self, sid, host, kind, request):
        with self.lock:
            key = (sid, host)
            existing = self.requests.get(key)
            if existing and existing[1]["id"] == request["id"]:
                if existing[0] != kind or existing[1] != request:
                    raise ValueError("a pending id cannot change its contents")
                return
            if request["id"] in self.history:
                raise ValueError("request id was already used in this registration")
            if len(self.history) >= 4096:
                raise ValueError("request id history is full; reconcile before a new registration")
            self.history.add(request["id"])
            lifetime = 120 if kind == "approval" else 600
            self.requests[key] = (kind, json.loads(json.dumps(request)), self.clock() + lifetime)

    def clear(self, sid, host="local"):
        with self.lock:
            self.requests.pop((sid, host), None)

    def reset(self):
        with self.lock:
            self.requests.clear()
            self.history.clear()
            self.ack = 0

    def consume(self, events, apply):
        with self.lock:
            for event in events:
                seq = event.get("sequence")
                if type(seq) is not int or seq < 1:
                    raise ValueError("invalid event sequence")
                if seq <= self.ack:
                    continue
                kind = event.get("type")
                key = (event.get("sessionId"), event.get("host"))
                pending = self.requests.get(key)
                valid = kind == "tap"
                if kind in ("approval", "question"):
                    expiry = event.get("expiresAt")
                    valid = (pending is not None and pending[0] == kind
                             and pending[1]["id"] == event.get("requestId")
                             and isinstance(expiry, (int, float)) and not isinstance(expiry, bool)
                             and math.isfinite(expiry) and self.clock() < min(expiry, pending[2]))
                    if valid and kind == "approval":
                        valid = event.get("decision") in ("allow", "deny")
                    if valid and kind == "question":
                        valid = valid_picks(pending[1]["questions"], event.get("picks"))
                if valid:
                    apply(event)  # exceptions leave it pending and unacknowledged for retry
                    if kind != "tap":
                        self.requests.pop(key, None)
                self.ack = seq  # stale/expired/unknown events are discarded and acknowledged


def valid_picks(questions, picks):
    if not isinstance(picks, list) or len(picks) != len(questions):
        return False
    for question, selected in zip(questions, picks):
        if (not isinstance(selected, list) or not selected
                or any(type(i) is not int or not 0 <= i < len(question["options"]) for i in selected)
                or len(set(selected)) != len(selected)
                or (not question.get("multiSelect", False) and len(selected) != 1)):
            return False
    return True


class Orbtile:
    def __init__(self, events=False):
        self.interactive = events
        self.adapter_token = None
        self.consumer = EventConsumer()
        self.sessions = {}  # session id -> merged fields sent so far
        self.load()

    def load(self):
        with open(API_FILE) as f:
            d = json.load(f)
        self.socket_path, self.base, self.token = d["socketPath"], d["basePath"], d["token"]

    def call(self, method, path, body=None, headers=None, retry=("401", "404"), full=None):
        """One request. `retry` names the recoveries still allowed; `full` is the body to send after registering
        again (the whole session, since the server lost it)."""
        conn = UnixHTTPConnection(self.socket_path, timeout=5)
        try:
            payload = None if method == "GET" else json.dumps(body or {}).encode()
            conn.request(method, self.base + path, body=payload,
                         headers={"Authorization": "Bearer " + self.token,
                                  "Content-Type": "application/json",
                                  **({"X-Orbtile-Adapter-Token": self.adapter_token}
                                     if self.adapter_token and path.startswith("/adapters/") else {}),
                                  **(headers or {})})
            r = conn.getresponse()
            raw = r.read()
            status, reply_headers = r.status, r.headers
        finally:
            conn.close()
        data = json.loads(raw) if raw else {}
        if status < 400:
            return status, data, reply_headers
        err = data.get("error", {})
        if status == 401 and "401" in retry:
            previous = self.token
            self.load()
            if self.token != previous:
                self.adapter_token = None
            return self.call(method, path, body, headers, tuple(r for r in retry if r != "401"), full)
        if err.get("code") == "adapter_not_registered" and "404" in retry:
            self.adapter_token = None
            self.register()
            if self.interactive:
                # Old prompts belong to a lost registration; reconcile with your UI and
                # create fresh request ids explicitly. Never blindly replay old approvals.
                return status, err, reply_headers
            return self.call(method, path, full or body, headers, (), None)
        if status == 429:
            time.sleep(float(reply_headers.get("Retry-After", "1")))
        return status, err, reply_headers

    def verify(self):
        nonce = secrets.token_hex(16)
        status, info, _ = self.call("GET", "/info", headers={"X-Orbtile-Nonce": nonce})
        want = hmac.new(self.token.encode(), nonce.encode(), hashlib.sha256).hexdigest()
        if status != 200 or not hmac.compare_digest(info.get("proof", ""), want):
            raise SystemExit("listener is not Orbtile; refusing to send session data")

    def registration_body(self):
        registration = dict(REG)
        if self.interactive:
            registration.update(capabilities=["tap", "approvals", "questions"], tapBehavior="alsoOpen")
        return registration

    def register(self):
        status, reply, _ = self.call("PUT", f"/adapters/{ADAPTER}", self.registration_body(), retry=("401",))
        if status in (200, 201) and self.interactive:
            token = reply.get("adapterToken")
            if not isinstance(token, str) or not token:
                raise RuntimeError("this host does not support adapter events (requires apiMinor 1)")
            if token != self.adapter_token:
                self.consumer.reset()
                # Keep normal session metadata; pending ids cannot cross registrations.
                for session in self.sessions.values():
                    session.pop("approval", None)
                    session.pop("question", None)
            self.adapter_token = token
        for w in reply.get("warnings", []):
            print("orbtile:", w)
        if status in (200, 201) and not reply.get("enabled"):
            print("registered; waiting for the user to allow it in Orbtile › Settings › Agents")

    def wait_until_enabled(self, timeout=120):
        """A fresh registration replies enabled:false until the user allows it; an approval published
        before that never reaches the keypad and is not republished. A re-registration is the
        cheapest poll (its reply carries `enabled`; a heartbeat is a bare 204)."""
        told = False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, reply, _ = self.call("PUT", f"/adapters/{ADAPTER}", self.registration_body(), retry=("401",))
            if status in (200, 201) and reply.get("enabled"):
                return
            if reply.get("reason") == "disabled_by_user":
                raise SystemExit("the user denied this adapter in Orbtile; the demo publishes nothing")
            if not told:
                print("waiting: click Allow for PyAgent in Orbtile › Settings › Agents", flush=True)
                told = True
            time.sleep(1)
        raise SystemExit("timed out waiting for Allow; allow PyAgent in Orbtile and run the demo again")

    def session(self, sid, **fields):
        full = self.sessions.setdefault(sid, {})
        full.update(fields)
        host = full.get("host", "local")
        for kind in ("approval", "question"):
            if kind in fields:
                if fields[kind] is None:
                    self.consumer.clear(sid, host)
                else:
                    self.consumer.pending(sid, host, kind, fields[kind])
        return self.call("POST", f"/adapters/{ADAPTER}/sessions/{sid}", fields, full=full)

    def end(self, sid):
        previous = self.sessions.pop(sid, {})
        self.consumer.clear(sid, previous.get("host", "local"))
        return self.call("DELETE", f"/adapters/{ADAPTER}/sessions/{sid}")

    def poll_events(self, apply, wait=2):
        if not self.interactive or not self.adapter_token:
            raise RuntimeError("register with events=True first")
        status, reply, _ = self.call("POST", f"/adapters/{ADAPTER}/events",
                                   {"after": self.consumer.ack, "ack": self.consumer.ack,
                                    "waitSeconds": wait})
        if status == 200:
            self.consumer.consume(reply["events"], apply)
        return status, reply


def events_demo(o):
    """Toy UI only: no command is executed. Real agents keep their own dialog authoritative."""
    sid = "demo-events"
    approval = {"id": "approval-" + secrets.token_hex(8), "toolName": "Bash",
                "commandSummary": "echo 'demo only; no command is executed'"}
    question = {"id": "question-" + secrets.token_hex(8), "questions": [
        {"text": "Which docs should this toy agent read?", "header": "Docs", "multiSelect": True,
         "options": [{"label": "README"}, {"label": "Protocol"}]}]}
    o.session(sid, title="Event demo (own UI still authoritative)", activity="waitingPermission",
              approval=approval, openURL="https://example.com/run/events")
    print("Own UI approval stays available.", flush=True)
    phase = "approval"
    def apply(event):
        nonlocal phase
        print("authoritative toy UI applied", json.dumps(event), flush=True)
        if event["type"] == "approval":
            phase = "question"
        elif event["type"] == "question":
            phase = "done"
    deadline = time.monotonic() + 90
    sent_question = False
    while time.monotonic() < deadline and phase != "done":
        status, _ = o.poll_events(apply)
        if status != 200:
            print("disconnected; return to the own UI and reconcile before new request ids", flush=True)
            break
        if phase == "question" and not sent_question:
            o.session(sid, activity="waitingQuestion", approval=None, question=question)
            sent_question = True
        # Empty responses can return immediately. Avoid consuming the API's request budget.
        time.sleep(0.1)
    if phase == "done":
        # Next poll acknowledges the answer; this update also retires the own-UI prompt.
        o.poll_events(apply, wait=0)
        o.session(sid, activity="done", question=None)
    else:
        print("prompt still belongs to the own UI; timeout is never implicit Allow", flush=True)
    o.end(sid)


if __name__ == "__main__":
    # parse_known_args preserves callers that run the demo using runpy with their own argv.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", action="store_true", help="demonstrate the v1.1 event channel")
    args, _ = parser.parse_known_args()
    o = Orbtile(events=args.events); o.verify(); o.register()
    if args.events:
        o.wait_until_enabled()
        events_demo(o)
        raise SystemExit(0)
    sid = "demo-1"
    o.session(sid, title="PyAgent demo", activity="thinking", model="toy-1")
    time.sleep(2)
    o.session(sid, activity="tool", toolLabel="Read  README.md")
    time.sleep(2)
    o.session(sid, activity="streaming", message={"id": "m1", "text": "Here is what I found in the README ..."})
    time.sleep(2)
    o.session(sid, activity="done", openURL="https://example.com/run/1")
    time.sleep(10)
    o.end(sid)
