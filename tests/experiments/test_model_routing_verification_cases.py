from collections import Counter, defaultdict

from scripts.model_routing_verification_cases import (
    build_verification_cases,
    dataset_hash,
)


EXPECTED_DATASET_HASH = (
    "041d72a7afc90ffa9ee28a50671ea63826db0fdbcac28211da1bb421550c7257"
)
REQUIREMENT_KEYS = {
    "task_complexity",
    "decision_impact",
    "evidence_synthesis",
}
TASK_MARKERS = {
    "extract",
    "summarize",
    "compare",
    "synthesize",
    "permission",
    "high-risk",
    "explain",
    "act",
}


def test_verification_corpus_has_preregistered_split_and_difficulty_counts():
    cases = build_verification_cases()

    assert len(cases) == 450
    assert Counter(case.split for case in cases) == {
        "train": 300,
        "holdout": 150,
    }
    assert Counter((case.split, case.difficulty) for case in cases) == {
        ("train", "low"): 100,
        ("train", "medium"): 100,
        ("train", "high"): 100,
        ("holdout", "low"): 50,
        ("holdout", "medium"): 50,
        ("holdout", "high"): 50,
    }


def test_scenario_families_are_group_disjoint_and_have_ten_distinct_variants():
    cases = build_verification_cases()
    splits_by_family = defaultdict(set)
    cases_by_family = defaultdict(list)
    for case in cases:
        splits_by_family[case.family_id].add(case.split)
        cases_by_family[case.family_id].append(case)

    assert len(cases_by_family) == 45
    assert Counter(next(iter(splits)) for splits in splits_by_family.values()) == {
        "train": 30,
        "holdout": 15,
    }
    assert all(len(splits) == 1 for splits in splits_by_family.values())
    assert all(len(family_cases) == 10 for family_cases in cases_by_family.values())
    assert all(
        len({case.request for case in family_cases}) == 10
        for family_cases in cases_by_family.values()
    )


def test_cases_are_unique_and_gold_requirements_are_complete_and_bounded():
    cases = build_verification_cases()

    assert len({case.case_id for case in cases}) == len(cases)
    assert len({(case.request, case.context) for case in cases}) == len(cases)
    for case in cases:
        assert set(case.requirements) == REQUIREMENT_KEYS
        assert all(
            isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 3
            for value in case.requirements.values()
        )
        assert case.required_facts
        assert case.forbidden_errors


def test_each_split_covers_all_task_types_and_keeps_explain_act_pairs_together():
    cases = build_verification_cases()
    for split in ("train", "holdout"):
        split_cases = [case for case in cases if case.split == split]
        observed = {
            marker
            for marker in TASK_MARKERS
            if any(f"-{marker}-" in case.case_id for case in split_cases)
        }
        assert observed == TASK_MARKERS

    cases_by_family = defaultdict(list)
    for case in cases:
        cases_by_family[case.family_id].append(case)
    for family_cases in cases_by_family.values():
        assert any("-explain-" in case.case_id for case in family_cases)
        assert any("-act-" in case.case_id for case in family_cases)


def test_domains_vary_within_each_level_and_across_both_splits():
    cases = build_verification_cases()
    for split in ("train", "holdout"):
        for difficulty in ("low", "medium", "high"):
            domains = {
                case.family_id.split("--", 1)[0]
                for case in cases
                if case.split == split and case.difficulty == difficulty
            }
            assert len(domains) >= 8


def test_dataset_hash_is_deterministic_and_preregistered():
    first = build_verification_cases()
    second = build_verification_cases()

    assert first == second
    assert dataset_hash(first) == dataset_hash(second)
    assert dataset_hash(first) == EXPECTED_DATASET_HASH


def test_holdout_uses_different_problem_constructions_from_training():
    cases = build_verification_cases()
    train_requests = {c.request for c in cases if c.split == "train"}
    holdout_requests = {c.request for c in cases if c.split == "holdout"}
    assert not train_requests.intersection(holdout_requests)
    assert all("세 선택지" in c.request for c in cases if c.split == "holdout" and c.case_id.endswith("-compare-03"))
    assert all("중복제거" in c.context for c in cases if c.split == "holdout" and "-synthesize-" in c.case_id)
