"""Staircase load: add users in steps to find where one instance stops keeping up.

Each user sends 2 requests per second, so users x 2 = offered requests per second.
Defaults: start at 40 users (80/s), add 20 users (40/s) every 2 minutes, stop after
200 users (400/s). That is 9 steps, 18 minutes.

Env vars: RAMP_START_USERS, RAMP_STEP_USERS, RAMP_STEP_SECONDS, RAMP_MAX_USERS.

    uv run locust -f load/ramp.py --headless --processes 4 --csv results/inc003_ramp

A shape class overrides -u, -r and -t. It lives in its own file so the baseline
command (locustfile.py with -u/-r/-t) still runs exactly as before.
"""

import os

from locust import LoadTestShape
from locustfile import SageMakerUser  # noqa: F401 - Locust finds the User class here

START_USERS = int(os.environ.get("RAMP_START_USERS", "40"))
STEP_USERS = int(os.environ.get("RAMP_STEP_USERS", "20"))
STEP_SECONDS = int(os.environ.get("RAMP_STEP_SECONDS", "120"))
MAX_USERS = int(os.environ.get("RAMP_MAX_USERS", "200"))


def ramp_users(run_seconds: float) -> int | None:
    """Users for this moment of the run, or None when the last step is over."""
    step = int(run_seconds // STEP_SECONDS)
    users = START_USERS + step * STEP_USERS
    if users > MAX_USERS:
        return None
    return users


class StepRamp(LoadTestShape):
    def tick(self) -> tuple[int, float] | None:
        users = ramp_users(self.get_run_time())
        if users is None:
            return None
        return users, 10  # add new users at 10 per second, so each step lands in 2 seconds
