import json
import logging
import unicodedata
from pathlib import Path

from sqlalchemy.orm.attributes import flag_modified

from .herramientas_municipio import TOOL_REGISTRY
from .estacionamiento_utils import _dist_m
from .google_maps_service import get_coordinates
from .estacionamiento_service import consultar_ocupacion
from .conversation_state import ConversationState

CONTEXTO_MUNICIPIO = "contexto_municipio_v2"

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

    # ------------------------------------------------------------------
    # Parking helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_text(value: str | None) -> str:
        """Return a lowercase, accent-free string for fuzzy comparisons."""

        if not value:
            return ""
        normalized = unicodedata.normalize("NFKD", value)
        without_accents = "".join(
            char for char in normalized if not unicodedata.combining(char)
        )
        return without_accents.lower().strip()

    @classmethod
    def _extract_street_and_number(cls, address: str | None) -> tuple[str | None, int | None]:
        """Extract street name and house number from an address string."""

        if not address:
            return None, None
        first_segment = str(address).split(",")[0].strip()
        if not first_segment:
            return None, None

        tokens = first_segment.split()
        number = None
        if tokens:
            last_token = tokens[-1]
            if last_token.isdigit():
                try:
                    number = int(last_token)
                    tokens = tokens[:-1]
                except ValueError:  # pragma: no cover - defensive
                    number = None
            elif last_token.replace("°", "").isdigit():
                try:
                    number = int(last_token.replace("°", ""))
                    tokens = tokens[:-1]
                except ValueError:  # pragma: no cover - defensive
                    number = None
        street = " ".join(tokens).strip() or first_segment
        return street, number

    def _spots_matching_address(self, address: str, limit: int = 5) -> list[dict]:
        """Return parking spots that closely match the provided address."""

        street, number = self._extract_street_and_number(address)
        if not street:
            return []

        normalized_street = self._normalize_text(street)
        if not normalized_street:
            return []

        ranked: list[tuple[tuple[float, float, float, float], dict]] = []
        for item in self.parking_data:
            spot_street, spot_number = self._extract_street_and_number(item.get("address"))
            if not spot_street:
                continue
            normalized_spot = self._normalize_text(spot_street)
            if not normalized_spot:
                continue

            match_priority = None
            if normalized_spot == normalized_street:
                match_priority = 0
            elif normalized_street in normalized_spot or normalized_spot in normalized_street:
                match_priority = 1
            elif normalized_street and normalized_street in self._normalize_text(item.get("address")):
                match_priority = 2

            if match_priority is None:
                continue

            diff = float("inf")
            if number is not None and spot_number is not None:
                diff = abs(number - spot_number)

            available = item.get("available_spots") or 0
            identifier = (
                float(item.get("id"))
                if isinstance(item.get("id"), (int, float))
                else float("inf")
            )
            ranked.append(((match_priority, diff, -available, identifier), item))

        ranked.sort(key=lambda element: element[0])
        unique = []
        seen_ids = set()
        for _, spot in ranked:
            key = (
                spot.get("id"),
                spot.get("lat"),
                spot.get("lon"),
            )
            if key in seen_ids:
                continue
            seen_ids.add(key)
            unique.append(spot)
            if len(unique) >= limit:
                break
        return unique

    @staticmethod
    def _merge_spot_lists(primary: list[dict], secondary: list[dict], limit: int) -> list[dict]:
        """Combine two lists of parking spots without duplicates."""

        combined: list[dict] = []
        seen = set()
        for sequence in (primary, secondary):
            for spot in sequence:
                key = (
                    spot.get("id"),
                    spot.get("lat"),
                    spot.get("lon"),
                )
                if key in seen:
                    continue
                seen.add(key)
                combined.append(spot)
                if len(combined) >= limit:
                    return combined
        return combined

    def _parking_response(self, location: dict | str) -> dict:
        """Generate a parking response based on coordinates or an address."""
        if not self.parking_data:
            from .municipio_responder import _message_with_menu
            payload = _message_with_menu(
                "No tengo datos de estacionamiento disponibles en este momento.",
                self.context,
            )
            payload["fuente"] = "points_of_interest_handler"
            return payload

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

        matching_spots = self._spots_matching_address(address) if address else []

        if lat is not None and lon is not None:
            def dist(item: dict) -> float:
                return _dist_m(lat, lon, item["lat"], item["lon"])

            sorted_spots = sorted(self.parking_data, key=dist)
            prioritized = sorted(matching_spots, key=dist) if matching_spots else []
            nearest = self._merge_spot_lists(prioritized, sorted_spots, 3)
        else:
            if matching_spots:
                nearest = matching_spots[:3]
            else:
                nearest = sorted(
                    self.parking_data,
                    key=lambda item: (-(item.get("available_spots") or 0), item.get("id") or 0),
                )[:3]

        if not nearest:
            nearest = self.parking_data[:3]

        lines = [
            f"Datos de estacionamiento cerca de {address or 'tu ubicación'}:" \
            + (f" (Fuente: {cam_name} {timestamp})" if cam_name and timestamp else "")
        ]

        remaining_libres = libres if isinstance(libres, int) and libres >= 0 else None

        for item in nearest:
            distance_val = None
            distance_text = ""
            if lat is not None and lon is not None:
                try:
                    distance_val = int(_dist_m(lat, lon, item["lat"], item["lon"]))
                    if distance_val >= 1000:
                        distance_text = f" ({distance_val / 1000:.1f} km)"
                    elif distance_val > 0:
                        distance_text = f" ({distance_val} m)"
                except Exception:  # pragma: no cover
                    distance_val = None

            dataset_available = int(item.get("available_spots") or 0)
            available_now = dataset_available
            if remaining_libres is not None:
                if dataset_available > 0:
                    available_now = min(dataset_available, remaining_libres)
                else:
                    available_now = 0
                remaining_libres = max((remaining_libres or 0) - available_now, 0)

            if available_now > 0:
                if available_now == 1:
                    status = "libre (1 lugar disponible)"
                else:
                    status = f"libre ({available_now} lugares disponibles)"
            else:
                status = "ocupado"

            lines.append(f"- {item['address']}{distance_text}: {status}")
            spots.append({
                "address": item["address"],
                "lat": item["lat"],
                "lon": item["lon"],
                "distance_m": distance_val,
                "available_spots": available_now,
                "capacity": dataset_available,
            })

        message_text = "\n".join(lines)
        try:
            from .municipio_responder import _message_with_menu  # local import to avoid circular dependency
            base_payload = _message_with_menu(message_text, self.context)
        except ImportError:  # pragma: no cover - fallback for circular imports in isolated tests
            base_payload = {"message_body": message_text}
        base_payload.update({
            "fuente": "points_of_interest_handler",
            "camera": cam_name,
            "timestamp": timestamp,
            "spots": spots,
        })
        return base_payload
    def handle(self, payload: dict) -> dict | None:
        original_question = payload.get("pregunta") or ""
        pregunta = original_question.lower()
        location = payload.get("location")

        keywords = ("estacionamiento", "estacionar", "lugar libre")
        if any(word in pregunta for word in keywords):
            municipio_ctx = (
                self.context.get("chat_db_context_data", {})
                .setdefault(CONTEXTO_MUNICIPIO, {})
            )
            chat_db_context = self.context.get("chat_db_context")

            if not location:
                # Try to geocode an address present in the question text
                address_candidate = pregunta
                for word in keywords:
                    address_candidate = address_candidate.replace(word, "")
                address_candidate = address_candidate.strip(",.;:- ")
                coords = None
                if len(address_candidate) > 3:
                    try:
                        coords = get_coordinates(address_candidate)
                    except Exception:  # pragma: no cover - defensive
                        coords = None
                if coords:
                    location = {
                        "address": address_candidate,
                        "lat": coords.get("lat"),
                        "lon": coords.get("lon"),
                    }
                    municipio_ctx["ultima_consulta_poi"] = original_question
                    if chat_db_context:
                        flag_modified(chat_db_context, "context_data")
                    return self._parking_response(location)

                # Geocoding failed; remember query and ask for location
                municipio_ctx["estado_conversacion"] = (
                    ConversationState.ESPERANDO_UBICACION_GENERAL.name
                )
                municipio_ctx["consulta_pendiente_ubicacion"] = original_question
                municipio_ctx["ultima_consulta_poi"] = original_question
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return {
                    "message_body": (
                        "Para encontrar estacionamiento libre, por favor compartí tu "
                        "ubicación o escribí una dirección."
                    ),
                    "options_list": [
                        {"texto": "Compartir ubicación", "action": "compartir_ubicacion"},
                        {"texto": "Cancelar", "action": "cancelar"},
                    ],
                    "message_type": "interactive_buttons",
                    "fuente": "pedir_ubicacion_estacionamiento",
                }

            municipio_ctx["ultima_consulta_poi"] = original_question
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
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

        accion = respuesta_llm.get("accion_backend")
        message_body = respuesta_llm.get("message_body", "")
        botones = respuesta_llm.get("botones", [])

        if accion == "ejecutar_herramienta":
            datos = respuesta_llm.get("datos_estructura", {})
            nombre = datos.get("nombre_herramienta")
            params = datos.get("parametros_herramienta", {})
            herramienta = TOOL_REGISTRY.get(nombre)
            if not herramienta:
                from .municipio_responder import _message_with_menu
                final_payload = _message_with_menu(message_body or "No tengo una herramienta para eso.", self.context)
                final_payload["fuente"] = "points_of_interest_handler"
                return final_payload
            try:
                resultado = herramienta["funcion"](**params)
                if isinstance(resultado, dict):
                    result_text = resultado.get("texto") or resultado.get("message_body") or str(resultado)
                else:
                    result_text = str(resultado)
                final_message = f"{message_body}\n{result_text}".strip() or result_text
                from .municipio_responder import _message_with_menu
                final_payload = _message_with_menu(final_message, self.context)
                final_payload["fuente"] = "points_of_interest_handler"
                return final_payload
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Error executing tool %s: %s", nombre, exc, exc_info=True)
                from .municipio_responder import _message_with_menu
                final_payload = _message_with_menu("Ocurrió un error al obtener la información solicitada.", self.context)
                final_payload["fuente"] = "points_of_interest_handler"
                return final_payload

        from .municipio_responder import _message_with_menu
        final_payload = _message_with_menu(message_body, self.context)
        final_payload["fuente"] = "points_of_interest_handler"
        return final_payload
