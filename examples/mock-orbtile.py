#!/usr/bin/env python3
"""A stand-in for the Orbtile Adapter API v1.2.

Use it to develop an adapter without Orbtile running. It serves the v1 endpoints on a Unix-domain
socket, writes a discovery file, checks auth and the basic field rules, and prints one line per
request. It keeps state in memory and draws nothing. It is not the real validator: the JSON Schema
and Orbtile itself are authoritative.

  python3 mock-orbtile.py /tmp/orbtile-mock/adapter-api.json
  ORBTILE_API_FILE=/tmp/orbtile-mock/adapter-api.json python3 ref.py

The discovery file path is required on purpose, so the mock never writes into the real
~/Library/Application Support/Orbtile.
"""
import argparse, base64, binascii, hashlib, hmac, http.server, json, os, re, secrets, signal, socketserver, sys, tempfile, threading, time

ACTIVITIES = ["starting", "idle", "thinking", "tool", "streaming", "waitingPermission", "waitingQuestion",
              "done", "error", "compacting", "offline"]
EDITABLE_STATES = {"idle", "starting", "thinking", "tool", "streaming", "done", "compacting", "offline",
                   "waitingPermission", "waitingQuestion"}
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*(\.[a-z0-9]+(-[a-z0-9]+)*)+$")
BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]{0,31}$")
URL_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
# Schemes an openURL may never use, whatever the registration says (ExternalAdapterSource.refusedSchemes).
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


def check_icon(body):
    """Basic v1.2 icon validation; Orbtile additionally decodes and re-encodes the image."""
    if "icon" not in body or body["icon"] is None:
        return None
    icon = body["icon"]
    if not isinstance(icon, dict) or not any(key in icon for key in ("png", "appBundleId")):
        return 422, "invalid_field", "icon: supply png and/or appBundleId", "icon"
    if "png" in icon:
        value = icon["png"]
        if not isinstance(value, str) or not value:
            return 422, "invalid_field", "icon.png: non-empty standard base64 PNG required", "icon.png"
        if len(value) > 43696:
            return 422, "limit_exceeded", "icon.png: base64 exceeds 43696 characters", "icon.png"
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            return 422, "invalid_field", "icon.png: invalid standard base64", "icon.png"
        if len(raw) > 32768:
            return 422, "limit_exceeded", "icon.png: decoded PNG exceeds 32768 bytes", "icon.png"
        if (len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[8:12] != b"\x00\x00\x00\r"
                or raw[12:16] != b"IHDR"):
            return 422, "invalid_field", "icon.png: invalid PNG header or IHDR", "icon.png"
        width, height = int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
        if width != height or not 16 <= width <= 256:
            return 422, "invalid_field", "icon.png: square dimensions must be 16..256 px", "icon.png"
    if "appBundleId" in icon:
        value = icon["appBundleId"]
        if not isinstance(value, str) or not 3 <= len(value) <= 128 or not BUNDLE_ID_RE.fullmatch(value):
            return 422, "invalid_field", "icon.appBundleId: invalid macOS bundle identifier", "icon.appBundleId"
    return None


def check_state_colours(palette):
    """Validate the optional v1.2 state map and normalize accepted values to lowercase."""
    if "states" not in palette or palette["states"] is None:
        return None
    states = palette["states"]
    if not isinstance(states, dict) or not 1 <= len(states) <= 10:
        return 422, "invalid_field", "palette.states: supply 1..10 state colours", "palette.states"
    for state, colour in states.items():
        field = "palette.states." + state
        if state not in EDITABLE_STATES:
            return 422, "invalid_field", field + ": unknown or fixed state", field
        if not isinstance(colour, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", colour):
            return 422, "invalid_field", field + ": #rrggbb required", field
    palette["states"] = {state: colour.lower() for state, colour in states.items()}
    return None


def resolved_hue(h):
    """The reserved-arc push of OrbPalette.make: hues within 45 degrees of 30.9 move to 345.9 or 75.9."""
    arc = (ERROR_HUE - h + 540) % 360 - 180
    if abs(arc) >= ERROR_BAND:
        return h
    return (ERROR_HUE + (-ERROR_BAND if arc > 0 else ERROR_BAND)) % 360


def validate_request(kind, request):
    """Basic schema bounds; production Orbtile is authoritative for sanitization and policy."""
    def text(value, maximum, required=True):
        if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
            raise ValueError("request text is missing or exceeds its bound")
    if kind == "approval":
        text(request.get("toolName"), 80)
        text(request.get("commandSummary"), 512)
        if request["toolName"] in ("AskUserQuestion", "ExitPlanMode", "request_user_input", "request_user_input_async"):
            raise ValueError("interactive tools must use question")
        return
    questions = request.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
        raise ValueError("1..4 questions required")
    for question in questions:
        if not isinstance(question, dict):
            raise ValueError("question must be an object")
        text(question.get("text"), 1024)
        if "header" in question:
            text(question["header"], 12, required=False)
        if "multiSelect" in question and type(question["multiSelect"]) is not bool:
            raise ValueError("multiSelect must be boolean")
        options = question.get("options")
        if not isinstance(options, list) or not 2 <= len(options) <= 4:
            raise ValueError("2..4 options required")
        if len({option.get("label") for option in options if isinstance(option, dict) and isinstance(option.get("label"), str)}) != len(options):
            raise ValueError("option labels must be unique")
        for option in options:
            if not isinstance(option, dict):
                raise ValueError("option must be an object")
            text(option.get("label"), 120)
            if "description" in option:
                text(option["description"], 512, required=False)


class EventState:
    """In-process development model, not a replacement for Orbtile's full validator.

    No decision is enabled by default. The explicit --events mock demo simulates a user
    opt-in and keypad actions; that flag never affects a real Orbtile installation.
    """
    def __init__(self, clock=time.time):
        self.clock = clock
        self.adapters = {}
        self.condition = threading.Condition(threading.RLock())

    def register(self, aid, capabilities, token=None):
        with self.condition:
            old = self.adapters.get(aid)
            if old is not None:
                self.authorize(aid, token)
                if old["capabilities"] != set(capabilities):
                    old["policy"] = False
                    old["events"] = []
                    for current in old["sessions"].values():
                        current["pending"] = None
                        current["fields"].pop("approval", None)
                        current["fields"].pop("question", None)
                old["capabilities"] = set(capabilities)
                if capabilities and old["token"] is None:
                    old["token"] = secrets.token_urlsafe(32)
                return old["token"]
            generated = secrets.token_urlsafe(32) if capabilities else None
            self.adapters[aid] = {"token": generated, "capabilities": set(capabilities), "policy": False,
                                  "sequence": 0, "delivered": 0, "ack": 0, "events": [],
                                  "sessions": {}, "history": set(), "polling": False}
            return generated

    def authorize(self, aid, token):
        state = self.adapters[aid]
        if state["token"] and (not isinstance(token, str)
                              or not hmac.compare_digest(state["token"], token)):
            raise PermissionError("wrong or missing adapter token")
        return state

    def set_policy(self, aid, enabled):
        with self.condition:
            state = self.adapters[aid]
            revoked = state["policy"] and not enabled
            state["policy"] = enabled
            if revoked:
                for current in state["sessions"].values():
                    current["decided"] = True
            self._prune(state)

    def update(self, aid, sid, fields):
        with self.condition:
            state = self.adapters[aid]
            key = (sid, fields.get("host", "local"))
            current = state["sessions"].get(key, {"fields": {}, "pending": None})
            merged = dict(current["fields"])
            merged.update(fields)
            explicit = any(kind in fields for kind in ("approval", "question"))
            approval, question = fields.get("approval"), fields.get("question")
            if approval is not None and question is not None:
                raise ValueError("only one pending request per session")
            pending = current["pending"]
            decided = current.get("decided", False)
            if explicit:
                request = approval if approval is not None else question
                kind = "approval" if approval is not None else "question"
                merged.pop("approval", None)
                merged.pop("question", None)
                if request is not None:
                    capability = "approvals" if kind == "approval" else "questions"
                    activity = "waitingPermission" if kind == "approval" else "waitingQuestion"
                    if capability not in state["capabilities"] or merged.get("activity") != activity:
                        raise ValueError("request needs capability and matching waiting activity")
                    rid = request.get("id") if isinstance(request, dict) else None
                    if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", rid):
                        raise ValueError("invalid request id")
                    validate_request(kind, request)
                    if pending and pending[1]["id"] == rid:
                        if pending[0] != kind or pending[1] != request:
                            raise ValueError("request id contents changed")
                    else:
                        if rid in state["history"]:
                            raise ValueError("request id was already used")
                        if len(state["history"]) >= 4096:
                            raise ValueError("request history limit reached")
                        if sum(s["pending"] is not None for s in state["sessions"].values()) >= 32 and not pending:
                            raise ValueError("pending request limit reached")
                        state["history"].add(rid)
                        pending = (kind, json.loads(json.dumps(request)), self.clock() + (120 if kind == "approval" else 600))
                        decided = False
                    merged[kind] = request
                else:
                    pending = None
            if pending:
                activity = "waitingPermission" if pending[0] == "approval" else "waitingQuestion"
                if merged.get("activity") != activity:
                    pending = None
                    merged.pop("approval", None)
                    merged.pop("question", None)
            state["sessions"][key] = {"fields": merged, "pending": pending, "decided": decided}
            self._prune(state)

    def _prune(self, state):
        def current(event):
            session = state["sessions"].get((event["sessionId"], event["host"]))
            if session is None:
                return False
            if event["type"] == "tap":
                return True
            pending = session["pending"]
            return (state["policy"] and pending is not None and pending[0] == event["type"]
                    and pending[1]["id"] == event["requestId"]
                    and self.clock() < event["expiresAt"])
        state["events"] = [event for event in state["events"] if current(event)]

    def clear(self, aid, sid, host="local"):
        with self.condition:
            state = self.adapters[aid]
            state["sessions"].pop((sid, host), None)
            self._prune(state)

    def _enqueue(self, state, sid, host, kind, **fields):
        self._prune(state)
        if len(state["events"]) >= 128:
            return False
        state["sequence"] += 1
        state["events"].append({"sequence": state["sequence"], "type": kind,
                                "sessionId": sid, "host": host, **fields})
        self.condition.notify_all()
        return True

    def tap(self, aid, sid, host="local"):
        with self.condition:
            state = self.adapters[aid]
            if "tap" not in state["capabilities"] or (sid, host) not in state["sessions"]:
                return False
            return self._enqueue(state, sid, host, "tap")

    def _pending(self, state, sid, host, rid, kind):
        current = state["sessions"].get((sid, host))
        pending = current and current["pending"]
        activity = "waitingPermission" if kind == "approval" else "waitingQuestion"
        if (not state["policy"] or not pending or current.get("decided", False) or pending[0] != kind
                or pending[1]["id"] != rid or self.clock() >= pending[2]
                or current["fields"].get("activity") != activity):
            return None
        return pending

    def decide(self, aid, sid, rid, decision, host="local"):
        with self.condition:
            state = self.adapters[aid]
            pending = self._pending(state, sid, host, rid, "approval")
            if decision not in ("allow", "deny") or pending is None:
                return False
            if not self._enqueue(state, sid, host, "approval", requestId=rid,
                                 decision=decision, expiresAt=pending[2]):
                return False
            state["sessions"][(sid, host)]["decided"] = True
            return True

    def answer(self, aid, sid, rid, picks, host="local"):
        with self.condition:
            state = self.adapters[aid]
            pending = self._pending(state, sid, host, rid, "question")
            if pending is None:
                return False
            questions = pending[1]["questions"]
            if not isinstance(picks, list) or len(picks) != len(questions):
                return False
            for question, chosen in zip(questions, picks):
                if (not isinstance(chosen, list) or not chosen
                        or any(type(i) is not int or not 0 <= i < len(question["options"]) for i in chosen)
                        or len(set(chosen)) != len(chosen)
                        or (not question.get("multiSelect", False) and len(chosen) != 1)):
                    return False
            if not self._enqueue(state, sid, host, "question", requestId=rid,
                                 picks=picks, expiresAt=pending[2]):
                return False
            state["sessions"][(sid, host)]["decided"] = True
            return True

    def simulate(self, aid):
        """Explicit demo only: pretend the user tapped, then answered in the mock keypad."""
        with self.condition:
            state = self.adapters[aid]
            self.set_policy(aid, True)
            for (sid, host), current in list(state["sessions"].items()):
                pending = current["pending"]
                if pending and not current.get("decided", False):
                    self.tap(aid, sid, host)
                    if pending[0] == "approval":
                        self.decide(aid, sid, pending[1]["id"], "deny", host)
                    else:
                        self.answer(aid, sid, pending[1]["id"], [[0] for _ in pending[1]["questions"]], host)

    def poll(self, aid, token, after=0, ack=0, wait=0):
        with self.condition:
            state = self.authorize(aid, token)
            if state["token"] is None:
                raise PermissionError("register event capabilities first")
            if (type(after) is not int or type(ack) is not int
                    or not state["ack"] <= ack <= after <= state["delivered"]
                    or type(wait) is not int or not 0 <= wait <= 2):
                raise ValueError("ack <= after <= highest delivered; cumulative ack cannot decrease; wait 0..2")
            if state["polling"]:
                raise ValueError("one outstanding poll per adapter")
            self._prune(state)
            state["ack"] = ack
            state["events"] = [e for e in state["events"] if e["sequence"] > ack]
            state["polling"] = True
            try:
                self.condition.wait_for(lambda: any(e["sequence"] > after for e in state["events"]), timeout=wait)
                self._prune(state)
                events = [dict(e) for e in state["events"] if e["sequence"] > after]
                if events:
                    state["delivered"] = max(state["delivered"], events[-1]["sequence"])
                return {"events": events, "lastSequence": state["sequence"], "acknowledged": state["ack"]}
            finally:
                state["polling"] = False


EVENTS = EventState()
DEMO_EVENTS = False


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
        if not isinstance(body, dict):
            return self.fail(400, "bad_request", "body must be a JSON object")
        parts = path[len("/a/v1/"):].split("/")
        m = self.command
        if parts == ["info"] and m == "GET":
            info = {"server": "orbtile", "api": 1, "apiMinor": 2, "version": "mock", "activities": ACTIVITIES,
                    "limits": {"maxBodyBytes": 65536, "maxSessionsPerAdapter": 32, "maxAdapters": 8,
                               "requestsPerSecond": 20, "burst": 60, "maxEvents": 128, "maxRequestIDs": 4096}}
            nonce = self.headers.get("X-Orbtile-Nonce")
            if nonce and re.fullmatch(r"[0-9a-f]{16,64}", nonce):
                info["proof"] = hmac.new(TOKEN.encode(), nonce.encode(), hashlib.sha256).hexdigest()
            return self.reply(200, info)
        if len(parts) < 2 or parts[0] != "adapters":
            return self.fail(404, "not_found", "unknown path")
        aid = parts[1]
        if not ID_RE.match(aid) or not 3 <= len(aid) <= 64 or aid.startswith("com.orbtile."):
            return self.fail(409, "reserved_id", "adapter ids need a dot and must not start with com.orbtile.")
        adapter_token = self.headers.get("X-Orbtile-Adapter-Token")
        if aid in adapters:
            try:
                EVENTS.authorize(aid, adapter_token)
            except PermissionError:
                return self.fail(401, "unauthorized", "wrong or missing adapter token")
        if len(parts) == 2 and m == "PUT":
            capabilities = body.get("capabilities", [])
            if (not isinstance(capabilities, list) or any(c not in ("tap", "approvals", "questions") for c in capabilities)
                    or len(capabilities) > 3 or len(set(capabilities)) != len(capabilities)):
                return self.fail(422, "invalid_field", "capabilities: unique tap/approvals/questions", "capabilities")
            if (body.get("tapBehavior", "alsoOpen") not in ("alsoOpen", "eventOnly")
                    or ("tapBehavior" in body and "tap" not in capabilities)):
                return self.fail(422, "invalid_field", "tapBehavior: alsoOpen or eventOnly", "tapBehavior")
            pal = body.get("palette")
            if not isinstance(body.get("name"), str) or not isinstance(pal, dict):
                return self.fail(422, "invalid_field", "name and palette are required", "palette")
            hue = pal.get("baseHue")
            if not isinstance(hue, (int, float)) or not 0 <= hue < 360:
                return self.fail(422, "invalid_field", "palette.baseHue must be in 0..<360", "palette.baseHue")
            bad = check_state_colours(pal) or check_icon(body) or check_url_schemes(body)
            if bad:
                return self.fail(*bad)
            warnings = [f"unknown field '{k}'" for k in body if k not in
                        ("name", "shortName", "palette", "glyph", "icon", "version", "homepage", "urlSchemes",
                         "capabilities", "tapBehavior")]
            if isinstance(body.get("icon"), dict):
                warnings.extend(f"unknown field 'icon.{k}' ignored" for k in body["icon"]
                                if k not in ("png", "appBundleId"))
            h0 = resolved_hue(float(hue))
            if h0 != hue:
                warnings.append(f"baseHue {hue:.1f} is inside the error band (30.9 +/- 45): pushed to {h0:.1f}")
            status = 200 if aid in adapters else 201
            event_token = EVENTS.register(aid, capabilities, adapter_token)
            adapters[aid] = body
            sessions.setdefault(aid, {})
            with EVENTS.condition:
                keypad = bool(EVENTS.adapters.get(aid, {}).get("policy"))
            reply = {"adapter": {"id": aid, "palette": pal, "resolvedBaseHue": h0},
                     "enabled": True, "keypadDecisions": keypad, "warnings": warnings}
            if event_token:
                reply["adapterToken"] = event_token
            return self.reply(status, reply)
        if aid not in adapters:
            return self.fail(404, "adapter_not_registered", "register with PUT /adapters/{id} first")
        if len(parts) == 2 and m == "DELETE":
            adapters.pop(aid, None); sessions.pop(aid, None)
            with EVENTS.condition:
                EVENTS.adapters.pop(aid, None)
                EVENTS.condition.notify_all()
            return self.reply(204)
        if parts[2:] == ["events"] and m == "POST":
            try:
                if DEMO_EVENTS:
                    EVENTS.simulate(aid)
                result = EVENTS.poll(aid, adapter_token, body.get("after", 0), body.get("ack", 0),
                                     body.get("waitSeconds", 0))
            except PermissionError:
                return self.fail(401, "unauthorized", "register event capabilities first")
            except ValueError as error:
                return self.fail(422, "invalid_field", str(error), "events")
            return self.reply(200, result)
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
            try:
                for session in listed:
                    EVENTS.update(aid, session["id"], session)
                keep = {(session["id"], session.get("host", "local")) for session in listed}
                for sid, host in list(EVENTS.adapters[aid]["sessions"]):
                    if (sid, host) not in keep:
                        EVENTS.clear(aid, sid, host)
            except ValueError as error:
                return self.fail(422, "invalid_field", str(error), "sessions")
            sessions[aid] = {s["id"]: s for s in listed}
            return self.reply(200, {})
        if len(parts) == 4 and parts[2] == "sessions":
            sid = parts[3]
            if m == "DELETE":
                sessions[aid].pop(sid, None)
                EVENTS.clear(aid, sid)
                return self.reply(204)
            if m == "POST":
                if "activity" in body and body["activity"] not in ACTIVITIES:
                    return self.fail(422, "invalid_field", f"activity: unknown value {body['activity']!r}", "activity")
                bad = check_open_url(body, "", adapters[aid].get("urlSchemes") or [])
                if bad:
                    return self.fail(*bad)
                if sid not in sessions[aid] and not ("title" in body and "activity" in body):
                    return self.fail(422, "invalid_field", "a new session needs title and activity", "title")
                try:
                    EVENTS.update(aid, sid, body)
                except ValueError as error:
                    return self.fail(422, "invalid_field", str(error), "approval/question")
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
    global DEMO_EVENTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("discovery_file")
    parser.add_argument("--events", action="store_true", help="simulate user opt-in, tap, Deny and question answer")
    args = parser.parse_args()
    DEMO_EVENTS = args.events
    api_file = os.path.abspath(args.discovery_file)
    run_dir = os.path.dirname(api_file)
    os.makedirs(run_dir, mode=0o700, exist_ok=True)
    sock_dir = None
    sock = os.path.join(run_dir, "mock.sock")
    if len(sock.encode()) > MAX_SOCKET_PATH:
        # Deep discovery directory: the path would not fit in sun_path. Bind in a short private
        # directory instead; the discovery file names the real socket, which is all clients read.
        sock_dir = tempfile.mkdtemp(prefix="orbmock-", dir=os.environ.get("TMPDIR", "/tmp"))   # 0700
        sock = os.path.join(sock_dir, "mock.sock")
    if os.path.exists(sock):
        os.unlink(sock)
    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
    server = Server(sock, Handler)
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
