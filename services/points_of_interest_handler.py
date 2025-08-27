import json
import logging
import random
from pathlib import Path

from .herramientas_municipio import TOOL_REGISTRY

logger = logging.getLogger(__name__)

PARKING_FILE = Path(__file__).resolve().parents[1] / "data" / "estacionamiento" / "parking_spots.json"

class PointsOfInterestHandler:
    """Handle generic points-of-interest queries using the LLM tool system.

    For parking queries (containing the word 'estacionamiento'), a lightweight
    local dataset is used to emulate available spots in Junín, Mendoza. This
    avoids external API calls during development and provides a deterministic
    experience for users.
    """

    def __init__(self, context: dict):
        self.context = context
        try:
            if PARKING_FILE.exists():
                with open(PARKING_FILE, "r", encoding="utf-8") as fh:
                    self.parking_data = json.load(fh)
            else:
                self.parking_data = []
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Error loading parking dataset: %s", exc, exc_info=True)
            self.parking_data = []

    def _parking_response(self, location: str) -> dict:
        if not self.parking_data:
            return {
                "message_body": "No tengo datos de estacionamiento disponibles en este momento.",
                "options_list": [],
                "message_type": "text",
                "fuente": "points_of_interest_handler"
            }

        sample = random.sample(self.parking_data, min(3, len(self.parking_data)))
        lines = [f"Estacionamientos cercanos a {location}:"]
        for item in sample:
            lines.append(f"- {item['address']}")
        return {
            "message_body": "\n".join(lines),
            "options_list": [],
            "message_type": "text",
            "fuente": "points_of_interest_handler"
        }

    def handle(self, payload: dict) -> dict | None:
        pregunta = (payload.get("pregunta") or "").lower()
        location = payload.get("location")

        if "estacionamiento" in pregunta:
            if not location:
                return {
                    "message_body": "Para buscar estacionamientos necesito tu ubicación.",
                    "options_list": [{"texto": "Compartir ubicación", "action": "compartir_ubicacion"}],
                    "message_type": "interactive_buttons",
                    "fuente": "points_of_interest_handler"
                }
            return self._parking_response(location)

        # --- Generic POI flow delegated to LLM + tools ---
        try:
            from .municipio_responder import llamar_gemini  # delayed import to avoid circular dependency
            mensaje_usuario = json.dumps({"pregunta": pregunta, "ubicacion": location})
            llm_result = llamar_gemini(
                None, mensaje_usuario, {"tipo_entidad": "municipio"}, [], None
            )
            if isinstance(llm_result, tuple):
                respuesta_llm = llm_result[0]
            else:  # pragma: no cover - unexpected but defensive
                respuesta_llm = llm_result
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error("Error invoking LLM for POI query: %s", exc, exc_info=True)
            return {
                "message_body": "Ocurrió un error al procesar la consulta.",
                "options_list": [],
                "message_type": "text",
                "fuente": "points_of_interest_handler"
            }

        accion = respuesta_llm.get("accion_backend")
        message_body = respuesta_llm.get("message_body", "")
        botones = respuesta_llm.get("botones", [])

        if accion == "ejecutar_herramienta":
            datos = respuesta_llm.get("datos_estructura", {})
            nombre = datos.get("nombre_herramienta")
            params = datos.get("parametros_herramienta", {})
            herramienta = TOOL_REGISTRY.get(nombre)
            if not herramienta:
                return {
                    "message_body": message_body or "No tengo una herramienta para eso.",
                    "options_list": [],
                    "message_type": "text",
                    "fuente": "points_of_interest_handler"
                }
            try:
                resultado = herramienta["funcion"](**params)
                if isinstance(resultado, dict):
                    result_text = resultado.get("texto") or resultado.get("message_body") or str(resultado)
                else:
                    result_text = str(resultado)
                final_message = f"{message_body}\n{result_text}".strip() or result_text
                return {
                    "message_body": final_message,
                    "options_list": [],
                    "message_type": "text",
                    "fuente": "points_of_interest_handler"
                }
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Error executing tool %s: %s", nombre, exc, exc_info=True)
                return {
                    "message_body": "Ocurrió un error al obtener la información solicitada.",
                    "options_list": [],
                    "message_type": "text",
                    "fuente": "points_of_interest_handler"
                }

        msg_type = "interactive_buttons" if botones else "text"
        return {
            "message_body": message_body,
            "options_list": botones,
            "message_type": msg_type,
            "fuente": "points_of_interest_handler"
        }
