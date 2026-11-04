# Orbtile adapter examples

Reference adapters for the [Orbtile Adapter Protocol v1.2](../protocol/README.md).
The API is off by default: the user turns on the "Adapter API" switch in Orbtile › Settings › Agents › Adapter API and
then allows each new adapter. To develop without Orbtile, use `mock-orbtile.py`. All of the examples talk HTTP over the
Unix socket named in the discovery file.

This kit's standalone suite runs the three reference adapters (`ref-shell.sh`, `ref.py`, `ref.mjs`) through the mock
handler over a local Unix socket: the happy path, the whole Python demo, a failing command, awkward titles and paths,
no discovery file, a regenerated token (401, then 404 `adapter_not_registered`: a new token ends every registration)
and a lost registration. It also runs the complete two-way event demo and checks v1.2 registration fields.

Staying registered: an adapter that has been silent for more than 15 s shows its sessions `offline`, and after 60 s its
registration and sessions are gone (the next call gets 404 and it must register again). The `ref.mjs` poller
sends the full list every 5 s and the `ref.py` demo never pauses longer than 10 s; a long-running adapter of your own
should send a `POST /adapters/<id>/heartbeat` every 10 s. `ref-shell.sh` does that: while its command runs, a
background loop sends a heartbeat every 10 s (`ORBTILE_HEARTBEAT_SECONDS` changes it) and, on 401 or 404, reads the
discovery file again, registers again and resends the running session. The loop ends with the command (or when the
script is killed) and never writes to the command's output.

| File | Needs | What it shows |
|---|---|---|
| `ref-shell.sh` | bash, curl, plutil, iconv | `ref-shell.sh make test`: the command shows as `tool`, then `done` or `error` |
| `ref.py` | Python 3.8+ stdlib | server proof, register, updates and recovery; `--events` adds tap, approval/question validation, dedup and ack/replay |
| `ref.mjs` | Node 18+ | a poller: full session list every 5 s |
| `mock-orbtile.py` | Python 3.8+ stdlib | a stand-in server; `--events` explicitly simulates user opt-in, tap, Deny and question answer |

Try them without Orbtile from this repository's root:

```sh
python3 examples/mock-orbtile.py /tmp/orbtile-mock/adapter-api.json &
export ORBTILE_API_FILE=/tmp/orbtile-mock/adapter-api.json
bash examples/ref-shell.sh sleep 2
python3 examples/ref.py
node examples/ref.mjs          # Ctrl-C to stop
kill %1                        # stop the mock; it removes its socket and discovery file
```

The mock prints one line per request. It checks the token and the basic field rules, including the `urlSchemes` rules
at registration (at most 4, lowercase, unique, never `file`, `javascript`, `data`, `orbtile` or `x-apple.systempreferences`)
and the `openURL` rule (its scheme must be one of the registered `urlSchemes`, else `422` on the field). But the
[JSON Schema](../protocol/adapter-protocol-v1.schema.json) and Orbtile itself are the authority. The mock does
not ask for an Allow (`enabled` is always `true`), keep time (no TTL, no 15 s offline, no 60 s reap), limit rates or end
registrations when a token changes.

When the discovery file sits in a deep directory (a Unix socket path holds at most 104 bytes), the mock binds its socket
in a short private folder under `TMPDIR` (or `/tmp` when unset) and the discovery file names it, so read `socketPath` from the file. Both files
are removed when the mock ends (SIGTERM or Ctrl-C).

Palettes: the shell example uses green (150°) and Node magenta (320°) generated palettes. Python uses a cyan base hue
(200°), four explicit state colours and the Terminal app icon as a v1.2 example. Shell and Node intentionally omit
`icon` and `palette.states`, demonstrating that v1.0/v1.1 registrations remain valid. The mock advertises `apiMinor: 2`
and checks icon and state fields as well as the existing registration rules. See the
[palette guide](../protocol/PALETTE.md) for the working, attention and state-colour rules.

For the 0.2.1 event demo, add `--events` to both the mock and `ref.py`. Against real Orbtile, the Python client waits
for the user's Allow before publishing. The mock uses Deny and executes no command. Keep your own dialog authoritative:
validate the current request and expiry atomically before applying an event, then acknowledge it. Restart or a new
registration requires reconciliation and fresh pending ids. Owner tokens protect an occupied live registration, not
persistent identity.

Run the standalone suite from this repository's root:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

It checks tap, Deny, multiple-choice answer and the final acknowledgement over a real Unix socket, as well as v1.2
schema fields and registration validation. It never starts Orbtile or reads its normal discovery file.

## v1.2 icon and state colours

A four-colour registration can also include an icon:

```json
{
  "name": "Example Agent",
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
  "icon": { "appBundleId": "com.example.agent" }
}
```

`icon` may contain `png`, `appBundleId` or both; `null` or omission clears the custom icon. PNG is standard RFC 4648
base64 without whitespace, at most 43,696 characters and 32,768 decoded bytes, with a valid PNG header/IHDR and square
dimensions from 16 through 256 px. Orbtile re-encodes it as 28×28 and 56×56 PNGs of at most 16,384 bytes each. A macOS
bundle identifier is 3...128 bytes matching `^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$`. An installed macOS app icon takes
precedence, then PNG, then the glyph. Windows uses PNG and ignores the bundle identifier after validating its syntax.
Malformed fields get `422 invalid_field`; oversized PNG data gets `422 limit_exceeded`.

The ten editable state keys are `idle`, `starting`, `thinking`, `tool`, `streaming`, `done`, `compacting`, `offline`,
`waitingPermission` and `waitingQuestion`. Values match `^#[0-9A-Fa-f]{6}$`; `error`, `empty`, unknown states and an
empty map are rejected. Omitted `tool` and `streaming` inherit `thinking`; omitted `waitingQuestion` inherits
`waitingPermission`. Other omitted states use the normal generated base or attention palette. The shared alarm-colour
setting ignores both waiting overrides, and a user hue in Settings replaces all adapter state colours. `error`, `empty`
and surface neutrals never change.

The ramps Orbtile 0.3.4 generates for this example (`baseHue: 270`) are below, in
`deep, base, hi, hot, heart` order:

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

Registration warnings use these exact strings:

```text
icon.appBundleId '{bundleId}' is not installed on this Mac; using the fallback icon
icon.appBundleId is ignored on Windows; send icon.png to show an icon on every platform
state '{state}' hue {hue} is within 15° of the error hue (30.9°), so it can be hard to tell apart from Error
```

They are informational, not registration failures. For the first, the fallback is PNG when supplied and the glyph
otherwise. See the [protocol reference](../protocol/README.md#state-colours-v12) for the Hex-Anchored ramp and lightness/chroma rules.

## Empty-key URL

The empty-key URL is configured by the user in Settings › Keypad › Keys. **It is a host-side user setting, not an
adapter field.** Keypad, popover and phone taps on an empty session key make the host open that URL on the host
computer; there is no adapter or PhonePad wire change.

Blank disables the action. The trimmed URL can be at most 512 UTF-8 bytes, must be absolute, and cannot contain
whitespace, controls or userinfo. `https` requires a host; `http` works only for loopback hosts. Custom app schemes are
allowed except the dangerous schemes listed in [the protocol reference](../protocol/README.md#empty-key-url-host-setting-not-an-adapter-field),
including `file`, `javascript`, `data`, `smb` and Orbtile/system settings schemes. Orbtile validates on save and again
on tap. Only empty slots 0...7 qualify, with demo, lock-screen, non-empty-view and long-hold guards plus a 250 ms
cooldown. The URL never syncs or imports with settings.
