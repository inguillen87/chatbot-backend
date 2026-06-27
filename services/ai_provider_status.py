from __future__ import annotations

import importlib.util
import os
from datetime import datetime, timezone
from typing import Any


CONTRACT_VERSION = "ai.provider_status.v1"


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
    return importlib.util.find_spec(name) is not None


def _safe_smoke_result(provider: str, ok: bool, **extra: Any) -> dict[str, Any]:
    result = {"provider": provider, "ok": bool(ok)}
    result.update(extra)
    return result


def _huggingface_live_smoke() -> dict[str, Any]:
    try:
        from services.huggingface_inference_service import classify_zero_shot

        os.environ.setdefault("HUGGINGFACE_ZERO_SHOT_ENABLED", "true")
        result = classify_zero_shot(
            "Hay una luminaria rota en la esquina y la zona queda oscura.",
            ["Luminaria", "Arbolado", "Limpieza y riego", "Arreglo de calle", "Perdida de agua", "Otros"],
            multi_label=False,
        )
        if not result:
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


def build_ai_provider_status(*, include_smoke: bool = False, include_live: bool = False) -> dict[str, Any]:
    """Return provider readiness without exposing secret values."""

    openai_configured = _configured("OPENAI_API_KEY")
    gemini_configured = _configured("GEMINI_API_KEY", "GOOGLE_GENAI_API_KEY")
    cohere_configured = _configured("COHERE_API_KEY")
    huggingface_configured = _configured("HUGGINGFACE_API_TOKEN", "HF_TOKEN")
    ollama_enabled = _env_truthy("OLLAMA_ENABLED") or _env_truthy("LLM_OLLAMA_ENABLED")

    providers = {
        "openai": {
            "configured": openai_configured,
            "chat_default": True,
            "required_env": ["OPENAI_API_KEY"],
        },
        "gemini": {
            "configured": gemini_configured,
            "chat_model": _env("GEMINI_CHAT_MODEL", _env("GEMINI_MODEL", "gemini-2.5-flash")),
            "provider_order_enabled": "gemini" in _provider_order(),
            "required_env": ["GEMINI_API_KEY"],
        },
        "cohere": {
            "configured": cohere_configured,
            "enabled": _env_truthy("LLM_COHERE_ENABLED") or _env_truthy("COHERE_ENABLED"),
            "required_env": ["COHERE_API_KEY", "LLM_COHERE_ENABLED"],
        },
        "ollama": {
            "configured": ollama_enabled,
            "enabled": ollama_enabled,
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
            "install_extras_enabled": _env_truthy("INSTALL_OPEN_SOURCE_AI_EXTRAS"),
            "max_file_mb": _env("DOCLING_MAX_FILE_MB", "15"),
        },
    }

    warnings: list[str] = []
    if "gemini" in _provider_order() and not gemini_configured:
        warnings.append("gemini_in_provider_order_but_missing_key")
    if "ollama" in _provider_order() and not ollama_enabled:
        warnings.append("ollama_in_provider_order_but_disabled")
    if providers["huggingface"]["enabled"] and not huggingface_configured:
        warnings.append("huggingface_enabled_but_missing_token")
    if providers["huggingface"]["embeddings_enabled"] and not huggingface_configured:
        warnings.append("huggingface_embeddings_enabled_but_missing_token")
    if providers["docling"]["enabled"] and not providers["docling"]["installed"]:
        warnings.append("docling_enabled_but_package_not_installed")

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

    chat_ready = any(
        [
            openai_configured and "openai" in _provider_order(),
            gemini_configured and "gemini" in _provider_order(),
            cohere_configured and providers["cohere"]["enabled"] and "cohere" in _provider_order(),
            ollama_enabled and "ollama" in _provider_order(),
        ]
    )

    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "secret_values_exposed": False,
        "llm_provider_order": _provider_order(),
        "readiness": {
            "chat_ready": chat_ready,
            "specialized_ai_ready": huggingface_configured or bool(providers["docling"]["installed"]),
            "status": "ready" if chat_ready and not warnings else "warning" if chat_ready else "blocked",
            "warnings": warnings,
        },
        "providers": providers,
        "smoke": smoke,
    }
