import logging
import os

from services.openai_bridge import (
    DEFAULT_CHAT_MODEL,
    llamar_openai,
)
from services.gemini_bridge import is_gemini_llm_configured
from services.ollama_bridge import is_ollama_llm_configured

logger = logging.getLogger(__name__)


def _safe_log_identifier(value: object, default: str = "unknown") -> str:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 80:
        return default
    if any(not (char.isalnum() or char in "._-/:") for char in candidate):
        return default
    return candidate


def _truthy_env(*names: str) -> bool:
    truthy = {"1", "true", "yes", "on"}
    return any(str(os.getenv(name) or "").strip().lower() in truthy for name in names)


def _cohere_llm_enabled() -> bool:
    return _truthy_env("LLM_COHERE_ENABLED", "COHERE_ENABLED")


def _gemini_llm_enabled() -> bool:
    return is_gemini_llm_configured()


def _ollama_llm_enabled() -> bool:
    return is_ollama_llm_configured()


def _provider_order_from_env() -> list[str]:
    raw_order = os.getenv("LLM_PROVIDER_ORDER", "openai")
    normalized = [provider.strip().lower() for provider in raw_order.split(",") if provider.strip()]
    if not normalized:
        normalized = ["openai"]

    seen: set[str] = set()
    ordered: list[str] = []
    for provider in normalized:
        if provider == "cohere" and not _cohere_llm_enabled():
            logger.info("Skipping Cohere LLM provider because it is not explicitly enabled.")
            continue
        if provider == "gemini" and not _gemini_llm_enabled():
            logger.info("Skipping Gemini LLM provider because GEMINI_API_KEY is not configured.")
            continue
        if provider == "ollama" and not _ollama_llm_enabled():
            logger.info("Skipping Ollama LLM provider because OLLAMA_ENABLED is not enabled.")
            continue
        if provider not in seen:
            seen.add(provider)
            ordered.append(provider)
    return ordered or ["openai"]


def _normalize_task_type(task_type: object) -> str:
    return str(task_type or "").strip().lower().replace("-", "_").replace(" ", "_")


def _task_provider_order(task_type: object | None = None) -> list[str]:
    """
    Return a provider order tuned by workload.

    Open-source/cheaper models such as GLM through Ollama are useful for
    backoffice and analytics workloads, but they stay opt-in for realtime
    WhatsApp and transactional flows until latency and JSON fidelity are proven.
    """

    base_order = _provider_order_from_env()
    normalized_task = _normalize_task_type(task_type)
    if not normalized_task:
        return base_order

    ollama_first_tasks = {
        "analytics",
        "batch_classification",
        "backoffice",
        "crm_summary",
        "insights",
        "report",
        "survey_insights",
        "ticket_summary",
    }
    conservative_tasks = {
        "checkout",
        "critical_ticket_creation",
        "live_chat",
        "order_creation",
        "pedido",
        "reclamo",
        "transactional",
        "voice",
        "whatsapp_realtime",
    }

    if normalized_task in ollama_first_tasks and _ollama_llm_enabled():
        return ["ollama", *[provider for provider in base_order if provider != "ollama"]]

    if normalized_task in conservative_tasks:
        stable_order = [provider for provider in base_order if provider != "ollama"]
        if "openai" not in stable_order:
            stable_order.append("openai")
        return stable_order

    return base_order


def build_llm_task_policy(task_type: object | None = None) -> dict:
    """Expose a secret-safe provider policy for frontend/admin diagnostics."""

    normalized_task = _normalize_task_type(task_type)
    ordered_providers = _task_provider_order(normalized_task)
    primary_provider = ordered_providers[0] if ordered_providers else "openai"
    conservative_tasks = {
        "checkout",
        "critical_ticket_creation",
        "live_chat",
        "order_creation",
        "pedido",
        "reclamo",
        "transactional",
        "voice",
        "whatsapp_realtime",
    }
    backoffice_tasks = {
        "analytics",
        "batch_classification",
        "backoffice",
        "crm_summary",
        "insights",
        "report",
        "survey_insights",
        "ticket_summary",
    }

    return {
        "contract_version": "llm.task_policy.v1",
        "task_type": normalized_task or "default",
        "primary_provider": primary_provider,
        "provider_order": ordered_providers,
        "open_source_ready": "ollama" in ordered_providers,
        "realtime_safe": normalized_task in conservative_tasks,
        "backoffice_optimized": normalized_task in backoffice_tasks,
        "model_env": {
            "openai": "OPENAI_CHAT_MODEL_DEFAULT",
            "gemini": "GEMINI_CHAT_MODEL",
            "ollama": "OLLAMA_CHAT_MODEL",
            "cohere": "COHERE_CHAT_MODEL",
        },
        "fallback_behavior": "pre_request_only_then_fail_closed",
    }


def _resolve_provider(name: str):
    if name == "openai":
        return "OpenAI", llamar_openai
    if name == "gemini" and _gemini_llm_enabled():
        from services.gemini_bridge import llamar_gemini

        return "Gemini", llamar_gemini
    if name == "cohere" and _cohere_llm_enabled():
        from services.cohere_bridge import llamar_cohere

        return "Cohere", llamar_cohere
    if name == "ollama" and _ollama_llm_enabled():
        from services.ollama_bridge import llamar_ollama

        return "Ollama", llamar_ollama
    return None


def _model_for_provider(provider_name: str, requested_model: str | None) -> str | None:
    requested = str(requested_model or "").strip()
    requested_lower = requested.lower()

    if provider_name == "OpenAI":
        if requested and not requested_lower.startswith(("gemini", "glm-", "llama", "qwen")):
            return requested
        return os.getenv("OPENAI_CHAT_MODEL_DEFAULT") or DEFAULT_CHAT_MODEL

    if provider_name == "Gemini":
        if requested_lower.startswith("gemini"):
            return requested
        return os.getenv("GEMINI_CHAT_MODEL") or os.getenv("GEMINI_MODEL") or None

    if provider_name == "Ollama":
        if requested and not requested_lower.startswith(("gpt-", "gemini")):
            return requested
        return os.getenv("OLLAMA_CHAT_MODEL") or None

    return None


def llamar_llm_con_fallback(
    app,
    mensaje_usuario: str,
    usuario: dict,
    historial: list,
    chat_session_id: str,
    model: str = DEFAULT_CHAT_MODEL,
    task_type: str | None = None,
):
    """
    Try configured LLM providers in priority order.

    OpenAI is the production default. Cohere is only used when explicitly
    enabled through env because this deployment standardizes on OpenAI billing,
    behavior and model contracts.

    Returns
    -------
    tuple
        `(response_dict, context_dict)` where `context_dict` is currently unused.
    """

    resolved_task_type = task_type or (usuario or {}).get("ai_task_type") or (usuario or {}).get("task_type")
    providers = [_resolve_provider(name) for name in _task_provider_order(resolved_task_type)]
    providers = [provider for provider in providers if provider]

    last_error_code = "llm_no_provider_available"
    for name, func in providers:
        try:
            logger.info("Attempting LLM call provider=%s", name)
            if name in {"OpenAI", "Gemini", "Ollama"}:
                provider_model = _model_for_provider(name, model)
                return func(
                    app,
                    mensaje_usuario,
                    usuario,
                    historial,
                    chat_session_id,
                    model=provider_model,
                )
            return func(app, mensaje_usuario, usuario, historial, chat_session_id)
        except Exception as exc:  # pragma: no cover - defensive provider boundary
            error_code = _safe_log_identifier(
                getattr(exc, "code", None),
                type(exc).__name__,
            )
            safe_to_fallback = bool(getattr(exc, "safe_to_fallback", False))
            last_error_code = error_code
            if safe_to_fallback:
                logger.warning(
                    "LLM provider unavailable before request provider=%s code=%s; "
                    "trying next configured provider",
                    name,
                    last_error_code,
                )
                continue

            # API/network/parse failures can happen after a request was accepted.
            # A provider switch would create an untracked semantic retry, so the
            # orchestrator deliberately stops here.
            logger.error(
                "LLM provider failure is not retry-safe provider=%s error_type=%s",
                name,
                type(exc).__name__,
            )
            break

    logger.error(
        "LLM orchestration failed closed code=%s",
        last_error_code,
    )
    error_response = {
        "message_body": (
            "Lo siento, nuestros asistentes de IA no estan disponibles en este momento. "
            "Por favor, intenta de nuevo mas tarde o contacta a un representante humano."
        ),
        "accion_backend": "error_fatal_llm",
        "datos_estructura": {},
        "pedir_info": None,
        "botones": [],
    }
    return error_response, {}
