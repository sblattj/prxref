# Review History, Trend Line and Regression Gate

`PRXREF_HISTORY_FILE` gives `prxref review` a memory across runs (#46). Each
completed review appends one line to a file in your repository. The summary
then says how this PR compares with the recent ones, and an opt-in gate can
fail the CI step when the count jumps.

All of it is off by default. With `PRXREF_HISTORY_FILE` empty, prxref reads
and writes nothing, the summary is unchanged and the `--format json` key
`history` is `null`.

## Turning it on

```sh
export PRXREF_HISTORY_FILE=.prxref-history.jsonl
prxref review --pr-url "$PR_URL"
```

The three settings are env-only: a `.prxref.toml` cannot set them, because a
PR could otherwise point the write at any path or switch the gate off.

| Variable | Default | Meaning |
|---|---|---|
| `PRXREF_HISTORY_FILE` | empty (off) | The file to append to and measure against. |
| `PRXREF_FAIL_ON_REGRESSION` | `off` | `on` turns on the regression gate. Needs `PRXREF_HISTORY_FILE`. |
| `PRXREF_REGRESSION_TOLERANCE` | `0.5` | How far above the mean the gate allows, as a fraction (0 to 10). |

A bad value exits `2` naming the variable, before any review runs:
`PRXREF_FAIL_ON_REGRESSION` other than `off` or `on`, a tolerance that is not
a number from 0 to 10, or `PRXREF_FAIL_ON_REGRESSION=on` without a history
file.

## The file format

One compact JSON object per line, one line per run, so a `git diff` of the
file is one added line per review. The keys are always written in this order:

| Key | Meaning |
|---|---|
| `timestamp` | When the run finished, ISO 8601 in UTC (`2026-11-26T09:30:00Z`). |
| `pr` | The PR number (`0` for a `--diff-file` run without `--pr-url`). |
| `head_sha` | The PR head the run reviewed (empty when the forge gave none). |
| `verdict` | The run's verdict, such as `Approved` or `Request-Changes`. |
| `findings` | Active findings by severity. `error`, `warning`, `spec` and `outofscope` are always present. |
| `findings_total` | The number of active findings. This is the number the trend and the gate use. |
| `dropped` | Findings the quality passes dropped, by pass name, sorted by name. The names are the `gate` label of `prxref_findings_dropped_total` in [metrics.md](metrics.md); `other` collects the rest. |
| `dropped_total` | The number of dropped findings. |
| `prxref_version` | The prxref version that wrote the line. |

An example line:

```json
{"timestamp":"2026-11-26T09:30:00Z","pr":412,"head_sha":"3f1c9a7e5b2d4c6a8e0f1b3d5c7e9a1b3d5f7a9c","verdict":"Request-Changes","findings":{"error":1,"warning":2,"spec":0,"outofscope":0},"findings_total":3,"dropped":{"apply_hedge_gate":1,"apply_quality_gate":2},"dropped_total":3,"prxref_version":"0.44.0"}
```

### Which runs are recorded

A line is appended only for a completed review. These runs are not
appended, and the JSON key `history.reason` says why:

- a run that ends with verdict `Error` or `Incomplete`;
- a partial review (some chunk reviews failed);
- an incremental re-review (`PRXREF_INCREMENTAL=on`) that reviewed only part
  of the PR, since its count is not comparable with a full review.

Dry runs (`--no-post`, `PRXREF_DRY_RUN`) and replays are recorded like any
other review, because the variable is set on purpose. `prxref eval run`
never writes the file. The webhook daemon (`prxref serve`) and the gate app
(`prxref gate serve`) ignore the variable: one file cannot describe every
repository a daemon sees.

### Bad lines and failed writes

- A line that is not a JSON object with a whole, non-negative
  `findings_total` is skipped, with a warning naming its line number. It is
  never fatal, and the JSON key `history.skipped_lines` counts them.
- A file without a trailing newline (a line cut short) gets one before the
  new line, so the new line never joins the broken one.
- A file that cannot be read or written is logged at WARNING and recorded in
  `history.read_error` or `history.error`. It never changes the verdict or
  the exit code.

## The trend line

The history is read once, before the review. Once it holds at least 3 valid
lines, the summary gains one line just above its footer:

> 📈 Trend: 5 findings on this PR vs a mean of 2.3 per PR over the last 3 runs (+114%); 4 dropped by quality gates.

- The mean is over the last 20 valid lines (or all of them, when fewer), one
  sample per run. Two runs on the same PR count twice.
- The percentage is left out when the mean is 0.
- The trend line is advisory. It never changes the verdict.
- With `--no-post` nothing is posted, so the text output prints it as
  `history: 📈 Trend: ...`, and the JSON key `history.trend` carries it.

## The regression gate

`PRXREF_FAIL_ON_REGRESSION=on` makes `prxref review` exit `1` when:

```
prior_runs >= 3  and  findings_total > mean * (1 + PRXREF_REGRESSION_TOLERANCE)
```

`mean` and `prior_runs` come from the same last-20 window as the trend line.
With the default tolerance `0.5` and a mean of 2, a PR with 3 findings passes
(limit 3.0) and a PR with 4 fails. The comparison is strict, so 0 findings
against a mean of 0 passes and 1 finding fails.

- With fewer than 3 prior runs the gate never fires
  (`history.regression.reason` is `skipped: N prior run(s), at least 3 needed`).
- A run that is not recorded (see above) is not gated either.
- Under `PRXREF_INCREMENTAL=on` the gate forces a full review, so its count
  is comparable with the history.
- It is independent of `PRXREF_FAIL_ON`, which keeps its meaning. The exit
  code is the higher of the two, and each prints its reason to stderr.
- The outcome is recorded in `history.regression` as
  `{"enabled", "fired", "threshold", "reason"}`.

## Committing the file back

prxref never runs git. Commit the file from your own CI step after the
review, so the next run reads it. A GitHub Actions example for PRs from
branches of the same repository:

```yaml
permissions:
  contents: write

steps:
  - uses: actions/checkout@v4
    with:
      ref: ${{ github.head_ref }}

  - name: Review
    env:
      PRXREF_HISTORY_FILE: .prxref-history.jsonl
      PRXREF_FAIL_ON_REGRESSION: "on"
    run: prxref review --pr-url "${{ github.event.pull_request.html_url }}"

  - name: Commit the review history
    if: always()
    run: |
      git config user.name "prxref-history"
      git config user.email "prxref-history@users.noreply.github.com"
      git add .prxref-history.jsonl
      git diff --cached --quiet || git commit -m "chore: record prxref review"
      git push origin "HEAD:${{ github.head_ref }}"
```

- `ref: ${{ github.head_ref }}` checks out the PR branch itself. Without it,
  `actions/checkout` checks out the PR's merge commit, and the push would put
  that merge commit on the PR branch.
- `if: always()` keeps the line even when the gate fails the review step.
- A PR from a fork cannot push with the default token. Run the commit step
  only for same-repository PRs, or use the dedicated-branch variant below.

Two open PRs that both append to the file conflict when the second merges.
Git's built-in union merge keeps both lines. Add this to `.gitattributes`:

```
.prxref-history.jsonl merge=union
```

Pushing to the PR branch adds a commit per run. To keep PR branches clean,
push the file to a dedicated branch instead and fetch it from that branch
before the review.

## The JSON key

`--format json` always carries `history`: `null` while `PRXREF_HISTORY_FILE`
is empty, otherwise:

| Key | Meaning |
|---|---|
| `file` | The history file. |
| `written` | `true` when this run's line was appended. |
| `reason` | Why it was not appended, `null` when it was. |
| `error` | The write error, `null` when none. |
| `read_error` | The read error, `null` when none. |
| `skipped_lines` | Lines skipped as unreadable. |
| `prior_runs` | How many runs the mean covers. |
| `mean` | The mean `findings_total` over them, `null` with no prior run. |
| `window` | The window size, 20. |
| `trend` | The trend line, `null` without enough history or when the run was not recorded. |
| `regression` | The gate outcome, see above. |
