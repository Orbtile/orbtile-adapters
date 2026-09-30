# Security

Report security vulnerabilities privately to **hello@orbtile.com**. Do not open a public issue containing exploit details, credentials or private session data.

Include the affected adapter or protocol version, expected and observed behaviour, reproduction steps and potential impact. Use synthetic data and remove tokens and personal information from attachments.

Keep the discovery file and its directory private. Read the token at runtime, send it only in the Authorization header, and never log it. A discovery token is a same-user capability: it cannot protect against another process already running as your user.

The mock server is for local development and does not implement all of Orbtile's validation, lifecycle or rate limits. Verify security-sensitive behaviour with Orbtile.
