# Orbtile adapters

Orbtile is a status pad for AI coding agents, showing their sessions on an LCD keypad and in the Mac menu bar. It displays activity, live text and requests for attention, with all adapter communication staying on the local computer.

An adapter is a separate local process that connects an agent or tool to Orbtile by sending small JSON messages over HTTP/1.1 through a Unix-domain socket on macOS or loopback TCP on Windows. You register your adapter, report sessions and send heartbeats; no code is loaded into Orbtile.

## Compatibility

- Base v1 display adapters require Orbtile 0.2 with Adapter API enabled, or the included mock server.
- Protocol v1.2 advertises `apiMinor: 2`. Use Orbtile 0.3.4 or later for the documented `icon` and `palette.states` behaviour. Older hosts accept these registrations, ignore the v1.2 fields with warnings, and use the glyph and `baseHue` palette; unchanged v1.0/v1.1 registrations remain valid.
- Two-way events are v1.1 and require Orbtile 0.2.1 or later. Keypad decisions are always on since Orbtile 0.2.15 once the user allows the adapter. The optional per-session `project` field requires Orbtile 0.2.17 or later.
- Check `GET /info` for `apiMinor >= 1` before using events and `apiMinor >= 2` before relying on v1.2 fields.
- The reference clients need Python 3.8+ or Node 18+; the shell wrapper needs macOS, bash, curl, plutil and iconv. The bundled examples use Unix sockets. Windows loopback TCP, Store/MSI discovery and local SDK support are covered in the [protocol specification](protocol/README.md).

## Quick start

1. In Orbtile, enable **Settings › Agents › Adapter API**.
2. On macOS, find `~/Library/Application Support/Orbtile/adapter-api.json`, or `~/Library/Application Support/Orbtile Enterprise/adapter-api.json` when Standard is missing. On Windows, discovery uses Store, Standard MSI, then Enterprise MSI paths as documented in the specification. The file's `socketPath`, `basePath` and `token` locate and authenticate the API; Windows discovery also supplies `url` and `port`. `ORBTILE_API_FILE` overrides the discovery location. A missing file means the API is off or Orbtile is not running.
3. Authenticate every request with `Authorization: Bearer <token from discovery>`, `Host: localhost` and `Content-Type: application/json`. Read the token at runtime; keep it out of source files, URLs, logs and command arguments. The Python client below reads it and verifies the server's HMAC proof.
4. Register with `PUT /a/v1/adapters/<your.reverse-dns.id>` and allow the adapter in Orbtile. New adapters stay hidden until allowed.
5. Send complete initial sessions with `POST /a/v1/adapters/<id>/sessions/<sessionId>`, then merge updates. Send `POST /a/v1/adapters/<id>/heartbeat` at least every 10 seconds while sessions are live.

From this repository's root, try the Python client:

```sh
python3 - <<'PYTHON'
import sys
sys.path.insert(0, "examples")
import ref
client = ref.Orbtile()
client.verify()
client.register()
print(client.session("hello", title="My first session", activity="thinking")[0])
print(client.session("hello", activity="done", message={"id": "result", "text": "Hello from my adapter!"})[0])
PYTHON
```

The reference clients use demonstration ids under `dev.example`. Replace the id and registration with a reverse-DNS id you own when building your adapter. After a token change or relaunch, rediscover, register again and resend each complete session. Python and Node wait for `Retry-After` on 429; the caller or next polling tick must retry the request.

To try the full demo without Orbtile, use two terminals from this repository's root:

```sh
# Terminal 1: choose an unused discovery location for the mock.
python3 examples/mock-orbtile.py .local-run/adapter-api.json
```

```sh
# Terminal 2
export ORBTILE_API_FILE="$PWD/.local-run/adapter-api.json"
bash examples/ref-shell.sh sleep 2
python3 examples/ref.py
node examples/ref.mjs
# Stop Node and the mock with Ctrl-C in their respective terminals.
```

For the two-way event demo, add `--events` to the mock and Python commands. The mock explicitly simulates user opt-in, a tap, Deny and a question answer. Against real Orbtile, run only `python3 examples/ref.py --events`; it waits for the user's Allow before publishing anything and never treats a timeout as implicit Allow.

The mock always allows adapters and validates basic fields. It does not enforce TTL, silence expiry, rates or token-change registration invalidation. It is a development aid, not a substitute for testing with Orbtile.

## Changes since v1.0

- v1.1 adds tap, approval and question events with owner tokens, validated event application and acknowledgement/replay. `ref.py --events` waits for the user's Allow.
- v1.2 adds optional adapter `icon` and `palette.states` fields, including PNG or macOS app icons and Hex-Anchored OKLCH state-colour ramps.
- Transport and discovery now cover loopback TCP on Windows, Microsoft Store and MSI discovery paths, and the macOS Orbtile Enterprise discovery file.
- Orbtile's dependency-free local SDK 0.1.0 sources support Node 20+ and Python 3.10+ (sync and asyncio), including Windows adapter support. They are not included in this kit.
- Optional per-session `project` grouping and key text for every activity are documented.

## Contents and tests

- [Protocol specification](protocol/README.md): transport, discovery, authentication, endpoints, events, errors, limits and lifecycle.
- [JSON schema](protocol/adapter-protocol-v1.schema.json): request and response definitions in `$defs`; choose the definition for the endpoint you validate.
- [Palette guide](protocol/PALETTE.md): choosing working, attention and per-state colours.
- [Examples](examples/README.md): shell, Python, Node and the mock server.
- [Standalone tests](tests/test_adapters.py): reference clients against a local fake server, with no application dependencies.
- [Changelog](CHANGELOG.md): protocol and kit changes.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Run the suite on macOS with all three runtimes installed. Missing prerequisites fail the suite. Tests generate temporary credentials at runtime and remove their temporary files; they never use your normal discovery file. On Unix systems without macOS's plutil, Python and Node examples still work, but the shell example requires macOS.

## License

MIT; see [LICENSE](LICENSE). Only Orbtile, made in Europe.
