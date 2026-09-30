# Orbtile adapters

Orbtile is a status pad for AI coding agents, showing their sessions on an LCD keypad and in the Mac menu bar. It displays activity, live text and requests for attention, with all adapter communication staying on your Mac.

An adapter is a separate local process that connects an agent or tool to Orbtile by sending small JSON messages over HTTP/1.1 through a Unix-domain socket. You register your adapter, report sessions and send heartbeats; no code is loaded into Orbtile.

See [the public adapter page](https://orbtile.com/adapters.html).

## Quick start

Requires Orbtile 0.2 with Adapter API enabled, or the included mock server. Python needs Python 3.8+; Node needs Node 18+; the shell wrapper needs macOS, bash, curl, plutil and iconv.

1. In Orbtile, enable **Settings › Agents › Adapter API**.
2. Find `~/Library/Application Support/Orbtile/adapter-api.json`. Its `socketPath`, `basePath` and `token` locate and authenticate the API. `ORBTILE_API_FILE` overrides the discovery location. A missing file means the API is off or Orbtile is not running.
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

The mock always allows adapters and validates basic fields. It does not enforce TTL, silence expiry, rates or token-change registration invalidation. It is a development aid, not a substitute for testing with Orbtile.

## Contents and tests

- [Protocol specification](protocol/README.md): transport, discovery, authentication, endpoints, errors, limits and lifecycle.
- [JSON schema](protocol/adapter-protocol-v1.schema.json): request and response definitions in `$defs`; choose the definition for the endpoint you validate.
- [Palette guide](protocol/PALETTE.md): choosing working and attention colours.
- [Examples](examples/README.md): shell, Python, Node and the mock server.
- [Standalone tests](tests/test_adapters.py): reference clients against a local fake server, with no application dependencies.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Run the suite on macOS with all three runtimes installed. Missing prerequisites fail the suite. Tests generate temporary credentials at runtime and remove their temporary files; they never use your normal discovery file. On Unix systems without macOS's plutil, Python and Node examples still work, but the shell example requires macOS.

Third-party adapters in Orbtile 0.2 cannot receive keypad answers, approval decisions or tap events. Taps can open a session through its registered URL scheme or application.

## License

MIT; see [LICENSE](LICENSE). Only Orbtile, made in Europe.
