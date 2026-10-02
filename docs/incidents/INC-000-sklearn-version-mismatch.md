# INC-000: scikit-learn version mismatch between training and serving

**Date:** 2026-10-02  **Fault flag:** none (found, not injected)  **Instance:** ml.m5.large

## Summary
The model was trained locally on scikit-learn 1.9.1 but served by a container running 1.9.0.
Predictions came back correct, but every model load logged a warning that results might be
invalid. No users were affected: it was caught on the first ever deploy, and the endpoint was
torn down before the fix.

## Detection
Found by reading the container logs live during the first deploy, not by an alarm:

```bash
aws logs tail /aws/sagemaker/Endpoints/incident-lab --follow --region eu-west-2
```

The warning appeared once per estimator, on every model load:

```
InconsistentVersionWarning: Trying to unpickle estimator HistGradientBoostingClassifier
from version 1.9.1 when using version 1.9.0. This might lead to breaking code or invalid
results. Use at your own risk.
```

Nothing else flagged it. The endpoint passed its health checks, reached InService, and
returned five correct predictions out of five.

## Timeline
All times UTC.

- 08:49 first deploy started (`FAULT=none`)
- 08:51:54 first `InconsistentVersionWarning` in the logs
- 08:52:38 endpoint InService, healthy despite the warning
- 09:00:46 smoke test returned correct predictions
- After teardown: root cause found in `train.py`, fix committed (`97b221a`)

## Evidence
- The warning was logged twice per startup, once for each gunicorn worker, because each
  worker loads its own copy of the model.
- No measurable impact on accuracy. Retraining on 1.9.0 gave identical metrics:
  ROC AUC 0.9794 and accuracy 0.9320, before and after.
- Latency was not affected: inside the container, input, predict and output took 84 ms in total.

## Root cause
There were two layers.

1. `pyproject.toml` allowed any 1.9.x (`>=1.9,<1.10`), so `uv` installed the newest patch,
   1.9.1.
2. The guard in `train.py` was meant to catch exactly this, but it only compared major and
   minor versions. It read the container image tag `1.9-0` as "version 1.9". The `-0` is
   AWS's container build number, not the scikit-learn patch version. The tag cannot tell
   you the patch version at all: only the running container can.

The test for the guard made the same assumption. It asserted that 1.9.1 against `1.9-0`
should pass, so the test suite confirmed the bug instead of catching it.

## Fix
- Pinned `scikit-learn==1.9.0` exactly in `pyproject.toml`.
- Added `SKLEARN_SERVING_VERSION = "1.9.0"` to `config.py`, kept separate from
  `SKLEARN_CONTAINER_VERSION = "1.9-0"`. One picks the image, the other records what is
  inside it.
- The guard now compares the full version string, with no parsing.
- Rewrote the test first: it now expects 1.9.1 against 1.9.0 to fail. It failed against the
  old guard and passes against the new one.

**Verification: pending.** On the next deploy, the logs should show `model_load` with no
`InconsistentVersionWarning`.

## Prevention
- The tightened test stops a looser version check from coming back unnoticed.
- Open gap: `SKLEARN_SERVING_VERSION` was read from the logs by hand. If AWS rebuilds the
  `1.9-0` image with a different patch version, nothing here will notice. A later
  improvement could fail the deploy, or raise an alarm, on any `InconsistentVersionWarning`
  in the endpoint logs.

## Lesson
A healthy endpoint is not the same as a correct one. Every built-in signal said "fine": the
health checks, InService, and correct predictions. The only evidence was a warning in the
logs, and only someone reading them would have seen it.
