from __future__ import annotations

import json
import threading
from decimal import Decimal

import pytest

from scripts import model_routing_verification_provider as provider


MESSAGES = [{"role": "user", "content": "classify this request"}]


class _RecordingClient:
    def __init__(self, response=None, error: BaseException | None = None):
        self.response = response
        self.error = error
        self.calls = 0

    def invoke_sync(self, messages, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


def _successful_response(*, prompt_tokens: int = 100, completion_tokens: int = 10):
    return {
        "choices": [{"message": {"role": "assistant", "content": "{}"}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


def test_budget_rejects_an_attempt_before_the_provider_call():
    ledger = provider.BudgetLedger(limit_usd=Decimal("0.000001"))
    raw_client = _RecordingClient(_successful_response())
    client = provider.BudgetedClient(raw_client, "gpt-4.1", ledger)

    with pytest.raises(provider.BudgetAdmissionError) as error:
        client.invoke_sync(MESSAGES, max_tokens=1)

    assert error.value.reason_code == "budget_reservation_exceeds_available"
    assert str(error.value) == "budget_reservation_exceeds_available"
    assert raw_client.calls == 0
    assert ledger.summary()["calls"]["attempted"] == 0


def test_budget_limit_cannot_exceed_the_authorized_thirty_dollars():
    with pytest.raises(provider.BudgetAdmissionError) as error:
        provider.BudgetLedger(limit_usd=Decimal("30.01"))

    assert error.value.reason_code == "budget_limit_above_authorized_cap"


def test_directly_wrapped_client_has_sdk_retries_disabled():
    raw_client = _RecordingClient(_successful_response())
    raw_client.max_retries = 4

    client = provider.BudgetedClient(
        raw_client,
        "gpt-4.1",
        provider.BudgetLedger(limit_usd=Decimal("30")),
    )

    assert raw_client.max_retries == 0
    assert not hasattr(client, "client")
    assert not hasattr(client, "_client")
    assert not hasattr(client, "__dict__")


def test_large_schema_is_included_in_the_pre_call_input_reservation():
    ledger = provider.BudgetLedger(limit_usd=Decimal("0.01"))
    raw_client = _RecordingClient(_successful_response())
    client = provider.BudgetedClient(raw_client, "gpt-4.1", ledger)
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "large_verification_schema",
            "schema": {
                "type": "object",
                "description": "x" * 10_000,
            },
        },
    }

    with pytest.raises(provider.BudgetAdmissionError) as error:
        client.invoke_sync(
            MESSAGES,
            max_tokens=1,
            response_format=response_format,
        )

    assert error.value.reason_code == "budget_reservation_exceeds_available"
    assert raw_client.calls == 0


def test_valid_usage_settles_the_reservation_from_actual_token_counts():
    ledger = provider.BudgetLedger(limit_usd=Decimal("30"))
    raw_client = _RecordingClient(
        _successful_response(prompt_tokens=100, completion_tokens=10)
    )
    client = provider.BudgetedClient(raw_client, "gpt-4.1", ledger)

    response = client.invoke_sync(MESSAGES, max_tokens=128)

    assert response["choices"][0]["message"]["content"] == "{}"
    assert ledger.summary() == {
        "limit_usd": "30",
        "charged_usd": "0.00028",
        "available_usd": "29.99972",
        "settled_usd": "0.00028",
        "reserved_usd": "0",
        "calls": {"attempted": 1, "succeeded": 1, "failed": 0},
        "pending_reservations": 0,
        "unknown_usage": 0,
        "blocked": False,
        "blocked_reason": None,
        "cost_basis": "catalog_standard_text_token_estimate",
    }


def test_missing_usage_keeps_the_conservative_charge_and_blocks_further_calls():
    secret = "payload-must-not-appear"
    ledger = provider.BudgetLedger(limit_usd=Decimal("30"))
    raw_client = _RecordingClient(
        {"choices": [{"message": {"role": "assistant", "content": secret}}]}
    )
    client = provider.BudgetedClient(raw_client, "gpt-4.1", ledger)

    with pytest.raises(provider.ProviderAttemptError) as error:
        client.invoke_sync(
            [{"role": "user", "content": secret}],
            max_tokens=128,
        )

    assert error.value.reason_code == "provider_usage_missing"
    assert str(error.value) == "provider_usage_missing"
    summary = ledger.summary()
    assert summary["charged_usd"] != "0"
    assert summary["pending_reservations"] == 0
    assert summary["unknown_usage"] == 1
    assert summary["blocked"] is True
    assert summary["blocked_reason"] == "provider_usage_unknown"
    assert secret not in json.dumps(summary)
    assert secret not in repr(client)

    with pytest.raises(provider.BudgetAdmissionError) as blocked:
        client.invoke_sync(MESSAGES, max_tokens=1)

    assert blocked.value.reason_code == "budget_calls_blocked"
    assert raw_client.calls == 1


def test_timeout_is_redacted_charged_as_unknown_and_blocks_further_calls():
    secret = "secret-timeout-detail"
    timeout = TimeoutError(secret)
    timeout.usage = {"prompt_tokens": 1, "completion_tokens": 0}
    ledger = provider.BudgetLedger(limit_usd=Decimal("30"))
    raw_client = _RecordingClient(error=timeout)
    client = provider.BudgetedClient(raw_client, "gpt-4.1", ledger)

    with pytest.raises(provider.ProviderAttemptError) as error:
        client.invoke_sync(MESSAGES, max_tokens=128)

    assert error.value.reason_code == "provider_timeout"
    assert str(error.value) == "provider_timeout"
    assert secret not in repr(error.value)
    assert ledger.summary()["unknown_usage"] == 1
    assert ledger.summary()["blocked"] is True


def test_provider_error_with_usage_is_settled_without_poisoning_the_ledger():
    underlying = RuntimeError("raw-provider-body")
    underlying.usage = {"input_tokens": 10, "output_tokens": 2}
    underlying.reason_code = "responses_incomplete"
    ledger = provider.BudgetLedger(limit_usd=Decimal("30"))
    client = provider.BudgetedClient(
        _RecordingClient(error=underlying),
        "gpt-4.1",
        ledger,
    )

    with pytest.raises(provider.ProviderAttemptError) as error:
        client.invoke_sync(MESSAGES, max_tokens=128)

    assert error.value.reason_code == "responses_incomplete"
    summary = ledger.summary()
    assert summary["charged_usd"] == "0.000036"
    assert summary["calls"] == {"attempted": 1, "succeeded": 0, "failed": 1}
    assert summary["unknown_usage"] == 0
    assert summary["blocked"] is False


def test_attempts_sharing_a_ledger_are_serialized():
    entered_first = threading.Event()
    release_first = threading.Event()
    entered_second = threading.Event()

    class SerializedProbe:
        def __init__(self):
            self.calls = 0

        def invoke_sync(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                entered_first.set()
                assert release_first.wait(timeout=2)
            else:
                entered_second.set()
            return _successful_response(prompt_tokens=1, completion_tokens=1)

    ledger = provider.BudgetLedger(limit_usd=Decimal("30"))
    raw_client = SerializedProbe()
    first = provider.BudgetedClient(raw_client, "gpt-4.1", ledger)
    second = provider.BudgetedClient(raw_client, "gpt-4.1", ledger)
    failures: list[BaseException] = []

    def invoke(client):
        try:
            client.invoke_sync(MESSAGES, max_tokens=8)
        except BaseException as exc:  # pragma: no cover - assertion aid
            failures.append(exc)

    first_thread = threading.Thread(target=invoke, args=(first,))
    second_thread = threading.Thread(target=invoke, args=(second,))
    first_thread.start()
    assert entered_first.wait(timeout=2)
    second_thread.start()

    assert not entered_second.wait(timeout=0.1)
    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert failures == []
    assert entered_second.is_set()
    assert ledger.summary()["calls"]["succeeded"] == 2


def test_env_client_uses_project_factory_without_exposing_the_key(monkeypatch):
    secret = "synthetic-api-key"
    captured = {}

    class FactoryClient:
        max_retries = 7

        def invoke_sync(self, messages, **kwargs):  # pragma: no cover - not invoked
            raise AssertionError("provider call is not part of construction")

    raw_client = FactoryClient()

    def fake_factory(provider_name, model_id, credentials):
        captured.update(
            provider=provider_name,
            model_id=model_id,
            credentials=dict(credentials),
        )
        return raw_client

    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(provider, "get_llm_client", fake_factory)

    client = provider.env_client(
        "gpt-4.1",
        provider.BudgetLedger(limit_usd=Decimal("30")),
    )

    assert captured == {
        "provider": "openai",
        "model_id": "gpt-4.1",
        "credentials": {
            "apiKey": secret,
            "baseUrl": "https://api.openai.com/v1",
        },
    }
    assert raw_client.max_retries == 0
    assert secret not in repr(client)


def test_env_client_missing_key_has_a_safe_configuration_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(provider.ProviderConfigurationError) as error:
        provider.env_client(
            "gpt-4.1",
            provider.BudgetLedger(limit_usd=Decimal("30")),
        )

    assert error.value.reason_code == "openai_api_key_missing"
    assert str(error.value) == "openai_api_key_missing"
