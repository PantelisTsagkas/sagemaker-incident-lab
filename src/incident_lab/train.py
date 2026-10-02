"""Train a small exoplanet classifier locally. The model is the patient, not the star."""

import json

import joblib
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.model_selection import train_test_split

from incident_lab.config import (
    ARTIFACTS_DIR,
    METRICS_FILE,
    MODEL_FILE,
    RAW_KOI_FILE,
    REFERENCE_FILE,
    SKLEARN_CONTAINER_VERSION,
)
from incident_lab.data import download_koi, prepare


def check_sklearn_matches_container(local_version: str, container_version: str) -> None:
    """Fail fast if the local sklearn minor version differs from the serving container's."""
    local_minor = ".".join(local_version.split(".")[:2])
    container_minor = container_version.split("-")[0]
    if local_minor != container_minor:
        raise RuntimeError(
            f"Local scikit-learn {local_version} does not match container "
            f"{container_version}. Align pyproject.toml and SKLEARN_CONTAINER_VERSION."
        )


def train_model(
    features: pd.DataFrame, labels: pd.Series, seed: int = 42
) -> tuple[HistGradientBoostingClassifier, pd.DataFrame, dict[str, float]]:
    x_train, x_test, y_train, y_test = train_test_split(
        features, labels, test_size=0.25, stratify=labels, random_state=seed
    )
    model = HistGradientBoostingClassifier(max_iter=200, random_state=seed)
    model.fit(x_train, y_train)

    probabilities = model.predict_proba(x_test)[:, 1]
    metrics = {
        "roc_auc": float(roc_auc_score(y_test, probabilities)),
        "accuracy": float(accuracy_score(y_test, (probabilities >= 0.5).astype(int))),
        "train_rows": float(len(x_train)),
        "test_rows": float(len(x_test)),
    }

    # The held-out set becomes the "what normal looks like" baseline for drift checks later.
    reference = x_test.copy()
    reference["label"] = y_test.to_numpy()
    reference["probability"] = probabilities
    return model, reference, metrics


def main() -> None:
    check_sklearn_matches_container(sklearn.__version__, SKLEARN_CONTAINER_VERSION)

    raw = pd.read_csv(download_koi(RAW_KOI_FILE))
    features, labels = prepare(raw)
    model, reference, metrics = train_model(features, labels)

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_FILE)
    reference.to_csv(REFERENCE_FILE, index=False)
    METRICS_FILE.write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
