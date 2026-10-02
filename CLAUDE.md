SageMaker Incident Lab: project brief
What we are building

A portfolio project that proves ML troubleshooting skills, not modelling skills. We deploy a small scikit-learn model to a SageMaker real-time endpoint, then break it on purpose, one incident at a time. For each incident: inject the fault, detect it from metrics and logs, find the root cause, fix it, and write a short postmortem.

The model (a Kepler exoplanet classifier on the NASA KOI catalogue) is the patient, not the star. Target audience: hiring managers for DevOps, Cloud and MLOps roles.

Current state

First deploy verified end to end on 2026-10-02 (InService in about 3 minutes, smoke test correct). 11 Pytest tests pass. Infra (artifacts bucket, least-privilege execution role, budget alert) lives in infra/ as Terraform; get env vars with `terraform -chdir=infra output -raw role_arn` and `bucket_name`. INC-000 (sklearn version mismatch) is written up in docs/incidents/. Next: Incident 0.

src/incident_lab/
  config.py     env-driven Settings, paths, SKLEARN_CONTAINER_VERSION (image tag), SKLEARN_SERVING_VERSION (sklearn inside it)
  data.py       downloads KOI data from the NASA Exoplanet Archive TAP API, prepares features
  train.py      HistGradientBoostingClassifier; writes model.joblib, metrics.json, reference.csv
  package.py    builds model.tar.gz (model.joblib + code/inference.py)
  deploy.py     uploads to S3, creates Model, EndpointConfig, Endpoint (or updates existing)
  invoke.py     boto3 smoke test with client-side timing
  teardown.py   deletes endpoint and sweeps all lab models and endpoint configs
serving/inference.py   container handlers: model_fn, input_fn, predict_fn, output_fn
tests/test_lab.py
docs/incidents/TEMPLATE.md

Run order: train, package, deploy, invoke, teardown, each via uv run python -m incident_lab.<module>. Required env vars: SAGEMAKER_ROLE_ARN, INCIDENT_LAB_BUCKET. Optional: AWS_REGION (default eu-west-2), ENDPOINT_NAME, INSTANCE_TYPE (default ml.m5.large), TTL_HOURS, FAULT.

Key design decisions (keep unless asked)
SageMaker Python SDK V3, low-level resources. We use sagemaker.core.resources (Model, EndpointConfig, Endpoint) and sagemaker.core.shapes, not ModelBuilder, so every layer (image, env vars, instance, config) stays visible. V3 has breaking changes from V2: do not follow V2 tutorials (SKLearnModel, Predictor, serializers). Check https://sagemaker.readthedocs.io/en/stable/ before changing SDK code.
Depend on sagemaker-core only. The full sagemaker package pulls in torch (about 6 GB venv vs about 350 MB).
boto3 for invoke and teardown. Invoke mirrors real clients. Teardown must never break, so it uses the most stable API.
Version pinning. Local scikit-learn is pinned to exactly 1.9.0, the version inside sagemaker-scikit-learn:1.9-0 (the tag's -0 is AWS's build number, not the sklearn patch). train refuses to run unless the full version matches SKLEARN_SERVING_VERSION. See INC-000.
Faults are config. FAULT is an env var on the SageMaker Model, read in serving/inference.py. Changing it means a new endpoint config and a rolling update.
Per-stage timing. Each handler logs one JSON line (event=stage_timing, stage, ms, fault) to stdout, queried with CloudWatch Logs Insights pct().
serving/inference.py runs inside the container: only import numpy, pandas, joblib, scikit-learn, and keep from __future__ import annotations. Its FEATURES list must match data.py (a test enforces this).
Rules for working on this repo
Cost safety first. A real-time endpoint bills every hour it exists. Always ask before running deploy. Remind me to run teardown at the end of every session. Never leave an endpoint running.
Python: uv for everything, type hints, explicit over clever, Pytest for tests, ruff for lint and format.
Commits: Conventional Commits (feat:, fix:, test:, docs:, chore:).
Writing style in docs and postmortems: plain language, no em dashes.
When there is a tradeoff, present the options and let me decide. If unsure about an AWS or library API, say so and check the docs rather than guessing.
Challenge my reasoning if an approach has a flaw.
Known unknowns (updated 2026-10-02 after the first deploy)
Resolved: the SDK V3 calls in deploy.py work against real AWS (create path; the update path is not yet exercised).
Resolved: the container runs scikit-learn 1.9.0, found via InconsistentVersionWarning in the logs. Local is now pinned to match (INC-000).
Resolved: output_fn returning a (body, content_type) tuple works in the 1.9-0 container.
Resolved: the NASA TAP download URL and column names work.
Open: whether InconsistentVersionWarning is gone after the fix. Check the logs for a clean model_load at the start of the next deploy, then mark INC-000 verified.
Open: what HTTP status the container returns when input_fn raises ValueError (answer during Incident 6).
Open: the update_endpoint path in deploy.py and check_update_landed have not run against AWS yet (first FAULT change will exercise them).
Observed on first deploy: two gunicorn workers on ml.m5.large, each loads its own model copy (model_load about 2.3 s); the model loads on the first ping, not at container start; first invoke took 1,323 ms client-side vs 84 ms in the container (hypothesis: cold client, to test in Incident 0).
Roadmap
Verify deploy end to end. Fix whatever the first real run reveals. (Done 2026-10-02: INC-000.)
Incident 0: baseline under load. Add Locust as a dev dependency. Because the endpoint needs SigV4 auth, use a custom Locust User wrapping the boto3 sagemaker-runtime client and fire events.request manually (not HttpUser). Record baseline p50, p95, p99 per stage and the CloudWatch ModelLatency and OverheadLatency metrics (both in microseconds).
Incidents 1 to 7, one at a time, each with a postmortem from the template:
#	Incident	How to inject	Signal to watch	Likely fix
1	Slow preprocessing	FAULT=slow_preprocess: row-by-row Python loop in input_fn	ModelLatency up, OverheadLatency flat, input stage p99	Vectorise
2	Undersized instance	Smaller INSTANCE_TYPE under Locust load	CPUUtilization, MemoryUtilization in /aws/sagemaker/Endpoints	Right-size
3	Traffic spike	Locust ramp on a single instance	Invocations, latency climb, errors	Target-tracking autoscaling; document the scaling lag
4	Cold start	Serverless variant (serverless_config on ProductionVariant)	ModelSetupTime, first-request latency	Provisioned concurrency or real-time
5	Memory leak	FAULT=memory_leak: global list grows per request	MemoryUtilization trending up, restarts	Remove the leak, add an alarm
6	Schema break	Client omits a feature	Error metrics, then the logs	Clear validation contract, versioned schema
7	Silent drift	Data capture to S3, shift input distributions client-side	Latency and errors healthy; Evidently report flags drift vs reference.csv	Detection job, retraining trigger

Evidently note: use the current 0.7.x API (from evidently import Report, presets in evidently.presets). Older tutorials use removed module paths.

Later ideas. A scheduled "cost janitor" Lambda that deletes endpoints tagged project=incident-lab older than their ttl-hours tag; a CloudWatch dashboard as code; least-privilege IAM role; GitHub Actions running Pytest and ruff.