"""Locust load test for the SageMaker endpoint.

Uses boto3 instead of HttpUser because the endpoint needs SigV4-signed requests.
Each request is reported to Locust manually via events.request.

BATCH_SIZE (env var, default 1) sets the rows per request. The baseline used 1.
"""

import json
import os
import time

import boto3
import pandas as pd
from botocore.config import Config
from locust import User, constant_throughput, task

from incident_lab.config import REFERENCE_FILE, load_endpoint_name, load_region
from incident_lab.invoke import build_payload

REFERENCE = pd.read_csv(REFERENCE_FILE)
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "1"))
# No retries: a throttled or failed request must show as an error, not a slow success.
NO_RETRIES = Config(retries={"total_max_attempts": 1})


class SageMakerUser(User):
    wait_time = constant_throughput(2)  # each user sends at most 2 requests per second

    def on_start(self) -> None:
        self.runtime = boto3.client(
            "sagemaker-runtime", region_name=load_region(), config=NO_RETRIES
        )
        self.endpoint_name = load_endpoint_name()

    @task
    def predict(self) -> None:
        body = json.dumps(build_payload(REFERENCE.sample(BATCH_SIZE)))
        start = time.perf_counter()
        exception: Exception | None = None
        response_length = 0
        try:
            response = self.runtime.invoke_endpoint(
                EndpointName=self.endpoint_name,
                ContentType="application/json",
                Accept="application/json",
                Body=body,
            )
            response_length = len(response["Body"].read())
        except Exception as error:  # noqa: BLE001 - every failure must be reported to Locust
            exception = error
        self.environment.events.request.fire(
            request_type="sagemaker",
            name="invoke_endpoint",
            response_time=(time.perf_counter() - start) * 1000,
            response_length=response_length,
            exception=exception,
            context={},
        )
