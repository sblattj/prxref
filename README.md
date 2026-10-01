# prxref

Fast automated AI code review for Bitbucket, GitLab, GitHub, Gitea/Forgejo, and Azure DevOps — Cloud and self-hosted.

prxref reviews pull and merge requests on Bitbucket, GitHub, GitLab, Gitea/Forgejo (including Codeberg), and Azure DevOps in sub-minute review cycles. It parses unified diffs, partitions changes into risk-ranked chunks, gives each worker the dependency pins and out-of-hunk definitions its chunk references when the forge can serve file content, fans out parallel single-shot LLM reviews across a cheap-first model fallback chain, filters findings through deterministic quality gates, and publishes inline comments alongside an executive summary. Give it the spec or ticket a change implements with `--spec` (a web page, a local file or directory, or a Jira ticket URL) and the review also checks the diff against that spec.

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
through eighteen more deterministic passes: the example-echo check, location validation, `package.json` claim
checks, line alignment, anchor snapping (verifying a finding's line against
the code it quotes in the head file), thread dedup, settled-thread
suppression, stable finding ids (on by default, `PRXREF_STABLE_IDS=0` opts out; see
[docs/env-vars.md](docs/env-vars.md)), severity
consistency, the removal-claim check, the hedge gate, the rule-scope check
(clearing a `rule` label no scoped team-rules section covers), finding grouping (opt-in
with `PRXREF_GROUP_FINDINGS`), the per-rule cap (on by default when review
rules are loaded; `PRXREF_MAX_FINDINGS_PER_RULE`), Also-at location
verification, the quality gate, sweep
dedup, and the containment note. A filtered finding is never discarded
silently — it is kept with a `drop_reason` for the run log, and visible in a
`--no-post` dry run or under `--format json`.

The CI wiring check (#66, on by default; `--ci-wiring off` or
`PRXREF_CI_WIRING=off` turns it off) flags a verification script, shebang
or test the PR adds that no CI configuration file invokes. Two more
deterministic checks are opt-in: the PR metadata rules (#70,
`metadata_rules = ".prxref/metadata.toml"` naming a separate TOML rules
file with `branch_patterns`, `commit_reference` and `area_globs`; see
[config-file.md](docs/config-file.md#pr-metadata-rules)), which check the branch name, the
commit subjects and the touched areas with zero model calls and report
a violation in a `PR metadata` section of the summary, never as a finding,
so it is never posted inline and never moves the verdict or
`PRXREF_FAIL_ON`; and the
execution-evidence drop (#69, `--evidence-file`), which drops a finding
claiming a header is missing when an exit-0 evidence item shows that header
as a `Name: value` line for the resource the finding names, and raises a
warning (at most 10 per run) at each `path:line` a failing evidence item
reports in a changed file, citing the command and its exit code.

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
[docs/env-vars.md](docs/env-vars.md) for the glob syntax. A PR-metadata
violation (#70) prints the same way, as one `pr metadata:` line each.

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

In CI, keep the pipeline to credentials and the endpoint, and commit a `.prxref.toml` at the repository root for everything else (see [docs/config-file.md](docs/config-file.md) and the [example](docs/examples/prxref.toml)); `prxref config check` validates it. On a lane that gates merges, read that file from the target branch, as [its security section](docs/config-file.md#security-which-copy-of-the-file-does-ci-read) shows.

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

# Gitea & Forgejo, on any host (including Codeberg)
prxref review --pr-url https://codeberg.org/owner/repository/pulls/7
prxref review --pr-url https://git.example.com/owner/repository/pulls/7

# Azure DevOps Services (dev.azure.com or the legacy *.visualstudio.com host)
prxref review --pr-url https://dev.azure.com/organization/project/_git/repository/pullrequest/42
prxref review --pr-url https://organization.visualstudio.com/project/_git/repository/pullrequest/42

# Azure DevOps Server (on-prem; the URL names the collection and the project)
prxref review --pr-url https://ado.corp.example/tfs/DefaultCollection/project/_git/repository/pullrequest/42
```

**Supported hosts.** Every forge is supported on any host. GitHub Enterprise Server and self-hosted GitLab share one adapter each with their SaaS products, which speak the same REST API at a different base URL. Bitbucket does not: Server / Data Center speaks `/rest/api/1.0` against different resource shapes, so it is a separate adapter selected automatically from the URL — `PRXREF_BITBUCKET_SERVER_TOKEN` for Data Center, `PRXREF_BITBUCKET_TOKEN` for Cloud. Gitea and Forgejo share one adapter on any host, Codeberg included, authenticated by `PRXREF_GITEA_TOKEN`; a public repository can be reviewed with no token. Azure DevOps Services and Server share one adapter. It has no diff endpoint to call, so it rebuilds the PR's diff from the changed files; a public project can be reviewed with no token at all. Posting to Azure DevOps is not yet verified against a live server, and Azure DevOps Server is untested. See [docs/forges.md](docs/forges.md).

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

On a reasoning model the hidden reasoning trace draws from the **same** completion budget as the answer, so turning `PRXREF_LLM_REASONING_EFFORT` up makes truncation *more* likely. A reply that hits the budget before it is usable is retried once at double the budget (capped at 16384), which costs one extra call and happens only in that case. If the retry is cut off too, the chunk is counted as failed and the posted summary names the reason and the variable to raise; see [Reasoning models and the token budget](docs/env-vars.md#reasoning-models-and-the-token-budget).

On `openai-compat` and `litellm`, temperature `0.0` and a sampling `seed` are sent on every call — `PRXREF_LLM_SEED` when set, else one random seed per process shared by the whole run (issue #56), and none at all with `PRXREF_LLM_SEED=off`, for providers such as Bedrock that reject it — but neither makes a review bit-reproducible — provider fingerprints, load-balanced backends, and gateways that ignore `seed` all still vary the model's output. The CLI backends send neither, and the run record's `sampling` field shows both as `null`. Everything downstream of the model is deterministic: findings are ordered by `(file, line, title)` and the caps break ties by content, and the run record's `sampling` field reports which knobs were in force. See [Determinism](docs/llm.md#determinism-what-is-pinned-and-what-still-varies).

See [docs/llm.md](docs/llm.md) for architecture, failover behavior, and backend setup, and [docs/env-vars.md](docs/env-vars.md#tuning-for-your-team) for tuning the confidence floor and finding caps to your team.

The model chain, the effort, the token budget and the other non-secret settings above can also be committed in the repository's `.prxref.toml` (`llm_models = ["z-ai/glm-5.3-flash"]`); the backend, the endpoint and the key stay in the environment. See [docs/config-file.md](docs/config-file.md); `prxref config check` validates the file.

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
| **Gitea / Forgejo** | `PRXREF_GITEA_TOKEN` | Access token: `read:repository` to review, `write:repository` plus `write:issue` to post. With none set, public repositories are read anonymously |
| **Azure DevOps** | `PRXREF_AZURE_DEVOPS_TOKEN` | Personal access token: Code (Read) to review, Code (Read & write) to post |
| **Azure DevOps (Pipelines)** | `SYSTEM_ACCESSTOKEN` | The job token, used when no PAT is set; map it into the step with `env: SYSTEM_ACCESSTOKEN: $(System.AccessToken)`. With neither set, public projects are read anonymously |

See [docs/env-vars.md](docs/env-vars.md) for the full configuration reference, [docs/forges.md](docs/forges.md) for forge specifics, [docs/quality.md](docs/quality.md) for the deterministic checks and every drop reason, and [docs/systemic-sweep.md](docs/systemic-sweep.md) for the whole-PR sweep's digest classes.

When the token can read a PR but not comment on it (a fork PR under a plain `pull_request` workflow, or a read-only pipeline token), prxref still delivers the review through the CI job itself — GitHub annotations and job summary, Azure Pipelines log issues, a GitLab Code Quality report, or the log — and records the failed posts in the run record; see [When prxref cannot post](docs/forges.md#when-prxref-cannot-post) (`PRXREF_FALLBACK`, on by default).

## Webhook Server

Run prxref as a persistent daemon to handle webhook events from GitHub, Bitbucket, GitLab, Gitea/Forgejo, and Azure DevOps:

```bash
prxref serve --port 8080 --host 0.0.0.0
```

The service exposes:
- `POST /webhook` — verifies HMAC or token signatures per forge (for Gitea and Forgejo, the `X-Forgejo-Signature` or `X-Gitea-Signature` HMAC against `PRXREF_GITEA_WEBHOOK_SECRET`, checked before the GitHub-compatible headers those forges also send; for Azure DevOps service hooks, the Basic-auth password against `PRXREF_AZURE_DEVOPS_WEBHOOK_SECRET`), enqueues incoming PR events, and responds immediately with `202 Accepted`. A background worker processes reviews serially. Registering each forge's webhook: [docs/deploy.md](docs/deploy.md#2-webhook-registration).
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

The file is plain text or Markdown: the ticket's title, description, and acceptance criteria, fetched from your tracker by a CI step. It must be a local file, and `prxref review` reads it before any network call. A URL, a missing file, a directory or other non-regular file, an unreadable file, or a file that is not UTF-8 is a configuration error: the run exits `2`, and the message names `--context-file`, `PRXREF_TICKET_CONTEXT_FILE` or the `.prxref.toml` key, whichever supplied the path. A path under the working directory that symlinks out of it is refused the same way. `--context-file PATH` wins over the variable for one run, and `--context-file ""` turns it off. Read the file from a trusted checkout or your CI, never from the PR under review, or the PR's author writes the ticket their change is judged against.

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
- `PRXREF_REVIEW_RULES_MAX_CHARS` (default `24000`) caps the body, with a warning when it truncates. The run record's `review_rules` carries the file's `sha256`, its character count, and the parsed map, never the rules text. It appears in `--format json`, the `-v` output, and the JSONL trace.
- **Path-scoped rules.** `--scoped-rules PATH` (repeatable), or `PRXREF_SCOPED_RULES` for every run, adds rules files that reach only part of the PR, next to that one file: each entry is a rules file, or a directory whose `*.md` files are read one level deep. A file's `applies_to:` front matter (alias `applyTo`, the `.github/instructions` spelling) lists the paths it covers, for example `applies_to: ["**/*.java", "!**/src/test/**"]`. Each chunk worker then gets only the files that match one of its paths, and the sweep gets the union; a file without `applies_to` reaches every unit. `PRXREF_SCOPED_RULES_MAX_CHARS` (default `24000`) caps the scoped text one unit receives. The run record's `scoped_rules` stamps each file's `sha256` and which unit received which file, and `-v` prints `scoped rules: 2 file(s) rules/helm.md=<first 12 hex> rules/java.md=<first 12 hex> cap=24000`. See [Path-scoped rules](docs/review-rules.md#path-scoped-rules).
- **Per-rule cap (0.15.0).** With either kind of rules file loaded, the model is asked to name the rule each finding applies, and one rule may produce at most `PRXREF_MAX_FINDINGS_PER_RULE` findings per review (default `2`), across files. The rest fold into the best one (highest severity, then confidence), whose comment lists them as `Also at: …`. `0` turns the cap off; without a rules file it does nothing. See [docs/quality.md](docs/quality.md).
- A missing, unreadable, or malformed file exits `2` before any network call, naming `--rules-file` or `PRXREF_REVIEW_RULES` (`--scoped-rules` or `PRXREF_SCOPED_RULES` for a scoped file).

**Read the rules from a trusted checkout, never from the PR under review.** In CI the workspace is usually the PR's own code, so a rules file inside it lets the PR rewrite its own review rules. Copy the file from the target branch or keep it outside the repository. See [docs/review-rules.md](docs/review-rules.md) for the grammar, the CI recipes, and the daemon.

## Prompt Template Overrides

Team rules add to prxref's review prompts but cannot take a line out of them. To change the prompts themselves, give prxref a directory of your own templates:

```bash
prxref prompts export ~/acme-prompts    # the packaged templates, byte for byte
# edit ~/acme-prompts/worker.md, and delete the templates you leave unchanged
prxref review --pr-url https://github.com/acme/widget/pull/42 --prompts-dir ~/acme-prompts

# or for every run, the webhook daemon included
export PRXREF_PROMPTS_DIR=~/acme-prompts
```

- **What can be overridden.** `worker.md` (each chunk worker's prompt), `systemic.md` (the whole-PR sweep's prompt) and `summary.md` (the posted summary comment). A template missing from the directory keeps the packaged one. The judge prompt of `prxref eval` cannot be overridden, so runs with different prompts are always graded by the same judge.
- **Checked before any network call.** A directory or template that fails a check exits `2`, naming `--prompts-dir`, `PRXREF_PROMPTS_DIR` or the `.prxref.toml` key, whichever supplied it. The checks are stated once, under `PRXREF_PROMPTS_DIR` in [docs/env-vars.md](docs/env-vars.md). `--prompts-dir DIR` wins over the variable for one run, and `--prompts-dir ""` turns it off.
- **Stamped on every run.** The run record's `prompt_templates` holds the directory and, for each template file present in it (edited or not, so an unchanged exported copy is stamped too), its path, the SHA-256 of its raw bytes and its length in characters, never its text; a template absent from the directory has no entry. It appears in `--format json` (`null` without a prompts directory) and the JSONL trace. The `-v` line reads `prompts: DIR summary=<sha256> worker=<sha256>`, showing the first 12 characters of each SHA-256, so every review names the exact templates that produced it.

**Read the templates from a trusted checkout, never from the PR under review.** Whoever controls the directory controls the whole review, so a PR that commits templates into the checkout CI reviews rewrites its own review. Copy the directory from the target branch or keep it outside the repository. See [docs/prompt-templates.md](docs/prompt-templates.md) for each template's placeholders, the CI recipe, the daemon, and upgrading.

## Repository Context

A chunk worker sees its chunk's diff and the definitions it references from the same file. Repository context also shows it the code the change leans on elsewhere: definitions from other files, and the API and database contracts a change points at. It is off by default:

```bash
PRXREF_REPO_CONTEXT=repo prxref review --pr-url https://github.com/acme/widget/pull/42 --no-post

# or from a local checkout of the PR head, with no forge at all
PRXREF_REPO_CONTEXT=repo prxref review --diff-file pr.diff --repo-dir ./widget
```

| `PRXREF_REPO_CONTEXT` | What each chunk worker also sees |
|---|---|
| `off` (default) | Nothing from repository context but the in-repo standards block below (#68), which is on by default at every level: with a reader and at least one document the standards globs select, each chunk gets its standards sections alone and the run record's `repo_context` has `mode` `standards`; otherwise no entry, no repository read beyond one listing, no `repo_context` trace event. The same-file definitions and dependency versions, Java and Kotlin ones included since 0.17.0, do not depend on this setting. |
| `diff` | Definitions from the PR's other changed files, and, for a type another chunk changes, the lines changed inside it. Java and Kotlin type declarations are found as well as JavaScript, TypeScript and Python ones. With a reader, a definition one file of the chunk references and another file of the same chunk holds outside its hunks is found too, and the in-repo standards block below. |
| `repo` | Also definitions from files outside the diff, found through imports, Java's same-package file convention, and a search of file names; a `### Contract excerpts` block with the OpenAPI operation or schema, JSON Schema, or earlier migration that a changed route, table or name matches; with a file listing, a `### Code elsewhere that reads state this chunk writes` block: short excerpts of unchanged code in the same language that reads a map, table or other state the chunk's added lines write to (at most 6 per chunk, ranked last in the budget); and, last, a `### In-repo standards for this chunk` block of heading-sliced sections of the repository's own standards documents (`docs/standards/**`, `docs/adr/**`, `STANDARDS*.md`, `SECURITY.md`, `CONTRIBUTING.md`, `.github/SECURITY.md`, `.github/CONTRIBUTING.md`), ranked per chunk by what the chunk itself names, capped by `PRXREF_CONTEXT_STANDARDS_MAX_CHARS` (default `6000`) and disabled with `PRXREF_CONTEXT_STANDARDS_GLOBS=off` (#68). |

- **Where it reads.** Files are read at the PR head through the forge (every forge adapter can read and list files, see [docs/forges.md](docs/forges.md)), or from `--repo-dir` when given. With neither, `diff` builds its entries from the diff's hunk lines, and `repo` does the same with a WARNING. At `repo` the repository's file listing is read once per run; without one, a WARNING says the name search and the contract files outside the PR are off.
- **What it never reads.** A floor that is always on keeps `**/expected.json`, `**/cases.json`, `**/case.json`, `**/prxref-eval/**`, `**/.env*`, `**/*.pem` and `**/*.key` out of every listing, read and repository-context entry. `PRXREF_CONTEXT_EXCLUDE_GLOBS` adds globs to it, and cannot take a floor path back out. Neither hides a changed file's hunks, which reach the prompt as the diff. `PRXREF_CONTEXT_CONTRACT_GLOBS` replaces the built-in set of contract globs.
- **Budget.** `PRXREF_REPO_CONTEXT_MAX_CHARS` (default `12000`) caps each chunk's entries, admitted in rank order until one does not fit, and a closing line counts the rest. Reads of files outside the PR are capped at 16 per chunk and 200 per run. The timeout retry, which follows a chunk call that outlasts `PRXREF_LLM_TIMEOUT`, drops all of it, the reader block included, to shrink the prompt.
- **Workers only.** The whole-PR sweep prompt is unchanged.
- **Recorded.** `--format json` and the run record carry `repo_context`: the reader, the listing size, the read counts, and each admitted entry's path, line and reason, never file text. The JSONL trace gains a `chunk context` event per chunk, and in text output `-v` prints a `repo context:` line.
- **Context follow-up (opt-in, #22).** A worker that cannot see a symbol's definition is told to ask about it at confidence 0.5 or below, so the default floor of 0.6 drops the question. With `PRXREF_CONTEXT_FOLLOWUP=on` at `repo`, a chunk whose first reply holds such a below-floor question is sent once more, with the whole definitions of up to 3 symbols its questions name, in backticks or as code-shaped plain text (every question gets one before any gets two), appended as a last block, `### Definitions referenced by this chunk, looked up for its open questions`. The lookup is deterministic, the model calls no tools, and a chunk gets at most one follow-up, none after a timeout retry, while the sweep gets none. A question the second reply confirms is replaced by the confirming finding; one it does not is dropped as `not confirmed by context follow-up`; the second reply's other findings are discarded. A follow-up that fails keeps the first reply's findings. It costs up to 8 reads, 4 excerpts and 4000 characters a chunk, and at most 2 more calls. At `off` (the default) nothing changes; at `on` below `repo`, or with no reader, one WARNING says it is off for the run. The run record carries `context_followup`. See [Context follow-up](docs/llm.md#context-follow-up-opt-in).

To measure what it changes, run [`prxref eval`](#evaluating-prxref) once per level and compare the two runs. Every setting: [docs/env-vars.md](docs/env-vars.md).

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
- **CLI text output** (`--no-post` or `-v`): the finding line ends in ` [scope: out]`, or ` [scope: in]` for a finding inside the ticket. With finding grouping on, or the per-rule cap active (review rules loaded), a finding that names a rule gets ` [rule: <rule>]` after that tag (see [docs/quality.md](docs/quality.md)).

Findings inside the ticket (`in`) and findings the reviewer could not place (`unknown`) carry no scope marker, so a run without a ticket context renders exactly the severity glyphs. Scope never changes a finding's severity. It is not counted separately either: the counts line counts every active finding by severity, and the verdict, the error cap, and `PRXREF_FAIL_ON` ignore scope.

Before 0.14.0, `outofscope` findings rendered 🟦. They now render ⬜ on every run, and 🟦 means only "outside the ticket".

Every glyph above is the default. `PRXREF_SEVERITY_MARKERS` (or `severity_markers` in `.prxref.toml`) replaces any of them with `name=glyph` pairs, for example `error=🔴,warning=🟡,out_of_ticket=🔷`; the names are `error`, `warning`, `spec`, `outofscope` and `out_of_ticket`, and the five glyphs must stay distinct. See [docs/env-vars.md](docs/env-vars.md).

## Code Suggestions

With `PRXREF_SUGGESTIONS=on` (off by default), an inline comment can carry replacement code for the lines it flags. Each forge gets the form it can apply:

| Forge | What the comment carries |
|---|---|
| GitHub (Cloud and Enterprise) | A `suggestion` block with the **Commit suggestion** button. A multi-line suggestion is posted as a comment on the whole line range. |
| GitLab (Cloud and self-managed) | A `suggestion:-0+N` block with the **Apply suggestion** button, anchored at the first line it replaces. |
| Bitbucket Cloud, Bitbucket Server / Data Center, Gitea / Forgejo, Azure DevOps | A **Suggested change** label naming the line or lines, then a plain code block to copy. No apply button. |

An empty suggestion deletes the lines: an empty `suggestion` block on GitHub and GitLab, and the sentence `Suggested change: delete line N` elsewhere. A suggestion replaces at most 20 lines, all inside one diff hunk. A suggestion that fails a check is dropped and the finding posts as a plain comment. The summary's findings list never shows suggestions.

## Incremental Re-review

By default every run re-reviews the whole PR. With `PRXREF_INCREMENTAL=on` (off by default), a push re-reviews only the files it changed:

- Each summary ends with an invisible marker naming the PR head it reviewed, `<!-- prxref-reviewed-head: <sha> -->`.
- On the next run, prxref reads that summary back and compares the marked head with the new one. Only the PR's files touched in between are chunked and reviewed, and only their earlier prxref inline comments are pruned. A merge from the base branch or a rebase cannot widen that set past the PR's own files.
- The whole-PR systemic sweep, the size advisory and the deterministic checks still see every file, so cross-file findings are not lost.
- The chunk findings, and so the verdict, cover the re-reviewed files only, plus whatever the sweep and the deterministic checks find anywhere in the PR. Earlier inline comments on the other files still stand, and the summary says so in one line. A push that changed nothing reviewable runs the sweep alone.
- When a review unit fails, the marker keeps the previous head, so the next push reviews those files again.

A run reviews every file on a first review, when the forge cannot read its summary or the read fails, when the summary has no marker, when the PR head is unknown or the forge cannot compare commits, and when the comparison fails (a force-push can make the marked head unknown). It also reviews every file with `--full-review`, with `PRXREF_POST_MODE=inline` (which never writes the marker), with `PRXREF_FAIL_ON` other than `never` (a gate's verdict must see the whole PR), and on every replay. `--full-review` and a `PRXREF_FAIL_ON` gate skip reading the previous summary, but they still record the head they reviewed, so the following push is incremental again (when a review unit of such a run fails, it records no head, and the following push reviews every file). The `--format json` key `incremental` says which scope a run took and why. Turning it on costs one extra forge read per run for the previous summary, plus a compare read when the head has moved. See [docs/env-vars.md](docs/env-vars.md).

## CLI Flags

`prxref review` takes:

- `--pr-url URL` — full web URL of the PR or MR on Bitbucket, GitHub, GitLab, Gitea/Forgejo, or Azure DevOps. Required unless `--diff-file` is given.
- `--no-post` — dry run; run review analysis and quality passes without writing comments to the forge. In text mode this also prints every active finding's location, title, and body, and every dropped finding with its drop reason.
- `--max-chunks N` — override maximum diff chunks evaluated (default `8`).
- `--timeout SECONDS` — override the per-model request deadline (default `45.0`, or `PRXREF_LLM_TIMEOUT` when set); the flag wins for the current invocation only.
- `--spec URL_OR_PATH` — a spec or ticket to review the PR against: a public web URL, a local file or directory, or a Jira ticket URL. Repeatable. When given, the flags replace `PRXREF_SPEC_SOURCES` entirely rather than adding to it. See [Review Against a Spec or Ticket](#review-against-a-spec-or-ticket).
- `--evidence-file PATH` — an execution-evidence file: command output from running this PR locally, shown to review units as trusted context. Repeatable; JSON or plain text. Replaces `PRXREF_EVIDENCE_FILES` for one run.
- `--rules-file PATH` — your team's review rules (Markdown or text, with optional front matter carrying a `severity:` map), added to every review prompt. Overrides `PRXREF_REVIEW_RULES` for this run, and `--rules-file ""` turns an environment-configured file off. Read it from a trusted checkout, never from the PR under review. See [Team Review Rules](#team-review-rules).
- `--metadata-rules PATH` — the PR-metadata rules file (TOML; format in [config-file.md](docs/config-file.md#pr-metadata-rules)): branch-name, commit-reference and area checks with no LLM call, reported in a `PR metadata` summary section and never as findings. Overrides `PRXREF_METADATA_RULES` for this run; `on` uses the flat `PRXREF_*` keys instead, and `""` or `off` turns the checks off. A bad or missing file exits `2` before any network call.
- `--scoped-rules PATH` — path-scoped team review rules: a rules file, or a directory whose `*.md` files are read one level deep. Each file reaches only the chunks whose paths match its `applies_to:` globs, and the whole-PR sweep gets the union. Repeatable. When given, the flags replace `PRXREF_SCOPED_RULES` entirely rather than adding to it, and `--scoped-rules ""` turns it off for this run. A file that fails its checks exits `2` before any network call. Read it from a trusted checkout, never from the PR under review. See [Path-scoped rules](docs/review-rules.md#path-scoped-rules).
- `--context-file PATH` — the ticket the PR is meant to implement (plain text or Markdown). Every finding is then marked in, out of, or of unknown ticket scope, and an empty file means "this PR has no ticket". Overrides `PRXREF_TICKET_CONTEXT_FILE` for this run, and `--context-file ""` turns it off. See [Ticket Context and Scope](#ticket-context-and-scope).
- `--prompts-dir DIR` — a directory of `worker.md`, `systemic.md` and `summary.md` templates that replace the packaged prompts; a template missing from it keeps the packaged one. Overrides `PRXREF_PROMPTS_DIR` for this run, and `--prompts-dir ""` turns it off. A directory that fails its checks exits `2` before any network call. Read it from a trusted checkout, never from the PR under review. See [Prompt Template Overrides](#prompt-template-overrides).
- `--repo-dir PATH` — a local checkout of the repository at the PR head. With `PRXREF_REPO_CONTEXT=repo`, repository context reads and lists files there instead of calling the forge (at `diff` it only reads there), so a `--diff-file` review gets repository context with no network. At any `PRXREF_REPO_CONTEXT` level, `off` included, a review with no forge file reader (`--diff-file` without `--pr-url`) also reads its chunk context there: the dependency versions and the same-file definitions a forge review gets. With `--pr-url` the forge still serves chunk context. It is not a replay flag: on its own it neither turns posting off nor adds a `replay` stamp. A path that is not an existing directory exits `2` before any network call, even while `PRXREF_REPO_CONTEXT` is `off`. See [Repository Context](#repository-context).
- `--trace-dir DIR` — write each review unit's exact prompt halves, raw model response, and metadata to `DIR` (`chunk0.system.md`, `chunk0.user.md`, `chunk0.response.json`, `chunk0.meta.json`, and so on for each chunk and for the whole-PR `sweep`). `PRXREF_TRACE_DIR` does the same for every run; the flag wins when both are set. The files number chunks from 0, while the JSONL trace's `chunk` events (`PRXREF_TRACE_FILE`) number them from 1 in `index`, so the event with `index` N pairs with `chunk{N-1}.*`.
- `--full-review` — review every file even when `PRXREF_INCREMENTAL=on`; the run still records the head it reviewed, so the following push is incremental again. See [Incremental Re-review](#incremental-re-review).
- `--ci-wiring {off,on}` — turn the CI wiring check (#66) on or off for this run (`PRXREF_CI_WIRING` does the same; `on` is the default): flag a check the PR adds — a verify/smoke/check script or flag, a file that gains a shebang, a test file outside the runner's default include — that no CI configuration file invokes. Needs the forge's head-sha file reads or `--repo-dir`.
- `--ci-wiring-globs GLOBS` — comma-separated globs selecting the CI configuration files the CI wiring check reads (`PRXREF_CI_WIRING_GLOBS`; a set value replaces the built-in set).
- `--context-standards-globs GLOBS` — comma-separated globs selecting the repository's own standards documents (`PRXREF_CONTEXT_STANDARDS_GLOBS`; a set value replaces the built-in set, and `''` or `off` reads no standards for this run).
- `--config PATH` — the [repository config file](docs/config-file.md) to read for this run, instead of `.prxref.toml` in the working directory. It wins over `PRXREF_CONFIG_FILE`, and `--config off` reads none. Paths inside the file resolve against the file's directory. A file that does not exist exits `2` with `configuration error: --config: config file not found: PATH`, and a file that fails its checks exits `2` naming the file and the key, both before any network call.
- `--no-config` — read no repository config file: neither `.prxref.toml` nor the file `PRXREF_CONFIG_FILE` names. `--config` and `--no-config` are mutually exclusive. Without either, the file comes from `PRXREF_CONFIG_FILE`, else `.prxref.toml` in the working directory (parent directories are never searched). The file sits below the environment: `defaults < .prxref.toml < PRXREF_* < flags`.
- `-v, --verbose` — output run timing, token counts, cost, and finding breakdowns to stdout, plus one line each for the rules file, the scoped rules files, the prompt templates, the ticket context (with the active findings' scope counts), the spec sources, and the repository context when they are configured. The repository-context line reads, for example, `repo context: mode=repo reader=forge listing=120(partial) reads=16 max_reads=200 max_chunk_reads=16 cap_hit=chunk entries=3 omitted=3`, where `reads` counts every repository-context fetch (PR diff files included, which spend neither cap), `max_reads` and `max_chunk_reads` are the run-wide and per-chunk read caps in force, and `cap_hit` names the cap that refused a read (`no`, `chunk`, `run` or `chunk+run`); the line is absent while `PRXREF_REPO_CONTEXT` is `off`. In text mode this also prints finding bodies and dropped findings, same as `--no-post`. These lines are text output only: with `--format json`, stdout holds the JSON object alone.
- `--format {text,json}` — output format for `review` (default `text`). `json` prints exactly one JSON object to stdout, with these keys in this order:
  - `verdict`;
  - `findings`: active first, then dropped, each with `file`, `line`, `severity`, `confidence`, `scope` (`in`, `out`, or `unknown` against the ticket context; always `unknown` without one), `rule` (the rule the finding names; `null` when it names none, and always `null` with finding grouping off and the per-rule cap inactive), `suggestion` (the kept replacement text, `""` deleting the lines; `null` when there is none, and always `null` with `PRXREF_SUGGESTIONS=off`), `suggestion_end_line` (the last line it replaces, `0` meaning `line` alone; always `0` when `suggestion` is `null`), `locations` (on a grouped finding, the other places its `Also at:` list names, in that order, as `{"file", "line"}` objects; on the best finding of a rule the per-rule cap folded, every location folded into it, across files, even past the five its `Also at:` list shows; `null` on every other row, including each member dropped as `grouped into <file>:<line>`, which keeps its own rule; see [docs/quality.md](docs/quality.md)), `anchor_unverified` (`true` on a model finding whose quoted evidence could not be anchored — no snippet parseable while file-level, a snippet the head file does not hold, or an ambiguous multi-match — whose confidence the anchor-snap pass lowered by 0.1; `false` on every other row), `id` (the finding's stable id, `file#rule#12-hex-claim-hash`; `null` only with `PRXREF_STABLE_IDS=0`), `anchor_block` (the enclosing function, YAML key or manifest key at the anchor; `null` with stable ids off or nothing enclosing it), `id_reused_from` (`run`, `verdict` or `thread` saying where a reused id came from; `null` when the id was freshly computed), `title`, `body`, `drop_reason`;
  - `chunk_count`, `chunks_reviewed`, `chunks_failed`;
  - `failed_chunks`: the review units that failed (#72). Always present, `[]` when every unit completed, and `null` when the run never reached review; otherwise one `{"unit", "kind", "files", "error"}` per failed unit in review order: `unit` is its 1-based position among `chunk_count`, `kind` is `chunk` (with that chunk's `files`) or `sweep` (the cross-file sweep, `files` empty), and `error` is the failure reason. The text summary prints the same files as `not reviewed:` after `coverage:`;
  - `chunks_over_budget`, `largest_chunk_tokens`, `overflow_files`, `chunk_token_budget`: how the diff was chunked. `chunk_token_budget` is the per-chunk token budget in force (`PRXREF_CHUNK_TOKEN_BUDGET`); `chunks_over_budget` is how many chunks' estimates (40 tokens per changed line) exceed it, `largest_chunk_tokens` the largest chunk's estimate, and `overflow_files` how many files were placed past the `PRXREF_MAX_CHUNKS` cap into the smallest chunk because no chunk had room. Such chunks get the same `PRXREF_LLM_MAX_TOKENS` for their answer as any other. The three counts are `0` when no chunking ran. When either `chunks_over_budget` or `overflow_files` is above `0`, text output adds one `chunks:` line saying so;
  - `elapsed_ms`, `input_tokens`, `output_tokens`;
  - `cost_usd`: the run's cost in USD, `null` when no source could price it (never `0` for an unknown cost), and `cost_estimated`: `true` when any part of it came from `PRXREF_PRICE_TABLE`. See [Cost accounting](docs/llm.md#cost-accounting);
  - `posted`;
  - `review_rules` (`path`, `sha256`, `chars`, `max_chars`, `truncated`, `severity_map`), `ticket_context` (`path`, `sha256`, `chars`, `max_chars`, `truncated`, `has_acceptance_criteria`, `empty`; never the ticket text), `spec_grounding` (`sources`, `ok`, `failed`, `constraints`, `digest_sha256`), and `size_advisory` (`changed_lines`, `changed_files`, `lines_limit`, `files_limit`, `triggered`, `message`). These four are always present and `null` when their feature is off;
  - `prompt_templates`: the prompt-template directory in force (`dir`, plus `templates` holding the `path`, `sha256` and `chars` of each template file present in the directory, edited or not; never the template text). Always present, and `null` when no prompts directory is configured. See [Prompt Template Overrides](#prompt-template-overrides);
  - `scoped_rules`: the path-scoped rules in force (`entries` as configured; `files`, one row per loaded file in load order with its `path`, `sha256`, `chars`, `max_chars`, `truncated`, `severity_map` and `applies_to`; `max_chars`, the per-unit cap; and `units`, the `path` and `chars` of the files each chunk and the sweep received; never the rules text). Always present, and `null` when no scoped rules are configured. See [Path-scoped rules](docs/review-rules.md#path-scoped-rules);
  - `rule_counts`: the per-rule cap's tally, one `{"rule", "kind", "total", "kept"}` object for each rule (`kind` `rule`), or for each normalized title among findings that name no rule (`kind` `title`), that at least two chunk findings share: `total` of them, `kept` active, most findings first. A rule or title with one finding gets no row. Always present: `[]` when the cap ran and nothing repeated, and `null` when it did not run (no review rules file, `PRXREF_MAX_FINDINGS_PER_RULE=0`, or a run that ended before the quality passes). See [docs/quality.md](docs/quality.md);
  - `rule_scope_cleared`: how many findings had their `rule` label cleared by the rule-scope check (#75) — the label named a scoped section of the loaded team rules (by heading or rule line) whose `scope:` line does not cover the finding's path. The findings themselves stay active and group by title. Always present: `0` when the check ran and cleared nothing, and `null` when no loaded rules file declares a section scope. See [docs/quality.md](docs/quality.md);
  - `repo_context`: the repository context in force (`mode`, `max_chars`, `max_reads` and `max_chunk_reads` (the run-wide and per-chunk read caps), `contract_globs` and `exclude_globs`; `reader`, which is `forge`, `repo-dir` or `null`; `listing`, `{"paths", "complete"}` or `null`; `reads`; `chunk_read_cap_hit` and `run_read_cap_hit`, true when that cap refused a read, and `read_cap_hit`, their OR; and `units`, `{"chunks": [...]}` holding one `{"entries", "omitted", "retry_dropped"}` row per chunk in chunk order, whose entries carry `path`, `line`, `symbol`, `kind`, `reason` and `chars`; never the file text). Always present, and `null` when `PRXREF_REPO_CONTEXT` is `off`. See [docs/env-vars.md](docs/env-vars.md);
  - `parse_retries`: how many times the run re-sent a request whose reply could not be used as a review (empty, not JSON, not a JSON object, or an object without a `findings` list), summed over every chunk and the whole-PR sweep. Always present: `0` when nothing was re-sent, and `null` when `PRXREF_LLM_PARSE_RETRIES` is `0`. With `--trace-dir`, each unit that retried has `parse_retries` and `first_error` in its meta file. See [docs/env-vars.md](docs/env-vars.md);
  - `context_followup`: the context follow-up's tally. Always present, and `null` while `PRXREF_CONTEXT_FOLLOWUP` is `off`; when it is `on`, `active` (`false` below `PRXREF_REPO_CONTEXT=repo` or without a repository reader), the run's `calls`, `confirmed`, `unconfirmed` and `discarded` counts and `input_tokens` and `output_tokens`, and `chunks`, one row per chunk with the names looked up, each excerpt's `path`, `line`, `symbol`, `source` and `chars` (never the file text), and why a chunk was `skipped`, `null` when not active. See [Context follow-up](docs/llm.md#context-follow-up-opt-in);
  - `suggestions`: the code-suggestion tally (#30). Always present, and `null` while `PRXREF_SUGGESTIONS` is `off`; when it is `on`, `{"kept": n, "cleared": {"<reason>": n, ...}}` over the posted findings, one `cleared` entry per reason a suggestion was withheld (see [docs/llm.md](docs/llm.md));
  - `incremental`: the incremental re-review scope (#34). Always present, and `null` while `PRXREF_INCREMENTAL` is `off`; when it is `on`, `{"mode", "reason", "since_sha", "files_total", "files_reviewed", "marker_sha"}`: `mode` is `incremental` or `full`, `reason` says why a run is full (`null` when incremental), `since_sha` is the reviewed head the run compared from, and `marker_sha` the head its summary is stamped with. See [Incremental Re-review](#incremental-re-review);
  - `ci_wiring`: the CI wiring check's tally (#66). Always present, and `null` while `PRXREF_CI_WIRING` is `off`; when it is `on` (the default), `{"candidates", "ci_files", "picked_up_default", "triggered"}`, or `{"triggered": false, "reason": ...}` when the run had no repository reader. See [docs/env-vars.md](docs/env-vars.md);
  - `evidence`: the execution-evidence tally (#69). Always present, and `null` while no evidence file was given; otherwise `{"files", "items", "matched_chunks", "max_chars"}`. See [docs/env-vars.md](docs/env-vars.md);
  - `stable_ids`: the stable-finding-id tally (#71). Always present, and `null` only when `PRXREF_STABLE_IDS=0`; otherwise `{"assigned", "reused_from_verdict", "reused_from_thread", "collisions"}` over every finding of the run. See [docs/env-vars.md](docs/env-vars.md);
  - `degraded`: the posts that failed (#48). Always present, and `null` when every attempted post succeeded or nothing was posted; otherwise `{"cause", "failed", "fallback", "annotations"}`: `cause` is `permission` when a post was refused with HTTP 401 or 403 (a read-only token) and `error` otherwise, `failed` lists `summary` and/or `inline`, `fallback` lists what the CI fallback emitted instead (`github-annotations`, `github-step-summary`, `azure-logging`, `gitlab-codequality`, `log`; empty with `PRXREF_FALLBACK=off`), and `annotations` counts the annotation lines or report entries emitted. Under `--format json` the stdout annotations and Azure logging commands are skipped, so `github-annotations` and `azure-logging` never appear in `fallback`. See [When prxref cannot post](docs/forges.md#when-prxref-cannot-post);
  - `metadata_rules`: the deterministic PR-metadata checks' tally (#70). Always present, and `null` while `PRXREF_METADATA_RULES` is `off`; when it is `on` or names a rules file, `{branch_pattern, commit_reference, area_globs, violations}`, each check `pass`, `fail` or `skipped: <reason>`, and `violations` one `{check, title, detail}` row per violation (violations are summary notes, never `findings` rows). See [docs/env-vars.md](docs/env-vars.md);
  - `config_file`: the repository config file the run read (#38). Always present, and `null` when no file was read; otherwise `{"path", "sha256", "keys"}`: the file as its errors name it (relative to the working directory when inside it), the sha256 of its bytes, and the sorted keys it sets, including any that the environment or a flag then overrode. With `-v` in text mode, one log line `config: <path> (<n> keys)` says the same. See [docs/config-file.md](docs/config-file.md);
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

The other subcommands: `prxref serve [--port N] [--host H] [--config PATH]` runs the [webhook server](#webhook-server) (default port `8080`, default host `0.0.0.0`); `prxref trace render FILE [-o OUT]` renders a JSONL run trace (`PRXREF_TRACE_FILE`) to a standalone HTML pipeline view, written next to the trace unless `-o`/`--out` names the output; and `prxref --version` prints the version.

`prxref serve --config PATH` names a [repository config file](docs/config-file.md) for every webhook review. The daemon never auto-discovers `.prxref.toml`, because its working directory is not the repository it reviews: it reads a file only when `--config` or `PRXREF_CONFIG_FILE` names one (`off` reads none), checks it before listening (a missing or invalid file exits `2`), and re-reads it on every webhook. `serve` has no `--no-config`; leave both unset, or give `--config off`.

`prxref config check [--config PATH | --no-config] [--format {text,json}]` checks the [repository config file](docs/config-file.md) and the environment without reading a pull request or calling a model. It resolves the file exactly as `review` does, then prints `config file: <path>` (or `config file: none`), one `<key> = <value>  (<source>)` line per setting, sorted, whose source is `default`, `file` or `env PRXREF_<NAME>`, and `ok`. Credentials and webhook secrets print only as `<set>` or `<unset>`. `--format json` prints one object, `{"config_file": <path or null>, "values": {"<key>": {"value", "source"}}}`. It exits `0` when the configuration is valid, and `2` with the `configuration error: ...` line a review would print when it is not; with `--format json`, an error leaves stdout empty. A bare `prxref config` prints the top-level help and exits `2`.

`prxref prompts export DIR [--force]` writes the packaged `worker.md`, `systemic.md` and `summary.md` prompt templates into `DIR`, byte for byte, as the starting point for a `PRXREF_PROMPTS_DIR` override directory, and prints each path it wrote. It creates `DIR` when it is missing. When any of the three files already exists it overwrites nothing, writes nothing, and exits `2` naming the file; `--force` overwrites them. The judge prompt of `prxref eval` is never exported, because it cannot be overridden. How to edit and use the exported templates: [Prompt Template Overrides](#prompt-template-overrides).

`prxref eval` scores [replays](#replay-mode-evaluation) against labelled human findings. It never posts, and it adds no environment variable. Its three actions:

- `prxref eval run --cases PATH --label NAME [--out DIR] [--rules-file PATH] [--scoped-rules PATH] [--prompts-dir DIR] [--resume] [--config PATH | --no-config]` replays every case and writes the run to `DIR/NAME/`:
  - `--cases PATH` — the labelled cases: a `cases.json` file, or a directory of `case-*/` directories. Required. A bad case exits `2`, naming `--cases`, the case id, and the field.
  - `--label NAME` — the run's name and its directory under `--out`. Required. An existing label exits `2` unless `--resume` is given.
  - `--out DIR` — the directory that holds the runs (default `./prxref-eval/`). `score` and `compare` take it too.
  - `--rules-file PATH` — team review rules for every case, as for `review`: it overrides `PRXREF_REVIEW_RULES`, and `--rules-file ""` turns it off.
  - `--scoped-rules PATH` — path-scoped rules for every case, as for `review`: it overrides `PRXREF_SCOPED_RULES`, and `--scoped-rules ""` turns it off. Repeatable.
  - `--prompts-dir DIR` — prompt templates for every case, as for `review`: it overrides `PRXREF_PROMPTS_DIR`, and `--prompts-dir ""` turns it off.
  - Each of the three is checked once, before the first case runs: a file or directory that fails its checks exits `2`, naming the flag, or the variable when the flag is not given. The run's `run.json` and `score.json` record the scoped rules in force, as `review` records them.
  - `--resume` — continue an existing `--label` run instead of refusing it.
  - `--config PATH` / `--no-config` — the [repository config file](docs/config-file.md) every case reviews with, as for `review`: without either, `PRXREF_CONFIG_FILE`, else `.prxref.toml` in the working directory. The file is resolved and checked once, before the first case runs; a missing or invalid one exits `2`. The run's `run.json` `config` reflects it.
- `prxref eval score --label NAME [--judge-model MODEL] [--out DIR]` grades the run against its labels and writes `score.json` and `score.md`:
  - `--judge-model MODEL` — the model that grades every label without a `must_match` predicate, on the review's own LLM backend. Required when any label lacks one; leaving it out then exits `2`. A judge model the review itself used logs a warning.
- `prxref eval compare A B [--out DIR]` prints two scored runs side by side, then every label whose credit changed and the cases and labels only one run has. `A` and `B` are each a label under `--out` or a run directory. It warns when the runs are not like for like.

The whole reference, from the case format to every `score.json` key: [docs/evals.md](docs/evals.md).

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
- **`--diff-file PATH`** reviews that file (`git diff` or `git format-patch` output) instead of fetching a diff. Without `--pr-url` nothing is contacted: there are no threads and no file context unless `--repo-dir` names a checkout to read it from, and a `git format-patch` file supplies the title, description and author. With `--pr-url` the file replaces the PR's diff, and without `--head-sha` a warning says that file context is still read at the PR's current head.
- **The title and description are pinned too.** A `--pr-url` replay shows the PR's title and description as they were at a cutoff, not as they are now, because a description edited after review (a table of the review fixes, say) tells the model what the reviewers found. The cutoff is the first of these that exists:
  1. `--as-of TIME`, when you give it;
  2. the PR's first human review or comment: not by the PR's author, not by a bot account, and not one of prxref's own posts;
  3. the date of the head commit (`--head-sha` when given, else the PR's current head).

  The history is read once, before the review starts, by one call to the forge's history reader; that network read is not recorded in the run trace. GitHub and Bitbucket Cloud can read it, and GitHub needs a token to (see "Description History (Replay)" in [docs/forges.md](docs/forges.md)). The title is pinned exactly when the description is. A cutoff before the PR was opened shows the original description.
- **Falling back to the current text.** When the history cannot pin them, the replay keeps the PR's *current* title and description, stamps `"description": "live"`, and logs a WARNING that starts `replay shows the PR's CURRENT title and description` and gives the reason: the forge cannot read description history (Bitbucket Server, GitLab, Gitea/Forgejo and Azure DevOps cannot), the read failed (no GitHub token, a 401 or 403, a transport error), the history has neither a first review nor a head commit date, or it does not reach the cutoff (it is incomplete, or the version then in force was deleted). None of these stops the review. An explicit `--as-of` on a forge that cannot read history is the exception: it exits `2` naming `--as-of`, because the time you asked for cannot be honoured.
- **`--description-file PATH` and `--no-description`** replace the description with that file's text or with nothing, read no history, and leave the title as it is now. Both also work without `--pr-url`, replacing the description a `git format-patch` file supplies. At most one of `--as-of`, `--description-file` and `--no-description` may be given.
- **What a pinned replay does not pin.** The PR's current threads still reach the prompt unless you add `--no-threads`; a replay at pinned SHAs without `--no-threads` logs a warning saying so. With `--pr-url`, `--diff-file` and no `--head-sha`, file context is read at the PR's current head, with a warning. The title stays the current one under `--description-file` and `--no-description`, and after a fall back to the current text.
- **The record.** A replay's JSON record gains a `replay` stamp, always with all seven keys, and the text summary prints it as a `replay:` line. A normal run's record has no `replay` key.

  ```json
  "replay": {"base_sha": null, "head_sha": null, "threads": "hidden", "diff_file": "change.diff", "description": "file", "as_of": null, "as_of_source": null}
  ```

  `threads` is `"hidden"` under `--no-threads` or with no `--pr-url`, else `"shown"`; `diff_file` is the path as you typed it. `description` is `"pinned"` (the title and description in force at the cutoff), `"live"` (the current ones), `"file"` (`--description-file`, or `--diff-file` without `--pr-url`, where any description comes from the file) or `"none"` (`--no-description`). `as_of` is the cutoff as a UTC time ending in `Z`, with a fraction of a second only when the source time had one, and `as_of_source` is `"flag"`, `"first-review"` or `"head-commit"`. Both are set whenever a cutoff was chosen, on a `"live"` stamp too: one whose history did not reach the cutoff, or whose history read failed after `--as-of` had chosen it. Both are `null` when no cutoff was chosen. Given back as `--as-of`, `as_of` names the same instant. The text line then ends `description=pinned as_of=2026-05-01T09:30:00Z (first-review)`.
- **Exit codes.** A bad set of replay flags exits `2` naming the flag, and it is checked before the PR URL is parsed. A review error inside a replay — an empty pinned range (a head already merged into the base) or a blank diff file — ends the run as an `Error` run: it exits `0` under the default `PRXREF_FAIL_ON=never`, and `1` under `error` or `any`, like any review that does not complete. See [Exit Codes](#exit-codes).
- The replay flags have no environment variable, on purpose, and the [webhook server](#webhook-server) never replays.

## Evaluating prxref

`prxref eval` turns replays into numbers. Give it pull requests that human reviewers have already reviewed, each labelled with the findings they left, and it replays every case through the real pipeline, grades prxref's findings against the labels, and compares two runs, so a new model, prompt template, rules file or setting can be judged across a dataset rather than on one PR:

```bash
prxref eval run --cases tests/evals --label base
PRXREF_LLM_MODELS=example-model-b prxref eval run --cases tests/evals --label cand
prxref eval score --label base
prxref eval score --label cand
prxref eval compare base cand
```

- **Cases** come as a `cases.json` file or as a directory of `case-*/` directories, the layout of [`tests/evals/`](tests/evals/README.md). A label is graded deterministically when it carries a `must_match` predicate, and by an LLM judge on the review's own backend (`--judge-model`) when it does not.
- **Recall** is micro recall over every label, with half credit for a `partial` judge grade, broken down by severity and by category. The score also reports unmatched AI findings per PR, severity agreement, failed chunks, time and cost, and never sums an unknown cost.
- **Runs** go to `./prxref-eval/<label>/` by default; add `prxref-eval/` to your `.gitignore`. A run never posts, and a case that fails is recorded and scored, never fatal.

The full reference is [docs/evals.md](docs/evals.md).

## Exit Codes

`prxref` is an advisor, never a gate. Its exit code says whether *prxref* was configured correctly, not whether your code is good.

| Code | Meaning |
|---|---|
| `0` | The run finished — **including every review error**: a network failure, an LLM timeout, bad forge credentials, an unrecognized URL, or a review in which every chunk failed. Diagnostics go to stderr; the pipeline step stays green. With `PRXREF_FAIL_ON` set to `error` or `any` (see below), only two outcomes turn this into `1`: a completed review whose active findings trip the policy, and a review that does not complete — it crashes, or it ends with verdict `Error` (the forge could not be read, the diff could not be parsed or chunked, or every chunk review failed) or verdict `Incomplete` (some chunk reviews failed, so the review only partially happened; the verdict ladder is `Error` → `Request-Changes` → `Incomplete` → `Approved`). An empty PR diff is not a failure (verdict `Approved`, exit `0`), and an unrecognized URL stays `0` because nothing was reviewed. |
| `1` | **Gated review outcome** — only when `PRXREF_FAIL_ON` is set: `error` exits `1` when the completed review carries an active error-severity finding, `any` exits `1` on any active finding, and under either value a review that does not complete also exits `1` — it crashes, or it ends with verdict `Error` (the forge could not be read, the diff could not be parsed or chunked, or every chunk review failed) or verdict `Incomplete` (some chunk reviews failed). An empty PR diff is not a failure (verdict `Approved`, exit `0`). The reason is printed to stderr. |
| `2` | **Usage or configuration error** — no subcommand, invalid command-line arguments, or a required value missing, malformed, outside its valid range, or outside its key's allowed vocabulary (`PRXREF_FAIL_ON` accepts only `never`, `error`, `any`). The message names the source that supplied it: the environment variable, or the CLI flag when a flag is what you typed. |

```
$ prxref review --pr-url https://github.com/org/repo/pull/1 --max-chunks 0
configuration error: --max-chunks: must be a finite number greater than 0, got 0
```

[Replay mode](#replay-mode-evaluation) keeps the same split. A bad set of replay flags exits `2` naming the flag: neither `--pr-url` nor `--diff-file`, a lone `--base-sha` or `--head-sha`, a SHA that is not full 40- or 64-character hex, two equal SHAs, SHAs without `--pr-url`, an unreadable `--diff-file`, two or more of `--as-of`, `--description-file` and `--no-description`, an `--as-of` that is not an ISO-8601 time with a UTC offset (a date alone included), `--as-of` without `--pr-url`, or an unreadable `--description-file`. These are checked before the PR URL is parsed, so they exit `2` even next to an unrecognized URL; pinned SHAs on a forge that cannot fetch a commit range also exit `2`, once the forge is known, and so does `--as-of` on a forge that cannot read description history (Bitbucket Server, GitLab, Gitea/Forgejo, Azure DevOps). A description history that cannot be read is not an error: the replay keeps the current title and description and logs a warning. An empty pinned range or a blank diff file is a review error, unlike an empty PR diff: the run ends as an `Error` run, which exits `0` under the default `PRXREF_FAIL_ON=never` and `1` under `error` or `any`.

`PRXREF_FAIL_ON` is the one opt-out of the advisory contract, and its default `never` is the doctrine above, unchanged. Setting it to `error` or `any` turns the reviewer into a merge gate — failing a build on a finding turns a probabilistic reviewer into a gate, and the first false positive teaches a team to bypass the gate, so think hard before you set it. Read the verdict from the posted summary, which also carries a partial-review banner when some chunks did not make it. Do not build a security control on the exit code. The webhook daemon has no exit code and is unaffected.
