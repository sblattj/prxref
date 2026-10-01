# EDGE-205: Return 404 for missing static assets at the edge

**Type:** Task  **Priority:** P2  **Epic:** Edge hardening

## Summary

Stop the SPA fallback from serving index.html for requests that are really
missing static files, so a broken asset URL fails loudly.

## Description

Today every unmatched path falls through to the SPA shell, which hides
missing asset files behind a 200 response. Add an nginx rule that answers
404 for asset-looking paths (anything whose last segment has a file
extension). Routing and hardening rules are owned by the specs; follow them.

## Acceptance Criteria

- [ ] Missing static assets such as `/static/app.js` return 404
- [ ] SPA routes still serve the application shell
- [ ] Conformance per docs/routes.md and docs/nginx-hardening.md (excerpts, normative)
