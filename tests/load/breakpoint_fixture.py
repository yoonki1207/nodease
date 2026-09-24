"""Pure workflow graph fixture for the provider-free breakpoint test."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import urlsplit, urlunsplit


BREAKPOINT_MOCK_HOST = "breakpoint-mock.nodease.invalid"
BREAKPOINT_MOCK_ENDPOINT = f"http://{BREAKPOINT_MOCK_HOST}"
SUPPORTED_WORKFLOW_DELAY_SECONDS = (10,)
UNSUPPORTED_WORKFLOW_DELAY_SECONDS = {
    30: (
        "the HTTP node and generic egress guard both cap timeout at 30 seconds; "
        "a 30-second response has no transport headroom"
    )
}


def validate_mock_endpoint(endpoint: str) -> str:
    """Return the canonical port-80 endpoint accepted by the HTTP node policy."""

    try:
        parsed = urlsplit(str(endpoint or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("breakpoint mock endpoint is invalid") from exc

    if (
        parsed.scheme.lower() != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("breakpoint mock endpoint is invalid")

    host = parsed.hostname.lower().rstrip(".")
    if host != BREAKPOINT_MOCK_HOST:
        raise ValueError("breakpoint mock endpoint is invalid")

    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit(("http", netloc, "", "", ""))


def build_io_workflow_graph(
    *,
    delay_seconds: int,
    endpoint: str = BREAKPOINT_MOCK_ENDPOINT,
) -> dict[str, Any]:
    """Build the supported Start -> delayed GET -> Answer workflow.

    The 30-second mock delay is useful for direct load-generator qualification,
    but cannot be a valid workflow fixture while the production HTTP boundary
    also has an exact 30-second maximum. It is rejected here rather than
    producing a graph whose normal network overhead makes it fail by design.
    """

    if delay_seconds not in SUPPORTED_WORKFLOW_DELAY_SECONDS:
        raise ValueError(
            "only the 10-second workflow is supported; the 30-second mock delay "
            "equals the runtime timeout ceiling"
        )
    safe_endpoint = validate_mock_endpoint(endpoint)

    graph: dict[str, Any] = {
        "nodes": [
            {
                "id": "start-breakpoint-io",
                "type": "startNode",
                "position": {"x": 120, "y": 120},
                "data": {
                    "title": "Breakpoint test input",
                    "description": "Accept a synthetic request marker.",
                    "displayNumber": 1,
                    "visibleProperties": [],
                    "triggerType": "manual",
                    "trigger_type": "manual",
                    "variables": [
                        {
                            "id": "message",
                            "name": "message",
                            "label": "Message",
                            "type": "paragraph",
                            "required": True,
                            "maxLength": 128,
                            "max_length": 128,
                        }
                    ],
                },
            },
            {
                "id": "http-breakpoint-io",
                "type": "httpRequestNode",
                "position": {"x": 540, "y": 120},
                "data": {
                    "title": "Wait on breakpoint mock",
                    "description": "Perform provider-free delayed HTTP I/O.",
                    "displayNumber": 2,
                    "visibleProperties": [],
                    "method": "GET",
                    "url": (
                        f"{safe_endpoint}/delay?seconds={delay_seconds}"
                        "&marker={{ marker }}"
                    ),
                    "headers": [],
                    "body": None,
                    "timeout": 30_000,
                    "authType": "none",
                    "authConfig": {},
                    "referenced_variables": [
                        {
                            "name": "marker",
                            "value_selector": ["start-breakpoint-io", "message"],
                        }
                    ],
                },
            },
            {
                "id": "answer-breakpoint-io",
                "type": "answerNode",
                "position": {"x": 960, "y": 120},
                "data": {
                    "title": "Return delayed result",
                    "description": "Return the deterministic marker response.",
                    "displayNumber": 3,
                    "visibleProperties": [],
                    "outputs": [
                        {
                            "variable": "answer_text",
                            "label": "Answer",
                            "value_selector": [
                                "http-breakpoint-io",
                                "data",
                                "answer_text",
                            ],
                        },
                        {
                            "variable": "delay_seconds",
                            "label": "Delay seconds",
                            "value_selector": [
                                "http-breakpoint-io",
                                "data",
                                "delay_seconds",
                            ],
                        },
                    ],
                },
            },
        ],
        "edges": [
            {
                "id": "edge-breakpoint-start-http",
                "source": "start-breakpoint-io",
                "target": "http-breakpoint-io",
            },
            {
                "id": "edge-breakpoint-http-answer",
                "source": "http-breakpoint-io",
                "target": "answer-breakpoint-io",
            },
        ],
        "viewport": {"x": 40, "y": 80, "zoom": 0.85},
    }
    return copy.deepcopy(graph)


__all__ = [
    "BREAKPOINT_MOCK_ENDPOINT",
    "BREAKPOINT_MOCK_HOST",
    "SUPPORTED_WORKFLOW_DELAY_SECONDS",
    "UNSUPPORTED_WORKFLOW_DELAY_SECONDS",
    "build_io_workflow_graph",
    "validate_mock_endpoint",
]
