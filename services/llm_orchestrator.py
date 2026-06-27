import logging
import os
from services.openai_bridge import llamar_openai
from services.gemini_bridge import is_gemini_llm_configured
from services.ollama_bridge import is_ollama_llm_configured

logger = logging.getLogger(__name__)


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


def llamar_llm_con_fallback(app, mensaje_usuario: str, usuario: dict, historial: list, chat_session_id: str, model: str = "gpt-4o-mini"):
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

    providers = [_resolve_provider(name) for name in _provider_order_from_env()]
    providers = [provider for provider in providers if provider]

    last_error = None
    for name, func in providers:
        try:
            logger.info(f"Attempting LLM call with provider: {name}")
            if name in {"OpenAI", "Gemini", "Ollama"}:
                return func(app, mensaje_usuario, usuario, historial, chat_session_id, model=model)
            return func(app, mensaje_usuario, usuario, historial, chat_session_id)
        except Exception as exc:  # pragma: no cover - defensive logging
            last_error = exc
            logger.warning(f"{name} call failed with error: {exc}")

    logger.error(
        "All LLM providers failed. Last error: %s", last_error, exc_info=True
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
