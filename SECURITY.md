# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.x.x   | ✓ Active  |

## Reporting a Vulnerability

**Please do NOT open a public GitHub issue for security vulnerabilities.**

Report privately via GitHub Security Advisories:
<https://github.com/angelnicolasc/forge/security/advisories/new>

Or email the maintainer: **angelnicolascorzo@gmail.com** (PGP key available on
request).

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (optional)

We will respond within 48 hours and aim to patch critical vulnerabilities within 7 days.

## Security Design

- All LLM calls are sandboxed — agents cannot execute arbitrary code without explicit tool grants
- Memory entries are tenant-isolated — no cross-tenant data leakage
- Evolution mutations require human approval in `mode=suggest` (default)
- Audit logs are append-only and tamper-evident
- No credentials are stored in the evolution journal or memory
