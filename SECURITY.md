# Security Policy

## Project status

Locestra is pre-1.0 alpha software for one trusted user on a local workstation. Only the latest commit on `main` is supported. The project is not currently designed to be exposed to a LAN, the public Internet, or untrusted users.

## Reporting a vulnerability

Please use GitHub private vulnerability reporting from the repository's **Security** tab. Do not open a public issue for an undisclosed vulnerability.

Include a concise description, affected component, reproduction steps, expected impact, and any safe remediation ideas. Do not include real credentials, private source code, personal data, production logs, or destructive proof-of-concept payloads.

## Sensitive data

Never commit or attach:

- `.env` files or API tokens;
- private keys, cookies, browser profiles, or credentials;
- model weights, databases, logs, runtime artifacts, or generated media;
- source code or documents that you are not authorized to disclose.

Cloud integrations are separate data boundaries. Enabling a connector or having valid credentials does not itself authorize uploading private data.
