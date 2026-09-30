#!/usr/bin/env python3
"""Orbtile Adapter Protocol v1 — reference adapter in Python (stdlib only).

Transport: HTTP/1.1 over the Unix-domain socket named in the discovery file (AF_UNIX).
Verifies that the listener is the real Orbtile (HMAC proof), registers, then mirrors
a toy agent loop. Re-reads the discovery file on 401 (token regenerated) and re-registers
on 404 adapter_not_registered; after an Orbtile relaunch both happen on one call. Honors
429 Retry-After.

Updates are merges, and a relaunched Orbtile has no sessions: the client keeps the merged
state of each session and sends all of it after registering again.
"""
import hashlib, hmac, http.client, json, os, secrets, socket, time

API_FILE = os.environ.get("ORBTILE_API_FILE",
                          os.path.expanduser("~/Library/Application Support/Orbtile/adapter-api.json"))
ADAPTER = "dev.example.python"
# Cyan working orb. A base hue in the calm arc needs an alarm colour of its own (see protocol/PALETTE.md).
REG = {"name": "PyAgent", "palette": {"baseHue": 200, "attentionHue": 18},
       "glyph": {"symbol": "sparkles", "monogram": "Py"}, "version": "1.0.0", "urlSchemes": ["https"]}


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


class Orbtile:
    def __init__(self):
        self.sessions = {}  # session id -> merged fields sent so far
        self.load()

    def load(self):
        with open(API_FILE) as f:
            d = json.load(f)
        self.socket_path, self.base, self.token = d["socketPath"], d["basePath"], d["token"]

    def call(self, method, path, body=None, headers=None, retry=("401", "404"), full=None):
        """One request. `retry` names the recoveries still allowed; `full` is the body to send after registering
        again (the whole session, since the server lost it)."""
        conn = UnixHTTPConnection(self.socket_path)
        try:
            payload = None if method == "GET" else json.dumps(body or {}).encode()
            conn.request(method, self.base + path, body=payload,
                         headers={"Authorization": "Bearer " + self.token,
                                  "Content-Type": "application/json", **(headers or {})})
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
            self.load()
            return self.call(method, path, body, headers, tuple(r for r in retry if r != "401"), full)
        if err.get("code") == "adapter_not_registered" and "404" in retry:
            self.register()
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

    def register(self):
        status, reply, _ = self.call("PUT", f"/adapters/{ADAPTER}", REG, retry=("401",))
        for w in reply.get("warnings", []):
            print("orbtile:", w)
        if status in (200, 201) and not reply.get("enabled"):
            print("registered; waiting for the user to allow it in Orbtile › Settings › Agents")

    def session(self, sid, **fields):
        full = self.sessions.setdefault(sid, {})
        full.update(fields)
        return self.call("POST", f"/adapters/{ADAPTER}/sessions/{sid}", fields, full=full)

    def end(self, sid):
        self.sessions.pop(sid, None)
        return self.call("DELETE", f"/adapters/{ADAPTER}/sessions/{sid}")


if __name__ == "__main__":
    o = Orbtile(); o.verify(); o.register()
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
