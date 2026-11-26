# Run dashboard

`prxref serve` and `prxref gate serve` can show a read-only page of their
recent reviews. It is off by default.

This is not `prxref eval dashboard`, which watches one eval campaign
directory (see [evals.md](evals.md)).

## Enable

Set `PRXREF_DASHBOARD_PORT` (env-only, `0` to `65535`, default `0` = off) in
the daemon's environment. For `gate serve` it can sit in the `--env-file`.
The daemon then opens one more listener, apart from the webhook port and the
[metrics](metrics.md) port:

```sh
PRXREF_DASHBOARD_PORT=9470 prxref serve --port 8080
curl -s http://127.0.0.1:9470/runs.json
```

Open `http://127.0.0.1:9470/` in a browser on the same host for the page.

The listener binds **`127.0.0.1` only**, and there is no setting to change
that. The page shows repository names, PR links and model names, so if you
put a proxy in front of it to reach it from elsewhere, that proxy must
authenticate. A bad value (not an integer, or above 65535) is a configuration
error and exits 2.

The AWS Lambda gate (`src/prxref/gate/aws.py`) serves no dashboard: a Lambda
has no long-lived process to hold a listener.

## What it serves

| Request | Answer |
| --- | --- |
| `GET /` | One self-contained HTML page: no script, no remote asset, no form. |
| `GET /runs.json` | The same rows as a JSON list, newest first. |
| any other path | 404 |
| `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS` | 405 |

There is no write surface: nothing on the page or in the API changes a run,
a setting or the queue.

Each row is one finished review, newest first, at most 50:

| Field | Meaning |
| --- | --- |
| `finished_at` | When the review finished, as Unix seconds. |
| `repo`, `pr_number`, `pr_url` | The PR reviewed. The page links the PR URL only when it is `http` or `https`. |
| `head_sha` | The head commit reviewed (the page shows 7 characters). The gate takes it from its queue; `prxref serve` takes the run record's `reviewed_head_sha`, the PR head the diff was fetched at. It is `null` when the run failed before the diff was fetched, or the forge gave no head (a local diff file). |
| `verdict` | `Approved`, `Request-Changes` or `Error`. |
| `findings` | Active (posted) findings by severity: `error`, `warning`, `spec`, `outofscope`, and `other` for anything else. Dropped findings are not counted. |
| `cost_usd`, `cost_estimated` | The run's cost in USD, `null` (shown as "unknown") when no source could price it. `cost_estimated` marks an estimate. |
| `input_tokens`, `output_tokens` | LLM tokens of the run. |
| `models` | The models that answered (the run record's `cost_by_model`), else the configured fallback chain. |
| `elapsed_ms` | Wall time of the review. |
| `source` | `memory` or `runs_dir`, as below. |

Every value on the page is HTML-escaped, and the page is sent with a
`Content-Security-Policy` that allows no script.

## Where the rows come from

The dashboard adds no persistence of its own.

- **`prxref gate serve`** reads the run records it already writes:
  `<PRXREF_GATE_RUNS_DIR>/<generation id>/result.json`, plus
  `run_record.json` for the models. It reads them on each request, the
  newest 50 by modification time, and takes the repository, PR number, head
  SHA and finish time from the gate's state file for that generation. A
  malformed, partial or unreadable record is skipped, never an error page.
- **`prxref serve`** keeps no run records on disk, so it keeps the last 50
  summarized rows in memory, recorded at the end of each review. They are
  lost when the daemon restarts, and the list starts empty.
