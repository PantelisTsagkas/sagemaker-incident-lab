"""Build model.tar.gz in the layout the SageMaker scikit-learn container expects.

    model.tar.gz
    ├── model.joblib
    └── code/
        └── inference.py

SageMaker extracts this to /opt/ml/model. The env vars set in deploy.py
(SAGEMAKER_PROGRAM, SAGEMAKER_SUBMIT_DIRECTORY) point the container at code/.
"""

import tarfile
from pathlib import Path

from incident_lab.config import ARCHIVE_FILE, MODEL_FILE, SERVING_DIR


def build_model_archive(model_file: Path, serving_dir: Path, archive_file: Path) -> Path:
    if not model_file.exists():
        raise FileNotFoundError(
            f"{model_file} not found. Run `uv run python -m incident_lab.train`."
        )

    archive_file.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_file, "w:gz") as tar:
        tar.add(model_file, arcname="model.joblib")
        tar.add(serving_dir / "inference.py", arcname="code/inference.py")
    return archive_file


def main() -> None:
    archive = build_model_archive(MODEL_FILE, SERVING_DIR, ARCHIVE_FILE)
    print(f"Built {archive} ({archive.stat().st_size / 1024:.1f} KiB)")


if __name__ == "__main__":
    main()
