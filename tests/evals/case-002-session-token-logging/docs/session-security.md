# Platform Security Spec — sessions and secrets (excerpt)

Normative for all services under `admin/` and `ingest/`. A
session-handling change is not mergeable until it passes S-1 and S-2.

## S-1 Secret handling in logs

Tokens, session tokens, bearer credentials, and `Authorization` header
values MUST NEVER be written to logs, metrics, traces, or error reports, at
any level including DEBUG. Log the opaque session ID instead; the session ID
is not a secret.

## S-2 Environment variable prefixes

- Server-side secrets MUST be read from environment variables carrying the
  `APP_` prefix only (e.g. `APP_SESSION_SECRET`).
- `VITE_`-prefixed variables are inlined into the browser bundle by the
  build pipeline and are FORBIDDEN as a source of server-side secrets.
  Reading any `VITE_*` variable from a server process is a violation
  regardless of the variable's contents.
