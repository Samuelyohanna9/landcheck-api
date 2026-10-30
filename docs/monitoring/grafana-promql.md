# LandCheck API PromQL panels

These queries assume the `landcheck-api` scrape job from
`prometheus-landcheck.yml` and the metric names emitted by the API.

## Request rate

```promql
sum(rate(landcheck_http_requests_total[5m]))
```

## Error rate

```promql
sum(rate(landcheck_http_requests_total{status=~"4..|5.."}[5m]))
/
clamp_min(sum(rate(landcheck_http_requests_total[5m])), 1)
```

## p95 latency

```promql
histogram_quantile(
  0.95,
  sum by (le) (rate(landcheck_http_request_duration_seconds_bucket[5m]))
)
```

## p99 latency

```promql
histogram_quantile(
  0.99,
  sum by (le) (rate(landcheck_http_request_duration_seconds_bucket[5m]))
)
```

For route-level panels, add `route` to the aggregation, for example:
`sum by (le, route) (...)`.
