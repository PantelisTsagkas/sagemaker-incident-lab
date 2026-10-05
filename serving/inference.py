"""Handlers for the SageMaker scikit-learn container.

The container calls these four functions in order:
model_fn (once, at startup) -> input_fn -> predict_fn -> output_fn (per request).

Every stage prints one JSON line to stdout, which lands in CloudWatch Logs under
/aws/sagemaker/Endpoints/<endpoint-name>. Logs Insights can then compute
percentiles per stage, which is how we find WHERE the time goes.

This file runs inside the container, not in the uv environment, so it only imports
what the container ships with (numpy, pandas, joblib, scikit-learn).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import joblib
import pandas as pd

# Keep in sync with src/incident_lab/data.py (a test enforces this).
FEATURES: list[str] = [
    "koi_period",
    "koi_duration",
    "koi_depth",
    "koi_prad",
    "koi_teq",
    "koi_insol",
    "koi_model_snr",
    "koi_impact",
    "koi_steff",
    "koi_slogg",
    "koi_srad",
]
JSON_CONTENT_TYPE = "application/json"

# Fault injection switch. Each incident adds one behaviour here, toggled by the
# FAULT environment variable on the SageMaker Model. "none" means healthy.
FAULT = os.environ.get("FAULT", "none")
KNOWN_FAULTS = {"none", "slow_preprocess"}


def log_event(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, "fault": FAULT, **fields}), flush=True)


@contextmanager
def timed(stage: str) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        log_event("stage_timing", stage=stage, ms=round(elapsed_ms, 3))


def model_fn(model_dir: str) -> Any:
    if FAULT not in KNOWN_FAULTS:
        log_event("unknown_fault", known=sorted(KNOWN_FAULTS))
    with timed("model_load"):
        return joblib.load(os.path.join(model_dir, "model.joblib"))


def input_fn(request_body: str | bytes, request_content_type: str) -> pd.DataFrame:
    with timed("input"):
        if request_content_type != JSON_CONTENT_TYPE:
            raise ValueError(f"Unsupported content type: {request_content_type}")

        payload = json.loads(request_body)
        instances = payload.get("instances")
        if not isinstance(instances, list) or not instances:
            raise ValueError('Body must be {"instances": [ {feature: value, ...}, ... ]}')

        missing = sorted({f for row in instances for f in FEATURES if f not in row})
        if missing:
            raise ValueError(f"Missing features: {missing}")

        if FAULT == "slow_preprocess":
            return rows_to_frame_one_by_one(instances)
        # Explicit column order: the model was trained on exactly this order.
        return pd.DataFrame(instances, columns=FEATURES).astype("float64")


def rows_to_frame_one_by_one(instances: list[dict[str, Any]]) -> pd.DataFrame:
    """INC-001 fault: grow the DataFrame one row at a time.

    Same result as the vectorised path, but every .loc append makes pandas reallocate
    and re-check the frame, so the cost grows with batch size. Invisible at one row.
    """
    frame = pd.DataFrame(columns=FEATURES)
    for row in instances:
        frame.loc[len(frame)] = [row[f] for f in FEATURES]
    return frame.astype("float64")


def predict_fn(input_data: pd.DataFrame, model: Any) -> list[float]:
    with timed("predict"):
        return model.predict_proba(input_data)[:, 1].tolist()


def output_fn(prediction: list[float], accept: str) -> tuple[str, str]:
    with timed("output"):
        body = {
            "predictions": [
                {"probability": round(p, 6), "label": "CONFIRMED" if p >= 0.5 else "FALSE POSITIVE"}
                for p in prediction
            ]
        }
        return json.dumps(body), JSON_CONTENT_TYPE
