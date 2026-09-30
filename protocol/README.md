# Orbtile Adapter Protocol v1

> **Status: v1.** Fields change only by the additive rules in §4. The API is off by default: the user turns on the
> "Adapter API" switch in Orbtile › Settings › Agents › Adapter API, and then allows each new adapter. To develop
> without Orbtile, use the mock (§8). This kit includes standalone tests against a local fake server.

The protocol lets any local process show its sessions on the Orbtile keypad and in the menu-bar popover. It is how
third parties add an agent to Orbtile. You do not ship code into the app. Your adapter is a separate process that sends
small JSON messages.

- Machine-readable bodies: [`adapter-protocol-v1.schema.json`](adapter-protocol-v1.schema.json) (JSON Schema 2020-12).
- Reference adapters: [`examples/`](../examples/) (shell + curl, Python stdlib, Node 18+). Copy
  them: they handle a regenerated token, an Orbtile relaunch and awkward text.
- Palette rules (how your base hue becomes key colours): [`PALETTE.md`](PALETTE.md).

## 1. Transport

- **HTTP/1.1 over a Unix-domain socket.** No TCP port. Browsers and other macOS users cannot reach it. Only the
  socket serves the API: `/a/` on a TCP port answers a plain `404`.
- The socket is the one Orbtile's own hooks use. Its path is in the discovery file (§2). Do not hard-code it: when the
  normal path is too long for a socket (104 bytes), Orbtile uses a path under `$TMPDIR` instead.
- The socket file is mode 0600 in a 0700 directory. Orbtile also checks that the peer runs as the same user.
- Request target: `basePath` from the discovery file (`/a/v1`) plus the endpoint path, for example `/a/v1/info`.
  Send `Host: localhost` (exactly; anything else gets `400`).
- Every request, `GET` included: `Content-Type: application/json` (a `charset` parameter is fine) and
  `Authorization: Bearer <token>`. Do not send an `Origin` header (such requests get `403 forbidden_origin`, before
  the token is looked at).
- Send bodies with `Content-Length`. Chunked bodies get `411`, bodies over 64 KB get `413`. Both answers come as soon
  as the headers are in, before Orbtile reads the body.
- Orbtile closes the connection after every response. Open a new connection per call. At most 8 adapter connections
  can be open at once; the 9th gets `503 limit_exceeded` with `Retry-After`.

How to connect in three languages:

| Client | How |
|---|---|
| curl | `curl --unix-socket "$SOCK" "http://localhost/a/v1/info"` |
| Python (stdlib) | `http.client.HTTPConnection` whose `connect()` opens `socket.AF_UNIX` on the socket path (see `ref.py`) |
| Node (18+) | `http.request({ socketPath, path: "/a/v1/info", ... })` (see `ref.mjs`). `fetch()` cannot use a Unix socket |

## 2. Discovery and auth

File: `~/Library/Application Support/Orbtile/adapter-api.json`, mode 0600, directory 0700. Tests and tools can
point `ORBTILE_API_FILE` at another path.

```json
{ "api": 1,
  "socketPath": "<socket path from Orbtile>",
  "basePath": "/a/v1",
  "token": "<43-char base64url, 256 bit>",
  "pid": 4242 }
```

- Orbtile writes the file when the API is on and the socket is listening. It removes the file on quit and when the
  API is turned off. **No file means "off or not running".** Back off; do not spin.
- The token is separate from the hook token. It changes when the user presses "Regenerate token" or turns the API
  on again. On `401`, read the file again once and retry.
- The token goes only in the `Authorization` header. Never put it in a URL or on a command line (`ps` shows argv).
  The shell example passes it to curl through `-K <(printf ...)`.
- Server check: `GET /info` with `X-Orbtile-Nonce: <16..64 hex chars>` returns
  `proof = hex(HMAC-SHA256(key: token, message: nonce))`. Only the real Orbtile knows the token. The Python and Node
  examples check the proof before they send session data.

## 3. Endpoints

| Method + path (after `/a/v1`) | Body | Success |
|---|---|---|
| `GET /info` | — | 200 `Info`: api, version, activities, limits, proof |
| `PUT /adapters/{adapterId}` | `AdapterRegistration` | 201 first time, then 200: `{adapter:{id, palette, resolvedBaseHue}, enabled, reason?, warnings?}` |
| `DELETE /adapters/{adapterId}` | — | 204. Ends its sessions. Settings keeps the adapter as "disconnected" |
| `POST /adapters/{adapterId}/sessions/{sessionId}` | `SessionUpdate` (merge: an absent field keeps its value, `null` clears it) | 200 `{session:{uid, visible}}` |
| `DELETE /adapters/{adapterId}/sessions/{sessionId}` | — | 204 |
| `PUT /adapters/{adapterId}/sessions` | `{sessions:[...]}`, full replace (for pollers) | 200 |
| `POST /adapters/{adapterId}/heartbeat` | — | 204. Renews the TTL of every session of this adapter and keeps the registration alive |

- **Registration lives in memory, and so does the token.** After Orbtile relaunches, the first call gets `401`
  (read the discovery file again and retry), then `404 adapter_not_registered` (register again and retry). The
  relaunched Orbtile has no sessions either: a retried `POST .../sessions/{id}` must carry the whole session
  (`title` and `activity` at least), not only the changed fields. The examples do all of this.
- **Stay in touch: a registration is not forever.** Every request to your `/adapters/{adapterId}` routes counts as a
  sign of life; a heartbeat is the cheapest (`GET /info` does not count). Sessions of an adapter that has been silent for more than 15 s show `offline`,
  whatever their `ttlSeconds`, and come back with your next request. A registration silent for 60 s loses its slot,
  and its sessions go with it (whatever their `ttlSeconds`); the next call gets `404 adapter_not_registered` and you
  must register again and resend the whole session. Settings keeps the adapter listed as "disconnected". Heartbeat
  at least every 10 s while you have sessions. A new token ("Regenerate Token") ends every registration at once, even
  for an adapter that already uses the new token: it gets `404` and registers again.
- **The user decides.** A new adapter id is not shown until the user allows it. Until then the reply is
  `enabled:false, reason:"awaiting_user"` (`"disabled_by_user"` after the user denies it), and Settings › Agents lists
  the adapter with a banner "X wants to show sessions" (Deny / Allow).
  A disabled or not-yet-allowed adapter still gets 200 replies with `visible:false`, so it does not loop on errors.
  The Allow is for the registration the user saw. A changed `name`, `shortName`, `palette` (`baseHue`, `chroma`,
  `lightness` or `attentionHue`), `urlSchemes` or `homepage` needs a new Allow: the reply is `enabled:false,
  reason:"awaiting_user"` again until the user allows it. That also holds after Orbtile relaunches. `version` and
  `glyph` may change freely.
- **Adapter id**: lowercase reverse-DNS that you own, with at least one dot, 3 to 64 bytes (`dev.aider`,
  `com.example.agent`). Ids without a dot belong to the built-in adapters (ids without a dot).
  `com.orbtile.*` is reserved. Both get `409 reserved_id`, so an external adapter can never pose as a built-in
  adapter. Any other id that breaks the pattern gets `422 invalid_field` (`field: "adapterId"`).
- **Session identity** is (adapter id, `host`, session id). `host` defaults to `local`. Send the same `host` on every
  update of a remote session; to end one, use `DELETE .../sessions/{sessionId}?host=<alias>`.
- **Strict values.** A value outside its type, range or length gets `422 invalid_field` with the field name; nothing
  is clamped. Unknown fields are ignored and named in `warnings`. Display text is cleaned before its length is
  checked: control characters become spaces (message text keeps newlines and tabs), and bidi overrides, isolates and
  marks are dropped. Text that is empty once cleaned (only bidi characters, only control characters, or only blanks)
  in a required field (`name`, `title`, ...) gets `422`. A wrong method on a known path gets `405` (`bad_request`).

### Registration

```json
{ "name": "Aider", "shortName": "Aider",
  "palette": { "baseHue": 150, "attentionHue": 330 },
  "glyph": { "symbol": "hammer", "monogram": "Ai" },
  "version": "1.0.0", "homepage": "https://example.com/aider-orbtile",
  "urlSchemes": ["https"] }
```

`palette` is required. `baseHue` is the OKLCH hue (degrees) of your working orb. `chroma` (0.5 to 1.3) and
`lightness` (−0.05 to +0.05) are optional. `attentionHue` (0 to <360, optional) is your alarm colour: the hue of the
`waitingPermission` orb. Without it, waiting is derived from `baseHue`; set it when your base hue is green, cyan or
blue and the reply warns `attention: ... set attentionHue`. The user can override the base hue in Settings; the reply
carries the effective value. A hue inside the reserved arc 345.9°–75.9° is moved to the nearer edge, and `warnings`
says so; an `attentionHue` inside 18.9°–55° is moved the same way. The palette
generator's notes (gamut clips, salience guard, WCAG lifts) also go to `warnings`. See
[PALETTE.md](PALETTE.md) before you pick a hue.

### Session update

```json
{ "title": "fix flaky test", "activity": "tool", "toolLabel": "Bash  npm test", "toolKind": "shell", "cwd": "example-project",
  "model": "gpt-x", "message": {"id": "m7", "text": "..."}, "statusDetail": null, "subagentCount": 0,
  "host": "local", "openURL": "myagent://run/7", "pid": 4242, "background": false, "liveText": true,
  "ttlSeconds": 120 }
```

- `activity` is one of `starting`, `idle`, `thinking`, `tool`, `streaming`, `waitingPermission`, `waitingQuestion`,
  `done`, `error`, `compacting`, `offline`. An unknown value gets `422`.
- A new session needs `title` and `activity`.
- `toolKind` (optional) picks the key's tool motion while `activity` is `tool`: `shell`, `edit`, `read`, `web`,
  `delegate`, `plan`, `integration` or `other`. An unknown value counts as none (a `warnings` entry, not an error);
  without it the key draws the `other` motion.
- Lengths: title 120, toolLabel 80, statusDetail 120, model 64, cwd 1024, message id 128. For message text the server
  keeps the last 4096 bytes. A new `message.id` restarts the typewriter on the key.
- Orbtile assigns the key (slot). A client cannot choose it.
- `openURL` opens when the user taps the key. Its scheme must be listed in your `urlSchemes`. `file`, `javascript`,
  `data`, `orbtile` and `x-apple.systempreferences` are always refused.
- `pid`: a local process of the session. It must be alive and belong to the calling user. On tap, Orbtile brings its
  app to the front.
- `ttlSeconds` (10 to 3600, default 120): with no update or heartbeat for that long, the session goes `offline`. It is
  removed 600 s later. Silence of the whole adapter is stricter than any TTL: more than 15 s without a request and
  every session of the adapter shows `offline`; 60 s and the registration and its sessions are gone (see §3).

## 4. Errors, limits, versions

Error body: `{"error":{"code":"invalid_field","message":"activity: unknown value 'busy'","field":"activity"}}`.

| Code | HTTP |
|---|---|
| `bad_request` | 400 |
| `unauthorized` | 401 |
| `forbidden_origin` | 403 |
| `not_found`, `adapter_not_registered` | 404 |
| `bad_request` (wrong method) | 405 |
| `reserved_id` | 409 |
| `length_required` (chunked bodies are refused) | 411 |
| `payload_too_large` | 413 |
| `unsupported_media_type` | 415 |
| `invalid_field`, `limit_exceeded` | 422 |
| `rate_limited` (+ `Retry-After`) | 429 |
| `internal` | 500 |
| `api_disabled` (+ `Retry-After`) | 503 |
| `limit_exceeded` (+ `Retry-After`): more than 8 adapter connections, or the API queue is full | 503 |

Limits: body 64 KB; 8 external adapters live, 32 remembered; 32 sessions per adapter; 20 requests/s per adapter
(burst 60) and 100 requests/s for all adapters together; at most 8 adapter connections at a time. The API has its own
queue (at most 256 waiting requests), so a busy adapter cannot slow down Orbtile's own hooks.

Versions: the path carries the major version (`/a/v1`). v1 changes only by adding optional fields and new
`info.activities` values. The server ignores unknown request fields and names them in `warnings`. Clients must ignore
unknown response fields. A breaking change gets `/a/v2`, and v1 stays for at least two minor releases.

## 5. Privacy and security

- Orbtile keeps session fields in memory only. It never writes message text, titles or cwd to disk or to logs. Logs
  carry the adapter id, the session uid, the activity and the error code.
- The discovery file holds no session data.
- The token is a same-user capability. It stops other macOS users, sandboxed apps without file access and web pages.
  It does not stop a process that already runs as you (nothing can).

## 6. What v1 cannot do in Orbtile 0.2

- Third-party adapters cannot receive keypad answers, approval decisions or tap events.
- Taps can open a session through a registered URL scheme or bring its application to the front.
- A tap on a `done` key turns it `idle` inside Orbtile only.
- Pinning a session to a key is not exposed.
- This kit supplies reference clients; packaged SDKs, per-adapter tokens and signed manifests are not part of v1.

## 7. Mapping tool events to session updates

| Tool event | v1 call |
|---|---|
| Session starts | `POST sessions/{id}` with `title`, `cwd`, `model`, `activity:"starting"` |
| Work begins | `{activity:"thinking", message:null}` |
| Tool runs | `{activity:"tool", toolLabel, toolKind}` |
| Output arrives | `{activity:"streaming", message:{id, text}}` |
| Permission needed | `{activity:"waitingPermission", statusDetail}` |
| Question for the user | `{activity:"waitingQuestion"}` |
| Context compaction | `compacting`, then the prior state |
| Work finishes | `done` plus `message` |
| Work fails | `error` plus `statusDetail` |
| Work interrupted | `{activity:"idle", statusDetail:"interrupted"}` |
| Subagents start or stop | `{subagentCount}` |
| Session ends | `DELETE sessions/{id}` |
| Polling session list | `PUT sessions` (full replace); `liveText:false` for state-only rows |
| Remote session | Stable `host` alias plus `ttlSeconds` |

## 8. Try it without Orbtile

[`examples/mock-orbtile.py`](../examples/mock-orbtile.py) serves the v1 endpoints on a Unix
socket, writes a discovery file and prints each request. It checks auth and the basic field rules, including the
`urlSchemes` and `openURL` rules of §3 (a scheme that is not registered, or is `file`, `javascript`, `data`, `orbtile` or
`x-apple.systempreferences`, gets `422 invalid_field`), and it draws nothing. When the discovery file sits in a deep
directory, the socket goes to a short private folder under `/tmp` (a Unix socket path holds at most 104 bytes); the
discovery file names it.

```sh
python3 examples/mock-orbtile.py /tmp/orbtile-mock/adapter-api.json &
ORBTILE_API_FILE=/tmp/orbtile-mock/adapter-api.json python3 examples/ref.py
```
