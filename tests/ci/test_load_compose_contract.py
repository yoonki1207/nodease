from __future__ import annotations

import ast
import importlib
import stat
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from urllib.parse import urlparse

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LOAD_MANAGER_MODULE = "tests.load.manage_compose"
RUN_BENCHMARK_PATH = REPOSITORY_ROOT / "tests" / "load" / "run_benchmark.py"
RUN_SCENARIO_PATH = REPOSITORY_ROOT / "tests" / "load" / "run_scenario.py"
SMOKE_TEST_PATH = REPOSITORY_ROOT / "tests" / "load" / "smoke_test.py"
LOCUST_USERS_PATH = REPOSITORY_ROOT / "tests" / "load" / "locust_users.py"
DOCKERIGNORE_PATH = REPOSITORY_ROOT / ".dockerignore"
FRONTEND_DOCKERFILE_PATH = REPOSITORY_ROOT / "docker" / "client" / "Dockerfile"
GATEWAY_DOCKERFILE_PATH = REPOSITORY_ROOT / "docker" / "gateway" / "Dockerfile"
WORKFLOW_DOCKERFILE_PATH = (
    REPOSITORY_ROOT / "docker" / "workflow_engine" / "Dockerfile"
)
LOG_SYSTEM_DOCKERFILE_PATH = (
    REPOSITORY_ROOT / "docker" / "log_system" / "Dockerfile"
)
SANDBOX_DOCKERFILE_PATH = REPOSITORY_ROOT / "docker" / "sandbox" / "Dockerfile"


def _load_manager() -> ModuleType:
    """Load the future pure orchestration seam without starting Docker."""

    return importlib.import_module(LOAD_MANAGER_MODULE)


def _recording_runner(commands: list[tuple[str, ...]]):
    def run(command: Sequence[str]) -> int:
        commands.append(tuple(command))
        return 0

    return run


def _project_name(command: tuple[str, ...]) -> str | None:
    for flag in ("--project-name", "-p"):
        if flag in command:
            index = command.index(flag)
            if index + 1 < len(command):
                return command[index + 1]
    return None


def test_default_down_is_fixed_project_scoped_and_preserves_volumes() -> None:
    manager = _load_manager()
    commands: list[tuple[str, ...]] = []

    manager.run_action(
        "down",
        container_owners={},
        environment={},
        run_command=_recording_runner(commands),
        emit=lambda _message: None,
    )

    down_commands = [command for command in commands if "down" in command]
    assert manager.PROJECT_NAME == "nodease-loadtest"
    assert len(down_commands) == 1
    assert down_commands[0][:4] == (
        "docker",
        "--context",
        "default",
        "compose",
    )
    assert _project_name(down_commands[0]) == "nodease-loadtest"
    assert "-v" not in down_commands[0]
    assert "--volumes" not in down_commands[0]


def test_compose_actions_use_the_dedicated_gitignored_environment_file() -> None:
    manager = _load_manager()
    commands: list[tuple[str, ...]] = []

    manager.run_action(
        "doctor",
        container_owners={},
        environment={},
        run_command=_recording_runner(commands),
        emit=lambda _message: None,
    )

    assert len(commands) == 1
    command = commands[0]
    assert "--env-file" in command
    env_path = Path(command[command.index("--env-file") + 1])
    assert env_path == manager.ENV_FILE
    assert env_path.name == ".env.load.local"
    project_directory = Path(
        command[command.index("--project-directory") + 1]
    )
    assert project_directory == manager.COMPOSE_FILE.parent
    compose_files = [
        Path(command[index + 1])
        for index, value in enumerate(command)
        if value == "--file"
    ]
    assert compose_files == [manager.COMPOSE_FILE, manager.COMPOSE_OVERRIDE_FILE]


def test_queue_monitor_uses_the_same_compose_path_boundary() -> None:
    monitor = importlib.import_module("tests.load.monitor_queue")
    command = monitor._compose_prefix()

    project_directory = Path(
        command[command.index("--project-directory") + 1]
    )
    assert project_directory == monitor.COMPOSE_FILE.parent


def test_load_override_exposes_only_nginx_on_ipv4_loopback() -> None:
    manager = _load_manager()
    source = manager.COMPOSE_OVERRIDE_FILE.read_text(encoding="utf-8")

    assert "postgres:" in source
    assert "redis:" in source
    assert "sandbox:" in source
    assert source.count("ports: !override []") == 3
    assert '"127.0.0.1:18080:80"' in source
    assert '"127.0.0.1:80:80"' not in source
    assert '"5432:5432"' not in source
    assert '"6379:6379"' not in source
    assert '"8194:8194"' not in source


def test_load_override_gives_only_nginx_a_host_reachable_ingress_network() -> None:
    manager = _load_manager()
    source = manager.COMPOSE_OVERRIDE_FILE.read_text(encoding="utf-8")

    assert source.count("load-ingress") == 2
    assert "networks:\n      - moduly-network\n      - load-ingress" in source
    assert "networks:\n  load-ingress:\n    driver: bridge" in source


def test_local_target_defaults_use_unprivileged_docker_desktop_port() -> None:
    expected_host = "http://127.0.0.1:18080"
    config = importlib.import_module("tests.load.config")
    runner = importlib.import_module("tests.load.run_scenario")

    assert config.DEFAULT_TARGET_HOST == expected_host
    assert runner._parser().parse_args(["ui"]).host == expected_host

    for path in (RUN_SCENARIO_PATH, SMOKE_TEST_PATH, LOCUST_USERS_PATH):
        source = path.read_text(encoding="utf-8")
        assert "DEFAULT_TARGET_HOST" in source
        assert '"http://127.0.0.1"' not in source

    benchmark_source = RUN_BENCHMARK_PATH.read_text(encoding="utf-8")
    assert f'DEFAULT_HOST = "{expected_host}"' in benchmark_source
    assert '"http://127.0.0.1"' not in benchmark_source


def test_provider_free_load_build_skips_unused_model_downloads() -> None:
    manager = _load_manager()
    source = manager.COMPOSE_OVERRIDE_FILE.read_text(encoding="utf-8")

    assert 'INSTALL_RAG_RERANKER: "false"' in source
    assert 'PREFETCH_MODEL_ROUTING_CLASSIFIER: "false"' in source


def test_load_stack_image_downloads_tolerate_transient_network_resets() -> None:
    frontend = FRONTEND_DOCKERFILE_PATH.read_text(encoding="utf-8")
    gateway = GATEWAY_DOCKERFILE_PATH.read_text(encoding="utf-8")
    workflow = WORKFLOW_DOCKERFILE_PATH.read_text(encoding="utf-8")
    log_system = LOG_SYSTEM_DOCKERFILE_PATH.read_text(encoding="utf-8")
    sandbox = SANDBOX_DOCKERFILE_PATH.read_text(encoding="utf-8")

    assert "NPM_CONFIG_FETCH_RETRIES=5" in frontend
    assert "NPM_CONFIG_FETCH_TIMEOUT=120000" in frontend
    for dockerfile in (gateway, workflow, log_system, sandbox):
        assert "PIP_DEFAULT_TIMEOUT=120" in dockerfile
        assert "PIP_RETRIES=10" in dockerfile


def test_docker_control_environment_cannot_redirect_to_a_remote_daemon() -> None:
    manager = _load_manager()

    sanitized = manager.sanitized_subprocess_environment(
        {
            "PATH": "/safe/path",
            "COMPOSE_PROJECT_NAME": "foreign",
            "DOCKER_HOST": "tcp://remote.example:2376",
            "DOCKER_CONTEXT": "remote-production",
        }
    )

    assert sanitized == {"PATH": "/safe/path"}


def test_compose_subprocess_drops_unrelated_host_credentials_and_flags() -> None:
    manager = _load_manager()
    sanitized = manager.sanitized_subprocess_environment(
        {
            "PATH": "/safe/path",
            "HOME": "/safe/home",
            "AWS_SECRET_ACCESS_KEY": "host-credential-canary",
            "MEMORY_PUBLIC_CAPABILITY_HMAC_KEY": "host-hmac-canary",
            "MEMORY_PUBLIC_CONVERSATION_ENABLED": "true",
            "LOAD_TEST_AUTH_TOKEN": "load-only-token",
        }
    )

    assert sanitized == {
        "PATH": "/safe/path",
        "HOME": "/safe/home",
        "LOAD_TEST_AUTH_TOKEN": "load-only-token",
    }


def test_environment_initialization_is_private_and_never_overwrites(tmp_path: Path) -> None:
    manager = _load_manager()
    path = tmp_path / ".env.load.local"
    messages: list[str] = []

    manager.initialize_environment_file(path, emit=messages.append)

    content = path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "LOAD_TEST_USER_PASSWORD=" in content
    assert "LOAD_TEST_AUTH_TOKEN=nodease_app_" in content
    assert "ENCRYPTION_KEY=" in content
    assert all(line not in "\n".join(messages) for line in content.splitlines())
    assert manager.load_environment_file(path)["NODE_ENV"] == "development"

    path.chmod(0o644)
    with pytest.raises(manager.ComposeActionError):
        manager.load_environment_file(path)
    path.chmod(0o600)

    with pytest.raises(manager.EnvironmentFileExistsError):
        manager.initialize_environment_file(path, emit=messages.append)


def test_seed_passes_secret_names_to_container_without_secret_values() -> None:
    manager = _load_manager()
    commands: list[tuple[str, ...]] = []
    password = "load-password-secret-canary"
    token = "load-token-secret-canary"

    manager.run_action(
        "seed",
        container_owners={"moduly-gateway": manager.PROJECT_NAME},
        environment={
            "LOAD_TEST_USER_PASSWORD": password,
            "LOAD_TEST_AUTH_TOKEN": token,
        },
        run_command=_recording_runner(commands),
        emit=lambda _message: None,
    )

    assert len(commands) == 1
    command = commands[0]
    assert "exec" in command
    assert "--no-tty" in command
    assert "LOAD_TEST_USER_PASSWORD" in command
    assert "LOAD_TEST_AUTH_TOKEN" in command
    assert password not in command
    assert token not in command
    assert command[-2].endswith("seed_load_test.py")
    assert command[-1] == "prepare"


def test_destroy_data_requires_exact_confirmation_and_only_targets_load_volumes() -> None:
    manager = _load_manager()
    commands: list[tuple[str, ...]] = []

    with pytest.raises(manager.DestructiveConfirmationError):
        manager.require_destroy_confirmation("docker")
    manager.require_destroy_confirmation(manager.PROJECT_NAME)
    manager.run_action(
        "destroy-data",
        container_owners={"moduly-postgres": manager.PROJECT_NAME},
        environment={},
        run_command=_recording_runner(commands),
        emit=lambda _message: None,
    )

    assert len(commands) == 1
    command = commands[0]
    assert _project_name(command) == manager.PROJECT_NAME
    assert "down" in command
    assert "--volumes" in command


@pytest.mark.parametrize("action", ("doctor", "up"))
def test_foreign_fixed_container_owner_fails_before_action(action: str) -> None:
    manager = _load_manager()
    commands: list[tuple[str, ...]] = []
    messages: list[str] = []

    assert "moduly-postgres" in manager.FIXED_CONTAINER_NAMES
    with pytest.raises(manager.ForeignContainerOwnerError):
        manager.run_action(
            action,
            container_owners={"moduly-postgres": "docker"},
            environment={},
            run_command=_recording_runner(commands),
            emit=messages.append,
        )

    assert commands == []


def test_action_failure_never_exposes_secret_values() -> None:
    manager = _load_manager()
    secret_canary = "nodease-load-secret-canary-do-not-print"
    messages: list[str] = []

    def fail_with_secret(_command: Sequence[str]) -> int:
        raise RuntimeError(f"docker failure included {secret_canary}")

    with pytest.raises(Exception) as captured:
        manager.run_action(
            "down",
            container_owners={},
            environment={"LOAD_TEST_AUTH_TOKEN": secret_canary},
            run_command=fail_with_secret,
            emit=messages.append,
        )

    assert secret_canary not in str(captured.value)
    assert secret_canary not in "\n".join(messages)


def test_master_key_is_redacted_and_private_env_files_never_enter_build_context() -> None:
    manager = _load_manager()
    secret_canary = "load-master-key-secret-canary"

    rendered = manager.redact_secrets(
        f"failure included {secret_canary}",
        {"MASTER_KEY": secret_canary},
    )
    dockerignore = DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines()

    assert secret_canary not in rendered
    assert "**/.env.*" in dockerignore
    assert "!**/.env.example" in dockerignore


def _literal_assignments(tree: ast.AST) -> dict[str, object]:
    assignments: dict[str, object] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if value is None:
            continue
        try:
            literal = ast.literal_eval(value)
        except (ValueError, TypeError):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                assignments[target.id] = literal
    return assignments


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    return next((item.value for item in call.keywords if item.arg == name), None)


def _resolved_literal(node: ast.AST | None, assignments: dict[str, object]) -> object:
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return assignments.get(node.id)
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def _host_argument(tree: ast.AST) -> ast.Call:
    matches = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_argument":
            continue
        names = {
            value
            for argument in node.args
            if isinstance((value := _resolved_literal(argument, {})), str)
        }
        if "--host" in names:
            matches.append(node)

    assert len(matches) == 1, "run_benchmark.py must define exactly one --host option"
    return matches[0]


def test_benchmark_target_is_explicit_or_defaults_to_loopback() -> None:
    tree = ast.parse(RUN_BENCHMARK_PATH.read_text(encoding="utf-8"))
    assignments = _literal_assignments(tree)
    host_argument = _host_argument(tree)

    required = _resolved_literal(_keyword(host_argument, "required"), assignments)
    if required is True:
        return

    default_host = _resolved_literal(_keyword(host_argument, "default"), assignments)
    assert isinstance(default_host, str), (
        "--host must be required or have an explicit loopback default"
    )
    parsed = urlparse(default_host)
    assert parsed.hostname in {"localhost", "127.0.0.1", "::1"}, (
        "load benchmarks must never default to a non-loopback target"
    )
