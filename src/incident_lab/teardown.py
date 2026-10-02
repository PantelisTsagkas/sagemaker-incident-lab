"""Delete every endpoint, endpoint config and model this lab created, then verify.

Plain boto3 on purpose: the one script that must never break (because a
forgotten endpoint bills every hour) depends on the most stable API there is.
It needs only AWS_REGION, so it works in a fresh shell. It sweeps by name,
not just the current ENDPOINT_NAME, so leftovers from half-failed deploys and
one-off variants (serverless, overrides) are caught too.
"""

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from incident_lab.config import PROJECT, load_endpoint_name, load_region


def lab_resource_names(
    client: BaseClient, operation: str, list_key: str, name_key: str
) -> list[str]:
    paginator = client.get_paginator(operation)
    names: list[str] = []
    for page in paginator.paginate(NameContains=PROJECT):
        names.extend(item[name_key] for item in page[list_key])
    return names


def endpoints_to_delete(client: BaseClient, configured_name: str) -> list[str]:
    """Every endpoint with the project in its name, plus ENDPOINT_NAME if it was overridden."""
    names = lab_resource_names(client, "list_endpoints", "Endpoints", "EndpointName")
    if configured_name not in names:
        names.append(configured_name)
    return names


def delete_endpoint(client: BaseClient, endpoint_name: str) -> bool:
    """Returns True if a delete was issued, False if there was nothing to delete."""
    try:
        client.delete_endpoint(EndpointName=endpoint_name)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ValidationException":
            raise
        return False
    print(f"Deleting endpoint {endpoint_name}")
    return True


def main() -> None:
    region = load_region()
    client = boto3.client("sagemaker", region_name=region)

    deleted = [
        name
        for name in endpoints_to_delete(client, load_endpoint_name())
        if delete_endpoint(client, name)
    ]

    # Configs must not be deleted while an endpoint is still using them, so wait first.
    waiter = client.get_waiter("endpoint_deleted")
    for name in deleted:
        print(f"Waiting for {name} to finish deleting...")
        waiter.wait(EndpointName=name, WaiterConfig={"Delay": 10, "MaxAttempts": 60})

    for name in lab_resource_names(
        client, "list_endpoint_configs", "EndpointConfigs", "EndpointConfigName"
    ):
        client.delete_endpoint_config(EndpointConfigName=name)
        print(f"Deleted endpoint config {name}")

    for name in lab_resource_names(client, "list_models", "Models", "ModelName"):
        client.delete_model(ModelName=name)
        print(f"Deleted model {name}")

    remaining = lab_resource_names(client, "list_endpoints", "Endpoints", "EndpointName")
    if remaining:
        raise RuntimeError(f"Endpoints still present in {region}: {remaining}. Check the console.")
    print(f"Verified: no {PROJECT} endpoints left in {region}. Nothing is billing.")


if __name__ == "__main__":
    main()
