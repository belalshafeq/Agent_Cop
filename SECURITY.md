# Security Policy

## Reporting a Vulnerability
If you discover a security vulnerability in AgentTrafficCop, please do not open a public issue. Instead, email me directly at belalshafeq@gmail.com. I will respond within 24 hours.

## Security Posture
- **API Keys:** Raw keys are never stored. We only store SHA-256 hashes.
- **Passwords:** Hashed using Bcrypt with a strict 72-byte limit.
- **Webhooks:** Billing webhooks are verified using HMAC signatures.
- **Rate Limiting:** Protected against memory exhaustion via an LRU-capped sliding window.
- **CORS:** Strictly configured to only allow trusted origins in production.