---
name: security-review
description: Security checklist for code review and the mandatory security pass. Use when reviewing any change, and ALWAYS when a change touches auth, secrets, PII, user input, or external I/O (the hard trigger). Primary users are reviewer and devops; any agent may read it before handoff.
---

# Security review

## Baseline — every review
- Input validation at every trust boundary (type, length, range, encoding). Reject invalid input; don't sanitize-and-hope.
- AuthN vs AuthZ: every endpoint/handler checks *authorization*, not just authentication. Object-level checks — no IDOR.
- Secrets: none in code, config, logs, or tests. Grep the diff for keys/tokens. Env vars or a secret store only.
- Injection: parameterized SQL, no shell string interpolation, no eval, no unsafe deserialization (pickle, yaml.load).
- Data handling: PII minimized and never logged; TLS in transit; error messages don't leak internals or stack traces.

## Hard trigger — mandatory deep pass before `done`
If the change touches **auth / secrets / PII / user input / external I/O**, additionally:
- Session/token lifecycle: expiry compared correctly (off-by-one on `<` vs `<=`), revocation, rotation.
- SSRF: any outbound fetch of a user-supplied URL goes through a host allowlist.
- Path traversal: any file path derived from input is normalized and jailed.
- Dependency risk: new dependencies pinned; run the ecosystem audit (`pip audit` / `npm audit`).
- Write the security verdict explicitly in your log — a silent pass doesn't count.

## Escalate
Crypto design, multi-tenant isolation, or compliance scope → architect spins a security specialist (team formation). Don't improvise.
