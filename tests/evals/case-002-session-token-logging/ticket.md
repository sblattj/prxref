# AUTH-991: Persist admin console sessions across restarts

**Type:** Story  **Priority:** P0  **Epic:** Admin console GA

## Summary

Move admin session state out of the in-process dict into a durable store so
deployments no longer log every user out.

## Description

Support tickets spike after every deploy: the admin console keeps sessions in
a process-local dict, so any restart invalidates them. Introduce a durable
session store and wire it into the app. Add an issuance log line so support
can correlate a session with a user when triaging incidents.

Non-goals: SSO, MFA changes.

## Acceptance Criteria

- [ ] Sessions survive an app restart
- [ ] Session issuance is logged for support correlation
- [ ] Secrets are wired from the environment
- [ ] Passes the platform security checklist (docs/session-security.md)
