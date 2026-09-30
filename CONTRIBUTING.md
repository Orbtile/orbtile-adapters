# Contributing

Small, focused pull requests are welcome. Describe the problem and the resulting behaviour, keep examples dependency-free, and follow the surrounding code style.

Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v` on macOS with Python 3.8+, Node 18+, bash, curl, plutil and iconv before submitting. Keep v1 changes additive, update the specification and schema together, and include a relevant fake-server test for behavioural changes.

Never include discovery files, credentials, session data or private paths. Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
