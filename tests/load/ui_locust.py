"""Twenty-five authenticated builder sessions."""

from tests.load.locust_users import UiWorkflowUserBase


class UiWorkflowUser(UiWorkflowUserBase):
    abstract = False
