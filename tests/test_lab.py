import importlib.util
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import boto3
import joblib
import numpy as np
import pandas as pd
import pytest
from botocore.stub import Stubber

from incident_lab import teardown
from incident_lab.config import SERVING_DIR
from incident_lab.data import FEATURES, prepare
from incident_lab.deploy import check_update_landed, container_environment, version_suffix
from incident_lab.invoke import build_payload
from incident_lab.package import build_model_archive
from incident_lab.train import check_sklearn_matches_container, train_model


def load_inference_module() -> ModuleType:
    """serving/inference.py is not part of the package (it runs in the container), so load by path."""
    spec = importlib.util.spec_from_file_location("inference", SERVING_DIR / "inference.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def synthetic_koi(rows: int = 400, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    data = {feature: rng.normal(size=rows) for feature in FEATURES}
    signal = data["koi_model_snr"] + data["koi_prad"]
    dispositions = np.where(signal > 0, "CONFIRMED", "FALSE POSITIVE").astype(object)
    dispositions[:20] = "CANDIDATE"
    data["koi_disposition"] = dispositions
    data["koi_period"][25] = np.nan
    return pd.DataFrame(data)


@pytest.fixture(scope="module")
def trained(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, pd.DataFrame]:
    features, labels = prepare(synthetic_koi())
    model, reference, _ = train_model(features, labels)
    model_dir = tmp_path_factory.mktemp("model")
    joblib.dump(model, model_dir / "model.joblib")
    return model_dir, reference


def test_prepare_drops_candidates_and_keeps_nan() -> None:
    features, labels = prepare(synthetic_koi())
    assert len(features) == 380
    assert set(labels.unique()) <= {0, 1}
    assert list(features.columns) == FEATURES
    assert features.isna().any().any()


def test_serving_features_match_training_features() -> None:
    assert load_inference_module().FEATURES == FEATURES


def test_sklearn_version_check() -> None:
    check_sklearn_matches_container("1.9.0", "1.9.0")
    # A patch-level mismatch must fail too: it caused InconsistentVersionWarning on the endpoint.
    with pytest.raises(RuntimeError, match="does not match"):
        check_sklearn_matches_container("1.9.1", "1.9.0")
    with pytest.raises(RuntimeError, match="does not match"):
        check_sklearn_matches_container("1.8.0", "1.9.0")


def test_handlers_end_to_end(
    trained: tuple[Path, pd.DataFrame], capsys: pytest.CaptureFixture[str]
) -> None:
    model_dir, reference = trained
    inference = load_inference_module()

    model = inference.model_fn(str(model_dir))
    body = json.dumps(build_payload(reference.head(3)))
    frame = inference.input_fn(body, "application/json")
    probabilities = inference.predict_fn(frame, model)
    output, content_type = inference.output_fn(probabilities, "application/json")

    assert content_type == "application/json"
    predictions = json.loads(output)["predictions"]
    assert len(predictions) == 3
    assert all(0.0 <= p["probability"] <= 1.0 for p in predictions)

    timing_lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    stages = [line["stage"] for line in timing_lines if line["event"] == "stage_timing"]
    assert stages == ["model_load", "input", "predict", "output"]


def test_input_fn_rejects_missing_features() -> None:
    inference = load_inference_module()
    body = json.dumps({"instances": [{"koi_period": 1.0}]})
    with pytest.raises(ValueError, match="Missing features"):
        inference.input_fn(body, "application/json")


def test_input_fn_rejects_wrong_content_type() -> None:
    with pytest.raises(ValueError, match="Unsupported content type"):
        load_inference_module().input_fn("a,b", "text/csv")


def test_build_payload_maps_nan_to_null() -> None:
    rows = pd.DataFrame([{f: 1.0 for f in FEATURES} | {"koi_depth": float("nan")}])
    payload = build_payload(rows)
    assert payload["instances"][0]["koi_depth"] is None
    json.dumps(payload)  # must be valid JSON (NaN is not)


def test_archive_layout(trained: tuple[Path, pd.DataFrame], tmp_path: Path) -> None:
    model_dir, _ = trained
    archive = build_model_archive(
        model_dir / "model.joblib", SERVING_DIR, tmp_path / "model.tar.gz"
    )
    with tarfile.open(archive) as tar:
        assert sorted(tar.getnames()) == ["code/inference.py", "model.joblib"]


def test_deploy_helpers() -> None:
    assert version_suffix(datetime(2026, 10, 1, 9, 5, 3, tzinfo=UTC)) == "20261001-090503"
    env = container_environment("none")
    assert env["SAGEMAKER_PROGRAM"] == "inference.py"
    assert env["FAULT"] == "none"


def test_check_update_landed_catches_rollback() -> None:
    check_update_landed({"EndpointConfigName": "new"}, "new")
    with pytest.raises(RuntimeError, match="rolled back"):
        check_update_landed(
            {"EndpointConfigName": "old", "FailureReason": "health check failed"}, "new"
        )


def test_teardown_needs_only_region_and_sweeps_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teardown must work in a fresh shell and catch endpoints beyond ENDPOINT_NAME."""
    for name in ("SAGEMAKER_ROLE_ARN", "INCIDENT_LAB_BUCKET", "ENDPOINT_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_REGION", "eu-west-2")

    client = boto3.client(
        "sagemaker",
        region_name="eu-west-2",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    stubber = Stubber(client)
    lab = {"NameContains": "incident-lab"}
    gone = {"service_error_code": "ValidationException", "service_message": "not found"}
    stamp = datetime(2026, 10, 1, tzinfo=UTC)

    def endpoint(name: str) -> dict[str, object]:
        return {
            "EndpointName": name,
            "EndpointArn": f"arn:aws:sagemaker:eu-west-2:123456789012:endpoint/{name}",
            "CreationTime": stamp,
            "LastModifiedTime": stamp,
            "EndpointStatus": "InService",
        }

    stubber.add_response(
        "list_endpoints",
        {"Endpoints": [endpoint("incident-lab"), endpoint("incident-lab-serverless")]},
        lab,
    )
    stubber.add_response("delete_endpoint", {}, {"EndpointName": "incident-lab"})
    stubber.add_response("delete_endpoint", {}, {"EndpointName": "incident-lab-serverless"})
    stubber.add_client_error("describe_endpoint", **gone)  # waiter: first endpoint gone
    stubber.add_client_error("describe_endpoint", **gone)  # waiter: second endpoint gone
    stubber.add_response(
        "list_endpoint_configs",
        {
            "EndpointConfigs": [
                {
                    "EndpointConfigName": "incident-lab-1",
                    "EndpointConfigArn": "arn:aws:sagemaker:eu-west-2:1:endpoint-config/x",
                    "CreationTime": stamp,
                }
            ]
        },
        lab,
    )
    stubber.add_response("delete_endpoint_config", {}, {"EndpointConfigName": "incident-lab-1"})
    stubber.add_response(
        "list_models",
        {
            "Models": [
                {
                    "ModelName": "incident-lab-1",
                    "ModelArn": "arn:aws:sagemaker:eu-west-2:1:model/x",
                    "CreationTime": stamp,
                }
            ]
        },
        lab,
    )
    stubber.add_response("delete_model", {}, {"ModelName": "incident-lab-1"})
    stubber.add_response("list_endpoints", {"Endpoints": []}, lab)

    monkeypatch.setattr(teardown.boto3, "client", lambda *args, **kwargs: client)
    with stubber:
        teardown.main()
    stubber.assert_no_pending_responses()


def test_slow_preprocess_returns_same_frame_as_healthy(
    trained: tuple[Path, pd.DataFrame], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The INC-001 fault must change only the speed, never the model input."""
    _, reference = trained
    rows = reference.head(5).copy()
    rows.loc[rows.index[1], "koi_depth"] = np.nan  # nulls must survive both paths
    body = json.dumps(build_payload(rows))
    inference = load_inference_module()

    healthy = inference.input_fn(body, "application/json")
    monkeypatch.setattr(inference, "FAULT", "slow_preprocess")
    slow = inference.input_fn(body, "application/json")

    pd.testing.assert_frame_equal(slow, healthy)
    assert slow["koi_depth"].isna().sum() == 1
