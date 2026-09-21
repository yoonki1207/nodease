from __future__ import annotations

import json
from math import ceil

import pytest

from tests.load import run_scenario
from tests.load.run_scenario import (
    empty_loadgen_result,
    merge_loadgen_result_artifact,
    persist_loadgen_result,
)
from tests.load.scenario_contract import (
    STABLE_METRIC_NAMES,
    build_mixed_fixed_counts,
    classify_achieved_rate,
    minimum_pacing_workers,
)


EXPECTED_STABLE_METRIC_NAMES = frozenset(
    {
        "ui.auth.login",
        "ui.apps.list",
        "ui.workflow.read",
        "ui.workflow.draft.read",
        "ui.workflow.draft.save",
        "ui.workflow.execute",
        "ui.trace.lookup",
        "api.deployment.run",
    }
)


@pytest.mark.parametrize(
    ("target_rps", "latency_budget_seconds", "expected_workers"),
    (
        (0.1, 10.0, 2),
        (1.0, 10.0, 15),
        (3.0, 10.0, 45),
        (3.0, 10.1, ceil(3.0 * 10.1 * 1.5)),
    ),
)
def test_pacing_worker_floor_preserves_arrival_capacity(
    target_rps: float,
    latency_budget_seconds: float,
    expected_workers: int,
) -> None:
    assert (
        minimum_pacing_workers(target_rps, latency_budget_seconds)
        == expected_workers
    )


@pytest.mark.parametrize(
    ("target_rps", "achieved_rps", "expected"),
    (
        (1.0, 0.949, "invalid"),
        (1.0, 0.95, "valid"),
        (1.0, 1.0, "valid"),
        (3.0, 2.849, "invalid"),
        (3.0, 2.85, "valid"),
    ),
)
def test_achieved_start_rate_below_95_percent_invalidates_the_run(
    target_rps: float,
    achieved_rps: float,
    expected: str,
) -> None:
    assert classify_achieved_rate(target_rps, achieved_rps) == expected


def test_mixed_profile_keeps_ui_and_api_fixed_counts_separate() -> None:
    counts = build_mixed_fixed_counts(ui_users=25, api_workers=15)

    assert counts.ui_fixed_count == 25
    assert counts.api_fixed_count == 15
    assert counts.total_users == 40


@pytest.mark.parametrize(
    ("ui_users", "api_workers"),
    ((0, 15), (25, 0), (-1, 15), (25, -1)),
)
def test_mixed_profile_rejects_non_positive_fixed_counts(
    ui_users: int,
    api_workers: int,
) -> None:
    with pytest.raises(ValueError):
        build_mixed_fixed_counts(ui_users=ui_users, api_workers=api_workers)


def test_load_request_metric_names_are_stable_and_dimension_free() -> None:
    assert frozenset(STABLE_METRIC_NAMES.values()) == EXPECTED_STABLE_METRIC_NAMES
    assert len(STABLE_METRIC_NAMES) == len(EXPECTED_STABLE_METRIC_NAMES)

    for metric_name in STABLE_METRIC_NAMES.values():
        assert metric_name == metric_name.lower()
        assert all(part.replace("_", "").isalnum() for part in metric_name.split("."))
        assert "{" not in metric_name
        assert "[" not in metric_name
        assert "/" not in metric_name


def test_loadgen_result_is_atomically_persisted_and_merged_without_metadata_loss(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_path = tmp_path / "loadgen_result.json"
    metadata_path = tmp_path / "run_metadata.json"
    metadata = {
        "scenario": "api",
        "target_rps": 1.0,
        "machine": {"system": "Darwin"},
        "result": "completed",
    }
    loadgen = {
        "target_start_rate_rps": 1.0,
        "achieved_start_rate_rps": 0.98,
        "started_total": 294,
        "classification": "valid",
    }
    replacements: list[tuple[object, object]] = []
    original_replace = run_scenario.os.replace

    def recording_replace(source, destination) -> None:
        replacements.append((source, destination))
        original_replace(source, destination)

    monkeypatch.setattr(run_scenario.os, "replace", recording_replace)

    persist_loadgen_result(artifact_path, loadgen)
    merged = merge_loadgen_result_artifact(
        metadata_path=metadata_path,
        metadata=metadata,
        artifact_path=artifact_path,
    )

    assert json.loads(artifact_path.read_text(encoding="utf-8")) == loadgen
    assert json.loads(metadata_path.read_text(encoding="utf-8")) == merged
    assert merged == {**metadata, "loadgen": loadgen}
    assert [destination for _source, destination in replacements] == [
        artifact_path,
        metadata_path,
    ]
    assert not tuple(tmp_path.glob("*.tmp"))
    assert empty_loadgen_result() == {
        "target_start_rate_rps": None,
        "achieved_start_rate_rps": None,
        "started_total": None,
        "classification": None,
    }

    rejected_path = tmp_path / "rejected.json"
    with pytest.raises(ValueError, match="fields are invalid") as caught:
        persist_loadgen_result(
            rejected_path,
            {**loadgen, "response_body": "secret-canary"},
        )
    assert "secret-canary" not in str(caught.value)
    assert not rejected_path.exists()


@pytest.mark.parametrize(
    ("metadata", "loadgen"),
    (
        (
            {"scenario": "api", "target_rps": 3.0},
            empty_loadgen_result(),
        ),
        (
            {"scenario": "mixed", "target_rps": 3.0},
            {
                "target_start_rate_rps": 1.0,
                "achieved_start_rate_rps": 1.0,
                "started_total": 300,
                "classification": "valid",
            },
        ),
        (
            {"scenario": "api", "target_rps": 1.0},
            {
                "target_start_rate_rps": 1.0,
                "achieved_start_rate_rps": 0.5,
                "started_total": 150,
                "classification": "valid",
            },
        ),
        (
            {"scenario": "api", "target_rps": 1.0},
            {
                "target_start_rate_rps": 1.0,
                "achieved_start_rate_rps": float("nan"),
                "started_total": 0,
                "classification": "invalid",
            },
        ),
        (
            {"scenario": "ui", "target_rps": None},
            {
                "target_start_rate_rps": 1.0,
                "achieved_start_rate_rps": 1.0,
                "started_total": 300,
                "classification": "valid",
            },
        ),
    ),
)
def test_loadgen_artifact_must_match_the_run_context(
    tmp_path,
    metadata: dict[str, object],
    loadgen: dict[str, object],
) -> None:
    artifact_path = tmp_path / "loadgen_result.json"
    metadata_path = tmp_path / "run_metadata.json"
    artifact_path.write_text(json.dumps(loadgen), encoding="utf-8")

    with pytest.raises(ValueError):
        merge_loadgen_result_artifact(
            metadata_path=metadata_path,
            metadata=metadata,
            artifact_path=artifact_path,
        )


def test_ui_loadgen_artifact_is_explicitly_not_applicable(tmp_path) -> None:
    artifact_path = tmp_path / "loadgen_result.json"
    metadata_path = tmp_path / "run_metadata.json"
    metadata = {"scenario": "ui", "target_rps": None}
    expected = empty_loadgen_result()
    persist_loadgen_result(artifact_path, expected)

    merged = merge_loadgen_result_artifact(
        metadata_path=metadata_path,
        metadata=metadata,
        artifact_path=artifact_path,
    )

    assert merged["loadgen"] == expected


def test_runner_passes_the_artifact_path_to_locust_and_merges_its_result(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_dir = tmp_path / "reports"
    manifest_path = tmp_path / "runtime.json"
    loadgen = {
        "target_start_rate_rps": 1.0,
        "achieved_start_rate_rps": 0.99,
        "started_total": 297,
        "classification": "valid",
    }

    class Completed:
        returncode = 0

    def fake_run(_command, *, cwd, env, check):
        assert cwd == run_scenario.REPOSITORY_ROOT
        assert check is False
        artifact_path = env["LOAD_TEST_LOADGEN_RESULT_PATH"]
        assert artifact_path.endswith("/loadgen_result.json")
        persist_loadgen_result(run_scenario.Path(artifact_path), loadgen)
        return Completed()

    monkeypatch.setattr(run_scenario, "REPORTS_DIR", reports_dir)
    monkeypatch.setattr(run_scenario, "load_runtime_manifest", lambda _path: None)
    monkeypatch.setattr(run_scenario.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(run_scenario.subprocess, "run", fake_run)

    assert run_scenario.main(
        [
            "api",
            "--api-profile",
            "peak",
            "--run-time",
            "1s",
            "--manifest",
            str(manifest_path),
        ]
    ) == 0

    report_dir = next(reports_dir.iterdir())
    metadata = json.loads(
        (report_dir / "run_metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["scenario"] == "api"
    assert metadata["machine"]["system"]
    assert metadata["result"] == "completed"
    assert metadata["loadgen"] == loadgen
