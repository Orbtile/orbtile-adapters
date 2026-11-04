# Changelog

## v1.2 — 2026-11-03

- Updated the protocol specification and JSON Schema to v1.2 (`apiMinor: 2`), keeping `/a/v1`, discovery `api:1` and unchanged v1.0/v1.1 registrations compatible.
- Added optional adapter `icon` and `palette.states` registrations, with PNG and macOS app-bundle icons, ten editable state colours, inheritance rules and Hex-Anchored OKLCH ramps.
- Documented loopback TCP on Windows, Microsoft Store and Standard/Enterprise MSI discovery, the macOS Orbtile Enterprise discovery path, and Orbtile's local Node 20+ and Python 3.10+ SDK 0.1.0 sources with Windows adapter support.
- Documented per-session `project` grouping and the key text shown for every activity.
- Added the v1.1 tap, approval and question event channel to the public kit. The Python `--events` demo now waits for the user's Allow before publishing and reconciles pending requests after registration loss.
- Updated all reference clients and the mock server, including v1.2 validation, and expanded the standalone standard-library test suite.
