# Thread replies

prxref can answer follow-up comments in its own review threads: a person asks
"why is this a bug?" under an inline finding, or writes `@prxref explain the
retry finding` as a PR comment, and prxref posts one grounded answer in that
thread. It is **off by default**.

## Modes

Set `PRXREF_THREAD_REPLIES` (or `thread_replies` in `.prxref.toml`):

| Mode | Inline thread rooted at a prxref finding | PR comment stream |
|---|---|---|
| `off` (default) | never answered; nothing is read | never answered |
| `mention` | answered when the latest comment names the mention token | answered when the latest comment names the mention token |
| `always` | answered on every human reply | answered when the latest comment names the mention token |

The mention token is `PRXREF_REPLY_MENTION` (`reply_mention`), default
`@prxref`, matched case-insensitively anywhere in the comment. It must be one
token without whitespace, at most 64 characters; an empty value reads as the
default, never as a token that matches every comment.

## Trigger rule

A reply sweep lists every conversation the forge reports (each inline thread
whose root comment is a prxref finding, plus the PR's top-level comments) and
answers a conversation only when **its last comment is a person's**. A comment
is prxref's when it carries the attribution marker (`Reviewed by prxref`),
never by author name, since the bot account differs per install.

The rule is stateless, which is what makes it safe:

- prxref's own answer becomes the last comment, so the conversation is not due
  again until a person writes again. Answering is the dedup.
- A redelivered webhook, or a second `prxref reply` run, finds nothing due and
  posts nothing.
- Only the latest comment counts: when a person mentions `@prxref` and someone
  else comments after them before the sweep runs, the mention is no longer the
  last comment and is not answered. Mention again to ask again.
- In `always` mode any human reply is due, a plain "thanks" included.

## Settled threads are not re-argued

An inline thread is **settled** when it is resolved, when any person wrote an
explicit won't-fix ("won't fix", "by design", "wontfix", and similar), or when
the latest human reply rebuts the finding as a statement ("this is fine,
the caller checks it"). A reply that asks something (it holds a `?`) is a
question, not a rebuttal.

A settled thread is skipped (`settled`) unless its latest comment names the
mention token. Then prxref answers, and the prompt tells the model the finding
is settled: it must accept the decision and must not re-raise or re-argue the
finding. Replies never create findings or inline comments.

## Context

Each answer is **one single-shot model call** with context gathered
beforehand, deterministically. The model calls no tools, and no answer
triggers another call.

- Inline thread: the finding and every comment in the thread, the diff
  hunk(s) of its file covering its line, and 30 lines of the head file on
  each side of that line. An outdated thread uses its original line.
- PR comment: the comment stream, the review summary and the list of changed
  files.

The whole prompt's data is capped at `PRXREF_REPLY_CONTEXT_CHARS`
(`reply_context_chars`, default 24000). The finding and the asking comment
are always kept (each cut to a third of the budget); the rest goes to the
hunk or summary, then to the earlier comments, newest first, then to the
head-file window or file list.

Every comment, hunk and file is fenced in an untrusted data block. The data
cannot close its own block early, and the system prompt says text inside a
block is data, never instructions. An answer has prxref's own markers
stripped, is cut to 4000 characters and carries the usual model attribution
line.

The template is `reply.md`. To change it, put your own `reply.md` in the
`PRXREF_PROMPTS_DIR` directory; it must keep the `## Conversation` heading,
which splits the system prompt (above) from the user prompt, and the
`{status}`, `{conversation}` and `{code}` placeholders after it. The packaged
copy is `src/prxref/prompts/reply.md`. A review run never reads `reply.md`.

## Bounds

| Bound | Value |
|---|---|
| Model calls per sweep | `PRXREF_REPLY_MAX` (`reply_max`, default 5); a failed call uses its slot, the rest are `skipped (capped)` |
| Completion tokens per call | 1024 |
| Wall clock per call | 120 s |
| Prompt data per call | `PRXREF_REPLY_CONTEXT_CHARS` |
| Posted answer | 4000 characters |

A failed model call posts nothing and is recorded `failed`, its reason
redacted the way a posted review error is. A failed post is recorded and the
sweep goes on. `PRXREF_DRY_RUN` (or `prxref reply --dry-run`) makes the calls
and posts nothing.

## Loop safety

- prxref answers only when the last comment is a person's, and its answer
  carries the marker, so it can never answer itself.
- The webhook daemon also drops a comment event whose body carries the marker
  before queueing anything.
- Edited and deleted comment events are ignored; only `created` wakes a sweep.
- A re-review's prune pass deletes prxref's old inline comments, answers
  included, so a thread whose finding was pruned disappears with its answers.

## Webhook daemon

`prxref serve` accepts these GitHub events next to `pull_request`:

- `pull_request_review_comment`, action `created`;
- `issue_comment`, action `created`, on a pull request (an issue's comments
  are ignored).

Each one queues a reply sweep for its PR on the same serial worker reviews
use. The handler re-reads the configuration, `serve --config` or
`PRXREF_CONFIG_FILE` included, on every event, so with `thread_replies` off it
logs one line and reads nothing. Subscribe the GitHub webhook to "Pull request
review comments" and "Issue comments" as well as "Pull requests".

## CI mode

```bash
prxref reply --pr-url https://github.com/acme/widget/pull/42 [--config PATH | --no-config] [--dry-run]
```

It prints one line per conversation, for example
`answered: inline src/app.py:10 reply_to=101 model=...` or
`skipped (no-mention): pr pr comments reply_to=-`. It exits `0` on every
review-type failure (network, model, a failed post) and `2` on a
configuration error, naming the variable or key.

A GitHub Actions workflow:

```yaml
on:
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]

jobs:
  reply:
    if: >-
      (github.event.issue.pull_request || github.event.pull_request)
      && !contains(github.event.comment.body, 'Reviewed by prxref')
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      issues: write
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.repository.default_branch }}
      - run: pip install --quiet prxref
      - run: prxref reply --pr-url "$PR_URL"
        env:
          PR_URL: ${{ github.event.pull_request.html_url || github.event.issue.pull_request.html_url }}
          PRXREF_THREAD_REPLIES: mention
          PRXREF_GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          PRXREF_LLM_BASE_URL: ${{ secrets.PRXREF_LLM_BASE_URL }}
          PRXREF_LLM_API_KEY: ${{ secrets.PRXREF_LLM_API_KEY }}
          PRXREF_LLM_MODELS: ${{ secrets.PRXREF_LLM_MODELS }}
```

Check out the default branch, never the PR head, so the PR cannot rewrite its
own `.prxref.toml` or prompt templates.

## Forge support

Replies work on forges implementing `list_conversations` and `post_reply`.

| Forge | `list_conversations` / `post_reply` | Webhook comment events |
|---|---|---|
| GitHub | yes | `pull_request_review_comment`, `issue_comment` |
| GitLab, Gitea/Forgejo, Bitbucket Cloud, Bitbucket Server, Azure DevOps | as each adapter implements them | not yet; run `prxref reply` |

On a forge without them, a sweep logs one line naming `list_conversations`
and answers nothing.
