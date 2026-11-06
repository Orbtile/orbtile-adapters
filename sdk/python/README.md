# orbtile-adapter

Send your own tool's status to Orbtile: a dependency-free client for Orbtile Adapter API v1.2.

Version 0.2.0 · Python 3.10+ · stdlib runtime only · MIT. Install with `python3 -m pip install orbtile-adapter`.

`icon` and `palette.states` are optional Adapter API v1.2 registration fields, passed through as given;
`null` clears a custom icon or the state colours. Older Orbtile hosts ignore them with warnings, so check
`info.apiMinor >= 2` (`info['apiMinor'] >= 2` in Python) before relying on them. Changing either asks the
user to Allow again. See the [protocol rules](https://github.com/Orbtile/orbtile-adapters/blob/main/protocol/README.md).

## Ten-line quick start

```python
from orbtile_adapter import Client, SessionUpdate
registration = {"name": "Tiny", "palette": {"baseHue": 150, "attentionHue": 330},
                "capabilities": ["tap", "approvals", "questions"]}
with Client("dev.example.tiny", registration) as pad:
    update: SessionUpdate = {"title": "Tiny agent", "activity": "thinking", "project": "Customer jobs"}
    pad.upsert_session("run", **update)
    pad.upsert_session("run", activity="streaming", message={"id": "m1", "text": "Hello"})
    for event in pad.events():
        pad.apply(event, lambda answer: print(answer["type"]))
        if event["type"] == "tap":
            break
```

Discovery uses `ORBTILE_API_FILE`, or Orbtile's platform discovery path. Pass `discovery_file=` for a fake server. macOS keeps Standard then Enterprise fallback. Windows searches `%USERPROFILE%\.orbtile\adapter-api.json` (Store), `%LOCALAPPDATA%\Orbtile\Windows\adapter-api.json` (Standard MSI), then `%LOCALAPPDATA%\Orbtile Enterprise\Windows\adapter-api.json` (Enterprise MSI), choosing the first existing file and defaulting to Store. HTTP/1.1 uses the discovered Unix socket on macOS or loopback TCP address on Windows. Each connection verifies the server's HMAC proof before registration, every call sends the discovery Bearer token, and interactive routes also send the ownership token. Tokens stay in memory and are never logged.

Allow the adapter in Orbtile Settings > Agents; once allowed, it can receive approval/question decisions from the keypad. `connect()` returns the registration reply, including `enabled` and `reason`; successful session replies with `visible:false` mean user consent is pending or denied.

`SessionUpdate` and `SessionBatchItem` provide typed dictionaries, including the optional `project: str | None` field (Orbtile 0.2.17+). It sets the per-session Project group and label within its host. Orbtile cleans it and trims surrounding whitespace; the result must be 1...128 characters, otherwise the update fails with HTTP 422 `invalid_field`. Omission keeps it on merges; `project=None` clears it and restores cwd grouping. This optional field is v1-compatible; older servers ignore it.

## API

`Client(adapter_id, registration, *, discovery_file=None, heartbeat_interval=5, timeout=5)` connects through `connect()` or a context manager. `AsyncClient` has the same awaited methods and an async context manager. The asyncio facade uses stdlib worker threads; no socket operation blocks the event loop.

- `verify()`, `register()`, `heartbeat()`, `close()`; `info`, `capabilities`, `registration_reply`, `heartbeat_error` expose state. A daemon renews leases every five seconds, including while polling.
- `upsert_session(id, **fields)`, `remove_session(id, host="local")`, `replace_sessions([{ "id": ..., "title": ..., "activity": ... }])`. Session identity includes host; send the remote host with every update. Replacement is display-only: publish requests with the dedicated helpers.
- `request_approval(id, request, **fields)` and `ask_question(id, request, **fields)` set the proper activity and clear the other kind. Requests are the documented JSON objects, including fresh `id`s. Helpers do not invent an inbound decision endpoint.
- `poll_events(wait_seconds=2)` returns the protocol reply. `events()` is a sync/async iterator that retries network failures and overload responses with an unchanged applied cursor. Only one poll is allowed per client. `cursor` is the local applied cursor; **`lastSequence` is never automatically acknowledged**.
- `apply(event, commit)` validates request kind/id, session/host, local and server expiry, decision and current question options under the same lock as `answer_locally(id, commit, host="local", request_id=...)`. A duplicate/invalid event returns `False` and is discarded. An own-UI callback is ignored if the request was already consumed or its supplied `request_id` was replaced. A successful callback retires the prompt and advances the cursor; a callback that raises leaves it pending and unacknowledged. `acknowledge(event)` explicitly discards a delivered event without executing it.

Callbacks are **synchronous atomic own-UI transactions**, including for `AsyncClient` (where they run in a worker thread). They must have no side effects if they raise, or use an idempotent own-UI ledger. Check your current UI choices inside the transaction. `answer_locally` commits and retires the prompt before sending its null clear; a transport failure cannot revive that prompt. Do not run commands from event text. After `apply` succeeds, clear `approval`/`question` with a null update to retire the host UI too.

Orbtile enforces Allow-hold and Send-hold on the pad; the SDK accepts only documented final decision events after checking a live local prompt. A disconnect, timeout, denial, missing opt-in or invalid event never means Allow. Keep your own dialog available as the fallback.

On 401 the client reloads discovery once and verifies the listener again. On a lost registration it registers again, restores cached **display metadata**, resets the event cursor, and drops every old interactive request. Interactive callers receive `LeaseLostError`; `reconciliation_required` remains true even if background heartbeats subsequently succeed. Reconcile your own UI, call `reconcile()` to clear this notification, and publish new request ids. `lease_generation` changes at a lease reset. A cursor is in-memory and valid only within that live registration, never across app restarts. Transport reconnects within the registration preserve it.

Errors inherit `AdapterError`: `APIError` exposes `status`, `code`, `field`, `retry_after`; `DiscoveryError`, `ProofError`, `PayloadTooLargeError`, `LeaseLostError`, `TransportError` cover local failures. Error text never includes remote error messages or tokens. Bodies exceeding 64 KiB after UTF-8 encoding fail before connection. HTTP errors from direct methods propagate; iterators alone retry transport/overload failures.

## Tiny agent and tests

The example streams a session and publishes a question, retaining a terminal fallback when no pad answer arrives. No command execution or automatic approval occurs.

From a checkout of github.com/Orbtile/orbtile-adapters:

```sh
python3 -m pip install orbtile-adapter
python3 examples/mock-orbtile.py /tmp/orbtile-sdk-python/api.json --events
# Separate terminal:
ORBTILE_API_FILE=/tmp/orbtile-sdk-python/api.json python3 sdk/python/examples/tiny_agent.py
PYTHONPATH=sdk/python/src python3 -m unittest discover -s sdk/python/tests -v
```

Tests use temporary Unix sockets on macOS or loopback TCP on Windows and in-process fake servers; they do not access the real app, listener or discovery directory.
