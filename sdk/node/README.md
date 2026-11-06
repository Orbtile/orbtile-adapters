# @orbtile/adapter

Send your own tool's status to Orbtile: a dependency-free client for Orbtile Adapter API v1.1.

Version 0.1.0. Node >=20, ESM, TypeScript declarations, no runtime dependencies. Implements Adapter API v1.1 using HTTP over the discovered Unix socket on macOS or loopback TCP address on Windows. Enable Orbtile's Adapter API and allow your adapter in Settings > Agents. Keypad decisions require a separate user opt-in.

## Quick start

Install with `npm install @orbtile/adapter`.

```js
import { AdapterClient } from '@orbtile/adapter';
const pad = new AdapterClient({
  adapterId: 'dev.example.agent',
  registration: { name: 'Tiny agent', palette: { baseHue: 150, attentionHue: 330 }, capabilities: ['tap'] },
});
await pad.connect();
await pad.upsertSession('run', { title: 'My task', activity: 'thinking', project: 'Customer jobs' });
for await (const event of pad.events()) {
  await pad.applyEvent(event, () => { console.log('Tapped', event.sessionId); });
}
```

`connect()` discovers `ORBTILE_API_FILE` (or the platform default), verifies HMAC proof before publishing data, registers, and starts a 10-second heartbeat. Pass `apiFile` for test servers. macOS keeps Standard then Enterprise fallback. Windows searches `%USERPROFILE%\.orbtile\adapter-api.json` (Store), `%LOCALAPPDATA%\Orbtile\Windows\adapter-api.json` (Standard MSI), then `%LOCALAPPDATA%\Orbtile Enterprise\Windows\adapter-api.json` (Enterprise MSI), choosing the first existing file and defaulting to Store. Heartbeat failures call `onLeaseError`; a lost pending prompt also sets persistent `reconciliationRequired` (clear with `reconcile()` only after restoring your UI). `leaseGeneration` increments when a lease ends. The SDK never logs. `close()` stops renewal; `unregister()` also ends sessions and the registration. Keep the process alive while using an idle client.

`register`, `heartbeat`, `upsertSession`, `removeSession(id, host)`, and `replaceSessions` mirror the documented endpoints. Updates keep a full in-memory snapshot; include `host` on every remote update. Batches are display-only: publish interactive requests through `requestApproval` and `askQuestion`, which also set the required waiting activity and clear the other request kind. Body size is checked in UTF-8 before transport; fields are validated by Orbtile, with no silent truncation. Server failures expose `AdapterError.code/status/field/retryAfter`; payload and transport errors have dedicated classes. Error messages do not include remote message text or either token.

`project?: string | null` (Orbtile 0.2.17+) sets the per-session Project group and label within its host. Orbtile cleans it and trims surrounding whitespace; the result must be 1...128 characters, otherwise the update fails with HTTP 422 `invalid_field`. Omission keeps it on merges; `project: null` clears it and restores cwd grouping. This optional field is v1-compatible; older servers ignore it.

## Events and authoritative answers

`pollEvents(0..2)` resumes from the applied cursor. `events({signal})` yields events and retries transient connection, rate-limit and poll-limit failures. No event is automatically acknowledged on delivery. `ack(sequence)` is an explicit deliberate-discard/commit operation; it accepts only a delivered sequence and never uses `lastSequence`. Consume sequentially in increasing sequence order; acknowledging a later sequence deliberately discards all earlier events. Cursor and request state live only for the current registration.

Prefer `applyEvent(event, commit)`: it serializes with `answerInUI`, checks request id, session/host, kind, both expiry times, approval decision and current question options, commits once, then acknowledges. Stale events are deliberately discarded. Your callback must atomically commit the answer in your authoritative UI, have no side effects on failure (or use an idempotent ledger), and must not call other serialized client methods. It receives a cloned event and current request. It must never execute commands from event text. `answerInUI(id, host, commit, {requestId})` checks the expected dialog id and retires an on-screen answer under the same lock and clears the published request. An expired/disconnected keypad prompt leaves your own UI available; it never implicitly allows anything. Allow-hold is enforced by Orbtile's keypad, not bypassed by this SDK.

A `401` or `adapter_not_registered` causes re-discovery, proof verification, registration and display snapshot restoration. Pending requests, used ids and cursors are cleared; an operation with pending requests (or an event poll) raises `RegistrationLostError`. Reconcile your UI and publish fresh ids before continuing. The client deliberately does not replay approval/question ids from a lost lease. An error callback during lease renewal is the signal to reconcile idle prompts too. Re-registration changes require Orbtile's new Allow; do not change descriptors with active requests.

## Example and tests

`node examples/tiny-agent.js` streams a toy session, asks a question and waits for keypad or terminal input. Set `ORBTILE_API_FILE` to an isolated mock discovery file to test without the app. See [`examples/mock-orbtile.py`](https://github.com/Orbtile/orbtile-adapters/blob/main/examples/mock-orbtile.py) `--events` for a demo server. Run `npm test`; tests create private temporary fake servers over the platform transport. No real app or real discovery file is needed.

MIT license. Protocol source: [Adapter Protocol](https://github.com/Orbtile/orbtile-adapters/blob/main/protocol/README.md).
