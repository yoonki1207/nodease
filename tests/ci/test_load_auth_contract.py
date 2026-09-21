from __future__ import annotations

import ast
from pathlib import Path

import pytest


LOAD_SCRIPT_DIR = Path(__file__).resolve().parents[1] / "load"
LOAD_SCRIPTS = (
    *(LOAD_SCRIPT_DIR / f"load{index}.py" for index in range(1, 4)),
    LOAD_SCRIPT_DIR / "smoke_test.py",
)
ENV_EXAMPLE = Path(__file__).resolve().parents[2] / "dev" / ".env.example"


@pytest.mark.parametrize("script_path", LOAD_SCRIPTS, ids=lambda path: path.name)
def test_load_script_uses_bearer_without_secret_preview(script_path: Path) -> None:
    source = script_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    string_literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert "X-Auth-Secret" not in string_literals
    assert "Authorization" in string_literals
    assert "token_preview" not in source

    authorization_values = [
        value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant) and key.value == "Authorization"
    ]
    assert len(authorization_values) == 1
    assert isinstance(authorization_values[0], ast.JoinedStr)
    assert any(
        isinstance(value, ast.Constant) and value.value == "Bearer "
        for value in authorization_values[0].values
    )


def test_load_environment_guidance_uses_bearer_contract() -> None:
    guidance = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert guidance.find("X-Auth-Secret") == -1
    assert guidance.find("Authorization: Bearer") >= 0


def test_smoke_uses_the_private_runtime_manifest_instead_of_the_root_env() -> None:
    source = (LOAD_SCRIPT_DIR / "smoke_test.py").read_text(encoding="utf-8")

    assert "load_runtime_manifest" in source
    assert "validate_target_host" in source
    assert "events.test_start" in source
    assert "self.client.trust_env = False" in source
    assert "timeout=REQUEST_TIMEOUT_SECONDS" in source
    assert "allow_redirects=False" in source
    assert "LocalRunner" in source
    assert "raise StopTest" in source
    assert "load_dotenv" not in source
    assert 'ROOT_DIR / ".env"' not in source


@pytest.mark.parametrize(
    "script_path",
    tuple(LOAD_SCRIPT_DIR / f"load{index}.py" for index in range(1, 4)),
    ids=lambda path: path.name,
)
def test_legacy_load_scripts_never_log_response_bodies(script_path: Path) -> None:
    source = script_path.read_text(encoding="utf-8")

    assert "response.text" not in source
    assert "Workflow failed: {data}" not in source


def test_queue_monitor_has_no_embedded_redis_credential_or_kubernetes_target() -> None:
    source = (LOAD_SCRIPT_DIR / "monitor_queue.py").read_text(encoding="utf-8")

    assert "REDIS_PASSWORD" not in source
    assert '"kubectl"' not in source
    assert "nodease-loadtest" in source
