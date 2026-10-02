"""Smoke test: send a few held-out rows and time the round trip from the client side.

Uses the plain boto3 runtime client because that is how real callers (and the
Locust load test later) hit the endpoint.
"""

import json
import math
import time
from typing import Any

import boto3
import pandas as pd

from incident_lab.config import REFERENCE_FILE, load_settings
from incident_lab.data import FEATURES


def build_payload(rows: pd.DataFrame) -> dict[str, Any]:
    """Convert rows to {"instances": [...]}, mapping NaN to JSON null."""
    instances = [
        {f: (None if math.isnan(value) else float(value)) for f, value in row.items()}
        for row in rows[FEATURES].to_dict(orient="records")
    ]
    return {"instances": instances}


def main(sample_size: int = 5) -> None:
    settings = load_settings()
    rows = pd.read_csv(REFERENCE_FILE).sample(sample_size, random_state=7)
    runtime = boto3.client("sagemaker-runtime", region_name=settings.region)

    start = time.perf_counter()
    response = runtime.invoke_endpoint(
        EndpointName=settings.endpoint_name,
        ContentType="application/json",
        Accept="application/json",
        Body=json.dumps(build_payload(rows)),
    )
    client_ms = (time.perf_counter() - start) * 1000
    predictions = json.loads(response["Body"].read())["predictions"]

    for truth, prediction in zip(rows["label"], predictions, strict=True):
        print(
            f"truth={truth}  predicted={prediction['label']:<15} p={prediction['probability']:.3f}"
        )
    print(f"Client-side round trip: {client_ms:.1f} ms (includes network + SageMaker overhead)")


if __name__ == "__main__":
    main()
