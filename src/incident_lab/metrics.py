"""Save per-minute endpoint metrics to a CSV, so a postmortem's numbers live in results/.

    uv run python -m incident_lab.metrics 09:13 09:32 results/inc003_ramp_cloudwatch.csv

Times are UTC, today's date. Latencies are converted from microseconds to ms.
Instance metrics (CPU, memory) are averages across all instances in the variant.
"""

import csv
import sys
from datetime import UTC, datetime, timedelta

import boto3

from incident_lab.config import load_endpoint_name, load_region

# (column, namespace, metric, statistic, divide by)
METRICS: list[tuple[str, str, str, str, float]] = [
    ("invocations", "AWS/SageMaker", "Invocations", "Sum", 1),
    ("invocations_per_instance", "AWS/SageMaker", "InvocationsPerInstance", "Sum", 1),
    ("errors_4xx", "AWS/SageMaker", "Invocation4XXErrors", "Sum", 1),
    ("errors_5xx", "AWS/SageMaker", "Invocation5XXErrors", "Sum", 1),
    ("model_latency_p50_ms", "AWS/SageMaker", "ModelLatency", "p50", 1000),
    ("model_latency_p99_ms", "AWS/SageMaker", "ModelLatency", "p99", 1000),
    ("overhead_latency_p50_ms", "AWS/SageMaker", "OverheadLatency", "p50", 1000),
    ("concurrent_requests_max", "AWS/SageMaker", "ConcurrentRequestsPerModel", "Maximum", 1),
    ("cpu_avg_pct", "/aws/sagemaker/Endpoints", "CPUUtilization", "Average", 1),
    ("memory_avg_pct", "/aws/sagemaker/Endpoints", "MemoryUtilization", "Average", 1),
]


def utc_today_at(hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime.now(UTC).replace(hour=hour, minute=minute, second=0, microsecond=0)


def fetch(start: datetime, end: datetime) -> dict[datetime, dict[str, float]]:
    dimensions = [
        {"Name": "EndpointName", "Value": load_endpoint_name()},
        {"Name": "VariantName", "Value": "AllTraffic"},
    ]
    queries = [
        {
            "Id": f"m{index}",
            "Label": column,
            "MetricStat": {
                "Metric": {"Namespace": namespace, "MetricName": metric, "Dimensions": dimensions},
                "Period": 60,
                "Stat": stat,
            },
        }
        for index, (column, namespace, metric, stat, _) in enumerate(METRICS)
    ]
    divisors = {column: divisor for column, _, _, _, divisor in METRICS}
    cloudwatch = boto3.client("cloudwatch", region_name=load_region())
    rows: dict[datetime, dict[str, float]] = {}
    for page in cloudwatch.get_paginator("get_metric_data").paginate(
        MetricDataQueries=queries, StartTime=start, EndTime=end
    ):
        for result in page["MetricDataResults"]:
            column = result["Label"]
            for timestamp, value in zip(result["Timestamps"], result["Values"], strict=True):
                rows.setdefault(timestamp, {})[column] = round(value / divisors[column], 2)
    return rows


def main() -> None:
    start_hhmm, end_hhmm, out_path = sys.argv[1:4]
    start = utc_today_at(start_hhmm)
    end = utc_today_at(end_hhmm)
    if end <= start:
        end += timedelta(days=1)
    rows = fetch(start, end)
    columns = [column for column, *_ in METRICS]
    with open(out_path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["minute_utc", *columns])
        for timestamp in sorted(rows):
            writer.writerow(
                [
                    f"{timestamp.astimezone(UTC):%H:%M}",
                    *(rows[timestamp].get(column, "") for column in columns),
                ]
            )
    print(f"Wrote {len(rows)} minutes to {out_path}")


if __name__ == "__main__":
    main()
