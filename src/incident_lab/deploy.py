"""Upload the model archive and create (or update) the real-time endpoint.

Uses SageMaker Python SDK V3's low-level resources (sagemaker.core) rather than
ModelBuilder on purpose: for troubleshooting we want every layer visible
(image, env vars, instance type, endpoint config), not hidden behind defaults.
"""

from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError
from sagemaker.core import image_uris
from sagemaker.core.resources import Endpoint, EndpointConfig, Model
from sagemaker.core.shapes import ContainerDefinition, ProductionVariant, Tag

from incident_lab.config import (
    ARCHIVE_FILE,
    PROJECT,
    SKLEARN_CONTAINER_VERSION,
    Settings,
    load_settings,
)


def version_suffix(now: datetime) -> str:
    return now.strftime("%Y%m%d-%H%M%S")


def container_environment(fault: str) -> dict[str, str]:
    return {
        "SAGEMAKER_PROGRAM": "inference.py",
        "SAGEMAKER_SUBMIT_DIRECTORY": "/opt/ml/model/code",
        "FAULT": fault,
    }


def lab_tags(settings: Settings) -> list[Tag]:
    return [
        Tag(key="project", value=PROJECT),
        Tag(key="ttl-hours", value=str(settings.ttl_hours)),
    ]


def upload_archive(settings: Settings, suffix: str) -> str:
    key = f"{PROJECT}/{suffix}/model.tar.gz"
    boto3.client("s3", region_name=settings.region).upload_file(
        str(ARCHIVE_FILE), settings.bucket, key
    )
    return f"s3://{settings.bucket}/{key}"


def endpoint_status(settings: Settings) -> str | None:
    """Current EndpointStatus, or None if the endpoint does not exist."""
    client = boto3.client("sagemaker", region_name=settings.region)
    try:
        return client.describe_endpoint(EndpointName=settings.endpoint_name)["EndpointStatus"]
    except ClientError as error:
        if error.response["Error"]["Code"] == "ValidationException":
            return None
        raise


def check_update_landed(description: dict[str, str], expected_config: str) -> None:
    """A failed rolling update rolls back to the OLD config and still reports InService.

    Without this check you would measure the previous FAULT and not know it.
    """
    actual = description["EndpointConfigName"]
    if actual != expected_config:
        reason = description.get("FailureReason", "no FailureReason given")
        raise RuntimeError(
            f"Endpoint is InService but on {actual}, not {expected_config}. "
            f"The update was rolled back: {reason}"
        )


def main() -> None:
    settings = load_settings()
    if not ARCHIVE_FILE.exists():
        raise FileNotFoundError("No model archive. Run `uv run python -m incident_lab.package`.")

    suffix = version_suffix(datetime.now(UTC))
    model_data_url = upload_archive(settings, suffix)
    image = image_uris.retrieve(
        framework="sklearn",
        region=settings.region,
        version=SKLEARN_CONTAINER_VERSION,
        instance_type=settings.instance_type,
    )
    print(f"Image: {image}\nModel data: {model_data_url}\nFAULT={settings.fault}")

    model_name = f"{PROJECT}-{suffix}"
    Model.create(
        model_name=model_name,
        primary_container=ContainerDefinition(
            image=image,
            model_data_url=model_data_url,
            environment=container_environment(settings.fault),
        ),
        execution_role_arn=settings.role_arn,
        tags=lab_tags(settings),
        region=settings.region,
    )

    config_name = f"{PROJECT}-{suffix}"
    EndpointConfig.create(
        endpoint_config_name=config_name,
        production_variants=[
            ProductionVariant(
                variant_name="AllTraffic",
                model_name=model_name,
                initial_instance_count=1,
                instance_type=settings.instance_type,
            )
        ],
        tags=lab_tags(settings),
        region=settings.region,
    )

    status = endpoint_status(settings)
    if status is not None and status != "InService":
        raise RuntimeError(
            f"{settings.endpoint_name} is {status}; it cannot be updated. "
            "Run `uv run python -m incident_lab.teardown` and deploy again."
        )

    if status == "InService":
        # Same path a real config change takes: new config, rolling update, minutes not seconds.
        print(f"Updating {settings.endpoint_name} to {config_name} (several minutes)...")
        boto3.client("sagemaker", region_name=settings.region).update_endpoint(
            EndpointName=settings.endpoint_name, EndpointConfigName=config_name
        )
        endpoint = Endpoint.get(endpoint_name=settings.endpoint_name, region=settings.region)
    else:
        print(f"Creating {settings.endpoint_name} (usually 5 to 10 minutes)...")
        endpoint = Endpoint.create(
            endpoint_name=settings.endpoint_name,
            endpoint_config_name=config_name,
            tags=lab_tags(settings),
            region=settings.region,
        )

    endpoint.wait_for_status(target_status="InService", timeout=30 * 60)
    description = boto3.client("sagemaker", region_name=settings.region).describe_endpoint(
        EndpointName=settings.endpoint_name
    )
    check_update_landed(description, config_name)
    print(
        f"{settings.endpoint_name} is InService. Remember: `uv run python -m incident_lab.teardown`."
    )


if __name__ == "__main__":
    main()
