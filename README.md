# SageMaker Incident Lab

**Status: ACTIVE**

Deploy a small model to a SageMaker real-time endpoint, then break it on purpose,
diagnose it from metrics and logs, fix it, and write the postmortem.

The model (a Kepler exoplanet classifier) is the patient, not the star.

## Setup (once)

1. An S3 bucket for model artifacts.
2. A SageMaker execution role (trusted by `sagemaker.amazonaws.com`) that can read that
   bucket, pull ECR images and write CloudWatch Logs. For the lab, the managed
   `AmazonSageMakerFullAccess` policy works; tightening it is a good later exercise.
3. An AWS Budgets alert at a low monthly threshold. Do this before the first deploy.

```bash
uv sync
export AWS_REGION=eu-west-2
export SAGEMAKER_ROLE_ARN=arn:aws:iam::<account>:role/<role>
export INCIDENT_LAB_BUCKET=<bucket>
# optional: ENDPOINT_NAME, INSTANCE_TYPE (default ml.m5.large), TTL_HOURS, FAULT
```

## Run

```bash
uv run python -m incident_lab.train      # downloads KOI data, trains, writes artifacts/
uv run python -m incident_lab.package    # builds artifacts/model.tar.gz
uv run python -m incident_lab.deploy     # 5 to 10 minutes; endpoint starts billing
uv run python -m incident_lab.invoke     # smoke test
uv run python -m incident_lab.teardown   # ALWAYS, at the end of every session (also removes autoscaling)
uv run pytest
```

## Load tests and autoscaling

```bash
uv run locust -f load/locustfile.py --headless -u 10 -r 2 -t 10m --csv results/baseline
uv run locust -f load/ramp.py  --headless --processes 4 --csv results/<name>   # staircase, finds one instance's limit
uv run locust -f load/spike.py --headless --processes 4 --csv results/<name>   # steady, sudden jump, steady
SCALE_TARGET_RPS=100 SCALE_MAX_INSTANCES=3 uv run python -m incident_lab.autoscale apply
uv run python -m incident_lab.autoscale status
uv run python -m incident_lab.metrics 09:13 09:32 results/<name>_cloudwatch.csv   # UTC
```

Each Locust user sends up to 2 requests per second. The autoscaling target is per second per
instance and is converted to the per-minute value the metric uses.

## Where the time goes

Every handler in `serving/inference.py` logs one JSON line per stage. In CloudWatch
Logs Insights, pick the log group `/aws/sagemaker/Endpoints/<endpoint-name>` and run:

```
fields @timestamp, stage, ms, fault
| filter event = "stage_timing"
| stats count() as n, pct(ms, 50) as p50, pct(ms, 95) as p95, pct(ms, 99) as p99 by stage
```

Compare with the endpoint metrics in the `AWS/SageMaker` namespace:
`ModelLatency` (inside the container) and `OverheadLatency` (SageMaker's plumbing).
Both are reported in **microseconds**.

## Design decisions

- **SDK V3, low-level resources.** `sagemaker.core` Model / EndpointConfig / Endpoint
  instead of `ModelBuilder`, so every layer is explicit. Depends on `sagemaker-core`
  only; the full `sagemaker` package pulls in torch (~6 GB venv vs ~350 MB).
- **boto3 for invoke and teardown.** Invoke matches how real clients call the endpoint.
  Teardown must never break, so it uses the most stable API and sweeps leftovers.
- **Version pinning.** Local scikit-learn must match the serving container
  (`SKLEARN_CONTAINER_VERSION`); `train` refuses to run if they differ.
- **Faults are config, not code you edit.** `FAULT` is an env var on the SageMaker
  Model. Changing it means a new endpoint config and a rolling update, exactly how
  config changes reach production.

## Incidents

| # | Incident | Status |
|---|----------|--------|
| 0 | Baseline under load (Locust) | done: [BASELINE](docs/BASELINE.md) |
| 1 | Slow preprocessing | done: [INC-001](docs/incidents/INC-001-slow-preprocessing.md) |
| 2 | Undersized instance | done: [INC-002](docs/incidents/INC-002-undersized-instance.md) |
| 3 | Traffic spike and autoscaling lag | done: [INC-003](docs/incidents/INC-003-traffic-spike.md) |
| 4 | Cold start (serverless variant) | todo |
| 5 | Memory leak | todo |
| 6 | Schema break | todo |
| 7 | Silent drift (data capture + Evidently) | todo |

Each one gets a postmortem in `docs/incidents/` from `TEMPLATE.md`.
