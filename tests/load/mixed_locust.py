"""Exact UI-session and paced API-worker counts in one local run."""

import os

from tests.load.locust_users import ApiWorkflowUserBase, UiWorkflowUserBase


UI_FIXED_COUNT = int(os.getenv("LOAD_TEST_UI_USERS", "25"))
API_FIXED_COUNT = int(os.getenv("LOAD_TEST_API_WORKERS", "15"))
if UI_FIXED_COUNT <= 0 or API_FIXED_COUNT <= 0:
    raise ValueError("mixed fixed counts must be positive")


class MixedUiWorkflowUser(UiWorkflowUserBase):
    abstract = False
    fixed_count = UI_FIXED_COUNT


class MixedApiWorkflowUser(ApiWorkflowUserBase):
    abstract = False
    fixed_count = API_FIXED_COUNT
