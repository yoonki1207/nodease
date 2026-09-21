"""Host ingress must work without opening protected workloads to direct egress."""

from pathlib import Path

import yaml


def test_nginx_has_host_ingress_while_application_networks_stay_internal():
    root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load((root / "docker/docker-compose.yml").read_text())
    networks = config["networks"]
    services = config["services"]
    ingress = {
        name for name in services["nginx"]["networks"]
        if not networks[name].get("internal", False)
    }
    assert ingress, "An internal-only Nginx cannot publish its host port"
    assert services["nginx"]["ports"] == ["127.0.0.1:80:80"]
    for name, service in services.items():
        if name in {"nginx", "proxy"}:
            continue
        assert not ingress.intersection(service["networks"]), name
        assert all(networks[network].get("internal", False)
                   for network in service["networks"]), name
