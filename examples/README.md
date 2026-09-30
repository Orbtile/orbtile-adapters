# Reference adapters

These examples implement the [Orbtile Adapter Protocol v1](../protocol/README.md). The API is off by default; enable it in Orbtile's Settings › Agents › Adapter API and allow each adapter.

| File | Requirements | Behaviour |
|---|---|---|
| `ref-shell.sh` | macOS, bash, curl, plutil, iconv | Runs a command as a `tool` session, sends heartbeats, reports `done` or `error`, and preserves its exit code |
| `ref.py` | Python 3.8+ standard library | Verifies the server proof, registers, sends a demo through thinking, tool, streaming and done, then deletes it |
| `ref.mjs` | Node 18+, no packages | Verifies the proof and sends a complete job list every five seconds |
| `mock-orbtile.py` | Python 3.8+ standard library | Serves local fake endpoints, writes discovery and prints requests |

Follow the [root quick start](../README.md) to run against Orbtile or the mock. Each client reads `ORBTILE_API_FILE`, falling back to the standard Orbtile discovery location. The shell wrapper runs its command even when the API is unavailable. Python and Node require discovery before starting.

The shell wrapper's heartbeat interval defaults to 10 seconds; `ORBTILE_HEARTBEAT_SECONDS` overrides it. All three clients can rediscover and register again after authentication or registration loss. Python keeps merged session state; Node resends the complete batch. Python and Node wait on a 429 response, but return it to the caller rather than automatically retrying it.

See the [palette guide](../protocol/PALETTE.md) for the examples' working and attention colours.

The mock requires a discovery location as its only argument and never writes to Orbtile's normal discovery location automatically. It checks authentication, basic registration and activity fields, registered URL schemes and refused links. It always allows adapters and omits TTL, silence expiry, rate limits and automatic registration invalidation on token changes. Stop it with Ctrl-C; it removes its generated discovery file and socket. A deep discovery path causes the socket to use a short private temporary directory; always read the socket path from discovery.

The [standalone suite](../tests/test_adapters.py) exercises the clients through the mock handler on a local Unix socket. It injects token rotation and lost registrations to test recovery. Orbtile application's Swift tests depend on application modules and are not included in this standalone kit.
