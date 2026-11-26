# Prometheus metrics

`prxref serve` and `prxref gate serve` can expose Prometheus metrics. It is
off by default.

## Enable

Set `PRXREF_METRICS_PORT` (env-only, `0` to `65535`, default `0` = off) in the
daemon's environment. For `gate serve` it can sit in the `--env-file`. The
daemon then also serves `GET /metrics` on its own listener, apart from the
webhook port:

```sh
PRXREF_METRICS_PORT=9464 prxref serve --port 8080
curl -s http://127.0.0.1:9464/metrics
```

The listener binds **`127.0.0.1` only**, and there is no setting to change
that. Scrape it from the same host (a local Prometheus or agent), or put your
own authenticating proxy in front of it. Any path other than `/metrics`
answers 404. A bad value (not an integer, or above 65535) is a configuration
error and exits 2.

The AWS Lambda gate (`src/prxref/gate/aws.py`) serves no `/metrics`: a
Lambda has no long-lived process to scrape.

Counters live in the daemon's memory and restart at 0 with it. Recording
costs a lock and a few additions per review, so it happens whether or not the
listener is on.

## Metric list

| Name | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `prxref_reviews_total` | counter | `verdict` (`Approved`, `Request-Changes`, `Error`, `other`) | Finished reviews. |
| `prxref_findings_posted_total` | counter | `severity` (`error`, `warning`, `spec`, `outofscope`, `other`) | Active findings of finished reviews. |
| `prxref_findings_dropped_total` | counter | `gate` | Findings dropped by a quality pass, labelled with the pass that wrote the `drop_reason` (for example `apply_hedge_gate`, `apply_quality_gate`, `apply_thread_dedup`, `apply_sweep_dedup`); `other` when no known reason matches. The reasons are tabulated in [quality.md](quality.md#drop-reasons). |
| `prxref_llm_cost_usd_total` | counter | `model` | Dollar cost of reviews with a known cost. `model` is the run's configured model chain, comma-joined. A run's result carries one cost for the whole run, so a fallback chain is not split per model. |
| `prxref_llm_cost_unknown_reviews_total` | counter | none | Reviews whose cost no source could price (never counted as 0). |
| `prxref_llm_tokens_total` | counter | `direction` (`input`, `output`) | LLM tokens of finished reviews. |
| `prxref_review_duration_seconds` | histogram | `le` | Wall time of one review, buckets 5 s to 1800 s. |
| `prxref_review_errors_total` | counter | none | Reviews that ended with the `Error` verdict. |

No label carries a PR URL, a repository, an owner or a file path, so
cardinality stays bounded by the verdicts, severities, quality passes and
your configured model chain.

## Example Grafana panel

A time series of reviews per verdict, to paste into a dashboard's JSON model
(replace `${DS_PROMETHEUS}` with your data source):

```json
{
  "type": "timeseries",
  "title": "prxref reviews per hour by verdict",
  "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
  "gridPos": { "h": 8, "w": 12, "x": 0, "y": 0 },
  "fieldConfig": { "defaults": { "unit": "short" }, "overrides": [] },
  "targets": [
    {
      "refId": "A",
      "expr": "sum by (verdict) (rate(prxref_reviews_total[1h])) * 3600",
      "legendFormat": "{{verdict}}"
    }
  ]
}
```

Other queries worth a panel:

- Findings dropped per gate: `sum by (gate) (increase(prxref_findings_dropped_total[1d]))`
- p95 review time: `histogram_quantile(0.95, sum by (le) (rate(prxref_review_duration_seconds_bucket[1h])))`
- Spend per day: `sum by (model) (increase(prxref_llm_cost_usd_total[1d]))`
