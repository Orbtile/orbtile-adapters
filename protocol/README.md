# Orbtile Adapter Protocol v1.2

> **Status: v1.2.** This additive extension keeps `/a/v1`, discovery `api:1`, and unchanged v1.0/v1.1 registrations
> working. Fields change only by the additive rules in §4. The API is off by default: the user turns on the
> "Adapter API" switch in Orbtile › Settings › Agents › Adapter API, and then allows each new adapter. To develop
> without Orbtile, use the mock (§8). This kit includes standalone tests against a local fake server.

The protocol lets any local process show its sessions on the Orbtile keypad and in the menu-bar popover. It is how
third parties add an agent to Orbtile. You do not ship code into the app. Your adapter is a separate process that sends
small JSON messages.

- Machine-readable bodies: [`adapter-protocol-v1.schema.json`](adapter-protocol-v1.schema.json) (JSON Schema 2020-12).
- Reference adapters: [example guide](../examples/README.md) (shell + curl, Python stdlib, Node 18+). Copy
  them: they handle a regenerated token, an Orbtile relaunch and awkward text.
- Palette rules (how your base hue becomes key colours): [`PALETTE.md`](PALETTE.md).

## 1. Transport

- **HTTP/1.1 over a Unix-domain socket on macOS, or loopback TCP on Windows.** There is no public listener. Only the
  socket serves the API on macOS: `/a/` on a TCP port answers a plain `404` there.
- The macOS socket is the one Orbtile's own hooks use. Its path is in the discovery file (§2). Do not hard-code it: when
  the normal path is too long for a socket (104 bytes), Orbtile uses a path under `$TMPDIR` instead. On Windows, use the
  discovery file's `url` and `port`; these identify only its `127.0.0.1` listener.
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

On macOS the file is `~/Library/Application Support/Orbtile/adapter-api.json`, mode 0600, directory 0700;
Orbtile Enterprise uses `~/Library/Application Support/Orbtile Enterprise/adapter-api.json`. Reference adapters use
Standard, or Enterprise only when Standard is missing.

On Windows the packaged Microsoft Store app writes `%USERPROFILE%\.orbtile\adapter-api.json` so unpackaged tools can
find it. Standard and Enterprise MSI keep `%LOCALAPPDATA%\Orbtile\Windows\adapter-api.json` and
`%LOCALAPPDATA%\Orbtile Enterprise\Windows\adapter-api.json`. Windows clients search Store, Standard MSI, then
Enterprise MSI and use the first existing file; when none exists they use the Store path. Windows protects the
containing directory and file with current-user-only ACLs. On both platforms, `ORBTILE_API_FILE` overrides the defaults.

```json
{ "api": 1,
  "socketPath": "/Users/me/Library/Application Support/Orbtile/run/hooks.sock",
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
| `GET /info` | none | 200 `Info`: api, apiMinor, version, activities, limits, proof |
| `PUT /adapters/{adapterId}` | `AdapterRegistration` | 201 first time, then 200: `{adapter:{id, palette, resolvedBaseHue}, enabled, reason?, warnings?}` |
| `DELETE /adapters/{adapterId}` | none | 204. Ends its sessions. Settings keeps the adapter as "disconnected" |
| `POST /adapters/{adapterId}/sessions/{sessionId}` | `SessionUpdate` (merge: an absent field keeps its value, `null` clears it) | 200 `{session:{uid, visible}}` |
| `DELETE /adapters/{adapterId}/sessions/{sessionId}` | none | 204 |
| `PUT /adapters/{adapterId}/sessions` | `{sessions:[...]}`, full replace (for pollers) | 200 |
| `POST /adapters/{adapterId}/heartbeat` | none | 204. Renews the TTL of every session of this adapter and keeps the registration alive |
| `POST /adapters/{adapterId}/events` | `EventsPoll`: `{after,ack,waitSeconds}` | 200 `EventsReply`: `{events,lastSequence,acknowledged}` (v1.1) |

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
  `lightness`, `attentionHue` or `states`), `icon`, `urlSchemes`, `homepage`, `capabilities` or `tapBehavior` needs a
  new Allow: the reply is `enabled:false, reason:"awaiting_user"` again until the user allows it. That also holds after
  Orbtile relaunches. `version` and `glyph` may change freely.
- **Adapter id**: lowercase reverse-DNS that you own, with at least one dot, 3 to 64 bytes (`dev.aider`,
  `com.example.agent`). Ids without a dot belong to the built-in adapters (`claude-code`, `codex`, `zcode`).
  `com.orbtile.*` is reserved. Both get `409 reserved_id`, so an external adapter can never pose as a built-in
  adapter. Any other id that breaks the pattern gets `422 invalid_field` (`field: "adapterId"`).
- **Session identity** is (adapter id, `host`, session id). `host` defaults to `local`. Send the same `host` on every
  update of a remote session; to end one, use `DELETE .../sessions/{sessionId}?host=<alias>`. Any `host` other than
  `local` marks the session as remote: a globe appears with it on the key, in the menu-bar popover and on the phone
  pad. The globe does not change taps: `openURL` still opens when the user taps the key.
- **Strict values.** A value outside its type, range or length gets `422 invalid_field` with the field name; nothing
  is clamped. Unknown fields are ignored and named in `warnings`. Display text is cleaned before its length is
  checked: control characters become spaces (message text keeps newlines and tabs), and bidi overrides, isolates and
  marks are dropped. Text that is empty once cleaned (only bidi characters, only control characters, or only blanks)
  in a required field (`name`, `title`, ...) gets `422`. A wrong method on a known path gets `405` (`bad_request`).

### Registration

```json
{ "name": "Example Agent", "shortName": "Example",
  "palette": {
    "baseHue": 270,
    "states": {
      "thinking": "#eab308",
      "waitingPermission": "#ef4444",
      "done": "#22c55e",
      "idle": "#6b7280"
    }
  },
  "glyph": { "monogram": "EA" },
  "icon": { "appBundleId": "com.example.agent" },
  "version": "1.0.0", "homepage": "https://example.com/example-agent",
  "urlSchemes": ["https"] }
```

`palette` is required. `baseHue` is the OKLCH hue (degrees) of your working orb. `chroma` (0.5 to 1.3) and
`lightness` (−0.05 to +0.05) are optional. `attentionHue` (0 to <360, optional) is your alarm colour: the hue of the
`waitingPermission` orb. Without it, waiting is derived from `baseHue`; set it when your base hue is green, cyan or
blue and the reply warns `attention: ... set attentionHue`. A base hue inside the reserved arc 345.9° to 75.9° is moved
to the nearer edge, and `warnings` says so; an `attentionHue` inside 18.9° to 55° is moved the same way. The palette
generator's notes (gamut clips, salience guard, WCAG lifts) also go to `warnings`. See
[PALETTE.md](PALETTE.md) before you pick a hue.

Protocol v1.2 adds optional `icon` and `palette.states` fields. Check `GET /info` for `apiMinor >= 2` before relying on
them. Older hosts accept the registration, ignore these fields with warnings, and use the glyph and `baseHue` palette.
Omitting both fields preserves v1.0/v1.1 registration and fingerprint behaviour. Adding or changing either field
requires a new user Allow, so an approved adapter cannot silently change its icon or state colours.

#### Adapter icon (v1.2)

`icon` is optional. Omit it or send `null` to use `glyph.symbol` / `glyph.monogram`; either form also clears a custom
icon from an earlier registration. An icon object must contain at least one of these fields:

| Field | Rules |
|---|---|
| `png` | Standard RFC 4648 base64 with no whitespace, 1...43,696 ASCII characters, decoding to 1...32,768 bytes. The PNG signature and IHDR must be valid, with square dimensions `16 <= width == height <= 256` px. |
| `appBundleId` | A 3...128-byte macOS bundle identifier matching `^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$`. Paths, whitespace, empty segments and `..` are invalid. |

Both fields may be supplied. Both are validated at registration. On macOS, an installed `appBundleId` takes precedence;
`png` is the fallback when the app is absent. Without a PNG or an installed app, the glyph is used. Windows validates
the bundle identifier but uses `png`; a bundle identifier alone falls back to the glyph there. Unknown keys inside
`icon` are ignored and reported as `unknown field 'icon.<key>' ignored`.

Orbtile does not render the uploaded PNG directly. It decodes the image, strips metadata, and re-encodes 28×28 and
56×56 sRGB PNGs, each at most 16,384 bytes. Icons follow the user's key-glyph and provider-app-icon settings. Invalid
icon objects and fields get `422 invalid_field` with `field` set to `icon`, `icon.png` or `icon.appBundleId`.
Base64 longer than 43,696 characters, decoded PNG data over 32,768 bytes, or a normalized icon over its output budget
gets `422 limit_exceeded` with `field: "icon.png"`. The existing 65,536-byte request-body limit still applies.

#### State colours (v1.2)

`palette.states` is an optional object or `null`. It contains 1...10 entries; `{}` is invalid. Keys are state raw names,
and values are six-digit sRGB hex strings matching `^#[0-9A-Fa-f]{6}$`, normalized to lowercase `#rrggbb`. Unknown
states, `error`, `empty`, an empty map and malformed values get `422 invalid_field`; the error's `field` identifies
`palette.states` or the exact `palette.states.<state>` entry.

| State | Colour when that entry is omitted |
|---|---|
| `idle`, `starting`, `thinking`, `done`, `compacting`, `offline` | Generated from `baseHue` |
| `tool`, `streaming` | Inherit `states.thinking` when set; otherwise generated from `baseHue` |
| `waitingPermission` | Generated from `attentionHue` or the derived attention hue |
| `waitingQuestion` | Inherits `states.waitingPermission` when set; otherwise uses the attention hue |
| `error`, `empty` | Fixed; adapters cannot set them |

An explicit `tool`, `streaming` or `waitingQuestion` entry takes precedence over inheritance. When **One alarm colour
for all agents** is enabled (`anchorAttention`), both waiting states ignore adapter waiting overrides and use the shared
terracotta. Other state overrides remain active. Dragging the hue ring in Settings › Agents replaces all adapter state
colours with a single-hue palette; resetting the user hue restores them. `error`, `empty` and surface neutrals never
change.

Each override uses the **Hex-Anchored OKLCH ramp**: the input supplies the exact hue and a chroma ratio capped at 2.5,
while lit stops receive a lightness nudge bounded to ±0.08. Explicit overrides bypass the normal error-band push,
attention-hue exclusion and work-salience attenuation. Text and chip colours still meet WCAG AA 4.5:1. For `idle` and
`offline`, the `hi` stop is the exact input hex and no ramp stop gains chroma beyond the input; their other stops keep
the bounded lightness nudge and the same chroma ceiling. Other states retain the normal cap of 2.5 and their anchor can
differ from the input when the lightness nudge reaches its bound.

The registration above sets four state colours. These are the sRGB ramps Orbtile 0.3.4 generates for it, in `deep, base, hi, hot, heart` order; `baseHue` is 270:

| State | Generated ramp |
|---|---|
| `thinking` | `#1e1702, #705600, #eab308, #ffd372, #fffaef` |
| `tool` (inherited) | `#1e1702, #6e5500, #eab308, #ffd475, #fffaef` |
| `streaming` (inherited) | `#231b04, #7a5e00, #eab308, #ffd67e, #fffaef` |
| `waitingPermission` | `#290700, #701600, #ef4444, #ff7372, #ffcece` |
| `waitingQuestion` (inherited) | `#290b03, #711600, #ef4444, #ff7978, #ffc9ca` |
| `done` | `#071c0f, #0a4d27, #5dbe74, #b2e1b8, #daf0dc` |
| `idle` | `#111214, #30333a, #6b7280, #a8b1c0, #d7deeb` |
| `error` (fixed) | `#1e0605, #4e100d, #9e2a22, #c8352b, #f07a60` |
| `empty` (fixed) | `#0b0908` |

#### Developer warnings

Registration can return these exact developer-facing warnings. They do not reject the registration:

```text
icon.appBundleId '{bundleId}' is not installed on this Mac; using the fallback icon
```

The fallback is `icon.png` when supplied, otherwise the glyph.

```text
icon.appBundleId is ignored on Windows; send icon.png to show an icon on every platform
```

```text
state '{state}' hue {hue} is within 15° of the error hue (30.9°), so it can be hard to tell apart from Error
```

The last warning is informational: Orbtile does not move an explicitly overridden state hue away from the error hue.

#### Empty-key URL (host setting, not an adapter field)

The empty-key URL is a **host-side user setting** in Settings › Keypad › Keys. It is not part of
`AdapterRegistration`, and there is no adapter or PhonePad wire change. Do not send `emptyKeyURL` in a registration.
The host opens the configured URL on the host computer when the user taps an empty session slot on the keypad, in the
popover or on a phone.

An empty or ASCII-whitespace-only value disables the action. After trimming outer ASCII spaces, tabs, CR and LF, the
URL is limited to 512 UTF-8 bytes and must be an absolute RFC 3986 URI with a 1...32-character lowercase-normalized
scheme matching `^[a-z][a-z0-9+.-]{0,31}$`. Embedded whitespace, ASCII control characters and userinfo are rejected.
`https` requires a non-empty host. Plain `http` is allowed only for `localhost`, `127.0.0.1` or `[::1]`. Other
syntactically valid application schemes are allowed except this refused set:

```text
file javascript data vbscript about blob orbtile x-apple.systempreferences
ms-settings ms-msdt search-ms ms-appinstaller ms-cxh ms-cxh-full ms-officecmd
shell mk res its ms-its mhtml ftp smb afp nfs dav davs telnet
```

Validation runs when the setting is saved and again before opening. Only genuinely empty slots 0...7 can open it: the
Hub key 8, notices, topics, focused or subagent views, demo mode, locked workstations and long holds do not launch it.
A 250 ms cooldown prevents key-bounce launches. The setting is independent of the session-key press action and is
excluded from settings sync and import.

### Session update

```json
{ "title": "fix flaky test", "activity": "tool", "toolLabel": "Bash  npm test", "toolKind": "shell", "cwd": "/Users/me/app",
  "model": "gpt-x", "message": {"id": "m7", "text": "..."}, "statusDetail": null, "subagentCount": 0,
  "host": "local", "openURL": "myagent://run/7", "pid": 4242, "background": false, "liveText": true,
  "ttlSeconds": 120 }
```

- `activity` is one of `starting`, `idle`, `thinking`, `tool`, `streaming`, `waitingPermission`, `waitingQuestion`,
  `done`, `error`, `compacting`, `offline`. An unknown value gets `422`.
- A new session needs `title` and `activity`.
- `project` (optional, Orbtile 0.2.17+) is a per-session Project group name for Settings > Keypad > Sessions >
  Group by > Project. It overrides `cwd` for the group and label; matching names on the same host group together,
  even across adapters. Different hosts stay separate, and named groups cannot collide with cwd-based groups.
  Display text is cleaned and trimmed, then must contain 1...128 Unicode scalars; blank or longer values get `422`.
  Omit it on a merge to keep the current value; send `null` to clear it and resume the existing `cwd` fallback
  (or No project when no absolute cwd is known). Built-in agents keep their existing cwd-based grouping.
  This is an additive v1-compatible field: `/a/v1` and discovery `api:1` are unchanged; older servers ignore it.
  For example, a remote job can send:

  ```json
  { "title": "Build customer report", "activity": "thinking", "host": "jobs-vm", "project": "Customer jobs" }
  ```
- `toolKind` (optional) picks the key's tool motion while `activity` is `tool`: `shell`, `edit`, `read`, `web`,
  `delegate`, `plan`, `integration` or `other`. An unknown value counts as none (a `warnings` entry, not an error);
  without it the key draws the `other` motion.
- What the key's text shows depends on `activity`. While `streaming`, the key types out `message.text`. While `tool`,
  it shows `toolLabel` (its first word as the tool name, the rest below it), not the message. `done` shows Done with
  the last line of `message.text`. `starting` shows the model; `thinking` and `compacting` show `statusDetail`;
  `waitingPermission` shows "Allow <tool>?" from `approval` (or from `statusDetail`); `waitingQuestion` shows
  Question; `error` shows `statusDetail`, split at its first `: `; `idle` and `offline` show the status only.
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
| `adapter_token_required`: an interactive adapter's route called without its `X-Orbtile-Adapter-Token` (§6) | 403 |
| `not_found`, `adapter_not_registered` | 404 |
| `bad_request` (wrong method) | 405 |
| `reserved_id`, `stale_request`, `registration_changed`, `invalid_cursor`, `events_not_supported`, `poll_pending` | 409 |
| `length_required` (chunked bodies are refused) | 411 |
| `payload_too_large` | 413 |
| `unsupported_media_type` | 415 |
| `invalid_field`, `limit_exceeded` | 422 |
| `rate_limited` (+ `Retry-After`) | 429 |
| `internal` | 500 |
| `api_disabled`, `poll_limit` (+ `Retry-After`) | 503 |
| `limit_exceeded` (+ `Retry-After`): more than 8 adapter connections, or the API queue is full | 503 |

Limits: body 64 KB; 8 external adapters live, 32 remembered; 32 sessions per adapter; 20 requests/s per adapter
(burst 60) and 100 requests/s for all adapters together; at most 8 adapter connections at a time. The API has its own
queue (at most 256 waiting requests), so a busy adapter cannot slow down Orbtile's own hooks.

Versions: the path carries the major version (`/a/v1`). v1 changes only by adding optional fields and new
`info.activities` values. `info.apiMinor` is 2 for v1.2; it was 1 for event capabilities and 0 before that. The server
ignores unknown request fields and names them in `warnings`. Clients must ignore unknown response fields. A breaking
change gets `/a/v2`, and v1 stays for at least two minor releases.

## 5. Privacy and security

- Orbtile keeps session fields in memory only. It never writes message text, titles or cwd to disk or to logs. Logs
  carry the adapter id, the session uid, the activity and the error code.
- The discovery file holds no session data.
- The token is a same-user capability. It stops other macOS users, sandboxed apps without file access and web pages.
  It does not stop a process that already runs as you (nothing can).

Owner tokens protect occupied interactive registrations, not persistent identity. A descriptor fingerprint matches
self-reported fields. After unregister, 60 seconds of silence, token regeneration or restart ends a lease, another
same-user process holding the discovery token can register the same id and matching descriptor, reusing remembered
display consent and keypad opt-in. Old events and requests do not survive. Legacy display-only registrations retain
the shared-Bearer model. This protocol does not isolate hostile processes already running as the same user.

## 6. Two-way events (v1.1, 0.2.1)

v1 adapters that omit capabilities keep their original display and URL/app opening behavior. Check `/info` for
`apiMinor >= 1` before using the extension. An interactive adapter registers capabilities explicitly:

```json
{"name":"MyAgent","palette":{"baseHue":150},"urlSchemes":["myagent"],
 "capabilities":["tap","approvals","questions"],"tapBehavior":"alsoOpen"}
```

The registration reply includes **`adapterToken` in the JSON body**. Keep it in memory, and send
`X-Orbtile-Adapter-Token: <adapterToken>` in addition to the discovery Bearer token on every route for this adapter,
including re-registration and unregister. `tapBehavior` requires the `tap` capability. An occupied interactive id cannot be replaced using the discovery token
alone, even when its owner temporarily removes every event capability. Authenticated re-registration keeps the
ownership token, sequence and used-id history. A changed registration clears pending requests and queued events,
cancels a held poll with `409 registration_changed`, and still requires the matching user Allow. Tokenless v1
registrations remain unchanged. Tokens, queued events and request history end on unregister, 60 s silence, token regeneration or host restart.
Never log either token, persist it in your adapter's session store, or put it in a URL or argv.

### Poll, apply, acknowledge

Open the event channel yourself; your adapter exposes no inbound listener. Send:

```json
{"after":0,"ack":0,"waitSeconds":2}
```

An example response:

```json
{"events":[{"sequence":1,"type":"tap","sessionId":"run-7","host":"local"},
 {"sequence":2,"type":"approval","sessionId":"run-7","host":"local",
  "requestId":"approval-91","decision":"deny","expiresAt":1790778000}],
 "lastSequence":2,"acknowledged":0}
```

Each registration has its own increasing sequence. Events belong only to the adapter that owns the session;
the session is identified by `(sessionId, host)` inside that adapter. Keep a local applied cursor over transport
reconnects. After applying or deliberately discarding these events, the next poll sends `after:2, ack:2`. Before
applying, reconnect with the old cursor to replay unacknowledged events. `ack <= after <= highest delivered`, and
acknowledgements never decrease. `lastSequence` is informational: **do not acknowledge it blindly**, since policy
changes can remove queued events and leave sequence gaps. New registrations reset the cursor to zero.

Only one outstanding poll is allowed per adapter, and at most **four** polls may wait globally. Further empty wait
polls get `503 poll_limit` with `Retry-After: 1`; retry with your unchanged applied cursor. This leaves four of the
eight adapter transport slots available for on-screen clears, session updates and heartbeats. `waitSeconds` is an integer 0...2; Orbtile waits asynchronously until an event
or timeout and closes the HTTP connection as usual. An admitted adapter response has a separate 3 s write deadline:
a peer that stops reading cannot retain its transport slot indefinitely. Reconnect with your cursor after network errors. An enqueued
host decision means queued, not executed by your adapter. Acknowledging an item alone never authorizes execution.

Before applying an approval/question, atomically re-check the pending request id, `(sessionId, host)`, event kind,
expiry (Unix epoch seconds) and your UI's current options. Deduplicate by sequence. Apply once in your authoritative
UI, then acknowledge; if the UI commit fails, leave the cursor unchanged. Your callback must have no side effects when it raises;
use an idempotent request ledger if your own UI cannot commit transactionally. An on-screen answer must retire its id
under the same lock so a delayed keypad event is discarded. Keep the own-UI dialog available after disconnect or
timeout: fail-open means return to that prompt, **never implicit Allow**. See `EventConsumer` in `ref.py` for the
serialized reference pattern. Do not execute commands provided by event text.

### Approval requests

Only adapters declaring `approvals` can send this session field:

```json
{"title":"Demo","activity":"waitingPermission",
 "approval":{"id":"approval-91","toolName":"Bash","commandSummary":"npm test"}}
```

Orbtile shows Deny/Allow with the built-in Allow-hold safety: a host correlation id, generation tokens, a commit
re-check, and current visibility, policy and freshness checks. **Keypad decisions are always on** (since 0.2.15):
once the user allows the adapter in Settings > Agents, the keypad can answer the approvals and questions it
declares. There is no separate Keypad switch. Disabling the adapter clears queued approval/question decisions. Unknown, replaced, consumed, expired or reused request ids cannot receive a decision.
Approvals expire 120 s after their first publication; toolName is 1...80 characters, commandSummary 1...512.

### Questions

Only adapters declaring `questions` can send:

```json
{"activity":"waitingQuestion","approval":null,
 "question":{"id":"question-92","questions":[{"text":"Which docs?","header":"Docs",
  "multiSelect":true,"options":[{"label":"README"},{"label":"Protocol","description":"API details"}]}]}}
```

The existing keypad question view and Send-hold checks produce:

```json
{"sequence":3,"type":"question","sessionId":"run-7","host":"local",
 "requestId":"question-92","picks":[[0,1]],"expiresAt":1790778480}
```

`picks` has one array of unique zero-based option indices per question; single-select accepts exactly one index,
multiSelect accepts one or more. Your on-screen dialog remains authoritative. The keypad's Other/open-screen action
returns to your UI, following your tap opening choice. Option labels must be unique within each question. Interactive tools such as AskUserQuestion use `question`,
not an approval request. Questions expire 600 s after first publication.

### Taps and bounds

With `tap`, a tap queues `{sequence,type:"tap",sessionId,host}` for its owner. `tapBehavior:"alsoOpen"` (default)
also opens the registered URL/app; `"eventOnly"` suppresses that opening. The user's tap preferences still apply:
mark-read keeps its existing behavior, and nothing sends nothing. Adapters without `tap` retain v1 opening.

Publish one request per session, at most 32 per adapter. Request ids are 1...128 ASCII identifier characters
(`[A-Za-z0-9._:-]`), fresh across sessions and request kinds for this registration, with at most 4096 distinct ids.
Identical pending updates can refresh liveness but never change contents or restart age. Clear `approval`/`question`
with `null` when your own UI answers. After a new registration, reconcile that UI first and issue a fresh id; never
blindly replay a request from the previous registration.

Question limits: 1...4 questions, 2...4 options, text 1024/header 12/label 120/description 512 characters. Existing
display control/bidi sanitization applies. The event queue holds at most 128 unacknowledged events per adapter;
when full, new events are refused, never evicted to admit a tap. Unacknowledged taps can fill the queue and
prevent a new approval or question decision from being queued. That failure leaves the pending request unconsumed;
the adapter's own dialog remains the fallback. Existing 64 KiB body, 20 requests/s burst 60 per
adapter, global 100 requests/s, 8 connections and 256 queued-request limits still apply. Use a persistent applied
cursor only within the live registration: Orbtile's event state is memory-only and disappears on host restart.

Dependency-free local SDK 0.1.0 sources are available in Orbtile: `@orbtile/adapter` (Node 20+) and
`orbtile-adapter` (Python 3.10+, sync and asyncio), with Windows adapter support using the loopback transport and
discovery rules above. Neither package is published to npm/PyPI yet; the SDK READMEs contain local installation and
publishing commands. The SDK sources and READMEs are not included in this kit. Pinning sessions to keys and signed
manifests remain deferred.

## 7. Built-in signals expressed in v1

The built-in adapters run inside the app, but each of their signals has a v1 call. This is the check that v1 can
express everything the keypad shows.

| Built-in signal | v1 call |
|---|---|
| Claude `SessionStart` / Codex first `session_meta` | `POST sessions/{id}` `{title, cwd, model, activity:"starting"}` |
| `UserPromptSubmit` / `task_started` | `{activity:"thinking", message:null}` |
| `PreToolUse` / `function_call` | `{activity:"tool", toolLabel}` |
| `MessageDisplay` / agent commentary | `{activity:"streaming", message:{id, text}}` |
| `PermissionRequest` | `{activity:"waitingPermission", statusDetail}`; v1.1 adds `approval` + decision event |
| AskUserQuestion / `request_user_input` | `{activity:"waitingQuestion"}`; v1.1 adds `question` + answer event |
| `PreCompact` / `PostCompact` | `compacting`, then the prior state |
| `Stop` / `task_complete` | `done` + `message` |
| `StopFailure` / `task_complete` with an error | `error` + `statusDetail` |
| `Interrupt` / `turn_aborted` | `{activity:"idle", statusDetail:"interrupted"}` |
| Subagent start / stop | `{subagentCount}` |
| `SessionEnd` (Claude) | `DELETE sessions/{id}` |
| Registry scan / `claude agents --json` / Codex state DB scan | `PUT sessions` (full replace), `liveText:false` for state-only rows |
| ZCode: task list (`tasks-index.sqlite`) + live turn state (`cli/db/db.sqlite`), both read-only | `PUT sessions` (full replace); automation runs `background:true`; a tap only activates the app (no `openURL`) |
| Remote hosts; deep links; pid → host app; background rows | `host:"<alias>"` (+ TTL → offline); `openURL`; `pid`; `background:true` |

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


To exercise all three v1.1 event kinds with no real agent or command execution, start the mock with the explicit
`--events` demo flag (it simulates user opt-in and a tap, Deny, then a question answer), then run the Python demo:

```sh
python3 examples/mock-orbtile.py /tmp/orbtile-events/adapter-api.json --events
# In another terminal:
ORBTILE_API_FILE=/tmp/orbtile-events/adapter-api.json python3 examples/ref.py --events
```

Without that flag the mock never simulates decisions. Against real Orbtile, run only the second command with its
normal discovery file and allow the adapter in Settings > Agents; `ref.py --events` waits for that Allow before
publishing. Keypad decisions need no other setting. Shell and Node examples stay unchanged v1 display-only clients,
which demonstrates compatibility.

Standalone reference tests over a local Unix socket:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Outside a sandbox, run Orbtile's `AdapterAPITests`, `ReferenceAdapterTests`, and event socket tests; also verify
long-poll wake/timeout/cancellation, TCP refusal and 8-connection saturation. Exercise `ref.py --events` against the
real Unix server, including reconnect-before-ack, own-UI answer races and host restart reconciliation.
