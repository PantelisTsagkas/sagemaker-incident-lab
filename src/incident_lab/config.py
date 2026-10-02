"""Settings read from environment variables, so nothing account-specific lives in git."""

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT = "incident-lab"

# Must match scikit-learn in pyproject.toml. A version mismatch between the
# training machine and the serving container is the classic deploy bug.
SKLEARN_CONTAINER_VERSION = "1.9-0"
# The tag's "-0" is AWS's container build number, not the sklearn patch, so the exact
# version inside the image was read from InconsistentVersionWarning in the endpoint
# logs on 2026-10-02. Keep pyproject.toml pinned to exactly this.
SKLEARN_SERVING_VERSION = "1.9.0"

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
ARTIFACTS_DIR = REPO_ROOT / "artifacts"
SERVING_DIR = REPO_ROOT / "serving"

RAW_KOI_FILE = DATA_DIR / "koi_cumulative.csv"
MODEL_FILE = ARTIFACTS_DIR / "model.joblib"
REFERENCE_FILE = ARTIFACTS_DIR / "reference.csv"
METRICS_FILE = ARTIFACTS_DIR / "metrics.json"
ARCHIVE_FILE = ARTIFACTS_DIR / "model.tar.gz"


@dataclass(frozen=True)
class Settings:
    region: str
    role_arn: str
    bucket: str
    endpoint_name: str
    instance_type: str
    ttl_hours: int
    fault: str


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing environment variable {name}. See README 'Setup'.")
    return value


def load_region() -> str:
    """Region only. Teardown uses this so it works in a fresh shell with nothing else set."""
    return os.environ.get("AWS_REGION", "eu-west-2")


def load_endpoint_name() -> str:
    return os.environ.get("ENDPOINT_NAME", PROJECT)


def load_settings() -> Settings:
    return Settings(
        region=load_region(),
        role_arn=_required("SAGEMAKER_ROLE_ARN"),
        bucket=_required("INCIDENT_LAB_BUCKET"),
        endpoint_name=load_endpoint_name(),
        instance_type=os.environ.get("INSTANCE_TYPE", "ml.m5.large"),
        ttl_hours=int(os.environ.get("TTL_HOURS", "3")),
        fault=os.environ.get("FAULT", "none"),
    )
