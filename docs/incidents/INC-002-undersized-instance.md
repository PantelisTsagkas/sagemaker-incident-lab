# INC-002: undersized instance under steady load

**Date:** 2026-10-05  **Fault flag:** `FAULT=none`, `INSTANCE_TYPE=ml.t2.medium`  **Instance:** ml.t2.medium, fixed on ml.m5.large (1 instance, 2 gunicorn workers each)

## Summary
The endpoint was deployed on ml.t2.medium, a smaller and burstable instance, and given 80 requests
per second. It saturated within the first minute. It served about 8.5 requests per second, client
median latency went from about 100 ms to 600 ms to 4 s, the p95 was around 16 s, and 17% of requests
failed, almost all with `ThrottlingException`. The endpoint's own error metrics counted 4 errors over
the same period. A rolling update back to ml.m5.large fixed it with no code change: the same 80
requests per second ran at about 51% CPU and a client median of 100 to 110 ms.

## How the fault works
No code changes. `INSTANCE_TYPE` is read by `deploy.py` and goes into the endpoint config, so the
fault and the fix are both a config change and a rolling update.

ml.t2.medium was the obvious "smaller" choice: it is the smallest general-purpose type in the real-time hosting
instance list, with 2 vCPUs and 4 GiB of memory, against ml.m5.large's 2 vCPUs and 8 GiB. Two things
follow from that:

- **Same vCPU count, so the same workers.** The scikit-learn container ran 2 gunicorn worker
  processes on both. They are gevent workers (gunicorn logs `Using worker: gevent`), so each process
  accepts many connections at once, but CPU-bound work like a predict runs one request at a time
  per process. Shrinking the instance did not change any of this, only how fast each CPU was.
- **t2 is burstable.** It earns CPU credits and spends them when running above its baseline share of
  CPU. When the credits run out, the CPU is throttled. SageMaker does not publish a credit balance
  metric for hosting instances: the `/aws/sagemaker/Endpoints` namespace only has CPUUtilization,
  MemoryUtilization and DiskUtilization. So throttling cannot be seen directly.

## Why 80 requests per second
At the baseline load (10 users, 20 requests per second) the m5.large used about 11% CPU. In this
metric 100% is one full vCPU, so a 2 vCPU instance goes up to 200%. Memory was 6.4%. At that load a
smaller instance would barely notice. The load was raised to 40 users at 2 requests per second each
(80 requests per second, 1 row per request), chosen so the m5.large would sit at moderate CPU. It
did: about 51%.

The load stayed fixed for the whole run. Only `INSTANCE_TYPE` changed. Changing the load is
Incident 3 (traffic spike).

```
uv run locust -f load/locustfile.py --headless -u 40 -r 4 -t 30m --csv results/inc002_t2_u40
```

## Detection
In order of what each signal told us:

1. **Client latency and failures.** Locust median went to several seconds and failures started
   2 minutes in. This says something is wrong, not where.
2. **`CPUUtilization` at 190 to 195% from the first minute** (out of 200%). The instance was out
   of CPU. Memory was 13.3% of 4 GiB, about the same absolute footprint as on the m5.large
   (6.4% of 8 GiB), so memory was not the limit.
3. **`ModelLatency` p50 1.2 s, `OverheadLatency` p50 2.9 ms.** The time was inside the container,
   not in SageMaker's routing.
4. **The stage logs showed the handlers themselves were mostly fast.** Predict p50 was 14 ms.
   So most of the 1.2 s was spent waiting, not working. With gevent workers, a request is accepted
   by a worker straight away, then waits inside it while the worker's single Python thread finishes
   other requests' CPU work. The predict tail (p95 2.5 s) explained why that wait was so long.

```
filter event = "stage_timing" and stage != "model_load"
| stats count(*) as n, pct(ms, 50) as p50, pct(ms, 95) as p95, pct(ms, 99) as p99, max(ms) as mx by stage
```

To check whether the predict tail was rare or common:

```
filter event = "stage_timing" and stage = "predict"
| stats count(*) as n, avg(ms) as mean, sum(ms > 1000) as over1s
```

## Timeline
All times UTC.

- 10:22:40 endpoint created on ml.t2.medium, model loaded on both workers by 10:24:48
- 10:26:57 Locust started, 40 users ramping at 4 per second
- 10:28 first full minute under load: CPU 193%
- 10:30:31 three 502s (nginx: connection reset by gunicorn)
- 10:31:05 first `ThrottlingException` on the client
- 10:33:19 one SageMaker health check (`GET /ping`) also got a connection reset
- 10:40 metrics and stage logs checked: CPU saturated, predict tail found
- 10:42:32 last `ThrottlingException`
- 10:43:39 fix deployed: new endpoint config on ml.m5.large, rolling update
- 10:45:50 one 502 on the t2.medium during the switch
- 10:45:58 m5.large loaded the model on both workers
- 10:46:33 endpoint InService on the new config
- 10:46:42 first request served by the m5.large, 10:47:37 last request served by the t2.medium
- 10:48 Locust reached the full 80 requests per second
- 10:49:35 to 10:50:10 traffic stall, 40 client failures (see Anomalies)
- 10:57 Locust finished, then teardown

## Evidence
Raw Locust output is in `results/inc002_t2_u40_*.csv`. That one run covers the fault, the switch
and the fix. Fault window below is 10:28 to 10:43, fix window 10:51 to 10:57.

**Inside the container**, from `stage_timing` logs:

| Stage | Baseline (m5.large, 20/s) | Fault (t2.medium, 80/s offered) | Fix (m5.large, 80/s) |
|---|---|---|---|
| input p50 / p99 | 0.55 / 2.3 ms | 0.47 / 7.2 ms | 0.63 / 2.0 ms |
| predict p50 | 5.2 ms | **14 ms** | 5.4 ms |
| predict p95 | 8.9 ms | **2,464 ms** | 7.5 ms |
| predict p99 | 14 ms | **2,711 ms** | 9.9 ms |
| predict mean | | **298 ms** | 5.2 ms |
| predicts over 1 s | | **632 of 5,567 (11%)** | 0 of 27,373 |

**SageMaker metrics**, converted from microseconds:

| Metric | Fault | Fix |
|---|---|---|
| Invocations | 7,646 in 15 min (8.5/s) | 4,800 per min (80/s) |
| CPUUtilization (of 200%) | 190 to 195% | 50 to 53% |
| ModelLatency p50 | **1,161 ms** | 10 to 13 ms |
| ModelLatency p95 | **17,368 ms** | 32 to 38 ms |
| ModelLatency p99 | **26,449 ms** | 41 to 63 ms |
| OverheadLatency p50 | 2.9 ms | 1.9 to 2.3 ms |
| Invocation5XXErrors | 3 (plus 1 during the switch) | 0 |
| Invocation4XXErrors | 0 | 0 |

**Client side** (Locust). Up to 10:43, before the fix: 9,788 requests, 1,704 failures (17%),
median 610 ms, p95 16 s. During the fix window: 80 requests per second, median 100 to 110 ms,
no failures. Whole run: 57,230 requests, 1,745 failures.

| Client error | Count | When |
|---|---|---|
| `ThrottlingException` | 1,701 | 10:31:05 to 10:42:32, t2.medium only |
| 502 from the container | 4 | 10:30:31 to 10:45:50, t2.medium only |
| `ConnectionClosedError` | 30 | 10:50:01 to 10:50:17 |
| Read timeout (60 s) | 10 | 10:50:33 to 10:50:34 |

Three things in these numbers are worth calling out:

- **The slow predicts roughly account for the lost throughput.** Each worker process runs one
  predict at a time. Two processes averaging 298 ms per predict can finish about 6 to 7 requests per
  second. Measured throughput was 8.5. The figures do not match exactly (about 5% of stage lines are
  lost to Logs Insights, and the mean is dominated by a few very slow predicts), but they are the
  same size. The problem is the 11% of predicts that took over a second, not the typical request.
- **Locust's offered load dropped too.** Each Locust user waits for its response before sending
  the next request. Once the endpoint slowed, the 40 users sent far fewer than 80 requests per
  second. Real clients that retry on errors would push harder instead, so a real incident would
  likely be worse than this one.
- **The endpoint's error metrics missed most failures.** 1,701 requests were rejected with
  `ThrottlingException`, but `Invocation4XXErrors` was 0 and `Invocation5XXErrors` was 4. Neither the
  per-variant metrics nor the container logs counted these requests, which suggests SageMaker rejected
  them before they reached the instance. An alarm on endpoint errors alone would have stayed quiet through a 17% failure rate.

## Root cause
The instance did not have enough CPU for 80 requests per second. The same load used about a quarter
of the m5.large's CPU, but saturated the t2.medium in under a minute.

The part that is not proven is why individual predicts took 2.5 s on the t2.medium when the same
code took under 10 ms on the m5.large, with the same 2 vCPUs and 2 workers. Slower cores alone do
not explain a gap of more than 200 times. Two explanations fit and the data cannot separate them:

- **CPU credit throttling.** The t2 may have been throttled to its baseline share early on. This
  would also explain why it collapsed in the first minute rather than slowly. There is no credit
  balance metric to confirm it.
- **Thread contention.** `HistGradientBoostingClassifier.predict` uses OpenMP threads, so two
  workers on two vCPUs can compete for the same cores. On a CPU that is saturated or throttled, a
  thread waiting for a core can stall for a long time. The m5.large has the same setup and showed
  no stalls at 51% CPU, so contention would only explain the stalls together with the saturation.

The first guess was "the t2 will run out of credits after some minutes and latency will creep up".
That was wrong in timing: there was no gradual phase. Either the credits ran out almost at once or
the instance was simply too small for this load from the start.

## Fix
Redeployed with `INSTANCE_TYPE=ml.m5.large` as a rolling update while Locust kept running. The two
instances overlapped for about 55 seconds. One 502 came from the t2.medium during the switch.

Verified on the m5.large at the same 80 requests per second: CPU about 51%, predict p99 9.9 ms with
no predict over 100 ms, ModelLatency p50 10 to 13 ms, no throttling and no 5XX.

## Anomalies
**A 35 second traffic stall at 10:49:35, three minutes after the fix.** Locust throughput fell to
almost zero. 30 requests failed with `ConnectionClosedError` and 10 hit the 60 s read timeout,
meaning they were sent at about 10:49:33 and never answered. During the stall the m5.large logged
no requests at all, and the handler times just before and after it were normal (20 ms or less).
SageMaker counted no errors. So the requests were lost before they reached the container: in
SageMaker's routing or on the network between the client and London. Which one is not known. It
looks like the single 24.5 s `ConnectionClosedError` in the baseline. Recorded, not chased.

**The four 502s.** All four were nginx reporting `recv() failed (104: Connection reset by peer)`
from gunicorn, the same message as the single 502 in INC-001. gunicorn logged no timeout and no
worker restart. They only happened while the t2.medium was saturated.

**A failed health check.** At 10:33:19 a SageMaker `GET /ping` got the same connection reset.
SageMaker did not replace the instance. Under heavier or longer overload, failed health checks
could cause SageMaker to replace an instance that is only slow, which would make things worse.

## Prevention
- **Load test an instance type before moving to it.** Run the expected peak load (here 80 per
  second) on the new type, and record its maximum throughput per instance. That number is also
  what autoscaling needs in Incident 3.
- **Avoid burstable instances for steady traffic.** t2 is fine for a demo or a test endpoint. For
  sustained load its real capacity depends on a credit balance that cannot be seen.
- **Alarm on CPUUtilization**, for example average above 150% of 200% for 5 minutes. It was the
  clearest server-side signal in this incident.
- **Alarm on client-side errors, not only endpoint errors.** The endpoint metrics showed 4 errors
  while clients saw 1,701. The calling service needs to publish its own error count, including
  throttling.

## Lesson
A smaller instance does not slow down evenly. Below a certain load it looked fine. Above it,
throughput fell to about a tenth of the offered load, most requests waited in a queue, and the
platform's own error metrics hid most of the failures. The useful numbers were CPU, ModelLatency
against the stage timings, and the client's error count.

## Cost
ml.t2.medium from 10:22 to about 10:47, and ml.m5.large from 10:45 to teardown after 10:57. Well
under $0.10.
