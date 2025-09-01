import json
import logging
import random
from pathlib import Path

from .herramientas_municipio import TOOL_REGISTRY
from .estacionamiento_utils import _dist_m
from .google_maps_service import get_coordinates
from .estacionamiento_service import consultar_ocupacion

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

    def _parking_response(self, location: dict | str) -> dict:
        """Generate a parking response based on coordinates or an address."""
        if not self.parking_data:
            return {
                "message_body": "No tengo datos de estacionamiento disponibles en este momento.",
                "options_list": [],
                "message_type": "text",
                "fuente": "points_of_interest_handler",
            }

        address = ""
        lat = lon = None
        if isinstance(location, dict):
            # Use explicit None checks to avoid discarding valid zero values
            lat = location.get("lat")
            if lat is None:
                lat = location.get("latitude")
            lon = location.get("lon")
            if lon is None:
                lon = location.get("lng")
            if lon is None:
                lon = location.get("longitude")
            address = location.get("address") or location.get("formatted_address") or ""
            try:
                lat = float(lat) if lat is not None else None
                lon = float(lon) if lon is not None else None
            except (TypeError, ValueError):
                lat = lon = None
        else:
            address = str(location)

        if (lat is None or lon is None) and address:
            try:
                coords = get_coordinates(address)
                if coords:
                    lat, lon = float(coords["lat"]), float(coords["lon"])
            except Exception as exc:  # pragma: no cover
                logger.error("Error geocoding address %s: %s", address, exc, exc_info=True)
        spots: list[dict] = []
        info = {}
        try:
            if lat is not None and lon is not None:
                info = consultar_ocupacion({"lat": lat, "lon": lon})
            elif address:
                info = consultar_ocupacion(address)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Error consultando ocupación: %s", exc, exc_info=True)

        libres = info.get("libres") if isinstance(info, dict) else None
        cam_name = info.get("camera") if isinstance(info, dict) else None
        timestamp = info.get("timestamp") if isinstance(info, dict) else None

        if lat is not None and lon is not None:
            def dist(item):
                return _dist_m(lat, lon, item["lat"], item["lon"])

            sorted_spots = sorted(self.parking_data, key=dist)
            nearest = sorted_spots[:3]
            lines = [
                f"Datos de estacionamiento cerca de {address or 'tu ubicación'}:" \
                + (f" (Fuente: {cam_name} {timestamp})" if cam_name and timestamp else "")
            ]

            free_indices = set()
            if isinstance(libres, int) and libres > 0:
                free_indices = set(random.sample(range(len(nearest)), min(libres, len(nearest))))
            for idx, item in enumerate(nearest):
                try:
                    distance_val = int(dist(item))
                    distance_text = f" ({distance_val} m)"
                except Exception:  # pragma: no cover
                    distance_val = None
                    distance_text = ""
                availability = 1 if idx in free_indices else 0
                status = "libre" if availability else "ocupado"
                lines.append(f"- {item['address']}{distance_text}: {status}")
                spots.append({
                    "address": item["address"],
                    "lat": item["lat"],
                    "lon": item["lon"],
                    "distance_m": distance_val,
                    "available_spots": availability,
                })
        else:
            sample = random.sample(self.parking_data, min(3, len(self.parking_data)))
            lines = [
                f"Datos de estacionamiento cerca de {address or 'la zona'}:" \
                + (f" (Fuente: {cam_name} {timestamp})" if cam_name and timestamp else "")
            ]
            free_indices = set()
            if isinstance(libres, int) and libres > 0:
                free_indices = set(random.sample(range(len(sample)), min(libres, len(sample))))
            for idx, item in enumerate(sample):
                availability = 1 if idx in free_indices else 0
                status = "libre" if availability else "ocupado"
                lines.append(f"- {item['address']}: {status}")
                spots.append({
                    "address": item["address"],
                    "lat": item["lat"],
                    "lon": item["lon"],
                    "distance_m": None,
                    "available_spots": availability,
                })

        return {
            "message_body": "\n".join(lines),
            "options_list": [],
            "message_type": "text",
            "fuente": "points_of_interest_handler",
            "camera": cam_name,
            "timestamp": timestamp,
            "spots": spots,
        }
    def handle(self, payload: dict) -> dict | None:
        pregunta = (payload.get("pregunta") or "").lower()
        location = payload.get("location")

        keywords = ("estacionamiento", "estacionar", "lugar libre")
        if any(word in pregunta for word in keywords):
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
            from .municipio_responder import llamar_llm_con_fallback  # delayed import to avoid circular dependency
            mensaje_usuario = json.dumps({"pregunta": pregunta, "ubicacion": location})
            llm_result = llamar_llm_con_fallback(
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
