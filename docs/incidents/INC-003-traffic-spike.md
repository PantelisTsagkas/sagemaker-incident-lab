# INC-003: traffic spike on a single instance

**Date:** 2026-10-11  **Fault flag:** `FAULT=none`, load jumps from 80 to 320 requests per second  **Instance:** ml.m5.large, 1 instance, fixed with target-tracking autoscaling (1 to 3 instances)

## Summary
Traffic jumped from 80 to 320 requests per second in about 2 seconds. One ml.m5.large can serve
about 200 per second, so SageMaker rejected the rest with `ThrottlingException`: about 35% of
requests failed for as long as the spike lasted. The endpoint's own error metrics counted almost
none of these failures. With target-tracking autoscaling turned on, the same spike still caused
failures for **7 minutes 55 seconds**, until a second instance came into service. After that,
failures stayed at zero for the rest of the spike. Most of the delay was detection (about 5
minutes), not starting instances (about 3 minutes).

## How the fault works
No code change. The load pattern is the fault. `load/spike.py` is a Locust shape: 40 users
(80 requests per second) for 3 minutes, then 160 users (320 per second) added at 50 users per
second, then back to 40 users. Each user sends at most 2 requests per second, 1 row each, with
no client retries.

320 per second was chosen from INC-002: 80 per second used about a quarter of an m5.large's CPU,
so 4 times that was expected to go past one instance's limit.

The spike and the staircase live in their own files so the baseline command
(`load/locustfile.py` with `-u/-r/-t`) still runs exactly as before. All runs used
`--processes 4` so the laptop could generate the load. It could: in the last staircase step it
offered 399 requests per second.

## Step 1: find one instance's limit
INC-002 said autoscaling needs the maximum throughput of one instance, so that was measured first
with a staircase (`load/ramp.py`): start at 80 requests per second and add 40 every 2 minutes, up
to 400.

```
uv run locust -f load/ramp.py --headless --processes 4 --csv results/inc003_ramp
```

| Offered (per second) | Served (per second) | Failed | CPU (of 200%) |
|---|---|---|---|
| 80 | 80 | 0% | 58 to 61% |
| 120 | 119 | 0% | 91 to 97% |
| 160 | 158 | 2.3% | 127 to 131% |
| 200 | 187 | 5.1% | 155% |
| 240 | 188 | 19% | 143 to 157% |
| 280 | 195 | 25% | 154 to 157% |
| 320 | 205 | 35% | 157% |
| 360 | 207 | 42% | 157% |
| 400 | 200 | 50% | 157% |

So one m5.large serves about **200 requests per second**, and rejections start at about 160.
Above the limit, served traffic stays flat and every extra request is rejected. Almost all
failures (69,631 of 69,675) were `ThrottlingException`.

Two things here were not expected:

- **CPU levelled off at about 157%, not 200%.** SageMaker started rejecting requests before the
  instance ran out of CPU. The handlers stayed fast the whole time (predict p50 about 7 ms in
  every step). `ConcurrentRequestsPerModel` peaked at about 100. So something limits how many
  requests SageMaker lets into one instance at once, and it is not the CPU. What exactly sets that
  limit is not known.
- **Latency did not explode.** Rejected requests fail fast, so client latency only went from about
  200 ms to about 300 ms at the median. In this incident the symptom is errors, not slowness.

Raw data: `results/inc003_ramp_*.csv`, `results/inc003_ramp_cloudwatch.csv`.

## Step 2: the incident (Run A, no autoscaling)
```
SPIKE_BEFORE_SECONDS=180 SPIKE_PEAK_SECONDS=600 SPIKE_AFTER_SECONDS=180 \
  uv run locust -f load/spike.py --headless --processes 4 --csv results/inc003_spike_noscale
```

The spike was held for 10 minutes rather than the default 15, because the staircase had already
shown what 320 per second does to one instance.

| Signal | Before (80/s) | During spike (320/s offered) | After (80/s) |
|---|---|---|---|
| Client failure rate | 0% | 25 to 37% each minute | 0% |
| Served per second | 80 | about 200 | 80 |
| CPU (of 200%) | 55 to 57% | 148 to 158% | 51 to 55% |
| ModelLatency p50 | 10 to 11 ms | 335 to 549 ms | 16 ms |
| Invocation5XXErrors per minute | 0 | 1 to 12 | 0 |
| Invocation4XXErrors | 0 | 0 | 0 |

The whole run had 210,430 requests and 61,308 failures: 61,254 `ThrottlingException`, 53 502s
from the container and 1 connection reset. It recovered the moment the spike ended. Nothing was
left broken, the instance was just too small for that minute.

Raw data: `results/inc003_spike_noscale_*.csv`, `results/inc003_spike_noscale_cloudwatch.csv`.

## Step 3: the fix (Run B, autoscaling on)
Autoscaling was added to the live endpoint with `incident_lab/autoscale.py` (boto3, Application
Auto Scaling):

```
SCALE_TARGET_RPS=100 SCALE_MAX_INSTANCES=3 uv run python -m incident_lab.autoscale apply
```

- **Minimum 1 instance, maximum 3.** The maximum is the cost cap.
- **Target-tracking policy on `SageMakerVariantInvocationsPerInstance`, target 100 requests per
  second per instance.** That is half the measured limit, the usual safety margin. The metric is
  per minute, so the target value sent to AWS is 6,000. A per-second value there would mean
  the policy scales out at 100 requests per minute.
- **Cooldowns at the AWS defaults for SageMaker:** 300 seconds for scale-out and scale-in.

AWS then created two CloudWatch alarms for the policy:

| Alarm | Rule |
|---|---|
| AlarmHigh (scale out) | InvocationsPerInstance above 6,000 for 3 of 3 one-minute periods |
| AlarmLow (scale in) | InvocationsPerInstance below 5,400 (90% of the target) for 15 of 15 one-minute periods |

Then the same spike, held for 20 minutes, with 22 minutes of normal traffic afterwards to watch
scale-in:

```
SPIKE_BEFORE_SECONDS=180 SPIKE_PEAK_SECONDS=1200 SPIKE_AFTER_SECONDS=1320 \
  uv run locust -f load/spike.py --headless --processes 4 --csv results/inc003_spike_autoscale
```

## Timeline (Run B)
All times UTC.

- 09:49:24 autoscaling applied, 1 instance
- 09:52:36 spike starts, 160 users by 09:52:38. Throttling starts at once.
- 09:52 to 09:54 InvocationsPerInstance above 6,000 for three minutes in a row
- 09:57:36 AlarmHigh goes to ALARM. The scaling activity "set desired instance count to 2"
  starts in the same second.
- 09:59:46 the new instance loads the model (first `model_load` in its log stream)
- 10:00:31 scaling activity done. **Throttling stops in the same second.** The last batch of
  failures was at 10:00:28.
- 10:00:36 AlarmHigh is still in ALARM, so a second activity starts: "set desired instance count
  to 3"
- 10:02:56 third instance loads the model
- 10:03:21 three instances in service
- 10:12:36 spike ends, back to 40 users
- 10:14:36 AlarmHigh back to OK
- 10:28:54 AlarmLow goes to ALARM, after 15 quiet minutes
- 10:30:55 to 10:31:25 scale-in from 3 to 2 instances
- 10:34:36 Locust stops, 10:36:53 teardown

## Where the 7 minutes 55 seconds went

| Stage | From | To | Time |
|---|---|---|---|
| Waiting for 3 one-minute datapoints, plus CloudWatch delay | spike 09:52:36 | alarm 09:57:36 | 5 min 0 s |
| Alarm to scaling activity | 09:57:36 | 09:57:36 | 0 s |
| Instance starts and loads the model | 09:57:36 | model loaded 09:59:46 | 2 min 10 s |
| Health checks, then SageMaker starts sending it traffic | 09:59:46 | 10:00:31 | 45 s |

So **about 63% of the outage was detection**. The alarm cannot fire until it has 3 full minutes
of data above the target, and SageMaker metrics reach CloudWatch with a delay of about a minute
or two. Starting an instance was the smaller part. A new instance is not useful until SageMaker
puts it into routing, and that happened 45 seconds after its model loaded.

## Evidence (Run B)
From `results/inc003_spike_autoscale_cloudwatch.csv` and the Locust history:

| Phase | Instances | Served per second | Client failures | CPU average (of 200%) | ModelLatency p50 |
|---|---|---|---|---|---|
| Before spike | 1 | 80 | 0 | 44 to 56% | 10 to 12 ms |
| Spike, 1 instance (09:53 to 09:59) | 1 | 170 to 206 | 30 to 36% | 105 to 158% | 265 to 371 ms |
| Spike, 2 instances (10:01 to 10:02) | 2 | 320 | 10 in 2 minutes | 101 to 102% | 15 to 18 ms |
| Spike, 3 instances (10:04 to 10:11) | 3 | 320 | 5 in 8 minutes | 64 to 67% | 11 to 12 ms |
| After spike | 3, then 2 | 80 | 4 in 22 minutes | 11 to 16% | 6 to 8 ms |

Whole run: 483,260 requests, 46,341 failures. 46,322 of them happened between 09:52:36 and
10:00:31, the time on one instance. The rest were single 502s or a connection reset spread over
half an hour, like the ones seen in INC-001 and INC-002.

With 3 instances, InvocationsPerInstance settled at about 6,400 per minute, just above the target
of 6,000: the policy was doing what it was set up to do.

Scaling activity log: `results/inc003_scaling_activities.txt`. Alarm settings and history, and
when each new instance loaded the model: `results/inc003_alarms.txt` (saved before teardown,
because deregistering the target deletes the alarms).

## Root cause
One instance has a fixed limit, about 200 requests per second for this model on ml.m5.large.
A spike above that limit gets rejected at SageMaker's front door. With no autoscaling there was
no way to add capacity, so the rejections lasted as long as the spike.

**The scale-out happened in two rounds, as predicted before the run.** Target tracking works out
how many instances it needs from the metric: current instances x metric / target. The metric
counts requests the instances *served*, not requests clients *sent*. A full instance serves its
200 per second and the rest are rejected and never counted. So AWS saw 12,000 per minute against
a target of 6,000 and asked for 2 instances, when the real 320 per second needed 3 at the target.
Only once 2 instances were serving could the metric show the real demand. This time it did not
make the outage longer, because 2 instances were already enough to stop the rejections. With a
bigger spike, the first round would not have been enough, and every extra round adds another
2 to 3 minutes of failures.

**The scale-out cooldown did not delay the second round.** The second activity started 5 seconds
after the first finished, even though the scale-out cooldown was 300 seconds. That matches the
Application Auto Scaling docs: during a scale-out cooldown the policy will not scale out again
"unless a larger scale out is triggered", and the capacity just added counts towards the next
calculation. Two instances serving 320 per second worked out to 3.2 instances needed (capped at
3), which was larger, so it went ahead.

## Fix
Target-tracking autoscaling, 1 to 3 instances, target 100 requests per second per instance.
Verified with the same spike: failures stopped at 10:00:31 and stayed near zero for the
remaining 12 minutes of the spike, at 320 requests per second, with ModelLatency back to
11 to 12 ms.

Autoscaling does not prevent the start of a spike. It limits how long it hurts. On this setup
that was about 8 minutes.

## Anomalies
**Slow requests just after the deploy.** In the first minutes of the staircase, at 80 requests
per second, ModelLatency p50 was 52 to 94 ms. Later, in Run A and Run B, the same 80 per second
ran at 10 to 12 ms, and in INC-002 at 10 to 13 ms. Predict stayed at about 7 ms the whole time,
so requests were waiting, not working. The staircase started 30 seconds after the endpoint went
InService. A new instance warming up is the likely cause, but this was not proven. Recorded,
not chased.

**Scale-in was slow and only one step.** It took 18 minutes from the end of the spike to remove
the first instance (15 quiet minutes for AlarmLow, then about 2 minutes). Then it removed only
one, from 3 to 2. The run ended before a second scale-in could happen, and the endpoint was torn
down at 2 instances. This is deliberate on AWS's side. For cost it means a short spike pays for
extra instances for at least 20 minutes.

## Prevention
- **Load test for the limit of one instance, and set the target from it.** Here: limit about
  200 per second, target 100. Without the staircase, the target would have been a guess, and a
  target above the limit would never trigger, because the metric caps at the limit.
- **Faster detection.** SageMaker has a high-resolution metric, `ConcurrentRequestsPerModel`,
  published every 10 seconds instead of every minute, and a predefined target-tracking type for
  it (`SageMakerVariantConcurrentRequestsPerModelHighResolution`). Detection was 63% of this
  outage, so this is the biggest lever. It was not tested here.
- **Keep spare capacity for known peaks.** If spikes are predictable (a launch, a marketing email),
  raise the minimum instance count ahead of time with scheduled scaling. Autoscaling alone always
  leaves a gap of several minutes.
- **Alarm on client-side errors.** For the third time in this lab (INC-002, the staircase, Run A),
  SageMaker's `Invocation4XXErrors` and `Invocation5XXErrors` missed almost all failures:
  tens of thousands of `ThrottlingException` against at most a dozen 5XX per minute. The calling
  service has to count its own errors, throttling included.
- **Teardown must remove autoscaling.** AWS says to deregister the scalable target before deleting
  the endpoint. Deregistering also deletes the policy and its alarms. `teardown.py` now does this
  first and checks nothing is left.

## Lesson
Autoscaling is not instant. Most of the gap is not starting instances but noticing the spike:
3 minutes of data, plus the metric delay. And the metric it watches only counts what the
instances managed to serve, so a full instance hides how much demand there really is. The useful
number was one instance's measured limit. It sets the target, it shows why the scale-out took two
rounds, and it tells you how big a spike the minimum capacity can absorb with no help.

## Cost
About 2.6 instance-hours of ml.m5.large: one instance from 09:10 to 10:37, a second from about
09:58 and a third from 10:00 to 10:31. Under $0.50.
