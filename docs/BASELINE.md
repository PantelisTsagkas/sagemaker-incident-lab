# Baseline: healthy endpoint under steady load

**Date:** 2026-10-05  **Fault flag:** `FAULT=none`  **Instance:** ml.m5.large (1 instance, 2 gunicorn workers)
**Client:** laptop in Athens, endpoint in eu-west-2 (London)

Every incident is compared against these numbers. If a later postmortem says "p99 doubled", it
means doubled from here.

## Load profile

Locust, 10 users at a constant 2 requests per second each, so 20 requests per second for
10 minutes. One row per request, sampled from `artifacts/reference.csv`. boto3 retries are
off, so a failure shows up as a failure and not as a slow success.

```bash
uv run locust -f load/locustfile.py --headless -u 10 -r 2 -t 10m --csv results/baseline
```

Raw output: `results/baseline_*.csv`.

## Results

**Client side (Locust):** 11,902 requests, 1 failure (0.01%).

| p50 | p95 | p99 | p99.9 | max |
|---|---|---|---|---|
| 100 ms | 140 ms | 180 ms | 480 ms | 24,497 ms (the failed request) |

**Inside the container** (`stage_timing` logs, CloudWatch Logs Insights):

| Stage | p50 | p95 | p99 |
|---|---|---|---|
| input | 0.55 ms | 1.0 ms | 2.3 ms |
| predict | 5.2 ms | 8.9 ms | 14.0 ms |
| output | 0.02 ms | 0.05 ms | 0.17 ms |

**SageMaker metrics** (CloudWatch, `AWS/SageMaker`, reported in microseconds, shown here in ms).
The range covers the two 10 minute windows of the run:

| Metric | p50 | p95 | p99 |
|---|---|---|---|
| ModelLatency | 7.7 to 12.2 ms | 18 to 31 ms | 23 to 43 ms |
| OverheadLatency | 2.0 to 2.8 ms | 3.4 to 8.5 ms | 6.3 to 13.4 ms |

Endpoint errors over the whole run: `Invocation4XXErrors` 0, `Invocation5XXErrors` 0.
Highest single `ModelLatency`: 96 ms.

Logs Insights query used for the stage table:

```
filter event = "stage_timing" and fault = "none" and stage != "model_load"
| stats count(*) as n, pct(ms, 50) as p50, pct(ms, 95) as p95, pct(ms, 99) as p99 by stage
```

## Where the 100 ms goes

| Layer | p50 | Source |
|---|---|---|
| Our handlers (input + predict + output) | about 6 ms | `stage_timing` logs |
| Container as a whole (adds nginx and gunicorn) | about 8 to 12 ms | ModelLatency |
| SageMaker routing and auth | about 2 to 3 ms | OverheadLatency |
| Network, Athens to London and back | about 85 ms | what is left of the 100 ms |

So at the median, about 85% of the latency a client sees is distance, not the endpoint.
A client in London would see a very different number from the same endpoint.

## Questions this run answered

**The slow first invoke was the client, not the endpoint.** On the first deploy, `invoke` took
1,323 ms while the container took 84 ms. Today, two back to back `invoke` runs took 1,373 ms
and 1,169 ms, even though the second hit an already warm endpoint. Warm Locust users on the same
endpoint saw 100 ms. Each new Python process pays about 1.1 seconds to load credentials and open a
TLS connection. Locust shows the same effect: its highest latency during ramp up (1,128 ms)
came from users making their first call.

**The 80 ms `predict` seen in smoke tests is warm-up.** Each gunicorn worker's first prediction
took 81 to 90 ms. Under load the p50 is 5.2 ms, and the slowest predict of the whole run
(90 ms) was one of those first calls.

**INC-000 is fixed.** Both workers logged `model_load` with no `InconsistentVersionWarning`.

## Anomalies

**One request failed after 24.5 seconds** with `ConnectionClosedError` at 08:55:21 UTC. Locust's
history file shows the max response time jumping to 24,497 ms at the same second, so it is one
event, not two. The endpoint never saw a problem: zero 4XX or 5XX errors, and no `ModelLatency`
above 96 ms. The connection dropped somewhere between the laptop and AWS. With 1 in 11,902
requests affected, it is recorded here and not chased further.

**Logs Insights undercounts per-stage lines.** Locust sent 11,902 requests, but the stage counts
were 11,222 (input), 11,385 (predict) and 11,781 (output). About 600 CloudWatch log events
contained more than one `stage_timing` line, because stdout lines written close together can be
grouped into one event, and Logs Insights only parses the first JSON object in a message. The
percentiles come from about 95% of requests, which is enough for comparison, but per-stage counts
should not be read as request counts. A possible fix is to log all stages of one request as a
single JSON line.

## Cost

Endpoint up for about 25 minutes on ml.m5.large, so roughly $0.06.
