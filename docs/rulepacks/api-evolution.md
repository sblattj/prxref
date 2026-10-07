---
name: api-evolution
description: |
  Public API and interface evolution: breaking changes, removals, silent
  default changes, surface growth, deprecation, wire formats and error types.
---
# API evolution rules

Apply these only to a public interface: a library's exported API, a service
endpoint, a CLI, a config or file format, or an event schema that code outside
this repository consumes. A change counts as public only when the diff shows it
(an export list, a public modifier, a route, a schema file, a documented flag).
Every finding must quote the changed line and name what a caller loses. Do not
guess how many callers exist.

Do not flag, under any rule below:

- additive changes that old callers can ignore (a new optional parameter at the
  end with a default, a new function, a new optional field, a new enum member
  on an output the docs say may grow);
- internal, private or underscore-prefixed code, and code outside the exported
  surface;
- test-only code, fixtures, examples and benchmarks;
- a project that states it is pre-1.0 or experimental in the diff, the README
  or a changelog, unless the same diff also promises stability;
- a major-version bump or a documented breaking release that the diff itself
  makes (changelog entry, version bump, migration note).

## Breaking signature change
Source: Semantic Versioning 2.0.0 (semver.org), rules on the public API and
the MAJOR version.

- error: a public function, method, constructor or endpoint loses a parameter,
  reorders positional parameters, makes an optional parameter required, narrows
  an accepted type, or widens a returned type, with no overload or shim that
  keeps the old call working.
- Why: callers compiled or written against the old signature break at their
  next upgrade, and a MINOR or PATCH release must not do that.
- Do not flag: a new trailing optional parameter, a widened accepted type, a
  narrowed return type, or a change to a symbol the diff shows is internal.

## Public symbol removed or renamed without a deprecation path
Source: Python PEP 387 (Backwards Compatibility Policy), which requires a
deprecation period before removal; Semantic Versioning 2.0.0, item on
deprecation.

- error: a public class, function, constant, module, route, flag, environment
  variable or config key is deleted or renamed, and the old name no longer
  resolves.
- warning: the old name is kept but nothing marks it deprecated (no
  annotation, warning, doc note or alias comment).
- Why: a rename is a removal plus an addition for every caller. A forwarding
  alias with a deprecation notice lets callers migrate on their own schedule.
- Do not flag: removal of a symbol that was already deprecated in an earlier
  release the diff or changelog names, or a rename inside private code.

## Silent change of a default
Source: Hyrum's Law (hyrumslaw.com): with enough users, every observable
behaviour is depended on by somebody.

- warning: a default value, ordering, timeout, limit, retry count, encoding,
  case rule, trailing-slash rule or enabled-by-default switch changes, and the
  diff adds no changelog or release note and no opt-in for the old behaviour.
- error: the changed default alters stored data, security posture or the
  meaning of an existing call (for example a permissive default becoming
  strict, or a unit changing from seconds to milliseconds).
- Why: callers that never passed the argument get different behaviour without
  touching their code.
- Do not flag: a default that only fixes a documented bug the diff cites, or a
  new default for a parameter that did not exist before.

## Public surface widened without need
Source: Effective Java, Item 15 (minimize the accessibility of classes and
members) and Item 22 (interfaces for defining types); Hyrum's Law.

- warning: a helper, field, type or module is exported, made public or added to
  a package's public index when the diff shows only same-package or same-file
  use, and no caller outside the module is added.
- warning: a new public method is added to an interface or abstract base that
  outside code implements, with no default implementation, so every existing
  implementer breaks.
- Why: everything public becomes a commitment to maintain and a candidate for
  accidental dependence.
- Do not flag: a symbol made public because the same diff adds an outside
  caller, or an export the PR description gives a reason for.

## Deprecation without notice or version
Source: Keep a Changelog (keepachangelog.com) on "Deprecated" and "Removed";
Semantic Versioning 2.0.0 on deprecation in a MINOR release.

- warning: a public symbol is marked deprecated with no replacement named, no
  version in which it was deprecated, or no planned removal version.
- warning: a diff that changes public behaviour touches no changelog, release
  note or migration doc, in a repository where earlier changes did (the
  changelog file is visible in the diff context or the file list).
- Why: a deprecation a caller cannot act on is only noise.
- Do not flag: a repository with no changelog convention visible in the diff,
  or a change the diff shows is not public.

## Serialized or wire format changed without versioning
Sources: Protocol Buffers documentation on updating message types (reserved
field numbers, never reuse a tag); Google API Improvement Proposals AIP-180
(backwards compatibility); Semantic Versioning 2.0.0.

- error: a field in a persisted or transmitted schema (JSON, protobuf, Avro,
  database column, event payload, file header) is removed, renamed, retyped,
  or has its meaning changed, or a numeric field tag or enum value is reused.
- error: a required field is added to an input that existing producers send
  without it.
- warning: a route, media type or message version is changed in place instead
  of adding a new version alongside the old one.
- Why: stored data and in-flight messages outlive the code that wrote them, and
  consumers deploy on their own schedule.
- Do not flag: a new optional field, a new route version added next to the old
  one, a schema that the diff shows is private to one process, or a migration
  the same diff supplies that rewrites existing data.

## Error behaviour callers depend on
Sources: Hyrum's Law; Semantic Versioning 2.0.0.

- error: a public function now raises, returns or responds with a different
  error type, error code, HTTP status or exit code for an existing failure,
  such as a specific exception replaced by a generic one, an error turned into
  a silent success, or `404` turned into `400`.
- warning: a new failure mode is added to an operation callers treat as
  infallible, with no mention in its documentation or signature.
- Why: callers branch on the error they know. A changed error type silently
  bypasses their handlers.
- Do not flag: a new error for an input that was previously undefined
  behaviour, a more specific subtype of the old error, or a change to an error
  message text that no documented contract promises.
