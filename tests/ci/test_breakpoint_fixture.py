from __future__ import annotations

import ipaddress
import json
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from apps.shared.schemas.workflow import EdgeSchema, NodeSchema, ViewportSchema
from apps.shared.services.egress_guard import OutboundEgressGuard
from apps.workflow_engine.adapters.outbound_http import generic_http_egress_policy
from apps.workflow_engine.adapters.providers.generic_http import GenericHttpEffectAdapter
from apps.workflow_engine.application.outbound_http import (
    OutboundHttpRequest,
    OutboundHttpResponse,
)
from apps.workflow_engine.workflow.core.workflow_node_factory import NodeFactory
from tests.load.breakpoint_fixture import (
    BREAKPOINT_MOCK_ENDPOINT,
    build_io_workflow_graph,
    validate_mock_endpoint,
)
from tests.load.breakpoint_mock import (
    DelayBoundsError,
    MarkerError,
    MockState,
    parse_delay_seconds,
    validate_marker,
    running_mock_server,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_io_fixture_uses_read_only_http_and_preserves_the_existing_api_contract() -> None:
    delay_seconds = 10
    graph = build_io_workflow_graph(
        delay_seconds=delay_seconds,
        endpoint=BREAKPOINT_MOCK_ENDPOINT,
    )

    assert [node["type"] for node in graph["nodes"]] == [
        "startNode",
        "httpRequestNode",
        "answerNode",
    ]
    start, http, answer = graph["nodes"]
    assert start["data"]["variables"] == [
        {
            "id": "message",
            "name": "message",
            "label": "Message",
            "type": "paragraph",
            "required": True,
            "maxLength": 128,
            "max_length": 128,
        }
    ]
    assert http["data"]["method"] == "GET"
    assert http["data"]["timeout"] == 30_000
    assert http["data"]["authType"] == "none"
    assert http["data"]["referenced_variables"] == [
        {
            "name": "marker",
            "value_selector": [start["id"], "message"],
        }
    ]
    assert http["data"]["url"] == (
        f"{BREAKPOINT_MOCK_ENDPOINT}/delay"
        f"?seconds={delay_seconds}&marker={{{{ marker }}}}"
    )
    assert answer["data"]["outputs"] == [
        {
            "variable": "answer_text",
            "label": "Answer",
            "value_selector": [http["id"], "data", "answer_text"],
        },
        {
            "variable": "delay_seconds",
            "label": "Delay seconds",
            "value_selector": [http["id"], "data", "delay_seconds"],
        },
    ]
    assert graph["edges"] == [
        {
            "id": "edge-breakpoint-start-http",
            "source": start["id"],
            "target": http["id"],
        },
        {
            "id": "edge-breakpoint-http-answer",
            "source": http["id"],
            "target": answer["id"],
        },
    ]


@pytest.mark.parametrize(
    "endpoint",
    (
        "ftp://breakpoint-mock.nodease.invalid",
        "http://user@breakpoint-mock.nodease.invalid",
        "http://breakpoint-mock.nodease.invalid:8080",
        "http://breakpoint-mock.nodease.invalid/path",
        "http://breakpoint-mock.nodease.invalid?query=yes",
        "http://127.0.0.1",
        "http://example.com",
    ),
)
def test_fixture_endpoint_rejects_unsafe_or_non_mock_targets(
    endpoint: str,
) -> None:
    with pytest.raises(ValueError, match="breakpoint mock endpoint"):
        validate_mock_endpoint(endpoint)


@pytest.mark.parametrize("delay_seconds", (9, 30))
def test_fixture_builder_rejects_unsupported_workflow_delays(
    delay_seconds: int,
) -> None:
    with pytest.raises(ValueError, match="only the 10-second workflow is supported"):
        build_io_workflow_graph(
            delay_seconds=delay_seconds,
            endpoint=BREAKPOINT_MOCK_ENDPOINT,
        )


def test_io_fixture_instantiates_and_executes_the_real_node_contracts() -> None:
    marker = "bp-contract-001"
    graph = build_io_workflow_graph(delay_seconds=10)
    node_schemas = [NodeSchema(**node) for node in graph["nodes"]]
    assert [EdgeSchema(**edge).id for edge in graph["edges"]] == [
        "edge-breakpoint-start-http",
        "edge-breakpoint-http-answer",
    ]
    assert ViewportSchema(**graph["viewport"]).zoom == 0.85
    nodes = {schema.id: NodeFactory.create(schema) for schema in node_schemas}

    class FixtureOutbound:
        def __init__(self) -> None:
            self.requests: list[OutboundHttpRequest] = []

        def send(self, request: OutboundHttpRequest) -> OutboundHttpResponse:
            self.requests.append(request)
            content = json.dumps(
                {
                    "marker": marker,
                    "answer_text": f"Nodease load response: {marker}",
                    "delay_seconds": 10.0,
                }
            ).encode("utf-8")
            return OutboundHttpResponse(
                status_code=200,
                headers=(("content-type", "application/json"),),
                content=content,
            )

    start_output = nodes["start-breakpoint-io"].execute({"message": marker})
    outbound = FixtureOutbound()
    with patch(
        "apps.workflow_engine.workflow.nodes.http.http_node."
        "build_generic_http_effect_adapter",
        return_value=GenericHttpEffectAdapter(outbound_http=outbound),
    ):
        http_output = nodes["http-breakpoint-io"].execute(
            {"start-breakpoint-io": start_output}
        )
    answer_output = nodes["answer-breakpoint-io"].execute(
        {
            "start-breakpoint-io": start_output,
            "http-breakpoint-io": http_output,
        }
    )

    assert len(outbound.requests) == 1
    request = outbound.requests[0]
    assert request.method == "GET"
    assert request.url == (
        f"{BREAKPOINT_MOCK_ENDPOINT}/delay?seconds=10&marker={marker}"
    )
    assert request.timeout_seconds == 30.0
    assert answer_output == {
        "answer_text": f"Nodease load response: {marker}",
        "delay_seconds": 10.0,
    }


def test_mock_address_is_accepted_by_the_existing_generic_http_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_address = "93.184.216.254"

    def resolve_mock(host: str, port: int, *, type: int):
        assert host == "breakpoint-mock.nodease.invalid"
        assert port == 80
        assert type == socket.SOCK_STREAM
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mock_address, port))]

    monkeypatch.setattr(
        "apps.shared.services.egress_guard.socket.getaddrinfo",
        resolve_mock,
    )
    guard = OutboundEgressGuard(generic_http_egress_policy())

    assert guard.validate_host_port_addresses(
        "breakpoint-mock.nodease.invalid",
        80,
    ) == ("breakpoint-mock.nodease.invalid", 80, (mock_address,))


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    (("0", 0.0), ("0.125", 0.125), ("10", 10.0), ("30", 30.0)),
)
def test_mock_delay_parser_accepts_only_the_bounded_range(
    raw_value: str,
    expected: float,
) -> None:
    assert parse_delay_seconds(raw_value) == expected


@pytest.mark.parametrize("raw_value", (None, "", "-0.1", "30.001", "nan", "inf"))
def test_mock_delay_parser_rejects_missing_or_out_of_range_values(
    raw_value: str | None,
) -> None:
    with pytest.raises(DelayBoundsError):
        parse_delay_seconds(raw_value)


def test_mock_marker_is_bounded_and_safe_for_the_fixture_query() -> None:
    assert validate_marker("bp-20260921_001:abc.def") == "bp-20260921_001:abc.def"

    for value in (None, "", "spaces are unsafe", "../path", "x" * 129):
        with pytest.raises(MarkerError):
            validate_marker(value)


def test_mock_echoes_marker_after_injected_delay_and_metrics_never_store_it() -> None:
    sleeps: list[float] = []
    marker = "secret-marker-canary"
    state = MockState(max_concurrency=2, sleep=sleeps.append)

    with running_mock_server(state=state) as server:
        base_url = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(
            f"{base_url}/delay?seconds=0.25&marker={marker}", timeout=2
        ) as response:
            payload = json.load(response)
        with urllib.request.urlopen(f"{base_url}/metrics", timeout=2) as response:
            metrics = json.load(response)

    assert payload == {
        "marker": marker,
        "answer_text": f"Nodease load response: {marker}",
        "delay_seconds": 0.25,
    }
    assert sleeps == [0.25]
    assert metrics == {
        "active_requests": 0,
        "max_active_requests": 1,
        "request_count": 1,
        "success_count": 1,
        "error_count": 0,
        "rejected_count": 0,
        "configured_max_concurrency": 2,
    }
    assert marker not in json.dumps(metrics, sort_keys=True)


def test_mock_rejects_above_concurrency_bound_and_accounts_for_rejection() -> None:
    entered_sleep = threading.Event()
    release_sleep = threading.Event()

    def blocking_sleep(_seconds: float) -> None:
        entered_sleep.set()
        assert release_sleep.wait(timeout=2)

    state = MockState(max_concurrency=1, sleep=blocking_sleep)
    first_result: list[dict[str, object]] = []

    with running_mock_server(state=state) as server:
        base_url = f"http://127.0.0.1:{server.server_port}"

        def first_request() -> None:
            with urllib.request.urlopen(
                f"{base_url}/delay?seconds=1&marker=first", timeout=2
            ) as response:
                first_result.append(json.load(response))

        thread = threading.Thread(target=first_request)
        thread.start()
        assert entered_sleep.wait(timeout=2)

        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(
                f"{base_url}/delay?seconds=1&marker=second", timeout=2
            )
        assert captured.value.code == 503

        release_sleep.set()
        thread.join(timeout=2)
        assert not thread.is_alive()

        with urllib.request.urlopen(f"{base_url}/metrics", timeout=2) as response:
            metrics = json.load(response)

    assert first_result == [
        {
            "marker": "first",
            "answer_text": "Nodease load response: first",
            "delay_seconds": 1.0,
        }
    ]
    assert metrics["max_active_requests"] == 1
    assert metrics["request_count"] == 2
    assert metrics["success_count"] == 1
    assert metrics["rejected_count"] == 1
    assert metrics["error_count"] == 0


def test_breakpoint_compose_routes_only_proxy_to_mock_on_an_internal_network() -> None:
    payload = yaml.safe_load(
        (REPOSITORY_ROOT / "tests/load/docker-compose.breakpoint.yml").read_text(
            encoding="utf-8"
        )
    )
    services = payload["services"]
    networks = payload["networks"]
    route_network = networks["breakpoint-mock-route"]
    ingress_network = networks["breakpoint-mock-ingress"]

    assert set(networks) == {"breakpoint-mock-route", "breakpoint-mock-ingress"}
    assert route_network["internal"] is True
    assert route_network["ipam"]["config"] == [
        {"subnet": "93.184.216.248/29"}
    ]
    assert ingress_network == {"driver": "bridge"}
    assert set(services) == {"workflow_engine", "proxy", "breakpoint_mock"}
    assert services["workflow_engine"] == {
        "extra_hosts": ["breakpoint-mock.nodease.invalid=93.184.216.254"],
        "depends_on": {
            "breakpoint_mock": {"condition": "service_healthy"},
        },
    }
    assert services["proxy"]["extra_hosts"] == [
        "breakpoint-mock.nodease.invalid=93.184.216.254"
    ]
    assert set(services["proxy"]) == {"extra_hosts", "networks"}
    assert "breakpoint-mock-route" in services["proxy"]["networks"]
    assert services["breakpoint_mock"]["networks"]["breakpoint-mock-route"] == {
        "ipv4_address": "93.184.216.254"
    }
    route_members = {
        name
        for name, service in services.items()
        if "breakpoint-mock-route" in service.get("networks", {})
    }
    assert route_members == {"proxy", "breakpoint_mock"}
    ingress_members = {
        name
        for name, service in services.items()
        if "breakpoint-mock-ingress" in service.get("networks", {})
    }
    assert ingress_members == {"breakpoint_mock"}
    assert services["breakpoint_mock"]["ports"] == [
        "127.0.0.1:${BREAKPOINT_MOCK_HOST_PORT:-18081}:80"
    ]
    assert services["breakpoint_mock"]["image"] == (
        "${BREAKPOINT_MOCK_IMAGE:-docker-gateway:latest}"
    )
    assert services["breakpoint_mock"]["entrypoint"] == [
        "python",
        "/mock.py",
        "--host",
        "0.0.0.0",
        "--port",
        "80",
        "--max-concurrency",
        "${BREAKPOINT_MOCK_MAX_CONCURRENCY:-300}",
    ]
    assert services["breakpoint_mock"]["volumes"] == [
        {
            "type": "bind",
            "source": "../tests/load/breakpoint_mock.py",
            "target": "/mock.py",
            "read_only": True,
        }
    ]
    assert services["breakpoint_mock"]["expose"] == ["80"]
    assert services["breakpoint_mock"]["read_only"] is True
    assert services["breakpoint_mock"]["cap_drop"] == ["ALL"]
    assert services["breakpoint_mock"]["cap_add"] == ["NET_BIND_SERVICE"]
    assert services["breakpoint_mock"]["security_opt"] == [
        "no-new-privileges:true"
    ]
    assert services["breakpoint_mock"]["pids_limit"] == 384
    assert services["breakpoint_mock"]["mem_limit"] == "512m"

    mock_address = ipaddress.ip_address("93.184.216.254")
    assert mock_address.is_global
    route_subnet = ipaddress.ip_network(route_network["ipam"]["config"][0]["subnet"])
    assert mock_address in route_subnet

    base_compose = yaml.safe_load(
        (REPOSITORY_ROOT / "docker/docker-compose.yml").read_text(encoding="utf-8")
    )
    base_subnets = [
        ipaddress.ip_network(config["subnet"])
        for network in base_compose["networks"].values()
        for config in network.get("ipam", {}).get("config", [])
    ]
    assert all(not route_subnet.overlaps(base_subnet) for base_subnet in base_subnets)
