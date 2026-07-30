from __future__ import annotations

import importlib.util
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any


CONTRACT_VERSION = "ai.provider_status.v1"
PUBLIC_CONTRACT_VERSION = "ai.provider_status_public.v1"
OPENAI_PROVIDER_VERIFICATION_MAX_AGE = timedelta(hours=168)

_SAFE_REASON_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,79}$")
_SAFE_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_OPENAI_CAPABILITY_KEYS = (
    "chat_responses",
    "vision",
    "stt",
    "tts",
    "realtime_voice",
)
_PUBLIC_PROVIDER_KEYS = frozenset(
    {"openai", "gemini", "cohere", "ollama", "huggingface", "docling"}
)
_OPENAI_PUBLIC_ENV_NAMES = frozenset(
    {
        "LLM_PROVIDER_ORDER",
        "OPENAI_API_KEY",
        "OPENAI_CHAT_MODEL_DEFAULT",
        "OPENAI_PROVIDER_LIVE_VERIFIED",
        "OPENAI_PROVIDER_LIVE_VERIFIED_AT",
        "OPENAI_REALTIME_INPUT_TRANSCRIPTION_MODEL",
        "OPENAI_REALTIME_MODEL",
        "OPENAI_REALTIME_SPEECH_MODEL",
        "OPENAI_STT_MODEL",
        "OPENAI_TTS_MODEL",
        "OPENAI_VISION_MODEL",
        "SOCKETIO_MESSAGE_QUEUE_URL",
        "SOCKETIO_REDIS_URL",
        "TWILIO_AUTH_TOKEN",
        "VOICE_STREAM_REPLAY_REDIS_URL",
        "VOICE_STREAM_SIGNING_SECRET",
    }
)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_truthy(name: str) -> bool:
    return _truthy(os.getenv(name))


def _configured(*keys: str) -> bool:
    return any(bool(_env(key)) for key in keys)


def _provider_order() -> list[str]:
    raw = _env("LLM_PROVIDER_ORDER", "openai") or "openai"
    providers: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        provider = item.strip().lower()
        if not provider or provider in seen:
            continue
        seen.add(provider)
        providers.append(provider)
    return providers or ["openai"]


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, AttributeError, ValueError):
        # Optional/provider SDK discovery must never take down an operational
        # status endpoint (nested modules can raise when their parent is absent).
        return False


def _safe_reason_codes(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple)) else []
    result: list[str] = []
    for item in values:
        normalized = str(item or "").strip().lower()
        if normalized and _SAFE_REASON_CODE_RE.fullmatch(normalized) and normalized not in result:
            result.append(normalized)
    return result


def _safe_env_names(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple)) else []
    result: list[str] = []
    for item in values:
        normalized = str(item or "").strip().upper()
        if (
            normalized in _OPENAI_PUBLIC_ENV_NAMES
            and _SAFE_ENV_NAME_RE.fullmatch(normalized)
            and normalized not in result
        ):
            result.append(normalized)
    return result


def _normalized_verification_timestamp(
    value: Any,
    *,
    now: datetime | None = None,
) -> str | None:
    """Return bounded UTC evidence or fail closed.

    A boolean rollout flag without a dated, timezone-aware observation is not
    evidence. Future timestamps and observations older than seven days are
    rejected so a bad or stale deployment variable cannot make an untested
    provider look verified indefinitely.
    """

    raw = str(value or "").strip()
    if not raw or len(raw) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    observed_at = parsed.astimezone(timezone.utc)
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if observed_at > current_time + timedelta(minutes=5):
        return None
    if observed_at < current_time - OPENAI_PROVIDER_VERIFICATION_MAX_AGE:
        return None
    return observed_at.isoformat().replace("+00:00", "Z")


def _configured_min_length(name: str, minimum_bytes: int) -> bool:
    value = _env(name)
    return bool(value and len(value.encode("utf-8")) >= minimum_bytes)


def _configured_redis_transport(*names: str) -> bool:
    for name in names:
        value = _env(name)
        if value and value.lower().startswith(("redis://", "rediss://")):
            return True
    return False


def _safe_smoke_result(provider: str, ok: bool, **extra: Any) -> dict[str, Any]:
    result = {"provider": provider, "ok": bool(ok)}
    result.update(extra)
    return result


def _safe_failure_view(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    failure: dict[str, str] = {}
    for key in ("reason_code", "task"):
        text = str(value.get(key) or "").strip().lower()
        if text and _SAFE_REASON_CODE_RE.fullmatch(text):
            failure[key] = text
    error_type = str(value.get("error_type") or "").strip()
    if error_type and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,79}", error_type):
        failure["error_type"] = error_type
    return failure or None


def _safe_provider_view(provider_key: str, value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    allowed_keys = (
        "configured",
        "enabled",
        "installed",
        "install_extras_enabled",
        "chat_default",
        "provider_order_enabled",
        "runtime_status",
        "runtime_configured",
        "key_configured",
        "credential_status",
        "live_verified",
        "live_verified_at",
        "quota_depleted",
        "fallback_behavior",
        "mode",
        "chat_model",
        "zero_shot_enabled",
        "zero_shot_model",
        "reclamo_category_min_score",
        "reclamo_priority_min_score",
        "reclamo_signal_min_score",
        "sentiment_min_score",
        "pyme_intent_min_score",
        "embeddings_enabled",
        "embedding_model",
        "vision_enabled",
        "max_file_mb",
        "recommended_uses",
        "required_env",
        "optional_env",
    )
    result: dict[str, Any] = {"provider": provider_key}
    for key in allowed_keys:
        if provider_key == "openai" and key in {
            "configured",
            "key_configured",
            "runtime_status",
            "credential_status",
            "live_verified",
            "live_verified_at",
        }:
            continue
        if key in source:
            result[key] = source.get(key)
    if provider_key == "openai":
        key_configured = _truthy(source.get("configured")) or _truthy(source.get("key_configured"))
        verified_at = _normalized_verification_timestamp(source.get("live_verified_at"))
        live_verified = bool(key_configured and _truthy(source.get("live_verified")) and verified_at)
        result.update(
            {
                "configured": key_configured,
                "key_configured": key_configured,
                "runtime_status": (
                    "live_verified"
                    if live_verified
                    else "configured_unverified"
                    if key_configured
                    else "not_configured"
                ),
                "credential_status": (
                    "live_verified"
                    if live_verified
                    else "present_unverified"
                    if key_configured
                    else "missing"
                ),
                "live_verified": live_verified,
                "live_verified_at": verified_at if live_verified else None,
            }
        )
    failure = _safe_failure_view(source.get("last_failure"))
    if failure:
        result["last_failure"] = failure
    return result


def _safe_openai_capability_view(capability_key: str, value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    runtime_configured = _truthy(source.get("runtime_configured"))
    status = "unverified" if runtime_configured else "blocked"
    return {
        "key": capability_key,
        "status": status,
        "runtime_configured": runtime_configured,
        "provider_live_verified": _truthy(source.get("provider_live_verified")),
        # There is intentionally no modality-specific live gate today.  Never
        # copy a truthy value from an untrusted/source payload until such a
        # durable evidence contract exists.
        "live_verified": False,
        "live_verified_at": None,
        "reason_codes": _safe_reason_codes(source.get("reason_codes")),
        "configuration_env": _safe_env_names(source.get("configuration_env")),
    }


def _safe_openai_suite_view(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    provider_source = (
        source.get("provider_verification")
        if isinstance(source.get("provider_verification"), dict)
        else {}
    )
    provider_status = str(provider_source.get("status") or "missing").strip().lower()
    if provider_status not in {"missing", "present_unverified", "live_verified"}:
        provider_status = "missing"
    provider_live_verified = provider_status == "live_verified" and _truthy(
        provider_source.get("live_verified")
    )
    verified_at = (
        _normalized_verification_timestamp(provider_source.get("live_verified_at"))
        if provider_live_verified
        else None
    )
    if provider_live_verified and not verified_at:
        provider_status = "present_unverified"
        provider_live_verified = False

    capabilities_source = (
        source.get("capabilities")
        if isinstance(source.get("capabilities"), dict)
        else {}
    )
    capabilities = {
        key: _safe_openai_capability_view(key, capabilities_source.get(key))
        for key in _OPENAI_CAPABILITY_KEYS
    }
    key_configured = _truthy(source.get("key_configured"))
    runtime_configured = any(
        capability.get("runtime_configured") is True for capability in capabilities.values()
    )
    # No per-capability evidence source exists yet, therefore the suite cannot
    # truthfully be fully live-verified even if provider connectivity was.
    suite_status = (
        "blocked"
        if not key_configured or not runtime_configured
        else "partially_verified"
        if provider_live_verified
        else "unverified"
    )

    return {
        "contract_version": "openai.suite_readiness.v1",
        "status": suite_status,
        "key_configured": key_configured,
        "runtime_configured": runtime_configured,
        "provider_verification": {
            "status": provider_status,
            "live_verified": provider_live_verified,
            "live_verified_at": verified_at,
            "scope": "provider_connectivity_only",
        },
        "capability_evidence_available": False,
        "capabilities": capabilities,
        "reason_codes": _safe_reason_codes(source.get("reason_codes")),
    }


def build_ai_provider_status_public_view(source_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Tenant/admin safe provider view for CRM surfaces.

    This intentionally excludes smoke results, base URLs and raw failure messages.
    It only exposes operational readiness signals and public env var names.
    """

    source = source_payload if isinstance(source_payload, dict) else build_ai_provider_status()
    source_providers = source.get("providers") if isinstance(source.get("providers"), dict) else {}
    providers = {
        str(provider_key): _safe_provider_view(str(provider_key), provider_value)
        for provider_key, provider_value in source_providers.items()
        if str(provider_key) in _PUBLIC_PROVIDER_KEYS
    }
    readiness_source = source.get("readiness") if isinstance(source.get("readiness"), dict) else {}
    warnings = _safe_reason_codes(readiness_source.get("warnings"))
    provider_order = [
        str(item).strip()
        for item in source.get("llm_provider_order") or []
        if str(item).strip() in _PUBLIC_PROVIDER_KEYS
    ]
    openai_suite = _safe_openai_suite_view(source.get("openai_suite"))
    chat_runtime_by_provider = {
        "openai": bool(
            openai_suite["capabilities"]["chat_responses"]["runtime_configured"]
        ),
        "gemini": _truthy((providers.get("gemini") or {}).get("runtime_configured")),
        "cohere": _truthy((providers.get("cohere") or {}).get("runtime_configured")),
        "ollama": _truthy((providers.get("ollama") or {}).get("runtime_configured")),
    }
    selected_chat_provider = next(
        (
            provider
            for provider in provider_order
            if chat_runtime_by_provider.get(provider, False)
        ),
        None,
    )
    chat_runtime_configured = selected_chat_provider is not None
    chat_ready = bool(
        selected_chat_provider == "openai"
        and openai_suite["capabilities"]["chat_responses"]["live_verified"]
    )
    specialized_ai_runtime_configured = bool(
        _truthy((providers.get("huggingface") or {}).get("runtime_configured"))
        or _truthy((providers.get("docling") or {}).get("runtime_configured"))
    )
    specialized_ai_ready = False
    if chat_runtime_configured and not chat_ready:
        warnings.append("chat_capability_live_verification_missing")
    if specialized_ai_runtime_configured:
        warnings.append("specialized_ai_live_verification_missing")
    warnings = list(dict.fromkeys(warnings))
    readiness_status = (
        "ready"
        if chat_ready and not warnings
        else "warning"
        if chat_runtime_configured
        else "blocked"
    )

    return {
        "contract_version": PUBLIC_CONTRACT_VERSION,
        "generated_at": _normalized_verification_timestamp(source.get("generated_at"))
        or datetime.now(timezone.utc).isoformat(),
        "secret_values_exposed": False,
        "llm_provider_order": provider_order,
        "readiness": {
            "selected_chat_provider": selected_chat_provider,
            "chat_runtime_configured": chat_runtime_configured,
            "chat_ready": chat_ready,
            "specialized_ai_runtime_configured": specialized_ai_runtime_configured,
            "specialized_ai_ready": specialized_ai_ready,
            "status": readiness_status,
            "warnings": warnings,
        },
        "providers": providers,
        "openai_suite": openai_suite,
        "frontend_contract": {
            "render_as": "operations_ai_provider_status",
            "access_tenant_scoped": True,
            "configuration_scope": "platform_runtime",
            "safe_for_tenant_crm": True,
            "advisory_only": True,
            "secret_values_exposed": False,
            "recommended_badges": [
                "provider_configuration",
                "provider_live_verification",
                "capability_readiness",
            ],
        },
    }


def _huggingface_live_smoke() -> dict[str, Any]:
    try:
        from services.huggingface_inference_service import classify_zero_shot, get_last_huggingface_failure

        os.environ.setdefault("HUGGINGFACE_ZERO_SHOT_ENABLED", "true")
        result = classify_zero_shot(
            "Hay una luminaria rota en la esquina y la zona queda oscura.",
            ["Luminaria", "Arbolado", "Limpieza y riego", "Arreglo de calle", "Perdida de agua", "Otros"],
            multi_label=False,
        )
        if not result:
            failure = get_last_huggingface_failure()
            if failure:
                return _safe_smoke_result(
                    "huggingface",
                    False,
                    task=failure.get("task") or "zero_shot",
                    reason_code=failure.get("reason_code") or "provider_call_failed",
                    error_type=failure.get("error_type"),
                )
            return _safe_smoke_result("huggingface", False, reason_code="empty_result")
        top = result[0]
        return _safe_smoke_result(
            "huggingface",
            True,
            task="zero_shot",
            top_label=top.get("label"),
            top_score=round(float(top.get("score") or 0), 4),
        )
    except Exception as exc:  # pragma: no cover - defensive runtime diagnostic
        return _safe_smoke_result("huggingface", False, reason_code="provider_call_failed", error_type=type(exc).__name__)


def _openai_capability_status(
    capability_key: str,
    *,
    runtime_configured: bool,
    provider_live_verified: bool,
    reason_codes: list[str],
    configuration_env: list[str],
) -> dict[str, Any]:
    reasons = list(dict.fromkeys(reason_codes))
    if runtime_configured:
        reasons.append("capability_live_verification_missing")
    return {
        "key": capability_key,
        "status": "unverified" if runtime_configured else "blocked",
        "runtime_configured": runtime_configured,
        "provider_live_verified": provider_live_verified,
        "live_verified": False,
        "live_verified_at": None,
        "reason_codes": list(dict.fromkeys(reasons)),
        "configuration_env": configuration_env,
    }


def _build_openai_suite_status(
    *,
    key_configured: bool,
    provider_live_verified: bool,
    provider_live_verified_at: str | None,
    provider_order: list[str],
) -> dict[str, Any]:
    sdk_installed = _module_available("openai")
    websocket_installed = _module_available("websocket")
    flask_sock_installed = _module_available("flask_sock")

    base_reasons: list[str] = []
    if not key_configured:
        base_reasons.append("openai_api_key_missing")
    if not sdk_installed:
        base_reasons.append("openai_sdk_missing")

    chat_reasons = list(base_reasons)
    if "openai" not in provider_order:
        chat_reasons.append("openai_not_in_provider_order")
    chat_runtime_configured = not chat_reasons

    direct_runtime_configured = not base_reasons

    realtime_reasons = list(base_reasons)
    if not websocket_installed:
        realtime_reasons.append("websocket_client_missing")
    if not flask_sock_installed:
        realtime_reasons.append("flask_sock_missing")

    twilio_auth_valid = _configured_min_length("TWILIO_AUTH_TOKEN", 16)
    dedicated_signing_valid = _configured_min_length("VOICE_STREAM_SIGNING_SECRET", 32)
    if not twilio_auth_valid:
        realtime_reasons.append("twilio_request_auth_missing")
    if not (dedicated_signing_valid or twilio_auth_valid):
        realtime_reasons.append("voice_stream_signing_missing")
    replay_store_configured = _configured_redis_transport(
        "VOICE_STREAM_REPLAY_REDIS_URL",
        "SOCKETIO_MESSAGE_QUEUE_URL",
        "SOCKETIO_REDIS_URL",
        "CELERY_BROKER_URL",
    )
    if not replay_store_configured:
        realtime_reasons.append("voice_stream_replay_store_missing")
    realtime_runtime_configured = not realtime_reasons

    capabilities = {
        "chat_responses": _openai_capability_status(
            "chat_responses",
            runtime_configured=chat_runtime_configured,
            provider_live_verified=provider_live_verified,
            reason_codes=chat_reasons,
            configuration_env=["OPENAI_API_KEY", "LLM_PROVIDER_ORDER", "OPENAI_CHAT_MODEL_DEFAULT"],
        ),
        "vision": _openai_capability_status(
            "vision",
            runtime_configured=direct_runtime_configured,
            provider_live_verified=provider_live_verified,
            reason_codes=list(base_reasons),
            configuration_env=["OPENAI_API_KEY", "OPENAI_VISION_MODEL"],
        ),
        "stt": _openai_capability_status(
            "stt",
            runtime_configured=direct_runtime_configured,
            provider_live_verified=provider_live_verified,
            reason_codes=list(base_reasons),
            configuration_env=["OPENAI_API_KEY", "OPENAI_STT_MODEL"],
        ),
        "tts": _openai_capability_status(
            "tts",
            runtime_configured=direct_runtime_configured,
            provider_live_verified=provider_live_verified,
            reason_codes=list(base_reasons),
            configuration_env=["OPENAI_API_KEY", "OPENAI_TTS_MODEL"],
        ),
        "realtime_voice": _openai_capability_status(
            "realtime_voice",
            runtime_configured=realtime_runtime_configured,
            provider_live_verified=provider_live_verified,
            reason_codes=realtime_reasons,
            configuration_env=[
                "OPENAI_API_KEY",
                "OPENAI_REALTIME_SPEECH_MODEL",
                "OPENAI_REALTIME_INPUT_TRANSCRIPTION_MODEL",
                "TWILIO_AUTH_TOKEN",
                "VOICE_STREAM_SIGNING_SECRET",
                "VOICE_STREAM_REPLAY_REDIS_URL",
                "SOCKETIO_MESSAGE_QUEUE_URL",
                "SOCKETIO_REDIS_URL",
            ],
        ),
    }

    configured_capabilities = sum(
        1 for capability in capabilities.values() if capability["runtime_configured"]
    )
    suite_reasons: list[str] = []
    if not key_configured:
        suite_reasons.append("openai_api_key_missing")
    if key_configured and not provider_live_verified:
        suite_reasons.append("provider_live_verification_missing")
    if configured_capabilities < len(capabilities):
        suite_reasons.append("capability_runtime_configuration_incomplete")
    suite_reasons.append("capability_live_verification_missing")

    if not key_configured or configured_capabilities == 0:
        suite_status = "blocked"
    elif provider_live_verified:
        suite_status = "partially_verified"
    else:
        suite_status = "unverified"

    return {
        "contract_version": "openai.suite_readiness.v1",
        "status": suite_status,
        "key_configured": key_configured,
        "runtime_configured": configured_capabilities > 0,
        "provider_verification": {
            "status": "live_verified" if provider_live_verified else "present_unverified" if key_configured else "missing",
            "live_verified": provider_live_verified,
            "live_verified_at": provider_live_verified_at if provider_live_verified else None,
            "scope": "provider_connectivity_only",
        },
        "capability_evidence_available": False,
        "capabilities": capabilities,
        "reason_codes": list(dict.fromkeys(suite_reasons)),
    }


def build_ai_provider_status(*, include_smoke: bool = False, include_live: bool = False) -> dict[str, Any]:
    """Return provider readiness without exposing secret values."""

    openai_configured = _configured("OPENAI_API_KEY")
    openai_live_verification_declared = _env_truthy("OPENAI_PROVIDER_LIVE_VERIFIED")
    openai_live_verified_at = _normalized_verification_timestamp(
        _env("OPENAI_PROVIDER_LIVE_VERIFIED_AT")
    )
    openai_live_verified = bool(
        openai_configured
        and openai_live_verification_declared
        and openai_live_verified_at
    )
    gemini_configured = _configured("GEMINI_API_KEY", "GOOGLE_GENAI_API_KEY")
    cohere_configured = _configured("COHERE_API_KEY")
    huggingface_configured = _configured("HUGGINGFACE_API_TOKEN", "HF_TOKEN")
    ollama_enabled = _env_truthy("OLLAMA_ENABLED") or _env_truthy("LLM_OLLAMA_ENABLED")
    huggingface_last_failure = None
    try:
        from services.huggingface_inference_service import get_last_huggingface_failure

        huggingface_last_failure = get_last_huggingface_failure()
    except Exception:  # pragma: no cover - status page must not fail on optional provider import
        huggingface_last_failure = None
    huggingface_failure_reason = str((huggingface_last_failure or {}).get("reason_code") or "")
    huggingface_quota_depleted = huggingface_failure_reason == "huggingface_quota_or_payment_required"
    huggingface_runtime_status = (
        "not_configured"
        if not huggingface_configured
        else "degraded"
        if huggingface_last_failure
        else "ready"
    )

    providers = {
        "openai": {
            "configured": openai_configured,
            "key_configured": openai_configured,
            "runtime_configured": openai_configured and _module_available("openai"),
            "chat_default": True,
            "runtime_status": (
                "live_verified"
                if openai_configured and openai_live_verified
                else "configured_unverified"
                if openai_configured
                else "not_configured"
            ),
            "credential_status": (
                "live_verified"
                if openai_configured and openai_live_verified
                else "present_unverified"
                if openai_configured
                else "missing"
            ),
            "live_verified": bool(openai_configured and openai_live_verified),
            "live_verified_at": (
                openai_live_verified_at
                if openai_configured and openai_live_verified
                else None
            ),
            "required_env": ["OPENAI_API_KEY", "OPENAI_PROVIDER_LIVE_VERIFIED"],
            "optional_env": ["OPENAI_PROVIDER_LIVE_VERIFIED_AT"],
        },
        "gemini": {
            "configured": gemini_configured,
            "runtime_configured": gemini_configured and _module_available("google.genai"),
            "chat_model": _env("GEMINI_CHAT_MODEL", _env("GEMINI_MODEL", "gemini-2.5-flash")),
            "provider_order_enabled": "gemini" in _provider_order(),
            "required_env": ["GEMINI_API_KEY"],
        },
        "cohere": {
            "configured": cohere_configured,
            "enabled": _env_truthy("LLM_COHERE_ENABLED") or _env_truthy("COHERE_ENABLED"),
            "runtime_configured": bool(
                cohere_configured
                and (_env_truthy("LLM_COHERE_ENABLED") or _env_truthy("COHERE_ENABLED"))
                and _module_available("cohere")
            ),
            "required_env": ["COHERE_API_KEY", "LLM_COHERE_ENABLED"],
        },
        "ollama": {
            "configured": ollama_enabled,
            "enabled": ollama_enabled,
            "runtime_configured": ollama_enabled and _module_available("openai"),
            "provider_order_enabled": "ollama" in _provider_order(),
            "chat_model": _env("OLLAMA_CHAT_MODEL", _env("OLLAMA_MODEL", "glm-5.2:cloud")),
            "base_url": _env("OLLAMA_BASE_URL", _env("OLLAMA_OPENAI_BASE_URL", "http://localhost:11434/v1")),
            "mode": "experimental",
            "recommended_uses": [
                "admin_analytics",
                "long_context_summaries",
                "internal_agent_workflows",
                "template_quality_review",
            ],
            "required_env": ["OLLAMA_ENABLED", "OLLAMA_CHAT_MODEL"],
            "optional_env": ["OLLAMA_API_KEY", "OLLAMA_BASE_URL", "OLLAMA_TIMEOUT_SECONDS"],
        },
        "huggingface": {
            "configured": huggingface_configured,
            "enabled": _env_truthy("HUGGINGFACE_ENABLED"),
            "runtime_configured": bool(
                huggingface_configured
                and _env_truthy("HUGGINGFACE_ENABLED")
                and not huggingface_quota_depleted
                and _module_available("huggingface_hub")
            ),
            "runtime_status": huggingface_runtime_status,
            "quota_depleted": huggingface_quota_depleted,
            "last_failure": huggingface_last_failure,
            "fallback_behavior": "deterministic_local_fallback",
            "provider": _env("HUGGINGFACE_PROVIDER", "auto"),
            "zero_shot_enabled": _env_truthy("HUGGINGFACE_ZERO_SHOT_ENABLED") or _env_truthy("HF_ZERO_SHOT_ENABLED"),
            "zero_shot_model": _env("HUGGINGFACE_ZERO_SHOT_MODEL", "joeddav/xlm-roberta-large-xnli"),
            "reclamo_category_min_score": _env("HUGGINGFACE_RECLAMO_CATEGORY_MIN_SCORE", "0.72"),
            "reclamo_priority_min_score": _env("HUGGINGFACE_RECLAMO_PRIORITY_MIN_SCORE", "0.66"),
            "reclamo_signal_min_score": _env("HUGGINGFACE_RECLAMO_SIGNAL_MIN_SCORE", "0.62"),
            "sentiment_min_score": _env("HUGGINGFACE_SENTIMENT_MIN_SCORE", "0.56"),
            "pyme_intent_min_score": _env("HUGGINGFACE_PYME_INTENT_MIN_SCORE", "0.62"),
            "embeddings_enabled": _env_truthy("HUGGINGFACE_EMBEDDINGS_ENABLED") or _env_truthy("HF_EMBEDDINGS_ENABLED"),
            "embedding_model": _env("HUGGINGFACE_EMBEDDING_MODEL", "intfloat/multilingual-e5-large"),
            "vision_enabled": _env_truthy("VISION_HUGGINGFACE_ENABLED") or _env_truthy("HUGGINGFACE_VISION_ENABLED"),
            "required_env": ["HUGGINGFACE_API_TOKEN"],
        },
        "docling": {
            "enabled": _env_truthy("DOCLING_ENABLED") or _env_truthy("OPEN_SOURCE_DOCUMENT_AI_ENABLED"),
            "installed": _module_available("docling"),
            "runtime_configured": bool(
                (_env_truthy("DOCLING_ENABLED") or _env_truthy("OPEN_SOURCE_DOCUMENT_AI_ENABLED"))
                and _module_available("docling")
            ),
            "install_extras_enabled": _env_truthy("INSTALL_OPEN_SOURCE_AI_EXTRAS"),
            "max_file_mb": _env("DOCLING_MAX_FILE_MB", "15"),
        },
    }

    warnings: list[str] = []
    if "openai" in _provider_order() and openai_configured and not openai_live_verified:
        warnings.append("openai_configured_but_not_live_verified")
    if openai_live_verification_declared and not openai_configured:
        warnings.append("openai_live_verified_without_key")
    if (
        openai_live_verification_declared
        and openai_configured
        and not openai_live_verified_at
    ):
        warnings.append("openai_live_verification_evidence_invalid")
    if "gemini" in _provider_order() and not gemini_configured:
        warnings.append("gemini_in_provider_order_but_missing_key")
    if "ollama" in _provider_order() and not ollama_enabled:
        warnings.append("ollama_in_provider_order_but_disabled")
    if providers["huggingface"]["enabled"] and not huggingface_configured:
        warnings.append("huggingface_enabled_but_missing_token")
    if providers["huggingface"]["embeddings_enabled"] and not huggingface_configured:
        warnings.append("huggingface_embeddings_enabled_but_missing_token")
    if huggingface_failure_reason:
        warnings.append(huggingface_failure_reason)
    if providers["docling"]["enabled"] and not providers["docling"]["installed"]:
        warnings.append("docling_enabled_but_package_not_installed")

    provider_order = _provider_order()
    openai_suite = _build_openai_suite_status(
        key_configured=openai_configured,
        provider_live_verified=openai_live_verified,
        provider_live_verified_at=openai_live_verified_at,
        provider_order=provider_order,
    )
    chat_runtime_by_provider = {
        "openai": bool(
            openai_suite["capabilities"]["chat_responses"]["runtime_configured"]
        ),
        "gemini": gemini_configured and _module_available("google.genai"),
        "cohere": bool(
            cohere_configured
            and providers["cohere"]["enabled"]
            and _module_available("cohere")
        ),
        "ollama": ollama_enabled and _module_available("openai"),
    }
    selected_chat_provider = next(
        (
            provider
            for provider in provider_order
            if chat_runtime_by_provider.get(provider, False)
        ),
        None,
    )
    chat_runtime_configured = selected_chat_provider is not None
    chat_ready = bool(
        selected_chat_provider == "openai"
        and openai_suite["capabilities"]["chat_responses"]["live_verified"]
    )
    specialized_ai_runtime_configured = bool(
        (
            huggingface_configured
            and not huggingface_quota_depleted
            and _module_available("huggingface_hub")
        )
        or (providers["docling"]["enabled"] and providers["docling"]["installed"])
    )
    # There is no dated, workload-specific evidence contract for these
    # specialized providers yet. Config/token/package presence is not readiness.
    specialized_ai_ready = False
    if chat_runtime_configured and not chat_ready:
        warnings.append("chat_capability_live_verification_missing")
    if specialized_ai_runtime_configured and not specialized_ai_ready:
        warnings.append("specialized_ai_live_verification_missing")
    warnings = list(dict.fromkeys(warnings))

    live_smoke_enabled = include_live and _env_truthy("AI_PROVIDER_STATUS_LIVE_SMOKE_ENABLED")
    smoke: dict[str, Any] = {
        "requested": include_smoke,
        "live_requested": include_live,
        "live_enabled": live_smoke_enabled,
        "results": [],
    }
    if include_smoke:
        smoke["results"].extend(
            [
                _safe_smoke_result("openai", openai_configured, mode="config"),
                _safe_smoke_result("gemini", gemini_configured and _module_available("google.genai"), mode="sdk"),
                _safe_smoke_result("ollama", ollama_enabled and _module_available("openai"), mode="openai_compatible_config"),
                _safe_smoke_result("huggingface", huggingface_configured and _module_available("huggingface_hub"), mode="sdk"),
            ]
        )
        if live_smoke_enabled and huggingface_configured:
            smoke["results"].append(_huggingface_live_smoke())

    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "secret_values_exposed": False,
        "llm_provider_order": provider_order,
        "readiness": {
            "selected_chat_provider": selected_chat_provider,
            "chat_runtime_configured": chat_runtime_configured,
            "chat_ready": chat_ready,
            "specialized_ai_runtime_configured": specialized_ai_runtime_configured,
            "specialized_ai_ready": specialized_ai_ready,
            "status": (
                "ready"
                if chat_ready and not warnings
                else "warning"
                if chat_runtime_configured
                else "blocked"
            ),
            "warnings": warnings,
        },
        "providers": providers,
        "openai_suite": openai_suite,
        "smoke": smoke,
    }
