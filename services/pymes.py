import logging
from typing import Any, Dict, Optional

from services.chat_orchestrator import ChatOrchestrator
from services.llm_orchestrator import llm_orchestrator
from services.logging_config import get_logger
from services.tts_orchestrator import TTSOrchestrator
from services.utils import clean_text, get_session_id
from constants import CONTEXTO_PYME_V2, DEFAULT_REPLY_PYME
from services.herramientas_pyme import get_or_create_pyme_user, log_pyme_interaction, get_static_pyme_data

logger = get_logger(__name__)

KEYWORD_TO_ACTION = {
    "menu": "pyme_menu_principal",
    "menú": "pyme_menu_principal",
    "inicio": "pyme_menu_principal",
    "catalogo": "pyme_productos_stock",
    "catálogo": "pyme_productos_stock",
    "ver productos": "pyme_productos_stock",
    "productos": "pyme_productos_stock",
    "stock": "pyme_productos_stock",
    "carrito": "pyme_ver_carrito",
    "ver carrito": "pyme_ver_carrito",
    "comprar": "pyme_hacer_pedido",
    "pedido": "pyme_hacer_pedido",
    "promociones": "pyme_promociones",
    "ofertas": "pyme_promociones",
    "envio": "pyme_info_envio",
    "delivery": "pyme_info_envio",
}

async def responder_pyme_v4(
    pyme_id: int,
    message: str,
    normalized_phone: str,
    profile_name: str,
    chat_context_data: Dict[str, Any],
    media_url: Optional[str] = None,
    location: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Main orchestrator for PYME responses, V4.
    Focuses on keyword routing before falling back to LLM.
    """
    session_id = get_session_id(normalized_phone, pyme_id)
    user_pyme = get_or_create_pyme_user(normalized_phone, profile_name, pyme_id)
    pyme_context = chat_context_data.get(CONTEXTO_PYME_V2, {})

    if not pyme_context:
        pyme_context = get_static_pyme_data(pyme_id)
        pyme_context["nombre_cliente"] = profile_name
        pyme_context["pyme_id"] = pyme_id

    cleaned_message = clean_text(message)
    action_id = KEYWORD_TO_ACTION.get(cleaned_message)

    if action_id:
        logger.info(f"Keyword match for '{cleaned_message}', routing to action '{action_id}'.")
        action_data = {"target": "pyme"}
        final_response = await ChatOrchestrator.execute_action(action_id, action_data, chat_context_data)
        final_response["fuente"] = final_response.get("fuente", action_id)
    else:
        logger.info("No keyword match, falling back to LLM.")
        final_response = {
            "message_body": DEFAULT_REPLY_PYME,
            "fuente": "llm_fallback_placeholder",
        }
        
    final_response["contexto_actualizado"] = {CONTEXTO_PYME_V2: pyme_context}
    
    return final_response