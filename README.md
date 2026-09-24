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
                  │ (GitHub / GitLab /   │
                  │  Bitbucket Cloud /   │
                  │  BB Server / ADO)    │
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
through the team severity map (only when the review rules declare one) and
spec grounding, two passes that relabel a severity and drop nothing, and then
through eleven more deterministic passes: location validation, `package.json` claim
checks, line alignment, thread dedup, settled-thread suppression, severity
consistency, the removal-claim check, the hedge gate, the quality gate, sweep
dedup, and the containment note. A filtered finding is never discarded
silently — it is kept with a `drop_reason` for the run log, and visible in a
`--no-post` dry run or under `--format json`.

The passes, the checks, every `drop_reason` string, and which of them have a
knob: [docs/quality.md](docs/quality.md).

## PR Size Advisory

A team that keeps PRs small can set `PRXREF_SIZE_WARN_LINES` (lines added plus
removed) and/or `PRXREF_SIZE_WARN_FILES` (files changed). A PR above either
threshold gets one line at the top of its summary, such as `This PR changes 812
lines in 24 files, above the team guideline of 500 lines and 20 files. Consider
splitting it.` The line names only the limits that were exceeded. Both
thresholds are unset by default, which turns the advisory off; `0` is a real
threshold that flags any change at all. The counts come from the parsed diff and
skip the common ecosystems' lockfiles (`package-lock.json`, `uv.lock`,
`Cargo.lock`, `go.sum`, …), generated files (`*.snap`, `__snapshots__/`, `*.min.js`, `*.map`,
`*.generated.*`, `*.auto.*`), and any path matching `PRXREF_SIZE_IGNORE_GLOBS`,
which adds to those built-ins and never replaces them. A binary file counts as one
file and zero lines, so the line count is a lower bound when a forge omits a
file's hunks. The advisory is not a finding: it never changes the verdict or the
exit code, and with `--no-post` or `PRXREF_POST_MODE=inline` it appears only in
the run record, under `--format json` as `size_advisory`, and as a
`size advisory:` line in the CLI output. See
[docs/env-vars.md](docs/env-vars.md) for the glob syntax.

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

To work on prxref itself (development setup, tests, and lint), see [CONTRIBUTING.md](CONTRIBUTING.md).

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

# Azure DevOps Services (dev.azure.com or the legacy *.visualstudio.com host)
prxref review --pr-url https://dev.azure.com/organization/project/_git/repository/pullrequest/42
prxref review --pr-url https://organization.visualstudio.com/project/_git/repository/pullrequest/42

# Azure DevOps Server (on-prem; the URL names the collection and the project)
prxref review --pr-url https://ado.corp.example/tfs/DefaultCollection/project/_git/repository/pullrequest/42
```

**Supported hosts.** Every forge is supported on any host. GitHub Enterprise Server and self-hosted GitLab share one adapter each with their SaaS products, which speak the same REST API at a different base URL. Bitbucket does not: Server / Data Center speaks `/rest/api/1.0` against different resource shapes, so it is a separate adapter selected automatically from the URL — `PRXREF_BITBUCKET_SERVER_TOKEN` for Data Center, `PRXREF_BITBUCKET_TOKEN` for Cloud. Azure DevOps Services and Server share one adapter. It has no diff endpoint to call, so it rebuilds the PR's diff from the changed files; a public project can be reviewed with no token at all. Posting to Azure DevOps is not yet verified against a live server, and Azure DevOps Server is untested. See [docs/forges.md](docs/forges.md).

## LLM Configuration

prxref operates without direct cloud provider SDK keys (no Anthropic API keys). It ships with **no default endpoint and no default model chain**: point it at any OpenAI-compatible `/chat/completions` server (OpenRouter, Together, Groq, vLLM, Ollama, a self-hosted gateway), install the optional `litellm` extra, or run the `claude` or `kiro-cli` CLI you are already logged in to. `PRXREF_LLM_MODELS` is required on every backend and `PRXREF_LLM_BASE_URL` on `openai-compat`; leaving a required one unset exits `2` with an error naming the variable.

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

# Optional: your own logged-in Claude Code CLI, on your own machine
export PRXREF_LLM_BACKEND=claude-cli
export PRXREF_LLM_MODELS="sonnet"
export PRXREF_LLM_TIMEOUT=120                    # each call includes CLI start-up
```

`claude-cli` and `kiro-cli` run the CLI already installed and logged in on your machine, on your subscription and for your own use only. Do not use them for a team, a shared webhook, or CI; use an API key through `openai-compat` or `litellm` there. See [Subscription CLI backends](docs/llm.md#subscription-cli-backends-claude-cli-and-kiro-cli).

On a reasoning model the hidden reasoning trace draws from the **same** completion budget as the answer, so turning `PRXREF_LLM_REASONING_EFFORT` up makes truncation *more* likely. A truncated chunk is counted as failed and the posted summary names the reason and the variable to raise; see [Reasoning models and the token budget](docs/env-vars.md#reasoning-models-and-the-token-budget).

On `openai-compat` and `litellm`, temperature `0.0` and a sampling `seed` are sent on every call — `PRXREF_LLM_SEED` when set, else one random seed per process shared by the whole run (issue #56) — but neither makes a review bit-reproducible — provider fingerprints, load-balanced backends, and gateways that ignore `seed` all still vary the model's output. The CLI backends send neither, and the run record's `sampling` field shows both as `null`. Everything downstream of the model is deterministic: findings are ordered by `(file, line, title)` and the caps break ties by content, and the run record's `sampling` field reports which knobs were in force. See [Determinism](docs/llm.md#determinism-what-is-pinned-and-what-still-varies).

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
| **Azure DevOps** | `PRXREF_AZURE_DEVOPS_TOKEN` | Personal access token: Code (Read) to review, Code (Read & write) to post |
| **Azure DevOps (Pipelines)** | `SYSTEM_ACCESSTOKEN` | The job token, used when no PAT is set; map it into the step with `env: SYSTEM_ACCESSTOKEN: $(System.AccessToken)`. With neither set, public projects are read anonymously |

See [docs/env-vars.md](docs/env-vars.md) for the full configuration reference, [docs/forges.md](docs/forges.md) for forge specifics, [docs/quality.md](docs/quality.md) for the deterministic checks and every drop reason, and [docs/systemic-sweep.md](docs/systemic-sweep.md) for the whole-PR sweep's digest classes.

## Webhook Server

Run prxref as a persistent daemon to handle webhook events from GitHub, Bitbucket, GitLab, and Azure DevOps:

```bash
prxref serve --port 8080 --host 0.0.0.0
```

The service exposes:
- `POST /webhook` — verifies HMAC or token signatures per forge (for Azure DevOps service hooks, the Basic-auth password against `PRXREF_AZURE_DEVOPS_WEBHOOK_SECRET`), enqueues incoming PR events, and responds immediately with `202 Accepted`. A background worker processes reviews serially. Registering each forge's webhook: [docs/deploy.md](docs/deploy.md#2-webhook-registration).
- `GET /health` — liveness probe returning `{"ok": true}`.

## Review Against a Spec or Ticket

Give prxref the spec or ticket a change implements, and the review also checks the diff against it. A source is a public web page, a local file, a local directory (up to 20 `.md`, `.markdown`, `.txt` or `.adoc` files directly inside it), or a Jira ticket URL:

```bash
prxref review --pr-url https://github.com/org/repo/pull/123 \
  --spec https://jira.example.com/browse/PROJ-42 \
  --spec https://spec.example.com/client-guidelines.html \
  --spec /etc/prxref/specs/
```

prxref fetches each source and keeps its RFC 2119 statements (MUST, SHOULD, MAY), version pins and naming rules; a Jira ticket's summary and description are kept line by line, up to 6,000 characters, and ranked first. It ranks the rest against the diff and adds that bounded digest (`PRXREF_SPEC_DIGEST_TOKENS`) to every chunk worker's prompt and to the whole-PR sweep's, with no extra model call. A finding whose only basis is one of those constraints is a 🔍 `spec` finding, and it quotes the constraint as `Spec: "…"`. `PRXREF_SPEC_SOURCES` sets the sources for every run, the webhook daemon included; `--spec` replaces that list for one run.

- **Jira.** A public ticket needs no configuration. For a private one set `PRXREF_JIRA_BASE_URL`, `PRXREF_JIRA_EMAIL` and `PRXREF_JIRA_API_TOKEN`: credentials only go to `PRXREF_JIRA_BASE_URL`, and every other fetch is anonymous.
- **Advisory.** 🔍 spec findings are advisory: they never change the verdict, and `PRXREF_FAIL_ON=error` ignores them. `PRXREF_FAIL_ON=any` is the opt-in gate.
- **Best-effort.** A source that cannot be fetched never fails the review. The summary gains a grounding note that counts the constraints injected and names each failed source by its position and kind (`source 2 (url)`), never by its path or URL. When the digest ends up with no constraint at all, the review runs as if no spec had been given, and a `spec` finding the model emits anyway is relabelled `warning`.

Every setting: [docs/env-vars.md](docs/env-vars.md). How grounding meets the quality passes: [docs/quality.md](docs/quality.md#spec-grounding). Spec sources in CI and on the daemon, fetch time bounds, and what the logs record: [docs/deploy.md](docs/deploy.md#7-spec-sources-in-ci-and-on-the-daemon).

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

**How the ticket reaches the model.** The ticket text goes into the user prompt of every chunk worker and of the whole-PR sweep, under a `### Ticket context` heading. It sits inside a code fence it cannot close, with a line telling the model that it is data, not instructions. The request to add a `scope` to every finding is prxref's own policy, so it goes into the system prompt instead. While that request is in the prompt, the example finding under `## Output Format` in the worker and sweep prompts also carries `"scope": "in"`, because a model that copies the example rather than following the instruction would otherwise never label scope; without a ticket, or with an empty one, the prompts are unchanged. `PRXREF_TICKET_CONTEXT_MAX_CHARS` (default `6000`) caps the text. A longer ticket is cut, and a line after the fence says how many of its characters are shown. Criteria past the cap are not in view, so they do not count toward the state above.

**What is recorded.** The `ticket_context` key of `--format json` and the run's `ticket ok` trace event hold the path, the SHA-256 of the file's raw bytes, its length in characters, the cap, whether it was truncated, whether it has acceptance criteria, and whether it was empty. The `-v` line shows the path, the start of the SHA-256, the length, and the active findings' `in`/`out`/`unknown` counts. None of these ever holds the ticket text. The text does appear in the prompt files that `--trace-dir` writes (`chunk0.user.md`, `sweep.user.md`), so treat that directory like the ticket itself.

**The webhook daemon ignores the file.** One file cannot describe every PR a daemon sees, so `prxref serve` never reads `PRXREF_TICKET_CONTEXT_FILE` and logs a warning once at startup when it is set.

## Team Review Rules

Give prxref your team's review checklist and every chunk worker and the whole-PR sweep review against it:

```bash
prxref review --pr-url https://github.com/acme/widget/pull/42 --rules-file "$RUNNER_TEMP/prxref-rules.md"
```

- `--rules-file PATH`, or `PRXREF_REVIEW_RULES` for every run, names a Markdown or plain-text file. Its body is added to the **system** prompt of every review unit under a `## Team review rules` heading. The chunk workers check their chunk against it, and the sweep applies only the whole-PR and cross-file rules. Unset, nothing changes.
- Optional front matter can map your team's severity words onto prxref's tiers in a `severity:` block (`blocker: error`, `major: warning`, `nit: outofscope`). A mapped word the model writes anyway is rewritten before every quality pass, so it is never dropped as an invalid severity. Other front-matter keys are ignored, so a skill file works unmodified.
- `PRXREF_REVIEW_RULES_MAX_CHARS` (default `12000`) caps the body, with a warning when it truncates. The run record's `review_rules` carries the file's `sha256`, its character count, and the parsed map, never the rules text. It appears in `--format json`, the `-v` output, and the JSONL trace.
- A missing, unreadable, or malformed file exits `2` before any network call, naming `--rules-file` or `PRXREF_REVIEW_RULES`.

**Read the rules from a trusted checkout, never from the PR under review.** In CI the workspace is usually the PR's own code, so a rules file inside it lets the PR rewrite its own review rules. Copy the file from the target branch or keep it outside the repository. See [docs/review-rules.md](docs/review-rules.md) for the grammar, the CI recipes, and the daemon.

## Finding Markers

Each severity has one glyph. It is the same in the summary's counts line, the summary's findings list, and the header of every inline comment:

| Marker | Severity | Meaning |
|---|---|---|
| 🟥 | `error` | The change breaks at runtime or is a real bug. |
| 🟧 | `warning` | A risk or smell the diff introduces or worsens. |
| 🔍 | `spec` | The diff contradicts a constraint quoted from a spec source. See [Review Against a Spec or Ticket](#review-against-a-spec-or-ticket). |
| ⬜ | `outofscope` | Minor: misleading naming, a TODO without context, dead code the diff adds. An unrecognised severity also renders ⬜. |

🟦 is not a severity. It marks a finding that the [ticket context](#ticket-context-and-scope) puts outside the ticket (`scope` is `out`), and it goes in front of the severity glyph, never in place of it:

- **Summary:** those findings are listed after the others, under their own heading, for example `**🟦 Outside the ticket (2)**` followed by ``- 🟦 🟧 `src/app.py:12` — …``. If every finding is outside the ticket, the first list reads `No in-ticket findings.`.
- **Inline comments:** the header reads, for example, `🤖 🟦 🟧 **[WARNING · OUTSIDE TICKET] …**`.
- **CLI text output** (`--no-post` or `-v`): the finding line ends in ` [scope: out]`, or ` [scope: in]` for a finding inside the ticket.

Findings inside the ticket (`in`) and findings the reviewer could not place (`unknown`) carry no scope marker, so a run without a ticket context renders exactly the severity glyphs. Scope never changes a finding's severity. It is not counted separately either: the counts line counts every active finding by severity, and the verdict, the error cap, and `PRXREF_FAIL_ON` ignore scope.

Before 0.14.0, `outofscope` findings rendered 🟦. They now render ⬜ on every run, and 🟦 means only "outside the ticket".

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
  - `replay`: the replay stamp (`base_sha`, `head_sha`, `threads`, `diff_file`, `description`, `as_of`, `as_of_source`), on replay runs only.

Replay flags, for evaluation (see [Replay Mode (Evaluation)](#replay-mode-evaluation)). Any of them turns posting off for the run:

- `--base-sha SHA` / `--head-sha SHA` — review the pinned range `BASE...HEAD` of the `--pr-url` repository (the merge-base diff, as the PR's own diff is), with file context read at `HEAD`. The two come as a pair, must be full 40- or 64-character hex commit SHAs, must differ, and need `--pr-url`.
- `--no-threads` — hide the PR's existing threads from the prompt and from the thread-dedup passes.
- `--diff-file PATH` — review this unified diff (`git diff` or `git format-patch` output) instead of fetching one; `--pr-url` becomes optional.
- `--as-of TIME` — show the reviewer the PR's title and description as they were at `TIME`: an ISO-8601 time with a UTC offset, such as `2026-05-01T09:30:00Z` or `2026-05-01T11:30:00+02:00`. A date alone or a time without an offset exits `2` rather than being read in the local time zone, and so does a value that is not ISO-8601. Needs `--pr-url`, and a forge that can read description history (GitHub or Bitbucket Cloud); on any other forge it exits `2`. Without it, a `--pr-url` replay pins the title and description to the PR's first human review, else to its head commit's date.
- `--description-file PATH` — use this file's text as the PR description, with or without `--pr-url`. It is read like `--rules-file`: a missing, unreadable or non-regular file, a file that is not UTF-8 or contains NUL bytes, and a path under the working directory that symlinks out of it each exit `2`. A blank file is an empty description.
- `--no-description` — review with an empty PR description.

`--as-of`, `--description-file` and `--no-description` are mutually exclusive: giving two or more exits `2` naming each one given. They are CLI-only, with no environment variable.

The other subcommands: `prxref serve [--port N] [--host H]` runs the [webhook server](#webhook-server) (default port `8080`, default host `0.0.0.0`); `prxref trace render FILE [-o OUT]` renders a JSONL run trace (`PRXREF_TRACE_FILE`) to a standalone HTML pipeline view, written next to the trace unless `-o`/`--out` names the output; and `prxref --version` prints the version.

`prxref prompts export DIR [--force]` writes the packaged `worker.md`, `systemic.md` and `summary.md` prompt templates into `DIR`, byte for byte, as the starting point for a `PRXREF_PROMPTS_DIR` override directory, and prints each path it wrote. It creates `DIR` when it is missing. When any of the three files already exists it overwrites nothing, writes nothing, and exits `2` naming the file; `--force` overwrites them. The judge prompt of `prxref eval` is never exported, because it cannot be overridden.

`prxref eval` scores [replays](#replay-mode-evaluation) against labelled human findings. It never posts, and it adds no environment variable. Its three actions:

- `prxref eval run --cases PATH --label NAME [--out DIR] [--rules-file PATH] [--resume]` replays every case and writes the run to `DIR/NAME/`:
  - `--cases PATH` — the labelled cases: a `cases.json` file, or a directory of `case-*/` directories. Required. A bad case exits `2`, naming `--cases`, the case id, and the field.
  - `--label NAME` — the run's name and its directory under `--out`. Required. An existing label exits `2` unless `--resume` is given.
  - `--out DIR` — the directory that holds the runs (default `./prxref-eval/`). `score` and `compare` take it too.
  - `--rules-file PATH` — team review rules for every case, as for `review`.
  - `--resume` — continue an existing `--label` run instead of refusing it.
- `prxref eval score --label NAME [--judge-model MODEL] [--out DIR]` grades the run against its labels and writes `score.json` and `score.md`:
  - `--judge-model MODEL` — the model that grades every label without a `must_match` predicate, on the review's own LLM backend. Required when any label lacks one; leaving it out then exits `2`. A judge model the review itself used logs a warning.
- `prxref eval compare A B [--out DIR]` prints two scored runs side by side, then every label whose credit changed. `A` and `B` are each a label under `--out` or a run directory.

## Replay Mode (Evaluation)

A replay reviews a pinned, reproducible input instead of a PR as it stands, so one change can be reviewed again later, by another model or another prxref build, and compared. Three invocations cover it:

```bash
# A blind replay of a PR at two pinned commits, without its existing discussion
prxref review --pr-url https://github.com/acme/widgets/pull/42 \
  --base-sha 0123456789abcdef0123456789abcdef01234567 \
  --head-sha 89abcdef0123456789abcdef0123456789abcdef \
  --no-threads --format json

# A diff on disk with its ticket and spec corpus; no PR and no forge at all
prxref review --diff-file change.diff --context-file TICKET.md --spec docs/specs --format json

# One eval case (see tests/evals/README.md)
prxref review --diff-file tests/evals/<case>/diff.patch --context-file tests/evals/<case>/ticket.md \
  --spec tests/evals/<case>/docs --no-post --format json
```

- **A replay never posts.** Any replay flag turns posting off for the run, with or without `--no-post`; when nothing else had already turned it off, the run logs `replay run: posting to the forge is disabled`. The replay forges also refuse every write, and a replay never prunes older comments.
- **Pinned SHAs:** `--base-sha` and `--head-sha` come as a pair, must be full 40- or 64-character hex commit SHAs (resolve a short one with `git rev-parse`), are lowercased, must name two different commits, and need `--pr-url`. The review reads the merge-base diff `BASE...HEAD` and file context at `HEAD`; the endpoint each forge uses is under "Pinned Commit Range (Replay)" in [docs/forges.md](docs/forges.md).
- **`--diff-file PATH`** reviews that file (`git diff` or `git format-patch` output) instead of fetching a diff. Without `--pr-url` nothing is contacted: there are no threads and no file context, and a `git format-patch` file supplies the title, description and author. With `--pr-url` the file replaces the PR's diff, and without `--head-sha` a warning says that file context is still read at the PR's current head.
- **The title and description are pinned too.** A `--pr-url` replay shows the PR's title and description as they were at a cutoff, not as they are now, because a description edited after review (a table of the review fixes, say) tells the model what the reviewers found. The cutoff is the first of these that exists:
  1. `--as-of TIME`, when you give it;
  2. the PR's first human review or comment: not by the PR's author, not by a bot account, and not one of prxref's own posts;
  3. the date of the head commit (`--head-sha` when given, else the PR's current head).

  The history is read once, before the review starts, by one call to the forge's history reader; that network read is not recorded in the run trace. GitHub and Bitbucket Cloud can read it, and GitHub needs a token to (see "Description History (Replay)" in [docs/forges.md](docs/forges.md)). The title is pinned exactly when the description is. A cutoff before the PR was opened shows the original description.
- **Falling back to the current text.** When the history cannot pin them, the replay keeps the PR's *current* title and description, stamps `"description": "live"`, and logs a WARNING that starts `replay shows the PR's CURRENT title and description` and gives the reason: the forge cannot read description history (Bitbucket Server, GitLab and Azure DevOps cannot), the read failed (no GitHub token, a 401 or 403, a transport error), the history has neither a first review nor a head commit date, or it does not reach the cutoff (it is incomplete, or the version then in force was deleted). None of these stops the review. An explicit `--as-of` on a forge that cannot read history is the exception: it exits `2` naming `--as-of`, because the time you asked for cannot be honoured.
- **`--description-file PATH` and `--no-description`** replace the description with that file's text or with nothing, read no history, and leave the title as it is now. Both also work without `--pr-url`, replacing the description a `git format-patch` file supplies. At most one of `--as-of`, `--description-file` and `--no-description` may be given.
- **What a pinned replay does not pin.** The PR's current threads still reach the prompt unless you add `--no-threads`; a replay at pinned SHAs without `--no-threads` logs a warning saying so. With `--pr-url`, `--diff-file` and no `--head-sha`, file context is read at the PR's current head, with a warning. The title stays the current one under `--description-file` and `--no-description`, and after a fall back to the current text.
- **The record.** A replay's JSON record gains a `replay` stamp, always with all seven keys, and the text summary prints it as a `replay:` line. A normal run's record has no `replay` key.

  ```json
  "replay": {"base_sha": null, "head_sha": null, "threads": "hidden", "diff_file": "change.diff", "description": "file", "as_of": null, "as_of_source": null}
  ```

  `threads` is `"hidden"` under `--no-threads` or with no `--pr-url`, else `"shown"`; `diff_file` is the path as you typed it. `description` is `"pinned"` (the title and description in force at the cutoff), `"live"` (the current ones), `"file"` (`--description-file`, or `--diff-file` without `--pr-url`, where any description comes from the file) or `"none"` (`--no-description`). `as_of` is the cutoff as a UTC time ending in `Z`, with a fraction of a second only when the source time had one, and `as_of_source` is `"flag"`, `"first-review"` or `"head-commit"`. Both are `null` when no cutoff was chosen, and both are set on a `"live"` stamp whose history did not reach its cutoff. Given back as `--as-of`, `as_of` names the same instant. The text line then ends `description=pinned as_of=2026-05-01T09:30:00Z (first-review)`.
- **Exit codes.** A bad set of replay flags exits `2` naming the flag, and it is checked before the PR URL is parsed. A review error inside a replay — an empty pinned range (a head already merged into the base) or a blank diff file — ends the run as an `Error` run: it exits `0` under the default `PRXREF_FAIL_ON=never`, and `1` under `error` or `any`, like any review that does not complete. See [Exit Codes](#exit-codes).
- The replay flags have no environment variable, on purpose, and the [webhook server](#webhook-server) never replays.

## Exit Codes

`prxref` is an advisor, never a gate. Its exit code says whether *prxref* was configured correctly, not whether your code is good.

| Code | Meaning |
|---|---|
| `0` | The run finished — **including every review error**: a network failure, an LLM timeout, bad forge credentials, an unrecognized URL, or a review in which every chunk failed. Diagnostics go to stderr; the pipeline step stays green. With `PRXREF_FAIL_ON` set to `error` or `any` (see below), only two outcomes turn this into `1`: a completed review whose active findings trip the policy, and a review that does not complete — it crashes, or it ends with verdict `Error` (the forge could not be read, the diff could not be parsed or chunked, or every chunk review failed). An empty PR diff is not a failure (verdict `Approved`, exit `0`), and an unrecognized URL stays `0` because nothing was reviewed. |
| `1` | **Gated review outcome** — only when `PRXREF_FAIL_ON` is set: `error` exits `1` when the completed review carries an active error-severity finding, `any` exits `1` on any active finding, and under either value a review that does not complete also exits `1` — it crashes, or it ends with verdict `Error` (the forge could not be read, the diff could not be parsed or chunked, or every chunk review failed). An empty PR diff is not a failure (verdict `Approved`, exit `0`). The reason is printed to stderr. |
| `2` | **Usage or configuration error** — no subcommand, invalid command-line arguments, or a required value missing, malformed, outside its valid range, or outside its key's allowed vocabulary (`PRXREF_FAIL_ON` accepts only `never`, `error`, `any`). The message names the source that supplied it: the environment variable, or the CLI flag when a flag is what you typed. |

```
$ prxref review --pr-url https://github.com/org/repo/pull/1 --max-chunks 0
configuration error: --max-chunks: must be a finite number greater than 0, got 0
```

[Replay mode](#replay-mode-evaluation) keeps the same split. A bad set of replay flags exits `2` naming the flag: neither `--pr-url` nor `--diff-file`, a lone `--base-sha` or `--head-sha`, a SHA that is not full 40- or 64-character hex, two equal SHAs, SHAs without `--pr-url`, an unreadable `--diff-file`, two or more of `--as-of`, `--description-file` and `--no-description`, an `--as-of` that is not an ISO-8601 time with a UTC offset (a date alone included), `--as-of` without `--pr-url`, or an unreadable `--description-file`. These are checked before the PR URL is parsed, so they exit `2` even next to an unrecognized URL; pinned SHAs on a forge that cannot fetch a commit range also exit `2`, once the forge is known, and so does `--as-of` on a forge that cannot read description history (Bitbucket Server, GitLab, Azure DevOps). A description history that cannot be read is not an error: the replay keeps the current title and description and logs a warning. An empty pinned range or a blank diff file is a review error, unlike an empty PR diff: the run ends as an `Error` run, which exits `0` under the default `PRXREF_FAIL_ON=never` and `1` under `error` or `any`.

`PRXREF_FAIL_ON` is the one opt-out of the advisory contract, and its default `never` is the doctrine above, unchanged. Setting it to `error` or `any` turns the reviewer into a merge gate — failing a build on a finding turns a probabilistic reviewer into a gate, and the first false positive teaches a team to bypass the gate, so think hard before you set it. Read the verdict from the posted summary, which also carries a partial-review banner when some chunks did not make it. Do not build a security control on the exit code. The webhook daemon has no exit code and is unaffected.
