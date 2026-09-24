# Spec-Grounded Review — Design

> **This is a design record, not the manual.** It is the pre-implementation
> design, written against the tree at 0.11.x; the feature shipped in 0.14.0.
> Its `file:line` citations, line ranges and version numbers are historical
> and no longer match the source, so resolve a citation by the symbol it
> names. Where what shipped differs materially from the design, an
> **As built (0.14.0)** note says so in place, and the shipped behaviour wins.
>
> - The dataset contract for `tests/evals/` is
>   [tests/evals/README.md](../tests/evals/README.md), not §7.
> - 🟦 in this document is the pre-0.14 `outofscope` glyph. `outofscope` now
>   renders ⬜, and 🟦 marks a finding outside the ticket's scope. The glyphs
>   live in one table, `prxref.markers`.
> - To use the feature, read the README's
>   [Review Against a Spec or Ticket](../README.md#review-against-a-spec-or-ticket),
>   [docs/quality.md](quality.md#spec-grounding) and
>   [docs/deploy.md](deploy.md#7-spec-sources-in-ci-and-on-the-daemon).

Status: design record (v1), implemented in 0.14.0. Owner: prxref.

## 0. What this feature is

Today every prxref seat reviews the diff against generic bug classes only
(`prompts/worker.md:1-23`). Spec-grounded review adds a second axis: the
operator supplies scope/intent — a ticket plus a docs/spec corpus — and the
reviewer must additionally catch **violations of that spec**, emitted as a new
`spec` severity (🔍), distinct from `error` 🟥 / `warning` 🟧 / `outofscope` 🟦.

Motivating case: a repo adapting to the MCP spec version `2026-07-28` is
reviewed against the spec's client/server best-practices docs, so "the diff
sends the protocol-version header the spec forbids" surfaces as a spec finding
rather than nothing.

### Locked decisions (user-approved, not to be re-litigated)

1. **Input**: repeatable `--spec <url-or-path>` flag + `PRXREF_SPEC_SOURCES`
   (comma/space separated). Accepts public web URLs, local file/dir paths, and
   Jira ticket URLs.
2. **Docs handling**: fetch + prune. Extract headings/constraints, prune to
   diff-relevant slices, inject the result. Never post full docs raw. No
   persistent graph index in v1 (§10).
3. **Jira auth**: REST + HTTP basic auth via `PRXREF_JIRA_BASE_URL` /
   `PRXREF_JIRA_EMAIL` / `PRXREF_JIRA_API_TOKEN`, plus anonymous access for
   public boards. (MCP noted as an alternative fetch path, not designed here.)
4. **Severity**: new `spec` severity, symbol 🔍, own ordering and fallback
   rules (§5).

### Non-negotiable posture carried over

- stdlib + `requests` only in core; the spec fetcher uses the same zero-extra
  dependency stack as the forges (`forges/github.py:10`).
- Non-blocking: any spec-fetch failure degrades to "reviewed un-grounded, with
  a note in the summary". Exit 2 remains reserved for config errors
  (`cli.py:8-15`).
- Every posted comment keeps model attribution (`forges/base.py:72`).

---

## 1. Input plumbing

### 1.1 CLI flag

Add to the `review` subparser in `_build_parser` (`cli.py:61-86`), beside
`--max-chunks`:

```python
rev.add_argument(
    "--spec",
    action="append",
    default=None,
    metavar="URL_OR_PATH",
    help="spec/ticket source to review against; repeatable (PRXREF_SPEC_SOURCES otherwise)",
)
```

`_cmd_review` passes `spec_sources=args.spec` into `_run_review`, which
forwards it as a `load_config` override exactly the way `max_chunks` does
today (`cli.py:175-180`), with
`source_labels={"spec_sources": "--spec"}` so a malformed value is reported as
the flag the operator typed. This keeps `--spec` on the identical
range-checked path as the env var and preserves the existing exit-2 contract
(`cli.py:271-284`).

Precedence: **`--spec` replaces `PRXREF_SPEC_SOURCES` when given; there is no merge.**
This matches the documented precedence "built-in defaults < environment <
overrides" (`config.py:104`).

The webhook daemon inherits the feature for free: `_webhook_handler` calls the
same `_run_review` (`cli.py:220-231`), so `PRXREF_SPEC_SOURCES` in the daemon's
environment grounds every webhook-triggered review.

### 1.2 Config keys

Six new keys, all in `config._DEFAULTS` (`config.py:136-180`):

| key | env var | type | default |
|---|---|---|---|
| `spec_sources` | `PRXREF_SPEC_SOURCES` | list (comma **and** whitespace separated) | `[]` |
| `spec_max_chars` | `PRXREF_SPEC_MAX_CHARS` | int | `120000` |
| `spec_digest_tokens` | `PRXREF_SPEC_DIGEST_TOKENS` | int | `3000` |
| `jira_base_url` | `PRXREF_JIRA_BASE_URL` | str | `""` |
| `jira_email` | `PRXREF_JIRA_EMAIL` | str | `""` |
| `jira_api_token` | `PRXREF_JIRA_API_TOKEN` | str | `""` |

Table placement:

- `_LIST_KEYS` (`config.py:189`) += `spec_sources`. The existing list
  coercion (`_coerce_env`, `config.py:295-296`) splits on commas only; widen it
  to `re.split(r"[,\s]+")` so `PRXREF_SPEC_SOURCES` accepts space-separated values as
  locked. The only other list key is `llm_models`, whose entries can never
  contain spaces, so the widened split is a no-op for it.
- `_INT_KEYS` (`config.py:182-186`) += `spec_max_chars`, `spec_digest_tokens`;
  `_RANGES` (`config.py:252-264`) += `_Range(0)` for both (positive,
  unbounded above, like every other size knob — `config.py:206-244`).
- `jira_*` are plain string keys: no int/float/choice table entry, same as
  every token key today.

Semantics of the two ints: `spec_max_chars` bounds **raw fetched bytes per
source** (post-decode); `spec_digest_tokens` bounds the **final digest text**
injected into prompts, converted at the same 4-chars-per-token estimate the
systemic digest uses (`systemic.py:70`).

### 1.3 What is deliberately NOT a config error

Missing/partial Jira credentials are **not** `ConfigError`. `load_config`
validates values, not combinations; a ticket URL with no credentials takes the
fetch-failure path (§2.5) and the review proceeds un-grounded with a note. The
one thing the CLI will refuse is a malformed `--spec`/`PRXREF_SPEC_SOURCES` value —
but since the value is an opaque string list, there is nothing to range-check;
v1 adds no URL validation at config time. (Judgment call J2, §11.)

---

## 2. Fetch layer: new module `src/prxref/specs.py`

One module, mirroring the shape of `systemic.py`: deterministic helpers, data
in / text out, no LLM in the loop, docstrings on public API, no inline
comments.

### 2.1 Data shape

```python
@dataclass
class SpecSource:
    origin: str          # the string the operator supplied, verbatim
    kind: str            # "file" | "dir" | "url" | "jira"
    text: str            # extracted plain text; "" when failed
    error: str           # "" on success, human-readable reason otherwise
```

```python
@dataclass
class TicketRef:
    base_url: str        # resolved REST base (env override or URL host)
    key: str             # e.g. "PROJ-123"
    url: str             # original ticket URL
```

### 2.2 Dispatch

`parse_ticket_url(url: str) -> TicketRef | None` recognizes:

- `{host}/browse/{KEY}-{n}` (Jira Cloud + Server classic),
- `{host}/rest/api/{2|3}/issue/{KEY}-{n}` (raw REST links),
- `{host}/jira/software/c/projects/{KEY}/issues/{KEY}-{n}` (Cloud new UI).

Resolution: `PRXREF_JIRA_BASE_URL` wins when set (self-hosted boards often sit
behind a different REST host than the browse URL); otherwise the ticket URL's
own `scheme://host`.

`fetch_specs(sources, *, max_chars, jira_base_url, jira_email, jira_api_token, session=None) -> list[SpecSource]`
dispatches per source, in the order given:

- `http://`/`https://` prefix + ticket match → Jira REST (§2.4).
- `http(s)` otherwise → plain GET (§2.3).
- existing filesystem path → file or directory (§2.3).
- anything else → `SpecSource(error="not a URL or path")` — a per-source
  failure, never an abort. The reason carries no path, because failure
  reasons are posted (§6.4).

> **As built (0.14.0):** `parse_ticket_url` also accepts a context path of up
> to two segments before `/browse/` and `/rest/api/{2|3}/issue/` (Jira Server
> under `/jira`), the Cloud team-managed issue view without `/c/`, and a
> Cloud board URL on a `/jira/` path carrying `selectedIssue`. The
> context-path bound keeps a Bitbucket Server `/projects/P/repos/R/browse/…`
> URL from matching. A local path goes through
> `text_inputs.confine_to_cwd` before anything stats or reads it (§2.3).

### 2.3 Web + local fetching

- **HTTP**: one `requests.Session` built like the forge sessions —
  `LoggingRetry(total=3, backoff_factor=1, status_forcelist=[429,500,502,503,504],
  allowed_methods=frozenset({"GET","HEAD","OPTIONS"}))` — reusing the read-only
  retry policy verbatim from `forges/github.py:57-86` (same duplicate-POST
  reasoning; the spec fetcher only ever GETs). Timeout
  `SPEC_FETCH_TIMEOUT_S = 15` (module constant, not a config key in v1 —
  §10). Content must arrive as `text/*`, `application/json`, or a text-like
  markdown/html type; other content types fail that source.
- **Size cap**: stream via `iter_content`, decode incrementally, stop at
  `max_chars` and append an explicit `[source truncated at N chars]` marker —
  truncation is announced, never silent (the same doctrine as
  `systemic.TRUNCATION_MARKER`, `systemic.py:72-74`).
- **HTML**: strip tags with `html.parser` (stdlib) to plain text before
  extraction — spec pages are HTML more often than markdown.
- **Local path**: read UTF-8 (errors → per-source failure), same
  `max_chars` cap. A directory reads `*.md`, `*.markdown`, `*.txt`, `*.adoc`
  in sorted filename order, capped at the first 20 files
  (`SPEC_DIR_MAX_FILES = 20`), each through the same cap.

> **As built (0.14.0):** the spec session does not reuse the forge retry
> policy. `specs._create_default_session` retries **once**
> (`LoggingRetry(total=1, …)`), with no backoff sleep before that retry, and
> ignores `Retry-After` (`respect_retry_after_header=False`). The daemon
> reviews one PR at a time, so a spec host that is down or asks for time is
> skipped, not waited for. Two module constants bound a source, and neither
> depends on `--timeout`:
>
> - `SPEC_FETCH_TIMEOUT_S = 15`: each attempt's connect timeout and its
>   timeout per read;
> - `SPEC_FETCH_BUDGET_S = 30`: the wall clock for the body, counted from
>   before the request.
>
> `specs._read_stream` reads the body one socket read at a time
> (`raw.read1(8192, decode_content=True)`, or `iter_content(chunk_size=1)`
> on urllib3 1.x) and checks a monotonic deadline before each read, so a
> trickling host costs at most the budget plus one read timeout, about 45 s.
> A host that accepts the connection and never answers costs about 30 s:
> two attempts of 15 s. The byte
> cap is `4 * max_chars + 4`. The charset comes from the header, then an HTML
> `<meta>` in the first 1024 bytes, then strict UTF-8 without a BOM, then
> cp1252 with replacement. HTML is stripped after the cut, and the
> truncation marker is appended after the stripping.
>
> Local files are read in bounded memory as strict UTF-8 without a BOM
> (`text_inputs.read_capped_file`). The marker is appended only when a file
> is longer than `max_chars`. A path under the working directory must still
> resolve under it once its symlinks are followed, while an absolute path
> outside it is read as given. A directory is read with `os.scandir`: it
> skips every symlinked entry, and it skips a file it cannot read or decode
> without failing the others. Skipped names are logged at WARNING only, and
> they become the source's error only when no file was read. No reason
> carries a local path: an `OSError` is reported by its class and
> `strerror`.

### 2.4 Jira REST (primary path)

```
GET {base}/rest/api/2/issue/{key}?fields=summary,description,issuetype,labels
Authorization: Basic base64(email:api_token)
```

- Credentials present (both `jira_email` and `jira_api_token` non-empty) →
  basic auth; absent → anonymous request (public boards work with no config).
- Render the ticket as plain text: `Summary: …`, `Type: …`, `Labels: …`,
  then the description body verbatim (Atlassian wiki-markup or ADF-plain —
  v1 passes the text through; it is prose, and the extractor §3.2 reads prose
  fine).
- HTTP 401/403 without credentials → error text that names the three
  `PRXREF_JIRA_*` variables, because that is the fix an operator can act on.
  The reason may end up posted (§6.4), so it is written to survive
  `redact_for_post` (`orchestrator.py:202-235`): variable NAMES only, never
  values, host kept (a host is not a credential and `_URL_RE` will strip the
  original ticket URL if it reappears).
- MCP as an alternative fetch path is noted here as a future option only
  (§10); REST basic-auth is the designed primary per the locked decisions.

> **As built (0.14.0):** credentials only ever go to `PRXREF_JIRA_BASE_URL`.
> Basic auth is sent only when that variable, `PRXREF_JIRA_EMAIL` and
> `PRXREF_JIRA_API_TOKEN` are all set; every other ticket fetch is
> anonymous. Credentials set without a base URL are withheld, with a WARNING
> naming `PRXREF_JIRA_BASE_URL`. A plain-`http://` base URL is used, with a
> WARNING. The variable-naming hint also fires on an anonymous 404, because
> Jira Cloud hides a private issue from anonymous readers as 404. The
> response streams through the same budgeted reader as a web page (§2.3): a
> body over the byte cap, or a 200 that is not a JSON issue, fails the
> source cleanly. A `Type:` or `Labels:` line whose value is empty is left
> out, because every non-blank ticket line becomes a digest constraint.

### 2.5 Failure doctrine

`fetch_specs` never raises. Every exception inside a source becomes that
source's `error` string. A run where **all** sources failed is not an error
run: the pipeline behaves exactly like a run with no specs, plus a summary
note listing the failures (§6.4). This is the same shape as `list_threads`
best-effort failure (`orchestrator.py:447-451`).

---

## 3. Prune/digest: constraint extraction and diff-relevance pruning

### 3.1 Goal

Each fetched source becomes a compact **spec constraint digest**: a bounded,
deterministic text the worker prompts can hold, containing the constraints
that could plausibly be violated by *this* diff. Deterministic and model-free,
like `systemic.build_digest` (`systemic.py:326-414`): same input → same text,
so evals and traces stay stable.

### 3.2 Extraction (regex pass, no model)

Per source, in document order, collect:

- **Headings**: `^#{1,6} ` (markdown), `^\n[A-Z][^\n]{0,80}\n[-=]{3,}$`
  (setext/asciidoc) — kept as `[heading]` scoping lines so a constraint stays
  attached to its section (e.g. "Client BEST PRACTICES" vs "Server").
- **Normative statements**: sentences carrying RFC-2119-strength keywords,
  matched inside blocks rather than physical lines (see *As built* below) —
  `MUST`, `MUST NOT`, `SHALL`, `SHALL NOT`, `REQUIRED`, `SHALL NOT`,
  `FORBIDDEN`, `MUST NEVER` (strength 3); `SHOULD`, `SHOULD NOT`,
  `RECOMMENDED`, `recommended to`, `forbidden to` (strength 2); `MAY`,
  `can`, `discouraged` (strength 1, lowest keep-priority). Case-sensitive for
  the RFC-2119 all-caps forms, case-insensitive for the prose forms.
- **Version pins**: `\b\d{4}-\d{2}-\d{2}\b` (spec revisions like
  `2026-07-28`), `\b(?:v?\d+\.\d+(?:\.\d+)?)\b` adjacent to the words
  `version|protocol|revision|draft`. A pinned version inside a kept constraint
  is quoted verbatim; a version pin on its own line is kept as its own
  constraint.
- **Naming/shape rules**: sentences matching
  `(?:MUST|SHOULD|SHALL)[^.]{0,120}(?:named|name|prefix|suffix|header|field|snake_case|camelCase|lowercase|uppercase)`
  — the "tools MUST be named `mcp__`"-class constraints.
- **Ticket text** (`kind == "jira"`): the summary always, and the description
  kept in full up to a per-source sub-budget (`min(spec_max_chars // 4, 6000)`
  chars) — the ticket is the *scope/intent*, small and high-value; it is not
  pruned to keywords.

Each kept unit renders as one line:

```
[spec:{origin-short}#{anchor-or-line-N}] (MUST) <statement text, single line, ≤400 chars>
```

`origin-short` is the source's basename or URL path tail; the Jira ticket's
tag is `[ticket:{KEY}]`.

> **As built (0.14.0):** matching runs on blocks and sentences, not physical
> lines (`specs._spec_units`).
>
> - A paragraph or list item joins its wrapped and indented continuation
>   lines. A blank line, heading, setext underline, code fence, table row or
>   new list item ends a block, and a table row or fenced line is a unit of
>   its own.
> - Each block is split into sentences (`e.g.`, `i.e.`, common abbreviations
>   and code spans never end one), and each sentence is matched on its own. A
>   block over 400 characters, or one with two or more matching sentences,
>   yields one unit per matching sentence. Otherwise the block is kept whole,
>   unless its only match is a prose `can`/`discouraged` or a bare version
>   pin, which keeps just that sentence.
> - Known limitation: only `.`, `;`, `!` or `?` ends a sentence, so keyword
>   lines with no such punctuation, one after another in a paragraph with no
>   blank line between them, form one sentence and so one unit, labelled with
>   the highest strength any of them carries (`specs._split_sentences`,
>   `specs._strength`): `Clients MAY cache tokens` directly above
>   `Servers MUST reject expired tokens` is a single `(MUST)` unit.
> - A unit ending in `:` carries the list that follows it, up to the cap. A
>   version pin counts only on a line that is nothing but the pin. Every unit
>   is anchored `L{n}` on its block's first line.
> - Headings render as `[spec:{short}#{slug}] (heading) text`.
> - A ticket unit renders `[ticket:{KEY}] statement` with no strength label.
>   Every non-blank ticket line is kept, up to a fixed 6000 characters
>   (`TICKET_DESC_BUDGET_CHARS`); the `min(spec_max_chars // 4, 6000)`
>   sub-budget is deferred.
> - `origin-short` never carries a URL's query, fragment, userinfo or port,
>   but a credential that *is* the last path segment survives.

### 3.3 Diff-relevance pruning

Rank kept constraints, then keep until budget:

1. **Ticket constraints** (from `jira` sources): always kept first — scope
   beats relevance.
2. **Relevant spec constraints**: score = overlap between the constraint's
   content tokens and the diff's token set (file path segments + changed-line
   text, both compound-split). Tokenization mirrors the existing evidence
   vocabulary — 4-char floor, stopword-filtered, snake/camel parts split —
   which `quality.py:443-475` (`_tokens`, `_evidence_tokens`) already
   implements; v1 imports that logic (promote to a small shared helper or
   duplicate narrowly; implementer's choice, pinned by a parity test).
   Constraints with score ≥ 1 are "relevant".
3. **Unmatched MUSTs** (strength 3, score 0): kept after relevant ones,
   ordered by source order — a MUST the diff doesn't obviously touch is still
   the cheapest place a sweep can find a violation of the "absence is
   evidence" kind, exactly the migration-DDL argument in
   `systemic.py:17-22`.
4. SHOULDs (2) then MAYs (1) with score 0 are dropped first, then trailing
   unmatched SHOULDs, as budget runs out.

> **As built (0.14.0):** the score is the constraint's content tokens shared
> with the diff, minus the normative keywords themselves
> (`specs._NORMATIVE_TOKENS`: `must`, `shall`, `should`, `never`,
> `required`, …), so a diff line that merely says "must" matches nothing.
> Relevant constraints sort by score, then source order, then document
> order. The unmatched tail runs MUST, then SHOULD, then MAY, and the budget
> walk cuts it from the end. The tokenizer is `quality._tokens` /
> `_evidence_tokens`, imported, not duplicated.

### 3.4 Budget

`build_spec_digest(sources, files, token_budget) -> str` enforces
`token_budget × 4` chars (`PRXREF_SPEC_DIGEST_TOKENS`, default 3000 → ~12k
chars), walking the ranked list and stopping with a final
`[spec digest truncated: budget reached]` line. Every source that contributed
at least one line gets an origin tag so the model can cite *which* spec a
constraint came from; a source that contributed nothing after pruning gets a
one-line `[spec:{origin}: nothing diff-relevant kept]` so silence is
explained, not inferred away.

The digest is built once per review, after `parse_unified_diff` (files are the
pruning input) and before the worker fan-out.

> **As built (0.14.0):**
>
> - `build_spec_digest` returns `""` when sources were given but no unit was
>   extracted from any of them, so the prompts show their no-specs text.
> - A failed source gets no line in the digest; the grounding note (§6.4)
>   reports it. The not-contributed line uses the short origin:
>   `[spec:{short}: nothing diff-relevant kept]`.
> - Ranking interleaves sections, so a constraint's heading line is
>   re-emitted whenever the open section changes, and a unit with no heading
>   after one that had one is preceded by `[spec:{short}] (heading) (no
>   section)`.
> - `specs.constraint_count` counts only the constraint lines. The intro,
>   heading lines, truncation markers and bookkeeping lines never count, and
>   a digest with no constraint line is not injected at all (§4.1).

---

## 4. Prompt integration

### 4.1 Where the digest enters

Zero extra LLM calls in v1: the digest rides the **existing** per-chunk calls
and the **existing** systemic sweep. (A dedicated spec-sweep call is §10 /
judgment call J6.)

Plumbing:

- `reviewer.review_chunk` (`reviewer.py:285-337`) and
  `reviewer.review_systemic` (`reviewer.py:340-371`) gain a
  `spec_digest: str = ""` keyword.
- `_render_prompt` (`reviewer.py:117-139`) and `_render_systemic_prompt`
  (`reviewer.py:142-163`) add `.replace("{spec_digest}", …)` next to the
  existing placeholder fills; empty digest renders the literal
  `(no specs provided for this review)`.
- The prompt templates gain a `### Spec constraints` block inside the
  `## Review Context` section (between `{pr_description}` and the diff/digest
  block), containing `{spec_digest}`.
- `orchestrate_review` (`orchestrator.py:262-280`) gains
  `spec_sources: Sequence[str] = ()`; it calls `specs.fetch_specs` + builds
  the digest inside the same never-raise fence as every other stage
  (`orchestrator.py:67-71`), then threads the digest into `_run_workers` →
  `_run_worker` → `_invoke_chunk` → `review_chunk` and into `_run_sweep` →
  `review_systemic`. Trace: one `tracer.event("specs", …)` recording
  sources/ok/fail counts and final digest chars, between `build_chunks` and
  the worker span.

> **As built (0.14.0):**
>
> - The digest travels in `reviewer.PromptContext.spec_digest`, alongside
>   the other context blocks, rather than as a separate keyword on each call.
> - Only a grounded digest is injected: `grounded =
>   specs.constraint_count(digest) > 0`. A digest with no constraint line (no
>   sources, every source failed, nothing extracted, or a budget too small
>   for one line) is not injected, so every prompt shows `(no specs provided
>   for this review)`.
> - The trace records `specs ok|fail` with `{sources, ok, constraints}`. A
>   `fail` event (no source fetched) also carries the raw, unredacted
>   `reasons`, and a crashed spec stage emits `specs fail` with one
>   `spec stage crashed: …` reason. Digest chars are not recorded. The event
>   is emitted after the thread listing and before the worker fan-out. A
>   later `specs relabel {findings}` event marks ungrounded `spec` findings
>   relabelled `warning` (§5.2).
> - The operator-facing WARNING and INFO lines and the run record's
>   `spec_grounding` key are documented in
>   [docs/deploy.md](deploy.md#what-the-logs-the-run-record-and-the-trace-say).

### 4.2 What the prompts say

`prompts/worker.md` — extend `## Severity Vocabulary` (`worker.md:7-11`) with:

> - `spec` — the diff violates a constraint quoted in the Spec constraints
>   block below: a MUST/SHALL/required behaviour not implemented, a
>   forbidden behaviour implemented, a version pin or naming rule broken.
>   Only when specs were provided. Quote the violated constraint verbatim in
>   the body, prefixed `Spec: "`.

and a new `## Spec-grounded rules` section: emit `spec` **only** for a
conflict between the diff and a quoted constraint — never for a generic best
practice not present in the block; when the block reads `(no specs provided
for this review)`, `spec` is not a legal severity. Cite the diff line that
violates it (same `file`/`line` contract as every finding, `worker.md:62`).

`prompts/systemic.md` — same vocabulary bullet, plus one mission line: with
the whole-diff digest plus any spec constraints in view, the sweep is the
natural seat for cross-file spec classes (naming rules, version pins, "no
component may do X" rules), while per-chunk seats catch line-local
violations. Nothing else in the sweep's class list changes
(`systemic.md:5-15`).

`prompts/summary.md` — see §6.1 (counts line only).

Severity wording matters for eval scoring: the constraint quote convention
(`Spec: "…"`) gives expected.json a machine-checkable field and gives the
severity-consistency pass distinctive titles (§5.3).

> **As built (0.14.0):**
>
> - Each template splits at `## Review Context`. The severity bullet and the
>   `## Spec-grounded rules` section sit in the system half; the
>   `### Spec constraints` block with `{spec_digest}` sits in the user half,
>   before the diff or digest. So every prompt changed in 0.14.0, and the
>   worker and sweep prompts carry spec text on every run, with sources or
>   without.
> - Both rule sections add one override sentence: when the only basis for a
>   finding is a constraint quoted in the block, its severity is `spec`. The
>   sweep's rules add that its own built-in classes (RLS, secrets, …) are
>   never spec constraints.
> - The expected.json field this paragraph anticipates was not built. The
>   shipped dataset matches findings by `must_match` (see §7).
> - A `Spec: "…"` quote also exempts its verbatim digest text from the hedge
>   gate (§5.2).

---

## 5. Quality-gate integration: the `spec` severity

### 5.1 Vocabulary and ordering

`quality.SEVERITIES` (`quality.py:52`) becomes
`{"error", "warning", "spec", "outofscope"}`.

**Ordering** — `spec` sits below `warning`, above `outofscope`:

```
error(0) > warning(1) > spec(2) > outofscope(3)
```

Rationale: a spec violation is an operator-requested contract breach — always
worth reporting — but it is not claimed to break at runtime, so it does not
outrank a generic warning. Updated everywhere the rank table is restated:

- `quality._SEVERITY_RANK` (`quality.py:556`) — drives severity-consistency
  max-raise; `spec: 2`, `outofscope: 3`.
- `orchestrator._SEVERITY_RANK` (`orchestrator.py:129`) — inline-comment
  priority; `.get(f.severity, 3)` at `orchestrator.py:522` keeps unknown
  severities last without further edit.
- `formatter._SEVERITY_ORDER` (`formatter.py:25`) — summary table order.

**Unknown-severity fallback**: unchanged in kind. The gate drops any severity
outside `SEVERITIES` with `drop_reason="invalid severity: …"`
(`quality.py:890-895`), so unknown strings never post; the rendering-layer
fallbacks (`formatter._norm_severity` → `"outofscope"`, `formatter.py:44-49`;
`orchestrator._SEVERITY_MARKERS.get(…, "🟦")`, `orchestrator.py:971,:1070`)
continue to map unknown → outofscope 🟦 for anything that reaches them. A
model that misspells `spec` therefore loses the finding loudly (drop, audit
trail) rather than silently mis-rendering it.

> **As built (0.14.0):** `orchestrator._SEVERITY_MARKERS` no longer exists.
> Every glyph comes from one table, `prxref.markers.SEVERITY_MARKERS`
> (`error` 🟥, `warning` 🟧, `spec` 🔍, `outofscope` ⬜), and the orchestrator
> renders through `markers.severity_marker`. An unknown severity renders
> `markers.FALLBACK_MARKER`, which is ⬜, the `outofscope` glyph; 🟦 is now
> `markers.OUT_OF_TICKET_MARKER`, a scope prefix, never a severity.
> `formatter._norm_severity` still maps an unknown severity to `outofscope`.
> The three rank tables above shipped as designed.

### 5.2 Gate mechanics per pass

- **Location validation** (`quality.py:70-93`): severity-agnostic — a spec
  finding must name a diff path like any other.
- **Line align** (`quality.py:349-411`): severity-agnostic — body-citation
  precedence, `snap_line` tolerance 5 (`quality.py:62`), blank-anchor guard
  all apply unchanged. The `Spec: "…"` quote in the body must not be mistaken
  for a location; it is prose, and the citation regexes
  (`quality.py:258-265`) only match `path:line` / `line N` shapes.
- **Thread dedup** (`quality.py:527-553`): unchanged; a spec finding
  duplicating an existing human thread is suppressed like any other.
- **Severity consistency** (`quality.py:718-838`): `spec` joins the rank map,
  so a title-group containing both `spec` and `warning` members raises to
  `warning`, `spec`+`error` raises to `error`, etc. — current max-raise
  semantics preserved (judgment call J5).
- **Quality gate** (`quality.py:866-928`): confidence floor applies to spec
  findings unchanged. The **error cap ignores `spec`** — it counts
  `severity == "error"` only (`quality.py:913`) — so a spec-heavy review is
  not crowded out by, nor crowding out, `PRXREF_MAX_ERROR_FINDINGS`. If spec
  findings ever need their own cap, that is a future `PRXREF_MAX_SPEC_FINDINGS`
  (not in v1).
- **Sweep dedup** (`quality.py:577-615`): unchanged; a sweep-emitted spec
  finding restating a surviving chunk finding still dedups on
  `(file, normalize_title)`.

> **As built (0.14.0):** two behaviours this list did not plan.
>
> - **A new pass, `quality.apply_spec_grounding`,** runs after the team
>   severity map and before location validation, over chunk and sweep
>   findings alike. On an ungrounded run it relabels every `spec` finding as
>   `warning` (compared trimmed and lower-cased); it never drops a finding and
>   never raises one to `spec`, and on a grounded run it changes nothing.
>   Because it runs ahead of severity consistency, an ungrounded `spec` can
>   never lift a same-title sibling. A relabel logs one INFO line and one
>   `specs relabel {findings}` trace event.
> - **The hedge gate exempts a verbatim spec quote.** After a `Spec:` marker,
>   the text a finding copies verbatim from the injected digest, compared
>   case-insensitively, up to a closing quote, is not read for hedges, in a
>   finding of any severity. With no digest injected, nothing is exempt.
>
> The user-facing description, including the hedge exemption's known
> limitation, is [docs/quality.md](quality.md#spec-grounding).

### 5.3 Verdict and exit codes

- **Verdict unchanged**: `Request-Changes` iff an active `error` survives
  (`orchestrator.py:480-484`). `spec` findings do **not** move the verdict in
  v1 (judgment call J3). `PRXREF_FAIL_ON=any` already gates on spec findings
  for lanes that want a hard signal (`cli.py:250-260`); `FAIL_ON=error`
  ignores them, exactly as designed.

---

## 6. Output

### 6.1 Summary counts line

`prompts/summary.md:5` and the fallback template
(`orchestrator.py:237-243`) become:

```
🟥 {error_count} error · 🟧 {warning_count} warning · 🔍 {spec_count} spec · 🟦 {outofscope_count} outofscope
```

`_render_summary` (`orchestrator.py:965-993`): initialize
`counts = {"error": 0, "warning": 0, "spec": 0, "outofscope": 0}` and add the
`.replace("{spec_count}", …)` to the chain. The findings-bullet marker lookup
(`orchestrator.py:971`) and inline renderer (`orchestrator.py:1069-1076`)
need `"spec": "🔍"` in `_SEVERITY_MARKERS` (`orchestrator.py:124`); the
inline header renders `[SPEC]`.

`formatter.py` mirrors all of this (`:20-25`, counts at `:172-180`) so the
forge-neutral renderer and the orchestrator renderer cannot drift; the
existing orchestrator-template pin tests
(`tests/test_orchestrator.py:30`, `tests/test_formatter.py:103,:115`) are
updated to the new counts line in the same change.

> **As built (0.14.0):** the shipped counts line ends `⬜ {outofscope_count}
> outofscope`, not 🟦 (see §5.1), and `🔍 {spec_count} spec` appears on every
> run, with spec sources or without. The line after it in
> `prompts/summary.md` is `{spec_note}{ticket_note}`. A parity test holds
> the summary templates' glyph literals to `prxref.markers`.

### 6.2 Inline rendering

Spec findings post as ordinary inline comments (`orchestrator.py:519-538`)
with the 🔍 marker and `[SPEC]` label, anchored like any finding. They count
against `PRXREF_MAX_INLINE_COMMENTS` by confidence+severity rank; at rank 2
they yield the anchor to errors/warnings first, which is the intended
posture.

### 6.3 Attribution

Unchanged: `_format_finding`'s trailing
`Reviewed by prxref · model=…` (`orchestrator.py:1075`) and the summary's
`{attribution}` line carry model attribution as required by convention.

### 6.4 Grounding note in the summary

When `spec_sources` was non-empty, the summary gains one blockquote line after
the counts (via a new `{spec_note}` placeholder that renders `""` when no
specs were requested, keeping today's output byte-identical otherwise):

```
> 🔍 Spec-grounded: 3 source(s) · 41 constraint(s) injected
> ⚠️ Spec fetch failed for 1 source(s): ticket PROJ-9 (HTTP 401 — set PRXREF_JIRA_EMAIL/PRXREF_JIRA_API_TOKEN)
```

Failure reasons pass through `redact_for_post` before the post
(`orchestrator.py:202-235` doctrine: everything interpolated into a posted
comment is redacted; URLs are stripped by `_URL_RE`). A total fetch failure
renders only the failure line, and the review reads as un-grounded — which it
was.

> **As built (0.14.0):** a failed source is labelled by its 1-based position
> in the configured list and its kind, `source 2 (url)`, or `source 2` when
> the kind was never determined; it is never named by its origin. The
> example above is therefore stale: the shipped failure line reads
> `> ⚠️ Spec fetch failed for 1 source(s): source 1 (jira): …`. The
> `Spec-grounded` line counts every configured source and the constraint
> lines actually injected, so it can read `0 constraint(s) injected` when
> sources fetched but none held a constraint. Because the note reaches only
> a posted summary, the same facts also go to the log and the run record on
> every run with spec sources, `--no-post` included (see
> [docs/deploy.md](deploy.md#what-the-logs-the-run-record-and-the-trace-say)).

---

## 7. Golden eval dataset: `tests/evals/`

> **As built (0.14.0):** the dataset shipped; the runner and the scoring did
> not. **[`tests/evals/README.md`](../tests/evals/README.md) is the dataset
> contract**, and it replaces the layout and `expected.json` schema this
> section first proposed. In short:
>
> - Three cases ship, each a `tests/evals/case-NNN-<slug>/` directory holding
>   `ticket.md`, `docs/`, `diff.patch`, `expected.json` and `meta.json`.
> - `expected.json` is a flat JSON array of must-find entries, each with
>   exactly `id`, `file`, `line_hint`, `severity`, `must_match` (a substring,
>   or a regex when prefixed `re:`) and `source` (`spec` for a planted
>   violation, `generic` for a plain bug). There is no `title_hint`, no
>   `constraint_ref` and no `nonfindings` list.
> - `meta.json` carries a planted-violation manifest that maps one-to-one
>   onto the `source: "spec"` entries. It carries no score floor.
> - `tests/evals/test_evals.py` is a structural scorer only. It proves every
>   case is well-formed and self-consistent and runs no LLM:
>   `uv run pytest tests/evals -q`.
>
> `source` keeps the meaning proposed here: `generic` marks an ordinary bug
> the unguided reviewer should also catch, so a later scoring pass can check
> that grounding costs no generic recall.

### 7.1 Runner (planned, not built)

> **Planned, not built.** Nothing in §7.1 or §7.2 exists in 0.14.0: there is
> no `harness.py`, no plumbing or recorded stub-LLM mode, and no P/R/F1
> scoring. What did ship, outside this design, is one pipeline run per case:
> [`tests/evals/test_eval_replay.py`](../tests/evals/test_eval_replay.py)
> reviews each case with one replay-mode `prxref review` call against a stub
> LLM that finds nothing, which proves the wiring, not the review. Scoring the
> findings against `expected.json` is still a manual, offline step
> ([`tests/evals/README.md`](../tests/evals/README.md), "Running a case"). The
> design below is kept for a later pass that automates that scoring.

`tests/evals/harness.py` (data-local, not shipped) + a thin wrapper in
`tests/evals/test_evals.py` (the file that holds today's structural scorer)
so `uv run pytest tests/evals -q` runs it in CI:

1. Load the case; build sources as `["<case>/ticket.md", "<case>/docs"]`
   (file sources — the fetch layer is exercised by `test_specs.py` with
   mocked sessions, not here).
2. Drive the real pipeline: `parse_unified_diff(diff.patch)` →
   `specs.build_spec_digest` → `reviewer.render prompt` paths → **stub LLM**
   → the full quality pass chain (`apply_location_validation` →
   `apply_line_align` → … → `apply_sweep_dedup`, `orchestrator.py:453-475`)
   → active findings.
3. **Stub LLM modes**:
   - *plumbing mode* — per-case `stub_response.json` hand-written findings
     exercising gate/anchor/dedup edges (drifted lines, sub-floor confidence,
     invalid severities, sweep-vs-chunk duplicates);
   - *recorded mode* — a real model's captured response for the case,
     replayed verbatim (regression mode for model drift).
   The stub satisfies the `LLMClient.invoke` shape (`reviewer.py:219-224`),
   is injected the way `test_reviewer.py` doubles are today.
4. Score **post-gate, post-alignment** output against `expected.json` —
   never raw model output, so the eval measures what a PR author receives.

### 7.2 Scoring metric (planned, not built)

> **Against the shipped dataset:** `expected.json` has no `line`,
> `title_hint`, `constraint_ref` or `nonfindings`. A scorer would match on
> `file`, on `line_hint` within the line tolerance, and on `must_match`
> against the finding body; the specificity check has no list to read; and
> a per-case floor would live in the scorer, since `meta.json` holds none.
> The line self-check below did ship, tighter than designed:
> `test_line_hints_anchor_added_lines` requires every `line_hint` to be an
> added line of the case diff, and `test_expected_json_schema` requires it
> to be at least 1, so there is no file-level `0`.

Matching rule, in order: same `file` (exact) **and**
`|pred.line − exp.line| ≤ quality.DEFAULT_LINE_TOLERANCE` (5,
`quality.py:62`) **and** token overlap between title+body and
`title_hint + constraint_ref` using `quality.normalize_title` /
`_tokens` (`quality.py:443-475, :561`). Expected `line: 0` (file-level)
matches any line in the same file.

- **Spec recall** = matched expected `source:"spec"` entries ÷ total spec
  entries.
- **Spec precision** = predicted `severity=="spec"` findings that match a spec
  entry ÷ all predicted spec findings.
- **Class-miss** (counted, not folded in): a prediction that matches an
  expected spec entry but carries a non-spec severity. Binary good/bad hides
  this third outcome — a "recall hit" that arrives as a generic warning is a
  grounding failure the F1 must not launder.
- **Specificity**: any predicted spec finding matching a `nonfindings` entry
  is a counted false-positive of the worst kind.
- **Anchor check**: predicted lines are snapped through the production
  `apply_line_align` before scoring, so eval anchor tolerance can never
  diverge from shipped behavior. Self-check: each expected line must be an
  added line of the case diff (or 0) — validated when the case is loaded, so
  a stale `expected.json` fails loudly, not as a mysterious 0-recall.
- Aggregate: per-case P/R/F1 table plus means; the pytest wrapper asserts a
  per-case floor (F1 ≥ 0.8, class-miss ≤ 1, zero specificity hits) from
  `meta.json`.

---

## 8. Config surfaces checklist + docs updates

The four-surface rule is enforced, not aspirational:
`tests/test_docs_consistency.py:58-118` fails the build when a `_DEFAULTS` key
misses any surface, and when `docs/env-vars.md`'s **stated counts** go stale.
For six new keys:

- [x] `src/prxref/config.py`: `_DEFAULTS` + six keys; `_LIST_KEYS` +=
      `spec_sources`; `_INT_KEYS` += `spec_max_chars`, `spec_digest_tokens`;
      `_RANGES` += both; module docstring env table (lines 5-83) += six
      entries.
- [x] `.env.example`: six commented entries with defaults (file pattern
      `.env.example:11-60`).
- [x] `docs/env-vars.md`: six table rows (LLM & Pipeline section for the
      three spec keys; a new "Spec Sources / Jira" subsection for the three
      `PRXREF_JIRA_*` keys); update the stated totals —
      `**35** configuration keys` → `**41**`, and
      `for 36 accepted variable names` → `for 42` (`docs/env-vars.md:122-128`;
      the test asserts these strings, `test_docs_consistency.py:103-118`).
- [x] `README.md`: short "Review against a spec or ticket" section with one
      copy-paste example.
- [x] `src/prxref/cli.py`: `--spec` flag + plumbing (§1.1).
- [x] Prompt templates: `worker.md`, `systemic.md` (§4.2), `summary.md` (§6.1).
- [x] No new `_CHOICE_KEYS` entry is needed (no enum-valued key in this
      feature).

> **As built (0.14.0):** every item shipped. The 35→41 / 36→42 totals above
> are history: other keys landed between this design and the spec keys, and
> when the spec keys landed the table held 55 configuration keys and 56
> accepted names (the 55 plus the one deprecated alias). The test does not
> hard-code either number. It computes them from `len(config._DEFAULTS)` and
> `config._LEGACY_ENV_ALIASES` and checks the totals `docs/env-vars.md`
> states.

---

## 9. Testing plan and rollout

### 9.1 Unit tests

- `tests/test_specs.py` (new): dispatch (file / dir / URL / ticket-URL
  shapes / garbage); Jira auth present vs anonymous vs 401-message content;
  size caps + truncation markers; HTML stripping; dir file cap + sort order;
  `fetch_specs` never raises; digest determinism (same inputs twice →
  identical bytes); pruning rank order (ticket > relevant > unmatched-MUST >
  SHOULD); budget truncation marker; relevance scoring parity with
  `quality._tokens`.
- `tests/test_quality.py`: `spec` passes the gate; rank order
  error > warning > spec > outofscope in `apply_severity_consistency`; unknown
  severity still dropped; error cap ignores spec findings.
- `tests/test_orchestrator.py`: `{spec_count}` + `{spec_note}` in the summary;
  🔍 marker in bullets and inline cards; verdict NOT moved by spec-only
  findings; all-sources-failed run completes with the failure note; digest
  reaches the worker prompt (captured via stub LLM) and the sweep prompt;
  `redact_for_post` applied to fetch-failure notes.
- `tests/test_reviewer.py`: `{spec_digest}` replacement; `(no specs provided…)`
  default; `spec` severity passes through unfiltered (reviewer never gates,
  `reviewer.py:299-300`).
- `tests/test_cli.py`: `--spec` repeatable; override replaces env;
  `--spec` values survive into `orchestrate_review` kwargs.
- `tests/test_docs_consistency.py`: must stay green untouched — it is the
  checklist enforcer (§8).

### 9.2 Rollout

- Version: `0.11.1` → **`0.12.0`** (`pyproject.toml:3`). New user-facing
  flag + new severity = feature minor; no breaking change (the severity
  vocabulary grows, but unknown-severity handling was already
  drop-with-audit, so older consumers of run records degrade safely).
- `CHANGELOG.md`: one feature entry in the style of the severity-rename entry
  (`CHANGELOG.md:173-184`): what `--spec`/`PRXREF_SPEC_SOURCES` accept, the 🔍
  `spec` severity and its ordering, Jira env vars, fetch-failure
  non-blocking behavior, and the eval harness. Explicit note that verdict and
  exit codes are unchanged (advisory doctrine preserved), and that
  `PRXREF_FAIL_ON=any` is the opt-in gate for spec findings.

> **As built:** the feature shipped in **0.14.0**, not 0.12.0. The eval work
> that shipped is the dataset plus its structural scorer (§7), not a
> harness.

---

## 10. Open questions / explicit non-goals (v1)

- **No persistent graph index** (locked): specs are fetched, pruned, and
  discarded per run. A future `PRXREF_SPEC_INDEX` (cache extracted constraint
  sets keyed by URL hash with TTL) is the natural v2 — the extraction pass is
  already deterministic and pure.
- **No auto-discovery of specs from the PR body** (non-goal): if a PR
  description links a ticket, prxref does not fetch it. Operators are
  explicit.
- **No multi-ticket traversal** (non-goal): multiple `--spec` sources are
  allowed, but each is fetched independently; no epic→story expansion, no
  linked-issue walks.
- **No dedicated spec-sweep LLM call** (judgment call J6): v1 injects the
  digest into existing chunk + sweep prompts. If recorded-mode evals show the
  sweep prompt too loaded to catch spec classes, v2 adds a third single-shot
  unit (mirroring `_run_sweep`, `orchestrator.py:847-918`) — one more
  `chunk_count` unit, same failure shape. *As built (0.14.0): recorded-mode
  evals were not built (§7.1), so this trigger cannot fire yet.*
- **No MCP ticket fetch** in v1 (locked: REST basic-auth is primary; MCP noted
  as an alternative path for a future backend).
- **No per-source timeout/Retry config knobs** in v1: module constants
  (`SPEC_FETCH_TIMEOUT_S`, `SPEC_DIR_MAX_FILES`); promote to env vars only if
  real usage demands it. *As built (0.14.0): a third constant,
  `SPEC_FETCH_BUDGET_S = 30`, bounds one source's body in wall-clock
  seconds (§2.3).*
- **Jira comments not fetched** (v1 keeps summary+description): comment
  threads are noisy and frequently carry the debate the review is supposed to
  settle.

---

## 11. Judgment calls for the user to confirm

> **As built (0.14.0):** all eight calls shipped as proposed. The notes on J4
> and J6 record what changed around them.

- **J1 — `--spec` replaces `PRXREF_SPEC_SOURCES`; no merge.** Matches load_config
  override precedence (`config.py:104`). Alternative: flag values append to
  env values.
- **J2 — missing Jira credentials are a fetch failure, not exit 2.** The
  review proceeds un-grounded with a note naming the three env vars.
  Alternative: a Jira ticket URL with zero `PRXREF_JIRA_*` config could be a
  config error (exit 2) on the "required value missing" theory.
- **J3 — `spec` findings do not move the verdict.** Verdict stays
  error-only (`orchestrator.py:480-484`); `PRXREF_FAIL_ON=any` is the hard
  gate. Alternative: any active `spec` finding also yields
  `Request-Changes`.
- **J4 — ordering `error > warning > spec > outofscope`; unknown still falls
  back to outofscope/🟦 at the render layer and is dropped by the gate.**
  *As built (0.14.0): the fallback glyph is now ⬜, `markers.FALLBACK_MARKER`,
  because `outofscope` itself renders ⬜ (§5.1).*
- **J5 — severity-consistency can rewrite a `spec` finding to `warning`/
  `error` on a title collision** (current max-raise semantics with `spec` at
  rank 2). Alternative: make `spec` sticky (exempt from raises), at the cost
  of the pass's group-coherence guarantee.
- **J6 — no extra LLM call in v1**: the digest rides chunk prompts + the
  systemic sweep. Alternative: a dedicated third sweep unit for spec classes.
  *As built (0.14.0): held; the recorded-mode evals that would test it were
  not built (§7.1, §10).*
- **J7 — list coercion widened to comma-or-whitespace for ALL list keys**
  (touches `llm_models`' coercion too; behavior-identical for it). 
  Alternative: a `spec_sources`-only split rule.
- **J8 — digest budget default 3000 tokens (~12k chars)**, on top of a
  25k-token chunk budget (`triage.py:17`). If prompts grow too large on the
  smallest configured budgets, the digest could be charged a fixed share of
  `PRXREF_CHUNK_TOKEN_BUDGET` instead of being an independent knob.
