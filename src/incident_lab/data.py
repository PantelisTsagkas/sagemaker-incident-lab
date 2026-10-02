"""Kepler Objects of Interest: download and turn into a binary classification table."""

import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

# Keep in sync with serving/inference.py (a test enforces this).
FEATURES: list[str] = [
    "koi_period",
    "koi_duration",
    "koi_depth",
    "koi_prad",
    "koi_teq",
    "koi_insol",
    "koi_model_snr",
    "koi_impact",
    "koi_steff",
    "koi_slogg",
    "koi_srad",
]
LABEL_SOURCE = "koi_disposition"
POSITIVE = "CONFIRMED"
NEGATIVE = "FALSE POSITIVE"

_TAP_URL = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"


def koi_query_url() -> str:
    columns = ", ".join(["kepoi_name", LABEL_SOURCE, *FEATURES])
    query = f"select {columns} from cumulative"
    return f"{_TAP_URL}?{urllib.parse.urlencode({'query': query, 'format': 'csv'})}"


def download_koi(dest: Path) -> Path:
    """Download the catalogue once; later runs reuse the local copy."""
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(koi_query_url(), dest)
    return dest


def prepare(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Keep only settled labels: CONFIRMED (1) vs FALSE POSITIVE (0).

    CANDIDATE rows are unresolved, so they are dropped rather than guessed.
    Missing feature values are kept: HistGradientBoosting handles NaN natively.
    """
    settled = raw[raw[LABEL_SOURCE].isin([POSITIVE, NEGATIVE])]
    features = settled[FEATURES].astype("float64").reset_index(drop=True)
    labels = (settled[LABEL_SOURCE] == POSITIVE).astype("int64").reset_index(drop=True)
    labels.name = "label"
    return features, labels
