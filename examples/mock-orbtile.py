#!/usr/bin/env python3
"""A stand-in for the Orbtile Adapter API v1.

Use it to develop an adapter without Orbtile running. It serves the v1 endpoints on a Unix-domain
socket, writes a discovery file, checks auth and the basic field rules, and prints one line per
request. It keeps state in memory and draws nothing. It is not the real validator: the JSON Schema
and Orbtile itself are authoritative.

  python3 mock-orbtile.py /tmp/orbtile-mock/adapter-api.json
  ORBTILE_API_FILE=/tmp/orbtile-mock/adapter-api.json python3 ref.py

The discovery file path is required on purpose, so the mock never writes into the real
~/Library/Application Support/Orbtile.
"""
import hashlib, hmac, http.server, json, os, re, secrets, signal, socketserver, sys, tempfile

ACTIVITIES = ["starting", "idle", "thinking", "tool", "streaming", "waitingPermission", "waitingQuestion",
              "done", "error", "compacting", "offline"]
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*(\.[a-z0-9]+(-[a-z0-9]+)*)+$")
SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]{0,31}$")
URL_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
# Schemes an openURL may never use, whatever the registration says.
REFUSED_SCHEMES = {"file", "javascript", "data", "orbtile", "x-apple.systempreferences"}
# A Unix socket path must fit in sockaddr_un.sun_path (104 bytes on macOS, 108 on Linux).
MAX_SOCKET_PATH = 100
ERROR_HUE, ERROR_BAND = 30.9, 45.0
TOKEN = secrets.token_urlsafe(32)
adapters = {}   # id -> registration
sessions = {}   # id -> {sessionId: fields}


def check_open_url(fields, prefix, schemes):
    """The server's openURL rule: absent keeps, null clears, else a 1..2048 character string whose
    scheme is one of the adapter's registered urlSchemes and not a refused one. Returns an error
    reply (status, code, message, field) or None."""
    if "openURL" not in fields or fields["openURL"] is None:
        return None
    field, value = prefix + "openURL", fields["openURL"]
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return 422, "invalid_field", f"{field}: a string of 1 to 2048 characters", field
    m = URL_SCHEME_RE.match(value)
    scheme = m.group(1).lower() if m else None
    if scheme is None or scheme in REFUSED_SCHEMES or scheme not in schemes:
        return 422, "invalid_field", f"{field}: its scheme must be one of the registered urlSchemes", field
    return None


def check_url_schemes(body):
    """The server's urlSchemes rule at registration: at most 4, lowercase, unique, none refused."""
    if "urlSchemes" not in body:
        return None
    items = body["urlSchemes"]
    if not isinstance(items, list) or len(items) > 4:
        return 422, "invalid_field", "urlSchemes: at most 4 schemes", "urlSchemes"
    for i, s in enumerate(items):
        if not isinstance(s, str) or not SCHEME_RE.match(s) or s in REFUSED_SCHEMES or s in items[:i]:
            return (422, "invalid_field", "urlSchemes: lowercase, unique, not file/javascript/data/orbtile",
                    "urlSchemes")
    return None


def resolved_hue(h):
    """The reserved-arc push: hues within 45 degrees of 30.9 move to 345.9 or 75.9."""
    arc = (ERROR_HUE - h + 540) % 360 - 180
    if abs(arc) >= ERROR_BAND:
        return h
    return (ERROR_HUE + (-ERROR_BAND if arc > 0 else ERROR_BAND)) % 360


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):   # client_address is empty on a Unix socket
        pass

    def reply(self, status, body=None, headers=None):
        raw = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def fail(self, status, code, message, field=None):
        err = {"code": code, "message": message}
        if field:
            err["field"] = field
        self.reply(status, {"error": err})

    def handle_any(self):
        path = self.path
        body = {}
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            return self.fail(413, "payload_too_large", "body over 64 KB")
        raw = self.rfile.read(n) if n else b""
        print(self.command, path, raw.decode("utf-8", "replace")[:200], flush=True)
        if self.headers.get("Origin") is not None:
            return self.fail(403, "forbidden_origin", "requests with an Origin header are refused")
        auth = self.headers.get("Authorization", "")
        if not hmac.compare_digest(auth.encode(), ("Bearer " + TOKEN).encode()):
            return self.fail(401, "unauthorized", "bad or missing token")
        if not path.startswith("/a/v1/"):
            return self.fail(404, "not_found", "unknown path")
        if raw:
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self.fail(415, "unsupported_media_type", "use application/json")
            try:
                body = json.loads(raw)
            except ValueError:
                return self.fail(400, "bad_request", "body is not JSON")
        parts = path[len("/a/v1/"):].split("/")
        m = self.command
        if parts == ["info"] and m == "GET":
            info = {"server": "orbtile", "api": 1, "version": "mock", "activities": ACTIVITIES,
                    "limits": {"maxBodyBytes": 65536, "maxSessionsPerAdapter": 32, "maxAdapters": 8,
                               "requestsPerSecond": 20, "burst": 60}}
            nonce = self.headers.get("X-Orbtile-Nonce")
            if nonce and re.fullmatch(r"[0-9a-f]{16,64}", nonce):
                info["proof"] = hmac.new(TOKEN.encode(), nonce.encode(), hashlib.sha256).hexdigest()
            return self.reply(200, info)
        if len(parts) < 2 or parts[0] != "adapters":
            return self.fail(404, "not_found", "unknown path")
        aid = parts[1]
        if not ID_RE.match(aid) or not 3 <= len(aid) <= 64 or aid.startswith("com.orbtile."):
            return self.fail(409, "reserved_id", "adapter ids need a dot and must not start with com.orbtile.")
        if len(parts) == 2 and m == "PUT":
            pal = body.get("palette")
            if not isinstance(body.get("name"), str) or not isinstance(pal, dict):
                return self.fail(422, "invalid_field", "name and palette are required", "palette")
            hue = pal.get("baseHue")
            if not isinstance(hue, (int, float)) or not 0 <= hue < 360:
                return self.fail(422, "invalid_field", "palette.baseHue must be in 0..<360", "palette.baseHue")
            bad = check_url_schemes(body)
            if bad:
                return self.fail(*bad)
            warnings = [f"unknown field '{k}'" for k in body if k not in
                        ("name", "shortName", "palette", "glyph", "version", "homepage", "urlSchemes")]
            h0 = resolved_hue(float(hue))
            if h0 != hue:
                warnings.append(f"baseHue {hue:.1f} is inside the error band (30.9 +/- 45): pushed to {h0:.1f}")
            status = 200 if aid in adapters else 201
            adapters[aid] = body
            sessions.setdefault(aid, {})
            return self.reply(status, {"adapter": {"id": aid, "palette": pal, "resolvedBaseHue": h0},
                                       "enabled": True, "warnings": warnings})
        if aid not in adapters:
            return self.fail(404, "adapter_not_registered", "register with PUT /adapters/{id} first")
        if len(parts) == 2 and m == "DELETE":
            adapters.pop(aid, None); sessions.pop(aid, None)
            return self.reply(204)
        if parts[2:] == ["heartbeat"] and m == "POST":
            return self.reply(204)
        if parts[2:] == ["sessions"] and m == "PUT":
            listed = body.get("sessions")
            if not isinstance(listed, list) or len(listed) > 32:
                return self.fail(422, "invalid_field", "sessions: array of at most 32", "sessions")
            for i, s in enumerate(listed):
                if s.get("activity") not in ACTIVITIES:
                    return self.fail(422, "invalid_field", f"activity: unknown value {s.get('activity')!r}", "activity")
                bad = check_open_url(s, f"sessions[{i}].", adapters[aid].get("urlSchemes") or [])
                if bad:
                    return self.fail(*bad)
            sessions[aid] = {s["id"]: s for s in listed}
            return self.reply(200, {})
        if len(parts) == 4 and parts[2] == "sessions":
            sid = parts[3]
            if m == "DELETE":
                sessions[aid].pop(sid, None)
                return self.reply(204)
            if m == "POST":
                if "activity" in body and body["activity"] not in ACTIVITIES:
                    return self.fail(422, "invalid_field", f"activity: unknown value {body['activity']!r}", "activity")
                bad = check_open_url(body, "", adapters[aid].get("urlSchemes") or [])
                if bad:
                    return self.fail(*bad)
                if sid not in sessions[aid] and not ("title" in body and "activity" in body):
                    return self.fail(422, "invalid_field", "a new session needs title and activity", "title")
                cur = sessions[aid].setdefault(sid, {})
                for k, v in body.items():
                    if v is None:
                        cur.pop(k, None)
                    else:
                        cur[k] = v
                return self.reply(200, {"session": {"uid": f"{aid}/{body.get('host', 'local')}/{sid}",
                                                    "visible": True}})
        return self.fail(404, "not_found", "unknown path")

    do_GET = do_PUT = do_POST = do_DELETE = handle_any


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: mock-orbtile.py <discovery-file-path>")
    api_file = os.path.abspath(sys.argv[1])
    run_dir = os.path.dirname(api_file)
    os.makedirs(run_dir, mode=0o700, exist_ok=True)
    sock_dir = None
    sock = os.path.join(run_dir, "mock.sock")
    if len(sock.encode()) > MAX_SOCKET_PATH:
        # Deep discovery directory: the path would not fit in sun_path. Bind in a short private
        # directory instead; the discovery file names the real socket, which is all clients read.
        sock_dir = tempfile.mkdtemp(prefix="orbmock-", dir="/tmp")   # 0700
        sock = os.path.join(sock_dir, "mock.sock")
    if os.path.exists(sock):
        os.unlink(sock)
    server = socketserver.UnixStreamServer(sock, Handler)
    os.chmod(sock, 0o600)
    fd = os.open(api_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"api": 1, "socketPath": sock, "basePath": "/a/v1", "token": TOKEN, "pid": os.getpid()}, f)
    print("mock Orbtile on", sock, "- discovery file", api_file, flush=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # so the finally block removes both files
    try:
        server.serve_forever()
    finally:
        os.unlink(api_file)
        os.unlink(sock)
        if sock_dir:
            os.rmdir(sock_dir)


if __name__ == "__main__":
    main()
