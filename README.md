# prxref

Fast automated AI code review for Bitbucket, GitLab, GitHub, and Azure DevOps — Cloud and self-hosted.

prxref reviews pull and merge requests on Bitbucket, GitHub, GitLab, and Azure DevOps in sub-minute review cycles. It parses unified diffs, partitions changes into risk-ranked chunks, gives each worker the dependency pins and out-of-hunk definitions its chunk references when the forge can serve file content, fans out parallel single-shot LLM reviews across a cheap-first model fallback chain, filters findings through deterministic quality gates, and publishes inline comments alongside an executive summary. Give it the spec or ticket a change implements with `--spec` (a web page, a local file or directory, or a Jira ticket URL) and the review also checks the diff against that spec.

```
                  ┌──────────────────────┐
                  │    Pull / MR URL     │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │     detect_forge     │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │    Forge Adapter     │
                  │ (BB / GitHub / GL)   │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │     Unified Diff     │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ Risk-Ranked Chunking │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ Parallel LLM Workers │
                  │  (Fallback Chain)    │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │     Quality Gate     │
                  │ Line Align / Dedup   │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ Post Inline Comments │
                  │     + Summary        │
                  └──────────────────────┘
```

## Deterministic Checks and Quality Passes

Not every finding comes from a model, and no finding posts unfiltered. prxref
computes one class of finding directly from the parsed diff — the
release-shaped-PR check — and then runs every finding, model-authored or not,
through eleven deterministic passes: location validation, `package.json` claim
checks, line alignment, thread dedup, settled-thread suppression, severity
consistency, the removal-claim check, the hedge gate, the quality gate, sweep
dedup, and the containment note. A filtered finding is never discarded
silently — it is kept with a `drop_reason` for the run log, and visible in a
`--no-post` dry run or under `--format json`.

The passes, the checks, every `drop_reason` string, and which of them have a
knob: [docs/quality.md](docs/quality.md).

## PR Size Advisory

<!-- 0.14 placeholder: W68 -->

## Quickstart

Run reviews instantly without local installation using `uvx`, or install the CLI globally:

```bash
# Run one-shot review via uvx
uvx prxref review --pr-url https://github.com/org/repo/pull/123

# Or install tool globally
uv tool install prxref
prxref review --pr-url https://github.com/org/repo/pull/123

# Or install straight from source (works before the first PyPI release)
uv tool install git+https://github.com/sblattj/prxref
```

### Review Any Forge

Pass any PR or MR URL directly. Forge type, repository namespace, and pull request ID are detected automatically:

```bash
# Bitbucket Cloud
prxref review --pr-url https://bitbucket.org/workspace/repo/pull-requests/42

# Bitbucket Server / Data Center (self-hosted, any host, with or without a deployment context path)
prxref review --pr-url https://bitbucket.corp.example/projects/PLAT/repos/api/pull-requests/42

# ...including a plain-HTTP deployment, e.g. the standalone install's default port
prxref review --pr-url http://bitbucket.internal:7990/projects/PLAT/repos/api/pull-requests/42

# GitHub & GitHub Enterprise
prxref review --pr-url https://github.com/owner/repository/pull/108

# GitLab & Self-Hosted GitLab (including nested subgroups)
prxref review --pr-url https://gitlab.com/group/subgroup/project/-/merge_requests/15
```

**Supported hosts.** Every forge is supported on any host. GitHub Enterprise Server and self-hosted GitLab share one adapter each with their SaaS products, which speak the same REST API at a different base URL. Bitbucket does not: Server / Data Center speaks `/rest/api/1.0` against different resource shapes, so it is a separate adapter selected automatically from the URL — `PRXREF_BITBUCKET_SERVER_TOKEN` for Data Center, `PRXREF_BITBUCKET_TOKEN` for Cloud. See [docs/forges.md](docs/forges.md).

## LLM Configuration

prxref operates without direct cloud provider SDK keys (no Anthropic API keys). It ships with **no default endpoint and no default model chain**: point it at any OpenAI-compatible `/chat/completions` server (OpenRouter, Together, Groq, vLLM, Ollama, a self-hosted gateway), or install the optional `litellm` extra. `PRXREF_LLM_BASE_URL` and `PRXREF_LLM_MODELS` are required — leaving either unset exits `2` with an error naming the variable.

```bash
# Default backend: plain HTTP to any OpenAI-compatible endpoint
export PRXREF_LLM_BACKEND=openai-compat        # aliases: ferry, http
export PRXREF_LLM_BASE_URL="https://openrouter.ai/api/v1"
export PRXREF_LLM_API_KEY="$OPENROUTER_API_KEY"
export PRXREF_LLM_MODELS="z-ai/glm-5.3-flash"
export PRXREF_LLM_REASONING_EFFORT=low
export PRXREF_LLM_MAX_TOKENS=4096              # raise this if you raise the effort

# Optional: in-process litellm extra
# pip install 'prxref[litellm]'
export PRXREF_LLM_BACKEND=litellm
export PRXREF_LLM_MODELS="openrouter/meta-llama/llama-3.3-70b-instruct,bedrock/anthropic.claude-3-7-sonnet-20250219-v1:0"
```

On a reasoning model the hidden reasoning trace draws from the **same** completion budget as the answer, so turning `PRXREF_LLM_REASONING_EFFORT` up makes truncation *more* likely. A truncated chunk is counted as failed and the posted summary names the reason and the variable to raise; see [Reasoning models and the token budget](docs/env-vars.md#reasoning-models-and-the-token-budget).

Temperature `0.0` and a sampling `seed` are sent on every call — `PRXREF_LLM_SEED` when set, else one random seed per process shared by the whole run (issue #56) — but neither makes a review bit-reproducible — provider fingerprints, load-balanced backends, and gateways that ignore `seed` all still vary the model's output. Everything downstream of the model is deterministic: findings are ordered by `(file, line, title)` and the caps break ties by content, and the run record's `sampling` field reports which knobs were in force. See [Determinism](docs/llm.md#determinism-what-is-pinned-and-what-still-varies).

See [docs/llm.md](docs/llm.md) for architecture, failover behavior, and backend setup, and [docs/env-vars.md](docs/env-vars.md#tuning-for-your-team) for tuning the confidence floor and finding caps to your team.

## Forge Authentication

Configure the authentication token matching your forge:

| Forge | Environment Variable | Notes |
|---|---|---|
| **Bitbucket Cloud** | `PRXREF_BITBUCKET_TOKEN` | Bearer token (workspace or repo access token) |
| **Bitbucket Cloud (Basic)** | `PRXREF_BITBUCKET_USER` + `PRXREF_BITBUCKET_APP_PASSWORD` | App password fallback |
| **Bitbucket Server / DC** | `PRXREF_BITBUCKET_SERVER_TOKEN` | HTTP access token (falls back to `PRXREF_BITBUCKET_TOKEN`) |
| **Bitbucket Server (Basic)** | `PRXREF_BITBUCKET_SERVER_USER` + `PRXREF_BITBUCKET_SERVER_PASSWORD` | Basic-auth fallback |
| **GitHub** | `PRXREF_GITHUB_TOKEN` | Personal Access Token (PAT) or GitHub App token |
| **GitHub Enterprise** | `PRXREF_GITHUB_ENTERPRISE_TOKEN` | Used when host is not `github.com` (falls back to `PRXREF_GITHUB_TOKEN`) |
| **GitLab** | `PRXREF_GITLAB_TOKEN` | Personal, project, or group access token (`PRIVATE-TOKEN`) |

See [docs/env-vars.md](docs/env-vars.md) for the full configuration reference, [docs/forges.md](docs/forges.md) for forge specifics, [docs/quality.md](docs/quality.md) for the deterministic checks and every drop reason, and [docs/systemic-sweep.md](docs/systemic-sweep.md) for the whole-PR sweep's digest classes.

## Webhook Server

Run prxref as a persistent daemon to handle webhook events from GitHub, Bitbucket, and GitLab:

```bash
prxref serve --port 8080 --host 0.0.0.0
```

The service exposes:
- `POST /webhook` — verifies HMAC or token signatures per forge, enqueues incoming PR events, and responds immediately with `202 Accepted`. A background worker processes reviews serially.
- `GET /health` — liveness probe returning `{"ok": true}`.

## Review Against a Spec or Ticket

<!-- 0.14 placeholder: W-SPECDOCS -->

## Ticket Context and Scope

Give prxref the ticket a PR is meant to implement, and every finding is marked `in`, `out`, or `unknown` against that ticket's scope:

```bash
prxref review --pr-url https://github.com/owner/repository/pull/108 --context-file ticket.md

# or for every run
export PRXREF_TICKET_CONTEXT_FILE=ticket.md
```

The file is plain text or Markdown: the ticket's title, description, and acceptance criteria, fetched from your tracker by a CI step. It must be a local file, and `prxref review` reads it before any network call. A URL, a missing file, a directory or other non-regular file, an unreadable file, or a file that is not UTF-8 is a configuration error: the run exits `2`, and the message names `--context-file` or `PRXREF_TICKET_CONTEXT_FILE`, whichever supplied the path. A path under the working directory that symlinks out of it is refused the same way. `--context-file PATH` wins over the variable for one run, and `--context-file ""` turns it off. Read the file from a trusted checkout or your CI, never from the PR under review, or the PR's author writes the ticket their change is judged against.

A configured ticket is in one of three states:

| File | Prompts | Summary note |
|---|---|---|
| Empty or whitespace only: "this PR has no ticket" | unchanged | `No ticket context for this PR — findings were not checked against a ticket's scope.` |
| Text without acceptance criteria | ticket and scope ask added | `The ticket context has no acceptance criteria — scope was judged from its description alone.` |
| Text with acceptance criteria | ticket and scope ask added | none |

Acceptance criteria are recognized by any one of these: a heading or label standing alone on its line (`Acceptance criteria`, `Acceptance test(s)`, or `Definition of done` in any case, or `AC` in capitals, optionally as a `#` heading, in bold, or with a trailing colon), a Markdown task-list item (`- [ ] …` or `- [x] …`), or a Gherkin `Given` line followed later by a `Then` line.

**What each finding's `scope` means.** `in`: the finding concerns what the ticket asks for, including code that visibly contradicts one of its acceptance criteria. `out`: it concerns a change the ticket does not ask for, such as an unrelated refactor or a drive-by edit. `unknown`: the ticket and the diff do not let the model tell. Without a ticket, or with an empty one, every finding is `unknown`, and so is any answer from the model other than exactly one of those three words. Scope is advisory only: it never changes a finding's severity or confidence, the verdict, the error cap, or `PRXREF_FAIL_ON`. The `outofscope` severity is unrelated and only means minor. How a scope shows on a posted comment is covered in [Finding Markers](#finding-markers).

**How the ticket reaches the model.** The ticket text goes into the user prompt of every chunk worker and of the whole-PR sweep, under a `### Ticket context` heading. It sits inside a code fence it cannot close, with a line telling the model that it is data, not instructions. The request to add a `scope` to every finding is prxref's own policy, so it goes into the system prompt instead. `PRXREF_TICKET_CONTEXT_MAX_CHARS` (default `6000`) caps the text. A longer ticket is cut, and a line after the fence says how many of its characters are shown. Criteria past the cap are not in view, so they do not count toward the state above.

**What is recorded.** The `ticket_context` key of `--format json` and the run's `ticket ok` trace event hold the path, the SHA-256 of the file's raw bytes, its length in characters, the cap, whether it was truncated, whether it has acceptance criteria, and whether it was empty. The `-v` line shows the path, the start of the SHA-256, the length, and the active findings' `in`/`out`/`unknown` counts. None of these ever holds the ticket text. The text does appear in the prompt files that `--trace-dir` writes (`chunk0.user.md`, `sweep.user.md`), so treat that directory like the ticket itself.

**The webhook daemon ignores the file.** One file cannot describe every PR a daemon sees, so `prxref serve` never reads `PRXREF_TICKET_CONTEXT_FILE` and logs a warning once at startup when it is set.

## Team Review Rules

<!-- 0.14 placeholder: W63 -->

## Finding Markers

<!-- 0.14 placeholder: W64B -->

## CLI Flags

`prxref review` takes:

- `--pr-url URL` — full web URL of the PR or MR on Bitbucket, GitHub, GitLab, or Azure DevOps. Required unless `--diff-file` is given.
- `--no-post` — dry run; run review analysis and quality passes without writing comments to the forge. In text mode this also prints every active finding's location, title, and body, and every dropped finding with its drop reason.
- `--max-chunks N` — override maximum diff chunks evaluated (default `8`).
- `--timeout SECONDS` — override the per-model request deadline (default `45.0`, or `PRXREF_LLM_TIMEOUT` when set); the flag wins for the current invocation only.
- `--spec URL_OR_PATH` — a spec or ticket to review the PR against: a public web URL, a local file or directory, or a Jira ticket URL. Repeatable. When given, the flags replace `PRXREF_SPEC_SOURCES` entirely rather than adding to it. See [Review Against a Spec or Ticket](#review-against-a-spec-or-ticket).
- `--rules-file PATH` — your team's review rules (Markdown or text, with optional front matter carrying a `severity:` map), added to every review prompt. Overrides `PRXREF_REVIEW_RULES` for this run, and `--rules-file ""` turns an environment-configured file off. Read it from a trusted checkout, never from the PR under review. See [Team Review Rules](#team-review-rules).
- `--context-file PATH` — the ticket the PR is meant to implement (plain text or Markdown). Every finding is then marked in, out of, or of unknown ticket scope, and an empty file means "this PR has no ticket". Overrides `PRXREF_TICKET_CONTEXT_FILE` for this run, and `--context-file ""` turns it off. See [Ticket Context and Scope](#ticket-context-and-scope).
- `--trace-dir DIR` — write each review unit's exact prompt halves, raw model response, and metadata to `DIR` (`chunk0.system.md`, `chunk0.user.md`, `chunk0.response.json`, `chunk0.meta.json`, and so on for each chunk and for the whole-PR `sweep`). `PRXREF_TRACE_DIR` does the same for every run; the flag wins when both are set.
- `-v, --verbose` — output run timing, token counts, cost, and finding breakdowns to stdout, plus one line each for the rules file, the ticket context (with the active findings' scope counts), and the spec sources when they are configured. In text mode this also prints finding bodies and dropped findings, same as `--no-post`.
- `--format {text,json}` — output format for `review` (default `text`). `json` prints exactly one JSON object to stdout, with these keys in this order:
  - `verdict`;
  - `findings`: active first, then dropped, each with `file`, `line`, `severity`, `confidence`, `scope` (`in`, `out`, or `unknown` against the ticket context; always `unknown` without one), `title`, `body`, `drop_reason`;
  - `chunk_count`, `chunks_reviewed`, `chunks_failed`, `elapsed_ms`, `input_tokens`, `output_tokens`;
  - `cost_usd`: the run's cost in USD, `null` when no source could price it (never `0` for an unknown cost), and `cost_estimated`: `true` when any part of it came from `PRXREF_PRICE_TABLE`. See [Cost accounting](docs/llm.md#cost-accounting);
  - `posted`;
  - `review_rules` (`path`, `sha256`, `chars`, `max_chars`, `truncated`, `severity_map`), `ticket_context` (`path`, `sha256`, `chars`, `max_chars`, `truncated`, `has_acceptance_criteria`, `empty`; never the ticket text), `spec_grounding` (`sources`, `ok`, `failed`, `constraints`, `digest_sha256`), and `size_advisory` (`changed_lines`, `changed_files`, `lines_limit`, `files_limit`, `triggered`, `message`). These four are always present and `null` when their feature is off;
  - `sampling`: the `temperature`, `seed`, and `models` the run had in force (every review result carries it);
  - `replay`: the replay stamp (`base_sha`, `head_sha`, `threads`, `diff_file`), on replay runs only.

Replay flags, for evaluation (see [Replay Mode (Evaluation)](#replay-mode-evaluation)). Any of them turns posting off for the run:

- `--base-sha SHA` / `--head-sha SHA` — review the pinned range `BASE...HEAD` of the `--pr-url` repository (the merge-base diff, as the PR's own diff is), with file context read at `HEAD`. The two come as a pair, must be full 40- or 64-character hex commit SHAs, must differ, and need `--pr-url`.
- `--no-threads` — hide the PR's existing threads from the prompt and from the thread-dedup passes.
- `--diff-file PATH` — review this unified diff (`git diff` or `git format-patch` output) instead of fetching one; `--pr-url` becomes optional.

The other subcommands: `prxref serve [--port N] [--host H]` runs the [webhook server](#webhook-server) (default port `8080`, default host `0.0.0.0`); `prxref trace render FILE [-o OUT]` renders a JSONL run trace (`PRXREF_TRACE_FILE`) to a standalone HTML pipeline view, written next to the trace unless `-o`/`--out` names the output; and `prxref --version` prints the version.

## Replay Mode (Evaluation)

<!-- 0.14 placeholder: W65C -->

## Exit Codes

`prxref` is an advisor, never a gate. Its exit code says whether *prxref* was configured correctly, not whether your code is good.

| Code | Meaning |
|---|---|
| `0` | The run finished — **including every review error**: an empty diff, a network failure, an LLM timeout, bad forge credentials, an unrecognized URL, or a review in which every chunk failed. Diagnostics go to stderr; the pipeline step stays green. With `PRXREF_FAIL_ON` set (see below) a finding or a failed review can turn this into `1`. |
| `1` | **Gated review outcome** — only when `PRXREF_FAIL_ON` is set: `error` exits `1` when the completed review carries an active error-severity finding, `any` exits `1` on any active finding, and under either value a review that fails to complete also exits `1`. The reason is printed to stderr. |
| `2` | **Usage or configuration error** — no subcommand, invalid command-line arguments, or a required value missing, malformed, outside its valid range, or outside its key's allowed vocabulary (`PRXREF_FAIL_ON` accepts only `never`, `error`, `any`). The message names the source that supplied it: the environment variable, or the CLI flag when a flag is what you typed. |

```
$ prxref review --pr-url https://github.com/org/repo/pull/1 --max-chunks 0
configuration error: --max-chunks: must be a finite number greater than 0, got 0
```

`PRXREF_FAIL_ON` is the one opt-out of the advisory contract, and its default `never` is the doctrine above, unchanged. Setting it to `error` or `any` turns the reviewer into a merge gate — failing a build on a finding turns a probabilistic reviewer into a gate, and the first false positive teaches a team to bypass the gate, so think hard before you set it. Read the verdict from the posted summary, which also carries a partial-review banner when some chunks did not make it. Do not build a security control on the exit code. The webhook daemon has no exit code and is unaffected.
