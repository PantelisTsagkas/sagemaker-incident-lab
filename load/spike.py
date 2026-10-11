"""Traffic spike: steady load, a sudden jump, then back to steady.

Each user sends 2 requests per second, so users x 2 = offered requests per second.
Defaults: 40 users (80/s) for 5 minutes, 160 users (320/s) for 15 minutes, then
40 users for 10 minutes. 30 minutes in total. The spike is added within a few
seconds, which is what makes it a spike rather than a ramp.

Env vars: SPIKE_BASE_USERS, SPIKE_PEAK_USERS, SPIKE_BEFORE_SECONDS,
SPIKE_PEAK_SECONDS, SPIKE_AFTER_SECONDS.

    uv run locust -f load/spike.py --headless --processes 4 --csv results/inc003_spike
"""

import os

from locust import LoadTestShape
from locustfile import SageMakerUser  # noqa: F401 - Locust finds the User class here

BASE_USERS = int(os.environ.get("SPIKE_BASE_USERS", "40"))
PEAK_USERS = int(os.environ.get("SPIKE_PEAK_USERS", "160"))
BEFORE_SECONDS = int(os.environ.get("SPIKE_BEFORE_SECONDS", "300"))
PEAK_SECONDS = int(os.environ.get("SPIKE_PEAK_SECONDS", "900"))
AFTER_SECONDS = int(os.environ.get("SPIKE_AFTER_SECONDS", "600"))


def spike_users(run_seconds: float) -> int | None:
    """Users for this moment of the run, or None when the run is over."""
    if run_seconds < BEFORE_SECONDS:
        return BASE_USERS
    if run_seconds < BEFORE_SECONDS + PEAK_SECONDS:
        return PEAK_USERS
    if run_seconds < BEFORE_SECONDS + PEAK_SECONDS + AFTER_SECONDS:
        return BASE_USERS
    return None


class Spike(LoadTestShape):
    def tick(self) -> tuple[int, float] | None:
        users = spike_users(self.get_run_time())
        if users is None:
            return None
        return users, 50  # 50 new users per second: the jump to peak lands in about 2 seconds
