# PROJ-4821: Upgrade MCP client to wire spec 2026-07-28

**Type:** Task  **Priority:** P1  **Epic:** MCP protocol adoption

## Summary

Bump our MCP client to the 2026-07-28 wire spec release so we can talk to
servers that require the new version, keeping the legacy fallback for older
servers.

## Description

The platform team cut the 2026-07-28 wire spec. Servers on the new version
reject our current initialize handshake. We need the client's session layer
updated to the new spec's client requirements: version pinning, the required
initialize params, and session-id continuity after the handshake.

Out of scope: server-side changes, transport (HTTP/SSE) replacement.

## Acceptance Criteria

- [ ] Client sends `protocolVersion` "2026-07-28" on initialize
- [ ] Initialize params carry every field the spec requires
- [ ] Session id returned by the server is echoed on later requests
- [ ] Legacy fallback to "2025-06-18" still available for old servers
- [ ] Conformance per docs/mcp-client-spec.md (excerpt, normative)
