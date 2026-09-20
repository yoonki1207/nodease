"""Fail-closed provider adapter for explicit model-routing verification runs.

The adapter intentionally keeps no prompts, responses, or credentials in its
ledger.  It reserves a conservative Standard text-token estimate before each
provider attempt and stops the shared experiment ledger whenever billed usage
cannot be established.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from apps.shared.services.llm_client import get_llm_client
from apps.shared.services.llm_model_pricing import ModelPricing, get_model_pricing


_AUTHORIZED_LIMIT_USD = Decimal("30")
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_MESSAGE_OVERHEAD_TOKENS = 32
_REQUEST_OVERHEAD_TOKENS = 256
_SAFE_PROVIDER_REASON_CODES = {
    "provider_connection_failed",
    "provider_egress_denied",
    "provider_endpoint_unsupported",
    "provider_http_error",
    "provider_response_rejected",
    "provider_timeout",
    "responses_empty_text",
    "responses_incomplete",
}


class VerificationProviderError(RuntimeError):
    """Base error that exposes only a stable, non-provider reason code."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class BudgetAdmissionError(VerificationProviderError):
    """A provider attempt was rejected before network I/O."""


class ProviderAttemptError(VerificationProviderError):
    """A provider attempt failed or could not be billed safely."""


class ProviderConfigurationError(VerificationProviderError):
    """The explicit live adapter cannot be constructed safely."""


@dataclass(frozen=True, slots=True)
class _Reservation:
    reservation_id: int
    amount_usd: Decimal


def _as_decimal(value: Any) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise BudgetAdmissionError("budget_limit_invalid") from None
    if not result.is_finite():
        raise BudgetAdmissionError("budget_limit_invalid")
    return result


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


class BudgetLedger:
    """One process-local, thread-safe USD cap shared by all experiment clients."""

    def __init__(self, limit_usd: Decimal) -> None:
        limit = _as_decimal(limit_usd)
        if limit <= 0:
            raise BudgetAdmissionError("budget_limit_not_positive")
        if limit > _AUTHORIZED_LIMIT_USD:
            raise BudgetAdmissionError("budget_limit_above_authorized_cap")

        self._limit_usd = limit
        self._state_lock = threading.Lock()
        self._attempt_lock = threading.Lock()
        self._reservations: dict[int, Decimal] = {}
        self._next_reservation_id = 1
        self._settled_usd = Decimal("0")
        self._unknown_reserved_usd = Decimal("0")
        self._attempted = 0
        self._succeeded = 0
        self._failed = 0
        self._unknown_usage = 0
        self._blocked = False
        self._blocked_reason: str | None = None

    def _reserved_usd(self) -> Decimal:
        return sum(self._reservations.values(), Decimal("0"))

    def _charged_usd(self) -> Decimal:
        return self._settled_usd + self._unknown_reserved_usd + self._reserved_usd()

    def _reserve(self, amount_usd: Decimal) -> _Reservation:
        if not amount_usd.is_finite() or amount_usd <= 0:
            raise BudgetAdmissionError("budget_reservation_invalid")
        with self._state_lock:
            if self._blocked:
                raise BudgetAdmissionError("budget_calls_blocked")
            if self._charged_usd() + amount_usd > self._limit_usd:
                raise BudgetAdmissionError("budget_reservation_exceeds_available")
            reservation = _Reservation(self._next_reservation_id, amount_usd)
            self._next_reservation_id += 1
            self._reservations[reservation.reservation_id] = amount_usd
            self._attempted += 1
            return reservation

    def _pop_reservation(self, reservation: _Reservation) -> Decimal:
        try:
            return self._reservations.pop(reservation.reservation_id)
        except KeyError:
            self._blocked = True
            self._blocked_reason = "budget_reservation_not_pending"
            raise ProviderAttemptError("budget_reservation_not_pending") from None

    def _settle_known(
        self,
        reservation: _Reservation,
        actual_usd: Decimal,
        *,
        succeeded: bool,
    ) -> None:
        with self._state_lock:
            reserved_usd = self._pop_reservation(reservation)
            self._settled_usd += actual_usd
            if succeeded:
                self._succeeded += 1
            else:
                self._failed += 1
            if actual_usd > reserved_usd or self._charged_usd() > self._limit_usd:
                self._blocked = True
                self._blocked_reason = "actual_cost_exceeded_reservation"
                raise ProviderAttemptError("actual_cost_exceeded_reservation")

    def _settle_unknown(self, reservation: _Reservation) -> None:
        with self._state_lock:
            reserved_usd = self._pop_reservation(reservation)
            self._unknown_reserved_usd += reserved_usd
            self._failed += 1
            self._unknown_usage += 1
            self._blocked = True
            self._blocked_reason = "provider_usage_unknown"

    def summary(self) -> dict[str, Any]:
        """Return aggregate accounting only; no request or credential data."""

        with self._state_lock:
            reserved = self._reserved_usd()
            charged = self._settled_usd + self._unknown_reserved_usd + reserved
            available = max(Decimal("0"), self._limit_usd - charged)
            return {
                "limit_usd": _decimal_text(self._limit_usd),
                "charged_usd": _decimal_text(charged),
                "available_usd": _decimal_text(available),
                "settled_usd": _decimal_text(self._settled_usd),
                "reserved_usd": _decimal_text(reserved),
                "calls": {
                    "attempted": self._attempted,
                    "succeeded": self._succeeded,
                    "failed": self._failed,
                },
                "pending_reservations": len(self._reservations),
                "unknown_usage": self._unknown_usage,
                "blocked": self._blocked,
                "blocked_reason": self._blocked_reason,
                "cost_basis": "catalog_standard_text_token_estimate",
            }


def _standard_cost(
    pricing: ModelPricing,
    *,
    prompt_tokens: int,
    completion_tokens: int,
) -> Decimal:
    input_rate = Decimal(str(pricing.standard_input_per_1k))
    output_rate = Decimal(str(pricing.standard_output_per_1k))
    return (
        Decimal(prompt_tokens) * input_rate
        + Decimal(completion_tokens) * output_rate
    ) / Decimal(1000)


def _request_input_bound(messages: Any, kwargs: Mapping[str, Any]) -> int:
    if not isinstance(messages, list):
        raise ProviderConfigurationError("provider_messages_invalid")
    try:
        canonical = json.dumps(
            {"messages": messages, "parameters": dict(kwargs)},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ProviderConfigurationError("provider_messages_invalid") from None
    return (
        len(canonical.encode("utf-8"))
        + len(messages) * _MESSAGE_OVERHEAD_TOKENS
        + _REQUEST_OVERHEAD_TOKENS
    )


def _output_token_bound(kwargs: Mapping[str, Any]) -> int:
    values: list[int] = []
    for name in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        if name not in kwargs:
            continue
        value = kwargs[name]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ProviderConfigurationError("provider_output_limit_invalid")
        values.append(value)
    if not values:
        raise ProviderConfigurationError("provider_output_limit_missing")
    return max(values)


def _billing_usage(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, dict):
        return None
    prompt_tokens = value.get("prompt_tokens", value.get("input_tokens"))
    completion_tokens = value.get("completion_tokens", value.get("output_tokens"))
    if (
        isinstance(prompt_tokens, bool)
        or not isinstance(prompt_tokens, int)
        or prompt_tokens < 0
        or isinstance(completion_tokens, bool)
        or not isinstance(completion_tokens, int)
        or completion_tokens < 0
    ):
        return None
    return prompt_tokens, completion_tokens


def _is_timeout(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if getattr(exc, "reason_code", None) == "provider_timeout":
        return True
    return any("timeout" in cls.__name__.lower() for cls in type(exc).__mro__)


def _safe_provider_reason(exc: Exception, *, timed_out: bool) -> str:
    if timed_out:
        return "provider_timeout"
    candidate = getattr(exc, "reason_code", None)
    if isinstance(candidate, str) and candidate in _SAFE_PROVIDER_REASON_CODES:
        return candidate
    return "provider_call_failed"


class BudgetedClient:
    """Runtime-Judge-compatible sync client with strict shared accounting."""

    __slots__ = ("__client", "_model_id", "_ledger", "_pricing")

    def __init__(self, client: Any, model_id: str, ledger: BudgetLedger) -> None:
        pricing = get_model_pricing(model_id)
        if pricing is None:
            raise ProviderConfigurationError("model_pricing_unavailable")
        if not hasattr(client, "invoke_sync"):
            raise ProviderConfigurationError("provider_client_invalid")
        _disable_sdk_retries_if_supported(client)
        self.__client = client
        self._model_id = str(model_id)
        self._ledger = ledger
        self._pricing = pricing

    def __repr__(self) -> str:
        return f"BudgetedClient(model_id={self._model_id!r}, redacted=True)"

    def invoke_sync(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        input_bound = _request_input_bound(messages, kwargs)
        output_bound = _output_token_bound(kwargs)
        reservation_cost = _standard_cost(
            self._pricing,
            prompt_tokens=input_bound,
            completion_tokens=output_bound,
        )

        # The shared lock makes reserve -> invoke -> settle one indivisible
        # accounting attempt across every client using this experiment ledger.
        with self._ledger._attempt_lock:
            reservation = self._ledger._reserve(reservation_cost)
            try:
                response = self.__client.invoke_sync(messages=messages, **kwargs)
            except Exception as exc:
                timed_out = _is_timeout(exc)
                usage = _billing_usage(getattr(exc, "usage", None))
                if timed_out or usage is None:
                    self._ledger._settle_unknown(reservation)
                else:
                    actual_cost = _standard_cost(
                        self._pricing,
                        prompt_tokens=usage[0],
                        completion_tokens=usage[1],
                    )
                    self._ledger._settle_known(
                        reservation,
                        actual_cost,
                        succeeded=False,
                    )
                raise ProviderAttemptError(
                    _safe_provider_reason(exc, timed_out=timed_out)
                ) from None

            usage = _billing_usage(
                response.get("usage") if isinstance(response, dict) else None
            )
            if usage is None:
                self._ledger._settle_unknown(reservation)
                raise ProviderAttemptError("provider_usage_missing")
            actual_cost = _standard_cost(
                self._pricing,
                prompt_tokens=usage[0],
                completion_tokens=usage[1],
            )
            self._ledger._settle_known(
                reservation,
                actual_cost,
                succeeded=True,
            )
            return response


def _disable_sdk_retries_if_supported(client: Any) -> None:
    """Disable SDK retries where a supplied client exposes that setting."""

    candidates = [client]
    for attribute in ("client", "_client"):
        nested = getattr(client, attribute, None)
        if nested is not None and nested is not client:
            candidates.append(nested)
    for candidate in candidates:
        if not hasattr(candidate, "max_retries"):
            continue
        try:
            setattr(candidate, "max_retries", 0)
        except (AttributeError, TypeError):
            continue


def env_client(model_id: str, ledger: BudgetLedger) -> BudgetedClient:
    """Build the explicit OpenAI-compatible live client from read-only env vars."""

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or not api_key.strip():
        raise ProviderConfigurationError("openai_api_key_missing")
    base_url = os.environ.get("OPENAI_BASE_URL", _DEFAULT_OPENAI_BASE_URL)
    if not base_url or base_url != base_url.strip():
        raise ProviderConfigurationError("openai_base_url_invalid")

    try:
        raw_client = get_llm_client(
            "openai",
            model_id,
            {"apiKey": api_key, "baseUrl": base_url},
        )
    except Exception:
        raise ProviderConfigurationError("openai_client_configuration_failed") from None
    _disable_sdk_retries_if_supported(raw_client)
    return BudgetedClient(raw_client, model_id, ledger)


__all__ = [
    "BudgetAdmissionError",
    "BudgetLedger",
    "BudgetedClient",
    "ProviderAttemptError",
    "ProviderConfigurationError",
    "VerificationProviderError",
    "env_client",
]
