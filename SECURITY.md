# Security

FaceMatch stores face embeddings, which are biometric data. Please report vulnerabilities privately through
GitHub's **Security → Report a vulnerability** form rather than a public issue.

## What the app does to protect your data
- Passwords are hashed with scrypt; sessions are random tokens stored only as SHA-256 hashes, in an `HttpOnly`, `SameSite=Lax` cookie (`Secure` over HTTPS).
- Sign-in is throttled per IP and per username, with exponential lockout.
- State-changing requests must come from the site's own origin (CSRF check); a strict Content-Security-Policy forbids inline scripts.
- Every photo, thumbnail, search result and person is checked against the signed-in user's folder permissions on the server.
- Share links are unguessable, expire, can be revoked, and expose only the photos chosen when they were created.
- First-run setup from another computer needs a one-time code printed on the server console.
- An audit log records sign-ins, exports, shares and admin changes.

## What it does not do
- It does not provide HTTPS itself. If you expose it beyond your own computer, put it behind a reverse proxy
  (see `Caddyfile.example`) and set `FACEMATCH_TRUST_PROXY=1`.
- The database and thumbnails are not encrypted at rest; use full-disk encryption.
