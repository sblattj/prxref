# Known-vulnerability scan (#36)

With `PRXREF_VULN_SCAN=on`, prxref looks up the dependency pins a PR changes
in the [OSV.dev](https://osv.dev) advisory database and posts one finding per
advisory that affects a changed pin. The scan makes no model call, so its
findings are deterministic.

It is off by default. On, it sends the names and versions of the changed
dependencies to a third party, the OSV API at `PRXREF_OSV_URL`. Nothing else
from the PR leaves the runner. That is why `PRXREF_VULN_SCAN` and
`PRXREF_OSV_URL` are env-only: a `.prxref.toml` in the repository cannot
turn the scan on or point it at another host.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `PRXREF_VULN_SCAN` | `off` | `on` runs the scan. Env-only. |
| `PRXREF_OSV_URL` | `https://api.osv.dev` | Base URL of the OSV API, for a mirror or a proxy. An http(s) URL, else exit `2`. Env-only. |
| `PRXREF_VULN_MAX` | `10` | Most vulnerability findings posted per PR. Also `vuln_max` in `.prxref.toml`. |

The eval harness pins the scan off (`prxref.evals.EVAL_PINS`), since its
answers change as advisories are published.

## Manifests

| File | OSV ecosystem | What counts as a pin |
|---|---|---|
| `package.json` | `npm` | `dependencies` and `devDependencies`: `1.2.3`, `=1.2.3`, `v1.2.3`, `^1.2.3` or `~1.2.3`. A caret or tilde range is queried at its lowest version. |
| `requirements*.txt` | `PyPI` | One `==` specifier, no wildcard. |
| `pyproject.toml` | `PyPI` | `[project] dependencies` with one `==` specifier. |
| `go.mod` | `Go` | `require` lines (single or block). `replace` and `exclude` are ignored. |
| `Cargo.toml` | `crates.io` | `[dependencies]`, `[dev-dependencies]` and `[build-dependencies]`: `1.2.3` or `=1.2.3`, queried at that version. |
| `pom.xml` | `Maven` | `<dependency>` versions, with properties and in-repository parents resolved. |
| `build.gradle`, `build.gradle.kts` | `Maven` | String notation `group:artifact:version`. |
| `libs.versions.toml` | `Maven` | `[libraries]` entries, with `version.ref` resolved. |

A changed dependency with any other version (a range, a tag, a URL, a
`${property}` the scan cannot resolve, no version at all) is not queried and
is counted as `unpinned`. A removed file is never scanned. Unchanged
dependencies are never queried. A manifest matched by
`PRXREF_REVIEW_SKIP_GLOBS` is kept out of the scan too, as it is kept out of
every other check.

The Maven and Gradle parsing reuses the JVM dependency context of #20
(`prxref.jvm_maven`, `prxref.jvm_gradle`).

## What "changed" means

When the head file can be read (through the forge's head-sha reads, or
`--repo-dir`), the scan rebuilds the base file by undoing the diff, parses
both sides whole and compares them. A pin is changed when it is new or its
declared version differs. A reorder, a reindent or a trailing comma is not a
change, and a Maven property bump changes every dependency that uses it.

Without a reader (for example `--diff-file` alone), the scan reads the hunks
only. A declaration on an added line is changed unless the same name and
version sit on a removed line. Each line needs its section inside the hunk:
a `package.json` line needs its `"dependencies"` opener in the hunk, for
example. A new file is always read whole.

## Findings

One finding per changed pin and advisory, on the added manifest line that
changed the pin: the first line naming both the dependency and its version,
else its version (a property line), else its name, else the file's first
added line.

OSV records that alias each other (a `GHSA` record and the `PYSEC` or `GO`
record for the same CVE) become one advisory. Its id is the `GHSA` one when
there is one.

| OSV severity | Posted as |
|---|---|
| `CRITICAL`, `HIGH` | `error` |
| `MODERATE` / `MEDIUM` | `warning` |
| `LOW` | `warning` |
| none given (or a CVSS vector only) | `warning` |

prxref has no severity below `warning`, so `LOW` and unrated advisories post
as `warning` too. The title keeps OSV's word (`MEDIUM` shows as `MODERATE`,
no rating as `UNKNOWN`), for example
`[HIGH] lodash 4.17.15: GHSA-p6mc-m468-83gw Prototype Pollution in lodash`.

The body names the advisory id, its aliases (CVE first), the severity, the
summary, the fixed versions, the link
`https://osv.dev/vulnerability/<id>`, and the source line `Source: the
OSV.dev advisory database, matched on the exact pinned version.` It ends with
`(deterministic check, no model)`.

The claim-checking quality passes (the manifest-claim check, the hedge
gate, the containment note, the guard-fix check and the contained-claim
check) leave these findings alone: the advisory text is OSV's, not a model
claim. They still count toward `PRXREF_MAX_ERROR_FINDINGS` and the other
severity caps of the quality gate.

## The per-PR cap

Over `PRXREF_VULN_MAX` advisories, the most severe are kept (ties by path,
line, package and id). The summary's top line then says how many more
matched but were not posted:

```text
> ⚠️ Dependency scan: 3 more known-vulnerability advisories matched this PR's changed dependencies but were not posted (PRXREF_VULN_MAX=10); the run record's vuln_scan lists the counts.
```

## Network and offline runs

The scan sends one `POST /v1/querybatch` with every changed pin, then one
`GET /v1/vulns/<id>` per advisory id (at most 200, 8 at a time). Each call
has a 10 second timeout and no retry, and the whole scan a 60 second
deadline.

Any failure (no network, a timeout, a non-2xx status, a body that is not
OSV's JSON) skips the whole scan. No vulnerability finding is posted, the
review goes on, and the exit code is the one the review would have had
without the scan.

A review whose every chunk failed ends with verdict `Error` and posts no
finding at all, deterministic ones included, so the scan's findings are not
posted either. The record still counts the `advisories` OSV returned, with
`posted` `0`.

## The run record

Every review's run record, and `--format json`, carry `vuln_scan`:

```json
{"status": "ok", "reason": null, "manifests": 2, "queried": 2,
 "advisories": 11, "posted": 10, "capped": 1, "unpinned": 1}
```

| Key | Meaning |
|---|---|
| `status` | `off` (the scan did not run), `ok` (OSV answered) or `skipped` (it could not be reached). |
| `reason` | Why it was skipped: the error class (`ConnectionError`, `ReadTimeout`), `HTTP <code>`, `bad response` or `deadline`. `null` otherwise. |
| `manifests` | Changed manifest files in the diff. |
| `queried` | Changed pins sent to OSV. |
| `advisories` | Advisories found, before the cap. |
| `posted` | Vulnerability findings left after the quality gates. |
| `capped` | Advisories left out by `PRXREF_VULN_MAX`. |
| `unpinned` | Changed dependencies skipped for having no concrete version. |

With the scan on, the trace also gets one `vuln_scan` event carrying the
same values.
