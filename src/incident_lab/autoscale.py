"""Turn on target-tracking autoscaling for the live endpoint, or show what it is doing.

    SCALE_TARGET_RPS=120 uv run python -m incident_lab.autoscale apply
    uv run python -m incident_lab.autoscale status

Plain boto3 (Application Auto Scaling), not Terraform: the scaling registration hangs
off an endpoint that deploy creates and teardown deletes every session, so Terraform
state would be stale every time. Teardown deregisters it, which also deletes the policy.

Do not run deploy while this is registered. An update that changes the instance type
fails, and a new endpoint config resets the instance count to 1.

The policy tracks SageMakerVariantInvocationsPerInstance. Two things about that metric:
- It is per MINUTE, so SCALE_TARGET_RPS (per second, which is how we measure load) is
  multiplied by 60.
- It counts requests the instances served. A saturated instance serves at its ceiling
  and rejects the rest, so the target must sit well below the ceiling or the metric
  can never reach it and nothing scales.
"""

import os
import sys

import boto3
from botocore.client import BaseClient

from incident_lab.config import load_endpoint_name, load_region

VARIANT = "AllTraffic"
DIMENSION = "sagemaker:variant:DesiredInstanceCount"
POLICY_NAME = "incident-lab-invocations-per-instance"
# AWS defaults for SageMaker variants, written out so the postmortem can point at them.
SCALE_OUT_COOLDOWN_SECONDS = 300
SCALE_IN_COOLDOWN_SECONDS = 300


def resource_id(endpoint_name: str) -> str:
    return f"endpoint/{endpoint_name}/variant/{VARIANT}"


def per_minute(requests_per_second: float) -> float:
    return requests_per_second * 60


def policy_config(target_rps: float) -> dict[str, object]:
    return {
        "TargetValue": per_minute(target_rps),
        "PredefinedMetricSpecification": {
            "PredefinedMetricType": "SageMakerVariantInvocationsPerInstance"
        },
        "ScaleOutCooldown": SCALE_OUT_COOLDOWN_SECONDS,
        "ScaleInCooldown": SCALE_IN_COOLDOWN_SECONDS,
    }


def apply(
    autoscaling: BaseClient, endpoint_name: str, target_rps: float, max_instances: int
) -> None:
    autoscaling.register_scalable_target(
        ServiceNamespace="sagemaker",
        ResourceId=resource_id(endpoint_name),
        ScalableDimension=DIMENSION,
        MinCapacity=1,
        MaxCapacity=max_instances,  # the cost cap: never more than this many instances
    )
    autoscaling.put_scaling_policy(
        PolicyName=POLICY_NAME,
        ServiceNamespace="sagemaker",
        ResourceId=resource_id(endpoint_name),
        ScalableDimension=DIMENSION,
        PolicyType="TargetTrackingScaling",
        TargetTrackingScalingPolicyConfiguration=policy_config(target_rps),
    )
    print(
        f"Autoscaling on for {endpoint_name}: 1 to {max_instances} instances, "
        f"target {target_rps:g} requests/s per instance ({per_minute(target_rps):g} per minute)."
    )


def status(autoscaling: BaseClient, sagemaker: BaseClient, endpoint_name: str) -> None:
    variant = sagemaker.describe_endpoint(EndpointName=endpoint_name)["ProductionVariants"][0]
    print(
        f"Instances: {variant['CurrentInstanceCount']} running, "
        f"{variant['DesiredInstanceCount']} wanted"
    )
    activities = autoscaling.describe_scaling_activities(
        ServiceNamespace="sagemaker", ResourceId=resource_id(endpoint_name)
    )["ScalingActivities"]
    for activity in reversed(activities):  # oldest first
        end = activity.get("EndTime")
        print(
            f"{activity['StartTime']:%H:%M:%S} to {f'{end:%H:%M:%S}' if end else 'running'} "
            f"{activity['StatusCode']}: {activity['Description']}"
        )
        print(f"    cause: {activity['Cause']}")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    region = load_region()
    endpoint_name = load_endpoint_name()
    autoscaling = boto3.client("application-autoscaling", region_name=region)
    if command == "apply":
        target_rps = float(os.environ["SCALE_TARGET_RPS"])
        max_instances = int(os.environ.get("SCALE_MAX_INSTANCES", "3"))
        apply(autoscaling, endpoint_name, target_rps, max_instances)
    elif command == "status":
        status(autoscaling, boto3.client("sagemaker", region_name=region), endpoint_name)
    else:
        raise SystemExit("Usage: python -m incident_lab.autoscale [apply|status]")


if __name__ == "__main__":
    main()
