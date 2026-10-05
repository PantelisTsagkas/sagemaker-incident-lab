# INC-001: slow preprocessing on batch requests

**Date:** 2026-10-05  **Fault flag:** `FAULT=slow_preprocess`  **Instance:** ml.m5.large (1 instance, 2 gunicorn workers)

## Summary
A change to `input_fn` built the model's input table one row at a time instead of all at once.
Predictions stayed correct, but time spent inside the container rose about 5 times at the
median and 5 to 10 times at p99 for requests of 50 rows. Single-row requests were almost
unaffected, so a one-row smoke test would have passed. During the incident there was one 502
in about 28,000 requests. The fix was rolled out with no downtime.

## How the fault works
`serving/inference.py` turns the JSON request into a pandas DataFrame before predicting. The
healthy path does it in one call:

```python
pd.DataFrame(instances, columns=FEATURES).astype("float64")
```

The faulty path grows the frame one row at a time, a common pandas anti-pattern:

```python
frame = pd.DataFrame(columns=FEATURES)
for row in instances:
    frame.loc[len(frame)] = [row[f] for f in FEATURES]
```

Each `.loc` append makes pandas copy and re-check the frame, so the cost grows with the number of
rows. Both paths return an identical frame. A test (`test_slow_preprocess_returns_same_frame_as_healthy`)
proves it, so the fault changes speed and nothing else.

In a real team the bad code would be a commit and the fix another commit. Here both paths live
in the file behind the `FAULT` switch, so the incident can be turned on and off with a config change.

## Why batches
The baseline sent 1 row per request. Timed locally, the slow path costs about 0.1 ms per row:

| Rows per request | Healthy | Slow |
|---|---|---|
| 1 | 0.09 ms | 0.18 ms |
| 10 | 0.10 ms | 1.3 ms |
| 50 | 0.16 ms | 5.4 ms |
| 100 | 0.20 ms | 10.6 ms |

At 1 row the fault is invisible, so Locust gained a `BATCH_SIZE` env var (default 1, so the
baseline command is unchanged). 50 rows was chosen to stay well under the point where the two
workers saturate: at 20 requests per second, each request has about 100 ms of worker time.

## Detection
In this lab the fault was injected on purpose, so detection was not a surprise. The useful part is
the order in which the signals narrow the problem down:

1. **Client latency rose:** p50 100 ms to 150 ms, p99 180 ms to 390 ms. This alone does not
   say where the time went. About 85 ms of every request is network.
2. **`ModelLatency` rose, `OverheadLatency` did not.** ModelLatency p50 went from about 10 ms to
   54 ms. OverheadLatency stayed at about 3 ms. So the time was spent inside our container, not
   in SageMaker's routing.
3. **The per-stage logs pointed at one stage.** Input p50 went from 0.55 ms to 34 ms. Predict and
   output did not move.

```
filter event = "stage_timing" and stage != "model_load"
| stats count(*) as n, pct(ms, 50) as p50, pct(ms, 95) as p95, pct(ms, 99) as p99 by fault, stage
```

Grouping by `fault` keeps healthy and faulty lines apart when one query window covers both.

## Timeline
All times UTC.

- 09:16 endpoint created with `FAULT=slow_preprocess`, InService at 09:19
- 09:19 Locust started, 50 rows per request, 20 requests per second, 10 minutes
- 09:20 first check of the stage logs: input p50 46 ms over the first 80 requests
- 09:30 to 09:33 Locust at 1 row per request, fault still on, to check the fault hides at small sizes
- 09:33 Locust restarted at 50 rows, left running through the fix
- 09:34:39 fix deployed: new endpoint config with `FAULT=none`, rolling update
- 09:35:58 one 502 from the faulty instance (see Anomalies)
- 09:36:53 replacement instance loaded the model on both workers
- 09:37:26 deploy reported InService on the new config (`check_update_landed` passed)
- 09:37:28 first request served by the fixed instance
- 09:37:41 last request served by the faulty instance
- 09:48 Locust finished, 09:50 teardown

## Evidence
All runs used 10 Locust users at 2 requests per second each. Raw output is in `results/inc001_*.csv`.

**Inside the container**, from `stage_timing` logs:

| Stage | Baseline (1 row) | Fault (1 row) | Fault (50 rows) | Fixed (50 rows) |
|---|---|---|---|---|
| input p50 | 0.55 ms | 1.5 ms | **34 ms** | 0.88 ms |
| input p95 | 1.0 ms | 3.0 ms | **51 ms** | 1.6 ms |
| input p99 | 2.3 ms | 3.9 ms | **86 ms** | 2.9 ms |
| predict p50 | 5.2 ms | 4.0 ms | 4.5 ms | 4.0 ms |

**SageMaker metrics**, `AWS/SageMaker`, converted from microseconds:

| Metric | Baseline (1 row) | Fault (50 rows) | Fixed (50 rows) |
|---|---|---|---|
| ModelLatency p50 | 8 to 12 ms | **54 ms** | 10 ms |
| ModelLatency p95 | 18 to 31 ms | **123 ms** | 35 ms |
| ModelLatency p99 | 23 to 43 ms | **226 ms** | 87 ms |
| OverheadLatency p50 | 2.0 to 2.8 ms | 3.0 ms | 2.6 ms |

**Client side** (Locust), fault at 50 rows: 11,251 requests, p50 150 ms, p95 250 ms, p99 390 ms.
At 1 row with the fault on, p50 was 100 ms, the same as the baseline.

Two things in these numbers are worth calling out:

- **ModelLatency p99 (226 ms) is far above input plus predict p99 (about 100 ms).** ModelLatency
  includes time a request waits in nginx and gunicorn for a free worker. With each request holding a
  worker for about 40 ms at 20 requests per second, the two workers were busy about 40% of the time.
  Bursts then queue, so the tail grows faster than the median. The slow code costs more than its own
  runtime under load.
- **Fixed ModelLatency p99 (87 ms) is still higher than at baseline.** The fix run sent 50 rows, so
  predict and JSON work per request are larger than in the 1-row baseline. These two runs are not
  directly comparable, which is why the input stage is the number to compare.

## Root cause
Building a DataFrame by appending rows with `.loc` inside a Python loop. The cost grows with every
row, while the vectorised constructor costs almost the same for 1 row or 100. The bug was invisible
in the existing tests and smoke test because those send very few rows.

## Fix
Redeployed with `FAULT=none`, which brings back the vectorised constructor. This was the first real
run of the `update_endpoint` path in `deploy.py`. SageMaker started a new instance, moved traffic
to it, and removed the old one. Both instances served traffic for about 13 seconds, and no request
failed during the switch. `check_update_landed` confirmed the endpoint was on the new config and
not rolled back.

Verified: input p50 back to 0.88 ms at 50 rows, and ModelLatency p50 back to 10 ms.

## Anomalies
**One 502 at 09:35:58, on the faulty instance.** nginx logged
`recv() failed (104: Connection reset by peer) while reading response header from upstream`
for a request to gunicorn, and `Invocation5XXErrors` shows 1 in that minute. gunicorn logged no
timeout and no worker restart, and that request's input stage took a normal 63 ms. The cause is not
proven. It happened while the fault was on and the workers were queueing, and nothing like it appeared
in the baseline or after the fix. One error in about 28,000 requests, recorded but not chased.

**Client connection drops.** The 10 minute fault run had 3 `ConnectionClosedError`s within
4 seconds, with zero 5XX on the endpoint. It is the same network blip as the one in the baseline,
not the fault.

**Client latency is noisy.** At the start of the fix run, with the fault still on, client p50 was
about 280 ms, against 150 ms in the first fault run. The server numbers did not change, so this was
the network between Athens and London. Client-side numbers are reported but not used to judge the fix.

## Prevention
- **Test with the shape of real traffic.** A smoke test or CI check should send a batch, not one row.
  A cheap version is a Pytest benchmark that times `input_fn` at 50 rows and fails above a threshold.
  This would have caught the bug before deploy.
- **Alarm on ModelLatency p99**, not the average. The median rose about 5 times, but the p99 rose
  5 to 10 times because of queueing. Averages would hide that.
- **Keep the per-stage timing logs.** Without them, ModelLatency only says "inside the container".
  The stage logs turned that into "the input stage" in one query.

## Lesson
A bug that scales with input size hides in small tests. The 1-row smoke test and the 1-row load test
both looked healthy while the bug was live. And a slow step costs more than its own runtime under
load, because requests wait behind it.

## Cost
Endpoint up from 09:16 to 09:50, about 34 minutes on ml.m5.large, plus a few minutes of a second
instance during the rolling update. Roughly $0.08.
