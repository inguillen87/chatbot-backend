import logging
import os
from services.openai_bridge import llamar_openai

logger = logging.getLogger(__name__)


def _truthy_env(*names: str) -> bool:
    truthy = {"1", "true", "yes", "on"}
    return any(str(os.getenv(name) or "").strip().lower() in truthy for name in names)


def _cohere_llm_enabled() -> bool:
    return _truthy_env("LLM_COHERE_ENABLED", "COHERE_ENABLED")


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
        if provider not in seen:
            seen.add(provider)
            ordered.append(provider)
    return ordered or ["openai"]


def _resolve_provider(name: str):
    if name == "openai":
        return "OpenAI", llamar_openai
    if name == "cohere" and _cohere_llm_enabled():
        from services.cohere_bridge import llamar_cohere

        return "Cohere", llamar_cohere
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
            if name == "OpenAI":
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
