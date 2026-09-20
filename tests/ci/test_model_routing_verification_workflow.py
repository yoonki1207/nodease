import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = (
    REPOSITORY_ROOT
    / ".github"
    / "workflows"
    / "test-model-routing-verification.yml"
)
EXTERNAL_ACTION_PATTERN = re.compile(
    r"^\s*(?:-\s*)?uses:\s+(?P<action>[^@\s]+)@(?P<reference>[^\s#]+)"
)
EXPECTED_TEST_TARGETS = {
    "apps/workflow_engine/tests/services/"
    "test_model_routing_verification_contract.py",
    "tests/ci/test_model_routing_verification_workflow.py",
    "tests/db/test_model_routing_activation_verification.py",
    "tests/experiments/test_model_routing_verification.py",
    "tests/experiments/test_model_routing_verification_cases.py",
    "tests/experiments/test_model_routing_verification_runtime.py",
    "tests/scripts/test_model_routing_verification_provider.py",
}
JUNIT_PATH = "artifacts/model-routing-verification.junit.xml"


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _pytest_step(workflow: str) -> str:
    return workflow.split(
        "- name: Run model routing verification contracts",
        maxsplit=1,
    )[1].split("- name: Upload machine-readable verification evidence", maxsplit=1)[0]


def test_workflow_supports_manual_and_reusable_execution():
    workflow = _workflow()
    trigger_block = workflow.split("permissions:", maxsplit=1)[0]

    assert "workflow_dispatch:" in trigger_block
    assert "workflow_call:" in trigger_block


def test_isolated_verification_branch_push_can_run_the_new_workflow():
    workflow = _workflow()
    trigger_block = workflow.split("permissions:", maxsplit=1)[0]

    assert "  push:" in trigger_block
    assert '- "codex/verify-model-routing-convergence"' in trigger_block
    push_paths = trigger_block.split("    paths:", maxsplit=1)[1]
    configured_paths = set(
        re.findall(r'^\s{6}- "([^"]+)"', push_paths, flags=re.MULTILINE)
    )
    assert configured_paths == {
        ".github/workflows/test-model-routing-verification.yml",
        "apps/workflow_engine/tests/services/"
        "test_model_routing_verification_contract.py",
        "scripts/model_routing_verification.py",
        "scripts/model_routing_verification_cases.py",
        "scripts/model_routing_verification_provider.py",
        "tests/ci/test_model_routing_verification_workflow.py",
        "tests/db/test_model_routing_activation_verification.py",
        "tests/experiments/test_model_routing_verification.py",
        "tests/experiments/test_model_routing_verification_cases.py",
        "tests/experiments/test_model_routing_verification_runtime.py",
        "tests/scripts/test_model_routing_verification_provider.py",
    }


def test_workflow_uses_disposable_postgres_gate_and_python_311():
    workflow = _workflow()

    assert "image: pgvector/pgvector:pg16" in workflow
    assert "POSTGRES_USER: postgres" in workflow
    assert "POSTGRES_PASSWORD: postgres" in workflow
    assert "POSTGRES_DB: postgres" in workflow
    assert "DB_HOST: 127.0.0.1" in workflow
    assert 'DB_PORT: "5432"' in workflow
    assert "DB_USER: postgres" in workflow
    assert "DB_PASSWORD: postgres" in workflow
    assert "NODEASE_DISPOSABLE_DB_MAINTENANCE_DB: postgres" in workflow
    assert 'NODEASE_RUN_DISPOSABLE_DB_TEST: "1"' in workflow
    assert 'python-version: "3.11"' in workflow


def test_workflow_runs_only_the_registered_offline_verification_targets():
    workflow = _workflow()
    pytest_step = _pytest_step(workflow)

    configured_targets = {
        line.strip().removesuffix(" \\")
        for line in pytest_step.splitlines()
        if line.strip().endswith(".py") or line.strip().endswith(".py \\")
    }
    assert configured_targets == EXPECTED_TEST_TARGETS
    assert pytest_step.count("-m pytest") == 1
    assert f"--junitxml={JUNIT_PATH}" in pytest_step
    assert "scripts/model_routing_verification.py" not in pytest_step
    assert "scripts/model_routing_verification_provider.py" not in pytest_step
    assert "curl " not in pytest_step
    assert "wget " not in pytest_step
    assert "gh " not in pytest_step
    assert "OPENAI_API_KEY" not in workflow
    assert "secrets." not in workflow


def test_workflow_uploads_junit_evidence_even_when_tests_fail():
    workflow = _workflow()
    upload_step = workflow.split(
        "- name: Upload machine-readable verification evidence",
        maxsplit=1,
    )[1]

    assert "if: always()" in upload_step
    assert "actions/upload-artifact@" in upload_step
    assert f"path: {JUNIT_PATH}" in upload_step
    assert "if-no-files-found: error" in upload_step


def test_workflow_pins_every_external_action_to_a_commit_sha():
    violations: list[str] = []

    for line_number, line in enumerate(_workflow().splitlines(), start=1):
        match = EXTERNAL_ACTION_PATTERN.match(line)
        if match is None:
            continue
        if re.fullmatch(r"[0-9a-f]{40}", match.group("reference")) is None:
            violations.append(f"line {line_number}: {match.group('reference')}")

    assert violations == []


def test_dependency_install_does_not_pull_the_workflow_engine_torch_extra():
    workflow = _workflow()
    install_step = workflow.split(
        "- name: Install verification dependencies",
        maxsplit=1,
    )[1].split("- name: Run model routing verification contracts", maxsplit=1)[0]

    assert '-e "apps/gateway[dev]"' in install_step
    assert "-e apps/shared" in install_step
    assert "apps/workflow_engine" not in install_step
    assert "torch" not in install_step.lower()
