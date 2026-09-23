# MCP Client Wire Spec — release 2026-07-28 (excerpt)

Scope: client-side requirements for protocol version `2026-07-28`. This
excerpt is normative for the client upgrade epic.

## 1. Protocol version

- The initialize request MUST carry `protocolVersion` exactly
  `"2026-07-28"`.
- On a server rejection, the client MAY retry once with `"2025-06-18"`
  (legacy fallback).

## 2. Initialize params (required fields)

The initialize params object MUST include ALL of:

- `protocolVersion`: string (see section 1)
- `capabilities`: object (MAY be empty)
- `clientInfo`: object with REQUIRED string fields:
  - `name`: the client product name
  - `version`: the client semantic version

An initialize request missing any required field MUST be treated as a spec
violation; conforming servers SHOULD reject it.

## 3. Session continuity

- The server MAY return an `Mcp-Session-Id` response header on initialize.
- If present, the client MUST store it and send it as the `Mcp-Session-Id`
  request header on every subsequent request. Omitting it is a protocol
  violation.

## 4. Forbidden

- Clients MUST NOT send `initialize` more than once per session.
