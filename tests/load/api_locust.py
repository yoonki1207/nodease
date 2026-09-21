"""Paced authenticated deployment executions."""

from tests.load.locust_users import ApiWorkflowUserBase


class ApiWorkflowUser(ApiWorkflowUserBase):
    abstract = False
